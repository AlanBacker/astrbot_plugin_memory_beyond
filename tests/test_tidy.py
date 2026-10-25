import asyncio
import json

import pytest

from core import tidy
from core.memstore import MemoryHeader, MemoryStore, compose_memory

NOW = 1_800_000_000.0
HOUR = 3600.0
UMO = "aiocqhttp:GroupMessage:100200"


def run(coro):
    return asyncio.run(coro)


def header(file, updated):
    return MemoryHeader(file=file, name=file[:-3], description="", type="user", mtime=updated, size=0, updated=updated)


def wrap(ops):
    return f"<operations>{json.dumps(ops, ensure_ascii=False)}</operations>"


def memory(name, description, body="正文"):
    return compose_memory(name, description, "user", body)


@pytest.fixture
def scope(tmp_path):
    store = MemoryStore(tmp_path / "memories").session_scope(UMO)
    run(store.write("a.md", memory("a", "甲")))
    run(store.write("b.md", memory("b", "乙")))
    store.note_used(["b.md"], now=NOW)
    return store


def model(reply):
    calls = []

    async def call(prompt):
        calls.append(prompt)
        return reply, [] if reply else ["provider-x: 超时"]

    call.calls = calls
    return call


# ---------------------------------------------------------------- 判断


def test_due_reason_gates():
    fresh = [header(f"{i}.md", NOW - 10) for i in range(tidy.MIN_CHANGES)]
    meta = tidy.TidyMeta()
    assert tidy.due_reason(meta, fresh, NOW, 24) == ""  # 从没整理过：攒够就整理
    assert "不多" in tidy.due_reason(meta, fresh[:-1], NOW, 24)

    meta.last_run = NOW - 23 * HOUR
    assert tidy.due_reason(meta, fresh, NOW, 24) == "未到间隔"
    meta.last_run = NOW - 25 * HOUR
    assert tidy.due_reason(meta, fresh, NOW, 24) == ""
    stale = [header(f"{i}.md", NOW - 30 * HOUR) for i in range(10)]
    assert "不多" in tidy.due_reason(meta, stale, NOW, 24)  # 上次之后没新东西

    meta.failures, meta.last_attempt = 1, NOW - 10
    assert "失败" in tidy.due_reason(meta, fresh, NOW, 24)
    meta.last_attempt = NOW - tidy.FAIL_BACKOFF - 1
    assert tidy.due_reason(meta, fresh, NOW, 24) == ""

    meta.pending = {"operations": [{}]}
    assert "待确认" in tidy.due_reason(meta, fresh, NOW, 24)


@pytest.mark.parametrize("raw, hours", [(None, 24), ("x", 24), (0, 1), (5, 5), (10**6, 24 * 30)])
def test_clamp_hours(raw, hours):
    assert tidy.clamp_hours(raw) == hours


def test_normalize_mode():
    assert tidy.normalize_mode(" Confirm ") == "confirm"
    assert tidy.normalize_mode("whatever") == "off"


# ---------------------------------------------------------------- 记录


def test_book_meta_runs_and_busy(tmp_path):
    book = tidy.TidyBook(tmp_path / "tidy")
    assert book.meta("global").last_run == 0 and book.all_meta() == {}

    def change(meta):
        meta.last_run = NOW
        meta.pending = {"createdAt": NOW, "operations": [{"op": "delete"}]}

    book.update_meta("session-k", change)
    assert book.meta("session-k").pending_count() == 1
    assert set(book.all_meta()) == {"session-k"}

    for i in range(tidy.KEEP_RUNS + 2):
        book.add_run("session-k", {"id": str(i), "ops": []})
    runs = book.runs("session-k")
    assert [r["id"] for r in runs] == [str(i) for i in range(tidy.KEEP_RUNS + 1, 1, -1)]
    book.mark_undone("session-k", runs[0]["id"], NOW)
    assert book.runs("session-k")[0]["undoneAt"] == NOW
    assert set(book.all_meta()) == {"session-k"}  # 整理记录不算一处

    assert book.claim("x") and not book.claim("x")
    book.release("x")
    assert book.claim("x")

    book.mark_asked(NOW)
    assert book.settings() == {"asked": True, "askedAt": NOW}
    book.remove("session-k")
    assert book.runs("session-k") == [] and book.all_meta() == {}


def test_meta_from_bad_raw():
    meta = tidy.TidyMeta.from_raw({"lastRun": "x", "failures": -3, "pending": "no"})
    assert (meta.last_run, meta.failures, meta.pending) == (0.0, 0, None)


