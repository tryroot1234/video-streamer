import asyncio
import glob
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from video_scanner import scan_videos
from transcoder import get_or_start_transcode, get_or_create_seekable, get_seekable_job, generate_full_m3u8
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


def cleanup_orphaned_seek_files():
    """清理缓存目录中孤立的 seek 分片文件"""
    cache_dir = Path(config.get("cache_dir"))
    if not cache_dir.exists():
        return
    cleaned = 0
    for seek_ts in cache_dir.rglob("seek_*_*.ts"):
        seek_ts.unlink(missing_ok=True)
        cleaned += 1
    for seek_m3u8 in cache_dir.rglob("seek_*.m3u8"):
        seek_m3u8.unlink(missing_ok=True)
        cleaned += 1
    if cleaned:
        print(f"Cleaned up {cleaned} orphaned seek files")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.load_settings()
    Path(config.get("cache_dir")).mkdir(parents=True, exist_ok=True)
    cleanup_old_processes()
    cleanup_orphaned_seek_files()
    asyncio.get_event_loop().run_in_executor(None, refresh_videos)
    yield

app = FastAPI(title="Video Streamer", lifespan=lifespan)

_video_cache: dict[str, dict] = {}
_last_scan: float = 0
_scan_interval = 60


def refresh_videos(force: bool = False):
    global _video_cache, _last_scan
    if force or time.time() - _last_scan > _scan_interval:
        videos = scan_videos()
        _video_cache = {v["id"]: v for v in videos}
        _last_scan = time.time()
    return list(_video_cache.values())


# ---------- Settings ----------

class SettingsUpdate(BaseModel):
    video_dir: str | None = None
    cache_dir: str | None = None
    max_cache_size_gb: int | None = None


@app.get("/api/settings")
async def api_get_settings():
    return config.get_all()


@app.put("/api/settings")
async def api_update_settings(body: SettingsUpdate):
    old_video_dir = config.get("video_dir")
    updated = config.update(body.model_dump(exclude_none=True))
    if updated["video_dir"] != old_video_dir:
        refresh_videos(force=True)
    return updated


# ---------- Videos ----------

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "static" / "home.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@app.get("/library", response_class=HTMLResponse)
async def library():
    html_path = Path(__file__).parent / "static" / "library.html"
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


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
        job = get_or_start_transcode(video["path"], video_id, quality)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, job.wait_ready, 30.0)

    # 动态生成 m3u8（支持 start 参数从指定位置截断）
    m3u8 = generate_full_m3u8(video_id, quality, video.get("duration", 0), start=start)
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

    loop = asyncio.get_event_loop()
    seek_job = await loop.run_in_executor(
        None, lambda: get_or_create_seekable(video["path"], video_id, quality)
    )
    ok = await loop.run_in_executor(None, lambda: seek_job.start_seek(position))
    return {"ok": ok}


@app.get("/api/video/{video_id}/stream/{quality}/segments-ready")
async def api_segments_ready(video_id: str, quality: str, seek: float = 0):
    """检查 seek 转码的分片是否就绪"""
    seek_job = get_seekable_job(video_id, quality)
    if not seek_job:
        return {"ready": False, "segments": 0}
    if seek > 0:
        return {"ready": seek_job.seek_ready, "segments": seek_job.get_seek_segment_count()}
    return {"ready": False, "segments": 0}


@app.get("/api/video/{video_id}/stream/{quality}/{segment}")
async def api_segment(video_id: str, quality: str, segment: str):
    from config import HLS_SEGMENT_TIME
    seg_path = Path(config.get("cache_dir")) / video_id / quality / segment
    if not seg_path.exists():
        # 尝试查找对应的 seek 分片（绝对编号 → seek 文件）
        m = re.match(r"seg_(\d+)\.ts", segment)
        if m:
            abs_idx = int(m.group(1))
            seek_job = get_seekable_job(video_id, quality)
            if seek_job and seek_job.seek_position > 0:
                seek_start = int(seek_job.seek_position) // HLS_SEGMENT_TIME
                seek_idx = abs_idx - seek_start
                if seek_idx >= 0:
                    seek_prefix = f"seek_{int(seek_job.seek_position)}"
                    seek_path = seg_path.parent / f"{seek_prefix}_{seek_idx:05d}.ts"
                    if seek_path.exists():
                        return FileResponse(seek_path, media_type="video/mp2t")
        # 返回 200 空响应，hls.js 视为 gap 而非 error，避免触发 recoverMediaError
        return Response(content=b"", status_code=200, media_type="video/mp2t")
    return FileResponse(seg_path, media_type="video/mp2t")


@app.get("/api/video/{video_id}/thumbnail")
async def api_thumbnail(video_id: str):
    refresh_videos()
    video = _video_cache.get(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    thumb_path = Path(config.get("cache_dir")) / video_id / "thumb.jpg"
    if not thumb_path.exists():
        import subprocess
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ffmpeg", "-y", "-i", video["path"], "-ss", "5", "-vframes", "1",
             "-vf", "scale=320:-1", str(thumb_path)],
            capture_output=True, timeout=15
        )
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


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


if __name__ == "__main__":
    import uvicorn
    config.load_settings()
    print(f"Starting Video Streamer on http://{config.HOST}:{config.PORT}")
    print(f"Video directory: {config.get('video_dir')}")
    print(f"Cache directory: {config.get('cache_dir')}")
    uvicorn.run(app, host=config.HOST, port=config.PORT)
