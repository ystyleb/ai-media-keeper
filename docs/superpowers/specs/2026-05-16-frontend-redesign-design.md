# NASVault 前端重设计

> 状态：**Design approved (2026-05-16)**，等待实施。Phase A 起手。
> 协作历史：brainstorming skill 6 段逐段确认；ExitPlanMode 通过。

## Context

NASVault 前端是单页应用：`templates/index.html` (2030 行) + `static/app.js` (4771 行) + 12 个 Bootstrap modal 堆在 topbar。Phase 5 ship 后 user 决定开源前彻底重做（big bang）。

**Anchored 决策**（已跟 user 确认）：
- **受众**：开源社区（家庭 NAS / PT 用户），参考 Jellyfin / Plex / Sonarr
- **核心痛点**：① IA — topbar 按钮堆多、主页主次不清 ② Modal 多层嵌套迷失感 ③ 状态信号杂（不在意视觉老土 / 不在意手机适配）
- **技术栈**：Flask + HTMX + Alpine.js + Tailwind CSS Standalone CLI（零 Node、零 build chain、`./run.sh` 起栈）
- **顶层 IA**：Sidebar + 多页（URL 可书签 / `hx-boost` 不闪屏）
- **Destructive action 形态**：右侧 drawer（preview → confirm → progress 同一 drawer 切 step；契约 #1 后端不变）
- **状态信号**：sidebar nav badge + 底部 status bar（VS Code 风）

**后端契约不变**（这次重做不动后端）：
- 契约 #1 destructive action 双段式 `/api/action/preview` + `/api/action/confirm` + signed token
- 契约 #5 AI 介入边界（confidence gate / category allowlist）
- `/api/providers/status` 60s TTL
- 所有 destructive 用 inode-anchored execute 防 TOCTOU
- API token Bearer auth

---

## Section 1: 整体架构 + Sidebar IA

### 视觉骨架

```
+----------+-----------------------------------------------+
|          |  breadcrumb · 当前页 title       provider hint|  ← topbar (薄)
| LOGO     +-----------------------------------------------+
|          |                                               |
| 🏠 概览  |                                               |
| 📁 文件  |          主区 (HTMX swap target)              |
| 🎬 媒体库·12 |                                          |
| 🔍 重复·5    |                                          |
| 📦 整理·3    |                                          |
| ⚙️ 设置      |                                          |
|              |                                          |
|              |                                          |
+----------+-----------------------------------------------+
| 🟢 TMDB · 🟢 DeepSeek · 🟡 Emby · 🟢 qBit · ⏳ organize 2/3 ·  scanner idle |
+--------------------------------------------------------------------+
                       ↑ 底部固定 status bar
```

### Sidebar nav 项 + URL 映射

| Nav 项 | URL | Badge 数据来源 | 内容 |
|---|---|---|---|
| 🏠 概览 | `/` | (无 badge，dashboard 本身就是聚合) | 后台 worker / provider / 需关注的 dedup / 最近 identify / qBit 完成等卡片 |
| 📁 文件 | `/files` | (无) | SSH 目录树 + 文件操作（删除/识别/organize），现在的主页内容 |
| 🎬 媒体库 | `/library` | `media_files where needs_identify=true` 计数 | TMDB 海报墙 + 筛选 + 搜索 + 详情 panel |
| 🔍 重复检测 | `/dedup` | dedup groups 待处理 | groups 列表 + quality 评分 + 已看候选 |
| 📦 整理 | `/organize` | `auto_organize_runs where status in (pending, organizing)` 计数 | tab 1: 历史 / tab 2: 自动整理配置 |
| ⚙️ 设置 | `/settings` | (无) | 子页：NAS · qBit · AI · Emby · 媒体库根 · 数据库工具 |

### 现有 12 个 modal 的去向

| 现有 modal | 新归宿 |
|---|---|
| `deleteModal` | 右侧 drawer (destructive preview/confirm/progress) |
| `organizeModal` (单文件) | 右侧 drawer |
| `organizeBatchModal` | 右侧 drawer (preview / progress 同 drawer step 切换) |
| `nfoWriteModal` | 右侧 drawer |
| `batchIdentifyModal` | 右侧 drawer (库视图触发) |
| `nasConfigModal` | `/settings/nas` 子页 |
| `qbitConfigModal` | `/settings/qbit` 子页 |
| `aiConfigModal` | `/settings/ai` 子页 |
| `embyConfigModal` | `/settings/emby` 子页 |
| `organizeConfigModal` (MOVIES_ROOT/TV_ROOT) | `/settings/library-roots` 子页 |
| `autoOrganizeConfigModal` (配置 tab) | `/settings/auto-organize` 子页 |
| `autoOrganizeConfigModal` (历史 tab) | `/organize` (历史 tab) |
| `scanModal` (全库扫描) | `/settings/database` 子页里的 "rescan" 按钮 |

