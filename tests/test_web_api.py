"""记忆管理页接口：用最小的 AstrBot 替身驱动 web_api，真实读写临时目录。"""

import asyncio
import importlib
import importlib.machinery
import importlib.util
import json
import os
import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "mb_plugin_under_test"


class FakeRequest:
    def __init__(self):
        self.query: dict = {}
        self.body = None

    async def json(self, default=None):
        return default if self.body is None else self.body


REQUEST = FakeRequest()


def _load_web_api():
    # web_api 用相对导入（from .core import …），这里把仓库根挂成一个包
    if "astrbot.api.web" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        api.logger = logging.getLogger("memory_beyond_test")
        web = types.ModuleType("astrbot.api.web")
        web.request = REQUEST
        astrbot.api = api
        api.web = web
        sys.modules.setdefault("astrbot", astrbot)
        sys.modules.setdefault("astrbot.api", api)
        sys.modules.setdefault("astrbot.api.web", web)
    if PACKAGE not in sys.modules:
        spec = importlib.machinery.ModuleSpec(PACKAGE, None, is_package=True)
        package = importlib.util.module_from_spec(spec)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.web_api")


web_api = _load_web_api()
pkg_archive = importlib.import_module(f"{PACKAGE}.core.archive")
pkg_compress = importlib.import_module(f"{PACKAGE}.core.compress")
pkg_memstore = importlib.import_module(f"{PACKAGE}.core.memstore")
pkg_registry = importlib.import_module(f"{PACKAGE}.core.registry")

UMO = "aiocqhttp:GroupMessage:100200"
KEY = pkg_memstore.safe_key(UMO)


class FakeContext:
    def __init__(self):
        self.registered_web_apis = []

    def register_web_api(self, route, view, methods, desc):
        self.registered_web_apis = [
            api
            for api in self.registered_web_apis
            if not (api[0] == route and api[2] == methods)
        ]
        self.registered_web_apis.append((route, view, methods, desc))


class FakeRuntime:
    def __init__(self, state):
        self.state = state


class FakePlugin:
    def __init__(self, base: Path):
        self.context = FakeContext()
        self._store = pkg_memstore.MemoryStore(base / "memories")
        self._registry = pkg_registry.SessionRegistry(base / "sessions.json")
        self._archive = pkg_archive.ArchiveStore(base / "archive")
        self._sessions: dict = {}
        self._state_dir = base / "state"
        self.config = {
            "enable_memory": True,
            "enable_global_memory": False,
            "enable_people_memory": False,
        }
        self.invalidated: list = []
        self.prompts: list = []
        self.summarizer_reply = ""

    def _global_on(self):
        return self.config["enable_global_memory"]

    def _people_on(self):
        return self.config["enable_people_memory"]

    def _bool(self, key, default):
        return self.config.get(key, default)

    def runtime_for_key(self, key):
        for umo, rt in self._sessions.items():
            if pkg_memstore.safe_key(umo) == key:
                return umo, rt
        return None

    def invalidate_for_key(self, key):
        self.invalidated.append(key)

    def state_path_for_key(self, key):
        return self._state_dir / f"{key}.json"

    async def load_state_file(self, path):
        if path.is_file():
            return pkg_compress.SessionState.from_dict(json.loads(path.read_text("utf-8")))
        return pkg_compress.SessionState()

    async def save_state_file(self, path, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state.to_dict()), "utf-8")

    async def call_summarizer(self, prompt):
        self.prompts.append(prompt)
        if not self.summarizer_reply:
            return "", ["provider-x: 超时"]
        return self.summarizer_reply, []


@pytest.fixture
def plugin(tmp_path):
    return FakePlugin(tmp_path)


@pytest.fixture
def dash(plugin):
    return web_api.MemoryDashboard(plugin)


def call(dash, route, query=None, body=None):
    handlers = {path: handler for path, _, handler, _ in dash._routes()}
    REQUEST.query = {k: str(v) for k, v in (query or {}).items()}
    REQUEST.body = body
    return asyncio.run(dash._wrap(handlers[route])())


