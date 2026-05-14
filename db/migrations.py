"""Schema migrations.

Phase 1/2 schema lives in db/schema.sql (idempotent CREATE-IF-NOT-EXISTS).
SQLite ALTER TABLE ADD COLUMN is not idempotent (no IF NOT EXISTS), so Phase 3+
runs through Python helpers that check PRAGMA table_info before ALTER.

Each migration function is idempotent: safe to run on a brand-new DB or
on an already-migrated DB. Apply order in app.py:
    init_schema(conn, schema_path)   # Phase 1/2 tables + indices
    phase3_migrate(conn)             # Phase 3 columns/tables/indices/data
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time

logger = logging.getLogger(__name__)


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> bool:
    existing = _existing_columns(conn, table)
    if column in existing:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return True


# Default dedup scoring weights (Phase 3.2). Seeded on first migration.
# Keep values aligned with services/dedup.py: any change requires migration bump
# AND quality_score recompute (score_weights_hash drift triggers in-memory recompute).
DEFAULT_DEDUP_WEIGHTS: dict[str, float] = {
    "resolution.4K": 40,
    "resolution.2160p": 40,
    "resolution.1080p": 25,
    "resolution.720p": 10,
    "resolution.480p": 2,
    "hdr.DolbyVision": 25,
    "hdr.HDR10+": 20,
    "hdr.HDR10": 10,
    "hdr.HLG": 5,
    "source.UltraHDBluRay": 18,
    "source.BluRay": 15,
    "source.WEB-DL": 10,
    "source.WEBRip": 6,
    "source.HDTV": 3,
    "source.DVDRip": 1,           # Phase 3.2 r2: dedup._normalize_source_key 映射 DVD/DVDRip 到此 key

    "codec.AV1": 18,
    "codec.H.265": 15,
    "codec.H.264": 5,
    "color_depth.10-bit": 5,
}


def _canonical_weights_hash(weights: dict[str, float]) -> str:
    """Canonical hash of weights map.

    [code-enforced] Numbers are normalized to float to keep hash stable across
    (a) Python int literals (40) in source vs (b) DB REAL readback (40.0).
    Without this, the meta hash from default-seeded weights would not match
    the hash computed after a roundtrip through dedup_weights table.
    """
    import hashlib

    canonical = json.dumps(
        {k: float(weights[k]) for k in sorted(weights.keys())},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_weights_from_db(conn: sqlite3.Connection) -> dict[str, float]:
    """Read current weights from DB; used as canonical source for meta hash."""
    rows = conn.execute("SELECT key, weight FROM dedup_weights").fetchall()
    return {row[0]: float(row[1]) for row in rows}


def phase3_migrate(conn: sqlite3.Connection) -> dict:
    """Apply Phase 3 schema changes. Idempotent.

    Returns a summary dict with counters (columns_added, rows_migrated, etc.)
    so callers can log meaningful info on first apply.
    """
    summary = {
        "columns_added": 0,
        "tmdb_id_rows_migrated": 0,
        "weights_seeded": 0,
        "weights_meta_refreshed": 0,
    }

    # --- media_files: 10 new columns (4 parse + 3 score + 3 tmdb id splits) ---
    new_columns = [
        # parse_*
        ("parse_codec", "TEXT"),
        ("parse_color_depth", "TEXT"),
        ("parse_container", "TEXT"),
        ("parse_audio_codec", "TEXT"),
        # quality_score 三件套
        ("quality_score", "REAL"),
        ("score_weights_hash", "TEXT"),
        ("score_computed_at", "INTEGER"),
        # tmdb id 拆分（Pattern B：不混 movie/tv id space）
        ("tmdb_movie_id", "TEXT"),
        ("tmdb_series_id", "TEXT"),
        ("tmdb_episode_id", "TEXT"),
    ]
    for col, decl in new_columns:
        if _add_column_if_missing(conn, "media_files", col, decl):
            summary["columns_added"] += 1

    # --- indices for new tmdb id columns + 归档候选 SQL ---
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_media_movie_id
          ON media_files(tmdb_movie_id);
        CREATE INDEX IF NOT EXISTS idx_media_series_se
          ON media_files(tmdb_series_id, season_number, episode_number);
        CREATE INDEX IF NOT EXISTS idx_media_episode_id
          ON media_files(tmdb_episode_id);
        CREATE INDEX IF NOT EXISTS idx_media_seen_size
          ON media_files(first_seen_at, size_bytes);
        """
    )

    # --- 老 row 数据迁移：tmdb_id → tmdb_movie_id / tmdb_series_id ---
    # idempotent：只动 tmdb_*_id 仍是 NULL 的行
    cur = conn.execute(
        """
        UPDATE media_files
           SET tmdb_movie_id = tmdb_id
         WHERE media_type = 'movie'
           AND tmdb_id IS NOT NULL
           AND tmdb_movie_id IS NULL
        """
    )
    summary["tmdb_id_rows_migrated"] += cur.rowcount or 0
    cur = conn.execute(
        """
        UPDATE media_files
           SET tmdb_series_id = tmdb_id
         WHERE media_type = 'tv'
           AND tmdb_id IS NOT NULL
           AND tmdb_series_id IS NULL
        """
    )
    summary["tmdb_id_rows_migrated"] += cur.rowcount or 0

    # --- HDR multi-profile 子表（Pattern B：精确匹配，不用 pipe-string）---
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS media_file_hdr_profiles (
          media_file_id  INTEGER NOT NULL REFERENCES media_files(id) ON DELETE CASCADE,
          profile        TEXT NOT NULL CHECK (profile IN ('HDR10', 'HDR10+', 'DolbyVision', 'HLG')),
          PRIMARY KEY (media_file_id, profile)
        );
        CREATE INDEX IF NOT EXISTS idx_hdr_profile
          ON media_file_hdr_profiles(profile);
        """
    )

    # --- watched_items 表（schema-enforce media_type ↔ id 互斥 + mapping join key 必填）---
    # CHECK constraints 写法见 plan 3.0 节
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS watched_items (
          id                INTEGER PRIMARY KEY,
          provider          TEXT NOT NULL CHECK (provider IN ('emby', 'plex', 'jellyfin', 'trakt')),
          provider_item_id  TEXT NOT NULL,
          media_type        TEXT NOT NULL CHECK (media_type IN ('movie', 'tv')),
          tmdb_movie_id     TEXT,
          tmdb_series_id    TEXT,
          tmdb_episode_id   TEXT,
          imdb_id           TEXT,
          season_number     INTEGER,
          episode_number    INTEGER,
          title             TEXT,
          year              INTEGER,
          watched_at        INTEGER NOT NULL,
          fetched_at        INTEGER NOT NULL,
          raw_hash          TEXT,
          mapping_status    TEXT NOT NULL DEFAULT 'mapped'
                              CHECK (mapping_status IN ('mapped', 'fallback_se', 'unmapped', 'ambiguous')),
          mapping_confidence REAL,
          mapping_source    TEXT,
          CHECK (
            (media_type = 'movie' AND tmdb_series_id IS NULL AND tmdb_episode_id IS NULL
                                  AND season_number IS NULL AND episode_number IS NULL)
            OR
            (media_type = 'tv' AND tmdb_movie_id IS NULL
                               AND season_number IS NOT NULL AND episode_number IS NOT NULL)
          ),
          CHECK (
            mapping_status = 'unmapped'
            OR mapping_status = 'ambiguous'
            OR (media_type = 'movie' AND tmdb_movie_id IS NOT NULL)
            OR (media_type = 'tv' AND (tmdb_episode_id IS NOT NULL OR tmdb_series_id IS NOT NULL))
          ),
          UNIQUE(provider, provider_item_id)
        );
        CREATE INDEX IF NOT EXISTS idx_watched_movie
          ON watched_items(tmdb_movie_id) WHERE tmdb_movie_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_watched_series_se
          ON watched_items(tmdb_series_id, season_number, episode_number)
          WHERE tmdb_series_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_watched_episode
          ON watched_items(tmdb_episode_id) WHERE tmdb_episode_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_watched_when
          ON watched_items(watched_at);
        """
    )

    # --- watch_sync_runs（单飞锁 via partial unique index）---
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS watch_sync_runs (
          id              INTEGER PRIMARY KEY,
          provider        TEXT NOT NULL,
          started_at      INTEGER NOT NULL,
          completed_at    INTEGER,
          status          TEXT NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running', 'done', 'failed', 'aborted')),
          items_fetched   INTEGER NOT NULL DEFAULT 0,
          items_inserted  INTEGER NOT NULL DEFAULT 0,
          items_updated   INTEGER NOT NULL DEFAULT 0,
          items_skipped   INTEGER NOT NULL DEFAULT 0,
          error           TEXT
        );
        CREATE UNIQUE INDEX IF NOT EXISTS uniq_watch_sync_running
          ON watch_sync_runs(provider) WHERE status = 'running';
        """
    )

    # --- dedup_weights + dedup_weights_meta ---
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS dedup_weights (
          key         TEXT PRIMARY KEY,
          weight      REAL NOT NULL,
          updated_at  INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dedup_weights_meta (
          id            INTEGER PRIMARY KEY CHECK (id = 1),
          current_hash  TEXT NOT NULL,
          updated_at    INTEGER NOT NULL
        );
        """
    )

    # Seed defaults — partial-state safe via INSERT OR IGNORE per row
    # (B3 fix: COUNT==0 short-circuit would miss "user changed N weights + we added
    # a new default in code" case; INSERT OR IGNORE backfills only missing keys
    # without touching user-edited rows).
    now = int(time.time())
    seeded = 0
    for k, v in DEFAULT_DEDUP_WEIGHTS.items():
        cur = conn.execute(
            "INSERT OR IGNORE INTO dedup_weights(key, weight, updated_at) VALUES (?, ?, ?)",
            (k, float(v), now),
        )
        seeded += cur.rowcount or 0
    summary["weights_seeded"] = seeded

    # Unconditionally rebuild dedup_weights_meta from current DB state.
    # Reasons:
    #   - If meta row is missing (partial-state recovery), this restores it.
    #   - If user edited weights, hash must reflect their values, not defaults.
    #   - Reading from DB (REAL → float) ensures hash matches what update_weights()
    #     produces at runtime (no int-vs-float drift).
    actual_weights = _read_weights_from_db(conn)
    current_hash = _canonical_weights_hash(actual_weights)
    meta_row = conn.execute(
        "SELECT current_hash FROM dedup_weights_meta WHERE id=1"
    ).fetchone()
    if meta_row is None or meta_row[0] != current_hash:
        conn.execute(
            "INSERT OR REPLACE INTO dedup_weights_meta(id, current_hash, updated_at) "
            "VALUES (1, ?, ?)",
            (current_hash, now),
        )
        summary["weights_meta_refreshed"] = 1

    conn.commit()
    return summary
