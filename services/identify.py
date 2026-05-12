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
        got = _normalize_title(c.title)
        if got == want:
            score += 0.7
            reasons.append("title exact")
        elif want in got or got in want:
            score += 0.4
            reasons.append("title substring")

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


def identify(path: str, provider: MetadataProvider) -> IdentifyResult:
    """完整 pipeline：解析文件名 → provider.search → top_pick。"""
    parse = parse_filename(path)
    if not parse.title:
        return IdentifyResult(
            parse=parse,
            candidates=[],
            top_pick=None,
            confidence=0.0,
            reasoning="guessit failed to parse title from filename",
        )
    candidates = provider.search(
        title=parse.title, year=parse.year, media_type=parse.media_type
    )
    top, score, reasoning = _pick_top(parse, candidates)
    return IdentifyResult(
        parse=parse,
        candidates=candidates,
        top_pick=top,
        confidence=score,
        reasoning=reasoning,
    )
