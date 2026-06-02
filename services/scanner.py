"""全库后台扫描 worker — Phase 2 闭环：一次按钮跑遍 NAS 库 + 增量复用 cache。

核心循环：
  list_video_paths(base, depth) → 批量 INSERT scan_items (pending)
  while pending exists 且未 abort:
      claim_next → in_progress
      ssh_stat(path) → 跟 media_files cache 比 mtime
        一致 → skipped_unchanged
        不一致 / 无缓存 → identify() + upsert → done
      异常 → failed + error
  完成 → scan_runs.status='done'

并发模型：单 worker thread + 模块级 lock 保证全局唯一 active scan（避免
N 个 worker 同时打爆 TMDB rate limit）。

依赖注入：worker 接受 list_video_paths / identify / ssh_stat 三个 callable，
解耦 SSH / TMDB / metadata_cache 三个 side effect，单元测试 mock 即可。

参考 plan v3 §2.3 scan_runs + scan_items 状态机。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import destructive_action, metadata_cache

logger = logging.getLogger(__name__)


# ─── 全局唯一 active scan guard ───
_active_lock = threading.Lock()
_active_scan_id: int | None = None
_active_thread: threading.Thread | None = None

# Worker 周期检查 abort 信号的间隔（每 N 个 item 检查一次 + sleep）
_ABORT_CHECK_EVERY = 5


class ConcurrentScanError(RuntimeError):
    """已有 scan 在跑 — 同时只允许一个，防 TMDB rate limit + DB 写争用。"""


# Dependency-inject 的 callable 签名
ListVideoPaths = Callable[[str, int], list[str]]  # (base_path, max_depth) → paths
SSHStat = Callable[
    [list[str]], dict[str, dict[str, Any]]
]  # paths → {path: {inode, size_bytes, mtime, exists, is_dir}}
Identify = Callable[[str], Any]  # path → IdentifyResult


@dataclass(frozen=True)
class ScanRunSummary:
    scan_run_id: int
    base_path: str
    max_depth: int
    status: str
    files_total: int
    files_done: int
    files_failed: int
    files_skipped: int
    current_path: str | None
    started_at: int
    completed_at: int | None
    error: str | None


def _now() -> int:
    return int(time.time())


def _is_active() -> int | None:
    """返回当前 active scan_run_id，None = idle。"""
    with _active_lock:
        return _active_scan_id


def start_scan(
    *,
    db_path: Path | str,
    base_path: str,
    max_depth: int,
    list_video_paths: ListVideoPaths,
    ssh_stat: SSHStat,
    identify: Identify,
) -> int:
    """开启 worker thread。返回 scan_run_id。

    全局只允许一个 active scan；已有则 raise ConcurrentScanError。
    """
    global _active_scan_id, _active_thread

    with _active_lock:
        if _active_scan_id is not None:
            raise ConcurrentScanError(f"scan {_active_scan_id} is already running; abort it first")

        # 1. 创建 scan_runs row
        conn = destructive_action.open_connection(db_path)
        try:
            cur = conn.execute(
                """
                INSERT INTO scan_runs (base_path, max_depth, started_at, status)
                VALUES (?, ?, ?, 'running')
                """,
                (base_path, max_depth, _now()),
            )
            scan_run_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
        assert scan_run_id is not None

        # 2. 启动 worker thread
        t = threading.Thread(
            target=_worker_main,
            args=(scan_run_id, db_path, base_path, max_depth, list_video_paths, ssh_stat, identify),
            name=f"scan-worker-{scan_run_id}",
            daemon=True,
        )
        _active_scan_id = scan_run_id
        _active_thread = t
        # bug #16: t.start() 抛错（如 "can't start new thread" 线程耗尽）时清 active
        # state 再抛，否则 _active_scan_id 永久卡住 + scan_runs row 永远 'running'
        # （_worker_main 没真正启动 → 它的 finally 不会清锁）。对齐
        # organize_runner.start_organize_executor 的处理。
        try:
            t.start()
        except Exception:
            _active_scan_id = None
            _active_thread = None
            try:
                fc = destructive_action.open_connection(db_path)
                try:
                    _mark_run_failed(fc, scan_run_id, "thread_start_failed")
                finally:
                    fc.close()
            except Exception:
                logger.exception(
                    f"[scan] failed to mark run {scan_run_id} failed after start error"
                )
            raise

    logger.info(f"[scan] started scan_run_id={scan_run_id} base={base_path}")
    return scan_run_id


def _worker_main(
    scan_run_id: int,
    db_path: Path | str,
    base_path: str,
    max_depth: int,
    list_video_paths: ListVideoPaths,
    ssh_stat: SSHStat,
    identify: Identify,
) -> None:
    """Worker thread 主循环。"""
    global _active_scan_id, _active_thread

    # bug #15: conn=None 初始化在 try 外 + open_connection 移进 try，否则 open 抛错时
    # finally 根本不执行 → active lock 永久卡住（对齐 organize_runner._worker_main）。
    conn: sqlite3.Connection | None = None
    try:
        conn = destructive_action.open_connection(db_path)
        # Phase 1: list paths + populate scan_items
        try:
            paths = list_video_paths(base_path, max_depth)
        except Exception as e:
            logger.exception(f"[scan] list_video_paths failed for run {scan_run_id}")
            _mark_run_failed(conn, scan_run_id, f"list_failed: {e}")
            return

        for p in paths:
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO scan_items (scan_run_id, path, status)
                    VALUES (?, ?, 'pending')
                    """,
                    (scan_run_id, p),
                )
            except sqlite3.Error as e:
                logger.warning(f"[scan] insert scan_item failed for {p}: {e}")
        conn.execute(
            "UPDATE scan_runs SET files_total = ? WHERE id = ?",
            (len(paths), scan_run_id),
        )
        conn.commit()

        logger.info(f"[scan] run {scan_run_id}: {len(paths)} files to process")

        # Phase 2: 循环处理 pending items
        processed = 0
        while True:
            # 周期检查 abort（每个 item 都查一次；DB 查询便宜）
            run_status = _get_run_status(conn, scan_run_id)
            if run_status != "running":
                logger.info(f"[scan] run {scan_run_id} status={run_status} → exit loop")
                break

            item = _claim_next_pending(conn, scan_run_id)
            if item is None:
                break  # 没 pending 了 → 完成

            path = item["path"]
            conn.execute(
                "UPDATE scan_runs SET current_path = ? WHERE id = ?",
                (path, scan_run_id),
            )
            conn.commit()

            try:
                _process_one_item(conn, scan_run_id, path, ssh_stat, identify)
            except Exception as e:
                logger.exception(f"[scan] item {path} crashed")
                _mark_item(conn, item["id"], "failed", error=f"{type(e).__name__}: {e}")
                _bump_count(conn, scan_run_id, "files_failed")

            processed += 1
            # 节制 TMDB rate limit (40 req/10s)。识别本身已 ≥0.5s，再加 50ms 安全。
            time.sleep(0.05)

        # Phase 3: 完成
        run_status = _get_run_status(conn, scan_run_id)
        if run_status == "running":
            conn.execute(
                """
                UPDATE scan_runs
                   SET status = 'done', completed_at = ?, current_path = NULL
                 WHERE id = ?
                """,
                (_now(), scan_run_id),
            )
            conn.commit()
            logger.info(f"[scan] run {scan_run_id}: done (processed {processed})")
        # 已 aborted / failed 的话状态已被设过，不覆盖

    except Exception as e:
        # bug #15: 任何未预期崩溃（含 open_connection 失败）都在这里兜底 —
        # best-effort 标 run failed（conn 可能为 None / 已坏，用 fresh conn），
        # 不向上抛（daemon thread 抛出只会污染 stderr）。finally 仍清 active lock。
        logger.exception(f"[scan] run {scan_run_id} worker crashed")
        try:
            fc = destructive_action.open_connection(db_path)
            try:
                _mark_run_failed(fc, scan_run_id, f"worker_crashed: {type(e).__name__}: {e}")
            finally:
                fc.close()
        except Exception:
            pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        with _active_lock:
            if _active_scan_id == scan_run_id:
                _active_scan_id = None
                _active_thread = None


