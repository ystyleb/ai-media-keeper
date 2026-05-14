"""Dedup engine: find duplicate media releases and recommend which to keep.

Pipeline:
  1. Find group keys via GROUP BY tmdb_movie_id (or tmdb_series_id+s+e)
     HAVING COUNT(*) >= 2 — bulk SQL, paginated.
  2. Bulk fetch all candidates' media_files rows in one IN-clause SELECT.
  3. Bulk fetch HDR sub-table for those file ids.
  4. Bulk fetch watched_items rows joined by movie_id / episode_id / series+s+e.
  5. In-memory assemble groups; recompute quality_score by current weights_hash
     when DB-cached score_weights_hash is stale.

Pattern B (cross-table join soundness):
  - Movie branch and TV branch are separate code paths — no mixed id spaces.
  - All joins explicitly check media_type + double-sided NOT NULL.

Pattern A (truth source for quality_score):
  - dedup_weights table + dedup_weights_meta.current_hash is THE truth.
  - media_files.quality_score is a derived cache; API always recomputes from
    DB-readback weights when stale, so a stale cache cannot mislead the UI.

LLM reasoning ("why this one?") is deferred to a later phase; this module
exposes score_breakdown so the default UX shows raw signals without LLM.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# Default weights mirror db/migrations.DEFAULT_DEDUP_WEIGHTS at seed time.
# Runtime always reads from dedup_weights table (UI may edit) — these are only
# a fallback when the table is somehow empty (should not happen after migration).
_FALLBACK_WEIGHTS: dict[str, float] = {
    "resolution.4K": 40, "resolution.2160p": 40,
    "resolution.1080p": 25,
    "resolution.720p": 10,
    "resolution.480p": 2,
    "hdr.DolbyVision": 25, "hdr.HDR10+": 20, "hdr.HDR10": 10, "hdr.HLG": 5,
    "source.UltraHDBluRay": 18, "source.BluRay": 15,
    "source.WEB-DL": 10, "source.WEBRip": 6, "source.HDTV": 3,
    "source.DVDRip": 1,                  # r2 BLOCKER fix: normalizer maps DVD/DVDRip here
    "codec.AV1": 18, "codec.H.265": 15, "codec.H.264": 5,
    "color_depth.10-bit": 5,
}


def _canonical_weights_hash(weights: dict[str, float]) -> str:
    """Canonical hash; mirrors db/migrations._canonical_weights_hash.

    Numbers normalized to float() so int literals match REAL readback.
    """
    canonical = json.dumps(
        {k: float(weights[k]) for k in sorted(weights.keys())},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _now() -> int:
    return int(time.time())


# ─── DTOs ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DedupCandidate:
    media_file_id: int          # stable id for LLM grounding (not path)
    path: str
    inode: int | None
    size_bytes: int | None
    mtime: int | None
    parse_resolution: str | None
    parse_source: str | None
    parse_codec: str | None
    hdr_profiles: list[str]     # canonical sorted
    parse_release_group: str | None
    quality_score: float
    score_breakdown: dict[str, float]   # {'resolution.4K': 40, ...}
    is_watched: bool
    keep_recommended: bool      # set after group-level ranking


@dataclass(frozen=True)
class DedupGroup:
    group_key: str              # 'movie:238' | 'tv:1399:s1e1' — stable UI id
    media_type: str             # 'movie' | 'tv'
    tmdb_movie_id: str | None
    tmdb_series_id: str | None
    season_number: int | None
    episode_number: int | None
    title: str | None
    year: int | None
    poster_url: str | None
    candidates: list[DedupCandidate]
    total_size_bytes: int
    deletable_size_bytes: int   # = total - max(quality_score 那份)


# ─── Quality score ────────────────────────────────────────────────


def get_current_weights(conn: sqlite3.Connection) -> dict[str, float]:
    """Read current weights from dedup_weights table (DB is truth)."""
    rows = conn.execute("SELECT key, weight FROM dedup_weights").fetchall()
    if not rows:
        # Should not happen post-migration; safety fallback so dedup never breaks.
        return dict(_FALLBACK_WEIGHTS)
    return {row[0]: float(row[1]) for row in rows}


def get_current_hash(conn: sqlite3.Connection) -> str:
    """Read current_hash from dedup_weights_meta (single source of truth)."""
    row = conn.execute(
        "SELECT current_hash FROM dedup_weights_meta WHERE id=1"
    ).fetchone()
    if row is None:
        # Self-heal: migrations should have written this; recompute from current weights.
        return _canonical_weights_hash(get_current_weights(conn))
    return row[0]


def compute_quality_score(
    media_row: dict[str, Any],
    hdr_profiles: list[str],
    weights: dict[str, float],
) -> tuple[float, dict[str, float]]:
    """Score a single media row 0-100 by current weights.

    Returns (score, breakdown). breakdown maps weight_key → contribution
    for UI tooltip "为什么这份得 X 分"。

    Each contributor is 0 by default and added if the row's parse_* field
    matches a weight key. Multiple HDR profiles each add their own weight.
    """
    breakdown: dict[str, float] = {}
    total = 0.0

    # resolution: parse_resolution exact-match against 'resolution.<value>' keys
    res = media_row.get("parse_resolution")
    if res:
        # weights might have 'resolution.4K' AND 'resolution.2160p' equivalent;
        # exact key match either is fine, we don't dedupe these — both keys map
        # to the same upgrade tier in practice.
        key = f"resolution.{res}"
        w = weights.get(key, 0.0)
        if w:
            breakdown[key] = w
            total += w

    # HDR: each profile contributes independently
    for profile in hdr_profiles:
        key = f"hdr.{profile}"
        w = weights.get(key, 0.0)
        if w:
            breakdown[key] = w
            total += w

    # source: normalize guessit values to weight keys
    src = media_row.get("parse_source")
    if src:
        # guessit returns "Ultra HD Blu-ray" / "Blu-ray" — normalize to keys
        src_key = _normalize_source_key(src)
        if src_key:
            key = f"source.{src_key}"
            w = weights.get(key, 0.0)
            if w:
                breakdown[key] = w
                total += w

    # codec: parse_codec is already normalized (H.265 / H.264 / AV1) in 3.1
    codec = media_row.get("parse_codec")
    if codec:
        key = f"codec.{codec}"
        w = weights.get(key, 0.0)
        if w:
            breakdown[key] = w
            total += w

    # color depth
    depth = media_row.get("parse_color_depth")
    if depth:
        key = f"color_depth.{depth}"
        w = weights.get(key, 0.0)
        if w:
            breakdown[key] = w
            total += w

    # Clamp to 0-100 (weights are designed to total <= ~110 max, but UI expects 0-100)
    total = max(0.0, min(100.0, total))
    return total, breakdown


def _normalize_source_key(src: str) -> str | None:
    """Normalize guessit source string + PT release variants to a weight key suffix.

    Coverage (review I2 fix — PT-naming common variants):
      'Blu-ray' / 'BluRay' / 'BDRip' / 'BDRemux' / 'Remux'   → BluRay
      'Ultra HD Blu-ray'                                     → UltraHDBluRay
      'Web' / 'WEB-DL' / 'WEBRip'                            → WEB-DL / WEBRip
      'HDTV' / 'DVDRip' / 'DVD'                              → HDTV / DVDRip
    """
    s = src.lower().replace("-", "").replace(" ", "")
    if "ultrahdbluray" in s or "uhdbluray" in s:
        return "UltraHDBluRay"
    if "bluray" in s or "bdrip" in s or "bdremux" in s:
        return "BluRay"
    # 'Remux' alone (no Blu-ray prefix in guessit output) typically means BD Remux
    if "remux" in s:
        return "BluRay"
    if "webdl" in s or s == "web":
        return "WEB-DL"
    if "webrip" in s:
        return "WEBRip"
    if "hdtv" in s:
        return "HDTV"
    if "dvdrip" in s or s == "dvd":
        return "DVDRip"
    return None


# ─── Group discovery ──────────────────────────────────────────────


def find_duplicate_groups(
    conn: sqlite3.Connection,
    *,
    media_type: str | None = None,
    watched_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[DedupGroup], int]:
    """Find paginated duplicate groups (movie + tv branches).

    [code-enforced bulk fetch] Total SQL queries are constant (4-8) regardless
    of group count — no N+1.

    Pagination semantics (self-review BLOCKER fix):
      - When media_type is 'movie' or 'tv', limit/offset apply to that branch
        directly via SQL LIMIT/OFFSET (efficient, scales to large libraries).
      - When media_type is None (both branches), we fetch ALL keys for each
        branch then merge + sort + slice in memory. This guarantees the API
        contract that `len(groups) <= limit` and offset advances linearly
        across the merged set. Trade-off: large libraries with thousands of
        dup groups will pay an O(N) memory cost; current MVP scale is fine.
        If the library grows beyond ~10k dup groups, switch to per-branch
        pagination with an explicit `media_type` filter.

    Returns (groups, total_groups_under_filter).
    """
    weights = get_current_weights(conn)

    if media_type == "movie":
        movie_keys, total = _find_movie_group_keys(
            conn, watched_only=watched_only, limit=limit, offset=offset,
        )
        return _build_groups_for_movies(conn, movie_keys, weights), total

    if media_type == "tv":
        tv_keys, total = _find_tv_group_keys(
            conn, watched_only=watched_only, limit=limit, offset=offset,
        )
        return _build_groups_for_tv(conn, tv_keys, weights), total

    # media_type=None: merge both branches with combined limit/offset
    movie_keys, movie_total = _find_movie_group_keys(
        conn, watched_only=watched_only, limit=10_000, offset=0,
    )
    tv_keys, tv_total = _find_tv_group_keys(
        conn, watched_only=watched_only, limit=10_000, offset=0,
    )
    movie_groups = _build_groups_for_movies(conn, movie_keys, weights)
    tv_groups = _build_groups_for_tv(conn, tv_keys, weights)
    # Sort merged by deletable bytes desc (ROI) then group_key for stable order
    merged = sorted(
        movie_groups + tv_groups,
        key=lambda g: (-g.deletable_size_bytes, g.group_key),
    )
    total = movie_total + tv_total
    return merged[offset:offset + limit], total


def _find_movie_group_keys(
    conn: sqlite3.Connection,
    *,
    watched_only: bool,
    limit: int,
    offset: int,
) -> tuple[list[str], int]:
    """Return (paginated movie group keys, total count)."""
    # Pattern B: movie branch only touches movie rows + tmdb_movie_id IS NOT NULL.
    if watched_only:
        # CTE: pre-filter watched movie_ids at group-SQL layer (codex r2 IMPORTANT)
        base_sql = """
        WITH watched_movie_ids AS (
          SELECT DISTINCT tmdb_movie_id FROM watched_items
           WHERE media_type='movie' AND tmdb_movie_id IS NOT NULL
        )
        SELECT tmdb_movie_id FROM media_files
         WHERE metadata_status='ok' AND media_type='movie' AND tmdb_movie_id IS NOT NULL
           AND tmdb_movie_id IN (SELECT tmdb_movie_id FROM watched_movie_ids)
         GROUP BY tmdb_movie_id
        HAVING COUNT(*) >= 2
        """
    else:
        base_sql = """
        SELECT tmdb_movie_id FROM media_files
         WHERE metadata_status='ok' AND media_type='movie' AND tmdb_movie_id IS NOT NULL
         GROUP BY tmdb_movie_id
        HAVING COUNT(*) >= 2
        """

    total_sql = f"SELECT COUNT(*) FROM ({base_sql})"
    total = conn.execute(total_sql).fetchone()[0]

    paged_sql = base_sql + " ORDER BY COUNT(*) DESC, tmdb_movie_id LIMIT ? OFFSET ?"
    rows = conn.execute(paged_sql, (limit, offset)).fetchall()
    return [row[0] for row in rows], total


def _find_tv_group_keys(
    conn: sqlite3.Connection,
    *,
    watched_only: bool,
    limit: int,
    offset: int,
) -> tuple[list[tuple[str, int, int]], int]:
    """Return (paginated (series_id, season, episode) tuples, total count)."""
    if watched_only:
        base_sql = """
        WITH watched_episode_keys AS (
          SELECT DISTINCT tmdb_series_id, season_number, episode_number FROM watched_items
           WHERE media_type='tv' AND tmdb_series_id IS NOT NULL
             AND season_number IS NOT NULL AND episode_number IS NOT NULL
        )
        SELECT tmdb_series_id, season_number, episode_number FROM media_files
         WHERE metadata_status='ok' AND media_type='tv'
           AND tmdb_series_id IS NOT NULL
           AND season_number IS NOT NULL AND episode_number IS NOT NULL
           AND (tmdb_series_id, season_number, episode_number) IN
               (SELECT tmdb_series_id, season_number, episode_number FROM watched_episode_keys)
         GROUP BY tmdb_series_id, season_number, episode_number
        HAVING COUNT(*) >= 2
        """
    else:
        base_sql = """
        SELECT tmdb_series_id, season_number, episode_number FROM media_files
         WHERE metadata_status='ok' AND media_type='tv'
           AND tmdb_series_id IS NOT NULL
           AND season_number IS NOT NULL AND episode_number IS NOT NULL
         GROUP BY tmdb_series_id, season_number, episode_number
        HAVING COUNT(*) >= 2
        """

    total_sql = f"SELECT COUNT(*) FROM ({base_sql})"
    total = conn.execute(total_sql).fetchone()[0]

    paged_sql = (
        base_sql
        + " ORDER BY COUNT(*) DESC, tmdb_series_id, season_number, episode_number"
        + " LIMIT ? OFFSET ?"
    )
    rows = conn.execute(paged_sql, (limit, offset)).fetchall()
    return [(row[0], row[1], row[2]) for row in rows], total


def _build_groups_for_movies(
    conn: sqlite3.Connection,
    movie_ids: list[str],
    weights: dict[str, float],
) -> list[DedupGroup]:
    if not movie_ids:
        return []
    placeholders = ",".join("?" for _ in movie_ids)
    rows = conn.execute(
        f"""
        SELECT id, path, inode, size_bytes, mtime,
               tmdb_movie_id, title, year, poster_url, parse_resolution,
               parse_source, parse_codec, parse_color_depth, parse_release_group,
               quality_score, score_weights_hash
          FROM media_files
         WHERE media_type='movie' AND tmdb_movie_id IN ({placeholders})
        """,
        tuple(movie_ids),
    ).fetchall()
    if not rows:
        return []

    file_ids = [r["id"] for r in rows]
    hdr_by_file = _bulk_fetch_hdr_profiles(conn, file_ids)
    watched_movies = _bulk_fetch_watched_movie_ids(conn, movie_ids)

    # Group by tmdb_movie_id
    by_movie: dict[str, list] = {mid: [] for mid in movie_ids}
    for r in rows:
        by_movie[r["tmdb_movie_id"]].append(r)

    groups: list[DedupGroup] = []
    for mid in movie_ids:
        cands = _build_candidates(
            rows=by_movie[mid],
            hdr_by_file=hdr_by_file,
            weights=weights,
            is_watched=mid in watched_movies,
        )
        if len(cands) < 2:                              # safety: skip if HAVING race
            continue
        first = by_movie[mid][0]
        groups.append(_make_group(
            group_key=f"movie:{mid}",
            media_type="movie",
            tmdb_movie_id=mid, tmdb_series_id=None,
            season_number=None, episode_number=None,
            title=first["title"], year=first["year"],
            poster_url=first["poster_url"],
            candidates=cands,
        ))
    return groups


def _build_groups_for_tv(
    conn: sqlite3.Connection,
    tv_keys: list[tuple[str, int, int]],
    weights: dict[str, float],
) -> list[DedupGroup]:
    if not tv_keys:
        return []
    # Bulk fetch all rows for these series — narrow by series_id IN list,
    # then in-memory bucket by (series, season, episode).
    series_ids = list({k[0] for k in tv_keys})
    placeholders = ",".join("?" for _ in series_ids)
    rows = conn.execute(
        f"""
        SELECT id, path, inode, size_bytes, mtime,
               tmdb_series_id, tmdb_episode_id, season_number, episode_number,
               title, year, poster_url, parse_resolution,
               parse_source, parse_codec, parse_color_depth, parse_release_group,
               quality_score, score_weights_hash
          FROM media_files
         WHERE media_type='tv' AND tmdb_series_id IN ({placeholders})
        """,
        tuple(series_ids),
    ).fetchall()
    if not rows:
        return []

    file_ids = [r["id"] for r in rows]
    hdr_by_file = _bulk_fetch_hdr_profiles(conn, file_ids)
    # watched_only check uses set membership of (series, season, episode) AND tmdb_episode_id
    watched_set = _bulk_fetch_watched_tv_keys(conn, tv_keys)
    watched_episode_ids = _bulk_fetch_watched_episode_ids(conn, series_ids)

    # Bucket by (series, season, episode)
    by_key: dict[tuple[str, int, int], list] = {k: [] for k in tv_keys}
    for r in rows:
        k = (r["tmdb_series_id"], r["season_number"], r["episode_number"])
        if k in by_key:
            by_key[k].append(r)

    groups: list[DedupGroup] = []
    for series_id, s, e in tv_keys:
        bucket = by_key[(series_id, s, e)]
        # is_watched: episode_id 优先 (mapped) → series+s+e fallback
        is_watched = False
        for r in bucket:
            ep_id = r["tmdb_episode_id"]
            if ep_id and ep_id in watched_episode_ids:
                is_watched = True
                break
        if not is_watched and (series_id, s, e) in watched_set:
            is_watched = True

        cands = _build_candidates(
            rows=bucket,
            hdr_by_file=hdr_by_file,
            weights=weights,
            is_watched=is_watched,
        )
        if len(cands) < 2:
            continue
        first = bucket[0]
        groups.append(_make_group(
            group_key=f"tv:{series_id}:s{s}e{e}",
            media_type="tv",
            tmdb_movie_id=None, tmdb_series_id=series_id,
            season_number=s, episode_number=e,
            title=first["title"], year=first["year"],
            poster_url=first["poster_url"],
            candidates=cands,
        ))
    return groups


def _bulk_fetch_hdr_profiles(
    conn: sqlite3.Connection, file_ids: list[int]
) -> dict[int, list[str]]:
    if not file_ids:
        return {}
    placeholders = ",".join("?" for _ in file_ids)
    rows = conn.execute(
        f"SELECT media_file_id, profile FROM media_file_hdr_profiles "
        f"WHERE media_file_id IN ({placeholders})",
        tuple(file_ids),
    ).fetchall()
    out: dict[int, list[str]] = {}
    for r in rows:
        out.setdefault(r["media_file_id"], []).append(r["profile"])
    # canonical sorted (matches identify._extract_hdr_profiles output)
    for fid in out:
        out[fid] = sorted(out[fid])
    return out


def _bulk_fetch_watched_movie_ids(
    conn: sqlite3.Connection, movie_ids: list[str]
) -> set[str]:
    """[Pattern B] Movie branch: media_type='movie' + double-sided NOT NULL."""
    if not movie_ids:
        return set()
    placeholders = ",".join("?" for _ in movie_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT tmdb_movie_id FROM watched_items
         WHERE media_type='movie'
           AND tmdb_movie_id IS NOT NULL
           AND tmdb_movie_id IN ({placeholders})
        """,
        tuple(movie_ids),
    ).fetchall()
    return {r[0] for r in rows}


