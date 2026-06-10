# NASVault 视觉重设计实施计划（方向 A · 媒体中心风）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按已批准 spec（`docs/superpowers/specs/2026-06-10-visual-redesign-design.md`）把全站视觉换成"媒体中心风"：深紫黑色板 + 渐变 + 线性图标，并修复 dashboard/settings 布局硬伤。

**Architecture:** 纯前端换肤。design tokens 集中在 `static/css/input.css`（CSS 变量 + `@layer components` 组件类）+ `tailwind.config.js` palette；模板只换 class/icon/结构；`app.js` 只动 render 函数里的 HTML 模板字符串；后端唯一改动是 `routes/ui_dashboard.py` activity 接口带 `poster_url`（读已有缓存列）和 `routes/pages_settings.py` 传配置状态 flags。

**Tech Stack:** Flask + Jinja2 + HTMX + Alpine.js + Tailwind standalone CLI + Bootstrap Icons（已加载，零新依赖）。

**全局约束（每个任务都遵守）：**
- 后端业务逻辑 / API / 路由零改动（上述两处 route 数据补充除外）
- `app.js` 只改模板字符串的 class 与 icon，不动逻辑
- Bootstrap CSS 必须保持在 Tailwind **之前**加载（base.html 现有顺序不动）
- 12 个 legacy modal 只在 `legacy.css` 换肤，不迁移
- 每个任务结束：`make tailwind-build` + 相关 pytest + Playwright 截图 + 独立 commit

**截图验证脚本（Task 1 创建，后续任务复用）：**
```bash
# 前置：dev server 在跑（.venv/bin/python -m flask --app app run --port 5001）
.venv/bin/python scripts/ui_screenshot.py            # 截全部 6 页到 /tmp/nas-ui-*.png
```

**测试注意：** `tests/unit/test_pages_routes.py` 和 `tests/unit/test_onboarding.py` 有约 10 条断言写死了 emoji 文案（如 `assert "🏠 概览" in html`）。删 emoji 的任务必须同步把断言改成纯文字（如 `assert "概览" in html`），这是合同测试跟随 UI 文案的正常更新，不是放松断言。

---

### Task 1: Design tokens + 全局壳（侧栏 / 状态栏 / 顶栏 / 光晕 / logo）

**Files:**
- Modify: `tailwind.config.js`
- Modify: `static/css/input.css`
- Modify: `templates/base.html`（header 配色 + 光晕层）
- Modify: `templates/_sidebar.html`、`templates/partials/sidebar/badges.html`
- Modify: `templates/_status_bar.html`
- Modify: `tests/unit/test_pages_routes.py`（emoji 断言 → 纯文字）
- Create: `scripts/ui_screenshot.py`

- [ ] **Step 1: 替换 `tailwind.config.js` palette**

```js
/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./static/**/*.js",
  ],
  safelist: [
    "sb-dot-ok",
    "sb-dot-warn",
    "sb-dot-err",
    "sb-dot-gray",
  ],
  theme: {
    extend: {
      colors: {
        // 方向 A 媒体中心风 palette（spec: 2026-06-10-visual-redesign-design.md）
        base: "#0d0b16",
        surface: { from: "#1a142f", to: "#130f22" },
        sidebar: {
          from: "#15102a",
          to: "#0d0b16",
          fg: "#9b91b8",
          "fg-active": "#d8cdf8",
          accent: "#a78bfa",
        },
        statusbar: {
          bg: "#0a0813",
          fg: "#8d80b5",
          ok: "#34d399",
          warn: "#fbbf24",
          err: "#f87171",
          gray: "#6f6590",
        },
        ink: {
          DEFAULT: "#e6e1f2",
          strong: "#f5f2fd",
          soft: "#9b91b8",
          mute: "#6f6590",
        },
        line: { DEFAULT: "#2a2150", faint: "#221a3e" },
        accent: { DEFAULT: "#a78bfa", deep: "#7c3aed", alt: "#d946ef", "alt-soft": "#f0abfc" },
      },
      borderRadius: { card: "16px" },
    },
  },
  plugins: [],
};
```

- [ ] **Step 2: 重写 `static/css/input.css`（tokens + 组件类）**

