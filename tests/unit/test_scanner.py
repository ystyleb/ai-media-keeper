"""scanner 单元测试：worker thread 流程 + abort + skip + failed."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from services import destructive_action, metadata_cache, scanner
from services.identify import FilenameParse, IdentifyResult
from services.metadata.base import MediaCandidate

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


@pytest.fixture
def db_path(tmp_path):
    """fresh sqlite db with full schema + Phase 3 migration."""
    p = tmp_path / "t.db"
    conn = destructive_action.open_connection(p)
    destructive_action.init_schema(conn, SCHEMA_PATH)
    from db import migrations

    migrations.phase3_migrate(conn)
    conn.close()
    # Reset module-level guard between tests
    with scanner._active_lock:
        scanner._active_scan_id = None
        scanner._active_thread = None
    return p


@pytest.fixture
def conn(db_path):
    c = destructive_action.open_connection(db_path)
    yield c
    c.close()


def _stat(path: str, *, mtime: int = 1000, inode: int = 1, size: int = 100) -> dict:
    return {"exists": True, "inode": inode, "size_bytes": size, "mtime": mtime, "is_dir": False}


def _candidate(tmdb_id: str = "60625") -> MediaCandidate:
    return MediaCandidate(
        id=f"tmdb:tv:{tmdb_id}",
        external_ids={"tmdb_id": tmdb_id},
        title="Show",
        original_title="Show",
        year=2020,
        media_type="tv",
        poster_url=None,
        overview=None,
        vote_average=8.0,
        raw={},
    )


def _identify_result(path: str, *, top: MediaCandidate | None = None) -> IdentifyResult:
    parse = FilenameParse(
        raw_name=path.rsplit("/", 1)[-1],
        title="Show",
        year=2020,
        season=1,
        episode=1,
        episode_title=None,
        media_type="episode",
        resolution="1080p",
        source="WEB-DL",
        release_group=None,
        codec=None,
        color_depth=None,
        hdr_profiles=[],
        container=None,
        audio_codec=None,
        raw={},
    )
    return IdentifyResult(
        parse=parse,
        candidates=[top] if top else [],
        top_pick=top,
        confidence=0.95 if top else 0.0,
        reasoning="ok" if top else "no candidates",
        pick_source="single_exact" if top else "needs_review",
    )


def _wait_for_completion(db_path: Path, scan_run_id: int, timeout: float = 5.0) -> None:
    """轮询 scan_runs.status 直到非 running 或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = destructive_action.open_connection(db_path)
        try:
            row = conn.execute(
                "SELECT status FROM scan_runs WHERE id = ?", (scan_run_id,)
            ).fetchone()
            if row and row["status"] != "running":
                return
        finally:
            conn.close()
        time.sleep(0.05)
    raise AssertionError(f"scan {scan_run_id} did not complete within {timeout}s")


# ---------------- happy path: list + identify + done ---------------- #


def test_happy_path_three_files_all_identified(db_path):
    paths = ["/a.mkv", "/b.mkv", "/c.mkv"]
    identify = MagicMock(side_effect=lambda p: _identify_result(p, top=_candidate()))
    ssh_stat = MagicMock(side_effect=lambda ps: {ps[0]: _stat(ps[0])})
    list_paths = MagicMock(return_value=paths)

    run_id = scanner.start_scan(
        db_path=db_path,
        base_path="/share",
        max_depth=5,
        list_video_paths=list_paths,
        ssh_stat=ssh_stat,
        identify=identify,
    )
    _wait_for_completion(db_path, run_id)

    conn = destructive_action.open_connection(db_path)
    try:
        summary = scanner.get_status(conn, run_id)
    finally:
        conn.close()
    assert summary is not None
    assert summary.status == "done"
    assert summary.files_total == 3
    assert summary.files_done == 3
    assert summary.files_failed == 0
    assert summary.files_skipped == 0
    assert identify.call_count == 3


# ---------------- skipped_unchanged: cache hit ---------------- #


def test_cached_files_are_skipped(db_path):
    """已识别且 mtime 一致 → skip identify."""
    # 预填 cache（mtime=2000）
    conn = destructive_action.open_connection(db_path)
    try:
        metadata_cache.upsert_identification(
            conn,
            path="/a.mkv",
            stat={"inode": 1, "size_bytes": 100, "mtime": 2000},
            identify_result=_identify_result("/a.mkv", top=_candidate()),
        )
    finally:
        conn.close()

    identify = MagicMock(side_effect=lambda p: _identify_result(p, top=_candidate()))
    # ssh_stat 返回跟 cache 一致的 mtime → 应该 skip
    ssh_stat = MagicMock(side_effect=lambda ps: {ps[0]: _stat(ps[0], mtime=2000)})

    run_id = scanner.start_scan(
        db_path=db_path,
        base_path="/share",
        max_depth=5,
        list_video_paths=lambda *a: ["/a.mkv"],
        ssh_stat=ssh_stat,
        identify=identify,
    )
    _wait_for_completion(db_path, run_id)

    conn = destructive_action.open_connection(db_path)
    try:
        s = scanner.get_status(conn, run_id)
    finally:
        conn.close()
    assert s.files_skipped == 1
    assert s.files_done == 0
    assert identify.call_count == 0  # 没调 TMDB


