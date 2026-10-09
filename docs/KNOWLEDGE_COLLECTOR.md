# 员工端采集：阶段 4 小范围测试

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
