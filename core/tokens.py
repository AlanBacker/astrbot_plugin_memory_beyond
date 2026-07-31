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


class TokenEstimator:
    """带校准的估算器。

    provider 返回真实用量时（on_llm_response 里能拿到 usage），用
    真实值 / 估算值 的比例做 EMA 校准，后续估算乘以该比例。
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
        if estimated <= 0 or actual <= 0:
            return
        observed = self._clamp(actual / estimated)
        self.ratio = self._clamp(
            self.ratio * (1 - CALIBRATION_EMA) + observed * CALIBRATION_EMA
        )

    def text(self, text: str) -> int:
        return math.ceil(estimate_text(text) * self.ratio)

    def messages(self, messages: list[dict] | None) -> int:
        return math.ceil(estimate_messages(messages) * self.ratio)
