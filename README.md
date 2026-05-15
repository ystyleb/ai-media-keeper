# AI Media Keeper

AI 原生的影视资源管理器（Phase 4A）。Web 版的 NAS 媒体库，专为 **QNAP / Synology / 任意 Linux NAS** 设计，针对 **PT 玩家做硬链接 + qBittorrent 种子的联动删除 + AI 自动识别媒体元数据 + 下载完成后一键整理到媒体库**优化。

> **解决的痛点**：用 SMB / Samba 删一部已经在 qBittorrent 做种的剧时，种子会变成 errored 状态扣保种率；手动到 qBit 里再删一遍又麻烦。这个工具一次操作把硬盘文件 + 关联硬链接 + qBit 种子（含下载文件）一起干掉，磁盘空间真正释放。同时 AI 自动识别电影/剧集 → 海报 + 简介 + 评分 + 演员，按 TMDB 浏览整个媒体库。下载完后一键整理 → atomic hardlink 到媒体库 + 写 NFO，保种不断，待 Plex/Emby 下次扫描即可识别。

## 核心功能

### 整理到媒体库（Phase 4A 新增）
- 🚚 **下载完一键整理**：详情面板「整理到媒体库」按钮 → preview 显示「源/识别/目标路径/NFO」→ confirm 后 atomic hardlink。源文件 inode 不变 → qBittorrent 继续保种
- 🎬 **自动目录约定**：电影 → `MOVIES_ROOT/<Title (Year)>/<basename>.mkv`，剧集 → `TV_ROOT/<Series (Year)>/Season NN/<basename>.mkv`，`tvshow.nfo` 落 series 根（Plex/Emby 标准结构）
- 📝 **NFO 自动生成**：从 TMDB cache 构造 NFOPayload → 写 `<movie>` / `<episodedetails>` / `<tvshow>`（含 uniqueid + plot + cast + genres + rating）。已有 NFO 不覆盖（atomic create-only ln 保证）
- ⚙️ **MOVIES_ROOT / TV_ROOT UI 配置**：topbar 「媒体库」按钮 → modal 输入两个根 + SSH 测试目录可写 + 落盘 `config/organize.json`
- 🔒 **契约 #6 双 inode 锚定**：preview 抓 src inode → confirm 阶段（a）验 src inode 不变（防 mv 偷换）+（b）ln 后验 dst inode == src inode（保证真 hardlink 不是 cp）
- 🛡️ **race 防护**：`[ -d dst ]` pre-check + `[ ! -f dst ]` post-stat 防 silent ln-into-dir；冲突时不自动 cleanup（POSIX 无 atomic verify-then-unlink），返 orphan path hint 让 user SSH 手工查
- 📊 **部分成功语义**：hardlink ok + NFO 失败 = `status='succeeded' + nfo_status='failed: ...'`，UI 单独 warning 提示「可手工补 NFO」，不回滚 hardlink

### 媒体管理
- 🎬 **AI 自动识别**：guessit 解析文件名 → TMDB 搜索候选 → DeepSeek V4 grounded select（**契约 #3：LLM 永远只从真实候选里选 ID，不生成 ID**）
- 📚 **库视图**：按 TMDB 浏览整个媒体库 — 海报墙 + 标题 + 年份 + 评分；按 movie/tv/年份/评分筛选 + 标题搜索（中英文 + 文件名任一命中）
- 🤖 **背景全库扫描**：threading 后台 worker，可中断恢复（scan_runs / scan_items 状态机），SQLite 缓存命中跳过未变动文件
- 📝 **NFO 写回**：识别后一键回写 Emby/Jellyfin/Kodi 兼容 `.nfo` 给本地媒体服务器读

### 重复 release 检测 + 清理（Phase 3）
- 🔍 **重复检测**：`GROUP BY tmdb_id, season, episode` 找出多份压制 → 按 quality_score（分辨率/HDR/source/codec/release_group 权重）推荐保留哪份
- 🎯 **Quality scoring**：硬编码默认权重，UI 可调（`POST /api/dedup/weights` 写回 `dedup_weights` 表）；NaN/inf/negative 拒绝
- 📺 **Emby 观看进度**：拉 IsPlayed 列表（Episode `ProviderIds.Tmdb` 是 episode id 不是 series id，自动通过 SeriesId 反查 series tmdb）；映射置信度 `mapped`/`fallback_se`/`unmapped`
- 🗂️ **已看完归档候选**：已看 + 180+ 天没动的 → 列表展示供手动清理
- 🔒 **Strict 删除契约**：dedup 来源删除强制 `snapshot_mode=strict`，每个 candidate 必须含 `expected_inode/size/mtime`；服务端 SSH 重读 + diff → 409 blocked 若 mismatch

