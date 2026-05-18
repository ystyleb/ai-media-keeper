"""Page: /settings — settings hub (7 cards, each triggers config modal)."""

from __future__ import annotations

from flask import Blueprint, make_response, render_template

pages_settings_bp = Blueprint("pages_settings", __name__)


@pages_settings_bp.route("/settings")
def settings():
    resp = make_response(
        render_template(
            "pages/settings.html",
            current_page="settings",
            badges={},
        )
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp
