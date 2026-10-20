# 员工端采集：阶段 4 小范围测试

## 2026-10-19 共用接入兼容修复：仅本地候选

本轮修复放在 RAG、MCP 服务端和处理详情页；不修改已交付的采集 Skill、员工定时提示词、MCP 地址或连接配置，不要求员工重新安装、重新创建任务、申请个人 profile 或补采历史。既有连续 15 天（360 小时）任务保持原任务 ID、周期、启停状态和采集进度；仍使用旧正文投稿、收件状态和知识检索工具。这里描述本地候选，不代表已经部署，也不证明员工任务已自然到时执行。

旧 `position` 和可选 `department` 保留原声明语义。服务端可将明确的原始“部门＋岗位”声明映射到标准岗位，或只为当前未导入条目确认岗位；两者不等于确认了真实员工身份。共用接入下不再把该 principal 的旧默认岗位自动套给后续所有资料。缺部门的旧请求仍被接受，但不能据此保存一个适用于所有同事的别名规则；没有足够岗位或来源依据时继续待核对，不能用模型猜测补齐。

员工本地保存的 `source_id`、投稿快照、检查点和收件 ID 继续复用。内部来源绑定由服务器保存，不能让旧 Agent 改编号来绕过冲突，也不依赖它理解新回执字段或自动改写本地岗位。无 `department` 或显式 `null` 的旧投稿仍生成相同原件字节、SHA256 和收件幂等键；回执 `source_id` 必须保持为客户端原提交值。任务每轮仍先续查旧收件，无合格新增跳过，读取失败不能记成无新增。

原声明与标准岗位分别记录：知识详情 `evidence.uploader_position` 及 `employee_declared` 来源继续反映原始投稿，`role_id/department/role_name` 表示正式目录归属。人工确认不得把目录名称伪装成员工原填写内容。未形成实际修订的待核对项返回 `knowledge_id=null, revision=null`；已有草稿、重复复用和正式发布继续返回实际知识 ID 与版本，状态含义不变。

这种兼容修复提供资料及岗位声明的区分，不提供个人身份认证、跨设备员工合并、丢失本地状态后的身份恢复，或自动把标准岗位写回员工的采集记录。两个同事使用相同声明、编号且来源关系无法确认时，仍需逐条确认。Skill 未变并不意味着任何旧客户端都已通过后台读取验收。

本地针对性回归见 `tests/test_knowledge_native_legacy_compat.py`，仅使用临时 SQLite、内存 OSS 和索引替身，默认禁止网络连接。服务端原接口兼容、任务原生读回、后台实际调用、收件、正式发布、正文和检索分别验收；截图或 Agent 自报 `ACTIVE / 360h` 不能代替真实平台运行记录。当前自然 15 天触发、真实员工客户端及生产迁移均不在本轮本地验收结论内。迁移与回滚约束见 [处理说明](KNOWLEDGE_PROCESSING.md)。

## 2026-10-16 岗位自动关联：本地候选，待确认部署

员工原有 `position` 声明继续使用，后台在既有岗位目录中按岗位名称做唯一匹配，写入正式 `role_id/department/role_name`；仅规整空白，不猜别名、不按正文适用对象归岗、不新建岗位。正文投稿可选传 `department` 消除同名歧义；省略或传 `null` 时仍生成与旧版相同的原件字节及收件幂等身份。已有 source-v2 包的“上传员工岗位”同样适用，无需修改旧包。

同一认证来源后续修订继承已经绑定的岗位，仍检查 `base_revision` 和写入权限。岗位声明与旧归属冲突时保留原归属，标为待确认；不能用不同员工身份、来源编号或过期版本覆盖旧稿。首次找不到岗位、同名无法消歧、填成部门或部门不符，也保留收件并说明具体原因，不把未归属稿自动发布。传统无有效岗位声明的旧导入保持原行为；已持久化的中断任务按冻结结果恢复，不因本次升级暗中补关联。

