# Third-party references and notices

本项目使用公开的 QQ NT 数据格式研究与既有本地数据接口设计作为参考。
根目录 [LICENSE](LICENSE) 描述本项目贡献的许可；它不替代第三方代码、依赖或资料原有的许可。
本说明区分方法/格式事实参考与源码复制或改编，不宣称进行了完整代码权属法律审查或严格 clean-room 开发。

## MIT 来源与保留声明

### fnyfree/qqnt-chat-decrypt

- 来源：[固定提交 a73ec7d0b576dbb67e13536d3641530d8fa640f9](https://github.com/fnyfree/qqnt-chat-decrypt/tree/a73ec7d0b576dbb67e13536d3641530d8fa640f9)。
- 参考范围：`scripts/dump_heap.py`、`scripts/find_key.py` 的 LLDB 可写内存读取、16 字节可打印候选和数据库验证路线；`scripts/decrypt.sh` 的 QQ SQLCipher 参数事实。
- 本项目相应位置：`src/qq_local_interface/capture.py`、`lldb_capture.py`、`crypto.py`。
- 关系：明确的方法参考，候选正则也有相同表达。当前实现改为在 LLDB 内认证目标库页面，仅保留命中 key，并自行实现页面/WAL 读侧流程；不把参考路线称作本项目首创。
- 为保留相关参考及可能改编范围的许可声明，附原始 [MIT 通知全文](LICENSES/fnyfree-qqnt-chat-decrypt-MIT.txt)。原文版权声明只给出年份，本项目不为其补造作者姓名。

### wechat-local-interface

- 来源：本项目开发时已有的同名本地接口项目，MIT 版权声明为 `Copyright (c) 2026 Ethan Wang`；原公开仓库 URL 未在此记录。
- 参考范围：来源隔离、稳定 ID、版本化请求/响应、固定快照查询、分页、变更及导出约定。
- 本项目相应位置：`src/qq_local_interface/source.py`、`protocol.py`、`util.py` 及相关接口设计。
- 关系：架构与接口设计参考；本说明不据相似的标准库辅助函数判断所有实质代码的来源。若存在移植或改编，应继续保留原 MIT 通知。
- 保留 [原始 MIT 通知全文](LICENSES/wechat-local-interface-MIT.txt)，不以本项目的 contributors 声明覆盖该通知。

以上通知保留不表示两项目作者对本项目背书，也不代替对新增代码实际来源的核对。

## 协议、格式与实现事实参考

以下资料用于理解 QQ 文件布局、数字字段、元素类型和公开行为。当前分发源码不将它们的程序、生成 parser/schema、QQ 客户端代码或私有解密二进制作为随附实现。
未来如果复制或实质改编其中的受许可代码，应按实际使用内容重新记录其许可，不能仅沿用本项目的 MIT 声明。

| 来源 | 参考内容 | 所核验许可与边界 |
| --- | --- | --- |
| [QQBackup/QQDecrypt](https://github.com/QQBackup/QQDecrypt/tree/0dba968ebcb9ba23a68571780eb6b53687f77493) | 扩展头、数字列、收藏及平台路径等格式事实 | 自定义非商用许可；未纳入其实现代码或大段文档 |
| [QQBackup/qq-win-db-key](https://github.com/QQBackup/qq-win-db-key/tree/5c643c39bdc65a7ce3ecc414052fdfd93d5f9107) | 分平台取 key 路线及适用条件 | 自定义非商用许可；未随附其脚本 |
| [QQBackup/nt_msg_db_util](https://github.com/QQBackup/nt_msg_db_util/tree/317024ce2c5e3482fdf9f310a5a7159ab4927e34) | 消息编号、Protobuf 字段与 SQLite 格式事实 | GPL-3.0；未搬运其 parser、生成 schema 或导出实现 |
| [NapNeko/NapCatQQ](https://github.com/NapNeko/NapCatQQ/tree/3420f0b76d7dc0ac92cde1663f21ea27f61e13bb) | QQNT 原生元素编号、收藏 API 等类型/行为事实 | Limited Redistribution License，含非商用与修改代码发布限制；未随附其代码、生成 schema 或运行程序 |
| [NapNeko/qq_dump_db](https://github.com/NapNeko/qq_dump_db/tree/9e411cee5b6e2811593af7b2c864f1850dbd2a76) | key/salt 表达、页 HMAC 和 SQLite 保留区问题 | 仓库整体许可未确认；没有据公开可见而复制其实现 |
| [weq-devs/WeQ](https://github.com/weq-devs/WeQ/tree/7c83c24b5971e21868173a629ee3f44030f0ff77) | 已有产品能力、原始库/收藏/MCP 工作流的对照 | CC BY-NC-SA 4.0；未随附其源码或私有来源的 `nt_helper` |
| [shuakami/qq-chat-exporter](https://github.com/shuakami/qq-chat-exporter/tree/4ae4b700939346b46b05b46346fb5c5f08b081) | NapCat bridge 批量取得/导出数据的路线对照 | GPL-3.0；未随附其实现 |

QQ 的列编号、协议消息号、文件布局与现有技术路线相同，不意味着本项目发明了它们；这里也不以“仅参考事实”的文字代替实际代码来源审查。

## 通用格式与运行依赖

- [SQLite 文件格式](https://www.sqlite.org/fileformat2.html)与 [WAL 格式](https://www.sqlite.org/fileformat2.html#wal_format)：页布局、保留区、提交帧与校验和。
- [SQLCipher API](https://www.zetetic.net/sqlcipher/sqlcipher-api/)：密钥派生、页面大小、HMAC 与兼容参数。
- 系统 LLDB 用于显式首次密钥捕获；正常查询和快照刷新读取已保存密钥。
- `cryptography`、可选 MCP SDK 及测试依赖仍受各自许可证约束。本项目通过依赖管理安装它们，不将其版权改为本项目所有。

更多格式来源见 [docs/sources.md](docs/sources.md)，公开前的范围说明见 [docs/publication.md](docs/publication.md)。
