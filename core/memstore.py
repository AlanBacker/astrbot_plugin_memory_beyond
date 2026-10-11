"""文件式记忆库。

一个文件一条事实（Markdown + frontmatter），每个作用域一个 MEMORY.md
索引，索引一条记忆占一行、只放指针永不放正文。

聊天场景的双层作用域：
    global/            —— 机器人自我的全局记忆（偏好、行为准则），所有会话共享注入
    session/<会话键>/  —— 当前会话（UMO）的记忆：本群 / 本私聊里的人和事

索引自动注入通道有 200 行 / 25KB 上限，超出从底部截断；截断时在末尾追加
一行说明还有多少行未加载、可用搜索工具找到——否则模型根本不知道自己有
记忆没看到。

索引由插件自动维护，不开放直接写入：每次写入记忆文件时，从 frontmatter
的 description 提取钩子、同步增改对应索引行；删除文件时同步移除索引行。
模型改索引行的唯一途径是改文件本身——单一事实来源，杜绝往索引里写正文、
漏登记、格式跑偏这一整类错误。索引接近注入上限时在写入回执里提醒合并
记忆文件。工具读文件读到的始终是完整文件（截断只发生在注入通道）。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

INDEX_FILENAME = "MEMORY.md"
INDEX_MAX_LINES = 200
INDEX_MAX_BYTES = 25 * 1024
INDEX_WARN_RATIO = 0.8

MEMORY_FILE_SUFFIX = ".md"
MAX_FILE_BYTES = 64 * 1024
MAX_SEARCH_RESULTS = 8
MAX_SEARCH_EXCERPT = 160
MAX_HOOK_CHARS = 100

# 召回打分只看文件开头这么多字符，避免个别大文件拖慢每轮的扫描
HEADER_TEXT_CHARS = 8 * 1024
MEMORY_TYPES = ("user", "feedback", "project", "reference")

# 插件自动维护的三个戳（写在 frontmatter 的 metadata 下）：
#   created  首次写入时间；改写、整理都不会把老记忆变"新"
#   updated  最近一次修改时间；时效提醒按它算，不再依赖会被整理刷新的 mtime
#   source   说出这条信息的人的数字 ID；与文件所记的人不同即为旁人转述
STAMP_KEYS = ("created", "updated", "source")
STAMP_FORMAT = "%Y-%m-%d %H:%M"
# source 最多保留最近几个 ID，避免被反复改写的文件越积越长
MAX_SOURCES = 5

_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
# 已经是 safe_key 输出形态的目录名（WebUI 按目录名定位会话时校验用）
_SCOPE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
_HOOK_WS_RE = re.compile(r"\s+")
_LINK_RE = re.compile(r"\[\[([^\[\]\n]{1,120})\]\]")
_INDEX_TARGET_RE = re.compile(r"\]\(([^()\s]+\.md)\)")
_BLOCK_SCALAR_RE = re.compile(r"^[>|][+-]?$")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _frontmatter_lines(block: str) -> list[str]:
    """把 frontmatter 展开成逻辑行：`key: >-` / `key: |` 的多行值折成一行。"""
    lines = block.splitlines()
    result: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        key, sep, value = line.partition(":")
        if not (sep and _BLOCK_SCALAR_RE.match(value.strip())):
            result.append(line)
            i += 1
            continue
        indent = _indent(line)
        parts: list[str] = []
        i += 1
        while i < len(lines) and (not lines[i].strip() or _indent(lines[i]) > indent):
            if lines[i].strip():
                parts.append(lines[i].strip())
            i += 1
        result.append(f"{key}: {' '.join(parts)}")
    return result


def format_stamp(ts: float) -> str:
    """本地时间，如 2026-10-11 18:30。"""
    return time.strftime(STAMP_FORMAT, time.localtime(ts))


def parse_stamp(text: str) -> float | None:
    """解析 created / updated 戳；只认日期或日期加时分，其他写法当没有。"""
    text = (text or "").strip()
    for fmt in (STAMP_FORMAT, "%Y-%m-%d"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    return None


_SOURCE_SEP_RE = re.compile(r"[,\s，、;；]+")


def split_sources(text: str) -> list[str]:
    """source 字段拆成 ID 列表（去重保序）。"""
    items: list[str] = []
    for raw in _SOURCE_SEP_RE.split(text or ""):
        item = raw.strip()
        if item and item not in items:
            items.append(item)
    return items


def merge_sources(existing: str, added: str) -> str:
    """并入新的来源 ID，只留最近 MAX_SOURCES 个。"""
    items = split_sources(existing)
    for item in split_sources(added):
        if item not in items:
            items.append(item)
    return ", ".join(items[-MAX_SOURCES:])


def parse_description(content: str) -> tuple[str, bool]:
    """从记忆文件内容提取索引钩子：(钩子文本, 是否来自 frontmatter)。

    优先取 frontmatter 的 description；文件没写 frontmatter 时退回正文
    首个非空行——保证索引行永远有钩子，但回执会提醒补全 frontmatter。
    """
    match = _FRONTMATTER_RE.match(content)
    if match:
        for line in _frontmatter_lines(match.group(1)):
            stripped = line.strip()
            if stripped.startswith("description:"):
                desc = stripped.split(":", 1)[1].strip().strip("'\"")
                if desc:
                    return desc, True
    body = content[match.end():] if match else content
    for line in body.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned, False
    return "", False


@dataclass
class MemoryDoc:
    """一个记忆文件拆开后的各部分；字段缺失时为空串。"""

    name: str
    description: str
    type: str
    body: str
    has_frontmatter: bool
    # metadata 下由插件维护的戳，原样字符串；缺失为空串
    created: str = ""
    updated: str = ""
    source: str = ""


def parse_memory(content: str) -> MemoryDoc:
    """拆解记忆文件：frontmatter 的 name / description / type、三个戳与正文。

    type 按规范写在 metadata 下一级（metadata:\\n  type: user），也兼容
    直接写在顶层的 type:，两处都有时以 metadata 下的为准。
    """
    match = _FRONTMATTER_RE.match(content or "")
    fields: dict[str, str] = {}
    nested: dict[str, str] = {}
    if match:
        in_metadata = False
        for line in _frontmatter_lines(match.group(1)):
            if not line.strip():
                continue
            key, sep, value = line.strip().partition(":")
            if not sep:
                continue
            key = key.strip().lower()
            value = value.strip().strip("'\"")
            if line[:1] in (" ", "\t"):
                if in_metadata and (key == "type" or key in STAMP_KEYS):
                    nested.setdefault(key, value)
                continue
            in_metadata = key == "metadata" and not value
            if key in ("name", "description", "type"):
                fields.setdefault(key, value)
    body = content[match.end():] if match else (content or "")
    mem_type = (nested.get("type") or fields.get("type", "")).lower()
    return MemoryDoc(
        name=fields.get("name", ""),
        description=fields.get("description", ""),
        type=mem_type,
        body=body.strip("\n"),
        has_frontmatter=match is not None,
        created=nested.get("created", ""),
        updated=nested.get("updated", ""),
        source=nested.get("source", ""),
    )


def compose_memory(
    name: str,
    description: str,
    mem_type: str,
    body: str,
    created: str = "",
    updated: str = "",
    source: str = "",
) -> str:
    """按标准格式拼出记忆文件：frontmatter（name / description / metadata.type）+ 正文。

    三个戳平时不用传——写入时由 stamp_memory 自动补；传了就原样写进 metadata。
    """
    description = _HOOK_WS_RE.sub(" ", description or "").strip()
    meta = [f"  type: {mem_type.strip()}"]
    for key, value in (("created", created), ("updated", updated), ("source", source)):
        value = " ".join(str(value or "").split())
        if value:
            meta.append(f"  {key}: {value}")
    return (
        "---\n"
        f"name: {name.strip()}\n"
        f"description: {description}\n"
        "metadata:\n" + "\n".join(meta) + "\n"
        "---\n\n"
        f"{(body or '').strip()}\n"
    )


def _rewrite_metadata(inner: str, fields: dict[str, str]) -> str:
    """把 fields 写进 frontmatter 的 metadata 块：同名旧行替换，其余行原样；
    没有 metadata 块就在末尾补一个。只认 metadata 的直接子键，更深的
    缩进（块标量的续行）不碰。"""
    lines = inner.split("\n")
    out: list[str] = []
    placed = False
    i = 0
    while i < len(lines):
        line = lines[i]
        key, sep, value = line.partition(":")
        is_meta = (
            not placed
            and bool(sep)
            and line[:1] not in (" ", "\t")
            and key.strip().lower() == "metadata"
            and not value.strip()
        )
        out.append(line)
        i += 1
        if not is_meta:
            continue
        indent = ""
        while i < len(lines) and (lines[i][:1] in (" ", "\t") or not lines[i].strip()):
            nested_line = lines[i]
            i += 1
            if nested_line.strip():
                depth = nested_line[: _indent(nested_line)]
                indent = indent or depth
                nested_key = nested_line.strip().partition(":")[0].strip().lower()
                if depth == indent and nested_key in fields:
                    continue
            out.append(nested_line)
        # 块尾的空行留在戳之后，顶层键之间的留白不变
        tail: list[str] = []
        while out and not out[-1].strip():
            tail.append(out.pop())
        indent = indent or "  "
        out.extend(f"{indent}{k}: {v}" for k, v in fields.items())
        out.extend(reversed(tail))
        placed = True
    if not placed:
        while out and not out[-1].strip():
            out.pop()
        out.append("metadata:")
        out.extend(f"  {k}: {v}" for k, v in fields.items())
    return "\n".join(out)


def stamp_memory(
    content: str, now: float, source: str = "", previous: str | None = None
) -> str:
    """补齐 metadata 下的 created / updated / source，其余内容一个字不动。

    created 先取已有文件的（改写、整理都不会把老记忆变"新"），没有才取
    内容里写的，再没有才取现在；updated 一律取现在；source 取内容里写的
    （没写则继承已有文件的），再并入本次的来源 ID。没有 frontmatter 的
    内容原样返回。
    """
    match = _FRONTMATTER_RE.match(content or "")
    if not match:
        return content
    doc = parse_memory(content)
    prev = parse_memory(previous) if previous else None
    created = ""
    for candidate in ((prev.created if prev else ""), doc.created):
        if candidate and parse_stamp(candidate) is not None:
            created = candidate.strip()
            break
    fields = {
        "created": created or format_stamp(now),
        "updated": format_stamp(now),
    }
    sources = merge_sources(doc.source or (prev.source if prev else ""), source)
    if sources:
        fields["source"] = sources
    head = _rewrite_metadata(match.group(1), fields)
    return content[: match.start(1)] + head + content[match.end(1):]


def extract_links(text: str) -> list[str]:
    """正文里的 [[name]] 链接目标（去重保序）。悬空链接也照样返回。"""
    seen: list[str] = []
    for match in _LINK_RE.finditer(text or ""):
        target = match.group(1).strip()
        if target and target not in seen:
            seen.append(target)
    return seen


def _clean_hook(description: str) -> str:
    """钩子清洗：折叠空白、方括号转全角（防止破坏索引行结构）、限长。"""
    hook = _HOOK_WS_RE.sub(" ", description or "").strip()
    hook = hook.replace("[", "［").replace("]", "］")
    if len(hook) > MAX_HOOK_CHARS:
        hook = hook[:MAX_HOOK_CHARS] + "…"
    return hook


def format_index_line(filename: str, description: str) -> str:
    """标准索引行：`- [名称](文件名) — 钩子`。名称即文件名去掉 .md。"""
    stem = filename[: -len(MEMORY_FILE_SUFFIX)]
    hook = _clean_hook(description) or "（无描述）"
    return f"- [{stem}]({filename}) — {hook}"


def safe_key(raw: str) -> str:
    """把用户键 / 会话键转成安全目录名；不可逆字符用短哈希保证唯一。"""
    cleaned = _SAFE_KEY_RE.sub("_", raw)[:80].strip("._") or "unknown"
    if cleaned == raw:
        return cleaned
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
    return f"{cleaned}-{digest}"


@dataclass
class IndexSnapshot:
    """经截断处理、可直接注入的索引文本。"""

    text: str
    total_lines: int
    loaded_lines: int

    @property
    def truncated(self) -> bool:
        return self.loaded_lines < self.total_lines


@dataclass
class WriteReport:
    ok: bool
    message: str


@dataclass
class MemoryHeader:
    """记忆文件的头信息：召回打分、WebUI 列表、整理时的清单都用它。"""

    file: str
    name: str
    description: str
    type: str
    mtime: float
    size: int
    # 小写化的文件开头（含 frontmatter），供关键词召回打分
    text: str = ""
    links: list[str] = field(default_factory=list)
    # 由 frontmatter 的戳解析；updated 缺省时退回 mtime，created 缺省为 0
    updated: float = 0.0
    created: float = 0.0
    source: str = ""

    def __post_init__(self) -> None:
        if not self.updated:
            self.updated = self.mtime


@dataclass
class IndexReport:
    """MEMORY.md 的健康状况：体积、是否截断、与实际文件是否对得上。"""

    exists: bool
    lines: int
    size: int
    truncated: bool
    # 有文件但索引里没有对应行
    missing: list[str]
    # 索引行指向的文件已不存在
    dangling: list[str]


def _truncate_index(raw: str) -> IndexSnapshot:
    lines = raw.splitlines()
    total = len(lines)
    kept: list[str] = []
    used_bytes = 0
    for line in lines:
        line_bytes = len(line.encode("utf-8")) + 1
        if len(kept) >= INDEX_MAX_LINES or used_bytes + line_bytes > INDEX_MAX_BYTES:
            break
        kept.append(line)
        used_bytes += line_bytes
    loaded = len(kept)
    text = "\n".join(kept)
    if loaded < total:
        text += (
            f"\n……（索引超出加载上限，还有 {total - loaded} 行未在此显示；"
            "未显示的记忆可用 memory_search 工具检索。"
            "请把相关记忆合并进同一个文件、删除多余文件以精简索引）"
        )
    return IndexSnapshot(text=text, total_lines=total, loaded_lines=loaded)


class ScopeStore:
    """单个作用域目录内的读 / 写删 / 搜索，带路径约束与文件锁。"""

    def __init__(self, root: Path):
        self.root = root
        self._lock = asyncio.Lock()
        # 文件名 -> ((mtime_ns, size), 头信息)；按修改时间失效
        self._headers: dict[str, tuple[tuple[int, int], MemoryHeader]] = {}
        # headers() 在工作线程里跑，召回与 WebUI 可能并发调用
        self._headers_lock = threading.Lock()

    # ------------------------------------------------------------ 路径约束

    def _resolve(self, name: str) -> Path | None:
        """把记忆文件名解析为作用域内的绝对路径；非法则返回 None。"""
        name = (name or "").strip().lstrip("/")
        if not name:
            return None
        if not name.endswith(MEMORY_FILE_SUFFIX):
            return None
        if not _SAFE_NAME_RE.match(name):
            return None
        path = (self.root / name).resolve()
        try:
            path.relative_to(self.root.resolve())
        except ValueError:
            return None
        return path

    @staticmethod
    def is_valid_name(name: str) -> bool:
        """文件名本身是否合规（不涉及磁盘）。"""
        return (
            bool(name)
            and name.endswith(MEMORY_FILE_SUFFIX)
            and bool(_SAFE_NAME_RE.match(name))
        )

    @staticmethod
    def path_rules() -> str:
        return (
            "文件名只能包含字母、数字、点、下划线、连字符，"
            f"必须以 {MEMORY_FILE_SUFFIX} 结尾，不允许目录分隔符"
        )

    # ------------------------------------------------------------ 读

    async def read(self, name: str) -> str | None:
        path = self._resolve(name)
        if path is None or not path.is_file():
            return None
        return await asyncio.to_thread(path.read_text, "utf-8")

    def list_files(self) -> list[str]:
        """作用域内的记忆文件（不含自动维护的 MEMORY.md 索引）。"""
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir()
            if p.is_file()
            and p.suffix == MEMORY_FILE_SUFFIX
            and p.name != INDEX_FILENAME
        )

    def exists(self, name: str) -> bool:
        path = self._resolve(name)
        return path is not None and path.is_file()

    def mtime(self, name: str) -> float | None:
        """文件最近写入时间；不存在或名称非法返回 None。"""
        path = self._resolve(name)
        try:
            return path.stat().st_mtime if path is not None else None
        except OSError:
            return None

    def headers(self) -> list[MemoryHeader]:
        """全部记忆文件的头信息（同步方法，调用方放进线程执行）。

        按 (mtime_ns, size) 缓存：每轮召回都要扫一遍，没改过的文件不重读。
        """
        with self._headers_lock:
            return self._scan_headers()

    def _scan_headers(self) -> list[MemoryHeader]:
        if not self.root.is_dir():
            self._headers.clear()
            return []
        result: list[MemoryHeader] = []
        alive: set[str] = set()
        with os.scandir(self.root) as entries:
            for entry in entries:
                name = entry.name
                if (
                    not name.endswith(MEMORY_FILE_SUFFIX)
                    or name == INDEX_FILENAME
                    or not entry.is_file()
                ):
                    continue
                try:
                    st = entry.stat()
                except OSError:
                    continue
                alive.add(name)
                stamp = (st.st_mtime_ns, st.st_size)
                cached = self._headers.get(name)
                if cached is not None and cached[0] == stamp:
                    result.append(cached[1])
                    continue
                try:
                    with open(entry.path, encoding="utf-8", errors="replace") as fh:
                        content = fh.read(MAX_FILE_BYTES)
                except OSError:
                    continue
                doc = parse_memory(content)
                description = doc.description or parse_description(content)[0]
                header = MemoryHeader(
                    file=name,
                    name=doc.name or name[: -len(MEMORY_FILE_SUFFIX)],
                    description=description,
                    type=doc.type,
                    mtime=st.st_mtime,
                    size=st.st_size,
                    text=content[:HEADER_TEXT_CHARS].lower(),
                    links=extract_links(doc.body),
                    updated=parse_stamp(doc.updated) or st.st_mtime,
                    created=parse_stamp(doc.created) or 0.0,
                    source=doc.source,
                )
                self._headers[name] = (stamp, header)
                result.append(header)
        for gone in set(self._headers) - alive:
            del self._headers[gone]
        result.sort(key=lambda h: h.file)
        return result

    # ------------------------------------------------------------ 写 / 删

    async def write(self, name: str, content: str, source: str = "") -> WriteReport:
        """写入记忆文件并自动同步索引行（钩子取自 frontmatter description）。

        source 是本次写入的来源 ID（通常是当前发送者），并入文件的 source 戳。
        """
        # 与 _resolve 同样的归一化：索引守门与索引行都要用整理后的名字，
        # 否则 " MEMORY.md" / "/MEMORY.md" 能绕过守门，索引行也会写错
        name = (name or "").strip().lstrip("/")
        path = self._resolve(name)
        if path is None:
            return WriteReport(False, f"文件名不合法：{self.path_rules()}")
        if name.lower() == INDEX_FILENAME.lower():
            return WriteReport(
                False,
                "MEMORY.md 索引由插件自动维护，不可直接写入。"
                "要修改某条索引行，改对应记忆文件的 frontmatter description；"
                "要删除索引行，删除对应文件（delete=true）即可，索引会自动同步。",
            )
        async with self._lock:
            previous = await asyncio.to_thread(self._read_if_exists, path)
            return await self._commit(
                name, path, content, f"已写入 {name}", source, previous
            )

    @staticmethod
    def _read_if_exists(path: Path) -> str | None:
        try:
            return path.read_text("utf-8") if path.is_file() else None
        except OSError:
            return None

    async def edit(
        self,
        name: str,
        old: str,
        new: str,
        replace_all: bool = False,
        source: str = "",
    ) -> WriteReport:
        """局部替换：old 在文件里须恰好出现一次（replace_all 时可多次），其余原样保留。

        读、换、写在同一把锁里完成，不会与并发的整文件写入交错；替换后的
        全文照样过体积守门并同步索引行，改了 description 索引钩子随之更新。
        """
        name = (name or "").strip().lstrip("/")
        path = self._resolve(name)
        if path is None:
            return WriteReport(False, f"文件名不合法：{self.path_rules()}")
        if name.lower() == INDEX_FILENAME.lower():
            return WriteReport(
                False,
                "MEMORY.md 索引由插件自动维护，不可直接修改。"
                "要改某条索引行，改对应记忆文件 frontmatter 里的 description 即可。",
            )
        # 文件统一以 \n 落盘，模型传来的片段带 \r\n 也能对上
        old = (old or "").replace("\r\n", "\n")
        new = (new or "").replace("\r\n", "\n")
        if not old:
            return WriteReport(False, "old_text 不能为空：请给出文件里要被替换的原文片段。")
        if old == new:
            return WriteReport(False, "old_text 与 new_text 相同，没有可做的修改。")
        async with self._lock:
            if not path.is_file():
                return WriteReport(
                    False, f"文件不存在：{name}（新建文件请用 memory_write）"
                )
            content = await asyncio.to_thread(path.read_text, "utf-8")
            count = content.count(old)
            if count == 0:
                return WriteReport(
                    False,
                    f"在 {name} 里没有找到 old_text。请先用 memory_read 读出当前内容，"
                    "原样复制要改的片段（空格、换行、标点都要一致）。",
                )
            if count > 1 and not replace_all:
                return WriteReport(
                    False,
                    f"old_text 在 {name} 里出现了 {count} 处，无法确定改哪一处："
                    "把片段写长一些让它唯一，或设 replace_all=true 全部替换。",
                )
            replaced = count if replace_all else 1
            updated = content.replace(old, new, replaced)
            if not updated.strip():
                return WriteReport(
                    False,
                    "改完整个文件就空了，不会写入；要删除文件请用 memory_write 并设 delete=true。",
                )
            return await self._commit(
                name, path, updated, f"已修改 {name}，替换 {replaced} 处", source, content
            )

    async def _commit(
        self,
        name: str,
        path: Path,
        content: str,
        done: str,
        source: str = "",
        previous: str | None = None,
    ) -> WriteReport:
        """持锁落盘：补戳 → 体积守门 → 原子写 → 失效头缓存 → 同步索引行。"""
        content = stamp_memory(content, time.time(), source, previous)
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return WriteReport(
                False,
                f"内容超过单文件上限 {MAX_FILE_BYTES // 1024}KB，请精简后重写",
            )
        description, from_frontmatter = parse_description(content)
        await asyncio.to_thread(self._write_atomic, path, content)
        self._drop_header(name)
        lines, size = await asyncio.to_thread(
            self._sync_index_line, name, description
        )
        message = f"{done}，索引行已同步"
        if not from_frontmatter:
            message += (
                "（未找到 frontmatter 的 description，索引钩子暂取正文首行；"
                "建议按标准格式补全 frontmatter）"
            )
        health = self._index_health(lines, size)
        if health:
            message += "。" + health
        return WriteReport(True, message)

    async def delete(self, name: str) -> WriteReport:
        name = (name or "").strip().lstrip("/")
        path = self._resolve(name)
        if path is None:
            return WriteReport(False, f"文件名不合法：{self.path_rules()}")
        if name.lower() == INDEX_FILENAME.lower():
            return WriteReport(
                False,
                "MEMORY.md 索引由插件自动维护，不可删除；删除记忆文件时会自动移除对应索引行。",
            )
        async with self._lock:
            if not path.is_file():
                return WriteReport(False, f"文件不存在：{name}")
            await asyncio.to_thread(path.unlink)
            self._drop_header(name)
            await asyncio.to_thread(self._remove_index_line, name)
        return WriteReport(True, f"已删除 {name}，对应索引行已同步移除")

    async def purge(self) -> int:
        """删除整个作用域目录（全部记忆文件与索引），返回删除的记忆文件数。"""
        async with self._lock:
            count = len(self.list_files())
            if self.root.is_dir():
                await asyncio.to_thread(shutil.rmtree, self.root)
            with self._headers_lock:
                self._headers.clear()
        return count

    def _drop_header(self, name: str) -> None:
        with self._headers_lock:
            self._headers.pop(name, None)

    def _write_atomic(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------ 索引维护

    def _sync_index_line(self, filename: str, description: str) -> tuple[int, int]:
        """增改 filename 对应的索引行（原位更新，保持行序稳定）。

        只动匹配行与末尾追加，其余行原样保留——用户手工整理过的索引
        （分组标题、排序）不会被破坏。返回 (总行数, 总字节数) 供守门。
        """
        index_path = self.root / INDEX_FILENAME
        existing = ""
        if index_path.is_file():
            existing = index_path.read_text("utf-8")
        lines = existing.splitlines()
        marker = f"]({filename})"
        new_line = format_index_line(filename, description)
        for i, line in enumerate(lines):
            if marker in line:
                lines[i] = new_line
                break
        else:
            lines.append(new_line)
        text = "\n".join(lines).strip("\n") + "\n"
        self._write_atomic(index_path, text)
        return len(text.splitlines()), len(text.encode("utf-8"))

    def _remove_index_line(self, filename: str) -> None:
        index_path = self.root / INDEX_FILENAME
        if not index_path.is_file():
            return
        marker = f"]({filename})"
        lines = [
            line
            for line in index_path.read_text("utf-8").splitlines()
            if marker not in line
        ]
        text = "\n".join(lines).strip("\n")
        self._write_atomic(index_path, text + "\n" if text else "")

    @staticmethod
    def _index_health(lines: int, size: int) -> str | None:
        """索引体积守门文案；健康时返回 None。上限是注入截断线，不是写入限制。"""
        if lines > INDEX_MAX_LINES or size > INDEX_MAX_BYTES:
            return (
                f"注意：MEMORY.md 已 {lines} 行 / {size} 字节，超出注入上限 "
                f"{INDEX_MAX_LINES} 行 / {INDEX_MAX_BYTES} 字节，超出部分不会注入。"
                "请把相关记忆合并进同一个文件、删除多余文件，索引会随之精简"
            )
        if (
            lines > INDEX_MAX_LINES * INDEX_WARN_RATIO
            or size > INDEX_MAX_BYTES * INDEX_WARN_RATIO
        ):
            return (
                f"MEMORY.md 已 {lines} 行 / {size} 字节，接近注入上限 "
                f"{INDEX_MAX_LINES} 行 / {INDEX_MAX_BYTES} 字节，"
                "建议把相关记忆合并进同一个文件、删除多余文件"
            )
        return None

    def index_report(self) -> IndexReport:
        """索引体检（同步方法）：体积、截断、缺行与悬空行。"""
        index_path = self.root / INDEX_FILENAME
        files = set(self.list_files())
        if not index_path.is_file():
            return IndexReport(False, 0, 0, False, sorted(files), [])
        raw = index_path.read_text("utf-8")
        listed: list[str] = []
        for line in raw.splitlines():
            match = _INDEX_TARGET_RE.search(line)
            if match and match.group(1) not in listed:
                listed.append(match.group(1))
        snap = _truncate_index(raw)
        return IndexReport(
            exists=True,
            lines=snap.total_lines,
            size=len(raw.encode("utf-8")),
            truncated=snap.truncated,
            missing=sorted(files - set(listed)),
            dangling=[name for name in listed if name not in files],
        )

    async def write_index(self, content: str) -> WriteReport:
        """管理员手工改写 MEMORY.md（调整分组、顺序、措辞）。

        模型不能直接写索引；这个入口只给 WebUI 用。
        """
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return WriteReport(
                False, f"内容超过单文件上限 {MAX_FILE_BYTES // 1024}KB"
            )
        text = content.strip("\n")
        async with self._lock:
            await asyncio.to_thread(
                self._write_atomic,
                self.root / INDEX_FILENAME,
                text + "\n" if text else "",
            )
        message = "索引已保存"
        health = self._index_health(
            len(text.splitlines()), len(text.encode("utf-8"))
        )
        if health:
            message += "。" + health
        return WriteReport(True, message)

    async def rebuild_index(self) -> int:
        """按磁盘上的实际文件重建 MEMORY.md，返回索引行数。

        已有行原位刷新钩子、保留手工加的标题与分组，指向不存在文件的行
        删掉，没登记的文件追加到末尾。用于手工改动文件目录之后的修复。
        """
        async with self._lock:
            return await asyncio.to_thread(self._rebuild_index_sync)

    def _rebuild_index_sync(self) -> int:
        headers = {h.file: h for h in self.headers()}
        index_path = self.root / INDEX_FILENAME
        existing = index_path.read_text("utf-8") if index_path.is_file() else ""
        lines: list[str] = []
        listed: set[str] = set()
        for line in existing.splitlines():
            match = _INDEX_TARGET_RE.search(line)
            if not match:
                lines.append(line)
                continue
            target = match.group(1)
            header = headers.get(target)
            if header is None or target in listed:
                continue
            listed.add(target)
            lines.append(format_index_line(target, header.description))
        for name, header in headers.items():
            if name not in listed:
                lines.append(format_index_line(name, header.description))
        text = "\n".join(lines).strip("\n")
        if not text and not headers:
            if index_path.is_file():
                index_path.unlink()
            return 0
        self._write_atomic(index_path, text + "\n")
        return len(headers)

    # ------------------------------------------------------------ 索引加载

    async def load_index(self) -> IndexSnapshot:
        raw = await self.read(INDEX_FILENAME)
        if raw is None:
            return IndexSnapshot(text="", total_lines=0, loaded_lines=0)
        return _truncate_index(raw)

    # ------------------------------------------------------------ 全文搜索

    async def search(self, query: str) -> list[str]:
        """大小写不敏感的多词 AND 全文搜索，返回可读的结果行。

        这是索引截断或索引行写得不好时的兜底通道：不依赖"模型知道这条
        记忆存在"，只要正文里有词就能找到。
        """
        terms = [t.lower() for t in query.split() if t.strip()]
        if not terms:
            return []
        results: list[str] = []
        for name in self.list_files():
            if len(results) >= MAX_SEARCH_RESULTS:
                break
            content = await self.read(name)
            if content is None:
                continue
            lowered = content.lower()
            if not all(t in lowered for t in terms):
                continue
            excerpt = self._first_hit_line(content, terms)
            results.append(f"{name}: {excerpt}")
        return results

    @staticmethod
    def _first_hit_line(content: str, terms: list[str]) -> str:
        for line in content.splitlines():
            lowered = line.lower()
            if any(t in lowered for t in terms):
                line = line.strip()
                if len(line) > MAX_SEARCH_EXCERPT:
                    line = line[:MAX_SEARCH_EXCERPT] + "…"
                return line
        return "（正文匹配）"


# 人物记忆一人一个文件：user-<数字ID>.md
_PERSON_FILE_RE = re.compile(r"^user-\d+\.md$")


def is_person_file(name: str) -> bool:
    return bool(_PERSON_FILE_RE.match((name or "").strip()))


class MemoryStore:
    """三层作用域的记忆库根：global（机器人自我）、session（每个会话）、
    people（每个平台的人物记忆）。作用域目录按需创建、ScopeStore 按键缓存。"""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self._scopes: dict[Path, ScopeStore] = {}

    def _scope(self, path: Path) -> ScopeStore:
        path = path.resolve()
        store = self._scopes.get(path)
        if store is None:
            store = ScopeStore(path)
            self._scopes[path] = store
        return store

    def global_scope(self) -> ScopeStore:
        return self._scope(self.base_dir / "global")

    def session_scope(self, session_key: str) -> ScopeStore:
        return self._scope(self.base_dir / "session" / safe_key(session_key))

    def session_scope_by_key(self, key: str) -> ScopeStore | None:
        """按目录名（safe_key 的输出）取会话作用域；目录名不合法返回 None。"""
        if not is_scope_key(key):
            return None
        return self._scope(self.base_dir / "session" / key)

    def session_keys(self) -> list[str]:
        """磁盘上已有记忆目录的会话（目录名）。"""
        return self._subdirs("session")

    def people_scope(self, platform: str) -> ScopeStore:
        """某个平台的人物记忆：一人一个文件，该平台的所有群聊和私聊共享。
        按平台分目录是因为不同平台的数字 ID 可能撞号。"""
        return self._scope(self.base_dir / "people" / safe_key(platform or "unknown"))

    def people_scope_by_key(self, key: str) -> ScopeStore | None:
        """按目录名取人物作用域；目录名不合法返回 None。"""
        if not is_scope_key(key):
            return None
        return self._scope(self.base_dir / "people" / key)

    def people_keys(self) -> list[str]:
        """磁盘上已有人物记忆目录的平台（目录名）。"""
        return self._subdirs("people")

    def _subdirs(self, layer: str) -> list[str]:
        root = self.base_dir / layer
        if not root.is_dir():
            return []
        return sorted(
            p.name for p in root.iterdir() if p.is_dir() and is_scope_key(p.name)
        )


def is_scope_key(key: str) -> bool:
    return bool(key) and bool(_SCOPE_KEY_RE.match(key))
