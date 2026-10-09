# 已核验原件到可复用知识

阶段 3 在阶段 2 持久队列上增加精确查重、版本基线、规则自动发布和异常补充。新收件使用 `receipt-rules-v2`，已有任意规则版本的收件不自动重跑；旧 v1 未结束任务仍按原规则只建草稿。员工定时采集属于后续阶段。

自动发布由 `KNOWLEDGE_AUTO_PUBLISH_ENABLED=1` 显式启用，默认关闭。关闭时仍可查重并保存草稿，已有正式知识查询不受影响。本页描述代码行为；实际上线以部署回执为准。

## 自动发布与旧知识兼容

复用 `company-assets-source-v2` ZIP 和原 `交付清单.csv` 的十列，不改变旧格式解析。自动发布资料须在原列后附加以下可选列（不允许重名或未知列）：`基线修订、共享范围、使用对象、输入、输出、依赖与权限、适用边界`。基线可留空用于新资产；更新时须指定当前最新修订。共享范围必须为 `全员`，用途、来源定位、证据状态和上述复用信息必须明确；`未取得/未提供/未知/待补` 等占位符不能通过。依赖明确为“无”可以通过。

来源完整、正文及必要附件可解析、无缺件/脱敏问题、共享范围与复用信息完整且版本关系无冲突的结构化交付才可自动发布。普通对话、仅有文件正文、清单含脱敏信息、凭据文件、缺失依赖、共享范围不明、版本冲突均停留待处理。提交身份经过认证，不代表独立作者或业务效果已经核实，作者仍为“未确认”；清单的证据状态按原声明保留。

这是可执行的结构和已知敏感模式检查，不是对所有敏感信息、事实正确性或语义冲突的完备判断。目前检查精确正文、附件指纹、业务元数据与同标题冲突，不做语义近似合并，也不会执行上传的 Skill 脚本。采集端需交付可复用资料，不把任意原始聊天直接当正式知识。

新增 `knowledge_revision_fingerprints`、`knowledge_submissions` 两张旁路表。初始化读取既有修订生成比较基线，不改旧正文、修订、发布状态；缺少历史附件证据记为 NULL，不能推断“无附件”。发现这种旧正文匹配时转待核验。来源与提交单独留痕，收件编号变化不会形成正文新版本。

- 同来源正文、附件和业务信息相同：复用已有修订，保留新的提交记录；已公开的精确重复显示 `duplicate`。
- 相同来源变更：形成新草稿，当前正式版继续可查。基线缺失或不匹配时不自动发布；补交当前修订号后重新判定。到达时间不代表内容先后顺序。
- 旧版本复传：显示 `outdated`，不覆盖当前版本。复用另一来源的公开知识不会获得修改它的权限；其他人的私有草稿不参与公共匹配。
- 解析产生的多个案例按已有稳定案例身份分别保存，不能仅用一个文件路径合并成同一项知识。

发布先写入不可变切片，再校验所有切片、向量召回、关键词召回与完整正文。最终在同一数据库事务中重新检查查重/冲突、版本号、generation、worker 租约，切换正式版本并写入处理回执与验证记录。任一失败都不切换；索引故障可重试，版本冲突进入待处理。人工 CLI 显式回退语义保留，自动任务不能借此回退旧版本。

## 异常闭环和显示

沿用动态 `company_knowledge.review` 权限，门户写开关和 CSRF 同时满足才能操作。`POST /jobs/{job}/items/{item}/resolve` 只接受 `needs_review` 项，使用 `expected_version` 做并发检查；原因必填，操作者从可信会话取得。

`archive` 保留原件与记录；`replace` 关联经原上传链路验证、同提交人且同 source_id 的新收件，生成或复用其处理任务。该操作表示已补充，不表示新材料通过审核。没有任意 URL 下载、直接改 OSS 对象或“一键跳过所有规则”的接口。归档记录、发布回执不会被后续任务重试覆盖。

