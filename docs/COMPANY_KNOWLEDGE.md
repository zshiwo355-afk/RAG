# 公司知识：存储、发布、全文检索与撤回

公司知识正式入库使用独立的 `company_knowledge_hybrid` OpenSearch 表，沿用现有 embedding；保留旧 `company_knowledge` 表兼容已有数据。目录使用 PostgreSQL，完整正文使用私有 OSS；SQLite/inline 模式仅用于开发与已有数据兼容。白酒产品接口和表保持原样。

公司知识检索现已接入文本向量＋标题/正文关键词两路召回、资产级 RRF 融合和模型重排，不调用图片检索。关键词查询要求全文索引，不能在旧纯向量表上直接启用：2026-09-28 的增量联调采用独立 `company_knowledge_hybrid` 表，保留旧表数据。设置 `KNOWLEDGE_TABLE=company_knowledge_hybrid` 和对应的 `KNOWLEDGE_PUSH_DATASOURCE=<实例ID>_company_knowledge_hybrid`；查询表和写入数据源必须成对匹配，不会自动切表。新表回填并验收前不要切换已有服务配置。

## 存储与资产身份

- PostgreSQL `knowledge_assets`：稳定 `knowledge_id`、最新版本、正式发布版本、变更代次及发布状态。
- PostgreSQL `knowledge_revisions`：不可变版本的元数据、来源、证据、正文哈希、私有 OSS 引用，以及每版审核归属。
- OSS：清洗后的完整 UTF-8 正文，以 SHA-256 内容寻址；同正文可复用对象。上传时明确设置 private，读取核对对象 ACL、长度和哈希。不返回对象地址或签名下载链接。
- OpenSearch：检索片段及向量；`knowledge_id + revision + chunk_id` 关联回完整资产。

一项 Skill、案例或模板沿用稳定 ID；正文或元数据变更产生新 revision，发布前旧正式版本继续可读。来源包名和提供者自己的 KA 编号不能单独作为全局 ID。本阶段仍由维护者指定稳定 ID，不提供自动跨包实体合并。

目录导入接收整理好的 MD/TXT 完整正文。另有下文的本地 ZIP 批量收件入口，支持文本、DOCX、XLSX、文本 PDF 解析及候选判定；图片 OCR、云端原始包归档、网页收件界面尚未实现，不把正文存储误称为全格式原件归档。

## 服务器配置与建表

在服务器配置 `KNOWLEDGE_DATABASE_URL`，仅支持 `postgresql://` 或 `postgres://`。连接凭证只放运行环境。数据库服务和数据库本身由服务器部署准备，本 CLI 只在指定数据库内初始化两张目录表。

```dotenv
KNOWLEDGE_DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/DATABASE
KNOWLEDGE_BODY_STORAGE=oss
KNOWLEDGE_TABLE=company_knowledge_hybrid
KNOWLEDGE_PUSH_DATASOURCE=YOUR_INSTANCE_ID_company_knowledge_hybrid
KNOWLEDGE_OSS_PREFIX=company-knowledge/bodies/
```

继续使用现有 `DASHSCOPE_API_KEY`、`OPENSEARCH_*` 与 `OSS_*` 连接配置。可用 `KNOWLEDGE_OSS_ENDPOINT`、`KNOWLEDGE_OSS_BUCKET`、`KNOWLEDGE_OSS_ACCESS_KEY_ID`、`KNOWLEDGE_OSS_ACCESS_KEY_SECRET` 分别覆盖知识正文配置。裸 OSS 主机名转为 HTTPS；显式 HTTP 被拒绝。知识正文不使用产品图片的 `OSS_PUBLIC_BASE_URL`。

```bash
python3 -m pip install -r requirements.txt
python3 scripts/knowledge.py init-db
python3 scripts/knowledge.py init-db --execute
```

第一次仅预览，第二次才建表。已有记录不覆盖。API 启动与读取不自动创建 PostgreSQL 表；未初始化时维护写入拒绝执行，不能静默改用 SQLite。PostgreSQL 对同一资产的导入、发布、撤回使用事务级锁，并保留 generation 检查。

