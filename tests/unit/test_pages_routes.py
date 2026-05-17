"""Phase B: pages_* route 契约测试 — test_client + monkeypatch boundary."""

from __future__ import annotations

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


def test_dashboard_page_renders(client, token):
    """GET / now renders dashboard (不再 redirect)."""
    resp = client.get("/", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "🏠 概览" in html
    assert "/ui/dashboard/system" in html
    assert "/ui/dashboard/workers" in html
    assert "/ui/dashboard/todo" in html


def test_organize_page_renders(client, token):
    resp = client.get("/organize", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert "📦 整理" in resp.data.decode()
    assert "autoOrganizeConfigModal" in resp.data.decode()


def test_settings_page_renders(client, token):
    resp = client.get("/settings", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "⚙️ 设置" in html
    assert "nasConfigModal" in html
    assert "qbitConfigModal" in html
    assert "aiConfigModal" in html


def test_ui_dashboard_system_401_without_token(client):
    resp = client.get("/ui/dashboard/system")
    assert resp.status_code == 401


def test_ui_dashboard_workers_renders(client, token):
    from unittest.mock import patch
    with patch("routes.ui_status._aggregate_running_workers", return_value=[]):
        resp = client.get("/ui/dashboard/workers", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert "无后台任务" in resp.data.decode()


def test_files_page_renders(client, token):
    """GET /files 应返 200 + 含 sidebar / topbar / breadcrumb / 12 modal HTML."""
    resp = client.get("/files")
    assert resp.status_code == 200
    html = resp.data.decode()
    # sidebar logo
    assert "🗄️ NASVault" in html
    # breadcrumb (注入到 base.html topbar)
    assert "📁 文件" in html
    # 12 modal HTML 全保留 (来自 _legacy_modals.html include)
    assert html.count('class="modal fade"') == 12
    # Bootstrap + app.js 都引 (来自 base.html)
    assert "bootstrap@5.3.0/dist/css" in html
    assert "bootstrap@5.3.0/dist/js" in html
    assert "static/app.js" in html


def test_library_page_renders(client, token):
    """/library 应返 200 + 含 library-panel div + 12 legacy modal + 面包屑。"""
    resp = client.get("/library")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "🎬 媒体库" in html
    assert "library-panel" in html  # library_view partial 含 id="library-panel"
    assert html.count('class="modal fade"') == 12  # legacy modals included


def test_dedup_page_renders(client, token):
    """/dedup 应返 200 + 含 dedup-panel div + 12 legacy modal + 面包屑。"""
    resp = client.get("/dedup")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "🔍 重复检测" in html
    assert "dedup-panel" in html  # dedup_view partial 含 id="dedup-panel"
    assert html.count('class="modal fade"') == 12  # legacy modals included


def test_files_page_no_other_views(client, token):
    """/files page 不该含 library-panel / dedup-panel (它们在各自专属 page)。"""
    resp = client.get("/files")
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "file-container" in html   # files view 仍有 file-container
    assert "library-panel" not in html
    assert "dedup-panel" not in html


def test_sidebar_library_active(client, token):
    """/library page sidebar 的 /library 导航项含 active class。"""
    import re
    resp = client.get("/library")
    html = resp.data.decode()
    # 匹配 <a href="/library" ... class="... active ..."
    m = re.search(r'<a href="/library"[^>]*class="([^"]+)"', html)
    assert m is not None, "sidebar /library link not found"
    assert "active" in m.group(1), f"active not in class: {m.group(1)}"
