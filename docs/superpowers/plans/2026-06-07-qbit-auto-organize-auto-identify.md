# qBit Auto-Organize 自动识别 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Commit 约定**：本项目习惯「只在用户要求时提交」。下面每个 Task 末尾的 commit step 是建议；实际执行时若用户未要求，可累积到最后统一提交。

**Goal:** 让新下载的 qBit 种子下载完成后自动识别，置信度 ≥ 0.95 时自动 hardlink 到媒体库；低置信度/识别失败留人工；67 个历史积压不动。

**Architecture:** 在现有 auto-organize cron 的 `dispatch_one` 内联自动识别 —— confidence_gate 判 `needs_identify` 时调注入的 identify callback 写 cache，再用高门槛(0.95) re-gate。复用已铺好的 path_resolver（namespace 已统一）。默认关闭，需显式开启。

**Tech Stack:** Python 3.14, Flask, sqlite3, pytest, APScheduler；services/qbit_auto.py + services/identify.py + services/metadata_cache.py + app.py

**Spec:** `docs/superpowers/specs/2026-06-06-qbit-auto-organize-auto-identify-design.md`

---

## File Structure

| 文件 | 职责 | 改动 |
|---|---|---|
| `app.py` (metadata_identify ~4341) | identify route | Task 1：validate 前接 path_resolver |
| `app.py` (config ~483-544) | auto-organize 配置 schema | Task 2：加 2 个字段 |
| `services/qbit_auto.py` (dispatch_one ~433) | dispatch 编排 | Task 3：内联自动识别 + re-gate |
| `app.py` (新 helper) | cron 自动识别 callback | Task 4：`_identify_and_cache` + `_auto_identify_paths` |
| `app.py` (_cron_qbit_auto_organize ~5787) | cron 触发 | Task 5：注入 callback |
| `tests/unit/test_qbit_auto.py` | dispatch_one 测试 | Task 3 |
| `tests/unit/test_metadata_identify_resolve.py` | identify route 测试 | Task 1（新建） |
| `tests/unit/test_qbit_auto_organize_config.py` | config 测试 | Task 2 |

---

## Task 1: identify route 接入 path_resolver（修 400 bug）

**Files:**
- Modify: `app.py` (metadata_identify, ~4348-4355)
- Test: `tests/unit/test_metadata_identify_resolve.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_metadata_identify_resolve.py`:

```python
"""identify route 应在 validate_path 前用 path_resolver 把 QNAP 别名路径
(/share/downloads/...) 翻成 canonical，否则别名路径被沙箱字面前缀拒 (400)。"""
from __future__ import annotations

import pytest

import app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def token():
    return app_module.API_TOKEN


def test_identify_resolves_alias_before_validate(client, token, monkeypatch):
    calls = {}

    def fake_resolve(p, ssh_fn):
        calls["resolved_input"] = p
        # 模拟 QNAP 别名 → canonical（落进 NAS_BASE_PATH 沙箱内）
        return p.replace("/share/downloads/", f"{app_module.NAS_BASE_PATH}/downloads/", 1)

    monkeypatch.setattr(app_module.path_resolver, "resolve", fake_resolve)
    # provider=None → 走 parse-only 分支返 200，不调真实 TMDB
    monkeypatch.setattr(app_module, "get_tmdb_provider", lambda: None)

    resp = client.post(
        "/api/metadata/identify",
        json={"path": "/share/downloads/Some.Movie.2020.1080p.mkv"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 200, resp.get_json()
    # resolve 收到的是原始别名路径（证明在 validate 之前调用）
    assert calls["resolved_input"] == "/share/downloads/Some.Movie.2020.1080p.mkv"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_metadata_identify_resolve.py -v`
Expected: FAIL — 别名路径被 `validate_path` 拒，返回 400（`assert 400 == 200`）。

- [ ] **Step 3: Implement — 在 validate_path 前插入 resolve**

In `app.py` `metadata_identify`, change (around line 4348-4355):