原收件状态接口的处理项增加可空的 `role_id/department/role_name`，供 Agent 与正式知识详情交叉核验；岗位声明读回不再等同正式目录归属验收。MCP 连接器候选版本为 1.6.0，工具 ID 与授权不变；Skill 候选版本为 0.3.2，仍只要求员工填写岗位和范围，沿用现有原生任务 ID、周期、启停状态和基线，不增加客户端程序。

本次不迁移历史资料、不写向量表。历史未归属资料单独生成带知识 ID、版本、原收件和目标岗位的只读建议清单；用户确认后才部署及处理线上数据。现有上线版仍以其部署记录为准。

历史补关联使用维护方库入口 `knowledge_role_maintenance.apply_role_assignment(store, plan)`，默认仅预览；实际写入要求条目 `decision=proposed`、`approved=true`，并明确传入 `execute=True` 和独占备份路径。仅支持当前发布版与最新修订均为第 1 版、原岗位全空、关联处理任务已完成的资料；版本、generation、双哈希、来源指纹及目标目录必须一致。事务内同步角色与派生哈希，保留原请求哈希、处理版本约束、正文、附件、向量和收件，写前保存快照。`rollback_role_assignment` 也默认预览，只有修改后状态没有再次变化时才允许撤销，generation 继续递增。该入口不暴露给员工 MCP，不创建任务，不自动读取或批准建议清单。本地演练通过不等于线上清单已经执行。

## 2026-10-16 当前交付：原生采集 0.3.1

当前员工包使用 WorkBuddy 或 Hermes 自带的历史/文件、MCP 和定时能力；员工填写岗位和采集范围，首次真实投稿验收后每 5 小时检查新增。无合格新增不上传。员工包不包含 Python、上传器、打包器或调度守护进程。下文早期脚本与测试记录仅为历史资料，不是新版安装步骤。

调用链为 `submit_company_knowledge` → `POST /api/knowledge-intake/text` → 原收件与处理 worker → 原目录发布；使用原 `get_company_knowledge_upload_status` 查询处理结果、原 `query_company_knowledge` 检索和读回。无需固定 MCP 连接名称，无需新增人员名单；已有上传权限继续适用。

正文端点在服务端生成确定性的 source-v2 原件包，使用原 OSS 前缀与同一收件/处理数据库。收件新增可信 `ingestion_source` 字段，旧记录默认为 `legacy`，只有该端点设置 `agent_collector`；客户端不能选来源或目标表。自动采集正文写入 `KNOWLEDGE_COLLECTOR_TABLE=company_knowledge_bt`，写入数据源必须显式配置 `KNOWLEDGE_COLLECTOR_PUSH_DATASOURCE=company_knowledge_bt`，错误不得回退原表。旧资料留在原表；原检索按已发布版本分表取候选，统一重排和拒答。

相同内容复用已发布知识，不新增版本、不改岗位。已有来源的修订不得换表；冲突进入 `needs_review/source_ingestion_source_conflict`，不反复重试。只上传正文不等于原 Word/PDF 附件二进制备份。正文上限 50,000 字符，无法完整读取或超限须留待处理，不能截断冒充完整资料。

首次投稿、自然到时、后台 MCP 调用分别验收。服务器端链路通过不能代替员工电脑真实每 5 小时执行证据；保留已有收件、基线和任务，不擅自恢复明确暂停的任务。

## 2026-10-14 历史脚本版岗位目录绑定（不用于原生版）

当前交付版 `company-knowledge-collector` 的配置可绑定 `role_id/department/role_name`；下文保留的 `scripts/knowledge_collector.py` 是早期试点脚本，两者不要混用命令。岗位目录由正式 SOP 的部门和岗位建立，Agent 先通过现有 `query_company_knowledge` 查询对应 SOP（可用 `category=sop`），读取返回的目录 ID，再对已存在 profile 执行交付包的 `knowledge_package.py --profile <profile.json> bind-role --role-id <目录ID> --department <部门> --role-name <岗位名称>`。不新增 MCP 工具或替换原连接权限。

