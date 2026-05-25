let hls = null;
let currentVideo = null;
let currentQuality = null;

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
        document.getElementById("input-video-dir").value = s.video_dir || "";
        document.getElementById("input-cache-dir").value = s.cache_dir || "";
        document.getElementById("input-max-cache").value = s.max_cache_size_gb || 50;
    } catch (e) {
        console.error("Failed to load settings", e);
    }
}

async function saveSettings() {
    const btn = document.getElementById("save-btn");
    const msg = document.getElementById("settings-msg");
    btn.disabled = true;
    msg.classList.add("hidden");

    const body = {
        video_dir: document.getElementById("input-video-dir").value.trim(),
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
    _maxBufferedEnd = 0;
    _seekingInProgress = false;
    document.getElementById("library").classList.add("hidden");
    document.getElementById("player-section").classList.remove("hidden");
    document.getElementById("toolbar").classList.add("hidden");
    document.getElementById("pagination").classList.add("hidden");
    document.getElementById("batch-progress").classList.add("hidden");
    document.getElementById("disk-warning").classList.add("hidden");

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

    switchQuality(video.recommended_quality);
    setupTimeDisplay();
}

let _seekingInProgress = false;
let _maxBufferedEnd = 0;
let _initialSeekDone = false;
let _pendingSeekTime = null;
let _pendingSeekTimer = null;
let _destroyed = false;
let _lastSeekTime = 0;
const SEEK_DEBOUNCE_MS = 800;

function switchQuality(quality) {
    if (!currentVideo) return;
    currentQuality = quality;

    const video = document.getElementById("video-player");
    const url = `/api/video/${encodeURIComponent(currentVideo.id)}/stream/${quality}`;

    if (hls) {
        hls.destroy();
        hls = null;
    }

    video.muted = true;
    _initialSeekDone = false;
    _seekingInProgress = false;
    _maxBufferedEnd = 0;
    _lastSeekTime = 0;
    if (_pendingSeekTimer) { clearTimeout(_pendingSeekTimer); _pendingSeekTimer = null; }
    _pendingSeekTime = null;
    _destroyed = false;

    if (Hls.isSupported()) {
        hls = new Hls({
            maxBufferLength: 30,
            maxMaxBufferLength: 120,
            startFragPrefetch: true,
            enableWorker: true,
            fragLoadingMaxRetry: 6,
            fragLoadingRetryDelay: 1000,
        });
        hls.loadSource(url);
        hls.attachMedia(video);
        let inited = false;
        hls.on(Hls.Events.FRAG_BUFFERED, () => {
            if (_pendingSeekTime !== null) {
                // 检查缓冲是否已覆盖 seek 目标位置
                const buf = video.buffered;
                let covered = false;
                for (let i = 0; i < buf.length; i++) {
                    if (_pendingSeekTime >= buf.start(i) && _pendingSeekTime <= buf.end(i)) {
                        covered = true;
                        break;
                    }
                }
                if (covered) {
                    // 缓冲已覆盖，执行跳转并恢复 seeking 监听
                    if (_pendingSeekTimer) { clearTimeout(_pendingSeekTimer); _pendingSeekTimer = null; }
                    video.currentTime = _pendingSeekTime;
                    _pendingSeekTime = null;
                    _initialSeekDone = true;
                    video.onseeking = () => handleVideoSeek(video, quality);
                    video.play().catch(() => {});
                }
                // 未覆盖时等待下一个 FRAG_BUFFERED 事件
                return;
            }
            if (!inited) {
                inited = true;
                _initialSeekDone = true;
                video.currentTime = 0;
                video.play().catch(() => {});
            }
            const buf = video.buffered;
            _maxBufferedEnd = 0;
            for (let i = 0; i < buf.length; i++) {
                if (buf.end(i) > _maxBufferedEnd) _maxBufferedEnd = buf.end(i);
            }
        });
        hls.on(Hls.Events.ERROR, (_, data) => {
            if (data.fatal) {
                console.error("HLS fatal error:", data);
                document.getElementById("status").textContent = "流媒体错误，请刷新页面";
            }
        });

        video.onseeking = () => handleVideoSeek(video, quality);
    } else {
        document.getElementById("status").textContent = "当前浏览器不支持 HLS";
    }
}

async function handleVideoSeek(video, quality) {
    if (_seekingInProgress) return;
    if (!_initialSeekDone) return;

    // 防抖：过滤 hls.js gap-controller 等内部 seek 事件
    const now = Date.now();
    if (now - _lastSeekTime < SEEK_DEBOUNCE_MS) return;
    _lastSeekTime = now;

    const seekTime = video.currentTime;
    const buf = video.buffered;

    // 重新计算当前缓冲末端（Safari 会主动回收已缓冲区间）
    _maxBufferedEnd = 0;
    for (let i = 0; i < buf.length; i++) {
        if (buf.end(i) > _maxBufferedEnd) _maxBufferedEnd = buf.end(i);
    }

    // 在已缓冲范围内，不需要断点缓存
    for (let i = 0; i < buf.length; i++) {
        if (seekTime >= buf.start(i) && seekTime <= buf.end(i)) return;
    }
    if (seekTime < _maxBufferedEnd) return;

    // 拖拽到未缓冲位置，触发断点缓存
    _seekingInProgress = true;
    const videoId = currentVideo.id;
    const statusEl = document.getElementById("status");

    try {
        statusEl.textContent = "正在从新位置加载...";

        // 启动 seek 转码（会自动停止之前的 seek 转码）
        const res = await fetch(`/api/video/${encodeURIComponent(videoId)}/seek/${quality}`, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({position: seekTime}),
        });
        const data = await res.json();
        if (!data.ok) {
            statusEl.textContent = "跳转失败";
            _seekingInProgress = false;
            return;
        }

        // 等待目标位置分片就绪
        let ready = false;
        for (let i = 0; i < 30; i++) {
            await new Promise(r => setTimeout(r, 500));
            if (_destroyed || currentVideo?.id !== videoId) break;
            try {
                const checkRes = await fetch(`/api/video/${encodeURIComponent(videoId)}/stream/${quality}/segments-ready?seek=${seekTime}`);
                const checkData = await checkRes.json();
                if (checkData.ready) { ready = true; break; }
            } catch (_) {}
        }

        if (ready && !_destroyed && currentVideo?.id === videoId) {
            // 分片就绪，加载从 seek 位置开始的 m3u8
            // start 参数让后端生成截断 m3u8，hls.js 直接从目标位置缓冲
            video.onseeking = null;
            _pendingSeekTime = seekTime;
            hls.loadSource(`/api/video/${encodeURIComponent(videoId)}/stream/${quality}?start=${seekTime}`);
            // 超时兜底：若 5 秒内缓冲未覆盖目标位置，强制跳转
            if (_pendingSeekTimer) clearTimeout(_pendingSeekTimer);
            _pendingSeekTimer = setTimeout(() => {
                if (_pendingSeekTime !== null && !_destroyed) {
                    video.currentTime = _pendingSeekTime;
                    _pendingSeekTime = null;
                    _initialSeekDone = true;
                    video.onseeking = () => handleVideoSeek(video, quality);
                    video.play().catch(() => {});
                }
                _pendingSeekTimer = null;
            }, 5000);
            statusEl.textContent = "";
        } else if (!ready) {
            statusEl.textContent = "加载超时，请重试";
        }
    } catch (e) {
        console.error("Seek error:", e);
        statusEl.textContent = "跳转出错";
    } finally {
        _seekingInProgress = false;
    }
}

