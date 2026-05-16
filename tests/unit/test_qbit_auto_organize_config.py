"""Phase 4C.0: qBit auto-organize config helper 单元测试。

只覆盖 load_qbit_auto_organize_config + save_qbit_auto_organize_config 的边界清洗。
Routes（GET/POST /api/config/qbit-auto-organize）4C.5 才加。
"""

from __future__ import annotations

import json

import pytest

import app as app_module


@pytest.fixture
def fake_cfg(tmp_path, monkeypatch):
    cfg_file = tmp_path / "qbit_auto_organize.json"
    monkeypatch.setattr(app_module, "QBIT_AUTO_ORGANIZE_CONFIG_FILE", cfg_file)
    return cfg_file


# ── load ───


def test_load_missing_file_returns_defaults(fake_cfg):
    cfg = app_module.load_qbit_auto_organize_config()
    assert cfg["enabled"] is False
    assert cfg["categories"] == []
    assert cfg["poll_interval_minutes"] == 5
    assert cfg["confidence_threshold"] == 0.85


def test_load_partial_file_merges_with_defaults(fake_cfg):
    fake_cfg.write_text(json.dumps({"enabled": True, "categories": ["Movies"]}))
    cfg = app_module.load_qbit_auto_organize_config()
    assert cfg["enabled"] is True
    assert cfg["categories"] == ["Movies"]
    # 未指定字段走默认
    assert cfg["poll_interval_minutes"] == 5
    assert cfg["confidence_threshold"] == 0.85


def test_load_invalid_json_returns_defaults(fake_cfg):
    fake_cfg.write_text("{{ not valid json")
    cfg = app_module.load_qbit_auto_organize_config()
    assert cfg == app_module.QBIT_AUTO_ORGANIZE_DEFAULTS


def test_load_clamps_poll_interval_min_1(fake_cfg):
    """poll_interval_minutes < 1 → 强制 1（防压垮 qBit API）。"""
    fake_cfg.write_text(json.dumps({"poll_interval_minutes": 0}))
    cfg = app_module.load_qbit_auto_organize_config()
    assert cfg["poll_interval_minutes"] == 1
    fake_cfg.write_text(json.dumps({"poll_interval_minutes": -10}))
    assert app_module.load_qbit_auto_organize_config()["poll_interval_minutes"] == 1


def test_load_clamps_confidence_threshold_to_0_to_1(fake_cfg):
    """confidence 越界 → clamp 到 [0,1]。"""
    fake_cfg.write_text(json.dumps({"confidence_threshold": 1.5}))
    assert app_module.load_qbit_auto_organize_config()["confidence_threshold"] == 1.0
    fake_cfg.write_text(json.dumps({"confidence_threshold": -0.5}))
    assert app_module.load_qbit_auto_organize_config()["confidence_threshold"] == 0.0


def test_load_handles_non_list_categories(fake_cfg):
    """categories 非 list（用户误配字符串） → fallback 空 list 不抛错。"""
    fake_cfg.write_text(json.dumps({"categories": "Movies"}))  # 字符串而非 list
    cfg = app_module.load_qbit_auto_organize_config()
    assert cfg["categories"] == []


def test_load_strips_empty_categories(fake_cfg):
    """空字符串 / 全空白 category 被过滤；保留有效项 + strip 空白。"""
    fake_cfg.write_text(json.dumps({"categories": ["Movies", "", "  ", "  TV  ", None]}))
    cfg = app_module.load_qbit_auto_organize_config()
    assert cfg["categories"] == ["Movies", "TV"]


def test_load_invalid_types_fall_back_to_defaults(fake_cfg):
    """非数值的 poll_interval / confidence → 静默 fallback 不抛 TypeError。"""
    fake_cfg.write_text(
        json.dumps(
            {
                "poll_interval_minutes": "not-a-number",
                "confidence_threshold": None,
            }
        )
    )
    cfg = app_module.load_qbit_auto_organize_config()
    assert cfg["poll_interval_minutes"] == 5
    assert cfg["confidence_threshold"] == 0.85


# ── save ───


def test_save_writes_only_4_schema_fields(fake_cfg):
    """save 拒绝写额外字段（防 schema bloat）。"""
    app_module.save_qbit_auto_organize_config(
        {
            "enabled": True,
            "categories": ["Movies"],
            "poll_interval_minutes": 10,
            "confidence_threshold": 0.9,
            "extra_field": "should_not_persist",  # 不该出现在文件
        }
    )
    saved = json.loads(fake_cfg.read_text())
    assert set(saved.keys()) == {
        "enabled",
        "categories",
        "poll_interval_minutes",
        "confidence_threshold",
    }


def test_save_round_trip(fake_cfg):
    app_module.save_qbit_auto_organize_config(
        {
            "enabled": True,
            "categories": ["Movies", "TV"],
            "poll_interval_minutes": 15,
            "confidence_threshold": 0.92,
        }
    )
    cfg = app_module.load_qbit_auto_organize_config()
    assert cfg["enabled"] is True
    assert cfg["categories"] == ["Movies", "TV"]
    assert cfg["poll_interval_minutes"] == 15
    assert cfg["confidence_threshold"] == 0.92


def test_save_clamps_poll_interval(fake_cfg):
    """save 阶段也 clamp（防止 UI bypass load 边界）。"""
    app_module.save_qbit_auto_organize_config(
        {
            "enabled": False,
            "categories": [],
            "poll_interval_minutes": 0,
            "confidence_threshold": 0.85,
        }
    )
    assert json.loads(fake_cfg.read_text())["poll_interval_minutes"] == 1


def test_save_clamps_confidence(fake_cfg):
    app_module.save_qbit_auto_organize_config(
        {
            "enabled": False,
            "categories": [],
            "poll_interval_minutes": 5,
            "confidence_threshold": 2.0,
        }
    )
    assert json.loads(fake_cfg.read_text())["confidence_threshold"] == 1.0


def test_save_defaults_when_fields_missing(fake_cfg):
    """完全空 dict 也能 save（不抛 KeyError），全走默认。"""
    app_module.save_qbit_auto_organize_config({})
    saved = json.loads(fake_cfg.read_text())
    assert saved["enabled"] is False
    assert saved["categories"] == []
    assert saved["poll_interval_minutes"] == 5
    assert saved["confidence_threshold"] == 0.85
