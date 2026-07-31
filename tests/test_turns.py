from core.turns import is_tool_result, split_leading_system, split_turns


def _history():
    return [
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "result"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]


def test_split_leading_system():
    assert split_leading_system(_history()) == 1
    assert split_leading_system([]) == 0
    assert split_leading_system([{"role": "user", "content": "x"}]) == 0


def test_is_tool_result():
    assert is_tool_result({"role": "tool"})
    assert is_tool_result({"role": "user", "tool_call_id": "t1"})
    assert not is_tool_result({"role": "user", "content": "hi"})


def test_split_turns_tool_result_stays_in_turn():
    turns = split_turns(_history(), start=1)
    assert len(turns) == 2
    assert turns[0].start == 1
    assert len(turns[0].messages) == 4  # user + assistant(tool_call) + tool + assistant
    assert turns[1].start == 5
    assert len(turns[1].messages) == 2


def test_split_turns_orphan_leading_messages():
    history = [
        {"role": "assistant", "content": "orphan"},
        {"role": "tool", "tool_call_id": "x", "content": "r"},
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "a"},
    ]
    turns = split_turns(history)
    assert len(turns) == 2
    assert turns[0].start == 0
    assert len(turns[0].messages) == 2


def test_split_turns_skips_non_dict():
    turns = split_turns([{"role": "user", "content": "q"}, "junk"])
    assert len(turns) == 1
    assert len(turns[0].messages) == 1
