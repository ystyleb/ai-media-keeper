"""Dev 工具：Playwright headless 截全部页面，视觉回归对照用。

用法：先起 dev server（端口 5001），然后
.venv/bin/python scripts/ui_screenshot.py [输出目录，默认 /tmp]
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:5001"
PAGES = [
    ("/", "dashboard"),
    ("/files", "files"),
    ("/library", "library"),
    ("/dedup", "dedup"),
    ("/organize", "organize"),
    ("/settings", "settings"),
]


def main() -> None:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp")
    token_path = Path(__file__).parent.parent / "config" / ".api_token"
    if not token_path.exists():
        sys.exit(f"ERROR: {token_path} 不存在 — 先启动 dev server 生成 config/.api_token")
    token = token_path.read_text().strip()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.add_init_script(f"localStorage.setItem('nas_token', '{token}');")
        for path, name in PAGES:
            page.goto(f"{BASE}{path}?token={token}", wait_until="domcontentloaded", timeout=15000)
            time.sleep(2.5)  # 等 HTMX load trigger + app.js fetch
            dest = out_dir / f"nas-ui-{name}.png"
            page.screenshot(path=str(dest))
            print(f"OK {dest}")
        browser.close()


if __name__ == "__main__":
    main()
