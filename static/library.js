/**
 * 播放器控件 — 周易之道
 *
 * 简易：一个入口（_seekTo）处理所有跳转，一个 overlay（showLoading）管理所有加载状态
 * 变易：状态驱动 UI，事件通过 _playerListeners 统一注册和清理
 * 不易：用户意图不变 — 点击进度条 → 跳转 → 播放
 *
 * 第一性原理：从「用户想看视频某个位置」出发推导代码结构
 */

let hls = null;
let currentVideo = null;
let currentQuality = null;
let _loadedMetadataHandler = null;

let allVideos = [];
let currentPage = 1;
let perPage = 12;
let currentSort = "time-desc";

let batchPollTimer = null;
let activePollTimer = null;
let pendingPlayVideo = null;

// ---------- Settings ----------

async function loadSettings() {
    try {
        const res = await fetch("/api/settings");
        const s = await res.json();
        renderVideoDirs(s.video_dirs || []);
        document.getElementById("input-cache-dir").value = s.cache_dir || "";
        document.getElementById("input-max-cache").value = s.max_cache_size_gb || 50;
    } catch (e) {
        console.error("Failed to load settings", e);
    }
}

function renderVideoDirs(dirs) {
    const container = document.getElementById("video-dirs-list");
    container.innerHTML = "";
    dirs.forEach((dir, i) => {
        const row = document.createElement("div");
        row.className = "dir-row";
        row.innerHTML = `<input type="text" class="dir-input" value="${dir}"><button class="dir-remove-btn" onclick="this.parentElement.remove()">✕</button>`;
        container.appendChild(row);
    });
}

function addVideoDir() {
    const container = document.getElementById("video-dirs-list");
    const row = document.createElement("div");
    row.className = "dir-row";
    row.innerHTML = `<input type="text" class="dir-input" value="" placeholder="/path/to/videos"><button class="dir-remove-btn" onclick="this.parentElement.remove()">✕</button>`;
    container.appendChild(row);
    row.querySelector("input").focus();
}

async function saveSettings() {
    const btn = document.getElementById("save-btn");
    const msg = document.getElementById("settings-msg");
    btn.disabled = true;
    msg.classList.add("hidden");

    const dirInputs = document.querySelectorAll("#video-dirs-list .dir-input");
    const video_dirs = Array.from(dirInputs).map(el => el.value.trim()).filter(Boolean);

    const body = {
        video_dirs,
        cache_dir: document.getElementById("input-cache-dir").value.trim(),
        max_cache_size_gb: parseInt(document.getElementById("input-max-cache").value, 10),
    };

    try {
        const res = await fetch("/api/settings", {
            method: "PUT",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body),
        });
        if (!res.ok) throw new Error(await res.text());
        msg.textContent = "已保存，正在刷新视频列表...";
        msg.className = "success";
        msg.classList.remove("hidden");
        loadVideos();
    } catch (e) {
        msg.textContent = "保存失败: " + e.message;
        msg.className = "error";
        msg.classList.remove("hidden");
    } finally {
        btn.disabled = false;
    }
}

function toggleSettings() {
    const panel = document.getElementById("settings-panel");
    const overlay = document.getElementById("settings-overlay");
    const isHidden = panel.classList.contains("hidden");
    if (isHidden) {
        loadSettings();
        panel.classList.remove("hidden");
        overlay.classList.remove("hidden");
    } else {
        panel.classList.add("hidden");
        overlay.classList.add("hidden");
    }
}

// ---------- Videos ----------

async function loadVideos() {
    try {
        const res = await fetch("/api/videos");
        const data = await res.json();
        allVideos = data.videos;
        applySortAndRender();
    } catch (e) {
        document.getElementById("loading").textContent = "加载视频失败";
    }
}

function sortVideos(videos, sortKey) {
    const sorted = [...videos];
    switch (sortKey) {
        case "name-asc":
            sorted.sort((a, b) => a.name.localeCompare(b.name));
            break;
        case "name-desc":
            sorted.sort((a, b) => b.name.localeCompare(a.name));
            break;
        case "time-asc":
            sorted.sort((a, b) => a.name.localeCompare(b.name));
            break;
        case "time-desc":
            sorted.sort((a, b) => b.name.localeCompare(a.name));
            break;
        case "size-desc":
            sorted.sort((a, b) => b.size - a.size);
            break;
        case "size-asc":
            sorted.sort((a, b) => a.size - b.size);
            break;
    }
    return sorted;
}

function applySortAndRender() {
    const sorted = sortVideos(allVideos, currentSort);
    const totalPages = Math.ceil(sorted.length / perPage) || 1;
    if (currentPage > totalPages) currentPage = totalPages;

    const start = (currentPage - 1) * perPage;
    const pageVideos = sorted.slice(start, start + perPage);

    renderLibrary(pageVideos);
    renderPagination(totalPages, sorted.length);
}

function changeSort(value) {
    currentSort = value;
    currentPage = 1;
    applySortAndRender();
}

function changePerPage(value) {
    perPage = parseInt(value, 10);
    currentPage = 1;
    applySortAndRender();
}

function goToPage(page) {
    currentPage = page;
    applySortAndRender();
    window.scrollTo({top: 0, behavior: "smooth"});
}

function formatDuration(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = Math.floor(seconds % 60);
    if (h > 0) return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
    return `${m}:${s.toString().padStart(2, "0")}`;
}

function formatSize(bytes) {
    if (bytes >= 1073741824) return (bytes / 1073741824).toFixed(1) + " GB";
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(0) + " MB";
    return (bytes / 1024).toFixed(0) + " KB";
}

