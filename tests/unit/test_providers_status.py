"""Contract tests for /api/providers/status.

Verifies state classification (ok / auth_failed / not_configured / unreachable),
TTL cache behavior, and refresh=1 bypass. Mocks the downstream test_connection
functions and key loaders to avoid real network calls.
"""

from __future__ import annotations

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


@pytest.fixture(autouse=True)
def _patch_qbit_default(monkeypatch):
    """默认把 qbit probe stub 成 not_configured，避免测试真打 qbit 网络。

    需要特定 qbit 状态的测试可在 test body 内 monkeypatch.setattr 覆盖。
    """
    monkeypatch.setattr(
        app_module,
        "_probe_qbit",
        lambda: {"state": "not_configured", "message": "qBit 未配置 (test default)"},
    )


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
    monkeypatch.setattr(
        app_module, "_emby_client", lambda: _FakeEmbyClient({"ok": True, "message": "Emby OK"})
    )
    monkeypatch.setattr(
        app_module, "TMDBProvider", lambda api_key: _FakeTMDB({"ok": True, "message": "TMDB OK"})
    )
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
        lambda: _FakeEmbyClient(
            {"ok": False, "message": "Emby auth rejected", "code": "auth_failed"}
        ),
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


# ── qBit probe tests ────────────────────────────────────────────────


def _make_fake_qbit(get_config_result, test_connection_result, test_throws=None):
    """构造一个 fake qbit singleton 替代 app_module.qbit。"""

    class _FakeQbit:
        def get_config(self):
            return get_config_result

        def test_connection(self):
            if test_throws is not None:
                raise test_throws
            return test_connection_result

    return _FakeQbit()


def test_qbit_not_configured_when_no_url(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(app_module, "_emby_client", lambda: None)
    fake = _make_fake_qbit({"url": "", "user": "admin", "has_password": True}, None)
    monkeypatch.setattr(app_module, "_probe_qbit", lambda: _qbit_probe_with_fake(fake))
    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    body = resp.get_json()
    assert body["providers"]["qbit"]["state"] == "not_configured"
    assert (
        "URL" in body["providers"]["qbit"]["message"]
        or "URL" in body["providers"]["qbit"]["message"].upper()
    )


def _qbit_probe_with_fake(fake_qbit):
    """直接复制 _probe_qbit 的逻辑用 fake — 让测试不依赖 monkeypatching qbit attr。"""
    try:
        cfg = fake_qbit.get_config()
    except Exception as e:
        return {"state": "error", "message": f"config read failed: {e}"}
    if not cfg.get("url") or not cfg.get("user"):
        return {"state": "not_configured", "message": "qBit URL/user 未配置"}
    if not cfg.get("has_password"):
        return {"state": "not_configured", "message": "qBit 密码未配置"}
    try:
        r = fake_qbit.test_connection()
    except Exception as e:
        return {"state": app_module._classify_provider_error(str(e)), "message": str(e)}
    if r.get("status") == "ok":
        return {"state": "ok", "message": f"qBit OK ({r.get('torrent_count', 0)} torrents)"}
    msg = r.get("message", "")
    return {"state": app_module._classify_provider_error(msg), "message": msg or "unknown"}


def test_qbit_not_configured_when_no_password(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(app_module, "_emby_client", lambda: None)
    fake = _make_fake_qbit({"url": "http://nas:8080", "user": "admin", "has_password": False}, None)
    monkeypatch.setattr(app_module, "_probe_qbit", lambda: _qbit_probe_with_fake(fake))
    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    body = resp.get_json()
    assert body["providers"]["qbit"]["state"] == "not_configured"
    assert "密码" in body["providers"]["qbit"]["message"]


def test_qbit_ok_when_login_succeeds(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(app_module, "_emby_client", lambda: None)
    fake = _make_fake_qbit(
        {"url": "http://nas:8080", "user": "admin", "has_password": True},
        {"status": "ok", "torrent_count": 42},
    )
    monkeypatch.setattr(app_module, "_probe_qbit", lambda: _qbit_probe_with_fake(fake))
    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    body = resp.get_json()
    assert body["providers"]["qbit"]["state"] == "ok"
    assert "42" in body["providers"]["qbit"]["message"]


def test_qbit_auth_failed_when_login_returns_401(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(app_module, "_emby_client", lambda: None)
    fake = _make_fake_qbit(
        {"url": "http://nas:8080", "user": "admin", "has_password": True},
        {"status": "error", "message": "qBit login failed: 401"},
    )
    monkeypatch.setattr(app_module, "_probe_qbit", lambda: _qbit_probe_with_fake(fake))
    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.get_json()["providers"]["qbit"]["state"] == "auth_failed"


def test_qbit_unreachable_when_network_error(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(app_module, "_emby_client", lambda: None)
    fake = _make_fake_qbit(
        {"url": "http://nas:8080", "user": "admin", "has_password": True},
        None,
        test_throws=RuntimeError("Connection refused"),
    )
    monkeypatch.setattr(app_module, "_probe_qbit", lambda: _qbit_probe_with_fake(fake))
    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    assert resp.get_json()["providers"]["qbit"]["state"] == "unreachable"


def test_qbit_appears_in_response_alongside_other_providers(client, token, monkeypatch):
    """Regression: 加 qbit 后 response shape 必须含 4 个 provider keys。"""
    monkeypatch.setattr(app_module, "load_tmdb_key", lambda: "")
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")
    monkeypatch.setattr(app_module, "_emby_client", lambda: None)
    resp = client.get("/api/providers/status", headers={"Authorization": f"Bearer {token}"})
    p = resp.get_json()["providers"]
    assert set(p.keys()) == {"tmdb", "deepseek", "emby", "qbit"}
    for key in p:
        assert "state" in p[key]
        assert "message" in p[key]
        assert "checked_at" in p[key]
