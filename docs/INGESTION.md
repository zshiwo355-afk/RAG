# 新增内容预检与导入说明

本文档记录当前项目的真实新增内容流程。当前推荐使用 `scripts/ingest_content.py` 作为统一入口：默认只做 dry-run 预检；只有显式传 `--execute` 并确认后，才会调用现有本地构建脚本。embedding 和 OpenSearch push 仍需继续使用原脚本手动执行。

## 安全原则

- 先预检，再导入：先运行 `scripts/ingest_content.py --dry-run`，确认 `source_sha1`、`source_doc_id`、`product_id` 推断结果。
- 先 dry-run，再正式写入：现有 push 脚本都支持 `--dry-run`，正式推送前应先生成报告。
- 不要随意全量重跑：全量 embedding 会重复调用模型，全量 push 可能覆盖或重复写入现有 OpenSearch 记录。
- 保留 output 产物：删除本地 `output/*.jsonl` 会影响恢复、删除和排查。

## 新增产品 Excel 的当前流程

产品 Excel 主要来源目录是：

```bash
素材/
产品信息/
```

真实链路如下：

```text
Excel 文件
-> scripts/excel_clean.py
-> output/products_cleaned.json / output/products_cleaned.csv
-> scripts/extract_excel_images.py
-> output/image_mapping.json / output/images/
-> scripts/embed_images.py
-> output/images_embedded.jsonl
-> scripts/analyze_images.py
-> output/image_analysis.jsonl
-> scripts/merge_image_into_products.py
-> output/products_enriched.json
-> scripts/build_documents.py
-> output/documents_preview_v2.json / output/documents_preview_v2.csv
-> scripts/embed_documents.py
-> output/documents_embedded_v2.jsonl
-> scripts/push_text_docs_to_opensearch.py
-> OpenSearch text_docs
```

图片相关还有两条入库链路：

```text
output/images_embedded.jsonl
-> scripts/push_image_vectors_to_opensearch.py
-> OpenSearch image_vectors
```

```text
output/image_analysis.jsonl
-> scripts/build_image_text_docs.py
-> output/image_text_docs.jsonl
-> scripts/embed_image_text_docs.py
-> output/image_text_embedded.jsonl
-> scripts/push_image_text_docs_to_opensearch.py
-> OpenSearch image_text_docs
```

只生成本地 output 的步骤：

- `excel_clean.py`
- `extract_excel_images.py`
- `embed_images.py`
- `analyze_images.py`
- `merge_image_into_products.py`
- `build_documents.py`
- `embed_documents.py`
- `build_image_text_docs.py`
- `embed_image_text_docs.py`

会写入 OpenSearch 的步骤：

- `push_text_docs_to_opensearch.py`
- `push_image_vectors_to_opensearch.py`
- `push_image_text_docs_to_opensearch.py`

## 新增质检报告 PDF 的当前流程

质检报告 PDF 来源目录是：

```bash
质检报告/
```

真实链路如下：

```text
质检报告/**/*.pdf
-> scripts/build_quality_report_docs.py
-> output/quality_report_documents.json
-> scripts/embed_documents.py --input output/quality_report_documents.json ...
-> output/quality_report_documents_embedded.jsonl 或增量 embedding JSONL
-> scripts/push_text_docs_to_opensearch.py --input 对应 embedding JSONL ...
-> OpenSearch text_docs
```

`build_quality_report_docs.py` 会读取：

- `output/products_enriched.json`
- `config/quality_report_product_map.csv`
- `output/quality_report_manifest.json`

它会维护 PDF 级 manifest，已处理且 sha1 不变的 PDF 会跳过；sha1 变化的 PDF 会标记为 `changed`，不会自动覆盖旧补充文档。

## 风险点

- 重复写入：push 脚本使用 OpenSearch `cmd: add`，项目层没有统一 upsert/replace 语义说明。
- 漏删旧数据：更新已有产品或 PDF 时，如果旧 `doc_id` 没有先定位并删除，OpenSearch 里可能残留旧内容。
- 全量重跑成本高：embedding 会调用外部模型，既有费用风险，也可能生成和旧版本不同的向量产物。
- product_id 不稳定风险：项目已有 `build_product_catalog.py` 尝试建立稳定产品目录，但主导入链路还没有完全以该 catalog 为唯一来源。
- 本地产物与 OpenSearch 不一致：删除脚本依赖本地 JSONL 收集 id，如果本地文件缺失或不是最新，可能漏删。

## 第 6.5 阶段：基于 change_plan 的安全删除执行器

删除 OpenSearch 文档前，必须先生成并审查变更计划。执行器只接受：

```bash
output/ingest_changes/change_plan.json
```

它只会根据计划中明确列出的 `target_doc_ids` 删除文档，不会按 `source_doc_id` 删除，不会按 query 批量删除，也不会删除本地 `output/` 文件或 manifest 历史记录。

先生成删除计划：

