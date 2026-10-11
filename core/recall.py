"""相关记忆召回：每轮挑出与当前消息最相关的少量记忆，把全文附在本轮请求末尾。

索引只是目录，模型需要细节时还得自己调 memory_read；召回把这一步提前：
插件按本轮消息选出最多几条相关记忆，以 _no_save 数据块附在历史末尾、
用户新消息之前。块放在末尾而不是开头，逐轮变化也不破坏前缀缓存。

挑选分两档，都不需要向量模型：
- 身份必选：发送者本人、被 @ 的人、消息里出现的数字 ID，对应的
  user-<ID>.md 存在就一定带上——聊天里最常用的记忆就是"正在说话的人"；
- 相关性：关键词模式按词重叠打分（英文按词、中文按相邻两字），名称与
  描述命中权重更高；模型模式把记忆清单交给摘要模型挑选，失败退回关键词。

每条召回记忆标注写入时间，超过一天的附上时效提醒：记忆是写入时的观察，
不是现状。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from .memstore import MemoryHeader

RECALL_BLOCK_OPEN = "<memory_beyond_recall>"
RECALL_BLOCK_CLOSE = "</memory_beyond_recall>"

DEFAULT_MAX_FILES = 5
# 单条记忆注入的字符上限与整块上限：召回是辅助通道，不能挤占对话本身
FILE_CHARS = 3000
TOTAL_CHARS = 9000
# 关键词打分的门槛：名称/描述命中一次即达标，只靠正文需命中三次
MIN_SCORE = 3
KEY_WEIGHT = 3
BODY_CAP = 6
DAY_SECONDS = 86400

_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RUN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_DIGIT_ID_RE = re.compile(r"(?<!\d)(\d{5,12})(?!\d)")

# 高频虚词组合：出现在几乎任何中文句子里，计入打分只会制造噪声
_STOP_BIGRAMS = frozenset(
    """
    什么 怎么 这个 那个 一个 我们 你们 他们 她们 它们 自己 就是 不是 没有
    可以 知道 还是 已经 因为 所以 但是 如果 然后 现在 时候 这样 那样 这么
    那么 一下 一些 有点 觉得 应该 真的 还有 的话 不过 而且 或者 这些 那些
    的人 了吗 是不 不要 不会 一起 东西 事情 问题 今天 明天 昨天 大家 有没
    是什 么样 么时 可能 一直 一样 出来 起来 下来 上去 过来 好的 好像 为什
    """.split()
)
_STOP_WORDS = frozenset(
    "the and for you are with this that what how not but can was have from".split()
)


def tokenize(text: str) -> set[str]:
    """把文本拆成召回打分用的词集：英文/数字按词，中文按相邻两字。"""
    lowered = (text or "").lower()
    tokens: set[str] = set()
    for word in _ASCII_WORD_RE.findall(lowered):
        if word in _STOP_WORDS:
            continue
        # 纯数字只认像 ID 的长串，短数字（年份、序号）噪声太大
        if word.isdigit() and len(word) < 5:
            continue
        if len(word) >= 2:
            tokens.add(word)
    for run in _CJK_RUN_RE.findall(lowered):
        for i in range(len(run) - 1):
            pair = run[i : i + 2]
            if pair not in _STOP_BIGRAMS:
                tokens.add(pair)
    return tokens


def score(query_tokens: set[str], header: MemoryHeader) -> int:
    """关键词相关度：名称与描述的命中按 KEY_WEIGHT 计，正文命中计 1、封顶 BODY_CAP。"""
    if not query_tokens:
        return 0
    key_tokens = tokenize(f"{header.name.replace('-', ' ')} {header.description}")
    key_hits = len(query_tokens & key_tokens)
    body_hits = len((query_tokens - key_tokens) & tokenize(header.text))
    return key_hits * KEY_WEIGHT + min(body_hits, BODY_CAP)


def identity_files(
    sender_id: str, mentioned_ids: list[str], query: str
) -> list[str]:
    """身份必选的候选文件名：发送者、被 @ 的人、消息正文里出现的数字 ID。"""
    ids: list[str] = []
    for raw in [sender_id, *mentioned_ids, *_DIGIT_ID_RE.findall(query or "")]:
        uid = str(raw or "").strip()
        if uid and uid.isdigit() and uid not in ids:
            ids.append(uid)
    return [f"user-{uid}.md" for uid in ids]


@dataclass
class Candidate:
    """一个可召回的记忆：作用域标签（global / session）+ 头信息。"""

    scope: str
    header: MemoryHeader

    @property
    def ref(self) -> str:
        return f"{self.scope}/{self.header.file}"


def select_by_keywords(
    candidates: list[Candidate],
    query: str,
    forced_files: list[str],
    limit: int = DEFAULT_MAX_FILES,
) -> list[Candidate]:
    """身份必选在前，其余按关键词得分（同分取较新）补足到 limit 条。"""
    if limit <= 0:
        return []
    chosen: list[Candidate] = []
    by_file: dict[str, list[Candidate]] = {}
    for cand in candidates:
        by_file.setdefault(cand.header.file, []).append(cand)
    for name in forced_files:
        for cand in by_file.get(name, []):
            # 记人只在会话作用域；全局作用域不该有 user-<ID> 文件，也不强选
            if cand.scope == "session" and cand not in chosen:
                chosen.append(cand)
    query_tokens = tokenize(query)
    scored = []
    for cand in candidates:
        if cand in chosen:
            continue
        value = score(query_tokens, cand.header)
        if value >= MIN_SCORE:
            scored.append((value, cand.header.mtime, cand))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    chosen.extend(cand for _, _, cand in scored)
    return chosen[:limit]


# ---------------------------------------------------------------- 模型挑选

SELECTOR_PROMPT = """你在帮一个聊天机器人挑选记忆。下面是它的记忆清单（每行：编号、作用域/文件名、类型、写入时间、一句话描述），以及它刚收到的一条新消息。

