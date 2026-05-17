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


def test_root_redirects_to_files(client, token):
    """GET / 应 302 redirect 到 /files (Phase B transitional)."""
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 308)
    assert resp.headers["Location"].endswith("/files")


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
