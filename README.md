# AI Media Keeper

AI 原生的影视资源管理器（v0）。当前是 Web 版的 NAS 文件管理器，专为 **QNAP / Synology / 任意 Linux NAS** 设计，针对 **PT 玩家做硬链接 + qBittorrent 种子的联动删除**优化。

> 路线图：把 NFO 元数据 + 文件浏览 + 联删能力作为基础设施，让 AI agent 能"住进"NAS 自动整理影视资源（识别重复、清理低质 release、按观看进度归档等）。当前版本是地基，AI 编排能力将逐步加入。

> 解决的核心痛点：用 SMB / Samba 删一部已经在 qBittorrent 做种的剧时，种子会变成 errored 状态扣保种率；手动到 qBit 里再删一遍又麻烦。这个工具一次操作把硬盘文件 + 关联硬链接 + qBit 种子（含下载文件）一起干掉，磁盘空间真正释放。

## 核心功能

- 📁 **文件浏览**：通过 SSH 列目录、查看磁盘用量
- 🔗 **硬链接探测**：自动识别硬链接 + 反查同 inode 的所有路径
- 🎬 **NFO 元数据解析**：识别 Emby/Jellyfin 的 .nfo（XML）→ 卡片化展示剧名 / 季集 / 剧情 / 演员；自动从同目录 `tvshow.nfo` 拉**中文剧名**
- 🗑️ **联删**：选中目录 / 文件 → 预览**真实占用** + **关联硬链接** + **qBit 种子** + **将执行的 SSH 命令** → 一键执行
- 🔒 **路径沙箱**：所有操作限制在 `NAS_BASE_PATH` 下，shell 元字符拦截 + `shlex.quote` 转义
- 🪪 **API Token 认证**：所有 API 都需 Bearer token

## 工作原理

1. Flask 后端通过 SSH（含 `ControlMaster` 连接复用）执行 NAS 上的命令
2. 路径匹配做 `readlink -f` 规范化（处理 QNAP `/share/<name>` → `/share/CACHEDEV*_DATA/<name>` symlink）
3. qBittorrent 通过 WebUI API 联动（登录复用 cookie）

## 截图

> （部署后自己截图替换这里）

## 快速开始

### 1. 准备 NAS 端

确保你能从开发机 SSH 免密登录 NAS：

```bash
# 把你的公钥推到 NAS（替换实际 IP / 端口 / 用户）
ssh-copy-id -p 22 admin@192.168.1.100
ssh -p 22 admin@192.168.1.100 'echo OK'   # 应输出 OK 不要密码
```

确保 qBittorrent 的 WebUI 已启用并能从开发机访问。

### 2. 安装依赖

```bash
git clone https://github.com/ystyleb/ai-media-keeper.git
cd ai-media-keeper
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入你的 NAS_HOST / NAS_PORT / NAS_BASE_PATH / NAS_API_TOKEN
# 启动前 source 进当前 shell（注意：app.py 不会自动读 .env）
set -a; source .env; set +a
```

或者直接 export：

```bash
export NAS_HOST=192.168.1.100
export NAS_PORT=22
export NAS_USER=admin
export NAS_BASE_PATH=/share/CACHEDEV1_DATA
export NAS_API_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

### 4. 启动

```bash
python3 app.py
# 浏览器打开 http://127.0.0.1:8080
# 第一次会弹窗输入 API Token（即上面的 NAS_API_TOKEN）
```

启动时如果 `NAS_API_TOKEN` 没设，应用会**直接退出**并打印生成命令——这是有意的，避免随机 token 进日志。设好之后启动会显示：
```
INFO:__main__:API Token loaded (64 chars).
```

## 配置参考

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `NAS_HOST` | `192.168.1.100` | NAS 内网 IP |
| `NAS_PORT` | `22` | SSH 端口 |
| `NAS_USER` | `admin` | SSH 用户 |
| `NAS_BASE_PATH` | `/share/CACHEDEV1_DATA` | 受控根路径，所有操作限制在此目录下 |
| `NAS_API_TOKEN` | **必填** | API 鉴权 token（≥16 字符），未设则启动退出 |
| `QBIT_URL` | `http://192.168.1.100:8080` | qBit WebUI 地址（UI 里也可改） |
| `QBIT_USER` | `admin` | qBit 用户名 |
| `QBIT_PASS` | 空 | qBit 密码（推荐通过此 env 持久化，否则需 UI 输入） |

### 关于 qBit 密码的安全策略

**密码永远不会写到磁盘上**。

- `config/qbit.json` 只存 `url` 和 `user`
- 密码来源优先级：UI 里主动设置（运行时） → 文件里残留的老明文（**自动迁移到内存 + 立即从文件擦除** + log warning） → `QBIT_PASS` env var
- 想跨重启持久化密码 → 设 `QBIT_PASS` 环境变量；不设则每次重启需要 UI 重新输入

这样设计的原因：加密落盘需要 key，key 也要存某处；key 泄露就跟明文一样。**最强保证是不存** —— 攻击者拿到 `config/qbit.json` 也只看到 url+user。`config/` 目录本身也已加 `.gitignore`。

## API

所有路由都需要 `Authorization: Bearer <NAS_API_TOKEN>`：

| 路由 | 说明 |
|---|---|
| `GET /api/disk` | NAS 磁盘用量 |
| `GET /api/files?path=...` | 列目录 |
| `GET /api/hardlinks?path=...` | 当前目录硬链接扫描（不递归） |
| `GET /api/inode/<inode>` | 反查同 inode 的所有路径 |
| `GET /api/file-content?path=...` | 读取小型文本文件（含 .nfo 结构化解析） |
| `POST /api/delete` | 纯文件删除（不动种子） |
| `POST /api/delete-preview` | 联删预览（硬链接 + qBit 种子 + 真实大小 + 命令清单） |
| `POST /api/delete-complete` | 联删执行 |
| `GET/POST /api/config/qbit[/test]` | qBit 配置管理 |

## 安全注意

- `validate_path` 强制路径在 `NAS_BASE_PATH` 之内 + 拒绝路径遍历 + 拦截 shell 元字符
- `_reject_base_path` 防止 `rm -rf` 整个根目录
- 默认绑定 `127.0.0.1:8080`，**不要直接暴露到公网**
- 如需远程访问：放到 Tailscale / WireGuard / 反向代理 + auth 后面，不要简单 NAT 转发

## 开发

```bash
# 推荐用 venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 app.py
```

生产部署用 `gunicorn` 而非 `flask run`：

```bash
pip install gunicorn
gunicorn -b 127.0.0.1:8080 -w 2 app:app
```

## License

MIT — 见 [LICENSE](LICENSE)
