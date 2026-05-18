# NASVault 端到端测试报告

> **执行时间**: 2026-05-18
> **范围**: Phase A-F merged main (HEAD: e2bf729)
> **工具**: Playwright headless Chromium + urllib API smoke
> **脚本**: `/tmp/e2e_full.py`

## 最终结果

✅ **85 PASS / 0 BUG** (Codex review 补测后)

第一轮: 42 PASS / 0 BUG (TC-01 → TC-12).
Codex review 抓出 6 类 gap → 补测 TC-13 → TC-18, 总 18 个用例, 85 PASS / 0 BUG.

## 覆盖汇总

| TC | 用例 | PASS | 关键证据 |
|---|---|---|---|
| TC-01 | Layout 全局一致性 (7 page) | 7/7 | Sidebar logo + breadcrumb + active class |
| TC-02 | 底部 status bar | 1/1 | providers 641B + workers 56B |
| TC-03 | Dashboard 3 cards | 3/3 | HTMX populated (sys + workers + todo) |
| TC-04 | /files 文件浏览器 | 2/2 | file-list 50 rows + NAS modal 触发 |
| TC-05 | /library 媒体库 | 1/1 | library-grid 670 items |
| TC-06 | /dedup 重复检测 | 1/1 | dedup-panel 80580B |
| TC-07 | /organize page | 1/1 | autoOrganize modal opens |
| TC-08 | /settings 7 modal triggers | 7/7 | 6 modal 都填了表单 (NAS host / qBit url / TMDB badge "已配置" / Emby url / org / autoOrg interval / scan) |
| TC-09 | /onboarding wizard | 6/6 | 4 step + done + unknown step redirect |
| TC-10 | /api/* 契约 smoke | 10/10 | providers/status, files, config/app, config/nas, library/items, library/stats, dedup/groups, auto-organize/runs, disk, scan/runs |
| TC-11 | Toast 系统 | 2/2 | dispatchEvent 1 toast 渲染 + 3.5s 自动消失 |
| TC-12 | Cross-page DOM null guard | 1/1 | /settings NAS save 无 null TypeError |
| TC-13 | 12 modal 完整闭环 | 12/12 | 全部 open/close/backdrop cleanup (deleteModal, nas/qbit/ai/emby/organize/autoOrganize/Config + scan + batchIdentify + nfoWrite + organizeBatch) |
| TC-14 | /ui/* HTMX endpoint 合同 | 12/12 | 6 endpoint × (auth 200 HTML + no-auth 401) — providers/workers/sidebar/dashboard/system/workers/todo |
| TC-15 | sidebar hx-boost 真点击导航 | 6/6 | 从 / 点击 6 个 sidebar link, URL + breadcrumb + active class 都对 |
| TC-16 | polling load + schedule + 跨页 | 4/4 | load trigger fire 1 次 + hx-trigger="load, every 5s" 注册正确 + 跨页 navigation 后 polling 不累积 (workers/providers 各 1 次而非多次重复绑定) |
| TC-17 | /files 交互 | 2/2 | 目录跳转 + 勾选 checkbox → `selected-count='1' selected-size='4.0 KB'` |
| TC-18 | API 负例 + schema | 8/8 | 无 token 401 (3 个 endpoint) + 非法 path 403 + /api/scan/status 必填参数 400 + providers schema 含 state/checked_at/message 字段 |

## 修复 trail (test infra + 一处 by-design 确认)

测试期间发现并修了 5 处脚本问题（不是 app bug）:

1. **TC-10 API endpoint 名错** (10 → 4 个 fix): `/api/library/list`→`/items`, `/api/organize-runs`→`/api/auto-organize/runs`, `/api/disks`→`/api/disk`, `/api/scan/status`→`/api/scan/runs?limit=5`, `/api/files?path=/`→`?path=<NAS_BASE_PATH>` 才合法
2. **TC-03 dashboard selector**: 改用 `div[hx-get='/ui/dashboard/system']` 而非不存在的 `getElementById('dashboard-system')`
3. **TC-08 Bootstrap modal backdrop 拦截 click**: 改用 `page.evaluate(fn_call())` 直接调函数 + 每次预先 cleanup `.modal-backdrop` + body classes
4. **TC-07/TC-08 async fetch timing**: `wait_for_function(modal.classList.contains('show'))` 5s timeout 替代固定 sleep
5. **TC-08 aiConfigModal `tmdb-key-input` 为空**: 确认 app.js:2348 故意 `value = ""` 避免 DOM 泄密。改测试断言 badge `tmdb-key-status` 显示 "已配置"

## 测试期 noise 过滤

以下被识别为非 bug 噪音，已从断言里过滤:

- `image.tmdb.org` / `cdn.jsdelivr.net` 外部 CDN `ERR_ABORTED` (浏览器 ORB 阻塞或网络问题，跟 app 无关)
- localhost `net::ERR_ABORTED` polling 请求 (page 导航走时 in-flight polling 必然被 abort)
- `favicon.ico` 缺失

## 关键发现确认

- **NAS 配置真实可填可保存**: TC-08 nasConfigModal field='192.168.31.48' (来自 config/nas.json)
- **Library 真有数据**: 670 items（user 配的真实库）
- **`/ui/status/workers` endpoint 真返 200**: TC-04/05 期间 load trigger fire 一次, 真浏览器下 every 5s polling 在 Phase B ship 时 manual 验证过, 但 e2e 仅 verify endpoint contract 不验证 auto-organize cron 真执行 (cron 业务行为不在 e2e 范围)
- **Onboarding 完整 4-step 流可走**: nas → qbit → tmdb → deepseek → done

## Phase 状态

- Phase A 脚手架: ✅ verified
- Phase B 6 page: ✅ verified (所有 hx-boost / sidebar active class / page-aware loader)
- Phase C destructive drawer: 仅 MVP route (/ui/drawer/*), drawer UI wire-up Phase F 延期 — 当前用 legacy modal
- Phase D onboarding: ✅ verified
- Phase E toast: ✅ verified
- Phase F 清理: app.js 4771 行 cleanup 延期 — 当前 production 可用

## 后续

- ❓ Phase F 完整化: app.js cleanup + drawer UI wire-up 替代 legacy modal trigger
- ❓ /settings 7 sub-page 拆分 (当前单 page 7 卡片，按规划 Phase B+C 应拆)
- ❓ /organize inline 化 (当前 2 modal-trigger card，按规划应嵌入表单)
- ✅ Open source release 已 ship (Phase F: providers status MCP server + 7c2dd27 commit)

## Codex review trail (round 2 补测)

Codex 一次 review 抓出 1 BLOCKER + 5 IMPORTANT + 1 NIT gap (无 stuck, 31K tokens):

| 等级 | Codex 抓的 gap | 对应补测 |
|---|---|---|
| BLOCKER | 12 个 legacy modal 没全部闭环, 只覆盖 9 个 | TC-13 ✅ |
| IMPORTANT | `/ui/sidebar/*` HTMX 合同没测 | TC-14 ✅ (sidebar/badges + status + dashboard 全覆盖) |
| IMPORTANT (deferred) | `/ui/drawer/*` HTMX 合同没测 | ⏸️ Phase F 延期 (drawer UI 当前用 legacy modal 替代, Phase F 完整化时一并补测) |
| IMPORTANT | sidebar `hx-boost` 真点击导航没测 | TC-15 ✅ |
| IMPORTANT | polling 没证明持续 + 跨页不重复绑定 | TC-16 ✅ (跨页累积验证) |
| IMPORTANT | /files 交互 (目录跳转/勾选 UI count) | TC-17 ✅ |
| IMPORTANT | /api/* 缺 schema 字段断言 + 负例 | TC-18 ✅ |
| NIT | toast 只测 synthetic, 未测真实 API 触发 | 保留 TC-11 synthetic (真实 API success/failure → HX-Trigger toast 端到端断言 **未自动化**, 仅 manual smoke) |

## 已知测试环境限制

- **Playwright headless 抑制 setTimeout**: 真浏览器 HTMX `every 5s` polling 正常, headless 默认 throttle 即使加 `--disable-background-timer-throttling` 仍会抑制 hidden page setTimeout. TC-16 改为验证 schedule 注册 + load trigger + 跨页清理, 不依赖 polling 触发次数. 真浏览器 manual smoke 仍需做.
- **TMDB/CDN 外部资源 `ERR_ABORTED`**: 浏览器 ORB / network 问题, 跟 app 无关, 已过滤.
- **localhost `ERR_ABORTED`**: page navigate 时 in-flight polling 必然 abort, 已过滤.
