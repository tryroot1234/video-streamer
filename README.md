# Video Streamer

一个轻量级的自托管视频流媒体服务器，支持实时转码和 HLS 流式播放。专为远程访问本地大体积视频资源（1080P / 4K）而设计，通过硬件加速转码将视频压缩为适合网络传输的码率，实现流畅的远程播放体验。

## 功能特性

- 实时转码播放，边转码边播放，无需等待完整转码
- Apple VideoToolbox 硬件加速（适用于 macOS / Apple Silicon）
- HLS 自适应流媒体，支持多码率切换（1080p / 720p / 480p）
- 转码结果自动缓存到磁盘，下次播放直接复用
- Web 页面可动态配置视频目录、缓存目录
- 自动扫描视频文件并提取元数据（分辨率、时长、编码等）
- 自动提取视频缩略图
- Safari / Chrome / Firefox 兼容
- 视频排序（按名称、大小）
- 分页浏览（默认每页 20 个）
- 首页显示访问链接，一键复制
- 深色主题响应式 UI

## 项目结构

```
player/
├── main.py              # FastAPI 应用入口，API 路由
├── config.py            # 配置管理（动态设置 + 持久化）
├── transcoder.py        # FFmpeg 转码核心逻辑 + m3u8 动态生成
├── video_scanner.py     # 视频文件扫描与元数据提取
├── cache_manager.py     # 转码缓存管理、批量缓存、清理
├── test_player.py       # 测试用例（100 个）
├── requirements.txt     # Python 依赖
├── settings.json        # 运行时配置（自动生成）
├── static/
│   ├── home.html        # 首页（访问链接 + 快捷入口）
│   ├── library.html     # 视频库页面（排序 + 分页 + 播放器）
│   ├── library.js       # 视频库前端逻辑 + HLS 播放器
│   └── style.css        # 样式
└── cache/               # 默认转码缓存目录（可在页面修改）
```

## 环境要求

- Python 3.10+
- FFmpeg（需支持 VideoToolbox 硬件加速）
- macOS（推荐 Apple Silicon）或其他支持 FFmpeg 的系统

### 安装 FFmpeg

```bash
# macOS (Homebrew)
brew install ffmpeg

# Ubuntu / Debian
sudo apt install ffmpeg

# 验证 VideoToolbox 支持 (macOS)
ffmpeg -encoders 2>&1 | grep videotoolbox
```

## 安装与启动

```bash
# 克隆项目
git clone https://github.com/your-username/video-streamer.git
cd video-streamer

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt

# 启动服务
python main.py
```

启动后浏览器访问 `http://localhost:8000`。

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `VIDEO_DIR` | `~/Videos` | 视频文件目录 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |

```bash
# 示例：指定视频目录启动
VIDEO_DIR=/path/to/videos python main.py

# 示例：指定端口
PORT=9000 python main.py
```

### 页面配置

启动后也可以在 Web 页面右上角齿轮图标中动态修改：

- **视频目录**：视频文件所在路径
- **缓存目录**：转码后 HLS 分片的存储路径
- **最大缓存大小**：超出后自动清理最旧的缓存

配置保存在 `settings.json`，重启后自动加载。

## 页面说明

### 首页 (`/`)

- 显示当前服务访问链接
- 一键复制链接（方便分享给远程设备）
- 快捷按钮进入视频库

### 视频库 (`/library`)

- 视频缩略图网格浏览
- 排序：按名称 A-Z / Z-A、按大小升序 / 降序
- 分页：默认每页 20 个，支持翻页
- 点击视频卡片进入播放器
- 播放器支持画质切换（1080p / 720p / 480p）
- 右上角齿轮图标可修改视频目录、缓存目录等配置

## 使用方法

1. 启动服务后，访问首页查看访问链接
2. 点击 "Enter Video Library" 进入视频库
3. 页面自动扫描视频目录并展示
4. 点击视频卡片开始播放
5. 首次播放会启动实时转码（约 3-5 秒缓冲）
6. 转码完成后自动缓存，后续播放秒开
7. 通过页面下拉菜单切换画质

## 远程访问

通过 Tailscale 等内网穿透工具访问：

```
http://<your-tailscale-ip>:8000
```

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/videos` | 获取视频列表 |
| `GET` | `/api/video/{id}/info` | 获取视频详情 |
| `GET` | `/api/video/{id}/stream/{quality}` | 获取 HLS 播放列表（动态生成完整 m3u8） |
| `GET` | `/api/video/{id}/stream/{quality}/{segment}` | 获取 TS 分片（缺失返回 200 空响应） |
| `POST` | `/api/video/{id}/seek/{quality}` | 从指定位置启动断点转码 |
| `GET` | `/api/video/{id}/stream/{quality}/segments-ready` | 检查 seek 转码分片是否就绪 |
| `GET` | `/api/video/{id}/thumbnail` | 获取视频缩略图 |
| `GET` | `/api/video/{id}/cache-status` | 获取视频已缓存画质 |
| `POST` | `/api/video/{id}/cache/clear` | 清除视频缓存 |
| `GET` | `/api/settings` | 获取当前配置 |
| `PUT` | `/api/settings` | 更新配置 |
| `GET` | `/api/cache/status` | 查看缓存状态 |
| `GET` | `/api/disk/status` | 查看磁盘状态 |
| `POST` | `/api/cache/init` | 启动批量缓存 |
| `POST` | `/api/cache/stop` | 停止批量缓存 |
| `GET` | `/api/cache/batch` | 查看批量缓存进度 |
| `GET` | `/api/cache/active-progress` | 查看所有活跃转码进度 |

## 测试

```bash
# 运行全部测试（100 个用例）
venv/bin/python -m pytest test_player.py -v

# 运行指定测试组
venv/bin/python -m pytest test_player.py -v -k "TestSegmentEndpoint"
```

测试覆盖：
- m3u8 动态生成（时长、分片、边界条件）
- segment 端点行为（已有分片、缺失分片、seek 映射）
- stream 端点统一流程
- seek 断点转码流程
- 缓存清除与重建
- Safari 播放稳定性（FRAG_BUFFERED 延迟 seek、防抖、200 空响应）

## 支持的视频格式

MP4, MKV, AVI, MOV, WMV, FLV, WebM, M4V, TS, RMVB

## 转码质量档位

| 档位 | 分辨率 | 视频码率 | 音频码率 |
|------|--------|----------|----------|
| 1080p | 1920x1080 | 5000 kbps | 192 kbps |
| 720p | 1280x720 | 2500 kbps | 128 kbps |
| 480p | 854x480 | 1000 kbps | 128 kbps |

- 4K 源视频：推荐 1080p，可选 720p / 480p
- 1080p 源视频：推荐 720p，可选 480p

## 许可证

MIT
