"""Emby watch-source provider.

Fetches the user's IsPlayed items (movies + episodes) and normalizes them
into our internal WatchedItem shape (see services/watch_sync.WatchedItem).

Key Emby API gotchas:
- Episode items expose ProviderIds.Tmdb that is the **episode** tmdb id,
  NOT the series id. Series tmdb id must be looked up via SeriesId →
  /Items/{SeriesId}?Fields=ProviderIds. We prefetch series in batches.
- Auth header is `X-Emby-Token: <api_key>`.
- LastPlayedDate is ISO-8601 UTC with optional fractional seconds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import requests

from services import http_client

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbyItem:
    """Raw Emby item shape we care about (Movie or Episode)."""

    id: str  # Emby internal ItemId (used as provider_item_id)
    type: str  # 'Movie' or 'Episode'
    name: str | None
    year: int | None
    tmdb_id: str | None  # ProviderIds.Tmdb (episode id for Episode, movie id for Movie)
    imdb_id: str | None
    series_id: str | None  # Episode only — internal Emby SeriesId
    season_number: int | None
    episode_number: int | None
    last_played_at: int | None  # unix ts, UTC


class EmbyAuthError(RuntimeError):
    """Raised when Emby returns 401/403."""


class EmbyClient:
    """Minimal Emby REST client (HTTP, sync). Stateless aside from session reuse."""

    def __init__(
        self,
        base_url: str,
        user_id: str,
        api_key: str,
        *,
        timeout: float = 15.0,
    ):
        if not base_url:
            raise ValueError("base_url required")
        if not user_id:
            raise ValueError("user_id required")
        if not api_key:
            raise ValueError("api_key required")
        self.base_url = base_url.rstrip("/")
        self.user_id = user_id
        self.api_key = api_key
        self.timeout = timeout
        # LAN host (Emby 一般在 NAS / 同网段) bypass shell http_proxy 直连 —
        # 否则装 Clash/V2Ray 的 user 撞 502 (proxy 不路由内网 IP).
        self._session = http_client.session_for(self.base_url)
        # X-Emby-Token is the documented auth header (Jellyfin also accepts it as
        # an alias for X-MediaBrowser-Token); see Emby Swagger.
        self._session.headers["X-Emby-Token"] = api_key
        self._session.headers["Accept"] = "application/json"

    def _get(self, path: str, params: dict | None = None) -> Any:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        resp = self._session.get(url, params=params or {}, timeout=self.timeout)
        if resp.status_code in (401, 403):
            raise EmbyAuthError(f"Emby auth failed: HTTP {resp.status_code}")
        resp.raise_for_status()
        return resp.json()

    def test_connection(self) -> dict:
        """Returns {ok, message, server_name?} without throwing."""
        try:
            info = self._get("System/Info/Public")
            return {
                "ok": True,
                "message": f"Emby OK ({info.get('ServerName', '?')})",
                "server_name": info.get("ServerName"),
                "version": info.get("Version"),
            }
        except EmbyAuthError as e:
            return {"ok": False, "message": str(e), "code": "auth_failed"}
        except requests.RequestException as e:
            return {"ok": False, "message": f"network: {e}", "code": "network"}
        except Exception as e:
            return {"ok": False, "message": str(e), "code": "unknown"}

    def list_watched(
        self,
        *,
        since_iso: str | None = None,
        limit: int = 5000,
    ) -> list[EmbyItem]:
        """Fetch IsPlayed Movies + Episodes for user_id.

        since_iso: optional ISO-8601 datetime; passed as MinDateLastSaved filter.
        Emby returns up to `Limit` items per call. For libraries >5000 watched
        items, page externally by calling with successively older since_iso.
        """
        params: dict[str, str] = {
            "Filters": "IsPlayed",
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode",
            "Fields": (
                "ProviderIds,UserData,SeriesName,SeriesId,"
                "IndexNumber,ParentIndexNumber,ProductionYear,Path"
            ),
            "SortBy": "DatePlayed",
            "SortOrder": "Descending",
            "Limit": str(limit),
        }
        if since_iso:
            # Emby uses MinDateLastSaved for "modified since"; for played items
            # specifically the per-user UserData.LastPlayedDate is what we
            # care about, but the closest server-side filter is MinDate which
            # filters on Date Added. We still send it as a hint; sync layer
            # double-checks by comparing last_played_at vs since.
            params["MinDate"] = since_iso

        data = self._get(f"Users/{self.user_id}/Items", params=params)
        items_raw = data.get("Items") or []
        return [_parse_item(it) for it in items_raw]

    def get_series_tmdb_ids(self, series_ids: list[str]) -> dict[str, str | None]:
        """Batch lookup: emby series internal Id → tmdb series id (or None).

        Used by sync to resolve Episode.SeriesId → series tmdb id for the
        fallback (series + season + episode) match path.
        """
        if not series_ids:
            return {}
        # Emby supports Ids comma-separated to /Items
        params = {
            "Ids": ",".join(series_ids),
            "Fields": "ProviderIds",
        }
        data = self._get(f"Users/{self.user_id}/Items", params=params)
        out: dict[str, str | None] = {}
        for it in data.get("Items") or []:
            sid = str(it.get("Id"))
            providers = it.get("ProviderIds") or {}
            out[sid] = providers.get("Tmdb")
        for sid in series_ids:
            out.setdefault(sid, None)
        return out


def _parse_item(raw: dict) -> EmbyItem:
    providers = raw.get("ProviderIds") or {}
    user_data = raw.get("UserData") or {}
    last_played = _parse_iso_to_ts(user_data.get("LastPlayedDate"))
    return EmbyItem(
        id=str(raw.get("Id") or ""),
        type=str(raw.get("Type") or ""),
        name=raw.get("Name"),
        year=raw.get("ProductionYear"),
        tmdb_id=providers.get("Tmdb"),
        imdb_id=providers.get("Imdb"),
        series_id=str(raw["SeriesId"]) if raw.get("SeriesId") else None,
        season_number=raw.get("ParentIndexNumber"),
        episode_number=raw.get("IndexNumber"),
        last_played_at=last_played,
    )


def _parse_iso_to_ts(s: str | None) -> int | None:
    """Emby returns timestamps like '2026-05-12T14:30:00.0000000Z'. Parse to unix ts."""
    if not s:
        return None
    try:
        # Strip extra fractional digits beyond 6 (Python max)
        if "." in s:
            head, frac_and_tz = s.split(".", 1)
            # frac may be followed by Z or +HH:MM
            for tz_sep in ("Z", "+", "-"):
                if tz_sep in frac_and_tz:
                    # Locate tz separator (skip leading digits)
                    for i in range(len(frac_and_tz)):
                        if frac_and_tz[i] in "Z+-":
                            frac = frac_and_tz[:i][:6]
                            tz = frac_and_tz[i:]
                            s = f"{head}.{frac}{tz}"
                            break
                    break
            else:
                s = head + "." + frac_and_tz[:6]
        # Normalize trailing Z to +00:00 (fromisoformat in Python 3.10 doesn't accept Z)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return int(dt.timestamp())
    except (ValueError, IndexError) as e:
        logger.warning("emby: failed to parse iso datetime %r: %s", s, e)
        return None
