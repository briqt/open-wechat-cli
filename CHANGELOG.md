# Changelog

## 0.1.0a9 — 2026-10-30

- 新增窄范围原子事实查询 `messages stats`：按时间窗口和会话聚合消息数、发言人数、首末时间与 content type 计数，不定义“活跃”“重要”等上层语义。
- Skill 增加 `messages stats` 路由、统计口径说明，以及 uv/pipx CLI 安装与 `.local\\bin\\owc.exe` 路径兜底。
- 保持能力边界：不新增活跃度、热度、真人讨论等结论型命令，避免 Agent 猜测内部 schema 或自行扫描大体量导出。
- 修复跨平台附件映射：Windows CacheMapping 中的反斜杠路径在 Linux/macOS 离线处理时统一为可解析路径，恢复导出和媒体测试在 CI 中的行为。

## 0.1.0a8 — 2026-10-30

- Skill 分发说明收口：Skill 默认已由 Agent 平台提供，CLI 独立通过 pipx 从 GitHub 安装；不要求使用者克隆源码仓库。
- 精简 Skill 的安装、前置检查和路由措辞，保留宽触发范围与实时/离线数据路径说明。
- frontmatter 改为面向用户意图的本地历史查询与导出描述，减少内部实现词汇对触发判断的干扰。

## 0.1.0a7 — 2026-10-30

- 最近消息导出：`messages export` 新增 `--limit` / `--order`，`--order desc --limit N` 取指定副本中的最近 N 条，查询层只读取所需数量，输出稿恢复时间正序。
- 新增导出可观测性：结果包含 `data_cutoff` 和 `freshness`（副本 mtime、年龄、快照创建时间）；副本超过 5 分钟时给出刷新提示。生成时间不再被当作数据最新时间。
- `--limit-per-conv` 明确仅适用于 TXT，其他格式返回 `USAGE`，避免参数被静默忽略。
- Agent/Skill 使用体验：补充意图到命令的路由表，区分实时 Windows WXWork 与 snapshot/decrypted 离线路径。
- 补充 JSON 成功/失败 envelope 示例、stdout/stderr 解析约定、PowerShell 可复制模板、history 后导出附件的二阶段流程。
- 补充 `USAGE` / `DEPENDENCY` 错误恢复说明，修正 `--key-file` 缺失行为描述，并明确 key、聊天正文和附件的敏感输出约束。
- 修正 `media decode` 错误 envelope 的 command 标识，使嵌套命令显示为 `media decode`。
- 新增 Agent 易用性与 Skill 审查记录，列出 argparse 错误统一、导出幂等、缓存签名和凭据保护等后续改进项。

## 0.1.0a5 — 2026-10-30

- 撤回残留媒体（content_type 22）：找回被撤回文件/视频的名字、体积、尺寸、时长、缩略图与本机源路径，`text` 如实写"已撤回的消息（270×480，20.2 MiB，xxx.mp4）"，不假装有正文。
- 老版天气卡片（132）只有 `6` 一段提示也能出正文；`_link()` 不再为全空字段造出空链接。
- 元信息（`17` 下 1042/1034 之外的子字段，如 `10018`）里的人话收进 `extra_text`；转发卡片只有 `4` 文本时也保留。
- 转发里的视频载荷除了 `103` 也认 `102`；子消息有载荷但解不出内容时标 `payload_empty`（撤回/客户端清空），走查按固有情况处理。
- 走查：全库分层取样结构缺口 7.1% → **6.6%**，结构性告警只剩 2 条老版引用块装饰串；三个群 1460 条 1 条误报。
- 测试 83 → 88（新增撤回残留媒体 2 个、老版天气、空 extra link、payload_empty）。

## 0.1.0a4 — 2026-10-30

- 导出路径：`--out` 改为可选，缺省落到 `<状态目录>/export/<会话或 all>/`；附件引用与 JSONL 的 `copy` 一律写成**相对路径**（资源在别处时用 `../assets/...`），再也不会把本机绝对路径写进 md/html/jsonl；JSONL 另存 `copy_abs` 便于程序回读。
- 抽取补齐：视频保留源路径（`202`）并把真实文件名（`20`）作为名字；被引用内容自己的 OCR（`17.1002.1.17.1042`）也挂到 `quote.ocr`；卡片里没认领的人话统一进 `extra_text`；未登记类型统一标 `other` 并走兜底抽取，不再整条丢弃。
- 走查：全库分层取样结构缺口 7.4% → **7.1%**，其中 4 类老版转发卡片仍有 12 条"引用块/图内文字"未覆盖；三个群 1460 条里 1 条误报。
- 测试 78 → 83（新增导出路径 5 个：默认目录、指定目录、media 在外部时用 `../`、JSONL 相对 `copy`、md 端到端）。

## 0.1.0a3 — 2026-10-30

