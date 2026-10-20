# 已核验原件到可复用知识

阶段 3 在阶段 2 持久队列上增加精确查重、版本基线、规则自动发布和异常补充。新收件使用 `receipt-rules-v2`，已有任意规则版本的收件不自动重跑；旧 v1 未结束任务仍按原规则只建草稿。员工定时采集属于后续阶段。

自动发布由 `KNOWLEDGE_AUTO_PUBLISH_ENABLED=1` 显式启用，默认关闭。关闭时仍可查重并保存草稿，已有正式知识查询不受影响。本页描述代码行为；实际上线以部署回执为准。

## 2026-10-19 共用接入、岗位别名与来源绑定：仅本地候选

本轮保持员工 Skill、原定时任务正文、MCP 连接配置及原有投稿/收件/检索工具不变。服务端将“这份资料的岗位确认”与“可以重复使用的完整岗位声明别名”分开保存；共用 principal 只代表接入授权，不能用于证明提交者是同一个员工。旧 `knowledge_uploader_roles` 数据保留作为历史，不再自动覆盖该 principal 的所有后续来源。下文“上传人默认岗位”段落是 2026-10-17 的旧契约，本轮候选使用本节逐项确认规则。

处理详情按 job 与 item 确认标准岗位，保留原部门、原岗位声明、操作者、版本和审计事件。仅原始部门和岗位完整且映射明确时可记住别名；声明不完整或别名发生冲突时保留待核对，不能扩大为整个人员账号的默认值。单条岗位确认不清除缺附件、脚本依赖、敏感内容、来源关系或版本冲突，worker 恢复后仍执行原有完整解析与发布规则；已经形成提交、草稿、重复引用或正式发布的条目不通过这条操作重绑。

外部 `source_id` 继续是旧 Agent 保存的工作编号，内部来源身份单独持久化。收件返回原始 `source_id`，不让客户端接管内部 UUID，也不依赖标题或来源描述证明真实员工身份。共用接入已有同编号来源时，除完整重复或可唯一核对的有效基线修订外，进入 `source_relation_review`；部门、岗位、标题或定位不同本身不足以证明独立新资料。审核者明确选择 `source_action=new` 才建立独立来源；需要确认时保留原件和阻塞状态，不能自动给旧资产改号、丢弃旧提交关系或覆盖既有发布。已有持久化 submission 的任务须恢复原知识身份、修订和发布状态；岗位别名调整本身不能重新计算历史资产身份。

### 接口兼容与迁移边界

旧请求省略 `department` 或传 `null` 的原件字节、SHA256、收件幂等和 `receipt.source_id` 均保持不变。标准岗位与原声明分字段保存，`evidence.uploader_position` 仍用于旧 Skill 的原岗位声明核验。未产生实际 revision 的候选在收件处理结果中返回 `knowledge_id=null`；已有 draft、duplicate、published 指针保留实际知识 ID 和 revision，不以候选编号冒充已入库。

MCP 服务端 `ProcessingItem` / `ProcessingResult` 拒绝未知字段；RAG 若新增处理回执元数据，必须同步兼容的 MCP 服务端模型并联合回归，不能只升级 RAG 后期待旧客户端自动忽略。普通 receipt 的未知字段会被旧模型忽略，也不能据此保证新元数据已经传到 Agent。正文投稿回执的 `source_id` 必须等于请求值；内部来源编号不可替换这一字段。

本轮仅可在新临时数据库执行初始化与回归。生产迁移须另行授权：先备份原数据库、应用版本与未完成任务清单，再显式创建所需旁路表；普通读请求不得隐式建表或补写历史记录。不能为了建新表运行会顺带补齐旧修订指纹的全量初始化，也不能批量重排全部历史收件。已发布、草稿、归档、重复复用及原件对象均须保留，缺少证据的旧来源不自动认领。

独立 `init-role-assignments` 仅负责本轮旁路 schema；`migrate-role-assignments` 默认只读预览，按同一 job 的明确历史 `uploader_role_selected` 事件核对旧选择，不把共享 principal 当前默认值复制给全部资料。同一时间选择不同岗位、多业务条目或岗位已不存在时保留歧义/阻塞。显式 `--execute` 只可恢复单业务条目、有明确历史选择、仅岗位阻塞且无 durable submission 的待处理项，preview 须展示是否可能恢复，执行结果逐项报告实际是否恢复。已发布、草稿、v1、多条目歧义或缺件等其他阻塞不能借迁移重排或发布；冻结历史选择与恢复处理必须分别说明。

