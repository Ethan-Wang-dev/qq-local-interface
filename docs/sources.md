# 格式来源、选型与核验边界

核验日期：2026-10-23。实现使用 QQ 格式事实与官方 SQLite/SQLCipher 文档，没有复制
QQBackup 非商用脚本、GPL 消息解析器或 WeQ 私有原生二进制作为本项目运行依赖。

| 来源 | 用途和边界 |
| --- | --- |
| [QQBackup/qq-win-db-key](https://github.com/QQBackup/qq-win-db-key/tree/5c643c39bdc65a7ce3ecc414052fdfd93d5f9107) | 分平台提取路径参考；其许可限制商用，未作为运行依赖 |
| [QQDecrypt](https://github.com/QQBackup/QQDecrypt/tree/0dba968ebcb9ba23a68571780eb6b53687f77493) | 文件扩展头、profile/group 数字列语义及 macOS 路线 |
| [fnyfree/qqnt-chat-decrypt](https://github.com/fnyfree/qqnt-chat-decrypt/tree/a73ec7d0b576dbb67e13536d3641530d8fa640f9) | MIT；提供不依赖函数偏移的 LLDB 内存候选路线；本项目改为只保留认证 key，不落整份 heap |
| [NapNeko/qq_dump_db](https://github.com/NapNeko/qq_dump_db/tree/9e411cee5b6e2811593af7b2c864f1850dbd2a76) | 密钥与 salt 组合模式、页面布局问题参考；Windows 实现未直接移植 |
| [QQBackup/nt_msg_db_util 字段文档](https://github.com/QQBackup/nt_msg_db_util/tree/317024ce2c5e3482fdf9f310a5a7159ab4927e34) | 消息编号和 Protobuf 字段事实；未复制生成的 GPL parser/schema 文件 |
| [NapCatQQ 的 QQNT 类型定义](https://github.com/NapNeko/NapCatQQ/tree/3420f0b76d7dc0ac92cde1663f21ea27f61e13bb/packages/napcat-core/types) | ElementType 数值与类型事实；独立实现解码，文件预览宽高不再决定消息类型 |
| [SQLite 文件格式](https://www.sqlite.org/fileformat2.html#wal_format) | WAL 校验和、提交边界、页面编号及保留区 |
| [SQLCipher API](https://www.zetetic.net/sqlcipher/sqlcipher-api/) | key 派生、页面大小、HMAC 与完整性语义 |
| [sqlite3_file_control](https://www.sqlite.org/c3ref/file_control.html) | 测试通过真实 SQLite C 引擎生成具有 48/80 字节保留区的合成文件 |
| 本机现有 `wechat-local-interface` | MIT；参考不可变快照、稳定 ID、版本化 NDJSON、分页及导出约定 |

## 已做核验

- Intel macOS 12.7.4、QQ 6.9.85（36362）下，使用已登录的指定账号。
- 密钥逐一对目标库做页面认证，不以“找到可打印字符串”当作命中。
- 真实 QQ 数据解密后运行 SQLite quick_check；页面头的保留区保持原值。
- 不使用代码仓库存放真实数据库、账号配置、密钥、heap 或聊天导出。
- SQLite 引擎生成的 WAL 用于验证明文及加密 WAL 的已提交数据回放；无提交尾部不发布。
- JSON/NDJSON 与 MCP 调用共用 dispatcher，错误和参数约定保持一致。

## 尚未承诺

- Apple Silicon、其他 QQ 版本、其他 cipher profile 的实机兼容性。
- `guild1.db` 的内容、未采样 Protobuf 类型的业务解释，以及完整在线群成员名单。
- 已删除数据恢复、云端漫游全量补齐、未缓存的远程媒体与媒体内容理解。
- 合并转发的外层 XML/资源标识可访问；缺少本地内层记录时，不宣称已展开全部转发消息。
- 微秒以下时间精度、跨库同一业务事务时间点和页面级增量解密。
