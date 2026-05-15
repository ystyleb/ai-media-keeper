"""Phase 4C.1: services/qbit_auto helpers unit tests.

覆盖：
  - list_completed_torrents 过滤逻辑（progress / state / category / content_path / hash）
  - filter_unprocessed_hashes 跟 auto_organize_runs 状态联动
  - row lifecycle: claim_pending / mark_organizing / mark_terminal / mark_skipped
  - list_history / get_run
"""

from __future__ import annotations

import pytest
import sqlite3

from db import migrations
from services import destructive_action, qbit_auto


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "t.db"
    c = destructive_action.open_connection(db_path)
    destructive_action.init_schema(c, __import__("pathlib").Path(__file__).resolve().parents[2] / "db" / "schema.sql")
    migrations.phase3_migrate(c)
    migrations.phase4_migrate(c)
    migrations.phase5_migrate(c)
    yield c
    c.close()


def _t(**kw):
    """Helper: 构造合规 completed torrent dict（默认全字段都过滤通过）."""
    return {
        "hash": kw.get("hash", "h1"),
        "name": kw.get("name", "Movie.Name.mkv"),
        "category": kw.get("category", "Movies"),
        "state": kw.get("state", "seeding"),
        "progress": kw.get("progress", 1.0),
        "content_path": kw.get("content_path", "/d/Movie.Name.mkv"),
        "save_path": kw.get("save_path", "/d"),
    }


# ── list_completed_torrents ───


def test_list_completed_empty_whitelist_returns_empty():
    assert qbit_auto.list_completed_torrents([_t()], []) == []


def test_list_completed_whitelist_with_empty_strings_no_match():
    """whitelist 全 '' / '   ' → 等价空白名单。"""
    # category whitelist 含空字符串，但实际 torrent.category != ''
    assert qbit_auto.list_completed_torrents([_t(category="Movies")], ["", "  "]) == []


def test_list_completed_filters_progress_lt_1():
    assert qbit_auto.list_completed_torrents([_t(progress=0.99)], ["Movies"]) == []


def test_list_completed_filters_downloading_state():
    assert qbit_auto.list_completed_torrents(
        [_t(state="downloading")], ["Movies"]
    ) == []


def test_list_completed_filters_missing_files_state():
    """missingFiles / error 等错误状态不该被当成 completed。"""
    assert qbit_auto.list_completed_torrents(
        [_t(state="missingFiles")], ["Movies"]
    ) == []


def test_list_completed_filters_category_not_in_whitelist():
    assert qbit_auto.list_completed_torrents(
        [_t(category="Music")], ["Movies", "TV"]
    ) == []


def test_list_completed_accepts_empty_category_torrent_only_if_empty_in_whitelist():
    """qBit 默认 category='' 的种子：只有 whitelist 显式含 '' 才纳入。"""
    # whitelist 不含 '' → 排除
    assert qbit_auto.list_completed_torrents([_t(category="")], ["Movies"]) == []


def test_list_completed_filters_missing_content_path():
    assert qbit_auto.list_completed_torrents([_t(content_path="")], ["Movies"]) == []


def test_list_completed_filters_missing_hash():
    assert qbit_auto.list_completed_torrents(
        [_t(hash="")], ["Movies"]
    ) == []


def test_list_completed_happy_path_returns_full_dict():
    torrents = [_t()]
    out = qbit_auto.list_completed_torrents(torrents, ["Movies"])
    assert len(out) == 1
    # 保留原始 dict 全字段（caller 用 hash / content_path / name / category）
    assert out[0]["hash"] == "h1"
    assert out[0]["content_path"] == "/d/Movie.Name.mkv"
    assert out[0]["name"] == "Movie.Name.mkv"
    assert out[0]["category"] == "Movies"


def test_list_completed_multiple_states_all_accepted():
    """各种 seeding / uploading / pausedUP / completed state 都该当 completed。"""
    states = ["seeding", "uploading", "stalledUP", "pausedUP", "queuedUP",
              "forcedUP", "completed"]
    torrents = [_t(hash=f"h{i}", state=s) for i, s in enumerate(states)]
    out = qbit_auto.list_completed_torrents(torrents, ["Movies"])
    assert len(out) == len(states)


# ── filter_unprocessed_hashes ───


def _insert_row(conn, qbit_hash, status, content_path="/x"):
    import time
    conn.execute(
        "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,attempts,created_at) "
        "VALUES(?,?,?,0,?)",
        (qbit_hash, content_path, status, int(time.time())),
    )
    conn.commit()


def test_filter_unprocessed_empty_input(conn):
    assert qbit_auto.filter_unprocessed_hashes(conn, []) == set()


