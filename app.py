"""NAS 文件管理器 - 通过 SSH 连接威联通 NAS 进行文件管理"""

import base64
import subprocess
import json
import os
import shlex
import logging
import sys
import time
import xml.etree.ElementTree as ET
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import requests
from flask import Flask, render_template, jsonify, request, abort, g

from services import destructive_action

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 配置文件路径
CONFIG_DIR = Path(__file__).parent / "config"
CONFIG_DIR.mkdir(exist_ok=True)
QBIT_CONFIG_FILE = CONFIG_DIR / "qbit.json"
NAS_CONFIG_FILE = CONFIG_DIR / "nas.json"

# NAS 配置（env 提供初始默认；可被 nas.json 运行时覆盖）
NAS_HOST = os.environ.get("NAS_HOST", "192.168.1.100")
NAS_PORT = int(os.environ.get("NAS_PORT", "22"))
NAS_USER = os.environ.get("NAS_USER", "admin")
NAS_BASE_PATH = os.environ.get("NAS_BASE_PATH", "/share/CACHEDEV1_DATA")
NAS_DISK_PATTERN = os.environ.get("NAS_DISK_PATTERN", "").strip()


def _validate_glob_pattern(p: str) -> str:
    """允许 / * ? [ ] 等通配符，但禁止 shell metachars 防注入"""
    if not p:
        return ""
    if any(c in p for c in ";|&`$()<>\"'\\\n\r\t"):
        raise ValueError("Invalid characters in pattern")
    return p


def load_nas_config():
    """如配置文件存在则覆盖 env 默认值"""
    global NAS_HOST, NAS_PORT, NAS_USER, NAS_BASE_PATH, NAS_DISK_PATTERN
    if not NAS_CONFIG_FILE.exists():
        return
    try:
        cfg = json.loads(NAS_CONFIG_FILE.read_text(encoding="utf-8"))
        NAS_HOST = (cfg.get("host") or NAS_HOST).strip()
        NAS_PORT = int(cfg.get("port") or NAS_PORT)
        NAS_USER = (cfg.get("user") or NAS_USER).strip()
        bp = (cfg.get("base_path") or NAS_BASE_PATH).strip().rstrip("/") or NAS_BASE_PATH
        NAS_BASE_PATH = bp
        NAS_DISK_PATTERN = _validate_glob_pattern((cfg.get("disk_pattern") or "").strip())
        logger.info(f"NAS config loaded: {NAS_USER}@{NAS_HOST}:{NAS_PORT} base={NAS_BASE_PATH}")
    except Exception as e:
        logger.error(f"Failed to load NAS config: {e}")


def save_nas_config(host: str, port: int, user: str, base_path: str, disk_pattern: str = ""):
    """保存到 nas.json 并热加载"""
    cfg = {
        "host": host.strip(),
        "port": int(port),
        "user": user.strip(),
        "base_path": base_path.strip().rstrip("/"),
        "disk_pattern": _validate_glob_pattern(disk_pattern.strip()),
    }
    NAS_CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    load_nas_config()


load_nas_config()

# API Token 加载策略：env > config 文件 > 首次启动自动生成 + 落盘
#
# 设计目标：零 env 启动可行，但保留 env 作为 override（CI / 多机部署）。
# 文件路径走 config/ 目录（已 .gitignore，跟 qBit config 同级），首次写入 chmod 600。
import secrets as _secrets

API_TOKEN_FILE = CONFIG_DIR / ".api_token"


