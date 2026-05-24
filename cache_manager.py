import shutil
import threading
import time
from pathlib import Path
import config


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


def is_cached(video_id: str, quality: str) -> bool:
    playlist = get_video_cache_dir(video_id, quality) / "playlist.m3u8"
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
            if is_cached(v["id"], v.get("recommended_quality", "720p")):
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

    def _run():
        from transcoder import get_or_start_transcode
        for v in videos:
            with _batch_lock:
                if not _batch_state["running"]:
                    _batch_state["stopped_reason"] = "用户手动停止"
                    break

            can, reason = can_cache_more()
            if not can:
                with _batch_lock:
                    _batch_state["running"] = False
                    _batch_state["stopped_reason"] = reason
                break

            vid = v["id"]
            quality = v.get("recommended_quality", "720p")

            if is_cached(vid, quality):
                with _batch_lock:
                    _batch_state["video_progress"][vid] = {"percent": 100, "status": "done"}
                    _batch_state["done"] += 1
                continue

            with _batch_lock:
                _batch_state["current"] = v["name"]
                _batch_state["current_video_id"] = vid
                _batch_state["video_progress"][vid] = {"percent": 0, "status": "caching"}

            try:
                job = get_or_start_transcode(v["path"], vid, quality)

                # 等待转码完成，同时更新进度
                estimated_total = max(1, int(v.get("duration", 0) / config.HLS_SEGMENT_TIME))
                while job.is_alive():
                    with _batch_lock:
                        if not _batch_state["running"]:
                            break
                    seg_count = job._segment_count()
                    pct = min(99, int(seg_count / estimated_total * 100))
                    with _batch_lock:
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


def _fmt(b: int) -> str:
    if b >= 1073741824:
        return f"{b / 1073741824:.1f} GB"
    return f"{b / 1048576:.0f} MB"