def test_filter_unprocessed_returns_all_when_no_rows(conn):
    """没 row 全部 unprocessed。"""
    out = qbit_auto.filter_unprocessed_hashes(conn, ["h1", "h2"])
    assert out == {"h1", "h2"}


def test_filter_unprocessed_excludes_terminal_statuses(conn):
    """succeeded / failed / skipped_* row 不再处理。"""
    _insert_row(conn, "h1", "succeeded")
    _insert_row(conn, "h2", "failed")
    _insert_row(conn, "h3", "skipped_low_confidence")
    out = qbit_auto.filter_unprocessed_hashes(conn, ["h1", "h2", "h3", "h4"])
    assert out == {"h4"}


def test_filter_unprocessed_excludes_organizing(conn):
    """organizing row 在跑，跳过。"""
    _insert_row(conn, "h1", "organizing")
    out = qbit_auto.filter_unprocessed_hashes(conn, ["h1", "h2"])
    assert out == {"h2"}


def test_filter_unprocessed_keeps_pending(conn):
    """pending row 继续处理（cron 重试场景）。"""
    _insert_row(conn, "h1", "pending")
    out = qbit_auto.filter_unprocessed_hashes(conn, ["h1", "h2"])
    assert out == {"h1", "h2"}


# ── claim_pending_run ───


def test_claim_pending_new_hash_inserts(conn):
    ok = qbit_auto.claim_pending_run(
        conn, "h1", category="Movies", torrent_name="Name", content_path="/x",
    )
    assert ok is True
    row = conn.execute("SELECT * FROM auto_organize_runs WHERE qbit_hash='h1'").fetchone()
    assert row["status"] == "pending"
    assert row["category"] == "Movies"
    assert row["torrent_name"] == "Name"
    assert row["content_path"] == "/x"
    assert row["attempts"] == 0


def test_claim_pending_duplicate_returns_false(conn):
    """已存在 row → INSERT OR IGNORE 不抛错但 rowcount=0。"""
    qbit_auto.claim_pending_run(conn, "h1", category="Movies",
                                 torrent_name="N", content_path="/x")
    ok = qbit_auto.claim_pending_run(conn, "h1", category="Movies",
                                       torrent_name="N", content_path="/x")
    assert ok is False
    # 原 row 不被覆盖
    row = conn.execute("SELECT count(*) as n FROM auto_organize_runs").fetchone()
    assert row["n"] == 1


# ── mark_organizing ───


def test_mark_organizing_pending_to_organizing(conn):
    qbit_auto.claim_pending_run(conn, "h1", category="Movies",
                                 torrent_name="N", content_path="/x")
    ok = qbit_auto.mark_organizing(conn, "h1", action_id="act-1")
    assert ok is True
    row = conn.execute("SELECT * FROM auto_organize_runs WHERE qbit_hash='h1'").fetchone()
    assert row["status"] == "organizing"
    assert row["action_id"] == "act-1"
    assert row["attempts"] == 1
    assert row["last_attempt_at"] is not None


def test_mark_organizing_terminal_rejected(conn):
    """terminal row 不能 → organizing（WHERE status='pending' 守门）。"""
    _insert_row(conn, "h1", "succeeded")
    ok = qbit_auto.mark_organizing(conn, "h1", action_id="act-1")
    assert ok is False


def test_mark_organizing_already_organizing_rejected(conn):
    """已 organizing → guarded UPDATE 不命中。"""
    _insert_row(conn, "h1", "organizing")
    ok = qbit_auto.mark_organizing(conn, "h1", action_id="act-x")
    assert ok is False


def test_mark_organizing_nonexistent_returns_false(conn):
    ok = qbit_auto.mark_organizing(conn, "nonexistent", action_id=None)
    assert ok is False


# ── mark_terminal ───


def test_mark_terminal_succeeded_writes_fields(conn):
    qbit_auto.claim_pending_run(conn, "h1", category="M",
                                 torrent_name="N", content_path="/x")
    qbit_auto.mark_organizing(conn, "h1", action_id="act-1")
    ok = qbit_auto.mark_terminal(
        conn, "h1", status="succeeded", action_id="act-1",
        files_succeeded=5, files_already_linked=1, files_failed=0,
    )
    assert ok is True
    row = conn.execute("SELECT * FROM auto_organize_runs WHERE qbit_hash='h1'").fetchone()
    assert row["status"] == "succeeded"
    assert row["files_succeeded"] == 5
    assert row["files_already_linked"] == 1
    assert row["files_failed"] == 0
    assert row["completed_at"] is not None
    # attempts 不在 mark_terminal 增（已在 mark_organizing 增过）
    assert row["attempts"] == 1


