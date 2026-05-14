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
    # Phase 3 migration (adds 10 media_files columns + new tables)
    from db import migrations
    migrations.phase3_migrate(c)
    yield c
    c.close()


def _make_parse(**overrides) -> FilenameParse:
    defaults = dict(
        raw_name="Show.S06E02.mkv", title="Show", year=2022,
        season=6, episode=2, episode_title="Ep Name",
        media_type="episode", resolution="1080p",
        source="WEB-DL", release_group="RG",
        codec=None, color_depth=None, hdr_profiles=[], container=None, audio_codec=None,
        raw={},
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
            conn, path=it["path"],
            stat={"inode": 1, "size_bytes": 100, "mtime": it.get("mtime", 1000)},
            identify_result=res,
        )


def test_query_library_filters_media_type(conn):
    _seed_library(conn, [
        {"path": "/a.mkv", "tmdb_id": "1", "media_type": "movie", "year": 2020},
        {"path": "/b.mkv", "tmdb_id": "2", "media_type": "tv", "year": 2021},
        {"path": "/c.mkv", "tmdb_id": "3", "media_type": "movie", "year": 2022},
    ])
    items, total = metadata_cache.query_library(conn, media_type="movie")
    assert total == 2
    assert all(i.media_type == "movie" for i in items)


def test_query_library_filters_year_range(conn):
    _seed_library(conn, [
        {"path": "/old.mkv", "tmdb_id": "1", "year": 1995},
        {"path": "/mid.mkv", "tmdb_id": "2", "year": 2010},
        {"path": "/new.mkv", "tmdb_id": "3", "year": 2022},
    ])
    items, total = metadata_cache.query_library(conn, year_from=2000, year_to=2015)
    assert total == 1
    assert items[0].year == 2010


def test_query_library_query_matches_title_and_original(conn):
    """模糊匹配应该 title (zh-CN) 和 original_title (en) 都命中。"""
    _seed_library(conn, [
        {"path": "/a.mkv", "tmdb_id": "1", "title": "瑞克和莫蒂",
         "original_title": "Rick and Morty"},
        {"path": "/b.mkv", "tmdb_id": "2", "title": "星际穿越",
         "original_title": "Interstellar"},
        {"path": "/c.mkv", "tmdb_id": "3", "title": "Other",
         "original_title": "Other"},
    ])
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
    _seed_library(conn, [
        {"path": "/a.mkv", "tmdb_id": "1", "year": 1995},
        {"path": "/b.mkv", "tmdb_id": "2", "year": 2022},
        {"path": "/c.mkv", "tmdb_id": "3", "year": 2010},
    ])
    items, _ = metadata_cache.query_library(conn, sort="year_desc")
    assert [i.year for i in items] == [2022, 2010, 1995]


def test_query_library_sort_vote_desc(conn):
    _seed_library(conn, [
        {"path": "/a.mkv", "tmdb_id": "1", "vote_average": 6.5},
        {"path": "/b.mkv", "tmdb_id": "2", "vote_average": 9.0},
        {"path": "/c.mkv", "tmdb_id": "3", "vote_average": 7.5},
    ])
    items, _ = metadata_cache.query_library(conn, sort="vote_desc")
    assert [i.vote_average for i in items] == [9.0, 7.5, 6.5]


def test_query_library_limit_offset(conn):
    _seed_library(conn, [
        {"path": f"/{i}.mkv", "tmdb_id": str(i), "year": 2000 + i}
        for i in range(10)
    ])
    items, total = metadata_cache.query_library(conn, sort="year_desc", limit=3, offset=2)
    assert total == 10
    assert len(items) == 3
    # year_desc → [2009, 2008, 2007, 2006, ...]; offset 2 limit 3 → [2007, 2006, 2005]
    assert [i.year for i in items] == [2007, 2006, 2005]


def test_query_library_excludes_needs_review(conn):
    # 一个 ok + 一个 needs_review
    _seed_library(conn, [{"path": "/a.mkv", "tmdb_id": "1"}])
    metadata_cache.upsert_identification(
        conn, path="/b.mkv",
        stat={"inode": 2, "size_bytes": 100, "mtime": 1000},
        identify_result=_make_result(top=None),  # needs_review
    )
    items, total = metadata_cache.query_library(conn)
    assert total == 1
    assert items[0].path == "/a.mkv"


