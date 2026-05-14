// NAS 文件管理器 - 前端逻辑

const API_BASE = "";
let nasBasePath = "";        // 启动时从 /api/config/app 拉
let currentPath = "";
let currentFiles = [];
let selectedFiles = new Set();
let deleteModal = null;
let apiToken = localStorage.getItem("nas_token") || "";

// 初始化
document.addEventListener("DOMContentLoaded", async () => {
    deleteModal = new bootstrap.Modal(document.getElementById("deleteModal"));

    // 检查 token
    if (!apiToken) {
        apiToken = prompt("请输入 API Token:");
        if (apiToken) {
            localStorage.setItem("nas_token", apiToken);
        } else {
            alert("需要 API Token 才能使用");
            return;
        }
    }

    // 先拿后端配置（NAS_BASE_PATH），再加载文件列表
    try {
        const res = await apiFetch(`${API_BASE}/api/config/app`);
        const cfg = await res.json();
        nasBasePath = cfg.nas_base_path;
        currentPath = nasBasePath;
    } catch (err) {
        showError(`加载配置失败: ${err.message}`);
        return;
    }

    // 未配置 NAS 时自动打开配置框
    try {
        const nasRes = await apiFetch(`${API_BASE}/api/config/nas`);
        const nasCfg = await nasRes.json();
        if (!nasCfg.configured) {
            addLog("尚未配置 NAS 连接，请填写后保存", "info");
            showNASConfig();
            return;
        }
    } catch (err) {
        // 忽略，按正常流程继续
    }

    refreshDisk();
    loadFiles(currentPath);
});

// API 请求封装
async function apiFetch(url, options = {}) {
    const headers = {
        ...options.headers,
        "Authorization": `Bearer ${apiToken}`
    };

    const res = await fetch(url, { ...options, headers });

    if (res.status === 401) {
        localStorage.removeItem("nas_token");
        alert("Token 无效或已过期，请刷新页面重新输入");
        throw new Error("Unauthorized");
    }

    return res;
}

// HTML 转义
function escapeHtml(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

// 安全创建元素
function createElement(tag, attrs = {}, children = []) {
    const el = document.createElement(tag);
    Object.entries(attrs).forEach(([key, value]) => {
        if (key === "className") {
            el.className = value;
        } else if (key === "textContent") {
            el.textContent = value;
        } else if (key === "innerHTML") {
            el.innerHTML = value;
        } else if (key === "dataset") {
            // 把 {path, isDir} 翻译成 data-path / data-is-dir，否则 DOMStringMap 拿不到
            Object.entries(value).forEach(([dk, dv]) => {
                const attr = "data-" + dk.replace(/[A-Z]/g, c => "-" + c.toLowerCase());
                el.setAttribute(attr, dv);
            });
        } else if (key.startsWith("on")) {
            el.addEventListener(key.slice(2).toLowerCase(), value);
        } else {
            el.setAttribute(key, value);
        }
    });
    children.forEach(child => {
        if (typeof child === "string") {
            el.appendChild(document.createTextNode(child));
        } else if (child) {
            el.appendChild(child);
        }
    });
    return el;
}

// ==================== 磁盘信息 ====================

async function refreshDisk() {
    try {
        const res = await apiFetch(`${API_BASE}/api/disk`);
        const data = await res.json();
        renderDiskCards(data.disks);
    } catch (err) {
        console.error("Failed to load disk info:", err);
    }
}

function renderDiskCards(disks) {
    const container = document.getElementById("disk-cards");
    container.innerHTML = "";

    disks.forEach(disk => {
        const percent = parseInt(disk.use_percent);
        const color = percent > 90 ? "danger" : percent > 70 ? "warning" : "success";
        // 用挂载点最后一段作为简短标签
        const label = (disk.mount || "").split("/").pop() || disk.mount;

        const card = createElement("div", { className: "col-md-6" }, [
            createElement("div", {
                className: "card disk-card",
                title: `${disk.mount}\n已用 ${disk.used} / 可用 ${disk.available} / 总计 ${disk.size} (${disk.use_percent})`
            }, [
                createElement("div", { className: "card-body" }, [
                    createElement("div", { className: "d-flex justify-content-between align-items-center" }, [
                        createElement("small", { className: "fw-bold", textContent: label }),
                        createElement("small", {
                            className: `text-${color}`,
                            textContent: `${disk.used}/${disk.size}`
                        })
                    ]),
                    createElement("div", { className: "progress" }, [
                        createElement("div", {
                            className: `progress-bar bg-${color}`,
                            style: `width: ${percent}%`
                        })
                    ])
                ])
            ])
        ]);

        container.appendChild(card);
    });
}

// ==================== 文件列表 ====================

async function loadFiles(path) {
    currentPath = path;
    selectedFiles.clear();
    updateSelectionUI();

    const loading = document.getElementById("loading");
    const fileList = document.getElementById("file-list");
    const emptyState = document.getElementById("empty-state");

    loading.style.display = "block";
    fileList.innerHTML = "";
    emptyState.style.display = "none";

    try {
        const res = await apiFetch(`${API_BASE}/api/files?path=${encodeURIComponent(path)}`);
        const data = await res.json();

        if (data.error) {
            showError(data.error);
            return;
        }

        currentFiles = data.files;
        updateBreadcrumb(path);
        renderFiles(data.files);
    } catch (err) {
        showError("加载失败: " + err.message);
    } finally {
        loading.style.display = "none";
    }
}

function renderFiles(files) {
    const tbody = document.getElementById("file-list");
    const emptyState = document.getElementById("empty-state");
    const fileCount = document.getElementById("file-count");

    tbody.innerHTML = "";

    // 添加"返回上级"行（如果不是根目录）
    if (currentPath !== nasBasePath) {
        const parentPath = currentPath.substring(0, currentPath.lastIndexOf("/")) || nasBasePath;

        const tr = createElement("tr", { className: "table-active" });
        const td1 = createElement("td", { className: "checkbox-cell" });
        tr.appendChild(td1);

        const td2 = createElement("td", { colspan: "4" });
        const icon = createElement("i", { className: "bi bi-arrow-left-circle file-icon dir" });
        td2.appendChild(icon);
        const link = createElement("a", {
            className: "file-name dir",
            textContent: ".. 返回上级"
        });
        link.addEventListener("click", () => loadFiles(parentPath));
        td2.appendChild(link);
        tr.appendChild(td2);

        const td3 = createElement("td");
        tr.appendChild(td3);

        tbody.appendChild(tr);
    }

    if (files.length === 0) {
        emptyState.style.display = "block";
        fileCount.textContent = "0 个项目";
        return;
    }

    emptyState.style.display = "none";
    fileCount.textContent = `${files.length} 个项目`;

    files.forEach(file => {
        const icon = getFileIcon(file);
        const isHardlink = file.hardlinks > 1;

        const tr = createElement("tr", {
            className: selectedFiles.has(file.path) ? "table-active" : "",
            dataset: { path: file.path, isDir: file.is_dir.toString() }
        });

        // 复选框
        const td1 = createElement("td", { className: "checkbox-cell" });
        const checkbox = createElement("input", {
            type: "checkbox",
            className: "form-check-input file-checkbox"
        });
        checkbox.checked = selectedFiles.has(file.path);
        checkbox.addEventListener("change", () => toggleSelect(file.path, file.size));
        td1.appendChild(checkbox);
        tr.appendChild(td1);

        // 文件名
        const td2 = createElement("td");
        const iconEl = createElement("i", { className: `bi ${icon.class} file-icon ${icon.type}` });
        td2.appendChild(iconEl);

        const nameLink = createElement("a", {
            className: `file-name ${file.is_dir ? "dir" : ""}`,
            textContent: file.name
        });
        nameLink.addEventListener("click", () => {
            if (file.is_dir) {
                loadFiles(file.path);
            } else {
                showDetail(file.path);
            }
        });
        td2.appendChild(nameLink);

        if (isHardlink) {
            const badge = createElement("span", {
                className: "badge bg-info hardlink-badge",
                textContent: file.hardlinks.toString(),
                title: `硬链接: ${file.hardlinks} 个`
            });
            td2.appendChild(badge);
        }
        tr.appendChild(td2);

        // 大小
        const td3 = createElement("td", {
            className: "text-end",
            textContent: file.size_human
        });
        tr.appendChild(td3);

        // 链接数
        const td4 = createElement("td", {
            className: "text-center",
            textContent: file.hardlinks.toString()
        });
        tr.appendChild(td4);

        // 修改时间
        const td5 = createElement("td", {
            className: "text-secondary",
            textContent: file.modified
        });
        tr.appendChild(td5);

        // 操作
        const td6 = createElement("td");
        const btnGroup = createElement("div", { className: "btn-group btn-group-sm" });

        if (file.is_dir) {
            const openBtn = createElement("button", {
                className: "btn btn-outline-warning",
                title: "进入目录"
            });
            openBtn.innerHTML = '<i class="bi bi-folder2-open"></i>';
            openBtn.addEventListener("click", () => loadFiles(file.path));
            btnGroup.appendChild(openBtn);
        } else {
            const infoBtn = createElement("button", {
                className: "btn btn-outline-info",
                title: "详情"
            });
            infoBtn.innerHTML = '<i class="bi bi-info-circle"></i>';
            infoBtn.addEventListener("click", () => showDetail(file.path));
            btnGroup.appendChild(infoBtn);
        }

        const deleteBtn = createElement("button", {
            className: "btn btn-outline-danger",
            title: "删除"
        });
        deleteBtn.innerHTML = '<i class="bi bi-trash"></i>';
        deleteBtn.addEventListener("click", () => deleteSingle(file.path));
        btnGroup.appendChild(deleteBtn);

        td6.appendChild(btnGroup);
        tr.appendChild(td6);

        tbody.appendChild(tr);
    });
}

function getFileIcon(file) {
    if (file.is_dir) return { class: "bi-folder-fill", type: "dir" };

    const ext = file.name.split(".").pop().toLowerCase();
    const videoExts = ["mkv", "mp4", "avi", "ts", "m4v", "wmv", "flv", "mov"];
    const subExts = ["srt", "ass", "ssa", "sub", "idx", "sup"];

    if (videoExts.includes(ext)) return { class: "bi-film", type: "video" };
    if (subExts.includes(ext)) return { class: "bi-file-earmark-text", type: "subtitle" };
    if (file.name.endsWith(".nfo")) return { class: "bi-file-earmark-code", type: "other" };
    if (file.name.match(/\.(jpg|jpeg|png|gif|bmp)$/i)) return { class: "bi-image", type: "other" };

    return { class: "bi-file-earmark", type: "other" };
}

// ==================== 面包屑导航 ====================

function updateBreadcrumb(path) {
    const parts = path.split("/").filter(p => p);
    const baseParts = nasBasePath.split("/").filter(p => p);
    const relativeParts = parts.slice(baseParts.length);

    const breadcrumb = document.getElementById("breadcrumb-list");
    breadcrumb.innerHTML = "";

    // 根目录
    const rootLi = createElement("li", { className: "breadcrumb-item" });
    const rootLink = createElement("a", { textContent: "NAS" });
    rootLink.addEventListener("click", () => loadFiles(nasBasePath));
    rootLi.appendChild(rootLink);
    breadcrumb.appendChild(rootLi);

    // 子目录
    let currentPath = nasBasePath;
    relativeParts.forEach((part, index) => {
        currentPath += "/" + part;
        const isLast = index === relativeParts.length - 1;
        const li = createElement("li", {
            className: `breadcrumb-item ${isLast ? "active" : ""}`
        });

        if (isLast) {
            li.textContent = part;
        } else {
            const link = createElement("a", { textContent: part });
            const pathCopy = currentPath;
            link.addEventListener("click", () => loadFiles(pathCopy));
            li.appendChild(link);
        }

        breadcrumb.appendChild(li);
    });

    document.getElementById("current-path").textContent = path;
}

// ==================== 文件筛选和排序 ====================

function filterFiles() {
    const search = document.getElementById("search-input").value.toLowerCase();
    const type = document.getElementById("filter-type").value;

    let filtered = currentFiles.filter(f => {
        if (search && !f.name.toLowerCase().includes(search)) return false;
        if (type === "dir" && !f.is_dir) return false;
        if (type === "video" && !f.name.match(/\.(mkv|mp4|avi|ts|m4v)$/i)) return false;
        if (type === "hardlink" && f.hardlinks <= 1) return false;
        return true;
    });

    renderFiles(filtered);
}

function sortFiles() {
    const sortBy = document.getElementById("sort-by").value;

    const sorted = [...currentFiles].sort((a, b) => {
        if (sortBy === "name") return a.name.localeCompare(b.name);
        if (sortBy === "size") return b.size - a.size;
        if (sortBy === "modified") return b.modified.localeCompare(a.modified);
        if (sortBy === "hardlinks") return b.hardlinks - a.hardlinks;
        return 0;
    });

    renderFiles(sorted);
}

// ==================== 文件选择 ====================

function toggleSelectAll() {
    const checked = document.getElementById("select-all").checked;
    const checkboxes = document.querySelectorAll(".file-checkbox");

    checkboxes.forEach(cb => {
        const row = cb.closest("tr");
        const path = row.dataset.path;
        const file = currentFiles.find(f => f.path === path);

        if (checked) {
            selectedFiles.add(path);
        } else {
            selectedFiles.delete(path);
        }
        cb.checked = checked;
        row.classList.toggle("table-active", checked);
    });

    updateSelectionUI();
}

function toggleSelect(path, size) {
    if (selectedFiles.has(path)) {
        selectedFiles.delete(path);
    } else {
        selectedFiles.add(path);
    }

    const row = document.querySelector(`tr[data-path="${CSS.escape(path)}"]`);
    if (row) {
        row.classList.toggle("table-active", selectedFiles.has(path));
    }

    updateSelectionUI();
}

function clearSelection() {
    selectedFiles.clear();
    document.querySelectorAll(".file-checkbox").forEach(cb => cb.checked = false);
    document.querySelectorAll("tr").forEach(row => row.classList.remove("table-active"));
    document.getElementById("select-all").checked = false;
    updateSelectionUI();
}

function updateSelectionUI() {
    const count = selectedFiles.size;
    document.getElementById("selected-count").textContent = count;

    let totalSize = 0;
    selectedFiles.forEach(path => {
        const file = currentFiles.find(f => f.path === path);
        if (file) totalSize += file.size;
    });
    document.getElementById("selected-size").textContent = humanSize(totalSize);

    document.getElementById("btn-delete").disabled = count === 0;
}

function humanSize(bytes) {
    const units = ["B", "KB", "MB", "GB", "TB"];
    let i = 0;
    while (bytes >= 1024 && i < units.length - 1) {
        bytes /= 1024;
        i++;
    }
    return `${bytes.toFixed(1)} ${units[i]}`;
}

// ==================== 文件详情 ====================

function renderNfoCard(p, nfoPath) {
    // 把 _parse_emby_nfo 返回的对象渲染成 Bootstrap 卡片。
    // nfoPath 可选——传了就在卡片头显示一行 sidecar 文件名（用于 sidecar 自动发现路径）。
    const card = createElement("div", { className: "card border-info mb-2" });
    const body = createElement("div", { className: "card-body p-2" });

    if (nfoPath) {
        const sideBadge = createElement("div", {
            className: "small text-secondary mb-1",
            textContent: `📋 ${nfoPath.split("/").pop()}`,
        });
        body.appendChild(sideBadge);
    }

    const typeLabel = { episodedetails: "剧集", tvshow: "剧", movie: "电影" }[p.type] || p.type;

    // 主标题：剧名（episodedetails 用 show_title，否则用 title）+ 季集编号
    const primaryBits = [];
    const showName = p.show_title || (p.type !== "episodedetails" ? p.title : null);
    if (showName) primaryBits.push(showName);
    const showOrig = p.show_original_title || (p.type !== "episodedetails" ? p.original_title : null);
    if (showOrig && showOrig !== showName) primaryBits.push(`(${showOrig})`);
    if (p.season && p.episode) {
        primaryBits.push(`S${String(p.season).padStart(2, "0")}E${String(p.episode).padStart(2, "0")}`);
    }
    const header = createElement("h6", {
        className: "card-title mb-1",
        textContent: primaryBits.join(" ")
    });
    const typeBadge = createElement("span", {
        className: "badge bg-info me-2",
        textContent: typeLabel
    });
    header.insertBefore(typeBadge, header.firstChild);
    body.appendChild(header);

    // 副标题：本集标题（episodedetails 才显示）。如果 title 跟自动编号一致就省略
    if (p.type === "episodedetails" && p.title) {
        const generic = `第 ${p.episode} 集`;
        if (p.title !== generic) {
            const sub = createElement("div", {
                className: "text-secondary small mb-2",
                textContent: p.episode ? `${generic} · ${p.title}` : p.title
            });
            body.appendChild(sub);
        }
    }

    if (p.plot) {
        body.appendChild(createElement("p", {
            className: "mb-2 small",
            textContent: p.plot
        }));
    }

    // 元数据：年份/播出/时长/评分/imdb 等
    const metaPairs = [];
    if (p.year) metaPairs.push(["年份", p.year]);
    if (p.aired) metaPairs.push(["播出", p.aired]);
    if (p.premiered && p.premiered !== p.aired) metaPairs.push(["首播", p.premiered]);
    if (p.runtime) metaPairs.push(["时长", `${p.runtime} 分钟`]);
    if (p.rating) metaPairs.push(["评分", p.rating]);
    if (p.genres && p.genres.length) metaPairs.push(["类型", p.genres.join(" / ")]);
    if (p.studios && p.studios.length) metaPairs.push(["出品", p.studios.join(" / ")]);
    if (p.directors && p.directors.length) metaPairs.push(["导演", p.directors.join(" / ")]);
    if (p.imdb_id) metaPairs.push(["IMDb", p.imdb_id]);
    if (p.tmdb_id) metaPairs.push(["TMDB", p.tmdb_id]);
    if (p.added) metaPairs.push(["入库时间", p.added]);

    if (metaPairs.length > 0) {
        const dl = createElement("dl", {
            className: "row mb-2 small"
        });
        dl.style.gap = "0.1rem 0";
        metaPairs.forEach(([k, v]) => {
            const dt = createElement("dt", {
                className: "col-4 col-md-3 text-secondary fw-normal mb-0",
                textContent: k
            });
            const dd = createElement("dd", {
                className: "col-8 col-md-9 mb-0",
                textContent: v
            });
            dl.appendChild(dt);
            dl.appendChild(dd);
        });
        body.appendChild(dl);
    }

    if (p.actors && p.actors.length > 0) {
        const actorH = createElement("div", {
            className: "small text-secondary mb-1",
            textContent: "演员"
        });
        body.appendChild(actorH);
        const actorList = createElement("div", { className: "small" });
        p.actors.forEach(a => {
            const item = createElement("div");
            item.textContent = a.role ? `${a.name} 饰 ${a.role}` : a.name;
            actorList.appendChild(item);
        });
        body.appendChild(actorList);
    }

    card.appendChild(body);
    return card;
}


async function configureDeepseekKeyPrompt() {
    const key = window.prompt(
        "粘贴 DeepSeek API key（sk-...）\n\n申请：https://platform.deepseek.com/api_keys\n（注册账号 → API Keys → 创建）\n\n用于：中文剧名识别 + 模糊匹配。比 OpenAI / Claude 便宜数倍，个人 PT 库扫描成本忽略不计。"
    );
    if (!key || !key.trim()) return false;
    try {
        const res = await apiFetch(`${API_BASE}/api/config/deepseek`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_key: key.trim() })
        });
        const data = await res.json();
        if (!data.ok) {
            alert("保存失败：" + (data.error || "unknown"));
            return false;
        }
        const tr = await apiFetch(`${API_BASE}/api/config/deepseek/test`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({})
        });
        const td = await tr.json();
        if (!td.ok) {
            alert("DeepSeek 连接失败：" + td.message);
            return false;
        }
        return true;
    } catch (err) {
        alert("出错：" + err.message);
        return false;
    }
}


async function configureTmdbKeyPrompt() {
    const key = window.prompt(
        "粘贴 TMDB API key（v3，~32 字符）\n\n免费申请：https://www.themoviedb.org/settings/api\n（注册账号 → 申请 Developer key → 复制 API Key (v3 auth)）"
    );
    if (!key || !key.trim()) return false;
    try {
        const res = await apiFetch(`${API_BASE}/api/config/tmdb`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_key: key.trim() })
        });
        const data = await res.json();
        if (!data.ok) {
            alert("保存失败：" + (data.error || "unknown"));
            return false;
        }
        // 顺手测一下
        const tr = await apiFetch(`${API_BASE}/api/config/tmdb/test`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({})
        });
        const td = await tr.json();
        if (!td.ok) {
            alert("TMDB 连接失败：" + td.message);
            return false;
        }
        return true;
    } catch (err) {
        alert("出错：" + err.message);
        return false;
    }
}

function renderMetadataCard(container, data) {
    container.innerHTML = "";
    // provider 未配置 → 提示用户去设置页填 TMDB key
    if (data.provider_state === "not_configured") {
        const warn = createElement("div", {
            className: "alert alert-warning py-2 small mb-2",
            innerHTML: 'TMDB API key 未配置。<a href="#" id="open-tmdb-config">点这里配置</a>（免费，1 分钟）。'
        });
        warn.querySelector("#open-tmdb-config").addEventListener("click", async (e) => {
            e.preventDefault();
            const ok = await configureTmdbKeyPrompt();
            if (ok) {
                warn.innerHTML = '<small class="text-success">已保存。再点一次 "AI 识别" 按钮试试。</small>';
            }
        });
        container.appendChild(warn);
        // 仍然显示文件名解析（guessit 结果）让用户看 parser 至少识别到了什么
        if (data.parse?.title) {
            const parseInfo = createElement("small", {
                className: "text-secondary d-block",
                textContent: `解析：${data.parse.title}${data.parse.year ? " ("+data.parse.year+")" : ""}${data.parse.season ? " S"+String(data.parse.season).padStart(2,"0") : ""}${data.parse.episode ? "E"+String(data.parse.episode).padStart(2,"0") : ""}`
            });
            container.appendChild(parseInfo);
        }
        return;
    }

    const top = data.top_pick;
    // 没绑定到 top_pick → 显示候选列表让用户挑（spike 阶段不入库，只展示）
    if (!top) {
        const banner = createElement("div", {
            className: "alert alert-warning py-2 small mb-2",
            textContent: `未自动匹配（${data.reasoning || "no candidate"}）。下面是候选：`
        });
        container.appendChild(banner);

        // hint：始终展示配置入口（指向顶部 AI 按钮），即使候选 0 也给用户出路
        if (!data.llm_configured) {
            const text = (data.candidates && data.candidates.length > 0)
                ? '🤖 候选有，但 confidence 太低自动跳过。配 DeepSeek 让 AI 帮选 → 顶部 <strong>AI</strong> 按钮'
                : '⚠️ TMDB 没找到候选。可能文件名无法解析。手动到 <a href="https://www.themoviedb.org/" target="_blank">themoviedb.org</a> 搜索确认，或配 DeepSeek 让 AI 试更模糊的搜索 → 顶部 <strong>AI</strong> 按钮';
            container.appendChild(createElement("div", {
                className: "alert alert-info py-2 small mb-2",
                innerHTML: text,
            }));
        }
        if (!data.candidates || data.candidates.length === 0) {
            container.appendChild(createElement("p", {
                className: "text-secondary small", textContent: "TMDB 也没找到匹配。"
            }));
            return;
        }
        const list = createElement("div", { className: "list-group small" });
        data.candidates.forEach(c => {
            const item = createElement("div", {
                className: "list-group-item list-group-item-action bg-transparent text-light border-secondary py-2"
            });
            const poster = c.poster_url
                ? `<img src="${c.poster_url}" style="width:50px;height:auto;flex-shrink:0;border-radius:3px;margin-right:8px"/>`
                : `<div style="width:50px;height:75px;flex-shrink:0;margin-right:8px;background:#222;border-radius:3px"></div>`;
            item.innerHTML = `
                <div class="d-flex">
                    ${poster}
                    <div class="flex-grow-1">
                        <div class="d-flex justify-content-between">
                            <strong>${c.title}${c.original_title && c.original_title !== c.title ? ` <small class="text-secondary">(${c.original_title})</small>` : ""}</strong>
                            <small class="text-secondary">${c.year || "?"} · ${c.media_type} · ⭐${c.vote_average?.toFixed(1) || "—"}</small>
                        </div>
                        ${c.overview ? `<small class="text-secondary d-block mt-1" style="line-height:1.3">${c.overview.slice(0, 200)}</small>` : ""}
                    </div>
                </div>
            `;
            list.appendChild(item);
        });
        container.appendChild(list);
        return;
    }

    // 主卡片：海报 + 标题 + 评分 + 剧情
    const card = createElement("div", { className: "d-flex gap-2 mb-2" });
    if (top.poster_url) {
        const img = createElement("img", {
            src: top.poster_url, className: "rounded"
        });
        Object.assign(img.style, { width: "90px", height: "auto", flexShrink: "0" });
        card.appendChild(img);
    }
    const text = createElement("div");
    const titleLine = `<strong>${top.title}</strong>` +
        (top.original_title && top.original_title !== top.title ? ` <small class="text-secondary">(${top.original_title})</small>` : "");
    const pickLabel = {
        single_exact: "单候选",
        heuristic: "启发式",
        llm: "🤖 AI",
        needs_review: "待复核"
    }[data.pick_source] || data.pick_source || "?";
    const metaLine = [
        top.year,
        top.media_type === "tv" ? "剧集" : "电影",
        `⭐ ${top.vote_average?.toFixed(1) || "—"}`,
        `置信度 ${(data.confidence * 100).toFixed(0)}% (${pickLabel})`
    ].filter(Boolean).join(" · ");

    text.innerHTML = `
        <div>${titleLine}</div>
        <small class="text-secondary d-block mb-1">${metaLine}</small>
    `;
    if (top.overview) {
        text.innerHTML += `<small class="d-block" style="line-height:1.4">${top.overview}</small>`;
    }
    card.appendChild(text);
    container.appendChild(card);

    // tv 剧 + 拿到了 episode 详情 → 显示单集卡
    if (data.details?.episode) {
        const ep = data.details.episode;
        const epCard = createElement("div", { className: "border-top pt-2 mt-2 small" });
        epCard.innerHTML = `
            <strong>S${String(ep.season_number).padStart(2,"0")}E${String(ep.episode_number).padStart(2,"0")} · ${ep.name || "—"}</strong>
            ${ep.air_date ? ` <small class="text-secondary">${ep.air_date}</small>` : ""}
            ${ep.overview ? `<div class="text-secondary mt-1" style="line-height:1.4">${ep.overview}</div>` : ""}
        `;
        container.appendChild(epCard);
    }

    // 演员 / 类型
    if (data.details) {
        if (data.details.genres?.length) {
            container.appendChild(createElement("small", {
                className: "text-secondary d-block mt-2",
                textContent: "类型：" + data.details.genres.join(" / ")
            }));
        }
        if (data.details.cast?.length) {
            container.appendChild(createElement("small", {
                className: "text-secondary d-block",
                textContent: "演员：" + data.details.cast.slice(0, 6).join(" · ")
            }));
        }
    }

    // 即使匹配上了，LLM 未配置时给个小提示——指向顶部 AI 按钮
    if (!data.llm_configured && data.pick_source !== "llm") {
        container.appendChild(createElement("small", {
            className: "d-block mt-2 text-info",
            innerHTML: '<i class="bi bi-lightbulb me-1"></i>配 DeepSeek 让 AI 帮选（中文 / 模糊命名都搞得定）→ 顶部 <strong>AI</strong> 按钮'
        }));
    }

    // 写 NFO 按钮（置信度 ≥ 0.7 才出）
    if (top && data.confidence >= 0.7 && container.__videoPath) {
        const writeBtnWrap = createElement("div", { className: "mt-2" });
        const writeBtn = createElement("button", {
            className: "btn btn-sm btn-outline-info",
            innerHTML: '<i class="bi bi-file-earmark-text me-1"></i>写入 NFO 元数据',
        });
        writeBtn.addEventListener("click", () => {
            const item = {
                video_path: container.__videoPath,
                tmdb_id: top.external_ids?.tmdb_id || top.id,
                media_type: top.media_type,
                title: top.title,
                original_title: top.original_title,
                season: data.parse?.season,
                episode: data.parse?.episode,
            };
            openNfoWritePreview([item]);
        });
        writeBtnWrap.appendChild(writeBtn);
        container.appendChild(writeBtnWrap);
    }
}