```bash
python3 scripts/plan_ingest_changes.py --mode delete-source --source-doc-id <source_doc_id> --check-opensearch --dry-run
```

查看计划：

```bash
sed -n '1,200p' output/ingest_changes/change_plan.md
```

执行器默认是 dry-run，不连接也不写 OpenSearch：

```bash
python3 scripts/apply_ingest_change_plan.py --plan output/ingest_changes/change_plan.json --dry-run
```

执行器会拒绝任何不安全计划。必须同时满足：

- `mode` 是 `delete-source` 或 `rollback-push`
- `safe_to_execute=true`
- `blockers=[]`
- `target_doc_ids` 非空且没有重复
- `target_indexes` 非空

真正删除时必须显式传 `--execute`，并在交互提示中输入 `yes`：

```bash
python3 scripts/apply_ingest_change_plan.py --plan output/ingest_changes/change_plan.json --execute --verify-after
```

自动化环境中如果已经人工确认计划，可以加 `--yes` 跳过交互确认，但仍然必须通过所有安全校验：

```bash
python3 scripts/apply_ingest_change_plan.py --plan output/ingest_changes/change_plan.json --execute --yes --verify-after
```

`--verify-after` 会在删除后只读 fetch 同一批 `doc_id`：

- 查不到：记为 `verified_missing`
- 仍然查得到：记为 `verify_failed`

每次运行都会生成报告：

```bash
output/ingest_changes/delete_report.md
output/ingest_changes/delete_report.json
```

报告包含执行模式、source 信息、目标索引、请求删除的 `doc_id`、已删除、已缺失、失败、删除后验证结果、拒绝原因和风险摘要。

正式删除完成后，会向 manifest 追加 `event_type=delete` 事件，记录：

- `delete_mode`
- `source_doc_id`
- `source_sha1`
- `target_indexes`
- `requested_doc_ids`
- `deleted_doc_ids`
- `already_missing_doc_ids`
- `failed_doc_ids`
- `delete_status`
- `deleted_at`

回滚某次 push 时，先根据 push report 生成回滚计划，再执行同一个安全执行器：

```bash
python3 scripts/plan_ingest_changes.py --mode rollback-push --from-push-report output/ingest_push/push_report.json --check-opensearch --dry-run
python3 scripts/apply_ingest_change_plan.py --plan output/ingest_changes/change_plan.json --execute --verify-after
```

删除后建议继续运行 verify-pushed / verify-retrieval 相关检查，确认受影响 `doc_id` 已不再可取回，并观察检索结果是否符合预期。

当前阶段仍然不做自动更新、不做自动重新 embedding、不做自动重新 push，也不删除任何本地文件。

## 新增前检查

推荐流程第一步，先运行统一入口的只读预检：

```bash
python3 scripts/ingest_content.py --source 质检报告 --dry-run
```

或检查全部默认来源：

```bash
python3 scripts/ingest_content.py --dry-run
```

重点看：

- `source_sha1` 是否和预期文件一致。
- `source_doc_id` 是否稳定。
- `product_id` 是否能推断出来；若是 `unknown`，需要补充映射或人工确认。
- `impact_types` 是否符合预期。
- `output/ingest_plan.jsonl` 中是否有不该导入的临时文件。

## 推荐操作方式

第一步：生成导入计划。

```bash
python3 scripts/ingest_content.py --source 质检报告 --dry-run
```

第二步：检查计划文件。

```bash
sed -n '1,20p' output/ingest_plan.jsonl
```

第三步：确认无误后执行本地构建阶段。

```bash
python3 scripts/ingest_content.py --source 质检报告 --execute
```

如果是在自动化环境中，并且已经确认计划无误，可以跳过交互确认：

```bash
python3 scripts/ingest_content.py --source 质检报告 --execute --yes
```

第四步：继续使用原脚本执行 embedding 和 OpenSearch push，并验证检索/问答效果。

`ingest_content.py --execute` 当前只会调用现有本地构建脚本：

- `quality_report`：调用 `scripts/build_quality_report_docs.py`
- `product_excel`：调用 `scripts/build_product_excel_pipeline.py`，该 wrapper 会在隔离工作目录中串起旧 Excel 清洗链路，再生成标准 `documents.json`

它不会调用 embedding 脚本，也不会写入 OpenSearch。

第五步：检查标准本地构建产物。

```bash
python3 scripts/ingest_content.py --check-build
```

确认没有空 `doc_id`、空 `source_doc_id`、空 `product_id` 或重复 `doc_id` 后，下一阶段才进入 embedding。

## Manifest 记录

统一入口会读取和写入：

```bash
output/ingest_manifest.jsonl
```

manifest 是追加式 JSONL 日志，用来记录哪些源文件已经被本地构建阶段处理过。每条记录包含：

- `source_doc_id`
- `source_path`
- `source_sha1`
- `source_type`
- `product_id`
- `generated_doc_ids`
- `embedding_model`
- `vector_tables`
- `status`
- `status_detail`
- `created_at`
- `updated_at`