**统计**：12 modal → 0 modal + 6 sidebar pages + 5 destructive drawers。

### Drawer 行为规范

- 右侧 drawer 宽度：768px (中等屏) / 50% (大屏) / 100% (< 768px)
- 同时只能 1 个 drawer（开第 2 个 → 提示先完成当前）
- Esc / 点 backdrop 关；如果有未确认的 destructive token，关之前确认（防误关丢 token）
- Drawer 内 step（preview → confirm → progress）用 `hx-swap-oob` 替换 drawer 主区
- Drawer URL 反映在 query string：`?drawer=action/<id>`，刷新可恢复（destructive_actions 表已经持有 pending state）

---

## Section 2: 单页 wireframe

> 6 页全部 condensed。每页关键内容 + HTMX/Alpine 关键交互点。

### 2.1 🏠 概览 / Dashboard (`/`)

```
breadcrumb: 概览

┌─ 系统状态 ──────────────┬─ 后台运行中 ─────────────┐
│ Provider:                │ ⏳ organize batch        │
│  🟢 TMDB · 🟢 DeepSeek   │     "S2 全季" · 12/40    │
│  🟡 Emby (401) · 🟢 qBit │     [打开 drawer]        │
│  [设置 →]                │ ⏳ scanner · 234/1797    │
└──────────────────────────┴──────────────────────────┘

┌─ 待你处理 ─────────────────────────────────────────┐
│ 🔍 dedup 待处理 5 组    [去看 →]                    │
│ 🎬 媒体库 12 个未识别   [去看 →]                    │
│ 📦 organize 失败 1     [去看 →]                    │
└────────────────────────────────────────────────────┘

┌─ 最近活动 ─────────────────────────────────────────┐
│ 16:42  ✅ identified "Lost.Highway.1997" → 1997 movie│
│ 16:38  ⚠️  organize skipped "S03" (low confidence)   │
│ 16:31  🗑️  deleted 3 files · 12.4 GB freed          │
└────────────────────────────────────────────────────┘
```

**关键交互**：所有卡片用 `hx-get` + `hx-trigger="load, every 30s"` 自动刷新；点卡片 `hx-boost` 跳子页。

### 2.2 📁 文件 (`/files`)

```
breadcrumb: 文件 / share / CACHEDEV2_DATA / Media

[+ 新建目录] [↑ 上级] [筛选: ☐ 仅视频]      搜索 [_____]

┌─ 当前路径下 ────────────────────────────────────┐
│ ☐  名称              大小    硬链接  操作        │
│ ☐  📁 Movies         -       -      [整理目录]   │
│ ☐  📁 TV             -       -      [整理目录]   │
│ ☐  🎬 a.mkv          5.2G    3      [识别][删除] │
│ ☐  🎬 b.mkv          8.1G    1      [识别][删除] │
└─────────────────────────────────────────────────┘
[批量: 删除 / 整理 / 识别]    选 0 项
```

**关键交互**：勾选触发 Alpine 计数；批量按钮启用 `:disabled="count===0"`；点"识别"`hx-get="/api/identify/<path>"` swap 行内联展开识别结果（不开 drawer，因为识别非 destructive）；点"删除/整理"开 drawer。

### 2.3 🎬 媒体库 (`/library`)

```
breadcrumb: 媒体库 · 1797 项 (12 未识别)

┌─ 筛选 ────────────────────────────────────────┐
│ [全部] [电影] [剧集]   评分 ≥ [_]    年份 [_]  │
│ 状态 [全部 ▾]   [⚠️ 仅未识别]    搜索 [____]   │
└───────────────────────────────────────────────┘

┌────┬────┬────┬────┬────┬────┐
│ 🎬 │ 🎬 │ 🎬 │ 🎬 │ 🎬 │ 🎬 │   ← 海报墙 (CSS grid)
│ 教父│ 1917│ ...│    │    │    │
│'72  │'19  │    │    │    │    │
│⭐9.2│⭐8.3│    │    │    │    │
└────┴────┴────┴────┴────┴────┘
[加载更多]   (auto-fill 进入页时连拉到 has_more=false)
```

**点海报** → URL `/library/<id>` swap 详情 panel 在主区（不是 drawer，因为详情是浏览不是 action）。详情里"识别 / 改绑 / 写 NFO / 整理"按钮点开 drawer。

