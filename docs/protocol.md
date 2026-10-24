# qq.local.protocol.v1

UTF-8 NDJSON：一行请求对应一行响应；空行忽略；错误 JSON 不影响下一行。

```json
{"protocol_version":"qq.local.protocol.v1","request_id":"r1","source_id":"qq-local","operation":"messages.read","params":{"limit":100}}
```

请求只接受 `protocol_version`、`request_id`、可选 `source_id`、`operation` 和 `params`。
`request_id` 为 1–128 字符。未知操作、未知参数与 source 不匹配会返回明确错误。

成功：

```json
{"ok":true,"data":{"items":[],"next_cursor":null},"meta":{"protocol_version":"qq.local.protocol.v1","schema_version":"qq.local.v1","request_id":"r1","source_id":"qq-local","operation":"messages.read","snapshot_id":"GENERATION_ID"}}
```

失败：

```json
{"ok":false,"error":{"code":"invalid_argument","message":"limit must be an integer between 1 and 5000","details":{}},"meta":{"protocol_version":"qq.local.protocol.v1","schema_version":"qq.local.v1","request_id":"r1","source_id":"qq-local","operation":"messages.read"}}
```

## 操作目录

| operation | params | 结果 |
| --- | --- | --- |
| `status` | 无 | 能力、数量、快照时间、同步状态 |
| `sync.status` | 无 | 配置及最近检查，不读取聊天正文 |
| `sync.refresh` | `mode=incremental/full`, `allow_partial=false` | 是否改变、更新/复用库、coverage、新快照代号 |
| `contacts.list` | `query`, `limit` | actor 数组 |
| `conversations.list` | `query`, `kinds`, `limit` | conversation 数组 |
| `contacts.read` | `query`, `limit`, `cursor` | 全量 actor 分页，items、next_cursor |
| `conversations.read` | `query`, `kinds`, `limit`, `cursor` | 全量 conversation 分页 |
| `groups.members` | `conversation_id` 必需，`limit`, `cursor` | members、complete、truncated、next_cursor |
| `messages.read` | 消息过滤器、`limit`, `cursor` | items、next_cursor、snapshot_id |
| `messages.search` | `query` 必需，其余同 read | 消息分页 |
| `messages.export` | `output` 必需，消息过滤器、`previous` | 完整导出包 manifest |
| `changes.list` | `cursor`, `limit`, `entity_types` | changes、cursor、next_cursor |
| `messages.get` | `item_id` 必需，`include_raw=false` | 单条消息及可选完整原始行 |
| `databases.list` | 无 | 所有库的 ready/uninitialized/unavailable 状态 |
| `tables.list` | `database` 必需 | 表、列、行数、内容代理模式和可访问状态 |
| `datasets.list` | `category`, `include_internal=false` | 按业务类别发现原始数据集，包含不可用库的状态 |
| `datasets.read` | `dataset_id` 必需，`equals`, `limit`, `cursor` | 无损原始行分页 |
| `records.read` | `database`, `table` 必需，`equals`, `limit`, `cursor` | 无损原始行分页 |
| `records.export` | `database`, `table`, `output` 必需，`equals` | 自包含的完整原始表 JSONL 导出 |
| `cells.read` | `cell_ref` 必需，`offset=0`, `length=65536` | 原始 text/BLOB 字节的 base64 分块 |
| `assets.list` | `kinds`, `query`, `limit`, `cursor` | 本地缓存快照的文件元数据分页 |
| `assets.read` | `asset_id` 必需，`offset=0`, `length=65536` | 本地媒体快照的 base64 分块 |
| `favorites.read` | `query`, `kinds`, `limit`, `cursor` | 已加载到本地的收藏分页；摘要与正文状态分开 |
| `favorites.get` | `favorite_id` 必需，`include_raw=false` | 单条收藏与可选无损原始行 |
| `favorites.export` | `output` 必需，`query`, `kinds`, `require_details=false` | 私有 JSONL 导出及缺失正文统计 |

消息过滤器：`conversation_ids`、`start`、`end`、`author_ids`、`kinds`、`directions`、`qualities`、`query`。
不同条件使用 AND，列表中的值使用 OR；`query` 匹配已解析文本，使用 Unicode casefold 子串匹配。
`directions` 为 incoming/outgoing/unknown。`limit` 为 1–5000，默认消息分页 100。

`messages.export` 不接受分页 `limit` / `cursor`，会完整导出匹配范围。
`previous` 必须是同 source、schema 和相同过滤条件的旧导出包；缺失记录只计数，不产生删除。

导出包包含 `manifest.json`、`actors.jsonl`、`conversations.jsonl`、`items.jsonl`、`changes.jsonl`。
导出不覆盖旧包。其路径是调用进程所在机器上的本地路径。

