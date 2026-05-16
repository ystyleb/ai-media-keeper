"""ROADMAP #1: /api/metadata/bind route contract tests.

User clicks a candidate from the needs_review/heuristic list → backend writes
cache with pick_source='manual', confidence=1.0. Mocks the SSH + provider
boundary so SQLite + identify pipeline are the only real layers exercised.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import os

import app as app_module
from services.metadata.base import MediaCandidate, MediaDetails


# config/nas.json 启动时把 env-set NAS_BASE_PATH 覆盖成 production "/share/CACHEDEV2_DATA"，
# 这里我们继承运行时实际值（无论 env / nas.json 谁赢），保证 validate_path 通过。
_BASE = app_module.NAS_BASE_PATH


def _p(rel: str) -> str:
    """Build a path under NAS_BASE_PATH so validate_path passes."""
    return f"{_BASE.rstrip('/')}/{rel}"


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def token():
    return app_module.API_TOKEN


def _make_movie_details(tmdb_id="603") -> MediaDetails:
    cand = MediaCandidate(
        id=f"tmdb:movie:{tmdb_id}",
        external_ids={"tmdb_id": tmdb_id, "imdb_id": "tt0133093"},
        title="The Matrix", original_title="The Matrix",
        year=1999, media_type="movie",
        poster_url="https://image.tmdb.org/p/poster.jpg",
        overview="A hacker discovers reality is a simulation.",
        vote_average=8.2, raw={},
    )
    return MediaDetails(
        candidate=cand, cast=["Keanu Reeves", "Carrie-Anne Moss"],
        genres=["Action", "Sci-Fi"], runtime_minutes=136,
    )


def _make_tv_details(tmdb_id="60625", season=6, episode=2) -> MediaDetails:
    cand = MediaCandidate(
        id=f"tmdb:tv:{tmdb_id}",
        external_ids={"tmdb_id": tmdb_id, "imdb_id": "tt2861424"},
        title="瑞克和莫蒂", original_title="Rick and Morty",
        year=2013, media_type="tv",
        poster_url="https://image.tmdb.org/p/poster.jpg",
        overview="Animated series.",
        vote_average=8.7, raw={},
    )
    return MediaDetails(
        candidate=cand,
        episode={
            "season_number": season, "episode_number": episode,
            "name": "Rickdependence Spray",
            "overview": "Specific episode plot.",
            "air_date": "2021-06-27",
            "still_url": "https://image.tmdb.org/p/still.jpg",
        },
        cast=["Justin Roiland"], genres=["Animation", "Comedy"],
    )


def _stat_ok(path=None):
    if path is None:
        path = _p("test.mkv")
    return {path: {"exists": True, "inode": 123, "size_bytes": 1000, "mtime": 2000}}


def _stat_missing(path=None):
    if path is None:
        path = _p("missing.mkv")
    return {path: {"exists": False}}


# ── 校验 ─────────────────────────────────────────────────


def test_bind_missing_path_returns_400(client, token):
    resp = client.post(
        "/api/metadata/bind",
        json={"tmdb_id": "603", "media_type": "movie"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "required" in resp.get_json()["error"]


def test_bind_missing_tmdb_id_returns_400(client, token):
    resp = client.post(
        "/api/metadata/bind",
        json={"path": "/share/x.mkv", "media_type": "movie"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_bind_invalid_media_type_returns_400(client, token):
    resp = client.post(
        "/api/metadata/bind",
        json={"path": "/share/x.mkv", "tmdb_id": "603", "media_type": "extra"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_bind_non_numeric_tmdb_id_returns_400(client, token):
    """SDK path injection 防护：tmdb_id 必须 numeric。"""
    resp = client.post(
        "/api/metadata/bind",
        json={"path": "/share/x.mkv", "tmdb_id": "../etc/passwd", "media_type": "movie"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "numeric" in resp.get_json()["error"]


def test_bind_file_missing_returns_404(client, token):
    """SSH stat 报 exists=False → 不写 cache，防错位到已删文件。"""
    with patch.object(app_module, "_ssh_stat_paths", return_value=_stat_missing()):
        resp = client.post(
            "/api/metadata/bind",
            json={"path": _p("missing.mkv"), "tmdb_id": "603", "media_type": "movie"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "src_missing"


def test_bind_provider_not_configured_returns_400(client, token):
    with patch.object(app_module, "get_tmdb_provider", return_value=None):
        resp = client.post(
            "/api/metadata/bind",
            json={"path": _p("x.mkv"), "tmdb_id": "603", "media_type": "movie"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 400
    assert "TMDB" in resp.get_json()["error"]


# ── happy path movie ─────────────────────────────────────


def test_bind_movie_happy_writes_cache_with_manual_pick_source(client, token):
    """绑定电影 → cache row metadata_pick_source='manual', confidence=1.0, status='ok'."""
    fake_provider = type("P", (), {
        "lookup_by_id": lambda self, *a, **kw: _make_movie_details(),
    })()
    path = _p("test.matrix.mkv")
    with patch.object(app_module, "_ssh_stat_paths", return_value=_stat_ok(path)), \
         patch.object(app_module, "get_tmdb_provider", return_value=fake_provider):
        resp = client.post(
            "/api/metadata/bind",
            json={"path": path, "tmdb_id": "603", "media_type": "movie"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["bound"] is True
    cached = body["cached"]
    assert cached is not None
    # _cached_to_library_dict 不暴露 metadata_pick_source — 直接查 DB
    from services import metadata_cache
    db = app_module.get_db_for_test() if hasattr(app_module, "get_db_for_test") else None
    if db is None:
        # Flask test client teardown 后 g.db 已 close — 用 app context 重读
        with app_module.app.app_context():
            row = app_module.get_db().execute(
                "SELECT metadata_pick_source, metadata_status, metadata_confidence, "
                "title, year, media_type, tmdb_id, tmdb_movie_id "
                "FROM media_files WHERE path = ?", (path,)
            ).fetchone()
    assert row["metadata_pick_source"] == "manual"
    assert row["metadata_status"] == "ok"
    assert row["metadata_confidence"] == 1.0
    assert row["media_type"] == "movie"
    assert row["title"] == "The Matrix"
    assert row["year"] == 1999
    assert row["tmdb_id"] == "603"
    assert row["tmdb_movie_id"] == "603"   # split id 也对


# ── happy path tv ────────────────────────────────────────


def test_bind_tv_happy_writes_episode_details(client, token):
    """绑定 tv 集 → cache + episode_overview / air_date / still 都从 lookup_by_id 拉。"""
    fake_provider = type("P", (), {
        "lookup_by_id": lambda self, *a, **kw: _make_tv_details(season=6, episode=2),
    })()
    path = _p("RnM.S06E02.mkv")
    with patch.object(app_module, "_ssh_stat_paths", return_value=_stat_ok(path)), \
         patch.object(app_module, "get_tmdb_provider", return_value=fake_provider), \
         patch.object(app_module.identify_svc, "parse_filename") as mock_parse:
        from services.identify import FilenameParse
        mock_parse.return_value = FilenameParse(
            raw_name="RnM.S06E02.mkv", title="RnM", year=None,
            season=6, episode=2, episode_title=None,
            media_type="episode", resolution="1080p", source="WEB-DL",
            release_group=None, codec=None, color_depth=None,
            hdr_profiles=[], container=None, audio_codec=None, raw={},
        )
        resp = client.post(
            "/api/metadata/bind",
            json={
                "path": path, "tmdb_id": "60625", "media_type": "tv",
                "season": 6, "episode": 2,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["bound"] is True
    with app_module.app.app_context():
        row = app_module.get_db().execute(
            "SELECT metadata_pick_source, media_type, season_number, episode_number, "
            "episode_overview, episode_air_date, episode_still_url, "
            "tmdb_id, tmdb_series_id "
            "FROM media_files WHERE path = ?", (path,)
        ).fetchone()
    assert row["metadata_pick_source"] == "manual"
    assert row["media_type"] == "tv"
    assert row["season_number"] == 6
    assert row["episode_number"] == 2
    assert row["episode_overview"] == "Specific episode plot."
    assert row["episode_air_date"] == "2021-06-27"
    assert row["tmdb_id"] == "60625"
    assert row["tmdb_series_id"] == "60625"     # tv 行的 split id


# ── error paths ──────────────────────────────────────────


def test_bind_lookup_raises_returns_502(client, token):
    """TMDB 网关 down → 502 + 错误透传给 user 让其知道是 provider 问题。"""
    def raise_provider(*a, **kw):
        raise RuntimeError("TMDB 502 Bad Gateway")
    fake_provider = type("P", (), {"lookup_by_id": lambda self, *a, **kw: raise_provider()})()
    with patch.object(app_module, "_ssh_stat_paths", return_value=_stat_ok()), \
         patch.object(app_module, "get_tmdb_provider", return_value=fake_provider):
        resp = client.post(
            "/api/metadata/bind",
            json={"path": _p("test.mkv"), "tmdb_id": "603", "media_type": "movie"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 502
    assert "tmdb_lookup_failed" in resp.get_json()["error"]


def test_bind_lookup_returns_none_returns_404(client, token):
    """tmdb_id 无效（404 on TMDB）→ provider 返 None → route 返 404 而非 500。"""
    fake_provider = type("P", (), {"lookup_by_id": lambda self, *a, **kw: None})()
    with patch.object(app_module, "_ssh_stat_paths", return_value=_stat_ok()), \
         patch.object(app_module, "get_tmdb_provider", return_value=fake_provider):
        resp = client.post(
            "/api/metadata/bind",
            json={"path": _p("test.mkv"), "tmdb_id": "99999999", "media_type": "movie"},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 404
    assert resp.get_json()["error"] == "tmdb_id_not_found"


# ── idempotency / re-bind ────────────────────────────────


def test_bind_rebind_overwrites_previous(client, token):
    """先绑 Matrix → 改主意再绑同 path 到别的电影 → cache 应反映最新选择。"""
    matrix = type("P", (), {"lookup_by_id": lambda self, *a, **kw: _make_movie_details(tmdb_id="603")})()
    # 第 2 次绑到 Inception
    inception_details = MediaDetails(
        candidate=MediaCandidate(
            id="tmdb:movie:27205", external_ids={"tmdb_id": "27205"},
            title="Inception", original_title="Inception",
            year=2010, media_type="movie",
            poster_url=None, overview=None, vote_average=8.4, raw={},
        ),
        cast=[], genres=[], runtime_minutes=148,
    )
    inception = type("P", (), {"lookup_by_id": lambda self, *a, **kw: inception_details})()
    path = _p("test.mkv")
    with patch.object(app_module, "_ssh_stat_paths", return_value=_stat_ok(path)):
        with patch.object(app_module, "get_tmdb_provider", return_value=matrix):
            resp1 = client.post(
                "/api/metadata/bind",
                json={"path": path, "tmdb_id": "603", "media_type": "movie"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp1.status_code == 200
        with patch.object(app_module, "get_tmdb_provider", return_value=inception):
            resp2 = client.post(
                "/api/metadata/bind",
                json={"path": path, "tmdb_id": "27205", "media_type": "movie"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert resp2.status_code == 200
    with app_module.app.app_context():
        rows = app_module.get_db().execute(
            "SELECT title, tmdb_id FROM media_files WHERE path = ?", (path,)
        ).fetchall()
    assert len(rows) == 1   # idempotent on path PK
    assert rows[0]["title"] == "Inception"
    assert rows[0]["tmdb_id"] == "27205"


# ── codex r1 BLOCKER: season/episode 注入防护 ──────────


def test_bind_season_string_returns_400(client, token):
    """codex r1 BLOCKER：season 必须 int，防 SDK URL path injection。"""
    with patch.object(app_module, "_ssh_stat_paths", return_value=_stat_ok()):
        resp = client.post(
            "/api/metadata/bind",
            json={
                "path": _p("test.mkv"), "tmdb_id": "60625", "media_type": "tv",
                "season": "1/episode/2/credits",   # ← inject 试图
                "episode": 1,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 400
    assert "season" in resp.get_json()["error"]


def test_bind_episode_negative_returns_400(client, token):
    with patch.object(app_module, "_ssh_stat_paths", return_value=_stat_ok()):
        resp = client.post(
            "/api/metadata/bind",
            json={
                "path": _p("test.mkv"), "tmdb_id": "60625", "media_type": "tv",
                "season": 1, "episode": -1,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 400


def test_bind_season_bool_rejected(client, token):
    """isinstance(True, int) == True in Python — 显式 reject bool 避免 silent pass-through."""
    with patch.object(app_module, "_ssh_stat_paths", return_value=_stat_ok()):
        resp = client.post(
            "/api/metadata/bind",
            json={
                "path": _p("test.mkv"), "tmdb_id": "60625", "media_type": "tv",
                "season": True, "episode": 1,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 400


def test_bind_season_too_large_rejected(client, token):
    """upper bound 9999 防 abuse."""
    with patch.object(app_module, "_ssh_stat_paths", return_value=_stat_ok()):
        resp = client.post(
            "/api/metadata/bind",
            json={
                "path": _p("test.mkv"), "tmdb_id": "60625", "media_type": "tv",
                "season": 1000000, "episode": 1,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 400
