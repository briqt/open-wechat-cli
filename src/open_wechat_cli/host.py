"""Windows 企业微信的"环境事实"：装在哪、版本多少、有哪些账号、每个账号有哪些库。

只读：只列目录、读 Config.cfg、读文件大小与时间，不打开任何客户端文件做写操作。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import winproc
from .errors import PreconditionError

DEFAULT_DATA_ROOT = Path(os.environ.get("USERPROFILE", "")) / "Documents" / "WXWork"
INSTALL_CANDIDATES = (
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "WXWork",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WXWork",
)

#: 与只读查询直接相关的库；Index 下的库走同一把 key
CORE_DBS = ("message.db", "session.db", "user.db", "file.db", "message_lookup.db")


@dataclass
class DbFile:
    rel: str
    path: str
    size: int
    mtime: float
    has_wal: bool

    def to_dict(self) -> dict:
        return {
            "rel": self.rel,
            "size": self.size,
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.mtime)),
            "has_wal": self.has_wal,
        }


@dataclass
class Account:
    corp_id: str
    root: str
    data_dir: str
    config: dict = field(default_factory=dict)
    dbs: list[DbFile] = field(default_factory=list)

    @property
    def config_path(self) -> str:
        return os.path.join(self.root, "Config.cfg")

    @property
    def is_chat_account(self) -> bool:
        return any(d.rel.endswith("message.db") for d in self.dbs)

    @property
    def last_activity(self) -> float:
        return max((d.mtime for d in self.dbs), default=0.0)

    def db(self, name: str) -> DbFile | None:
        for d in self.dbs:
            if d.rel == f"Data/{name}" or d.rel.endswith("/" + name):
                return d
        return None

    def to_dict(self) -> dict:
        return {
            "corp_id": self.corp_id,
            "root": self.root,
            "login_user_global_id": self.config.get("LoginUserGlobalId"),
            "login_company_id": self.config.get("LoginCompanyId"),
            "db_key_version": self.config.get("DbKeyVerKey"),
            "has_message_db": self.is_chat_account,
            "last_activity": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(self.last_activity)
            ) if self.last_activity else None,
            "dbs": [d.to_dict() for d in self.dbs],
        }


@dataclass
class Install:
    root: str
    exe: str
    version: str | None

    def to_dict(self) -> dict:
        return {"root": self.root, "exe": self.exe, "version": self.version}


def data_root() -> Path:
    override = os.environ.get("OWC_WECOM_DATA")
    if override:
        return Path(override)
    return DEFAULT_DATA_ROOT


def detect_installs() -> list[Install]:
    """找安装目录；版本号优先取安装目录下的版本子目录，其次文件名版本资源。"""
    out: list[Install] = []
    for root in INSTALL_CANDIDATES:
        exe = root / "WXWork.exe"
        if not exe.exists():
            continue
        version = None
        try:
            subdirs = [p.name for p in root.iterdir() if p.is_dir()]
        except OSError:
            subdirs = []
        versions = [s for s in subdirs if s[:1].isdigit() and s.count(".") >= 2]
        if versions:
            version = sorted(versions, key=lambda s: [int(x) if x.isdigit() else 0 for x in s.split(".")])[-1]
        out.append(Install(root=str(root), exe=str(exe), version=version))
    return out


def _read_config(path: str) -> dict:
    """Config.cfg 是带 BOM 的 JSON；解析失败时安静降级为空配置。"""
    try:
        raw = Path(path).read_bytes().decode("utf-8-sig", errors="replace")
    except OSError:
        return {}
    start = raw.find("{")
    if start < 0:
        return {}
    try:
        data = json.loads(raw[start:])
    except json.JSONDecodeError:
        return {}
    cfg = data.get("config")
    return cfg if isinstance(cfg, dict) else {}


def _inventory(account_root: Path) -> list[DbFile]:
    out: list[DbFile] = []
    for sub in ("Data", "Index"):
        d = account_root / sub
        if not d.is_dir():
            continue
        try:
            entries = sorted(d.glob("*.db"))
        except OSError:
            continue
        for f in entries:
            try:
                st = f.stat()
            except OSError:
                continue
            out.append(
                DbFile(
                    rel=f"{sub}/{f.name}",
                    path=str(f),
                    size=st.st_size,
                    mtime=st.st_mtime,
                    has_wal=(d / (f.name + "-wal")).exists(),
                )
            )
    return out


def list_accounts(root: Path | None = None) -> list[Account]:
    base = Path(root) if root else data_root()
    if not base.is_dir():
        raise PreconditionError(
            f"没找到企业微信数据目录：{base}",
            repair="确认客户端已登录过；数据目录一般固定在 %USERPROFILE%\\Documents\\WXWork",
            details={"data_root": str(base)},
        )
    accounts: list[Account] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir() or not entry.name.isdigit():
            continue
        data_dir = entry / "Data"
        if not data_dir.is_dir():
            continue
        accounts.append(
            Account(
                corp_id=entry.name,
                root=str(entry),
                data_dir=str(data_dir),
                config=_read_config(str(entry / "Config.cfg")),
                dbs=_inventory(entry),
            )
        )
    accounts.sort(key=lambda a: a.last_activity, reverse=True)
    return accounts


def select_account(accounts: list[Account], wanted: str | None) -> Account:
    """按 corp id 选账号；不给就挑最近有活动的、且带 message.db 的。"""
    if not accounts:
        raise PreconditionError(
            "没有可用账号目录",
            repair="先登录一次企业微信，让 Data 目录生成出来",
        )
    if wanted:
        for a in accounts:
            if a.corp_id == wanted:
                return a
        raise PreconditionError(
            f"账号目录里没有 {wanted}",
            repair="用 `owc accounts` 看可用 corp id",
            details={"available": [a.corp_id for a in accounts]},
        )
    chat = [a for a in accounts if a.is_chat_account]
    pool = chat or accounts
    return max(pool, key=lambda a: a.last_activity)


def running_processes() -> list[dict]:
    if not winproc.IS_WINDOWS:
        return []
    out = []
    for pid in winproc.find_pids():
        info = winproc.process_details(pid)
        info["pid"] = pid
        out.append(info)
    return out


def environment() -> dict:
    """doctor 用的环境快照。"""
    from . import codec  # 延迟导入，避免在非 Windows 环境下也碰 crypto

    root = data_root()
    accounts: list[Account] = []
    accounts_error = None
    try:
        accounts = list_accounts(root)
    except PreconditionError as exc:
        accounts_error = exc.message

    return {
        "platform": "windows" if os.name == "nt" else os.name,
        "python": {"version": __import__("sys").version.split()[0],
                   "executable": __import__("sys").executable},
        "pycryptodome": codec.AES is not None,
        "client_installs": [i.to_dict() for i in detect_installs()],
        "running": running_processes(),
        "data_root": str(root),
        "data_root_exists": root.is_dir(),
        "accounts": [a.to_dict() for a in accounts],
        "accounts_error": accounts_error,
    }
