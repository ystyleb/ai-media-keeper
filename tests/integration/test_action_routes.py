"""Integration tests: Flask routes + destructive_action contract end-to-end.

SSH 和 qBit 都被 monkeypatch 成 fake，验证契约 #1 + #2 在 routes 层正确串通。
覆盖 plan v3 1.7 验收清单未被单元测试覆盖的项：
- #2 端到端 UI 流程（test client 跑 preview → confirm）
- #7 Legacy 410（curl /api/delete 不带 token → 410）
- #8 file-browser snapshot mismatch
- #9 inode-anchored execute
"""

from __future__ import annotations

import os
import sys
import json
from pathlib import Path

import pytest


@pytest.fixture(scope="module", autouse=True)
def _set_app_env(tmp_path_factory):
    """Override DB / config dir to module-scoped tmp so test runs are isolated."""
    tmpdir = tmp_path_factory.mktemp("nas-app")
    os.environ["NAS_CONFIG_DIR_OVERRIDE"] = str(tmpdir)
    # 早于 app.py import 设置；conftest.pytest_configure 已设核心 env
    yield


@pytest.fixture
def client(monkeypatch):
    """Flask test client + monkeypatch SSH/qBit so no real network calls."""
    # late import to honor test env
    sys.modules.pop("app", None)
    import app

    # Fake SSH stat returning fixed inode/size/mtime
    def fake_ssh_stat_paths(paths):
        return {
            p: {
                "exists": True,
                "inode": 1000 + i,
                "size_bytes": 1024 * (i + 1),
                "mtime": 1_700_000_000,
                "is_dir": p.endswith("/"),
            }
            for i, p in enumerate(paths)
        }

    def fake_resolve_real_paths(paths):
        return {p: p for p in paths}

    def fake_resolve_all_hardlink_paths(paths):
        return {p: {"inode": 1000 + i, "size": 1024 * (i + 1), "all_paths": [p]} for i, p in enumerate(paths)}

    def fake_get_real_sizes(paths):
        return {p: 1024 * (i + 1) for i, p in enumerate(paths)}

    def fake_enumerate_dir_files(*a, **kw):
        return []

    def fake_reject_base_path(paths):
        pass

    def fake_validate_path(p):
        return p

    class FakeQbit:
        def find_torrents_by_paths(self, paths):
            return []

        def delete_torrents(self, hashes, delete_files):
            return None

    monkeypatch.setattr(app, "_ssh_stat_paths", fake_ssh_stat_paths)
    monkeypatch.setattr(app, "_resolve_real_paths", fake_resolve_real_paths)
    monkeypatch.setattr(app, "_resolve_all_hardlink_paths", fake_resolve_all_hardlink_paths)
    monkeypatch.setattr(app, "_get_real_sizes", fake_get_real_sizes)
    monkeypatch.setattr(app, "_enumerate_dir_files", fake_enumerate_dir_files)
    monkeypatch.setattr(app, "_reject_base_path", fake_reject_base_path)
    monkeypatch.setattr(app, "validate_path", fake_validate_path)
    monkeypatch.setattr(app, "qbit", FakeQbit())

    # delete executor's SSH calls also need to be neutered
    def fake_ssh_exec(cmd, timeout=30):
        # 模拟成功删除：rm -rf 返回 OK；find -inum 返回模拟匹配的路径
        if "find " in cmd and "-inum" in cmd:
            # 提取 inode；返回模拟"被删除的 path 列表"
            import re
            m = re.search(r"-inum (\d+)", cmd)
            inum = m.group(1) if m else "0"
            return 0, f"/share/test/inode-{inum}-anchor\n", ""
        if "rm -rf" in cmd:
            return 0, "OK", ""
        return 0, "", ""

    monkeypatch.setattr(app, "ssh_exec", fake_ssh_exec)

    app.app.config["TESTING"] = True
    with app.app.test_client() as c:
        yield c, app


AUTH = {"Authorization": "Bearer test-token-must-be-at-least-16-chars-long-padding"}


# ─── Legacy 410 ──────────────────────────────────────────────


def test_legacy_delete_returns_410(client):
    c, _ = client
    rv = c.post("/api/delete", json={"files": ["/share/test/x"]}, headers=AUTH)
    assert rv.status_code == 410
    body = rv.get_json()
    assert body["error"] == "deprecated"
    assert "use_preview" in body


def test_legacy_delete_complete_returns_410(client):
    c, _ = client
    rv = c.post(
        "/api/delete-complete",
        json={"files": ["/share/test/x"], "delete_torrents": True},
        headers=AUTH,
    )
    assert rv.status_code == 410


# ─── Preview ────────────────────────────────────────────────


