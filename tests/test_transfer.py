import asyncio
import json
import os

import pytest

from core import transfer
from core.archive import ArchiveStore
from core.memstore import (
    CONFLICT_NEWER,
    CONFLICT_OVERWRITE,
    CONFLICT_SKIP,
    IMPORT_ADDED,
    IMPORT_INVALID,
    IMPORT_KEPT,
    IMPORT_REPLACED,
    IMPORT_SAME,
    INDEX_FILENAME,
    ImportEntry,
    MemoryStore,
    Usage,
    compose_memory,
    safe_key,
)
from core.registry import SessionRegistry, umo_from_key

OLD = 1_700_000_000.0
UMO = "aiocqhttp:GroupMessage:100200"
KEY = safe_key(UMO)
NEW_UMO = "qq_new:GroupMessage:100200"
NEW_KEY = safe_key(NEW_UMO)


def run(coro):
    return asyncio.run(coro)


def memory(name, description, body="正文", updated="2024-01-02 03:04"):
    # 带上指定的 updated 戳，模拟一条老记忆
    return compose_memory(name, description, "user", body, created=updated, updated=updated)


@pytest.fixture
def env(tmp_path):
    store = MemoryStore(tmp_path / "memories")
    registry = SessionRegistry(tmp_path / "sessions.json")
    archive = ArchiveStore(tmp_path / "archive")
    return store, registry, archive, transfer.MemoryTransfer(store, registry, archive)


def write_raw(scope, name, content, mtime=OLD):
    scope.root.mkdir(parents=True, exist_ok=True)
    path = scope.root / name
    path.write_text(content, "utf-8")
    os.utime(path, (mtime, mtime))


# ---------------------------------------------------------------- 原样导入


def test_import_keeps_stamps_mtime_and_usage(tmp_path):
    scope = MemoryStore(tmp_path).session_scope(UMO)
    content = memory("a", "老记忆")
    usage = Usage(last_used=OLD + 5, uses=3)
    results = run(scope.import_files([ImportEntry("a.md", content, usage=usage, mtime=OLD)]))
    assert [(r.name, r.outcome) for r in results] == [("a.md", IMPORT_ADDED)]
    assert (scope.root / "a.md").read_text("utf-8") == content  # 没补新戳
    assert abs(scope.mtime("a.md") - OLD) < 1e-3
    assert scope.usage_of("a.md") == usage
    assert "[a](a.md)" in (scope.root / INDEX_FILENAME).read_text("utf-8")


def test_import_conflict_policies(tmp_path):
    scope = MemoryStore(tmp_path).session_scope(UMO)
    write_raw(scope, "a.md", memory("a", "现有", updated="2024-05-01 00:00"))
    older = ImportEntry("a.md", memory("a", "旧版", updated="2024-01-01 00:00"))
    newer = ImportEntry("a.md", memory("a", "新版", updated="2024-09-01 00:00"))
    assert run(scope.import_files([older], CONFLICT_NEWER, dry_run=True))[0].outcome == IMPORT_KEPT
    assert run(scope.import_files([newer], CONFLICT_NEWER, dry_run=True))[0].outcome == IMPORT_REPLACED
    assert run(scope.import_files([newer], CONFLICT_SKIP, dry_run=True))[0].outcome == IMPORT_KEPT
    assert run(scope.import_files([older], CONFLICT_OVERWRITE, dry_run=True))[0].outcome == IMPORT_REPLACED
    assert "现有" in (scope.root / "a.md").read_text("utf-8")  # 预演不落盘
    same = ImportEntry("a.md", (scope.root / "a.md").read_text("utf-8"))
    assert run(scope.import_files([same]))[0].outcome == IMPORT_SAME


def test_import_replace_merges_usage(tmp_path):
    scope = MemoryStore(tmp_path).session_scope(UMO)
    write_raw(scope, "a.md", memory("a", "现有"))
    scope.note_used(["a.md"], now=OLD + 100)
    entry = ImportEntry("a.md", memory("a", "覆盖"), usage=Usage(last_used=OLD, uses=5))
    run(scope.import_files([entry], CONFLICT_OVERWRITE))
    usage = scope.usage_of("a.md")
    assert usage.last_used == OLD + 100 and usage.uses == 5


def test_import_rejects_bad_entries(tmp_path):
    scope = MemoryStore(tmp_path).session_scope(UMO)
    entries = [
        ImportEntry("../x.md", "内容"),
        ImportEntry("MEMORY.md", "内容"),
        ImportEntry("b.md", "  "),
        ImportEntry("c.md", memory("c", "c")),
        ImportEntry("c.md", memory("c", "c2")),
    ]
    outcomes = [r.outcome for r in run(scope.import_files(entries))]
    assert outcomes == [IMPORT_INVALID] * 3 + [IMPORT_ADDED, IMPORT_INVALID]


