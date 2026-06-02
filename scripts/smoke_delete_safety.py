"""Phase 1 删除安全 — 真实 NAS 端到端冒烟（bug #4 #30 #31 + happy path）。

价值：用**真实 SSH** 跑新写的 shell 命令（`find <path> -maxdepth 0 -inum N`、
`stat -c %i` + `rm -rf`），这是 mock 单测抓不到的层（shell 引号 / stat 格式 / NAS
busybox 差异只在真机暴露）。删除走真实 preview→confirm 流程，第三方核对用独立 SSH。

安全设计：
  - 只在专用 sandbox `<NAS_BASE_PATH>/_nasvault_smoke_<pid>_<ts>/` 造临时文件
  - finally 必清理 sandbox（rm -rf 前校验路径含 smoke marker + 在 base 下）
  - 默认 gate：需 NASVAULT_SMOKE_CONFIRM=1 才真跑（它做真实删除，虽然只在 sandbox）
  - filesystem 测试用 options.delete_torrents=False，完全不碰 qBit
  - #31 只读 dry-run（不删任何东西），需有多文件种子才跑

跑：
  NASVAULT_SMOKE_CONFIRM=1 NAS_DISABLE_CRON=1 NAS_SKIP_WORKER_LOCK=1 \
    .venv/bin/python scripts/smoke_delete_safety.py

无法自动安全验证的（#1 reaper 计时 / #5 qBit 删种失败 / #31 真实删整种），
脚本末尾打印手动 checklist。
"""

from __future__ import annotations

import os
import sys
import time

# scripts/ 下运行时 repo root 不在 sys.path → 显式加，保证 `import app` 可用
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# env 必须在 import app 之前设（app 在 import 时读 env + 起 cron + 抢 worker lock）
os.environ.setdefault("NAS_DISABLE_CRON", "1")
os.environ.setdefault("NAS_SKIP_WORKER_LOCK", "1")

import app

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"

_results: list[tuple[str, str, str]] = []  # (status, name, detail)


def _record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))


# ── SSH 第三方核对 helpers（独立于删除逻辑的 find -inum / stat） ──


def _ssh(cmd: str, timeout: int = 30) -> tuple[int, str, str]:
    return app.ssh_exec(cmd, timeout=timeout)


def _ssh_must(cmd: str, timeout: int = 30) -> str:
    rc, out, err = _ssh(cmd, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"ssh failed (rc={rc}): {cmd[:80]} :: {err[:120]}")
    return out


def _exists(path: str) -> bool:
    import shlex

    _, out, _ = _ssh(f"test -e {shlex.quote(path)} && echo YES || echo NO")
    return out.strip().endswith("YES")


def _inode(path: str) -> int:
    import shlex

    _, out, _ = _ssh(f"stat -c %i -- {shlex.quote(path)} 2>/dev/null || echo 0")
    try:
        return int(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def _mkfile(path: str, mb: int = 2) -> None:
    import shlex

    sp = shlex.quote(path)
    _ssh_must(
        f"mkdir -p -- $(dirname {sp}) && dd if=/dev/zero of={sp} bs=1M count={mb} 2>/dev/null"
    )


# ── HTTP flow helpers（真实 preview→confirm，executor 跑真 SSH） ──


def _client():
    app.app.config["TESTING"] = True
    return app.app.test_client()


def _auth() -> dict:
    return {"Authorization": f"Bearer {app.API_TOKEN}"}


def _preview(client, candidates: list[dict], *, delete_torrents: bool = False) -> dict:
    resp = client.post(
        "/api/action/preview",
        json={
            "kind": "delete",
            "source": "file_browser",
            "candidates": candidates,
            "options": {"delete_torrents": delete_torrents},
        },
        headers=_auth(),
    )
    return {"status": resp.status_code, "body": resp.get_json()}


def _confirm(client, action_id: str, signed_token: str) -> dict:
    resp = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed_token},
        headers=_auth(),
    )
    return {"status": resp.status_code, "body": resp.get_json()}


# ── 测试场景 ──────────────────────────────────────────────────────


def scenario_happy_path_file_delete(client, sandbox: str) -> None:
    """基础：删一个文件 → 第三方 SSH 确认真没了 + 应用报告一致。"""
    f = f"{sandbox}/happy.mkv"
    _mkfile(f)
    assert _exists(f), "fixture not created"

    pv = _preview(client, [{"path": f}])
    if pv["status"] != 200:
        _record(FAIL, "happy_path file delete", f"preview {pv['status']}: {pv['body']}")
        return
    cf = _confirm(client, pv["body"]["action_id"], pv["body"]["signed_token"])
    gone = not _exists(f)  # 第三方 ground truth
    app_says = (
        cf["body"].get("result", {}).get("total_files_deleted", 0) >= 1 if cf["body"] else False
    )
    if gone and cf["status"] == 200:
        _record(PASS, "happy_path file delete", f"file gone (app_deleted={app_says})")
    else:
        _record(FAIL, "happy_path file delete", f"gone={gone} confirm={cf['status']} {cf['body']}")