`status.change_cursor` 是当前不可变快照末尾的变化水位；可先保存它，再读取完整快照，
之后从该水位继续消费未过滤的变化流，避免初始化与刷新交错时漏掉数据。

`contacts.list` 和 `conversations.list` 保留旧数组返回格式，受 `limit` 限制；需要全部数据时使用 `.read`，
持续跟随 `next_cursor`。群成员的分页完成不意味着在线群成员名单完整。

## 全表、字段与媒体

`datasets.list` 的类别包括 contacts、groups、messages、favorites、emoticons、resources、channels、settings、misc。
它通过实际表结构发现数据，调用者不需要猜 QQ 的列名。原始表名和数字列名保留为可发现的原生标识，
语义化消息接口和原始字段接口共同提供访问；不承诺所有数字列都已有业务解释。

`equals` 只允许已存在列的精确相等条件，不接受 SQL。原始记录包含 id、revision、row_ref、values。
每个 values 字段都有 SQLite `type`：

| type | 精确编码 |
| --- | --- |
| null | `value:null` |
| integer | 十进制字符串，含完整有符号 64 位范围 |
| real | Python/C99 十六进制浮点字符串，例如 `0x1.4000000000000p+0`；调用方按浮点十六进制解析 |
| text | UTF-8 原文；无效 UTF-8 使用 base64，保留原始字节 |
| blob | base64 原始字节 |

超过 4096 字节的 text/BLOB 返回 `encoding:"reference"`、`cell_ref`、length、sha256；
用 `cells.read` 从 offset=0 读取，直到 next_offset=null。length 为 1–1048576。
每块有 chunk_sha256；拼接后应验证整个字段的 sha256。cell_ref 绑定来源和快照。
`records.export` 自动实体化所有大字段，导出的 JSONL 无需再访问旧快照即可还原字节。

QQ 自定义 FTS5 tokenizer 无法由标准 SQLite 加载时，表接口代理到其外部/内部内容表；
contentless 索引按原生逻辑返回实际 rowid 对应的 NULL 内容，原始索引字节仍可读。
没有可验证内容映射的虚拟表必须标为不可访问，禁止空数组冒充成功。

`assets.read` 读取该代快照的缓存副本，原缓存被 QQ 删除或改写后仍可读取。
附件的 `local_assets` 给出候选 asset_id 和匹配依据；只有完整原生路径匹配才标为 local_snapshot，
单纯文件名/标识匹配仍明确为 candidate。没有缓存的远程资源只返回已有元数据。

变化流包含 actors、conversations、messages、memberships、databases、assets；
另有 favorites 的已加载缓存变化；
原始辅助表变化通过 databases 事件提示重新读取。原始 row_ref 是定位符，不是跨代变化游标。

`coverage.complete` 只表示选中的本地库均无解密缺口，不表示在线历史、已删数据、未缓存媒体或群成员全部完整。
`coverage.excluded` 和 manifest.selection 明确记录主动排除的数据库；它们不参与完整性成功计数。
显式 `allow_partial:true` 可以发布有缺口的辅助库快照；unavailable 库的读取会返回 database_unavailable，
而不是成功的空结果。未初始化库没有 SQLite 数据页，状态与缺 key 明确区分。

## 错误码

`invalid_json`、`invalid_request`、`invalid_argument`、`protocol_version_mismatch`、
`unsupported_operation`、`source_mismatch`、`source_conflict`、`invalid_cursor`、`not_found`、
`not_configured`、`snapshot_required`、`source_changed`、`invalid_snapshot`、`invalid_database`、
`unsupported_schema`、`unsupported_cipher`、`invalid_wal`、`authentication_failed`、`integrity_failed`、
`key_required`、`invalid_key`、`insecure_key_file`、`source_busy`、`sync_busy`、
`qq_login_required`、`account_not_found`、`key_not_found`、`debug_access_required`、`dependency_missing`、
`database_unavailable`、`database_uninitialized`、`unsupported_table`、`asset_missing`、`asset_unavailable`、
`refresh_required`、`incomplete_data`、`internal_error`。

错误响应不返回 Python traceback、执行的 SQL 或密钥。原始结构通过表目录与字段接口显式发现。

## 机器可读契约

- [请求/响应信封](../schemas/qq.local.protocol.v1.json)
- [操作和参数](../schemas/qq.local.operations.v1.json)
- [主要数据记录](../schemas/qq.local.records.v1.json)
- [无损原始字段和记录](../schemas/qq.local.data.v1.json)

响应可以增加新字段；调用方应忽略未知字段并检查版本。当前 QQ 协议借用了微信接口的操作命名，
但有独立命名空间，不能假定支持微信的收藏夹、朋友圈、公众号等全部操作。
