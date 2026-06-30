"""原盘文件夹（BDMV/VIDEO_TS）organize：_organize_disc_folder 单测。

mock SSH 边界，覆盖 cp -al 递归 hardlink 创建路径 + already_linked + 抽样 inode 核验失败。
（真盘 e2e 只验证到 already_linked 检测——Banshees dst 2023 年就存在；create 路径靠本单测覆盖。）
"""

from __future__ import annotations

import pytest

import app as app_module


@pytest.fixture
def _mock_ssh(monkeypatch):
    """mock SSH 边界。state 记录 cp -al / mkdir / nfo 是否被调。"""
    state = {"cp_al": None, "mkdir": None, "nfo": None}

    def fake_stat(paths):
        out = {}
        for p in paths:
            if "sample.m2ts" in p:
                out[p] = {"exists": True, "inode": 42, "is_dir": False}
            elif p == state.get("dst_exists"):
                out[p] = {"exists": True, "inode": 7, "is_dir": True}
            else:
                out[p] = {"exists": False}
        return out

    def fake_ssh(cmd, timeout=30):
        if "-type f" in cmd and "head -1" in cmd:
            return (0, "/src/disc/BDMV/sample.m2ts\n", "")
        if cmd.startswith("cp -al"):
            state["cp_al"] = cmd
            return (0, "", "")
        return (0, "", "")

    def fake_nfo(src, nfo_path, kind, expected_metadata=None):
        state["nfo"] = (nfo_path, kind)
        return "created"

    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh)
    monkeypatch.setattr(app_module, "_ssh_mkdir_p", lambda p: (state.__setitem__("mkdir", p), (0, "", ""))[1])
    monkeypatch.setattr(app_module, "_write_organize_nfo", fake_nfo)
    return state


def test_disc_create_path_cp_al_and_movie_nfo(_mock_ssh):
    """dst 不存在 → mkdir 父 + cp -al + 抽样 inode 匹配 → succeeded + 写 movie.nfo。"""
    plan = {"dst_dir": "/movies/Title (2022)"}
    res = app_module._organize_disc_folder("/src/disc", plan, {"title": "Title"})
    assert res["status"] == "succeeded"
    assert res["container_type"] == "disc_folder"
    assert res["dst_path"] == "/movies/Title (2022)"
    assert res["src_inode"] == res["dst_inode"] == 42
    # cp -al 真被调用，target = dst_dir
    assert _mock_ssh["cp_al"] is not None and "/movies/Title (2022)" in _mock_ssh["cp_al"]
    # mkdir 父目录（movies_root）
    assert _mock_ssh["mkdir"] == "/movies"
    # movie.nfo 写在 dst_dir/movie.nfo
    assert _mock_ssh["nfo"] == ("/movies/Title (2022)/movie.nfo", "movie")
    assert res["nfo_status"] == "created"


def test_disc_already_linked_skips_cp_al(_mock_ssh):
    """dst 已存在且抽样 inode 一致 → already_linked，不 cp -al、不写 nfo。"""
    _mock_ssh["dst_exists"] = "/movies/Title (2022)"
    # dst 存在时抽样: src/dst 同名文件都返 inode 42 → 匹配
    plan = {"dst_dir": "/movies/Title (2022)"}
    res = app_module._organize_disc_folder("/src/disc", plan, {"title": "Title"})
    assert res["status"] == "already_linked"
    assert res["shared_inode"] == 42
    assert _mock_ssh["cp_al"] is None  # 没 cp -al
    assert _mock_ssh["nfo"] is None    # 没写 nfo


def test_disc_verify_fail_inode_mismatch(monkeypatch, _mock_ssh):
    """cp -al 后抽样 inode 不一致 → failed（hardlink 没成）。"""
    def stat_mismatch(paths):
        out = {}
        for p in paths:
            if "sample.m2ts" in p and p.startswith("/src/"):
                out[p] = {"exists": True, "inode": 42, "is_dir": False}
            elif "sample.m2ts" in p:
                out[p] = {"exists": True, "inode": 999, "is_dir": False}  # dst 不同 inode
            else:
                out[p] = {"exists": False}
        return out

    monkeypatch.setattr(app_module, "_ssh_stat_paths", stat_mismatch)
    plan = {"dst_dir": "/movies/Title (2022)"}
    res = app_module._organize_disc_folder("/src/disc", plan, {"title": "Title"})
    assert res["status"] == "failed"
    assert res["reason"] == "disc_hardlink_verify_failed"
