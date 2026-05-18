"""Page: /library — TMDB-based media library view."""

from __future__ import annotations

from flask import Blueprint, make_response, render_template

pages_library_bp = Blueprint("pages_library", __name__)


@pages_library_bp.route("/library")
def library():
    """Library page (TMDB-based catalog view)."""
    resp = make_response(
        render_template(
            "pages/library.html",
            current_page="library",
            badges={},
        )
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp
