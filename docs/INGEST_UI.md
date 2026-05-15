# RAG 数据导入管理台

`scripts/ingest_dashboard.py` 是本地 Streamlit 管理页面，用中文界面编排现有安全 wrapper 脚本。它不直接调用 embedding API，不直接连接 OpenSearch，不改 API 服务，不改检索逻辑，也不提供删除执行按钮。

## 启动页面

```bash
streamlit run scripts/ingest_dashboard.py
```

也可以使用：

```bash
python3 -m streamlit run scripts/ingest_dashboard.py
```

如果环境缺少 Streamlit：

```bash
python3 -m pip install -r requirements.txt
```

## 页面结构

页面分为 4 个 tab：

- `操作台`：上传文件、按步骤执行预检、构建、向量化、入库和验证。
- `报告中心`：查看 ingest plan、构建报告、向量化报告、入库报告、验证报告、变更/删除报告。
- `运行日志`：查看最近 20 条命令执行记录。
- `使用说明`：查看推荐流程、安全边界和失败排查入口。

侧边栏包含：

- `数据类型`：自动识别、产品 Excel、质检报告。
- `处理条数限制`：默认 3，超过 3 会提示风险。
- `上传文件`：支持 Excel / PDF / TXT / Markdown，单文件不超过 200MB。

上传文件会保存到：

```text
uploads/ingest/<run_id>/
```

页面会展示文件名、文件大小、文件 SHA1、保存路径和数据类型。

## 按钮和脚本映射

| 中文按钮 | 调用脚本 |
| --- | --- |
| 生成预检计划 | `python3 scripts/ingest_content.py --source <uploaded_path> --dry-run --type <source_type> --plan-output <run_dir>/ingest_plan.jsonl` |
| 执行本地构建 | `python3 scripts/ingest_content.py --source <uploaded_path> --execute --type <source_type> --yes --plan-output <run_dir>/ingest_plan.jsonl` |
| 检查构建结果 | `python3 scripts/ingest_content.py --check-build` |
| 向量化预检 | `python3 scripts/embed_ingest_build.py --source-type <source_type> --dry-run --limit <N>` |
| 执行向量化 | `python3 scripts/embed_ingest_build.py --source-type <source_type> --execute --limit <N> --yes` |
| 入库预检 | `python3 scripts/push_ingest_embeddings.py --source-type <source_type> --dry-run --limit <N>` |
| 检查 OpenSearch | `python3 scripts/push_ingest_embeddings.py --check-opensearch` |
| 执行入库 | `python3 scripts/push_ingest_embeddings.py --source-type <source_type> --execute --limit <N> --yes` |
| 验证写入结果 | `python3 scripts/push_ingest_embeddings.py --verify-pushed --verify-limit <N>` |
| 验证检索召回 | `python3 scripts/verify_retrieval.py --execute --verify-limit <N> --yes` |

页面内部命令使用 list 参数传给 subprocess，不拼接 shell 字符串。

## 风险说明

完全离线步骤：

- 上传文件
- 生成预检计划
- 执行本地构建
- 检查构建结果
- 向量化预检
- 入库预检

可能产生费用：

- `执行向量化` 会调用 embedding API。
- `验证检索召回` 会走真实检索，可能触发 query embedding。

会写 OpenSearch：

- 只有 `执行入库` 会写 OpenSearch。
- `检查 OpenSearch`、`验证写入结果`、`验证检索召回` 是只读检查或检索验证。

不会删除数据：

- 第一版页面不提供删除执行按钮。
- 删除仍需在命令行人工审查 `change_plan` 后使用现有删除执行器。

## YES_EMBED 和 YES_PUSH

`执行向量化` 前必须输入：

```text
YES_EMBED
```

含义：确认本次会调用 embedding API，可能产生费用，但不会写 OpenSearch。

`执行入库` 前必须输入：

```text
YES_PUSH
```

含义：确认本次会写 OpenSearch，默认沿用 wrapper 的 skip existing 行为，不会删除任何数据。

页面不暴露 `--force`，避免误覆盖或扩大影响面。

## 报告中心

报告中心展示：

- 当前预检计划 `run_dir/ingest_plan.jsonl`
- 产品构建报告
- 质检报告构建报告
- 向量化报告
- 入库报告 Markdown / JSON
- 写入验证报告 Markdown / JSON
- 检索验证报告 Markdown / JSON
- 变更计划报告
- 删除报告

文件不存在时显示“尚未生成”。JSONL 展示前 50 行表格，JSON 格式化展示，Markdown 直接渲染。

## 运行日志

每次点击按钮都会追加：

```text
uploads/ingest_runs/<run_id>/run_log.jsonl
```

记录包含：

- 步骤
- 状态
- 开始时间
- 结束时间
- 返回码
- 命令
- stdout 摘要
- stderr 摘要

页面显示最近 20 条记录。

## 推荐小样本验收流程

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

## 失败处理

优先查看：

- 操作台中的最近一次执行状态
- stdout / stderr 展开区
- 报告中心对应报告
- `uploads/ingest_runs/<run_id>/run_log.jsonl`

常见方向：

- dry-run 失败：检查上传文件扩展名、数据类型和 plan 输出。
- build 失败：查看构建报告和旧脚本 stderr。
- embedding 失败：检查 API key、模型额度和向量化报告。
- push 失败：先跑检查 OpenSearch，确认 `.env`、表名和 mapping。
- verify 失败：查看 missing doc_id、warnings 和检索验证报告。

## 重新启动页面

如果页面进程停止，重新运行：

```bash
streamlit run scripts/ingest_dashboard.py
```

已有上传文件、报告和运行日志保留在 `uploads/` 和 `output/` 下。
