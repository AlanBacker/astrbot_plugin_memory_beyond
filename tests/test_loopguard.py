from types import SimpleNamespace

import pytest

from core import compress, loopguard, tokens
from core.loopguard import GuardOutcome, LoopGuard

CLEARED = compress.TOOL_RESULT_CLEARED


def _call(call_id="c0", name="fetch", args="{}"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": args}}
        ],
    }


def _tool(chars, call_id="c0"):
    return {"role": "tool", "tool_call_id": call_id, "content": "r" * chars}


def _round(index, chars):
    return [_call(f"c{index}"), _tool(chars, f"c{index}")]


def _base():
    return [
        {"role": "system", "content": "persona"},
        {"role": "user", "content": "帮我把这几个网页读一遍"},
    ]


def _flat(*rounds):
    out = _base()
    for r in rounds:
        out.extend(r)
    return out


class Msg:
    """pydantic Message 的最小替身：属性读写，content 可直接赋值。"""

    def __init__(self, role, content=None, tool_calls=None, tool_call_id=None):
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id


class Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class ToolCallObj:
    def __init__(self, name, arguments="{}"):
        self.function = Fn(name, arguments)


def _as_objects(messages):
    out = []
    for m in messages:
        calls = m.get("tool_calls")
        out.append(
            Msg(
                m["role"],
                m.get("content"),
                [ToolCallObj(c["function"]["name"], c["function"]["arguments"]) for c in calls]
                if calls
                else None,
                m.get("tool_call_id"),
            )
        )
    return out


def _est():
    return tokens.TokenEstimator(1.0)


def _gain(chars):
    """一条 chars 字符的工具结果换成占位说明省下的 token 数。"""
    return tokens.estimate_text("r" * chars) - tokens.estimate_text(CLEARED)


# ---------------------------------------------------------------- 消息读取


def test_message_view_passes_dict_through():
    message = {"role": "tool", "content": "x"}
    assert loopguard.message_view(message) is message


def test_message_view_object_matches_dict_estimate():
    objects = _as_objects(_flat(_round(0, 800)))
    dicts = _flat(_round(0, 800))
    est = _est()
    assert est.messages([loopguard.message_view(m) for m in objects]) == est.messages(dicts)


def test_message_view_parts_are_light():
    parts = [
        SimpleNamespace(type="text", text="你好"),
        SimpleNamespace(type="think", think="想一想"),
        SimpleNamespace(type="image_url", image_url=SimpleNamespace(url="data:" + "A" * 100_000)),
        SimpleNamespace(type="audio_url"),
        {"type": "text", "text": "dict part"},
        "bare string",
    ]
    view = loopguard.message_view(Msg("user", parts))
    assert view["content"][0] == {"type": "text", "text": "你好"}
    assert view["content"][1] == {"type": "text", "text": "想一想"}
    # 图片只留类型，不复制 base64
    assert view["content"][2] == {"type": "image_url"}
    assert view["content"][3] == {"type": "audio"}
    assert view["content"][4] == {"type": "text", "text": "dict part"}
    assert view["content"][5] == "bare string"
    est = tokens.estimate_message(view)
    assert est >= tokens.IMAGE_TOKENS + tokens.AUDIO_TOKENS


def test_message_view_tool_calls_objects_and_dicts():
    view = loopguard.message_view(Msg("assistant", None, [ToolCallObj("fetch", '{"u": 1}')]))
    assert view["tool_calls"] == [{"function": {"name": "fetch", "arguments": '{"u": 1}'}}]
    raw = {"id": "1", "function": {"name": "a", "arguments": "{}"}}
    assert loopguard.message_view(Msg("assistant", None, [raw]))["tool_calls"] == [raw]
    assert loopguard.message_view(Msg("assistant", "hi"))["tool_calls"] is None
    assert loopguard.message_view(Msg("assistant", "hi", []))["tool_calls"] is None


def test_message_view_tolerates_odd_content():
    assert loopguard.message_view(Msg("user", None))["content"] is None
    assert loopguard.message_view(Msg("user", 123))["content"] == "123"