def test_import_forgotten_and_restore(tmp_path):
    scope = MemoryStore(tmp_path).session_scope(UMO)
    content = memory("a", "睡着的")
    run(scope.import_files([ImportEntry("a.md", content, forgotten=True, mtime=OLD)]))
    assert scope.list_files() == [] and scope.forgotten_files() == ["a.md"]
    assert scope.usage_of("a.md").forgotten_at == OLD
    # 同一条在册导入：恢复在册
    assert run(scope.import_files([ImportEntry("a.md", content)]))[0].outcome == IMPORT_SAME
    assert scope.list_files() == ["a.md"] and scope.forgotten_files() == []
    # 已遗忘的导入件不顶替在册的同名记忆
    other = ImportEntry("a.md", memory("a", "另一份"), forgotten=True)
    assert run(scope.import_files([other], CONFLICT_OVERWRITE))[0].outcome == IMPORT_KEPT


def test_import_adopts_bundle_index_only_when_missing(tmp_path):
    scope = MemoryStore(tmp_path).session_scope(UMO)
    index = "## 分组\n- [b](b.md) — 乙\n- [a](a.md) — 甲\n- [gone](gone.md) — 不在了\n"
    entries = [ImportEntry("a.md", memory("a", "甲")), ImportEntry("b.md", memory("b", "乙"))]
    run(scope.import_files(entries, index=index))
    text = (scope.root / INDEX_FILENAME).read_text("utf-8")
    assert "## 分组" in text and text.index("b.md") < text.index("a.md")
    assert "gone.md" not in text


# ---------------------------------------------------------------- 备份


def test_export_import_roundtrip(env, tmp_path):
    store, registry, archive, mover = env
    session = store.session_scope(UMO)
    write_raw(session, "a.md", memory("a", "甲"))
    session.note_used(["a.md"], now=OLD + 1)
    registry.touch(KEY, UMO, "开黑群", now=OLD)
    registry.flush()
    write_raw(store.global_scope(), "self.md", memory("self", "自我"))
    bundle = run(mover.export(now=OLD + 10))
    assert bundle["format"] == transfer.BUNDLE_FORMAT
    assert {(s["scope"], s["key"]) for s in bundle["scopes"]} == {("global", ""), ("session", KEY)}
    text = json.dumps(bundle, ensure_ascii=False)

    store2 = MemoryStore(tmp_path / "other")
    registry2 = SessionRegistry(tmp_path / "other.json")
    mover2 = transfer.MemoryTransfer(store2, registry2, ArchiveStore(tmp_path / "a2"))
    scopes = transfer.parse_bundle(json.loads(text))
    summary = mover2.bundle_summary(scopes)
    by_id = {item["scope"]: item for item in summary}
    assert by_id["session"]["name"] == "开黑群" and by_id["session"]["umo"] == UMO
    assert by_id["session"]["fileCount"] == 1

    reports = run(mover2.import_bundle(scopes, dry_run=True))
    assert all(r["counts"][IMPORT_ADDED] == 1 for r in reports)
    assert store2.session_keys() == []  # 预演不落盘
    run(mover2.import_bundle(scopes))
    copied = store2.session_scope(UMO)
    assert (copied.root / "a.md").read_text("utf-8") == (session.root / "a.md").read_text("utf-8")
    assert copied.usage_of("a.md").uses == 1
    record = registry2.get(KEY)
    assert record.umo == UMO and record.name == "开黑群" and record.first_seen == OLD


@pytest.mark.parametrize(
    "data, message",
    [
        ([], "不是记忆备份"),
        ({"format": "x"}, "不是记忆备份"),
        ({"format": transfer.BUNDLE_FORMAT, "version": 9, "scopes": []}, "版本不受支持"),
        ({"format": transfer.BUNDLE_FORMAT, "version": 1}, "没有作用域"),
    ],
)
def test_parse_bundle_errors(data, message):
    with pytest.raises(transfer.TransferError, match=message):
        transfer.parse_bundle(data)


def test_parse_bundle_skips_bad_scopes():
    data = {
        "format": transfer.BUNDLE_FORMAT,
        "version": 1,
        "scopes": [
            {"scope": "session", "key": "../x", "files": []},
            {"scope": "elsewhere"},
            {"scope": "global", "files": [{"name": "a.md", "content": "x"}, {"name": 1}]},
            {"scope": "global", "files": []},
        ],
    }
    scopes = transfer.parse_bundle(data)
    assert [s.ref.id for s in scopes] == ["global"]
    assert [e.name for e in scopes[0].entries] == ["a.md"]


def test_people_scope_only_takes_person_files(env):
    store, _, _, mover = env
    scopes = [
        transfer.BundleScope(
            ref=transfer.ScopeRef("people", "aiocqhttp"),
            entries=[
                ImportEntry("user-1.md", memory("user-1", "阿杰")),
                ImportEntry("notes.md", memory("notes", "杂项")),
            ],
        )
    ]
    report = run(mover.import_bundle(scopes))[0]
    assert report["counts"][IMPORT_ADDED] == 1 and report["counts"][IMPORT_INVALID] == 1
    assert report["issues"][0]["reason"] == transfer.PEOPLE_ONLY
    assert store.people_scope_by_key("aiocqhttp").list_files() == ["user-1.md"]


