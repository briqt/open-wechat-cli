# 开发与仓库维护

## 从源码运行和修改

需要 Git 和 Python 3.10+。Windows 实时读取使用 Windows Python，而不是 WSL 的 Linux Python。

```powershell
git clone https://github.com/briqt/open-wechat-cli.git
cd open-wechat-cli
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\owc.exe --version
```

macOS / Linux 可运行离线逻辑和合成测试：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

## 目录

- `src/open_wechat_cli/`：程序实现；`cli.py` 负责入口，codec / snapshot / store 分别负责格式、复制与查询。
- `tests/`：合成数据库和离线测试，不使用真实聊天作为夹具。
- `skills/open-wechat-cli/`：Agent 调用说明，复用同一 CLI。
- `scripts/`：项目检查脚本。
- `.github/workflows/`：跨平台测试、构建和分发包检查。

## 两类文件

**开源并跟踪**：完整源码、测试、公开使用/开发说明、Skill、安装和依赖声明、CI、构建脚本、许可证及合成夹具。新增项目文件默认属于这一类。

**排除**：私有协作资料、本机配置和凭据、真实运行数据、虚拟环境、缓存及可重新构建的产物。规则集中在根 `.gitignore`，不使用忽略所有点文件的通配规则，以免藏掉项目配置。

不要把真实聊天、key、数据库或进程转储提交为测试样本。新增文件先检查 `git status --short` 和 `git diff --cached`；`.gitignore` 不是访问控制，也不会清除已经提交的内容。

公开文档不依赖维护者的本机环境或私有资料。安装、构建和测试应能从干净 clone 重现。

## 测试和构建

```bash
python -m pytest -q
python scripts/check_repository.py
python -m build
python scripts/check_repository.py --archives dist/*
```

可另用 `pip install --no-deps <wheel-path>` 检查本地 wheel 安装。构建产物不提交到 Git。测试依赖声明在 `.[dev]`；CI 另外安装 `build`，避免业务依赖包含构建工具。

自动化测试使用合成数据，不代表任何新的客户端版本已经真机验证。实时兼容性验证与普通 CI 分开，避免 CI 触及个人账号或聊天记录。

## Git 和发布

- `main` 是交付主线。提交记录描述实质变化；默认使用正常提交和 fast-forward 更新工作副本。
- 仅为移除已泄露资产或明确的历史修复重写已发布历史，普通整理不重写历史。
- 预览版本采用 Python PEP 440 版本号，例如 `0.1.0a2`，Git tag 为 `v0.1.0a2`。稳定版本为 `0.1.0` / `v0.1.0`。
- 发布前同步版本、更新 `CHANGELOG.md`、通过测试/构建/检查，再提交、推送和创建 tag。
- 已发布 tag 保持不变；修复使用新版本。预览 Release 标为 prerelease，不作为稳定 Latest。
- 当前发布入口为 GitHub 源码 tag，不发布到 PyPI，也不默认附带 wheel。wheel 仍可供开发者自行构建。

Git 工作副本用 `git clone` / `git pull --ff-only` 更新，避免用文件复制模拟 Git 同步。被 Git 排除的本地资料独立备份，不混入公开提交。

## 反馈与许可

可通过 Issues 报告问题；附上复现步骤、Python 和客户端版本，以及脱敏错误输出。不要附带原始数据库、key 或真实导出。

修改与贡献遵循 [MIT License](LICENSE)，第三方组件保留各自许可。
