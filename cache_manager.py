import math
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import config

def get_max_concurrent() -> int:
    return config.get("max_concurrent_transcode") or 1

_transcode_semaphore = threading.Semaphore(get_max_concurrent())


def update_concurrent_semaphore():
    """设置变更后更新信号量"""
    global _transcode_semaphore
    _transcode_semaphore = threading.Semaphore(get_max_concurrent())


def get_cache_size() -> int:
    total = 0
    cache = Path(config.get("cache_dir"))
    if cache.exists():
        for f in cache.rglob("*"):
            if f.is_file():
                total += f.stat().st_size
    return total


def get_disk_status() -> dict:
    cache_dir = Path(config.get("cache_dir"))
    try:
        usage = shutil.disk_usage(cache_dir)
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "free_percent": round(usage.free / usage.total * 100, 1),
            "used_percent": round(usage.used / usage.total * 100, 1),
        }
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "free_percent": 0, "used_percent": 0}


def can_cache_more() -> tuple[bool, str]:
    """检查是否可以继续缓存。返回 (是否可继续, 原因)"""
    disk = get_disk_status()
    cache_size = get_cache_size()
    max_bytes = config.get("max_cache_size_gb") * 1024 * 1024 * 1024

    # 规则: max_cache_size + 已用空间 <= 磁盘总容量 * 80%
    max_allowed = int(disk["total"] * 0.8) - (disk["used"] - cache_size)
    effective_max = min(max_bytes, max_allowed) if max_allowed > 0 else 0

    if cache_size >= effective_max:
        return False, f"已到达最大缓存大小 ({_fmt(effective_max)})"

    if disk["free_percent"] < 20:
        return False, f"磁盘可用空间不足 20% (剩余 {disk['free_percent']}%)，请尽快扩容"

    return True, ""


def check_disk_for_new_cache(size_needed: int = 0) -> tuple[bool, str]:
    """检查磁盘空间是否足够缓存新视频"""
    disk = get_disk_status()
    if disk["free_percent"] < 20:
        return False, f"磁盘可用空间不足 20% (剩余 {disk['free_percent']}%)，请先扩容磁盘空间"

    cache_size = get_cache_size()
    max_bytes = config.get("max_cache_size_gb") * 1024 * 1024 * 1024
    max_allowed = int(disk["total"] * 0.8) - (disk["used"] - cache_size)

    if cache_size >= min(max_bytes, max_allowed):
        return False, "已到达最大缓存限制"

    return True, ""


