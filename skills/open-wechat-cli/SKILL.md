---
name: open-wechat-cli
description: 只读查询、搜索、统计、总结与导出本机企业微信（WXWork 5.x，Windows 与 macOS）本地历史聊天记录、群聊/单聊会话、通讯录联系人及本地缓存附件（图片、文件、语音、视频、链接卡片、合并转发记录）。当用户提到“看/查/搜企业微信聊天记录”“某群最近聊了什么”“总结某群今天/本周讨论”“搜一下企微里谁发过某个词/文件/链接”“导出我和某人或某群的聊天记录”“提取企微语音/图片/附件”“统计企微群消息量”，或给出 owc、WXWork 本地数据目录、snapshot、decrypted 目录时触发。仅用于读取本机企业微信历史；若用户要在线向企业微信发消息，改用 wecomcli-message。
---

# open-wechat-cli · 企业微信本地历史查询与导出

`owc` CLI 与 Skill 分开分发；用户只需安装本 Skill，**由 Agent 自动检查、下载安装并调用 `owc` 原生单文件二进制**完成所有本地企业微信历史读取与导出。

## 1. CLI 自动检查与按需安装（首次调用必检）

执行任何查询前，先检查本机 `owc` 是否已安装：

```bash
export PATH="$HOME/.local/bin:$PATH"
owc --version
```

若 `owc` 未安装，Agent **应直接自动从公开仓库 `briqt/open-wechat-cli` 的 GitHub Releases 下载对应平台的预编译单文件二进制**（零运行时依赖，无需 Python），不要让用户手动去装：

- **macOS（自动识别 Apple Silicon `arm64` / Intel `x86_64`）**：
  ```bash
  mkdir -p "$HOME/.local/bin"
  ARCH="$(uname -m)"
  if [ "$ARCH" = "arm64" ]; then
    ASSET="owc-aarch64-apple-darwin"
  else
    ASSET="owc-x86_64-apple-darwin"
  fi
  curl -fsSL "https://github.com/briqt/open-wechat-cli/releases/latest/download/${ASSET}" -o "$HOME/.local/bin/owc"
  chmod +x "$HOME/.local/bin/owc"
  xattr -d com.apple.quarantine "$HOME/.local/bin/owc" 2>/dev/null || true
  export PATH="$HOME/.local/bin:$PATH"
  owc --version
  ```
- **Windows（PowerShell，x86_64）**：
  ```powershell
  $InstallDir = "$env:USERPROFILE\.local\bin"
  New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
  Invoke-WebRequest -Uri "https://github.com/briqt/open-wechat-cli/releases/latest/download/owc-x86_64-pc-windows-msvc.exe" -OutFile "$InstallDir\owc.exe"
  $env:Path = "$InstallDir;$env:Path"
  owc --version
  ```
- **WSL（Windows Subsystem for Linux，调用宿主机 `owc.exe`）**：
  ```bash
  WIN_USER="$(cmd.exe /c "echo %USERNAME%" 2>/dev/null | tr -d '\r')"
  OWC_BIN="/mnt/c/Users/${WIN_USER}/.local/bin/owc.exe"
  mkdir -p "/mnt/c/Users/${WIN_USER}/.local/bin"
  if [ ! -f "$OWC_BIN" ]; then
    curl -fsSL "https://github.com/briqt/open-wechat-cli/releases/latest/download/owc-x86_64-pc-windows-msvc.exe" -o "$OWC_BIN"
  fi
  "$OWC_BIN" --version
  ```
- **版本更新提示处理（必须同时更新 Skill 与 CLI 二进制，不可只更其一）**：
  当 `owc` 输出 `[更新提示]` 或 `[公告]`（位于文本模式尾部或 `--json` 模式 stderr 的 `diag` 行）时，Agent **必须连续执行以下两步**：
  1. **更新 Skill 文档（`SKILL.md`）**：执行 `npx skills add briqt/open-wechat-cli -g -y`。
  2. **更新 `owc` CLI 二进制**：执行上方对应平台（macOS / Windows / WSL）的下载命令，从 `releases/latest/download/...` 覆盖本地 `~/.local/bin/owc`（或 `owc.exe`），并运行 `owc --version` 确认版本号已升至最新。

