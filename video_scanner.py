import json
import subprocess
from pathlib import Path
import config


def get_video_id(filepath: Path) -> str:
    """用文件名（不含扩展名）作为 ID"""
    return filepath.stem


def probe_video(filepath: Path) -> dict | None:
    try:
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
            return None

        fmt = data.get("format", {})
        return {
            "width": int(video_stream.get("width", 0)),
            "height": int(video_stream.get("height", 0)),
            "duration": float(fmt.get("duration", 0)),
            "size": int(fmt.get("size", 0)),
            "codec": video_stream.get("codec_name", "unknown"),
            "bitrate": int(fmt.get("bit_rate", 0)),
        }
    except Exception:
        return None


def scan_videos() -> list[dict]:
    video_dir = Path(config.get("video_dir"))
    if not video_dir.exists():
        return []

    videos = []
    for filepath in sorted(video_dir.rglob("*")):
        if filepath.suffix.lower() not in config.VIDEO_EXTENSIONS:
            continue
        if filepath.name.startswith("."):
            continue

        info = probe_video(filepath)
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
