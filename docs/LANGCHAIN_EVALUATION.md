# LangChain 评估说明

当前项目暂时不建议整体迁移到 LangChain。

原因很直接：项目的核心复杂度不在标准 RAG chain，而在酒类产品 Excel 清洗、图片提取、产品字段合并、质检报告匹配、manifest 去重和本地 pipeline 编排。这些都是强业务规则和本地文件状态管理，直接用框架替换收益不高，反而容易把已经可控的链路变得不透明。

## 可以逐步使用 LangChain 的边界

- Document 对象适配：把当前 `documents.json` 的 `{page_content, metadata}` 映射成 LangChain `Document`，这是最安全的接入点。
- text splitter：如果后续需要统一 PDF、长文本、网页内容的切分策略，可以局部评估 LangChain splitter。
- OpenSearch vector store：在确认现有 OpenSearch mapping、字段名、向量维度、过滤条件兼容后，可以做只读检索层 PoC。
- retriever：可以在不改写现有数据入库链路的前提下，试用 LangChain retriever 做召回实验。
- RAG chain：如果 retriever 效果、可观测性和维护成本都可接受，再考虑把问答编排局部迁移。

## 不建议直接替代的部分

- Excel 清洗：当前清洗包含产品字段识别、表头推断和源文件约束，不是通用 loader 能完整替代的。
- 图片提取：Excel 内图片定位、命名、映射到产品行是项目特有逻辑。
- 产品字段合并：文本字段、图片分析、产品 ID、source 信息之间有明确业务规则。
- manifest 去重：当前基于 `source_doc_id`、`source_sha1`、执行状态和本地产物记录，属于导入治理能力。
- 本地 pipeline 编排：现阶段目标是安全、可预检、可断点续跑，框架不应替代这些控制面。

## 渐进式迁移方案

第一步：新增 adapter。

把当前标准产物：

```text
output/ingest_build/*/documents.json
```

转换成 LangChain `Document`，保持 `page_content` 和 `metadata` 不变。这个 adapter 只读，不影响现有 embedding 和 push。

第二步：只在检索层试用 LangChain retriever。

保留现有 OpenSearch 数据和 API，通过旁路脚本对比 LangChain retriever 与现有检索服务的召回结果。

第三步：评估 OpenSearch vector store 兼容性。

重点检查：

- 现有 index/table mapping 是否兼容。
- 向量字段名是否可配置为 `source_text_vector`。
- metadata filter 是否能表达现有 `product_id`、`doc_type`、`source_doc_id` 条件。
- score 方向、topK、rerank 前后的排序是否一致。

第四步：收益明显时再考虑 RAG chain。

如果 LangChain 在 tracing、retriever 组合、提示编排、工具调用上带来可验证收益，再考虑把问答链路的一小段迁移为 PoC。

## 结论

不要为了使用框架而重写已经稳定的业务清洗链路。更合理的路径是：先在标准文档边界和检索边界增加 adapter，再用离线评估证明收益，最后决定是否迁移更上层的 RAG chain。
