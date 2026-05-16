"""Phase 3.3: /api/action/preview strict snapshot_mode + source互锁 contract tests.

Tests focus on the route-level validation chain in `_do_action_preview`
(app.py). We mock `_build_delete_snapshot` so the SSH layer doesn't run.
This is a route contract test, not an integration test.
"""

from __future__ import annotations

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


def _ok_snapshot(items: list[dict]) -> dict:
    """Build a minimal canonical snapshot that _do_action_preview will accept."""
    return {
        "captured_at": 1700000000,
        "mode": "strict",
        "blocked": False,
        "mismatches": [],
        "items": [
            {
                "path": it["path"],
                "realpath": it["path"],
                "exists": True,
                "inode": it.get("inode", 100),
                "size_bytes": it.get("size", 1024),
                "mtime": it.get("mtime", 1000),
                "is_dir": False,
                "real_size": it.get("size", 1024),
                "hardlinks": [],
            }
            for it in items
        ],
        "all_to_delete": [it["path"] for it in items],
        "torrents": [],
        "qbit_status": {"ok": True, "message": ""},
    }


def _blocked_snapshot(items: list[dict], mismatches: list[dict]) -> dict:
    snap = _ok_snapshot(items)
    snap["blocked"] = True
    snap["mismatches"] = mismatches
    return snap


# ── source 校验 ──────────────────────────────────────────────────


