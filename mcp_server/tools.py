"""MCP tool definitions + dispatch — 把 NASClient 调用变成 MCP tool schema。

设计原则：
- 每个 tool 一一对应一个 REST endpoint（不做组合 / 业务逻辑）
- inputSchema 用 JSONSchema 严格描述参数，让 Claude 准确知道怎么调
- destructive tool `prepare_destructive_action` + `confirm_destructive_action`
  严格保持契约 #1 双段式；不能合并成一个 "delete_path" tool（那会绕过 preview）
"""

from __future__ import annotations

from typing import Any, Callable

from .client import NASClient

# ─── Tool schema definitions ─────────────────────────────────

# 注意：MCP 的 inputSchema 必须是 valid JSONSchema dict（不是 Python type hint）
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_files",
        "description": (
            "列出 NAS 上指定目录的文件与子目录。返回每个 entry 的 name / path / "
            "size / mtime / hardlinks / inode / is_dir。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "绝对路径，如 /share/CACHEDEV2_DATA/media。空字符串 = 使用 NAS_BASE_PATH 默认。",
                },
            },
            "required": [],
        },
    },
    {
        "name": "find_recent_downloads",
        "description": (
            "查指定目录（通常是 qBit 的下载目录）下的视频文件，标记 has_nfo "
            "用于判断哪些刚下完还没识别。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "下载目录绝对路径，如 /share/Download/qBit。必填。",
                },
                "max_depth": {"type": "integer", "default": 2, "minimum": 1, "maximum": 5},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
            "required": ["path"],
        },
    },
    {
        "name": "get_disk_usage",
        "description": (
            "获取所有 NAS 挂载点容量（filesystem / size / used / available / use_percent / mount）。"
        ),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "find_duplicates",
        "description": ("查找重复 release 组（同一 tmdb id 的多版本）。按可释放空间降序。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "media_type": {
                    "type": "string",
                    "enum": ["movie", "tv"],
                    "description": "限定只看 movie 或 tv；不传则两类都返。",
                },
                "watched_only": {
                    "type": "boolean",
                    "default": False,
                    "description": "True = 仅展示用户已看完的组（推荐先清这些）。",
                },
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
            },
            "required": [],
        },
    },
    {
        "name": "list_archive_candidates",
        "description": ("查找已看完 + N 天未动的媒体（候选归档 / 删除）。N 默认 180 天。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "default": 180,
                    "minimum": 1,
                    "description": "看完后超过多少天才进入 stale。",
                },
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
                "offset": {"type": "integer", "default": 0, "minimum": 0},
            },
            "required": [],
        },
    },
    {
        "name": "providers_status",
        "description": (
            "查 TMDB / DeepSeek / Emby / qBit 4 个 provider 当前可用性。"
            "state ∈ {ok, auth_failed, not_configured, unreachable, error}。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {
                    "type": "boolean",
                    "default": False,
                    "description": "True = 强制 re-probe；否则 60s TTL cache。",
                },
            },
            "required": [],
        },
    },
    {
        "name": "prepare_destructive_action",
        "description": (
            "**契约 #1 第一段**：生成 preview snapshot + signed_token，"
            "不真正修改任何文件。返回 action_id + signed_token + 详细 preview。"
            "调用方查看 preview 后调 confirm_destructive_action 才执行。"
            "kind 必须 ∈ {delete, organize, nfo_write, archive, purge_provider}。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["delete", "organize", "nfo_write", "archive", "purge_provider"],
                },
                "payload": {
                    "type": "object",
                    "description": (
                        "kind-specific payload。delete 需 source + candidates；"
                        "organize 需 items；nfo_write 需 target + payload；"
                        "详情见 /api/action/preview 路由文档。"
                    ),
                    "additionalProperties": True,
                },
            },
            "required": ["kind", "payload"],
        },
    },
    {
        "name": "confirm_destructive_action",
        "description": (
            "**契约 #1 第二段**：用 signed_token 真正执行 prepare 生成的 action。"
            "签名错 / 已被消费 / 过期 → 失败。organize 大批量时返 202 + polling_url，"
            "需要后续调 action_status 轮询。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action_id": {"type": "string"},
                "signed_token": {"type": "string"},
            },
            "required": ["action_id", "signed_token"],
        },
    },
    {
        "name": "action_status",
        "description": (
            "轮询 destructive action 的执行进度（特别是 organize 大批量场景）。"
            "返 status ∈ {pending, running, succeeded, failed, needs_manual_recovery}。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"action_id": {"type": "string"}},
            "required": ["action_id"],
        },
    },
]


# ─── Tool dispatch ───────────────────────────────────────────


def _t_list_files(client: NASClient, args: dict) -> dict:
    return client.list_files(path=args.get("path") or None)


def _t_find_recent_downloads(client: NASClient, args: dict) -> dict:
    path = args.get("path")
    if not path:
        raise ValueError("path required (e.g. /share/Download/qBit)")
    return client.find_recent_downloads(
        path=path,
        max_depth=int(args.get("max_depth", 2)),
        limit=int(args.get("limit", 50)),
    )


def _t_get_disk_usage(client: NASClient, args: dict) -> dict:
    return client.get_disk_usage()


def _t_find_duplicates(client: NASClient, args: dict) -> dict:
    return client.find_duplicates(
        media_type=args.get("media_type"),
        watched_only=bool(args.get("watched_only", False)),
        limit=int(args.get("limit", 50)),
        offset=int(args.get("offset", 0)),
    )


def _t_list_archive_candidates(client: NASClient, args: dict) -> dict:
    return client.list_archive_candidates(
        days=int(args.get("days", 180)),
        limit=int(args.get("limit", 50)),
        offset=int(args.get("offset", 0)),
    )


def _t_providers_status(client: NASClient, args: dict) -> dict:
    return client.providers_status(refresh=bool(args.get("refresh", False)))


def _t_prepare_destructive_action(client: NASClient, args: dict) -> dict:
    kind = args.get("kind")
    if not kind:
        raise ValueError("kind required")
    payload = args.get("payload") or {}
    if not isinstance(payload, dict):
        raise ValueError("payload must be object")
    # **kwargs 展开 payload，让所有 kind-specific 字段都透传到 /api/action/preview
    return client.preview_action(kind=kind, **payload)


def _t_confirm_destructive_action(client: NASClient, args: dict) -> dict:
    action_id = args.get("action_id")
    signed_token = args.get("signed_token")
    if not action_id or not signed_token:
        raise ValueError("action_id and signed_token required")
    return client.confirm_action(action_id, signed_token)


def _t_action_status(client: NASClient, args: dict) -> dict:
    action_id = args.get("action_id")
    if not action_id:
        raise ValueError("action_id required")
    return client.action_status(action_id)


_DISPATCH: dict[str, Callable[[NASClient, dict], dict]] = {
    "list_files": _t_list_files,
    "find_recent_downloads": _t_find_recent_downloads,
    "get_disk_usage": _t_get_disk_usage,
    "find_duplicates": _t_find_duplicates,
    "list_archive_candidates": _t_list_archive_candidates,
    "providers_status": _t_providers_status,
    "prepare_destructive_action": _t_prepare_destructive_action,
    "confirm_destructive_action": _t_confirm_destructive_action,
    "action_status": _t_action_status,
}


def dispatch_tool_call(client: NASClient, name: str, arguments: dict) -> dict:
    """根据 tool name 路由到具体 handler；未知 tool 抛 ValueError。"""
    handler = _DISPATCH.get(name)
    if handler is None:
        raise ValueError(f"unknown tool: {name!r}; known: {list(_DISPATCH)}")
    return handler(client, arguments or {})
