"""文件名解析 + provider 搜索 + top_pick 选择。

Pipeline:
  path → guessit parse → title/year/season/episode → provider.search() → candidates
                                                  ↘
                                                   top_pick：如果候选 ≥ 1 + title 完全
                                                            匹配 + 高 confidence → 直接绑
                                                   否则：metadata_status='needs_review'

Phase 2 spike 不接 LLM；top_pick 用简单 heuristic（candidate 数量 + title 模糊匹配）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from guessit import guessit

from . import llm
from .metadata.base import MediaCandidate, MetadataProvider


@dataclass(frozen=True)
class FilenameParse:
    raw_name: str
    title: str
    year: int | None
    season: int | None
    episode: int | None
    episode_title: str | None
    media_type: str  # 'movie' | 'episode' | 'unknown' | 'extra' | 'part'
    resolution: str | None
    source: str | None
    release_group: str | None
    # Phase 3.1: quality 字段（dedup engine 用）。extras 路径下 codec/HDR/container
    # 全部 None / [] — 附属文件不参与 quality score，故省一次解析（plan 3.1）。
    codec: str | None
    color_depth: str | None
    hdr_profiles: list[str]  # canonical sorted; 空 list 表示无 HDR
    container: str | None
    audio_codec: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class IdentifyResult:
    parse: FilenameParse
    candidates: list[MediaCandidate]
    top_pick: MediaCandidate | None  # None = needs_review
    confidence: float  # 0.0-1.0
    reasoning: str
    pick_source: str  # 'single_exact' | 'heuristic' | 'llm' | 'needs_review'


_EXTRA_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bextras?[-_.\s]*\d", re.I), "extra"),
    (re.compile(r"\bfeaturettes?\b", re.I), "featurette"),
    (re.compile(r"\binterviews?\b", re.I), "interview"),
    (re.compile(r"\btrailers?\b", re.I), "trailer"),
    (re.compile(r"\bdeleted[-_.\s]*scenes?\b", re.I), "deleted_scene"),
    (re.compile(r"\bbehind[-_.\s]*the[-_.\s]*scenes?\b", re.I), "bts"),
    (re.compile(r"\bmaking[-_.\s]*of\b", re.I), "making_of"),
    (re.compile(r"\bbloopers?\b", re.I), "bloopers"),
    (re.compile(r"\bgag[-_.\s]*reel\b", re.I), "gag_reel"),
    (re.compile(r"\bsample\b", re.I), "sample"),
]

# 多盘分段：BD1/BD2 / Disc1/Disc2 / CD1/CD2 / DVD1/DVD2
# 老电影常分 2-3 盘发，每盘是主片的一段（不是花絮）。Part1/Pt1 太歧义不包含
# （"Pirates of the Caribbean Part 1" 是片名而非分盘）。
_DISC_PATTERN = re.compile(r"\b(BD|Disc|CD|DVD)[-_.\s]*(\d+)\b", re.I)


# HDR keyword 长 token 优先匹配 + 移除已匹配段避免短 token 二次误中
# 例：'HDR10+' 必须先匹配，否则会被 'HDR10' / 'HDR' 提前吃掉变 [HDR10, HDR]
# 顺序：HDR10+ > Dolby Vision/DolbyVision > HDR10 > HLG。'HDR' 单独留空兜底
# （只检测有 profile 的；'HDR' 这种笼统标签无法决定具体 profile，跳过）
_HDR_KEYWORDS: list[tuple[str, str]] = [
    ("HDR10+", "HDR10+"),
    ("Dolby Vision", "DolbyVision"),
    ("DolbyVision", "DolbyVision"),
    ("HDR10", "HDR10"),
    ("HLG", "HLG"),
]


def _extract_hdr_profiles(other_field: Any, fallback_text: str = "") -> list[str]:
    """从 guessit['other'] + raw filename 抽 HDR profile 列表。

    返 canonical sorted list[str]；空 list 表示无 HDR。

    fallback_text：guessit 当前版本不识别 'HDR10+' / 'HDR10Plus'（'+' 当 noise 截断
    成 'HDR10'）。PT 命名习惯 HDR10+ 普遍，所以同时扫 raw filename 作为兜底。
    [code-enforced] 长 token 先匹配后**移除**, 防止 'HDR10+' 被后续 'HDR10' 二次匹配
    """
    items: list[str] = []
    if other_field is not None:
        items = other_field if isinstance(other_field, list) else [other_field]
    remaining = " | ".join(str(x) for x in items)
    if fallback_text:
        # 用 separator 拼接避免子串跨界（如 "HDR10" 接 raw "+...")
        remaining = remaining + " | " + fallback_text

    hits: set[str] = set()
    # 额外识别 'HDR10Plus' / 'HDR10+' 文本变体（guessit 默认不识别）
    extra_keywords = [
        ("HDR10Plus", "HDR10+"),
        ("HDR10+", "HDR10+"),
    ]
    # [code-enforced] 用左/右边界正则避免子串误中（如 'NotHDR10Plus' 不应当 HDR10+）
    # 不用 \b 因为 'HDR10+' 末尾的 '+' 不是 word char，\b 在 '+' 跟字母间不存在。
    # 自己定义边界：左边界 = 行首或非字母数字，右边界 = 行尾或非字母数字。
    for kw_in, kw_out in extra_keywords + _HDR_KEYWORDS:
        pat = re.compile(rf"(?:^|[^A-Za-z0-9]){re.escape(kw_in)}(?:[^A-Za-z0-9]|$)", re.IGNORECASE)
        if pat.search(remaining):
            hits.add(kw_out)
            remaining = pat.sub(" ", remaining)
    # r2 BLOCKER 修订: HDR10+ implies HDR10。当 guessit 'other' 字段已包含 HDR10
    # 又从 raw filename fallback 抽到 HDR10+ 时，两者会同时在 hits set —— dedup
    # score 重复加分。移除冗余 HDR10。
    if "HDR10+" in hits:
        hits.discard("HDR10")
    return sorted(hits)


def _first_or_str(v: Any) -> str | None:
    """guessit 部分字段（audio_codec）会返 list 或 str；取 first 或原值。"""
    if v is None:
        return None
    if isinstance(v, list):
        return str(v[0]) if v else None
    return str(v) or None


# Codec 检测 fallback：guessit 不识别 AV1（v3 stable），PT 命名常带 AV1/HEVC/x264 等
# tokens。这里按 raw filename 兜底。优先级跟 _HDR_KEYWORDS 一样：长 token 优先 + 移除。
_CODEC_PATTERNS: list[tuple[str, str]] = [
    ("AV1", "AV1"),
    ("HEVC", "H.265"),
    ("H.265", "H.265"),
    ("H265", "H.265"),
    ("x265", "H.265"),
    ("VP9", "VP9"),
    ("H.264", "H.264"),
    ("H264", "H.264"),
    ("x264", "H.264"),
]


def _extract_codec(video_codec_field: Any, fallback_text: str = "") -> str | None:
    """优先 guessit video_codec，缺时按 raw filename regex 兜底。

    [accepted limitation] guessit v3 不识别 AV1，所以 fallback 必要。归一化映射：
      AV1 → 'AV1'；HEVC/x265/H265 → 'H.265'；x264/H264 → 'H.264'。
    """
    if video_codec_field:
        s = str(video_codec_field)
        if s:
            return s
    if not fallback_text:
        return None
    text = fallback_text
    for kw_in, kw_out in _CODEC_PATTERNS:
        if re.search(rf"\b{re.escape(kw_in)}\b", text, re.IGNORECASE):
            return kw_out
    return None


def _detect_part_index(name: str) -> int | None:
    """检测多盘分段 marker，返回 part index（1, 2, 3, ...）或 None。

    BD1/Disc1/CD1/DVD1 → 1（第一盘 = 主片入口）
    BD2/Disc2/... → 2+（后续段，库视图隐藏，详情卡聚合显示）
    """
    m = _DISC_PATTERN.search(name)
    if not m:
        return None
    try:
        return int(m.group(2))
    except ValueError:
        return None


def _detect_extra(name: str) -> str | None:
    """识别特典 / 花絮 / 采访 / 预告 / 删除场景 / 样片等附属内容。

    返回 extra 子类（'extra' / 'featurette' / 'interview' / ...）或 None。
    用 \\b 边界 + 数字后缀避免误伤（"The Interview" 这种电影本名很少撞），
    但 'sample' / 'trailer' 这类 keyword 在主片文件名里也几乎不会出现。
    """
    for pat, kind in _EXTRA_PATTERNS:
        if pat.search(name):
            return kind
    return None


def _normalize_int(v: Any) -> int | None:
    """guessit 对 ambiguous 文件名（如 "Judex.1916.BD2..." 老电影）会返回 list[int]
    表示"可能是这几个之一"。FilenameParse.season/episode 语义是 int|None，
    需要把 list 规约掉——否则下游 SQL bind / NFO XML 都会撞类型错。

    规约规则：
      - None / int → 原样
      - list[int] 长度=1 → 取唯一值
      - list[int] 多个值 → None（明显 ambiguous，宁可 None 让 TMDB 全候选挑）
      - 其他类型 → None
    """
    if v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, list):
        if len(v) == 1 and isinstance(v[0], int):
            return v[0]
        return None
    return None


def parse_filename(path: str) -> FilenameParse:
    """用 guessit 解析路径末尾的文件名。

    特典 / 花絮 / 预告等附属文件先于 guessit 检测 — guessit 对 'Extras-01' 这种
    后缀会硬当 S1997E01 episode 解析，污染 season/episode，并诱导下游绑到
    无关 TV 剧。检出 extra 时直接标 media_type='extra' 且不带 season/episode。
    """
    name = path.rsplit("/", 1)[-1]

    extra_kind = _detect_extra(name)
    if extra_kind:
        # extras：title 取去掉 release tag 的 stem 给 UI 显示；不靠 guessit 推断 type
        # （不调 guessit 也能省一次解析，但 year 提取还有用，保留 guessit）
        # codec/HDR/container 故意不抽：附属文件不参与 dedup quality score，省解析
        g = dict(guessit(name))
        year = _normalize_int(g.get("year"))
        return FilenameParse(
            raw_name=name,
            title=g.get("title", "") or extra_kind,
            year=year,
            season=None,  # extras 不参与 S/E 索引
            episode=None,
            episode_title=None,
            media_type="extra",
            resolution=str(g.get("screen_size") or "") or None,
            source=str(g.get("source") or "") or None,
            release_group=str(g.get("release_group") or "") or None,
            codec=None,  # 不抽（plan 3.1：附属文件省解析）
            color_depth=None,
            hdr_profiles=[],
            container=None,
            audio_codec=None,
            raw={**g, "_extra_kind": extra_kind},
        )

    # 多盘分段：BD1/BD2/Disc1/Disc2/CD1/CD2/DVD1/DVD2
    # 第 2+ 盘标 'part'（库视图隐藏，详情卡聚合）；第 1 盘按 movie/tv 正常走 TMDB
    # 共同点：清空 guessit 错推的 s/e（如 "19" 是把 1916 前两位当 season）
    part_index = _detect_part_index(name)

    g = dict(guessit(name))

    season = _normalize_int(g.get("season"))
    episode = _normalize_int(g.get("episode"))
    year = _normalize_int(g.get("year"))

    media_type = "movie"
    if g.get("type") == "episode":
        media_type = "episode"
    elif g.get("type") == "movie":
        media_type = "movie"
    elif season or episode:
        media_type = "episode"

    if part_index is not None:
        # 检测到 BD/Disc/CD/DVD 分盘 marker → 强制覆盖 guessit 的推断
        # 分盘几乎只用于 movie（TV 剧用 SxxExx），所以 part_index == 1 → movie
        # 同时清掉污染的 s/e（guessit 把 1916 前两位当 season=19、BD1 当 episode）
        season = None
        episode = None
        if part_index >= 2:
            media_type = "part"
        else:
            media_type = "movie"  # BD1/Disc1/CD1 → 主片入口走 TMDB

    # 续集 "Part N"（罗马字/阿拉伯字）— guessit 把 "Part II" 抽到 `part` 字段，title 留下纯
    # "The Godfather"。但 TMDB 上正版 title 是 "The Godfather Part II"，所以 search 必须
    # 把 "Part N" 合并回去，否则候选 #1 是 Godfather 1972 而非 1974。
    # 跟上面的 BD/Disc 分盘 (_DISC_PATTERN) 不冲突：BDx 的 g['part'] 通常不存在（guessit
    # 把 BD1 当 source.disc 不是 part），所以这里只处理 "Part N" 形式的续集编号。
    title = g.get("title", "")
    guessit_part = g.get("part")
    if title and guessit_part is not None and part_index is None:
        title = f"{title} Part {guessit_part}"

    return FilenameParse(
        raw_name=name,
        title=title,
        year=year,
        season=season,
        episode=episode,
        episode_title=g.get("episode_title"),
        media_type=media_type,
        resolution=str(g.get("screen_size") or "") or None,
        source=str(g.get("source") or "") or None,
        release_group=str(g.get("release_group") or "") or None,
        # Phase 3.1 quality 字段 — 即使 media_type='part'（BD2/CD2 分盘）也抽，
        # 因为分盘文件本身有真实 codec/container/HDR 信息可供 dedup 引用
        codec=_extract_codec(g.get("video_codec"), fallback_text=name),
        color_depth=str(g.get("color_depth") or "") or None,
        hdr_profiles=_extract_hdr_profiles(g.get("other"), fallback_text=name),
        container=str(g.get("container") or "") or None,
        audio_codec=_first_or_str(g.get("audio_codec")),
        raw={**g, "_part_index": part_index} if part_index is not None else g,
    )


def _normalize_title(s: str) -> str:
    """大小写不敏感 + 去除标点 / 多余空白用于匹配。"""
    return re.sub(r"[^\w\s]+", "", s.lower()).strip()


def _pick_top(
    parse: FilenameParse, candidates: list[MediaCandidate]
) -> tuple[MediaCandidate | None, float, str]:
    """简单 heuristic：spike 不接 LLM，只看候选数量 + title 匹配 + year 匹配。"""
    if not candidates:
        return None, 0.0, "no candidates from provider"
    if not parse.title:
        return None, 0.0, "guessit could not extract title"

    want = _normalize_title(parse.title)
    want_year = parse.year

    # 优先在 episode 类型时只看 tv 候选
    if parse.media_type == "episode":
        candidates = [c for c in candidates if c.media_type == "tv"] or candidates

    scored: list[tuple[float, MediaCandidate, str]] = []
    for c in candidates:
        score = 0.0
        reasons: list[str] = []
        # 关键：同时比对 title（本地化，如 'zh-CN' 下 "瑞克和莫蒂"）和
        # original_title（"Rick and Morty"）。guessit 出的多半是英文，
        # 但 TMDB 返回的 title 是 language-localized，必须两边都看。
        got_title = _normalize_title(c.title)
        got_orig = _normalize_title(c.original_title or "")
        title_score = 0.0
        title_reason = ""
        for got, label in [(got_title, "title"), (got_orig, "original")]:
            if not got:
                continue
            if got == want:
                cand_score, cand_reason = 0.7, f"{label} exact"
            elif want in got or got in want:
                cand_score, cand_reason = 0.4, f"{label} substring"
            else:
                continue
            if cand_score > title_score:
                title_score, title_reason = cand_score, cand_reason
        if title_score > 0:
            score += title_score
            reasons.append(title_reason)

        if want_year and c.year == want_year:
            score += 0.2
            reasons.append("year exact")
        elif want_year and c.year and abs(c.year - want_year) == 1:
            score += 0.05
            reasons.append("year ±1")
        elif want_year and c.year:
            # 双方都知道 year 但不匹配 → 强负信号。文件名 year 通常很准（PT 命名习惯），
            # candidate year 在 TMDB 也是权威。"The Godfather 1972" 在 query year=1974
            # 时是错的候选，必须低于"The Godfather Part II 1974"。
            score -= 0.2
            reasons.append("year mismatch")

        # vote 高 + 候选首位（TMDB 默认按热度排）轻微 boost
        if c.vote_average and c.vote_average >= 7:
            score += 0.05
            reasons.append("vote ≥7")

        scored.append((min(score, 1.0), c, ", ".join(reasons)))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_score, top, reasons = scored[0]
    if top_score < 0.7:
        return None, top_score, f"low confidence (top={top_score:.2f}): {reasons}"
    return top, top_score, reasons


def _llm_filename_rescue(
    path: str, parse: FilenameParse, provider: MetadataProvider, api_key: str
) -> tuple[FilenameParse, list[MediaCandidate], str]:
    """LLM 解析文件名 → 用 LLM 提取的 title 重搜 TMDB。返 (updated_parse, candidates, reason)。

    专门救中文 release（"死亡笔记.BDrip..."）这种 guessit 拿不到 title 的文件名。
    LLM 不直接绑 TMDB id（仍属上游），下游 _pick_top / select_candidate 接着 ground。
    """
    filename = path.rsplit("/", 1)[-1]
    extraction, err = llm.extract_title_from_filename(filename, api_key=api_key)
    if extraction is None:
        # err 形如 "llm_error: NotFoundError: Model 'deepseek-v4-flash' not found"
        # 透传给 UI 让 user 看具体原因
        return parse, [], f"llm_rescue_failed: {err}"

    # 用 LLM 提取的 title 重搜 TMDB
    media_type = (
        extraction.media_type if extraction.media_type != "unknown" else parse.media_type or "movie"
    )
    candidates = provider.search(
        title=extraction.title,
        year=extraction.year,
        media_type=media_type,
    )
    # 如果 alt_title 不同且 candidates 仍空，试 alt
    if not candidates and extraction.alt_title and extraction.alt_title != extraction.title:
        candidates = provider.search(
            title=extraction.alt_title,
            year=extraction.year,
            media_type=media_type,
        )

    # 更新 parse：用 LLM 的 title / year / season / episode 替换（guessit 拿不到的部分）
    # quality 字段沿用 guessit 抽出的结果（codec/HDR/container 不依赖 title 文本）
    new_parse = FilenameParse(
        raw_name=parse.raw_name,
        title=extraction.title,
        year=extraction.year or parse.year,
        season=extraction.season or parse.season,
        episode=extraction.episode or parse.episode,
        episode_title=parse.episode_title,
        media_type=media_type,
        resolution=parse.resolution,
        source=parse.source,
        release_group=parse.release_group,
        codec=parse.codec,
        color_depth=parse.color_depth,
        hdr_profiles=parse.hdr_profiles,
        container=parse.container,
        audio_codec=parse.audio_codec,
        raw=parse.raw,
    )
    return new_parse, candidates, f"llm_rescue: title={extraction.title!r}"


def identify(
    path: str,
    provider: MetadataProvider,
    *,
    llm_api_key: str | None = None,
) -> IdentifyResult:
    """完整 pipeline：解析文件名 → provider.search → 三级 fallback top_pick。

    Pick 优先级（节省 LLM token）:
      0. guessit fail / TMDB 0 候选 + llm_api_key 可用 → LLM 重新解析文件名
         （针对中文 release / 模糊命名）后重搜 TMDB
      1. single_exact：候选只有 1 个且 title/original_title exact match → 直接绑
      2. heuristic：_pick_top 算出 ≥ 0.9 → 直接绑（很高把握）
      3. llm：heuristic 在 [0, 0.9) 且 llm_api_key 可用 → 走契约 #3 grounded select
      4. needs_review：以上都 fail → top_pick=None
    """
    parse = parse_filename(path)
    rescue_reason = ""

    # Step 0: extras / featurette / trailer / sample 等附属文件 → 短路
    # 不调 TMDB（省 API + 避免 mismatch），UI 上单独归类，不和 main feature 混展
    if parse.media_type == "extra":
        extra_kind = parse.raw.get("_extra_kind", "extra")
        return IdentifyResult(
            parse=parse,
            candidates=[],
            top_pick=None,
            confidence=1.0,
            reasoning=f"detected as {extra_kind} (附属文件，不调 TMDB)",
            pick_source="extra",
        )

    # Step 0.5: 多盘分段第 2+ 盘 → 短路（库视图隐藏，详情卡聚合）
    # 第 1 盘走正常 movie/tv 识别流程
    if parse.media_type == "part":
        part_index = parse.raw.get("_part_index")
        return IdentifyResult(
            parse=parse,
            candidates=[],
            top_pick=None,
            confidence=1.0,
            reasoning=f"multi-disc part #{part_index} (附属于主片，不调 TMDB)",
            pick_source="part",
        )

    # Step 0a: guessit 没拿到 title → LLM rescue（如果可用）
    if not parse.title and llm_api_key:
        parse, candidates, rescue_reason = _llm_filename_rescue(path, parse, provider, llm_api_key)
    elif not parse.title:
        return IdentifyResult(
            parse=parse,
            candidates=[],
            top_pick=None,
            confidence=0.0,
            reasoning="guessit failed to parse title (LLM not configured to rescue)",
            pick_source="needs_review",
        )
    else:
        candidates = provider.search(
            title=parse.title,
            year=parse.year,
            media_type=parse.media_type,
        )

    # Step 0b: 有 title 但 provider 返 0 → LLM 重解析（也许 guessit 截出的 title 不对）
    if not candidates and llm_api_key:
        new_parse, new_cands, rescue_reason = _llm_filename_rescue(
            path, parse, provider, llm_api_key
        )
        if new_cands:
            parse, candidates = new_parse, new_cands

    if not candidates:
        return IdentifyResult(
            parse=parse,
            candidates=[],
            top_pick=None,
            confidence=0.0,
            reasoning=f"no candidates from provider{' (' + rescue_reason + ')' if rescue_reason else ''}",
            pick_source="needs_review",
        )

    # 路径 1: 唯一候选 + title exact → 直接绑（省 token，最常见 happy path）
    if len(candidates) == 1:
        c = candidates[0]
        norm_want = _normalize_title(parse.title)
        norm_t = _normalize_title(c.title)
        norm_o = _normalize_title(c.original_title or "")
        if norm_want and norm_want in (norm_t, norm_o):
            return IdentifyResult(
                parse=parse,
                candidates=candidates,
                top_pick=c,
                confidence=0.95,
                reasoning="only candidate with exact title match",
                pick_source="single_exact",
            )

    # 路径 2: heuristic 高分（≥0.9）→ 直接绑
    heur_top, heur_score, heur_reason = _pick_top(parse, candidates)
    if heur_top is not None and heur_score >= 0.9:
        return IdentifyResult(
            parse=parse,
            candidates=candidates,
            top_pick=heur_top,
            confidence=heur_score,
            reasoning=f"heuristic high: {heur_reason}",
            pick_source="heuristic",
        )

    # 路径 3: LLM 可用 → grounded select
    if llm_api_key:
        parse_dict = {
            "title": parse.title,
            "year": parse.year,
            "season": parse.season,
            "episode": parse.episode,
            "episode_title": parse.episode_title,
            "media_type": parse.media_type,
            "resolution": parse.resolution,
            "source": parse.source,
        }
        cand_dicts = [
            {
                "id": c.id,
                "title": c.title,
                "original_title": c.original_title,
                "year": c.year,
                "media_type": c.media_type,
                "overview": c.overview,
                "vote_average": c.vote_average,
            }
            for c in candidates
        ]
        sel = llm.select_candidate(parse_dict, cand_dicts, api_key=llm_api_key)
        if sel.selected_id:
            # 找回对应的 MediaCandidate object（契约 #3 已 enforce id ∈ candidates）
            chosen = next((c for c in candidates if c.id == sel.selected_id), None)
            if chosen:
                return IdentifyResult(
                    parse=parse,
                    candidates=candidates,
                    top_pick=chosen,
                    confidence=sel.confidence,
                    reasoning=f"llm: {sel.reasoning}",
                    pick_source="llm",
                )

        # LLM 没选出或失败（timeout / API error / no_candidates）→
        # fallback 到 heuristic top_pick 给用户一个 best-guess（不是 needs_review 死局）
        # 仅当 heuristic 有 ≥ 0.5 把握时 fallback；更低就 needs_review
        is_llm_error = sel.reasoning.startswith("llm_error") or "timeout" in sel.reasoning.lower()
        if heur_top is not None and heur_score >= 0.5:
            label = "heuristic_fallback_after_llm_error" if is_llm_error else "heuristic_fallback"
            return IdentifyResult(
                parse=parse,
                candidates=candidates,
                top_pick=heur_top,
                confidence=heur_score,
                reasoning=f"{label}: llm said '{sel.reasoning}'; heuristic best-guess: {heur_reason}",
                pick_source="heuristic",
            )

        # 真没把握 → needs_review，但仍透传 LLM 失败原因 + heuristic top（让用户在 UI 看到候选 #1）
        return IdentifyResult(
            parse=parse,
            candidates=candidates,
            top_pick=None,
            confidence=sel.confidence,
            reasoning=f"llm needs_review: {sel.reasoning}",
            pick_source="needs_review",
        )

    # 路径 4: LLM 不可用 → needs_review，附 heuristic reasoning
    return IdentifyResult(
        parse=parse,
        candidates=candidates,
        top_pick=heur_top,
        confidence=heur_score,
        reasoning=f"heuristic: {heur_reason} (LLM not configured, would help)",
        pick_source="heuristic" if heur_top else "needs_review",
    )
