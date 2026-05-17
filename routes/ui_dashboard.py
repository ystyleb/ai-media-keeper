"""UI dashboard fragments — HTMX poll endpoints for dashboard cards."""

from __future__ import annotations

from functools import wraps

from flask import Blueprint, render_template

ui_dashboard_bp = Blueprint("ui_dashboard", __name__, url_prefix="/ui")


def _require_token(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        from app import require_token  # deferred (循环 import 避免)
        return require_token(view)(*args, **kwargs)
    return wrapped


@ui_dashboard_bp.route("/dashboard/system", methods=["GET"])
@_require_token
def system_card():
    """Provider 状态卡片 (复用 ui_status providers data)."""
    from app import get_cached_providers_status
    from routes.ui_status import _to_status_segments
    raw = get_cached_providers_status(force=False)
    providers = _to_status_segments(raw)
    return render_template("partials/dashboard/system_card.html", providers=providers)


@ui_dashboard_bp.route("/dashboard/workers", methods=["GET"])
@_require_token
def workers_card():
    """后台 worker 卡片 (复用 ui_status workers data)."""
    from routes.ui_status import _aggregate_running_workers
    workers = _aggregate_running_workers()
    return render_template("partials/dashboard/workers_card.html", workers=workers)


@ui_dashboard_bp.route("/dashboard/todo", methods=["GET"])
@_require_token
def todo_card():
    """待处理卡片 (复用 ui_status badges data)."""
    from routes.ui_status import _compute_sidebar_badges
    badges = _compute_sidebar_badges()
    return render_template("partials/dashboard/todo_card.html", badges=badges)