### 文件浏览 + 联删
- 📁 通过 SSH 列目录、查看磁盘用量
- 🔗 **硬链接探测**：自动识别硬链接 + 反查同 inode 的所有路径
- 🗑️ **联删**：选中目录 / 文件 → 预览**真实占用** + **关联硬链接** + **qBit 种子** + **将执行的 SSH 命令** → signed token 一键执行
- 🔒 **契约 #1 Destructive Action**：所有 mutating 操作走统一 `/api/action/preview` + `/api/action/confirm`（HMAC 签名 token + inode-anchored execute 防 TOCTOU）

### 文件名陷阱处理
- 🎭 **特典/花絮检测**：`Extras-NN` / `Featurette` / `Interview` / `Trailer` / `Sample` / `Deleted Scenes` / `Making Of` / `Behind The Scenes` / `Bloopers` → `media_type='extra'`，**不调 TMDB**（省 API + 避免 mismatch），库视图默认隐藏，主片详情聚合显示
- 💿 **多盘分段**：`BD1` / `BD2` / `Disc1` / `CD2` / `DVD2` → BD1 当主片走 TMDB，BD2+ 标 `media_type='part'`（老电影长片分盘），库视图隐藏，主片详情聚合显示
- 📖 **续集 "Part N"**：guessit 把 `The.Godfather.Part.II` 解析成 `title="The Godfather", part=2`，自动把 `Part N` 合并回 title 让 TMDB 搜到正确续集
- 🚫 **BDMV/STREAM/CERTIFICATE/AUXDATA** 路径排除：扫描跳过蓝光镜像内部的 `.m2ts` 噪音文件
- 📅 **Year mismatch penalty**：候选 year 跟 query year 不一致时 `-0.2`，避免 title-exact 旧片胜过 title-substring 新片

### 安全
- 🪪 **API Token 鉴权**：所有路由都需 `Authorization: Bearer <token>`，首次启动自动生成到 `config/.api_token`（chmod 600）
- 🛡️ **路径沙箱**：所有 SSH 操作限制在 `NAS_BASE_PATH` 下，shell 元字符拦截 + `shlex.quote` 转义 + `_reject_base_path` 防整盘 `rm -rf`
- 🔏 **HMAC signed token**：destructive 操作的 confirm 必须带签名 + 一次性消费 + 三段状态机（pending→running→succeeded/failed）

## 工作原理

1. Flask 后端通过 SSH（`ControlMaster` 连接复用）执行 NAS 上的命令
2. 路径匹配做 `readlink -f` 规范化（处理 QNAP `/share/<name>` → `/share/CACHEDEV*_DATA/<name>` symlink）
3. qBittorrent 通过 WebUI API 联动（登录复用 cookie）
4. SQLite WAL mode 缓存识别结果 + scan_runs/scan_items 任务队列 + destructive_actions 状态表
5. TMDB v3 + DeepSeek V4（OpenAI-compatible SDK）通过 BYOK 配置；密钥落盘 `config/.tmdb_key` / `config/.deepseek_key`（chmod 600，不进 env）

## 快速开始

### 1. 准备 NAS 端

确保你能从开发机 SSH 免密登录 NAS：

```bash
ssh-copy-id -p 22 admin@192.168.1.100
ssh -p 22 admin@192.168.1.100 'echo OK'   # 应输出 OK 不要密码
```

确保 qBittorrent 的 WebUI 已启用并能从开发机访问。

### 2. 安装依赖

```bash
git clone https://github.com/ystyleb/ai-media-keeper.git
cd ai-media-keeper
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. 启动（零环境变量）

```bash
python3 app.py
```

首次启动会**自动生成 API token**并打印到 stderr，类似：

```
============================================================
  Generated new API token → config/.api_token
  Token (copy into UI on first visit):

    9a3a864effbac0d7447496dd1dfadc7db760688a7fdb179454643d2e528bb779

  Persists across restarts. Delete the file to rotate.
