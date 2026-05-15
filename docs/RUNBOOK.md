# 运行手册

## 交互测试

单条问题：

```bash
python3 scripts/interactive_answer_test.py "红色礼盒春节送礼推荐哪些酒"
```

进入交互模式：

```bash
python3 scripts/interactive_answer_test.py
```

## API 服务

启动：

```bash
python3 rag_api.py
```

或：

```bash
bash deploy/start.sh
```

健康检查：

```bash
curl http://127.0.0.1:8000/api/rag/health
```

只搜索不生成回答：

```bash
curl -X POST http://127.0.0.1:8000/api/rag/search \
  -H "Content-Type: application/json" \
  -d '{"query":"红色礼盒春节送礼推荐哪些酒","top_k":10}'
```

RAG 回答：

```bash
curl -X POST http://127.0.0.1:8000/api/rag/answer \
  -H "Content-Type: application/json" \
  -d '{"query":"红色礼盒春节送礼推荐哪些酒","top_k":5,"debug":true}'
```

交互命令：

- `/debug`：显示检索、rerank、回答模型调试信息。
- `/urls`：检查返回的图片 URL 是否可访问。
- `/save`：保存本轮会话日志到 `output/interactive_answer_test_log.*`。
- `/examples`：显示示例问题。

## OSS 上传

预演，不真正上传：

```bash
python3 scripts/upload_images_to_oss.py --dry-run
```

正式上传：

```bash
python3 scripts/upload_images_to_oss.py
```

上传脚本会生成：

- `output/image_url_mapping.json`
- `output/image_url_mapping.csv`
- `output/image_upload_report.md`

## OpenSearch 入库脚本

文本主库：

```bash
python3 scripts/push_text_docs_to_opensearch.py
```

图片本体向量：

```bash
python3 scripts/push_image_vectors_to_opensearch.py
```

图片语义文本向量：

```bash
python3 scripts/push_image_text_docs_to_opensearch.py
```

## 数据构建脚本

```bash
python3 scripts/build_documents.py
python3 scripts/build_image_text_docs.py
python3 scripts/embed_image_text_docs.py
```

不要在生产验证时随意运行全量 embedding 或入库脚本，避免重复调用模型或重复写入 OpenSearch。

## 环境变量

`.env` 需要配置以下变量，不要把真实值写入文档或提交：

```text
DASHSCOPE_API_KEY
ANSWER_MODEL
RERANK_MODEL
RERANK_FINAL_MODE
OPENSEARCH_ENDPOINT
OPENSEARCH_INSTANCE_ID
OPENSEARCH_USERNAME
OPENSEARCH_PASSWORD
OSS_ACCESS_KEY_ID
OSS_ACCESS_KEY_SECRET
OSS_ENDPOINT
OSS_BUCKET
OSS_PUBLIC_BASE_URL
OSS_UPLOAD_PREFIX
```

推荐默认：

```text
ANSWER_MODEL=qwen3.6-plus
RERANK_MODEL=gte-rerank-v2
RERANK_FINAL_MODE=rerank_only
OSS_UPLOAD_PREFIX=rag/images/
```

## 排查工具

检查 OSS 环境变量：

```bash
python3 scripts/tools/check_oss_env.py
```

检查 OpenSearch 向量分数方向：

```bash
python3 scripts/tools/check_opensearch_score_direction.py
```

该工具只读 OpenSearch，会生成分数方向检查报告。
