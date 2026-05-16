"""TMDB metadata provider（v3 API，query-param 鉴权）。

API docs: https://developer.themoviedb.org/reference/intro/getting-started

Phase 2 spike: BYOK API key（用户 UI 配置 → config/.tmdb_key 落盘）。
默认 language=zh-CN，回退 en-US。免费 tier rate limit 40 req / 10s。
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .base import MediaCandidate, MediaDetails, MetadataProvider

logger = logging.getLogger(__name__)

BASE = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/w500"


class TMDBProvider(MetadataProvider):
    name = "tmdb"

    def __init__(self, api_key: str, language: str = "zh-CN"):
        if not api_key:
            raise ValueError("TMDB api_key required")
        self.api_key = api_key
        self.language = language
        self._session = requests.Session()

    def _get(self, path: str, **params) -> dict[str, Any]:
        params.setdefault("api_key", self.api_key)
        params.setdefault("language", self.language)
        r = self._session.get(f"{BASE}{path}", params=params, timeout=15)
        if r.status_code == 401:
            raise RuntimeError("TMDB auth failed (invalid api_key)")
        if r.status_code == 429:
            raise RuntimeError("TMDB rate limit exceeded (40 req / 10s)")
        r.raise_for_status()
        return r.json()

    def _movie_to_candidate(self, m: dict) -> MediaCandidate:
        return MediaCandidate(
            id=f"tmdb:movie:{m['id']}",
            external_ids={"tmdb_id": str(m["id"])},
            title=m.get("title") or m.get("name") or "",
            original_title=m.get("original_title"),
            year=_year_from(m.get("release_date")),
            media_type="movie",
            poster_url=_poster_url(m.get("poster_path")),
            overview=m.get("overview"),
            vote_average=m.get("vote_average"),
            raw=m,
        )

    def _tv_to_candidate(self, t: dict) -> MediaCandidate:
        return MediaCandidate(
            id=f"tmdb:tv:{t['id']}",
            external_ids={"tmdb_id": str(t["id"])},
            title=t.get("name") or "",
            original_title=t.get("original_name"),
            year=_year_from(t.get("first_air_date")),
            media_type="tv",
            poster_url=_poster_url(t.get("poster_path")),
            overview=t.get("overview"),
            vote_average=t.get("vote_average"),
            raw=t,
        )

    def search(
        self,
        title: str,
        year: int | None = None,
        media_type: str | None = None,
    ) -> list[MediaCandidate]:
        if not title.strip():
            return []
        candidates: list[MediaCandidate] = []
        # 当 media_type 不指定时同时搜 movie + tv，按 vote_average 排序
        search_movie = media_type in (None, "movie", "episode")
        search_tv = media_type in (None, "tv", "episode")
        # episode 视为 tv（guessit 给的是 "episode"，TMDB 没有"episode"独立搜索，找剧集）
        if media_type == "episode":
            search_movie = False
            search_tv = True

        if search_movie:
            params: dict[str, Any] = {"query": title}
            if year:
                params["year"] = year
            try:
                data = self._get("/search/movie", **params)
                for m in (data.get("results") or [])[:5]:
                    candidates.append(self._movie_to_candidate(m))
            except Exception as e:
                logger.error(f"[tmdb] movie search failed: {e}")

        if search_tv:
            # TV 搜索故意不传 first_air_date_year — multi-season 剧的 first_air_date 是
            # S01 首播年，但文件名里的 year 多半是 episode air year / release year
            # (典型: "All Creatures Great and Small S02 2021" 实际剧 first_air_date=2020)。
            # year 留给下游 _pick_top 做 soft score boost，不在这里 hard filter 误杀。
            try:
                data = self._get("/search/tv", query=title)
                for t in (data.get("results") or [])[:5]:
                    candidates.append(self._tv_to_candidate(t))
            except Exception as e:
                logger.error(f"[tmdb] tv search failed: {e}")

        return candidates[:10]

    def lookup_by_id(
        self,
        external_id: str,
        id_type: str = "tmdb_id",
        media_type: str = "movie",
        season: int | None = None,
        episode: int | None = None,
    ) -> MediaDetails | None:
        if id_type != "tmdb_id":
            raise NotImplementedError(f"id_type={id_type} not supported by TMDB; only tmdb_id")
        if media_type not in ("movie", "tv"):
            raise ValueError(f"media_type must be movie or tv, got {media_type}")

        try:
            if media_type == "movie":
                m = self._get(f"/movie/{external_id}", append_to_response="credits")
                cand = self._movie_to_candidate(m)
                cast = [c["name"] for c in (m.get("credits", {}).get("cast") or [])[:10]]
                genres = [g["name"] for g in (m.get("genres") or [])]
                return MediaDetails(
                    candidate=cand,
                    cast=cast,
                    genres=genres,
                    runtime_minutes=m.get("runtime"),
                )
            t = self._get(f"/tv/{external_id}", append_to_response="credits")
            cand = self._tv_to_candidate(t)
            cast = [c["name"] for c in (t.get("credits", {}).get("cast") or [])[:10]]
            genres = [g["name"] for g in (t.get("genres") or [])]
            ep_info = None
            if season is not None and episode is not None:
                try:
                    ep = self._get(f"/tv/{external_id}/season/{season}/episode/{episode}")
                    ep_info = {
                        "season_number": ep.get("season_number"),
                        "episode_number": ep.get("episode_number"),
                        "name": ep.get("name"),
                        "overview": ep.get("overview"),
                        "still_url": _poster_url(ep.get("still_path")),
                        "air_date": ep.get("air_date"),
                        "vote_average": ep.get("vote_average"),
                    }
                except Exception as e:
                    logger.warning(f"[tmdb] episode lookup failed: {e}")
            return MediaDetails(
                candidate=cand,
                episode=ep_info,
                cast=cast,
                genres=genres,
                runtime_minutes=None,
            )
        except Exception as e:
            logger.error(f"[tmdb] lookup_by_id({external_id}, {media_type}) failed: {e}")
            return None

    def test_connection(self) -> dict:
        try:
            # /configuration 是无副作用的最便宜验证 key 的 endpoint
            data = self._get("/configuration")
            # rate-limit header（v3 API 在 response headers 里）
            return {
                "ok": True,
                "message": "TMDB OK",
                "images_base_url": data.get("images", {}).get("base_url"),
            }
        except RuntimeError as e:
            return {"ok": False, "message": str(e)}
        except requests.RequestException as e:
            return {"ok": False, "message": f"network: {e}"}


def _year_from(date_str: str | None) -> int | None:
    if not date_str:
        return None
    try:
        return int(date_str[:4])
    except (ValueError, IndexError):
        return None


def _poster_url(poster_path: str | None) -> str | None:
    if not poster_path:
        return None
    return f"{IMAGE_BASE}{poster_path}"
