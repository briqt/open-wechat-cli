"""从运行中的企业微信进程内存里定位 raw key（16 字节）。

不是盲扫：page1 的 [16:24) 是真实明文头片段，等于一个免费的 64 位校验器。
对每个 16 字节窗口做一次 AES-CBC 首块解密，只有前 8 字节命中才做整页校验，
因此假阳性率约 2^-64，且命中后还能用同账号任意库二次确认。

整个过程只用 ReadProcessMemory：不 attach、不注入、不 hook、不改目标文件。
"""

from __future__ import annotations

import ctypes
import hashlib
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from . import codec, winproc
from .errors import KeyNotFoundError, PreconditionError

CHUNK = 4 * 1024 * 1024


@dataclass
class KeyHit:
    key: bytes
    pid: int
    address: int
    candidates: int
    scanned_bytes: int
    regions: int
    elapsed: float
    hits_total: int = 1
    process_path: str | None = None

    def to_public_dict(self) -> dict:
        """对外输出：只给指纹，不给 key。"""
        return {
            "key_fingerprint": codec.key_fingerprint(self.key),
            "pid": self.pid,
            "process_path": self.process_path,
            "address": hex(self.address),
            "candidates_tested": self.candidates,
            "scanned_mb": round(self.scanned_bytes / 1048576, 1),
            "regions": self.regions,
            "elapsed_s": round(self.elapsed, 2),
            "occurrences": self.hits_total,
        }


@dataclass
class ScanStats:
    candidates: int = 0
    scanned_bytes: int = 0
    regions: int = 0
    pids: list[int] = field(default_factory=list)


_ZERO16 = b"\x00" * 16


def _plausible(cand: bytes) -> bool:
    """挡掉零填充/重复字节窗口，减少无意义 AES 调用（随机 key 几乎不可能被误杀）。"""
    if cand == _ZERO16:
        return False
    if cand == cand[:1] * 16:
        return False
    return True


def _candidates(buf: memoryview, step: int) -> Iterable[tuple[int, bytes]]:
    end = len(buf) - 16
    for i in range(0, end + 1, step):
        cand = bytes(buf[i:i + 16])
        if _plausible(cand):
            yield i, cand


def hunt_key(
    target_db: str,
    *,
    pids: list[int] | None = None,
    step: int = 4,
    max_hits: int = 1,
    time_budget: float | None = None,
    progress: Callable[[str], None] | None = None,
) -> KeyHit:
    """扫描目标进程内存，返回第一个（或前 N 个）通过 page1 校验的 key。"""
    if not winproc.IS_WINDOWS:
        raise PreconditionError(
            "取 key 需要在企业微信所在的 Windows 主机上运行",
            repair="在 Windows 上执行 `owc key scan --account <id>`；导出与查询可以在别处离线做",
        )

    from Crypto.Cipher import AES  # 局部导入，便于先报友好依赖错误

    probe = codec.probe_db(target_db)
    if probe["plaintext"]:
        raise PreconditionError(
            "目标库已是明文，不需要取 key",
            details={"db": target_db},
        )
    if not probe["header_fragment_ok"]:
        raise PreconditionError(
            "目标库 page1 不像 wxSQLite3 加密格式",
            repair="确认 --db 指向企业微信 Data 目录下的 .db；若是个人微信库则不属于本命令范围",
            details=probe,
        )

    with open(target_db, "rb") as fh:
        page1 = fh.read(codec.PAGE_SIZE)
    frag = page1[16:24]
    ct16 = page1[8:16] + page1[24:32]
    iv1 = codec.page_iv(1)
    page1_key_suffix = b"\x01\x00\x00\x00" + codec.KEY_SALT

    pids = pids or winproc.find_pids()
    if not pids:
        raise PreconditionError(
            "没找到正在运行的 WXWork.exe",
            repair="先登录企业微信（key 只在登录期内存里，落盘不存）；登录后再跑一次",
        )

    stats = ScanStats(pids=list(pids))
    hits: list[KeyHit] = []
    started = time.time()

    for pid in pids:
        handle = winproc.open_process(pid)
        try:
            regions = list(winproc.iter_regions(handle, private_only=True))
            stats.regions += len(regions)
            if progress:
                total_mb = sum(r.size for r in regions) / 1048576
                progress(f"pid={pid} private regions={len(regions)} total={total_mb:.0f}MB")
            buf = ctypes.create_string_buffer(CHUNK)
            for region in regions:
                off = 0
                while off < region.size:
                    if time_budget is not None and time.time() - started > time_budget:
                        raise KeyNotFoundError(
                            "扫描时间预算用尽，未命中",
                            repair="去掉 --time-budget 让它扫完，或确认该账号当前已登录",
                            details={"candidates_tested": stats.candidates,
                                     "scanned_mb": round(stats.scanned_bytes / 1048576, 1)},
                        )
                    n = min(CHUNK, region.size - off)
                    raw = winproc.read_memory(handle, region.base + off, n, buf)
                    if raw:
                        stats.scanned_bytes += len(raw)
                        mv = memoryview(raw)
                        for idx, cand in _candidates(mv, step):
                            stats.candidates += 1
                            pk = hashlib.md5(cand + page1_key_suffix).digest()
                            blk = AES.new(pk, AES.MODE_CBC, iv1).decrypt(ct16)
                            if blk[:8] != frag:
                                continue
                            if codec.verify_key(cand, target_db):
                                hits.append(
                                    KeyHit(
                                        key=cand,
                                        pid=pid,
                                        address=region.base + off + idx,
                                        candidates=stats.candidates,
                                        scanned_bytes=stats.scanned_bytes,
                                        regions=stats.regions,
                                        elapsed=time.time() - started,
                                        hits_total=len(hits) + 1,
                                        process_path=winproc.process_path(pid),
                                    )
                                )
                                if progress:
                                    progress(
                                        f"hit key_fingerprint={codec.key_fingerprint(cand)} "
                                        f"at {hits[-1].address:#x} after {stats.candidates:,} candidates"
                                    )
                                if len(hits) >= max_hits:
                                    return hits[0]
                    # 区块重叠 16 字节，避免 key 跨块被切断
                    off += max(1, n - 16)
                    if n <= 16:
                        break
        finally:
            winproc.close(handle)

    raise KeyNotFoundError(
        "内存里没有找到能通过校验的 key",
        repair=(
            "确认该账号当前已登录（未登录账号的库拿不到 key）；"
            "可加 --step 1 放宽对齐，或 --all-regions 把映射区也纳入扫描"
        ),
        details={
            "candidates_tested": stats.candidates,
            "scanned_mb": round(stats.scanned_bytes / 1048576, 1),
            "pids": stats.pids,
            "elapsed_s": round(time.time() - started, 1),
        },
    )
