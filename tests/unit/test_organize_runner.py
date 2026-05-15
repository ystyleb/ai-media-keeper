"""Phase 4B.3: organize_runner 单元测试。

覆盖 background worker 行为：顺序处理 items / progressive result_json 写 /
abort flag / selected_indices skip / 全局 lock / 异常 catch / partial failure
不 mark action failed。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from services import destructive_action, organize_runner

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


# ── helpers ───────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_runner_state():
    """每个测试前后清模块级状态，避免测试相互污染。"""
    organize_runner._clear_active("anything")
    with organize_runner._active_lock:
        organize_runner._active_action_id = None
        organize_runner._active_thread = None
    with organize_runner._abort_flags_lock:
        organize_runner._abort_flags.clear()
    yield
    with organize_runner._active_lock:
        organize_runner._active_action_id = None
        organize_runner._active_thread = None
    with organize_runner._abort_flags_lock:
        organize_runner._abort_flags.clear()


@pytest.fixture
def db_path(tmp_path):
    p = tmp_path / "actions.db"
    c = destructive_action.open_connection(p)
    destructive_action.init_schema(c, SCHEMA_PATH)
    c.close()
    return p


def _seed_running_action(db_path, n_items=3):
    """在 DB 里造一个 status='running' 的 organize action，返回 (action_id, payload)。"""
    items = [
        {
            "src_path": f"/dl/m{i}.mkv",
            "computed_plan": {"dst_path": f"/media/movies/M{i}/m{i}.mkv"},
            "src_snapshot": {"inode": 100 + i, "size_bytes": 1024, "mtime": 1000},
            "media_type": "movie",
            "metadata_snapshot": {
                "tmdb_id": f"{i}", "title": f"M{i}", "year": 2020,
                "media_type": "movie", "season_number": None, "episode_number": None,
            },
        }
        for i in range(n_items)
    ]
    payload = {"kind": "organize", "items": items, "snapshot": {"captured_at": 1000}}

    secret = b"a" * 64
    c = destructive_action.open_connection(db_path)
    try:
        res = destructive_action.create_preview(
            c, kind="organize", payload=payload, server_secret=secret,
        )
        # consume → running
        pre = c.execute(
            "SELECT payload_hash FROM destructive_actions WHERE action_id = ?",
            (res.action_id,),
        ).fetchone()
        destructive_action._atomic_consume(c, res.action_id, pre["payload_hash"], int(time.time()))
    finally:
        c.close()
    return res.action_id, payload


def _wait_until_terminal(db_path, action_id, timeout=5.0):
    """polling 等到 destructive_action.status ∈ terminal states。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        c = destructive_action.open_connection(db_path)
        try:
            row = c.execute(
                "SELECT status FROM destructive_actions WHERE action_id = ?",
                (action_id,),
            ).fetchone()
        finally:
            c.close()
        if row and row["status"] in ("succeeded", "failed", "needs_manual_recovery"):
            return row["status"]
        time.sleep(0.05)
    raise TimeoutError(f"action {action_id} did not reach terminal in {timeout}s")


# ── tests ─────────────────────────────────────────────────────────


def test_runner_processes_all_items_in_order(db_path):
    """3 items mock executor 都返 succeeded → action.status=succeeded + result.items 顺序对。"""
    action_id, payload = _seed_running_action(db_path, n_items=3)
    seen = []

    def fake_exec(item, expected):
        seen.append(item["src_path"])
        return {"src_path": item["src_path"], "status": "succeeded",
                "dst_path": item["computed_plan"]["dst_path"]}

    organize_runner.start_organize_executor(
        db_path=db_path, action_id=action_id, payload=payload,
        execute_one_item=fake_exec,
    )
    _wait_until_terminal(db_path, action_id)

    assert seen == ["/dl/m0.mkv", "/dl/m1.mkv", "/dl/m2.mkv"]

    c = destructive_action.open_connection(db_path)
    try:
        info = organize_runner.get_organize_status(c, action_id)
    finally:
        c.close()
    assert info["status"] == "succeeded"
    assert info["result"]["total_succeeded"] == 3
    assert info["result"]["status_counts"] == {"succeeded": 3}


