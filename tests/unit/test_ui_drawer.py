"""Phase C: /ui/drawer/* HTML fragment endpoints — contract tests.

Tests auth (401), input validation (400/404), and response content.
Internal _do_action_preview / action_confirm are monkeypatched at boundary.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


@pytest.fixture
def client():
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def token():
    import app as app_module

    return app_module.API_TOKEN


# ---------------------------------------------------------------------------
# Auth tests — all endpoints return 401 without token
# ---------------------------------------------------------------------------


def test_delete_preview_401_without_token(client):
    resp = client.post("/ui/drawer/delete/preview", json={"paths": ["/x.mkv"]})
    assert resp.status_code == 401


def test_organize_preview_401_without_token(client):
    resp = client.post("/ui/drawer/organize/preview", json={"items": []})
    assert resp.status_code == 401


def test_nfo_preview_401_without_token(client):
    resp = client.post("/ui/drawer/nfo_write/preview", json={})
    assert resp.status_code == 401


def test_action_confirm_401_without_token(client):
    resp = client.post(
        "/ui/drawer/delete/confirm",
        json={"action_id": "abc", "signed_token": "tok"},
    )
    assert resp.status_code == 401


def test_action_status_401_without_token(client):
    resp = client.get("/ui/drawer/action/abc123/status")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_delete_preview_empty_paths_returns_error_fragment(client, token):
    resp = client.post(
        "/ui/drawer/delete/preview",
        json={"paths": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "无文件选中" in resp.data.decode("utf-8")


def test_action_status_404_for_unknown_action(client, token):
    resp = client.get(
        "/ui/drawer/action/nonexistent_abc123/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404
    assert b"action not found" in resp.data


# ---------------------------------------------------------------------------
# delete_preview success path (monkeypatched _do_action_preview)
# ---------------------------------------------------------------------------


def _make_preview_response(app_module, data: dict):
    """Return a Flask JSON Response like _do_action_preview does on success."""
    from flask import Response

    payload = json.dumps(data)
    r = Response(payload, status=200, mimetype="application/json")
    return r


def test_delete_preview_renders_html_fragment(client, token):
    import app as app_module

    fake_result = {
        "action_id": "act_test_001",
        "signed_token": "tok_test",
        "files": [{"path": "/data/movie.mkv"}],
        "total_size": 1234567,
    }

    with patch.object(
        app_module,
        "_do_action_preview",
        return_value=_make_preview_response(app_module, fake_result),
    ):
        resp = client.post(
            "/ui/drawer/delete/preview",
            json={"paths": ["/data/movie.mkv"]},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.data.decode()
    assert "act_test_001" in body
    assert "删除预览" in body
    assert "movie.mkv" in body


def test_organize_preview_renders_html_fragment(client, token):
    import app as app_module

    fake_result = {
        "action_id": "act_org_001",
        "signed_token": "tok_org",
        "items": [{"src": "/data/a.mkv", "dst": "/sorted/a.mkv"}],
    }

    with patch.object(
        app_module,
        "_do_action_preview",
        return_value=_make_preview_response(app_module, fake_result),
    ):
        resp = client.post(
            "/ui/drawer/organize/preview",
            json={"items": [{"path": "/data/a.mkv"}]},
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    body = resp.data.decode()
    assert "整理预览" in body
    assert "act_org_001" in body
