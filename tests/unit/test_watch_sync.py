"""Phase 3.4 Emby watch-source + watch_sync tests.

Coverage:
  - EmbyItem parsing: Movie + Episode shapes (incl. ISO datetime fractional seconds)
  - map_emby_item_to_watched: mapped (episode_id), fallback_se (series_id+s+e), unmapped
  - Pattern B writer-side mutex respected by upsert
  - claim_sync_run single-flight via partial unique index
  - finalize_run terminal status update
  - sync_emby end-to-end via fake EmbyClient (no network)
  - reap_stuck_sync_runs marks long-running rows aborted
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from clients.watch.emby import EmbyClient, EmbyItem, _parse_iso_to_ts, _parse_item
from db import migrations
from services import destructive_action, watch_sync

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "t.db"
    c = destructive_action.open_connection(p)
    destructive_action.init_schema(c, SCHEMA_PATH)
    migrations.phase3_migrate(c)
    c.close()
    return p


@pytest.fixture
def conn(db_path):
    c = destructive_action.open_connection(db_path)
    yield c
    c.close()


# ── ISO datetime parsing ───────────────────────────────────────


def test_parse_iso_with_fractional_seconds():
    """Emby returns '2026-05-12T14:30:00.0000000Z' — 7 frac digits exceed Python's 6."""
    ts = _parse_iso_to_ts("2026-05-12T14:30:00.0000000Z")
    assert ts is not None
    # Sanity: should be a positive unix ts
    assert ts > 1_700_000_000


def test_parse_iso_with_tz_offset():
    ts = _parse_iso_to_ts("2026-05-12T14:30:00+00:00")
    assert ts is not None


def test_parse_iso_returns_none_on_bogus():
    assert _parse_iso_to_ts("not a date") is None
    assert _parse_iso_to_ts(None) is None
    assert _parse_iso_to_ts("") is None


# ── EmbyItem parsing ───────────────────────────────────────────


def test_parse_item_movie_with_tmdb_id():
    raw = {
        "Id": "abc-movie-1",
        "Type": "Movie",
        "Name": "The Godfather",
        "ProductionYear": 1972,
        "ProviderIds": {"Tmdb": "238", "Imdb": "tt0068646"},
        "UserData": {"LastPlayedDate": "2026-05-10T20:00:00Z"},
    }
    item = _parse_item(raw)
    assert item.type == "Movie"
    assert item.tmdb_id == "238"
    assert item.imdb_id == "tt0068646"
    assert item.year == 1972
    assert item.series_id is None
    assert item.season_number is None


def test_parse_item_episode_with_tmdb_id_is_episode_id():
    """ProviderIds.Tmdb on Episode is the EPISODE id, not the series id."""
    raw = {
        "Id": "abc-ep-1",
        "Type": "Episode",
        "Name": "Pilot",
        "ProductionYear": 2013,
        "ProviderIds": {"Tmdb": "ep-9999"},  # episode tmdb id
        "SeriesId": "emby-series-1399",
        "ParentIndexNumber": 1,
        "IndexNumber": 1,
        "UserData": {"LastPlayedDate": "2026-05-10T20:00:00Z"},
    }
    item = _parse_item(raw)
    assert item.type == "Episode"
    assert item.tmdb_id == "ep-9999"  # episode tmdb id, NOT series
    assert item.series_id == "emby-series-1399"
    assert item.season_number == 1
    assert item.episode_number == 1


# ── Mapping: movie ──────────────────────────────────────────────


def test_map_movie_with_tmdb_returns_mapped():
    item = EmbyItem(
        id="m1",
        type="Movie",
        name="X",
        year=2020,
        tmdb_id="238",
        imdb_id=None,
        series_id=None,
        season_number=None,
        episode_number=None,
        last_played_at=1700000000,
    )
    w = watch_sync.map_emby_item_to_watched(item, series_tmdb_cache={})
    assert w.media_type == "movie"
    assert w.tmdb_movie_id == "238"
    assert w.tmdb_series_id is None
    assert w.tmdb_episode_id is None
    assert w.season_number is None
    assert w.mapping_status == "mapped"
    assert w.mapping_confidence == 1.0
    assert w.mapping_source == "emby.provider_ids"