async function showDetail(path) {
    const file = currentFiles.find(f => f.path === path);
    if (!file) return;

    const panel = document.getElementById("file-detail");
    const content = document.getElementById("file-detail-content");
    content.innerHTML = "";

    // 文件信息表格
    const h6 = createElement("h6", { textContent: file.name });
    content.appendChild(h6);

    const table = createElement("table", { className: "table table-sm table-dark mb-2" });
    const rows = [
        ["路径", file.path],
        ["大小", file.size_human],
        ["修改时间", file.modified],
        ["权限", file.permissions],
        ["所有者", `${file.owner}:${file.group}`],
        ["硬链接数", file.hardlinks.toString()]
    ];

    rows.forEach(([label, value]) => {
        const tr = createElement("tr");
        tr.appendChild(createElement("td", { textContent: label }));
        const valueTd = createElement("td");
        if (label === "权限") {
            valueTd.appendChild(createElement("code", { textContent: value }));
        } else {
            valueTd.textContent = value;
        }
        tr.appendChild(valueTd);
        table.appendChild(tr);
    });
    content.appendChild(table);

    // === 元数据区（视频文件） ===
    // 流程：(1) 先查 sidecar .nfo 是否存在 → 存在直接展示已有 metadata
    //       (2) 提供"AI 识别"按钮（无 NFO 时叫"AI 识别", 有 NFO 时叫"重新识别（覆盖）"）
    const VIDEO_EXTS = new Set(["mkv","mp4","avi","mov","ts","m4v","mpg","wmv","flv","webm","m2ts","rmvb"]);
    const fileExt = (file.name.split(".").pop() || "").toLowerCase();
    if (!file.is_dir && VIDEO_EXTS.has(fileExt)) {
        const aiSection = createElement("div", { className: "mb-3" });
        const nfoCard = createElement("div", { className: "mb-2" });           // sidecar .nfo 卡片
        const cachedCard = createElement("div", { className: "mb-2" });        // DB cache 卡片
        const aiBtn = createElement("button", {
            className: "btn btn-sm btn-outline-info",
            innerHTML: '<span class="spinner-border spinner-border-sm me-1"></span>检查缓存...',
            disabled: true,
        });
        const aiResult = createElement("div", { className: "mt-2" });
        aiResult.__videoPath = file.path;

        // 并行：(1) 查 DB cache (2) 查 sidecar .nfo
        // 两者都可能命中（cache 来自 TMDB；NFO 来自 sidecar 文件，可能不同步）。
        // 都展示给用户看，按钮 label 反映"有 cache"优先信号。
        const cachePromise = apiFetch(`${API_BASE}/api/metadata/cached?path=${encodeURIComponent(file.path)}`)
            .then(r => r.json())
            .catch(() => ({ cached: false }));
        const nfoPromise = apiFetch(`${API_BASE}/api/metadata/from-nfo?video_path=${encodeURIComponent(file.path)}`)
            .then(r => r.json())
            .catch(() => ({ has_nfo: false }));

        Promise.all([cachePromise, nfoPromise]).then(([cacheData, nfoData]) => {
            let labelSuffix = "";
            if (cacheData.cached) {
                renderMetadataCard(cachedCard, cacheData);
                labelSuffix = "（覆盖缓存）";
            } else if (cacheData.stale) {
                cachedCard.innerHTML = '<small class="text-warning">⚠ 缓存已过期（文件 mtime 变了），重新识别推荐</small>';
            }
            if (nfoData.has_nfo && nfoData.parsed) {
                nfoCard.innerHTML = "";
                nfoCard.appendChild(renderNfoCard(nfoData.parsed, nfoData.nfo_path));
                labelSuffix = labelSuffix || "（覆盖 NFO）";
            } else if (nfoData.has_nfo && !nfoData.parsed) {
                nfoCard.innerHTML = '<small class="text-warning">⚠ 同目录有 .nfo 但解析失败</small>';
            }
            if (labelSuffix) {
                aiBtn.innerHTML = `<i class="bi bi-arrow-clockwise me-1"></i>重新识别${labelSuffix}`;
            } else {
                aiBtn.innerHTML = '<i class="bi bi-stars me-1"></i>AI 识别（TMDB）';
            }
        }).finally(() => {
            aiBtn.disabled = false;
        });

        aiBtn.addEventListener("click", async () => {
            const origLabel = aiBtn.innerHTML;
            aiBtn.disabled = true;
            aiBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>识别中...';
            aiResult.innerHTML = "";
            try {
                const res = await apiFetch(`${API_BASE}/api/metadata/identify`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ path: file.path })
                });
                const data = await res.json();
                renderMetadataCard(aiResult, data);
                // 写入成功 → 旧的 cache 卡片用新数据替换（让用户看到刚 cache 的）
                cachedCard.innerHTML = "";
            } catch (err) {
                aiResult.innerHTML = "";
                aiResult.appendChild(createElement("p", {
                    className: "text-danger small", textContent: `识别失败：${err.message}`
                }));
            } finally {
                aiBtn.disabled = false;
                aiBtn.innerHTML = origLabel;
            }
        });
        aiSection.appendChild(cachedCard);
        aiSection.appendChild(nfoCard);
        aiSection.appendChild(aiBtn);
        aiSection.appendChild(aiResult);
        content.appendChild(aiSection);
    }

    // 文本文件预览（.nfo / .srt / .log / .ass 等）
    const TEXT_EXTS = new Set([
        "nfo", "txt", "log", "md", "readme",
        "srt", "ass", "ssa", "sub", "idx", "vtt",
        "json", "yml", "yaml", "ini", "conf", "cfg",
        "sh", "py", "js", "html", "xml", "csv"
    ]);
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    if (!file.is_dir && TEXT_EXTS.has(ext)) {
        const previewH6 = createElement("h6", {
            className: "mt-3", textContent: "内容预览"
        });
        content.appendChild(previewH6);

        const previewMeta = createElement("small", {
            className: "text-secondary d-block mb-1",
            textContent: "加载中..."
        });
        content.appendChild(previewMeta);

        const previewPre = createElement("pre", {
            className: "border rounded p-2 mb-2"
        });
        Object.assign(previewPre.style, {
            background: "#1e1e1e",
            color: "#e0e0e0",
            maxHeight: "400px",
            overflow: "auto",
            fontSize: "0.78rem",
            lineHeight: "1.3",
            whiteSpace: "pre"
        });
        content.appendChild(previewPre);

        // 异步加载（不阻塞 panel 显示和硬链接查询）
        apiFetch(`${API_BASE}/api/file-content?path=${encodeURIComponent(path)}`)
            .then(res => res.json())
            .then(data => {
                if (data.error) {
                    previewMeta.textContent = `预览失败: ${data.error}`;
                    previewMeta.className = "text-danger d-block mb-1";
                    previewPre.remove();
                    return;
                }
                const truncMsg = data.truncated ? " · 已截断（仅前 256 KB）" : "";
                previewMeta.textContent = `编码: ${data.encoding} · ${humanSize(data.size)}${truncMsg}`;

                // 如果是 Emby/Jellyfin .nfo 且解析成功 → 结构化卡片 + 折叠 raw
                if (data.parsed) {
                    const card = renderNfoCard(data.parsed);
                    previewPre.parentNode.insertBefore(card, previewPre);

                    // raw text 放折叠区
                    const details = createElement("details", { className: "mb-2" });
                    const summary = createElement("summary", {
                        className: "text-secondary small",
                        textContent: "查看原始 XML"
                    });
                    summary.style.cursor = "pointer";
                    details.appendChild(summary);
                    previewPre.parentNode.insertBefore(details, previewPre);
                    details.appendChild(previewPre);
                }
                previewPre.textContent = data.text;
            })
            .catch(err => {
                previewMeta.textContent = `预览失败: ${err.message}`;
                previewMeta.className = "text-danger d-block mb-1";
                previewPre.remove();
            });
    }

    // 如果是硬链接，显示关联文件
    if (file.hardlinks > 1 && file.inode > 0) {
        const h6Links = createElement("h6", { className: "mt-3", textContent: "硬链接关联" });
        content.appendChild(h6Links);

        const loadingDiv = createElement("div", {
            className: "hardlink-target",
            textContent: "正在查找关联文件..."
        });
        content.appendChild(loadingDiv);

        panel.style.display = "block";

        try {
            const res = await apiFetch(`${API_BASE}/api/inode/${file.inode}`);
            const data = await res.json();

            if (data.paths && data.paths.length > 1) {
                loadingDiv.remove();
                data.paths
                    .filter(p => p !== path)
                    .forEach(p => {
                        const linkDiv = createElement("div", {
                            className: "hardlink-target mb-1"
                        });
                        const small = createElement("small", { textContent: p });
                        linkDiv.appendChild(small);
                        content.appendChild(linkDiv);
                    });
            } else {
                loadingDiv.textContent = "无其他链接";
            }
        } catch (err) {
            loadingDiv.textContent = "获取失败";
        }
    } else {
        panel.style.display = "block";
    }
}

// ==================== 硬链接检测 ====================

async function showHardlinks() {
    const panel = document.getElementById("hardlink-panel");
    const content = document.getElementById("hardlink-content");

    panel.style.display = "block";
    content.innerHTML = "";

    const loadingDiv = createElement("div", { className: "text-center py-3" });
    const spinner = createElement("div", {
        className: "spinner-border spinner-border-sm text-primary",
        role: "status"
    });
    loadingDiv.appendChild(spinner);
    loadingDiv.appendChild(createElement("span", {
        className: "ms-2",
        textContent: "正在扫描硬链接..."
    }));
    content.appendChild(loadingDiv);

    try {
        const res = await apiFetch(`${API_BASE}/api/hardlinks?path=${encodeURIComponent(currentPath)}`);
        const data = await res.json();

        if (data.error) {
            content.innerHTML = "";
            content.appendChild(createElement("p", {
                className: "text-danger",
                textContent: data.error
            }));
            return;
        }

        content.innerHTML = "";

        if (data.hardlinks.length === 0) {
            const emptyDiv = createElement("div", { className: "text-center text-secondary py-3" });
            emptyDiv.innerHTML = '<i class="bi bi-check-circle" style="font-size: 2rem;"></i>';
            emptyDiv.appendChild(createElement("p", {
                className: "mt-2",
                textContent: "当前目录没有硬链接文件"
            }));
            content.appendChild(emptyDiv);
            return;
        }

        data.hardlinks.forEach(hl => {
            const card = createElement("div", { className: "card bg-dark mb-2" });
            const body = createElement("div", { className: "card-body py-2 px-3" });

            const header = createElement("div", {
                className: "d-flex justify-content-between align-items-start"
            });

            const infoDiv = createElement("div", { className: "text-truncate me-2" });
            infoDiv.appendChild(createElement("small", {
                className: "text-primary",
                textContent: hl.path.split("/").pop()
            }));
            infoDiv.appendChild(createElement("br"));
            infoDiv.appendChild(createElement("small", {
                className: "text-secondary",
                textContent: `${hl.size_human} · ${hl.hardlinks} 个链接`
            }));

            const deleteBtn = createElement("button", {
                className: "btn btn-sm btn-outline-danger"
            });
            deleteBtn.innerHTML = '<i class="bi bi-trash"></i>';
            deleteBtn.addEventListener("click", () => deleteSingle(hl.path));

            header.appendChild(infoDiv);
            header.appendChild(deleteBtn);
            body.appendChild(header);

            if (hl.targets.length > 0) {
                const targetsDiv = createElement("div", { className: "mt-2" });
                targetsDiv.appendChild(createElement("small", {
                    className: "text-secondary",
                    textContent: "链接到："
                }));

                hl.targets.forEach(t => {
                    const targetDiv = createElement("div", {
                        className: "hardlink-target mt-1"
                    });
                    targetDiv.appendChild(createElement("small", { textContent: t }));
                    targetsDiv.appendChild(targetDiv);
                });

                body.appendChild(targetsDiv);
            }

            card.appendChild(body);
            content.appendChild(card);
        });

    } catch (err) {
        content.innerHTML = "";
        content.appendChild(createElement("p", {
            className: "text-danger",
            textContent: `加载失败: ${err.message}`
        }));
    }
}

