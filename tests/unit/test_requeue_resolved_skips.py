"""Phase 3 防复发：qbit_auto.requeue_resolved_skips 单测。

覆盖：把「文件现已识别」的 skipped_needs_identify row 重置 pending，让 cron 重新 dispatch。
- 全 ok → requeue
- 无 media_files（.iso / BDMV）→ 不动
- 混入未识别 / 低置信 / 不支持类型 → 不动
- attempts 达上限 → 不动（防 thrash）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from db import migrations
from services import destructive_action, qbit_auto


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "t.db"
    c = destructive_action.open_connection(db_path)
    destructive_action.init_schema(
        c, Path(__file__).resolve().parents[2] / "db" / "schema.sql"
    )
    migrations.phase3_migrate(c)
    migrations.phase4_migrate(c)
    migrations.phase5_migrate(c)
    yield c
    c.close()


def _seed_skip(conn, qbit_hash, content_path, attempts=1):
    conn.execute(
        "INSERT INTO auto_organize_runs (qbit_hash, content_path, status, attempts, created_at) "
        "VALUES (?, ?, 'skipped_needs_identify', ?, 0)",
        (qbit_hash, content_path, attempts),
    )
    conn.commit()


def _seed_media(conn, path, media_type="movie", status="ok", confidence=0.95):
    conn.execute(
        "INSERT INTO media_files (path, media_type, metadata_status, metadata_confidence, "
        "first_seen_at, last_updated_at) VALUES (?, ?, ?, ?, 0, 0)",
        (path, media_type, status, confidence),
    )
    conn.commit()


def _identity(p):
    """测试里 content_path 直接当 canonical（不做 alias→canonical）。"""
    return p


def _status(conn, qbit_hash):
    return conn.execute(
        "SELECT status FROM auto_organize_runs WHERE qbit_hash=?", (qbit_hash,)
    ).fetchone()["status"]


def test_all_ok_requeues_to_pending(conn):
    """content_path 下文件全部 ok 识别 → 重置 pending。"""
    _seed_skip(conn, "h1", "/d/Movie")
    _seed_media(conn, "/d/Movie/Movie.mkv", "movie", "ok", 0.95)
    out = qbit_auto.requeue_resolved_skips(conn, resolve_fn=_identity, threshold=0.85)
    assert out == ["h1"]
    assert _status(conn, "h1") == "pending"
    # action_id 被清空
    assert conn.execute(
        "SELECT action_id FROM auto_organize_runs WHERE qbit_hash='h1'"
    ).fetchone()["action_id"] is None


def test_multi_episode_all_ok_requeues(conn):
    """多集剧全 ok → requeue。"""
    _seed_skip(conn, "h2", "/d/Show")
    _seed_media(conn, "/d/Show/E01.mkv", "tv", "ok", 0.95)
    _seed_media(conn, "/d/Show/E02.mkv", "tv", "ok", 0.90)
    out = qbit_auto.requeue_resolved_skips(conn, resolve_fn=_identity, threshold=0.85)
    assert out == ["h2"]
    assert _status(conn, "h2") == "pending"


def test_no_media_files_stays_skipped(conn):
    """.iso / BDMV：content_path 下没扫到任何视频文件 → 不动。"""
    _seed_skip(conn, "h3", "/d/Black.Adam.iso")
    out = qbit_auto.requeue_resolved_skips(conn, resolve_fn=_identity, threshold=0.85)
    assert out == []
    assert _status(conn, "h3") == "skipped_needs_identify"


def test_partial_unidentified_stays_skipped(conn):
    """一集还没识别（media_type=None）→ live gate 会 needs_identify → 不 requeue。"""
    _seed_skip(conn, "h4", "/d/Show")
    _seed_media(conn, "/d/Show/E01.mkv", "tv", "ok", 0.95)
    _seed_media(conn, "/d/Show/E02.mkv", None, "needs_review", None)
    out = qbit_auto.requeue_resolved_skips(conn, resolve_fn=_identity, threshold=0.85)
    assert out == []
    assert _status(conn, "h4") == "skipped_needs_identify"


def test_low_confidence_stays_skipped(conn):
    """置信度低于门槛 → 不 requeue。"""
    _seed_skip(conn, "h5", "/d/M")
    _seed_media(conn, "/d/M/M.mkv", "movie", "ok", 0.50)
    out = qbit_auto.requeue_resolved_skips(conn, resolve_fn=_identity, threshold=0.85)
    assert out == []
    assert _status(conn, "h5") == "skipped_needs_identify"


def test_unsupported_media_type_stays_skipped(conn):
    """花絮 media_type='extra' 不在 SUPPORTED → 不 requeue。"""
    _seed_skip(conn, "h6", "/d/Extra")
    _seed_media(conn, "/d/Extra/x.mkv", "extra", "ok", 0.99)
    out = qbit_auto.requeue_resolved_skips(conn, resolve_fn=_identity, threshold=0.85)
    assert out == []
    assert _status(conn, "h6") == "skipped_needs_identify"


def test_respects_max_attempts(conn):
    """attempts 达上限 → 不再 requeue（防 thrash）。"""
    _seed_skip(conn, "h7", "/d/M", attempts=5)
    _seed_media(conn, "/d/M/M.mkv", "movie", "ok", 0.95)
    out = qbit_auto.requeue_resolved_skips(
        conn, resolve_fn=_identity, threshold=0.85, max_attempts=5
    )
    assert out == []
    assert _status(conn, "h7") == "skipped_needs_identify"


def test_resolve_fn_exception_skips_row(conn):
    """resolve_fn 抛错（SSH 挂等）→ 跳过该 row，不崩。"""
    _seed_skip(conn, "h8", "/d/M")
    _seed_media(conn, "/d/M/M.mkv", "movie", "ok", 0.95)

    def _boom(_):
        raise RuntimeError("ssh down")

    out = qbit_auto.requeue_resolved_skips(conn, resolve_fn=_boom, threshold=0.85)
    assert out == []
    assert _status(conn, "h8") == "skipped_needs_identify"


def test_only_touches_skipped_needs_identify(conn):
    """其他状态（succeeded / skipped_unsupported）不被误碰。"""
    conn.execute(
        "INSERT INTO auto_organize_runs (qbit_hash, content_path, status, attempts, created_at) "
        "VALUES ('hs', '/d/Done', 'succeeded', 1, 0)"
    )
    conn.execute(
        "INSERT INTO auto_organize_runs (qbit_hash, content_path, status, attempts, created_at) "
        "VALUES ('hu', '/d/Unsup', 'skipped_unsupported', 1, 0)"
    )
    conn.commit()
    _seed_media(conn, "/d/Done/x.mkv", "movie", "ok", 0.95)
    _seed_media(conn, "/d/Unsup/x.mkv", "movie", "ok", 0.95)
    out = qbit_auto.requeue_resolved_skips(conn, resolve_fn=_identity, threshold=0.85)
    assert out == []
    assert _status(conn, "hs") == "succeeded"
    assert _status(conn, "hu") == "skipped_unsupported"
