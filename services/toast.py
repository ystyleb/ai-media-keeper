"""Phase E: Toast helper — add HX-Trigger header to Flask response.

Server-side trigger toast notification via HTMX HX-Trigger header convention.
Frontend (base.html) listens for htmx:afterRequest event with HX-Trigger='{"toast": {...}}'
and dispatches @toast.window event consumed by Alpine toastStack().
"""

from __future__ import annotations

import json
from typing import Literal

SEVERITY_LITERAL = Literal["success", "warning", "error", "info"]


def add_toast(response, severity: str, message: str):
    """Attach a toast to Flask response via HX-Trigger header.

    Args:
        response: Flask Response object (from make_response / jsonify).
        severity: 'success' | 'warning' | 'error' | 'info'
        message: Display text (≤ 100 chars recommended).

    Returns:
        The same response (mutated, for chain calls).
    """
    payload = {"toast": {"severity": severity, "message": message}}
    response.headers["HX-Trigger"] = json.dumps(payload)
    return response


def add_multi_toast(response, toasts: list):
    """Multiple toasts in one response.

    HX-Trigger only fires once per response; we stack them under 'toasts' (array) —
    frontend dispatches one @toast event per item.

    Args:
        response: Flask Response.
        toasts: list of {"severity": str, "message": str} dicts.
    """
    # 简化：take first toast only (multi-toast 需扩展 frontend listener).
    # Future: server emit `<script>...</script>` in response body, or use SSE.
    if toasts:
        return add_toast(response, toasts[0].get("severity", "info"), toasts[0].get("message", ""))
    return response
