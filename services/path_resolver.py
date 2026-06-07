"""NAS symlink path resolver.

QNAP 默认在 /share/ 下挂 share-folder symlink 指到存储卷 canonical path：
  /share/downloads     -> CACHEDEV2_DATA/downloads
  /share/pt            -> CACHEDEV2_DATA/pt
  /share/Moives        -> CACHEDEV2_DATA/Moives
  /share/TV show       -> CACHEDEV2_DATA/TV show

qBit reports content_path 用 symlink alias (`/share/downloads/...`)，而 NASVault
扫库走 `ssh find /share/CACHEDEV2_DATA/...` 写 canonical path 到 media_files —
两套 namespace 不一致 → auto-organize cron lookup cache 100% miss。

这个 module 提供 prefix-replace resolver: 5 min TTL in-memory cache, 启动时按需
惰性加载, 失败时返原 path (safe fallback)。
"""

from __future__ import annotations

import re
import shlex
import threading
import time
from typing import Callable

# Module-level alias map cache + TTL.
# _aliases: {alias_abs_prefix: canonical_abs_prefix}
# e.g. {"/share/downloads": "/share/CACHEDEV2_DATA/downloads", ...}
_lock = threading.Lock()
_aliases: dict[str, str] | None = None
_loaded_at: float = 0.0
_TTL_SEC: float = 300.0  # 5 min


# ls -la 行匹配，倒数捕获 "name -> target"
# 示例 input line:
#   lrwxrwxrwx   1 admin administrators   24 Apr 16 17:14 downloads -> CACHEDEV2_DATA/downloads
# 因 QNAP filename 可含空格 (e.g. "TV show -> ..."), 用 right-anchored " -> " 拆分.
_LS_SYMLINK_RE = re.compile(r"^l[\w\-]+\s")


def parse_ls_symlinks(stdout: str, parent_dir: str) -> dict[str, str]:
    """从 `ls -la <parent_dir>` 输出抽 symlink, 返 {abs_alias: abs_canonical}.

    支持:
      - filename 含空格 ("TV show -> CACHEDEV2_DATA/TV show")
      - target 是 relative ("CACHEDEV2_DATA/downloads") 或 absolute ("/share/foo/bar")
    """
    parent = parent_dir.rstrip("/")
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        # 只看 symlink 行 (mode bits 第 1 位是 'l')
        if not _LS_SYMLINK_RE.match(line):
            continue
        # 找 " -> " 作为 name/target 分界 (在 ls 输出末尾)
        arrow_idx = line.find(" -> ")
        if arrow_idx < 0:
            continue
        target_raw = line[arrow_idx + 4 :].strip()
        # name = arrow 之前 split whitespace 后的最后一个 token (含可能的空格 filename 需要更小心)
        # ls -la format: <mode> <links> <user> <group> <size> <date1> <date2> <date3> <name>
        # 用 split(None, 8) 拆 9 段, 最后一段是 name
        head = line[:arrow_idx].rstrip()
        parts = head.split(None, 8)
        if len(parts) < 9:
            continue
        name = parts[8]
        if not name or "/" in name:
            continue
        alias_abs = f"{parent}/{name}"
        if target_raw.startswith("/"):
            canonical_abs = target_raw.rstrip("/")
        else:
            canonical_abs = f"{parent}/{target_raw.rstrip('/')}"
        out[alias_abs] = canonical_abs
    return out


def _load_aliases(ssh_exec_fn: Callable, parent_dir: str = "/share") -> dict[str, str]:
    """SSH 扫 parent_dir 第一层 symlink, 返 alias map.

    Args:
        ssh_exec_fn: app.ssh_exec 之类的可调用 (cmd, timeout=...) -> (code, stdout, stderr)
        parent_dir: 扫的父目录, 默认 QNAP 风格 /share/
    """
    cmd = f"ls -la {shlex.quote(parent_dir)} 2>/dev/null"
    try:
        code, stdout, _stderr = ssh_exec_fn(cmd, timeout=10)
    except Exception:
        return {}
    if code != 0 or not stdout:
        return {}
    return parse_ls_symlinks(stdout, parent_dir)


def get_aliases(
    ssh_exec_fn: Callable, *, force: bool = False, parent_dir: str = "/share"
) -> dict[str, str]:
    """拿 alias map (cached, TTL=5min).

    force=True 强制刷新 (admin endpoint 用)。
    """
    global _aliases, _loaded_at
    now = time.time()
    if not force and _aliases is not None and (now - _loaded_at) < _TTL_SEC:
        return _aliases
    with _lock:
        # double-check inside lock 避免重复 SSH
        if not force and _aliases is not None and (now - _loaded_at) < _TTL_SEC:
            return _aliases
        _aliases = _load_aliases(ssh_exec_fn, parent_dir=parent_dir)
        _loaded_at = now
        return _aliases


def resolve(p: str, ssh_exec_fn: Callable) -> str:
    """把 alias path 翻成 canonical path, longest-prefix-match。

    无匹配 / SSH 失败 / 空输入 → 返原样 (safe fallback, 永不抛)。
    """
    if not p:
        return p
    try:
        aliases = get_aliases(ssh_exec_fn)
    except Exception:
        return p
    if not aliases:
        return p
    # longest prefix first 避免 /share/downloads 被 /share 短前缀错配
    candidates = [alias for alias in aliases if p == alias or p.startswith(alias + "/")]
    if not candidates:
        return p
    best = max(candidates, key=len)
    return aliases[best] + p[len(best) :]


def resolve_many(paths: list[str], ssh_exec_fn: Callable) -> list[str]:
    """Batch resolve, 单次 SSH 拿 alias map 再本地映射."""
    if not paths:
        return paths
    # 触发一次 SSH (cached) 然后 N 次本地 string ops
    get_aliases(ssh_exec_fn)
    return [resolve(p, ssh_exec_fn) for p in paths]


def invalidate() -> None:
    """清 cache, 下次 get_aliases 强制重 SSH."""
    global _aliases, _loaded_at
    with _lock:
        _aliases = None
        _loaded_at = 0.0


def snapshot() -> dict[str, str] | None:
    """Debug: 返当前 cache 内容 (None 如果未加载)。Admin endpoint 用。"""
    return None if _aliases is None else dict(_aliases)