```css
@tailwind base;
@tailwind components;
@tailwind utilities;

@layer base {
  html { @apply text-sm; }
  body { @apply bg-base text-ink; }
}

@layer components {
  /* ===== 卡片语言（方向 A）===== */
  .nv-card {
    @apply rounded-card border border-line p-[18px];
    background: linear-gradient(145deg, theme(colors.surface.from), theme(colors.surface.to));
  }
  .nv-card-title {
    @apply text-[11px] font-bold uppercase tracking-[1.2px] text-ink-soft flex items-center gap-[7px] mb-3;
  }
  .nv-bignum {
    @apply text-[34px] font-extrabold leading-none;
    background: linear-gradient(90deg, #c4b5fd, #f0abfc);
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }
  .nv-chip {
    @apply inline-flex items-center gap-[5px] rounded-full px-[10px] py-[3px] text-[11px] text-accent mr-[6px];
    background: rgba(124, 58, 237, .16);
    border: 1px solid rgba(124, 58, 237, .3);
  }
  .nv-pill { @apply rounded-full px-[9px] py-[2px] text-[10px] font-semibold shrink-0; }
  .nv-pill-ok    { @apply nv-pill text-statusbar-ok;   background: rgba(52, 211, 153, .15); }
  .nv-pill-warn  { @apply nv-pill text-statusbar-warn; background: rgba(251, 191, 36, .14); }
  .nv-pill-err   { @apply nv-pill text-statusbar-err;  background: rgba(248, 113, 113, .14); }
  .nv-pill-gray  { @apply nv-pill text-ink-mute;       background: rgba(111, 101, 144, .15); }
  .nv-gradbar {
    @apply block h-full rounded-[3px];
    background: linear-gradient(90deg, #7c3aed, #d946ef);
  }
  /* 顶部氛围光晕（base.html main 区内一处） */
  .nv-glow {
    @apply pointer-events-none absolute -top-32 left-1/3 w-[600px] h-[300px];
    background: radial-gradient(ellipse, rgba(124, 58, 237, .18), transparent 70%);
  }
  /* ghost icon button（files 行操作等） */
  .nv-ghost-btn {
    @apply inline-flex items-center justify-center w-7 h-7 rounded-lg text-ink-mute
           hover:text-ink-strong hover:bg-white/5 transition-colors;
  }
  /* 工具栏胶囊按钮 */
  .nv-toolbar-btn {
    @apply inline-flex items-center gap-1.5 rounded-lg border border-line px-3 py-1.5 text-xs
           text-ink hover:border-accent/50 hover:text-ink-strong transition-colors;
    background: linear-gradient(145deg, theme(colors.surface.from), theme(colors.surface.to));
  }

  /* ===== Status bar ===== */
  .sb-seg { @apply px-3 py-1 inline-flex items-center gap-1 cursor-pointer hover:bg-white/5; }
  .sb-dot-ok    { @apply w-2 h-2 rounded-full bg-statusbar-ok; }
  .sb-dot-warn  { @apply w-2 h-2 rounded-full bg-statusbar-warn; }
  .sb-dot-err   { @apply w-2 h-2 rounded-full bg-statusbar-err; }
  .sb-dot-gray  { @apply w-2 h-2 rounded-full bg-statusbar-gray; }

  /* ===== Sidebar ===== */
  .nav-item {
    @apply flex items-center justify-between px-3 py-[9px] rounded-[9px] text-[13.5px]
           text-sidebar-fg hover:text-sidebar-fg-active hover:bg-white/5;
  }
  .nav-item.active {
    @apply text-sidebar-fg-active;
    background: linear-gradient(90deg, rgba(124, 58, 237, .28), rgba(124, 58, 237, .05));
    box-shadow: inset 2px 0 0 theme(colors.accent.DEFAULT);
  }
  .nav-badge {
    @apply text-[10.5px] font-semibold rounded-full px-2 py-0.5 text-sidebar-fg-active;
    background: rgba(124, 58, 237, .35);
    border: 1px solid rgba(167, 139, 250, .35);
  }
  .nav-icon { @apply w-4 text-center mr-2.5 inline-block; }
}
```

- [ ] **Step 3: `templates/_sidebar.html` — 渐变背景 + 渐变 logo 块**

```html
<aside class="w-56 flex flex-col border-r border-line-faint"
       style="background: linear-gradient(180deg, #15102a 0%, #0d0b16 60%)">
    <div class="flex items-center gap-2.5 px-4 py-4">
        <div class="w-[30px] h-[30px] rounded-[9px] flex items-center justify-center shrink-0"
             style="background: linear-gradient(135deg, #7c3aed, #d946ef); box-shadow: 0 4px 14px rgba(124,58,237,.4)">
            <i class="bi bi-hdd-stack text-white text-sm"></i>
        </div>
        <div class="text-base font-extrabold tracking-[.3px] text-ink-strong">NASVault</div>
    </div>

    <nav class="flex-1 px-2 py-2 space-y-[3px]" id="sidebar-nav">
        <!-- Server-render only (含 current_page active 高亮); 不 HTMX poll 防止抹 active class.
             每次 page navigate 都重新渲染 sidebar; badge stale 60s 可接受 trade-off. -->
        {% include "partials/sidebar/badges.html" %}
    </nav>

    <div class="px-4 py-2 text-xs text-ink-mute border-t border-line-faint">
        v{{ app_version | default('dev') }}
    </div>
</aside>
```

- [ ] **Step 4: `templates/partials/sidebar/badges.html` — emoji → bi 图标**

```html
<a href="/" class="nav-item {% if current_page == 'dashboard' %}active{% endif %}" hx-boost="true"><span><i class="bi bi-house-door nav-icon"></i>概览</span></a>
<a href="/files" class="nav-item {% if current_page == 'files' %}active{% endif %}" hx-boost="true"><span><i class="bi bi-folder2 nav-icon"></i>文件</span></a>
<a href="/library" class="nav-item {% if current_page == 'library' %}active{% endif %}" hx-boost="true">
    <span><i class="bi bi-collection-play nav-icon"></i>媒体库</span>
    {% if badges.library | default(0) > 0 %}<span class="nav-badge">{{ badges.library }}</span>{% endif %}
</a>
<a href="/dedup" class="nav-item {% if current_page == 'dedup' %}active{% endif %}" hx-boost="true">
    <span><i class="bi bi-search nav-icon"></i>重复检测</span>
    {% if badges.dedup | default(0) > 0 %}<span class="nav-badge">{{ badges.dedup }}</span>{% endif %}
</a>
<a href="/organize" class="nav-item {% if current_page == 'organize' %}active{% endif %}" hx-boost="true">
    <span><i class="bi bi-box-seam nav-icon"></i>整理</span>
    {% if badges.organize | default(0) > 0 %}<span class="nav-badge">{{ badges.organize }}</span>{% endif %}
</a>
<a href="/settings" class="nav-item {% if current_page == 'settings' %}active{% endif %}" hx-boost="true"><span><i class="bi bi-gear nav-icon"></i>设置</span></a>
```

