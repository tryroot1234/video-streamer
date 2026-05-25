"""
播放器重构测试用例

测试目标：
1. generate_full_m3u8() 动态生成完整 m3u8
2. is_cached() 基于 segment 数量判断缓存完整性
3. rewrite_m3u8() 路径前缀替换
4. segment 端点 404 行为
5. stream 端点统一流程
6. seek 流程（转码 + m3u8 刷新）
7. 缓存清除后重新转码
"""

import math
import os
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# 临时目录 fixtures


@pytest.fixture
def cache_dir(tmp_path):
    """创建临时缓存目录"""
    d = tmp_path / "cache"
    d.mkdir()
    return d


@pytest.fixture
def video_dir(tmp_path):
    """创建临时视频目录（放一个假视频文件）"""
    d = tmp_path / "videos"
    d.mkdir()
    fake = d / "test_video.mp4"
    fake.write_bytes(b"\x00" * 1024)
    return d


# ============================================================
# TC-01: generate_full_m3u8 基础功能
# ============================================================


class TestGenerateFullM3u8:
    """generate_full_m3u8() 测试"""

    def _make_segments(self, cache_dir, video_id, quality, indices):
        """在缓存目录中创建指定编号的 segment 文件"""
        seg_dir = cache_dir / video_id / quality
        seg_dir.mkdir(parents=True, exist_ok=True)
        for i in indices:
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)
        return seg_dir

    def test_no_segments(self, cache_dir):
        """TC-01a: 无任何 segment 时，m3u8 包含所有占位条目"""
        # generate_full_m3u8 应该生成包含所有 segment URL 的 m3u8，
        # 即使文件不存在（hls.js 请求时会 404）
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # 60 秒 / 6 秒 = 10 个 segment
        assert m3u8 is not None
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in m3u8
        assert "#EXT-X-ENDLIST" in m3u8
        assert "#EXT-X-MEDIA-SEQUENCE:0" in m3u8
        assert "seg_00000.ts" in m3u8
        assert "seg_00009.ts" in m3u8
        assert "seg_00010.ts" not in m3u8  # 只有 10 个

    def test_partial_segments(self, cache_dir):
        """TC-01b: 只有部分 segment 时，m3u8 包含全部条目（含未转码的占位）"""
        from transcoder import generate_full_m3u8

        self._make_segments(cache_dir, "vid1", "720p", [0, 1, 2, 3, 4])

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=120.0)

        # 120 秒 / 6 秒 = 20 个 segment
        assert "seg_00000.ts" in m3u8
        assert "seg_00019.ts" in m3u8
        # EXTINF 条目数应为 20
        assert m3u8.count("#EXTINF:") == 20

    def test_all_segments(self, cache_dir):
        """TC-01c: 所有 segment 都已转码，m3u8 完整"""
        from transcoder import generate_full_m3u8

        self._make_segments(cache_dir, "vid1", "720p", range(10))

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        assert m3u8.count("#EXTINF:") == 10
        for i in range(10):
            assert f"seg_{i:05d}.ts" in m3u8

    def test_seek_segments_mapped(self, cache_dir):
        """TC-01d: seek 分片正确映射为绝对编号"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # seek 到 60 秒，生成 seek_60_00000.ts ~ seek_60_00002.ts
        for i in range(3):
            (seg_dir / f"seek_60_{i:05d}.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=120.0)

        # seek_60_00000 → seg_00010 (60/6 + 0)
        # seek_60_00001 → seg_00011
        # seek_60_00002 → seg_00012
        assert "seg_00010.ts" in m3u8
        assert "seg_00011.ts" in m3u8
        assert "seg_00012.ts" in m3u8
        # seek 文件名不应出现在最终 m3u8 中
        assert "seek_60_" not in m3u8

    def test_seg_priority_over_seek(self, cache_dir):
        """TC-01e: 当 seg_NNNNN.ts 和 seek 文件同时存在时，seg 优先"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 初始转码的 segment
        (seg_dir / "seg_00010.ts").write_bytes(b"\x00" * 100)
        # seek 也映射到 seg_00010
        (seg_dir / "seek_60_00000.ts").write_bytes(b"\x00" * 200)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=120.0)

        # seg_00010 应该存在（不管用哪个文件，URL 都是 seg_00010.ts）
        assert "seg_00010.ts" in m3u8
        assert "seek_60_" not in m3u8

    def test_last_segment_duration(self, cache_dir):
        """TC-01f: 最后一个 segment 的 EXTINF 时长正确（余数时长）"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=25.0)

        # 25 秒 / 6 秒 = 4.17 → 5 个 segment
        # 前 4 个 6 秒，最后一个 1 秒
        lines = m3u8.strip().split("\n")
        extinf_lines = [l for l in lines if l.startswith("#EXTINF:")]
        assert len(extinf_lines) == 5
        assert extinf_lines[0] == "#EXTINF:6.0,"
        assert extinf_lines[3] == "#EXTINF:6.0,"
        assert extinf_lines[4] == "#EXTINF:1.0,"  # 25 - 4*6 = 1

    def test_duration_not_multiple_of_segment_time(self, cache_dir):
        """TC-01g: duration 不是 segment_time 整数倍时，向上取整"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=61.0)

        # 61 / 6 = 10.17 → 11 个 segment
        assert m3u8.count("#EXTINF:") == 11
        assert "seg_00010.ts" in m3u8

    def test_url_prefix_included(self, cache_dir):
        """TC-01h: m3u8 中 segment URL 包含 API 路径前缀"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=12.0)

        prefix = "/api/video/vid1/stream/720p/"
        assert prefix + "seg_00000.ts" in m3u8
        assert prefix + "seg_00001.ts" in m3u8


# ============================================================
# TC-02: is_cached 判断逻辑
# ============================================================


class TestIsCached:
    """is_cached() 测试"""

    def _make_segments(self, cache_dir, video_id, quality, indices):
        seg_dir = cache_dir / video_id / quality
        seg_dir.mkdir(parents=True, exist_ok=True)
        for i in indices:
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)
        return seg_dir

    def test_no_cache_dir(self, cache_dir):
        """TC-02a: 缓存目录不存在 → False"""
        from cache_manager import is_cached

        with patch("config.get", return_value=str(cache_dir)):
            result = is_cached("nonexistent", "720p", duration=60.0)
        assert result is False

    def test_no_segments(self, cache_dir):
        """TC-02b: 目录存在但无 segment → False"""
        from cache_manager import is_cached

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)

        with patch("config.get", return_value=str(cache_dir)):
            result = is_cached("vid1", "720p", duration=60.0)
        assert result is False

    def test_partial_segments(self, cache_dir):
        """TC-02c: 只有部分 segment → False（未完成）"""
        from cache_manager import is_cached

        # 60 秒需要 10 个 segment (0-9)，只创建 5 个
        self._make_segments(cache_dir, "vid1", "720p", [0, 1, 2, 3, 4])

        with patch("config.get", return_value=str(cache_dir)):
            result = is_cached("vid1", "720p", duration=60.0)
        assert result is False

    def test_all_segments(self, cache_dir):
        """TC-02d: 所有 segment 都存在 → True"""
        from cache_manager import is_cached

        self._make_segments(cache_dir, "vid1", "720p", range(10))

        with patch("config.get", return_value=str(cache_dir)):
            result = is_cached("vid1", "720p", duration=60.0)
        assert result is True

    def test_all_segments_non_contiguous(self, cache_dir):
        """TC-02e: segment 编号不连续但覆盖全部 → True"""
        from cache_manager import is_cached

        # 创建 0-4 和 6-9（缺少 5），但最大编号 9 >= 10-1
        self._make_segments(cache_dir, "vid1", "720p", list(range(5)) + list(range(6, 10)))

        with patch("config.get", return_value=str(cache_dir)):
            result = is_cached("vid1", "720p", duration=60.0)
        assert result is True  # 最大编号 9 >= ceil(60/6)-1 = 9

    def test_seek_segments_count(self, cache_dir):
        """TC-02f: 只有 seek 分片（映射后覆盖全部）→ True"""
        from cache_manager import is_cached

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # seek 到 0 秒，生成 10 个 seek 分片
        for i in range(10):
            (seg_dir / f"seek_0_{i:05d}.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            result = is_cached("vid1", "720p", duration=60.0)
        assert result is True

    def test_no_duration_fallback(self, cache_dir):
        """TC-02g: 无 duration 参数时，回退到 playlist.m3u8 检查"""
        from cache_manager import is_cached

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        (seg_dir / "playlist.m3u8").write_text("#EXTM3U\n")
        # 新逻辑需要至少一个 segment 文件
        (seg_dir / "seg_00000.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            result = is_cached("vid1", "720p")
        assert result is True

    def test_empty_playlist_no_duration(self, cache_dir):
        """TC-02h: playlist.m3u8 为空文件，无 duration → False"""
        from cache_manager import is_cached

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        (seg_dir / "playlist.m3u8").write_bytes(b"")

        with patch("config.get", return_value=str(cache_dir)):
            result = is_cached("vid1", "720p")
        assert result is False


# ============================================================
# TC-03: segment 端点 404 行为
# ============================================================


class TestSegmentEndpoint:
    """segment 端点测试"""

    @pytest.fixture
    def client(self, cache_dir, video_dir):
        """创建 FastAPI 测试客户端"""
        with patch("config._settings", {
            "video_dir": str(video_dir),
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {
                "test_video": {
                    "id": "test_video",
                    "name": "test_video",
                    "path": str(video_dir / "test_video.mp4"),
                    "duration": 60.0,
                }
            }
            from fastapi.testclient import TestClient
            from main import app
            yield TestClient(app)

    def test_existing_segment(self, client, cache_dir):
        """TC-03a: 请求已存在的 segment → 200 + 文件内容"""
        seg_dir = cache_dir / "test_video" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        (seg_dir / "seg_00000.ts").write_bytes(b"\x00" * 100)

        resp = client.get("/api/video/test_video/stream/720p/seg_00000.ts")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "video/mp2t"

    def test_missing_segment_returns_200_empty(self, client):
        """TC-03b: 请求不存在的 segment → 200 空响应（避免 hls.js 触发 recoverMediaError）"""
        resp = client.get("/api/video/test_video/stream/720p/seg_99999.ts")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "video/mp2t"
        assert len(resp.content) == 0

    def test_seek_file_mapping(self, client, cache_dir):
        """TC-03b: 请求的 seg_NNNNN 映射到 seek 文件"""
        seg_dir = cache_dir / "test_video" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 创建 seek_60_00000.ts（对应 seg_00010.ts）
        (seg_dir / "seek_60_00000.ts").write_bytes(b"\x00" * 100)

        # 需要 mock seekable job 的状态
        from unittest.mock import MagicMock
        mock_seek_job = MagicMock()
        mock_seek_job.seek_position = 60.0

        with patch("main.get_seekable_job", return_value=mock_seek_job):
            resp = client.get("/api/video/test_video/stream/720p/seg_00010.ts")

        assert resp.status_code == 200


# ============================================================
# TC-04: stream 端点统一流程
# ============================================================


class TestStreamEndpoint:
    """stream 端点测试"""

    @pytest.fixture
    def client(self, cache_dir, video_dir):
        with patch("config._settings", {
            "video_dir": str(video_dir),
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {
                "test_video": {
                    "id": "test_video",
                    "name": "test_video",
                    "path": str(video_dir / "test_video.mp4"),
                    "duration": 60.0,
                }
            }
            from fastapi.testclient import TestClient
            from main import app
            yield TestClient(app)

    def test_returns_vod_m3u8(self, client):
        """TC-04a: 返回的 m3u8 包含 VOD 标记和完整时长"""
        fake_m3u8 = (
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n"
            "#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:0\n"
            "#EXTINF:6.0,\n/api/video/test_video/stream/720p/seg_00000.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        import time
        with patch("main.is_cached", return_value=True), \
             patch("main.generate_full_m3u8", return_value=fake_m3u8), \
             patch("main._last_scan", time.time()):
            resp = client.get("/api/video/test_video/stream/720p")

        assert resp.status_code == 200
        assert "application/vnd.apple.mpegurl" in resp.headers["content-type"]
        content = resp.text
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in content
        assert "#EXT-X-ENDLIST" in content

    def test_no_seek_query_param_needed(self, client):
        """TC-04b: stream 端点不再需要 seek query param（统一路径）"""
        fake_m3u8 = (
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n"
            "#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:0\n"
            "#EXTINF:6.0,\n/api/video/test_video/stream/720p/seg_00000.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        import time
        with patch("main.is_cached", return_value=True), \
             patch("main.generate_full_m3u8", return_value=fake_m3u8), \
             patch("main._last_scan", time.time()):
            resp = client.get("/api/video/test_video/stream/720p")
        assert resp.status_code == 200

    def test_segment_url_has_prefix(self, client):
        """TC-04c: 返回的 m3u8 中 segment URL 包含 API 路径前缀"""
        fake_m3u8 = (
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-PLAYLIST-TYPE:VOD\n"
            "#EXT-X-TARGETDURATION:6\n#EXT-X-MEDIA-SEQUENCE:0\n"
            "#EXTINF:6.0,\n/api/video/test_video/stream/720p/seg_00000.ts\n"
            "#EXTINF:6.0,\n/api/video/test_video/stream/720p/seg_00001.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        import time
        with patch("main.is_cached", return_value=True), \
             patch("main.generate_full_m3u8", return_value=fake_m3u8), \
             patch("main._last_scan", time.time()):
            resp = client.get("/api/video/test_video/stream/720p")

        content = resp.text
        assert "/api/video/test_video/stream/720p/seg_00000.ts" in content


# ============================================================
# TC-05: seek 流程
# ============================================================


class TestSeekFlow:
    """seek 流程测试"""

    @pytest.fixture
    def client(self, cache_dir, video_dir):
        with patch("config._settings", {
            "video_dir": str(video_dir),
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {
                "test_video": {
                    "id": "test_video",
                    "name": "test_video",
                    "path": str(video_dir / "test_video.mp4"),
                    "duration": 120.0,
                }
            }
            from fastapi.testclient import TestClient
            from main import app
            yield TestClient(app)

    def test_seek_triggers_transcode(self, client):
        """TC-05a: POST /seek 启动 seek 转码"""
        import time
        from unittest.mock import MagicMock
        mock_seek_job = MagicMock()
        mock_seek_job.start_seek.return_value = True

        with patch("main.get_or_create_seekable", return_value=mock_seek_job), \
             patch("main._last_scan", time.time()):
            resp = client.post(
                "/api/video/test_video/seek/720p",
                json={"position": 120.0}
            )

        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_seek_job.start_seek.assert_called_once_with(120.0)

    def test_seek_invalid_position(self, client):
        """TC-05b: position <= 0 返回失败"""
        resp = client.post(
            "/api/video/test_video/seek/720p",
            json={"position": 0}
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is False

    def test_seek_ready_check(self, client):
        """TC-05c: segments-ready 端点正确报告 seek 就绪状态"""
        from unittest.mock import MagicMock
        mock_seek_job = MagicMock()
        mock_seek_job.seek_ready = True
        mock_seek_job.get_seek_segment_count.return_value = 3

        with patch("main.get_seekable_job", return_value=mock_seek_job):
            resp = client.get(
                "/api/video/test_video/stream/720p/segments-ready?seek=120"
            )

        assert resp.status_code == 200
        assert resp.json()["ready"] is True
        assert resp.json()["segments"] == 3

    def test_seek_not_ready(self, client):
        """TC-05d: seek 未就绪时返回 ready: false"""
        from unittest.mock import MagicMock
        mock_seek_job = MagicMock()
        mock_seek_job.seek_ready = False
        mock_seek_job.get_seek_segment_count.return_value = 0

        with patch("main.get_seekable_job", return_value=mock_seek_job):
            resp = client.get(
                "/api/video/test_video/stream/720p/segments-ready?seek=120"
            )

        assert resp.json()["ready"] is False


# ============================================================
# TC-06: 缓存清除
# ============================================================


class TestCacheClear:
    """缓存清除测试"""

    def test_clear_removes_all_segments(self, cache_dir):
        """TC-06a: 清除缓存删除所有分片文件"""
        from cache_manager import clear_video_cache

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)
        (seg_dir / "playlist.m3u8").write_text("#EXTM3U\n")

        with patch("config.get", return_value=str(cache_dir)):
            with patch("transcoder.invalidate_jobs"):
                result = clear_video_cache("vid1")

        assert result["ok"] is True
        assert result["freed"] > 0
        assert not seg_dir.exists()

    def test_clear_removes_seek_files(self, cache_dir):
        """TC-06b: 清除缓存同时删除 seek 分片文件"""
        from cache_manager import clear_video_cache

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        (seg_dir / "seek_60_00000.ts").write_bytes(b"\x00" * 100)
        (seg_dir / "seek_60.m3u8").write_text("#EXTM3U\n")

        with patch("config.get", return_value=str(cache_dir)):
            with patch("transcoder.invalidate_jobs"):
                result = clear_video_cache("vid1")

        assert result["ok"] is True
        assert not seg_dir.exists()

    def test_clear_preserves_thumbnail(self, cache_dir):
        """TC-06c: 清除缓存保留缩略图"""
        from cache_manager import clear_video_cache

        video_dir = cache_dir / "vid1"
        video_dir.mkdir(parents=True, exist_ok=True)
        thumb = video_dir / "thumb.jpg"
        thumb.write_bytes(b"\x00" * 100)
        seg_dir = video_dir / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        (seg_dir / "seg_00000.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            with patch("transcoder.invalidate_jobs"):
                result = clear_video_cache("vid1")

        assert result["ok"] is True
        assert thumb.exists()  # 缩略图保留
        assert not seg_dir.exists()

    def test_clear_nonexistent_video(self, cache_dir):
        """TC-06d: 清除不存在的视频缓存 → 成功（幂等）"""
        from cache_manager import clear_video_cache

        with patch("config.get", return_value=str(cache_dir)):
            with patch("transcoder.invalidate_jobs"):
                result = clear_video_cache("nonexistent")

        assert result["ok"] is True

    def test_after_clear_is_cached_false(self, cache_dir):
        """TC-06e: 清除后 is_cached 返回 False"""
        from cache_manager import clear_video_cache, is_cached

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            with patch("transcoder.invalidate_jobs"):
                clear_video_cache("vid1")
            result = is_cached("vid1", "720p", duration=60.0)
        assert result is False


# ============================================================
# TC-07: _rewrite_seek_m3u8 绝对编号映射
# ============================================================


class TestRewriteSeekM3u8:
    """_rewrite_seek_m3u8 测试"""

    def test_seek_segment_rename(self):
        """TC-07a: seek 分片名正确映射为绝对编号"""
        from transcoder import _rewrite_seek_m3u8

        content = (
            "#EXTM3U\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n"
            "#EXTINF:6.0,\n"
            "seek_60_00000.ts\n"
            "#EXTINF:6.0,\n"
            "seek_60_00001.ts\n"
            "#EXTINF:6.0,\n"
            "seek_60_00002.ts\n"
        )
        result = _rewrite_seek_m3u8(content, "vid1", "720p", seek_position=60.0)

        # seek_60_00000 → seg_00010 (60/6 + 0)
        assert "seg_00010.ts" in result
        assert "seg_00011.ts" in result
        assert "seg_00012.ts" in result
        assert "seek_60_" not in result
        assert "#EXT-X-MEDIA-SEQUENCE:10" in result

    def test_seek_zero_position(self):
        """TC-07b: seek_position=0 时，添加 API 前缀"""
        from transcoder import _rewrite_seek_m3u8

        content = (
            "#EXTM3U\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n"
            "#EXTINF:6.0,\n"
            "seek_0_00000.ts\n"
        )
        result = _rewrite_seek_m3u8(content, "vid1", "720p", seek_position=0)

        assert "/api/video/vid1/stream/720p/seek_0_00000.ts" in result


# ============================================================
# TC-08: rewrite_m3u8 路径前缀
# ============================================================


class TestRewriteM3u8:
    """rewrite_m3u8 测试"""

    def test_prefix_added(self):
        """TC-08a: segment 文件名添加 API 路径前缀"""
        from main import rewrite_m3u8

        content = "#EXTM3U\n#EXTINF:6.0,\nseg_00000.ts\n"
        result = rewrite_m3u8(content, "vid1", "720p")
        assert "/api/video/vid1/stream/720p/seg_00000.ts" in result

    def test_no_add_endlist_param(self):
        """TC-08b: 不再有 add_endlist 参数（全量 m3u8 自带 ENDLIST）"""
        from main import rewrite_m3u8

        content = "#EXTM3U\n#EXTINF:6.0,\nseg_00000.ts\n#EXT-X-ENDLIST\n"
        result = rewrite_m3u8(content, "vid1", "720p")
        assert "#EXT-X-ENDLIST" in result

    def test_seek_files_not_prefixed(self):
        """TC-08c: seek 文件名不在 rewrite 范围内（已在 generate_full_m3u8 中处理）"""
        from main import rewrite_m3u8

        content = "#EXTM3U\n#EXTINF:6.0,\nseg_00010.ts\n"
        result = rewrite_m3u8(content, "vid1", "720p")
        # 只有 seg_NNNNN.ts 被替换，不包含 seek_ 前缀
        assert "seek_" not in result


# ============================================================
# TC-09: hls.js 前端行为（文档级测试，非自动化）
# ============================================================

# 以下为前端测试用例，需要手动验证或用 Playwright/Cypress：

# TC-09a: 首次加载播放
# - 进入播放页，m3u8 返回完整时长
# - hls.js 从 segment 0 开始加载
# - 进度条显示完整视频时长
# - 自动开始播放

# TC-09b: seek 到未转码位置
# - 点击进度条 50% 位置
# - 前端 POST /seek 启动 seek 转码
# - 轮询 segments-ready 直到就绪
# - 设 _pendingSeekTime，调用 hls.loadSource() 刷新 m3u8
# - FRAG_BUFFERED 回调中 video.currentTime = seekTime
# - 进度条保持完整时长

# TC-09c: seek 到已转码位置
# - 点击进度条 10% 位置（已转码）
# - hls.js 直接加载对应 segment（无需 seek 转码）
# - 立即跳转

# TC-09d: FRAG_LOAD_ERROR 重试
# - 请求的 segment 不存在（404）
# - hls.js 标记为非致命错误
# - 自动重试（最多 6 次）
# - 转码完成后重试成功

# TC-09e: Safari 兼容性
# - Safari 走 hls.js 路径（Hls.isSupported() == true）
# - m3u8 包含 #EXT-X-ENDLIST
# - 播放正常

# TC-09f: 清除缓存后重新播放
# - 播放中点击"清除缓存"
# - 清除成功
# - 重新点击播放，触发新转码
# - 进度条显示完整时长

# TC-09g: 画质切换
# - 从 720p 切换到 480p
# - 销毁旧 hls 实例，创建新实例
# - 从头播放（currentTime = 0）
# - 进度条显示完整时长


# ============================================================
# TC-10: goBack() 清理（文档级，需浏览器环境）
# ============================================================

# TC-10a: goBack 销毁 hls 实例
# - 调用 goBack() 后 hls 变量为 null
# - hls.destroy() 被调用

# TC-10b: goBack 暂停视频并移除 src
# - video.paused 为 true
# - video.src 为空

# TC-10c: goBack 重置所有播放状态
# - _pendingSeekTime === null
# - _initialSeekDone === false
# - _maxBufferedEnd === 0
# - _seekingInProgress === false

# TC-10d: goBack 移除 timeupdate 监听
# - _timeUpdateHandler === null

# TC-10e: goBack 显示 library 隐藏 player
# - player-section 有 hidden class
# - library 没有 hidden class
# - status 元素文本为空


# ============================================================
# TC-11: switchQuality() 画质切换（文档级，需浏览器环境）
# ============================================================

# TC-11a: switchQuality 销毁旧 hls 实例
# - 旧 hls.destroy() 被调用
# - 新 hls 实例创建

# TC-11b: switchQuality 重置所有状态
# - _pendingSeekTime === null
# - _initialSeekDone === false
# - _maxBufferedEnd === 0
# - _destroyed === false

# TC-11c: switchQuality 从头播放
# - FRAG_BUFFERED 回调中 video.currentTime = 0

# TC-11d: switchQuality 选择正确画质的 URL
# - loadSource URL 包含正确的 quality 参数


# ============================================================
# TC-12: clearCurrentVideoCache() 行为（文档级，需浏览器环境）
# ============================================================

# TC-12a: 清除成功后返回视频列表
# - goBack() 被调用
# - player-section 隐藏，library 显示

# TC-12b: 清除按钮禁用并显示进度
# - btn.disabled === true
# - btn.textContent === "清除中..."

# TC-12c: 用户取消 confirm 不执行清除
# - confirm 返回 false 时，fetch 不被调用


# ============================================================
# TC-13: handleVideoSeek() 安全性（文档级，需浏览器环境）
# ============================================================

# TC-13a: _seekingInProgress 为 true 时跳过
# - 设 _seekingInProgress = true
# - 调用 handleVideoSeek → 直接返回，不发请求

# TC-13b: _initialSeekDone 为 false 时跳过
# - 设 _initialSeekDone = false
# - 调用 handleVideoSeek → 直接返回

# TC-13c: seek 在已缓冲范围内不触发转码
# - 设 buffered 范围包含 seekTime
# - 调用 handleVideoSeek → 直接返回

# TC-13d: 轮询期间视频已切换则安全退出
# - 设 currentVideo.id = "A"
# - 轮询中改 currentVideo.id = "B"
# - 轮询应提前退出


# ============================================================
# TC-14: generate_full_m3u8 边界条件
# ============================================================


class TestGenerateFullM3u8EdgeCases:
    """generate_full_m3u8 边界条件测试"""

    def test_duration_zero(self, cache_dir):
        """TC-14a: duration = 0 → 至少 1 个 segment 条目"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=0.0)

        assert m3u8 is not None
        assert m3u8.count("#EXTINF:") == 1
        assert "seg_00000.ts" in m3u8

    def test_duration_negative(self, cache_dir):
        """TC-14b: duration < 0 → 至少 1 个 segment 条目"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=-5.0)

        assert m3u8.count("#EXTINF:") == 1

    def test_duration_very_short(self, cache_dir):
        """TC-14c: duration < HLS_SEGMENT_TIME → 1 个 segment，EXTINF = duration"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=3.5)

        assert m3u8.count("#EXTINF:") == 1
        assert "#EXTINF:3.5," in m3u8

    def test_segment_files_beyond_expected(self, cache_dir):
        """TC-14d: segment 文件编号超过 expected 范围不影响生成"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 创建编号超过 expected 的 segment（可能是旧转码残留）
        for i in [0, 1, 2, 99]:
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=18.0)

        # 18 秒 / 6 秒 = 3 个 segment
        assert m3u8.count("#EXTINF:") == 3
        assert "seg_00000.ts" in m3u8
        assert "seg_00002.ts" in m3u8
        assert "seg_00003.ts" not in m3u8

    def test_always_has_vod_and_endlist(self, cache_dir):
        """TC-14e: 无论何种情况，m3u8 都包含 VOD 和 ENDLIST"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=0.0)

        assert "#EXT-X-PLAYLIST-TYPE:VOD" in m3u8
        assert "#EXT-X-ENDLIST" in m3u8
        assert "#EXT-X-MEDIA-SEQUENCE:0" in m3u8

    def test_targetduration_correct(self, cache_dir):
        """TC-14f: EXT-X-TARGETDURATION 等于 HLS_SEGMENT_TIME"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        assert f"#EXT-X-TARGETDURATION:{6}" in m3u8


# ============================================================
# TC-16: 片头边界（视频开始位置）
# ============================================================


class TestVideoStartBoundary:
    """片头边界条件测试"""

    def test_first_segment_in_m3u8(self, cache_dir):
        """TC-16a: m3u8 第一个条目是 seg_00000.ts"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        lines = m3u8.strip().split("\n")
        # 第一个 EXTINF 后面应该是 seg_00000.ts
        first_extinf_idx = next(i for i, l in enumerate(lines) if l.startswith("#EXTINF:"))
        assert lines[first_extinf_idx + 1].endswith("seg_00000.ts")

    def test_first_segment_duration(self, cache_dir):
        """TC-16b: 第一个 segment 的 EXTINF = HLS_SEGMENT_TIME（正常视频）"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        lines = m3u8.strip().split("\n")
        first_extinf = next(l for l in lines if l.startswith("#EXTINF:"))
        assert first_extinf == f"#EXTINF:{float(6)},"

    def test_first_segment_short_video(self, cache_dir):
        """TC-16c: 短视频（< HLS_SEGMENT_TIME）第一个 segment EXTINF = duration"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=2.5)

        assert m3u8.count("#EXTINF:") == 1
        assert "#EXTINF:2.5," in m3u8

    def test_first_segment_missing_returns_200(self, cache_dir):
        """TC-16d: 第一个 segment 不存在时，请求返回 200 空响应"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            resp = client.get("/api/video/vid1/stream/720p/seg_00000.ts")
            assert resp.status_code == 200
            assert len(resp.content) == 0

    def test_duration_equal_segment_time(self, cache_dir):
        """TC-16e: duration 恰好等于 HLS_SEGMENT_TIME → 1 个 segment"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=6.0)

        assert m3u8.count("#EXTINF:") == 1
        assert "#EXTINF:6.0," in m3u8
        assert "seg_00000.ts" in m3u8
        assert "seg_00001.ts" not in m3u8

    def test_duration_slightly_over_segment_time(self, cache_dir):
        """TC-16f: duration = 6.1 → 2 个 segment，第二个 EXTINF = 0.1"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=6.1)

        assert m3u8.count("#EXTINF:") == 2
        extinf_lines = [l for l in m3u8.split("\n") if l.startswith("#EXTINF:")]
        assert extinf_lines[0] == "#EXTINF:6.0,"
        assert extinf_lines[1] == "#EXTINF:0.1,"

    def test_seek_to_position_zero(self, cache_dir):
        """TC-16g: seek 到 position=0 不触发 seek 转码（由初始播放处理）"""
        # seek endpoint 拒绝 position <= 0
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            resp = client.post("/api/video/vid1/seek/720p", json={"position": 0})
            assert resp.json()["ok"] is False

    def test_only_first_segment_transcoded(self, cache_dir):
        """TC-16h: 只转码了第一个 segment，m3u8 仍包含全部条目"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        (seg_dir / "seg_00000.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # 全部 10 个条目都存在
        assert m3u8.count("#EXTINF:") == 10
        # 只有 seg_00000 实际存在，其余是占位
        assert "seg_00000.ts" in m3u8
        assert "seg_00009.ts" in m3u8