def test_delete_preview_invalid_source_returns_400(client, token):
    resp = client.post(
        "/api/action/preview",
        json={"kind": "delete", "source": "evil_input", "candidates": [{"path": "/x.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "source_invalid"


def test_delete_preview_missing_source_defaults_file_browser(client, token):
    """缺 source 应 fallback 到 file_browser + lenient (legacy 兼容)。"""
    with patch.object(
        app_module, "_build_delete_snapshot", return_value=_ok_snapshot([{"path": "/x.mkv"}])
    ):
        resp = client.post(
            "/api/action/preview",
            json={"kind": "delete", "candidates": [{"path": "/x.mkv"}]},
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["source"] == "file_browser"
    assert body["snapshot_mode"] == "lenient"


def test_delete_preview_dedup_source_cannot_use_lenient(client, token):
    """r2 关键互锁：source='dedup' 不允许 lenient。"""
    resp = client.post(
        "/api/action/preview",
        json={
            "kind": "delete",
            "source": "dedup",
            "snapshot_mode": "lenient",
            "candidates": [
                {
                    "path": "/x.mkv",
                    "expected_inode": 1,
                    "expected_size": 1,
                    "expected_mtime": 1,
                }
            ],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "dedup_source_must_use_strict_mode"


def test_delete_preview_dedup_source_default_mode_is_strict(client, token):
    """source='dedup' 不传 snapshot_mode → 默认 strict（仍要求 expected_*）。"""
    resp = client.post(
        "/api/action/preview",
        json={"kind": "delete", "source": "dedup", "candidates": [{"path": "/x.mkv"}]},
        headers={"Authorization": f"Bearer {token}"},
    )
    # 默认 strict + 缺 expected_* → 400 strict_mode_requires_expected_fields
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "strict_mode_requires_expected_fields"


# ── strict mode 必填 expected_* ──────────────────────────────────


def test_strict_mode_missing_expected_inode_returns_400(client, token):
    resp = client.post(
        "/api/action/preview",
        json={
            "kind": "delete",
            "source": "dedup",
            "snapshot_mode": "strict",
            "candidates": [
                {
                    "path": "/x.mkv",
                    "expected_size": 1024,
                    "expected_mtime": 1000,
                    # 缺 expected_inode
                }
            ],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "strict_mode_requires_expected_fields"
    assert body["missing"] == "expected_inode"


def test_strict_mode_missing_expected_size_returns_400(client, token):
    resp = client.post(
        "/api/action/preview",
        json={
            "kind": "delete",
            "source": "dedup",
            "snapshot_mode": "strict",
            "candidates": [
                {
                    "path": "/x.mkv",
                    "expected_inode": 100,
                    "expected_mtime": 1000,
                }
            ],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["missing"] == "expected_size"


def test_strict_mode_missing_expected_mtime_returns_400(client, token):
    resp = client.post(
        "/api/action/preview",
        json={
            "kind": "delete",
            "source": "dedup",
            "snapshot_mode": "strict",
            "candidates": [
                {
                    "path": "/x.mkv",
                    "expected_inode": 100,
                    "expected_size": 1024,
                }
            ],
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert resp.get_json()["missing"] == "expected_mtime"


# ── strict mode mismatch → 409 blocked ──────────────────────────


def test_strict_mode_all_match_returns_token(client, token):
    """strict + 全部 match → 200 + signed_token。"""
    snap = _ok_snapshot([{"path": "/x.mkv", "inode": 100, "size": 1024, "mtime": 1000}])
    with patch.object(app_module, "_build_delete_snapshot", return_value=snap):
        resp = client.post(
            "/api/action/preview",
            json={
                "kind": "delete",
                "source": "dedup",
                "snapshot_mode": "strict",
                "candidates": [
                    {
                        "path": "/x.mkv",
                        "expected_inode": 100,
                        "expected_size": 1024,
                        "expected_mtime": 1000,
                    }
                ],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 200
    body = resp.get_json()
    assert "signed_token" in body
    assert "action_id" in body
    assert body["source"] == "dedup"
    assert body["snapshot_mode"] == "strict"


def test_strict_mode_mtime_diff_returns_409_blocked(client, token):
    """strict + mtime diff → 409 + mismatches；不 create_preview / 不返 token。"""
    blocked_snap = _blocked_snapshot(
        items=[{"path": "/x.mkv"}],
        mismatches=[
            {
                "path": "/x.mkv",
                "diffs": ["mtime_changed"],
                "expected": {"inode": 100, "size_bytes": 1024, "mtime": 1000},
                "current": {"inode": 100, "size_bytes": 1024, "mtime": 9999},
            }
        ],
    )
    with patch.object(app_module, "_build_delete_snapshot", return_value=blocked_snap):
        resp = client.post(
            "/api/action/preview",
            json={
                "kind": "delete",
                "source": "dedup",
                "snapshot_mode": "strict",
                "candidates": [
                    {
                        "path": "/x.mkv",
                        "expected_inode": 100,
                        "expected_size": 1024,
                        "expected_mtime": 1000,
                    }
                ],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.status_code == 409
    body = resp.get_json()
    assert body["blocked"] is True
    assert len(body["mismatches"]) == 1
    assert "mtime_changed" in body["mismatches"][0]["diffs"]
    assert "signed_token" not in body  # 不产 token


# ── _build_delete_snapshot strict 模式单元测试 ─────────────────


def test_build_delete_snapshot_strict_detects_inode_change(monkeypatch):
    """_build_delete_snapshot mode='strict' 在 inode 变化时填 mismatches[]."""
    cand = [
        {
            "path": "/test/a.mkv",
            "expected_inode": 999,  # expected differs from actual
            "expected_size": 1024,
            "expected_mtime": 1000,
        }
    ]

    monkeypatch.setattr(app_module, "validate_path", lambda p: p)
    monkeypatch.setattr(app_module, "_reject_base_path", lambda *_: None)
    monkeypatch.setattr(
        app_module,
        "_ssh_stat_paths",
        lambda paths: {
            "/test/a.mkv": {
                "exists": True,
                "inode": 100,
                "size_bytes": 1024,
                "mtime": 1000,
                "is_dir": False,
            },
        },
    )
    monkeypatch.setattr(app_module, "_resolve_real_paths", lambda paths: {p: p for p in paths})
    monkeypatch.setattr(app_module, "_resolve_all_hardlink_paths", lambda paths: {})
    monkeypatch.setattr(app_module, "_enumerate_dir_files", lambda *_, **__: [])
    monkeypatch.setattr(app_module, "_get_real_sizes", lambda paths: {p: 1024 for p in paths})
    monkeypatch.setattr(app_module.qbit, "find_torrents_by_paths", lambda paths: [])

    snap = app_module._build_delete_snapshot(cand, mode="strict")
    assert snap["mode"] == "strict"
    assert snap["blocked"] is True
    assert len(snap["mismatches"]) == 1
    assert "inode_changed" in snap["mismatches"][0]["diffs"]


def test_build_delete_snapshot_strict_all_match_not_blocked(monkeypatch):
    cand = [
        {
            "path": "/test/a.mkv",
            "expected_inode": 100,
            "expected_size": 1024,
            "expected_mtime": 1000,
        }
    ]

    monkeypatch.setattr(app_module, "validate_path", lambda p: p)
    monkeypatch.setattr(app_module, "_reject_base_path", lambda *_: None)
    monkeypatch.setattr(
        app_module,
        "_ssh_stat_paths",
        lambda paths: {
            "/test/a.mkv": {
                "exists": True,
                "inode": 100,
                "size_bytes": 1024,
                "mtime": 1000,
                "is_dir": False,
            },
        },
    )
    monkeypatch.setattr(app_module, "_resolve_real_paths", lambda paths: {p: p for p in paths})
    monkeypatch.setattr(app_module, "_resolve_all_hardlink_paths", lambda paths: {})
    monkeypatch.setattr(app_module, "_enumerate_dir_files", lambda *_, **__: [])
    monkeypatch.setattr(app_module, "_get_real_sizes", lambda paths: {p: 1024 for p in paths})
    monkeypatch.setattr(app_module.qbit, "find_torrents_by_paths", lambda paths: [])

    snap = app_module._build_delete_snapshot(cand, mode="strict")
    assert snap["blocked"] is False
    assert snap["mismatches"] == []


def test_build_delete_snapshot_lenient_ignores_expected_fields(monkeypatch):
    """lenient mode 即使带了 expected_* 也不对比，不阻塞。"""
    cand = [
        {
            "path": "/test/a.mkv",
            "expected_inode": 999,  # 故意 wrong
            "expected_size": 9999,
            "expected_mtime": 9999,
        }
    ]

    monkeypatch.setattr(app_module, "validate_path", lambda p: p)
    monkeypatch.setattr(app_module, "_reject_base_path", lambda *_: None)
    monkeypatch.setattr(
        app_module,
        "_ssh_stat_paths",
        lambda paths: {
            "/test/a.mkv": {
                "exists": True,
                "inode": 100,
                "size_bytes": 1024,
                "mtime": 1000,
                "is_dir": False,
            },
        },
    )
    monkeypatch.setattr(app_module, "_resolve_real_paths", lambda paths: {p: p for p in paths})
    monkeypatch.setattr(app_module, "_resolve_all_hardlink_paths", lambda paths: {})
    monkeypatch.setattr(app_module, "_enumerate_dir_files", lambda *_, **__: [])
    monkeypatch.setattr(app_module, "_get_real_sizes", lambda paths: {p: 1024 for p in paths})
    monkeypatch.setattr(app_module.qbit, "find_torrents_by_paths", lambda paths: [])

    snap = app_module._build_delete_snapshot(cand, mode="lenient")
    assert snap["mode"] == "lenient"
    assert snap["blocked"] is False
    assert snap["mismatches"] == []


def test_build_delete_snapshot_invalid_mode_raises():
    with pytest.raises(ValueError):
        app_module._build_delete_snapshot([{"path": "/x"}], mode="bogus")