def test_map_movie_without_tmdb_returns_unmapped():
    item = EmbyItem(
        id="m2",
        type="Movie",
        name="X",
        year=2020,
        tmdb_id=None,
        imdb_id=None,
        series_id=None,
        season_number=None,
        episode_number=None,
        last_played_at=1700000000,
    )
    w = watch_sync.map_emby_item_to_watched(item, series_tmdb_cache={})
    assert w.mapping_status == "unmapped"
    assert w.tmdb_movie_id is None


# ── Mapping: episode strong signal ──────────────────────────────


def test_map_episode_with_tmdb_episode_id_mapped_strong():
    item = EmbyItem(
        id="e1",
        type="Episode",
        name="Pilot",
        year=2013,
        tmdb_id="ep-9999",  # episode tmdb id
        imdb_id=None,
        series_id="emby-series-1399",
        season_number=1,
        episode_number=1,
        last_played_at=1700000000,
    )
    w = watch_sync.map_emby_item_to_watched(item, series_tmdb_cache={"emby-series-1399": "1399"})
    assert w.media_type == "tv"
    assert w.tmdb_movie_id is None
    assert w.tmdb_series_id == "1399"  # filled from cache
    assert w.tmdb_episode_id == "ep-9999"
    assert w.season_number == 1
    assert w.mapping_status == "mapped"
    assert w.mapping_confidence == 1.0


def test_map_episode_without_episode_id_falls_back_to_series_se():
    """No ProviderIds.Tmdb on Episode but series tmdb id available + s/e known."""
    item = EmbyItem(
        id="e2",
        type="Episode",
        name="Pilot",
        year=2013,
        tmdb_id=None,
        imdb_id=None,
        series_id="emby-series-1399",
        season_number=1,
        episode_number=1,
        last_played_at=1700000000,
    )
    w = watch_sync.map_emby_item_to_watched(item, series_tmdb_cache={"emby-series-1399": "1399"})
    assert w.tmdb_episode_id is None
    assert w.tmdb_series_id == "1399"
    assert w.mapping_status == "fallback_se"
    assert w.mapping_confidence == 0.7
    assert w.mapping_source == "emby.series_provider_ids+se"


def test_map_episode_no_episode_id_no_series_tmdb_unmapped():
    item = EmbyItem(
        id="e3",
        type="Episode",
        name="x",
        year=2020,
        tmdb_id=None,
        imdb_id=None,
        series_id="some-series",
        season_number=1,
        episode_number=1,
        last_played_at=1700000000,
    )
    w = watch_sync.map_emby_item_to_watched(item, series_tmdb_cache={})
    assert w.tmdb_episode_id is None
    assert w.tmdb_series_id is None
    assert w.mapping_status == "unmapped"


# ── Pattern B: writer-side mutex ──────────────────────────────


def test_upsert_movie_inserts_only_movie_id(conn):
    item = EmbyItem(
        id="ins-1",
        type="Movie",
        name="Godfather",
        year=1972,
        tmdb_id="238",
        imdb_id=None,
        series_id=None,
        season_number=None,
        episode_number=None,
        last_played_at=1700000000,
    )
    w = watch_sync.map_emby_item_to_watched(item, series_tmdb_cache={})
    result = watch_sync._upsert_watched_item(conn, w)
    assert result == "inserted"
    row = conn.execute(
        "SELECT media_type, tmdb_movie_id, tmdb_series_id, tmdb_episode_id "
        "FROM watched_items WHERE provider_item_id='ins-1'"
    ).fetchone()
    assert row["media_type"] == "movie"
    assert row["tmdb_movie_id"] == "238"
    assert row["tmdb_series_id"] is None
    assert row["tmdb_episode_id"] is None


