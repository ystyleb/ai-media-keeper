# Changelog

## [Unreleased] — 2026-05-17

### 前端重设计 Phase A-E

**Phase A: 脚手架**（commits a9ea0f4..8931a98）
- 引入 Tailwind CSS Standalone CLI 工具链（零 Node，binary 下到 `bin/`）
- 新 layout：`base.html` + sidebar（6 nav）+ topbar + 底部 status bar（VS Code 风）+ drawer 容器
- `routes/ui_status.py` blueprint：`/ui/status/providers` + `/workers` + `/ui/sidebar/badges` HTMX fragments
- `get_cached_providers_status()` 60s TTL cache 共享 `/api` + `/ui`
- Token auth：HTMX `htmx:configRequest` header 自动注入 `localStorage.nas_token` Bearer
- **API 行为微调** `/api/providers/status`：响应 `age` 字段从固定 `0.0` 改为真实 `time.time() - checked_at`（cache 命中时让 caller 知道 cache 数据有多旧）。schema 不变，仅值语义增强。

**Phase B: 6 page 拆分**（commits dfee773..dfb7a07）
- 6 个独立 Flask blueprint + Jinja2 template `extends base.html`
- 路由：`/`（dashboard）、`/files`、`/library`、`/dedup`、`/organize`、`/settings`
- 12 modal HTML 抽 `partial templates/_legacy_modals.html`
- 3 view 抽 partial（files_view / library_view / dedup_view）
- dashboard 3 卡片 + `ui_dashboard` 3 HTMX poll endpoint
- `/organize` `/settings` 简化版：modal trigger cards（Phase F 才精细化）
- sidebar server-render only + active class 高亮

**Phase C: Drawer endpoints**（commit b1c690b）
- `routes/ui_drawer.py` blueprint：`/ui/drawer/<action>/{preview,confirm,status}`
- delete / organize / nfo_write drawer template fragments
- generic confirm route 支持同步 `_done` + 异步 `_progress` polling
- 老 modal trigger 保留（Phase F 才 wire up drawer to page UI）

**Phase D: Onboarding wizard**（commit db8b77b）
- `services/onboarding.py`：`check_status()` + `is_onboarded()`
- `/onboarding` + `/onboarding/step/<step>` 4-step wizard（nas / qbit / tmdb / deepseek / done）
- Progress bar + 各 step 触发对应 modal

**Phase E: Toast 信号收敛**（commit 327bdba）
- `base.html` toast-host Alpine `x-data` + 4 severity + 3s auto-dismiss
- HX-Trigger header convention：server response 含 `{"toast": {...}}` JSON 自动 dispatch
- `services/toast.py`：`add_toast()` helper
- Demo：`/ui/status/providers` auth_failed → warning toast

### 后端契约不变

- `/api/*` JSON 路由零变更（MCP server / 第三方调用不受影响）
- 契约 #1 destructive action 双段式 preview/confirm + signed token
- 契约 #5 AI 介入边界（confidence gate / category allowlist）

### Tests

- 668 tests pass（629 baseline + 39 new for Phase A-E）
- Pre-existing race in `test_abort_stops_worker_loop`（单测单独跑 pass）

### Phase F（in progress）

- 老 4771 行 `app.js` 清理（modal trigger 改用 drawer）
- `/organize` `/settings` 主区从 modal trigger card → inline 表单
- 平台 / 开源 release polish