# ---------------------------------------------------------------- 工具返回值


def test_describe_result_shapes():
    assert loopguard.describe_result(None) == ("", 0)
    assert loopguard.describe_result("plain") == ("plain", 0)
    assert loopguard.describe_result(object()) == ("", 0)
    assert loopguard.describe_result(SimpleNamespace(content=None)) == ("", 0)


def test_describe_result_call_tool_result():
    result = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text="第一段"),
            SimpleNamespace(type="image", data="AAAA", mimeType="image/png"),
            SimpleNamespace(type="resource", resource=SimpleNamespace(text="资源正文")),
            SimpleNamespace(type="resource", resource=SimpleNamespace(blob="BBBB")),
            SimpleNamespace(type="text", text="第二段"),
        ]
    )
    text, images = loopguard.describe_result(result)
    assert text == "第一段\n\n资源正文\n\n第二段"
    assert images == 1


# ---------------------------------------------------------------- 就地清理


def test_set_cleared_dict_and_object():
    tool = _tool(500)
    assert loopguard._set_cleared(tool) is True and tool["content"] == CLEARED
    other = {"role": "assistant", "content": "keep"}
    assert loopguard._set_cleared(other) is False and other["content"] == "keep"
    obj = Msg("tool", "r" * 500, tool_call_id="c0")
    assert loopguard._set_cleared(obj) is True
    assert obj.content == CLEARED and obj.tool_call_id == "c0"
    user = Msg("user", "hello")
    assert loopguard._set_cleared(user) is False and user.content == "hello"


def test_set_cleared_readonly_object_is_skipped():
    class Frozen:
        role = "tool"

        def __setattr__(self, name, value):
            raise AttributeError("read only")

    assert loopguard._set_cleared(Frozen()) is False


# ---------------------------------------------------------------- 清理规划


def _plan3(views, total, *, target, limit, hard, keep_rounds=2, ceiling=None):
    return loopguard.plan_clearing(
        views,
        total=total,
        target=target,
        limit=limit,
        hard=hard,
        estimator=_est(),
        keep_rounds=keep_rounds,
        ceiling=ceiling,
    )


def _plan(views, total, *, target, limit, hard, keep_rounds=2):
    chosen, remaining, futile = _plan3(
        views, total, target=target, limit=limit, hard=hard, keep_rounds=keep_rounds
    )
    assert futile is False  # 没给 ceiling 时永远不会判徒劳
    return chosen, remaining


def test_plan_noop_under_limit():
    views = _flat(*[_round(i, 1000) for i in range(5)])
    assert _plan(views, 900, target=500, limit=1000, hard=2000) == ([], 900)
    assert _plan(views, 1000, target=500, limit=1000, hard=2000) == ([], 1000)


def test_plan_clears_oldest_first_until_target():
    views = _flat(*[_round(i, 1000) for i in range(6)])
    # 受保护的是最近 2 轮（第 4、5 轮），可清的是第 0–3 轮
    gain = _gain(1000)
    total = 1500
    chosen, remaining = _plan(views, total, target=total - 2 * gain, limit=1000, hard=10_000)
    assert chosen == [3, 5]  # 第 0、1 轮的结果：由旧到新
    assert remaining == total - 2 * gain


def test_plan_stops_as_soon_as_target_reached():
    views = _flat(*[_round(i, 1000) for i in range(6)])
    gain = _gain(1000)
    chosen, remaining = _plan(views, 1500, target=1500 - gain, limit=1000, hard=10_000)
    assert chosen == [3]
    assert remaining == 1500 - gain


def test_plan_spares_recent_rounds_below_hard_line():
    views = _flat(*[_round(i, 1000) for i in range(2)])
    # 只有 2 轮，全部算最近几轮；总量超线但没到 hard：一条都不动
    assert _plan(views, 1500, target=500, limit=1000, hard=2000) == ([], 1500)


