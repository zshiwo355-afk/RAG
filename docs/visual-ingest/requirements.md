# RAG 资料入库可视化控制台需求

目标是在当前 RAG 项目中新增一个内部自用控制台，把资料输入、文件上传、解析预览、切分预览、preflight、dry-run、检索验证和结果报告放到页面上完成。

v1 默认只做 dry-run 和本地预览，不开放真实 embedding、push、delete、reindex 或覆盖写入。

## 支持输入

- 文件上传：`.txt`、`.md`、`.json`、`.csv`、`.xlsx`、`.xls`、`.pdf`
- 文本粘贴
- JSON 粘贴

文件上传后必须先解析预览、切分预览和 preflight，不会直接入库。

## 入库场景

- `supplement_existing`：补入已有知识库，默认推荐。
- `create_new_knowledge`：新增知识，允许填写名称、描述、标签、版本号。

两种场景都必须先 dry-run。v1 不开放真实写库。

## 验证

- 默认只调用 search 验证。
- `/api/rag/answer` 默认关闭。
- answer 验证必须手动开启并输入确认口令。