def ok(result):
    assert result["status"] == "ok", result
    return result["data"]


def err(result):
    assert result["status"] == "error", result
    return result["message"]


def session_store(plugin):
    return plugin._store.session_scope(UMO)


def memory(name, description, body="正文", mem_type="user"):
    return pkg_memstore.compose_memory(name, description, mem_type, body)


# ---------------------------------------------------------------- 注册


def test_register_replaces_and_unregister_purges(plugin, dash):
    plugin.context.registered_web_apis.append(("/other/x", None, ["GET"], ""))
    dash.register()
    dash.register()  # 插件重载：不重复登记
    ours = [a for a in plugin.context.registered_web_apis if a[0].startswith(web_api.ROUTE_PREFIX)]
    assert len(ours) == len(dash._routes())
    assert {a[0] for a in ours} >= {web_api.ROUTE_PREFIX + "overview", web_api.ROUTE_PREFIX + "file/save"}
    dash.unregister()
    assert plugin.context.registered_web_apis == [("/other/x", None, ["GET"], "")]


def test_register_skips_on_old_astrbot(plugin, dash):
    plugin.context = types.SimpleNamespace()
    dash.register()  # 没有 register_web_api：静默跳过
    dash.unregister()


# ---------------------------------------------------------------- 查询


def test_overview_merges_memory_dirs_and_registry(plugin, dash):
    asyncio.run(session_store(plugin).write("user-1.md", memory("user-1", "阿杰")))
    plugin._registry.touch("tg_FriendMessage_42", "tg:FriendMessage:42", "小鱼")
    data = ok(call(dash, "overview"))
    assert data["memoryEnabled"] is True
    assert data["global"] == {"fileCount": 0, "updatedAt": 0.0, "enabled": False}
    sessions = {s["key"]: s for s in data["sessions"]}
    assert sessions[KEY]["fileCount"] == 1
    assert sessions[KEY]["kind"] == "group"  # 没登记过的目录从目录名推断
    assert sessions[KEY]["target"] == "100200"
    assert sessions["tg_FriendMessage_42"]["name"] == "小鱼"
    assert sessions["tg_FriendMessage_42"]["fileCount"] == 0


def test_scope_detail_and_file(plugin, dash):
    asyncio.run(session_store(plugin).write("user-1.md", memory("user-1", "阿杰", "见 [[user-2]]")))
    data = ok(call(dash, "scope", {"scope": "session", "key": KEY}))
    assert [f["file"] for f in data["files"]] == ["user-1.md"]
    assert data["index"]["lines"] == 1 and data["index"]["missing"] == []
    assert data["index"]["maxLines"] == pkg_memstore.INDEX_MAX_LINES
    assert data["session"]["key"] == KEY
    assert data["archiveCount"] == 0

    detail = ok(call(dash, "file", {"scope": "session", "key": KEY, "file": "user-1"}))
    assert detail["file"] == "user-1.md"
    assert (detail["description"], detail["type"], detail["body"]) == ("阿杰", "user", "见 [[user-2]]")
    assert detail["links"] == ["user-2"]
    assert detail["mtime"] > 0
    assert "不存在" in err(call(dash, "file", {"scope": "session", "key": KEY, "file": "nope.md"}))

    g = ok(call(dash, "scope", {"scope": "global"}))
    assert g["files"] == [] and g["enabled"] is False and g["key"] is None


@pytest.mark.parametrize(
    "query, message",
    [
        ({"scope": "session", "key": "../escape"}, "会话标识不合法"),
        ({"scope": "session", "key": ""}, "会话标识不合法"),
        ({"scope": "elsewhere", "key": KEY}, "作用域只能是"),
    ],
)
def test_scope_rejects_bad_locators(dash, query, message):
    assert message in err(call(dash, "scope", query))


@pytest.mark.parametrize("name", ["MEMORY.md", "MEMORY", "../x.md", "a/b.md", ".hidden.md"])
def test_file_rejects_bad_names(dash, name):
    assert err(call(dash, "file", {"scope": "session", "key": KEY, "file": name}))


