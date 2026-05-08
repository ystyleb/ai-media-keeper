"""NAS 文件管理器 - 通过 SSH 连接威联通 NAS 进行文件管理"""

import base64
import subprocess
import json
import os
import shlex
import logging
import sys
import xml.etree.ElementTree as ET
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, jsonify, request, abort

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 配置文件路径
CONFIG_DIR = Path(__file__).parent / "config"
CONFIG_DIR.mkdir(exist_ok=True)
QBIT_CONFIG_FILE = CONFIG_DIR / "qbit.json"

# NAS 配置（从环境变量读取）
NAS_HOST = os.environ.get("NAS_HOST", "192.168.1.100")
NAS_PORT = int(os.environ.get("NAS_PORT", "22"))
NAS_USER = os.environ.get("NAS_USER", "admin")
NAS_BASE_PATH = os.environ.get("NAS_BASE_PATH", "/share/CACHEDEV1_DATA")

# 简单的 API Token 认证（强制通过 NAS_API_TOKEN 环境变量配置）
API_TOKEN = os.environ.get("NAS_API_TOKEN", "").strip()
if not API_TOKEN:
    sys.stderr.write(
        "ERROR: NAS_API_TOKEN environment variable is required.\n"
        "Generate one with:\n"
        '  python3 -c "import secrets; print(secrets.token_hex(32))"\n'
        "Then export it before starting the server.\n"
    )
    sys.exit(1)
if len(API_TOKEN) < 16:
    sys.stderr.write("ERROR: NAS_API_TOKEN is too short (need ≥ 16 chars).\n")
    sys.exit(1)
logger.info(f"API Token loaded ({len(API_TOKEN)} chars).")

# qBittorrent 配置（默认值，可被运行时配置覆盖；密码不落盘）
DEFAULT_QBIT_CONFIG = {
    "url": os.environ.get("QBIT_URL", "http://192.168.1.100:8080"),
    "user": os.environ.get("QBIT_USER", "admin"),
    "password": os.environ.get("QBIT_PASS", ""),
}