def _bulk_fetch_watched_tv_keys(
    conn: sqlite3.Connection, tv_keys: list[tuple[str, int, int]]
) -> set[tuple[str, int, int]]:
    """[Pattern B] TV series+s+e fallback (mapping_status='fallback_se')."""
    if not tv_keys:
        return set()
    series_ids = list({k[0] for k in tv_keys})
    placeholders = ",".join("?" for _ in series_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT tmdb_series_id, season_number, episode_number FROM watched_items
         WHERE media_type='tv'
           AND tmdb_series_id IS NOT NULL
           AND season_number IS NOT NULL AND episode_number IS NOT NULL
           AND tmdb_series_id IN ({placeholders})
        """,
        tuple(series_ids),
    ).fetchall()
    db_keys = {(r["tmdb_series_id"], r["season_number"], r["episode_number"]) for r in rows}
    return db_keys & set(tv_keys)


def _bulk_fetch_watched_episode_ids(
    conn: sqlite3.Connection, series_ids: list[str]
) -> set[str]:
    """[Pattern B + scope fix B2] TV episode_id strong-signal join scoped to
    only series present in current dedup groups — avoids loading full watched
    table for libraries with 100k+ watched items.
    """
    if not series_ids:
        return set()
    placeholders = ",".join("?" for _ in series_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT tmdb_episode_id FROM watched_items
         WHERE media_type='tv'
           AND tmdb_episode_id IS NOT NULL
           AND tmdb_series_id IS NOT NULL
           AND tmdb_series_id IN ({placeholders})
        """,
        tuple(series_ids),
    ).fetchall()
    return {r[0] for r in rows}


