"""消息轮次切分。

一整轮 = 一条 user 消息 + 其后的 assistant 回复及所有 tool_call / tool_result，
是压缩时不可分割的最小单元：把 tool_call 和它的结果切开会让 provider 直接报错。

切分规则：以 role == "user" 的消息为轮次边界；role == "tool"（或带
tool_call_id 的消息）永远归属当前轮。历史开头若有孤儿消息（assistant/tool
先于任何 user 出现，通常是上游截断的产物），归入一个引导轮，作为最老的
可压缩单元处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Turn:
    """一个不可分割的对话轮次。start 是首条消息在原列表中的下标。"""

    start: int
    messages: list[dict] = field(default_factory=list)


def is_tool_result(message: dict) -> bool:
    return message.get("role") == "tool" or "tool_call_id" in message


def split_leading_system(messages: list[dict]) -> int:
    """返回开头连续 system 消息之后的第一个下标。"""
    index = 0
    while index < len(messages) and messages[index].get("role") == "system":
        index += 1
    return index


def split_turns(messages: list[dict], start: int = 0) -> list[Turn]:
    """把 messages[start:] 切分为轮次列表。"""
    turns: list[Turn] = []
    current: Turn | None = None
    for index in range(start, len(messages)):
        message = messages[index]
        if not isinstance(message, dict):
            continue
        is_boundary = message.get("role") == "user" and not is_tool_result(message)
        if is_boundary or current is None:
            current = Turn(start=index)
            turns.append(current)
        current.messages.append(message)
    return turns
