"""极简 protobuf 解码：只做字段遍历与按路径取值，带严格边界检查。

企微消息正文是 protobuf；文本在不同消息类型里落在不同层，所以需要
"沿路径找第一个能解成可读文本的 bytes 字段"这种能力。
"""

from __future__ import annotations

MAX_DEPTH = 8
MAX_FIELDS = 4096
MAX_BYTES = 4 * 1024 * 1024

WT_VARINT, WT_64BIT, WT_LEN, WT_32BIT = 0, 1, 2, 5


class ProtobufError(ValueError):
    pass


def varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    val = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            raise ProtobufError("varint 过长")
    raise ProtobufError("varint 越界")


def fields(buf: bytes, *, limit: int = MAX_FIELDS) -> list[tuple[int, int, bytes | int]]:
    """返回 [(field_no, wire_type, value)]；value 对 LEN 是 bytes，对 varint 是 int。"""
    if len(buf) > MAX_BYTES:
        raise ProtobufError("消息体过大")
    out: list[tuple[int, int, bytes | int]] = []
    i = 0
    while i < len(buf) and len(out) < limit:
        key, i = varint(buf, i)
        fn, wt = key >> 3, key & 7
        if fn == 0:
            raise ProtobufError("field 号为 0")
        if wt == WT_VARINT:
            v, i = varint(buf, i)
            out.append((fn, wt, v))
        elif wt == WT_64BIT:
            if i + 8 > len(buf):
                raise ProtobufError("64bit 越界")
            out.append((fn, wt, buf[i:i + 8]))
            i += 8
        elif wt == WT_LEN:
            ln, i = varint(buf, i)
            if ln < 0 or i + ln > len(buf):
                raise ProtobufError("length-delimited 越界")
            out.append((fn, wt, buf[i:i + ln]))
            i += ln
        elif wt == WT_32BIT:
            if i + 4 > len(buf):
                raise ProtobufError("32bit 越界")
            out.append((fn, wt, buf[i:i + 4]))
            i += 4
        else:
            raise ProtobufError(f"不支持的 wire type {wt}")
    return out


def walk_path(buf: bytes, path: tuple[int, ...], *, depth: int = 0) -> bytes | None:
    """沿字段号路径取第一个 length-delimited 值。"""
    if depth > MAX_DEPTH:
        return None
    cur: bytes | None = buf
    for want in path:
        if cur is None:
            return None
        nxt = None
        try:
            for fn, wt, v in fields(cur):
                if fn == want and wt == WT_LEN and isinstance(v, bytes):
                    nxt = v
                    break
        except ProtobufError:
            return None
        if nxt is None:
            return None
        cur = nxt
    return cur


def iter_len_fields(buf: bytes) -> list[bytes]:
    try:
        return [v for _, wt, v in fields(buf) if wt == WT_LEN and isinstance(v, bytes)]
    except ProtobufError:
        return []


def looks_like_protobuf(buf: bytes) -> bool:
    try:
        fs = fields(buf, limit=32)
        return bool(fs)
    except ProtobufError:
        return False
