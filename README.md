# Video Streamer

A lightweight self-hosted video streaming server with real-time transcoding and HLS playback. Designed for remotely accessing large local video files (1080p/4K) over the network — hardware-accelerated transcoding compresses videos to stream-friendly bitrates for smooth remote playback.

一个轻量级的自托管视频流媒体服务器，支持实时转码和 HLS 流式播放。专为远程访问本地大体积视频资源（1080p/4K）而设计，通过硬件加速转码将视频压缩为适合网络传输的码率，实现流畅的远程播放体验。

## Features / 功能特性

- **Real-time transcoding** — play while transcoding, no need to wait for full encode
- **实时转码播放** — 边转码边播放，无需等待完整转码

- **Hardware acceleration** — Apple VideoToolbox on macOS, VA-API on Linux
- **硬件加速** — macOS 使用 VideoToolbox，Linux 使用 VA-API

- **HLS adaptive streaming** — switch between 1080p / 720p / 480p on the fly
- **HLS 自适应流媒体** — 支持多码率实时切换（1080p / 720p / 480p）

- **Smart caching** — transcoded segments cached to disk, instant replay
- **智能缓存** — 转码结果自动缓存到磁盘，后续播放秒开

- **Parallel transcoding** — up to 3 videos transcoded simultaneously via worker pool
- **并行转码** — worker 池支持最多 3 个视频同时转码

- **Sprite preview** — hover/touch progress bar to see video preview thumbnails
- **进度条预览** — 鼠标悬停或触摸进度条可预览对应时间点的画面

- **Multi-directory** — add multiple video folders from the web UI
- **多目录支持** — 可在 Web 页面添加多个视频文件目录

- **Auto pre-transcode** — uncached videos are queued for background encoding
- **自动预转码** — 未缓存的视频自动排队后台转码

- **Cross-browser** — Safari / Chrome / Firefox, iOS custom controls
- **跨浏览器兼容** — Safari / Chrome / Firefox，iOS 自定义控件

- **Dark theme responsive UI**
- **深色主题响应式 UI**

## Project Structure / 项目结构

```
player/
├── main.py              # FastAPI entry point & API routes
├── config.py            # Configuration management (dynamic + persistent)
├── transcoder.py        # FFmpeg transcoding core + worker pool + m3u8 generation
├── video_scanner.py     # Video file scanning & metadata extraction
├── cache_manager.py     # Cache management, batch caching, pre-transcode
├── test_player.py       # Test suite (200+ cases)
├── requirements.txt     # Python dependencies
├── settings.json        # Runtime config (auto-generated)
├── LICENSE              # MIT License
├── static/
│   ├── home.html        # Landing page
│   ├── library.html     # Video library + player
│   ├── library.js       # Frontend logic + HLS player + i18n
│   ├── player.js        # Player components
│   ├── style.css        # Styles
│   ├── hls.min.js       # HLS.js library
│   └── favicon.svg      # Favicon
└── cache/               # Default transcoded segment cache
```

## Requirements / 环境要求

- Python 3.10+
- FFmpeg with hardware acceleration support
  - macOS: VideoToolbox (included with FFmpeg via Homebrew)
  - Linux: VA-API (`ffmpeg -hwaccel vaapi`)

### Install FFmpeg / 安装 FFmpeg

```bash
# macOS (Homebrew)
brew install ffmpeg

# Ubuntu / Debian
sudo apt install ffmpeg

# Verify VideoToolbox support (macOS)
ffmpeg -encoders 2>&1 | grep videotoolbox
```

## Installation / 安装与启动

```bash
# Clone / 克隆
git clone https://github.com/anthropics/video-streamer.git
cd video-streamer

# Virtual environment / 虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# Dependencies / 安装依赖
pip install -r requirements.txt

# Run / 启动
python main.py
```

Open `http://localhost:8000` in your browser.  
浏览器访问 `http://localhost:8000`。

### Environment Variables / 环境变量

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Listen address |
| `PORT` | `8000` | Listen port |

```bash
PORT=9000 python main.py
```

### Web Settings / 页面配置

After startup, click the gear icon in the top-right corner to configure:

启动后点击右上角齿轮图标可配置：