def test_runner_progressive_result_json_updates_during_run(db_path):
    """跑期间 polling 看到 items_completed 逐渐增长（progressive write 不是终态写一次）."""
    action_id, payload = _seed_running_action(db_path, n_items=5)
    gate = threading.Event()
    seen_progress = []

    def fake_exec(item, expected):
        # 阻塞 worker 让 polling 能捕获中间态
        c = destructive_action.open_connection(db_path)
        try:
            info = organize_runner.get_organize_status(c, action_id)
            seen_progress.append(info["items_completed"])
        finally:
            c.close()
        gate.wait(0.1)  # 模拟 SSH 慢
        return {"src_path": item["src_path"], "status": "succeeded"}

    organize_runner.start_organize_executor(
        db_path=db_path, action_id=action_id, payload=payload,
        execute_one_item=fake_exec,
    )
    _wait_until_terminal(db_path, action_id, timeout=10)

    # 看到至少 2 个不同的中间 items_completed 值（不是一次写全 5）
    assert len(set(seen_progress)) >= 2
    assert max(seen_progress) >= 1  # 至少看到过 1
    assert max(seen_progress) < 5    # 没看到 5（终态写在 _mark_terminal）


def test_runner_abort_skips_remaining_items(db_path):
    """abort 在第 2 item 触发 → item 3..5 都标 skipped_by_abort，action succeeded with partial."""
    action_id, payload = _seed_running_action(db_path, n_items=5)
    call_count = {"n": 0}

    def fake_exec(item, expected):
        call_count["n"] += 1
        if call_count["n"] == 2:
            organize_runner.request_abort(action_id)
        return {"src_path": item["src_path"], "status": "succeeded"}

    organize_runner.start_organize_executor(
        db_path=db_path, action_id=action_id, payload=payload,
        execute_one_item=fake_exec,
    )
    _wait_until_terminal(db_path, action_id)

    c = destructive_action.open_connection(db_path)
    try:
        info = organize_runner.get_organize_status(c, action_id)
    finally:
        c.close()
    counts = info["result"]["status_counts"]
    assert counts.get("succeeded") == 2
    assert counts.get("skipped_by_abort") == 3
    assert info["status"] == "succeeded"  # 部分失败不 mark failed
    # recovery_hint 应该提示 aborted
    assert info["recovery_hint"] is not None
    assert "abort" in info["recovery_hint"].lower()


def test_runner_selected_indices_skips_unselected(db_path):
    """selected_indices=[0,2] → item 1 标 skipped_by_user，items 0+2 跑。"""
    action_id, payload = _seed_running_action(db_path, n_items=3)
    seen = []

    def fake_exec(item, expected):
        seen.append(item["src_path"])
        return {"src_path": item["src_path"], "status": "succeeded"}

    organize_runner.start_organize_executor(
        db_path=db_path, action_id=action_id, payload=payload,
        execute_one_item=fake_exec, selected_indices=[0, 2],
    )
    _wait_until_terminal(db_path, action_id)

    assert seen == ["/dl/m0.mkv", "/dl/m2.mkv"]  # m1 跳了
    c = destructive_action.open_connection(db_path)
    try:
        info = organize_runner.get_organize_status(c, action_id)
    finally:
        c.close()
    counts = info["result"]["status_counts"]
    assert counts["succeeded"] == 2
    assert counts["skipped_by_user"] == 1


def test_runner_global_lock_rejects_second_organize(db_path):
    """同时只允许一个 organize worker — 第二个 start 抛 ConcurrentOrganizeError。"""
    action_id_1, payload_1 = _seed_running_action(db_path, n_items=3)
    action_id_2, payload_2 = _seed_running_action(db_path, n_items=3)

    gate = threading.Event()

    def slow_exec(item, expected):
        gate.wait(2.0)
        return {"src_path": item["src_path"], "status": "succeeded"}

    organize_runner.start_organize_executor(
        db_path=db_path, action_id=action_id_1, payload=payload_1,
        execute_one_item=slow_exec,
    )

    with pytest.raises(organize_runner.ConcurrentOrganizeError):
        organize_runner.start_organize_executor(
            db_path=db_path, action_id=action_id_2, payload=payload_2,
            execute_one_item=slow_exec,
        )

    gate.set()  # 让第一个完成
    _wait_until_terminal(db_path, action_id_1, timeout=10)


def test_runner_executor_exception_marks_item_failed(db_path):
    """executor 抛异常 → item 标 failed + reason='executor_crashed: ...'，
    其它 items 继续跑（不打断 batch）."""
    action_id, payload = _seed_running_action(db_path, n_items=3)

    def fake_exec(item, expected):
        if "m1.mkv" in item["src_path"]:
            raise RuntimeError("boom!")
        return {"src_path": item["src_path"], "status": "succeeded"}

    organize_runner.start_organize_executor(
        db_path=db_path, action_id=action_id, payload=payload,
        execute_one_item=fake_exec,
    )
    _wait_until_terminal(db_path, action_id)

    c = destructive_action.open_connection(db_path)
    try:
        info = organize_runner.get_organize_status(c, action_id)
    finally:
        c.close()
    counts = info["result"]["status_counts"]
    assert counts["succeeded"] == 2
    assert counts["failed"] == 1
    # action 整体仍 succeeded（partial failure 不 abort action）
    assert info["status"] == "succeeded"
    # 失败 item 的 reason 包含 crash 提示
    failed = [it for it in info["result"]["items"] if it["status"] == "failed"]
    assert len(failed) == 1
    assert "boom" in failed[0]["reason"]
    # recovery_hint 提示 partial_failure
    assert "partial_failure" in (info["recovery_hint"] or "")


