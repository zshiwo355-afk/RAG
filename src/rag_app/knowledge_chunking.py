"""Versioned Markdown chunk previews with offsets into the unchanged body."""

from __future__ import annotations

import re
from typing import Any


LEGACY_VERSION = "legacy-v1"
STRUCTURE_VERSION = "structure-v1"
STRUCTURE_V2_VERSION = "structure-v2"
CHUNKING_VERSIONS = (LEGACY_VERSION, STRUCTURE_VERSION, STRUCTURE_V2_VERSION)
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_SENTENCE_END = re.compile(r"[。！？!?](?:[\"'”’）)]*)|\.(?=\s)|\n")
_DECORATION = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")


def _table_header(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines) or "|" not in lines[index]:
        return False
    cells = lines[index + 1].strip().strip("|").split("|")
    return bool(cells) and all(re.fullmatch(r"\s*:?-{3,}:?\s*", cell) for cell in cells)


def _blocks(content: str) -> list[dict[str, Any]]:
    lines = content.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    sections: list[tuple[int, str]] = []
    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index]
        heading, fence = _HEADING.match(line), _FENCE.match(line)
        kind, header = "text", ""
        end = index + 1
        if fence:
            kind = "code"
            marker = fence.group(1)
            closing = re.compile(r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*(?:\r?\n)?$")
            while end < len(lines):
                end += 1
                if closing.fullmatch(lines[end - 1]):
                    break
        elif heading:
            level, title = len(heading.group(1)), heading.group(2).strip()
            without_hashes = title.rstrip("#")
            if not without_hashes or without_hashes[-1].isspace():
                title = without_hashes.rstrip()
            sections = [(depth, label) for depth, label in sections if depth < level]
            sections.append((level, title))
        elif _table_header(lines, index):
            kind = "table"
            end = index + 2
            header = content[offsets[index]:offsets[end]]
            while end < len(lines) and lines[end].strip() and "|" in lines[end]:
                if _HEADING.match(lines[end]) or _FENCE.match(lines[end]):
                    break
                end += 1
        else:
            while end < len(lines) and lines[end].strip():
                if (_HEADING.match(lines[end]) or _FENCE.match(lines[end])
                        or _table_header(lines, end)):
                    break
                end += 1
        while end < len(lines) and not lines[end].strip():
            end += 1
        blocks.append({"start_offset": offsets[index], "end_offset": offsets[end],
                       "section_path": [label for _, label in sections],
                       "block_type": kind, "header": header})
        index = end
    return blocks


def _validate(content: str, max_chars: int, overlap: int) -> None:
    if not isinstance(content, str):
        raise ValueError("content must be text")
    if type(max_chars) is not int or not 1 <= max_chars <= 900:
        raise ValueError("max_chars must be between 1 and 900")
    if type(overlap) is not int or not 0 <= overlap <= 120 or overlap >= max_chars:
        raise ValueError("overlap must be between 0 and 120 and less than max_chars")


def split_structure(content: str, max_chars: int = 900, overlap: int = 120) -> list[dict[str, Any]]:
    """Frozen v1: cover every character; only continued tables repeat a prefix.

    ``source_text == prefix + content[start_offset:end_offset]``. Offsets are
    Python character offsets into the unchanged body, not UTF-8 byte offsets.
    """
    _validate(content, max_chars, overlap)
    blocks = []
    for block in _blocks(content):
        previous = blocks[-1] if blocks else None
        if (previous and previous["block_type"] == block["block_type"] == "text"
                and previous["section_path"] == block["section_path"]
                and block["end_offset"] - previous["start_offset"] <= max_chars):
            previous["end_offset"] = block["end_offset"]
        else:
            blocks.append(block)
    return _split_blocks(content, blocks, max_chars, overlap)


def split_structure_v2(content: str, max_chars: int = 900, overlap: int = 120) -> list[dict[str, Any]]:
    """Attach headings to content; omit standalone blank/thematic-break blocks.

    The body is unchanged. Coverage gaps may contain only whitespace or Markdown
    thematic breaks; headings and even one-character content remain covered.
    All other offsets/prefix semantics are identical to v1.
    """
    _validate(content, max_chars, overlap)
    blocks = []
    pending = None
    for block in _blocks(content):
        lines = [line for line in content[block["start_offset"]:block["end_offset"]].splitlines() if line.strip()]
        if block["block_type"] == "text" and all(_DECORATION.fullmatch(line) for line in lines):
            continue
        if block["block_type"] == "text" and all(_HEADING.match(line) or _DECORATION.fullmatch(line) for line in lines):
            if pending is not None:
                block["start_offset"] = pending["start_offset"]
            pending = block
            continue
        if pending is not None:
            block["start_offset"] = pending["start_offset"]
            pending = None
        previous = blocks[-1] if blocks else None
        if (previous and previous["block_type"] == block["block_type"] == "text"
                and previous["section_path"] == block["section_path"]
                and block["end_offset"] - previous["start_offset"] <= max_chars):
            previous["end_offset"] = block["end_offset"]
        else:
            blocks.append(block)
    if pending is not None:
        # A trailing heading may itself be meaningful. Never discard it.
        if blocks and pending["end_offset"] - blocks[-1]["start_offset"] <= max_chars:
            blocks[-1]["end_offset"] = pending["end_offset"]
        else:
            blocks.append(pending)
    # Attaching a heading can span a long decorative gap that is split again.
    return [chunk for chunk in _split_blocks(content, blocks, max_chars, overlap)
            if chunk["block_type"] != "text" or any(
                line.strip() and not _DECORATION.fullmatch(line)
                for line in chunk["source_text"].splitlines())]


def _split_blocks(content: str, blocks: list[dict[str, Any]], max_chars: int, overlap: int) -> list[dict[str, Any]]:
    chunks = []
    for block in blocks:
        start, end = block["start_offset"], block["end_offset"]
        covered = start
        header = block["header"]
        # ponytail: an oversized header cannot fit with a body; keep all text
        # without repetition. Rich table normalization belongs in intake.
        repeat_header = header if len(header) < max_chars else ""
        while start < end:
            prefix = repeat_header if start > block["start_offset"] else ""
            limit = max_chars - len(prefix)
            stop = min(end, start + limit)
            boundary = False
            if stop < end:
                part = content[start:stop]
                if block["block_type"] in ("table", "code"):
                    cut = part.rfind("\n") + 1
                else:
                    matches = list(_SENTENCE_END.finditer(part))
                    cut = matches[-1].end() if matches else 0
                if cut and start + cut > covered:
                    stop, boundary = start + cut, True
            chunks.append({"source_text": prefix + content[start:stop],
                           "start_offset": start, "end_offset": stop,
                           "section_path": block["section_path"],
                           "block_type": block["block_type"], "prefix": prefix})
            if stop == end:
                break
            # Only a long block needs overlap; preserve complete sentence/row
            # boundaries when one fits, otherwise overlap the hard-cut fragment.
            shared = 0 if boundary else min(overlap, (stop - start) // 2)
            covered = stop
            start = stop - shared
    return chunks