- [ ] **Step 5: `templates/base.html` — header 配色 + 光晕 + main 区 relative**

只改两处（其余不动）：

```html
<!-- header 那行（原 bg-slate-800 border-slate-700）改为： -->
<header class="border-b border-line-faint px-4 py-2 flex items-center justify-between"
        style="background: rgba(13, 11, 22, .8)">
    <div class="text-ink-soft text-sm">
        {% block breadcrumb %}
        <span class="text-ink-mute">/</span>
        {% endblock %}
    </div>
    <div class="text-xs text-ink-mute">
        {% block topbar_right %}{% endblock %}
    </div>
</header>

<!-- main 容器（原 <div id="main-content" class="flex-1 overflow-auto p-4">）改为： -->
<div id="main-content" class="flex-1 overflow-auto p-5 relative">
    <div class="nv-glow"></div>
    {% block main %}{% endblock %}
</div>
```

- [ ] **Step 6: `templates/_status_bar.html` — 底色换 token**

```html
<footer class="bg-statusbar-bg text-statusbar-fg text-xs flex items-center border-t border-line-faint">
    <!-- 内部两个 hx div 原样保留，只把 border-slate-700 分隔线换成 border-line-faint -->
```

（status bar 内部结构不变，仅 `border-slate-700` → `border-line-faint`。）

- [ ] **Step 7: 各 page 模板 breadcrumb 去 emoji**

`templates/pages/*.html` 7 个文件的 `{% block breadcrumb %}` 里 `🏠 概览` / `📁 文件` / `🎬 媒体库` / `🔍 重复检测` / `📦 整理` / `⚙️ 设置` 等改为纯文字（如 `<span>概览</span>`）。onboarding 留给 Task 6。

- [ ] **Step 8: 更新 `tests/unit/test_pages_routes.py` emoji 断言**

把所有 `assert "🏠 概览" in html` 类断言改为纯文字 + 图标 class 断言，例如：

```python
assert "概览" in html
assert "bi-house-door" in html      # sidebar 图标存在
# "🗄️ NASVault" → assert "NASVault" in html
```

逐条对照：`🏠 概览`→`概览`、`📁 文件`→`文件`、`🎬 媒体库`→`媒体库`、`🔍 重复检测`→`重复检测`、`📦 整理`→`整理`、`⚙️ 设置`→`设置`、`🗄️ NASVault`→`NASVault`。

- [ ] **Step 9: 创建 `scripts/ui_screenshot.py`**

```python
"""Dev 工具：Playwright headless 截全部页面，视觉回归对照用。

用法：先起 dev server（.venv/bin/python -m flask --app app run --port 5001），
然后 .venv/bin/python scripts/ui_screenshot.py [输出目录，默认 /tmp]
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5001"
PAGES = [
    ("/", "dashboard"),
    ("/files", "files"),
    ("/library", "library"),
    ("/dedup", "dedup"),
    ("/organize", "organize"),
    ("/settings", "settings"),
]


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp")
    token = (Path(__file__).parent.parent / "config" / ".api_token").read_text().strip()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.add_init_script(f"localStorage.setItem('nas_token', '{token}');")
        for path, name in PAGES:
            page.goto(f"{BASE}{path}?token={token}", wait_until="domcontentloaded", timeout=15000)
            time.sleep(2.5)  # 等 HTMX load trigger + app.js fetch
            dest = out_dir / f"nas-ui-{name}.png"
            page.screenshot(path=str(dest))
            print(f"OK {dest}")
        browser.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 10: build + 测试 + 截图**

```bash
make tailwind-build                                   # 期望：无错，output.css 重新生成
.venv/bin/python -m pytest tests/unit/test_pages_routes.py tests/unit/test_ui_status.py tests/unit/test_toast.py -q
# 期望：全 PASS（断言已同步更新）
.venv/bin/python scripts/ui_screenshot.py             # 全站底色/侧栏/状态栏已换肤
```

- [ ] **Step 11: Commit**

```bash
git add tailwind.config.js static/css/input.css templates/ tests/unit/test_pages_routes.py scripts/ui_screenshot.py
git commit -m "feat(ui): 方向 A design tokens + 全局壳换肤 (侧栏/状态栏/光晕/logo, emoji 清零第一批)"
```

---

### Task 2: 概览页 bento 网格 + 海报缩略图

**Files:**
- Modify: `templates/pages/dashboard.html`
- Modify: `templates/partials/dashboard/todo_card.html`、`disk_card.html`、`library_stats_card.html`、`activity_card.html`
- Modify: `routes/ui_dashboard.py`（activity 带 poster_url）
- Test: `tests/unit/test_ui_dashboard.py`（如有；没有则跑 test_pages_routes 回归）

**注意：** `/ui/dashboard/system` 和 `/ui/dashboard/workers` 两个端点**保留不删**（合同测试在用；status bar 也展示同信息），只是 dashboard.html 不再引用。

- [ ] **Step 1: 重写 `templates/pages/dashboard.html` main block 为 bento 网格**

```html
{% extends "base.html" %}

