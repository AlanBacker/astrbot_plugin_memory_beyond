import asyncio

from core import memstore
from core.memstore import (
    INDEX_FILENAME,
    INDEX_MAX_LINES,
    MemoryStore,
    ScopeStore,
    _truncate_index,
    safe_key,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- safe_key


def test_safe_key_keeps_safe_names():
    assert safe_key("aiocqhttp_12345") == "aiocqhttp_12345"


def test_safe_key_hashes_unsafe_names():
    key = safe_key("qq:group/123 456")
    assert "/" not in key and ":" not in key and " " not in key
    assert safe_key("qq:group/123 456") == key  # 稳定
    assert safe_key("qq:group/123-456") != key  # 不同输入不碰撞


# ---------------------------------------------------------------- 路径约束


def test_resolve_rejects_traversal(tmp_path):
    store = ScopeStore(tmp_path)
    assert store._resolve("../evil.md") is None
    assert store._resolve("sub/evil.md") is None
    assert store._resolve("no-suffix") is None
    assert store._resolve(".hidden.md") is None
    assert store._resolve("") is None
    assert store._resolve("note.md") is not None


# ---------------------------------------------------------------- 读写删


def test_write_read_delete_roundtrip(tmp_path):
    store = ScopeStore(tmp_path)
    report = run(store.write("fact.md", "# hello"))
    assert report.ok
    assert run(store.read("fact.md")) == "# hello"
    assert store.list_files() == ["fact.md"]

    report = run(store.delete("fact.md"))
    assert report.ok
    assert run(store.read("fact.md")) is None
    assert store.list_files() == []


def test_delete_index_refused(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write(INDEX_FILENAME, "- [a](a.md) — x"))
    report = run(store.delete(INDEX_FILENAME))
    assert not report.ok


def test_write_rejects_oversize(tmp_path):
    store = ScopeStore(tmp_path)
    report = run(store.write("big.md", "x" * (memstore.MAX_FILE_BYTES + 1)))
    assert not report.ok


def test_delete_missing_file(tmp_path):
    store = ScopeStore(tmp_path)
    assert not run(store.delete("nope.md")).ok


# ---------------------------------------------------------------- 索引守门


def test_index_gate_over_limit_writes_but_errors(tmp_path):
    store = ScopeStore(tmp_path)
    content = "\n".join(f"- line {i}" for i in range(INDEX_MAX_LINES + 10))
    report = run(store.write(INDEX_FILENAME, content))
    assert not report.ok  # 报错要求精简
    assert run(store.read(INDEX_FILENAME)) == content  # 但写入成功


def test_index_gate_warns_near_limit(tmp_path):
    store = ScopeStore(tmp_path)
    content = "\n".join(f"- line {i}" for i in range(INDEX_MAX_LINES - 10))
    report = run(store.write(INDEX_FILENAME, content))
    assert report.ok
    assert "接近上限" in report.message


# ---------------------------------------------------------------- 索引截断


def test_truncate_index_appends_notice():
    raw = "\n".join(f"- line {i}" for i in range(INDEX_MAX_LINES + 50))
    snap = _truncate_index(raw)
    assert snap.truncated
    assert snap.loaded_lines == INDEX_MAX_LINES
    assert "50 行" in snap.text
    assert "memory_search" in snap.text


def test_truncate_index_small_untouched():
    snap = _truncate_index("- a\n- b")
    assert not snap.truncated
    assert snap.text == "- a\n- b"


def test_load_index_missing(tmp_path):
    snap = run(ScopeStore(tmp_path).load_index())
    assert snap.text == "" and snap.total_lines == 0


def test_append_index_line(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.append_index_line("- [a](a.md) — first"))
    run(store.append_index_line("- [b](b.md) — second"))
    content = run(store.read(INDEX_FILENAME))
    assert content == "- [a](a.md) — first\n- [b](b.md) — second\n"


# ---------------------------------------------------------------- 搜索


def test_search_multi_term_and(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("alpha.md", "user likes concise Python answers"))
    run(store.write("beta.md", "project deadline is 2026-08-15"))

    hits = run(store.search("python concise"))
    assert len(hits) == 1 and hits[0].startswith("alpha.md")
    assert run(store.search("python deadline")) == []
    assert run(store.search("")) == []


# ---------------------------------------------------------------- 双作用域


def test_memory_store_scopes_isolated(tmp_path):
    store = MemoryStore(tmp_path)
    g = store.global_scope("qq:1001")
    s = store.session_scope("qq:group:2002")
    run(g.write("me.md", "global fact"))
    run(s.write("me.md", "session fact"))
    assert run(g.read("me.md")) == "global fact"
    assert run(s.read("me.md")) == "session fact"
    assert store.global_scope("qq:1001") is g  # 按键缓存
