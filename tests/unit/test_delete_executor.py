"""删除执行器安全修复单元测试（bug #4 #5 #30 #31）。

- #31: _partition_torrents_for_deletion — 只删「全部文件都在用户预览删除集内」的种子
- #5:  qBit 删种失败 → _delete_executor re-raise，零文件删除
- #4:  文件删除按预览 path 集 scoped 删（不用全局 find <base> -inum）
- #30: 目录删除前 re-verify inode
"""

from __future__ import annotations

import pytest

import app as app_module

# ── #31: torrent partition (pure, injectable) ──────────────────────


def _identity_resolve(paths):
    return {p: p for p in paths}


def test_partition_single_file_torrent_fully_contained_is_deleted():
    snap = {
        "items": [{"path": "/share/dl/movie.mkv", "is_dir": False, "hardlinks": []}],
        "torrents": [
            {
                "hash": "H1",
                "name": "movie",
                "save_path": "/share/dl",
                "content_path": "/share/dl/movie.mkv",
            }
        ],
    }
    files = {"H1": [{"name": "movie.mkv"}]}
    to_delete, skipped = app_module._partition_torrents_for_deletion(
        snap, get_files_fn=lambda h: files[h], resolve_fn=_identity_resolve
    )
    assert to_delete == ["H1"]
    assert skipped == []


def test_partition_whole_dir_selected_deletes_torrent():
    snap = {
        "items": [{"path": "/share/dl/SeasonPack", "is_dir": True, "hardlinks": []}],
        "torrents": [
            {
                "hash": "H2",
                "name": "SeasonPack",
                "save_path": "/share/dl",
                "content_path": "/share/dl/SeasonPack",
            }
        ],
    }
    files = {"H2": [{"name": "SeasonPack/ep01.mkv"}, {"name": "SeasonPack/ep02.mkv"}]}
    to_delete, skipped = app_module._partition_torrents_for_deletion(
        snap, get_files_fn=lambda h: files[h], resolve_fn=_identity_resolve
    )
    assert to_delete == ["H2"]
    assert skipped == []


def test_partition_single_episode_of_multifile_torrent_is_skipped():
    """核心安全用例：选一集 → 种子含未选中文件 → 不删种子。"""
    snap = {
        "items": [{"path": "/share/dl/SeasonPack/ep01.mkv", "is_dir": False, "hardlinks": []}],
        "torrents": [
            {
                "hash": "H3",
                "name": "SeasonPack",
                "save_path": "/share/dl",
                "content_path": "/share/dl/SeasonPack",
            }
        ],
    }
    files = {"H3": [{"name": "SeasonPack/ep01.mkv"}, {"name": "SeasonPack/ep02.mkv"}]}
    to_delete, skipped = app_module._partition_torrents_for_deletion(
        snap, get_files_fn=lambda h: files[h], resolve_fn=_identity_resolve
    )
    assert to_delete == []
    assert len(skipped) == 1
    assert skipped[0]["hash"] == "H3"
    assert skipped[0]["reason"] == "partial_torrent_not_removed"


def test_partition_files_list_unavailable_is_conservatively_skipped():
    snap = {
        "items": [{"path": "/share/dl/x.mkv", "is_dir": False, "hardlinks": []}],
        "torrents": [
            {"hash": "H4", "name": "x", "save_path": "/share/dl", "content_path": "/share/dl/x.mkv"}
        ],
    }

    def boom(h):
        raise RuntimeError("qBit files API down")

    to_delete, skipped = app_module._partition_torrents_for_deletion(
        snap, get_files_fn=boom, resolve_fn=_identity_resolve
    )
    assert to_delete == []
    assert skipped[0]["reason"] == "files_list_unavailable"


# ── #5: qBit delete failure must abort before file deletion ─────────


def test_delete_executor_qbit_failure_aborts_before_file_deletion(monkeypatch):
    """qBit delete_torrents 抛错 → _delete_executor re-raise，绝不进入文件删除循环。"""
    snapshot = {
        "captured_at": 1,
        "mode": "lenient",
        "items": [
            {
                "path": "/share/dl/a.mkv",
                "exists": True,
                "inode": 111,
                "size_bytes": 10,
                "mtime": 1,
                "is_dir": False,
                "real_size": 10,
                "hardlinks": [],
            }
        ],
        "all_to_delete": ["/share/dl/a.mkv"],
        "torrents": [
            {"hash": "H1", "name": "a", "save_path": "/share/dl", "content_path": "/share/dl/a.mkv"}
        ],
        "qbit_status": {"ok": True, "message": ""},
    }
    payload = {
        "candidates": [{"path": "/share/dl/a.mkv"}],
        "snapshot": snapshot,
        "options": {"delete_torrents": True},
    }

    # 无 drift
    monkeypatch.setattr(app_module, "_build_delete_snapshot", lambda c, mode="lenient": snapshot)
    # 种子全包含（让删种被触发）
    monkeypatch.setattr(
        app_module,
        "_partition_torrents_for_deletion",
        lambda snap, get_files_fn, resolve_fn: (["H1"], []),
    )
    # qBit 删种抛错
    monkeypatch.setattr(
        app_module.qbit,
        "delete_torrents",
        lambda hashes, delete_files=True: (_ for _ in ()).throw(RuntimeError("qBit down")),
    )
    # ssh_exec 若被调到 = 文件删除执行了 = bug
    ssh_calls = []
    monkeypatch.setattr(
        app_module, "ssh_exec", lambda cmd, timeout=300: ssh_calls.append(cmd) or (0, "", "")
    )

    with pytest.raises(RuntimeError):
        app_module._delete_executor(payload)

    assert ssh_calls == [], "files were deleted despite qBit failure (orphan torrents)"