def _build_candidates(
    rows: list,
    hdr_by_file: dict[int, list[str]],
    weights: dict[str, float],
    is_watched: bool,
) -> list[DedupCandidate]:
    """Compute quality_score for each row (server-recompute by current weights).

    Pattern A truth: don't trust media_files.quality_score (may be stale if
    user just edited weights); recompute in memory from current weights.
    """
    cands: list[DedupCandidate] = []
    for r in rows:
        fid = r["id"]
        hdr_profiles = hdr_by_file.get(fid, [])
        row_dict = {
            "parse_resolution": r["parse_resolution"],
            "parse_source": r["parse_source"],
            "parse_codec": r["parse_codec"],
            "parse_color_depth": r["parse_color_depth"],
        }
        score, breakdown = compute_quality_score(row_dict, hdr_profiles, weights)
        cands.append(DedupCandidate(
            media_file_id=fid,
            path=r["path"],
            inode=r["inode"],
            size_bytes=r["size_bytes"],
            mtime=r["mtime"],
            parse_resolution=r["parse_resolution"],
            parse_source=r["parse_source"],
            parse_codec=r["parse_codec"],
            hdr_profiles=hdr_profiles,
            parse_release_group=r["parse_release_group"],
            quality_score=score,
            score_breakdown=breakdown,
            is_watched=is_watched,
            keep_recommended=False,         # set below after group ranking
        ))
    if not cands:
        return cands
    # Mark the highest-score candidate as keep_recommended
    # Ties: keep the largest size (typically better quality at same resolution/codec)
    max_score = max(c.quality_score for c in cands)
    tied = [c for c in cands if c.quality_score == max_score]
    if len(tied) == 1:
        winner = tied[0]
    else:
        winner = max(tied, key=lambda c: c.size_bytes or 0)
    # Replace winner with keep_recommended=True copy.
    # Use dataclasses.replace (safe idiom for frozen dataclass) — review I4 fix.
    # __dict__ unpacking would alias the list[str] hdr_profiles across copies.
    cands = [
        dataclasses.replace(
            c, keep_recommended=(c.media_file_id == winner.media_file_id)
        )
        for c in cands
    ]
    return cands