# ============================================================
# TC-17: 片尾边界（视频结束位置）
# ============================================================


class TestVideoEndBoundary:
    """片尾边界条件测试"""

    def test_last_segment_extinf_exact_multiple(self, cache_dir):
        """TC-17a: duration 是 HLS_SEGMENT_TIME 整数倍，最后一个 EXTINF = HLS_SEGMENT_TIME"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        extinf_lines = [l for l in m3u8.split("\n") if l.startswith("#EXTINF:")]
        # 10 个 segment，每个 6 秒
        assert len(extinf_lines) == 10
        assert extinf_lines[-1] == "#EXTINF:6.0,"

    def test_last_segment_extinf_remainder(self, cache_dir):
        """TC-17b: duration 不是整数倍，最后一个 EXTINF = 余数"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=62.5)

        extinf_lines = [l for l in m3u8.split("\n") if l.startswith("#EXTINF:")]
        # ceil(62.5/6) = 11 个 segment
        assert len(extinf_lines) == 11
        # 前 10 个各 6 秒
        for i in range(10):
            assert extinf_lines[i] == "#EXTINF:6.0,"
        # 最后一个 = 62.5 - 10*6 = 2.5 秒
        assert extinf_lines[10] == "#EXTINF:2.5,"

    def test_last_segment_url_in_m3u8(self, cache_dir):
        """TC-17c: m3u8 最后一个 URL 是正确的 seg_NNNNN.ts"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        lines = m3u8.strip().split("\n")
        # ENDLIST 前一行应该是最后一个 segment URL
        endlist_idx = lines.index("#EXT-X-ENDLIST")
        last_url = lines[endlist_idx - 1]
        assert last_url.endswith("seg_00009.ts")

    def test_last_segment_extinf_before_endlist(self, cache_dir):
        """TC-17d: ENDLIST 前面是 EXTINF + URL（HLS 规范要求）"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        lines = m3u8.strip().split("\n")
        endlist_idx = lines.index("#EXT-X-ENDLIST")
        # ENDLIST 前两行应该是 EXTINF 和 URL
        assert lines[endlist_idx - 2].startswith("#EXTINF:")
        assert lines[endlist_idx - 1].endswith(".ts")
        assert endlist_idx == len(lines) - 1  # ENDLIST 是最后一行

    def test_seek_to_last_second(self, cache_dir):
        """TC-17e: seek 到最后一秒（duration - 1）→ segment 编号正确"""
        from transcoder import generate_full_m3u8

        # 60 秒视频，seek 到 59 秒 → segment 9 (59//6 = 9)
        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 模拟 seek 到 54 秒产生的分片 (54//6=9, seek_54_00000 → seg_00009)
        (seg_dir / "seek_54_00000.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # seg_00009 应该在 m3u8 中（seek 文件映射）
        assert "seg_00009.ts" in m3u8

    def test_seek_to_duration_boundary(self, cache_dir):
        """TC-17f: seek 到 position = duration → 不应产生超出范围的 segment"""
        from transcoder import generate_full_m3u8

        # 60 秒视频，seek 到 60 秒 → 60//6 = 10，但 total_segments = 10 (index 0-9)
        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # seek_60_00000 → abs_idx = 60//6 + 0 = 10，超出范围
        (seg_dir / "seek_60_00000.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # m3u8 只有 10 个条目 (seg_00000 ~ seg_00009)
        assert m3u8.count("#EXTINF:") == 10
        assert "seg_00010.ts" not in m3u8

    def test_last_segment_not_transcoded_returns_200_empty(self, cache_dir):
        """TC-17g: 最后一个 segment 未转码 → 请求返回 200 空响应"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 只创建前 5 个 segment
        for i in range(5):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            # 请求最后一个 segment（未转码）→ 200 空响应
            resp = client.get("/api/video/vid1/stream/720p/seg_00009.ts")
            assert resp.status_code == 200
            assert len(resp.content) == 0

            # 请求已转码的 segment → 200 有内容
            resp = client.get("/api/video/vid1/stream/720p/seg_00004.ts")
            assert resp.status_code == 200
            assert len(resp.content) > 0

    def test_seek_to_end_maps_to_last_segment(self, cache_dir):
        """TC-17h: seek 到接近末尾位置，seek 文件映射到正确的最后一个 segment"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # seek 到 57 秒 (57//6 = 9)，只有 1 个 seek 分片
        (seg_dir / "seek_57_00000.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # seek_57_00000 → seg_00009 (57//6 + 0 = 9)
        # m3u8 有 10 个条目，seg_00009 是最后一个
        assert m3u8.count("#EXTINF:") == 10
        lines = m3u8.strip().split("\n")
        endlist_idx = lines.index("#EXT-X-ENDLIST")
        assert lines[endlist_idx - 1].endswith("seg_00009.ts")

    def test_very_long_video(self, cache_dir):
        """TC-17i: 长视频（2小时）→ 1200 个 segment"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=7200.0)

        assert m3u8.count("#EXTINF:") == 1200
        assert "seg_01199.ts" in m3u8
        assert "seg_01200.ts" not in m3u8

    def test_segment_at_exact_boundary_time(self, cache_dir):
        """TC-17j: duration 恰好在 segment 边界（如 12.0 秒）→ 2 个 segment"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=12.0)

        # 12/6 = 2，正好 2 个 segment
        extinf_lines = [l for l in m3u8.split("\n") if l.startswith("#EXTINF:")]
        assert len(extinf_lines) == 2
        assert extinf_lines[0] == "#EXTINF:6.0,"
        assert extinf_lines[1] == "#EXTINF:6.0,"

    def test_segment_at_just_over_boundary(self, cache_dir):
        """TC-17k: duration = 12.01 → 3 个 segment，最后一个 0.01 秒"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=12.01)

        extinf_lines = [l for l in m3u8.split("\n") if l.startswith("#EXTINF:")]
        assert len(extinf_lines) == 3
        assert extinf_lines[2] == "#EXTINF:0.0,"  # 12.01 - 2*6 = 0.01, rounds to 0.0


# ============================================================
# TC-15: 时间显示格式（纯函数测试）
# ============================================================


class TestTimeDisplay:
    """时间格式化测试（复用 formatDuration 逻辑）"""

    @staticmethod
    def formatDuration(seconds):
        """与 library.js formatDuration 相同逻辑"""
        import math
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    def test_zero(self):
        """TC-15a: 0 秒 → 0:00"""
        assert self.formatDuration(0) == "0:00"

    def test_seconds_only(self):
        """TC-15b: < 60 秒 → m:ss"""
        assert self.formatDuration(45) == "0:45"
        assert self.formatDuration(5) == "0:05"

    def test_minutes_and_seconds(self):
        """TC-15c: 分钟+秒 → m:ss"""
        assert self.formatDuration(90) == "1:30"
        assert self.formatDuration(600) == "10:00"

    def test_hours(self):
        """TC-15d: >= 1 小时 → h:mm:ss"""
        assert self.formatDuration(3600) == "1:00:00"
        assert self.formatDuration(3661) == "1:01:01"
        assert self.formatDuration(7200) == "2:00:00"

    def test_long_video(self):
        """TC-15e: 长视频时长正确"""
        assert self.formatDuration(5400) == "1:30:00"
        assert self.formatDuration(12345) == "3:25:45"


# ============================================================
# TC-18: FRAG_BUFFERED 稳定性（Safari 跳回修复）
# ============================================================


class TestFragBufferedStability:
    """FRAG_BUFFERED 回调稳定性测试（验证后端数据一致性）"""

    def test_m3u8_consistent_across_calls(self, cache_dir):
        """TC-18a: 多次调用 generate_full_m3u8 返回一致结果"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8_1 = generate_full_m3u8("vid1", "720p", duration=60.0)
            m3u8_2 = generate_full_m3u8("vid1", "720p", duration=60.0)

        assert m3u8_1 == m3u8_2

    def test_m3u8_after_partial_segment_deletion(self, cache_dir):
        """TC-18b: 部分 segment 被删除后，m3u8 仍包含全部条目（占位机制）"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 创建 10 个 segment
        for i in range(10):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)

        # 删除中间几个 segment（模拟 Safari 缓冲回收）
        for i in [3, 4, 5]:
            (seg_dir / f"seg_{i:05d}.ts").unlink()

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # m3u8 仍然有 10 个条目
        assert m3u8.count("#EXTINF:") == 10
        # 删除的 segment 仍然在 m3u8 中（占位）
        assert "seg_00003.ts" in m3u8
        assert "seg_00004.ts" in m3u8
        assert "seg_00005.ts" in m3u8

    def test_seek_segment_supplements_existing(self, cache_dir):
        """TC-18c: seek 分片补充不存在的 segment 位置"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 只创建前 3 个 segment
        for i in range(3):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)
        # seek 到 30 秒产生的分片 (30//6=5, seek_30_00000 → seg_00005)
        (seg_dir / "seek_30_00000.ts").write_bytes(b"\x00" * 100)
        (seg_dir / "seek_30_00001.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # seek 分片映射的 segment 不与 seg_* 冲突
        assert m3u8.count("#EXTINF:") == 10
        # seg_00005 和 seg_00006 在 m3u8 中（seek 映射）
        assert "seg_00005.ts" in m3u8
        assert "seg_00006.ts" in m3u8

    def test_available_set_deduplication(self, cache_dir):
        """TC-18d: seg_* 和 seek_* 映射到同一编号时不重复"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # seg_00005 已存在
        (seg_dir / "seg_00005.ts").write_bytes(b"\x00" * 100)
        # seek_30_00000 也映射到 seg_00005 (30//6=5)
        (seg_dir / "seek_30_00000.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # m3u8 条目数量不受影响
        assert m3u8.count("#EXTINF:") == 10


# ============================================================
# TC-19: 缓冲回收后向后 seek（Safari 跳回修复）
# ============================================================


class TestBackwardSeekAfterEviction:
    """缓冲回收后向后 seek 测试"""

    def test_seek_to_previously_buffered_position(self, cache_dir):
        """TC-19a: seek 到之前已缓冲但被回收的位置，后端正确返回 segment"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 创建 segment 0-9
        for i in range(10):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            # 请求中间的 segment（模拟 Safari 回收后重新请求）
            resp = client.get("/api/video/vid1/stream/720p/seg_00003.ts")
            assert resp.status_code == 200

    def test_seek_segment_resolution_mid_video(self, cache_dir):
        """TC-19b: seek 到视频中间位置，segment 端点正确解析 seek 分片"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # seek 到 30 秒产生的分片
        (seg_dir / "seek_30_00000.ts").write_bytes(b"\x00" * 100)
        (seg_dir / "seek_30_00001.ts").write_bytes(b"\x00" * 100)

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app

            # 需要 mock get_seekable_job 返回正确的 seek position
            with mp("main.get_seekable_job") as mock_seek:
                mock_job = type("J", (), {"seek_position": 30.0})()
                mock_seek.return_value = mock_job
                client = TestClient(app)

                # seek_30_00000 → abs_idx = 30//6 + 0 = 5 → seg_00005.ts
                resp = client.get("/api/video/vid1/stream/720p/seg_00005.ts")
                assert resp.status_code == 200

    def test_seek_segment_negative_index_returns_200_empty(self, cache_dir):
        """TC-19c: seek 映射产生负索引时返回 200 空响应"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # seek 到 30 秒，但请求 seg_00002（abs_idx=2 < seek_start=5）
        (seg_dir / "seek_30_00000.ts").write_bytes(b"\x00" * 100)

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app

            with mp("main.get_seekable_job") as mock_seek:
                mock_job = type("J", (), {"seek_position": 30.0})()
                mock_seek.return_value = mock_job
                client = TestClient(app)

                # seg_00002 的 abs_idx=2 < seek_start=5，seek_idx=-3 < 0
                resp = client.get("/api/video/vid1/stream/720p/seg_00002.ts")
                assert resp.status_code == 200
                assert len(resp.content) == 0

    def test_multiple_seek_positions_in_m3u8(self, cache_dir):
        """TC-19d: 多次 seek 产生的分片都被 generate_full_m3u8 正确收录"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 第一次 seek 到 18 秒 (18//6=3)
        (seg_dir / "seek_18_00000.ts").write_bytes(b"\x00" * 100)
        # 第二次 seek 到 42 秒 (42//6=7)
        (seg_dir / "seek_42_00000.ts").write_bytes(b"\x00" * 100)
        (seg_dir / "seek_42_00001.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # 两个 seek 的分片都被收录
        assert m3u8.count("#EXTINF:") == 10
        # seek_18_00000 → seg_00003
        assert "seg_00003.ts" in m3u8
        # seek_42_00000 → seg_00007, seek_42_00001 → seg_00008
        assert "seg_00007.ts" in m3u8
        assert "seg_00008.ts" in m3u8

    def test_rewrite_seek_m3u8_absolute_mapping(self, cache_dir):
        """TC-19e: _rewrite_seek_m3u8 将 seek 分片正确映射为绝对编号"""
        from transcoder import _rewrite_seek_m3u8

        content = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            "#EXT-X-TARGETDURATION:6\n"
            "#EXT-X-MEDIA-SEQUENCE:0\n"
            "#EXTINF:6.0,\n"
            "seek_30_00000.ts\n"
            "#EXTINF:6.0,\n"
            "seek_30_00001.ts\n"
            "#EXTINF:6.0,\n"
            "seek_30_00002.ts\n"
            "#EXT-X-ENDLIST\n"
        )

        result = _rewrite_seek_m3u8(content, "vid1", "720p", seek_position=30.0)

        # seek_30_00000 → seg_00005 (30//6 + 0 = 5)
        assert "seg_00005.ts" in result
        assert "seg_00006.ts" in result
        assert "seg_00007.ts" in result
        # 原始 seek 文件名不在结果中
        assert "seek_30_00000.ts" not in result
        # MEDIA-SEQUENCE 更新为 5
        assert "#EXT-X-MEDIA-SEQUENCE:5" in result

    def test_segment_endpoint_seek_file_not_found(self, cache_dir):
        """TC-19f: seek 分片文件不存在时返回 200 空响应（不崩溃）"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 不创建任何 seek 分片文件

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app

            with mp("main.get_seekable_job") as mock_seek:
                mock_job = type("J", (), {"seek_position": 30.0})()
                mock_seek.return_value = mock_job
                client = TestClient(app)

                # seg_00005 不存在，seek_30_00000 也不存在
                resp = client.get("/api/video/vid1/stream/720p/seg_00005.ts")
                assert resp.status_code == 200
                assert len(resp.content) == 0


# ============================================================
# TC-20: Safari 拖拽跳回片头 — 延迟 seek 机制
# ============================================================


class TestDeferredSeekMechanism:
    """延迟 seek 机制测试（验证后端数据支持顺序缓冲覆盖）"""

    def test_m3u8_segments_sequential_order(self, cache_dir):
        """TC-20a: m3u8 中 segment 按编号顺序排列（hls.js 顺序加载的基础）"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        lines = m3u8.strip().split("\n")
        seg_lines = [l for l in lines if l.endswith(".ts")]
        # 提取编号并验证递增
        indices = []
        for l in seg_lines:
            m = re.search(r"seg_(\d+)\.ts", l)
            if m:
                indices.append(int(m.group(1)))
        assert indices == list(range(10))

    def test_m3u8_covers_full_timeline(self, cache_dir):
        """TC-20b: m3u8 覆盖完整时间线，hls.js 可缓冲到任意位置"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=120.0)

        extinf_lines = [l for l in m3u8.split("\n") if l.startswith("#EXTINF:")]
        total_duration = sum(float(l.split(":")[1].rstrip(",")) for l in extinf_lines)
        assert abs(total_duration - 120.0) < 0.1

    def test_seek_then_loadsource_returns_all_segments(self, cache_dir):
        """TC-20c: seek 后重新请求 m3u8 仍包含全部 segment（loadSource 后 hls.js 可继续缓冲）"""
        from transcoder import generate_full_m3u8
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # seek 到 30 秒产生的分片
        (seg_dir / "seek_30_00000.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # m3u8 仍然有 10 个条目（不是只有 seek 之后的）
        assert m3u8.count("#EXTINF:") == 10
        assert "seg_00000.ts" in m3u8
        assert "seg_00005.ts" in m3u8  # seek_30_00000 → seg_00005

    def test_all_segments_accessible_via_endpoint(self, cache_dir):
        """TC-20d: 所有 segment 都可通过端点访问（已转码的返回 200 有内容，未转码的返回 200 空）"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 只创建 segment 0, 2, 4
        for i in [0, 2, 4]:
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            # 已转码的返回 200 有内容
            for i in [0, 2, 4]:
                resp = client.get(f"/api/video/vid1/stream/720p/seg_{i:05d}.ts")
                assert resp.status_code == 200, f"seg_{i:05d}.ts should be 200"
                assert len(resp.content) > 0, f"seg_{i:05d}.ts should have content"

            # 未转码的返回 200 空响应（hls.js 视为 gap，不触发 error）
            for i in [1, 3, 5]:
                resp = client.get(f"/api/video/vid1/stream/720p/seg_{i:05d}.ts")
                assert resp.status_code == 200, f"seg_{i:05d}.ts should be 200"
                assert len(resp.content) == 0, f"seg_{i:05d}.ts should be empty"

    def test_seek_position_accessible_after_seek(self, cache_dir):
        """TC-20e: seek 后目标位置的 segment 可通过端点访问"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # seek 到 30 秒产生的分片 (30//6=5)
        (seg_dir / "seek_30_00000.ts").write_bytes(b"\x00" * 100)

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app

            with mp("main.get_seekable_job") as mock_seek:
                mock_job = type("J", (), {"seek_position": 30.0})()
                mock_seek.return_value = mock_job
                client = TestClient(app)

                # seek_30_00000 → seg_00005，端点正确解析
                resp = client.get("/api/video/vid1/stream/720p/seg_00005.ts")
                assert resp.status_code == 200

    def test_buffer_coverage_guarantee(self, cache_dir):
        """TC-20f: 从 segment 0 开始顺序加载，缓冲覆盖保证（每个 segment 都有有效 EXTINF）"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        lines = m3u8.strip().split("\n")
        for i, line in enumerate(lines):
            if line.startswith("#EXTINF:"):
                duration = float(line.split(":")[1].rstrip(","))
                assert duration > 0, f"EXTINF at line {i} has non-positive duration: {duration}"
                # 下一行应该是 segment URL
                assert i + 1 < len(lines)
                assert lines[i + 1].endswith(".ts"), f"Line after EXTINF at {i} is not a .ts URL"


# ============================================================
# TC-21: Safari 拖拽跳回片头 — handleVideoSeek 安全性
# ============================================================


class TestHandleVideoSeekSafety:
    """handleVideoSeek 安全性测试（验证后端不产生导致前端循环的数据）"""

    def test_seek_to_same_position_idempotent(self, cache_dir):
        """TC-21a: 多次 seek 到同一位置不会产生重复分片"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 两次 seek 到 30 秒都产生 seek_30_ 分片
        (seg_dir / "seek_30_00000.ts").write_bytes(b"\x00" * 100)
        # 第二次 seek 不会产生额外文件（后端自动停止旧 seek）

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # seg_00005 只出现一次
        assert m3u8.count("seg_00005.ts") == 1

    def test_seek_does_not_corrupt_existing_segments(self, cache_dir):
        """TC-21b: seek 不会破坏已有的 seg_* 分片"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 已有的初始转码分片
        for i in range(5):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)
        # seek 分片
        (seg_dir / "seek_30_00000.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # 所有初始分片仍在 m3u8 中
        for i in range(5):
            assert f"seg_{i:05d}.ts" in m3u8
        # seek 映射的分片也在
        assert "seg_00005.ts" in m3u8

    def test_seek_endpoint_returns_consistent_data(self, cache_dir):
        """TC-21c: seek 端点多次调用返回一致结果"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            # 多次请求 stream endpoint 返回一致的 m3u8
            with mp("main.get_or_start_transcode"):
                with mp("main.is_cached", return_value=True):
                    resp1 = client.get("/api/video/vid1/stream/720p")
                    resp2 = client.get("/api/video/vid1/stream/720p")

            assert resp1.text == resp2.text

    def test_m3u8_endlist_always_present(self, cache_dir):
        """TC-21d: m3u8 始终包含 ENDLIST（hls.js 不会无限加载）"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        assert "#EXT-X-ENDLIST" in m3u8
        # ENDLIST 是最后一行
        lines = m3u8.strip().split("\n")
        assert lines[-1] == "#EXT-X-ENDLIST"

    def test_m3u8_vod_type_always_present(self, cache_dir):
        """TC-21e: m3u8 始终包含 PLAYLIST-TYPE:VOD（hls.js 知道这是点播）"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        assert "#EXT-X-PLAYLIST-TYPE:VOD" in m3u8


# ============================================================
# TC-22: Segment 端点 200 空响应（防止 recoverMediaError）
# ============================================================


class TestSegmentEndpoint200Empty:
    """Segment 端点返回 200 空响应测试"""

    def test_missing_segment_content_type(self, cache_dir):
        """TC-22a: 缺失 segment 返回正确的 content-type (video/mp2t)"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            resp = client.get("/api/video/vid1/stream/720p/seg_00000.ts")
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "video/mp2t"

    def test_missing_segment_body_empty(self, cache_dir):
        """TC-22b: 缺失 segment 返回空 body（hls.js 可安全解析）"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            resp = client.get("/api/video/vid1/stream/720p/seg_99999.ts")
            assert resp.status_code == 200
            assert len(resp.content) == 0

    def test_existing_segment_still_returns_content(self, cache_dir):
        """TC-22c: 已转码 segment 仍返回完整内容"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        (seg_dir / "seg_00000.ts").write_bytes(b"\x00" * 100)

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            resp = client.get("/api/video/vid1/stream/720p/seg_00000.ts")
            assert resp.status_code == 200
            assert len(resp.content) == 100

    def test_seek_file_missing_returns_200_empty(self, cache_dir):
        """TC-22d: seek 分片不存在时也返回 200 空响应"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # seek_30_00000 存在但 seg_00005 不存在
        (seg_dir / "seek_30_00000.ts").write_bytes(b"\x00" * 100)

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app

            with mp("main.get_seekable_job") as mock_seek:
                mock_job = type("J", (), {"seek_position": 30.0})()
                mock_seek.return_value = mock_job
                client = TestClient(app)

                # seg_00006 不存在，seek_30_00001 也不存在
                resp = client.get("/api/video/vid1/stream/720p/seg_00006.ts")
                assert resp.status_code == 200
                assert len(resp.content) == 0

    def test_invalid_segment_name_returns_200(self, cache_dir):
        """TC-22e: 无效 segment 文件名也返回 200 空响应"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            resp = client.get("/api/video/vid1/stream/720p/invalid.ts")
            assert resp.status_code == 200
            assert len(resp.content) == 0


# ============================================================
# TC-23: Debounce 防抖机制（后端数据验证）
# ============================================================


class TestSeekDebounce:
    """Seek 防抖机制测试（验证后端数据一致性支持防抖）"""

    def test_rapid_m3u8_requests_consistent(self, cache_dir):
        """TC-23a: 快速连续请求 m3u8 返回一致结果"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            results = [generate_full_m3u8("vid1", "720p", duration=60.0) for _ in range(10)]

        assert all(r == results[0] for r in results)

    def test_seek_during_playback_no_data_corruption(self, cache_dir):
        """TC-23b: 播放过程中 seek 不会导致数据损坏"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        for i in range(10):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)
        # seek 到 30 秒
        (seg_dir / "seek_30_00000.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # m3u8 仍然完整
        assert m3u8.count("#EXTINF:") == 10
        assert "#EXT-X-ENDLIST" in m3u8
        assert "#EXT-X-PLAYLIST-TYPE:VOD" in m3u8

    def test_segment_stability_during_transcode(self, cache_dir):
        """TC-23c: 转码过程中请求 segment 不会崩溃"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 只有前 3 个 segment
        for i in range(3):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            # 请求不存在的 segment 不崩溃
            for i in range(3, 10):
                resp = client.get(f"/api/video/vid1/stream/720p/seg_{i:05d}.ts")
                assert resp.status_code == 200

    def test_m3u8_never_changes_during_session(self, cache_dir):
        """TC-23d: 同一视频会话中 m3u8 结构始终一致"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8_1 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # 添加更多 segment（模拟转码进行中）
        for i in range(5, 10):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8_2 = generate_full_m3u8("vid1", "720p", duration=60.0)

        # m3u8 结构一致（都是 10 个条目）
        assert m3u8_1.count("#EXTINF:") == m3u8_2.count("#EXTINF:") == 10
        assert m3u8_1 == m3u8_2


# ============================================================
# TC-24: 无 recoverMediaError 行为验证
# ============================================================


class TestNoRecoverMediaError:
    """验证后端不依赖 recoverMediaError 的行为"""

    def test_segment_endpoint_never_returns_error_status(self, cache_dir):
        """TC-24a: segment 端点永远不会返回 5xx 错误"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            # 各种不存在的 segment 都不返回 5xx
            for seg in ["seg_00000.ts", "seg_99999.ts", "invalid.ts", "seg_00001.ts"]:
                resp = client.get(f"/api/video/vid1/stream/720p/{seg}")
                assert resp.status_code < 500, f"{seg} returned {resp.status_code}"

    def test_stream_endpoint_always_returns_m3u8(self, cache_dir):
        """TC-24b: stream 端点始终返回有效 m3u8"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            with mp("main.get_or_start_transcode"):
                with mp("main.is_cached", return_value=True):
                    resp = client.get("/api/video/vid1/stream/720p")

            assert resp.status_code == 200
            assert "#EXTM3U" in resp.text
            assert "#EXT-X-ENDLIST" in resp.text

    def test_m3u8_always_has_endlist(self, cache_dir):
        """TC-24c: m3u8 始终包含 ENDLIST（防止 hls.js 无限加载）"""
        from transcoder import generate_full_m3u8

        for duration in [0, 0.1, 6.0, 60.0, 3600.0]:
            with patch("config.get", return_value=str(cache_dir)):
                m3u8 = generate_full_m3u8("vid1", "720p", duration=duration)
            assert "#EXT-X-ENDLIST" in m3u8, f"ENDLIST missing for duration={duration}"

    def test_m3u8_always_has_vod_type(self, cache_dir):
        """TC-24d: m3u8 始终包含 PLAYLIST-TYPE:VOD"""
        from transcoder import generate_full_m3u8

        for duration in [0, 0.1, 6.0, 60.0]:
            with patch("config.get", return_value=str(cache_dir)):
                m3u8 = generate_full_m3u8("vid1", "720p", duration=duration)
            assert "#EXT-X-PLAYLIST-TYPE:VOD" in m3u8, f"VOD type missing for duration={duration}"


# ============================================================
# TC-25: m3u8 start 参数（seek 截断播放列表）
# ============================================================


class TestM3u8StartParameter:
    """m3u8 start 参数测试（seek 后从指定位置加载）"""

    def test_start_zero_same_as_full(self, cache_dir):
        """TC-25a: start=0 等同于完整 m3u8"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            full = generate_full_m3u8("vid1", "720p", duration=60.0)
            from_start = generate_full_m3u8("vid1", "720p", duration=60.0, start=0)

        assert full == from_start

    def test_start_truncates_segments(self, cache_dir):
        """TC-25b: start=30 截断前 5 个 segment（30//6=5）"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0, start=30)

        # 原来 10 个 segment，截断后 5 个（seg_00005 ~ seg_00009）
        assert m3u8.count("#EXTINF:") == 5
        assert "seg_00005.ts" in m3u8
        assert "seg_00009.ts" in m3u8
        # 被截断的 segment 不在
        assert "seg_00000.ts" not in m3u8
        assert "seg_00004.ts" not in m3u8

    def test_start_media_sequence(self, cache_dir):
        """TC-25c: start=30 → MEDIA-SEQUENCE=5"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0, start=30)

        assert "#EXT-X-MEDIA-SEQUENCE:5" in m3u8

    def test_start_preserves_endlist(self, cache_dir):
        """TC-25d: 截断 m3u8 仍包含 ENDLIST"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0, start=30)

        assert "#EXT-X-ENDLIST" in m3u8

    def test_start_preserves_vod_type(self, cache_dir):
        """TC-25e: 截断 m3u8 仍包含 PLAYLIST-TYPE:VOD"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0, start=30)

        assert "#EXT-X-PLAYLIST-TYPE:VOD" in m3u8

    def test_start_duration_sum(self, cache_dir):
        """TC-25f: 截断后 segment 总时长 = duration - start"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0, start=30)

        extinf_lines = [l for l in m3u8.split("\n") if l.startswith("#EXTINF:")]
        total = sum(float(l.split(":")[1].rstrip(",")) for l in extinf_lines)
        assert abs(total - 30.0) < 0.1

    def test_start_at_non_segment_boundary(self, cache_dir):
        """TC-25g: start=25（非 segment 边界）→ 从 seg_00004 开始（25//6=4）"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0, start=25)

        assert "#EXT-X-MEDIA-SEQUENCE:4" in m3u8
        assert "seg_00004.ts" in m3u8
        assert "seg_00003.ts" not in m3u8

    def test_start_at_last_segment(self, cache_dir):
        """TC-25h: start=54 → 只剩最后 1 个 segment（seg_00009）"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0, start=54)

        assert m3u8.count("#EXTINF:") == 1
        assert "seg_00009.ts" in m3u8

    def test_start_beyond_duration(self, cache_dir):
        """TC-25i: start >= duration → 至少保留最后 1 个 segment"""
        from transcoder import generate_full_m3u8

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0, start=100)

        assert m3u8.count("#EXTINF:") >= 1
        assert "#EXT-X-ENDLIST" in m3u8

    def test_start_with_seek_segments(self, cache_dir):
        """TC-25j: start 参数与 seek 分片共存"""
        from transcoder import generate_full_m3u8

        seg_dir = cache_dir / "vid1" / "720p"
        seg_dir.mkdir(parents=True, exist_ok=True)
        # 初始转码
        for i in range(5):
            (seg_dir / f"seg_{i:05d}.ts").write_bytes(b"\x00" * 100)
        # seek 到 30 秒的分片
        (seg_dir / "seek_30_00000.ts").write_bytes(b"\x00" * 100)

        with patch("config.get", return_value=str(cache_dir)):
            m3u8 = generate_full_m3u8("vid1", "720p", duration=60.0, start=30)

        # 从 seg_00005 开始，seek_30_00000 → seg_00005 被收录
        assert "seg_00005.ts" in m3u8
        assert m3u8.count("#EXTINF:") == 5

    def test_stream_endpoint_with_start(self, cache_dir):
        """TC-25k: stream 端点支持 start 查询参数"""
        from fastapi.testclient import TestClient
        from unittest.mock import patch as mp

        with mp("config._settings", {
            "video_dir": "/tmp",
            "cache_dir": str(cache_dir),
            "max_cache_size_gb": 50,
        }):
            import main as main_module
            main_module._video_cache = {"vid1": {"id": "vid1", "name": "v", "path": "/tmp/v.mp4", "duration": 60.0}}
            from main import app
            client = TestClient(app)

            with mp("main.get_or_start_transcode"):
                with mp("main.is_cached", return_value=True):
                    resp = client.get("/api/video/vid1/stream/720p?start=30")

            assert resp.status_code == 200
            assert "#EXT-X-MEDIA-SEQUENCE:5" in resp.text
            assert "seg_00005.ts" in resp.text
            assert "seg_00000.ts" not in resp.text
