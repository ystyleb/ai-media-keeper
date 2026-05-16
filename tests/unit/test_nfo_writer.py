"""nfo_writer 单元测试：XML round-trip 验证生成的 NFO 能被 _parse_emby_nfo 解析回来。"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from services.nfo_writer import (
    NFOPayload,
    build_episode_nfo,
    build_movie_nfo,
    build_nfo,
    build_tvshow_nfo,
    nfo_path_for_video,
)


def _parse(xml: str) -> ET.Element:
    """裸 ET.fromstring，去掉前导 BOM / xml declaration。"""
    stripped = xml.lstrip("﻿").strip()
    return ET.fromstring(stripped)


# ---------------- nfo_path_for_video ---------------- #


def test_nfo_path_mkv():
    assert nfo_path_for_video("/a/b/Show.S01E01.mkv") == "/a/b/Show.S01E01.nfo"


def test_nfo_path_dotted_name():
    assert (
        nfo_path_for_video("/a/Rick.and.Morty.S06E02.1080p.WEB-DL.mkv")
        == "/a/Rick.and.Morty.S06E02.1080p.WEB-DL.nfo"
    )


def test_nfo_path_no_extension():
    assert nfo_path_for_video("/a/b/somefile") == "/a/b/somefile.nfo"


# ---------------- build_episode_nfo ---------------- #


def _episode_payload(**overrides) -> NFOPayload:
    defaults = dict(
        media_type="episode",
        title="瑞克和莫蒂",
        original_title="Rick and Morty",
        year=2013,
        plot="A misfit family across the multiverse.",
        tmdb_id="60625",
        imdb_id="tt2861424",
        tvdb_id="275274",
        rating=8.7,
        genres=["Animation", "Comedy"],
        cast=["Justin Roiland", "Chris Parnell"],
        runtime_minutes=22,
        poster_url="https://image.tmdb.org/abc.jpg",
        season=6,
        episode=2,
        episode_title="Rick - A Mort Well Lived",
        episode_overview="Summer is trapped in a video game.",
        episode_air_date="2022-09-11",
        episode_still_url="https://image.tmdb.org/still.jpg",
    )
    defaults.update(overrides)
    return NFOPayload(**defaults)


def test_episode_nfo_root_element():
    xml = build_episode_nfo(_episode_payload())
    root = _parse(xml)
    assert root.tag == "episodedetails"


def test_episode_nfo_basic_fields():
    xml = build_episode_nfo(_episode_payload())
    root = _parse(xml)
    assert root.findtext("title") == "Rick - A Mort Well Lived"
    assert root.findtext("originaltitle") == "Rick and Morty"
    assert root.findtext("showtitle") == "瑞克和莫蒂"
    assert root.findtext("season") == "6"
    assert root.findtext("episode") == "2"
    assert root.findtext("plot") == "Summer is trapped in a video game."
    assert root.findtext("aired") == "2022-09-11"
    assert root.findtext("rating") == "8.7"
    assert root.findtext("runtime") == "22"


def test_episode_nfo_uniqueid_double_write():
    """tmdb / imdb 都该有 <uniqueid> + 旧式 <tmdbid>/<imdbid>。"""
    xml = build_episode_nfo(_episode_payload())
    root = _parse(xml)
    uniqueids = {u.get("type"): u.text for u in root.findall("uniqueid")}
    assert uniqueids["tmdb"] == "60625"
    assert uniqueids["imdb"] == "tt2861424"
    assert uniqueids["tvdb"] == "275274"
    assert root.findtext("tmdbid") == "60625"
    assert root.findtext("imdbid") == "tt2861424"
    # 仅 tmdb 标 default
    tmdb_el = next(u for u in root.findall("uniqueid") if u.get("type") == "tmdb")
    assert tmdb_el.get("default") == "true"


def test_episode_nfo_actors_and_genres():
    xml = build_episode_nfo(_episode_payload())
    root = _parse(xml)
    actors = [a.findtext("name") for a in root.findall("actor")]
    assert actors == ["Justin Roiland", "Chris Parnell"]
    genres = [g.text for g in root.findall("genre")]
    assert genres == ["Animation", "Comedy"]


def test_episode_nfo_fallback_title_when_no_episode_title():
    """没拿到单集标题时 fallback 到 '剧名 SxxExx'。"""
    p = _episode_payload(episode_title=None)
    xml = build_episode_nfo(p)
    root = _parse(xml)
    assert root.findtext("title") == "瑞克和莫蒂 S06E02"


def test_episode_nfo_thumb_prefers_episode_still():
    xml = build_episode_nfo(_episode_payload())
    assert _parse(xml).findtext("thumb") == "https://image.tmdb.org/still.jpg"


def test_episode_nfo_thumb_falls_back_to_poster():
    p = _episode_payload(episode_still_url=None)
    xml = build_episode_nfo(p)
    assert _parse(xml).findtext("thumb") == "https://image.tmdb.org/abc.jpg"


def test_episode_nfo_no_empty_tags_for_missing_fields():
    """缺字段时不写空 tag（Emby 会把 <plot></plot> 当成空字符串覆盖现有数据）。"""
    p = _episode_payload(plot=None, episode_overview=None, imdb_id=None, tvdb_id=None)
    xml = build_episode_nfo(p)
    root = _parse(xml)
    assert root.find("plot") is None
    assert root.find("imdbid") is None
    assert all(u.get("type") != "imdb" for u in root.findall("uniqueid"))


# ---------------- build_movie_nfo ---------------- #


def _movie_payload(**overrides) -> NFOPayload:
    defaults = dict(
        media_type="movie",
        title="盗梦空间",
        original_title="Inception",
        year=2010,
        plot="A thief who enters dreams.",
        tmdb_id="27205",
        imdb_id="tt1375666",
        tvdb_id=None,
        rating=8.4,
        genres=["Action", "Science Fiction"],
        cast=["Leonardo DiCaprio"],
        runtime_minutes=148,
        poster_url="https://image.tmdb.org/inception.jpg",
    )
    defaults.update(overrides)
    return NFOPayload(**defaults)


def test_movie_nfo_root_element():
    xml = build_movie_nfo(_movie_payload())
    assert _parse(xml).tag == "movie"


def test_movie_nfo_basic_fields():
    xml = build_movie_nfo(_movie_payload())
    root = _parse(xml)
    assert root.findtext("title") == "盗梦空间"
    assert root.findtext("originaltitle") == "Inception"
    assert root.findtext("year") == "2010"
    assert root.findtext("plot") == "A thief who enters dreams."
    assert root.findtext("rating") == "8.4"
    assert root.findtext("runtime") == "148"
    assert root.findtext("thumb") == "https://image.tmdb.org/inception.jpg"
    assert root.findtext("tmdbid") == "27205"
    assert root.findtext("imdbid") == "tt1375666"


# ---------------- build_tvshow_nfo ---------------- #


def test_tvshow_nfo_root_element():
    p = _episode_payload(media_type="tvshow")
    xml = build_tvshow_nfo(p)
    root = _parse(xml)
    assert root.tag == "tvshow"
    # tvshow 级不带 season/episode 字段
    assert root.find("season") is None
    assert root.find("episode") is None


def test_tvshow_nfo_premiered_from_year():
    p = _episode_payload(media_type="tvshow", year=2013)
    xml = build_tvshow_nfo(p)
    assert _parse(xml).findtext("premiered") == "2013-01-01"


# ---------------- build_nfo dispatch ---------------- #


def test_build_nfo_dispatches_by_media_type():
    assert _parse(build_nfo(_episode_payload())).tag == "episodedetails"
    assert _parse(build_nfo(_movie_payload())).tag == "movie"
    assert _parse(build_nfo(_episode_payload(media_type="tvshow"))).tag == "tvshow"


def test_build_nfo_unknown_media_type_raises():
    with pytest.raises(ValueError, match="unknown media_type"):
        build_nfo(_episode_payload(media_type="bogus"))


# ---------------- 与 _parse_emby_nfo round-trip 验证 ---------------- #


def test_round_trip_with_emby_parser():
    """生成的 NFO 应该能被 app._parse_emby_nfo 解析回所有关键字段。"""

    # 在测试里 inline 一份 _parse_emby_nfo（避免 import app.py 触发 Flask + DB 初始化）
    def parse_emby(text: str) -> dict | None:
        stripped = text.lstrip("﻿").strip()
        if not stripped.startswith("<"):
            return None
        root = ET.fromstring(stripped)
        if root.tag not in ("episodedetails", "tvshow", "movie"):
            return None

        def g(t: str) -> str:
            el = root.find(t)
            return (el.text or "").strip() if el is not None and el.text else ""

        return {
            "type": root.tag,
            "title": g("title"),
            "tmdb_id": g("tmdbid"),
            "imdb_id": g("imdbid") or g("uniqueid"),
            "year": g("year"),
            "season": g("season"),
            "episode": g("episode"),
            "plot": g("plot"),
        }

    xml = build_episode_nfo(_episode_payload())
    parsed = parse_emby(xml)
    assert parsed is not None
    assert parsed["type"] == "episodedetails"
    assert parsed["title"] == "Rick - A Mort Well Lived"
    assert parsed["tmdb_id"] == "60625"
    assert parsed["imdb_id"] == "tt2861424"
    assert parsed["year"] == "2013"
    assert parsed["season"] == "6"
    assert parsed["episode"] == "2"
    assert parsed["plot"] == "Summer is trapped in a video game."
