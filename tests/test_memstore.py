import asyncio

from core import memstore
from core.memstore import (
    INDEX_FILENAME,
    INDEX_MAX_LINES,
    MAX_HOOK_CHARS,
    MemoryStore,
    ScopeStore,
    _truncate_index,
    format_index_line,
    parse_description,
    safe_key,
)


def run(coro):
    return asyncio.run(coro)


def memory_md(name: str, description: str, body: str = "正文。") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        "  type: project\n"
        "---\n\n"
        f"{body}\n"
    )


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


# ------------------------------------------------------------ 钩子提取与索引行


def test_parse_description_prefers_frontmatter():
    desc, from_fm = parse_description(memory_md("fact", "一句话钩子"))
    assert desc == "一句话钩子" and from_fm


def test_parse_description_strips_quotes():
    content = '---\nname: x\ndescription: "带引号的钩子"\n---\n正文\n'
    assert parse_description(content) == ("带引号的钩子", True)


def test_parse_description_falls_back_to_first_body_line():
    desc, from_fm = parse_description("# 标题行\n\n正文细节\n")
    assert desc == "标题行" and not from_fm


def test_parse_description_empty_content():
    assert parse_description("---\nname: x\n---\n") == ("", False)


def test_format_index_line_sanitizes_hook():
    line = format_index_line("fact.md", "有[方括号]\n和换行   多空白")
    assert line == "- [fact](fact.md) — 有［方括号］ 和换行 多空白"


def test_format_index_line_clips_and_defaults():
    long_line = format_index_line("a.md", "钩" * (MAX_HOOK_CHARS + 20))
    assert long_line.endswith("…")
    assert format_index_line("a.md", "  ") == "- [a](a.md) — （无描述）"


# ---------------------------------------------------------------- 读写删


def test_write_read_delete_syncs_index(tmp_path):
    store = ScopeStore(tmp_path)
    content = memory_md("fact", "项目截止日是 2026-08-15")
    report = run(store.write("fact.md", content))
    assert report.ok and "索引行已同步" in report.message
    assert run(store.read("fact.md")) == content
    assert store.list_files() == ["fact.md"]  # 不含 MEMORY.md
    assert (
        run(store.read(INDEX_FILENAME))
        == "- [fact](fact.md) — 项目截止日是 2026-08-15\n"
    )

    report = run(store.delete("fact.md"))
    assert report.ok and "移除" in report.message
    assert run(store.read("fact.md")) is None
    assert run(store.read(INDEX_FILENAME)) == ""


def test_write_without_frontmatter_nudges(tmp_path):
    store = ScopeStore(tmp_path)
    report = run(store.write("note.md", "# 只有正文\n细节\n"))
    assert report.ok
    assert "正文首行" in report.message  # 提醒补全 frontmatter
    assert run(store.read(INDEX_FILENAME)) == "- [note](note.md) — 只有正文\n"


def test_sync_updates_line_in_place(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "旧钩子")))
    run(store.write("b.md", memory_md("b", "乙")))
    run(store.write("a.md", memory_md("a", "新钩子")))
    assert run(store.read(INDEX_FILENAME)) == (
        "- [a](a.md) — 新钩子\n- [b](b.md) — 乙\n"
    )


def test_sync_preserves_hand_edited_lines(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "甲")))
    index_path = tmp_path / INDEX_FILENAME
    index_path.write_text(
        "# 分组标题\n- [a](a.md) — 甲\n", encoding="utf-8"
    )
    run(store.write("a.md", memory_md("a", "甲改")))
    assert index_path.read_text("utf-8") == "# 分组标题\n- [a](a.md) — 甲改\n"
    run(store.delete("a.md"))
    assert index_path.read_text("utf-8") == "# 分组标题\n"


def test_write_index_refused(tmp_path):
    store = ScopeStore(tmp_path)
    report = run(store.write(INDEX_FILENAME, "- 乱写"))
    assert not report.ok and "自动维护" in report.message
    assert run(store.read(INDEX_FILENAME)) is None  # 未落盘


def test_delete_index_refused(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "甲")))
    report = run(store.delete(INDEX_FILENAME))
    assert not report.ok
    assert run(store.read(INDEX_FILENAME)) is not None


def test_index_guard_ignores_padding_and_case(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "甲")))
    for name in ("/MEMORY.md", " MEMORY.md", "MEMORY.md ", "memory.md"):
        assert not run(store.write(name, "# 乱写")).ok, name
        assert not run(store.delete(name)).ok, name
    assert run(store.read(INDEX_FILENAME)) == "- [a](a.md) — 甲\n"


