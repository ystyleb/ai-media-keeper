# NASVault 视觉重设计 · 方向 A 媒体中心风

> 状态：**Design approved (2026-06-10)**，等待实施。
> 协作历史：brainstorming skill + visual companion；三方向 mockup 对比选定 A；概览页高保真 mockup 已确认（`.superpowers/brainstorm/15909-1781063956/content/dashboard-mock-a.html`，本地不入库）。

## Context

2026-05-16 的前端重设计（`2026-05-16-frontend-redesign-design.md`）重做了 IA：sidebar + 6 页 + drawer + status bar，当时明确"不在意视觉老土"。本次补视觉层：用户反馈"页面不好看"。

**确认的范围**（跟 user 确认过）：
- **视觉 + 局部布局**：整套视觉语言重做；顺手修布局硬伤（settings 空旷、dashboard 卡片高度失衡）
- **不动**：页面结构、路由、IA、后端、任何业务逻辑
- **风格方向**：A · 媒体中心风（Jellyfin/Plex 气质：深紫黑、渐变点缀、海报优先、柔光阴影）——从 A 媒体中心 / B 专业工具 / C 柔和 SaaS 三方向 mockup 对比中选定

## 设计语言（Design Tokens）

### 色板

| Token | 值 | 用途 |
|---|---|---|
| `--bg-base` | `#0d0b16` | 页面底色（深紫黑） |
| `--bg-surface-from / -to` | `#1a142f` / `#130f22` | 卡片渐变面（145deg） |
| `--bg-sidebar-from / -to` | `#15102a` / `#0d0b16` | 侧栏渐变（180deg） |
| `--bg-statusbar` | `#0a0813` | 底部状态栏 |
| `--border-subtle` | `#2a2150` | 卡片边框 |
| `--border-faint` | `#221a3e` | 分隔线 / 侧栏边 |
| `--text-primary` | `#f5f2fd` | 标题 |
| `--text-body` | `#e6e1f2` / `#d5cdeb` | 正文 |
| `--text-secondary` | `#9b91b8` / `#8d80b5` | 次级 / 卡片 label |
| `--text-muted` | `#6f6590` | 弱化信息 |
| `--accent` | `#a78bfa`（深 `#7c3aed`） | 主强调紫 |
| `--accent-2` | `#d946ef`（浅 `#f0abfc`） | 品红（渐变对、次强调） |
| `--ok` / `--warn` / `--danger` | `#34d399` / `#fbbf24` / `#f87171` | 语义色 |

渐变对统一为 `linear-gradient(90deg, #c4b5fd, #f0abfc)`（文字）和 `linear-gradient(90deg, #7c3aed, #d946ef)`（条/块）。

### 形态与层级

- 卡片圆角 16px、内部元素 9–11px、药丸 99px；卡片 1px `--border-subtle` 边框 + 渐变面
- 页面顶部淡氛围光晕：`radial-gradient(ellipse, rgba(124,58,237,.18), transparent 70%)` 绝对定位纯 CSS，每页一处
- 字级：页面标题 24px/800；卡片 label 11px/700 大写 + 1.2px 字距；英雄数字 34px/800 渐变文字；正文 12.5–13.5px
- 字体沿用系统栈（-apple-system / PingFang SC）

### 图标

- **全站 emoji 清零**（sidebar 导航、settings 卡片、dashboard 卡片标题、files 工具栏、最近活动列表）
- 统一 Bootstrap Icons（`base.html` 已加载，零新依赖）；mockup 中的内联 SVG 实施时映射到等价 `bi-*` class
- Logo：渐变色块（`#7c3aed → #d946ef`，圆角 9px，紫色投影）+ 白色图形 + "NASVault" 粗体

### Token 落点

- CSS 变量 + 组件类（`.nv-card` / `.nv-chip` / `.nv-pill` / `.nv-badge` / 导航态）定义在 `static/css/input.css` 的 `@layer components`，全站复用，**不在模板里堆长 utility 串**
- `tailwind.config.js` theme 扩展替换现有 `sidebar` / `statusbar` palette 为新色板

## 逐页改动

### 概览 `/`（参照已确认 mockup）

- 卡片区改 **bento 网格**（`grid-template-columns: 1.25fr 1fr 1fr`）：
  - 媒体库统计卡跨 2 行（左列）：渐变大数字 + 类型 chip + 评分分布渐变条
  - 磁盘卡：conic-gradient 圆环（88% 那种）替代横条，中心显示百分比
  - **"系统状态 + 后台运行 + 待处理"三张半空卡合并为一张"待处理"行动卡**：行式条目（媒体库未识别 816 → / 重复检测待处理 64 →），点击跳对应页；后台 worker 状态信息下沉到 status bar（已有）
  - 最近活动卡跨 2 列：每行 34×48 海报缩略图 + 文件名 + 时间/来源 + 状态药丸（✓ 已入库 / 待确认）。海报：TMDB 缓存有 `poster_path` 用真图，无则渐变占位块