当前阶段没有执行 embedding 和 OpenSearch push，所以：

- 如果本地标准产物里能读取到 `metadata.doc_id`，`generated_doc_ids` 会写入这些 doc id。
- 如果只生成 `needs_mapping.jsonl` 或旧脚本没有返回 doc id，`generated_doc_ids` 会写空数组。
- `embedding_model` 暂时为空。
- `vector_tables` 暂时写空数组。
- `status_detail` 会记录本次调用了哪些旧构建脚本。

dry-run 会把 manifest 判断结果写回 plan JSONL：

- `is_seen_source_doc_id`
- `is_seen_source_sha1`
- `suggested_action`

`suggested_action` 含义：

- `new`：新文件，建议处理。
- `unchanged`：内容已成功处理且 sha1 相同，默认跳过。
- `changed`：同一路径或同一 source 标识出现不同 sha1，建议人工确认后重新处理。
- `duplicate_content`：不同 source 记录出现相同 sha1，疑似重复文件。
- `unknown`：现有记录不足，无法可靠判断。

查看导入历史：

```bash
python3 scripts/ingest_content.py --status
```

如果只想检查某次计划：

```bash
python3 scripts/ingest_content.py --source 质检报告 --dry-run
sed -n '1,20p' output/ingest_plan.jsonl
```

默认行为会跳过已成功处理且内容未变的文件。等价参数是：

```bash
python3 scripts/ingest_content.py --source 质检报告 --execute --skip-existing
```

如果确认需要重新处理，例如修复了上游构建逻辑、想重建本地文档，使用：

```bash
python3 scripts/ingest_content.py --source 质检报告 --execute --force
```

`--force` 只影响本地构建阶段是否跳过记录，不会执行 embedding，也不会写 OpenSearch。

## 本地构建产物规范

第三阶段开始，统一本地构建目录是：

```text
output/ingest_build/
├── product_excel/
│   ├── documents.json
│   ├── needs_mapping.jsonl
│   └── build_report.md
└── quality_report/
    ├── documents.json
    └── build_report.md
```

这些文件不会替换旧 `output/` 产物，只是把后续 embedding 可消费的本地文档集中放到一个标准位置。

### product_excel 构建产物

第 3.5 阶段开始，推荐通过统一入口执行产品 Excel 本地 pipeline：

```bash
python3 scripts/ingest_content.py --source 素材/新文件.xlsx --execute --type product_excel
```

`ingest_content.py` 会调用：

```bash
python3 scripts/build_product_excel_pipeline.py --source 素材/新文件.xlsx --output-dir output/ingest_build/product_excel --yes
```

`scripts/build_product_excel_pipeline.py` 是旧产品 Excel 链路的外围编排器。它会先把指定 Excel 复制到隔离工作目录：

```text
output/product_excel_pipeline/
```

然后在这个工作目录内运行旧脚本，旧链路产物会落在：

```text
output/product_excel_pipeline/output/
```

标准本地构建产物会落在：

```text
output/ingest_build/product_excel/
```

产品 Excel 本地 pipeline 的真实步骤是：

```text
scripts/excel_clean.py
-> output/products_cleaned.json
-> scripts/extract_excel_images.py
-> output/image_mapping.json / output/images/
-> output/image_analysis.jsonl
-> scripts/merge_image_into_products.py
-> output/products_enriched.json
-> scripts/build_documents.py
-> output/documents_preview_v2.json
-> scripts/build_product_excel_docs.py
-> output/ingest_build/product_excel/documents.json
```

注意：

- 图片 AI 分析默认不自动执行，因为 `scripts/analyze_images.py` 可能调用外部模型/API。
- 默认会创建空的 `image_analysis.jsonl`，因此 pipeline 会优先完成文本和图片映射相关本地构建。
- 如果确实已有可信的 `output/image_analysis.jsonl`，可以直接运行 `scripts/build_product_excel_pipeline.py --reuse-existing-image-analysis` 复用旧分析结果。
- 如果旧脚本只能写固定 `output/` 路径，pipeline 会通过隔离 `--work-dir` 避免覆盖项目根目录下的重要旧产物。

`scripts/build_product_excel_docs.py` 是标准产物适配脚本：

- 如果指定 Excel 已经由 pipeline 清洗并生成 `documents_preview_v2.json`，会优先把这些文档转换/复制到 `output/ingest_build/product_excel/documents.json`。
- 如果能在 `products_enriched.json` 中按 `source_file` 找到产品记录，也会生成 `documents.json`。
- 如果 Excel 字段不匹配、旧清洗脚本无法识别产品记录，或指定 Excel 没有进入 `products_enriched.json`，不会猜测字段映射，会生成 `output/ingest_build/product_excel/needs_mapping.jsonl`，并标记 `status=needs_mapping`。

`product_excel/documents.json` 是 JSON list，每条为：