// ---------- Time Display ----------

let _timeUpdateHandler = null;

function setupTimeDisplay() {
    const video = document.getElementById("video-player");
    const curEl = document.getElementById("player-current-time");
    const totalEl = document.getElementById("player-total-time");

    // 清除旧的事件监听
    if (_timeUpdateHandler) {
        video.removeEventListener("timeupdate", _timeUpdateHandler);
    }

    _timeUpdateHandler = () => {
        curEl.textContent = formatDuration(video.currentTime);
        // duration 可能是 Infinity (直播流) 或 NaN
        if (video.duration && isFinite(video.duration) && video.duration > 0) {
            totalEl.textContent = formatDuration(video.duration);
        } else if (currentVideo) {
            totalEl.textContent = formatDuration(currentVideo.duration);
        }
    };
    video.addEventListener("timeupdate", _timeUpdateHandler);

    // 初始化显示
    curEl.textContent = "0:00";
    totalEl.textContent = currentVideo ? formatDuration(currentVideo.duration) : "0:00";
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
    _destroyed = true;
    if (hls) {
        hls.destroy();
        hls = null;
    }
    const video = document.getElementById("video-player");
    video.pause();
    video.removeAttribute("src");
    if (_timeUpdateHandler) {
        video.removeEventListener("timeupdate", _timeUpdateHandler);
        _timeUpdateHandler = null;
    }

    _seekingInProgress = false;
    _maxBufferedEnd = 0;
    _initialSeekDone = false;
    _lastSeekTime = 0;
    if (_pendingSeekTimer) { clearTimeout(_pendingSeekTimer); _pendingSeekTimer = null; }
    _pendingSeekTime = null;
    _destroyed = false;

    document.getElementById("player-section").classList.add("hidden");
    document.getElementById("library").classList.remove("hidden");
    document.getElementById("toolbar").classList.remove("hidden");
    document.getElementById("pagination").classList.remove("hidden");
    document.getElementById("status").textContent = "";
    currentVideo = null;

    checkDiskStatus();
}

// ---------- Init ----------

loadVideos();
checkDiskStatus();
pollActiveProgress();
