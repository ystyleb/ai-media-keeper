"""LLM filename rescue 单元测试 — 中文 release 等 guessit 拿不到 title 的场景。"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from services import llm


def _make_openai_mock(response_text: str):
    fake_choice = MagicMock()
    fake_choice.message.content = response_text
    fake_resp = MagicMock()
    fake_resp.choices = [fake_choice]
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_resp
    fake_class = MagicMock(return_value=fake_client)
    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = fake_class
    return fake_module


def test_extract_title_happy_path():
    fake = _make_openai_mock(json.dumps({
        "title": "死亡笔记", "alt_title": "Death Note",
        "year": 2006, "season": 1, "episode": None, "media_type": "tv",
    }))
    with patch.dict(sys.modules, {"openai": fake}):
        ext = llm.extract_title_from_filename(
            "死亡笔记.BDrip1080P.X264.AC3.LGGZ S.01.mkv",
            api_key="fake-key",
        )
    assert ext is not None
    assert ext.title == "死亡笔记"
    assert ext.alt_title == "Death Note"
    assert ext.year == 2006
    assert ext.media_type == "tv"


def test_extract_title_returns_null_when_unrecognizable():
    fake = _make_openai_mock(json.dumps({"title": None}))
    with patch.dict(sys.modules, {"openai": fake}):
        ext = llm.extract_title_from_filename(
            "random.gibberish.mkv", api_key="fake-key"
        )
    assert ext is None


def test_extract_title_empty_string_treated_as_null():
    fake = _make_openai_mock(json.dumps({"title": "", "media_type": "unknown"}))
    with patch.dict(sys.modules, {"openai": fake}):
        ext = llm.extract_title_from_filename("noise.mkv", api_key="fake-key")
    assert ext is None


def test_extract_title_no_api_key_returns_none():
    assert llm.extract_title_from_filename("anything.mkv", api_key="") is None


def test_extract_title_malformed_json_returns_none():
    fake = _make_openai_mock("not valid json at all")
    with patch.dict(sys.modules, {"openai": fake}):
        ext = llm.extract_title_from_filename("anything.mkv", api_key="fake-key")
    assert ext is None


def test_extract_title_markdown_fence_tolerated():
    """DeepSeek 偶发返回 ```json ... ``` 包裹的 JSON，要能解析。"""
    response = """```json
{"title": "庆余年", "year": 2019, "season": 2, "episode": 1, "media_type": "tv"}
```"""
    fake = _make_openai_mock(response)
    with patch.dict(sys.modules, {"openai": fake}):
        ext = llm.extract_title_from_filename(
            "庆余年.S02E01.1080p.WEB-DL.mkv", api_key="fake-key"
        )
    assert ext is not None
    assert ext.title == "庆余年"
    assert ext.year == 2019
    assert ext.season == 2


def test_extract_title_invalid_media_type_falls_to_unknown():
    fake = _make_openai_mock(json.dumps({
        "title": "Something", "media_type": "tv_or_movie",  # invalid enum
    }))
    with patch.dict(sys.modules, {"openai": fake}):
        ext = llm.extract_title_from_filename("x.mkv", api_key="fake-key")
    assert ext is not None
    assert ext.media_type == "unknown"


def test_extract_title_non_int_year_handled():
    fake = _make_openai_mock(json.dumps({
        "title": "X", "year": "not a number", "media_type": "movie",
    }))
    with patch.dict(sys.modules, {"openai": fake}):
        ext = llm.extract_title_from_filename("x.mkv", api_key="fake-key")
    assert ext is not None
    assert ext.year is None  # parse failure → None, not crash


def test_extract_title_network_error_returns_none():
    fake_class = MagicMock(side_effect=RuntimeError("connection timeout"))
    fake_module = types.ModuleType("openai")
    fake_module.OpenAI = fake_class
    with patch.dict(sys.modules, {"openai": fake_module}):
        ext = llm.extract_title_from_filename("x.mkv", api_key="fake-key")
    assert ext is None
