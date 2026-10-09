# 私有原件可靠收件

本阶段实现 MCP 申请上传 → 客户端直传 OSS → 确认收件 → 后台流式核验 → 查询状态。
`verified` 只表示原件长度和 SHA-256 已通过服务端核验；它不表示完成清洗、筛选、知识入库或发布。
现有 `/api/knowledge/search`、正文查询、知识表和发布流程继续独立工作。

## 部署前配置

功能默认关闭，关闭时所有收件 HTTP 接口返回 503。以下配置只放服务器环境，不提交仓库或写入日志。

| 配置 | 要求 |
| --- | --- |
| `KNOWLEDGE_RECEIPTS_ENABLED` | 明确设为 `1` 才开放收件接口和 CLI 校验 worker |
| `KNOWLEDGE_RECEIPTS_TOKEN` | 专用内部 Bearer，至少 32 字节，不复用员工 PAT 或旧知识查询密钥 |
| `KNOWLEDGE_DATABASE_URL` | 现有 PostgreSQL 连接；新增独立 `knowledge_upload_receipts` 表，不修改旧知识表 |
| `KNOWLEDGE_RECEIPTS_OSS_ENDPOINT` | 公网 HTTPS OSS endpoint，例如 `https://oss-cn-hangzhou.aliyuncs.com` |
| `KNOWLEDGE_RECEIPTS_OSS_REGION` | 与 endpoint 一致，例如 `cn-hangzhou` |
| `KNOWLEDGE_RECEIPTS_OSS_BUCKET` | 新的独立私有 Bucket，不能等于已配置的旧 OSS Bucket |
| `KNOWLEDGE_RECEIPTS_OSS_ACCESS_KEY_ID` / `ACCESS_KEY_SECRET` | 显式收件配置；本次试点在服务器复用已有 RAG OSS 身份，不改旧配置。SECRET 全名同样以 `KNOWLEDGE_RECEIPTS_OSS_` 开头 |
| `KNOWLEDGE_RECEIPTS_MAX_BYTES` | 可选，默认 64 MiB，最大不能超过 512 MiB |

