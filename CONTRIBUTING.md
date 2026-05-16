# Contributing to NASVault

NASVault 欢迎 PR + issue。本文档分**4 个改动层**，每层有不同的难度 / review 要求。

## 改动层（按风险递增）

### L1: 元数据 / Provider 适配（lowest risk）

加新 metadata provider（豆瓣 / TVDB / Anidb / Trakt）：
- 实现 `services/metadata/base.py::MetadataProvider` 抽象（search / lookup_by_id / test_connection）
- 在 `app.py` 加 BYOK config helper + UI 配置 modal + `/api/providers/status` probe
- 写 unit tests（mock requests 即可，不需要真打 API）

无副作用，最容易合并。

### L2: BT 客户端 adapter

加 Transmission / Deluge / rTorrent 支持：
- 抽 `services/bittorrent.py` 抽象（test_connection / get_torrents / find_by_paths / delete_torrents）
- 现有 `QBitClient` 改实现该接口
- 配置 UI 加 "type" 选择器

测试时**必须**用真实 BT 客户端 + 测种子（dummy 1KB 文件做种），不允许只 mock — qBit 的 cookie session / pause 行为 mock 不准。

### L3: Watch source provider（观看进度）

加 Plex / Jellyfin / Trakt 同步：
- 实现 `services/watch_sync.py::WatchSourceProvider` 抽象（test_connection / fetch_watched）
- 至少处理 movie + tv episode 两类，TV 类必须支持 `mapping_status ∈ {mapped, fallback_se, unmapped}`
- 写至少一个 fixture 测试（捕获真实 API response 落 JSON file），不允许凭想象编 fixture

### L4: Mutating 行为（highest risk）

**任何写文件 / 写 DB / 调外部 destructive API 的改动**：
- 必须走契约 #1 `/api/action/preview` + `/api/action/confirm` 双段式
- 必须在 `db/schema.sql` 的 `destructive_actions.kind` CHECK 加新值（并写 migration）
- 必须有 inode-anchored execute（删 / 改名 用 `find -inum N`，不用裸 path）
- 必须有至少 1 轮 codex / code-reviewer agent review；review 抓的 BLOCKER 全修后再 PR
- 必须有 **端到端冒烟**记录在 PR description（真跑一次，第三方验证 — SSH `ls`、qBit API、Emby web UI）

参考 Phase 4A/B/C 的实现轨迹（commit log + ROADMAP.md）作为模板。

## 开发流程

```bash
git clone <fork-url>
cd nas
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 起 dev server
python3 app.py

# 跑测试
.venv/bin/python -m pytest tests/unit -q
```

## 不变量（任何 PR 都不能破坏）

1. **AI 不直接 destructive**：所有 mutating 走契约 #1 preview+confirm 双段式
2. **LLM grounded**：LLM 只从 candidates 选 id，永不生成 id（contract test enforce）
3. **Snapshot ground truth**：confirm 阶段重读 SSH，不信 client / 不信缓存
4. **Inode-anchored execute**：删 / 归档用 `find -inum`，不用裸 path
5. **DeepSeek-first**：默认 LLM = DeepSeek V4，不 default Anthropic（成本敏感）
6. **零 env，全 UI 配置**：所有 key / 密码走 `config/*` chmod 600，不强制 env
7. **单 worker**：organize_runner / scanner 用进程内 lock，不支持多 worker（gunicorn.conf.py + app.py 5 层 hard guard 拦下任何 multi-worker 启动尝试）

## PR Checklist

- [ ] 测试 pass：`pytest tests/unit -q` 全绿
- [ ] 新代码有对应测试（route 用 test_client + monkeypatch boundary，参考 `tests/unit/test_*_action.py`）
- [ ] L4 改动有 codex / code-reviewer agent review 痕迹 + 端到端冒烟
- [ ] 不破坏上面 7 条不变量
- [ ] ROADMAP.md 更新（功能加项 / 状态变化）
- [ ] README.md 更新（user-visible 变化）
- [ ] commit message 描述 "why" 不只是 "what"

## 平台兼容矩阵

NASVault 目标支持：

| 平台 | 状态 | 注意 |
|---|---|---|
| **QNAP** (QTS 5.x) | ✅ 主要测试平台 | `/share/CACHEDEV*_DATA/` 路径、BusyBox `find` / `stat`（不支持 GNU 长 flag） |
| **Synology** (DSM 7.x) | ⚠️ 未官方测试 | 路径 `/volume1/` 不同，`stat` 可能行为有差异 — 欢迎 PR + bug 反馈 |
| **任意 Linux NAS** (Ubuntu/Debian/Arch) | ✅ | GNU coreutils 全特性，无适配压力 |
| **macOS** (本机开发) | ✅ | 远程 SSH 到 NAS；本机不能直接当 NAS（`stat -c` 不支持） |
| **TrueNAS / unRAID** | ❓ 未测试 | ZFS / btrfs / xfs hardlink 行为不同，PR 欢迎 |

## 命名约定 / coding style

- Python 3.11+，type hints 必须（routes / services 必须 type-annotated）
- ruff 默认规则（lint 一并 fix 简单错误）：`ruff check . && ruff format .`
- 中文 user-facing 文案（log / error message），英文 code identifier
- 跨进程边界（HTTP / SSH / DB）的字段名用 snake_case；前端 JS 也是 snake_case 跟后端对齐

## 报 bug

- 描述真实数据特征（文件名样本 / 库大小 / TMDB id），不用泛泛"识别不准"
- 附 `tail -100 nasvault.log`（如果 log 含密钥 / token 自行脱敏）
- 附 `git log --oneline -5`（看你跑的是哪个版本）

## 路线图 + 不做的事

详见 [ROADMAP.md](ROADMAP.md)。

明确**不做**的：
- 自己的 BT 下载器（重复造 qBit/Transmission 轮子）
- 自己的 media server（重复造 Plex/Jellyfin）
- 移动 app（web 响应式够用）
- 完全无人值守 agent（v1 永久 `suggest_only`，AI 不主动 destructive）
- 多用户 / 团队功能（个人工具定位）

如果你的需求在"不做"清单里，建议 fork 一份在自己分支折腾，不强求 upstream 接受。