============================================================
```

浏览器打开 http://127.0.0.1:8080，弹窗粘贴 token 即可。

### 4. UI 里配置（不再用 env）

顶部右上角设置图标：
- **NAS**：host / port / user / base_path（落 `config/nas.json`）
- **qBit**：URL / user / password（user url 落 `config/qbit.json`；password 落 `config/.qbit_pass` chmod 600）
- **TMDB API key**：免费申请 [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api)（落 `config/.tmdb_key`）
- **DeepSeek API key**：免费 [platform.deepseek.com](https://platform.deepseek.com)（落 `config/.deepseek_key`）

DeepSeek 用于 grounded LLM 候选选择，没配也能跑（fallback 到 heuristic），但识别准确率会降。

### 5. 扫库

库视图 → 右上"开始扫描" → worker 后台跑（可中断、断点续）→ 完成后整个媒体库以海报墙呈现。

## 配置文件

所有配置都在 `./config/` 目录（已 `.gitignore`），不用环境变量：

| 文件 | 内容 | 权限 |
|---|---|---|
| `config/.api_token` | API 鉴权 token（首次启动自动生成 64 hex） | 600 |
| `config/.signing_key` | 契约 #1 HMAC 签名密钥（首次启动自动生成） | 600 |
| `config/.tmdb_key` | TMDB API key（UI 配置） | 600 |
| `config/.deepseek_key` | DeepSeek API key（UI 配置） | 600 |
| `config/.qbit_pass` | qBit 密码（UI 配置） | 600 |
| `config/qbit.json` | qBit url + user（明文） | 644 |
| `config/nas.json` | NAS host / port / user / base_path（明文） | 644 |
| `config/actions.db` | SQLite：destructive_actions / media_files / scan_runs / scan_items | 644 |

### Env override（可选，CI / 多机部署用）

| 环境变量 | 作用 |
|---|---|
| `NAS_API_TOKEN` | 强制 token，覆盖 `config/.api_token` |
| `NAS_ACTION_SIGNING_KEY` | （可选）显式签名密钥；不设则首次启动自动生成并落 `config/.signing_key` chmod 600 |
| `NAS_HOST` / `NAS_PORT` / `NAS_USER` / `NAS_BASE_PATH` | 强制 NAS 连接，覆盖 `config/nas.json` |
| `QBIT_URL` / `QBIT_USER` / `QBIT_PASS` | 强制 qBit 配置 |

## API

所有路由都需要 `Authorization: Bearer <API_TOKEN>`：

### 文件 + 联删
| 路由 | 说明 |
|---|---|
| `GET /api/disk` | NAS 磁盘用量 |
| `GET /api/files?path=...` | 列目录 |
| `GET /api/hardlinks?path=...` | 当前目录硬链接扫描 |
| `GET /api/inode/<inode>` | 反查同 inode 的所有路径 |
| `GET /api/file-content?path=...` | 读取小型文本文件（含 .nfo 结构化解析） |
| `POST /api/action/preview` | 契约 #1 统一 preview（kind=`delete`/`nfo_write`） |
| `POST /api/action/confirm` | 契约 #1 统一 confirm（带 signed token） |
| `POST /api/delete-preview` | 旧路径 alias 到 `/api/action/preview` (kind='delete')，保留兼容 |

### 元数据 / AI 识别
| 路由 | 说明 |
|---|---|
| `POST /api/metadata/identify` | 单文件按需 TMDB 识别 + LLM grounded select |
| `POST /api/metadata/preview-nfo-write` | 预览 NFO 写回 → signed token → `/api/action/confirm` |

### 全库扫描
| 路由 | 说明 |
|---|---|
| `POST /api/scan/start` | 开启 base_path 全库扫描 worker（已在跑则拒绝） |
| `GET /api/scan/status?id=<run_id>` | 当前扫描进度（files_total/done/skipped/failed） |
| `POST /api/scan/abort?id=<run_id>` | 中断当前扫描（worker 下次 loop 检查时退出） |
| `GET /api/scan/runs` | 最近 N 次扫描历史 |
| `GET /api/scan/failed?id=<run_id>` | 某次扫描里 failed 的 item 列表 |

### 库视图
| 路由 | 说明 |
|---|---|
| `GET /api/library/items` | 库主查询（media_type / year_from / year_to / q / sort / limit / offset） |
| `GET /api/library/stats` | 库总览（总数 / by_media_type / by_decade / top_genres / vote 直方图） |
| `GET /api/library/companions-in-dir?path=<main_path>` | main feature 同目录的附属：parts（多盘）+ extras（花絮） |
| `GET /api/library/watched-stale?days=N&limit&offset` | 已看完 + N 天没动的归档候选（3 分支 UNION：movie / tv-episode_id / tv-series+s+e） |

### 重复检测（Phase 3）
| 路由 | 说明 |
|---|---|
| `GET /api/dedup/groups?media_type&watched_only&limit&offset` | 重复 release 组（按 deletable bytes 倒序） |
| `GET /api/dedup/weights` | 当前权重 + canonical hash |
| `POST /api/dedup/weights` | 原子更新权重（拒绝 NaN/inf/negative）+ 重算 hash |
| `POST /api/dedup/refresh` | 后台批量 recompute media_files.quality_score 缓存（500 行一 batch） |

### 观看进度（Phase 3）
| 路由 | 说明 |
|---|---|
| `GET/POST /api/config/emby` | url + user_id + api_key（key 落 `config/.emby_key` chmod 600） |
| `POST /api/config/emby/test` | 临时验证连接 |
| `POST /api/watch/sync` | 启动后台同步（partial unique index 保证同 provider 单飞） |
| `GET /api/watch/sync/status?id=<run_id>` | 同步进度 + counters |
| `GET /api/watch/status` | provider 状态（ok/not_configured/auth_failed/...) |

### 配置
| 路由 | 说明 |
|---|---|
| `GET/POST /api/config/{nas,qbit,tmdb,deepseek,emby}` | 配置管理（落 `config/`） |
| `POST /api/config/{qbit,tmdb,deepseek,emby}/test` | 连接性测试 |

## 安全注意

- `validate_path` 强制路径在 `NAS_BASE_PATH` 之内 + 拒绝路径遍历 + 拦截 shell 元字符
- `_reject_base_path` 防止 `rm -rf` 整个根目录
- 契约 #1 HMAC token + 一次性消费防 replay attack
- 契约 #2 inode-anchored execute（`find -inum N -delete` 而非裸 `rm <path>`）防 TOCTOU
- 默认绑定 `127.0.0.1:8080`，**不要直接暴露到公网**
- 如需远程访问：放到 Tailscale / WireGuard / 反向代理 + auth 后面，不要简单 NAT 转发

## 开发

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

测试：

```bash
.venv/bin/python -m pytest tests/unit/ -q
# 284 passed (Phase 3 complete)
```

生产部署用 `gunicorn`，**强制单 worker**（`organize_runner` / `scanner` 用进程
内 lock + abort flag，多 worker 会让 batch organize / 全库扫描互相打架）：

```bash
pip install gunicorn
# 单 worker 是硬要求 — 即使你忘了 -w 1，第二个 worker fork 后 fcntl.flock
# 抢 config/.worker.lock 失败会立刻 sys.exit(1)，避免静默并发。
gunicorn -b 127.0.0.1:8080 -w 1 app:app
```

如果要多 worker（不推荐，Phase 4B 阶段不支持），需要等 Phase 4C 升级到
SQLite lease 跨进程互斥。**当前 hard guard**：
- `app.py` 启动期检查 `WEB_CONCURRENCY != 1` 拒启动
- `fcntl.flock(config/.worker.lock)` 跨进程抢锁，第二个 worker fork 必失败

## 路线图

| Phase | 状态 | 内容 |
|---|---|---|
| **Phase 1** | ✅ | 文件管理 + 联删 + 契约 #1 destructive action 协议 |
| **Phase 2** | ✅ | TMDB 元数据 + DeepSeek grounded select + 全库扫描 + 库视图 + NFO 写回 |
| **Phase 3** | ✅ | 重复 release 检测（quality scoring）+ Emby 观看进度 + 已看完归档候选；archive executor 仅 stub（不接 SSH 真执行） |
| Phase 4 | 🗓️ | MCP server（Claude Desktop / Code 直接管 NAS）+ 开源 onboarding |

详见 [ROADMAP.md](ROADMAP.md)。

## License

MIT — 见 [LICENSE](LICENSE)