class QBitClient:
    """qBittorrent WebAPI 客户端（支持配置文件持久化）"""

    def __init__(self):
        self.session = requests.Session()
        self._logged_in = False
        self._config: dict = {}
        self.load_config()

    def load_config(self):
        """加载配置：url / user 从文件读，password 只在内存（env var 或 UI 输入）。

        密码来源优先级：UI 主动设置（运行时） > 文件遗留的老明文（自动迁移） > QBIT_PASS env。
        密码永远不写盘——只能通过 QBIT_PASS env var 跨重启持久化。
        """
        self._config = {
            "url": DEFAULT_QBIT_CONFIG["url"],
            "user": DEFAULT_QBIT_CONFIG["user"],
            "password": os.environ.get("QBIT_PASS", ""),
        }

        legacy_plaintext_found = False
        if QBIT_CONFIG_FILE.exists():
            try:
                with open(QBIT_CONFIG_FILE, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self._config["url"] = saved.get("url", self._config["url"])
                self._config["user"] = saved.get("user", self._config["user"])
                # 老格式：明文 password 字段。加载到内存，下面立刻擦除文件里的它
                if saved.get("password"):
                    self._config["password"] = saved["password"]
                    legacy_plaintext_found = True
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"Failed to load qBit config: {e}")
        else:
            self.save_config()

        self._logged_in = False
        self.session.cookies.clear()

        if legacy_plaintext_found:
            logger.warning(
                "qBit config: migrated plaintext password from disk to memory only. "
                "Set QBIT_PASS env var to persist across restarts."
            )
            self.save_config()  # 立刻擦除文件里的 password 字段

    def save_config(self):
        """保存配置到文件——只写 url / user，password 永不落盘"""
        on_disk = {
            "url": self._config.get("url", ""),
            "user": self._config.get("user", ""),
        }
        QBIT_CONFIG_FILE.write_text(
            json.dumps(on_disk, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @property
    def url(self) -> str:
        return self._config["url"].rstrip("/")

    @property
    def user(self) -> str:
        return self._config["user"]

    def get_config(self) -> dict:
        """返回配置（密码脱敏）"""
        return {
            "url": self._config["url"],
            "user": self._config["user"],
            "has_password": bool(self._config.get("password")),
        }

    def update_config(self, url: str, user: str, password: str):
        """更新配置；password 为空表示保留现有内存里的密码（UI 改 url/user 时常见）"""
        # URL 校验：拒绝非 http(s) scheme 和 userinfo（防止 http://user:pass@host 把凭据落盘）
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("URL must use http or https scheme")
        if parsed.username or parsed.password:
            raise ValueError("URL must not contain userinfo (user:pass@host)")
        if not parsed.hostname:
            raise ValueError("URL must have a hostname")

        self._config["url"] = url
        self._config["user"] = user
        if password:
            self._config["password"] = password
        self.save_config()
        self._logged_in = False
        self.session.cookies.clear()

    def _ensure_login(self):
        if self._logged_in:
            return
        if not self._config.get("password"):
            raise RuntimeError("qBit password not set (use UI or QBIT_PASS env var)")
        resp = self.session.post(
            f"{self.url}/api/v2/auth/login",
            data={"username": self._config["user"], "password": self._config["password"]},
            headers={"Referer": self.url},
            timeout=10,
        )
        if resp.status_code != 200 or resp.text.strip() != "Ok.":
            raise RuntimeError(f"qBit login failed: {resp.status_code}")
        self._logged_in = True

    def test_connection(self) -> dict:
        """测试连接，返回状态"""
        try:
            self._ensure_login()
            # 获取种子数量验证连接正常
            torrents = self.get_torrents()
            return {"status": "ok", "torrent_count": len(torrents)}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_torrents(self) -> list[dict]:
        self._ensure_login()
        resp = self.session.get(f"{self.url}/api/v2/torrents/info", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def find_torrents_by_paths(self, paths: list[str]) -> list[dict]:
        """根据文件路径列表匹配种子（先 realpath 规范化，处理 QNAP symlink）"""
        torrents = self.get_torrents()
        if not torrents or not paths:
            return []

        # 收集两边所有路径，一次批量规范化
        to_resolve: set[str] = set(paths)
        for t in torrents:
            cp = t.get("content_path", "")
            sp = t.get("save_path", "")
            name = t.get("name", "")
            if cp:
                to_resolve.add(cp)
            if sp and name:
                to_resolve.add(os.path.join(sp, name))
        real = _resolve_real_paths(list(to_resolve))

        matched = []
        seen_hashes = set()
        for torrent in torrents:
            if torrent["hash"] in seen_hashes:
                continue
            content = torrent.get("content_path", "")
            save = torrent.get("save_path", "")
            name = torrent.get("name", "")
            torrent_dir = os.path.join(save, name) if save and name else ""
            content_real = real.get(content, content)
            dir_real = real.get(torrent_dir, torrent_dir)
            for p in paths:
                p_real = real.get(p, p)
                # 双向 ancestor-descendant 匹配，覆盖：
                #   - 用户选文件，文件就是 content_path（单文件种子）
                #   - 用户选文件，文件在 save_path/name 下（多文件种子）
                #   - 用户选目录，目录里有 content_path（qBit content_path 常指向种子内某个文件）
                if _path_overlaps(p_real, content_real) or _path_overlaps(p_real, dir_real):
                    matched.append(torrent)
                    seen_hashes.add(torrent["hash"])
                    break
        return matched

    def delete_torrents(self, hashes: list[str], delete_files: bool = True):
        self._ensure_login()
        # qBit WebAPI 标准是 POST（旧版接受 GET，但开源版本走标准）
        resp = self.session.post(
            f"{self.url}/api/v2/torrents/delete",
            data={
                "hashes": "|".join(hashes),
                "deleteFiles": "true" if delete_files else "false",
            },
            timeout=60,
        )
        resp.raise_for_status()


qbit = QBitClient()


def require_token(f):
    """API Token 认证装饰器"""
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token or token != API_TOKEN:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


def validate_path(path: str) -> str:
    """验证并规范化路径，确保在 NAS_BASE_PATH 下"""
    if not path:
        abort(400, description="Path is required")

    # 规范化路径
    try:
        normalized = os.path.normpath(path)
    except ValueError:
        abort(400, description="Invalid path")

    # 检查路径遍历
    if ".." in normalized.split(os.sep):
        abort(400, description="Path traversal not allowed")

    # 检查是否在允许的基础路径下（用边界检查防止 /base_evil/ 之类的前缀绕过）
    base = NAS_BASE_PATH.rstrip("/")
    if normalized != base and not normalized.startswith(base + "/"):
        abort(403, description="Access denied: path outside NAS base")

    # 检查危险字符（括号在文件名中是合法的，shlex.quote 会处理转义）
    dangerous_chars = [";", "&", "|", "$", "`", "\n", "\r"]
    for char in dangerous_chars:
        if char in normalized:
            abort(400, description=f"Invalid character in path: {char}")

    return normalized


SSH_CONTROL_PATH = "/tmp/nas-ssh-%r@%h:%p"


def ssh_exec(cmd: str, timeout: int = 30) -> tuple[int, str, str]:
    """执行 SSH 命令（ControlMaster 连接复用 + shlex 转义）"""
    ssh_cmd = [
        "ssh",
        "-p", str(NAS_PORT),
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={SSH_CONTROL_PATH}",
        "-o", "ControlPersist=300",
        f"{NAS_USER}@{NAS_HOST}",
        cmd
    ]
    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.returncode != 0:
            logger.error(f"SSH command failed: {cmd[:100]}... stderr: {result.stderr[:200]}")
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        logger.error(f"SSH command timed out: {cmd[:100]}...")
        return -1, "", "Command timed out"
    except Exception as e:
        logger.error(f"SSH command error: {e}")
        return -1, "", "Internal server error"


def human_size(size_bytes: int) -> str:
    """将字节数转换为人类可读格式"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config/app")
@require_token
def app_config():
    """应用级运行时配置（前端启动时需要的非敏感参数）"""
    return jsonify({"nas_base_path": NAS_BASE_PATH})


# ==================== 配置管理 API ====================


@app.route("/api/config/qbit")
@require_token
def get_qbit_config():
    """获取 qBittorrent 配置"""
    return jsonify(qbit.get_config())


@app.route("/api/config/qbit", methods=["POST"])
@require_token
def update_qbit_config():
    """更新 qBittorrent 配置"""
    data = request.json
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    url = data.get("url", "").strip()
    user = data.get("user", "").strip()
    password = data.get("password", "")

    if not url:
        return jsonify({"error": "URL is required"}), 400

    try:
        qbit.update_config(url, user, password)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"status": "saved"})


@app.route("/api/config/qbit/test", methods=["POST"])
@require_token
def test_qbit_connection():
    """测试 qBittorrent 连接"""
    result = qbit.test_connection()
    return jsonify(result)


@app.route("/api/disk")
@require_token
def disk_usage():
    """获取磁盘使用情况"""
    # QNAP 通常有 1-2 个存储池（CACHEDEV*_DATA），用 glob 兼容；其他 NAS 自行调整
    cmd = "df -h /share/CACHEDEV*_DATA 2>/dev/null"
    code, stdout, stderr = ssh_exec(cmd)
    if code != 0:
        return jsonify({"error": "Failed to get disk info"}), 500

    disks = []
    for line in stdout.strip().split("\n")[1:]:
        parts = line.split()
        if len(parts) >= 6:
            disks.append({
                "filesystem": parts[0],
                "size": parts[1],
                "used": parts[2],
                "available": parts[3],
                "use_percent": parts[4],
                "mount": parts[5]
            })
    return jsonify({"disks": disks})


@app.route("/api/files")
@require_token
def list_files():
    """列出目录内容"""
    path = request.args.get("path", NAS_BASE_PATH)

    # 安全验证
    validated_path = validate_path(path)

    # 获取文件列表（使用 shlex.quote 转义路径）
    safe_path = shlex.quote(validated_path)
    cmd = f"ls -la --time-style=long-iso {safe_path} 2>/dev/null | tail -n +2"
    code, stdout, stderr = ssh_exec(cmd)
    if code != 0:
        return jsonify({"error": "Failed to list directory"}), 500

    files = []
    need_inode_paths = []
    for line in stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue

        permissions = parts[0]
        hardlinks = int(parts[1])
        owner = parts[2]
        group = parts[3]
        size = int(parts[4])
        date = f"{parts[5]} {parts[6]}"
        name = parts[7]

        # 跳过 . 和 ..
        if name in (".", ".."):
            continue

        is_dir = permissions.startswith("d")
        full_path = f"{validated_path}/{name}".replace("//", "/")

        if hardlinks > 1 and not is_dir:
            need_inode_paths.append(full_path)

        files.append({
            "name": name,
            "path": full_path,
            "is_dir": is_dir,
            "size": size,
            "size_human": human_size(size) if not is_dir else "-",
            "modified": date,
            "permissions": permissions,
            "hardlinks": hardlinks,
            "inode": 0,
            "owner": owner,
            "group": group
        })

    # 批量获取 inode（一次 SSH 替代 N 次）
    if need_inode_paths:
        quoted_paths = " ".join(shlex.quote(p) for p in need_inode_paths)
        stat_cmd = f"stat -c '%i %n' {quoted_paths} 2>/dev/null"
        stat_code, stat_out, _ = ssh_exec(stat_cmd)
        if stat_code == 0:
            inode_map = {}
            for stat_line in stat_out.strip().split("\n"):
                if not stat_line:
                    continue
                # 格式: "inode path"（路径可能含空格，只 split 一次）
                idx = stat_line.find(" ")
                if idx > 0 and stat_line[:idx].isdigit():
                    inode_map[stat_line[idx + 1:]] = int(stat_line[:idx])
            for f in files:
                if f["path"] in inode_map:
                    f["inode"] = inode_map[f["path"]]

    # 排序：目录在前，然后按名称
    files.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

    return jsonify({
        "path": validated_path,
        "parent": os.path.dirname(validated_path) if validated_path != NAS_BASE_PATH else None,
        "files": files
    })


@app.route("/api/hardlinks")
@require_token
def find_hardlinks():
    """查找指定目录下的硬链接文件（只扫描当前目录，不递归）"""
    path = request.args.get("path", NAS_BASE_PATH)

    # 安全验证
    validated_path = validate_path(path)

    # 一次 SSH：find + stat 合一，拿到 inode/链接数/大小/路径
    safe_path = shlex.quote(validated_path)
    cmd = (
        f"find {safe_path} -maxdepth 1 -type f -links +1"
        f" -exec stat -c '%i %h %s %n' {{}} + 2>/dev/null"
    )
    code, stdout, stderr = ssh_exec(cmd, timeout=15)
    if code != 0 and not stdout.strip():
        return jsonify({"error": "Failed to scan hardlinks"}), 500

    # 解析 stat 输出，收集去重后的 inode
    entries = []
    inodes_to_find = set()
    for line in stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            inode = int(parts[0])
            links = int(parts[1])
            size = int(parts[2])
            filepath = parts[3]
        except (ValueError, IndexError):
            continue
        entries.append({
            "path": filepath,
            "inode": inode,
            "links": links,
            "size": size,
        })
        inodes_to_find.add(inode)

    # 一次 SSH：批量查找所有 inode 的关联路径
    inode_targets: dict[int, list[str]] = {ino: [] for ino in inodes_to_find}
    if inodes_to_find:
        inum_expr = " -o ".join(f"-inum {ino}" for ino in inodes_to_find)
        find_cmd = f"find {shlex.quote(NAS_BASE_PATH)} \\( {inum_expr} \\) -print 2>/dev/null"
        _, find_out, _ = ssh_exec(find_cmd, timeout=60)
        if find_out.strip():
            # 需要反查每个路径的 inode 来分组
            all_found = [p for p in find_out.strip().split("\n") if p]
            if all_found:
                quoted = " ".join(shlex.quote(p) for p in all_found)
                stat_cmd = f"stat -c '%i %n' {quoted} 2>/dev/null"
                _, stat_out, _ = ssh_exec(stat_cmd)
                for stat_line in stat_out.strip().split("\n"):
                    if not stat_line:
                        continue
                    idx = stat_line.find(" ")
                    if idx > 0 and stat_line[:idx].isdigit():
                        ino = int(stat_line[:idx])
                        found_path = stat_line[idx + 1:]
                        if ino in inode_targets:
                            inode_targets[ino].append(found_path)

    hardlinks = []
    for entry in entries:
        targets = [
            t for t in inode_targets.get(entry["inode"], [])
            if t != entry["path"]
        ]
        hardlinks.append({
            "path": entry["path"],
            "inode": entry["inode"],
            "hardlinks": entry["links"],
            "size": entry["size"],
            "size_human": human_size(entry["size"]),
            "targets": targets
        })

    return jsonify({"hardlinks": hardlinks})


def _ensure_real_path_under_base(path: str):
    """通过远端 readlink -f 解析路径 + base，防止 symlink 逃逸沙箱。
    用于读取 / 删除等需要保证 real path 仍在 NAS_BASE_PATH 真实路径下的场景。
    """
    real = _resolve_real_paths([path, NAS_BASE_PATH])
    real_target = real.get(path, path)
    real_base = real.get(NAS_BASE_PATH, NAS_BASE_PATH).rstrip("/")
    if real_target != real_base and not real_target.startswith(real_base + "/"):
        abort(403, description="Path resolves outside NAS base (symlink escape)")


def _read_remote_text(path: str, max_bytes: int = 256 * 1024) -> str | None:
    """读 NAS 上小型文本文件并按概率解码。读不到 / 解码失败 / 越界返回 None。"""
    # 沙箱校验：real path 必须在 NAS_BASE_PATH 真实路径下
    real = _resolve_real_paths([path, NAS_BASE_PATH])
    real_target = real.get(path, path)
    real_base = real.get(NAS_BASE_PATH, NAS_BASE_PATH).rstrip("/")
    if real_target != real_base and not real_target.startswith(real_base + "/"):
        return None
    safe = shlex.quote(path)
    code, stdout, _ = ssh_exec(f"head -c {max_bytes} {safe} 2>/dev/null | base64", timeout=15)
    if code != 0 or not stdout.strip():
        return None
    try:
        raw = base64.b64decode(stdout.strip())
    except Exception:
        return None
    for enc in ("utf-8-sig", "utf-8", "cp437", "cp866", "gb18030", "big5", "shift_jis"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _parse_emby_nfo(text: str) -> dict | None:
    """尝试把 Emby/Jellyfin .nfo (XML) 解析成结构化字段。识别失败返回 None。
    支持 <episodedetails> / <tvshow> / <movie> 三种根元素。
    """
    stripped = text.lstrip("﻿").strip()
    if not stripped.startswith("<"):
        return None
    try:
        root = ET.fromstring(stripped)
    except ET.ParseError:
        return None

    if root.tag not in ("episodedetails", "tvshow", "movie"):
        return None

    def get(tag: str) -> str:
        el = root.find(tag)
        return (el.text or "").strip() if el is not None and el.text else ""

    actors = []
    for actor in root.findall("actor")[:10]:
        name_el = actor.find("name")
        role_el = actor.find("role")
        name = (name_el.text or "").strip() if name_el is not None and name_el.text else ""
        role = (role_el.text or "").strip() if role_el is not None and role_el.text else ""
        if name:
            actors.append({"name": name, "role": role})

    genres = [(g.text or "").strip() for g in root.findall("genre") if g.text]
    studios = [(s.text or "").strip() for s in root.findall("studio") if s.text]
    directors = [(d.text or "").strip() for d in root.findall("director") if d.text]

    fields = {
        "type": root.tag,
        "title": get("title"),
        "original_title": get("originaltitle"),
        "plot": get("plot"),
        "outline": get("outline"),
        "year": get("year"),
        "aired": get("aired"),
        "premiered": get("premiered"),
        "season": get("season"),
        "episode": get("episode"),
        "runtime": get("runtime"),
        "rating": get("rating"),
        "imdb_id": get("imdbid") or get("uniqueid"),
        "tmdb_id": get("tmdbid"),
        "added": get("dateadded"),
        "studios": studios,
        "directors": directors,
        "genres": genres,
        "actors": actors,
    }
    # 去掉空值
    return {k: v for k, v in fields.items() if v}


# 文本类文件白名单（含 .nfo / 字幕 / 配置文件等）
TEXT_PREVIEW_EXTS = {
    ".nfo", ".txt", ".log", ".md", ".readme",
    ".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt",
    ".json", ".yml", ".yaml", ".ini", ".conf", ".cfg",
    ".sh", ".py", ".js", ".html", ".xml", ".csv",
}
MAX_PREVIEW_BYTES = 256 * 1024  # 256 KB


@app.route("/api/file-content")
@require_token
def file_content():
    """读取小型文本文件内容（用于预览 .nfo / .srt / .log 等）。
    NFO 常见编码：UTF-8 / CP437（西欧 ASCII art）/ CP866（俄）/ GBK（中）。
    后端按概率顺序尝试解码，挑第一个成功的返回。
    """
    path = request.args.get("path", "")
    validated_path = validate_path(path)
    _ensure_real_path_under_base(validated_path)

    ext = os.path.splitext(validated_path)[1].lower()
    if ext not in TEXT_PREVIEW_EXTS:
        return jsonify({"error": f"Unsupported extension for preview: {ext}"}), 400

    # 先 stat 看大小；同时用 head -c | base64 读前 N 字节（一次 SSH）
    safe = shlex.quote(validated_path)
    cmd = (
        f"stat -c '%s' {safe} 2>/dev/null && "
        f"head -c {MAX_PREVIEW_BYTES} {safe} | base64"
    )
    code, stdout, stderr = ssh_exec(cmd, timeout=20)
    if code != 0:
        return jsonify({"error": "Read failed", "detail": stderr[:200]}), 500

    # stdout 第一行是文件大小，剩下是 base64
    parts = stdout.split("\n", 1)
    if len(parts) < 2:
        return jsonify({"error": "Unexpected stat output"}), 500
    try:
        file_size = int(parts[0].strip())
    except ValueError:
        return jsonify({"error": "Stat parse failed"}), 500

    try:
        raw = base64.b64decode(parts[1].strip())
    except Exception as e:
        return jsonify({"error": f"Base64 decode failed: {e}"}), 500

    # 按概率顺序尝试解码
    text = None
    encoding_used = None
    for enc in ("utf-8-sig", "utf-8", "cp437", "cp866", "gb18030", "big5", "shift_jis"):
        try:
            text = raw.decode(enc)
            encoding_used = enc
            break
        except UnicodeDecodeError:
            continue

    if text is None:
        text = raw.decode("latin-1", errors="replace")
        encoding_used = "latin-1 (fallback)"

    # 对 .nfo 尝试结构化解析（Emby/Jellyfin XML 元数据）
    parsed = _parse_emby_nfo(text) if ext == ".nfo" else None

    # episode 级 nfo 的 <title> 只是集标题，剧名要从同目录的 tvshow.nfo 拿
    if parsed and parsed.get("type") == "episodedetails":
        show_dir = os.path.dirname(validated_path)
        tvshow_text = _read_remote_text(os.path.join(show_dir, "tvshow.nfo"))
        if tvshow_text:
            tv = _parse_emby_nfo(tvshow_text)
            if tv:
                if tv.get("title"):
                    parsed["show_title"] = tv["title"]
                if tv.get("original_title"):
                    parsed["show_original_title"] = tv["original_title"]
                if tv.get("year") and not parsed.get("year"):
                    parsed["year"] = tv["year"]

    return jsonify({
        "text": text,
        "encoding": encoding_used,
        "size": file_size,
        "preview_bytes": min(MAX_PREVIEW_BYTES, file_size),
        "truncated": file_size > MAX_PREVIEW_BYTES,
        "parsed": parsed,
    })


@app.route("/api/inode/<int:inode>")
@require_token
def find_by_inode(inode: int):
    """根据 inode 查找所有硬链接"""
    if inode <= 0:
        return jsonify({"error": "Invalid inode"}), 400

    cmd = f"find {shlex.quote(NAS_BASE_PATH)} -inum {inode} 2>/dev/null"
    code, stdout, stderr = ssh_exec(cmd)
    if code != 0:
        return jsonify({"error": "Failed to find by inode"}), 500

    paths = [p for p in stdout.strip().split("\n") if p]
    return jsonify({"inode": inode, "paths": paths})


@app.route("/api/delete", methods=["POST"])
@require_token
def delete_files():
    """删除文件（支持批量，2 次 SSH 完成）"""
    data = request.json
    if not data or not isinstance(data.get("files"), list):
        return jsonify({"error": "Invalid request: files array required"}), 400

    files = data.get("files", [])
    if not files:
        return jsonify({"error": "No files specified"}), 400

    # 安全验证所有路径
    validated_files = []
    for f in files:
        try:
            validated_files.append(validate_path(f))
        except Exception as e:
            return jsonify({"error": f"Invalid path: {f}"}), 400

    _reject_base_path(validated_files)
    logger.info(f"[delete] request: {len(validated_files)} file(s): {validated_files}")

    # 批量 stat（一次 SSH）
    quoted_paths = " ".join(shlex.quote(p) for p in validated_files)
    stat_cmd = f"stat -c '%h %i %s %n' {quoted_paths} 2>/dev/null"
    code, stdout, _ = ssh_exec(stat_cmd)

    file_info: dict[str, dict] = {}
    if code == 0:
        for line in stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            try:
                file_info[parts[3]] = {
                    "links": int(parts[0]),
                    "inode": int(parts[1]),
                    "size": int(parts[2]),
                }
            except (ValueError, IndexError):
                continue

    # 批量删除（一次 SSH，shell 循环逐个删并输出结果）
    rm_parts = []
    for filepath in validated_files:
        safe = shlex.quote(filepath)
        rm_parts.append(f'rm {safe} && echo "OK {safe}" || echo "FAIL {safe}"')
    rm_cmd = " ; ".join(rm_parts)
    _, rm_out, _ = ssh_exec(rm_cmd)

    rm_results: dict[str, bool] = {}
    for line in rm_out.strip().split("\n"):
        if line.startswith("OK "):
            rm_results[line[3:].strip("'")] = True
        elif line.startswith("FAIL "):
            rm_results[line[5:].strip("'")] = False

    results = []
    for filepath in validated_files:
        info = file_info.get(filepath)
        if not info:
            results.append({"path": filepath, "status": "error", "message": "File not found"})
            continue

        if rm_results.get(filepath):
            remaining = info["links"] - 1
            results.append({
                "path": filepath,
                "status": "deleted",
                "inode": info["inode"],
                "remaining_links": remaining,
                "space_freed": remaining == 0,
                "size": info["size"]
            })
            logger.info(f"Deleted: {filepath} (inode: {info['inode']}, remaining links: {remaining})")
        else:
            results.append({"path": filepath, "status": "error", "message": "Delete failed"})

    return jsonify({"results": results})


def _stat_path_types(paths: list[str]) -> dict[str, str]:
    """批量获取路径类型，返回 {path: 'directory'|'regular file'|...|'missing'}"""
    if not paths:
        return {}
    quoted = " ".join(shlex.quote(p) for p in paths)
    code, stdout, _ = ssh_exec(f"stat -c '%F|%n' {quoted} 2>/dev/null")
    types: dict[str, str] = {p: "missing" for p in paths}
    if code != 0 and not stdout.strip():
        return types
    for line in stdout.strip().split("\n"):
        if not line or "|" not in line:
            continue
        type_str, path = line.split("|", 1)
        if path in types:
            types[path] = type_str
    return types


def _get_real_sizes(paths: list[str]) -> dict[str, int]:
    """批量获取真实占用（du -sb 对文件返回大小、对目录递归计算）"""
    if not paths:
        return {}
    quoted = " ".join(shlex.quote(p) for p in paths)
    # 大目录可能耗时，给 120s
    code, stdout, _ = ssh_exec(f"du -sb {quoted} 2>/dev/null", timeout=120)
    sizes: dict[str, int] = {p: 0 for p in paths}
    if code != 0 and not stdout.strip():
        return sizes
    for line in stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            sizes[parts[1]] = int(parts[0])
        except ValueError:
            continue
    return sizes


def _build_rm_command(path: str, is_dir: bool) -> str:
    """构建删除单个路径的 shell 命令（目录用 rm -rf，文件用 rm）"""
    safe = shlex.quote(path)
    return f"rm -rf {safe}" if is_dir else f"rm {safe}"


def _path_overlaps(a: str, b: str) -> bool:
    """两个路径是否有 ancestor-descendant 关系或相等（双向 startswith）"""
    if not a or not b:
        return False
    if a == b:
        return True
    a_pref = a.rstrip("/") + "/"
    b_pref = b.rstrip("/") + "/"
    return a.startswith(b_pref) or b.startswith(a_pref)


def _resolve_real_paths(paths: list[str]) -> dict[str, str]:
    """批量 realpath：把含 symlink 的路径解析成真实路径。
    QNAP 在 /share/ 下给每个 share folder 建了 symlink 指向 /share/CACHEDEV*_DATA/<name>，
    qBit 的 save_path / content_path 通常用 symlink 路径，文件浏览器看到的是真实路径。
    比较前必须两边都规范化才能正确匹配。
    """
    if not paths:
        return {}
    quoted = " ".join(shlex.quote(p) for p in paths)
    # QNAP busybox 没有 realpath，用 readlink -f；不存在的路径 fallback 到原路径
    script = (
        f"set -- {quoted}; "
        f'for p; do r=$(readlink -f "$p" 2>/dev/null); printf "%s|%s\\n" "$p" "${{r:-$p}}"; done'
    )
    code, stdout, _ = ssh_exec(script)
    result: dict[str, str] = {p: p for p in paths}
    if code == 0 and stdout.strip():
        for line in stdout.strip().split("\n"):
            if "|" not in line:
                continue
            orig, resolved = line.split("|", 1)
            if orig in result and resolved.strip():
                result[orig] = resolved.strip()
    return result


def _reject_base_path(paths: list[str]):
    """删除前的额外护栏：拒绝把 NAS_BASE_PATH 本身作为删除目标"""
    for p in paths:
        if p.rstrip("/") == NAS_BASE_PATH.rstrip("/"):
            abort(400, description="Cannot delete NAS base path itself")


def _resolve_all_hardlink_paths(file_paths: list[str]) -> dict:
    """查找文件的所有硬链接路径，返回 {原始路径: {inode, size, all_paths}}"""
    if not file_paths:
        return {}

    # 批量 stat 获取 inode 和大小
    quoted = " ".join(shlex.quote(p) for p in file_paths)
    stat_cmd = f"stat -c '%i %s %n' {quoted} 2>/dev/null"
    code, stdout, _ = ssh_exec(stat_cmd)
    if code != 0:
        return {}

    path_info: dict[str, dict] = {}
    inodes_to_find: set[int] = set()
    for line in stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            inode = int(parts[0])
            size = int(parts[1])
            filepath = parts[2]
        except (ValueError, IndexError):
            continue
        path_info[filepath] = {"inode": inode, "size": size, "all_paths": []}
        inodes_to_find.add(inode)

    # 批量 find 所有 inode 的关联路径
    if inodes_to_find:
        inum_expr = " -o ".join(f"-inum {ino}" for ino in inodes_to_find)
        find_cmd = f"find {shlex.quote(NAS_BASE_PATH)} \\( {inum_expr} \\) -print 2>/dev/null"
        _, find_out, _ = ssh_exec(find_cmd, timeout=60)
        if find_out.strip():
            all_found = [p for p in find_out.strip().split("\n") if p]
            if all_found:
                fq = " ".join(shlex.quote(p) for p in all_found)
                _, stat_out, _ = ssh_exec(f"stat -c '%i %n' {fq} 2>/dev/null")
                inode_to_paths: dict[int, list[str]] = {}
                for stat_line in stat_out.strip().split("\n"):
                    if not stat_line:
                        continue
                    idx = stat_line.find(" ")
                    if idx > 0 and stat_line[:idx].isdigit():
                        ino = int(stat_line[:idx])
                        inode_to_paths.setdefault(ino, []).append(stat_line[idx + 1:])
                for info in path_info.values():
                    info["all_paths"] = inode_to_paths.get(info["inode"], [])

    return path_info


@app.route("/api/delete-preview", methods=["POST"])
@require_token
def delete_preview():
    """预览联删：返回硬链接路径 + 关联 PT 种子（不执行删除）"""
    data = request.json
    if not data or not isinstance(data.get("files"), list):
        return jsonify({"error": "Invalid request: files array required"}), 400

    files = data["files"]
    validated_files = []
    for f in files:
        try:
            validated_files.append(validate_path(f))
        except Exception:
            return jsonify({"error": f"Invalid path: {f}"}), 400

    _reject_base_path(validated_files)

    # 区分文件/目录 + 真实大小（目录递归计算）
    types = _stat_path_types(validated_files)
    real_sizes = _get_real_sizes(validated_files)

    # 查找所有硬链接路径
    path_info = _resolve_all_hardlink_paths(validated_files)

    # 收集所有路径用于匹配 PT 种子
    all_paths = set()
    for info in path_info.values():
        all_paths.update(info["all_paths"])
    for f in validated_files:
        all_paths.add(f)

    # 匹配 qBittorrent 种子
    torrents = []
    try:
        matched = qbit.find_torrents_by_paths(list(all_paths))
        for t in matched:
            torrents.append({
                "hash": t["hash"],
                "name": t["name"],
                "size": t.get("total_size", 0),
                "size_human": human_size(t.get("total_size", 0)),
                "save_path": t.get("save_path", ""),
                "content_path": t.get("content_path", ""),
                "state": t.get("state", ""),
                "progress": t.get("progress", 0),
            })
    except Exception as e:
        logger.error(f"qBit lookup failed: {e}")

    # 收集所有要删除的路径（用户选 + 硬链接），构建命令清单
    all_to_delete = set(validated_files)
    for info in path_info.values():
        all_to_delete.update(info["all_paths"])

    # 硬链接路径可能不在 types 里，补一次
    extra_paths = [p for p in all_to_delete if p not in types]
    if extra_paths:
        types.update(_stat_path_types(extra_paths))

    commands = []
    for p in sorted(all_to_delete):
        is_dir = types.get(p) == "directory"
        commands.append(_build_rm_command(p, is_dir))

    # 构建预览结果（用真实大小）
    preview_files = []
    for filepath in validated_files:
        info = path_info.get(filepath, {})
        other_links = [p for p in info.get("all_paths", []) if p != filepath]
        is_dir = types.get(filepath) == "directory"
        real_size = real_sizes.get(filepath, info.get("size", 0))
        preview_files.append({
            "path": filepath,
            "is_dir": is_dir,
            "inode": info.get("inode", 0),
            "size": real_size,
            "size_human": human_size(real_size),
            "hardlink_paths": other_links,
        })

    total_real_size = sum(real_sizes.get(p, 0) for p in validated_files)
    return jsonify({
        "files": preview_files,
        "torrents": torrents,
        "total_hardlinks": sum(len(f["hardlink_paths"]) for f in preview_files),
        "commands": commands,
        "total_size": total_real_size,
        "total_size_human": human_size(total_real_size),
    })


@app.route("/api/delete-complete", methods=["POST"])
@require_token
def delete_complete():
    """联删：硬链接 + PT 种子 + 文件，一次全部清理"""
    data = request.json
    if not data or not isinstance(data.get("files"), list):
        return jsonify({"error": "Invalid request: files array required"}), 400

    files = data["files"]
    delete_torrents = data.get("delete_torrents", True)

    validated_files = []
    for f in files:
        try:
            validated_files.append(validate_path(f))
        except Exception:
            return jsonify({"error": f"Invalid path: {f}"}), 400

    _reject_base_path(validated_files)
    logger.info(
        f"[delete-complete] request: {len(validated_files)} file(s), "
        f"delete_torrents={delete_torrents}: {validated_files}"
    )

    # 查找所有硬链接路径
    path_info = _resolve_all_hardlink_paths(validated_files)

    # 收集所有需要删除的文件路径（用户选的 + 硬链接）
    all_paths_to_delete = set(validated_files)
    for info in path_info.values():
        all_paths_to_delete.update(info["all_paths"])

    # 1) 删除 PT 种子（先删种子，因为 deleteFiles=true 会删源文件）
    torrent_results = []
    qbit_deleted_paths = set()
    if delete_torrents:
        try:
            all_paths_list = list(all_paths_to_delete)
            matched = qbit.find_torrents_by_paths(all_paths_list)
            if matched:
                hashes = [t["hash"] for t in matched]
                qbit.delete_torrents(hashes, delete_files=True)
                for t in matched:
                    torrent_results.append({
                        "hash": t["hash"],
                        "name": t["name"],
                        "status": "deleted",
                    })
                    # 记录种子管理的路径（qBit 已经删了这些文件）
                    cp = t.get("content_path", "")
                    if cp:
                        qbit_deleted_paths.add(cp)
                logger.info(f"Deleted {len(matched)} torrents: {[t['name'] for t in matched]}")
        except Exception as e:
            logger.error(f"qBit delete failed: {e}")
            torrent_results.append({"status": "error", "message": str(e)})

    # 2) 删除剩余的硬链接文件（qBit 可能已删了源文件，这里删剩下的链接）
    remaining_to_delete = [
        p for p in all_paths_to_delete
        if p not in qbit_deleted_paths
    ]

    file_results = []
    if remaining_to_delete:
        # 先检查哪些文件还存在（qBit 可能已经删了一部分）
        check_parts = []
        for p in remaining_to_delete:
            safe = shlex.quote(p)
            check_parts.append(f'[ -e {safe} ] && echo "EXISTS {safe}" || echo "GONE {safe}"')
        check_cmd = " ; ".join(check_parts)
        _, check_out, _ = ssh_exec(check_cmd)

        still_exists = []
        for line in check_out.strip().split("\n"):
            if line.startswith("EXISTS "):
                still_exists.append(line[7:].strip("'"))

        if still_exists:
            # 区分文件/目录：目录用 rm -rf，文件用 rm
            existing_types = _stat_path_types(still_exists)
            rm_parts = []
            for filepath in still_exists:
                is_dir = existing_types.get(filepath) == "directory"
                rm_inner = _build_rm_command(filepath, is_dir)
                safe = shlex.quote(filepath)
                rm_parts.append(f'{rm_inner} && echo "OK {safe}" || echo "FAIL {safe}"')
            rm_cmd = " ; ".join(rm_parts)
            logger.info(
                f"[delete-complete] executing: "
                f"{[_build_rm_command(p, existing_types.get(p) == 'directory') for p in still_exists]}"
            )
            # 大目录可能耗时（unlink 大量文件），延长 timeout
            _, rm_out, _ = ssh_exec(rm_cmd, timeout=300)

            for line in rm_out.strip().split("\n"):
                if line.startswith("OK "):
                    path = line[3:].strip("'")
                    file_results.append({"path": path, "status": "deleted"})
                    logger.info(f"Deleted hardlink: {path}")
                elif line.startswith("FAIL "):
                    path = line[5:].strip("'")
                    file_results.append({"path": path, "status": "error", "message": "Delete failed"})

        # 已被 qBit 删除的
        for p in remaining_to_delete:
            if p not in still_exists:
                file_results.append({"path": p, "status": "already_gone"})

    # 被 qBit deleteFiles 删除的源文件
    for p in qbit_deleted_paths:
        file_results.append({"path": p, "status": "deleted_by_qbit"})

    # 汇总释放空间（用真实大小：目录递归算，文件 stat 大小；同 inode 去重避免硬链接重复算）
    real_sizes = _get_real_sizes(validated_files)
    freed_inodes = set()
    total_freed = 0
    for path in validated_files:
        info = path_info.get(path, {})
        inode = info.get("inode")
        if inode and inode in freed_inodes:
            continue
        if inode:
            freed_inodes.add(inode)
        total_freed += real_sizes.get(path, info.get("size", 0))

    status_counts: dict[str, int] = {}
    for r in file_results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    torrents_removed = sum(1 for r in torrent_results if r.get("status") == "deleted")
    logger.info(
        f"[delete-complete] done: file_status={status_counts}, "
        f"torrents_removed={torrents_removed}, space_freed={human_size(total_freed)}"
    )

    return jsonify({
        "file_results": file_results,
        "torrent_results": torrent_results,
        "total_files_deleted": sum(1 for r in file_results if r["status"] in ("deleted", "deleted_by_qbit", "already_gone")),
        "total_torrents_deleted": sum(1 for r in torrent_results if r.get("status") == "deleted"),
        "space_freed": total_freed,
        "space_freed_human": human_size(total_freed),
    })


if __name__ == "__main__":
    # 默认绑定 127.0.0.1，生产环境应使用 gunicorn
    app.run(host="127.0.0.1", port=8080, debug=False)