运行环境必须安装支持 `AuthV4` 和签名附加请求头的 `oss2`（本地验证版本为 2.19.1），使用已有依赖，无需新服务或消息队列。
OSS 配置不回退到 `OSS_*` 或 `KNOWLEDGE_OSS_*`。每次签发和核验均检查 Bucket 私有、版本控制未开启。
本阶段要求版本控制状态为未开启；`Enabled` 或 `Suspended` 都拒绝。原因是 OSS 在这两种状态下不会执行 `x-oss-forbid-overwrite` 的禁止覆盖约束。
参见 [阿里云版本控制说明](https://www.alibabacloud.com/help/en/oss/user-guide/overview-78/)。后续如果需要开启版本控制，应先实现固定 VersionId 的收件模型。

### 试点 Bucket 与 RAM 策略

本次试点已改为复用现有 RAG OSS 身份，在服务器内部复制到显式的 `KNOWLEDGE_RECEIPTS_OSS_*` 配置，不创建新角色或 AccessKey，不向客户端提供凭据。
当前复用身份已核实为 Account/root；因此下面的最小 RAM JSON 是将来收窄身份权限的配置参考，**不是当前身份的实际权限边界**，本次不附加该策略。签名上传仍限定固定对象键、大小、私有 ACL 和有效期。

RAG 服务器地域已核实为 `cn-hangzhou`。建议新建独立私有 Bucket `hr-knowledge-intake-20261008`，使用 `https://oss-cn-hangzhou.aliyuncs.com`；名称可用性仍以实际创建时为准。
此处是待配置方案，不表示 Bucket 或 RAM 授权已经创建。先只上传合成测试文件，不接入真实员工资料，不配置自动删除规则。
版本控制必须保持**从未开通**：官方 GetBucketVersioning 响应此时不包含 Status；不能用“曾开启后暂停”代替。因为已开启和已暂停都会使禁止覆盖上传头失效。
参见 [版本状态返回说明](https://help.aliyun.com/zh/oss/developer-reference/getbucketversioning) 和 [PutObject 禁止覆盖头说明](https://help.aliyun.com/zh/oss/developer-reference/putobject)。

为没有其他宽泛 OSS 权限的专用 RAM 身份配置以下自定义策略。仅授权该 Bucket 的两项只读检查，以及 `knowledge-receipts/raw/*` 范围内的读写/ACL 操作；写入必须显式指定 `private`。

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "oss:GetBucketAcl",
        "oss:GetBucketVersioning"
      ],
      "Resource": "acs:oss:*:*:hr-knowledge-intake-20261008"
    },
    {
      "Effect": "Allow",
      "Action": [
        "oss:GetObject",
        "oss:GetObjectAcl"
      ],
      "Resource": "acs:oss:*:*:hr-knowledge-intake-20261008/knowledge-receipts/raw/*"
    },
    {
      "Effect": "Allow",
      "Action": [
        "oss:PutObject",
        "oss:PutObjectAcl"
      ],
      "Resource": "acs:oss:*:*:hr-knowledge-intake-20261008/knowledge-receipts/raw/*",
      "Condition": {
        "StringEquals": {
          "oss:x-oss-object-acl": "private"
        }
      }
    }
  ]
}
```

API 与 RAM Action 已按阿里云官方资料核对：

| 本模块操作 | 策略 Action | 官方依据 |
| --- | --- | --- |
| `get_bucket_acl` | `oss:GetBucketAcl` | [GetBucketAcl](https://www.alibabacloud.com/help/en/oss/developer-reference/getbucketacl) |
| `get_bucket_versioning` | `oss:GetBucketVersioning` | [get-bucket-versioning 权限要求](https://help.aliyun.com/zh/oss/developer-reference/get-bucket-versioning) |
| 上传原件 | `oss:PutObject` | [PutObject 权限要求](https://help.aliyun.com/zh/oss/developer-reference/putobject) |
| HEAD 元数据、GET 流式核验 | `oss:GetObject` | [HeadObject 权限要求](https://help.aliyun.com/zh/oss/developer-reference/headobject)、[API 与 Action 对照](https://help.aliyun.com/zh/oss/user-guide/authorization-syntax-and-elements) |
| 读取对象 ACL | `oss:GetObjectAcl` | [API 与 Action 对照](https://help.aliyun.com/zh/oss/user-guide/authorization-syntax-and-elements) |
| 设置上传对象的私有 ACL | `oss:PutObjectAcl`，并以 `oss:x-oss-object-acl=private` 收紧 | [PutObjectACL 的内联 ACL 说明和 Action](https://help.aliyun.com/zh/oss/developer-reference/putobjectacl/)、[ACL 条件关键字](https://help.aliyun.com/zh/oss/user-guide/authorization-syntax-and-elements) |

ACL 权限边界：官方 PutObjectACL 文档说明，PutObject 携带 `x-oss-object-acl` 可在上传时设置 ACL，效果等同 PutObjectACL；上面的策略据此显式保留 `oss:PutObjectAcl`，且仅允许设为私有。当前 PutObject 的权限表没有单独列出这一内联场景的附加鉴权要求，因此这里不声称已经证实它在所有配置下都必需。真实合成上传应核对这项授权；本模块不单独调用 PutObjectACL，也不借此把对象改为公开。若要进一步减少这一 Action，应先在隔离试点中验证移除后仍能完成带私有 ACL 的签名 PUT，再调整策略。

此策略不允许读取旧 Bucket，也不授予删除、列举、生命周期修改、Bucket ACL 修改、版本控制修改或其他 Bucket 权限。它不覆盖其他策略的宽泛授权，因此专用身份不应叠加 `AliyunOSSFullAccess`。
上传程序只取得固定 key 的短时 PUT 签名，不取得 RAM 凭据或整套权限。不要把服务端 RAM 凭据发给 Hermes。
Bucket 应由管理员保持私有、阻止公共访问，并检查是否存在允许其他身份覆盖原件的策略。Bucket 策略、地域、对象权限和生命周期配置需要云端验收；本模块及此文档更新均不执行云上配置。

### 初始化收件表

在正式环境启用前，先完成备份的隔离恢复验证，再显式建表：

```sh
python3 scripts/knowledge_receipts.py init-db
python3 scripts/knowledge_receipts.py init-db --execute
```

第一条只输出 dry-run；第二条保留已有行并创建附加表和索引。HTTP 请求和 worker 都不会自动建表。
CLI 读取已导入进程环境的配置，不主动读取或展示 `.env`。生产应使用现有服务管理器注入环境。
仅本地开发允许 `--sqlite /absolute/path/test.db`；HTTP 开发环境必须同时明确配置 `KNOWLEDGE_RECEIPTS_DEVELOPMENT=1` 和 `KNOWLEDGE_RECEIPTS_SQLITE_PATH`。
生产不提供 SQLite 自动降级。

## HTTP 契约

三个入口都要求内部 `Authorization: Bearer ...`，通过后才接受 `X-Knowledge-Principal`。
MCP 必须从当前已认证主体获取这个值，不允许工具参数指定他人身份；RAG 对每个 receipt 再做主体归属检查。
错误主体查询或确认统一返回 404。新写通道必须在可信网络内使用 TLS，或通过已验证的安全隧道访问。
现有公开读取权限不能作为新写入口的授权依据。

申请：`POST /api/knowledge-intake/uploads`

```json
{
  "idempotency_key": "synthetic-session-001-sha256-1",
  "source_id": "hermes:synthetic-session-001",
  "filename": "sample.json",
  "byte_length": 42,
  "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
}
```

示例 SHA-256 是占位值，不是可通过校验的样本。`source_id` 在同一来源后续更新时保持稳定；`idempotency_key` 标识一次提交，重试保持不变，新内容使用新键。
同一主体、同一键、完全相同元数据返回同一个 receipt；同键不同内容或元数据返回 409，不覆盖。
不同主体互不共享收件记录，也不会因为相同文件泄露彼此的上传状态。
文件名只能是 basename，禁止路径或控制字符。请求只能包含以上五项，任何额外字段都被拒绝。

统一响应：

```json
{
  "ok": true,
  "receipt": {
    "receipt_id": "32位小写十六进制标识",
    "source_id": "hermes:synthetic-session-001",
    "filename": "sample.json",
    "byte_length": 42,
    "sha256": "64位小写十六进制",
    "status": "awaiting_upload",
    "content_verified": false,
    "published": false,
    "created_at": "UTC时间",
    "updated_at": "UTC时间",
    "error_code": null,
    "retryable": true
  },
  "upload": {
    "method": "PUT",
    "url": "短时签名地址，仅传递给上传程序，不写入日志",
    "headers": {"Content-Type": "application/octet-stream", "Content-Length": "42"},
    "expires_at": "UTC时间"
  }
}
```

实际 `headers` 还包括必须原样发送的私有 ACL、禁止覆盖、receipt ID 与 SHA-256 元数据头。
V4 签名额外绑定 Content-Length，全部 `x-oss-*` 头也参与签名。客户端将原始文件作为 PUT body 直接发到 OSS，不经过 RAG 的文件中转。
这里的签名方案依据 [阿里云 V4 签名 URL 文档](https://www.alibabacloud.com/help/en/oss/developer-reference/add-signatures-to-urls)。

签名默认 15 分钟有效。过期或上传结果不确定时，用同一幂等键重新申请；固定 key 保持不变，已存在的对象不可覆盖。
如果 PUT 返回已经存在，调用 complete 交给服务端核验，不自行宣称上传成功。
同键处于后续状态时 `upload=null`；拒绝的对象需要修正内容并以新键提交。

确认：`POST /api/knowledge-intake/uploads/{receipt_id}/complete`，请求体 `{}`。
检查私有 ACL、长度、对象元数据并固定 ETag 后，状态变为 `pending_verification`，此时 `content_verified=false`。
未找到对象返回 `upload_not_found`；远程服务暂不可用返回固定错误码，保留原有状态。

状态：`GET /api/knowledge-intake/uploads/{receipt_id}`。确认和状态响应始终 `upload=null`，不返回 object key、数据库字段、内部主体或下载地址。

## 后台核验与恢复

```sh
python3 scripts/knowledge_receipts.py verify --once
python3 scripts/knowledge_receipts.py verify --loop
```

worker 应由现有服务管理器保持运行。单次命令的 `task_claimed` 仅表示领取过任务，验收结果以状态 API 为准。

| 状态 | 含义和下一步 |
| --- | --- |
| `awaiting_upload` | 等待原件；同一申请可刷新短时上传地址 |
| `pending_verification` | 已确认对象或等待临时失败后的重试 |
| `verifying` | worker 持有租约，正在校验 |
| `verified` | 服务端流式读取的长度和 SHA-256 一致；尚未进入知识库 |
| `rejected` | 大小、哈希、对象 ACL、元数据或 ETag 不符合；不自动重试 |
| `failed` | 临时异常或进程失联累计达到上限；同主体 complete 可显式重新入队 |

PostgreSQL 短事务和一个全局租约槽限制同时校验为 1 个。SQLite 使用写事务实现同样的开发行为。
网络读写不持有数据库事务。任务有 90 秒租约，流式读取期间每 30 秒续租；过期后另一 worker 可恢复，旧 token 的续租和完成操作不会生效。
最多尝试 5 次，临时失败按 30、60、120、240 秒退避，封顶 300 秒；重复进程崩溃同样受次数限制。
GET 使用固定 ETag 的 If-Match，读取前后核对对象属性。读取块最多 64 KiB，不把原件落盘或整份加载内存；单份流式核验限时 300 秒。
请求超时、限流、网络异常保持固定错误码，不保存 OSS 异常文本、签名 URL 或原始内容。

停止 worker 后，收件状态仍在数据库，OSS 原件仍保留。关闭 `KNOWLEDGE_RECEIPTS_ENABLED` 会停止 HTTP 新收件和新启动的 CLI worker；运行中的 worker 应由服务管理器停止。
本模块不解析 ZIP，不读取员工其他文件，不做自动发布，也不改变原有知识检索指针。

## Hermes 试点上传程序

`scripts/upload_knowledge_receipt.py` 使用本机 Hermes 的原生 MCP 连接调用申请、确认和状态工具，无需修改 Hermes 核心。
它只读取 `--file` 指定的文件，不扫描会话目录或员工磁盘。先以合成文件验证：

```sh
/Users/xx/.hermes/hermes-agent/venv/bin/python scripts/upload_knowledge_receipt.py --file /absolute/path/synthetic.json --source-id hermes:synthetic-session-001
/Users/xx/.hermes/hermes-agent/venv/bin/python scripts/upload_knowledge_receipt.py --file /absolute/path/synthetic.json --source-id hermes:synthetic-session-001 --execute --wait-seconds 60
```

第一条默认 dry-run；第二条使用现有 `company-mcp` 连接申请直传，完成后最多等待 60 秒查询核验状态。
服务器尚未部署新工具或权限未授予时应明确失败，不回退到本地假成功或旧只读工具。
正式定时采集和真实员工资料处理属于后续阶段，本命令不会自动创建计划任务。

## 验证边界

离线回归：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -m offline -p no:cacheprovider tests/test_knowledge_receipts.py
```

