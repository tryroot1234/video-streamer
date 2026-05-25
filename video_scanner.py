import json
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import config


def get_video_id(filepath: Path) -> str:
    """用文件名（不含扩展名）作为 ID"""
    return filepath.stem


# ffprobe 结果缓存: {(path, mtime): info_dict}
_probe_cache: dict[tuple[str, float], dict | None] = {}


def probe_video(filepath: Path) -> dict | None:
    try:
        stat = filepath.stat()
        cache_key = (str(filepath), stat.st_mtime)
        if cache_key in _probe_cache:
            return _probe_cache[cache_key]

        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format", "-show_streams",
                str(filepath)
            ],
            capture_output=True, text=True, timeout=30
        )
        data = json.loads(result.stdout)

        video_stream = next(
            (s for s in data.get("streams", []) if s.get("codec_type") == "video"),
            None
        )
        if not video_stream:
            _probe_cache[cache_key] = None
            return None

        fmt = data.get("format", {})
        info = {
            "width": int(video_stream.get("width", 0)),
            "height": int(video_stream.get("height", 0)),
            "duration": float(fmt.get("duration", 0)),
            "size": int(fmt.get("size", stat.st_size)),
            "codec": video_stream.get("codec_name", "unknown"),
            "bitrate": int(fmt.get("bit_rate", 0)),
        }
        _probe_cache[cache_key] = info
        return info
    except Exception:
        return None


def _probe_worker(filepath: Path) -> tuple[str, dict | None]:
    """线程池 worker，返回 (路径, 结果)"""
    return (str(filepath), probe_video(filepath))


def scan_videos() -> list[dict]:
    dirs = config.get("video_dirs") or []
    if not dirs:
        return []

    # 收集所有视频文件
    video_files = []
    for d in dirs:
        video_dir = Path(d)
        if not video_dir.exists():
            continue
        for filepath in sorted(video_dir.rglob("*")):
            if filepath.suffix.lower() not in config.VIDEO_EXTENSIONS:
                continue
            if filepath.name.startswith("."):
                continue
            video_files.append(filepath)

    if not video_files:
        return []

    # 并行 ffprobe（最多 8 线程）
    results: dict[str, dict | None] = {}
    max_workers = min(8, len(video_files))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_probe_worker, f): f for f in video_files}
        for future in as_completed(futures):
            path_str, info = future.result()
            results[path_str] = info

    # 按原始顺序组装结果
    videos = []
    for filepath in video_files:
        info = results.get(str(filepath))
        if not info:
            continue

        vid = get_video_id(filepath)
        if info["height"] >= 2160:
            recommended = "1080p"
        elif info["height"] >= 1080:
            recommended = "720p"
        else:
            recommended = "480p"

        videos.append({
            "id": vid,
            "name": filepath.stem,
            "filename": filepath.name,
            "path": str(filepath),
            **info,
            "recommended_quality": recommended,
        })

    return videos