# ---------------------------------------------------------------- 复盘、应用、撤销


def test_propose_nothing_to_do(scope):
    call = model("<operations>[]</operations>")
    proposal = run(tidy.propose(scope, "开黑群", call))
    assert proposal.operations == [] and proposal.files == ["a.md", "b.md"]
    assert "「开黑群」" in call.calls[0]


def test_propose_errors(scope, tmp_path):
    with pytest.raises(tidy.TidyError, match="超时"):
        run(tidy.propose(scope, "x", model("")))
    with pytest.raises(tidy.TidyError, match="无法解析"):
        run(tidy.propose(scope, "x", model("看起来不错")))
    empty = MemoryStore(tmp_path / "empty").global_scope()
    with pytest.raises(tidy.TidyError, match="还没有记忆"):
        run(tidy.propose(empty, "x", model("[]")))


def merge_ops():
    return [
        {"op": "write", "file": "a.md", "type": "user", "description": "甲乙", "content": "合并", "reason": "合并"},
        {"op": "write", "file": "c.md", "type": "user", "description": "丙", "content": "新的", "reason": "拆分"},
        {"op": "delete", "file": "b.md", "reason": "并入 a"},
    ]


def test_apply_and_undo_restores_everything(scope):
    before_a = run(scope.read("a.md"))
    before_b = run(scope.read("b.md"))
    usage_b = scope.usage_of("b.md")
    proposal = run(tidy.propose(scope, "x", model(wrap(merge_ops()))))
    results = run(tidy.apply(scope, proposal.operations))
    assert [r["ok"] for r in results] == [True, True, True]
    assert scope.list_files() == ["a.md", "c.md"]
    run_record = tidy.make_run(tidy.TRIGGER_AUTO, results, NOW)
    assert all("before" not in r for r in tidy.public_results(results))
    public = tidy.public_run(run_record)
    assert [(op["file"], op["created"]) for op in public["ops"]] == [
        ("a.md", False),
        ("c.md", True),
        ("b.md", False),
    ]

    undone = run(tidy.undo(scope, run_record))
    assert all(r["ok"] for r in undone), undone
    assert scope.list_files() == ["a.md", "b.md"]
    assert run(scope.read("a.md")) == before_a
    assert run(scope.read("b.md")) == before_b
    assert scope.usage_of("b.md") == usage_b  # 使用统计也还回来了


def test_undo_skips_files_changed_afterwards(scope):
    proposal = run(tidy.propose(scope, "x", model(wrap(merge_ops()))))
    results = run(tidy.apply(scope, proposal.operations))
    run_record = tidy.make_run(tidy.TRIGGER_MANUAL, results, NOW)
    run(scope.write("a.md", memory("a", "之后又改了")))
    run(scope.write("b.md", memory("b", "同名新写的")))
    undone = {r["file"]: r for r in run(tidy.undo(scope, run_record))}
    assert not undone["a.md"]["ok"] and "又被改过" in undone["a.md"]["message"]
    assert not undone["b.md"]["ok"] and "已经存在" in undone["b.md"]["message"]
    assert undone["c.md"]["ok"]
    assert "之后又改了" in run(scope.read("a.md"))


def test_apply_skips_stale_and_protects_sources(scope):
    proposal = run(tidy.propose(scope, "x", model(wrap(merge_ops()))))
    run(scope.write("a.md", memory("a", "复盘后改过")))
    results = {r["file"]: r for r in run(tidy.apply(scope, proposal.operations))}
    assert not results["a.md"]["ok"] and "被改过" in results["a.md"]["message"]
    assert not results["b.md"]["ok"] and "跳过删除" in results["b.md"]["message"]
    assert scope.exists("b.md")


def test_apply_people_only_person_files(tmp_path):
    people = MemoryStore(tmp_path).people_scope("aiocqhttp")
    run(people.write("user-1.md", memory("user-1", "阿杰")))
    ops = [{"op": "write", "file": "notes.md", "type": "user", "description": "x", "content": "y"}]
    proposal = run(tidy.propose(people, "x", model(wrap(ops))))
    results = run(tidy.apply(people, proposal.operations, people=True))
    assert results[0]["message"] == tidy.PEOPLE_ONLY and not people.exists("notes.md")
    assert tidy.make_run(tidy.TRIGGER_AUTO, results, NOW) is None