def test_upsert_episode_strong_inserts_episode_and_series(conn):
    item = EmbyItem(
        id="ins-ep-1",
        type="Episode",
        name="Pilot",
        year=2013,
        tmdb_id="ep-1",
        imdb_id=None,
        series_id="emby-1399",
        season_number=1,
        episode_number=1,
        last_played_at=1700000000,
    )
    w = watch_sync.map_emby_item_to_watched(item, {"emby-1399": "1399"})
    assert watch_sync._upsert_watched_item(conn, w) == "inserted"
    row = conn.execute(
        "SELECT tmdb_movie_id, tmdb_series_id, tmdb_episode_id, season_number "
        "FROM watched_items WHERE provider_item_id='ins-ep-1'"
    ).fetchone()
    assert row["tmdb_movie_id"] is None
    assert row["tmdb_series_id"] == "1399"
    assert row["tmdb_episode_id"] == "ep-1"
    assert row["season_number"] == 1


def test_upsert_tv_without_se_skipped(conn):
    """TV row with missing season/episode fails CHECK; we should skip not raise."""
    w = watch_sync.WatchedItem(
        provider="emby",
        provider_item_id="bad-ep",
        media_type="tv",
        tmdb_movie_id=None,
        tmdb_series_id=None,
        tmdb_episode_id=None,
        imdb_id=None,
        season_number=None,
        episode_number=None,
        title=None,
        year=None,
        watched_at=1700000000,
        raw_hash="x",
        mapping_status="unmapped",
        mapping_confidence=0.0,
        mapping_source="unknown",
    )
    result = watch_sync._upsert_watched_item(conn, w)
    assert result == "skipped"


def test_upsert_same_row_skips_when_raw_hash_unchanged(conn):
    item = EmbyItem(
        id="dup-1",
        type="Movie",
        name="X",
        year=2020,
        tmdb_id="100",
        imdb_id=None,
        series_id=None,
        season_number=None,
        episode_number=None,
        last_played_at=1700000000,
    )
    w = watch_sync.map_emby_item_to_watched(item, {})
    assert watch_sync._upsert_watched_item(conn, w) == "inserted"
    assert watch_sync._upsert_watched_item(conn, w) == "skipped"


def test_upsert_updates_when_watched_at_changes(conn):
    item1 = EmbyItem(
        id="re-1",
        type="Movie",
        name="X",
        year=2020,
        tmdb_id="100",
        imdb_id=None,
        series_id=None,
        season_number=None,
        episode_number=None,
        last_played_at=1700000000,
    )
    item2 = EmbyItem(
        id="re-1",
        type="Movie",
        name="X",
        year=2020,
        tmdb_id="100",
        imdb_id=None,
        series_id=None,
        season_number=None,
        episode_number=None,
        last_played_at=1800000000,
    )
    w1 = watch_sync.map_emby_item_to_watched(item1, {})
    w2 = watch_sync.map_emby_item_to_watched(item2, {})
    assert watch_sync._upsert_watched_item(conn, w1) == "inserted"
    assert watch_sync._upsert_watched_item(conn, w2) == "updated"
    row = conn.execute(
        "SELECT watched_at FROM watched_items WHERE provider_item_id='re-1'"
    ).fetchone()
    assert row["watched_at"] == 1800000000


# ── single-flight ─────────────────────────────────────────────


def test_claim_sync_run_single_flight_per_provider(conn):
    run1 = watch_sync.claim_sync_run(conn, "emby")
    assert run1 > 0
    with pytest.raises(watch_sync.ConcurrentSyncError):
        watch_sync.claim_sync_run(conn, "emby")


def test_claim_sync_run_after_done_allows_new(conn):
    run1 = watch_sync.claim_sync_run(conn, "emby")
    watch_sync.finalize_run(conn, run1, status="done", summary=watch_sync.SyncSummary())
    run2 = watch_sync.claim_sync_run(conn, "emby")
    assert run2 != run1


