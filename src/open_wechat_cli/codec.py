"""企业微信（WXWork 5.x）库格式：wxSQLite3 风格逐页 AES-128-CBC。

实测格式（Windows 5.0.13.6005）：

    page_size = 4096，文件长度恒为 4096 的整数倍

    page 1 落盘布局
      [0:8)    固定 magic 4c 23 a5 b5 0a c4 ef e7
      [8:16)   ciphertext[0:8)
      [16:24)  明文 P[16:24)  <-- 既是头片段也是 key 的 64 位校验器
      [24:)    ciphertext[8:)

    每页 key  = MD5(raw_key(16B) || page_no(uint32 LE) || b"sAlT")
    每页 IV   = MD5( 4 个 LE u32，由 modmult(52774,40692,3791,2147483399, n) 迭代产生 )
    加解密    = AES-128-CBC，无 HMAC

WAL 与个人微信（SQLCipher 4）完全不同：这里没有 salt 参与 key 派生。
"""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from typing import BinaryIO, Callable

from .errors import FormatError

try:  # pragma: no cover - 依赖缺失路径在 doctor 里显式报告
    from Crypto.Cipher import AES
except ImportError:  # pragma: no cover
    AES = None  # type: ignore[assignment]

PAGE_SIZE = 4096
SQLITE_HDR = b"SQLite format 3\x00"
PAGE1_MAGIC = bytes.fromhex("4c23a5b50ac4efe7")
KEY_SALT = b"sAlT"

WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
WAL_MAGIC_BE = 0x377F0682

#: page1 头片段之后必定是 0x40 0x20 0x20（payload 相关常量），用于快速排除
_HEADER_TAIL = (0x40, 0x20, 0x20)


class _NeedAes(RuntimeError):
    pass


def _aes():
    if AES is None:
        raise _NeedAes("pycryptodome 未安装")
    return AES


# --------------------------------------------------------------------------- #
# 密钥派生
# --------------------------------------------------------------------------- #
def _modmult(a: int, b: int, c: int, m: int, s: int) -> int:
    q = s // a
    v = b * (s - a * q) - c * q
    if v < 0:
        v += m
    return v & 0xFFFFFFFF


_IV_CACHE: dict[int, bytes] = {}


def page_iv(page_no: int) -> bytes:
    """每页 IV：与 SQLCipher 相同的 PRNG 迭代 + MD5。"""
    iv = _IV_CACHE.get(page_no)
    if iv is not None:
        return iv
    z = page_no + 1
    buf = bytearray()
    for _ in range(4):
        z = _modmult(52774, 40692, 3791, 2147483399, z)
        buf += struct.pack("<I", z)
    iv = hashlib.md5(bytes(buf)).digest()
    if len(_IV_CACHE) < 65536:
        _IV_CACHE[page_no] = iv
    return iv


def derive_page_key(raw_key: bytes, page_no: int) -> bytes:
    if len(raw_key) != 16:
        raise FormatError(
            f"key 长度应为 16 字节，实际 {len(raw_key)}",
            repair="用 `owc key scan` 重新取 key；key 是 16 字节 raw key，不是 32 字节 hex 字符串",
        )
    return hashlib.md5(raw_key + struct.pack("<I", page_no) + KEY_SALT).digest()


# --------------------------------------------------------------------------- #
# 单页解密
# --------------------------------------------------------------------------- #
def header_fragment_ok(page: bytes) -> bool:
    """page1 的 [16:24) 是否长得像 sqlite 头片段（10 00 02 02 00 40 20 20）。"""
    if len(page) < 24:
        return False
    page_size = (page[16] << 8) | page[17]
    if page_size == 1:
        page_size = 65536
    return (
        512 <= page_size <= 65536
        and page_size & (page_size - 1) == 0
        and page[21] == _HEADER_TAIL[0]
        and page[22] == _HEADER_TAIL[1]
        and page[23] == _HEADER_TAIL[2]
    )


def is_plaintext_page1(page: bytes) -> bool:
    return page.startswith(SQLITE_HDR)


def decrypt_page(raw_key: bytes, page: bytes, page_no: int) -> bytes:
    """解密单页。page1 用落盘布局单独处理。"""
    if len(page) < PAGE_SIZE:
        page = page + b"\x00" * (PAGE_SIZE - len(page))
    elif len(page) > PAGE_SIZE:
        page = page[:PAGE_SIZE]

    aes = _aes()
    if page_no == 1:
        if is_plaintext_page1(page):
            return page
        key = derive_page_key(raw_key, 1)
        work = bytearray(page)
        frag = bytes(work[16:24])
        work[16:24] = work[8:16]  # 把 [16:24) 的明文片段换成对应密文
        tail = aes.new(key, aes.MODE_CBC, page_iv(1)).decrypt(bytes(work[16:]))
        out = SQLITE_HDR + tail
        if out[16:24] != frag:
            raise FormatError(
                "page1 头片段校验失败：key 不对，或该库不是 wxSQLite3 格式",
                repair="先跑 `owc key verify --db <该库>`；若 key 正确仍失败，按 README 的常量锚点重新定位 codec",
            )
        return out

    return aes.new(derive_page_key(raw_key, page_no), aes.MODE_CBC, page_iv(page_no)).decrypt(page)