新增项状态 `duplicate/outdated/indexing/published`；job 生命周期保持不变。`published` 为发布历史回执，正文预览另用 `currently_published` 判断该修订是否仍是当前正式版本。概览 `published_assets` 实时统计正式知识资产，与处理项数、修订数和切片数不同。

以下是沿用的阶段 2 解析、保存与队列能力；其中旧 v1 专属行为已明确标注。

## 输入与结果

支持独立 `.md`、`.txt` 和现有 ZIP 包。单文件适配为内部 ZIP 后复用 `knowledge_intake.process_batch`；包内仍使用已有格式解析与依赖检查。来源命名空间由服务端以可信 `principal` 与 `source_id` 计算，不能用员工自填名字或单独的来源 ID 覆盖别人的知识。提交人不等于作者，草稿 `contributor=未确认`。

- 原件最多 64 MiB；ZIP 最多 2,000 项、累计展开 32 MiB、单项 8 MiB。超限或无法打开的包整体进入 `needs_review`，原件保留，不对超限清单声称已经逐项解析。
- 正常包中的每个文件都有去向。主文件由资产结果代表，相关附件及不入库脚本等另存文件结果，避免同一主文件同时计为草稿和归档。包内派生资产可以一份源文件产生多项结果。
- v1 无解析、依赖或敏感问题的内容可保存为未批准 `draft`；v2 另按上面的规则判断，作者未知不会被认证提交身份伪装成已核实作者。
- 缺附件、无法解析、敏感替换、版本冲突等进入 `needs_review`，保留固定原因码。发现凭据文件时整个包的候选都不得自动导为草稿。脚本仅登记为归档，不执行。
- 旧 v1 同来源不同 payload 不自动创建修订；v2 使用前述稳定来源身份和版本基线。

## 存储与恢复

处理表为 `knowledge_processing_jobs`、`knowledge_processing_items`、`knowledge_processing_events`，另有前述两张治理表。条目新增 version、resolution_json、publication_json；数据库保存状态、元数据、对象引用和事件，不保存大正文。原收件表及其 `verified` 语义保持不变。

清洗正文、候选元数据和处理结果使用新收件 Bucket 的 `knowledge-receipts/processed/` 前缀，内容寻址、私有 ACL、禁止覆盖，写后读取校验。导入的草稿正文复用既有 `KnowledgeObjects` 和正式目录配置，不改旧正文。预览只从处理产物读取已清洗正文，校验 ACL、字节数及 SHA-256，最多 500,000 字符；不返回原包、OSS 路径、签名 URL 或凭据。

任务使用 PostgreSQL/SQLite 持久表，不使用进程内任务队列。一个有效处理槽、90 秒租约、每 30 秒续租；临时失败最多 5 次，退避 30/60/120/240 秒后终止为 `failed`。手动重试只接受 `failed`，并记录服务端传入的真实 actor；`needs_review` 不提供无意义重试。

领取、进度、结果、结束都检查租约。草稿写事务内通过同一数据库连接锁住租约行并校验当前 token，过期 worker 不能提交草稿。正文 OSS 写在事务外，失败尝试可能留下未引用的私有对象，但不会变为已发布知识，不自动删除。若草稿提交后回执未落库，重新解析相同原件并复用同一草稿，不新增修订；确定性的正文及元数据对象也可复用。临时工作目录只在进程内短期存在，正常退出/异常会清理，持久产物和原件保留。进程强杀留下的临时目录不作为恢复依据。

## 显式初始化和 worker

先完成收件表及既有知识目录初始化、数据库备份。显式初始化增加缺失的表、索引和列，并生成旧修订基线；API 和普通读取不建表。

```sh
python scripts/knowledge_processing.py init-db
python scripts/knowledge_processing.py init-db --execute
```

首条默认 dry-run。worker 还需要显式 `KNOWLEDGE_PROCESSING_ENABLED=1`：

```sh
python scripts/knowledge_processing.py work --once
python scripts/knowledge_processing.py work --loop
```

