"""文件式记忆库。

一个文件一条事实（Markdown + frontmatter），每个作用域一个 MEMORY.md
索引，索引一条记忆占一行、只放指针永不放正文。

聊天场景必需的双层作用域：
    global/<用户键>/   —— 跟人走，存 user 类，该用户出现在任何会话都注入
    session/<会话键>/  —— 跟群走，存 project / feedback 类，只在本会话注入

索引自动注入通道有 200 行 / 25KB 上限，超出从底部截断；截断时在末尾追加
一行说明还有多少行未加载、可用搜索工具找到——否则模型根本不知道自己有
记忆没看到。

写入侧守门：写完索引后检查体积，接近上限提醒精简合并，超限则写入成功但
返回错误要求重写。工具读文件读到的始终是完整文件（截断只发生在注入通道）。
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

_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")


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
            "未显示的记忆可用 memory_search 工具检索，"
            "并请尽快用 memory_write 精简合并 MEMORY.md）"
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
        if not self.root.is_dir():
            return []
        return sorted(
            p.name for p in self.root.iterdir()
            if p.is_file() and p.suffix == MEMORY_FILE_SUFFIX
        )

    # ------------------------------------------------------------ 写 / 删

    async def write(self, name: str, content: str) -> WriteReport:
        path = self._resolve(name)
        if path is None:
            return WriteReport(False, f"文件名不合法：{self.path_rules()}")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            return WriteReport(
                False,
                f"内容超过单文件上限 {MAX_FILE_BYTES // 1024}KB，请精简后重写",
            )
        async with self._lock:
            await asyncio.to_thread(self._write_atomic, path, content)
        if name == INDEX_FILENAME:
            return self._gate_index(content)
        return WriteReport(True, f"已写入 {name}")

    async def delete(self, name: str) -> WriteReport:
        path = self._resolve(name)
        if path is None:
            return WriteReport(False, f"文件名不合法：{self.path_rules()}")
        if name == INDEX_FILENAME:
            return WriteReport(False, "索引文件不可删除；如需清空请写入空内容")
        async with self._lock:
            if not path.is_file():
                return WriteReport(False, f"文件不存在：{name}")
            await asyncio.to_thread(path.unlink)
        return WriteReport(True, f"已删除 {name}，请记得同步移除 MEMORY.md 中对应的索引行")

    def _write_atomic(self, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------ 索引守门

    def _gate_index(self, content: str) -> WriteReport:
        lines = len(content.splitlines())
        size = len(content.encode("utf-8"))
        if lines > INDEX_MAX_LINES or size > INDEX_MAX_BYTES:
            return WriteReport(
                False,
                f"MEMORY.md 已写入，但体积超限（{lines} 行 / {size} 字节，"
                f"上限 {INDEX_MAX_LINES} 行 / {INDEX_MAX_BYTES} 字节），"
                "超出部分不会被自动注入。请立即合并同类记忆、精简钩子文案，"
                "重写 MEMORY.md 到上限以内（重写前先用 memory_read 读取完整索引）",
            )
        if (
            lines > INDEX_MAX_LINES * INDEX_WARN_RATIO
            or size > INDEX_MAX_BYTES * INDEX_WARN_RATIO
        ):
            return WriteReport(
                True,
                f"MEMORY.md 已写入（{lines} 行 / {size} 字节），已接近上限 "
                f"{INDEX_MAX_LINES} 行 / {INDEX_MAX_BYTES} 字节，"
                "建议尽快合并同类记忆、精简索引行",
            )
        return WriteReport(True, f"MEMORY.md 已写入（{lines} 行 / {size} 字节）")

    # ------------------------------------------------------------ 索引加载

    async def load_index(self) -> IndexSnapshot:
        raw = await self.read(INDEX_FILENAME)
        if raw is None:
            return IndexSnapshot(text="", total_lines=0, loaded_lines=0)
        return _truncate_index(raw)

    async def append_index_line(self, line: str) -> WriteReport:
        """插件侧追加一行索引（压缩提取记忆时用；模型维护索引走 write）。"""
        async with self._lock:
            path = self.root / INDEX_FILENAME
            existing = ""
            if path.is_file():
                existing = await asyncio.to_thread(path.read_text, "utf-8")
            if existing and not existing.endswith("\n"):
                existing += "\n"
            await asyncio.to_thread(self._write_atomic, path, existing + line + "\n")
        return WriteReport(True, "索引已追加")

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

    def global_scope(self, user_key: str) -> ScopeStore:
        return self._scope(self.base_dir / "global" / safe_key(user_key))

    def session_scope(self, session_key: str) -> ScopeStore:
        return self._scope(self.base_dir / "session" / safe_key(session_key))
