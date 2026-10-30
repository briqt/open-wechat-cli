# open-wechat-cli

读取本地微信生态历史的 Python CLI 与 Agent Skill。当前实现 Windows 企业微信（WXWork）的只读历史访问：会话、联系人、消息查询，图片/文件/语音/链接卡片与聊天记录（转发）的结构化抽取，以及 JSONL / TXT 导出（可连带附件）。

项目使用 [MIT License](LICENSE)，可以使用、修改和分发。

## 当前支持

- Windows 企业微信 **5.0.13.6005 / PE32 / 4096 字节页**：已有真机验证记录。
- 本地账号发现、进程内存候选密钥验证、数据库复制与离线解密。
- 会话、联系人和消息列表；按会话、时间、关键词过滤；游标分页。
- 消息结构抽取：多段文本与 @提及、图片、文件、语音（时长 + 缓存文件）、视频、文档/公众号/小程序/微盘链接卡片、聊天记录（合并转发，含子消息的文本与附件）。
- 系统消息与卡片：红包、通话记录、统计小结、会议邀请、机器人卡片、文件卡片、视频号、撤回通知都有结构化字段；撤回通知会用 `message_revoke_record_table_v2` 补出撤回者与时间（`recalled`）。
- 语音：识别 SILK v3 缓存并提供 `owc media decode` 转 wav（解码器需外部提供，缺失时给结构化错误）。
- 附件落地：用账号下的 `CacheMapping` 把资源键映射到 `Cache/Image|File|Voice|Video` 的真实文件，导出时可复制出来（语音补 `.silk` 后缀）。
- JSON 机器输出，以及 JSONL / 按会话拆分的 TXT / **Markdown** / **单文件 HTML** 四种导出形态；后两种默认连附件一起落地，图片、语音、聊天记录直接可读。

当前是 alpha 版本。其他客户端版本、macOS 企业微信和个人微信尚未验证或实现；未被客户端缓存过的附件只能给出名称与字节数，语音未转码，撤回消息未还原。

## 从源码安装

需要 Python 3.10+ 和 Git。在运行企业微信的 **Windows 宿主机**上执行 PowerShell：

```powershell
git clone https://github.com/briqt/open-wechat-cli.git
cd open-wechat-cli
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install .
.venv\Scripts\owc.exe doctor
```

也可以激活环境后直接使用 `owc`。开发者的可编辑安装、测试和构建方法见 [CONTRIBUTING.md](CONTRIBUTING.md)。

源码已经包含程序、测试、Skill 和构建配置；GitHub Release 的源码 tag 是版本入口，不要求下载 wheel，也尚未发布到 PyPI。

## 使用

以下命令假设 `owc` 已在 PATH 中，否则使用 `.venv\Scripts\owc.exe`：

```powershell
owc doctor
owc accounts -v

# 首次会取 key、复制和解密数据库；默认显示最新消息
owc history --account <account-id> --limit 20 --tz Asia/Shanghai

# 或分步执行，复用解密后的工作副本
owc key scan --account <account-id>
owc snapshot --account <account-id> --out ./runtime/snapshot
owc decrypt --snapshot ./runtime/snapshot --out ./runtime/decrypted --account <account-id>
owc sessions list --db-dir ./runtime/decrypted/Data --limit 20
owc contacts list --db-dir ./runtime/decrypted/Data --query <name>
owc messages list --db-dir ./runtime/decrypted/Data --since 2026-10-01 --limit 50
owc messages export --db-dir ./runtime/decrypted/Data --out ./runtime/export/messages.jsonl
# 按会话/按时间查询是大库的主要瓶颈：给解密副本补一次索引（建一次，之后毫秒级）
owc messages export --db-dir ./runtime/decrypted/Data --conv <conv-id> --index --out ./runtime/export/conv.jsonl
# 连附件一起导出（图片/文件/语音：同卷硬链接、跨卷复制，语音补 .silk）
owc messages export --db-dir ./runtime/decrypted/Data --conv <conv-id> --out ./runtime/export/conv.jsonl --copy-media
# 给人看的稿子：Markdown 或单文件 HTML（默认复制附件，图片内嵌引用）
owc messages export --db-dir ./runtime/decrypted/Data --conv <conv-id> --since 2026-10-22 --format md --out ./runtime/export/conv.md
owc messages export --db-dir ./runtime/decrypted/Data --conv <conv-id> --since 2026-10-22 --format html --out ./runtime/export/conv.html
# 语音转 wav：解码器用路径命令或模板，缺失时会报 DEPENDENCY 并给修复建议
owc media decode --in ./runtime/export/media --out ./runtime/export/wav
owc messages export --db-dir ./runtime/decrypted/Data --conv <conv-id> --out ./runtime/export/conv.jsonl --copy-media --decode-voice
```

