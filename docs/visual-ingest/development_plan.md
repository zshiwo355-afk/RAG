# RAG 资料入库可视化控制台开发计划

## 实现形态

- 前端：`web/visual-ingest`，Vite + Vue3 + TypeScript + Element Plus。
- 后端：`src/rag_app/visual_ingest_api.py`，由 `rag_api.py` 接入。
- 页面路径：`/rag/visual-ingest`。

## 后端接口

- `POST /api/rag/visual-ingest/upload`
- `POST /api/rag/visual-ingest/parse`
- `POST /api/rag/visual-ingest/chunks/preview`
- `POST /api/rag/visual-ingest/preflight`
- `POST /api/rag/visual-ingest/run`
- `POST /api/rag/visual-ingest/search-verify`
- `POST /api/rag/visual-ingest/answer-verify`
- `GET /api/rag/visual-ingest/runs/{batch_id}`

## 安全默认值

- `runMode=dry_run`
- `answerVerify=false`
- `overwrite=false`
- real-run 直接 blocked
- 不暴露 force/delete/push/reindex
- visual ingest 产物只写 `uploads/visual_ingest*`