### 2.4 🔍 重复检测 (`/dedup`)

```
breadcrumb: 重复检测 · 5 组待处理 · 已看可归档 23

[Tab: 重复版本 (5) | 已看待归档 (23)]

┌─ 当前 tab: 重复版本 ──────────────────────────┐
│ ▾ "Breaking Bad" S05E14 · 3 份                │
│   ⭐ 推荐保留: 2160p.HDR.WEB-DL (评分 92)      │
│   保留版本：[2160p.HDR ▾]                     │
│   待删: 1080p.WEB-DL (28G) + 720p.HEVC (4G)   │
│   [预览删除 →]  [快速取舍 / 调权重]            │
│ ▸ "Westworld" S04E08 · 2 份                   │
│ ▸ ...                                         │
└───────────────────────────────────────────────┘
```

**关键**：展开/折叠用 Alpine `x-data`；权重调节弹小 popover 直接 fetch `/api/dedup/weights`；"预览删除" → drawer。

### 2.5 📦 整理 (`/organize`)

```
breadcrumb: 整理

[Tab: 历史 (43) | 自动整理配置]

历史 tab：
┌─ 状态筛选 [全部 ▾]    [搜种子名/分类] ───────┐
│ 时间    状态       种子名 / 分类     文件统计 │
│ 16:38   ⚠️ skip    "S03 整季"/tv    -        │
│ 16:31   ✅ ok      "电影合集"/movie  4/0/0    │
│ 15:50   ❌ fail    "Lost"/movie     0/0/2    │
│ ...                                          │
└──────────────────────────────────────────────┘
[每行点开] → 右侧 drawer 看 result_json 详情；terminal 行有 [reset] 按钮（contract: terminal-only）

自动整理配置 tab：
┌─ 启用 [○○○] ───────────────────────────────┐
│ qBit category 白名单: [movies][tv][+加]      │
│ cron 周期: [5] 分钟    confidence 门槛: [.85] │
│ [保存]                                       │
│                                              │
│ 说明: cron 周期到点 → 扫 category 命中 →     │
│ confidence ≥ 阈值 → atomic hardlink 到媒体库 │
└──────────────────────────────────────────────┘
```

### 2.6 ⚙️ 设置 (`/settings/<sub>`)

```
breadcrumb: 设置 · NAS

侧边 sub-nav (settings 二级 sidebar)：
├ NAS (SSH)
├ qBittorrent
├ AI (TMDB + DeepSeek)
├ Emby
├ 媒体库根目录 (MOVIES_ROOT / TV_ROOT)
├ 自动整理
└ 数据库工具 (rescan / vacuum / 导出 NFO 批量)

NAS sub-page：
┌──────────────────────────────────────┐
│ SSH host    [admin@192.168.1.100  ]  │
│ SSH 端口    [22                    ] │
│ 沙箱根路径  [/share/CACHEDEV2_DATA ] │
│ [测试连接]  [保存]                   │
│                                      │
│ 状态: 🟢 last ok 2026-05-16 16:42    │
└──────────────────────────────────────┘
```

**Settings 走两层**：左 sidebar 永远显示主 nav，主区里 settings 自己再有一层 sub-nav。这是嵌套但不是模态嵌套，跟 user 痛点不冲突（路由可见 / URL 可恢复 / 任何时刻看 breadcrumb 知道在哪）。

## Section 3: Drawer flow + HTMX 数据流

### 3.1 双套路由设计：`/api/*` JSON + `/ui/*` HTML

后端契约不变，但 **HTMX 期望 HTML fragment swap**。Pattern：

| 调用方 | URL | 返回 | 谁用 |
|---|---|---|---|
| MCP server / 脚本 / 第三方 | `/api/action/preview` | JSON | 不动 |
| 浏览器 HTMX | `/ui/drawer/delete/preview` | HTML fragment | 新增 |

`/ui/*` 路由实现 = 内部调 `/api/*` JSON API → 拿 data → `render_template('drawer/...partial.html')` → 返 HTML。**单一 data source，双输出格式。**

### 3.2 Destructive drawer 三阶段 flow（以 delete 为例）

**Step 1: User 触发**
```
/files 页面，user 勾 3 个文件 → 点 [删除]

HTML:
<button hx-post="/ui/drawer/delete/preview"
        hx-vals='{"paths": $selected}'
        hx-target="#drawer"
        hx-swap="innerHTML"
        hx-push-url="true">删除 3 项</button>
```

