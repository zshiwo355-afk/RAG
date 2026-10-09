"""Derived public-knowledge links; bounded process memory, no source writes."""
from __future__ import annotations

from collections import Counter, defaultdict, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import posixpath
import re
import threading
import time
from urllib.parse import unquote

MAX_DOCUMENTS = 1000
MAX_TERMS = 128
MAX_TERM_DOCUMENTS = 64
MAX_MENTIONS = 32
MAX_EDGES = 2000
MAX_BYTES = 2_800_000
EXCERPT = 120
RETRY_SECONDS = 60
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
LINK = re.compile(r"(?<!!)\[([^\]\n]*)\]\(((?:[^()\n]|\([^()\n]*\))+)\)")
WIKI = re.compile(r"\[\[([^\]\n]{1,240})\]\]")
WORDS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,39}|[\u3400-\u9fff]{2,}")
EDGE_STOP = set("的地得是在和与及或了着把被让将为对从到由我你他她它们这那该其各每有无不也就都很更已可需能要与于")
STOP = {"知识", "资料", "内容", "上传", "来源", "正文", "方法", "案例", "流程", "模板", "参考", "员工", "公司", "项目",
        "进行", "使用", "相关", "处理", "提供", "包括", "通过", "完成", "当前", "需要", "可以", "一个", "以及", "问题",
        "确认", "记录", "输出", "输入", "说明", "结果", "信息", "步骤", "适用", "测试", "边界", "场景", "能力",
        "系统", "执行", "操作", "功能", "支持", "情况", "部分", "工作", "实际", "不同", "对应", "是否", "方式",
        "未取得", "待确认", "待核验", "未确认", "待补充", "未验证", "the", "and", "for", "with", "from", "this", "that", "true", "false"}
PROVENANCE = re.compile(r"^(?:来源|来源定位|来源信息|源文件|源片段|采集信息|提交信息|沉淀说明|审计信息|真实性说明|证据说明|共享声明|source|provenance)$", re.I)


def _key(record):
    return (record["knowledge_id"], record["revision"], record["content_hash"], record["published_at"])


def _narrative(content, *, exclude_provenance=True):
    lines, fence, omitted_level = [], None, None
    for line in content.splitlines(keepends=True):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                fence = None
            lines.append(" " * len(line))
            continue
        if marker:
            fence = marker[1]
            lines.append(" " * len(line))
            continue
        heading = re.match(r"^\s*(#{1,6})\s+(.+?)\s*$", line)
        if heading:
            level = len(heading[1])
            if omitted_level is not None and level <= omitted_level:
                omitted_level = None
            if exclude_provenance and PROVENANCE.fullmatch(heading[2].strip("# :：")):
                omitted_level = level
        if omitted_level is not None or (exclude_provenance and re.match(r"^\s*(?:[-*]\s*)?(?:来源定位|来源文件|来源会话|采集时间|提交人|收件编号|资产编号|知识编号|内容哈希|SHA-?256)\s*[:：]", line, re.I)):
            lines.append(" " * len(line))
            continue
        lines.append(line)
    return re.sub(r"(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)", lambda match: " " * len(match[0]), "".join(lines), flags=re.S)


def _excerpt(text, offset):
    start = max(0, offset - 35)
    return text[start:start + EXCERPT].strip()


def _tokens(text):
    counts = Counter()
    for match in WORDS.finditer(text):
        word = match[0].lower()
        if word.isascii():
            if word not in STOP:
                counts[word] += 1
            continue
        for size in (2, 3, 4):
            for offset in range(len(word) - size + 1):
                token = word[offset:offset + size]
                if token[0] not in EDGE_STOP and token[-1] not in EDGE_STOP and token not in STOP:
                    counts[token] += 1
    return counts


