"""Page: /dedup — duplicate detection view."""

from __future__ import annotations

from flask import Blueprint, make_response, render_template

pages_dedup_bp = Blueprint("pages_dedup", __name__)


@pages_dedup_bp.route("/dedup")
def dedup():
    """Dedup page (find duplicate releases + archived candidates)."""
    resp = make_response(render_template(
        "pages/dedup.html",
        current_page="dedup",
        badges={},
    ))
    resp.headers["Cache-Control"] = "no-store"
    return resp