function hideHardlinks() {
    document.getElementById("hardlink-panel").style.display = "none";
}

// ==================== 删除操作 ====================

let deletePreviewData = null;

function deleteSingle(path) {
    selectedFiles.clear();
    selectedFiles.add(path);
    showDeleteConfirm();
}

function deleteSelected() {
    if (selectedFiles.size === 0) return;
    showDeleteConfirm();
}

async function showDeleteConfirm() {
    const loading = document.getElementById("delete-loading");
    const previewContent = document.getElementById("delete-preview-content");
    const confirmBtn = document.getElementById("btn-confirm-delete");

    // 显示弹窗 + loading
    loading.style.display = "block";
    previewContent.style.display = "none";
    confirmBtn.disabled = true;
    deleteModal.show();

    try {
        // 调用预览 API
        const res = await apiFetch(`${API_BASE}/api/delete-preview`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ files: Array.from(selectedFiles) })
        });
        deletePreviewData = await res.json();

        if (deletePreviewData.error) {
            loading.style.display = "none";
            previewContent.style.display = "block";
            document.getElementById("delete-file-list").innerHTML = "";
            document.getElementById("delete-file-list").appendChild(
                createElement("p", { className: "text-danger", textContent: deletePreviewData.error })
            );
            return;
        }

        renderDeletePreview(deletePreviewData);
    } catch (err) {
        loading.style.display = "none";
        previewContent.style.display = "block";
        document.getElementById("delete-file-list").innerHTML = "";
        document.getElementById("delete-file-list").appendChild(
            createElement("p", { className: "text-danger", textContent: `预览失败: ${err.message}` })
        );
    }
}

function renderDeletePreview(data) {
    const loading = document.getElementById("delete-loading");
    const previewContent = document.getElementById("delete-preview-content");
    const confirmBtn = document.getElementById("btn-confirm-delete");

    loading.style.display = "none";
    previewContent.style.display = "block";
    confirmBtn.disabled = false;

    // 选中的文件（用真实占用大小，目录递归算的）
    const fileList = document.getElementById("delete-file-list");
    fileList.innerHTML = "";
    data.files.forEach(file => {
        const row = createElement("div", {
            className: "d-flex justify-content-between align-items-center py-1"
        });
        const nameSpan = createElement("span", { className: "text-truncate me-2" });
        nameSpan.appendChild(createElement("i", {
            className: file.is_dir ? "bi bi-folder-fill text-warning me-1" : "bi bi-file-earmark me-1"
        }));
        nameSpan.appendChild(document.createTextNode(file.path.split("/").pop()));
        row.appendChild(nameSpan);
        row.appendChild(createElement("span", {
            className: "text-secondary text-nowrap",
            textContent: file.size_human
        }));
        fileList.appendChild(row);
    });

    // 总大小（顶部标题旁）
    const totalSizeEl = document.getElementById("delete-total-size");
    if (totalSizeEl) {
        totalSizeEl.textContent = data.total_size_human
            ? ` · 总计 ${data.total_size_human}`
            : "";
    }

    // 硬链接
    const hlSection = document.getElementById("hardlink-section");
    const hlList = document.getElementById("hardlink-list");
    hlList.innerHTML = "";

    const allHardlinks = data.files.flatMap(f => f.hardlink_paths);
    if (allHardlinks.length > 0) {
        hlSection.style.display = "block";
        allHardlinks.forEach(p => {
            const row = createElement("div", { className: "py-1" });
            row.appendChild(createElement("small", {
                className: "text-secondary",
                textContent: p
            }));
            hlList.appendChild(row);
        });
    } else {
        hlSection.style.display = "none";
    }

    // PT 种子
    const torrentSection = document.getElementById("torrent-section");
    const torrentList = document.getElementById("torrent-list");
    torrentList.innerHTML = "";

    if (data.torrents && data.torrents.length > 0) {
        torrentSection.style.display = "block";
        const heading = torrentSection.querySelector("h6");
        if (heading) {
            heading.innerHTML = '<i class="bi bi-cloud-arrow-down me-1" style="color:var(--blue);"></i>关联 PT 种子（含下载文件）';
        }
        data.torrents.forEach(t => {
            const row = createElement("div", {
                className: "d-flex justify-content-between align-items-center py-1"
            });
            const nameSpan = createElement("span", { className: "text-truncate me-2" });
            nameSpan.appendChild(createElement("i", { className: "bi bi-cloud-arrow-down me-1" }));
            nameSpan.appendChild(document.createTextNode(t.name));
            row.appendChild(nameSpan);

            const infoSpan = createElement("span", { className: "text-secondary text-nowrap" });
            const progress = Math.round(t.progress * 100);
            infoSpan.textContent = `${t.size_human} · ${progress}%`;
            row.appendChild(infoSpan);

            torrentList.appendChild(row);
        });
    } else {
        // 即使没匹配到种子，也展示原因（qBit 未连 / 文件无硬链接 / 单纯没匹配上）
        torrentSection.style.display = "block";
        const heading = torrentSection.querySelector("h6");
        const qbitStatus = data.qbit_status || { ok: true };
        const hl = data.hardlink_summary || {};

        let iconHtml, title, hint;
        if (!qbitStatus.ok) {
            iconHtml = '<i class="bi bi-plug me-1" style="color:var(--amber);"></i>';
            title = "未连接到 qBittorrent";
            hint = `${qbitStatus.message || "请在 qBit 设置里填写密码并测试连接"} — 无法检测关联种子。`;
        } else if (hl.all_independent) {
            iconHtml = '<i class="bi bi-info-circle me-1" style="color:var(--text-3);"></i>';
            title = "未找到关联 PT 种子";
            hint = `已检查 ${hl.checked_files} 个文件，全部为独立拷贝（无硬链接）；qBit 中也没有路径相关的种子。`;
        } else if (hl.with_hardlinks > 0) {
            iconHtml = '<i class="bi bi-info-circle me-1" style="color:var(--text-3);"></i>';
            title = "未找到关联 PT 种子";
            hint = `检测到 ${hl.with_hardlinks}/${hl.checked_files} 个文件存在跨目录硬链接，但 qBit 里没有指向这些路径的种子。`;
        } else {
            iconHtml = '<i class="bi bi-info-circle me-1" style="color:var(--text-3);"></i>';
            title = "未找到关联 PT 种子";
            hint = "qBit 中没有路径相关的种子。";
        }
        if (heading) heading.innerHTML = iconHtml + title;
        torrentList.innerHTML = "";
        const msgRow = createElement("div", { className: "py-1" });
        msgRow.appendChild(createElement("small", {
            className: "text-secondary",
            textContent: hint,
        }));
        torrentList.appendChild(msgRow);
    }

    // 将执行的 SSH 命令清单
    const cmdSection = document.getElementById("commands-section");
    const cmdList = document.getElementById("commands-list");
    if (data.commands && data.commands.length > 0) {
        cmdSection.style.display = "block";
        cmdList.textContent = data.commands.join("\n");
    } else {
        cmdSection.style.display = "none";
        cmdList.textContent = "";
    }
}

async function confirmDelete() {
    const btn = document.getElementById("btn-confirm-delete");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 删除中...';

    // 契约 #1：必须用 preview 拿到的 action_id + signed_token 才能 confirm。
    // 没有 token 说明用户绕过了 preview UI（不应该发生）；早早 fail-loud。
    if (!deletePreviewData || !deletePreviewData.action_id || !deletePreviewData.signed_token) {
        addLog("删除失败：缺少 preview token，请重新打开删除窗口", "danger");
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-trash"></i> 确认删除';
        return;
    }

    try {
        const res = await apiFetch(`${API_BASE}/api/action/confirm`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                action_id: deletePreviewData.action_id,
                signed_token: deletePreviewData.signed_token,
            })
        });

        const data = await res.json();

        // 三态：succeeded / target_already_changed / 其他 failure
        if (data.status === "target_already_changed") {
            addLog(`目标已变化，请刷新后重试：${data.hint || ""}`, "warning");
            return;
        }

        if (data.status !== "succeeded") {
            addLog(`删除失败: ${data.error || data.detail || "unknown"}`, "danger");
            return;
        }

        const result = data.result || {};

        if (result.total_files_deleted > 0) {
            addLog(`已删除 ${result.total_files_deleted} 个文件`, "success");
        }
        if (result.total_files_already_gone > 0) {
            // already_gone = preview 后文件已不在；要么 qBit 顺手删了，要么有其他进程动过。
            // 区分显示让用户知道"我们没真去删它，但它现在没了"
            addLog(`${result.total_files_already_gone} 个文件已不存在（已被其他途径处理）`, "warning");
        }

        if (Array.isArray(result.torrent_results)) {
            result.torrent_results.forEach(t => {
                if (t.status === "deleted") {
                    addLog(`已删除种子: ${t.name}`, "success");
                } else if (t.status === "error") {
                    addLog(`种子删除失败: ${t.message}`, "danger");
                }
            });
        }

        if (result.space_freed > 0) {
            addLog(`释放空间: ${result.space_freed_human}`, "info");
        }

    } catch (err) {
        addLog(`删除操作失败: ${err.message}`, "danger");
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-trash"></i> 确认删除';
        deleteModal.hide();
        deletePreviewData = null;
        // 所有路径都刷新 — 成功 / 已变化 / 失败都可能让本地视图与 NAS 状态错位
        try { await loadFiles(currentPath); } catch {}
        refreshDisk();
    }
}

// ==================== 操作日志 ====================

function addLog(message, type = "info") {
    const log = document.getElementById("action-log");
    const time = new Date().toLocaleTimeString();

    // 移除"暂无操作记录"
    const empty = log.querySelector(".text-center");
    if (empty) empty.remove();

    const icons = {
        success: "bi-check-circle text-success",
        danger: "bi-x-circle text-danger",
        warning: "bi-exclamation-triangle text-warning",
        info: "bi-info-circle text-info"
    };

    const item = createElement("div", {
        className: "list-group-item bg-transparent border-secondary py-2"
    });

    const wrapper = createElement("div", { className: "d-flex align-items-start" });
    wrapper.appendChild(createElement("i", {
        className: `bi ${icons[type]} me-2 mt-1`
    }));

    const contentDiv = createElement("div", { className: "flex-grow-1" });
    contentDiv.appendChild(createElement("small", {
        className: "text-secondary",
        textContent: time
    }));
    contentDiv.appendChild(createElement("div", { textContent: message }));

    wrapper.appendChild(contentDiv);
    item.appendChild(wrapper);

    log.insertBefore(item, log.firstChild);

    // 只保留最近 50 条
    while (log.children.length > 50) {
        log.removeChild(log.lastChild);
    }
}

// ==================== 工具函数 ====================

function showError(message) {
    const tbody = document.getElementById("file-list");
    tbody.innerHTML = "";

    const tr = createElement("tr");
    const td = createElement("td", {
        colspan: "6",
        className: "text-center text-danger py-4"
    });

    td.innerHTML = '<i class="bi bi-exclamation-triangle" style="font-size: 2rem;"></i>';
    td.appendChild(createElement("p", { className: "mt-2", textContent: message }));

    const retryBtn = createElement("button", {
        className: "btn btn-sm btn-outline-primary",
        textContent: "重试"
    });
    retryBtn.innerHTML = '<i class="bi bi-arrow-clockwise"></i> 重试';
    retryBtn.addEventListener("click", () => loadFiles(currentPath));
    td.appendChild(retryBtn);

    tr.appendChild(td);
    tbody.appendChild(tr);
}

function refresh() {
    loadFiles(currentPath);
}

// ==================== 批量识别 ====================

let batchIdentifyModal = null;
let batchAborted = false;
let batchInProgress = false;
// 批量 identify 结果缓存：idx → identify response（含 top_pick + parse），供"批量写入 NFO"采集