def test_plan_dips_into_recent_rounds_near_hard_line():
    # 下标：0 system、1 user、2/3 第 0 轮、4/5 第 1 轮；只有 2 轮，全部算最近几轮
    views = _flat(*[_round(i, 1000) for i in range(2)])
    gain = _gain(1000)
    # 没到 hard：一条都不动
    assert _plan(views, 1200, target=500, limit=1000, hard=2000) == ([], 1200)
    # 越过 hard 且一条清完仍高于 limit：两条都清，由旧到新
    chosen, remaining = _plan(views, 2100, target=500, limit=1000, hard=2000)
    assert chosen == [3, 5] and remaining == 2100 - 2 * gain
    # 清一条就降到 limit 以下：只清最早的一条
    chosen, remaining = _plan(views, 2100, target=500, limit=1900, hard=2000)
    assert chosen == [3] and remaining == 2100 - gain


def test_plan_dips_only_as_far_as_limit():
    # 下标：2/3、4/5、6/7 三轮；最近 2 轮是后两轮，第 0 轮的结果（3）算更早的
    views = _flat(*[_round(i, 1000) for i in range(3)])
    gain = _gain(1000)
    total = 2200
    chosen, remaining = _plan(views, total, target=2000, limit=1000, hard=1500)
    assert chosen == [3, 5, 7] and remaining == total - 3 * gain
    # limit 放宽到清完第二条就够了：不再往下清
    chosen, remaining = _plan(views, total, target=2000, limit=1700, hard=1500)
    assert chosen == [3, 5] and remaining == total - 2 * gain


def test_plan_skips_short_cleared_and_non_tool():
    views = _base()  # 0 system、1 user
    views += _round(0, 100)  # 2/3：太短
    views += [_call("c1"), {"role": "tool", "tool_call_id": "c1", "content": CLEARED}]  # 4/5：已清
    views += _round(2, 1000)  # 6/7
    views += _round(3, 1000)  # 8/9：最近 2 轮从这里开始
    views += _round(4, 1000)  # 10/11
    chosen, _ = _plan(views, 5000, target=0, limit=1000, hard=10_000)
    assert chosen == [7]


def test_plan_orphan_tool_counts_as_older():
    views = _base() + [_tool(1000, "orphan")] + _round(1, 1000) + _round(2, 1000)
    chosen, _ = _plan(views, 5000, target=0, limit=1000, hard=10_000)
    assert chosen == [2]


def test_plan_keep_rounds_zero_protects_nothing():
    views = _flat(*[_round(i, 1000) for i in range(2)])
    chosen, _ = _plan(views, 5000, target=0, limit=1000, hard=10_000, keep_rounds=0)
    assert chosen == [3, 5]


def test_plan_ignores_non_dict_entries():
    views = [None, "oops"] + _flat(*[_round(i, 1000) for i in range(4)])
    chosen, _ = _plan(views, 5000, target=0, limit=1000, hard=10_000)
    assert all(isinstance(views[i], dict) and views[i]["role"] == "tool" for i in chosen)


def test_plan_futile_when_clearing_everything_still_exceeds_ceiling():
    views = _flat(*[_round(i, 1000) for i in range(6)])
    gain = _gain(1000)
    total = 50_000
    # 6 条结果全清也只省 6 * gain，远不够压进 ceiling：判徒劳，一条都不清
    chosen, remaining, futile = _plan3(
        views, total, target=100, limit=1000, hard=1500, ceiling=20_000
    )
    assert (chosen, remaining, futile) == ([], total, True)
    assert 6 * gain < total - 20_000


def test_plan_not_futile_when_clearing_can_reach_ceiling():
    views = _flat(*[_round(i, 1000) for i in range(6)])
    gain = _gain(1000)
    total = 3000
    # 全清后 total - 6 * gain 低于 ceiling：照常清理
    chosen, remaining, futile = _plan3(
        views, total, target=2000, limit=2500, hard=1e9, ceiling=total - 6 * gain + 1
    )
    assert futile is False
    needed = -(-(total - 2000) // gain)  # 降到 target 需要清的条数（向上取整）
    assert 0 < needed < 6
    assert chosen == [3, 5, 7, 9, 11, 13][:needed]
    assert remaining == total - needed * gain <= 2000


def test_plan_futility_counts_recent_rounds_too():
    # 只有 2 轮，全在保护范围内；全清（含最近几轮）恰好够到 ceiling 就不算徒劳
    views = _flat(*[_round(i, 1000) for i in range(2)])
    gain = _gain(1000)
    total = 2100
    _, _, futile = _plan3(
        views, total, target=500, limit=1000, hard=2000, ceiling=total - 2 * gain
    )
    assert futile is False
    _, _, futile = _plan3(
        views, total, target=500, limit=1000, hard=2000, ceiling=total - 2 * gain - 1
    )
    assert futile is True


def test_plan_under_limit_is_never_futile():
    views = _flat(*[_round(i, 1000) for i in range(3)])
    assert _plan3(views, 900, target=500, limit=1000, hard=2000, ceiling=10) == ([], 900, False)


# ---------------------------------------------------------------- 守卫


def _guard(**overrides):
    params = dict(limit=1000, target=600, hard=1400, tools_cost=0, ratio=1.0)
    params.update(overrides)
    return LoopGuard(**params)


def test_for_window_derives_lines_from_compress_settings():
    guard = LoopGuard.for_window(
        100_000, threshold=0.7, target_ratio=0.535, tools_cost=1234, ratio=1.2
    )
    assert guard.limit == 70_000
    assert guard.target == 53_500
    assert guard.hard == int(100_000 * loopguard.HARD_RATIO)
    assert guard.hard > guard.limit
    assert guard.tools_cost == 1234 and guard.ratio == 1.2
    assert guard.ceiling == 100_000


def test_ceiling_is_optional_and_clamped():
    assert LoopGuard(limit=10, target=5, hard=20).ceiling is None
    assert LoopGuard(limit=10, target=5, hard=20, ceiling=-3).ceiling == 0


def test_for_window_hard_never_below_limit():
    guard = LoopGuard.for_window(
        100_000, threshold=0.8, target_ratio=0.6, tools_cost=0, ratio=1.0
    )
    assert guard.hard >= guard.limit


def test_guard_clamps_target_to_limit():
    guard = LoopGuard(limit=500, target=900, hard=100)
    assert guard.target == 500 and guard.hard == 500


def test_after_tool_under_limit_is_noop():
    messages = _flat(_round(0, 500))
    snapshot = [dict(m) for m in messages]
    guard = _guard()
    assert guard.after_tool(messages, name="fetch", args={}, result_text="r" * 300) is None
    assert messages == snapshot
    assert guard.cleared_total == 0


def test_after_tool_clears_older_results_in_place():
    messages = _flat(*[_round(i, 1000) for i in range(6)])
    assistants = [m for m in messages if m["role"] == "assistant"]
    guard = _guard(limit=1000, target=700, hard=1e9)
    outcome = guard.after_tool(messages, name="fetch", args={"u": 1}, result_text="r" * 1000)
    assert isinstance(outcome, GuardOutcome)
    assert outcome.cleared >= 1 and outcome.before > 1000 and outcome.after < outcome.before
    cleared_ids = [m["tool_call_id"] for m in messages if m["role"] == "tool" and m["content"] == CLEARED]
    assert cleared_ids == [f"c{i}" for i in range(len(cleared_ids))]  # 由旧到新、连续
    assert "c5" not in cleared_ids and "c4" not in cleared_ids  # 最近 2 轮不动
    # 配对结构和其余消息纹丝不动
    assert [m for m in messages if m["role"] == "assistant"] == assistants
    assert [m["tool_call_id"] for m in messages if m["role"] == "tool"] == [f"c{i}" for i in range(6)]
    assert messages[0]["content"] == "persona" and messages[1]["role"] == "user"
    assert guard.cleared_total == outcome.cleared


def test_after_tool_works_on_objects_like_pydantic_messages():
    objects = _as_objects(_flat(*[_round(i, 1000) for i in range(6)]))
    dict_guard, obj_guard = _guard(hard=1e9), _guard(hard=1e9)
    dict_outcome = dict_guard.after_tool(
        _flat(*[_round(i, 1000) for i in range(6)]), name="fetch", args={}, result_text="r" * 500
    )
    obj_outcome = obj_guard.after_tool(objects, name="fetch", args={}, result_text="r" * 500)
    assert obj_outcome is not None and dict_outcome is not None
    assert (obj_outcome.cleared, obj_outcome.before, obj_outcome.after) == (
        dict_outcome.cleared,
        dict_outcome.before,
        dict_outcome.after,
    )
    cleared = [m for m in objects if m.role == "tool" and m.content == CLEARED]
    assert len(cleared) == obj_outcome.cleared
    assert all(m.tool_call_id for m in objects if m.role == "tool")


def test_batch_accumulates_across_tools_of_one_step():
    messages = _flat(*[_round(i, 1000) for i in range(5)])
    guard = _guard(limit=2600, target=1500, hard=1e9)
    est = _est()
    base = est.messages(messages)
    assert base < 2600
    # 第一个工具的结果还不足以越线，第二个叠上去才越线
    first = guard.after_tool(messages, name="a", args={}, result_text="r" * 2000)
    assert first is None
    second = guard.after_tool(messages, name="b", args={}, result_text="r" * 2000)
    assert second is not None and second.cleared >= 1


def test_batch_resets_after_it_joins_the_list():
    messages = _base()
    guard = _guard(limit=1000, target=600, hard=1e9)
    # 第 1 步：两个工具各约 300 token，合计未越线
    assert guard.after_tool(messages, name="a", args={}, result_text="r" * 1000) is None
    assert guard.after_tool(messages, name="b", args={}, result_text="r" * 1000) is None
    # 上一批并入列表（运行器在整批跑完后才追加）
    messages.extend([_call("a"), _tool(1000, "a"), _tool(1000, "b")])
    # 第 2 步：一个很小的结果。若批次没有清零，会把上一批重复算一遍而误触发
    assert guard.after_tool(messages, name="c", args={}, result_text="r" * 100) is None


def _tool_ids(messages, *, cleared):
    def content(m):
        return m["content"] if isinstance(m, dict) else m.content

    def call_id(m):
        return m["tool_call_id"] if isinstance(m, dict) else m.tool_call_id

    def role(m):
        return m["role"] if isinstance(m, dict) else m.role

    return {
        call_id(m)
        for m in messages
        if role(m) == "tool" and (content(m) == CLEARED) is cleared
    }


@pytest.mark.parametrize("as_objects", [False, True], ids=["dict", "object"])
def test_second_check_in_same_step_sees_the_cleared_list(as_objects):
    raw = _flat(*[_round(i, 1000) for i in range(6)])
    messages = _as_objects(raw) if as_objects else raw
    # 6 轮 + 本批约 2200 token：清掉最早的 3 条就降到目标（1400）以下，后面 3 条留着
    guard = _guard(limit=2000, target=1400, hard=1e9)
    first = guard.after_tool(messages, name="a", args={}, result_text="r" * 1000)
    assert first is not None and first.cleared == 3 and first.after <= 1400
    cleared_after_first = _tool_ids(messages, cleared=True)
    assert cleared_after_first == {"c0", "c1", "c2"}

    # 清完已降到目标水位：同一步里再来一个很小的结果，不该再动手
    second = guard.after_tool(messages, name="b", args={}, result_text="r" * 10)
    assert second is None
    assert _tool_ids(messages, cleared=True) == cleared_after_first
    assert guard.cleared_total == first.cleared


def test_cleared_results_are_never_cleared_again(monkeypatch):
    # 占位说明本来就短于下限；即便将来下限被调低，也不能重复清理已清的结果
    monkeypatch.setattr(compress, "TOOL_RESULT_CLEAR_MIN_CHARS", 0)
    views = _base() + [
        _call("c0"),
        {"role": "tool", "tool_call_id": "c0", "content": CLEARED},
    ] + _round(1, 1000) + _round(2, 1000) + _round(3, 1000)
    chosen, _ = _plan(views, 5000, target=0, limit=1000, hard=10_000)
    assert 3 not in chosen
    assert chosen == [5]


def test_tools_cost_and_ratio_count():
    def fresh():
        return _flat(_round(0, 1000), _round(1, 1000), _round(2, 1000))

    def run(**overrides):
        guard = _guard(limit=2500, **overrides)
        return guard.after_tool(fresh(), name="a", args={}, result_text="r" * 100)

    assert run() is None  # 约 1000 token，远低于线
    assert run(tools_cost=5000) is not None  # 工具声明占用计入
    assert run(ratio=3.0) is not None  # 校准比例放大估算（约 3000）


def test_images_count_toward_batch():
    def fresh():
        return _flat(_round(0, 1000), _round(1, 1000), _round(2, 1000))

    # 列表约 950 token：没有图片远低于线，两张图（2 × 765）把它推过线
    plain = _guard(limit=2000, target=1500, hard=1e9)
    assert plain.after_tool(fresh(), name="a", args={}, result_text="x") is None
    with_images = _guard(limit=2000, target=1500, hard=1e9)
    outcome = with_images.after_tool(fresh(), name="a", args={}, result_text="x", images=2)
    assert outcome is not None and outcome.before > 2000


def test_stuck_reported_once():
    # 全是短结果、没有可清的：越线也只能放弃，且只报告一次
    messages = _flat(_round(0, 100), _round(1, 100))
    guard = _guard(limit=300, target=100, hard=400)
    first = guard.after_tool(messages, name="a", args={}, result_text="r" * 2000)
    assert first is not None and first.cleared == 0 and first.stuck is True
    second = guard.after_tool(messages, name="b", args={}, result_text="r" * 2000)
    assert second is not None and second.cleared == 0 and second.stuck is False
    assert guard.cleared_total == 0


def test_after_tool_rejects_bad_input():
    guard = _guard()
    assert guard.after_tool(None, name="a", args={}, result_text="x") is None
    assert guard.after_tool([], name="a", args={}, result_text="x") is None
    assert guard.after_tool(tuple(_base()), name="a", args={}, result_text="x") is None


def test_args_text_variants():
    assert loopguard._args_text(None) == ""
    assert loopguard._args_text({}) == ""
    assert loopguard._args_text({"q": "中文"}) == '{"q": "中文"}'
    assert loopguard._args_text({"s": {1, 2}}) == '{"s": "{1, 2}"}'  # 不可序列化的值转字符串
    loop = {}
    loop["self"] = loop
    assert loopguard._args_text(loop) == str(loop)  # 循环引用：退回 str


def test_repr_is_short_even_with_huge_run_context():
    guard = _guard()
    guard.run_context = SimpleNamespace(messages=[{"role": "user", "content": "SECRET" * 100_000}])
    text = repr(guard)
    assert "SECRET" not in text and len(text) < 200
    assert f"limit={guard.limit}" in text


def test_guard_constants_stay_below_builtin_line():
    assert loopguard.HARD_RATIO < compress.BUILTIN_THRESHOLD
    assert loopguard.LOOP_KEEP_ROUNDS >= 1


# ---------------------------------------------------------------- 多步循环模拟


def _tokens_of_next_request(messages, tools_cost):
    return tools_cost + _est().messages(messages)


def _run_loop(guard, messages, *, steps, chars, tools_per_step=1, tools_cost=0):
    """像运行器那样推进循环：每步所有工具返回后（每个都过一次守卫）才并入列表。

    返回每一步并入之后「下一次请求」的估算大小列表。
    """
    sizes = []
    for step in range(steps):
        ids = [f"s{step}t{k}" for k in range(tools_per_step)]
        for call_id in ids:
            guard.after_tool(messages, name="fetch", args={"u": call_id}, result_text="r" * chars)
        assistant = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": i, "type": "function", "function": {"name": "fetch", "arguments": json_args(i)}}
                for i in ids
            ],
        }
        messages.append(assistant)
        messages.extend({"role": "tool", "tool_call_id": i, "content": "r" * chars} for i in ids)
        sizes.append(_tokens_of_next_request(messages, tools_cost))
    return sizes