未配置数据库 URL 时，本地目录由 `KNOWLEDGE_DATA_DIR` 指定，默认 `output/company_knowledge/knowledge.sqlite3`。本地默认正文 inline；可显式 `KNOWLEDGE_BODY_STORAGE=oss` 测试私有对象存储。原有本地数据库不会自动迁移到 PostgreSQL；若存在历史记录，迁移须保留 ID、版本、时间、审核记录、generation 和正式指针，不能逐条重新 import 冒充无损迁移。

## OpenSearch 独立表

字段与索引导入文件：[company_knowledge_opensearch.json](company_knowledge_opensearch.json)。只用于新建公司知识表，不导入到产品表，不对已有产品表重建索引。

| 字段 | 配置 |
|---|---|
| id | STRING，主键 |
| source_text_vector | 多值 FLOAT，1024 维，向量索引同名，InnerProduct |
| knowledge_id | STRING，稳定资产 ID |
| revision | INT64，版本 |
| revision_key | STRING，索引及过滤属性，例如 `review-method__r1` |
| chunk_id | STRING，与主键一致 |
| chunk_index | INT64，分片序号 |
| title | STRING，原始显示标题，最多 240 字符 |
| source_text | STRING，原始检索片段，参与哈希核验 |
| title_terms | TEXT，title 的分词副本，chn_standard，独立全文索引 |
| text_terms | TEXT，source_text 的分词副本，chn_standard，独立全文索引 |
| content_hash | STRING，片段 SHA-256 |

向量模型沿用 `text-embedding-v4`，由应用生成并推送。全量数据来源选 API，无需在表中另配文本 embedding。表名用于查询，推送数据源名称用于写入；部署时按控制台实际名称核对，不能只凭相似名称替换。

显示和哈希使用 STRING 原字段（保留 attributes 和 summaries）；关键词查询使用 TEXT 副本（不放 attributes，不作为原文返回）。真实测试发现 TEXT 字段在处理非 BMP 字符时会丢失部分 emoji，即使 summary 也不能代替原文，因此必须将分词副本与原文分开。当前 JSON 默认定义 `company_knowledge_hybrid` 新表，不是对既有表的安全增量迁移脚本。已有 API 数据表不要直接重建，先完成全量恢复/备份或在新表回填核对，避免历史数据丢失。