def scenario_hardlink_not_swept(client, sandbox: str) -> None:
    """bug #4：preview 后新建的同 inode hardlink 不应被删（只删预览捕获的 path 集）。

    A = 主文件；B = preview 前就有的 hardlink（应随 A 一起删）；
    C = preview 之后才建的 hardlink（模拟 organize cron ln 进库）→ 必须存活。
    """
    import shlex

    a = f"{sandbox}/hl_A.mkv"
    b = f"{sandbox}/hl_B.mkv"
    c = f"{sandbox}/hl_C.mkv"
    _mkfile(a)
    _ssh_must(f"ln {shlex.quote(a)} {shlex.quote(b)}")  # preview 前的 hardlink

    pv = _preview(client, [{"path": a}])
    if pv["status"] != 200:
        _record(FAIL, "#4 hardlink not swept", f"preview {pv['status']}: {pv['body']}")
        return

    # preview 与 confirm 之间：模拟 organize cron 新建同 inode hardlink C
    _ssh_must(f"ln {shlex.quote(a)} {shlex.quote(c)}")

    cf = _confirm(client, pv["body"]["action_id"], pv["body"]["signed_token"])

    a_gone = not _exists(a)
    b_gone = not _exists(b)
    c_survived = _exists(c)  # 关键：C 必须活着
    if cf["status"] == 200 and a_gone and b_gone and c_survived:
        _record(PASS, "#4 hardlink not swept", "A+B deleted, post-preview hardlink C survived")
    else:
        _record(
            FAIL,
            "#4 hardlink not swept",
            f"a_gone={a_gone} b_gone={b_gone} c_survived={c_survived} confirm={cf['status']}",
        )


def scenario_dir_delete_happy(client, sandbox: str) -> None:
    """bug #30 happy：inode 匹配时目录真删（验证 stat %i + rm -rf 命令在真机能跑）。"""
    import shlex

    d = f"{sandbox}/dir_ok"
    _mkfile(f"{d}/inner.mkv", mb=1)
    assert _exists(d)

    pv = _preview(client, [{"path": d}])
    if pv["status"] != 200:
        _record(FAIL, "#30 dir delete happy", f"preview {pv['status']}: {pv['body']}")
        return
    cf = _confirm(client, pv["body"]["action_id"], pv["body"]["signed_token"])
    gone = not _exists(d)
    if cf["status"] == 200 and gone:
        _record(PASS, "#30 dir delete happy", "dir removed via stat-verify + rm -rf")
    else:
        _record(FAIL, "#30 dir delete happy", f"gone={gone} confirm={cf['status']} {cf['body']}")
    _ssh(f"rm -rf -- {shlex.quote(d)}")  # 兜底清理（若没删成）


def scenario_dir_swap_not_deleted(client, sandbox: str) -> None:
    """bug #30：preview 后把目录 swap 成另一个目录（不同 inode），confirm 必须不删新目录。

    实际由两道防线保障：confirm 重建 snapshot 的 drift 检查（重 stat item inode）+
    删除分支的 #30 inode recheck。端到端结果：swap 后的新目录必须存活。
    """
    import shlex

    d = f"{sandbox}/dir_swap"
    _mkfile(f"{d}/orig.mkv", mb=1)

    pv = _preview(client, [{"path": d}])
    if pv["status"] != 200:
        _record(FAIL, "#30 dir swap not deleted", f"preview {pv['status']}: {pv['body']}")
        return

    # swap：原目录移走，同路径建一个全新目录（不同 inode）+ 哨兵文件
    _ssh_must(
        f"mv {shlex.quote(d)} {shlex.quote(d + '_old')} && "
        f"mkdir -p {shlex.quote(d)} && touch {shlex.quote(d + '/SENTINEL')}"
    )
    new_inode = _inode(d)

    cf = _confirm(client, pv["body"]["action_id"], pv["body"]["signed_token"])

    sentinel_alive = _exists(d + "/SENTINEL")  # 关键：新目录哨兵必须活着
    new_dir_alive = _inode(d) == new_inode
    if sentinel_alive and new_dir_alive:
        _record(
            PASS,
            "#30 dir swap not deleted",
            f"swapped dir survived (confirm={cf['status']}, status not 'deleted')",
        )
    else:
        _record(
            FAIL,
            "#30 dir swap not deleted",
            f"sentinel_alive={sentinel_alive} new_dir_alive={new_dir_alive} — WRONG DIR DELETED",
        )
    _ssh(f"rm -rf -- {shlex.quote(d)} {shlex.quote(d + '_old')}")


