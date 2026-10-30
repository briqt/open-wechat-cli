---
name: open-wechat-cli
description: 读取 Windows 企业微信（WXWork 5.x）本地历史——会话、联系人、消息、按时间/会话/关键词检索与导出，含图片、文件、语音、链接卡片与聊天记录（转发）的结构化抽取和附件落地。当用户说「看企业微信聊天记录」「查企微某个人/某个群的历史消息」「把企微消息导出成 jsonl/txt」「把群里的图片/语音/文件导出来」「统计企微消息」「企微 message.db 解密」时使用。只读，不发送、不改客户端。
---

# open-wechat-cli · 企业微信本地历史（Windows，只读）

本 Skill 是 `owc` CLI 的调用说明，不另写一套逻辑。所有能力都在 CLI 里，Skill 只负责触发、前置检查、命令选择与错误恢复。

## 什么时候用

- 需要**读**企业微信本地聊天记录、会话列表、联系人或按关键词/时间检索消息。
- 需要把消息导出成 JSONL 或按会话拆分的文本，包括图片、文件、语音、链接卡片和聊天记录（转发）。
- 需要排障：客户端版本、账号目录、库大小、解密是否通过 `quick_check`。

不适用：发送消息、改资料、改客户端、个人微信（SQLCipher 4 另一套格式）、macOS 版企业微信。

## 前置检查（每次先做，别跳）

```bash
owc doctor            # 或 owc doctor --json
```

如果 `owc` 启动失败，先区分环境错误：`No Python at ...` 检查基础解释器；`ModuleNotFoundError: open_wechat_cli` 检查本项目是否安装在当前 Python 环境中。不要仅凭错误现象推断解释器被系统删除：

```bash
py -3 -c "import sys; print(sys.executable)"    # 拿真实解释器
# 使用有效解释器新建 venv，再 python -m pip install .；开发安装见 CONTRIBUTING.md
```

判断：

1. `platform = windows` —— 其他平台只能做离线解密，取 key 必须在企业微信所在的 Windows 上跑。
2. `client_installs[].version` 有值，且 `running` 里能看到 `WXWork.exe`。**没运行 = 取不到 key**，key 只在登录期内存里，不落盘。
3. `accounts[]` 里挑 `has_message_db: true` 且 `last_activity` 最新的那个 `corp_id`。
4. `pycryptodome: true`；`tz_database: ok`（缺 `tzdata` 时只能 `--tz local`）。

## 主路径

```bash
owc history --account <corp_id> --limit 20 --tz Asia/Shanghai
```

一条命令串起定位账号、key、数据库复制、解密和查询。key 缓存含敏感信息；Unix 上尝试 chmod 0600，Windows 需依靠目录和 NTFS ACL 保护，不能把 chmod 当作隔离保证。

需要复用已有解密结果时加 `--reuse`；换缓存目录用 `--cache-dir`。

## 分步路径（排障或要复用产物时）

```bash
owc accounts -v
owc key scan --account <corp_id>                 # 只输出指纹；--reveal-key 才打印明文
owc key verify --account <corp_id> --db <某个 .db>
owc snapshot --account <corp_id> --out <dir> --dbs message.db,session.db,user.db
owc decrypt  --snapshot <dir> --out <dec>        # 每个库都要 quick_check=ok
owc schema   --db-dir <dec>/Data                 # 表/列映射；schema 变了先看这里
owc sessions list --db-dir <dec>/Data --limit 20
owc contacts list --db-dir <dec>/Data --query <关键词>
owc messages list --db-dir <dec>/Data --conv <conv_id> --since 2026-10-01 --limit 50
owc messages export --db-dir <dec>/Data --out <dir>/msgs.jsonl --since 2026-10-29 --copy-media
```

`--json` 可放在子命令前后任意位置：stdout 只有结果信封；stderr 是 diag 行 +（失败时）最后一行的单行错误体。解析时取 stderr 最后一行。

## 附件、链接与转发

- 图片/文件/语音/视频解析进 `media[]`。`--db-dir` 指向 `<cache>/decrypted/Data` 时会自动从快照 manifest 认出账号目录，用 `CacheMapping` 把资源键映射到 `Cache/Image|File|Voice|Video` 下的真实文件；也可用 `--media-root` 或 `--account` 显式指定。
- `media[]` 字段：`kind`（image/file/voice/video）、`name`、`size`、`width`、`height`、`md5`、`key`、`duration_s`（语音秒数）、`local_path`、`exists`。`exists: false` 表示客户端缓存已清理或从未下载，此时不给路径。
- 导出加 `--copy-media`（配合 `--media-out`）会把附件复制出来；语音复制时补 `.silk` 后缀（企微缓存无扩展名，内容是 silk 容器）。
- 链接卡片（文档、公众号、小程序、微盘）落在 `link{url,title,desc,icon}`，`type_label` 区分 `doc_link` / `article` / `miniprogram` / `drive_file` / `drive_notice`。
- 聊天记录/合并转发是 `chat_record`：`forward{title,total,members,items[]}`，子消息带 `sender_name / send_time_epoch / content_type / text / media / link / forward / entries / quote`（转发的转发最多展开三层）。
- 撤回通知（`system`/`recall`）如果有对应撤回记录，`text` 会是"该消息已被撤回"，`recalled{revoker_id,revoked_at}` 给撤回者与时间；拿不到就保持空正文，不猜。
- 引用回复落在 `quote{sender_name,title,text,forward,media}`；图片 OCR 落在 `media[].ocr`；卡片/小程序标题等附属文本落在 `extra_text`——它们不混进 `text`，但 `--query` 会一起检索。
- 语音是 SILK v3（`\x02#!SILK_V3`），播放前用 `owc media decode --in <目录> --out <目录>` 转 wav；解码器由外部提供（`--decoder` 模板 / `OWC_SILK_DECODER` / PATH 上的 `silk_v3_decoder`），导出加 `--decode-voice` 会在复制后顺手转码，缺解码器时给 `DEPENDENCY` 而不是静默跳过。
- 导出可以不给 `--out`：默认 `<状态目录>/export/<会话或 all>/`，附件在同级 `media/`；引用一律相对路径（`media/x.png` 或 `../assets/x.png`），JSONL 的 `copy` 相对导出文件、`copy_abs` 是绝对路径。
- 要给人看时用 `--format md`（Markdown）或 `--format html`（单文件 HTML，内嵌样式）：按天分组、图片内嵌、引用与转发展开、语音给时长与文件链接；这两种格式默认复制附件，图片引用相对路径。渲染上限 20000 条。

## 性能与重复执行

- 大库（2 GB / 80 万条）上"按会话/按时间"的查询默认是全表扫描（1.3 s）；加一次 `--index` 会在解密副本上补索引，之后同类查询 7 ms。索引属于派生数据，`owc decrypt` 重建副本后需重新加一次。
- `owc history` 默认 `--max-age 600`：解密产物还在且源库指纹未变就复用，复跑 0.2 s 而不是 30 s；要最新数据时报 `--refresh`。
- `--copy-media` 优先硬链接（同卷不占额外磁盘），跨卷才复制；重复导出不会重复落地同一文件。
- 抽取侧开销：单条约 68 µs（type 80 富文本 27 µs，聊天记录 640 µs），全库约 56 s；`owc messages` 每次都会读会话/联系人映射（约 50 ms），属正常固定成本。

## 结果怎么读

- 消息对象字段：`message_id / conv_id / conv_name / sender_id / sender_name / send_time / send_time_epoch / content_type / type_label / text / urls / ref / link / media / segments / entries / forward / voice`。
- `text: null` 不代表丢数据：附件看 `media`，链接看 `link`，转发看 `forward`，语音看 `voice`；`ref` 是资源键前 48 字符。
- `segments` 用于文本类：`kind: text|at`，`at` 带 `user_id` 与 `name`；多段文本已按顺序拼进 `text`。
- `entries` 是多元素消息（`rich_text`，文本与图片混排）的逐段结果。
- 分页：把 `next_cursor` 原样回填到 `--cursor`。`--order desc` 从最新往回翻，`--order asc` 从头往后翻，两者游标不混用。
- 统计口径：`--with-counts` 才会扫全库算总数（默认关，因为 2GB 库要几秒）。
- `--query` 检索正文 + 链接标题/URL + 附件名 + 转发子消息正文。

## 错误与修复

| code | 含义 | 怎么做 |
|---|---|---|
| `PRECONDITION` | 客户端没运行 / 没登录 / 平台不对 / key 文件缺失 | 先登录企业微信再重跑；`--key-file` 指到不存在的文件会回退现场取 key |
| `KEY_NOT_FOUND` | 内存里没有通过校验的 key | 确认账号当前已登录；加 `--step 1` 放宽对齐；`--time-budget` 去掉让它扫完 |
| `FORMAT_MISMATCH` | page1 结构或页大小不符 | 用 `owc key verify` 检查 key；保留脱敏的版本和错误信息，按不兼容处理，不输出未验证数据 |
| `SNAPSHOT` | 库被独占锁定 / 空间不足 | 关掉占用窗口；换 `--out` 到空间足够的盘；2GB 库边写边拷撕裂时重拍一次 |
| `DATA` | `quick_check` 不过 / 表列缺失 | 重跑 `owc snapshot`；仍失败看 `owc schema` 的列映射 |

## 边界（必须守住）

- **只读**：不发送消息、不改客户端文件、不写注册表、不建服务；写操作只发生在用户给的 `--out`/缓存目录。
- **key 不回显**：默认只输出 `key_fingerprint`；除非用户明确要明文，否则不加 `--reveal-key`。
- **不要把真实聊天内容贴进无关上下文**：需要展示时只给用户要求的片段，导出文件路径优先于正文。
- **不要用本地历史里的会话 id 直接发消息**：读取范围和发送授权是两件事。
- **附件是原文拷贝**：图片/语音/文件导出后就是真实内容，放在受保护目录，别提交到公开仓库。

## 已知限制

- 只验证了 5.0.13.6005 / Windows x86。其他版本先 `owc doctor` + `key verify` 再承诺可用性。
- 未被客户端缓存过的附件只有名称与字节数（`exists: false`）；语音未转码（silk 需自行转换），也没有本地 ASR。
- 撤回消息未还原；少数系统类型只做兜底文本抽取。
- 消息正文结构（每种 content_type 的字段位置）见项目私有知识库的「企微消息内容结构」。