function renderLibrary(videos) {
    const grid = document.getElementById("video-grid");
    const loading = document.getElementById("loading");

    if (allVideos.length === 0) {
        loading.textContent = "未找到视频，请点击右上角齿轮图标设置视频目录";
        loading.classList.remove("hidden");
        grid.innerHTML = "";
        return;
    }

    loading.classList.add("hidden");
    grid.innerHTML = "";

    videos.forEach(v => {
        const card = document.createElement("div");
        card.className = "video-card";
        card.onclick = () => playVideo(v);

        const resLabel = v.height >= 2160 ? "4K" : v.height >= 1080 ? "1080p" : v.height >= 720 ? "720p" : `${v.height}p`;

        card.innerHTML = `
            <div class="thumb">
                <img src="/api/video/${encodeURIComponent(v.id)}/thumbnail" onerror="this.style.display='none'" loading="lazy">
                <span class="badge">${resLabel}</span>
                <div class="cache-progress"><div class="cache-progress-bar" id="pgb-${v.id}"></div></div>
                <span class="cache-pct" id="pgp-${v.id}"></span>
                <button class="cache-pause-btn hidden" id="pgk-${v.id}" onclick="event.stopPropagation(); togglePauseCache('${v.id}')">
                    <svg class="pause-icon" viewBox="0 0 24 24"><rect x="6" y="4" width="4" height="16" fill="currentColor"/><rect x="14" y="4" width="4" height="16" fill="currentColor"/></svg>
                    <svg class="resume-icon hidden" viewBox="0 0 24 24"><polygon points="6,4 20,12 6,20" fill="currentColor"/></svg>
                </button>
            </div>
            <div class="info">
                <h3 title="${v.name}">${v.name}</h3>
                <div class="meta">
                    <span>${formatDuration(v.duration)}</span>
                    <span>${formatSize(v.size)}</span>
                    <span>${v.codec}</span>
                </div>
            </div>
        `;
        grid.appendChild(card);
    });
}

function renderPagination(totalPages, totalItems) {
    const container = document.getElementById("pagination");
    const pageInfo = document.getElementById("page-info");

    if (totalPages <= 1) {
        container.innerHTML = "";
        pageInfo.textContent = `共 ${totalItems} 个视频`;
        return;
    }

    pageInfo.textContent = `共 ${totalItems} 个视频 · 第 ${currentPage}/${totalPages} 页`;

    let html = "";
    html += `<button ${currentPage === 1 ? "disabled" : ""} onclick="goToPage(${currentPage - 1})">&laquo;</button>`;

    const range = 2;
    let start = Math.max(1, currentPage - range);
    let end = Math.min(totalPages, currentPage + range);

    if (start > 1) {
        html += `<button onclick="goToPage(1)">1</button>`;
        if (start > 2) html += `<span class="dots">...</span>`;
    }

    for (let i = start; i <= end; i++) {
        html += `<button class="${i === currentPage ? "active" : ""}" onclick="goToPage(${i})">${i}</button>`;
    }

    if (end < totalPages) {
        if (end < totalPages - 1) html += `<span class="dots">...</span>`;
        html += `<button onclick="goToPage(${totalPages})">${totalPages}</button>`;
    }

    html += `<button ${currentPage === totalPages ? "disabled" : ""} onclick="goToPage(${currentPage + 1})">&raquo;</button>`;

    container.innerHTML = html;
}

// ---------- Batch Cache ----------

async function startBatchCache() {
    try {
        // 按当前排序顺序发送视频 ID
        const sorted = sortVideos(allVideos, currentSort);
        const videoIds = sorted.map(v => v.id);

        const res = await fetch("/api/cache/init", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({video_ids: videoIds}),
        });
        const data = await res.json();
        if (!data.ok) {
            alert(data.msg);
            return;
        }
        document.getElementById("cache-init-btn").classList.add("hidden");
        document.getElementById("cache-stop-btn").classList.remove("hidden");
        document.getElementById("batch-progress").classList.remove("hidden");
        pollBatchProgress();
    } catch (e) {
        alert("启动批量缓存失败");
    }
}

async function stopBatchCache() {
    await fetch("/api/cache/stop", {method: "POST"});
}

async function clearAllCache() {
    if (!confirm("确定清除所有视频缓存？此操作不可撤销。")) return;
    const btn = document.getElementById("cache-clear-all-btn");
    btn.disabled = true;
    btn.textContent = "清除中...";
    try {
        const res = await fetch("/api/cache/clear-all", {method: "POST"});
        const data = await res.json();
        if (data.ok) {
            btn.textContent = `已清除 ${formatSize(data.freed)}`;
            setTimeout(() => { btn.textContent = "一键清除全部"; btn.disabled = false; }, 3000);
        } else {
            btn.textContent = "清除失败";
            setTimeout(() => { btn.textContent = "一键清除全部"; btn.disabled = false; }, 2000);
        }
    } catch (e) {
        btn.textContent = "清除失败";
        setTimeout(() => { btn.textContent = "一键清除全部"; btn.disabled = false; }, 2000);
    }
}

