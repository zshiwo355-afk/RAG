"""Local, resumable ZIP intake. Outputs are unapproved drafts, never publication.

Archive instructions are data. No packaged executable or network link is run.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import posixpath
import re
import stat
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from .knowledge_chunking import STRUCTURE_V2_VERSION, split_structure_v2
from .knowledge_store import normalize_entry
from .knowledge_text import clean_text, credential_name, parse_text_file


INTAKE_VERSION = "intake-v2"
SOURCE_PROTOCOL = "company-assets-source-v2"
SOURCE_COLUMNS = ("资产编号", "名称", "类型", "状态", "用途", "正文路径", "来源定位", "证据状态", "限制与待办", "合并目标")
SOURCE_TYPES = {"方法": "method", "流程": "process", "模板": "template", "提示词": "prompt",
                "Skill方法": "method", "案例": "case", "规则": "policy", "参考": "reference"}
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_FILE = 32 * 1024 * 1024
TEXT_EXTENSIONS = {".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".docx", ".xlsx", ".pdf"}
SCRIPT_EXTENSIONS = {".py", ".ps1", ".bat", ".sh", ".js", ".exe", ".cmd"}
DECISIONS = {"candidate", "repair", "method_only", "archive"}
DECISION_LABELS = {"candidate": "可入待审库", "repair": "修复或补件", "method_only": "只提取方法", "archive": "过程归档", "review_required": "尚待内容判断"}


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_sources(path):
    """Source IDs are receipt identities, not guesses about author or department."""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("sources"), list) or not value["sources"]:
        raise ValueError("input manifest requires sources")
    sources = []
    for source in value["sources"]:
        if not isinstance(source, dict) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source.get("source_id", "")):
            raise ValueError("each package requires a stable source_id")
        package = Path(source["path"]).expanduser().resolve()
        if package.suffix.lower() != ".zip" or not package.is_file():
            raise ValueError("each input must be an existing ZIP")
        encodings = source.get("text_encodings", {})
        if (not isinstance(encodings, dict) or any(not isinstance(key, str) or value not in {"utf-8-sig", "utf-16", "gb18030"} for key, value in encodings.items())):
            raise ValueError("text_encodings must explicitly map file names to a supported encoding")
        department = source.get("department")
        if department is not None and (not isinstance(department, str) or not department.strip() or len(department) > 200):
            raise ValueError("department must contain 1-200 characters")
        sources.append({"path": package, "source_id": source["source_id"],
                        "contributor": source.get("contributor", "未确认"),
                        "package": package.name, "sha256": file_hash(package), "text_encodings": encodings,
                        **({"department": department.strip()} if department is not None else {})})
    return sources


def _safe_members(archive):
    infos = [item for item in archive.infolist() if not item.is_dir()]
    if len(infos) > 20_000 or sum(item.file_size for item in infos) > MAX_ARCHIVE:
        raise ValueError("archive limits exceeded")
    names = set()
    for item in infos:
        name = item.filename.replace("\\", "/")
        if (name.startswith(("/", "~")) or ".." in PurePosixPath(name).parts
                or re.match(r"^[A-Za-z]:", name) or "\x00" in name
                or name in names or stat.S_ISLNK(item.external_attr >> 16)):
            raise ValueError("unsafe archive structure")
        names.add(name)
    return infos


def _relative(name):
    parts = PurePosixPath(name).parts
    # Only strip an actual delivery wrapper, never a meaningful first directory.
    return "/".join(parts[1:]) if len(parts) > 2 and parts[1] in {"附件", "知识正文"} else name


def _incomplete(issues):
    return any(issue["code"].endswith(("_limit", "_not_parsed"))
               or issue["code"] in {"needs_ocr", "no_text_extracted", "xlsx_formula_cache_missing", "asset_too_large", "referenced_material_unparsed"}
               for issue in issues)


def _is_skill(name):
    return bool(re.fullmatch(r"SKILL(?:[_-].*)?\.md", PurePosixPath(name).name, re.I))


def _references(text):
    # Examples inside fenced code are not claims about required attachments.
    narrative, fence = [], None
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*\r?\n?", line):
                fence = None
        elif marker:
            fence = marker[1]
        else:
            narrative.append(line)
    text = "".join(narrative)
    code_spans = re.compile(r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)", re.S)
    values = re.findall(r"\[[^\]\n]*\]\(((?:[^()\n]|\([^()\n]*\))+)\)", code_spans.sub(" ", text))
    values += [match[2].strip() for match in code_spans.finditer(text)
               if re.fullmatch(r"(?:\.\./)*(?:references|templates|assets|scripts|tools|附件)/[^\n]+", match[2].strip())]
    return sorted(set(value.strip(" <>") for value in values if value.strip()))


def _resolve_reference(name, reference, documents):
    ref = unquote(reference.split("#", 1)[0]).replace("\\", "/")
    if not ref or re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", ref) or ref.startswith(("/", "~")):
        return None
    exact = posixpath.normpath(posixpath.join(posixpath.dirname(name), ref))
    if exact in documents:
        return exact
    if ref.startswith("附件/"):
        root = name.split("/知识正文/", 1)[0] if "/知识正文/" in name else ""
        candidate = posixpath.join(root, ref)
        if candidate in documents:
            return candidate
    # References may themselves use paths relative to the owning Skill root.
    parent = PurePosixPath(name).parent
    for ancestor in (parent, *parent.parents):
        if str(ancestor / "SKILL.md") in documents:
            candidate = posixpath.normpath(str(ancestor / ref))
            if candidate in documents:
                return candidate
    # Flattened exports rename references/a.md to references_a.md. Resolve only
    # unique siblings; ambiguous/missing links remain visible review issues.
    sibling = posixpath.dirname(name)
    flattened = ref.replace("/", "_")
    matches = [key for key in documents if posixpath.dirname(key) == sibling
               and PurePosixPath(key).name == flattened]
    return matches[0] if len(matches) == 1 else None


def scan_package(source, cache):
    report = {key: source[key] for key in ("package", "source_id", "sha256")}
    report.update(files=[], issues=[])
    documents = {}
    try:
        if source["path"].stat().st_size > MAX_ARCHIVE:
            raise ValueError("archive limits exceeded")
        with zipfile.ZipFile(source["path"]) as archive:
            all_infos = [item for item in archive.infolist() if not item.is_dir()]
            report["declared_files"] = len(all_infos)
            try:
                infos = _safe_members(archive)
            except ValueError:
                report["files"] = [{"name": info.filename, "bytes": info.file_size, "state": "archive_rejected", "disposition": "not_parsed"} for info in all_infos[:20_000]]
                if len(all_infos) > 20_000:
                    report["issues"].append({"code": "file_inventory_limit", "not_listed": len(all_infos) - 20_000})
                raise
            for info in infos:
                name = info.filename.replace("\\", "/")
                item = {"name": name, "bytes": info.file_size, "disposition": "not_parsed"}
                report["files"].append(item)
                suffix = PurePosixPath(name).suffix.lower()
                if credential_name(name):
                    item["state"] = "credential_skipped"
                elif info.flag_bits & 1 or info.file_size > MAX_FILE:
                    item["state"] = "encrypted_or_oversize"
                elif suffix in SCRIPT_EXTENSIONS:
                    item["state"] = "script_not_ingested"
                elif suffix not in TEXT_EXTENSIONS:
                    item["state"] = "needs_ocr" if suffix in {".png", ".jpg", ".jpeg", ".tiff"} else "unsupported_format"
                else:
                    try:
                        data = archive.read(info)
                        item["sha256"] = sha(data)
                        encoding = source.get("text_encodings", {}).get(name)
                        key = sha(INTAKE_VERSION + suffix + str(encoding) + item["sha256"])
                        cached = cache / (key + ".json")
                        parsed = json.loads(cached.read_text(encoding="utf-8")) if cached.exists() else None
                        if parsed is None:
                            if encoding and suffix in {".docx", ".xlsx", ".pdf"}:
                                raise ValueError("encoding_override_not_text")
                            parsed = parse_text_file(name, data.decode(encoding).encode("utf-8") if encoding else data)
                            if encoding:
                                parsed["issues"].append({"code": "explicit_encoding", "encoding": encoding})
                            cleaned, changes = clean_text(parsed["text"])
                            parsed = {**parsed, "text": cleaned, "cleaned_hash": sha(cleaned), "redactions": changes}
                            write_json(cached, parsed)
                        if sha(parsed["text"]) != parsed["cleaned_hash"]:
                            raise ValueError("cache content mismatch")
                        documents[name] = {**parsed, "sha256": item["sha256"], "item": item}
                        if PurePosixPath(name).name == "交付清单.csv":
                            # Parse the original CSV before per-cell cleaning can alter quoting.
                            if _incomplete(parsed["issues"]):
                                raise ValueError("source_manifest_invalid")
                            rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True))
                            if not rows or tuple(rows[0]) != SOURCE_COLUMNS or any(len(row) != len(SOURCE_COLUMNS) for row in rows[1:]):
                                raise ValueError("source_manifest_invalid")
                            documents[name]["csv_rows"] = [dict(zip(SOURCE_COLUMNS, (clean_text(cell)[0].strip() for cell in row))) for row in rows[1:]]
                        item.update(state="parsed", disposition="supporting_material",
                                    redactions=parsed["redactions"], issues=parsed["issues"],
                                    cleaned_hash=parsed["cleaned_hash"], cache_key=key)
                    except Exception as error:
                        documents.pop(name, None)
                        item["state"] = "parse_failed"
                        if isinstance(error, ValueError) and re.fullmatch(r"[a-z_]{1,80}", str(error)):
                            item["error_code"] = str(error)
                if item["state"] != "parsed":
                    report["issues"].append({"file": name, "code": item["state"]})
    except Exception:
        report["issues"].append({"code": "archive_failed", "detail": "归档读取、安全校验或大小限制失败"})
        documents = {}
    return report, documents


def _source_protocol(source):
    # Only the presence marker is needed before resume checks; parsing remains in scan_package.
    try:
        with zipfile.ZipFile(source["path"]) as archive:
            if any(PurePosixPath(info.filename.replace("\\", "/")).name == "交付清单.csv"
                   for info in archive.infolist() if not info.is_dir()):
                return {"source_protocol": SOURCE_PROTOCOL}
    except (OSError, zipfile.BadZipFile):
        pass
    return {}


def _source_manifest(report, documents):
    names = [item["name"] for item in report["files"] if PurePosixPath(item["name"]).name == "交付清单.csv"]
    if not names:
        return None
    report["source_protocol"] = SOURCE_PROTOCOL
    try:
        if len(names) != 1 or names[0] not in documents or "csv_rows" not in documents[names[0]]:
            raise ValueError("source_manifest_invalid")
        document = documents[names[0]]
        if _incomplete(document["issues"]):
            raise ValueError("source_manifest_invalid")
        rows, ids, paths = document["csv_rows"], {}, set()
        delivered = {}
        for row in rows:
            key, state = row["资产编号"], row["状态"]
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", key)
                    or key.casefold() in ids or not row["名称"] or row["类型"] not in SOURCE_TYPES
                    or state not in {"交付", "待补", "排除", "合并"}):
                raise ValueError("source_manifest_invalid_row")
            ids[key.casefold()] = row
            if state == "交付":
                path = row["正文路径"].replace("\\", "/")
                if (not path or path.startswith(("/", "~")) or ":" in path or "\x00" in path
                        or any(part in {"", ".", ".."} for part in path.split("/"))
                        or PurePosixPath(path).suffix.lower() not in {".md", ".txt"}):
                    raise ValueError("source_manifest_unsafe_path")
                name = posixpath.join(posixpath.dirname(names[0]), path)
                if name.casefold() in paths:
                    raise ValueError("source_manifest_duplicate_body")
                paths.add(name.casefold())
                if name not in documents or not documents[name]["text"].strip() or _incomplete(documents[name]["issues"]):
                    raise ValueError("source_manifest_body_unavailable")
                delivered[name] = row
            elif row["正文路径"]:
                raise ValueError("source_manifest_nondelivery_body")
            if (state == "合并") != bool(row["合并目标"]):
                raise ValueError("source_manifest_invalid_merge")
        for row in rows:
            seen, current = set(), row
            while current["状态"] == "合并":
                key = current["资产编号"].casefold()
                target = current["合并目标"].casefold()
                if key in seen or target not in ids:
                    raise ValueError("source_manifest_invalid_merge")
                seen.add(key)
                current = ids[target]
            if row["状态"] == "合并" and current["状态"] != "交付":
                raise ValueError("source_manifest_invalid_merge")
        report["source_records"] = rows
        document["item"]["disposition"] = "source_manifest"
        for name, doc in documents.items():
            if "知识正文" in PurePosixPath(name).parts and name not in delivered:
                doc["item"]["disposition"] = "unregistered_body"
                report["issues"].append({"file": name, "code": "source_manifest_unregistered_body"})
        return names[0], delivered
    except ValueError as error:
        report["issues"].append({"code": str(error)})
        return names[0], {}


def _public_locator(value):
    if re.search(r"(?:[A-Za-z]:[\\/]|\\\\|(?:^|[\s\"'（(：:])(?:/|~/))", value):
        return "本机路径未公开，原定位保留在收件清单"
    return value or "未取得"


def _title(text, fallback):
    lines = text.splitlines()
    heading = next((line.lstrip("# ") for line in lines if re.match(r"^#{1,6}\s+", line)), None)
    return (heading or fallback)[:240]


def _reference_collection(name, text):
    # ponytail: only an explicitly named corpus gets a new boundary. Ordinary
    # method references stay together; ambiguous files remain for review.
    labels = (_title(text, ""), PurePosixPath(name).stem)
    return any(re.search(r"(?:案例库|案例集|爆款榜|榜单|视频档案|case[ _-](?:library|studies)|rankings?)"
                         r"(?:\s*[（(][^）)]*[）)]|\s*\d{4}(?:[-–]\d{4})?)?\s*$", label, re.I)
               for label in labels)


def _case_sections(text):
    """Explicit numbered ATX cases, excluding headings inside code fences."""
    headings, stack, fence, offset = [], [], None, 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*\r?\n?", line):
                fence = None
        elif marker:
            fence = marker[1]
        else:
            heading = re.match(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*\r?\n?$", line)
            if heading:
                level, title = len(heading[1]), re.sub(r"\s+#+\s*$", "", heading[2])
                stack = [item for item in stack if item["level"] < level]
                item = {"start": offset, "heading_end": offset + len(line), "level": level,
                        "title": title, "ancestors": list(stack)}
                headings.append(item)
                stack.append(item)
        offset += len(line)
    cases, seen = [], set()
    for index, heading in enumerate(headings):
        number = re.match(r"^(?:案例\s*)?([A-Z]\d{1,3}|\d{2,3})(?:\s+|[｜|、:：])\S", heading["title"], re.I)
        if not number:
            continue
        scope = tuple(item["title"] for item in heading["ancestors"])
        key = number[1].upper()
        if key in seen:
            return [], "duplicate_case_number", headings
        seen.add(key)
        end = next((item["start"] for item in headings[index + 1:] if item["level"] <= heading["level"]), len(text))
        cases.append({**heading, "case_number": number[1], "end": end, "section_path": list(scope)})
    # Nested numbered headings could be substeps rather than independent cases.
    if any(left["end"] > right["start"] for left, right in zip(cases, cases[1:])):
        return [], "nested_case_boundaries", headings
    return cases, None if cases else "no_explicit_case_boundaries", headings


def _compose(documents, spans):
    parts, locations, length = [], [], 0
    for name, start, end in spans:
        doc = documents[name]
        text = doc["text"][start:end]
        prefix = "" if not parts else ("\n\n" if locations[-1]["source_path"] == name
                                       else "\n\n---\n\n## 配套材料：" + PurePosixPath(name).name + "\n\n")
        length += len(prefix)
        parts.append(prefix + text)
        locations.append({"source_path": name, "source_sha256": doc["sha256"],
                          "start_offset": length, "end_offset": length + len(text),
                          "source_start_offset": start, "source_end_offset": end,
                          "offset_basis": "cleaned_source_text", "locations": doc["locations"]})
        length += len(text)
    return "".join(parts), locations


def _derive_cases(asset, documents):
    """Keep library commentary and each case; never inherit a library review."""
    name = asset["source_path"]
    text = documents[name]["text"]
    sections, issue, headings = _case_sections(text)
    asset["asset_role"] = "reference_overview"
    asset["metadata"]["kind"] = "reference_overview"
    asset["metadata"]["evidence"]["asset_role"] = "reference_overview"
    if issue:
        asset["issues"].append({"code": "case_boundary_requires_review", "reason": issue})
        return [asset]
    asset["full_bundle_content"] = text
    asset["full_bundle_members"] = asset["members"]
    remaining, cursor = [], 0
    for section in sections:
        if cursor < section["start"]:
            remaining.append((name, cursor, section["start"]))
        cursor = section["end"]
    if cursor < len(text):
        remaining.append((name, cursor, len(text)))
    asset["content"], asset["members"] = _compose(documents, remaining)
    # Carry the shared reading/provenance notice, not the first year's category.
    preface_end = next((h["start"] for h in sections[0]["ancestors"] if h["start"] > 0), sections[0]["start"])
    preface_end = min([preface_end, *(h["start"] for h in headings if h["start"] < sections[0]["start"]
                                    and re.search(r"索引|目录|总览|速查", h["title"]))])
    preface = [(name, 0, preface_end)] if preface_end else []
    for index, heading in enumerate(headings):
        if (preface_end <= heading["start"] < sections[0]["start"]
                and re.search(r"阅读说明|阅读前必知|使用说明|真实性说明|证据边界|口径说明", heading["title"])):
            end = next((h["start"] for h in headings[index + 1:] if h["level"] <= heading["level"]), len(text))
            preface.append((name, heading["start"], min(end, sections[0]["start"])))
    derived = []
    for section in sections:
        item = copy.deepcopy(asset)
        item.pop("full_bundle_content", None)
        item.pop("full_bundle_members", None)
        identity = "/".join(section["section_path"] + [section["case_number"]])
        item["knowledge_id"] = "ka_" + sha(asset["knowledge_id"] + "/case/" + identity)[:24]
        spans = list(preface)
        spans += [(name, h["start"], h["heading_end"]) for h in section["ancestors"] if h["start"] >= preface_end]
        spans.append((name, section["start"], section["end"]))
        item["content"], item["members"] = _compose(documents, spans)
        item["asset_role"] = "reference_case"
        item["metadata"].update(title=section["title"][:240], kind="reference_case")
        fragment = {"source_path": name, "source_sha256": asset["source_sha256"],
                    "start_offset": section["start"], "end_offset": section["end"],
                    "offset_basis": "cleaned_source_text", "case_number": section["case_number"],
                    "section_path": section["section_path"]}
        item["source_fragment"] = fragment
        item["metadata"]["evidence"].update(asset_role="reference_case", source_fragment=fragment,
                                           parent_reference_id=asset["knowledge_id"])
        item["related_knowledge_ids"] = sorted(set(item["related_knowledge_ids"] + [asset["knowledge_id"]]))
        item["context_hash"] = sha(asset["context_hash"] + json.dumps(fragment, ensure_ascii=False, sort_keys=True))
        derived.append(item)
    asset["related_knowledge_ids"] = sorted(set(asset["related_knowledge_ids"] + [item["knowledge_id"] for item in derived]))
    asset["metadata"]["evidence"]["derived_case_count"] = len(derived)
    return [asset, *derived]


def _prepare_assets(source, report, documents):
    manifest = _source_manifest(report, documents)
    delivered = manifest[1] if manifest is not None else None
    seeds = list(delivered) if delivered is not None else [name for name in documents if "知识正文" in PurePosixPath(name).parts or _is_skill(name)]
    if not seeds and delivered is None:
        # Generic exports still get enumerated; interpretation requires review.
        seeds = [name for name in documents if PurePosixPath(name).suffix.lower() in {".md", ".txt", ".docx", ".xlsx", ".pdf"}
                 and PurePosixPath(name).name not in {"交付说明.md", "README.md"}]
    primary_count = len(seeds)
    owned, collections = {}, {}

    def asset_identity(path):
        identity = "source-v2/" + delivered[path]["资产编号"] if delivered is not None else _relative(path)
        return "ka_" + sha(source["source_id"] + "/" + identity)[:24]

    for name in seeds:
        if delivered is not None or PurePosixPath(name).name.lower() != "skill.md":
            continue
        root = posixpath.dirname(name) + "/"
        owned[name] = [key for key in sorted(documents) if key.startswith(root)
                       and key[len(root):].split("/", 1)[0] in {"references", "templates"}
                       and not _is_skill(key)]
        for key in owned[name]:
            if _reference_collection(key, documents[key]["text"]):
                collections.setdefault(key, set()).add(name)
    seeds = sorted(set(seeds) | set(collections))
    report["primary_asset_count"] = primary_count
    assets = []
    for name in sorted(seeds):
        doc = documents[name]
        if not doc["text"].strip():
            doc["item"]["disposition"] = "empty_needs_review"
            continue
        members, issues, related = [name], list(doc["issues"]), set(collections.get(name, ()))
        available = {file["name"] for file in report["files"]}
        # A real Skill directory owns its textual references/templates. Sibling
        # flattened Skills are separate assets, never all bundled by KA number.
        members += [key for key in owned.get(name, ()) if key not in collections]
        related.update(key for key in owned.get(name, ()) if key in collections)
        pending = list(members)
        checked = set()
        while pending:
            member = pending.pop(0)
            if member in checked:
                continue
            checked.add(member)
            for reference in _references(documents[member]["text"]):
                if reference.startswith("#") or "://" in reference or "*" in reference or re.search(r"[<>~]|KA-(?:NNN|00X)", reference):
                    continue
                if reference.endswith("/"):
                    root_reference = posixpath.normpath(posixpath.join(posixpath.dirname(member), reference)) + "/"
                    if reference.startswith("附件/") and "/知识正文/" in member:
                        root_reference = member.split("/知识正文/", 1)[0] + "/" + reference
                    related.update(seed for seed in seeds if seed.startswith(root_reference))
                    # Directory mentions are not claims about any one missing file.
                    continue
                resolved = _resolve_reference(member, reference, available)
                if (delivered is not None and resolved and "知识正文" in PurePosixPath(resolved).parts
                        and resolved not in delivered):
                    issues.append({"code": "unresolved_reference", "reference": reference})
                    continue
                if resolved and resolved not in documents:
                    code = "script_dependency_not_provided" if PurePosixPath(resolved).suffix.lower() in SCRIPT_EXTENSIONS else "referenced_material_unparsed"
                    issues.append({"code": code, "reference": reference})
                    continue
                if resolved and resolved not in members and resolved not in seeds and name not in collections:
                    members.append(resolved)
                    pending.append(resolved)
                elif resolved is None:
                    code = "script_dependency_not_provided" if PurePosixPath(reference).suffix.lower() in SCRIPT_EXTENSIONS else "unresolved_reference"
                    issues.append({"code": code, "reference": reference})
                elif resolved in seeds and resolved != name:
                    related.add(resolved)
        body, locations = _compose(documents, [(member, 0, len(documents[member]["text"])) for member in members])
        for member in members:
            documents[member]["item"].setdefault("asset_memberships", []).append(name)
            if member != name:
                issues.extend(documents[member]["issues"])
        kind = "skill" if _is_skill(name) else "未知"
        asset_id = asset_identity(name)
        redactions = sum(sum(documents[member]["redactions"].values()) for member in members)
        if redactions:
            issues.append({"code": "redaction_review", "count": redactions})
        if _is_skill(name) and re.search(r"(?:scripts|tools)/[^\s`]+", doc["text"]):
            issues.append({"code": "script_dependency_not_provided", "detail": "只作方法参考，不承诺可直接执行"})
        metadata = {"title": _title(doc["text"], PurePosixPath(name).stem), "kind": kind,
                    "contributor": source["contributor"], "chunking_version": STRUCTURE_V2_VERSION,
                    "sources": [{"name": source["package"], "locator": member} for member in members],
                    "evidence": {"review_status": "pending", "processing": INTAKE_VERSION,
                                 "processing_issues": sorted({issue["code"] for issue in issues}),
                                 "source_complete": not _incomplete(issues),
                                 "delivery_scope": "method_reference_only" if any(issue["code"] == "script_dependency_not_provided" for issue in issues) else "text_material",
                                 "proposed": "待核验", "adopted": "未核验", "executed": "未核验", "validated": "未核验"}}
        if source.get("department") is not None:
            metadata["department"] = source["department"]
        if delivered is not None:
            row = delivered[name]
            metadata.update(title=row["名称"][:240], kind=SOURCE_TYPES[row["类型"]])
            metadata["sources"].append({"name": PurePosixPath(manifest[0]).name,
                                        "locator": "资产 " + row["资产编号"] + "；" + _public_locator(row["来源定位"])})
            metadata["evidence"].update(source_protocol=SOURCE_PROTOCOL, source_asset_id=row["资产编号"],
                                        source_type=row["类型"], source_disposition=row["状态"],
                                        source_purpose=row["用途"], source_evidence_status=row["证据状态"],
                                        source_limitations=row["限制与待办"], source_approval=False)
            if row["类型"] == "Skill方法":
                metadata["evidence"]["delivery_scope"] = "method_reference_only"
        asset = {"knowledge_id": asset_id, "metadata": metadata, "content": body,
                       "source_id": source["source_id"], "source_path": name,
                       "source_package": source["package"], "source_sha256": doc["sha256"],
                       "members": locations, "issues": issues,
                       "related_knowledge_ids": [asset_identity(path) for path in sorted(related)],
                       "decision": "review_required", "published": False,
                       "context_hash": sha(json.dumps([(_relative(m["source_path"]), m["source_sha256"]) for m in locations], ensure_ascii=False))}
        if delivered is not None:
            asset["context_hash"] = sha(asset["context_hash"] + json.dumps(row, sort_keys=True, ensure_ascii=False))
        if any(key in collections for key in owned.get(name, ())):
            full_members = [name, *owned[name], *(member for member in members[1:] if member not in owned[name])]
            asset["full_bundle_content"], asset["full_bundle_members"] = _compose(
                documents, [(member, 0, len(documents[member]["text"])) for member in full_members])
            asset["context_hash"] = sha(json.dumps([(_relative(member), documents[member]["sha256"])
                                                    for member in full_members], ensure_ascii=False))
            metadata["evidence"]["asset_boundary"] = "method_with_required_references"
        assets.extend(_derive_cases(asset, documents) if name in collections else [asset])
        doc["item"]["disposition"] = "asset_candidate"
    by_id = {asset["knowledge_id"]: asset for asset in assets}
    for asset in assets:
        asset["issues"] = [issue for issue in asset["issues"] if issue["code"] != "asset_too_large"]
        if len(asset["content"]) > 500_000:
            asset["issues"].append({"code": "asset_too_large", "detail": "独立资产超过正文上限，完整副本保留待审"})
        related_assets = []
        for key in asset["related_knowledge_ids"]:
            target = by_id.get(key)
            if target is None:
                continue
            related_assets.append({"knowledge_id": key, "source_package": target["source_package"],
                                   "source_locator": _relative(target["source_path"]),
                                   "relation": target.get("asset_role", "related_method"),
                                   "review_status": "pending", "published": False,
                                   **({"source_fragment": target["source_fragment"]} if "source_fragment" in target else {})})
        asset["metadata"]["evidence"]["related_assets"] = related_assets
        asset["metadata"]["evidence"]["processing_issues"] = sorted({issue["code"] for issue in asset["issues"]})
        asset["metadata"]["evidence"]["source_complete"] = not _incomplete(asset["issues"])
    for name, doc in documents.items():
        if doc["item"]["disposition"] == "supporting_material":
            doc["item"]["disposition"] = "included_reference" if doc["item"].get("asset_memberships") else "unassigned_needs_review"
    return assets


def read_reviews(path):
    if path is None:
        return []
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def _accepted_method_references(asset, metadata):
    """Only a validated main-document review may narrow missing files to method use."""
    evidence = metadata.get("evidence", {})
    main = next((row for row in asset.get("review", {}).get("materials", [])
                 if row.get("source_path") == asset.get("source_path")), {})
    values = main.get("accepted_missing_references")
    if not isinstance(values, list) or not values:
        return []
    missing = [issue.get("reference") for issue in asset["issues"] if issue["code"] == "unresolved_reference"]
    if (asset["decision"] != "candidate" or main.get("decision") != "candidate"
            or main.get("source_sha256") != asset["source_sha256"]
            or metadata.get("kind") != "method" or evidence.get("source_protocol") != SOURCE_PROTOCOL
            or evidence.get("source_type") != "Skill方法" or evidence.get("delivery_scope") != "method_reference_only"
            or not missing
            or not all(isinstance(ref, str) and ref and ref == ref.strip()
                       and not ref.startswith(("/", "\\", "~", "#")) and "\x00" not in ref
                       and not re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", ref) for ref in values)
            or len(set(values)) != len(values) or set(values) != set(missing)):
        return []
    return values


def _apply_review(asset, reviews, documents):
    # Library/file reviews do not approve newly derived cases or commentary.
    if asset.get("asset_role") in {"reference_case", "reference_overview"}:
        return
    assessed = []
    for member in asset["members"]:
        path = member["source_path"]
        matches = [row for row in reviews if row.get("source_package") == asset["source_package"] and row.get("source_path") == path]
        if not matches:
            continue
        review = matches[-1]
        text, anchors = documents[path]["text"], review.get("evidence_anchors", [])
        valid = (review.get("source_sha256") == member["source_sha256"]
                 and review.get("decision") in DECISIONS and isinstance(review.get("reasons"), list)
                 and bool(review["reasons"]) and isinstance(anchors, list) and bool(anchors)
                 and all(isinstance(anchor, dict) and isinstance(anchor.get("quote"), str)
                         and bool(clean_text(anchor["quote"])[0].strip()) and clean_text(anchor["quote"])[0] in text for anchor in anchors))
        if not valid:
            asset["issues"].append({"code": "review_evidence_mismatch", "source_path": path})
            continue
        assessed.append({"source_path": path, "decision": review["decision"], "reviewer": review.get("reviewer", "未确认"),
                         "reasons": [clean_text(str(reason))[0] for reason in review["reasons"]],
                         "evidence_anchors": [{"quote": clean_text(anchor["quote"])[0], "location": str(anchor.get("location", ""))} for anchor in anchors]})
        if "accepted_missing_references" in review:
            assessed[-1].update(source_sha256=review["source_sha256"],
                                accepted_missing_references=review["accepted_missing_references"])
        if path == asset["source_path"]:
            asset["decision"] = review["decision"]
    if not assessed:
        return
    # A good main document cannot override problematic required attachments.
    if asset["decision"] == "candidate" and any(row["decision"] != "candidate" for row in assessed):
        asset["decision"] = "repair"
        asset["issues"].append({"code": "dependency_review_requires_curation"})
    asset["review"] = {"review_status": "ai_suggested", "materials": assessed,
                       "assessed_members": len(assessed), "total_members": len(asset["members"]),
                       "scope": "按列出的原文锚点核对；未审阅配套材料、跨文冲突及共享条件仍需复核"}
    asset["metadata"]["evidence"]["intake_decision"] = asset["decision"]
    if any("accepted_missing_references" in row for row in assessed):
        accepted = _accepted_method_references(asset, asset.get("metadata", {}))
        if accepted:
            asset["metadata"]["evidence"].update(accepted_missing_references=accepted, source_complete=False)
        else:
            asset["issues"].append({"code": "missing_reference_acceptance_invalid"})


def process_batch(sources, output, *, reviews=None, resume=False):
    output = Path(output)
    identity = {"version": INTAKE_VERSION, "sources": [{**{key: source[key] for key in ("source_id", "package", "sha256", "contributor")}, "text_encodings": source.get("text_encodings", {}),
                 **({"department": source["department"]} if "department" in source else {}), **_source_protocol(source)} for source in sources],
                "reviews_hash": sha(json.dumps(reviews or [], sort_keys=True, ensure_ascii=False))}
    if resume:
        if json.loads((output / "receipt.json").read_text(encoding="utf-8")) != identity:
            raise ValueError("resume requires identical sources, review input and processing version")
    else:
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / "receipt.json", identity)
    for name in ("cache", "assets", "chunks"):
        (output / name).mkdir(exist_ok=True)
    reports, assets, identity_versions = [], [], {}
    for source in sources:
        report, documents = scan_package(source, output / "cache")
        reports.append(report)
        for asset in _prepare_assets(source, report, documents):
            _apply_review(asset, reviews or [], documents)
            key = (asset["knowledge_id"], asset["context_hash"])
            if key in identity_versions:
                kept = identity_versions[key]
                kept["metadata"]["sources"].extend(asset["metadata"]["sources"])
                kept.setdefault("duplicate_sources", []).append({"package": source["package"], "path": asset["source_path"]})
                continue
            prior = [value for (knowledge_id, _), value in identity_versions.items() if knowledge_id == asset["knowledge_id"]]
            if prior:
                asset["issues"].append({"code": "same_asset_different_version", "detail": "保留差异，不按收件时间自动认定采用版本"})
                for other in prior:
                    other["issues"].append({"code": "same_asset_different_version"})
            asset["candidate_id"] = asset["knowledge_id"] + "_" + asset["context_hash"][:12]
            identity_versions[key] = asset
            assets.append(asset)
    content_owners = {}
    for asset in assets:
        content_hash = sha(asset["content"])
        if content_hash in content_owners and content_owners[content_hash] != asset["knowledge_id"]:
            asset["issues"].append({"code": "possible_duplicate", "knowledge_id": content_owners[content_hash]})
        content_owners.setdefault(content_hash, asset["knowledge_id"])
        candidate_id = asset["candidate_id"]
        body_path = output / "assets" / (candidate_id + ".md")
        metadata_path = output / "assets" / (candidate_id + ".json")
        body_path.write_text(asset["content"], encoding="utf-8")
        if "full_bundle_content" in asset:
            full_path = output / "assets" / (candidate_id + ".full.md")
            full_path.write_text(asset.pop("full_bundle_content"), encoding="utf-8")
            asset.update(full_bundle_file=str(full_path.relative_to(output)), full_bundle_hash=file_hash(full_path),
                         full_bundle_scope="cleaned_parsed_source_members")
        chunks = []
        try:
            normalize_entry({"knowledge_id": asset["knowledge_id"], "content": asset["content"], **asset["metadata"]})
            for index, chunk in enumerate(split_structure_v2(asset["content"]), 1):
                chunks.append({**chunk, "knowledge_id": asset["knowledge_id"], "candidate_id": candidate_id,
                               "chunk_index": index, "chunking_version": STRUCTURE_V2_VERSION, "published": False})
        except ValueError:
            asset["issues"].append({"code": "draft_validation_failed"})
        asset["metadata"]["evidence"].update(processing_issues=sorted({issue["code"] for issue in asset["issues"]}),
                                              source_complete=not _incomplete(asset["issues"])
                                              and not asset["metadata"]["evidence"].get("accepted_missing_references"))
        write_json(metadata_path, asset["metadata"])
        chunk_path = output / "chunks" / (candidate_id + ".jsonl")
        chunk_path.write_text("".join(json.dumps(chunk, ensure_ascii=False) + "\n" for chunk in chunks), encoding="utf-8")
        asset.update(content_hash=content_hash, chunk_count=len(chunks), body_file=str(body_path.relative_to(output)),
                     metadata_file=str(metadata_path.relative_to(output)), metadata_hash=file_hash(metadata_path),
                     chunk_file=str(chunk_path.relative_to(output)), state="draft_ready" if chunks and not _incomplete(asset["issues"]) else "repair_required")
        del asset["content"]
    summary = {"package_count": len(reports), "file_count": sum(report.get("declared_files", len(report["files"])) for report in reports),
               "parsed_files": sum(file["state"] == "parsed" for report in reports for file in report["files"]),
               "asset_count": len(assets), "chunk_count": sum(asset["chunk_count"] for asset in assets),
               "primary_asset_count": sum(asset.get("asset_role") is None for asset in assets),
               "reference_overview_count": sum(asset.get("asset_role") == "reference_overview" for asset in assets),
               "derived_case_count": sum(asset.get("asset_role") == "reference_case" for asset in assets),
               "decisions": dict(Counter(asset["decision"] for asset in assets)),
               "unassigned_files": sum(file["disposition"] == "unassigned_needs_review" for report in reports for file in report["files"]),
               "published": False, "database_written": False, "model_called": False}
    batch = {"version": INTAKE_VERSION, "summary": summary, "packages": reports, "assets": assets}
    write_json(output / "batch.json", batch)
    lines = ["# 批量知识收件结果", "", "状态：候选待审；未写数据库、未调用模型 API、未向量化或发布。",
             "正文是规则清洗副本，模型判定建议不是内容修复或发布批准。", "",
             f"收到 {summary['package_count']} 包、{summary['file_count']} 文件；解析 {summary['parsed_files']} 文件，整理 {summary['asset_count']} 个候选版本，预览 {summary['chunk_count']} 个片段。",
             f"其中原主资产 {summary['primary_asset_count']} 个版本、参考库概览 {summary['reference_overview_count']} 份、编号案例派生 {summary['derived_case_count']} 条；派生条目不代表新增员工交付或真实性已核验。",
             f"另有 {summary['unassigned_files']} 份已解析材料尚待确认资产归属，完整状态见 batch.json。", "",
             "判定为“可入待审库”也不代表已批准共享；其余项先保留全文和依据，不自动丢弃。", "",
             "|候选|类型|判定建议|主要依据|全文|", "|---|---|---|---|---|"]
    for asset in assets:
        title = asset["metadata"]["title"].replace("|", "／").replace("\n", " ")
        materials = asset.get("review", {}).get("materials", [])
        reason = materials[0]["reasons"][0] if materials else "已完成基础解析与清洗，价值判断待补"
        reason = reason.replace("|", "／").replace("\n", " ")[:140]
        lines.append(f"|{title}|{asset['metadata']['kind']}|{DECISION_LABELS[asset['decision']]}|{reason}|[完整正文]({asset['body_file']})|")
    lines.extend(["", "## 异常与未归属附件", "", "下列材料仍保留在本地收件结果中，未当作无价值或已完整处理。详细证据、替换统计和版本关系见 [batch.json](batch.json)。", ""])
    for report in reports:
        for item in report["files"]:
            if item["disposition"] == "unassigned_needs_review":
                lines.append(f"- 待确认归属：{item['name']}（[已解析副本](cache/{item['cache_key']}.json)）")
            elif item["state"] not in {"parsed", "script_not_ingested"}:
                lines.append(f"- 未解析：{item['name']}；原因 `{item.get('error_code', item['state'])}`。")
    for asset in assets:
        if asset["state"] != "draft_ready":
            lines.append(f"- 正文导入被阻止：{asset['metadata']['title']}；完整副本已保留，原因：{', '.join(sorted({issue['code'] for issue in asset['issues']}))}。")
    lines.extend(["", "所有切片均保留父正文引用、章节与顺序。预览片段没有发布版本号；正式 revision 在导入草稿时分配。",
                  "未归属附件与无法解析文件不会被当成无价值；下一步由异常复核或指定归并关系处理。"])
    (output / "报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return batch


def import_batch(output, service, *, execute=False):
    """Import only individually selected AI-candidate suggestions as drafts.

    Conflicting versions and unresolved dependencies need curation first. This
    command cannot approve or publish; normal publication remains separate.
    """
    output = Path(output).resolve()
    batch = json.loads((output / "batch.json").read_text(encoding="utf-8"))
    model_file = output / "model_reviews.json"
    model_rows = json.loads(model_file.read_text(encoding="utf-8"))["assets"] if model_file.exists() else []
    results, skipped = [], []
    for asset in batch["assets"]:
        model_review = next((row for row in model_rows if row.get("candidate_id") == asset["candidate_id"]
                             and row.get("content_hash") == asset["content_hash"]), None)
        decision = asset["decision"]
        if decision == "review_required" and model_review is not None:
            decision = model_review.get("decision") if model_review.get("coverage_complete") is True else "review_required"
        if (decision != "candidate" or asset["state"] != "draft_ready"
                or model_review is not None and model_review.get("decision") != "candidate"):
            skipped.append({"candidate_id": asset["candidate_id"], "reason": "needs_review_or_curation"})
            continue
        accepted = _accepted_method_references(asset, asset.get("metadata", {}))
        if any(issue["code"] in {"same_asset_different_version", "draft_validation_failed", "missing_reference_acceptance_invalid"}
               or issue["code"] == "unresolved_reference" and not accepted for issue in asset["issues"]):
            skipped.append({"candidate_id": asset["candidate_id"], "reason": "version_or_dependency_unresolved"})
            continue
        try:
            files = [(output / asset[key]).resolve() for key in ("body_file", "metadata_file")]
            if any(output not in file.parents for file in files):
                raise ValueError("unsafe output path")
            content = files[0].read_text(encoding="utf-8")
            if sha(content) != asset["content_hash"] or file_hash(files[1]) != asset["metadata_hash"]:
                raise ValueError("generated asset changed; reprocess instead")
            entry = {**json.loads(files[1].read_text(encoding="utf-8")), "knowledge_id": asset["knowledge_id"], "content": content}
            if accepted and (_accepted_method_references(asset, entry) != accepted
                             or entry["evidence"].get("accepted_missing_references") != accepted
                             or entry["evidence"].get("source_complete") is not False):
                raise ValueError("missing-reference acceptance changed; reprocess instead")
            if model_review is not None:
                from .knowledge_judge import validate_judgment
                reviewed = clean_text(content)[0]
                position = 0
                for window in model_review["windows"]:
                    if window["status"] != "assessed" or window["start"] != position or not position < window["end"] <= len(reviewed):
                        raise ValueError("incomplete model coverage")
                    validate_judgment(window, reviewed[position:window["end"]])
                    position = window["end"]
                if position != len(reviewed):
                    raise ValueError("incomplete model coverage")
                decisions = {window["decision"] for window in model_review["windows"]}
                if "candidate" not in decisions or decisions & {"repair", "method_only"}:
                    raise ValueError("model summary conflicts with window decisions")
                entry["evidence"]["model_review"] = {"status": "ai_suggested", "decision": decision, "content_hash": asset["content_hash"]}
            result = service.import_document(entry, execute=execute)
            results.append({"candidate_id": asset["candidate_id"], "status": "draft" if execute else "dry_run",
                            "revision": result.get("revision")})
        except Exception:
            results.append({"candidate_id": asset["candidate_id"], "status": "failed"})
    return {"results": results, "skipped": skipped, "published": False, "will_call_embedding": False}
