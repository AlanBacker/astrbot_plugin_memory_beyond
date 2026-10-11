import json

from core import consolidate
from core.memstore import MemoryHeader, parse_memory

NOW = 1_800_000_000.0


def header(file, mtime=NOW):
    return MemoryHeader(file=file, name=file[:-3], description="", type="user", mtime=mtime, size=0)


def wrap(ops):
    return f"<operations>{json.dumps(ops, ensure_ascii=False)}</operations>"


def test_build_prompt_includes_files_and_skips_over_budget():
    big = "x" * consolidate.MAX_INPUT_CHARS
    prompt, skipped = consolidate.build_prompt(
        "周末开黑群",
        [(header("a.md", NOW - 2 * 86400), "内容A"), (header("b.md"), big)],
        now=NOW,
    )
    assert "「周末开黑群」" in prompt
    assert "=== a.md（2 天前写入）===\n内容A" in prompt
    assert skipped == ["b.md"]


def test_build_prompt_empty():
    prompt, skipped = consolidate.build_prompt("x", [])
    assert "（空）" in prompt
    assert skipped == []


def test_parse_operations_validates_entries():
    existing = {"user-1.md", "user-2.md"}
    text = wrap(
        [
            {"op": "write", "file": "user-1", "type": "USER", "description": " 阿杰\n后端 ", "content": "合并后", "reason": "合并"},
            {"op": "delete", "file": "user-2.md", "reason": "已合并"},
            {"op": "delete", "file": "ghost.md"},  # 删除不存在的文件
            {"op": "write", "file": "MEMORY.md", "type": "user", "content": "x"},
            {"op": "write", "file": "../evil.md", "type": "user", "content": "x"},
            {"op": "write", "file": "bad-type.md", "type": "secret", "content": "x"},
            {"op": "write", "file": "empty.md", "type": "user", "content": "  "},
            {"op": "rename", "file": "user-1.md"},
            {"op": "delete", "file": "user-1.md"},  # 同一文件第二次出现
            "not a dict",
        ]
    )
    ops = consolidate.parse_operations(text, existing)
    assert [(op.op, op.file) for op in ops] == [("write", "user-1.md"), ("delete", "user-2.md")]
    assert ops[0].type == "user"
    assert ops[0].description == "阿杰 后端"
    assert ops[0].reason == "合并"


def test_parse_operations_unparseable_vs_empty():
    assert consolidate.parse_operations("我觉得都挺好", set()) is None
    assert consolidate.parse_operations('<operations>{"op": 1}</operations>', set()) is None
    assert consolidate.parse_operations("<operations>[]</operations>", set()) == []


def test_parse_operations_tolerates_fences_and_chatter():
    text = '好的，方案如下：\n```json\n[{"op": "delete", "file": "a.md"}]\n```'
    ops = consolidate.parse_operations(text, {"a.md"})
    assert [op.file for op in ops] == ["a.md"]


def test_parse_operations_caps_reason_and_rejects_oversize_write():
    ops = consolidate.parse_operations(
        wrap([
            {
                "op": "write",
                "file": "a.md",
                "type": "project",
                "content": "c" * 10,
                "reason": "r" * 500,
            },
            {
                "op": "write",
                "file": "b.md",
                "type": "project",
                "content": "c" * (consolidate.MAX_CONTENT_CHARS + 5),
            },
            {"op": "delete", "file": "memory.md"},
        ]),
        {"memory.md"},
    )
    # 超长正文整条丢弃而不是截断（截断后源文件照删会丢内容）；索引不分大小写
    assert [op.file for op in ops] == ["a.md"]
    assert len(ops[0].reason) == consolidate.MAX_REASON_CHARS


def test_base_mtime_roundtrip_and_staleness():
    op = consolidate.Operation(op="delete", file="b.md", base_mtime=1700000000.123)
    ops = consolidate.operations_from_payload([op.to_dict()], {"b.md"})
    assert ops[0].base_mtime == op.base_mtime
    assert not consolidate.is_stale(ops[0], 1700000000.1234)  # JSON 往返误差
    assert consolidate.is_stale(ops[0], 1700000005.0)  # 期间被改写
    assert consolidate.is_stale(ops[0], None)  # 文件没了
    new = consolidate.Operation(op="write", file="n.md")
    assert not consolidate.is_stale(new, None)
    assert consolidate.is_stale(new, 1.0)  # 要新建的文件已被别人建了
    assert consolidate.requested_files(
        [{"file": "a"}, {"file": "b.md"}, "x", {"op": "delete"}]
    ) == {"a.md", "b.md"}


def test_render_produces_standard_memory_file():
    op = consolidate.Operation(op="write", file="user-1.md", type="user", description="阿杰", content="正文")
    doc = parse_memory(op.render())
    assert (doc.name, doc.description, doc.type, doc.body) == ("user-1", "阿杰", "user", "正文")


def test_to_dict_and_payload_roundtrip():
    ops = [
        consolidate.Operation(op="delete", file="b.md", reason="r"),
        consolidate.Operation(op="write", file="a.md", type="user", description="d", content="c"),
    ]
    payload = [op.to_dict() for op in ops]
    assert payload[0] == {"op": "delete", "file": "b.md", "reason": "r", "baseMtime": 0.0}
    restored = consolidate.operations_from_payload(payload, {"b.md"})
    assert restored == ops
    # 回传时文件已被删掉：删除操作被重新校验丢弃
    assert consolidate.operations_from_payload(payload, set()) == [ops[1]]
    assert consolidate.operations_from_payload("nope", {"b.md"}) == []


def test_ordered_writes_before_deletes():
    ops = [
        consolidate.Operation(op="delete", file="b.md"),
        consolidate.Operation(op="write", file="a.md", type="user", content="c"),
        consolidate.Operation(op="delete", file="c.md"),
    ]
    assert [op.file for op in consolidate.ordered(ops)] == ["a.md", "b.md", "c.md"]
