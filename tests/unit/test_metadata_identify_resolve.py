"""identify route 应在 validate_path 前用 path_resolver 把 QNAP 别名路径
(/share/downloads/...) 翻成 canonical，否则别名路径被沙箱字面前缀拒 (400)。"""
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


def test_identify_resolves_alias_before_validate(client, token, monkeypatch):
    calls = {}

    def fake_resolve(p, ssh_fn):
        calls["resolved_input"] = p
        # 模拟 QNAP 别名 → canonical（落进 NAS_BASE_PATH 沙箱内）
        return p.replace("/share/downloads/", f"{app_module.NAS_BASE_PATH}/downloads/", 1)

    monkeypatch.setattr(app_module.path_resolver, "resolve", fake_resolve)
    # provider=None → 走 parse-only 分支返 200，不调真实 TMDB
    monkeypatch.setattr(app_module, "get_tmdb_provider", lambda: None)

    resp = client.post(
        "/api/metadata/identify",
        json={"path": "/share/downloads/Some.Movie.2020.1080p.mkv"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200, resp.get_json()
    # resolve 收到的是原始别名路径（证明在 validate 之前调用）
    assert calls["resolved_input"] == "/share/downloads/Some.Movie.2020.1080p.mkv"
