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

import json
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
        # qBit progress 是 float（JSON round-trip 可能 1.0000001 / 0.9999999）。
        # 用 >= 0.999 而非 == 1.0 防漏（B2 codex BLOCKER）；state ∈ COMPLETED_STATES
        # 是 secondary 守门，已排掉 downloading / error / missingFiles。
        if t.get("progress", 0) < 0.999:
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


VALID_STATUSES = frozenset({
    "pending", "organizing", "succeeded", "failed",
    "skipped_needs_identify", "skipped_low_confidence", "skipped_unsupported",
})


def list_history(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    offset: int = 0,
    status_filter: str | None = None,
) -> list[dict[str, Any]]:
    """UI 历史视图查询 helper.

    Ordering：进行中（pending / organizing）置顶；其余按 completed_at desc.

    Raises ValueError if status_filter is not in VALID_STATUSES (codex r1 N3).
    """
    if status_filter is not None and status_filter not in VALID_STATUSES:
        raise ValueError(
            f"invalid status_filter {status_filter!r}; must be one of {sorted(VALID_STATUSES)}"
        )
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


# ── 4C.2 confidence gate ───

# 支持自动 organize 的 media_type；其余（unknown / extra / ...）→ skipped_unsupported
SUPPORTED_MEDIA_TYPES = frozenset({"movie", "tv"})


def evaluate_confidence_gate(
    conn: sqlite3.Connection,
    paths: list[str],
    *,
    threshold: float,
) -> dict[str, Any]:
    """检查 paths 是否**全部**满足 confidence 门槛 → 决定能否自动 organize.

    优先级（fail-fast，任一不满足整体 fail）：
      1. 任一 path 在 metadata_cache 没 row / media_type=None → needs_identify
         (user 必须先去 file 视图手动识别)
      2. 任一 path media_type ∉ {'movie','tv'} → unsupported
         (无法 organize 到媒体库，如 sample / extra / 未知格式)
      3. 任一 path metadata_confidence < threshold → low_confidence
         (LLM 识别可信度不够，user 必须先手动确认)
      4. 全过 → pass

    注意：metadata_confidence is None 视为 0（None ≠ "未识别"——这是 LLM 不返置信度的
    极少 case，按谨慎态度 fail）。

    返回:
      {
        "status": "pass" | "skipped_needs_identify" | "skipped_low_confidence" | "skipped_unsupported",
        "reason": str (人类可读),
        "blockers": [{"path": str, "reason": str, "confidence": float|None,
                      "media_type": str|None}],  # 仅在非 pass 时填充
        "checked_count": int,
      }

    [code-enforced 契约 #5 边界]：confidence ≥ threshold + media_type ∈ supported
    是 user 通过 (a) 配 categories whitelist (b) 配 confidence threshold 两层授权后
    AI 才能 destructive 的硬条件；任一不满足 fail-fast，AI 不主动越界。
    """
    if not paths:
        return {
            "status": "skipped_needs_identify",
            "reason": "no video files found in torrent content path",
            "blockers": [],
            "checked_count": 0,
        }

    # 一次 batch 查（4C.1 / Phase 4B 已有 get_many_by_path 500-chunk SQL IN）
    # 避免循环 import：lazy import metadata_cache
    from . import metadata_cache  # noqa: PLC0415

    cache_map = metadata_cache.get_many_by_path(conn, paths)

    needs_identify: list[dict] = []
    unsupported: list[dict] = []
    low_confidence: list[dict] = []

    for p in paths:
        entry = cache_map.get(p)
        if entry is None:
            needs_identify.append({
                "path": p, "reason": "no cache row", "confidence": None,
                "media_type": None,
            })
            continue
        cached, _status = entry  # (CachedMetadata, freshness_label)
        if cached is None or cached.media_type is None:
            needs_identify.append({
                "path": p, "reason": "cache exists but media_type is None",
                "confidence": cached.metadata_confidence if cached else None,
                "media_type": None,
            })
            continue
        if cached.media_type not in SUPPORTED_MEDIA_TYPES:
            unsupported.append({
                "path": p,
                "reason": f"media_type={cached.media_type!r} not in {sorted(SUPPORTED_MEDIA_TYPES)}",
                "confidence": cached.metadata_confidence,
                "media_type": cached.media_type,
            })
            continue
        conf = cached.metadata_confidence
        if conf is None or conf < threshold:
            low_confidence.append({
                "path": p,
                "reason": f"confidence {conf!r} < threshold {threshold}",
                "confidence": conf,
                "media_type": cached.media_type,
            })
            continue

    # 优先级 fail-fast：needs_identify > unsupported > low_confidence > pass
    # （needs_identify 最致命，user 一无所知；其他至少有部分信息）
    if needs_identify:
        return {
            "status": "skipped_needs_identify",
            "reason": (
                f"{len(needs_identify)}/{len(paths)} file(s) not yet identified; "
                f"user must identify first (open file browser → 识别)"
            ),
            "blockers": needs_identify,
            "checked_count": len(paths),
        }
    if unsupported:
        return {
            "status": "skipped_unsupported",
            "reason": (
                f"{len(unsupported)}/{len(paths)} file(s) have unsupported "
                f"media_type (not movie/tv)"
            ),
            "blockers": unsupported,
            "checked_count": len(paths),
        }
    if low_confidence:
        return {
            "status": "skipped_low_confidence",
            "reason": (
                f"{len(low_confidence)}/{len(paths)} file(s) below confidence "
                f"threshold {threshold}; user must manually confirm"
            ),
            "blockers": low_confidence,
            "checked_count": len(paths),
        }
    return {
        "status": "pass",
        "reason": f"all {len(paths)} file(s) meet threshold {threshold}",
        "blockers": [],
        "checked_count": len(paths),
    }


