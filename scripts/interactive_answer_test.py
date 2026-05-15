#!/usr/bin/env python3
"""
交互式终端测试 RAG 回答效果（只读调用 answer_service，不改数据与核心逻辑）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from rag_app.answer_service import answer_query
from rag_app.config import load_env, safe_text
from rag_app.rerank_service import DEFAULT_RERANK_MODEL

OUTPUT_DIR = ROOT / "output"
MD_LOG = OUTPUT_DIR / "interactive_answer_test_log.md"
JSON_LOG = OUTPUT_DIR / "interactive_answer_test_log.json"

BANNER_LINES = (
    "RAG 交互测试已启动，输入问题开始测试。",
    "输入 q / quit / exit 退出。",
    "输入 /debug 开关 debug 显示。",
    "输入 /urls 开关图片 URL 可访问性检查。",
    "输入 /save 保存本轮对话记录。",
)

EXAMPLE_QUESTIONS = (
    "红色礼盒春节送礼推荐哪些酒",
    "适合商务送礼的高端酱香酒有哪些",
    "有没有53度酱香型礼盒装",
    "大民族有哪些适合送礼的酒",
    "石荣霄有什么高端礼盒",
    "蓝色礼盒包装的白酒有哪些",
    "复古包装适合商务礼品的酱香酒",
    "宴请接待用什么酱香酒比较合适",
)

REQUIRED_ENV_VARS = (
    "DASHSCOPE_API_KEY",
    "OPENSEARCH_ENDPOINT",
    "OPENSEARCH_INSTANCE_ID",
    "OPENSEARCH_USERNAME",
    "OPENSEARCH_PASSWORD",
)


def collect_missing_env() -> list[str]:
    return [name for name in REQUIRED_ENV_VARS if not os.getenv(name, "").strip()]


def describe_url_check_result(status_code: int | None, err: str | None) -> str:
    if err:
        if "timed out" in err.lower() or "timeout" in err.lower():
            return "超时异常"
        return f"异常（{err}）"
    if status_code is None:
        return "异常（无状态码）"
    if 200 <= status_code < 300:
        return "可访问"
    if status_code == 403:
        return "无权限"
    if status_code == 404:
        return "未找到"
    return f"HTTP {status_code}"


def check_image_url_head_or_get(url: str, timeout: float = 5.0) -> tuple[int | None, str | None]:
    """返回 (status_code, error_message)。不下载正文。"""
    url = safe_text(url).strip()
    if not url:
        return None, "空 URL"

    def do_head() -> tuple[int | None, str | None]:
        req = urllib_request.Request(url, method="HEAD")
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:
                return resp.getcode() or 200, None
        except urllib_error.HTTPError as exc:
            return exc.code, None
        except urllib_error.URLError as exc:
            return None, safe_text(str(exc.reason or exc))
        except TimeoutError:
            return None, "timeout"
        except OSError as exc:
            return None, safe_text(str(exc))

    code, err = do_head()
    if err and "timeout" in err.lower():
        return None, "timeout"
    if err:
        return None, err

    # 部分站点不支持 HEAD，405 等时尝试 Range GET
    if code in (405, 501):
        req = urllib_request.Request(url, method="GET")
        req.add_header("Range", "bytes=0-0")
        try:
            with urllib_request.urlopen(req, timeout=timeout) as resp:
                return resp.getcode() or 200, None
        except urllib_error.HTTPError as exc:
            return exc.code, None
        except urllib_error.URLError as exc:
            return None, safe_text(str(exc.reason or exc))
        except TimeoutError:
            return None, "timeout"
        except OSError as exc:
            return None, safe_text(str(exc))

    return code, err


def format_routes(routes: Any) -> str:
    if isinstance(routes, list) and routes:
        return ", ".join(safe_text(r).strip() for r in routes if safe_text(r).strip())
    return ""


def print_debug_block(debug: dict[str, Any]) -> None:
    retrieval_count = debug.get("retrieval_count", 0)
    rerank_count = debug.get("rerank_count", 0)
    final_product_count = debug.get("final_product_count", 0)
    answer_model = safe_text(debug.get("answer_model"))
    rerank_model = safe_text(debug.get("rerank_model")).strip() or DEFAULT_RERANK_MODEL
    fallback_val = debug.get("fallback_used", False)
    fallback_used = "true" if fallback_val else "false"
    llm_err = debug.get("llm_error")
    if llm_err is None or safe_text(llm_err) == "":
        llm_error_display = "None"
    else:
        llm_error_display = safe_text(llm_err)

    print("Debug：")
    print(f"retrieval_count：{retrieval_count}")
    print(f"rerank_count：{rerank_count}")
    print(f"final_product_count：{final_product_count}")
    print(f"answer_model：{answer_model}")
    print(f"rerank_model：{rerank_model}")
    print(f"fallback_used：{fallback_used}")
    print(f"llm_error：{llm_error_display}")
    if safe_text(debug.get("error")):
        print(f"error：{safe_text(debug.get('error'))}")


def print_answer_block(
    query: str,
    payload: dict[str, Any],
    *,
    show_debug: bool,
    check_urls: bool,
) -> None:
    answer = safe_text(payload.get("answer"))
    products = payload.get("products") or []
    if not isinstance(products, list):
        products = []
    debug = payload.get("debug") if isinstance(payload.get("debug"), dict) else {}

    print("==============================")
    print("问题：")
    print(query)
    print()
    print("回答：")
    print(answer if answer else "（空）")
    print()
    print("推荐产品：")
    if not products:
        print("（无）")
    else:
        for index, product in enumerate(products, start=1):
            if not isinstance(product, dict):
                continue
            name = safe_text(product.get("product_name")) or safe_text(product.get("product_id")) or "（未命名）"
            pid = safe_text(product.get("product_id"))
            reason = safe_text(product.get("reason"))
            img = safe_text(product.get("best_image_url")).strip()
            img_line = img if img else "无图片 URL"
            score = product.get("score")
            try:
                score_s = f"{float(score):.6f}"
            except (TypeError, ValueError):
                score_s = safe_text(score) or "—"
            routes_s = format_routes(product.get("routes"))
            sources = product.get("sources") or []
            n_sources = len(sources) if isinstance(sources, list) else 0

            print(f"{index}. 产品名称：{name}")
            print(f"   product_id：{pid}")
            print(f"   推荐理由：{reason}")
            print(f"   图片URL：{img_line}")
            print(f"   分数：{score_s}")
            print(f"   routes：{routes_s}")
            print(f"   sources数量：{n_sources}")
            print()

    if show_debug:
        print_debug_block(debug)
        print()

    if check_urls and products:
        print("图片检查：")
        for index, product in enumerate(products, start=1):
            if not isinstance(product, dict):
                continue
            img = safe_text(product.get("best_image_url")).strip()
            if not img:
                print(f"{index}. （无 URL）跳过")
                continue
            try:
                code, err = check_image_url_head_or_get(img, timeout=5.0)
            except Exception as exc:
                code, err = None, safe_text(str(exc))
            label = describe_url_check_result(code, err)
            if code is not None and 200 <= code < 300:
                print(f"{index}. {code} {label}")
            elif code is not None:
                print(f"{index}. {code} {label}")
            else:
                print(f"{index}. {label}")

    print("==============================")


def build_log_record(query: str, payload: dict[str, Any]) -> dict[str, Any]:
    products = payload.get("products") or []
    if not isinstance(products, list):
        products = []
    best_urls = []
    for p in products:
        if isinstance(p, dict):
            best_urls.append(safe_text(p.get("best_image_url")).strip())
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "answer": payload.get("answer"),
        "products": products,
        "best_image_url": best_urls,
        "sources": [p.get("sources") for p in products if isinstance(p, dict)],
        "debug": payload.get("debug") if isinstance(payload.get("debug"), dict) else {},
    }


def append_markdown_record(record: dict[str, Any]) -> str:
    lines = [
        f"## {record['timestamp']}",
        "",
        f"**问题**：{record['query']}",
        "",
        "**回答**：",
        "",
        str(record.get("answer") or ""),
        "",
        "**products**（JSON）：",
        "",
        "```json",
        json.dumps(record.get("products"), ensure_ascii=False, indent=2),
        "```",
        "",
        "**best_image_url**：",
        "",
        json.dumps(record.get("best_image_url"), ensure_ascii=False),
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


def save_session(history: list[dict[str, Any]]) -> tuple[bool, str]:
    if not history:
        return False, "当前没有可保存的对话记录（尚未成功完成任何问答）。"

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, f"无法创建输出目录：{exc}"

    try:
        with JSON_LOG.open("w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except OSError as exc:
        return False, f"写入 JSON 失败：{exc}"

    try:
        header = "# RAG 交互测试对话记录\n\n"
        body = "".join(append_markdown_record(r) for r in history)
        with MD_LOG.open("w", encoding="utf-8") as f:
            f.write(header + body)
    except OSError as exc:
        return False, f"写入 Markdown 失败：{exc}"

    return True, f"已保存 {len(history)} 条记录到：\n  {MD_LOG}\n  {JSON_LOG}"


def run_one_query(
    query: str,
    *,
    show_debug: bool,
    check_urls: bool,
    history: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """执行一次查询；异常时打印说明并尽量返回 payload。"""
    payload: dict[str, Any] | None = None
    try:
        payload = answer_query(query)
    except Exception as exc:
        print("==============================")
        print("问题：")
        print(query)
        print()
        print("执行 answer_query 时发生未捕获异常（这通常不应发生）：")
        print(f"类型：{type(exc).__name__}")
        print(f"说明：{safe_text(str(exc)).strip()}")
        print("==============================")
        return None

    print_answer_block(query, payload, show_debug=show_debug, check_urls=check_urls)

    if history is not None:
        history.append(build_log_record(query, payload))

    return payload


def print_examples() -> None:
    print("示例问题：")
    for i, q in enumerate(EXAMPLE_QUESTIONS, start=1):
        print(f"{i}. {q}")


def interactive_loop(single_query: str | None) -> int:
    load_env(ROOT)
    missing = collect_missing_env()
    if missing:
        print("提示：以下环境变量未设置或为空，检索或回答可能失败：")
        for name in missing:
            print(f"  - {name}")
        print("（不会在终端打印任何密钥内容。）")
        print()

    show_debug = False
    check_urls = False
    history: list[dict[str, Any]] = []

    if single_query is not None:
        q = safe_text(single_query).strip()
        if not q:
            print("单轮模式下问题为空，已退出。")
            return 1
        run_one_query(q, show_debug=show_debug, check_urls=check_urls, history=None)
        return 0

    for line in BANNER_LINES:
        print(line)
    print()

    while True:
        try:
            raw = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return 0

        if not raw:
            print("输入为空，请重新输入问题（不会调用模型）。")
            continue

        lower = raw.lower()
        if lower in ("q", "quit", "exit"):
            print("已退出。")
            return 0

        if raw == "/debug":
            show_debug = not show_debug
            print(f"Debug 显示已{'开启' if show_debug else '关闭'}。")
            continue
        if raw == "/urls":
            check_urls = not check_urls
            print(f"图片 URL 可访问性检查已{'开启' if check_urls else '关闭'}。")
            continue
        if raw == "/save":
            ok, msg = save_session(history)
            print(msg)
            if not ok:
                print("(保存未成功)")
            continue
        if raw == "/examples":
            print_examples()
            continue

        run_one_query(raw, show_debug=show_debug, check_urls=check_urls, history=history)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="交互式测试 RAG 回答（answer_service.answer_query）。")
    p.add_argument("query", nargs="?", default=None, help="若提供，则单轮执行后退出。")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    return interactive_loop(args.query)


if __name__ == "__main__":
    raise SystemExit(main())
