import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import config

BASE_DIR = Path(__file__).parent


def get_video_id(filepath: Path) -> str:
    """用文件名（不含扩展名）作为 ID"""
    return filepath.stem


# ---------- 缓存层 ----------

# 内存缓存: {(path, mtime): info_dict}
_probe_cache: dict[tuple[str, float], dict | None] = {}

# 磁盘缓存: {filepath_str: {"mtime": float, "info": dict | null}}
_DISK_CACHE_FILE = BASE_DIR / "video_cache.json"
_disk_cache: dict[str, dict] | None = None
_disk_lock = threading.Lock()


def _load_disk_cache() -> dict[str, dict]:
    global _disk_cache
    if _disk_cache is not None:
        return _disk_cache
    with _disk_lock:
        if _disk_cache is not None:
            return _disk_cache
        if _DISK_CACHE_FILE.exists():
            try:
                _disk_cache = json.loads(_DISK_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                _disk_cache = {}
        else:
            _disk_cache = {}
        return _disk_cache


def _save_disk_cache():
    if _disk_cache is None:
        return
    with _disk_lock:
        _DISK_CACHE_FILE.write_text(
            json.dumps(_disk_cache, ensure_ascii=False), encoding="utf-8"
        )


# ---------- ffprobe ----------

def probe_video(filepath: Path) -> dict | None:
    try:
        stat = filepath.stat()
        cache_key = (str(filepath), stat.st_mtime)

        # 1. 内存缓存
        if cache_key in _probe_cache:
            return _probe_cache[cache_key]

        # 2. 磁盘缓存
        dc = _load_disk_cache()
        path_str = str(filepath)
        cached = dc.get(path_str)
        if cached and cached.get("mtime") == stat.st_mtime:
            info = cached.get("info")
            _probe_cache[cache_key] = info
            return info

        # 3. ffprobe
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


# ---------- 扫描 ----------

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

    # 筛选需要 ffprobe 的文件（磁盘缓存未命中）
    dc = _load_disk_cache()
    need_probe: list[Path] = []
    for filepath in video_files:
        path_str = str(filepath)
        cached = dc.get(path_str)
        if not cached:
            need_probe.append(filepath)
            continue
        try:
            mtime = filepath.stat().st_mtime
            if cached.get("mtime") != mtime:
                need_probe.append(filepath)
        except OSError:
            need_probe.append(filepath)

    # 并行 ffprobe（仅对需要的文件）
    if need_probe:
        max_workers = min(8, len(need_probe))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_probe_worker, f): f for f in need_probe}
            for future in as_completed(futures):
                path_str, info = future.result()
                # 写入磁盘缓存
                try:
                    mtime = Path(path_str).stat().st_mtime
                    dc[path_str] = {"mtime": mtime, "info": info}
                except OSError:
                    pass
        _save_disk_cache()

    # 按原始顺序组装结果
    videos = []
    for filepath in video_files:
        path_str = str(filepath)
        # 从内存缓存或磁盘缓存获取结果
        info = None
        try:
            cache_key = (path_str, filepath.stat().st_mtime)
            info = _probe_cache.get(cache_key)
        except OSError:
            pass
        if info is None:
            cached = dc.get(path_str)
            info = cached.get("info") if cached else None
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
