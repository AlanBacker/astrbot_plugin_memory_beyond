"""上下文压缩状态机：摘要 + 水位线。

插件为每个会话维护「摘要 + 水位线（已摘要覆盖到第几条消息）」，每轮拼出
    发送视图 = 开头 system 消息 + 摘要块 + 水位线之后的原文消息
再次压缩时做滚动摘要：上一份摘要和其后的旧轮次一起重摘要，推进水位线。

平台侧的关键事实：AstrBot 每轮会把实际发送的消息列表回存为会话历史
（整体替换）。压缩一旦发生，被摘要覆盖的原文就会随下一次回存从平台历史
卸下，此后由摘要（存于插件状态、每轮以 _no_save 消息注入）继续代表。

水位线用消息下标表示，状态里记录水位线前一条消息的指纹，每轮由
reconcile() 校验状态与当前历史是否仍对得上：
- 锚点吻合——平台侧仍保留着原文（未发生回存固化），按下标继续滚动；
- 锚点找不到但摘要在手——判定为压缩视图已被平台回存固化，把水位线归零、
  保留摘要，此后整份历史都视为摘要之后的尾部；
- 对话切换或历史被清空——状态整体作废重来。

摘要 LLM 不可用时的兜底：本轮用「已有摘要 + 对半砍旧轮次」拼出应急裁剪
视图并进入指数退避冷却（冷却结束自动重试）。在会回存历史的 AstrBot 上
裁剪结果同样会被固化，属于有损的最后手段，仅在摘要不可用且超预算时启用。
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field

from . import tokens
from .turns import Turn, split_leading_system, split_turns

# 内置压缩器阈值。本插件阈值必须低于它，内置压缩器的 should_compress()
# 才会永远返回 False、自动退化为 no-op（同时留在下游当安全网）。
BUILTIN_THRESHOLD = 0.82
# 阈值配置非法（>= 0.82 或 <= 0）时回退到的安全值。
THRESHOLD_FALLBACK = 0.70
THRESHOLD_MAX = 0.80

# 摘要失败后的指数退避：5 分钟起步，每次翻倍，上限 1 小时。
COOLDOWN_BASE_SECONDS = 300.0
COOLDOWN_MAX_SECONDS = 3600.0

# 应急视图里单条消息 content 的裁剪起点（逐级减半直到满足预算）。
CLIP_START_CHARS = 4000
CLIP_MIN_CHARS = 200

# 渲染摘要转写时单条消息的字符上限（丢弃完整工具输出，保留头部语义）。
TRANSCRIPT_MSG_CHARS = 2000


@dataclass
class SessionState:
    """一个会话（以 conversation id 锚定）的压缩状态，可 JSON 持久化。"""

    cid: str = ""
    watermark: int = 0
    summary: str = ""
    anchor: str = ""
    ratio: float = 1.0
    # 最近一次请求的 token 估算值。
    last_estimate: int = 0
    # 最近一次估算的分项构成：overhead（固定开销）/ index（索引块）/
    # summary（摘要）/ history（历史原文），四项之和即 last_estimate；
    # 空字典表示尚无记录（如旧版本升级来的状态文件）。
    last_parts: dict = field(default_factory=dict)
    # 最近一次请求命中提示词缓存的 token 数；-1 表示提供商未上报。
    cache_hit_tokens: int = -1
    fail_count: int = 0
    fail_until: float = 0.0
    compressed_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "cid": self.cid,
            "watermark": self.watermark,
            "summary": self.summary,
            "anchor": self.anchor,
            "ratio": self.ratio,
            "last_estimate": self.last_estimate,
            "last_parts": dict(self.last_parts),
            "cache_hit_tokens": self.cache_hit_tokens,
            "fail_count": self.fail_count,
            "fail_until": self.fail_until,
            "compressed_at": self.compressed_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SessionState":
        state = cls()
        if not isinstance(raw, dict):
            return state
        state.cid = str(raw.get("cid", ""))
        state.summary = str(raw.get("summary", ""))
        state.anchor = str(raw.get("anchor", ""))
        try:
            state.watermark = max(0, int(raw.get("watermark", 0)))
            state.ratio = float(raw.get("ratio", 1.0))
            state.last_estimate = max(0, int(raw.get("last_estimate", 0)))
            state.cache_hit_tokens = max(-1, int(raw.get("cache_hit_tokens", -1)))
            state.fail_count = max(0, int(raw.get("fail_count", 0)))
            state.fail_until = float(raw.get("fail_until", 0.0))
            state.compressed_at = float(raw.get("compressed_at", 0.0))
        except (TypeError, ValueError):
            return cls(cid=state.cid, summary=state.summary, anchor=state.anchor)
        if not math.isfinite(state.ratio) or state.ratio <= 0:
            state.ratio = 1.0
        raw_parts = raw.get("last_parts")
        if isinstance(raw_parts, dict):
            parts: dict = {}
            for key in ("overhead", "index", "summary", "history"):
                try:
                    parts[key] = max(0, int(raw_parts.get(key, 0)))
                except (TypeError, ValueError):
                    parts[key] = 0
            # 全零视同没有记录（正常记录 overhead 恒为正）
            if any(parts.values()):
                state.last_parts = parts
        return state

    def reset_compression(self) -> None:
        """丢弃摘要与水位线（保留 token 校准值）。"""
        self.watermark = 0
        self.summary = ""
        self.anchor = ""

    # ------------------------------------------------------------ 失败退避

    def in_cooldown(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) < self.fail_until

    def record_failure(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self.fail_count += 1
        delay = min(
            COOLDOWN_BASE_SECONDS * (2 ** (self.fail_count - 1)),
            COOLDOWN_MAX_SECONDS,
        )
        self.fail_until = now + delay

    def record_success(self) -> None:
        self.fail_count = 0
        self.fail_until = 0.0


def fingerprint(message: dict) -> str:
    """一条消息的稳定指纹，用于校验水位线仍指向同一段历史。"""
    try:
        payload = json.dumps(
            {
                "role": message.get("role"),
                "content": message.get("content"),
                "tool_calls": message.get("tool_calls"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        payload = str(message)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def state_matches(state: SessionState, cid: str, contexts: list[dict]) -> bool:
    """状态是否仍适用于当前历史：对话没换、水位线没越界、锚点对得上。"""
    if state.cid != cid:
        return False
    if state.watermark <= 0:
        return True
    if state.watermark > len(contexts):
        return False
    anchor_msg = contexts[state.watermark - 1]
    return isinstance(anchor_msg, dict) and fingerprint(anchor_msg) == state.anchor


RECONCILE_MATCH = "match"
RECONCILE_COLLAPSED = "collapsed"
RECONCILE_RESET = "reset"


def reconcile(state: SessionState, cid: str, contexts: list[dict]) -> str:
    """把压缩状态与当前历史对齐（就地修改 state），返回所做的处理。

    - RECONCILE_MATCH：状态适用，无需变动；
    - RECONCILE_COLLAPSED：水位线锚点找不到了但摘要在手，判定为压缩视图
      已被平台回存固化——水位线归零、摘要保留，整份历史此后视为尾部；
      平台不回存时锚点每轮都能对上，永远不会走到这一支；
    - RECONCILE_RESET：对话切换或历史被清空，压缩状态作废（cid 重新锚定）。
    """
    if state.cid != cid:
        state.cid = cid
        state.reset_compression()
        return RECONCILE_RESET
    if not contexts:
        if state.watermark or state.summary or state.anchor:
            state.reset_compression()
            return RECONCILE_RESET
        return RECONCILE_MATCH
    if state.watermark <= 0:
        return RECONCILE_MATCH
    if state.watermark <= len(contexts):
        anchor_msg = contexts[state.watermark - 1]
        if isinstance(anchor_msg, dict) and fingerprint(anchor_msg) == state.anchor:
            return RECONCILE_MATCH
    if state.summary:
        state.watermark = 0
        state.anchor = ""
        return RECONCILE_COLLAPSED
    state.reset_compression()
    return RECONCILE_RESET


def validate_threshold(raw: float) -> tuple[float, str]:
    """校验触发阈值必须落在 (0, 0.82) 内，否则回退并给出告警文案。

    这是整套抢先压缩方案唯一的失效条件：阈值不低于内置压缩器的 0.82，
    内置压缩器会抢先触发、自研压缩形同虚设，必须硬性拦住。
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return THRESHOLD_FALLBACK, f"压缩阈值配置无法解析，已回退 {THRESHOLD_FALLBACK}"
    if not math.isfinite(value) or value <= 0:
        return THRESHOLD_FALLBACK, f"压缩阈值必须为正数，已回退 {THRESHOLD_FALLBACK}"
    if value >= BUILTIN_THRESHOLD:
        return (
            THRESHOLD_MAX,
            f"压缩阈值 {value} 不低于 AstrBot 内置压缩器的 {BUILTIN_THRESHOLD}，"
            f"内置压缩器会抢先触发、本插件压缩将形同虚设；已强制钳制为 {THRESHOLD_MAX}",
        )
    return value, ""


