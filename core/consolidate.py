"""记忆整理：把一个作用域的全部记忆交给模型复盘，产出一份可预览的修改方案。

记忆越积越多，难免出现同一个人分散在几个文件、前后矛盾、相对日期失效、
描述写得含糊这类问题。整理就是定期复盘：合并重复、修正过时、规范格式。
方案只包含两种操作——整文件写入与删除——先在 WebUI 里预览，管理员勾选
确认后才落盘；模型不直接动磁盘。应用时先写后删，合并途中出错也不丢内容。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .memstore import (
    INDEX_FILENAME,
    MEMORY_FILE_SUFFIX,
    MEMORY_TYPES,
    MemoryHeader,
    compose_memory,
)
from .recall import age_label

# 一次整理最多带上的记忆正文总字符数；超出的文件本次不参与
MAX_INPUT_CHARS = 60000
MAX_OPERATIONS = 60
# 单条写入正文上限：超出的条目整条丢弃而不是截断——截断后合并文件残缺、
# 源文件又照删，内容就丢了。上限与输入预算一致，正常合并不会撞上
MAX_CONTENT_CHARS = MAX_INPUT_CHARS
MAX_REASON_CHARS = 200
# 预览与应用之间文件时间戳允许的误差（JSON 往返的浮点精度）
MTIME_TOLERANCE = 1e-3

_OPERATIONS_RE = re.compile(r"<operations>(.*?)</operations>", re.S | re.I)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|```$", re.M)
_SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.md$")

CONSOLIDATE_PROMPT = """你是聊天机器人的记忆整理助手。下面是记忆库「{scope_label}」里的全部记忆文件（每个文件标注了最近写入时间与最近被用到的时间）。请像整理笔记一样做一次复盘：

- 合并重复：同一个人（同一个数字 ID）或同一件事分散在几个文件里的，合并进一个文件——记人的文件统一命名为 user-<数字ID>.md——并删除其余文件；
- 修正过时与矛盾：同一事实前后矛盾时以较新的为准，删去明显已失效的内容；正文里的相对日期（明天、下周）按写入时间换算成绝对日期；
- 精简：长期没被用到、内容又明显只是一次性的条目（如早已过去的临时安排）可以删除；有长期价值的即使没用过也保留；
- 规范格式：type 只能是 user、feedback、project、reference 之一；description 写成一句能让人判断"何时该读全文"的钩子；feedback 与 project 类正文附 **Why:** 与 **How to apply:** 两行；
- 不编造：只能重组、精简、改写已有信息，不得添加文件里没有的事实；
- 拿不准的不动。不需要整理就输出空数组。

输出放在 <operations></operations> 标签内，是一个 JSON 数组，元素只有两种：
{{"op": "write", "file": "文件名.md", "type": "类型", "description": "一句话钩子", "content": "完整正文（不含 frontmatter）", "reason": "为什么这样改"}}
{{"op": "delete", "file": "文件名.md", "reason": "为什么删除，如：已合并进 user-123.md"}}
write 会整文件覆盖（新建或改写），所以 content 必须是完整正文，不能只写改动的部分。

