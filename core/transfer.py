"""记忆迁移：备份导出、从备份导入、作用域之间搬记忆、平台改名后整体迁移。

搬的都是记忆原文：created / updated / source 戳和使用统计原样带过去（见
ScopeStore.import_files），老记忆搬完不会看起来像刚写的。同名冲突按策略
处理（留较新的 / 留现有的 / 用搬来的覆盖），每一步都能先预演再执行。

压缩状态和平台上的某段对话绑定，换个会话就没有意义，不在迁移范围内；
平台改名时压缩归档随会话一起搬，备份文件里不含归档。

平台改名的场景：AstrBot 里删掉重建了机器人适配器、或改了它的 ID，会话
标识（UMO）的第一段随之变化，记忆目录就对不上了。迁移按旧 UMO 换上新
平台 ID 算出新目录，把记忆、登记信息、归档一并搬过去。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .archive import ArchiveStore
from .memstore import (
    CONFLICT_NEWER,
    CONFLICT_POLICIES,
    IMPORT_ADDED,
    IMPORT_INVALID,
    IMPORT_KEPT,
    IMPORT_REPLACED,
    IMPORT_SAME,
    ImportEntry,
    ImportResult,
    MemoryStore,
    ScopeStore,
    Usage,
    is_person_file,
    is_scope_key,
    safe_key,
)
from .registry import (
    SessionRecord,
    SessionRegistry,
    guess_from_key,
    parse_umo,
    umo_from_key,
)

BUNDLE_FORMAT = "memory-beyond-backup"
BUNDLE_VERSION = 1

SCOPE_GLOBAL = "global"
SCOPE_SESSION = "session"
SCOPE_PEOPLE = "people"
SCOPES = (SCOPE_GLOBAL, SCOPE_SESSION, SCOPE_PEOPLE)

MODE_COPY = "copy"
MODE_MOVE = "move"

PEOPLE_ONLY = "人物记忆只收 user-<数字ID>.md"
MAX_PLATFORM_CHARS = 64
OUTCOMES = (IMPORT_ADDED, IMPORT_REPLACED, IMPORT_SAME, IMPORT_KEPT, IMPORT_INVALID)


class TransferError(Exception):
    """可以直接展示给管理员的错误。"""


@dataclass(frozen=True)
class ScopeRef:
    scope: str
    key: str = ""

    @property
    def id(self) -> str:
        return self.scope if self.scope == SCOPE_GLOBAL else f"{self.scope}:{self.key}"


@dataclass
class BundleScope:
    """备份文件里的一个作用域。"""

    ref: ScopeRef
    entries: list[ImportEntry]
    index: str = ""
    session: dict = field(default_factory=dict)


def make_ref(scope: object, key: object = "") -> ScopeRef:
    """校验作用域与目录名；不合法抛 TransferError。"""
    scope = str(scope or "").strip().lower()
    key = str(key or "").strip()
    if scope == SCOPE_GLOBAL:
        return ScopeRef(SCOPE_GLOBAL, "")
    if scope not in SCOPES:
        raise TransferError("作用域只能是 global、session 或 people")
    if not is_scope_key(key):
        raise TransferError("会话标识不合法" if scope == SCOPE_SESSION else "平台标识不合法")
    return ScopeRef(scope, key)


def check_policy(policy: object) -> str:
    policy = str(policy or CONFLICT_NEWER).strip().lower()
    if policy not in CONFLICT_POLICIES:
        raise TransferError("冲突处理只能是 newer、skip 或 overwrite")
    return policy


def check_platform(raw: object) -> str:
    platform = str(raw or "").strip()
    if not platform or ":" in platform or len(platform) > MAX_PLATFORM_CHARS:
        raise TransferError(f"平台 ID 不能为空、不能含冒号，最长 {MAX_PLATFORM_CHARS} 个字符")
    return platform


def count_outcomes(results: list[ImportResult]) -> dict[str, int]:
    counts = {name: 0 for name in OUTCOMES}
    for item in results:
        counts[item.outcome] = counts.get(item.outcome, 0) + 1
    return counts


def _entry_to_json(entry: ImportEntry) -> dict:
    data: dict = {"name": entry.name, "content": entry.content}
    if entry.mtime:
        data["mtime"] = round(entry.mtime, 3)
    if entry.forgotten:
        data["forgotten"] = True
    data.update(
        {
            key: value
            for key, value in (
                ("lastUsed", round(entry.usage.last_used, 3)),
                ("uses", entry.usage.uses),
                ("forgottenAt", round(entry.usage.forgotten_at, 3)),
            )
            if value
        }
    )
    return data


def _entry_from_json(raw: object) -> ImportEntry | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    content = raw.get("content")
    if not isinstance(name, str) or not isinstance(content, str):
        return None
    usage = Usage.from_raw(
        {
            "last_used": raw.get("lastUsed"),
            "uses": raw.get("uses"),
            "forgotten_at": raw.get("forgottenAt"),
        }
    )
    try:
        mtime = float(raw.get("mtime") or 0.0)
    except (TypeError, ValueError):
        mtime = 0.0
    return ImportEntry(
        name=name.strip(),
        content=content,
        forgotten=bool(raw.get("forgotten")),
        usage=usage,
        mtime=mtime if mtime > 0 else 0.0,
    )


def _session_to_json(record: SessionRecord | None) -> dict:
    if record is None:
        return {}
    return {
        key: value
        for key, value in (
            ("umo", record.umo),
            ("name", record.name),
            ("remark", record.remark),
            ("firstSeen", record.first_seen),
            ("lastActive", record.last_active),
        )
        if value
    }


def parse_bundle(data: object) -> list[BundleScope]:
    """读备份文件；格式不对抛 TransferError。不合法的作用域与文件条目跳过。"""
    if not isinstance(data, dict) or data.get("format") != BUNDLE_FORMAT:
        raise TransferError("这不是记忆备份文件")
    try:
        version = int(data.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 1 or version > BUNDLE_VERSION:
        raise TransferError("备份文件的版本不受支持，请升级插件后再导入")
    scopes = data.get("scopes")
    if not isinstance(scopes, list):
        raise TransferError("备份文件里没有作用域列表")
    result: list[BundleScope] = []
    seen: set[str] = set()
    for raw in scopes:
        if not isinstance(raw, dict):
            continue
        try:
            ref = make_ref(raw.get("scope"), raw.get("key"))
        except TransferError:
            continue
        if ref.id in seen:
            continue
        seen.add(ref.id)
        entries = [
            entry
            for entry in map(_entry_from_json, raw.get("files") or [])
            if entry is not None
        ]
        session = raw.get("session")
        index = raw.get("index")
        result.append(
            BundleScope(
                ref=ref,
                entries=entries,
                index=index if isinstance(index, str) else "",
                session=session if isinstance(session, dict) else {},
            )
        )
    return result


def _people_filter(
    ref: ScopeRef, entries: list[ImportEntry]
) -> tuple[list[ImportEntry], list[ImportResult]]:
    """人物作用域一人一个文件：不是 user-<数字ID>.md 的条目直接判为不合法。"""
    if ref.scope != SCOPE_PEOPLE:
        return entries, []
    kept = [e for e in entries if is_person_file(e.name)]
    rejected = [
        ImportResult(e.name, IMPORT_INVALID, PEOPLE_ONLY)
        for e in entries
        if not is_person_file(e.name)
    ]
    return kept, rejected


def _has_memories(store: ScopeStore) -> bool:
    return bool(store.list_files() or store.forgotten_files())


def _float(value: object) -> float:
    try:
        return float(value or 0.0)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


class MemoryTransfer:
    """迁移的执行者：同步的磁盘活放进线程，记忆文件的落盘经各作用域的锁。"""

    def __init__(
        self, store: MemoryStore, registry: SessionRegistry, archive: ArchiveStore
    ):
        self.store = store
        self.registry = registry
        self.archive = archive

    def scope_store(self, ref: ScopeRef) -> ScopeStore:
        if ref.scope == SCOPE_GLOBAL:
            return self.store.global_scope()
        if ref.scope == SCOPE_PEOPLE:
            found = self.store.people_scope_by_key(ref.key)
        else:
            found = self.store.session_scope_by_key(ref.key)
        if found is None:
            raise TransferError("作用域标识不合法")
        return found

    def describe(self, ref: ScopeRef) -> dict:
        """给页面展示用的作用域信息（名称由页面按语言拼）。"""
        data = {"scope": ref.scope, "key": ref.key}
        if ref.scope == SCOPE_SESSION:
            record = self.registry.get(ref.key)
            if record is None:
                platform, kind, target = guess_from_key(ref.key)
                record = SessionRecord(
                    key=ref.key, platform=platform, kind=kind, target=target
                )
            data.update(
                umo=record.umo,
                platform=record.platform,
                kind=record.kind,
                target=record.target,
                name=record.name,
                remark=record.remark,
            )
        elif ref.scope == SCOPE_PEOPLE:
            data["platform"] = ref.key
        return data

    def all_refs(self) -> list[ScopeRef]:
        refs = [ScopeRef(SCOPE_GLOBAL)]
        refs += [ScopeRef(SCOPE_PEOPLE, key) for key in self.store.people_keys()]
        refs += [ScopeRef(SCOPE_SESSION, key) for key in self.store.session_keys()]
        return refs

    # ------------------------------------------------------------ 备份

    async def export(
        self,
        refs: list[ScopeRef] | None = None,
        include_forgotten: bool = True,
        now: float | None = None,
    ) -> dict:
        """打一份备份：每个作用域的记忆原文、使用统计、索引与会话登记信息。"""
        now = time.time() if now is None else now
        targets = refs if refs is not None else self.all_refs()
        stores = [(ref, self.scope_store(ref)) for ref in targets]
        scopes: list[dict] = []
        for ref, store in stores:
            entries, index = await asyncio.to_thread(store.snapshot, include_forgotten)
            if not entries:
                continue
            item: dict = {
                "scope": ref.scope,
                "key": ref.key,
                "files": [_entry_to_json(e) for e in entries],
            }
            if index:
                item["index"] = index
            if ref.scope == SCOPE_SESSION:
                session = _session_to_json(self.registry.get(ref.key))
                if session:
                    item["session"] = session
            scopes.append(item)
        return {
            "format": BUNDLE_FORMAT,
            "version": BUNDLE_VERSION,
            "exportedAt": round(now, 3),
            "scopes": scopes,
        }

    async def import_bundle(
        self,
        scopes: list[BundleScope],
        policy: str = CONFLICT_NEWER,
        dry_run: bool = False,
    ) -> list[dict]:
        """把备份里选中的作用域原样导回；返回每个作用域的去向统计。"""
        reports = []
        for item in scopes:
            store = self.scope_store(item.ref)
            entries, rejected = _people_filter(item.ref, item.entries)
            results = rejected + await store.import_files(
                entries, policy, index=item.index, dry_run=dry_run
            )
            if (
                not dry_run
                and item.ref.scope == SCOPE_SESSION
                and any(r.landed for r in results)
            ):
                info = item.session
                umo = str(info.get("umo") or "")
                await asyncio.to_thread(
                    self.registry.adopt,
                    item.ref.key,
                    umo if safe_key(umo) == item.ref.key else "",
                    name=str(info.get("name") or ""),
                    remark=str(info.get("remark") or ""),
                    first_seen=_float(info.get("firstSeen")),
                    last_active=_float(info.get("lastActive")),
                )
            reports.append(self._report(item.ref, None, results))
        return reports

    def bundle_summary(self, scopes: list[BundleScope]) -> list[dict]:
        """备份里有什么（不碰磁盘上的记忆），供导入前勾选。"""
        items = []
        for item in scopes:
            data = self.describe(item.ref)
            info = item.session
            if item.ref.scope == SCOPE_SESSION:
                umo = str(info.get("umo") or "")
                if umo and not data.get("umo") and safe_key(umo) == item.ref.key:
                    platform, kind, target = parse_umo(umo)
                    data.update(umo=umo, platform=platform, kind=kind, target=target)
                for name in ("name", "remark"):
                    if info.get(name) and not data.get(name):
                        data[name] = str(info[name])
            data["fileCount"] = sum(1 for e in item.entries if not e.forgotten)
            data["forgottenCount"] = sum(1 for e in item.entries if e.forgotten)
            items.append(data)
        return items

    # ------------------------------------------------------------ 作用域之间搬

    async def transfer(
        self,
        src: ScopeRef,
        dst: ScopeRef,
        *,
        dst_umo: str = "",
        files: list[str] | None = None,
        include_forgotten: bool = True,
        mode: str = MODE_COPY,
        policy: str = CONFLICT_NEWER,
        dry_run: bool = False,
    ) -> dict:
        """把 src 的记忆（全部或挑出的文件）复制或移动到 dst。

        移动只删成功落到目标里的文件；因冲突被保留在目标外的仍留在源处。
        整个作用域一起搬、目标原本没有索引时，源的索引（分组与顺序）跟过去。
        """
        if src == dst:
            raise TransferError("来源和目标是同一处")
        if mode not in (MODE_COPY, MODE_MOVE):
            raise TransferError("迁移方式只能是 copy 或 move")
        src_store = self.scope_store(src)
        dst_store = self.scope_store(dst)
        entries, index = await asyncio.to_thread(src_store.snapshot, include_forgotten)
        if files is not None:
            wanted = set(files)
            entries = [e for e in entries if e.name in wanted]
            index = ""
        if not entries:
            raise TransferError("来源没有可迁移的记忆")
        entries, rejected = _people_filter(dst, entries)
        results = rejected + await dst_store.import_files(
            entries, policy, index=index, dry_run=dry_run
        )
        moved = 0
        if not dry_run:
            if dst.scope == SCOPE_SESSION and any(r.landed for r in results):
                umo = dst_umo if dst_umo and safe_key(dst_umo) == dst.key else ""
                await asyncio.to_thread(self.registry.adopt, dst.key, umo)
            if mode == MODE_MOVE:
                moved = await self._remove_landed(src_store, entries, results)
        report = self._report(src, dst, results)
        report["moved"] = moved
        return report

    @staticmethod
    async def _remove_landed(
        store: ScopeStore, entries: list[ImportEntry], results: list[ImportResult]
    ) -> int:
        forgotten = {e.name for e in entries if e.forgotten}
        moved = 0
        for item in results:
            if not item.landed:
                continue
            if item.name in forgotten:
                report = await store.delete_forgotten(item.name)
            else:
                report = await store.delete(item.name)
            moved += 1 if report.ok else 0
        return moved

    # ------------------------------------------------------------ 平台改名

    def platform_sessions(self, platform: str) -> list[tuple[str, str, SessionRecord | None]]:
        """属于某个平台 ID 的会话：(目录名, 可还原的 UMO 或空串, 登记记录)。"""
        records = self.registry.all()
        keys = sorted(set(self.store.session_keys()) | set(records))
        found = []
        for key in keys:
            if not is_scope_key(key):
                continue
            record = records.get(key)
            umo = record.umo if record is not None else ""
            if not umo:
                candidate = umo_from_key(key)
                umo = candidate if candidate and safe_key(candidate) == key else ""
            owner = umo.split(":", 1)[0] if umo else (
                record.platform if record is not None else guess_from_key(key)[0]
            )
            if owner == platform:
                found.append((key, umo, record))
        return found

    async def remap_platform(
        self,
        src_platform: str,
        dst_platform: str,
        *,
        mode: str = MODE_MOVE,
        policy: str = CONFLICT_NEWER,
        include_people: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """把旧平台 ID 下所有会话的记忆、登记与归档搬到新平台 ID 下。

        每个会话按旧 UMO 换上新平台 ID 算出新目录；移动模式下源会话的
        记忆全部落到目标后，源目录、登记记录一并清掉。
        """
        src_platform = check_platform(src_platform)
        dst_platform = check_platform(dst_platform)
        if src_platform == dst_platform:
            raise TransferError("新旧平台 ID 相同")
        if mode not in (MODE_COPY, MODE_MOVE):
            raise TransferError("迁移方式只能是 copy 或 move")
        items: list[dict] = []
        skipped: list[dict] = []
        sessions = await asyncio.to_thread(self.platform_sessions, src_platform)
        for key, umo, record in sessions:
            src = ScopeRef(SCOPE_SESSION, key)
            src_store = self.scope_store(src)
            has_files = await asyncio.to_thread(_has_memories, src_store)
            archives = await asyncio.to_thread(self.archive.count, key)
            if not has_files and not archives:
                continue
            if not umo:
                skipped.append(
                    {**self.describe(src), "reason": "缺少这个会话的完整标识，无法换算新会话"}
                )
                continue
            new_umo = dst_platform + ":" + umo.split(":", 1)[1]
            dst = ScopeRef(SCOPE_SESSION, safe_key(new_umo))
            if has_files:
                report = await self.transfer(
                    src, dst, dst_umo=new_umo, mode=mode, policy=policy, dry_run=dry_run
                )
            else:
                report = self._report(src, dst, [])
                report["moved"] = 0
            report["archives"] = archives
            report["to"]["umo"] = new_umo
            if not dry_run:
                await self._carry_session(key, dst.key, new_umo, record, mode, src_store)
                report["to"] = {**self.describe(dst), "umo": new_umo}
            items.append(report)
        if include_people:
            src = ScopeRef(SCOPE_PEOPLE, safe_key(src_platform))
            dst = ScopeRef(SCOPE_PEOPLE, safe_key(dst_platform))
            src_store = self.scope_store(src)
            if await asyncio.to_thread(_has_memories, src_store):
                report = await self.transfer(
                    src, dst, mode=mode, policy=policy, dry_run=dry_run
                )
                if not dry_run and mode == MODE_MOVE:
                    await self._drop_if_empty(src_store)
                items.append(report)
        totals = {name: 0 for name in OUTCOMES}
        for report in items:
            for name, value in report["counts"].items():
                totals[name] = totals.get(name, 0) + value
        return {"items": items, "skipped": skipped, "totals": totals}

    async def _carry_session(
        self,
        src_key: str,
        dst_key: str,
        dst_umo: str,
        record: SessionRecord | None,
        mode: str,
        src_store: ScopeStore,
    ) -> None:
        """登记信息与归档跟着会话走；移动且源已搬空时清掉源会话。"""
        await asyncio.to_thread(
            self.registry.adopt,
            dst_key,
            dst_umo,
            name=record.name if record else "",
            remark=record.remark if record else "",
            first_seen=record.first_seen if record else 0.0,
            last_active=record.last_active if record else 0.0,
        )
        move = mode == MODE_MOVE
        await asyncio.to_thread(self.archive.transfer, src_key, dst_key, move)
        if move and await self._drop_if_empty(src_store):
            await asyncio.to_thread(self.registry.remove, src_key)

    @staticmethod
    async def _drop_if_empty(store: ScopeStore) -> bool:
        """作用域里在册与已遗忘的记忆都没了就删掉整个目录；返回是否已空。"""
        if await asyncio.to_thread(_has_memories, store):
            return False
        await store.purge()
        return True

    # ------------------------------------------------------------ 报告

    def _report(
        self, src: ScopeRef, dst: ScopeRef | None, results: list[ImportResult]
    ) -> dict:
        report = {
            "from": self.describe(src),
            "counts": count_outcomes(results),
            "issues": [
                {"name": r.name, "outcome": r.outcome, "reason": r.reason}
                for r in results
                if r.outcome in (IMPORT_KEPT, IMPORT_INVALID)
            ],
        }
        report["to"] = self.describe(dst) if dst is not None else report["from"]
        return report
