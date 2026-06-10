"""Phase D: /onboarding wizard — contract tests."""

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


def test_onboarding_root_renders_nas_step(client, token):
    """GET /onboarding 默认显示 nas step."""
    resp = client.get("/onboarding", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Step 1: NAS SSH" in html
    assert "欢迎" in html


def test_onboarding_nas_step_renders(client, token):
    """GET /onboarding/step/nas 渲染 nas step."""
    resp = client.get("/onboarding/step/nas", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "Step 1: NAS SSH" in html
    assert "欢迎" in html


def test_onboarding_redirects_unknown_step_to_nas(client, token):
    """未知 step 302 redirect 到 nas."""
    resp = client.get(
        "/onboarding/step/invalid",
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=False,
    )
    assert resp.status_code in (302, 308)
    location = resp.headers.get("Location", "")
    assert "nas" in location


def test_onboarding_done_step_shows_unconfigured(client, token):
    """Done step 展示 4 必填 status — qbit 未配置时显示警告."""
    fake_status = {
        "nas_ssh": "ok",
        "qbit": "unconfigured",
        "tmdb_key": "ok",
        "deepseek_key": "ok",
        "library_roots": "unconfigured",
        "emby": "unconfigured",
    }
    with patch("services.onboarding.check_status", return_value=fake_status):
        resp = client.get(
            "/onboarding/step/done",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    html = resp.data.decode()
    assert "仍有未配置项" in html
    assert "qBittorrent" in html


def test_onboarding_done_step_shows_complete(client, token):
    """Done step: 4 必填全 ok 时显示 '设置完成'."""
    fake_status = {
        "nas_ssh": "ok",
        "qbit": "ok",
        "tmdb_key": "ok",
        "deepseek_key": "ok",
        "library_roots": "unconfigured",
        "emby": "unconfigured",
    }
    with patch("services.onboarding.check_status", return_value=fake_status):
        resp = client.get(
            "/onboarding/step/done",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    assert "设置完成" in resp.data.decode()


def test_is_onboarded_when_all_four_ok():
    """is_onboarded() True when 4 required keys present."""
    from services.onboarding import is_onboarded

    fake = {
        "nas_ssh": "ok",
        "qbit": "ok",
        "tmdb_key": "ok",
        "deepseek_key": "ok",
        "library_roots": "unconfigured",
        "emby": "unconfigured",
    }
    with patch("services.onboarding.check_status", return_value=fake):
        assert is_onboarded() is True


def test_is_onboarded_false_when_any_missing():
    """is_onboarded() False when any of 4 required keys unconfigured."""
    from services.onboarding import is_onboarded

    fake = {
        "nas_ssh": "ok",
        "qbit": "unconfigured",
        "tmdb_key": "ok",
        "deepseek_key": "ok",
        "library_roots": "unconfigured",
        "emby": "unconfigured",
    }
    with patch("services.onboarding.check_status", return_value=fake):
        assert is_onboarded() is False
