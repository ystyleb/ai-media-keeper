"""Phase A: /ui/status/* + /ui/sidebar/badges 路由契约测试.

模式：test_client + monkeypatch 后端 boundary (rule: testing-and-fixtures.md
"Flask route 测试用 test_client + monkeypatch backend boundary")
"""

from __future__ import annotations

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


def test_status_providers_renders_4_segments(client, token):
    fake_raw = {
        "tmdb":     {"state": "ok", "last_check": "16:42"},
        "deepseek": {"state": "ok", "last_check": "16:42"},
        "emby":     {"state": "auth_failed", "last_check": "16:42"},
        "qbit":     {"state": "not_configured", "last_check": None},
    }
    with patch("app._compute_providers_status", return_value=fake_raw):
        resp = client.get("/ui/status/providers")

    assert resp.status_code == 200
    html = resp.data.decode()
    assert "TMDB" in html
    assert "DeepSeek" in html
    assert "Emby" in html
    assert "qBit" in html
    assert "sb-dot-ok" in html       # tmdb / deepseek
    assert "sb-dot-err" in html      # emby auth_failed
    assert "sb-dot-gray" in html     # qbit not_configured
    assert "401" in html             # emby detail
    assert "未配" in html             # qbit detail


def test_status_workers_idle_when_no_running(client):
    """auto_organize_runs / organize_runs / scan_runs 全 idle 时显示 idle."""
    with patch(
        "routes.ui_status._aggregate_running_workers", return_value=[]
    ):
        resp = client.get("/ui/status/workers")
    assert resp.status_code == 200
    assert "workers idle" in resp.data.decode()


def test_status_workers_lists_running(client):
    fake_workers = [
        {"kind": "organize", "done": 12, "total": 40, "id": "abc"},
        {"kind": "scanner", "done": 234, "total": 1797, "id": 7},
    ]
    with patch(
        "routes.ui_status._aggregate_running_workers", return_value=fake_workers
    ):
        resp = client.get("/ui/status/workers")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "organize 12/40" in html
    assert "scanner 234/1797" in html


def test_sidebar_badges_zero_hidden(client):
    """badge=0 时 partial 不应该渲染 .nav-badge span (零计数 hidden)."""
    fake_badges = {"library": 0, "dedup": 0, "organize": 0}
    with patch(
        "routes.ui_status._compute_sidebar_badges", return_value=fake_badges
    ):
        resp = client.get("/ui/sidebar/badges")
    assert resp.status_code == 200
    html = resp.data.decode()
    # 6 个 nav-item 都在
    assert html.count("nav-item") == 6
    # 但没有任何 nav-badge（因为 count=0 全跳过）
    assert "nav-badge" not in html


def test_sidebar_badges_renders_counts(client):
    fake_badges = {"library": 12, "dedup": 5, "organize": 3}
    with patch(
        "routes.ui_status._compute_sidebar_badges", return_value=fake_badges
    ):
        resp = client.get("/ui/sidebar/badges")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert ">12<" in html
    assert ">5<" in html
    assert ">3<" in html