async function openBatchIdentify() {
    if (!batchIdentifyModal) {
        batchIdentifyModal = new bootstrap.Modal(document.getElementById("batchIdentifyModal"));
    }
    // 重置 UI
    document.getElementById("batch-summary").innerHTML = '<span class="text-secondary">扫描视频文件中...</span>';
    document.getElementById("batch-progress-bar").style.width = "0%";
    document.getElementById("batch-progress-text").textContent = "0 / 0";
    document.getElementById("batch-result-tbody").innerHTML = "";
    document.getElementById("batch-start-btn").disabled = true;
    document.getElementById("batch-stop-btn").style.display = "none";
    document.getElementById("batch-nfo-write-btn").style.display = "none";
    batchIdentifyModal.show();

    // 列视频文件
    try {
        const url = `${API_BASE}/api/metadata/list-videos?path=${encodeURIComponent(currentPath)}&max_depth=2&limit=200`;
        const res = await apiFetch(url);
        const data = await res.json();
        const videos = data.videos || [];
        const truncated = data.truncated;

        if (videos.length === 0) {
            document.getElementById("batch-summary").innerHTML = '<span class="text-warning">该目录没有视频文件（mkv/mp4/...）。请进入有视频的目录后再试。</span>';
            return;
        }

        const skipNfoCount = videos.filter(v => v.has_nfo).length;
        const totalSize = videos.reduce((s, v) => s + v.size_bytes, 0);
        let summary = `找到 ${videos.length} 个视频文件 · 总计 ${humanSize(totalSize)}`;
        if (skipNfoCount > 0) summary += ` · ${skipNfoCount} 个已有 .nfo`;
        if (truncated) summary += ` · ⚠️ 超过 200 上限被截断（按子目录分批识别）`;
        document.getElementById("batch-summary").innerHTML = summary;

        // 渲染初始表（每行 status='pending'）
        const tbody = document.getElementById("batch-result-tbody");
        tbody.innerHTML = "";
        videos.forEach((v, idx) => {
            const tr = createElement("tr", { dataset: { idx: idx.toString() } });
            const tdPoster = createElement("td");
            tdPoster.innerHTML = `<div style="width:50px;height:75px;background:#222;border-radius:3px;"></div>`;
            tr.appendChild(tdPoster);

            const tdName = createElement("td");
            const nfoBadge = v.has_nfo ? '<span class="badge bg-secondary ms-1" style="font-size:9px;">NFO</span>' : '';
            tdName.innerHTML = `
                <div class="text-truncate" style="max-width:520px;">${v.name}${nfoBadge}</div>
                <small class="text-secondary" id="batch-result-${idx}">等待中…</small>
            `;
            tr.appendChild(tdName);

            tr.appendChild(createElement("td", {
                className: "small text-secondary",
                innerHTML: `<span id="batch-conf-${idx}">—</span>`
            }));
            tr.appendChild(createElement("td", {
                className: "small",
                innerHTML: `<span id="batch-source-${idx}" class="text-secondary">—</span>`
            }));

            tr.dataset.path = v.path;
            tr.dataset.hasNfo = v.has_nfo ? "1" : "0";
            tbody.appendChild(tr);
        });

        document.getElementById("batch-start-btn").disabled = false;
        document.getElementById("batch-progress-text").textContent = `0 / ${videos.length}`;
    } catch (err) {
        document.getElementById("batch-summary").innerHTML = `<span class="text-danger">扫描失败: ${err.message}</span>`;
    }
}


async function startBatchIdentify() {
    const tbody = document.getElementById("batch-result-tbody");
    const rows = Array.from(tbody.querySelectorAll("tr"));
    const skipNfo = document.getElementById("batch-skip-nfo").checked;

    batchAborted = false;
    batchInProgress = true;
    // reset 缓存
    rows.forEach(r => { delete r.__identifyData; });
    document.getElementById("batch-nfo-write-btn").style.display = "none";
    document.getElementById("batch-start-btn").style.display = "none";
    document.getElementById("batch-stop-btn").style.display = "inline-block";

    const total = rows.length;
    let done = 0;
    const updateProgress = () => {
        const pct = total > 0 ? (done / total * 100) : 0;
        document.getElementById("batch-progress-bar").style.width = pct + "%";
        document.getElementById("batch-progress-text").textContent = `${done} / ${total}`;
    };

    for (let i = 0; i < rows.length; i++) {
        if (batchAborted) break;
        const row = rows[i];
        const path = row.dataset.path;
        const hasNfo = row.dataset.hasNfo === "1";
        const idx = i;

        if (skipNfo && hasNfo) {
            document.getElementById(`batch-result-${idx}`).innerHTML = '<span class="text-secondary">已跳过（有 .nfo）</span>';
            done++;
            updateProgress();
            continue;
        }

        document.getElementById(`batch-result-${idx}`).innerHTML = '<span class="text-info"><span class="spinner-border spinner-border-sm me-1" style="width:10px;height:10px;border-width:1px;"></span>识别中…</span>';

        try {
            const res = await apiFetch(`${API_BASE}/api/metadata/identify`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ path })
            });
            const data = await res.json();
            row.__identifyData = data;
            renderBatchRow(idx, row, data);
        } catch (err) {
            document.getElementById(`batch-result-${idx}`).innerHTML = `<span class="text-danger">错误: ${err.message}</span>`;
        }
        done++;
        updateProgress();
    }

    batchInProgress = false;
    document.getElementById("batch-stop-btn").style.display = "none";
    document.getElementById("batch-start-btn").style.display = "inline-block";
    document.getElementById("batch-start-btn").innerHTML = '<i class="bi bi-arrow-clockwise"></i> 重新跑';

    // 统计可写 NFO 的行（confidence ≥ 0.7 且有 top_pick）
    const eligible = rows.filter(r => {
        const d = r.__identifyData;
        return d && d.top_pick && d.confidence >= 0.7;
    });
    if (eligible.length > 0 && !batchAborted) {
        document.getElementById("batch-nfo-eligible-count").textContent = eligible.length;
        document.getElementById("batch-nfo-write-btn").style.display = "inline-block";
    }

    addLog(`批量识别完成: ${done} 个文件`, batchAborted ? "warning" : "success");
}


function stopBatchIdentify() {
    batchAborted = true;
    document.getElementById("batch-stop-btn").disabled = true;
    document.getElementById("batch-stop-btn").innerHTML = '<i class="bi bi-stop-fill"></i> 停止中...';
}


// ==================== NFO 写入流程（contract #1 preview → confirm） ====================

let nfoWriteModal = null;
// 当前 preview 的状态：action_id + signed_token，confirm 阶段用
let nfoWritePending = null;

async function openBatchNfoWrite() {
    // 从 batch identify 结果里采集 eligible items
    const tbody = document.getElementById("batch-result-tbody");
    const rows = Array.from(tbody.querySelectorAll("tr"));
    const items = [];
    rows.forEach(r => {
        const d = r.__identifyData;
        if (!d || !d.top_pick || d.confidence < 0.7) return;
        items.push({
            video_path: r.dataset.path,
            tmdb_id: d.top_pick.external_ids?.tmdb_id || d.top_pick.id,
            media_type: d.top_pick.media_type,
            title: d.top_pick.title,
            original_title: d.top_pick.original_title,
            season: d.parse?.season,
            episode: d.parse?.episode,
        });
    });
    if (items.length === 0) {
        alert("没有可写入 NFO 的识别结果（需要 confidence ≥ 70%）");
        return;
    }
    await openNfoWritePreview(items);
}

async function openNfoWritePreview(items) {
    if (!nfoWriteModal) {
        nfoWriteModal = new bootstrap.Modal(document.getElementById("nfoWriteModal"));
    }
    // 重置 UI
    document.getElementById("nfo-write-summary").innerHTML = '<span class="text-secondary"><span class="spinner-border spinner-border-sm me-1"></span>正在生成预览...</span>';
    document.getElementById("nfo-write-tbody").innerHTML = "";
    document.getElementById("nfo-write-warning").style.display = "none";
    document.getElementById("nfo-write-result").style.display = "none";
    document.getElementById("nfo-write-confirm-btn").disabled = true;
    document.getElementById("nfo-write-confirm-btn").innerHTML = '<i class="bi bi-check-lg me-1"></i>确认写入';
    nfoWriteModal.show();

    try {
        const res = await apiFetch(`${API_BASE}/api/metadata/preview-nfo-write`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ items })
        });
        const data = await res.json();
        if (!res.ok) {
            document.getElementById("nfo-write-summary").innerHTML = `<span class="text-danger">生成预览失败: ${data.error || JSON.stringify(data)}</span>`;
            return;
        }
        renderNfoWritePreview(data);
    } catch (err) {
        document.getElementById("nfo-write-summary").innerHTML = `<span class="text-danger">网络错误: ${err.message}</span>`;
    }
}

function renderNfoWritePreview(data) {
    nfoWritePending = {
        action_id: data.action_id,
        signed_token: data.signed_token,
    };
    const p = data.preview;
    const tbody = document.getElementById("nfo-write-tbody");
    tbody.innerHTML = "";

    const summary = document.getElementById("nfo-write-summary");
    summary.innerHTML = `共 <strong>${p.total}</strong> 个文件 · 新建 <strong class="text-success">${p.to_create}</strong> · 覆盖 <strong class="text-warning">${p.to_overwrite}</strong>`;

    if (p.errors && p.errors.length > 0) {
        const warn = document.getElementById("nfo-write-warning");
        warn.innerHTML = `<strong>${p.errors.length} 个 item 准备时失败：</strong><ul class="mb-0 mt-1">` +
            p.errors.map(e => `<li>#${e.index}: ${e.reason}</li>`).join("") + "</ul>";
        warn.style.display = "block";
    }

    p.items.forEach(it => {
        const tr = createElement("tr");
        const tdPath = createElement("td", { className: "small" });
        const filename = it.nfo_path.split("/").pop();
        const parentPath = it.nfo_path.substring(0, it.nfo_path.length - filename.length - 1);
        tdPath.innerHTML = `<div class="text-truncate" style="max-width:480px;" title="${it.nfo_path}"><span class="text-secondary">${parentPath}/</span><strong>${filename}</strong></div>`;
        tr.appendChild(tdPath);

        const tdAction = createElement("td", { className: "small" });
        if (it.action === "create") {
            tdAction.innerHTML = '<span class="badge bg-success">新建</span>';
        } else {
            tdAction.innerHTML = '<span class="badge bg-warning text-dark" title="原文件会备份到 .nfo.bak">覆盖</span>';
        }
        tr.appendChild(tdAction);

        tr.appendChild(createElement("td", {
            className: "small text-secondary",
            textContent: humanSize(it.xml_size_bytes),
        }));
        tbody.appendChild(tr);
    });

    document.getElementById("nfo-write-confirm-btn").disabled = false;
}

async function confirmNfoWrite() {
    if (!nfoWritePending) return;
    const btn = document.getElementById("nfo-write-confirm-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>写入中...';

    try {
        const res = await apiFetch(`${API_BASE}/api/action/confirm`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(nfoWritePending),
        });
        const data = await res.json();
        const resultBox = document.getElementById("nfo-write-result");
        resultBox.style.display = "block";

        if (!res.ok) {
            resultBox.innerHTML = `<div class="alert alert-danger py-2 mb-0">写入失败: ${data.error || JSON.stringify(data)}</div>`;
            return;
        }
        const r = data.result || {};
        const counts = r.status_counts || {};
        const lines = [];
        if (r.total_created) lines.push(`<span class="text-success">✓ 新建 ${r.total_created}</span>`);
        if (r.total_overwrote) lines.push(`<span class="text-success">✓ 覆盖 ${r.total_overwrote}</span>`);
        if (r.total_skipped) lines.push(`<span class="text-warning">⚠ 跳过 ${r.total_skipped}</span>`);
        if (r.total_failed) lines.push(`<span class="text-danger">✗ 失败 ${r.total_failed}</span>`);
        resultBox.innerHTML = `<div class="alert alert-success py-2 mb-0">${lines.join(" · ")}</div>`;

        // 列出 skipped/failed 详情
        if (r.items) {
            const issues = r.items.filter(i => i.status === "skipped" || i.status === "failed");
            if (issues.length > 0) {
                resultBox.innerHTML += `<details class="mt-2 small"><summary class="text-secondary">查看 ${issues.length} 个未写入项</summary><ul class="mt-1 mb-0">` +
                    issues.map(i => `<li><code>${i.nfo_path}</code> — ${i.status}: ${i.reason || ""}</li>`).join("") + "</ul></details>";
            }
        }

        addLog(`NFO 写入: ${lines.join(" / ").replace(/<[^>]+>/g, "")}`, r.total_failed ? "warning" : "success");
        btn.innerHTML = '<i class="bi bi-check-lg me-1"></i>完成';
        // 刷新当前目录视图（新建/覆盖的 .nfo 出现在列表里）
        if ((r.total_created || 0) + (r.total_overwrote || 0) > 0) {
            await loadFiles(currentPath);
        }
    } catch (err) {
        document.getElementById("nfo-write-result").style.display = "block";
        document.getElementById("nfo-write-result").innerHTML = `<div class="alert alert-danger py-2 mb-0">网络错误: ${err.message}</div>`;
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-check-lg me-1"></i>重试';
    } finally {
        nfoWritePending = null;
    }
}


function renderBatchRow(idx, row, data) {
    const top = data.top_pick;
    const resultEl = document.getElementById(`batch-result-${idx}`);
    const confEl = document.getElementById(`batch-conf-${idx}`);
    const sourceEl = document.getElementById(`batch-source-${idx}`);

    const pickLabel = {
        single_exact: "单候选",
        heuristic: "启发式",
        llm: "🤖 AI",
        needs_review: "待复核"
    }[data.pick_source] || data.pick_source || "?";

    if (top) {
        // 更新海报
        const tdPoster = row.cells[0];
        if (top.poster_url) {
            tdPoster.innerHTML = `<img src="${top.poster_url}" style="width:50px;height:auto;border-radius:3px;"/>`;
        }
        // 标题 + s/e
        const epLine = data.parse?.season && data.parse?.episode
            ? ` <small class="text-secondary">S${String(data.parse.season).padStart(2,"0")}E${String(data.parse.episode).padStart(2,"0")}</small>` : "";
        resultEl.innerHTML = `<span class="text-success">✓ ${top.title}</span>${top.original_title && top.original_title !== top.title ? ` <small class="text-secondary">(${top.original_title})</small>` : ""}${epLine} <small class="text-secondary">${top.year || "?"} · ⭐${top.vote_average?.toFixed(1) || "—"}</small>`;
        confEl.innerHTML = `<span class="text-success">${(data.confidence * 100).toFixed(0)}%</span>`;
        sourceEl.innerHTML = `<span class="${data.pick_source === 'llm' ? 'text-info' : 'text-secondary'}">${pickLabel}</span>`;
    } else {
        const candCount = (data.candidates || []).length;
        resultEl.innerHTML = `<span class="text-warning">⚠ 待复核（${candCount} 候选）</span> <small class="text-secondary">${data.reasoning?.slice(0, 80) || ""}</small>`;
        confEl.innerHTML = `<span class="text-secondary">${(data.confidence * 100).toFixed(0)}%</span>`;
        sourceEl.innerHTML = `<span class="text-warning">复核</span>`;
    }
}