- 结构化抽取重写：多段文本与 @提及不再只剩第一段；新增图片、文件、语音（时长 + 缓存文件）、视频、文档/公众号/小程序/微盘链接卡片，以及聊天记录（合并转发）的子消息、附件与卡片。
- 附件落地：新增 `CacheMapping` 映射，把消息里的资源键解析到 `Cache/Image|File|Voice|Video` 的真实文件；`messages list/export`、`sessions list`、`history` 支持 `--account` / `--media-root`，导出新增 `--copy-media` / `--media-out`（语音补 `.silk` 后缀）。
- 类型标签修正：`4 → chat_record`、`13 → doc_link`、`16 → voice`、`80 → rich_card`、`123 → rich_text`，其余卡片与系统类型补齐；`--query` 现在也检索链接标题/URL、附件名和转发子消息正文。
- 修复：`--key-file` 指向不存在或损坏的文件时给 `PRECONDITION`（并回退现场取 key），不再抛 `DATA` 未预期失败。
- 修复：输出重定向时 stdout/stderr 固定 UTF-8，中文内容与 emoji 不再产生非法字节或编码异常。
- 导出不再输出内部字段 `_cursor_id`。
- 新增 `--format md` / `--format html` 人工阅读稿：按天分组，图片内嵌，链接卡片给标题与 URL，聊天记录缩进展开子消息，语音给时长与文件；两种格式默认复制附件并引用相对路径（渲染上限 20000 条）。
- 抽取补齐：转发内的再转发（`107`）、表情（`106`）、卡片（`105`）、文档引用（`17.1034`）；图片 OCR（`17.1042`）；引用回复（`17.1002`）；卡片 JSON 里的人话；视频资源键在 `2` 且旧版文件名在 `107`；天气卡片（132）与群成员同步（516）识别；附件缺名时按 md5 合成名字并标明来源。
- 全库分层走查（6911 条取样）后结构缺口降到 9.2%，其中 70% 是"客户端没缓存附件"与"系统消息不落正文"两类固有情况；三个 daily-agent 群 1432 条里结构缺口 1 条。
- 性能：`_as_text` 改用 C 层控制字符正则，单条抽取 118 → 68 µs（type 80 从 93 → 27 µs）；`self_id()` 不再在 2 GB 消息表上跑 `GROUP BY`，`conversations()` 1583 → 49 ms；卡片 JSON 兜底与 URL 扫描按需触发。
- 新增 `--index`：在解密副本上补 `(conversation_id, send_time)` 与 `send_time` 索引，按会话查询 1295 → 7 ms（一次性 4 s / +24 MB）。
- `history` 新增 `--max-age`（默认 600 s）与 `--refresh`：源库指纹未变时复用解密产物，复跑 33.9 → 0.2 s；JSON 结果带 `reused_cache`。
- `--copy-media` 改为优先硬链接，跨卷回退复制（381 个 / 72.5 MiB：0.41 s → 0.05 s，且不额外占盘）。
- 确定性：渲染转发子消息时间改用消息自带时区偏移，输出不再随本机时区变化。
- 覆盖补齐：红包(26)、通话记录(40)、统计小结(82)、机器人卡片(212)、会议邀请(516)、文件卡片(8)、视频号(146)、老版转发子消息（发送者在 `6`、载荷在 `105.107`）、朋友圈通知里的文章卡片与图片、公众号摘要/账号名与正文图、表情来源名、视频文件名（拒绝把资源键当文件名）；卡片里没被认领的人话统一收进 `extra_text`。
- 撤回还原：新增 `revoke.py`，把 1011 撤回通知与 `message_revoke_record_table_v2` 对上，补出"该消息已被撤回"、撤回者与撤回时间，消息对象新增 `recalled` 字段。
- 语音转码：新增 `owc media decode`（SILK v3 → wav），解码器按模板/环境变量/PATH 自动发现，导出支持 `--decode-voice`；不内置解码器实现，缺失时给结构化 `DEPENDENCY` 与修复建议。
- 走查：三个 daily-agent 群 1460 条里结构缺口 1 条（误报）；全库分层取样 6911 条结构缺口 7.4%，其余为"附件未缓存"与"系统消息不落正文"两类固有情况。
- 新增消息内容结构说明（content_type 字段位置、附件缓存映射）与对应回归测试。

## 0.1.0a2 — 2026-10-29

- Establish the complete source distribution under the MIT License, with installation, development and release instructions.
- Add Windows/Linux Python CI, build checks and repository/distribution boundary tests.
- Include the CLI, tests, Agent Skill and public documents in reproducible source builds; keep generated artifacts out of Git.
- Clarify local key storage, shared-read copy consistency limits, and supported client scope.
- Use source tags as the release entry point; wheels remain buildable locally rather than a default Release attachment.

## 0.1.0a1 — 2026-10-29

- Initial Windows WXWork local-history reader: discovery, key verification, database copy/decode, session/contact/message queries and JSONL/TXT export.
- JSON result envelopes, structured runtime errors, cursors and explicit timestamps.
- Offline synthetic tests and a live compatibility observation for WXWork 5.0.13.6005 / PE32 / 4096-byte pages.
