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
from urllib.parse import urlparse

import requests


def is_lan_host(host: str) -> bool:
    """LAN 判定: private IP / loopback / link-local / *.local / 'localhost'."""
    if not host:
        return False
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


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