绑定保留既有采集 baseline、冻结包和收件历史；旧包不得为了补岗位而改字节或重复上传。新包在既有 source-v2 清单后增加三个字段，由服务端核对目录；未知或不匹配岗位进入待处理，不能回退到猜测。员工自行声明的 `uploader_position` 继续作为上传归属快照，与资料绑定岗位独立。岗位名称相同但部门不同，必须选择相应 SOP 的真实 ID。

Skill 方法说明仍可作为知识阅读；可安装包需另附独立 ZIP，通过 `skill_package_path` 登记在收件包内的位置，并检查 SKILL.md 和显式包内依赖。完整安装包与方法说明在门户分别标识；本地依赖齐全不代表脚本已经运行或业务效果通过。完整规则、版本基线、原件 OSS 和离线联测入口见 [处理与岗位目录说明](KNOWLEDGE_PROCESSING.md)。

2026-10-09：在本人 Mac 的 Hermes 上做少量测试，三个授权目录为“数字资产、RAG、MCP”。用户随后授权线上完整执行，已验证单份联测说明从新增对话、原生定时采集到自动发布及 Hermes 实际查询；正式知识 148→149。没有启用长期采集或部署服务端新版本。

## 当前可运行范围

- `scripts/knowledge_collector.py`：本地 SQLite 检查点、清洗后消息、固定 ZIP 待传队列、状态与显式上传。首次启用仅登记基线，重复初始化保留原起点。
- `knowledge_collector_hermes.py`：只读 Hermes SQLite；同一读取事务检查旧消息元数据及新增正文。撤回、压缩、重写、fork、恢复旧会话或目录变化转为新基线并记录缺口，不补采历史。
- `knowledge_collector_sources.py`：Codex / WorkBuddy JSONL 增量适配，已有文件基线在 EOF。原始历史字节仅用于前缀哈希，不保留或交模型分析。已有 Codex 会话需要新的工作目录上下文后才纳入；重命名沿用同 inode 检查点，范围变化重新建立基线。这两种适配仅完成离线测试，未开通真实设备采集。
- `knowledge_collector_review.py`：复用本机 Hermes 现有模型配置，独立子进程、单轮、最多 4096 输出 tokens；禁用工具、目录上下文、记忆和会话持久化。只选择连续原文跨度和目录信息，保留 user / assistant 的原角色，不将模型概括当成执行或作者证据。
- `upload_knowledge_receipt.py`：保留原 Hermes 上传方式，新增官方 MCP SDK HTTP 方式；工具权限仍由现有 MCP 角色决定。当前 collector CLI 只接 Hermes 已配置的 `company-mcp`。

入口不依赖门户登录。没有创建账号、复制凭据、写死人员权限或开放管理员写入。

## 队列及失败行为

1. 只处理授权目录及启用后的新增 user / assistant 文本，排除工具、系统和推理字段；没有项目归属的会话不纳入。
2. 在落地前复用服务端文本清洗，并拦截额外常见凭据格式；命中内容只保存清洗后文本、转本地待处理，不交模型、不上传。模式检测不能保证发现所有语义敏感信息。
3. 清洗后的事件与对应游标在同一个 SQLite 事务提交，失败一起回滚。去重身份包含 Agent、会话和消息 ID。
4. 每次最多筛选一个会话、24,000 字符。超限、受限或不完整资料保留为待处理/等待后续材料，不截断后宣称完整。
5. 采用现有 `company-assets-source-v2` ZIP。原文保留角色、来源定位及“效果未独立核实”边界。默认生成包使用“测试待确认”共享范围；显式 `prepare --session-id <ID> --allow-company-sharing` 且原筛选结果为 company 时才能标记全员。uncertain 保持未确认，restricted 留本地；已冻结包不被新开关改写。服务端正常规则仍独立执行。
6. 包的固定字节与已打包消息先一起提交 SQLite，再生成本地文件。文件尚未写出时崩溃，可从队列恢复相同字节，无需重新调用模型；已有文件被改动则拒绝上传。
7. 同一包复用文件名、来源身份、哈希与幂等键。失败保留原包并计算退避时间，下一次显式执行上传时恢复；本轮没有后台重试任务。

