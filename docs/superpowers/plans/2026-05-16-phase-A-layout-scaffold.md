# Phase A: Layout 脚手架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 引入 Tailwind CSS Standalone CLI + HTMX + Alpine.js + 新 sidebar/topbar/status_bar layout 骨架；老 12 modal 全部保留可用（transitional state）。

**Architecture:** Flask + Jinja2 base.html 继承体系；HTMX/Alpine CDN 注入；Tailwind binary 编译 `static/css/input.css` 到 `output.css`；新增 `routes/ui_status.py` blueprint 给 status bar / sidebar badge 提供 HTML fragment；老 `templates/index.html` 改为 `extends base.html` 暂塞老主区，老 modal 保留触发点临时挂到 sidebar 或主区按钮（不在 topbar）。

**Tech Stack:** Flask + Jinja2 + HTMX 1.9 + Alpine.js 3.x + Tailwind CSS 3.4 Standalone CLI

**Spec reference:** `docs/superpowers/specs/2026-05-16-frontend-redesign-design.md` Section 6 Phase A

---

## File Structure

### 新建文件

| 路径 | 职责 |
|---|---|
| `scripts/install_tailwind.sh` | 下 Tailwind binary 到 `bin/tailwindcss`（首次运行 / CI） |
| `bin/.gitignore` | 排除 `bin/tailwindcss` binary（不入 git） |
| `Makefile` | `tailwind:install` / `tailwind:watch` / `tailwind:build` / `dev` / `test` target |
| `static/css/input.css` | Tailwind 入口：`@tailwind base; @tailwind components; @tailwind utilities` + 少量 @layer custom |
| `static/css/output.css` | (生成) Tailwind 编译产物；`.gitignore` 排除 |
| `tailwind.config.js` | Tailwind 配置：content paths（指向 templates/ + static/js/）+ theme extend |
| `templates/base.html` | Layout 主模板：含 sidebar / topbar / 主区 block / status_bar / drawer container |
| `templates/_sidebar.html` | Sidebar nav 6 项 partial（include 进 base.html） |
| `templates/_topbar.html` | Topbar partial：breadcrumb 占位 + 右侧用户 / hint |
| `templates/_status_bar.html` | 底部 status bar 容器（HTMX poll provider + worker） |
| `templates/_drawer.html` | 右侧 drawer 空容器（Phase A 不实现内容，仅 layout 槽位） |
| `templates/partials/status/providers.html` | `/ui/status/providers` 渲染的 HTML fragment |
| `templates/partials/status/workers.html` | `/ui/status/workers` 渲染的 HTML fragment |
| `templates/partials/sidebar/badges.html` | `/ui/sidebar/badges` 渲染的 HTML fragment |
| `routes/__init__.py` | empty package marker |
| `routes/ui_status.py` | Flask Blueprint `ui_status_bp` 提供 `/ui/status/*` + `/ui/sidebar/badges` |
| `tests/unit/test_ui_status.py` | Blueprint route 的 contract test（test_client + monkeypatch services） |

### 修改文件

| 路径 | 修改 |
|---|---|
| `app.py` | (1) 注册 `ui_status_bp` blueprint (2) 老 topbar 渲染的按钮 / state 删除 |
| `templates/index.html` | 改为 `{% extends "base.html" %}` + `{% block main %}` 包老主区；删 topbar / banner HTML（迁到 base.html 适当位置） |
| `.gitignore` | 加 `bin/tailwindcss` + `static/css/output.css` |
| `README.md` | （末尾）补充：`./scripts/install_tailwind.sh && make tailwind:build` 是第一步 |

### 不动文件

- 所有 `services/*.py`（业务层不变）
- 所有 `/api/*` JSON 路由（MCP server 依赖）
- 所有 12 个 modal HTML（Phase C 再删；A 期保留可用）
- `static/app.js`（Phase B/C 才动）
- 所有 `tests/unit/test_*.py`（不动现有测试）
- DB schema（不动）

---

## Task 1: Tailwind 工具链引入

**Files:**
- Create: `scripts/install_tailwind.sh`
- Create: `bin/.gitignore`
- Create: `Makefile`
- Create: `static/css/input.css`
- Create: `tailwind.config.js`
- Modify: `.gitignore`

**Why this matters:** Tailwind Standalone CLI 让我们零 Node 用 Tailwind。binary 不入 git 但跑 `make tailwind:install` 一次性下到 `bin/`，CI / 朋友 clone 时同 step。input.css 通过 binary 编译成 output.css。

### Step 1: 写 install_tailwind.sh

Create `scripts/install_tailwind.sh`:

```bash
#!/bin/bash
# Install Tailwind CSS Standalone CLI binary.
# Idempotent: skips if binary already present.
set -euo pipefail

BIN_DIR="$(cd "$(dirname "$0")/.." && pwd)/bin"
BIN_PATH="$BIN_DIR/tailwindcss"
VERSION="v3.4.13"

if [[ -x "$BIN_PATH" ]]; then
    echo "✓ tailwindcss already installed at $BIN_PATH"
    "$BIN_PATH" --help >/dev/null && exit 0
fi

mkdir -p "$BIN_DIR"

OS="$(uname -s)"
ARCH="$(uname -m)"

case "$OS-$ARCH" in
    Darwin-arm64)  TARGET="macos-arm64" ;;
    Darwin-x86_64) TARGET="macos-x64" ;;
    Linux-x86_64)  TARGET="linux-x64" ;;
    Linux-aarch64) TARGET="linux-arm64" ;;
    *) echo "Unsupported platform: $OS-$ARCH" >&2; exit 1 ;;
esac

URL="https://github.com/tailwindlabs/tailwindcss/releases/download/$VERSION/tailwindcss-$TARGET"
echo "Downloading $URL → $BIN_PATH"
curl -sSL -o "$BIN_PATH" "$URL"
chmod +x "$BIN_PATH"
echo "✓ tailwindcss installed at $BIN_PATH"
"$BIN_PATH" --help >/dev/null
```

Then:
```bash
chmod +x /Users/winson/Workspace/projects/nas/scripts/install_tailwind.sh
```

- [ ] **Step 1: 写 install_tailwind.sh + chmod +x**

### Step 2: 写 bin/.gitignore + 主 .gitignore 更新

