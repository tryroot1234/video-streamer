let hls = null;
let currentVideo = null;
let currentQuality = null;

let allVideos = [];
let currentPage = 1;
let perPage = 12;
let currentSort = "time-desc";

let batchPollTimer = null;
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
        const res = await fetch("/api/cache/init", {method: "POST"});
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

                // 检查磁盘状态
                checkDiskStatus();
            }
        } catch (e) {
            console.error("Poll batch progress error", e);
        }
    }, 2000);
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
        if (!data.cached_qualities.includes(quality)) {
            showCacheModal(video);
            return;
        }
    } catch (e) {
        // 如果检查失败，直接播放
    }
    doPlay(video);
}

function doPlay(video) {
    currentVideo = video;
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
}

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

    if (Hls.isSupported()) {
        hls = new Hls({
            maxBufferLength: 30,
            maxMaxBufferLength: 120,
            startFragPrefetch: true,
            liveDurationInfinity: true,
            enableWorker: true,
        });
        hls.loadSource(url);
        hls.attachMedia(video);
        let seekedToStart = false;
        hls.on(Hls.Events.FRAG_BUFFERED, () => {
            if (!seekedToStart) {
                seekedToStart = true;
                video.currentTime = 0;
                video.play().catch(() => {});
            }
        });
        hls.on(Hls.Events.ERROR, (_, data) => {
            if (data.fatal) {
                console.error("HLS fatal error:", data);
                document.getElementById("status").textContent = "流媒体错误";
            }
        });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = url;
        video.addEventListener("loadedmetadata", () => {
            video.currentTime = 0;
            video.play().catch(() => {});
        });
    } else {
        document.getElementById("status").textContent = "当前浏览器不支持 HLS";
    }
}

function goBack() {
    if (hls) {
        hls.destroy();
        hls = null;
    }
    const video = document.getElementById("video-player");
    video.pause();
    video.removeAttribute("src");

    document.getElementById("player-section").classList.add("hidden");
    document.getElementById("library").classList.remove("hidden");
    document.getElementById("toolbar").classList.remove("hidden");
    document.getElementById("pagination").classList.remove("hidden");
    currentVideo = null;

    checkDiskStatus();
}

// ---------- Init ----------

loadVideos();
checkDiskStatus();
