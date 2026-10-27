"""工具循环中途的上下文保护。

压缩只在每条用户消息开头判断一次，而模型一次回复里可以连续调用很多轮工具，
结果一轮轮塞进上下文：开头压到了阈值以下，循环里照样会一路涨过模型窗口
（越过平台内置压缩线时，平台会成段删掉较早的消息，其中就有较早的工具结果）。

这里在每个工具返回之后盯一次：估算「下一次请求」的大小——已经在消息列表里的
内容、尚未并入列表的本批工具结果、工具声明——超过触发线，就把较早的工具结果
换成一行占位说明，直到降回压缩目标。不调用模型，只做本地估算。

- 只改 role == "tool" 消息的 content；tool_call 与结果的配对结构、其余消息一概不动。
- 由旧到新清理。最近 LOOP_KEEP_ROUNDS 轮工具调用的结果最后才动，而且清完更早的
  仍逼近内置压缩线（HARD_RATIO）时才动——模型刚读过的结果，往往还要拿来写最终答复。
- 本批结果并入列表之前改不到；单个结果自己就超窗口时，这里救不了——清掉所有较早的
  结果仍高于窗口，就一条都不清：既救不回来，又会白白丢掉还有用的内容（平台把超大
  结果落盘成预览时，真正并入列表的其实很小，这时更不该动）。

纯逻辑，不依赖 AstrBot：消息按鸭子类型读 role / content / tool_calls，
字典和 pydantic 模型都行；main.py 负责接钩子。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import compress, tokens

# 最近多少轮工具调用的结果最后才清。一轮 = 一条带 tool_calls 的 assistant 消息
# 及其后的工具结果。
LOOP_KEEP_ROUNDS = 2
# 「逼近内置压缩线」的判定线（占窗口比例）：清完更早的结果仍高于它，
# 才去动最近几轮的结果。
HARD_RATIO = compress.BUILTIN_THRESHOLD - 0.04


# ---------------------------------------------------------------- 消息读取


def _part_view(part: Any) -> Any:
    """多模态 content 里的一段，转成估算器认得的字典形态。

    图片、音频只报类型：估算按固定张数计，不需要也不该复制 base64 数据。
    """
    if isinstance(part, (str, dict)):
        return part
    kind = getattr(part, "type", None)
    if kind in ("image_url", "image"):
        return {"type": "image_url"}
    if kind in ("audio_url", "audio"):
        return {"type": "audio"}
    if kind == "text":
        return {"type": "text", "text": str(getattr(part, "text", "") or "")}
    if kind == "think":
        return {"type": "text", "text": str(getattr(part, "think", "") or "")}
    return str(part)


def _content_view(content: Any) -> Any:
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return [_part_view(part) for part in content]
    return str(content)


def _calls_view(calls: Any) -> list | None:
    if not calls:
        return None
    view: list = []
    for call in calls:
        if isinstance(call, dict):
            view.append(call)
            continue
        function = getattr(call, "function", None)
        view.append(
            {
                "function": {
                    "name": getattr(function, "name", "") or "",
                    "arguments": getattr(function, "arguments", "") or "",
                }
            }
        )
    return view


def message_view(message: Any) -> dict:
    """一条消息的估算视图：字典原样返回，对象按鸭子类型取字段。"""
    if isinstance(message, dict):
        return message
    return {
        "role": getattr(message, "role", None),
        "content": _content_view(getattr(message, "content", None)),
        "tool_calls": _calls_view(getattr(message, "tool_calls", None)),
        "tool_call_id": getattr(message, "tool_call_id", None),
    }


def describe_result(tool_result: Any) -> tuple[str, int]:
    """从工具钩子拿到的返回值里取出 (文本, 图片张数)。

    正常是 MCP 的 CallToolResult（content 里有文本、图片、内嵌资源）；也兼容
    纯字符串与 None。认不出的形态按空算：少算只会少清理，不会误伤。
    """
    if tool_result is None:
        return "", 0
    if isinstance(tool_result, str):
        return tool_result, 0
    items = getattr(tool_result, "content", None)
    if not isinstance(items, (list, tuple)):
        return "", 0
    texts: list[str] = []
    images = 0
    for item in items:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            texts.append(text)
            continue
        if getattr(item, "type", "") == "image":
            images += 1
            continue
        resource_text = getattr(getattr(item, "resource", None), "text", None)
        if isinstance(resource_text, str):
            texts.append(resource_text)
    return "\n\n".join(texts), images


def _set_cleared(message: Any) -> bool:
    """把一条 tool 消息的 content 换成占位说明，成功返回 True。"""
    if isinstance(message, dict):
        if message.get("role") != "tool":
            return False
        message["content"] = compress.TOOL_RESULT_CLEARED
        return True
    if getattr(message, "role", None) != "tool":
        return False
    try:
        message.content = compress.TOOL_RESULT_CLEARED
    except Exception:  # noqa: BLE001 - 只读对象等：放弃这一条，不影响其余
        return False
    return True


# ---------------------------------------------------------------- 清理规划


def _clearable(view: Any) -> bool:
    """值得清的工具结果：还没清过，且足够长（短结果占不了多少上下文）。"""
    return (
        isinstance(view, dict)
        and view.get("role") == "tool"
        and view.get("content") != compress.TOOL_RESULT_CLEARED
        and len(compress.content_to_text(view.get("content")))
        >= compress.TOOL_RESULT_CLEAR_MIN_CHARS
    )


def _clear_gain(view: dict, estimator: tokens.TokenEstimator) -> int:
    """把这条结果换成占位说明能省下的 token 数。"""
    before = estimator.messages([view])
    after = estimator.messages([{**view, "content": compress.TOOL_RESULT_CLEARED}])
    return max(0, before - after)


def _protected_from(views: list, keep_rounds: int) -> int:
    """最近 keep_rounds 轮工具调用的起点下标；该下标及之后的结果算「最近几轮」。

    不足 keep_rounds 轮时整份列表都算最近几轮（返回 0）。
    """
    if keep_rounds <= 0:
        return len(views)
    rounds = 0
    for index in range(len(views) - 1, -1, -1):
        view = views[index]
        if (
            isinstance(view, dict)
            and view.get("role") == "assistant"
            and view.get("tool_calls")
        ):
            rounds += 1
            if rounds >= keep_rounds:
                return index
    return 0


def plan_clearing(
    views: list,
    *,
    total: int,
    target: int,
    limit: int,
    hard: int,
    estimator: tokens.TokenEstimator,
    keep_rounds: int = LOOP_KEEP_ROUNDS,
    ceiling: int | None = None,
) -> tuple[list[int], int, bool]:
    """选出要换成占位说明的 tool 消息下标。

    返回 (按清理顺序的下标, 清完后的估算总量, 是否徒劳)。

    total 超过 limit 才动手。第一步由旧到新清「更早」的结果，降到 target 为止；
    若仍高于 hard（逼近内置压缩线），第二步再由旧到新清最近几轮的结果，
    降到 limit 为止。给了 ceiling（窗口本身）时，若清光所有可清的结果仍高于它，
    清理救不了这个请求，判为徒劳：一条都不清。
    """
    if total <= limit:
        return [], total, False
    protected_from = _protected_from(views, keep_rounds)
    older: list[int] = []
    recent: list[int] = []
    for index, view in enumerate(views):
        if _clearable(view):
            (older if index < protected_from else recent).append(index)
    gains = {index: _clear_gain(views[index], estimator) for index in (*older, *recent)}
    if ceiling is not None and total - sum(gains.values()) > ceiling:
        return [], total, True

    chosen: list[int] = []
    remaining = total
    for index in older:
        if remaining <= target:
            break
        chosen.append(index)
        remaining -= gains[index]
    if remaining > hard:
        for index in recent:
            if remaining <= limit:
                break
            chosen.append(index)
            remaining -= gains[index]
    return chosen, remaining, False


# ---------------------------------------------------------------- 循环守卫


@dataclass
class GuardOutcome:
    """一次检查的结果，供调用方记日志。"""

    cleared: int
    before: int
    after: int
    # 清完仍高于触发线（已无可清的结果），或清理徒劳而没有动手；每个守卫实例只报告一次
    stuck: bool = False
    # 清光所有较早的结果也救不了这个请求，所以一条都没清
    futile: bool = False


def _args_text(args: Any) -> str:
    if not args:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


class LoopGuard:
    """一次回复（一个 Agent 运行）的循环内保护。

    on_llm_request 里按本轮预算建好，随事件传到工具钩子；预算在整轮里不变，
    批次累计与估算缓存随循环推进。
    """

    def __init__(
        self,
        *,
        limit: int,
        target: int,
        hard: int,
        tools_cost: int = 0,
        ratio: float = 1.0,
        keep_rounds: int = LOOP_KEEP_ROUNDS,
        ceiling: int | None = None,
    ):
        self.limit = max(0, int(limit))
        self.target = max(0, min(int(target), self.limit))
        self.hard = max(self.limit, int(hard))
        self.tools_cost = max(0, int(tools_cost))
        self.ratio = ratio
        self.keep_rounds = max(0, int(keep_rounds))
        # 窗口本身：清光较早的结果也压不进它的请求，清理徒劳（None 表示不判）
        self.ceiling = None if ceiling is None else max(0, int(ceiling))
        # Agent 开始运行时由钩子填入，其 messages 就是运行器每步重新读取的列表
        self.run_context: Any = None
        self.cleared_total = 0
        self._key: tuple | None = None
        self._batch = 0
        self._views: list | None = None
        self._list_tokens = 0
        self._stuck_reported = False

    @classmethod
    def for_window(
        cls,
        window: int,
        *,
        threshold: float,
        target_ratio: float,
        tools_cost: int,
        ratio: float,
        keep_rounds: int = LOOP_KEEP_ROUNDS,
    ) -> "LoopGuard":
        """按窗口与压缩设置建守卫：触发线 = 压缩阈值，目标 = 压缩目标水位。"""
        limit = int(window * threshold)
        return cls(
            limit=limit,
            target=int(window * target_ratio),
            hard=int(window * HARD_RATIO),
            tools_cost=tools_cost,
            ratio=ratio,
            keep_rounds=keep_rounds,
            ceiling=window,
        )

    def __repr__(self) -> str:
        # 事件的额外字段偶尔会被整体打进日志：不能把 run_context 的消息列表带出来
        return (
            f"LoopGuard(limit={self.limit}, target={self.target}, "
            f"cleared={self.cleared_total})"
        )

    def _tool_cost(
        self,
        estimator: tokens.TokenEstimator,
        name: str,
        args: Any,
        text: str,
        images: int,
    ) -> int:
        """一个工具的调用与结果并入消息列表后，会占多少 token。"""
        call = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"function": {"name": name, "arguments": _args_text(args)}}],
        }
        result = {"role": "tool", "content": text}
        return estimator.messages([call, result]) + estimator.scale(
            images * tokens.IMAGE_TOKENS
        )

    def after_tool(
        self,
        messages: Any,
        *,
        name: str,
        args: Any,
        result_text: str,
        images: int = 0,
    ) -> GuardOutcome | None:
        """一个工具刚返回：估算下一次请求，必要时就地清理较早的工具结果。

        messages 是运行器正在用的消息列表（本批的调用与结果此时还没并入）。
        没超线返回 None；超线则返回结果（可能一条也没清成）。
        """
        if not isinstance(messages, list) or not messages:
            return None
        estimator = tokens.TokenEstimator(self.ratio)

        # 列表长度或末条变了，说明上一批已并入、新的一批开始：批次从零累计
        key = (len(messages), id(messages[-1]))
        if key != self._key:
            self._key = key
            self._batch = 0
            self._views = None
        self._batch += self._tool_cost(estimator, name, args, result_text, images)

        if self._views is None:
            self._views = [message_view(m) for m in messages]
            self._list_tokens = estimator.messages(self._views)
        total = self.tools_cost + self._list_tokens + self._batch
        if total <= self.limit:
            return None

        chosen, after, futile = plan_clearing(
            self._views,
            total=total,
            target=self.target,
            limit=self.limit,
            hard=self.hard,
            estimator=estimator,
            keep_rounds=self.keep_rounds,
            ceiling=self.ceiling,
        )
        cleared = 0
        for index in chosen:
            if _set_cleared(messages[index]):
                cleared += 1
        if cleared:
            # 内容变了：下一次检查时重新取视图与估算
            self._views = None
            self.cleared_total += cleared
        stuck = (futile or after > self.limit) and not self._stuck_reported
        if stuck:
            self._stuck_reported = True
        return GuardOutcome(
            cleared=cleared, before=total, after=after, stuck=stuck, futile=futile
        )
