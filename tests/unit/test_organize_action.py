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
    """Patch metadata_cache.get_by_path to return (cached, 'hit')."""
    monkeypatch.setattr(
        app_module.metadata_cache, "get_by_path",
        lambda conn, path, current_mtime=None, current_inode=None: (cached_or_none, "hit"),
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


def test_preview_src_missing_returns_400(client, token, monkeypatch):
    """SSH stat 说文件不存在 → 400 src_missing."""
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
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "src_missing"
    assert body["src_path"] == "/dl/missing.mkv"


def test_preview_src_not_identified_returns_400(client, token, monkeypatch):
    """cache miss → 400 src_not_identified."""
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
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "src_not_identified"


def test_preview_tv_missing_episode_returns_400(client, token, monkeypatch):
    """TV 但 episode_number=None → OrganizeNotApplicable → 400."""
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
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "organize_not_applicable"
    assert "season + episode" in body["reason"]


def test_preview_unsupported_media_type_returns_400(client, token, monkeypatch):
    """cache.media_type='anime' 不支持 → 400 src_not_identified（合并到未识别情形）."""
    _patch_organize_config(monkeypatch)
    monkeypatch.setattr(app_module, "_ssh_stat_paths", lambda paths: _src_stat("/dl/x.mkv"))
    _patch_cache(monkeypatch, _CachedStub(title="X", media_type="anime"))
    resp = client.post(
        "/api/action/preview",
        json={"kind": "organize", "items": [{"src_path": "/dl/x.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    # media_type 不在 {movie, tv} → 视为 not_identified（preview 不在这里区分两种 reject reason）
    assert resp.get_json()["error"] == "src_not_identified"


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
        lambda src_path, nfo, kind: "created",
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
        lambda src_path, nfo, kind: nfo_calls.append((nfo, kind)) or "created",
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


def test_confirm_ln_fails_rmdir_empty_dst_dir(client, token, monkeypatch):
    """ln 失败 → best-effort rmdir 空的 dst_dir。"""
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

    def fake_ssh_exec(cmd, timeout=30):
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
    assert "ln_failed" in item["reason"]
    assert "EXDEV" in item["reason"]
    # rmdir best-effort 被调
    assert any("rmdir" in c for c in rmdir_calls)


def test_confirm_ln_verify_inode_mismatch_unlinks_dst(client, token, monkeypatch):
    """ln 成功但 verify 时 dst inode 不匹配 src → unlink dst + rmdir + failed."""
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

    cleanup_calls = []

    def fake_ssh_exec(cmd, timeout=30):
        cleanup_calls.append(cmd)
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
    # 必须有 rm -f dst + rmdir 清理
    assert any("rm -f" in c and dst_path in c for c in cleanup_calls)
    assert any("rmdir" in c for c in cleanup_calls)


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
        lambda src_path, nfo, kind: "failed: write_failed: permission",
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