def scenario_partition_dryrun_real_qbit() -> None:
    """bug #31 只读 dry-run：找一个真实多文件种子，验证「选其中一个文件」会被判定为
    skipped（不删整种）。不删任何东西。无多文件种子则 SKIP。
    """
    try:
        torrents = app.qbit.get_torrents()
    except Exception as e:
        _record(SKIP, "#31 partition dry-run", f"qBit 不可达：{e}")
        return

    multi = None
    for t in torrents:
        try:
            files = app.qbit.get_torrent_files(t["hash"])
        except Exception:
            continue
        if len(files) >= 2 and t.get("save_path"):
            multi = (t, files)
            break
    if not multi:
        _record(SKIP, "#31 partition dry-run", "无多文件种子可测（需 qBit 有 ≥2 文件的种子）")
        return

    t, files = multi
    save = t["save_path"]
    one_file = os.path.join(save, files[0]["name"])
    # 构造「用户只选这一个文件」的 snapshot（不删，只判定）
    snap = {
        "items": [{"path": one_file, "is_dir": False, "hardlinks": []}],
        "torrents": [
            {
                "hash": t["hash"],
                "name": t.get("name", ""),
                "save_path": save,
                "content_path": t.get("content_path", ""),
            }
        ],
    }
    to_delete, skipped = app._partition_torrents_for_deletion(
        snap, get_files_fn=app.qbit.get_torrent_files, resolve_fn=app._resolve_real_paths
    )
    if t["hash"] in [s["hash"] for s in skipped] and t["hash"] not in to_delete:
        _record(
            PASS,
            "#31 partition dry-run",
            f"multi-file torrent {t['hash'][:8]} ({len(files)} files) → skipped when 1 file selected",
        )
    else:
        _record(
            FAIL,
            "#31 partition dry-run",
            f"DANGER: torrent would be deleted on single-file selection! to_delete={to_delete}",
        )


# ── 入口 ──────────────────────────────────────────────────────────


def main() -> int:
    if os.environ.get("NASVAULT_SMOKE_CONFIRM") != "1":
        print(__doc__)
        print(
            "⛔ 拒绝运行：本脚本会在 NAS 上真实删除（仅 sandbox 内）。\n"
            "   确认后用：NASVAULT_SMOKE_CONFIRM=1 NAS_DISABLE_CRON=1 NAS_SKIP_WORKER_LOCK=1 "
            ".venv/bin/python scripts/smoke_delete_safety.py"
        )
        return 2

    # NAS 可达性 + base path 校验
    rc, out, err = _ssh("echo NAS_OK")
    if rc != 0 or "NAS_OK" not in out:
        print(f"⛔ NAS 不可达（rc={rc}）：{err[:200]}\n   先确认 config/nas.json + SSH key。")
        return 2

    base = app.NAS_BASE_PATH.rstrip("/")
    sandbox = f"{base}/_nasvault_smoke_{os.getpid()}_{int(time.time())}"
    # 安全断言：sandbox 必须在 base 下且含 smoke marker（cleanup 前还会再查）
    assert sandbox.startswith(base + "/") and "_nasvault_smoke_" in sandbox

    print(f"\n=== NAS 删除安全 e2e 冒烟 ===\nNAS: {app.NAS_USER}@{app.NAS_HOST}  base={base}")
    print(f"sandbox: {sandbox}\n")

    client = _client()
    try:
        _ssh_must(f"mkdir -p {__import__('shlex').quote(sandbox)}")
        print("--- Phase A: filesystem 删除安全（真实删除，sandbox 内） ---")
        scenario_happy_path_file_delete(client, sandbox)
        scenario_hardlink_not_swept(client, sandbox)
        scenario_dir_delete_happy(client, sandbox)
        scenario_dir_swap_not_deleted(client, sandbox)
        print("\n--- Phase B: qBit #31 只读 dry-run（不删任何东西） ---")
        scenario_partition_dryrun_real_qbit()
    finally:
        # 兜底清理 sandbox（再次校验路径，防误删）
        import shlex

        if "_nasvault_smoke_" in sandbox and sandbox.startswith(base + "/"):
            _ssh(f"rm -rf -- {shlex.quote(sandbox)}", timeout=60)
            print(f"\n🧹 cleaned sandbox: {sandbox}")

    # 汇总
    n_pass = sum(1 for s, _, _ in _results if s == PASS)
    n_fail = sum(1 for s, _, _ in _results if s == FAIL)
    n_skip = sum(1 for s, _, _ in _results if s == SKIP)
    print(f"\n=== 结果：{n_pass} PASS / {n_fail} FAIL / {n_skip} SKIP ===")

    print(
        "\n--- 无法自动安全验证（需手动）---\n"
        "  #1 reaper 计时：构造 >1800s 的删除几乎不可能在冒烟里跑；已由单测覆盖\n"
        "     (test_confirm_*_does_not_clobber_reaper_flag)。\n"
        "  #5 qBit 删种失败：需断网/停 qBit + 真种子；已由单测覆盖\n"
        "     (test_delete_executor_qbit_failure_aborts_before_file_deletion)。\n"
        "  #31 真实删整种：拿一个**可丢弃的多文件种子**，UI 里只勾其中一个文件→删除→\n"
        "     确认 qBit 里该种子仍在、其余文件未被删（df + qBit API 第三方核对）。"
    )
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