def verify_key(raw_key: bytes, db_path: str | os.PathLike[str]) -> bool:
    """用 page1 做端到端校验：必须解出 SQLite 头，且 b-tree 页类型合法。"""
    try:
        with open(db_path, "rb") as fh:
            page1 = fh.read(PAGE_SIZE)
    except OSError:
        return False
    if len(page1) < PAGE_SIZE:
        return False
    if is_plaintext_page1(page1):
        return True
    if not header_fragment_ok(page1):
        return False
    try:
        out = decrypt_page(raw_key, page1, 1)
    except Exception:
        return False
    return out.startswith(SQLITE_HDR) and out[100] in (0x02, 0x05, 0x0A, 0x0D)


def key_fingerprint(raw_key: bytes) -> str:
    """key 的对外指纹：可用来判断"是不是同一把 key"，不可用来还原 key。"""
    return hashlib.sha256(b"owc-key-v1" + raw_key).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 文件级
# --------------------------------------------------------------------------- #
def probe_db(db_path: str | os.PathLike[str]) -> dict:
    """不改动文件地看一眼格式：页大小、是否明文、magic、页数。"""
    size = os.path.getsize(db_path)
    with open(db_path, "rb") as fh:
        head = fh.read(PAGE_SIZE)
    info = {
        "path": os.fspath(db_path),
        "size": size,
        "pages": size // PAGE_SIZE,
        "size_aligned": size % PAGE_SIZE == 0,
        "plaintext": is_plaintext_page1(head),
        "magic": head[:8].hex() if len(head) >= 8 else "",
        "magic_ok": head[:8] == PAGE1_MAGIC,
        "header_fragment_ok": header_fragment_ok(head),
    }
    if len(head) >= 18 and header_fragment_ok(head):
        ps = (head[16] << 8) | head[17]
        info["page_size_field"] = 65536 if ps == 1 else ps
    return info


def decrypt_file(
    raw_key: bytes,
    src: str | os.PathLike[str],
    dst: str | os.PathLike[str],
    *,
    progress: Callable[[int, int], None] | None = None,
    chunk_pages: int = 512,
) -> dict:
    """逐页解密整个库。写入 dst（调用方负责临时文件与替换语义）。"""
    size = os.path.getsize(src)
    if size % PAGE_SIZE:
        raise FormatError(
            f"{os.path.basename(os.fspath(src))} 长度 {size} 不是 4096 的整数倍",
            repair="该库页大小可能不是 4096；用 `owc doctor` 看 page_size_field，再按 README 重新定位 codec",
            details={"size": size},
        )
    total_pages = size // PAGE_SIZE
    done = 0
    os.makedirs(os.path.dirname(os.fspath(dst)) or ".", exist_ok=True)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            buf = fi.read(PAGE_SIZE * chunk_pages)
            if not buf:
                break
            out = bytearray()
            for i in range(0, len(buf), PAGE_SIZE):
                page_no = done + i // PAGE_SIZE + 1
                out += decrypt_page(raw_key, buf[i:i + PAGE_SIZE], page_no)
            done += len(buf) // PAGE_SIZE
            fo.write(out)
            if progress:
                progress(done, total_pages)
    return {"src": os.fspath(src), "dst": os.fspath(dst), "pages": total_pages}


# --------------------------------------------------------------------------- #
# WAL
# --------------------------------------------------------------------------- #
@dataclass
class WalInfo:
    exists: bool
    size: int = 0
    pagesize: int = 0
    salt1: int = 0
    salt2: int = 0
    frames: int = 0
    last_commit_frame: int = 0
    valid_frames: int = 0
    applied_frames: int = 0
    checksum_alg: str = "unknown"
    header_ok: bool = False

    def to_dict(self) -> dict:
        return {
            "exists": self.exists,
            "size": self.size,
            "pagesize": self.pagesize,
            "frames": self.frames,
            "last_commit_frame": self.last_commit_frame,
            "valid_frames": self.valid_frames,
            "applied_frames": self.applied_frames,
            "checksum_alg": self.checksum_alg,
            "header_ok": self.header_ok,
        }