def test_claim_sync_run_different_providers_concurrent(conn):
    a = watch_sync.claim_sync_run(conn, "emby")
    b = watch_sync.claim_sync_run(conn, "plex")
    assert a != b


# ── finalize_run ──────────────────────────────────────────────


def test_finalize_run_writes_terminal_status(conn):
    run_id = watch_sync.claim_sync_run(conn, "emby")
    summary = watch_sync.SyncSummary(fetched=10, inserted=8, updated=1, skipped=1)
    watch_sync.finalize_run(conn, run_id, status="done", summary=summary)
    row = conn.execute(
        "SELECT status, items_fetched, items_inserted FROM watch_sync_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    assert row["status"] == "done"
    assert row["items_fetched"] == 10
    assert row["items_inserted"] == 8


def test_finalize_run_failed_carries_error(conn):
    run_id = watch_sync.claim_sync_run(conn, "emby")
    watch_sync.finalize_run(conn, run_id, status="failed", error="emby 500")
    row = conn.execute("SELECT status, error FROM watch_sync_runs WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "emby 500"


def test_finalize_invalid_status_rejected(conn):
    run_id = watch_sync.claim_sync_run(conn, "emby")
    with pytest.raises(ValueError):
        watch_sync.finalize_run(conn, run_id, status="bogus")


# ── reaper ────────────────────────────────────────────────────


def test_reap_stuck_marks_old_running_as_aborted(conn):
    run_id = watch_sync.claim_sync_run(conn, "emby")
    # Force started_at way in the past
    conn.execute(
        "UPDATE watch_sync_runs SET started_at=? WHERE id=?",
        (int(time.time()) - 99999, run_id),
    )
    conn.commit()
    n = watch_sync.reap_stuck_sync_runs(conn, timeout_secs=600)
    assert n == 1
    row = conn.execute("SELECT status, error FROM watch_sync_runs WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "aborted"


def test_reap_does_not_touch_recent_running(conn):
    watch_sync.claim_sync_run(conn, "emby")
    n = watch_sync.reap_stuck_sync_runs(conn, timeout_secs=600)
    assert n == 0


# ── end-to-end sync_emby with fake client ─────────────────────


class _FakeEmbyClient:
    def __init__(self, items, series_map):
        self._items = items
        self._series_map = series_map

    def list_watched(self, *, since_iso=None):
        return self._items

    def get_series_tmdb_ids(self, series_ids):
        return {sid: self._series_map.get(sid) for sid in series_ids}


def test_sync_emby_inserts_mapped_items(db_path):
    items = [
        EmbyItem(
            id="m-1",
            type="Movie",
            name="Godfather",
            year=1972,
            tmdb_id="238",
            imdb_id=None,
            series_id=None,
            season_number=None,
            episode_number=None,
            last_played_at=1700000000,
        ),
        EmbyItem(
            id="e-1",
            type="Episode",
            name="Pilot",
            year=2013,
            tmdb_id="ep-1",
            imdb_id=None,
            series_id="emby-1399",
            season_number=1,
            episode_number=1,
            last_played_at=1700000000,
        ),
    ]
    client = _FakeEmbyClient(items, {"emby-1399": "1399"})

    def open_conn():
        return destructive_action.open_connection(db_path)

    run_id = watch_sync.sync_emby(
        open_conn=open_conn,
        emby_client=client,
        run_in_thread=False,
    )

    conn = open_conn()
    try:
        run = watch_sync.get_sync_status(conn, run_id)
        assert run["status"] == "done"
        assert run["items_fetched"] == 2
        assert run["items_inserted"] == 2
        rows = conn.execute(
            "SELECT provider_item_id, media_type, tmdb_movie_id, tmdb_series_id, tmdb_episode_id "
            "FROM watched_items ORDER BY provider_item_id"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 2
    movie_row = [r for r in rows if r["provider_item_id"] == "m-1"][0]
    ep_row = [r for r in rows if r["provider_item_id"] == "e-1"][0]
    assert movie_row["tmdb_movie_id"] == "238"
    assert movie_row["tmdb_series_id"] is None
    assert ep_row["tmdb_movie_id"] is None
    assert ep_row["tmdb_episode_id"] == "ep-1"
    assert ep_row["tmdb_series_id"] == "1399"


# ── bug #13: raw_hash 含 item-defining 字段 + UPDATE 写全 ──


def test_map_raw_hash_changes_when_season_episode_corrected():
    """#13a: 修正 s/e（last_played_at 不变）必须改变 raw_hash，否则 re-sync 静默跳过。"""
    base = dict(
        id="e1",
        type="Episode",
        name="Pilot",
        year=2013,
        tmdb_id="ep-1",
        imdb_id=None,
        series_id="s1",
        last_played_at=1700000000,
    )
    w1 = watch_sync.map_emby_item_to_watched(
        EmbyItem(**base, season_number=1, episode_number=1), {"s1": "1399"}
    )
    w2 = watch_sync.map_emby_item_to_watched(
        EmbyItem(**base, season_number=2, episode_number=5), {"s1": "1399"}
    )
    assert w1.raw_hash != w2.raw_hash


def test_upsert_update_persists_corrected_season_episode_and_imdb(conn):
    """#13b: raw_hash 变触发 UPDATE 时，s/e/media_type/imdb_id 必须真写进 DB。"""
    base = dict(
        id="e1",
        type="Episode",
        name="Pilot",
        year=2013,
        tmdb_id="ep-1",
        series_id="s1",
        last_played_at=1700000000,
    )
    w1 = watch_sync.map_emby_item_to_watched(
        EmbyItem(**base, imdb_id=None, season_number=1, episode_number=1), {"s1": "1399"}
    )
    assert watch_sync._upsert_watched_item(conn, w1) == "inserted"
    # 修正 s/e + imdb（同 provider_item_id）
    w2 = watch_sync.map_emby_item_to_watched(
        EmbyItem(**base, imdb_id="tt999", season_number=2, episode_number=5), {"s1": "1399"}
    )
    assert watch_sync._upsert_watched_item(conn, w2) == "updated"
    row = conn.execute(
        "SELECT season_number, episode_number, imdb_id FROM watched_items "
        "WHERE provider='emby' AND provider_item_id='e1'"
    ).fetchone()
    assert (row["season_number"], row["episode_number"], row["imdb_id"]) == (2, 5, "tt999")


# ── bug #10: 单个坏 item 不应 rollback 整批 ──


def test_sync_emby_one_bad_item_does_not_abort_batch(db_path):
    """#10: 单个 item map 抛错（type 未知）不应 rollback 整批；其余 item 入库，run=done。"""
    items = [
        EmbyItem(
            id="m-1",
            type="Movie",
            name="Good1",
            year=1972,
            tmdb_id="238",
            imdb_id=None,
            series_id=None,
            season_number=None,
            episode_number=None,
            last_played_at=1700000000,
        ),
        EmbyItem(
            id="bad",
            type="Weird",
            name="Bad",
            year=None,
            tmdb_id=None,
            imdb_id=None,
            series_id=None,
            season_number=None,
            episode_number=None,
            last_played_at=1700000000,
        ),
        EmbyItem(
            id="m-2",
            type="Movie",
            name="Good2",
            year=1974,
            tmdb_id="240",
            imdb_id=None,
            series_id=None,
            season_number=None,
            episode_number=None,
            last_played_at=1700000000,
        ),
    ]
    client = _FakeEmbyClient(items, {})

    def open_conn():
        return destructive_action.open_connection(db_path)

    run_id = watch_sync.sync_emby(open_conn=open_conn, emby_client=client, run_in_thread=False)
    conn = open_conn()
    try:
        run = watch_sync.get_sync_status(conn, run_id)
        ids = [
            r["provider_item_id"]
            for r in conn.execute(
                "SELECT provider_item_id FROM watched_items ORDER BY provider_item_id"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert run["status"] == "done", "one bad item aborted the whole sync"
    assert ids == ["m-1", "m-2"], f"good items lost: {ids}"
    assert run["items_skipped"] >= 1


# ── bug #14: watch-sync reaper 必须接进 cron ──


def test_cron_reap_stuck_invokes_watch_sync_reaper(monkeypatch):
    """#14: _cron_reap_stuck 必须调 watch_sync.reap_stuck_sync_runs，否则崩溃的同步
    永久占住 uniq_watch_sync_running 单飞槽，所有后续同步被 ConcurrentSyncError 拒。"""
    import app as app_module

    class _FakeConn:
        def close(self):
            pass

    calls = {"sync_reaper": 0}
    monkeypatch.setattr(app_module.destructive_action, "open_connection", lambda p: _FakeConn())
    monkeypatch.setattr(app_module.destructive_action, "reap_stuck_actions", lambda c: 0)
    monkeypatch.setattr(app_module.destructive_action, "cleanup_expired_pending", lambda c: 0)

    def _fake_sync_reaper(conn, **kw):
        calls["sync_reaper"] += 1
        return 0

    monkeypatch.setattr(app_module.watch_sync, "reap_stuck_sync_runs", _fake_sync_reaper)

    app_module._cron_reap_stuck()
    assert calls["sync_reaper"] == 1, "watch-sync reaper not wired into cron"


def test_sync_emby_concurrent_call_raises(db_path):
    """Concurrent invocation while a previous run is still 'running' → ConcurrentSyncError."""
    items = []
    client = _FakeEmbyClient(items, {})

    def open_conn():
        return destructive_action.open_connection(db_path)

    # First call: synchronous (run_in_thread=False) — finalizes immediately
    watch_sync.sync_emby(open_conn=open_conn, emby_client=client, run_in_thread=False)

    # Now manually open a "stuck" run to simulate concurrent state
    c = open_conn()
    try:
        watch_sync.claim_sync_run(c, "emby")
    finally:
        c.close()

    # Second call should hit ConcurrentSyncError
    with pytest.raises(watch_sync.ConcurrentSyncError):
        watch_sync.sync_emby(open_conn=open_conn, emby_client=client, run_in_thread=False)


# ── EmbyClient unit tests (no real HTTP) ──────────────────────


def test_emby_client_init_requires_all_args():
    with pytest.raises(ValueError):
        EmbyClient(base_url="", user_id="u", api_key="k")
    with pytest.raises(ValueError):
        EmbyClient(base_url="https://x", user_id="", api_key="k")
    with pytest.raises(ValueError):
        EmbyClient(base_url="https://x", user_id="u", api_key="")


def test_emby_client_test_connection_handles_auth_failure(monkeypatch):
    client = EmbyClient(base_url="https://emby.example", user_id="u", api_key="bad")

    class FakeResp:
        status_code = 401

        def json(self):
            return {}

        def raise_for_status(self):
            pass

    def fake_get(*args, **kwargs):
        return FakeResp()

    monkeypatch.setattr(client._session, "get", fake_get)
    result = client.test_connection()
    assert result["ok"] is False
    assert result["code"] == "auth_failed"


def test_emby_client_test_connection_ok(monkeypatch):
    client = EmbyClient(base_url="https://emby.example", user_id="u", api_key="ok")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"ServerName": "Home Emby", "Version": "4.8.0"}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(client._session, "get", lambda *a, **kw: FakeResp())
    result = client.test_connection()
    assert result["ok"] is True
    assert "Home Emby" in result["message"]
    assert result["server_name"] == "Home Emby"
