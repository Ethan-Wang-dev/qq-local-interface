# QQ Local Interface

把 macOS QQ 本地数据库变成可同步、可查询、可交给其他 Agent 使用的稳定接口。

[GitHub](https://github.com/Ethan-Wang-dev/qq-local-interface) · [版本记录](CHANGELOG.zh.md) · [合成测试](https://github.com/Ethan-Wang-dev/qq-local-interface/actions/workflows/tests.yml)

接口沿用 `wechat-local-interface` 的分层思路和常用操作命名，独立提供 QQ 的
密钥接入、全量/增量快照、标准化数据、Python API、JSON/NDJSON 和 MCP stdio。

```text
macOS QQ 数据目录 ── 显式首次取 key ── 私有密钥文件
        │                                │
        └─ 稳定复制 DB + WAL ─ 认证解密 ──┘
                              │
                     不可变快照 + 变更索引
                              │
                    原子发布 current.json
                              │
               Python / CLI / NDJSON / MCP → Agent
```

## 当前可用能力

| 层 | 能力 |
| --- | --- |
| 密钥 | 从已登录的 QQ 进程读取并认证密钥；也可接入外部密钥文件 |
| 解密 | QQ NT 的 1024 字节扩展头、SQLCipher 页面 HMAC、保留原始页面布局 |
| 同步 | 全量；跳过未变化库的增量；处理已提交 WAL；失败保留上一份快照 |
| 查询 | 联系人/发言人、会话、群成员索引、消息读取、全文子串搜索、时间与类型过滤 |
| 全表 | 自动发现全部本地数据库；按数据类别/表分页读取全部字段；保留 SQLite 原始类型和字节 |
| 媒体 | 本地缓存生成独立快照；图片、语音、视频和文件可分块读取与校验 |
| 变化 | 稳定 ID、revision、跨同步保留的 added/updated/missing 游标 |
| 导出 | 完整 JSONL 包；与旧导出比较新增/更新；不受屏幕分页条数限制 |
| Agent | 版本化 JSON/NDJSON 协议；21 个核心 MCP 工具，另有 3 个本地收藏读取/导出工具；机器可读 schema |

“更新”指同步 QQ 的本地新增/变更数据。它不编辑 QQ 数据库，也不发送消息。

已在 **Intel macOS 12.7.4 + QQ 6.9.85（36362）** 完成真实账号的密钥认证、
所选数据库的逐行/逐字段比对、快照查询和 MCP 调用。
当前验收按用户要求排除频道：19 个所选库中 16 个有数据、3 个未初始化，192 张表全部可读；
`guild1.db` 和 `guild_msg.db` 明确记录为排除项，**不把排除等同于已解密或已验证**。
完整结果与复测方式见 [覆盖报告](docs/coverage.md)。Apple Silicon 和其他 QQ 版本尚未实测；
捕获器不依赖寄存器或硬编码函数地址，但不把这一点当作跨版本兼容保证。

## 安装

Python 3.10+；macOS 首次取 key 使用 Xcode Command Line Tools 自带的 `lldb`。

```bash
git clone https://github.com/Ethan-Wang-dev/qq-local-interface.git
cd qq-local-interface
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[mcp]'
```

以下示例使用 `.venv/bin/qq-local-interface`。也可以使用已安装环境中的
`python -m qq_local_interface`。普通查询与日常同步不需要 LLDB 或 SQLCipher CLI。

## 首次接入

```bash
# 检查系统、QQ 版本、进程状态；不输出聊天或密钥
.venv/bin/qq-local-interface doctor

# 必须明确指定自己的 QQ 账号；同一 source_id 不能切到其他账号
.venv/bin/qq-local-interface --source-id my-qq init --account YOUR_QQ_NUMBER

# 在 QQ 中登录该账号，再显式抓取一次 key
.venv/bin/qq-local-interface --source-id my-qq capture-key

# 首次生成完整快照
.venv/bin/qq-local-interface --source-id my-qq sync --mode full
```

默认同步要求所有选中的有数据数据库成功解密。若辅助库缺 key，主消息库仍可用，
可显式选择 `sync --allow-partial` 发布其余数据；返回 `coverage.complete:false` 和具体缺失库。
它不会把失败库计为解密成功，也不会为它提供空表伪装成功。`nt_msg.db` 始终必须成功。

不需要频道时，可明确排除两个频道库，使其不阻塞日常严格同步：

```bash
.venv/bin/qq-local-interface --source-id my-qq init --account YOUR_QQ_NUMBER \
  --exclude-database guild1.db --exclude-database guild_msg.db
```

`coverage.complete` 只覆盖所选数据库，`coverage.excluded` 保留排除名单。
需要恢复全库选择时，用同账号重新 `init --include-all-databases`；此前排除的库仍需各自通过认证。

`capture-key` 只保留通过目标数据库 HMAC 验证的 key，不保存整份进程内存，
不会打印 key，也不修改 QQ 的签名。若系统拒绝调试，返回 `debug_access_required`，
需要单独解决本机调试权限；程序不自动重签名或关闭系统保护。

已从其他工具得到 key 时，使用权限为 `0600` 的私有 JSON 文件：

```json
{"version":1,"default":{"kind":"passphrase","value":"YOUR_DATABASE_KEY"}}
```

也支持 `databases` 按文件名选 key，以及 `keys_by_salt` 按盐值选 key；
`kind="raw"` 时 `value` 为 32 字节派生密钥的 64 位十六进制。
`default` 只适用于实际共享同一个 passphrase 的库，每个页面仍会认证。

```bash
.venv/bin/qq-local-interface --source-id my-qq init \
  --account YOUR_QQ_NUMBER --key-file /private/path/keys.json
```

## 查询与更新

```bash
.venv/bin/qq-local-interface status
.venv/bin/qq-local-interface sync
.venv/bin/qq-local-interface contacts --query "关键词"
.venv/bin/qq-local-interface conversations --kind group
.venv/bin/qq-local-interface members CONVERSATION_ID
.venv/bin/qq-local-interface messages --conversation CONVERSATION_ID --limit 100
.venv/bin/qq-local-interface search "关键词" --conversation CONVERSATION_ID
.venv/bin/qq-local-interface messages --start 2026-10-01T00:00:00Z --end 2026-11-01T00:00:00Z
.venv/bin/qq-local-interface changes --limit 100
.venv/bin/qq-local-interface export --conversation CONVERSATION_ID --output /private/exports
.venv/bin/qq-local-interface databases
.venv/bin/qq-local-interface datasets --category favorites
.venv/bin/qq-local-interface tables group_info.db
.venv/bin/qq-local-interface records group_info.db group_member3 --limit 100
.venv/bin/qq-local-interface message ITEM_ID --include-raw
.venv/bin/qq-local-interface assets --kind image --limit 100
.venv/bin/qq-local-interface asset ASSET_ID --offset 0 --length 65536
.venv/bin/qq-local-interface call contacts.read --params '{"limit":100}'
```

默认 source ID 为 `qq-local`。全局 `--source-id`、`--vault` 和 `--snapshot` 放在命令之前。

持续运行时，可显式启动下面的进程；没有自动安装后台服务：

```bash
.venv/bin/qq-local-interface watch --interval 30
```

增量同步会比对 DB **及 WAL** 的内容指纹与缓存文件版本，跳过无需解密的库，然后重建当前标准化索引。
这是按库跳过工作的增量刷新，不是页面级增量解密。标准化器版本变化也会触发重建。

每次发布一代快照，再原子替换 `current.json`；数据库覆盖情况由 `coverage` 明示。正在查询的旧快照仍可读；
QQ 写入、错误 key、坏页、热回滚日志或刷新并发失败都不会覆盖上一份已发布结果。

## 给其他 Agent 使用

生成当前安装路径对应的 MCP 配置：

```bash
.venv/bin/qq-local-interface agent-config
```

把生成的 `mcpServers.qq-local` 加到支持 MCP 的客户端。默认 stdio，不监听网络端口。

工具：`qq_status`、`qq_sync_refresh`、`qq_contacts`、`qq_conversations`、
`qq_contacts_page`、`qq_conversations_page`、`qq_group_members`、`qq_messages`、
`qq_message`、`qq_search`、`qq_changes`、`qq_export`、`qq_databases`、`qq_tables`、
`qq_datasets`、`qq_dataset`、`qq_records`、`qq_cell`、`qq_assets`、`qq_asset`、`qq_export_records`。

本地收藏缓存另有 `qq_favorites`、`qq_favorite`、`qq_export_favorites`。它们区分摘要与正文缓存状态；
本轮已发现 7 条真实摘要，但全文需额外登录态，已按用户要求暂缓。详情见 [收藏调查](docs/favorites.md)。

不使用 MCP 的 Agent 可以调用稳定的 NDJSON 子进程协议：

```bash
.venv/bin/qq-local-interface rpc < requests.ndjson > responses.ndjson
```

```json
{"protocol_version":"qq.local.protocol.v1","request_id":"r1","operation":"sync.refresh","params":{"mode":"incremental"}}
{"protocol_version":"qq.local.protocol.v1","request_id":"r2","operation":"conversations.list","params":{"kinds":["group"],"limit":100}}
```

完整调用规则见 [Agent 指南](docs/agent-guide.md) 和 [协议](docs/protocol.md)。

## Python API

```python
from qq_local_interface import QQVault, QQSource

vault = QQVault(source_id="my-qq")
vault.refresh()  # 显式同步；只使用已保存 key
snapshot, manifest = vault.current()
source = QQSource(snapshot)

conversations = source.list_conversations(kinds=["group"])
page = source.read_items([conversations[0]["id"]], limit=100) if conversations else None
changes = source.changes(cursor=previous_change_cursor)  # None 表示从头读取
```

上例的 `previous_change_cursor` 由调用方持久化。跨同步使用变化游标；
消息分页游标只属于同一快照和同一筛选条件。

## 本地数据位置

默认私有目录：`~/Library/Application Support/qq-local-interface/`。

```text
sources/<source_id>/
├── config.json            # 账号与本机目录映射，不进入代码仓库
├── keys.json              # 0600；不通过 Agent 接口暴露
├── current.json           # 当前快照代号；覆盖状态见 manifest
├── last-check.json        # 最近检查时间
└── generations/<id>/
    ├── manifest.json
    ├── records.db         # Agent 的标准化查询与变化索引
    ├── data/*.db          # 原始解密库，保留供审计与扩展
    └── assets/asset_*     # 这一代缓存文件的独立字节副本
```

目录为 `0700`，密钥和配置为 `0600`，发布的数据库与媒体副本为 `0400`。
macOS 优先使用 APFS clonefile 创建缓存副本；不支持时复制文件。原缓存变化不会改变已发布媒体。
聊天、密钥与明文数据库不写入项目目录。旧代快照保留用于一致读取；
当前版本不自动清理历史代，长期运行时需安排保留策略。

## 数据边界

- `contacts` 包含好友、群成员和观察到的发言人；只有明确的好友索引才标为 `friend`。
- 群成员结果明确返回 `complete:false`，不能当作在线完整群成员名单。
- 已观察到的 12 种消息元素可以分类，保留全部原始字段；未知元素标为 `unsupported`，解码错误单独标记。
- 图片、语音、视频和文件可读取**已缓存**的原始字节。语音转写、视频理解、自动远程下载与已删除数据恢复不在本接口内。
- `datasets.list/read` 和 `records.read/export` 覆盖所有成功解密的表，包含收藏、表情、资源、设置、频道消息及 QQ 自定义索引的内容代理。
- `messages.read/search` 的语义索引当前来自 `c2c_msg_table` 与 `group_msg_table`；其他消息表通过 datasets/records 读取，不把无真实样本的类型宣称为已完成语义解析。
- 频道已排除；收藏库已读到 7 条真实摘要，正文表仍为空，全文同步暂缓，不能把摘要当成完整正文。
- 字符串/二进制大字段通过 `cells.read` 无损分块读取；整数与浮点字段使用精确字符串编码，避免 Agent JSON 数值精度损失。
- `missing` 表示当前快照没观察到，**不是确定删除**。
- 分库捕获会检查文件集合在复制期间未变化；不承诺跨多个 QQ 库的同一业务事务时间点。
- 每次变化重建查询索引，变化日志持续保留；大量数据的检索和保留策略需要后续优化。

## 开发与验证

```bash
.venv/bin/python -m pip install -e '.[test]'
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m compileall -q src
```

测试只使用合成数据，包括 SQLite 引擎生成的保留区/WAL、加密认证、坏页、
提交边界、同步失败回退、并发锁、分页、变化游标、跨账号隔离、全部接口与 MCP stdio、无损字段、媒体快照与完整导出。

真实账号的全量比对（报告及导出写入私有 vault，不输出聊天正文）：

```bash
.venv/bin/python tools/audit_coverage.py --export-tables
.venv/bin/python tools/audit_transports.py --core-only
```

报告把“可访问数据逐项验证通过”和“全部本地库都已可访问”分开统计。

MIT。格式来源与复用边界见 [sources.md](docs/sources.md)、[第三方通知](THIRD_PARTY_NOTICES.md)。
工程定位、与现有项目的异同及发布范围见 [独立发布说明](docs/publication.md)。