def _make_group(
    *,
    group_key: str,
    media_type: str,
    tmdb_movie_id: str | None,
    tmdb_series_id: str | None,
    season_number: int | None,
    episode_number: int | None,
    title: str | None,
    year: int | None,
    poster_url: str | None,
    candidates: list[DedupCandidate],
) -> DedupGroup:
    total = sum(c.size_bytes or 0 for c in candidates)
    keep = next((c for c in candidates if c.keep_recommended), None)
    deletable = total - (keep.size_bytes or 0) if keep else 0
    return DedupGroup(
        group_key=group_key,
        media_type=media_type,
        tmdb_movie_id=tmdb_movie_id,
        tmdb_series_id=tmdb_series_id,
        season_number=season_number,
        episode_number=episode_number,
        title=title,
        year=year,
        poster_url=poster_url,
        candidates=candidates,
        total_size_bytes=total,
        deletable_size_bytes=deletable,
    )


# ─── Weights management ───────────────────────────────────────────


class InvalidWeightError(ValueError):
    """Raised when a weight value is non-finite (NaN/inf) or negative.

    Such values would corrupt quality_score (min(100, NaN)==NaN poisons every
    downstream score) and silently break the canonical hash since they don't
    serialize stably.
    """


def _validate_weights(updates: dict[str, float]) -> None:
    """[B1 fix] Reject NaN/Inf/negative — they would corrupt all derived scores."""
    for key, v in updates.items():
        if not isinstance(v, (int, float)):
            raise InvalidWeightError(f"weight {key!r} must be number, got {type(v).__name__}")
        f = float(v)
        if not math.isfinite(f):
            raise InvalidWeightError(f"weight {key!r} must be finite, got {v!r}")
        if f < 0:
            raise InvalidWeightError(f"weight {key!r} must be >= 0, got {v!r}")


