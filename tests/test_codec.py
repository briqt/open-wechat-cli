"""codec 自证：合成密文 → 解密 → 校验头片段、WAL 回放与提交边界。

合成时严格按落盘布局构造，所以这一组测试同时说明了"我们以为的格式"和
"我们实现的解码器"是自洽的；真机验证另外由 live 测试覆盖。
"""

from __future__ import annotations

import struct

import pytest
from Crypto.Cipher import AES

from open_wechat_cli import codec


def encrypt_page(raw_key: bytes, plain: bytes, page_no: int) -> bytes:
    """按落盘布局把明文页加密成磁盘页。"""
    assert len(plain) == codec.PAGE_SIZE
    key = codec.derive_page_key(raw_key, page_no)
    iv = codec.page_iv(page_no)
    if page_no == 1:
        cipher = AES.new(key, AES.MODE_CBC, iv)
        body = cipher.encrypt(plain[16:])
        frag = plain[16:24]
        return codec.PAGE1_MAGIC + body[0:8] + frag + body[8:]
    return AES.new(key, AES.MODE_CBC, iv).encrypt(plain)


def make_page(no: int) -> bytes:
    page = bytearray((no * 7 + i) & 0xFF for i in range(codec.PAGE_SIZE))
    if no == 1:
        page[0:16] = codec.SQLITE_HDR
        page[16:18] = codec.PAGE_SIZE.to_bytes(2, "big")
        page[21:24] = bytes((0x40, 0x20, 0x20))
        page[100] = 0x0D
    return bytes(page)


KEY = bytes(range(16))


def test_page_key_and_iv_are_stable():
    assert codec.derive_page_key(KEY, 1).hex() == codec.derive_page_key(KEY, 1).hex()
    assert codec.derive_page_key(KEY, 1) != codec.derive_page_key(KEY, 2)
    assert codec.page_iv(1) != codec.page_iv(2)
    assert codec.page_iv(1) == codec.page_iv(1)


def test_page1_roundtrip_and_fragment(tmp_path):
    plain = make_page(1)
    disk = encrypt_page(KEY, plain, 1)
    assert disk[:8] == codec.PAGE1_MAGIC
    assert disk[16:24] == plain[16:24], "头片段必须是明文"
    back = codec.decrypt_page(KEY, disk, 1)
    assert back == plain
    assert codec.header_fragment_ok(disk)


def test_page1_wrong_key_fails(tmp_path):
    disk = encrypt_page(KEY, make_page(1), 1)
    bad = bytes(16)
    with pytest.raises(codec.FormatError):
        codec.decrypt_page(bad, disk, 1)


def test_other_pages_roundtrip():
    for no in (2, 3, 999):
        plain = make_page(no)
        disk = encrypt_page(KEY, plain, no)
        assert codec.decrypt_page(KEY, disk, no) == plain


def test_verify_key_on_file(tmp_path):
    db = tmp_path / "message.db"
    db.write_bytes(encrypt_page(KEY, make_page(1), 1) + encrypt_page(KEY, make_page(2), 2))
    assert codec.verify_key(KEY, db)
    assert not codec.verify_key(bytes(16), db)
    probe = codec.probe_db(db)
    assert probe["magic_ok"] and probe["header_fragment_ok"] and probe["pages"] == 2
    assert probe["page_size_field"] == codec.PAGE_SIZE


def test_verify_key_on_plaintext_sqlite(tmp_path):
    import sqlite3

    db = tmp_path / "plain.db"
    con = sqlite3.connect(db)
    con.execute("create table t(x)")
    con.commit()
    con.close()
    assert codec.verify_key(b"anything", db) is True
    assert codec.probe_db(db)["plaintext"] is True


def build_wal(path, raw_key: bytes, frames: list[tuple[int, bytes]], dbsize_map: dict[int, int]):
    salt1, salt2 = 0x11223344, 0x55667788
    header = bytearray(32)
    header[0:4] = struct.pack(">I", codec.WAL_MAGIC_BE)
    header[4:8] = struct.pack(">I", 3007000)
    header[8:12] = struct.pack(">I", codec.PAGE_SIZE)
    header[16:20] = struct.pack(">I", salt1)
    header[20:24] = struct.pack(">I", salt2)
    s0, s1 = codec._wal_checksum((0, 0), bytes(header[0:24]))
    header[24:28] = struct.pack(">I", s0)
    header[28:32] = struct.pack(">I", s1)

    out = bytearray(header)
    state = (s0, s1)
    for idx, (page_no, plain) in enumerate(frames, start=1):
        enc = encrypt_page(raw_key, plain, page_no)
        fh = bytearray(24)
        fh[0:4] = struct.pack(">I", page_no)
        fh[4:8] = struct.pack(">I", dbsize_map.get(idx, 0))
        fh[8:12] = struct.pack(">I", salt1)
        fh[12:16] = struct.pack(">I", salt2)
        state = codec._wal_checksum(state, bytes(fh[0:8]) + enc)
        fh[16:20] = struct.pack(">I", state[0])
        fh[20:24] = struct.pack(">I", state[1])
        out += fh + enc
    path.write_bytes(bytes(out))
    return path


def test_wal_replay_respects_commit_boundary(tmp_path):
    db = tmp_path / "db"
    db.write_bytes(b"".join(encrypt_page(KEY, make_page(n), n) for n in (1, 2, 3)))

    committed = make_page(2)
    committed = b"COMMITTED" + committed[9:]
    uncommitted = b"UNCOMMITT" + make_page(2)[9:]
    wal = build_wal(
        tmp_path / "db-wal",
        KEY,
        [(2, committed), (2, uncommitted)],
        dbsize_map={1: 3},
    )

    info = codec.inspect_wal(wal, KEY)
    assert info.frames == 2
    assert info.last_commit_frame == 1, "只应回放到最后一个提交帧"
    assert info.valid_frames == 2

    codec.apply_wal(KEY, wal, db)
    landed = db.read_bytes()[codec.PAGE_SIZE:2 * codec.PAGE_SIZE]
    assert landed.startswith(b"COMMITTED"), "未提交事务不能落进结果"


def test_wal_checksum_semantics_detected(tmp_path):
    frames = [(2, make_page(2)), (3, make_page(3))]
    wal = build_wal(tmp_path / "w", KEY, frames, dbsize_map={2: 3})
    info = codec.inspect_wal(wal, KEY)
    assert info.checksum_alg == "ciphertext", "本项目构造的 WAL 校验和覆盖密文"
