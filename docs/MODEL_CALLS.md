# 模型与外部服务调用位置

## 回答大模型

```text
文件：src/rag_app/answer_service.py
函数：call_answer_llm()
默认模型：qwen3.6-plus
环境变量：DASHSCOPE_API_KEY, ANSWER_MODEL
作用：根据 top5 产品上下文生成最终自然语言回答和结构化产品推荐。
```

调用链：

```text
answer_query()
-> build_answer_context()
-> call_answer_llm()
```

## 文本向量模型

```text
文件：src/rag_app/retrieval_service.py
函数：embed_text_query()
模型：text-embedding-v4
环境变量：DASHSCOPE_API_KEY
作用：把用户 query 转成 1024 维文本向量，用于查询 text_docs.source_text_vector。
```

## 图片语义/多模态向量模型

```text
文件：src/rag_app/retrieval_service.py
函数：embed_multimodal_text_query()
模型：qwen3-vl-embedding
环境变量：DASHSCOPE_API_KEY
作用：把用户 query 转成 1024 维多模态语义向量，用于查询 image_text_docs.source_text_vector。
```

图片语义文本离线向量化脚本：

```text
文件：scripts/embed_image_text_docs.py
模型：qwen3-vl-embedding
作用：把 output/image_text_docs.jsonl 向量化为 output/image_text_embedded.jsonl。
```

图片本体离线向量化脚本：

```text
文件：scripts/embed_images.py
模型：qwen3-vl-embedding
作用：把 output/images/ 下的图片向量化为 output/images_embedded.jsonl，用于 image_vectors。
```

## Rerank 模型

```text
文件：src/rag_app/rerank_service.py
函数：call_rerank_model()
默认模型：gte-rerank-v2
环境变量：DASHSCOPE_API_KEY, RERANK_MODEL
作用：对 RRF top30 产品候选做精排。当前最终排序为 Rerank-only。
```

## OpenSearch

```text
客户端文件：src/rag_app/opensearch_client.py
查询文件：src/rag_app/retrieval_service.py
导入脚本：scripts/push_text_docs_to_opensearch.py
导入脚本：scripts/push_image_vectors_to_opensearch.py
导入脚本：scripts/push_image_text_docs_to_opensearch.py
环境变量：OPENSEARCH_ENDPOINT, OPENSEARCH_INSTANCE_ID, OPENSEARCH_USERNAME, OPENSEARCH_PASSWORD
```

查询表：

- `<instance_id>_text_docs`
- `<instance_id>_image_text_docs`
- `<instance_id>_image_vectors`

向量查询字段：

- `text_docs.source_text_vector`
- `image_text_docs.source_text_vector`
- `image_vectors.source_image_vector`

## OSS 图片处理

```text
上传脚本：scripts/upload_images_to_oss.py
URL 映射：src/rag_app/image_url_service.py
映射文件：output/image_url_mapping.json
环境变量：OSS_ACCESS_KEY_ID, OSS_ACCESS_KEY_SECRET, OSS_ENDPOINT, OSS_BUCKET, OSS_PUBLIC_BASE_URL, OSS_UPLOAD_PREFIX
```

`upload_images_to_oss.py` 只负责上传和生成映射文件，不会写回 OpenSearch。运行时结果补图由 `image_url_service.enrich_product_images()` 完成。

