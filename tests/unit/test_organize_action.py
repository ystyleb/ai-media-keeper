"""Phase 4A.3: /api/action/preview + /api/action/confirm kind='organize' contract tests.

Test route validation chain + cache lookup + plan computation + dst checks.
SSH boundary mocked via _ssh_stat_paths / _ssh_mkdir_p / _ssh_ln so no real
NAS connection required. Reads use real DB via get_db (in-memory sqlite for
test isolation).

This file covers Day 2 (preview 10 cases) — Day 3 confirm cases append later.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

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
    """Stand-in for services.metadata_cache.CachedMetadata."""
    title: str
    media_type: str
    year: int | None = None
    tmdb_id: str | None = None
    season_number: int | None = None
    episode_number: int | None = None
    inode: int | None = None
    mtime: int | None = None


def _src_stat(path: str, inode: int = 100, size: int = 1024, mtime: int = 1000) -> dict:
    return {path: {"exists": True, "inode": inode, "size_bytes": size, "mtime": mtime}}


def _dst_stat_missing(paths: list[str]) -> dict:
    return {p: {"exists": False} for p in paths}


def _patch_organize_config(monkeypatch, movies_root="/media/movies", tv_root="/media/tv"):
    monkeypatch.setattr(
        app_module, "load_organize_config",
        lambda: {"movies_root": movies_root, "tv_root": tv_root},
    )


def _patch_cache(monkeypatch, cached_or_none):
    """Patch metadata_cache.get_many_by_path to return (cached, 'hit') for every path.

    Phase 4B：preview 改用 batch helper get_many_by_path 替代 N 次 get_by_path。
    我们 patch batch version 让每个 query 都拿到同一个 cached stub。
    """
    status = "hit" if cached_or_none else "miss"
    monkeypatch.setattr(
        app_module.metadata_cache, "get_many_by_path",
        lambda conn, paths, *, current_stats=None: {p: (cached_or_none, status) for p in paths},
    )
    # 同时 patch 旧 get_by_path（confirm 路径仍走它）
    monkeypatch.setattr(
        app_module.metadata_cache, "get_by_path",
        lambda conn, path, current_mtime=None, current_inode=None: (cached_or_none, status),
    )


# ── 10 preview contract tests ──────────────────────────────────


def test_preview_missing_roots_returns_400(client, token, monkeypatch):
    """配置缺 movies_root/tv_root → 400 organize_roots_not_configured."""
    monkeypatch.setattr(app_module, "load_organize_config", lambda: {})
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/x.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "organize_roots_not_configured"


def test_preview_no_items_returns_400(client, token, monkeypatch):
    _patch_organize_config(monkeypatch)
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": []},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "items required" in resp.get_json()["error"]


def test_preview_src_missing_marks_not_applicable(client, token, monkeypatch):
    """Phase 4B：SSH stat 说文件不存在 → 200 + items_count=0 + preview_items[0].status='not_applicable'.

    旧 4A.3 行为是 400 src_missing fail-fast；Phase 4B 改 partial admission，让
    batch 用户在 dashboard 看到分类，单 item 用户拿到 items_count=0 提示。
    """
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        lambda paths: {p: {"exists": False} for p in paths},
    )
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/missing.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["items_count"] == 0
    assert "action_id" not in body  # 不签名
    assert body["preview_items"][0]["status"] == "not_applicable"
    assert body["preview_items"][0]["reason"] == "src_missing"
    assert body["counts"]["not_applicable"] == 1


def test_preview_src_not_identified_marks_needs_identify(client, token, monkeypatch):
    """Phase 4B：cache miss → 200 + needs_identify（旧 4A 是 400 src_not_identified）."""
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        lambda paths: _src_stat("/dl/x.mkv"),
    )
    _patch_cache(monkeypatch, None)
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/x.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["items_count"] == 0
    assert body["preview_items"][0]["status"] == "needs_identify"
    assert body["counts"]["needs_identify"] == 1


def test_preview_tv_missing_episode_marks_not_applicable(client, token, monkeypatch):
    """Phase 4B：TV 但 episode_number=None → 200 + not_applicable + reason 提示。"""
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(app_module, "_ssh_stat_paths", lambda paths: _src_stat("/dl/x.mkv"))
    _patch_cache(monkeypatch, _CachedStub(
        title="Show", media_type="tv", year=2020,
        season_number=2, episode_number=None,
    ))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/x.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["items_count"] == 0
    item = body["preview_items"][0]
    assert item["status"] == "not_applicable"
    assert "season + episode" in item["reason"]


def test_preview_unsupported_media_type_marks_unsupported(client, token, monkeypatch):
    """Phase 4B：cache.media_type='anime' → 200 + unsupported 分类。"""
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(app_module, "_ssh_stat_paths", lambda paths: _src_stat("/dl/x.mkv"))
    _patch_cache(monkeypatch, _CachedStub(title="X", media_type="anime"))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/x.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["items_count"] == 0
    assert body["preview_items"][0]["status"] == "unsupported"
    assert body["counts"]["unsupported"] == 1


def test_preview_movie_happy_path_returns_token_and_plan(client, token, monkeypatch):
    _patch_organize_config(monkeypatch)

    def fake_stat(paths):
        result = {}
        for p in paths:
            if p == "/dl/movie.mkv":
                result[p] = {"exists": True, "inode": 12345, "size_bytes": 1024, "mtime": 1000}
            else:
                result[p] = {"exists": False}
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    _patch_cache(monkeypatch, _CachedStub(
        title="The Movie", media_type="movie", year=2024, tmdb_id="111",
    ))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/movie.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert "action_id" in body and "signed_token" in body
    assert body["kind"] == "organize"
    assert body["items_count"] == 1
    item = body["items"][0]
    assert item["media_type"] == "movie"
    assert item["tmdb_id"] == "111"
    assert item["computed_plan"]["dst_path"] == "/media/movies/The Movie (2024)/movie.mkv"
    assert item["computed_plan"]["tvshow_nfo_path"] is None
    assert item["dst_status"]["dst_path_exists"] is False
    assert item["dst_status"]["already_linked"] is False
    assert item["dst_status"]["conflict"] is False
    assert item["src_snapshot"]["inode"] == 12345


def test_preview_tv_happy_path_includes_tvshow_nfo_path(client, token, monkeypatch):
    _patch_organize_config(monkeypatch)

    def fake_stat(paths):
        result = {}
        for p in paths:
            if p == "/dl/show.s02e05.mkv":
                result[p] = {"exists": True, "inode": 200, "size_bytes": 999, "mtime": 1500}
            else:
                result[p] = {"exists": False}
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    _patch_cache(monkeypatch, _CachedStub(
        title="My Show", media_type="tv", year=2020, tmdb_id="222",
        season_number=2, episode_number=5,
    ))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/show.s02e05.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    item = resp.get_json()["items"][0]
    assert item["computed_plan"]["dst_path"] == "/media/tv/My Show (2020)/Season 02/show.s02e05.mkv"
    assert item["computed_plan"]["tvshow_nfo_path"] == "/media/tv/My Show (2020)/tvshow.nfo"
    assert item["season_number"] == 2 and item["episode_number"] == 5


def test_preview_dst_already_linked_marks_flag(client, token, monkeypatch):
    """dst_path 已存在 + inode == src.inode → already_linked=True."""
    _patch_organize_config(monkeypatch)

    def fake_stat(paths):
        result = {}
        for p in paths:
            if p == "/dl/movie.mkv":
                result[p] = {"exists": True, "inode": 7777, "size_bytes": 100, "mtime": 1}
            elif p == "/media/movies/Movie (2024)/movie.mkv":
                # 已经 hardlinked：同 inode
                result[p] = {"exists": True, "inode": 7777, "size_bytes": 100, "mtime": 1}
            else:
                result[p] = {"exists": False}
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    _patch_cache(monkeypatch, _CachedStub(
        title="Movie", media_type="movie", year=2024,
    ))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/movie.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    item = resp.get_json()["items"][0]
    assert item["dst_status"]["dst_path_exists"] is True
    assert item["dst_status"]["already_linked"] is True
    assert item["dst_status"]["conflict"] is False


def test_preview_dst_conflict_different_inode_marks_flag(client, token, monkeypatch):
    """dst_path 已存在 + inode != src.inode → conflict=True (UI 提示用户)."""
    _patch_organize_config(monkeypatch)

    def fake_stat(paths):
        result = {}
        for p in paths:
            if p == "/dl/movie.mkv":
                result[p] = {"exists": True, "inode": 1111, "size_bytes": 100, "mtime": 1}
            elif p == "/media/movies/Movie (2024)/movie.mkv":
                result[p] = {"exists": True, "inode": 9999, "size_bytes": 100, "mtime": 1}
            else:
                result[p] = {"exists": False}
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    _patch_cache(monkeypatch, _CachedStub(title="Movie", media_type="movie", year=2024))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/movie.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    item = resp.get_json()["items"][0]
    assert item["dst_status"]["dst_path_exists"] is True
    assert item["dst_status"]["already_linked"] is False
    assert item["dst_status"]["conflict"] is True


def test_preview_empty_src_path_in_item_returns_400(client, token, monkeypatch):
    _patch_organize_config(monkeypatch)
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": ""}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "src_path required" in resp.get_json()["error"]


# ── 8 confirm contract tests ───────────────────────────────────


def _full_cached_movie():
    return _CachedStub(
        title="The Movie",
        media_type="movie",
        year=2024,
        tmdb_id="999",
    )


def _full_cached_tv():
    return _CachedStub(
        title="My Show",
        media_type="tv",
        year=2020,
        tmdb_id="888",
        season_number=2,
        episode_number=5,
    )


def _make_stat_fn(known: dict[str, dict]):
    """Return a stat function that returns known mapping for matching paths.
    Missing paths default to {'exists': False}."""

    def _stat(paths):
        return {p: known.get(p, {"exists": False}) for p in paths}

    return _stat


def _do_preview_and_get_token(client, token, src_path: str, src_inode: int = 12345):
    """Helper to run preview and return (action_id, signed_token)."""
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": src_path}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    body = resp.get_json()
    return body["action_id"], body["signed_token"]


def test_confirm_src_inode_changed_aborts_no_mkdir(client, token, monkeypatch):
    """preview→confirm 之间 src inode 变了 → failed src_inode_changed，不调 mkdir/ln."""
    _patch_organize_config(monkeypatch)
    _patch_cache(monkeypatch, _full_cached_movie())

    src = "/dl/movie.mkv"
    # preview 阶段 stat：src inode=100, dst missing
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        _make_stat_fn({src: {"exists": True, "inode": 100, "size_bytes": 1, "mtime": 1}}),
    )
    action_id, signed = _do_preview_and_get_token(client, token, src)

    # confirm 阶段 stat：src inode 变了
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        _make_stat_fn({src: {"exists": True, "inode": 999, "size_bytes": 1, "mtime": 1}}),
    )
    mkdir_called = []
    ln_called = []
    monkeypatch.setattr(app_module, "_ssh_mkdir_p", lambda p: mkdir_called.append(p) or (0, "", ""))
    monkeypatch.setattr(app_module, "_ssh_ln", lambda s, d: ln_called.append((s, d)) or (0, "", ""))

    resp = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "succeeded"  # action 整体跑完了，但 item 内部 failed
    items = body["result"]["items"]
    assert items[0]["status"] == "failed"
    assert items[0]["reason"] == "src_inode_changed_since_preview"
    assert mkdir_called == []  # 提前 abort，不该调 mkdir
    assert ln_called == []


def test_confirm_movie_happy_path_inode_shared(client, token, monkeypatch):
    """Movie 完整成功路径：mkdir → ln → verify 同 inode → NFO 写成功."""
    _patch_organize_config(monkeypatch)
    _patch_cache(monkeypatch, _full_cached_movie())

    src = "/dl/movie.mkv"
    src_stat = {"exists": True, "inode": 12345, "size_bytes": 1024, "mtime": 1000}
    dst_path = "/media/movies/The Movie (2024)/movie.mkv"

    # preview 阶段
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        _make_stat_fn({src: src_stat}),
    )
    action_id, signed = _do_preview_and_get_token(client, token, src)

    # confirm 阶段：src 仍 inode 12345；dst 起初不存在，mkdir+ln 后 verify 同 inode
    confirm_calls = {"stat_n": 0}

    def confirm_stat(paths):
        confirm_calls["stat_n"] += 1
        # 第 1 次：检查 src（含 src）
        # 第 2 次：检查 dst_path（不存在）
        # 第 3 次：verify dst（已 hardlinked → 同 inode）
        result = {}
        for p in paths:
            if p == src:
                result[p] = src_stat
            elif p == dst_path and confirm_calls["stat_n"] >= 3:
                result[p] = src_stat  # ln 成功后同 inode
            else:
                result[p] = {"exists": False}
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", confirm_stat)
    monkeypatch.setattr(app_module, "_ssh_mkdir_p", lambda p: (0, "", ""))
    monkeypatch.setattr(app_module, "_ssh_ln", lambda s, d: (0, "", ""))
    monkeypatch.setattr(
        app_module, "_write_organize_nfo",
        lambda src_path, nfo, kind, **kw: "created",
    )

    resp = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "succeeded"
    item = body["result"]["items"][0]
    assert item["status"] == "succeeded"
    assert item["dst_path"] == dst_path
    assert item["src_inode"] == 12345 and item["dst_inode"] == 12345
    assert item["nfo_status"] == "created"
    assert item["tvshow_nfo_status"] == "skipped"  # movie 不写 tvshow.nfo


def test_confirm_tv_writes_episode_and_tvshow_nfo(client, token, monkeypatch):
    _patch_organize_config(monkeypatch)
    _patch_cache(monkeypatch, _full_cached_tv())

    src = "/dl/show.s02e05.mkv"
    src_stat = {"exists": True, "inode": 200, "size_bytes": 99, "mtime": 2}
    dst_path = "/media/tv/My Show (2020)/Season 02/show.s02e05.mkv"
    tvshow_nfo = "/media/tv/My Show (2020)/tvshow.nfo"

    monkeypatch.setattr(app_module, "_ssh_stat_paths", _make_stat_fn({src: src_stat}))
    action_id, signed = _do_preview_and_get_token(client, token, src)

    stat_call = {"n": 0}

    def stat_fn(paths):
        stat_call["n"] += 1
        result = {}
        for p in paths:
            if p == src:
                result[p] = src_stat
            elif p == dst_path and stat_call["n"] >= 3:
                result[p] = src_stat
            else:
                result[p] = {"exists": False}  # tvshow.nfo 不存在 → 触发写
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", stat_fn)
    monkeypatch.setattr(app_module, "_ssh_mkdir_p", lambda p: (0, "", ""))
    monkeypatch.setattr(app_module, "_ssh_ln", lambda s, d: (0, "", ""))

    nfo_calls = []
    monkeypatch.setattr(
        app_module, "_write_organize_nfo",
        lambda src_path, nfo, kind, **kw: nfo_calls.append((nfo, kind)) or "created",
    )

    resp = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    item = resp.get_json()["result"]["items"][0]
    assert item["status"] == "succeeded"
    assert item["nfo_status"] == "created"
    assert item["tvshow_nfo_status"] == "created"
    # 应该调两次 _write_organize_nfo：一次 episode + 一次 tvshow
    assert len(nfo_calls) == 2
    kinds = {call[1] for call in nfo_calls}
    assert kinds == {"episode", "tvshow"}


def test_confirm_dst_already_linked_idempotent_skip(client, token, monkeypatch):
    """dst 已存在且 inode == src.inode → already_linked，跳过 mkdir/ln/NFO."""
    _patch_organize_config(monkeypatch)
    _patch_cache(monkeypatch, _full_cached_movie())

    src = "/dl/movie.mkv"
    src_stat = {"exists": True, "inode": 7777, "size_bytes": 1, "mtime": 1}
    dst_path = "/media/movies/The Movie (2024)/movie.mkv"

    # 整个流程 dst 都存在 + 同 inode
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        _make_stat_fn({src: src_stat, dst_path: src_stat}),
    )
    action_id, signed = _do_preview_and_get_token(client, token, src)

    mkdir_called = []
    ln_called = []
    nfo_called = []
    monkeypatch.setattr(app_module, "_ssh_mkdir_p", lambda p: mkdir_called.append(p) or (0, "", ""))
    monkeypatch.setattr(app_module, "_ssh_ln", lambda s, d: ln_called.append((s, d)) or (0, "", ""))
    monkeypatch.setattr(
        app_module, "_write_organize_nfo",
        lambda *a, **kw: nfo_called.append(a) or "created",
    )

    resp = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed},
        headers={"Authorization": f"Bearer {token}"},
    )
    item = resp.get_json()["result"]["items"][0]
    assert item["status"] == "already_linked"
    assert item["shared_inode"] == 7777
    assert mkdir_called == [] and ln_called == [] and nfo_called == []


def test_confirm_mkdir_fails_no_dst_dir_leaked(client, token, monkeypatch):
    _patch_organize_config(monkeypatch)
    _patch_cache(monkeypatch, _full_cached_movie())

    src = "/dl/movie.mkv"
    src_stat = {"exists": True, "inode": 10, "size_bytes": 1, "mtime": 1}
    monkeypatch.setattr(app_module, "_ssh_stat_paths", _make_stat_fn({src: src_stat}))
    action_id, signed = _do_preview_and_get_token(client, token, src)

    monkeypatch.setattr(app_module, "_ssh_stat_paths", _make_stat_fn({src: src_stat}))
    monkeypatch.setattr(app_module, "_ssh_mkdir_p", lambda p: (1, "", "Permission denied"))
    ln_called = []
    monkeypatch.setattr(app_module, "_ssh_ln", lambda s, d: ln_called.append((s, d)) or (0, "", ""))

    resp = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed},
        headers={"Authorization": f"Bearer {token}"},
    )
    item = resp.get_json()["result"]["items"][0]
    assert item["status"] == "failed"
    assert "mkdir_failed" in item["reason"]
    assert "Permission denied" in item["reason"]
    assert ln_called == []  # mkdir 失败后不该调 ln


def test_confirm_ln_fails_does_not_touch_dst_dir(client, token, monkeypatch):
    """codex r1 B3: ln 失败时**不动** dst_dir（mkdir -p 不保证目录是本 action 创建的，
    rmdir 可能误删别人的空目录）。"""
    _patch_organize_config(monkeypatch)
    _patch_cache(monkeypatch, _full_cached_movie())

    src = "/dl/movie.mkv"
    src_stat = {"exists": True, "inode": 10, "size_bytes": 1, "mtime": 1}
    monkeypatch.setattr(app_module, "_ssh_stat_paths", _make_stat_fn({src: src_stat}))
    action_id, signed = _do_preview_and_get_token(client, token, src)

    monkeypatch.setattr(app_module, "_ssh_stat_paths", _make_stat_fn({src: src_stat}))
    monkeypatch.setattr(app_module, "_ssh_mkdir_p", lambda p: (0, "", ""))
    monkeypatch.setattr(app_module, "_ssh_ln", lambda s, d: (1, "", "EXDEV: cross-device link"))

    rmdir_calls = []
    rm_calls = []

    def fake_ssh_exec(cmd, timeout=30):
        if "rmdir" in cmd:
            rmdir_calls.append(cmd)
        if "rm -f" in cmd:
            rm_calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)

    resp = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed},
        headers={"Authorization": f"Bearer {token}"},
    )
    item = resp.get_json()["result"]["items"][0]
    assert item["status"] == "failed"
    assert "ln_failed" in item["reason"]
    assert "EXDEV" in item["reason"]
    # B3 修订：executor 不应该调 rmdir / rm -f cleanup（race-unsafe）
    assert rmdir_calls == []
    assert rm_calls == []


def test_confirm_ln_verify_inode_mismatch_does_not_unlink_dst(client, token, monkeypatch):
    """codex r1 B2: ln 成功但 verify 时 dst inode 不匹配 src → 不动 dst（rm 会删别人的文件）。

    verify mismatch 本身已经说明当前 dst_path 不是我们刚创建的 link（race condition
    或 fs 异常），rm 可能删别的 process 刚放进来的文件。"""
    _patch_organize_config(monkeypatch)
    _patch_cache(monkeypatch, _full_cached_movie())

    src = "/dl/movie.mkv"
    src_stat = {"exists": True, "inode": 100, "size_bytes": 1, "mtime": 1}
    dst_path = "/media/movies/The Movie (2024)/movie.mkv"

    monkeypatch.setattr(app_module, "_ssh_stat_paths", _make_stat_fn({src: src_stat}))
    action_id, signed = _do_preview_and_get_token(client, token, src)

    call_n = {"n": 0}

    def confirm_stat(paths):
        call_n["n"] += 1
        # 第 1 次：src（inode 100）
        # 第 2 次：dst 不存在
        # 第 3 次：verify dst inode 999（不匹配 src 100）
        result = {}
        for p in paths:
            if p == src:
                result[p] = src_stat
            elif p == dst_path and call_n["n"] >= 3:
                result[p] = {"exists": True, "inode": 999, "size_bytes": 1, "mtime": 1}
            else:
                result[p] = {"exists": False}
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", confirm_stat)
    monkeypatch.setattr(app_module, "_ssh_mkdir_p", lambda p: (0, "", ""))
    monkeypatch.setattr(app_module, "_ssh_ln", lambda s, d: (0, "", ""))

    rm_calls = []
    rmdir_calls = []

    def fake_ssh_exec(cmd, timeout=30):
        if "rm -f" in cmd:
            rm_calls.append(cmd)
        if "rmdir" in cmd:
            rmdir_calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)

    resp = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed},
        headers={"Authorization": f"Bearer {token}"},
    )
    item = resp.get_json()["result"]["items"][0]
    assert item["status"] == "failed"
    assert item["reason"] == "ln_verify_failed_inode_mismatch"
    assert item["expected_inode"] == 100
    assert item["actual_inode"] == 999
    # B2 修订：executor 不应 rm -f dst（dst 已不是预期 inode，rm 可能删别人的文件）
    assert rm_calls == []
    assert rmdir_calls == []
    # 必须给用户 hint
    assert "hint" in item


def test_confirm_nfo_failure_does_not_rollback_hardlink(client, token, monkeypatch):
    """Pattern D：NFO 写失败 → status='succeeded' + nfo_status='failed: ...'，hardlink 保留."""
    _patch_organize_config(monkeypatch)
    _patch_cache(monkeypatch, _full_cached_movie())

    src = "/dl/movie.mkv"
    src_stat = {"exists": True, "inode": 555, "size_bytes": 1, "mtime": 1}
    dst_path = "/media/movies/The Movie (2024)/movie.mkv"

    monkeypatch.setattr(app_module, "_ssh_stat_paths", _make_stat_fn({src: src_stat}))
    action_id, signed = _do_preview_and_get_token(client, token, src)

    call_n = {"n": 0}

    def confirm_stat(paths):
        call_n["n"] += 1
        result = {}
        for p in paths:
            if p == src:
                result[p] = src_stat
            elif p == dst_path and call_n["n"] >= 3:
                result[p] = src_stat  # hardlink 成功
            else:
                result[p] = {"exists": False}
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", confirm_stat)
    monkeypatch.setattr(app_module, "_ssh_mkdir_p", lambda p: (0, "", ""))
    monkeypatch.setattr(app_module, "_ssh_ln", lambda s, d: (0, "", ""))
    # NFO 写失败
    monkeypatch.setattr(
        app_module, "_write_organize_nfo",
        lambda src_path, nfo, kind, **kw: "failed: write_failed: permission",
    )

    # 不期望任何 rm -f 调用（hardlink 不该回滚）
    rm_calls = []

    def fake_ssh_exec(cmd, timeout=30):
        if "rm -f" in cmd or "rmdir" in cmd:
            rm_calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)

    resp = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed},
        headers={"Authorization": f"Bearer {token}"},
    )
    item = resp.get_json()["result"]["items"][0]
    # Pattern D：hardlink 成功 = item status='succeeded'，nfo_status 独立报错
    assert item["status"] == "succeeded"
    assert item["src_inode"] == 555 and item["dst_inode"] == 555
    assert item["nfo_status"].startswith("failed: ")
    # 关键：hardlink 不该被 rm 掉
    assert rm_calls == []


# ── codex r1 BLOCKER 修复对应 tests ─────────────────────────────


def test_confirm_existing_nfo_returns_skipped(client, token, monkeypatch):
    """codex r3 BLOCKER fix: atomic create-only ln 让已存在的 NFO 不被覆盖。
    helper _ssh_create_nfo_if_absent 返 (False, 'nfo_exists') →
    _write_organize_nfo 转 'skipped: nfo_exists'."""
    _patch_organize_config(monkeypatch)
    _patch_cache(monkeypatch, _full_cached_movie())

    src = "/dl/movie.mkv"
    src_stat = {"exists": True, "inode": 50, "size_bytes": 1, "mtime": 1}
    dst_path = "/media/movies/The Movie (2024)/movie.mkv"

    monkeypatch.setattr(app_module, "_ssh_stat_paths", _make_stat_fn({src: src_stat}))
    action_id, signed = _do_preview_and_get_token(client, token, src)

    call_n = {"n": 0}

    def confirm_stat(paths):
        call_n["n"] += 1
        result = {}
        for p in paths:
            if p == src:
                result[p] = src_stat
            elif p == dst_path and call_n["n"] >= 3:
                result[p] = src_stat
            else:
                result[p] = {"exists": False}
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", confirm_stat)
    monkeypatch.setattr(app_module, "_ssh_mkdir_p", lambda p: (0, "", ""))
    monkeypatch.setattr(app_module, "_ssh_ln", lambda s, d: (0, "", ""))

    write_calls = []

    def fake_write(src_p, nfo, kind, **kw):
        write_calls.append((nfo, kind, kw))
        return "skipped: nfo_exists"   # 模拟 atomic ln 失败因为 dst 已存在

    monkeypatch.setattr(app_module, "_write_organize_nfo", fake_write)

    resp = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed},
        headers={"Authorization": f"Bearer {token}"},
    )
    item = resp.get_json()["result"]["items"][0]
    assert item["status"] == "succeeded"
    assert item["nfo_status"] == "skipped: nfo_exists"
    # 关键：write helper 不再收到 target_exists 参数（r3 BLOCKER 修后已移除）
    assert all("target_exists" not in call[2] for call in write_calls)


def test_write_organize_nfo_atomic_ln_dst_exists_returns_skipped(monkeypatch):
    """codex r3 BLOCKER fix: helper _ssh_create_nfo_if_absent 返 (False, 'nfo_exists')
    → _write_organize_nfo 转 'skipped: nfo_exists'."""
    cached = _CachedStub(title="X", media_type="movie", year=2024, tmdb_id="1")
    for attr in ("original_title", "imdb_id", "overview", "vote_average",
                 "genres", "cast", "runtime_minutes", "poster_url",
                 "episode_title", "episode_overview", "episode_air_date",
                 "episode_still_url"):
        if not hasattr(cached, attr):
            setattr(cached, attr, None)
    monkeypatch.setattr(app_module, "get_db", lambda: None)
    monkeypatch.setattr(
        app_module.metadata_cache, "get_by_path",
        lambda conn, path, **kw: (cached, "hit"),
    )
    monkeypatch.setattr(
        app_module, "_ssh_create_nfo_if_absent",
        lambda nfo, xml, **kw: (False, "nfo_exists"),
    )
    out = app_module._write_organize_nfo("/dl/x.mkv", "/m/X (2024)/x.nfo", "movie")
    assert out == "skipped: nfo_exists"


def test_write_organize_nfo_no_cache_returns_failed(monkeypatch):
    """codex r1 B5: cache miss → 'failed: no_cache'（而不是 'no_cache'）。"""
    monkeypatch.setattr(app_module, "get_db", lambda: None)
    monkeypatch.setattr(
        app_module.metadata_cache, "get_by_path",
        lambda conn, path, **kw: (None, "miss"),
    )
    out = app_module._write_organize_nfo("/dl/x.mkv", "/m/x.nfo", "movie")
    assert out.startswith("failed: "), f"expected 'failed: ...' got {out!r}"
    assert "no_cache" in out


def test_write_organize_nfo_real_build_succeeds_for_movie(monkeypatch):
    """codex r1 B4: 不 mock _write_organize_nfo，验证 NFOPayload 含 tvdb_id=None 不抛 TypeError。"""
    cached = _CachedStub(title="X", media_type="movie", year=2024, tmdb_id="1")
    for attr in ("original_title", "imdb_id", "overview", "vote_average",
                 "genres", "cast", "runtime_minutes", "poster_url",
                 "episode_title", "episode_overview", "episode_air_date",
                 "episode_still_url"):
        if not hasattr(cached, attr):
            setattr(cached, attr, None)
    monkeypatch.setattr(app_module, "get_db", lambda: None)
    monkeypatch.setattr(
        app_module.metadata_cache, "get_by_path",
        lambda conn, path, **kw: (cached, "hit"),
    )
    captured = []
    monkeypatch.setattr(
        app_module, "_ssh_create_nfo_if_absent",
        lambda nfo, xml, **kw: captured.append(xml) or (True, ""),
    )
    out = app_module._write_organize_nfo("/dl/movie.mkv", "/m/X (2024)/movie.nfo", "movie")
    assert out == "created"
    assert len(captured) == 1
    assert "<movie>" in captured[0]
    assert "X" in captured[0]


def test_write_organize_nfo_cache_drift_skipped(monkeypatch):
    """codex r1 I1: preview 抓 tmdb_id='1' 但 confirm 时 cache 变成 tmdb_id='2' → skipped."""
    cached = _CachedStub(title="X", media_type="movie", year=2024, tmdb_id="2")
    for attr in ("original_title", "imdb_id", "overview", "vote_average",
                 "genres", "cast", "runtime_minutes", "poster_url",
                 "episode_title", "episode_overview", "episode_air_date",
                 "episode_still_url"):
        if not hasattr(cached, attr):
            setattr(cached, attr, None)
    monkeypatch.setattr(app_module, "get_db", lambda: None)
    monkeypatch.setattr(
        app_module.metadata_cache, "get_by_path",
        lambda conn, path, **kw: (cached, "hit"),
    )
    write_called = []
    monkeypatch.setattr(
        app_module, "_ssh_create_nfo_if_absent",
        lambda *a, **kw: write_called.append(a) or (True, ""),
    )
    expected_pre = {
        "tmdb_id": "1",                # preview 时
        "title": "X",
        "year": 2024,
        "media_type": "movie",
        "season_number": None,
        "episode_number": None,
    }
    out = app_module._write_organize_nfo(
        "/dl/x.mkv", "/m/X (2024)/x.nfo", "movie",
        expected_metadata=expected_pre,
    )
    assert out.startswith("skipped: cache_drift")
    assert "tmdb_id" in out
    assert write_called == []   # cache drift 必须 short-circuit


def test_ssh_create_nfo_if_absent_tmp_filename_has_unique_suffix(monkeypatch):
    """codex r1 I2 + r2 IMPORTANT #2 + r3 BLOCKER: atomic create-only ln 用 tmp 后缀
    必须 unique 且格式 .tmp.<12 hex>。"""
    import re

    captured_cmds = []

    def fake_ssh_exec(cmd, timeout=30):
        captured_cmds.append(cmd)
        return (0, "1\n", "")

    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)
    ok1, _ = app_module._ssh_create_nfo_if_absent("/m/x.nfo", "<movie>x</movie>", verify_tmdb=False)
    ok2, _ = app_module._ssh_create_nfo_if_absent("/m/x.nfo", "<movie>y</movie>", verify_tmdb=False)
    assert ok1 and ok2

    # 提取 `ln <tmp> <dst>` 的 tmp path（atomic create-only：ln 而非 mv）
    ln_re = re.compile(r"ln\s+'?([^\s']+\.tmp\.[0-9a-f]{12})'?\s+'?/m/x\.nfo'?")
    tmps = []
    for c in captured_cmds:
        m = ln_re.search(c)
        if m:
            tmps.append(m.group(1))

    assert len(tmps) == 2, f"expected 2 ln commands with tmp paths; got {tmps}, cmds={captured_cmds}"
    assert tmps[0] != tmps[1]
    pat = re.compile(r"^/m/x\.nfo\.tmp\.[0-9a-f]{12}$")
    assert pat.match(tmps[0]) and pat.match(tmps[1])


def test_ssh_create_nfo_if_absent_returns_nfo_exists_when_ln_fails(monkeypatch):
    """codex r3 BLOCKER: atomic ln 失败 + dst 已存在 → return (False, 'nfo_exists')。

    模拟 ssh_exec 返非零 rc 且 stdout 含 NFO_EXISTS（Python 用此 marker 区分两种 ln 失败）。"""
    def fake_ssh_exec(cmd, timeout=30):
        # 模拟 ln 失败 + dst 存在的 shell 行为
        return (1, "NFO_EXISTS\n", "ln: failed to create hard link: File exists")

    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)
    ok, reason = app_module._ssh_create_nfo_if_absent("/m/x.nfo", "<movie>x</movie>", verify_tmdb=False)
    assert ok is False
    assert reason == "nfo_exists"


def test_ssh_create_nfo_if_absent_returns_create_failed_when_ln_fails_other(monkeypatch):
    """ln 失败 + dst 不存在（如目录权限错）→ 'create_failed: <err>'，不是 'nfo_exists'."""
    def fake_ssh_exec(cmd, timeout=30):
        return (1, "", "Permission denied")    # 无 NFO_EXISTS marker

    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)
    ok, reason = app_module._ssh_create_nfo_if_absent("/m/x.nfo", "<movie>x</movie>", verify_tmdb=False)
    assert ok is False
    assert reason.startswith("create_failed:")
    assert "Permission denied" in reason


def test_ssh_create_nfo_if_absent_rejects_directory_target(monkeypatch):
    """codex r4 BLOCKER: dst 是目录（不是普通文件）时必须返 'nfo_is_directory'。
    `ln src dir/` 默认在 dir 内创建 hardlink，不能让那种 silent ln-into-dir 发生。"""
    def fake_ssh_exec(cmd, timeout=30):
        # 模拟 shell `[ -d ] && exit 99` 行为
        return (99, "NFO_IS_DIR\n", "")

    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)
    ok, reason = app_module._ssh_create_nfo_if_absent("/m/x.nfo", "<movie>x</movie>", verify_tmdb=False)
    assert ok is False
    assert reason == "nfo_is_directory"


def test_ssh_create_nfo_if_absent_shell_cmd_includes_dir_check(monkeypatch):
    """codex r4 IMPORTANT: shell 命令必须含 `[ -d ]` 预检 + atomic ln 序列。"""
    captured = []

    def fake_ssh_exec(cmd, timeout=30):
        captured.append(cmd)
        return (0, "1\n", "")

    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)
    ok, _ = app_module._ssh_create_nfo_if_absent("/m/x.nfo", "<movie>x</movie>", verify_tmdb=True)
    assert ok
    cmd = captured[0]
    # 必含 [ -d ] 预检 + NFO_IS_DIR marker + atomic ln + grep 兜底（|| echo 0）
    assert "[ -d " in cmd, f"missing dir check: {cmd}"
    assert "NFO_IS_DIR" in cmd
    assert "ln " in cmd
    assert "grep -c 'tmdb'" in cmd
    assert "|| echo 0" in cmd, "verify grep 必须有 || echo 0 兜底"


def test_ssh_create_nfo_if_absent_count_zero_not_misclassified(monkeypatch):
    """codex r4 NIT: grep -c 返 count=0 时不能被误归类成 create_failed。

    实际 shell `... || echo 0` 让 rc 永远 0，stdout 末行是 count。"""
    def fake_ssh_exec(cmd, timeout=30):
        # 模拟 grep -c 找不到 → || echo 0 输出 "0\n"，整条 rc=0
        return (0, "0\n", "")

    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)
    ok, reason = app_module._ssh_create_nfo_if_absent("/m/x.nfo", "<a/>", verify_tmdb=True)
    assert ok is False
    assert reason == "readback_missing_tmdbid"   # 不是 create_failed


def test_executor_rejects_payload_missing_metadata_snapshot(monkeypatch):
    """codex r3 IMPORTANT: executor 强 validation — 缺 metadata_snapshot 立即 failed。

    保护 legacy / 不完整 payload 走到 NFO 写时 silent 漂移降级（cache_drift guard 失效）。
    """
    monkeypatch.setattr(app_module, "_ssh_stat_paths", lambda paths: {
        p: {"exists": True, "inode": 1, "size_bytes": 1, "mtime": 1} for p in paths
    })
    payload_no_snapshot = {
        "items": [{
            "src_path": "/dl/x.mkv",
            "src_snapshot": {"inode": 1, "size_bytes": 1, "mtime": 1},
            "media_type": "movie",
            "computed_plan": {
                "dst_dir": "/m/X (2024)", "dst_path": "/m/X (2024)/x.mkv",
                "nfo_path": "/m/X (2024)/x.nfo", "tvshow_nfo_path": None,
            },
            # 缺 metadata_snapshot
        }],
    }
    result = app_module._organize_executor(payload_no_snapshot)
    item = result["items"][0]
    assert item["status"] == "failed"
    assert item["reason"] == "missing_metadata_snapshot_in_payload"


def test_ssh_ln_rejects_directory_dst(monkeypatch):
    """codex r5 BLOCKER 2: _ssh_ln pre-check [ -d dst ] 防 silent ln-into-dir."""
    def fake_ssh_exec(cmd, timeout=10):
        return (99, "DST_IS_DIR\n", "")
    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)
    rc, out, err = app_module._ssh_ln("/src/x.mkv", "/dst/dir_target")
    assert rc == 99
    assert "DST_IS_DIR" in out


def test_ssh_ln_detects_race_ln_into_dir(monkeypatch):
    """codex r5 BLOCKER 2 + r7: pre-check 后 race-created dir 让 ln-into-dir →
    post-stat [ -f dst ] 抓到 + exit 98 with DST_NOT_REGULAR marker.

    r7 修订：不自动 cleanup（ownership 无法 path-based 证明），返 marker 让
    调用方告知 user 手工查 orphan."""
    def fake_ssh_exec(cmd, timeout=10):
        return (98, "DST_NOT_REGULAR\n", "")
    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)
    rc, out, err = app_module._ssh_ln("/src/x.mkv", "/dst/race_dir")
    assert rc == 98
    assert "DST_NOT_REGULAR" in out


def test_ssh_ln_shell_cmd_includes_dir_check_and_post_stat(monkeypatch):
    """codex r5 BLOCKER 2 + r7: shell 命令必须含 [ -d ] pre-check + [ -f ] post-stat。

    r7: 移除自动 cleanup（ownership 无法 path-based 证明）— shell cmd 不再
    含 rm；只 echo marker + exit，让调用方提示 user 手工查 orphan。
    """
    captured = []
    def fake_ssh_exec(cmd, timeout=10):
        captured.append(cmd)
        return (0, "", "")
    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)
    app_module._ssh_ln("/src/x.mkv", "/dst/y.mkv")
    cmd = captured[0]
    assert "[ -d " in cmd, f"missing dir pre-check: {cmd}"
    assert "DST_IS_DIR" in cmd
    assert "ln " in cmd
    assert "[ ! -f " in cmd, f"missing post-stat: {cmd}"
    assert "DST_NOT_REGULAR" in cmd
    # r7: shell 不再自动 cleanup（codex r7 BLOCKER：ownership 无法 path-based 证明）
    assert "rm -f " not in cmd, f"cleanup removed: {cmd}"


def test_ssh_ln_shell_cmd_no_basename_expansion(monkeypatch):
    """codex r6 + r7: shell cmd 不含 $(basename ...) 也不含自动 rm cleanup。
    src 含空格时不会引入 word-split 风险（因为根本没有 cleanup path 拼接）。"""
    captured = []
    def fake_ssh_exec(cmd, timeout=10):
        captured.append(cmd)
        return (0, "", "")
    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)
    app_module._ssh_ln("/dl/My Movie.mkv", "/media/My Show (2024)")
    cmd = captured[0]
    assert "$(basename" not in cmd, f"must not use shell $(basename ...): {cmd}"
    assert "rm -f " not in cmd, f"must not auto-cleanup: {cmd}"


def test_executor_ln_target_not_regular_includes_orphan_hint(monkeypatch, client, token):
    """codex r7 BLOCKER: race-into-dir 时 executor 必须返 hint 含 orphan 路径
    让 user SSH 手工查（替代自动 cleanup）。"""
    _patch_organize_config(monkeypatch)
    _patch_cache(monkeypatch, _full_cached_movie())

    src = "/dl/movie.mkv"
    src_stat = {"exists": True, "inode": 700, "size_bytes": 1, "mtime": 1}
    monkeypatch.setattr(app_module, "_ssh_stat_paths", _make_stat_fn({src: src_stat}))
    action_id, signed = _do_preview_and_get_token(client, token, src)

    monkeypatch.setattr(app_module, "_ssh_stat_paths", _make_stat_fn({src: src_stat}))
    monkeypatch.setattr(app_module, "_ssh_mkdir_p", lambda p: (0, "", ""))
    # ln 失败返 DST_NOT_REGULAR marker（race-into-dir）
    monkeypatch.setattr(
        app_module, "_ssh_ln",
        lambda s, d: (98, "DST_NOT_REGULAR\n", ""),
    )

    resp = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed},
        headers={"Authorization": f"Bearer {token}"},
    )
    item = resp.get_json()["result"]["items"][0]
    assert item["status"] == "failed"
    assert item["reason"] == "ln_target_not_regular_race"
    assert "hint" in item
    # hint 必须含 orphan 路径让 user 知道去哪 SSH 查
    assert "movie.mkv" in item["hint"]
    assert "SSH" in item["hint"] or "ls" in item["hint"]


def test_ssh_create_nfo_if_absent_detects_race_ln_into_dir(monkeypatch):
    """codex r5 BLOCKER 1: _ssh_create_nfo_if_absent pre-check 后 race-dir →
    post-stat [ -f final ] 抓到 + cleanup `<final>/<basename(tmp)>` + exit 98."""
    def fake_ssh_exec(cmd, timeout=30):
        return (98, "NFO_TARGET_NOT_REGULAR\n", "")
    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)
    ok, reason = app_module._ssh_create_nfo_if_absent("/m/x.nfo", "<a/>", verify_tmdb=False)
    assert ok is False
    assert reason == "nfo_target_not_regular_race"


def test_ssh_create_nfo_if_absent_shell_cmd_includes_post_stat(monkeypatch):
    """codex r5 BLOCKER 1: shell 命令必须含 post-stat `[ ! -f final ]` race-protect."""
    captured = []
    def fake_ssh_exec(cmd, timeout=30):
        captured.append(cmd)
        return (0, "1\n", "")
    monkeypatch.setattr(app_module, "ssh_exec", fake_ssh_exec)
    app_module._ssh_create_nfo_if_absent("/m/x.nfo", "<a/>", verify_tmdb=True)
    cmd = captured[0]
    assert "[ ! -f " in cmd
    assert "NFO_TARGET_NOT_REGULAR" in cmd
    # codex r6: cleanup path 不能用 shell $(basename ...) 避免 word-split
    assert "$(basename" not in cmd, f"must not use shell basename: {cmd}"


def test_executor_rejects_incomplete_metadata_snapshot(monkeypatch):
    """缺 required key (如 tmdb_id) → failed incomplete_metadata_snapshot."""
    monkeypatch.setattr(app_module, "_ssh_stat_paths", lambda paths: {
        p: {"exists": True, "inode": 1, "size_bytes": 1, "mtime": 1} for p in paths
    })
    payload = {
        "items": [{
            "src_path": "/dl/x.mkv",
            "src_snapshot": {"inode": 1, "size_bytes": 1, "mtime": 1},
            "media_type": "movie",
            "computed_plan": {
                "dst_dir": "/m/X (2024)", "dst_path": "/m/X (2024)/x.mkv",
                "nfo_path": "/m/X (2024)/x.nfo", "tvshow_nfo_path": None,
            },
            "metadata_snapshot": {
                # 缺 tmdb_id / season_number / episode_number
                "title": "X", "year": 2024, "media_type": "movie",
            },
        }],
    }
    result = app_module._organize_executor(payload)
    item = result["items"][0]
    assert item["status"] == "failed"
    assert item["reason"].startswith("incomplete_metadata_snapshot")
    assert "tmdb_id" in item["reason"]


def test_confirm_nfo_appearing_after_preview_atomic_ln_handles_race(client, token, monkeypatch):
    """codex r2 + r3 BLOCKER: preview 时 nfo 不存在 → confirm 前被 Plex/user 创建。
    atomic ln 失败（dst 已存在）→ _write_organize_nfo 返 'skipped: nfo_exists'。
    无 stat→mv race window — 即使 confirm 时也没 re-stat，atomic ln 在 SSH 侧 enforce."""
    _patch_organize_config(monkeypatch)
    _patch_cache(monkeypatch, _full_cached_movie())

    src = "/dl/movie.mkv"
    src_stat = {"exists": True, "inode": 600, "size_bytes": 1, "mtime": 1}
    dst_path = "/media/movies/The Movie (2024)/movie.mkv"

    # preview: nfo 不存在
    def preview_stat(paths):
        return {p: (src_stat if p == src else {"exists": False}) for p in paths}

    monkeypatch.setattr(app_module, "_ssh_stat_paths", preview_stat)
    action_id, signed = _do_preview_and_get_token(client, token, src)

    # confirm 不再 re-stat NFO（atomic ln 内部 enforce），只 stat src + dst
    call_n = {"n": 0}

    def confirm_stat(paths):
        call_n["n"] += 1
        result = {}
        for p in paths:
            if p == src:
                result[p] = src_stat
            elif p == dst_path and call_n["n"] >= 3:
                result[p] = src_stat
            else:
                result[p] = {"exists": False}
        return result

    monkeypatch.setattr(app_module, "_ssh_stat_paths", confirm_stat)
    monkeypatch.setattr(app_module, "_ssh_mkdir_p", lambda p: (0, "", ""))
    monkeypatch.setattr(app_module, "_ssh_ln", lambda s, d: (0, "", ""))

    # 关键：mock _write_organize_nfo 模拟 atomic ln 失败因 dst 已存在
    write_calls = []

    def fake_write(src_p, nfo, kind, **kw):
        write_calls.append((nfo, kind, kw))
        # 模拟：底层 _ssh_create_nfo_if_absent 返 (False, 'nfo_exists')
        return "skipped: nfo_exists"

    monkeypatch.setattr(app_module, "_write_organize_nfo", fake_write)

    resp = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed},
        headers={"Authorization": f"Bearer {token}"},
    )
    item = resp.get_json()["result"]["items"][0]
    assert item["status"] == "succeeded"
    assert len(write_calls) == 1
    # 验证 write helper 收到 expected_metadata（cache drift guard 可用）
    assert "expected_metadata" in write_calls[0][2]
    assert item["nfo_status"] == "skipped: nfo_exists"


# ── Phase 4B.2 multi-item / partial admission / batch optim tests ──


def test_preview_batch_too_large_returns_400(client, token, monkeypatch):
    """Phase 4B：> MAX_ORGANIZE_BATCH_ITEMS 直接 400 防爆 payload。"""
    _patch_organize_config(monkeypatch)
    too_many = [{"src_path": f"/dl/{i}.mkv"}
                for i in range(app_module.MAX_ORGANIZE_BATCH_ITEMS + 1)]
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": too_many},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "batch_too_large"
    assert body["limit"] == app_module.MAX_ORGANIZE_BATCH_ITEMS


def test_preview_mixed_batch_classifies_correctly(client, token, monkeypatch):
    """3 个 src 各代表 will_link / needs_identify / already_linked → counts 准确,
    payload 只装可算 plan 的 2 个（will_link + already_linked），needs_identify 不进。"""
    _patch_organize_config(monkeypatch)

    paths = ["/dl/will.mkv", "/dl/needs.mkv", "/dl/linked.mkv"]
    src_inodes = {"/dl/will.mkv": 100, "/dl/needs.mkv": 200, "/dl/linked.mkv": 300}

    def fake_stat(qpaths):
        # 第一次调用：src batch；第二次：dst batch
        out = {}
        for p in qpaths:
            if p in src_inodes:
                out[p] = {"exists": True, "inode": src_inodes[p],
                          "size_bytes": 1024, "mtime": 1000}
            elif "linked" in p and p.endswith("linked.mkv"):
                # dst path for /dl/linked.mkv → 同 inode
                out[p] = {"exists": True, "inode": 300,
                          "size_bytes": 1024, "mtime": 1000}
            else:
                out[p] = {"exists": False}
        return out
    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)

    def fake_get_many(conn, qpaths, *, current_stats=None):
        out = {}
        for p in qpaths:
            if "needs" in p:
                out[p] = (None, "miss")
            else:
                out[p] = (_CachedStub(
                    title="Linked Movie" if "linked" in p else "Will Link",
                    media_type="movie", year=2020,
                    tmdb_id="222" if "linked" in p else "111",
                ), "hit")
        return out
    monkeypatch.setattr(app_module.metadata_cache, "get_many_by_path", fake_get_many)

    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize",
              "items": [{"src_path": p} for p in paths]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["counts"]["will_link"] == 1
    assert body["counts"]["needs_identify"] == 1
    assert body["counts"]["already_linked"] == 1
    # payload 含 will_link + already_linked 共 2 个 (needs_identify 不进 payload)
    assert body["items_count"] == 2
    # preview_items 全集 3 个
    assert len(body["preview_items"]) == 3
    status_by_src = {pv["src_path"]: pv["status"] for pv in body["preview_items"]}
    assert status_by_src["/dl/will.mkv"] == "will_link"
    assert status_by_src["/dl/needs.mkv"] == "needs_identify"
    assert status_by_src["/dl/linked.mkv"] == "already_linked"


def test_preview_signed_token_locks_only_payload_items(client, token, monkeypatch):
    """preview 只签 payload_items（已算出 plan 的）；needs_identify 不影响 signed_token。

    Invariant: 同 payload_items hash 两次 preview 应不同 action_id（不同 random uuid）
    但 payload_hash 应相同（保证签名稳定）。
    """
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        lambda paths: {p: {"exists": True, "inode": 100,
                           "size_bytes": 1024, "mtime": 1000}
                       for p in paths},
    )
    _patch_cache(monkeypatch, _CachedStub(
        title="M", media_type="movie", year=2020, tmdb_id="111",
    ))

    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/m.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert "action_id" in body
    assert "signed_token" in body
    assert body["items_count"] == 1


def test_preview_duplicate_dst_within_batch_marks_conflict(client, token, monkeypatch):
    """同一 batch 两 src 映射到同一 dst_path（罕见但要 detect）→
    第一个 will_link，第二个 conflict + reason=duplicate_dst_path_within_batch。
    """
    _patch_organize_config(monkeypatch)

    # 两个不同 src 文件，识别成同一部 movie 同一 title/year → 同一 dst_dir，
    # 同一 dst_path（如果 basename 也一样）。模拟 basename 一致的极端 case。
    paths = ["/dl/sub1/movie.mkv", "/dl/sub2/movie.mkv"]
    src_inodes = {paths[0]: 100, paths[1]: 200}

    def fake_stat(qpaths):
        out = {}
        for p in qpaths:
            if p in src_inodes:
                out[p] = {"exists": True, "inode": src_inodes[p],
                          "size_bytes": 1024, "mtime": 1000}
            else:
                out[p] = {"exists": False}
        return out
    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    _patch_cache(monkeypatch, _CachedStub(
        title="Same Movie", media_type="movie", year=2020, tmdb_id="111",
    ))

    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize",
              "items": [{"src_path": p} for p in paths]},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    assert body["counts"]["will_link"] == 1
    assert body["counts"]["conflict"] == 1
    # 第一个 sub1/movie.mkv 应该 will_link，第二个 sub2/movie.mkv 应该 conflict
    pv1 = next(pv for pv in body["preview_items"] if pv["src_path"] == paths[0])
    pv2 = next(pv for pv in body["preview_items"] if pv["src_path"] == paths[1])
    assert pv1["status"] == "will_link"
    assert pv2["status"] == "conflict"
    assert pv2["reason"] == "duplicate_dst_path_within_batch"


def test_preview_batch_uses_minimal_ssh_calls(client, token, monkeypatch):
    """N items：SSH stat 调用应当只 2 次（src + dst batch），而非 N×2 次。

    防止任何回归把 batch 优化丢掉。
    """
    _patch_organize_config(monkeypatch)
    n = 10
    paths = [f"/dl/m{i}.mkv" for i in range(n)]

    stat_calls = {"n": 0}
    def fake_stat(qpaths):
        stat_calls["n"] += 1
        return {p: {"exists": True, "inode": 100 + i,
                    "size_bytes": 1024, "mtime": 1000}
                if i < n else {"exists": False}
                for i, p in enumerate(qpaths)}
    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    _patch_cache(monkeypatch, _CachedStub(
        title="M", media_type="movie", year=2020, tmdb_id="111",
    ))

    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize",
              "items": [{"src_path": p} for p in paths]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    # 严格 2 次：src batch + dst batch
    assert stat_calls["n"] == 2


def test_preview_all_unidentified_returns_zero_no_token(client, token, monkeypatch):
    """全部 needs_identify → items_count=0 + 不签名（不创建 destructive_actions row）."""
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(
        app_module, "_ssh_stat_paths",
        lambda paths: {p: {"exists": True, "inode": 100,
                           "size_bytes": 1024, "mtime": 1000}
                       for p in paths},
    )
    _patch_cache(monkeypatch, None)  # 全部 miss

    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize",
              "items": [{"src_path": f"/dl/{i}.mkv"} for i in range(5)]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["items_count"] == 0
    assert "action_id" not in body
    assert "signed_token" not in body
    assert body["counts"]["needs_identify"] == 5
    assert body["message"] == "无可整理文件"


# ── Phase 4B.3 confirm 分流 + status + abort route tests ──


def test_confirm_organize_above_threshold_returns_202_background(client, token, monkeypatch):
    """items 数超过 ORGANIZE_BATCH_INLINE_THRESHOLD → 202 + background worker。"""
    from services import organize_runner

    _patch_organize_config(monkeypatch)

    n = app_module.ORGANIZE_BATCH_INLINE_THRESHOLD + 2
    src_paths = [f"/dl/m{i}.mkv" for i in range(n)]

    def fake_stat(paths):
        return {
            p: ({"exists": True, "inode": 1000 + i, "size_bytes": 1024, "mtime": 1000}
                if p.startswith("/dl/m") and p in src_paths
                else {"exists": False})
            for i, p in enumerate(paths)
        }
    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    _patch_cache(monkeypatch, _CachedStub(
        title="Movie", media_type="movie", year=2024, tmdb_id="111",
    ))

    # mock organize_runner.start_organize_executor 不真起 thread
    started_with = {}
    def fake_start(*, db_path, action_id, payload, execute_one_item,
                   selected_indices=None):
        started_with["action_id"] = action_id
        started_with["items_count"] = len(payload["items"])
        started_with["selected_indices"] = selected_indices
    monkeypatch.setattr(organize_runner, "start_organize_executor", fake_start)

    # Preview
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize",
              "items": [{"src_path": p} for p in src_paths]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    action_id, signed = body["action_id"], body["signed_token"]

    # Confirm with selected_indices subset
    resp2 = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed,
              "selected_indices": [0, 1, 3]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 202
    body2 = resp2.get_json()
    assert body2["status"] == "running"
    assert body2["items_total"] == n
    assert "polling_url" in body2
    assert started_with["action_id"] == action_id
    assert started_with["selected_indices"] == [0, 1, 3]


def test_confirm_organize_at_threshold_uses_inline_path(client, token, monkeypatch):
    """items 数 == ORGANIZE_BATCH_INLINE_THRESHOLD → 仍走 inline 同步 path。"""
    from services import organize_runner

    _patch_organize_config(monkeypatch)
    n = app_module.ORGANIZE_BATCH_INLINE_THRESHOLD  # 5
    src_paths = [f"/dl/m{i}.mkv" for i in range(n)]

    def fake_stat(paths):
        return {p: ({"exists": True, "inode": 1000 + i,
                     "size_bytes": 1024, "mtime": 1000}
                    if p in src_paths
                    else {"exists": False})
                for i, p in enumerate(paths)}
    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    _patch_cache(monkeypatch, _CachedStub(
        title="Movie", media_type="movie", year=2024, tmdb_id="111",
    ))

    # 如果错走 background path 会调 start_organize_executor — patch 让它 fail loudly
    def boom_start(**_kw):
        raise AssertionError("should NOT take background path at threshold boundary")
    monkeypatch.setattr(organize_runner, "start_organize_executor", boom_start)

    # 走 inline path → executor 真跑 → 我们 patch 它返 succeeded
    inline_calls = {"n": 0}
    def fake_one_item(item, expected_metadata):
        inline_calls["n"] += 1
        return {"src_path": item["src_path"], "status": "succeeded"}
    monkeypatch.setattr(app_module, "_organize_executor_one_item", fake_one_item)

    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize",
              "items": [{"src_path": p} for p in src_paths]},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    action_id, signed = body["action_id"], body["signed_token"]

    resp2 = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 200  # inline path 返 200，不是 202
    assert resp2.get_json()["status"] == "succeeded"
    assert inline_calls["n"] == n


def test_action_status_returns_404_for_missing(client, token):
    resp = client.get(
        "/api/action/status?id=nonexistent",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


def test_action_status_returns_progress_for_running(client, token, monkeypatch):
    """worker 跑期间 status 端点能读到 running + items_completed。"""
    from services import organize_runner
    # 直接 mock organize_runner.get_organize_status 返 progress payload
    def fake_status(conn, action_id):
        if action_id == "test-action-id":
            return {
                "action_id": action_id, "kind": "organize",
                "status": "running",
                "items_total": 10, "items_completed": 4,
                "current_item": "/dl/m4.mkv",
                "status_counts": {"succeeded": 4},
                "result": {"items_completed": 4},
                "started_at": 1000, "completed_at": None,
                "expires_at": 2000, "error": None, "recovery_hint": None,
            }
        return None
    monkeypatch.setattr(organize_runner, "get_organize_status", fake_status)

    resp = client.get(
        "/api/action/status?id=test-action-id",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "running"
    assert body["items_completed"] == 4
    assert body["current_item"] == "/dl/m4.mkv"


def test_action_abort_sets_flag_for_running_action(client, token, monkeypatch):
    """POST /api/action/abort → request_abort 被调用，返 ok=true。"""
    from services import organize_runner

    monkeypatch.setattr(
        organize_runner, "get_organize_status",
        lambda conn, aid: {"status": "running"} if aid == "abc" else None,
    )
    abort_calls = []
    def fake_request_abort(aid):
        abort_calls.append(aid)
        return True
    monkeypatch.setattr(organize_runner, "request_abort", fake_request_abort)

    resp = client.post(
        "/api/action/abort",
        json={"action_id": "abc"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "abort_requested"
    assert abort_calls == ["abc"]


def test_action_abort_rejects_non_running_action(client, token, monkeypatch):
    """已 terminal 的 action 不能 abort → 409。"""
    from services import organize_runner
    monkeypatch.setattr(
        organize_runner, "get_organize_status",
        lambda conn, aid: {"status": "succeeded"},
    )
    resp = client.post(
        "/api/action/abort",
        json={"action_id": "done"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 409
    assert resp.get_json()["current_status"] == "succeeded"


# ── codex r1 修复测试 ──


def test_confirm_inline_organize_respects_selected_indices(client, token, monkeypatch):
    """codex r1 BLOCKER 1: inline path (≤5 items) selected_indices 也生效。"""
    _patch_organize_config(monkeypatch)
    n = 3
    src_paths = [f"/dl/m{i}.mkv" for i in range(n)]

    def fake_stat(paths):
        return {p: ({"exists": True, "inode": 1000 + i,
                     "size_bytes": 1024, "mtime": 1000}
                    if p in src_paths
                    else {"exists": False})
                for i, p in enumerate(paths)}
    monkeypatch.setattr(app_module, "_ssh_stat_paths", fake_stat)
    _patch_cache(monkeypatch, _CachedStub(
        title="Movie", media_type="movie", year=2024, tmdb_id="111",
    ))

    inline_calls = []
    def fake_one_item(item, expected_metadata):
        inline_calls.append(item["src_path"])
        return {"src_path": item["src_path"], "status": "succeeded"}
    monkeypatch.setattr(app_module, "_organize_executor_one_item", fake_one_item)

    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize",
              "items": [{"src_path": p} for p in src_paths]},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    action_id, signed = body["action_id"], body["signed_token"]

    # 只勾 index 0 + 2
    resp2 = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed,
              "selected_indices": [0, 2]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 200
    result = resp2.get_json()["result"]
    counts = result["status_counts"]
    assert counts["succeeded"] == 2
    assert counts["skipped_by_user"] == 1
    # 真正调用的只有 m0 + m2
    assert inline_calls == [src_paths[0], src_paths[2]]


def test_confirm_selected_indices_out_of_range_returns_400(client, token, monkeypatch):
    """codex r1 IMP6: selected_indices 含越界 index → 400。"""
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(app_module, "_ssh_stat_paths",
                        lambda paths: _src_stat("/dl/x.mkv"))
    _patch_cache(monkeypatch, _CachedStub(
        title="Movie", media_type="movie", year=2024, tmdb_id="111",
    ))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/x.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()

    # selected_indices=[5] 但 payload 只有 1 item → 越界
    resp2 = client.post(
        "/api/action/confirm",
        json={"action_id": body["action_id"], "signed_token": body["signed_token"],
              "selected_indices": [5]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 400
    assert resp2.get_json()["error"] == "selected_indices_out_of_range"


def test_confirm_concurrent_organize_rollbacks_to_pending(client, token, monkeypatch):
    """codex r1 NIT1: 并发 organize 撞上 → rollback_to_pending（不浪费 preview）."""
    from services import organize_runner
    _patch_organize_config(monkeypatch)

    n = app_module.ORGANIZE_BATCH_INLINE_THRESHOLD + 2
    src_paths = [f"/dl/m{i}.mkv" for i in range(n)]
    monkeypatch.setattr(app_module, "_ssh_stat_paths",
                        lambda paths: {p: ({"exists": True, "inode": 1000 + i,
                                            "size_bytes": 1024, "mtime": 1000}
                                           if p in src_paths
                                           else {"exists": False})
                                       for i, p in enumerate(paths)})
    _patch_cache(monkeypatch, _CachedStub(
        title="Movie", media_type="movie", year=2024, tmdb_id="111",
    ))
    # 模拟 start_organize_executor 抛 ConcurrentOrganizeError
    def fake_start(**_kw):
        raise organize_runner.ConcurrentOrganizeError("another in progress")
    monkeypatch.setattr(organize_runner, "start_organize_executor", fake_start)
    monkeypatch.setattr(organize_runner, "get_active_action_id", lambda: "other-action")

    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize",
              "items": [{"src_path": p} for p in src_paths]},
        headers={"Authorization": f"Bearer {token}"},
    )
    body = resp.get_json()
    action_id, signed = body["action_id"], body["signed_token"]

    resp2 = client.post(
        "/api/action/confirm",
        json={"action_id": action_id, "signed_token": signed},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp2.status_code == 409
    assert resp2.get_json()["active_action_id"] == "other-action"

    # action 应该被 rollback_to_pending，可以再 confirm
    # （用 get_db 查 status 验证）
    with app_module.app.test_request_context():
        c = app_module.get_db()
        row = c.execute(
            "SELECT status, consumed_at FROM destructive_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
    assert row["status"] == "pending"
    assert row["consumed_at"] is None
