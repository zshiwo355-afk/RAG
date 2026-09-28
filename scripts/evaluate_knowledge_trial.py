#!/usr/bin/env python3
"""Evaluate an isolated trial through its local knowledge API; default dry-run.

Manifest v1: trial_id, base_url, assets [{knowledge_id, revision, title,
content_file, content_sha256}], queries [{query_id, category, source, query,
expected_ids, forbidden_ids?, top_k?, must_be_empty?}]. Body paths are relative
to the manifest. Categories: locator (title/term), business
(manual_business/real_user), negative (negative_fixture).

Only --execute sends requests. The API may call its configured embedding/index
services. This script never loads credentials, changes data, or calls an LLM.
Reports contain queries, ranks and hashes, never full bodies or provider URLs.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import sys
import time
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit


ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
SOURCES = {"locator": {"title", "term"}, "business": {"manual_business", "real_user"},
           "negative": {"negative_fixture"}}


class InputError(ValueError):
    """A fixed validation code, safe to show without source/provider contents."""


def require(condition, message):
    if not condition:
        raise InputError(message)


def loopback_url(value):
    require(isinstance(value, str) and not any(c.isspace() for c in value), "invalid_base_url")
    try:
        url = urlsplit(value)
        host = url.hostname
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
        require(local and url.scheme in {"http", "https"} and not url.username and not url.password
                and url.path in {"", "/"} and not url.query and not url.fragment
                and url.port != 0, "base_url_must_be_loopback_origin")
    except (TypeError, ValueError):
        raise InputError("base_url_must_be_loopback_origin") from None
    return value.rstrip("/")


def read_manifest(path, base_url=None):
    raw = path.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    require(isinstance(data, dict) and type(data.get("schema_version")) is int
            and data["schema_version"] == 1, "invalid_schema_version")
    require(isinstance(data.get("trial_id"), str) and ID.fullmatch(data["trial_id"]), "invalid_trial_id")
    origin = loopback_url(base_url if base_url is not None else data.get("base_url"))
    require(isinstance(data.get("assets"), list) and data["assets"], "assets_required")
    assets = {}
    for asset in data["assets"]:
        require(isinstance(asset, dict), "invalid_asset")
        key, revision = asset.get("knowledge_id"), asset.get("revision")
        require(isinstance(key, str) and ID.fullmatch(key) and key not in assets, "duplicate_or_invalid_asset_id")
        require(type(revision) is int and 1 <= revision <= 2**63 - 1, "invalid_asset_revision")
        relative = asset.get("content_file")
        require(isinstance(relative, str) and relative and not Path(relative).is_absolute(), "body_path_must_be_relative")
        body_path = (path.parent / relative).resolve()
        require(body_path.is_relative_to(path.parent.resolve()), "body_path_outside_manifest_directory")
        require(body_path.stat().st_size <= 2_000_000, "standard_body_too_large")
        body = body_path.read_bytes()
        content = body.decode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        require(0 < len(body) <= 2_000_000 and content.strip() and len(content) <= 500_000, "invalid_standard_body")
        require(digest == asset.get("content_sha256"), "standard_body_hash_mismatch")
        assets[key] = {"revision": revision, "content_sha256": digest, "content_chars": len(content),
                       "title": asset.get("title", key)}
    require(isinstance(data.get("queries"), list) and data["queries"], "queries_required")
    seen = set()
    queries = []
    for query in data["queries"]:
        require(isinstance(query, dict), "invalid_query")
        key, category = query.get("query_id"), query.get("category")
        require(isinstance(key, str) and ID.fullmatch(key) and key not in seen, "duplicate_or_invalid_query_id")
        seen.add(key)
        require(isinstance(category, str) and isinstance(query.get("source"), str)
                and query["source"] in SOURCES.get(category, set()), "invalid_query_category_or_source")
        require(isinstance(query.get("query"), str) and query["query"].strip() and len(query["query"]) <= 4000, "invalid_query_text")
        expected, forbidden = query.get("expected_ids", []), query.get("forbidden_ids", [])
        for values in (expected, forbidden):
            require(isinstance(values, list) and all(isinstance(v, str) and ID.fullmatch(v) for v in values)
                    and len(values) == len(set(values)), "invalid_expected_or_forbidden_ids")
        require(set(expected) <= assets.keys() and not set(expected) & set(forbidden), "invalid_expected_assets")
        require(category == "negative" or expected, "positive_query_requires_expected_ids")
        top_k, empty = query.get("top_k", 5), query.get("must_be_empty", False)
        require(type(top_k) is int and 1 <= top_k <= 30, "invalid_top_k")
        require(type(empty) is bool and (not empty or category == "negative") and not (empty and expected), "invalid_empty_expectation")
        queries.append({"query_id": key, "category": category, "source": query["source"], "query": query["query"],
                        "expected_ids": expected, "forbidden_ids": forbidden, "top_k": top_k, "must_be_empty": empty})
    return {"trial_id": data["trial_id"], "base_url": origin, "assets": assets, "queries": queries,
            "manifest_sha256": hashlib.sha256(raw).hexdigest()}


class NoRedirect(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(url, payload=None, timeout=120):
    started = time.monotonic()
    status, result, error = None, None, None
    try:
        request = urllib_request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                                         headers={"Content-Type": "application/json"})
        # Stay on the supplied local origin, including when proxy variables or
        # a redirect response are present in the caller's environment.
        opener = urllib_request.build_opener(urllib_request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            data = response.read(64_000_001)
        require(len(data) <= 64_000_000, "response_too_large")
        result = json.loads(data.decode("utf-8"))
    except urllib_error.HTTPError as exc:
        status, error = exc.code, "HTTPError"
        exc.close()
    except Exception as exc:
        error = type(exc).__name__  # Never persist provider exception text/body.
    return status, result, round((time.monotonic() - started) * 1000, 2), error


def body_hash(record):
    if not isinstance(record, dict) or not isinstance(record.get("content"), str):
        return None
    try:
        return hashlib.sha256(record["content"].encode("utf-8")).hexdigest()
    except UnicodeError:
        return None


def body_matches(record, asset):
    return (body_hash(record) == asset["content_sha256"] == record.get("content_hash")
            and len(record["content"]) == asset["content_chars"]
            and type(record.get("revision")) is int and record["revision"] == asset["revision"]
            and record.get("status") == "published")


def evaluate_query(trial, query, timeout):
    started = time.monotonic()
    status, payload, elapsed, error = request_json(trial["base_url"] + "/api/knowledge/search", {
        "query": query["query"], "top_k": query["top_k"], "include_content": True,
    }, timeout)
    result = {**query, "http_status": status, "search_elapsed_ms": elapsed,
              "results": [], "errors": []}
    errors = result["errors"]
    rows = []
    if error or status != 200:
        errors.append("search_" + (error or "unexpected_http_status"))
    elif (not isinstance(payload, dict) or payload.get("ok") is not True
          or not isinstance(payload.get("results"), list)):
        errors.append("invalid_search_response")
    else:
        rows = payload["results"]
        if (len(rows) > query["top_k"] or type(payload.get("result_count")) is not int
                or payload["result_count"] != len(rows)):
            errors.append("invalid_result_count")
            rows = rows[:query["top_k"]]
    seen = set()
    ranks = dict.fromkeys(query["expected_ids"])
    for rank, row in enumerate(rows, 1):
        key = row.get("knowledge_id") if isinstance(row, dict) else None
        if not isinstance(key, str) or not ID.fullmatch(key):
            errors.append("invalid_returned_id")
            continue
        if key in seen:
            errors.append("duplicate_asset")
        seen.add(key)
        if key in ranks and ranks[key] is None:
            ranks[key] = rank
        if key in query["forbidden_ids"]:
            errors.append("forbidden_asset_returned")
        returned = {"rank": rank, "knowledge_id": key, "body_matches": False,
                    "read_body_matches": False}
        result["results"].append(returned)
        asset = trial["assets"].get(key)
        if asset is None:
            errors.append("asset_outside_trial")
            continue  # Do not follow a URL or retain content from another corpus.
        returned.update(title=asset["title"], revision=row.get("revision") if type(row.get("revision")) is int else None,
                        body_matches=body_matches(row, asset), content_sha256=body_hash(row),
                        expected_content_sha256=asset["content_sha256"], expected_revision=asset["revision"])
        if not returned["body_matches"]:
            errors.append("search_body_or_version_mismatch")
        score = row.get("score")
        returned["score"] = score if type(score) in {int, float} and -1e100 < score < 1e100 else None
        canonical = f"/api/knowledge/{key}?revision={asset['revision']}"
        if row.get("read_url") != canonical:
            errors.append("read_url_mismatch")
            continue
        returned["read_url"] = canonical
        read_status, read, read_elapsed, read_error = request_json(trial["base_url"] + canonical, timeout=timeout)
        returned.update(read_http_status=read_status, read_elapsed_ms=read_elapsed)
        if read_error or read_status != 200:
            errors.append("read_" + (read_error or "unexpected_http_status"))
        else:
            record = read.get("knowledge") if isinstance(read, dict) and read.get("ok") is True else None
            returned["read_content_sha256"] = body_hash(record)
            returned["read_body_matches"] = bool(isinstance(record, dict) and record.get("knowledge_id") == key
                                                 and body_matches(record, asset))
            if not returned["read_body_matches"]:
                errors.append("read_body_or_version_mismatch")
    if query["must_be_empty"] and rows:
        errors.append("expected_empty_result")
    result.update(expected_ranks=ranks, any_hit=any(v is not None for v in ranks.values()) if ranks else None,
                  expected_coverage=sum(v is not None for v in ranks.values()) / len(ranks) if ranks else None,
                  checks_ok=not errors, total_elapsed_ms=round((time.monotonic() - started) * 1000, 2))
    result["errors"] = sorted(set(errors))
    return result


def summarize(trial, results):
    groups = {}
    for category in SOURCES:
        rows = [r for r in results if r["category"] == category]
        expected = [r for r in rows if r["expected_ids"]]
        # A corrupt/wrong-version response must not earn a retrieval hit.
        hits = sum(r["checks_ok"] and r["any_hit"] for r in expected)
        groups[category] = {"queries": len(rows), "errors": sum(not r["checks_ok"] for r in rows),
                            "expected_queries": len(expected), "any_hits": hits,
                            "top1_hits": sum(r["checks_ok"] and 1 in r["expected_ranks"].values() for r in expected),
                            "any_hit_fraction": hits / len(expected) if expected else None,
                            "sources": sorted({r["source"] for r in rows})}
    latencies = sorted(r["search_elapsed_ms"] for r in results)
    return {"trial_id": trial["trial_id"], "manifest_sha256": trial["manifest_sha256"],
            "status": "completed" if all(r["checks_ok"] for r in results) else "completed_with_errors",
            "asset_count": len(trial["assets"]), "query_count": len(results), "groups": groups,
            "search_latency_ms": {"mean": round(sum(latencies) / len(latencies), 2),
                                  "p95": latencies[math.ceil(len(latencies) * .95) - 1]},
            "metric_scope": "定位题与业务样例分别计数；不代表真实用户 Recall@K；负例不默认要求空结果。"}


def markdown_report(summary, results):
    lines = ["# 隔离知识试用检索评测", "", summary["metric_scope"], "",
             "| 类别 | 题数 | 有期待答案题数 | 首位命中 | 期待集合任一命中 | 接口/全文校验失败 |",
             "|---|---:|---:|---:|---:|---:|"]
    labels = {"locator": "脚本定位题", "business": "业务自然语言样例", "negative": "负例观察"}
    for category, group in summary["groups"].items():
        lines.append(f"| {labels[category]} | {group['queries']} | {group['expected_queries']} | {group['top1_hits']} | {group['any_hits']} | {group['errors']} |")
    lines += ["", "命中只计接口、版本和全文校验均通过的响应。每题完整排名、错误和耗时见 queries.jsonl。", "",
              "| 题号 | 查询 | 期待 ID 排名 | 搜索耗时 ms | 校验错误 |", "|---|---|---|---:|---|"]
    for result in results:
        question = result["query"].replace("|", "\\|").replace("\n", " ")[:160]
        ranks = ", ".join(f"{k}: {v if v is not None else '未命中'}" for k, v in result["expected_ranks"].items()) or "未设相关答案"
        lines.append(f"| {result['query_id']} | {question} | {ranks} | {result['search_elapsed_ms']} | {', '.join(result['errors']) or '通过'} |")
    return "\n".join(lines) + "\n"


def execute_trial(trial, output, timeout):
    output.mkdir(parents=True, exist_ok=False)
    summary_path = output / "summary.json"
    summary_path.write_text(json.dumps({"trial_id": trial["trial_id"], "status": "running",
                                        "planned_queries": len(trial["queries"])}, ensure_ascii=False), encoding="utf-8")
    results = []
    with (output / "queries.jsonl").open("x", encoding="utf-8") as stream:
        for query in trial["queries"]:
            result = evaluate_query(trial, query, timeout)
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
            stream.flush()
            results.append(result)
    summary = summarize(trial, results)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "report.md").write_text(markdown_report(summary, results), encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--base-url")
    parser.add_argument("--out", type=Path, required=True, help="New report directory; never overwrite a prior run.")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--timeout", type=int, default=120, help="Per HTTP request timeout in seconds (1-300).")
    args = parser.parse_args(argv)
    try:
        trial = read_manifest(args.manifest, args.base_url)
        require(1 <= args.timeout <= 300, "invalid_timeout")
        if args.execute:
            summary = execute_trial(trial, args.out, args.timeout)
            print(json.dumps(summary, ensure_ascii=False))
            return 0 if summary["status"] == "completed" else 1
        print(json.dumps({"status": "dry_run", "asset_count": len(trial["assets"]),
                          "query_count": len(trial["queries"])}, ensure_ascii=False))
        return 0
    except InputError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    except (ValueError, OSError, UnicodeError):
        print(json.dumps({"status": "error", "error": "invalid_trial_input_or_output"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
