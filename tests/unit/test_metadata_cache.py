"""metadata_cache 单元测试：upsert / get_by_path / stale 检测。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from services import destructive_action, metadata_cache
from services.identify import FilenameParse, IdentifyResult
from services.metadata.base import MediaCandidate, MediaDetails

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


@pytest.fixture
def conn(tmp_path):
    db_file = tmp_path / "t.db"
    c = destructive_action.open_connection(db_file)
    destructive_action.init_schema(c, SCHEMA_PATH)
    # Phase 3 migration (adds 10 media_files columns + new tables)
    from db import migrations

    migrations.phase3_migrate(c)
    yield c
    c.close()


def _make_parse(**overrides) -> FilenameParse:
    defaults = dict(
        raw_name="Show.S06E02.mkv",
        title="Show",
        year=2022,
        season=6,
        episode=2,
        episode_title="Ep Name",
        media_type="episode",
        resolution="1080p",
        source="WEB-DL",
        release_group="RG",
        codec=None,
        color_depth=None,
        hdr_profiles=[],
        container=None,
        audio_codec=None,
        raw={},
    )
    defaults.update(overrides)
    return FilenameParse(**defaults)


def _make_candidate(**overrides) -> MediaCandidate:
    defaults = dict(
        id="tmdb:tv:60625",
        external_ids={"tmdb_id": "60625", "imdb_id": "tt2861424"},
        title="瑞克和莫蒂",
        original_title="Rick and Morty",
        year=2013,
        media_type="tv",
        poster_url="https://image.tmdb.org/abc.jpg",
        overview="A misfit family across the multiverse.",
        vote_average=8.7,
        raw={},
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
        conn,
        path="/share/a.mkv",
        stat={"inode": 123, "size_bytes": 1000, "mtime": 2000},
        identify_result=res,
    )
    cached, status = metadata_cache.get_by_path(
        conn, "/share/a.mkv", current_mtime=2000, current_inode=123
    )
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
        conn,
        path="/share/b.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    cached, status = metadata_cache.get_by_path(
        conn, "/share/b.mkv", current_mtime=9999, current_inode=1
    )
    assert status == "stale"
    assert cached is not None
    assert cached.mtime == 1000  # cached value is still the old mtime


def test_stale_on_inode_change(conn):
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn,
        path="/share/c.mkv",
        stat={"inode": 5, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    cached, status = metadata_cache.get_by_path(
        conn, "/share/c.mkv", current_mtime=1000, current_inode=99
    )
    assert status == "stale"


def test_skip_stale_check_when_current_not_provided(conn):
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn,
        path="/share/d.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    cached, status = metadata_cache.get_by_path(conn, "/share/d.mkv")
    assert status == "hit"


# ---------------- upsert is idempotent (same path → no new row) ---------------- #


def test_upsert_idempotent_same_path(conn):
    res1 = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn,
        path="/share/e.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res1,
    )
    res2 = _make_result(top=_make_candidate(title="Updated", original_title="Updated Orig"))
    metadata_cache.upsert_identification(
        conn,
        path="/share/e.mkv",
        stat={"inode": 2, "size_bytes": 200, "mtime": 2000},
        identify_result=res2,
    )
    rows = conn.execute(
        "SELECT COUNT(*) FROM media_files WHERE path = ?", ("/share/e.mkv",)
    ).fetchone()
    assert rows[0] == 1
    cached, status = metadata_cache.get_by_path(
        conn, "/share/e.mkv", current_mtime=2000, current_inode=2
    )
    assert status == "hit"
    assert cached.title == "Updated"
    assert cached.original_title == "Updated Orig"
    assert cached.mtime == 2000
    assert cached.inode == 2


# ---------------- needs_review also gets persisted ---------------- #


def test_needs_review_persists(conn):
    res = _make_result(top=None)  # 没 top_pick → status='needs_review'
    metadata_cache.upsert_identification(
        conn,
        path="/share/f.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    cached, _ = metadata_cache.get_by_path(
        conn, "/share/f.mkv", current_mtime=1000, current_inode=1
    )
    assert cached is not None
    assert cached.metadata_status == "needs_review"
    assert cached.tmdb_id is None
    # parse 字段仍然写入了
    assert cached.parse_raw_name == "Show.S06E02.mkv"
    assert cached.season_number == 6


def test_needs_review_episode_preserves_tv_media_type(conn):
    """needs_review (top=None) 但文件名明确是 episode → DB media_type 应回退到 'tv'
    而非 None（保留文件名已知类型，供 UI 分组 / 分类层判定）。"""
    res = _make_result(top=None)  # default parse: media_type="episode", season=6, episode=2
    metadata_cache.upsert_identification(
        conn,
        path="/share/ep.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    cached, _ = metadata_cache.get_by_path(
        conn, "/share/ep.mkv", current_mtime=1000, current_inode=1
    )
    assert cached is not None
    assert cached.metadata_status == "needs_review"
    assert cached.media_type == "tv"  # 回退保留，不再是 None
    assert cached.tmdb_id is None
    assert cached.season_number == 6
    assert cached.episode_number == 2


def test_needs_review_movie_preserves_movie_media_type(conn):
    """needs_review 但文件名是 movie → media_type 回退 'movie'."""
    parse = _make_parse(media_type="movie", season=None, episode=None, year=1999)
    res = _make_result(parse=parse, top=None)
    metadata_cache.upsert_identification(
        conn,
        path="/share/m.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    cached, _ = metadata_cache.get_by_path(
        conn, "/share/m.mkv", current_mtime=1000, current_inode=1
    )
    assert cached is not None
    assert cached.metadata_status == "needs_review"
    assert cached.media_type == "movie"
    assert cached.tmdb_id is None


def test_needs_review_unknown_media_type_stays_none(conn):
    """needs_review 且文件名类型真未知 (unknown) → media_type 仍 None."""
    parse = _make_parse(media_type="unknown", season=None, episode=None, title="???")
    res = _make_result(parse=parse, top=None)
    metadata_cache.upsert_identification(
        conn,
        path="/share/u.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    cached, _ = metadata_cache.get_by_path(
        conn, "/share/u.mkv", current_mtime=1000, current_inode=1
    )
    assert cached is not None
    assert cached.metadata_status == "needs_review"
    assert cached.media_type is None


# ---------------- upsert_details patches non-None fields only ---------------- #


def test_upsert_details_patches_only_provided(conn):
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn,
        path="/share/g.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    metadata_cache.upsert_details(
        conn,
        path="/share/g.mkv",
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
        conn,
        path="/share/h.mkv",
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
        conn,
        path="/share/i.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    assert metadata_cache.delete_by_path(conn, "/share/i.mkv") is True
    cached, status = metadata_cache.get_by_path(conn, "/share/i.mkv")
    assert status == "miss"


def test_delete_nonexistent_returns_false(conn):
    assert metadata_cache.delete_by_path(conn, "/share/never.mkv") is False


# ---------------- query_library + get_library_stats ---------------- #


def _seed_library(conn, items):
    """便利 seed：items 是 dicts，路径/年份/title 写入 + status=ok。"""
    for it in items:
        c = MediaCandidate(
            id=f"tmdb:{it.get('media_type', 'movie')}:{it.get('tmdb_id', '1')}",
            external_ids={"tmdb_id": str(it.get("tmdb_id", "1"))},
            title=it.get("title", "X"),
            original_title=it.get("original_title"),
            year=it.get("year", 2000),
            media_type=it.get("media_type", "movie"),
            poster_url=None,
            overview=it.get("overview"),
            vote_average=it.get("vote_average", 7.5),
            raw={},
        )
        res = _make_result(top=c)
        metadata_cache.upsert_identification(
            conn,
            path=it["path"],
            stat={"inode": 1, "size_bytes": 100, "mtime": it.get("mtime", 1000)},
            identify_result=res,
        )


def test_query_library_filters_media_type(conn):
    _seed_library(
        conn,
        [
            {"path": "/a.mkv", "tmdb_id": "1", "media_type": "movie", "year": 2020},
            {"path": "/b.mkv", "tmdb_id": "2", "media_type": "tv", "year": 2021},
            {"path": "/c.mkv", "tmdb_id": "3", "media_type": "movie", "year": 2022},
        ],
    )
    items, total = metadata_cache.query_library(conn, media_type="movie")
    assert total == 2
    assert all(i.media_type == "movie" for i in items)


def test_query_library_filters_year_range(conn):
    _seed_library(
        conn,
        [
            {"path": "/old.mkv", "tmdb_id": "1", "year": 1995},
            {"path": "/mid.mkv", "tmdb_id": "2", "year": 2010},
            {"path": "/new.mkv", "tmdb_id": "3", "year": 2022},
        ],
    )
    items, total = metadata_cache.query_library(conn, year_from=2000, year_to=2015)
    assert total == 1
    assert items[0].year == 2010


def test_query_library_query_matches_title_and_original(conn):
    """模糊匹配应该 title (zh-CN) 和 original_title (en) 都命中。"""
    _seed_library(
        conn,
        [
            {
                "path": "/a.mkv",
                "tmdb_id": "1",
                "title": "瑞克和莫蒂",
                "original_title": "Rick and Morty",
            },
            {
                "path": "/b.mkv",
                "tmdb_id": "2",
                "title": "星际穿越",
                "original_title": "Interstellar",
            },
            {"path": "/c.mkv", "tmdb_id": "3", "title": "Other", "original_title": "Other"},
        ],
    )
    # 中文搜
    items, total = metadata_cache.query_library(conn, query="瑞克")
    assert total == 1 and items[0].tmdb_id == "1"
    # 英文搜
    items, total = metadata_cache.query_library(conn, query="Interstellar")
    assert total == 1 and items[0].tmdb_id == "2"
    # 大小写不敏感
    items, total = metadata_cache.query_library(conn, query="interstellar")
    assert total == 1


def test_query_library_sort_year_desc(conn):
    _seed_library(
        conn,
        [
            {"path": "/a.mkv", "tmdb_id": "1", "year": 1995},
            {"path": "/b.mkv", "tmdb_id": "2", "year": 2022},
            {"path": "/c.mkv", "tmdb_id": "3", "year": 2010},
        ],
    )
    items, _ = metadata_cache.query_library(conn, sort="year_desc")
    assert [i.year for i in items] == [2022, 2010, 1995]


def test_query_library_sort_vote_desc(conn):
    _seed_library(
        conn,
        [
            {"path": "/a.mkv", "tmdb_id": "1", "vote_average": 6.5},
            {"path": "/b.mkv", "tmdb_id": "2", "vote_average": 9.0},
            {"path": "/c.mkv", "tmdb_id": "3", "vote_average": 7.5},
        ],
    )
    items, _ = metadata_cache.query_library(conn, sort="vote_desc")
    assert [i.vote_average for i in items] == [9.0, 7.5, 6.5]


def test_query_library_limit_offset(conn):
    _seed_library(
        conn, [{"path": f"/{i}.mkv", "tmdb_id": str(i), "year": 2000 + i} for i in range(10)]
    )
    items, total = metadata_cache.query_library(conn, sort="year_desc", limit=3, offset=2)
    assert total == 10
    assert len(items) == 3
    # year_desc → [2009, 2008, 2007, 2006, ...]; offset 2 limit 3 → [2007, 2006, 2005]
    assert [i.year for i in items] == [2007, 2006, 2005]


def test_query_library_excludes_needs_review(conn):
    # 一个 ok + 一个 needs_review
    _seed_library(conn, [{"path": "/a.mkv", "tmdb_id": "1"}])
    metadata_cache.upsert_identification(
        conn,
        path="/b.mkv",
        stat={"inode": 2, "size_bytes": 100, "mtime": 1000},
        identify_result=_make_result(top=None),  # needs_review
    )
    items, total = metadata_cache.query_library(conn)
    assert total == 1
    assert items[0].path == "/a.mkv"


def test_query_library_excludes_extras_by_default(conn):
    """media_type='extra' 默认从库视图过滤。"""
    # 用 raw upsert 模拟扫描时检测到的 extra（直接 insert）
    _seed_library(
        conn,
        [
            {
                "path": "/Movies/Foo/Foo.2020.mkv",
                "tmdb_id": "1",
                "media_type": "movie",
                "year": 2020,
            },
        ],
    )
    # 手动 insert 一个 extra
    conn.execute("""
        INSERT INTO media_files (path, inode, size_bytes, mtime, media_type, title, year,
                                  metadata_status, metadata_source, metadata_provider,
                                  metadata_fetched_at, first_seen_at, last_updated_at,
                                  parse_raw_name)
        VALUES ('/Movies/Foo/Foo.2020.Extras-01.mkv', 2, 100, 1000, 'extra', 'Foo Extras', 2020,
                'ok', 'tmdb', 'tmdb', 1000, 1000, 1000, 'Foo.2020.Extras-01.mkv')
    """)
    conn.commit()
    items, total = metadata_cache.query_library(conn)
    assert total == 1
    assert items[0].media_type == "movie"
    # include_extras=True 时能拿到
    items2, total2 = metadata_cache.query_library(conn, include_extras=True)
    assert total2 == 2


def test_list_extras_in_dir_returns_same_dir_only(conn):
    """同目录的 extra 返回，子目录或别目录的不返回。"""
    _seed_library(
        conn,
        [
            {
                "path": "/Movies/Foo/Foo.2020.mkv",
                "tmdb_id": "1",
                "media_type": "movie",
                "year": 2020,
            },
        ],
    )
    conn.executemany(
        """INSERT INTO media_files (path, inode, size_bytes, mtime, media_type, title,
                                     metadata_status, metadata_source, metadata_provider,
                                     metadata_fetched_at, first_seen_at, last_updated_at,
                                     parse_raw_name)
           VALUES (?, ?, 100, 1000, 'extra', 'Foo Extras', 'ok', 'tmdb', 'tmdb', 1000, 1000, 1000, ?)""",
        [
            ("/Movies/Foo/Foo.2020.Extras-01.mkv", 10, "Foo.2020.Extras-01.mkv"),
            ("/Movies/Foo/Foo.2020.Featurette.mkv", 11, "Foo.2020.Featurette.mkv"),
            # 子目录的不该被返回
            ("/Movies/Foo/sub/inner.Extras-01.mkv", 12, "inner.Extras-01.mkv"),
            # 别目录的不该被返回
            ("/Movies/Bar/Bar.2021.Extras-01.mkv", 13, "Bar.2021.Extras-01.mkv"),
        ],
    )
    conn.commit()
    extras = metadata_cache.list_extras_in_dir(conn, "/Movies/Foo")
    paths = sorted(e.path for e in extras)
    assert paths == [
        "/Movies/Foo/Foo.2020.Extras-01.mkv",
        "/Movies/Foo/Foo.2020.Featurette.mkv",
    ]


def test_list_extras_in_dir_empty_returns_empty(conn):
    assert metadata_cache.list_extras_in_dir(conn, "/nonexistent") == []
    # 边界：空字符串 / 单 slash 不能匹配全表
    assert metadata_cache.list_extras_in_dir(conn, "") == []
    assert metadata_cache.list_extras_in_dir(conn, "/") == []


def test_library_stats_basic_counts(conn):
    _seed_library(
        conn,
        [
            {
                "path": "/a.mkv",
                "tmdb_id": "1",
                "media_type": "movie",
                "year": 1995,
                "vote_average": 8.5,
            },
            {
                "path": "/b.mkv",
                "tmdb_id": "2",
                "media_type": "tv",
                "year": 2010,
                "vote_average": 7.5,
            },
            {
                "path": "/c.mkv",
                "tmdb_id": "3",
                "media_type": "movie",
                "year": 1998,
                "vote_average": 6.5,
            },
            {
                "path": "/d.mkv",
                "tmdb_id": "4",
                "media_type": "movie",
                "year": 2020,
                "vote_average": 9.2,
            },
        ],
    )
    stats = metadata_cache.get_library_stats(conn)
    assert stats["total"] == 4
    assert stats["by_media_type"] == {"movie": 3, "tv": 1}
    assert stats["by_decade"] == {1990: 2, 2010: 1, 2020: 1}
    # vote buckets
    assert stats["by_vote_bucket"]["8-9"] == 1
    assert stats["by_vote_bucket"]["7-8"] == 1
    assert stats["by_vote_bucket"]["6-7"] == 1
    assert stats["by_vote_bucket"]["9-10"] == 1


def test_library_stats_ignores_needs_review(conn):
    """stats 只算 ok 的，needs_review 不计入。"""
    _seed_library(conn, [{"path": "/a.mkv", "tmdb_id": "1"}])
    metadata_cache.upsert_identification(
        conn,
        path="/b.mkv",
        stat={"inode": 2, "size_bytes": 100, "mtime": 1000},
        identify_result=_make_result(top=None),
    )
    stats = metadata_cache.get_library_stats(conn)
    assert stats["total"] == 1


# ─── Phase 3.1: split tmdb id + HDR 子表 + 互斥 enforcement (3.0 carry-over) ───


def _make_movie_candidate(tmdb_id: str = "238") -> MediaCandidate:
    return _make_candidate(
        id=f"tmdb:movie:{tmdb_id}",
        external_ids={"tmdb_id": tmdb_id, "imdb_id": "tt0068646"},
        title="教父",
        original_title="The Godfather",
        year=1972,
        media_type="movie",
    )


def test_upsert_movie_writes_tmdb_movie_id_not_series(conn):
    res = _make_result(
        parse=_make_parse(media_type="movie", season=None, episode=None),
        top=_make_movie_candidate("238"),
    )
    metadata_cache.upsert_identification(
        conn,
        path="/share/godfather.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    row = conn.execute(
        "SELECT tmdb_movie_id, tmdb_series_id, tmdb_episode_id, tmdb_id FROM media_files WHERE path=?",
        ("/share/godfather.mkv",),
    ).fetchone()
    assert row["tmdb_movie_id"] == "238"
    assert row["tmdb_series_id"] is None  # 互斥
    assert row["tmdb_episode_id"] is None  # episode_id 仅 watched_items 写
    assert row["tmdb_id"] == "238"  # 老字段保留兼容


def test_upsert_tv_writes_tmdb_series_id_not_movie(conn):
    res = _make_result(top=_make_candidate())  # default candidate is tv (60625)
    metadata_cache.upsert_identification(
        conn,
        path="/share/rick.mkv",
        stat={"inode": 2, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    row = conn.execute(
        "SELECT tmdb_movie_id, tmdb_series_id, tmdb_episode_id FROM media_files WHERE path=?",
        ("/share/rick.mkv",),
    ).fetchone()
    assert row["tmdb_movie_id"] is None
    assert row["tmdb_series_id"] == "60625"
    assert row["tmdb_episode_id"] is None


def test_upsert_companion_extras_no_tmdb_ids(conn):
    """Extras 路径 → 所有 tmdb_*_id 都该是 NULL（互斥 + 不参与 join）。"""
    parse = _make_parse(media_type="extra", season=None, episode=None, hdr_profiles=[], codec=None)
    res = _make_result(parse=parse, top=None)
    metadata_cache.upsert_identification(
        conn,
        path="/share/Movie/Extras.mkv",
        stat={"inode": 3, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    row = conn.execute(
        "SELECT media_type, tmdb_movie_id, tmdb_series_id, tmdb_episode_id "
        "FROM media_files WHERE path=?",
        ("/share/Movie/Extras.mkv",),
    ).fetchone()
    assert row["media_type"] == "extra"
    assert row["tmdb_movie_id"] is None
    assert row["tmdb_series_id"] is None
    assert row["tmdb_episode_id"] is None


def test_upsert_writes_parse_quality_fields(conn):
    """parse_codec / parse_container / parse_color_depth / parse_audio_codec 应入库。"""
    parse = _make_parse(
        media_type="movie",
        season=None,
        episode=None,
        codec="H.265",
        color_depth="10-bit",
        container="mkv",
        audio_codec="Dolby TrueHD",
        hdr_profiles=["DolbyVision", "HDR10"],
    )
    res = _make_result(parse=parse, top=_make_movie_candidate("238"))
    metadata_cache.upsert_identification(
        conn,
        path="/share/q.mkv",
        stat={"inode": 4, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    row = conn.execute(
        "SELECT parse_codec, parse_color_depth, parse_container, parse_audio_codec "
        "FROM media_files WHERE path=?",
        ("/share/q.mkv",),
    ).fetchone()
    assert row["parse_codec"] == "H.265"
    assert row["parse_color_depth"] == "10-bit"
    assert row["parse_container"] == "mkv"
    assert row["parse_audio_codec"] == "Dolby TrueHD"


def test_upsert_writes_hdr_profiles_to_subtable(conn):
    parse = _make_parse(
        media_type="movie",
        season=None,
        episode=None,
        hdr_profiles=["DolbyVision", "HDR10"],
    )
    res = _make_result(parse=parse, top=_make_movie_candidate("238"))
    metadata_cache.upsert_identification(
        conn,
        path="/share/hdr.mkv",
        stat={"inode": 5, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    fid = conn.execute("SELECT id FROM media_files WHERE path=?", ("/share/hdr.mkv",)).fetchone()[
        "id"
    ]
    profiles = [
        r["profile"]
        for r in conn.execute(
            "SELECT profile FROM media_file_hdr_profiles WHERE media_file_id=? ORDER BY profile",
            (fid,),
        )
    ]
    assert profiles == ["DolbyVision", "HDR10"]


def test_upsert_clears_stale_hdr_profiles_on_reidentify(conn):
    """Re-identify 后 HDR 子表先 DELETE 再 INSERT；旧 profile 不残留。"""
    # 第一次：DolbyVision + HDR10
    parse1 = _make_parse(
        media_type="movie", season=None, episode=None, hdr_profiles=["DolbyVision", "HDR10"]
    )
    metadata_cache.upsert_identification(
        conn,
        path="/share/r.mkv",
        stat={"inode": 6, "size_bytes": 100, "mtime": 1000},
        identify_result=_make_result(parse=parse1, top=_make_movie_candidate("238")),
    )
    # 第二次：重剪版只 HDR10
    parse2 = _make_parse(media_type="movie", season=None, episode=None, hdr_profiles=["HDR10"])
    metadata_cache.upsert_identification(
        conn,
        path="/share/r.mkv",
        stat={"inode": 6, "size_bytes": 200, "mtime": 2000},
        identify_result=_make_result(parse=parse2, top=_make_movie_candidate("238")),
    )
    fid = conn.execute("SELECT id FROM media_files WHERE path=?", ("/share/r.mkv",)).fetchone()[
        "id"
    ]
    profiles = sorted(
        r["profile"]
        for r in conn.execute(
            "SELECT profile FROM media_file_hdr_profiles WHERE media_file_id=?", (fid,)
        )
    )
    assert profiles == ["HDR10"]  # DolbyVision 必须被清掉


def test_upsert_empty_hdr_profiles_results_in_no_subtable_rows(conn):
    parse = _make_parse(media_type="movie", season=None, episode=None, hdr_profiles=[])
    metadata_cache.upsert_identification(
        conn,
        path="/share/sdr.mkv",
        stat={"inode": 7, "size_bytes": 100, "mtime": 1000},
        identify_result=_make_result(parse=parse, top=_make_movie_candidate("238")),
    )
    cnt = conn.execute("SELECT COUNT(*) FROM media_file_hdr_profiles").fetchone()[0]
    assert cnt == 0


def test_split_tmdb_ids_helper_enforces_mutual_exclusion():
    """_split_tmdb_ids 是 carry-over 互斥 enforce 的 helper。"""
    assert metadata_cache._split_tmdb_ids("movie", "238") == ("238", None, None)
    assert metadata_cache._split_tmdb_ids("tv", "60625") == (None, "60625", None)
    assert metadata_cache._split_tmdb_ids("extra", "999") == (None, None, None)
    assert metadata_cache._split_tmdb_ids("part", "999") == (None, None, None)
    assert metadata_cache._split_tmdb_ids(None, "x") == (None, None, None)
    assert metadata_cache._split_tmdb_ids("movie", None) == (None, None, None)


# ── Phase 4B.1：get_many_by_path batch helper ──────────────────────


def test_get_many_empty_paths_returns_empty_dict(conn):
    result = metadata_cache.get_many_by_path(conn, [])
    assert result == {}


def test_get_many_all_miss_returns_none_for_each(conn):
    """没 seed 任何 row → 三个 path 都 (None, miss)。"""
    result = metadata_cache.get_many_by_path(conn, ["/a", "/b", "/c"])
    assert len(result) == 3
    assert all(cached is None and status == "miss" for cached, status in result.values())


def test_get_many_mixed_hit_and_miss(conn):
    """seed 2 个 path，查 3 个 → 2 hit + 1 miss。"""
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn,
        path="/share/movie.mkv",
        stat={"inode": 100, "size_bytes": 1000, "mtime": 2000},
        identify_result=res,
    )
    metadata_cache.upsert_identification(
        conn,
        path="/share/tv.s01e01.mkv",
        stat={"inode": 200, "size_bytes": 2000, "mtime": 3000},
        identify_result=res,
    )
    result = metadata_cache.get_many_by_path(
        conn, ["/share/movie.mkv", "/share/tv.s01e01.mkv", "/share/missing.mkv"]
    )
    assert result["/share/movie.mkv"][1] == "hit"
    assert result["/share/movie.mkv"][0] is not None
    assert result["/share/tv.s01e01.mkv"][1] == "hit"
    assert result["/share/missing.mkv"][0] is None
    assert result["/share/missing.mkv"][1] == "miss"


def test_get_many_detects_stale_on_mtime_change(conn):
    """current_stats 不一致 → 标 stale。"""
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn,
        path="/share/a.mkv",
        stat={"inode": 100, "size_bytes": 1000, "mtime": 2000},
        identify_result=res,
    )
    current = {"/share/a.mkv": {"inode": 100, "mtime": 9999}}  # mtime 变了
    result = metadata_cache.get_many_by_path(conn, ["/share/a.mkv"], current_stats=current)
    cached, status = result["/share/a.mkv"]
    assert status == "stale"
    assert cached is not None  # stale 仍返回 cached（caller 可判旧值）


def test_get_many_detects_stale_on_inode_change(conn):
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn,
        path="/share/a.mkv",
        stat={"inode": 100, "size_bytes": 1000, "mtime": 2000},
        identify_result=res,
    )
    current = {"/share/a.mkv": {"inode": 9999, "mtime": 2000}}  # inode 变了
    result = metadata_cache.get_many_by_path(conn, ["/share/a.mkv"], current_stats=current)
    assert result["/share/a.mkv"][1] == "stale"


def test_get_many_skips_stale_check_when_path_not_in_current_stats(conn):
    """current_stats 没传该 path 的 stat → 不做 stale 检测，按 hit 返回。
    （场景：src 文件已被删，SSH stat 返 exists=False，不进 current_stats）"""
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn,
        path="/share/a.mkv",
        stat={"inode": 100, "size_bytes": 1000, "mtime": 2000},
        identify_result=res,
    )
    result = metadata_cache.get_many_by_path(
        conn,
        ["/share/a.mkv"],
        current_stats={},  # 空 current_stats
    )
    assert result["/share/a.mkv"][1] == "hit"


def test_get_many_handles_more_than_chunk_size():
    """750 paths > 500 chunk_size → 拆 2 chunk，正确返回。

    SQLite SQLITE_MAX_VARIABLE_NUMBER 默认 999；用 750 path 强制走 2 chunk。
    """
    import tempfile

    from services import destructive_action as da

    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        c = da.open_connection(f.name)
        da.init_schema(c, SCHEMA_PATH)
        from db import migrations

        migrations.phase3_migrate(c)
        paths = [f"/share/file_{i:04d}.mkv" for i in range(750)]
        result = metadata_cache.get_many_by_path(c, paths)
        assert len(result) == 750
        for p in paths:
            assert result[p] == (None, "miss")
        c.close()


# ---------------- ensure_episode_details (ROADMAP #9) ---------------- #


class _FakeProvider:
    """Stub MetadataProvider — 记录 lookup_by_id 调用 + 返预设结果。"""

    def __init__(self, *, episode=None, raise_exc=None, returns_none=False):
        self._episode = episode
        self._raise = raise_exc
        self._none = returns_none
        self.calls: list[dict] = []

    def lookup_by_id(
        self, external_id, *, id_type="tmdb_id", media_type="movie", season=None, episode=None
    ):
        self.calls.append(
            {
                "external_id": external_id,
                "media_type": media_type,
                "season": season,
                "episode": episode,
            }
        )
        if self._raise:
            raise self._raise
        if self._none:
            return None
        cand = _make_candidate()
        return MediaDetails(
            candidate=cand,
            episode=self._episode,
            cast=[],
            genres=[],
            runtime_minutes=None,
        )

    def search(self, *a, **kw):  # 协议要求，不会被 ensure 调
        return []

    def test_connection(self):
        return {"ok": True, "message": "fake"}


def _seed_tv_episode(conn, path="/share/show.S01E01.mkv"):
    """创建一条 tv episode cache row，episode_* 字段都是 None。"""
    res = _make_result(top=_make_candidate())
    metadata_cache.upsert_identification(
        conn,
        path=path,
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    cached, _ = metadata_cache.get_by_path(conn, path)
    return cached


def test_ensure_episode_details_provider_none_returns_original(conn):
    cached = _seed_tv_episode(conn)
    out, drift = metadata_cache.ensure_episode_details(conn, None, cached)
    assert out is cached  # 完全 noop，不读 DB
    assert drift is False


def test_ensure_episode_details_skips_movie(conn):
    """movie cached 不该触发 lookup（episode 字段不适用）。"""
    movie_cand = _make_candidate(
        media_type="movie", title="Some Movie", external_ids={"tmdb_id": "1000"}
    )
    movie_parse = _make_parse(media_type="movie", season=None, episode=None)
    res = IdentifyResult(
        parse=movie_parse,
        candidates=[movie_cand],
        top_pick=movie_cand,
        confidence=0.95,
        reasoning="movie",
        pick_source="single_exact",
    )
    metadata_cache.upsert_identification(
        conn,
        path="/share/m.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    cached, _ = metadata_cache.get_by_path(conn, "/share/m.mkv")
    provider = _FakeProvider(episode={"overview": "X"})
    out, drift = metadata_cache.ensure_episode_details(conn, provider, cached)
    assert provider.calls == []  # 没调 TMDB
    assert out is cached
    assert drift is False


def test_ensure_episode_details_skips_missing_season(conn):
    """tv 但 season=None → 跳过（无法形成 /tv/{id}/season/{N}/episode/{N} 路径）。"""
    cached = _seed_tv_episode(conn)
    # 手动改 cache 模拟"识别后 season 没拿到"的边缘情况
    conn.execute("UPDATE media_files SET season_number = NULL WHERE path = ?", (cached.path,))
    conn.commit()
    cached, _ = metadata_cache.get_by_path(conn, cached.path)
    provider = _FakeProvider(episode={"overview": "X"})
    out, drift = metadata_cache.ensure_episode_details(conn, provider, cached)
    assert provider.calls == []
    assert drift is False


def test_ensure_episode_details_skips_missing_tmdb_id(conn):
    """needs_review row（tmdb_id NULL）即使有 season/episode 也不该 lookup。"""
    cached = _seed_tv_episode(conn)
    conn.execute("UPDATE media_files SET tmdb_id = NULL WHERE path = ?", (cached.path,))
    conn.commit()
    cached, _ = metadata_cache.get_by_path(conn, cached.path)
    provider = _FakeProvider(episode={"overview": "X"})
    out, drift = metadata_cache.ensure_episode_details(conn, provider, cached)
    assert provider.calls == []
    assert drift is False


def test_ensure_episode_details_skips_already_enriched(conn):
    """任一字段已填（哪怕只有 air_date） → 跳过，幂等保证。"""
    cached = _seed_tv_episode(conn)
    metadata_cache.upsert_details(
        conn,
        path=cached.path,
        episode_air_date="2022-01-01",  # 任一字段非 None 即 enriched
    )
    cached, _ = metadata_cache.get_by_path(conn, cached.path)
    provider = _FakeProvider(episode={"overview": "Should not see"})
    out, drift = metadata_cache.ensure_episode_details(conn, provider, cached)
    assert provider.calls == []
    assert out.episode_air_date == "2022-01-01"
    assert out.episode_overview is None  # 仍然 None，没被新值覆盖
    assert drift is False


def test_ensure_episode_details_fetches_and_persists(conn):
    """happy path: 缺字段 → 调 lookup → upsert_details → 返回 refreshed cached。"""
    cached = _seed_tv_episode(conn)
    assert cached.episode_overview is None
    provider = _FakeProvider(
        episode={
            "name": "Pilot",
            "overview": "Specific episode plot.",
            "air_date": "2013-12-02",
            "still_url": "https://image.tmdb.org/p/still.jpg",
        }
    )
    out, drift = metadata_cache.ensure_episode_details(conn, provider, cached)
    assert drift is False
    assert len(provider.calls) == 1
    call = provider.calls[0]
    assert call["external_id"] == "60625"
    assert call["media_type"] == "tv"
    assert call["season"] == 6
    assert call["episode"] == 2
    # 返回值是 refreshed cached，含新字段
    assert out.episode_overview == "Specific episode plot."
    assert out.episode_air_date == "2013-12-02"
    assert out.episode_still_url == "https://image.tmdb.org/p/still.jpg"
    # DB 真持久化（不是内存对象修补）
    db_cached, _ = metadata_cache.get_by_path(conn, cached.path)
    assert db_cached.episode_overview == "Specific episode plot."


def test_ensure_episode_details_provider_raises_returns_original(conn):
    """provider 抛错（网关挂 / 限流 / network） → 返回原 cached，不抛。"""
    cached = _seed_tv_episode(conn)
    provider = _FakeProvider(raise_exc=RuntimeError("TMDB 502"))
    out, drift = metadata_cache.ensure_episode_details(conn, provider, cached)
    assert out.episode_overview is None  # cache 不变
    assert out.path == cached.path
    assert drift is False  # provider 错不是 drift


def test_ensure_episode_details_lookup_returns_none(conn):
    """provider.lookup 返 None（剧 ID 无效） → 不抛，cache 不变。"""
    cached = _seed_tv_episode(conn)
    provider = _FakeProvider(returns_none=True)
    out, drift = metadata_cache.ensure_episode_details(conn, provider, cached)
    assert out.episode_overview is None
    assert drift is False


def test_ensure_episode_details_episode_none_returns_original(conn):
    """剧存在但 TMDB 某集没收录（details.episode is None）→ 不抛，cache 不变。"""
    cached = _seed_tv_episode(conn)
    provider = _FakeProvider(episode=None)  # MediaDetails returned but episode=None
    out, drift = metadata_cache.ensure_episode_details(conn, provider, cached)
    assert len(provider.calls) == 1  # lookup 真调了
    assert out.episode_overview is None  # 但 cache 没改
    assert drift is False


def test_ensure_episode_details_signals_drift_when_guarded_update_misses(conn):
    """codex r2 BLOCKER：drift_detected=True 强 signal 调用方，**不**靠 refreshed cached.

    模拟：fake provider 在 lookup_by_id 里把 cache 的 tmdb_id 改成另一个剧（模拟 scanner 改写）。
    guarded UPDATE WHERE 不匹配 → rowcount=0 → drift_detected=True → caller short-circuit。
    """
    cached = _seed_tv_episode(conn)

    class _DriftProvider:
        """lookup_by_id 调用期间偷偷改 cache row（模拟 scanner concurrent re-identify）。"""

        def __init__(self, db_conn):
            self._db = db_conn
            self.calls = 0

        def lookup_by_id(self, *a, **kw):
            self.calls += 1
            # 模拟外部把 cache 改成另一个剧
            self._db.execute(
                "UPDATE media_files SET tmdb_id = '99999' WHERE path = ?",
                (cached.path,),
            )
            self._db.commit()
            return MediaDetails(
                candidate=_make_candidate(),
                episode={
                    "name": "Old Ep",
                    "overview": "Should not land",
                    "air_date": "2013-01-01",
                    "still_url": "u",
                },
            )

    provider = _DriftProvider(conn)
    out, drift = metadata_cache.ensure_episode_details(conn, provider, cached)
    assert drift is True  # 关键：强 signal caller "drift detected"
    # cache 当前 tmdb_id 已经是 99999（被 fake scanner 改的），episode_overview 仍 None
    db_row = conn.execute(
        "SELECT tmdb_id, episode_overview, episode_air_date FROM media_files WHERE path = ?",
        (cached.path,),
    ).fetchone()
    assert db_row["tmdb_id"] == "99999"
    assert db_row["episode_overview"] is None  # 旧 episode 数据没被写到新 row
    assert db_row["episode_air_date"] is None


class _FlakyDBProxy:
    """Connection wrapper：UPDATE media_files 抛 OperationalError，其它 passthrough。

    sqlite3.Connection.execute 是 C-level read-only attribute，不能直接 monkeypatch，
    用 proxy 包一层模拟 DB 写失败。
    """

    def __init__(self, real):
        self._real = real

    def execute(self, sql, *a, **kw):
        if "UPDATE media_files" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *a, **kw)

    def commit(self):
        return self._real.commit()

    def rollback(self):
        return self._real.rollback()


def test_ensure_episode_details_db_error_returns_original(conn):
    """upsert/update 阶段 SQLite 抛错 → catch 后返原 cached，不让 hardlink success path crash。"""
    cached = _seed_tv_episode(conn)
    provider = _FakeProvider(
        episode={
            "name": "Pilot",
            "overview": "X",
            "air_date": "2013-01-01",
            "still_url": "u",
        }
    )
    flaky = _FlakyDBProxy(conn)
    out, drift = metadata_cache.ensure_episode_details(flaky, provider, cached)
    # 不抛，返回的是原 cached（DB 没写入）
    assert out.episode_overview is None
    assert drift is False  # DB error 不是 drift（caller 沿用原 cached 即可）
    db_row = conn.execute(
        "SELECT episode_overview FROM media_files WHERE path = ?",
        (cached.path,),
    ).fetchone()
    assert db_row["episode_overview"] is None