def json_args(call_id):
    return '{"u": "%s"}' % call_id


def _paired(messages):
    """每条 tool 消息都有更早的 assistant tool_call 与之配对。"""
    seen = set()
    for m in messages:
        if m["role"] == "assistant":
            seen.update(c["id"] for c in m.get("tool_calls") or [])
        elif m["role"] == "tool":
            if m["tool_call_id"] not in seen:
                return False
    return True


def test_long_loop_stays_under_the_builtin_line():
    window, tools_cost = 20_000, 800
    guard = LoopGuard.for_window(
        window, threshold=0.7, target_ratio=0.535, tools_cost=tools_cost, ratio=1.0
    )
    # 不带守卫：每步约 1.2k token，20 步涨到 25k，越过整个窗口
    bare = _flat()
    bare_sizes = _run_loop(_NoGuard(), bare, steps=20, chars=4000, tools_cost=tools_cost)
    assert bare_sizes[-1] > window

    guarded = _flat()
    sizes = _run_loop(guard, guarded, steps=20, chars=4000, tools_cost=tools_cost)
    builtin_line = int(window * compress.BUILTIN_THRESHOLD)
    assert max(sizes) < builtin_line, sizes
    assert guard.cleared_total > 0
    assert _paired(guarded)
    # 最近 2 轮的结果完好（每轮 [assistant, tool]，所以是倒数第 1、第 3 条）
    recent = [m for m in guarded if m["role"] == "tool"][-2:]
    assert [m["content"] for m in recent] == ["r" * 4000, "r" * 4000]


