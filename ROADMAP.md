# NASVault Roadmap

> 用户感知优先级排序，配合 plan v3 (`~/.claude/plans/ai-native-mutable-bubble.md`) 看更全。
> 维护原则：每完成一项把它移到"已完成"段，重排"接下来"的次序。

---

## 已完成（截至 2026-05-15）

### Phase 4 Phase C: qBit 完成后自动 organize

Phase 4A/B 是手动触发。**Phase 4C** 加 cron 定时扫 qBit 已完成种子 → confidence + category 双层授权后**自动** organize。契约 #5 (AI 介入边界) 仍守住：confidence ≥ threshold + category ∈ user 配置白名单 = user 通过规则**显式授权** AI 触发，AI 没决策"哪些该 organize"。

- **4C.0 schema migration + config**：
  * `auto_organize_runs` 表（qbit_hash PK + 7 状态 + partial unique idx status='organizing'）
  * `phase5_migrate` idempotent helper（老 DB 加表，新 schema.sql 已含）
  * `config/qbit_auto_organize.json` (enabled / categories[] / poll_interval_minutes / confidence_threshold) + load/save helper 边界清洗（防 `0 or 5=5` short-circuit / None category 过滤 / clamp 越界）
- **4C.1 qBit completion detector + lifecycle helpers**：
  * `services/qbit_auto.list_completed_torrents`（progress >= 0.999 + state ∈ 7 个 completed 枚举 + category 在 whitelist + content_path 非空 + hash 非空）
  * `filter_unprocessed_hashes` 跳过已 terminal / organizing
  * row 生命周期：`claim_pending_run`（INSERT OR IGNORE 幂等单语句）/ `mark_organizing`（guarded UPDATE WHERE status='pending' + partial unique 兜底）/ `mark_terminal`（COALESCE 保护 action_id；attempts 不在此增防双计数）/ `mark_skipped_at_pending`（confidence_gate fail 直转 terminal skip）
