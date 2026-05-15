#!/usr/bin/env python3
"""Local Streamlit dashboard for the safe ingest wrapper pipeline."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import streamlit as st  # noqa: E402

from rag_app.ingest_dashboard_helpers import (  # noqa: E402
    RUN_ROOT,
    build_command,
    dashboard_report_paths,
    read_report,
    read_run_logs,
    required_confirmation_token,
    run_command,
    save_upload,
    validate_confirmation,
)


SOURCE_TYPE_LABELS = {
    "auto": "自动识别",
    "product_excel": "产品 Excel",
    "quality_report": "质检报告",
}

STEP_LABELS = {
    "dry_run": "生成预检计划",
    "build": "执行本地构建",
    "check_build": "检查构建结果",
    "embedding_dry_run": "向量化预检",
    "embedding_execute": "执行向量化",
    "push_dry_run": "入库预检",
    "check_opensearch": "检查 OpenSearch",
    "push_execute": "执行入库",
    "verify_pushed": "验证写入结果",
    "verify_retrieval": "验证检索召回",
}

REPORT_LABELS = {
    "当前 ingest_plan.jsonl": "当前预检计划",
    "product_excel build_report.md": "产品构建报告",
    "quality_report build_report.md": "质检报告构建报告",
    "embedding_report.md": "向量化报告",
    "push_report.md": "入库报告 Markdown",
    "push_report.json": "入库报告 JSON",
    "verify_report.md": "写入验证报告 Markdown",
    "verify_report.json": "写入验证报告 JSON",
    "retrieval_verify_report.md": "检索验证报告 Markdown",
    "retrieval_verify_report.json": "检索验证报告 JSON",
    "change_plan.md": "变更计划报告",
    "delete_report.md": "删除报告",
}


def ensure_session() -> None:
    st.session_state.setdefault("run_id", "")
    st.session_state.setdefault("run_dir", "")
    st.session_state.setdefault("uploaded_path", "")
    st.session_state.setdefault("upload_info", None)
    st.session_state.setdefault("last_result", None)


def apply_css() -> None:
    st.markdown(
        """
        <style>
        .stApp {
            background: #f5f7fb;
        }
        .block-container {
            padding-top: 1.6rem;
            padding-bottom: 2rem;
        }
        .hero {
            background: linear-gradient(135deg, #172554 0%, #1d4ed8 55%, #0f766e 100%);
            color: white;
            padding: 26px 30px;
            border-radius: 14px;
            margin-bottom: 18px;
            box-shadow: 0 10px 30px rgba(15, 23, 42, 0.15);
        }
        .hero h1 {
            margin: 0 0 8px 0;
            font-size: 34px;
            letter-spacing: 0;
        }
        .hero p {
            margin: 0;
            color: #dbeafe;
            font-size: 16px;
        }
        .status-card, .stage-card, .notice-card, .file-card {
            background: white;
            border: 1px solid #e5e7eb;
            border-radius: 12px;
            padding: 16px 18px;
            box-shadow: 0 4px 18px rgba(15, 23, 42, 0.06);
            margin-bottom: 14px;
        }
        .stage-card h3 {
            margin-top: 0;
            color: #0f172a;
        }
        .notice-card.safe {
            border-left: 5px solid #2563eb;
        }
        .notice-card.cost {
            border-left: 5px solid #f59e0b;
        }
        .notice-card.write {
            border-left: 5px solid #dc2626;
        }
        .metric-label {
            color: #64748b;
            font-size: 13px;
            margin-bottom: 4px;
        }
        .metric-value {
            color: #0f172a;
            font-size: 15px;
            font-weight: 650;
            overflow-wrap: anywhere;
        }
        .danger-text {
            background: #fff7ed;
            color: #9a3412;
            border: 1px solid #fed7aa;
            border-radius: 10px;
            padding: 12px 14px;
            margin: 10px 0;
        }
        .readonly-text {
            background: #ecfdf5;
            color: #166534;
            border: 1px solid #bbf7d0;
            border-radius: 10px;
            padding: 12px 14px;
            margin: 10px 0;
        }
        div.stButton > button {
            width: 100%;
            border-radius: 9px;
            border: 1px solid #cbd5e1;
            font-weight: 650;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def card(title: str, body: str, kind: str = "safe") -> None:
    st.markdown(
        f"""
        <div class="notice-card {kind}">
          <div class="metric-value">{title}</div>
          <div class="metric-label">{body}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_kv(label: str, value: Any) -> None:
    st.markdown(
        f"""
        <div class="status-card">
          <div class="metric-label">{label}</div>
          <div class="metric-value">{value or "未设置"}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_report(label: str, path: Path) -> None:
    report = read_report(path)
    with st.expander(label, expanded=False):
        st.caption(report["path"])
        if not report["exists"]:
            st.info(report["message"])
        elif report["type"] == "jsonl":
            st.dataframe(report["rows"], use_container_width=True, height=320)
        elif report["type"] == "json":
            st.json(report["json"])
        elif report["type"] == "markdown":
            st.markdown(report["content"])
        else:
            st.code(report["content"])


def execute_step(step: str, source_type: str, limit: int, token: str = "") -> None:
    if not st.session_state.get("uploaded_path") and step not in {"check_build", "check_opensearch", "verify_pushed", "verify_retrieval"}:
        st.error("请先上传文件。")
        return
    expected = required_confirmation_token(step)
    if expected and not validate_confirmation(step, token):
        st.error(f"请输入确认口令 {expected}")
        return
    run_id = st.session_state["run_id"]
    run_dir = Path(st.session_state["run_dir"])
    command = build_command(
        step=step,
        uploaded_path=st.session_state.get("uploaded_path") or "",
        source_type=source_type,
        run_dir=run_dir,
        limit=limit,
    )
    label = STEP_LABELS.get(step, step)
    with st.spinner(f"正在执行：{label}"):
        result = run_command(
            command=command,
            cwd=ROOT,
            run_id=run_id,
            step=step,
            log_path=run_dir / "run_log.jsonl",
        )
    st.session_state["last_result"] = result
    if result["status"] == "success":
        st.success("命令执行成功。")
    else:
        st.error("命令执行失败，请查看 stderr 和对应 report。")


def render_sidebar() -> tuple[str, int]:
    with st.sidebar:
        st.header("数据配置")
        source_type = st.selectbox(
            "数据类型",
            ["auto", "product_excel", "quality_report"],
            index=0,
            format_func=lambda value: SOURCE_TYPE_LABELS[value],
        )
        limit = int(st.number_input("处理条数限制", min_value=1, value=3, step=1))
        if limit > 3:
            st.warning("处理条数限制大于 3，会增加费用或写库影响面，请谨慎。")

        st.divider()
        st.header("上传文件")
        st.caption("支持 Excel / PDF / TXT / Markdown，单文件不超过 200MB。")
        uploaded = st.file_uploader("选择文件", type=["xlsx", "xls", "pdf", "txt", "md"])
        if uploaded and st.button("保存上传文件", type="primary"):
            try:
                run_id = st.session_state.get("run_id")
                if not run_id:
                    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                info = save_upload(ROOT, uploaded.name, uploaded.getvalue(), timestamp=run_id)
                run_dir = (ROOT / RUN_ROOT / run_id).resolve()
                run_dir.mkdir(parents=True, exist_ok=True)
                st.session_state["run_id"] = run_id
                st.session_state["run_dir"] = str(run_dir)
                st.session_state["uploaded_path"] = info["path"]
                st.session_state["upload_info"] = info
                st.success("上传已保存")
            except Exception as exc:
                st.error(str(exc))
    return source_type, limit


def render_file_card(source_type: str, limit: int) -> None:
    upload_info = st.session_state.get("upload_info")
    run_dir = st.session_state.get("run_dir") or "尚未创建"
    if upload_info:
        st.markdown('<div class="file-card">', unsafe_allow_html=True)
        st.subheader("当前文件")
        cols = st.columns([1.2, 1, 1.5, 2])
        cols[0].metric("文件名", upload_info["filename"])
        cols[1].metric("文件大小", f"{upload_info['size']} B")
        cols[2].metric("文件 SHA1", upload_info["sha1"][:16] + "...")
        cols[3].metric("数据类型", SOURCE_TYPE_LABELS[source_type])
        st.caption(f"保存路径：{upload_info['path']}")
        st.caption(f"当前运行目录：{run_dir}")
        st.markdown("</div>", unsafe_allow_html=True)
    else:
        st.info("请先在左侧上传文件。")

    cols = st.columns(4)
    with cols[0]:
        render_kv("当前文件", upload_info["filename"] if upload_info else "未上传")
    with cols[1]:
        render_kv("数据类型", SOURCE_TYPE_LABELS[source_type])
    with cols[2]:
        render_kv("处理条数限制", limit)
    with cols[3]:
        render_kv("安全状态", "默认只预检，不写库")


def render_command_output() -> None:
    st.subheader("最近一次执行状态")
    result = st.session_state.get("last_result")
    if not result:
        st.info("未执行")
        return

    if result["status"] == "success":
        st.success("命令执行成功。")
    else:
        st.error("命令执行失败，请查看 stderr 和对应 report。")

    cols = st.columns(3)
    cols[0].metric("状态", "成功" if result["status"] == "success" else "失败")
    cols[1].metric("返回码", result["returncode"])
    cols[2].metric("步骤", STEP_LABELS.get(result["step"], result["step"]))
    st.code(" ".join(result.get("command") or []), language="bash")
    with st.expander("标准输出 stdout", expanded=False):
        st.code(result.get("stdout_tail") or "无输出")
    with st.expander("错误输出 stderr", expanded=result["status"] != "success"):
        st.code(result.get("stderr_tail") or "无错误输出")


def render_stage_one(source_type: str, limit: int) -> None:
    st.markdown('<div class="stage-card"><h3>① 预检与本地构建</h3>', unsafe_allow_html=True)
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("生成预检计划", key="dry_run"):
            execute_step("dry_run", source_type, limit)
    with col2:
        build_ok = st.checkbox("我确认这一步只做本地构建，不调用 embedding，不写 OpenSearch。")
        if st.button("执行本地构建", key="build"):
            if build_ok:
                execute_step("build", source_type, limit)
            else:
                st.error("请先勾选本地构建确认框。")
    with col3:
        if st.button("检查构建结果", key="check_build"):
            execute_step("check_build", source_type, limit)
    st.markdown("</div>", unsafe_allow_html=True)


def render_stage_two(source_type: str, limit: int) -> None:
    st.markdown('<div class="stage-card"><h3>② 向量化 Embedding</h3>', unsafe_allow_html=True)
    st.markdown('<div class="danger-text">执行向量化会调用 embedding API，可能产生费用，但不会写 OpenSearch。</div>', unsafe_allow_html=True)
    col1, col2 = st.columns(2)
    with col1:
        if st.button("向量化预检", key="embedding_dry_run"):
            execute_step("embedding_dry_run", source_type, limit)
    with col2:
        embed_token = st.text_input("输入 YES_EMBED 后允许执行向量化", key="embed_token")
        if st.button("执行向量化", key="embedding_execute"):
            execute_step("embedding_execute", source_type, limit, embed_token)
    st.markdown("</div>", unsafe_allow_html=True)


def render_stage_three(source_type: str, limit: int) -> None:
    st.markdown('<div class="stage-card"><h3>③ OpenSearch 入库</h3>', unsafe_allow_html=True)
    st.markdown(
        '<div class="danger-text">执行入库会写 OpenSearch。默认跳过已存在 doc_id，不会删除任何数据。建议 limit=1 或 3。</div>',
        unsafe_allow_html=True,
    )
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("入库预检", key="push_dry_run"):
            execute_step("push_dry_run", source_type, limit)
    with col2:
        if st.button("检查 OpenSearch", key="check_opensearch"):
            execute_step("check_opensearch", source_type, limit)
    with col3:
        push_token = st.text_input("输入 YES_PUSH 后允许执行入库", key="push_token")
        if st.button("执行入库", key="push_execute"):
            execute_step("push_execute", source_type, limit, push_token)
    st.markdown("</div>", unsafe_allow_html=True)


def render_stage_four(source_type: str, limit: int) -> None:
    st.markdown('<div class="stage-card"><h3>④ 写入后验证</h3>', unsafe_allow_html=True)
    st.markdown('<div class="readonly-text">验证只读检查，不删除数据。</div>', unsafe_allow_html=True)
    col1, col2 = st.columns(2)
    with col1:
        if st.button("验证写入结果", key="verify_pushed"):
            execute_step("verify_pushed", source_type, limit)
    with col2:
        if st.button("验证检索召回", key="verify_retrieval"):
            execute_step("verify_retrieval", source_type, limit)
    st.markdown("</div>", unsafe_allow_html=True)


def render_console(source_type: str, limit: int) -> None:
    render_file_card(source_type, limit)
    render_stage_one(source_type, limit)
    render_stage_two(source_type, limit)
    render_stage_three(source_type, limit)
    render_stage_four(source_type, limit)
    render_command_output()


def render_reports() -> None:
    st.subheader("报告中心")
    run_dir = Path(st.session_state["run_dir"]) if st.session_state.get("run_dir") else ROOT / RUN_ROOT / "未创建"
    for label, path in dashboard_report_paths(ROOT, run_dir):
        render_report(REPORT_LABELS.get(label, label), path)


def render_logs() -> None:
    st.subheader("运行日志")
    if not st.session_state.get("run_dir"):
        st.info("暂无运行记录。")
        return
    rows = read_run_logs(Path(st.session_state["run_dir"]) / "run_log.jsonl", limit=20)
    if not rows:
        st.info("暂无运行记录。")
        return
    display_rows = []
    for row in rows:
        display_rows.append(
            {
                "步骤": STEP_LABELS.get(row.get("step"), row.get("step")),
                "状态": "成功" if row.get("status") == "success" else "失败",
                "开始时间": row.get("started_at"),
                "结束时间": row.get("finished_at"),
                "返回码": row.get("returncode"),
                "命令": " ".join(row.get("command") or []),
                "stdout 摘要": row.get("stdout_tail"),
                "stderr 摘要": row.get("stderr_tail"),
            }
        )
    st.dataframe(display_rows, use_container_width=True, height=420)


def render_help() -> None:
    st.subheader("推荐小样本流程")
    st.markdown(
        """
        1. 上传文件
        2. 生成预检计划
        3. 执行本地构建
        4. 检查构建结果
        5. 向量化预检
        6. 执行向量化，建议 limit=3
        7. 入库预检
        8. 检查 OpenSearch
        9. 执行入库，建议 limit=3
        10. 验证写入结果
        11. 验证检索召回
        """
    )
    st.subheader("安全边界")
    col1, col2, col3 = st.columns(3)
    with col1:
        card("完全离线", "上传、预检、本地构建、检查构建结果、向量化预检、入库预检。", "safe")
    with col2:
        card("可能产生费用", "执行向量化会调用 embedding API；检索验证可能调用查询向量。", "cost")
    with col3:
        card("会写 OpenSearch", "只有执行入库会写库，且必须输入 YES_PUSH。", "write")
    st.info("第一版不支持删除执行。删除必须继续在命令行审查 change_plan 后人工执行。")
    st.markdown(
        """
        **出错后看哪里**

        - 页面最近一次执行状态中的 stderr
        - 报告中心对应的 Markdown / JSON 报告
        - `uploads/ingest_runs/<run_id>/run_log.jsonl`
        """
    )


def main() -> None:
    st.set_page_config(
        page_title="RAG 数据导入管理台",
        page_icon="🧠",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    ensure_session()
    apply_css()
    source_type, limit = render_sidebar()

    st.markdown(
        """
        <div class="hero">
          <h1>RAG 数据导入管理台</h1>
          <p>上传文件、预检、构建、向量化、入库和验证的一站式安全操作台</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    cols = st.columns(3)
    with cols[0]:
        card("默认安全", "默认只做预检，不写 OpenSearch。", "safe")
    with cols[1]:
        card("费用提醒", "只有执行向量化和检索验证可能调用模型 API。", "cost")
    with cols[2]:
        card("写库提醒", "只有执行入库会写 OpenSearch，且必须输入 YES_PUSH。", "write")

    tab_console, tab_reports, tab_logs, tab_help = st.tabs(["操作台", "报告中心", "运行日志", "使用说明"])
    with tab_console:
        render_console(source_type, limit)
    with tab_reports:
        render_reports()
    with tab_logs:
        render_logs()
    with tab_help:
        render_help()


if __name__ == "__main__":
    main()