```python
    data = request.json or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "path required"}), 400
    # QNAP 别名路径 (/share/downloads/...) 先翻 canonical，否则被沙箱字面前缀拒。
    # 复用 _list_video_paths 已有写法（path_resolver.resolve，safe fallback 永不抛）。
    path = path_resolver.resolve(path, ssh_exec)
    try:
        path = validate_path(path)
    except Exception:
        return jsonify({"error": f"invalid path: {path}"}), 400
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_metadata_identify_resolve.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app.py tests/unit/test_metadata_identify_resolve.py
git commit -m "fix(identify): route 接 path_resolver — 别名路径不再 400"
```

---

## Task 2: 配置 schema 加 auto_identify 字段

**Files:**
- Modify: `app.py` (`QBIT_AUTO_ORGANIZE_DEFAULTS` ~482, `load_qbit_auto_organize_config` ~490, `save_qbit_auto_organize_config` ~523)
- Test: `tests/unit/test_qbit_auto_organize_config.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/test_qbit_auto_organize_config.py`:

```python
def test_auto_identify_defaults_off(tmp_path, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "QBIT_AUTO_ORGANIZE_CONFIG_FILE", tmp_path / "qao.json")
    cfg = app_module.load_qbit_auto_organize_config()
    assert cfg["auto_identify"] is False
    assert cfg["auto_identify_confidence_threshold"] == 0.95


def test_auto_identify_save_roundtrip_and_clamp(tmp_path, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module, "QBIT_AUTO_ORGANIZE_CONFIG_FILE", tmp_path / "qao.json")
    app_module.save_qbit_auto_organize_config(
        {
            "enabled": True,
            "categories": ["movie"],
            "auto_identify": True,
            "auto_identify_confidence_threshold": 1.5,  # 越界 → clamp 到 1.0
        }
    )
    cfg = app_module.load_qbit_auto_organize_config()
    assert cfg["auto_identify"] is True
    assert cfg["auto_identify_confidence_threshold"] == 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_qbit_auto_organize_config.py -k auto_identify -v`
Expected: FAIL — `KeyError: 'auto_identify'`

- [ ] **Step 3: Implement — defaults + load + save**

In `app.py`, `QBIT_AUTO_ORGANIZE_DEFAULTS` (~482) add two keys:

```python
QBIT_AUTO_ORGANIZE_DEFAULTS = {
    "enabled": False,
    "categories": [],
    "poll_interval_minutes": 5,
    "confidence_threshold": 0.85,
    "auto_identify": False,  # 下载完自动识别未知种子（默认关，destructive 自动化保守）
    "auto_identify_confidence_threshold": 0.95,  # 自动识别后整理门槛（高于手动 0.85）
}
```

In `load_qbit_auto_organize_config` (~519, before `return merged`) add:

```python
    merged["auto_identify"] = bool(merged.get("auto_identify", False))
    try:
        ait = float(merged.get("auto_identify_confidence_threshold", 0.95))
        merged["auto_identify_confidence_threshold"] = min(1.0, max(0.0, ait))
    except (TypeError, ValueError):
        merged["auto_identify_confidence_threshold"] = 0.95
    return merged
```

In `save_qbit_auto_organize_config` (~534, before building `payload`) add:

```python
    raw_ait = cfg.get("auto_identify_confidence_threshold")
    try:
        ait = min(1.0, max(0.0, float(raw_ait))) if raw_ait is not None else 0.95
    except (TypeError, ValueError):
        ait = 0.95
```

and add two keys to the `payload` dict:

```python
    payload = {
        "enabled": bool(cfg.get("enabled", False)),
        "categories": [str(c).strip() for c in cats if c is not None and str(c).strip()],
        "poll_interval_minutes": poll,
        "confidence_threshold": conf,
        "auto_identify": bool(cfg.get("auto_identify", False)),
        "auto_identify_confidence_threshold": ait,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_qbit_auto_organize_config.py -k auto_identify -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app.py tests/unit/test_qbit_auto_organize_config.py
git commit -m "feat(auto-organize): config 加 auto_identify + 0.95 门槛字段"
```

---

## Task 3: dispatch_one 内联自动识别 + re-gate