def test_write_normalizes_name_for_index_line(tmp_path):
    store = ScopeStore(tmp_path)
    assert run(store.write(" foo.md", memory_md("foo", "钩子"))).ok
    assert "](foo.md)" in run(store.read(INDEX_FILENAME))
    assert run(store.delete("foo.md")).ok
    assert "foo.md" not in run(store.read(INDEX_FILENAME))


def test_parse_frontmatter_block_scalars():
    content = (
        "---\nname: a\ndescription: >-\n  第一行钩子\n  第二行\n"
        "metadata:\n  type: user\nnote: |\n  多行\n  备注\n---\n正文"
    )
    assert memstore.parse_description(content) == ("第一行钩子 第二行", True)
    doc = memstore.parse_memory(content)
    assert (doc.description, doc.type, doc.body) == ("第一行钩子 第二行", "user", "正文")


def test_write_rejects_oversize(tmp_path):
    store = ScopeStore(tmp_path)
    report = run(store.write("big.md", "x" * (memstore.MAX_FILE_BYTES + 1)))
    assert not report.ok


def test_delete_missing_file(tmp_path):
    store = ScopeStore(tmp_path)
    assert not run(store.delete("nope.md")).ok


# ---------------------------------------------------------------- 索引体积守门


def test_index_health_warns_near_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(memstore, "INDEX_MAX_LINES", 10)
    store = ScopeStore(tmp_path)
    for i in range(9):
        report = run(store.write(f"m{i}.md", memory_md(f"m{i}", f"钩子{i}")))
    assert report.ok and "接近注入上限" in report.message


def test_index_health_over_limit_still_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(memstore, "INDEX_MAX_LINES", 10)
    store = ScopeStore(tmp_path)
    for i in range(11):
        report = run(store.write(f"m{i}.md", memory_md(f"m{i}", f"钩子{i}")))
    assert report.ok  # 写入本身成功，守门只是提醒
    assert "超出注入上限" in report.message and "合并" in report.message
    assert len(store.list_files()) == 11


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


# ---------------------------------------------------------------- 搜索


def test_search_multi_term_and(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("alpha.md", "user likes concise Python answers"))
    run(store.write("beta.md", "project deadline is 2026-08-15"))

    hits = run(store.search("python concise"))
    assert len(hits) == 1 and hits[0].startswith("alpha.md")
    assert run(store.search("python deadline")) == []
    assert run(store.search("")) == []


def test_search_excludes_index(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("empty.md", "---\nname: empty\n---\n"))
    # 钩子兜底文案只存在于 MEMORY.md，搜索不应命中索引文件
    assert "无描述" in run(store.read(INDEX_FILENAME))
    assert run(store.search("无描述")) == []


# ---------------------------------------------------------------- 双作用域


def test_memory_store_scopes_isolated(tmp_path):
    store = MemoryStore(tmp_path)
    g = store.global_scope()  # 机器人自我，全局唯一目录
    s = store.session_scope("qq:group:2002")
    run(g.write("me.md", "global fact"))
    run(s.write("me.md", "session fact"))
    assert run(g.read("me.md")) == "global fact"
    assert run(s.read("me.md")) == "session fact"
    assert store.global_scope() is g  # 按键缓存
    assert store.session_scope("qq:group:2002") is s


# ---------------------------------------------------------------- 文件格式


def test_parse_memory_reads_nested_and_top_level_type():
    doc = memstore.parse_memory(memory_md("x", "钩子", "正文\n"))
    assert (doc.name, doc.description, doc.type, doc.body) == ("x", "钩子", "project", "正文")
    assert doc.has_frontmatter
    top = memstore.parse_memory("---\nname: y\ntype: User\n---\nbody")
    assert top.type == "user"
    both = memstore.parse_memory(
        "---\ntype: user\nmetadata:\n  type: feedback\n---\nb"
    )
    assert both.type == "feedback"  # metadata 下的优先


def test_parse_memory_without_frontmatter():
    doc = memstore.parse_memory("只有正文")
    assert not doc.has_frontmatter
    assert doc.body == "只有正文"
    assert doc.name == doc.description == doc.type == ""


def test_compose_memory_roundtrip():
    text = memstore.compose_memory(" user-1 ", "阿杰\n  后端", "user", "\n正文\n\n")
    assert text == (
        "---\nname: user-1\ndescription: 阿杰 后端\nmetadata:\n  type: user\n---\n\n正文\n"
    )
    doc = memstore.parse_memory(text)
    assert (doc.name, doc.description, doc.type, doc.body) == ("user-1", "阿杰 后端", "user", "正文")


def test_extract_links():
    links = memstore.extract_links("见 [[user-1]] 与 [[ project-x ]]，重复 [[user-1]]，坏的 [[]]")
    assert links == ["user-1", "project-x"]


