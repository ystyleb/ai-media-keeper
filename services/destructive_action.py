"""DestructiveAction service — 契约 #1 实现。

所有 mutating NAS / qBit 操作的统一入口：
    preview → 落 row (status='pending') + 签发 signed_token
    confirm → atomic consume + HMAC 验签 → 执行业务 → 写终态

Phase 1 spike scope：先把 contract 跑通，executor 注册由调用方传入。
不内置任何 SSH / qBit 逻辑，保持 service 纯 contract 层。

参考：~/.claude/plans/ai-native-mutable-bubble.md 契约 #1 / #2
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# kind → TTL（秒）。archive 长操作给 30 min，delete 5 min。
TTL_BY_KIND: dict[str, int] = {
    "delete": 300,
    "nfo_write": 1800,
    "archive": 1800,
    "purge_provider": 600,
}

# crash recovery 阈值：running 持续超过此值视为 crash
RUNNING_TIMEOUT_BY_KIND: dict[str, int] = {
    "delete": 60,
    "nfo_write": 120,
    "archive": 1800,
    "purge_provider": 120,
}


class ActionError(Exception):
    """所有 destructive action 异常的基类。"""


class ActionNotFound(ActionError):
    pass


class ActionExpired(ActionError):
    pass


class ActionAlreadyConsumed(ActionError):
    pass


class ActionPayloadTampered(ActionError):
    pass


class ActionTokenInvalid(ActionError):
    pass


class ServerSecretMisconfigured(RuntimeError):
    """生产模式必须显式设 NAS_ACTION_SIGNING_KEY。"""


@dataclass(frozen=True)
class PreviewResult:
    action_id: str
    kind: str
    payload: dict[str, Any]
    signed_token: str
    expires_at: int


@dataclass(frozen=True)
class ConfirmResult:
    action_id: str
    status: str
    result: dict[str, Any] | None
    error: str | None


# Executor 签名：(payload) -> result_dict
# raise 任何异常视为 failed
Executor = Callable[[dict[str, Any]], dict[str, Any]]


def _canonical_json(obj: Any) -> str:
    """稳定 JSON：sort_keys + compact separators，用于 hash + signing。"""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _now() -> int:
    return int(time.time())


def load_server_secret(
    *,
    worker_count: int = 1,
    config_path: Path | str | None = None,
) -> bytes:
    """加载持久 server_secret。

    优先级：
    1. env NAS_ACTION_SIGNING_KEY（≥32 字符）
    2. config_path 指向的文件（first start 自动生成 + chmod 600）
    3. 未提供 config_path 且 worker_count == 1 时，进程内 ephemeral 临时 secret + warning
       （多 worker 时强制 raise——必须共享持久 secret）
    """
    env_key = os.environ.get("NAS_ACTION_SIGNING_KEY", "").strip()
    if env_key:
        if len(env_key) < 32:
            raise ServerSecretMisconfigured(
                "NAS_ACTION_SIGNING_KEY must be at least 32 chars; "
                "generate with: python3 -c 'import secrets; print(secrets.token_hex(32))'"
            )
        return env_key.encode("utf-8")

    if config_path is not None:
        p = Path(config_path)
        if p.exists():
            content = p.read_text(encoding="utf-8").strip()
            if len(content) < 32:
                raise ServerSecretMisconfigured(
                    f"signing key file {p} contains a key shorter than 32 chars; "
                    "delete it to regenerate, or set NAS_ACTION_SIGNING_KEY env"
                )
            return content.encode("utf-8")
        # 首次启动：自动生成 + 写文件 + chmod 600
        new_key = secrets.token_hex(32)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(new_key, encoding="utf-8")
        try:
            os.chmod(p, 0o600)
        except OSError as e:
            logger.warning(f"[destructive_action] could not chmod 600 {p}: {e}")
        logger.info(
            f"[destructive_action] generated new server_secret → {p} "
            "(persists across restarts; delete to rotate)"
        )
        return new_key.encode("utf-8")

    # 既没 env 也没 config_path → 纯内存 ephemeral
    if worker_count > 1:
        raise ServerSecretMisconfigured(
            "Multi-worker requires persistent signing key (NAS_ACTION_SIGNING_KEY env "
            "or config_path)."
        )

    dev_secret = secrets.token_hex(32)
    logger.warning(
        "[destructive_action] no persistent signing key configured — using ephemeral "
        "in-memory secret. In-flight tokens will NOT survive restart."
    )
    return dev_secret.encode("utf-8")


def _signed_token(secret: bytes, action_id: str, payload_hash: str) -> str:
    msg = f"{action_id}|{payload_hash}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def _verify_token(secret: bytes, action_id: str, payload_hash: str, token: str) -> bool:
    expected = _signed_token(secret, action_id, payload_hash)
    return hmac.compare_digest(expected, token)


def init_schema(conn: sqlite3.Connection, schema_path: Path | str) -> None:
    """idempotent schema apply。调用方传入 connection 后由 caller 控制事务/关闭。"""
    sql = Path(schema_path).read_text(encoding="utf-8")
    conn.executescript(sql)
    conn.commit()


def create_preview(
    conn: sqlite3.Connection,
    *,
    kind: str,
    payload: dict[str, Any],
    server_secret: bytes,
    created_by: str = "web_ui",
    ttl_override: int | None = None,
) -> PreviewResult:
    """生成一个 pending action。

    payload 必须含 'snapshot' 字段（契约 #2），但本 service 不校验 snapshot
    具体结构——由调用方负责生成 ground-truth snapshot 后传入。
    """
    if kind not in TTL_BY_KIND:
        raise ValueError(f"unknown kind: {kind}")
    if not isinstance(payload, dict):
        raise ValueError("payload must be dict")

    action_id = str(uuid.uuid4())
    ttl = ttl_override if ttl_override is not None else TTL_BY_KIND[kind]
    now = _now()
    expires_at = now + ttl

    payload_json = _canonical_json(payload)
    p_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    token = _signed_token(server_secret, action_id, p_hash)

    conn.execute(
        """
        INSERT INTO destructive_actions
          (action_id, kind, payload_hash, payload_json, expires_at,
           status, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (action_id, kind, p_hash, payload_json, expires_at, created_by, now),
    )
    conn.commit()
    return PreviewResult(
        action_id=action_id,
        kind=kind,
        payload=payload,
        signed_token=token,
        expires_at=expires_at,
    )


