"""LLM grounded selection — 契约 #3 唯一出口。

**Provider 默认：DeepSeek**（用户偏好，按 ~/.claude/projects/<this>/memory/
feedback_llm_provider.md：Anthropic 太贵不用）。走 OpenAI-compatible 协议
chat-completions endpoint（参考 ai-sdk-third-party.md 第三方网关经验）。

**Single call site rule** (plan v3 R2-I5):
Any module that needs LLM **must** route through this file. Direct
`import openai` outside `services/llm.py` is a code review red flag.
The SDK is lazily imported inside the function so the rest of the
project never touches it.

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

# DeepSeek V4 系列（按用户偏好 2026-05-12 确认）：
#   deepseek-v4-flash — 默认（更便宜、更快，足够 grounded-select 任务）
#   deepseek-v4-pro   — 可在 UI / env 切换（复杂推理 / 精度敏感场景）
# Legacy alias `deepseek-chat` 仍可用，但项目内统一走 V4 显式命名。
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"

# LLM filename rescue prompt：guessit / 正则解析不出 title 时让 LLM 看文件名
# 不是 grounded（LLM 自由输出 title 字符串）—— 但下游会用 title 去 TMDB ground 一次，
# 不会让 LLM 直接绑 TMDB id。中文 PT release "死亡笔记.BDrip1080P.X264.AC3.LGGZ S.01.mkv"
# guessit 拿不到 title，LLM 几乎一秒看懂"死亡笔记 / Death Note"。
EXTRACT_TITLE_PROMPT_V1 = """Extract the media title and basic metadata from this filename.

Many filenames mix Chinese / English / Japanese release naming with codecs / sources / encoder tags. Strip those and recover the *show* or *movie* title.

Filename: {filename}

Return strict JSON ONLY (no markdown, no commentary):
{{
  "title": "<best guess at title in its primary language (zh / en / ja)>",
  "alt_title": "<alternate-language name if reasonably confident, else null>",
  "year": <number or null>,
  "season": <number or null>,
  "episode": <number or null>,
  "media_type": "movie" or "tv" or "unknown"
}}