def test_search(plugin, dash):
    asyncio.run(session_store(plugin).write("a.md", memory("a", "开黑", "每周六晚上开黑")))
    data = ok(call(dash, "search", {"scope": "session", "key": KEY, "q": "周六 开黑"}))
    assert [r["file"] for r in data["results"]] == ["a.md"]
    assert data["results"][0]["excerpt"]
    assert ok(call(dash, "search", {"scope": "session", "key": KEY, "q": " "}))["results"] == []


# ---------------------------------------------------------------- 编辑


def save(dash, **fields):
    body = {"scope": "session", "key": KEY, "mode": "fields", "type": "user"}
    body.update(fields)
    return call(dash, "file/save", body=body)


def test_file_save_create_edit_rename(plugin, dash):
    store = session_store(plugin)
    data = ok(save(dash, file="user-1", description=" 阿杰\n 群主 ", body="\n正文\n"))
    assert data["file"] == "user-1.md"
    assert data["message"].startswith("已写入")
    assert plugin.invalidated == [KEY]
    doc = pkg_memstore.parse_memory(asyncio.run(store.read("user-1.md")))
    assert (doc.name, doc.description, doc.body) == ("user-1", "阿杰 群主", "正文")

    # 新建同名文件被拒
    assert "已存在" in err(save(dash, file="user-1.md", description="d", body="b"))

    # 编辑：带上打开时的写入时间
    base = store.mtime("user-1.md")
    ok(save(dash, file="user-1.md", oldFile="user-1.md", description="新描述", body="b", baseMtime=base))
    # 期间被改过（时间对不上）就拒绝
    assert "被修改过" in err(
        save(dash, file="user-1.md", oldFile="user-1.md", description="x", body="b", baseMtime=base - 100)
    )

    # 改名：先写新文件再删旧文件，索引跟着变
    base = store.mtime("user-1.md")
    ok(save(dash, file="user-2.md", oldFile="user-1.md", description="改名", body="b", baseMtime=base))
    assert store.list_files() == ["user-2.md"]
    report = store.index_report()
    assert report.missing == [] and report.dangling == []
    # 改名的目标已存在同样被拒
    asyncio.run(store.write("user-3.md", memory("user-3", "d")))
    assert "已存在" in err(save(dash, file="user-3.md", oldFile="user-2.md", description="d", body="b"))
    # 原文件已被删掉
    assert "已不存在" in err(save(dash, file="x.md", oldFile="gone.md", description="d", body="b"))


def test_file_save_raw_mode(plugin, dash):
    content = "---\nname: a\ndescription: 原文模式\nmetadata:\n  type: project\n---\n\n正文\n"
    ok(call(dash, "file/save", body={"scope": "session", "key": KEY, "file": "a.md", "mode": "raw", "content": content}))
    saved = asyncio.run(session_store(plugin).read("a.md"))
    # 原文照存，只在 metadata 下补了戳
    assert saved.startswith("---\nname: a\ndescription: 原文模式\nmetadata:\n  type: project\n  created: ")
    assert saved.endswith("\n---\n\n正文\n")
    assert "不能为空" in err(
        call(dash, "file/save", body={"scope": "session", "key": KEY, "file": "b.md", "mode": "raw", "content": "  "})
    )


def test_file_save_validates_fields(dash):
    assert "类型" in err(save(dash, file="a.md", type="secret", description="d", body="b"))
    assert "描述" in err(save(dash, file="a.md", description="  ", body="b"))
    assert "MEMORY.md" in err(save(dash, file="MEMORY.md", description="d", body="b"))


def test_global_scope_edits_invalidate_all(plugin, dash):
    body = {"scope": "global", "mode": "fields", "file": "style.md", "type": "feedback", "description": "简洁", "body": "b"}
    ok(call(dash, "file/save", body=body))
    assert plugin.invalidated == [None]
    assert plugin._store.global_scope().list_files() == ["style.md"]