```json
{
  "page_content": "用于后续 embedding 的文本",
  "metadata": {
    "doc_id": "...",
    "product_id": "...",
    "product_name": "...",
    "doc_type": "product_excel_source_row",
    "field_name": "source_row",
    "source_kind": "product_excel",
    "source_file": "素材/xxx.xlsx",
    "source_sheet": "...",
    "source_row": "...",
    "source_doc_id": "product__...",
    "source_sha1": "",
    "build_status": "ready_from_products_enriched"
  }
}
```

`product_excel/needs_mapping.jsonl` 每行是一个待人工确认的 sheet 摘要：

```json
{
  "source_path": "产品信息/xxx.xlsx",
  "source_type": "product_excel",
  "source_sheet": "...",
  "source_doc_id": "...",
  "product_id": "unknown",
  "status": "needs_mapping",
  "header_row": 1,
  "headers": ["产品名称", "包装", "规格"],
  "row_count": 8,
  "preview_rows": []
}
```

出现 `needs_mapping.jsonl` 时，应检查：

- Excel 是否是 `.xlsx` / `.xlsm`，且不是临时文件 `~$...`。
- 表头中是否有旧清洗脚本能识别的产品字段，例如产品名称、品牌、规格、包装、酒质、卖点等。
- 是否存在合并单元格、跨行表头、空表头或多个非产品 sheet。
- 是否需要先补充字段映射，再进入正式 embedding。

产品 Excel 新增推荐流程：

```bash
python3 scripts/ingest_content.py --source 素材/新文件.xlsx --dry-run --type product_excel
python3 scripts/ingest_content.py --source 素材/新文件.xlsx --execute --type product_excel
sed -n '1,120p' output/ingest_build/product_excel/build_report.md
sed -n '1,20p' output/ingest_build/product_excel/needs_mapping.jsonl
python3 scripts/ingest_content.py --check-build
python3 scripts/ingest_content.py --status
```

如果 `output/ingest_build/product_excel/documents.json` 中有 ready documents，下一阶段才进入 embedding。如果只有 `needs_mapping.jsonl`，先处理字段映射问题。

显式指定单个 Excel 文件时，即使文件不在 `素材/` 或 `产品信息/` 下，dry-run 也会按扩展名识别为 `product_excel` 候选；默认目录扫描仍只递归扫描 `素材/`、`产品信息/`、`质检报告/` 这类安全目录，避免误扫未知大目录。建议正式流程仍把文件放入受管理目录，便于后续 source 路径、manifest 和审计记录保持稳定。

### quality_report 构建产物

当前真实质检报告链路是：

```text
scripts/build_quality_report_docs.py
-> output/quality_report_documents.json
-> output/ingest_build/quality_report/documents.json
```

`quality_report/documents.json` 也是 JSON list，每条为：

```json
{
  "page_content": "用于后续 embedding 的 PDF 文本块",
  "metadata": {
    "doc_id": "supp__...__c001",
    "product_id": "...",
    "product_name": "...",
    "doc_type": "quality_report",
    "field_name": "quality_report",
    "source_file": "质检报告/xxx.pdf",
    "source_kind": "pdf_text",
    "source_doc_id": "pdf__...",
    "source_sha1": "...",
    "chunk_index": 1,
    "chunk_total": 3,
    "page_start": 1,
    "page_end": 2
  }
}
```

### 后续 embedding 使用的字段

后续 `scripts/embed_documents.py` 读取的输入格式是 JSON list，并使用：

- `page_content`：送入 embedding 模型。
- `metadata.doc_id`：作为文档 ID。
- `metadata.product_id`：产品聚合和检索结果归属。
- `metadata.doc_type`、`metadata.field_name`：写入向量库 metadata 字段。
- `metadata.source_doc_id`、`metadata.source_sha1`、`metadata.source_file`：用于重复检测、更新、删除和溯源。

必须尽量稳定的字段：

- `doc_id`
- `product_id`
- `source_doc_id`
- `source_sha1`
- `source_file`
- `chunk_index`

最终待 embedding 产物：

- `output/ingest_build/product_excel/documents.json`
- `output/ingest_build/quality_report/documents.json`

中间/辅助产物：

- `output/products_cleaned.json`
- `output/products_enriched.json`
- `output/product_excel_pipeline/output/products_cleaned.json`
- `output/product_excel_pipeline/output/image_mapping.json`
- `output/product_excel_pipeline/output/products_enriched.json`
- `output/product_excel_pipeline/output/documents_preview_v2.json`
- `output/quality_report_documents.json`
- `output/ingest_build/product_excel/needs_mapping.jsonl`
- 各类 `*_report.md`

## 第 3.5 阶段推荐流程

### 产品 Excel

第一步：只读预检，不执行本地构建。

```bash
python3 scripts/ingest_content.py --source 素材/新文件.xlsx --dry-run --type product_excel
```

第二步：检查计划和 manifest 判断。

```bash
sed -n '1,20p' output/ingest_plan.jsonl
```

重点看 `suggested_action`：

