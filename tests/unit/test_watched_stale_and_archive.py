"""Phase 3.5: watched-stale 3-branch SQL + archive executor stub tests."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

import app as app_module
from db import migrations
from services import destructive_action, dedup

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "t.db"
    c = destructive_action.open_connection(db)
    destructive_action.init_schema(c, SCHEMA_PATH)
    migrations.phase3_migrate(c)
    yield c
    c.close()


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def token():
    return app_module.API_TOKEN


def _now():
    return int(time.time())


def _seed_movie(conn, path, *, tmdb_movie_id, first_seen_at, size=1_000_000_000):
    conn.execute(
        """
        INSERT INTO media_files(
          path, inode, size_bytes, mtime, tmdb_id, tmdb_movie_id, media_type,
          title, year, metadata_status, first_seen_at, last_updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, 'movie', 'M', 2020, 'ok', ?, ?)
        """,
        (path, abs(hash(path)) % 10_000_000, size, first_seen_at,
         tmdb_movie_id, tmdb_movie_id, first_seen_at, first_seen_at),
    )
    conn.commit()


def _seed_tv(conn, path, *, tmdb_series_id, season, episode,
             tmdb_episode_id=None, first_seen_at=None, size=500_000_000):
    fs = first_seen_at or _now()
    conn.execute(
        """
        INSERT INTO media_files(
          path, inode, size_bytes, mtime, tmdb_id, tmdb_series_id, tmdb_episode_id,
          media_type, title, year, season_number, episode_number,
          metadata_status, first_seen_at, last_updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'tv', 'S', 2020, ?, ?, 'ok', ?, ?)
        """,
        (path, abs(hash(path)) % 10_000_000, size, fs, tmdb_series_id, tmdb_series_id,
         tmdb_episode_id, season, episode, fs, fs),
    )
    conn.commit()


def _seed_watched_movie(conn, tmdb_movie_id):
    conn.execute(
        """
        INSERT INTO watched_items(provider, provider_item_id, media_type,
          tmdb_movie_id, watched_at, fetched_at, mapping_status,
          mapping_confidence, mapping_source)
        VALUES ('emby', ?, 'movie', ?, ?, ?, 'mapped', 1.0, 'emby.provider_ids')
        """,
        (f"emby-m-{tmdb_movie_id}", tmdb_movie_id, _now(), _now()),
    )
    conn.commit()


def _seed_watched_tv(conn, *, tmdb_series_id, season, episode, tmdb_episode_id=None):
    status = "mapped" if tmdb_episode_id else "fallback_se"
    src = "emby.provider_ids" if tmdb_episode_id else "emby.series_provider_ids+se"
    conf = 1.0 if tmdb_episode_id else 0.7
    conn.execute(
        """
        INSERT INTO watched_items(provider, provider_item_id, media_type,
          tmdb_series_id, tmdb_episode_id, season_number, episode_number,
          watched_at, fetched_at, mapping_status, mapping_confidence, mapping_source)
        VALUES ('emby', ?, 'tv', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (f"emby-tv-{tmdb_series_id}-{season}-{episode}",
         tmdb_series_id, tmdb_episode_id, season, episode,
         _now(), _now(), status, conf, src),
    )
    conn.commit()


# ── 3-branch watched-stale SQL ────────────────────────────────


def test_watched_stale_movie_branch_only(conn):
    """Movie 已看且 first_seen_at 旧 → 返回。"""
    old = _now() - 200 * 86400
    _seed_movie(conn, "/old-movie.mkv", tmdb_movie_id="238", first_seen_at=old)
    _seed_watched_movie(conn, "238")
    items, total = dedup.find_watched_stale_media(conn, days=180)
    assert total == 1
    assert items[0]["path"] == "/old-movie.mkv"
    assert items[0]["days_since_first_seen"] >= 180


def test_watched_stale_recently_added_movie_excluded(conn):
    fresh = _now() - 10 * 86400
    _seed_movie(conn, "/new-movie.mkv", tmdb_movie_id="238", first_seen_at=fresh)
    _seed_watched_movie(conn, "238")
    items, total = dedup.find_watched_stale_media(conn, days=180)
    assert total == 0
    assert items == []


def test_watched_stale_unwatched_old_movie_excluded(conn):
    """Old but not watched → not returned."""
    old = _now() - 200 * 86400
    _seed_movie(conn, "/old-but-unwatched.mkv", tmdb_movie_id="238", first_seen_at=old)
    # 不 seed watched
    items, total = dedup.find_watched_stale_media(conn, days=180)
    assert total == 0


def test_watched_stale_tv_episode_id_strong_branch(conn):
    """TV episode_id 命中（strong signal）→ 返回。"""
    old = _now() - 200 * 86400
    _seed_tv(conn, "/old-ep.mkv", tmdb_series_id="1399",
             season=1, episode=1, tmdb_episode_id="ep-99", first_seen_at=old)
    _seed_watched_tv(conn, tmdb_series_id="1399", season=1, episode=1,
                     tmdb_episode_id="ep-99")
    items, total = dedup.find_watched_stale_media(conn, days=180)
    assert total == 1
    assert items[0]["path"] == "/old-ep.mkv"


def test_watched_stale_tv_series_se_fallback_branch(conn):
    """TV: m 无 episode_id + watched 仅有 series+s+e → fallback 分支命中。"""
    old = _now() - 200 * 86400
    _seed_tv(conn, "/old-ep-fb.mkv", tmdb_series_id="1399",
             season=2, episode=3, tmdb_episode_id=None, first_seen_at=old)
    _seed_watched_tv(conn, tmdb_series_id="1399", season=2, episode=3,
                     tmdb_episode_id=None)
    items, total = dedup.find_watched_stale_media(conn, days=180)
    assert total == 1