- **4C.2 confidence gate (契约 #5 enforcement)**：
  * `evaluate_confidence_gate(conn, paths, threshold)` fail-fast 4 优先级：needs_identify > unsupported > low_confidence > pass
  * None confidence 视为 0；media_type ∉ {movie, tv} → skipped_unsupported
- **4C.3 auto trigger executor (callback injection)**：
  * `dispatch_one(conn, torrent, *, list_video_paths_fn, confidence_threshold, build_and_start_organize_fn)` 整套 orchestration：claim → list paths → confidence_gate → build_and_start → mark_organizing。返 dict.action enum (started / skip_existing / skipped / locked / errored / claim_lost)
  * `reconcile_organizing_rows(conn)` 周期同步 organizing → terminal（基于 destructive_actions 真实状态；succeeded → 抽 result_json counts；failed / needs_manual_recovery → mark_terminal failed）
  * `app.py::_build_and_start_auto_organize(paths, qbit_hash)` 内层 callback：with app.app_context() 包裹（cron thread 无 request context）→ 复用 organize preview 构造逻辑（compute_plan + dst stat）但精简（confidence_gate 已守门，不需要 6 状态分类）→ created_by='cron' + atomic_consume + verify + start_organize_executor。ConcurrentOrganizeError → rollback_to_pending + return locked；其他异常 → _mark_terminal failed
- **4C.4 cron scheduler hook**：
  * `_cron_qbit_auto_organize` APScheduler job (interval 从 config 读，max_instances=1 + coalesce=True 防 backlog)
  * 周期：reconcile (独立于 enabled) → load config → enabled/whitelist 守门 → qbit.get_torrents → list_completed → filter unprocessed → for-each dispatch_one (locked → break 让 organize 跑完)
  * 任意单种子 fail 不阻塞其余 (catch per-torrent)
- **4C.5 UI config + history view**：
  * 4 个 routes：GET/POST /api/config/qbit-auto-organize (含现存 qBit categories suggest + qbit_fetch_error graceful), GET /api/auto-organize/runs (status filter + pagination), POST /api/auto-organize/reset (terminal 才允许)
  * Topbar 加 "自动整理" 按钮 + autoOrganizeConfigModal modal-lg 双 tab (配置 + 历史)
  * categories chip 输入 + qBit 现存 category click-to-add
- **4C.6 review + docs**：
  * code-reviewer agent 2 轮 review：r1 抓 2 BLOCKER + 1 IMPORTANT + 2 NIT（progress 浮点 / claim_lost 游离 worker / enabled+空 whitelist / lambda 注释 / status_filter 枚举校验），r2 抓 r1 派生 1 BLOCKER（claim_lost 用 _mark_terminal 无守门污染 worker 状态机；正解：完全不动，靠 worker 自身 mark_terminal_if_running guard + organize 幂等）
  * 总 tests 466 → 565（+99 涵盖 schema migration / config / completion detector / confidence gate / dispatch / reconcile / cron / 4 routes / regression）

### Phase 4 Phase B: 批量目录 organize

Phase 4A 是单文件 manual organize。**Phase 4B** 把单步骤扩到一次性整理一整个目录（一个剧 25 集 / 一个发布版本的多 part），分类 + 勾选 + 后台进度 + abort。

- **4B.0 schema TTL 调整 + helper**：
  * `destructive_action.TTL_BY_KIND['organize']` 600 → 1800s（N=500 plan 给用户 30 min 审阅）
  * `RUNNING_TIMEOUT_BY_KIND['organize']` 60 → 1800s（executor 跑 N=500 文件 5-8 min，reaper 不能误判）
  * 新增 `update_running_result(conn, action_id, partial_result)` helper（仅 status='running' 时生效，避免踩 terminal 状态）
  * `MAX_ORGANIZE_BATCH_ITEMS=500` + `ORGANIZE_BATCH_INLINE_THRESHOLD=5` 常量
- **4B.1 dir-preview endpoint + batch cache**：
  * `GET /api/organize/dir-preview?path=&max_depth=2&limit=500`：复用 `_list_video_paths` + `_ssh_stat_paths` 一次性批量 stat；调 `metadata_cache.get_many_by_path` 一次 SQL IN
  * 6 状态分类：`will_link` / `already_linked` / `conflict` / `needs_identify` / `unsupported` / `not_applicable`
  * 只读 dashboard，不签名、不落 destructive_actions row；返 counts + items + base_path/max_depth/limit_reached/total
  * `services/metadata_cache.get_many_by_path` batch helper（500-chunk + stale 检测，替代 N 次 get_by_path）
- **4B.2 preview multi-item + partial admission**：
  * `_do_action_preview` organize 分支从 fail-fast 改 partial admission：所有可算 plan 的 items（will_link + already_linked + conflict）都进 payload 给 executor 处理；needs_identify / unsupported / not_applicable 仅进 preview_items 显示
  * batch SSH stat: src + 所有 dst 合并 2 次 round-trip（之前 N×2 次）；batch metadata_cache batch SQL（之前 N 次单 SELECT）
  * soft cap MAX_ORGANIZE_BATCH_ITEMS=500，超出 400 batch_too_large
  * batch-level duplicate dst_path detection（同 batch 两 src → 同 dst → 后一个标 conflict + duplicate_dst_path_within_batch reason）
  * 0 will_link → 不签名不落 row（节省 destructive_actions row + reaper noise）
- **4B.3 background runner + status polling + abort**：
  * `services/organize_runner.py`：单 thread + 模块级 `_active_lock` + `_active_action_id`（抄 scanner pattern）+ module-level `_abort_flags` dict
  * `start_organize_executor(...)`、`request_abort(...)`、`get_organize_status(...)`、`try_acquire_inline_lock` / `release_inline_lock` 公共 API
  * 提取 `_organize_executor_one_item`：per-item 核心逻辑 inline + background 共用（保证两路径行为一致）
  * `/api/action/confirm` 分流：`kind=organize + items > 5` → 手动 atomic_consume + verify token + start background worker + return 202 + polling_url；否则 inline 同步
  * `/api/action/status?id=<action_id>` 复用 destructive_actions.result_json 渐进式写（每 item commit 一次给前端 polling 即时看到）
  * `/api/action/abort` POST 设 abort flag，worker 下一 item 边界自然退出（不取消正在跑的 SSH）
  * 契约 #7（partial 部分成功）+ #8（preview 分类状态机）+ #9（progressive result_json）
- **4B.4 三 step UI modal**：
  * 文件浏览器目录行新增「整理目录」按钮（folder-symlink icon）
  * `organizeBatchModal` modal-xl 三 step pane：dashboard / preview / progress
  * step 1 dashboard：6 状态 counts cards + 折叠分组列表（will_link 默认展开 + 引导 needs_identify 用户先去文件视图批量识别）
  * step 2 preview：紧凑 table + checkbox + 状态徽章 + 默认全勾 will_link；already_linked/conflict readonly disabled；全选/全不选 toggle
  * step 3 progress：进度条 + status_counts + current_item + failed items 折叠列表 + abort 按钮
  * request seq 防 stale response（codex r1 B2 模式）+ modal hidden 清 polling timer
- **4B.5 端到端冒烟 + 6 轮 backend codex review + 文档**：
  * 6 轮 backend codex review 累计 7 BLOCKER + 10 IMPORTANT + 5 NIT 全修
  * 真机端到端冒烟见 README「整理到媒体库」section
- **关键发现**（codex r1-r6 trace）：
  * codex r3 BLOCKER 2 inline organize 不持 active lock → background + inline 可并发副作用，修法：`_organize_executor` 顶部 acquire `try_acquire_inline_lock`，失败全标 `another_organize_running` failed
  * codex r4 BLOCKER `gunicorn -w N` 不会自动设 `WEB_CONCURRENCY=N` → r3 的 env check 被绕过 → 改用 `fcntl.flock(.worker.lock)` 跨进程硬锁
  * codex r5 BLOCKER `gunicorn --preload` 模式 fcntl 被 fork 继承绕过 → 加 sys.argv 检测 + gunicorn.conf.py 模板 + on_starting hook
  * codex r6 BLOCKER preload 通过 config file / `GUNICORN_CMD_ARGS` env 设置 sys.argv 无法捕获 → 加 `os.register_at_fork(after_in_child=...)` callback (preload-aware ground truth)
  * **5 层 worker hard guard**（WEB_CONCURRENCY env + sys.argv check + fcntl.flock import-time + at_fork in child + gunicorn cfg hook）覆盖所有 gunicorn 启动路径下的 multi-worker / preload 风险；100% 多 worker safety 留 Phase 4C SQLite lease
- **432 unit tests pass**（379 → 432，+53 涵盖 dir-preview / get_many / multi-item preview / background runner / status / abort / inline lock / fork callback / bool index reject）

### Phase 4 Phase A: 下载后 organize（hardlink + 独立目录 + NFO）

PT 玩家的核心工作流：下载完文件留在 `/downloads/` 占位保种，手动 hardlink 到媒体库太繁琐 + 易错（特殊字符 / TV 季集编号 / NFO 单独写）。Phase 4A 把这块自动化。**手动触发版**（验证识别精度后再考虑批量 → Phase 4B / 自动监听 → Phase 4C）。

- **4A.0 schema migration**：`destructive_actions.kind` CHECK 加 `'organize'`（重建表 + 数据迁移 idempotent，269 行无损迁移真机验过）；`config/organize.json` 配置文件 + load/save helper
- **4A.1 services/organize.py**：`sanitize_for_path`（替换 `/\:*?"<>|` 控制字符为 `-`，合并多空格，去 SMB 末尾 dot/space）+ `compute_organize_plan` 推 movie/tv 目标路径 + `(YYYY)` 双重年份去重 + TV `tvshow.nfo` 落 series 根而非 season 目录。19 unit tests
- **4A.2 SSH helpers**：`_ssh_mkdir_p` + `_ssh_ln`（含 `[ -d ]` pre-check + `[ ! -f ]` post-stat 防 silent ln-into-dir race；BusyBox 兼容不依赖 `-T` flag；race-into-dir 时返 `DST_NOT_REGULAR` marker，不自动 cleanup 防误删 — orphan 留给 user SSH 手工查）
- **4A.3 organize executor + preview validator**：
  * `_do_action_preview` kind=`organize`：校验 movies_root/tv_root → SSH stat src + metadata_cache.get_by_path → `compute_organize_plan` → SSH stat dst 看 already_linked / conflict
  * `_organize_executor`：Pattern C 双 inode 锚定（src + dst verify）+ Pattern D NFO 独立 status（hardlink ok + NFO 失败 = 部分成功，不回滚 hardlink）+ metadata_snapshot 强 validation
  * `_ssh_create_nfo_if_absent`：atomic create-only NFO 写入（`ln tmp final` + `[ -d ]` pre-check + `[ ! -f ]` post-stat），dst 已存在则 ln 失败返 `nfo_exists` skip 不覆盖
  * 18+ contract tests（preview + confirm + race coverage + metadata_snapshot 校验）
- **4A.4 整理 UI**：详情面板「整理到媒体库」按钮（仅 media_type ∈ {movie, tv} 显示）+ organizeModal preview/confirm 双段。预览显示源/识别/目标/NFO/tvshow.nfo + already_linked/conflict 状态徽章。confirm 后 per-item 结果：成功（含 src/dst inode 共享 + qBit 保种提示）/ 部分成功（hardlink ok + NFO 失败单独 warning）/ already_linked（idempotent skip）/ failed + reason + hint（orphan path 提示）。修复 stale response race 用 request id guard + action snapshot binding
- **4A.5 媒体库目标根配置**：topbar 加「媒体库」按钮 + organizeConfigModal（MOVIES_ROOT / TV_ROOT input + 测试 + 保存）+ 后端 3 routes（GET/POST/test 都强制绝对路径校验）
- **8 轮 backend codex review 收敛**：累计 14 BLOCKER + 10+ IMPORTANT 全修。最深教训：POSIX 没有 atomic verify-then-unlink primitive → 接受「race-into-dir orphan + hint user 自助查」策略，比自动 cleanup 误删风险小
- **2 轮 UI codex review 收敛**：UI race（modal stale response / confirm 响应绑定）+ error mapping 完整覆盖
- **379 unit tests pass**（284 → 379，+95 涵盖 organize 全链路）

### Phase 3: 重复检测 + Emby 观看进度 + Archive stub

- **3.0 Schema migration**：media_files 加 10 字段（4 parse quality + 3 score cache + 3 tmdb id splits）；新表 `watched_items` / `watch_sync_runs` / `dedup_weights` / `dedup_weights_meta` / `media_file_hdr_profiles`。Pattern B 通过 CHECK constraint 强制 watched_items 的 movie/tv 互斥 + mapping_status 强制 join key。Partial unique index 保证 watch_sync_runs 同 provider 单飞
- **3.1 Quality 字段提取**：FilenameParse 加 codec/color_depth/hdr_profiles/container/audio_codec；HDR 长 token 优先 + word-boundary 避子串误中 + HDR10+ implies HDR10 自动去重；codec fallback 解决 guessit AV1 限制；upsert 同步写 split tmdb ids + HDR 子表 DELETE+INSERT 同事务
- **3.2 Dedup engine**：`/api/dedup/groups` 按 deletable bytes 倒排；`compute_quality_score(row, hdr_list, weights)`；Pattern A truth source = `dedup_weights` 表 + `current_hash`；bulk fetch 4-6 SQL 不随 group 数 scale；`update_weights` 拒绝 NaN/Inf/negative；`refresh_all_quality_scores` 500 行一 batch commit
- **3.3 Strict snapshot delete**：`/api/action/preview` `kind=delete` 必传 `source`；`source='dedup'` 必走 `snapshot_mode='strict'`；strict 模式 candidate 必含 `expected_inode/size/mtime`；inode/size/mtime mismatch → 409 + mismatches[]（不产 token）；前端"重复"tab + 选中后双段 confirm
- **3.4 Emby provider + watch_sync**：`X-Emby-Token` REST；Episode `ProviderIds.Tmdb` 是 episode id 不是 series（map 时通过 SeriesId 批查 series tmdb）；mapping_status `mapped`/`fallback_se`/`unmapped`；`claim_sync_run` 用 `BEGIN IMMEDIATE` + partial unique index 单飞；reaper 标 stuck runs 为 aborted
- **3.5 Watched-stale + archive stub**：`/api/library/watched-stale` 用 3 分支 UNION（movie / tv-episode_id / tv-series-se-fallback），每支双侧 NOT NULL + media_type 等值；archive executor stub raise `ArchiveDisabledError` → `destructive_actions.status='failed' + error='ArchiveDisabledError: archive_kind_disabled_in_phase3'`，preview/confirm 仍走完整契约 #1
- **284 unit tests**（189 → 284 = +95 新）+ 多轮 code-reviewer review 收敛

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

### 1. ~~可点击候选 → 手动绑定~~ ✅ 2026-05-16 完成

needs_review / heuristic_fallback 时候选列表点击触发 `POST /api/metadata/bind` → `pick_source='manual'` + `confidence=1.0` 强写 cache。15 unit tests。

**契约保证**：
- `tmdb_id` 强制 numeric regex + `season/episode` 强制 `int && !bool && 0..9999` 防 SDK URL path injection
- SSH stat 必须 `exists=True` 才写 cache（防错位到已删文件）
- 复用 `upsert_identification` + IdentifyResult 通道，跟 LLM/heuristic 写入路径完全等价
- UI 通过 `CustomEvent('metadata-bound', bubbles:true)` 让 organize 按钮立即响应

**review 经过**：codex r1 抓 BLOCKER season/episode injection + IMPORTANT organizeBtn 不刷新 → r2 0 BLOCKER ship.

### 2. `/api/providers/status` + 顶部 banner（S，依赖：无）

UI 顶部显示 TMDB / DeepSeek 当前可用性（OK / auth_failed / not_configured / rate_limited）。Scanner 按 state 降级而非崩。

**为什么重要**：当前 key 错了 / 失效，用户看到的是"识别失败"，不知道是 key 问题。

### 3. ~~全库后台扫描 worker~~ ✅ Phase 2 已完成

`services/scanner.py` + scan_runs/scan_items 状态机 + 4 routes (`/api/scan/start`/`status`/`abort`/`runs`)；增量识别（mtime+inode stale 检测）；UI 库视图右上"开始扫描"按钮 + worker 后台跑。

### 4. ~~重复 release 检测~~ ✅ Phase 3.2/3.3 已完成

### 5. ~~观看进度集成~~ ✅ Phase 3.4 已完成（Emby；Plex/Jellyfin/Trakt 通过 WatchSourceProvider ABC 后续可加）

### 6. ~~Archive 操作~~ ⏸️ Phase 3.5 仅 stub（executor 不接 SSH 真执行）；用户选定"看完就删"派，schema 占位保留以备后续

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

### 9. NFO episode-specific 元数据补全（✅ 2026-05-16 完成）

**问题（已修）**：Phase 4A NFO writer 写 episode `.nfo` 时，`<plot>` / `<aired>` / `<thumb>` 直接 copy series-level 字段。结果 S01E01 / S01E02 / ... 的 NFO 全部 plot 相同 = series 整体剧情。Plex/Emby 仍能识别（靠 season+episode 编号），但"本集简介"是 series 简介，体验降级。

**修法**：`services/metadata_cache.py` 加 `ensure_episode_details(conn, provider, cached) -> (CachedMetadata, drift_detected)` helper — 当 cached tv episode 缺 `episode_overview/air_date/still_url` 时 lazy 调 TMDB `/tv/{id}/season/{N}/episode/{N}` 补全，guarded UPDATE 防 lookup 期间 cache drift。`_write_organize_nfo` 在 build payload 前调用，build NFO 时优先用 episode-specific 字段。

**契约保证**：
- [drift-safe] guarded UPDATE `WHERE path=? AND tmdb_id=? AND season=? AND episode=?` + 显式 drift_detected flag，scanner concurrent 改 cache → 不写错位数据 + caller short-circuit
- [Pattern D] `_write_organize_nfo` 整段包 try，DB / build error 都转 `failed: ...` nfo_status，不污染 hardlink 主状态
- Lazy enrich（不是 scanner 阶段）— 每集多 1 次 TMDB API 只在真正 organize 时付出

**测试**：11 unit test 覆盖 provider None / movie / 缺 season / 缺 tmdb_id / 已 enriched / happy path / provider raises / lookup None / episode None / drift signaled / DB error。580 total tests pass。

**review 经过**：codex r1 抓 BLOCKER drift race + IMPORTANT DB leak → r2 抓派生 BLOCKER refresh fallback silently 退旧 + IMPORTANT cache read 在 try 外 → r3 0 BLOCKER ship。

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