def test_mark_terminal_invalid_status_raises(conn):
    _insert_row(conn, "h1", "pending")
    with pytest.raises(ValueError, match="invalid terminal status"):
        qbit_auto.mark_terminal(conn, "h1", status="organizing")


def test_mark_terminal_rejects_pending_status(conn):
    """status='pending' / 'organizing' 不是 terminal，不能传进 mark_terminal。"""
    _insert_row(conn, "h1", "pending")
    with pytest.raises(ValueError):
        qbit_auto.mark_terminal(conn, "h1", status="pending")


def test_mark_terminal_nonexistent_returns_false(conn):
    ok = qbit_auto.mark_terminal(conn, "ghost", status="failed", error="x")
    assert ok is False


def test_mark_terminal_preserves_action_id_when_passed_none(conn):
    """action_id=None 不该清掉已有的 action_id（COALESCE 保护）。"""
    qbit_auto.claim_pending_run(conn, "h1", category="M",
                                 torrent_name="N", content_path="/x")
    qbit_auto.mark_organizing(conn, "h1", action_id="act-set")
    qbit_auto.mark_terminal(conn, "h1", status="failed", action_id=None,
                              error="something")
    row = conn.execute(
        "SELECT action_id FROM auto_organize_runs WHERE qbit_hash='h1'"
    ).fetchone()
    assert row["action_id"] == "act-set"


# ── mark_skipped_at_pending ───


def test_mark_skipped_at_pending_transitions(conn):
    qbit_auto.claim_pending_run(conn, "h1", category="M",
                                 torrent_name="N", content_path="/x")
    ok = qbit_auto.mark_skipped_at_pending(
        conn, "h1", status="skipped_low_confidence",
        error="some files below threshold",
    )
    assert ok is True
    row = conn.execute("SELECT * FROM auto_organize_runs WHERE qbit_hash='h1'").fetchone()
    assert row["status"] == "skipped_low_confidence"
    assert row["last_error"] == "some files below threshold"
    assert row["attempts"] == 1
    assert row["completed_at"] is not None


def test_mark_skipped_at_pending_only_when_status_pending(conn):
    """已 organizing / terminal row 不能 → skipped (WHERE status='pending' 守门)."""
    _insert_row(conn, "h1", "organizing")
    ok = qbit_auto.mark_skipped_at_pending(conn, "h1",
                                              status="skipped_low_confidence")
    assert ok is False


def test_mark_skipped_at_pending_rejects_non_skip_status(conn):
    _insert_row(conn, "h1", "pending")
    with pytest.raises(ValueError, match="must be skipped_"):
        qbit_auto.mark_skipped_at_pending(conn, "h1", status="failed")


# ── list_history + get_run ───


def test_list_history_orders_pending_organizing_first(conn):
    """pending / organizing 置顶，其余按 completed_at desc。"""
    import time
    base = int(time.time())
    _insert_row(conn, "h1", "succeeded")
    conn.execute("UPDATE auto_organize_runs SET completed_at=? WHERE qbit_hash='h1'",
                 (base - 1000,))
    _insert_row(conn, "h2", "pending")
    _insert_row(conn, "h3", "failed")
    conn.execute("UPDATE auto_organize_runs SET completed_at=? WHERE qbit_hash='h3'",
                 (base - 500,))
    _insert_row(conn, "h4", "organizing")
    conn.commit()

    rows = qbit_auto.list_history(conn, limit=10)
    statuses = [r["status"] for r in rows]
    # 前两个是 pending / organizing 任一顺序
    assert statuses[0] in {"pending", "organizing"}
    assert statuses[1] in {"pending", "organizing"}
    # 后面 failed (newer completed_at) 排 succeeded 前
    assert statuses[2] == "failed"
    assert statuses[3] == "succeeded"


def test_list_history_status_filter(conn):
    _insert_row(conn, "h1", "succeeded")
    _insert_row(conn, "h2", "failed")
    _insert_row(conn, "h3", "succeeded")
    rows = qbit_auto.list_history(conn, status_filter="succeeded")
    assert len(rows) == 2
    assert all(r["status"] == "succeeded" for r in rows)