例如，在显式指定的**本地临时测试库**中，先分别预览 schema 与历史选择，再决定是否执行；不要把以下路径替换为生产库来完成本轮本地验收：

```sh
PYTHON_DOTENV_DISABLED=1 python scripts/knowledge_processing.py --sqlite /absolute/path/local-test.db init-role-assignments
PYTHON_DOTENV_DISABLED=1 python scripts/knowledge_processing.py --sqlite /absolute/path/local-test.db migrate-role-assignments
PYTHON_DOTENV_DISABLED=1 python scripts/knowledge_processing.py --sqlite /absolute/path/local-test.db review-published-roles --limit 100 --offset 0
```

前两条仅在已审阅预览、备份并确认执行范围后才附加 `--execute`；只读 `review-published-roles` 没有执行开关。这些新增入口要求数据库已显式存在，不从缺失路径创建库，不调用原 `init-db`。

**回滚不能只恢复旧 worker 二进制并直接放开队列。**一旦新内部来源绑定已被采用，旧 worker 不认识这套绑定，可能按旧 `source_id` 重新计算身份。切换或回滚前必须停止领取新任务；保留旁路表、绑定和审计数据，并使用识别新旧来源的兼容版本，或停用受影响任务直至完成受控迁移与核验。不能删除新表或还原旧快照后自动重跑来“恢复”。本地兼容测试不等于已经验证生产回滚、真实 PostgreSQL 并发或员工自然 15 天执行。

针对性本地回归入口为 `tests/test_knowledge_native_legacy_compat.py`：旧无部门请求和冻结原件幂等、重启续查、未绑定候选指针、草稿/发布/重复回执保护，共用接入碰撞必须明确确认新来源，以及后来新增别名不能改变历史已发布条目的岗位显示。测试仅使用临时 SQLite 和注入的 OSS、索引替身；`tests/conftest.py` 禁用 dotenv 并阻止普通单测联网。该独立 9 项与只读审核、原维护功能合计 46 项通过；核心、API、MCP、前端及迁移另按各自回归报告验收，不能由这一组测试代替。

### 已发布资料只读复核

`knowledge_role_maintenance.review_published_role_assignments(store, limit=100, offset=0)` 接受显式数据库对应的 `KnowledgeStore` 或 `ProcessingStore`，只读取当前正式修订的元数据和既有关系；不建表、不读取 OSS 正文、不修改正式版本、源对象、向量或队列。返回原声明与正式岗位差异、该收件历史选择与正式岗位不一致/同时冲突，以及同号来源仍待核对的关联线索。它只生成待复核清单，不断言每条都是错误归岗，也不自动更正。

历史选择只取该收件的 job 事件，并排除发布后的事件。共用 principal 当前默认值、后来新增的别名、文档自行声明的 confirmation 均不能证明该正式版本曾获确认。只有当前正式修订原提交关联的冻结 item assignment、目标岗位一致且来源为 `manual/alias/legacy`，才标记 `confirmed=true`，避免把正式人工别名当成错误。此标记只说明岗位确认，不能消除另一个仍待核对的来源关系问题。

分页的 `total` 是当前正式修订总数，`total_basis=published_revisions`；`scanned_count` 是本页实际检查数，`review_candidates_this_page` 和 `items` 仅包含本页疑似项，`has_more` 表示仍有正式修订未翻页。不得因本页没有 items 就宣称全库检查通过。缺少旧/新关系表会列在 `unavailable_checks`；候选来源或事件证据超出有界读取时也返回明确截断原因。输出只保留核对需要的知识、修订、收件、条目和关联 job ID，不输出 principal、actor、凭据、正文或对象地址。

`tests/test_knowledge_role_review.py` 与既有维护测试本地共 37 项通过，覆盖只读性、两种 store、旧 schema、可信与伪造确认、共享默认/后来别名不能代替历史证据、事件歧义与发布后事件排除、分页和非法参数。该结果只覆盖本地合成数据，不表示线上已发布资料已经审查。

## 按 SOP 建立部门与岗位目录

