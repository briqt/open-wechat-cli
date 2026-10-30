"""资源键 → 本地缓存文件的映射测试（合成 CacheMapping，不碰真实客户端数据）。"""

from __future__ import annotations

import sqlite3

from open_wechat_cli import media, store


def _make_account(tmp_path):
    root = tmp_path / "acct"
    (root / "CacheMapping").mkdir(parents=True)
    img_dir = root / "Cache" / "Image" / "2026-10"
    img_dir.mkdir(parents=True)
    img = img_dir / "shot.png"
    img.write_bytes(b"\x89PNG" + b"0" * 60)
    voice_dir = root / "Cache" / "Voice" / "2026-10"
    voice_dir.mkdir(parents=True)
    voice = voice_dir / "abc-voice"
    voice.write_bytes(b"#!SILK" + b"1" * 40)
    external = tmp_path / "外置文件.pdf"
    external.write_bytes(b"%PDF" + b"2" * 30)

    con = sqlite3.connect(root / "CacheMapping" / "map.db")
    con.execute(
        "create table mapping(type integer, key text, file_name text,"
        " last_modify_time integer, file_md5 integer, primary key (type, key))"
    )
    con.executemany(
        "insert into mapping values (?,?,?,?,?)",
        [
            (2, "ab" * 40, "2026-10\\shot.png", 0, 0),
            (3, "cd" * 40, "2026-10\\abc-voice", 0, 0),
            (1, "ef" * 40, str(external), 0, 0),
        ],
    )
    con.commit()
    con.close()
    return root


def test_resolve_kinds_and_absolute_paths(tmp_path):
    root = _make_account(tmp_path)
    idx = media.MediaIndex.from_account_root(root)
    assert idx is not None and len(idx) == 3

    image = idx.resolve("ab" * 40)
    assert image["kind"] == "image" and image["exists"] is True
    assert image["path"].endswith("shot.png") and image["size"] == 64

    voice = idx.resolve("cd" * 40)
    assert voice["kind"] == "voice" and voice["path"].endswith("abc-voice")

    document = idx.resolve("ef" * 40)
    assert document["kind"] == "file" and document["path"].endswith("外置文件.pdf")

    assert idx.resolve("ff" * 40) is None
    assert idx.resolve(None) is None


def test_missing_or_empty_mapping_returns_none(tmp_path):
    assert media.MediaIndex.from_account_root(tmp_path / "nope") is None
    empty = tmp_path / "acct2"
    (empty / "CacheMapping").mkdir(parents=True)
    assert media.MediaIndex.from_account_root(empty) is None


def test_is_ascii_hex():
    assert media.is_ascii_hex(b"3069020102043069")
    assert media.is_ascii_hex("3" * 40)
    assert not media.is_ascii_hex(b"306902010204")       # 太短，不是资源键
    assert not media.is_ascii_hex(b"3069-not-hex-1234")
    assert not media.is_ascii_hex(None)


def test_store_attaches_media_path(tmp_path):
    """Store 拿到账号目录后，图片消息要带本地缓存路径；拿不到就留空不编造。"""
    root = _make_account(tmp_path)
    db_dir = tmp_path / "data"
    db_dir.mkdir()

    key = ("ab" * 40).encode()
    content = (
        b"\x0a" + bytes([len(key)]) + key
        + b"\x12" + bytes([len("shot.png")]) + b"shot.png"
        + b"\x20" + bytes([66])
    )
    con = sqlite3.connect(db_dir / "message.db")
    con.execute(
        "create table message_table (message_id text, sender_id text, conversation_id text,"
        " content_type integer, send_time integer, content blob)"
    )
    con.execute("insert into message_table values ('m1','u1','c1',14,1700000000,?)", (content,))
    con.commit()
    con.close()
    con = sqlite3.connect(db_dir / "session.db")
    con.execute("create table conversation_table (id text, name text, roomname_remark text,"
                " last_message_time integer)")
    con.execute("insert into conversation_table values ('c1','群A','',1700000000)")
    con.commit()
    con.close()
    con = sqlite3.connect(db_dir / "user.db")
    con.execute("create table user_table (id text, name text, english_name text)")
    con.execute("insert into user_table values ('u1','张三','zhangsan')")
    con.commit()
    con.close()

    with_media = store.Store(str(db_dir), account_root=str(root)).messages(limit=5)
    item = with_media["items"][0]
    assert item["type_label"] == "image"
    assert item["media"][0]["exists"] is True
    assert item["media"][0]["local_path"].endswith("shot.png")

    without = store.Store(str(db_dir)).messages(limit=5)
    assert without["items"][0]["media"][0]["local_path"] is None