# ── 4C.3 dispatch orchestrator ───
# Callback injection 模式：service 层不知道 SSH / Flask / organize_runner 存在；
# caller (app.py cron) 注入 SSH list 函数 + organize 触发函数。这样:
#   - service 模块 framework-agnostic + 单测 mock 直观
#   - "组装 preview payload + atomic_consume + start_organize_executor" 逻辑保留在 app.py
#     避免 Phase 4B existing code 大重构
#
# 返回 dict.action 枚举（caller 统计 / log）:
#   started               — organize worker 已起，mark_organizing 成功
#   skip_existing_run     — 已有 terminal/organizing row，跳过
#   skipped               — confidence gate fail / unsupported / list_paths_failed
#   locked                — organize_runner active lock 冲突，留 pending 让下周期重试
#   errored               — build/start callback 报错
#   claim_lost            — 极少 race，mark_organizing 未命中（row 不再 pending）


def dispatch_one(
    conn: sqlite3.Connection,
    torrent: dict,
    *,
    list_video_paths_fn,                   # callable(content_path: str) -> list[str]
    confidence_threshold: float,
    build_and_start_organize_fn,           # callable(paths, qbit_hash) -> {action_id, status, error?}
) -> dict[str, Any]:
    """对一个 completed torrent 触发自动 organize flow（不阻塞，async worker）.

    Args:
        torrent: 单 torrent dict（含 hash / name / category / content_path）— 来自
                 list_completed_torrents 输出.
        list_video_paths_fn: caller 注入；接 content_path 返该路径下视频文件 list.
                             失败时抛任意 Exception，dispatch_one 会 catch 标 skipped.
        confidence_threshold: 走 evaluate_confidence_gate 的门槛.
        build_and_start_organize_fn: caller 注入；接 (paths, qbit_hash) 做
                                      organize preview payload 构造 + atomic consume +
                                      start_organize_executor。**必须返**:
                                      `{"action_id": str | None, "status":
                                        "started" | "locked" | "error",
                                        "error": str | None}`.
    """
    qbit_hash = torrent.get("hash") or ""
    name = (torrent.get("name") or "").strip()
    category = (torrent.get("category") or None)
    content_path = (torrent.get("content_path") or "").strip()
    if not (qbit_hash and content_path):
        return {"action": "skipped", "status": "skipped_unsupported",
                "qbit_hash": qbit_hash,
                "reason": "torrent missing hash or content_path"}

    # 1. claim pending row
    is_new = claim_pending_run(
        conn, qbit_hash, category=category,
        torrent_name=name, content_path=content_path,
    )
    if not is_new:
        existing = get_run(conn, qbit_hash)
        if existing and existing["status"] != "pending":
            return {"action": "skip_existing_run",
                    "qbit_hash": qbit_hash,
                    "current_status": existing["status"]}
        # status='pending'（cron 之前 row 留下 / locked 重试）→ 继续走

    # 2. 列视频文件
    try:
        paths = list_video_paths_fn(content_path)
    except Exception as e:  # noqa: BLE001
        mark_skipped_at_pending(
            conn, qbit_hash, status="skipped_unsupported",
            error=f"list_paths_failed: {type(e).__name__}: {e}",
        )
        return {"action": "skipped", "status": "skipped_unsupported",
                "qbit_hash": qbit_hash, "error": str(e)}

    # 3. confidence gate
    gate = evaluate_confidence_gate(conn, paths, threshold=confidence_threshold)
    if gate["status"] != "pass":
        mark_skipped_at_pending(
            conn, qbit_hash, status=gate["status"], error=gate["reason"],
        )
        return {"action": "skipped", "status": gate["status"],
                "qbit_hash": qbit_hash, "reason": gate["reason"],
                "blockers": gate.get("blockers", [])}

    # 4. build preview + start worker (caller-injected)
    try:
        result = build_and_start_organize_fn(paths, qbit_hash)
    except Exception as e:  # noqa: BLE001
        mark_skipped_at_pending(
            conn, qbit_hash, status="skipped_unsupported",
            error=f"build_or_start_raised: {type(e).__name__}: {e}",
        )
        return {"action": "errored", "qbit_hash": qbit_hash,
                "error": f"{type(e).__name__}: {e}"}

    action_id = result.get("action_id")
    if result.get("status") == "locked":
        # 留 pending 让下周期 cron 重试（lock 是临时状态）
        return {"action": "locked", "qbit_hash": qbit_hash}
    if result.get("status") == "error":
        # callback 自己识别为永久错误（unsupported / preview build failed）
        mark_skipped_at_pending(
            conn, qbit_hash, status="skipped_unsupported",
            error=f"build_or_start_failed: {result.get('error') or 'unknown'}",
        )
        return {"action": "errored", "qbit_hash": qbit_hash,
                "error": result.get("error")}

    # 5. claim organizing（worker 已 started → pending → organizing）
    # codex r1 B1 fix: 这步失败 = race（cron 双触发 / user 抢标 / reaper 抢标）。
    # worker 已经在跑，必须把它对应的 destructive_actions row 标 failed，否则:
    #   - destructive_actions.status=running（worker 跑）
    #   - auto_organize_runs.status=pending（claim 失败留下的）
    #   下周期 cron 看 row 是 pending → 再 dispatch_one → 又起一个 worker → 双副作用
    # 我们 mark destructive_action 失败让 reconcile 下周期看到 failed → 不会再起。
    # 注意：单飞 lock 已经在 build_and_start 里 acquired，worker 真在跑，
    # 此处 _mark_terminal 让 worker 完成后看到 status≠'running' 自我 abort
    # (mark_terminal_if_running guard 已在 organize_runner._worker_main 处理)
    ok = mark_organizing(conn, qbit_hash, action_id=action_id)
    if not ok:
        from . import destructive_action as _da  # noqa: PLC0415
        try:
            _da._mark_terminal(
                conn, action_id, status="failed", result=None,
                error=f"auto_organize_claim_lost: row no longer pending (race)",
            )
        except Exception:
            # 不让 cleanup 抛错掩盖根因；reaper 兜底
            pass
        return {"action": "claim_lost", "qbit_hash": qbit_hash,
                "action_id": action_id}
    return {"action": "started", "qbit_hash": qbit_hash, "action_id": action_id}