覆盖完整合成原件流程、真实 SDK V4 签名约束、主体隔离、并发幂等、超时租约隔离、重启恢复、临时错误退避、篡改拒绝、默认关闭和显式建表。
这些检查不代表真实 OSS、生产 PostgreSQL、云权限或 Hermes 原生工具调用已经通过验收。正式启用前，需要以合成文件完成一次真实云链路，并核对原有检索仍然可用。

另有 `tests/test_knowledge_receipts_postgres.py`：只有显式提供 `KNOWLEDGE_TEST_DATABASE_URL` 才运行，使用随机隔离 schema 检查真实 PostgreSQL 的并发幂等、全局任务槽和过期租约隔离，并在结束后清理该 schema。
不要将正式数据库连接直接复制为测试配置；应使用可创建测试 schema 的隔离测试库。未配置时该测试明确跳过，不计为 PostgreSQL 验收通过。

## 本次上线辅助与备份

最小增量包只包含收件模块、CLI、验证测试、备份脚本和本说明，不包含环境文件、员工资料、旧 OSS 内容或本机完整 `rag_api.py`。
应从服务器正在运行的版本创建新的发布目录，保留原产品映射和旧知识模块；在该新目录只添加收件文件，并用增量包的 `register_receipts_router.py` 对 `rag_api.py` 做两行路由注册。不能把本机全部未提交修改覆盖到线上。
当前部署的 `rag_api.py` 会调用 `load_env(ROOT)` 读取发布目录 `.env`；worker 和收件 CLI 需要服务管理器显式注入完整 EnvironmentFile。
本次不新增运行依赖，`requirements.txt` 未改变；目标解释器仍须具有支持 V4 签名附加头的 oss2，以及已有 FastAPI、Pydantic、psycopg 和 python-dotenv。

