"""Phase 4C.1：qBit completion detector + auto_organize_runs 行生命周期 helpers.

只暴露纯逻辑函数（不直接调 qBit API；接受 torrents list 输入），方便单测。
Cron job (4C.4) 自己调 qbit.get_torrents() 然后传给本模块 filter / dispatch。

设计契约：
  - **不变量**：同一 qbit_hash 在表里至多一条 row（PK 强制）
  - **状态机**：(没 row) → pending → organizing → terminal (succeeded / failed /
    skipped_*)，单向，从 terminal 退不出来（重试 = 老 row 由 user 手动 reset / 加新逻辑）
  - **并发保护**：partial unique idx (status='organizing') 兜底防同 hash 并发 organizing；
    organize_runner 本身的 module-level lock 是第一道（同进程同时只一个 organize 跑）
  - **claim 语义**：mark_organizing 走 guarded UPDATE WHERE status='pending'，
    rowcount=1 才算 claim 成功；其他 caller 全 fail（terminal / 别 worker 抢先）
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

# qBit state 中"已完成"枚举值（progress=1.0 仍是主判据，state 是 secondary）。
# Source: qBittorrent WebAPI doc - https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)
#   uploading / stalledUP / pausedUP / queuedUP / forcedUP / completed / seeding
COMPLETED_STATES = frozenset({
    "uploading", "stalledUP", "pausedUP", "queuedUP", "forcedUP",
    "completed", "seeding",
})


# ── completion detection ───


def list_completed_torrents(
    torrents: list[dict],
    category_whitelist: list[str] | tuple[str, ...],
) -> list[dict]:
    """从 qBit /torrents/info 原始 list 过滤出「应该自动 organize」的种子.

    判据：
      1. progress == 1.0（主判据；下载完成）
      2. state ∈ COMPLETED_STATES（secondary；排 downloading/error/missing files）
      3. category ∈ whitelist（user 显式授权的分类才动；whitelist 空 = 不触发）
      4. content_path 非空（无路径无法 organize）
      5. hash 字段存在且非空

    返回原始 torrent dict 子集（caller 用 hash / content_path / name / category）。
    """
    if not category_whitelist:
        return []
    whitelist_set = {c for c in category_whitelist if c}
    if not whitelist_set:
        return []
    result: list[dict] = []
    for t in torrents:
        if not (t.get("hash") or "").strip():
            continue
        if t.get("progress", 0) != 1.0:
            continue
        if t.get("state") not in COMPLETED_STATES:
            continue
        if (t.get("category") or "") not in whitelist_set:
            continue
        if not (t.get("content_path") or "").strip():
            continue
        result.append(t)
    return result


def filter_unprocessed_hashes(
    conn: sqlite3.Connection,
    hashes: list[str],
) -> set[str]:
    """从 hash 列表筛出 auto_organize_runs 还需要处理的（不存在或 status='pending'）.

    跳过 terminal 状态（succeeded / failed / skipped_*）和 organizing（已在跑）.
    返 set 让 caller 用 set 操作快速判定。
    """
    if not hashes:
        return set()
    placeholders = ",".join("?" * len(hashes))
    rows = conn.execute(
        f"SELECT qbit_hash, status FROM auto_organize_runs WHERE qbit_hash IN ({placeholders})",
        hashes,
    ).fetchall()
    # 已存在且非 pending 的 hash 跳过（terminal / organizing）
    skip = {r["qbit_hash"] for r in rows if r["status"] != "pending"}
    return set(hashes) - skip


# ── row lifecycle ───


def claim_pending_run(
    conn: sqlite3.Connection,
    qbit_hash: str,
    *,
    category: str | None,
    torrent_name: str,
    content_path: str,
) -> bool:
    """新种子第一次进入 pipeline：INSERT pending row.

    幂等：已存在 row（任何 status）→ False。否则 INSERT pending → True。
    用 INSERT OR IGNORE 单语句 + rowcount 判定（比 SELECT-then-INSERT 少一个 race window）。
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO auto_organize_runs(qbit_hash,category,torrent_name,"
        "content_path,status,attempts,created_at) VALUES(?,?,?,?,?,?,?)",
        (qbit_hash, category, torrent_name, content_path, "pending", 0, int(time.time())),
    )
    conn.commit()
    return cur.rowcount > 0