function pollBatchProgress() {
    if (batchPollTimer) clearInterval(batchPollTimer);
    batchPollTimer = setInterval(async () => {
        try {
            const res = await fetch("/api/cache/batch");
            const s = await res.json();

            const statusEl = document.getElementById("batch-status");
            const currentEl = document.getElementById("batch-current");
            const fillEl = document.getElementById("progress-fill");

            if (s.total > 0) {
                const pct = Math.round(s.done / s.total * 100);
                fillEl.style.width = pct + "%";
                statusEl.textContent = `${s.done} / ${s.total} (${pct}%)`;
            }

            if (s.running) {
                currentEl.textContent = s.current ? `正在缓存: ${s.current}` : "";
            } else {
                clearInterval(batchPollTimer);
                batchPollTimer = null;
                document.getElementById("cache-init-btn").classList.remove("hidden");
                document.getElementById("cache-stop-btn").classList.add("hidden");

                if (s.stopped_reason) {
                    currentEl.textContent = s.stopped_reason;
                }

                checkDiskStatus();
            }
        } catch (e) {
            console.error("Poll batch progress error", e);
        }
    }, 1000);
}

// ---------- Active Progress (per-card) ----------

function pollActiveProgress() {
    if (activePollTimer) return;
    activePollTimer = setInterval(async () => {
        try {
            const res = await fetch("/api/cache/active-progress");
            const data = await res.json();
            const progress = data.video_progress || {};
            const activeIds = new Set();

            for (const [vid, prog] of Object.entries(progress)) {
                activeIds.add(vid);
                const bar = document.getElementById(`pgb-${vid}`);
                const pctEl = document.getElementById(`pgp-${vid}`);
                const btn = document.getElementById(`pgk-${vid}`);
                if (!bar || !pctEl) continue;

                bar.style.width = prog.percent + "%";

                if (prog.status === "caching") {
                    pctEl.textContent = prog.percent + "%";
                    pctEl.style.display = "block";
                    bar.className = "cache-progress-bar";
                    if (btn) { btn.classList.remove("hidden"); showPauseIcon(btn); }
                } else if (prog.status === "paused") {
                    pctEl.textContent = "已暂停 " + prog.percent + "%";
                    pctEl.style.display = "block";
                    bar.className = "cache-progress-bar paused";
                    if (btn) { btn.classList.remove("hidden"); showResumeIcon(btn); }
                } else if (prog.status === "done") {
                    pctEl.textContent = "已缓存";
                    pctEl.style.display = "block";
                    bar.className = "cache-progress-bar done";
                    if (btn) btn.classList.add("hidden");
                } else if (prog.status === "error") {
                    pctEl.textContent = "失败";
                    pctEl.style.display = "block";
                    bar.className = "cache-progress-bar error";
                    if (btn) btn.classList.add("hidden");
                }
            }

            // Hide pause button for videos no longer active
            document.querySelectorAll(".cache-pause-btn:not(.hidden)").forEach(btn => {
                const vid = btn.id.replace("pgk-", "");
                if (!activeIds.has(vid)) btn.classList.add("hidden");
            });

            // 预转码状态
            try {
                const ptRes = await fetch("/api/pretranscode/status");
                const pt = await ptRes.json();
                const el = document.getElementById("pretranscode-status");
                if (!el) return;
                if (pt.running && pt.total > 0) {
                    el.classList.remove("hidden");
                    if (pt.current) {
                        el.innerHTML = `<div class="mini-spinner"></div> 预转码中: ${pt.current} (${pt.done}/${pt.total})`;
                    } else {
                        el.innerHTML = `<div class="mini-spinner"></div> 预转码准备中 (${pt.done}/${pt.total})`;
                    }
                } else {
                    el.classList.add("hidden");
                }
            } catch (_) {}
        } catch (e) {
            // ignore
        }
    }, 1000);
}

function showPauseIcon(btn) {
    btn.querySelector(".pause-icon").classList.remove("hidden");
    btn.querySelector(".resume-icon").classList.add("hidden");
}

function showResumeIcon(btn) {
    btn.querySelector(".pause-icon").classList.add("hidden");
    btn.querySelector(".resume-icon").classList.remove("hidden");
}

async function togglePauseCache(videoId) {
    const btn = document.getElementById(`pgk-${videoId}`);
    if (!btn) return;
    const isPaused = btn.querySelector(".resume-icon").classList.contains("hidden") === false;
    try {
        if (isPaused) {
            await fetch("/api/cache/resume", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({video_id: videoId}),
            });
        } else {
            await fetch("/api/cache/pause", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({video_id: videoId}),
            });
        }
    } catch (e) {
        console.error("Toggle pause error", e);
    }
}

// ---------- Disk Status ----------

async function checkDiskStatus() {
    try {
        const res = await fetch("/api/disk/status");
        const d = await res.json();
        const warn = document.getElementById("disk-warning");

        if (d.free_percent < 20) {
            warn.textContent = `⚠ 磁盘可用空间不足 20% (剩余 ${d.free_percent}%)，请尽快扩容`;
            warn.classList.remove("hidden");
        } else if (!d.can_cache_more && d.stop_reason) {
            warn.textContent = `⚠ ${d.stop_reason}`;
            warn.classList.remove("hidden");
        } else {
            warn.classList.add("hidden");
        }
    } catch (e) {
        // ignore
    }
}

// ---------- Cache Modal ----------

async function showCacheModal(video) {
    pendingPlayVideo = video;
    const quality = video.recommended_quality;
    const diskRes = await fetch("/api/disk/status");
    const disk = await diskRes.json();

    const msg = document.getElementById("cache-modal-msg");
    msg.innerHTML = `视频 <strong>${video.name}</strong> 暂无缓存。<br><br>当前最大缓存: ${formatSize(disk.max_cache_size)}<br>已用缓存: ${formatSize(disk.cache_size)}<br>磁盘可用: ${disk.free_percent}%`;

    document.getElementById("cache-modal-expand").classList.add("hidden");
    document.getElementById("cache-modal-msg2").classList.add("hidden");
    document.getElementById("cache-modal-overlay").classList.remove("hidden");
    document.getElementById("cache-modal").classList.remove("hidden");
}

