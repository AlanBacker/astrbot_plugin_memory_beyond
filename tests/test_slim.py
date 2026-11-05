import re

from core.slim import TOOL_RESULT_PLACEHOLDER, slim_history

PREFIX = "<122808: 系统附加提醒：短一点>用户输入："
PREFIX_RX = re.compile(r"<122808:.*?>(?=用户输入：)", re.DOTALL)
REMINDER = (
    "<system_reminder>Group name: 测试群\n"
    "Current datetime: 2026-11-05 08:00 (CST), Weekday: Thursday</system_reminder>"
)
SEARCH = ["web_search*", "*extract_web_page*"]


def _search_round(call_id: str, name: str, result: str) -> list[dict]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": result},
    ]


def test_strips_prefix_and_trims_reminder_in_list_content():
    history = [
        {"role": "system", "content": "persona"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"[发送者：阿明｜123]\n{PREFIX}在吗"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
                {"type": "text", "text": REMINDER},
            ],
        },
        {"role": "assistant", "content": "在"},
    ]
    slimmed, saved = slim_history(history, PREFIX_RX, SEARCH)
    parts = slimmed[1]["content"]
    assert parts[0]["text"] == "[发送者：阿明｜123]\n用户输入：在吗"
    assert parts[1] == history[1]["content"][1]
    assert parts[2]["text"] == (
        "<system_reminder>Current datetime: 2026-11-05 08:00 (CST), "
        "Weekday: Thursday</system_reminder>"
    )
    assert slimmed[0] is history[0] and slimmed[2] is history[2]
    assert saved > 0


def test_drops_part_holding_only_a_reminder_without_datetime():
    history = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "你好"},
                {
                    "type": "text",
                    "text": "<system_reminder>主动插话：随便聊</system_reminder>",
                },
                {"type": "text", "text": ""},
            ],
        }
    ]
    slimmed, _ = slim_history(history, None, [])
    assert slimmed[0]["content"] == [
        {"type": "text", "text": "你好"},
        {"type": "text", "text": ""},
    ]


def test_keeps_group_chat_reminder():
    block = "<system_reminder>You are in a group chat. 群友A: 吃了吗</system_reminder>"
    history = [{"role": "user", "content": f"{PREFIX}嗯{block}"}]
    slimmed, _ = slim_history(history, PREFIX_RX, [])
    assert slimmed[0]["content"] == f"用户输入：嗯{block}"


def test_replaces_matching_tool_results_only():
    history = [
        {"role": "user", "content": "查一下"},
        *_search_round("c1", "web_search_tavily", "很长的搜索结果" * 50),
        *_search_round("c2", "memory_read", "记忆原文"),
        *_search_round("c3", "tavily_extract_web_page", "网页原文" * 50),
        {"role": "assistant", "content": "查到了"},
    ]
    slimmed, saved = slim_history(history, None, SEARCH)
    assert slimmed[2]["content"] == TOOL_RESULT_PLACEHOLDER
    assert slimmed[4]["content"] == "记忆原文"
    assert slimmed[6]["content"] == TOOL_RESULT_PLACEHOLDER
    assert [m.get("tool_call_id") for m in slimmed] == [
        m.get("tool_call_id") for m in history
    ]
    assert slimmed[1] is history[1]
    assert saved > 0


def test_idempotent_and_unchanged_history_returned_as_is():
    history = [
        {"role": "user", "content": f"{PREFIX}在吗{REMINDER}"},
        *_search_round("c1", "web_search_bocha", "结果" * 100),
    ]
    once, _ = slim_history(history, PREFIX_RX, SEARCH)
    twice, saved = slim_history(once, PREFIX_RX, SEARCH)
    assert twice is once and saved == 0


def test_literal_prefix_from_current_config():
    rx = re.compile(re.escape(PREFIX))
    history = [{"role": "user", "content": f"{PREFIX}在吗"}]
    slimmed, _ = slim_history(history, rx, [])
    assert slimmed[0]["content"] == "在吗"
