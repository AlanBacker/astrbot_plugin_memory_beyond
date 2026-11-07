"""较早轮次瘦身：去掉历史里每轮重复的样板，旧搜索结果换成一句话。

AstrBot 把「提示词前缀」拼进每条用户消息、把 <system_reminder>（群名、时间、
其他插件追加的提醒）附在每条用户消息上，并原样回存进会话历史，聊得越久，
历史里重复的样板越多；网页搜索、网页提取工具的原文也一直留在历史里。
这里只处理历史（req.contexts），本轮新消息不在其中，不受影响。

平台每轮回存实际发送的消息列表，瘦过的轮次之后读进来已经是瘦的，再处理一遍
不会变（幂等）：每次请求只改到刚结束的那一轮，更早的前缀与上次完全一样，
提示词缓存照常命中。

另有短回复连发检测（short_reply_streak）：模型会照着自己刚说过的话往下接，
一连几条只有几个字的回复进了历史，之后就越回越短、越回越冲；检测到这种
情况时由调用方给本轮附一句 SHORT_STREAK_NOTE。

被引用质疑检测（quoted_pushback）：有人引用机器人自己那条、只回个问号或一句
"并非"，模型常搬出记忆嘴硬、反呛对方，几个来回就吵起来；这时附一句 PUSHBACK_NOTE。
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase

REMINDER = re.compile(r"<system_reminder>(.*?)</system_reminder>", re.DOTALL)
# 旧版 AstrBot 把群聊记录放在提醒块里，那是真实的聊天内容，原样保留
GROUP_CONTEXT_START = "You are in a group chat."
# 旧提醒只留这一行，模型仍能知道每句话是什么时候说的
KEPT_REMINDER_LINE = "Current datetime:"
TOOL_RESULT_PLACEHOLDER = "[旧搜索结果已省略，结论见你当时的回复；需要细节就重新搜]"
# 短回复连发：最近这么多条回复都不超过这么多字（表情包标签不算）就算连发
SHORT_STREAK_REPLIES = 3
SHORT_STREAK_MAX_CHARS = 6
MEME_TAG = re.compile(r"&&.*?&&")
# 写成不带时间行的系统提醒：开着瘦身时，下一轮就从历史里整块去掉，不会越攒越多
SHORT_STREAK_NOTE = (
    "<system_reminder>你最近几条回复都只有几个字。这条按你平时的样子好好回，"
    "语气别冲，也别敷衍。</system_reminder>"
)
# 被引用质疑：引用机器人自己那条、正文只有问号或一句短否认
PUSHBACK_TEXT = re.compile(
    r"[?？¿]+|(?:嗯|啊|啥|什么|蛤)[?？¿]+"
    r"|(?:并非|不是|不对|没有|才没有|才不是|哪有|瞎说|胡说|乱说|你?记错了)[?？¿!！。.…]*"
)
PUSHBACK_NOTE = (
    "<system_reminder>对方引用了你刚才那条，只回了个问号或者一句否认，多半是觉得你说得不对或者没看懂。"
    "说的是他本人的事就以他说的为准，顺着改口；没头没脑的就解释一句或者问他指啥。"
    "别嘴硬，别翻旧账，别呛人。</system_reminder>"
)


def _keep_datetime(match: re.Match) -> str:
    """把一个旧提醒块缩成它的时间行；群聊记录块原样保留，没有时间行则整块去掉。"""
    body = match.group(1)
    if body.startswith(GROUP_CONTEXT_START):
        return match.group(0)
    lines = [line for line in body.split("\n") if line.startswith(KEPT_REMINDER_LINE)]
    return f"<system_reminder>{lines[0]}</system_reminder>" if lines else ""


def slim_history(
    contexts: list[dict],
    prefix_rx: re.Pattern | None,
    tool_patterns: list[str],
) -> tuple[list[dict], int]:
    """瘦身一份会话历史。

    - 用户消息：文字段去掉 prefix_rx 匹配到的前缀，提醒块缩成时间行；
      只剩被去掉的提醒的文字段整段拿掉，图片等非文字段不动；
    - 工具结果：调用名匹配 tool_patterns（支持 * 通配）的结果换成一句占位；
      tool_call 与结果的配对结构不变；
    - 其他消息（system、助手回复、工具调用）原样保留。

    Args:
        contexts: 会话历史（不含本轮新消息）。
        prefix_rx: 要去掉的提示词前缀；None 表示不处理前缀。
        tool_patterns: 结果要换成占位的工具名模式；空列表表示不处理工具结果。

    Returns:
        (瘦身后的消息列表, 省下的字符数)。没有改动时原样返回传入的列表与 0。
    """
    tool_names: dict = {}
    slimmed: list = []
    saved = 0
    changed = False
    for message in contexts:
        if not isinstance(message, dict):
            slimmed.append(message)
            continue
        role = message.get("role")
        if role == "assistant":
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict):
                    tool_names[call.get("id")] = (call.get("function") or {}).get(
                        "name"
                    )
        elif (
            role == "tool"
            and message.get("content") != TOOL_RESULT_PLACEHOLDER
            and any(
                fnmatchcase(tool_names.get(message.get("tool_call_id")) or "", p)
                for p in tool_patterns
            )
        ):
            saved += len(str(message.get("content") or "")) - len(
                TOOL_RESULT_PLACEHOLDER
            )
            message = {**message, "content": TOOL_RESULT_PLACEHOLDER}
            changed = True
        elif role == "user":
            content = message.get("content")
            parts = (
                content
                if isinstance(content, list)
                else [{"type": "text", "text": content or ""}]
            )
            new_parts = []
            message_saved = 0
            for part in parts:
                text = (
                    part.get("text")
                    if isinstance(part, dict) and part.get("type") == "text"
                    else None
                )
                if not isinstance(text, str):
                    new_parts.append(part)
                    continue
                new_text = prefix_rx.sub("", text) if prefix_rx else text
                new_text = REMINDER.sub(_keep_datetime, new_text)
                message_saved += len(text) - len(new_text)
                # 只剩被去掉的提醒的段整段拿掉；本来就空的段保留，结构不变
                if new_text or not text:
                    new_parts.append(
                        {**part, "text": new_text} if new_text != text else part
                    )
            if message_saved:
                saved += message_saved
                message = {
                    **message,
                    "content": new_parts
                    if isinstance(content, list)
                    else "".join(p["text"] for p in new_parts),
                }
                changed = True
        slimmed.append(message)
    return (slimmed, saved) if changed else (contexts, 0)


def short_reply_streak(contexts: list[dict]) -> bool:
    """最近几条机器人回复是不是都只有几个字。

    只看纯文字回复：带工具调用的助手消息、没有文字的回复跳过，表情包标签
    （&&…&&）不计字数。

    Args:
        contexts: 会话历史（不含本轮新消息）。

    Returns:
        最近 SHORT_STREAK_REPLIES 条回复都不超过 SHORT_STREAK_MAX_CHARS 字时为 True；
        回复不够这么多条时为 False。
    """
    seen = 0
    for message in reversed(contexts):
        if (
            not isinstance(message, dict)
            or message.get("role") != "assistant"
            or message.get("tool_calls")
        ):
            continue
        content = message.get("content")
        parts = (
            content
            if isinstance(content, list)
            else [{"type": "text", "text": content or ""}]
        )
        text = "".join(
            part.get("text") or ""
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        )
        text = MEME_TAG.sub("", text).strip()
        if not text:
            continue
        if len(text) > SHORT_STREAK_MAX_CHARS:
            return False
        seen += 1
        if seen == SHORT_STREAK_REPLIES:
            return True
    return False


def quoted_pushback(segments: list, self_id: str) -> bool:
    """本条消息是不是引用了机器人自己的话、正文只回了问号或一句短否认。

    模型手里有记忆和自己刚说过的话，被人打个问号或者一句"并非"顶回来时，
    常会搬出记忆坚持原说法、反呛对方，一来一回就吵起来；这种时候由调用方给本轮附一句
    PUSHBACK_NOTE。按类名认消息段，不依赖平台的组件类。

    Args:
        segments: 本条消息的消息段（event.get_messages()）。
        self_id: 机器人自己的 ID；为空时无法判断引用的是谁，直接返回 False。

    Returns:
        引用了机器人自己的消息，且除 @ 以外的文字只有问号或一句短否认时为 True。
    """
    if not self_id:
        return False
    quoted_self = False
    text = ""
    for seg in segments or []:
        kind = type(seg).__name__
        if kind == "Reply":
            quoted_self = (
                quoted_self or str(getattr(seg, "sender_id", "") or "") == self_id
            )
        elif kind == "Plain":
            text += str(getattr(seg, "text", "") or "")
    return quoted_self and bool(PUSHBACK_TEXT.fullmatch(text.strip()))
