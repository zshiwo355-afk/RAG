# RAG API 接口文档

## 1. 基础信息

服务部署完成后，由部署同事提供实际访问地址。

示例：

```text
http://服务器IP:8000
```

或：

```text
https://rag.example.com
```

下文统一用：

```text
{BASE_URL}
```

表示实际服务地址。

## 2. 健康检查

用于确认服务是否启动、环境变量是否配置、图片 URL 映射文件是否存在。

### 请求

```http
GET {BASE_URL}/api/rag/health
```

### curl 示例

```bash
curl {BASE_URL}/api/rag/health
```

### 成功返回示例

```json
{
  "ok": true,
  "service": "rag",
  "retrieval_ready": true,
  "rerank_ready": true,
  "answer_ready": true,
  "image_mapping_ready": true,
  "image_mapping_count": 312,
  "missing_env": []
}
```

### 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | boolean | 服务是否基本可用 |
| `service` | string | 服务名称 |
| `retrieval_ready` | boolean | OpenSearch 检索配置是否齐全 |
| `rerank_ready` | boolean | rerank 所需配置是否齐全 |
| `answer_ready` | boolean | 回答模型所需配置是否齐全 |
| `image_mapping_ready` | boolean | 图片 URL 映射文件是否可用 |
| `image_mapping_count` | number | 图片 URL 映射数量 |
| `missing_env` | array | 缺失的环境变量名 |

## 3. RAG 问答接口

这是主要接口。调用方传入用户问题，我们返回自然语言回答、推荐产品、图片 URL 和来源。

### 请求

```http
POST {BASE_URL}/api/rag/answer
Content-Type: application/json
```

### 请求参数

```json
{
  "query": "红色礼盒春节送礼推荐哪些酒",
  "top_k": 5,
  "debug": false
}
```

### 请求字段说明

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `query` | string | 是 | 用户问题，不能为空 |
| `top_k` | number | 否 | 返回产品数量，建议传 `5`，最大按服务限制返回 |
| `debug` | boolean | 否 | 是否返回调试信息；正式业务建议传 `false` |

### curl 示例

```bash
curl -X POST {BASE_URL}/api/rag/answer \
  -H "Content-Type: application/json" \
  -d '{"query":"红色礼盒春节送礼推荐哪些酒","top_k":5,"debug":false}'
```

### 成功返回示例

```json
{
  "ok": true,
  "query": "红色礼盒春节送礼推荐哪些酒",
  "answer": "针对红色礼盒春节送礼需求，推荐以下几款产品...",
  "products": [
    {
      "product_id": "prod_0200_石荣霄_r110",
      "product_name": "石荣霄酒·大师手作 臻品迎春",
      "reason": "中国红主色调搭配帝王金，符合春节喜庆审美，适合节庆送礼。",
      "best_image_url": "https://example-bucket.oss-cn-beijing.aliyuncs.com/rag/images/demo.png",
      "score": 0.1943615539,
      "routes": ["image_text", "text_dense", "text_keyword"],
      "sources": [
        {
          "type": "text",
          "route": "text_dense",
          "content": "产品资料片段...",
          "image_url": ""
        },
        {
          "type": "image_text",
          "route": "image_text",
          "content": "图片语义资料片段...",
          "image_url": "https://example-bucket.oss-cn-beijing.aliyuncs.com/rag/images/demo.png"
        }
      ]
    }
  ],
  "debug": {}
}
```

### 返回字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | boolean | 请求是否成功 |
| `query` | string | 原始问题 |
| `answer` | string | 可直接展示给用户的自然语言回答 |
| `products` | array | 推荐产品列表 |
| `debug` | object | 调试信息；`debug=false` 时为空对象 |

### products 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `product_id` | string | 产品唯一 ID |
| `product_name` | string | 产品名称 |
| `reason` | string | 推荐理由 |
| `best_image_url` | string | 产品图片 URL，可直接用于页面展示 |
| `score` | number | 排序分数，仅用于参考 |
| `routes` | array | 命中的召回来源 |
| `sources` | array | 回答来源资料 |

### sources 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `type` | string | 来源类型，`text` 或 `image_text` |
| `route` | string | 召回路线，例如 `text_dense`、`text_keyword`、`image_text` |
| `content` | string | 来源内容片段 |
| `image_url` | string | 图片语义来源对应图片 URL，没有则为空 |

### 前端/业务建议使用字段

页面展示通常只需要：

```text
answer
products[].product_name
products[].reason
products[].best_image_url
products[].sources
```

`score`、`routes`、`debug` 主要用于排查，不建议直接展示给普通用户。

## 4. 搜索接口

