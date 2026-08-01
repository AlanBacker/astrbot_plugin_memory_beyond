from core import compress, tokens
from core.compress import (
    BUILTIN_THRESHOLD,
    COOLDOWN_BASE_SECONDS,
    COOLDOWN_MAX_SECONDS,
    THRESHOLD_FALLBACK,
    THRESHOLD_MAX,
    SessionState,
    fingerprint,
    plan_compression,
    state_matches,
    validate_threshold,
)


def _history(turn_count=4):
    messages = [{"role": "system", "content": "persona"}]
    for i in range(turn_count):
        messages.append({"role": "user", "content": f"q{i}"})
        messages.append({"role": "assistant", "content": f"a{i}"})
    return messages


# ---------------------------------------------------------------- 阈值校验


def test_validate_threshold_passes_valid():
    value, warning = validate_threshold(0.7)
    assert value == 0.7 and warning == ""


def test_validate_threshold_clamps_at_builtin():
    value, warning = validate_threshold(BUILTIN_THRESHOLD)
    assert value == THRESHOLD_MAX and "0.82" in warning
    value, _ = validate_threshold(0.95)
    assert value == THRESHOLD_MAX


def test_validate_threshold_fallback_on_garbage():
    assert validate_threshold("abc")[0] == THRESHOLD_FALLBACK
    assert validate_threshold(0)[0] == THRESHOLD_FALLBACK
    assert validate_threshold(float("nan"))[0] == THRESHOLD_FALLBACK


# ---------------------------------------------------------------- 指纹与锚点


def test_fingerprint_stable_and_sensitive():
    message = {"role": "user", "content": "hello"}
    assert fingerprint(message) == fingerprint(dict(message))
    assert fingerprint(message) != fingerprint({"role": "user", "content": "hell0"})


def test_state_matches():
    contexts = _history()
    state = SessionState(cid="c1")
    assert state_matches(state, "c1", contexts)  # 未压缩过，仅比对 cid
    assert not state_matches(state, "c2", contexts)

    state.watermark = 3
    state.anchor = fingerprint(contexts[2])
    assert state_matches(state, "c1", contexts)

    state.anchor = "bogus"
    assert not state_matches(state, "c1", contexts)

    state.watermark = len(contexts) + 5
    assert not state_matches(state, "c1", contexts)


# ---------------------------------------------------------------- 压缩规划


def test_plan_compression_keeps_recent_turns():
    contexts = _history(turn_count=4)  # system + 4 轮
    plan = plan_compression(contexts, watermark=0, keep_recent_turns=3)
    assert plan is not None
    assert plan.new_watermark == 3  # 第二轮的 user 下标
    # system 消息也在 to_summarize 里，但 render_transcript 会跳过
    assert [m["role"] for m in plan.to_summarize] == ["system", "user", "assistant"]


def test_plan_compression_none_when_too_few_turns():
    assert plan_compression(_history(3), watermark=0, keep_recent_turns=3) is None


def test_plan_compression_rolls_forward():
    contexts = _history(6)
    first = plan_compression(contexts, watermark=0, keep_recent_turns=3)
    # 剩余轮数不超过保留数时不再压缩
    assert plan_compression(contexts, first.new_watermark, keep_recent_turns=3) is None
    # 对话继续增长后，从水位线接着滚动
    for i in range(6, 9):
        contexts.append({"role": "user", "content": f"q{i}"})
        contexts.append({"role": "assistant", "content": f"a{i}"})
    second = plan_compression(contexts, first.new_watermark, keep_recent_turns=3)
    assert second is not None
    assert second.new_watermark > first.new_watermark
    assert second.to_summarize[0] == contexts[first.new_watermark]


def test_plan_compression_keep_at_least_one_turn():
    contexts = _history(4)
    plan = plan_compression(contexts, watermark=0, keep_recent_turns=0)
    assert plan is not None
    assert plan.new_watermark == 7  # 最后一轮 user 的下标，最新一轮保留原文