- `new`：可以处理。
- `unchanged`：内容未变，默认 execute 会跳过。
- `changed`：同一 source 出现新 sha1，需要确认是否要重新构建。
- `duplicate_content`：不同路径内容相同，确认是否重复文件。

第三步：执行本地 Excel pipeline。

```bash
python3 scripts/ingest_content.py --source 素材/新文件.xlsx --execute --type product_excel
```

如果已经确认无需交互：

```bash
python3 scripts/ingest_content.py --source 素材/新文件.xlsx --execute --type product_excel --yes
```

第四步：查看 pipeline 报告和标准产物。

```bash
sed -n '1,160p' output/ingest_build/product_excel/build_report.md
python3 scripts/ingest_content.py --check-build
```

第五步：查看 manifest 执行记录。

```bash
python3 scripts/ingest_content.py --status
```

### 质检报告 PDF

```bash
python3 scripts/ingest_content.py --source 质检报告/某报告.pdf --dry-run --type quality_report
python3 scripts/ingest_content.py --source 质检报告/某报告.pdf --execute --type quality_report
python3 scripts/ingest_content.py --check-build
python3 scripts/ingest_content.py --status
```

确认本地标准产物无误后，下一阶段才进入 embedding 和 OpenSearch push。

## 第四阶段：本地 embedding

第四阶段新增：

```bash
scripts/embed_ingest_build.py
```

它只读取标准本地构建产物，生成本地 embedding JSONL 文件，不写 OpenSearch。

输入文件：

```text
output/ingest_build/product_excel/documents.json
output/ingest_build/quality_report/documents.json
```

输出文件：

```text
output/ingest_embeddings/product_excel/embeddings.jsonl
output/ingest_embeddings/quality_report/embeddings.jsonl
output/ingest_embeddings/embedding_report.md
```

每条 embedding JSONL 记录包含：

```json
{
  "doc_id": "...",
  "source_type": "product_excel",
  "source_doc_id": "...",
  "source_sha1": "...",
  "product_id": "...",
  "content_sha1": "...",
  "embedding_model": "text-embedding-v4",
  "embedding_dim": 1024,
  "text": "实际送入 embedding 的文本",
  "page_content": "同 text，兼容旧 push 脚本",
  "doc_type": "...",
  "field_name": "...",
  "metadata": {},
  "embedding": []
}
```

安全规则：

- 默认不调用 embedding API。没有 `--execute` 时只做 dry-run。
- embedding 会调用外部模型，可能产生费用。
- `--execute` 前会打印输入数量、已有 embedding 数、跳过数量、将生成数量、模型和输出路径。
- 没有 `--yes` 时，必须手动输入 `yes` 才会调用 API。
- 如果缺少 `DASHSCOPE_API_KEY`，会在 execute 前友好报错。
- 生成时采用追加写入；失败时不会删除已有成功结果，方便断点续跑。

重复检测规则：

- 读取已有 `embeddings.jsonl`。
- 用 `doc_id + content_sha1 + embedding_model` 判断是否已经生成。
- 默认跳过未变化的记录。
- 如果同一个 `doc_id + embedding_model` 出现新的 `content_sha1`，dry-run 会提示 changed content，并在 execute 时重新生成。
- `--force` 会强制重新生成，即使已有相同记录；这会在本地 JSONL 中留下重复记录，后续 `--status` 会提示。

本地 embedding 推荐流程：

第一步：源文件预检。

```bash
python3 scripts/ingest_content.py --source 素材/新文件.xlsx --dry-run --type product_excel
```

第二步：本地构建。

```bash
python3 scripts/ingest_content.py --source 素材/新文件.xlsx --execute --type product_excel
```

第三步：检查构建产物。

```bash
python3 scripts/ingest_content.py --check-build
```

第四步：embedding dry-run。

```bash
python3 scripts/embed_ingest_build.py --source-type product_excel --dry-run
```

只测试前 3 条：

```bash
python3 scripts/embed_ingest_build.py --source-type product_excel --dry-run --limit 3
python3 scripts/embed_ingest_build.py --source-type product_excel --execute --limit 3
```

第五步：确认后生成本地 embedding。

```bash
python3 scripts/embed_ingest_build.py --source-type product_excel --execute
```

自动确认版：

```bash
python3 scripts/embed_ingest_build.py --source-type product_excel --execute --yes
```

第六步：检查 embedding 结果。

```bash
python3 scripts/embed_ingest_build.py --status
```

或只看某类：

```bash
python3 scripts/embed_ingest_build.py --source-type quality_report --status
```

第七步：下一阶段才考虑 OpenSearch push。当前本地 embedding 文件保留了旧 push 脚本需要的兼容字段：

- `doc_id`
- `product_id`
- `doc_type`
- `field_name`
- `page_content`
- `metadata`
- `embedding`

但本阶段不会自动调用任何 `push_*_to_opensearch.py`。

## 第五阶段：安全 push OpenSearch

第五阶段新增：

```bash
scripts/push_ingest_embeddings.py
```