def test_file_delete(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("a.md", memory("a", "d")))
    ok(call(dash, "file/delete", body={"scope": "session", "key": KEY, "file": "a.md"}))
    assert store.list_files() == []
    assert err(call(dash, "file/delete", body={"scope": "session", "key": KEY, "file": "a.md"}))


def test_index_save_and_rebuild(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("a.md", memory("a", "A")))
    data = ok(call(dash, "index/save", body={"scope": "session", "key": KEY, "content": "## 分组\n"}))
    assert data["message"].startswith("索引已保存")
    assert store.index_report().missing == ["a.md"]
    data = ok(call(dash, "index/rebuild", body={"scope": "session", "key": KEY}))
    assert data["count"] == 1
    report = store.index_report()
    assert report.missing == [] and report.lines == 2


# ---------------------------------------------------------------- 会话


def test_session_state_reads_disk_or_live(plugin, dash):
    state = pkg_compress.SessionState(cid="c", watermark=12, summary="旧摘要", last_parts={"history": 5})
    asyncio.run(plugin.save_state_file(plugin.state_path_for_key(KEY), state))
    data = ok(call(dash, "session/state", {"key": KEY}))
    assert data["live"] is False
    assert data["watermark"] == 12 and data["summary"] == "旧摘要"
    assert data["lastParts"]["history"] == 5
    assert data["now"] > 0

    plugin._sessions[UMO] = FakeRuntime(pkg_compress.SessionState(cid="c", watermark=30))
    data = ok(call(dash, "session/state", {"key": KEY}))
    assert data["live"] is True and data["watermark"] == 30


def test_session_reset(plugin, dash):
    assert "还没有压缩状态" in ok(call(dash, "session/reset", body={"key": KEY}))["message"]
    assert not plugin.state_path_for_key(KEY).exists()  # 不凭空创建状态文件

    path = plugin.state_path_for_key(KEY)
    state = pkg_compress.SessionState(cid="c", watermark=12, summary="s", fail_count=3, fail_until=9e12)
    asyncio.run(plugin.save_state_file(path, state))
    ok(call(dash, "session/reset", body={"key": KEY}))
    saved = json.loads(path.read_text("utf-8"))
    assert saved["watermark"] == 0 and saved["summary"] == ""
    assert saved["fail_count"] == 0
    assert plugin.invalidated == [KEY]


def test_session_remark(plugin, dash):
    data = ok(call(dash, "session/remark", body={"key": KEY, "remark": "  开黑群 " + "x" * 100}))
    assert data["remark"].startswith("开黑群")
    assert len(data["remark"]) <= web_api.MAX_REMARK
    assert plugin._registry.get(KEY).remark == data["remark"]


def test_session_purge_memories_then_all(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("a.md", memory("a", "d")))
    plugin._registry.touch(KEY, UMO, "开黑群")
    plugin._archive.save(KEY, umo=UMO, cid="c", messages=[{"role": "user", "content": "hi"}], summary="s", keep=3)
    path = plugin.state_path_for_key(KEY)
    asyncio.run(plugin.save_state_file(path, pkg_compress.SessionState(cid="c")))
    plugin._sessions[UMO] = FakeRuntime(pkg_compress.SessionState())

    assert ok(call(dash, "session/purge", body={"key": KEY}))["removed"] == 1
    assert store.list_files() == []
    assert plugin._registry.get(KEY) is not None  # 只删记忆

    ok(call(dash, "session/purge", body={"key": KEY, "target": "all"}))
    assert plugin._registry.get(KEY) is None
    assert plugin._archive.count(KEY) == 0
    assert not path.exists()
    assert UMO not in plugin._sessions


def test_archives(plugin, dash):
    archive_id = plugin._archive.save(
        KEY, umo=UMO, cid="c", messages=[{"role": "user", "content": "hi"}], summary="s", keep=3
    )
    listed = ok(call(dash, "archives", {"key": KEY}))["archives"]
    assert [a["id"] for a in listed] == [archive_id]
    detail = ok(call(dash, "archive", {"key": KEY, "id": archive_id}))
    assert detail["messages"] == [{"role": "user", "text": "hi"}]
    assert "归档不存在" in err(call(dash, "archive", {"key": KEY, "id": "../x"}))


