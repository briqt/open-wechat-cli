# open-wechat-cli

面向微信与企业微信的 CLI 与 Agent Skill：把本地历史读成结构化数据，供人和程序使用。

当前覆盖 **Windows 企业微信（WXWork 5.x）本地库的只读读取**：会话、联系人、消息、按时间/会话/关键词检索、JSONL 与纯文本导出。

这是兼容性原型，不是对所有客户端版本、架构和 Windows 权限配置的支持承诺。实现边界、数据流和兼容性门槛见 [设计说明](docs/design.md)。

## 状态

- `v0.1.0a1`：已在 Windows 企业微信 **5.0.13.6005（PE32，含 `.vmp0` 区段）**、`message.db` 2.0 GB / 515,476 页的真实库上跑通全链路；解密后 `pragma quick_check = ok`。
- 只有一个平台一个版本的真机验证记录。其他版本/架构未验证，不要据此推断兼容。
- 当前范围是**只读**：不发送、不修改客户端文件、不改注册表、不装服务。

## 原理（为什么不需要注入/调试）

企业微信的本地库是 wxSQLite3 风格的逐页 AES-128-CBC：

```
每页 key = MD5(raw_key(16B) ‖ page_no(uint32 LE) ‖ "sAlT")
每页 IV  = MD5(4 个 LE u32，由 modmult 迭代生成)
page1 落盘时 [16:24) 仍留着明文头片段 —— 既是校验器，也是取 key 时的 64 位过滤器
```

因此整条链路只需要：

1. **同用户权限**打开 `WXWork.exe`，用 `ReadProcessMemory` 扫私有内存里 16 字节窗口，用 page1 的已知明文命中校验；
2. 用**共享读句柄**把库（和 `-wal`）拷到工作目录；
3. 离线逐页解密，回放已提交的 WAL 帧，用 `quick_check` 做落盘门禁。

本实现不 attach 调试器、不注入、不 hook、不写目标进程或客户端文件；读取是否成功仍取决于 Windows 权限、进程保护和客户端版本。

## 安装

```bash
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e .
```

依赖：Python 3.10+、`pycryptodome`、`tzdata`（Windows 上 `zoneinfo` 需要它）。

### Windows：`No Python at ...` 怎么办

症状：装好的 `owc` 或 venv 里的 python 起不来，报 `No Python at '"C:\Users\...\WindowsApps\PythonSoftwareFoundation...\python.exe'`。

原因：venv 的 `pyvenv.cfg` 里 `home` 记录的是建 venv 时那个解释器。用 Microsoft Store 版 Python 建 venv 时，`home` 指向 `WindowsApps` 下的执行别名；别名被系统刷新/清理或 Store 包被移除后，venv 就失去基础解释器——**这是解释器入口的问题，不是本项目代码的问题**。

修复（三条命令）：

```powershell
py -3 -c "import sys; print(sys.executable)"   # 找到当前真实解释器，例如 C:\Program Files\Python312\python.exe
Remove-Item -Recurse -Force .venv              # 删掉坏 venv
& "C:\Program Files\Python312\python.exe" -m venv "C:\path\to\open-wechat-cli\.venv"
.venv\Scripts\python -m pip install -e .
```

根治：优先用 python.org 安装包（或用它建的 venv），不要依赖 Store 版别名。

## 用法

```bash
# 0) 环境自检：平台、客户端版本、进程、账号、依赖
owc doctor

# 1) 一条命令：取 key → 快照 → 解密 → 查询（默认按时间倒序给最新 N 条）
owc history --account <corp_id> --limit 20 --tz Asia/Shanghai

# 2) 或者分步来，便于复用与排障
owc accounts -v                                   # 账号目录与库清单
owc key scan --account <corp_id>                  # 扫内存取 key（只输出指纹，key 落 0600 文件）
owc snapshot --account <corp_id> --out ./snap --dbs message.db,session.db,user.db
owc decrypt  --snapshot ./snap --out ./dec
owc schema   --db-dir ./dec/Data                  # 表/列映射与统计
owc sessions list --db-dir ./dec/Data --limit 20
owc contacts list --db-dir ./dec/Data --query 张
owc messages list --db-dir ./dec/Data --conv 'R:123456' --since 2026-10-01 --limit 50
owc messages export --db-dir ./dec/Data --out ./export/day.jsonl --since 2026-10-29
```

所有命令都支持 `--json`：stdio 分开，stdout 只有结果信封，stderr 先出若干 `{"type":"diag"}` 行、失败时最后一行是错误信封（单行 JSON，直接取最后一行解析即可）。

## 输出契约

- 成功：`{"ok": true, "command": ..., "schema": 1, "data": {...}, "meta": {...}}`
- 失败：`{"ok": false, "error": {"code", "message", "repair", "details"}}`，单行 JSON 落在 stderr 最后一行，并带稳定退出码
  （2 参数 / 3 前置条件 / 4 找不到 key / 5 格式不符 / 6 快照 / 7 数据不可用 / 8 缺依赖）。
- 消息分页用不透明游标：把上次的 `next_cursor` 原样回填，不要自己拼。
- 时间一律带显式偏移（`--tz local` 或 IANA 名），`send_time_epoch` 同时给出。
- key 默认不回显：输出里只有 `key_fingerprint`，需要明文必须显式 `--reveal-key`。

## 只读边界

- 只读 `%USERPROFILE%\Documents\WXWork\<corp_id>\Data\*.db` 与对应 `-wal`；写入只发生在你自己的 `--out`/缓存目录。
- 不 attach、不注入、不改客户端二进制、不写注册表、不建服务与计划任务。
- 未登录账号的库拿不到 key（key 只在登录期内存里，落盘不存在）。
- 解密产物与导出文件是真实数据，仓库 `.gitignore` 已排除常见的 `snapshots/`、`decrypted/`、`exports/`、`out/`、`keys.json` 路径；请不要把自定义输出目录加入 Git。

## 已知限制

- 只验证了 5.0.13.6005 / Windows x86；页大小若不是 4096 会明确报错而不是产出垃圾。
- 图片/语音/文件本体不在 `message.db` 里（只有引用）。
- 撤回消息还原、富文本卡片的结构化字段尚未实现。
- macOS 版企业微信、个人微信不在当前实现范围。

## 开发

```bash
pip install -e ".[dev]"
pytest -q          # 36 个单测：codec 自证、WAL 提交边界、protobuf 抽取、schema 适配、CLI 契约
```

## 许可

尚未确定；在明确许可前不视为可再分发。