Create `bin/.gitignore`:
```
# Tailwind binary downloaded by scripts/install_tailwind.sh
tailwindcss
```

Append to `.gitignore`:
```
# Tailwind 编译产物 + 工具
static/css/output.css
bin/tailwindcss
```

- [ ] **Step 2: 加 bin/.gitignore + 主 .gitignore 加 output.css 排除**

### Step 3: 写 tailwind.config.js

Create `tailwind.config.js`:

```js
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./static/js/**/*.js",
  ],
  theme: {
    extend: {
      colors: {
        // NASVault palette — keep small, expand later
        sidebar: {
          bg: "#1a1d23",
          fg: "#cdd2d8",
          fg_active: "#ffffff",
          accent: "#6699cc",
        },
        statusbar: {
          bg: "#2d3138",
          fg: "#a0a4ab",
          ok: "#7eb377",
          warn: "#d9b04a",
          err: "#cc6666",
          gray: "#6c727b",
        },
      },
    },
  },
  plugins: [],
};
```

- [ ] **Step 3: 写 tailwind.config.js**

### Step 4: 写 static/css/input.css

Create `static/css/input.css`:

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

/* Custom global styles — keep minimal, prefer utility classes inline */
@layer base {
  html {
    @apply text-sm;
  }
  body {
    @apply bg-slate-900 text-slate-100;
  }
}

@layer components {
  /* Status bar segment */
  .sb-seg {
    @apply px-3 py-1 inline-flex items-center gap-1 cursor-pointer hover:bg-slate-700;
  }
  .sb-dot-ok    { @apply w-2 h-2 rounded-full bg-statusbar-ok; }
  .sb-dot-warn  { @apply w-2 h-2 rounded-full bg-statusbar-warn; }
  .sb-dot-err   { @apply w-2 h-2 rounded-full bg-statusbar-err; }
  .sb-dot-gray  { @apply w-2 h-2 rounded-full bg-statusbar-gray; }

  /* Sidebar nav item */
  .nav-item {
    @apply flex items-center justify-between px-4 py-2 text-sidebar-fg hover:text-sidebar-fg_active hover:bg-slate-800 rounded;
  }
  .nav-item.active {
    @apply text-sidebar-fg_active bg-slate-800;
  }
  .nav-badge {
    @apply text-xs bg-statusbar-warn text-slate-900 rounded-full px-2 py-0.5 font-semibold;
  }
}
```

- [ ] **Step 4: 写 input.css**

### Step 5: 写 Makefile

Create `Makefile`:

```makefile
.PHONY: tailwind\:install tailwind\:watch tailwind\:build dev test

tailwind\:install:
	@./scripts/install_tailwind.sh

tailwind\:watch: tailwind\:install
	@./bin/tailwindcss -i static/css/input.css -o static/css/output.css --watch

tailwind\:build: tailwind\:install
	@./bin/tailwindcss -i static/css/input.css -o static/css/output.css --minify

dev: tailwind\:build
	@flask --app app run --debug --port 5001

test:
	@pytest tests/ -v
```

Note: Make 不允许 target 名含 `:`，要用 `\:` 转义。如果你 shell 不支持转义 target，用 `tailwind-install` / `tailwind-watch` 替代。

实际上更稳的版本（target 名用 `-` 不用 `:`）：

```makefile
.PHONY: tailwind-install tailwind-watch tailwind-build dev test

tailwind-install:
	@./scripts/install_tailwind.sh

tailwind-watch: tailwind-install
	@./bin/tailwindcss -i static/css/input.css -o static/css/output.css --watch

tailwind-build: tailwind-install
	@./bin/tailwindcss -i static/css/input.css -o static/css/output.css --minify

dev: tailwind-build
	@flask --app app run --debug --port 5001

test:
	@pytest tests/ -v
```

- [ ] **Step 5: 写 Makefile（用 dash 不用 colon target 名）**

### Step 6: 跑安装 + 编译一次验证

```bash
cd /Users/winson/Workspace/projects/nas
make tailwind-install
# 期望输出: ✓ tailwindcss installed at ./bin/tailwindcss

make tailwind-build
# 期望输出: Tailwind 处理 → output.css 生成
ls -lh static/css/output.css
# 期望: 10-50KB 之间（minify 后）
```

- [ ] **Step 6: 跑 make tailwind-install + make tailwind-build 验证 binary OK + output.css 生成**

### Step 7: Commit

```bash
git add scripts/install_tailwind.sh bin/.gitignore Makefile tailwind.config.js static/css/input.css .gitignore
git commit -m "Phase A.1: Tailwind Standalone CLI 工具链 (零 Node build)

- scripts/install_tailwind.sh 下 binary 到 bin/tailwindcss
- Makefile target: tailwind-install/watch/build
- input.css 含 sidebar / statusbar 自定义色板
- .gitignore 排除 binary + output.css
"
```

- [ ] **Step 7: Commit Task 1**

---

## Task 2: base.html + 4 partials 空骨架

**Files:**
- Create: `templates/base.html`
- Create: `templates/_sidebar.html`
- Create: `templates/_topbar.html`
- Create: `templates/_status_bar.html`
- Create: `templates/_drawer.html`

**Why this matters:** 先建空骨架（layout 完整，但 sidebar nav / status bar / drawer 都用静态 placeholder）。下一 Task 才接动态数据。这种"先骨架后填充"风格让 layout 设计错误能在动态数据接入前发现。

### Step 1: 写 base.html

Create `templates/base.html`:

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{% block title %}NASVault{% endblock %}</title>
    <link rel="stylesheet" href="{{ url_for('static', filename='css/output.css') }}">
    <!-- HTMX 1.9.x -->
    <script src="https://unpkg.com/htmx.org@1.9.12"></script>
    <!-- Alpine.js 3.x -->
    <script defer src="https://unpkg.com/alpinejs@3.13.10/dist/cdn.min.js"></script>
    {% block head_extra %}{% endblock %}
</head>
<body class="h-screen flex flex-col overflow-hidden" x-data="{ drawerOpen: false }">

    <!-- Top region: sidebar + main -->
    <div class="flex-1 flex overflow-hidden">

        {% include "_sidebar.html" %}

        <main class="flex-1 flex flex-col overflow-hidden">
            {% include "_topbar.html" %}

            <div id="main-content" class="flex-1 overflow-auto p-4">
                {% block main %}
                <p class="text-slate-400">main block 未定义（这是 base.html 默认占位）</p>
                {% endblock %}
            </div>
        </main>

        {% include "_drawer.html" %}
    </div>

    {% include "_status_bar.html" %}

    <!-- Toast host (Phase E 实现，A 期空 div) -->
    <div id="toast-host" class="fixed bottom-12 right-4 z-50"></div>

</body>
</html>
```