解释器、工作目录和服务管理器应明确配置。CLI 不自行加载 `.env`，不使用 `source` 回显秘密。沿用 `KNOWLEDGE_DATABASE_URL`、收件私有桶的 `KNOWLEDGE_RECEIPTS_OSS_*`，另需既有草稿正文的 `KNOWLEDGE_OSS_*` 配置。复用依赖，无需新增队列服务。`--sqlite` 仅用于明确的本地开发/测试。

`--once` 的 `task_claimed=true` 只表示领取了一项任务，不代表导入成功；须读取任务状态和每项原因。关闭处理 worker 不影响收件核验或旧查询。此文档不是生产部署回执。

## 管理 API 对接

`ProcessingStore` 复用 `ReceiptStore` 的构造参数；`principal=None` 只供已被管理层明确授权的全局查询，路由不得直接信任客户端提交这个范围。

| 方法 | 返回/限制 |
| --- | --- |
| `list_jobs(principal, status=None, limit=50, offset=0)` | `{items: [summary], total}`，每页最多 100；summary 不含 items/events/正文 |
| `stats(principal)` | SQL 聚合 `receipts, queued, running, completed, needs_review, failed, draft_items, review_items, archived_items` |
| `get_job(job_id, principal, limit=100, item_offset=0, event_offset=0)` | summary 加 `items`、`events`；二者各自分页，limit 最大 100，offset 最大 100 万。返回 `item_total,event_total,item_offset,event_offset,limit`，counts 始终按全量 SQL 聚合 |
| `retry(job_id, principal, actor=None)` | 仅 failed；事件记录真实操作人 |
| `ProcessingService.get_item(job_id, item_id, principal)` | 该身份可见的清洗正文与安全项目信息；无正文项返回固定错误 |

job/item/event ID 都是小写 32 位十六进制。job 状态为 `queued/running/completed/needs_review/failed`；`completed` 表示该任务技术处理结束，是否正式发布须看逐项结果。`counts` 统计处理结果项，不能冒充独立员工来源数、原始文件数或正式知识资产数。详情只返回当前页，管理台必须按 total 和 offset 提供更多项/事件，不能把第一页当作全包结果。

错误类型 `ProcessingError(code, status_code)` 只提供固定码：不存在或跨主体统一 `processing_job_not_found`/`processing_item_not_found`（404），非法筛选 `invalid_processing_query`（400），不可重试 `processing_retry_not_allowed`（409），没有预览 `processing_preview_unavailable`（409），对象预览校验/读取失败同码 503。下游异常不返回 SDK 消息、原文或凭据。

## 当前正式知识的只读关系图谱

RAG 提供 `GET /api/rag/knowledge-processing/graph`，门户通过 MCP 的 `GET /portal/knowledge/api/graph` 访问；两端都不接受查询参数。权限沿用公司范围 `company_knowledge.read` 或 `company_knowledge.review`，仅有 dashboard 或本人提交权限不能读取。图谱只使用当前正式发布的修订，不读取私有草稿，不把原始 OSS 包、附件、来源路径、收件身份或私有元数据公开。

响应包含 `nodes/edges/status/counts/truncated/generated_at`。节点使用 `knowledge_id/title/kind/revision/uploader_position`；边使用 `id/source/target/relation/label/directed/reason/evidence/terms`。证据只含当前正式知识的 `knowledge_id/revision/excerpt`，摘录为原文连续子串。`counts` 分别统计 `published_assets/indexed_assets/failed_assets/explicit_edges/related_edges`，状态为 `building/ready/partial`；不能把已索引量或边数当作正式知识总量。

实线分为“正文引用”和“已记录关联”：前者仅接受可唯一定位的规范知识 ID、Wiki 精确唯一标题或可信范围中的 Markdown 相对文件路径；后者来自现有 `evidence.related_assets` 中确实指向当前正式 ID 的声明，没有正文证据时不称为引用。相对文件路径只在相同非空 `evidence.governance_receipt_id` 内按源目录解析，并要求目标唯一；该字段由治理导入写入。来源域缺失、跨收件同名包、歧义标题、代码示例、不存在的相对目标均不推断连线，不提供包根回退。