def _atomic_consume(
    conn: sqlite3.Connection, action_id: str, submitted_payload_hash: str, now: int
) -> sqlite3.Row | None:
    """Stage 1: 原子 consume + status='running'。

    单条 guarded UPDATE，必须 rowcount=1 否则视为失败（已消费 / 过期 / 篡改 / 状态错）。
    返回 row（含 payload_json 等），或 None 表示消费失败。
    """
    cur = conn.execute(
        """
        UPDATE destructive_actions
           SET consumed_at = ?,
               started_at  = ?,
               status      = 'running'
         WHERE action_id    = ?
           AND consumed_at  IS NULL
           AND expires_at   > ?
           AND payload_hash = ?
           AND status       = 'pending'
        """,
        (now, now, action_id, now, submitted_payload_hash),
    )
    if cur.rowcount != 1:
        conn.rollback()
        return None
    conn.commit()
    row = conn.execute(
        "SELECT * FROM destructive_actions WHERE action_id = ?", (action_id,)
    ).fetchone()
    return row


def _rollback_to_pending(conn: sqlite3.Connection, action_id: str) -> None:
    """Stage 2 验签失败：回滚 status='running' 到 'pending' 并清 consumed_at。

    严格只在签名验证失败时调用——保证 action_id 不会因为攻击者拿到正确
    payload_hash 但错误 HMAC token 就被永久占用。
    """
    conn.execute(
        """
        UPDATE destructive_actions
           SET consumed_at = NULL,
               started_at  = NULL,
               status      = 'pending'
         WHERE action_id   = ?
           AND status      = 'running'
        """,
        (action_id,),
    )
    conn.commit()