def cleanup_cache():
    cache_size = get_cache_size()
    max_bytes = config.get("max_cache_size_gb") * 1024 * 1024 * 1024
    if cache_size <= max_bytes:
        return

    cache = Path(config.get("cache_dir"))
    if not cache.exists():
        return

    video_dirs = sorted(
        [d for d in cache.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_atime
    )

    for d in video_dirs:
        shutil.rmtree(d, ignore_errors=True)
        cache_size = get_cache_size()
        if cache_size <= max_bytes * 0.8:
            break


def evict_oldest() -> bool:
    """淘汰最旧的缓存目录，为新视频腾出空间"""
    cache = Path(config.get("cache_dir"))
    if not cache.exists():
        return False

    video_dirs = sorted(
        [d for d in cache.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_atime
    )
    if not video_dirs:
        return False

    shutil.rmtree(video_dirs[0], ignore_errors=True)
    return True


def get_video_cache_dir(video_id: str, quality: str) -> Path:
    return Path(config.get("cache_dir")) / video_id / quality


def is_cached(video_id: str, quality: str, duration: float = None) -> bool:
    """检查视频是否已完整缓存。

    有 duration 时：检查最大 segment 编号是否覆盖完整时长。
    无 duration 时：回退到检查 playlist.m3u8 是否存在。
    """
    cache_dir = get_video_cache_dir(video_id, quality)
    if not cache_dir.exists():
        return False

    segs = list(cache_dir.glob("seg_*.ts"))
    if not segs:
        return False

    if duration:
        max_idx = 0
        for s in segs:
            m = re.match(r"seg_(\d+)\.ts", s.name)
            if m:
                max_idx = max(max_idx, int(m.group(1)))
        expected = math.ceil(duration / config.HLS_SEGMENT_TIME)
        return max_idx >= expected - 1

    playlist = cache_dir / "playlist.m3u8"
    return playlist.exists() and playlist.stat().st_size > 0


def get_cached_qualities(video_id: str) -> list[str]:
    """获取某个视频已缓存的画质列表"""
    cache_dir = Path(config.get("cache_dir")) / video_id
    if not cache_dir.exists():
        return []
    qualities = []
    for d in cache_dir.iterdir():
        if d.is_dir() and (d / "playlist.m3u8").exists():
            qualities.append(d.name)
    return qualities


def clear_all_cache() -> dict:
    """清除所有视频缓存，保留缩略图"""
    from transcoder import invalidate_all_jobs
    cache = Path(config.get("cache_dir"))
    if not cache.exists():
        return {"ok": True, "freed": 0, "msg": "无缓存"}

    freed = 0
    for video_dir in cache.iterdir():
        if not video_dir.is_dir():
            continue
        for q_dir in video_dir.iterdir():
            if not q_dir.is_dir():
                continue
            for f in q_dir.iterdir():
                if f.is_file():
                    freed += f.stat().st_size
                    f.unlink(missing_ok=True)
            q_dir.rmdir()
    invalidate_all_jobs()
    stop_batch_cache()
    stop_pretranscode()
    return {"ok": True, "freed": freed}


def clear_video_cache(video_id: str) -> dict:
    """清除指定视频的所有缓存分片，保留缩略图"""
    from transcoder import invalidate_jobs
    cache_dir = Path(config.get("cache_dir")) / video_id
    if not cache_dir.exists():
        invalidate_jobs(video_id)
        return {"ok": True, "freed": 0, "msg": "无缓存"}

    freed = 0
    for d in cache_dir.iterdir():
        if d.is_dir():
            for f in d.iterdir():
                if f.is_file():
                    freed += f.stat().st_size
                    f.unlink(missing_ok=True)
            d.rmdir()
    invalidate_jobs(video_id)
    return {"ok": True, "freed": freed}


# ---------- 批量缓存 ----------

_batch_state = {
    "running": False,
    "total": 0,
    "done": 0,
    "current": "",
    "current_video_id": "",
    "video_progress": {},  # {video_id: {"percent": 0-100, "status": "caching"|"done"|"error"|"pending"}}
    "stopped_reason": "",
    "errors": [],
}
_batch_lock = threading.Lock()


def get_batch_state() -> dict:
    with _batch_lock:
        return dict(_batch_state)


def get_video_progress(video_id: str) -> dict:
    with _batch_lock:
        return _batch_state["video_progress"].get(video_id, {"percent": 0, "status": "pending"})


def start_batch_cache(videos: list[dict]):
    """启动批量缓存，按传入的视频列表顺序执行"""
    with _batch_lock:
        if _batch_state["running"]:
            return False
        progress = {}
        for v in videos:
            if is_cached(v["id"], v.get("recommended_quality", "720p"), v.get("duration")):
                progress[v["id"]] = {"percent": 100, "status": "done"}
            else:
                progress[v["id"]] = {"percent": 0, "status": "pending"}
        _batch_state.update({
            "running": True,
            "total": len(videos),
            "done": 0,
            "current": "",
            "current_video_id": "",
            "video_progress": progress,
            "stopped_reason": "",
            "errors": [],
        })

    def _transcode_one(v: dict):
        from transcoder import get_or_start_transcode
        vid = v["id"]
        quality = v.get("recommended_quality", "720p")

        if is_cached(vid, quality, v.get("duration")):
            with _batch_lock:
                _batch_state["video_progress"][vid] = {"percent": 100, "status": "done"}
                _batch_state["done"] += 1
            return

        with _batch_lock:
            if not _batch_state["running"]:
                _batch_state["video_progress"][vid] = {"percent": 0, "status": "pending"}
                return
            _batch_state["video_progress"][vid] = {"percent": 0, "status": "caching"}

        can, reason = can_cache_more()
        if not can:
            with _batch_lock:
                _batch_state["video_progress"][vid] = {"percent": 0, "status": "error"}
                _batch_state["errors"].append(f"{v['name']}: {reason}")
                _batch_state["done"] += 1
            return

        # 获取信号量后启动转码，启动完成后立即释放
        _transcode_semaphore.acquire()
        try:
            job = get_or_start_transcode(v["path"], vid, quality)
        finally:
            _transcode_semaphore.release()

        # 监控进度（信号量已释放，其他任务可以并发）
        try:
            estimated_total = max(1, int(v.get("duration", 0) / config.HLS_SEGMENT_TIME))
            while job.is_alive():
                with _batch_lock:
                    if not _batch_state["running"]:
                        break
                if not job.paused:
                    seg_count = job._segment_count()
                    pct = min(99, int(seg_count / estimated_total * 100))
                    with _batch_lock:
                        if _batch_state["video_progress"].get(vid, {}).get("status") != "paused":
                            _batch_state["video_progress"][vid] = {"percent": pct, "status": "caching"}
                time.sleep(1)

            if job.error:
                with _batch_lock:
                    _batch_state["video_progress"][vid] = {"percent": 0, "status": "error"}
                    _batch_state["errors"].append(v["name"])
            else:
                with _batch_lock:
                    _batch_state["video_progress"][vid] = {"percent": 100, "status": "done"}
        except Exception as e:
            with _batch_lock:
                _batch_state["video_progress"][vid] = {"percent": 0, "status": "error"}
                _batch_state["errors"].append(f"{v['name']}: {e}")

        with _batch_lock:
            _batch_state["done"] += 1

    def _run():
        with ThreadPoolExecutor(max_workers=get_max_concurrent()) as pool:
            futures = {pool.submit(_transcode_one, v): v for v in videos}
            for future in as_completed(futures):
                with _batch_lock:
                    if not _batch_state["running"]:
                        break
                try:
                    future.result()
                except Exception:
                    pass

        with _batch_lock:
            _batch_state["running"] = False
            _batch_state["current_video_id"] = ""
            if not _batch_state["stopped_reason"]:
                _batch_state["stopped_reason"] = "缓存完成"

    threading.Thread(target=_run, daemon=True).start()
    return True


def stop_batch_cache():
    with _batch_lock:
        _batch_state["running"] = False


def _find_job(video_id: str):
    from transcoder import get_job
    # Try recommended quality first
    job = get_job(video_id, "720p")
    if not job:
        # Try other qualities
        for q in ("1080p", "480p", "360p"):
            job = get_job(video_id, q)
            if job and not job.finished:
                break
            job = None
    return job


def pause_batch_video(video_id: str) -> bool:
    with _batch_lock:
        prog = _batch_state["video_progress"].get(video_id)
        if not prog or prog["status"] != "caching":
            return False
    job = _find_job(video_id)
    if job and job.pause():
        with _batch_lock:
            if video_id in _batch_state["video_progress"]:
                _batch_state["video_progress"][video_id]["status"] = "paused"
        return True
    return False


def resume_batch_video(video_id: str) -> bool:
    with _batch_lock:
        prog = _batch_state["video_progress"].get(video_id)
        if not prog or prog["status"] != "paused":
            return False
    job = _find_job(video_id)
    if job and job.resume():
        with _batch_lock:
            if video_id in _batch_state["video_progress"]:
                _batch_state["video_progress"][video_id]["status"] = "caching"
        return True
    return False


def _fmt(b: int) -> str:
    if b >= 1073741824:
        return f"{b / 1073741824:.1f} GB"
    return f"{b / 1048576:.0f} MB"


# ---------- 自动预转码 ----------

_pretranscode_state = {
    "running": False,
    "queue": [],           # 待转码视频列表
    "current": "",         # 当前正在转码的视频名
    "current_video_id": "",
    "done": 0,
    "total": 0,
    "paused": False,       # 暂停（用户正在播放时暂停）
}
_pretranscode_lock = threading.Lock()
_pretranscode_stop_event = threading.Event()


def get_pretranscode_state() -> dict:
    with _pretranscode_lock:
        return dict(_pretranscode_state)


def stop_pretranscode():
    _pretranscode_stop_event.set()
    with _pretranscode_lock:
        _pretranscode_state["running"] = False


def pause_pretranscode():
    with _pretranscode_lock:
        _pretranscode_state["paused"] = True


def resume_pretranscode():
    with _pretranscode_lock:
        _pretranscode_state["paused"] = False


def start_auto_pretranscode(videos: list[dict]):
    """启动自动预转码（后台单线程，顺序执行，最低优先级）"""
    with _pretranscode_lock:
        if _pretranscode_state["running"]:
            return False
        if _batch_state["running"]:
            return False

        # 筛选未缓存的视频
        queue = []
        for v in videos:
            quality = v.get("recommended_quality", "720p")
            if not is_cached(v["id"], quality, v.get("duration")):
                queue.append(v)

        if not queue:
            return False

        _pretranscode_stop_event.clear()
        _pretranscode_state.update({
            "running": True,
            "queue": [v["name"] for v in queue],
            "current": "",
            "current_video_id": "",
            "done": 0,
            "total": len(queue),
            "paused": False,
        })

    def _run():
        from transcoder import get_or_start_transcode

        # 批量提交到 worker 池（池自动限制并发）
        jobs = []
        for v in queue:
            if _pretranscode_stop_event.is_set():
                break
            vid = v["id"]
            quality = v.get("recommended_quality", "720p")
            if is_cached(vid, quality, v.get("duration")):
                with _pretranscode_lock:
                    _pretranscode_state["done"] += 1
                continue
            can, reason = can_cache_more()
            if not can:
                print(f"[预转码] 停止: {reason}")
                break
            job = get_or_start_transcode(v["path"], vid, quality)
            jobs.append((v, job))

        # 轮询等待所有 job 完成
        while jobs:
            if _pretranscode_stop_event.is_set():
                break
            with _batch_lock:
                if _batch_state["running"]:
                    break
            while True:
                if _pretranscode_stop_event.is_set():
                    break
                with _pretranscode_lock:
                    if not _pretranscode_state["paused"]:
                        break
                time.sleep(2)
            if _pretranscode_stop_event.is_set():
                break

            done_now = []
            running_names = []
            for v, job in jobs:
                if job.finished or job.error:
                    done_now.append((v, job))
                    if job.error:
                        print(f"[预转码] 失败: {v['name']}")
                    else:
                        print(f"[预转码] 完成: {v['name']}")
                elif not job.queued:
                    running_names.append(v["name"])

            for item in done_now:
                jobs.remove(item)
                with _pretranscode_lock:
                    _pretranscode_state["done"] += 1

            with _pretranscode_lock:
                _pretranscode_state["current"] = ", ".join(running_names[:3]) if running_names else ""

            time.sleep(2)

        with _pretranscode_lock:
            _pretranscode_state["running"] = False
            _pretranscode_state["current"] = ""
            _pretranscode_state["current_video_id"] = ""

    threading.Thread(target=_run, daemon=True).start()
    return True
