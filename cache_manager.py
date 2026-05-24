import shutil
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


def get_video_cache_dir(video_id: str, quality: str) -> Path:
    return Path(config.get("cache_dir")) / video_id / quality


def is_cached(video_id: str, quality: str) -> bool:
    playlist = get_video_cache_dir(video_id, quality) / "playlist.m3u8"
    return playlist.exists() and playlist.stat().st_size > 0
