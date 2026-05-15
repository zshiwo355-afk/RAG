# 接口变化说明

## 1. 结论

本次代码相较于之前的 API 形态：

- **路由路径无变化**
- **请求参数无变化**
- **主要是返回字段增加**
- **新增字段为向后兼容的附加字段**
- **旧前端如果忽略未知字段，仍可正常工作**

## 2. 无变化的接口

以下接口路径没有变化：

- `GET /api/rag/health`
- `POST /api/rag/answer`
- `POST /api/rag/search`
- `POST /api/rag/search-by-image`

请求参数也没有变化：

- `/api/rag/answer`
  - `query`
  - `top_k`
  - `debug`
- `/api/rag/search`
  - `query`
  - `top_k`

因此不存在“前端请求入参不兼容”的 breaking change。

## 3. 有返回字段新增的接口

### 3.1 `POST /api/rag/search`

之前已存在的核心字段仍保留：

- `ok`
- `query`
- `route_counts`
- `retrieval_count`
- `result_count`
- `products[]`
- `products[].product_id`
- `products[].final_score`
- `products[].rrf_score`
- `products[].rerank_score`
- `products[].rerank_mode`
- `products[].routes`
- `products[].best_text`
- `products[].best_image_path`
- `products[].best_image_url`
- `products[].doc_hits`
- `products[].image_hits`

本次新增的返回字段：

- `products[].best_pdf_hit`
- `products[].images[]`
- `products[].pdf_reports[]`

以及命中详情里补充的 metadata 字段：

- `metadata.source_kind`
- `metadata.source_file`
- `metadata.source_doc_id`
- `metadata.source_sha1`
- `metadata.source_url`
- `metadata.chunk_index`
- `metadata.page_start`
- `metadata.page_end`
- `metadata.supplement_title`

新增的 PDF / 图片回源字段：

- `best_pdf_hit.pdf_url`
- `best_pdf_hit.pdf_path`
- `best_pdf_hit.page_start`
- `best_pdf_hit.page_end`
- `best_pdf_hit.supplement_title`
- `images[].image_url`
- `images[].image_path`
- `images[].source_doc_id`
- `images[].source_file`
- `images[].source_url`
- `pdf_reports[].pdf_url`
- `pdf_reports[].pdf_path`
- `pdf_reports[].page_start`
- `pdf_reports[].page_end`
- `pdf_reports[].supplement_title`

### 3.2 `POST /api/rag/answer`

之前已存在并继续兼容：

- `ok`
- `query`
- `answer`
- `products[]`
- `debug`

`products[]` 中已存在并继续兼容：

- `product_id`
- `product_name`
- `reason`
- `best_image_url`
- `score`
- `routes`
- `sources`

本次新增：

- `products[].best_image_path`
- `products[].best_pdf_hit`
- `products[].images[]`
- `products[].pdf_reports[]`

也就是说，`answer` 接口也具备了图片/PDF 本体回源能力。

## 4. 图片 / PDF 回源相关变化

### 图片

当前前端主用字段已经明确为：

- `best_image_url`
- `images[].image_url`

兼容保留排查字段：

- `best_image_path`
- `images[].image_path`

### PDF

当前新增并可返回：

- `best_pdf_hit`
- `pdf_reports[]`

关键字段包括：

- `pdf_url`
- `pdf_path`
- `page_start`
- `page_end`
- `supplement_title`
- `source_doc_id`
- `source_file`
- `source_url`

## 5. 是否兼容旧前端

结论：**兼容**。

原因：

- 没有删旧字段
- 没有改路由
- 没有改请求体结构
- 新增字段都只是附加在原返回对象上

如果旧前端不读取这些新字段：

- 原有调用仍能正常工作
- 只是不具备“展示图片本体 / 打开 PDF 本体”的增强能力

## 6. 是否需要前端同步修改

如果前端只维持原有功能：

- **不需要强制修改**

如果前端要用上图片/PDF 回源增强能力：

- **建议做最小同步修改**

最小修改点：

- 图片展示优先读：
  - `products[].best_image_url`
  - `products[].images[]`
- PDF 展示/打开优先读：
  - `products[].best_pdf_hit.pdf_url`
  - `products[].pdf_reports[]`

## 7. breaking change 判断

当前没有发现会影响旧前端调用的 breaking change。

唯一需要注意的是：

- 如果旧前端写死了返回字段白名单并严格校验完整对象 schema，可能需要放宽对“多余字段”的限制

但就普通 JSON 解析和渲染场景来说，这次变更属于**纯增量兼容扩展**。
