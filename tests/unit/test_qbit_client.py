"""QBitClient 线程安全 + 重认证测试（bug #7 #8）。

- #8: cookie 过期/qBit 重启后 401/403 → 重置登录态 + 重登 + 重试一次（不再永久失败）
- #7: 跨线程共享 session 用 RLock 串行化（结构性检查）
"""

from __future__ import annotations

import threading

import requests

import app as app_module


class _FakeResp:
    def __init__(self, code, payload=None, text="Ok."):
        self.status_code = code
        self._payload = payload if payload is not None else []
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeCookies:
    def clear(self):
        pass


class _FakeSession:
    """模拟 qBit session：第 1 次 torrents/info 返 403（cookie 过期），重登后返 200。"""

    def __init__(self):
        self.cookies = _FakeCookies()
        self.login_calls = 0
        self.info_calls = 0

    def post(self, url, **kw):
        if url.endswith("/auth/login"):
            self.login_calls += 1
            return _FakeResp(200, text="Ok.")
        return _FakeResp(200)

    def _handle(self, method, url, **kw):
        if url.endswith("/torrents/info"):
            self.info_calls += 1
            if self.info_calls == 1:
                return _FakeResp(403)  # 过期 cookie
            return _FakeResp(200, payload=[{"hash": "h1"}])
        if url.endswith("/auth/login"):
            self.login_calls += 1
            return _FakeResp(200, text="Ok.")
        return _FakeResp(200, payload=[])

    # 兼容当前代码（.get/.post）与重构后（.request）
    def get(self, url, **kw):
        return self._handle("GET", url, **kw)

    def request(self, method, url, **kw):
        return self._handle(method, url, **kw)


def _make_client():
    qc = app_module.QBitClient()
    qc._config = {"url": "http://nas:8080", "user": "admin", "password": "pw"}
    qc.session = _FakeSession()
    return qc


def test_qbit_reauth_on_403_cookie_expiry():
    """#8: 已登录态下 cookie 过期 → get_torrents 收 403 → 重登 + 重试 → 成功返数据。"""
    qc = _make_client()
    qc._logged_in = True  # 假装已登录但 cookie 实际过期
    result = qc.get_torrents()
    assert result == [{"hash": "h1"}]
    assert qc.session.login_calls == 1  # 收 403 后重登了一次
    assert qc.session.info_calls == 2  # 重试了一次


def test_qbit_has_reentrant_lock():
    """#7: QBitClient 必须有 RLock 串行化跨线程（cron + 请求线程）的共享 session 访问。"""
    qc = _make_client()
    assert hasattr(qc, "_lock")
    # RLock 实例（允许 find_torrents_by_paths→get_torrents 重入）
    assert isinstance(qc._lock, type(threading.RLock()))