def test_runner_clears_active_state_after_completion(db_path):
    """worker 完成后 get_active_action_id 返 None（允许下次 start）."""
    action_id, payload = _seed_running_action(db_path, n_items=2)

    organize_runner.start_organize_executor(
        db_path=db_path, action_id=action_id, payload=payload,
        execute_one_item=lambda it, _md: {"src_path": it["src_path"], "status": "succeeded"},
    )
    _wait_until_terminal(db_path, action_id)
    time.sleep(0.05)  # 让 finally 块完成
    assert organize_runner.get_active_action_id() is None


def test_runner_status_endpoint_returns_none_for_missing_action(db_path):
    c = destructive_action.open_connection(db_path)
    try:
        info = organize_runner.get_organize_status(c, "nonexistent")
    finally:
        c.close()
    assert info is None


def test_runner_handles_action_with_zero_items(db_path):
    """空 items[]（不应发生但防御）→ worker 直接终态 succeeded with empty status_counts。"""
    action_id, _ = _seed_running_action(db_path, n_items=0)
    payload = {"kind": "organize", "items": [], "snapshot": {"captured_at": 1000}}

    organize_runner.start_organize_executor(
        db_path=db_path, action_id=action_id, payload=payload,
        execute_one_item=lambda it, _md: {"src_path": "n/a", "status": "succeeded"},
    )
    _wait_until_terminal(db_path, action_id)

    c = destructive_action.open_connection(db_path)
    try:
        info = organize_runner.get_organize_status(c, action_id)
    finally:
        c.close()
    assert info["status"] == "succeeded"
    assert info["result"]["items_total"] == 0
    assert info["result"]["total_succeeded"] == 0


# ── codex r2 修复测试 ──


def test_runner_aborts_when_reaper_intervenes_mid_run(db_path):
    """codex r2 IMP2: reaper 抢标 needs_manual_recovery → worker 下一 item
    update_running_result 返 False → 早退，后续 items 不执行 (no 副作用泄漏)."""
    action_id, payload = _seed_running_action(db_path, n_items=5)
    call_count = {"n": 0}

    def fake_exec(item, expected):
        call_count["n"] += 1
        # 在第 2 个 item 执行前，模拟 reaper 标 terminal
        if call_count["n"] == 2:
            c = destructive_action.open_connection(db_path)
            try:
                c.execute(
                    "UPDATE destructive_actions "
                    "SET status='needs_manual_recovery', recovery_hint='test_reaper' "
                    "WHERE action_id = ?", (action_id,),
                )
                c.commit()
            finally:
                c.close()
        return {"src_path": item["src_path"], "status": "succeeded"}

    organize_runner.start_organize_executor(
        db_path=db_path, action_id=action_id, payload=payload,
        execute_one_item=fake_exec,
    )
    # 等 worker 真退出（不能直接 _wait_until_terminal 因为 terminal 已写）
    time.sleep(0.5)

    # call_count <= 2（reaper 介入后立刻早退，3..5 不执行）
    assert call_count["n"] <= 2

    c = destructive_action.open_connection(db_path)
    try:
        row = c.execute(
            "SELECT status FROM destructive_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
    finally:
        c.close()
    # status 仍是 reaper 标的，不被 worker 覆盖
    assert row["status"] == "needs_manual_recovery"


def test_runner_malformed_selected_indices_does_not_leak_active_lock(db_path):
    """codex r2 IMP3: selected_indices=set([不可哈希]) 抛 TypeError 时 finally
    仍能清 active state（不卡 lock）。"""
    action_id, payload = _seed_running_action(db_path, n_items=2)

    # 传一个不能 set() 化的对象触发 worker 内部 TypeError
    class _BadList(list):
        def __iter__(self):
            raise TypeError("intentional iter failure")
    bad_indices = _BadList([0])

    organize_runner.start_organize_executor(
        db_path=db_path, action_id=action_id, payload=payload,
        execute_one_item=lambda it, _md: {"src_path": it["src_path"], "status": "succeeded"},
        selected_indices=bad_indices,
    )
    # 等 worker 自然结束（其内部 set() 会抛 → finally 清 active）
    time.sleep(0.5)
    # 关键 invariant：active state 不应卡住
    assert organize_runner.get_active_action_id() is None
