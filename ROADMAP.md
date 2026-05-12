# NASVault Roadmap

> 用户感知优先级排序，配合 plan v3 (`~/.claude/plans/ai-native-mutable-bubble.md`) 看更全。
> 维护原则：每完成一项把它移到"已完成"段，重排"接下来"的次序。

---

## 已完成（截至 2026-05-12）

### Phase 1: Destructive Action 契约（基建）

- 契约 #1：`/api/action/preview` + `/api/action/confirm` 双段式 + HMAC signed_token + 3 段状态机 (`pending → running → succeeded/failed/needs_manual_recovery`)
- 契约 #2：Ground truth snapshot（preview 阶段 SSH stat 落 payload，confirm 重读比对，mtime/inode 漂移 → abort）
- 执行用 inode 锚定 (`find -xdev -inum N`)，防 preview→confirm 之间 mv 撞错文件
- Legacy `/api/delete` + `/api/delete-complete` 已 410 Gone
- Server secret 持久化（`config/.signing_key` chmod 600）+ ephemeral dev fallback
- Cron：crash recovery（running 超时 → needs_manual_recovery）+ 过期 pending 清理
- 73 tests（13 routes + 23 destructive_action + 其他 helper）

### Phase 2: 元数据增强（部分）

- MetadataProvider 抽象 + TMDB v3 provider（BYOK，UI 配置，落 `config/.tmdb_key`）
- 4-tier identify pipeline:
  - `single_exact`：候选只有 1 个 + title exact → 直绑
  - `heuristic ≥ 0.9`：title+year+vote 评分够高 → 直绑
  - `llm`：LLM grounded select（DeepSeek V4 reasoning，从 candidates 里挑 id）
  - `needs_review`：以上都 fail → 留给用户
- LLM filename rescue：guessit 拿不到 title / TMDB 0 候选 → LLM 重提取 title 再搜
- 单文件 AI 识别（详情面板按钮）+ 批量识别（目录扫 200 个文件）
- NFO 写回（kind=`nfo_write`，走契约 #1，atomic mv + .bak backup + readback verify）
- Sidecar `.nfo` 自动展示（点视频文件直接看 NFO 卡，不必每次按按钮识别）
- **媒体识别结果持久化** (`media_files` 表) — 识别一次后下次秒出，stale 检测靠 mtime+inode
- 102 tests（含 11 个 metadata_cache + 18 个 nfo_writer）

---

## 接下来（按 ROI 排序）

每一项都标注 **依赖**、**工程量**（S=1天 / M=2-3天 / L=1周+）、**用户感知**。

### 1. 可点击候选 → 手动绑定（S，依赖：无）

needs_review / heuristic_fallback 时 UI 已展示候选列表，但点不动。让用户从列表里点一个 → 当作绑定 + 写 cache + 可顺手写 NFO。

**为什么先做这个**：LLM 不一定都能选对，需要人工兜底。一次点击就能让"50% 待复核" 变 "95% 已绑定"。

### 2. `/api/providers/status` + 顶部 banner（S，依赖：无）

UI 顶部显示 TMDB / DeepSeek 当前可用性（OK / auth_failed / not_configured / rate_limited）。Scanner 按 state 降级而非崩。

**为什么重要**：当前 key 错了 / 失效，用户看到的是"识别失败"，不知道是 key 问题。

### 3. 全库后台扫描 worker（M，依赖：现有持久化）

接入 plan v3 的 `scan_runs` + `scan_items` 状态机。用户点 "扫描整个 NAS"，后台 worker 跑遍：
- 增量：跳过 mtime 不变的（已 cache）
- 进度推送：前端轮询 `/api/scan/status?id=N` 看 `files_done / files_total`
- 可中断恢复：worker crash 后 reclaim `in_progress` 超时项

**为什么这个时机做**：cache 已有，scan 只是"批量+持久化+进度追踪"的组合。没有这个，用户得逐目录手动按"批量识别"。

### 4. 重复 release 检测（M-L，依赖：#3 全库扫描）

Phase 3 杀手锏。SQL `GROUP BY tmdb_id, season_number, episode_number HAVING COUNT(*) > 1` 找出重复，按 quality_score（resolution / HDR / source / codec / release_group）推荐保留哪份。删除走契约 #1（preview → snapshot 重核 → confirm）。

**为什么这是杀手锏**：tinyMediaManager / Sonarr 都不做"找重复"。这是 PT 玩家最痛的点之一（误下不同压制版本占满硬盘）。

### 5. 观看进度集成（M，依赖：#3）

Plex / Jellyfin / Trakt 各一个 WatchSourceProvider，拉"已看完"列表入库。UI 新面板"已看完 + N 天未动 = 归档候选"。

### 6. Archive 操作（M，依赖：#5）

冷存储归档：copy → verify(hash) → mv → unlink 源文件。走契约 #1，kind=`archive`，三段状态机 + 失败恢复（`partial_state` 字段 + manual recovery 面板）。

### 7. MCP server（M，依赖：#1-#6 业务能力齐了）

Claude Desktop / Code 直接管 NAS：
- `list_files` / `find_recent_downloads` / `get_disk_usage`
- `find_duplicates` / `list_archive_candidates`
- `prepare_destructive_action` + `confirm_destructive_action`（wrap REST API，不另开口子）

**安全模式**：默认 stdio + localhost，远程访问推 Tailscale。

### 8. 开源发布（S，依赖：#7 + 完整功能）

- README + 3 张 GIF（删除 / 元数据 / 重复检测）
- CONTRIBUTING.md 分层（加 BT adapter L1 / metadata provider L2 / watch source L3 / mutating 行为 L4）
- Docker Compose（`docker-compose up` 一键跑）
- 模块化拆 app.py（拆 routes / services 模块）+ ES module 前端
- 引入 ruff CI + lint
- 平台兼容矩阵（QNAP / Synology / Linux 的 `stat` / `du` 差异 shim）

---

## 不做的事（明确排除）

- 自己的 BT 下载器（重复造 qBit/Transmission 轮子）
- 自己的 media server（重复造 Plex/Jellyfin）
- 移动 app（web 响应式够用）
- 完全无人值守 agent（v1 永久 `suggest_only`，AI 不主动 destructive）
- 多用户 / 团队功能（个人工具定位）

---

## 长期定位（Phase 5+，远期想法）

- AI 主动建议（每周扫库 → "你库里 N 个待办" 推送，但不自动执行）
- 智能命名修复（解析失败的文件，让 LLM 提议改名 → 用户 confirm）
- 跨 NAS 同步（家里 + 公司 + VPS）

---

## 关键不变量（任何新功能都不能破坏）

1. **AI 不直接 destructive**：所有 mutating 走契约 #1 preview+confirm 双段式
2. **LLM grounded**：LLM 只从 candidates 选 id，永不生成 id
3. **Snapshot ground truth**：confirm 阶段重读 SSH，不信 client / 不信缓存
4. **Inode-anchored execute**：删 / 归档用 `find -inum`，不用裸 path
5. **DeepSeek-first**：默认 LLM = DeepSeek V4，不 default Anthropic（成本敏感）
6. **零 env，全 UI 配置**：所有 key / 密码走 `config/*` chmod 600，不强制 env
