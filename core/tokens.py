"""Token 估算。

口径刻意对齐 AstrBot 平台的字符启发式（中文 ×0.6、其他 ×0.3、图片 765、
音频 500），保证本插件与内置压缩器对同一份上下文算出同一量级的占用——
否则我们认为 60% 时平台可能已认为 85%，内置压缩器会抢先触发。

每条消息额外加一个小常数，让我们的估算略高于平台：宁可早触发自己的压缩，
也不能让内置压缩器先动手。
"""

from __future__ import annotations

import json
import math
from typing import Any

CHINESE_WEIGHT = 0.6
OTHER_WEIGHT = 0.3
IMAGE_TOKENS = 765
AUDIO_TOKENS = 500
# 每条消息的固定开销（role 字段、消息边界标记等），取小值保持口径接近平台。
MESSAGE_OVERHEAD = 4

# 校准比例的钳制范围：真实用量 / 估算值超出该范围时按边界截断，
# 防止一次异常的 usage 上报把估算彻底带偏。
CALIBRATION_MIN = 0.5
CALIBRATION_MAX = 3.0
# 指数滑动平均权重：新观测占 40%，兼顾响应速度与稳定性。
CALIBRATION_EMA = 0.4


def _is_chinese(ch: str) -> bool:
    return "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿"


def estimate_text(text: str) -> int:
    """按字符启发式估算一段纯文本的 token 数。"""
    if not text:
        return 0
    chinese = sum(1 for ch in text if _is_chinese(ch))
    other = len(text) - chinese
    return math.ceil(chinese * CHINESE_WEIGHT + other * OTHER_WEIGHT)


def _estimate_segment(segment: Any) -> int:
    """估算多模态 content 列表中的一个消息段。"""
    if isinstance(segment, str):
        return estimate_text(segment)
    if not isinstance(segment, dict):
        return estimate_text(str(segment))
    seg_type = segment.get("type", "")
    if seg_type == "text":
        return estimate_text(str(segment.get("text", "")))
    if seg_type in ("image_url", "image"):
        return IMAGE_TOKENS
    if seg_type in ("input_audio", "audio", "record"):
        return AUDIO_TOKENS
    # 未知消息段：按其 JSON 文本长度估算，避免漏算成 0。
    try:
        return estimate_text(json.dumps(segment, ensure_ascii=False))
    except (TypeError, ValueError):
        return estimate_text(str(segment))


def estimate_content(content: Any) -> int:
    """估算一条消息的 content 字段（str / 多模态 list / None）。"""
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_text(content)
    if isinstance(content, list):
        return sum(_estimate_segment(seg) for seg in content)
    return estimate_text(str(content))


def estimate_message(message: dict) -> int:
    """估算一条 OpenAI 风格消息字典的 token 数。"""
    total = MESSAGE_OVERHEAD + estimate_content(message.get("content"))
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            total += estimate_text(str(tool_call))
            continue
        function = tool_call.get("function") or {}
        total += estimate_text(str(function.get("name", "")))
        total += estimate_text(str(function.get("arguments", "")))
    return total


def estimate_messages(messages: list[dict] | None) -> int:
    if not messages:
        return 0
    return sum(estimate_message(m) for m in messages if isinstance(m, dict))


def _usage_field(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


# 各家 provider 上报缓存命中的字段名不同，逐个探测。
_CACHE_HIT_KEYS = (
    "prompt_cache_hit_tokens",  # DeepSeek
    "cache_read_input_tokens",  # Anthropic 系
    "cached_content_token_count",  # Gemini 系
)


def extract_cache_hit(usage: Any) -> int | None:
    """从 usage（dict 或对象）提取提示词缓存命中的 token 数，未上报返回 None。"""
    if usage is None:
        return None
    candidates = [_usage_field(usage, key) for key in _CACHE_HIT_KEYS]
    details = _usage_field(usage, "prompt_tokens_details")  # OpenAI 系
    if details is not None:
        candidates.append(_usage_field(details, "cached_tokens"))
    for value in candidates:
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None


class TokenEstimator:
    """带校准的估算器。

    provider 返回真实用量时（on_llm_response 里能拿到 usage），学习
    真实值相对无校准原始估算的绝对比例（EMA），后续估算乘以该比例。
    比例被钳制在 [0.5, 3.0]，防止异常上报污染。
    """

    def __init__(self, ratio: float = 1.0):
        self.ratio = self._clamp(ratio)

    @staticmethod
    def _clamp(value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            return 1.0
        return min(max(value, CALIBRATION_MIN), CALIBRATION_MAX)

    def calibrate(self, estimated: int, actual: int) -> None:
        """estimated 必须是用当前比例产出的估算值。

        先按当前比例还原出无校准的原始估算，学习 真实/原始 的绝对比例，
        再与当前比例做 EMA。不能直接拿 真实/已校准估算 做 EMA：那样的
        收敛点是真实比例的平方根（残差因子恰好等于比例本身时即停），
        估算会永远系统性偏低。
        """
        if estimated <= 0 or actual <= 0:
            return
        raw = estimated / self.ratio
        observed = self._clamp(actual / raw)
        self.ratio = self._clamp(
            self.ratio * (1 - CALIBRATION_EMA) + observed * CALIBRATION_EMA
        )

    def text(self, text: str) -> int:
        return math.ceil(estimate_text(text) * self.ratio)

    def messages(self, messages: list[dict] | None) -> int:
        return math.ceil(estimate_messages(messages) * self.ratio)

    def scale(self, count: int) -> int:
        """把固定 token 常数（工具配额、图片等）也纳入校准比例。

        估算的每一项都经过比例缩放，calibrate() 才能用 估算值/比例
        精确还原原始估算。
        """
        return math.ceil(count * self.ratio)
