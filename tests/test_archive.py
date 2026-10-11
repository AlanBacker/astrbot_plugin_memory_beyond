from core import archive
from core.archive import ArchiveStore

NOW = 1_800_000_000.5

MESSAGES = [
    {"role": "system", "content": "系统提示"},
    {"role": "user", "content": "这周六还开黑吗"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {"name": "memory_read", "arguments": "{}"}}],
    },
    {"role": "tool", "content": "记忆内容"},
    {"role": "assistant", "content": [{"type": "text", "text": "开的"}]},
]


def test_save_simplifies_messages(tmp_path):
    store = ArchiveStore(tmp_path)
    archive_id = store.save(
        "k1", umo="u", cid="c", messages=MESSAGES, summary="摘要", keep=5, now=NOW
    )
    assert archive_id == archive.make_archive_id(NOW)
    data = store.load("k1", archive_id)
    assert data["count"] == 4  # system 不归档
    assert data["summary"] == "摘要"
    assert data["messages"][0] == {"role": "user", "text": "这周六还开黑吗"}
    assert data["messages"][1]["tools"] == ["memory_read"]
    assert data["messages"][3]["text"] == "开的"


def test_save_skips_platform_internal_records(tmp_path):
    store = ArchiveStore(tmp_path)
    messages = MESSAGES + [{"role": "_checkpoint", "content": {"id": "ckpt-1"}}]
    archive_id = store.save(
        "k1", umo="u", cid="c", messages=messages, summary="s", keep=5, now=NOW
    )
    data = store.load("k1", archive_id)
    assert data["count"] == 4
    assert all(m["role"] != "_checkpoint" for m in data["messages"])


def test_save_truncates_long_messages(tmp_path):
    store = ArchiveStore(tmp_path)
    long_text = "x" * (archive.MESSAGE_CHARS + 10)
    archive_id = store.save(
        "k1",
        umo="u",
        cid="c",
        messages=[{"role": "user", "content": long_text}],
        summary="",
        keep=5,
        now=NOW,
    )
    text = store.load("k1", archive_id)["messages"][0]["text"]
    assert text.endswith("（归档截断）")
    assert len(text) < len(long_text) + 20


def test_keep_zero_or_empty_does_nothing(tmp_path):
    store = ArchiveStore(tmp_path)
    assert store.save("k1", umo="u", cid="c", messages=MESSAGES, summary="", keep=0) is None
    only_system = [{"role": "system", "content": "s"}]
    assert store.save("k1", umo="u", cid="c", messages=only_system, summary="", keep=5) is None
    assert store.count("k1") == 0
    assert store.list("k1") == []


def test_prune_keeps_newest_and_list_order(tmp_path):
    store = ArchiveStore(tmp_path)
    ids = [
        store.save("k1", umo="u", cid="c", messages=MESSAGES, summary="s" * i, keep=2, now=NOW + i)
        for i in range(4)
    ]
    assert store.count("k1") == 2
    listed = store.list("k1")
    assert [item["id"] for item in listed] == [ids[3], ids[2]]
    assert listed[0]["count"] == 4
    assert listed[0]["summaryChars"] == 3
    assert store.load("k1", ids[0]) is None


def test_load_rejects_bad_ids(tmp_path):
    store = ArchiveStore(tmp_path)
    store.save("k1", umo="u", cid="c", messages=MESSAGES, summary="", keep=2, now=NOW)
    assert store.load("k1", "../../etc/passwd") is None
    assert store.load("k1", "") is None
    assert store.load("k1", "20990101-000000-000") is None


def test_purge(tmp_path):
    store = ArchiveStore(tmp_path)
    store.save("k1", umo="u", cid="c", messages=MESSAGES, summary="", keep=2, now=NOW)
    store.purge("k1")
    assert store.count("k1") == 0
    store.purge("missing")  # 不存在也不报错
