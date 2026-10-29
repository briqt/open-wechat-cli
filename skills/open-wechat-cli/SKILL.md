---
name: open-wechat-cli
description: 读取 Windows 企业微信（WXWork 5.x）本地历史——会话、联系人、消息、按时间/会话/关键词检索与导出。当用户说「看企业微信聊天记录」「查企微某个人/某个群的历史消息」「把企微消息导出成 jsonl/txt」「统计企微消息」「企微 message.db 解密」时使用。只读，不发送、不改客户端。
---

# open-wechat-cli · 企业微信本地历史（Windows，只读）

本 Skill 是 `owc` CLI 的调用说明，不另写一套逻辑。所有能力都在 CLI 里，Skill 只负责触发、前置检查、命令选择与错误恢复。

## 什么时候用

- 需要**读**企业微信本地聊天记录、会话列表、联系人或按关键词/时间检索消息。
- 需要把消息导出成 JSONL 或按会话拆分的文本。
- 需要排障：客户端版本、账号目录、库大小、解密是否通过 `quick_check`。

不适用：发送消息、改资料、改客户端、个人微信（SQLCipher 4 另一套格式）、macOS 版企业微信。

## 前置检查（每次先做，别跳）

```bash
owc doctor            # 或 owc doctor --json
```

如果 `owc` 连启动都失败（`No Python at ...`、`Scripts\python.exe` 不存在、`ModuleNotFoundError: open_wechat_cli`），先修解释器入口，不要怀疑业务逻辑：

```bash
py -3 -c "import sys; print(sys.executable)"    # 拿真实解释器
# 删掉旧 venv 用该解释器重建，再 pip install -e .
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

一条命令走完：定位账号 → 取 key（首次现场扫内存并落 0600 key 文件）→ 快照 → 解密 → 查询。

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
owc messages export --db-dir <dec>/Data --out <dir>/msgs.jsonl --since 2026-10-29
```

`--json` 可放在子命令前后任意位置：stdout 只有结果信封；stderr 是 diag 行 +（失败时）最后一行的单行错误体。解析时取 stderr 最后一行。

## 结果怎么读

- 消息对象字段：`message_id / conv_id / conv_name / sender_id / sender_name / send_time / send_time_epoch / content_type / type_label / text / urls / ref`。
- `text: null` 不代表丢数据：多半是图片/文件/链接卡片，`type_label` 会说明类型，`ref` 是资源 id，`urls` 是能抽到的链接。
- 分页：把 `next_cursor` 原样回填到 `--cursor`。`--order desc` 从最新往回翻，`--order asc` 从头往后翻，两者游标不混用。
- 统计口径：`--with-counts` 才会扫全库算总数（默认关，因为 2GB 库要几秒）。

## 错误与修复

| code | 含义 | 怎么做 |
|---|---|---|
| `PRECONDITION` | 客户端没运行 / 没登录 / 平台不对 | 先登录企业微信，再重跑；跨机请到 Windows 上跑取 key 那一步 |
| `KEY_NOT_FOUND` | 内存里没有通过校验的 key | 确认账号当前已登录；加 `--step 1` 放宽对齐；`--time-budget` 去掉让它扫完 |
| `FORMAT_MISMATCH` | page1 结构或页大小不符 | 客户端可能升级了；先 `owc key verify` 确认 key，再按 README「原理」里的常量锚点重新定位 codec |
| `SNAPSHOT` | 库被独占锁定 / 空间不足 | 关掉占用窗口；换 `--out` 到空间足够的盘；2GB 库边写边拷撕裂时重拍一次 |
| `DATA` | `quick_check` 不过 / 表列缺失 | 重跑 `owc snapshot`；仍失败看 `owc schema` 的列映射 |

## 边界（必须守住）

- **只读**：不发送消息、不改客户端文件、不写注册表、不建服务；写操作只发生在用户给的 `--out`/缓存目录。
- **key 不回显**：默认只输出 `key_fingerprint`；除非用户明确要明文，否则不加 `--reveal-key`。
- **不要把真实聊天内容贴进无关上下文**：需要展示时只给用户要求的片段，导出文件路径优先于正文。
- **不要用本地历史里的会话 id 直接发消息**：读取范围和发送授权是两件事。

## 已知限制

- 只验证了 5.0.13.6005 / Windows x86。其他版本先 `owc doctor` + `key verify` 再承诺可用性。
- 图片/语音/文件本体不在 `message.db`；撤回消息还原、富文本卡片结构化字段未实现。