它读取第四阶段生成的本地 embedding 文件，并写入现有 OpenSearch `text_docs` 表。默认只 dry-run，只有显式传 `--execute` 才会真正写 OpenSearch。

输入文件：

```text
output/ingest_embeddings/product_excel/embeddings.jsonl
output/ingest_embeddings/quality_report/embeddings.jsonl
```

目标表：

```text
<OPENSEARCH_INSTANCE_ID>_text_docs
```

主要写入字段：

- `id` / `doc_id`
- `product_id`
- `doc_type`
- `field_name`
- `source_kind`
- `source_file`
- `source_doc_id`
- `source_sha1`
- `source_url`
- `source_text`
- `source_text_vector`
- `source_type`
- `content_sha1`
- `embedding_model`
- `embedding_dim`
- `metadata`

安全规则：

- 默认不会写 OpenSearch。
- 默认不会删除任何 OpenSearch 数据。
- 默认会检查已存在的 `doc_id`，已存在则跳过。
- `--force` 会允许已存在 `doc_id` 走 upsert/add，可能覆盖或改变线上召回结果，使用前必须确认。
- 单条写入失败会记录失败并继续处理后续记录。
- push 后会生成 `push_report.md`，并向 `output/ingest_manifest.jsonl` 追加 `event_type=push` 的事件记录。

push dry-run：

```bash
python3 scripts/push_ingest_embeddings.py --source-type product_excel --dry-run
```

检查 OpenSearch 连接和 mapping：

```bash
python3 scripts/push_ingest_embeddings.py --check-opensearch
```

只测试前 3 条：

```bash
python3 scripts/push_ingest_embeddings.py --source-type product_excel --dry-run --limit 3
python3 scripts/push_ingest_embeddings.py --source-type product_excel --execute --limit 3
```

正式 push：

```bash
python3 scripts/push_ingest_embeddings.py --source-type product_excel --execute
```

自动确认版：

```bash
python3 scripts/push_ingest_embeddings.py --source-type product_excel --execute --yes
```

查看报告：

```bash
sed -n '1,200p' output/ingest_push/push_report.md
cat output/ingest_push/push_report.json
```

查看 manifest push 事件：

```bash
python3 scripts/ingest_content.py --status
```

回查 OpenSearch 中是否存在目标 `doc_id`，当前建议优先使用只读检索/API 或后续专门的只读检查脚本。不要为了确认误写而直接运行删除脚本。

第 5.5 阶段新增 push 后只读验证能力：

```bash
python3 scripts/push_ingest_embeddings.py --verify-pushed
```

它会读取最近的：

```text
output/ingest_push/push_report.json
```

取其中 `pushed_doc_ids`，只读 fetch OpenSearch，验证这些 doc 是否能查回，并检查：

- `doc_id`
- `source_text` / `text` / `page_content`
- `source_type`
- `source_doc_id`
- `source_sha1`
- `product_id`
- `content_sha1`
- `embedding_model`
- `embedding_dim`
- `source_text_vector`
- `metadata`

验证输出：

```text
output/ingest_push/verify_report.md
output/ingest_push/verify_report.json
```

只验证指定 doc_id：

```bash
python3 scripts/push_ingest_embeddings.py --verify-doc-id <doc_id>
python3 scripts/push_ingest_embeddings.py --verify-doc-id <doc_id_1> --verify-doc-id <doc_id_2>
```

限制验证数量：

```bash
python3 scripts/push_ingest_embeddings.py --verify-pushed --verify-limit 3
```

从指定 JSON 报告读取 doc_id：

```bash
python3 scripts/push_ingest_embeddings.py --verify-pushed --verify-from-report output/ingest_push/push_report.json
```

第五阶段完整推荐流程：

```bash
python3 scripts/ingest_content.py --source 素材/新文件.xlsx --dry-run --type product_excel
python3 scripts/ingest_content.py --source 素材/新文件.xlsx --execute --type product_excel
python3 scripts/ingest_content.py --check-build
python3 scripts/embed_ingest_build.py --source-type product_excel --dry-run
python3 scripts/embed_ingest_build.py --source-type product_excel --execute
python3 scripts/embed_ingest_build.py --source-type product_excel --status
python3 scripts/push_ingest_embeddings.py --source-type product_excel --dry-run
python3 scripts/push_ingest_embeddings.py --check-opensearch
python3 scripts/push_ingest_embeddings.py --source-type product_excel --execute --limit 3
sed -n '1,200p' output/ingest_push/push_report.md
python3 scripts/push_ingest_embeddings.py --verify-pushed
sed -n '1,200p' output/ingest_push/verify_report.md
```

确认前 3 条 push 和 verify 都正常后，再扩大 push 范围。push 完成后再验证问答效果：

```bash
python3 test_rag_api.py
python3 scripts/interactive_answer_test.py "新增产品相关问题"
```

## 第 5.6 阶段：只读检索验证

第 5.6 阶段新增：

