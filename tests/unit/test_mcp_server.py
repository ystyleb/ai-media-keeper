"""MCP server unit tests — mock HTTP layer，确认 9 个 tool 正确 wrap REST。

测试策略：
- NASClient 用 responses-mock 拦截 HTTP 请求（不真启 Flask）
- dispatch_tool_call 路由正确性 + 参数透传
- ValueError vs NASClientError 错误分类
- 契约 #1 双段式 prepare → confirm 流程

不测 stdio MCP framing（那是 SDK 责任）；测我们写的业务适配层。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from mcp_server.client import NASClient, NASClientError
from mcp_server.tools import TOOL_DEFINITIONS, dispatch_tool_call


@pytest.fixture
def fake_token(monkeypatch):
    monkeypatch.setenv("NAS_API_TOKEN", "fake-test-token")
    monkeypatch.setenv("NAS_BASE_URL", "http://127.0.0.1:5001")


@pytest.fixture
def client(fake_token) -> NASClient:
    return NASClient()


def _mock_resp(status_code: int = 200, json_data: dict | None = None) -> MagicMock:
    """构造 fake requests.Response。"""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = "" if json_data else ""
    return resp


# ─── Init / auth tests ──────────────────────────────────────


def test_client_requires_token(monkeypatch):
    monkeypatch.delenv("NAS_API_TOKEN", raising=False)
    with pytest.raises(ValueError, match="NAS_API_TOKEN"):
        NASClient()


def test_client_uses_env_token(fake_token):
    c = NASClient()
    assert c.api_token == "fake-test-token"
    assert c.base_url == "http://127.0.0.1:5001"


def test_client_constructor_override_token(fake_token):
    c = NASClient(api_token="explicit", base_url="http://other:9999")
    assert c.api_token == "explicit"
    assert c.base_url == "http://other:9999"


def test_client_strips_trailing_slash(fake_token):
    c = NASClient(base_url="http://host:5001/")
    assert c.base_url == "http://host:5001"


def test_client_session_bypasses_env_proxy_for_loopback(fake_token):
    """Regression: 默认 base_url 是 127.0.0.1 — Session 必须 trust_env=False，
    否则用户 shell 的 http_proxy（Clash 等）会把 loopback 请求送进代理 → 502。"""
    c = NASClient()
    assert c.session.trust_env is False


def test_client_session_keeps_env_proxy_for_public_host(fake_token):
    """公网 base_url 仍走 env proxy（用 IP 字面量避免单测触发真实 DNS）。"""
    c = NASClient(base_url="http://93.184.216.34:8080")
    assert c.session.trust_env is True


# ─── Read tools ─────────────────────────────────────────────


def test_list_files_proxies_to_api(client):
    fake = _mock_resp(200, {"files": [{"name": "a.mkv"}]})
    with patch.object(client.session, "request", return_value=fake) as m:
        result = client.list_files(path="/share/foo")
    assert result == {"files": [{"name": "a.mkv"}]}
    args, kwargs = m.call_args
    assert args[0] == "GET"
    assert "/api/files?path=" in args[1]


def test_get_disk_usage(client):
    fake = _mock_resp(200, {"disks": [{"mount": "/share/CACHEDEV2_DATA"}]})
    with patch.object(client.session, "request", return_value=fake) as m:
        client.get_disk_usage()
    assert m.call_args[0][1].endswith("/api/disk")


def test_find_duplicates_query_params(client):
    fake = _mock_resp(200, {"groups": []})
    with patch.object(client.session, "request", return_value=fake) as m:
        client.find_duplicates(media_type="movie", watched_only=True, limit=20)
    url = m.call_args[0][1]
    assert "media_type=movie" in url
    assert "watched_only=1" in url
    assert "limit=20" in url


def test_find_duplicates_no_optional_params(client):
    fake = _mock_resp(200, {"groups": []})
    with patch.object(client.session, "request", return_value=fake) as m:
        client.find_duplicates()
    url = m.call_args[0][1]
    assert "media_type" not in url  # 不传 media_type 时不该出现
    assert "watched_only" not in url


def test_list_archive_candidates(client):
    fake = _mock_resp(200, {"items": [], "total": 0})
    with patch.object(client.session, "request", return_value=fake) as m:
        client.list_archive_candidates(days=90)
    assert "days=90" in m.call_args[0][1]


def test_find_recent_downloads_requires_path(client):
    with pytest.raises(NASClientError, match="path required"):
        client.find_recent_downloads()


def test_find_recent_downloads_proxies_to_list_videos(client):
    fake = _mock_resp(200, {"videos": []})
    with patch.object(client.session, "request", return_value=fake) as m:
        client.find_recent_downloads(path="/share/Download", limit=10)
    url = m.call_args[0][1]
    assert "/api/metadata/list-videos" in url
    assert "limit=10" in url


# ─── Destructive tools (契约 #1) ────────────────────────────


def test_preview_action_sends_json_body(client):
    fake = _mock_resp(200, {"action_id": "abc", "signed_token": "xyz"})
    with patch.object(client.session, "request", return_value=fake) as m:
        client.preview_action(kind="delete", source="dedup", candidates=[{"path": "/x.mkv"}])
    kwargs = m.call_args.kwargs
    assert kwargs["json"]["kind"] == "delete"
    assert kwargs["json"]["source"] == "dedup"
    assert kwargs["json"]["candidates"][0]["path"] == "/x.mkv"


def test_confirm_action(client):
    fake = _mock_resp(200, {"action_id": "abc", "status": "succeeded"})
    with patch.object(client.session, "request", return_value=fake) as m:
        client.confirm_action("abc", "token-xyz")
    kwargs = m.call_args.kwargs
    assert kwargs["json"] == {"action_id": "abc", "signed_token": "token-xyz"}
    # confirm 用更长 timeout 防 hardlink/NFO 写慢
    assert kwargs["timeout"] == 60.0


def test_action_status_polling(client):
    fake = _mock_resp(200, {"action_id": "abc", "status": "running"})
    with patch.object(client.session, "request", return_value=fake) as m:
        client.action_status("abc")
    assert "/api/action/status" in m.call_args[0][1]
    assert "id=abc" in m.call_args[0][1]


# ─── HTTP error handling ────────────────────────────────────


def test_404_raises_clientservererror(client):
    fake = _mock_resp(404, {"error": "action_not_found", "action_id": "abc"})
    with patch.object(client.session, "request", return_value=fake):
        with pytest.raises(NASClientError) as exc:
            client.action_status("abc")
    assert exc.value.status_code == 404
    assert "action_not_found" in str(exc.value)
    assert exc.value.body["error"] == "action_not_found"


def test_500_propagates_with_body(client):
    fake = _mock_resp(500, {"error": "internal"})
    with patch.object(client.session, "request", return_value=fake):
        with pytest.raises(NASClientError) as exc:
            client.get_disk_usage()
    assert exc.value.status_code == 500


def test_network_error_wrapped_as_clienterror(client):
    with patch.object(
        client.session,
        "request",
        side_effect=requests.ConnectionError("refused"),
    ):
        with pytest.raises(NASClientError, match="network error"):
            client.get_disk_usage()


def test_non_json_response_returns_raw_text(client):
    fake = MagicMock()
    fake.status_code = 200
    fake.json.side_effect = ValueError("not json")
    fake.text = "Plain text body"
    with patch.object(client.session, "request", return_value=fake):
        result = client.list_files()
    assert "raw" in result
    assert "Plain text body" in result["raw"]


# ─── Tool dispatch ──────────────────────────────────────────


def test_unknown_tool_raises(client):
    with pytest.raises(ValueError, match="unknown tool"):
        dispatch_tool_call(client, "no_such_tool", {})


def test_dispatch_list_files(client):
    fake = _mock_resp(200, {"files": []})
    with patch.object(client.session, "request", return_value=fake):
        result = dispatch_tool_call(client, "list_files", {"path": "/x"})
    assert result == {"files": []}


def test_dispatch_prepare_destructive_action_passes_payload(client):
    """Critical: kind + payload 必须正确透传，否则破坏契约 #1。"""
    fake = _mock_resp(200, {"action_id": "abc", "signed_token": "xyz"})
    with patch.object(client.session, "request", return_value=fake) as m:
        dispatch_tool_call(
            client,
            "prepare_destructive_action",
            {
                "kind": "delete",
                "payload": {
                    "source": "dedup",
                    "snapshot_mode": "strict",
                    "candidates": [
                        {
                            "path": "/a",
                            "expected_inode": 1,
                            "expected_size": 100,
                            "expected_mtime": 1000,
                        }
                    ],
                },
            },
        )
    body = m.call_args.kwargs["json"]
    assert body["kind"] == "delete"
    assert body["source"] == "dedup"
    assert body["snapshot_mode"] == "strict"
    assert body["candidates"][0]["expected_inode"] == 1