// ==================== AI / 元数据配置 ====================

// ==================== 全库扫描 modal ====================

let scanModal = null;
let scanPollTimer = null;
let activeScanRunId = null;
let scanStartedAt = 0;

async function openScanModal() {
    if (!scanModal) {
        scanModal = new bootstrap.Modal(document.getElementById("scanModal"));
    }
    // 重置 UI 到 setup 视图
    document.getElementById("scan-setup").style.display = "block";
    document.getElementById("scan-progress").style.display = "none";
    document.getElementById("scan-base-path").value = nasBasePath || "";
    document.getElementById("scan-depth").value = 5;
    document.getElementById("scan-depth-label").textContent = "5";
    document.getElementById("scan-start-btn").style.display = "inline-block";
    document.getElementById("scan-abort-btn").style.display = "none";
    document.getElementById("scan-result").style.display = "none";

    // depth slider 联动
    document.getElementById("scan-depth").oninput = (e) => {
        document.getElementById("scan-depth-label").textContent = e.target.value;
    };

    scanModal.show();

    // 加载历史 + 检查 active scan
    try {
        const res = await apiFetch(`${API_BASE}/api/scan/runs?limit=5`);
        const data = await res.json();
        renderScanRecent(data.runs || []);
        // 找正在跑的 scan
        const active = (data.runs || []).find(r => r.status === "running");
        if (active) {
            // 已有 active scan → 直接进入进度视图监听
            attachToRunningScan(active.id);
        }
    } catch (err) {
        console.warn("load scan history failed", err);
    }
}

function renderScanRecent(runs) {
    const box = document.getElementById("scan-recent");
    if (!runs.length) {
        box.innerHTML = "（暂无历史记录）";
        return;
    }
    const rows = runs.map(r => {
        const at = new Date(r.started_at * 1000).toLocaleString();
        const statusEmoji = {
            running: "⏳", done: "✓", aborted: "⊘", failed: "✗"
        }[r.status] || "·";
        const counts = `${r.files_done}/${r.files_total} done · ${r.files_skipped} skip · ${r.files_failed} fail`;
        return `<div style="line-height:1.4">${statusEmoji} <code style="font-size:11px;">${r.base_path}</code> · ${counts} · <span style="font-size:10px;opacity:0.7">${at}</span></div>`;
    }).join("");
    box.innerHTML = `<strong>最近扫描：</strong><div class="mt-1">${rows}</div>`;
}

async function startFullScan() {
    const basePath = document.getElementById("scan-base-path").value.trim();
    const maxDepth = parseInt(document.getElementById("scan-depth").value, 10);
    if (!basePath) {
        alert("请输入扫描根路径");
        return;
    }
    const btn = document.getElementById("scan-start-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>启动中...';
    try {
        const res = await apiFetch(`${API_BASE}/api/scan/start`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ base_path: basePath, max_depth: maxDepth }),
        });
        const data = await res.json();
        if (!res.ok) {
            alert(`启动失败：${data.error || JSON.stringify(data)}\n${data.detail || ""}`);
            return;
        }
        attachToRunningScan(data.scan_run_id);
    } catch (err) {
        alert(`网络错误：${err.message}`);
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-play-fill me-1"></i>开始扫描';
    }
}

function attachToRunningScan(scanRunId) {
    activeScanRunId = scanRunId;
    scanStartedAt = Date.now() / 1000;
    document.getElementById("scan-setup").style.display = "none";
    document.getElementById("scan-progress").style.display = "block";
    document.getElementById("scan-start-btn").style.display = "none";
    document.getElementById("scan-abort-btn").style.display = "inline-block";
    document.getElementById("scan-abort-btn").disabled = false;
    document.getElementById("scan-abort-btn").innerHTML = '<i class="bi bi-stop-fill me-1"></i>中止';
    // 启动轮询
    if (scanPollTimer) clearInterval(scanPollTimer);
    pollScanStatus();
    scanPollTimer = setInterval(pollScanStatus, 2000);
}

async function pollScanStatus() {
    if (activeScanRunId === null) return;
    try {
        const res = await apiFetch(`${API_BASE}/api/scan/status?id=${activeScanRunId}`);
        const s = await res.json();
        if (!res.ok) {
            console.warn("scan status fetch failed", s);
            return;
        }
        renderScanProgress(s);
        if (s.status !== "running") {
            // 完成 / 失败 / aborted → 停止轮询 + 显示终态
            clearInterval(scanPollTimer);
            scanPollTimer = null;
            await renderScanResult(s);
        }
    } catch (err) {
        console.warn("scan status poll error", err);
    }
}

function renderScanProgress(s) {
    const total = s.files_total || 0;
    const processed = (s.files_done || 0) + (s.files_skipped || 0) + (s.files_failed || 0);
    const pct = total > 0 ? (processed / total * 100) : 0;
    document.getElementById("scan-progress-bar").style.width = pct + "%";
    document.getElementById("scan-progress-text").textContent = `${processed} / ${total}`;
    document.getElementById("scan-status-label").innerHTML =
        s.status === "running" ? '<span class="text-info">⏳ 扫描中</span>'
        : s.status === "done" ? '<span class="text-success">✓ 完成</span>'
        : s.status === "aborted" ? '<span class="text-warning">⊘ 已中止</span>'
        : s.status === "failed" ? '<span class="text-danger">✗ 失败</span>'
        : s.status;
    document.getElementById("scan-counts").innerHTML =
        `<span class="text-success">${s.files_done} done</span> · ` +
        `<span class="text-secondary">${s.files_skipped} skip</span> · ` +
        `<span class="text-danger">${s.files_failed} fail</span>`;
    document.getElementById("scan-current-path").textContent = s.current_path || "—";

    // ETA：基于已处理速率
    if (s.status === "running" && processed > 0 && total > processed) {
        const elapsed = Date.now() / 1000 - scanStartedAt;
        const rate = processed / elapsed;
        const remaining = (total - processed) / rate;
        const m = Math.floor(remaining / 60);
        const sec = Math.floor(remaining % 60);
        document.getElementById("scan-eta").textContent = `预计剩余 ${m}m ${sec}s`;
    } else {
        document.getElementById("scan-eta").textContent = "";
    }
}

async function renderScanResult(s) {
    document.getElementById("scan-abort-btn").style.display = "none";
    document.getElementById("scan-start-btn").style.display = "inline-block";
    document.getElementById("scan-start-btn").innerHTML = '<i class="bi bi-arrow-clockwise me-1"></i>再扫一次';

    const box = document.getElementById("scan-result");
    box.style.display = "block";

    let html = "";
    if (s.status === "done") {
        html += `<div class="alert alert-success py-2 mb-2 small">✓ 扫描完成：${s.files_done} 识别 · ${s.files_skipped} 跳过 · ${s.files_failed} 失败</div>`;
        addLog(`扫描完成: ${s.files_done} 识别 / ${s.files_skipped} 跳过 / ${s.files_failed} 失败`, "success");
    } else if (s.status === "aborted") {
        html += `<div class="alert alert-warning py-2 mb-2 small">⊘ 已中止：完成 ${(s.files_done||0)+(s.files_skipped||0)} / ${s.files_total}</div>`;
    } else if (s.status === "failed") {
        html += `<div class="alert alert-danger py-2 mb-2 small">✗ 扫描失败：${s.error || "unknown"}</div>`;
    }

    // 拉 failed 详情列表（如果有）
    if (s.files_failed > 0) {
        try {
            const r = await apiFetch(`${API_BASE}/api/scan/failed?id=${s.scan_run_id}&limit=50`);
            const data = await r.json();
            if (data.items && data.items.length > 0) {
                html += `<details class="small mt-2"><summary class="text-secondary" style="cursor:pointer">查看 ${data.items.length} 个失败项</summary><ul class="mt-1 mb-0" style="font-size:11px;">`;
                data.items.forEach(it => {
                    html += `<li><code>${it.path}</code> — ${it.error || "?"}</li>`;
                });
                html += `</ul></details>`;
            }
        } catch (err) {
            console.warn("load failed items error", err);
        }
    }
    box.innerHTML = html;

    activeScanRunId = null;
}

async function abortFullScan() {
    if (activeScanRunId === null) return;
    const btn = document.getElementById("scan-abort-btn");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>停止中...';
    try {
        await apiFetch(`${API_BASE}/api/scan/abort`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ scan_run_id: activeScanRunId }),
        });
        // worker 会在下一 loop 退出。继续轮询直到 status != running
    } catch (err) {
        alert(`中止失败：${err.message}`);
        btn.disabled = false;
    }
}


// ==================== AI / 元数据配置 ====================

let aiConfigModal = null;

async function showAIConfig() {
    if (!aiConfigModal) {
        aiConfigModal = new bootstrap.Modal(document.getElementById("aiConfigModal"));
    }
    // 先读现有 key 状态，更新 badge
    try {
        const [tmdbRes, dsRes] = await Promise.all([
            apiFetch(`${API_BASE}/api/config/tmdb`),
            apiFetch(`${API_BASE}/api/config/deepseek`),
        ]);
        const tmdb = await tmdbRes.json();
        const ds = await dsRes.json();
        _setKeyBadge("tmdb-key-status", tmdb.has_key);
        _setKeyBadge("deepseek-key-status", ds.has_key);
    } catch (err) {
        console.error("load key status failed", err);
    }
    document.getElementById("tmdb-key-input").value = "";
    document.getElementById("deepseek-key-input").value = "";
    document.getElementById("tmdb-test-status").innerHTML = "";
    document.getElementById("deepseek-test-status").innerHTML = "";
    aiConfigModal.show();
}

function _setKeyBadge(id, hasKey) {
    const el = document.getElementById(id);
    if (!el) return;
    if (hasKey) {
        el.textContent = "已配置";
        el.className = "badge bg-success ms-1";
    } else {
        el.textContent = "未配置";
        el.className = "badge bg-secondary ms-1";
    }
}

async function testTMDBKey() {
    const input = document.getElementById("tmdb-key-input").value.trim();
    const status = document.getElementById("tmdb-test-status");
    status.innerHTML = '<span class="text-secondary">测试中...</span>';
    try {
        const body = input ? { api_key: input } : {};
        const res = await apiFetch(`${API_BASE}/api/config/tmdb/test`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.ok) {
            status.innerHTML = `<span class="text-success">✓ TMDB OK${data.images_base_url ? ' · 图床: '+data.images_base_url : ''}</span>`;
        } else {
            status.innerHTML = `<span class="text-danger">✗ ${data.message}</span>`;
        }
    } catch (err) {
        status.innerHTML = `<span class="text-danger">网络错误: ${err.message}</span>`;
    }
}

async function testDeepseekKey() {
    const input = document.getElementById("deepseek-key-input").value.trim();
    const status = document.getElementById("deepseek-test-status");
    status.innerHTML = '<span class="text-secondary">测试中（一次小调用 ~1 秒）...</span>';
    try {
        const body = input ? { api_key: input } : {};
        const res = await apiFetch(`${API_BASE}/api/config/deepseek/test`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (data.ok) {
            status.innerHTML = `<span class="text-success">✓ DeepSeek OK · 模型 ${data.model} · 样本：${data.sample}</span>`;
        } else {
            status.innerHTML = `<span class="text-danger">✗ ${data.message}</span>`;
        }
    } catch (err) {
        status.innerHTML = `<span class="text-danger">网络错误: ${err.message}</span>`;
    }
}

async function saveAIConfig() {
    const tmdb = document.getElementById("tmdb-key-input").value.trim();
    const ds = document.getElementById("deepseek-key-input").value.trim();
    const ops = [];
    if (tmdb) {
        ops.push(apiFetch(`${API_BASE}/api/config/tmdb`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_key: tmdb }),
        }).then(r => r.json()).then(d => ({ kind: "TMDB", ok: d.ok })));
    }
    if (ds) {
        ops.push(apiFetch(`${API_BASE}/api/config/deepseek`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ api_key: ds }),
        }).then(r => r.json()).then(d => ({ kind: "DeepSeek", ok: d.ok })));
    }
    if (ops.length === 0) {
        alert("没有改动。填一个 key 再保存，或直接关闭。");
        return;
    }
    const results = await Promise.all(ops);
    const ok = results.every(r => r.ok);
    if (ok) {
        addLog("AI 配置已保存：" + results.map(r => r.kind).join(", "), "success");
        // 重读 badge
        showAIConfig();
    } else {
        const failed = results.filter(r => !r.ok).map(r => r.kind).join(", ");
        alert("部分保存失败：" + failed);
    }
}


// ==================== qBittorrent 配置 ====================

let qbitConfigModal = null;

function showQBitConfig() {
    // 初始化模态框
    if (!qbitConfigModal) {
        qbitConfigModal = new bootstrap.Modal(document.getElementById("qbitConfigModal"));
    }

    // 加载当前配置
    loadQBitConfig();
    qbitConfigModal.show();
}