[阿里云 API 数据源文档](https://help.aliyun.com/zh/open-search/vector-search-edition/api-dataseouce)说明字段和索引配置流程；API 数据源的重建可能清除之前推送的数据，本项目 CLI 不执行重建。

## 导入草稿与元数据

```bash
python3 scripts/knowledge.py import \
  --file /path/to/已整理方法.md \
  --id decision-review \
  --title "决策复盘方法" \
  --contributor "材料贡献人" \
  --department "品牌部" \
  --scenario "方案评审" --scenario "复盘" \
  --source-name "选定工作会话" \
  --source-locator "方案取舍部分"
```

默认 dry-run，仅验证输入，不建库、不传 OSS、不调用模型、不写索引。加 `--execute` 保存待审草稿。正文上限 500000 字符；不自动下载 URL，不执行 Skill 或脚本。

可选 `--metadata metadata.json` 接受 `title`、`contributor`、`kind`、`sources`、`evidence`、`department`、`scenarios`。CLI 显式参数覆盖同名元数据；重复 `--scenario` 会替换整个 scenarios 列表。

```json
{
  "kind": "method",
  "department": "品牌部",
  "scenarios": ["方案评审", "复盘"],
  "sources": [{"name": "工作会话", "kind": "conversation", "locator": "方案取舍部分"}],
  "evidence": {"proposed_by": "本人和 AI", "adopted": "待确认", "executed": "未取得", "validated": "未取得"}
}
```

部门/场景是检索标签，不是读取权限。未知部门不猜测；跨部门内容不用复制多份。证据可登记未知状态，系统不替代内容事实核验。来源仅接受 name/locator/kind/url，拒绝本地绝对路径和带凭证的 URL。原始附件路径不进入共享 API。

## 草稿向量准备、发布与撤回

可先向量化已导入的草稿，不提前发布：

```bash
python3 scripts/knowledge.py index-draft --id decision-review --revision 1
python3 scripts/knowledge.py index-draft --id decision-review --revision 1 --execute
```

默认只预览；`--execute` 才调用 embedding、写入并验证索引，成功返回 `indexed_draft` 和 `published=false`。不会修改审核人、发布指针或版本状态。草稿片段与正式片段共用知识表，但检索只允许目录中已发布的版本；这不是一个独立草稿表。索引准备期间发生版本变动会报告错误。之后发布仍重新验证，不把一次准备成功当永久有效。

```bash
python3 scripts/knowledge.py list
python3 scripts/knowledge.py export --id decision-review
python3 scripts/knowledge.py publish --id decision-review --revision 1 --confirmed-by "确认人"
python3 scripts/knowledge.py publish --id decision-review --revision 1 --confirmed-by "确认人" --execute
python3 scripts/knowledge.py withdraw --id decision-review --execute
```

发布：读取不可变正文 → embedding 与分片推送 → 逐块取回比对 → 限定该版本召回验证 → 事务切换正式指针。写入或验证失败不替换旧版；期间发生导入/撤回/发布时，旧 generation 不得覆盖新状态。

OSS 上传和校验先于目录写入；失败不新增版本。对象已传但目录事务失败时，可能留下无引用对象，不影响正式版本；本轮不做自动垃圾回收。

撤回先清除目录中的发布指针，搜索及全文读取立即按目录拒绝。历史对象和云端片段保留，不把撤回等同物理删除。维护导出支持 `--revision`，共享正文接口只允许当前正式版本。

## 查询：命中片段，默认返回完整资产

公司知识直接检索使用 `POST /api/knowledge/search`，按 ID 取全文使用 `GET /api/knowledge/{knowledge_id}`。这两个只读接口不要求 API Key，不生成回答。原 `/api/rag/search` 和 `/api/rag/answer` 仍服务产品库，不能替代公司知识接口。

上述说明针对只读检索接口；服务器实际访问范围由其运行版本、反向代理和网络边界决定。新增收件与处理接口使用独立开关、服务端认证和调用方权限，见[收件说明](KNOWLEDGE_RECEIPTS.md)及[处理说明](KNOWLEDGE_PROCESSING.md)。

```bash
curl -X POST http://127.0.0.1:8000/api/knowledge/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"做方案复盘要检查什么？","top_k":3,"department":"品牌部","scenario":"复盘"}'

curl 'http://127.0.0.1:8000/api/knowledge/decision-review?revision=1'
```

搜索默认 `include_content=true`。每条结果包含稳定 ID、版本、title、完整 content、正文 hash、来源/证据、部门/场景（如登记）、发布信息，并保留命中 snippet、chunk_id、score、read_url。相同父资产多个命中只返回一次，完整正文直接从相同发布版本读取，不拼接重叠片段。

两路均在云查询前使用已发布版本 allowlist。各路跨过滤批次汇总后，先按父资产及版本聚合，以最佳片段分数给资产排序，不直接混加关键词分数与向量分数。同分资产共享平均名次，每个资产每路最多保留三个片段；同分截断时优先保留向量分更高的候选，最后按 ID 稳定输出，ID 不产生相关性分。资产 RRF 为各路 `1 / (60 + rank)` 之和；重复片段不累计加分。取融合前 50 个资产，使用标题及最多三个不同命中片段调用已有 `gte-rerank-v2` 适配器，成功后按真实重排分数排序。返回 `retrieval_routes`、`rrf_score`、`rerank_score` 和 `rerank_mode`。重排失败或响应无效时明确返回 `fallback_rrf`，`rerank_score=null`；不会把融合分数冒充模型分数。关键词索引不存在或任一路云查询失败时返回 503，不静默退回单路。

当前实例实测关键词原始分数均为 0，即使显式设置权重为 1 也相同。因此关键词只作为匹配信号，使用并列名次参与融合，不能声称 BM25 评分已启用。[当前混合查询 API](https://help.aliyun.com/zh/open-search/vector-search-edition/inverted-query)公开了分词、过滤、权重和 RRF 参数，未公开 BM25 开关；另一套引擎的排序表达式不能直接套用。零分候选不能全部当作第 1 名，例如 100 个同分候选共享名次 50.5，避免集体压过向量候选。

重排前校验原文片段与目录正文哈希，并复核发布版本；重排后再复核一次。返回的完整正文仍是同一资产和版本。重排不是“资料库无答案”的判定器，本轮未设置未经样例校准的分数阈值。

候选全文读取最多 4 路并发，避免逐份串行访问 PostgreSQL／OSS；不缓存已撤回正文，不跳过哈希或前后版本检查。同一资产的多个片段共用一次正文读取，重排输入优先保留最相关的完整命中片段。

需要列表预览时显式设置 `include_content=false`。read_url 绑定 `?revision=N`，旧链接不会自动换成新版；没有 revision 时读取当前正式版。草稿、撤回、非当前版本返回 404。全文缺失、哈希不符或对象存储故障返回 503，不退化成残缺正文。

department/scenario 是可选的精确标签筛选（去首尾空白，区分大小写），在检索前筛出正式版本，查询结束再次核对。缺字段的条目不会匹配指定标签；不传筛选字段时检索全部已发布知识。模型或索引故障不改查白酒产品表。

接口参数边界：query 1–4000 字符，top_k 1–30 的整数，include_content 严格布尔值，筛选标签 1–200 字符；拒绝调用者传入表名或任意过滤表达式。不会静默截断完整正文，调用者应按正文长度选择 top_k，必要时使用轻量检索再逐项读取。

## 访问与规模边界

- 两个公司知识读取接口均无应用层鉴权，能访问入口的调用方可读取全部已发布知识。草稿、撤回和旧版本仍由发布目录过滤；这两个读取接口不执行写入；手工维护仍通过 CLI，新增收件与处理接口另行认证和授权。
- MCP 调用上述检索/全文接口，不调用生成回答接口；MCP 自身已有账号、工具权限和审计规则保持原样。
- 当前每 32 个允许版本分别发起向量和关键词查询，查询 embedding 仅一次。先保留这条安全过滤；大目录的吞吐优化须另做真实规模测试，不能删除 allowlist 后让草稿/旧片段挤占召回。
- top_k 是最多返回的资产数量，不保证填满。应用层已对返回的片段按父资产去重，但云端每批固定窗口仍可能被长文档片段占用；不能将后处理描述为保证全量召回，目标规模验收后再决定是否扩大窗口或拆分批次。未登记切片版本的旧记录保持原 900 字符/120 重叠算法；已有 `structure-v1` 记录保持原结果，新批量收件使用 `structure-v2`。切片策略变化产生新草稿版本，不改变已有发布版本的校验规则。
- 未集成 Jev/Laya，不执行资料里的业务脚本。员工端增量采集和私有 OSS 原件上传属于独立模块，见[采集说明](KNOWLEDGE_COLLECTOR.md)与[收件说明](KNOWLEDGE_RECEIPTS.md)；员工持续调度和跨平台实机验收的边界以相应文档为准。

## 验证

```bash
KNOWLEDGE_DATABASE_URL='' KNOWLEDGE_BODY_STORAGE=inline \
KNOWLEDGE_TEST_CLOUD=0 python3 -m pytest -q -m 'not integration'
```

离线验证覆盖 ID/版本幂等、OSS 私有对象与哈希、异常不提交目录、全文返回、部门场景筛选、旧链接/撤回保护、并发发布保护及原产品接口。真实依赖联调也可从本机执行，不要求先部署服务器；离线通过不能代替真实联调或部署验收。

真实 PostgreSQL 检查须预先设置 `KNOWLEDGE_TEST_DATABASE_URL`，指向允许创建、删除临时 schema 的独立测试数据库，不使用正式目录。Python 环境须安装 requirements.txt 中声明的 psycopg 依赖：

```bash
KNOWLEDGE_DATABASE_URL='' KNOWLEDGE_BODY_STORAGE=inline \
KNOWLEDGE_TEST_CLOUD=0 python3 -m pytest -q tests/test_knowledge_postgres.py
```

完整链路另须有效的私有 OSS、embedding、OpenSearch 配置，并明确启用云测试。推送数据源必须与公司知识表匹配，例如（将 YOUR_INSTANCE_ID 替换为目标实例ID）：

```bash
KNOWLEDGE_DATABASE_URL='' KNOWLEDGE_BODY_STORAGE=inline KNOWLEDGE_TEST_CLOUD=1 \
KNOWLEDGE_TABLE=company_knowledge_hybrid \
KNOWLEDGE_PUSH_DATASOURCE=YOUR_INSTANCE_ID_company_knowledge_hybrid \
python3 -m pytest -q tests/test_knowledge_integration.py
```

缺少测试数据库或云测试开关时默认跳过。三个命令分别启动独立进程；前缀隔离本地测试可能继承的正式数据库和正文存储配置。完整联调显式注入真实测试 PG 连接与 OSS 对象，因此命令中的 inline 不会跳过真实 OSS 验证。完整测试仅生成两版合成正文，使用随机知识 ID、schema 和 `company-knowledge/tests/<run_id>/` 对象前缀；验证草稿不可见、PG 仅保存正文引用、OSS 私有及匿名拒绝、命中片段返回同版本全文、版本替换、服务重建后读取和撤回。首次写入前保存 `output/knowledge_integration/<run_id>/cleanup_manifest.json`（可用 `KNOWLEDGE_TEST_REPORT_DIR` 改父目录），逐项预检目标不存在；403 等预检失败时不写入、不删除。

测试结束精确删除本次切片、记录到的本次 OSS 对象版本及临时 schema，核对 Fetch、Query、OSS 和 PG 均无残留。报告分别保留主失败与清理失败，不保存密钥或 SDK 原始异常文本。进程被强制终止时 finally 无法保证执行，应按清理清单核对；清理失败不能视作测试完成。此测试不修改正式知识目录、不发布同事候选资料，也不等同于服务器部署。

## 批量收件、判断与切片

在同一个项目使用 `scripts/knowledge.py` 的三个新命令。第一版是可在后台服务器运行的 CLI 和本地检查点，不新增队列平台，也没有网页上传界面。

输入清单示例（仅运行环境内保存路径，不进入共享来源字段）：

```json
{"sources": [
  {"path": "/receipts/同事A首轮.zip", "source_id": "receipt-source-a", "contributor": "未确认"},
  {"path": "/receipts/同事A补充.zip", "source_id": "receipt-source-a", "contributor": "未确认"},
  {"path": "/receipts/同事B.zip", "source_id": "receipt-source-b", "contributor": "未确认"}
]}
```

`source_id` 是确认过的收件来源标识，不根据部门或 KA 编号推测身份。同源资料更新沿用该 ID；资产 ID 由来源标识和包内相对路径确定，内容指纹只识别候选版本。文件改名、跨提供者共创的归并仍需明确映射，不能自动合并。格式来源明确但无 BOM 的旧文本可在对应 source 下用 `text_encodings` 按完整包内文件名指定 `gb18030` 等编码，结果保留 `explicit_encoding`，默认不猜编码。

```bash
# 只在新的本地目录产出候选，不初始化数据库、不调用模型、不上传。
python3 scripts/knowledge.py intake --manifest receipts.json --out output/intake/batch-01
# 同样的来源、指纹、规则版本及审阅输入可续跑；已成功的解析复用缓存。
python3 scripts/knowledge.py intake --manifest receipts.json --out output/intake/batch-01 --resume
# 可选：接入已对照原文的 AI 审阅建议，不代表人工批准。
python3 scripts/knowledge.py intake --manifest receipts.json --reviews source_reviews.jsonl --out output/intake/batch-02

# 预览模型任务数量；加 --execute 才将规则清洗正文发往现有百炼回答接口。
python3 scripts/knowledge.py judge-batch --batch output/intake/batch-01
python3 scripts/knowledge.py judge-batch --batch output/intake/batch-01 --execute
# 先预览，再选择是否导入符合条件的候选为草稿；不发布、不生成向量。
python3 scripts/knowledge.py import-batch --batch output/intake/batch-01
python3 scripts/knowledge.py import-batch --batch output/intake/batch-01 --execute
```

### 各阶段实际做什么

- **读取与登记**：检查 ZIP 路径、符号链接、重复路径、大小上限；原包不修改，不执行脚本，不读取凭证文件。拒收文件、脚本、图片待 OCR、解析失败均记录去向。DOCX 保留段落/表格，XLSX 保留工作表、单元格位置、公式/缓存值区别和数字格式；PDF 按页提取，空页标注待 OCR。未支持内容或超限不冒充已完整取得。
- **清洗**：统一 BOM/行尾；对可识别的凭证、账号、电话、证件、邮箱做稳定占位。正文数字、否定、失败反馈、单位、缩进不普遍删除；普通 URL 内数字不当电话处理。占位符是用于对应关系的假名，不代表不可还原的匿名化或完整隐私审核。语义纠错与冲突修复仍列为待办，不擅自把未知结果改写成成功。
- **完整资产**：正文与独立 Skill 分别枚举；标准 Skill 目录的方法 references/templates 加入完整父正文，扁平导出仅按可确定的引用归并。明确命名的案例库、榜单和视频档案拆成关联参考资产；其中编号和标题边界明确的案例各自保留完整正文、共同阅读说明、原文位置及来源哈希。目录、全库索引及年度总结留在库概览，不复制到每个案例。边界含糊或编号冲突时保留整份并标待审，不按长度猜分。原组合全文另存本地 `assets/*.full.md` 追溯副本，不提高正文上限或截断内容。派生案例与概览保持待审，不继承主 Skill 的判定；关联 ID 随 evidence 保存，不代表关联内容已获准公开。脚本只登记依赖，不作可执行交付。一个 KA 附件目录有多个 Skill 时分别保留；未归属附件不丢弃。
- **去重与版本**：同来源、同路径、同正文及附件指纹合并并保留来源；相同资产不同版本并列待审，不按日期自动选采用版。不同来源相同正文只标疑似重复。原包同事与资产作者分别记录，未知不猜。
- **判断**：收到的审阅 JSONL 必须匹配源文件 SHA-256，且引文确实出现在对应清洗原文。自动判断复用现有百炼回答 API，模型只提供价值、完整性、证据、共享风险的分流建议。逐段覆盖全篇，不截取开头或近期内容；每段最多 6000 字符，缓存含提示词、模型和文本指纹。失败进入待复核并可重跑，绝不自动判无用；跨段冲突和结论仍需整体复核。尚未实测 Jev/Laya，也不将这套分流声称为事实认证。
- **切片**：完整父正文先保存；`structure-v2` 按 ATX 标题、段落、句子、围栏代码和管道表格切分，最长 900 字符，硬切长块才需要最多 120 字符重叠，长表续片重复表头。纯标题附着到后续实质内容，独立分隔线不生成检索片段；完整父正文不删减。预览含原文位置、章节、顺序和父资产 ID，向量输入带资产名称、章节路径及片段正文；不增加云表字段。其他 Markdown 结构按普通文本保留；超过正文上限的完整资产保留本地但阻止导入，不自动截断。
- **导入**：只选择 `candidate` 且通过完整性检查的条目导入草稿。模型建议需校验正文指纹、全部窗口覆盖和真实引文。冲突版本、引用未解决、解析截断不进入自动草稿导入。`repair/method_only/archive/review_required` 保留在批次中等待整理；共享仍必须走原有明确发布确认。

### 产物与边界

`报告.md` 提供候选全文入口；`batch.json` 记录每个文件、异常、来源、ID和版本关系；`assets/` 是完整规则清洗正文与可导入元数据；`assets/*.full.md` 保存拆分前完整组合及案例库副本；`chunks/` 是切片预览；`cache/` 保存解析副本供恢复；`model_reviews.json` 保存模型建议及逐段依据。预览没有正式 revision，目录导入时才分配版本。

这些目录都是内部待审材料，可能仍包含需业务核验或共享审核的信息，不作为静态网站或公开文件目录。不要手工覆盖生成文件再沿用旧指纹；修订正文应走新的草稿导入。程序不自动修复矛盾的业务结论，不承诺把所有 Skill 变成无需依赖即可执行的技能包。

当前批处理顺序执行，有逐文件和逐段检查点。先以实际误判率、覆盖率和耗时验收；需要并行时再加有界工作进程，不在首版引入额外队列服务。

### 验证范围

离线检查覆盖解析、切片全文覆盖、引用关系、输入异常、版本绑定、撤回、并发和失败边界。真实联调另行验证 PostgreSQL、OSS、embedding、向量写入及查询；测试只操作随机标识的隔离数据，并精确清理。具体批次、机器配置排查过程及私有资源标识保留在本地忽略的验收目录，不随代码提交。

结构切分同时保留独立方法、必要引用和完整包副本；超大案例库按明确来源边界拆成概览与独立案例，不能把截短后的主文当完整交付。切片算法升级需保持旧版本的重算结果，避免已有向量与父正文校验不一致。

## 正式批次入库与服务器迁移（2026-09-28）

已增加 `company-assets-source-v2` 的 `交付清单.csv` 适配。固定十列表头、唯一资产号及正文路径、相对路径安全、正文存在/可解析、合并关系均须通过校验；仅“交付”正文建立候选，待补/排除/合并保留处置记录。来源 manifest 可提供 department；contributor 未知时保留“未确认”。source_id 是永久来源命名空间，不应将全体同部门人员合为一个来源。

`Skill方法`按方法参考入库，不冒充完整Skill。默认缺失引用仍阻止导入；仅经正文hash及原文锚点验证的价值评审，可以用 `accepted_missing_references` 精确枚举全部缺失引用，判断独立方法正文仍值得参考。此例外限定 source-v2 的Skill方法/candidate；保留缺失清单和 `source_complete=false`，不补写正文、不放行其他异常。代码块和inline code里的数学/索引表达式不会被误识别成Markdown附件链接。

复用已有命令，清洗、切分、向量化、上传和验收不调用生成式AI：

```sh
python scripts/knowledge.py intake --manifest sources.json --reviews value_reviews.jsonl --out batch
python scripts/knowledge.py import-batch --batch batch
python scripts/knowledge.py import-batch --batch batch --execute
python scripts/knowledge.py publish-batch --batch batch --confirmed-by '本次实际发布授权记录'
python scripts/knowledge.py publish-batch --batch batch --confirmed-by '本次实际发布授权记录' --execute
```

`import.json`逐条记录固定ID、revision、正文hash、payload hash和generation；`publication.json`保存逐条发布结果及云端验证回执。发布必须PostgreSQL+OSS。相同已发布版本续跑不重新embedding/推送；更新、撤回、回执与输入不符时拒绝执行。批次首次导入同ID不同payload也会在数据库事务锁内拒绝，防止无意修改旧资产；明确的新版本更新继续使用单条import/publish流程。批次不是全有或全无事务，失败时保留完成项并按回执续跑。

首个经授权的正式批次：5份全文、55片已发布，另外7份正文暂缓。10个固定标题/业务查询的目标均Top1；5份独立关键词召回、同版本全文返回及幂等重试通过。这不是大规模压力测试；无关问题仍可能返回相似候选，尚未设置无答案阈值。

本地正式目录使用PostgreSQL持久卷，与既有试用目录隔离。原试用396条记录及两个知识表中各2995个既有切片字段指纹前后相同。完整目录dump、恢复说明已私有备份到OSS，并恢复到新建隔离数据库验证了两表指纹、5份全文和真实查询；验证库已移除。

迁移服务器必须同时完成以下步骤，不能只复制代码或重跑ZIP：

1. 下载并按SHA256核验私有目录dump；在新的空PostgreSQL数据库恢复。保留两张表的全部行、revision、generation、发布指针及历史审核字段。
2. 使用原来的OSS bucket和 `KNOWLEDGE_OSS_PREFIX`、`company_knowledge_hybrid` 表和匹配数据源；更新为服务器的 `KNOWLEDGE_DATABASE_URL`，设置 `KNOWLEDGE_BODY_STORAGE=oss`。凭证通过服务环境配置，不进入版本库。
3. 可用原生 `pg_restore --exit-on-error --single-transaction --no-owner --no-privileges --dbname company_knowledge catalogue.dump`，认证由服务器环境配置。恢复目标必须为空，不能用清库选项覆盖现有服务器数据。
4. 核对目录数量/全行指纹、正文hash、同版本完整返回与真实查询，再切换客户端地址。既有云正文和向量继续使用，无需重新向量化。

当前本地正式API为 `http://127.0.0.1:8790/docs`；本轮未部署到服务器。批次、备份和验证材料在 `output/company_knowledge_production/`；其中带private名称的运行配置含本机连接信息，不应分享或提交。发布回执、restore manifest不包含密钥。最新离线检查602项通过；旧测试加载真实环境的隔离缺陷已修复，本轮误写的单条未发布测试草稿及对应新建OSS对象已精确清理。