def test_stale_cache_triggers_reidentify(db_path):
    """mtime 不一致 → 重识别 (status='done')."""
    conn = destructive_action.open_connection(db_path)
    try:
        metadata_cache.upsert_identification(
            conn,
            path="/a.mkv",
            stat={"inode": 1, "size_bytes": 100, "mtime": 1000},  # 老 mtime
            identify_result=_identify_result("/a.mkv", top=_candidate()),
        )
    finally:
        conn.close()

    identify = MagicMock(side_effect=lambda p: _identify_result(p, top=_candidate("12345")))
    # ssh_stat 给新 mtime
    ssh_stat = MagicMock(side_effect=lambda ps: {ps[0]: _stat(ps[0], mtime=9999)})

    run_id = scanner.start_scan(
        db_path=db_path,
        base_path="/share",
        max_depth=5,
        list_video_paths=lambda *a: ["/a.mkv"],
        ssh_stat=ssh_stat,
        identify=identify,
    )
    _wait_for_completion(db_path, run_id)

    conn = destructive_action.open_connection(db_path)
    try:
        s = scanner.get_status(conn, run_id)
        cached, _ = metadata_cache.get_by_path(conn, "/a.mkv")
    finally:
        conn.close()
    assert s.files_done == 1
    assert s.files_skipped == 0
    assert identify.call_count == 1
    # cache 已更新为新的 tmdb_id + mtime
    assert cached.tmdb_id == "12345"
    assert cached.mtime == 9999


# ---------------- failed item doesn't stop the run ---------------- #


def test_failed_item_doesnt_stop_subsequent(db_path):
    def maybe_fail(p):
        if "bad" in p:
            raise RuntimeError("synthetic identify failure")
        return _identify_result(p, top=_candidate())

    identify = MagicMock(side_effect=maybe_fail)
    ssh_stat = MagicMock(side_effect=lambda ps: {ps[0]: _stat(ps[0])})

    run_id = scanner.start_scan(
        db_path=db_path,
        base_path="/share",
        max_depth=5,
        list_video_paths=lambda *a: ["/a.mkv", "/bad.mkv", "/c.mkv"],
        ssh_stat=ssh_stat,
        identify=identify,
    )
    _wait_for_completion(db_path, run_id)

    conn = destructive_action.open_connection(db_path)
    try:
        s = scanner.get_status(conn, run_id)
        failed = scanner.list_failed_items(conn, run_id)
    finally:
        conn.close()
    assert s.status == "done"
    assert s.files_done == 2
    assert s.files_failed == 1
    assert len(failed) == 1
    assert failed[0]["path"] == "/bad.mkv"
    assert "synthetic identify failure" in failed[0]["error"]


# ---------------- file not found at scan time ---------------- #


def test_file_not_found_marks_failed(db_path):
    identify = MagicMock(side_effect=lambda p: _identify_result(p, top=_candidate()))
    # 模拟 stat 报文件不存在
    ssh_stat = MagicMock(side_effect=lambda ps: {ps[0]: {"exists": False}})

    run_id = scanner.start_scan(
        db_path=db_path,
        base_path="/share",
        max_depth=5,
        list_video_paths=lambda *a: ["/gone.mkv"],
        ssh_stat=ssh_stat,
        identify=identify,
    )
    _wait_for_completion(db_path, run_id)

    conn = destructive_action.open_connection(db_path)
    try:
        s = scanner.get_status(conn, run_id)
        failed = scanner.list_failed_items(conn, run_id)
    finally:
        conn.close()
    assert s.files_failed == 1
    assert failed[0]["error"] == "file_not_found_at_scan_time"
    assert identify.call_count == 0  # 文件不存在跳过 identify


# ---------------- abort signal ---------------- #


def test_abort_stops_worker_loop(db_path):
    """abort 后 worker 应该在下一轮 loop 退出，剩下的 pending 不处理。"""
    # 用 Event 让 identify 在第一个 item 处 block 住，给我们机会 abort
    pause = threading.Event()
    processed: list[str] = []

    def slow_identify(p):
        processed.append(p)
        pause.wait(timeout=2.0)
        return _identify_result(p, top=_candidate())

    identify = MagicMock(side_effect=slow_identify)
    ssh_stat = MagicMock(side_effect=lambda ps: {ps[0]: _stat(ps[0])})

    run_id = scanner.start_scan(
        db_path=db_path,
        base_path="/share",
        max_depth=5,
        list_video_paths=lambda *a: ["/a.mkv", "/b.mkv", "/c.mkv", "/d.mkv"],
        ssh_stat=ssh_stat,
        identify=identify,
    )

    # 等 worker 卡在第一个 item
    deadline = time.time() + 2
    while not processed and time.time() < deadline:
        time.sleep(0.02)
    assert processed, "worker never started processing"

    # abort
    conn = destructive_action.open_connection(db_path)
    try:
        ok = scanner.abort_scan(conn, run_id)
    finally:
        conn.close()
    assert ok is True

    # 解锁第一个 item, worker 完成它然后下一轮看到 abort 退出
    pause.set()
    _wait_for_completion(db_path, run_id, timeout=3)

    conn = destructive_action.open_connection(db_path)
    try:
        s = scanner.get_status(conn, run_id)
    finally:
        conn.close()
    assert s.status == "aborted"
    # 第一个 item 完成了，剩 3 个没处理
    assert s.files_done == 1
    assert len(processed) == 1


