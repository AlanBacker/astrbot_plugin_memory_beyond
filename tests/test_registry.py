import json
import time

from core import registry
from core.registry import SessionRegistry

NOW = 1_800_000_000.0


def test_parse_umo():
    assert registry.parse_umo("aiocqhttp:GroupMessage:100200") == (
        "aiocqhttp",
        "group",
        "100200",
    )
    assert registry.parse_umo("tg:FriendMessage:42") == ("tg", "private", "42")
    assert registry.parse_umo("x:Weird:1") == ("x", "other", "1")
    assert registry.parse_umo("broken") == ("", "other", "broken")


def test_guess_from_key():
    assert registry.guess_from_key("aiocqhttp_GroupMessage_100200-0a1b2c3d") == (
        "aiocqhttp",
        "group",
        "100200",
    )
    assert registry.guess_from_key("qq_FriendMessage_42") == ("qq", "private", "42")
    assert registry.guess_from_key("whatever") == ("", "other", "whatever")


def test_touch_creates_record_and_reports_urgent(tmp_path):
    reg = SessionRegistry(tmp_path / "sessions.json")
    now = time.time()
    assert reg.touch("k1", "qq:GroupMessage:1", "开黑群", now=now) is True
    record = reg.get("k1")
    assert (record.platform, record.kind, record.target) == ("qq", "group", "1")
    assert record.name == "开黑群"
    assert record.first_seen == record.last_active == now
    reg.flush()
    # 已落盘、名字没变、未到节流间隔：不需要再写
    assert reg.touch("k1", "qq:GroupMessage:1", "开黑群", now=now + 1) is False
    # 改名立即需要落盘
    assert reg.touch("k1", "qq:GroupMessage:1", "新群名", now=now + 2) is True
    assert reg.get("k1").last_active == now + 2
    # 会话 UMO 变了（如平台适配器改名）也立即落盘
    reg.flush()
    assert reg.touch("k1", "qq2:GroupMessage:1", now=now + 3) is True
    assert reg.get("k1").platform == "qq2"


def test_touch_throttle_interval(tmp_path):
    reg = SessionRegistry(tmp_path / "sessions.json")
    reg.touch("k1", "qq:GroupMessage:1", now=NOW)
    reg.flush()
    later = reg._flushed_at + registry.FLUSH_INTERVAL + 1
    assert reg.touch("k1", "qq:GroupMessage:1", now=later) is True


def test_flush_and_reload_roundtrip(tmp_path):
    path = tmp_path / "sessions.json"
    reg = SessionRegistry(path)
    reg.touch("k1", "qq:FriendMessage:42", "  阿杰\n ", now=NOW)
    reg.set_remark("k1", "群主私聊")
    reg.flush()
    payload = json.loads(path.read_text("utf-8"))
    assert payload["version"] == 1
    assert "key" not in payload["sessions"]["k1"]
    again = SessionRegistry(path).get("k1")
    assert again.name == "阿杰"  # 空白被规整
    assert again.remark == "群主私聊"
    assert again.kind == "private"


def test_set_remark_without_record_guesses_from_key(tmp_path):
    reg = SessionRegistry(tmp_path / "sessions.json")
    record = reg.set_remark("qq_GroupMessage_100", "x" * 100)
    assert (record.platform, record.kind, record.target) == ("qq", "group", "100")
    assert len(record.remark) == registry.MAX_TEXT
    assert (tmp_path / "sessions.json").is_file()


def test_remove(tmp_path):
    path = tmp_path / "sessions.json"
    reg = SessionRegistry(path)
    reg.touch("k1", "qq:GroupMessage:1", now=NOW)
    reg.flush()
    reg.remove("k1")
    assert reg.get("k1") is None
    assert SessionRegistry(path).all() == {}


def test_load_tolerates_garbage(tmp_path):
    path = tmp_path / "sessions.json"
    path.write_text(
        json.dumps(
            {
                "sessions": {
                    "ok": {"kind": "martian", "first_seen": "bad", "name": 3},
                    "bad": "not a dict",
                }
            }
        ),
        "utf-8",
    )
    reg = SessionRegistry(path)
    assert list(reg.all()) == ["ok"]
    record = reg.get("ok")
    assert record.kind == "other"
    assert record.first_seen == 0.0
    assert record.name == ""
    path.write_text("{not json", "utf-8")
    assert SessionRegistry(path).all() == {}
