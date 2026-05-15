# 最小运行包说明

## 1. 交付目标

本次交付包只保留当前 API 运行和部署所需的最小文件，不包含开发报告、测试样本、归档数据、原始素材和离线构建脚本。

## 2. 已保留文件

运行入口与部署文件：

- `rag_api.py`
- `requirements.txt`
- `deploy/start.sh`

运行核心代码：

- `src/rag_app/__init__.py`
- `src/rag_app/config.py`
- `src/rag_app/opensearch_client.py`
- `src/rag_app/retrieval_service.py`
- `src/rag_app/rerank_service.py`
- `src/rag_app/answer_service.py`
- `src/rag_app/image_url_service.py`

运行期必需数据文件：

- `output/image_url_mapping.json`
- `output/products_enriched.json`

说明：

- `output/image_url_mapping.json` 用于图片回源和健康检查里的 `image_mapping_ready` / `image_mapping_count`。
- `output/products_enriched.json` 用于按 `product_id` 回填产品图片索引，否则 `images[]` 和 `best_image_url` 的补全能力会受影响。
- 产品主文本、图片向量、PDF 向量已经在线上 OpenSearch 中，API 运行时不直接依赖本地嵌入 JSONL，因此没有打进最小包。

## 3. 已排除内容

以下内容未打包，因为不影响当前 API 运行：

- `archive/`
  - 历史报告、旧测试、旧评估结果
- `docs/`
  - 项目说明和接口文档
- `dist/`
  - 历史压缩包
- `scripts/`
  - 除 `deploy/start.sh` 外的离线构建、推送、运维脚本
- `output/` 中的大部分文件
  - embedding 产物、push 报告、manual review、候选 CSV、sample rebuild、md 报告等
- `output/images/`
  - 本地图片原图；当前线上展示主用 `image_url`，运行 API 不必依赖本地图片文件
- `output_excel_clean_test/`
- `产品信息/`
- `素材/`
- `质检报告/`
- `README.md`
- `test_rag_api.py`
- `__pycache__/`、`.pyc`、`.DS_Store`

排除原因：

- 这些文件属于开发辅助、测试验证、历史归档或原始数据，不是当前最小服务启动的硬依赖。

## 4. 启动命令

推荐启动步骤：

```bash
python3 -m pip install -r requirements.txt
bash deploy/start.sh
```

也可以直接启动：

```bash
python3 -m uvicorn rag_api:app --host 0.0.0.0 --port 8000
```

## 5. 必需环境变量

必须准备：

- `DASHSCOPE_API_KEY`
- `OPENSEARCH_ENDPOINT`
- `OPENSEARCH_INSTANCE_ID`
- `OPENSEARCH_USERNAME`
- `OPENSEARCH_PASSWORD`

可选：

- `RAG_API_HOST`
- `RAG_API_PORT`
- `RAG_API_LOG_LEVEL`
- `RERANK_MODEL`
- `ANSWER_MODEL`
- `RERANK_FINAL_MODE`

## 6. 缺失会影响运行的文件

以下文件缺失会影响服务能力：

- `rag_api.py`
  - 无法启动 FastAPI 服务
- `src/rag_app/*.py`
  - 检索、精排、回答、图片回源全部依赖这里
- `output/image_url_mapping.json`
  - 健康检查会显示图片映射未就绪，图片 URL 补全能力受影响
- `output/products_enriched.json`
  - 产品级图片索引无法补全，`images[]` 聚合能力受影响
- `requirements.txt`
  - 无法按最小依赖安装运行环境

## 7. 当前包的边界

这个最小包可以直接部署当前 API 服务，但不包含以下离线能力：

- 重建 embedding
- 重新 push OpenSearch
- 重新上传 OSS
- PDF / Excel 增量构建
- unmatched 修复

如果后续要做数据重建或运维操作，需要回到完整项目目录。