def _load_api_token() -> str:
    env_token = os.environ.get("NAS_API_TOKEN", "").strip()
    if env_token:
        if len(env_token) < 16:
            sys.stderr.write("ERROR: NAS_API_TOKEN is too short (need ≥ 16 chars).\n")
            sys.exit(1)
        return env_token
    if API_TOKEN_FILE.exists():
        token = API_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(token) < 16:
            sys.stderr.write(
                f"ERROR: {API_TOKEN_FILE} has a token shorter than 16 chars. "
                "Delete it to regenerate.\n"
            )
            sys.exit(1)
        return token
    # 首次启动：自动生成并 chmod 600
    new_token = _secrets.token_hex(32)
    API_TOKEN_FILE.write_text(new_token, encoding="utf-8")
    try:
        os.chmod(API_TOKEN_FILE, 0o600)
    except OSError:
        pass
    sys.stderr.write(
        "\n" + "=" * 60 + "\n"
        f"  Generated new API token → {API_TOKEN_FILE}\n"
        f"  Token (copy into UI on first visit):\n\n"
        f"    {new_token}\n\n"
        f"  Persists across restarts. Delete the file to rotate.\n"
        + "=" * 60 + "\n\n"
    )
    return new_token


API_TOKEN = _load_api_token()
logger.info(f"API Token loaded ({len(API_TOKEN)} chars).")

# 契约 #1: server_secret 加载 + SQLite schema 初始化
# WEB_CONCURRENCY 是 gunicorn 约定 env；未设视为单 worker（dev / flask run）
WORKER_COUNT = int(os.environ.get("WEB_CONCURRENCY", "1"))
SIGNING_KEY_FILE = CONFIG_DIR / ".signing_key"
try:
    SERVER_SECRET = destructive_action.load_server_secret(
        worker_count=WORKER_COUNT,
        config_path=SIGNING_KEY_FILE,
    )
except destructive_action.ServerSecretMisconfigured as e:
    sys.stderr.write(f"ERROR: {e}\n")
    sys.exit(1)

DB_PATH = CONFIG_DIR / "actions.db"
SCHEMA_PATH = Path(__file__).parent / "db" / "schema.sql"
_init_conn = destructive_action.open_connection(DB_PATH)
try:
    destructive_action.init_schema(_init_conn, SCHEMA_PATH)
    logger.info(f"Destructive action DB ready at {DB_PATH}")
finally:
    _init_conn.close()


def get_db():
    """Flask 请求级 SQLite 连接。每请求开一个，teardown 时关闭。"""
    if "db" not in g:
        g.db = destructive_action.open_connection(DB_PATH)
    return g.db


@app.teardown_appcontext
def _close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


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
        """用已保存的配置测试连接"""
        try:
            self._ensure_login()
            torrents = self.get_torrents()
            return {"status": "ok", "torrent_count": len(torrents)}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @staticmethod
    def test_connection_with(url: str, user: str, password: str) -> dict:
        """用临时凭据测试，不修改任何已保存状态。
        没有传 password 时退回到 client 的当前内存密码（用于"测试我已经保存的"）。
        """
        url = (url or "").strip().rstrip("/")
        if not url:
            return {"status": "error", "message": "URL is required"}
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return {"status": "error", "message": "URL must use http or https"}
        if parsed.username or parsed.password:
            return {"status": "error", "message": "URL must not contain userinfo"}
        if not password:
            return {"status": "error", "message": "Password is required"}

        session = requests.Session()
        try:
            r = session.post(
                f"{url}/api/v2/auth/login",
                data={"username": user, "password": password},
                headers={"Referer": url},
                timeout=10,
            )
            if r.status_code != 200 or r.text.strip() != "Ok.":
                return {"status": "error", "message": f"登录失败: HTTP {r.status_code}"}
            t = session.get(f"{url}/api/v2/torrents/info", timeout=20)
            t.raise_for_status()
            return {"status": "ok", "torrent_count": len(t.json())}
        except requests.RequestException as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": str(e)}
        finally:
            session.close()

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
    """测试 qBittorrent 连接。
    body 含 url/user/password 时用提交值临时测试（不写盘）；
    body 为空时用已保存的配置（密码取内存里的值）。
    """
    data = request.json or {}
    url = (data.get("url") or "").strip()
    user = (data.get("user") or "").strip()
    password = data.get("password") or ""

    # 没传任何字段 → 测已保存配置
    if not url and not user and not password:
        return jsonify(qbit.test_connection())

    # 传了字段 → 用提交值测试，缺失项回落到当前配置
    cur = qbit.get_config()
    final_url = url or cur.get("url", "")
    final_user = user or cur.get("user", "")
    # password 是 secret：前端可能留空表示"用现存密码"
    final_password = password or qbit._config.get("password", "")
    return jsonify(QBitClient.test_connection_with(final_url, final_user, final_password))