`owc` 所有运行时配置与有状态数据统一存放在 `~/.config/agent-skills/open-wechat-cli/`（含 `config.json`、`keys/<corp_id>.json`、`cache/<corp_id>/`、`export/`）。

## 2. 适用场景与边界

- **适用**：
  - 查看或搜索本机企业微信群聊/单聊历史消息、总结某群近期讨论、查找某人发过的文字/链接/文件/图片/语音。
  - 列出或按名称搜索企业微信本地会话列表（`sessions list`）、通讯录联系人（`contacts list`）。
  - 按会话或时间段统计消息条数、发言人数、首末时间与消息类型分布（`messages stats`）。
  - 将聊天记录连同本地缓存附件导出为 Markdown（`md`）、单文件网页（`html`）、结构化 `jsonl` 或按会话分文件的 `txt`，以及把 SILK v3 语音转码为 `wav`。
- **不适用**：
  - 向企业微信发送消息（请使用 `wecomcli-message`）。
  - 修改客户端数据库或设置、读取个人微信（WeChat）。

## 3. Agent 极简路由与命令速查

> **Agent 友好特性**：
> 1. **始终加 `--json`**：stdout 仅输出结构化 JSON 信封（`{"ok":true,"command":...,"data":...}`）；即便参数写错，stderr 最后一行也会输出结构化错误信封（`{"ok":false,"error":{"code":"USAGE","message":...,"repair":...}}`）。
> 2. **`--conv` 支持直接传名称**：`--conv` 既可传原始 `conv_id`（`R:...` / `S:...`），也可**直接传群名称或联系人姓名子串**（如 `--conv "研发群"` 或 `--conv "张三"`），`owc` 会自动解析匹配。
> 3. **`--db-dir` 全部可选**：`sessions list`、`contacts list`、`messages stats`、`messages list`、`messages export`、`schema` 均**不需要手动传 `--db-dir`**；省略时会自动复用 `~/.config/agent-skills/open-wechat-cli/cache/<corp_id>/decrypted/Data`，若尚未生成缓存则自动执行快照与解密。

| 用户意图 | 首选命令（直接复制执行） |
|---|---|
| 首次环境与账号自检 | `owc doctor --json` |
| 拉取最新/最近消息（默认 600s 缓存，要强制最新加 `--refresh`） | `owc history --conv "<群名/人名/conv_id>" --limit 30 --json` |
| 强制刷新最新快照后再查 | `owc history --refresh --conv "<群名/人名>" --since 2026-10-01 --limit 50 --json` |
| 搜索群聊或单聊会话 | `owc sessions list --query "<关键词>" --limit 50 --json` |
| 搜索本地联系人 | `owc contacts list --query "<姓名/别名>" --limit 50 --json` |
| 统计某会话或时间段消息量 | `owc messages stats --conv "<群名/人名>" --since 2026-10-01 --json` |
| 按会话 + 时间 + 关键词精确检索消息 | `owc messages list --conv "<群名/人名>" --since 2026-10-01 --query "<关键词>" --limit 50 --json` |
| 导出为 Markdown / HTML 阅读稿（自动附带图片等媒体） | `owc messages export --conv "<群名/人名>" --since 2026-10-01 --format md --json` |
| 导出为 JSONL（供脚本或大范围分析） | `owc messages export --conv "<群名/人名>" --format jsonl --copy-media --json` |
| 导出并把 SILK 语音自动转 WAV | `owc messages export --conv "<群名/人名>" --format md --copy-media --decode-voice --json` |

### 翻页与排序说明

- `history` 默认 `--order desc`（从最新往回看），`messages list` 默认 `--order asc`（按时间正序看）。
- 当返回 `data.next_cursor` 非空时，将其原样传给 `--cursor <next_cursor>` 即可继续翻页（保持相同的 `--order` 与过滤参数）。
- 大库（数十万条消息）若需频繁按会话/时间过滤，可在命令上加 `--index`，会在解密副本上建立索引，后续查询降至毫秒级。

## 4. 前置检查与首次密钥缓存说明

首次运行 `owc doctor --json` 时关注：

