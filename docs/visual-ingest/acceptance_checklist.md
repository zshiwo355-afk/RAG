# RAG 资料入库控制台验收清单

## 页面

- [ ] `/rag/visual-ingest` 可打开。
- [ ] 有 8 步步骤条。
- [ ] 支持文件上传、文本、JSON。
- [ ] 支持补入已有知识库和新增知识。
- [ ] 支持解析预览、切分预览、preflight、dry-run、search 验证、报告复制。
- [ ] Raw JSON 可查看。

## 安全

- [ ] 默认 dry-run。
- [ ] 默认不调用 answer。
- [ ] 默认不调用 LLM。
- [ ] 默认不写 OpenSearch。
- [ ] 默认不 push。
- [ ] 默认不 delete。
- [ ] preflight blocked 时不能执行 run。
- [ ] real-run 在 v1 被 blocked。

## 验证命令

- [ ] `make lint`
- [ ] `make test`
- [ ] `npm run lint`
- [ ] `npm run typecheck`
- [ ] `npm run build`
