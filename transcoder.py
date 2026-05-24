import math
import os
import re
import signal
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
        self.paused = False

    def _segment_count(self) -> int:
        if not self.playlist.exists():
            return 0
        return len(list(self.output_dir.glob("seg_*.ts")))

    def _seek_segment_count(self, seek_prefix: str) -> int:
        return len(list(self.output_dir.glob(f"{seek_prefix}_*.ts")))

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
            "-hls_flags", "omit_endlist",
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

    def pause(self) -> bool:
        if self.process and self.is_alive() and not self.paused and not self.finished:
            try:
                os.kill(self.process.pid, signal.SIGSTOP)
                self.paused = True
                return True
            except (ProcessLookupError, OSError):
                return False
        return False

    def resume(self) -> bool:
        if self.process and self.is_alive() and self.paused:
            try:
                os.kill(self.process.pid, signal.SIGCONT)
                self.paused = False
                return True
            except (ProcessLookupError, OSError):
                return False
        return False

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
            # 检查文件是否实际存在（缓存可能已被清除）
            if job.playlist.exists() or job.is_alive():
                return job
            # 文件已删除，清除旧 job
            del _jobs[key]
        job = TranscodeJob(video_path, video_id, quality)
        _jobs[key] = job
    job.start()
    return job


def get_job(video_id: str, quality: str) -> TranscodeJob | None:
    key = f"{video_id}:{quality}"
    with _jobs_lock:
        return _jobs.get(key)


def get_all_active_jobs() -> dict[str, dict]:
    """返回所有活跃转码任务的进度信息"""
    result = {}
    with _jobs_lock:
        for key, job in _jobs.items():
            if job.finished and not job.paused:
                continue
            vid = job.video_id
            seg_count = job._segment_count()
            if job.finished:
                status = "done"
                pct = 100
            elif job.paused:
                status = "paused"
                pct = min(99, seg_count * 5)
            elif job.error:
                status = "error"
                pct = 0
            else:
                status = "caching"
                pct = min(99, seg_count * 5)
            result[vid] = {"percent": pct, "status": status, "quality": job.quality}
    return result


# ---------- 可跳转转码 ----------

_seek_jobs: dict[str, "SeekableTranscodeJob"] = {}
_seek_jobs_lock = threading.Lock()


