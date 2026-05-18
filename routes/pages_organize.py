"""Page: /organize — organize history + auto-organize config."""

from __future__ import annotations

from flask import Blueprint, make_response, render_template

pages_organize_bp = Blueprint("pages_organize", __name__)


@pages_organize_bp.route("/organize")
def organize():
    resp = make_response(
        render_template(
            "pages/organize.html",
            current_page="organize",
            badges={},
        )
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp
