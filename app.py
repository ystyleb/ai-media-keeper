"""NAS 文件管理器 - 通过 SSH 连接威联通 NAS 进行文件管理"""

import base64
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from functools import wraps
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests
from flask import (
    Flask,
    abort,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from services import (
    dedup,
    destructive_action,
    http_client,
    llm,
    metadata_cache,
    nfo_writer,
    organize_runner,
    path_resolver,
    qbit_auto,
    scanner,
    watch_sync,
)
from services import identify as identify_svc
from services import organize as organize_svc
from services.metadata.base import ProviderUnavailable
from services.metadata.tmdb import TMDBProvider

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 以 `python app.py` 直接运行时模块名是 __main__；但 routes/*.py 蓝图用
# `from app import ...` 延迟导入。不 alias 的话 `from app import X` 在 sys.modules
# 找不到 `app` → 把整个 6000 行 app.py 当成第二个模块重新 import → import-lock 死锁
# (并发请求全堵在 importlib 的 _ModuleLock 上) + 重复执行模块级副作用 (worker lock /
# 调度器 / QBitClient)。注册别名让 `python app.py` 与 `gunicorn app:app` 行为一致。
sys.modules.setdefault("app", sys.modules[__name__])

app = Flask(__name__)
# Dev 阶段禁掉 static (app.js/css) 的浏览器缓存，避免 hard reload 也拿不到新版
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

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
    NAS_CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
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
        f"  Persists across restarts. Delete the file to rotate.\n" + "=" * 60 + "\n\n"
    )
    return new_token


API_TOKEN = _load_api_token()
logger.info(f"API Token loaded ({len(API_TOKEN)} chars).")

# 契约 #1: server_secret 加载 + SQLite schema 初始化
# Phase 4B codex r2/r4/r5 BLOCKER: organize_runner + scanner 用 module-level
# lock + abort flag，**必须**单 worker。三层 enforcement:
#   1. WEB_CONCURRENCY env check（友好提示；user 显式声明 worker 数时拒）
#   2. sys.argv 检测 --preload / -w N（gunicorn 命令行参数；覆盖 99% 部署 case）
#   3. fcntl.flock 跨进程文件锁（fork 模式 ground truth；preload 模式被 #2 拦住）
# 三层缺一不可。100% 完美的多-worker 安全要等 Phase 4C 升级到 SQLite lease。
import fcntl as _fcntl

# Layer 1: WEB_CONCURRENCY env
WORKER_COUNT = int(os.environ.get("WEB_CONCURRENCY", "1"))
if WORKER_COUNT != 1:
    sys.stderr.write(
        f"ERROR: NAS Vault requires single worker (WEB_CONCURRENCY=1). "
        f"got WEB_CONCURRENCY={WORKER_COUNT}.\n"
        f"   organize_runner / scanner use module-level lock + abort flag,\n"
        f"   which cannot span workers. Run gunicorn with -w 1 or use flask dev.\n"
    )
    sys.exit(1)


# Layer 2: sys.argv check —— 拒 gunicorn --preload / -w >1
# 这是 ground truth for preload 模式（fcntl 在 preload 下被 fork 继承绕过）
def _check_gunicorn_args() -> None:
    argv = list(sys.argv)
    # gunicorn 直接命令行参数
    if "--preload" in argv:
        sys.stderr.write(
            "ERROR: gunicorn --preload is not supported by NAS Vault.\n"
            "   preload 模式下 module-level lock 被 fork 继承绕过，\n"
            "   导致 organize_runner / scanner 在多 worker 间无互斥。\n"
            "   Use: gunicorn -b 127.0.0.1:8080 -w 1 app:app (no --preload)\n"
        )
        sys.exit(1)
    # 检测 gunicorn worker count flag
    for idx, arg in enumerate(argv):
        if arg in ("-w", "--workers"):
            try:
                n = int(argv[idx + 1])
                if n != 1:
                    sys.stderr.write(
                        f"ERROR: gunicorn workers={n} not supported. NAS Vault requires -w 1.\n"
                    )
                    sys.exit(1)
            except (IndexError, ValueError):
                pass
        if arg.startswith("--workers="):
            try:
                n = int(arg.split("=", 1)[1])
                if n != 1:
                    sys.stderr.write(
                        f"ERROR: gunicorn workers={n} not supported. NAS Vault requires -w 1.\n"
                    )
                    sys.exit(1)
            except ValueError:
                pass


_check_gunicorn_args()

# Layer 3: fcntl.flock 跨进程文件锁 — fork-mode ground truth
# 标准 gunicorn (无 --preload) 下每个 worker fork 后重新 import → 再次 import-time
# 抢同一 .worker.lock → 第二个 worker 必失败。
# 测试 / dev pytest 场景跳过（让 pytest 跟 dev server 能共存；测试不真启 worker thread）
_WORKER_LOCK_FILE = CONFIG_DIR / ".worker.lock"
_is_test_env = (
    any("pytest" in arg for arg in sys.argv)
    or "pytest" in sys.modules
    or os.environ.get("PYTEST_CURRENT_TEST")
    or os.environ.get("NAS_SKIP_WORKER_LOCK") == "1"
)
_worker_lock_fd: int | None = None
if not _is_test_env:
    try:
        _worker_lock_fd = os.open(
            str(_WORKER_LOCK_FILE),
            os.O_CREAT | os.O_WRONLY | os.O_TRUNC,
            0o600,
        )
        _fcntl.flock(_worker_lock_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        os.write(_worker_lock_fd, f"{os.getpid()}\n".encode())
        # fd 故意泄漏：进程退出时 OS 释放 flock，下一次启动可再 acquire
    except BlockingIOError:
        sys.stderr.write(
            f"ERROR: another NAS Vault worker holds {_WORKER_LOCK_FILE}.\n"
            f"   NAS Vault requires single worker.\n"
            f"   If you see this with gunicorn, you ran with -w >1 — \n"
            f"   organize_runner / scanner are not safe under multiple workers.\n"
            f"   Use gunicorn -w 1 (Phase 4C will add SQLite lease for multi-worker).\n"
            f"   (If you're running tests while a dev server is up, set "
            f"NAS_SKIP_WORKER_LOCK=1)\n"
        )
        sys.exit(1)


# Layer 4: register_at_fork callback — preload 模式 ground truth (r6 BLOCKER)
# 在 gunicorn --preload (无论 via 命令行 / config file / GUNICORN_CMD_ARGS) 下，
# master 进程 import app + 抢 lock。fork 出的每个 worker 在 child 进程内触发
# at_fork callback → close 继承 fd + 自己 reopen + flock → 跟 master 持有的
# OFD 冲突 → fail → os._exit。
# 只在 gunicorn 上下文下注册（pytest / flask dev 不触发，避免测试干扰）。
def _enforce_singleton_after_fork() -> None:
    global _worker_lock_fd
    if _worker_lock_fd is not None:
        try:
            os.close(_worker_lock_fd)
        except OSError:
            pass
    try:
        new_fd = os.open(
            str(_WORKER_LOCK_FILE),
            os.O_CREAT | os.O_WRONLY,
            0o600,
        )
        _fcntl.flock(new_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        os.write(new_fd, f"{os.getpid()}\n".encode())
        _worker_lock_fd = new_fd
    except BlockingIOError:
        sys.stderr.write(
            f"ERROR: preload + multi-worker not supported. Worker {os.getpid()}\n"
            f"   cannot reacquire {_WORKER_LOCK_FILE} (master still holds it).\n"
            f"   Use gunicorn -c gunicorn.conf.py app:app (no --preload).\n"
        )
        os._exit(1)


# 注册条件：sys.argv[0] 路径含 'gunicorn' (覆盖 gunicorn 直跑 + gunicorn entrypoint)
# 不在 pytest / flask 直跑时注册（避免测试场景被 callback 干扰）
_argv0 = sys.argv[0] if sys.argv else ""
if "gunicorn" in os.path.basename(_argv0):
    os.register_at_fork(after_in_child=_enforce_singleton_after_fork)

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
    from db import migrations as _migrations

    phase3_summary = _migrations.phase3_migrate(_init_conn)
    phase4_summary = _migrations.phase4_migrate(_init_conn)
    phase5_summary = _migrations.phase5_migrate(_init_conn)
    logger.info(
        f"DB ready at {DB_PATH}; phase3 migration: {phase3_summary}; "
        f"phase4 migration: {phase4_summary}; "
        f"phase5 migration: {phase5_summary}"
    )
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


# qBittorrent 配置（默认值；密码现在落盘到 config/.qbit_pass，chmod 600）
DEFAULT_QBIT_CONFIG = {
    "url": os.environ.get("QBIT_URL", "http://192.168.1.100:8080"),
    "user": os.environ.get("QBIT_USER", "admin"),
    "password": os.environ.get("QBIT_PASS", ""),
}

QBIT_PASS_FILE = CONFIG_DIR / ".qbit_pass"
TMDB_KEY_FILE = CONFIG_DIR / ".tmdb_key"
DEEPSEEK_KEY_FILE = CONFIG_DIR / ".deepseek_key"
EMBY_KEY_FILE = CONFIG_DIR / ".emby_key"
EMBY_CONFIG_FILE = CONFIG_DIR / "emby.json"
ORGANIZE_CONFIG_FILE = CONFIG_DIR / "organize.json"
QBIT_AUTO_ORGANIZE_CONFIG_FILE = CONFIG_DIR / "qbit_auto_organize.json"

# Phase 4B：批量目录 organize 上限。N=500 时 payload_json ≈ 750KB（SQLite TEXT
# 单 row 无硬限制，但渐进式 result_json 更新会重写整 row，500 次写放大 ~50s），
# 选 500 是用户体感「最多 500 文件单次整理」+ 不爆 commit 耗时的折中。
MAX_ORGANIZE_BATCH_ITEMS = 500
# ≤ 此阈值的 confirm 走 inline 同步（响应 < 3s）；超过走 background worker + polling。
ORGANIZE_BATCH_INLINE_THRESHOLD = 5


def load_tmdb_key() -> str:
    """env > config/.tmdb_key > 空"""
    env = os.environ.get("TMDB_API_KEY", "").strip()
    if env:
        return env
    if TMDB_KEY_FILE.exists():
        return TMDB_KEY_FILE.read_text(encoding="utf-8").strip()
    return ""


def save_tmdb_key(key: str) -> None:
    """落盘 + chmod 600；空 key 删文件"""
    key = key.strip()
    if not key:
        if TMDB_KEY_FILE.exists():
            TMDB_KEY_FILE.unlink()
        return
    TMDB_KEY_FILE.write_text(key, encoding="utf-8")
    try:
        os.chmod(TMDB_KEY_FILE, 0o600)
    except OSError as e:
        logger.warning(f"could not chmod 600 {TMDB_KEY_FILE}: {e}")


def get_tmdb_provider() -> TMDBProvider | None:
    """每请求按需创建（key 可能 UI 上刚改），key 为空返回 None。"""
    key = load_tmdb_key()
    if not key:
        return None
    return TMDBProvider(api_key=key)


def load_deepseek_key() -> str:
    return llm.load_api_key(DEEPSEEK_KEY_FILE)


def save_deepseek_key(key: str) -> None:
    key = key.strip()
    if not key:
        if DEEPSEEK_KEY_FILE.exists():
            DEEPSEEK_KEY_FILE.unlink()
        return
    DEEPSEEK_KEY_FILE.write_text(key, encoding="utf-8")
    try:
        os.chmod(DEEPSEEK_KEY_FILE, 0o600)
    except OSError as e:
        logger.warning(f"could not chmod 600 {DEEPSEEK_KEY_FILE}: {e}")


# ─── Emby config ────────────────────────────────────────────────


def load_emby_config() -> dict:
    if EMBY_CONFIG_FILE.exists():
        try:
            return json.loads(EMBY_CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_emby_config(url: str, user_id: str) -> None:
    EMBY_CONFIG_FILE.write_text(
        json.dumps({"url": url.strip(), "user_id": user_id.strip()}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_emby_key() -> str:
    if EMBY_KEY_FILE.exists():
        return EMBY_KEY_FILE.read_text(encoding="utf-8").strip()
    return ""


def save_emby_key(key: str) -> None:
    key = key.strip()
    if not key:
        if EMBY_KEY_FILE.exists():
            EMBY_KEY_FILE.unlink()
        return
    EMBY_KEY_FILE.write_text(key, encoding="utf-8")
    try:
        os.chmod(EMBY_KEY_FILE, 0o600)
    except OSError as e:
        logger.warning(f"could not chmod 600 {EMBY_KEY_FILE}: {e}")


def _emby_client():
    """Return EmbyClient or None if not configured. Lazy import for test isolation."""
    cfg = load_emby_config()
    key = load_emby_key()
    if not (cfg.get("url") and cfg.get("user_id") and key):
        return None
    from clients.watch.emby import EmbyClient

    return EmbyClient(base_url=cfg["url"], user_id=cfg["user_id"], api_key=key)


# ─── Organize config (Phase 4A) ─────────────────────────────────


def load_organize_config() -> dict:
    """Phase 4A: 媒体库 hardlink 目标根配置。返回 {movies_root, tv_root} or {}."""
    if ORGANIZE_CONFIG_FILE.exists():
        try:
            return json.loads(ORGANIZE_CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_organize_config(cfg: dict) -> None:
    """落盘 config/organize.json。仅保留 movies_root / tv_root 两字段。"""
    payload = {
        "movies_root": (cfg.get("movies_root") or "").strip(),
        "tv_root": (cfg.get("tv_root") or "").strip(),
    }
    ORGANIZE_CONFIG_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ─── qBit auto-organize config (Phase 4C) ─────────────────────────────────

QBIT_AUTO_ORGANIZE_DEFAULTS = {
    "enabled": False,
    "categories": [],  # qBit category 白名单（[] = 不触发任何种子；显式列表防误触）
    "poll_interval_minutes": 5,  # cron 周期；最小 1min（防压垮 qBit API + DB）
    "confidence_threshold": 0.85,  # identifier confidence 门槛；< 标 skipped_low_confidence
    "auto_identify": False,  # 下载完自动识别未知种子（默认关，destructive 自动化保守）
    "auto_identify_confidence_threshold": 0.95,  # 自动识别后整理门槛（高于手动 0.85）
}


def load_qbit_auto_organize_config() -> dict:
    """Phase 4C: qBit 自动 organize 配置 — 默认 enabled=False（user 显式开启才生效）。

    Schema：{enabled, categories[], poll_interval_minutes, confidence_threshold}
    缺字段用默认值（前向兼容，新增字段 graceful）。
    """
    if QBIT_AUTO_ORGANIZE_CONFIG_FILE.exists():
        try:
            raw = json.loads(QBIT_AUTO_ORGANIZE_CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            raw = {}
    else:
        raw = {}
    merged = {**QBIT_AUTO_ORGANIZE_DEFAULTS, **raw}
    # 类型清洗 + 边界（防恶意 / 误配）
    merged["enabled"] = bool(merged.get("enabled", False))
    cats = merged.get("categories") or []
    if not isinstance(cats, list):
        cats = []
    # 过滤 None / 空白 / 非字符串可转后为空的项；先 None check 防 str(None)='None' 入选
    merged["categories"] = [str(c).strip() for c in cats if c is not None and str(c).strip()]
    try:
        merged["poll_interval_minutes"] = max(1, int(merged.get("poll_interval_minutes", 5)))
    except (TypeError, ValueError):
        merged["poll_interval_minutes"] = 5
    try:
        ct = float(merged.get("confidence_threshold", 0.85))
        merged["confidence_threshold"] = min(1.0, max(0.0, ct))
    except (TypeError, ValueError):
        merged["confidence_threshold"] = 0.85
    merged["auto_identify"] = bool(merged.get("auto_identify", False))
    try:
        ait = float(merged.get("auto_identify_confidence_threshold", 0.95))
        merged["auto_identify_confidence_threshold"] = min(1.0, max(0.0, ait))
    except (TypeError, ValueError):
        merged["auto_identify_confidence_threshold"] = 0.95
    return merged


def save_qbit_auto_organize_config(cfg: dict) -> None:
    """落盘 config/qbit_auto_organize.json。仅保留 schema 内 4 字段，做边界清洗。"""
    cats = cfg.get("categories") or []
    if not isinstance(cats, list):
        cats = []
    # 防 `0 or 5 = 5` 让 clamp 失效：先 None check，再 cast + clamp
    raw_poll = cfg.get("poll_interval_minutes")
    try:
        poll = max(1, int(raw_poll)) if raw_poll is not None else 5
    except (TypeError, ValueError):
        poll = 5
    raw_conf = cfg.get("confidence_threshold")
    try:
        conf = min(1.0, max(0.0, float(raw_conf))) if raw_conf is not None else 0.85
    except (TypeError, ValueError):
        conf = 0.85
    raw_ait = cfg.get("auto_identify_confidence_threshold")
    try:
        ait = min(1.0, max(0.0, float(raw_ait))) if raw_ait is not None else 0.95
    except (TypeError, ValueError):
        ait = 0.95
    payload = {
        "enabled": bool(cfg.get("enabled", False)),
        "categories": [str(c).strip() for c in cats if c is not None and str(c).strip()],
        "poll_interval_minutes": poll,
        "confidence_threshold": conf,
        "auto_identify": bool(cfg.get("auto_identify", False)),
        "auto_identify_confidence_threshold": ait,
    }
    QBIT_AUTO_ORGANIZE_CONFIG_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class QBitClient:
    """qBittorrent WebAPI 客户端（支持配置文件持久化）"""

    def __init__(self):
        self.session = requests.Session()
        self._logged_in = False
        # bug #7: cron 线程 + Flask 请求线程并发用同一 session（requests.Session 非线程
        # 安全）→ cookie jar / 连接池交错。RLock 串行化所有 HTTP 方法；RLock 允许
        # find_torrents_by_paths→get_torrents 这种重入。
        self._lock = threading.RLock()
        self._config: dict = {}
        self.load_config()

    def load_config(self):
        """加载配置：url / user 从 qbit.json，密码从 config/.qbit_pass（plain text）。

        密码来源优先级：UI 运行时设置 > config/.qbit_pass > QBIT_PASS env > 老 qbit.json
        明文（自动迁移到 .qbit_pass 并擦除旧字段）。

        落盘是 plain text + chmod 600。原本"密码永不落盘"的设计在 config/.api_token
        和 config/.signing_key 已落盘后失去相对意义——攻击者拿到 config 目录就拥有
        所有凭据，再藏密码无收益。
        """
        self._config = {
            "url": DEFAULT_QBIT_CONFIG["url"],
            "user": DEFAULT_QBIT_CONFIG["user"],
            "password": os.environ.get("QBIT_PASS", ""),
        }

        legacy_plaintext_found = False
        if QBIT_CONFIG_FILE.exists():
            try:
                with open(QBIT_CONFIG_FILE, encoding="utf-8") as f:
                    saved = json.load(f)
                self._config["url"] = saved.get("url", self._config["url"])
                self._config["user"] = saved.get("user", self._config["user"])
                # 老格式残留：旧 qbit.json 可能含明文 password。迁移到 .qbit_pass + 擦除
                if saved.get("password"):
                    self._config["password"] = saved["password"]
                    legacy_plaintext_found = True
            except (json.JSONDecodeError, OSError) as e:
                logger.error(f"Failed to load qBit config: {e}")
        else:
            self.save_config()

        # 从独立密码文件读（优先级低于 env，高于老 qbit.json 残留逻辑：
        # env 已经填进 self._config["password"]，若仍为空再读 .qbit_pass）
        if not self._config["password"] and QBIT_PASS_FILE.exists():
            try:
                pwd = QBIT_PASS_FILE.read_text(encoding="utf-8").strip()
                if pwd:
                    self._config["password"] = pwd
            except OSError as e:
                logger.error(f"Failed to read {QBIT_PASS_FILE}: {e}")

        self._logged_in = False
        self.session.cookies.clear()
        # LAN host (NAS / 同网段 qBit) bypass shell http_proxy 直连;
        # 否则 user 装 Clash/V2Ray 时 LAN 请求被代理路由 → 502.
        http_client.apply_proxy_policy(self.session, self._config.get("url") or "")

        if legacy_plaintext_found:
            logger.warning(
                f"qBit config: migrated plaintext password from qbit.json → {QBIT_PASS_FILE}"
            )
            self.save_config()  # 擦除 qbit.json 里的 password + 写新 .qbit_pass

    def save_config(self):
        """url / user → qbit.json；password → config/.qbit_pass（chmod 600）"""
        on_disk = {
            "url": self._config.get("url", ""),
            "user": self._config.get("user", ""),
        }
        QBIT_CONFIG_FILE.write_text(
            json.dumps(on_disk, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        password = self._config.get("password") or ""
        if password:
            QBIT_PASS_FILE.write_text(password, encoding="utf-8")
            try:
                os.chmod(QBIT_PASS_FILE, 0o600)
            except OSError as e:
                logger.warning(f"could not chmod 600 {QBIT_PASS_FILE}: {e}")
        elif QBIT_PASS_FILE.exists():
            # 显式清空密码（UI "清除" 等场景）→ 删文件
            try:
                QBIT_PASS_FILE.unlink()
            except OSError as e:
                logger.warning(f"could not unlink {QBIT_PASS_FILE}: {e}")

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
        # bug #7: 与并发的 _authed_request 串行化，避免改配置时清 cookie 撞正在跑的请求
        with self._lock:
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

    def _authed_request(self, method: str, path: str, *, timeout: int, **kwargs):
        """bug #7+#8: 持锁串行化共享 session + 401/403（cookie 过期/qBit 重启）时
        重置登录态 + 重登 + 重试一次。避免过期 cookie 永久 403（cron 静默停摆）。
        """
        with self._lock:
            self._ensure_login()
            url = f"{self.url}{path}"
            resp = self.session.request(method, url, timeout=timeout, **kwargs)
            if resp.status_code in (401, 403):
                self._logged_in = False
                self.session.cookies.clear()
                self._ensure_login()
                resp = self.session.request(method, url, timeout=timeout, **kwargs)
            resp.raise_for_status()
            return resp

    def test_connection(self) -> dict:
        """用已保存的配置测试连接"""
        try:
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

        session = http_client.session_for(url)
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
        return self._authed_request("GET", "/api/v2/torrents/info", timeout=30).json()

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

    def get_torrent_files(self, torrent_hash: str) -> list[dict]:
        """返回种子内文件清单。每个 dict 的 'name' 是相对 save_path 的路径
        （多文件种子含种子根目录名，如 'SeasonPack/ep01.mkv'）。

        bug #31：删种前用它枚举文件，只有全部文件都在用户预览删除集内才 delete_files。
        """
        return self._authed_request(
            "GET", "/api/v2/torrents/files", timeout=30, params={"hash": torrent_hash}
        ).json()

    def delete_torrents(self, hashes: list[str], delete_files: bool = True):
        # qBit WebAPI 标准是 POST（旧版接受 GET，但开源版本走标准）
        self._authed_request(
            "POST",
            "/api/v2/torrents/delete",
            timeout=60,
            data={
                "hashes": "|".join(hashes),
                "deleteFiles": "true" if delete_files else "false",
            },
        )


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
        "-p",
        str(NAS_PORT),
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "BatchMode=yes",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={SSH_CONTROL_PATH}",
        "-o",
        "ControlPersist=300",
        f"{NAS_USER}@{NAS_HOST}",
        cmd,
    ]
    try:
        result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)
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
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} PB"


# Phase B: app.js cache-bust by file mtime — browsers refresh after JS changes.
@app.context_processor
def _inject_static_versions():
    def _mtime(rel: str) -> int | str:
        try:
            return int(Path(__file__).parent.joinpath(rel).stat().st_mtime)
        except OSError:
            return "dev"

    return {
        "app_js_mtime": _mtime("static/app.js"),
        "legacy_css_mtime": _mtime("static/css/legacy.css"),
    }


# 注册 UI blueprints (server-rendered HTML fragments for HTMX)
from routes.ui_status import ui_status_bp

app.register_blueprint(ui_status_bp)

# Phase B pages blueprints
from routes.pages_files import pages_files_bp

app.register_blueprint(pages_files_bp)
from routes.pages_library import pages_library_bp

app.register_blueprint(pages_library_bp)
from routes.pages_dedup import pages_dedup_bp

app.register_blueprint(pages_dedup_bp)
from routes.pages_organize import pages_organize_bp

app.register_blueprint(pages_organize_bp)
from routes.pages_settings import pages_settings_bp

app.register_blueprint(pages_settings_bp)
from routes.pages_dashboard import pages_dashboard_bp

app.register_blueprint(pages_dashboard_bp)
from routes.ui_dashboard import ui_dashboard_bp

app.register_blueprint(ui_dashboard_bp)
from routes.ui_drawer import ui_drawer_bp

app.register_blueprint(ui_drawer_bp)
from routes.pages_onboarding import pages_onboarding_bp

app.register_blueprint(pages_onboarding_bp)


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
    return jsonify(
        {
            "host": NAS_HOST,
            "port": NAS_PORT,
            "user": NAS_USER,
            "base_path": NAS_BASE_PATH,
            "disk_pattern": NAS_DISK_PATTERN,
            "configured": NAS_CONFIG_FILE.exists(),
        }
    )


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
        "-p",
        str(port),
        "-o",
        "ConnectTimeout=5",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "BatchMode=yes",
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
            disks.append(
                {
                    "filesystem": parts[0],
                    "size": parts[1],
                    "used": parts[2],
                    "available": parts[3],
                    "use_percent": parts[4],
                    "mount": parts[5],
                }
            )
    return jsonify({"disks": disks})


@app.route("/api/files")
@require_token
def list_files():
    """列出目录内容。返回禁缓存 header — 删除/写 NFO 后前端 loadFiles 必须拿 fresh 数据。"""
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

        files.append(
            {
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
                "group": group,
            }
        )

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
                    inode_map[stat_line[idx + 1 :]] = int(stat_line[:idx])
            for f in files:
                if f["path"] in inode_map:
                    f["inode"] = inode_map[f["path"]]

    # 排序：目录在前，然后按名称
    files.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

    resp = jsonify(
        {
            "path": validated_path,
            "parent": os.path.dirname(validated_path) if validated_path != NAS_BASE_PATH else None,
            "files": files,
        }
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
        entries.append(
            {
                "path": filepath,
                "inode": inode,
                "links": links,
                "size": size,
            }
        )
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
                        found_path = stat_line[idx + 1 :]
                        if ino in inode_targets:
                            inode_targets[ino].append(found_path)

    hardlinks = []
    for entry in entries:
        targets = [t for t in inode_targets.get(entry["inode"], []) if t != entry["path"]]
        hardlinks.append(
            {
                "path": entry["path"],
                "inode": entry["inode"],
                "hardlinks": entry["links"],
                "size": entry["size"],
                "size_human": human_size(entry["size"]),
                "targets": targets,
            }
        )

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
    ".nfo",
    ".txt",
    ".log",
    ".md",
    ".readme",
    ".srt",
    ".ass",
    ".ssa",
    ".sub",
    ".idx",
    ".vtt",
    ".json",
    ".yml",
    ".yaml",
    ".ini",
    ".conf",
    ".cfg",
    ".sh",
    ".py",
    ".js",
    ".html",
    ".xml",
    ".csv",
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
    cmd = f"stat -c '%s' {safe} 2>/dev/null && head -c {MAX_PREVIEW_BYTES} {safe} | base64"
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

    return jsonify(
        {
            "text": text,
            "encoding": encoding_used,
            "size": file_size,
            "preview_bytes": min(MAX_PREVIEW_BYTES, file_size),
            "truncated": file_size > MAX_PREVIEW_BYTES,
            "parsed": parsed,
        }
    )


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
    return jsonify(
        {
            "error": "deprecated",
            "use_preview": "/api/action/preview",
            "use_confirm": "/api/action/confirm",
            "doc": "Destructive operations now require preview→confirm with signed token.",
        }
    ), 410


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
                        inode_to_paths.setdefault(ino, []).append(stat_line[idx + 1 :])
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
    return jsonify(
        {
            "error": "deprecated",
            "use_preview": "/api/action/preview",
            "use_confirm": "/api/action/confirm",
            "doc": "Destructive operations now require preview→confirm with signed token.",
        }
    ), 410


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


def _ssh_mkdir_p(path: str) -> tuple[int, str, str]:
    """SSH `mkdir -p <path>`. 已存在不抛错。返回 (rc, stdout, stderr)。"""
    return ssh_exec(f"mkdir -p {shlex.quote(path)}", timeout=10)


def _ssh_ln(src: str, dst: str) -> tuple[int, str, str]:
    """SSH `ln <src> <dst>` 创建硬链接。

    Phase 4A.2: 不加 -f flag —— dst 已存在直接报错。
    codex r5 BLOCKER 2: 加 pre-check `[ -d dst ]` + post-stat `[ -f dst ]`
    防 silent ln-into-dir race。BusyBox 不支持 `-T` flag，所以走 shell
    控制流而非 GNU 专属 option。

    codex r7 BLOCKER: 取消自动 cleanup — name-based unlink 不能证明
    ownership（即使 inode check 也 TOCTOU）。race-into-dir 时只 report
    marker + orphan hint，让调用方告知 user 手工清理。这跟 r1 B2 的决策
    «不动 dst_path 防误删» 是同源原则。

    stdout marker:
      DST_IS_DIR    — pre-check 命中：dst 已是目录（exit 99）
      DST_NOT_REGULAR — ln 后 dst 不是 regular file（race ln-into-dir
                        发生，orphan 可能在 <dst>/<basename(src)> exit 98）
      其他失败 → 普通 ln 错误（exit ln_rc，stderr 含 ln msg）
    """
    safe_src = shlex.quote(src)
    safe_dst = shlex.quote(dst)
    cmd = (
        f"if [ -d {safe_dst} ]; then echo DST_IS_DIR; exit 99; fi; "
        f"ln {safe_src} {safe_dst}; LN_RC=$?; "
        f"if [ $LN_RC -ne 0 ]; then exit $LN_RC; fi; "
        f"if [ ! -f {safe_dst} ]; then "
        f"  echo DST_NOT_REGULAR; exit 98; "
        f"fi"
    )
    return ssh_exec(cmd, timeout=10)


def _build_delete_snapshot(candidates: list[dict], *, mode: str = "lenient") -> dict:
    """SSH 实时拉 ground truth + 硬链接 + qBit 匹配，生成 canonical snapshot。

    candidates: [{"path": ...}, ...]，client 提交的原始候选——其他字段在 lenient 模式下忽略。
    Server-side authoritative：所有 inode/size/mtime/realpath 都重新算。

    mode='strict' (Phase 3.3 dedup source):
      - 每个 candidate 必须含 expected_inode + expected_size + expected_mtime
      - SSH stat 后强制对比；任一不一致 → snapshot 含 mismatches[] + blocked=True
      - 调用方应该看到 blocked=True 就 abort，不继续 create_preview
    mode='lenient' (file_browser source):
      - 不要求 expected_* 字段，纯 ground truth snapshot
    """
    if mode not in ("strict", "lenient"):
        raise ValueError(f"invalid snapshot mode: {mode!r}")
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
            torrents.append(
                {
                    "hash": t["hash"],
                    "name": t["name"],
                    "size": t.get("total_size", 0),
                    "content_path": t.get("content_path", ""),
                    "save_path": t.get("save_path", ""),
                    "state": t.get("state", ""),
                }
            )
    except Exception as e:
        logger.error(f"[snapshot] qBit lookup failed: {e}")
        qbit_status = {"ok": False, "message": str(e)}

    # 8. 构建 items（每个用户提交的 path 一条）
    items = []
    for p in paths:
        stat = stat_map.get(p, {"exists": False})
        hl_info = hardlink_info.get(p, {})
        items.append(
            {
                "path": p,
                "realpath": realpath_map.get(p, p),
                "exists": stat.get("exists", False),
                "inode": stat.get("inode", 0),
                "size_bytes": stat.get("size_bytes", 0),
                "mtime": stat.get("mtime", 0),
                "is_dir": stat.get("is_dir", False),
                "real_size": real_sizes.get(p, 0),
                "hardlinks": [hp for hp in hl_info.get("all_paths", []) if hp != p],
            }
        )

    # Phase 3.3: strict mode 强制对比 expected_* 字段 → 任一不一致都生成 mismatches
    mismatches: list[dict] = []
    blocked = False
    if mode == "strict":
        # candidates 跟 paths 同序（validate_path 是纯路径规整，顺序保持）
        for original, p in zip(candidates, paths):
            stat = stat_map.get(p, {"exists": False})
            diffs_for_path: list[str] = []
            if not stat.get("exists"):
                diffs_for_path.append("missing")
            else:
                if original.get("expected_inode") is not None and original[
                    "expected_inode"
                ] != stat.get("inode"):
                    diffs_for_path.append("inode_changed")
                if original.get("expected_size") is not None and original[
                    "expected_size"
                ] != stat.get("size_bytes"):
                    diffs_for_path.append("size_changed")
                if original.get("expected_mtime") is not None and original[
                    "expected_mtime"
                ] != stat.get("mtime"):
                    diffs_for_path.append("mtime_changed")
            if diffs_for_path:
                mismatches.append(
                    {
                        "path": p,
                        "diffs": diffs_for_path,
                        "expected": {
                            "inode": original.get("expected_inode"),
                            "size_bytes": original.get("expected_size"),
                            "mtime": original.get("expected_mtime"),
                        },
                        "current": {
                            "inode": stat.get("inode"),
                            "size_bytes": stat.get("size_bytes"),
                            "mtime": stat.get("mtime"),
                        },
                    }
                )
        blocked = bool(mismatches)

    return {
        "captured_at": int(time.time()),
        "mode": mode,
        "blocked": blocked,
        "mismatches": mismatches,
        "items": items,
        "all_to_delete": sorted(
            {p for p in paths}
            | {hp for info in hardlink_info.values() for hp in info.get("all_paths", [])}
        ),
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
                diffs.append(
                    {
                        "path": path,
                        "kind": f"{key}_changed",
                        "expected": exp.get(key),
                        "current": cur.get(key),
                    }
                )
    return diffs


def _partition_torrents_for_deletion(
    snapshot: dict,
    *,
    get_files_fn: Callable[[str], list[dict]],
    resolve_fn: Callable[[list[str]], dict[str, str]],
) -> tuple[list[str], list[dict]]:
    """bug #31：决定哪些种子可以连文件一起删（delete_files=True）。

    只有当一个种子的**全部文件**都落在用户预览删除集（items 文件 + 它们的 hardlinks +
    目录 item 的子树）内时，才删该种子；否则跳过——防止"选一集 → delete_files 删整季"
    这种不可逆误删。拿不到文件清单的种子保守跳过。

    返回 (to_delete_hashes, skipped)；skipped: [{hash, name, reason}]。
    依赖注入 get_files_fn / resolve_fn 便于单测。
    """
    torrents = snapshot.get("torrents", [])
    if not torrents:
        return [], []

    items = snapshot.get("items", [])
    file_items = [it for it in items if not it.get("is_dir")]
    dir_items = [it for it in items if it.get("is_dir")]

    # 1. 收集所有要 realpath 规范化的 path（删除集 + 种子文件绝对路径）
    raw_paths: set[str] = set()
    for it in items:
        raw_paths.add(it["path"])
        for hp in it.get("hardlinks", []) or []:
            raw_paths.add(hp)

    torrent_files_abs: dict[str, list[str]] = {}
    files_unavailable: set[str] = set()
    for t in torrents:
        h = t["hash"]
        save = t.get("save_path", "") or ""
        try:
            files = get_files_fn(h)
        except Exception as e:
            logger.warning(f"[delete/#31] get_torrent_files({h}) failed: {e}")
            files_unavailable.add(h)
            continue
        abs_list = []
        for f in files:
            name = f.get("name", "")
            if not name:
                continue
            ap = os.path.join(save, name) if save else name
            abs_list.append(ap)
            raw_paths.add(ap)
        torrent_files_abs[h] = abs_list

    # 2. 一次性 realpath（处理 QNAP symlink，让删除集与种子文件在同一命名空间比对）
    real = resolve_fn(sorted(raw_paths)) if raw_paths else {}

    def _rp(p: str) -> str:
        return real.get(p, p)

    deleted_files_real: set[str] = set()
    for it in file_items:
        deleted_files_real.add(_rp(it["path"]))
        for hp in it.get("hardlinks", []) or []:
            deleted_files_real.add(_rp(hp))
    deleted_dirs_real = [_rp(it["path"]).rstrip("/") for it in dir_items]

    def _contained(fp_real: str) -> bool:
        if fp_real in deleted_files_real:
            return True
        for d in deleted_dirs_real:
            if fp_real == d or fp_real.startswith(d + "/"):
                return True
        return False

    to_delete: list[str] = []
    skipped: list[dict] = []
    for t in torrents:
        h = t["hash"]
        name = t.get("name", "")
        if h in files_unavailable:
            skipped.append({"hash": h, "name": name, "reason": "files_list_unavailable"})
            continue
        files = torrent_files_abs.get(h, [])
        if files and all(_contained(_rp(fp)) for fp in files):
            to_delete.append(h)
        else:
            skipped.append({"hash": h, "name": name, "reason": "partial_torrent_not_removed"})
    return to_delete, skipped


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
    # bug #31: 只删「全部文件都在用户预览删除集内」的种子；含未选中文件的种子跳过
    #          （只在磁盘删用户预览的具体文件）——防"选一集 → delete_files 删整季"。
    # bug #5:  删种失败不再吞掉 + 继续删文件（会留下 orphan 种子 / 状态分裂）。
    #          删种异常直接传播 → confirm 标 failed → 零文件删除（文件循环在其后）。
    torrent_results: list[dict] = []
    qbit_deleted_paths: set[str] = set()
    if delete_torrents and expected_snapshot.get("torrents"):
        to_delete_hashes, skipped_torrents = _partition_torrents_for_deletion(
            expected_snapshot,
            get_files_fn=qbit.get_torrent_files,
            resolve_fn=_resolve_real_paths,
        )
        by_hash = {t["hash"]: t for t in expected_snapshot["torrents"]}
        for st in skipped_torrents:
            torrent_results.append(
                {
                    "hash": st["hash"],
                    "name": st.get("name", ""),
                    "status": "skipped",
                    "reason": st["reason"],
                }
            )
            logger.info(
                f"[action/delete] torrent {st['hash'][:8]} skipped ({st['reason']}); "
                f"only previewed files removed"
            )
        if to_delete_hashes:
            # bug #5: 不 try/except 吞 — 失败直接抛，confirm 标 failed，零文件删除
            qbit.delete_torrents(to_delete_hashes, delete_files=True)
            for h in to_delete_hashes:
                t = by_hash.get(h, {})
                torrent_results.append({"hash": h, "name": t.get("name", ""), "status": "deleted"})
                if t.get("content_path"):
                    qbit_deleted_paths.add(t["content_path"])
            logger.info(f"[action/delete] removed {len(to_delete_hashes)} torrents")

    # 4. 删除剩余文件/目录（scoped 到预览的具体 path + inode 校验）
    file_results: list[dict] = []
    for item in expected_snapshot["items"]:
        path = item["path"]
        if path in qbit_deleted_paths:
            file_results.append({"path": path, "status": "deleted_by_qbit"})
            continue
        expected_inode = int(item.get("inode") or 0)
        if item["is_dir"]:
            # bug #30: 目录删除前 re-verify inode == 预览快照 inode，避免 drift→rm 之间
            #          路径被 swap 成另一个目录而误删用户没预览过的目录。
            if expected_inode == 0 or not item.get("exists", True):
                file_results.append({"path": path, "status": "already_gone"})
                continue
            safe_path = shlex.quote(path)
            cmd = (
                f'if [ "$(stat -c %i -- {safe_path} 2>/dev/null || echo 0)" = '
                f'"{expected_inode}" ]; then rm -rf -- {safe_path} && echo OK || echo RMFAIL; '
                f"else echo INODE_MISMATCH; fi"
            )
            _, out, _ = ssh_exec(cmd, timeout=300)
            last = out.strip().splitlines()[-1] if out.strip() else "RMFAIL"
            if last == "OK":
                file_results.append({"path": path, "status": "deleted"})
            elif last == "INODE_MISMATCH":
                file_results.append({"path": path, "status": "skipped_inode_mismatch"})
            elif last == "RMFAIL":
                # Codex CONCERN: rm -rf 真失败（权限 / busy / IO）≠ already_gone。
                # 单独标 delete_failed，避免误报"已删"让 ops 以为目录没了实际还在。
                file_results.append({"path": path, "status": "delete_failed"})
            else:
                file_results.append({"path": path, "status": "already_gone"})
        else:
            # bug #4: 不用全局 `find <base> -xdev -inum N -delete`（会删走 preview 后
            #          新建的同 inode hardlink，如 organize cron 刚 ln 进库的库内副本）。
            #          只删预览快照捕获的精确 path 集（item path + 该 item 的 hardlinks），
            #          每条用 scoped `find <path> -maxdepth 0 -inum N` 校验 inode 后删。
            #          (残留 TOCTOU：find 自身 stat→unlink 之间窗口，已是 shell 能做到的最小。)
            if not item["exists"] or expected_inode == 0:
                file_results.append({"path": path, "status": "already_gone"})
                continue
            target_paths = [path] + [hp for hp in (item.get("hardlinks") or []) if hp]
            removed_paths: list[str] = []
            for tp in target_paths:
                safe_tp = shlex.quote(tp)
                cmd = (
                    f"find {safe_tp} -maxdepth 0 -inum {expected_inode} -print -delete 2>/dev/null"
                )
                _, out, _ = ssh_exec(cmd, timeout=300)
                if [ln for ln in out.strip().splitlines() if ln]:
                    removed_paths.append(tp)
            if removed_paths:
                file_results.append(
                    {
                        "path": path,
                        "status": "deleted",
                        "inode_paths": removed_paths,
                    }
                )
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
        "total_files_already_gone": sum(1 for r in file_results if r["status"] == "already_gone"),
        "total_torrents_deleted": sum(1 for r in torrent_results if r.get("status") == "deleted"),
        "space_freed": total_freed,
        "space_freed_human": human_size(total_freed),
    }


def _nfo_write_executor(payload: dict) -> dict:
    """Confirm 阶段：重读 .nfo + video snapshot → atomic write → readback verify。

    Per-item 错误不让整批 fail；每个 item 给单独 status，summary 在 result 里。
    """
    items = payload["items"]
    # 重读所有 nfo + video path 的 ground truth
    all_paths = [it["nfo_path"] for it in items] + [it["video_path"] for it in items]
    stat_now = _ssh_stat_paths(all_paths)

    results: list[dict] = []
    for it in items:
        video_path = it["video_path"]
        nfo_path = it["nfo_path"]
        new_xml = it["new_xml"]
        tmdb_id = it.get("tmdb_id") or ""

        video_now = stat_now.get(video_path, {"exists": False})
        nfo_now = stat_now.get(nfo_path, {"exists": False})

        # video 文件如果消失 → 写 NFO 没意义，skip
        if not video_now.get("exists"):
            results.append(
                {
                    "video_path": video_path,
                    "nfo_path": nfo_path,
                    "status": "skipped",
                    "reason": "video_missing",
                }
            )
            continue

        # nfo 的 preview-to-confirm 漂移检测
        snap = it["nfo_snapshot"]
        if snap["existed"]:
            if not nfo_now.get("exists"):
                # 用户在 preview→confirm 之间删了 nfo —— 还是写新的（不再有冲突）
                pass
            else:
                # 仍然存在：size/mtime 必须跟 snapshot 一致；否则有人改过，跳过
                if nfo_now["size_bytes"] != snap["size_bytes"] or nfo_now["mtime"] != snap["mtime"]:
                    results.append(
                        {
                            "video_path": video_path,
                            "nfo_path": nfo_path,
                            "status": "skipped",
                            "reason": "nfo_changed_since_preview",
                        }
                    )
                    continue
        else:
            # snapshot 时不存在；现在存在 → 别人写了一份
            if nfo_now.get("exists"):
                results.append(
                    {
                        "video_path": video_path,
                        "nfo_path": nfo_path,
                        "status": "skipped",
                        "reason": "nfo_appeared_since_preview",
                    }
                )
                continue

        # 执行写入：base64 over SSH，原子 mv
        try:
            xml_b64 = base64.b64encode(new_xml.encode("utf-8")).decode("ascii")
            safe_nfo = shlex.quote(nfo_path)
            safe_tmp = shlex.quote(nfo_path + ".tmp")
            safe_bak = shlex.quote(nfo_path + ".bak")

            # 1. 若旧 .nfo 存在 → cp 到 .bak（覆盖之前的 .bak）
            backup_step = (
                f"[ -e {safe_nfo} ] && cp -p {safe_nfo} {safe_bak}; " if snap["existed"] else ""
            )
            # 2. 写 .tmp（base64 解码 + 重定向）
            # 3. 原子 mv
            # 4. readback 提取 tmdbid 验证
            # codex r5 IMPORTANT: grep -c 在 count=0 时 rc=1 让 && 链断 → 整条
            # rc=1 → write_failed 误归类。用 subshell `(grep || echo 0)` 隔离：
            # mv 成功 → 跑 subshell；mv 失败 → && 链断不跑 subshell，rc=mv_rc
            # 保 mv 失败的 write_failed 语义，同时 grep count=0 不再误归类。
            cmd = (
                f"{backup_step}"
                f"printf '%s' {shlex.quote(xml_b64)} | base64 -d > {safe_tmp} && "
                f"mv {safe_tmp} {safe_nfo} && "
                f"( grep -c 'tmdb' {safe_nfo} || echo 0 )"
            )
            rc, out, err = ssh_exec(cmd, timeout=30)
            if rc != 0:
                results.append(
                    {
                        "video_path": video_path,
                        "nfo_path": nfo_path,
                        "status": "failed",
                        "reason": f"write_failed: {err.strip()[:200]}",
                    }
                )
                continue
            # readback verify: grep -c 'tmdb' 至少应该 ≥ 1（我们写了 uniqueid + tmdbid）
            count = int(out.strip().splitlines()[-1]) if out.strip() else 0
            if tmdb_id and count < 1:
                results.append(
                    {
                        "video_path": video_path,
                        "nfo_path": nfo_path,
                        "status": "failed",
                        "reason": "readback_missing_tmdbid",
                    }
                )
                continue
            results.append(
                {
                    "video_path": video_path,
                    "nfo_path": nfo_path,
                    "status": "overwrote" if snap["existed"] else "created",
                    "backup_path": (nfo_path + ".bak") if snap["existed"] else None,
                }
            )
        except Exception as e:
            results.append(
                {
                    "video_path": video_path,
                    "nfo_path": nfo_path,
                    "status": "failed",
                    "reason": f"{type(e).__name__}: {e}",
                }
            )

    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    logger.info(f"[action/nfo_write] done: {counts}")
    return {
        "items": results,
        "status_counts": counts,
        "total_created": counts.get("created", 0),
        "total_overwrote": counts.get("overwrote", 0),
        "total_skipped": counts.get("skipped", 0),
        "total_failed": counts.get("failed", 0),
    }


class ArchiveDisabledError(Exception):
    """Sentinel: archive executor disabled in Phase 3, but still resolves through
    the standard destructive-action confirm pipeline so MCP / automation clients
    see a normal failure with a recognizable error message.
    """


def _archive_executor(payload: dict) -> dict:
    """Phase 3.5 stub: archive kind 不接入 SSH copy/verify/unlink。

    保留契约 #1 一致性 — preview 仍返 signed_token；confirm 走到此处会 raise，
    destructive_action.confirm 把 exception 映射到 status='failed' +
    error='ArchiveDisabledError: ...'。MCP / 自动化客户端可识别此模式。

    [code-enforced] 不调任何 SSH / qBit / 文件操作。
    """
    raise ArchiveDisabledError(
        "archive_kind_disabled_in_phase3: "
        "Archive 操作未启用，Phase 3 不实施 SSH copy/verify/unlink。"
    )


def _ssh_create_nfo_if_absent(
    nfo_path: str, xml: str, *, verify_tmdb: bool = True
) -> tuple[bool, str]:
    """Atomic create-only NFO write：写 tmp → ln tmp final → rm tmp。

    与 `_ssh_atomic_write_nfo`（overwrite-via-mv）的关键区别 — 用 `ln` 而非 `mv`，
    dst 已存在时 ln 失败（hardlink 创建只在 final 不存在时成功），返
    (False, 'nfo_exists') 让调用方转 'skipped: nfo_exists'。

    这是 codex r3 BLOCKER 修法：把「不覆盖已有 NFO」做成 write-boundary atomic
    contract，而不是依赖 stat→mv 之间无 race 的预 check。

    tmp 文件名带 uuid12 后缀防并发互踩。verify_tmdb=False 时跳过 grep readback
    （payload 没 tmdb_id 时不该 false-positive）。
    """
    try:
        import uuid

        xml_b64 = base64.b64encode(xml.encode("utf-8")).decode("ascii")
        tmp_basename = f"{os.path.basename(nfo_path)}.tmp.{uuid.uuid4().hex[:12]}"
        nfo_dir = os.path.dirname(nfo_path)
        tmp_path = f"{nfo_dir}/{tmp_basename}" if nfo_dir else tmp_basename
        safe_nfo = shlex.quote(nfo_path)
        safe_tmp = shlex.quote(tmp_path)
        # codex r4 NIT: grep -c 在 count=0 时 rc=1 让整个 shell rc!=0 进 create_failed 分支，
        # 误归类。用 `|| echo 0` 兜底保证 rc=0，count 由 stdout 决定。
        verify_step = f"; grep -c 'tmdb' {safe_nfo} || echo 0" if verify_tmdb else ""
        # codex r4 + r5 BLOCKER: `ln src dir/` 会在 dir 内 link 不失败。
        # 双重防护：pre-check [-d] + post-stat [-f]（race-window：dir 在
        # pre-check 后 ln 前出现时，post-stat 抓到）
        # codex r7 BLOCKER: 取消自动 cleanup tmp-in-dir — name-based unlink
        # 不能证明 ownership。orphan tmp file 留在 race-dir 是 garbage 但比误删安全。
        # 1. 检查 dst 不是 directory → echo NFO_IS_DIR exit 99
        # 2. 写 tmp + ln tmp final（atomic create-only）
        # 3. 无论 ln 成败 rm tmp（独占 uuid 后缀的 tmp 在 nfo_dir 是安全的——
        #    不会跟 race-into-dir 的 tmp 冲突，因为后者在 final 内部）
        # 4. ln 失败 + dst 已存在 → echo NFO_EXISTS（exit ln_rc）
        # 5. ln 成功但 dst 不是 regular file → ln-into-dir race，echo
        #    NFO_TARGET_NOT_REGULAR + exit 98（不 cleanup orphan，让 user 手工查）
        cmd = (
            f"if [ -d {safe_nfo} ]; then echo NFO_IS_DIR; exit 99; fi; "
            f"( printf '%s' {shlex.quote(xml_b64)} | base64 -d > {safe_tmp} && "
            f"ln {safe_tmp} {safe_nfo} ); LN_RC=$?; "
            f"rm -f {safe_tmp}; "
            f"if [ $LN_RC -ne 0 ]; then "
            f"  [ -e {safe_nfo} ] && echo NFO_EXISTS; "
            f"  exit $LN_RC; "
            f"fi; "
            f"if [ ! -f {safe_nfo} ]; then "
            f"  echo NFO_TARGET_NOT_REGULAR; exit 98; "
            f"fi{verify_step}"
        )
        rc, out, err = ssh_exec(cmd, timeout=30)
        if rc != 0:
            if "NFO_IS_DIR" in (out or ""):
                return False, "nfo_is_directory"
            if "NFO_TARGET_NOT_REGULAR" in (out or ""):
                return False, "nfo_target_not_regular_race"
            if "NFO_EXISTS" in (out or ""):
                return False, "nfo_exists"
            return False, f"create_failed: {err.strip()[:200]}"
        if verify_tmdb:
            try:
                count = int(out.strip().splitlines()[-1])
            except (ValueError, IndexError):
                count = 0
            if count < 1:
                return False, "readback_missing_tmdbid"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _build_nfo_payload_for_organize(cached, nfo_kind: str):
    """根据 cached metadata + nfo_kind 构造 NFOPayload。

    nfo_kind ∈ {'movie', 'episode', 'tvshow'} (nfo_writer 协议)
    """
    return nfo_writer.NFOPayload(
        media_type=nfo_kind,
        title=cached.title,
        original_title=cached.original_title,
        year=cached.year,
        plot=cached.overview,
        tmdb_id=cached.tmdb_id,
        imdb_id=cached.imdb_id,
        tvdb_id=None,  # cache 不存 tvdb_id
        rating=cached.vote_average,
        genres=cached.genres or [],
        cast=cached.cast or [],
        runtime_minutes=cached.runtime_minutes,
        poster_url=cached.poster_url,
        season=cached.season_number if nfo_kind == "episode" else None,
        episode=cached.episode_number if nfo_kind == "episode" else None,
        episode_title=cached.episode_title if nfo_kind == "episode" else None,
        episode_overview=cached.episode_overview if nfo_kind == "episode" else None,
        episode_air_date=cached.episode_air_date if nfo_kind == "episode" else None,
        episode_still_url=cached.episode_still_url if nfo_kind == "episode" else None,
    )


def _write_organize_nfo(
    src_path: str,
    target_nfo_path: str,
    nfo_kind: str,
    *,
    expected_metadata: dict | None = None,
) -> str:
    """读 cache → 验证 + 构造 NFOPayload → build_nfo → atomic create-only write。

    codex r3: 不再用 target_exists pre-check（stat→mv 之间有 race window）；
    依靠 _ssh_create_nfo_if_absent 的 atomic ln 做 write-boundary enforcement。
    dst 已存在 → ln 失败 → helper 返 'nfo_exists' → 这里转 'skipped: nfo_exists'。

    expected_metadata: preview 阶段签进 payload 的 {tmdb_id,title,year,season_number,
    episode_number}；confirm 用 cache 重读后比对，不一致 → skipped (cache 已漂移，
    不写 NFO 避免跟 hardlink 后的目录位置不一致)。

    返回 status string:
      'created' / 'skipped: nfo_exists' / 'skipped: cache_drift' /
      'failed: no_cache' / 'failed: <reason>'。
    Pattern D: 失败不抛 — 调用方根据 status 决定要不要告知用户「文件已整理但 NFO 失败」。
    """
    # codex r2 IMPORTANT: cache read / enrich / build payload 整段都包 Pattern D 边界
    # —— hardlink 已成功，任何 DB / build error 都必须转 nfo_status='failed: ...'，
    # 不能 leak 到 executor 让整个 item 被记 'executor_crashed'（破坏 Pattern D 语义）。
    try:
        cached, _ = metadata_cache.get_by_path(get_db(), src_path)
        if cached is None:
            return "failed: no_cache"

        # 防 cache 漂移：preview 跟 confirm 之间 metadata 被改了 → 不写 NFO（避免目录跟 NFO 不一致）
        if expected_metadata is not None:
            for key, expected in expected_metadata.items():
                actual = getattr(cached, key, None)
                if expected != actual:
                    return f"skipped: cache_drift ({key} {expected!r}→{actual!r})"

        # ROADMAP #9: tv episode NFO 写回前 lazy enrich episode-specific 字段
        # （scanner 阶段没拉 episode 详情，到这里如果还缺就主动调一次 TMDB）。
        # 失败返回原 cached，<plot> 仍 fallback 到 series overview（旧行为，不退化）。
        if nfo_kind == "episode":
            cached, drift_detected = metadata_cache.ensure_episode_details(
                get_db(),
                get_tmdb_provider(),
                cached,
            )
            # codex r2 BLOCKER: enrich 内 guarded UPDATE rowcount=0 显式 signal drift
            # → 不能信任 refreshed cached（可能 refresh 失败回退到旧 snapshot），
            # 直接 short-circuit，不依赖第二次 drift check 来兜底。
            if drift_detected:
                return "skipped: cache_drift (during episode enrich)"
            # 第二道 drift check 抓 enrich helper 没覆盖的主字段（title/year）变化
            if expected_metadata is not None:
                for key, expected in expected_metadata.items():
                    actual = getattr(cached, key, None)
                    if expected != actual:
                        return f"skipped: cache_drift ({key} {expected!r}→{actual!r})"

        payload = _build_nfo_payload_for_organize(cached, nfo_kind)
        xml = nfo_writer.build_nfo(payload)
    except Exception as e:
        # DB error / build error 等 → Pattern D 返 nfo_status='failed: ...'
        # 不让 hardlink success 的 organize item 变 'executor_crashed'
        return f"failed: build_nfo {type(e).__name__}: {e}"
    ok, reason = _ssh_create_nfo_if_absent(
        target_nfo_path,
        xml,
        verify_tmdb=bool(cached.tmdb_id),
    )
    if ok:
        return "created"
    if reason == "nfo_exists":
        return "skipped: nfo_exists"
    # codex r8 IMPORTANT: race-into-dir 时给 orphan hint 让 user SSH 手工查
    if reason == "nfo_target_not_regular_race":
        return (
            f"failed: {reason}; orphan tmp possibly at "
            f"{target_nfo_path.rstrip('/')}/{os.path.basename(target_nfo_path)}.tmp.<uuid>"
        )
    return f"failed: {reason}"


_ORGANIZE_REQUIRED_METADATA_KEYS = {
    "tmdb_id",
    "title",
    "year",
    "media_type",
    "season_number",
    "episode_number",
}


def _organize_executor_one_item(item: dict, expected_metadata: dict | None) -> dict:
    """Phase 4B.3：提取自 _organize_executor 的 per-item 逻辑，让 inline 和
    background worker 都能调。

    流程：
      1. Re-stat src（防 mv 偷换）— src inode 锚 #1
      2. Check dst（已存在 + inode 同 → already_linked；已存在 + inode 异 → failed）
      3. mkdir -p dst_dir（已存在不抛错）
      4. ln src dst
      5. Verify dst inode == src inode — Pattern C 锚 #2
      6. 写 episode/movie NFO + tvshow.nfo（仅 tv 且未存在）

    Cleanup 策略（codex r1 修订）：
      - ln 失败：**不动** dst_dir（mkdir -p 不保证目录是本 action 创建的）
      - ln verify inode mismatch：**不动** dst_path（已不是预期 inode）
      - 已有 NFO：skip 不覆盖（status='skipped: nfo_exists'）

    返回值：dict 必含 src_path + status；status ∈ {succeeded, already_linked, failed}。
    """
    src_path = item["src_path"]
    plan = item["computed_plan"]
    media_type = item["media_type"]
    src_snap_pre = item["src_snapshot"]

    # codex r3 IMPORTANT: metadata_snapshot 必须存在且完整
    if not isinstance(expected_metadata, dict):
        return {
            "src_path": src_path,
            "status": "failed",
            "reason": "missing_metadata_snapshot_in_payload",
        }
    missing_keys = _ORGANIZE_REQUIRED_METADATA_KEYS - set(expected_metadata.keys())
    if missing_keys:
        return {
            "src_path": src_path,
            "status": "failed",
            "reason": f"incomplete_metadata_snapshot: missing_keys={sorted(missing_keys)}",
        }

    src_now = _ssh_stat_paths([src_path]).get(src_path, {"exists": False})
    if not src_now.get("exists"):
        return {
            "src_path": src_path,
            "status": "failed",
            "reason": "src_missing_at_confirm",
        }
    # Pattern C 锚 #1
    if src_now.get("inode") != src_snap_pre.get("inode"):
        return {
            "src_path": src_path,
            "status": "failed",
            "reason": "src_inode_changed_since_preview",
            "preview_inode": src_snap_pre.get("inode"),
            "current_inode": src_now.get("inode"),
        }

    dst_path = plan["dst_path"]
    dst_now = _ssh_stat_paths([dst_path]).get(dst_path, {"exists": False})

    if dst_now.get("exists") and dst_now.get("inode") == src_now.get("inode"):
        return {
            "src_path": src_path,
            "status": "already_linked",
            "dst_path": dst_path,
            "shared_inode": src_now.get("inode"),
        }
    if dst_now.get("exists"):
        return {
            "src_path": src_path,
            "status": "failed",
            "reason": "dst_exists_different_inode",
            "dst_path": dst_path,
            "dst_inode": dst_now.get("inode"),
            "src_inode": src_now.get("inode"),
        }

    # 1. mkdir -p
    rc, _, err = _ssh_mkdir_p(plan["dst_dir"])
    if rc != 0:
        return {
            "src_path": src_path,
            "status": "failed",
            "reason": f"mkdir_failed: {err.strip()[:200]}",
        }

    # 2. ln src dst — _ssh_ln 内部含 pre-check + post-stat 防 race-into-dir
    rc, ln_out, err = _ssh_ln(src_path, dst_path)
    if rc != 0:
        item_result = {"src_path": src_path, "status": "failed"}
        if "DST_IS_DIR" in (ln_out or ""):
            item_result["reason"] = "dst_is_directory_at_ln"
            item_result["hint"] = (
                f"dst 已是目录: {dst_path}. SSH 检查后再 organize (可能需要 mv 或 rm 该目录)"
            )
        elif "DST_NOT_REGULAR" in (ln_out or ""):
            item_result["reason"] = "ln_target_not_regular_race"
            item_result["hint"] = (
                f"race-into-dir 检测到: dst {dst_path} 在 ln 之间被替换成"
                f"目录。可能 orphan hardlink 在 "
                f"{dst_path.rstrip('/')}/{os.path.basename(src_path)}, "
                f"请 SSH 手工 ls -li 验证 inode 后再 rm。"
            )
        else:
            item_result["reason"] = f"ln_failed: {err.strip()[:200]}"
        return item_result

    # 3. Pattern C 锚 #2: verify dst inode == src inode
    verify = _ssh_stat_paths([dst_path]).get(dst_path, {"exists": False})
    if not verify.get("exists") or verify.get("inode") != src_now.get("inode"):
        return {
            "src_path": src_path,
            "status": "failed",
            "reason": "ln_verify_failed_inode_mismatch",
            "expected_inode": src_now.get("inode"),
            "actual_inode": verify.get("inode"),
            "hint": "dst_path 已不是预期 inode；可能并发 process 改了它。请 SSH 手工检查后再决定。",
        }

    # 4. 写 NFO（Pattern D：失败不回滚 hardlink）
    nfo_kind = "episode" if media_type == "tv" else "movie"
    nfo_status = _write_organize_nfo(
        src_path,
        plan["nfo_path"],
        nfo_kind,
        expected_metadata=expected_metadata,
    )
    tvshow_nfo_status = "skipped"
    if media_type == "tv" and plan.get("tvshow_nfo_path"):
        tvshow_nfo_status = _write_organize_nfo(
            src_path,
            plan["tvshow_nfo_path"],
            "tvshow",
            expected_metadata=expected_metadata,
        )

    return {
        "src_path": src_path,
        "dst_path": dst_path,
        "status": "succeeded",
        "src_inode": src_now.get("inode"),
        "dst_inode": verify.get("inode"),
        "nfo_path": plan["nfo_path"],
        "nfo_status": nfo_status,
        "tvshow_nfo_path": plan.get("tvshow_nfo_path"),
        "tvshow_nfo_status": tvshow_nfo_status,
    }


def _organize_executor_one_item_threadsafe(item: dict, expected_metadata: dict | None) -> dict:
    """Worker thread 调用的 wrapper：push 独立 Flask app context。

    Worker thread 不继承 request 的 app context，直接调 get_db() 拿 g.db 会撞
    `RuntimeError: Working outside of application context`。每 item 一个 app
    context → 每 item 一个独立 SQLite connection（teardown_appcontext 自动 close），
    不复用 request 的 g.db（也不该复用——sqlite3 connection thread-affinity）。
    """
    with app.app_context():
        return _organize_executor_one_item(item, expected_metadata)


def _build_and_start_auto_organize(paths: list[str], qbit_hash: str) -> dict:
    """Phase 4C.3 cron 触发 callback：组装 organize action + 起 background worker.

    走 Phase 4B background dispatch 同 stack（atomic_consume + verify token +
    start_organize_executor），但:
    - created_by='cron'（destructive_actions audit 区分）
    - signed_token 内部生成 + 立刻消费（不暴露 HTTP）→ 保持契约 #1 一致性
    - 不接受 selected_indices（auto = 跑全部）
    - 调用方（services/qbit_auto.dispatch_one）已经做过 confidence_gate，假设
      paths 全部可识别且 supported；这里只做 plan 计算 + dst stat + payload sign.

    返 {"action_id", "status": "started"|"locked"|"error", "error": str|None}.

    Threading：cron 在 APScheduler thread 跑（不继承 request 的 Flask app context）。
    入口 push app.app_context() 让 get_db() / metadata_cache 调用拿得到 g.db。
    详见 [[methodology-patterns]] Threading 章节.
    """
    with app.app_context():
        return _build_and_start_auto_organize_impl(paths, qbit_hash)


def _build_and_start_auto_organize_impl(paths: list[str], qbit_hash: str) -> dict:
    """Inner impl，假设 caller 已 push app context."""
    org_cfg = load_organize_config()
    movies_root = (org_cfg.get("movies_root") or "").strip()
    tv_root = (org_cfg.get("tv_root") or "").strip()
    if not (movies_root and tv_root):
        return {"action_id": None, "status": "error", "error": "organize_roots_not_configured"}

    if not paths:
        return {"action_id": None, "status": "error", "error": "empty_paths"}
    if len(paths) > MAX_ORGANIZE_BATCH_ITEMS:
        return {
            "action_id": None,
            "status": "error",
            "error": f"batch_too_large: {len(paths)} > {MAX_ORGANIZE_BATCH_ITEMS}",
        }

    try:
        # ── compute plan for each path ───
        src_stat_now = _ssh_stat_paths(paths)
        # confidence_gate 已 cached.media_type ∈ {movie, tv}，所以 cache 必命中可识别
        db = get_db()
        current_stats = {
            p: {"inode": s.get("inode"), "mtime": s.get("mtime")}
            for p, s in src_stat_now.items()
            if s.get("exists")
        }
        cache_map = metadata_cache.get_many_by_path(db, paths, current_stats=current_stats)

        plans_by_src: dict[str, organize_svc.OrganizePlan] = {}
        dst_check_paths: list[str] = []
        skipped_during_build: list[dict] = []  # audit：路径 missing / cache 漂移 等
        for sp in paths:
            src_stat = src_stat_now.get(sp, {"exists": False})
            if not src_stat.get("exists"):
                skipped_during_build.append({"path": sp, "reason": "src_missing"})
                continue
            cached, cache_status = cache_map.get(sp, (None, "miss"))
            if (
                cached is None
                or cache_status == "stale"
                or cached.media_type not in ("movie", "tv")
            ):
                # confidence_gate 之后到这里之间 cache 被改了 = 罕见 race；skip
                skipped_during_build.append(
                    {
                        "path": sp,
                        "reason": f"cache_drift: status={cache_status} media_type="
                        f"{getattr(cached, 'media_type', None)!r}",
                    }
                )
                continue
            try:
                plan = organize_svc.compute_organize_plan(sp, cached, movies_root, tv_root)
            except organize_svc.OrganizeNotApplicable as e:
                skipped_during_build.append({"path": sp, "reason": f"not_applicable: {e}"})
                continue
            plans_by_src[sp] = plan
            dst_check_paths.extend([plan.dst_dir, plan.dst_path, plan.nfo_path])
            if plan.tvshow_nfo_path:
                dst_check_paths.append(plan.tvshow_nfo_path)

        if not plans_by_src:
            # confidence_gate 通过但所有 path 都漂移 — 极罕见
            return {
                "action_id": None,
                "status": "error",
                "error": f"no plans computed for {qbit_hash}; "
                f"all paths drifted: {skipped_during_build}",
            }

        dst_stat = _ssh_stat_paths(dst_check_paths) if dst_check_paths else {}

        # ── 构造 payload_items（跳过 6 状态分类，confidence_gate 已守门）───
        payload_items: list[dict] = []
        for sp, plan in plans_by_src.items():
            src_stat = src_stat_now[sp]
            dst_now = dst_stat.get(plan.dst_path, {"exists": False})
            src_inode = src_stat.get("inode")
            already_linked = bool(dst_now.get("exists") and dst_now.get("inode") == src_inode)
            payload_items.append(
                {
                    "src_path": sp,
                    "src_snapshot": {
                        "inode": src_stat.get("inode"),
                        "size_bytes": src_stat.get("size_bytes"),
                        "mtime": src_stat.get("mtime"),
                    },
                    "media_type": plan.media_type,
                    "tmdb_id": plan.tmdb_id,
                    "title": plan.title,
                    "year": plan.year,
                    "season_number": plan.season_number,
                    "episode_number": plan.episode_number,
                    "computed_plan": {
                        "dst_dir": plan.dst_dir,
                        "dst_path": plan.dst_path,
                        "nfo_path": plan.nfo_path,
                        "tvshow_nfo_path": plan.tvshow_nfo_path,
                    },
                    "metadata_snapshot": {
                        "tmdb_id": plan.tmdb_id,
                        "title": plan.title,
                        "year": plan.year,
                        "media_type": plan.media_type,
                        "season_number": plan.season_number,
                        "episode_number": plan.episode_number,
                    },
                    "dst_status": {
                        "dst_dir_exists": dst_stat.get(plan.dst_dir, {}).get("exists", False),
                        "dst_path_exists": dst_now.get("exists", False),
                        "nfo_path_exists": dst_stat.get(plan.nfo_path, {}).get("exists", False),
                        "tvshow_nfo_exists": (
                            dst_stat.get(plan.tvshow_nfo_path, {}).get("exists", False)
                            if plan.tvshow_nfo_path
                            else False
                        ),
                        "already_linked": already_linked,
                        "conflict": dst_now.get("exists") and not already_linked,
                    },
                }
            )

        payload = {
            "kind": "organize",
            "items": payload_items,
            "snapshot": {"captured_at": int(time.time())},
            "auto_organize": {
                "qbit_hash": qbit_hash,
                "skipped_during_build": skipped_during_build,
            },
        }

        # ── create preview row（created_by='cron'）───
        res = destructive_action.create_preview(
            db,
            kind="organize",
            payload=payload,
            server_secret=SERVER_SECRET,
            created_by="cron",
        )
        action_id = res.action_id
        signed_token = res.signed_token

        # PreviewResult 没暴露 payload_hash，重读 DB 拿（_atomic_consume / _verify_token 要用）
        pre_row = db.execute(
            "SELECT payload_hash FROM destructive_actions WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        if pre_row is None:
            return {
                "action_id": action_id,
                "status": "error",
                "error": "preview row missing after create (internal bug)",
            }
        payload_hash = pre_row["payload_hash"]

        # ── atomic consume + verify（同 confirm 路由 background 分支）───
        row = destructive_action._atomic_consume(
            db,
            action_id,
            payload_hash,
            int(time.time()),
        )
        if row is None:
            return {
                "action_id": action_id,
                "status": "error",
                "error": "atomic_consume_failed (internal race)",
            }
        if not destructive_action._verify_token(
            SERVER_SECRET, action_id, payload_hash, signed_token
        ):
            destructive_action._rollback_to_pending(db, action_id)
            return {
                "action_id": action_id,
                "status": "error",
                "error": "verify_token_failed (internal bug)",
            }

        # ── start worker ───
        try:
            organize_runner.start_organize_executor(
                db_path=DB_PATH,
                action_id=action_id,
                payload=payload,
                execute_one_item=_organize_executor_one_item_threadsafe,
                selected_indices=None,
            )
        except organize_runner.ConcurrentOrganizeError:
            destructive_action._rollback_to_pending(db, action_id)
            return {"action_id": action_id, "status": "locked", "error": None}
        except Exception as e:
            logger.exception(f"[auto-organize] start worker failed for {qbit_hash}")
            destructive_action._mark_terminal(
                db,
                action_id,
                status="failed",
                result=None,
                error=f"worker_start_failed: {type(e).__name__}: {e}",
            )
            return {"action_id": action_id, "status": "error", "error": f"{type(e).__name__}: {e}"}

        logger.info(
            f"[auto-organize] started action_id={action_id} qbit_hash={qbit_hash} "
            f"items={len(payload_items)}"
        )
        return {"action_id": action_id, "status": "started", "error": None}

    except Exception as e:
        logger.exception(f"[auto-organize] build_and_start failed for {qbit_hash}")
        return {"action_id": None, "status": "error", "error": f"{type(e).__name__}: {e}"}


def _organize_executor(payload: dict, selected_indices: list[int] | None = None) -> dict:
    """Phase 4A.3 inline 入口：循环调 _organize_executor_one_item。

    Phase 4B：保留作为 inline (≤ ORGANIZE_BATCH_INLINE_THRESHOLD) 路径。
    codex r1 BLOCKER 1: inline 路径也支持 selected_indices（≤5 文件用户也可能勾子集）
    codex r1 IMP5: 单 item 异常被 catch 标 failed，跟 background 行为对齐。
    codex r3 BLOCKER 2: 持 organize_runner inline lock，防一个 background organize
    跑期间 inline organize 并发跑 hardlink/NFO/qBit 副作用（违反「全局唯一 active organize」契约）。
    """
    # codex r3 BLOCKER 2: acquire inline lock — 失败 = 已有 organize 在跑，全 fail
    if not organize_runner.try_acquire_inline_lock():
        active = organize_runner.get_active_action_id()
        items = payload["items"]
        logger.warning(
            f"[organize/inline] another organize active ({active!r}); rejecting {len(items)} items"
        )
        results = [
            {
                "src_path": it.get("src_path"),
                "status": "failed",
                "index": idx,
                "reason": "another_organize_running",
                "hint": f"另一个 organize ({active}) 正在执行；等其完成或中止后再试。",
            }
            for idx, it in enumerate(items)
        ]
        counts = {"failed": len(results)}
        return {
            "items": results,
            "status_counts": counts,
            "total_succeeded": 0,
            "total_already_linked": 0,
            "total_failed": len(results),
            "total_skipped_by_user": 0,
        }

    try:
        items = payload["items"]
        selected_set: set[int] | None = (
            set(selected_indices) if selected_indices is not None else None
        )
        results: list[dict] = []
        for idx, it in enumerate(items):
            src_path = it.get("src_path")
            if selected_set is not None and idx not in selected_set:
                results.append(
                    {
                        "src_path": src_path,
                        "status": "skipped_by_user",
                        "index": idx,
                    }
                )
                continue
            try:
                r = _organize_executor_one_item(it, it.get("metadata_snapshot"))
                r.setdefault("src_path", src_path)
                r.setdefault("status", "failed")
            except Exception as e:
                logger.exception(f"[organize/inline] item {src_path!r} crashed")
                r = {
                    "src_path": src_path,
                    "status": "failed",
                    "reason": f"executor_crashed: {type(e).__name__}: {e}",
                }
            r["index"] = idx
            results.append(r)

        counts: dict[str, int] = {}
        for r in results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        logger.info(f"[action/organize] done: {counts}")
        return {
            "items": results,
            "status_counts": counts,
            "total_succeeded": counts.get("succeeded", 0),
            "total_already_linked": counts.get("already_linked", 0),
            "total_failed": counts.get("failed", 0),
            "total_skipped_by_user": counts.get("skipped_by_user", 0),
        }
    finally:
        organize_runner.release_inline_lock()


def _route_executor_by_kind(payload: dict) -> dict:
    """Phase 4A.3: 支持 'delete' + 'nfo_write' + 'archive' (stub) + 'organize'."""
    kind = payload.get("kind")
    if kind == "archive":
        return _archive_executor(payload)
    if kind == "delete":
        return _delete_executor(payload)
    if kind == "nfo_write":
        return _nfo_write_executor(payload)
    if kind == "organize":
        return _organize_executor(payload)
    raise ValueError(f"unsupported kind: {kind!r}")


# ─────────────────────────────────────────────────────────────
# 新路由：/api/action/preview + /api/action/confirm
# ─────────────────────────────────────────────────────────────


_VALID_DELETE_SOURCES = {"dedup", "file_browser"}


def _do_action_preview(kind: str, raw_data: dict):
    """Preview 阶段共用逻辑——/api/action/preview 和 /api/delete-preview alias 都调它。

    Phase 3.3: kind='delete' 加显式 source + snapshot_mode 字段，强制互锁:
      - source='dedup' AND snapshot_mode != 'strict' → 400
      - snapshot_mode='strict' 时每个 candidate 必须含 expected_inode/size/mtime
    """
    if kind == "delete":
        candidates_in = raw_data.get("candidates") or raw_data.get("files") or []
        if not isinstance(candidates_in, list) or not candidates_in:
            return jsonify({"error": "candidates required (or legacy 'files')"}), 400

        # source: 显式 field，legacy 调用方（旧 file-browser 前端）不传则 fallback
        source = raw_data.get("source") or "file_browser"
        if source not in _VALID_DELETE_SOURCES:
            return jsonify(
                {
                    "error": "source_invalid",
                    "detail": f"source must be one of {sorted(_VALID_DELETE_SOURCES)}, got {source!r}",
                }
            ), 400

        # snapshot_mode: dedup 强制 strict；file_browser 默认 lenient
        snapshot_mode = raw_data.get("snapshot_mode") or (
            "strict" if source == "dedup" else "lenient"
        )
        if snapshot_mode not in ("strict", "lenient"):
            return jsonify(
                {"error": f"snapshot_mode must be strict|lenient, got {snapshot_mode!r}"}
            ), 400

        # 互锁：dedup 来源**不允许** lenient（强 enforcement，避免前端 bug 绕过）
        if source == "dedup" and snapshot_mode != "strict":
            return jsonify(
                {
                    "error": "dedup_source_must_use_strict_mode",
                    "detail": "dedup-source delete must enforce strict expected_* snapshot",
                }
            ), 400

        # Strict 模式必填 expected_inode + expected_size + expected_mtime
        if snapshot_mode == "strict":
            for i, c in enumerate(candidates_in):
                if not isinstance(c, dict):
                    return jsonify(
                        {
                            "error": "strict_mode_requires_expected_fields",
                            "candidate_index": i,
                            "detail": "candidate must be object containing expected_inode/size/mtime",
                        }
                    ), 400
                for f in ("expected_inode", "expected_size", "expected_mtime"):
                    val = c.get(f)
                    if val is None:
                        return jsonify(
                            {
                                "error": "strict_mode_requires_expected_fields",
                                "candidate_index": i,
                                "missing": f,
                            }
                        ), 400
                    # bug #22: bool 是 int 子类，expected_inode=true 会绕过类型校验，
                    # 后续跟 server 端 int 做 != 比较时 True==1 让 drift guard 失效。
                    # 必须先显式拒 bool 再判 int。
                    if isinstance(val, bool) or not isinstance(val, int):
                        return jsonify(
                            {
                                "error": "strict_mode_requires_integer_fields",
                                "candidate_index": i,
                                "field": f,
                                "detail": f"{f} must be a non-negative integer",
                            }
                        ), 400

        # 兼容：candidates 可以是 [{path}, ...] 也可以是 [path, ...]
        candidates = []
        for c in candidates_in:
            if isinstance(c, str):
                candidates.append({"path": c})
            elif isinstance(c, dict) and c.get("path"):
                cand = {"path": c["path"]}
                # strict mode 把 expected_* 也带进去给 _build_delete_snapshot 用
                for f in ("expected_inode", "expected_size", "expected_mtime"):
                    if c.get(f) is not None:
                        cand[f] = c[f]
                candidates.append(cand)
            else:
                return jsonify({"error": f"invalid candidate: {c!r}"}), 400

        options = raw_data.get("options") or {}
        if "delete_torrents" in raw_data:  # legacy field 兼容
            options.setdefault("delete_torrents", raw_data["delete_torrents"])

        snapshot = _build_delete_snapshot(candidates, mode=snapshot_mode)

        # strict mode 撞到 mismatch → 立刻返 blocked，不生成 signed_token
        if snapshot.get("blocked"):
            resp = jsonify(
                {
                    "blocked": True,
                    "kind": "delete",
                    "source": source,
                    "snapshot_mode": snapshot_mode,
                    "mismatches": snapshot["mismatches"],
                    "message": "以下文件已变化，请刷新索引后重试",
                }
            )
            resp.status_code = 409  # Conflict: state diverged
            return resp

        payload = {
            "kind": "delete",
            "source": source,
            "snapshot_mode": snapshot_mode,
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
        preview_files = [
            {
                "path": it["path"],
                "is_dir": it["is_dir"],
                "inode": it["inode"],
                "size": it["real_size"],
                "size_human": human_size(it["real_size"]),
                "hardlink_paths": it["hardlinks"],
            }
            for it in snapshot["items"]
        ]
        return jsonify(
            {
                "action_id": res.action_id,
                "signed_token": res.signed_token,
                "expires_at": res.expires_at,
                "kind": "delete",
                "source": source,
                "snapshot_mode": snapshot_mode,
                "snapshot": snapshot,
                # legacy-compatible preview shape
                "files": preview_files,
                "torrents": [
                    {**t, "size_human": human_size(t["size"])} for t in snapshot["torrents"]
                ],
                "qbit_status": snapshot["qbit_status"],
                "total_size": sum(it["real_size"] for it in snapshot["items"]),
                "total_size_human": human_size(sum(it["real_size"] for it in snapshot["items"])),
                "total_hardlinks": sum(len(it["hardlinks"]) for it in snapshot["items"]),
            }
        )
    if kind == "archive":
        # Phase 3.5 stub: preview 仍走完整契约 #1（产 signed_token），但不 SSH stat。
        # confirm 时 _archive_executor raise → destructive_action.confirm 落
        # status='failed' + error='ArchiveDisabledError: ...'。
        candidates_in = raw_data.get("candidates") or []
        if not isinstance(candidates_in, list) or not candidates_in:
            return jsonify({"error": "candidates required"}), 400
        candidates = []
        for c in candidates_in:
            if isinstance(c, str):
                candidates.append({"path": c})
            elif isinstance(c, dict) and c.get("path"):
                candidates.append({"path": c["path"]})
            else:
                return jsonify({"error": f"invalid candidate: {c!r}"}), 400
        snapshot_stub = {
            "captured_at": int(time.time()),
            "mode": "stub",
            "blocked": False,
            "mismatches": [],
            "items": [{"path": c["path"]} for c in candidates],
            "all_to_delete": [],
            "torrents": [],
            "qbit_status": {"ok": True, "message": "(archive stub — no SSH performed)"},
        }
        payload = {
            "kind": "archive",
            "candidates": candidates,
            "snapshot": snapshot_stub,
            "options": raw_data.get("options") or {},
        }
        res = destructive_action.create_preview(
            get_db(),
            kind="archive",
            payload=payload,
            server_secret=SERVER_SECRET,
            created_by="web_ui",
        )
        return jsonify(
            {
                "action_id": res.action_id,
                "signed_token": res.signed_token,
                "expires_at": res.expires_at,
                "kind": "archive",
                "snapshot": snapshot_stub,
                "warning": "archive_executor_disabled",
                "warning_message": (
                    "Archive 操作目前未启用：confirm 会落 status='failed' + "
                    "error='ArchiveDisabledError: archive_kind_disabled_in_phase3: ...'"
                ),
            }
        )
    if kind == "organize":
        # Phase 4A.3 + Phase 4B：partial admission 多 item preview。
        # 契约 #1 双段 + Pattern C 双 inode 锚定 + 契约 #8 分类状态。
        # signed_token 仅锁 will_link 子集；其它状态 item 进 preview_items 给 UI
        # 显示但不入 payload，不可 confirm。
        items_in = raw_data.get("items") or []
        if not isinstance(items_in, list) or not items_in:
            return jsonify({"error": "items required for organize"}), 400

        # 4B.2: soft cap 防 user 选 /share/ 根扫出 10000 文件爆 payload
        if len(items_in) > MAX_ORGANIZE_BATCH_ITEMS:
            return jsonify(
                {
                    "error": "batch_too_large",
                    "message": f"批量整理最多 {MAX_ORGANIZE_BATCH_ITEMS} 个文件，"
                    f"请选更深子目录或减少范围",
                    "limit": MAX_ORGANIZE_BATCH_ITEMS,
                    "got": len(items_in),
                }
            ), 400

        # [code-enforced] 必须配置 MOVIES_ROOT / TV_ROOT
        org_cfg = load_organize_config()
        movies_root = (org_cfg.get("movies_root") or "").strip()
        tv_root = (org_cfg.get("tv_root") or "").strip()
        if not (movies_root and tv_root):
            return jsonify(
                {
                    "error": "organize_roots_not_configured",
                    "message": "请先在 UI 配置 MOVIES_ROOT / TV_ROOT 后再 organize",
                }
            ), 400

        # 收集 src_paths，逐 item 校验 src_path 字段存在
        src_paths: list[str] = []
        for it in items_in:
            sp = (it.get("src_path") or "").strip()
            if not sp:
                return jsonify({"error": "src_path required per item"}), 400
            src_paths.append(sp)

        # 4B.2: batch SSH stat src（一次 round-trip 而非 N 次）
        src_stat_now = _ssh_stat_paths(src_paths)

        # 4B.2: batch query metadata cache（一次 SQL IN 而非 N 次单 SELECT）
        db = get_db()
        current_stats = {
            p: {"inode": s.get("inode"), "mtime": s.get("mtime")}
            for p, s in src_stat_now.items()
            if s.get("exists")
        }
        cache_map = metadata_cache.get_many_by_path(db, src_paths, current_stats=current_stats)

        # Pass 1：算出每个 src 的 plan（如能算）+ 累积 dst paths 给 batch dst stat
        plans_by_src: dict[str, organize_svc.OrganizePlan] = {}
        plan_errors: dict[str, str] = {}
        dst_check_paths: list[str] = []
        for sp in src_paths:
            src_stat = src_stat_now.get(sp, {"exists": False})
            if not src_stat.get("exists"):
                continue
            cached, cache_status = cache_map.get(sp, (None, "miss"))
            if cached is None or cache_status == "stale":
                continue
            if cached.media_type not in ("movie", "tv"):
                continue
            try:
                plan = organize_svc.compute_organize_plan(sp, cached, movies_root, tv_root)
                plans_by_src[sp] = plan
                dst_check_paths.extend([plan.dst_dir, plan.dst_path, plan.nfo_path])
                if plan.tvshow_nfo_path:
                    dst_check_paths.append(plan.tvshow_nfo_path)
            except organize_svc.OrganizeNotApplicable as e:
                plan_errors[sp] = str(e)

        # batch SSH stat 所有 dst（一次 round-trip 而非 N×4 次）
        dst_stat = _ssh_stat_paths(dst_check_paths) if dst_check_paths else {}

        # Pass 2：对每个 src 算最终 status + 构造 plan item / preview_item。
        # 4B 设计：
        #   - **可算 plan** 的 items（will_link + already_linked + conflict）都进 payload
        #     给 executor 处理（4A 行为：executor 会按 dst 实际状态 idempotent skip / fail）
        #   - **不可算 plan** 的（needs_identify / unsupported / not_applicable）只进
        #     preview_items 给 UI 显示，不签名、不进 payload
        # batch-level duplicate dst_path detection：同 batch 两 src → 同 dst_path
        # → 第一个 will_link，后续 conflict (reason=duplicate_dst_path_within_batch)
        seen_dst_paths: set[str] = set()
        preview_items: list[dict] = []
        payload_items: list[dict] = []
        counts = {
            "will_link": 0,
            "already_linked": 0,
            "conflict": 0,
            "needs_identify": 0,
            "unsupported": 0,
            "not_applicable": 0,
        }

        for sp in src_paths:
            src_stat = src_stat_now.get(sp, {"exists": False})
            cached, cache_status = cache_map.get(sp, (None, "miss"))

            base_item = {
                "src_path": sp,
                "name": sp.rsplit("/", 1)[-1],
            }

            # src 不存在
            if not src_stat.get("exists"):
                preview_items.append(
                    {
                        **base_item,
                        "status": "not_applicable",
                        "reason": "src_missing",
                    }
                )
                counts["not_applicable"] += 1
                continue

            # cache miss / stale
            if cached is None or cache_status == "stale":
                preview_items.append(
                    {
                        **base_item,
                        "status": "needs_identify",
                        "reason": "stale_cache" if cache_status == "stale" else "no_cache",
                    }
                )
                counts["needs_identify"] += 1
                continue

            # 不支持的 media_type（extra / part / unknown）
            if cached.media_type not in ("movie", "tv"):
                preview_items.append(
                    {
                        **base_item,
                        "status": "unsupported",
                        "media_type": cached.media_type,
                        "title": cached.title,
                        "reason": f"media_type={cached.media_type!r}",
                    }
                )
                counts["unsupported"] += 1
                continue

            # compute_plan 抛 OrganizeNotApplicable
            if sp in plan_errors:
                preview_items.append(
                    {
                        **base_item,
                        "status": "not_applicable",
                        "media_type": cached.media_type,
                        "title": cached.title,
                        "year": cached.year,
                        "reason": plan_errors[sp],
                    }
                )
                counts["not_applicable"] += 1
                continue

            plan = plans_by_src[sp]
            dst_now = dst_stat.get(plan.dst_path, {"exists": False})
            src_inode = src_stat.get("inode")
            already_linked = bool(dst_now.get("exists") and dst_now.get("inode") == src_inode)
            dst_conflict_with_fs = bool(dst_now.get("exists") and not already_linked)

            # batch 内 dst 重名检测
            duplicate_in_batch = plan.dst_path in seen_dst_paths
            if not duplicate_in_batch:
                seen_dst_paths.add(plan.dst_path)

            # 算状态
            if duplicate_in_batch:
                item_status = "conflict"
                conflict_reason = "duplicate_dst_path_within_batch"
            elif already_linked:
                item_status = "already_linked"
                conflict_reason = None
            elif dst_conflict_with_fs:
                item_status = "conflict"
                conflict_reason = "dst_exists_different_inode"
            else:
                item_status = "will_link"
                conflict_reason = None

            counts[item_status] += 1

            # payload item（含全部 4A executor 需要的 snapshot + plan）
            plan_item = {
                "src_path": sp,
                "src_snapshot": {
                    "inode": src_stat.get("inode"),
                    "size_bytes": src_stat.get("size_bytes"),
                    "mtime": src_stat.get("mtime"),
                },
                "media_type": plan.media_type,
                "tmdb_id": plan.tmdb_id,
                "title": plan.title,
                "year": plan.year,
                "season_number": plan.season_number,
                "episode_number": plan.episode_number,
                "computed_plan": {
                    "dst_dir": plan.dst_dir,
                    "dst_path": plan.dst_path,
                    "nfo_path": plan.nfo_path,
                    "tvshow_nfo_path": plan.tvshow_nfo_path,
                },
                # codex I1: NFO 内容按 preview 时的 metadata 写。preview→confirm 之间
                # cache 若被 re-identify 改了，confirm 用此 snapshot 跟 cache 重读对比，
                # 不一致 → skip NFO (status='skipped: cache_drift')。
                "metadata_snapshot": {
                    "tmdb_id": plan.tmdb_id,
                    "title": plan.title,
                    "year": plan.year,
                    "media_type": plan.media_type,
                    "season_number": plan.season_number,
                    "episode_number": plan.episode_number,
                },
                "dst_status": {
                    "dst_dir_exists": dst_stat.get(plan.dst_dir, {}).get("exists", False),
                    "dst_path_exists": dst_now.get("exists", False),
                    "nfo_path_exists": dst_stat.get(plan.nfo_path, {}).get("exists", False),
                    "tvshow_nfo_exists": (
                        dst_stat.get(plan.tvshow_nfo_path, {}).get("exists", False)
                        if plan.tvshow_nfo_path
                        else False
                    ),
                    "already_linked": already_linked,
                    "conflict": dst_conflict_with_fs or duplicate_in_batch,
                },
            }
            if duplicate_in_batch:
                plan_item["dst_status"]["conflict_reason"] = conflict_reason
            payload_items.append(plan_item)

            # preview_items 的紧凑视图（给 UI dashboard 用）
            pv = {
                **base_item,
                "status": item_status,
                "media_type": plan.media_type,
                "title": plan.title,
                "year": plan.year,
                "tmdb_id": plan.tmdb_id,
                "dst_path": plan.dst_path,
                "nfo_path": plan.nfo_path,
                "tvshow_nfo_path": plan.tvshow_nfo_path,
                "season_number": plan.season_number,
                "episode_number": plan.episode_number,
                "size_bytes": src_stat.get("size_bytes"),
            }
            if item_status == "already_linked":
                pv["shared_inode"] = src_inode
            elif item_status == "conflict":
                pv["conflict_inode"] = dst_now.get("inode")
                pv["reason"] = conflict_reason
            preview_items.append(pv)

        # 4B.2: payload_items 为空（全部 needs_identify / unsupported / not_applicable）
        # → 不签名、不落 destructive_actions row。
        if not payload_items:
            return jsonify(
                {
                    "kind": "organize",
                    "items_count": 0,
                    "preview_items": preview_items,
                    "counts": counts,
                    "message": "无可整理文件",
                }
            )

        payload = {
            "kind": "organize",
            "items": payload_items,
            "snapshot": {"captured_at": int(time.time())},
        }
        res = destructive_action.create_preview(
            db,
            kind="organize",
            payload=payload,
            server_secret=SERVER_SECRET,
            created_by="web_ui",
        )
        return jsonify(
            {
                "action_id": res.action_id,
                "signed_token": res.signed_token,
                "expires_at": res.expires_at,
                "kind": "organize",
                "items_count": len(payload_items),
                "preview_items": preview_items,
                "counts": counts,
                # 4A 兼容：单文件 organize UI 用 'items' 字段读 plan
                "items": payload_items,
            }
        )
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

    # Phase 4B.3：organize + items > ORGANIZE_BATCH_INLINE_THRESHOLD 走 background。
    # 必须先读 kind + items count，再决定路径。
    db = get_db()
    pre = db.execute(
        "SELECT kind, payload_json, payload_hash FROM destructive_actions WHERE action_id = ?",
        (action_id,),
    ).fetchone()
    if pre is None:
        return jsonify({"error": "action_not_found", "action_id": action_id}), 404

    # 4B.3 / codex r1 IMP6 + r2 NIT: selected_indices 校验（inline 和 background 都用）
    # type(i) is int 比 isinstance(i, int) 更严，排除 JSON bool（Python 中 True/False 是 int 子类）
    selected_indices_raw = data.get("selected_indices")
    selected_indices: list[int] | None = None
    if selected_indices_raw is not None:
        if not isinstance(selected_indices_raw, list) or not all(
            type(i) is int for i in selected_indices_raw
        ):
            return jsonify({"error": "selected_indices must be list[int]"}), 400
        if pre["kind"] != "organize":
            return jsonify({"error": "selected_indices only valid for organize"}), 400
        try:
            payload_for_check = json.loads(pre["payload_json"])
        except (json.JSONDecodeError, TypeError):
            return jsonify({"error": "payload_corrupted", "action_id": action_id}), 500
        items_count = len(payload_for_check.get("items", []))
        if any(i < 0 or i >= items_count for i in selected_indices_raw):
            return jsonify(
                {
                    "error": "selected_indices_out_of_range",
                    "valid_range": [0, items_count - 1],
                }
            ), 400
        selected_indices = selected_indices_raw

    if pre["kind"] == "organize":
        try:
            payload = json.loads(pre["payload_json"])
        except (json.JSONDecodeError, TypeError):
            return jsonify({"error": "payload_corrupted", "action_id": action_id}), 500
        items = payload.get("items", [])
        if len(items) > ORGANIZE_BATCH_INLINE_THRESHOLD:
            # Background path: 手动 consume + verify token + start worker，返 202
            # Stage 1: atomic consume
            row = destructive_action._atomic_consume(
                db, action_id, pre["payload_hash"], int(time.time())
            )
            if row is None:
                # 重查精确原因
                cur = db.execute(
                    "SELECT status, consumed_at, expires_at FROM destructive_actions "
                    "WHERE action_id = ?",
                    (action_id,),
                ).fetchone()
                if cur is None:
                    return jsonify({"error": "action_not_found"}), 404
                if cur["consumed_at"] is not None:
                    return jsonify({"error": "action_already_consumed"}), 409
                if cur["expires_at"] <= int(time.time()):
                    return jsonify({"error": "action_expired"}), 410
                return jsonify({"error": "action_state_invalid"}), 400

            # Stage 2: HMAC 验签
            if not destructive_action._verify_token(
                SERVER_SECRET, action_id, pre["payload_hash"], signed_token
            ):
                destructive_action._rollback_to_pending(db, action_id)
                return jsonify({"error": "invalid_signed_token"}), 401

            # Stage 3: 起 background worker
            # codex r1 NIT1: ConcurrentOrganizeError → rollback_to_pending 不消耗 row
            # codex r1 IMP1: broad Exception catch — 任何 start failure 都不让 row 卡 reaper
            try:
                organize_runner.start_organize_executor(
                    db_path=DB_PATH,
                    action_id=action_id,
                    payload=payload,
                    execute_one_item=_organize_executor_one_item_threadsafe,
                    selected_indices=selected_indices,
                )
            except organize_runner.ConcurrentOrganizeError:
                # 没启 worker，user 可以稍后 retry；回滚 row 到 pending 不浪费 preview
                destructive_action._rollback_to_pending(db, action_id)
                return jsonify(
                    {
                        "error": "another_organize_running",
                        "active_action_id": organize_runner.get_active_action_id(),
                        "hint": "另一个 organize 在跑；中止它后重试当前 preview 即可。",
                    }
                ), 409
            except Exception as e:
                logger.exception(f"[action/confirm] start_organize_executor failed for {action_id}")
                destructive_action._mark_terminal(
                    db,
                    action_id,
                    status="failed",
                    result=None,
                    error=f"worker_start_failed: {type(e).__name__}: {e}",
                )
                return jsonify(
                    {
                        "error": "worker_start_failed",
                        "detail": f"{type(e).__name__}: {e}",
                    }
                ), 500

            return jsonify(
                {
                    "action_id": action_id,
                    "status": "running",
                    "items_total": len(items),
                    "polling_url": f"/api/action/status?id={action_id}",
                }
            ), 202

    # Default path: inline confirm（Phase 4A 行为；4B 加 selected_indices 闭包绑定）
    # codex r1 BLOCKER 1: inline 路径也尊重 selected_indices（≤5 item 用户可勾子集）
    def _wrapped_executor(pl: dict) -> dict:
        if pl.get("kind") == "organize":
            return _organize_executor(pl, selected_indices=selected_indices)
        return _route_executor_by_kind(pl)

    try:
        out = destructive_action.confirm(
            db,
            action_id=action_id,
            signed_token=signed_token,
            server_secret=SERVER_SECRET,
            executor=_wrapped_executor,
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

    response: dict = {"action_id": out.action_id, "status": out.status}
    if out.status == "succeeded":
        response["result"] = out.result
    else:
        response["error"] = out.error
        if out.error and out.error.startswith("SnapshotMismatch:"):
            response["status"] = "target_already_changed"
            response["hint"] = "Target changed since preview; refresh and retry."
    return jsonify(response)


@app.route("/api/action/status", methods=["GET"])
@require_token
def action_status():
    """Phase 4B.3：polling endpoint。给前端 background organize 进度。

    Query: ?id=<action_id>
    Returns: {action_id, kind, status, items_total, items_completed, current_item,
              status_counts, result, error, recovery_hint, ...}
    """
    action_id = request.args.get("id", "").strip()
    if not action_id:
        return jsonify({"error": "id required"}), 400
    info = organize_runner.get_organize_status(get_db(), action_id)
    if info is None:
        return jsonify({"error": "action_not_found"}), 404
    return jsonify(info)


@app.route("/api/action/abort", methods=["POST"])
@require_token
def action_abort():
    """Phase 4B.3：中止 background organize。

    POST {"action_id": "..."}
    设 abort 标志位 — worker 下一个 item 边界自然退出（不取消正在跑的 SSH）。
    已完成的 items 保留，未完成的 items 标 'skipped_by_abort'。
    """
    data = request.json or {}
    action_id = (data.get("action_id") or "").strip()
    if not action_id:
        return jsonify({"error": "action_id required"}), 400

    info = organize_runner.get_organize_status(get_db(), action_id)
    if info is None:
        return jsonify({"error": "action_not_found"}), 404
    if info["status"] != "running":
        return jsonify(
            {
                "error": "action_not_running",
                "current_status": info["status"],
            }
        ), 409

    organize_runner.request_abort(action_id)
    logger.info(f"[action/abort] requested for {action_id}")
    return jsonify({"ok": True, "action_id": action_id, "status": "abort_requested"})


# ─────────────────────────────────────────────────────────────
# 手工恢复面板：列出 needs_manual_recovery / running 的 action
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
# Phase 2: 媒体元数据识别（TMDB）
# ─────────────────────────────────────────────────────────────


@app.route("/api/config/tmdb", methods=["GET"])
@require_token
def get_tmdb_config():
    return jsonify({"has_key": bool(load_tmdb_key())})


@app.route("/api/config/tmdb", methods=["POST"])
@require_token
def set_tmdb_config():
    data = request.json or {}
    key = (data.get("api_key") or "").strip()
    save_tmdb_key(key)
    return jsonify({"ok": True, "has_key": bool(key)})


@app.route("/api/config/deepseek", methods=["GET"])
@require_token
def get_deepseek_config():
    return jsonify({"has_key": bool(load_deepseek_key())})


@app.route("/api/config/deepseek", methods=["POST"])
@require_token
def set_deepseek_config():
    data = request.json or {}
    key = (data.get("api_key") or "").strip()
    save_deepseek_key(key)
    return jsonify({"ok": True, "has_key": bool(key)})


@app.route("/api/config/deepseek/test", methods=["POST"])
@require_token
def test_deepseek_config():
    """临时 key 验证：body 里传 api_key 直接测；不传用现有 key。
    用一个最小 message 测真实 SDK 调用，验证 key + 网络通畅。"""
    data = request.json or {}
    key = (data.get("api_key") or "").strip() or load_deepseek_key()
    if not key:
        return jsonify({"ok": False, "message": "no key configured"}), 400
    try:
        import openai

        client = openai.OpenAI(api_key=key, base_url=llm.DEFAULT_BASE_URL, timeout=10)
        resp = client.chat.completions.create(
            model=llm.DEFAULT_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": "Reply with just: OK"}],
        )
        text = (resp.choices[0].message.content or "").strip() if resp.choices else ""
        return jsonify({"ok": True, "model": llm.DEFAULT_MODEL, "sample": text[:50]})
    except ImportError:
        return jsonify({"ok": False, "message": "openai SDK not installed"}), 500
    except Exception as e:
        return jsonify({"ok": False, "message": f"{type(e).__name__}: {e}"}), 200


@app.route("/api/config/tmdb/test", methods=["POST"])
@require_token
def test_tmdb_config():
    """临时 key 验证：body 里传 api_key 直接测，不落盘；不传则用现有 key。"""
    data = request.json or {}
    key = (data.get("api_key") or "").strip() or load_tmdb_key()
    if not key:
        return jsonify({"ok": False, "message": "no key configured"}), 400
    try:
        result = TMDBProvider(api_key=key).test_connection()
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 200
    return jsonify(result)


# Providers status：聚合 TMDB / DeepSeek / Emby 当前可用性，60s TTL cache
# state 枚举：ok / auth_failed / not_configured / unreachable
_PROVIDERS_STATUS_CACHE: dict = {"data": None, "checked_at": 0.0}
# Flask 请求线程与 APScheduler cron 线程会并发读写这个 dict；无锁时可能读到
# "新 data + 旧 checked_at" 的不一致组合（age 计算错误）。
_PROVIDERS_STATUS_LOCK = threading.Lock()
_PROVIDERS_STATUS_TTL = 60.0


def _classify_provider_error(message: str) -> str:
    """从下游 test_connection 的 message 推断 state。"""
    m = (message or "").lower()
    if any(s in m for s in ("401", "403", "auth", "invalid api key", "unauthor")):
        return "auth_failed"
    return "unreachable"


def _probe_tmdb() -> dict:
    key = load_tmdb_key()
    if not key:
        return {"state": "not_configured", "message": "TMDB key 未配置"}
    try:
        r = TMDBProvider(api_key=key).test_connection()
    except Exception as e:
        return {"state": _classify_provider_error(str(e)), "message": str(e)}
    if r.get("ok"):
        return {"state": "ok", "message": r.get("message") or "TMDB OK"}
    return {
        "state": _classify_provider_error(r.get("message", "")),
        "message": r.get("message") or "unknown",
    }


def _probe_deepseek() -> dict:
    key = load_deepseek_key()
    if not key:
        return {"state": "not_configured", "message": "DeepSeek key 未配置"}
    try:
        import openai

        client = openai.OpenAI(api_key=key, base_url=llm.DEFAULT_BASE_URL, timeout=10)
        client.chat.completions.create(
            model=llm.DEFAULT_MODEL,
            max_tokens=5,
            messages=[{"role": "user", "content": "ping"}],
        )
        return {"state": "ok", "message": f"DeepSeek OK ({llm.DEFAULT_MODEL})"}
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        return {"state": _classify_provider_error(msg), "message": msg}


def _probe_emby() -> dict:
    client = _emby_client()
    if client is None:
        return {"state": "not_configured", "message": "Emby 未配置"}
    try:
        r = client.test_connection()
    except Exception as e:
        return {"state": _classify_provider_error(str(e)), "message": str(e)}
    if r.get("ok"):
        return {"state": "ok", "message": r.get("message") or "Emby OK"}
    code = (r.get("code") or "").lower()
    if code == "auth_failed":
        return {"state": "auth_failed", "message": r.get("message") or "auth failed"}
    return {
        "state": _classify_provider_error(r.get("message", "")),
        "message": r.get("message") or "unknown",
    }


def _probe_qbit() -> dict:
    """qBit WebUI 探活：未配置密码 → not_configured；login fail → auth_failed；其他 → unreachable。

    QBitClient.test_connection() 返 {status: 'ok' | 'error', message?}（schema 与 TMDB
    不同），所以这里做一层适配映射成 providers/status 统一的 {state, message}。
    """
    try:
        cfg = qbit.get_config()
    except Exception as e:
        return {"state": "error", "message": f"config read failed: {e}"}
    if not cfg.get("url") or not cfg.get("user"):
        return {"state": "not_configured", "message": "qBit URL/user 未配置"}
    if not cfg.get("has_password"):
        return {"state": "not_configured", "message": "qBit 密码未配置"}
    try:
        r = qbit.test_connection()
    except Exception as e:
        return {"state": _classify_provider_error(str(e)), "message": str(e)}
    if r.get("status") == "ok":
        return {"state": "ok", "message": f"qBit OK ({r.get('torrent_count', 0)} torrents)"}
    msg = r.get("message", "")
    return {"state": _classify_provider_error(msg), "message": msg or "unknown"}


def _compute_providers_status() -> dict:
    now = time.time()
    return {
        "tmdb": {**_probe_tmdb(), "checked_at": now},
        "deepseek": {**_probe_deepseek(), "checked_at": now},
        "emby": {**_probe_emby(), "checked_at": now},
        "qbit": {**_probe_qbit(), "checked_at": now},
    }


def _providers_status_with_meta(force: bool = False) -> tuple[dict, bool, float]:
    """Provider status with 60s TTL cache. 返 (data, was_cached, age_seconds).

    锁覆盖整个 check-then-compute：并发 cache miss 时只有第一个线程真探活，
    其余线程等锁释放后直接命中缓存——这正是本 cache "多 tab × 60s poll 不
    放大 DeepSeek 探活调用" 的设计意图。代价是 force 刷新期间（探活最长
    ~10s/provider）其他 status 请求会阻塞等待，对单用户工具可接受。
    """
    with _PROVIDERS_STATUS_LOCK:
        cache = _PROVIDERS_STATUS_CACHE
        age = time.time() - cache["checked_at"]
        if not force and cache["data"] is not None and age < _PROVIDERS_STATUS_TTL:
            return cache["data"], True, age
        data = _compute_providers_status()
        cache["data"] = data
        cache["checked_at"] = time.time()
        return data, False, 0.0


def get_cached_providers_status(force: bool = False) -> dict:
    """Provider status with 60s TTL cache.

    Shared by /api/providers/status (JSON) and /ui/status/providers (HTML)
    so multi-tab × 60s poll does NOT multiply DeepSeek probe calls.
    """
    data, _, _ = _providers_status_with_meta(force=force)
    return data


@app.route("/api/providers/status", methods=["GET"])
@require_token
def providers_status():
    """聚合 TMDB / DeepSeek / Emby / qBit 当前可用性。

    query ?refresh=1 跳过 cache 强制 re-probe；否则 60s TTL cache。
    每个 provider 真实跑一次 test_connection（TMDB /configuration、Emby
    System/Info/Public 是免费的；DeepSeek 一次 5-token chat completion；
    qBit 走 WebUI /api/v2/auth/login + /api/v2/torrents/info）。
    """
    force = request.args.get("refresh") in ("1", "true", "yes")
    data, was_cached, age = _providers_status_with_meta(force=force)
    resp = jsonify({"providers": data, "cached": was_cached, "age": round(age, 1)})
    resp.headers["Cache-Control"] = "no-store"
    return resp


VIDEO_EXTS = ["mkv", "mp4", "avi", "mov", "ts", "m4v", "mpg", "wmv", "flv", "webm", "m2ts", "rmvb"]

# 排除原盘镜像内部目录：BDMV (Blu-ray) 和 VIDEO_TS (DVD) 子树里的 .m2ts/.vob
# 不该作为独立媒体识别——一个 Blu-ray 镜像就有几十个 m2ts 文件，全去 TMDB 搜会
# 把识别结果污染成同一个 tmdb_id 的多个误命中（看真实扫库数据：968 个 movie 文件
# 里 ~50 个是 BDMV/STREAM/*.m2ts，全被错误 group 到几个 tmdb_id 上）。
_PATH_EXCLUDES = ["*/BDMV/*", "*/VIDEO_TS/*", "*/CERTIFICATE/*", "*/AUXDATA/*"]


def _list_video_paths(path: str, max_depth: int = 2, limit: int | None = 200) -> list[str]:
    """SSH find 视频文件路径，按 max_depth 递归。limit=None → 不截断（全库扫描用）。

    自动排除 Blu-ray / DVD 原盘镜像内部目录（BDMV / VIDEO_TS / CERTIFICATE / AUXDATA）。

    输入 path 自动通过 path_resolver 翻译 QNAP /share/<alias> symlink 到
    canonical /share/CACHEDEV2_DATA/<alias>，保证 find 输出 (及其 caller — 比如
    auto-organize cron 拿来查 metadata cache) 跟扫库写入 media_files 的 path 同名空间。
    """
    canonical_path = path_resolver.resolve(path, ssh_exec)
    safe_path = shlex.quote(canonical_path)
    iname_clauses = " -o ".join(f"-iname '*.{ext}'" for ext in VIDEO_EXTS)
    not_path = " ".join(f"-not -path {shlex.quote(p)}" for p in _PATH_EXCLUDES)
    cap = f"| head -n {limit + 1}" if limit else ""
    cmd = (
        f"find {safe_path} -maxdepth {max_depth} -type f {not_path} "
        f"\\( {iname_clauses} \\) 2>/dev/null {cap}"
    )
    _, out, _ = ssh_exec(cmd, timeout=120)
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


@app.route("/api/metadata/list-videos", methods=["GET"])
@require_token
def metadata_list_videos():
    """列出目录里所有视频文件（含子目录递归 N 层），供批量识别用。

    Query: ?path=<dir>&max_depth=2&limit=200
    Returns: {videos: [{path, name, size_bytes, has_nfo}], total, truncated}
    """
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path required"}), 400
    try:
        path = validate_path(path)
    except Exception:
        return jsonify({"error": f"invalid path: {path}"}), 400
    max_depth = max(1, min(5, int(request.args.get("max_depth", "2"))))
    limit = max(1, min(500, int(request.args.get("limit", "200"))))

    raw_paths = _list_video_paths(path, max_depth=max_depth, limit=limit)
    truncated = len(raw_paths) > limit
    raw_paths = raw_paths[:limit]
    video_exts = VIDEO_EXTS  # 兼容下面循环用
    if not raw_paths:
        return jsonify({"videos": [], "total": 0, "truncated": False})

    # 批量拿大小 + 同目录 .nfo 存在性（一次 SSH 完成）
    stats = _ssh_stat_paths(raw_paths)
    # 对每个视频文件，检查同目录同名 .nfo
    # 例如 /share/a/show.mkv → /share/a/show.nfo
    nfo_paths = []
    for p in raw_paths:
        if p.lower().endswith(tuple(f".{ext}" for ext in video_exts)):
            # 去最后一个 . 之前的部分加 .nfo
            base = p.rsplit(".", 1)[0]
            nfo_paths.append(base + ".nfo")
    nfo_stats = _ssh_stat_paths(nfo_paths) if nfo_paths else {}

    videos = []
    for p in raw_paths:
        st = stats.get(p, {})
        nfo_path = p.rsplit(".", 1)[0] + ".nfo"
        has_nfo = nfo_stats.get(nfo_path, {}).get("exists", False)
        videos.append(
            {
                "path": p,
                "name": p.rsplit("/", 1)[-1],
                "size_bytes": st.get("size_bytes", 0),
                "size_human": human_size(st.get("size_bytes", 0)),
                "has_nfo": has_nfo,
            }
        )

    return jsonify(
        {
            "videos": videos,
            "total": len(videos),
            "truncated": truncated,
        }
    )


@app.route("/api/organize/dir-preview", methods=["GET"])
@require_token
def organize_dir_preview():
    """Phase 4B：批量目录 organize 第一步 dashboard。

    扫描目录里所有视频文件，按 organize 可行性分类。只读 — 不签名、
    不落 destructive_actions row。

    Query: ?path=<dir>&max_depth=2&limit=500

    Returns:
      {
        base_path, max_depth, limit_reached, total,
        counts: {will_link, already_linked, conflict, needs_identify, unsupported, not_applicable},
        items: [{path, name, size_bytes, size_human, status, title?, year?, media_type?,
                 tmdb_id?, confidence?, dst_path?, reason?, season_number?, episode_number?}, ...]
      }
    """
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path required"}), 400
    try:
        path = validate_path(path)
    except Exception:
        return jsonify({"error": f"invalid path: {path}"}), 400

    max_depth = max(1, min(5, int(request.args.get("max_depth", "2"))))
    limit = max(1, min(MAX_ORGANIZE_BATCH_ITEMS, int(request.args.get("limit", "500"))))

    # organize roots 必须配（不然 plan 算不出 dst_path）
    org_cfg = load_organize_config()
    movies_root = (org_cfg.get("movies_root") or "").strip()
    tv_root = (org_cfg.get("tv_root") or "").strip()
    if not (movies_root and tv_root):
        return jsonify(
            {
                "error": "organize_roots_not_configured",
                "message": "请先在 UI 配置 MOVIES_ROOT / TV_ROOT 后再批量整理",
            }
        ), 400

    raw_paths = _list_video_paths(path, max_depth=max_depth, limit=limit)
    limit_reached = len(raw_paths) > limit
    raw_paths = raw_paths[:limit]

    counts = {
        "will_link": 0,
        "already_linked": 0,
        "conflict": 0,
        "needs_identify": 0,
        "unsupported": 0,
        "not_applicable": 0,
    }
    if not raw_paths:
        return jsonify(
            {
                "base_path": path,
                "max_depth": max_depth,
                "limit_reached": False,
                "counts": counts,
                "items": [],
                "total": 0,
            }
        )

    # 批量 SSH stat 所有 src（一次 round-trip）
    src_stats = _ssh_stat_paths(raw_paths)

    # 批量查 cache（一次 SQL IN）
    db = get_db()
    current_stats = {
        p: {"inode": s.get("inode"), "mtime": s.get("mtime")}
        for p, s in src_stats.items()
        if s.get("exists")
    }
    cache_map = metadata_cache.get_many_by_path(db, raw_paths, current_stats=current_stats)

    # compute plan + 累积 dst paths
    plans_by_path: dict[str, organize_svc.OrganizePlan] = {}
    plan_errors: dict[str, str] = {}
    dst_stat_paths: list[str] = []
    for p in raw_paths:
        cached, cache_status = cache_map.get(p, (None, "miss"))
        if cached is None or cache_status == "stale":
            continue
        if cached.media_type not in ("movie", "tv"):
            continue
        try:
            plan = organize_svc.compute_organize_plan(p, cached, movies_root, tv_root)
            plans_by_path[p] = plan
            dst_stat_paths.append(plan.dst_path)
        except organize_svc.OrganizeNotApplicable as e:
            plan_errors[p] = str(e)

    # 批量 stat 所有 dst（一次 round-trip）
    dst_stats = _ssh_stat_paths(dst_stat_paths) if dst_stat_paths else {}

    items = []
    for p in raw_paths:
        src_stat = src_stats.get(p, {"exists": False})
        cached, cache_status = cache_map.get(p, (None, "miss"))
        name = p.rsplit("/", 1)[-1]
        size_bytes = src_stat.get("size_bytes", 0)
        item: dict = {
            "path": p,
            "name": name,
            "size_bytes": size_bytes,
            "size_human": human_size(size_bytes),
        }

        # 源不存在（race / scanner cache 滞后）
        if not src_stat.get("exists"):
            item["status"] = "not_applicable"
            item["reason"] = "src_missing"
            counts["not_applicable"] += 1
            items.append(item)
            continue

        # cache miss / stale → 需要先识别
        if cached is None or cache_status == "stale":
            item["status"] = "needs_identify"
            item["reason"] = "stale_cache" if cache_status == "stale" else "no_cache"
            counts["needs_identify"] += 1
            items.append(item)
            continue

        # 已识别但不支持 organize（extra / part / unknown）
        if cached.media_type not in ("movie", "tv"):
            item["status"] = "unsupported"
            item["media_type"] = cached.media_type
            item["title"] = cached.title
            item["reason"] = f"media_type={cached.media_type!r}"
            counts["unsupported"] += 1
            items.append(item)
            continue

        # compute_plan 抛 OrganizeNotApplicable（如 tv 缺 season/episode）
        if p in plan_errors:
            item["status"] = "not_applicable"
            item["media_type"] = cached.media_type
            item["title"] = cached.title
            item["year"] = cached.year
            item["reason"] = plan_errors[p]
            counts["not_applicable"] += 1
            items.append(item)
            continue

        # 算出 plan → 查 dst 状态
        plan = plans_by_path[p]
        item["media_type"] = plan.media_type
        item["title"] = plan.title
        item["year"] = plan.year
        item["tmdb_id"] = plan.tmdb_id
        item["confidence"] = cached.metadata_confidence  # 给 Phase 4C 用
        item["dst_path"] = plan.dst_path
        if plan.media_type == "tv":
            item["season_number"] = plan.season_number
            item["episode_number"] = plan.episode_number

        dst_stat = dst_stats.get(plan.dst_path, {"exists": False})
        src_inode = src_stat.get("inode")
        if dst_stat.get("exists"):
            if dst_stat.get("inode") == src_inode:
                item["status"] = "already_linked"
                counts["already_linked"] += 1
            else:
                item["status"] = "conflict"
                item["conflict_inode"] = dst_stat.get("inode")
                counts["conflict"] += 1
        else:
            item["status"] = "will_link"
            counts["will_link"] += 1
        items.append(item)

    return jsonify(
        {
            "base_path": path,
            "max_depth": max_depth,
            "limit_reached": limit_reached,
            "counts": counts,
            "items": items,
            "total": len(items),
        }
    )


@app.route("/api/metadata/identify", methods=["POST"])
@require_token
def metadata_identify():
    """识别单个文件：返回 guessit 解析 + TMDB 候选 + top_pick。

    body: {"path": "/share/..."}
    """
    data = request.json or {}
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "path required"}), 400
    # QNAP 别名路径 (/share/downloads/...) 先翻 canonical，否则被沙箱字面前缀拒。
    # 复用 _list_video_paths 已有写法（path_resolver.resolve，safe fallback 永不抛）。
    path = path_resolver.resolve(path, ssh_exec)
    try:
        path = validate_path(path)
    except Exception:
        return jsonify({"error": f"invalid path: {path}"}), 400

    provider = get_tmdb_provider()
    if provider is None:
        # 仅文件名解析，不查 provider
        parse = identify_svc.parse_filename(path)
        return jsonify(
            {
                "parse": {
                    "raw_name": parse.raw_name,
                    "title": parse.title,
                    "year": parse.year,
                    "season": parse.season,
                    "episode": parse.episode,
                    "episode_title": parse.episode_title,
                    "media_type": parse.media_type,
                    "resolution": parse.resolution,
                    "source": parse.source,
                    "release_group": parse.release_group,
                },
                "candidates": [],
                "top_pick": None,
                "confidence": 0.0,
                "reasoning": "TMDB API key not configured. Set it in settings to enable metadata lookup.",
                "provider_state": "not_configured",
            }
        )

    try:
        result = identify_svc.identify(path, provider, llm_api_key=load_deepseek_key() or None)
    except ProviderUnavailable as e:
        # bug #12: TMDB 瞬时不可用（429/网络/5xx）≠ 没结果。返 503 retryable，
        # 不让前端/调用方把它当 "no candidates" 缓存成 needs_review。
        return jsonify({"error": "provider_unavailable", "detail": str(e), "retryable": True}), 503

    response = {
        "parse": {
            "raw_name": result.parse.raw_name,
            "title": result.parse.title,
            "year": result.parse.year,
            "season": result.parse.season,
            "episode": result.parse.episode,
            "episode_title": result.parse.episode_title,
            "media_type": result.parse.media_type,
            "resolution": result.parse.resolution,
            "source": result.parse.source,
            "release_group": result.parse.release_group,
        },
        "candidates": [_candidate_to_dict(c) for c in result.candidates],
        "top_pick": _candidate_to_dict(result.top_pick) if result.top_pick else None,
        "confidence": result.confidence,
        "reasoning": result.reasoning,
        "pick_source": result.pick_source,
        "llm_configured": bool(load_deepseek_key()),
        "provider_state": "ok",
    }

    # tv 类型且有 top_pick → 顺手把单集详情也带上（episode title / overview / still）
    if (
        result.top_pick
        and result.parse.media_type == "episode"
        and result.top_pick.media_type == "tv"
    ):
        tmdb_id = result.top_pick.external_ids.get("tmdb_id")
        if tmdb_id:
            details = provider.lookup_by_id(
                tmdb_id,
                media_type="tv",
                season=result.parse.season,
                episode=result.parse.episode,
            )
            if details:
                response["details"] = {
                    "episode": details.episode,
                    "cast": details.cast,
                    "genres": details.genres,
                }
    elif result.top_pick and result.parse.media_type == "movie":
        tmdb_id = result.top_pick.external_ids.get("tmdb_id")
        if tmdb_id:
            details = provider.lookup_by_id(tmdb_id, media_type="movie")
            if details:
                response["details"] = {
                    "cast": details.cast,
                    "genres": details.genres,
                    "runtime_minutes": details.runtime_minutes,
                }

    # 持久化到 media_files：以 path 为键 upsert。成功 / needs_review 都写入
    # （让"已尝试过"被记录，避免下次 detail 面板再点白白浪费 API）。
    try:
        stat_map = _ssh_stat_paths([path])
        stat = stat_map.get(path, {})
        if stat.get("exists"):
            db = get_db()
            metadata_cache.upsert_identification(
                db,
                path=path,
                stat=stat,
                identify_result=result,
            )
            # 把 lookup_by_id 拿到的 details 补丁式写入（不覆盖核心字段）
            d = response.get("details") or {}
            ep = d.get("episode") or {}
            metadata_cache.upsert_details(
                db,
                path=path,
                genres=d.get("genres") or None,
                cast=d.get("cast") or None,
                runtime_minutes=d.get("runtime_minutes"),
                episode_air_date=ep.get("air_date"),
                episode_overview=ep.get("overview"),
                episode_still_url=ep.get("still_url"),
            )
            response["cached"] = True
    except Exception as e:
        # cache 写失败不影响识别 response 返回（best-effort 持久化）
        logger.warning(f"[metadata_cache] upsert failed for {path}: {e}")

    return jsonify(response)


@app.route("/api/metadata/bind", methods=["POST"])
@require_token
def metadata_bind():
    """ROADMAP #1: 用户从候选列表手动选一个 tmdb_id 强绑（pick_source='manual'）。

    needs_review / heuristic 低 confidence 时 UI 显示候选列表 → 用户点一条 → 调本 route
    → lookup_by_id 拉权威详情 + 强写 cache (metadata_status='ok', confidence=1.0)。

    body: {
        "path": "/share/...",          # required
        "tmdb_id": "60625",            # required
        "media_type": "movie"|"tv",    # required
        "season": 6,                   # optional override (tv only;不传用 guessit parse)
        "episode": 2,                  # optional override
    }

    返回 (200): {bound: true, cached: <CachedMetadata dict>}
    错误: 400 invalid input / 404 file not found / 500 lookup failed
    """
    data = request.json or {}
    path_in = (data.get("path") or "").strip()
    tmdb_id = str(data.get("tmdb_id") or "").strip()
    media_type = (data.get("media_type") or "").strip()
    if not (path_in and tmdb_id and media_type in ("movie", "tv")):
        return jsonify({"error": "path/tmdb_id/media_type ∈ {movie,tv} required"}), 400
    if not re.fullmatch(r"[0-9]+", tmdb_id):
        # SDK 路径拼接 SSRF 防护（external-tools.md SDK URL composition rule）
        return jsonify({"error": "tmdb_id must be numeric"}), 400

    try:
        path = validate_path(path_in)
    except Exception:
        return jsonify({"error": f"invalid path: {path_in}"}), 400

    # season/episode 纯输入校验（不依赖 provider / SSH） — 提到 provider 检查之前
    # codex r1 BLOCKER: 跟 tmdb_id 同样的 SDK path injection 攻击面 —
    # season/episode 直接进 lookup_by_id → 拼到 `/tv/{id}/season/{N}/episode/{N}` URL
    # 非 int / bool / 负数 / 超大值 → 400 拒掉
    season_arg = data.get("season")
    episode_arg = data.get("episode")
    for label, val in (("season", season_arg), ("episode", episode_arg)):
        if val is None:
            continue
        if isinstance(val, bool) or not isinstance(val, int) or val < 0 or val > 9999:
            return jsonify(
                {
                    "error": f"{label} must be a non-negative integer ≤ 9999",
                }
            ), 400

    provider = get_tmdb_provider()
    if provider is None:
        return jsonify({"error": "TMDB API key not configured"}), 400

    # SSH stat 确认文件还在（防 cache write 错位到不存在文件）
    stat_map = _ssh_stat_paths([path])
    stat = stat_map.get(path, {})
    if not stat.get("exists"):
        return jsonify({"error": "src_missing", "path": path}), 404

    # season/episode override：优先 body 传入，否则用 guessit parse
    parse = identify_svc.parse_filename(path)
    if media_type == "tv":
        if season_arg is None:
            season_arg = parse.season
        if episode_arg is None:
            episode_arg = parse.episode

    # lookup_by_id 拉权威详情（cast/genres/runtime/episode）
    try:
        details = provider.lookup_by_id(
            tmdb_id,
            media_type=media_type,
            season=season_arg,
            episode=episode_arg,
        )
    except Exception as e:
        logger.warning(f"[metadata_bind] lookup failed {tmdb_id}: {e}")
        return jsonify({"error": f"tmdb_lookup_failed: {type(e).__name__}: {e}"}), 502
    if details is None:
        return jsonify({"error": "tmdb_id_not_found", "tmdb_id": tmdb_id}), 404

    cand = details.candidate

    # 构造 IdentifyResult — 复用 upsert_identification 写 cache 通道：
    # parse 用真实 guessit (保留 quality / parse_raw_name 等);
    # 但 media_type / season / episode 用 user 选的（强 override 在 IdentifyResult 之外，
    # 走 parse 字段不行因 FilenameParse frozen，直接构造 new instance）。
    bound_parse = identify_svc.FilenameParse(
        raw_name=parse.raw_name,
        title=cand.title,  # 用 cand 权威 title 而非 guessit
        year=cand.year or parse.year,
        season=season_arg if media_type == "tv" else None,
        episode=episode_arg if media_type == "tv" else None,
        episode_title=parse.episode_title,
        media_type=media_type,  # 强写 movie/tv
        resolution=parse.resolution,
        source=parse.source,
        release_group=parse.release_group,
        codec=parse.codec,
        color_depth=parse.color_depth,
        hdr_profiles=parse.hdr_profiles,
        container=parse.container,
        audio_codec=parse.audio_codec,
        raw=parse.raw,
    )
    bound_result = identify_svc.IdentifyResult(
        parse=bound_parse,
        candidates=[cand],
        top_pick=cand,
        confidence=1.0,  # manual = full confidence
        reasoning="manual binding by user",
        pick_source="manual",
    )

    try:
        db = get_db()
        metadata_cache.upsert_identification(
            db,
            path=path,
            stat=stat,
            identify_result=bound_result,
        )
        # 写 details（cast/genres/runtime + episode_*）
        ep = details.episode or {}
        metadata_cache.upsert_details(
            db,
            path=path,
            genres=details.genres or None,
            cast=details.cast or None,
            runtime_minutes=details.runtime_minutes,
            episode_air_date=ep.get("air_date"),
            episode_overview=ep.get("overview"),
            episode_still_url=ep.get("still_url"),
        )
    except Exception as e:
        logger.exception(f"[metadata_bind] cache write failed for {path}")
        return jsonify({"error": f"cache_write_failed: {type(e).__name__}: {e}"}), 500

    # 重读返回最新 snapshot 给前端
    cached, _ = metadata_cache.get_by_path(db, path)
    return jsonify(
        {
            "bound": True,
            "cached": _cached_to_library_dict(cached) if cached else None,
        }
    )


@app.route("/api/metadata/cached", methods=["GET"])
@require_token
def metadata_cached():
    """读 media_files cache，不调 TMDB。

    query: path
    返回：
      hit   → {cached: true, stale: false, top_pick, parse, confidence, ..., fetched_at}
      miss  → {cached: false, stale: false}
      stale → {cached: false, stale: true, fetched_at}（前端应触发重识别）
    """
    path = request.args.get("path", "").strip()
    if not path:
        return jsonify({"error": "path required"}), 400
    try:
        validated = validate_path(path)
    except Exception:
        return jsonify({"error": f"invalid path: {path}"}), 400

    # SSH stat 拿当前 mtime/inode 用于 stale 检测
    stat_map = _ssh_stat_paths([validated])
    stat = stat_map.get(validated, {})
    if not stat.get("exists"):
        # 文件已不存在 — cache 即使有也没意义
        return jsonify({"cached": False, "stale": False, "reason": "file_not_found"})

    cached, status = metadata_cache.get_by_path(
        get_db(),
        validated,
        current_mtime=stat["mtime"],
        current_inode=stat["inode"],
    )
    if status == "miss":
        return jsonify({"cached": False, "stale": False})
    if status == "stale":
        return jsonify(
            {
                "cached": False,
                "stale": True,
                "fetched_at": cached.metadata_fetched_at if cached else None,
            }
        )

    # hit — 还原 shape 接近 /api/metadata/identify
    c = cached
    top_pick = None
    if c.tmdb_id:
        top_pick = {
            "id": f"tmdb:{c.media_type}:{c.tmdb_id}",
            "external_ids": {
                k: v for k, v in (("tmdb_id", c.tmdb_id), ("imdb_id", c.imdb_id)) if v
            },
            "title": c.title,
            "original_title": c.original_title,
            "year": c.year,
            "media_type": c.media_type,
            "poster_url": c.poster_url,
            "overview": c.overview,
            "vote_average": c.vote_average,
        }
    details = None
    if c.genres or c.cast or c.runtime_minutes or c.episode_air_date:
        details = {
            "genres": c.genres,
            "cast": c.cast,
            "runtime_minutes": c.runtime_minutes,
        }
        if c.episode_air_date or c.episode_overview or c.episode_still_url:
            details["episode"] = {
                "season_number": c.season_number,
                "episode_number": c.episode_number,
                "name": c.episode_title,
                "overview": c.episode_overview,
                "still_url": c.episode_still_url,
                "air_date": c.episode_air_date,
            }
    return jsonify(
        {
            "cached": True,
            "stale": False,
            "fetched_at": c.metadata_fetched_at,
            "parse": {
                "raw_name": c.parse_raw_name,
                "title": c.title,
                "year": c.year,
                "season": c.season_number,
                "episode": c.episode_number,
                "episode_title": c.episode_title,
                "media_type": "episode" if c.media_type == "tv" else c.media_type,
                "resolution": c.parse_resolution,
                "source": c.parse_source,
                "release_group": c.parse_release_group,
            },
            "candidates": [],  # cache 不存全候选；只要 top_pick 够前端展示
            "top_pick": top_pick,
            "confidence": c.metadata_confidence or 0.0,
            "reasoning": c.metadata_reasoning or "",
            "pick_source": c.metadata_pick_source or "cached",
            "details": details,
            "metadata_status": c.metadata_status,
            "llm_configured": bool(load_deepseek_key()),
            "provider_state": "ok",
        }
    )


def _candidate_to_dict(c) -> dict:
    return {
        "id": c.id,
        "external_ids": c.external_ids,
        "title": c.title,
        "original_title": c.original_title,
        "year": c.year,
        "media_type": c.media_type,
        "poster_url": c.poster_url,
        "overview": c.overview,
        "vote_average": c.vote_average,
    }


@app.route("/api/metadata/from-nfo", methods=["GET"])
@require_token
def metadata_from_nfo():
    """读视频同目录的 sidecar .nfo（若存在）并返回结构化元数据。

    用途：点选视频文件时直接展示已有 .nfo 信息，省一次 TMDB 调用。
    query: video_path（NAS 真实路径，validate_path 校验）
    返回: {has_nfo: bool, nfo_path?: str, parsed?: {...}}
        parsed 字段同 file-content 的 .nfo 解析结果（_parse_emby_nfo + show_title 补全）
    """
    video_path = request.args.get("video_path", "").strip()
    if not video_path:
        return jsonify({"error": "video_path required"}), 400
    try:
        validated = validate_path(video_path)
    except Exception:
        return jsonify({"error": f"invalid video_path: {video_path}"}), 400

    nfo_path = nfo_writer.nfo_path_for_video(validated)
    # 沙箱校验沿用 _read_remote_text 内部（real path 必须在 NAS_BASE_PATH 下）
    text = _read_remote_text(nfo_path)
    if text is None:
        return jsonify({"has_nfo": False, "nfo_path": nfo_path})

    parsed = _parse_emby_nfo(text)
    if not parsed:
        # nfo 存在但 XML 解析失败（编码 / 非 Emby schema）→ 仍标 has_nfo 但 parsed=None
        return jsonify({"has_nfo": True, "nfo_path": nfo_path, "parsed": None})

    # episodedetails → 顺手补剧名 from sibling tvshow.nfo（同 file-content 逻辑）
    if parsed.get("type") == "episodedetails":
        show_dir = os.path.dirname(validated)
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

    return jsonify({"has_nfo": True, "nfo_path": nfo_path, "parsed": parsed})


@app.route("/api/metadata/preview-nfo-write", methods=["POST"])
@require_token
def metadata_preview_nfo_write():
    """生成 NFO 写回的 destructive action preview。

    body: {
        "items": [
            {
                "video_path": "/share/.../Show.S01E01.mkv",
                "tmdb_id": "60625",
                "media_type": "tv" | "movie",
                "season": 6,           # tv 时必填
                "episode": 2,          # tv 时必填
                "title": "瑞克和莫蒂",     # 用于 fallback；后端会用 lookup_by_id 重新拉权威值
                "original_title": "Rick and Morty"
            },
            ...
        ]
    }

    返回 {action_id, signed_token, expires_at, kind: 'nfo_write', preview: {items: [...]}}
    用户用 signed_token 走标准 /api/action/confirm 执行。
    """
    data = request.json or {}
    items_in = data.get("items") or []
    if not isinstance(items_in, list) or not items_in:
        return jsonify({"error": "items required"}), 400
    if len(items_in) > 200:
        return jsonify({"error": "too many items (max 200 per batch)"}), 400

    provider = get_tmdb_provider()
    if provider is None:
        return jsonify({"error": "TMDB API key not configured"}), 400

    # Step 1: 每个 item 调 TMDB lookup_by_id + 构造 XML
    enriched_items: list[dict] = []
    errors: list[dict] = []
    for idx, raw in enumerate(items_in):
        try:
            video_path = validate_path(raw["video_path"])
        except Exception:
            errors.append(
                {"index": idx, "reason": f"invalid video_path: {raw.get('video_path')!r}"}
            )
            continue
        tmdb_id = str(raw.get("tmdb_id") or "").strip()
        media_type = raw.get("media_type", "movie")
        # 把 tv (provider 内部) → episode/movie (NFO 内部) 的映射统一
        nfo_media_type = "episode" if media_type == "tv" else "movie"
        if media_type == "tv" and not raw.get("season") and not raw.get("episode"):
            # 没 season/episode 时退化成 tvshow 级 NFO
            nfo_media_type = "tvshow"

        if not tmdb_id:
            errors.append({"index": idx, "reason": "tmdb_id required"})
            continue

        details = provider.lookup_by_id(
            tmdb_id,
            media_type="tv" if media_type == "tv" else "movie",
            season=raw.get("season"),
            episode=raw.get("episode"),
        )
        if details is None:
            errors.append({"index": idx, "reason": f"tmdb lookup failed for id={tmdb_id}"})
            continue
        cand = details.candidate

        nfo_payload = nfo_writer.NFOPayload(
            media_type=nfo_media_type,
            title=cand.title,
            original_title=cand.original_title,
            year=cand.year,
            plot=cand.overview,
            tmdb_id=cand.external_ids.get("tmdb_id") or tmdb_id,
            imdb_id=cand.external_ids.get("imdb_id"),
            tvdb_id=cand.external_ids.get("tvdb_id"),
            rating=cand.vote_average,
            genres=details.genres,
            cast=details.cast,
            runtime_minutes=details.runtime_minutes,
            poster_url=cand.poster_url,
            season=raw.get("season") if nfo_media_type == "episode" else None,
            episode=raw.get("episode") if nfo_media_type == "episode" else None,
            episode_title=(details.episode or {}).get("name") if details.episode else None,
            episode_overview=(details.episode or {}).get("overview") if details.episode else None,
            episode_air_date=(details.episode or {}).get("air_date") if details.episode else None,
            episode_still_url=(details.episode or {}).get("still_url") if details.episode else None,
        )
        xml = nfo_writer.build_nfo(nfo_payload)
        nfo_path = nfo_writer.nfo_path_for_video(video_path)
        enriched_items.append(
            {
                "video_path": video_path,
                "nfo_path": nfo_path,
                "new_xml": xml,
                "tmdb_id": nfo_payload.tmdb_id,
                "media_type": nfo_media_type,
            }
        )

    if not enriched_items:
        return jsonify({"error": "no_valid_items", "errors": errors}), 400

    # Step 2: SSH stat video + nfo paths（一次性批量）
    all_paths = [it["video_path"] for it in enriched_items] + [
        it["nfo_path"] for it in enriched_items
    ]
    stat_map = _ssh_stat_paths(all_paths)

    snap_items: list[dict] = []
    for it in enriched_items:
        v_stat = stat_map.get(it["video_path"], {"exists": False})
        n_stat = stat_map.get(it["nfo_path"], {"exists": False})
        snap_items.append(
            {
                **it,
                "video_snapshot": {
                    "exists": v_stat.get("exists", False),
                    "inode": v_stat.get("inode", 0),
                    "size_bytes": v_stat.get("size_bytes", 0),
                    "mtime": v_stat.get("mtime", 0),
                },
                "nfo_snapshot": {
                    "existed": n_stat.get("exists", False),
                    "inode": n_stat.get("inode", 0),
                    "size_bytes": n_stat.get("size_bytes", 0),
                    "mtime": n_stat.get("mtime", 0),
                },
            }
        )

    # Step 3: 落 destructive_actions 表
    payload = {
        "kind": "nfo_write",
        "items": snap_items,
        "captured_at": int(time.time()),
    }
    res = destructive_action.create_preview(
        get_db(),
        kind="nfo_write",
        payload=payload,
        server_secret=SERVER_SECRET,
        created_by="web_ui",
    )

    # Step 4: 返回给前端的 preview shape（XML 太大不全返；只返 head 200 字符）
    preview_items = [
        {
            "video_path": it["video_path"],
            "nfo_path": it["nfo_path"],
            "tmdb_id": it["tmdb_id"],
            "media_type": it["media_type"],
            "video_exists": it["video_snapshot"]["exists"],
            "nfo_existed": it["nfo_snapshot"]["existed"],
            "action": "overwrite" if it["nfo_snapshot"]["existed"] else "create",
            "xml_preview": it["new_xml"][:300],
            "xml_size_bytes": len(it["new_xml"].encode("utf-8")),
        }
        for it in snap_items
    ]

    return jsonify(
        {
            "action_id": res.action_id,
            "signed_token": res.signed_token,
            "expires_at": res.expires_at,
            "kind": "nfo_write",
            "preview": {
                "items": preview_items,
                "errors": errors,
                "total": len(preview_items),
                "to_create": sum(1 for it in preview_items if it["action"] == "create"),
                "to_overwrite": sum(1 for it in preview_items if it["action"] == "overwrite"),
            },
        }
    )


# ─────────────────────────────────────────────────────────────
# Phase 2: 全库后台扫描 worker (services/scanner.py)
# ─────────────────────────────────────────────────────────────


@app.route("/api/scan/start", methods=["POST"])
@require_token
def scan_start():
    """启动后台扫描 worker（一次只允许一个）。

    body: {base_path: "/share/...", max_depth?: 5}
    return: {scan_run_id} 或 409 ConcurrentScanError
    """
    data = request.json or {}
    base_path = (data.get("base_path") or NAS_BASE_PATH).strip()
    try:
        base_path = validate_path(base_path)
    except Exception:
        return jsonify({"error": f"invalid base_path: {base_path}"}), 400
    max_depth = max(1, min(10, int(data.get("max_depth", 5))))

    provider = get_tmdb_provider()
    if provider is None:
        return jsonify({"error": "TMDB API key not configured"}), 400
    llm_key = load_deepseek_key() or None

    # 绑定 dependency-injected callables for the scanner worker
    def _scan_identify(p: str):
        return identify_svc.identify(p, provider, llm_api_key=llm_key)

    try:
        scan_run_id = scanner.start_scan(
            db_path=DB_PATH,
            base_path=base_path,
            max_depth=max_depth,
            list_video_paths=lambda bp, md: _list_video_paths(bp, max_depth=md, limit=None),
            ssh_stat=_ssh_stat_paths,
            identify=_scan_identify,
        )
    except scanner.ConcurrentScanError as e:
        return jsonify({"error": "scan_already_running", "detail": str(e)}), 409
    except Exception as e:
        return jsonify({"error": "scan_start_failed", "detail": str(e)}), 500

    return jsonify({"scan_run_id": scan_run_id, "base_path": base_path})


@app.route("/api/scan/status", methods=["GET"])
@require_token
def scan_status():
    """查询某个 scan_run 的实时状态。前端 ~3s 轮询。"""
    scan_run_id = request.args.get("id", "").strip()
    if not scan_run_id.isdigit():
        return jsonify({"error": "id required (integer)"}), 400
    summary = scanner.get_status(get_db(), int(scan_run_id))
    if summary is None:
        return jsonify({"error": "scan_run_not_found"}), 404
    resp = jsonify(
        {
            "scan_run_id": summary.scan_run_id,
            "base_path": summary.base_path,
            "max_depth": summary.max_depth,
            "status": summary.status,
            "files_total": summary.files_total,
            "files_done": summary.files_done,
            "files_failed": summary.files_failed,
            "files_skipped": summary.files_skipped,
            "current_path": summary.current_path,
            "started_at": summary.started_at,
            "completed_at": summary.completed_at,
            "error": summary.error,
        }
    )
    resp.headers["Cache-Control"] = "no-store"  # 轮询不能 cache
    return resp


@app.route("/api/scan/abort", methods=["POST"])
@require_token
def scan_abort():
    """请求 worker 停止：标 status='aborted'，worker 下一 loop 自然退出。"""
    data = request.json or {}
    scan_run_id = data.get("scan_run_id")
    if not isinstance(scan_run_id, int):
        return jsonify({"error": "scan_run_id required (integer)"}), 400
    ok = scanner.abort_scan(get_db(), scan_run_id)
    if not ok:
        return jsonify({"error": "not_running_or_not_found"}), 404
    return jsonify({"ok": True})


@app.route("/api/scan/runs", methods=["GET"])
@require_token
def scan_runs_list():
    """历史扫描记录（最近 N 次）。"""
    limit = max(1, min(50, int(request.args.get("limit", "10"))))
    runs = scanner.list_recent_runs(get_db(), limit=limit)
    return jsonify({"runs": runs})


@app.route("/api/scan/failed", methods=["GET"])
@require_token
def scan_failed_items():
    """某次 scan 的 failed item 详情列表。"""
    scan_run_id = request.args.get("id", "").strip()
    if not scan_run_id.isdigit():
        return jsonify({"error": "id required (integer)"}), 400
    limit = max(1, min(500, int(request.args.get("limit", "100"))))
    items = scanner.list_failed_items(get_db(), int(scan_run_id), limit=limit)
    return jsonify({"items": items, "total": len(items)})


# ─────────────────────────────────────────────────────────────
# Phase 2: 库视图 — 按 TMDB 元数据浏览（不按目录树）
# ─────────────────────────────────────────────────────────────


def _cached_to_library_dict(c) -> dict:
    """CachedMetadata → 库视图 API response dict（瘦身版，不含 provenance）。"""
    return {
        "path": c.path,
        "tmdb_id": c.tmdb_id,
        "tmdb_series_id": c.tmdb_series_id,  # for frontend series aggregation
        "imdb_id": c.imdb_id,
        "media_type": c.media_type,
        "title": c.title,
        "original_title": c.original_title,
        "year": c.year,
        "season": c.season_number,
        "episode": c.episode_number,
        "episode_title": c.episode_title,
        "poster_url": c.poster_url,
        "overview": c.overview,
        "vote_average": c.vote_average,
        "genres": c.genres,
        "cast": c.cast[:6],
        "runtime_minutes": c.runtime_minutes,
        "first_seen_at": c.first_seen_at,
        "size_bytes": c.size_bytes,  # for series card total size
        "resolution": c.parse_resolution,
        "source": c.parse_source,
    }


@app.route("/api/library/items", methods=["GET"])
@require_token
def library_items():
    """库视图主查询。

    query params:
      media_type:  'movie' | 'tv'   过滤
      year_from:   int               年份下限
      year_to:     int               年份上限
      q:           str               title 模糊匹配（中文/英文/raw_name 任一命中）
      sort:        added_desc | year_desc | year_asc | vote_desc | title_asc
      limit:       默认 200，最大 500
      offset:      分页偏移
    """
    args = request.args
    media_type = args.get("media_type") or None
    year_from = args.get("year_from", type=int)
    year_to = args.get("year_to", type=int)
    query = (args.get("q") or "").strip() or None
    sort = args.get("sort", "added_desc")
    limit = max(1, min(500, args.get("limit", 200, type=int)))
    offset = max(0, args.get("offset", 0, type=int))

    items, total = metadata_cache.query_library(
        get_db(),
        media_type=media_type,
        year_from=year_from,
        year_to=year_to,
        query=query,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    resp = jsonify(
        {
            "items": [_cached_to_library_dict(c) for c in items],
            "total": total,
            "has_more": offset + len(items) < total,
            "limit": limit,
            "offset": offset,
        }
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/library/stats", methods=["GET"])
@require_token
def library_stats():
    """库概览统计。"""
    stats = metadata_cache.get_library_stats(get_db())
    resp = jsonify(stats)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/library/watched-stale", methods=["GET"])
@require_token
def library_watched_stale():
    """Phase 3.5: 已看完 + N 天未动的媒体（候选删除 / 归档）。

    Query params:
      days:    default 180; min 1
      limit:   default 50, max 500
      offset:  default 0
    """
    args = request.args
    days = max(1, args.get("days", 180, type=int))
    limit = max(1, min(500, args.get("limit", 50, type=int)))
    offset = max(0, args.get("offset", 0, type=int))

    items, total = dedup.find_watched_stale_media(
        get_db(),
        days=days,
        limit=limit,
        offset=offset,
    )
    total_bytes = sum(it.get("size_bytes", 0) or 0 for it in items)
    resp = jsonify(
        {
            "items": items,
            "total": total,
            "total_bytes_on_page": total_bytes,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(items) < total,
            "days_threshold": days,
        }
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/library/companions-in-dir", methods=["GET"])
@app.route("/api/library/extras-in-dir", methods=["GET"])  # 旧名 alias
@require_token
def library_companions_in_dir():
    """列出某 main feature 同目录的附属文件：花絮（extra）+ 多盘分段（part）。

    query: path = main feature 的完整路径（dirname 用来 prefix-match）
    返回 items 每条带 kind 字段：'extra' 或 'part'
    """
    path = (request.args.get("path") or "").strip()
    if not path:
        return jsonify({"error": "missing path"}), 400
    # validate_path 防穿越 + 必须在 nas_base_path 下；失败会直接 abort()
    validate_path(path)
    dir_path = path.rsplit("/", 1)[0] if "/" in path else ""
    companions = metadata_cache.list_companions_in_dir(get_db(), dir_path)
    resp = jsonify(
        {
            "dir": dir_path,
            "items": [
                {
                    "path": c.path,
                    "raw_name": c.parse_raw_name,
                    "kind": c.media_type,  # 'extra' | 'part'
                    "size_bytes": c.size_bytes,
                    "resolution": c.parse_resolution,
                    "source": c.parse_source,
                }
                for c in companions
            ],
            "count": len(companions),
        }
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ─────────────────────────────────────────────────────────────
# Phase 3.2: Dedup engine — 重复 release 检测
# ─────────────────────────────────────────────────────────────


@app.route("/api/dedup/groups", methods=["GET"])
@require_token
def dedup_groups():
    """重复 release 组列表（按 ROI 降序）。

    query params:
      media_type:    'movie' | 'tv'      只看某类（None=两类都返）
      watched_only:  '1' / 'true'        仅展示已看完组（用户决定先清这些）
      limit:         default 50, max 200
      offset:        default 0
    """
    args = request.args
    media_type = args.get("media_type") or None
    if media_type not in (None, "movie", "tv"):
        return jsonify({"error": "media_type must be 'movie' or 'tv'"}), 400
    watched_only = args.get("watched_only", "").strip().lower() in ("1", "true", "yes")
    limit = max(1, min(200, args.get("limit", 50, type=int)))
    offset = max(0, args.get("offset", 0, type=int))

    groups, total = dedup.find_duplicate_groups(
        get_db(),
        media_type=media_type,
        watched_only=watched_only,
        limit=limit,
        offset=offset,
    )
    total_deletable = sum(g.deletable_size_bytes for g in groups)
    resp = jsonify(
        {
            "groups": [dedup.group_to_dict(g) for g in groups],
            "total_groups": total,
            "total_deletable_bytes": total_deletable,
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(groups) < total,
        }
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/dedup/weights", methods=["GET"])
@require_token
def dedup_weights_get():
    rows = (
        get_db()
        .execute("SELECT key, weight, updated_at FROM dedup_weights ORDER BY key")
        .fetchall()
    )
    current_hash = dedup.get_current_hash(get_db())
    return jsonify(
        {
            "weights": [dict(r) for r in rows],
            "current_hash": current_hash,
        }
    )


@app.route("/api/dedup/weights", methods=["POST"])
@require_token
def dedup_weights_post():
    """Update 一个或多个 weight。 body: {weights: {key: number, ...}}.

    返 {new_hash, rows_changed}; 调用方可立即触发 /api/dedup/refresh
    在后台批量回写 quality_score 缓存。
    """
    data = request.json or {}
    updates = data.get("weights") or {}
    if not isinstance(updates, dict) or not updates:
        return jsonify({"error": "body must contain {weights: {key: number}}"}), 400
    # 验证：key 合法 + value 数字（NaN/inf/negative 在 dedup.update_weights 内部拒绝）
    for k, v in updates.items():
        if not isinstance(k, str) or not k.strip():
            return jsonify({"error": f"invalid weight key: {k!r}"}), 400
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return jsonify({"error": f"weight value must be number: {k}={v!r}"}), 400
    try:
        new_hash, changed = dedup.update_weights(get_db(), updates)
    except dedup.InvalidWeightError as e:
        # NaN / Inf / negative — 400 客户端错误（非服务端 bug）
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"new_hash": new_hash, "rows_changed": changed})


@app.route("/api/dedup/refresh", methods=["POST"])
@require_token
def dedup_refresh():
    """后台批量 recompute quality_score 缓存。

    不强制——live queries 始终按当前 hash recompute。这只是把 stale 缓存写回
    避免下次查询时多算一次。
    """
    try:
        updated = dedup.refresh_all_quality_scores(get_db())
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"updated": updated})


# ─────────────────────────────────────────────────────────────
# Phase 3.4: Emby watch-source integration
# ─────────────────────────────────────────────────────────────


@app.route("/api/config/emby", methods=["GET"])
@require_token
def get_emby_config():
    cfg = load_emby_config()
    return jsonify(
        {
            "url": cfg.get("url", ""),
            "user_id": cfg.get("user_id", ""),
            "has_key": bool(load_emby_key()),
        }
    )


@app.route("/api/config/emby", methods=["POST"])
@require_token
def set_emby_config():
    data = request.json or {}
    url = (data.get("url") or "").strip()
    user_id = (data.get("user_id") or "").strip()
    api_key = (data.get("api_key") or "").strip()
    if not url or not user_id:
        return jsonify({"ok": False, "message": "url and user_id required"}), 400
    save_emby_config(url, user_id)
    if api_key:
        save_emby_key(api_key)
    return jsonify({"ok": True, "has_key": bool(load_emby_key())})


@app.route("/api/config/emby/test", methods=["POST"])
@require_token
def test_emby_config():
    """临时验证 emby key：body 可传 url/user_id/api_key 直接测，不落盘。"""
    data = request.json or {}
    cfg = load_emby_config()
    url = (data.get("url") or "").strip() or cfg.get("url", "")
    user_id = (data.get("user_id") or "").strip() or cfg.get("user_id", "")
    api_key = (data.get("api_key") or "").strip() or load_emby_key()
    if not (url and user_id and api_key):
        return jsonify({"ok": False, "message": "url, user_id, api_key all required"}), 400
    try:
        from clients.watch.emby import EmbyClient

        result = EmbyClient(base_url=url, user_id=user_id, api_key=api_key).test_connection()
    except Exception as e:
        return jsonify({"ok": False, "message": f"{type(e).__name__}: {e}"}), 200
    return jsonify(result)


# ─── Organize (Phase 4A.5): MOVIES_ROOT / TV_ROOT 配置 ────────


@app.route("/api/config/organize", methods=["GET"])
@require_token
def get_organize_config():
    cfg = load_organize_config()
    return jsonify(
        {
            "movies_root": cfg.get("movies_root", ""),
            "tv_root": cfg.get("tv_root", ""),
            "configured": bool(cfg.get("movies_root") and cfg.get("tv_root")),
        }
    )


@app.route("/api/config/organize", methods=["POST"])
@require_token
def set_organize_config():
    """落盘 MOVIES_ROOT / TV_ROOT。两个 path 必须 absolute（防相对路径歧义）。"""
    data = request.json or {}
    movies_root = (data.get("movies_root") or "").strip()
    tv_root = (data.get("tv_root") or "").strip()
    if not movies_root or not tv_root:
        return jsonify({"ok": False, "message": "movies_root and tv_root required"}), 400
    if not movies_root.startswith("/") or not tv_root.startswith("/"):
        return jsonify({"ok": False, "message": "paths must be absolute (start with /)"}), 400
    save_organize_config({"movies_root": movies_root, "tv_root": tv_root})
    return jsonify({"ok": True, "configured": True})


@app.route("/api/config/organize/test", methods=["POST"])
@require_token
def test_organize_config():
    """SSH 检查两个 root 目录存在 + 可写。body 可传新 path 直接测不落盘。

    codex r1 IMPORTANT 2: 跟 POST save 一样强制绝对路径，避免 test/save 接口
    contract 漂移（用户在 test 时传相对路径"看着 ok" 但 save 阶段被 reject）。
    """
    data = request.json or {}
    cfg = load_organize_config()
    movies_root = (data.get("movies_root") or "").strip() or cfg.get("movies_root", "")
    tv_root = (data.get("tv_root") or "").strip() or cfg.get("tv_root", "")
    if not movies_root or not tv_root:
        return jsonify({"ok": False, "message": "movies_root and tv_root required"}), 400
    if not movies_root.startswith("/") or not tv_root.startswith("/"):
        return jsonify({"ok": False, "message": "paths must be absolute (start with /)"}), 400
    rc_m, _, _ = ssh_exec(
        f"test -d {shlex.quote(movies_root)} -a -w {shlex.quote(movies_root)}",
        timeout=10,
    )
    rc_t, _, _ = ssh_exec(
        f"test -d {shlex.quote(tv_root)} -a -w {shlex.quote(tv_root)}",
        timeout=10,
    )
    return jsonify(
        {
            "movies_ok": rc_m == 0,
            "tv_ok": rc_t == 0,
            "ok": rc_m == 0 and rc_t == 0,
            "movies_root": movies_root,
            "tv_root": tv_root,
        }
    )


def _open_db_conn():
    """Mint a fresh sqlite3 connection for background workers (not request-scoped)."""
    return destructive_action.open_connection(DB_PATH)


# ─── Phase 4C.5: qBit auto-organize config + history routes ────────


@app.route("/api/config/qbit-auto-organize", methods=["GET"])
@require_token
def get_qbit_auto_organize_config():
    """返当前 config + 配套：现有 qBit categories list (UI 多选用)."""
    cfg = load_qbit_auto_organize_config()
    # qBit categories 可能 fetch 失败 (qbit down)；不影响 config GET 主路径
    qbit_categories: list[str] = []
    qbit_error: str | None = None
    try:
        torrents = qbit.get_torrents()
        qbit_categories = sorted(
            {
                (t.get("category") or "").strip()
                for t in torrents
                if (t.get("category") or "").strip()
            }
        )
    except Exception as e:
        qbit_error = f"{type(e).__name__}: {e}"
    return jsonify(
        {
            **cfg,
            "available_qbit_categories": qbit_categories,
            "qbit_fetch_error": qbit_error,
            "active_changes_require_restart": True,
        }
    )


@app.route("/api/config/qbit-auto-organize", methods=["POST"])
@require_token
def set_qbit_auto_organize_config():
    """落盘 + 提示重启生效（poll_interval 跟 cron 注册时绑定）.

    Phase 4C v1: interval 改动需要重启 server（APScheduler 周期变更需 reschedule）.

    codex r1 I2 fix: enabled=True 但 categories=[] = 用户配置错误（cron 跑后只能
    warn 然后不触发任何种子）→ 保存阶段直接 fail-fast 400，让用户立刻看到错误。
    """
    data = request.json or {}
    enabled = bool(data.get("enabled", False))
    cats = data.get("categories") or []
    if not isinstance(cats, list):
        cats = []
    cleaned_cats = [str(c).strip() for c in cats if c is not None and str(c).strip()]
    if enabled and not cleaned_cats:
        return jsonify(
            {
                "ok": False,
                "error": "categories_required_when_enabled",
                "message": "启用前必须配置至少一个 qBit category 白名单",
            }
        ), 400
    # 类型 / 边界清洗在 save 内做
    save_qbit_auto_organize_config(data)
    new_cfg = load_qbit_auto_organize_config()
    return jsonify(
        {
            "ok": True,
            **new_cfg,
            "active_changes_require_restart": True,
            "message": "保存成功；poll_interval 变更需重启 server 生效",
        }
    )


@app.route("/api/auto-organize/runs", methods=["GET"])
@require_token
def list_auto_organize_runs():
    """UI 历史视图查询.

    Query: ?status=<filter>&limit=50&offset=0
    Returns: {runs: [...], total, limit, offset}
    """
    try:
        limit = max(1, min(200, int(request.args.get("limit", "50"))))
    except (TypeError, ValueError):
        limit = 50
    try:
        offset = max(0, int(request.args.get("offset", "0")))
    except (TypeError, ValueError):
        offset = 0
    status_filter = request.args.get("status", "").strip() or None
    db = get_db()
    try:
        runs = qbit_auto.list_history(
            db,
            limit=limit,
            offset=offset,
            status_filter=status_filter,
        )
    except ValueError as e:
        return jsonify({"error": "invalid_status_filter", "message": str(e)}), 400
    # total count for pagination UI
    if status_filter:
        total = db.execute(
            "SELECT count(*) FROM auto_organize_runs WHERE status = ?",
            (status_filter,),
        ).fetchone()[0]
    else:
        total = db.execute("SELECT count(*) FROM auto_organize_runs").fetchone()[0]
    return jsonify(
        {
            "runs": runs,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@app.route("/api/auto-organize/reset", methods=["POST"])
@require_token
def reset_auto_organize_run():
    """User 手动 reset 单 row (failed / skipped_* → 删除).

    Body: {"qbit_hash": str, "trigger_now": bool (optional, default False)}

    - trigger_now=False（默认）：仅 DELETE row，下周期 cron 重新尝试（最多等 5min）
    - trigger_now=True：DELETE 后立即调 qbit.get_torrents() 找 hash 对应种子 → dispatch_one
      → 5s 出结果。qBit 不可达 / 种子不存在 → trigger_result 字段说明，但 reset 仍 ok

    只允许删除 terminal 状态的 row (不能删 organizing / pending — 可能有副作用未完成).
    """
    data = request.json or {}
    qbit_hash = (data.get("qbit_hash") or "").strip()
    if not qbit_hash:
        return jsonify({"ok": False, "error": "qbit_hash required"}), 400
    trigger_now = bool(data.get("trigger_now", False))

    db = get_db()
    row = qbit_auto.get_run(db, qbit_hash)
    if row is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    if row["status"] in ("pending", "organizing"):
        return jsonify(
            {
                "ok": False,
                "error": "cannot_reset_active",
                "current_status": row["status"],
                "message": f"row 当前 {row['status']}，等其变为 terminal 再 reset",
            }
        ), 409
    db.execute("DELETE FROM auto_organize_runs WHERE qbit_hash = ?", (qbit_hash,))
    db.commit()

    trigger_result = None
    if trigger_now:
        try:
            torrents = qbit.get_torrents()
            target = next((t for t in torrents if t.get("hash") == qbit_hash), None)
            if target is None:
                trigger_result = {
                    "action": "qbit_torrent_not_found",
                    "message": "qBit 中找不到该 hash 对应的种子（已删 / hash 不匹配）",
                }
            else:
                cfg = load_qbit_auto_organize_config()
                threshold = cfg.get("confidence_threshold", 0.85)
                # reset 立即重试与 cron 行为一致：auto_identify 开启时也走自动识别，
                # 否则已卡 needs_identify 的种子点 reset 仍跳过识别 → 又卡同一状态
                # （新 skipped row 让 cron 后续也不再碰）→ 用户无法恢复。
                auto_identify = cfg.get("auto_identify", False)
                auto_id_threshold = cfg.get("auto_identify_confidence_threshold", 0.95)
                identify_fn = (
                    (lambda ni_paths: _auto_identify_paths(db, ni_paths))
                    if auto_identify
                    else None
                )
                trigger_result = qbit_auto.dispatch_one(
                    db,
                    target,
                    list_video_paths_fn=lambda cp: _list_video_paths(
                        cp,
                        max_depth=3,
                        limit=MAX_ORGANIZE_BATCH_ITEMS,
                    ),
                    confidence_threshold=threshold,
                    build_and_start_organize_fn=_build_and_start_auto_organize,
                    identify_paths_fn=identify_fn,
                    auto_identify_threshold=auto_id_threshold if auto_identify else None,
                )
        except Exception as e:
            logger.exception(f"[auto-organize] trigger_now failed for {qbit_hash}")
            trigger_result = {
                "action": "trigger_failed",
                "error": f"{type(e).__name__}: {e}",
                "message": "立即触发失败（qBit 不可达？）；下周期 cron 仍会重试",
            }
    return jsonify(
        {
            "ok": True,
            "qbit_hash": qbit_hash,
            "previous_status": row["status"],
            "trigger_result": trigger_result,
        }
    )


@app.route("/api/watch/sync", methods=["POST"])
@require_token
def watch_sync_start():
    """Start an Emby sync run. Returns run_id; client polls /api/watch/sync/status."""
    client = _emby_client()
    if client is None:
        return jsonify(
            {
                "error": "emby_not_configured",
                "detail": "configure /api/config/emby first (url + user_id + api_key)",
            }
        ), 400

    try:
        run_id = watch_sync.sync_emby(
            open_conn=_open_db_conn,
            emby_client=client,
            since_iso=None,
        )
    except watch_sync.ConcurrentSyncError as e:
        return jsonify({"error": "concurrent_sync", "detail": str(e)}), 409
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500
    return jsonify({"run_id": run_id})


@app.route("/api/watch/sync/status", methods=["GET"])
@require_token
def watch_sync_status():
    run_id = request.args.get("id", "").strip()
    if not run_id.isdigit():
        return jsonify({"error": "id required (integer)"}), 400
    info = watch_sync.get_sync_status(get_db(), int(run_id))
    if info is None:
        return jsonify({"error": "not_found"}), 404
    resp = jsonify(info)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/watch/status", methods=["GET"])
@require_token
def watch_provider_status():
    """Overall provider readiness: configured? connection healthy?"""
    cfg = load_emby_config()
    has_key = bool(load_emby_key())
    configured = bool(cfg.get("url") and cfg.get("user_id") and has_key)
    if not configured:
        return jsonify({"emby": {"state": "not_configured"}})
    client = _emby_client()
    try:
        result = client.test_connection()
        state = "ok" if result.get("ok") else (result.get("code") or "error")
        return jsonify(
            {
                "emby": {
                    "state": state,
                    "message": result.get("message"),
                    "server_name": result.get("server_name"),
                }
            }
        )
    except Exception as e:
        return jsonify({"emby": {"state": "error", "message": str(e)}})


@app.route("/api/action/recovery", methods=["GET"])
@require_token
def action_recovery_list():
    """列出需要人工恢复的 action（spike 阶段简单只读视图）。"""
    rows = (
        get_db()
        .execute(
            """
        SELECT action_id, kind, status, created_at, started_at, completed_at,
               error, recovery_hint, payload_json
          FROM destructive_actions
         WHERE status IN ('needs_manual_recovery', 'running', 'failed')
         ORDER BY created_at DESC
         LIMIT 50
        """
        )
        .fetchall()
    )
    return jsonify(
        {
            "actions": [dict(r) for r in rows],
        }
    )


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
            # bug #14: 必须同时 reap 卡死的 watch_sync_runs，否则崩溃的同步永久占住
            # uniq_watch_sync_running 单飞槽 → 所有后续同步被拒。timeout 取 1800s
            # （远大于最长合法 Emby sync），ground-truth 兜底不误杀正常长同步。
            sync_reaped = watch_sync.reap_stuck_sync_runs(conn, timeout_secs=1800)
            if reaped or cleaned or sync_reaped:
                logger.info(
                    f"[cron] reaped {reaped} stuck running, cleaned {cleaned} expired pending, "
                    f"reaped {sync_reaped} stuck sync runs"
                )
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"[cron] reap failed: {e}")


def _identify_and_cache(conn, path: str) -> dict:
    """cron 自动识别单个 canonical path：TMDB+LLM 识别 → 写 media_files。

    线程安全：用传入的 conn（cron 独立连接），**不调 get_db()** —— cron 是
    BackgroundScheduler 后台线程，get_db() 会撞 'Working outside of application
    context'（identify_svc 走 HTTP / _ssh_stat_paths 走 SSH，均不需 Flask 上下文）。

    返回 {"identified": bool, "provider_unavailable": bool}。
    """
    provider = get_tmdb_provider()
    if provider is None:
        return {"identified": False, "provider_unavailable": False}
    try:
        result = identify_svc.identify(path, provider, llm_api_key=load_deepseek_key() or None)
    except ProviderUnavailable:
        return {"identified": False, "provider_unavailable": True}
    stat_map = _ssh_stat_paths([path])
    stat = stat_map.get(path, {})
    if not stat.get("exists"):
        return {"identified": False, "provider_unavailable": False}
    metadata_cache.upsert_identification(conn, path=path, stat=stat, identify_result=result)
    return {"identified": True, "provider_unavailable": False}


def _auto_identify_paths(conn, paths: list[str]) -> dict:
    """对 needs_identify 的 paths 逐个自动识别。任一 provider_unavailable → 短路返回
    （临时错误，dispatch 会标 locked 留下周期重试）。"""
    for p in paths:
        outcome = _identify_and_cache(conn, p)
        if outcome["provider_unavailable"]:
            return {"provider_unavailable": True}
    return {"provider_unavailable": False}


def _cron_qbit_auto_organize():
    """Phase 4C.4: 定时扫 qBit completed torrents + 触发自动 organize.

    每周期执行：
      1. reconcile：同步上周期 organizing → terminal（基于 destructive_actions 状态）
      2. load config — disabled 直接返回
      3. 调 qbit.get_torrents() → list_completed_torrents(whitelist)
      4. filter_unprocessed_hashes → 跳过 terminal/organizing
      5. for each unprocessed hash → dispatch_one (callback = _build_and_start_auto_organize)
      6. organize_runner module-level lock 同时只允许 1 个 organize 跑：
         dispatch_one 内部 build_and_start_organize_fn 返 locked → 留 pending 让下周期重试

    任何单个 torrent 失败都不阻塞其余（catch per-torrent）.
    """
    try:
        conn = destructive_action.open_connection(DB_PATH)
        try:
            # 1. reconcile 上周期 organizing → terminal（独立 reconcile，dispatch 失败也跑）
            synced = qbit_auto.reconcile_organizing_rows(conn)
            if synced:
                logger.info(
                    f"[auto-organize] reconciled {len(synced)} organizing rows: "
                    f"{[r['synced_to'] for r in synced]}"
                )

            cfg = load_qbit_auto_organize_config()
            if not cfg.get("enabled"):
                return
            whitelist = cfg.get("categories", [])
            if not whitelist:
                logger.warning(
                    "[auto-organize] enabled=True 但 categories 白名单空，不触发任何种子"
                )
                return

            # 2. 拉 qBit completed torrents
            try:
                torrents = qbit.get_torrents()
            except Exception as e:
                logger.error(f"[auto-organize] qbit.get_torrents failed: {e}")
                return
            completed = qbit_auto.list_completed_torrents(torrents, whitelist)
            if not completed:
                return

            # 3. 跳过已处理 hash
            all_hashes = [t["hash"] for t in completed]
            unprocessed = qbit_auto.filter_unprocessed_hashes(conn, all_hashes)
            todo = [t for t in completed if t["hash"] in unprocessed]
            if not todo:
                return

            logger.info(
                f"[auto-organize] cycle: {len(completed)} completed in whitelist "
                f"{whitelist}, {len(todo)} to dispatch"
            )

            # 4. dispatch each
            stats: dict[str, int] = {}
            threshold = cfg.get("confidence_threshold", 0.85)
            auto_identify = cfg.get("auto_identify", False)
            auto_id_threshold = cfg.get("auto_identify_confidence_threshold", 0.95)
            identify_fn = (
                (lambda ni_paths: _auto_identify_paths(conn, ni_paths)) if auto_identify else None
            )
            for t in todo:
                try:
                    out = qbit_auto.dispatch_one(
                        conn,
                        t,
                        # cp 是 lambda 参数（dispatch_one 调用时传入），不是闭包捕获 —
                        # 不存在 Python late-binding 陷阱。
                        list_video_paths_fn=lambda cp: _list_video_paths(
                            cp,
                            max_depth=3,
                            limit=MAX_ORGANIZE_BATCH_ITEMS,
                        ),
                        confidence_threshold=threshold,
                        build_and_start_organize_fn=_build_and_start_auto_organize,
                        identify_paths_fn=identify_fn,
                        auto_identify_threshold=auto_id_threshold if auto_identify else None,
                    )
                    action = out.get("action", "unknown")
                    stats[action] = stats.get(action, 0) + 1
                    if action in ("started", "skipped", "errored", "locked"):
                        logger.info(
                            f"[auto-organize] hash={t['hash'][:8]} name={t.get('name')!r} "
                            f"action={action} status={out.get('status')} "
                            f"action_id={out.get('action_id')} reason={out.get('reason')}"
                        )
                    # locked → 不会再 dispatch 别的 torrent，break 让 organize 跑完再说
                    if action == "locked":
                        logger.info(
                            "[auto-organize] organize_runner locked，本周期剩余 torrents 推迟下周期"
                        )
                        break
                except Exception as e:
                    logger.exception(f"[auto-organize] dispatch failed for {t['hash'][:8]}: {e}")
                    stats["exception"] = stats.get("exception", 0) + 1
            logger.info(f"[auto-organize] cycle done: {stats}")
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"[auto-organize] cron job crashed: {e}", exc_info=True)


def _start_scheduler():
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler(daemon=True)
    sched.add_job(_cron_reap_stuck, "interval", minutes=1, id="reap_stuck", max_instances=1)
    # Phase 4C.4: qBit auto-organize cron — interval 从配置读，最低 1min
    qbit_auto_cfg = load_qbit_auto_organize_config()
    poll_minutes = qbit_auto_cfg.get("poll_interval_minutes", 5)
    sched.add_job(
        _cron_qbit_auto_organize,
        "interval",
        minutes=poll_minutes,
        id="qbit_auto_organize",
        max_instances=1,
        # coalesce=True: 多个 missed run 合并成一个执行（防 backlog）
        coalesce=True,
        # next_run_time 让 cron 启动后立刻跑一次 (而非等 poll_minutes 才第一次)
        # 不写 next_run_time → 默认下一周期才跑（可能 5min 后），调试不便
    )
    sched.start()
    logger.info(
        f"[cron] BackgroundScheduler started (reap=1min, "
        f"qbit_auto_organize={poll_minutes}min enabled={qbit_auto_cfg.get('enabled')})"
    )
    return sched


_scheduler = None
if not _DISABLE_CRON:
    _scheduler = _start_scheduler()


if __name__ == "__main__":
    # 默认绑定 127.0.0.1，生产环境应使用 gunicorn
    app.run(host="127.0.0.1", port=8080, debug=False)