**Step 2: Drawer preview 渲染**
```
Server:
  1. 收 POST /ui/drawer/delete/preview {paths}
  2. 内部调 _do_action_preview('delete', {...}) 拿 (action_id, signed_token, preview_info)
  3. render_template('drawer/delete_preview.html', action_id, signed_token, files, hardlinks, qbit_torrents, ssh_cmds, total_size)
  4. Return HTML fragment with:
     <div data-step="preview">
       <h3>预览：删除 3 项 · 实际释放 12.4 GB</h3>
       <table>...影响列表...</table>
       <button hx-post="/ui/drawer/delete/confirm"
               hx-vals='{"action_id":"...","signed_token":"..."}'
               hx-target="#drawer"
               hx-confirm="确定？此操作不可逆">确认删除</button>
       <button @click="$dispatch('drawer:close')">取消</button>
     </div>
```

**Step 3: Confirm + progress**
```
Server (confirm):
  1. 收 POST /ui/drawer/delete/confirm {action_id, signed_token}
  2. 内部调 /api/action/confirm → 拿 status (succeeded / running)
  3a. 同步完成 (delete 快) → render drawer/delete_done.html with success/fail counts
  3b. 异步 (organize batch > 5) → render drawer/progress.html with hx-trigger="every 2s" hx-get="/ui/drawer/action/<id>/status"
  
Progress polling 模板：
  <div hx-get="/ui/drawer/action/<id>/status"
       hx-trigger="every 2s"
       hx-swap="outerHTML">
    <progress value="..." max="..."></progress>
    <p>正在处理 12/40 · 当前: "S2E14.mkv"</p>
    <button hx-post="/api/organize-runs/<id>/abort">中止</button>
  </div>
  
Status endpoint server 端：
  - status terminal (succeeded/failed/aborted) → return final HTML (HTMX 自动停 polling 因为新 HTML 没 hx-trigger)
  - status running → return same shape with updated counts (HTMX 继续 polling)
```

### 3.3 Drawer URL 反映 + 刷新恢复

```
User 在 /files 触发 delete preview → hx-push-url="true" 后 URL 变成:
  /files?drawer=action/abc123&step=preview

页面刷新 → Flask /files 路由检测 query string drawer=action/abc123:
  - 从 destructive_actions 表读 status
  - status='pending' → render 主页 + 嵌入 drawer fragment with step=preview
  - status='running' → render 主页 + drawer fragment with step=progress
  - status='succeeded' → render 主页 + drawer fragment with step=done
  - status='consumed/expired' → render 主页 + drawer fragment with "此 action 已过期" + 关闭按钮

Drawer container 永远在 layout HTML 里:
  <aside id="drawer" :class="{open: drawerOpen}" ... ></aside>
Alpine drawer 状态:
  x-data="{ drawerOpen: false }"
  $watch('drawerOpen', false → URL 去除 ?drawer=...)
```

### 3.4 哪些 action 走 drawer

| Action | URL prefix | 异步? | Drawer step 数 |
|---|---|---|---|
| delete (单/批) | `/ui/drawer/delete/*` | 否 | 2 (preview, done) |
| organize 单文件 | `/ui/drawer/organize/*` | 否 | 2 |
| organize batch | `/ui/drawer/organize-batch/*` | 是 (> 5 items) | 3 (preview-dashboard, confirm-selection, progress) |
| nfo_write | `/ui/drawer/nfo/*` | 否 | 2 |
| batchIdentify | `/ui/drawer/identify-batch/*` | 是 | 2-3 (queue progress) |

**非 destructive 但流程长的 actions（batch identify / 全库 scan）也用 drawer**：统一交互模型 + 用户不需要分辨"是否 destructive"，永远是"右侧 drawer 看任务"。

### 3.5 关键约束

- **drawer 内不允许嵌套 modal / 嵌套 drawer**（除了原生 hx-confirm 这种"按钮级一次性确认"）
- **drawer URL 一定 push** (`hx-push-url="true"`)：浏览器 back 等价 cancel
- **drawer close 检查 unconsumed token**：preview 完没 confirm 直接关 → 弹 toast "已取消，token 自动过期"，无副作用（destructive_actions 表的 row 自然 TTL 过期）
- **drawer 容器只 1 个**：layout 永远渲染一个 `<aside id="drawer">`，HTMX 一律 target 这个 ID

## Section 4: 状态信号 / status bar

> 4 类信号同时存在：(a) provider 状态 / (b) 后台 worker / (c) sidebar nav badge / (d) 瞬时事件（key 失效 / 操作完成 / cron 触发）。各自有归宿，不互相打架。

