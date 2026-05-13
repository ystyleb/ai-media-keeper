"""media_files 表的 upsert / get / stale 检测。

纯 SQLite 操作，不调 TMDB / 不做 SSH。调用方（routes 层）负责：
  - 拉 IdentifyResult（identify.identify）
  - 拉当前 ground truth stat（inode/size/mtime）
  - 把两者一起塞给 upsert_identification

stale 检测：mtime 变了视为文件被替换/重剪，cache 失效——调用方应触发重识别。
inode 变了在硬链接场景下不必失效（同 inode 多个 path 都合法）；但如果同 path 的 inode
变了，意味着 path 指向了一个新文件，也该失效。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CachedMetadata:
    """从 DB 读出的缓存条目。Shape 接近 IdentifyResult 但是 plain dict-friendly。"""
    path: str
    inode: int | None
    size_bytes: int | None
    mtime: int | None
    # 媒体字段
    tmdb_id: str | None
    imdb_id: str | None
    media_type: str | None
    title: str | None
    original_title: str | None
    year: int | None
    season_number: int | None
    episode_number: int | None
    episode_title: str | None
    poster_url: str | None
    overview: str | None
    vote_average: float | None
    genres: list[str]
    cast: list[str]
    runtime_minutes: int | None
    episode_air_date: str | None
    episode_overview: str | None
    episode_still_url: str | None
    # provenance
    metadata_source: str
    metadata_provider: str | None
    metadata_fetched_at: int
    metadata_status: str
    metadata_confidence: float | None
    metadata_pick_source: str | None
    metadata_reasoning: str | None
    # parse
    parse_raw_name: str | None
    parse_resolution: str | None
    parse_source: str | None
    parse_release_group: str | None
    # 时间戳
    first_seen_at: int
    last_updated_at: int


def _now() -> int:
    return int(time.time())


def upsert_identification(
    conn: sqlite3.Connection,
    *,
    path: str,
    stat: dict[str, Any],
    identify_result: Any,                # services.identify.IdentifyResult
    metadata_source: str = "tmdb",
    metadata_provider: str = "tmdb",
) -> None:
    """把 IdentifyResult + SSH stat 持久化到 media_files。

    使用 INSERT ... ON CONFLICT(path) DO UPDATE 保证 idempotent。
    needs_review / failed 的也写入，让"已尝试过"被记录（避免下次重复浪费 API）。
    """
    parse = identify_result.parse
    top = identify_result.top_pick
    now = _now()

    is_extra = parse.media_type == "extra"
    is_part = parse.media_type == "part"
    is_companion = is_extra or is_part  # 都不调 TMDB，都从库视图隐藏

    # 决定 metadata_status
    if is_companion:
        status = "ok"                               # extras/parts 明确归类，不算"待审核"
    elif top is None:
        status = "needs_review"
    else:
        status = "ok"

    # 从 top_pick 提取字段（top 可能 None → 全 None）
    tmdb_id = top.external_ids.get("tmdb_id") if top else None
    imdb_id = top.external_ids.get("imdb_id") if top else None
    if is_companion:
        media_type = parse.media_type               # 'extra' / 'part'（top 一定 None）
    else:
        media_type = top.media_type if top else None
    title = top.title if top else parse.title  # 没 top 时退化到 guessit 解析的 title
    original_title = top.original_title if top else None
    year = top.year if top else parse.year
    poster_url = top.poster_url if top else None
    overview = top.overview if top else None
    vote_average = top.vote_average if top else None

    # season/episode 优先用 parse 出来的（最贴合实际文件）
    # companions 强制 None — guessit 把 'Extras-01' / 'BD1' 解析的 S/E 是污染数据
    season_number = None if is_companion else parse.season
    episode_number = None if is_companion else parse.episode
    episode_title = None if is_companion else parse.episode_title

    # details 是可选的 — 从 IdentifyResult 拿不到，调用方如要可单独传
    # spike 阶段先 None，前端命中 cache 后展示足够，详情可按需重新调 TMDB
    genres: list[str] = []
    cast: list[str] = []
    runtime_minutes: int | None = None
    episode_air_date: str | None = None
    episode_overview: str | None = None
    episode_still_url: str | None = None

    conn.execute(
        """
        INSERT INTO media_files (
          path, inode, size_bytes, mtime,
          tmdb_id, imdb_id, media_type, title, original_title, year,
          season_number, episode_number, episode_title,
          poster_url, overview, vote_average,
          genres_json, cast_json, runtime_minutes,
          episode_air_date, episode_overview, episode_still_url,
          metadata_source, metadata_provider, metadata_fetched_at,
          metadata_status, metadata_confidence, metadata_pick_source, metadata_reasoning,
          parse_raw_name, parse_resolution, parse_source, parse_release_group,
          first_seen_at, last_updated_at
        ) VALUES (
          ?, ?, ?, ?,
          ?, ?, ?, ?, ?, ?,
          ?, ?, ?,
          ?, ?, ?,
          ?, ?, ?,
          ?, ?, ?,
          ?, ?, ?,
          ?, ?, ?, ?,
          ?, ?, ?, ?,
          ?, ?
        )
        ON CONFLICT(path) DO UPDATE SET
          inode                = excluded.inode,
          size_bytes           = excluded.size_bytes,
          mtime                = excluded.mtime,
          tmdb_id              = excluded.tmdb_id,
          imdb_id              = excluded.imdb_id,
          media_type           = excluded.media_type,
          title                = excluded.title,
          original_title       = excluded.original_title,
          year                 = excluded.year,
          season_number        = excluded.season_number,
          episode_number       = excluded.episode_number,
          episode_title        = excluded.episode_title,
          poster_url           = excluded.poster_url,
          overview             = excluded.overview,
          vote_average         = excluded.vote_average,
          genres_json          = excluded.genres_json,
          cast_json            = excluded.cast_json,
          runtime_minutes      = excluded.runtime_minutes,
          episode_air_date     = excluded.episode_air_date,
          episode_overview     = excluded.episode_overview,
          episode_still_url    = excluded.episode_still_url,
          metadata_source      = excluded.metadata_source,
          metadata_provider    = excluded.metadata_provider,
          metadata_fetched_at  = excluded.metadata_fetched_at,
          metadata_status      = excluded.metadata_status,
          metadata_confidence  = excluded.metadata_confidence,
          metadata_pick_source = excluded.metadata_pick_source,
          metadata_reasoning   = excluded.metadata_reasoning,
          parse_raw_name       = excluded.parse_raw_name,
          parse_resolution     = excluded.parse_resolution,
          parse_source         = excluded.parse_source,
          parse_release_group  = excluded.parse_release_group,
          last_updated_at      = excluded.last_updated_at
        """,
        (
            path, stat.get("inode"), stat.get("size_bytes"), stat.get("mtime"),
            tmdb_id, imdb_id, media_type, title, original_title, year,
            season_number, episode_number, episode_title,
            poster_url, overview, vote_average,
            json.dumps(genres, ensure_ascii=False), json.dumps(cast, ensure_ascii=False), runtime_minutes,
            episode_air_date, episode_overview, episode_still_url,
            metadata_source, metadata_provider, now,
            status, identify_result.confidence, identify_result.pick_source, identify_result.reasoning,
            parse.raw_name, parse.resolution, parse.source, parse.release_group,
            now, now,
        ),
    )
    conn.commit()


def upsert_details(
    conn: sqlite3.Connection,
    *,
    path: str,
    genres: list[str] | None = None,
    cast: list[str] | None = None,
    runtime_minutes: int | None = None,
    episode_air_date: str | None = None,
    episode_overview: str | None = None,
    episode_still_url: str | None = None,
) -> None:
    """补丁式更新 details（lookup_by_id 拿到的额外字段）。只覆盖非 None 字段。"""
    fields = []
    values: list[Any] = []
    if genres is not None:
        fields.append("genres_json = ?")
        values.append(json.dumps(genres, ensure_ascii=False))
    if cast is not None:
        fields.append("cast_json = ?")
        values.append(json.dumps(cast, ensure_ascii=False))
    if runtime_minutes is not None:
        fields.append("runtime_minutes = ?")
        values.append(runtime_minutes)
    if episode_air_date is not None:
        fields.append("episode_air_date = ?")
        values.append(episode_air_date)
    if episode_overview is not None:
        fields.append("episode_overview = ?")
        values.append(episode_overview)
    if episode_still_url is not None:
        fields.append("episode_still_url = ?")
        values.append(episode_still_url)
    if not fields:
        return
    fields.append("last_updated_at = ?")
    values.append(_now())
    values.append(path)
    conn.execute(
        f"UPDATE media_files SET {', '.join(fields)} WHERE path = ?",
        tuple(values),
    )
    conn.commit()


def _row_to_cached(row: sqlite3.Row) -> CachedMetadata:
    def _json(s: str | None, default: list[Any]) -> list[Any]:
        if not s:
            return default
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return default

    return CachedMetadata(
        path=row["path"],
        inode=row["inode"],
        size_bytes=row["size_bytes"],
        mtime=row["mtime"],
        tmdb_id=row["tmdb_id"],
        imdb_id=row["imdb_id"],
        media_type=row["media_type"],
        title=row["title"],
        original_title=row["original_title"],
        year=row["year"],
        season_number=row["season_number"],
        episode_number=row["episode_number"],
        episode_title=row["episode_title"],
        poster_url=row["poster_url"],
        overview=row["overview"],
        vote_average=row["vote_average"],
        genres=_json(row["genres_json"], []),
        cast=_json(row["cast_json"], []),
        runtime_minutes=row["runtime_minutes"],
        episode_air_date=row["episode_air_date"],
        episode_overview=row["episode_overview"],
        episode_still_url=row["episode_still_url"],
        metadata_source=row["metadata_source"],
        metadata_provider=row["metadata_provider"],
        metadata_fetched_at=row["metadata_fetched_at"],
        metadata_status=row["metadata_status"],
        metadata_confidence=row["metadata_confidence"],
        metadata_pick_source=row["metadata_pick_source"],
        metadata_reasoning=row["metadata_reasoning"],
        parse_raw_name=row["parse_raw_name"],
        parse_resolution=row["parse_resolution"],
        parse_source=row["parse_source"],
        parse_release_group=row["parse_release_group"],
        first_seen_at=row["first_seen_at"],
        last_updated_at=row["last_updated_at"],
    )


def get_by_path(
    conn: sqlite3.Connection,
    path: str,
    *,
    current_mtime: int | None = None,
    current_inode: int | None = None,
) -> tuple[CachedMetadata | None, str]:
    """读 cache。返回 (cached, status)。

    status 取值：
      'hit'    — 找到 + mtime/inode 一致
      'miss'   — DB 无该 path
      'stale'  — DB 有但 mtime/inode 跟 current 不一致（调用方应触发重识别）

    current_mtime / current_inode 缺省时跳过 stale 检测（直接返 hit / miss）。
    """
    row = conn.execute(
        "SELECT * FROM media_files WHERE path = ?", (path,)
    ).fetchone()
    if row is None:
        return None, "miss"
    cached = _row_to_cached(row)
    if current_mtime is not None and cached.mtime is not None and cached.mtime != current_mtime:
        return cached, "stale"
    if current_inode is not None and cached.inode is not None and cached.inode != current_inode:
        return cached, "stale"
    return cached, "hit"


def delete_by_path(conn: sqlite3.Connection, path: str) -> bool:
    """删除某条 cache（用户强制重识别 / 文件已删除）。返回是否删了行。"""
    cur = conn.execute("DELETE FROM media_files WHERE path = ?", (path,))
    conn.commit()
    return cur.rowcount > 0


# ─── 库视图查询 ───

_SORT_SQL = {
    "added_desc":  "ORDER BY first_seen_at DESC, id DESC",
    "year_desc":   "ORDER BY year DESC NULLS LAST, title COLLATE NOCASE",
    "year_asc":    "ORDER BY year ASC NULLS LAST, title COLLATE NOCASE",
    "vote_desc":   "ORDER BY vote_average DESC NULLS LAST, year DESC NULLS LAST",
    "title_asc":   "ORDER BY title COLLATE NOCASE ASC",
}


def query_library(
    conn: sqlite3.Connection,
    *,
    media_type: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    query: str | None = None,
    sort: str = "added_desc",
    limit: int = 200,
    offset: int = 0,
    include_extras: bool = False,
) -> tuple[list[CachedMetadata], int]:
    """库视图主查询。返回 (items, total_count_under_filter)。

    只看 metadata_status='ok' 的行（needs_review 不展示——那是后台 worker
    自动尝试失败的，不应出现在用户的"我的库"视图里）。

    extras / featurette / trailer 等附属文件默认不展示（include_extras=False），
    它们和 main feature 同目录，库视图按 main feature 聚合更符合用户心智。
    """
    where = ["metadata_status = 'ok'"]
    params: list[Any] = []
    if media_type:
        where.append("media_type = ?")
        params.append(media_type)
    elif not include_extras:
        where.append("media_type IN ('movie', 'tv')")
    if year_from is not None:
        where.append("year >= ?")
        params.append(year_from)
    if year_to is not None:
        where.append("year <= ?")
        params.append(year_to)
    if query:
        # 模糊匹配：title / original_title / parse_raw_name 任一命中
        where.append(
            "(title LIKE ? OR original_title LIKE ? OR parse_raw_name LIKE ?)"
        )
        like = f"%{query}%"
        params.extend([like, like, like])
    where_sql = " AND ".join(where)

    sort_sql = _SORT_SQL.get(sort, _SORT_SQL["added_desc"])

    total = conn.execute(
        f"SELECT COUNT(*) FROM media_files WHERE {where_sql}", params
    ).fetchone()[0]

    rows = conn.execute(
        f"SELECT * FROM media_files WHERE {where_sql} {sort_sql} LIMIT ? OFFSET ?",
        (*params, limit, offset),
    ).fetchall()
    return [_row_to_cached(r) for r in rows], total


def list_companions_in_dir(
    conn: sqlite3.Connection, dir_path: str
) -> list[CachedMetadata]:
    """列出同目录的附属文件（extra 花絮 + part 多盘分段，按 path prefix 匹配）。

    dir_path 不要带尾部斜杠。匹配 path LIKE 'dir/%' AND path NOT LIKE 'dir/%/%'
    保证只取该目录的直接子项（不递归子目录）。
    """
    if not dir_path or dir_path == "/":
        return []
    prefix = dir_path.rstrip("/") + "/"
    rows = conn.execute(
        """
        SELECT * FROM media_files
         WHERE media_type IN ('extra', 'part')
           AND path LIKE ?
           AND path NOT LIKE ?
         ORDER BY media_type, parse_raw_name
        """,
        (f"{prefix}%", f"{prefix}%/%"),
    ).fetchall()
    return [_row_to_cached(r) for r in rows]


# 保留旧名作为 alias 避免单测 / 调用方破坏
list_extras_in_dir = list_companions_in_dir


def get_library_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    """库总览统计：总数 / media_type / 年代分布 / genre top10 / vote 直方图。"""
    total = conn.execute(
        "SELECT COUNT(*) FROM media_files WHERE metadata_status = 'ok'"
    ).fetchone()[0]

    by_type = dict(conn.execute(
        """
        SELECT media_type, COUNT(*) FROM media_files
         WHERE metadata_status = 'ok' AND media_type IS NOT NULL
         GROUP BY media_type
        """
    ).fetchall())

    # 年代：1970s / 1980s / ...，用 (year/10)*10 算 decade
    by_decade = dict(conn.execute(
        """
        SELECT (year / 10) * 10 AS decade, COUNT(*)
          FROM media_files
         WHERE metadata_status = 'ok' AND year IS NOT NULL
         GROUP BY decade ORDER BY decade
        """
    ).fetchall())

    # vote 分桶：[0-6) / [6-7) / [7-8) / [8-9) / [9-10]
    vote_buckets: dict[str, int] = {"<6": 0, "6-7": 0, "7-8": 0, "8-9": 0, "9-10": 0, "unrated": 0}
    for row in conn.execute(
        "SELECT vote_average FROM media_files WHERE metadata_status = 'ok'"
    ):
        v = row[0]
        if v is None:
            vote_buckets["unrated"] += 1
        elif v < 6:
            vote_buckets["<6"] += 1
        elif v < 7:
            vote_buckets["6-7"] += 1
        elif v < 8:
            vote_buckets["7-8"] += 1
        elif v < 9:
            vote_buckets["8-9"] += 1
        else:
            vote_buckets["9-10"] += 1

    # genre top10：genres_json 是 JSON array，Python 侧 unroll
    genre_counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT genres_json FROM media_files WHERE metadata_status = 'ok' AND genres_json IS NOT NULL"
    ):
        try:
            for g in json.loads(row[0]):
                if g:
                    genre_counts[g] = genre_counts.get(g, 0) + 1
        except (json.JSONDecodeError, TypeError):
            continue
    top_genres = sorted(genre_counts.items(), key=lambda x: x[1], reverse=True)[:10]

    return {
        "total": total,
        "by_media_type": by_type,
        "by_decade": by_decade,
        "by_vote_bucket": vote_buckets,
        "top_genres": [{"name": g, "count": c} for g, c in top_genres],
    }
