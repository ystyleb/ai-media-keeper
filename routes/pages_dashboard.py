"""Page: / — dashboard (system status + workers + todo + activity cards)."""

from __future__ import annotations

from flask import Blueprint, make_response, render_template

pages_dashboard_bp = Blueprint("pages_dashboard", __name__)


@pages_dashboard_bp.route("/", endpoint="dashboard")
def dashboard():
    resp = make_response(render_template(
        "pages/dashboard.html",
        current_page="dashboard",
        badges={},
    ))
    resp.headers["Cache-Control"] = "no-store"
    return resp
