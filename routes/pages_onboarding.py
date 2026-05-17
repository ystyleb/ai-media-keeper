"""Page: /onboarding wizard for first-time setup."""

from __future__ import annotations

from flask import Blueprint, make_response, redirect, render_template, url_for

pages_onboarding_bp = Blueprint("pages_onboarding", __name__)

STEPS = ["nas", "qbit", "tmdb", "deepseek", "done"]


@pages_onboarding_bp.route("/onboarding")
def onboarding_root():
    """GET /onboarding — 默认跳 nas step."""
    return onboarding(step="nas")


@pages_onboarding_bp.route("/onboarding/step/<step>")
def onboarding(step: str = "nas"):
    """Onboarding wizard. step ∈ STEPS."""
    from services.onboarding import check_status, is_onboarded

    if step not in STEPS:
        return redirect(url_for("pages_onboarding.onboarding", step="nas"))

    status = check_status()
    step_index = STEPS.index(step)

    resp = make_response(render_template(
        "pages/onboarding.html",
        current_page="onboarding",
        badges={},
        step=step,
        step_index=step_index,
        steps=STEPS,
        status=status,
        is_onboarded=is_onboarded(),
    ))
    resp.headers["Cache-Control"] = "no-store"
    return resp