def test_dispatch_prepare_missing_kind_raises(client):
    with pytest.raises(ValueError, match="kind required"):
        dispatch_tool_call(client, "prepare_destructive_action", {})


def test_dispatch_prepare_invalid_payload_type_raises(client):
    with pytest.raises(ValueError, match="payload must be object"):
        dispatch_tool_call(
            client,
            "prepare_destructive_action",
            {
                "kind": "delete",
                "payload": "not-a-dict",
            },
        )


def test_dispatch_confirm_missing_args_raises(client):
    with pytest.raises(ValueError, match="action_id and signed_token required"):
        dispatch_tool_call(client, "confirm_destructive_action", {"action_id": "x"})


def test_dispatch_action_status_missing_id(client):
    with pytest.raises(ValueError, match="action_id required"):
        dispatch_tool_call(client, "action_status", {})


# ─── Tool definitions metadata ──────────────────────────────


def test_all_9_tools_have_complete_schemas():
    """每个 tool 必须有 name + description + inputSchema 三段；schema 必须 valid JSONSchema dict。"""
    assert len(TOOL_DEFINITIONS) == 9, f"expected 9 tools, got {len(TOOL_DEFINITIONS)}"
    seen_names = set()
    for tool in TOOL_DEFINITIONS:
        assert "name" in tool
        assert "description" in tool
        assert "inputSchema" in tool
        assert tool["name"] not in seen_names, f"duplicate tool name: {tool['name']}"
        seen_names.add(tool["name"])
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "required" in schema


def test_destructive_tools_have_required_fields():
    """prepare / confirm 必填字段不可省。"""
    prepare = next(t for t in TOOL_DEFINITIONS if t["name"] == "prepare_destructive_action")
    assert prepare["inputSchema"]["required"] == ["kind", "payload"]
    confirm = next(t for t in TOOL_DEFINITIONS if t["name"] == "confirm_destructive_action")
    assert set(confirm["inputSchema"]["required"]) == {"action_id", "signed_token"}


def test_kind_enum_matches_backend_check_constraint():
    """contract test: prepare 的 kind enum 必须跟 db/schema.sql CHECK 一致。"""
    prepare = next(t for t in TOOL_DEFINITIONS if t["name"] == "prepare_destructive_action")
    kinds = set(prepare["inputSchema"]["properties"]["kind"]["enum"])
    # 与 destructive_actions.kind CHECK 必须完全一致（Phase 4A migration 0003 加 organize）
    assert kinds == {"delete", "organize", "nfo_write", "archive", "purge_provider"}