def test_apply_summary_advances_state():
    contexts = _history(4)
    state = SessionState(cid="c1", fail_count=2, fail_until=9e9)
    plan = plan_compression(contexts, 0, 3)
    compress.apply_summary(state, contexts, plan, "  摘要内容  ")
    assert state.summary == "摘要内容"
    assert state.watermark == plan.new_watermark
    assert state.anchor == fingerprint(contexts[plan.new_watermark - 1])
    assert state.fail_count == 0 and state.fail_until == 0.0
    assert state_matches(state, "c1", contexts)


# ---------------------------------------------------------------- 失败退避


def test_record_failure_exponential_backoff():
    state = SessionState()
    state.record_failure(now=1000.0)
    assert state.fail_until == 1000.0 + COOLDOWN_BASE_SECONDS
    state.record_failure(now=1000.0)
    assert state.fail_until == 1000.0 + COOLDOWN_BASE_SECONDS * 2
    for _ in range(10):
        state.record_failure(now=1000.0)
    assert state.fail_until == 1000.0 + COOLDOWN_MAX_SECONDS
    assert state.in_cooldown(1000.0)
    assert not state.in_cooldown(1000.0 + COOLDOWN_MAX_SECONDS + 1)


# ---------------------------------------------------------------- 状态序列化


def test_state_roundtrip():
    state = SessionState(
        cid="c1",
        watermark=5,
        summary="s",
        anchor="a",
        ratio=1.2,
        last_estimate=6034,
        cache_hit_tokens=321,
    )
    restored = SessionState.from_dict(state.to_dict())
    assert restored == state


def test_state_from_garbage():
    state = SessionState.from_dict({"watermark": "abc", "ratio": "x"})
    assert state.watermark == 0 and state.ratio == 1.0
    assert SessionState.from_dict("junk") == SessionState()


def test_state_cache_hit_default_and_clamp():
    assert SessionState.from_dict({}).cache_hit_tokens == -1
    assert SessionState.from_dict({"cache_hit_tokens": -99}).cache_hit_tokens == -1
    assert SessionState.from_dict({"last_estimate": -5}).last_estimate == 0


# ---------------------------------------------------------------- 应急裁剪


def test_fit_tail_under_budget_untouched():
    tail = _history(2)[1:]
    estimator = tokens.TokenEstimator()
    fitted, clipped = compress.fit_tail_to_budget(tail, estimator, 100_000)
    assert fitted == tail and not clipped


def test_fit_tail_halves_old_turns():
    tail = []
    for i in range(4):
        tail.append({"role": "user", "content": "x" * 1000})
        tail.append({"role": "assistant", "content": "y" * 1000})
    estimator = tokens.TokenEstimator()
    one_turn = estimator.messages(tail[:2])
    fitted, clipped = compress.fit_tail_to_budget(tail, estimator, one_turn + 10)
    assert clipped
    assert len(fitted) == 2
    assert fitted[0]["content"] == tail[-2]["content"]  # 保住最新一轮


def test_fit_tail_clips_single_huge_turn():
    tail = [
        {"role": "user", "content": "z" * 50_000},
        {"role": "assistant", "content": "ok"},
    ]
    estimator = tokens.TokenEstimator()
    fitted, clipped = compress.fit_tail_to_budget(tail, estimator, 500)
    assert clipped
    assert len(fitted) == 2
    assert "应急裁剪" in fitted[0]["content"]
    assert len(fitted[0]["content"]) < 50_000
    assert tail[0]["content"] == "z" * 50_000  # 原列表不被改动


# ---------------------------------------------------------------- 转写渲染


def test_render_transcript():
    messages = [
        {"role": "system", "content": "skip me"},
        {"role": "user", "content": "问题"},
        {
            "role": "assistant",
            "content": "回答",
            "tool_calls": [
                {"function": {"name": "memory_read", "arguments": '{"f": 1}'}}
            ],
        },
        {"role": "tool", "tool_call_id": "t", "content": "tool output"},
    ]
    text = compress.render_transcript(messages)
    assert "skip me" not in text
    assert "用户：问题" in text
    assert "memory_read" in text
    assert "工具结果：tool output" in text


def test_render_transcript_truncates_long_tool_output():
    messages = [{"role": "tool", "content": "x" * 10_000}]
    text = compress.render_transcript(messages)
    assert len(text) < 3000 and "截断" in text