- [ ] **Step 1: 写 base.html**

### Step 2: 写 _sidebar.html（静态 placeholder，badge 用 Jinja2 nav_badges 字典占位）

Create `templates/_sidebar.html`:

```html
<aside class="w-56 bg-sidebar-bg text-sidebar-fg flex flex-col">
    <div class="px-4 py-3 text-lg font-bold text-sidebar-fg_active border-b border-slate-700">
        🗄️ NASVault
    </div>

    <nav class="flex-1 px-2 py-3 space-y-1" id="sidebar-nav"
         hx-get="/ui/sidebar/badges"
         hx-trigger="load, every 60s"
         hx-swap="innerHTML">
        <!-- Initial render: server-side included; HTMX poll 覆盖 -->
        {% include "partials/sidebar/badges.html" %}
    </nav>

    <div class="px-4 py-2 text-xs text-slate-500 border-t border-slate-700">
        v{{ app_version | default('dev') }}
    </div>
</aside>
```

- [ ] **Step 2: 写 _sidebar.html**

### Step 3: 写 _topbar.html

Create `templates/_topbar.html`:

```html
<header class="bg-slate-800 border-b border-slate-700 px-4 py-2 flex items-center justify-between">
    <div class="text-slate-300 text-sm">
        {% block breadcrumb %}
        <span class="text-slate-500">/</span>
        {% endblock %}
    </div>
    <div class="text-xs text-slate-400">
        {% block topbar_right %}{% endblock %}
    </div>
</header>
```

- [ ] **Step 3: 写 _topbar.html**

### Step 4: 写 _status_bar.html

Create `templates/_status_bar.html`:

```html
<footer class="bg-statusbar-bg text-statusbar-fg text-xs flex items-center border-t border-slate-700">
    <div id="status-providers"
         hx-get="/ui/status/providers"
         hx-trigger="load, every 60s"
         hx-swap="innerHTML"
         class="flex items-center">
        {% include "partials/status/providers.html" %}
    </div>

    <div class="border-l border-slate-700 h-4 mx-2"></div>

    <div id="status-workers"
         hx-get="/ui/status/workers"
         hx-trigger="load, every 5s"
         hx-swap="innerHTML"
         class="flex items-center">
        {% include "partials/status/workers.html" %}
    </div>
</footer>
```

- [ ] **Step 4: 写 _status_bar.html**

### Step 5: 写 _drawer.html（empty container）

Create `templates/_drawer.html`:

```html
<aside id="drawer"
       x-show="drawerOpen"
       x-transition:enter="transition ease-out duration-200"
       x-transition:enter-start="translate-x-full"
       x-transition:enter-end="translate-x-0"
       x-transition:leave="transition ease-in duration-150"
       x-transition:leave-start="translate-x-0"
       x-transition:leave-end="translate-x-full"
       @keydown.escape.window="drawerOpen = false"
       class="w-[768px] max-w-[50%] bg-slate-800 border-l border-slate-700 flex flex-col overflow-hidden"
       style="display: none;">
    <header class="px-4 py-3 border-b border-slate-700 flex items-center justify-between">
        <h2 class="text-slate-100 font-semibold" id="drawer-title">Drawer</h2>
        <button @click="drawerOpen = false" class="text-slate-400 hover:text-slate-100">✕</button>
    </header>
    <div class="flex-1 overflow-auto p-4" id="drawer-content">
        <!-- Phase C: HTMX 把 fragments 渲到这里 -->
        <p class="text-slate-500">Phase A: drawer 容器就绪，内容待 Phase C 实现。</p>
    </div>
</aside>
```

- [ ] **Step 5: 写 _drawer.html**

### Step 6: 写 3 个 partial 占位（让 base.html include 不 404）

Create `templates/partials/sidebar/badges.html`:

```html
<a href="/" class="nav-item" hx-boost="true"><span>🏠 概览</span></a>
<a href="/files" class="nav-item" hx-boost="true"><span>📁 文件</span></a>
<a href="/library" class="nav-item" hx-boost="true">
    <span>🎬 媒体库</span>
    {% if badges.library | default(0) > 0 %}<span class="nav-badge">{{ badges.library }}</span>{% endif %}
</a>
<a href="/dedup" class="nav-item" hx-boost="true">
    <span>🔍 重复检测</span>
    {% if badges.dedup | default(0) > 0 %}<span class="nav-badge">{{ badges.dedup }}</span>{% endif %}
</a>
<a href="/organize" class="nav-item" hx-boost="true">
    <span>📦 整理</span>
    {% if badges.organize | default(0) > 0 %}<span class="nav-badge">{{ badges.organize }}</span>{% endif %}
</a>
<a href="/settings" class="nav-item" hx-boost="true"><span>⚙️ 设置</span></a>
```

Create `templates/partials/status/providers.html`:

```html
{% for p in providers | default([]) %}
<span class="sb-seg" title="{{ p.tooltip }}">
    <span class="sb-dot-{{ p.dot }}"></span>
    <span>{{ p.name }}</span>
    {% if p.detail %}<span class="text-statusbar-fg/60">({{ p.detail }})</span>{% endif %}
</span>
{% else %}
<span class="sb-seg text-slate-500">providers 加载中…</span>
{% endfor %}
```

Create `templates/partials/status/workers.html`:

```html
{% if workers and workers | length > 0 %}
{% for w in workers %}
<span class="sb-seg">⏳ {{ w.kind }} {{ w.done }}/{{ w.total }}</span>
{% endfor %}
{% else %}
<span class="sb-seg text-slate-500">workers idle</span>
{% endif %}
```

```bash
mkdir -p /Users/winson/Workspace/projects/nas/templates/partials/sidebar
mkdir -p /Users/winson/Workspace/projects/nas/templates/partials/status
```

- [ ] **Step 6: 写 3 个 partial 占位 + mkdir 父目录**

### Step 7: 临时让 / 路由继承 base.html 验证骨架（不接动态数据，纯 layout 跑通）

