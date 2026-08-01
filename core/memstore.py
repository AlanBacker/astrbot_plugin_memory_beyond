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
import re
from dataclasses import dataclass
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

_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
_HOOK_WS_RE = re.compile(r"\s+")


def parse_description(content: str) -> tuple[str, bool]:
    """从记忆文件内容提取索引钩子：(钩子文本, 是否来自 frontmatter)。

    优先取 frontmatter 的 description；文件没写 frontmatter 时退回正文
    首个非空行——保证索引行永远有钩子，但回执会提醒补全 frontmatter。
    """
    match = _FRONTMATTER_RE.match(content)
    if match:
        for line in match.group(1).splitlines():
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

    # ------------------------------------------------------------ 写 / 删

    async def write(self, name: str, content: str) -> WriteReport:
        """写入记忆文件并自动同步索引行（钩子取自 frontmatter description）。"""
        path = self._resolve(name)
        if path is None:
            return WriteReport(False, f"文件名不合法：{self.path_rules()}")
        if name == INDEX_FILENAME:
            return WriteReport(
                False,
                "MEMORY.md 索引由插件自动维护，不可直接写入。"
                "要修改某条索引行，重写对应记忆文件的 frontmatter description；"
                "要删除索引行，删除对应文件（delete=true）即可，索引会自动同步。",
            )
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return WriteReport(
                False,
                f"内容超过单文件上限 {MAX_FILE_BYTES // 1024}KB，请精简后重写",
            )
        description, from_frontmatter = parse_description(content)
        async with self._lock:
            await asyncio.to_thread(self._write_atomic, path, content)
            lines, size = await asyncio.to_thread(
                self._sync_index_line, name, description
            )
        message = f"已写入 {name}，索引行已同步"
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
        path = self._resolve(name)
        if path is None:
            return WriteReport(False, f"文件名不合法：{self.path_rules()}")
        if name == INDEX_FILENAME:
            return WriteReport(
                False,
                "MEMORY.md 索引由插件自动维护，不可删除；删除记忆文件时会自动移除对应索引行。",
            )
        async with self._lock:
            if not path.is_file():
                return WriteReport(False, f"文件不存在：{name}")
            await asyncio.to_thread(path.unlink)
            await asyncio.to_thread(self._remove_index_line, name)
        return WriteReport(True, f"已删除 {name}，对应索引行已同步移除")

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


class MemoryStore:
    """双层作用域的记忆库根。作用域目录按需创建、ScopeStore 按键缓存。"""

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