`knowledge_roles` 保存独立岗位目录。岗位由 SOP 中明确填写的“所属部门＋岗位名称”建立，`role_id` 根据这一二元组稳定生成；同名岗位分属不同部门时 ID 不同。当前目录只增加岗位，不提供自动改名或合并。没有正式知识的已登记岗位仍会展示，三类计数为零。

通过现有收件接口上传独立 DOCX，核验原件后进入同一持久处理队列。只有正文包含明确岗位 SOP 标题，且部门、岗位字段唯一一致，解析器才识别为岗位 SOP。此时元数据使用 `kind=process`、`category=sop`、`source_protocol=company-role-sop-v1`，从文档取得岗位，无需再手填通用交付清单的复用字段；正式 SOP 不另加逐份审核步骤。文档标题优先使用明确的文档名称，再使用“部门＋岗位＋岗位SOP”，不会拿“文档基本信息”等章节标题作为资料名称。不能确定岗位、解析不全、敏感内容或版本冲突仍进入待处理，不根据上传人或文件名猜岗位。

source-v2 清单新增可选 `role_id`、`department`、`role_name` 列。Agent 应先查询该岗位 SOP，读取目录返回的三个值，再固定绑定；每次提交均核对目录的一致性。角色 ID 未知、部门/岗位名称不匹配或 SOP 正文与清单声明冲突时，返回待处理原因。`上传员工岗位` 及 `uploader_attribution` 仍表示上传人自行声明的岗位，不能替代资料归属岗位，也不代表 HR 核验。

本轮不从旧岗位字符串猜测映射，不改写旧正文、修订、哈希或发布指针。未绑定的历史资料保留在“未归属”列表；旧 v2 收件已有持久化 submission 时，重试恢复其绑定版本的正文、元数据、规则判定与发布 generation，不使用新版 DOCX 解析覆盖原指纹或补岗位/文件字段。此恢复也覆盖 submission 已提交而 processing item 尚未保存的中断；仍执行原有索引验证、租约和当前版本检查，已发布条目跳过且不重复索引。若解析改变附件成员或派生案例边界，导致原 submission 的候选身份消失或不匹配，则在任何新增导入/索引前转为 `processing_source_identity_changed` 或 `processing_submission_mismatch` 待处理，不新建身份后丢下旧条目。原记录缺少规则判定则转 `processing_decision_unavailable`。尚无 submission 的任务使用当前解析规则，升级前应清点这类旧积压，不能把规则变化当成对旧资料的授权补齐。新岗位 ID、资料类别和附件参与新资产版本指纹；相同正文属于不同岗位时保留各自归属，不静默合并到另一个岗位。

版本仍使用原有基线保护：首版 SOP 可自动发布；同来源变更不能仅凭上传时间替换正式版。稳定 source-v2 资产更新时通过 `基线修订` 指定当前最新 revision；仅再次上传变更后的裸 DOCX，不带基线时会保留旧正式版并进入 `source_base_revision_required`。当前尚无保持裸 DOCX 来源身份的新版提交入口，不能简单改包为 source-v2 就声称已更新原资产。重复上传相同字节与业务信息仍复用已有收件/版本。

岗位及附件 API 位于 `/api/rag/knowledge-processing`，经 MCP 门户同名只读代理访问：

| 路径 | 行为 |
| --- | --- |
| `GET /roles` | `{items:[{role_id,department,name,counts:{sop,skill,other},total}]}`；计数只使用当前正式版，保留零计数岗位 |
| `GET /catalog?role_id=…&category=sop` | `category` 为 `sop/skill/other`；省略岗位查全库，`role_id=unknown` 查未归属历史；兼容原 kind 和 uploader_position 筛选 |
| `GET /catalog/{knowledge_id}` | 增加 role_id、department、role_name、category 和 files；历史无附件时返回空数组 |
| `GET /catalog/{knowledge_id}/files/{file_id}?disposition=inline` | 仅当前正式版可见文件可签名；inline 仅接受安全预览件，下载原件使用 attachment；返回 url、expires_in、filename、media_type |

读取目录、预览与下载沿用公司知识读取权限。签名 URL 默认 300 秒有效，查询前后复核当前发布版本；已经发出的签名在过期前仍有效。客户端不能传 OSS key 或任意 URL 请求签名。搜索的 `role_id/category` 在调用索引前限制允许的发布版本，不能先取全库 topK 再筛岗位；`unknown` 只用于目录导航，不是语义检索岗位。