### 4.1 底部 status bar（VS Code 风固定条）

```
┌─[layout 永远渲染 in footer]─────────────────────────────────────────┐
│ 🟢 TMDB · 🟢 DeepSeek · 🟡 Emby (401) · 🟢 qBit │ ⏳ organize 2/3 │ scanner idle │ cron next 03:42 │
└────────────────────────────────────────────────────────────────────┘
   ↑ provider 段                              ↑ worker 段        ↑ cron 段
```

**Provider 段**：
- 源：`GET /ui/status/providers` → `<span>` group with class by state（green / yellow / red / gray）
- 刷新：`hx-trigger="load, every 60s"` （跟后端 60s TTL cache 一致）
- 点 segment：`hx-boost` 跳 `/settings/<provider>` 子页
- hover：tooltip 显示 `state + last_check + reason`（如 `auth_failed: HTTP 401 at 16:42`）

**Worker 段**：
- 源：`GET /ui/status/workers` 聚合 `auto_organize_runs` (running) + `scan_runs` (running) + `organize_runs` (running)
- 刷新：`hx-trigger="load, every 5s"`（worker 状态变化快）
- 点 segment：开右侧 drawer 看具体 worker progress
- idle 时显示 "idle"，不闪烁

**Cron 段**（如果启用自动整理）：
- 源：`GET /ui/status/cron` → 下次触发时间
- 刷新：`hx-trigger="load, every 30s"`

### 4.2 Sidebar nav badge

每个 nav 项右侧 badge 反映"该模块有多少要处理"：

| nav | badge 计数 endpoint | 刷新频率 |
|---|---|---|
| 🎬 媒体库 | `SELECT count(*) FROM media_files WHERE needs_identify=1` | 60s |
| 🔍 重复检测 | `dedup_groups_count(not_archived)` | 60s |
| 📦 整理 | `SELECT count(*) FROM auto_organize_runs WHERE status IN ('pending','organizing')` | 5s（user 主动触发 organize 后期望快反馈） |

badge HTML by Alpine（template literal 渲染计数 + 颜色 by severity）：
```html
<span class="nav-item" hx-get="/ui/sidebar/badges" hx-trigger="load, every 60s" hx-swap="innerHTML">
  ...每个 li 自己持有 .badge .badge-warning .badge-zero
</span>
```

零计数 hidden，避免视觉噪音。

### 4.3 Toast (瞬时事件)

**触发场景**：
- destructive action 完成（drawer 已经显示了，但用户可能在 drawer 没开就完了→ toast 通知）
- cron 自动触发了一个 organize（user 不主动）
- provider key 突然失效（HTMX response header `HX-Trigger: provider:auth_failed` 触发 toast）
- 错误恢复 / 警告

**实现**：layout 底部固定 `<div id="toast-host">`，Alpine 监听 `@toast.window` 事件 push 到栈，3s 自渐隐。

**HTMX 触发 toast 的服务端约定**：
```python
resp = make_response(html_fragment)
resp.headers["HX-Trigger"] = json.dumps({"toast": {"severity": "warning", "message": "TMDB key 失效"}})
return resp
```

前端 Alpine：
```html
<div @toast.window="addToast($event.detail)"></div>
```

### 4.4 Banner（仅 critical 全局事件）

原来顶部红 banner（provider auth_failed 全局）保留，但**严格限定触发场景**：仅当系统**不可用**时（如所有 provider 都挂、SSH 连接断、DB 锁死）。其他场景全部退化到 toast / status bar。

Banner 位置：sidebar 下方主区上方（不抢 breadcrumb 区），fixed 红色条，single line + 关闭按钮 + 「去修复」按钮跳 `/settings/<相关 provider>`。

### 4.5 严重程度对照表

| 事件 | 渠道 |
|---|---|
| Provider 健康度变化 | status bar provider 段 + (auth_failed 时) toast |
| Worker 进度更新 | status bar worker 段 + drawer 内 progress |
| Worker 完成（成功）| toast `severity=success` |
| Worker 失败 | toast `severity=error` + status bar 闪红一次后 idle |
| Cron 即将触发 | status bar cron 段（不弹 toast） |
| Cron 自动触发 organize | toast `severity=info` + drawer 自动开（如果 user 在用页面） |
| 系统不可用（全 provider 挂 / SSH 断）| banner |
| user 主动操作完成（删除 / 整理 / 写 NFO）| drawer 内显示 + toast `severity=success` |
| 后端 schema migration / version 升级 | banner |

