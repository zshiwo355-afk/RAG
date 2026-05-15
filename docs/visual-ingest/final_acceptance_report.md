# RAG 资料入库可视化控制台 v1 验收报告

## 页面与入口

- 前端子项目：`web/visual-ingest`
- 页面路径：`/rag/visual-ingest`
- 技术栈：Vue3 + TypeScript + Element Plus + Vite

## 后端接口

- `POST /api/rag/visual-ingest/upload`
- `POST /api/rag/visual-ingest/parse`
- `POST /api/rag/visual-ingest/chunks/preview`
- `POST /api/rag/visual-ingest/preflight`
- `POST /api/rag/visual-ingest/run`
- `POST /api/rag/visual-ingest/search-verify`
- `POST /api/rag/visual-ingest/answer-verify`
- `GET /api/rag/visual-ingest/runs/{batch_id}`

## 安全结论

- 默认 `dry_run`
- 默认不调用 `/api/rag/answer`
- 默认不调用 LLM
- v1 不开放真实入库
- v1 不执行 embedding / push / delete / reindex / force
- visual ingest 产物只写入 `uploads/visual_ingest*`
- 不写 `output/ingest_build`
- 不写 `output/ingest_embeddings`
- 不写 OpenSearch

## 已执行验证

- `make lint`：通过
- `make test`：通过，153 passed
- `npm run lint`：通过
- `npm run typecheck`：通过
- `npm run build`：通过

## 备注

- `npm install` 首次在沙箱内因网络解析失败卡住，随后使用联网授权命令安装成功。
- Vite build 有 chunk size warning，不影响本次功能。