{% block title %}概览 · NASVault{% endblock %}

{% block breadcrumb %}
<span>概览</span>
{% endblock %}

{% block main %}
<div class="space-y-5">
    <header>
        <h2 class="text-2xl font-extrabold text-ink-strong">概览</h2>
        <p class="text-xs text-ink-mute mt-1">媒体库 · 磁盘 · 待处理一览</p>
    </header>

    <div class="grid grid-cols-1 lg:grid-cols-[1.25fr_1fr_1fr] gap-4">
        <!-- 媒体库统计：左列跨 2 行 -->
        <div class="nv-card lg:row-span-2">
            <h3 class="nv-card-title"><i class="bi bi-collection-play text-accent"></i>媒体库</h3>
            <div hx-get="/ui/dashboard/library-stats" hx-trigger="load, every 60s" hx-swap="innerHTML">
                <p class="text-sm text-ink-mute italic">加载中…</p>
            </div>
        </div>

        <!-- 磁盘：圆环 -->
        <div class="nv-card text-center">
            <h3 class="nv-card-title justify-center"><i class="bi bi-device-hdd text-statusbar-warn"></i>磁盘空间</h3>
            <div hx-get="/ui/dashboard/disk" hx-trigger="load, every 60s" hx-swap="innerHTML">
                <p class="text-sm text-ink-mute italic">加载中…</p>
            </div>
        </div>

        <!-- 待处理：行动卡（原 系统状态/后台运行/待处理 三卡合并；前两者信息在底部 status bar） -->
        <div class="nv-card">
            <h3 class="nv-card-title"><i class="bi bi-check2-circle text-statusbar-ok"></i>待处理</h3>
            <div hx-get="/ui/dashboard/todo" hx-trigger="load, every 60s" hx-swap="innerHTML">
                <p class="text-sm text-ink-mute italic">加载中…</p>
            </div>
        </div>

        <!-- 最近活动：跨 2 列 -->
        <div class="nv-card lg:col-span-2">
            <h3 class="nv-card-title"><i class="bi bi-clock-history text-accent"></i>最近活动</h3>
            <div hx-get="/ui/dashboard/activity" hx-trigger="load, every 30s" hx-swap="innerHTML">
                <p class="text-sm text-ink-mute italic">加载中…</p>
            </div>
        </div>
    </div>

    {% include "_legacy_modals.html" %}
</div>
{% endblock %}
```

- [ ] **Step 2: `todo_card.html` — 行动卡样式**

现文件内容是徽章计数列表；改为行动行（保留原跳转目标和 badge 变量名）：

```html
<div class="space-y-2">
    {% if (badges.library | default(0)) == 0 and (badges.dedup | default(0)) == 0 and (badges.organize | default(0)) == 0 %}
    <div class="text-ink-mute text-xs italic">没有待处理事项 ✨ 全部处理完了</div>
    {% endif %}
    {% if badges.library | default(0) > 0 %}
    <a href="/library" hx-boost="true"
       class="flex items-center justify-between px-3 py-2.5 rounded-[11px] border transition-colors hover:border-accent/50"
       style="background: rgba(124,58,237,.1); border-color: rgba(124,58,237,.22)">
        <span class="text-[12.5px] text-ink">媒体库未识别</span>
        <span class="font-extrabold text-accent text-[15px]">{{ badges.library }} →</span>
    </a>
    {% endif %}
    {% if badges.dedup | default(0) > 0 %}
    <a href="/dedup" hx-boost="true"
       class="flex items-center justify-between px-3 py-2.5 rounded-[11px] border transition-colors hover:border-accent/50"
       style="background: rgba(124,58,237,.1); border-color: rgba(124,58,237,.22)">
        <span class="text-[12.5px] text-ink">重复检测待处理</span>
        <span class="font-extrabold text-accent text-[15px]">{{ badges.dedup }} →</span>
    </a>
    {% endif %}
    {% if badges.organize | default(0) > 0 %}
    <a href="/organize" hx-boost="true"
       class="flex items-center justify-between px-3 py-2.5 rounded-[11px] border transition-colors hover:border-accent/50"
       style="background: rgba(124,58,237,.1); border-color: rgba(124,58,237,.22)">
        <span class="text-[12.5px] text-ink">整理进行中</span>
        <span class="font-extrabold text-accent text-[15px]">{{ badges.organize }} →</span>
    </a>
    {% endif %}
