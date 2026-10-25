"""WebUI 记忆管理页的后端接口。

页面（pages/memory）经 AstrBot 的插件页面桥接调用这里注册的接口，
管理员可以查看并编辑每个会话的记忆、整理记忆、查看压缩状态与归档，
设置定期整理并确认它给出的方案、撤销最近的整理，以及导出导入备份、
在会话之间或平台改名后迁移记忆。

接口约定：
    - 只用 GET / POST；成功返回 {"status": "ok", "message": "", "data": {...}}，
      失败返回 {"status": "error", "message": "原因", "data": {}}，HTTP 状态码
      一律 200（面板把 status=error 转成页面里的报错文案）。
    - 作用域用 scope=global|session 加 key（会话目录名）定位；目录名先校验
      格式再拼路径，不接受任何路径片段。
    - 改动记忆后作废对应会话的索引快照，下一轮对话就能看到。

AstrBot v4.24.1 起支持插件页面；更早的版本没有 register_web_api，
这里静默跳过注册，插件其余功能照常。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from astrbot.api import logger

from .core import consolidate, memstore, registry, tidy, transfer
from .core.compress import SessionState

if TYPE_CHECKING:
    from .main import MemoryBeyondPlugin

try:  # v4.26 起的插件 Web API 请求对象
    from astrbot.api.web import request as _plugin_request
except ImportError:  # 更早的版本走 Quart
    _plugin_request = None

PLUGIN_NAME = "astrbot_plugin_memory_beyond"
ROUTE_PREFIX = f"/{PLUGIN_NAME}/"
LOG_PREFIX = "[MemoryBeyond] "

SCOPE_GLOBAL = "global"
SCOPE_SESSION = "session"
SCOPE_PEOPLE = "people"
PEOPLE_FILE_RULE = "人物记忆一人一个文件，文件名必须是 user-<数字ID>.md"
MAX_REMARK = 64
MAX_QUERY = 200
# 导出、导入的暂存：先生成再下载、先预演再执行，期间内容放在内存里
STAGE_TTL = 30 * 60
MAX_STAGED = 4


class ApiError(Exception):
    """可以直接展示给管理员的错误。"""


def _ok(data: Any = None, message: str = "") -> dict:
    data = {} if data is None else data
    # 面板只把 data 转交给页面，提示文案（如索引快满）随 data 一起带过去
    if message and isinstance(data, dict):
        data.setdefault("message", message)
    return {"status": "ok", "message": message, "data": data}


def _error(message: str) -> dict:
    return {"status": "error", "message": message, "data": {}}


def _query(name: str, default: str = "") -> str:
    if _plugin_request is not None:
        value = _plugin_request.query.get(name, default)
    else:
        from quart import request

        value = request.args.get(name, default)
    return str(value if value is not None else default)


async def _body() -> dict:
    if _plugin_request is not None:
        data = await _plugin_request.json(default={})
    else:
        from quart import request

        data = await request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _text(raw: Any, limit: int | None = None) -> str:
    value = str(raw if raw is not None else "")
    return value[:limit] if limit else value


def _header_dict(header: memstore.MemoryHeader) -> dict:
    return {
        "file": header.file,
        "name": header.name,
        "description": header.description,
        "type": header.type,
        "mtime": header.mtime,
        "updated": header.updated,
        "created": header.created,
        "source": header.source,
        "size": header.size,
        "links": header.links,
        "lastUsed": header.last_used,
        "uses": header.uses,
        "forgottenAt": header.forgotten_at,
    }


def _record_dict(key: str, record: registry.SessionRecord | None) -> dict:
    if record is None:
        platform, kind, target = registry.guess_from_key(key)
        record = registry.SessionRecord(
            key=key, platform=platform, kind=kind, target=target
        )
    return {
        "key": key,
        "umo": record.umo,
        "platform": record.platform,
        "kind": record.kind,
        "target": record.target,
        "name": record.name,
        "remark": record.remark,
        "firstSeen": record.first_seen,
        "lastActive": record.last_active,
    }


def _state_dict(state: SessionState) -> dict:
    return {
        "watermark": state.watermark,
        "summary": state.summary,
        "ratio": state.ratio,
        "lastEstimate": state.last_estimate,
        "lastActual": state.last_actual,
        "lastParts": dict(state.last_parts or {}),
        "cacheHitTokens": state.cache_hit_tokens,
        "failCount": state.fail_count,
        "failUntil": state.fail_until,
        "compressedAt": state.compressed_at,
        "turnsSinceExtract": state.turns_since_extract,
        "extractedAt": state.extracted_at,
        "lastRequestAt": state.last_request_at,
    }


def _scan_scope(store: memstore.ScopeStore) -> tuple[int, float]:
    """(记忆文件数, 最近写入时间)；同步，放进线程执行。"""
    count = 0
    latest = 0.0
    if not store.root.is_dir():
        return 0, 0.0
    for name in store.list_files():
        count += 1
        mtime = store.mtime(name) or 0.0
        latest = max(latest, mtime)
    return count, latest


class MemoryDashboard:
    """把 WebUI 接口挂到 AstrBot 上，并实现每个接口。"""

    def __init__(self, plugin: "MemoryBeyondPlugin"):
        self.plugin = plugin
        self.transfer = transfer.MemoryTransfer(
            plugin._store, plugin._registry, plugin._archive
        )
        # 令牌 -> (过期时间, 内容)：生成好的备份、上传待导入的备份
        self._staged: dict[str, tuple[float, Any]] = {}

    # ------------------------------------------------------------ 注册

    def _routes(self) -> list[tuple[str, str, Callable[[], Awaitable[dict]], str]]:
        return [
            ("overview", "GET", self.overview, "记忆管理：会话总览"),
            ("scope", "GET", self.scope_detail, "记忆管理：作用域详情"),
            ("file", "GET", self.file_detail, "记忆管理：读取记忆文件"),
            ("search", "GET", self.search, "记忆管理：全文搜索"),
            ("session/state", "GET", self.session_state, "记忆管理：压缩状态"),
            ("archives", "GET", self.archives, "记忆管理：归档列表"),
            ("archive", "GET", self.archive_detail, "记忆管理：归档内容"),
            ("forgotten/file", "GET", self.forgotten_file, "记忆管理：读取已遗忘的文件"),
            ("file/save", "POST", self.file_save, "记忆管理：保存记忆文件"),
            ("file/delete", "POST", self.file_delete, "记忆管理：删除记忆文件"),
            ("file/forget", "POST", self.file_forget, "记忆管理：遗忘记忆文件"),
            ("forgotten/restore", "POST", self.forgotten_restore, "记忆管理：恢复已遗忘的文件"),
            ("forgotten/delete", "POST", self.forgotten_delete, "记忆管理：彻底删除已遗忘的文件"),
            ("index/save", "POST", self.index_save, "记忆管理：保存索引"),
            ("index/rebuild", "POST", self.index_rebuild, "记忆管理：重建索引"),
            ("session/reset", "POST", self.session_reset, "记忆管理：重置压缩状态"),
            ("session/remark", "POST", self.session_remark, "记忆管理：会话备注"),
            ("session/purge", "POST", self.session_purge, "记忆管理：清除会话数据"),
            (
                "consolidate/preview",
                "POST",
                self.consolidate_preview,
                "记忆管理：生成整理方案",
            ),
            (
                "consolidate/apply",
                "POST",
                self.consolidate_apply,
                "记忆管理：应用整理方案",
            ),
            ("autotidy", "GET", self.autotidy_status, "记忆管理：定期整理状态"),
            ("autotidy/config", "POST", self.autotidy_config, "记忆管理：设置定期整理"),
            ("autotidy/dismiss", "POST", self.autotidy_dismiss, "记忆管理：放弃待确认的方案"),
            ("tidy/runs", "GET", self.tidy_runs, "记忆管理：整理记录"),
            ("tidy/undo", "POST", self.tidy_undo, "记忆管理：撤销整理"),
            ("migrate/info", "GET", self.migrate_info, "记忆管理：迁移可选项"),
            ("export/prepare", "POST", self.export_prepare, "记忆管理：生成备份"),
            ("export/file", "GET", self.export_file, "记忆管理：下载备份"),
            ("import/preview", "POST", self.import_preview, "记忆管理：预览导入"),
            ("import/apply", "POST", self.import_apply, "记忆管理：导入备份"),
            ("migrate/scope", "POST", self.migrate_scope, "记忆管理：迁移一处记忆"),
            ("migrate/platform", "POST", self.migrate_platform, "记忆管理：平台改名迁移"),
        ]

    def register(self) -> None:
        context = self.plugin.context
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            logger.info(
                LOG_PREFIX
                + "当前 AstrBot 版本不支持插件 Web 接口，记忆管理页不可用"
                "（需要 v4.24.1 及以上），其余功能不受影响"
            )
            return
        # 插件重载时旧实例的路由可能还在，先清掉再挂新的
        self._purge_routes()
        for path, method, handler, desc in self._routes():
            register(ROUTE_PREFIX + path, self._wrap(handler), [method], desc)

    def unregister(self) -> None:
        self._purge_routes()

    def _purge_routes(self) -> None:
        apis = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(apis, list):
            apis[:] = [
                api
                for api in apis
                if not str(api[0] if api else "").startswith(ROUTE_PREFIX)
            ]

    @staticmethod
    def _wrap(handler: Callable[[], Awaitable[dict]]):
        async def view(*_args, **_kwargs):
            try:
                return await handler()
            except (ApiError, transfer.TransferError, tidy.TidyError) as exc:
                return _error(str(exc))
            except Exception:  # noqa: BLE001 - 接口异常不能打断面板
                logger.error(LOG_PREFIX + "记忆管理接口出错", exc_info=True)
                return _error("服务器内部错误，详情见 AstrBot 日志")

        view.__name__ = f"memory_beyond_{handler.__name__}"
        return view

    # ------------------------------------------------------------ 定位

    def _session_key(self, raw: Any) -> str:
        key = _text(raw).strip()
        if not memstore.is_scope_key(key):
            raise ApiError("会话标识不合法")
        return key

    def _scope(self, scope: Any, key: Any) -> tuple[str, str | None, memstore.ScopeStore]:
        scope = _text(scope).strip().lower()
        if scope == SCOPE_GLOBAL:
            return SCOPE_GLOBAL, None, self.plugin._store.global_scope()
        if scope == SCOPE_SESSION:
            session_key = self._session_key(key)
            store = self.plugin._store.session_scope_by_key(session_key)
            if store is None:
                raise ApiError("会话标识不合法")
            return SCOPE_SESSION, session_key, store
        if scope == SCOPE_PEOPLE:
            people_key = _text(key).strip()
            store = self.plugin._store.people_scope_by_key(people_key)
            if store is None:
                raise ApiError("平台标识不合法")
            return SCOPE_PEOPLE, people_key, store
        raise ApiError("作用域只能是 global、session 或 people")

    def _scope_from_query(self):
        return self._scope(_query("scope"), _query("key"))

    def _scope_from_body(self, body: dict):
        return self._scope(body.get("scope"), body.get("key"))

    @staticmethod
    def _file_name(raw: Any) -> str:
        name = _text(raw).strip()
        if name and not name.endswith(memstore.MEMORY_FILE_SUFFIX):
            name += memstore.MEMORY_FILE_SUFFIX
        # 不分大小写：Windows / macOS 上 memory.md 和 MEMORY.md 是同一个文件
        if name.lower() == memstore.INDEX_FILENAME.lower():
            raise ApiError("MEMORY.md 请在「索引」页编辑")
        if not name or not memstore.ScopeStore.is_valid_name(name):
            raise ApiError("文件名不合法：" + memstore.ScopeStore.path_rules())
        return name

    def _changed(self, key: str | None) -> None:
        self.plugin.invalidate_for_key(key)

    def _scope_label(self, scope: str, key: str | None) -> str:
        record = self.plugin._registry.get(key or "") if scope == SCOPE_SESSION else None
        return tidy.scope_label(scope, key, record)

    def _stage(self, payload: Any) -> str:
        now = time.time()
        for token, (expires, _) in list(self._staged.items()):
            if expires < now:
                del self._staged[token]
        while len(self._staged) >= MAX_STAGED:
            del self._staged[min(self._staged, key=lambda t: self._staged[t][0])]
        token = secrets.token_urlsafe(16)
        self._staged[token] = (now + STAGE_TTL, payload)
        return token

    def _staged_payload(self, raw: Any) -> Any:
        found = self._staged.get(_text(raw).strip())
        if found is None or found[0] < time.time():
            raise ApiError("内容已过期，请重新选择备份文件或重新生成")
        return found[1]

    @staticmethod
    def _tidy_info(meta: tidy.TidyMeta) -> dict:
        pending = meta.pending or {}
        return {
            "lastRun": meta.last_run,
            "lastAttempt": meta.last_attempt,
            "outcome": meta.outcome,
            "message": meta.message,
            "pendingCount": meta.pending_count(),
            "pendingAt": pending.get("createdAt", 0) if pending else 0,
        }

    # ------------------------------------------------------------ 查询

    async def overview(self) -> dict:
        plugin = self.plugin
        store = plugin._store
        records = plugin._registry.all()
        keys = sorted(set(store.session_keys()) | set(records))
        # 作用域对象在事件循环里取好再进线程：同一目录只能有一个实例
        # （串行写入的锁挂在上面），线程里懒建会和循环上的处理器撞车
        global_scope = store.global_scope()
        scopes = {
            key: store.session_scope_by_key(key)
            for key in keys
            if memstore.is_scope_key(key)
        }
        people_scopes = {
            key: store.people_scope_by_key(key) for key in store.people_keys()
        }

        def collect() -> tuple[dict, list[dict], list[dict], dict]:
            metas = plugin._tidy.all_meta()

            def pending(scope: str, key: str | None) -> int:
                meta = metas.get(tidy.scope_id(scope, key))
                return meta.pending_count() if meta is not None else 0

            g_count, g_latest = _scan_scope(global_scope)
            sessions = []
            for key, scope in scopes.items():
                count, latest = _scan_scope(scope) if scope else (0, 0.0)
                item = _record_dict(key, records.get(key))
                item.update(
                    fileCount=count,
                    updatedAt=latest,
                    archiveCount=plugin._archive.count(key),
                    hasState=plugin.state_path_for_key(key).is_file(),
                    tidyPending=pending(SCOPE_SESSION, key),
                )
                sessions.append(item)
            platforms = []
            for key, scope in people_scopes.items():
                count, latest = _scan_scope(scope) if scope else (0, 0.0)
                platforms.append(
                    {
                        "key": key,
                        "platform": key,
                        "fileCount": count,
                        "updatedAt": latest,
                        "tidyPending": pending(SCOPE_PEOPLE, key),
                    }
                )
            global_info = {
                "fileCount": g_count,
                "updatedAt": g_latest,
                "tidyPending": pending(SCOPE_GLOBAL, None),
            }
            return global_info, sessions, platforms, plugin._tidy.settings()

        global_info, sessions, platforms, settings = await asyncio.to_thread(collect)
        global_info["enabled"] = plugin._global_on()
        return _ok(
            {
                "memoryEnabled": plugin._bool("enable_memory", True),
                "global": global_info,
                "people": {"enabled": plugin._people_on(), "platforms": platforms},
                "sessions": sessions,
                "autoTidy": self._autotidy_settings(settings),
            }
        )

    def _autotidy_settings(self, settings: dict) -> dict:
        mode = self.plugin.tidy_mode()
        return {
            "mode": mode,
            "hours": self.plugin.tidy_hours(),
            # 配置页里已经开过的不算没问过
            "asked": bool(settings.get("asked")) or mode != tidy.MODE_OFF,
            "minChanges": tidy.MIN_CHANGES,
        }

    async def scope_detail(self) -> dict:
        scope, key, store = self._scope_from_query()

        def collect() -> dict:
            headers = store.headers()
            report = store.index_report()
            index_path = store.root / memstore.INDEX_FILENAME
            index_text = (
                index_path.read_text("utf-8") if index_path.is_file() else ""
            )
            return {
                "files": [_header_dict(h) for h in headers],
                "forgotten": [_header_dict(h) for h in store.forgotten_headers()],
                "index": {
                    "exists": report.exists,
                    "content": index_text,
                    "lines": report.lines,
                    "size": report.size,
                    "truncated": report.truncated,
                    "missing": report.missing,
                    "dangling": report.dangling,
                    "maxLines": memstore.INDEX_MAX_LINES,
                    "maxBytes": memstore.INDEX_MAX_BYTES,
                },
            }

        data = await asyncio.to_thread(collect)
        meta = await asyncio.to_thread(
            self.plugin._tidy.meta, tidy.scope_id(scope, key)
        )
        data["scope"] = scope
        data["key"] = key
        data["tidy"] = self._tidy_info(meta)
        # 待确认的方案原样给页面，确认时再走 consolidate/apply 重新校验
        data["tidy"]["plan"] = meta.pending
        data["tidyMode"] = self.plugin.tidy_mode()
        data["forgettingEnabled"] = self.plugin._forgetting_on()
        data["forgetAfterDays"] = self.plugin._forget_after_days()
        if scope == SCOPE_SESSION:
            data["session"] = _record_dict(key, self.plugin._registry.get(key))
            data["archiveCount"] = await asyncio.to_thread(
                self.plugin._archive.count, key
            )
        elif scope == SCOPE_PEOPLE:
            data["people"] = {"key": key, "platform": key}
            data["enabled"] = self.plugin._people_on()
        else:
            data["enabled"] = self.plugin._global_on()
        return _ok(data)

    async def file_detail(self) -> dict:
        _, _, store = self._scope_from_query()
        name = self._file_name(_query("file"))
        content = await store.read(name)
        if content is None:
            raise ApiError(f"文件不存在：{name}")
        doc = memstore.parse_memory(content)
        used = await asyncio.to_thread(store.usage_of, name)
        return _ok(
            {
                "file": name,
                "content": content,
                "mtime": store.mtime(name) or 0.0,
                "lastUsed": used.last_used,
                "uses": used.uses,
                "name": doc.name,
                "description": doc.description,
                "type": doc.type,
                "body": doc.body,
                "created": doc.created,
                "updated": doc.updated,
                "source": doc.source,
                "hasFrontmatter": doc.has_frontmatter,
                "links": memstore.extract_links(content),
            }
        )

    async def search(self) -> dict:
        _, _, store = self._scope_from_query()
        query = _query("q").strip()[:MAX_QUERY]
        if not query:
            return _ok({"results": []})
        results = []
        for line in await store.search(query):
            file, _, excerpt = line.partition(": ")
            results.append({"file": file, "excerpt": excerpt})
        return _ok({"results": results})

    async def _session_state(self, key: str) -> tuple[SessionState, bool]:
        """(压缩状态, 是否正被插件持有)；持有中的以内存为准。"""
        found = self.plugin.runtime_for_key(key)
        if found is not None:
            return found[1].state, True
        return await self.plugin.load_state_file(self.plugin.state_path_for_key(key)), False

    async def session_state(self) -> dict:
        key = self._session_key(_query("key"))
        state, live = await self._session_state(key)
        data = _state_dict(state)
        data["live"] = live
        data["now"] = time.time()
        return _ok(data)

    async def archives(self) -> dict:
        key = self._session_key(_query("key"))
        items = await asyncio.to_thread(self.plugin._archive.list, key)
        return _ok({"archives": items})

    async def archive_detail(self) -> dict:
        key = self._session_key(_query("key"))
        archive_id = _query("id").strip()
        data = await asyncio.to_thread(self.plugin._archive.load, key, archive_id)
        if data is None:
            raise ApiError("归档不存在或已被清理")
        return _ok(data)

    # ------------------------------------------------------------ 记忆编辑

    async def file_save(self) -> dict:
        """保存记忆文件。

        mode=fields：按表单字段（名称取自文件名、类型、描述、正文）生成标准格式；
        mode=raw：原样保存管理员编辑的完整文本。
        oldFile 与 file 不同时视为改名：先写新文件、再删旧文件。
        baseMtime 是打开文件时的写入时间，期间文件被改过（比如模型刚写过）
        就拒绝保存，避免覆盖。
        """
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        name = self._file_name(body.get("file"))
        if scope == SCOPE_PEOPLE and not memstore.is_person_file(name):
            raise ApiError(PEOPLE_FILE_RULE)
        old_raw = _text(body.get("oldFile")).strip()
        old = self._file_name(old_raw) if old_raw else ""
        renaming = bool(old) and old != name

        if _text(body.get("mode")).strip().lower() == "raw":
            content = _text(body.get("content"))
        else:
            mem_type = _text(body.get("type")).strip().lower()
            if mem_type not in memstore.MEMORY_TYPES:
                raise ApiError("类型只能是 user、feedback、project、reference 之一")
            if scope == SCOPE_PEOPLE and mem_type != "user":
                raise ApiError("人物记忆的类型只能是 user")
            description = " ".join(_text(body.get("description")).split())
            if not description:
                raise ApiError("请填写描述：它会成为索引里的那一行")
            content = memstore.compose_memory(
                name[: -len(memstore.MEMORY_FILE_SUFFIX)],
                description,
                mem_type,
                _text(body.get("body")).strip("\n"),
            )
        if not content.strip():
            raise ApiError("内容不能为空")

        if (not old or renaming) and store.exists(name):
            raise ApiError(f"已存在同名文件 {name}，请换一个文件名")
        if old:
            if not store.exists(old):
                raise ApiError(f"文件 {old} 已不存在，可能刚被删除，请刷新")
            base = body.get("baseMtime")
            current = store.mtime(old) or 0.0
            if isinstance(base, (int, float)) and abs(current - float(base)) > 1e-3:
                raise ApiError("这个文件在你编辑期间被修改过（可能是模型刚写入），请刷新后再改")

        report = await store.write(name, content)
        if not report.ok:
            raise ApiError(report.message)
        if renaming:
            await store.delete(old)
        self._changed(key)
        return _ok({"file": name, "mtime": store.mtime(name) or 0.0}, report.message)

    async def file_delete(self) -> dict:
        body = await _body()
        _, key, store = self._scope_from_body(body)
        name = self._file_name(body.get("file"))
        report = await store.delete(name)
        if not report.ok:
            raise ApiError(report.message)
        self._changed(key)
        return _ok({"file": name}, report.message)

    # ------------------------------------------------------------ 遗忘

    async def forgotten_file(self) -> dict:
        _, _, store = self._scope_from_query()
        name = self._file_name(_query("file"))
        content = await store.read_forgotten(name)
        if content is None:
            raise ApiError(f"没有已遗忘的 {name}")
        used = await asyncio.to_thread(store.usage_of, name)
        return _ok(
            {
                "file": name,
                "content": content,
                "lastUsed": used.last_used,
                "uses": used.uses,
                "forgottenAt": used.forgotten_at,
            }
        )

    async def file_forget(self) -> dict:
        """管理员手动把一个在册文件移入已遗忘。"""
        body = await _body()
        _, key, store = self._scope_from_body(body)
        name = self._file_name(body.get("file"))
        report = await store.forget(name)
        if not report.ok:
            raise ApiError(report.message)
        self._changed(key)
        return _ok({"file": name}, report.message)

    async def forgotten_restore(self) -> dict:
        body = await _body()
        _, key, store = self._scope_from_body(body)
        name = self._file_name(body.get("file"))
        report = await store.restore(name)
        if not report.ok:
            raise ApiError(report.message)
        self._changed(key)
        return _ok({"file": name}, report.message)

    async def forgotten_delete(self) -> dict:
        body = await _body()
        _, _, store = self._scope_from_body(body)
        name = self._file_name(body.get("file"))
        report = await store.delete_forgotten(name)
        if not report.ok:
            raise ApiError(report.message)
        return _ok({"file": name}, report.message)

    async def index_save(self) -> dict:
        body = await _body()
        _, key, store = self._scope_from_body(body)
        report = await store.write_index(_text(body.get("content")))
        if not report.ok:
            raise ApiError(report.message)
        self._changed(key)
        return _ok({}, report.message)

    async def index_rebuild(self) -> dict:
        body = await _body()
        _, key, store = self._scope_from_body(body)
        count = await store.rebuild_index()
        self._changed(key)
        return _ok({"count": count}, f"索引已按 {count} 个记忆文件重建")

    # ------------------------------------------------------------ 会话管理

    async def session_reset(self) -> dict:
        body = await _body()
        key = self._session_key(body.get("key"))
        plugin = self.plugin
        found = plugin.runtime_for_key(key)
        path = plugin.state_path_for_key(key)
        if found is not None:
            state = found[1].state
        elif path.is_file():
            state = await plugin.load_state_file(path)
        else:
            return _ok({}, "该会话还没有压缩状态")
        state.reset_compression()
        state.record_success()
        plugin.invalidate_for_key(key)
        await plugin.save_state_file(path, state)
        return _ok({}, "已清空该会话的摘要与水位线")

    async def session_remark(self) -> dict:
        body = await _body()
        key = self._session_key(body.get("key"))
        remark = _text(body.get("remark")).strip()[:MAX_REMARK]
        record = await asyncio.to_thread(self.plugin._registry.set_remark, key, remark)
        return _ok(_record_dict(key, record), "备注已保存")

    async def session_purge(self) -> dict:
        """清除会话数据。

        target=memories（默认）：只删该会话的记忆目录；
        target=all：连同压缩状态、归档、登记记录一并删除。
        """
        body = await _body()
        key = self._session_key(body.get("key"))
        plugin = self.plugin
        store = plugin._store.session_scope_by_key(key)
        removed = await store.purge() if store is not None else 0
        plugin.invalidate_for_key(key)
        # 记忆都没了，待确认的方案与整理记录也跟着作废
        await asyncio.to_thread(
            plugin._tidy.remove, tidy.scope_id(SCOPE_SESSION, key)
        )
        if _text(body.get("target")).strip().lower() != "all":
            return _ok({"removed": removed}, f"已删除 {removed} 个记忆文件")

        found = plugin.runtime_for_key(key)
        if found is not None:
            plugin._sessions.pop(found[0], None)

        def wipe() -> None:
            plugin._archive.purge(key)
            path = plugin.state_path_for_key(key)
            if path.is_file():
                path.unlink()
            plugin._registry.remove(key)

        await asyncio.to_thread(wipe)
        return _ok(
            {"removed": removed},
            "已删除该会话的记忆、压缩状态与归档；会话再次发言时会重新登记",
        )

    # ------------------------------------------------------------ 记忆整理

    async def consolidate_preview(self) -> dict:
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        book = self.plugin._tidy
        sid = tidy.scope_id(scope, key)
        if not book.claim(sid):
            raise ApiError("这个作用域正在整理中，请等待结果")
        try:
            proposal = await tidy.propose(
                store, self._scope_label(scope, key), self.plugin.call_consolidator
            )
        finally:
            book.release(sid)
        return _ok(
            {
                "operations": [op.to_dict() for op in proposal.operations],
                "skipped": proposal.skipped,
                "fileCount": len(proposal.files),
                "files": proposal.files,
            }
        )

    async def consolidate_apply(self) -> dict:
        """应用整理方案（管理页手动整理，或确认定期整理留下的方案）。

        fromPending=true 表示这是定期整理待确认的方案：应用后方案清掉。
        每次应用都记一份可撤销的记录，也算一次整理，定期整理重新计时。
        """
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        raw_ops = body.get("operations")
        from_pending = bool(body.get("fromPending"))
        existing = set(await asyncio.to_thread(store.list_files))
        ops = consolidate.operations_from_payload(raw_ops, existing)
        requested = consolidate.requested_files(raw_ops)
        if not ops and not requested:
            raise ApiError("没有可应用的操作")
        book = self.plugin._tidy
        sid = tidy.scope_id(scope, key)
        if not book.claim(sid):
            raise ApiError("这个作用域正在整理中，请稍后再试")
        try:
            # 没通过重新校验的条目（比如复盘后文件已不在）也要让管理员看到
            skipped = [
                {"op": "", "file": file, "ok": False, "message": "未通过校验，已跳过"}
                for file in sorted(requested - {op.file for op in ops})
            ]
            results = await tidy.apply(store, ops, people=scope == SCOPE_PEOPLE)
            now = time.time()
            run = tidy.make_run(
                tidy.TRIGGER_CONFIRMED if from_pending else tidy.TRIGGER_MANUAL,
                results,
                now,
            )
            done = sum(1 for item in results if item["ok"])

            def record() -> None:
                if run is not None:
                    book.add_run(sid, run)

                def change(meta: tidy.TidyMeta) -> None:
                    meta.last_run = now
                    meta.failures = 0
                    meta.outcome = tidy.OUTCOME_APPLIED
                    meta.message = f"应用了 {done} 项修改"
                    if from_pending:
                        meta.pending = None

                book.update_meta(sid, change)

            await asyncio.to_thread(record)
        finally:
            book.release(sid)
        self._changed(key)
        results = skipped + tidy.public_results(results)
        return _ok(
            {"results": results, "runId": run["id"] if run else ""},
            f"已应用 {done} / {len(results)} 项操作",
        )

    # ------------------------------------------------------------ 定期整理

    async def autotidy_status(self) -> dict:
        """定期整理的设置，以及每处最近一次整理的结论与待确认的方案。"""
        book = self.plugin._tidy
        settings, metas = await asyncio.to_thread(
            lambda: (book.settings(), book.all_meta())
        )
        scopes = []
        for sid, meta in metas.items():
            scope, _, key = sid.partition("-")
            if scope not in (SCOPE_GLOBAL, SCOPE_SESSION, SCOPE_PEOPLE):
                continue
            if scope != SCOPE_GLOBAL and not memstore.is_scope_key(key):
                continue
            ref = transfer.ScopeRef(scope, key if scope != SCOPE_GLOBAL else "")
            item = self.transfer.describe(ref)
            item.update(self._tidy_info(meta))
            scopes.append(item)
        scopes.sort(key=lambda item: -max(item["lastRun"], item["lastAttempt"]))
        data = self._autotidy_settings(settings)
        data["scopes"] = scopes
        data["pendingTotal"] = sum(1 for item in scopes if item["pendingCount"])
        return _ok(data)

    async def autotidy_config(self) -> dict:
        """改定期整理的方式与间隔（写回插件配置），并记下已经问过。"""
        body = await _body()
        mode = _text(body.get("mode")).strip().lower()
        if mode not in tidy.MODES:
            raise ApiError("定期整理的方式只能是 off、confirm 或 auto")
        hours = body.get("hours")
        hours = tidy.clamp_hours(hours) if hours is not None else None
        await self.plugin.save_tidy_settings(mode, hours)
        await asyncio.to_thread(self.plugin._tidy.mark_asked)
        labels = {
            tidy.MODE_OFF: "已关闭定期整理",
            tidy.MODE_CONFIRM: "已开启定期整理：有方案时先问你",
            tidy.MODE_AUTO: "已开启定期整理：有方案时直接应用",
        }
        data = self._autotidy_settings({"asked": True})
        return _ok(data, labels[mode])

    async def autotidy_dismiss(self) -> dict:
        body = await _body()
        scope, key, _ = self._scope_from_body(body)

        def change(meta: tidy.TidyMeta) -> None:
            meta.pending = None
            meta.outcome = ""
            meta.message = "待确认的方案已放弃"

        await asyncio.to_thread(
            self.plugin._tidy.update_meta, tidy.scope_id(scope, key), change
        )
        return _ok({}, "已放弃这份方案；下次定期整理时会重新复盘")

    async def tidy_runs(self) -> dict:
        scope, key, _ = self._scope_from_query()
        book = self.plugin._tidy
        sid = tidy.scope_id(scope, key)
        runs, meta = await asyncio.to_thread(lambda: (book.runs(sid), book.meta(sid)))
        return _ok(
            {
                "runs": [tidy.public_run(run) for run in runs],
                "tidy": self._tidy_info(meta),
                "mode": self.plugin.tidy_mode(),
            }
        )

    async def tidy_undo(self) -> dict:
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        run_id = _text(body.get("id")).strip()
        book = self.plugin._tidy
        sid = tidy.scope_id(scope, key)
        runs = await asyncio.to_thread(book.runs, sid)
        run = next((r for r in runs if r.get("id") == run_id), None)
        if run is None:
            raise ApiError("这条整理记录已不存在")
        if run.get("undoneAt"):
            raise ApiError("这次整理已经撤销过了")
        if not book.claim(sid):
            raise ApiError("这个作用域正在整理中，请稍后再试")
        try:
            results = await tidy.undo(store, run)
            await asyncio.to_thread(book.mark_undone, sid, run_id, time.time())
        finally:
            book.release(sid)
        self._changed(key)
        done = sum(1 for item in results if item["ok"])
        return _ok({"results": results}, f"已撤销 {done} / {len(results)} 项")

    # ------------------------------------------------------------ 迁移与备份

    def _ref(self, raw: Any) -> transfer.ScopeRef:
        if not isinstance(raw, dict):
            raise ApiError("缺少作用域")
        return transfer.make_ref(raw.get("scope"), raw.get("key"))

    def _changed_ref(self, ref: transfer.ScopeRef) -> None:
        if ref.scope == SCOPE_SESSION:
            self._changed(ref.key)
        elif ref.scope == SCOPE_GLOBAL:
            self._changed(None)

    async def _drop_pending(self, ref: transfer.ScopeRef) -> None:
        """记忆被搬动后，那里待确认的整理方案就对不上了。"""

        def change(meta: tidy.TidyMeta) -> None:
            meta.pending = None

        sid = tidy.scope_id(ref.scope, ref.key or None)
        meta = await asyncio.to_thread(self.plugin._tidy.meta, sid)
        if meta.pending:
            await asyncio.to_thread(self.plugin._tidy.update_meta, sid, change)

    async def migrate_info(self) -> dict:
        """迁移时可选的平台 ID：已有会话与人物记忆用到的，加上当前启用的机器人。"""
        records = self.plugin._registry.all()
        counts: dict[str, int] = {}

        def collect() -> list[str]:
            for key in sorted(set(self.plugin._store.session_keys()) | set(records)):
                record = records.get(key)
                platform = (
                    record.platform if record is not None and record.platform
                    else registry.guess_from_key(key)[0]
                )
                if platform:
                    counts[platform] = counts.get(platform, 0) + 1
            return self.plugin._store.people_keys()

        people = await asyncio.to_thread(collect)
        return _ok(
            {
                "platforms": [
                    {"platform": p, "sessions": n, "people": p in people}
                    for p, n in sorted(counts.items())
                ]
                + [
                    {"platform": p, "sessions": 0, "people": True}
                    for p in people
                    if p not in counts
                ],
                "adapters": self.plugin.platform_ids(),
            }
        )

    async def export_prepare(self) -> dict:
        """生成一份备份暂存起来，返回下载用的令牌和内容概要。"""
        body = await _body()
        raw_scopes = body.get("scopes")
        refs = None
        if isinstance(raw_scopes, list):
            refs = []
            for raw in raw_scopes:
                ref = self._ref(raw)
                if ref not in refs:
                    refs.append(ref)
        bundle = await self.transfer.export(
            refs, include_forgotten=bool(body.get("includeForgotten", True))
        )
        if not bundle["scopes"]:
            raise ApiError("选中的地方都还没有记忆，没有可导出的内容")
        token = self._stage(bundle)
        files = sum(len(item["files"]) for item in bundle["scopes"])
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(bundle["exportedAt"]))
        return _ok(
            {
                "token": token,
                "scopeCount": len(bundle["scopes"]),
                "fileCount": files,
                "filename": f"memory-backup-{stamp}.json",
            }
        )

    async def export_file(self) -> dict:
        """下载生成好的备份：直接返回备份内容本身。"""
        bundle = self._staged_payload(_query("token"))
        if not isinstance(bundle, dict) or bundle.get("format") != transfer.BUNDLE_FORMAT:
            raise ApiError("内容已过期，请重新生成备份")
        return bundle

    async def import_preview(self) -> dict:
        """读入备份并预演：每处有多少条会新增、覆盖、保留。

        首次传 bundle（备份内容），之后改冲突策略时只传 token。
        """
        body = await _body()
        if body.get("token"):
            token = _text(body.get("token")).strip()
            scopes = self._staged_payload(token)
        else:
            scopes = transfer.parse_bundle(body.get("bundle"))
            token = self._stage(scopes)
        if not isinstance(scopes, list):
            raise ApiError("内容已过期，请重新选择备份文件")
        if not scopes:
            raise ApiError("备份里没有记忆")
        policy = transfer.check_policy(body.get("policy"))
        summary = self.transfer.bundle_summary(scopes)
        reports = await self.transfer.import_bundle(scopes, policy, dry_run=True)
        for item, report in zip(summary, reports):
            item["counts"] = report["counts"]
            item["issues"] = report["issues"]
        return _ok({"token": token, "scopes": summary})

    async def import_apply(self) -> dict:
        body = await _body()
        scopes = self._staged_payload(body.get("token"))
        if not isinstance(scopes, list):
            raise ApiError("内容已过期，请重新选择备份文件")
        policy = transfer.check_policy(body.get("policy"))
        wanted = body.get("selection")
        if isinstance(wanted, list):
            ids = {self._ref(raw).id for raw in wanted}
            scopes = [item for item in scopes if item.ref.id in ids]
        if not scopes:
            raise ApiError("没有选中要导入的地方")
        reports = await self.transfer.import_bundle(scopes, policy)
        for item in scopes:
            self._changed_ref(item.ref)
            await self._drop_pending(item.ref)
        written = sum(
            r["counts"]["added"] + r["counts"]["replaced"] for r in reports
        )
        return _ok(
            {"reports": reports},
            f"导入完成：{len(reports)} 处，写入 {written} 条记忆",
        )

    async def migrate_scope(self) -> dict:
        """把一处的记忆复制或移动到另一处；目标会话可以用完整的会话标识新建。"""
        body = await _body()
        src = self._ref(body.get("from"))
        target = body.get("to") if isinstance(body.get("to"), dict) else {}
        dst_umo = _text(target.get("umo")).strip()
        if dst_umo:
            if target.get("scope") not in (None, "", SCOPE_SESSION):
                raise ApiError("会话标识只能用于会话")
            if dst_umo.count(":") < 2:
                raise ApiError("会话标识的格式是 平台ID:消息类型:会话ID")
            dst = transfer.ScopeRef(SCOPE_SESSION, memstore.safe_key(dst_umo))
        elif target.get("scope") == SCOPE_PEOPLE and target.get("platform"):
            # 还没有人物记忆的平台：按平台 ID 算出目录名
            platform = transfer.check_platform(target.get("platform"))
            dst = transfer.ScopeRef(SCOPE_PEOPLE, memstore.safe_key(platform))
        else:
            dst = self._ref(target)
        raw_files = body.get("files")
        files = (
            [_text(name).strip() for name in raw_files]
            if isinstance(raw_files, list)
            else None
        )
        dry_run = bool(body.get("dryRun"))
        mode = _text(body.get("mode") or transfer.MODE_COPY).strip().lower()
        report = await self.transfer.transfer(
            src,
            dst,
            dst_umo=dst_umo,
            files=files,
            include_forgotten=bool(body.get("includeForgotten", True)),
            mode=mode,
            policy=transfer.check_policy(body.get("policy")),
            dry_run=dry_run,
        )
        if dry_run:
            return _ok(report)
        self._changed_ref(dst)
        await self._drop_pending(dst)
        if report.get("moved"):
            self._changed_ref(src)
            await self._drop_pending(src)
        counts = report["counts"]
        message = f"已写入 {counts['added'] + counts['replaced']} 条"
        if report.get("moved"):
            message += f"，已从原处移走 {report['moved']} 条"
        return _ok(report, message)

    async def migrate_platform(self) -> dict:
        """平台 ID 变了之后，把旧 ID 下所有会话的记忆搬到新 ID 下。"""
        body = await _body()
        dry_run = bool(body.get("dryRun"))
        mode = _text(body.get("mode") or transfer.MODE_MOVE).strip().lower()
        result = await self.transfer.remap_platform(
            _text(body.get("from")),
            _text(body.get("to")),
            mode=mode,
            policy=transfer.check_policy(body.get("policy")),
            include_people=bool(body.get("includePeople", True)),
            dry_run=dry_run,
        )
        if dry_run:
            return _ok(result)
        for item in result["items"]:
            for side in ("from", "to"):
                ref = transfer.ScopeRef(item[side]["scope"], item[side]["key"])
                self._changed_ref(ref)
                await self._drop_pending(ref)
        totals = result["totals"]
        return _ok(
            result,
            f"迁移完成：{len(result['items'])} 处，写入 "
            f"{totals['added'] + totals['replaced']} 条记忆",
        )
