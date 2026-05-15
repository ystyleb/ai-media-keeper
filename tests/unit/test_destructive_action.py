"""单元测试：契约 #1 行为。

覆盖 plan v3 的 1.7 验收清单 13 项中可单测的部分：
- replay rejected (#4)
- tampered payload rejected (#3)
- rolling restart 持久 secret OK (#5)
- dev fallback 单 worker OK，多 worker 拒绝启动 (#6)
- legacy route 不在本测试覆盖（属 routes 层）
- snapshot mismatch 不在本测试覆盖（属 contract #2 层，由调用方传入 executor 实现）
- atomic consume 在并发下 rowcount=1 (#12)
- concurrent preview 同 payload (#11)
- crash recovery (#13)
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from services import destructive_action as da

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "db" / "schema.sql"


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "test.db"
    c = da.open_connection(db_path)
    da.init_schema(c, SCHEMA_PATH)
    yield c
    c.close()


@pytest.fixture
def secret():
    return b"a" * 64  # 测试固定 secret，模拟生产持久


@pytest.fixture
def payload():
    return {
        "kind": "delete",
        "candidates": [{"path": "/share/test/a.mkv"}],
        "snapshot": {
            "captured_at": 1_700_000_000,
            "items": [
                {"path": "/share/test/a.mkv", "inode": 12345, "size_bytes": 1024, "mtime": 1_699_000_000},
            ],
        },
    }


# ---------------- preview ---------------- #


def test_preview_creates_pending_row(conn, secret, payload):
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    assert res.kind == "delete"
    assert res.signed_token  # non-empty
    assert res.expires_at > 0

    row = conn.execute(
        "SELECT status, kind, payload_hash FROM destructive_actions WHERE action_id = ?",
        (res.action_id,),
    ).fetchone()
    assert row["status"] == "pending"
    assert row["kind"] == "delete"
    assert row["payload_hash"]


def test_preview_concurrent_same_payload_yields_distinct_ids(conn, secret, payload):
    """R2-I2：同 payload 多次 preview 拿不同 action_id。"""
    a = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    b = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    assert a.action_id != b.action_id
    # 但 payload_hash 应当相同
    rows = conn.execute(
        "SELECT payload_hash FROM destructive_actions WHERE action_id IN (?, ?)",
        (a.action_id, b.action_id),
    ).fetchall()
    assert rows[0]["payload_hash"] == rows[1]["payload_hash"]


def test_preview_rejects_unknown_kind(conn, secret, payload):
    with pytest.raises(ValueError):
        da.create_preview(
            conn, kind="not_a_real_kind", payload=payload, server_secret=secret
        )


# ---------------- confirm happy path ---------------- #


def test_confirm_happy_path_marks_succeeded(conn, secret, payload):
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    executed_with = {}

    def executor(p):
        executed_with["payload"] = p
        return {"freed_bytes": 1024, "files_removed": 1}

    out = da.confirm(
        conn,
        action_id=res.action_id,
        signed_token=res.signed_token,
        server_secret=secret,
        executor=executor,
    )
    assert out.status == "succeeded"
    assert out.result == {"freed_bytes": 1024, "files_removed": 1}
    assert executed_with["payload"]["snapshot"]["items"][0]["inode"] == 12345

    row = conn.execute(
        "SELECT status, completed_at, result_json FROM destructive_actions WHERE action_id = ?",
        (res.action_id,),
    ).fetchone()
    assert row["status"] == "succeeded"
    assert row["completed_at"] is not None
    assert "freed_bytes" in row["result_json"]


# ---------------- attack/error paths ---------------- #


def test_confirm_replay_rejected(conn, secret, payload):
    """第 4 项：重放同 token 必须拒绝。"""
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    da.confirm(conn, action_id=res.action_id, signed_token=res.signed_token,
               server_secret=secret, executor=lambda p: {})

    with pytest.raises(da.ActionAlreadyConsumed):
        da.confirm(conn, action_id=res.action_id, signed_token=res.signed_token,
                   server_secret=secret, executor=lambda p: {})


def test_confirm_tampered_token_keeps_action_pending(conn, secret, payload):
    """第 3 项：HMAC 验签失败 → 不消费 action_id（rollback 到 pending）。"""
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    bogus = "0" * 64

    with pytest.raises(da.ActionTokenInvalid):
        da.confirm(conn, action_id=res.action_id, signed_token=bogus,
                   server_secret=secret, executor=lambda p: {})

    # 关键：合法 token 仍然能 confirm（attacker 不能通过给假 token 永久占用 action_id）
    out = da.confirm(conn, action_id=res.action_id, signed_token=res.signed_token,
                     server_secret=secret, executor=lambda p: {"ok": True})
    assert out.status == "succeeded"


def test_confirm_with_different_secret_rejected(conn, secret, payload):
    """rolling restart 切到完全不同的 secret → 旧 token 拒绝（虽然生产推荐 persistent，
    但万一真 rotate 了 secret 必须拒绝旧 token）。"""
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    new_secret = b"b" * 64

    with pytest.raises(da.ActionTokenInvalid):
        da.confirm(conn, action_id=res.action_id, signed_token=res.signed_token,
                   server_secret=new_secret, executor=lambda p: {})


def test_confirm_expired_action_rejected(conn, secret, payload, monkeypatch):
    """expired 的 action 不能 confirm。"""
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    # 把 row 的 expires_at 调到过去
    conn.execute(
        "UPDATE destructive_actions SET expires_at = ? WHERE action_id = ?",
        (1, res.action_id),
    )
    conn.commit()

    with pytest.raises(da.ActionExpired):
        da.confirm(conn, action_id=res.action_id, signed_token=res.signed_token,
                   server_secret=secret, executor=lambda p: {})


def test_confirm_unknown_action_id(conn, secret):
    with pytest.raises(da.ActionNotFound):
        da.confirm(conn, action_id="does-not-exist", signed_token="x",
                   server_secret=secret, executor=lambda p: {})


def test_executor_exception_marks_failed(conn, secret, payload):
    """executor 抛异常 → status='failed' 落 error。"""
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)

    def boom(p):
        raise RuntimeError("simulated SSH failure")

    out = da.confirm(conn, action_id=res.action_id, signed_token=res.signed_token,
                     server_secret=secret, executor=boom)
    assert out.status == "failed"
    assert "simulated SSH failure" in out.error

    row = conn.execute(
        "SELECT status, error FROM destructive_actions WHERE action_id = ?",
        (res.action_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert "simulated SSH failure" in row["error"]


# ---------------- atomic consume / concurrency ---------------- #


def test_two_concurrent_confirms_only_one_wins(tmp_path, secret, payload):
    """R2-I1: atomic UPDATE 在并发下只允许一个赢家。

    用两个独立 connection 模拟两个 worker process。
    """
    db_path = tmp_path / "concurrent.db"
    setup = da.open_connection(db_path)
    da.init_schema(setup, SCHEMA_PATH)
    res = da.create_preview(setup, kind="delete", payload=payload, server_secret=secret)
    setup.close()

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker():
        c = da.open_connection(db_path)
        try:
            barrier.wait()
            out = da.confirm(
                c, action_id=res.action_id, signed_token=res.signed_token,
                server_secret=secret, executor=lambda p: {"who": threading.current_thread().name}
            )
            results.append(out)
        except Exception as e:
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, name=f"t{i}") for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 恰好一个 succeeded，另一个 ActionAlreadyConsumed
    assert len(results) == 1
    assert results[0].status == "succeeded"
    assert len(errors) == 1
    assert isinstance(errors[0], da.ActionAlreadyConsumed)


# ---------------- crash recovery ---------------- #


def test_reap_stuck_running_action(conn, secret, payload):
    """第 13 项：status='running' 超过 timeout → needs_manual_recovery。"""
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)

    # 模拟 confirm 到一半 crash：手工把状态改成 running 且 started_at 是远古
    very_old = 1
    conn.execute(
        """
        UPDATE destructive_actions
           SET status = 'running', consumed_at = ?, started_at = ?
         WHERE action_id = ?
        """,
        (very_old, very_old, res.action_id),
    )
    conn.commit()

    reaped = da.reap_stuck_actions(conn)
    assert reaped == 1

    row = conn.execute(
        "SELECT status, recovery_hint FROM destructive_actions WHERE action_id = ?",
        (res.action_id,),
    ).fetchone()
    assert row["status"] == "needs_manual_recovery"
    assert "running > 60" in row["recovery_hint"]


def test_reap_does_not_touch_recently_started(conn, secret, payload):
    """刚 confirm 的 action 不应被误 reap。"""
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    # 模拟 confirm 刚开始执行
    now = da._now()
    conn.execute(
        """
        UPDATE destructive_actions
           SET status = 'running', consumed_at = ?, started_at = ?
         WHERE action_id = ?
        """,
        (now, now, res.action_id),
    )
    conn.commit()

    reaped = da.reap_stuck_actions(conn)
    assert reaped == 0


def test_cleanup_expired_pending(conn, secret, payload):
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    conn.execute(
        "UPDATE destructive_actions SET expires_at = 1 WHERE action_id = ?",
        (res.action_id,),
    )
    conn.commit()

    cleaned = da.cleanup_expired_pending(conn)
    assert cleaned == 1

    row = conn.execute(
        "SELECT status, error FROM destructive_actions WHERE action_id = ?",
        (res.action_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert row["error"] == "expired_before_confirm"


# ---------------- server_secret loading ---------------- #


def test_load_server_secret_uses_env(monkeypatch):
    monkeypatch.setenv("NAS_ACTION_SIGNING_KEY", "x" * 64)
    s = da.load_server_secret(worker_count=4)
    assert s == ("x" * 64).encode("utf-8")


def test_load_server_secret_rejects_short_env(monkeypatch):
    monkeypatch.setenv("NAS_ACTION_SIGNING_KEY", "tooshort")
    with pytest.raises(da.ServerSecretMisconfigured):
        da.load_server_secret(worker_count=1)


def test_load_server_secret_dev_fallback_single_worker(monkeypatch, caplog):
    """没 env 没 config_path → 单 worker ephemeral + warning。"""
    monkeypatch.delenv("NAS_ACTION_SIGNING_KEY", raising=False)
    with caplog.at_level("WARNING"):
        s = da.load_server_secret(worker_count=1)
    assert len(s) > 32
    assert any("ephemeral" in r.message for r in caplog.records)


def test_load_server_secret_dev_fallback_multi_worker_refused(monkeypatch):
    """多 worker 没设 env 也没 config_path → 必须 raise。"""
    monkeypatch.delenv("NAS_ACTION_SIGNING_KEY", raising=False)
    with pytest.raises(da.ServerSecretMisconfigured):
        da.load_server_secret(worker_count=2)


def test_load_server_secret_config_file_first_time(monkeypatch, tmp_path):
    """config_path 不存在 → 自动生成 + 写文件 + chmod 600。"""
    monkeypatch.delenv("NAS_ACTION_SIGNING_KEY", raising=False)
    keyfile = tmp_path / ".signing_key"
    s = da.load_server_secret(worker_count=4, config_path=keyfile)
    assert len(s) >= 64
    assert keyfile.exists()
    # chmod 600 验证
    import stat as st
    mode = keyfile.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got 0o{mode:o}"


def test_load_server_secret_config_file_persistent_across_calls(monkeypatch, tmp_path):
    """已存在的 config_path → 第二次加载读同一个 secret。"""
    monkeypatch.delenv("NAS_ACTION_SIGNING_KEY", raising=False)
    keyfile = tmp_path / ".signing_key"
    s1 = da.load_server_secret(worker_count=1, config_path=keyfile)
    s2 = da.load_server_secret(worker_count=1, config_path=keyfile)
    assert s1 == s2


def test_load_server_secret_env_overrides_config_file(monkeypatch, tmp_path):
    """env 优先级 > config 文件。"""
    keyfile = tmp_path / ".signing_key"
    keyfile.write_text("file-key-" + "x" * 32)
    monkeypatch.setenv("NAS_ACTION_SIGNING_KEY", "env-key-" + "y" * 32)
    s = da.load_server_secret(worker_count=1, config_path=keyfile)
    assert s.startswith(b"env-key-")


def test_load_server_secret_config_file_rejects_short(monkeypatch, tmp_path):
    """config 文件里的 secret 太短 → raise。"""
    monkeypatch.delenv("NAS_ACTION_SIGNING_KEY", raising=False)
    keyfile = tmp_path / ".signing_key"
    keyfile.write_text("tooshort")
    with pytest.raises(da.ServerSecretMisconfigured):
        da.load_server_secret(worker_count=1, config_path=keyfile)


# ---------------- payload canonicalization ---------------- #


def test_canonical_json_is_stable(payload):
    """同 payload 不同 key 顺序 → 同 hash。防御 client 提交 reordered payload。"""
    p1 = {"a": 1, "b": [3, 2, 1], "c": {"y": 2, "x": 1}}
    p2 = {"c": {"x": 1, "y": 2}, "b": [3, 2, 1], "a": 1}
    assert da._payload_hash(p1) == da._payload_hash(p2)


# ── Phase 4B.0：TTL + update_running_result ──────────────────────


def test_organize_ttl_extended_to_1800():
    """Phase 4B：organize TTL 从 600s → 1800s（给 N=500 plan 审核 30 min）。"""
    assert da.TTL_BY_KIND["organize"] == 1800


def test_organize_running_timeout_extended_to_1800():
    """Phase 4B：organize running_timeout 60s → 1800s。N=500 confirm 跑 5-8 min，
    reaper 必须比 executor 慢，不能误标 needs_manual_recovery。"""
    assert da.RUNNING_TIMEOUT_BY_KIND["organize"] == 1800


def test_update_running_result_writes_partial_progress(conn, secret, payload):
    """在 status='running' 时增量写 result_json，不改 status/completed_at。"""
    # Step 1: preview + atomic consume 进入 running 状态
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    pre = conn.execute(
        "SELECT payload_hash FROM destructive_actions WHERE action_id = ?",
        (res.action_id,),
    ).fetchone()
    row = da._atomic_consume(conn, res.action_id, pre["payload_hash"], da._now())
    assert row is not None
    assert row["status"] == "running"

    # Step 2: progressive write
    partial = {"items_total": 100, "items_completed": 42, "current": "/a/b.mkv"}
    ok = da.update_running_result(conn, res.action_id, partial)
    assert ok is True

    # Step 3: 验证 result_json 落盘 + status / completed_at 不变
    after = conn.execute(
        "SELECT status, completed_at, result_json FROM destructive_actions "
        "WHERE action_id = ?",
        (res.action_id,),
    ).fetchone()
    assert after["status"] == "running"
    assert after["completed_at"] is None
    assert "items_completed" in after["result_json"]
    assert "42" in after["result_json"]


def test_update_running_result_skips_terminal_rows(conn, secret, payload):
    """status != 'running' 时 update 不生效，返回 False。防御踩 terminal 状态。"""
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    # 直接 mark terminal（绕过 confirm 流程）
    da._mark_terminal(
        conn, res.action_id, status="succeeded", result={"x": 1}, error=None
    )
    ok = da.update_running_result(conn, res.action_id, {"items_completed": 99})
    assert ok is False
    # 原 result 不被覆盖
    row = conn.execute(
        "SELECT result_json FROM destructive_actions WHERE action_id = ?",
        (res.action_id,),
    ).fetchone()
    assert '"x":1' in row["result_json"]
    assert "99" not in row["result_json"]


def test_update_running_result_skips_pending_rows(conn, secret, payload):
    """status='pending'（未 consume）也不应被增量写。"""
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    ok = da.update_running_result(conn, res.action_id, {"foo": "bar"})
    assert ok is False


def test_update_running_result_missing_action_returns_false(conn):
    """action_id 不存在 → False，不抛错。"""
    ok = da.update_running_result(conn, "nonexistent-action-id", {"x": 1})
    assert ok is False


def test_mark_terminal_if_running_flips_running_to_succeeded(conn, secret, payload):
    """status='running' 时正常翻成 succeeded，返 True。"""
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    pre = conn.execute(
        "SELECT payload_hash FROM destructive_actions WHERE action_id = ?",
        (res.action_id,),
    ).fetchone()
    row = da._atomic_consume(conn, res.action_id, pre["payload_hash"], da._now())
    assert row is not None

    ok = da.mark_terminal_if_running(
        conn, res.action_id, status="succeeded",
        result={"items": [{"x": 1}]}, error=None,
    )
    assert ok is True
    after = conn.execute(
        "SELECT status, completed_at FROM destructive_actions WHERE action_id = ?",
        (res.action_id,),
    ).fetchone()
    assert after["status"] == "succeeded"
    assert after["completed_at"] is not None


def test_mark_terminal_if_running_no_op_when_already_terminal(conn, secret, payload):
    """row 已 terminal（reaper 抢标 needs_manual_recovery）→ 不被覆盖，返 False。"""
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    # reaper 直接标 terminal
    conn.execute(
        "UPDATE destructive_actions SET status='needs_manual_recovery' "
        "WHERE action_id = ?", (res.action_id,),
    )
    conn.commit()

    ok = da.mark_terminal_if_running(
        conn, res.action_id, status="succeeded", result={"x": 1}, error=None,
    )
    assert ok is False
    # 原 status 不变
    after = conn.execute(
        "SELECT status FROM destructive_actions WHERE action_id = ?",
        (res.action_id,),
    ).fetchone()
    assert after["status"] == "needs_manual_recovery"


def test_mark_terminal_if_running_no_op_for_pending(conn, secret, payload):
    """pending 状态 → 不 flip（必须先 consume 才能进 running 再 terminal）。"""
    res = da.create_preview(conn, kind="delete", payload=payload, server_secret=secret)
    ok = da.mark_terminal_if_running(
        conn, res.action_id, status="succeeded", result={"x": 1}, error=None,
    )
    assert ok is False
