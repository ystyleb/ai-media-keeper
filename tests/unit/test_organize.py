"""Phase 4A: services/organize.py 路径推断单测。

覆盖 sanitize 各种字符 / movie 完整 + 无 year + title 已含 year /
tv 完整 + 缺 episode/season / 不支持 media_type / S 补零 /
tvshow.nfo 路径在 series 根而非 season 目录。

所有测试纯函数，不需要 SSH / DB / fixture conn。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from services.organize import (
    OrganizeNotApplicable,
    compute_organize_plan,
    sanitize_for_path,
)


@dataclass
class _FakeCached:
    """Duck-typed CachedMetadata stand-in for unit tests."""
    title: str
    year: int | None = None
    media_type: str = "movie"
    tmdb_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None


# ── sanitize_for_path ──────────────────────────────────────────


def test_sanitize_replaces_slash_and_colon():
    assert sanitize_for_path("Mr. Robot: Hello/World") == "Mr. Robot- Hello-World"


def test_sanitize_collapses_multiple_spaces():
    assert sanitize_for_path("Movie    Title   2024") == "Movie Title 2024"


def test_sanitize_strips_trailing_dots():
    assert sanitize_for_path("Movie...") == "Movie"
    assert sanitize_for_path("Title .") == "Title"


def test_sanitize_preserves_chinese_chars():
    assert sanitize_for_path("瑞克和莫蒂") == "瑞克和莫蒂"
    assert sanitize_for_path("大时代") == "大时代"


def test_sanitize_empty_returns_empty():
    assert sanitize_for_path("") == ""
    assert sanitize_for_path("   ") == ""


def test_sanitize_strips_control_chars():
    # 控制字符（\x00, \x1f）被替换为 '-'；末尾 '-' 不被 rstrip 去掉（rstrip 只针对 dot/space）
    assert sanitize_for_path("Movie\x00Title\x1f") == "Movie-Title-"


def test_sanitize_handles_pipe_and_wildcards():
    assert sanitize_for_path('Movie?<>|"*') == "Movie------"


# ── compute_organize_plan: movie ───────────────────────────────


MOVIES = "/media/movies"
TV = "/media/tv"


def test_plan_movie_with_year():
    cached = _FakeCached(title="The Godfather", year=1972, media_type="movie", tmdb_id="238")
    plan = compute_organize_plan(
        "/downloads/Godfather.1972.1080p.BluRay.x264-CLASSIC.mkv",
        cached, MOVIES, TV,
    )
    assert plan.media_type == "movie"
    assert plan.dst_dir == "/media/movies/The Godfather (1972)"
    assert plan.dst_path == "/media/movies/The Godfather (1972)/Godfather.1972.1080p.BluRay.x264-CLASSIC.mkv"
    assert plan.nfo_path == "/media/movies/The Godfather (1972)/Godfather.1972.1080p.BluRay.x264-CLASSIC.nfo"
    assert plan.tvshow_nfo_path is None
    assert plan.tmdb_id == "238"


def test_plan_movie_no_year_uses_title_only():
    cached = _FakeCached(title="Old Film", year=None, media_type="movie")
    plan = compute_organize_plan("/dl/Old.Film.mkv", cached, MOVIES, TV)
    assert plan.dst_dir == "/media/movies/Old Film"
    assert plan.dst_path == "/media/movies/Old Film/Old.Film.mkv"


def test_plan_movie_title_already_has_year_no_double_append():
    """已有 (2024) 后缀的 title 不应被追加成 'Movie (2024) (2024)'."""
    cached = _FakeCached(title="The Movie (2024)", year=2024, media_type="movie")
    plan = compute_organize_plan("/dl/X.mkv", cached, MOVIES, TV)
    assert plan.dst_dir == "/media/movies/The Movie (2024)"


def test_plan_movie_sanitizes_unsafe_title_chars():
    cached = _FakeCached(title="Movie: Subtitle/Part", year=2024, media_type="movie")
    plan = compute_organize_plan("/dl/X.mkv", cached, MOVIES, TV)
    assert plan.dst_dir == "/media/movies/Movie- Subtitle-Part (2024)"


def test_plan_movie_strips_trailing_slash_in_root():
    cached = _FakeCached(title="Foo", year=2020, media_type="movie")
    plan = compute_organize_plan("/dl/X.mkv", cached, MOVIES + "/", TV + "/")
    assert plan.dst_dir == "/media/movies/Foo (2020)"


# ── compute_organize_plan: tv ──────────────────────────────────


def test_plan_tv_full():
    cached = _FakeCached(
        title="Rick and Morty", year=2013, media_type="tv", tmdb_id="60625",
        season_number=4, episode_number=10,
    )
    plan = compute_organize_plan(
        "/downloads/RickAndMorty.S04E10.1080p.WEB-DL.x265.mkv",
        cached, MOVIES, TV,
    )
    assert plan.media_type == "tv"
    assert plan.dst_dir == "/media/tv/Rick and Morty (2013)/Season 04"
    assert plan.dst_path == (
        "/media/tv/Rick and Morty (2013)/Season 04/"
        "RickAndMorty.S04E10.1080p.WEB-DL.x265.mkv"
    )
    assert plan.nfo_path == (
        "/media/tv/Rick and Morty (2013)/Season 04/"
        "RickAndMorty.S04E10.1080p.WEB-DL.x265.nfo"
    )
    assert plan.tvshow_nfo_path == "/media/tv/Rick and Morty (2013)/tvshow.nfo"
    assert plan.season_number == 4 and plan.episode_number == 10


def test_plan_tv_missing_episode_raises():
    cached = _FakeCached(
        title="Show", year=2020, media_type="tv",
        season_number=2, episode_number=None,
    )
    with pytest.raises(OrganizeNotApplicable, match="season \\+ episode"):
        compute_organize_plan("/dl/X.mkv", cached, MOVIES, TV)


def test_plan_tv_missing_season_raises():
    cached = _FakeCached(
        title="Show", year=2020, media_type="tv",
        season_number=None, episode_number=5,
    )
    with pytest.raises(OrganizeNotApplicable):
        compute_organize_plan("/dl/X.mkv", cached, MOVIES, TV)


def test_plan_tv_season_padded():
    """单位数 season / episode 都补零到 2 位（Plex/Emby 标准）。"""
    cached = _FakeCached(
        title="Foo", year=2020, media_type="tv",
        season_number=2, episode_number=5,
    )
    plan = compute_organize_plan("/dl/X.mkv", cached, MOVIES, TV)
    assert "Season 02" in plan.dst_dir
    assert plan.season_number == 2  # numeric 不变


def test_plan_tv_tvshow_nfo_at_series_root_not_season():
    """tvshow.nfo 必须落在 series root（一剧一份），不在每个 season 目录里。"""
    cached = _FakeCached(
        title="Show", year=2020, media_type="tv",
        season_number=3, episode_number=8,
    )
    plan = compute_organize_plan("/dl/X.mkv", cached, MOVIES, TV)
    assert plan.tvshow_nfo_path == "/media/tv/Show (2020)/tvshow.nfo"
    # 确认不在 season 目录里
    assert "Season" not in plan.tvshow_nfo_path


# ── compute_organize_plan: 错误路径 ────────────────────────────


def test_plan_unsupported_media_type_raises():
    cached = _FakeCached(title="X", media_type="anime")
    with pytest.raises(OrganizeNotApplicable, match="unsupported media_type"):
        compute_organize_plan("/dl/X.mkv", cached, MOVIES, TV)


def test_plan_empty_title_after_sanitize_raises():
    """sanitize 后 title 为空 → raise（防止生成 dst_dir='/media/movies/(2024)'）。"""
    cached = _FakeCached(title="   ", year=2024, media_type="movie")
    with pytest.raises(OrganizeNotApplicable, match="title is empty"):
        compute_organize_plan("/dl/X.mkv", cached, MOVIES, TV)