class SeekableTranscodeJob:
    """支持从任意位置开始新转码的转码任务"""

    def __init__(self, video_path: str, video_id: str, quality: str):
        self.video_path = video_path
        self.video_id = video_id
        self.quality = quality
        self.output_dir = Path(config.get("cache_dir")) / video_id / quality
        self.initial_job: TranscodeJob | None = None
        self.seek_job: TranscodeJob | None = None
        self.seek_position: float = 0
        self.seek_ready = False

    def start_initial(self) -> TranscodeJob:
        self.initial_job = TranscodeJob(self.video_path, self.video_id, self.quality)
        self.initial_job.start()
        return self.initial_job

    def start_seek(self, position: float) -> bool:
        if self.seek_job and self.seek_job.is_alive():
            if abs(self.seek_position - position) < HLS_SEGMENT_TIME * 2:
                return True
            self._stop_seek()

        self.seek_position = position
        self.seek_ready = False
        seek_prefix = f"seek_{int(position)}"

        self.output_dir.mkdir(parents=True, exist_ok=True)

        w, h, v_bitrate, a_bitrate = QUALITY_PROFILES[self.quality]
        cmd = [
            "ffmpeg", "-y",
            "-hwaccel", "videotoolbox",
            "-ss", str(int(position)),
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
            "-hls_flags", "omit_endlist",
            "-hls_segment_filename", str(self.output_dir / f"{seek_prefix}_%05d.ts"),
            str(self.output_dir / f"{seek_prefix}.m3u8"),
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            return False

        # Build a lightweight wrapper
        job = TranscodeJob.__new__(TranscodeJob)
        job.video_path = self.video_path
        job.video_id = self.video_id
        job.quality = self.quality
        job.process = proc
        job.output_dir = self.output_dir
        job.playlist = self.output_dir / f"{seek_prefix}.m3u8"
        job._lock = threading.Lock()
        job.started = True
        job.ready = False
        job.finished = False
        job.error = False
        job.paused = False
        self.seek_job = job

        threading.Thread(target=self._wait_seek_ready, daemon=True).start()
        return True

    def _wait_seek_ready(self):
        job = self.seek_job
        if not job:
            return
        seek_prefix = f"seek_{int(self.seek_position)}"
        timeout = 60
        waited = 0
        while waited < timeout:
            if not job.is_alive():
                if job._seek_segment_count(seek_prefix) > 0:
                    self.seek_ready = True
                    job.finished = True
                else:
                    job.error = True
                return
            if job._seek_segment_count(seek_prefix) >= MIN_SEGMENTS_BEFORE_SERVE:
                self.seek_ready = True
                break
            time.sleep(1)
            waited += 1
        if job.process:
            job.process.wait()
            job.finished = True

    def _stop_seek(self):
        if self.seek_job and self.seek_job.is_alive():
            try:
                self.seek_job.process.terminate()
            except (ProcessLookupError, OSError):
                pass
        self.seek_job = None
        self.seek_ready = False

    def stop_seek(self):
        self._stop_seek()

    def is_seek_alive(self) -> bool:
        return self.seek_job is not None and self.seek_job.is_alive()

    def get_seek_segment_count(self) -> int:
        if not self.seek_job:
            return 0
        seek_prefix = f"seek_{int(self.seek_position)}"
        return self.seek_job._seek_segment_count(seek_prefix)

    def get_seek_playlist_content(self, video_id: str, quality: str) -> str | None:
        if not self.seek_job or not self.seek_job.playlist.exists():
            return None
        try:
            content = self.seek_job.playlist.read_text(encoding="utf-8")
            return _rewrite_seek_m3u8(content, video_id, quality, self.seek_position)
        except Exception:
            return None


def _rewrite_seek_m3u8(content: str, video_id: str, quality: str, seek_position: float = 0) -> str:
    """Rewrite seek m3u8 to use absolute segment paths and sequence numbers"""
    prefix = f"/api/video/{video_id}/stream/{quality}/"
    if seek_position > 0:
        start_seg = int(seek_position) // HLS_SEGMENT_TIME
        # 将 seek_N_XXXXX.ts 重命名为绝对编号的 seg_XXXXX.ts
        def _replace_seg(m):
            idx = int(m.group(1))
            return f"seg_{start_seg + idx:05d}.ts"
        content = re.sub(r"seek_\d+_(\d+)\.ts", _replace_seg, content)
        content = re.sub(r"#EXT-X-MEDIA-SEQUENCE:\d+", f"#EXT-X-MEDIA-SEQUENCE:{start_seg}", content)
    else:
        content = re.sub(r"(seek_\d+_\d+\.ts)", prefix + r"\1", content)
    return content


def generate_full_m3u8(video_id: str, quality: str, duration: float) -> str:
    """动态生成覆盖完整视频时间线的 m3u8 播放列表。

    扫描缓存目录中已有的 segment 文件，为未转码的位置生成占位条目。
    hls.js 请求不存在的 segment 时会收到 404 并自动重试。
    """
    cache_dir = Path(config.get("cache_dir")) / video_id / quality
    prefix = f"/api/video/{video_id}/stream/{quality}/"
    total_segments = max(1, math.ceil(duration / HLS_SEGMENT_TIME))

    # 收集已存在的 segment 绝对编号
    available = set()

    # 1. 扫描 seg_NNNNN.ts（初始转码产出）
    if cache_dir.exists():
        for f in cache_dir.glob("seg_*.ts"):
            m = re.match(r"seg_(\d+)\.ts", f.name)
            if m:
                available.add(int(m.group(1)))

    # 2. 扫描 seek_*_*.ts，映射为绝对编号（仅补充不存在的）
    if cache_dir.exists():
        for f in cache_dir.glob("seek_*_*.ts"):
            m = re.match(r"seek_(\d+)_(\d+)\.ts", f.name)
            if m:
                seek_pos = int(m.group(1))
                seek_idx = int(m.group(2))
                abs_idx = seek_pos // HLS_SEGMENT_TIME + seek_idx
                if abs_idx not in available:
                    available.add(abs_idx)

    # 生成 m3u8
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-TARGETDURATION:{HLS_SEGMENT_TIME}",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]

    for i in range(total_segments):
        if i < total_segments - 1:
            seg_duration = HLS_SEGMENT_TIME
        else:
            seg_duration = duration - (total_segments - 1) * HLS_SEGMENT_TIME
        lines.append(f"#EXTINF:{seg_duration:.1f},")
        lines.append(f"{prefix}seg_{i:05d}.ts")

    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def get_or_create_seekable(video_path: str, video_id: str, quality: str) -> SeekableTranscodeJob:
    key = f"{video_id}:{quality}"
    with _seek_jobs_lock:
        job = _seek_jobs.get(key)
        if job:
            return job
        job = SeekableTranscodeJob(video_path, video_id, quality)
        _seek_jobs[key] = job
    job.start_initial()
    return job


def get_seekable_job(video_id: str, quality: str) -> SeekableTranscodeJob | None:
    key = f"{video_id}:{quality}"
    with _seek_jobs_lock:
        return _seek_jobs.get(key)


def invalidate_jobs(video_id: str):
    """清除指定视频的所有转码任务引用（缓存被清除时调用）"""
    with _jobs_lock:
        keys_to_remove = [k for k in _jobs if k.startswith(f"{video_id}:")]
        for k in keys_to_remove:
            job = _jobs[k]
            if job.is_alive():
                job.process.terminate()
            del _jobs[k]
    with _seek_jobs_lock:
        keys_to_remove = [k for k in _seek_jobs if k.startswith(f"{video_id}:")]
        for k in keys_to_remove:
            job = _seek_jobs[k]
            if job.is_seek_alive():
                job.stop_seek()
            del _seek_jobs[k]
