from core import recall
from core.memstore import MemoryHeader
from core.recall import Candidate, Recalled

NOW = 1_800_000_000.0
DAY = 86400


def header(file, description="", body="", mtime=NOW, mem_type="user"):
    name = file[:-3]
    text = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}".lower()
    return MemoryHeader(
        file=file,
        name=name,
        description=description,
        type=mem_type,
        mtime=mtime,
        size=len(text),
        text=text,
    )


def cand(file, description="", body="", scope="session", mtime=NOW):
    return Candidate(scope, header(file, description, body, mtime))


# ---------------------------------------------------------------- 分词与打分


def test_tokenize_mixes_words_and_cjk_bigrams():
    tokens = recall.tokenize("Golang 后端工程师 2024 123456789")
    assert "golang" in tokens
    assert {"后端", "端工", "工程", "程师"} <= tokens
    assert "2024" not in tokens  # 短数字噪声大，不计
    assert "123456789" in tokens  # 像 ID 的长数字保留


def test_score_weights_description_over_body():
    query = recall.tokenize("周六开黑")
    in_desc = header("a.md", description="每周六晚上的开黑活动")
    in_body = header("b.md", description="无关", body="每周六晚上的开黑活动")
    assert recall.score(query, in_desc) > recall.score(query, in_body) > 0
    assert recall.score(set(), in_desc) == 0


def test_identity_files_dedupes_and_keeps_order():
    files = recall.identity_files("123456", ["654321", "123456"], "提到 777777 和 12")
    assert files == ["user-123456.md", "user-654321.md", "user-777777.md"]
    assert recall.identity_files("", [], "") == []
    assert recall.identity_files("abc", [], "") == []


# ---------------------------------------------------------------- 关键词挑选


def test_select_by_keywords_forced_first_then_scored():
    cands = [
        cand("project-game.md", "每周六晚上的开黑活动"),
        cand("user-123456.md", "群主阿杰"),
        cand("reference-rules.md", "群规公告的位置"),
    ]
    chosen = recall.select_by_keywords(cands, "这周六还开黑吗", ["user-123456.md"], 5)
    assert [c.header.file for c in chosen] == ["user-123456.md", "project-game.md"]


def test_select_by_keywords_never_forces_global_user_files():
    cands = [cand("user-123456.md", "x", scope="global")]
    assert recall.select_by_keywords(cands, "", ["user-123456.md"], 5) == []


def test_select_by_keywords_respects_limit_and_prefers_newer_on_tie():
    old = cand("a.md", "开黑活动安排", mtime=NOW - 10 * DAY)
    new = cand("b.md", "开黑活动安排", mtime=NOW)
    chosen = recall.select_by_keywords([old, new], "开黑活动", [], 1)
    assert chosen == [new]
    assert recall.select_by_keywords([old, new], "开黑活动", [], 0) == []


def test_select_by_keywords_ignores_weak_body_matches():
    weak = cand("a.md", "无关描述", body="开黑")
    assert recall.select_by_keywords([weak], "开黑", [], 5) == []


# ---------------------------------------------------------------- 模型挑选


def test_selector_prompt_lists_refs_and_age():
    cands = [cand("user-1.md", "阿杰", mtime=NOW - 3 * DAY)]
    prompt = recall.build_selector_prompt(cands, "你好", 3, now=NOW)
    assert "1. session/user-1.md｜user｜3 天前｜阿杰" in prompt
    assert "最多 3 条" in prompt


def test_parse_selector_response_accepts_refs_and_bare_names():
    cands = [cand("a.md"), cand("b.md", scope="global")]
    chosen = recall.parse_selector_response(
        '好的：["session/a.md", "b.md", "missing.md", "session/a.md"]', cands
    )
    assert [c.ref for c in chosen] == ["session/a.md", "global/b.md"]
    # 答非所问 / 不是数组 / 空回复：返回 None，调用方退回关键词模式
    assert recall.parse_selector_response("没有 JSON", cands) is None
    assert recall.parse_selector_response("抱歉，我无法判断。", cands) is None
    assert recall.parse_selector_response('{"a": 1}', cands) is None
    assert recall.parse_selector_response("", cands) is None
    # 明确选了空数组：模型认为没有相关的，不退回关键词
    assert recall.parse_selector_response("[]", cands) == []


# ---------------------------------------------------------------- 注入块


def test_freshness_note_only_for_older_than_a_day():
    assert recall.freshness_note(NOW, now=NOW) == ""
    assert recall.freshness_note(NOW - DAY, now=NOW) == ""
    assert "3 天前" in recall.freshness_note(NOW - 3 * DAY, now=NOW)


def test_age_label():
    assert recall.age_label(NOW, now=NOW) == "今天"
    assert recall.age_label(NOW - DAY, now=NOW) == "昨天"
    assert recall.age_label(NOW - 5 * DAY, now=NOW) == "5 天前"
    assert recall.age_label(NOW + 100, now=NOW) == "今天"


def test_build_recall_block_structure_and_budget():
    items = [
        Recalled("session/a.md", "A" * (recall.FILE_CHARS + 50), NOW - 4 * DAY),
        Recalled("session/b.md", "B" * recall.FILE_CHARS, NOW),
        Recalled("session/c.md", "C" * recall.FILE_CHARS, NOW),
        Recalled("session/d.md", "D" * 10, NOW),
    ]
    block = recall.build_recall_block(items, "[数据]", now=NOW)
    assert block.startswith(recall.RECALL_BLOCK_OPEN + "\n[数据]")
    assert block.endswith(recall.RECALL_BLOCK_CLOSE)
    assert "### session/a.md（4 天前写入）" in block
    assert "已截断" in block
    assert "4 天前，记录的是当时的情况" in block
    # a（截断后略超 3000 字）+ b 之后再放 c 会超出 9000 字预算：从 c 起整条略去
    assert "session/b.md" in block
    assert "session/c.md" not in block
    assert "session/d.md" not in block


def test_build_recall_block_empty():
    assert recall.build_recall_block([], "[数据]") is None


def test_build_recall_message_is_not_saved():
    msg = recall.build_recall_message("block")
    assert msg == {"role": "user", "content": "block", "_no_save": True}