**Files:**
- Modify: `services/qbit_auto.py` (`dispatch_one` ~433-518)
- Test: `tests/unit/test_qbit_auto.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/test_qbit_auto.py`:

```python
# ── dispatch_one 自动识别 (Task 3) ───


def _dispatch_with_gates(conn, monkeypatch, gate_sequence, *, identify_fn=None,
                         auto_threshold=0.95, build_status="started"):
    """跑 dispatch_one，monkeypatch evaluate_confidence_gate 返回受控 gate 序列。
    返回 (out, build_calls)。"""
    gates = iter(gate_sequence)
    monkeypatch.setattr(qbit_auto, "evaluate_confidence_gate", lambda *a, **k: next(gates))
    build_calls = []

    def build_fn(paths, qbit_hash):
        build_calls.append((paths, qbit_hash))
        return {"action_id": "act-1", "status": build_status}

    out = qbit_auto.dispatch_one(
        conn,
        _t(hash="hX", content_path="/d/m.mkv"),
        list_video_paths_fn=lambda cp: ["/share/CACHEDEV2_DATA/downloads/m.mkv"],
        confidence_threshold=0.85,
        build_and_start_organize_fn=build_fn,
        identify_paths_fn=identify_fn,
        auto_identify_threshold=auto_threshold,
    )
    return out, build_calls


def test_dispatch_auto_identify_high_conf_organizes(conn, monkeypatch):
    """needs_identify → 自动识别 → re-gate pass → 起 organize。"""
    identify_calls = []

    def identify_fn(paths):
        identify_calls.append(list(paths))
        return {"provider_unavailable": False}

    out, build_calls = _dispatch_with_gates(
        conn,
        monkeypatch,
        [
            {"status": "skipped_needs_identify", "reason": "no cache",
             "blockers": [{"path": "/share/CACHEDEV2_DATA/downloads/m.mkv"}], "checked_count": 1},
            {"status": "pass", "reason": "ok", "blockers": [], "checked_count": 1},
        ],
        identify_fn=identify_fn,
    )
    assert out["action"] == "started"
    assert identify_calls == [["/share/CACHEDEV2_DATA/downloads/m.mkv"]]
    assert len(build_calls) == 1


def test_dispatch_auto_identify_low_conf_skips(conn, monkeypatch):
    """识别出但 re-gate(0.95) 判 low_confidence → 留人工，不整理。"""
    out, build_calls = _dispatch_with_gates(
        conn,
        monkeypatch,
        [
            {"status": "skipped_needs_identify", "reason": "no cache",
             "blockers": [{"path": "/share/CACHEDEV2_DATA/downloads/m.mkv"}], "checked_count": 1},
            {"status": "skipped_low_confidence", "reason": "0.9 < 0.95",
             "blockers": [], "checked_count": 1},
        ],
        identify_fn=lambda paths: {"provider_unavailable": False},
    )
    assert out["action"] == "skipped"
    assert out["status"] == "skipped_low_confidence"
    assert build_calls == []


def test_dispatch_auto_identify_provider_unavailable_locks(conn, monkeypatch):
    """provider 临时不可用 → action=locked（留 pending 重试，不落 terminal）。"""
    out, build_calls = _dispatch_with_gates(
        conn,
        monkeypatch,
        [
            {"status": "skipped_needs_identify", "reason": "no cache",
             "blockers": [{"path": "/share/CACHEDEV2_DATA/downloads/m.mkv"}], "checked_count": 1},
        ],
        identify_fn=lambda paths: {"provider_unavailable": True},
    )
    assert out["action"] == "locked"
    assert build_calls == []
    # row 仍 pending（未落 terminal skip）
    row = qbit_auto.get_run(conn, "hX")
    assert row["status"] == "pending"


def test_dispatch_no_identify_fn_unchanged(conn, monkeypatch):
    """identify_paths_fn=None → 行为不变：needs_identify 直接落 terminal skip。"""
    out, build_calls = _dispatch_with_gates(
        conn,
        monkeypatch,
        [
            {"status": "skipped_needs_identify", "reason": "no cache",
             "blockers": [{"path": "/x"}], "checked_count": 1},
        ],
        identify_fn=None,
    )
    assert out["action"] == "skipped"
    assert out["status"] == "skipped_needs_identify"
    assert build_calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/unit/test_qbit_auto.py -k "auto_identify or no_identify_fn" -v`