def test_parallel_tools_per_step_stay_under_the_line():
    window, tools_cost = 20_000, 800
    guard = LoopGuard.for_window(
        window, threshold=0.7, target_ratio=0.535, tools_cost=tools_cost, ratio=1.0
    )
    messages = _flat()
    sizes = _run_loop(guard, messages, steps=8, chars=2500, tools_per_step=3, tools_cost=tools_cost)
    assert max(sizes) < int(window * compress.BUILTIN_THRESHOLD), sizes
    assert _paired(messages)


def test_one_huge_result_rescued_by_clearing_the_previous_one():
    # 短对话 + 两个各占窗口近一半的结果：第二个返回时，上一个（最近一轮）
    # 已被模型读过，只有它能让位——越过 hard 线才允许动最近几轮
    window = 20_000
    guard = LoopGuard.for_window(
        window, threshold=0.7, target_ratio=0.535, tools_cost=500, ratio=1.0
    )
    messages = _flat()
    sizes = _run_loop(guard, messages, steps=2, chars=28_000, tools_cost=500)
    assert sizes[0] < int(window * 0.7)
    assert sizes[1] < int(window * compress.BUILTIN_THRESHOLD)
    tools = [m for m in messages if m["role"] == "tool"]
    assert tools[0]["content"] == CLEARED and tools[1]["content"] == "r" * 28_000
    assert _paired(messages)


class _NoGuard:
    """不做任何事的替身：对照组。"""

    def after_tool(self, *args, **kwargs):
        return None


def test_futile_leaves_messages_untouched_and_reports_once():
    messages = _flat(*[_round(i, 1000) for i in range(6)])
    snapshot = [dict(m) for m in messages]
    guard = _guard(limit=1000, target=700, hard=1400, ceiling=2000)
    # 本批一个结果就远超 ceiling：清光较早的也压不进去
    first = guard.after_tool(messages, name="a", args={}, result_text="r" * 30_000)
    assert first is not None
    assert first.futile is True and first.stuck is True and first.cleared == 0
    assert messages == snapshot and guard.cleared_total == 0
    second = guard.after_tool(messages, name="b", args={}, result_text="r" * 30_000)
    assert second is not None and second.futile is True and second.stuck is False
    assert messages == snapshot


def test_not_futile_when_the_batch_fits_after_clearing():
    messages = _flat(*[_round(i, 1000) for i in range(6)])
    guard = _guard(limit=1000, target=700, hard=1400, ceiling=5000)
    outcome = guard.after_tool(messages, name="a", args={}, result_text="r" * 1000)
    assert outcome is not None and outcome.futile is False and outcome.cleared >= 1
