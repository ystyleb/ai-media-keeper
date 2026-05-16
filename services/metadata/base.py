"""Metadata provider abstraction — 契约 #3 grounded candidates 的载体。

每个 provider 实现 search() 返回 1-10 真实 MediaCandidate（永远不返回伪造结果），
LLM 或用户从这些 candidates 里 select_id；下游绑定时强制 selected ∈ {c.id for c in candidates}。

Phase 2 spike 阶段只有 TMDBProvider；豆瓣 / TVDB 留给 Phase 2 全量。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class MediaCandidate:
    """Provider 返回的候选项。id 唯一，下游 LLM/用户只能从已返回的 id 集合中选。"""

    id: str  # provider 内部 id（TMDB 的 movie.id / tv.id 转字符串）
    external_ids: dict[str, str]  # {"tmdb_id": "...", "imdb_id": "...", "tvdb_id": "..."}
    title: str
    original_title: str | None
    year: int | None
    media_type: str  # 'movie' | 'tv'
    poster_url: str | None
    overview: str | None
    vote_average: float | None  # 0-10
    raw: dict[str, Any] = field(default_factory=dict)  # provider 原始响应，作 provenance


@dataclass(frozen=True)
class MediaDetails:
    """lookup_by_id 返回的完整详情（候选 + season/episode 列表 + 演员等）。"""

    candidate: MediaCandidate
    episode: dict[str, Any] | None = None  # tv 时按 season/episode 拉到的某集详情
    cast: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    runtime_minutes: int | None = None


class MetadataProvider(ABC):
    """所有 metadata source 的统一接口。"""

    name: str  # 'tmdb' | 'douban' | 'tvdb'

    @abstractmethod
    def search(
        self,
        title: str,
        year: int | None = None,
        media_type: str | None = None,
    ) -> list[MediaCandidate]:
        """按 title 搜索，返回 1-10 个真实候选；找不到返回空列表，**永远不伪造**。"""

    @abstractmethod
    def lookup_by_id(
        self,
        external_id: str,
        id_type: str = "tmdb_id",
        media_type: str = "movie",
        season: int | None = None,
        episode: int | None = None,
    ) -> MediaDetails | None:
        """按外部 ID 拉详情。tv 类型可附 season/episode 拉单集信息。"""

    @abstractmethod
    def test_connection(self) -> dict:
        """返回 {ok: bool, message: str, rate_limit_remaining?: int}"""
