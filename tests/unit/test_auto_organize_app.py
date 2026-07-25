"""Phase 4C.3: app-level _build_and_start_auto_organize callback tests.

覆盖 cron 触发 callback 的关键路径：config 守门 / batch 上限 / lock 冲突 /
worker start 失败 / happy path. SSH + organize_runner 全部 monkeypatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

import app as app_module
from db import migrations
from services import destructive_action


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 隔离 DB：每个 test 用独立 tmp actions.db，避免读/写真实 config/actions.db
    # （否则真库的真实 run 行会干扰断言，如 limit=N 把 seed 行挤出去）。
    db_path = tmp_path / "actions.db"
    c = destructive_action.open_connection(db_path)
    destructive_action.init_schema(
        c, __import__("pathlib").Path(__file__).resolve().parents[2] / "db" / "schema.sql"
    )
    migrations.phase3_migrate(c)
    migrations.phase4_migrate(c)
    migrations.phase5_migrate(c)
    c.close()
    monkeypatch.setattr(app_module, "DB_PATH", db_path)
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
    metadata_status: str = "ok"


def _src_stat_ok(p, inode=100, size=1024, mtime=1000):
    return {p: {"exists": True, "inode": inode, "size_bytes": size, "mtime": mtime}}


def _patch_organize_config(monkeypatch, movies_root="/m", tv_root="/t"):
    monkeypatch.setattr(
        app_module,
        "load_organize_config",
        lambda: {"movies_root": movies_root, "tv_root": tv_root},
    )


def _patch_cache(monkeypatch, cached_or_none):
    status = "hit" if cached_or_none else "miss"
    monkeypatch.setattr(
        app_module.metadata_cache,
        "get_many_by_path",
        lambda conn, paths, *, current_stats=None: {p: (cached_or_none, status) for p in paths},
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
        app_module,
        "_ssh_stat_paths",
        lambda paths: {
            p: {"exists": True, "inode": 100, "size_bytes": 1024, "mtime": 1000} for p in paths
        },
    )
    _patch_cache(
        monkeypatch,
        _CachedStub(
            title="Movie",
            media_type="movie",
            year=2024,
            tmdb_id="1",
        ),
    )

    def raise_concurrent(*a, **kw):
        raise app_module.organize_runner.ConcurrentOrganizeError("busy")

    monkeypatch.setattr(
        app_module.organize_runner,
        "start_organize_executor",
        raise_concurrent,
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
        app_module,
        "_ssh_stat_paths",
        lambda paths: {
            p: {"exists": True, "inode": 100, "size_bytes": 1024, "mtime": 1000} for p in paths
        },
    )
    _patch_cache(
        monkeypatch,
        _CachedStub(
            title="Movie",
            media_type="movie",
            year=2024,
            tmdb_id="1",
        ),
    )

    def raise_oserror(*a, **kw):
        raise OSError("can't start new thread")

    monkeypatch.setattr(
        app_module.organize_runner,
        "start_organize_executor",
        raise_oserror,
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
        app_module,
        "_ssh_stat_paths",
        lambda paths: {
            p: {"exists": True, "inode": 100, "size_bytes": 1024, "mtime": 1000} for p in paths
        },
    )
    _patch_cache(
        monkeypatch,
        _CachedStub(
            title="Movie",
            media_type="movie",
            year=2024,
            tmdb_id="1",
        ),
    )

    started_kwargs = {}

    def capture_start(*, db_path, action_id, payload, execute_one_item, selected_indices):
        started_kwargs["action_id"] = action_id
        started_kwargs["payload"] = payload
        started_kwargs["selected_indices"] = selected_indices
        started_kwargs["execute_one_item"] = execute_one_item

    monkeypatch.setattr(
        app_module.organize_runner,
        "start_organize_executor",
        capture_start,
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
        app_module,
        "_ssh_stat_paths",
        lambda paths: {p: {"exists": False} for p in paths},
    )
    _patch_cache(
        monkeypatch,
        _CachedStub(
            title="Movie",
            media_type="movie",
            year=2024,
            tmdb_id="1",
        ),
    )
    out = app_module._build_and_start_auto_organize(["/x.mkv"], "hash-drift")
    assert out["status"] == "error"
    assert "no plans computed" in out["error"]
    assert "src_missing" in out["error"]


# ── 4C.4 _cron_qbit_auto_organize ───
# 单测直接调函数（不起 APScheduler），mock qbit + config + dispatch_one.


def test_cron_disabled_returns_early(client, monkeypatch):
    """enabled=False → 不调 qbit.get_torrents。"""
    monkeypatch.setattr(
        app_module,
        "load_qbit_auto_organize_config",
        lambda: {**app_module.QBIT_AUTO_ORGANIZE_DEFAULTS, "enabled": False},
    )
    called = MagicMock()
    monkeypatch.setattr(app_module.qbit, "get_torrents", called)
    app_module._cron_qbit_auto_organize()
    called.assert_not_called()


def test_cron_empty_whitelist_skips_dispatch(client, monkeypatch):
    """enabled=True 但 categories=[] → 不调 qbit (whitelist 空 = 不触发)."""
    monkeypatch.setattr(
        app_module,
        "load_qbit_auto_organize_config",
        lambda: {**app_module.QBIT_AUTO_ORGANIZE_DEFAULTS, "enabled": True, "categories": []},
    )
    called = MagicMock()
    monkeypatch.setattr(app_module.qbit, "get_torrents", called)
    app_module._cron_qbit_auto_organize()
    called.assert_not_called()


def test_cron_qbit_api_failure_logs_and_continues(client, monkeypatch, caplog):
    """qbit.get_torrents() 抛异常 → log error 但不 propagate（不让单次 API 故障杀 cron）."""
    monkeypatch.setattr(
        app_module,
        "load_qbit_auto_organize_config",
        lambda: {
            **app_module.QBIT_AUTO_ORGANIZE_DEFAULTS,
            "enabled": True,
            "categories": ["Movies"],
        },
    )

    def boom():
        raise RuntimeError("qbit down")

    monkeypatch.setattr(app_module.qbit, "get_torrents", boom)
    # 不抛异常（cron job 不能 die）
    app_module._cron_qbit_auto_organize()


def test_cron_dispatches_unprocessed_torrents(client, monkeypatch):
    """完整 happy path：扫 → filter → dispatch_one 被调一次每未处理 hash."""
    monkeypatch.setattr(
        app_module,
        "load_qbit_auto_organize_config",
        lambda: {
            **app_module.QBIT_AUTO_ORGANIZE_DEFAULTS,
            "enabled": True,
            "categories": ["Movies"],
        },
    )
    monkeypatch.setattr(
        app_module.qbit,
        "get_torrents",
        lambda: [
            {
                "hash": "h1",
                "name": "M1",
                "category": "Movies",
                "state": "seeding",
                "progress": 1.0,
                "content_path": "/d/M1.mkv",
            },
            {
                "hash": "h2",
                "name": "M2",
                "category": "Movies",
                "state": "seeding",
                "progress": 1.0,
                "content_path": "/d/M2.mkv",
            },
            {
                "hash": "h3",
                "name": "M3",
                "category": "Music",  # not in whitelist
                "state": "seeding",
                "progress": 1.0,
                "content_path": "/d/M3.mkv",
            },
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
        app_module,
        "load_qbit_auto_organize_config",
        lambda: {
            **app_module.QBIT_AUTO_ORGANIZE_DEFAULTS,
            "enabled": True,
            "categories": ["Movies"],
        },
    )
    monkeypatch.setattr(
        app_module.qbit,
        "get_torrents",
        lambda: [
            {
                "hash": "h1",
                "name": "M1",
                "category": "Movies",
                "state": "seeding",
                "progress": 1.0,
                "content_path": "/d/M1.mkv",
            },
            {
                "hash": "h2",
                "name": "M2",
                "category": "Movies",
                "state": "seeding",
                "progress": 1.0,
                "content_path": "/d/M2.mkv",
            },
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
            ("h1-already-done", "/x", "succeeded", 1, int(_t.time()), int(_t.time())),
        )
        c.commit()

    monkeypatch.setattr(
        app_module,
        "load_qbit_auto_organize_config",
        lambda: {
            **app_module.QBIT_AUTO_ORGANIZE_DEFAULTS,
            "enabled": True,
            "categories": ["Movies"],
        },
    )
    monkeypatch.setattr(
        app_module.qbit,
        "get_torrents",
        lambda: [
            {
                "hash": "h1-already-done",
                "name": "M1",
                "category": "Movies",
                "state": "seeding",
                "progress": 1.0,
                "content_path": "/d/M1.mkv",
            },
            {
                "hash": "h2-new",
                "name": "M2",
                "category": "Movies",
                "state": "seeding",
                "progress": 1.0,
                "content_path": "/d/M2.mkv",
            },
        ],
    )
    dispatched = []
    monkeypatch.setattr(
        app_module.qbit_auto,
        "dispatch_one",
        lambda conn, t, **kw: (
            dispatched.append(t["hash"])
            or {"action": "started", "qbit_hash": t["hash"], "action_id": "x"}
        ),
    )
    app_module._cron_qbit_auto_organize()
    assert dispatched == ["h2-new"]
    # 清理（避免影响其他测试）
    with app_module.app.test_request_context():
        c = app_module.get_db()
        c.execute(
            "DELETE FROM auto_organize_runs WHERE qbit_hash IN (?,?)", ("h1-already-done", "h2-new")
        )
        c.commit()


def test_cron_calls_reconcile_before_dispatch(client, monkeypatch):
    """reconcile_organizing_rows 总是先调（独立于 enabled）."""
    monkeypatch.setattr(
        app_module,
        "load_qbit_auto_organize_config",
        lambda: {**app_module.QBIT_AUTO_ORGANIZE_DEFAULTS, "enabled": False},
    )
    reconcile_called = MagicMock(return_value=[])
    monkeypatch.setattr(
        app_module.qbit_auto,
        "reconcile_organizing_rows",
        reconcile_called,
    )
    app_module._cron_qbit_auto_organize()
    reconcile_called.assert_called_once()


# ── 4C.5 routes ───


@pytest.fixture
def token():
    return app_module.API_TOKEN


def test_get_config_returns_defaults(client, token, monkeypatch, tmp_path):
    """GET /api/config/qbit-auto-organize 默认 disabled + 空 categories."""
    cfg_file = tmp_path / "qbit_auto_organize.json"
    monkeypatch.setattr(app_module, "QBIT_AUTO_ORGANIZE_CONFIG_FILE", cfg_file)
    monkeypatch.setattr(app_module.qbit, "get_torrents", lambda: [])
    resp = client.get(
        "/api/config/qbit-auto-organize",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["enabled"] is False
    assert body["categories"] == []
    assert body["poll_interval_minutes"] == 5
    assert body["confidence_threshold"] == 0.85
    assert body["available_qbit_categories"] == []
    assert body["qbit_fetch_error"] is None
    assert body["active_changes_require_restart"] is True


def test_get_config_lists_qbit_categories(client, token, monkeypatch, tmp_path):
    """available_qbit_categories 列出当前 qBit 现存 categories（dedup + 排序）."""
    cfg_file = tmp_path / "qbit_auto_organize.json"
    monkeypatch.setattr(app_module, "QBIT_AUTO_ORGANIZE_CONFIG_FILE", cfg_file)
    monkeypatch.setattr(
        app_module.qbit,
        "get_torrents",
        lambda: [
            {"category": "Movies"},
            {"category": "TV"},
            {"category": "Movies"},
            {"category": "Music"},
            {"category": ""},  # 空 category 应过滤
        ],
    )
    resp = client.get(
        "/api/config/qbit-auto-organize",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    assert body["available_qbit_categories"] == ["Movies", "Music", "TV"]


def test_get_config_handles_qbit_error_gracefully(client, token, monkeypatch, tmp_path):
    """qBit down → qbit_fetch_error 填入，主路径仍 200."""
    cfg_file = tmp_path / "qbit_auto_organize.json"
    monkeypatch.setattr(app_module, "QBIT_AUTO_ORGANIZE_CONFIG_FILE", cfg_file)

    def boom():
        raise ConnectionError("qbit unreachable")

    monkeypatch.setattr(app_module.qbit, "get_torrents", boom)
    resp = client.get(
        "/api/config/qbit-auto-organize",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["available_qbit_categories"] == []
    assert "ConnectionError" in body["qbit_fetch_error"]


def test_post_config_saves_and_returns_clamped(client, token, monkeypatch, tmp_path):
    """POST 落盘 + 边界清洗 + return clamp 后的值."""
    cfg_file = tmp_path / "qbit_auto_organize.json"
    monkeypatch.setattr(app_module, "QBIT_AUTO_ORGANIZE_CONFIG_FILE", cfg_file)
    resp = client.post(
        "/api/config/qbit-auto-organize",
        json={
            "enabled": True,
            "categories": ["Movies", "TV"],
            "poll_interval_minutes": 0,  # → clamp 1
            "confidence_threshold": 2.5,  # → clamp 1.0
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["enabled"] is True
    assert body["categories"] == ["Movies", "TV"]
    assert body["poll_interval_minutes"] == 1
    assert body["confidence_threshold"] == 1.0
    assert "重启 server" in body["message"]


def test_list_runs_empty(client, token):
    resp = client.get(
        "/api/auto-organize/runs",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    assert resp.status_code == 200
    assert isinstance(body["runs"], list)
    assert "total" in body
    assert body["limit"] == 50
    assert body["offset"] == 0


def test_list_runs_with_status_filter(client, token):
    """status filter + total count 跟 filter 一致."""
    import time

    with app_module.app.test_request_context():
        c = app_module.get_db()
        c.execute(
            "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,attempts,"
            "created_at) VALUES(?,?,?,?,?)",
            ("h-success-1", "/x", "succeeded", 1, int(time.time())),
        )
        c.execute(
            "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,attempts,"
            "created_at) VALUES(?,?,?,?,?)",
            ("h-failed-1", "/x", "failed", 1, int(time.time())),
        )
        c.commit()

    resp = client.get(
        "/api/auto-organize/runs?status=succeeded&limit=10",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    assert resp.status_code == 200
    hashes = {r["qbit_hash"] for r in body["runs"]}
    assert "h-success-1" in hashes
    assert "h-failed-1" not in hashes
    assert body["total"] >= 1  # 可能有其他 test 残留

    # cleanup
    with app_module.app.test_request_context():
        c = app_module.get_db()
        c.execute(
            "DELETE FROM auto_organize_runs WHERE qbit_hash IN (?,?)", ("h-success-1", "h-failed-1")
        )
        c.commit()


def test_reset_run_missing_hash_returns_400(client, token):
    resp = client.post(
        "/api/auto-organize/reset",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_reset_run_not_found_returns_404(client, token):
    resp = client.post(
        "/api/auto-organize/reset",
        json={"qbit_hash": "ghost-hash-xyz"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


def test_reset_run_rejects_active_status(client, token):
    """organizing / pending 不能 reset (副作用未完成)."""
    import time

    with app_module.app.test_request_context():
        c = app_module.get_db()
        c.execute(
            "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,attempts,"
            "created_at) VALUES(?,?,?,?,?)",
            ("h-organizing-1", "/x", "organizing", 1, int(time.time())),
        )
        c.commit()
    try:
        resp = client.post(
            "/api/auto-organize/reset",
            json={"qbit_hash": "h-organizing-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 409
        body = resp.get_json()
        assert body["current_status"] == "organizing"
    finally:
        with app_module.app.test_request_context():
            c = app_module.get_db()
            c.execute("DELETE FROM auto_organize_runs WHERE qbit_hash=?", ("h-organizing-1",))
            c.commit()


def test_reset_run_terminal_deletes_row(client, token):
    """failed / skipped_* row 可 reset (DELETE)，下周期 cron 重试."""
    import time

    with app_module.app.test_request_context():
        c = app_module.get_db()
        c.execute(
            "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,attempts,"
            "created_at,completed_at) VALUES(?,?,?,?,?,?)",
            ("h-failed-reset-1", "/x", "failed", 1, int(time.time()), int(time.time())),
        )
        c.commit()
    resp = client.post(
        "/api/auto-organize/reset",
        json={"qbit_hash": "h-failed-reset-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["previous_status"] == "failed"
    # row 真的删了
    with app_module.app.test_request_context():
        c = app_module.get_db()
        assert qbit_auto_check_get(c, "h-failed-reset-1") is None


def qbit_auto_check_get(conn, qbit_hash):
    from services import qbit_auto as qa

    return qa.get_run(conn, qbit_hash)


# ─── codex r1 regression ───


def test_post_config_enabled_without_categories_rejected(client, token, monkeypatch, tmp_path):
    """codex r1 I2 fix: enabled=True + categories=[] → 400 fail-fast，不让用户
    误以为已启用但实际不触发任何种子."""
    cfg_file = tmp_path / "qbit_auto_organize.json"
    monkeypatch.setattr(app_module, "QBIT_AUTO_ORGANIZE_CONFIG_FILE", cfg_file)
    resp = client.post(
        "/api/config/qbit-auto-organize",
        json={"enabled": True, "categories": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "categories_required_when_enabled"


def test_post_config_disabled_with_empty_categories_allowed(client, token, monkeypatch, tmp_path):
    """disabled 时 categories=[] 仍允许（用户关掉自动整理后保留空 list）."""
    cfg_file = tmp_path / "qbit_auto_organize.json"
    monkeypatch.setattr(app_module, "QBIT_AUTO_ORGANIZE_CONFIG_FILE", cfg_file)
    resp = client.post(
        "/api/config/qbit-auto-organize",
        json={"enabled": False, "categories": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_post_config_enabled_with_whitespace_categories_rejected(
    client, token, monkeypatch, tmp_path
):
    """categories 全是空白 → 清洗后为空 → 等价 enabled+empty 应 400."""
    cfg_file = tmp_path / "qbit_auto_organize.json"
    monkeypatch.setattr(app_module, "QBIT_AUTO_ORGANIZE_CONFIG_FILE", cfg_file)
    resp = client.post(
        "/api/config/qbit-auto-organize",
        json={"enabled": True, "categories": ["", "  ", None]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_list_runs_invalid_status_filter_returns_400(client, token):
    """codex r1 N3: 非法 status_filter → 400 + 提示."""
    resp = client.get(
        "/api/auto-organize/runs?status=bogus",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "invalid_status_filter"


# ─── reset trigger_now (UX improvement) ───


def test_reset_without_trigger_now_does_not_call_qbit(client, token, monkeypatch):
    """trigger_now=False (default) → 只 DELETE row 不调 qBit。"""
    import time as _t

    with app_module.app.test_request_context():
        c = app_module.get_db()
        c.execute(
            "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,attempts,"
            "created_at,completed_at) VALUES(?,?,?,?,?,?)",
            ("h-no-trigger", "/x", "failed", 1, int(_t.time()), int(_t.time())),
        )
        c.commit()

    qbit_called = False

    def fake_get_torrents():
        nonlocal qbit_called
        qbit_called = True
        return []

    monkeypatch.setattr(app_module.qbit, "get_torrents", fake_get_torrents)
    resp = client.post(
        "/api/auto-organize/reset",
        json={"qbit_hash": "h-no-trigger"},  # no trigger_now
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["trigger_result"] is None
    assert qbit_called is False


def test_reset_with_trigger_now_qbit_torrent_not_found(client, token, monkeypatch):
    """trigger_now=True 但 qBit 没这个 hash → trigger_result.action='qbit_torrent_not_found'."""
    import time as _t

    with app_module.app.test_request_context():
        c = app_module.get_db()
        c.execute(
            "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,attempts,"
            "created_at,completed_at) VALUES(?,?,?,?,?,?)",
            ("h-not-in-qbit", "/x", "failed", 1, int(_t.time()), int(_t.time())),
        )
        c.commit()

    monkeypatch.setattr(
        app_module.qbit,
        "get_torrents",
        lambda: [
            {"hash": "different-hash", "name": "other"},
        ],
    )
    resp = client.post(
        "/api/auto-organize/reset",
        json={"qbit_hash": "h-not-in-qbit", "trigger_now": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    assert body["ok"] is True
    assert body["trigger_result"]["action"] == "qbit_torrent_not_found"


def test_reset_with_trigger_now_qbit_down_returns_trigger_failed(client, token, monkeypatch):
    """trigger_now=True 但 qBit 502 → trigger_result.action='trigger_failed'，reset 仍 ok."""
    import time as _t

    with app_module.app.test_request_context():
        c = app_module.get_db()
        c.execute(
            "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,attempts,"
            "created_at,completed_at) VALUES(?,?,?,?,?,?)",
            ("h-qbit-down", "/x", "failed", 1, int(_t.time()), int(_t.time())),
        )
        c.commit()

    def boom():
        raise RuntimeError("qBit login failed: 502")

    monkeypatch.setattr(app_module.qbit, "get_torrents", boom)
    resp = client.post(
        "/api/auto-organize/reset",
        json={"qbit_hash": "h-qbit-down", "trigger_now": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    assert body["ok"] is True  # reset 仍成功
    assert body["trigger_result"]["action"] == "trigger_failed"
    assert "502" in body["trigger_result"]["error"]


def test_reset_with_trigger_now_dispatches(client, token, monkeypatch):
    """trigger_now=True + qBit 有 hash → 调 dispatch_one + 返结果."""
    import time as _t

    with app_module.app.test_request_context():
        c = app_module.get_db()
        c.execute(
            "INSERT INTO auto_organize_runs(qbit_hash,content_path,status,attempts,"
            "created_at,completed_at) VALUES(?,?,?,?,?,?)",
            ("h-dispatch", "/x", "failed", 1, int(_t.time()), int(_t.time())),
        )
        c.commit()

    monkeypatch.setattr(
        app_module.qbit,
        "get_torrents",
        lambda: [
            {
                "hash": "h-dispatch",
                "name": "X",
                "category": "Movies",
                "state": "seeding",
                "progress": 1.0,
                "content_path": "/d/x.mkv",
            },
        ],
    )
    # mock dispatch_one 返 started
    captured = {}

    def fake_dispatch(conn, torrent, **kw):
        captured["called_with_hash"] = torrent["hash"]
        return {"action": "started", "qbit_hash": torrent["hash"], "action_id": "act-trig-1"}

    monkeypatch.setattr(app_module.qbit_auto, "dispatch_one", fake_dispatch)
    resp = client.post(
        "/api/auto-organize/reset",
        json={"qbit_hash": "h-dispatch", "trigger_now": True},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    assert body["ok"] is True
    assert body["trigger_result"]["action"] == "started"
    assert body["trigger_result"]["action_id"] == "act-trig-1"
    assert captured["called_with_hash"] == "h-dispatch"
