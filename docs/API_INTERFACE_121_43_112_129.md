# RAG 产品查询接口文档

## 1. 服务地址

当前接口服务地址固定为：

```text
http://121.43.112.129:3001
```

下文所有接口都基于这个地址调用。

## 2. 接口总览

目前对外主要有两个业务接口：

| 接口 | 类型 | 说明 | 适用场景 |
| --- | --- | --- | --- |
| `POST /api/rag/answer` | answer接口 | 加入 AI 分析，会返回自然语言回答、推荐理由、产品和图片 | 用户问答、智能推荐、销售话术 |
| `POST /api/rag/search` | search接口 | 单纯查询，只返回检索和排序后的产品结果，不生成 AI 回答 | 搜索列表、调试查询、只要候选产品 |

简单理解：

- `answer`：带 AI 分析。
- `search`：只做查询。

## 3. answer：AI 分析问答

调用方传入用户问题，接口会先查询产品资料，再调用 AI 做分析，最终返回可直接展示给用户的回答、推荐产品、推荐理由和产品图片。

### 请求地址

```http
POST http://121.43.112.129:3001/api/rag/answer
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

### 参数说明

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `query` | string | 是 | 无 | 用户问题，不能为空 |
| `top_k` | number | 否 | `5` | 返回产品数量，范围 `1-10`；问答接口最多推荐 5 个产品 |
| `debug` | boolean | 否 | `false` | 是否返回调试信息；正式业务建议传 `false` |

### curl 示例

```bash
curl -X POST http://121.43.112.129:3001/api/rag/answer \
  -H "Content-Type: application/json" \
  -d '{"query":"红色礼盒春节送礼推荐哪些酒","top_k":5,"debug":false}'
```

### 返回示例

```json
{
  "ok": true,
  "query": "红色礼盒春节送礼推荐哪些酒",
  "answer": "针对红色礼盒春节送礼需求，推荐以下几款产品...",
  "products": [
    {
      "product_id": "prod_0200_石荣霄_r110",
      "product_name": "石荣霄酒·大师手作 臻品迎春",
      "reason": "中国红主色调搭配帝王金，符合春节喜庆审美，适合春节送礼。",
      "best_image_url": "https://xxx.oss-cn-beijing.aliyuncs.com/rag/images/xxx.png",
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
          "image_url": "https://xxx.oss-cn-beijing.aliyuncs.com/rag/images/xxx.png"
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
| `query` | string | 本次查询问题 |
| `answer` | string | AI 生成的自然语言回答，可直接展示给用户 |
| `products` | array | AI 推荐的产品列表 |
| `debug` | object | 调试信息；`debug=false` 时为空对象 |
| `message` | string | 可选字段；当 AI 回答模型失败但检索可用时，会提示返回的是检索兜底结果 |

### products 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `product_id` | string | 产品唯一 ID |
| `product_name` | string | 产品名称 |
| `reason` | string | AI 给出的推荐理由 |
| `best_image_url` | string | 产品图片 URL，可直接用于前端展示 |
| `score` | number | 排序分数，仅供参考 |
| `routes` | array | 命中的召回来源 |
| `sources` | array | 回答依据的资料片段 |

### 前端建议展示字段

```text
answer
products[].product_name
products[].reason
products[].best_image_url
products[].sources
```

## 4. search：单纯查询

该接口只做产品资料查询、召回和排序，不调用 AI 生成自然语言回答。适合只需要拿到产品候选列表的场景。

### 请求地址

```http
POST http://121.43.112.129:3001/api/rag/search
Content-Type: application/json
```

### 请求参数

```json
{
  "query": "红色礼盒春节送礼推荐哪些酒",
  "top_k": 10
}
```

### 参数说明

| 字段 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- |
| `query` | string | 是 | 无 | 查询内容，不能为空 |
| `top_k` | number | 否 | `10` | 返回候选产品数量，范围 `1-30` |

### curl 示例

```bash
curl -X POST http://121.43.112.129:3001/api/rag/search \
  -H "Content-Type: application/json" \
  -d '{"query":"红色礼盒春节送礼推荐哪些酒","top_k":10}'
```

### 返回示例

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
      "best_image_path": "output/images/xxx.png",
      "best_image_url": "https://xxx.oss-cn-beijing.aliyuncs.com/rag/images/xxx.png",
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
| `query` | string | 本次查询内容 |
| `route_counts` | object | 各召回路线命中数量 |
| `retrieval_count` | number | 检索候选数量 |
| `result_count` | number | 最终返回产品数量 |
| `products` | array | 查询和排序后的产品列表 |

### products 字段说明

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `product_id` | string | 产品唯一 ID |
| `final_score` | number | 最终排序分数 |
| `rrf_score` | number | 多路召回融合分数 |
| `rerank_score` | number | 精排分数 |
| `rerank_mode` | string | `model` 表示模型精排；`fallback_rrf` 表示模型精排失败后使用召回分数兜底 |
| `routes` | array | 命中的召回来源 |
| `best_text` | string | 产品资料摘要 |
| `best_image_path` | string | 服务器本地图片路径，仅用于排查，不建议前端展示 |
| `best_image_url` | string | 产品图片 URL，可直接用于前端展示 |
| `image_source` | string | 图片来源 |
| `doc_hits` | array | 文本命中摘要 |
| `image_hits` | array | 图片语义命中摘要 |

### 前端建议展示字段

```text
products[].best_text
products[].best_image_url
products[].routes
```

## 5. 健康检查接口

该接口用于确认服务是否正常，一般只在联调或运维检查时使用。

### 请求地址

```http
GET http://121.43.112.129:3001/api/rag/health
```

### curl 示例

```bash
curl http://121.43.112.129:3001/api/rag/health
```

### 返回示例

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

## 7. 接入建议

普通用户问答、智能推荐、需要推荐理由时，使用 a 开头接口：

```text
POST http://121.43.112.129:3001/api/rag/answer
```

只需要产品查询结果、不需要 AI 分析时，使用 s 开头接口：

```text
POST http://121.43.112.129:3001/api/rag/search
```

正式上线推荐参数：

```json
{
  "top_k": 5,
  "debug": false
}
```

## 8. 前端调用示例

### AI 分析问答

```js
const BASE_URL = "http://121.43.112.129:3001";

const response = await fetch(`${BASE_URL}/api/rag/answer`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json"
  },
  body: JSON.stringify({
    query: "红色礼盒春节送礼推荐哪些酒",
    top_k: 5,
    debug: false
  })
});

const data = await response.json();

if (data.ok) {
  console.log(data.answer);
  console.log(data.products);
} else {
  console.error(data.error || "服务暂时不可用");
}
```

### 单纯查询

```js
const BASE_URL = "http://121.43.112.129:3001";

const response = await fetch(`${BASE_URL}/api/rag/search`, {
  method: "POST",
  headers: {
    "Content-Type": "application/json"
  },
  body: JSON.stringify({
    query: "红色礼盒春节送礼推荐哪些酒",
    top_k: 10
  })
});

const data = await response.json();

if (data.ok) {
  console.log(data.products);
} else {
  console.error(data.error || "服务暂时不可用");
}
```