- **Video directories** / 视频目录 — paths to your video files (multiple supported)
- **Cache directory** / 缓存目录 — where HLS segments are stored
- **Max cache size** / 最大缓存大小 — auto-evicts oldest cache when exceeded
- **Concurrent transcodes** / 转码并发数 — number of parallel transcode workers (1-3)

Settings are saved to `settings.json` and persist across restarts.  
配置保存在 `settings.json`，重启后自动加载。

## Usage / 使用方法

1. Start the server and open the landing page to find your access URL  
   启动服务后，访问首页查看访问链接

2. Click "Video Library" to browse your videos  
   点击进入视频库浏览视频

3. Click a video card to play — first play starts real-time transcoding (~3-5s buffer)  
   点击视频卡片开始播放，首次播放会启动实时转码（约 3-5 秒缓冲）

4. Once transcoded, the video is cached for instant replay  
   转码完成后自动缓存，后续播放秒开

5. Use the quality dropdown to switch between 1080p / 720p / 480p  
   通过画质下拉菜单切换 1080p / 720p / 480p

## Remote Access / 远程访问

Use Tailscale, WireGuard, or any VPN to access from outside your LAN:

使用 Tailscale、WireGuard 等内网穿透工具远程访问：

```
http://<your-tailscale-ip>:8000
```

## API Reference / API 接口

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/videos` | List all videos / 获取视频列表 |
| `GET` | `/api/video/{id}/info` | Video details / 视频详情 |
| `GET` | `/api/video/{id}/stream/{quality}` | HLS playlist (dynamic m3u8) / HLS 播放列表 |
| `GET` | `/api/video/{id}/stream/{quality}/{segment}` | TS segment / TS 分片 |
| `POST` | `/api/video/{id}/seek/{quality}` | Start seek transcode / 断点转码 |
| `GET` | `/api/video/{id}/thumbnail` | Video thumbnail / 缩略图 |
| `GET` | `/api/video/{id}/sprite` | Sprite sheet for preview / 预览雪碧图 |
| `GET` | `/api/video/{id}/cache-status` | Cached qualities / 已缓存画质 |
| `POST` | `/api/video/{id}/cache/clear` | Clear video cache / 清除缓存 |
| `GET` | `/api/settings` | Get settings / 获取配置 |
| `PUT` | `/api/settings` | Update settings / 更新配置 |
| `GET` | `/api/cache/status` | Cache status / 缓存状态 |
| `GET` | `/api/disk/status` | Disk status / 磁盘状态 |
| `POST` | `/api/cache/init` | Start batch cache / 启动批量缓存 |
| `POST` | `/api/cache/stop` | Stop batch cache / 停止批量缓存 |
| `GET` | `/api/cache/batch` | Batch progress / 批量缓存进度 |
| `GET` | `/api/cache/active-progress` | Active transcode progress / 活跃转码进度 |

## Testing / 测试

```bash
# Run all tests / 运行全部测试
.venv/bin/python -m pytest test_player.py -v

# Run specific group / 运行指定测试组
.venv/bin/python -m pytest test_player.py -v -k "TestSegmentEndpoint"
```

Coverage includes: m3u8 generation, segment endpoints, seek flow, cache management, Safari playback, i18n, fullscreen controls.

覆盖：m3u8 生成、分片端点、跳转流程、缓存管理、Safari 播放、国际化、全屏控制。

## Supported Formats / 支持的视频格式

MP4, MKV, AVI, MOV, WMV, FLV, WebM, M4V, TS, RMVB

## Quality Profiles / 转码质量档位

| Profile | Resolution | Video Bitrate | Audio Bitrate |
|---------|------------|---------------|---------------|
| 1080p | 1920x1080 | 5000 kbps | 192 kbps |
| 720p | 1280x720 | 2500 kbps | 128 kbps |
| 480p | 854x480 | 1000 kbps | 128 kbps |

- 4K source: recommended 1080p, optional 720p / 480p  
  4K 源视频：推荐 1080p，可选 720p / 480p
- 1080p source: recommended 720p, optional 480p  
  1080p 源视频：推荐 720p，可选 480p

## License / 许可证

[MIT](LICENSE)
