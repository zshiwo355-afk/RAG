# RAG API 部署说明

## 1. 服务器要求

```text
Python 3.10+
2核4G 起步
不需要 GPU
能访问阿里云 OpenSearch、百炼 DashScope、OSS
```

## 2. 上传代码

建议把整个项目打包给服务器同事，至少包含：

```text
src/
scripts/
rag_api.py
test_rag_api.py
requirements.txt
.env.example
output/image_url_mapping.json
output/image_url_mapping.csv
output/products_enriched.json
output/documents_embedded_v2.jsonl
output/images_embedded.jsonl
output/image_text_embedded.jsonl
output/images/
deploy/
docs/
README.md
```

可以不传：

```text
archive/
旧测试报告
本机缓存文件
```

不要把 `.env` 里的真实密钥提交或发到群里。需要部署时，建议让同事在服务器上按 `.env.example` 自己填写，或单独私发给负责部署的人。

## 3. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 4. 配置 .env

```bash
cp .env.example .env
vim .env
```

必须填写真实值：

```text
DASHSCOPE_API_KEY
OPENSEARCH_ENDPOINT
OPENSEARCH_INSTANCE_ID
OPENSEARCH_USERNAME
OPENSEARCH_PASSWORD
OSS_ACCESS_KEY_ID
OSS_ACCESS_KEY_SECRET
OSS_ENDPOINT
OSS_BUCKET
OSS_PUBLIC_BASE_URL
```

常用默认值：

```text
ANSWER_MODEL=qwen3.6-plus
RERANK_MODEL=gte-rerank-v2
RERANK_FINAL_MODE=rerank_only
OSS_UPLOAD_PREFIX=rag/images/
RAG_API_HOST=0.0.0.0
RAG_API_PORT=8000
```

## 5. 启动服务

```bash
bash deploy/start.sh
```

也可以直接运行：

```bash
python3 rag_api.py
```

## 6. 测试接口

health：

```bash
curl http://127.0.0.1:8000/api/rag/health
```

answer：

```bash
curl -X POST http://127.0.0.1:8000/api/rag/answer \
  -H "Content-Type: application/json" \
  -d '{"query":"红色礼盒春节送礼推荐哪些酒","top_k":5,"debug":true}'
```

search：

```bash
curl -X POST http://127.0.0.1:8000/api/rag/search \
  -H "Content-Type: application/json" \
  -d '{"query":"红色礼盒春节送礼推荐哪些酒","top_k":10}'
```

本地 smoke test：

```bash
python3 test_rag_api.py
```

## 7. 返回结果说明

`/api/rag/answer` 返回：

- `answer`：自然语言回答
- `products`：推荐产品列表
- `best_image_url`：产品主图 URL
- `sources`：回答来源片段
- `debug`：调试信息，只有请求 `debug=true` 时返回完整内容

`/api/rag/search` 返回：

- `products`：rerank 后候选产品
- `final_score`：最终排序分
- `rrf_score`：RRF 候选池分
- `rerank_score`：真实 rerank 分
- `doc_hits` / `image_hits`：摘要命中来源

## 8. 常见问题

### health 失败

先看 `missing_env`，确认 `.env` 是否存在、变量名是否写对。再确认 `output/image_url_mapping.json` 是否存在。

### OpenSearch 连不上

检查：

- `OPENSEARCH_ENDPOINT`
- `OPENSEARCH_INSTANCE_ID`
- `OPENSEARCH_USERNAME`
- `OPENSEARCH_PASSWORD`
- 服务器网络是否允许访问 OpenSearch 公网或内网地址

### DashScope Key 错误

检查：

- `DASHSCOPE_API_KEY` 是否填对
- 账号是否开通 `text-embedding-v4`、`qwen3-vl-embedding`、`gte-rerank-v2`、`qwen3.6-plus`

### 图片 URL 403

检查：

- `OSS_PUBLIC_BASE_URL` 是否与 bucket 匹配
- OSS object 是否存在
- OSS bucket 或 object 的访问权限是否符合当前访问方式

### .env 没加载

确认服务从项目根目录启动，或使用：

```bash
bash deploy/start.sh
```

### 端口 8000 被占用

换端口：

```bash
RAG_API_PORT=8010 bash deploy/start.sh
```