def test_list_history_limit_offset(conn):
    for i in range(5):
        _insert_row(conn, f"h{i}", "succeeded")
    page1 = qbit_auto.list_history(conn, limit=2, offset=0)
    page2 = qbit_auto.list_history(conn, limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    assert {r["qbit_hash"] for r in page1} & {r["qbit_hash"] for r in page2} == set()


def test_get_run_existing_returns_dict(conn):
    qbit_auto.claim_pending_run(conn, "h1", category="M",
                                 torrent_name="N", content_path="/x")
    r = qbit_auto.get_run(conn, "h1")
    assert r is not None
    assert r["qbit_hash"] == "h1"
    assert r["status"] == "pending"


def test_get_run_nonexistent_returns_none(conn):
    assert qbit_auto.get_run(conn, "ghost") is None


# ─── 4C.2 evaluate_confidence_gate ───
# Mock metadata_cache.get_many_by_path 让单测不依赖真 cache row insertion.

from dataclasses import dataclass


@dataclass
class _CachedStub:
    """Minimal CachedMetadata stub — 只填 confidence gate 用到的字段。"""
    media_type: str | None
    metadata_confidence: float | None
    path: str = ""


def _patch_cache(monkeypatch, cache_map):
    """patch get_many_by_path 返指定 {path: (CachedStub | None, status)} map.

    None entry / 不在 map 里都 = needs_identify。
    """
    from services import metadata_cache as mc

    def fake_get_many(conn, paths):
        # 模拟真实 helper：不在 map 里的 path 不在返回字典里
        return {p: cache_map[p] for p in paths if p in cache_map}

    monkeypatch.setattr(mc, "get_many_by_path", fake_get_many)


def test_gate_empty_paths_returns_needs_identify(conn):
    out = qbit_auto.evaluate_confidence_gate(conn, [], threshold=0.85)
    assert out["status"] == "skipped_needs_identify"
    assert "no video files" in out["reason"]
    assert out["checked_count"] == 0


def test_gate_no_cache_row_returns_needs_identify(conn, monkeypatch):
    """get_many_by_path 没返某 path → needs_identify."""
    _patch_cache(monkeypatch, {})  # 全 miss
    out = qbit_auto.evaluate_confidence_gate(
        conn, ["/x.mkv", "/y.mkv"], threshold=0.85,
    )
    assert out["status"] == "skipped_needs_identify"
    assert out["checked_count"] == 2
    assert len(out["blockers"]) == 2
    assert all(b["media_type"] is None for b in out["blockers"])


def test_gate_cache_exists_but_media_type_none_returns_needs_identify(conn, monkeypatch):
    """cache row 存在但未识别 (media_type=None) → needs_identify."""
    _patch_cache(monkeypatch, {
        "/x.mkv": (_CachedStub(media_type=None, metadata_confidence=None), "hit"),
    })
    out = qbit_auto.evaluate_confidence_gate(conn, ["/x.mkv"], threshold=0.85)
    assert out["status"] == "skipped_needs_identify"
    assert "media_type is None" in out["blockers"][0]["reason"]


def test_gate_unsupported_media_type_returns_unsupported(conn, monkeypatch):
    """media_type='extra' / 'sample' / 'unknown' → unsupported (但优先级低于 needs_identify)."""
    _patch_cache(monkeypatch, {
        "/x.mkv": (_CachedStub(media_type="extra", metadata_confidence=0.99), "hit"),
    })
    out = qbit_auto.evaluate_confidence_gate(conn, ["/x.mkv"], threshold=0.85)
    assert out["status"] == "skipped_unsupported"
    assert "extra" in out["blockers"][0]["reason"]


def test_gate_confidence_below_threshold_returns_low_confidence(conn, monkeypatch):
    _patch_cache(monkeypatch, {
        "/x.mkv": (_CachedStub(media_type="movie", metadata_confidence=0.5), "hit"),
    })
    out = qbit_auto.evaluate_confidence_gate(conn, ["/x.mkv"], threshold=0.85)
    assert out["status"] == "skipped_low_confidence"
    assert "0.5" in out["blockers"][0]["reason"]
    assert "0.85" in out["blockers"][0]["reason"]


def test_gate_confidence_none_treated_as_zero(conn, monkeypatch):
    """metadata_confidence is None (LLM 没返置信度) → 视为 0 → low_confidence."""
    _patch_cache(monkeypatch, {
        "/x.mkv": (_CachedStub(media_type="movie", metadata_confidence=None), "hit"),
    })
    out = qbit_auto.evaluate_confidence_gate(conn, ["/x.mkv"], threshold=0.85)
    assert out["status"] == "skipped_low_confidence"


def test_gate_confidence_exactly_at_threshold_passes(conn, monkeypatch):
    """confidence == threshold → pass（>= 边界）."""
    _patch_cache(monkeypatch, {
        "/x.mkv": (_CachedStub(media_type="movie", metadata_confidence=0.85), "hit"),
    })
    out = qbit_auto.evaluate_confidence_gate(conn, ["/x.mkv"], threshold=0.85)
    assert out["status"] == "pass"
    assert out["blockers"] == []


def test_gate_all_pass_happy_path(conn, monkeypatch):
    _patch_cache(monkeypatch, {
        "/a.mkv": (_CachedStub(media_type="movie", metadata_confidence=0.9), "hit"),
        "/b.mkv": (_CachedStub(media_type="tv", metadata_confidence=0.95), "hit"),
    })
    out = qbit_auto.evaluate_confidence_gate(
        conn, ["/a.mkv", "/b.mkv"], threshold=0.85,
    )
    assert out["status"] == "pass"
    assert out["checked_count"] == 2
    assert "2 file(s) meet threshold" in out["reason"]


def test_gate_priority_needs_identify_beats_unsupported(conn, monkeypatch):
    """优先级：needs_identify > unsupported > low_confidence."""
    _patch_cache(monkeypatch, {
        "/a.mkv": (None, "miss"),  # needs_identify
        "/b.mkv": (_CachedStub(media_type="extra", metadata_confidence=0.99), "hit"),  # unsupported
    })
    out = qbit_auto.evaluate_confidence_gate(
        conn, ["/a.mkv", "/b.mkv"], threshold=0.85,
    )
    # needs_identify 胜出
    assert out["status"] == "skipped_needs_identify"


def test_gate_priority_unsupported_beats_low_confidence(conn, monkeypatch):
    _patch_cache(monkeypatch, {
        "/a.mkv": (_CachedStub(media_type="extra", metadata_confidence=0.99), "hit"),  # unsupported
        "/b.mkv": (_CachedStub(media_type="movie", metadata_confidence=0.5), "hit"),    # low_confidence
    })
    out = qbit_auto.evaluate_confidence_gate(
        conn, ["/a.mkv", "/b.mkv"], threshold=0.85,
    )
    # unsupported 胜出
    assert out["status"] == "skipped_unsupported"


def test_gate_mixed_pass_and_fail_returns_blockers_for_fail_only(conn, monkeypatch):
    """部分文件 pass、部分 low_confidence → 整体 fail，blockers 只列 fail 项."""
    _patch_cache(monkeypatch, {
        "/good.mkv": (_CachedStub(media_type="movie", metadata_confidence=0.9), "hit"),
        "/bad.mkv": (_CachedStub(media_type="movie", metadata_confidence=0.5), "hit"),
    })
    out = qbit_auto.evaluate_confidence_gate(
        conn, ["/good.mkv", "/bad.mkv"], threshold=0.85,
    )
    assert out["status"] == "skipped_low_confidence"
    assert len(out["blockers"]) == 1
    assert out["blockers"][0]["path"] == "/bad.mkv"


def test_gate_supported_media_types_constant():
    """加新 media_type 时（如 anime / docu）必须同步更新 SUPPORTED_MEDIA_TYPES."""
    assert qbit_auto.SUPPORTED_MEDIA_TYPES == {"movie", "tv"}


# ─── 4C.3 dispatch_one ───
# Mock list_video_paths_fn + build_and_start_organize_fn 让 service-level 单测
# 不依赖 SSH 或 organize_runner 状态.


def _torrent(**kw):
    return {
        "hash": kw.get("hash", "h1"),
        "name": kw.get("name", "Some.Movie.mkv"),
        "category": kw.get("category", "Movies"),
        "content_path": kw.get("content_path", "/d/Some.Movie.mkv"),
    }


def test_dispatch_one_missing_hash_returns_skipped(conn):
    out = qbit_auto.dispatch_one(
        conn, _torrent(hash=""),
        list_video_paths_fn=lambda p: [],
        confidence_threshold=0.85,
        build_and_start_organize_fn=lambda paths, h: {"action_id": None, "status": "error"},
    )
    assert out["action"] == "skipped"
    assert "missing hash" in out["reason"]


def test_dispatch_one_skips_already_terminal_row(conn):
    _insert_row(conn, "h1", "succeeded")
    out = qbit_auto.dispatch_one(
        conn, _torrent(),
        list_video_paths_fn=lambda p: [],
        confidence_threshold=0.85,
        build_and_start_organize_fn=lambda paths, h: {"action_id": "x", "status": "started"},
    )
    assert out["action"] == "skip_existing_run"
    assert out["current_status"] == "succeeded"


def test_dispatch_one_skips_already_organizing_row(conn):
    _insert_row(conn, "h1", "organizing")
    out = qbit_auto.dispatch_one(
        conn, _torrent(),
        list_video_paths_fn=lambda p: [],
        confidence_threshold=0.85,
        build_and_start_organize_fn=lambda paths, h: {"action_id": "x", "status": "started"},
    )
    assert out["action"] == "skip_existing_run"
    assert out["current_status"] == "organizing"


def test_dispatch_one_list_paths_raises_marks_skipped(conn):
    def boom(content_path):
        raise RuntimeError("ssh failed")

    out = qbit_auto.dispatch_one(
        conn, _torrent(),
        list_video_paths_fn=boom,
        confidence_threshold=0.85,
        build_and_start_organize_fn=lambda paths, h: {"action_id": "x", "status": "started"},
    )
    assert out["action"] == "skipped"
    assert out["status"] == "skipped_unsupported"
    # row 应该被 mark_skipped_at_pending 落库
    row = qbit_auto.get_run(conn, "h1")
    assert row["status"] == "skipped_unsupported"
    assert "list_paths_failed" in row["last_error"]


def test_dispatch_one_confidence_gate_fail_marks_skipped(conn, monkeypatch):
    _patch_cache(monkeypatch, {})  # 全 needs_identify

    out = qbit_auto.dispatch_one(
        conn, _torrent(),
        list_video_paths_fn=lambda p: ["/d/Some.Movie.mkv"],
        confidence_threshold=0.85,
        build_and_start_organize_fn=lambda paths, h: pytest.fail("不该被调"),
    )
    assert out["action"] == "skipped"
    assert out["status"] == "skipped_needs_identify"
    row = qbit_auto.get_run(conn, "h1")
    assert row["status"] == "skipped_needs_identify"


def test_dispatch_one_locked_keeps_pending(conn, monkeypatch):
    """build_and_start 返 locked → row 留 pending 让下周期重试.

    NOT mark_skipped — locked 是临时状态.
    """
    _patch_cache(monkeypatch, {
        "/d/Some.Movie.mkv": (_CachedStub(media_type="movie", metadata_confidence=0.9), "hit"),
    })
    out = qbit_auto.dispatch_one(
        conn, _torrent(),
        list_video_paths_fn=lambda p: ["/d/Some.Movie.mkv"],
        confidence_threshold=0.85,
        build_and_start_organize_fn=lambda paths, h: {
            "action_id": None, "status": "locked", "error": None,
        },
    )
    assert out["action"] == "locked"
    row = qbit_auto.get_run(conn, "h1")
    assert row["status"] == "pending"  # 留 pending 重试


def test_dispatch_one_error_marks_skipped(conn, monkeypatch):
    """build_and_start 返 error → mark_skipped_at_pending(skipped_unsupported)."""
    _patch_cache(monkeypatch, {
        "/d/Some.Movie.mkv": (_CachedStub(media_type="movie", metadata_confidence=0.9), "hit"),
    })
    out = qbit_auto.dispatch_one(
        conn, _torrent(),
        list_video_paths_fn=lambda p: ["/d/Some.Movie.mkv"],
        confidence_threshold=0.85,
        build_and_start_organize_fn=lambda paths, h: {
            "action_id": None, "status": "error", "error": "preview build failed: X",
        },
    )
    assert out["action"] == "errored"
    assert "preview build failed" in out["error"]
    row = qbit_auto.get_run(conn, "h1")
    assert row["status"] == "skipped_unsupported"


def test_dispatch_one_callback_raises_marks_skipped(conn, monkeypatch):
    """build_and_start 内部抛异常 → mark_skipped 不 propagate."""
    _patch_cache(monkeypatch, {
        "/d/Some.Movie.mkv": (_CachedStub(media_type="movie", metadata_confidence=0.9), "hit"),
    })

    def boom(paths, h):
        raise RuntimeError("kaboom")

    out = qbit_auto.dispatch_one(
        conn, _torrent(),
        list_video_paths_fn=lambda p: ["/d/Some.Movie.mkv"],
        confidence_threshold=0.85,
        build_and_start_organize_fn=boom,
    )
    assert out["action"] == "errored"
    assert "kaboom" in out["error"]
    row = qbit_auto.get_run(conn, "h1")
    assert row["status"] == "skipped_unsupported"


def test_dispatch_one_happy_path_started(conn, monkeypatch):
    _patch_cache(monkeypatch, {
        "/d/Some.Movie.mkv": (_CachedStub(media_type="movie", metadata_confidence=0.9), "hit"),
    })
    out = qbit_auto.dispatch_one(
        conn, _torrent(),
        list_video_paths_fn=lambda p: ["/d/Some.Movie.mkv"],
        confidence_threshold=0.85,
        build_and_start_organize_fn=lambda paths, h: {
            "action_id": "act-123", "status": "started", "error": None,
        },
    )
    assert out["action"] == "started"
    assert out["action_id"] == "act-123"
    row = qbit_auto.get_run(conn, "h1")
    assert row["status"] == "organizing"
    assert row["action_id"] == "act-123"
    assert row["attempts"] == 1


def test_dispatch_one_resumes_pending_row(conn, monkeypatch):
    """row 已存在 status='pending'（之前 locked 留下）→ 继续走 flow，不是 skip_existing."""
    _insert_row(conn, "h1", "pending")
    _patch_cache(monkeypatch, {
        "/d/Some.Movie.mkv": (_CachedStub(media_type="movie", metadata_confidence=0.9), "hit"),
    })
    out = qbit_auto.dispatch_one(
        conn, _torrent(),
        list_video_paths_fn=lambda p: ["/d/Some.Movie.mkv"],
        confidence_threshold=0.85,
        build_and_start_organize_fn=lambda paths, h: {
            "action_id": "act-retry", "status": "started", "error": None,
        },
    )
    assert out["action"] == "started"
    row = qbit_auto.get_run(conn, "h1")
    assert row["status"] == "organizing"


# ─── 4C.3 reconcile_organizing_rows ───


def _insert_organizing_with_action(conn, qbit_hash, action_id):
    import time
    conn.execute(
        "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,attempts,"
        "created_at,action_id) VALUES(?,?,?,?,?,?)",
        (qbit_hash, "/x", "organizing", 1, int(time.time()), action_id),
    )
    conn.commit()


def _insert_destructive_action(conn, action_id, status, *, result_json=None, error=None):
    import time
    conn.execute(
        "INSERT INTO destructive_actions(action_id,kind,payload_hash,payload_json,"
        "expires_at,status,created_by,created_at,result_json,error) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (action_id, "organize", "ph", "{}", int(time.time()) + 1800, status,
         "cron", int(time.time()), result_json, error),
    )
    conn.commit()