def test_query_library_excludes_extras_by_default(conn):
    """media_type='extra' 默认从库视图过滤。"""
    # 用 raw upsert 模拟扫描时检测到的 extra（直接 insert）
    _seed_library(conn, [
        {"path": "/Movies/Foo/Foo.2020.mkv", "tmdb_id": "1", "media_type": "movie", "year": 2020},
    ])
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
    _seed_library(conn, [
        {"path": "/Movies/Foo/Foo.2020.mkv", "tmdb_id": "1", "media_type": "movie", "year": 2020},
    ])
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
    _seed_library(conn, [
        {"path": "/a.mkv", "tmdb_id": "1", "media_type": "movie", "year": 1995, "vote_average": 8.5},
        {"path": "/b.mkv", "tmdb_id": "2", "media_type": "tv", "year": 2010, "vote_average": 7.5},
        {"path": "/c.mkv", "tmdb_id": "3", "media_type": "movie", "year": 1998, "vote_average": 6.5},
        {"path": "/d.mkv", "tmdb_id": "4", "media_type": "movie", "year": 2020, "vote_average": 9.2},
    ])
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
        conn, path="/b.mkv",
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
        title="教父", original_title="The Godfather",
        year=1972, media_type="movie",
    )


def test_upsert_movie_writes_tmdb_movie_id_not_series(conn):
    res = _make_result(
        parse=_make_parse(media_type="movie", season=None, episode=None),
        top=_make_movie_candidate("238"),
    )
    metadata_cache.upsert_identification(
        conn, path="/share/godfather.mkv",
        stat={"inode": 1, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    row = conn.execute(
        "SELECT tmdb_movie_id, tmdb_series_id, tmdb_episode_id, tmdb_id FROM media_files WHERE path=?",
        ("/share/godfather.mkv",),
    ).fetchone()
    assert row["tmdb_movie_id"] == "238"
    assert row["tmdb_series_id"] is None        # 互斥
    assert row["tmdb_episode_id"] is None       # episode_id 仅 watched_items 写
    assert row["tmdb_id"] == "238"              # 老字段保留兼容


def test_upsert_tv_writes_tmdb_series_id_not_movie(conn):
    res = _make_result(top=_make_candidate())  # default candidate is tv (60625)
    metadata_cache.upsert_identification(
        conn, path="/share/rick.mkv",
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
    parse = _make_parse(media_type="extra", season=None, episode=None,
                         hdr_profiles=[], codec=None)
    res = _make_result(parse=parse, top=None)
    metadata_cache.upsert_identification(
        conn, path="/share/Movie/Extras.mkv",
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
        media_type="movie", season=None, episode=None,
        codec="H.265", color_depth="10-bit",
        container="mkv", audio_codec="Dolby TrueHD",
        hdr_profiles=["DolbyVision", "HDR10"],
    )
    res = _make_result(parse=parse, top=_make_movie_candidate("238"))
    metadata_cache.upsert_identification(
        conn, path="/share/q.mkv",
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
        media_type="movie", season=None, episode=None,
        hdr_profiles=["DolbyVision", "HDR10"],
    )
    res = _make_result(parse=parse, top=_make_movie_candidate("238"))
    metadata_cache.upsert_identification(
        conn, path="/share/hdr.mkv",
        stat={"inode": 5, "size_bytes": 100, "mtime": 1000},
        identify_result=res,
    )
    fid = conn.execute(
        "SELECT id FROM media_files WHERE path=?", ("/share/hdr.mkv",)
    ).fetchone()["id"]
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
    parse1 = _make_parse(media_type="movie", season=None, episode=None,
                          hdr_profiles=["DolbyVision", "HDR10"])
    metadata_cache.upsert_identification(
        conn, path="/share/r.mkv",
        stat={"inode": 6, "size_bytes": 100, "mtime": 1000},
        identify_result=_make_result(parse=parse1, top=_make_movie_candidate("238")),
    )
    # 第二次：重剪版只 HDR10
    parse2 = _make_parse(media_type="movie", season=None, episode=None,
                          hdr_profiles=["HDR10"])
    metadata_cache.upsert_identification(
        conn, path="/share/r.mkv",
        stat={"inode": 6, "size_bytes": 200, "mtime": 2000},
        identify_result=_make_result(parse=parse2, top=_make_movie_candidate("238")),
    )
    fid = conn.execute(
        "SELECT id FROM media_files WHERE path=?", ("/share/r.mkv",)
    ).fetchone()["id"]
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
        conn, path="/share/sdr.mkv",
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
