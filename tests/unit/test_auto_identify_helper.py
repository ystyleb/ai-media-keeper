"""cron 自动识别 callback：_identify_and_cache 用传入 conn（线程安全，不用 get_db），
区分 identified / provider_unavailable；_auto_identify_paths 聚合多 path。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import app as app_module
from db import migrations
from services import destructive_action
from services.metadata.base import ProviderUnavailable


@pytest.fixture
def conn(tmp_path):
    import pathlib

    db_path = tmp_path / "t.db"
    c = destructive_action.open_connection(db_path)
    destructive_action.init_schema(
        c, pathlib.Path(__file__).resolve().parents[2] / "db" / "schema.sql"
    )
    migrations.phase3_migrate(c)
    migrations.phase4_migrate(c)
    migrations.phase5_migrate(c)
    yield c
    c.close()


def test_identify_and_cache_provider_unavailable(conn, monkeypatch):
    monkeypatch.setattr(app_module, "get_tmdb_provider", lambda: MagicMock())
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")

    def boom(*a, **k):
        raise ProviderUnavailable("429")

    monkeypatch.setattr(app_module.identify_svc, "identify", boom)

    out = app_module._identify_and_cache(conn, "/share/CACHEDEV2_DATA/downloads/m.mkv")
    assert out["provider_unavailable"] is True
    assert out["identified"] is False


def test_identify_and_cache_no_provider(conn, monkeypatch):
    monkeypatch.setattr(app_module, "get_tmdb_provider", lambda: None)
    out = app_module._identify_and_cache(conn, "/x/m.mkv")
    assert out == {"identified": False, "provider_unavailable": False}


def test_auto_identify_paths_short_circuits_on_unavailable(conn, monkeypatch):
    calls = []

    def fake(_conn, p):
        calls.append(p)
        return {"identified": False, "provider_unavailable": True}

    monkeypatch.setattr(app_module, "_identify_and_cache", fake)
    out = app_module._auto_identify_paths(conn, ["/a", "/b"])
    assert out["provider_unavailable"] is True
    assert calls == ["/a"]  # 第一个就 unavailable → 短路，不继续