1. `accounts[]`：自动挑选 `has_message_db: true` 且 `last_activity` 最新的 `corp_id`（多账号时可通过 `--account <corp_id>` 指定）。
2. `accounts[].key_status`：
   - 若为 `verified`：说明该账号的本地数据库密钥已缓存在 `~/.config/agent-skills/open-wechat-cli/keys/<corp_id>.json` 且通过本地库第 1 页校验。**此时即使企业微信客户端未运行，所有查询与导出命令也可直接执行**。
   - 若为 `missing` 或 `stale`：首次提取密钥需要企业微信客户端正在运行且已登录（Windows 直接读取同名用户下的 `WXWork.exe`；macOS 若 `memory_access_ok: false`，按 `doctor` 返回的修复提示处理 SIP 或对 `/Applications/企业微信.app` 执行一次性本地重签后运行 `owc history --json` 自动完成提取并缓存）。

## 5. 消息字段与富媒体结构解读

`history` 与 `messages list` 返回的 `data.items[]` 每条消息包含：

- **基础字段**：`message_id`、`conv_id`、`conv_name`（会话/群名称）、`sender_id`、`sender_name`（发送人姓名）、`send_time`、`send_time_epoch`、`content_type`、`type_label`、`text`。
- **附件（`media[]`）**：图片（`image`）、文件（`file`）、语音（`voice`）、视频（`video`）、表情（`emotion`）的信息位于 `media[]`（图文混排 `rich_text` 消息同时存在于 `media[]` 与 `entries[].media`），含 `kind`、`name`、`size`、`width`、`height`、`md5`、`duration_s`、`local_path`、`exists`（`exists: true` 时 `local_path` 为本机真实缓存路径）。
  - **读取图片/截图内容的最佳实践**：当消息包含图片（`type_label == "image"` 或 `media[]` 非空）且 `media[].exists == true` 时，**不要只看 `text`**；直接读取 `media[].local_path` 指向的本地图片文件（可通过看图工具或 OCR 工具读取截图内容；在 WSL 下用 `wslpath -u "<local_path>"` 将 `C:\Users\...` 转为 `/mnt/c/Users/...` 即可直接访问）。
- **链接与卡片（`link`）**：在线文档（`doc_link`）、公众号文章（`article`）、小程序（`miniprogram`）、微盘文件（`drive_file`）包含 `link{url, title, desc}`。
- **合并转发聊天记录（`forward`）**：`type_label: "chat_record"` 时，`forward{title, total, items[]}` 展开子消息（最多递归展开 3 层）。
- **引用回复（`quote`）与撤回信息（`recalled`）**：引用消息带 `quote{sender_name, text}`；撤回通知带 `recalled{revoker_id, revoked_at}`。
- **全文检索范围**：`--query <关键词>` 会同时匹配正文 `text`、链接标题/URL、附件文件名、图片本地 OCR 文字（`media[].ocr`）以及合并转发子消息正文。

## 6. 错误码与自愈处理

当命令退出码非 0 时，读取 stderr 最后一行的 JSON 错误信封 `error.{code, message, repair}`：

| `error.code` | 含义 | Agent 处理方式 |
|---|---|---|
| `USAGE` | 参数格式或时间格式有误 | 按 `error.repair` 修正参数后重跑（时间格式用 `YYYY-MM-DD` 或 `YYYY-MM-DDTHH:MM:SS`） |
| `PRECONDITION` | 客户端未运行 / 未登录 / 首次缺 key | 先运行 `owc doctor --json` 查看账号与 `key_status`，按 `repair` 提示用户或切换 `--account` |
| `PERMISSION` | macOS 缺完全磁盘访问权限或首次扫内存受限 | 向用户展示 `error.repair` 中的具体授权命令 |
| `DEPENDENCY` | 开启 `--decode-voice` 但未安装 `silk_v3_decoder` | 去掉 `--decode-voice` 先导出 `.silk` 原文件，或提示安装 `silk_v3_decoder` |
| `DATA` / `SNAPSHOT` | 活库写入剧烈导致快照页校验未过 | 加 `--refresh` 重跑一次即可自动重拍快照 |

## 7. 安全与隐私红线

- **只读原则**：绝不修改企业微信客户端原目录任何文件。
- **密钥不外露**：绝不使用 `--reveal-key` 把 `key_hex` 打印到对话上下文中。
- **按需展示**：向用户汇报查询结果时，优先按用户问题提炼摘要或给出导出文件路径，避免将无关私密聊天大段贴入上下文。
