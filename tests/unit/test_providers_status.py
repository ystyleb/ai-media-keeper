"""Contract tests for /api/providers/status.

Verifies state classification (ok / auth_failed / not_configured / unreachable),
TTL cache behavior, and refresh=1 bypass. Mocks the downstream test_connection
functions and key loaders to avoid real network calls.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def token():
    return app_module.API_TOKEN


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the in-memory providers status cache between tests."""
    app_module._PROVIDERS_STATUS_CACHE["data"] = None
    app_module._PROVIDERS_STATUS_CACHE["checked_at"] = 0.0
    yield
    app_module._PROVIDERS_STATUS_CACHE["data"] = None
    app_module._PROVIDERS_STATUS_CACHE["checked_at"] = 0.0


class _FakeTMDB:
    def __init__(self, result):
        self._result = result

    def test_connection(self):
        return self._result


class _FakeEmbyClient:
    def __init__(self, result):
        self._result = result

    def test_connection(self):
        return self._result


def _patch_openai_ok(monkeypatch):
    """Patch openai SDK so DeepSeek probe succeeds without network."""
    class _Resp:
        choices = [type("Choice", (), {"message": type("Msg", (), {"content": "pong"})})]

    class _Completions:
        def create(self, **kwargs):
            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Client:
        def __init__(self, *a, **kw):
            self.chat = _Chat()

    import openai
    monkeypatch.setattr(openai, "OpenAI", _Client)


def _patch_openai_authfail(monkeypatch):
    class _Client:
        def __init__(self, *a, **kw):
            self.chat = self

        @property
        def completions(self):
            return self

        def create(self, **kwargs):
            raise RuntimeError("401 Unauthorized: invalid API key")

    import openai
    monkeypatch.setattr(openai, "OpenAI", _Client)


def test_all_three_ok(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "tk")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "dk")
    monkeypatch.setattr(app_module, "_emby_client", lambda: _FakeEmbyClient({"ok": True, "message": "Emby OK"}))
    monkeypatch.setattr(app_module, "TMDBProvider", lambda api_key: _FakeTMDB({"ok": True, "message": "TMDB OK"}))
    _patch_openai_ok(monkeypatch)

    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.get_json()
    p = body["providers"]
    assert p["tmdb"]["state"] == "ok"
    assert p["deepseek"]["state"] == "ok"
    assert p["emby"]["state"] == "ok"
    assert body["cached"] is False


def test_tmdb_auth_failed_message_pattern(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "bad")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(app_module, "_emby_client", lambda: None)
    monkeypatch.setattr(
        app_module,
        "TMDBProvider",
        lambda api_key: _FakeTMDB({"ok": False, "message": "401 Unauthorized: Invalid API key"}),
    )

    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    body = resp.get_json()
    assert body["providers"]["tmdb"]["state"] == "auth_failed"
    assert body["providers"]["deepseek"]["state"] == "not_configured"
    assert body["providers"]["emby"]["state"] == "not_configured"


def test_tmdb_network_classified_as_unreachable(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "tk")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(app_module, "_emby_client", lambda: None)
    monkeypatch.setattr(
        app_module,
        "TMDBProvider",
        lambda api_key: _FakeTMDB({"ok": False, "message": "network: Connection refused"}),
    )

    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.get_json()["providers"]["tmdb"]["state"] == "unreachable"


def test_deepseek_not_configured_when_key_missing(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(app_module, "_emby_client", lambda: None)

    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    p = resp.get_json()["providers"]
    assert p["deepseek"]["state"] == "not_configured"
    assert p["tmdb"]["state"] == "not_configured"


def test_deepseek_auth_failure_classified(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "bad")
    monkeypatch.setattr(app_module, "_emby_client", lambda: None)
    _patch_openai_authfail(monkeypatch)

    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.get_json()["providers"]["deepseek"]["state"] == "auth_failed"


def test_emby_auth_failed_code_respected(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(
        app_module,
        "_emby_client",
        lambda: _FakeEmbyClient({"ok": False, "message": "Emby auth rejected", "code": "auth_failed"}),
    )

    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.get_json()["providers"]["emby"]["state"] == "auth_failed"


def test_emby_network_code_classified_unreachable(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(
        app_module,
        "_emby_client",
        lambda: _FakeEmbyClient({"ok": False, "message": "network: timed out", "code": "network"}),
    )

    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.get_json()["providers"]["emby"]["state"] == "unreachable"


def test_cache_hit_second_call_does_not_re_probe(client, token, monkeypatch):
    calls = {"tmdb": 0}

    def _fake_tmdb_factory(api_key):
        calls["tmdb"] += 1
        return _FakeTMDB({"ok": True, "message": "TMDB OK"})

    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "tk")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(app_module, "_emby_client", lambda: None)
    monkeypatch.setattr(app_module, "TMDBProvider", _fake_tmdb_factory)

    r1 = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    r2 = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    assert r1.get_json()["cached"] is False
    assert r2.get_json()["cached"] is True
    assert calls["tmdb"] == 1


def test_refresh_param_bypasses_cache(client, token, monkeypatch):
    calls = {"tmdb": 0}

    def _fake_tmdb_factory(api_key):
        calls["tmdb"] += 1
        return _FakeTMDB({"ok": True, "message": "TMDB OK"})

    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "tk")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(app_module, "_emby_client", lambda: None)
    monkeypatch.setattr(app_module, "TMDBProvider", _fake_tmdb_factory)

    client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    r2 = client.get("/api/providers/status?refresh=1", headers={"Authorization": f"Bearer {token}"})
    assert r2.get_json()["cached"] is False
    assert calls["tmdb"] == 2


def test_response_has_no_store(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(app_module, "_emby_client", lambda: None)
    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    assert "no-store" in resp.headers.get("Cache-Control", "").lower()
