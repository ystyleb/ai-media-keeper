"""UI status blueprint — 提供 status bar / sidebar badges HTML fragments.

约定:
- 路由前缀 /ui
- 返回 HTML fragment (不是完整页)
- 业务数据从 services/ 或 app module attr 拉, 不重复实现
- 鉴权: 跟 /api/* 共用 require_token 装饰器 (从 app 导入)
"""

from __future__ import annotations

from flask import Blueprint, render_template

ui_status_bp = Blueprint("ui_status", __name__, url_prefix="/ui")


def _require_token(view):
    """Wrap a view with require_token from app module.

    NOTE (Phase A Task 4 followup): currently UNUSED — 3 个 endpoint 在
    Phase A transitional state 不强制 token. Task 4 整合 / 路由时统一加
    @_require_token decorator. 不要在 Phase A 末删, 否则 Task 4 实施时还要重写.

    Import deferred to avoid circular import at module load.
    """
    from app import require_token  # deferred (避免 routes/ 在 app init 前 import app)

    return require_token(view)


@ui_status_bp.route("/status/providers", methods=["GET"])
def status_providers():
    """Provider 4 段 (TMDB / DeepSeek / Emby / qBit) HTML fragment."""
    from app import _compute_providers_status  # deferred (循环 import 避免)

    raw = _compute_providers_status()
    providers = _to_status_segments(raw)
    return render_template("partials/status/providers.html", providers=providers)


@ui_status_bp.route("/status/workers", methods=["GET"])
def status_workers():
    """运行中的 worker 列表 HTML fragment."""
    workers = _aggregate_running_workers()
    return render_template("partials/status/workers.html", workers=workers)


@ui_status_bp.route("/sidebar/badges", methods=["GET"])
def sidebar_badges():
    """Sidebar nav 每项的 badge 计数."""
    badges = _compute_sidebar_badges()
    return render_template("partials/sidebar/badges.html", badges=badges)


# ---------- helpers ----------


_DOT_BY_STATE = {
    "ok": "ok",
    "auth_failed": "err",
    "not_configured": "gray",
    "unreachable": "err",
    "error": "err",
    "unknown": "gray",
}


def _to_status_segments(raw: dict) -> list[dict]:
    """raw = {"tmdb": {"state": "ok", ...}, "deepseek": {...}, ...}.

    返回 [{"name": "TMDB", "dot": "ok", "detail": None, "tooltip": "..."}].
    """
    name_by_key = {"tmdb": "TMDB", "deepseek": "DeepSeek", "emby": "Emby", "qbit": "qBit"}
    segments = []
    for key in ("tmdb", "deepseek", "emby", "qbit"):
        entry = raw.get(key, {}) or {}
        state = entry.get("state", "unknown")
        dot = _DOT_BY_STATE.get(state, "gray")
        detail = None
        if state == "auth_failed":
            detail = "401"
        elif state == "not_configured":
            detail = "未配"
        elif state == "unreachable":
            detail = "无连接"
        tooltip = f"{name_by_key[key]}: {state}"
        if entry.get("last_check"):
            tooltip += f" @ {entry['last_check']}"
        segments.append({
            "name": name_by_key[key],
            "dot": dot,
            "detail": detail,
            "tooltip": tooltip,
        })
    return segments


def _aggregate_running_workers() -> list[dict]:
    """聚合 scan_runs 中 running 的任务.

    注意: organize_runs 表在此 schema 版本为空骨架 (Phase 4B 完整实现时补全).
    scan_runs 列名: id / files_total / files_done / status / started_at.

    返回 [{"kind": "scanner", "done": 234, "total": 1797, "id": 7}].
    """
    from app import get_db  # deferred (循环 import 避免)

    db = get_db()
    workers: list[dict] = []

    # scan_runs (background scanner) — 用真实列名 files_total / files_done
    rows = db.execute(
        "SELECT id, files_total, files_done FROM scan_runs WHERE status = 'running' "
        "ORDER BY started_at DESC LIMIT 3"
    ).fetchall()
    for r in rows:
        workers.append({
            "kind": "scanner",
            "done": r["files_done"] or 0,
            "total": r["files_total"] or 0,
            "id": r["id"],
        })

    return workers


def _compute_sidebar_badges() -> dict:
    """每个 sidebar nav 项的 badge count.

    返回 {"library": N, "dedup": N, "organize": N}.
    """
    from app import get_db  # deferred (循环 import 避免)

    db = get_db()
    badges: dict = {}

    # 媒体库未识别 / 需 review
    # Spec Section 2.3: badge 显示"待用户处理"的库项 = 未绑 TMDB (tmdb_id IS NULL)
    # 或识别后状态为 needs_review / failed (LLM 抓不准 / TMDB 调用失败)
    # 注意: 'needs_identify' 不是合法 metadata_status 值 (schema CHECK: ok/needs_review/failed/stale)
    row = db.execute(
        "SELECT count(*) AS c FROM media_files "
        "WHERE tmdb_id IS NULL OR metadata_status IN ('needs_review', 'failed')"
    ).fetchone()
    badges["library"] = row["c"] if row else 0

    # dedup 待处理 (group count)
    try:
        from services.dedup import count_unresolved_groups  # deferred (循环 import 避免)

        badges["dedup"] = count_unresolved_groups(db)
    except (ImportError, AttributeError):
        badges["dedup"] = 0

    # organize 进行中（pending + organizing）
    row = db.execute(
        "SELECT count(*) AS c FROM auto_organize_runs "
        "WHERE status IN ('pending', 'organizing')"
    ).fetchone()
    badges["organize"] = row["c"] if row else 0

    return badges
