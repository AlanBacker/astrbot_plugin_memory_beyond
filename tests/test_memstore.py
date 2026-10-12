import asyncio
import time

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
    saved = memstore.parse_memory(run(store.read("fact.md")))
    assert saved.body == "正文。" and saved.description == "项目截止日是 2026-08-15"
    # 写入时自动补上 created / updated 戳
    assert saved.created and saved.updated == saved.created
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


# ---------------------------------------------------------------- 局部编辑


def test_edit_replaces_once_and_syncs_index(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "旧钩子", "喜欢周六开黑。")))
    report = run(store.edit("a.md", "旧钩子", "新钩子"))
    assert report.ok and "替换 1 处" in report.message
    assert "description: 新钩子" in run(store.read("a.md"))
    assert run(store.read(INDEX_FILENAME)) == "- [a](a.md) — 新钩子\n"
    # 正文局部改动，frontmatter 与其余正文原样
    assert run(store.edit("a.md", "周六", "周日")).ok
    doc = memstore.parse_memory(run(store.read("a.md")))
    assert doc.description == "新钩子" and doc.body == "喜欢周日开黑。"


def test_edit_requires_unique_match_unless_replace_all(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "钩", "甲 乙 甲")))
    report = run(store.edit("a.md", "甲", "丙"))
    assert not report.ok and "2 处" in report.message
    assert "甲 乙 甲" in run(store.read("a.md"))
    report = run(store.edit("a.md", "甲", "丙", replace_all=True))
    assert report.ok and "替换 2 处" in report.message
    assert "丙 乙 丙" in run(store.read("a.md"))


def test_edit_rejects_bad_input(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "钩", "正文")))
    whole = run(store.read("a.md"))
    assert "没有找到" in run(store.edit("a.md", "不存在的片段", "x")).message
    assert not run(store.edit("a.md", "", "x")).ok
    assert not run(store.edit("a.md", "正文", "正文")).ok
    assert "memory_write" in run(store.edit("nope.md", "a", "b")).message
    assert not run(store.edit("../a.md", "a", "b")).ok
    assert not run(store.edit(INDEX_FILENAME, "a", "b")).ok
    # 改完文件就空了：拒绝，提示走删除
    report = run(store.edit("a.md", whole, ""))
    assert not report.ok and "delete=true" in report.message
    assert run(store.read("a.md")) == whole


# ---------------------------------------------------------------- 时间与来源戳


def _ts(text: str) -> float:
    return time.mktime(time.strptime(text, "%Y-%m-%d %H:%M"))


def test_write_stamps_created_updated_and_source(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "钩"), source="111"))
    doc = memstore.parse_memory(run(store.read("a.md")))
    assert memstore.parse_stamp(doc.created) is not None
    assert doc.updated == doc.created and doc.source == "111"
    # 改写：created 保留，内容里没写的戳从旧文件继承，再并入新来源
    run(store.write("a.md", memory_md("a", "新钩"), source="222"))
    doc2 = memstore.parse_memory(run(store.read("a.md")))
    assert doc2.created == doc.created and doc2.source == "111, 222"
    assert doc2.description == "新钩"
    # 局部编辑同样补戳
    run(store.edit("a.md", "正文。", "正文二。", source="333"))
    doc3 = memstore.parse_memory(run(store.read("a.md")))
    assert doc3.created == doc.created and doc3.source == "111, 222, 333"
    assert doc3.body == "正文二。"


def test_stamp_memory_keeps_everything_else():
    now = _ts("2026-10-11 18:30")
    raw = (
        "---\nname: a\ndescription: >-\n  两行\n  描述\nmetadata:\n  type: user\n"
        "  note: 保留\n\ntags: x\n---\n\n正文 [[b]]\n"
    )
    out = memstore.stamp_memory(raw, now, "111")
    assert out == (
        "---\nname: a\ndescription: >-\n  两行\n  描述\nmetadata:\n  type: user\n"
        "  note: 保留\n  created: 2026-10-11 18:30\n  updated: 2026-10-11 18:30\n"
        "  source: 111\n\ntags: x\n---\n\n正文 [[b]]\n"
    )
    # 再盖一次：同名戳行替换而不是重复；created 不变、updated 更新、来源并入
    again = memstore.stamp_memory(out, now + 3600, "222", previous=out)
    assert again.count("created:") == 1 and again.count("updated:") == 1
    assert "  created: 2026-10-11 18:30\n  updated: 2026-10-11 19:30\n  source: 111, 222\n" in again
    # 没有 metadata 块就补一个；没有 frontmatter 原样返回
    assert "metadata:\n  created:" in memstore.stamp_memory("---\nname: a\n---\n正文\n", now)
    assert memstore.stamp_memory("# 无 frontmatter\n", now) == "# 无 frontmatter\n"
    # 旧文件的 created 优先于内容里写的；写坏的戳不采用
    bad = raw.replace("  type: user\n", "  type: user\n  created: 昨天\n")
    newer = memstore.stamp_memory(bad, now + 3600, previous=out)
    assert "  created: 2026-10-11 18:30\n" in newer and "昨天" not in newer
    assert "  created: 2026-10-11 19:30\n" in memstore.stamp_memory(bad, now + 3600)