def test_reconcile_skips_running_actions(conn):
    """destructive_action 还 running → 不动 auto_organize_runs row."""
    _insert_organizing_with_action(conn, "h1", "act-1")
    _insert_destructive_action(conn, "act-1", "running")
    out = qbit_auto.reconcile_organizing_rows(conn)
    assert out == []
    row = qbit_auto.get_run(conn, "h1")
    assert row["status"] == "organizing"  # 不动


def test_reconcile_succeeded_action_syncs(conn):
    """destructive_action succeeded → mark_terminal(succeeded) + 抽 counts."""
    import json
    _insert_organizing_with_action(conn, "h1", "act-1")
    _insert_destructive_action(
        conn, "act-1", "succeeded",
        result_json=json.dumps({
            "total_succeeded": 5,
            "total_already_linked": 1,
            "total_failed": 0,
        }),
    )
    out = qbit_auto.reconcile_organizing_rows(conn)
    assert len(out) == 1 and out[0]["synced_to"] == "succeeded"
    row = qbit_auto.get_run(conn, "h1")
    assert row["status"] == "succeeded"
    assert row["files_succeeded"] == 5
    assert row["files_already_linked"] == 1
    assert row["files_failed"] == 0
    assert row["completed_at"] is not None


def test_reconcile_failed_action_syncs(conn):
    _insert_organizing_with_action(conn, "h1", "act-1")
    _insert_destructive_action(conn, "act-1", "failed", error="something broke")
    out = qbit_auto.reconcile_organizing_rows(conn)
    assert out[0]["synced_to"] == "failed"
    assert out[0]["action_status"] == "failed"
    row = qbit_auto.get_run(conn, "h1")
    assert row["status"] == "failed"
    assert row["last_error"] == "something broke"


