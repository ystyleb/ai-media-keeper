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
    media_type: str  # 'movie' | 'episode' | 'unknown'
    resolution: str | None
    source: str | None
    release_group: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class IdentifyResult:
    parse: FilenameParse
    candidates: list[MediaCandidate]
    top_pick: MediaCandidate | None  # None = needs_review
    confidence: float                # 0.0-1.0
    reasoning: str
    pick_source: str                 # 'single_exact' | 'heuristic' | 'llm' | 'needs_review'


def parse_filename(path: str) -> FilenameParse:
    """用 guessit 解析路径末尾的文件名。"""
    name = path.rsplit("/", 1)[-1]
    g = dict(guessit(name))
    media_type = "movie"
    if g.get("type") == "episode":
        media_type = "episode"
    elif g.get("type") == "movie":
        media_type = "movie"
    elif g.get("season") or g.get("episode"):
        media_type = "episode"

    return FilenameParse(
        raw_name=name,
        title=g.get("title", ""),
        year=g.get("year"),
        season=g.get("season"),
        episode=g.get("episode"),
        episode_title=g.get("episode_title"),
        media_type=media_type,
        resolution=str(g.get("screen_size") or "") or None,
        source=str(g.get("source") or "") or None,
        release_group=str(g.get("release_group") or "") or None,
        raw=g,
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
    media_type = extraction.media_type if extraction.media_type != "unknown" else parse.media_type or "movie"
    candidates = provider.search(
        title=extraction.title, year=extraction.year, media_type=media_type,
    )
    # 如果 alt_title 不同且 candidates 仍空，试 alt
    if not candidates and extraction.alt_title and extraction.alt_title != extraction.title:
        candidates = provider.search(
            title=extraction.alt_title, year=extraction.year, media_type=media_type,
        )

    # 更新 parse：用 LLM 的 title / year / season / episode 替换（guessit 拿不到的部分）
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

    # Step 0a: guessit 没拿到 title → LLM rescue（如果可用）
    if not parse.title and llm_api_key:
        parse, candidates, rescue_reason = _llm_filename_rescue(path, parse, provider, llm_api_key)
    elif not parse.title:
        return IdentifyResult(
            parse=parse, candidates=[], top_pick=None,
            confidence=0.0, reasoning="guessit failed to parse title (LLM not configured to rescue)",
            pick_source="needs_review",
        )
    else:
        candidates = provider.search(
            title=parse.title, year=parse.year, media_type=parse.media_type,
        )

    # Step 0b: 有 title 但 provider 返 0 → LLM 重解析（也许 guessit 截出的 title 不对）
    if not candidates and llm_api_key:
        new_parse, new_cands, rescue_reason = _llm_filename_rescue(path, parse, provider, llm_api_key)
        if new_cands:
            parse, candidates = new_parse, new_cands

    if not candidates:
        return IdentifyResult(
            parse=parse, candidates=[], top_pick=None,
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
                parse=parse, candidates=candidates, top_pick=c,
                confidence=0.95, reasoning="only candidate with exact title match",
                pick_source="single_exact",
            )

    # 路径 2: heuristic 高分（≥0.9）→ 直接绑
    heur_top, heur_score, heur_reason = _pick_top(parse, candidates)
    if heur_top is not None and heur_score >= 0.9:
        return IdentifyResult(
            parse=parse, candidates=candidates, top_pick=heur_top,
            confidence=heur_score, reasoning=f"heuristic high: {heur_reason}",
            pick_source="heuristic",
        )

    # 路径 3: LLM 可用 → grounded select
    if llm_api_key:
        parse_dict = {
            "title": parse.title, "year": parse.year,
            "season": parse.season, "episode": parse.episode,
            "episode_title": parse.episode_title,
            "media_type": parse.media_type,
            "resolution": parse.resolution, "source": parse.source,
        }
        cand_dicts = [{
            "id": c.id, "title": c.title, "original_title": c.original_title,
            "year": c.year, "media_type": c.media_type,
            "overview": c.overview, "vote_average": c.vote_average,
        } for c in candidates]
        sel = llm.select_candidate(parse_dict, cand_dicts, api_key=llm_api_key)
        if sel.selected_id:
            # 找回对应的 MediaCandidate object（契约 #3 已 enforce id ∈ candidates）
            chosen = next((c for c in candidates if c.id == sel.selected_id), None)
            if chosen:
                return IdentifyResult(
                    parse=parse, candidates=candidates, top_pick=chosen,
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
                parse=parse, candidates=candidates, top_pick=heur_top,
                confidence=heur_score,
                reasoning=f"{label}: llm said '{sel.reasoning}'; heuristic best-guess: {heur_reason}",
                pick_source="heuristic",
            )

        # 真没把握 → needs_review，但仍透传 LLM 失败原因 + heuristic top（让用户在 UI 看到候选 #1）
        return IdentifyResult(
            parse=parse, candidates=candidates, top_pick=None,
            confidence=sel.confidence,
            reasoning=f"llm needs_review: {sel.reasoning}",
            pick_source="needs_review",
        )

    # 路径 4: LLM 不可用 → needs_review，附 heuristic reasoning
    return IdentifyResult(
        parse=parse, candidates=candidates, top_pick=heur_top,
        confidence=heur_score,
        reasoning=f"heuristic: {heur_reason} (LLM not configured, would help)",
        pick_source="heuristic" if heur_top else "needs_review",
    )
