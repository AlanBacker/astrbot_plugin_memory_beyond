"""WebUI 记忆管理页的后端接口。

页面（pages/memory）经 AstrBot 的插件页面桥接调用这里注册的接口，
管理员可以查看并编辑每个会话的记忆、整理记忆、查看压缩状态与归档。

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
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from astrbot.api import logger

from .core import consolidate, memstore, registry
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
MAX_REMARK = 64
MAX_QUERY = 200


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
        "size": header.size,
        "links": header.links,
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
        # 正在整理的作用域，防止重复点击发起多次模型调用
        self._consolidating: set[str] = set()

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
            ("file/save", "POST", self.file_save, "记忆管理：保存记忆文件"),
            ("file/delete", "POST", self.file_delete, "记忆管理：删除记忆文件"),
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
            except ApiError as exc:
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
        raise ApiError("作用域只能是 global 或 session")

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
        if scope == SCOPE_GLOBAL:
            return "机器人自我记忆（全局）"
        record = self.plugin._registry.get(key or "")
        if record is not None and (record.remark or record.name):
            return record.remark or record.name
        return key or ""

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

        def collect() -> tuple[dict, list[dict]]:
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
                )
                sessions.append(item)
            return {"fileCount": g_count, "updatedAt": g_latest}, sessions

        global_info, sessions = await asyncio.to_thread(collect)
        global_info["enabled"] = plugin._global_on()
        return _ok(
            {
                "memoryEnabled": plugin._bool("enable_memory", True),
                "global": global_info,
                "sessions": sessions,
            }
        )

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
        data["scope"] = scope
        data["key"] = key
        if scope == SCOPE_SESSION:
            data["session"] = _record_dict(key, self.plugin._registry.get(key))
            data["archiveCount"] = await asyncio.to_thread(
                self.plugin._archive.count, key
            )
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
        return _ok(
            {
                "file": name,
                "content": content,
                "mtime": store.mtime(name) or 0.0,
                "name": doc.name,
                "description": doc.description,
                "type": doc.type,
                "body": doc.body,
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
        old_raw = _text(body.get("oldFile")).strip()
        old = self._file_name(old_raw) if old_raw else ""
        renaming = bool(old) and old != name

        if _text(body.get("mode")).strip().lower() == "raw":
            content = _text(body.get("content"))
        else:
            mem_type = _text(body.get("type")).strip().lower()
            if mem_type not in memstore.MEMORY_TYPES:
                raise ApiError("类型只能是 user、feedback、project、reference 之一")
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
        lock_id = f"{scope}:{key or ''}"
        if lock_id in self._consolidating:
            raise ApiError("这个作用域正在整理中，请等待结果")
        self._consolidating.add(lock_id)
        try:
            headers = await asyncio.to_thread(store.headers)
            if not headers:
                raise ApiError("这里还没有记忆，无需整理")
            files = []
            for header in headers:
                content = await store.read(header.file)
                if content:
                    files.append((header, content))
            prompt, skipped = consolidate.build_prompt(
                self._scope_label(scope, key), files
            )
            text, attempts = await self.plugin.call_summarizer(prompt)
            if not text:
                raise ApiError(
                    "整理模型调用失败："
                    + ("；".join(attempts) or "没有可用的模型提供商")
                )
            existing = {h.file for h in headers}
            ops = consolidate.parse_operations(text, existing)
            if ops is None:
                raise ApiError("模型返回的整理方案无法解析，请重试")
            # 记下预览时各目标文件的写入时间，应用时据此识别期间被改过的文件
            for op in ops:
                op.base_mtime = store.mtime(op.file) or 0.0
            return _ok(
                {
                    "operations": [op.to_dict() for op in ops],
                    "skipped": skipped,
                    "fileCount": len(files),
                }
            )
        finally:
            self._consolidating.discard(lock_id)

    async def consolidate_apply(self) -> dict:
        body = await _body()
        _, key, store = self._scope_from_body(body)
        raw_ops = body.get("operations")
        existing = set(await asyncio.to_thread(store.list_files))
        ops = consolidate.operations_from_payload(raw_ops, existing)
        requested = consolidate.requested_files(raw_ops)
        if not ops and not requested:
            raise ApiError("没有可应用的操作")
        results = []
        # 没通过重新校验的条目（比如预览后文件已不在）也要让管理员看到
        for file in sorted(requested - {op.file for op in ops}):
            results.append(
                {"op": "", "file": file, "ok": False, "message": "未通过校验，已跳过"}
            )
        write_failed = False
        for op in consolidate.ordered(ops):
            if consolidate.is_stale(op, store.mtime(op.file)):
                ok, message = False, "预览之后这个文件被改过，已跳过；请重新整理"
            elif op.op == "write":
                report = await store.write(op.file, op.render())
                ok, message = report.ok, report.message
            elif write_failed:
                # 合并目标没写成就不能删源文件，否则内容就丢了
                ok, message = False, "有写入未完成，为避免丢内容已跳过删除"
            else:
                report = await store.delete(op.file)
                ok, message = report.ok, report.message
            if op.op == "write" and not ok:
                write_failed = True
            results.append({"op": op.op, "file": op.file, "ok": ok, "message": message})
        self._changed(key)
        done = sum(1 for item in results if item["ok"])
        return _ok(
            {"results": results},
            f"已应用 {done} / {len(results)} 项操作",
        )