# ---------------- concurrent start raises ---------------- #


def test_concurrent_start_raises(db_path):
    """全局只允许一个 active scan。"""
    pause = threading.Event()
    identify = MagicMock(
        side_effect=lambda p: (pause.wait(timeout=2.0), _identify_result(p, top=_candidate()))[1]
    )
    ssh_stat = MagicMock(side_effect=lambda ps: {ps[0]: _stat(ps[0])})

    scanner.start_scan(
        db_path=db_path,
        base_path="/share",
        max_depth=5,
        list_video_paths=lambda *a: ["/a.mkv"],
        ssh_stat=ssh_stat,
        identify=identify,
    )

    # 第二次 start 应当 raise
    with pytest.raises(scanner.ConcurrentScanError):
        scanner.start_scan(
            db_path=db_path,
            base_path="/share",
            max_depth=5,
            list_video_paths=lambda *a: ["/b.mkv"],
            ssh_stat=ssh_stat,
            identify=identify,
        )

    pause.set()  # 让 worker 完成


# ---------------- list paths failure marks run failed ---------------- #


def test_list_paths_failure_marks_run_failed(db_path):
    def bad_list(*a):
        raise RuntimeError("ssh disconnect")

    identify = MagicMock()
    ssh_stat = MagicMock()
    run_id = scanner.start_scan(
        db_path=db_path,
        base_path="/share",
        max_depth=5,
        list_video_paths=bad_list,
        ssh_stat=ssh_stat,
        identify=identify,
    )
    _wait_for_completion(db_path, run_id)

    conn = destructive_action.open_connection(db_path)
    try:
        s = scanner.get_status(conn, run_id)
    finally:
        conn.close()
    assert s.status == "failed"
    assert s.error is not None
    assert "ssh disconnect" in s.error
    assert identify.call_count == 0
    assert ssh_stat.call_count == 0


# ---------------- list_recent_runs ---------------- #


def test_list_recent_runs_returns_most_recent_first(db_path):
    identify = MagicMock(side_effect=lambda p: _identify_result(p, top=_candidate()))
    ssh_stat = MagicMock(side_effect=lambda ps: {ps[0]: _stat(ps[0])})

    ids = []
    for i in range(3):
        # 等前一次完成
        if ids:
            _wait_for_completion(db_path, ids[-1])
        run_id = scanner.start_scan(
            db_path=db_path,
            base_path=f"/share/{i}",
            max_depth=5,
            list_video_paths=lambda *a: ["/x.mkv"],
            ssh_stat=ssh_stat,
            identify=identify,
        )
        ids.append(run_id)
        time.sleep(0.05)  # 让 started_at 严格递增
    _wait_for_completion(db_path, ids[-1])

    conn = destructive_action.open_connection(db_path)
    try:
        runs = scanner.list_recent_runs(conn, limit=10)
    finally:
        conn.close()
    assert len(runs) == 3
    # 最新的在前
    assert runs[0]["base_path"] == "/share/2"
    assert runs[2]["base_path"] == "/share/0"


# ---------------- claim_next_pending atomic ---------------- #


def test_claim_next_pending_atomic(conn):
    """SELECT-then-guarded-UPDATE：同一 row 被 claim 一次后再 claim 应返回不同 row 或 None。"""
    # 手动准备 scan_run + 2 items
    conn.execute(
        "INSERT INTO scan_runs (base_path, max_depth, started_at, status) "
        "VALUES (?, 5, ?, 'running')",
        ("/share", int(time.time())),
    )
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for p in ["/a.mkv", "/b.mkv"]:
        conn.execute(
            "INSERT INTO scan_items (scan_run_id, path, status) VALUES (?, ?, 'pending')",
            (run_id, p),
        )
    conn.commit()

    first = scanner._claim_next_pending(conn, run_id)
    second = scanner._claim_next_pending(conn, run_id)
    third = scanner._claim_next_pending(conn, run_id)

    assert first is not None and second is not None
    assert first["id"] != second["id"]
    assert third is None  # 没 pending 了


def test_claim_next_pending_no_pending_returns_none(conn):
    conn.execute(
        "INSERT INTO scan_runs (base_path, max_depth, started_at, status) "
        "VALUES (?, 5, ?, 'running')",
        ("/share", int(time.time())),
    )
    run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    assert scanner._claim_next_pending(conn, run_id) is None
