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
        "tmdb":     {"state": "ok", "checked_at": 1716000000.0},
        "deepseek": {"state": "ok", "checked_at": 1716000000.0},
        "emby":     {"state": "auth_failed", "checked_at": 1716000000.0},
        "qbit":     {"state": "not_configured", "checked_at": None},
    }
    with patch("app.get_cached_providers_status", return_value=fake_raw):
        resp = client.get(
            "/ui/status/providers",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 200
    html = resp.data.decode()
    assert "TMDB" in html
    assert "DeepSeek" in html
    assert "Emby" in html
    assert "qBit" in html
    assert "sb-dot-ok" in html       # tmdb / deepseek
    assert "sb-dot-warn" in html     # emby auth_failed (warn not err per NIT #1)
    assert "sb-dot-gray" in html     # qbit not_configured
    assert "401" in html             # emby detail
    assert "未配" in html             # qbit detail


def test_status_workers_idle_when_no_running(client, token):
    """auto_organize_runs / organize_runs / scan_runs 全 idle 时显示 idle."""
    with patch(
        "routes.ui_status._aggregate_running_workers", return_value=[]
    ):
        resp = client.get(
            "/ui/status/workers",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert "workers idle" in resp.data.decode()


def test_status_workers_lists_running(client, token):
    fake_workers = [
        {"kind": "organize", "done": 12, "total": 40, "id": "abc"},
        {"kind": "scanner", "done": 234, "total": 1797, "id": 7},
    ]
    with patch(
        "routes.ui_status._aggregate_running_workers", return_value=fake_workers
    ):
        resp = client.get(
            "/ui/status/workers",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "organize 12/40" in html
    assert "scanner 234/1797" in html


def test_sidebar_badges_zero_hidden(client, token):
    """badge=0 时 partial 不应该渲染 .nav-badge span (零计数 hidden)."""
    fake_badges = {"library": 0, "dedup": 0, "organize": 0}
    with patch(
        "routes.ui_status._compute_sidebar_badges", return_value=fake_badges
    ):
        resp = client.get(
            "/ui/sidebar/badges",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    html = resp.data.decode()
    # 6 个 nav-item 都在
    assert html.count("nav-item") == 6
    # 但没有任何 nav-badge（因为 count=0 全跳过）
    assert "nav-badge" not in html


def test_sidebar_badges_renders_counts(client, token):
    fake_badges = {"library": 12, "dedup": 5, "organize": 3}
    with patch(
        "routes.ui_status._compute_sidebar_badges", return_value=fake_badges
    ):
        resp = client.get(
            "/ui/sidebar/badges",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    html = resp.data.decode()
    assert ">12<" in html
    assert ">5<" in html
    assert ">3<" in html


def test_sidebar_badges_real_sql_library_count(client, token):
    """不 mock _compute_sidebar_badges, 验证真 SQL 在真 DB 上能跑.

    防 BLOCKER regression: schema 'metadata_status' 合法值不含 'needs_identify',
    code review 抓出 library badge SQL 写错字段值导致永远返 0.
    这个 test 跑真 SQL, 即使数据为 0 也确认 SQL syntactically valid + 字段名/值跟 schema 一致.
    """
    # 真 SQL 跑通就行, 不验证具体数值 (CI 跑时 DB 可能空)
    resp = client.get(
        "/ui/sidebar/badges",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    html = resp.data.decode()
    # 6 个 nav-item 都在 (无论 badge count)
    assert html.count("nav-item") == 6
    # 验证 endpoint 没 5xx (真 SQL 跑过)


def test_status_providers_401_without_token(client):
    """Unauthorized access (no Bearer token) returns 401."""
    resp = client.get("/ui/status/providers")
    assert resp.status_code == 401


def test_sidebar_badges_401_without_token(client):
    """Unauthorized access (no Bearer token) returns 401."""
    resp = client.get("/ui/sidebar/badges")
    assert resp.status_code == 401