@app.route("/api/config/nas")
@require_token
def get_nas_config():
    """获取 NAS 连接配置（不含敏感信息）"""
    return jsonify({
        "host": NAS_HOST,
        "port": NAS_PORT,
        "user": NAS_USER,
        "base_path": NAS_BASE_PATH,
        "disk_pattern": NAS_DISK_PATTERN,
        "configured": NAS_CONFIG_FILE.exists(),
    })


@app.route("/api/config/nas", methods=["POST"])
@require_token
def update_nas_config():
    """更新 NAS 连接配置"""
    data = request.json or {}
    host = (data.get("host") or "").strip()
    user = (data.get("user") or "").strip()
    base_path = (data.get("base_path") or "").strip()
    port_raw = data.get("port", 22)
    disk_pattern = (data.get("disk_pattern") or "").strip()

    if not host:
        return jsonify({"error": "Host is required"}), 400
    if not user:
        return jsonify({"error": "User is required"}), 400
    if not base_path or not base_path.startswith("/"):
        return jsonify({"error": "Base path must be an absolute path"}), 400

    try:
        port = int(port_raw)
        if port < 1 or port > 65535:
            raise ValueError("port out of range")
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid port"}), 400

    try:
        save_nas_config(host, port, user, base_path, disk_pattern)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"save_nas_config failed: {e}")
        return jsonify({"error": "Failed to save"}), 500

    return jsonify({"status": "saved"})


