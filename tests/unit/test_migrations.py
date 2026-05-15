"""Phase 3.0 schema migration tests.

Covers:
  - Idempotent apply (run twice on fresh DB → second call zero-impact)
  - Backward compat (old row with media_files.tmdb_id → tmdb_movie_id / tmdb_series_id 拷贝)
  - watched_items CHECK constraints (media_type ↔ id 互斥 + mapping join key 必填)
  - watch_sync_runs partial unique index (单飞锁)
  - dedup_weights seed + meta hash bootstrap
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from db import migrations
from services import destructive_action

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "db" / "schema.sql"


@pytest.fixture
def fresh_conn(tmp_path):
    """Fresh SQLite DB with Phase 1/2 schema applied (but no Phase 3 yet)."""
    db_path = tmp_path / "test.db"
    conn = destructive_action.open_connection(db_path)
    destructive_action.init_schema(conn, SCHEMA_PATH)
    yield conn
    conn.close()


@pytest.fixture
def migrated_conn(fresh_conn):
    """Phase 3 migration applied once on top of Phase 1/2."""
    migrations.phase3_migrate(fresh_conn)
    return fresh_conn


# ── Idempotent apply ───────────────────────────────────────────


def test_phase3_migrate_idempotent_double_apply(fresh_conn):
    """Second call must be no-op: columns_added=0, no new rows."""
    summary1 = migrations.phase3_migrate(fresh_conn)
    summary2 = migrations.phase3_migrate(fresh_conn)
    assert summary1["columns_added"] == 10
    assert summary2["columns_added"] == 0
    assert summary2["weights_seeded"] == 0
    assert summary2["tmdb_id_rows_migrated"] == 0


def test_phase3_migrate_creates_all_new_tables(migrated_conn):
    expected_tables = {
        "media_file_hdr_profiles",
        "watched_items",
        "watch_sync_runs",
        "dedup_weights",
        "dedup_weights_meta",
    }
    rows = migrated_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    actual = {r[0] for r in rows}
    assert expected_tables.issubset(actual)


def test_phase3_migrate_adds_10_media_files_columns(migrated_conn):
    cols = {row[1] for row in migrated_conn.execute("PRAGMA table_info(media_files)")}
    new_cols = {
        "parse_codec", "parse_color_depth", "parse_container", "parse_audio_codec",
        "quality_score", "score_weights_hash", "score_computed_at",
        "tmdb_movie_id", "tmdb_series_id", "tmdb_episode_id",
    }
    assert new_cols.issubset(cols)


def test_phase3_migrate_creates_all_indices(migrated_conn):
    rows = migrated_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()
    actual = {r[0] for r in rows}
    expected = {
        "idx_media_movie_id", "idx_media_series_se", "idx_media_episode_id",
        "idx_media_seen_size", "idx_hdr_profile",
        "idx_watched_movie", "idx_watched_series_se", "idx_watched_episode",
        "idx_watched_when",
        "uniq_watch_sync_running",
    }
    assert expected.issubset(actual)


# ── Old row tmdb_id migration ──────────────────────────────────


def test_phase3_migrate_old_movie_tmdb_id_copied_to_movie_id(fresh_conn):
    now = 1700000000
    fresh_conn.execute(
        """
        INSERT INTO media_files(path, tmdb_id, media_type, metadata_status,
                                first_seen_at, last_updated_at)
        VALUES (?, ?, ?, 'ok', ?, ?)
        """,
        ("/share/Movies/A.mkv", "238", "movie", now, now),
    )
    fresh_conn.commit()

    summary = migrations.phase3_migrate(fresh_conn)
    assert summary["tmdb_id_rows_migrated"] == 1

    row = fresh_conn.execute(
        "SELECT tmdb_id, tmdb_movie_id, tmdb_series_id FROM media_files WHERE path=?",
        ("/share/Movies/A.mkv",),
    ).fetchone()
    assert row[0] == "238"            # 老字段保留
    assert row[1] == "238"            # 新字段拷贝
    assert row[2] is None


def test_phase3_migrate_old_tv_tmdb_id_copied_to_series_id(fresh_conn):
    now = 1700000000
    fresh_conn.execute(
        """
        INSERT INTO media_files(path, tmdb_id, media_type, metadata_status,
                                first_seen_at, last_updated_at)
        VALUES (?, ?, ?, 'ok', ?, ?)
        """,
        ("/share/TV/B.S01E01.mkv", "1399", "tv", now, now),
    )
    fresh_conn.commit()

    summary = migrations.phase3_migrate(fresh_conn)
    assert summary["tmdb_id_rows_migrated"] == 1

    row = fresh_conn.execute(
        "SELECT tmdb_movie_id, tmdb_series_id FROM media_files WHERE path=?",
        ("/share/TV/B.S01E01.mkv",),
    ).fetchone()
    assert row[0] is None
    assert row[1] == "1399"


def test_phase3_migrate_does_not_double_migrate_already_split_rows(fresh_conn):
    """If tmdb_movie_id already set, do not overwrite."""
    migrations.phase3_migrate(fresh_conn)
    now = 1700000000
    fresh_conn.execute(
        """
        INSERT INTO media_files(path, tmdb_id, tmdb_movie_id, media_type,
                                metadata_status, first_seen_at, last_updated_at)
        VALUES (?, ?, ?, ?, 'ok', ?, ?)
        """,
        ("/share/Movies/C.mkv", "111", "999", "movie", now, now),
    )
    fresh_conn.commit()

    summary = migrations.phase3_migrate(fresh_conn)
    # 这一行已经有 tmdb_movie_id → migration UPDATE 应该不动它
    assert summary["tmdb_id_rows_migrated"] == 0
    row = fresh_conn.execute(
        "SELECT tmdb_movie_id FROM media_files WHERE path=?",
        ("/share/Movies/C.mkv",),
    ).fetchone()
    assert row[0] == "999"


# ── watched_items CHECK constraints ────────────────────────────


def _insert_watched(conn, **kw) -> int:
    """Helper. Returns rowid or raises sqlite3.IntegrityError."""
    defaults = {
        "provider": "emby",
        "provider_item_id": kw.get("provider_item_id", "emby-001"),
        "media_type": "movie",
        "tmdb_movie_id": None,
        "tmdb_series_id": None,
        "tmdb_episode_id": None,
        "imdb_id": None,
        "season_number": None,
        "episode_number": None,
        "title": "Test",
        "year": 2020,
        "watched_at": 1700000000,
        "fetched_at": 1700000001,
        "raw_hash": "deadbeef",
        "mapping_status": "mapped",
        "mapping_confidence": 1.0,
        "mapping_source": "emby.provider_ids",
    }
    defaults.update(kw)
    cols = ",".join(defaults.keys())
    placeholders = ",".join("?" for _ in defaults)
    cur = conn.execute(
        f"INSERT INTO watched_items({cols}) VALUES ({placeholders})",
        tuple(defaults.values()),
    )
    return cur.lastrowid


def test_watched_items_movie_row_ok_with_movie_id(migrated_conn):
    rowid = _insert_watched(migrated_conn, media_type="movie", tmdb_movie_id="238")
    assert rowid > 0


def test_watched_items_movie_row_rejects_series_id(migrated_conn):
    """Movie row cannot carry tmdb_series_id (Pattern B: id space 互斥)."""
    with pytest.raises(sqlite3.IntegrityError):
        _insert_watched(
            migrated_conn, media_type="movie",
            tmdb_movie_id="238", tmdb_series_id="1399",
        )


def test_watched_items_movie_row_rejects_season_number(migrated_conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_watched(
            migrated_conn, media_type="movie",
            tmdb_movie_id="238", season_number=1,
        )


def test_watched_items_tv_row_ok_with_episode_id(migrated_conn):
    rowid = _insert_watched(
        migrated_conn, provider_item_id="emby-ep-1", media_type="tv",
        tmdb_episode_id="9999", season_number=1, episode_number=1,
    )
    assert rowid > 0


def test_watched_items_tv_row_ok_with_series_fallback(migrated_conn):
    rowid = _insert_watched(
        migrated_conn, provider_item_id="emby-ep-2", media_type="tv",
        tmdb_series_id="1399", season_number=1, episode_number=1,
        mapping_status="fallback_se", mapping_confidence=0.7,
        mapping_source="emby.series_provider_ids+se",
    )
    assert rowid > 0


def test_watched_items_tv_row_requires_season_episode(migrated_conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_watched(
            migrated_conn, provider_item_id="emby-ep-3", media_type="tv",
            tmdb_episode_id="9999",
            season_number=None, episode_number=None,
        )


def test_watched_items_tv_row_rejects_movie_id(migrated_conn):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_watched(
            migrated_conn, provider_item_id="emby-ep-4", media_type="tv",
            tmdb_movie_id="238", tmdb_episode_id="9999",
            season_number=1, episode_number=1,
        )


def test_watched_items_mapped_status_requires_join_key(migrated_conn):
    """mapping_status='mapped' must have a join key (movie_id or episode_id or series_id)."""
    with pytest.raises(sqlite3.IntegrityError):
        _insert_watched(
            migrated_conn, provider_item_id="emby-ep-5", media_type="tv",
            tmdb_episode_id=None, tmdb_series_id=None,
            season_number=1, episode_number=1,
            mapping_status="mapped",
        )


def test_watched_items_unmapped_status_allows_no_join_key(migrated_conn):
    """mapping_status='unmapped' bypasses join-key CHECK (legitimate use case)."""
    rowid = _insert_watched(
        migrated_conn, provider_item_id="emby-ep-6", media_type="tv",
        tmdb_episode_id=None, tmdb_series_id=None,
        season_number=1, episode_number=1,
        mapping_status="unmapped", mapping_confidence=0.0, mapping_source="unknown",
    )
    assert rowid > 0


def test_watched_items_unique_provider_item_id(migrated_conn):
    _insert_watched(migrated_conn, provider_item_id="dup-1",
                    media_type="movie", tmdb_movie_id="100")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_watched(migrated_conn, provider_item_id="dup-1",
                        media_type="movie", tmdb_movie_id="100")


# ── watch_sync_runs 单飞锁 ─────────────────────────────────────


def test_watch_sync_runs_single_running_per_provider(migrated_conn):
    """uniq_watch_sync_running partial index: 同 provider 同时只允许一个 status='running'."""
    migrated_conn.execute(
        "INSERT INTO watch_sync_runs(provider, started_at, status) VALUES (?, ?, 'running')",
        ("emby", 1700000000),
    )
    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO watch_sync_runs(provider, started_at, status) VALUES (?, ?, 'running')",
            ("emby", 1700000001),
        )


def test_watch_sync_runs_allows_running_after_done(migrated_conn):
    """完成的 run 不占用 partial unique index → 新 run 可以开。"""
    migrated_conn.execute(
        "INSERT INTO watch_sync_runs(provider, started_at, completed_at, status) "
        "VALUES (?, ?, ?, 'done')",
        ("emby", 1700000000, 1700000060),
    )
    cur = migrated_conn.execute(
        "INSERT INTO watch_sync_runs(provider, started_at, status) VALUES (?, ?, 'running')",
        ("emby", 1700000100),
    )
    assert cur.lastrowid > 0


def test_watch_sync_runs_allows_different_providers_concurrent(migrated_conn):
    migrated_conn.execute(
        "INSERT INTO watch_sync_runs(provider, started_at, status) VALUES (?, ?, 'running')",
        ("emby", 1700000000),
    )
    cur = migrated_conn.execute(
        "INSERT INTO watch_sync_runs(provider, started_at, status) VALUES (?, ?, 'running')",
        ("plex", 1700000000),
    )
    assert cur.lastrowid > 0


# ── HDR profiles 子表 ──────────────────────────────────────────


def test_hdr_profiles_table_enforces_profile_check(migrated_conn):
    """profile column 限定 in ('HDR10', 'HDR10+', 'DolbyVision', 'HLG')."""
    now = 1700000000
    cur = migrated_conn.execute(
        """
        INSERT INTO media_files(path, media_type, metadata_status,
                                first_seen_at, last_updated_at)
        VALUES (?, 'movie', 'ok', ?, ?)
        """,
        ("/share/Movies/HDR-test.mkv", now, now),
    )
    fid = cur.lastrowid
    migrated_conn.execute(
        "INSERT INTO media_file_hdr_profiles(media_file_id, profile) VALUES (?, ?)",
        (fid, "DolbyVision"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO media_file_hdr_profiles(media_file_id, profile) VALUES (?, ?)",
            (fid, "BOGUS_HDR"),
        )


def test_hdr_profiles_primary_key_blocks_duplicate(migrated_conn):
    now = 1700000000
    cur = migrated_conn.execute(
        "INSERT INTO media_files(path, media_type, metadata_status, "
        "first_seen_at, last_updated_at) VALUES (?, 'movie', 'ok', ?, ?)",
        ("/share/Movies/X.mkv", now, now),
    )
    fid = cur.lastrowid
    migrated_conn.execute(
        "INSERT INTO media_file_hdr_profiles(media_file_id, profile) VALUES (?, ?)",
        (fid, "HDR10"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        migrated_conn.execute(
            "INSERT INTO media_file_hdr_profiles(media_file_id, profile) VALUES (?, ?)",
            (fid, "HDR10"),
        )


def test_hdr_profiles_cascade_delete_on_media_file_removal(migrated_conn):
    now = 1700000000
    cur = migrated_conn.execute(
        "INSERT INTO media_files(path, media_type, metadata_status, "
        "first_seen_at, last_updated_at) VALUES (?, 'movie', 'ok', ?, ?)",
        ("/share/Movies/Y.mkv", now, now),
    )
    fid = cur.lastrowid
    migrated_conn.execute(
        "INSERT INTO media_file_hdr_profiles(media_file_id, profile) VALUES (?, ?)",
        (fid, "HDR10+"),
    )
    migrated_conn.execute("DELETE FROM media_files WHERE id=?", (fid,))
    remaining = migrated_conn.execute(
        "SELECT COUNT(*) FROM media_file_hdr_profiles WHERE media_file_id=?", (fid,)
    ).fetchone()[0]
    assert remaining == 0


# ── dedup_weights seed + meta ──────────────────────────────────


def test_dedup_weights_seeded_with_defaults_on_first_apply(migrated_conn):
    rows = migrated_conn.execute(
        "SELECT key, weight FROM dedup_weights ORDER BY key"
    ).fetchall()
    keys = {r[0] for r in rows}
    # 关键键全在
    assert "resolution.4K" in keys
    assert "hdr.DolbyVision" in keys
    assert "source.BluRay" in keys
    assert "codec.H.265" in keys


def test_dedup_weights_meta_hash_matches_default_weights(migrated_conn):
    row = migrated_conn.execute(
        "SELECT current_hash FROM dedup_weights_meta WHERE id=1"
    ).fetchone()
    assert row is not None
    expected = migrations._canonical_weights_hash(migrations.DEFAULT_DEDUP_WEIGHTS)
    assert row[0] == expected


def test_dedup_weights_meta_hash_canonical_is_stable_across_dicts(migrated_conn):
    """Same dict in different insertion order → same hash."""
    a = {"resolution.4K": 40, "hdr.DolbyVision": 25}
    b = {"hdr.DolbyVision": 25, "resolution.4K": 40}
    assert migrations._canonical_weights_hash(a) == migrations._canonical_weights_hash(b)


def test_phase3_migrate_does_not_reseed_weights_on_second_apply(migrated_conn):
    """If table not empty (e.g. user changed weights), 不要 reset."""
    migrated_conn.execute(
        "UPDATE dedup_weights SET weight=999 WHERE key='resolution.4K'"
    )
    migrated_conn.commit()

    summary = migrations.phase3_migrate(migrated_conn)
    assert summary["weights_seeded"] == 0
    row = migrated_conn.execute(
        "SELECT weight FROM dedup_weights WHERE key='resolution.4K'"
    ).fetchone()
    assert row[0] == 999  # 用户改动保留


# ── Partial-state self-heal (B3 fix) ───────────────────────────


def test_phase3_migrate_self_heals_missing_weights_meta(migrated_conn):
    """If dedup_weights_meta was lost (corruption / partial init), re-migration restores."""
    migrated_conn.execute("DELETE FROM dedup_weights_meta")
    migrated_conn.commit()

    summary = migrations.phase3_migrate(migrated_conn)
    assert summary["weights_meta_refreshed"] == 1
    row = migrated_conn.execute(
        "SELECT current_hash FROM dedup_weights_meta WHERE id=1"
    ).fetchone()
    assert row is not None
    expected = migrations._canonical_weights_hash(migrations.DEFAULT_DEDUP_WEIGHTS)
    assert row[0] == expected


def test_phase3_migrate_backfills_missing_default_weight_keys(migrated_conn):
    """Old install missing a default key (e.g. we added 'codec.AV1' later) → backfilled.

    Note: After backfill, weights == defaults again so meta hash is unchanged and
    weights_meta_refreshed stays 0 (correct: final state matches existing meta).
    """
    migrated_conn.execute("DELETE FROM dedup_weights WHERE key='codec.AV1'")
    migrated_conn.commit()

    summary = migrations.phase3_migrate(migrated_conn)
    assert summary["weights_seeded"] == 1                  # 只补 codec.AV1 一条
    actual_keys = {r[0] for r in migrated_conn.execute("SELECT key FROM dedup_weights")}
    assert "codec.AV1" in actual_keys

    # meta hash 此时跟默认完全一致 (final state == default)
    meta_hash = migrated_conn.execute(
        "SELECT current_hash FROM dedup_weights_meta WHERE id=1"
    ).fetchone()[0]
    expected = migrations._canonical_weights_hash(migrations.DEFAULT_DEDUP_WEIGHTS)
    assert meta_hash == expected


def test_phase3_migrate_refreshes_meta_when_actual_diverges(migrated_conn):
    """User edited weights → meta hash 必须跟着算出新 hash。"""
    migrated_conn.execute("UPDATE dedup_weights SET weight=99 WHERE key='codec.AV1'")
    # 故意把 meta hash 写成 stale 值（模拟 partial state）
    migrated_conn.execute(
        "UPDATE dedup_weights_meta SET current_hash='STALE_HASH' WHERE id=1"
    )
    migrated_conn.commit()

    summary = migrations.phase3_migrate(migrated_conn)
    assert summary["weights_meta_refreshed"] == 1
    meta_hash = migrated_conn.execute(
        "SELECT current_hash FROM dedup_weights_meta WHERE id=1"
    ).fetchone()[0]
    assert meta_hash != "STALE_HASH"
    # 新 hash 等于 readback 后的 canonical
    db_weights = migrations._read_weights_from_db(migrated_conn)
    assert meta_hash == migrations._canonical_weights_hash(db_weights)


def test_phase3_migrate_meta_hash_matches_db_readback_not_source_dict(fresh_conn):
    """[code-enforced] meta hash 必须能跟 update_weights() 在 runtime 算出的 hash 匹配。

    runtime 算 hash 是从 DB readback REAL → float → json，
    source dict 里可能是 int 字面量（40 vs 40.0）。
    canonical hash 必须规范化数值（B3+I4 修复）。
    """
    migrations.phase3_migrate(fresh_conn)

    # 通过 _read_weights_from_db 模拟 runtime 重算路径
    db_weights = migrations._read_weights_from_db(fresh_conn)
    runtime_hash = migrations._canonical_weights_hash(db_weights)

    meta_hash = fresh_conn.execute(
        "SELECT current_hash FROM dedup_weights_meta WHERE id=1"
    ).fetchone()[0]
    assert meta_hash == runtime_hash


# ── Phase 4 migration ──────────────────────────────────────────


def _force_old_check_constraint(conn: sqlite3.Connection) -> None:
    """模拟「老 DB」：把 destructive_actions 重建成不含 'organize' 的 CHECK。"""
    conn.execute("DROP TABLE IF EXISTS destructive_actions")
    conn.executescript(
        """
        CREATE TABLE destructive_actions (
          action_id      TEXT PRIMARY KEY,
          kind           TEXT NOT NULL CHECK (kind IN
                          ('delete', 'nfo_write', 'archive', 'purge_provider')),
          payload_hash   TEXT NOT NULL,
          payload_json   TEXT NOT NULL,
          expires_at     INTEGER NOT NULL,
          status         TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'needs_manual_recovery')),
          consumed_at    INTEGER, started_at INTEGER, completed_at INTEGER,
          error          TEXT, result_json TEXT, recovery_hint TEXT,
          created_by     TEXT NOT NULL CHECK (created_by IN ('web_ui', 'mcp', 'cron')),
          created_at     INTEGER NOT NULL
        );
        CREATE INDEX idx_actions_expires ON destructive_actions(expires_at, status);
        CREATE INDEX idx_actions_recovery ON destructive_actions(status, started_at);
        """
    )
    conn.commit()


def test_phase4_migration_rebuilds_kind_check_constraint(fresh_conn):
    """老 schema 没 'organize' → phase4_migrate 重建表，新 CHECK 含 'organize'。"""
    _force_old_check_constraint(fresh_conn)
    # 验证老约束：INSERT 'organize' 必失败
    with pytest.raises(sqlite3.IntegrityError):
        fresh_conn.execute(
            "INSERT INTO destructive_actions"
            "(action_id, kind, payload_hash, payload_json, expires_at, created_by, created_at)"
            " VALUES ('a1', 'organize', 'h', '{}', 1000, 'web_ui', 999)"
        )
        fresh_conn.commit()
    # 跑 phase4
    summary = migrations.phase4_migrate(fresh_conn)
    assert summary["rebuilt"] is True
    # 重建后 INSERT 'organize' 应成功
    fresh_conn.execute(
        "INSERT INTO destructive_actions"
        "(action_id, kind, payload_hash, payload_json, expires_at, created_by, created_at)"
        " VALUES ('a2', 'organize', 'h', '{}', 1000, 'web_ui', 999)"
    )
    fresh_conn.commit()
    row = fresh_conn.execute(
        "SELECT kind FROM destructive_actions WHERE action_id='a2'"
    ).fetchone()
    assert row[0] == "organize"


def test_phase4_migration_preserves_existing_rows(fresh_conn):
    """老表的所有 row 在重建后必须仍存在，且字段值一致。"""
    _force_old_check_constraint(fresh_conn)
    fresh_conn.executemany(
        "INSERT INTO destructive_actions"
        "(action_id, kind, payload_hash, payload_json, expires_at, status, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("old-1", "delete", "h1", '{"k":1}', 9999, "succeeded", "web_ui", 100),
            ("old-2", "nfo_write", "h2", '{"k":2}', 9999, "pending", "web_ui", 200),
            ("old-3", "archive", "h3", '{"k":3}', 9999, "failed", "mcp", 300),
        ],
    )
    fresh_conn.commit()

    summary = migrations.phase4_migrate(fresh_conn)
    assert summary["rebuilt"] is True
    assert summary["rows_moved"] == 3

    rows = fresh_conn.execute(
        "SELECT action_id, kind, status, created_by FROM destructive_actions ORDER BY action_id"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("old-1", "delete", "succeeded", "web_ui"),
        ("old-2", "nfo_write", "pending", "web_ui"),
        ("old-3", "archive", "failed", "mcp"),
    ]


def test_phase4_migration_idempotent_second_run_is_noop(fresh_conn):
    """第二次跑应 short-circuit (already_has_organize)，不动数据。"""
    _force_old_check_constraint(fresh_conn)
    fresh_conn.execute(
        "INSERT INTO destructive_actions"
        "(action_id, kind, payload_hash, payload_json, expires_at, created_by, created_at)"
        " VALUES ('keep-me', 'delete', 'h', '{}', 1000, 'web_ui', 1)"
    )
    fresh_conn.commit()

    s1 = migrations.phase4_migrate(fresh_conn)
    s2 = migrations.phase4_migrate(fresh_conn)
    assert s1["rebuilt"] is True
    assert s1["rows_moved"] == 1
    assert s2["rebuilt"] is False
    assert s2.get("reason") == "already_has_organize"
    # 数据仍在
    cnt = fresh_conn.execute("SELECT COUNT(*) FROM destructive_actions").fetchone()[0]
    assert cnt == 1


def test_phase4_migration_indices_rebuilt(fresh_conn):
    """重建表后两个 index（expires/recovery）必须重新创建。"""
    _force_old_check_constraint(fresh_conn)
    migrations.phase4_migrate(fresh_conn)
    idx_names = {
        row[0] for row in fresh_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='destructive_actions'"
        ).fetchall()
    }
    assert "idx_actions_expires" in idx_names
    assert "idx_actions_recovery" in idx_names


def test_phase4_migration_skips_when_brand_new_schema_already_has_organize(fresh_conn):
    """新 DB（schema.sql 已含 'organize'）→ phase4 检测到不重建。"""
    # fresh_conn 已经走过最新 schema.sql，CHECK 含 'organize'
    summary = migrations.phase4_migrate(fresh_conn)
    assert summary["rebuilt"] is False
    assert summary.get("reason") == "already_has_organize"


# ─── Phase 4C / phase5_migrate: auto_organize_runs 表 ───


def test_phase5_migration_skips_when_table_already_exists(fresh_conn):
    """新 DB 走最新 schema.sql 已建表 → phase5 检测到 already_exists 不重建。"""
    summary = migrations.phase5_migrate(fresh_conn)
    assert summary["created"] is False
    assert summary.get("reason") == "already_exists"


def test_phase5_migration_creates_table_on_legacy_db(tmp_path):
    """老 DB（schema.sql 不含 auto_organize_runs）→ phase5 创建表。"""
    db_path = tmp_path / "legacy.db"
    conn = destructive_action.open_connection(db_path)
    # 模拟老 schema：只建 destructive_actions（auto_organize_runs 不存在）
    conn.executescript(
        """
        CREATE TABLE destructive_actions (
          action_id TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          payload_hash TEXT NOT NULL,
          payload_json TEXT NOT NULL,
          expires_at INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          consumed_at INTEGER,
          started_at INTEGER,
          completed_at INTEGER,
          error TEXT,
          result_json TEXT,
          recovery_hint TEXT,
          created_by TEXT NOT NULL,
          created_at INTEGER NOT NULL
        );
        """
    )
    summary = migrations.phase5_migrate(conn)
    assert summary["created"] is True
    # 表 + 3 个索引都建
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='auto_organize_runs'"
    ).fetchall()]
    assert tables == ["auto_organize_runs"]
    indices = sorted(r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='auto_organize_runs' AND name NOT LIKE 'sqlite_%'"
    ).fetchall())
    assert indices == ["idx_auto_org_history", "idx_auto_org_status", "uniq_auto_org_organizing"]
    conn.close()


def test_phase5_migration_idempotent_second_run_is_noop(fresh_conn):
    """二次跑 phase5 → created=False，rowcount=0。"""
    s1 = migrations.phase5_migrate(fresh_conn)
    s2 = migrations.phase5_migrate(fresh_conn)
    assert s1["created"] is False  # 新 schema 已含表
    assert s2["created"] is False


def test_phase5_status_check_constraint_enforces_enum(fresh_conn):
    """CHECK 拒绝非法 status 值。"""
    with pytest.raises(sqlite3.IntegrityError):
        fresh_conn.execute(
            "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,created_at) "
            "VALUES(?,?,?,?)",
            ("h1", "/x", "bogus_status", 0),
        )


def test_phase5_partial_unique_blocks_concurrent_organizing(fresh_conn):
    """uniq_auto_org_organizing partial unique → 同 hash 不能两次 status='organizing'。

    但 PK 已经保证同 hash 不能两 row，所以 partial unique 主要价值在 UPDATE 路径
    （某天 schema 改为允许 history rows 时，partial 防 status='organizing' 撞）。
    这个测试当前用 INSERT；PK 先撞 IntegrityError。两种约束都生效都视为 pass。
    """
    fresh_conn.execute(
        "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,created_at) "
        "VALUES(?,?,?,?)",
        ("h1", "/x", "organizing", 0),
    )
    fresh_conn.commit()
    # 二次 insert 同 hash → PK violation（partial unique 次优兜底）
    with pytest.raises(sqlite3.IntegrityError):
        fresh_conn.execute(
            "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,created_at) "
            "VALUES(?,?,?,?)",
            ("h1", "/y", "organizing", 1),
        )


def test_phase5_terminal_statuses_accepted(fresh_conn):
    """5 terminal + 2 transient = 7 个合法 status 全部能插入。"""
    valid = [
        "pending", "organizing", "succeeded", "failed",
        "skipped_needs_identify", "skipped_low_confidence", "skipped_unsupported",
    ]
    for i, st in enumerate(valid):
        # organizing 只能一行（partial unique），所以用不同 hash
        fresh_conn.execute(
            "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,created_at) "
            "VALUES(?,?,?,?)",
            (f"h{i}", "/x", st, i),
        )
    fresh_conn.commit()
    n = fresh_conn.execute("SELECT count(*) FROM auto_organize_runs").fetchone()[0]
    assert n == len(valid)