def mark_organizing(
    conn: sqlite3.Connection,
    qbit_hash: str,
    *,
    action_id: str | None,
) -> bool:
    """转 pending → organizing.

    Guarded UPDATE WHERE status='pending' — partial unique idx 兜底防并发 organizing.
    返 True 表示成功 claim；False 表示已 terminal / organizing / row 不存在。
    """
    now = int(time.time())
    try:
        cur = conn.execute(
            "UPDATE auto_organize_runs SET status='organizing', action_id=?, "
            "last_attempt_at=?, attempts=attempts+1 WHERE qbit_hash=? AND status='pending'",
            (action_id, now, qbit_hash),
        )
        conn.commit()
        return cur.rowcount > 0
    except sqlite3.IntegrityError:
        # partial unique 撞（罕见 race，第一道 module-level lock 应阻断）
        return False


def mark_terminal(
    conn: sqlite3.Connection,
    qbit_hash: str,
    *,
    status: str,
    action_id: str | None = None,
    files_succeeded: int | None = None,
    files_already_linked: int | None = None,
    files_failed: int | None = None,
    error: str | None = None,
) -> bool:
    """设置 row 为终态。记 completed_at + last_attempt_at + last_error.

    可重入：terminal → 同 status 重写仍 ok（idempotent，幂等冒烟用）.
    返 True 表示 row 存在并 update 成功。
    注意：attempts **不在这里增**（mark_organizing 已增过）— 避免双计数.
    """
    if status not in {"succeeded", "failed", "skipped_needs_identify",
                      "skipped_low_confidence", "skipped_unsupported"}:
        raise ValueError(f"mark_terminal: invalid terminal status {status!r}")
    now = int(time.time())
    cur = conn.execute(
        "UPDATE auto_organize_runs SET status=?, action_id=COALESCE(?, action_id), "
        "files_succeeded=?, files_already_linked=?, files_failed=?, "
        "last_error=?, last_attempt_at=?, completed_at=? WHERE qbit_hash=?",
        (status, action_id, files_succeeded, files_already_linked, files_failed,
         error, now, now, qbit_hash),
    )
    conn.commit()
    return cur.rowcount > 0


def mark_skipped_at_pending(
    conn: sqlite3.Connection,
    qbit_hash: str,
    *,
    status: str,
    error: str | None = None,
) -> bool:
    """专门给 confidence_gate / unsupported 判定用：从 pending 直接转 terminal skip，
    不经 organizing.

    UPDATE WHERE status='pending' — 防误覆盖已 organizing / terminal row.
    """
    if not status.startswith("skipped_"):
        raise ValueError(f"mark_skipped_at_pending: status must be skipped_*, got {status!r}")
    now = int(time.time())
    cur = conn.execute(
        "UPDATE auto_organize_runs SET status=?, last_error=?, last_attempt_at=?, "
        "completed_at=?, attempts=attempts+1 WHERE qbit_hash=? AND status='pending'",
        (status, error, now, now, qbit_hash),
    )
    conn.commit()
    return cur.rowcount > 0


# ── query / UI ───


def list_history(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
    status_filter: str | None = None,
) -> list[dict[str, Any]]:
    """UI 历史视图查询 helper.

    Ordering：进行中（pending / organizing）置顶；其余按 completed_at desc.
    """
    sql = "SELECT * FROM auto_organize_runs"
    params: list = []
    if status_filter:
        sql += " WHERE status = ?"
        params.append(status_filter)
    sql += (
        " ORDER BY CASE WHEN status IN ('organizing','pending') THEN 0 ELSE 1 END,"
        " COALESCE(completed_at, 0) DESC, created_at DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_run(conn: sqlite3.Connection, qbit_hash: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM auto_organize_runs WHERE qbit_hash=?", (qbit_hash,)
    ).fetchone()
    return dict(row) if row else None
