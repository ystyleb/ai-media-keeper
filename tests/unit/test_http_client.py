"""services.http_client LAN-aware proxy bypass tests."""

import pytest

from services import http_client


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """默认禁用真实 DNS（避免单测网络依赖 + 不确定性）。需要解析的测试自行
    monkeypatch getaddrinfo 覆盖。每个测试清 _lan_cache 防污染。"""
    http_client._lan_cache.clear()

    def _fail(host, *a, **k):
        raise OSError("dns disabled in tests")

    monkeypatch.setattr(http_client.socket, "getaddrinfo", _fail)
    yield
    http_client._lan_cache.clear()


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


# ── bug #9: bare-hostname LAN host via DNS resolution ──


def test_is_lan_host_bare_hostname_resolving_to_private_ip(monkeypatch):
    """#9: http://nas:8096 这类裸主机名解析到私网 IP → 应判 LAN（直连不走代理）。"""
    monkeypatch.setattr(
        http_client.socket,
        "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("192.168.31.48", 0))],
    )
    assert http_client.is_lan_host("mynas") is True


def test_is_lan_host_bare_hostname_resolving_to_public_ip(monkeypatch):
    """裸主机名解析到公网 IP → 不该 bypass（保留外网代理）。"""
    monkeypatch.setattr(
        http_client.socket,
        "getaddrinfo",
        lambda host, *a, **k: [(2, 1, 6, "", ("8.8.8.8", 0))],
    )
    assert http_client.is_lan_host("cdn.example.com") is False


def test_is_lan_host_unresolvable_hostname_is_public_and_not_cached(monkeypatch):
    """解析失败 → 当公网，且不缓存（下次 DNS 恢复能重判）。"""
    calls = {"n": 0}

    def boom(host, *a, **k):
        calls["n"] += 1
        raise OSError("nxdomain")

    monkeypatch.setattr(http_client.socket, "getaddrinfo", boom)
    assert http_client.is_lan_host("nope.invalid") is False
    assert http_client.is_lan_host("nope.invalid") is False
    assert calls["n"] == 2  # 失败不缓存 → 两次都真去解析


def test_is_lan_host_caches_successful_resolution(monkeypatch):
    """成功解析结果缓存 → 第二次不再打 DNS。"""
    calls = {"n": 0}

    def once(host, *a, **k):
        calls["n"] += 1
        return [(2, 1, 6, "", ("10.0.0.9", 0))]

    monkeypatch.setattr(http_client.socket, "getaddrinfo", once)
    assert http_client.is_lan_host("homenas") is True
    assert http_client.is_lan_host("homenas") is True
    assert calls["n"] == 1  # 第二次命中缓存
