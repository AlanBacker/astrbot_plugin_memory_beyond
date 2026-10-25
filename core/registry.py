"""会话登记簿：记下每个用过记忆的会话是谁，供 WebUI 列表展示。

记忆目录名是会话标识（UMO）经 safe_key 转换后的结果，带哈希、不可逆，
光看目录认不出是哪个群、哪个人。插件在每轮请求时把「目录名 → UMO、
平台、会话类型、群名或昵称」登记下来；WebUI 另可给会话加备注。
没有登记记录的旧目录（升级前就有的）按目录名的固定格式尽量还原。

写盘做了节流：只在新会话、名称变化或距上次落盘超过 FLUSH_INTERVAL
时写，聊得再频繁也不会每条消息写一次盘。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

FLUSH_INTERVAL = 300.0
MAX_TEXT = 64

KIND_GROUP = "group"
KIND_PRIVATE = "private"
KIND_OTHER = "other"

_MESSAGE_TYPES = {
    "GroupMessage": KIND_GROUP,
    "FriendMessage": KIND_PRIVATE,
    "OtherMessage": KIND_OTHER,
}
# safe_key 把 "平台:消息类型:会话ID" 的冒号换成下划线并追加 -<8位哈希>
_LEGACY_KEY_RE = re.compile(
    r"^(?P<platform>.+?)_(?P<type>GroupMessage|FriendMessage|OtherMessage)_"
    r"(?P<target>.+?)(?:-[0-9a-f]{8})?$"
)


def parse_umo(umo: str) -> tuple[str, str, str]:
    """把 UMO 拆成 (平台, 会话类型, 会话 ID)；格式不符时类型为 other。"""
    parts = str(umo or "").split(":", 2)
    if len(parts) == 3:
        platform, msg_type, target = parts
        return platform, _MESSAGE_TYPES.get(msg_type, KIND_OTHER), target
    return "", KIND_OTHER, str(umo or "")


def guess_from_key(key: str) -> tuple[str, str, str]:
    """从目录名尽量还原 (平台, 会话类型, 会话 ID)，用于没有登记记录的旧目录。"""
    match = _LEGACY_KEY_RE.match(key or "")
    if not match:
        return "", KIND_OTHER, key
    return (
        match.group("platform"),
        _MESSAGE_TYPES[match.group("type")],
        match.group("target"),
    )


def umo_from_key(key: str) -> str:
    """从目录名拼回 UMO 的候选；不符合固定格式返回空串。

    目录名转换时被替换掉的字符还原不回来，调用方要再用 safe_key 核对一遍。
    """
    match = _LEGACY_KEY_RE.match(key or "")
    if not match:
        return ""
    return ":".join(match.group("platform", "type", "target"))


def _clip(value) -> str:
    return " ".join(str(value or "").split())[:MAX_TEXT]


@dataclass
class SessionRecord:
    key: str
    umo: str = ""
    platform: str = ""
    kind: str = KIND_OTHER
    target: str = ""
    # 平台提供的群名（群聊）或对方昵称（私聊），随请求自动更新
    name: str = ""
    # 管理员在 WebUI 里填写的备注，优先于 name 显示
    remark: str = ""
    first_seen: float = 0.0
    last_active: float = 0.0

    @classmethod
    def from_dict(cls, key: str, raw: dict) -> "SessionRecord":
        record = cls(key=key)
        for field_name in ("umo", "platform", "kind", "target", "name", "remark"):
            value = raw.get(field_name)
            if isinstance(value, str):
                setattr(record, field_name, value)
        for field_name in ("first_seen", "last_active"):
            try:
                setattr(record, field_name, float(raw.get(field_name) or 0.0))
            except (TypeError, ValueError):
                pass
        if record.kind not in (KIND_GROUP, KIND_PRIVATE, KIND_OTHER):
            record.kind = KIND_OTHER
        return record

    def to_dict(self) -> dict:
        data = asdict(self)
        data.pop("key")
        return data


class SessionRegistry:
    """sessions.json 的读写。

    touch() 只改内存（首次调用读一次盘），可以直接在事件循环里调用；
    flush() 写盘，调用方放进线程执行。所有入口经同一把锁串行。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._records: dict[str, SessionRecord] | None = None
        self._dirty = False
        self._flushed_at = 0.0
        self._lock = threading.RLock()

    def _load(self) -> dict[str, SessionRecord]:
        if self._records is not None:
            return self._records
        records: dict[str, SessionRecord] = {}
        try:
            payload = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            payload = {}
        sessions = payload.get("sessions") if isinstance(payload, dict) else None
        if isinstance(sessions, dict):
            for key, raw in sessions.items():
                if isinstance(raw, dict):
                    records[str(key)] = SessionRecord.from_dict(str(key), raw)
        self._records = records
        return records

    # ------------------------------------------------------------ 读

    def get(self, key: str) -> SessionRecord | None:
        with self._lock:
            return self._load().get(key)

    def all(self) -> dict[str, SessionRecord]:
        with self._lock:
            return dict(self._load())

    # ------------------------------------------------------------ 写

    def touch(
        self, key: str, umo: str, name: str = "", now: float | None = None
    ) -> bool:
        """记录一次活跃；返回是否该落盘了（新会话、改名或超过节流间隔）。"""
        now = time.time() if now is None else now
        with self._lock:
            records = self._load()
            record = records.get(key)
            urgent = False
            if record is None:
                platform, kind, target = parse_umo(umo)
                record = SessionRecord(
                    key=key,
                    umo=umo,
                    platform=platform,
                    kind=kind,
                    target=target,
                    first_seen=now,
                )
                records[key] = record
                urgent = True
            elif record.umo != umo:
                record.umo = umo
                record.platform, record.kind, record.target = parse_umo(umo)
                urgent = True
            name = _clip(name)
            if name and name != record.name:
                record.name = name
                urgent = True
            record.last_active = now
            self._dirty = True
            return urgent or now - self._flushed_at >= FLUSH_INTERVAL

    def set_remark(self, key: str, remark: str) -> SessionRecord:
        with self._lock:
            records = self._load()
            record = records.get(key)
            if record is None:
                platform, kind, target = guess_from_key(key)
                record = SessionRecord(
                    key=key, platform=platform, kind=kind, target=target
                )
                records[key] = record
            record.remark = _clip(remark)
            self._dirty = True
            self.flush()
            return record

    def adopt(
        self,
        key: str,
        umo: str = "",
        *,
        name: str = "",
        remark: str = "",
        first_seen: float = 0.0,
        last_active: float = 0.0,
    ) -> SessionRecord:
        """登记迁入的会话（导入备份、会话之间搬记忆时用），不算一次活跃。

        已有记录只补空缺：来源、群名、备注原本为空才填，首次出现取更早的，
        最近活跃取更晚的。立即落盘。
        """
        with self._lock:
            records = self._load()
            record = records.get(key)
            if record is None:
                if umo:
                    platform, kind, target = parse_umo(umo)
                else:
                    platform, kind, target = guess_from_key(key)
                record = SessionRecord(
                    key=key, umo=umo, platform=platform, kind=kind, target=target
                )
                records[key] = record
            elif umo and not record.umo:
                record.umo = umo
                record.platform, record.kind, record.target = parse_umo(umo)
            if name and not record.name:
                record.name = _clip(name)
            if remark and not record.remark:
                record.remark = _clip(remark)
            if first_seen > 0 and (
                not record.first_seen or first_seen < record.first_seen
            ):
                record.first_seen = first_seen
            if last_active > record.last_active:
                record.last_active = last_active
            self._dirty = True
            self.flush()
            return record

    def remove(self, key: str) -> None:
        with self._lock:
            if self._load().pop(key, None) is not None:
                self._dirty = True
                self.flush()

    def flush(self) -> None:
        with self._lock:
            if not self._dirty or self._records is None:
                return
            payload = {
                "version": 1,
                "sessions": {
                    key: rec.to_dict() for key, rec in self._records.items()
                },
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=".sessions-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp_name, self.path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            self._dirty = False
            self._flushed_at = time.time()
