"""NASVault MCP server — stdio entry point.

Run:
  NAS_API_TOKEN=<token> python -m mcp_server.server

Claude Desktop config (claude_desktop_config.json):
  {
    "mcpServers": {
      "nasvault": {
        "command": "/path/to/.venv/bin/python",
        "args": ["-m", "mcp_server.server"],
        "env": {
          "NAS_API_TOKEN": "<paste from config/.api_token>",
          "NAS_BASE_URL": "http://127.0.0.1:5001"
        }
      }
    }
  }

工具完整列表见 mcp_server/tools.py。每个 tool 都 wrap 一个 REST endpoint，
契约 #1 destructive 双段式（prepare + confirm）严格保留。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from .client import NASClient, NASClientError
from .tools import TOOL_DEFINITIONS, dispatch_tool_call

logger = logging.getLogger(__name__)


def _make_server() -> Server:
    """构造 MCP server 实例 + 注册 7 个 tool。

    用 lazy NASClient — 启动时不需要 NAS_API_TOKEN（让 list_tools 也能在
    没 token 时跑，方便 Claude Desktop 列工具诊断）。真正 call_tool 才解析 token。
    """
    server: Server = Server("nasvault")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name=spec["name"],
                description=spec["description"],
                inputSchema=spec["inputSchema"],
            )
            for spec in TOOL_DEFINITIONS
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        try:
            client = NASClient()
        except ValueError as e:
            # NAS_API_TOKEN 没设
            return [
                TextContent(
                    type="text",
                    text=f"配置错误: {e}\n请在 Claude Desktop config 里设置 env.NAS_API_TOKEN",
                )
            ]

        try:
            result = dispatch_tool_call(client, name, arguments)
        except NASClientError as e:
            return [
                TextContent(
                    type="text",
                    text=f"NAS API 错误 (HTTP {e.status_code}): {e}\n"
                    f"响应体: {json.dumps(e.body, ensure_ascii=False)[:500]}",
                )
            ]
        except ValueError as e:
            # 参数验证错误（mcp_server.tools 内 raise）
            return [TextContent(type="text", text=f"参数错误: {e}")]
        except Exception as e:
            logger.exception(f"[mcp] tool {name} crashed")
            return [
                TextContent(
                    type="text",
                    text=f"工具 '{name}' 内部错误 ({type(e).__name__}): {e}",
                )
            ]

        # 返回 pretty JSON — Claude 可直接读
        return [
            TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2),
            )
        ]

    return server


async def _async_main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    server = _make_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> None:
    """Entry point: `python -m mcp_server.server`."""
    # 友好提示：未设 NAS_API_TOKEN 时给个 hint（但不 exit — list_tools 仍可跑）
    if not os.environ.get("NAS_API_TOKEN"):
        sys.stderr.write(
            "[mcp_server] warning: NAS_API_TOKEN not set; "
            "tool calls will fail until env is configured.\n"
        )
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
