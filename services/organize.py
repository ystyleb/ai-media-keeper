"""Phase 4A: organize 路径推断（hardlink 目标路径 + NFO 路径）。

输入：源文件路径 + 已识别的 CachedMetadata + MOVIES_ROOT/TV_ROOT
输出：OrganizePlan（dst_dir / dst_path / nfo_path / tvshow_nfo_path）

只做纯计算，不调 SSH、不调 DB。所有 SSH 副作用在 app.py 的 _organize_executor。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# 文件系统不安全字符：/ \ : * ? " < > | 与控制字符
_UNSAFE_CHARS = re.compile(r'[\/\\:\*\?"<>\|\x00-\x1f]')
_MULTI_SPACE = re.compile(r"\s+")
# 检测 title 末尾是否已有 (YYYY) — 避免双重 year 追加
_TRAILING_YEAR = re.compile(r"\s*\((19|20)\d{2}\)\s*$")


class OrganizeNotApplicable(Exception):
    """media_type 不支持 organize 或缺关键字段。"""


@dataclass(frozen=True)
class OrganizePlan:
    src_path: str
    src_basename: str
    media_type: str
    title: str
    year: int | None
    tmdb_id: str | None
    season_number: int | None
    episode_number: int | None
    dst_dir: str
    dst_path: str
    nfo_path: str
    tvshow_nfo_path: str | None


def sanitize_for_path(name: str) -> str:
    """文件系统安全化：替换不安全字符、合并多空格、去头尾空白 + 末尾 dot/space。

    末尾 dot/space 在 SMB/CIFS 有兼容问题 → 主动去掉。
    """
    if not name:
        return ""
    cleaned = _UNSAFE_CHARS.sub("-", name)
    cleaned = _MULTI_SPACE.sub(" ", cleaned).strip()
    cleaned = cleaned.rstrip(". ")
    return cleaned


def _title_with_year(safe_title: str, year: int | None) -> str:
    """如果 title 已含 (YYYY) → 不再追加 year；否则按 'Title (Year)' 形态拼接。"""
    if not year:
        return safe_title
    if _TRAILING_YEAR.search(safe_title):
        return safe_title
    return f"{safe_title} ({year})"


def compute_organize_plan(
    src_path: str,
    cached,
    movies_root: str,
    tv_root: str,
) -> OrganizePlan:
    """根据已识别 metadata 推断目标路径。

    cached: services.metadata_cache.CachedMetadata（duck-typed:
        需要 title / year / media_type / tmdb_id / season_number / episode_number 字段）

    Raises OrganizeNotApplicable 当：
      - cached.title 经 sanitize 后为空
      - tv 但缺 season_number 或 episode_number
      - media_type 不是 'movie' / 'tv'
    """
    src_basename = os.path.basename(src_path)
    safe_title = sanitize_for_path(cached.title or "")
    if not safe_title:
        raise OrganizeNotApplicable("title is empty after sanitize")
    stem = os.path.splitext(src_basename)[0]

    if cached.media_type == "movie":
        dirname = _title_with_year(safe_title, cached.year)
        dst_dir = f"{movies_root.rstrip('/')}/{dirname}"
        return OrganizePlan(
            src_path=src_path,
            src_basename=src_basename,
            media_type="movie",
            title=cached.title,
            year=cached.year,
            tmdb_id=cached.tmdb_id,
            season_number=None,
            episode_number=None,
            dst_dir=dst_dir,
            dst_path=f"{dst_dir}/{src_basename}",
            nfo_path=f"{dst_dir}/{stem}.nfo",
            tvshow_nfo_path=None,
        )
    if cached.media_type == "tv":
        if cached.season_number is None or cached.episode_number is None:
            raise OrganizeNotApplicable("tv requires season + episode numbers")
        series_dir = _title_with_year(safe_title, cached.year)
        series_full = f"{tv_root.rstrip('/')}/{series_dir}"
        season_full = f"{series_full}/Season {cached.season_number:02d}"
        return OrganizePlan(
            src_path=src_path,
            src_basename=src_basename,
            media_type="tv",
            title=cached.title,
            year=cached.year,
            tmdb_id=cached.tmdb_id,
            season_number=cached.season_number,
            episode_number=cached.episode_number,
            dst_dir=season_full,
            dst_path=f"{season_full}/{src_basename}",
            nfo_path=f"{season_full}/{stem}.nfo",
            tvshow_nfo_path=f"{series_full}/tvshow.nfo",
        )
    raise OrganizeNotApplicable(f"unsupported media_type: {cached.media_type!r}")
