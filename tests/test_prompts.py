import json

from core import prompts
from core.prompts import (
    MAX_EXTRACTED_MEMORIES,
    MemoryDraft,
    build_index_block,
    build_summary_message,
    build_summary_prompt,
    index_line,
    parse_summary_response,
    render_memory_file,
)


# ---------------------------------------------------------------- 注入块


def test_index_block_none_when_empty():
    assert build_index_block("", "  \n ") is None


def test_index_block_contains_scopes_and_tags():
    block = build_index_block("- [a](a.md) — x", "")
    assert block.startswith(prompts.INDEX_BLOCK_OPEN)
    assert block.endswith(prompts.INDEX_BLOCK_CLOSE)
    assert "全局记忆索引" in block and "会话记忆索引" in block
    assert "- [a](a.md) — x" in block
    assert "（暂无）" in block  # 空的会话作用域


def test_summary_message_is_data_not_instruction():
    message = build_summary_message("摘要正文")
    assert message["role"] == "user"
    assert "摘要正文" in message["content"]
    assert "数据标注" in message["content"]
    assert prompts.SUMMARY_BLOCK_OPEN in message["content"]


# ---------------------------------------------------------------- 摘要提示词


def test_build_summary_prompt_default_template():
    prompt = build_summary_prompt("", "旧摘要", "对话转写", extract_memories=False)
    assert "旧摘要" in prompt and "对话转写" in prompt
    assert "<memories>" not in prompt


def test_build_summary_prompt_extract_appended():
    prompt = build_summary_prompt("", "", "t", extract_memories=True)
    assert "<memories>" in prompt


def test_build_summary_prompt_custom_template_missing_key():
    prompt = build_summary_prompt(
        "自定义 {transcript} {unknown}", "prev", "对话", extract_memories=False
    )
    assert "对话" in prompt
    assert "{unknown}" in prompt  # 缺失占位符原样保留而不是 KeyError


def test_build_summary_prompt_empty_previous():
    prompt = build_summary_prompt("", "", "t", extract_memories=False)
    assert "（无）" in prompt


# ---------------------------------------------------------------- 响应解析


def test_parse_summary_with_tags_and_memories():
    entries = [
        {"type": "project", "name": "My Deadline", "description": "d", "content": "c"},
        {"type": "user", "name": "not-allowed", "description": "d", "content": "c"},
        {"type": "feedback", "name": "", "description": "d", "content": "c"},
    ]
    text = (
        f"<summary>## 结论\n压缩摘要</summary>\n"
        f"<memories>{json.dumps(entries, ensure_ascii=False)}</memories>"
    )
    summary, drafts = parse_summary_response(text)
    assert summary == "## 结论\n压缩摘要"
    assert len(drafts) == 1  # user 类型与空 name 被过滤
    assert drafts[0].name == "my-deadline"  # slug 归一化
    assert drafts[0].filename == "my-deadline.md"


def test_parse_summary_without_tags_falls_back():
    summary, drafts = parse_summary_response("模型没按格式，直接输出了摘要。")
    assert summary == "模型没按格式，直接输出了摘要。"
    assert drafts == []


def test_parse_summary_fenced_memories_json():
    text = (
        "<summary>s</summary>\n<memories>```json\n"
        '[{"type": "feedback", "name": "be-brief", "description": "d", "content": "c"}]'
        "\n```</memories>"
    )
    _, drafts = parse_summary_response(text)
    assert len(drafts) == 1 and drafts[0].type == "feedback"


def test_parse_summary_memories_capped():
    entries = [
        {"type": "project", "name": f"item-{i}", "description": "d", "content": "c"}
        for i in range(MAX_EXTRACTED_MEMORIES + 3)
    ]
    text = f"<summary>s</summary><memories>{json.dumps(entries)}</memories>"
    _, drafts = parse_summary_response(text)
    assert len(drafts) == MAX_EXTRACTED_MEMORIES


def test_parse_summary_bad_json_recovered_or_empty():
    text = '<summary>s</summary><memories>看这里 [{"type": "project", "name": "a", "description": "d", "content": "c"}] 完</memories>'
    _, drafts = parse_summary_response(text)
    assert len(drafts) == 1
    _, drafts = parse_summary_response("<summary>s</summary><memories>乱码</memories>")
    assert drafts == []


def test_parse_summary_empty():
    assert parse_summary_response("") == ("", [])


# ---------------------------------------------------------------- 记忆文件


def test_render_memory_file_frontmatter():
    draft = MemoryDraft(
        type="project", name="deadline", description="项目截止日", content="2026-08-15 截止"
    )
    text = render_memory_file(draft)
    assert text.startswith("---\nname: deadline\n")
    assert "description: 项目截止日" in text
    assert "  type: project" in text
    assert text.endswith("2026-08-15 截止\n")


def test_index_line_format():
    assert index_line("deadline", "项目截止日") == "- [deadline](deadline.md) — 项目截止日"