# ---------------------------------------------------------------- 整理


def test_consolidate_preview_and_apply(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("user-1.md", memory("user-1", "阿杰")))
    asyncio.run(store.write("user-1-old.md", memory("user-1-old", "阿杰旧档")))
    plugin._registry.touch(KEY, UMO, "开黑群")
    plugin.summarizer_reply = "<operations>" + json.dumps(
        [
            {"op": "write", "file": "user-1.md", "type": "user", "description": "阿杰（合并）", "content": "合并后正文", "reason": "合并"},
            {"op": "delete", "file": "user-1-old.md", "reason": "已合并"},
            {"op": "delete", "file": "ghost.md"},
        ],
        ensure_ascii=False,
    ) + "</operations>"
    data = ok(call(dash, "consolidate/preview", body={"scope": "session", "key": KEY}))
    assert [op["file"] for op in data["operations"]] == ["user-1.md", "user-1-old.md"]
    assert data["fileCount"] == 2 and data["skipped"] == []
    assert data["files"] == ["user-1-old.md", "user-1.md"]
    assert "「开黑群」" in plugin.prompts[0]

    result = ok(
        call(dash, "consolidate/apply", body={"scope": "session", "key": KEY, "operations": data["operations"]})
    )
    assert [r["ok"] for r in result["results"]] == [True, True]
    assert result["message"] == "已应用 2 / 2 项操作"
    assert store.list_files() == ["user-1.md"]
    doc = pkg_memstore.parse_memory(asyncio.run(store.read("user-1.md")))
    assert (doc.description, doc.body) == ("阿杰（合并）", "合并后正文")
    assert store.index_report().dangling == []


def test_consolidate_errors(plugin, dash):
    body = {"scope": "session", "key": KEY}
    assert "还没有记忆" in err(call(dash, "consolidate/preview", body=body))
    asyncio.run(session_store(plugin).write("a.md", memory("a", "d")))
    assert "provider-x: 超时" in err(call(dash, "consolidate/preview", body=body))
    plugin.summarizer_reply = "我觉得挺好"
    assert "无法解析" in err(call(dash, "consolidate/preview", body=body))
    plugin.summarizer_reply = "<operations>[]</operations>"
    assert ok(call(dash, "consolidate/preview", body=body))["operations"] == []
    assert dash._consolidating == set()  # 每次都释放了占用
    assert "没有可应用" in err(call(dash, "consolidate/apply", body={**body, "operations": "x"}))


def test_consolidate_busy_guard(plugin, dash):
    asyncio.run(session_store(plugin).write("a.md", memory("a", "d")))
    dash._consolidating.add(f"session:{KEY}")
    assert "正在整理" in err(call(dash, "consolidate/preview", body={"scope": "session", "key": KEY}))


# ---------------------------------------------------------------- 兜底


def test_unexpected_errors_are_masked(plugin, dash):
    plugin._registry = None  # 让 overview 内部抛 AttributeError
    assert "服务器内部错误" in err(call(dash, "overview"))


def test_consolidate_apply_reports_skipped_and_stale(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("user-1.md", memory("user-1", "阿杰")))
    asyncio.run(store.write("user-1-old.md", memory("user-1-old", "阿杰旧档")))
    plugin.summarizer_reply = "<operations>" + json.dumps(
        [
            {"op": "write", "file": "user-1.md", "type": "user", "description": "合并", "content": "合并后正文", "reason": "合并"},
            {"op": "delete", "file": "user-1-old.md", "reason": "已合并"},
        ],
        ensure_ascii=False,
    ) + "</operations>"
    data = ok(call(dash, "consolidate/preview", body={"scope": "session", "key": KEY}))
    assert all(op["baseMtime"] > 0 for op in data["operations"])

    # 预览之后模型又改了合并目标：写入要跳过，源文件也不能删；没通过校验的也要报出来
    path = store.root / "user-1.md"
    later = path.stat().st_mtime + 5
    os.utime(path, (later, later))
    payload = data["operations"] + [{"op": "delete", "file": "ghost.md"}]
    result = ok(call(dash, "consolidate/apply", body={"scope": "session", "key": KEY, "operations": payload}))
    by_file = {r["file"]: r for r in result["results"]}
    assert "未通过校验" in by_file["ghost.md"]["message"]
    assert "被改过" in by_file["user-1.md"]["message"]
    assert "跳过删除" in by_file["user-1-old.md"]["message"]
    assert result["message"] == "已应用 0 / 3 项操作"
    assert set(store.list_files()) == {"user-1.md", "user-1-old.md"}
    assert "合并后正文" not in asyncio.run(store.read("user-1.md"))


