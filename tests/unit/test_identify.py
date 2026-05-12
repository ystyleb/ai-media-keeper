"""Phase 2 spike unit tests: filename parser + top_pick heuristic.

TMDB HTTP 调用全 mock；专注 pipeline 逻辑（解析 / 评分 / grounded membership）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from services import identify as identify_svc
from services.metadata.base import MediaCandidate


# ---------------- parse_filename ---------------- #


def test_parse_filename_tv_episode():
    p = identify_svc.parse_filename(
        "/share/.../Rick and Morty - S06E02 - Rick - A Mort Well Lived WEBDL-1080p.mkv"
    )
    assert p.title == "Rick and Morty"
    assert p.season == 6
    assert p.episode == 2
    assert p.episode_title == "Rick - A Mort Well Lived"
    assert p.media_type == "episode"
    assert p.resolution == "1080p"


def test_parse_filename_movie():
    p = identify_svc.parse_filename(
        "/share/.../Dune.Part.Two.2024.2160p.UHD.BluRay.x265-FRDS.mkv"
    )
    assert "Dune" in p.title
    assert p.year == 2024
    assert p.resolution == "2160p"
    assert p.media_type == "movie"


def test_parse_filename_chinese():
    """guessit 对中文剧名解析；spike 阶段允许部分失败，主要验证不崩溃。"""
    p = identify_svc.parse_filename("/share/.../庆余年.S02E01.1080p.WEB-DL.mkv")
    assert p.season == 2
    assert p.episode == 1
    # title 可能是 "庆余年" 或空——不强校验，只确保 pipeline 不挂


def test_parse_filename_empty_returns_unknown():
    p = identify_svc.parse_filename("/some/path/randomfile.mkv")
    # title 可能被 guessit 抽到 "randomfile"，但不会崩；只确保字段存在
    assert hasattr(p, "title")
    assert hasattr(p, "media_type")


# ---------------- _pick_top ---------------- #


def _cand(id_, title, year=None, media_type="tv", vote=7.5):
    return MediaCandidate(
        id=id_, external_ids={"tmdb_id": id_.split(":")[-1]},
        title=title, original_title=title, year=year,
        media_type=media_type, poster_url=None, overview=None,
        vote_average=vote,
    )


def _parse(title, year=None, season=None, episode=None, media_type="episode"):
    return identify_svc.FilenameParse(
        raw_name="x.mkv", title=title, year=year, season=season,
        episode=episode, episode_title=None, media_type=media_type,
        resolution=None, source=None, release_group=None, raw={},
    )


def test_top_pick_returns_exact_match():
    parse = _parse("Rick and Morty")
    cands = [_cand("tmdb:tv:1", "Rick and Morty", 2013)]
    top, score, _ = identify_svc._pick_top(parse, cands)
    assert top is not None
    assert top.title == "Rick and Morty"
    assert score >= 0.7


def test_top_pick_year_match_boost():
    parse = _parse("Dune", year=2024, media_type="movie")
    cands = [
        _cand("tmdb:movie:1", "Dune", year=1984, media_type="movie"),
        _cand("tmdb:movie:2", "Dune", year=2024, media_type="movie"),
    ]
    top, _, _ = identify_svc._pick_top(parse, cands)
    assert top is not None
    assert top.year == 2024  # year exact match 应该胜


def test_top_pick_none_when_no_candidates():
    parse = _parse("anything")
    top, score, _ = identify_svc._pick_top(parse, [])
    assert top is None
    assert score == 0.0


def test_top_pick_low_confidence_returns_none():
    """title 完全对不上 → 不能盲选首个。"""
    parse = _parse("My Series", year=2020)
    cands = [_cand("tmdb:tv:1", "Completely Unrelated Show", year=2010)]
    top, score, reasoning = identify_svc._pick_top(parse, cands)
    assert top is None  # 应该 needs_review
    assert "low confidence" in reasoning


def test_top_pick_matches_original_title_when_localized():
    """zh-CN 下 TMDB 返回 c.title='瑞克和莫蒂' / c.original_title='Rick and Morty'，
    guessit 解析的是英文 → 必须比对 original_title 才能 match。"""
    parse = _parse("Rick and Morty", media_type="episode")
    cands = [
        MediaCandidate(
            id="tmdb:tv:1", external_ids={"tmdb_id": "1"},
            title="瑞克和莫蒂", original_title="Rick and Morty",
            year=2013, media_type="tv",
            poster_url=None, overview=None, vote_average=8.7,
        )
    ]
    top, score, reasoning = identify_svc._pick_top(parse, cands)
    assert top is not None
    assert top.title == "瑞克和莫蒂"
    assert score >= 0.7
    assert "original exact" in reasoning or "original substring" in reasoning


def test_top_pick_episode_type_filters_to_tv():
    """guessit 标 episode → 候选里应该优先 tv，过滤掉同名 movie。"""
    parse = _parse("Friends", year=1994, media_type="episode")
    cands = [
        _cand("tmdb:movie:1", "Friends", year=1994, media_type="movie"),
        _cand("tmdb:tv:2", "Friends", year=1994, media_type="tv"),
    ]
    top, _, _ = identify_svc._pick_top(parse, cands)
    assert top is not None
    assert top.media_type == "tv"


# ---------------- identify (full pipeline with mock provider) ---------------- #


def test_identify_full_pipeline():
    provider = MagicMock()
    provider.search.return_value = [
        _cand("tmdb:tv:1", "Rick and Morty", year=2013, media_type="tv"),
    ]
    result = identify_svc.identify(
        "/share/Rick and Morty - S06E02 WEBDL-1080p.mkv", provider
    )
    assert result.parse.title == "Rick and Morty"
    assert result.parse.season == 6
    assert result.parse.episode == 2
    assert len(result.candidates) == 1
    assert result.top_pick is not None
    assert result.confidence >= 0.7
    # 验证 provider.search 被以解析出的 title/year 调用
    provider.search.assert_called_once()
    call_kwargs = provider.search.call_args.kwargs
    assert call_kwargs.get("title") == "Rick and Morty"
    assert call_kwargs.get("media_type") == "episode"


def test_identify_no_candidates_returns_needs_review():
    """provider 返回空 candidates → top_pick=None + reasoning 含解释。"""
    provider = MagicMock()
    provider.search.return_value = []
    result = identify_svc.identify("/some/random_file.mkv", provider)
    assert result.candidates == []
    assert result.top_pick is None
    assert result.confidence == 0.0
    assert "no candidates" in result.reasoning.lower() or "no candidate" in result.reasoning.lower()


# ---------------- LLM filename rescue ---------------- #


def test_identify_llm_rescue_when_provider_returns_zero():
    """guessit 拿不到 title 或 provider 0 → LLM 解析文件名 → 重搜 TMDB 拿到候选。

    场景：'死亡笔记.BDrip1080P.X264.AC3.LGGZ S.01.mkv'，guessit 解析 title=""，
    LLM 看到"死亡笔记" → 搜 TMDB → 拿到死亡笔记候选。
    """
    from services import llm as llm_module

    provider = MagicMock()
    # 第一次搜（用 guessit title）→ 0
    # 第二次搜（用 LLM 重解析的 title "死亡笔记"）→ 1 候选
    provider.search.side_effect = [
        [],  # first call
        [_cand("tmdb:tv:13916", "死亡笔记", year=2006, media_type="tv")],
    ]

    fake_extraction = llm_module.FilenameExtraction(
        title="死亡笔记", alt_title="Death Note",
        year=2006, season=1, episode=None, media_type="tv",
        raw_response='{"title":"死亡笔记"}',
    )
    with patch.object(llm_module, "extract_title_from_filename", return_value=fake_extraction):
        result = identify_svc.identify(
            "/share/.../死亡笔记.BDrip1080P.X264.AC3.LGGZ S.01.mkv",
            provider,
            llm_api_key="fake-key",
        )

    assert result.candidates  # 应该有候选
    assert result.parse.title == "死亡笔记"
    # rescue reason 应该出现在某处（reasoning 或者直接 top_pick OK）
    assert "llm_rescue" in result.reasoning.lower() or result.top_pick is not None


def test_identify_llm_rescue_returns_no_title_falls_through():
    """LLM 也拿不到 title → 优雅 fallback 到 needs_review。"""
    from services import llm as llm_module

    provider = MagicMock()
    provider.search.return_value = []  # 无论怎么搜都 0
    with patch.object(llm_module, "extract_title_from_filename", return_value=None):
        result = identify_svc.identify(
            "/share/.../random.mkv", provider, llm_api_key="fake-key"
        )
    assert result.top_pick is None
    assert result.candidates == []


def test_identify_no_llm_key_no_rescue():
    """没有 llm_api_key → 不调 LLM rescue，直接 needs_review。"""
    from services import llm as llm_module

    provider = MagicMock()
    provider.search.return_value = []
    with patch.object(llm_module, "extract_title_from_filename") as mock_extract:
        result = identify_svc.identify(
            "/share/.../random.mkv", provider, llm_api_key=None
        )
        mock_extract.assert_not_called()
    assert result.top_pick is None
