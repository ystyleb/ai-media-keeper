# NASVault 前端重设计 端到端测试计划

> 状态: 准备执行
> 范围: Phase A-F merged main (HEAD: 4a59d63)
> 测试目标: 找出所有 UI/JS/API 集成 bug 一次性批量修, 不再单点反复

## 测试环境

- **Server**: `flask --app app run --port 5001 --debug` (env `NAS_SKIP_WORKER_LOCK=1`)
- **Token**: 9a3a864effbac0d7447496dd1dfadc7db760688a7fdb179454643d2e528bb779
- **Tools**:
  1. **gstack browse** (headless Chromium with snapshot + console + network capture)
  2. **curl + jq** (server-side API verification)
  3. **Chrome DevTools manual** (user-side if gstack fails)
- **Pre-existing**: NAS / qBit / TMDB / DeepSeek 都已配置 (config/ 目录有真实 config)

## 测试范围

### In scope
- Sidebar nav (6 link, active class, hx-boost)
- 底部 status bar (provider 段 + worker 段)
- 6 page (/, /files, /library, /dedup, /organize, /settings) 渲染 + DOM elements 完整
- Onboarding wizard (/onboarding 4-step)
- 12 legacy Bootstrap modal 触发 + 关闭 + 表单填写
- Console errors after each action (No JS exception 阈值)
- Network requests (每 user action 触发预期 API call)
- HTMX polling endpoints (/ui/status/*, /ui/dashboard/*, /ui/sidebar/*)

### Out of scope (这次不测)
- 真正的 destructive operations (delete / organize)— 仅测 preview & modal flow
- Drawer UI wire up (Phase F deferred)
- MCP server (后端契约不变, 已 verified)
- 老 app.js 内 large mass of file browser interaction (浏览器 list / search / filter — 抽样测)

## 测试用例

### TC-01: Layout 全局一致性 (cross-page)

每 page 访问后 verify:
- [ ] HTTP 200
- [ ] Sidebar 渲染 (logo + 6 nav 链接)
- [ ] 当前 page nav-item 含 `active` class
- [ ] Topbar 含 breadcrumb (📁/🎬/🔍/📦/⚙️/🏠)
- [ ] 底部 footer status bar 容器 (`<footer ...id="status-providers" / id="status-workers">`)
- [ ] No JS console error on page load

Pages: `/, /files, /library, /dedup, /organize, /settings, /onboarding`

### TC-02: Status bar 动态数据

- [ ] GET /ui/status/providers (with token) → HTML fragment with 4 providers (TMDB/DeepSeek/Emby/qBit)
- [ ] GET /ui/status/workers (with token) → HTML fragment (idle 或 worker 列表)
- [ ] No token → 401
- [ ] HTMX poll triggered after page load (DevTools Network 看 60s 后第二次 hit)
- [ ] auth_failed provider → HX-Trigger header 含 toast warning

### TC-03: Dashboard cards (/)

- [ ] GET / → 3 卡片 (系统状态 / 后台运行 / 待处理)
- [ ] GET /ui/dashboard/system → providers 列表
- [ ] GET /ui/dashboard/workers → workers idle 或列表
- [ ] GET /ui/dashboard/todo → 待处理项 (按 badges 计数)
- [ ] HTMX hx-trigger 真在跑

### TC-04: /files 文件浏览器

- [ ] GET /files → 老主区渲染 (file-container / file-list table)
- [ ] page load 自动调 GET /api/files?path=... 拉文件列表
- [ ] file-list 渲染文件行 (无 console error)
- [ ] 点目录 → 导航 + breadcrumb 更新
- [ ] 临时按钮区 (AI / qBit / NAS / 媒体库 / 自动整理 / 全库扫描 / 批量识别) 都触发对应 modal
- [ ] 文件勾选 → updateSelectionUI 显示 count + size
- [ ] 删除 / 整理 / 识别 modal 可弹出 (不实际执行, 仅 verify modal 打开)

### TC-05: /library 媒体库

- [ ] GET /library → library-panel 渲染
- [ ] page load 触发 library 数据加载 (API call)
- [ ] 海报墙渲染 (有 media 时)
- [ ] 筛选 type / 搜索 / 排序 work
- [ ] No console error

### TC-06: /dedup 重复检测

- [ ] GET /dedup → dedup-panel 渲染
- [ ] page load 触发 dedup 数据加载
- [ ] 重复 group 列表 + quality score 显示
- [ ] 已看待归档 tab 切换 work
- [ ] No console error

### TC-07: /organize page

- [ ] GET /organize → 2 卡片 (自动整理 / 媒体库根)
- [ ] 点卡片 → modal 弹出 (autoOrganizeConfigModal / organizeConfigModal)
- [ ] modal 关闭 → page 状态正常

### TC-08: /settings page

- [ ] GET /settings → 7 卡片
- [ ] 点每张 card → 对应 modal 弹出
- [ ] **关键: NAS 配置 modal 保存** — 填表 → 点保存 → 期望: modal 关 + toast + 配置文件真实更新
  - 现状已知 bug: 保存后 user 反馈 "文件什么都刷新不出来" — 待诊断
- [ ] qBit / AI / Emby / 媒体库根 / 自动整理 / 数据库工具 同样测

### TC-09: /onboarding wizard

- [ ] GET /onboarding → step 1 (NAS)
- [ ] step navigation (/onboarding/step/<step>) 4 step + done
- [ ] 未知 step → redirect /onboarding/step/nas
- [ ] done step 显示 4 必填状态

### TC-10: API 契约不变

- [ ] GET /api/providers/status (with token) → 200 + JSON
- [ ] GET /api/files?path=... → 200 + JSON
- [ ] GET /api/config/nas → 200 + 现有 config
- [ ] POST /api/config/nas → 200 + saved
- [ ] /api/* 其他 endpoint 跑一遍 smoke

### TC-11: Toast 系统

- [ ] base.html 含 toast-host + Alpine toastStack
- [ ] 触发 'toast' window event → toast 出现 + 3s 自动消失
- [ ] HX-Trigger header from /ui/status/providers auth_failed → toast warning

### TC-12: Cross-page DOM null guard

针对 app.js 跨 page 调用 (saveNASConfig → addLog/loadFiles 等):
- [ ] 在 /settings 触发 NAS 保存 → 不撞 querySelector null
- [ ] 在 /library 触发任何 modal save → 同样不撞

## 执行方法

### Method A: gstack browse (优先)
```bash
B=/Users/winson/.claude/skills/gstack/browse/dist/browse
$B goto "http://localhost:5001/?token=<TOKEN>"
$B snapshot -i
$B console --errors  # 检查 JS error
$B network           # 检查 API calls
$B screenshot /tmp/<page>.png
```

### Method B: curl + grep (server-side smoke)
```bash
for path in / /files /library /dedup /organize /settings /onboarding; do
  curl -s -o /tmp/page.html -w "%{http_code}\n" -H "Authorization: Bearer $TOKEN" "http://localhost:5001$path"
  # 验证 DOM element 存在 / sidebar active class / breadcrumb
done
```

### Method C: API smoke (curl)
```bash
for ep in /api/providers/status /api/files?path=/ /api/config/app /api/config/nas; do
  curl -s -H "Authorization: Bearer $TOKEN" "http://localhost:5001$ep" | jq .
done
```

## Bug 收集格式

每发现 bug:
```
ID: BUG-001
File: <path>:<line>
Severity: BLOCKER / IMPORTANT / NIT
描述: <what's wrong>
Steps to reproduce: <minimal repro>
Expected: <what should happen>
Actual: <what actually happens>
Fix proposal: <code change>
```

收集完所有 bug 后 → 批量 fix → 再跑一遍 verify → 0 BLOCKER 即可 ship。

## 执行优先级

1. **Phase 1 (无 dependency)**: TC-01, TC-02, TC-03, TC-10 (layout / status / dashboard / API smoke)
2. **Phase 2 (read-only)**: TC-04 base, TC-05, TC-06, TC-09 (各 page 渲染 + 无 error)
3. **Phase 3 (modal trigger)**: TC-07, TC-08, TC-12 (modal 打开关闭 + cross-page)
4. **Phase 4 (interaction)**: TC-04 advanced (filter / select / breadcrumb), TC-11 (toast)
5. **Phase 5**: batch fix + re-verify