## 自动发布与旧知识兼容

复用 `company-assets-source-v2` ZIP 和原 `交付清单.csv` 的十列，不改变旧格式解析。自动发布资料须在原列后附加以下可选列（不允许重名或未知列）：`基线修订、共享范围、使用对象、输入、输出、依赖与权限、适用边界`。基线可留空用于新资产；更新时须指定当前最新修订。共享范围必须为 `全员`，用途、来源定位、证据状态和上述复用信息必须明确；`未取得/未提供/未知/待补` 等占位符不能通过。依赖明确为“无”可以通过。

来源完整、正文及必要附件可解析、无缺件/脱敏问题、共享范围与复用信息完整且版本关系无冲突的结构化交付可自动发布；上文明确识别的岗位 SOP 使用其专门规则。其余普通对话、仅有文件正文、清单含脱敏信息、凭据文件、缺失依赖、共享范围不明、版本冲突均停留待处理。提交身份经过认证，不代表独立作者或业务效果已经核实，作者仍为“未确认”；清单的证据状态按原声明保留。

这是可执行的结构和已知敏感模式检查，不是对所有敏感信息、事实正确性或语义冲突的完备判断。目前检查精确正文、附件指纹、业务元数据与同标题冲突，不做语义近似合并，也不会执行上传的 Skill 脚本。采集端需交付可复用资料，不把任意原始聊天直接当正式知识。

新增 `knowledge_revision_fingerprints`、`knowledge_submissions` 两张旁路表。初始化读取既有修订生成比较基线，不改旧正文、修订、发布状态；缺少历史附件证据记为 NULL，不能推断“无附件”。发现这种旧正文匹配时转待核验。来源与提交单独留痕，收件编号变化不会形成正文新版本。

- 同来源正文、附件和业务信息相同：复用已有修订，保留新的提交记录；已公开的精确重复显示 `duplicate`。
- 相同来源变更：形成新草稿，当前正式版继续可查。基线缺失或不匹配时不自动发布；补交当前修订号后重新判定。到达时间不代表内容先后顺序。
- 旧版本复传：显示 `outdated`，不覆盖当前版本。复用另一来源的公开知识不会获得修改它的权限；其他人的私有草稿不参与公共匹配。
- 解析产生的多个案例按已有稳定案例身份分别保存，不能仅用一个文件路径合并成同一项知识。

发布先写入不可变切片，再校验所有切片、向量召回、关键词召回与完整正文。最终在同一数据库事务中重新检查查重/冲突、版本号、generation、worker 租约，切换正式版本并写入处理回执与验证记录。任一失败都不切换；索引故障可重试，版本冲突进入待处理。人工 CLI 显式回退语义保留，自动任务不能借此回退旧版本。

## 异常闭环和显示

`POST /roles` 仅接受 `{department, name}`，要求 `company_knowledge.review`。字段长度为 1–200，
禁止控制字符，去除首尾空白后不能为空；部门必须精确匹配当前已保存的岗位目录，接口不会创建新部门。
未知部门返回 `role_department_not_found`（404），非法字段返回 `invalid_processing_request`（422）。
同部门同名岗位重复添加复用原 `role_id`；返回与 `GET /roles` 单行相同的
`{role_id, department, name, counts, total}`，数量始终来自当前正式知识。添加岗位不自动选择上传人岗位。

处理详情支持为该收件的真实上传人选择已登记岗位并记住。`GET /jobs/{job}/uploader-role`
返回 `{role, version, updated_at, requeued}`；未配置时 `role=null, version=0`。
`POST` 同路径仅接受 `{role_id, expected_version}`，两种操作都要求 `company_knowledge.review`；
上传人从服务端 job 取得，客户端不能指定 principal 或 actor。返回的 role 仅含 `role_id/department/name`。
选择持久保存在 `knowledge_uploader_roles`，按 principal 隔离，保留确认人、时间和版本，并在当前 job 记录
`uploader_role_selected` 事件。原文中的上传岗位、部门和来源声明保持不变；新来源资料采用已确认岗位，
与文档明确指定的岗位冲突时仍需核对。已有同源岗位不会被默认值覆盖；历史未归属来源不应用新增默认，
原有显式岗位声明匹配规则保持不变。同源后续修订可保留已确认时的原始岗位、部门声明，不要求修改原件。
岗位确认不是发布审批，内容与版本校验照常执行。