def test_watched_stale_tv_episode_id_branch_excludes_se_only(conn):
    """如果 m 有 episode_id 但 watched 只有 series+s+e fallback：

    第二分支需要双侧 episode_id NOT NULL → 不命中；
    第三分支 m.tmdb_episode_id IS NULL 排除 → 也不命中。
    这反映 plan r3 polish ROI 衰减注记的"recall 损失" — 已接受。
    """
    old = _now() - 200 * 86400
    _seed_tv(conn, "/has-ep-but-fallback-watched.mkv", tmdb_series_id="1399",
             season=1, episode=1, tmdb_episode_id="ep-99", first_seen_at=old)
    _seed_watched_tv(conn, tmdb_series_id="1399", season=1, episode=1,
                     tmdb_episode_id=None)  # fallback only
    items, total = dedup.find_watched_stale_media(conn, days=180)
    assert total == 0    # acceptable recall loss (documented)


def test_watched_stale_pattern_b_movie_does_not_match_tv_watched(conn):
    """Movie m + TV watched 同 tmdb id 值 → 不应误中（不同 id space）。"""
    old = _now() - 200 * 86400
    _seed_movie(conn, "/m.mkv", tmdb_movie_id="999", first_seen_at=old)
    # tv watched 用同样的字符串 — 不该 join 上
    _seed_watched_tv(conn, tmdb_series_id="999", season=1, episode=1,
                     tmdb_episode_id="999")
    items, total = dedup.find_watched_stale_media(conn, days=180)
    assert total == 0


def test_watched_stale_pagination(conn):
    old = _now() - 200 * 86400
    for i in range(5):
        _seed_movie(conn, f"/old-m-{i}.mkv",
                    tmdb_movie_id=str(100 + i),
                    first_seen_at=old,
                    size=(5 - i) * 1_000_000_000)        # decreasing size
        _seed_watched_movie(conn, str(100 + i))
    page1, total = dedup.find_watched_stale_media(conn, days=180, limit=2, offset=0)
    page2, _ = dedup.find_watched_stale_media(conn, days=180, limit=2, offset=2)
    assert total == 5
    assert len(page1) == 2
    assert len(page2) == 2
    # ORDER BY size_bytes DESC → page1 has the biggest
    assert page1[0]["size_bytes"] > page2[0]["size_bytes"]


# ── route /api/library/watched-stale ──────────────────────────


def test_watched_stale_route_returns_list(client, token, monkeypatch):
    fake_items = [{
        "id": 1, "path": "/x.mkv", "media_type": "movie",
        "title": "X", "year": 2020, "tmdb_movie_id": "238",
        "tmdb_series_id": None, "tmdb_episode_id": None,
        "season_number": None, "episode_number": None,
        "size_bytes": 1_000_000_000, "mtime": 1700000000,
        "first_seen_at": 1700000000, "poster_url": None,
        "parse_resolution": "1080p", "parse_source": "Blu-ray",
        "days_since_first_seen": 200,
    }]
    with patch.object(dedup, "find_watched_stale_media", return_value=(fake_items, 1)):
        resp = client.get(
            "/api/library/watched-stale?days=180&limit=10",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == 1
    assert body["days_threshold"] == 180
    assert len(body["items"]) == 1


# ── archive executor stub ─────────────────────────────────────


def test_archive_preview_returns_token_with_disabled_warning(client, token):
    resp = client.post(
        "/api/action/preview",
        json={"kind": "archive", "candidates": [{"path": "/test/a.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert "signed_token" in body
    assert "action_id" in body
    assert body["kind"] == "archive"
    assert body["warning"] == "archive_executor_disabled"


def test_archive_executor_raises_disabled_error():
    with pytest.raises(app_module.ArchiveDisabledError) as exc_info:
        app_module._archive_executor({"candidates": [{"path": "/x"}]})
    assert "archive_kind_disabled_in_phase3" in str(exc_info.value)


def test_archive_executor_does_not_call_ssh_or_qbit(monkeypatch):
    """Stub 必须不接触 SSH / qBit / 文件系统。"""
    ssh_called = []

    def fake_ssh(*args, **kwargs):
        ssh_called.append(args)
        return 0, "", ""

    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh)
    monkeypatch.setattr(app_module.qbit, "find_torrents_by_paths",
                        lambda *a, **k: ssh_called.append("qbit"))
    with pytest.raises(app_module.ArchiveDisabledError):
        app_module._archive_executor({"candidates": [{"path": "/x"}]})
    assert ssh_called == []      # no SSH or qBit touched


def test_archive_route_executor_dispatches_disabled():
    """_route_executor_by_kind kind='archive' → _archive_executor → ArchiveDisabledError."""
    with pytest.raises(app_module.ArchiveDisabledError):
        app_module._route_executor_by_kind({"kind": "archive"})


def test_archive_preview_missing_candidates_400(client, token):
    resp = client.post(
        "/api/action/preview",
        json={"kind": "archive"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_archive_confirm_writes_failed_with_disabled_marker(client, token):
    """End-to-end via test client: preview → confirm should land status=failed
    with error containing 'ArchiveDisabledError' / 'disabled_kind' marker."""
    # 1. preview
    p = client.post(
        "/api/action/preview",
        json={"kind": "archive", "candidates": [{"path": "/test/a.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert p.status_code == 200
    pd = p.get_json()

    # 2. confirm
    c = client.post(
        "/api/action/confirm",
        json={"action_id": pd["action_id"], "signed_token": pd["signed_token"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert c.status_code == 200
    cd = c.get_json()
    assert cd["status"] == "failed"
    assert "ArchiveDisabledError" in cd["error"] or "disabled_kind" in cd["error"]
