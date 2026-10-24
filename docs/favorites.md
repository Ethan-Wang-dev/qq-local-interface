# 收藏调查与当前范围

`collection.db` 已经能够认证解密。最初快照只有空的元信息表，不代表账号没有收藏。
QQ 按需加载：打开收藏列表后，原本为空的 WAL 日志写入了实际数据，现有同步逻辑回放已提交 WAL 后即读到 7 条真实收藏摘要。

随后打开一条详情，客户端创建了 `content_list_info_table`，但本次观察到的行数仍为 0。
因此本次已验证的是收藏摘要与原始字段，尚不能把它们当作详情全文。

## 原生数据关系

- `collection_list_info_table`：收藏索引。180001 是稳定原生标识，180002 是内容类型，180008 是分类；两种类型不能混用。
- 180004 与 180015 是分别带有同号信封的 Protobuf。180015 内部再按原生内容类型选择摘要子消息。
- 本次 7 条的内容类型均为富媒体；分类含聊天内容、笔记、带链接的消息。
- 收藏时间使用毫秒，消息库的秒级时间处理不能直接复用。
- `content_list_info_table`：180700 关联原生收藏标识，180720 为缓存记录时间，180701/180704/180708 等列分别承载内容信封。
- 有效空 LEN 字段与尾部零填充不同。解析器只在字段边界识别全零剩余区，原始字节始终保留；不直接 rstrip 零字节。

数字字段与类型是协议事实，归一化和解析代码在本项目内独立实现；未复制以下项目的生成 parser/schema 或客户端代码。
可对照 [QQDecrypt 的收藏字段资料](https://github.com/QQBackup/QQDecrypt/tree/0dba968ebcb9ba23a68571780eb6b53687f77493/docs/database/db_file_analysis/collection)、
[NapCat 的原生收藏服务定义](https://github.com/NapNeko/NapCatQQ/blob/3420f0b76d7dc0ac92cde1663f21ea27f61e13bb/packages/napcat-core/services/NodeIKernelCollectionService.ts)
及 [WeQ 的格式分析](https://github.com/weq-devs/WeQ/blob/7c83c24b5971e21868173a629ee3f44030f0ff77/docs/database/collection.md)。
来源和许可边界见 [第三方说明](../THIRD_PARTY_NOTICES.md)。

## 已有接口

`favorites.read` / `qq_favorites` 提供本地缓存分页与搜索；`favorites.get` / `qq_favorite`
可附带原始 SQLite 字段。`favorites.export` / `qq_export_favorites` 导出：

- `favorites.jsonl`：归一化摘要与明确的 detail.state。
- `native.jsonl`：自包含的精确原始索引/详情行，长字段会实体化。
- `manifest.json`：条数、已有正文数及缺失或过期详情数；不宣称在线总量已完整覆盖。

`require_details=true` 遇到缺失或过期正文会返回 incomplete_data，避免生成被误认为全文备份的结果。
本地原始库、摘要和导出访问不需要另行登录网络服务。

## 全文路径与暂缓决定

公开实现证实微云收藏网关提供独立的列表和详情读取操作；完整正文需要当前账号的微云登录态。
本机传统 QQ 快登端口未能完成兼容握手，现有 Chrome 也没有可验证的微云登录态，官方网页登录要求扫码。
这表明缺口在取得当前账号授权会话及未缓存的正文，而非未知的数据库密码。

用户已明确要求暂缓收藏全文，本轮已停止云端尝试并关闭为此新建的登录页。
项目当前不提供自动登录/获取票据/云端收藏抓取模块，也不把私有诊断脚本或提取的 QQ 客户端资源纳入分发包。
本地摘要缓存能力保留，未来是否接入全文及其授权流程另行确定。