# ---------------------------------------------------------------- 作用域之间搬


def test_transfer_copy_and_move(env):
    store, registry, _, mover = env
    src = store.session_scope(UMO)
    write_raw(src, "a.md", memory("a", "甲", updated="2024-01-01 00:00"))
    write_raw(src, "b.md", memory("b", "乙"))
    dst = store.session_scope(NEW_UMO)
    write_raw(dst, "a.md", memory("a", "目标较新", updated="2024-06-01 00:00"))
    src_ref = transfer.ScopeRef("session", KEY)
    dst_ref = transfer.ScopeRef("session", NEW_KEY)

    report = run(mover.transfer(src_ref, dst_ref, dst_umo=NEW_UMO, dry_run=True))
    assert report["counts"][IMPORT_ADDED] == 1 and report["counts"][IMPORT_KEPT] == 1
    assert dst.list_files() == ["a.md"]

    report = run(mover.transfer(src_ref, dst_ref, dst_umo=NEW_UMO, mode=transfer.MODE_MOVE))
    assert report["moved"] == 1
    assert src.list_files() == ["a.md"]  # 冲突留在目标外的没删
    assert dst.list_files() == ["a.md", "b.md"]
    assert "目标较新" in (dst.root / "a.md").read_text("utf-8")
    assert registry.get(NEW_KEY).umo == NEW_UMO

    with pytest.raises(transfer.TransferError, match="同一处"):
        run(mover.transfer(src_ref, src_ref))


def test_transfer_selected_files(env):
    store, _, _, mover = env
    src = store.session_scope(UMO)
    write_raw(src, "a.md", memory("a", "甲"))
    write_raw(src, "b.md", memory("b", "乙"))
    report = run(
        mover.transfer(
            transfer.ScopeRef("session", KEY), transfer.ScopeRef("global"), files=["b.md"]
        )
    )
    assert report["counts"][IMPORT_ADDED] == 1
    assert store.global_scope().list_files() == ["b.md"]
    with pytest.raises(transfer.TransferError, match="没有可迁移"):
        run(mover.transfer(transfer.ScopeRef("session", KEY), transfer.ScopeRef("global"), files=["x.md"]))


# ---------------------------------------------------------------- 平台改名


def test_umo_from_key_roundtrip():
    assert umo_from_key(KEY) == UMO
    assert umo_from_key("whatever") == ""


def test_remap_platform_moves_sessions_people_and_archives(env):
    store, registry, archive, mover = env
    src = store.session_scope(UMO)
    write_raw(src, "a.md", memory("a", "甲"))
    registry.touch(KEY, UMO, "开黑群", now=OLD)
    registry.flush()
    archive.save(KEY, umo=UMO, cid="c", messages=[{"role": "user", "content": "你好"}], summary="s", keep=3)
    # 认得出平台、却拼不回完整会话标识的目录（转换时丢了字符）：报告为跳过
    odd_key = "aiocqhttp_GroupMessage_a_b-0000abcd"
    write_raw(store.session_scope_by_key(odd_key), "x.md", memory("x", "怪"))
    # 认不出平台的目录不归这个平台管
    write_raw(store.session_scope_by_key("weird"), "y.md", memory("y", "怪"))
    people = store.people_scope("aiocqhttp")
    write_raw(people, "user-1.md", memory("user-1", "阿杰"))

    preview = run(mover.remap_platform("aiocqhttp", "qq_new", dry_run=True))
    assert len(preview["items"]) == 2 and preview["totals"][IMPORT_ADDED] == 2
    assert preview["items"][0]["to"]["umo"] == NEW_UMO
    assert store.session_scope_by_key(NEW_KEY).list_files() == []

    result = run(mover.remap_platform("aiocqhttp", "qq_new"))
    assert [s["key"] for s in result["skipped"]] == [odd_key]
    assert store.session_scope_by_key(odd_key).list_files() == ["x.md"]
    assert store.session_scope_by_key("weird").list_files() == ["y.md"]
    assert store.session_scope_by_key(NEW_KEY).list_files() == ["a.md"]
    assert archive.count(NEW_KEY) == 1 and archive.count(KEY) == 0
    assert registry.get(KEY) is None
    moved = registry.get(NEW_KEY)
    assert moved.umo == NEW_UMO and moved.name == "开黑群" and moved.first_seen == OLD
    assert KEY not in store.session_keys()
    assert store.people_scope_by_key("qq_new").list_files() == ["user-1.md"]
    assert "aiocqhttp" not in store.people_keys()


@pytest.mark.parametrize("src, dst, message", [("a", "a", "相同"), ("a:b", "c", "冒号"), ("", "c", "不能为空")])
def test_remap_platform_validates(env, src, dst, message):
    with pytest.raises(transfer.TransferError, match=message):
        run(env[3].remap_platform(src, dst))
