"""metadata_cache 单元测试：upsert / get_by_path / stale 检测。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from services import destructive_action, metadata_cache
from services.identify import FilenameParse, IdentifyResult
from services.metadata.base import MediaCandidate


SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


@pytest.fixture
def conn(tmp_path):
    db_file = tmp_path / "t.db"
    c = destructive_action.open_connection(db_file)
    destructive_action.init_schema(c, SCHEMA_PATH)
    yield c
    c.close()


def _make_parse(**overrides) -> FilenameParse:
    defaults = dict(
        raw_name="Show.S06E02.mkv", title="Show", year=2022,
        season=6, episode=2, episode_title="Ep Name",
        media_type="episode", resolution="1080p",
        source="WEB-DL", release_group="RG", raw={},
    )
    defaults.update(overrides)
    return FilenameParse(**defaults)


def _make_candidate(**overrides) -> MediaCandidate:
    defaults = dict(
        id="tmdb:tv:60625",
        external_ids={"tmdb_id": "60625", "imdb_id": "tt2861424"},
        title="瑞克和莫蒂", original_title="Rick and Morty",
        year=2013, media_type="tv",
        poster_url="https://image.tmdb.org/abc.jpg",
        overview="A misfit family across the multiverse.",
        vote_average=8.7, raw={},
    )
    defaults.update(overrides)
    return MediaCandidate(**defaults)


def _make_result(top: MediaCandidate | None = None, **overrides) -> IdentifyResult:
    defaults = dict(
        parse=_make_parse(),
        candidates=[top] if top else [],
        top_pick=top,
        confidence=0.95 if top else 0.2,
        reasoning="single_exact" if top else "no candidates",
        pick_source="single_exact" if top else "needs_review",
    )
    defaults.update(overrides)
    return IdentifyResult(**defaults)


# ---------------- upsert + get hit ---------------- #


def test_upsert_then_get_hit(conn):
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn, path="/share/a.mkv",
        stat={"inode": 123, "size_bytes": 1000, "mtime": 2000},
        identify_result=res,
    )
    cached, status = metadata_cache.get_by_path(conn, "/share/a.mkv",
                                                 current_mtime=2000, current_inode=123)
    assert status == "hit"
    assert cached is not None
    assert cached.tmdb_id == "60625"
    assert cached.title == "瑞克和莫蒂"
    assert cached.original_title == "Rick and Morty"
    assert cached.season_number == 6
    assert cached.episode_number == 2
    assert cached.metadata_confidence == 0.95
    assert cached.metadata_status == "ok"
    assert cached.metadata_source == "tmdb"
    assert cached.metadata_pick_source == "single_exact"


def test_get_miss_for_unknown_path(conn):
    cached, status = metadata_cache.get_by_path(conn, "/share/nothing.mkv")
    assert status == "miss"
    assert cached is None


def test_stale_on_mtime_change(conn):
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn, path="/share/b.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    cached, status = metadata_cache.get_by_path(conn, "/share/b.mkv",
                                                 current_mtime=9999, current_inode=1)
    assert status == "stale"
    assert cached is not None
    assert cached.mtime == 1000  # cached value is still the old mtime


def test_stale_on_inode_change(conn):
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn, path="/share/c.mkv",
        stat={"inode": 5, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    cached, status = metadata_cache.get_by_path(conn, "/share/c.mkv",
                                                 current_mtime=1000, current_inode=99)
    assert status == "stale"


def test_skip_stale_check_when_current_not_provided(conn):
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn, path="/share/d.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    cached, status = metadata_cache.get_by_path(conn, "/share/d.mkv")
    assert status == "hit"


# ---------------- upsert is idempotent (same path → no new row) ---------------- #


def test_upsert_idempotent_same_path(conn):
    res1 = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn, path="/share/e.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res1,
    )
    res2 = _make_result(top=_make_candidate(title="Updated", original_title="Updated Orig"))
    metadata_cache.upsert_identification(
        conn, path="/share/e.mkv",
        stat={"inode": 2, "size_bytes": 200, "mtime": 2000},
        identify_result=res2,
    )
    rows = conn.execute("SELECT COUNT(*) FROM media_files WHERE path = ?", ("/share/e.mkv",)).fetchone()
    assert rows[0] == 1
    cached, status = metadata_cache.get_by_path(conn, "/share/e.mkv",
                                                 current_mtime=2000, current_inode=2)
    assert status == "hit"
    assert cached.title == "Updated"
    assert cached.original_title == "Updated Orig"
    assert cached.mtime == 2000
    assert cached.inode == 2


# ---------------- needs_review also gets persisted ---------------- #


def test_needs_review_persists(conn):
    res = _make_result(top=None)  # 没 top_pick → status='needs_review'
    metadata_cache.upsert_identification(
        conn, path="/share/f.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    cached, _ = metadata_cache.get_by_path(conn, "/share/f.mkv",
                                            current_mtime=1000, current_inode=1)
    assert cached is not None
    assert cached.metadata_status == "needs_review"
    assert cached.tmdb_id is None
    # parse 字段仍然写入了
    assert cached.parse_raw_name == "Show.S06E02.mkv"
    assert cached.season_number == 6


# ---------------- upsert_details patches non-None fields only ---------------- #


def test_upsert_details_patches_only_provided(conn):
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn, path="/share/g.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    metadata_cache.upsert_details(
        conn, path="/share/g.mkv",
        genres=["Animation", "Comedy"],
        cast=["Justin Roiland"],
        runtime_minutes=22,
    )
    cached, _ = metadata_cache.get_by_path(conn, "/share/g.mkv")
    assert cached.genres == ["Animation", "Comedy"]
    assert cached.cast == ["Justin Roiland"]
    assert cached.runtime_minutes == 22
    # 未传入的字段保持原值（不被 None 覆盖）
    assert cached.title == "瑞克和莫蒂"


def test_upsert_details_noop_when_empty(conn):
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn, path="/share/h.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    # 全 None → 不该改 last_updated_at
    metadata_cache.upsert_details(conn, path="/share/h.mkv")
    cached, _ = metadata_cache.get_by_path(conn, "/share/h.mkv")
    assert cached.runtime_minutes is None  # 仍是初始值


# ---------------- delete_by_path ---------------- #


def test_delete_by_path(conn):
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn, path="/share/i.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    assert metadata_cache.delete_by_path(conn, "/share/i.mkv") is True
    cached, status = metadata_cache.get_by_path(conn, "/share/i.mkv")
    assert status == "miss"


def test_delete_nonexistent_returns_false(conn):
    assert metadata_cache.delete_by_path(conn, "/share/never.mkv") is False
