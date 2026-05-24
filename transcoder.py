import subprocess
import threading
import time
from pathlib import Path
import config
from config import HLS_SEGMENT_TIME, QUALITY_PROFILES

MIN_SEGMENTS_BEFORE_SERVE = 3


class TranscodeJob:
    def __init__(self, video_path: str, video_id: str, quality: str):
        self.video_path = video_path
        self.video_id = video_id
        self.quality = quality
        self.process: subprocess.Popen | None = None
        self.output_dir = Path(config.get("cache_dir")) / video_id / quality
        self.playlist = self.output_dir / "playlist.m3u8"
        self._lock = threading.Lock()
        self.started = False
        self.ready = False  # 有足够分片可以播放
        self.finished = False
        self.error = False

    def _segment_count(self) -> int:
        if not self.playlist.exists():
            return 0
        return len(list(self.output_dir.glob("seg_*.ts")))

    def start(self) -> bool:
        with self._lock:
            if self.started:
                return True
            self.started = True

        self.output_dir.mkdir(parents=True, exist_ok=True)

        w, h, v_bitrate, a_bitrate = QUALITY_PROFILES[self.quality]

        cmd = [
            "ffmpeg", "-y",
            "-hwaccel", "videotoolbox",
            "-i", self.video_path,
            "-vf", f"scale={w}:{h}",
            "-c:v", "h264_videotoolbox",
            "-b:v", f"{v_bitrate}k",
            "-maxrate", f"{v_bitrate}k",
            "-bufsize", f"{v_bitrate * 2}k",
            "-c:a", "aac",
            "-b:a", f"{a_bitrate}k",
            "-ac", "2",
            "-f", "hls",
            "-hls_time", str(HLS_SEGMENT_TIME),
            "-hls_list_size", "0",
            "-hls_flags", "delete_segments+omit_endlist",
            "-hls_segment_filename", str(self.output_dir / "seg_%05d.ts"),
            str(self.playlist),
        ]

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # 后台等待分片就绪
            threading.Thread(target=self._wait_ready, daemon=True).start()
            return True
        except Exception:
            self.started = False
            self.error = True
            return False

    def _wait_ready(self):
        """等待至少 MIN_SEGMENTS_BEFORE_SERVE 个分片生成"""
        timeout = 60
        waited = 0
        while waited < timeout:
            if not self.is_alive():
                # 转码已结束（可能是出错或视频很短）
                if self._segment_count() > 0:
                    self.ready = True
                    self.finished = True
                else:
                    self.error = True
                return
            if self._segment_count() >= MIN_SEGMENTS_BEFORE_SERVE:
                self.ready = True
                break
            time.sleep(1)
            waited += 1

        # 继续监控转码进程
        if self.process:
            self.process.wait()
            self.finished = True

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def wait_ready(self, timeout: float = 30.0) -> bool:
        """等待直到有足够分片可播放"""
        waited = 0
        while waited < timeout:
            if self.ready:
                return True
            if self.error:
                return False
            time.sleep(0.5)
            waited += 0.5
        return self.ready


# 全局转码任务管理
_jobs: dict[str, TranscodeJob] = {}
_jobs_lock = threading.Lock()


def get_or_start_transcode(video_path: str, video_id: str, quality: str) -> TranscodeJob:
    key = f"{video_id}:{quality}"
    with _jobs_lock:
        job = _jobs.get(key)
        if job and not job.error:
            return job
        job = TranscodeJob(video_path, video_id, quality)
        _jobs[key] = job
    job.start()
    return job


def get_job(video_id: str, quality: str) -> TranscodeJob | None:
    key = f"{video_id}:{quality}"
    with _jobs_lock:
        return _jobs.get(key)