临时修改 `app.py` 里 `/` 路由，加一个 query param `?layout=v2` 切换走 base.html：

```python
# app.py 里找 def index() / @app.route("/")
@app.route("/")
def index():
    # ... existing logic ...
    if request.args.get("layout") == "v2":
        return render_template("base.html", badges={}, providers=[], workers=[])
    # ... existing return ...
```

然后跑：
```bash
make tailwind-build
flask --app app run --debug --port 5001
# 浏览器 http://localhost:5001/?layout=v2
# 期望: 看到 sidebar (6 项) + topbar (空 breadcrumb) + main 主区 (默认占位文字) + status bar (providers 加载中 / workers idle)
```

视觉验收：
- Sidebar 左边深色，6 项可见，可 hover
- 主区中部，"main block 未定义" 默认占位
- 底部 status bar 显示 "providers 加载中…"（HTMX 还没真 endpoint，会 404，但 DOM 显示初始 partial）
- Drawer 不可见（drawerOpen=false）
- 试 alt+点 sidebar 项 hx-boost 跳转（这会跳到老 / 路由不带 ?layout=v2，看到老 UI — 正常，Phase B 拆 page 才会有新 page）

- [ ] **Step 7: 临时加 ?layout=v2 fallback + 跑 dev server + 浏览器视觉验收**

### Step 8: Commit

```bash
git add templates/base.html templates/_sidebar.html templates/_topbar.html templates/_status_bar.html templates/_drawer.html templates/partials/ app.py
git commit -m "Phase A.2: base.html + 4 partials 空骨架

- base.html 含 sidebar / topbar / main block / status bar / drawer 容器
- HTMX + Alpine CDN 注入
- partials: sidebar badges / status providers / status workers 占位
- index 路由 ?layout=v2 临时跳新 layout 验证骨架
"
```

- [ ] **Step 8: Commit Task 2**

---

## Task 3: routes/ui_status.py blueprint + 4 个 HTML fragment 路由

**Files:**
- Create: `routes/__init__.py`
- Create: `routes/ui_status.py`
- Create: `tests/unit/test_ui_status.py`
- Modify: `app.py` (register blueprint)

**Why this matters:** spec Section 3 关键设计：`/api/*` JSON + `/ui/*` HTML 双路径。这里建立第一个 `/ui/*` blueprint，确立 pattern。所有 HTML fragment endpoint 都通过 blueprint 走，跟 `/api/*` JSON 明确分离。

### Step 1: 写 routes/__init__.py 空 marker

Create `routes/__init__.py`:

