"""Phase 4A.3: /api/action/preview + /api/action/confirm kind='organize' contract tests.

Test route validation chain + cache lookup + plan computation + dst checks.
SSH boundary mocked via _ssh_stat_paths / _ssh_mkdir_p / _ssh_ln so no real
NAS connection required. Reads use real DB via get_db (in-memory sqlite for
test isolation).

This file covers Day 2 (preview 10 cases) — Day 3 confirm cases append later.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass
class _CachedStub:
    """Stand-in for services.metadata_cache.CachedMetadata."""
    title: str
    media_type: str
    year: int | None = None
    tmdb_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    inode: int | None = None
    mtime: int | None = None


def _src_stat(path: str, inode: int = 100, size: int = 1024, mtime: int = 1000) -> dict:
    return {path: {"exists": True, "inode": inode, "size_bytes": size, "mtime": mtime}}


def _dst_stat_missing(paths: list[str]) -> dict:
    return {p: {"exists": False} for p in paths}


def _patch_organize_config(monkeypatch, movies_root="/media/movies", tv_root="/media/tv"):
    monkeypatch.setattr(
        app_module, "load_organize_config",
        lambda: {"movies_root": movies_root, "tv_root": tv_root},
    )


def _patch_cache(monkeypatch, cached_or_none):
    """Patch metadata_cache.get_by_path to return (cached, 'hit')."""
    monkeypatch.setattr(
        app_module.metadata_cache, "get_by_path",
        lambda conn, path, current_mtime=None, current_inode=None: (cached_or_none, "hit"),
    )


# ── 10 preview contract tests ──────────────────────────────────


def test_preview_missing_roots_returns_400(client, token, monkeypatch):
    """配置缺 movies_root/tv_root → 400 organize_roots_not_configured."""
    monkeypatch.setattr(app_module, "load_organize_config", lambda: {})
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/x.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "organize_roots_not_configured"


def test_preview_no_items_returns_400(client, token, monkeypatch):
    _patch_organize_config(monkeypatch)
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "items required" in resp.get_json()["error"]


def test_preview_src_missing_returns_400(client, token, monkeypatch):
    """SSH stat 说文件不存在 → 400 src_missing."""
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        lambda paths: {p: {"exists": False} for p in paths},
    )
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/missing.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "src_missing"
    assert body["src_path"] == "/dl/missing.mkv"


def test_preview_src_not_identified_returns_400(client, token, monkeypatch):
    """cache miss → 400 src_not_identified."""
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        lambda paths: _src_stat("/dl/x.mkv"),
    )
    _patch_cache(monkeypatch, None)
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/x.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "src_not_identified"


def test_preview_tv_missing_episode_returns_400(client, token, monkeypatch):
    """TV 但 episode_number=None → OrganizeNotApplicable → 400."""
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(app_module, "_ssh_stat_paths", lambda paths: _src_stat("/dl/x.mkv"))
    _patch_cache(monkeypatch, _CachedStub(
        title="Show", media_type="tv", year=2020,
        season_number=2, episode_number=None,
    ))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/x.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "organize_not_applicable"
    assert "season + episode" in body["reason"]


def test_preview_unsupported_media_type_returns_400(client, token, monkeypatch):
    """cache.media_type='anime' 不支持 → 400 src_not_identified（合并到未识别情形）."""
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(app_module, "_ssh_stat_paths", lambda paths: _src_stat("/dl/x.mkv"))
    _patch_cache(monkeypatch, _CachedStub(title="X", media_type="anime"))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/x.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    # media_type 不在 {movie, tv} → 视为 not_identified（preview 不在这里区分两种 reject reason）
    assert resp.get_json()["error"] == "src_not_identified"


def test_preview_movie_happy_path_returns_token_and_plan(client, token, monkeypatch):
    _patch_organize_config(monkeypatch)

    def fake_stat(paths):
        result = {}
        for p in paths:
            if p == "/dl/movie.mkv":
                result[p] = {"exists": True, "inode": 12345, "size_bytes": 1024, "mtime": 1000}
            else:
                result[p] = {"exists": False}
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    _patch_cache(monkeypatch, _CachedStub(
        title="The Movie", media_type="movie", year=2024, tmdb_id="111",
    ))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/movie.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert "action_id" in body and "signed_token" in body
    assert body["kind"] == "organize"
    assert body["items_count"] == 1
    item = body["items"][0]
    assert item["media_type"] == "movie"
    assert item["tmdb_id"] == "111"
    assert item["computed_plan"]["dst_path"] == "/media/movies/The Movie (2024)/movie.mkv"
    assert item["computed_plan"]["tvshow_nfo_path"] is None
    assert item["dst_status"]["dst_path_exists"] is False
    assert item["dst_status"]["already_linked"] is False
    assert item["dst_status"]["conflict"] is False
    assert item["src_snapshot"]["inode"] == 12345


def test_preview_tv_happy_path_includes_tvshow_nfo_path(client, token, monkeypatch):
    _patch_organize_config(monkeypatch)

    def fake_stat(paths):
        result = {}
        for p in paths:
            if p == "/dl/show.s02e05.mkv":
                result[p] = {"exists": True, "inode": 200, "size_bytes": 999, "mtime": 1500}
            else:
                result[p] = {"exists": False}
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    _patch_cache(monkeypatch, _CachedStub(
        title="My Show", media_type="tv", year=2020, tmdb_id="222",
        season_number=2, episode_number=5,
    ))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/show.s02e05.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    item = resp.get_json()["items"][0]
    assert item["computed_plan"]["dst_path"] == "/media/tv/My Show (2020)/Season 02/show.s02e05.mkv"
    assert item["computed_plan"]["tvshow_nfo_path"] == "/media/tv/My Show (2020)/tvshow.nfo"
    assert item["season_number"] == 2 and item["episode_number"] == 5


def test_preview_dst_already_linked_marks_flag(client, token, monkeypatch):
    """dst_path 已存在 + inode == src.inode → already_linked=True."""
    _patch_organize_config(monkeypatch)

    def fake_stat(paths):
        result = {}
        for p in paths:
            if p == "/dl/movie.mkv":
                result[p] = {"exists": True, "inode": 7777, "size_bytes": 100, "mtime": 1}
            elif p == "/media/movies/Movie (2024)/movie.mkv":
                # 已经 hardlinked：同 inode
                result[p] = {"exists": True, "inode": 7777, "size_bytes": 100, "mtime": 1}
            else:
                result[p] = {"exists": False}
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    _patch_cache(monkeypatch, _CachedStub(
        title="Movie", media_type="movie", year=2024,
    ))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/movie.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    item = resp.get_json()["items"][0]
    assert item["dst_status"]["dst_path_exists"] is True
    assert item["dst_status"]["already_linked"] is True
    assert item["dst_status"]["conflict"] is False


def test_preview_dst_conflict_different_inode_marks_flag(client, token, monkeypatch):
    """dst_path 已存在 + inode != src.inode → conflict=True (UI 提示用户)."""
    _patch_organize_config(monkeypatch)

    def fake_stat(paths):
        result = {}
        for p in paths:
            if p == "/dl/movie.mkv":
                result[p] = {"exists": True, "inode": 1111, "size_bytes": 100, "mtime": 1}
            elif p == "/media/movies/Movie (2024)/movie.mkv":
                result[p] = {"exists": True, "inode": 9999, "size_bytes": 100, "mtime": 1}
            else:
                result[p] = {"exists": False}
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    _patch_cache(monkeypatch, _CachedStub(title="Movie", media_type="movie", year=2024))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/movie.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    item = resp.get_json()["items"][0]
    assert item["dst_status"]["dst_path_exists"] is True
    assert item["dst_status"]["already_linked"] is False
    assert item["dst_status"]["conflict"] is True


def test_preview_empty_src_path_in_item_returns_400(client, token, monkeypatch):
    _patch_organize_config(monkeypatch)
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": ""}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "src_path required" in resp.get_json()["error"]