function closeCacheModal() {
    document.getElementById("cache-modal-overlay").classList.add("hidden");
    document.getElementById("cache-modal").classList.add("hidden");
    document.getElementById("cache-modal-expand").classList.add("hidden");
    document.getElementById("cache-modal-msg2").classList.add("hidden");
    pendingPlayVideo = null;
}

async function expandCacheAndPlay() {
    const diskRes = await fetch("/api/disk/status");
    const disk = await diskRes.json();

    if (disk.free_percent < 20) {
        document.getElementById("cache-modal-msg2").textContent = `⚠ 磁盘可用空间不足 20% (剩余 ${disk.free_percent}%)，请先扩容磁盘空间`;
        document.getElementById("cache-modal-msg2").className = "disk-warning";
        document.getElementById("cache-modal-msg2").classList.remove("hidden");
        return;
    }

    const maxAllowed = Math.floor((disk.total * 0.8 - (disk.used - disk.cache_size)) / 1073741824);
    const currentMax = disk.max_cache_size / 1073741824;

    document.getElementById("new-max-cache").value = Math.min(maxAllowed, currentMax + 50);
    document.getElementById("cache-modal-hint").textContent = `磁盘 80% 容量限制下最大可设为 ${maxAllowed} GB`;
    document.getElementById("cache-modal-expand").classList.remove("hidden");
}

async function confirmExpandAndPlay() {
    const newMax = parseInt(document.getElementById("new-max-cache").value, 10);
    if (!newMax || newMax < 1) return;

    const diskRes = await fetch("/api/disk/status");
    const disk = await diskRes.json();
    const maxAllowed = Math.floor((disk.total * 0.8 - (disk.used - disk.cache_size)) / 1073741824);

    if (newMax > maxAllowed) {
        document.getElementById("cache-modal-msg2").textContent = `⚠ 设置值超过磁盘 80% 容量限制 (${maxAllowed} GB)，请先扩容磁盘空间`;
        document.getElementById("cache-modal-msg2").className = "disk-warning";
        document.getElementById("cache-modal-msg2").classList.remove("hidden");
        return;
    }

    await fetch("/api/settings", {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({max_cache_size_gb: newMax}),
    });

    closeCacheModal();
    if (pendingPlayVideo) {
        doPlay(pendingPlayVideo);
    }
}

async function evictAndPlay() {
    if (!pendingPlayVideo) return;
    const video = pendingPlayVideo;

    const res = await fetch(`/api/cache/evict-and-start?video_id=${encodeURIComponent(video.id)}&quality=${video.recommended_quality}`, {method: "POST"});
    const data = await res.json();

    if (!data.ok) {
        document.getElementById("cache-modal-msg2").textContent = `⚠ ${data.msg}`;
        document.getElementById("cache-modal-msg2").className = "disk-warning";
        document.getElementById("cache-modal-msg2").classList.remove("hidden");
        return;
    }

    closeCacheModal();
    doPlay(video);
}

// ---------- Player ----------

async function playVideo(video) {
    const quality = video.recommended_quality;
    try {
        const res = await fetch(`/api/video/${encodeURIComponent(video.id)}/cache-status`);
        const data = await res.json();
        if (data.cached_qualities.includes(quality)) {
            doPlay(video);
            return;
        }
    } catch (e) {
        doPlay(video);
        return;
    }

    // 未缓存，检查磁盘空间
    try {
        const diskRes = await fetch("/api/disk/status");
        const disk = await diskRes.json();
        if (disk.can_cache_more) {
            // 空间充足，显示加载状态，等待转码出足够分片后再播放
            showLoadingOverlay(video, quality);
        } else {
            showCacheModal(video);
        }
    } catch (e) {
        doPlay(video);
    }
}

function showLoadingOverlay(video, quality) {
    document.getElementById("loading-overlay-msg").textContent = `正在加载 ${video.name}...`;
    document.getElementById("loading-overlay").classList.remove("hidden");

    // 先触发转码
    fetch(`/api/video/${encodeURIComponent(video.id)}/stream/${quality}`).then(() => {
        // 转码已有足够分片，直接播放
        document.getElementById("loading-overlay").classList.add("hidden");
        doPlay(video);
    }).catch(() => {
        document.getElementById("loading-overlay").classList.add("hidden");
        doPlay(video);
    });
}

