# 交付打包清单

## 必须包含

```text
src/
scripts/
rag_api.py
test_rag_api.py
requirements.txt
.env.example
README.md
docs/
deploy/
output/image_url_mapping.json
output/image_url_mapping.csv
output/products_enriched.json
output/documents_embedded_v2.jsonl
output/images_embedded.jsonl
output/image_text_embedded.jsonl
output/image_text_docs.jsonl
output/image_analysis.jsonl
output/image_mapping.json
output/column_mapping.json
output/images/
素材/
素材.zip
```

## 可不包含

```text
archive/
旧测试报告
旧 preview 文件
本机 __pycache__
.DS_Store
```

## 不能提交或群发

```text
.env
任何 AccessKey
任何 API Key
任何 Password
```

如果同事需要 `.env`，建议由部署负责人单独私发，或让同事根据 `.env.example` 在服务器上填写。