只做检索和 rerank，不调用大模型生成回答。适合搜索页、候选列表页、调试召回结果。

### 请求

```http
POST {BASE_URL}/api/rag/search
Content-Type: application/json
```

### 请求参数

```json
{
  "query": "红色礼盒春节送礼推荐哪些酒",
  "top_k": 10
}
```

### 请求字段说明

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `query` | string | 是 | 用户问题，不能为空 |
| `top_k` | number | 否 | 返回候选产品数量，建议 `10` |

### curl 示例

```bash
curl -X POST {BASE_URL}/api/rag/search \
  -H "Content-Type: application/json" \
  -d '{"query":"红色礼盒春节送礼推荐哪些酒","top_k":10}'
```

### 成功返回示例

```json
{
  "ok": true,
  "query": "红色礼盒春节送礼推荐哪些酒",
  "route_counts": {
    "text_dense": 50,
    "text_keyword": 50,
    "image_text": 30,
    "image_vector": 0
  },
  "retrieval_count": 30,
  "result_count": 10,
  "products": [
    {
      "product_id": "prod_0200_石荣霄_r110",
      "final_score": 0.1943615539,
      "rrf_score": 0.0639931056,
      "rerank_score": 0.1943615539,
      "rerank_mode": "model",
      "routes": ["image_text", "text_dense", "text_keyword"],
      "best_text": "产品资料摘要...",
      "best_image_path": "output/images/demo.png",
      "best_image_url": "https://example-bucket.oss-cn-beijing.aliyuncs.com/rag/images/demo.png",
      "image_source": "image_hit",
      "doc_hits": [],
      "image_hits": []
    }
  ]
}
```

### 返回字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `ok` | boolean | 请求是否成功 |
| `query` | string | 原始问题 |
| `route_counts` | object | 各召回路线命中数量 |
| `retrieval_count` | number | RRF 候选数量 |
| `result_count` | number | 最终返回数量 |
| `products` | array | rerank 后候选产品 |

### search products 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `product_id` | string | 产品唯一 ID |
| `final_score` | number | 最终排序分数 |
| `rrf_score` | number | RRF 候选分数 |
| `rerank_score` | number | rerank 分数 |
| `rerank_mode` | string | `model` 表示真实 rerank，`fallback_rrf` 表示回退 |
| `routes` | array | 命中的召回来源 |
| `best_text` | string | 产品资料摘要 |
| `best_image_path` | string | 本地图片路径，仅用于排查 |
| `best_image_url` | string | 产品图片 URL |
| `image_source` | string | 图片来源 |
| `doc_hits` | array | 文本命中摘要 |
| `image_hits` | array | 图片语义命中摘要 |

## 5. 图搜图接口预留

当前暂未实现，只是占位。

### 请求

```http
POST {BASE_URL}/api/rag/search-by-image
```

### 返回示例

```json
{
  "ok": false,
  "message": "search-by-image is not implemented yet"
}
```

## 6. 错误返回

### query 为空

HTTP 状态码：`400`

```json
{
  "detail": "query 不能为空"
}
```

### 服务内部失败

HTTP 状态码通常为 `200`，但 `ok=false`。

```json
{
  "ok": false,
  "error": "错误原因",
  "query": "用户问题"
}
```

说明：

- 不会返回 API Key、AccessKey、Secret、Password。
- 详细异常会写在服务端日志里。
- 如果大模型回答失败，但检索和 rerank 成功，接口会尽量返回检索结果摘要和产品列表。

## 7. 调用方接入建议

### 普通问答场景

使用：

```text
POST /api/rag/answer
```

展示：

```text
answer
products
products[].product_name
products[].reason
products[].best_image_url
```

### 搜索列表场景

使用：

```text
POST /api/rag/search
```

展示：

```text
products[].best_text
products[].best_image_url
products[].routes
```

### 调试场景

`/api/rag/answer` 请求里传：

```json
{
  "debug": true
}
```

正式上线建议：

```json
{
  "debug": false
}
```

## 8. 完整示例

### 请求

```bash
curl -X POST http://127.0.0.1:8000/api/rag/answer \
  -H "Content-Type: application/json" \
  -d '{"query":"大民族有哪些适合商务送礼的酒","top_k":5,"debug":false}'
```

### 前端可用伪代码

```js
const response = await fetch(`${BASE_URL}/api/rag/answer`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    query: userQuestion,
    top_k: 5,
    debug: false
  })
});

const data = await response.json();

if (data.ok) {
  renderAnswer(data.answer);
  renderProducts(data.products);
} else {
  showError(data.error || "服务暂时不可用");
}
```

