# RAG 白酒产品与公司知识库

这是一个面向白酒产品资料的后端 RAG 项目，支持文本检索、图片语义检索、图片本体检索、多路召回、Rerank-only 精排、OSS 图片 URL 返回，以及基于产品上下文的销售助手回答。

公司知识正式入库使用独立的 `company_knowledge_hybrid` 检索表和 `/api/knowledge/*` 只读接口，支持 PostgreSQL 版本目录、私有 OSS 完整正文、ZIP/CSV 批次采集、草稿导入、发布与撤回。检索命中片段后默认返回同版本完整资产，支持部门/场景筛选；读取访问边界由部署网关控制，维护操作仅通过 CLI。本地 SQLite 模式保留兼容，白酒接口和数据表保持原样。配置与操作步骤见 [公司知识指南](docs/COMPANY_KNOWLEDGE.md)。

公司知识现支持文本向量＋关键词召回、资产级 RRF 融合和 `gte-rerank-v2` 重排，不接入图片召回；重排不可用时明确返回 RRF 回退状态。全文索引使用独立分词字段，保留原文及哈希。正式批次已使用新表 `company_knowledge_hybrid`，旧纯向量表不重建；既有资料迁移需保留版本目录及 OSS 引用，不能只切换表名或重新导入，步骤见公司知识指南。

## 当前能力

- 文本主库向量检索：`text_docs`
- 图片语义文本检索：`image_text_docs`
- 图片本体向量检索：`image_vectors`
- 多路召回和 RRF 产品级候选池
- 真实 rerank：`gte-rerank-v2`
- Rerank-only 最终排序，失败时 fallback 到 RRF
- OSS 图片上传和 `best_image_url` 返回
- 回答层：`qwen3.6-plus`
- 终端交互式测试

## 目录结构

```text
src/rag_app/          核心服务代码
scripts/              人工运行脚本
scripts/tools/        排查工具
output/               数据产物，运行依赖
docs/                 项目文档
archive/              旧测试、旧报告、不确定文件归档
素材/                 原始 Excel 素材
```

## 快速运行

```bash
python3 scripts/interactive_answer_test.py "红色礼盒春节送礼推荐哪些酒"
```

进入交互模式：

```bash
python3 scripts/interactive_answer_test.py
```

## 启动 API

```bash
python3 rag_api.py
```

默认监听：

```text
0.0.0.0:8000
```

也可以用部署脚本：

```bash
bash deploy/start.sh
```

常用接口：

```bash
curl http://127.0.0.1:8000/api/rag/health
```

```bash
curl -X POST http://127.0.0.1:8000/api/rag/answer \
  -H "Content-Type: application/json" \
  -d '{"query":"红色礼盒春节送礼推荐哪些酒","top_k":5,"debug":true}'
```

```bash
curl -X POST http://127.0.0.1:8000/api/rag/search \
  -H "Content-Type: application/json" \
  -d '{"query":"红色礼盒春节送礼推荐哪些酒","top_k":10}'
```

## 核心链路

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

## 重要环境变量

`.env` 需要配置：

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

不要把真实密钥写入代码、README 或文档。

## 不要删除

- `.env`
- `.env.example`
- `output/products_enriched.json`
- `output/documents_embedded_v2.jsonl`
- `output/images_embedded.jsonl`
- `output/image_text_embedded.jsonl`
- `output/image_url_mapping.json`
- `output/image_url_mapping.csv`
- `output/images/`
- `素材/` 和 `素材.zip`

## 后续 API 开发

如果要接 API，建议直接调用：

```python
from rag_app.answer_service import answer_query

payload = answer_query("红色礼盒春节送礼推荐哪些酒")
```

API 层只需要包装 `answer_query()` 的返回结构，不需要重新实现检索、rerank、图片补全或 LLM 调用。

更多说明见：

- `docs/PROJECT_STRUCTURE.md`
- `docs/CORE_FLOW.md`
- `docs/MODEL_CALLS.md`
- `docs/DATA_FILES.md`
- `docs/RUNBOOK.md`
- `docs/API_INTERFACE.md`