**核心原则**：同一事件不重复渠道（worker 完成不会同时 status bar 闪 + toast + drawer 三处都通知，按 user 是否在看 drawer 选最相关一个）。

## Section 5: Onboarding / 第一次启动

> 开源受众 = 别人 clone repo 跑 `./run.sh` → 第一次进 web UI。**这是开源项目能不能留住用户的决定性 5 分钟**。

### 5.1 第一次启动检测

`/` 路由（dashboard）渲染前先调 `services/onboarding.check_status()`：

```python
class OnboardingStatus:
    nas_ssh: Literal['ok', 'unconfigured', 'failed']      # 必填
    qbit: Literal['ok', 'unconfigured', 'failed']         # 必填
    tmdb_key: Literal['ok', 'unconfigured', 'failed']     # 必填 (BYOK)
    deepseek_key: Literal['ok', 'unconfigured', 'failed'] # 必填 (BYOK)
    library_roots: Literal['ok', 'unconfigured']          # 可选 (无 → organize 功能 disabled)
    emby: Literal['ok', 'unconfigured', 'failed']         # 可选

def is_onboarded() -> bool:
    return all of nas_ssh, qbit, tmdb_key, deepseek_key == 'ok'
```

`/` 路由逻辑：
```python
if not is_onboarded() and request.path == '/':
    return redirect('/onboarding')
```

但 `/onboarding` 本身不阻断访问其他页 — user 可以手动跳 `/settings` 自己摸（高级用户路径）。

### 5.2 Onboarding wizard `/onboarding`