async function loadQBitConfig() {
    const statusEl = document.getElementById("qbit-connection-status");
    statusEl.innerHTML = '<span class="text-secondary">检查中...</span>';
    statusEl.className = "";

    try {
        const res = await apiFetch(`${API_BASE}/api/config/qbit`);
        const config = await res.json();

        document.getElementById("qbit-url").value = config.url || "";
        document.getElementById("qbit-user").value = config.user || "";
        document.getElementById("qbit-password").value = "";

        // 测试连接状态
        testQBitConnection();
    } catch (err) {
        statusEl.innerHTML = "";
        statusEl.appendChild(createElement("span", {
            className: "text-danger",
            textContent: `加载失败: ${err.message}`
        }));
    }
}

async function saveQBitConfig() {
    const url = document.getElementById("qbit-url").value.trim();
    const user = document.getElementById("qbit-user").value.trim();
    const password = document.getElementById("qbit-password").value;

    if (!url) {
        alert("请填写 qBittorrent 地址");
        return;
    }

    const btn = document.getElementById("btn-save-qbit");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 保存中...';

    try {
        const res = await apiFetch(`${API_BASE}/api/config/qbit`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, user, password })
        });

        const data = await res.json();
        if (data.error) {
            alert(`保存失败: ${data.error}`);
            return;
        }

        addLog("qBit 配置已保存", "success");
        testQBitConnection();
    } catch (err) {
        alert(`保存失败: ${err.message}`);
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-save"></i> 保存配置';
    }
}

async function testQBitConnection() {
    const statusEl = document.getElementById("qbit-connection-status");
    statusEl.innerHTML = '<div class="spinner-border spinner-border-sm text-primary"></div> 测试中...';
    statusEl.className = "mt-2";

    // 用当前表单里的值测试（无需先保存）；password 留空表示沿用已保存的
    const url = (document.getElementById("qbit-url").value || "").trim();
    const user = (document.getElementById("qbit-user").value || "").trim();
    const password = document.getElementById("qbit-password").value || "";

    try {
        const res = await apiFetch(`${API_BASE}/api/config/qbit/test`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, user, password })
        });

        const data = await res.json();

        statusEl.innerHTML = "";
        if (data.status === "ok") {
            const ok = createElement("span", { className: "text-success" });
            ok.appendChild(createElement("i", { className: "bi bi-check-circle me-1" }));
            ok.appendChild(document.createTextNode("连接成功"));
            statusEl.appendChild(ok);
            statusEl.appendChild(createElement("br"));
            statusEl.appendChild(createElement("small", {
                className: "text-secondary",
                textContent: `种子数量: ${data.torrent_count}`
            }));
        } else {
            renderConnectError(statusEl, data.message || "未知错误");
        }
    } catch (err) {
        statusEl.innerHTML = "";
        renderConnectError(statusEl, err.message);
    }
}

function renderConnectError(statusEl, message) {
    const wrap = createElement("span", { className: "text-danger" });
    wrap.appendChild(createElement("i", { className: "bi bi-x-circle me-1" }));
    wrap.appendChild(document.createTextNode("连接失败"));
    statusEl.appendChild(wrap);
    statusEl.appendChild(createElement("br"));
    statusEl.appendChild(createElement("small", {
        className: "text-danger",
        textContent: message
    }));
}

// ==================== NAS 配置 ====================

let nasConfigModal = null;

function showNASConfig() {
    if (!nasConfigModal) {
        nasConfigModal = new bootstrap.Modal(document.getElementById("nasConfigModal"));
    }
    loadNASConfig();
    nasConfigModal.show();
}

async function loadNASConfig() {
    const statusEl = document.getElementById("nas-connection-status");
    statusEl.innerHTML = "";

    try {
        const res = await apiFetch(`${API_BASE}/api/config/nas`);
        const cfg = await res.json();
        document.getElementById("nas-host").value = cfg.host || "";
        document.getElementById("nas-port").value = cfg.port || 22;
        document.getElementById("nas-user").value = cfg.user || "";
        document.getElementById("nas-base-path").value = cfg.base_path || "";
        document.getElementById("nas-disk-pattern").value = cfg.disk_pattern || "";

        if (cfg.configured) {
            // 已保存过，自动测一次
            testNASConnection();
        } else {
            const hint = createElement("span", { className: "text-secondary" });
            hint.appendChild(createElement("i", { className: "bi bi-info-circle me-1" }));
            hint.appendChild(document.createTextNode("尚未配置过，请填写后点击测试连接"));
            statusEl.appendChild(hint);
        }
    } catch (err) {
        renderConnectError(statusEl, `加载失败: ${err.message}`);
    }
}

function _readNASForm() {
    return {
        host: document.getElementById("nas-host").value.trim(),
        port: parseInt(document.getElementById("nas-port").value, 10) || 22,
        user: document.getElementById("nas-user").value.trim(),
        base_path: document.getElementById("nas-base-path").value.trim(),
        disk_pattern: document.getElementById("nas-disk-pattern").value.trim(),
    };
}

async function saveNASConfig() {
    const data = _readNASForm();
    if (!data.host) { alert("请填写主机地址"); return; }
    if (!data.user) { alert("请填写 SSH 用户名"); return; }
    if (!data.base_path || !data.base_path.startsWith("/")) {
        alert("文件根路径必须以 / 开头");
        return;
    }

    const btn = document.getElementById("btn-save-nas");
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 保存中...';

    try {
        const res = await apiFetch(`${API_BASE}/api/config/nas`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(data),
        });
        const json = await res.json();
        if (json.error) {
            alert(`保存失败: ${json.error}`);
            return;
        }
        addLog("NAS 配置已保存", "success");

        // 重新拉取配置（base_path 可能变了）并刷新 UI
        const cfgRes = await apiFetch(`${API_BASE}/api/config/app`);
        const cfg = await cfgRes.json();
        nasBasePath = cfg.nas_base_path;
        currentPath = nasBasePath;

        refreshDisk();
        loadFiles(currentPath);
        testNASConnection();
    } catch (err) {
        alert(`保存失败: ${err.message}`);
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-check2 me-1"></i>保存并应用';
    }
}

async function testNASConnection() {
    const statusEl = document.getElementById("nas-connection-status");
    statusEl.innerHTML = '<div class="spinner-border spinner-border-sm text-primary"></div> 测试中...';

    try {
        const res = await apiFetch(`${API_BASE}/api/config/nas/test`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(_readNASForm()),
        });
        const data = await res.json();
        statusEl.innerHTML = "";
        if (data.status === "ok") {
            const ok = createElement("span", { className: "text-success" });
            ok.appendChild(createElement("i", { className: "bi bi-check-circle me-1" }));
            ok.appendChild(document.createTextNode("连接成功"));
            statusEl.appendChild(ok);
            statusEl.appendChild(createElement("br"));
            statusEl.appendChild(createElement("small", {
                className: "text-secondary",
                textContent: data.message || "",
            }));
        } else {
            renderConnectError(statusEl, data.message || "未知错误");
        }
    } catch (err) {
        statusEl.innerHTML = "";
        renderConnectError(statusEl, err.message);
    }
}


// ==================== 库视图（按 TMDB 元数据浏览） ====================

let currentView = "files";              // 'files' | 'library'
let libraryOffset = 0;
let libraryHasMore = false;
const LIBRARY_PAGE_SIZE = 60;

function switchView(view) {
    console.log("[switchView] called with:", view);
    currentView = view;
    const fileTab = document.getElementById("view-tab-files");
    const libTab = document.getElementById("view-tab-library");
    const dedupTab = document.getElementById("view-tab-dedup");
    const libPanel = document.getElementById("library-panel");
    const dedupPanel = document.getElementById("dedup-panel");
    if (!libPanel) {
        console.error("[switchView] #library-panel 不存在！HTML 没有加载新版本，请 hard reload");
        return;
    }
    fileTab.classList.toggle("active", view === "files");
    libTab.classList.toggle("active", view === "library");
    if (dedupTab) dedupTab.classList.toggle("active", view === "dedup");

    libPanel.classList.toggle("active", view === "library");
    if (dedupPanel) dedupPanel.classList.toggle("active", view === "dedup");

    if (view === "library") {
        loadLibrary(true);
        loadLibraryStats();
    } else if (view === "dedup") {
        _bindDedupFilters();
        loadDedupGroups();
    }
}

let libraryDebounce = null;
function _bindLibraryFilters() {
    const trigger = () => {
        clearTimeout(libraryDebounce);
        libraryDebounce = setTimeout(() => loadLibrary(true), 200);
    };
    ["library-search", "library-year-from", "library-year-to"].forEach(id => {
        const el = document.getElementById(id);
        if (el && !el.__bound) {
            el.addEventListener("input", trigger);
            el.__bound = true;
        }
    });
    ["library-type", "library-sort"].forEach(id => {
        const el = document.getElementById(id);
        if (el && !el.__bound) {
            el.addEventListener("change", () => loadLibrary(true));
            el.__bound = true;
        }
    });
}

function _libraryQueryParams() {
    const p = new URLSearchParams();
    const q = document.getElementById("library-search").value.trim();
    const t = document.getElementById("library-type").value;
    const sort = document.getElementById("library-sort").value;
    const yf = document.getElementById("library-year-from").value;
    const yt = document.getElementById("library-year-to").value;
    if (q) p.set("q", q);
    if (t) p.set("media_type", t);
    if (sort) p.set("sort", sort);
    if (yf) p.set("year_from", yf);
    if (yt) p.set("year_to", yt);
    p.set("limit", LIBRARY_PAGE_SIZE);
    p.set("offset", libraryOffset);
    return p;
}

async function loadLibrary(reset = false) {
    _bindLibraryFilters();
    if (reset) {
        libraryOffset = 0;
        document.getElementById("library-grid").innerHTML = "";
    }
    try {
        const res = await apiFetch(`${API_BASE}/api/library/items?${_libraryQueryParams()}`);
        const data = await res.json();
        if (!res.ok) {
            console.warn("library load failed", data);
            return;
        }
        renderLibraryGrid(data.items, !reset);
        libraryHasMore = data.has_more;
        libraryOffset += data.items.length;
        document.getElementById("library-count").textContent = `${data.total} 个媒体`;
        document.getElementById("library-empty").style.display = (data.total === 0) ? "block" : "none";
        document.getElementById("library-loadmore").style.display = libraryHasMore ? "block" : "none";
    } catch (err) {
        console.error("library load error", err);
    }
}

async function loadMoreLibrary() {
    if (!libraryHasMore) return;
    await loadLibrary(false);
}

async function loadLibraryStats() {
    try {
        const res = await apiFetch(`${API_BASE}/api/library/stats`);
        const s = await res.json();
        const parts = [`${s.total} 部`];
        if (s.by_media_type?.movie) parts.push(`${s.by_media_type.movie} 电影`);
        if (s.by_media_type?.tv) parts.push(`${s.by_media_type.tv} 剧集`);
        if (s.top_genres?.length) parts.push(`Top: ${s.top_genres.slice(0, 3).map(g => g.name).join(" · ")}`);
        document.getElementById("library-stats-mini").textContent = parts.join(" · ");
    } catch (err) {
        console.warn("library stats error", err);
    }
}

function renderLibraryGrid(items, append = false) {
    const grid = document.getElementById("library-grid");
    if (!append) grid.innerHTML = "";
    items.forEach(it => {
        const card = createElement("div", { className: "lib-card" });
        const poster = createElement("div", { className: "lib-poster" });
        if (it.poster_url) {
            poster.style.backgroundImage = `url('${it.poster_url}')`;
        } else {
            poster.classList.add("no-poster");
            poster.innerHTML = '<i class="bi bi-film"></i>';
        }
        if (it.media_type) {
            const badge = createElement("div", { className: "lib-type-badge", textContent: it.media_type === "tv" ? "TV" : "电影" });
            poster.appendChild(badge);
        }
        if (it.vote_average) {
            const rating = createElement("div", { className: "lib-rating", textContent: "⭐ " + it.vote_average.toFixed(1) });
            poster.appendChild(rating);
        }
        card.appendChild(poster);

        const meta = createElement("div", { className: "lib-meta" });
        const titleText = it.title || "未命名";
        const epSuffix = (it.season && it.episode)
            ? ` S${String(it.season).padStart(2, "0")}E${String(it.episode).padStart(2, "0")}` : "";
        meta.appendChild(createElement("div", { className: "lib-title", textContent: titleText + epSuffix }));
        const subParts = [];
        if (it.year) subParts.push(it.year);
        if (it.resolution) subParts.push(it.resolution);
        meta.appendChild(createElement("div", { className: "lib-sub", textContent: subParts.join(" · ") }));
        card.appendChild(meta);

        card.addEventListener("click", () => showLibraryItemDetail(it));
        grid.appendChild(card);
    });
}

