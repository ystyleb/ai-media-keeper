"""UI drawer fragments — HTMX endpoints for destructive / long-running action drawer.

Pattern: /ui/drawer/<action>/<step> render HTML fragment with hx-target='#drawer-content'.
Internal: call _do_action_preview + action_confirm JSON helpers from app.py.

Phase C MVP:
  - delete drawer: preview → confirm → done (3 step)
  - organize 单文件 drawer: preview → confirm → done
  - nfo_write drawer: preview → confirm → done
  - Generic confirm (forwards to app.action_confirm)
  - Polling status for async actions (organize batch)
"""

from __future__ import annotations

import json
from functools import wraps

from flask import Blueprint, render_template, request

ui_drawer_bp = Blueprint("ui_drawer", __name__, url_prefix="/ui/drawer")


def _require_token(view):
    """Inline token check — mirrors app.require_token but importable at module level."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        from app import API_TOKEN  # deferred — avoid circular import at module load

        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token or token != API_TOKEN:
            from flask import jsonify

            return jsonify({"error": "Unauthorized"}), 401
        return view(*args, **kwargs)

    return wrapped


def _parse_preview_response(preview_data):
    """Extract dict from _do_action_preview result.

    Returns (result_dict, error_tuple_or_None).
    error_tuple = (error_message_str, http_status_int).
    """
    # _do_action_preview returns either (Response, status_code) tuple on error
    # or a single Response object on success.
    if isinstance(preview_data, tuple):
        body, status = preview_data
        try:
            err = json.loads(body.get_data(as_text=True))
            return None, (err.get("error", "preview failed"), status)
        except Exception:
            return None, ("preview failed", status)

    try:
        result = json.loads(preview_data.get_data(as_text=True))
        return result, None
    except Exception:
        return None, ("preview response 解析失败", 500)


# ---------------------------------------------------------------------------
# Delete drawer
# ---------------------------------------------------------------------------


@ui_drawer_bp.route("/delete/preview", methods=["POST"])
@_require_token
def delete_preview():
    """Delete drawer step 1: preview. POST JSON body forwarded to _do_action_preview."""
    from app import _do_action_preview  # deferred

    data = request.json or {}
    paths = data.get("paths", [])
    if not paths:
        return render_template("drawer/_error.html", error="无文件选中"), 400

    # Build candidates list from paths (file-browser source, lenient mode)
    candidates = [{"path": p} for p in paths]
    preview_raw = _do_action_preview(
        "delete",
        {
            "candidates": candidates,
            "source": "file_browser",
            "snapshot_mode": "lenient",
            "delete_torrents": data.get("delete_torrents", False),
        },
    )

    result, err = _parse_preview_response(preview_raw)
    if err:
        return render_template("drawer/_error.html", error=err[0]), err[1]

    return render_template(
        "drawer/delete_preview.html",
        action_id=result.get("action_id"),
        signed_token=result.get("signed_token"),
        preview=result,
    )


# ---------------------------------------------------------------------------
# Organize drawer
# ---------------------------------------------------------------------------


@ui_drawer_bp.route("/organize/preview", methods=["POST"])
@_require_token
def organize_preview():
    """Organize 单文件/目录 drawer step 1: preview. POST body forwarded to _do_action_preview."""
    from app import _do_action_preview  # deferred

    data = request.json or {}
    preview_raw = _do_action_preview("organize", data)

    result, err = _parse_preview_response(preview_raw)
    if err:
        return render_template("drawer/_error.html", error=err[0]), err[1]

    return render_template(
        "drawer/organize_preview.html",
        action_id=result.get("action_id"),
        signed_token=result.get("signed_token"),
        preview=result,
        preview_items=result.get("items", []),  # avoid Jinja2 dict.items() collision
    )


# ---------------------------------------------------------------------------
# NFO write drawer
# ---------------------------------------------------------------------------


@ui_drawer_bp.route("/nfo_write/preview", methods=["POST"])
@_require_token
def nfo_preview():
    """NFO write drawer step 1: preview. POST body forwarded to _do_action_preview."""
    from app import _do_action_preview  # deferred

    data = request.json or {}
    preview_raw = _do_action_preview("nfo_write", data)

    result, err = _parse_preview_response(preview_raw)
    if err:
        return render_template("drawer/_error.html", error=err[0]), err[1]

    return render_template(
        "drawer/nfo_preview.html",
        action_id=result.get("action_id"),
        signed_token=result.get("signed_token"),
        preview=result,
    )


# ---------------------------------------------------------------------------
# Generic confirm (all action kinds)
# ---------------------------------------------------------------------------


@ui_drawer_bp.route("/<action>/confirm", methods=["POST"])
@_require_token
def action_confirm_drawer(action):
    """Generic confirm endpoint for all action kinds (delete/organize/nfo_write/...).

    Reads action_id + signed_token from request.json, calls action_confirm() internally,
    then renders drawer/<action>_done.html or drawer/_progress.html.
    """
    from app import action_confirm as _api_confirm  # deferred; same request context

    # Call the existing route handler directly (same Flask request context, same request.json)
    response = _api_confirm()

    # Unpack result
    if isinstance(response, tuple):
        body, status = response
    else:
        body, status = response, 200

    try:
        result = json.loads(body.get_data(as_text=True))
    except Exception:
        return render_template("drawer/_error.html", error="confirm response 解析失败"), 500

    if status not in (200, 202):
        return render_template(
            "drawer/_error.html", error=result.get("error", "confirm failed")
        ), status

    action_id = result.get("action_id") or (request.json or {}).get("action_id")
    drawer_status = result.get("status", "unknown")

    # Async (organize batch) → progress polling fragment
    if drawer_status in ("running", "pending"):
        return render_template(
            "drawer/_progress.html",
            action_id=action_id,
            action=action,
            result=result,
        )

    # Synchronous completion → try action-specific done template, fall back to generic
    try:
        return render_template(
            f"drawer/{action}_done.html",
            result=result,
            action_id=action_id,
        )
    except Exception:
        return render_template(
            "drawer/_done.html",
            result=result,
            action_id=action_id,
            action=action,
        )


# ---------------------------------------------------------------------------
# Polling status for async actions
# ---------------------------------------------------------------------------


@ui_drawer_bp.route("/action/<action_id>/status", methods=["GET"])
@_require_token
def action_status(action_id):
    """Polling status for async actions (organize batch etc.).

    GET → progress fragment (continues polling via hx-trigger every 2s)
       or done fragment (HTMX stops polling on swap).
    """
    from app import get_db  # deferred

    db = get_db()
    row = db.execute(
        "SELECT kind, status FROM destructive_actions WHERE action_id = ?",
        (action_id,),
    ).fetchone()

    if row is None:
        return render_template("drawer/_error.html", error="action not found"), 404

    if row["status"] in ("running", "pending"):
        return render_template(
            "drawer/_progress.html",
            action_id=action_id,
            action=row["kind"],
            result={"status": row["status"]},
        )

    # Terminal state
    return render_template(
        "drawer/_done.html",
        action_id=action_id,
        action=row["kind"],
        result={"status": row["status"]},
    )