If you cannot identify the work at all, return {{"title": null}}.
"""


# 契约 #3 prompt template（v1，单一注册名）
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
class FilenameExtraction:
    """LLM 从文件名拆出的最小媒体信息。下游用 title 去 TMDB 二次 ground。"""

    title: str | None
    alt_title: str | None
    year: int | None
    season: int | None
    episode: int | None
    media_type: str  # 'movie' | 'tv' | 'unknown'
    raw_response: str


@dataclass(frozen=True)
class LLMSelection:
    selected_id: str | None       # 必 ∈ {c.id for c in candidates}，否则置 None
    confidence: float             # 0.0-1.0
    reasoning: str                # 简短解释
    raw_response: str             # provenance：原始 LLM 输出，便于 audit


def load_api_key(config_path: Path | str | None = None) -> str:
    """env DEEPSEEK_API_KEY > config 文件 > 空。同 .api_token / .tmdb_key 模式。"""
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env:
        return env
    if config_path:
        p = Path(config_path)
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    return ""


def is_available(config_path: Path | str | None = None) -> bool:
    return bool(load_api_key(config_path))


def extract_title_from_filename(
    filename: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 90.0,  # reasoning 模式比 chat 慢，30s 经常不够
) -> tuple[FilenameExtraction | None, str]:
    """LLM 解析文件名 → title/year/season/episode/media_type。

    Returns: (FilenameExtraction or None, error_reason_string)
    error_reason 形态:
      ""                                  → 成功
      "no_filename" / "no_api_key"        → 调用前 short circuit
      "sdk_missing"                       → openai SDK 没装
      "llm_error: <ErrorType>: <msg[:200]>" → SDK 抛错（含 model 不存在 / auth 失败等）
      "empty_response"                    → API 200 但 content 空
      "malformed_json: <raw[:80]>"        → JSON 解析失败
      "no_title"                          → LLM 明确说不认识

    用途：guessit / 正则在中文 release / 模糊命名上失败时的 fallback。
    本函数不返回 TMDB id（只生成 title 字符串），仍属契约 #3 grounded 流程的
    上游——下游会拿 title 去 TMDB.search() 再过 grounded select。
    """
    if not filename:
        return None, "no_filename"
    if not api_key:
        return None, "no_api_key"
    try:
        import openai
    except ImportError:
        return None, "sdk_missing"

    prompt = EXTRACT_TITLE_PROMPT_V1.format(filename=filename)
    try:
        client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        resp = client.chat.completions.create(
            model=model,
            max_tokens=1500,  # reasoning 模式需要内部思考 token 预算，给宽裕
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001
        msg = str(e)[:200]
        logger.error(f"[llm] extract_title call failed: {type(e).__name__}: {msg}")
        return None, f"llm_error: {type(e).__name__}: {msg}"

    try:
        msg = resp.choices[0].message
        raw = (msg.content or "").strip()
        # DeepSeek V4 / R1 reasoning 模式：真实输出在 reasoning_content
        # 而非 content。content 可能完全空 → 必须 fallback。
        if not raw and hasattr(msg, "reasoning_content") and msg.reasoning_content:
            raw = msg.reasoning_content.strip()
            logger.info(f"[llm] using reasoning_content (model is in reasoning mode)")
    except (AttributeError, IndexError):
        return None, "empty_response"

    if not raw:
        return None, "empty_response (content + reasoning_content both empty)"

    # 容忍 markdown fence
    json_text = raw
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", json_text)
    if m:
        json_text = m.group(1)

    # reasoning_content 通常带 narrative + 末尾的 JSON；找最后一段 JSON 块
    if "{" in json_text and not json_text.lstrip().startswith("{"):
        # 取最后一个 { 开头到对应 } 的子串
        last_open = json_text.rfind("{")
        last_close = json_text.rfind("}")
        if last_open != -1 and last_close > last_open:
            json_text = json_text[last_open:last_close + 1]

    try:
        data = json.loads(json_text)
    except json.JSONDecodeError:
        logger.warning(f"[llm] extract_title JSON parse failed; raw={raw[:300]!r}")
        return None, f"malformed_json: {raw[:80]}"

    # title 必须是非空字符串才算成功
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        return None, "no_title"

    def _int_or_none(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    media_type = data.get("media_type")
    if media_type not in ("movie", "tv", "unknown"):
        media_type = "unknown"

    return FilenameExtraction(
        title=title.strip(),
        alt_title=(data.get("alt_title") or None) if isinstance(data.get("alt_title"), str) else None,
        year=_int_or_none(data.get("year")),
        season=_int_or_none(data.get("season")),
        episode=_int_or_none(data.get("episode")),
        media_type=media_type,
        raw_response=raw,
    ), ""


def select_candidate(
    parse: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = 90.0,  # reasoning 模式 + 多候选时 30s 不够，APITimeoutError 频发
) -> LLMSelection:
    """grounded selection。永远不让 LLM 编 id；output schema enforce 后失败 → needs_review。

    Args:
        parse: filename 解析结果（含 title/year/season/episode/...）
        candidates: provider 返回的候选 dict 列表，必须含 'id'、'title'
        api_key: BYOK DeepSeek key
        model: 默认 deepseek-v4-flash（V4 Pro 可在调用方覆盖）
        base_url: 默认 DeepSeek（https://api.deepseek.com）；可改成 OpenAI / 第三方网关
    """
    if not candidates:
        return LLMSelection(None, 0.0, "no_candidates", "")
    if not api_key:
        return LLMSelection(None, 0.0, "no_api_key", "")

    # 懒 import：openai SDK 没装时优雅 fallback；其他模块不该直接 import
    try:
        import openai
    except ImportError:
        return LLMSelection(None, 0.0, "openai_sdk_missing", "")

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
        client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        resp = client.chat.completions.create(
            model=model,
            max_tokens=2000,  # reasoning 模式 + 多候选时给宽裕预算
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
            # DeepSeek 支持 response_format={'type':'json_object'} 但有的网关不支持；
            # 我们靠 prompt 要求 JSON + markdown-fence 容忍的解析做兜底，更可移植
        )
    except Exception as e:  # noqa: BLE001 — SDK 多种异常都视作 LLM 不可用
        logger.error(f"[llm] call failed: {e}")
        return LLMSelection(None, 0.0, f"llm_error: {type(e).__name__}", "")

    try:
        msg = resp.choices[0].message
        raw = (msg.content or "").strip()
        # DeepSeek V4 / R1 reasoning 模式 fallback
        if not raw and hasattr(msg, "reasoning_content") and msg.reasoning_content:
            raw = msg.reasoning_content.strip()
    except (AttributeError, IndexError):
        return LLMSelection(None, 0.0, "empty_response", "")

    if not raw:
        return LLMSelection(None, 0.0, "empty_response_both_fields", "")

    # 容忍 markdown code fence 包裹（DeepSeek 偶发会加 ```json）
    json_text = raw
    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", json_text)
    if m:
        json_text = m.group(1)

    # reasoning_content 通常含 narrative；取最后一段 JSON 子串
    if "{" in json_text and not json_text.lstrip().startswith("{"):
        last_open = json_text.rfind("{")
        last_close = json_text.rfind("}")
        if last_open != -1 and last_close > last_open:
            json_text = json_text[last_open:last_close + 1]

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