# ── #4: file deletion is scoped to previewed paths, not global find ──


def test_delete_executor_file_uses_scoped_not_global_find(monkeypatch):
    """文件删除不能用全局 `find <base> -inum`（会扫到 preview 后新建的同 inode hardlink）。
    必须 scoped 到预览的具体 path。"""
    snapshot = {
        "captured_at": 1,
        "mode": "lenient",
        "items": [
            {
                "path": "/share/dl/a.mkv",
                "exists": True,
                "inode": 222,
                "size_bytes": 10,
                "mtime": 1,
                "is_dir": False,
                "real_size": 10,
                "hardlinks": ["/share/dl/a-copy.mkv"],
            }
        ],
        "all_to_delete": ["/share/dl/a.mkv", "/share/dl/a-copy.mkv"],
        "torrents": [],
        "qbit_status": {"ok": True, "message": ""},
    }
    payload = {
        "candidates": [{"path": "/share/dl/a.mkv"}],
        "snapshot": snapshot,
        "options": {"delete_torrents": False},
    }
    monkeypatch.setattr(app_module, "_build_delete_snapshot", lambda c, mode="lenient": snapshot)

    ssh_calls = []

    def fake_ssh(cmd, timeout=300):
        ssh_calls.append(cmd)
        return (0, "DELETED", "")

    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh)
    app_module._delete_executor(payload)

    joined = "\n".join(ssh_calls)
    # 不能出现全局扫 base 的 find
    assert "-xdev -inum" not in joined or "/share/dl/a.mkv" in joined
    # 必须 scoped 到预览的具体 path（item path + 它的 hardlink）
    assert "/share/dl/a.mkv" in joined
    assert "/share/dl/a-copy.mkv" in joined, "previewed hardlink not deleted"
    # 必须按 inode 校验
    assert "222" in joined


# ── #30: directory deletion re-verifies inode before rm -rf ──────────


def test_delete_executor_dir_verifies_inode_before_rm(monkeypatch):
    snapshot = {
        "captured_at": 1,
        "mode": "lenient",
        "items": [
            {
                "path": "/share/dl/somedir",
                "exists": True,
                "inode": 333,
                "size_bytes": 0,
                "mtime": 1,
                "is_dir": True,
                "real_size": 100,
                "hardlinks": [],
            }
        ],
        "all_to_delete": ["/share/dl/somedir"],
        "torrents": [],
        "qbit_status": {"ok": True, "message": ""},
    }
    payload = {
        "candidates": [{"path": "/share/dl/somedir"}],
        "snapshot": snapshot,
        "options": {"delete_torrents": False},
    }
    monkeypatch.setattr(app_module, "_build_delete_snapshot", lambda c, mode="lenient": snapshot)

    ssh_calls = []
    monkeypatch.setattr(
        app_module, "ssh_exec", lambda cmd, timeout=300: ssh_calls.append(cmd) or (0, "OK", "")
    )
    app_module._delete_executor(payload)

    joined = "\n".join(ssh_calls)
    # rm -rf 仍在，但必须先验 inode（含 stat %i 与 expected inode 333）
    assert "rm -rf" in joined
    assert "333" in joined, "dir deletion did not verify inode"
    assert "stat -c %i" in joined or "-inum 333" in joined


def test_delete_executor_dir_rm_failure_reported_as_delete_failed(monkeypatch):
    """Codex CONCERN: inode 匹配但 rm -rf 失败（RMFAIL）必须报 delete_failed，
    不能误报成 already_gone（会让 ops 以为删成功了实际目录还在）。"""
    snapshot = {
        "captured_at": 1,
        "mode": "lenient",
        "items": [
            {
                "path": "/share/dl/somedir",
                "exists": True,
                "inode": 333,
                "size_bytes": 0,
                "mtime": 1,
                "is_dir": True,
                "real_size": 100,
                "hardlinks": [],
            }
        ],
        "all_to_delete": ["/share/dl/somedir"],
        "torrents": [],
        "qbit_status": {"ok": True, "message": ""},
    }
    payload = {
        "candidates": [{"path": "/share/dl/somedir"}],
        "snapshot": snapshot,
        "options": {"delete_torrents": False},
    }
    monkeypatch.setattr(app_module, "_build_delete_snapshot", lambda c, mode="lenient": snapshot)
    # inode 匹配但 rm 失败 → shell 输出 RMFAIL
    monkeypatch.setattr(app_module, "ssh_exec", lambda cmd, timeout=300: (0, "RMFAIL", ""))
    result = app_module._delete_executor(payload)
    statuses = [r["status"] for r in result["file_results"]]
    assert statuses == ["delete_failed"], f"expected delete_failed, got {statuses}"
