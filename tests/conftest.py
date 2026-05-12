"""Shared test setup: pre-set env so app.py imports cleanly + use tmp DB."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def pytest_configure(config):
    """Runs before any test module is collected.

    app.py has side effects at import time (token validation, schema init).
    We must set required env BEFORE app.py is imported anywhere.
    """
    os.environ.setdefault(
        "NAS_API_TOKEN",
        "test-token-must-be-at-least-16-chars-long-padding",
    )
    os.environ.setdefault(
        "NAS_ACTION_SIGNING_KEY",
        "test-signing-key-must-be-at-least-32-chars-long-padding",
    )
    # 测试期间禁用 BackgroundScheduler（避免 cron 在测试之间跑）
    os.environ.setdefault("NAS_DISABLE_CRON", "1")
    # SSH 永远不应该真跑：默认指向一个不存在的 host
    os.environ.setdefault("NAS_HOST", "127.0.0.1")
    os.environ.setdefault("NAS_PORT", "1")  # nonexistent port
    os.environ.setdefault("NAS_USER", "nobody")
    os.environ.setdefault("NAS_BASE_PATH", str(Path(tempfile.gettempdir()) / "nas-test-base"))