版本以 `expected_version` 做乐观并发控制；同一 job、同一岗位、同一前置版本的最近一次保存可重复请求，
返回原保存结果，不重复写事件或再次排队。其他旧版本写入返回 `uploader_role_version_conflict`（409）；
针对排队中或处理中的任务的新操作返回 `uploader_role_job_busy`（409）。`requeued` 表示这次保存
（或其幂等重放）曾恢复当前任务；GET 始终返回 false，任务实时进度仍应读取 job。

只有整个任务尚无草稿/发布/提交记录、待处理项全部是岗位未找到、同名歧义、部门误填、部门不匹配，
或新来源显式岗位与默认岗位冲突，且原收件仍已核验时，才恢复原 job 队列。还须核对每个待处理项的
真实上传人、source_id 与来源资产身份完全没有历史提交；已绑定历史来源的岗位冲突不在恢复范围。
原 receipt、逐项结果和历史事件保留，worker 重新执行完整
解析和发布检查。缺附件、脚本依赖、敏感内容及既有来源岗位冲突等仍保持待处理，但默认岗位可保存供后续新资料使用。
新表仅由显式 `knowledge_processing.py init-db --execute` 创建；旧 schema 的读取兼容为未配置，
普通读取不自动建表，保存返回 `uploader_role_schema_required`（503）。部署迁移及线上恢复需另行执行和验收。

沿用动态 `company_knowledge.review` 权限，门户写开关和 CSRF 同时满足才能操作。`POST /jobs/{job}/items/{item}/resolve` 只接受 `needs_review` 项，使用 `expected_version` 做并发检查；原因必填，操作者从可信会话取得。

`archive` 保留原件与记录；`replace` 关联经原上传链路验证、同提交人且同 source_id 的新收件，生成或复用其处理任务。该操作表示已补充，不表示新材料通过审核。没有任意 URL 下载、直接改 OSS 对象或“一键跳过所有规则”的接口。归档记录、发布回执不会被后续任务重试覆盖。

新增项状态 `duplicate/outdated/indexing/published`；job 生命周期保持不变。`published` 为发布历史回执，正文预览另用 `currently_published` 判断该修订是否仍是当前正式版本。概览 `published_assets` 实时统计正式知识资产，与处理项数、修订数和切片数不同。

任务列表和详情增加 `blocking_reasons`、`blocking_reasons_truncated`。仅为当前 `needs_review/failed`
任务汇总当前 job 错误码和全部 `needs_review` 项的原因，不受详情 items 分页限制，也不读取历史事件、
已归档或已发布项的原因。每页批量读取，按当前查询身份隔离；去重排序后最多返回 32 个固定公开原因码，
超出时标记截断。未知或异常原因统一为 `manual_review_required`，不返回原始错误文本或上传正文，
也不把未识别原因误报成没有阻塞。其他任务状态返回空原因数组。

以下是沿用的阶段 2 解析、保存与队列能力；其中旧 v1 专属行为已明确标注。

## 输入与结果

支持独立 `.md`、`.txt`、岗位 SOP `.docx` 和现有 ZIP 包。单文件适配为内部 ZIP 后复用 `knowledge_intake.process_batch`；包内仍使用已有格式解析与依赖检查。DOCX 的章节层级与表格会转为可检索结构，同一流程相邻操作表与验收表仅在步骤号完整一致时合并，保留两张源表定位。来源命名空间由服务端以可信 `principal` 与 `source_id` 计算，不能用员工自填名字或单独的来源 ID 覆盖别人的知识。提交人不等于作者，草稿 `contributor=未确认`。

- 原件最多 64 MiB；ZIP 最多 2,000 项、累计展开 32 MiB、单项 8 MiB。超限或无法打开的包整体进入 `needs_review`，原件保留，不对超限清单声称已经逐项解析。
- 正常包中的每个文件都有去向。主文件由资产结果代表，相关附件及不入库脚本等另存文件结果，避免同一主文件同时计为草稿和归档。包内派生资产可以一份源文件产生多项结果。
- v1 无解析、依赖或敏感问题的内容可保存为未批准 `draft`；v2 另按上面的规则判断，作者未知不会被认证提交身份伪装成已核实作者。
- 缺附件、无法解析、敏感替换、版本冲突等进入 `needs_review`，保留固定原因码。发现凭据文件时整个包的候选都不得自动导为草稿。脚本仅登记为归档，不执行。
- 旧 v1 同来源不同 payload 不自动创建修订；v2 使用前述稳定来源身份和版本基线。