def test_reconcile_needs_manual_recovery_treated_as_failed(conn):
    """needs_manual_recovery 同 failed 处理（auto 端没有 manual_recovery 状态）."""
    _insert_organizing_with_action(conn, "h1", "act-1")
    _insert_destructive_action(conn, "act-1", "needs_manual_recovery",
                                error="reaper intervened")
    out = qbit_auto.reconcile_organizing_rows(conn)
    assert out[0]["synced_to"] == "failed"
    row = qbit_auto.get_run(conn, "h1")
    assert row["status"] == "failed"


def test_reconcile_missing_action_row_marks_failed(conn):
    """auto_organize_runs.action_id 指向已删 destructive_action row → mark failed."""
    _insert_organizing_with_action(conn, "h1", "act-missing")
    # 不 insert destructive_action
    out = qbit_auto.reconcile_organizing_rows(conn)
    assert out[0]["synced_to"] == "failed"
    assert out[0]["reason"] == "action_row_missing"
    row = qbit_auto.get_run(conn, "h1")
    assert "destructive_action row missing" in row["last_error"]


def test_reconcile_organizing_without_action_id_marks_failed(conn):
    """auto_organize_runs 异常状态: organizing 但 action_id NULL → corrupted, mark failed."""
    _insert_organizing_with_action(conn, "h1", None)
    out = qbit_auto.reconcile_organizing_rows(conn)
    assert out[0]["synced_to"] == "failed"
    assert out[0]["reason"] == "missing_action_id"
    row = qbit_auto.get_run(conn, "h1")
    assert "corrupted state" in row["last_error"]


