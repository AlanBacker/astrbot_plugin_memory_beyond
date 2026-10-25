"""压缩归档：每次滚动摘要前，把即将卸下的原文另存一份到磁盘。

AstrBot 每轮把发送视图回存为会话历史，摘要覆盖的原文随之从平台历史
永久消失；归档让管理员仍能在 WebUI 里查看当时到底聊了什么、摘要是否
漏掉了重要内容。归档只存渲染后的纯文本（角色 + 文本 + 工具调用名），
不存图片等二进制内容；每个会话只保留最近 N 份，旧的自动删除。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .compress import content_to_text
from .turns import is_internal_message

ARCHIVE_SUFFIX = ".json"
MESSAGE_CHARS = 20000
_ARCHIVE_ID_RE = re.compile(r"^\d{8}-\d{6}-\d{3}$")


def _simplify(message: dict) -> dict | None:
    if not isinstance(message, dict):
        return None
    role = str(message.get("role", ""))
    if role == "system" or is_internal_message(message):
        return None
    text = content_to_text(message.get("content")).strip()
    if len(text) > MESSAGE_CHARS:
        text = text[:MESSAGE_CHARS] + "…（归档截断）"
    item = {"role": role, "text": text}
    calls = []
    for call in message.get("tool_calls") or []:
        if isinstance(call, dict):
            fn = call.get("function") or {}
            calls.append(str(fn.get("name") or "?"))
    if calls:
        item["tools"] = calls
    return item


def make_archive_id(now: float) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    return f"{stamp}-{int((now % 1) * 1000):03d}"


class ArchiveStore:
    """archive/<会话目录名>/<归档ID>.json；同步方法，调用方放进线程执行。"""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)

    def _dir(self, key: str) -> Path:
        return self.base_dir / key

    def save(
        self,
        key: str,
        *,
        umo: str,
        cid: str,
        messages: list[dict],
        summary: str,
        keep: int,
        now: float | None = None,
    ) -> str | None:
        """写一份归档并按 keep 清理旧档；keep <= 0 表示不归档。"""
        if keep <= 0:
            return None
        now = time.time() if now is None else now
        items = [item for item in map(_simplify, messages) if item]
        if not items:
            return None
        archive_id = make_archive_id(now)
        folder = self._dir(key)
        folder.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": archive_id,
            "created_at": now,
            "umo": umo,
            "cid": cid,
            "count": len(items),
            "summary": summary,
            "messages": items,
        }
        fd, tmp_name = tempfile.mkstemp(prefix=".archive-", dir=folder)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False)
            os.replace(tmp_name, folder / f"{archive_id}{ARCHIVE_SUFFIX}")
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self._prune(folder, keep)
        return archive_id

    def _prune(self, folder: Path, keep: int) -> None:
        files = sorted(folder.glob(f"*{ARCHIVE_SUFFIX}"))
        for stale in files[: max(0, len(files) - keep)]:
            try:
                stale.unlink()
            except OSError:
                pass

    def list(self, key: str) -> list[dict]:
        """归档清单（新的在前），每项只含元信息。"""
        folder = self._dir(key)
        if not folder.is_dir():
            return []
        result = []
        for path in sorted(folder.glob(f"*{ARCHIVE_SUFFIX}"), reverse=True):
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            result.append(
                {
                    "id": path.stem,
                    "createdAt": data.get("created_at", 0),
                    "count": data.get("count", 0),
                    "summaryChars": len(str(data.get("summary", ""))),
                }
            )
        return result

    def count(self, key: str) -> int:
        folder = self._dir(key)
        if not folder.is_dir():
            return 0
        return sum(1 for _ in folder.glob(f"*{ARCHIVE_SUFFIX}"))

    def load(self, key: str, archive_id: str) -> dict | None:
        if not _ARCHIVE_ID_RE.match(archive_id or ""):
            return None
        path = self._dir(key) / f"{archive_id}{ARCHIVE_SUFFIX}"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def purge(self, key: str) -> None:
        folder = self._dir(key)
        if folder.is_dir():
            shutil.rmtree(folder)

    def transfer(self, src_key: str, dst_key: str, move: bool = False) -> int:
        """把一个会话的归档并进另一个会话（平台改名迁移用），返回并入的份数。

        目标已有的同 ID 归档不覆盖；move 时并完删掉源目录。
        """
        src = self._dir(src_key)
        if src_key == dst_key or not src.is_dir():
            return 0
        dst = self._dir(dst_key)
        dst.mkdir(parents=True, exist_ok=True)
        count = 0
        for path in sorted(src.glob(f"*{ARCHIVE_SUFFIX}")):
            target = dst / path.name
            if target.exists():
                continue
            if move:
                path.replace(target)
            else:
                shutil.copy2(path, target)
            count += 1
        if move:
            shutil.rmtree(src, ignore_errors=True)
        return count
