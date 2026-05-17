"""Page: /files — file browser (cut from old index.html main area)."""

from __future__ import annotations

from flask import Blueprint, make_response, render_template

pages_files_bp = Blueprint("pages_files", __name__)


@pages_files_bp.route("/files")
def files():
    """File browser page."""
    resp = make_response(render_template(
        "pages/files.html",
        current_page="files",
        badges={},  # HTMX poll /ui/sidebar/badges 会覆盖
    ))
    resp.headers["Cache-Control"] = "no-store"
    return resp