def test_reconcile_handles_invalid_json_result(conn):
    """destructive_actions.result_json 损坏 / 非法 JSON → silent fallback 不 raise."""
    _insert_organizing_with_action(conn, "h1", "act-1")
    _insert_destructive_action(conn, "act-1", "succeeded",
                                result_json="{not valid json")
    out = qbit_auto.reconcile_organizing_rows(conn)
    assert out[0]["synced_to"] == "succeeded"
    row = qbit_auto.get_run(conn, "h1")
    # 没 result_json → counts 为 0 / None
    assert row["status"] == "succeeded"
    assert row["files_succeeded"] in (0, None)


def test_reconcile_skips_non_organizing_rows(conn):
    """status=pending / succeeded / failed 的 row 不被 reconcile 触碰."""
    _insert_row(conn, "h1", "pending")
    _insert_row(conn, "h2", "succeeded")
    _insert_row(conn, "h3", "failed")
    out = qbit_auto.reconcile_organizing_rows(conn)
    assert out == []
    # 三个 row 状态都不变
    assert qbit_auto.get_run(conn, "h1")["status"] == "pending"
    assert qbit_auto.get_run(conn, "h2")["status"] == "succeeded"
    assert qbit_auto.get_run(conn, "h3")["status"] == "failed"


# ─── codex r1 regression tests ───