def _features(record, content):
    narrative = _narrative(content)
    link_text = _narrative(content, exclude_provenance=False)
    mentions = []
    for pattern, kind in ((WIKI, "wiki"), (LINK, "markdown")):
        for match in pattern.finditer(link_text):
            target = match[1].split("|", 1)[0].strip() if kind == "wiki" else match[2].strip(" <>")
            if len(target) <= 500 and len(mentions) < MAX_MENTIONS:
                mentions.append((kind, target, _excerpt(content, match.start())))
    # Lexical recommendations are a bounded excerpt-based aid, not full semantic analysis.
    analyzed = narrative[:32_000]
    counts = _tokens(analyzed)
    counts.update({key: value * 3 for key, value in _tokens(record["title"][:240]).items()})
    retained = sorted(counts, key=lambda term: (-math.sqrt(counts[term]) * len(term), term))[:MAX_TERMS]
    lower = analyzed.lower()
    return {"terms": {term: counts[term] for term in retained}, "mentions": mentions,
            "snippets": {term: _excerpt(content, lower.find(term)) for term in retained if lower.find(term) >= 0}}


def _safe_path(value):
    value = unquote(value.split("#", 1)[0]).replace("\\", "/")
    if (not value or len(value) > 500 or value.startswith(("/", "~"))
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", value) or "\x00" in value):
        return None
    return posixpath.normpath(value)


def _edge(source, target, relation, label, reason, *, evidence=(), terms=(), directed=False):
    identity = "|".join((relation, source, target))
    return {"id": hashlib.sha256(identity.encode()).hexdigest()[:24], "source": source, "target": target,
            "relation": relation, "label": label, "directed": directed, "reason": reason,
            "evidence": list(evidence), "terms": list(terms)}


def _references(records, features):
    titles, paths = defaultdict(set), defaultdict(set)
    for kid, record in records.items():
        titles[record["title"].strip()].add(kid)
        for alias in record["source_aliases"]:
            path = _safe_path(alias["locator"])
            namespace = record.get("source_namespace")
            if path and isinstance(namespace, str) and namespace:
                paths[(namespace, alias["name"], path)].add(kid)
    edges = {}
    for kid, record in records.items():
        for target in record["recorded_related_ids"]:
            if target in records and target != kid:
                edges[(kid, target)] = _edge(kid, target, "reference", "已记录关联", "原资料元数据记录的关联；不是自动推断的引用。", directed=True)
        for kind, raw, excerpt in features.get(kid, {}).get("mentions", []):
            destination = raw.split("#", 1)[0]
            token = destination.removeprefix("knowledge://").removeprefix("knowledge:")
            targets = {token} if ID.fullmatch(token) and token in records else set()
            if not targets and kind == "wiki":
                targets = titles.get(destination, set())
            if not targets and kind == "markdown":
                path = _safe_path(raw)
                namespace = record.get("source_namespace")
                if path and isinstance(namespace, str) and namespace:
                    for alias in record["source_aliases"]:
                        origin = _safe_path(alias["locator"])
                        if origin:
                            targets |= paths.get((namespace, alias["name"], posixpath.normpath(posixpath.join(posixpath.dirname(origin), path))), set())
            if len(targets) == 1:
                target = next(iter(targets))
                if target != kid:
                    edges[(kid, target)] = _edge(kid, target, "reference", "正文引用", "正文包含可唯一定位到这份正式知识的链接。",
                        evidence=[{"knowledge_id": kid, "revision": record["revision"], "excerpt": excerpt}], directed=True)
    return list(edges.values())


def _related(records, features):
    posting = defaultdict(dict)
    for kid, feature in features.items():
        for term, count in feature["terms"].items():
            if term in feature["snippets"]:
                posting[term][kid] = count
    size = len(features)
    # shortcut: skip common terms to bound candidate expansion; revisit with a
    # dedicated retrieval index if the 1,000-document graph limit is raised.
    max_frequency = min(MAX_TERM_DOCUMENTS, max(3, int(size * .4)))
    vectors = defaultdict(dict)
    for term, documents in posting.items():
        if len(documents) > max_frequency:
            continue
        idf = math.log((size + 1) / (len(documents) + 1)) + 1
        for kid, count in documents.items():
            vectors[kid][term] = math.sqrt(count) * idf
    norms = {kid: math.sqrt(sum(weight * weight for weight in vector.values())) for kid, vector in vectors.items()}
    pairs = defaultdict(float)
    for term, documents in posting.items():
        if not 2 <= len(documents) <= max_frequency:
            continue
        ids = sorted(documents)
        for i, left in enumerate(ids):
            for right in ids[i + 1:]:
                pairs[(left, right)] += vectors[left][term] * vectors[right][term]
    candidates, ranked = {}, defaultdict(list)
    for (left, right), dot in pairs.items():
        if not norms.get(left) or not norms.get(right):
            continue
        score = dot / (norms[left] * norms[right])
        if score < .15:
            continue
        shared = vectors[left].keys() & vectors[right].keys()
        terms = []
        for term in sorted(shared, key=lambda term: (-vectors[left][term] * vectors[right][term], -len(term), term)):
            if not any(term in existing or existing in term for existing in terms):
                terms.append(term)
            if len(terms) == 2:
                break
        if len(terms) < 2:
            continue
        candidates[(left, right)] = (score, terms)
        ranked[left].append((score, right))
        ranked[right].append((score, left))
    top = {kid: {other for _, other in sorted(items, key=lambda item: (-item[0], item[1]))[:3]} for kid, items in ranked.items()}
    result = []
    for (left, right), (score, terms) in sorted(candidates.items(), key=lambda item: (-item[1][0], item[0])):
        if right not in top[left] or left not in top[right]:
            continue
        evidence = [{"knowledge_id": kid, "revision": records[kid]["revision"],
                     "excerpt": features[kid]["snippets"][terms[0]]} for kid in (left, right)]
        result.append(_edge(left, right, "related", "内容相关", "共同用词：" + "、".join(terms) + "。基于正文用词的推荐，不代表引用或已验证的事实。",
                            evidence=evidence, terms=terms))
    return result


class KnowledgeGraph:
    def __init__(self, *, executor=None, clock=time.monotonic, max_documents=MAX_DOCUMENTS):
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="knowledge-graph")
        self.clock = clock
        self.max_documents = min(max_documents, MAX_DOCUMENTS)
        self.lock = threading.RLock()
        self.states = OrderedDict()
        self.pending_total = 0

    @staticmethod
    def _identity(catalog):
        identity = catalog.database_url or str(catalog.path.resolve())
        return hashlib.sha256(identity.encode()).hexdigest()

    def _load(self, catalog, record, state):
        key = _key(record)
        try:
            with self.lock:
                if state["expected"].get(record["knowledge_id"]) != key:
                    return
            # Each worker owns its OSS client; never share its mutable HTTP session.
            from .knowledge_store import KnowledgeStore
            reader = KnowledgeStore(database_url=catalog.database_url) if catalog.database_url else KnowledgeStore(catalog.path)
            body = reader.get_published(record["knowledge_id"])
            if body is None or _key(body) != key:
                raise ValueError("publication_changed")
            feature = _features(record, body["content"])
        except Exception:
            with self.lock:
                if state["expected"].get(record["knowledge_id"]) == key:
                    state["failed"][key] = self.clock()
        else:
            with self.lock:
                if state["expected"].get(record["knowledge_id"]) == key:
                    state["features"][key] = feature
                    state["failed"].pop(key, None)
        finally:
            with self.lock:
                state["pending"].discard(key)
                self.pending_total -= 1

    def read(self, catalog, kind_for):
        initial = catalog.graph_internal_snapshot()
        selected = initial[:self.max_documents]
        expected = {row["knowledge_id"]: _key(row) for row in selected}
        identity = self._identity(catalog)
        with self.lock:
            state = self.states.setdefault(identity, {"expected": {}, "features": {}, "pending": set(), "failed": {},
                                                       "edges_key": None, "edges": [], "edges_truncated": False})
            self.states.move_to_end(identity)
            while len(self.states) > 2:
                _, evicted = self.states.popitem(last=False)
                evicted["expected"] = {}
                evicted["features"].clear()
                evicted["edges"].clear()
            state["expected"] = expected
            keys = set(expected.values())
            state["features"] = {key: value for key, value in state["features"].items() if key in keys}
            state["failed"] = {key: value for key, value in state["failed"].items() if key in keys}
            for record in selected:
                key = _key(record)
                if (key not in state["features"] and key not in state["pending"]
                        and self.clock() - state["failed"].get(key, -RETRY_SECONDS) >= RETRY_SECONDS):
                    if self.pending_total >= MAX_DOCUMENTS:
                        break
                    state["pending"].add(key)
                    self.pending_total += 1
                    try:
                        self.executor.submit(self._load, catalog, record, state)
                    except Exception:
                        state["pending"].discard(key)
                        self.pending_total -= 1
                        state["failed"][key] = self.clock()
            features = {kid: state["features"][key] for kid, key in expected.items() if key in state["features"]}
            failed = set(state["failed"])
            edges_key = (frozenset(keys), frozenset(expected[kid] for kid in features))
            if state["edges_key"] != edges_key:
                records = {record["knowledge_id"]: record for record in selected}
                edges = _references(records, features)
                explicit_pairs = {frozenset((edge["source"], edge["target"])) for edge in edges}
                edges += [edge for edge in _related(records, features)
                          if frozenset((edge["source"], edge["target"])) not in explicit_pairs]
                retained, budget = [], MAX_BYTES
                for edge in edges:
                    length = len(json.dumps(edge, ensure_ascii=False).encode()) + 2
                    if len(retained) >= MAX_EDGES or length > budget:
                        continue
                    retained.append(edge)
                    budget -= length
                state.update(edges_key=edges_key, edges=retained, edges_truncated=len(retained) < len(edges))
            edges = state["edges"]
            edges_truncated = state["edges_truncated"]
        # Publication may change during OSS work or graph generation. Only fresh
        # pointers authorize nodes, links, and the snippets attached to those links.
        fresh = catalog.graph_internal_snapshot()
        current = fresh[:self.max_documents]
        stable = {row["knowledge_id"] for row in current if expected.get(row["knowledge_id"]) == _key(row)}
        nodes = [{"knowledge_id": row["knowledge_id"], "title": row["title"][:240], "kind": kind_for(row),
                  "revision": row["revision"], "uploader_position": row["uploader_position"]} for row in current]
        indexed = len(stable.intersection(features))
        failed_count = sum(_key(row) in failed for row in current)
        output = []
        budget = MAX_BYTES - len(json.dumps(nodes, ensure_ascii=False).encode()) - 2048
        truncated = len(fresh) > self.max_documents or edges_truncated
        for edge in edges:
            if edge["source"] not in stable or edge["target"] not in stable:
                continue
            length = len(json.dumps(edge, ensure_ascii=False).encode()) + 2
            if len(output) >= MAX_EDGES or length > budget:
                truncated = True
                continue
            budget -= length
            output.append(edge)
        counts = {"published_assets": len(fresh), "indexed_assets": indexed, "failed_assets": failed_count,
                  "explicit_edges": sum(edge["relation"] == "reference" for edge in output),
                  "related_edges": sum(edge["relation"] == "related" for edge in output)}
        status = "building" if indexed + failed_count < len(current) else "partial" if failed_count or truncated else "ready"
        return {"nodes": nodes, "edges": output, "status": status, "counts": counts, "truncated": truncated,
                "generated_at": datetime.now(timezone.utc).isoformat()}
