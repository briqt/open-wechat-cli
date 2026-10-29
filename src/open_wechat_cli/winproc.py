"""Windows 只读进程访问原语（不改目标、不注入、不调试）。

只用 OpenProcess(PROCESS_VM_READ) + VirtualQueryEx + ReadProcessMemory：
默认按同用户权限尝试读取，不 attach、不注入、不 hook、不写目标进程；实际能否读取仍取决于 Windows 权限、进程保护和客户端版本。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
from dataclasses import dataclass
from typing import Iterator

from .errors import PreconditionError

IS_WINDOWS = os.name == "nt"

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
TH32CS_SNAPPROCESS = 0x00000002

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
MEM_IMAGE = 0x1000000
MEM_MAPPED = 0x40000
PAGE_GUARD = 0x100
PAGE_NOACCESS = 0x01


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("PartitionId", wt.WORD),
        ("_pad", wt.WORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * 260),
    ]


@dataclass(frozen=True)
class Region:
    base: int
    size: int
    protect: int
    type: int


def _k32():
    if not IS_WINDOWS:
        raise PreconditionError(
            "该操作只能在 Windows 上执行",
            repair="在企业微信所在的 Windows 主机上运行本命令；跨机请用 `owc snapshot --remote`",
        )
    return ctypes.WinDLL("kernel32", use_last_error=True)


def list_processes() -> list[tuple[int, str]]:
    """(pid, exe_name) 列表，走 Toolhelp 快照，不依赖 tasklist 输出编码。"""
    k32 = _k32()
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == -1:
        raise PreconditionError("CreateToolhelp32Snapshot 失败")
    out: list[tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), entry.szExeFile))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def find_pids(image_names: tuple[str, ...] = ("WXWork.exe", "WXWork")) -> list[int]:
    """按可执行名找进程（大小写不敏感）。"""
    names = {n.lower() for n in image_names}
    return [pid for pid, name in list_processes() if name.lower() in names]


def process_path(pid: int) -> str | None:
    """用 QueryFullProcessImageNameW 取进程路径（需要 PROCESS_QUERY_LIMITED_INFORMATION）。"""
    k32 = _k32()
    handle = k32.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        k32.CloseHandle(handle)


def open_process(pid: int):
    k32 = _k32()
    handle = k32.OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not handle:
        err = ctypes.get_last_error()
        raise PreconditionError(
            f"OpenProcess(pid={pid}) 失败，Win32 错误 {err}",
            repair="确认与客户端同一用户运行；跨用户/跨会话读取需要管理员命令行，且不属于默认支持路径",
            details={"pid": pid, "win32_error": err},
        )
    return handle


def iter_regions(handle, *, private_only: bool = True, max_region: int = 512 * 1024 * 1024) -> Iterator[Region]:
    """遍历目标进程已提交的可读内存区域。"""
    k32 = _k32()
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    limit = 0x7FFF0000  # 32 位目标用户空间上界；64 位目标下也只会遇到空洞
    while addr < limit:
        rc = k32.VirtualQueryEx(handle, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not rc:
            break
        base = mbi.BaseAddress or 0
        size = int(mbi.RegionSize)
        if size <= 0:
            break
        readable = (
            mbi.State == MEM_COMMIT
            and not (mbi.Protect & PAGE_GUARD)
            and (mbi.Protect & 0xFF) != PAGE_NOACCESS
        )
        if readable and size <= max_region:
            wanted = (mbi.Type == MEM_PRIVATE) if private_only else (mbi.Type in (MEM_PRIVATE, MEM_MAPPED))
            if wanted:
                yield Region(base, size, int(mbi.Protect), int(mbi.Type))
        addr = base + size


def read_memory(handle, address: int, size: int, buffer=None):
    k32 = _k32()
    if buffer is None:
        buffer = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(
        handle, ctypes.c_void_p(address), buffer, size, ctypes.byref(got)
    )
    if not ok:
        return None
    return buffer.raw[: got.value]


def read_int(handle, address: int, size: int) -> int | None:
    raw = read_memory(handle, address, size)
    if raw is None or len(raw) < size:
        return None
    return int.from_bytes(raw, "little")


def close(handle) -> None:
    if handle:
        _k32().CloseHandle(handle)


__all__ = [
    "IS_WINDOWS",
    "Region",
    "find_pids",
    "list_processes",
    "process_path",
    "open_process",
    "iter_regions",
    "read_memory",
    "close",
]


class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _filetime_to_epoch(ft: int) -> float:
    return (ft - 116444736000000000) / 10_000_000


def process_details(pid: int) -> dict:
    """进程路径 / 启动时间 / 内存占用，全部只读。"""
    import datetime as _dt

    k32 = _k32()
    info: dict = {"exe": None, "started": None, "working_set_mb": None, "private_mb": None}
    handle = k32.OpenProcess(0x0400 | 0x1000, False, pid)  # QUERY_INFORMATION | QUERY_LIMITED
    if not handle:
        return info
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(buf))
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            info["exe"] = buf.value
        ft = [wt.FILETIME() for _ in range(4)]
        if k32.GetProcessTimes(handle, *[ctypes.byref(x) for x in ft]):
            created = (ft[0].dwHighDateTime << 32) | ft[0].dwLowDateTime
            info["started"] = _dt.datetime.fromtimestamp(
                _filetime_to_epoch(created)
            ).strftime("%Y-%m-%dT%H:%M:%S")
        try:
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
            if psapi.GetProcessMemoryInfo(handle, ctypes.byref(pmc), pmc.cb):
                info["working_set_mb"] = round(pmc.WorkingSetSize / 1048576, 1)
                info["private_mb"] = round(pmc.PagefileUsage / 1048576, 1)
        except OSError:
            pass
    finally:
        k32.CloseHandle(handle)
    return info
