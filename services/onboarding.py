"""Phase D: Onboarding status check helpers."""

from __future__ import annotations

from pathlib import Path

_CONFIG_DIR = Path(__file__).parent.parent / "config"


def check_status() -> dict:
    """检查 onboarding 4 必填 + 2 可选状态.

    简化: 仅检查 config 文件存在性.
    连接验证走 /api/<provider>/test (用户主动触发).
    """
    c = _CONFIG_DIR
    return {
        "nas_ssh": "ok" if (c / "nas.json").exists() else "unconfigured",
        "qbit": "ok" if (c / "qbit.json").exists() else "unconfigured",
        "tmdb_key": "ok" if (c / ".tmdb_key").exists() else "unconfigured",
        "deepseek_key": "ok" if (c / ".deepseek_key").exists() else "unconfigured",
        "library_roots": "ok" if (c / "organize.json").exists() else "unconfigured",
        "emby": "ok" if (c / "emby.json").exists() else "unconfigured",
    }


def is_onboarded() -> bool:
    """4 必填全 ok 才算 onboarded (nas_ssh / qbit / tmdb_key / deepseek_key)."""
    status = check_status()
    return all(status[k] == "ok" for k in ("nas_ssh", "qbit", "tmdb_key", "deepseek_key"))