def test_parse_stamp_and_sources():
    assert memstore.parse_stamp("2026-10-11 18:30") == _ts("2026-10-11 18:30")
    assert memstore.parse_stamp("2026-10-11") is not None
    assert memstore.parse_stamp("昨天") is None and memstore.parse_stamp("") is None
    assert memstore.split_sources("111, 222，333 111") == ["111", "222", "333"]
    assert memstore.merge_sources("1, 2, 3, 4, 5", "6") == "2, 3, 4, 5, 6"
    assert memstore.merge_sources("", "") == ""


def test_headers_use_updated_stamp(tmp_path):
    store = ScopeStore(tmp_path)
    old = (
        "---\nname: a\ndescription: 钩\nmetadata:\n  type: user\n"
        "  created: 2026-01-01 08:00\n  updated: 2026-01-02 09:00\n  source: 111\n---\n\n正文\n"
    )
    (tmp_path / "a.md").write_text(old, encoding="utf-8")
    (tmp_path / "b.md").write_text(memory_md("b", "钩"), encoding="utf-8")
    heads = {h.file: h for h in store.headers()}
    assert heads["a.md"].updated == _ts("2026-01-02 09:00")
    assert heads["a.md"].created == _ts("2026-01-01 08:00")
    assert heads["a.md"].source == "111"
    assert heads["b.md"].updated == heads["b.md"].mtime and heads["b.md"].created == 0.0


def test_compose_memory_with_stamps():
    text = memstore.compose_memory("a", "d", "user", "正文", created="2026-01-01 08:00", source="111")
    assert "metadata:\n  type: user\n  created: 2026-01-01 08:00\n  source: 111\n---" in text
    assert "updated" not in text


def test_edit_accepts_crlf_fragment_and_guards_size(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "钩", "第一行\n第二行")))
    report = run(store.edit("a.md", "第一行\r\n第二行", "合并行"))
    assert report.ok and "合并行" in run(store.read("a.md"))
    report = run(store.edit("a.md", "合并行", "x" * memstore.MAX_FILE_BYTES))
    assert not report.ok and "上限" in report.message
    assert "合并行" in run(store.read("a.md"))


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


def test_people_scope_per_platform(tmp_path):
    store = MemoryStore(tmp_path)
    assert store.people_keys() == []
    p = store.people_scope("aiocqhttp")
    run(p.write("user-1.md", "x"))
    assert (tmp_path / "people" / "aiocqhttp" / "user-1.md").is_file()
    assert store.people_keys() == ["aiocqhttp"]
    assert store.people_scope_by_key("aiocqhttp") is p
    assert store.people_scope_by_key("../x") is None
    assert store.people_scope("") is store.people_scope("unknown")
    assert store.people_scope("a") is not store.session_scope("a")
    assert memstore.is_person_file("user-12345678.md")
    assert not memstore.is_person_file("user-ajie.md")
    assert not memstore.is_person_file("project-x.md")
    assert not memstore.is_person_file("")


# ---------------------------------------------------------------- 使用统计与遗忘

DAY = 86400.0

FEEDBACK_MD = (
    "---\nname: rule\ndescription: 指导\nmetadata:\n  type: feedback\n---\n\n别这样。\n"
)


def test_note_used_records_usage_and_updates_headers(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "钩子A")))
    run(store.write("b.md", memory_md("b", "钩子B")))
    assert {h.file: h.uses for h in store.headers()} == {"a.md": 0, "b.md": 0}
    store.note_used(["a.md", "a.md", "MEMORY.md", "../x.md"], now=1000.0)
    store.note_used(["a.md"], now=2000.0)
    by_file = {h.file: h for h in store.headers()}
    assert by_file["a.md"].uses == 2 and by_file["a.md"].last_used == 2000.0
    assert by_file["b.md"].uses == 0 and by_file["b.md"].last_used == 0.0
    assert by_file["a.md"].idle_since() == 2000.0
    assert by_file["b.md"].idle_since() == by_file["b.md"].updated
    assert store.usage_of("a.md").uses == 2
    assert set(store.usage()) == {"a.md"}
    assert (tmp_path / memstore.USAGE_FILENAME).is_file()
    # 旁存文件不是记忆：不进清单、不进索引体检
    assert store.list_files() == ["a.md", "b.md"]
    assert store.index_report().missing == []
    # 彻底删除后统计也不留
    run(store.delete("a.md"))
    assert store.usage() == {}
    assert not (tmp_path / memstore.USAGE_FILENAME).exists()


def test_forget_stale_moves_unused_files_and_keeps_feedback(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("old.md", memory_md("old", "很久没用")))
    run(store.write("fresh.md", memory_md("fresh", "刚用过")))
    run(store.write("rule.md", FEEDBACK_MD))
    now = time.time()
    # 从没用过的按最近修改算：刚写入，不到期
    assert run(store.forget_stale(30 * DAY, now)) == []
    far = now + 100 * DAY
    store.note_used(["fresh.md"], now=far - DAY)
    assert run(store.forget_stale(30 * DAY, far)) == ["old.md"]
    assert store.list_files() == ["fresh.md", "rule.md"]
    assert store.forgotten_files() == ["old.md"]
    assert store.has_forgotten("old.md") and not store.exists("old.md")
    index = (tmp_path / INDEX_FILENAME).read_text("utf-8")
    assert "](old.md)" not in index and "](fresh.md)" in index
    assert [h.file for h in store.headers()] == ["fresh.md", "rule.md"]
    forgotten = store.forgotten_headers()
    assert [h.file for h in forgotten] == ["old.md"]
    assert abs(forgotten[0].forgotten_at - far) < 0.002 and forgotten[0].description == "很久没用"
    assert run(store.read("old.md")) is None
    assert "很久没用" in run(store.read_forgotten("old.md"))
    assert run(store.search("很久")) == []
    assert run(store.search_forgotten("很久")) == ["old.md: description: 很久没用"]
    # 再扫一次不会重复，也不会动已遗忘的
    assert run(store.forget_stale(30 * DAY, far)) == []


def test_restore_and_delete_forgotten(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("old.md", memory_md("old", "钩子")))
    assert not run(store.forget("MEMORY.md")).ok
    assert not run(store.forget("nope.md")).ok
    assert run(store.forget("old.md", now=5.0)).ok
    assert store.forgotten_files() == ["old.md"]
    assert store.usage_of("old.md").forgotten_at == 5.0
    assert not run(store.restore("nope.md")).ok
    report = run(store.restore("old.md", now=9.0))
    assert report.ok and "索引行已同步" in report.message
    assert store.list_files() == ["old.md"] and store.forgotten_files() == []
    assert not (tmp_path / memstore.FORGOTTEN_DIRNAME).exists()
    assert "](old.md)" in (tmp_path / INDEX_FILENAME).read_text("utf-8")
    used = store.usage_of("old.md")
    assert used.last_used == 9.0 and used.uses == 1 and used.forgotten_at == 0.0
    # 同名在册时不能恢复
    assert run(store.forget("old.md", now=10.0)).ok
    (tmp_path / "old.md").write_text(memory_md("old", "手工放回"), "utf-8")
    assert "仍在册" in run(store.restore("old.md")).message
    assert run(store.delete_forgotten("old.md")).ok
    assert store.forgotten_files() == [] and store.usage_of("old.md").forgotten_at == 0.0
    assert not run(store.delete_forgotten("old.md")).ok


def test_write_over_forgotten_name_supersedes_copy(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("p.md", memory_md("p", "第一版")))
    created = memstore.parse_memory(run(store.read("p.md"))).created
    assert run(store.forget("p.md")).ok
    run(store.write("p.md", memory_md("p", "第二版")))
    assert store.forgotten_files() == [] and store.usage_of("p.md").forgotten_at == 0.0
    doc = memstore.parse_memory(run(store.read("p.md")))
    assert doc.created == created and doc.description == "第二版"
    assert not (tmp_path / memstore.FORGOTTEN_DIRNAME).exists()


def test_purge_removes_forgotten_and_usage(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory_md("a", "A")))
    run(store.write("b.md", memory_md("b", "B")))
    store.note_used(["a.md"])
    assert run(store.forget("b.md")).ok
    assert run(store.purge()) == 1
    assert not tmp_path.exists()
    assert store.forgotten_files() == [] and store.usage() == {}
