-- Phase 1 spike schema: destructive_actions only.
-- Full Phase 1 schema (media_files / watched_items / provider_configs /
-- torrent_snapshots) lands after spike validates the contract.
--
-- Reference: ~/.claude/plans/ai-native-mutable-bubble.md 契约 #1

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS destructive_actions (
  action_id      TEXT PRIMARY KEY,
  kind           TEXT NOT NULL CHECK (kind IN ('delete', 'nfo_write', 'archive', 'purge_provider', 'organize')),
  payload_hash   TEXT NOT NULL,
  payload_json   TEXT NOT NULL,
  expires_at     INTEGER NOT NULL,
  status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'needs_manual_recovery')),
  consumed_at    INTEGER,
  started_at     INTEGER,
  completed_at   INTEGER,
  error          TEXT,
  result_json    TEXT,
  recovery_hint  TEXT,
  created_by     TEXT NOT NULL CHECK (created_by IN ('web_ui', 'mcp', 'cron')),
  created_at     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_actions_expires
  ON destructive_actions(expires_at, status);

CREATE INDEX IF NOT EXISTS idx_actions_recovery
  ON destructive_actions(status, started_at);


-- Phase 2: 媒体识别结果持久化。
-- path 唯一索引（视频文件的 NAS 真实路径）；inode/size/mtime 用于 stale 检测
-- （mtime 变化 → 文件被替换/重剪 → cache 失效）。
-- tmdb_id+season+episode 联合索引为 Phase 3 重复检测准备。
CREATE TABLE IF NOT EXISTS media_files (
  id                       INTEGER PRIMARY KEY,
  path                     TEXT UNIQUE NOT NULL,
  inode                    INTEGER,
  size_bytes               INTEGER,
  mtime                    INTEGER,
  -- 媒体识别核心字段
  tmdb_id                  TEXT,
  imdb_id                  TEXT,
  media_type               TEXT,                  -- 'movie' | 'tv'
  title                    TEXT,                  -- 本地化（zh-CN 下"瑞克和莫蒂"）
  original_title           TEXT,                  -- "Rick and Morty"
  year                     INTEGER,
  season_number            INTEGER,               -- tv only
  episode_number           INTEGER,               -- tv only
  episode_title            TEXT,                  -- 单集标题
  -- 展示用字段（render 卡片不再调 TMDB）
  poster_url               TEXT,
  overview                 TEXT,
  vote_average             REAL,
  genres_json              TEXT,                  -- JSON array
  cast_json                TEXT,                  -- JSON array of names
  runtime_minutes          INTEGER,
  episode_air_date         TEXT,
  episode_overview         TEXT,
  episode_still_url        TEXT,
  -- 契约 #4 provenance
  metadata_source          TEXT,                  -- 'tmdb' | 'nfo' | 'manual'
  metadata_provider        TEXT,                  -- 'tmdb' | 'douban' | ...
  metadata_fetched_at      INTEGER,               -- unix ts
  metadata_status          TEXT NOT NULL DEFAULT 'ok'
                              CHECK (metadata_status IN ('ok', 'needs_review', 'failed', 'stale')),
  metadata_confidence      REAL,                  -- 0.0-1.0
  metadata_pick_source     TEXT,                  -- 'single_exact'|'heuristic'|'llm'|'needs_review'|'manual'
  metadata_reasoning       TEXT,
  -- 解析阶段的原始信息（供前端展示 / debug）
  parse_raw_name           TEXT,
  parse_resolution         TEXT,
  parse_source             TEXT,
  parse_release_group      TEXT,
  -- 时间戳
  first_seen_at            INTEGER NOT NULL,
  last_updated_at          INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_inode
  ON media_files(inode);

CREATE INDEX IF NOT EXISTS idx_media_tmdb
  ON media_files(tmdb_id);

-- Phase 3 重复检测：同一 tmdb_id + 同 season + 同 episode 多份 = 候选
CREATE INDEX IF NOT EXISTS idx_media_dedup
  ON media_files(tmdb_id, season_number, episode_number);

CREATE INDEX IF NOT EXISTS idx_media_status
  ON media_files(metadata_status);


-- Phase 2: 全库后台扫描 worker 用的状态机表。
-- 一个 scan_runs 对应一次"扫某个 base_path"的尝试，包含 N 个 scan_items（每个视频文件）。
-- worker 顺序 claim pending items，写完 done/failed/skipped_unchanged。可被 abort。
CREATE TABLE IF NOT EXISTS scan_runs (
  id              INTEGER PRIMARY KEY,
  base_path       TEXT NOT NULL,
  max_depth       INTEGER NOT NULL DEFAULT 5,
  started_at      INTEGER NOT NULL,
  completed_at    INTEGER,
  status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'done', 'failed', 'aborted')),
  files_total     INTEGER NOT NULL DEFAULT 0,
  files_done      INTEGER NOT NULL DEFAULT 0,
  files_failed    INTEGER NOT NULL DEFAULT 0,
  files_skipped   INTEGER NOT NULL DEFAULT 0,
  current_path    TEXT,                            -- worker 当前正在处理的 path（前端展示用）
  error           TEXT                             -- 整个 run 失败时的原因
);