function doPlay(video) {
    currentVideo = video;
    playerState.maxSeekPosition = 0;
    _resetPlayerState();
    const clearBtn = document.getElementById("clear-cache-btn");
    clearBtn.textContent = "清除缓存";
    clearBtn.disabled = false;
    document.getElementById("library").classList.add("hidden");
    document.getElementById("player-section").classList.remove("hidden");
    document.getElementById("toolbar").classList.add("hidden");
    document.getElementById("pagination").classList.add("hidden");
    document.getElementById("batch-progress").classList.add("hidden");
    document.getElementById("disk-warning").classList.add("hidden");

    // 暂停预转码，避免与播放竞争资源
    fetch("/api/pretranscode/pause", { method: "POST" }).catch(() => {});

    const select = document.getElementById("quality-select");
    select.innerHTML = "";
    const qualities = video.height >= 2160 ? ["1080p", "720p", "480p"] : ["720p", "480p"];
    qualities.forEach(q => {
        const opt = document.createElement("option");
        opt.value = q;
        opt.textContent = q;
        if (q === video.recommended_quality) opt.selected = true;
        select.appendChild(opt);
    });

    const resLabel = video.height >= 2160 ? "4K" : `${video.width}x${video.height}`;
    document.getElementById("video-info").innerHTML = `
        <strong>${video.name}</strong> &nbsp;&middot;&nbsp;
        ${resLabel} &nbsp;&middot;&nbsp;
        ${formatDuration(video.duration)} &nbsp;&middot;&nbsp;
        ${formatSize(video.size)} &nbsp;&middot;&nbsp;
        ${video.codec}
    `;

    const vidEl = document.getElementById("video-player");
    const wrapper = document.querySelector(".player-wrapper");

    // Safari / iOS：原生控件模式（原生 HLS 处理更稳定）
    const ua = navigator.userAgent;
    const isIOS = /iphone|ipad|ipod/i.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    const isDesktopSafari = !isIOS && /safari/i.test(ua) && !/chrome|crios|fxios|edg/i.test(ua);
    if ((isIOS || isDesktopSafari) && vidEl.canPlayType("application/vnd.apple.mpegurl")) {
        playerState.nativeHls = true;
        playerState.nativeControls = true;
        vidEl.controls = true;
        wrapper.classList.add("native-controls-mode");

        const streamUrl = `/api/video/${encodeURIComponent(video.id)}/stream/${video.recommended_quality}`;
        vidEl.muted = true;
        vidEl.src = streamUrl;
        const onMeta = () => {
            vidEl.removeEventListener("loadedmetadata", onMeta);
            vidEl.currentTime = 0;
            vidEl.play().catch(() => {});
            playerState.initialSeekDone = true;
        };
        vidEl.addEventListener("loadedmetadata", onMeta, { once: true });

        // 追踪已缓冲范围（供画质切换等使用）
        _listen(vidEl, "timeupdate", () => {
            const buf = vidEl.buffered;
            for (let i = 0; i < buf.length; i++) {
                if (buf.end(i) > playerState.maxSeekPosition) {
                    playerState.maxSeekPosition = buf.end(i);
                }
            }
        });

        currentQuality = video.recommended_quality;
        hideLoading();
        setupTimeDisplay();
        return;
    }

    // Chrome / Safari 17+：hls.js + 自定义控件
    vidEl.controls = false;
    wrapper.classList.remove("native-controls-mode");

    _listen(vidEl, "play", _updatePlayPauseIcon);
    _listen(vidEl, "pause", _updatePlayPauseIcon);
    _listen(vidEl, "click", _onVideoClick);
    _listen(document.getElementById("progress-container"), "click", _onProgressClick);
    _listen(document.getElementById("progress-container"), "touchstart", _onProgressTouchStart);
    _listen(document.getElementById("progress-container"), "touchmove", _onProgressTouchMove);
    _listen(document.getElementById("progress-container"), "touchend", _onProgressTouchEnd);
    _listen(document, "keydown", _onKeydown);

    // 重置进度条
    document.getElementById("progress-played").style.width = "0%";
    document.getElementById("progress-buffered").style.width = "0%";
    document.getElementById("progress-transcoded").style.width = "0%";
    document.getElementById("progress-handle").style.left = "0%";
    document.getElementById("ctrl-time").textContent = "0:00 / " + formatDuration(video.duration);
    hideLoading();

    switchQuality(video.recommended_quality);
    setupTimeDisplay();
}

// ---------- Player State ----------

const _playerListeners = [];
function _listen(el, evt, fn) {
    el.addEventListener(evt, fn);
    _playerListeners.push([el, evt, fn]);
}

function showLoading(msg) {
    const el = document.getElementById("loading-overlay");
    if (el) {
        if (msg) document.getElementById("loading-overlay-msg").textContent = msg;
        el.classList.remove("hidden");
    }
}

function hideLoading() {
    document.getElementById("loading-overlay")?.classList.add("hidden");
}

const playerState = {
    seekingInProgress: false,
    maxSeekPosition: 0,
    initialSeekDone: false,
    destroyed: false,
    lastSeekTime: 0,
    safetyTimeoutId: null,
    seekLocked: false,       // 进度条锁定（seek 期间防止 timeupdate 覆盖）
    seekTargetTime: 0,       // 锁定的目标时间
    nativeHls: false,        // Safari 原生 HLS（无 MSE/MMS）
    nativeControls: false,   // 原生控件模式（Safari <17）
};

const SEEK_DEBOUNCE_MS = 300;

function _resetPlayerState() {
    playerState.seekingInProgress = false;
    playerState.initialSeekDone = false;
    playerState.destroyed = false;
    playerState.lastSeekTime = 0;
    playerState.seekLocked = false;
    playerState.nativeHls = false;
    playerState.nativeControls = false;
    if (playerState.safetyTimeoutId) {
        clearTimeout(playerState.safetyTimeoutId);
        playerState.safetyTimeoutId = null;
    }
}

// ---------- Unified Seek ----------