## 存储与恢复

处理表为 `knowledge_processing_jobs`、`knowledge_processing_items`、`knowledge_processing_events`，另有前述两张治理表。条目新增 version、resolution_json、publication_json；数据库保存状态、元数据、对象引用和事件，不保存大正文。原收件表及其 `verified` 语义保持不变。

清洗正文、候选元数据和处理结果使用新收件 Bucket 的 `knowledge-receipts/processed/` 前缀，内容寻址、私有 ACL、禁止覆盖，写后读取校验。导入的草稿正文复用既有 `KnowledgeObjects` 和正式目录配置，不改旧正文。预览只从处理产物读取已清洗正文，校验 ACL、字节数及 SHA-256，最多 500,000 字符；不返回原包、OSS 路径、签名 URL 或凭据。

岗位新资料另有独立文件对象：原始 DOCX、转义后的安全 HTML 预览、独立 Skill 安装 ZIP 使用正文私有桶的 `company-knowledge/files/` 前缀，不把整个收件 ZIP 当单篇 SOP 的下载文件。原件与预览成功存储、读回并校验后，才将 `files` 引用和正文写入同一 revision；文件故障不能发布一个缺原件或缺预览的新版本。上段“处理项正文预览”接口保持只返回清洗文字；文件预览和下载使用新的当前正式版签名接口。

source-v2 可选 `skill_package_path` 指向收件 ZIP 内单独的安装 ZIP，仅接受 `Skill方法` 类型。安装包须有唯一 SKILL.md，路径安全且显式 Markdown 本地依赖齐全；保存校验清单和 `execution_verified=false`，不执行脚本、不承诺业务效果。未提供安装包的 Skill 资料仍是“方法说明”，不能标成可安装包。

服务器不持久保存原件：原件和派生预览长期保存在私有 OSS，数据库只保存对象引用和业务状态，向量库保存检索片段。处理时使用临时目录，正常结束及受控异常自动清理；强杀残留需要运维清理，不作为恢复资料，也不加入备份。撤回和归档不自动删除 OSS 原件、历史预览或索引片段。失败写入可留下未引用对象，本轮不实现自动物理删除；清理必须先核对收件、历史版本引用和保留期限，不能只根据“当前未发布”删除对象。

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

### 解决问题后的去向与 AI 补充要求

正常资料通过完整性、归属、来源、版本和检索检查后自动发布；重复资料保留与已有知识的关联；无须入库的资料可记录原因后归档。三种完成结果都退出“需要关注”，在“收件记录”继续保留。归档只改变本次处理结果，不删除原文件、收件记录或已发布知识。同一任务还有待核对或索引未完成的条目时，不能把整个任务提前标记完成。

`GET /api/knowledge-intake/uploads/{receipt_id}` 在原 `processing` 合同之外返回可选的顶层 `repair`：
`items_total` 是全部待处理项数量，`items` 最多 20 项，`truncated` 标记还有未返回的条目。每项包含 `item_id/version/title/status/reason_codes/missing_references`；原因使用当前原因而非已经解决的历史原因。MCP 将这些数据映射为固定的修复动作，原 AI 在查询时领取，不会主动唤醒已经结束的会话。原文件、标题、路径及正文都是待处理数据，不能成为工具执行指令。

解析时把最多 20 个安全包内相对引用路径写入 `knowledge_processing_items.repair_json`。上线前需执行已有的 processing 初始化迁移以增加这个可空列；读取旧表仍可返回原因，历史记录未保存的具体附件名称保持为空，不根据标题猜测。状态查询只读数据库，不额外读取 OSS 原件或正文。

AI 提交前应先核对正文、引用文件、脚本/Skill 包、部门与岗位、使用对象、输入输出、依赖权限和共享范围。资料不全时先自行找回真实材料，不编造缺项或声称检查通过。服务端继续执行既有发布检查；AI 自检不等于服务端已验收。补充材料仍由原提交人的渠道上传，获得核验通过的新收件后，审核员可在原条目关联补充收件并跟进新处理结果。

