"""Watch-source synchronization: Emby (and future Plex/Jellyfin/Trakt).

Single-flight enforcement: at most one watch_sync_runs row may have
status='running' per provider (partial unique index `uniq_watch_sync_running`
in db/migrations.py). Sync goroutines acquire the slot via
BEGIN IMMEDIATE + INSERT INTO watch_sync_runs; on UNIQUE violation we raise
ConcurrentSyncError. Background _sync_worker thread runs the actual pull,
then issues a single terminal UPDATE.

Pattern B enforcement (writer side):
  - Movie WatchedItem rows: only tmdb_movie_id may be non-null; season/
    episode/series_id/episode_id MUST be null.
  - TV WatchedItem rows: only tmdb_series_id / tmdb_episode_id may be
    non-null; tmdb_movie_id MUST be null; season+episode_number required.
  - Insert/upsert respects the CHECK constraints added in 3.0 migration.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class ConcurrentSyncError(RuntimeError):
    """Raised when a sync for this provider is already running."""


@dataclass(frozen=True)
class WatchedItem:
    """Internal watch-source-agnostic shape, ready for INSERT INTO watched_items."""

    provider: str                         # 'emby'
    provider_item_id: str                 # Emby Id
    media_type: str                       # 'movie' | 'tv'
    tmdb_movie_id: str | None
    tmdb_series_id: str | None
    tmdb_episode_id: str | None
    imdb_id: str | None
    season_number: int | None
    episode_number: int | None
    title: str | None
    year: int | None
    watched_at: int                       # unix ts
    raw_hash: str
    mapping_status: str                   # 'mapped' | 'fallback_se' | 'unmapped' | 'ambiguous'
    mapping_confidence: float
    mapping_source: str


@dataclass
class SyncSummary:
    """Returned from the worker thread; mirrored to watch_sync_runs DB row."""

    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    skipped: int = 0


# ─── Mapping ──────────────────────────────────────────────────────


def map_emby_item_to_watched(
    item: Any,
    series_tmdb_cache: dict[str, str | None],
) -> WatchedItem:
    """Project an EmbyItem onto our WatchedItem shape.

    For Episodes: ProviderIds.Tmdb is the EPISODE tmdb id (mapped via TMDB's
    /tv/{id}/season/{s}/episode/{e}/external_ids), NOT the series id. The
    series tmdb id is looked up separately from item.series_id via the
    series_tmdb_cache.

    Pattern B enforcement: the output respects movie/tv mutex on id fields.
    """
    raw_hash_seed = f"{item.id}|{item.last_played_at or 0}"
    raw_hash = hashlib.sha256(raw_hash_seed.encode("utf-8")).hexdigest()
    watched_at = item.last_played_at or int(time.time())

    if item.type == "Movie":
        movie_tmdb = item.tmdb_id
        if movie_tmdb:
            status, conf, source = "mapped", 1.0, "emby.provider_ids"
        else:
            status, conf, source = "unmapped", 0.0, "unknown"
        return WatchedItem(
            provider="emby",
            provider_item_id=item.id,
            media_type="movie",
            tmdb_movie_id=movie_tmdb,
            tmdb_series_id=None,
            tmdb_episode_id=None,
            imdb_id=item.imdb_id,
            season_number=None,
            episode_number=None,
            title=item.name,
            year=item.year,
            watched_at=watched_at,
            raw_hash=raw_hash,
            mapping_status=status,
            mapping_confidence=conf,
            mapping_source=source,
        )

    if item.type == "Episode":
        episode_tmdb = item.tmdb_id
        series_tmdb = series_tmdb_cache.get(item.series_id) if item.series_id else None
        has_se = item.season_number is not None and item.episode_number is not None
        if episode_tmdb and has_se:
            status, conf, source = "mapped", 1.0, "emby.provider_ids"
        elif series_tmdb and has_se:
            status, conf, source = "fallback_se", 0.7, "emby.series_provider_ids+se"
        elif has_se:
            status, conf, source = "unmapped", 0.0, "unknown"
        else:
            # CHECK constraint requires season+episode for tv rows.
            # If both missing, we cannot persist this item under the tv schema —
            # caller (sync worker) must drop it. We still construct an item so
            # the worker can log it; persistence will be filtered out.
            status, conf, source = "unmapped", 0.0, "unknown"
        return WatchedItem(
            provider="emby",
            provider_item_id=item.id,
            media_type="tv",
            tmdb_movie_id=None,
            tmdb_series_id=series_tmdb,
            tmdb_episode_id=episode_tmdb,
            imdb_id=item.imdb_id,
            season_number=item.season_number,
            episode_number=item.episode_number,
            title=item.name,
            year=item.year,
            watched_at=watched_at,
            raw_hash=raw_hash,
            mapping_status=status,
            mapping_confidence=conf,
            mapping_source=source,
        )

    raise ValueError(f"unknown Emby item type {item.type!r}")


# ─── Persistence helpers ──────────────────────────────────────────


def _upsert_watched_item(conn: sqlite3.Connection, w: WatchedItem) -> str:
    """INSERT or UPDATE by (provider, provider_item_id) unique key.

    Returns 'inserted' | 'updated' | 'skipped'.
    """
    # Schema CHECK constraints will reject TV rows with NULL season/episode.
    # Skip such rows (mapping_status='unmapped' + no s/e) — log so user knows.
    if w.media_type == "tv" and (w.season_number is None or w.episode_number is None):
        logger.warning(
            "emby: skipping episode without season/episode numbers: %s (%s)",
            w.provider_item_id, w.title,
        )
        return "skipped"

    fetched_at = int(time.time())
    existing = conn.execute(
        "SELECT id, watched_at, raw_hash FROM watched_items "
        "WHERE provider=? AND provider_item_id=?",
        (w.provider, w.provider_item_id),
    ).fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO watched_items(
              provider, provider_item_id, media_type,
              tmdb_movie_id, tmdb_series_id, tmdb_episode_id, imdb_id,
              season_number, episode_number,
              title, year, watched_at, fetched_at, raw_hash,
              mapping_status, mapping_confidence, mapping_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                w.provider, w.provider_item_id, w.media_type,
                w.tmdb_movie_id, w.tmdb_series_id, w.tmdb_episode_id, w.imdb_id,
                w.season_number, w.episode_number,
                w.title, w.year, w.watched_at, fetched_at, w.raw_hash,
                w.mapping_status, w.mapping_confidence, w.mapping_source,
            ),
        )
        return "inserted"

    # Existing row — only update if raw_hash differs (re-watched / mapping refined)
    if existing["raw_hash"] == w.raw_hash:
        return "skipped"
    conn.execute(
        """
        UPDATE watched_items SET
          watched_at=?, fetched_at=?, raw_hash=?,
          mapping_status=?, mapping_confidence=?, mapping_source=?,
          tmdb_movie_id=?, tmdb_series_id=?, tmdb_episode_id=?,
          title=?, year=?
        WHERE id=?
        """,
        (
            w.watched_at, fetched_at, w.raw_hash,
            w.mapping_status, w.mapping_confidence, w.mapping_source,
            w.tmdb_movie_id, w.tmdb_series_id, w.tmdb_episode_id,
            w.title, w.year,
            existing["id"],
        ),
    )
    return "updated"


# ─── Sync orchestration ───────────────────────────────────────────


def claim_sync_run(conn: sqlite3.Connection, provider: str) -> int:
    """[code-enforced single-flight] Atomically open a run slot.

    Uses BEGIN IMMEDIATE so the INSERT competes for the write lock;
    raises ConcurrentSyncError if uniq_watch_sync_running rejects (already
    a 'running' row for this provider). Catches OperationalError for
    db-locked races too.
    """
    now = int(time.time())
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "INSERT INTO watch_sync_runs(provider, started_at, status) "
            "VALUES (?, ?, 'running')",
            (provider, now),
        )
        run_id = cur.lastrowid
        conn.commit()
        return run_id
    except sqlite3.IntegrityError as e:
        try:
            conn.rollback()
        except Exception:
            pass
        raise ConcurrentSyncError(f"{provider} sync already running") from e
    except sqlite3.OperationalError as e:
        try:
            conn.rollback()
        except Exception:
            pass
        if "locked" in str(e).lower():
            raise ConcurrentSyncError(f"db locked while claiming {provider} run") from e
        raise


def finalize_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    summary: SyncSummary | None = None,
    error: str | None = None,
) -> None:
    """Single terminal UPDATE: status + counters + completed_at."""
    if status not in ("done", "failed", "aborted"):
        raise ValueError(f"invalid terminal status {status!r}")
    s = summary or SyncSummary()
    conn.execute(
        """
        UPDATE watch_sync_runs
           SET status=?, completed_at=?, error=?,
               items_fetched=?, items_inserted=?, items_updated=?, items_skipped=?
         WHERE id=?
        """,
        (status, int(time.time()), error,
         s.fetched, s.inserted, s.updated, s.skipped, run_id),
    )
    conn.commit()


def sync_emby(
    open_conn,                          # callable () -> sqlite3.Connection
    emby_client,                        # clients.watch.emby.EmbyClient
    *,
    since_iso: str | None = None,
    run_in_thread: bool = True,
) -> int:
    """Top-level entrypoint. Returns run_id; caller polls watch_sync_runs.

    open_conn: callable that returns a fresh sqlite3 connection. Because the
    worker runs in a background thread we cannot share Flask's request-scoped
    connection — must mint a new one.

    run_in_thread=False is for unit tests that want synchronous execution.
    """
    # 1) Claim the slot in the calling thread (so failure surfaces immediately)
    claim_conn = open_conn()
    try:
        run_id = claim_sync_run(claim_conn, "emby")
    finally:
        claim_conn.close()

    def _worker() -> None:
        conn = open_conn()
        summary = SyncSummary()
        try:
            items = emby_client.list_watched(since_iso=since_iso)
            summary.fetched = len(items)

            # Pre-fetch series tmdb cache to avoid N+1 episode lookups
            series_ids = list({it.series_id for it in items if it.series_id})
            series_cache = emby_client.get_series_tmdb_ids(series_ids) if series_ids else {}

            for it in items:
                w = map_emby_item_to_watched(it, series_cache)
                result = _upsert_watched_item(conn, w)
                if result == "inserted":
                    summary.inserted += 1
                elif result == "updated":
                    summary.updated += 1
                else:
                    summary.skipped += 1
            conn.commit()
            finalize_run(conn, run_id, status="done", summary=summary)
        except Exception as e:  # noqa: BLE001
            logger.exception("emby sync failed")
            try:
                conn.rollback()
            except Exception:
                pass
            try:
                finalize_run(conn, run_id, status="failed", summary=summary, error=str(e))
            except Exception:
                logger.exception("failed to mark sync_run as failed")
        finally:
            conn.close()

    if run_in_thread:
        threading.Thread(target=_worker, daemon=True, name=f"emby-sync-{run_id}").start()
    else:
        _worker()
    return run_id


def get_sync_status(conn: sqlite3.Connection, run_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT id, provider, status, started_at, completed_at,
               items_fetched, items_inserted, items_updated, items_skipped, error
          FROM watch_sync_runs WHERE id=?
        """,
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def reap_stuck_sync_runs(conn: sqlite3.Connection, *, timeout_secs: int = 600) -> int:
    """Cron-driven recovery: mark long-running rows as aborted.

    Mirrors the destructive_actions reaper. Without this, a crashed sync
    worker would forever hold the partial unique index slot.
    """
    cutoff = int(time.time()) - timeout_secs
    cur = conn.execute(
        """
        UPDATE watch_sync_runs
           SET status='aborted',
               completed_at=?,
               error=COALESCE(error, 'reaped: stuck running past timeout')
         WHERE status='running' AND started_at < ?
        """,
        (int(time.time()), cutoff),
    )
    conn.commit()
    return cur.rowcount
