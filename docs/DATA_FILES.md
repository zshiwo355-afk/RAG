# 数据文件说明

## output/products_enriched.json

- 当前数量：285 条产品记录。
- 来源：Excel 清洗、图片抽取、产品图片合并后的产物。
- 用途：运行时按 `product_id` 补齐产品图片；也是产品元数据来源之一。
- 运行时必须：是。
- 是否可以删除：不可以。

## output/documents_embedded_v2.jsonl

- 当前数量：2135 条文本向量记录。
- 来源：`scripts/build_documents.py` 和 `scripts/embed_documents.py`。
- 用途：导入 OpenSearch `text_docs` 表的主文本向量数据。
- 运行时必须：OpenSearch 已导入后，服务查询不直接读取；但这是可恢复入库的重要数据。
- 是否可以删除：不建议删除。

## output/images_embedded.jsonl

- 当前数量：255 条图片本体向量记录。
- 来源：`scripts/embed_images.py`。
- 用途：导入 OpenSearch `image_vectors` 表；也可用于图搜图测试。
- 运行时必须：OpenSearch 已导入后，文本问答不直接读取；但这是可恢复入库的重要数据。
- 是否可以删除：不建议删除。

## output/image_text_embedded.jsonl

- 当前数量：300 条图片语义文本向量记录。
- 来源：`scripts/build_image_text_docs.py` 和 `scripts/embed_image_text_docs.py`。
- 用途：导入 OpenSearch `image_text_docs` 表，支持文搜图。
- 运行时必须：OpenSearch 已导入后，服务查询不直接读取；但这是可恢复入库的重要数据。
- 是否可以删除：不建议删除。

## output/image_text_docs.jsonl

- 当前数量：300 条图片语义文本记录。
- 来源：`output/image_analysis.jsonl`。
- 用途：图片 caption、OCR、视觉标签拼成 `source_text`，用于离线向量化。
- 运行时必须：不是线上查询必读，但重建图片语义向量时需要。
- 是否可以删除：不建议删除。

## output/image_url_mapping.json

- 当前数量：312 条图片 URL 映射。
- 来源：`scripts/upload_images_to_oss.py`。
- 用途：运行时把 `best_image_path` 转成 OSS 可访问 URL。
- 运行时必须：是。
- 是否可以删除：不可以。

## output/image_url_mapping.csv

- 当前数量：312 条图片 URL 映射的表格版本。
- 来源：`scripts/upload_images_to_oss.py`。
- 用途：人工检查图片路径和 OSS URL。
- 运行时必须：不是，运行时读取 JSON。
- 是否可以删除：不建议删除，除非确认不需要人工核对。

## 其他保留文件

- `output/products_cleaned.json`、`output/products_cleaned.csv`：清洗后的中间产品数据。
- `output/image_analysis.jsonl`：图片分析结果，包含 caption、OCR、visual tags。
- `output/image_mapping.json`：本地图片抽取映射。
- `output/column_mapping.json`：字段清洗映射。
- `output/images/`：本地图片原文件，OSS 上传和重建映射时需要。

