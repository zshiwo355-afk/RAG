from __future__ import annotations

import pytest

from rag_app.knowledge_chunking import split_structure, split_structure_v2


pytestmark = pytest.mark.offline


def assert_coverage(content, chunks, limit=900):
    covered = 0
    for chunk in chunks:
        start, end = chunk["start_offset"], chunk["end_offset"]
        assert start <= covered < end
        assert covered - start <= 120
        assert chunk["source_text"] == chunk["prefix"] + content[start:end]
        assert 0 < len(chunk["source_text"]) <= limit
        covered = end
    assert covered == len(content)


def test_sections_paragraphs_and_parent_text_are_preserved():
    content = "  前言。\n\n# C#\n\n段一。\n\n段二。\n\n## 第二节 ##\n正文。\n\n# 末节\n尾文。  \n"
    chunks = split_structure(content)
    assert_coverage(content, chunks)
    assert "".join(chunk["source_text"] for chunk in chunks) == content
    assert [chunk["section_path"] for chunk in chunks] == [[], ["C#"], ["C#", "第二节"], ["末节"]]
    assert chunks[1]["source_text"] == "# C#\n\n段一。\n\n段二。\n\n"
    assert split_structure(content) == chunks


def test_long_paragraph_prefers_sentences_and_hard_cut_overlap_is_bounded():
    sentence = "先检查证据，再整理方法。"
    content = sentence * 180
    chunks = split_structure(content)
    assert_coverage(content, chunks)
    assert all(chunk["source_text"].endswith("。") for chunk in chunks)
    assert "".join(chunk["source_text"] for chunk in chunks) == content
    hard = split_structure("长" * 2000)
    assert_coverage("长" * 2000, hard)
    assert hard[0]["end_offset"] - hard[1]["start_offset"] == 120


def test_long_table_repeats_header_without_dropping_or_reordering_rows():
    header = "| 序号 | 内容 |\n| --- | --- |\n"
    rows = [f"| {number:03d} | {'证据' * 18} |\n" for number in range(80)]
    content = "# 表格\n\n" + header + "".join(rows) + "\n结论。\n"
    chunks = split_structure(content)
    assert_coverage(content, chunks)
    tables = [chunk for chunk in chunks if chunk["block_type"] == "table"]
    assert len(tables) > 2 and tables[0]["prefix"] == ""
    assert all(chunk["prefix"] == header for chunk in tables[1:])
    body = "".join(content[chunk["start_offset"]:chunk["end_offset"]] for chunk in tables)
    assert body == header + "".join(rows) + "\n"
    assert all(chunk["source_text"].startswith(header) for chunk in tables)


@pytest.mark.parametrize("marker", ["```", "~~~~"])
def test_fenced_code_headings_are_not_sections_and_code_is_fully_covered(marker):
    content = "# 实际章节\n\n" + marker + "python\n" + ("# 代码注释\nvalue = 1\n" * 130) + marker + "\n\n## 结尾\n说明。"
    chunks = split_structure(content)
    assert_coverage(content, chunks)
    code = [chunk for chunk in chunks if chunk["block_type"] == "code"]
    assert len(code) > 1
    assert all(chunk["section_path"] == ["实际章节"] for chunk in code)
    assert chunks[-1]["section_path"] == ["实际章节", "结尾"]
    assert "".join(chunk["source_text"] for chunk in chunks) == content


def test_long_table_row_and_oversized_header_remain_bounded_and_lossless():
    for content in (
        "| 列 |\n| --- |\n| " + "长" * 2300 + " |\n",
        "| " + "标题" * 460 + " |\n| --- |\n| 正文 |\n",
        "\n" * 1000 + "实际内容" + "\n" * 1000,
        "```\n# 未闭合围栏\n" + "代码" * 2000,
        "a" * 899 + ". " + "b" * 1000,
    ):
        assert_coverage(content, split_structure(content))


def test_table_does_not_swallow_following_heading_or_fence_containing_pipes():
    content = "| 名称 |\n| --- |\n| 正文 |\n# 后续 | 章节\n说明。\n```text|\n# 代码\n```\n"
    chunks = split_structure(content)
    assert_coverage(content, chunks)
    assert chunks[0]["block_type"] == "table"
    assert chunks[1]["section_path"] == ["后续 | 章节"]
    assert chunks[-1]["block_type"] == "code"