```bash
scripts/verify_retrieval.py
src/rag_app/retrieval_verify.py
```

它只读验证已 push 的文档是否能通过现有检索链路搜到，不写 OpenSearch，不删除数据，不改 API，也不改检索逻辑。

默认从最近的 push JSON 报告读取待验证 doc_id：

```text
output/ingest_push/push_report.json
```

dry-run：

```bash
python3 scripts/verify_retrieval.py --from-push-report output/ingest_push/push_report.json --dry-run
```

执行只读检索验证：

```bash
python3 scripts/verify_retrieval.py --from-push-report output/ingest_push/push_report.json --execute --verify-limit 3
```

验证指定 doc_id：

```bash
python3 scripts/verify_retrieval.py --doc-id <doc_id> --execute
```

人工 query 验证：

```bash
python3 scripts/verify_retrieval.py --query "某个产品名称或质检关键词" --execute --top-k 5
```

输出报告：

```text
output/ingest_push/retrieval_verify_report.md
output/ingest_push/retrieval_verify_report.json
```

报告会包含：

- 验证模式：dry-run / execute
- 验证 doc_id 数量
- query 数量
- top_k
- 命中数量和 miss 数量
- 每个 doc_id 的 `auto_query`、hit/miss、rank、top_k doc_id、top_k score
- 每个人工 query 的 top-k 结果、doc_id、score、source_type、product_id、source_doc_id、text 摘要
- warnings
- `retrieval_status`

第 5.6 阶段推荐流程：

```bash
python3 scripts/push_ingest_embeddings.py --source-type product_excel --execute --limit 3
python3 scripts/push_ingest_embeddings.py --verify-pushed
python3 scripts/verify_retrieval.py --from-push-report output/ingest_push/push_report.json --dry-run
python3 scripts/verify_retrieval.py --from-push-report output/ingest_push/push_report.json --execute --verify-limit 3
sed -n '1,200p' output/ingest_push/retrieval_verify_report.md
python3 scripts/verify_retrieval.py --query "某个产品名称或质检关键词" --execute --top-k 5
```

确认 push、verify-pushed、verify-retrieval 都正常后，再做人工 API / 问答验证。当前阶段仍不自动调用回答模型做效果评估，避免把检索验证和问答生成混在一起。

如果 push 错了，当前暂时不要盲目删除。删除、更新、回滚会在后续阶段单独设计，避免因为本地文件或 id 范围判断不完整造成误删。

## 第六阶段：删除 / 更新 / 回滚计划

第六阶段新增：

```bash
scripts/plan_ingest_changes.py
```

当前只支持 dry-run 计划，不会真实删除 OpenSearch 数据，不会写 OpenSearch，不会回滚。即使传入 `--execute`，脚本也会直接中止并提示：

```text
execute deletion is not implemented in this stage
```

### inspect-source

查看某个 `source_doc_id` 在 manifest 和可选 OpenSearch 里的状态：

```bash
python3 scripts/plan_ingest_changes.py --mode inspect-source --source-doc-id <source_doc_id>
```

如果要只读确认 OpenSearch 中 doc_id 是否还存在：

```bash
python3 scripts/plan_ingest_changes.py --mode inspect-source --source-doc-id <source_doc_id> --check-opensearch
```

输出会包含：

- manifest 中所有相关记录
- `source_sha1`
- `generated_doc_ids`
- `pushed_doc_ids`
- `push_status`
- `verify_status`
- `retrieval_status`
- OpenSearch 中是否查到目标 doc_id
- 最近更新时间

### delete-source dry-run

生成按 source 删除的计划，但不删除：

```bash
python3 scripts/plan_ingest_changes.py --mode delete-source --source-doc-id <source_doc_id> --dry-run
```

带 OpenSearch 只读检查：

```bash
python3 scripts/plan_ingest_changes.py --mode delete-source --source-doc-id <source_doc_id> --check-opensearch
```

计划会列出：

- 将要删除的 doc_id
- doc_id 来源：manifest `generated_doc_ids` / push event `pushed_doc_ids` / OpenSearch source 查询
- 涉及的 index/table
- `source_type`
- `product_id`
- 风险和 blockers
- `safe_to_execute`

### rollback-push dry-run

根据某次 push 报告生成回滚计划：

```bash
python3 scripts/plan_ingest_changes.py --mode rollback-push --from-push-report output/ingest_push/push_report.json --dry-run
```

这个模式只根据 `push_report.json` 里的 `pushed_doc_ids` 计划回滚范围，并检查这些 doc_id 当前是否还能在 OpenSearch 查到。

### update-source dry-run

用于源文件内容变化后的更新计划：

```bash
python3 scripts/plan_ingest_changes.py --mode update-source --source-doc-id <source_doc_id> --dry-run
```

如果能在 `output/ingest_plan.jsonl` 找到同一 `source_path` 的新 `source_sha1`，会比较旧 sha1 和新 sha1：

