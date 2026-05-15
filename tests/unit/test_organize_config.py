"""Phase 4A.5: /api/config/organize GET/POST + /test contract tests.

覆盖 movies_root / tv_root 配置 CRUD + SSH stat 测试目录 + 路径校验。
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    # 用临时 organize.json 避免污染真实配置
    fake_cfg_file = tmp_path / "organize.json"
    monkeypatch.setattr(app_module, "ORGANIZE_CONFIG_FILE", fake_cfg_file)
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def token():
    return app_module.API_TOKEN


# ── GET /api/config/organize ───────────────────────────────────


def test_get_empty_returns_configured_false(client, token):
    resp = client.get(
        "/api/config/organize",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["movies_root"] == ""
    assert body["tv_root"] == ""
    assert body["configured"] is False


def test_get_after_save_returns_configured_true(client, token):
    client.post(
        "/api/config/organize",
        json={"movies_root": "/a/movies", "tv_root": "/a/tv"},
        headers={"Authorization": f"Bearer {token}"},
    )
    resp = client.get(
        "/api/config/organize",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    assert body["movies_root"] == "/a/movies"
    assert body["tv_root"] == "/a/tv"
    assert body["configured"] is True


# ── POST /api/config/organize ──────────────────────────────────


def test_post_missing_movies_root_returns_400(client, token):
    resp = client.post(
        "/api/config/organize",
        json={"tv_root": "/a/tv"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "required" in resp.get_json()["message"]


def test_post_missing_tv_root_returns_400(client, token):
    resp = client.post(
        "/api/config/organize",
        json={"movies_root": "/a/movies"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_post_relative_path_rejected(client, token):
    resp = client.post(
        "/api/config/organize",
        json={"movies_root": "media/movies", "tv_root": "/a/tv"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "absolute" in resp.get_json()["message"]


def test_post_both_relative_rejected(client, token):
    resp = client.post(
        "/api/config/organize",
        json={"movies_root": "movies", "tv_root": "tv"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_post_happy_path_persists(client, token):
    resp = client.post(
        "/api/config/organize",
        json={"movies_root": "/share/media/movies", "tv_root": "/share/media/tv"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["configured"] is True
    # 再 GET 一次确认落盘
    g = client.get(
        "/api/config/organize",
        headers={"Authorization": f"Bearer {token}"},
    ).get_json()
    assert g["movies_root"] == "/share/media/movies"
    assert g["tv_root"] == "/share/media/tv"


def test_post_strips_whitespace(client, token):
    resp = client.post(
        "/api/config/organize",
        json={"movies_root": "  /a/movies  ", "tv_root": "/a/tv\n"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    g = client.get(
        "/api/config/organize",
        headers={"Authorization": f"Bearer {token}"},
    ).get_json()
    assert g["movies_root"] == "/a/movies"
    assert g["tv_root"] == "/a/tv"


# ── POST /api/config/organize/test ─────────────────────────────


def test_test_both_dirs_writable_returns_ok(client, token, monkeypatch):
    """两个 root 都存在 + writable → ok=True."""
    monkeypatch.setattr(app_module, "ssh_exec", lambda cmd, timeout=10: (0, "", ""))
    resp = client.post(
        "/api/config/organize/test",
        json={"movies_root": "/a/m", "tv_root": "/a/t"},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    assert body["ok"] is True
    assert body["movies_ok"] is True
    assert body["tv_ok"] is True


def test_test_movies_missing_returns_partial_failure(client, token, monkeypatch):
    """movies dir 不存在但 tv ok → movies_ok=False, tv_ok=True, ok=False."""
    def fake_ssh(cmd, timeout=10):
        if "/a/missing" in cmd:
            return (1, "", "")
        return (0, "", "")
    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh)
    resp = client.post(
        "/api/config/organize/test",
        json={"movies_root": "/a/missing", "tv_root": "/a/t"},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    assert body["movies_ok"] is False
    assert body["tv_ok"] is True
    assert body["ok"] is False


def test_test_uses_saved_config_when_body_missing(client, token, monkeypatch):
    """body 不传 path 时用已落盘的配置去测。"""
    # 先 save
    client.post(
        "/api/config/organize",
        json={"movies_root": "/saved/m", "tv_root": "/saved/t"},
        headers={"Authorization": f"Bearer {token}"},
    )
    seen_cmds = []
    def fake_ssh(cmd, timeout=10):
        seen_cmds.append(cmd)
        return (0, "", "")
    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh)
    resp = client.post(
        "/api/config/organize/test",
        json={},   # 空 body
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    # 两个 SSH 命令必须包含 saved 的 path
    assert any("/saved/m" in c for c in seen_cmds)
    assert any("/saved/t" in c for c in seen_cmds)


def test_test_missing_both_returns_400(client, token):
    """既没 saved config 也没 body 传 path → 400."""
    resp = client.post(
        "/api/config/organize/test",
        json={},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_test_ssh_uses_test_d_and_w(client, token, monkeypatch):
    """SSH 命令必须用 `test -d <path> -a -w <path>` 验证目录+可写。"""
    seen_cmds = []
    def fake_ssh(cmd, timeout=10):
        seen_cmds.append(cmd)
        return (0, "", "")
    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh)
    client.post(
        "/api/config/organize/test",
        json={"movies_root": "/a/m", "tv_root": "/a/t"},
        headers={"Authorization": f"Bearer {token}"},
    )
    for c in seen_cmds:
        assert "test -d" in c, f"cmd should use test -d: {c}"
        assert "-w" in c, f"cmd should check writable: {c}"
