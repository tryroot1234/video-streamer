import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
import config
from config import HLS_SEGMENT_TIME, QUALITY_PROFILES

MIN_SEGMENTS_BEFORE_SERVE = 1
MAX_CONCURRENT_JOBS = 3


def _get_encode_bitrate(source_kbps: int, target_kbps: int) -> int:
    """自适应码率：源码率低于目标时不放大"""
    if source_kbps <= 0:
        return target_kbps
    return min(target_kbps, max(int(source_kbps * 1.2), 500))


class TranscodeJob:
    def __init__(self, video_path: str, video_id: str, quality: str, source_bitrate_kbps: int = 0):
        self.video_path = video_path
        self.video_id = video_id
        self.quality = quality
        self.source_bitrate_kbps = source_bitrate_kbps
        self.process: subprocess.Popen | None = None
        self.output_dir = Path(config.get("cache_dir")) / video_id / quality
        self.tmp_dir = self.output_dir / "_tmp"
        self._lock = threading.Lock()
        self.started = False
        self.ready = False
        self.finished = False
        self.error = False
        self.paused = False
        self.queued = False
        self._initial_process: subprocess.Popen | None = None

    def _total_segments(self) -> int:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", self.video_path],
                capture_output=True, text=True, timeout=10,
            )
            info = __import__("json").loads(result.stdout)
            duration = float(info["format"]["duration"])
            return max(1, math.ceil(duration / HLS_SEGMENT_TIME))
        except Exception:
            return 0

    def _cached_indices(self) -> set[int]:
        indices = set()
        if self.output_dir.exists():
            for f in self.output_dir.glob("seg_*.ts"):
                m = re.match(r"seg_(\d+)\.ts", f.name)
                if m:
                    indices.add(int(m.group(1)))
        return indices

    def _segment_count(self) -> int:
        return len(self._cached_indices())

    def _run_ffmpeg(self, seek_position: float = 0) -> subprocess.Popen:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        w, h, v_bitrate, a_bitrate = QUALITY_PROFILES[self.quality]
        v_bitrate = _get_encode_bitrate(self.source_bitrate_kbps, v_bitrate)

        cmd = ["ffmpeg", "-y", "-hwaccel", "videotoolbox"]
        if seek_position > 0:
            cmd += ["-ss", str(int(seek_position)), "-noaccurate_seek"]
        cmd += [
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
            "-hls_segment_filename", str(self.tmp_dir / "seg_%05d.ts"),
            str(self.tmp_dir / "playlist.m3u8"),
        ]
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _move_segments(self, start_seg: int):
        next_idx = 0
        while True:
            tmp_file = self.tmp_dir / f"seg_{next_idx:05d}.ts"
            next_tmp = self.tmp_dir / f"seg_{next_idx + 1:05d}.ts"

            if tmp_file.exists() and (next_tmp.exists() or not self.is_alive()):
                abs_idx = start_seg + next_idx
                dest = self.output_dir / f"seg_{abs_idx:05d}.ts"
                if not dest.exists():
                    try:
                        tmp_file.rename(dest)
                    except OSError:
                        pass
                else:
                    try:
                        tmp_file.unlink()
                    except OSError:
                        pass
                next_idx += 1
                continue

            if not self.is_alive() and not tmp_file.exists():
                break

            time.sleep(0.5)

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def start(self) -> bool:
        with self._lock:
            if self.started:
                return True
            self.started = True

        self.output_dir.mkdir(parents=True, exist_ok=True)

        cached = self._cached_indices()
        total = self._total_segments()
        if cached and total > 0 and max(cached) >= total - 1:
            self.ready = True
            self.finished = True
            _pool.on_job_finished(self)
            return True

        try:
            start_seg = (max(cached) + 1) if cached else 0
            seek_pos = start_seg * HLS_SEGMENT_TIME
            self.process = self._run_ffmpeg(seek_pos)
            self._initial_process = self.process
            threading.Thread(target=self._wait_ready, args=(start_seg,), daemon=True).start()
            return True
        except Exception:
            self.started = False
            self.error = True
            _pool.on_job_finished(self)
            return False

    def _wait_ready(self, start_seg: int):
        threading.Thread(target=self._move_segments, args=(start_seg,), daemon=True).start()

        timeout = 60
        waited = 0
        while waited < timeout:
            if not self.is_alive():
                if self._segment_count() > 0:
                    self.ready = True
                    self.finished = True
                else:
                    self.error = True
                _pool.on_job_finished(self)
                return
            first_seg = self.output_dir / f"seg_{start_seg:05d}.ts"
            if first_seg.exists():
                self.ready = True
                break
            time.sleep(1)
            waited += 1

        if self.process:
            self.process.wait()
        self.finished = True
        _pool.on_job_finished(self)

    def start_seek(self, position: float) -> bool:
        start_seg = int(position) // HLS_SEGMENT_TIME
        cached = self._cached_indices()
        if start_seg in cached:
            return True

        if self.process and self.process != self._initial_process and self.process.poll() is None:
            try:
                self.process.terminate()
            except (ProcessLookupError, OSError):
                pass

        try:
            self.process = self._run_ffmpeg(position)
        except Exception:
            return False

        if self._initial_process and self._initial_process.poll() is None:
            try:
                self._initial_process.terminate()
            except (ProcessLookupError, OSError):
                pass
            self._initial_process = None

        threading.Thread(target=self._wait_ready, args=(start_seg,), daemon=True).start()
        return True

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
        waited = 0
        while waited < timeout:
            if self.ready:
                return True
            if self.error:
                return False
            time.sleep(0.5)
            waited += 0.5
        return self.ready


