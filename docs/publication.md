# 独立 Repo 的定位与公开范围

这套实现可以作为独立的 `qq-local-interface` 工程发布。适合的定位是：

公开仓库地址：[Ethan-Wang-dev/qq-local-interface](https://github.com/Ethan-Wang-dev/qq-local-interface)，首次公开版本为 `v0.2.0`。

> 面向 Agent 的 macOS QQ 本地数据读侧接口：使用已有的 QQ 取 key 与 SQLCipher 格式研究，提供可审计的页面/WAL 读取、同步快照、精确原始数据和版本化接口。

核心实现使用 Python、开源 `cryptography` 密码库和显式首次捕获时的系统 LLDB；不依赖第三方私有 QQ 解密插件。它从本机已登录的 QQ 取得现有密钥并认证目标库，不是破解登录密码或发明新的密码算法。

## 与已有项目的关系

- [fnyfree/qqnt-chat-decrypt](https://github.com/fnyfree/qqnt-chat-decrypt) 是相近的 Mac LLDB 候选路线。本项目承认这种方法来源，在其基础上选择不保存整份 heap，而只保留经过目标页面认证的 key。
- [WeQ](https://github.com/weq-devs/WeQ) 已有 Agent、MCP、收藏、数据库与 BLOB 工具，不能把这些功能写成首创。这里的侧重点是公开的 QQ 读侧实现、不可变数据库/媒体快照、精确类型和字节分块、受限参数接口及可复现验证。
- [qq-chat-exporter](https://github.com/shuakami/qq-chat-exporter) 代表通过 NapCat bridge 取得消息和漫游数据的另一条路线。功能有交集，输入与处理架构不同。

这种差异足以说明独立工程的用途，但不是对其他项目能力、质量或许可安全的否定。可公开的来源说明见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) 与 [sources.md](sources.md)。

## 适合公开的内容

公开范围为本项目有权发布的源代码、机器可读协议、文档、合成数据测试和通用验证脚本。
QQ 客户端二进制/ASAR、真实数据库及 WAL、密钥、heap、账号配置、缓存文件、真实聊天或收藏导出不属于源码发布内容。
验证脚本可以读取所有者自己的私有 vault；它们产生的个人数据报告与导出保留在 vault 中。

项目根目录 MIT 许可覆盖本项目相应贡献。实际复制或实质改编的第三方内容仍需保留原许可；已有的 MIT 参考/潜在改编通知保存在 `LICENSES/`，不能用 contributors 声明覆盖他人通知。
GPL、非商用或许可未知的项目不能因为代码公开可见，就直接搬入后统一标为 MIT。
这里的来源说明没有替发布者确认全部权属，也不宣称未经接触原实现的 clean-room 开发。
本轮已生成 wheel 和源码压缩包，并检查两者包含第三方通知及两份 MIT 原文；源码包同时包含合成测试辅助文件、协议 schema 与审计脚本。
打包文件清单没有真实数据库、WAL、ASAR、密钥配置、二进制捕获或 JSONL 数据导出。后续新增内容后仍需按实际分发文件核对。

## 能力与验证应如何写

- 当前真实客户端验证环境是 Intel macOS 12.7.4、QQ 6.9.85（36362）。其他 CPU 架构、QQ 版本与加密 profile 的实际兼容性需分别记录。
- Linux 上运行合成 SQLite、密钥、字节编码、协议与 MCP 测试，只证明这些测试通过，不代表可以在 Linux 直接取得 Mac QQ 数据，也不等于已验证 Linux QQ。
- 收藏范围取决于 QQ 已经同步并落地到本机的列表/详情及缓存。发现表、读取摘要、取得完整正文和读取附件字节应分别写明，不把空详情表或摘要当作完整正文。
- 频道不在当前验收范围。未验证的频道内容、未缓存的远程媒体、被删历史和在线漫游不应写成已有完整支持。
- 数量、支持类型和质量以当前 [覆盖报告](coverage.md) 为准；原始字段可读不等于全部业务含义已经解释。

禁止使用“首个 QQ MCP”“全新破解算法”“支持全部 Mac QQ 版本”或“全部类型完美恢复”等尚无证据的发布描述。

## 公开前需要核对的具体事项

1. 确认发布者对拟公开的项目贡献有相应权利，并核对是否存在组织或雇佣代码的权属约定。此文不替发布者作出身份或权属确认。
2. 对照实际新增代码核对第三方来源；保留已随附的 MIT 原文，遇到 GPL/非商用代码实质复用时重新处理相应许可。
3. 检查最终待发布文件，确认个人数据、安装包、提取的客户端源码与私有配置没有被纳入；不能只依赖 `.gitignore` 名称匹配。
4. 将版本、功能列表、测试范围与当前实现保持一致，收藏正文或其他仍未验证的能力明确列出边界。
5. 核对发布目标、版本和可见性；公开仓库为 Ethan-Wang-dev/qq-local-interface。后续发布仍应核验远程提交、标签及分发内容。

## 合成测试 CI

[`.github/workflows/tests.yml`](../.github/workflows/tests.yml) 配置了 Ubuntu 上 Python 3.10 与 3.12 的合成测试：安装 `.[test]`、运行单元/协议/MCP 测试和编译检查。
它不安装 QQ、不连接真实账号、不执行 LLDB，也不上传 vault 或测试产物。
远程执行结果以仓库 [Actions](https://github.com/Ethan-Wang-dev/qq-local-interface/actions/workflows/tests.yml) 为准；本机测试结果与真实 QQ 的验证记录分别以覆盖报告为准。