记忆文件：
{files}"""


@dataclass
class Operation:
    op: str
    file: str
    reason: str = ""
    type: str = ""
    description: str = ""
    content: str = ""
    # 预览时目标文件的写入时间（不存在为 0），应用时据此识别期间被改过的文件
    base_mtime: float = 0.0

    def render(self) -> str:
        name = self.file[: -len(MEMORY_FILE_SUFFIX)]
        return compose_memory(name, self.description, self.type, self.content)

    def to_dict(self) -> dict:
        data = {
            "op": self.op,
            "file": self.file,
            "reason": self.reason,
            "baseMtime": self.base_mtime,
        }
        if self.op == "write":
            data.update(
                type=self.type, description=self.description, content=self.content
            )
        return data


def build_prompt(
    scope_label: str,
    files: list[tuple[MemoryHeader, str]],
    now: float | None = None,
) -> tuple[str, list[str]]:
    """拼整理提示词；返回 (提示词, 因超出预算未纳入的文件名)。"""
    blocks: list[str] = []
    skipped: list[str] = []
    used = 0
    for header, content in files:
        if used + len(content) > MAX_INPUT_CHARS:
            skipped.append(header.file)
            continue
        used += len(content)
        blocks.append(
            f"=== {header.file}（{age_label(header.updated, now)}写入，"
            f"{usage_label(header, now)}）===\n{content.strip()}"
        )
    prompt = CONSOLIDATE_PROMPT.format(
        scope_label=scope_label, files="\n\n".join(blocks) or "（空）"
    )
    return prompt, skipped


def usage_label(header: MemoryHeader, now: float | None = None) -> str:
    """整理清单里的使用情况：模型据此判断哪些记忆长期没派上用场。"""
    if header.last_used <= 0:
        return "从未被用到"
    return f"最近用到于{age_label(header.last_used, now)}"


def _load_array(text: str):
    match = _OPERATIONS_RE.search(text or "")
    raw = _FENCE_RE.sub("", match.group(1) if match else (text or "")).strip()
    try:
        return json.loads(raw)
    except ValueError:
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(raw[start : end + 1])
        except ValueError:
            return None


def parse_operations(text: str, existing: set[str]) -> list[Operation] | None:
    """解析并校验整理方案；输出无法解析时返回 None（区别于"无需整理"的空列表）。

    丢弃的条目：文件名不合法、指向 MEMORY.md、删除不存在的文件、
    写入缺正文、正文超长或类型非法、同一文件出现多次（只认第一次）。
    """
    data = _load_array(text)
    if not isinstance(data, list):
        return None
    ops: list[Operation] = []
    seen: set[str] = set()
    for entry in data:
        if len(ops) >= MAX_OPERATIONS or not isinstance(entry, dict):
            continue
        op = str(entry.get("op", "")).strip().lower()
        file = str(entry.get("file", "")).strip()
        if not file.endswith(MEMORY_FILE_SUFFIX):
            file += MEMORY_FILE_SUFFIX
        if (
            file in seen
            or file.lower() == INDEX_FILENAME.lower()
            or not _SAFE_FILE_RE.match(file)
        ):
            continue
        reason = " ".join(str(entry.get("reason", "")).split())[:MAX_REASON_CHARS]
        try:
            base_mtime = float(entry.get("baseMtime") or 0.0)
        except (TypeError, ValueError):
            base_mtime = 0.0
        if op == "delete":
            if file not in existing:
                continue
            ops.append(
                Operation(op="delete", file=file, reason=reason, base_mtime=base_mtime)
            )
        elif op == "write":
            mem_type = str(entry.get("type", "")).strip().lower()
            content = str(entry.get("content", "")).strip()
            if (
                mem_type not in MEMORY_TYPES
                or not content
                or len(content) > MAX_CONTENT_CHARS
            ):
                continue
            ops.append(
                Operation(
                    op="write",
                    file=file,
                    reason=reason,
                    type=mem_type,
                    description=" ".join(str(entry.get("description", "")).split()),
                    content=content,
                    base_mtime=base_mtime,
                )
            )
        else:
            continue
        seen.add(file)
    return ops


def operations_from_payload(raw, existing: set[str]) -> list[Operation]:
    """把 WebUI 回传的（可能被管理员改过的）方案重新校验一遍再应用。"""
    if not isinstance(raw, list):
        return []
    ops = parse_operations(json.dumps(raw, ensure_ascii=False), existing)
    return ops or []


def requested_files(raw) -> set[str]:
    """回传方案里点到的文件名（不论是否通过校验），用于报告被跳过的条目。"""
    files: set[str] = set()
    if not isinstance(raw, list):
        return files
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        file = str(entry.get("file", "")).strip()
        if not file:
            continue
        if not file.endswith(MEMORY_FILE_SUFFIX):
            file += MEMORY_FILE_SUFFIX
        files.add(file)
    return files


def is_stale(op: Operation, current_mtime: float | None) -> bool:
    """预览之后目标文件是否变过：已有文件被改写、或要新建的文件已被别人建了。"""
    return abs((current_mtime or 0.0) - op.base_mtime) > MTIME_TOLERANCE


def ordered(ops: list[Operation]) -> list[Operation]:
    """应用顺序：先写后删，合并途中出错也不会先把源文件删掉。"""
    return [op for op in ops if op.op == "write"] + [
        op for op in ops if op.op == "delete"
    ]
