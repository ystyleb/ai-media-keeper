"""Phase 4B.3：批量 organize 后台 worker — 单 thread + progressive result_json
写 + abort flag。

设计抄 services/scanner.py：模块级 _active_lock + _active_action_id 保证
全局唯一 active organize（避免 N 个并发 worker 打爆 SSH ControlMaster +
DB 写争用 + qBit racing）。

跟 scanner 的差异：
  - 不维护独立 scan_items 表（organize items 已经全部装在 destructive_actions.
    payload_json 里）
  - 进度通过 destructive_action.update_running_result 写 result_json，前端
    polling /api/action/status 读这一字段
  - abort 信号用 module-level dict（spike 阶段 — 用户不在意 cross-restart 恢复，
    reaper 在 RUNNING_TIMEOUT_BY_KIND['organize']=1800s 后兜底）

[code-enforced single-worker constraint]（codex r1 BLOCKER 2 / IMP4 + r2 BLOCKER）：
此模块的 _active_lock / _abort_flags 都是**进程内**状态，gunicorn 多 worker
场景下两个进程会各自 start worker 互不感知，且 abort 信号可能落到错进程。
**app.py 启动时硬 enforce WORKER_COUNT==1**（多 worker 抛 RuntimeError 拒启动），
保证 module-level lock + abort flag 在跨请求间一致。
真要多 worker 时切换到 SQLite lease（destructive_actions 表加 worker_id 列
+ guarded UPDATE claim + abort 信号入 DB）— 留 Phase 4C 后置。

依赖注入：execute_one_item callable 接受 (payload_item, expected_metadata)，
返 dict result。这样 SSH / NFO write / qBit 副作用都解耦，单元测试 mock 即可。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

from . import destructive_action

logger = logging.getLogger(__name__)

# 全局唯一 active organize：避免 N 个并发 worker
_active_lock = threading.Lock()
_active_action_id: str | None = None
_active_thread: threading.Thread | None = None

# Abort 信号：module-level dict（spike 阶段够用；进程重启后由 reaper 兜底）
_abort_flags: dict[str, bool] = {}
_abort_flags_lock = threading.Lock()

# Per-item executor 签名：(payload_item, expected_metadata) -> result_dict
# 调用方注入；负责 SSH stat + mkdir + ln + verify + NFO write 副作用
ExecuteOneItem = Callable[[dict, dict | None], dict]


class ConcurrentOrganizeError(RuntimeError):
    """同时只允许一个 organize worker — 防 SSH ControlMaster 争用 + DB 写争用。"""


# ─── module-level state accessors ───


def get_active_action_id() -> str | None:
    with _active_lock:
        return _active_action_id


def is_aborted(action_id: str) -> bool:
    with _abort_flags_lock:
        return _abort_flags.get(action_id, False)


def request_abort(action_id: str) -> bool:
    """设 abort 信号。worker 下一个 item 边界检查后退出（不取消正在跑的 SSH）。

    返回是否成功设标志（重复调用幂等 True）。
    """
    with _abort_flags_lock:
        _abort_flags[action_id] = True
    return True


def _clear_abort(action_id: str) -> None:
    with _abort_flags_lock:
        _abort_flags.pop(action_id, None)


def _clear_active(action_id: str) -> None:
    """清模块级 active state — 仅清当前 action（防多 worker 误清）。"""
    global _active_action_id, _active_thread
    with _active_lock:
        if _active_action_id == action_id:
            _active_action_id = None
            _active_thread = None


# ─── public API ───


def start_organize_executor(
    *,
    db_path: Path | str,
    action_id: str,
    payload: dict[str, Any],
    execute_one_item: ExecuteOneItem,
    selected_indices: list[int] | None = None,
) -> None:
    """开 worker thread 跑 batch organize。

    Pre-condition：调用方必须先 atomic_consume 把 destructive_actions row 标
    status='running'（在 confirm 路由层做），然后传 action_id + payload 给本函数。

    Args:
        db_path: SQLite 路径
        action_id: status='running' 的 destructive_action row id
        payload: payload_json 解出的内容（含 items[]）
        execute_one_item: 调用方注入的 per-item executor。函数签名
            (item_dict, expected_metadata) → result_dict；
            返回 {src_path, status, ...}（status: succeeded / already_linked /
            failed），异常 worker 会自己 catch + 落 failed。
        selected_indices: optional — UI 勾选的 items[] index 子集；不在 set
            内的 index 标 'skipped_by_user'。None = 跑全部。
    """
    global _active_action_id, _active_thread
    with _active_lock:
        if _active_action_id is not None:
            raise ConcurrentOrganizeError(
                f"organize {_active_action_id!r} is already running; abort it first"
            )
        t = threading.Thread(
            target=_worker_main,
            args=(db_path, action_id, payload, execute_one_item, selected_indices),
            name=f"organize-worker-{action_id[:8]}",
            daemon=True,
        )
        _active_action_id = action_id
        _active_thread = t
        _clear_abort(action_id)  # 清旧 abort flag 防 stale
        # codex r2 IMP1: t.start() 抛错（如 "can't start new thread"）时清 lock 再抛，
        # 避免 active state 永远卡住 — finally _clear_active 不会被触发因为 thread
        # 没真正启动 → _worker_main 不会跑 finally。
        try:
            t.start()
        except Exception:
            _active_action_id = None
            _active_thread = None
            _clear_abort(action_id)
            raise
    logger.info(f"[organize_runner] started action_id={action_id} items={len(payload.get('items', []))}")


def _worker_main(
    db_path: Path | str,
    action_id: str,
    payload: dict[str, Any],
    execute_one_item: ExecuteOneItem,
    selected_indices: list[int] | None,
) -> None:
    """Worker thread 主循环：顺序处理 items，每个 item 完成后 progressive 写 result_json。

    codex r1 IMP2 修复：open_connection 移入 try 块，并把 conn=None 初始化在外面
    防止 open_connection 抛错时 active lock 永久卡住（finally 无条件 _clear_active）。

    codex r1 IMP7 修复：注释 fix — 实际上每 item 最多写 2 次 result_json（开始前
    写 current_item 给 polling 看 + 结束后写完成结果）。total ~2N commit per action。
    SQLite WAL + DEFERRED 下 N=500 增 ~50s commit 成本可接受。

    codex r2 IMP3 修复：selected_set 初始化移入 try 块（malformed indices 抛
    TypeError 时 finally 仍能清 active state）。
    """
    conn: sqlite3.Connection | None = None
    try:
        # codex r2 IMP3: 移进 try 块，malformed args 抛错时 finally 仍清 lock
        selected_set: set[int] | None = (
            set(selected_indices) if selected_indices is not None else None
        )
        items = payload.get("items", [])
        total = len(items)
        conn = destructive_action.open_connection(db_path)
        results: list[dict] = []
        status_counts: dict[str, int] = {}

        # 初始 progress：N items 全 pending
        # codex r2 IMP2: 每次 update_running_result 返 False 说明 row 已 terminal
        # （reaper 抢标 needs_manual_recovery / 别处 mark_terminal），立刻早退停
        # 后续副作用（hardlink / NFO write）— ownership 已丢失，继续执行就是孤儿写。
        still_owns = destructive_action.update_running_result(conn, action_id, {
            "items_total": total,
            "items_completed": 0,
            "current_item": None,
            "status_counts": {},
            "items": [],
        })
        if not still_owns:
            logger.warning(
                f"[organize_runner] action {action_id} ownership lost before any item "
                f"(reaper intervened?); aborting worker"
            )
            return

        for idx, item in enumerate(items):
            src_path = item.get("src_path")

            # 1) abort 优先于一切：用户中止 → 不再跑后续 items
            if is_aborted(action_id):
                results.append({
                    "src_path": src_path, "status": "skipped_by_abort",
                    "index": idx,
                })
                status_counts["skipped_by_abort"] = status_counts.get("skipped_by_abort", 0) + 1
                continue

            # 2) UI 未勾选 → 标 skipped_by_user
            if selected_set is not None and idx not in selected_set:
                results.append({
                    "src_path": src_path, "status": "skipped_by_user",
                    "index": idx,
                })
                status_counts["skipped_by_user"] = status_counts.get("skipped_by_user", 0) + 1
                continue

            # 3) progressive：先写 current_item 让 polling 看到「正在跑哪个」
            # codex r2 IMP2: ownership guard — 同上，丢失 ownership 就早退
            still_owns = destructive_action.update_running_result(conn, action_id, {
                "items_total": total,
                "items_completed": idx,
                "current_item": src_path,
                "status_counts": dict(status_counts),
                "items": list(results),
            })
            if not still_owns:
                logger.warning(
                    f"[organize_runner] action {action_id} ownership lost at item {idx} "
                    f"({src_path!r}); aborting worker"
                )
                return

            # 4) 真跑 item（异常被 catch → failed）
            try:
                r = execute_one_item(item, item.get("metadata_snapshot"))
                # 防御：执行器返回缺 status / src_path → 补字段
                r.setdefault("src_path", src_path)
                r.setdefault("status", "failed")
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[organize_runner] item {src_path!r} crashed")
                r = {
                    "src_path": src_path, "status": "failed",
                    "reason": f"executor_crashed: {type(e).__name__}: {e}",
                }
            r["index"] = idx
            results.append(r)
            st = r["status"]
            status_counts[st] = status_counts.get(st, 0) + 1

            # 5) 跑完一个就 commit 一次（用户 polling 即时看到 +1）
            # 完成阶段也 guard：如果 reaper 抢标了，我们已经做了这次副作用，但下一个 item 不再做
            destructive_action.update_running_result(conn, action_id, {
                "items_total": total,
                "items_completed": idx + 1,
                "current_item": src_path,
                "status_counts": dict(status_counts),
                "items": list(results),
            })

        # 终态：组装 result + 写 succeeded（partial failure 不 mark failed —
        # 契约 #7：整 action succeeded，per-item status 在 result 里）
        final_result = {
            "items": results,
            "status_counts": status_counts,
            "items_total": total,
            "items_completed": sum(status_counts.values()),
            "total_succeeded": status_counts.get("succeeded", 0),
            "total_already_linked": status_counts.get("already_linked", 0),
            "total_failed": status_counts.get("failed", 0),
            "total_skipped_by_user": status_counts.get("skipped_by_user", 0),
            "total_skipped_by_abort": status_counts.get("skipped_by_abort", 0),
        }

        # recovery_hint 辅助 audit：纯成功不写；部分失败 / aborted 写解释串
        recovery_hint: str | None = None
        if status_counts.get("skipped_by_abort"):
            recovery_hint = (
                f"aborted_by_user: {final_result['total_succeeded']} succeeded, "
                f"{status_counts['skipped_by_abort']} not processed"
            )
        elif final_result["total_failed"] > 0:
            recovery_hint = (
                f"partial_failure: {final_result['total_succeeded']} succeeded, "
                f"{final_result['total_failed']} failed"
            )

        # codex r1 IMP3: 用 mark_terminal_if_running guard，避免覆盖 reaper 已写入
        # 的 needs_manual_recovery / 其他进程已 mark 的 terminal 状态
        flipped = destructive_action.mark_terminal_if_running(
            conn, action_id, status="succeeded",
            result=final_result, error=None,
        )
        if not flipped:
            logger.warning(
                f"[organize_runner] action {action_id} already terminal at finish "
                f"(reaper may have intervened); not overwriting"
            )
        elif recovery_hint:
            conn.execute(
                "UPDATE destructive_actions SET recovery_hint = ? WHERE action_id = ?",
                (recovery_hint, action_id),
            )
            conn.commit()
        logger.info(f"[organize_runner] action {action_id} done: {status_counts}")

    except Exception as e:  # noqa: BLE001
        logger.exception(f"[organize_runner] action {action_id} worker crashed")
        if conn is not None:
            try:
                destructive_action.mark_terminal_if_running(
                    conn, action_id, status="failed", result=None,
                    error=f"worker_crashed: {type(e).__name__}: {e}",
                )
            except sqlite3.Error:
                pass
    finally:
        # codex r1 IMP2: 无条件清 active lock，否则 open_connection 抛错时 lock 永久卡住
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        _clear_active(action_id)
        _clear_abort(action_id)


# ─── status query helper ───


def get_organize_status(conn: sqlite3.Connection, action_id: str) -> dict | None:
    """读 destructive_actions row 给前端 polling。

    返回 dict（含 status / result_json 解析后 / error / kind / 时间戳）；
    row 不存在返 None。
    """
    row = conn.execute(
        "SELECT * FROM destructive_actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    if row is None:
        return None

    import json as _json
    result: dict | None = None
    if row["result_json"]:
        try:
            result = _json.loads(row["result_json"])
        except Exception:
            result = None

    return {
        "action_id": action_id,
        "kind": row["kind"],
        "status": row["status"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "expires_at": row["expires_at"],
        "error": row["error"],
        "recovery_hint": row["recovery_hint"],
        "result": result,
        # 便利字段：从 result 里抽 progress 顶层，省前端 nesting
        "items_total": (result or {}).get("items_total"),
        "items_completed": (result or {}).get("items_completed"),
        "current_item": (result or {}).get("current_item"),
        "status_counts": (result or {}).get("status_counts"),
    }
