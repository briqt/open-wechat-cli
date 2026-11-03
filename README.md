# open-wechat-cli (`owc`)

只读读取本机已登录**企业微信（WXWork 5.x，Windows / macOS）**的会话、联系人、历史消息与本地附件缓存（图片、文件、语音、视频、链接卡片、合并转发聊天记录），供 AI Agent 与命令行直接检索、统计、总结和导出。

- **严格只读**：不修改客户端原库、不注入目标进程、不向任何外部服务回传数据；所有快照、解密缓存与导出产物均落在本机 `~/.config/agent-skills/open-wechat-cli/`。
- **跨平台支持**：原生支持 Windows（x86_64）与 macOS（Apple Silicon `arm64` / Intel `x86_64`）企业微信 5.x 客户端。
- **Agent 友好**：所有子命令支持 `--json` 单一 JSON 信封输出、错误码分级（`USAGE`/`PRECONDITION`/`PERMISSION`/`DEPENDENCY`/`DATA`）与修复建议（`repair`），支持直接按**群名/联系人姓名**过滤会话（`--conv`），并在缺少缓存时自动完成快照与解密。

---

## 快速安装（推荐：只需安装 Skill）

如果你通过 AI Agent（Claude Code、Cursor、Windsurf、OpenClaw、Antigravity 等）使用，**只需要安装本仓库的 Skill**：

```bash
npx skills add briqt/open-wechat-cli -g -y
```

> **无需手动安装 `owc` CLI**：安装 Skill 后，当你向 Agent 提出“查看/搜索/总结/导出企业微信聊天记录”等需求时，**Agent 会根据 Skill 指引自动检测当前系统架构，并从本仓库 [GitHub Releases](https://github.com/briqt/open-wechat-cli/releases/latest) 下载、校验、安装 `owc` 二进制到 `~/.local/bin/owc`**。

### 安装后可直接对 Agent 说：

- “帮我看看企业微信里最近活跃的群聊有哪些？”
- “总结一下【研发群】今天大家讨论了什么？”
- “在企业微信聊天记录里搜一下谁发过 `合同` 或相关文件”
- “把我和【张三】最近一周的聊天记录导出成 Markdown / HTML”
- “统计一下各个群最近 7 天的消息数量”

### 版本升级说明（同时更新 Skill 与 CLI）

当 `owc` 在命令行尾部提示 `[更新提示]` 或 `[公告]` 时，直接让 Agent 帮你升级，或按以下两步同时更新 Skill 文档与 `owc` 二进制：

1. **更新 Skill（`SKILL.md`）**：
   ```bash
   npx skills add briqt/open-wechat-cli -g -y
   ```
2. **更新 `owc` 二进制**：按下方对应平台的命令重新下载覆盖 `~/.local/bin/owc`（Windows 为 `owc.exe`），并执行 `owc --version` 核验。

---

## 可选：手动安装 `owc` CLI

仅当你**不通过 Agent**、希望自己在终端手敲 `owc` 命令时，才需要手动下载二进制（[GitHub Releases](https://github.com/briqt/open-wechat-cli/releases/latest)）：

### macOS（Apple Silicon / Intel）

```bash
mkdir -p ~/.local/bin
ARCH="$(uname -m)"
if [ "$ARCH" = "arm64" ]; then
  ASSET="owc-aarch64-apple-darwin"
else
  ASSET="owc-x86_64-apple-darwin"
fi
curl -fL "https://github.com/briqt/open-wechat-cli/releases/latest/download/${ASSET}" -o ~/.local/bin/owc
chmod +x ~/.local/bin/owc
export PATH="$HOME/.local/bin:$PATH"
owc --version
```

### Windows（PowerShell，x86_64）

```powershell
$DestDir = "$HOME\.local\bin"
New-Item -ItemType Directory -Force -Path $DestDir | Out-Null
Invoke-WebRequest -Uri "https://github.com/briqt/open-wechat-cli/releases/latest/download/owc-x86_64-pc-windows-msvc.exe" -OutFile "$DestDir\owc.exe"
$env:Path = "$DestDir;$env:Path"
owc --version
```

---

## 平台前置条件

1. 本机已安装并登录企业微信 5.x（`WXWork.exe` 或 `/Applications/企业微信.app`）。
2. **首次提取密钥时的系统权限要求**（密钥落盘到 `~/.config/agent-skills/open-wechat-cli/keys/<corp_id>.json` 后，后续快照、解密、查询与导出均**不再读取进程内存**）：
   - **Windows**：当前终端用户与 `WXWork.exe` 同属当前登录用户即可直接读取。
   - **macOS**：Apple Silicon / Intel 正式签名版企业微信受 Hardened Runtime 保护；若 `owc doctor` 显示 `memory_access_ok: false`，需满足以下任一条件完成**首次**密钥提取：
     - SIP 已允许调试（`csrutil status` 为 disabled）；或
     - 对本机 `/Applications/企业微信.app` 执行过一次性本地重签（`codesign --force --deep --sign - /Applications/企业微信.app`）并重启企业微信。

---

## CLI 常用命令速查

> 所有查询类子命令（`sessions list`、`contacts list`、`messages stats`、`messages list`、`messages export`、`schema`）均**无需手动传 `--db-dir`**，会自动复用或生成当前账号的解密缓存；`--conv` 支持直接传**群名/联系人姓名**或原始 `conv_id`（`R:...` / `S:...`）。

```bash
# 1. 环境自检（检查客户端、进程、账号、缓存密钥状态、语音转码依赖）
owc doctor --json

# 2. 一键拉取最新聊天记录（自动完成：取 key -> 快照 -> 解密 -> 查询）
owc history --limit 20 --json
owc history --conv "研发群" --since 2026-10-01 --query "发布" --limit 50 --json

# 3. 列出或搜索会话与联系人
owc sessions list --query "项目" --limit 50 --json
owc contacts list --query "张三" --limit 50 --json

# 4. 消息统计、条件检索与多格式导出（jsonl / txt / md / html）
owc messages stats --conv "研发群" --since 2026-10-01 --json
owc messages list --conv "研发群" --since 2026-10-01 --limit 50 --json
owc messages export --conv "研发群" --format md --copy-media --decode-voice --json
```

---

## 本机目录与配置说明

所有状态、配置、密钥与缓存均集中存放在 `~/.config/agent-skills/open-wechat-cli/`（可通过环境变量 `OWC_STATE_DIR` 覆盖）：

| 路径 | 用途 |
|---|---|
| `~/.config/agent-skills/open-wechat-cli/config.json` | 用户配置文件（可设置默认 `account`、`tz`、`silk_decoder`、`disable_update_check` 等） |
| `~/.config/agent-skills/open-wechat-cli/keys/<corp_id>.json` | 账号本地数据库密钥缓存（权限 `0600`） |
| `~/.config/agent-skills/open-wechat-cli/cache/<corp_id>/` | 只读快照（`snapshot/`）与解密后 SQLite 缓存（`decrypted/Data/`） |
| `~/.config/agent-skills/open-wechat-cli/export/` | `owc messages export` 默认导出目录 |

## 许可证

[MIT](./LICENSE)
