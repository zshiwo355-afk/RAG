# 当前交付版接口说明

## 1. 适用范围

这份文档用于说明**当前可部署版本**的 RAG API，对外重点回答三件事：

- 现在服务能提供哪些接口
- 返回结构和之前相比有哪些变化
- 前端是否需要立刻改代码

这不是历史设计文档，也不是离线构建文档，只面向当前上线包。

## 1.1 当前服务地址

当前服务器地址：

```text
http://121.43.112.129:3001
```

下文如果没有特别说明，默认都基于这个地址调用。

## 2. 当前可用接口

### 健康检查

```http
GET http://121.43.112.129:3001/api/rag/health
```

用途：

- 检查服务是否启动
- 检查 OpenSearch 连接配置是否齐全
- 检查图片 URL 映射是否就绪

### 搜索接口

```http
POST http://121.43.112.129:3001/api/rag/search
Content-Type: application/json
```

请求体：

```json
{
  "query": "匠王洞酒 藏10號 质检报告",
  "top_k": 10
}
```

用途：

- 只做检索和排序
- 不调用回答大模型
- 适合搜索列表页、调试页、候选产品页

### 问答接口

```http
POST http://121.43.112.129:3001/api/rag/answer
Content-Type: application/json
```

请求体：

```json
{
  "query": "适合送礼的高端酱酒有哪些",
  "top_k": 5,
  "debug": false
}
```

用途：

- 先检索，再精排，再调用回答模型
- 返回自然语言回答和产品结构化结果

### 预留接口

```http
POST http://121.43.112.129:3001/api/rag/search-by-image
```

当前状态：

- 已保留路由
- 仍未实现

## 3. 当前返回能力

### 3.1 产品主文本

当前线上已经重新补回：

- `product_full`
- `product_field`

因此现在查询产品名时，已经可以命中产品主文本，而不只是 PDF 和图片链路。

### 3.2 图片回源

当前图片回源已经正式可用：

- 前端主用：`best_image_url`
- 补充列表：`images[]`
- 排查字段：`best_image_path`、`images[].image_path`

图片本体展示应优先使用：

- `products[].best_image_url`
- `products[].images[].image_url`

### 3.3 PDF 回源

当前已匹配成功并入库的 PDF，已经可以返回：

- `best_pdf_hit`
- `pdf_reports[]`
- `pdf_url`
- `pdf_path`
- `page_start`
- `page_end`
- `supplement_title`

前端打开 PDF 时应优先使用：

- `products[].best_pdf_hit.pdf_url`
- `products[].pdf_reports[].pdf_url`

排查时保留：

- `pdf_path`
- `source_file`

## 4. 与旧接口相比的变化

## 4.1 没有变化的部分

以下内容没有变化：

- 路由路径没有变化
- 请求方法没有变化
- 请求体字段没有变化

保持不变的接口：

- `GET /api/rag/health`
- `POST /api/rag/search`
- `POST /api/rag/answer`
- `POST /api/rag/search-by-image`

保持不变的请求参数：

- `/api/rag/search`
  - `query`
  - `top_k`
- `/api/rag/answer`
  - `query`
  - `top_k`
  - `debug`

因此不存在请求入参层面的 breaking change。

### 4.2 新增的返回字段

#### `/api/rag/search`

`products[]` 中新增/强化了这些字段：

- `best_image_url`
- `images[]`
- `best_pdf_hit`
- `pdf_reports[]`

其中 PDF 相关关键字段包括：

- `best_pdf_hit.pdf_url`
- `best_pdf_hit.pdf_path`
- `best_pdf_hit.page_start`
- `best_pdf_hit.page_end`
- `best_pdf_hit.supplement_title`
- `pdf_reports[].pdf_url`
- `pdf_reports[].pdf_path`
- `pdf_reports[].page_start`
- `pdf_reports[].page_end`
- `pdf_reports[].supplement_title`

图片相关关键字段包括：

- `best_image_url`
- `best_image_path`
- `images[].image_url`
- `images[].image_path`
- `images[].source_doc_id`
- `images[].source_file`

命中详情 metadata 中也新增了回源字段：

- `source_kind`
- `source_file`
- `source_doc_id`
- `source_sha1`
- `source_url`
- `chunk_index`
- `page_start`
- `page_end`
- `supplement_title`

#### `/api/rag/answer`

`products[]` 中也新增/强化了：

- `best_image_url`
- `best_image_path`
- `images[]`
- `best_pdf_hit`
- `pdf_reports[]`

因此 `answer` 接口同样具备图片/PDF 本体回源能力。

## 5. 是否兼容旧前端

结论：**兼容**。

原因：

- 没有删除旧字段
- 没有修改原请求参数
- 新字段都是附加字段

如果旧前端不读取新增字段：

- 原有调用仍能正常工作
- 只是不会展示图片本体和 PDF 本体

## 6. 如果前端要最小适配，改哪里

如果前端要用上当前增强能力，最小改动建议如下。

### 图片

展示主图优先读：

- `products[].best_image_url`

如果要做图片列表，再读：

- `products[].images[]`

### PDF

展示“查看质检报告”按钮优先读：

- `products[].best_pdf_hit.pdf_url`

如果要做“多个报告”列表，再读：

- `products[].pdf_reports[]`

建议展示字段：

- `pdf_url`
- `pdf_path`
- `page_start`
- `page_end`
- `supplement_title`

## 7. 当前数据边界

当前线上可用的数据范围：

- 产品主文本：已上线
- 图片回源：已上线
- 已匹配成功的 quality_report：已上线且已补 OSS URL

当前仍未完全覆盖的部分：

- 仍有一批 unmatched PDF 未进入 quality_report 链路
- 这部分不是接口问题，而是数据绑定问题

因此当前接口是可部署、可使用的，但并不代表所有 PDF 都已被纳入知识库。

## 8. 当前交付结论

当前这版服务已经满足：

- 可部署
- 可检索
- 可返回产品主文本
- 可返回图片本体 URL
- 可返回 PDF 本体 URL 和页码

对外可以按“当前交付版接口”使用，不需要前端重写，只需在需要时增量读取新增字段。
