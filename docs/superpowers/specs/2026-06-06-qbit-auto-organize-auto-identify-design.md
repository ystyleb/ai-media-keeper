# qBit Auto-Organize 自动识别 — 设计 spec

> 日期：2026-06-06
> 状态：设计已 brainstorm 确认，待 user review → writing-plans

## Context（为什么做）

用户期望：qBit 下载完成 → 自动整理（hardlink）到媒体库。实际行为是「下载完什么都没发生」。

端到端真跑排查后的**完整根因链**：

```
下载完没自动整理
  └─ 种子卡在「未识别」(media_files 无 row)
      ├─【根因1】识别不是自动的（cron 只有 reap + qbit_auto_organize 两个 job，
      │           没有 scan/identify cron）→ 新种子永远不会被识别
      └─【根因2】identify route (app.py:4341) 没接 path_resolver →
                  qBit 报的别名路径 /share/downloads/... 被 validate_path 字面前缀
                  拒绝 (400) → 连「手动识别」这条退路都堵死
  └─【已铺好的基础 / 未提交 in-progress 工作】
      path_resolver.py (untracked) + _list_video_paths (app.py:4081) 已接入 →
      auto-organize 的 confidence_gate 拿到的已是 canonical path，namespace 已统一
```

设计目标：让**新下载**的种子下载完成后自动识别、高置信度自动整理；67 个历史积压**不动**（用户明确选择）。

诊断已通过 ground truth 验证：
- DB 查 `media_files` 该电影 0 行（确认从未识别）
- SSH `readlink -f /share/downloads` → `/share/CACHEDEV2_DATA/downloads`（确认是 symlink，物理在沙箱内）
- 用 canonical path 调 identify → 成功（confidence 0.95），再走 organize preview→confirm → SSH 验证 hardlink（src_inode == dst_inode == 434176160，link count=2）

## 决策（已 brainstorm 确认）

| # | 决策 | 理由 |
|---|---|---|
| D1 | 自动识别的种子 **confidence ≥ 0.95** 才自动整理；0.85~0.95 留人工 | 全自动无人工 review 兜底，唯一新增风险是「识别错但 confidence 虚高→移错位置」，高门槛缓解 |
| D2 | 范围只管**新种子**，67 积压不动 | 用户选择；大幅简化（不需改 filter / 批量重置 / backoff） |
| D3 | 架构 = **内联**（dispatch_one 内自动识别 + re-gate），非独立 cron | 新种子都是机器识别、无混合来源，内联最简洁 |
| D4 | 默认 `auto_identify=false`，需显式开启 | destructive 自动化默认保守 |

## 架构与数据流

复用现有 auto-organize dispatch 流程，在 confidence_gate 判 `needs_identify` 时插入「自动识别 → re-gate」一步：

```
cron _cron_qbit_auto_organize
  → qbit.get_torrents → list_completed_torrents(whitelist)
  → filter_unprocessed_hashes (跳过 terminal — 67 积压自动被排除，满足 D2)
  → dispatch_one(torrent, list_video_paths_fn, build_and_start_organize_fn,
                 identify_paths_fn?, confidence_threshold, auto_identify_threshold?)
       → paths = list_video_paths_fn(content_path)   # 已 path_resolver→canonical
       → gate = evaluate_confidence_gate(paths, threshold=0.85)
       → if gate==needs_identify AND identify_paths_fn:        # ← 新增
            result = identify_paths_fn(needs_identify_paths)   # 调 TMDB+DeepSeek 写 cache
            if result.provider_unavailable: return action=locked  # 留 pending 重试，不落 terminal
            gate = evaluate_confidence_gate(paths, threshold=0.95)  # re-gate 高门槛
       → if gate != pass: mark_skipped_at_pending(gate.status)  # low_confidence/unsupported 留人工
       → else: build_and_start_organize_fn → hardlink 到媒体库
```

## 改动单元

每个单元独立可测，按依赖顺序实现。

### 单元 1 — identify route 接 path_resolver（修 400 bug）
- **文件**：app.py `metadata_identify` (4341)
- **改动**：`validate_path(path)` 前先 `path = path_resolver.resolve(path, ssh_exec)`，复用 `_list_video_paths` (4081) 已有写法。
- **效果**：UI 用别名路径 `/share/downloads/...` 手动识别不再 400。
- **独立价值**：本身是一个真 bug 修复，与自动识别解耦。

