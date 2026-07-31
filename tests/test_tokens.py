import math

from core import tokens


def test_estimate_text_english():
    assert tokens.estimate_text("hello") == math.ceil(5 * tokens.OTHER_WEIGHT)


def test_estimate_text_chinese():
    assert tokens.estimate_text("你好") == math.ceil(2 * tokens.CHINESE_WEIGHT)


def test_estimate_text_mixed_and_empty():
    assert tokens.estimate_text("") == 0
    mixed = tokens.estimate_text("你好ab")
    assert mixed == math.ceil(2 * tokens.CHINESE_WEIGHT + 2 * tokens.OTHER_WEIGHT)


def test_estimate_content_multimodal():
    content = [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "http://x"}},
        {"type": "input_audio", "input_audio": {}},
    ]
    expected = (
        tokens.estimate_text("hello") + tokens.IMAGE_TOKENS + tokens.AUDIO_TOKENS
    )
    assert tokens.estimate_content(content) == expected


def test_estimate_message_counts_tool_calls():
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"function": {"name": "memory_read", "arguments": '{"file": "a.md"}'}}
        ],
    }
    assert tokens.estimate_message(message) > tokens.MESSAGE_OVERHEAD


def test_estimate_messages_skips_non_dict():
    assert tokens.estimate_messages([{"role": "user", "content": "hi"}, "junk"]) == \
        tokens.estimate_message({"role": "user", "content": "hi"})


def test_estimator_calibrate_moves_toward_observed():
    est = tokens.TokenEstimator()
    est.calibrate(estimated=100, actual=200)
    assert 1.0 < est.ratio <= 2.0


def test_estimator_calibrate_clamped():
    est = tokens.TokenEstimator()
    for _ in range(50):
        est.calibrate(estimated=1, actual=10_000)
    assert est.ratio <= tokens.CALIBRATION_MAX
    est2 = tokens.TokenEstimator()
    for _ in range(50):
        est2.calibrate(estimated=10_000, actual=1)
    assert est2.ratio >= tokens.CALIBRATION_MIN


def test_estimator_bad_init_ratio_resets():
    assert tokens.TokenEstimator(float("nan")).ratio == 1.0
    assert tokens.TokenEstimator(-5).ratio == 1.0


def test_estimator_scales_text():
    est = tokens.TokenEstimator(2.0)
    assert est.text("hello") == math.ceil(tokens.estimate_text("hello") * 2.0)
