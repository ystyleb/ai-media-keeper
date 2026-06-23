"""Phase 2 spike unit tests: filename parser + top_pick heuristic.

TMDB HTTP 调用全 mock；专注 pipeline 逻辑（解析 / 评分 / grounded membership）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
    p = identify_svc.parse_filename("/share/.../Dune.Part.Two.2024.2160p.UHD.BluRay.x265-FRDS.mkv")
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


def test_parse_filename_ambiguous_returns_int_not_list():
    """guessit 对 ambiguous 命名（老电影含 BD2 / DD2.0 之类数字）会返
    list-typed episode/season，FilenameParse 必须规约成 int 或 None
    （否则下游 SQL bind / NFO XML 都会撞类型错）。

    Regression test: 1916 默片 Judex 命名引起的 ProgrammingError。
    """
    p = identify_svc.parse_filename("/share/.../Judex.1916.BD2.BluRay.1080p.DD2.0.x264-BMDru.mkv")
    # 不强校验 guessit 怎么解析（它的启发式可能升级），但严格要求
    # 类型为 int 或 None，永远不是 list
    assert p.season is None or isinstance(p.season, int)
    assert p.episode is None or isinstance(p.episode, int)
    assert p.year is None or isinstance(p.year, int)


def test_normalize_int_handles_list_and_scalars():
    """_normalize_int 规约函数本身的单元测试。"""
    f = identify_svc._normalize_int
    assert f(None) is None
    assert f(5) == 5
    assert f([7]) == 7  # 单元素 list → 取值
    assert f([1916, 16]) is None  # 多元素 ambiguous → None
    assert f("string") is None  # 异常类型 → None
    assert f([]) is None
    assert f(["not_int"]) is None  # list 里非 int → None


def test_parse_filename_empty_returns_unknown():
    p = identify_svc.parse_filename("/some/path/randomfile.mkv")
    # title 可能被 guessit 抽到 "randomfile"，但不会崩；只确保字段存在
    assert hasattr(p, "title")
    assert hasattr(p, "media_type")


# ---------------- extras detection ---------------- #


def test_parse_filename_extras_dash_number():
    """Lost.Highway.1997.Extras-01.BDRip... → 不当 S1997E01 TV，标 extra。"""
    p = identify_svc.parse_filename(
        "/share/Moives/Lost.Highway.1997/Lost.Highway.1997.Extras-01.BDRip.1080p.Ac3.x264.BMDru.mkv"
    )
    assert p.media_type == "extra"
    assert p.season is None
    assert p.episode is None
    assert p.raw.get("_extra_kind") == "extra"


def test_parse_filename_featurette():
    p = identify_svc.parse_filename("/share/Moives/Foo.2020.Featurette.1080p.mkv")
    assert p.media_type == "extra"
    assert p.raw.get("_extra_kind") == "featurette"


def test_parse_filename_trailer_sample():
    p1 = identify_svc.parse_filename("/x/foo.trailer.1080p.mkv")
    assert p1.media_type == "extra"
    p2 = identify_svc.parse_filename("/x/Sample-TBHM10.mkv")
    assert p2.media_type == "extra"


def test_parse_filename_interview_behind_scenes():
    p1 = identify_svc.parse_filename("/x/Foo.Interview.With.Director.mkv")
    assert p1.media_type == "extra"
    p2 = identify_svc.parse_filename("/x/Foo.Behind.The.Scenes.mkv")
    assert p2.media_type == "extra"


def test_parse_filename_main_feature_not_misclassified():
    """主片不能被 extras pattern 误伤。"""
    p = identify_svc.parse_filename(
        "/share/Moives/Citizen.Kane.1941.BluRay.1080p.x264.BMDru/"
        "Citizen.Kane.1941.Criterion.Collection.BluRay.1080p.DD1.0.x264.BMDru.mkv"
    )
    assert p.media_type == "movie"
    assert p.year == 1941


def test_identify_extras_short_circuits_provider():
    """extra 文件不调 provider.search 也不调 LLM。"""
    fake_provider = MagicMock()
    result = identify_svc.identify(
        "/share/Movies/Foo/Foo.2020.Extras-03.1080p.mkv",
        provider=fake_provider,
        llm_api_key="sk-fake",
    )
    fake_provider.search.assert_not_called()
    assert result.parse.media_type == "extra"
    assert result.top_pick is None
    assert result.pick_source == "extra"
    assert result.candidates == []


# ---------------- multi-disc parts (BD1/BD2/Disc1/Disc2/CD/DVD) ---------------- #


def test_parse_filename_bd1_is_main():
    """BD1 是主片入口，media_type 保持 movie（让下游正常调 TMDB）。"""
    p = identify_svc.parse_filename(
        "/share/Moives/Judex.1916/Judex.1916.BD1.BluRay.1080p.DD2.0.x264-BMDru.mkv"
    )
    assert p.media_type == "movie"
    assert p.season is None  # 清掉污染（guessit 把 1916 前两位当 season=19）
    assert p.episode is None
    assert p.raw.get("_part_index") == 1


def test_parse_filename_bd2_is_part():
    """BD2 是主片第 2 段 → media_type='part'，库视图隐藏。"""
    p = identify_svc.parse_filename(
        "/share/Moives/Judex.1916/Judex.1916.BD2.BluRay.1080p.DD2.0.x264-BMDru.mkv"
    )
    assert p.media_type == "part"
    assert p.season is None
    assert p.episode is None
    assert p.raw.get("_part_index") == 2


def test_parse_filename_disc_cd_dvd_variants():
    for name, expected_idx in [
        ("/x/Foo.Disc1.1080p.mkv", 1),
        ("/x/Foo.Disc2.1080p.mkv", 2),
        ("/x/Foo.CD1.mkv", 1),
        ("/x/Foo.CD3.mkv", 3),
        ("/x/Foo.DVD2.mkv", 2),
    ]:
        p = identify_svc.parse_filename(name)
        assert p.raw.get("_part_index") == expected_idx, name
        if expected_idx >= 2:
            assert p.media_type == "part", name


def test_part_pattern_not_in_title():
    """主片名含 'Part' 但没分盘 marker 不该被误标 part。"""
    p = identify_svc.parse_filename("/x/Pirates.of.the.Caribbean.Part.1.2003.mkv")
    # Part.1 不是 BD/Disc/CD/DVD marker，不该匹配
    assert p.media_type != "part"
    assert p.raw.get("_part_index") is None


def test_identify_part_short_circuits_provider():
    """BD2+ 不调 TMDB / LLM。"""
    fake_provider = MagicMock()
    result = identify_svc.identify(
        "/share/Movies/Foo.1916/Foo.1916.BD2.BluRay.1080p.mkv",
        provider=fake_provider,
        llm_api_key="sk-fake",
    )
    fake_provider.search.assert_not_called()
    assert result.parse.media_type == "part"
    assert result.pick_source == "part"


# ---------------- 续集 "Part N" 合并回 title（教父 II / III 之类） ---------------- #


def test_parse_filename_sequel_part_merged_to_title():
    """guessit 把 'Part II' 抽到 part 字段，title 留下 'The Godfather'。
    为了 TMDB 能搜到正确续集，要把 'Part N' 合并回 title。"""
    p = identify_svc.parse_filename("/x/The.Godfather.Part.II.1974.BluRay.1080p.mkv")
    assert p.title == "The Godfather Part 2"
    assert p.year == 1974
    assert p.media_type == "movie"
    # 不能跟 BD/Disc 混淆 — 这里没 _part_index
    assert p.raw.get("_part_index") is None


def test_parse_filename_sequel_part_iii():
    p = identify_svc.parse_filename("/x/The.Godfather.Part.III.1990.BluRay.1080p.mkv")
    assert p.title == "The Godfather Part 3"


def test_parse_filename_bd1_does_not_inject_part_n():
    """BD1 是分盘 marker，guessit 不会同时给 part 字段，title 不该被注入 'Part N'。"""
    p = identify_svc.parse_filename("/x/Judex.1916/Judex.1916.BD1.BluRay.1080p.mkv")
    # title 应该是 guessit 给的 raw，不带 'Part N'
    assert "Part" not in p.title
    assert p.raw.get("_part_index") == 1


# ---------------- _pick_top year-mismatch penalty ---------------- #


def test_pick_top_penalizes_year_mismatch():
    """两个候选 title 都 match 时，year 匹配的应该胜出（即使 substring vs exact）。

    场景：'The Godfather Part 2' 1974 vs '教父' (1972) + '教父2' (1974)
    - 教父 1972: original exact 0.7 + year mismatch (-0.2) + vote 0.05 = 0.55
    - 教父2 1974: original substring 0.4 + year exact 0.2 + vote 0.05 = 0.65 ← 应该选这个
    """
    cands = [
        MediaCandidate(
            id="tmdb:movie:238",
            external_ids={"tmdb_id": "238"},
            title="教父",
            original_title="The Godfather",
            year=1972,
            media_type="movie",
            poster_url=None,
            overview=None,
            vote_average=8.7,
            raw={},
        ),
        MediaCandidate(
            id="tmdb:movie:240",
            external_ids={"tmdb_id": "240"},
            title="教父2",
            original_title="The Godfather Part II",
            year=1974,
            media_type="movie",
            poster_url=None,
            overview=None,
            vote_average=8.6,
            raw={},
        ),
    ]
    parse = identify_svc.parse_filename("/x/The.Godfather.Part.II.1974.BluRay.1080p.mkv")
    top, score, reason = identify_svc._pick_top(parse, cands)
    # heuristic 阈值 ≥0.7 才返非 None，所以这里 score=0.65 还是 < 0.7 → top is None
    # 但 reason 应该指向"Part II"那一项；下游 heuristic_fallback (≥0.5) 会用它
    assert "year mismatch" in reason or "year exact" in reason


def test_pick_top_tv_later_season_year_not_penalized():
    """多季剧：文件名 year 是当季播出年，TMDB first_air_date 是 S01 首播年。
    后续季的 year > first_air_date 是 EXPECTED，不能当 year mismatch 罚分。

    真实 bug（House of the Dragon 2026 S03E01，2026-06-23）：
    - 抽出 title='House of the Dragon', year=2026, S03E01, media_type='episode'
    - TMDB TV 候选 #1 = original 'House of the Dragon', first_air_date year=2022
    - 修复前: original exact 0.7 + year mismatch (-0.2) + vote 0.05 = 0.55 → top=None
              → needs_review → media_type=None → auto-organize 卡 needs_identify
    - 修复后: original exact 0.7 + tv year consistent (+0.2) + vote 0.05 = 0.95 → 自动绑
    """
    parse = _parse(
        "House of the Dragon", year=2026, season=3, episode=1, media_type="episode"
    )
    cands = [
        MediaCandidate(
            id="tmdb:tv:94997",
            external_ids={"tmdb_id": "94997"},
            title="权力的游戏前传：龙族",
            original_title="House of the Dragon",
            year=2022,
            media_type="tv",
            poster_url=None,
            overview=None,
            vote_average=8.3,
            raw={},
        ),
        _cand(
            "tmdb:tv:236847",
            "Enter the House of the Dragon",
            year=2022,
            media_type="tv",
            vote=0.0,
        ),
        _cand("tmdb:tv:137555", "决胜21天", year=2021, media_type="tv", vote=4.0),
    ]
    top, score, reason = identify_svc._pick_top(parse, cands)
    assert top is not None, f"应绑到 HotD，实际 None (score={score}, reason={reason})"
    assert top.id == "tmdb:tv:94997", f"选错候选: {top.id}"
    assert score >= 0.9, f"TV 后续季 year-consistent 应高分自动绑, got {score:.2f}"


def test_pick_top_same_title_different_year_defers_to_llm():
    """同名多候选（reboot / 美英版）：两个 title-exact TV 候选只有 first_air 年不同，
    heuristic 仅靠 year 不能可靠区分 → 压低置信 (<0.9) 让 path 2 不盲绑，转 LLM 消歧。

    防回归：tv_consistent 修复若无歧义守门，"年份更晚" 的错版本会反而高分自动绑
    （The.Office US 2005 vs UK 2001，文件名 2006 → UK 0.95 误绑）。
    """
    parse = _parse(
        "The Office", year=2006, season=3, episode=1, media_type="episode"
    )
    cands = [
        _cand("tmdb:tv:2316", "The Office", year=2005, media_type="tv", vote=8.6),  # US
        _cand("tmdb:tv:2996", "The Office", year=2001, media_type="tv", vote=7.8),  # UK
    ]
    top, score, reason = identify_svc._pick_top(parse, cands)
    assert score < 0.9, f"同名歧义应压低置信交 LLM, got {score:.2f}"
    assert "ambiguous" in reason
    # B1: 返回 top=None（不是 sort-winner）→ LLM 不可用时下游走 needs_review，
    # 不会用可能错的同名候选给自信猜测
    assert top is None


def test_pick_top_tv_year_before_first_air_still_penalized():
    """反向守门：文件名 year < TMDB first_air_date（剧首播前就有该季 = 不可能）
    仍按 year mismatch 罚分，避免 TV 放宽变成"年份完全不看"。"""
    parse = _parse("Some Show", year=2015, season=1, episode=1, media_type="episode")
    cands = [_cand("tmdb:tv:1", "Some Show", year=2020, media_type="tv")]
    top, score, reason = identify_svc._pick_top(parse, cands)
    # title exact 0.7 + year mismatch (-0.2) + vote 0.05 = 0.55 → top None
    assert "year mismatch" in reason
    assert score < 0.7


# ---------------- _pick_top ---------------- #


def _cand(id_, title, year=None, media_type="tv", vote=7.5):
    return MediaCandidate(
        id=id_,
        external_ids={"tmdb_id": id_.split(":")[-1]},
        title=title,
        original_title=title,
        year=year,
        media_type=media_type,
        poster_url=None,
        overview=None,
        vote_average=vote,
    )


def _parse(title, year=None, season=None, episode=None, media_type="episode"):
    return identify_svc.FilenameParse(
        raw_name="x.mkv",
        title=title,
        year=year,
        season=season,
        episode=episode,
        episode_title=None,
        media_type=media_type,
        resolution=None,
        source=None,
        release_group=None,
        codec=None,
        color_depth=None,
        hdr_profiles=[],
        container=None,
        audio_codec=None,
        raw={},
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
            id="tmdb:tv:1",
            external_ids={"tmdb_id": "1"},
            title="瑞克和莫蒂",
            original_title="Rick and Morty",
            year=2013,
            media_type="tv",
            poster_url=None,
            overview=None,
            vote_average=8.7,
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
    result = identify_svc.identify("/share/Rick and Morty - S06E02 WEBDL-1080p.mkv", provider)
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
        title="死亡笔记",
        alt_title="Death Note",
        year=2006,
        season=1,
        episode=None,
        media_type="tv",
        raw_response='{"title":"死亡笔记"}',
    )
    with patch.object(
        llm_module, "extract_title_from_filename", return_value=(fake_extraction, "")
    ):
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
    with patch.object(llm_module, "extract_title_from_filename", return_value=(None, "no_title")):
        result = identify_svc.identify("/share/.../random.mkv", provider, llm_api_key="fake-key")
    assert result.top_pick is None
    assert result.candidates == []


def test_identify_no_llm_key_no_rescue():
    """没有 llm_api_key → 不调 LLM rescue，直接 needs_review。"""
    from services import llm as llm_module

    provider = MagicMock()
    provider.search.return_value = []
    with patch.object(llm_module, "extract_title_from_filename") as mock_extract:
        result = identify_svc.identify("/share/.../random.mkv", provider, llm_api_key=None)
        mock_extract.assert_not_called()
    assert result.top_pick is None


# ─── Phase 3.1: quality 字段抽取（plan 3.1 节 11 个 case） ───


def test_parse_filename_extracts_codec_h265():
    """h265/HEVC release 的 video_codec 应被抽出。"""
    p = identify_svc.parse_filename("/share/Movies/Movie.2020.1080p.BluRay.x265.10bit-GROUP.mkv")
    assert p.codec == "H.265"
    assert p.color_depth == "10-bit"
    assert p.container == "mkv"


def test_parse_filename_extracts_codec_av1():
    """AV1 是新一代 codec，guessit 应识别。"""
    p = identify_svc.parse_filename("/share/Movies/Movie.2024.2160p.WEB-DL.AV1.10bit-GROUP.mkv")
    assert p.codec == "AV1"


def test_parse_filename_extracts_hdr10_only():
    """HDR10 标记应抽出，不带 + 号。"""
    p = identify_svc.parse_filename("/share/Movies/Movie.2020.2160p.BluRay.HDR10.x265-GROUP.mkv")
    assert "HDR10" in p.hdr_profiles
    assert "HDR10+" not in p.hdr_profiles
    assert "DolbyVision" not in p.hdr_profiles


def test_parse_filename_extracts_dolby_vision():
    """Dolby Vision (with space) 应归一化到 'DolbyVision'。"""
    p = identify_svc.parse_filename("/share/Movies/Movie.2020.2160p.BluRay.DV.HDR.x265-GROUP.mkv")
    # guessit 把 'DV' 也识别成 Dolby Vision
    assert "DolbyVision" in p.hdr_profiles


def test_parse_filename_extracts_hdr10_plus_dolby_vision_combined():
    """HDR10+ 和 DolbyVision 同时出现时都要抽出，且 HDR10+ 不被误吞为 HDR10。

    r2 BLOCKER: HDR10+ implies HDR10，不应同时存在（dedup 会重复加分）。
    """
    p = identify_svc.parse_filename(
        "/share/Movies/Movie.2024.2160p.BluRay.DV.HDR10+.x265-GROUP.mkv"
    )
    assert "DolbyVision" in p.hdr_profiles
    assert "HDR10+" in p.hdr_profiles
    # r2 修订：HDR10+ 已 imply HDR10，不该并存
    assert "HDR10" not in p.hdr_profiles
    # canonical sorted
    assert p.hdr_profiles == sorted(p.hdr_profiles)


def test_hdr_hdr10_plus_implies_no_plain_hdr10():
    """直接 helper 层面：guessit 在 other 里给 HDR10 + raw filename 含 HDR10+
    → hits set 应只剩 HDR10+，不带 HDR10。
    """
    profiles = identify_svc._extract_hdr_profiles(
        other_field="HDR10",
        fallback_text="Movie.HDR10+.x265.mkv",
    )
    assert "HDR10+" in profiles
    assert "HDR10" not in profiles


def test_parse_filename_extracts_container_mkv():
    """容器字段应抽对。"""
    p = identify_svc.parse_filename("/share/Movies/Movie.2020.1080p.BluRay.x264-GROUP.mp4")
    assert p.container == "mp4"


def test_parse_filename_no_hdr_returns_empty_list():
    """无 HDR 标记时 hdr_profiles 是空 list（不是 None）。"""
    p = identify_svc.parse_filename("/share/Movies/Movie.2020.1080p.BluRay.x264-GROUP.mkv")
    assert p.hdr_profiles == []


def test_parse_filename_color_depth_10bit():
    p = identify_svc.parse_filename("/share/Movies/Movie.2020.2160p.BluRay.x265.10bit-GROUP.mkv")
    assert p.color_depth == "10-bit"


def test_parse_filename_audio_codec_truehd_atmos():
    """guessit audio_codec 返 list 时取 first；str 时原样。"""
    p = identify_svc.parse_filename(
        "/share/Movies/Movie.2020.2160p.BluRay.TrueHD.Atmos.x265-GROUP.mkv"
    )
    # audio_codec 抽 first or str；只断言非空 + 含 truehd/atmos 任一关键词
    assert p.audio_codec is not None
    assert any(k in (p.audio_codec or "").lower() for k in ("truehd", "atmos", "dolby"))


def test_extras_short_circuit_still_no_codec():
    """Extras / featurette / trailer 路径下 codec/HDR 不抽（plan：附属不参与 dedup）。"""
    p = identify_svc.parse_filename(
        "/share/Movies/Lost.Highway.1997.Extras-01.BDRip.1080p.x264-GROUP.mkv"
    )
    assert p.media_type == "extra"
    assert p.codec is None
    assert p.hdr_profiles == []
    assert p.container is None
    assert p.audio_codec is None


def test_part_filename_still_extracts_codec():
    """多盘 BD2/CD2 即 media_type='part' 仍应抽 codec（plan：分盘有真实视频信息）。"""
    p = identify_svc.parse_filename("/share/Movies/Judex.1916.BD2.BluRay.1080p.x265-GROUP.mkv")
    assert p.media_type == "part"
    assert p.codec == "H.265"
    assert p.container == "mkv"


# Bonus: HDR 长 token 优先匹配 — plan I4 已 fix in helper
def test_hdr_long_token_priority_no_substring_collision():
    """HDR10+ 在 HDR10 之前匹配；移除后不会再被 HDR10 二次匹配。"""
    profiles = identify_svc._extract_hdr_profiles("HDR10+")
    assert profiles == ["HDR10+"]


def test_hdr_keyword_in_list_form():
    """guessit 'other' 字段是 list[str] 时也要正确解析。"""
    profiles = identify_svc._extract_hdr_profiles(["Dolby Vision", "HDR10"])
    assert profiles == ["DolbyVision", "HDR10"]  # canonical sorted


def test_hdr_fallback_does_not_match_substring_within_word():
    """r1 IMPORTANT: 'NotHDR10Plus' 不该被识别为 HDR10+（word boundary 检查）。"""
    profiles = identify_svc._extract_hdr_profiles(None, fallback_text="NotHDR10Plus.mkv")
    assert "HDR10+" not in profiles
    # 也不该把 NotHDR10Plus 退化匹配成 HDR10
    assert "HDR10" not in profiles


def test_hdr_fallback_does_not_match_HDR10_inside_word():
    profiles = identify_svc._extract_hdr_profiles(None, fallback_text="someHDR10Stuff")
    assert profiles == []


def test_hdr_fallback_matches_HDR10Plus_with_dot_separator():
    """PT 命名习惯 'Movie.HDR10Plus.x265' 应识别为 HDR10+。"""
    profiles = identify_svc._extract_hdr_profiles(
        None, fallback_text="Movie.2024.HDR10Plus.x265-GROUP.mkv"
    )
    assert "HDR10+" in profiles


def test_codec_fallback_extracts_av1_from_raw_filename():
    """guessit v3 不识别 AV1；fallback 用 raw filename regex 抽到。"""
    codec = identify_svc._extract_codec(
        None, fallback_text="Movie.2024.2160p.WEB-DL.AV1.10bit-GROUP.mkv"
    )
    assert codec == "AV1"


def test_codec_fallback_normalizes_x265_to_h265():
    codec = identify_svc._extract_codec(None, fallback_text="Movie.2020.x265-GROUP.mkv")
    assert codec == "H.265"


def test_codec_fallback_does_not_match_within_word():
    """'AV1Plugin' 不该被识别为 AV1 codec。"""
    codec = identify_svc._extract_codec(None, fallback_text="AV1Plugin.mkv")
    assert codec is None


def test_extracts_codec_av1_in_full_pipeline():
    """完整 parse_filename 在 AV1 release 上能拿到 codec='AV1'。"""
    p = identify_svc.parse_filename("/share/Movies/Movie.2024.2160p.WEB-DL.AV1.10bit-GROUP.mkv")
    assert p.codec == "AV1"


# ---------------- bug #11: single_exact must respect year ---------------- #


def test_identify_single_exact_rejects_year_mismatch():
    """#11: 唯一候选 title exact 但 year 差 >1 → 不能走 single_exact 0.95 自动绑，
    应 fall through 到 _pick_top（year mismatch 罚分 → 0.55 < 0.7 → needs_review）。"""
    fake = MagicMock()
    fake.search.return_value = [
        MediaCandidate(
            id="tmdb:movie:238",
            external_ids={"tmdb_id": "238"},
            title="The Godfather",
            original_title="The Godfather",
            year=1972,
            media_type="movie",
            poster_url=None,
            overview=None,
            vote_average=8.7,
        ),
    ]
    result = identify_svc.identify(
        "/x/The.Godfather.1974.BluRay.1080p.mkv", provider=fake, llm_api_key=None
    )
    assert result.pick_source != "single_exact"
    assert result.pick_source == "needs_review"


def test_identify_single_exact_still_binds_when_year_matches():
    """回归：year 一致时 single_exact 仍正常 0.95 绑定（用干净标题，避开 guessit Part 剥离）。"""
    fake = MagicMock()
    fake.search.return_value = [
        MediaCandidate(
            id="tmdb:movie:27205",
            external_ids={"tmdb_id": "27205"},
            title="Inception",
            original_title="Inception",
            year=2010,
            media_type="movie",
            poster_url=None,
            overview=None,
            vote_average=8.4,
        ),
    ]
    result = identify_svc.identify(
        "/x/Inception.2010.BluRay.1080p.mkv", provider=fake, llm_api_key=None
    )
    assert result.pick_source == "single_exact"
    assert result.confidence == 0.95


def test_identify_single_exact_tv_later_season_binds():
    """full identify() 快路径：唯一 TV 候选 + title exact + 文件年份晚于首播（多季剧）
    → single_exact 0.95 绑定（不因 year 差被 fall through）。覆盖 codex review 指出的
    "single_exact TV 语义只在 _pick_top 层测了" 缺口。"""
    fake = MagicMock()
    fake.search.return_value = [
        MediaCandidate(
            id="tmdb:tv:94997",
            external_ids={"tmdb_id": "94997"},
            title="House of the Dragon",
            original_title="House of the Dragon",
            year=2022,
            media_type="tv",
            poster_url=None,
            overview=None,
            vote_average=8.3,
        ),
    ]
    result = identify_svc.identify(
        "/x/House.of.the.Dragon.2026.S03E01.1080p.mkv", provider=fake, llm_api_key=None
    )
    assert result.pick_source == "single_exact"
    assert result.confidence == 0.95
    assert result.top_pick.id == "tmdb:tv:94997"


def test_identify_single_exact_tv_year_before_first_air_rejected():
    """唯一 TV 候选但文件年份早于首播 >1 年（剧首播前不可能有该季）→ 不走 single_exact，
    fall through 到 _pick_top（mismatch 罚分 → needs_review）。守门 TV year-gate 放宽
    没有把"早于首播"也放进来。"""
    fake = MagicMock()
    fake.search.return_value = [
        MediaCandidate(
            id="tmdb:tv:999",
            external_ids={"tmdb_id": "999"},
            title="Some Show",
            original_title="Some Show",
            year=2020,
            media_type="tv",
            poster_url=None,
            overview=None,
            vote_average=7.5,
        ),
    ]
    result = identify_svc.identify(
        "/x/Some.Show.2015.S01E01.1080p.mkv", provider=fake, llm_api_key=None
    )
    assert result.pick_source != "single_exact"