`scripts/knowledge_receipts_backup.py` 默认 dry-run，正式执行仅允许环境文件中的目标为 `127.0.0.1:65432/company_knowledge`，固定使用容器 `rag-company-postgres`。
它读取一致性快照，以 `pg_dump` 保存**完整数据库**，在本次随机创建的 `receipt_restore_*` 空数据库中恢复，核对所有用户表的行数以及知识目录/修订/已有收件表的全行指纹，之后仅删除自己成功创建的验证库。
备份目录 0700、dump 和 manifest 0600，备份保留在服务器，不上传 OSS；不对原表写入，不用 `--clean` 或 `DROP DATABASE ... FORCE`。
异常或中断时检查保留的 `restore-manifest.json`；它保存随机验证库名称、已完成阶段和清理结果，不包含数据库 URL 或密码。

用已核实的线上 Python 执行，以下环境文件及解释器位置来自本次部署基线，上线时再次核对：

```sh
/www/wwwroot/ragapi/releases/company-knowledge-20260929/.venv/bin/python /absolute/staging/scripts/knowledge_receipts_backup.py --env-file /www/wwwroot/ragapi/rag-company.env --out-dir /www/backup/rag-receipts-20261008
/www/wwwroot/ragapi/releases/company-knowledge-20260929/.venv/bin/python /absolute/staging/scripts/knowledge_receipts_backup.py --env-file /www/wwwroot/ragapi/rag-company.env --out-dir /www/backup/rag-receipts-20261008 --execute
```

若同时传 `--check-receipts`，脚本会用同一解释器在已恢复的随机验证库中运行收件 PostgreSQL 并发测试；测试还会新建随机 schema，结束后一起清理。须将最小增量包的 `src/` 和 `tests/test_knowledge_receipts_postgres.py` 与脚本一起上传，且解释器具备 pytest。该步骤不在正式库创建收件表。

输出目录必须尚不存在，父目录必须已存在；脚本拒绝复用旧备份目录。备份或恢复失败时保留已有文件并停止上线。
恢复结果只证明本次数据库快照可恢复，不包括 OSS 原件、云索引和客户侧检索验收。`source_catalogue_unchanged=false` 表示期间目录发生变化，需要核对是否为合法并发写入，不能把旧快照覆盖回正式库。