def _wal_checksum(prev: tuple[int, int], data: bytes) -> tuple[int, int]:
    s0, s1 = prev
    for i in range(0, len(data) - 7, 8):
        x0 = int.from_bytes(data[i:i + 4], "little")
        x1 = int.from_bytes(data[i + 4:i + 8], "little")
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def inspect_wal(
    wal_path: str | os.PathLike[str],
    raw_key: bytes | None = None,
    *,
    sample_frames: int = 64,
) -> WalInfo:
    """读 WAL 头 + 帧头，找出最后提交帧；顺带判断校验和算在密文还是明文上。

    两遍扫描：第一遍只走帧头（快，确定提交边界），第二遍抽样几十帧做校验和判定。
    只读，不改任何东西。
    """
    info = WalInfo(exists=os.path.exists(wal_path))
    if not info.exists:
        return info
    info.size = os.path.getsize(wal_path)
    if info.size <= WAL_HEADER_SIZE:
        return info

    with open(wal_path, "rb") as fh:
        hdr = fh.read(WAL_HEADER_SIZE)
        if len(hdr) < WAL_HEADER_SIZE:
            return info
        magic = int.from_bytes(hdr[0:4], "big")
        info.header_ok = (magic & 0xFFFFFFFE) == WAL_MAGIC_BE
        info.pagesize = int.from_bytes(hdr[8:12], "big") or PAGE_SIZE
        info.salt1 = int.from_bytes(hdr[16:20], "big")
        info.salt2 = int.from_bytes(hdr[20:24], "big")

        frame_size = WAL_FRAME_HEADER_SIZE + info.pagesize
        info.frames = (info.size - WAL_HEADER_SIZE) // frame_size

        # 第一遍：只读帧头，确定 valid_frames 与 last_commit_frame
        pos = WAL_HEADER_SIZE
        for frame_no in range(1, info.frames + 1):
            fh.seek(pos)
            h = fh.read(WAL_FRAME_HEADER_SIZE)
            if len(h) < WAL_FRAME_HEADER_SIZE:
                info.frames = frame_no - 1
                break
            dbsize = int.from_bytes(h[4:8], "big")
            fs1 = int.from_bytes(h[8:12], "big")
            fs2 = int.from_bytes(h[12:16], "big")
            if fs1 == info.salt1 and fs2 == info.salt2:
                info.valid_frames += 1
                if dbsize:
                    info.last_commit_frame = frame_no
            pos += frame_size

        # 第二遍：抽样若干帧，判断校验和覆盖的是密文还是明文
        if raw_key is not None and info.valid_frames:
            ct_ok = pt_ok = 0
            state = _wal_checksum((0, 0), hdr[0:24])
            if state == (int.from_bytes(hdr[24:28], "big"),
                         int.from_bytes(hdr[28:32], "big")):
                info.checksum_alg = "header_matches"
            pos = WAL_HEADER_SIZE
            for _ in range(min(sample_frames, info.frames)):
                fh.seek(pos)
                h = fh.read(WAL_FRAME_HEADER_SIZE)
                page = fh.read(info.pagesize)
                if len(h) < WAL_FRAME_HEADER_SIZE or len(page) < info.pagesize:
                    break
                pos += frame_size
                want = (int.from_bytes(h[16:20], "big"), int.from_bytes(h[20:24], "big"))
                state = _wal_checksum(state, h[0:8] + page)
                if state == want:
                    ct_ok += 1
                    continue
                page_no = int.from_bytes(h[0:4], "big")
                if page_no > 0:
                    try:
                        plain = decrypt_page(raw_key, page, page_no)
                    except Exception:
                        continue
                    if _wal_checksum((0, 0), h[0:8] + plain) == want:
                        pt_ok += 1
            if ct_ok or pt_ok:
                info.checksum_alg = "ciphertext" if ct_ok >= pt_ok else "plaintext"
    return info


def apply_wal(
    raw_key: bytes,
    wal_path: str | os.PathLike[str],
    db_path: str | os.PathLike[str],
    *,
    info: WalInfo | None = None,
) -> WalInfo:
    """把 WAL 里已提交但未 checkpoint 的页回放到已解密库。

    只回放到"最后一个提交帧"，避免把未提交事务写进结果。
    """
    info = info or inspect_wal(wal_path, raw_key)
    if not info.exists or info.frames == 0 or info.last_commit_frame == 0:
        # 没有提交帧的 WAL 只包含未完成事务；不能把它覆盖到结果库。
        return info
    with open(wal_path, "rb") as wf, open(db_path, "r+b") as df:
        hdr = wf.read(WAL_HEADER_SIZE)
        if len(hdr) < WAL_HEADER_SIZE:
            return info
        pagesize = int.from_bytes(hdr[8:12], "big") or PAGE_SIZE
        frame_size = WAL_FRAME_HEADER_SIZE + pagesize
        limit = info.last_commit_frame or info.frames
        applied = 0
        for _ in range(limit):
            fh_i = wf.read(WAL_FRAME_HEADER_SIZE)
            if len(fh_i) < WAL_FRAME_HEADER_SIZE:
                break
            page = wf.read(pagesize)
            if len(page) < pagesize:
                break
            page_no = int.from_bytes(fh_i[0:4], "big")
            fs1 = int.from_bytes(fh_i[8:12], "big")
            fs2 = int.from_bytes(fh_i[12:16], "big")
            if page_no <= 0 or fs1 != info.salt1 or fs2 != info.salt2:
                continue
            plain = decrypt_page(raw_key, page, page_no)
            df.seek((page_no - 1) * pagesize)
            df.write(plain)
            applied += 1
        info.applied_frames = applied
    return info