def _process_one_item(
    conn: sqlite3.Connection,
    scan_run_id: int,
    path: str,
    ssh_stat: SSHStat,
    identify: Identify,
) -> None:
    """处理单个 scan_item：stat → 看 cache 是否 stale → identify or skip。"""
    item_row = conn.execute(
        "SELECT id FROM scan_items WHERE scan_run_id = ? AND path = ?",
        (scan_run_id, path),
    ).fetchone()
    if item_row is None:
        return
    item_id = item_row["id"]

    # SSH stat 拿当前 mtime
    stat_map = ssh_stat([path])
    stat = stat_map.get(path, {})
    if not stat.get("exists"):
        _mark_item(conn, item_id, "failed", error="file_not_found_at_scan_time")
        _bump_count(conn, scan_run_id, "files_failed")
        return

    # 看 cache 是否 fresh (mtime + inode 都一致 → skip identify)
    cached, cache_status = metadata_cache.get_by_path(
        conn,
        path,
        current_mtime=stat.get("mtime"),
        current_inode=stat.get("inode"),
    )
    if cache_status == "hit" and cached and cached.metadata_status == "ok":
        _mark_item(conn, item_id, "skipped_unchanged")
        _bump_count(conn, scan_run_id, "files_skipped")
        return

    # 跑识别 + 写 cache
    result = identify(path)
    metadata_cache.upsert_identification(
        conn,
        path=path,
        stat=stat,
        identify_result=result,
    )
    _mark_item(conn, item_id, "done")
    _bump_count(conn, scan_run_id, "files_done")


