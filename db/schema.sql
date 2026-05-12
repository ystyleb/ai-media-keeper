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
  kind           TEXT NOT NULL CHECK (kind IN ('delete', 'nfo_write', 'archive', 'purge_provider')),
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