def test_list_completed_accepts_progress_0_9999(conn):
    """codex r1 B2 fix: 浮点 round-trip 1.0 略偏小（如 0.9999999）也算 completed."""
    out = qbit_auto.list_completed_torrents(
        [_t(progress=0.9999999, hash="h-fp")], ["Movies"],
    )
    assert len(out) == 1


def test_list_completed_accepts_progress_1_0000001(conn):
    """浮点 round-trip 略偏大也算 completed (>= 0.999 thresh)."""
    out = qbit_auto.list_completed_torrents(
        [_t(progress=1.0000001, hash="h-fp2")], ["Movies"],
    )
    assert len(out) == 1


def test_list_completed_filters_progress_below_threshold(conn):
    """progress < 0.999 仍要排除（防误算未完成种子）."""
    out = qbit_auto.list_completed_torrents(
        [_t(progress=0.95)], ["Movies"],
    )
    assert out == []


def test_dispatch_one_claim_lost_does_not_touch_destructive_action(conn, monkeypatch):
    """codex r2 派生 fix: claim_lost 路径不能动 destructive_actions row（防污染
    worker 在跑的状态机）。worker 自己 mark_terminal_if_running 处理终态，
    organize 幂等保证下周期 cron 重 dispatch 安全。
    """
    _patch_cache(monkeypatch, {
        "/d/Some.Movie.mkv": (_CachedStub(media_type="movie", metadata_confidence=0.9), "hit"),
    })

    # 模拟 race: mark_organizing 返 False
    monkeypatch.setattr(qbit_auto, "mark_organizing", lambda *a, **kw: False)

    # 加 spy 监视 destructive_action._mark_terminal — 不该被调
    from services import destructive_action as _da
    mark_terminal_called = []
    original = _da._mark_terminal
    monkeypatch.setattr(
        _da, "_mark_terminal",
        lambda *a, **kw: mark_terminal_called.append((a, kw)) or original(*a, **kw),
    )

    out = qbit_auto.dispatch_one(
        conn, _torrent(hash="h-claim-lost"),
        list_video_paths_fn=lambda p: ["/d/Some.Movie.mkv"],
        confidence_threshold=0.85,
        build_and_start_organize_fn=lambda paths, h: {
            "action_id": "act-ghost", "status": "started", "error": None,
        },
    )
    assert out["action"] == "claim_lost"
    assert out["action_id"] == "act-ghost"
    # 关键断言：claim_lost 路径**没**调 _mark_terminal（不污染 worker）
    assert mark_terminal_called == [], (
        f"claim_lost path should not touch destructive_action, but _mark_terminal "
        f"was called {len(mark_terminal_called)} times"
    )


def test_list_history_rejects_invalid_status_filter(conn):
    """codex r1 N3: 非法 status_filter 抛 ValueError（route 层转 400）."""
    import pytest
    with pytest.raises(ValueError, match="invalid status_filter"):
        qbit_auto.list_history(conn, status_filter="bogus")


def test_list_history_accepts_all_valid_statuses(conn):
    """VALID_STATUSES 包含所有 schema CHECK 允许的 status."""
    for status in qbit_auto.VALID_STATUSES:
        # 不该抛 — 只验枚举校验，结果可以空
        qbit_auto.list_history(conn, status_filter=status)
