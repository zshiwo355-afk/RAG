# RAG 资料入库控制台边界说明

## 允许

- 新增 Vue3 独立控制台页面。
- 新增内部 dry-run API。
- 保存上传文件到隔离目录。
- 解析和切分预览。
- search 验证。
- 手动 answer 验证。

## 禁止

- 默认调用 `/api/rag/answer`。
- 默认调用 LLM。
- embedding execute。
- push execute。
- delete。
- reindex。
- 使用 `--force`。
- 写 OpenSearch。
- 覆盖旧文档。
- 把 mock 结果当真实入库结果。

## 不确定时

如果不确定某动作是否会写库、删除、覆盖或调用 LLM，按危险操作处理，禁止默认执行。