虚线“内容相关”由正文词项的 TF-IDF 权重比较得到，要求至少两个互不包含的共同词项、相似门槛和双向前三候选；提供共同用词及两端正文摘录。相似不是概率、语义事实、真实引用、业务依赖或效果证明；任一方向已有明确关系时，不再给同一对知识叠加推荐边。为限制来源模板污染，推荐特征排除来源声明、代码等段落，但来源段中的实际引用仍可解析。每篇词项分析最多前 32,000 字符、128 个词项，明确链接最多 32 项；推荐候选词最多覆盖 64 篇且受库内文档频率比例限制。

实现使用 API 进程内有界缓存，不持久化完整正文或派生结果，最多保留两个目录身份。4 个后台线程调用现有 `get_published` 校验并读取缺失正文，缓存键包含知识 ID、修订、内容哈希及发布时间；正常轮询不重复读取未变化正文，普通新草稿也不使公开缓存失效。发布新增、换版或同修订撤回后再发布则补读。待处理任务总量上限 1,000，过期任务实际读取前检查当前键，失败至少退避 60 秒，在后续请求中重试。派生边缓存按完整发布键及已完成特征集合失效；每个请求结束前重新读取发布快照，过滤已撤回/替换的节点、关系和旧版本证据，不缓存授权结果。

服务端每次最多纳入 1,000 份当前正式知识、返回 2,000 条边，响应预算小于 3 MiB；全库总数单独返回，截断明确标记 `truncated`。冷启动返回 `building` 并异步补齐；失败或容量截断展示 `partial`。门户可见、联网且已登录时每 30 秒刷新，后台发布后的节点和关系随后更新；这不是实时推送。进程重启将重新建立派生缓存。本模块**无新 schema 迁移、依赖、环境变量或处理 Worker 变更**，无需为图谱重跑收件或修改已有发布链路。页面交互及完整 API 字段见 [KNOWLEDGE_PORTAL.md](../web/visual-ingest/KNOWLEDGE_PORTAL.md)。

2026-10-09 本轮代码相关回归为 `191 passed, 1 skipped`。RAG 与 MCP 已完成部署，**线上只读验收通过**：北京时间 16:30 的[验收 JSON](/Users/xx/Documents/ChatGPT/数字资产/output/knowledge-wiki-20261009/live-acceptance.json)为 `ok=true`，8 项检查全部通过；149 项正式知识全部索引，0 项失败，0 条明确关系，68 条内容相关。读取 69 篇当前正式正文，逐一核对 136 段证据均为同修订原文连续子串；同时验证目录/图谱修订一致、边端点公开、非法查询与私有范围拒绝、重复读取稳定，以及退出后访问拒绝。完整部署与验收范围见 [Wiki 知识图谱上线验收](/Users/xx/Documents/ChatGPT/数字资产/Wiki知识图谱上线验收_20261009.md)。

本轮线上验收没有生产上传、生产发布或权限变更，未把真实员工新增资料送入链路以实测发布后的自动增量更新；该行为已有本地回归，仍需后续实际业务材料验收。可反查的摘录不等于推荐业务相关性已经逐条确认，词项相似也不证明依赖或事实关系。当前 0 条明确关系如实保留，不根据分类或相似词补造引用。

## 验证边界

`tests/test_knowledge_processing.py` 使用真实 SQLite 事务、ZIP 解析、脱敏与草稿存储，OSS 接口使用替身。覆盖幂等/领取并发、租约过期写入拒绝、草稿提交后中断恢复、身份隔离、原件复验、清洗预览、缺件/敏感/脚本/非法 ZIP、尺寸限制和有界重试。真实 PostgreSQL 并发与真实 OSS 读写须另行在隔离环境验证，不能由本地替身测试推断。

旧测试中的 `judge-batch` dry-run 可能加载本机 `.env`；运行联合离线回归前必须禁用该加载并禁止真实外连，不能仅相信 `offline` 标记。截至 2026-10-08 用户要求暂停时，阶段 2 未进行生产部署或对象清理；后续按用户恢复指令与实施计划继续。