async function _seekTo(targetTime) {
    if (!currentVideo || playerState.seekingInProgress || !playerState.initialSeekDone) return;
    if (playerState.nativeControls) return; // 原生播放器处理 seek

    const video = document.getElementById("video-player");

    // 1. 已缓冲 → 直接跳
    const buf = video.buffered;
    for (let i = 0; i < buf.length; i++) {
        if (targetTime >= buf.start(i) && targetTime <= buf.end(i)) {
            _lockSeek(targetTime);
            video.currentTime = targetTime;
            _unlockOnSeeked(video, targetTime);
            return;
        }
    }

    // 2. 已转码 → HLS.js 自动拉分片
    if (targetTime < playerState.maxSeekPosition) {
        _lockSeek(targetTime);
        video.currentTime = targetTime;
        _unlockOnSeeked(video, targetTime);
        return;
    }

    // 3. 未转码 → 转码 + 等待 + 播放
    playerState.seekingInProgress = true;
    showLoading("正在从新位置加载...");

    const quality = currentQuality || currentVideo.recommended_quality;
    const videoId = currentVideo.id;

    try {
        const res = await fetch(`/api/video/${encodeURIComponent(videoId)}/seek/${quality}`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({position: targetTime}),
        });
        const data = await res.json();
        if (!data.ok) { _seekFailed("跳转失败"); return; }

        let ready = false;
        for (let i = 0; i < 30; i++) {
            await new Promise(r => setTimeout(r, 300));
            if (playerState.destroyed || currentVideo?.id !== videoId) return;
            try {
                const checkRes = await fetch(`/api/video/${encodeURIComponent(videoId)}/stream/${quality}/segments-ready?seek=${targetTime}`);
                if ((await checkRes.json()).ready) { ready = true; break; }
            } catch (_) {}
        }

        if (!ready || playerState.destroyed || currentVideo?.id !== videoId) {
            _seekFailed("加载超时"); return;
        }

        playerState.maxSeekPosition = Math.max(playerState.maxSeekPosition, targetTime);
        _reloadHlsAtPosition(videoId, quality, targetTime);
    } catch (e) {
        console.error("Seek error:", e);
        _seekFailed("跳转出错");
    }
}

function _lockSeek(targetTime) {
    playerState.seekLocked = true;
    playerState.seekTargetTime = targetTime;
}

function _unlockOnSeeked(video, targetTime) {
    const onSeeked = () => {
        video.removeEventListener("seeked", onSeeked);
        // 视频已跳转到目标位置附近，解锁
        if (Math.abs(video.currentTime - targetTime) < 2) {
            playerState.seekLocked = false;
        }
    };
    video.addEventListener("seeked", onSeeked);
    // 安全超时：防止 seeked 事件不触发时永久锁定
    setTimeout(() => {
        playerState.seekLocked = false;
    }, 3000);
}

function _seekFailed(msg) {
    document.getElementById("status").textContent = msg;
    playerState.seekingInProgress = false;
    hideLoading();
}

// ---------- HLS Instance Factory ----------

function _createHlsInstance(videoId, quality, options) {
    const video = document.getElementById("video-player");
    const {
        url,
        onFirstFragment,
        onSeekError,
        driftTargetTime,
        safetyTimeoutMs = 0,
    } = options;

    if (hls) { hls.destroy(); hls = null; }

    hls = new Hls({
        maxBufferLength: 30,
        maxMaxBufferLength: 120,
        startFragPrefetch: true,
        enableWorker: true,
        fragLoadingMaxRetry: 6,
        fragLoadingRetryDelay: 1000,
    });

    let initialized = false;
    let mediaErrorRetries = 0;
    const MAX_MEDIA_ERROR_RETRIES = 3;

    hls.on(Hls.Events.FRAG_BUFFERED, () => {
        if (playerState.destroyed || currentVideo?.id !== videoId) return;
        if (!initialized) {
            initialized = true;
            playerState.initialSeekDone = true;
            if (onFirstFragment) onFirstFragment(video);
        } else if (driftTargetTime !== undefined) {
            const diff = Math.abs(video.currentTime - driftTargetTime);
            if (diff > 1) {
                video.currentTime = driftTargetTime;
            }
        }
        const buf = video.buffered;
        for (let i = 0; i < buf.length; i++) {
            if (buf.end(i) > playerState.maxSeekPosition) {
                playerState.maxSeekPosition = buf.end(i);
            }
        }
    });

    hls.on(Hls.Events.ERROR, (_, data) => {
        if (!data.fatal) return;
        if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            if (mediaErrorRetries < MAX_MEDIA_ERROR_RETRIES) {
                mediaErrorRetries++;
                console.warn(`HLS media error (attempt ${mediaErrorRetries}/${MAX_MEDIA_ERROR_RETRIES})`);
                hls.recoverMediaError();
            } else {
                console.error("HLS media error: max retries exceeded");
                document.getElementById("status").textContent = "流媒体错误";
                if (onSeekError) onSeekError();
            }
        } else {
            console.error("HLS fatal error:", data);
            document.getElementById("status").textContent = "流媒体错误";
            if (onSeekError) onSeekError();
        }
    });

    hls.attachMedia(video);
    hls.loadSource(url);

    if (safetyTimeoutMs > 0) {
        playerState.safetyTimeoutId = setTimeout(() => {
            playerState.safetyTimeoutId = null;
            if (playerState.seekingInProgress && currentVideo?.id === videoId && !initialized) {
                playerState.seekingInProgress = false;
                hideLoading();
                video.onseeking = () => handleVideoSeek(video, quality);
            }
        }, safetyTimeoutMs);
    }
}

// ---------- HLS Reload at Seek Position ----------