def test_file_name_guard_is_case_insensitive(plugin, dash):
    asyncio.run(session_store(plugin).write("a.md", memory("a", "d")))
    msg = err(call(dash, "file", query={"scope": "session", "key": KEY, "file": "memory.md"}))
    assert "索引" in msg


def test_file_detail_and_listing_expose_stamps(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("s.md", memory("s", "d"), source="111"))
    detail = ok(call(dash, "file", {"scope": "session", "key": KEY, "file": "s.md"}))
    assert detail["source"] == "111" and detail["created"] and detail["updated"] == detail["created"]
    listing = ok(call(dash, "scope", {"scope": "session", "key": KEY}))
    item = next(f for f in listing["files"] if f["file"] == "s.md")
    assert item["source"] == "111" and item["created"] > 0 and item["updated"] > 0
    # 管理页改写：created 保留、source 继承
    ok(call(dash, "file/save", body={
        "scope": "session", "key": KEY, "file": "s.md", "oldFile": "s.md", "mode": "fields",
        "type": "user", "description": "d2", "body": "b2", "baseMtime": detail["mtime"],
    }))
    again = ok(call(dash, "file", {"scope": "session", "key": KEY, "file": "s.md"}))
    assert again["created"] == detail["created"] and again["source"] == "111"
    assert (again["description"], again["body"]) == ("d2", "b2")


def test_people_scope_routes(plugin, dash):
    assert ok(call(dash, "overview"))["people"] == {"enabled": False, "platforms": []}
    people = plugin._store.people_scope("aiocqhttp")
    asyncio.run(people.write("user-1.md", memory("user-1", "阿杰")))
    plugin.config["enable_people_memory"] = True
    data = ok(call(dash, "overview"))
    assert data["people"]["enabled"] is True
    assert data["people"]["platforms"][0]["key"] == "aiocqhttp"
    assert data["people"]["platforms"][0]["fileCount"] == 1
    detail = ok(call(dash, "scope", {"scope": "people", "key": "aiocqhttp"}))
    assert detail["enabled"] is True and detail["people"]["platform"] == "aiocqhttp"
    assert [f["file"] for f in detail["files"]] == ["user-1.md"]
    assert "不合法" in err(call(dash, "scope", {"scope": "people", "key": "../x"}))
    assert "people" in err(call(dash, "scope", {"scope": "nope", "key": ""}))
    base = {
        "scope": "people", "key": "aiocqhttp", "mode": "fields",
        "type": "user", "description": "d", "body": "b",
    }
    assert "user-<数字ID>" in err(call(dash, "file/save", body={**base, "file": "note.md"}))
    assert "只能是 user" in err(
        call(dash, "file/save", body={**base, "file": "user-2.md", "type": "project"})
    )
    ok(call(dash, "file/save", body={**base, "file": "user-2.md"}))
    assert set(people.list_files()) == {"user-1.md", "user-2.md"}
    plugin.config["enable_people_memory"] = False
    # 关闭后文件仍在、页面仍可看，只是标成未启用
    assert ok(call(dash, "scope", {"scope": "people", "key": "aiocqhttp"}))["enabled"] is False
    assert ok(call(dash, "overview"))["people"]["platforms"][0]["fileCount"] == 2