- sha1 没变：建议 skip。
- sha1 变了：建议重新本地构建、embedding、push、verify-pushed、verify-retrieval，确认新数据可用后，再单独计划删除旧 doc_id。

### 输出

默认生成：

```text
output/ingest_changes/change_plan.json
output/ingest_changes/change_plan.md
```

JSON 包含：

- `mode`
- `dry_run`
- `source_doc_id`
- `source_sha1`
- `target_doc_ids`
- `target_indexes`
- `source_types`
- `product_ids`
- `opensearch_existing_doc_ids`
- `missing_doc_ids`
- `unknown_doc_ids`
- `risks`
- `blockers`
- `safe_to_execute`
- `recommended_next_steps`

### 为什么不能盲目删除

同一个产品可能有多个 source、多个 doc 分块、多个 push 批次。删除前必须确认：

- `push_report.json`
- `verify_report.json`
- `retrieval_verify_report.json`
- `change_plan.json/md`

还要确认是否有后续更新已经成功、是否有同 product_id 的其他来源、是否存在 OpenSearch 中有但 manifest 没记录的 doc_id。下一阶段才考虑真正执行删除。

## 新增后验证

本地验证：

```bash
python3 scripts/ingest_content.py --source 新增文件路径 --dry-run
```

API 验证：

```bash
python3 test_rag_api.py
```

或：

```bash
python3 scripts/interactive_answer_test.py "新增产品相关问题"
```

OpenSearch push 后应检查对应报告：

- `output/push_text_docs_report*.md`
- `output/push_image_vectors_report*.md`
- `output/push_image_text_docs_report*.md`
- `output/quality_report_manifest.json`
- `output/quality_report_unmatched.csv`

## 失败时先看哪里

- 预检失败：看控制台错误和 `output/ingest_plan.jsonl` 是否生成。
- PDF 匹配失败：看 `output/quality_report_unmatched.csv` 和 `config/quality_report_product_map.csv`。
- embedding 失败：看对应 embedding report，例如 `output/embedding_report_v2.md`、`output/quality_report_embedding_report.md`。
- OpenSearch 推送失败：看对应 `push_*_report*.md`。
- 图片 URL 问题：看 `output/image_url_mapping.json`、`output/image_upload_report.md`。

## 测试

第 4.5 阶段新增了离线 pytest 测试体系。默认测试不会调用 embedding API，不会写 OpenSearch，不依赖线上配置。

每次修改 ingest 相关代码后，建议运行：

```bash
make test
```

或直接运行：

```bash
python3 -m pytest -m "not integration"
```

只跑离线测试：

```bash
make test-offline
```

只跑某个测试文件：

```bash
python3 -m pytest tests/test_embed_ingest_build.py
```

只跑某个测试函数：

```bash
python3 -m pytest tests/test_embed_ingest_build.py::test_dry_run_does_not_call_embedding_api
```

语法级检查：

```bash
make lint
```

当前覆盖范围：

- `tests/test_plan_ingest.py`：源文件扫描、稳定 `source_sha1/source_doc_id`、必需字段、dry-run 不触发外部调用。
- `tests/test_ingest_manifest.py`：manifest 追加、读取、查找、摘要、重复 sha1、变更 source_doc_id。
- `tests/test_ingest_content.py`：dry-run 默认不 execute、确认中止、manifest skip/force、status、check-build。
- `tests/test_build_product_excel_docs.py`：`documents_preview_v2.json` 优先、`products_enriched.json` fallback、`needs_mapping.jsonl`。
- `tests/test_build_product_excel_pipeline.py`：pipeline dry-run、默认跳过图片 AI 分析、复用已有 image analysis、失败报告、成功产物。
- `tests/test_embed_ingest_build.py`：embedding dry-run、limit、重复跳过、内容变更、空文本 warning、status。
- `tests/test_ingest_e2e_offline.py`：最小离线闭环，从 plan 到 manifest 判断、标准文档、embedding dry-run/status。

每次接入新 Excel/PDF 前，仍建议先跑业务 dry-run：

```bash
python3 scripts/ingest_content.py --source 素材/新文件.xlsx --dry-run --type product_excel
python3 scripts/ingest_content.py --source 质检报告/某报告.pdf --dry-run --type quality_report
```

不会产生费用的测试：

- 默认 `make test`
- `make test-offline`
- `python3 -m pytest -m "not integration"`
- `scripts/ingest_content.py --dry-run`
- `scripts/embed_ingest_build.py --dry-run`

可能调用外部 API 或依赖 OpenSearch 的测试，后续必须标记为 `integration`，默认不跑。OpenSearch 写入测试会在后续阶段单独设计，不混入当前离线单元测试。

## 当前阶段不做的事

当前阶段只标准化“新增内容之前先知道会发生什么”和“源文件到本地标准文档”的链路，不修改：

- OpenSearch 写入逻辑
- embedding 模型
- API 服务
- 检索和 rerank 逻辑
- 现有 loader/chunker/vector_store 结构
