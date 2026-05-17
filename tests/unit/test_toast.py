"""Phase E: Toast helper + HX-Trigger header convention."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


def test_add_toast_sets_hx_trigger_header():
    import app as app_module
    from flask import make_response
    from services.toast import add_toast

    with app_module.app.app_context():
        resp = make_response("ok")
        add_toast(resp, "warning", "TMDB key 失效")

        trigger = resp.headers.get("HX-Trigger")
        assert trigger is not None
        payload = json.loads(trigger)
        assert payload["toast"]["severity"] == "warning"
        assert payload["toast"]["message"] == "TMDB key 失效"


def test_add_toast_returns_response_for_chaining():
    import app as app_module
    from flask import make_response
    from services.toast import add_toast

    with app_module.app.app_context():
        resp = make_response("ok")
        result = add_toast(resp, "info", "msg")
        assert result is resp


def test_status_providers_emits_toast_on_auth_failed(client, token):
    """Provider auth_failed → response 含 HX-Trigger toast warning."""
    fake_raw = {
        "tmdb": {"state": "ok", "checked_at": 1.0},
        "deepseek": {"state": "ok", "checked_at": 1.0},
        "emby": {"state": "auth_failed", "checked_at": 1.0},
        "qbit": {"state": "ok", "checked_at": 1.0},
    }
    with patch("app.get_cached_providers_status", return_value=fake_raw):
        resp = client.get("/ui/status/providers", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    trigger = resp.headers.get("HX-Trigger")
    assert trigger is not None
    payload = json.loads(trigger)
    assert payload["toast"]["severity"] == "warning"
    assert "Emby" in payload["toast"]["message"]


def test_status_providers_no_toast_when_all_ok(client, token):
    """All providers ok → no HX-Trigger."""
    fake_raw = {
        "tmdb": {"state": "ok", "checked_at": 1.0},
        "deepseek": {"state": "ok", "checked_at": 1.0},
        "emby": {"state": "ok", "checked_at": 1.0},
        "qbit": {"state": "ok", "checked_at": 1.0},
    }
    with patch("app.get_cached_providers_status", return_value=fake_raw):
        resp = client.get("/ui/status/providers", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") is None


@pytest.fixture
def client():
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def token():
    import app as app_module
    return app_module.API_TOKEN