- 涉及：`templates/pages/dashboard.html`、`templates/partials/dashboard/*`、`routes/ui_dashboard.py`（最近活动需带 poster 字段——只是查已有缓存列，不加新 API）

### 文件 `/files`

- 顶部工具栏按钮统一"icon+文字"胶囊样式（现在是 emoji+文字的杂色按钮堆）
- 文件行操作按钮：现在三个常驻红/绿色块 → **hover 显示的 ghost 图标按钮**（默认 `--text-muted`，hover 着色）
- 右侧"已选择"面板套卡片语言；"操作日志"区同
- 涉及：`templates/pages/files.html`、`static/app.js` 中 `renderXxx` 系列模板字符串（只改 class / icon，不动逻辑）

### 媒体库 `/library`

- 海报卡：hover 浮起（translateY + 阴影加深）+ 底部渐变遮罩内放标题/年份（替代卡片下方独立文字行）
- 筛选条 / 搜索框统一新输入控件样式
- 涉及：`templates/pages/library.html`、`app.js` 库渲染函数

### 重复检测 `/dedup`

- 组卡片套统一卡片语言；质量评分改渐变条；候选行的"已看"等徽章药丸化
- 涉及：`templates/pages/dedup.html`、`app.js` dedup 渲染函数

### 设置 `/settings`

- 修空旷硬伤：7 张小卡 → **2 列大卡**，每卡：彩色 icon 徽章（圆角方块底 + 语义色）+ 标题 + 描述 + **配置状态徽章（✓ 已配置 / 未配置，灰）**
- 状态数据源：已有 `/api/providers/status` + 各 config load 函数（NAS/qBit/AI/Emby 可判断"已配置"；媒体库根/自动整理/数据库工具按各自配置判断）；页面加载时由现有 status 端点带出，不加新 API
- 涉及：`templates/pages/settings.html`、`routes/pages_settings.py`（若需传配置状态）

### 侧栏 / 状态栏 / 全局（`base.html` + partials）

- 侧栏：渐变背景、渐变 logo 块、active 项紫色渐变底 + 左侧光条（`inset 2px 0 0 #a78bfa`）、badge 药丸化（紫底紫边）
- 状态栏：底色 `--bg-statusbar`、dot 沿用语义色
- Toast / drawer 表面色对齐新卡片语言
- Onboarding 页同语言（低优先，最后扫一遍）

## 范围边界（明确不做）

1. **12 个 Bootstrap legacy modal 只轻度换肤**：在 `legacy.css` 覆盖 modal 配色（背景/边框/按钮对齐新色板）。modal → drawer/子页迁移是 ROADMAP Phase F 的事，本次不做
2. **app.js 只改渲染模板字符串里的 class 和 icon**，任何逻辑、事件、API 调用不动
3. 后端路由 / API 零改动（ui_dashboard 最近活动带 poster 字段除外——读已有缓存列）
4. 不引入新依赖、不上构建工具；继续 Tailwind standalone CLI + Bootstrap Icons
5. 不做浅色主题、不做手机适配优化（维持现有响应行为）

## 实施顺序（建议的改动单元）

1. **Tokens 单元**：`input.css` 变量 + 组件类 + `tailwind.config.js` + `base.html`（侧栏/状态栏/光晕/logo）——这一单元落地后全站底色和侧栏先换肤
2. **概览页单元**：bento 网格 + 卡片合并 + 海报缩略图
3. **文件页单元**：工具栏 + 行操作按钮 + 右侧面板
4. **媒体库 + 重复检测单元**：海报卡 hover + dedup 卡片
5. **设置页单元**：2 列大卡 + 配置状态徽章
6. **收尾单元**：legacy modal 换肤、onboarding、toast/drawer、全站 emoji 扫尾（grep 验证清零）

每单元独立 commit + 截图对照 + 测试回归（参照"独立改动单元 review"惯例）。

## 验证

- `make tailwind-build` 无错；`pytest tests/unit`（744）全绿——UI 合同测试（`test_pages_routes.py` / `test_ui_*.py` / `test_toast.py`）覆盖模板渲染
- Playwright headless 逐页截图（dashboard / files / library / dedup / settings / onboarding），与 mockup 对照
- 重点回归点（历史踩坑）：modal 触发后 form 有值、跨页 DOM null guard、`app.js` cache-bust 生效、Bootstrap CSS 在 Tailwind **之前**加载的顺序不变
- 全站 emoji 清零验证（grep 不支持 emoji 区间，用 Python 扫）：
  `python3 -c "import re,pathlib; [print(p,i+1,l.strip()) for p in list(pathlib.Path('templates').rglob('*.html'))+[pathlib.Path('static/app.js')] for i,l in enumerate(p.read_text().splitlines()) if re.search(r'[\U0001F300-\U0001FAFF☀-➿]', l)]"` 无业务 UI 命中
