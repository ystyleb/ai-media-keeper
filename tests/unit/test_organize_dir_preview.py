"""Phase 4B.1: /api/organize/dir-preview contract tests.

测试目录扫描 + batch cache + 状态分类聚合。SSH / cache boundary mocked。
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def token():
    return app_module.API_TOKEN


@dataclass
class _CachedStub:
    """Stand-in for services.metadata_cache.CachedMetadata。"""

    path: str
    title: str
    media_type: str
    year: int | None = None
    tmdb_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    inode: int | None = None
    mtime: int | None = None
    original_title: str | None = None
    metadata_confidence: float | None = 0.9


def _ok_cfg(monkeypatch, movies_root="/media/movies", tv_root="/media/tv"):
    monkeypatch.setattr(
        app_module,
        "load_organize_config",
        lambda: {"movies_root": movies_root, "tv_root": tv_root},
    )


def _patch_validate_path(monkeypatch):
    monkeypatch.setattr(app_module, "validate_path", lambda p: p)


# ── error path ───────────────────────────────────────────────────


def test_dir_preview_missing_roots_returns_400(client, token, monkeypatch):
    monkeypatch.setattr(app_module, "load_organize_config", lambda: {})
    _patch_validate_path(monkeypatch)
    resp = client.get(
        "/api/organize/dir-preview?path=/dl/x",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "organize_roots_not_configured"


def test_dir_preview_empty_path_returns_400(client, token):
    resp = client.get(
        "/api/organize/dir-preview?path=",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400


def test_dir_preview_empty_directory_returns_zero_counts(client, token, monkeypatch):
    _ok_cfg(monkeypatch)
    _patch_validate_path(monkeypatch)
    monkeypatch.setattr(app_module, "_list_video_paths", lambda *a, **kw: [])
    resp = client.get(
        "/api/organize/dir-preview?path=/dl/empty",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total"] == 0
    assert body["items"] == []
    assert body["counts"]["will_link"] == 0
    assert body["counts"]["needs_identify"] == 0


# ── happy mixed path ─────────────────────────────────────────────


def test_dir_preview_classifies_mixed_directory(client, token, monkeypatch):
    """5 个文件覆盖 5 种状态：will_link / already_linked / conflict / needs_identify / unsupported"""
    _ok_cfg(monkeypatch)
    _patch_validate_path(monkeypatch)

    paths = [
        "/dl/movie.will_link.mkv",
        "/dl/movie.already_linked.mkv",
        "/dl/movie.conflict.mkv",
        "/dl/needs_identify.mkv",
        "/dl/extra.mkv",
    ]
    monkeypatch.setattr(app_module, "_list_video_paths", lambda *a, **kw: paths)

    # src stat: 全部存在，inode 各不相同
    src_inodes = {
        "/dl/movie.will_link.mkv": 100,
        "/dl/movie.already_linked.mkv": 200,
        "/dl/movie.conflict.mkv": 300,
        "/dl/needs_identify.mkv": 400,
        "/dl/extra.mkv": 500,
    }

    # cache lookup: 4 已识别，1 miss
    def fake_get_many(conn, qpaths, *, current_stats=None):
        out = {p: (None, "miss") for p in qpaths}
        out["/dl/movie.will_link.mkv"] = (
            _CachedStub(
                path="/dl/movie.will_link.mkv",
                title="Will Link",
                media_type="movie",
                year=2020,
                tmdb_id="111",
            ),
            "hit",
        )
        out["/dl/movie.already_linked.mkv"] = (
            _CachedStub(
                path="/dl/movie.already_linked.mkv",
                title="Already",
                media_type="movie",
                year=2021,
                tmdb_id="222",
            ),
            "hit",
        )
        out["/dl/movie.conflict.mkv"] = (
            _CachedStub(
                path="/dl/movie.conflict.mkv",
                title="Conflict",
                media_type="movie",
                year=2022,
                tmdb_id="333",
            ),
            "hit",
        )
        out["/dl/extra.mkv"] = (
            _CachedStub(path="/dl/extra.mkv", title="Extra Content", media_type="extra"),
            "hit",
        )
        return out

    monkeypatch.setattr(app_module.metadata_cache, "get_many_by_path", fake_get_many)

    # SSH stat: 第一次调用是 src，第二次是 dst
    call_count = {"n": 0}

    def fake_stat(qpaths):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # src batch — 都存在
            return {
                p: {
                    "exists": True,
                    "inode": src_inodes[p],
                    "size_bytes": 1024 * 1024,
                    "mtime": 1700000000,
                }
                for p in qpaths
            }
        else:
            # dst batch — already_linked 同 inode，conflict 不同 inode，will_link 不存在
            out = {}
            for q in qpaths:
                if "Already" in q:
                    out[q] = {
                        "exists": True,
                        "inode": 200,
                        "size_bytes": 0,
                        "mtime": 0,
                        "is_dir": False,
                    }
                elif "Conflict" in q:
                    out[q] = {
                        "exists": True,
                        "inode": 9999,
                        "size_bytes": 0,
                        "mtime": 0,
                        "is_dir": False,
                    }
                else:
                    out[q] = {"exists": False}
            return out

    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)

    resp = client.get(
        "/api/organize/dir-preview?path=/dl",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["counts"]["will_link"] == 1
    assert body["counts"]["already_linked"] == 1
    assert body["counts"]["conflict"] == 1
    assert body["counts"]["needs_identify"] == 1
    assert body["counts"]["unsupported"] == 1
    assert body["counts"]["not_applicable"] == 0

    # 每个 item 都带正确 status
    status_by_path = {it["path"]: it["status"] for it in body["items"]}
    assert status_by_path["/dl/movie.will_link.mkv"] == "will_link"
    assert status_by_path["/dl/movie.already_linked.mkv"] == "already_linked"
    assert status_by_path["/dl/movie.conflict.mkv"] == "conflict"
    assert status_by_path["/dl/needs_identify.mkv"] == "needs_identify"
    assert status_by_path["/dl/extra.mkv"] == "unsupported"


def test_dir_preview_uses_only_two_ssh_stat_calls(client, token, monkeypatch):
    """N=10 文件 → src batch + dst batch 共 2 次 SSH stat（性能 invariant）。"""
    _ok_cfg(monkeypatch)
    _patch_validate_path(monkeypatch)

    paths = [f"/dl/movie_{i}.mkv" for i in range(10)]
    monkeypatch.setattr(app_module, "_list_video_paths", lambda *a, **kw: paths)

    def fake_get_many(conn, qpaths, *, current_stats=None):
        return {
            p: (
                _CachedStub(path=p, title=f"M{i}", media_type="movie", year=2020, tmdb_id=str(i)),
                "hit",
            )
            for i, p in enumerate(qpaths)
        }

    monkeypatch.setattr(app_module.metadata_cache, "get_many_by_path", fake_get_many)

    call_count = {"n": 0}

    def fake_stat(qpaths):
        call_count["n"] += 1
        return (
            {
                p: {"exists": True, "inode": 100 + i, "size_bytes": 1024, "mtime": 1000}
                for i, p in enumerate(qpaths)
            }
            if call_count["n"] == 1
            else {p: {"exists": False} for p in qpaths}
        )

    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)

    resp = client.get(
        "/api/organize/dir-preview?path=/dl",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert call_count["n"] == 2  # 严格 2 次


def test_dir_preview_not_applicable_for_tv_missing_episode(client, token, monkeypatch):
    """已识别为 tv 但缺 episode_number → OrganizeNotApplicable → not_applicable。"""
    _ok_cfg(monkeypatch)
    _patch_validate_path(monkeypatch)
    monkeypatch.setattr(app_module, "_list_video_paths", lambda *a, **kw: ["/dl/show.mkv"])

    def fake_get_many(conn, qpaths, *, current_stats=None):
        # tv 但没 season/episode
        return {
            "/dl/show.mkv": (
                _CachedStub(
                    path="/dl/show.mkv",
                    title="Show",
                    media_type="tv",
                    year=2020,
                    tmdb_id="999",
                    season_number=None,
                    episode_number=None,
                ),
                "hit",
            ),
        }

    monkeypatch.setattr(app_module.metadata_cache, "get_many_by_path", fake_get_many)
    monkeypatch.setattr(
        app_module,
        "_ssh_stat_paths",
        lambda paths: {
            p: {"exists": True, "inode": 100, "size_bytes": 1024, "mtime": 1000} for p in paths
        },
    )

    resp = client.get(
        "/api/organize/dir-preview?path=/dl",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["counts"]["not_applicable"] == 1
    assert body["items"][0]["status"] == "not_applicable"
    assert (
        "episode" in body["items"][0]["reason"].lower()
        or "season" in body["items"][0]["reason"].lower()
    )


def test_dir_preview_stale_cache_treated_as_needs_identify(client, token, monkeypatch):
    """cache hit 但 mtime/inode 变了 → status=needs_identify（不能直接复用旧 cache）。"""
    _ok_cfg(monkeypatch)
    _patch_validate_path(monkeypatch)
    monkeypatch.setattr(app_module, "_list_video_paths", lambda *a, **kw: ["/dl/x.mkv"])

    def fake_get_many(conn, qpaths, *, current_stats=None):
        # 返回 stale 状态
        return {
            "/dl/x.mkv": (
                _CachedStub(
                    path="/dl/x.mkv", title="X", media_type="movie", year=2020, tmdb_id="111"
                ),
                "stale",
            ),
        }

    monkeypatch.setattr(app_module.metadata_cache, "get_many_by_path", fake_get_many)
    monkeypatch.setattr(
        app_module,
        "_ssh_stat_paths",
        lambda paths: {
            p: {"exists": True, "inode": 100, "size_bytes": 1024, "mtime": 1000} for p in paths
        },
    )

    resp = client.get(
        "/api/organize/dir-preview?path=/dl",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    assert body["counts"]["needs_identify"] == 1
    assert body["items"][0]["reason"] == "stale_cache"


def test_dir_preview_limit_reached_flag(client, token, monkeypatch):
    """find 返 limit + 1 个 path → limit_reached=True + items 切到 limit。"""
    _ok_cfg(monkeypatch)
    _patch_validate_path(monkeypatch)
    # 模拟 _list_video_paths 已经把 cap 计入但返了 limit+1 表示有更多
    monkeypatch.setattr(
        app_module,
        "_list_video_paths",
        lambda path, max_depth, limit: [f"/dl/f{i}.mkv" for i in range(limit + 1)],
    )
    monkeypatch.setattr(
        app_module.metadata_cache,
        "get_many_by_path",
        lambda c, qp, **kw: {p: (None, "miss") for p in qp},
    )
    monkeypatch.setattr(
        app_module,
        "_ssh_stat_paths",
        lambda paths: {
            p: {"exists": True, "inode": i, "size_bytes": 1024, "mtime": 1000}
            for i, p in enumerate(paths)
        },
    )

    resp = client.get(
        "/api/organize/dir-preview?path=/dl&limit=5",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    assert body["limit_reached"] is True
    assert body["total"] == 5  # 截断到 limit


def test_dir_preview_src_missing_marks_not_applicable(client, token, monkeypatch):
    """find 列出来的 path 但 SSH stat 显示不存在（race）→ not_applicable + src_missing。"""
    _ok_cfg(monkeypatch)
    _patch_validate_path(monkeypatch)
    monkeypatch.setattr(app_module, "_list_video_paths", lambda *a, **kw: ["/dl/ghost.mkv"])
    monkeypatch.setattr(
        app_module.metadata_cache,
        "get_many_by_path",
        lambda c, qp, **kw: {p: (None, "miss") for p in qp},
    )
    monkeypatch.setattr(
        app_module,
        "_ssh_stat_paths",
        lambda paths: {p: {"exists": False} for p in paths},
    )

    resp = client.get(
        "/api/organize/dir-preview?path=/dl",
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    assert body["counts"]["not_applicable"] == 1
    assert body["items"][0]["reason"] == "src_missing"