</div>
```

（先读现有 `todo_card.html` 确认变量名与跳转，以现文件为准做等价改写——结构照上面。）

- [ ] **Step 3: `disk_card.html` — conic-gradient 圆环**

保持 `disks` / `error` 变量契约，每盘一个圆环：

```html
{% if error %}
<p class="text-xs text-statusbar-err">{{ error }}</p>
{% elif not disks %}
<p class="text-xs text-ink-mute italic">无磁盘信息</p>
{% else %}
<div class="flex flex-wrap gap-4 justify-center">
    {% for d in disks %}
    {% set ring_color = '#f87171' if d.use_percent >= 90 else ('#fbbf24' if d.use_percent >= 75 else '#34d399') %}
    <div class="text-center">
        <div class="w-[92px] h-[92px] rounded-full flex items-center justify-center mx-auto"
             style="background: conic-gradient({{ ring_color }} 0% {{ d.use_percent }}%, #221a3e {{ d.use_percent }}% 100%)">
            <div class="w-[70px] h-[70px] rounded-full flex flex-col items-center justify-center"
                 style="background: #161128">
                <span class="text-xl font-extrabold" style="color: {{ ring_color }}">{{ d.use_percent }}%</span>
                <span class="text-[9px] text-ink-mute">已用</span>
            </div>
        </div>
        <div class="text-[11px] text-ink-soft mt-2" title="{{ d.mount }}">
            {{ d.used }} / {{ d.size }} · 剩 <b style="color: {{ ring_color }}">{{ d.available }}</b>
        </div>
    </div>
    {% endfor %}
</div>
{% endif %}
```

- [ ] **Step 4: `library_stats_card.html` — 渐变大数字 + chip + 渐变评分条**

读现文件确认 `stats` 字段名（total / by_type / rating_dist / top_genres 之类），等价改写视觉：总数用 `<div class="nv-bignum">{{ stats.total }}</div>`；类型计数用 `.nv-chip`；评分分布行用：

```html
<div class="flex items-center gap-2 text-[11px] text-ink-soft mt-[7px]">
    <span class="w-7">8–9</span>
    <div class="flex-1 h-1.5 rounded-[3px] overflow-hidden" style="background:#221a3e">
        <span class="nv-gradbar" style="width: {{ pct }}%"></span>
    </div>
    <span>{{ count }}</span>