关联补充收件必须精确匹配原提交人、来源及条目身份；同来源下的另一份资料不能用来关闭本条问题。新收件已完成时，还须唯一匹配同一知识身份的已发布或重复条目，不重新处理已有正式结果。需要重新检查的补充条目可继承原条目明确、唯一的人工岗位选择，新条目自己的选择优先；不根据标题或多个冲突选择推测岗位，不创建全局岗位规则。重新检查仍须通过附件、正文、版本和发布检查。

错误类型 `ProcessingError(code, status_code)` 只提供固定码：不存在或跨主体统一 `processing_job_not_found`/`processing_item_not_found`（404），非法筛选 `invalid_processing_query`（400），不可重试 `processing_retry_not_allowed`（409），没有预览 `processing_preview_unavailable`（409），对象预览校验/读取失败同码 503。下游异常不返回 SDK 消息、原文或凭据。

## 当前正式知识的只读关系图谱

RAG 提供 `GET /api/rag/knowledge-processing/graph`，门户通过 MCP 的 `GET /portal/knowledge/api/graph` 访问；两端都不接受查询参数。权限沿用公司范围 `company_knowledge.read` 或 `company_knowledge.review`，仅有 dashboard 或本人提交权限不能读取。图谱只使用当前正式发布的修订，不读取私有草稿，不把原始 OSS 包、附件、来源路径、收件身份或私有元数据公开。

响应包含 `nodes/edges/status/counts/truncated/generated_at`。节点使用 `knowledge_id/title/kind/revision/uploader_position`；边使用 `id/source/target/relation/label/directed/reason/evidence/terms`。证据只含当前正式知识的 `knowledge_id/revision/excerpt`，摘录为原文连续子串。`counts` 分别统计 `published_assets/indexed_assets/failed_assets/explicit_edges/related_edges`，状态为 `building/ready/partial`；不能把已索引量或边数当作正式知识总量。

实线分为“正文引用”和“已记录关联”：前者仅接受可唯一定位的规范知识 ID、Wiki 精确唯一标题或可信范围中的 Markdown 相对文件路径；后者来自现有 `evidence.related_assets` 中确实指向当前正式 ID 的声明，没有正文证据时不称为引用。相对文件路径只在相同非空 `evidence.governance_receipt_id` 内按源目录解析，并要求目标唯一；该字段由治理导入写入。来源域缺失、跨收件同名包、歧义标题、代码示例、不存在的相对目标均不推断连线，不提供包根回退。

虚线“内容相关”由正文词项的 TF-IDF 权重比较得到，要求至少两个互不包含的共同词项、相似门槛和双向前三候选；提供共同用词及两端正文摘录。相似不是概率、语义事实、真实引用、业务依赖或效果证明；任一方向已有明确关系时，不再给同一对知识叠加推荐边。为限制来源模板污染，推荐特征排除来源声明、代码等段落，但来源段中的实际引用仍可解析。每篇词项分析最多前 32,000 字符、128 个词项，明确链接最多 32 项；推荐候选词最多覆盖 64 篇且受库内文档频率比例限制。

实现使用 API 进程内有界缓存，不持久化完整正文或派生结果，最多保留两个目录身份。4 个后台线程调用现有 `get_published` 校验并读取缺失正文，缓存键包含知识 ID、修订、内容哈希及发布时间；正常轮询不重复读取未变化正文，普通新草稿也不使公开缓存失效。发布新增、换版或同修订撤回后再发布则补读。待处理任务总量上限 1,000，过期任务实际读取前检查当前键，失败至少退避 60 秒，在后续请求中重试。派生边缓存按完整发布键及已完成特征集合失效；每个请求结束前重新读取发布快照，过滤已撤回/替换的节点、关系和旧版本证据，不缓存授权结果。

服务端每次最多纳入 1,000 份当前正式知识、返回 2,000 条边，响应预算小于 3 MiB；全库总数单独返回，截断明确标记 `truncated`。冷启动返回 `building` 并异步补齐；失败或容量截断展示 `partial`。门户可见、联网且已登录时，构建中每 2 秒串行读取进度并保留已有节点；`ready` 后停止快速轮询，稳定页面每 5 分钟刷新数据，恢复可见或联网也刷新。每 30 秒确认会话与权限，不因此重复请求图谱。这不是实时推送；进程重启将重新建立派生缓存。图谱模块**无新 schema 迁移、依赖、环境变量或处理 Worker 变更**，无需为图谱重跑收件或修改已有发布链路。页面交互及完整 API 字段见 [KNOWLEDGE_PORTAL.md](../web/visual-ingest/KNOWLEDGE_PORTAL.md)。