```python
"""Flask blueprints — server-rendered HTML fragments for HTMX.

`/api/*` (JSON) lives in app.py; `/ui/*` (HTML fragments) lives here.
"""
```

- [ ] **Step 1: 写 routes/__init__.py**

### Step 2: 写 routes/ui_status.py

Create `routes/ui_status.py`:

```python
"""UI status blueprint — 提供 status bar / sidebar badges HTML fragments.

约定:
- 路由前缀 /ui
- 返回 HTML fragment（不是完整页）
- 业务数据从 services/ 或 app module attr 拉，不重复实现
- 鉴权: 跟 /api/* 共用 require_token 装饰器（从 app 导入）
"""

from __future__ import annotations

from flask import Blueprint, render_template

ui_status_bp = Blueprint("ui_status", __name__, url_prefix="/ui")


def _require_token(view):
    """Wrap a view with require_token from app module.

    Import deferred to avoid circular import at module load.
    """
    from app import require_token  # noqa: PLC0415 — deferred

    return require_token(view)


@ui_status_bp.route("/status/providers", methods=["GET"])
def status_providers():
    """Provider 4 段（TMDB / DeepSeek / Emby / qBit）HTML fragment."""
    from app import _compute_providers_status  # noqa: PLC0415

    raw = _compute_providers_status()
    providers = _to_status_segments(raw)
    return render_template("partials/status/providers.html", providers=providers)


@ui_status_bp.route("/status/workers", methods=["GET"])
def status_workers():
    """运行中的 worker 列表 HTML fragment."""
    workers = _aggregate_running_workers()
    return render_template("partials/status/workers.html", workers=workers)


@ui_status_bp.route("/sidebar/badges", methods=["GET"])
def sidebar_badges():
    """Sidebar nav 每项的 badge 计数."""
    badges = _compute_sidebar_badges()
    return render_template("partials/sidebar/badges.html", badges=badges)


# ---------- helpers ----------


_DOT_BY_STATE = {
    "ok": "ok",
    "auth_failed": "err",
    "not_configured": "gray",
    "unreachable": "err",
    "error": "err",
    "unknown": "gray",
}


def _to_status_segments(raw: dict) -> list[dict]:
    """raw = {"tmdb": {"state": "ok", ...}, "deepseek": {...}, ...}.

    返回 [{"name": "TMDB", "dot": "ok", "detail": None, "tooltip": "..."}].
    """
    name_by_key = {"tmdb": "TMDB", "deepseek": "DeepSeek", "emby": "Emby", "qbit": "qBit"}
    segments = []
    for key in ("tmdb", "deepseek", "emby", "qbit"):
        entry = raw.get(key, {})
        state = entry.get("state", "unknown")
        dot = _DOT_BY_STATE.get(state, "gray")
        detail = None
        if state == "auth_failed":
            detail = "401"
        elif state == "not_configured":
            detail = "未配"
        elif state == "unreachable":
            detail = "无连接"
        tooltip = f"{name_by_key[key]}: {state}"
        if entry.get("last_check"):
            tooltip += f" @ {entry['last_check']}"
        segments.append({
            "name": name_by_key[key],
            "dot": dot,
            "detail": detail,
            "tooltip": tooltip,
        })
    return segments


def _aggregate_running_workers() -> list[dict]:
    """聚合 auto_organize_runs / scan_runs / organize_runs 中 running 的任务.

    返回 [{"kind": "organize", "done": 12, "total": 40, "id": "..."}].
    """
    from app import get_db  # noqa: PLC0415

    db = get_db()
    workers: list[dict] = []

    # organize_runs (Phase 4B background worker)
    rows = db.execute(
        "SELECT action_id, total, done FROM organize_runs WHERE status = 'running' "
        "ORDER BY started_at DESC LIMIT 5"
    ).fetchall()
    for r in rows:
        workers.append({
            "kind": "organize",
            "done": r["done"] or 0,
            "total": r["total"] or 0,
            "id": r["action_id"],
        })

    # scan_runs (background scanner)
    rows = db.execute(
        "SELECT id, total_files, processed_files FROM scan_runs WHERE status = 'running' "
        "ORDER BY started_at DESC LIMIT 3"
    ).fetchall()
    for r in rows:
        workers.append({
            "kind": "scanner",
            "done": r["processed_files"] or 0,
            "total": r["total_files"] or 0,
            "id": r["id"],
        })

    return workers


def _compute_sidebar_badges() -> dict:
    """每个 sidebar nav 项的 badge count.

    返回 {"library": N, "dedup": N, "organize": N}.
    """
    from app import get_db  # noqa: PLC0415

    db = get_db()
    badges = {}

    # 媒体库未识别
    row = db.execute(
        "SELECT count(*) AS c FROM media_files WHERE needs_identify = 1"
    ).fetchone()
    badges["library"] = row["c"] if row else 0

    # dedup 待处理 (group count)
    # 简化版：count distinct (tmdb_movie_id, season, episode) 有 ≥2 物理 inode 的 group
    # Phase A 仅占位；真值由 services/dedup 模块算
    try:
        from services.dedup import count_unresolved_groups  # noqa: PLC0415

        badges["dedup"] = count_unresolved_groups(db)
    except (ImportError, AttributeError):
        badges["dedup"] = 0

    # organize 进行中（pending + organizing）
    row = db.execute(
        "SELECT count(*) AS c FROM auto_organize_runs "
        "WHERE status IN ('pending', 'organizing')"
    ).fetchone()
    badges["organize"] = row["c"] if row else 0

    return badges
```

- [ ] **Step 2: 写 routes/ui_status.py**

### Step 3: app.py 注册 blueprint

Modify `app.py` — 在 `app = Flask(...)` 之后某处加：

```python
# 注册 blueprints
from routes.ui_status import ui_status_bp
app.register_blueprint(ui_status_bp)
```

放在 require_token 装饰器定义**之后**（因为 ui_status.py 通过 deferred import 用 require_token）。

- [ ] **Step 3: app.py register_blueprint**

### Step 4: 检查 services/dedup 有没有 count_unresolved_groups

```bash
grep -n "def count_unresolved_groups" /Users/winson/Workspace/projects/nas/services/dedup.py
```

如果**没有**，在 services/dedup.py 加一个最小实现（占位）：

```python
def count_unresolved_groups(db) -> int:
    """Count dedup groups with ≥2 physical inodes that user hasn't resolved yet.

    Phase A 占位实现 — Phase B/C 拆 page 时按 spec 精确化.
    """
    # 现在简化：count groups by (tmdb_movie_id, season, episode) 有 multiple inodes 的
    row = db.execute(
        """SELECT count(*) AS c FROM (
            SELECT tmdb_movie_id, season, episode FROM media_files
            WHERE tmdb_movie_id IS NOT NULL
            GROUP BY tmdb_movie_id, season, episode
            HAVING count(DISTINCT inode) >= 2
        )"""
    ).fetchone()
    return row["c"] if row else 0
```

- [ ] **Step 4: grep + 如缺则加 count_unresolved_groups**

### Step 5: 写 tests/unit/test_ui_status.py 契约测试

Create `tests/unit/test_ui_status.py`:

```python
"""Phase A: /ui/status/* + /ui/sidebar/badges 路由契约测试.

模式：test_client + monkeypatch 后端 boundary (rule: testing-and-fixtures.md
"Flask route 测试用 test_client + monkeypatch backend boundary")
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def client():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def token():
    import app as app_module

    return app_module.API_TOKEN


def test_status_providers_renders_4_segments(client, token):
    fake_raw = {
        "tmdb":     {"state": "ok", "last_check": "16:42"},
        "deepseek": {"state": "ok", "last_check": "16:42"},
        "emby":     {"state": "auth_failed", "last_check": "16:42"},
        "qbit":     {"state": "not_configured", "last_check": None},
    }
    with patch("app._compute_providers_status", return_value=fake_raw):
        resp = client.get("/ui/status/providers")

    assert resp.status_code == 200
    html = resp.data.decode()
    assert "TMDB" in html
    assert "DeepSeek" in html
    assert "Emby" in html
    assert "qBit" in html
    assert "sb-dot-ok" in html       # tmdb / deepseek
    assert "sb-dot-err" in html      # emby auth_failed
    assert "sb-dot-gray" in html     # qbit not_configured
    assert "401" in html             # emby detail
    assert "未配" in html             # qbit detail


def test_status_workers_idle_when_no_running(client):
    """auto_organize_runs / organize_runs / scan_runs 全 idle 时显示 idle."""
    with patch(
        "routes.ui_status._aggregate_running_workers", return_value=[]
    ):
        resp = client.get("/ui/status/workers")
    assert resp.status_code == 200
    assert "workers idle" in resp.data.decode()


def test_status_workers_lists_running(client):
    fake_workers = [
        {"kind": "organize", "done": 12, "total": 40, "id": "abc"},
        {"kind": "scanner", "done": 234, "total": 1797, "id": 7},
    ]
    with patch(
        "routes.ui_status._aggregate_running_workers", return_value=fake_workers
    ):
        resp = client.get("/ui/status/workers")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "organize 12/40" in html
    assert "scanner 234/1797" in html


def test_sidebar_badges_zero_hidden(client):
    """badge=0 时 partial 不应该渲染 .nav-badge span (零计数 hidden)."""
    fake_badges = {"library": 0, "dedup": 0, "organize": 0}
    with patch(
        "routes.ui_status._compute_sidebar_badges", return_value=fake_badges
    ):
        resp = client.get("/ui/sidebar/badges")
    assert resp.status_code == 200
    html = resp.data.decode()
    # 6 个 nav-item 都在
    assert html.count("nav-item") == 6
    # 但没有任何 nav-badge（因为 count=0 全跳过）
    assert "nav-badge" not in html


def test_sidebar_badges_renders_counts(client):
    fake_badges = {"library": 12, "dedup": 5, "organize": 3}
    with patch(
        "routes.ui_status._compute_sidebar_badges", return_value=fake_badges
    ):
        resp = client.get("/ui/sidebar/badges")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert ">12<" in html
    assert ">5<" in html
    assert ">3<" in html
```

- [ ] **Step 5: 写 test_ui_status.py 5 个 case**

### Step 6: 跑测试验证

```bash
cd /Users/winson/Workspace/projects/nas
pytest tests/unit/test_ui_status.py -v
```

期望 5 个 case 全 PASS。

如果 fail：
- `template not found` → 检查 `templates/partials/sidebar/badges.html` / `templates/partials/status/*.html` 是否在 Task 2 已建
- `Blueprint not registered` → Task 3.3 没改 app.py
- `_compute_providers_status not found` → app.py module 里这个函数名不存在；grep 确认

- [ ] **Step 6: 跑 pytest 5 case 全 PASS**

### Step 7: 跑 dev server 端到端验证 status bar

```bash
make tailwind-build
flask --app app run --debug --port 5001
# 浏览器 http://localhost:5001/?layout=v2
```

期望：
- 状态栏左边 4 段 provider，圆点颜色按真实 provider state
- 右边 workers idle 或具体进度
- 等 60s 后 provider 段会重新拉一次（看 Network 面板有 GET /ui/status/providers）
- 等 5s 后 workers 段重新拉

- [ ] **Step 7: dev server 验收 status bar 动态数据**

### Step 8: Commit

```bash
git add routes/ tests/unit/test_ui_status.py app.py services/dedup.py
git commit -m "Phase A.3: /ui/status/* + /ui/sidebar/badges blueprint

- routes/ui_status.py blueprint: providers / workers / badges HTML fragments
- 5 contract tests: 0/N count behavior + provider state mapping + worker idle
- 共享 app._compute_providers_status / get_db / require_token (deferred import 防循环)
"
```

- [ ] **Step 8: Commit Task 3**

---

## Task 4: index.html 改 extends base.html + 删 topbar 老按钮

**Files:**
- Modify: `templates/index.html` (大规模简化)
- Modify: `app.py` (`/` 路由统一走 base.html，移除 `?layout=v2` 临时分支)

**Why this matters:** Task 1-3 是新建。这一步是**删除**——把老 topbar 8+ 按钮 + banner HTML 从 index.html 砍掉，剩下的老主区内容塞进 base.html 的 `{% block main %}`。12 modal 暂保留可用（Phase C 才删），但触发入口从 topbar 迁到主区按钮或暂时挂 sidebar 下方。

### Step 1: 先备份当前 index.html 行号区段

```bash
wc -l /Users/winson/Workspace/projects/nas/templates/index.html
# 期望: 2030 行
```

理清当前 index.html 大致结构：
- 上半（1-1100 行左右）：topbar + banner + 主区 + 各种 button
- 下半（1322-end）：12 个 modal HTML

策略：
- 删除：所有 `<header>` / topbar 内 button、banner div、global script 引用
- 保留：12 modal 整段（仍要工作）
- 新增包装：`{% extends "base.html" %}{% block main %}` 包主区

### Step 2: 读 index.html 顶部 200 行确定 topbar 位置

```bash
sed -n '1,50p' /Users/winson/Workspace/projects/nas/templates/index.html
```

找到 `<header>` / topbar / banner 起止行号 (假设是 `<header class="topbar">` 到 `</header>`)。

实际操作（用 Read 工具，不是 sed/cat — 跟 user rule 一致）：

```
Read templates/index.html offset=1 limit=200
```

记录 topbar 起止 / banner 起止 / 主区起止行号。

- [ ] **Step 2: Read 顶部确定 topbar / banner / 主区 边界**

### Step 3: 写新 index.html 模板（替换整个文件）

新 `templates/index.html` 结构：

```jinja2
{% extends "base.html" %}

{% block title %}文件 · NASVault{% endblock %}

{% block breadcrumb %}
<span>📁 文件</span>
{% endblock %}

{% block topbar_right %}
<span class="text-slate-500">老主区（Phase B 拆 page 后会重写）</span>
{% endblock %}

{% block main %}
<!-- ============ 老主区内容（保留功能）============ -->

<!-- 文件浏览器 / 库视图 / dedup 视图 / 操作按钮 / 等等 -->
<!-- 从原 index.html 主区段（line ~200 ~ 1300 区段）整体粘贴进来 -->

<!-- 操作入口（替代老 topbar 按钮）：在主区顶部加一行临时入口 -->
<div class="mb-4 flex gap-2 flex-wrap p-3 bg-slate-800 rounded">
    <button class="px-3 py-1 bg-slate-700 rounded text-sm" data-bs-toggle="modal" data-bs-target="#aiConfigModal">AI 配置</button>
    <button class="px-3 py-1 bg-slate-700 rounded text-sm" data-bs-toggle="modal" data-bs-target="#embyConfigModal">Emby</button>
    <button class="px-3 py-1 bg-slate-700 rounded text-sm" data-bs-toggle="modal" data-bs-target="#qbitConfigModal">qBit</button>
    <button class="px-3 py-1 bg-slate-700 rounded text-sm" data-bs-toggle="modal" data-bs-target="#nasConfigModal">NAS</button>
    <button class="px-3 py-1 bg-slate-700 rounded text-sm" data-bs-toggle="modal" data-bs-target="#organizeConfigModal">媒体库根</button>
    <button class="px-3 py-1 bg-slate-700 rounded text-sm" data-bs-toggle="modal" data-bs-target="#autoOrganizeConfigModal">自动整理</button>
    <button class="px-3 py-1 bg-slate-700 rounded text-sm" data-bs-toggle="modal" data-bs-target="#scanModal">全库扫描</button>
    <button class="px-3 py-1 bg-slate-700 rounded text-sm" data-bs-toggle="modal" data-bs-target="#batchIdentifyModal">批量识别</button>
    <span class="ml-auto text-xs text-slate-500">Phase A transitional — Phase B 把这些迁到 /settings + 各 page</span>
</div>

<!-- 老主区原有 HTML / 表格 / 浏览器 / library / dedup 全粘贴 -->
[原 index.html line N ~ M 的主区内容]

<!-- ============ 老 12 modal HTML（保留）============ -->
[原 index.html line 1322 ~ 2030 全部 modal 整段粘贴]

<!-- 加 Bootstrap 5 CSS/JS 以让老 modal 还能用（之前 index.html 应该已经引了） -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>

<!-- 老 app.js 仍要加载（修改 modal 触发后的 listener 仍生效） -->
<script src="{{ url_for('static', filename='app.js') }}"></script>
{% endblock %}
```

执行步骤（具体）：
1. Read `templates/index.html` 完整 2030 行
2. 找到 `<body>` 开始位置、topbar `</header>` 结束位置 → 主区开始
3. 找到主区结束位置（第一个 modal `<div class="modal ...` 之前）
4. 把主区 HTML 全 copy 进新 index.html `{% block main %}` 内
5. 把 12 modal HTML 全 copy 进 main block 内（放在主区后）
6. 删除原 index.html 的 topbar / banner / `<html>` `<head>` `<body>` outer 标签（base.html 已经有）

实际写文件需要把所有原 main + modal 内容粘贴进去。

- [ ] **Step 3: Read index.html → 切片 → Write 新 index.html（extends base + 包老内容 + 临时按钮）**

### Step 4: app.py / 路由清理 ?layout=v2 临时分支

去掉 Task 2 加的 `?layout=v2` 临时分支，让 `/` 直接渲染 index.html（现在它 extends base.html）：

```python
@app.route("/")
def index():
    # ... existing logic to load data ...
    return render_template("index.html", **context_data)
```

注意：传给 index.html 的 context 数据要 review — base.html 期望的 `app_version` 等如果不传会用 default。如果原 index.html 用了 query / context 数据，保留。

- [ ] **Step 4: 删 ?layout=v2 临时分支**

### Step 5: dev server 端到端冒烟（最关键的验收）

```bash
make tailwind-build
flask --app app run --debug --port 5001
```

浏览器 http://localhost:5001/ 验收：

**功能不退化（每条都跑）**：
- [ ] 文件浏览器：ls 一个目录正常显示
- [ ] 删一个文件：点删除 → 老 deleteModal 弹出 → preview 看影响 → confirm → 看 progress → 成功
- [ ] 识别一个文件：点识别 → 老 batchIdentifyModal 或单文件流程正常
- [ ] organize 一个文件：点整理 → 老 organizeModal 流程正常
- [ ] 配置入口：临时按钮区点 "AI 配置" / "qBit" / "NAS" 各 modal 能开关
- [ ] Status bar 底部仍显示 provider + workers
- [ ] Sidebar 6 项可见，hover 有效（点 nav 跳 / / /files / ... 都跳到老主区 — Phase B 拆才会有独立 page）

**视觉验收**：
- [ ] 整体深色（base.html bg-slate-900）
- [ ] Topbar 极薄 + breadcrumb
- [ ] Sidebar 左边深 + nav items
- [ ] 主区有"Phase A transitional" 提示行 + 临时按钮区
- [ ] Status bar 在底部

如果任一功能退化 → 退到 Task 4 重做，**不能 commit broken state**。

- [ ] **Step 5: 端到端冒烟 — 所有老功能仍工作 + status bar 仍动 + 老 modal 仍弹**

### Step 6: 跑全测试套件确认没破

```bash
pytest tests/ -v
```

期望全 PASS（Phase A 不应该破任何老测试）。

如果有 fail：
- 是不是 conftest.py 用了 `os.environ["NAS_BASE_PATH"]` 而 app 启动 load nas.json 覆盖了？这是 testing-and-fixtures.md 已知陷阱
- 修复后再 commit

- [ ] **Step 6: pytest tests/ -v 全 PASS**

### Step 7: Commit

```bash
git add templates/index.html app.py
git commit -m "Phase A.4: index.html 改 extends base.html + 删老 topbar

- index.html 改 {% extends 'base.html' %}{% block main %}
- 老 topbar 按钮 + banner HTML 删除
- 主区临时按钮区暂挂 6 个配置 modal 入口 (Phase B 拆 settings page 后会删)
- 12 modal HTML + Bootstrap CSS/JS + app.js 保留 (Phase C 才迁 drawer)
- transitional state: 新 layout + 老 modal 共存
"
```

- [ ] **Step 7: Commit Task 4**

---

## Task 5: codex review + 多轮收敛 + push

**Files:**
- (取决于 review 抓的 BLOCKER / IMPORTANT，按需修)

**Why this matters:** [[methodology]] 每个 Phase end 必跑 codex review。Phase A 涉及新 blueprint pattern + Tailwind 工具链引入 + layout 全换，review 期望抓 1-3 BLOCKER。

### Step 1: codex review r1

```bash
codex exec --skip-git-repo-check --cd /Users/winson/Workspace/projects/nas \
  "Review Phase A scaffolding commits (HEAD~3..HEAD):

  Spec ref: docs/superpowers/specs/2026-05-16-frontend-redesign-design.md Section 6 Phase A.

  Focus:
  1. Tailwind binary install script — platform mapping (macOS arm64/x64, Linux x64/aarch64) 是否完整? install_tailwind.sh 是否 idempotent? curl 失败有 fail-fast?
  2. base.html + partials 设计 — HTMX/Alpine CDN 引入是否安全（任意 user 浏览器都能 load）? base.html 是否暴露未鉴权的 HTMX poll endpoints (e.g. /ui/status/* 应该跟 /api/* 一致 require_token, 当前实现真有挂吗)?
  3. routes/ui_status.py — deferred import (避循环) 是否正确? require_token wrapper 是否真挂到 view? blueprint url_prefix=/ui 是否跟 spec 一致?
  4. _aggregate_running_workers / _compute_sidebar_badges — SQL 是否 safe (无 user input)? schema 字段是否存在 (organize_runs.total / done, scan_runs.total_files / processed_files, auto_organize_runs.status)?
  5. index.html 改 extends base.html 后, 老 12 modal 是否还在? Bootstrap CSS/JS 是否仍 inject? 临时按钮区 data-bs-toggle 是否正确?
  6. 测试覆盖 — 5 case 是否够? 有没有遗漏 (provider state==unreachable / scan_runs 跟 organize_runs 并存)?
  7. Transitional state ship 风险 — 这个 commit 可单独 ship 到 main 吗? 还是必须 feature branch 累积到 Phase F merge?
  Find BLOCKER / IMPORTANT / NIT." < /dev/null 2>&1 | grep -v "^2026-" | tail -100
```

- [ ] **Step 1: 跑 codex review r1**

### Step 2: 按 codex r1 findings 修 BLOCKER

按 [[methodology-review]] "多轮 codex review 收敛规律" — r1 抓 design 层 BLOCKER。逐条 fix：

(实际 fix 步骤 depends on codex 输出，写 plan 时不能预测；fix 后 commit 一个 fixup)

```bash
# 例（占位）：
git add ...
git commit -m "Phase A.5: codex r1 fixes (N BLOCKER + M IMPORTANT)"
```

- [ ] **Step 2: 修 r1 BLOCKER + commit fixup**

### Step 3: codex review r2 验证派生 fix 不引入新问题

```bash
codex exec --skip-git-repo-check --cd /Users/winson/Workspace/projects/nas \
  "Phase A.5 codex r2: review HEAD~1..HEAD for derived bugs introduced by r1 fixes.

  Specifically check whether fixes for BLOCKER 1..N opened new race / typing / scope issues.

  Find BLOCKER / IMPORTANT / NIT." < /dev/null 2>&1 | grep -v "^2026-" | tail -100
```

期望 r2 抓 0 BLOCKER（按 [[methodology-review]] r1 design / r2 implementation / r3 polish 收敛规律）。如果 r2 仍抓 BLOCKER → 修 → r3。

- [ ] **Step 3: codex review r2 + 如有 BLOCKER 修 + commit**

### Step 4: feature branch push (不 merge main)

按 spec Section 6 风险节："不要 ship transitional state 到公开 release"。Phase A 单独不能 merge main，必须累积到 Phase F merge。

```bash
git checkout -b feat/frontend-redesign-phase-a
git push -u origin feat/frontend-redesign-phase-a
```

或如果已经在 feature branch：

```bash
git push
```

不开 PR（等 Phase B/C/D/E/F 全部累积再开 1 个大 PR）。

- [ ] **Step 4: push feature branch（不 merge main，不开 PR）**

### Step 5: 最终验证 + 收尾

跑一次完整 sanity：

```bash
cd /Users/winson/Workspace/projects/nas
pytest tests/ -v
# 期望 全 PASS

make tailwind-build
# 期望 output.css 生成

flask --app app run --debug --port 5001
# 浏览器手动跑端到端冒烟（Task 4 Step 5 那份 checklist）
```

更新 spec / plan 文件：在 spec Section 6 Phase A 后面加 "(✓ shipped 2026-05-N, commit hash)"。

- [ ] **Step 5: pytest + tailwind build + 手动冒烟 + spec mark shipped**

---

## Self-Review (writing-plans skill 要求)

### 1. Spec coverage

| Spec Phase A 要求 | 对应 Task |
|---|---|
| 引入 Tailwind Standalone CLI + Makefile | Task 1 |
| 拆 templates/index.html 为 base.html + 4 partial | Task 2 + Task 4 |
| 实现 /ui/status/providers + /ui/status/workers | Task 3 |
| Sidebar 6 nav 项 + URL push | Task 2（sidebar HTML）+ Task 3（badges endpoint）|
| 删除老 topbar 按钮 | Task 4 |
| 老 modal 触发改用 sidebar / 主区按钮 | Task 4 临时按钮区 |
| 端到端冒烟 | Task 4 Step 5 + Task 5 Step 5 |
| codex review | Task 5 |

✅ 全覆盖。

### 2. Placeholder scan

无 "TBD" / "TODO" / "fill in details"。所有代码块完整可粘贴。

Task 5 Step 2 必然 depends on codex 输出无法预写具体 fix — 这是 inherent 不可预测，标注了 "实际 fix 步骤 depends on codex 输出"。这不算 placeholder（属于"必然要 runtime 决定的步骤"，类似 TDD 的 "see test fail before implementing"）。

### 3. Type consistency

- `_compute_providers_status()` raw dict key 跟 spec / `_to_status_segments` mapping 一致：`tmdb / deepseek / emby / qbit`
- `_aggregate_running_workers()` 返回 list[dict] with keys `kind / done / total / id` 跟 partials/status/workers.html template 一致
- `_compute_sidebar_badges()` 返回 dict with keys `library / dedup / organize` 跟 partials/sidebar/badges.html template 一致
- `Blueprint url_prefix="/ui"` + route paths `/status/providers` `/status/workers` `/sidebar/badges` → 完整 URL `/ui/status/providers` 等，跟 spec Section 3 / Section 4 一致

✅ 一致。

### 4. 跨 Task 依赖正确

- Task 1 必须先（其他都依赖 Tailwind binary 编译 output.css）
- Task 2 在 Task 1 之后（base.html 引用 output.css）
- Task 3 必须在 Task 2 之后（routes 渲染 partials/ 目录里的 template）
- Task 4 在 Task 3 之后（index.html extends base.html 时，base.html include 的 sidebar.html 期望 /ui/sidebar/badges endpoint 已经存在）
- Task 5 是收尾

✅ 依赖正确。

---

## Verification

完成 Phase A 后跑这套端到端 verification（也是 Task 5 Step 5 的内容）：

1. **Clean checkout 起栈测试**：在另一个目录 `git clone` 当前 feature branch + `./scripts/install_tailwind.sh && make tailwind-build && flask --app app run --debug` → 浏览器看到新 layout
2. **老功能不退化**：12 个 modal 都能开关；删除 / 整理 / 识别 / 配置 全流程
3. **新 layout 视觉**：sidebar / topbar / status bar / drawer (空) 各就位
4. **HTMX poll 真在跑**：浏览器 Network 面板看 GET /ui/status/providers (60s 一次) + GET /ui/status/workers (5s 一次) + GET /ui/sidebar/badges (60s 一次)
5. **MCP server 未受影响**：跑 mcp_server stdio 测一次 `prepare_destructive_action` 全流程
6. **全测试套件 PASS**：`pytest tests/ -v` 0 fail

---

## 下一步

- Phase A ship 后写 Phase B implementation plan (`docs/superpowers/plans/<date>-phase-B-pages.md`)
- Phase B 是真把 6 sidebar URL 拆成独立 page route + 模板（A 期 sidebar nav 跳的全是老主区，B 期才有真 page）
- Phase B 实施细节会受 A 实际实现影响，所以 **A ship 完才写 B plan**
