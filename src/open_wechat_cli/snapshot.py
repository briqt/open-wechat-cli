"""一致性快照：在客户端运行时把库读出来，且不碰原文件。

两条路：
* 普通共享读（默认）：CreateFileW 带 FILE_SHARE_READ|WRITE|DELETE，与客户端并行读，
  不需要管理员。读的时候记前后 stat，用来判断"读的过程中有没有被改"。
* esentutl /y /vss（可选，需要管理员）：真正的一致快照，适合 2GB 级大库的高频写入期。

落盘全部写到调用方指定目录，原目录只读。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from .errors import PreconditionError, SnapshotError
from .host import Account

CHUNK = 8 * 1024 * 1024
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


@dataclass
class FileCopy:
    src: str
    dst: str
    size: int = 0
    sha256: str = ""
    mtime: float = 0.0
    changed_during_copy: bool = False
    duration_s: float = 0.0
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "size": self.size,
            "sha256": self.sha256,
            "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(self.mtime)) if self.mtime else None,
            "changed_during_copy": self.changed_during_copy,
            "duration_s": round(self.duration_s, 2),
            "truncated": self.truncated,
        }


@dataclass
class SnapshotResult:
    out_dir: str
    account: str
    files: dict[str, FileCopy] = field(default_factory=dict)
    wal: dict[str, FileCopy] = field(default_factory=dict)
    skipped: list[dict] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float = 0.0

    def to_dict(self) -> dict:
        return {
            "out_dir": self.out_dir,
            "account": self.account,
            "duration_s": round(self.finished - self.started, 2),
            "dbs": {k: v.to_dict() for k, v in self.files.items()},
            "wals": {k: v.to_dict() for k, v in self.wal.items()},
            "skipped": self.skipped,
        }


def _k32():
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateFileW.restype = wt.HANDLE
    dll.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                                wt.DWORD, wt.DWORD, wt.HANDLE]
    dll.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD,
                             ctypes.POINTER(wt.DWORD), ctypes.c_void_p]
    dll.CloseHandle.argtypes = [wt.HANDLE]
    return dll


def copy_shared(src: str, dst: str, *, chunk: int = CHUNK) -> FileCopy:
    """用共享读句柄拷贝一个可能被客户端独占打开的文件。"""
    if os.name != "nt":
        return _copy_plain(src, dst, chunk=chunk)

    k32 = _k32()
    try:
        before = os.stat(src)
    except OSError as exc:
        raise SnapshotError(f"无法 stat {src}: {exc}", details={"path": src})

    handle = k32.CreateFileW(
        src, GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, None,
    )
    if not handle or handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        raise SnapshotError(
            f"打开 {os.path.basename(src)} 失败（Win32 {err}）",
            repair="该库可能被独占锁定；关掉相关窗口重试，或改用管理员 + esentutl /vss 快照",
            details={"path": src, "win32_error": err},
        )

    h = hashlib.sha256()
    copied = 0
    t0 = time.time()
    buf = ctypes.create_string_buffer(chunk)
    got = wt.DWORD(0)
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    try:
        with open(dst, "wb") as out:
            while True:
                if not k32.ReadFile(handle, buf, chunk, ctypes.byref(got), None):
                    err = ctypes.get_last_error()
                    raise SnapshotError(
                        f"读取 {os.path.basename(src)} 中断（Win32 {err}）",
                        details={"path": src, "win32_error": err, "copied": copied},
                    )
                if not got.value:
                    break
                data = buf.raw[: got.value]
                out.write(data)
                h.update(data)
                copied += len(data)
    finally:
        k32.CloseHandle(handle)

    after = os.stat(src)
    changed = (after.st_size != before.st_size) or (after.st_mtime_ns != before.st_mtime_ns)
    return FileCopy(
        src=src,
        dst=dst,
        size=copied,
        sha256=h.hexdigest(),
        mtime=after.st_mtime,
        changed_during_copy=changed,
        duration_s=time.time() - t0,
        truncated=copied < after.st_size,
    )


def _copy_plain(src: str, dst: str, *, chunk: int = CHUNK) -> FileCopy:
    h = hashlib.sha256()
    copied = 0
    t0 = time.time()
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            data = fi.read(chunk)
            if not data:
                break
            fo.write(data)
            h.update(data)
            copied += len(data)
    st = os.stat(src)
    return FileCopy(src=src, dst=dst, size=copied, sha256=h.hexdigest(),
                    mtime=st.st_mtime, duration_s=time.time() - t0)


def _check_space(out_dir: Path, need: int) -> None:
    try:
        usage = shutil.disk_usage(out_dir)
    except OSError:
        return
    if usage.free < need * 1.1:
        raise SnapshotError(
            f"目标盘剩余空间不足：需要约 {need / 2**30:.1f} GiB，可用 {usage.free / 2**30:.1f} GiB",
            repair="换一个输出目录（--out），或只快照需要的库（--dbs session,user）",
        )


def snapshot_account(
    account: Account,
    out_dir: str,
    *,
    dbs: tuple[str, ...] | None = None,
    with_index: bool = False,
    progress=None,
) -> SnapshotResult:
    """把一个账号下指定的库 + 对应 -wal 拷到 out_dir，并写 manifest.json。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    wanted = tuple(dbs or ("message.db", "session.db", "user.db"))
    selected = []
    for d in account.dbs:
        name = d.rel.split("/")[-1]
        if d.rel.startswith("Index/"):
            if not with_index:
                continue
            if name not in wanted and "*" not in wanted:
                continue
        elif name not in wanted:
            continue
        selected.append(d)

    if not selected:
        raise SnapshotError(
            "没有匹配到要快照的库",
            repair="用 `owc accounts` 看账号下有哪些库；--dbs 用文件名，例如 --dbs message.db,session.db",
        )

    need = sum(d.size for d in selected) * 2
    _check_space(out, need)

    result = SnapshotResult(out_dir=str(out), account=account.corp_id)
    for d in selected:
        name = d.rel.split("/")[-1]
        if progress:
            progress(f"snapshot {d.rel} ({d.size / 2**20:.1f} MiB)")
        try:
            cp = copy_shared(d.path, str(out / d.rel))
            result.files[d.rel] = cp
        except SnapshotError as exc:
            result.skipped.append({"rel": d.rel, "reason": exc.message})
            continue
        wal_src = d.path + "-wal"
        if os.path.exists(wal_src):
            try:
                result.wal[d.rel] = copy_shared(wal_src, str(out / (d.rel + "-wal")))
            except SnapshotError as exc:
                result.skipped.append({"rel": d.rel + "-wal", "reason": exc.message})

    result.finished = time.time()
    manifest = {
        "schema": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "account": account.to_dict(),
        "files": result.to_dict(),
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def load_manifest(snapshot_dir: str) -> dict:
    p = Path(snapshot_dir) / "manifest.json"
    if not p.exists():
        raise SnapshotError(
            f"{snapshot_dir} 下没有 manifest.json",
            repair="用 `owc snapshot` 生成快照；手工拷贝的目录请补一份 manifest 或直接用 `owc decrypt --db-dir`",
        )
    return json.loads(p.read_text(encoding="utf-8"))


def iter_snapshot_dbs(snapshot_dir: str):
    """遍历快照里的 .db（跳过 -wal / -shm）。"""
    root = Path(snapshot_dir)
    for p in sorted(root.rglob("*.db")):
        if p.name.endswith(("-wal", "-shm")):
            continue
        yield p


def ensure_writable_dir(path: str) -> Path:
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PreconditionError(
            f"输出目录不可写：{path}",
            repair="换一个可写路径，例如 %TEMP%\\owc",
            details={"error": str(exc)},
        )
    return p