function _reloadHlsAtPosition(videoId, quality, targetTime) {
    const video = document.getElementById("video-player");
    video.onseeking = null;
    video.pause();
    showLoading();

    // hls.js 路径
    let seekInited = false;
    let seekRetryCount = 0;
    const MAX_SEEK_RETRIES = 10;

    function trySetCurrentTime() {
        if (playerState.destroyed) return;

        const sr = video.seekable;
        let canSeek = false;
        for (let k = 0; k < sr.length; k++) {
            if (targetTime >= sr.start(k) && targetTime <= sr.end(k)) {
                canSeek = true;
                break;
            }
        }

        if (canSeek || seekRetryCount >= MAX_SEEK_RETRIES) {
            video.currentTime = targetTime;
            video.onseeking = () => handleVideoSeek(video, quality);
            playerState.seekingInProgress = false;
            playerState.seekLocked = false;
            hideLoading();
            video.play().catch(() => {});
        } else {
            seekRetryCount++;
            setTimeout(trySetCurrentTime, 100);
        }
    }

    _createHlsInstance(videoId, quality, {
        url: `/api/video/${encodeURIComponent(videoId)}/stream/${quality}?start=${targetTime}`,
        onFirstFragment: () => {
            seekInited = true;
            trySetCurrentTime();
        },
        onSeekError: () => {
            playerState.seekingInProgress = false;
            hideLoading();
        },
        driftTargetTime: targetTime,
        safetyTimeoutMs: 15000,
    });

    if (_loadedMetadataHandler) {
        video.removeEventListener("loadedmetadata", _loadedMetadataHandler);
        _loadedMetadataHandler = null;
    }
    _loadedMetadataHandler = () => {
        if (playerState.destroyed) return;
        if (!seekInited) {
            video.currentTime = targetTime;
        }
    };
    video.addEventListener("loadedmetadata", _loadedMetadataHandler, { once: true });
}

// ---------- Switch Quality ----------

function switchQuality(quality) {
    if (!currentVideo) return;
    currentQuality = quality;

    const video = document.getElementById("video-player");
    const videoId = currentVideo.id;
    const url = `/api/video/${encodeURIComponent(videoId)}/stream/${quality}`;

    video.muted = true;
    _resetPlayerState();
    playerState.maxSeekPosition = 0;
    hideLoading();

    // 原生控件模式：直接设置 src，保留播放位置
    if (playerState.nativeControls) {
        const currentTime = video.currentTime;
        const wasPlaying = !video.paused;
        video.src = url;
        const onMeta = () => {
            video.removeEventListener("loadedmetadata", onMeta);
            video.currentTime = currentTime;
            if (wasPlaying) video.play().catch(() => {});
            playerState.initialSeekDone = true;
        };
        video.addEventListener("loadedmetadata", onMeta, { once: true });
        return;
    }

    if (Hls.isSupported()) {
        _createHlsInstance(videoId, quality, {
            url: url,
            onFirstFragment: (v) => {
                v.currentTime = 0;
                v.play().catch(() => {});
            },
        });
        video.onseeking = () => handleVideoSeek(video, quality);
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        playerState.nativeHls = true;
        video.src = url;
        const onMeta = () => {
            video.currentTime = 0;
            video.play().catch(() => {});
            playerState.initialSeekDone = true;
            video.onseeking = () => handleVideoSeek(video, quality);
        };
        video.addEventListener("loadedmetadata", onMeta, { once: true });
    } else {
        document.getElementById("status").textContent = "当前浏览器不支持 HLS";
    }
}

// ---------- Handle Video Seek ----------

function handleVideoSeek(video, quality) {
    if (playerState.nativeControls) return;
    if (playerState.seekingInProgress || !playerState.initialSeekDone) return;
    const now = Date.now();
    if (now - playerState.lastSeekTime < SEEK_DEBOUNCE_MS) return;
    playerState.lastSeekTime = now;
    _seekTo(video.currentTime);
}

// ---------- Progress Bar & Custom Controls ----------

let _timeUpdateHandler = null;

function _updateProgressBar() {
    const video = document.getElementById("video-player");
    const duration = currentVideo?.duration || video.duration || 0;
    if (!duration) return;

    // seek 锁定期间，进度条保持在目标位置不动
    if (playerState.seekLocked) {
        const targetPct = (playerState.seekTargetTime / duration) * 100;
        document.getElementById("progress-played").style.width = targetPct + "%";
        document.getElementById("progress-handle").style.left = targetPct + "%";
        document.getElementById("ctrl-time").textContent =
            formatDuration(playerState.seekTargetTime) + " / " + formatDuration(duration);
        // 仍更新缓冲和转码进度条
        _updateBufferedBars(video, duration);
        return;
    }

    const current = video.currentTime;
    const pct = (current / duration) * 100;

    document.getElementById("progress-played").style.width = pct + "%";
    document.getElementById("progress-handle").style.left = pct + "%";
    document.getElementById("ctrl-time").textContent =
        formatDuration(current) + " / " + formatDuration(duration);

    _updateBufferedBars(video, duration);
}

function _updateBufferedBars(video, duration) {
    const buf = video.buffered;
    let maxBuf = 0;
    for (let i = 0; i < buf.length; i++) {
        if (buf.end(i) > maxBuf) maxBuf = buf.end(i);
    }
    document.getElementById("progress-buffered").style.width = (maxBuf / duration) * 100 + "%";
    const transcodedEnd = Math.max(maxBuf, playerState.maxSeekPosition);
    document.getElementById("progress-transcoded").style.width = (transcodedEnd / duration) * 100 + "%";
}

function setupTimeDisplay() {
    const video = document.getElementById("video-player");

    if (_timeUpdateHandler) {
        video.removeEventListener("timeupdate", _timeUpdateHandler);
    }

    _timeUpdateHandler = () => _updateProgressBar();
    video.addEventListener("timeupdate", _timeUpdateHandler);

    _updateProgressBar();
}