@pytest.mark.parametrize("kwargs", [
    {"max_chars": 0}, {"max_chars": 901}, {"max_chars": True},
    {"overlap": 121}, {"overlap": -1}, {"overlap": False},
    {"max_chars": 100, "overlap": 100},
])
def test_invalid_limits_fail_explicitly(kwargs):
    with pytest.raises(ValueError):
        split_structure("正文", **kwargs)


def test_empty_and_invalid_content():
    assert split_structure("") == []
    with pytest.raises(ValueError):
        split_structure(None)


def assert_v2_coverage(content, chunks, limit=900):
    """Only empty lines and thematic breaks may be absent from the index."""
    import re

    covered = 0
    for chunk in chunks:
        start, end = chunk["start_offset"], chunk["end_offset"]
        gap = content[covered:start]
        assert all(not line.strip() or re.fullmatch(r" {0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})", line)
                   for line in gap.splitlines())
        assert start >= 0 and end > covered and covered - start <= 120
        assert chunk["source_text"] == chunk["prefix"] + content[start:end]
        assert 0 < len(chunk["source_text"]) <= limit
        covered = end
    assert all(not line.strip() or re.fullmatch(r" {0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})", line)
               for line in content[covered:].splitlines())


def test_v2_attaches_heading_chain_to_table_without_changing_v1():
    heading = "# 方法\n\n## 所需输入\n\n"
    table = "| 输入 | 说明 |\n| --- | --- |\n| A | 原值 |\n\n"
    content = heading + table + "---\n\n短。"
    legacy = split_structure(content)
    assert [row["source_text"] for row in legacy] == ["# 方法\n\n", "## 所需输入\n\n", table, "---\n\n短。"]
    chunks = split_structure_v2(content)
    assert [row["source_text"] for row in chunks] == [heading + table, "短。"]
    assert chunks[0]["section_path"] == ["方法", "所需输入"]
    assert chunks[0]["block_type"] == "table"
    assert_v2_coverage(content, chunks)


def test_v2_long_table_repeats_header_and_retains_original_offsets():
    heading = "# 方法\n\n## 检查表\n\n"
    header = "| 序号 | 内容 |\n| --- | --- |\n"
    content = "---\n\n" + heading + header + "".join(f"| {i} | {'检查原值' * 20} |\n" for i in range(40)) + "\n***\n"
    chunks = split_structure_v2(content)
    assert len(chunks) > 2
    assert chunks[0]["source_text"].startswith(heading + header)
    assert chunks[0]["start_offset"] == len("---\n\n")
    assert all(chunk["prefix"] == header for chunk in chunks[1:])
    assert all(chunk["section_path"] == ["方法", "检查表"] for chunk in chunks)
    assert_v2_coverage(content, chunks)


@pytest.mark.parametrize("content", ["停", "\n---\n\n停\n\n___\n", "# 只有标题\n", "# " + "长标题" * 700 + "\n\n短。"])
def test_v2_preserves_short_content_and_long_headings(content):
    chunks = split_structure_v2(content)
    assert chunks
    assert_v2_coverage(content, chunks)


def test_v2_keeps_json_templates_and_trailing_headings_but_skips_decoration():
    content = '# 参数模板\n\n## 输入\n\n```json\n{"limit": 3}\n```\n\n---\n\n## 备注\n'
    chunks = split_structure_v2(content)
    assert len(chunks) == 1
    assert chunks[0]["block_type"] == "code"
    assert chunks[0]["source_text"] == content
    assert_v2_coverage(content, chunks)
    decorations = "---\n\n* * *\n\n___\n"
    assert split_structure_v2(decorations) == []
    assert_v2_coverage(decorations, [])


@pytest.mark.parametrize("gap", ["\n" * 2000, "---\n\n" * 300])
def test_v2_long_gap_after_heading_does_not_create_decorative_chunks(gap):
    content = "# 方法\n" + gap + "停"
    chunks = split_structure_v2(content)
    assert_v2_coverage(content, chunks)
    assert "# 方法" in chunks[0]["source_text"]
    assert chunks[-1]["source_text"].endswith("停")
    assert all(any(line.strip() and line.strip() != "---" for line in chunk["source_text"].splitlines())
               for chunk in chunks)