def _mark_terminal(
    conn: sqlite3.Connection,
    action_id: str,
    status: str,
    result: dict[str, Any] | None,
    error: str | None,
) -> None:
    conn.execute(
        """
        UPDATE destructive_actions
           SET status       = ?,
               completed_at = ?,
               error        = ?,
               result_json  = ?
         WHERE action_id    = ?
        """,
        (
            status,
            _now(),
            error,
            _canonical_json(result) if result else None,
            action_id,
        ),
    )
    conn.commit()


def confirm(
    conn: sqlite3.Connection,
    *,
    action_id: str,
    signed_token: str,
    server_secret: bytes,
    executor: Executor,
) -> ConfirmResult:
    """三段状态机：consume → verify → execute → terminal。"""
    now = _now()

    # 先查 row 拿 payload_hash 给 atomic consume 用作 guard 条件
    pre = conn.execute(
        "SELECT payload_hash, status, consumed_at, expires_at "
        "FROM destructive_actions WHERE action_id = ?",
        (action_id,),
    ).fetchone()
    if pre is None:
        raise ActionNotFound(action_id)

    # Stage 1: atomic consume
    row = _atomic_consume(conn, action_id, pre["payload_hash"], now)
    if row is None:
        # 失败可能原因：已 consumed / expired / status 已变 — 重查精确报错
        cur = conn.execute(
            "SELECT status, consumed_at, expires_at FROM destructive_actions "
            "WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if cur is None:
            raise ActionNotFound(action_id)
        if cur["consumed_at"] is not None:
            raise ActionAlreadyConsumed(action_id)
        if cur["expires_at"] <= now:
            raise ActionExpired(action_id)
        # 其他原因（状态机错位）
        raise ActionError(f"could not consume {action_id}: status={cur['status']}")

    # Stage 2: HMAC 验签
    if not _verify_token(server_secret, action_id, row["payload_hash"], signed_token):
        _rollback_to_pending(conn, action_id)
        raise ActionTokenInvalid(action_id)

    # Stage 3: 执行业务
    payload = json.loads(row["payload_json"])
    try:
        result = executor(payload)
    except Exception as exc:  # noqa: BLE001 — executor 任何异常视为 failed
        logger.exception("[destructive_action] executor raised for %s", action_id)
        _mark_terminal(
            conn,
            action_id,
            status="failed",
            result=None,
            error=f"{type(exc).__name__}: {exc}",
        )
        return ConfirmResult(
            action_id=action_id,
            status="failed",
            result=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    _mark_terminal(conn, action_id, status="succeeded", result=result, error=None)
    return ConfirmResult(
        action_id=action_id,
        status="succeeded",
        result=result,
        error=None,
    )


def reap_stuck_actions(conn: sqlite3.Connection, *, now: int | None = None) -> int:
    """Cron job：把 status='running' 但超过 timeout 的标 needs_manual_recovery。

    返回处理的 row 数。recovery_hint 由后续 kind-specific 增强填充——
    spike 阶段只标状态。
    """
    now = now if now is not None else _now()
    total = 0
    for kind, timeout in RUNNING_TIMEOUT_BY_KIND.items():
        cur = conn.execute(
            """
            UPDATE destructive_actions
               SET status        = 'needs_manual_recovery',
                   recovery_hint = COALESCE(recovery_hint,
                                            'auto: running > ' || ? || 's')
             WHERE status        = 'running'
               AND kind          = ?
               AND started_at    IS NOT NULL
               AND started_at    < ?
            """,
            (timeout, kind, now - timeout),
        )
        total += cur.rowcount
    conn.commit()
    return total


def cleanup_expired_pending(conn: sqlite3.Connection, *, now: int | None = None) -> int:
    """Cron job：把 expired 的 pending row 标 failed。"""
    now = now if now is not None else _now()
    cur = conn.execute(
        """
        UPDATE destructive_actions
           SET status       = 'failed',
               error        = 'expired_before_confirm',
               completed_at = ?
         WHERE status       = 'pending'
           AND expires_at   <= ?
        """,
        (now, now),
    )
    conn.commit()
    return cur.rowcount


def open_connection(db_path: Path | str) -> sqlite3.Connection:
    """开 SQLite connection 并设好 row_factory + pragma。"""
    conn = sqlite3.connect(db_path, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    # 每个 connection 都要确保 WAL（schema 里 PRAGMA 只在初次 setup 时生效）
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn
