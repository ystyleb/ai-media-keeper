"""LLM grounded selection 单元测试 — 契约 #3 enforcement。

mock anthropic SDK，专注验证 4 个关键 case：
  1. happy path: LLM 返回 valid id → 接受
  2. 伪造 id: LLM 返回 candidates 里不存在的 id → 强制拒绝（防幻觉）
  3. 低 confidence: 即使 selected 合法但 confidence < 0.7 → needs_review
  4. malformed JSON / network error → 优雅 fallback 不崩溃
"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from services import llm


def _make_anthropic_mock(response_text: str):
    """构造 mock anthropic.Anthropic 类，调 messages.create 返指定 text。"""
    fake_msg = MagicMock()
    fake_msg.content = [MagicMock(text=response_text)]
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_msg
    fake_class = MagicMock(return_value=fake_client)
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = fake_class
    return fake_module


PARSE_SAMPLE = {"title": "Rick and Morty", "season": 5, "episode": 1, "media_type": "episode"}
CANDIDATES_SAMPLE = [
    {"id": "tmdb:tv:60625", "title": "瑞克和莫蒂", "original_title": "Rick and Morty",
     "year": 2013, "media_type": "tv", "overview": "Mad scientist Rick..."},
    {"id": "tmdb:tv:202559", "title": "瑞克和莫蒂：日漫版", "original_title": "Rick and Morty: The Anime",
     "year": 2024, "media_type": "tv", "overview": "Anime spin-off..."},
]


def test_happy_path_valid_selection():
    fake = _make_anthropic_mock(json.dumps({
        "selected": "tmdb:tv:60625",
        "confidence": 0.95,
        "reasoning": "original_title exact match",
    }))
    with patch.dict(sys.modules, {"anthropic": fake}):
        result = llm.select_candidate(
            PARSE_SAMPLE, CANDIDATES_SAMPLE, api_key="fake-key"
        )
    assert result.selected_id == "tmdb:tv:60625"
    assert result.confidence == 0.95
    assert "exact" in result.reasoning


def test_fabricated_id_rejected():
    """LLM 返回 candidates 里不存在的 id → 必须拒绝（契约 #3 hard rule）。"""
    fake = _make_anthropic_mock(json.dumps({
        "selected": "tmdb:tv:99999999",  # 假的，不在 candidates 里
        "confidence": 0.99,
        "reasoning": "I made this up",
    }))
    with patch.dict(sys.modules, {"anthropic": fake}):
        result = llm.select_candidate(
            PARSE_SAMPLE, CANDIDATES_SAMPLE, api_key="fake-key"
        )
    assert result.selected_id is None
    assert result.confidence == 0.0
    assert "fabricated_id_rejected" in result.reasoning


def test_low_confidence_returns_none():
    """selected 合法但 confidence < 0.7 → needs_review。"""
    fake = _make_anthropic_mock(json.dumps({
        "selected": "tmdb:tv:60625",
        "confidence": 0.5,
        "reasoning": "not sure, could be either",
    }))
    with patch.dict(sys.modules, {"anthropic": fake}):
        result = llm.select_candidate(
            PARSE_SAMPLE, CANDIDATES_SAMPLE, api_key="fake-key"
        )
    assert result.selected_id is None
    assert result.confidence == 0.5
    assert "not sure" in result.reasoning


def test_null_selection_returns_none():
    """LLM 主动返 selected=null → needs_review。"""
    fake = _make_anthropic_mock(json.dumps({
        "selected": None,
        "confidence": 0.6,
        "reasoning": "title mismatch",
    }))
    with patch.dict(sys.modules, {"anthropic": fake}):
        result = llm.select_candidate(
            PARSE_SAMPLE, CANDIDATES_SAMPLE, api_key="fake-key"
        )
    assert result.selected_id is None


def test_malformed_json_graceful_fallback():
    fake = _make_anthropic_mock("not valid json at all {")
    with patch.dict(sys.modules, {"anthropic": fake}):
        result = llm.select_candidate(
            PARSE_SAMPLE, CANDIDATES_SAMPLE, api_key="fake-key"
        )
    assert result.selected_id is None
    assert "malformed_json" in result.reasoning


def test_markdown_fenced_json_accepted():
    """LLM 偶发会在 JSON 外套 ```json ... ``` ——应该容忍解析。"""
    fake = _make_anthropic_mock("""Here's my answer:
```json
{"selected": "tmdb:tv:60625", "confidence": 0.9, "reasoning": "exact match"}
```
""")
    with patch.dict(sys.modules, {"anthropic": fake}):
        result = llm.select_candidate(
            PARSE_SAMPLE, CANDIDATES_SAMPLE, api_key="fake-key"
        )
    assert result.selected_id == "tmdb:tv:60625"


def test_network_error_graceful_fallback():
    fake_class = MagicMock(side_effect=RuntimeError("connection timeout"))
    fake_module = types.ModuleType("anthropic")
    fake_module.Anthropic = fake_class
    with patch.dict(sys.modules, {"anthropic": fake_module}):
        result = llm.select_candidate(
            PARSE_SAMPLE, CANDIDATES_SAMPLE, api_key="fake-key"
        )
    assert result.selected_id is None
    assert "llm_error" in result.reasoning


def test_no_candidates_short_circuits():
    """空 candidates → 不调 LLM，立刻返 None。"""
    fake = _make_anthropic_mock("should not be called")
    with patch.dict(sys.modules, {"anthropic": fake}):
        result = llm.select_candidate(PARSE_SAMPLE, [], api_key="fake-key")
    assert result.selected_id is None
    assert result.reasoning == "no_candidates"
    fake.Anthropic.assert_not_called()


def test_no_api_key_short_circuits():
    """空 api_key → 不调 LLM。"""
    result = llm.select_candidate(PARSE_SAMPLE, CANDIDATES_SAMPLE, api_key="")
    assert result.selected_id is None
    assert result.reasoning == "no_api_key"


def test_anthropic_sdk_missing_graceful():
    """anthropic SDK 没装 → 不崩，返 needs_review。"""
    import builtins
    real_import = builtins.__import__

    def deny_anthropic(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", side_effect=deny_anthropic):
        result = llm.select_candidate(
            PARSE_SAMPLE, CANDIDATES_SAMPLE, api_key="fake-key"
        )
    assert result.selected_id is None
    assert "sdk_missing" in result.reasoning


# ─── load_api_key tests ──────────────────────────────────────


def test_load_api_key_env_priority(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    f = tmp_path / ".anthropic_key"
    f.write_text("file-key")
    assert llm.load_api_key(f) == "env-key"


def test_load_api_key_file_fallback(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    f = tmp_path / ".anthropic_key"
    f.write_text("file-key\n")
    assert llm.load_api_key(f) == "file-key"


def test_load_api_key_empty(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert llm.load_api_key(tmp_path / "nonexistent") == ""
