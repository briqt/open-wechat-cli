# Changelog

## 0.1.2 — 2026-11-22

- 更新检查：只有远端版本**高于**本地时才提示（按 SemVer 比较，不再提示降级）；本地比清单更新时不再展示旧公告。清单 `notice.json` 增加 `schema_version`，并校验版本号、发布链接与公告长度，公告去掉控制字符；缓存超过 7 天不再用于提示，公告撤回后随即停止展示。
- 更新提示与公告改写到 stderr（文本模式下也不再混入 stdout），缓存文件改为原子写入。
- 配置只读运行时目录的 `config.json`（`~/.config/agent-skills/open-wechat-cli/`，`OWC_STATE_DIR` 可改位置），不再读取当前目录或 skill 目录里的 `.config.json`；配置文件无效时报 `PRECONDITION` 错误并给出修复方式，不再静默忽略。
- Skill：description 按能力边界改写；收到更新提示时先告知用户，在已有授权内才升级，不再自动执行升级；SKILL.md 增加 `metadata.version`。
- 发布流程：先发布带二进制的新 Release、再清理旧 Release、最后更新 `notice.json`，避免 `releases/latest/download` 出现空窗；`release_url` 指向具体版本。

## 0.1.1 — 2026-11-03

- 修复图片与富媒体本地缓存路径解析：Windows 独立图片 `_thumb` / `file_md5` 映射、`*1*` 附件键与视频元数据。
- 更新检查在 Windows 上兼容 Schannel 代理（`--ssl-no-revoke`），并设置连接与总超时。

## 0.1.0 — 2026-11-02

首个正式版本：只读查询、搜索、统计与导出本机企业微信（WXWork 5.x，Windows 与 macOS）本地历史，支持附件提取与 SILK 语音转码。
