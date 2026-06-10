"""HTTP client wrapping NASVault Flask REST API.

复用现有 /api/* 路由 — 不引入额外业务逻辑，让 MCP server 仅作协议适配层。
所有契约 #1 destructive 双段式 / metadata 守门 / validate_path 校验都
通过 HTTP 一并继承。

Auth: 走 `Authorization: Bearer <API_TOKEN>` header（同 Web UI 模式）。
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlencode

import requests

from services import http_client


class NASClientError(RuntimeError):
    """HTTP / 协议错误的统一异常。"""

    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class NASClient:
    """Thin HTTP client for NASVault.

    - base_url: 默认 http://127.0.0.1:5001（与 install.sh / docker 默认一致）
    - api_token: 默认从环境变量 NAS_API_TOKEN 读
    - timeout: 默认 30s；destructive confirm 单独 60s（hardlink / NFO 写回可能慢）
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_token: str | None = None,
        timeout: float = 30.0,
    ):
        self.base_url = (
            base_url or os.environ.get("NAS_BASE_URL") or "http://127.0.0.1:5001"
        ).rstrip("/")
        self.api_token = api_token or os.environ.get("NAS_API_TOKEN", "")
        if not self.api_token:
            raise ValueError(
                "NAS_API_TOKEN not set (export NAS_API_TOKEN=<token from config/.api_token>)"
            )
        self.timeout = timeout
        # LAN-aware Session：默认 base_url 是 127.0.0.1，用户 shell 设了
        # http_proxy（Clash 等）时裸 Session 会把 loopback 请求也送进代理 → 502。
        # session_for 对 LAN/loopback host 自动 trust_env=False，公网 host 保持走代理。
        self.session = http_client.session_for(self.base_url)
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_token}",
                "Accept": "application/json",
            }
        )

    def _url(self, path: str, params: dict | None = None) -> str:
        url = f"{self.base_url}{path}"
        if params:
            # 过滤 None value（让调用方写 `limit=None` 表示不传）
            filtered = {k: v for k, v in params.items() if v is not None}
            if filtered:
                url = f"{url}?{urlencode(filtered)}"
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        url = self._url(path, params)
        try:
            resp = self.session.request(method, url, json=json, timeout=timeout or self.timeout)
        except requests.RequestException as e:
            raise NASClientError(f"network error: {e}") from e

        # 试 parse JSON; 不能 parse 时附 raw text 给上层
        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:500]}

        if resp.status_code >= 400:
            err_msg = body.get("error") or body.get("detail") or f"HTTP {resp.status_code}"
            raise NASClientError(
                f"{path} → HTTP {resp.status_code}: {err_msg}",
                status_code=resp.status_code,
                body=body,
            )
        return body

    # ─── Read tools ────────────────────────────────────────

    def list_files(self, path: str | None = None) -> dict:
        """GET /api/files — 列出目录内容。"""
        return self._request("GET", "/api/files", params={"path": path} if path else None)

    def list_videos(self, path: str, max_depth: int = 2, limit: int = 200) -> dict:
        """GET /api/metadata/list-videos — 列视频文件（含 has_nfo 标记）。"""
        return self._request(
            "GET",
            "/api/metadata/list-videos",
            params={"path": path, "max_depth": max_depth, "limit": limit},
        )

    def get_disk_usage(self) -> dict:
        """GET /api/disk — 磁盘容量。"""
        return self._request("GET", "/api/disk")

    def find_duplicates(
        self,
        media_type: str | None = None,
        watched_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """GET /api/dedup/groups — 重复 release 组列表（按 ROI 降序）。"""
        params: dict = {"limit": limit, "offset": offset}
        if media_type:
            params["media_type"] = media_type
        if watched_only:
            params["watched_only"] = "1"
        return self._request("GET", "/api/dedup/groups", params=params)

    def list_archive_candidates(self, days: int = 180, limit: int = 50, offset: int = 0) -> dict:
        """GET /api/library/watched-stale — 已看完 + N 天未动的媒体（候选删除）。"""
        return self._request(
            "GET",
            "/api/library/watched-stale",
            params={"days": days, "limit": limit, "offset": offset},
        )

    def find_recent_downloads(
        self, path: str | None = None, max_depth: int = 2, limit: int = 50
    ) -> dict:
        """复用 list_videos 拿目录下视频（按文件创建时间排序留前端做）。

        没有专门的 /api/recent 路由 — 复用 list-videos。调用方传 downloads 目录路径
        （如 /share/Download/qBit），按 has_nfo=False 过滤即可识别"刚下载未识别"。
        """
        if not path:
            raise NASClientError("path required (e.g. /share/Download/qBit)")
        return self.list_videos(path, max_depth=max_depth, limit=limit)

    # ─── Destructive tools (契约 #1 双段式) ────────────────

    def preview_action(self, kind: str, **kwargs) -> dict:
        """POST /api/action/preview — 第一段：生成 preview snapshot + signed_token。

        Args:
            kind: 'delete' | 'organize' | 'archive' | 'nfo_write' | 'purge_provider'
            **kwargs: kind-specific payload（如 delete: paths/candidates/source/snapshot_mode；
                      organize: items；nfo_write: target/payload；archive: id 等）
        """
        body = {"kind": kind, **kwargs}
        return self._request("POST", "/api/action/preview", json=body)

    def confirm_action(self, action_id: str, signed_token: str) -> dict:
        """POST /api/action/confirm — 第二段：用 signed_token 真正执行。

        organize + items>5 时服务端会返 202 + polling_url（仍是 dict），调用方
        自己再调 action_status() 轮询。
        """
        return self._request(
            "POST",
            "/api/action/confirm",
            json={"action_id": action_id, "signed_token": signed_token},
            timeout=60.0,  # 6 状态机里 organize / nfo_write 同步路径可能慢
        )

    def action_status(self, action_id: str) -> dict:
        """GET /api/action/status — 轮询 background organize 进度。"""
        return self._request("GET", "/api/action/status", params={"id": action_id})

    def providers_status(self, refresh: bool = False) -> dict:
        """GET /api/providers/status — 4 provider 当前可用性（用于 health-check）。"""
        params = {"refresh": "1"} if refresh else None
        return self._request("GET", "/api/providers/status", params=params)
