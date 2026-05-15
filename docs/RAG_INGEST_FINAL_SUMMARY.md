# RAG 数据导入最终总览

更新时间：2026-05-14

## 1. 当前项目数据状态

本项目当前已经完成产品 Excel 的安全补充入库和新产品入库流程。核心链路仍然保持分阶段执行：

1. 本地构建 documents
2. 生成 embeddings
3. push dry-run
4. push execute
5. verify pushed
6. product bundle 只读预览

生产写入目标为 `text_docs`，向量字段为 `source_text_vector`，mapping 维度为 `1024`。

## 2. 已入库内容

### 大民族与石荣霄 confirmed supplement

- 大民族 supplement：113 条
- 石荣霄 supplement：873 条

### 剩余 49 个产品最终分流

- supplement：25 个产品，187 条 supplement documents
- new_product：24 个产品，120 条 new product documents

## 3. 本轮写入 text_docs 总量

- supplement documents 总数：1173 条
  - 大民族 confirmed supplement：113 条
  - 石荣霄 confirmed supplement：873 条
  - 49 个复查后 supplement：187 条
- new product documents 总数：120 条

## 4. 新产品 ID 与 registry

新产品使用 `config/product_catalog_registry.csv` 的稳定 `prd_*` 编号。

- 新增 product_id 数量：24
- 新 product_id 范围：`prd_000287` 到 `prd_000334`
- 实际 ID 清单来源：`output/review/product_catalog_registry_new_24.csv`
- 已同步到：`config/product_catalog_registry.csv`
- 同步前备份：`config/product_catalog_registry.csv.bak_before_new_24`
- 同步报告：`output/final/product_catalog_registry_sync.md`

## 5. 未处理或需后续关注的数据

- 未确认产品：已从本轮入库范围排除；后续必须重新确认后再进入 embedding / push。
- 无质检报告产品：product bundle 可以正常返回产品资料，但 `quality_reports` 可能为空，需要后续补充质检报告源文件。
- 已入库 supplement 不覆盖旧资料；如与旧资料存在差异，应通过 product bundle 的 `conflicts` 字段提示。

## 6. 当前保留的核心产物

必须保留：

- `output/ingest_build/`
- `output/ingest_embeddings/`
- `output/ingest_manifest.jsonl`
- `config/product_identity_map.csv`
- `config/product_identity_map_srx.csv`
- `config/product_catalog_registry.csv`
- `config/product_catalog_registry.csv.bak_before_new_24`
- `docs/RAG_INGEST_FINAL_SUMMARY.md`

核心脚本保留：

- `scripts/ingest_content.py`
- `scripts/build_product_excel_pipeline.py`
- `scripts/build_product_excel_docs.py`
- `scripts/build_supplement_documents_from_mapping.py`
- `scripts/build_new_product_documents_from_review.py`
- `scripts/embed_ingest_build.py`
- `scripts/push_ingest_embeddings.py`
- `scripts/plan_product_identity_mapping.py`
- `scripts/preview_product_bundle.py`
- `scripts/plan_cleanup_outputs.py`

## 7. 当前 API 能力

当前 API 已接入 `product_bundles` 字段，并保持旧 `doc_hits` 兼容。

product bundle 支持返回：

- `product_full`
- `product_field` / `basic_info`
- `packaging`
- `selling_points`
- `gift_attributes`
- `quality_report`
- `image_text`
- `supplement`
- `conflicts`

回答上下文优先使用 product bundle 摘要，并保留 quality report 和 supplement 摘要。如果 supplement 与旧资料存在差异，应在 `conflicts` 中提示，不直接覆盖旧字段。

## 8. 后续新增文件推荐流程

新增 Excel / PDF 时建议继续按安全链路执行：

1. 画像：只读分析文件结构
2. dry-run：生成 ingest plan
3. 本地构建：生成 documents
4. 产品身份映射：只读匹配线上已有产品
5. 人工确认：confirmed / needs_review / new_product / ignore
6. 生成 supplement 或 new product documents
7. embedding dry-run
8. embedding execute
9. push dry-run
10. OpenSearch 安全门检查
11. push execute
12. verify pushed
13. product bundle 预览

默认不要全量重跑，不要使用 `--force`，不要覆盖旧 doc_id。

## 9. 回滚与追溯入口

如需追溯或回滚，应优先查看：

- `output/ingest_manifest.jsonl`
- `output/ingest_push/*push_report*.json`
- `output/ingest_push/*verify_report*.json`
- `output/ingest_build/**/documents.json`
- `output/ingest_embeddings/**/embeddings.jsonl`

删除必须重新生成 change plan，并通过 `scripts/apply_ingest_change_plan.py` 的安全删除执行器执行。不要按 `source_doc_id` 或 query 直接批量删除。

## 10. 清理归档说明

清理阶段会把大量验收报告、review 报告、push/verify report 和临时流水线产物归档到：

- `archive/cleanup_<timestamp>/`

核心产物不归档、不删除。缓存类文件可以删除：

- `__pycache__/`
- `.pytest_cache/`

清理完成后请查看：

- `output/cleanup/final_cleanup_plan.md`
- `output/cleanup/final_cleanup_archive_report.md`
- `output/final/cleanup_done.md`