CREATE INDEX IF NOT EXISTS idx_scan_runs_status
  ON scan_runs(status, started_at);

CREATE TABLE IF NOT EXISTS scan_items (
  id              INTEGER PRIMARY KEY,
  scan_run_id     INTEGER NOT NULL REFERENCES scan_runs(id),
  path            TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'in_progress', 'done', 'failed', 'skipped_unchanged')),
  error           TEXT,
  last_attempt_at INTEGER,
  UNIQUE(scan_run_id, path)
);

-- claim_next_pending 的核心查询：WHERE scan_run_id=? AND status='pending' LIMIT 1
CREATE INDEX IF NOT EXISTS idx_scan_items_claim
  ON scan_items(scan_run_id, status);

-- Phase 4C: qBit completion 自动 organize 状态机
-- 每个 qBit 种子的 info hash 在表里至多一条 row（PK 强制）。Cron 触发时按 hash dedup：
-- 已 terminal 的不再处理；pending/organizing 的等下个周期看 status；
-- 失败的（status='failed'）由 user 手动 reset 或加 attempts 上限。
--
-- terminal 状态：succeeded / failed / skipped_needs_identify / skipped_low_confidence / skipped_unsupported
-- 临时状态：pending（下次 cron 重试）/ organizing（已起 worker，跟 destructive_actions running 关联）
-- 'skipped_locked' 不持久化（lock 冲突时 row 留 pending 让下次 cron 重试）
CREATE TABLE IF NOT EXISTS auto_organize_runs (
  qbit_hash             TEXT PRIMARY KEY,
  category              TEXT,                             -- 触发时种子的 qBit category（审计用）
  torrent_name          TEXT,                             -- 友好显示（审计 / UI 用）
  content_path          TEXT NOT NULL,                    -- 目录或单文件路径
  status                TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'organizing', 'succeeded', 'failed',
                                            'skipped_needs_identify', 'skipped_low_confidence',
                                            'skipped_unsupported')),
  attempts              INTEGER NOT NULL DEFAULT 0,
  last_attempt_at       INTEGER,
  last_error            TEXT,
  action_id             TEXT,                             -- 成功时关联 destructive_actions.action_id
  files_succeeded       INTEGER,
  files_already_linked  INTEGER,
  files_failed          INTEGER,
  created_at            INTEGER NOT NULL,
  completed_at          INTEGER                           -- terminal 时间戳
);

-- partial unique index 防并发 organizing：同 qbit_hash 同时只能一个 organizing
-- （在 PK 之外多一道保险，因为 status 是可变字段，update 冲突直接 IntegrityError）
CREATE UNIQUE INDEX IF NOT EXISTS uniq_auto_org_organizing
  ON auto_organize_runs(qbit_hash) WHERE status = 'organizing';

CREATE INDEX IF NOT EXISTS idx_auto_org_status
  ON auto_organize_runs(status, last_attempt_at);

CREATE INDEX IF NOT EXISTS idx_auto_org_history
  ON auto_organize_runs(completed_at);