def test_is_valid_name():
    assert ScopeStore.is_valid_name("user-123.md")
    assert not ScopeStore.is_valid_name("user-123.txt")
    assert not ScopeStore.is_valid_name("../x.md")
    assert not ScopeStore.is_valid_name(".hidden.md")
    assert not ScopeStore.is_valid_name("")


# ---------------------------------------------------------------- 头信息与 mtime


def test_headers_and_cache_refresh(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "第一版", "见 [[b]]")))
    run(store.write("b.md", "没有 frontmatter 的首行\n正文"))
    headers = {h.file: h for h in store.headers()}
    assert set(headers) == {"a.md", "b.md"}  # 不含 MEMORY.md
    assert headers["a.md"].description == "第一版"
    assert headers["a.md"].type == "project"
    assert headers["a.md"].links == ["b"]
    assert headers["b.md"].name == "b"
    assert headers["b.md"].description  # 兜底取正文首行
    run(store.write("a.md", memory_md("a", "第二版更长一些")))
    assert {h.file: h for h in store.headers()}["a.md"].description == "第二版更长一些"
    run(store.delete("b.md"))
    assert [h.file for h in store.headers()] == ["a.md"]


def test_mtime(tmp_path):
    store = ScopeStore(tmp_path)
    assert store.mtime("a.md") is None
    run(store.write("a.md", memory_md("a", "d")))
    assert store.mtime("a.md") > 0
    assert store.mtime("../a.md") is None


# ---------------------------------------------------------------- 索引体检与管理员编辑


def test_index_report_detects_missing_and_dangling(tmp_path):
    store = ScopeStore(tmp_path)
    assert store.index_report().exists is False
    run(store.write("a.md", memory_md("a", "d")))
    (tmp_path / "orphan.md").write_text(memory_md("orphan", "d"), "utf-8")
    index = (tmp_path / INDEX_FILENAME).read_text("utf-8")
    (tmp_path / INDEX_FILENAME).write_text(index + "- [gone](gone.md) — 已删\n", "utf-8")
    report = store.index_report()
    assert report.exists and report.lines == 2
    assert report.missing == ["orphan.md"]
    assert report.dangling == ["gone.md"]
    assert report.truncated is False


def test_write_index_keeps_manual_layout(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "旧钩子")))
    report = run(store.write_index("## 群友\n\n- [a](a.md) — 手改\n\n"))
    assert report.ok
    assert (tmp_path / INDEX_FILENAME).read_text("utf-8") == "## 群友\n\n- [a](a.md) — 手改\n"
    # 之后写文件只改匹配行，标题保留
    run(store.write("a.md", memory_md("a", "新钩子")))
    text = (tmp_path / INDEX_FILENAME).read_text("utf-8")
    assert text.startswith("## 群友\n")
    assert "新钩子" in text and "手改" not in text


def test_write_index_rejects_oversize(tmp_path):
    store = ScopeStore(tmp_path)
    report = run(store.write_index("x" * (memstore.MAX_FILE_BYTES + 1)))
    assert not report.ok


def test_rebuild_index_repairs(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "A")))
    (tmp_path / "orphan.md").write_text(memory_md("orphan", "孤儿"), "utf-8")
    (tmp_path / INDEX_FILENAME).write_text(
        "## 标题\n- [a](a.md) — 过时\n- [gone](gone.md) — 已删\n- [a](a.md) — 重复\n",
        "utf-8",
    )
    assert run(store.rebuild_index()) == 2
    lines = (tmp_path / INDEX_FILENAME).read_text("utf-8").splitlines()
    assert lines[0] == "## 标题"
    assert len([line for line in lines if "(a.md)" in line]) == 1
    assert "gone.md" not in "\n".join(lines)
    assert "(orphan.md)" in lines[-1]
    report = store.index_report()
    assert report.missing == [] and report.dangling == []


def test_rebuild_index_empty_scope_removes_index(tmp_path):
    store = ScopeStore(tmp_path)
    (tmp_path / INDEX_FILENAME).write_text("- [gone](gone.md) — x\n", "utf-8")
    assert run(store.rebuild_index()) == 0
    assert not (tmp_path / INDEX_FILENAME).exists()


# ---------------------------------------------------------------- 会话目录


def test_session_keys_and_scope_by_key(tmp_path):
    store = MemoryStore(tmp_path)
    s = store.session_scope("qq:GroupMessage:100")
    run(s.write("a.md", "x"))
    key = safe_key("qq:GroupMessage:100")
    assert store.session_keys() == [key]
    assert store.session_scope_by_key(key) is s
    assert store.session_scope_by_key("../escape") is None
    assert store.session_scope_by_key("") is None
    assert memstore.is_scope_key(key)
    assert not memstore.is_scope_key("a/b")