</div>
```

- [ ] **Step 5: `routes/ui_dashboard.py` — activity 带 poster_url**

在 `activity_card()` 的 organize 循环前加 helper，并给 organize items 附 `poster_url`（scan items 不带）：

```python
def _poster_for_content_path(conn, content_path: str | None) -> str | None:
    """content_path（目录或单文件）→ media_files.poster_url（已有缓存列，无新 API）。"""
    if not content_path:
        return None
    row = conn.execute(
        "SELECT poster_url FROM media_files WHERE path = ? AND poster_url IS NOT NULL LIMIT 1",
        (content_path,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT poster_url FROM media_files WHERE path LIKE ? || '/%' AND poster_url IS NOT NULL LIMIT 1",
            (content_path,),
        ).fetchone()
    return row["poster_url"] if row else None
```

organize 查询的 SELECT 加 `content_path` 列，item dict 加 `"poster_url": _poster_for_content_path(conn, row["content_path"])`；scan item 加 `"poster_url": None`。

- [ ] **Step 6: `activity_card.html` — 海报缩略图 + 状态药丸**

```html
<div>
    {% if not items %}
    <div class="text-ink-mute text-xs italic">暂无活动</div>
    {% else %}
    {% for it in items %}
    {% set ok_states = ['succeeded', 'done'] %}
    {% set warn_states = ['skipped_low_confidence', 'skipped_needs_identify', 'skipped_unsupported'] %}
    <div class="flex items-center gap-[11px] py-2 border-b last:border-b-0" style="border-color: rgba(42,33,80,.5)">
        {% if it.poster_url %}
        <img src="{{ it.poster_url }}" alt="" loading="lazy"
             class="w-[34px] h-12 rounded-md object-cover shrink-0"
             style="box-shadow: 0 3px 10px rgba(0,0,0,.5)">
        {% else %}
        <div class="w-[34px] h-12 rounded-md shrink-0 flex items-center justify-center text-ink-mute"
             style="background: linear-gradient(160deg, #2a2150, #161128)">
            <i class="bi {% if it.kind == 'organize' %}bi-box-seam{% else %}bi-search{% endif %} text-xs"></i>
        </div>
        {% endif %}
        <div class="flex-1 min-w-0">
            <div class="text-[12.5px] text-ink truncate" title="{{ it.label }}">{{ it.label }}</div>
            <div class="text-[10.5px] text-ink-mute mt-0.5">
                {% if it.ts_label %}{{ it.ts_label }}{% endif %}
                {% if it.ok or it.fail %} · {{ it.ok }} 成功{% if it.fail %} / {{ it.fail }} 失败{% endif %}{% endif %}
            </div>
        </div>
        {% if it.status in ok_states %}<span class="nv-pill-ok">✓ 已入库</span>
        {% elif it.status in warn_states %}<span class="nv-pill-warn">待确认</span>
        {% elif it.status in ['failed', 'aborted'] %}<span class="nv-pill-err">失败</span>
        {% elif it.status in ['running', 'organizing', 'pending'] %}<span class="nv-pill-gray">进行中…</span>
        {% else %}<span class="nv-pill-gray">{{ it.status }}</span>
        {% endif %}
    </div>
    {% endfor %}
    {% endif %}
</div>
```

- [ ] **Step 7: 测试 + 截图 + commit**

```bash
make tailwind-build
.venv/bin/python -m pytest tests/unit -q -k "dashboard or ui_status or pages_routes"
# 期望全 PASS；如有 activity 合同测试断言 emoji（📦/🔍），同步改为断言 bi- class
.venv/bin/python scripts/ui_screenshot.py
git add templates/pages/dashboard.html templates/partials/dashboard/ routes/ui_dashboard.py tests/
git commit -m "feat(ui): 概览页 bento 网格 + 磁盘圆环 + 活动海报缩略图 (三张半空卡合并)"
```

---

### Task 3: 文件页（工具栏 / 行操作 / 右侧面板）

**Files:**
- Modify: `templates/pages/files.html`
- Modify: `static/app.js`（仅 render 模板字符串）

- [ ] **Step 1: `files.html` 工具栏按钮统一 `.nv-toolbar-btn`**

读现文件，把顶部按钮区每个按钮改为（保留全部 onclick 与 id）：

```html
<button type="button" class="nv-toolbar-btn" onclick="showAIConfig()">
    <i class="bi bi-robot"></i>AI 配置
</button>
```

emoji→icon 映射：AI 配置`bi-robot`、Emby`bi-display`、qBit`bi-globe2`、NAS`bi-hdd-network`、媒体库根`bi-folder-symlink`、自动整理`bi-magic`、全库扫描`bi-arrow-repeat`、批量识别`bi-stars`、刷新`bi-arrow-clockwise`。

- [ ] **Step 2: `app.js` 文件行操作按钮 → ghost icon**

在 `app.js` 中 grep 渲染文件行的函数（`renderFiles` / 行模板字符串含 `btn-outline-success`、`btn-outline-danger` 或行内三个彩色小按钮的位置）。把行操作按钮的 class 换成 `nv-ghost-btn`，icon 保留语义：

```js
// 旧（示意，以实际 grep 结果为准）：
// `<button class="btn btn-sm btn-outline-danger" onclick="deleteSingle(...)">🗑</button>`
// 新：
`<button class="nv-ghost-btn" title="删除" onclick="deleteSingle(${idx})"><i class="bi bi-trash3"></i></button>`
```

同一行三个操作（进入/识别/删除）统一此样式；删除按钮 hover 态加 `hover:text-statusbar-err`。**只改 class 与 icon 字符串，事件与参数原样。**

- [ ] **Step 3: 右侧"已选择"面板 + 操作日志套卡片**

`files.html` 右侧面板容器 class 改 `nv-card`（去掉原 slate 底色 class），内部"已选择 0 / 0.0 B"数字用 `text-2xl font-extrabold text-ink-strong`，删除/清除按钮用 `.nv-toolbar-btn`（删除加 `text-statusbar-err`）。

- [ ] **Step 4: 测试 + 截图 + commit**

```bash
make tailwind-build
.venv/bin/python -m pytest tests/unit -q -k "pages_routes or files"
.venv/bin/python scripts/ui_screenshot.py
git add templates/pages/files.html static/app.js
git commit -m "feat(ui): 文件页工具栏胶囊化 + 行操作 ghost icon + 选择面板换肤"
```

---

### Task 4: 媒体库 + 重复检测

**Files:**
- Modify: `templates/pages/library.html`、`templates/pages/dedup.html`（壳很薄，主要在 app.js）
- Modify: `static/app.js`（library / dedup render 函数）

- [ ] **Step 1: 库海报卡 hover 浮起 + 渐变遮罩标题**

grep `app.js` 中渲染库卡片的函数（库 grid 卡片模板字符串）。卡片容器加：

```js
`<div class="group relative rounded-xl overflow-hidden cursor-pointer
             transition-transform duration-200 hover:-translate-y-1 hover:shadow-[0_8px_30px_rgba(0,0,0,.6)]" ...>
    <img src="${posterUrl}" loading="lazy" class="w-full aspect-[2/3] object-cover">
    <div class="absolute inset-x-0 bottom-0 px-2 pt-8 pb-2"
         style="background: linear-gradient(180deg, transparent, rgba(13,11,22,.92))">
        <div class="text-xs font-semibold text-ink-strong truncate">${title}</div>
        <div class="text-[10px] text-ink-soft">${year}${rating ? ' · ⭐' + rating : ''}</div>
    </div>
 </div>`
```

（以现有模板字符串为基础改 class/结构，绑定的 onclick/data 属性原样保留；星标如原本是文本符号可保留。）

- [ ] **Step 2: 筛选条 / 搜索框统一输入样式**

library/dedup 顶部筛选控件 class 统一为：

```
bg-transparent border border-line rounded-lg px-3 py-1.5 text-xs text-ink
focus:border-accent/60 focus:outline-none
```

- [ ] **Step 3: dedup 组卡片 + 质量评分渐变条 + 徽章药丸**

dedup render 函数中：组容器 → `nv-card`（JS 字符串里直接写 class）；质量评分横条 → 外层 `h-1.5 rounded-[3px] overflow-hidden` + 内层 `nv-gradbar`；"已看"/"最佳"等徽章 → `nv-pill-ok` / `nv-pill-warn` / `nv-pill-gray`。

- [ ] **Step 4: 测试 + 截图 + commit**

```bash
make tailwind-build
.venv/bin/python -m pytest tests/unit -q -k "pages_routes or dedup or library"
.venv/bin/python scripts/ui_screenshot.py
git add templates/pages/library.html templates/pages/dedup.html static/app.js
git commit -m "feat(ui): 媒体库海报卡 hover 遮罩 + dedup 卡片/评分条/徽章换肤"
```

---

### Task 5: 设置页（2 列大卡 + 配置状态徽章）

**Files:**
- Modify: `routes/pages_settings.py`（传配置状态 flags）
- Modify: `templates/pages/settings.html`
- Test: `tests/unit/test_pages_routes.py`（settings 部分）

- [ ] **Step 1: `pages_settings.py` 计算配置状态**

```python
"""Page: /settings — settings hub (7 cards, each triggers config modal)."""

from __future__ import annotations

import os

from flask import Blueprint, make_response, render_template

pages_settings_bp = Blueprint("pages_settings", __name__)


def _config_status() -> dict:
    """各配置卡的"已配置"判定 — 全部读现有 loader，不加新 API。

    失败一律按未配置处理（设置页永不因状态计算 500）。
    """
    from app import (
        CONFIG_DIR,
        _emby_client,
        load_deepseek_key,
        load_organize_config,
        load_qbit_auto_organize_config,
        load_tmdb_key,
        qbit,
    )

    status: dict[str, bool] = {}
    try:
        status["nas"] = os.path.exists(os.path.join(str(CONFIG_DIR), "nas.json"))
    except Exception:
        status["nas"] = False
    try:
        cfg = qbit.get_config()
        status["qbit"] = bool(cfg.get("url") and cfg.get("user") and cfg.get("has_password"))
    except Exception:
        status["qbit"] = False
    try:
        status["ai"] = bool(load_tmdb_key()) and bool(load_deepseek_key())
    except Exception:
        status["ai"] = False
    try:
        status["emby"] = _emby_client() is not None
    except Exception:
        status["emby"] = False
    try:
        org = load_organize_config()
        status["roots"] = bool(
            (org.get("movies_root") or org.get("MOVIES_ROOT"))
            and (org.get("tv_root") or org.get("TV_ROOT"))
        )
    except Exception:
        status["roots"] = False
    try:
        status["auto_organize"] = bool(load_qbit_auto_organize_config().get("enabled"))
    except Exception:
        status["auto_organize"] = False
    return status


@pages_settings_bp.route("/settings")
def settings():
    resp = make_response(
        render_template(
            "pages/settings.html",
            current_page="settings",
            badges={},
            cfg_status=_config_status(),
        )
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp
```

**执行时先验证字段名**：`load_organize_config()` 的 roots key 名跑一次确认：
`.venv/bin/python -c "import app; print(app.load_organize_config())"`，按实际 key 收紧上面的 `or` 双保险写法。

- [ ] **Step 2: `settings.html` — 2 列大卡**

7 张卡统一此结构（保留各自 onclick；icon/语义色按映射表）：

```html
{% macro cfg_badge(ok) %}
{% if ok %}<span class="nv-pill-ok">✓ 已配置</span>{% else %}<span class="nv-pill-gray">未配置</span>{% endif %}
{% endmacro %}

<div class="grid grid-cols-1 md:grid-cols-2 gap-4 max-w-4xl">
    <button type="button" class="nv-card text-left hover:border-accent/50 transition-colors flex items-start gap-4"
            onclick="showNASConfig()">
        <span class="w-11 h-11 rounded-xl flex items-center justify-center shrink-0"
              style="background: rgba(167,139,250,.15)">
            <i class="bi bi-hdd-network text-accent text-xl"></i>
        </span>
        <span class="flex-1 min-w-0">
            <span class="flex items-center justify-between gap-2">
                <span class="text-base font-bold text-ink-strong">NAS</span>
                {{ cfg_badge(cfg_status.nas) }}
            </span>
            <span class="block text-xs text-ink-soft mt-1">SSH host + port + 沙箱根路径</span>
        </span>
    </button>
    <!-- 其余 6 张同结构 -->
</div>
```

7 卡映射表（icon / 徽章底色 / 状态 key）：

| 卡 | onclick | icon | icon 色 / 底 | cfg_status key |
|---|---|---|---|---|
| NAS | `showNASConfig()` | `bi-hdd-network` | `text-accent` / `rgba(167,139,250,.15)` | `nas` |
| qBittorrent | `showQBitConfig()` | `bi-globe2` | `text-statusbar-ok` / `rgba(52,211,153,.12)` | `qbit` |
| AI (TMDB + DeepSeek) | `showAIConfig()` | `bi-robot` | `text-accent-alt-soft` / `rgba(217,70,239,.12)` | `ai` |
| Emby | `showEmbyConfig()` | `bi-display` | `text-statusbar-warn` / `rgba(251,191,36,.12)` | `emby` |
| 媒体库根目录 | `showOrganizeConfig()` | `bi-folder-symlink` | `text-accent` / `rgba(167,139,250,.15)` | `roots` |
| 自动整理 | `showAutoOrganizeConfig()` | `bi-magic` | `text-accent-alt-soft` / `rgba(217,70,239,.12)` | `auto_organize` |
| 数据库工具 | `openScanModal()` | `bi-database-gear` | `text-ink-soft` / `rgba(111,101,144,.15)` | （无徽章） |

- [ ] **Step 3: 测试 + 截图 + commit**

```bash
make tailwind-build
.venv/bin/python -m pytest tests/unit -q -k "pages_routes or settings"
# settings 合同测试如断言旧文案（"📡 NAS"），同步改纯文字断言
.venv/bin/python scripts/ui_screenshot.py
git add routes/pages_settings.py templates/pages/settings.html tests/
git commit -m "feat(ui): 设置页 2 列大卡 + 彩色 icon 徽章 + 配置状态徽章 (修空旷)"
```

---

### Task 6: 收尾（legacy modal 换肤 / onboarding / toast / emoji 清零验证）

**Files:**
- Modify: `static/css/legacy.css`（追加 modal 换肤段，不删现有规则）
- Modify: `templates/pages/onboarding.html`、`templates/pages/organize.html`
- Modify: `templates/base.html`（toast icon → bi）
- Modify: `tests/unit/test_onboarding.py`
- Modify: `static/app.js`（剩余 emoji 扫尾）

- [ ] **Step 1: `legacy.css` 末尾追加 modal 换肤段**

```css
/* ===== 视觉重设计（方向 A）: legacy Bootstrap modal 轻换肤 =====
   只覆盖配色对齐新色板；modal → drawer 迁移是 Phase F 的事，此处不动结构。 */
.modal-content {
  background: linear-gradient(145deg, #1a142f, #130f22) !important;
  border: 1px solid #2a2150 !important;
  border-radius: 16px !important;
  color: #e6e1f2 !important;
}
.modal-header { border-bottom-color: #221a3e !important; }
.modal-footer { border-top-color: #221a3e !important; }
.modal-content .form-control,
.modal-content .form-select {
  background: #0d0b16 !important;
  border-color: #2a2150 !important;
  color: #e6e1f2 !important;
}
.modal-content .form-control:focus,
.modal-content .form-select:focus {
  border-color: #a78bfa !important;
  box-shadow: 0 0 0 3px rgba(167, 139, 250, .15) !important;
}
.modal-content .btn-primary {
  background: linear-gradient(90deg, #7c3aed, #d946ef) !important;
  border: none !important;
}
.modal-backdrop.show { opacity: .7 !important; }
```

- [ ] **Step 2: onboarding / organize 页 emoji → bi 图标 + 同步测试断言**

`onboarding.html` 的 `👋 欢迎` 等、`organize.html` breadcrumb 残留 emoji 全部换 bi 图标或纯文字。`tests/unit/test_onboarding.py` 的 `assert "👋 欢迎" in html` → `assert "欢迎" in html`。

- [ ] **Step 3: base.html toast icon → bi**

`iconFor()` 返回值从 `'✓'/'⚠'/'❌'/'ℹ'` 改为 bi class，模板 span 改用 `:class`：

```html
<i :class="iconFor(t.severity)" class="text-lg leading-none"></i>
```

```js
iconFor(severity) {
    return {
        success: 'bi bi-check-circle',
        warning: 'bi bi-exclamation-triangle',
        error: 'bi bi-x-circle',
        info: 'bi bi-info-circle',
    }[severity] || 'bi bi-info-circle';
},
```

关闭按钮 `✕` → `<i class="bi bi-x"></i>`。`tests/unit/test_toast.py` 如断言 icon 文本则同步更新。

- [ ] **Step 4: 全站 emoji 清零验证**

```bash
.venv/bin/python -c "
import re, pathlib
pat = re.compile(r'[\U0001F300-\U0001FAFF☀-➿]')
hits = []
for p in list(pathlib.Path('templates').rglob('*.html')) + [pathlib.Path('static/app.js')]:
    for i, l in enumerate(p.read_text().splitlines()):
        if pat.search(l):
            hits.append(f'{p}:{i+1}: {l.strip()[:80]}')
print('\n'.join(hits) or 'CLEAN')
"
# 期望：CLEAN（或仅剩注释里的非 UI 命中，逐条确认）
# app.js 模板字符串里剩余的 emoji（如 addLog 前缀、按钮文案）逐个换 bi 图标或删除
```

- [ ] **Step 5: 全量回归 + 全页截图 + commit**

```bash
make tailwind-build
.venv/bin/python -m pytest tests/unit -q          # 期望：744+ 全 PASS
.venv/bin/python scripts/ui_screenshot.py /tmp/redesign-final
git add static/css/legacy.css templates/ static/app.js tests/
git commit -m "feat(ui): 收尾 — legacy modal 换肤 + onboarding/toast 图标化 + 全站 emoji 清零"
```

- [ ] **Step 6: 真浏览器手动 smoke（headless 测不出的部分）**

用户在真 Chrome 过一遍：modal 打开表单有值、删除 drawer 流程、status bar 5s 轮询、海报真图加载。参照 `docs/testing/` 既有 e2e checklist 的 TC 项。

---

## Self-Review 记录

- **Spec 覆盖**：tokens(T1) / 概览 bento+海报(T2) / 文件(T3) / 媒体库+dedup(T4) / 设置(T5) / modal 换肤+onboarding+toast+emoji 清零(T6) — spec 六个单元全覆盖 ✓
- **Placeholder**：app.js 的 render 函数名因 4825 行文件未逐一列出，每处都给了 grep 锚点（class 名 / 函数语义）+ 完整目标代码模式；执行者照锚点定位后做 class/icon 等价替换 ✓
- **类型一致性**：组件类名（nv-card / nv-chip / nv-pill-* / nv-gradbar / nv-ghost-btn / nv-toolbar-btn / nav-icon）在 T1 定义、T2-T6 引用一致 ✓
- **风险点**：① `load_organize_config()` roots key 名未实证 — T5 Step 1 已写验证命令；② 合同测试断言文案 — 各任务步骤已含同步更新；③ `bg-statusbar-*` 等旧 token 在模板/JS 其他位置的引用 — T1 的 palette 保留了 `statusbar.*` 同名结构（值换新），旧引用自动拿到新色，不会 build 错