2026-10-09 本轮代码相关回归为 `191 passed, 1 skipped`。RAG 与 MCP 已完成部署，**线上只读验收通过**：北京时间 16:30 的[验收 JSON](/Users/xx/Documents/ChatGPT/数字资产/output/knowledge-wiki-20261009/live-acceptance.json)为 `ok=true`，8 项检查全部通过；149 项正式知识全部索引，0 项失败，0 条明确关系，68 条内容相关。读取 69 篇当前正式正文，逐一核对 136 段证据均为同修订原文连续子串；同时验证目录/图谱修订一致、边端点公开、非法查询与私有范围拒绝、重复读取稳定，以及退出后访问拒绝。完整部署与验收范围见 [Wiki 知识图谱上线验收](/Users/xx/Documents/ChatGPT/数字资产/Wiki知识图谱上线验收_20261009.md)。

本轮线上验收没有生产上传、生产发布或权限变更，未把真实员工新增资料送入链路以实测发布后的自动增量更新；该行为已有本地回归，仍需后续实际业务材料验收。可反查的摘录不等于推荐业务相关性已经逐条确认，词项相似也不证明依赖或事实关系。当前 0 条明确关系如实保留，不根据分类或相似词补造引用。

## 验证边界

本轮岗位链使用 `tests/test_knowledge_roles.py` 离线验证，包括真实收件/处理、同名跨部门岗位、Agent 绑定、未知岗位、跨岗相同正文、附件失败不发布、Skill 安装包和下载权限。`PYTHON_DOTENV_DISABLED=1`，测试只使用临时 SQLite 和注入的 OSS/索引替身，不读生产数据。

浏览器联测复用现有 `scripts/preview_knowledge_roles.py`。先构建 `web/visual-ingest` 前端，并使用能够导入本仓库及 MCP 仓库依赖的 Python 解释器。下例的 SOP 应是本轮“总经办／AI架构师”测试文档；证据路径必须单独指定，不能指向输入文档、现有数据库或其他需保留文件。

```sh
PYTHON_DOTENV_DISABLED=1 python3 scripts/preview_knowledge_roles.py \
  --sop '/absolute/path/总经办_AI架构师_岗位SOP.docx' \
  --mcp-root '/absolute/path/MCP' \
  --collector-root '/absolute/path/company-knowledge-collector' \
  --evidence '/absolute/path/local-role-preview/acceptance.json' \
  --port 19014
```

入口为 `http://127.0.0.1:19014/rag/knowledge`，终端显示仅供本地预览的测试登录信息。脚本清除云环境变量、禁用 dotenv、阻止非 loopback 网络，知识目录和门户身份库位于新临时目录；OSS 使用内存替身，检索和重排使用确定性本地替身。脚本调用真实 MCP 工具、收件和处理服务，并用实际 Collector 打包案例与 Skill。既有资料保护由新临时库内的固定样本验证，绝不连接真实旧库。生成的 acceptance.json 证明本地功能链，不证明真实 OSS 传输、云向量召回质量、生产部署或员工客户端验收。停止进程后临时库及内存对象不再可用，指定的验收 JSON 保留。

`tests/test_knowledge_processing.py` 使用真实 SQLite 事务、ZIP 解析、脱敏与草稿存储，OSS 接口使用替身。覆盖幂等/领取并发、租约过期写入拒绝、草稿提交后中断恢复、身份隔离、原件复验、清洗预览、缺件/敏感/脚本/非法 ZIP、尺寸限制和有界重试。真实 PostgreSQL 并发与真实 OSS 读写须另行在隔离环境验证，不能由本地替身测试推断。

旧测试中的 `judge-batch` dry-run 可能加载本机 `.env`；运行联合离线回归前必须禁用该加载并禁止真实外连，不能仅相信 `offline` 标记。截至 2026-10-08 用户要求暂停时，阶段 2 未进行生产部署或对象清理；后续按用户恢复指令与实施计划继续。