function togglePlayPause() {
    const video = document.getElementById("video-player");
    if (!currentVideo) return;
    if (video.paused) {
        video.play().catch(() => {});
    } else {
        video.pause();
    }
}

function _updatePlayPauseIcon() {
    const video = document.getElementById("video-player");
    const playIcon = document.getElementById("icon-play");
    const pauseIcon = document.getElementById("icon-pause");
    if (!video || !playIcon || !pauseIcon) return;
    if (video.paused) {
        playIcon.style.display = "";
        pauseIcon.style.display = "none";
    } else {
        playIcon.style.display = "none";
        pauseIcon.style.display = "";
    }
}

function _onVideoClick(e) {
    if (!currentVideo) return;
    // 如果点击的是控件区域，不处理
    if (e.target.closest(".custom-controls")) return;
    togglePlayPause();
}

function _onProgressClick(e) {
    if (!currentVideo) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    _seekTo(ratio * (currentVideo.duration || 0));
}

function _onProgressTouchStart(e) {
    if (!currentVideo) return;
    e.preventDefault();
    const touch = e.touches[0];
    _progressDrag(touch);
}

function _onProgressTouchMove(e) {
    if (!currentVideo) return;
    e.preventDefault();
    _progressDrag(e.touches[0]);
}

function _onProgressTouchEnd(e) {
    if (!currentVideo) return;
    e.preventDefault();
    const touch = e.changedTouches[0];
    const container = document.getElementById("progress-container");
    const rect = container.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (touch.clientX - rect.left) / rect.width));
    _seekTo(ratio * (currentVideo.duration || 0));
}

function _progressDrag(touch) {
    const container = document.getElementById("progress-container");
    const rect = container.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (touch.clientX - rect.left) / rect.width));
    const duration = currentVideo.duration || 0;
    const pct = ratio * 100;
    document.getElementById("progress-played").style.width = pct + "%";
    document.getElementById("progress-handle").style.left = pct + "%";
    document.getElementById("ctrl-time").textContent =
        formatDuration(ratio * duration) + " / " + formatDuration(duration);
}

function toggleFullscreen() {
    const wrapper = document.querySelector(".player-wrapper");
    if (!wrapper) return;
    if (document.fullscreenElement) {
        document.exitFullscreen();
    } else {
        wrapper.requestFullscreen().catch(() => {});
    }
}

function _onKeydown(e) {
    if (!currentVideo) return;
    const video = document.getElementById("video-player");
    switch (e.key) {
        case " ":
        case "k":
            e.preventDefault();
            togglePlayPause();
            break;
        case "ArrowLeft":
            e.preventDefault();
            video.currentTime = Math.max(0, video.currentTime - 5);
            break;
        case "ArrowRight":
            e.preventDefault();
            video.currentTime = Math.min(video.duration || 0, video.currentTime + 5);
            break;
        case "f":
            e.preventDefault();
            toggleFullscreen();
            break;
    }
}

// ---------- Clear Video Cache ----------

async function clearCurrentVideoCache() {
    if (!currentVideo) return;
    if (!confirm(`确定清除「${currentVideo.name}」的缓存分片？`)) return;

    const btn = document.getElementById("clear-cache-btn");
    btn.disabled = true;
    btn.textContent = "清除中...";

    try {
        const res = await fetch(`/api/video/${encodeURIComponent(currentVideo.id)}/cache/clear`, {
            method: "POST",
        });
        const data = await res.json();
        if (data.ok) {
            goBack();
            return;
        } else {
            btn.textContent = "清除失败";
            setTimeout(() => {
                btn.textContent = "清除缓存";
                btn.disabled = false;
            }, 2000);
        }
    } catch (e) {
        console.error("Clear cache error:", e);
        btn.textContent = "清除失败";
        setTimeout(() => {
            btn.textContent = "清除缓存";
            btn.disabled = false;
        }, 2000);
    }
}

function goBack() {
    playerState.destroyed = true;
    if (hls) {
        hls.destroy();
        hls = null;
    }
    const video = document.getElementById("video-player");
    video.pause();
    video.removeAttribute("src");
    video.controls = false;
    video.onseeking = null;
    document.querySelector(".player-wrapper")?.classList.remove("native-controls-mode");

    // 批量移除事件监听
    for (const [el, evt, fn] of _playerListeners) {
        el.removeEventListener(evt, fn);
    }
    _playerListeners.length = 0;
    if (_loadedMetadataHandler) {
        video.removeEventListener("loadedmetadata", _loadedMetadataHandler);
        _loadedMetadataHandler = null;
    }
    if (_timeUpdateHandler) {
        video.removeEventListener("timeupdate", _timeUpdateHandler);
        _timeUpdateHandler = null;
    }

    _resetPlayerState();
    playerState.maxSeekPosition = 0;
    hideLoading();

    document.getElementById("player-section").classList.add("hidden");
    document.getElementById("library").classList.remove("hidden");
    document.getElementById("toolbar").classList.remove("hidden");
    document.getElementById("pagination").classList.remove("hidden");
    document.getElementById("status").textContent = "";
    currentVideo = null;
    currentQuality = null;

    // 恢复预转码
    fetch("/api/pretranscode/resume", { method: "POST" }).catch(() => {});
    checkDiskStatus();
}

// ---------- Init ----------

loadVideos();
checkDiskStatus();
pollActiveProgress();
