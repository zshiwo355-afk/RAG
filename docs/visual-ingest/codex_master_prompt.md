# Codex Master Prompt

为当前 RAG 项目实现内部自用的资料入库可视化控制台。

必须遵守：

- 使用 Vue3 + Element Plus。
- 支持文件上传、文本粘贴、JSON 粘贴。
- 支持补入已有知识库和新增知识。
- 默认 dry-run。
- 默认不调用 `/api/rag/answer`。
- 默认不调用 LLM。
- v1 不执行 embedding、push、delete、reindex、覆盖。
- 所有产物写入隔离目录。
- search 验证兼容 `product_bundles`、`products[].product_bundle`、`doc_hits`。
