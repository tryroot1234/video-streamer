import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
SETTINGS_FILE = BASE_DIR / "settings.json"

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".rmvb"}
HLS_SEGMENT_TIME = 6
HLS_LIST_SIZE = 0

QUALITY_PROFILES = {
    "1080p": (1920, 1080, 5000, 192),
    "720p":  (1280, 720,  2500, 128),
    "480p":  (854,  480,  1000, 128),
}

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))


# 可动态修改的设置
_settings: dict = {
    "video_dirs": [],
    "cache_dir": str(BASE_DIR / "cache"),
    "max_cache_size_gb": 50,
}


def load_settings():
    global _settings
    if SETTINGS_FILE.exists():
        try:
            saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            # 向后兼容：旧的 video_dir 字符串 → video_dirs 列表
            if "video_dir" in saved and "video_dirs" not in saved:
                saved["video_dirs"] = [saved.pop("video_dir")]
            _settings.update(saved)
        except Exception:
            pass
    # 确保目录存在
    Path(_settings["cache_dir"]).mkdir(parents=True, exist_ok=True)


def save_settings():
    SETTINGS_FILE.write_text(json.dumps(_settings, ensure_ascii=False, indent=2), encoding="utf-8")


def get(key: str):
    return _settings.get(key)


def get_all() -> dict:
    return dict(_settings)


def update(data: dict) -> dict:
    global _settings
    allowed = {"video_dirs", "cache_dir", "max_cache_size_gb"}
    for k, v in data.items():
        if k in allowed and v is not None:
            _settings[k] = v
    Path(_settings["cache_dir"]).mkdir(parents=True, exist_ok=True)
    save_settings()
    return dict(_settings)