```
┌──────────────────────────────────────────────────────────┐
│  欢迎使用 NASVault                                       │
│  设置 4 个必填，预计 5 分钟                              │
│                                                          │
│  ●━━━━○━━━━○━━━━○━━━━○                                   │
│  NAS   qBit  TMDB  DeepSeek  完成                        │
│                                                          │
│  ┌─ Step 1: NAS SSH ──────────────────────────────────┐ │
│  │ 我们用 SSH 操作 NAS 文件（不动 Samba）             │ │
│  │                                                    │ │
│  │ SSH 用户名@host  [admin@192.168.1.100  ]          │ │
│  │ SSH 端口         [22                    ]          │ │
│  │ 沙箱根路径       [/share/CACHEDEV2_DATA] (?)      │ │
│  │                                                    │ │
│  │ [测试连接]                                         │ │
│  │   🟢 SSH 通了 · 沙箱可读                            │ │
│  │                                                    │ │
│  │ [跳过] (?)            [下一步: qBit →]            │ │
│  └────────────────────────────────────────────────────┘ │
│                                                          │
│  💡 我们不会存你的密码 / token 到环境变量,落盘            │
│     `config/*.json` 文件 chmod 600 (仅你自己可读)        │
└──────────────────────────────────────────────────────────┘
```

**关键设计**：
- 每步 inline 「测试」按钮 (`hx-post="/api/config/<provider>/test"`)，**没测通不让 next**
- 跳过有警告 ("这一项不配会让 X 功能 disable")
- 每步右下角有「？」打开 help popover（解释为什么需要这个 key + 去哪申请）
- 进度条用 `<progress>` 原生 + Alpine
- 完成 step 后 `hx-push-url="/onboarding/step/<n+1>"`，刷新 / back 可恢复
- 全部完成 → redirect `/`

### 5.3 Step 内容

| Step | 内容 |
|---|---|
| 1. NAS SSH | host + 端口 + 沙箱根路径 + 测试 + 文档链接（如何 `ssh-copy-id`） |
| 2. qBittorrent | WebUI URL + 用户名 + 密码 + 测试 + 文档（如何启用 WebUI） |
| 3. TMDB | API key + 测试（拉 1 个 sample movie 验证）+ 申请 key 链接 |
| 4. DeepSeek | API key + base URL（默认 official，可换 NewAPI 等）+ 测试 + 申请 key 链接 |
| 5. 完成 | 总结 + 「现在去看看库」按钮 → `/`（dashboard 第一次进会显示「未识别 0 / 已识别 0，运行一次扫描？」） |

### 5.4 后续 onboarding 入口

- `/settings/onboarding` 可手动重跑 wizard（如换了 NAS / 换 key 想测试）
- Dashboard 检测到任一 required provider 是 `unconfigured` → 顶部红 banner「配置未完成」link to `/onboarding`

### 5.5 开源 README 配套

README 提到「跑 `./run.sh` → 打开浏览器 → 跟着 5 分钟 wizard」即可。不再需要 README 详写每个 config 文件格式（wizard 替代）。

## Section 6: 实施路径分阶段

> Big bang 重做 ≠ 一个 PR 全改完。这个项目仍按 [[methodology]] 「每个独立改动单元都要 typecheck/lint + Codex review，不批量」纪律走。下面 6 个 Phase 各自一个 PR / 一个 commit 单元，每个完成才进入下一个。

### Phase A: 脚手架 + layout 骨架（1-2 day）

**目标**：引入 Tailwind + 新 layout，老 modal 全保留可用。

- 引入 Tailwind CSS Standalone CLI（下 binary + Makefile target `tailwind:watch`）
- 拆 `templates/index.html` 为 `base.html` (layout) + `_topbar.html` + `_sidebar.html` + `_status_bar.html` + `_drawer.html` (empty container)
- 实现 `/ui/status/providers` + `/ui/status/workers` HTML fragment 路由
- Sidebar 6 nav 项 + URL push（但每个 nav 点击仍跳到老主区 — 是 transitional state）
- 删除老 topbar 上 8+ 个按钮（被 sidebar 替代）；老 modal 触发改用 sidebar / 主区上的按钮

**验收**：layout 完整，老功能不退化（手动跑端到端冒烟：删一个文件 / identify 一个 / organize 一个）。

### Phase B: 6 大 page 拆分 + URL 路由（2-3 day）

**目标**：每个 sidebar nav 项对应独立 Flask route + Jinja2 template。

- 新增 `routes/` 模块：`pages_dashboard.py` / `pages_files.py` / `pages_library.py` / `pages_dedup.py` / `pages_organize.py` / `pages_settings.py`
- 每个 page 自己的 `templates/pages/<name>.html`，extends `base.html`
- `hx-boost="true"` on sidebar nav links → 页面跳转 PJAX swap 主区不闪屏
- 实现 dashboard 卡片 polling

**验收**：所有 URL 可书签；浏览器 back/forward 正常；端到端冒烟全过。

### Phase C: Destructive drawer（3-4 day）

**目标**：5 类 drawer flow 全替换对应 modal。

按 ROI 顺序：
- C.1 delete（最高频；样板）
- C.2 organize 单文件（最简单 destructive）
- C.3 organize batch（最复杂；包含 progress polling 模板）
- C.4 nfo_write（短）
- C.5 batch identify（非 destructive 但流程长）

每个 sub-phase：
- 新增 `/ui/drawer/<action>/preview|confirm|status` 路由 + `templates/drawer/<action>/*.html`
- 旧 modal HTML 删除 + 旧 `app.js` 里对应 fetch+DOM 逻辑删除（每删一处 reading：「这个还有谁 import？」）
- 单点端到端冒烟 → codex review → ship

**验收**：原 12 modal 全删；URL `?drawer=action/<id>&step=<>` 刷新可恢复；progress polling 正常。

### Phase D: Onboarding wizard（1-2 day）

**目标**：第一次启动 5 分钟流畅。

- `services/onboarding.py` 实现 `check_status()` + `is_onboarded()`
- `/onboarding` + `/onboarding/step/<n>` 路由 + wizard 模板
- `/` 路由加 redirect logic
- 用 `config/` 空目录测试 wizard 全流程

**验收**：clean checkout + 空 `config/` 跑 `./run.sh` → 浏览器进 `/` → 5 分钟内完成所有必填 → 跳 dashboard 看到数据。

### Phase E: Toast / 错误信号收敛（1 day）

**目标**：删掉散乱 banner，统一到 toast / status bar。

- 实现 toast host + Alpine `addToast()`
- HX-Trigger response header 约定（`{toast: {severity, message}}` → 前端 dispatch）
- 老 banner 代码扫除（grep `bannerHtml` / `showBanner` / `errorBanner` 全删）
- 仅保留 critical banner（系统不可用）

**验收**：触发各种 provider 401 / worker 失败 / cron 触发，确认每个事件**只走一个渠道**（不重复通知）。

### Phase F: 清理 + 开源 ship（1 day）

**目标**：删 4771 行 vanilla JS 残留 + 文档 polish + 截 GitHub README 截图。

- 现在 `static/app.js` 应该已经空了大半，pass-through 删
- 保留少量 module（如 dedup table sort 这种独立 utility），单文件每个 < 200 行
- 更新 README 截图（Dashboard / Library / Drawer 各一张）
- CHANGELOG 写 "Phase 6: frontend redesign — HTMX + Alpine + Tailwind"
- 走开源 release 流程

**验收**：`./run.sh` clone 后能跑；codex review final pass 0 BLOCKER；GitHub README 截图替换。

---

## 风险 / 反例

- **Phase A 跟 Phase C 之间是过渡期**：layout 是新的但 modal 是老的，是 transitional state，**不要 ship 中间状态到公开 release**（feature branch 多 Phase 累积再 merge main）
- **`/api/*` 和 `/ui/*` 双路由维护成本**：service 层必须 properly factored — 业务逻辑在 `services/`，`/api/*` 和 `/ui/*` 都只是 thin adapter。grep `request.json` 出现在 services/ 下 → 是泄露，必须移到 routes 层
- **HTMX 不是银弹**：dedup table 排序 / library 海报滚动 加载 / settings 表单复杂校验 这种富交互场景 HTMX 不一定优雅，仍需 Alpine 写 component。Phase B 实施时若发现某个 page 用 HTMX 写比 vanilla 更复杂 → 该 page 局部用 Alpine 单组件
- **drawer 内"重新触发 destructive"的 race**：user 在 progress 阶段 back 按钮 → drawer URL 退到 preview state → 再次 confirm 同 token 触发？已被后端契约 #1 一次性消费 token 防住（second confirm 拿到 410 expired），UI 显示「token 已被消费」即可

---

## 关键复用的现有代码 / 文件

| 已存在 | 复用 |
|---|---|
| `services/destructive_action.py` (preview/confirm/atomic_consume) | 不动，`/ui/*` adapter 调用 |
| `services/llm.py` 契约 #3 grounded select | 不动 |
| `auto_organize_runs` 表 + cron + status state machine | 不动，前端只读 |
| `media_files` + `dedup_groups` 视图 | 不动 |
| `routes/...` (如果有 blueprints) | 新增 `routes/pages_*.py` + `routes/drawer_*.py` |
| `templates/index.html` 现有 12 modal HTML | 大部分删除，少量片段（如 NFO XML preview）移到 drawer partial |

---

## Verification

完整端到端测试覆盖（每个 Phase 都要跑一遍，最终 Phase F 也跑）：

1. **Empty config 启动 → onboarding 5 分钟**：clean checkout，rm -rf `config/`，跑 `./run.sh`，浏览器进 `/`，跟 wizard 走完
2. **Files 页删除 3 个文件**：勾选 → 删除 drawer → preview 看影响 → confirm → 看 progress / done → 第三方 ssh + qbit API verify (按 [[methodology]] 危险操作 third-party verify 规则)
3. **Library 视图 1797 项加载 + 筛选 + 详情**：reset auto-fill 看 4 batches × 500 渐进 render 顺利；筛选切换不闪屏
4. **Dedup 组取舍 + 删除**：选保留 → 删除 drawer → progress
5. **Organize batch 12 items**：drawer step 1 dashboard → step 2 select → step 3 progress polling → done
6. **自动整理 cron 触发**：dashboard 看到 worker 段 + toast `cron 自动触发`
7. **Provider 401 模拟**：手动改 TMDB key 失效 → 60s 后 status bar 段变黄 + toast 提示 + sidebar 媒体库 badge 不变（库视图本身仍可看缓存）
8. **MCP server 仍工作**：通过 `mcp_server` stdio 走全套 destructive flow，确认 `/api/*` 路由没被 `/ui/*` 改造污染
9. **codex review 各 Phase commit**：每个 Phase end 跑一次 codex review，按 [[methodology-review]] 多轮收敛规律
10. **手动 readme onboarding test**：找一个完全没见过这个项目的人（你哥们 / 同事），让他按 README 跑起来，看是否 5 分钟内不卡

---

## 工程估算

| Phase | 天数（净写代码 + review + 端到端冒烟） |
|---|---|
| A 脚手架 + layout | 1-2 |
| B 6 page 拆分 | 2-3 |
| C destructive drawer (5 sub-phase) | 3-4 |
| D onboarding wizard | 1-2 |
| E toast / 信号收敛 | 1 |
| F 清理 + 开源 ship | 1 |
| **总计** | **9-13 天**（净）|

实际 calendar 时间可能 2-3 周（含 review wait / 多轮 codex 收敛 / 端到端冒烟撞 bug 修）。

---

## 下一步（plan mode 退出后）

1. 这份 plan 作为 brainstorming skill 的 design doc，**应转 implementation plan**（superpowers:writing-plans skill）
2. 创建 `docs/superpowers/specs/2026-05-16-frontend-redesign-design.md` 作为正式 spec（这份 plan 文件可以 cp 过去）
3. 由 Phase A 第一个 commit 开始执行