def update_weights(
    conn: sqlite3.Connection, updates: dict[str, float]
) -> tuple[str, int]:
    """Atomically merge weight updates and refresh current_hash.

    [code-enforced] Same-transaction UPDATE of dedup_weights + dedup_weights_meta.
    Hash is computed from DB readback (not the input dict) so it stays consistent
    with what compute_quality_score will see at query time.

    Validation (B1 fix): NaN/Inf/negative weights are rejected before any DB write.

    Returns (new_hash, rows_changed).
    """
    if not updates:
        return get_current_hash(conn), 0
    _validate_weights(updates)

    now = _now()
    changed = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        for key, weight in updates.items():
            cur = conn.execute(
                "INSERT INTO dedup_weights(key, weight, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET weight=excluded.weight, updated_at=excluded.updated_at",
                (key, float(weight), now),
            )
            changed += cur.rowcount or 0
        # Read all weights back from DB (truth) and compute canonical hash.
        actual = get_current_weights(conn)
        new_hash = _canonical_weights_hash(actual)
        conn.execute(
            "INSERT INTO dedup_weights_meta(id, current_hash, updated_at) "
            "VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET current_hash=excluded.current_hash, "
            "updated_at=excluded.updated_at",
            (new_hash, now),
        )
        conn.commit()
    except Exception:
        # I3 fix: rollback may itself throw on closed connection; don't let
        # secondary exception mask the original cause.
        try:
            conn.rollback()
        except Exception as rb_err:
            logger.warning("rollback after update_weights failure also failed: %r", rb_err)
        raise
    return new_hash, changed