# ---------- Worker 池：最多 MAX_CONCURRENT_JOBS 个视频同时转码 ----------

class _WorkerPool:
    def __init__(self):
        self._lock = threading.Lock()
        self._running: set[str] = set()  # job keys currently running
        self._queue: list[str] = []      # queued job keys
        self._jobs: dict[str, TranscodeJob] = {}

    def submit(self, job: TranscodeJob) -> TranscodeJob:
        key = f"{job.video_id}:{job.quality}"
        with self._lock:
            existing = self._jobs.get(key)
            if existing and not existing.error:
                if existing.is_alive() or existing.finished:
                    return existing
                del self._jobs[key]

            self._jobs[key] = job

            if len(self._running) < MAX_CONCURRENT_JOBS:
                self._running.add(key)
                threading.Thread(target=self._start_job, args=(job,), daemon=True).start()
            else:
                job.queued = True
                self._queue.append(key)

        return job

    def _start_job(self, job: TranscodeJob):
        job.start()

    def on_job_finished(self, job: TranscodeJob):
        key = f"{job.video_id}:{job.quality}"
        with self._lock:
            self._running.discard(key)
            # 从队列取下一个
            while self._queue:
                next_key = self._queue.pop(0)
                next_job = self._jobs.get(next_key)
                if next_job and not next_job.started:
                    next_job.queued = False
                    self._running.add(next_key)
                    threading.Thread(target=self._start_job, args=(next_job,), daemon=True).start()
                    break

    def get_job(self, video_id: str, quality: str) -> TranscodeJob | None:
        key = f"{video_id}:{quality}"
        with self._lock:
            return self._jobs.get(key)

    def get_all_active_jobs(self) -> dict[str, dict]:
        result = {}
        with self._lock:
            for key, job in self._jobs.items():
                if job.finished and not job.paused:
                    continue
                vid = job.video_id
                seg_count = job._segment_count()
                if job.queued:
                    status = "queued"
                    pct = 0
                elif job.finished:
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

    def invalidate_video(self, video_id: str):
        with self._lock:
            keys_to_remove = [k for k in self._jobs if k.startswith(f"{video_id}:")]
            for k in keys_to_remove:
                job = self._jobs[k]
                if job.is_alive():
                    job.process.terminate()
                self._running.discard(k)
                if k in self._queue:
                    self._queue.remove(k)
                del self._jobs[k]

    def invalidate_all(self):
        with self._lock:
            for job in self._jobs.values():
                if job.is_alive():
                    try:
                        job.process.terminate()
                    except (ProcessLookupError, OSError):
                        pass
            self._jobs.clear()
            self._running.clear()
            self._queue.clear()


_pool = _WorkerPool()


def get_or_start_transcode(video_path: str, video_id: str, quality: str,
                           source_bitrate_kbps: int = 0) -> TranscodeJob:
    job = TranscodeJob(video_path, video_id, quality, source_bitrate_kbps)
    return _pool.submit(job)


def get_job(video_id: str, quality: str) -> TranscodeJob | None:
    return _pool.get_job(video_id, quality)


def get_all_active_jobs() -> dict[str, dict]:
    return _pool.get_all_active_jobs()


def generate_full_m3u8(video_id: str, quality: str, duration: float, start: float = 0, is_live: bool = False) -> str:
    cache_dir = Path(config.get("cache_dir")) / video_id / quality
    prefix = f"/api/video/{video_id}/stream/{quality}/"
    total_segments = max(1, math.ceil(duration / HLS_SEGMENT_TIME))

    start_seg = max(0, int(start) // HLS_SEGMENT_TIME) if start > 0 else 0
    start_seg = min(start_seg, total_segments - 1)

    available = set()
    if cache_dir.exists():
        for f in cache_dir.glob("seg_*.ts"):
            m = re.match(r"seg_(\d+)\.ts", f.name)
            if m:
                available.add(int(m.group(1)))

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
    ]
    if not is_live:
        lines.append("#EXT-X-PLAYLIST-TYPE:VOD")
    lines.append(f"#EXT-X-TARGETDURATION:{HLS_SEGMENT_TIME}")
    lines.append(f"#EXT-X-MEDIA-SEQUENCE:{start_seg}")

    for i in range(start_seg, total_segments):
        if is_live and i not in available:
            continue
        if i < total_segments - 1:
            seg_duration = HLS_SEGMENT_TIME
        else:
            seg_duration = duration - (total_segments - 1) * HLS_SEGMENT_TIME
        lines.append(f"#EXTINF:{seg_duration:.1f},")
        lines.append(f"{prefix}seg_{i:05d}.ts")

    if not is_live:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def invalidate_jobs(video_id: str):
    _pool.invalidate_video(video_id)


def invalidate_all_jobs():
    _pool.invalidate_all()
