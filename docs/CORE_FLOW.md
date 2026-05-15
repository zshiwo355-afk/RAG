# 核心链路

当前后端链路：

```text
用户问题
-> retrieval_service.retrieve()
-> text_dense / text_keyword / image_text
-> RRF 候选池 top30
-> rerank_service.rerank_candidates()
-> gte-rerank-v2 Rerank-only
-> 取 top5
-> image_url_service 补图
-> answer_service.call_answer_llm()
-> qwen3.6-plus 生成回答
-> 返回 answer + products + sources + image_url
```

## 召回阶段

文件：`src/rag_app/retrieval_service.py`

- `search_text_dense()`：使用 `text-embedding-v4` 生成 query 向量，查 `text_docs.source_text_vector`。
- `search_text_keyword()`：使用原始 query 查 `text_docs.source_text`。
- `search_image_text()`：使用 `qwen3-vl-embedding` 生成 query 向量，查 `image_text_docs.source_text_vector`。
- `search_image_by_existing_vector()`：用已有图片向量查 `image_vectors.source_image_vector`，主要用于图搜图或排查。
- `rrf_fuse()`：按 `product_id` 聚合多路结果，生成产品级候选池。

所有向量查询都显式设置 `order="DESC"`，适配当前 InnerProduct 分数越大越相似的结论。

## 精排阶段

文件：`src/rag_app/rerank_service.py`

- `build_rerank_text()`：将产品级候选压缩成适合 rerank 的文本。
- `call_rerank_model()`：调用 `gte-rerank-v2`。
- `rerank_candidates()`：默认对 RRF top30 候选做精排，输出 top10。

最终排序策略是 Rerank-only：真实 rerank 成功时 `final_score = rerank_score`；调用失败时回退到 RRF。

## 图片补全

文件：`src/rag_app/image_url_service.py`

`rerank_candidates()` 返回前会调用 `enrich_product_images()`，给每个产品补齐：

- `best_image_path`
- `best_image_url`
- `images`
- `image_source`

优先使用图片命中，其次使用 `output/products_enriched.json` 中的产品图片，再用 `output/image_url_mapping.json` 转成 OSS URL。

## 回答阶段

文件：`src/rag_app/answer_service.py`

- `answer_query()`：完整入口。
- `build_answer_context()`：取 rerank top5，构造产品上下文。
- `call_answer_llm()`：调用 `qwen3.6-plus` 生成 JSON 格式回答。

回答要求只基于检索资料，不编造价格、库存、销量、官方排名，并返回产品来源和图片 URL。