def test_preview_via_action_route(client):
    c, _ = client
    rv = c.post(
        "/api/action/preview",
        json={"kind": "delete", "candidates": [{"path": "/share/test/a.mkv"}]},
        headers=AUTH,
    )
    assert rv.status_code == 200
    data = rv.get_json()
    assert data["action_id"]
    assert data["signed_token"]
    assert data["expires_at"] > 0
    assert data["kind"] == "delete"
    assert len(data["files"]) == 1


def test_preview_via_legacy_alias_returns_token(client):
    """delete-preview alias 必须返回 action_id + signed_token（不再是旧 shape）。"""
    c, _ = client
    rv = c.post(
        "/api/delete-preview",
        json={"files": ["/share/test/a.mkv"]},
        headers=AUTH,
    )
    assert rv.status_code == 200
    data = rv.get_json()
    assert "action_id" in data
    assert "signed_token" in data


def test_preview_missing_kind_400(client):
    c, _ = client
    rv = c.post("/api/action/preview", json={"candidates": []}, headers=AUTH)
    assert rv.status_code == 400


def test_preview_unknown_kind_400(client):
    c, _ = client
    rv = c.post(
        "/api/action/preview",
        json={"kind": "drop_database", "candidates": [{"path": "/share/x"}]},
        headers=AUTH,
    )
    assert rv.status_code == 400


def test_preview_without_auth_401(client):
    c, _ = client
    rv = c.post(
        "/api/action/preview",
        json={"kind": "delete", "candidates": [{"path": "/share/x"}]},
    )
    assert rv.status_code == 401


# ─── Confirm ────────────────────────────────────────────────


def test_full_preview_confirm_happy_path(client):
    c, _ = client
    pv = c.post(
        "/api/action/preview",
        json={"kind": "delete", "candidates": [{"path": "/share/test/a.mkv"}]},
        headers=AUTH,
    ).get_json()

    rv = c.post(
        "/api/action/confirm",
        json={"action_id": pv["action_id"], "signed_token": pv["signed_token"]},
        headers=AUTH,
    )
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["status"] == "succeeded"
    assert body["result"]["total_files_deleted"] >= 1


def test_confirm_replay_returns_409(client):
    c, _ = client
    pv = c.post(
        "/api/action/preview",
        json={"kind": "delete", "candidates": [{"path": "/share/test/a.mkv"}]},
        headers=AUTH,
    ).get_json()
    c.post("/api/action/confirm",
           json={"action_id": pv["action_id"], "signed_token": pv["signed_token"]},
           headers=AUTH)
    # replay
    rv = c.post("/api/action/confirm",
                json={"action_id": pv["action_id"], "signed_token": pv["signed_token"]},
                headers=AUTH)
    assert rv.status_code == 409


def test_confirm_bad_token_returns_401(client):
    c, _ = client
    pv = c.post(
        "/api/action/preview",
        json={"kind": "delete", "candidates": [{"path": "/share/test/a.mkv"}]},
        headers=AUTH,
    ).get_json()
    rv = c.post("/api/action/confirm",
                json={"action_id": pv["action_id"], "signed_token": "deadbeef" * 8},
                headers=AUTH)
    assert rv.status_code == 401


def test_confirm_unknown_action_404(client):
    c, _ = client
    rv = c.post("/api/action/confirm",
                json={"action_id": "no-such-id", "signed_token": "x"},
                headers=AUTH)
    assert rv.status_code == 404


def test_snapshot_mismatch_returns_target_already_changed(client, monkeypatch):
    """File-browser path: preview 之后 ground truth 改了 → confirm 返回 target_already_changed。"""
    c, app_mod = client
    pv = c.post(
        "/api/action/preview",
        json={"kind": "delete", "candidates": [{"path": "/share/test/a.mkv"}]},
        headers=AUTH,
    ).get_json()

    # 切换 fake stat 返回不同的 mtime（模拟文件被改过）
    def stat_after_change(paths):
        return {p: {
            "exists": True, "inode": 1000, "size_bytes": 1024,
            "mtime": 1_700_000_999,  # 不同！
            "is_dir": False,
        } for p in paths}

    monkeypatch.setattr(app_mod, "_ssh_stat_paths", stat_after_change)

    rv = c.post("/api/action/confirm",
                json={"action_id": pv["action_id"], "signed_token": pv["signed_token"]},
                headers=AUTH)
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["status"] == "target_already_changed"
    assert body["hint"]


# ─── Recovery view ─────────────────────────────────────────


def test_recovery_endpoint_returns_actions(client):
    c, _ = client
    # 故意建一个 action，但不 confirm，让它落 pending
    c.post(
        "/api/action/preview",
        json={"kind": "delete", "candidates": [{"path": "/share/test/a.mkv"}]},
        headers=AUTH,
    )
    rv = c.get("/api/action/recovery", headers=AUTH)
    assert rv.status_code == 200
    # pending 的 action 不会出现在 recovery view（filter 只看 needs_manual_recovery/running/failed）
    # 所以这里只验证端点能返回 JSON
    assert isinstance(rv.get_json()["actions"], list)
