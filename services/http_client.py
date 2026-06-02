"""LAN-aware requests.Session 工厂.

NASVault 目标受众 (家庭 NAS / PT user) 几乎都装 Clash / V2Ray / Surge 等代理软件.
shell 设了 http_proxy/https_proxy 后, Python `requests` 默认会**对所有 host 走代理** —
包括 LAN IP (NAS / qBit / Emby), 代理不路由内网 → 502 Bad Gateway.

`is_lan_host(host)`: True 表示该 host 应该直连不走 env proxy.
`session_for(url)`: 构造 Session, LAN host 自动 `trust_env=False`.
`apply_proxy_policy(session, url)`: 对**已有** Session 按 url 调整 trust_env
(用于 Session 在 url 已知前就创建, 例如 QBitClient).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import requests

# bug #9: bare-hostname → 私网 IP 的 DNS 解析结果缓存（host → is_lan）。
# 只缓存成功解析，避免一次性 DNS 抖动把 LAN host 永久误判公网。
_lan_cache: dict[str, bool] = {}


def _ip_is_lan(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def is_lan_host(host: str) -> bool:
    """LAN 判定: private IP / loopback / link-local / *.local / 'localhost' / 解析到私网的主机名。

    bug #9: 原来只认 IP 字面量 + *.local + localhost，漏掉用裸主机名配置的 LAN 服务
    （如 http://nas:8096 / http://qnap-ts453d:8096）→ 被当公网走 Clash proxy → 502，
    正是本模块要防的场景。补一层 best-effort DNS 解析：解析到私网/环回/链路本地即 LAN。

    ⚠️ caveat: Clash/V2Ray 常劫持 DNS，被代理环境下 getaddrinfo 可能解析到假 IP 使此
    检查失效。终极方案是用户显式 LAN allowlist（留待后续）。
    """
    if not host:
        return False
    if host == "localhost" or host.endswith(".local"):
        return True
    # IP 字面量
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_private or ip.is_loopback or ip.is_link_local
    except ValueError:
        pass
    # 裸主机名 → best-effort DNS 解析（缓存成功结果）
    if host in _lan_cache:
        return _lan_cache[host]
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False  # 解析失败 → 当公网，但不缓存（下次重试）
    result = any(_ip_is_lan(info[4][0]) for info in infos if info[4])
    _lan_cache[host] = result
    return result


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").strip()
    except Exception:
        return ""


def apply_proxy_policy(session: requests.Session, url: str) -> None:
    """按 url 设 session.trust_env: LAN → False (直连), 公网 → True (走 env proxy)."""
    session.trust_env = not is_lan_host(_host_of(url))


def session_for(url: str) -> requests.Session:
    """构造新 Session, LAN host 自动 bypass env proxy."""
    session = requests.Session()
    apply_proxy_policy(session, url)
    return session
