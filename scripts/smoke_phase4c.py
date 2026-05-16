"""Phase 4C 端到端冒烟脚本 — mock qBit API (502 down) + 真实跑 cron + cross-check.

跑：
  NAS_SKIP_WORKER_LOCK=1 NAS_DISABLE_CRON=1 .venv/bin/python scripts/smoke_phase4c.py

不依赖 qBit 服务可达；用 monkeypatch qbit.get_torrents 注入 fake 已完成种子。
组织：Stranger Things 4B 已整理过的种子 → 应走 already_linked path（幂等）.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import time

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

import app  # noqa: E402
from services import qbit_auto  # noqa: E402

# ── 1. mock qBit get_torrents 返 fake completed torrents ───

FAKE_TORRENTS = [
    # 4B 已整理过的 Stranger Things — 应被识别 → confidence_gate pass → already_linked
    {
        "hash": "smoke4c-stranger-things-s04p1",
        "name": "Stranger.Things.S04.Part.1.1080p.NF.WEBRip.DDP5.1.Atmos.x264-TBD[rartv]",
        "category": "TestAutoOrganize",
        "state": "seeding",
        "progress": 1.0,
        "content_path": "/share/CACHEDEV2_DATA/downloads/Stranger.Things.S04.Part.1.1080p.NF.WEBRip.DDP5.1.Atmos.x264-TBD[rartv]",
        "save_path": "/share/CACHEDEV2_DATA/downloads",
    },
    # 没识别的种子 → 应被标 skipped_needs_identify
    {
        "hash": "smoke4c-unknown-torrent",
        "name": "Unknown.Torrent.That.Was.Never.Identified.mkv",
        "category": "TestAutoOrganize",
        "state": "seeding",
        "progress": 1.0,
        "content_path": "/share/CACHEDEV2_DATA/downloads/_smoke_nonexistent_dir_/Unknown.mkv",
        "save_path": "/share/CACHEDEV2_DATA/downloads/_smoke_nonexistent_dir_",
    },
    # progress < 1.0 → 应被 list_completed_torrents 过滤
    {
        "hash": "smoke4c-downloading",
        "name": "Still.Downloading.mkv",
        "category": "TestAutoOrganize",
        "state": "downloading",
        "progress": 0.5,
        "content_path": "/d/x.mkv",
    },
    # category 不在白名单 → 应被过滤
    {
        "hash": "smoke4c-wrong-category",
        "name": "Right.Progress.Wrong.Cat.mkv",
        "category": "NotInWhitelist",
        "state": "seeding",
        "progress": 1.0,
        "content_path": "/d/y.mkv",
    },
]

original_get_torrents = app.qbit.get_torrents
app.qbit.get_torrents = lambda: FAKE_TORRENTS


def step(msg):
    print(f"\n{'═' * 70}\n  {msg}\n{'═' * 70}")


# ── 2. 设置 config: enabled + 白名单 + threshold 0.85 ───

step("STEP 1: 配置 qbit_auto_organize (enabled, whitelist=[TestAutoOrganize])")
app.save_qbit_auto_organize_config(
    {
        "enabled": True,
        "categories": ["TestAutoOrganize"],
        "poll_interval_minutes": 5,
        "confidence_threshold": 0.85,
    }
)
cfg = app.load_qbit_auto_organize_config()
print(f"  config saved: {cfg}")

# ── 3. 清理潜在残留 auto_organize_runs row ───

step("STEP 2: 清理潜在残留 (上次 smoke 跑过的 row)")
conn = sqlite3.connect("config/actions.db")
conn.row_factory = sqlite3.Row
n = conn.execute("DELETE FROM auto_organize_runs WHERE qbit_hash LIKE 'smoke4c-%'").rowcount
conn.commit()
print(f"  cleaned {n} previous smoke rows")

# ── 4. 跑一次 cron job ───

step("STEP 3: 调 _cron_qbit_auto_organize() 一次")
start = time.time()
app._cron_qbit_auto_organize()
elapsed = time.time() - start
print(f"  dispatch cycle done in {elapsed:.1f}s")

# 让 worker 有时间跑完（最多 30s）
print("  waiting up to 30s for organize_runner to finish (if dispatched any)...")
for i in range(30):
    rows = conn.execute(
        "SELECT qbit_hash, status FROM auto_organize_runs WHERE qbit_hash LIKE 'smoke4c-%'"
    ).fetchall()
    organizing = [r["qbit_hash"] for r in rows if r["status"] == "organizing"]
    if not organizing:
        print(f"  no organizing rows after {i + 1}s")
        break
    time.sleep(1)

# 跑一次 reconcile 确保终态同步（cron 期间可能 organizing 没 sync 完）
step("STEP 4: 调 reconcile_organizing_rows 同步 organizing → terminal")
synced = qbit_auto.reconcile_organizing_rows(conn)
print(f"  reconciled: {synced}")

# ── 5. cross-check auto_organize_runs 状态 ───

step("STEP 5: 检查 auto_organize_runs 各 hash 终态")
rows = conn.execute(
    "SELECT * FROM auto_organize_runs WHERE qbit_hash LIKE 'smoke4c-%' ORDER BY created_at"
).fetchall()
for r in rows:
    print(
        f"  {r['qbit_hash']:40} status={r['status']:25} "
        f"action_id={r['action_id'] or '-'} attempts={r['attempts']}"
    )
    if r["last_error"]:
        print(f"    error: {r['last_error'][:200]}")
    if r["files_succeeded"] is not None:
        print(
            f"    files: succeeded={r['files_succeeded']} "
            f"already_linked={r['files_already_linked']} failed={r['files_failed']}"
        )

# ── 6. 期望断言 ───

step("STEP 6: 期望 vs 实际")
hash_status = {r["qbit_hash"]: r["status"] for r in rows}
expected = {
    "smoke4c-stranger-things-s04p1": ["succeeded"],  # 4B 已整理 → already_linked → succeeded
    "smoke4c-unknown-torrent": ["skipped_needs_identify", "skipped_unsupported"],
    # progress<1 + 错 category 不该在表里出现（list_completed_torrents filter 掉）
}
forbidden = {"smoke4c-downloading", "smoke4c-wrong-category"}

ok = True
for h, allowed in expected.items():
    actual = hash_status.get(h, "(missing)")
    mark = "✓" if actual in allowed else "✗"
    if actual not in allowed:
        ok = False
    print(f"  {mark} {h}: actual={actual} expected in {allowed}")
for h in forbidden:
    if h in hash_status:
        ok = False
        print(f"  ✗ {h}: row 不该存在 (被 list_completed_torrents 过滤)")
    else:
        print(f"  ✓ {h}: 未进入表 (正确过滤)")

# ── 7. cross-check: destructive_actions table 看 created_by='cron' row ───

step("STEP 7: 看本次 cron 创建的 destructive_actions row")
recent = conn.execute(
    """
    SELECT action_id, kind, status, created_by, error, started_at, completed_at
    FROM destructive_actions
    WHERE created_by = 'cron' AND started_at >= ?
    ORDER BY started_at DESC LIMIT 5
""",
    (int(time.time()) - 120,),
).fetchall()
print(f"  found {len(recent)} cron-created actions in last 2 min:")
for r in recent:
    print(
        f"  {r['action_id'][:8]} kind={r['kind']} status={r['status']} created_by={r['created_by']}"
    )
    if r["error"]:
        print(f"    error: {r['error'][:200]}")

# ── cleanup smoke rows ───
step("CLEANUP: 删 smoke4c-* rows")
n = conn.execute("DELETE FROM auto_organize_runs WHERE qbit_hash LIKE 'smoke4c-%'").rowcount
conn.commit()
print(f"  deleted {n} smoke rows")

# 关掉 enabled 防意外触发
app.save_qbit_auto_organize_config(
    {
        "enabled": False,
        "categories": [],
        "poll_interval_minutes": 5,
        "confidence_threshold": 0.85,
    }
)
print("  reverted config to disabled+empty whitelist")

conn.close()

step("DONE" if ok else "FAILURES detected")
sys.exit(0 if ok else 1)