`<account-id>` 是 `owc accounts` 列出的本地账号目录标识。查询和导出也可以针对复制到其他平台的已解密数据库离线运行；此时想要附件路径就加 `--media-root <账号目录>`。

### 输出

- `--json` 支持放在子命令前后。成功结果写入 stdout 的 JSON 信封。
- 诊断写入 stderr；JSON 模式下诊断为 JSON Lines，失败时最后一行为错误信封。
- 错误包含 `code`、`message` 和适用时的 `repair`；参数解析错误仍由 argparse 输出 usage。
- 列表结果提供 `next_cursor`，原样传给 `--cursor`；翻页时保持过滤条件和排序一致。
- 时间包含时区偏移；消息同时提供 `send_time_epoch`。
- 消息对象含 `link`、`media`、`segments`、`entries`、`forward`、`voice` 等结构；附件项的 `exists: false` 表示本地缓存里没有该文件。
- `--format md|html` 产出人工阅读稿：按天分组、图片内嵌、链接卡片、转发子消息缩进展示、语音给时长与文件；渲染上限 20000 条，超大范围请先用 JSONL。
- `--index` 在解密副本上补查询索引（`conversation_id, send_time` 与 `send_time`）：一次性约 4 s / +24 MB，按会话查询从 1.3 s 降到毫秒级；删掉解密副本即回收。
- `history` 默认带 `--max-age 600`：解密产物还在、源库大小/时间戳没变就跳过复制+解密（首次 30 s → 复跑 0.2 s）；`--refresh` 强制重建，`--reuse` 保持原语义。
- `--copy-media` 优先用硬链接落地附件（同卷零拷贝、不额外占盘），跨卷自动回退为复制。
- key 默认不回显，显式使用 `--reveal-key` 才输出明文。

Agent 使用说明见 [SKILL.md](skills/open-wechat-cli/SKILL.md)。版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 数据和运行边界

工具不注入、不 hook、不附加调试器，不写目标进程或客户端数据库，也不发送消息。读取权限及兼容性由 Windows 和客户端版本决定；大库操作会产生磁盘与 CPU 开销。

数据库和 WAL 是顺序共享读副本，**不是同一时点快照保证**。解密结果经过 SQLite `quick_check`，但结构检查不等于完整事务一致性或内容真实性证明。

key 会保存在本地状态文件中；快照、明文库、导出和复制出来的附件均包含敏感数据。Unix 上尝试收紧 key 文件权限；Windows 的 `chmod` 不等于 NTFS ACL 隔离。请将这些数据保存在受保护的本地目录，不要提交到公开仓库。

## 排障

- `No Python at ...`：先用 `py -3 -c "import sys; print(sys.executable)"` 检查可用解释器。基础 Python 卸载、移动或升级后，旧 venv 可能失效；保留工作数据，用有效解释器新建环境并重新安装。
- `ModuleNotFoundError: open_wechat_cli`：确认使用安装本项目的同一个 Python 环境，执行 `python -m pip install .`。
- 取 key 失败：确认目标账号已登录，检查 `owc doctor`，再按错误中的 `repair` 处理。
- 解密检查失败：重新复制数据库；不要把失败产物视为可用历史。
- 附件没有路径：对照 `CacheMapping` 是否还在、该图片/语音是否已被客户端清理；`exists: false` 是事实，不是 bug。

## 许可与依赖

项目代码、测试和公开文档使用 [MIT License](LICENSE)。运行依赖保留各自许可，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