# ---------------------------------------------------------------- 压缩规划


@dataclass
class CompressionPlan:
    """一次滚动摘要的范围：把 contexts[old_watermark:new_watermark] 卷入摘要。"""

    new_watermark: int
    to_summarize: list[dict] = field(default_factory=list)


def plan_compression(
    contexts: list[dict],
    watermark: int,
    keep_recent_turns: int,
) -> CompressionPlan | None:
    """选定本次滚动摘要覆盖的消息范围。

    以 user 消息为边界切轮，最近 keep_recent_turns 轮保留原文
    （至少 1 轮：最新一整轮无条件保原文），其余旧轮卷入摘要。
    没有可卷入的旧轮时返回 None。
    """
    start = max(watermark, split_leading_system(contexts))
    turns = split_turns(contexts, start)
    keep = max(1, keep_recent_turns)
    if len(turns) <= keep:
        return None
    boundary_turn = turns[len(turns) - keep]
    new_watermark = boundary_turn.start
    if new_watermark <= watermark:
        return None
    return CompressionPlan(
        new_watermark=new_watermark,
        to_summarize=[
            m for m in contexts[watermark:new_watermark] if isinstance(m, dict)
        ],
    )


def apply_summary(
    state: SessionState,
    contexts: list[dict],
    plan: CompressionPlan,
    summary: str,
) -> None:
    """摘要成功后推进水位线并记录锚点指纹。"""
    state.summary = summary.strip()
    state.watermark = plan.new_watermark
    state.anchor = fingerprint(contexts[plan.new_watermark - 1])
    state.compressed_at = time.time()
    state.record_success()