请选出机器人回复这条消息时**确实用得上**的记忆，最多 {limit} 条。只选有把握有用的；拿不准就不选；一条都用不上就输出空数组。

记忆清单：
{manifest}

新消息：
{query}

只输出一个 JSON 数组，元素是所选记忆的「作用域/文件名」，例如 ["session/user-12345678.md"]，不要输出其他内容。"""


def age_days(mtime: float, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return max(0, int((now - mtime) // DAY_SECONDS))


def age_label(mtime: float, now: float | None = None) -> str:
    days = age_days(mtime, now)
    if days == 0:
        return "今天"
    if days == 1:
        return "昨天"
    return f"{days} 天前"


def build_selector_prompt(
    candidates: list[Candidate], query: str, limit: int, now: float | None = None
) -> str:
    lines = []
    for i, cand in enumerate(candidates, 1):
        header = cand.header
        description = header.description or "（无描述）"
        lines.append(
            f"{i}. {cand.ref}｜{header.type or '未标注'}｜"
            f"{age_label(header.mtime, now)}｜{description}"
        )
    return SELECTOR_PROMPT.format(
        limit=limit, manifest="\n".join(lines), query=(query or "").strip()[:2000]
    )


def parse_selector_response(
    text: str, candidates: list[Candidate]
) -> list[Candidate] | None:
    """解析模型挑选结果：空数组表示"没有相关的"；找不到 JSON 数组返回 None，
    调用方据此退回关键词模式——模型答了句闲话不该让召回落空。"""
    if not text:
        return None
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    by_ref = {cand.ref: cand for cand in candidates}
    by_file: dict[str, Candidate] = {}
    for cand in candidates:
        by_file.setdefault(cand.header.file, cand)
    chosen: list[Candidate] = []
    for item in data:
        ref = str(item or "").strip()
        cand = by_ref.get(ref) or by_file.get(ref.rsplit("/", 1)[-1])
        if cand is not None and cand not in chosen:
            chosen.append(cand)
    return chosen


# ---------------------------------------------------------------- 注入块


@dataclass
class Recalled:
    ref: str
    content: str
    mtime: float


def freshness_note(mtime: float, now: float | None = None) -> str:
    """超过一天的记忆附上时效提醒；今天、昨天写的不打扰。"""
    days = age_days(mtime, now)
    if days <= 1:
        return ""
    return (
        f"（这条记忆写于 {days} 天前，记录的是当时的情况；"
        "涉及现状的内容请先与当前对话核实再采信）"
    )


def build_recall_block(
    items: list[Recalled], data_notice: str, now: float | None = None
) -> str | None:
    """拼召回块；超出字符预算的条目整条略去（截断只发生在单条内部）。"""
    sections: list[str] = []
    used = 0
    for item in items:
        content = item.content.strip()
        if len(content) > FILE_CHARS:
            content = content[:FILE_CHARS] + "\n…（已截断，完整内容用 memory_read 读取）"
        if sections and used + len(content) > TOTAL_CHARS:
            break
        used += len(content)
        title = f"### {item.ref}（{age_label(item.mtime, now)}写入）"
        note = freshness_note(item.mtime, now)
        sections.append("\n".join(part for part in (title, note, content) if part))
    if not sections:
        return None
    body = "\n\n".join(sections)
    return (
        f"{RECALL_BLOCK_OPEN}\n"
        f"{data_notice}\n"
        "以下是插件按本轮消息自动召回的相关记忆全文，可直接参考；"
        "它们和索引一样只是线索，与当前对话矛盾时以当前对话为准，并更新或删除过时的记忆。\n\n"
        f"{body}\n"
        f"{RECALL_BLOCK_CLOSE}"
    )


def build_recall_message(block: str) -> dict:
    # 与索引块相同：_no_save 声明不回存，召回内容只存在于本轮请求
    return {"role": "user", "content": block, "_no_save": True}
