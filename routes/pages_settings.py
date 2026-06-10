"""Page: /settings — settings hub (7 cards, each triggers config modal)."""

from __future__ import annotations

import logging
import os

from flask import Blueprint, make_response, render_template

logger = logging.getLogger(__name__)

pages_settings_bp = Blueprint("pages_settings", __name__)


def _config_status() -> dict:
    """各配置卡的"已配置"判定 — 全部读现有 loader（纯本地 config 读取，无网络 I/O）。

    失败一律按未配置处理（设置页永不因状态计算 500），但 debug log 留痕防静默吞错。
    """
    from app import (
        CONFIG_DIR,
        _emby_client,
        load_deepseek_key,
        load_organize_config,
        load_qbit_auto_organize_config,
        load_tmdb_key,
        qbit,
    )

    status: dict[str, bool] = {}
    # --- NAS ---
    try:
        status["nas"] = os.path.exists(os.path.join(str(CONFIG_DIR), "nas.json"))
    except Exception as e:
        logger.debug("_config_status[nas] failed: %s", e)
        status["nas"] = False
    # --- qBittorrent ---
    try:
        cfg = qbit.get_config()
        status["qbit"] = bool(cfg.get("url") and cfg.get("user") and cfg.get("has_password"))
    except Exception as e:
        logger.debug("_config_status[qbit] failed: %s", e)
        status["qbit"] = False
    # --- AI (TMDB + DeepSeek) ---
    try:
        status["ai"] = bool(load_tmdb_key()) and bool(load_deepseek_key())
    except Exception as e:
        logger.debug("_config_status[ai] failed: %s", e)
        status["ai"] = False
    # --- Emby ---
    try:
        status["emby"] = _emby_client() is not None
    except Exception as e:
        logger.debug("_config_status[emby] failed: %s", e)
        status["emby"] = False
    # --- 媒体库根目录 ---
    try:
        org = load_organize_config()
        status["roots"] = bool(org.get("movies_root") and org.get("tv_root"))
    except Exception as e:
        logger.debug("_config_status[roots] failed: %s", e)
        status["roots"] = False
    # --- qBit 自动整理 ---
    try:
        status["auto_organize"] = bool(load_qbit_auto_organize_config().get("enabled"))
    except Exception as e:
        logger.debug("_config_status[auto_organize] failed: %s", e)
        status["auto_organize"] = False
    return status


@pages_settings_bp.route("/settings")
def settings():
    resp = make_response(
        render_template(
            "pages/settings.html",
            current_page="settings",
            badges={},
            cfg_status=_config_status(),
        )
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp
