import asyncio
import glob
import json
import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from pydantic import BaseModel

import config
from video_scanner import scan_videos
from transcoder import get_or_start_transcode, generate_full_m3u8
from cache_manager import is_cached


def cleanup_old_processes():
    """清理上一次服务器遗留的 ffmpeg 进程"""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "ffmpeg.*videotoolbox"],
            capture_output=True, text=True
        )
        if result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            for pid in pids:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass
            print(f"Cleaned up {len(pids)} old ffmpeg processes")
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.load_settings()
    Path(config.get("cache_dir")).mkdir(parents=True, exist_ok=True)
    cleanup_old_processes()
    # 刷新视频列表后自动启动预转码
    def _init_with_pretranscode():
        videos = refresh_videos()
        if videos:
            from cache_manager import start_auto_pretranscode
            start_auto_pretranscode(videos)
    asyncio.get_event_loop().run_in_executor(None, _init_with_pretranscode)
    yield

app = FastAPI(title="Video Streamer", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)

_video_cache: dict[str, dict] = {}
_last_scan: float = 0
_scan_interval = 60
_scanning: bool = False

# ---------- HTML ----------
_static_dir = Path(__file__).parent / "static"


def refresh_videos(force: bool = False):
    global _video_cache, _last_scan, _scanning
    now = time.time()

    # 缓存未过期 → 直接返回
    if not force and now - _last_scan < _scan_interval and _video_cache:
        return list(_video_cache.values())

    # 正在扫描中 → 返回旧数据
    if _scanning and _video_cache:
        return list(_video_cache.values())

    # 冷启动（无缓存）→ 同步等待首次扫描
    if not _video_cache:
        videos = scan_videos()
        _video_cache = {v["id"]: v for v in videos}
        _last_scan = now
        return list(_video_cache.values())

    # 缓存过期 → 后台刷新，立即返回旧数据
    _scanning = True
    def _bg_scan():
        global _video_cache, _last_scan, _scanning
        try:
            videos = scan_videos()
            _video_cache = {v["id"]: v for v in videos}
            _last_scan = time.time()
        finally:
            _scanning = False
    threading.Thread(target=_bg_scan, daemon=True).start()
    return list(_video_cache.values())


# ---------- Settings ----------

class SettingsUpdate(BaseModel):
    video_dirs: list[str] | None = None
    cache_dir: str | None = None
    max_cache_size_gb: int | None = None
    max_concurrent_transcode: int | None = None


@app.get("/api/settings")
async def api_get_settings():
    return config.get_all()


@app.put("/api/settings")
async def api_update_settings(body: SettingsUpdate):
    old_video_dirs = config.get("video_dirs") or []
    old_concurrent = config.get("max_concurrent_transcode")
    updated = config.update(body.model_dump(exclude_none=True))
    if updated.get("video_dirs") != old_video_dirs:
        refresh_videos(force=True)
        from cache_manager import stop_pretranscode, start_auto_pretranscode
        stop_pretranscode()
        videos = list(_video_cache.values())
        if videos:
            start_auto_pretranscode(videos)
    if updated.get("max_concurrent_transcode") != old_concurrent:
        from cache_manager import update_concurrent_semaphore
        update_concurrent_semaphore()
    return updated


# ---------- Videos ----------

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((_static_dir / "home.html").read_text(encoding="utf-8"))


@app.get("/library", response_class=HTMLResponse)
async def library():
    return HTMLResponse((_static_dir / "library.html").read_text(encoding="utf-8"))


@app.get("/api/videos")
async def api_videos():
    videos = await asyncio.get_event_loop().run_in_executor(None, refresh_videos)
    return {"videos": videos}


