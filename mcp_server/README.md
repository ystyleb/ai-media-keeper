# NASVault MCP Server

让 Claude Desktop / Claude Code 直接管 NAS：列文件、查磁盘、找重复、归档候选，以及通过**契约 #1 双段式**安全执行 destructive 操作（删除 / 整理 / NFO 写回 / 归档）。

## 工具列表

| Tool | 说明 |
|---|---|
| `list_files(path?)` | 列目录内容 |
| `find_recent_downloads(path, max_depth?, limit?)` | 查下载目录视频（含 has_nfo 标记） |
| `get_disk_usage()` | 磁盘容量 |
| `find_duplicates(media_type?, watched_only?, limit?, offset?)` | 重复 release 组（按可释放空间降序） |
| `list_archive_candidates(days?, limit?, offset?)` | 已看完 N 天未动的媒体 |
| `providers_status(refresh?)` | TMDB / DeepSeek / Emby / qBit 可用性 |
| `prepare_destructive_action(kind, payload)` | **契约 #1 第一段**：生成 preview + signed_token |
| `confirm_destructive_action(action_id, signed_token)` | **契约 #1 第二段**：真正执行 |
| `action_status(action_id)` | 轮询 destructive action 进度 |

## 架构

```
Claude Desktop ─── stdio ─── mcp_server.server ─── HTTP ─── NASVault Flask (5001)
                              (this package)                  (existing app.py)
```

MCP server 是**纯协议适配层**，所有业务逻辑（契约 #1 双段式 / 路径校验 / metadata grounding）都由 Flask 端持有。这样：
- 不会绕过任何 destructive 守门
- 单源契约（同一 SQL / Python 代码，不二次实现）
- MCP server 升级独立，不影响 Web UI 用户

## 安装

NASVault 主项目已含 `mcp>=1.0,<2.0` 依赖；`pip install -r requirements.txt` 即可。

## Claude Desktop 配置

编辑 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "nasvault": {
      "command": "/path/to/nas/.venv/bin/python",
      "args": ["-m", "mcp_server.server"],
      "env": {
        "NAS_API_TOKEN": "<paste from config/.api_token>",
        "NAS_BASE_URL": "http://127.0.0.1:5001"
      },
      "cwd": "/path/to/nas"
    }
  }
}
```

**关键**：
- `command` 指向 NASVault 的 `.venv/bin/python`（保证 mcp / requests / openai 等依赖装在了正确环境）
- `cwd` 必须设成 NASVault repo 根目录（让 `python -m mcp_server.server` 找到 module）
- `NAS_API_TOKEN` 从 `config/.api_token` 复制（chmod 600 的文件）
- 改完 config 后**完全退出 Claude Desktop 再重启**

## Claude Code 配置（CLI）

```bash
claude mcp add nasvault \
  -- /path/to/nas/.venv/bin/python -m mcp_server.server \
  --env NAS_API_TOKEN=<token> \
  --env NAS_BASE_URL=http://127.0.0.1:5001
```

或编辑 `~/.claude.json` 手动添加。

## 安全模式

**默认**：stdio + localhost (`127.0.0.1:5001`)。
- 不开 TCP 监听 — 攻击者 reach 不到 MCP 进程
- Flask 端 listen `0.0.0.0` 是另一回事；如果想限制 Flask 也只本机，改 `app.py:5298` `host=` 或加防火墙

**远程访问**（笔记本 ↔ NAS）：
- 推荐 [Tailscale](https://tailscale.com) — 加 Flask host 到 tailnet，`NAS_BASE_URL=http://nas.tailnet:5001`，所有流量自动 WireGuard 加密 + ACL
- 不要直接公网暴露 Flask（即使有 API_TOKEN）— stdio MCP server 在本地 trust boundary 内才安全

## 测试

```bash
# Unit tests（28 个）
.venv/bin/python -m pytest tests/unit/test_mcp_server.py -v

# Smoke test：列工具
NAS_API_TOKEN=fake-token .venv/bin/python -c "
from mcp_server.tools import TOOL_DEFINITIONS
for t in TOOL_DEFINITIONS:
    print(f'{t[\"name\"]}: {t[\"description\"][:60]}...')
"
```

## 契约 #1 destructive 流程示例

通过 MCP 删除一组重复文件：

```
1. Claude: prepare_destructive_action(kind="delete", payload={
     "source": "dedup",
     "snapshot_mode": "strict",
     "candidates": [
       {"path": "/share/movies/X.4k.mkv",
        "expected_inode": 123456,
        "expected_size": 50000000000,
        "expected_mtime": 1747200000}
     ]
   })
   → {action_id: "abc", signed_token: "xyz", preview: {space_freed: "46.6 GB", ...}}

2. (Claude 向用户展示 preview，等用户同意)

3. Claude: confirm_destructive_action(action_id="abc", signed_token="xyz")
   → {status: "succeeded", result: {space_freed_bytes: 50000000000, ...}}
```

**无法绕过的守门**：
- snapshot_mode="strict" + 三元组（inode/size/mtime）一致才允许 confirm（防 preview-confirm 间被 mv 偷换）
- signed_token 是 HMAC，伪造立即被拒
- token 单次消费、10 分钟过期、status 三段状态机
- 所有这些由 Flask 端 `services/destructive_action.py` enforce，MCP 层无法绕过