### 单元 2 — 配置 schema
- **文件**：app.py `load/save_qbit_auto_organize_config` (490/523) + `config/qbit_auto_organize.json`
- **新增字段**：
  - `auto_identify`: bool，默认 `false`
  - `auto_identify_confidence_threshold`: float，默认 `0.95`，clamp [0,1]
- **向后兼容**：旧 config 无字段 → 取默认（关闭）。

### 单元 3 — identify helper 抽取
- **文件**：app.py 新增 `_identify_and_cache(conn, canonical_path) -> dict`
- **逻辑**：抽 `metadata_identify` 的核心（`identify_svc.identify` + `_ssh_stat_paths` + `metadata_cache.upsert_identification`），返回 `{identified: bool, provider_unavailable: bool, confidence: float|None}`。
- **关键约束（线程安全）**：用**传入的 conn**，**不调 `get_db()`** —— cron 是 BackgroundScheduler 后台线程，`get_db()` 会撞 `Working outside of application context`（CLAUDE.md 踩过）。`identify_svc.identify` 走 HTTP（http_client LAN-aware）+ `_ssh_stat_paths` 走 SSH，均不需 Flask 上下文。
- **错误处理**：`ProviderUnavailable` → 返回 `provider_unavailable=True`（不写 cache、不视为识别失败）。

### 单元 4 — dispatch_one 自动识别
- **文件**：services/qbit_auto.py `dispatch_one` (433)
- **新增可选参数**：`identify_paths_fn=None`、`auto_identify_threshold=None`
- **逻辑**：gate==`skipped_needs_identify` 且 `identify_paths_fn` 提供时：
  1. 对 `gate.blockers` 里的 path 调 `identify_paths_fn`
  2. 任一 `provider_unavailable` → 返回 `{action: "locked"}`（留 pending，下周期重试，**不落 terminal**）
  3. 否则用 `auto_identify_threshold` re-gate；非 pass → `mark_skipped_at_pending`（low_confidence/unsupported 留人工）；pass → 正常整理
- **向后兼容**：`identify_paths_fn=None` 时跳过整段，行为与现状完全一致。

### 单元 5 — cron 注入
- **文件**：app.py `_cron_qbit_auto_organize` (5787)
- **改动**：读 `auto_identify` 配置；为 true 时注入 `identify_paths_fn`（包 `_identify_and_cache`，用 cron 已有的独立 conn）+ `auto_identify_threshold`。

### 单元 6 —（可选）UI 开关
- 「自动整理」配置 modal 加「下载完自动识别」开关 + 门槛输入。可后置，先支持 config 文件。

## 错误处理

| 情况 | 行为 |
|---|---|
| 识别出 confidence ≥ 0.95 + movie/tv | 自动整理 |
| 识别出 confidence 0.85~0.95 | `skipped_low_confidence`（terminal，留人工） |
| 识别出 media_type 非 movie/tv | `skipped_unsupported`（terminal） |
| 识别无候选 / media_type None | `skipped_needs_identify`（terminal，留人工；D2 下不重试） |
| TMDB ProviderUnavailable（429/网络/5xx 临时） | `action=locked`，留 pending，下周期重试，**不落 terminal** |

## 安全保证

- 0.95 高门槛防误移（D1）
- 临时错误不落 terminal（可重试，不误判失败）
- organize 本身幂等（已验证：dst_inode==src_inode；重复 dispatch → already_linked）
- 默认 `auto_identify=false`，需显式开启（D4）
- 契约 #5 边界不破：低置信度/识别失败仍被现有 gate 挡住，AI 不越 destructive 边界

## 测试

- **单元（dispatch_one，mock identify_paths_fn）**：
  - 识别 ≥0.95 → 整理
  - 识别 0.9 → `skipped_low_confidence`
  - 识别无候选 → `skipped_needs_identify` terminal
  - `provider_unavailable` → `action=locked`（留 pending，不落 terminal）
  - `identify_paths_fn=None` → 行为不变（向后兼容回归测试）
- **集成（identify route）**：别名路径 `/share/downloads/...` 不再 400（path_resolver 生效）
- **端到端**：配 `auto_identify=true` → 下载测试种子（或临时重置一个积压 row）→ 等一个 cron cycle → 看日志自动识别 + 整理 → SSH 验证 hardlink

## 非目标（YAGNI）

- 67 个历史积压的批量自动处理（D2 明确不做；用户可后续手动识别或单独加批量重置）
- 独立识别 cron（D3 选了内联）
- source-aware 双门槛（无混合来源场景，不需要）