状态分别为 `prepared`（本地已打包）、`retry`（待重试）、`received`（云端收件核验通过）、`rejected`。`received` 不代表已清洗、入库或正式发布；正式发布状态需要另查服务端处理结果。

本机私有队列：`~/.local/share/company-knowledge-pilot/collector.sqlite3`。目录 0700、数据库和包 0600（Mac）。保留待传内容，不自动删除。Windows 文件权限与调度仍需实机核查。

## 本机操作

以下只适用于已测试的这台 Mac，依赖现有 Hermes Python；尚未提供员工通用安装包或下载地址。

```sh
/Users/xx/.hermes/hermes-agent/venv/bin/python /Users/xx/ai/rag/scripts/knowledge_collector.py --home /Users/xx/.local/share/company-knowledge-pilot status
```

将末尾 `status` 换为 `collect` 可读一次增量；换为 `prepare` 可对一个已入队会话调用模型筛选并生成本地包。可用 `--session-id` 限定会话；公司共享须再显式加 `--allow-company-sharing`。`upload` 默认只显示状态，只有 `upload --execute` 才调用现有 MCP/OSS 上传，可再加 `--package-id <ID> --wait-seconds 30` 只传指定包；等待范围 0–60 秒，默认 5 秒。不要用删除队列或修改启用时间来“重试”。

原始初始化使用 `init --root <授权目录>`；CLI 暂不支持调整来源或采集周期。状态中的 `interval_days=15` 仅记录计划默认值，**没有生效的半月任务**；`scheduled=false` 表示 collector 没有配置持续调度。

## 已验证与未验证

- 离线 202 项通过：采集与审阅 71 项、上传客户端 41 项，加原入库及阶段 3 规则兼容检查 90 项。覆盖回滚、同 ID 跨会话、脱敏、固定包重试、物化中断恢复、篡改拒绝和测试包禁止自动发布。
- 原生 Hermes 基线读取成功，初始新增数 0。
- 真实本机 Hermes 模型对一份明确的合成方法完成筛选，返回 1 项 `uncertain`，未上传。
- Hermes 原生一次性 `no_agent` 脚本在 2026-10-09 14:29:22（上海时间）实际触发，新增数 0；任务 `74ee9962aee0` 执行后已从任务列表移除。
- 后续线上完整执行：原生 Hermes 新会话 2 条消息 → 原生一次性任务 → 1 个 3,882 字节包 → OSS 原件 verified → RAG 自动发布 1 项知识；Hermes 实际搜索与全文调用均成功。源包、门户、MCP 正文 4,588 字节及哈希一致，自然语言检索第 1；同包重试返回原收件，无新增知识。该样本是明确共享的联测方法，不代表真实业务资料质量验收。相关回归更新为 218 项。
- 未验证：真实员工业务资料质量、真实上传中断恢复、Windows 实机、Codex / WorkBuddy 原生定时触发、全员安装、半月持续运行、权限撤销与离职接管。

离线传输测试使用 MockTransport/FakeBucket，并不等于实际网络中断验收。完整阶段 4 仍需安装入口、周期修改、暂停/恢复、升级、真实设备验证及处理结果查询。老板看板和图谱属于后续阶段，本轮未改动。

线上证据保存在数字资产工作区 `output/knowledge-phase4-live-20261009/final-acceptance.json`。此次没有增加服务端工具或权限，采集端当前仍只知道 received；发布结果由单独的本人门户查询及 MCP 正文核验取得，尚待产品化回传。
