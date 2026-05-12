"""LLM grounded selection — 契约 #3 唯一出口。

**Single call site rule** (plan v3 R2-I5):
Any other module that needs LLM **must** route through this file.
Direct `import anthropic` outside `services/llm.py` is a code review
red flag — the Anthropic SDK is intentionally imported lazily here
so the rest of the project never touches it.

Reference: ~/.claude/plans/ai-native-mutable-bubble.md 契约 #3
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# CLAUDE.md 偏好：Claude 4.x 系列最新。Haiku 4.5 在中文识别上够用 + 便宜
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# 契约 #3 prompt template（v1，单一注册名，禁止其他模块自定义）
SELECT_PROMPT_V1 = """You are matching a video file to its TMDB entry. Given the parsed filename info and a list of TMDB candidates, pick the best match.

**Hard rules**:
- You MUST return one candidate's exact `id` from the list, OR `null` if no match.
- Never invent an id. The `id` strings below are the ONLY valid options.
- If `confidence < 0.7`, return `selected: null` — the system will treat that as needs_review.
- The filename may be in any language (English title for Chinese show, romaji for Japanese, etc); the candidates' `title` may be localized (e.g. zh-CN). Compare against both `title` AND `original_title`.

Parsed filename info:
{parse_json}

Candidates (you MUST pick one of these `id`s or null):
{candidates_json}

Return strict JSON ONLY (no markdown, no commentary):
{{
  "selected": "<exact id from candidates>" or null,
  "confidence": 0.0 to 1.0,
  "reasoning": "<one short sentence why>"
}}
"""


@dataclass(frozen=True)
class LLMSelection:
    selected_id: str | None       # 必 ∈ {c.id for c in candidates}，否则置 None
    confidence: float             # 0.0-1.0
    reasoning: str                # 简短解释
    raw_response: str             # provenance：原始 LLM 输出，便于 audit


def load_api_key(config_path: Path | str | None = None) -> str:
    """env > config 文件 > 空。同 .api_token / .tmdb_key 模式。"""
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env:
        return env
    if config_path:
        p = Path(config_path)
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return ""


def is_available(config_path: Path | str | None = None) -> bool:
    return bool(load_api_key(config_path))


def select_candidate(
    parse: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 30.0,
) -> LLMSelection:
    """grounded selection。永远不让 LLM 编 id；output schema enforce 后失败 → needs_review。

    Args:
        parse: filename 解析结果（含 title/year/season/episode/...）
        candidates: provider 返回的候选 dict 列表，必须含 'id'、'title'
        api_key: BYOK Anthropic key
        model: 默认 claude-haiku-4-5-20251001（CLAUDE.md 偏好）
    """
    if not candidates:
        return LLMSelection(None, 0.0, "no_candidates", "")
    if not api_key:
        return LLMSelection(None, 0.0, "no_api_key", "")

    # 懒 import，让项目不强依赖 anthropic（用户没装/没 key 仍能跑）
    try:
        import anthropic
    except ImportError:
        return LLMSelection(None, 0.0, "anthropic_sdk_missing", "")

    valid_ids = {c["id"] for c in candidates}

    # 精简 candidate 字段给 LLM 看（避免 prompt token 爆炸）
    summary = [
        {
            "id": c["id"],
            "title": c.get("title", ""),
            "original_title": c.get("original_title"),
            "year": c.get("year"),
            "media_type": c.get("media_type"),
            "overview": (c.get("overview") or "")[:200],
        }
        for c in candidates
    ]

    prompt = SELECT_PROMPT_V1.format(
        parse_json=json.dumps(parse, ensure_ascii=False, indent=2),
        candidates_json=json.dumps(summary, ensure_ascii=False, indent=2),
    )

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        msg = client.messages.create(
            model=model,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001 — SDK 多种异常都视作 LLM 不可用
        logger.error(f"[llm] anthropic call failed: {e}")
        return LLMSelection(None, 0.0, f"llm_error: {type(e).__name__}", "")

    # SDK response 拿文本
    try:
        raw = msg.content[0].text.strip()
    except (AttributeError, IndexError):
        return LLMSelection(None, 0.0, "empty_response", "")

    # 容忍 markdown code fence 包裹（虽然 prompt 要求 strict JSON，但 LLM 偶发会加）
    json_text = raw
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", json_text)
    if m:
        json_text = m.group(1)

    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError:
        logger.warning(f"[llm] JSON parse failed; raw={raw[:200]!r}")
        return LLMSelection(None, 0.0, f"malformed_json: {raw[:80]}", raw)

    selected = parsed.get("selected")
    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    reasoning = (parsed.get("reasoning") or "")[:300]

    # 契约 #3 强制：selected 必须 in valid_ids 或 None
    if selected is not None and selected not in valid_ids:
        logger.warning(
            f"[llm] fabricated id rejected: got={selected!r}, valid={list(valid_ids)[:3]}..."
        )
        return LLMSelection(
            None, 0.0,
            f"fabricated_id_rejected (got {selected!r})", raw,
        )

    # 契约 #3 强制：confidence < 0.7 → needs_review
    if confidence < 0.7 or selected is None:
        return LLMSelection(
            None, confidence,
            reasoning or "low_confidence_or_null", raw,
        )

    return LLMSelection(selected, confidence, reasoning, raw)
