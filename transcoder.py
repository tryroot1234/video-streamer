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
        self._initial_process: subprocess.Popen | None = None

    def _total_segments(self) -> int:
        """从视频文件获取总时长并计算总 segment 数"""
        try:
            import subprocess as sp
            result = sp.run(
                ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", self.video_path],
                capture_output=True, text=True, timeout=10,
            )
            import json
            info = json.loads(result.stdout)
            duration = float(info["format"]["duration"])
            return max(1, math.ceil(duration / HLS_SEGMENT_TIME))
        except Exception:
            return 0

    def _cached_indices(self) -> set[int]:
        """扫描缓存中已有的 segment 绝对编号"""
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
        """统一的 ffmpeg 启动（从头或从指定位置）"""
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

        return subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _move_segments(self, start_seg: int):
        """后台线程：持续将完成的 segment 从临时目录移入缓存目录"""
        next_idx = 0
        while True:
            tmp_file = self.tmp_dir / f"seg_{next_idx:05d}.ts"
            next_tmp = self.tmp_dir / f"seg_{next_idx + 1:05d}.ts"

            # 当前 segment 存在，且下一个也存在（说明当前已写完）或进程已结束
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

            # 进程已结束且没有更多文件
            if not self.is_alive() and not tmp_file.exists():
                break

            time.sleep(0.5)

        # 清理临时目录
        try:
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass

    def start(self) -> bool:
        with self._lock:
            if self.started:
                return True
            self.started = True

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 检查是否已完全缓存
        cached = self._cached_indices()
        total = self._total_segments()
        if cached and total > 0 and max(cached) >= total - 1:
            self.ready = True
            self.finished = True
            return True

        try:
            self.process = self._run_ffmpeg(0)
            self._initial_process = self.process
            threading.Thread(target=self._wait_ready, args=(0,), daemon=True).start()
            return True
        except Exception:
            self.started = False
            self.error = True
            return False

    def _wait_ready(self, start_seg: int):
        """等待至少 MIN_SEGMENTS_BEFORE_SERVE 个分片生成，同时持续移动 segment"""
        # 启动 segment 移动线程
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
                return
            first_seg = self.output_dir / f"seg_{start_seg:05d}.ts"
            if first_seg.exists():
                self.ready = True
                break
            time.sleep(1)
            waited += 1

        # 继续监控转码进程
        if self.process:
            self.process.wait()
            self.finished = True

    def start_seek(self, position: float) -> bool:
        """从指定位置开始转码（跳转）"""
        start_seg = int(position) // HLS_SEGMENT_TIME
        cached = self._cached_indices()

        # 目标 segment 已存在 → 无需转码
        if start_seg in cached:
            return True

        # 停止旧的 seek 进程（非初始进程）
        if self.process and self.process != self._initial_process and self.process.poll() is None:
            try:
                self.process.terminate()
            except (ProcessLookupError, OSError):
                pass

        try:
            self.process = self._run_ffmpeg(position)
        except Exception:
            return False

        # 终止初始转码（seek 位置已超过初始进度）
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


def get_or_start_transcode(video_path: str, video_id: str, quality: str,
                           source_bitrate_kbps: int = 0) -> TranscodeJob:
    key = f"{video_id}:{quality}"
    with _jobs_lock:
        job = _jobs.get(key)
        if job and not job.error:
            if job.is_alive() or job.finished:
                return job
            # 文件已删除，清除旧 job
            del _jobs[key]
        job = TranscodeJob(video_path, video_id, quality, source_bitrate_kbps)
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


def generate_full_m3u8(video_id: str, quality: str, duration: float, start: float = 0, is_live: bool = False) -> str:
    """动态生成覆盖完整视频时间线的 m3u8 播放列表。

    扫描缓存目录中已有的 seg_NNNNN.ts 文件。
    hls.js 请求不存在的 segment 时会收到 200 空响应（视为 gap）。

    start > 0 时，生成从指定秒数开始的截断 m3u8。
    is_live = True 时，不写 #EXT-X-ENDLIST，hls.js 会定期轮询。
    """
    cache_dir = Path(config.get("cache_dir")) / video_id / quality
    prefix = f"/api/video/{video_id}/stream/{quality}/"
    total_segments = max(1, math.ceil(duration / HLS_SEGMENT_TIME))

    start_seg = max(0, int(start) // HLS_SEGMENT_TIME) if start > 0 else 0
    start_seg = min(start_seg, total_segments - 1)

    # 只扫描 seg_NNNNN.ts
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
    """清除指定视频的所有转码任务引用（缓存被清除时调用）"""
    with _jobs_lock:
        keys_to_remove = [k for k in _jobs if k.startswith(f"{video_id}:")]
        for k in keys_to_remove:
            job = _jobs[k]
            if job.is_alive():
                job.process.terminate()
            del _jobs[k]


def invalidate_all_jobs():
    """终止并清除所有转码任务"""
    with _jobs_lock:
        for job in _jobs.values():
            if job.is_alive():
                try:
                    job.process.terminate()
                except (ProcessLookupError, OSError):
                    pass
        _jobs.clear()
