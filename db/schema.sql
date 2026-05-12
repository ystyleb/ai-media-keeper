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
