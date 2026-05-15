# 项目结构

```text
rag/
├── src/rag_app/          # 核心服务代码
├── scripts/              # 人工运行脚本，包含数据构建、入库、上传、交互测试
├── scripts/tools/        # 保留的排查工具
├── output/               # 运行依赖的数据产物
├── docs/                 # 项目说明文档
├── archive/              # 已归档的旧测试、旧报告、不确定文件
├── 素材/                 # 原始 Excel 素材
├── 素材.zip              # 原始素材压缩包
├── .env                  # 本地环境变量，不能删除，不能提交
├── .env.example          # 环境变量模板
└── README.md
```

## src/rag_app/

核心后端能力都在这里。线上 API 或命令行入口应优先调用这里的模块。

- `config.py`：项目根目录、`.env` 加载、通用文本工具。
- `opensearch_client.py`：OpenSearch 向量检索版 SDK 客户端初始化。
- `retrieval_service.py`：多路召回、OpenSearch 查询、query embedding、RRF 候选池。
- `rerank_service.py`：`gte-rerank-v2` 精排，Rerank-only 最终排序，失败时 fallback 到 RRF。
- `answer_service.py`：完整 RAG 回答层，负责 retrieve、rerank、构造上下文、调用回答模型。
- `image_url_service.py`：读取图片 URL 映射，根据 `product_id` 给结果补图。

## scripts/

人工运行脚本。默认都以项目根目录为 `--root`，可以从项目根目录直接运行。

- `interactive_answer_test.py`：正式终端交互测试入口。
- `upload_images_to_oss.py`：上传本地图片到 OSS 并生成 URL 映射。
- `push_text_docs_to_opensearch.py`：导入文本向量到 OpenSearch。
- `push_image_vectors_to_opensearch.py`：导入图片本体向量到 OpenSearch。
- `push_image_text_docs_to_opensearch.py`：导入图片语义文本向量到 OpenSearch。
- `build_documents.py`：从清洗后的产品数据构建文本 docs。
- `build_image_text_docs.py`：从图片分析结果构建图片语义文本 docs。
- `embed_image_text_docs.py`：对图片语义文本向量化。
- `embed_documents.py`、`embed_images.py`、`analyze_images.py`：保留的数据构建脚本。
- `excel_clean.py`、`extract_excel_images.py`、`merge_image_into_products.py`：Excel 清洗、图片抽取、产品图片合并脚本。
- `tools/check_oss_env.py`：检查 OSS 环境变量是否齐全，不打印密钥。
- `tools/check_opensearch_score_direction.py`：只读检查 OpenSearch 向量分数排序方向。

## output/

运行依赖的数据产物。不要随意删除，尤其是向量文件、产品文件、图片 URL 映射。

## docs/

项目结构、链路、模型调用、数据文件、运行手册。

## archive/

清理时归档的旧文件。

- `archive/old_tests/`：早期测试和评估脚本。
- `archive/old_reports/`：早期报告、preview、评估结果。
- `archive/uncertain/`：不确定是否可删的缓存或系统文件。