_REFRESH_BATCH_SIZE = 500


def refresh_all_quality_scores(conn: sqlite3.Connection) -> int:
    """Recompute quality_score for all media_files rows by current weights.

    Returns rows updated. Idempotent — running twice yields zero changes.
    Used by POST /api/dedup/refresh to flush cached scores after bulk weight
    changes; live queries always recompute by current hash anyway, so this is
    a perf optimization, not a correctness step.

    [B3 fix] Batched commits every _REFRESH_BATCH_SIZE rows so a 50k+ library
    doesn't hold a single write-lock for the whole pass (would block scan
    worker / NFO writes for minutes).
    """
    weights = get_current_weights(conn)
    current_hash = get_current_hash(conn)
    now = _now()

    rows = conn.execute(
        """
        SELECT mf.id, mf.parse_resolution, mf.parse_source, mf.parse_codec,
               mf.parse_color_depth, mf.score_weights_hash
          FROM media_files mf
         WHERE mf.metadata_status='ok'
        """
    ).fetchall()

    # Bulk fetch HDR for all rows
    file_ids = [r["id"] for r in rows]
    hdr_by_file = _bulk_fetch_hdr_profiles(conn, file_ids)

    updated = 0
    pending_in_batch = 0

    def commit_batch():
        nonlocal pending_in_batch
        if pending_in_batch:
            conn.commit()
            pending_in_batch = 0

    try:
        conn.execute("BEGIN IMMEDIATE")
        for r in rows:
            if r["score_weights_hash"] == current_hash:
                continue
            score, _ = compute_quality_score(
                {
                    "parse_resolution": r["parse_resolution"],
                    "parse_source": r["parse_source"],
                    "parse_codec": r["parse_codec"],
                    "parse_color_depth": r["parse_color_depth"],
                },
                hdr_by_file.get(r["id"], []),
                weights,
            )
            conn.execute(
                "UPDATE media_files SET quality_score=?, score_weights_hash=?, "
                "score_computed_at=? WHERE id=?",
                (score, current_hash, now, r["id"]),
            )
            updated += 1
            pending_in_batch += 1
            # Yield write lock every batch — gives concurrent scan/identify a chance
            if pending_in_batch >= _REFRESH_BATCH_SIZE:
                commit_batch()
                conn.execute("BEGIN IMMEDIATE")
        commit_batch()
    except Exception:
        try:
            conn.rollback()
        except Exception as rb_err:
            logger.warning("rollback after refresh failure also failed: %r", rb_err)
        raise
    return updated