Expected: FAIL — `dispatch_one() got an unexpected keyword argument 'identify_paths_fn'`

- [ ] **Step 3: Implement — dispatch_one 新参数 + 自动识别分支**

In `services/qbit_auto.py`, update `dispatch_one` signature (~433-440):

```python
def dispatch_one(
    conn: sqlite3.Connection,
    torrent: dict,
    *,
    list_video_paths_fn,
    confidence_threshold: float,
    build_and_start_organize_fn,
    identify_paths_fn=None,  # callable(paths) -> {"provider_unavailable": bool}; None=不自动识别
    auto_identify_threshold: float | None = None,  # 自动识别后 re-gate 门槛
) -> dict[str, Any]:
```

Then replace the confidence-gate block (现 ~503-518) with:

```python
    # 3. confidence gate
    gate = evaluate_confidence_gate(conn, paths, threshold=confidence_threshold)

    # 3.5 自动识别（可选）：needs_identify 时调 identify_paths_fn 写 cache + re-gate 高门槛。
    # provider 临时不可用 → locked（留 pending 重试，不落 terminal）。
    if gate["status"] == "skipped_needs_identify" and identify_paths_fn is not None:
        ni_paths = [b["path"] for b in gate.get("blockers", []) if b.get("path")]
        outcome = identify_paths_fn(ni_paths)
        if outcome.get("provider_unavailable"):
            return {
                "action": "locked",
                "qbit_hash": qbit_hash,
                "reason": "provider_unavailable_during_auto_identify",
            }
        regate_threshold = (
            auto_identify_threshold
            if auto_identify_threshold is not None
            else confidence_threshold
        )
        gate = evaluate_confidence_gate(conn, paths, threshold=regate_threshold)

    if gate["status"] != "pass":
        mark_skipped_at_pending(
            conn,
            qbit_hash,
            status=gate["status"],
            error=gate["reason"],
        )
        return {
            "action": "skipped",
            "status": gate["status"],
            "qbit_hash": qbit_hash,
            "reason": gate["reason"],
            "blockers": gate.get("blockers", []),
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/unit/test_qbit_auto.py -k "auto_identify or no_identify_fn" -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Run full qbit_auto suite (regression)**

Run: `.venv/bin/pytest tests/unit/test_qbit_auto.py -v`
Expected: PASS（所有既有测试不破 — 向后兼容）

- [ ] **Step 6: Commit**

```bash
git add services/qbit_auto.py tests/unit/test_qbit_auto.py
git commit -m "feat(auto-organize): dispatch_one 内联自动识别 + 高门槛 re-gate"
```

---

## Task 4: app 层 identify callback helper

**Files:**
- Modify: `app.py` (新增 `_identify_and_cache` + `_auto_identify_paths`，放在 `_cron_qbit_auto_organize` 之前)
- Test: `tests/unit/test_auto_identify_helper.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_auto_identify_helper.py`:

```python
"""cron 自动识别 callback：_identify_and_cache 用传入 conn（线程安全，不用 get_db），
区分 identified / provider_unavailable；_auto_identify_paths 聚合多 path。"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import app as app_module
from db import migrations
from services import destructive_action
from services.metadata.base import ProviderUnavailable


@pytest.fixture
def conn(tmp_path):
    import pathlib

    db_path = tmp_path / "t.db"
    c = destructive_action.open_connection(db_path)
    destructive_action.init_schema(
        c, pathlib.Path(__file__).resolve().parents[2] / "db" / "schema.sql"
    )
    migrations.phase3_migrate(c)
    migrations.phase4_migrate(c)
    migrations.phase5_migrate(c)
    yield c
    c.close()


def test_identify_and_cache_provider_unavailable(conn, monkeypatch):
    monkeypatch.setattr(app_module, "get_tmdb_provider", lambda: MagicMock())
    monkeypatch.setattr(app_module, "load_deepseek_key", lambda: "")

    def boom(*a, **k):
        raise ProviderUnavailable("429")

    monkeypatch.setattr(app_module.identify_svc, "identify", boom)

    out = app_module._identify_and_cache(conn, "/share/CACHEDEV2_DATA/downloads/m.mkv")
    assert out["provider_unavailable"] is True
    assert out["identified"] is False


def test_identify_and_cache_no_provider(conn, monkeypatch):
    monkeypatch.setattr(app_module, "get_tmdb_provider", lambda: None)
    out = app_module._identify_and_cache(conn, "/x/m.mkv")
    assert out == {"identified": False, "provider_unavailable": False}


def test_auto_identify_paths_short_circuits_on_unavailable(conn, monkeypatch):
    calls = []

    def fake(_conn, p):
        calls.append(p)
        return {"identified": False, "provider_unavailable": True}

    monkeypatch.setattr(app_module, "_identify_and_cache", fake)
    out = app_module._auto_identify_paths(conn, ["/a", "/b"])
    assert out["provider_unavailable"] is True
    assert calls == ["/a"]  # 第一个就 unavailable → 短路，不继续
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/unit/test_auto_identify_helper.py -v`
Expected: FAIL — `AttributeError: module 'app' has no attribute '_identify_and_cache'`

- [ ] **Step 3: Implement — 两个 helper**

In `app.py`, add **before** `def _cron_qbit_auto_organize()` (~5786):

```python
def _identify_and_cache(conn, path: str) -> dict:
    """cron 自动识别单个 canonical path：TMDB+LLM 识别 → 写 media_files。

    线程安全：用传入的 conn（cron 独立连接），**不调 get_db()** —— cron 是
    BackgroundScheduler 后台线程，get_db() 会撞 'Working outside of application
    context'（identify_svc 走 HTTP / _ssh_stat_paths 走 SSH，均不需 Flask 上下文）。

    返回 {"identified": bool, "provider_unavailable": bool}。
    """
    provider = get_tmdb_provider()
    if provider is None:
        return {"identified": False, "provider_unavailable": False}
    try:
        result = identify_svc.identify(path, provider, llm_api_key=load_deepseek_key() or None)
    except ProviderUnavailable:
        return {"identified": False, "provider_unavailable": True}
    stat_map = _ssh_stat_paths([path])
    stat = stat_map.get(path, {})
    if not stat.get("exists"):
        return {"identified": False, "provider_unavailable": False}
    metadata_cache.upsert_identification(conn, path=path, stat=stat, identify_result=result)
    return {"identified": True, "provider_unavailable": False}


def _auto_identify_paths(conn, paths: list[str]) -> dict:
    """对 needs_identify 的 paths 逐个自动识别。任一 provider_unavailable → 短路返回
    （临时错误，dispatch 会标 locked 留下周期重试）。"""
    for p in paths:
        outcome = _identify_and_cache(conn, p)
        if outcome["provider_unavailable"]:
            return {"provider_unavailable": True}
    return {"provider_unavailable": False}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/unit/test_auto_identify_helper.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app.py tests/unit/test_auto_identify_helper.py
git commit -m "feat(auto-organize): cron 自动识别 callback helper（线程安全 conn）"
```

---

## Task 5: cron 注入自动识别 callback

**Files:**
- Modify: `app.py` (`_cron_qbit_auto_organize` ~5844-5861)
- Test: 复用 Task 3/4 单元测试 + Task 6 端到端

- [ ] **Step 1: Implement — cron 读配置 + 注入**

In `app.py` `_cron_qbit_auto_organize`, after `threshold = cfg.get("confidence_threshold", 0.85)` (~5846) add:

```python
            auto_identify = cfg.get("auto_identify", False)
            auto_id_threshold = cfg.get("auto_identify_confidence_threshold", 0.95)
            identify_fn = (
                (lambda ni_paths: _auto_identify_paths(conn, ni_paths))
                if auto_identify
                else None
            )
```

Then in the `dispatch_one(...)` call (~5849-5861) add two kwargs:

```python
                    out = qbit_auto.dispatch_one(
                        conn,
                        t,
                        list_video_paths_fn=lambda cp: _list_video_paths(
                            cp,
                            max_depth=3,
                            limit=MAX_ORGANIZE_BATCH_ITEMS,
                        ),
                        confidence_threshold=threshold,
                        build_and_start_organize_fn=_build_and_start_auto_organize,
                        identify_paths_fn=identify_fn,
                        auto_identify_threshold=auto_id_threshold if auto_identify else None,
                    )
```

- [ ] **Step 2: Run full test suite (no regression)**

Run: `.venv/bin/pytest tests/ -q`
Expected: PASS（cron 改动不破任何既有测试）

- [ ] **Step 3: Lint**

Run: `.venv/bin/ruff check app.py services/qbit_auto.py && .venv/bin/ruff format --check app.py services/qbit_auto.py`
Expected: no errors

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat(auto-organize): cron 注入自动识别 callback（auto_identify 开关）"
```

---

## Task 6: 端到端验证（手动，非代码）

**Files:** none（运维验证）

- [ ] **Step 1: 开启 auto_identify**

Edit `config/qbit_auto_organize.json`：把 `auto_identify` 设 `true`（确认 `auto_identify_confidence_threshold: 0.95`、`enabled: true`、`categories` 含你的下载分类）。

- [ ] **Step 2: 重启 dev server**

```bash
NAS_SKIP_WORKER_LOCK=1 .venv/bin/flask --app app run --debug --port 5001
```

- [ ] **Step 3: 触发一个未识别种子被自动处理**

选一个新种子（或临时把某个积压 row 重置为 pending 做验证）：

```bash
# 临时重置一个积压 row 让 cron 重新 dispatch（验证用；正式场景是新下载）
sqlite3 config/actions.db "DELETE FROM auto_organize_runs WHERE qbit_hash LIKE '<hash前缀>%';"
```

- [ ] **Step 4: 观察 cron 日志（≤5min 一个 cycle）**

Expected 日志序列：
```
[auto-organize] cycle: N completed in whitelist [...], 1 to dispatch
[auto-organize] hash=... action=started status=... (识别 ≥0.95 → 整理)
   或 action=skipped status=skipped_low_confidence (0.85~0.95 → 留人工)
```

- [ ] **Step 5: SSH 第三方验证 hardlink ground truth**

```bash
ssh -p 36000 admin@192.168.31.48 'ls -li "/share/Moives/<片名> (年份)/"'
```
Expected: mkv 的 inode == 源文件 inode（hardlink），link count ≥ 2，`.nfo` 已生成。

- [ ] **Step 6: 验证低置信度不被误整理**

确认 `auto_organize_runs` 里 0.85~0.95 的种子状态是 `skipped_low_confidence`（留人工），未出现在媒体库。

---

## Self-Review

- **Spec coverage**：单元1→Task1，单元2→Task2，单元3(dispatch)→Task3，单元4(helper)→Task4，单元5(cron)→Task5，验证→Task6。单元6(UI) 按 spec 标注后置/YAGNI，不在本 plan。✓
- **Placeholder scan**：无 TBD/TODO，每个 code step 含完整代码。✓
- **Type consistency**：`identify_paths_fn` 契约 `(paths) -> {"provider_unavailable": bool}` 在 Task3(消费)/Task4(`_auto_identify_paths` 生产)/Task5(注入) 三处一致；`_identify_and_cache` 返回 `{"identified", "provider_unavailable"}` 在 Task4 定义并被 `_auto_identify_paths` 消费一致。✓
- **关键风险已覆盖**：cron 线程安全（Task4 用传入 conn 不用 get_db）；provider 临时错误不落 terminal（Task3 locked）；向后兼容（Task3 `identify_paths_fn=None` 测试）；67 积压不动（D2 — cron filter 天然跳过 terminal，无需改动）。