def reconcile_organizing_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """同步 organizing → terminal：扫所有 status='organizing' row 查对应 destructive_actions 终态.

    Cron 每周期跑一次（独立于 dispatch）：
      - destructive_actions.status='running' → 还在跑，跳过等下次
      - succeeded → auto_organize_runs mark_terminal(succeeded) + 抽 counts
      - failed / needs_manual_recovery → mark_terminal(failed) + error 串
      - action row 不存在（reaper 已清极端 case）→ mark_terminal(failed) + missing 错误

    Reaper 本身已托管 destructive_actions 卡死回收（RUNNING_TIMEOUT_BY_KIND['organize']=1800s），
    所以这里只做 "follow downstream truth"，不主动判超时.
    """
    rows = conn.execute(
        "SELECT qbit_hash, action_id FROM auto_organize_runs WHERE status='organizing'"
    ).fetchall()
    results: list[dict] = []
    for row in rows:
        qbit_hash = row["qbit_hash"]
        action_id = row["action_id"]
        if not action_id:
            mark_terminal(conn, qbit_hash, status="failed",
                          error="organizing row has no action_id (corrupted state)")
            results.append({"qbit_hash": qbit_hash, "synced_to": "failed",
                            "reason": "missing_action_id"})
            continue
        action_row = conn.execute(
            "SELECT status, result_json, error FROM destructive_actions WHERE action_id=?",
            (action_id,),
        ).fetchone()
        if action_row is None:
            mark_terminal(conn, qbit_hash, status="failed",
                          action_id=action_id,
                          error=f"destructive_action row missing: {action_id}")
            results.append({"qbit_hash": qbit_hash, "synced_to": "failed",
                            "reason": "action_row_missing"})
            continue
        a_status = action_row["status"]
        if a_status == "running":
            continue  # 还在跑，下个周期再 check
        # terminal: succeeded / failed / needs_manual_recovery
        try:
            result_data = (
                json.loads(action_row["result_json"]) if action_row["result_json"] else {}
            )
        except json.JSONDecodeError:
            result_data = {}
        if a_status == "succeeded":
            mark_terminal(
                conn, qbit_hash, status="succeeded", action_id=action_id,
                files_succeeded=result_data.get("total_succeeded", 0),
                files_already_linked=result_data.get("total_already_linked", 0),
                files_failed=result_data.get("total_failed", 0),
            )
            results.append({"qbit_hash": qbit_hash, "synced_to": "succeeded",
                            "action_id": action_id})
        else:
            mark_terminal(
                conn, qbit_hash, status="failed", action_id=action_id,
                files_succeeded=result_data.get("total_succeeded"),
                files_already_linked=result_data.get("total_already_linked"),
                files_failed=result_data.get("total_failed"),
                error=action_row["error"] or f"action_status={a_status}",
            )
            results.append({"qbit_hash": qbit_hash, "synced_to": "failed",
                            "action_id": action_id, "action_status": a_status})
    return results
