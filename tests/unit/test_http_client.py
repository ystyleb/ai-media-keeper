"""services.http_client LAN-aware proxy bypass tests."""

import pytest

from services import http_client


@pytest.mark.parametrize(
    "host,expected",
    [
        # private IPv4
        ("192.168.31.48", True),
        ("10.0.0.5", True),
        ("172.16.0.1", True),
        ("172.31.255.254", True),
        # loopback
        ("127.0.0.1", True),
        ("localhost", True),
        # link-local
        ("169.254.10.210", True),
        # mDNS / hostname.local
        ("winson-nas.local", True),
        ("emby.local", True),
        # IPv6 loopback / link-local
        ("::1", True),
        ("fe80::1", True),
        # public IPv4 — 不该 bypass
        ("8.8.8.8", False),
        ("1.1.1.1", False),
        ("172.32.0.1", False),  # 边界外 (172.16-31 才是 private)
        # public hostname — 不该 bypass (避免错误关掉外网代理)
        ("api.themoviedb.org", False),
        ("github.com", False),
        # empty / 无效
        ("", False),
        ("not a host!", False),
    ],
)
def test_is_lan_host(host, expected):
    assert http_client.is_lan_host(host) is expected


@pytest.mark.parametrize(
    "url,expected_trust_env",
    [
        ("http://192.168.31.48:8096", False),  # LAN → bypass proxy
        ("http://localhost:6363", False),
        ("http://winson-nas.local:8096", False),
        ("https://api.themoviedb.org/3/movie", True),  # 公网 → 走 proxy
        ("https://api.deepseek.com/v1/chat", True),
        ("", True),  # 空 url 不知道 host → 保留默认 (走 env proxy)
    ],
)
def test_session_for_trust_env(url, expected_trust_env):
    session = http_client.session_for(url)
    assert session.trust_env is expected_trust_env


def test_apply_proxy_policy_mutates_existing_session():
    import requests

    s = requests.Session()
    assert s.trust_env is True  # default
    http_client.apply_proxy_policy(s, "http://192.168.1.1")
    assert s.trust_env is False
    http_client.apply_proxy_policy(s, "https://api.themoviedb.org")
    assert s.trust_env is True