def _claim_next_pending(conn: sqlite3.Connection, scan_run_id: int) -> sqlite3.Row | None:
    """原子 claim：找到一个 pending → in_progress，返回该行。

    SQLite 没有 SELECT FOR UPDATE，用 guarded UPDATE：
      UPDATE scan_items SET status='in_progress'
       WHERE id = (SELECT id ... WHERE status='pending' LIMIT 1)
         AND status = 'pending';
    若 rowcount=1 → 真 claim 到了；返回该行。
    """
    # 先 SELECT 一个 pending id；
    pick = conn.execute(
        """
        SELECT id FROM scan_items
         WHERE scan_run_id = ? AND status = 'pending'
         ORDER BY id LIMIT 1
        """,
        (scan_run_id,),
    ).fetchone()
    if pick is None:
        return None

    # guarded UPDATE
    cur = conn.execute(
        """
        UPDATE scan_items
           SET status = 'in_progress', last_attempt_at = ?
         WHERE id = ? AND status = 'pending'
        """,
        (_now(), pick["id"]),
    )
    if cur.rowcount != 1:
        conn.rollback()
        return None
    conn.commit()
    return conn.execute("SELECT * FROM scan_items WHERE id = ?", (pick["id"],)).fetchone()


def _mark_item(
    conn: sqlite3.Connection,
    item_id: int,
    status: str,
    *,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE scan_items
           SET status = ?, error = ?, last_attempt_at = ?
         WHERE id = ?
        """,
        (status, error, _now(), item_id),
    )
    conn.commit()


def _bump_count(conn: sqlite3.Connection, scan_run_id: int, col: str) -> None:
    """col ∈ {'files_done', 'files_failed', 'files_skipped'}"""
    assert col in ("files_done", "files_failed", "files_skipped")
    conn.execute(
        f"UPDATE scan_runs SET {col} = {col} + 1 WHERE id = ?",
        (scan_run_id,),
    )
    conn.commit()


def _get_run_status(conn: sqlite3.Connection, scan_run_id: int) -> str:
    row = conn.execute("SELECT status FROM scan_runs WHERE id = ?", (scan_run_id,)).fetchone()
    return row["status"] if row else "unknown"


def _mark_run_failed(conn: sqlite3.Connection, scan_run_id: int, error: str) -> None:
    conn.execute(
        """
        UPDATE scan_runs
           SET status = 'failed', completed_at = ?, error = ?, current_path = NULL
         WHERE id = ?
        """,
        (_now(), error, scan_run_id),
    )
    conn.commit()


def abort_scan(conn: sqlite3.Connection, scan_run_id: int) -> bool:
    """设 scan_runs.status='aborted'，worker 下一个 loop 自然退出。

    立刻不会 cancel 当前正在处理的 item（避免 partial cache write）。
    """
    cur = conn.execute(
        """
        UPDATE scan_runs
           SET status = 'aborted'
         WHERE id = ? AND status = 'running'
        """,
        (scan_run_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def get_status(conn: sqlite3.Connection, scan_run_id: int) -> ScanRunSummary | None:
    row = conn.execute("SELECT * FROM scan_runs WHERE id = ?", (scan_run_id,)).fetchone()
    if row is None:
        return None
    return ScanRunSummary(
        scan_run_id=row["id"],
        base_path=row["base_path"],
        max_depth=row["max_depth"],
        status=row["status"],
        files_total=row["files_total"],
        files_done=row["files_done"],
        files_failed=row["files_failed"],
        files_skipped=row["files_skipped"],
        current_path=row["current_path"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error=row["error"],
    )


def list_failed_items(conn: sqlite3.Connection, scan_run_id: int, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT path, error, last_attempt_at
          FROM scan_items
         WHERE scan_run_id = ? AND status = 'failed'
         ORDER BY id LIMIT ?
        """,
        (scan_run_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def list_recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    # ORDER BY started_at DESC, id DESC —— 同 second 内启动的 run 用 id 兜底排序
    # （started_at 是 int(time.time())，可能撞同值）
    rows = conn.execute(
        """
        SELECT id, base_path, status, started_at, completed_at,
               files_total, files_done, files_failed, files_skipped
          FROM scan_runs
         ORDER BY started_at DESC, id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
