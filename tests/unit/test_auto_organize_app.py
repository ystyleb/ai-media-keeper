"""Phase 4C.3: app-level _build_and_start_auto_organize callback tests.

覆盖 cron 触发 callback 的关键路径：config 守门 / batch 上限 / lock 冲突 /
worker start 失败 / happy path. SSH + organize_runner 全部 monkeypatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@dataclass
class _CachedStub:
    title: str
    media_type: str
    year: int | None = None
    tmdb_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    inode: int | None = None
    mtime: int | None = None


def _src_stat_ok(p, inode=100, size=1024, mtime=1000):
    return {p: {"exists": True, "inode": inode, "size_bytes": size, "mtime": mtime}}


def _patch_organize_config(monkeypatch, movies_root="/m", tv_root="/t"):
    monkeypatch.setattr(
        app_module, "load_organize_config",
        lambda: {"movies_root": movies_root, "tv_root": tv_root},
    )


def _patch_cache(monkeypatch, cached_or_none):
    status = "hit" if cached_or_none else "miss"
    monkeypatch.setattr(
        app_module.metadata_cache, "get_many_by_path",
        lambda conn, paths, *, current_stats=None: {
            p: (cached_or_none, status) for p in paths
        },
    )


# ── config guard ───


def test_build_missing_organize_roots_returns_error(client, monkeypatch):
    monkeypatch.setattr(app_module, "load_organize_config", lambda: {})
    out = app_module._build_and_start_auto_organize(["/x.mkv"], "hash1")
    assert out["status"] == "error"
    assert out["error"] == "organize_roots_not_configured"


def test_build_empty_paths_returns_error(client, monkeypatch):
    _patch_organize_config(monkeypatch)
    out = app_module._build_and_start_auto_organize([], "hash1")
    assert out["status"] == "error"
    assert out["error"] == "empty_paths"


def test_build_batch_too_large_returns_error(client, monkeypatch):
    _patch_organize_config(monkeypatch)
    paths = [f"/x{i}.mkv" for i in range(app_module.MAX_ORGANIZE_BATCH_ITEMS + 1)]
    out = app_module._build_and_start_auto_organize(paths, "hash1")
    assert out["status"] == "error"
    assert "batch_too_large" in out["error"]


# ── lock / start_worker error paths ───


def test_build_concurrent_organize_returns_locked(client, monkeypatch):
    """ConcurrentOrganizeError → 应回滚 row 到 pending + 返 locked（cron 下周期重试）."""
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        lambda paths: {p: {"exists": True, "inode": 100, "size_bytes": 1024, "mtime": 1000}
                       for p in paths},
    )
    _patch_cache(monkeypatch, _CachedStub(
        title="Movie", media_type="movie", year=2024, tmdb_id="1",
    ))

    def raise_concurrent(*a, **kw):
        raise app_module.organize_runner.ConcurrentOrganizeError("busy")

    monkeypatch.setattr(
        app_module.organize_runner, "start_organize_executor", raise_concurrent,
    )
    out = app_module._build_and_start_auto_organize(["/x.mkv"], "hash-locked")
    assert out["status"] == "locked"
    assert out["error"] is None
    # action_id 存在但 row 已 rollback 到 pending（不会卡 reaper）
    assert out["action_id"] is not None
    with app_module.app.test_request_context():
        c = app_module.get_db()
        row = c.execute(
            "SELECT status, consumed_at FROM destructive_actions WHERE action_id=?",
            (out["action_id"],),
        ).fetchone()
    assert row["status"] == "pending"


def test_build_worker_start_raises_returns_error_marks_failed(client, monkeypatch):
    """非 ConcurrentOrganizeError 异常 → action row 标 failed（不让 reaper 卡死）."""
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        lambda paths: {p: {"exists": True, "inode": 100, "size_bytes": 1024, "mtime": 1000}
                       for p in paths},
    )
    _patch_cache(monkeypatch, _CachedStub(
        title="Movie", media_type="movie", year=2024, tmdb_id="1",
    ))

    def raise_oserror(*a, **kw):
        raise OSError("can't start new thread")

    monkeypatch.setattr(
        app_module.organize_runner, "start_organize_executor", raise_oserror,
    )
    out = app_module._build_and_start_auto_organize(["/x.mkv"], "hash-err")
    assert out["status"] == "error"
    assert "OSError" in out["error"]
    # action 应标 failed
    with app_module.app.test_request_context():
        c = app_module.get_db()
        row = c.execute(
            "SELECT status FROM destructive_actions WHERE action_id=?",
            (out["action_id"],),
        ).fetchone()
    assert row["status"] == "failed"


# ── happy path ───


def test_build_happy_path_starts_worker_with_cron_created_by(client, monkeypatch):
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        lambda paths: {p: {"exists": True, "inode": 100, "size_bytes": 1024, "mtime": 1000}
                       for p in paths},
    )
    _patch_cache(monkeypatch, _CachedStub(
        title="Movie", media_type="movie", year=2024, tmdb_id="1",
    ))

    started_kwargs = {}

    def capture_start(*, db_path, action_id, payload, execute_one_item, selected_indices):
        started_kwargs["action_id"] = action_id
        started_kwargs["payload"] = payload
        started_kwargs["selected_indices"] = selected_indices
        started_kwargs["execute_one_item"] = execute_one_item

    monkeypatch.setattr(
        app_module.organize_runner, "start_organize_executor", capture_start,
    )
    out = app_module._build_and_start_auto_organize(["/x.mkv"], "hash-ok")
    assert out["status"] == "started"
    assert out["error"] is None
    assert out["action_id"] is not None

    # 验证 created_by='cron' + selected_indices=None (auto 跑全部)
    with app_module.app.test_request_context():
        c = app_module.get_db()
        row = c.execute(
            "SELECT created_by, status, kind FROM destructive_actions WHERE action_id=?",
            (out["action_id"],),
        ).fetchone()
    assert row["created_by"] == "cron"
    assert row["kind"] == "organize"
    # status='running' （atomic_consume 之后）
    assert row["status"] == "running"
    # worker 接到正确 callback
    assert started_kwargs["selected_indices"] is None
    assert started_kwargs["execute_one_item"] is app_module._organize_executor_one_item_threadsafe
    # payload 含 auto_organize section（qbit_hash audit）
    assert started_kwargs["payload"]["auto_organize"]["qbit_hash"] == "hash-ok"


def test_build_drifted_cache_returns_error(client, monkeypatch):
    """confidence_gate pass 但到 build 时 cache 漂移 / src 全 missing → no plans error."""
    _patch_organize_config(monkeypatch)
    # src 全部 not exists
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        lambda paths: {p: {"exists": False} for p in paths},
    )
    _patch_cache(monkeypatch, _CachedStub(
        title="Movie", media_type="movie", year=2024, tmdb_id="1",
    ))
    out = app_module._build_and_start_auto_organize(["/x.mkv"], "hash-drift")
    assert out["status"] == "error"
    assert "no plans computed" in out["error"]
    assert "src_missing" in out["error"]


# ── 4C.4 _cron_qbit_auto_organize ───
# 单测直接调函数（不起 APScheduler），mock qbit + config + dispatch_one.


def test_cron_disabled_returns_early(client, monkeypatch):
    """enabled=False → 不调 qbit.get_torrents。"""
    monkeypatch.setattr(
        app_module, "load_qbit_auto_organize_config",
        lambda: {**app_module.QBIT_AUTO_ORGANIZE_DEFAULTS, "enabled": False},
    )
    called = MagicMock()
    monkeypatch.setattr(app_module.qbit, "get_torrents", called)
    app_module._cron_qbit_auto_organize()
    called.assert_not_called()


def test_cron_empty_whitelist_skips_dispatch(client, monkeypatch):
    """enabled=True 但 categories=[] → 不调 qbit (whitelist 空 = 不触发)."""
    monkeypatch.setattr(
        app_module, "load_qbit_auto_organize_config",
        lambda: {**app_module.QBIT_AUTO_ORGANIZE_DEFAULTS,
                 "enabled": True, "categories": []},
    )
    called = MagicMock()
    monkeypatch.setattr(app_module.qbit, "get_torrents", called)
    app_module._cron_qbit_auto_organize()
    called.assert_not_called()


def test_cron_qbit_api_failure_logs_and_continues(client, monkeypatch, caplog):
    """qbit.get_torrents() 抛异常 → log error 但不 propagate（不让单次 API 故障杀 cron）."""
    monkeypatch.setattr(
        app_module, "load_qbit_auto_organize_config",
        lambda: {**app_module.QBIT_AUTO_ORGANIZE_DEFAULTS,
                 "enabled": True, "categories": ["Movies"]},
    )

    def boom():
        raise RuntimeError("qbit down")

    monkeypatch.setattr(app_module.qbit, "get_torrents", boom)
    # 不抛异常（cron job 不能 die）
    app_module._cron_qbit_auto_organize()


def test_cron_dispatches_unprocessed_torrents(client, monkeypatch):
    """完整 happy path：扫 → filter → dispatch_one 被调一次每未处理 hash."""
    monkeypatch.setattr(
        app_module, "load_qbit_auto_organize_config",
        lambda: {**app_module.QBIT_AUTO_ORGANIZE_DEFAULTS,
                 "enabled": True, "categories": ["Movies"]},
    )
    monkeypatch.setattr(
        app_module.qbit, "get_torrents",
        lambda: [
            {"hash": "h1", "name": "M1", "category": "Movies",
             "state": "seeding", "progress": 1.0, "content_path": "/d/M1.mkv"},
            {"hash": "h2", "name": "M2", "category": "Movies",
             "state": "seeding", "progress": 1.0, "content_path": "/d/M2.mkv"},
            {"hash": "h3", "name": "M3", "category": "Music",  # not in whitelist
             "state": "seeding", "progress": 1.0, "content_path": "/d/M3.mkv"},
        ],
    )

    dispatched: list[str] = []

    def fake_dispatch(conn, torrent, **kw):
        dispatched.append(torrent["hash"])
        return {"action": "started", "qbit_hash": torrent["hash"], "action_id": "act-x"}

    monkeypatch.setattr(app_module.qbit_auto, "dispatch_one", fake_dispatch)
    app_module._cron_qbit_auto_organize()
    # h3 不在白名单 → 不 dispatch; h1, h2 dispatch
    assert sorted(dispatched) == ["h1", "h2"]


def test_cron_locked_breaks_remaining_torrents(client, monkeypatch):
    """dispatch_one 返 locked → break，剩余 torrents 推迟下周期不再 dispatch."""
    monkeypatch.setattr(
        app_module, "load_qbit_auto_organize_config",
        lambda: {**app_module.QBIT_AUTO_ORGANIZE_DEFAULTS,
                 "enabled": True, "categories": ["Movies"]},
    )
    monkeypatch.setattr(
        app_module.qbit, "get_torrents",
        lambda: [
            {"hash": "h1", "name": "M1", "category": "Movies",
             "state": "seeding", "progress": 1.0, "content_path": "/d/M1.mkv"},
            {"hash": "h2", "name": "M2", "category": "Movies",
             "state": "seeding", "progress": 1.0, "content_path": "/d/M2.mkv"},
        ],
    )
    dispatched: list[str] = []

    def fake_dispatch(conn, torrent, **kw):
        dispatched.append(torrent["hash"])
        return {"action": "locked", "qbit_hash": torrent["hash"]}

    monkeypatch.setattr(app_module.qbit_auto, "dispatch_one", fake_dispatch)
    app_module._cron_qbit_auto_organize()
    # 第一个 locked 后 break，第二个不被 dispatch
    assert dispatched == ["h1"]


def test_cron_skips_already_processed_hashes(client, monkeypatch):
    """auto_organize_runs 已有 succeeded row → filter 跳过该 hash."""
    # 预先标记 h1 succeeded
    with app_module.app.test_request_context():
        c = app_module.get_db()
        import time as _t
        c.execute(
            "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,attempts,"
            "created_at,completed_at) VALUES(?,?,?,?,?,?)",
            ("h1-already-done", "/x", "succeeded", 1, int(_t.time()),
             int(_t.time())),
        )
        c.commit()

    monkeypatch.setattr(
        app_module, "load_qbit_auto_organize_config",
        lambda: {**app_module.QBIT_AUTO_ORGANIZE_DEFAULTS,
                 "enabled": True, "categories": ["Movies"]},
    )
    monkeypatch.setattr(
        app_module.qbit, "get_torrents",
        lambda: [
            {"hash": "h1-already-done", "name": "M1", "category": "Movies",
             "state": "seeding", "progress": 1.0, "content_path": "/d/M1.mkv"},
            {"hash": "h2-new", "name": "M2", "category": "Movies",
             "state": "seeding", "progress": 1.0, "content_path": "/d/M2.mkv"},
        ],
    )
    dispatched = []
    monkeypatch.setattr(
        app_module.qbit_auto, "dispatch_one",
        lambda conn, t, **kw: dispatched.append(t["hash"])
        or {"action": "started", "qbit_hash": t["hash"], "action_id": "x"},
    )
    app_module._cron_qbit_auto_organize()
    assert dispatched == ["h2-new"]
    # 清理（避免影响其他测试）
    with app_module.app.test_request_context():
        c = app_module.get_db()
        c.execute("DELETE FROM auto_organize_runs WHERE qbit_hash IN (?,?)",
                  ("h1-already-done", "h2-new"))
        c.commit()


def test_cron_calls_reconcile_before_dispatch(client, monkeypatch):
    """reconcile_organizing_rows 总是先调（独立于 enabled）."""
    monkeypatch.setattr(
        app_module, "load_qbit_auto_organize_config",
        lambda: {**app_module.QBIT_AUTO_ORGANIZE_DEFAULTS, "enabled": False},
    )
    reconcile_called = MagicMock(return_value=[])
    monkeypatch.setattr(
        app_module.qbit_auto, "reconcile_organizing_rows", reconcile_called,
    )
    app_module._cron_qbit_auto_organize()
    reconcile_called.assert_called_once()