@app.route("/api/config/nas/test", methods=["POST"])
@require_token
def test_nas_connection():
    """用提交的（或当前的）配置做一次轻量 SSH 探活"""
    data = request.json or {}
    host = (data.get("host") or NAS_HOST).strip()
    user = (data.get("user") or NAS_USER).strip()
    try:
        port = int(data.get("port") or NAS_PORT)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "message": "Invalid port"})
    base_path = (data.get("base_path") or NAS_BASE_PATH).strip()

    cmd = [
        "ssh",
        "-p", str(port),
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        f"{user}@{host}",
        f"test -d {shlex.quote(base_path)} && echo nasvault-ok || echo missing-path",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        out = (r.stdout or "").strip()
        if r.returncode == 0 and "nasvault-ok" in out:
            return jsonify({"status": "ok", "message": f"已连接 {user}@{host}:{port}，根路径可读"})
        if "missing-path" in out:
            return jsonify({"status": "error", "message": f"已连接，但根路径 {base_path} 不存在"})
        err = (r.stderr or "").strip().splitlines()[-1][:200] if r.stderr else "未知错误"
        return jsonify({"status": "error", "message": err})
    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "连接超时（5s 内未响应）"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/disk")
@require_token
def disk_usage():
    """获取磁盘使用情况"""
    # 优先用用户配置的 disk_pattern（支持 glob，如 /share/CACHEDEV*_DATA）
    # 缺省回落到 NAS_BASE_PATH 单挂载点
    pattern = NAS_DISK_PATTERN if NAS_DISK_PATTERN else NAS_BASE_PATH
    try:
        _validate_glob_pattern(pattern)
    except ValueError:
        return jsonify({"error": "Invalid disk pattern in config"}), 500
    # -P (POSIX) 强制单行输出。busybox df 默认会把长 filesystem 名换行，导致解析全失败。
    cmd = f"df -hP {pattern} 2>/dev/null"
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
    """[deprecated] 直删入口被契约 #1 替代。返回 410。

    Migrate: 用 POST /api/action/preview (kind='delete') 拿 signed_token，
             再 POST /api/action/confirm 才能执行删除。
    """
    return jsonify({
        "error": "deprecated",
        "use_preview": "/api/action/preview",
        "use_confirm": "/api/action/confirm",
        "doc": "Destructive operations now require preview→confirm with signed token.",
    }), 410


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


def _enumerate_dir_files(dir_paths: list[str], max_files: int = 500) -> list[str]:
    """递归列出目录内的文件路径（用于硬链接 / 种子匹配）。

    为什么需要：用户删除一个目录时，目录内的文件才是 qBit 下载的硬链接。
    `stat <dir>` 只返回目录自己的 inode，不会展开内容，导致硬链接 → 种子关系链断掉。
    """
    if not dir_paths:
        return []
    quoted = " ".join(shlex.quote(d) for d in dir_paths)
    # head 限流防止超大目录拖死 SSH；500 个文件足够命中绝大多数 PT 种子
    cmd = f"find {quoted} -type f 2>/dev/null | head -n {max_files}"
    code, stdout, _ = ssh_exec(cmd, timeout=60)
    if code != 0:
        return []
    return [line.strip() for line in stdout.strip().split("\n") if line.strip()]


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
    """[deprecated] alias 到 /api/action/preview (kind='delete')。

    Spike 阶段保留路由名以减少前端切换冲击；行为完全等同 /api/action/preview。
    legacy 字段 'files' / 'delete_torrents' 兼容，新代码用 'candidates' / 'options'。
    """
    data = request.json or {}
    # 兼容旧 shape：把 'files' 当成 candidates
    return _do_action_preview("delete", data)


@app.route("/api/delete-complete", methods=["POST"])
@require_token
def delete_complete():
    """[deprecated] Direct execute removed (契约 #1 R2-B1).

    Migrate: POST /api/action/preview (kind='delete') → get signed_token,
             then POST /api/action/confirm to execute.
    """
    return jsonify({
        "error": "deprecated",
        "use_preview": "/api/action/preview",
        "use_confirm": "/api/action/confirm",
        "doc": "Destructive operations now require preview→confirm with signed token.",
    }), 410




# ─────────────────────────────────────────────────────────────
# 契约 #1 + #2：统一 Destructive Action 协议 + Ground Truth Snapshot
# ─────────────────────────────────────────────────────────────


class SnapshotMismatch(Exception):
    """Confirm 阶段重读 snapshot 发现 ground truth 已变。"""

    def __init__(self, diffs: list[dict]):
        super().__init__(f"target changed since preview: {len(diffs)} diff(s)")
        self.diffs = diffs


def _ssh_stat_paths(paths: list[str]) -> dict[str, dict]:
    """SSH stat 一批 path，返回 {path: {inode, size_bytes, mtime, exists, is_dir}}。

    用 stat -c 通用 Linux 格式（GNU + BusyBox 兼容）。**关键**：把 path 让 stat 自己
    用 %n 输出，不嵌进 format string——否则 shlex.quote 的引号会变成 stat 输出里
    的字面量，导致解析 key 跟原 path 不匹配（结果 inode=0 / exists=False，下游
    `find -inum` 跳过 → 文件没真删却返回 already_gone）。
    """
    if not paths:
        return {}
    SEP = "\x1f"  # ASCII Unit Separator — 几乎不会出现在 path 里
    quoted = " ".join(shlex.quote(p) for p in paths)
    fmt = f"STAT{SEP}%n{SEP}%i{SEP}%s{SEP}%Y{SEP}%F"
    cmd = f"stat -c {shlex.quote(fmt)} {quoted} 2>/dev/null"
    _, out, _ = ssh_exec(cmd, timeout=60)

    result: dict[str, dict] = {}
    for line in out.splitlines():
        line = line.rstrip("\r\n")
        if not line.startswith(f"STAT{SEP}"):
            continue
        try:
            _, p, inode, size, mtime, ftype = line.split(SEP, 5)
            result[p] = {
                "exists": True,
                "inode": int(inode),
                "size_bytes": int(size),
                "mtime": int(mtime),
                "is_dir": "directory" in ftype,
            }
        except (ValueError, IndexError):
            continue

    # 兜底：命令返回里没出现的 path 标为缺失
    for p in paths:
        result.setdefault(p, {"exists": False})
    return result


def _build_delete_snapshot(candidates: list[dict]) -> dict:
    """SSH 实时拉 ground truth + 硬链接 + qBit 匹配，生成 canonical snapshot。

    candidates: [{"path": ...}, ...]，client 提交的原始候选——其他字段忽略。
    Server-side authoritative：所有 inode/size/mtime/realpath 都重新算。
    """
    paths = [validate_path(c["path"]) for c in candidates]
    _reject_base_path(paths)

    # 1. SSH stat → 拿 inode / size / mtime / is_dir
    stat_map = _ssh_stat_paths(paths)

    # 2. realpath 规范化
    realpath_map = _resolve_real_paths(paths)

    # 3. 硬链接遍历（沿用现有逻辑）
    hardlink_info = _resolve_all_hardlink_paths(paths)

    # 4. 目录扩展（用于 torrent 匹配）
    dir_paths = [p for p in paths if stat_map.get(p, {}).get("is_dir")]
    inner_files: list[str] = []
    inner_hardlinks: dict = {}
    if dir_paths:
        inner_files = _enumerate_dir_files(dir_paths)
        if inner_files:
            inner_hardlinks = _resolve_all_hardlink_paths(inner_files)

    # 5. 收集所有候选 path（顶层 + 顶层硬链接 + 目录内文件 + 内文件硬链接）
    all_paths: set[str] = set(paths)
    for info in hardlink_info.values():
        all_paths.update(info.get("all_paths", []))
    all_paths.update(inner_files)
    for info in inner_hardlinks.values():
        all_paths.update(info.get("all_paths", []))

    # 6. 真实大小（目录递归）
    real_sizes = _get_real_sizes(paths)

    # 7. qBit 种子匹配（顶层 + 内层 + 全部硬链接路径）
    torrents: list[dict] = []
    qbit_status = {"ok": True, "message": ""}
    try:
        matched = qbit.find_torrents_by_paths(list(all_paths))
        for t in matched:
            torrents.append({
                "hash": t["hash"],
                "name": t["name"],
                "size": t.get("total_size", 0),
                "content_path": t.get("content_path", ""),
                "save_path": t.get("save_path", ""),
                "state": t.get("state", ""),
            })
    except Exception as e:
        logger.error(f"[snapshot] qBit lookup failed: {e}")
        qbit_status = {"ok": False, "message": str(e)}

    # 8. 构建 items（每个用户提交的 path 一条）
    items = []
    for p in paths:
        stat = stat_map.get(p, {"exists": False})
        hl_info = hardlink_info.get(p, {})
        items.append({
            "path": p,
            "realpath": realpath_map.get(p, p),
            "exists": stat.get("exists", False),
            "inode": stat.get("inode", 0),
            "size_bytes": stat.get("size_bytes", 0),
            "mtime": stat.get("mtime", 0),
            "is_dir": stat.get("is_dir", False),
            "real_size": real_sizes.get(p, 0),
            "hardlinks": [hp for hp in hl_info.get("all_paths", []) if hp != p],
        })

    return {
        "captured_at": int(time.time()),
        "items": items,
        "all_to_delete": sorted({
            p for p in paths
        } | {
            hp for info in hardlink_info.values() for hp in info.get("all_paths", [])
        }),
        "torrents": torrents,
        "qbit_status": qbit_status,
    }


def _diff_snapshots(expected: dict, current: dict) -> list[dict]:
    """对比 preview 和 confirm 两次 snapshot 的 items，返回不一致的字段。

    只对比 user-submitted paths（snapshot.items），不对比 hardlinks/torrents——
    hardlinks 增减是合理的（其他进程加/删硬链接不该阻塞删除），inode 才是 anchor。
    """
    diffs = []
    exp_by_path = {it["path"]: it for it in expected.get("items", [])}
    cur_by_path = {it["path"]: it for it in current.get("items", [])}
    for path, exp in exp_by_path.items():
        cur = cur_by_path.get(path)
        if cur is None:
            diffs.append({"path": path, "kind": "missing_in_current"})
            continue
        # exists / inode / size / mtime 任一变化都阻塞
        for key in ("exists", "inode", "size_bytes", "mtime"):
            if exp.get(key) != cur.get(key):
                diffs.append({
                    "path": path,
                    "kind": f"{key}_changed",
                    "expected": exp.get(key),
                    "current": cur.get(key),
                })
    return diffs


def _delete_executor(payload: dict) -> dict:
    """Confirm 阶段执行删除：重读 snapshot 比对 → 删 qBit 种子 → inode-anchored 删剩余文件。

    raise SnapshotMismatch 视为业务失败（destructive_action.confirm 会捕获并落 status='failed'）。
    """
    expected_snapshot = payload["snapshot"]
    candidates = payload["candidates"]
    options = payload.get("options", {})
    delete_torrents = options.get("delete_torrents", True)

    # 1. 重新拉 ground truth snapshot
    current_snapshot = _build_delete_snapshot(candidates)

    # 2. 对比 — 不一致则阻塞
    diffs = _diff_snapshots(expected_snapshot, current_snapshot)
    if diffs:
        raise SnapshotMismatch(diffs)

    # 3. 删除 qBit 种子（先种子，避免 deleteFiles 之后我们还要删 rm）
    torrent_results = []
    qbit_deleted_paths: set[str] = set()
    if delete_torrents and expected_snapshot.get("torrents"):
        hashes = [t["hash"] for t in expected_snapshot["torrents"]]
        try:
            qbit.delete_torrents(hashes, delete_files=True)
            for t in expected_snapshot["torrents"]:
                torrent_results.append({
                    "hash": t["hash"], "name": t["name"], "status": "deleted",
                })
                if t.get("content_path"):
                    qbit_deleted_paths.add(t["content_path"])
            logger.info(f"[action/delete] removed {len(hashes)} torrents")
        except Exception as e:  # noqa: BLE001
            logger.error(f"[action/delete] qBit delete failed: {e}")
            torrent_results.append({"status": "error", "message": str(e)})

    # 4. inode-anchored 删除剩余文件
    # 文件：用 `find <base> -xdev -inum N -delete` 锚定 inode（处理 confirm 之间被 mv）
    # 目录：直接 rm -rf <path>（目录没有真硬链接概念）
    file_results: list[dict] = []
    base = NAS_BASE_PATH.rstrip("/")
    safe_base = shlex.quote(base)
    for item in expected_snapshot["items"]:
        path = item["path"]
        if path in qbit_deleted_paths:
            file_results.append({"path": path, "status": "deleted_by_qbit"})
            continue
        if item["is_dir"]:
            # 目录：直接 rm -rf path（验证存在）
            safe_path = shlex.quote(path)
            cmd = f'[ -e {safe_path} ] && rm -rf -- {safe_path} && echo OK || echo GONE'
            _, out, _ = ssh_exec(cmd, timeout=300)
            last = out.strip().splitlines()[-1] if out.strip() else "GONE"
            if last == "OK":
                file_results.append({"path": path, "status": "deleted"})
            else:
                file_results.append({"path": path, "status": "already_gone"})
        else:
            # 文件：find -inum 删所有 hardlink；inode=0（不存在）跳过
            if not item["exists"] or item["inode"] == 0:
                file_results.append({"path": path, "status": "already_gone"})
                continue
            inum = int(item["inode"])
            # -xdev 限制不跨文件系统；-print 让我们看删了哪些
            cmd = f'find {safe_base} -xdev -inum {inum} -print -delete 2>/dev/null'
            _, out, _ = ssh_exec(cmd, timeout=300)
            removed_paths = [ln for ln in out.strip().splitlines() if ln]
            if removed_paths:
                file_results.append({
                    "path": path, "status": "deleted", "inode_paths": removed_paths,
                })
            else:
                file_results.append({"path": path, "status": "already_gone"})

    # 5. 汇总释放空间（用 expected snapshot 的 real_size；同 inode 去重）
    freed_inodes: set[int] = set()
    total_freed = 0
    for item in expected_snapshot["items"]:
        inode = item.get("inode") or 0
        if inode and inode in freed_inodes:
            continue
        if inode:
            freed_inodes.add(inode)
        total_freed += item.get("real_size", 0)

    status_counts: dict[str, int] = {}
    for r in file_results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1

    logger.info(
        f"[action/delete] done: file_status={status_counts}, "
        f"torrents_removed={sum(1 for r in torrent_results if r.get('status') == 'deleted')}, "
        f"space_freed={human_size(total_freed)}"
    )

    return {
        "file_results": file_results,
        "torrent_results": torrent_results,
        # 'deleted' / 'deleted_by_qbit' 都是真消失了；
        # 'already_gone' 单独算（preview 后被外部删除 / 我们 find 没匹配）
        "total_files_deleted": sum(
            1 for r in file_results if r["status"] in ("deleted", "deleted_by_qbit")
        ),
        "total_files_already_gone": sum(
            1 for r in file_results if r["status"] == "already_gone"
        ),
        "total_torrents_deleted": sum(1 for r in torrent_results if r.get("status") == "deleted"),
        "space_freed": total_freed,
        "space_freed_human": human_size(total_freed),
    }


def _route_executor_by_kind(payload: dict) -> dict:
    """Spike 阶段只支持 'delete'。后续 Phase 2/3 加 nfo_write / archive / purge_provider。"""
    kind = payload.get("kind")
    if kind == "delete":
        return _delete_executor(payload)
    raise ValueError(f"unsupported kind: {kind!r}")


# ─────────────────────────────────────────────────────────────
# 新路由：/api/action/preview + /api/action/confirm
# ─────────────────────────────────────────────────────────────


def _do_action_preview(kind: str, raw_data: dict):
    """Preview 阶段共用逻辑——/api/action/preview 和 /api/delete-preview alias 都调它。"""
    if kind == "delete":
        candidates_in = raw_data.get("candidates") or raw_data.get("files") or []
        if not isinstance(candidates_in, list) or not candidates_in:
            return jsonify({"error": "candidates required (or legacy 'files')"}), 400
        # 兼容：candidates 可以是 [{path}, ...] 也可以是 [path, ...]
        candidates = []
        for c in candidates_in:
            if isinstance(c, str):
                candidates.append({"path": c})
            elif isinstance(c, dict) and c.get("path"):
                candidates.append({"path": c["path"]})
            else:
                return jsonify({"error": f"invalid candidate: {c!r}"}), 400
        options = raw_data.get("options") or {}
        if "delete_torrents" in raw_data:  # legacy field 兼容
            options.setdefault("delete_torrents", raw_data["delete_torrents"])
        snapshot = _build_delete_snapshot(candidates)
        payload = {
            "kind": "delete",
            "candidates": candidates,
            "snapshot": snapshot,
            "options": options,
        }
        res = destructive_action.create_preview(
            get_db(),
            kind="delete",
            payload=payload,
            server_secret=SERVER_SECRET,
            created_by="web_ui",
        )
        # 兼容旧 UI：preview 字段保留 delete-preview 原 shape 一部分
        preview_files = [{
            "path": it["path"],
            "is_dir": it["is_dir"],
            "inode": it["inode"],
            "size": it["real_size"],
            "size_human": human_size(it["real_size"]),
            "hardlink_paths": it["hardlinks"],
        } for it in snapshot["items"]]
        return jsonify({
            "action_id": res.action_id,
            "signed_token": res.signed_token,
            "expires_at": res.expires_at,
            "kind": "delete",
            "snapshot": snapshot,
            # legacy-compatible preview shape
            "files": preview_files,
            "torrents": [{**t, "size_human": human_size(t["size"])} for t in snapshot["torrents"]],
            "qbit_status": snapshot["qbit_status"],
            "total_size": sum(it["real_size"] for it in snapshot["items"]),
            "total_size_human": human_size(sum(it["real_size"] for it in snapshot["items"])),
            "total_hardlinks": sum(len(it["hardlinks"]) for it in snapshot["items"]),
        })
    return jsonify({"error": f"kind '{kind}' not supported in spike"}), 400


@app.route("/api/action/preview", methods=["POST"])
@require_token
def action_preview():
    data = request.json or {}
    kind = data.get("kind")
    if not kind:
        return jsonify({"error": "kind required"}), 400
    return _do_action_preview(kind, data)


@app.route("/api/action/confirm", methods=["POST"])
@require_token
def action_confirm():
    data = request.json or {}
    action_id = data.get("action_id")
    signed_token = data.get("signed_token")
    if not action_id or not signed_token:
        return jsonify({"error": "action_id and signed_token required"}), 400

    try:
        out = destructive_action.confirm(
            get_db(),
            action_id=action_id,
            signed_token=signed_token,
            server_secret=SERVER_SECRET,
            executor=_route_executor_by_kind,
        )
    except destructive_action.ActionNotFound:
        return jsonify({"error": "action_not_found", "action_id": action_id}), 404
    except destructive_action.ActionAlreadyConsumed:
        return jsonify({"error": "action_already_consumed", "action_id": action_id}), 409
    except destructive_action.ActionExpired:
        return jsonify({"error": "action_expired", "action_id": action_id}), 410
    except destructive_action.ActionTokenInvalid:
        return jsonify({"error": "invalid_signed_token", "action_id": action_id}), 401
    except destructive_action.ActionError as e:
        return jsonify({"error": "action_error", "detail": str(e)}), 400

    # SnapshotMismatch 走 destructive_action 的 "failed" 路径：检查 error 字段
    response: dict = {"action_id": out.action_id, "status": out.status}
    if out.status == "succeeded":
        response["result"] = out.result
    else:
        response["error"] = out.error
        # 把 snapshot mismatch 升级为 client 友好 status
        if out.error and out.error.startswith("SnapshotMismatch:"):
            response["status"] = "target_already_changed"
            # 从 DB 读 result_json 拿不到（executor 抛异常没 result）；用 error 字符串解析
            # spike 阶段简单：UI 提示用户刷新
            response["hint"] = "Target changed since preview; refresh and retry."
    return jsonify(response)


# ─────────────────────────────────────────────────────────────
# 手工恢复面板：列出 needs_manual_recovery / running 的 action
# ─────────────────────────────────────────────────────────────


@app.route("/api/action/recovery", methods=["GET"])
@require_token
def action_recovery_list():
    """列出需要人工恢复的 action（spike 阶段简单只读视图）。"""
    rows = get_db().execute(
        """
        SELECT action_id, kind, status, created_at, started_at, completed_at,
               error, recovery_hint, payload_json
          FROM destructive_actions
         WHERE status IN ('needs_manual_recovery', 'running', 'failed')
         ORDER BY created_at DESC
         LIMIT 50
        """
    ).fetchall()
    return jsonify({
        "actions": [dict(r) for r in rows],
    })


# ─────────────────────────────────────────────────────────────
# Cron jobs：crash recovery + expired pending cleanup
# ─────────────────────────────────────────────────────────────
# Spike 阶段用 in-process BackgroundScheduler。多 worker 生产部署应该把 cron
# 拆出独立 worker（Phase 1 正式落地时处理）。
#
# 跳过 reaper：测试场景（pytest）或显式 NAS_DISABLE_CRON=1
_DISABLE_CRON = os.environ.get("NAS_DISABLE_CRON", "").strip().lower() in ("1", "true", "yes")


def _cron_reap_stuck():
    try:
        conn = destructive_action.open_connection(DB_PATH)
        try:
            reaped = destructive_action.reap_stuck_actions(conn)
            cleaned = destructive_action.cleanup_expired_pending(conn)
            if reaped or cleaned:
                logger.info(
                    f"[cron] reaped {reaped} stuck running, cleaned {cleaned} expired pending"
                )
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        logger.error(f"[cron] reap failed: {e}")


def _start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(_cron_reap_stuck, "interval", minutes=1, id="reap_stuck", max_instances=1)
    sched.start()
    logger.info("[cron] BackgroundScheduler started (reap interval=1min)")
    return sched


_scheduler = None
if not _DISABLE_CRON:
    _scheduler = _start_scheduler()


if __name__ == "__main__":
    # 默认绑定 127.0.0.1，生产环境应使用 gunicorn
    app.run(host="127.0.0.1", port=8080, debug=False)