# ─── Serialization for HTTP API ───────────────────────────────────


def candidate_to_dict(c: DedupCandidate) -> dict:
    return {
        "media_file_id": c.media_file_id,
        "path": c.path,
        "inode": c.inode,
        "size_bytes": c.size_bytes,
        "mtime": c.mtime,
        "resolution": c.parse_resolution,
        "source": c.parse_source,
        "codec": c.parse_codec,
        "hdr_profiles": c.hdr_profiles,
        "release_group": c.parse_release_group,
        "quality_score": c.quality_score,
        "score_breakdown": c.score_breakdown,
        "is_watched": c.is_watched,
        "keep_recommended": c.keep_recommended,
    }


def find_watched_stale_media(
    conn: sqlite3.Connection,
    *,
    days: int = 180,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """Find media files: watched + first_seen_at older than `days`.

    [code-enforced Pattern B] Three explicit UNION branches:
      1. movie: m.media_type='movie' AND m.tmdb_movie_id IS NOT NULL
         watched join: w.media_type='movie' AND w.tmdb_movie_id IS NOT NULL
      2. tv episode_id strong: m.media_type='tv' AND m.tmdb_episode_id IS NOT NULL
         watched join: w.media_type='tv' AND w.tmdb_episode_id IS NOT NULL
      3. tv series+s+e fallback: m.media_type='tv' AND m.tmdb_episode_id IS NULL
         AND m.tmdb_series_id IS NOT NULL AND s/e NOT NULL
         watched join: w.media_type='tv' AND w.tmdb_series_id IS NOT NULL
                       AND s/e match
    Each branch has double-sided NOT NULL on join keys + media_type equality.

    Returns (rows, total). Each row is a dict of media_files columns plus
    the resolved `days_since_first_seen` for UI display.
    """
    cutoff = _now() - days * 86400

    where_movie = (
        "m.first_seen_at < :cutoff "
        "AND m.media_type = 'movie' "
        "AND m.tmdb_movie_id IS NOT NULL "
        "AND EXISTS ("
        "  SELECT 1 FROM watched_items w "
        "   WHERE w.media_type = 'movie' "
        "     AND w.tmdb_movie_id IS NOT NULL "
        "     AND w.tmdb_movie_id = m.tmdb_movie_id"
        ")"
    )
    where_tv_episode_id = (
        "m.first_seen_at < :cutoff "
        "AND m.media_type = 'tv' "
        "AND m.tmdb_episode_id IS NOT NULL "
        "AND EXISTS ("
        "  SELECT 1 FROM watched_items w "
        "   WHERE w.media_type = 'tv' "
        "     AND w.tmdb_episode_id IS NOT NULL "
        "     AND w.tmdb_episode_id = m.tmdb_episode_id"
        ")"
    )
    where_tv_se_fallback = (
        "m.first_seen_at < :cutoff "
        "AND m.media_type = 'tv' "
        "AND m.tmdb_episode_id IS NULL "                # avoid double-match w/ branch 2
        "AND m.tmdb_series_id IS NOT NULL "
        "AND m.season_number IS NOT NULL "
        "AND m.episode_number IS NOT NULL "
        "AND EXISTS ("
        "  SELECT 1 FROM watched_items w "
        "   WHERE w.media_type = 'tv' "
        "     AND w.tmdb_series_id IS NOT NULL "
        "     AND w.tmdb_series_id = m.tmdb_series_id "
        "     AND w.season_number = m.season_number "
        "     AND w.episode_number = m.episode_number"
        ")"
    )
    union_all_where = f"({where_movie}) OR ({where_tv_episode_id}) OR ({where_tv_se_fallback})"

    total = conn.execute(
        f"SELECT COUNT(*) FROM media_files m WHERE {union_all_where}",
        {"cutoff": cutoff},
    ).fetchone()[0]

    rows = conn.execute(
        f"""
        SELECT m.id, m.path, m.media_type, m.title, m.year,
               m.tmdb_movie_id, m.tmdb_series_id, m.tmdb_episode_id,
               m.season_number, m.episode_number,
               m.size_bytes, m.mtime, m.first_seen_at, m.poster_url,
               m.parse_resolution, m.parse_source
          FROM media_files m
         WHERE {union_all_where}
         ORDER BY m.size_bytes DESC, m.id
         LIMIT :limit OFFSET :offset
        """,
        {"cutoff": cutoff, "limit": limit, "offset": offset},
    ).fetchall()

    now = _now()
    return [
        {
            **dict(r),
            "days_since_first_seen": (now - r["first_seen_at"]) // 86400
                if r["first_seen_at"] else None,
        }
        for r in rows
    ], total


def group_to_dict(g: DedupGroup) -> dict:
    return {
        "group_key": g.group_key,
        "media_type": g.media_type,
        "tmdb_movie_id": g.tmdb_movie_id,
        "tmdb_series_id": g.tmdb_series_id,
        "season_number": g.season_number,
        "episode_number": g.episode_number,
        "title": g.title,
        "year": g.year,
        "poster_url": g.poster_url,
        "candidates": [candidate_to_dict(c) for c in g.candidates],
        "total_size_bytes": g.total_size_bytes,
        "deletable_size_bytes": g.deletable_size_bytes,
    }