function showLibraryItemDetail(item) {
    const panel = document.getElementById("file-detail");
    const content = document.getElementById("file-detail-content");
    panel.style.display = "block";
    content.innerHTML = "";
    const sidebar = document.querySelector(".sidebar");
    if (sidebar) sidebar.scrollTop = 0;

    const titleEl = createElement("h6", { textContent: item.title || "未命名" });
    if (item.season && item.episode) {
        titleEl.innerHTML += ` <small class="text-secondary">S${String(item.season).padStart(2, "0")}E${String(item.episode).padStart(2, "0")}</small>`;
    }
    content.appendChild(titleEl);
    if (item.original_title && item.original_title !== item.title) {
        content.appendChild(createElement("small", { className: "text-secondary d-block mb-2", textContent: item.original_title }));
    }

    const fakeData = {
        provider_state: "ok",
        top_pick: {
            title: item.title, original_title: item.original_title,
            year: item.year, media_type: item.media_type,
            poster_url: item.poster_url, overview: item.overview,
            vote_average: item.vote_average, external_ids: {tmdb_id: item.tmdb_id},
            id: `tmdb:${item.media_type}:${item.tmdb_id}`,
        },
        confidence: 1.0,
        reasoning: "cached",
        pick_source: "cached",
        parse: { title: item.title, year: item.year, season: item.season, episode: item.episode },
        details: {
            genres: item.genres || [],
            cast: item.cast || [],
            runtime_minutes: item.runtime_minutes,
        },
        llm_configured: true,
    };
    const cardBox = createElement("div");
    cardBox.__videoPath = item.path;
    renderMetadataCard(cardBox, fakeData);
    content.appendChild(cardBox);

    content.appendChild(createElement("hr"));
    content.appendChild(createElement("small", {
        className: "text-secondary d-block mb-1", textContent: "文件路径",
    }));
    content.appendChild(createElement("code", {
        textContent: item.path,
        style: "font-size:10px;word-break:break-all;display:block;margin-bottom:8px;",
    }));

    const btnRow = createElement("div", { className: "d-flex gap-2 mt-2" });
    const gotoBtn = createElement("button", {
        className: "btn btn-sm btn-outline-secondary",
        innerHTML: '<i class="bi bi-folder2-open me-1"></i>进入所在目录',
    });
    gotoBtn.addEventListener("click", () => {
        const dir = item.path.substring(0, item.path.lastIndexOf("/")) || nasBasePath;
        switchView("files");
        loadFiles(dir);
    });
    btnRow.appendChild(gotoBtn);
    content.appendChild(btnRow);

    // 异步加载同目录附属文件（特典/花絮/采访/预告）
    loadExtrasForMain(item.path, content);
}

async function loadExtrasForMain(mainPath, container) {
    try {
        const res = await apiFetch(`${API_BASE}/api/library/companions-in-dir?path=${encodeURIComponent(mainPath)}`);
        if (!res.ok) return;
        const data = await res.json();
        if (!data.items || data.items.length === 0) return;

        // 按 kind 分组（part = 多盘分段，extra = 花絮）
        const parts = data.items.filter(i => i.kind === "part");
        const extras = data.items.filter(i => i.kind === "extra");

        const renderGroup = (label, items, parent) => {
            if (items.length === 0) return;
            parent.appendChild(createElement("hr", { className: "my-2" }));
            parent.appendChild(createElement("small", {
                className: "text-secondary d-block mb-1",
                textContent: `${label} (${items.length})`,
            }));
            const list = createElement("div", { className: "list-group list-group-flush small" });
            items.forEach(it => {
                const row = createElement("div", {
                    className: "list-group-item bg-transparent text-light border-secondary py-1 px-2",
                    style: "font-size:11px;",
                });
                const sizeHuman = it.size_bytes ? humanSize(it.size_bytes) : "?";
                const tags = [it.resolution, it.source].filter(Boolean).join(" · ");
                row.innerHTML = `
                    <div style="word-break:break-all;">${it.raw_name || it.path}</div>
                    <div class="text-secondary" style="font-size:10px;">${sizeHuman}${tags ? " · " + tags : ""}</div>
                `;
                list.appendChild(row);
            });
            parent.appendChild(list);
        };

        const wrap = createElement("div", { className: "mt-3" });
        renderGroup("本片其他分段", parts, wrap);    // BD2 / Disc2 等
        renderGroup("本目录附属文件", extras, wrap); // 花絮 / 采访 / 预告
        container.appendChild(wrap);
    } catch (e) {
        // 加载失败静默 — 不影响主详情展示
        console.warn("[loadExtrasForMain] failed:", e);
    }
}


// ─── Phase 3.3: 重复检测视图 ────────────────────────────────────

const dedupState = {
    groups: [],
    selectedFileIds: new Set(),
};

async function loadDedupGroups() {
    const params = new URLSearchParams();
    const type = document.getElementById("dedup-type").value;
    const watchedOnly = document.getElementById("dedup-watched-only").checked;
    if (type) params.set("media_type", type);
    if (watchedOnly) params.set("watched_only", "1");
    params.set("limit", "50");
    params.set("offset", "0");

    const container = document.getElementById("dedup-groups");
    container.innerHTML = '<div class="text-secondary py-3 text-center">加载中…</div>';
    dedupState.selectedFileIds.clear();
    _updateDedupDeleteBtn();

    try {
        const res = await apiFetch(`${API_BASE}/api/dedup/groups?${params}`);
        const data = await res.json();
        if (!res.ok) {
            container.innerHTML = `<div class="text-danger py-3 text-center">加载失败: ${data.error || res.status}</div>`;
            return;
        }
        dedupState.groups = data.groups;
        document.getElementById("dedup-stats").textContent =
            `${data.total_groups} 组重复，可释放约 ${humanSize(data.total_deletable_bytes || 0)}`;
        document.getElementById("dedup-empty").style.display =
            (data.groups.length === 0) ? "block" : "none";
        container.innerHTML = "";
        data.groups.forEach(g => container.appendChild(_renderDedupGroup(g)));
    } catch (e) {
        console.error("[loadDedupGroups] failed:", e);
        container.innerHTML = '<div class="text-danger py-3 text-center">加载异常</div>';
    }
}

function _renderDedupGroup(group) {
    const wrap = createElement("div", { className: "dedup-group" });
    const title = group.title || group.tmdb_movie_id || group.tmdb_series_id || "(未识别)";
    const heading = (group.media_type === "tv")
        ? `${title} (S${group.season_number}E${group.episode_number})`
        : `${title}${group.year ? " (" + group.year + ")" : ""}`;
    wrap.appendChild(createElement("h4", { textContent: heading }));
    wrap.appendChild(createElement("div", {
        className: "group-stats",
        textContent: `${group.candidates.length} 份 · 总占用 ${humanSize(group.total_size_bytes)} · 可删 ${humanSize(group.deletable_size_bytes)}`,
    }));

    group.candidates.forEach(c => {
        const row = createElement("div", {
            className: "dedup-candidate" + (c.keep_recommended ? " keep-recommended" : ""),
        });
        const chk = createElement("input", {
            type: "checkbox",
            // 默认勾选非推荐保留的（用户最常想删的）
            checked: !c.keep_recommended,
        });
        if (!c.keep_recommended) {
            dedupState.selectedFileIds.add(c.media_file_id);
        }
        chk.dataset.fileId = String(c.media_file_id);
        chk.addEventListener("change", () => {
            if (chk.checked) {
                dedupState.selectedFileIds.add(c.media_file_id);
            } else {
                dedupState.selectedFileIds.delete(c.media_file_id);
            }
            _updateDedupDeleteBtn();
        });
        row.appendChild(chk);

        if (c.keep_recommended) {
            row.appendChild(createElement("span", { className: "keep-badge", textContent: "推荐保留" }));
        }
        if (c.is_watched) {
            row.appendChild(createElement("span", { className: "watched-badge", textContent: "已看" }));
        }
        const tags = [c.resolution, ...(c.hdr_profiles || []), c.source, c.codec, c.release_group]
            .filter(Boolean).join(" · ");
        row.appendChild(createElement("div", {
            className: "path",
            innerHTML: `<strong>${humanSize(c.size_bytes || 0)}</strong> · ${tags || "?"}<br>${c.path}`,
        }));
        row.appendChild(createElement("span", {
            className: "score-badge",
            textContent: c.quality_score.toFixed(0),
            title: JSON.stringify(c.score_breakdown, null, 2),
        }));
        wrap.appendChild(row);
    });
    _updateDedupDeleteBtn();
    return wrap;
}

function _updateDedupDeleteBtn() {
    const btn = document.getElementById("dedup-delete-btn");
    if (!btn) return;
    const n = dedupState.selectedFileIds.size;
    btn.disabled = n === 0;
    btn.innerHTML = n === 0
        ? '<i class="bi bi-trash3"></i> 删除选中'
        : `<i class="bi bi-trash3"></i> 删除选中 (${n})`;
}

async function deleteDedupSelection() {
    if (dedupState.selectedFileIds.size === 0) return;
    // 收集所有选中候选的 candidate 对象（含 expected_*）
    const candidates = [];
    for (const g of dedupState.groups) {
        for (const c of g.candidates) {
            if (dedupState.selectedFileIds.has(c.media_file_id)) {
                candidates.push({
                    path: c.path,
                    expected_inode: c.inode,
                    expected_size: c.size_bytes,
                    expected_mtime: c.mtime,
                });
            }
        }
    }
    if (!confirm(`确认删除 ${candidates.length} 个文件？\n\n会按 strict 模式校验，文件期间被修改将阻止删除。`)) {
        return;
    }

    try {
        const previewRes = await apiFetch(`${API_BASE}/api/action/preview`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                kind: "delete",
                source: "dedup",
                snapshot_mode: "strict",
                candidates,
                options: { delete_torrents: true },
            }),
        });
        const data = await previewRes.json();

        if (previewRes.status === 409) {
            const summary = (data.mismatches || []).map(m =>
                `${m.path}: ${m.diffs.join(", ")}`
            ).join("\n");
            alert(`以下文件已变化，请刷新后重试：\n\n${summary}`);
            await loadDedupGroups();
            return;
        }
        if (!previewRes.ok) {
            alert(`Preview 失败: ${data.error || previewRes.status}\n${data.detail || ""}`);
            return;
        }

        // 显示 preview 摘要 + 第二次 confirm
        const sizeFreed = data.total_size_human;
        const nFiles = data.files.length;
        const nTorrents = (data.torrents || []).length;
        const nHardlinks = data.total_hardlinks;
        const msg = `Preview:\n  ${nFiles} 个文件 (${sizeFreed})\n  关联 ${nTorrents} 个种子\n  关联 ${nHardlinks} 个硬链接\n\nconfirm 后将立即执行删除（含 qBit 种子）。`;
        if (!confirm(msg)) return;

        const confirmRes = await apiFetch(`${API_BASE}/api/action/confirm`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                action_id: data.action_id,
                signed_token: data.signed_token,
            }),
        });
        const confirmData = await confirmRes.json();
        if (!confirmRes.ok) {
            alert(`Confirm 失败: ${confirmData.error || confirmRes.status}`);
            return;
        }
        alert(`删除完成。${JSON.stringify(confirmData.result || {}, null, 2).slice(0, 300)}`);
        await loadDedupGroups();
    } catch (e) {
        console.error("[deleteDedupSelection] failed:", e);
        alert(`操作失败: ${e.message || e}`);
    }
}

function _bindDedupFilters() {
    ["dedup-type", "dedup-watched-only"].forEach(id => {
        const el = document.getElementById(id);
        if (el && !el.__bound) {
            el.addEventListener("change", () => loadDedupGroups());
            el.__bound = true;
        }
    });
}


// Phase 3.5: 已看完归档候选 — 切换 dedup-groups 容器显示 watched-stale 列表
async function loadWatchedStale() {
    const container = document.getElementById("dedup-groups");
    container.innerHTML = '<div class="text-secondary py-3 text-center">加载中…</div>';
    document.getElementById("dedup-stats").textContent = "已看完候选";

    try {
        const res = await apiFetch(`${API_BASE}/api/library/watched-stale?days=180&limit=100`);
        const data = await res.json();
        if (!res.ok) {
            container.innerHTML = `<div class="text-danger py-3 text-center">加载失败: ${data.error || res.status}</div>`;
            return;
        }
        document.getElementById("dedup-stats").textContent =
            `${data.total} 个候选，本页 ${humanSize(data.total_bytes_on_page || 0)}`;
        if (data.items.length === 0) {
            container.innerHTML =
                '<div class="empty-state text-center"><i class="bi bi-emoji-smile" style="font-size:2rem;"></i><div>没有归档候选</div><small class="text-secondary">配置 Emby + 同步后才能看到</small></div>';
            return;
        }

        container.innerHTML = "";
        const banner = createElement("div", {
            className: "dedup-group",
            innerHTML: `<div class="text-secondary" style="font-size:12px;">这些文件你已看完且 180+ 天没动。本页占用 ${humanSize(data.total_bytes_on_page)}。点路径进入文件视图，从那里手动删除（走 file_browser 流程）。</div>`,
        });
        container.appendChild(banner);

        data.items.forEach(it => {
            const row = createElement("div", { className: "dedup-candidate" });
            const tags = [it.parse_resolution, it.parse_source].filter(Boolean).join(" · ");
            const dayLabel = (it.days_since_first_seen != null)
                ? `${it.days_since_first_seen} 天未动`
                : "—";
            const title = it.title || it.tmdb_movie_id || "(未识别)";
            const subtitle = (it.media_type === "tv")
                ? `${title} (S${it.season_number}E${it.episode_number})`
                : `${title}${it.year ? " (" + it.year + ")" : ""}`;
            row.appendChild(createElement("div", {
                className: "path",
                innerHTML: `<strong>${subtitle}</strong> · ${humanSize(it.size_bytes || 0)} · ${tags || "?"} · ${dayLabel}<br>${it.path}`,
            }));
            const btn = createElement("button", {
                className: "tb-btn",
                innerHTML: '<i class="bi bi-folder2-open"></i>',
                title: "在文件视图中打开",
            });
            btn.addEventListener("click", () => {
                const dir = it.path.substring(0, it.path.lastIndexOf("/"));
                switchView("files");
                loadFiles(dir);
            });
            row.appendChild(btn);
            container.appendChild(row);
        });
    } catch (e) {
        console.error("[loadWatchedStale] failed:", e);
        container.innerHTML = '<div class="text-danger py-3 text-center">加载异常</div>';
    }
}