@app.get("/api/video/{video_id}/info")
async def api_video_info(video_id: str):
    refresh_videos()
    video = _video_cache.get(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def rewrite_m3u8(content: str, video_id: str, quality: str) -> str:
    prefix = f"/api/video/{video_id}/stream/{quality}/"
    content = re.sub(r"(seg_\d+\.ts)", prefix + r"\1", content)
    return content


@app.get("/api/video/{video_id}/stream/{quality}")
async def api_stream(video_id: str, quality: str, request: Request, start: float = 0):
    refresh_videos()
    video = _video_cache.get(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # 如果没有足够分片，启动转码并等待
    if not is_cached(video_id, quality, video.get("duration")):
        job = get_or_start_transcode(video["path"], video_id, quality,
                                     source_bitrate_kbps=video.get("bitrate", 0))
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, job.wait_ready, 30.0)

    # 动态生成 VOD m3u8（始终返回完整时间线，hls.js 自动处理缺失分片）
    m3u8 = generate_full_m3u8(video_id, quality, video.get("duration", 0), start=start, is_live=False)
    return Response(
        content=m3u8,
        media_type="application/vnd.apple.mpegurl",
    )


@app.post("/api/video/{video_id}/seek/{quality}")
async def api_seek(video_id: str, quality: str, request: Request):
    """从指定位置开始新的转码"""
    body = await request.json()
    position = body.get("position", 0)
    if position <= 0:
        return {"ok": False, "msg": "Invalid position"}

    refresh_videos()
    video = _video_cache.get(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    job = get_or_start_transcode(video["path"], video_id, quality,
                                 source_bitrate_kbps=video.get("bitrate", 0))
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, lambda: job.start_seek(position))
    return {"ok": ok}


@app.get("/api/video/{video_id}/stream/{quality}/segments-ready")
async def api_segments_ready(video_id: str, quality: str, seek: float = 0):
    """检查指定位置的分片是否就绪"""
    if seek <= 0:
        return {"ready": False, "segments": 0}
    from config import HLS_SEGMENT_TIME
    seg_idx = int(seek) // HLS_SEGMENT_TIME
    seg_path = Path(config.get("cache_dir")) / video_id / quality / f"seg_{seg_idx:05d}.ts"
    return {"ready": seg_path.exists(), "segments": 1 if seg_path.exists() else 0}


@app.get("/api/video/{video_id}/stream/{quality}/{segment}")
async def api_segment(video_id: str, quality: str, segment: str):
    seg_path = Path(config.get("cache_dir")) / video_id / quality / segment
    if seg_path.exists():
        return FileResponse(seg_path, media_type="video/mp2t")
    return Response(content=b"", status_code=200, media_type="video/mp2t")


@app.get("/api/video/{video_id}/sprite")
async def api_sprite(video_id: str):
    refresh_videos()
    video = _video_cache.get(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    sprite_path = Path(config.get("cache_dir")) / video_id / "sprite.jpg"
    meta_path = Path(config.get("cache_dir")) / video_id / "sprite.json"
    if sprite_path.exists() and meta_path.exists():
        meta = json.loads(meta_path.read_text())
        return FileResponse(sprite_path, media_type="image/jpeg",
                            headers={"X-Sprite-Thumb-W": str(meta["thumb_w"]),
                                     "X-Sprite-Thumb-H": str(meta["thumb_h"]),
                                     "X-Sprite-Cols": str(meta["cols"]),
                                     "X-Sprite-Interval": str(meta["interval"])})

    duration = video.get("duration", 0)
    if duration <= 0:
        raise HTTPException(status_code=400, detail="Unknown duration")

    thumb_w, thumb_h = 160, 90
    cols = 10
    interval = max(10, int(duration / 60))
    total_frames = max(1, int(duration / interval))
    rows = math.ceil(total_frames / cols)

    sprite_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = sprite_path.parent / "_sprite_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # 两步法：先并行提取缩略图，再拼接 sprite（避免 tile 滤镜缓冲所有帧）
    async def _grab_thumb(idx: int):
        ts = idx * interval
        out = tmp_dir / f"t_{idx:04d}.jpg"
        if out.exists():
            return
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", str(ts), "-i", video["path"],
            "-vframes", "1", "-vf", f"scale={thumb_w}:{thumb_h}",
            "-q:v", "5", str(out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()

    # 并行提取（最多 8 路并发）
    sem = asyncio.Semaphore(8)
    async def _grab_with_sem(idx):
        async with sem:
            await _grab_thumb(idx)
    await asyncio.gather(*[_grab_with_sem(i) for i in range(total_frames)])

    # 拼接 sprite（用 concat demuxer，避免 tile+多 -i 出黑图）
    inputs = []
    for i in range(total_frames):
        p = tmp_dir / f"t_{i:04d}.jpg"
        if p.exists():
            inputs.append(p)
    if not inputs:
        raise HTTPException(status_code=404, detail="Sprite generation failed")

    actual_rows = math.ceil(len(inputs) / cols)
    concat_file = tmp_dir / "concat.txt"
    concat_file.write_text("".join(f"file '{p.name}'\n" for p in inputs))
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat_file),
        "-filter_complex", f"tile={cols}x{actual_rows}",
        "-frames:v", "1", "-q:v", "5", str(sprite_path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()

    # 清理临时文件
    shutil.rmtree(tmp_dir, ignore_errors=True)

    if sprite_path.exists():
        meta = {"thumb_w": thumb_w, "thumb_h": thumb_h, "cols": cols, "interval": interval}
        meta_path.write_text(json.dumps(meta))
        return FileResponse(sprite_path, media_type="image/jpeg",
                            headers={"X-Sprite-Thumb-W": str(thumb_w),
                                     "X-Sprite-Thumb-H": str(thumb_h),
                                     "X-Sprite-Cols": str(cols),
                                     "X-Sprite-Interval": str(interval)})
    raise HTTPException(status_code=404, detail="Sprite generation failed")


@app.get("/api/video/{video_id}/thumbnail")
async def api_thumbnail(video_id: str):
    refresh_videos()
    video = _video_cache.get(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    thumb_path = Path(config.get("cache_dir")) / video_id / "thumb.jpg"
    if not thumb_path.exists():
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        # 跳到视频 10% 处抓帧，避开片头黑屏
        seek_to = max(1, int(video.get("duration", 60) * 0.1))
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-ss", str(seek_to), "-i", video["path"],
            "-vf", "scale=320:-1",
            "-frames:v", "1", str(thumb_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
    if thumb_path.exists():
        return FileResponse(thumb_path, media_type="image/jpeg")
    raise HTTPException(status_code=404, detail="Thumbnail generation failed")


@app.get("/api/cache/status")
async def api_cache_status():
    from cache_manager import get_cache_size, get_disk_status
    return {"size_bytes": get_cache_size(), "disk": get_disk_status()}


@app.get("/api/disk/status")
async def api_disk_status():
    from cache_manager import get_disk_status, get_cache_size, can_cache_more
    disk = get_disk_status()
    cache_size = get_cache_size()
    max_bytes = config.get("max_cache_size_gb") * 1024 * 1024 * 1024
    can, reason = can_cache_more()
    return {
        **disk,
        "cache_size": cache_size,
        "max_cache_size": max_bytes,
        "can_cache_more": can,
        "stop_reason": reason,
    }


@app.post("/api/cache/init")
async def api_cache_init(request: Request):
    from cache_manager import start_batch_cache, get_batch_state
    body = await request.json()
    video_ids = body.get("video_ids", [])

    all_vids = refresh_videos()
    if video_ids:
        vid_map = {v["id"]: v for v in all_vids}
        videos = [vid_map[vid] for vid in video_ids if vid in vid_map]
    else:
        videos = all_vids

    if get_batch_state()["running"]:
        return {"ok": False, "msg": "批量缓存已在运行中"}
    ok = start_batch_cache(videos)
    return {"ok": ok}


@app.get("/api/video/{video_id}/transcode-progress")
async def api_transcode_progress(video_id: str):
    from cache_manager import get_video_progress, get_cached_qualities
    progress = get_video_progress(video_id)
    cached = get_cached_qualities(video_id)
    return {"video_id": video_id, "progress": progress, "cached_qualities": cached}


@app.post("/api/cache/stop")
async def api_cache_stop():
    from cache_manager import stop_batch_cache
    stop_batch_cache()
    return {"ok": True}


@app.post("/api/cache/clear-all")
async def api_clear_all_cache():
    from cache_manager import clear_all_cache
    return clear_all_cache()


@app.get("/api/cache/batch")
async def api_cache_batch():
    from cache_manager import get_batch_state
    return get_batch_state()


@app.post("/api/cache/pause")
async def api_cache_pause(request: Request):
    body = await request.json()
    video_id = body.get("video_id", "")
    from cache_manager import pause_batch_video
    ok = pause_batch_video(video_id)
    return {"ok": ok}


@app.post("/api/cache/resume")
async def api_cache_resume(request: Request):
    body = await request.json()
    video_id = body.get("video_id", "")
    from cache_manager import resume_batch_video
    ok = resume_batch_video(video_id)
    return {"ok": ok}


@app.get("/api/cache/active-progress")
async def api_active_progress():
    """返回所有活跃转码任务的进度（包括非批量缓存的）"""
    from transcoder import get_all_active_jobs
    from cache_manager import get_batch_state
    jobs = get_all_active_jobs()
    batch = get_batch_state()
    # Merge: batch state has more accurate progress for batch videos
    merged = {}
    for vid, prog in jobs.items():
        merged[vid] = prog
    for vid, prog in batch.get("video_progress", {}).items():
        if prog["status"] in ("caching", "paused", "done", "error"):
            merged[vid] = prog
    return {"video_progress": merged}


@app.get("/api/video/{video_id}/cache-status")
async def api_video_cache_status(video_id: str):
    from cache_manager import get_cached_qualities
    return {"video_id": video_id, "cached_qualities": get_cached_qualities(video_id)}


@app.post("/api/video/{video_id}/cache/clear")
async def api_clear_video_cache(video_id: str):
    from cache_manager import clear_video_cache
    return clear_video_cache(video_id)


@app.post("/api/cache/evict-and-start")
async def api_evict_and_start(video_id: str, quality: str = "720p"):
    """淘汰最旧缓存并开始转码指定视频"""
    from cache_manager import evict_oldest, check_disk_for_new_cache
    refresh_videos()
    video = _video_cache.get(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    ok, reason = check_disk_for_new_cache()
    if not ok:
        return {"ok": False, "msg": reason}

    evict_oldest()
    return {"ok": True}


@app.get("/api/pretranscode/status")
async def api_pretranscode_status():
    from cache_manager import get_pretranscode_state
    return get_pretranscode_state()


@app.post("/api/pretranscode/pause")
async def api_pretranscode_pause():
    from cache_manager import pause_pretranscode
    pause_pretranscode()
    return {"ok": True}


@app.post("/api/pretranscode/resume")
async def api_pretranscode_resume():
    from cache_manager import resume_pretranscode
    resume_pretranscode()
    return {"ok": True}


@app.post("/api/pretranscode/stop")
async def api_pretranscode_stop():
    from cache_manager import stop_pretranscode
    stop_pretranscode()
    return {"ok": True}


from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


class StaticCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "public, max-age=86400"
        return response


app.add_middleware(StaticCacheMiddleware)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


if __name__ == "__main__":
    import uvicorn
    config.load_settings()
    print(f"Starting Video Streamer on http://{config.HOST}:{config.PORT}")
    print(f"Video directories: {config.get('video_dirs')}")
    print(f"Cache directory: {config.get('cache_dir')}")
    uvicorn.run(app, host=config.HOST, port=config.PORT)