# ---------------------------------------------------------------- 视图构建


def build_view(
    contexts: list[dict],
    state: SessionState,
    summary_message: dict | None,
) -> list[dict]:
    """拼出发送视图：开头 system 消息 + 摘要块 + 水位线之后的原文。"""
    n_system = split_leading_system(contexts)
    watermark = max(state.watermark, n_system) if state.watermark else n_system
    view = list(contexts[:n_system])
    if summary_message is not None and state.summary:
        view.append(summary_message)
    view.extend(contexts[watermark:] if state.watermark else contexts[n_system:])
    return view


def _clip_content(content, cap: int):
    if isinstance(content, str) and len(content) > cap:
        omitted = len(content) - cap
        return content[:cap] + f"…（Memory Beyond 应急裁剪，省略 {omitted} 字符）"
    return content


def clip_messages(messages: list[dict], cap: int) -> list[dict]:
    """裁剪单条消息的超长 content（应急视图专用，只影响本轮发送）。"""
    clipped = []
    for message in messages:
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            message = {**message, "content": _clip_content(message["content"], cap)}
        clipped.append(message)
    return clipped


def fit_tail_to_budget(
    tail: list[dict],
    estimator: tokens.TokenEstimator,
    budget: int,
) -> tuple[list[dict], bool]:
    """把水位线之后的原文消息压进预算：反复对半砍旧轮次，仍超则裁剪 content。

    只作用于本轮发送视图，水位线与磁盘状态都不动；在会把发送视图回存为
    历史的平台上，被裁内容会随回存固化丢失（有损兜底，仅摘要不可用时启用）。
    返回 (裁剪后的消息列表, 是否发生了裁剪)。
    """
    if estimator.messages(tail) <= budget:
        return tail, False

    turns = split_turns(tail)
    while len(turns) > 1:
        remaining = estimator.messages([m for t in turns for m in t.messages])
        if remaining <= budget:
            break
        turns = turns[max(1, len(turns) // 2):]
    kept = [m for t in turns for m in t.messages]

    cap = CLIP_START_CHARS
    while estimator.messages(kept) > budget and cap >= CLIP_MIN_CHARS:
        kept = clip_messages(kept, cap)
        cap //= 2
    return kept, True


# ---------------------------------------------------------------- 转写渲染


def _content_to_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for seg in content:
            if isinstance(seg, dict):
                seg_type = seg.get("type", "")
                if seg_type == "text":
                    parts.append(str(seg.get("text", "")))
                else:
                    parts.append(f"[{seg_type or '非文本内容'}]")
            else:
                parts.append(str(seg))
        return " ".join(p for p in parts if p)
    return str(content)


def render_transcript(messages: list[dict]) -> str:
    """把待摘要的消息渲染成转写文本；完整工具输出被截断（摘要本就该丢弃它们）。"""
    lines: list[str] = []
    for message in messages:
        role = message.get("role", "")
        text = _content_to_text(message.get("content")).strip()
        if len(text) > TRANSCRIPT_MSG_CHARS:
            text = text[:TRANSCRIPT_MSG_CHARS] + "…（截断）"
        if role == "assistant":
            for tool_call in message.get("tool_calls") or []:
                if isinstance(tool_call, dict):
                    fn = tool_call.get("function") or {}
                    args = str(fn.get("arguments", ""))[:200]
                    lines.append(f"助手：[调用工具 {fn.get('name', '?')}({args})]")
            if text:
                lines.append(f"助手：{text}")
        elif role == "tool":
            lines.append(f"工具结果：{text or '（空）'}")
        elif role == "user":
            lines.append(f"用户：{text or '（非文本消息）'}")
        elif role == "system":
            continue
        else:
            lines.append(f"{role}：{text}")
    return "\n".join(lines)
