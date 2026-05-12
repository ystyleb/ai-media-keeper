"""NFO XML 生成器 — Emby/Jellyfin/Kodi 兼容格式。

把 MediaCandidate + 解析结果序列化成 <episodedetails> / <movie> / <tvshow> 三种 NFO。
镜像 app._parse_emby_nfo 读取的字段，写出来的能被同一 parser 解析回来（round-trip）。

Reference schema (Emby/Jellyfin/Kodi 公共子集):
  https://kodi.wiki/view/NFO_files/Episodes
  https://kodi.wiki/view/NFO_files/Movies
  https://kodi.wiki/view/NFO_files/TV_shows

设计原则：
  - 纯函数，**不做 SSH I/O**。SSH 写在 app.py 的 executor 里
  - XML declaration 用 UTF-8（Emby/Jellyfin 默认编码）
  - 缺字段就跳过对应 element，不写空标签（Emby 会把 <plot></plot> 当成空字符串覆盖）
  - <uniqueid> + 兼容旧 <tmdbid>/<imdbid> 双写，最大兼容性
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from xml.dom import minidom


@dataclass(frozen=True)
class NFOPayload:
    """构造 NFO 需要的输入（来自 identify 结果 + lookup_by_id）。"""
    media_type: str                   # 'episode' | 'movie' | 'tvshow'
    title: str                        # 本地化标题（zh-CN 下"瑞克和莫蒂"）
    original_title: str | None        # 原始语言标题
    year: int | None
    plot: str | None                  # overview
    tmdb_id: str | None
    imdb_id: str | None
    tvdb_id: str | None
    rating: float | None              # vote_average 0-10
    genres: list[str]
    cast: list[str]                   # 仅 name；NFO actor 元素需要 name+role 时降级为 only name
    runtime_minutes: int | None
    poster_url: str | None
    # episode-specific
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
    episode_overview: str | None = None
    episode_air_date: str | None = None
    episode_still_url: str | None = None


def _pretty(root: ET.Element) -> str:
    """ElementTree → 漂亮缩进的 UTF-8 XML 字符串。

    minidom.toprettyxml 会加一行 <?xml version="1.0" ?>，我们换成带 encoding 的版本。
    """
    raw = ET.tostring(root, encoding="unicode")
    parsed = minidom.parseString(raw)
    # toprettyxml 默认 4 空格 indent + 头部加 declaration
    pretty = parsed.toprettyxml(indent="  ", encoding="UTF-8")
    return pretty.decode("utf-8")


def _add_text(parent: ET.Element, tag: str, value: Any) -> None:
    """只在 value 非空时加 child element。"""
    if value is None:
        return
    s = str(value).strip()
    if not s:
        return
    el = ET.SubElement(parent, tag)
    el.text = s


def _add_uniqueids(parent: ET.Element, payload: NFOPayload) -> None:
    """Emby/Jellyfin 新格式：<uniqueid type="tmdb">123</uniqueid>。
    同时双写传统 <tmdbid>/<imdbid> tag，给老版 Kodi/旧 parser 兜底。
    """
    if payload.tmdb_id:
        u = ET.SubElement(parent, "uniqueid", {"type": "tmdb", "default": "true"})
        u.text = payload.tmdb_id
        _add_text(parent, "tmdbid", payload.tmdb_id)
    if payload.imdb_id:
        u = ET.SubElement(parent, "uniqueid", {"type": "imdb"})
        u.text = payload.imdb_id
        _add_text(parent, "imdbid", payload.imdb_id)
    if payload.tvdb_id:
        u = ET.SubElement(parent, "uniqueid", {"type": "tvdb"})
        u.text = payload.tvdb_id


def _add_actors(parent: ET.Element, names: list[str]) -> None:
    """<actor><name>...</name><role>...</role></actor>。

    我们只有 name（TMDB credits.cast 的 name），role 留空——Emby 接受。
    """
    for name in names[:15]:
        if not name:
            continue
        a = ET.SubElement(parent, "actor")
        n = ET.SubElement(a, "name")
        n.text = name


def _add_genres(parent: ET.Element, genres: list[str]) -> None:
    for g in genres:
        if g:
            _add_text(parent, "genre", g)


def build_episode_nfo(payload: NFOPayload) -> str:
    """剧集单集 NFO。<episodedetails> root。

    必填：title (集名 优先 episode_title，否则 fallback 到剧名 + sNNeNN)
        season / episode
    选填：plot (用 episode_overview > 总剧 plot)
        aired (episode_air_date)
        rating (优先 episode rating，spike 不传 → 用剧整体 rating)
        thumb (still_url 优先，否则 poster_url)
        showtitle (剧名)
    """
    root = ET.Element("episodedetails")
    # title：episode_title 优先；否则用 "剧名 SxxExx"
    title = payload.episode_title or (
        f"{payload.title} S{payload.season:02d}E{payload.episode:02d}"
        if payload.season is not None and payload.episode is not None
        else payload.title
    )
    _add_text(root, "title", title)
    _add_text(root, "originaltitle", payload.original_title)
    _add_text(root, "showtitle", payload.title)
    _add_text(root, "season", payload.season)
    _add_text(root, "episode", payload.episode)
    _add_text(root, "plot", payload.episode_overview or payload.plot)
    _add_text(root, "year", payload.year)
    _add_text(root, "aired", payload.episode_air_date)
    if payload.rating is not None:
        _add_text(root, "rating", f"{payload.rating:.1f}")
    if payload.runtime_minutes:
        _add_text(root, "runtime", payload.runtime_minutes)
    # thumb：单集 still 优先，没有就用剧 poster（Emby 会 fallback）
    if payload.episode_still_url or payload.poster_url:
        _add_text(root, "thumb", payload.episode_still_url or payload.poster_url)
    _add_uniqueids(root, payload)
    _add_genres(root, payload.genres)
    _add_actors(root, payload.cast)
    return _pretty(root)


def build_movie_nfo(payload: NFOPayload) -> str:
    """电影 NFO。<movie> root。"""
    root = ET.Element("movie")
    _add_text(root, "title", payload.title)
    _add_text(root, "originaltitle", payload.original_title)
    _add_text(root, "plot", payload.plot)
    _add_text(root, "year", payload.year)
    if payload.rating is not None:
        _add_text(root, "rating", f"{payload.rating:.1f}")
    if payload.runtime_minutes:
        _add_text(root, "runtime", payload.runtime_minutes)
    if payload.poster_url:
        _add_text(root, "thumb", payload.poster_url)
    _add_uniqueids(root, payload)
    _add_genres(root, payload.genres)
    _add_actors(root, payload.cast)
    return _pretty(root)


def build_tvshow_nfo(payload: NFOPayload) -> str:
    """剧名级 NFO（同目录的 tvshow.nfo）。<tvshow> root。

    一个剧只写一次（season 目录的 parent 下），不跟单集 episode NFO 重复。
    """
    root = ET.Element("tvshow")
    _add_text(root, "title", payload.title)
    _add_text(root, "originaltitle", payload.original_title)
    _add_text(root, "plot", payload.plot)
    _add_text(root, "year", payload.year)
    _add_text(root, "premiered", f"{payload.year}-01-01" if payload.year else None)
    if payload.rating is not None:
        _add_text(root, "rating", f"{payload.rating:.1f}")
    if payload.poster_url:
        _add_text(root, "thumb", payload.poster_url)
    _add_uniqueids(root, payload)
    _add_genres(root, payload.genres)
    _add_actors(root, payload.cast)
    return _pretty(root)


def build_nfo(payload: NFOPayload) -> str:
    """根据 media_type 路由到具体 builder。"""
    if payload.media_type == "episode":
        return build_episode_nfo(payload)
    if payload.media_type == "movie":
        return build_movie_nfo(payload)
    if payload.media_type == "tvshow":
        return build_tvshow_nfo(payload)
    raise ValueError(f"unknown media_type: {payload.media_type!r}")


def nfo_path_for_video(video_path: str) -> str:
    """把 /share/.../Show.S01E01.mkv → /share/.../Show.S01E01.nfo。

    Emby/Jellyfin/Kodi 共识：sibling 同名 .nfo（替换扩展名）。
    """
    if "." not in video_path.rsplit("/", 1)[-1]:
        return video_path + ".nfo"
    return video_path.rsplit(".", 1)[0] + ".nfo"
