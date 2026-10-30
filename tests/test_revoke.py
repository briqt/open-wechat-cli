"""撤回还原测试：撤回通知（1011）+ 撤回表 + 消息 id 映射。"""

from __future__ import annotations

import sqlite3

from open_wechat_cli import revoke, store


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field(no: int, payload: bytes) -> bytes:
    return _varint((no << 3) | 2) + _varint(len(payload)) + payload


def _msgdata(revoker: int, text: str = "该消息已被撤回") -> bytes:
    return (_varint((4 << 3) | 0) + _varint(revoker)
            + _field(8, text.encode()))


def _make_db(tmp_path, *, with_lookup: bool = True, delta: int = 0):
    db = tmp_path
    con = sqlite3.connect(db / "message.db")
    con.execute("create table message_table (message_id integer, sender_id text,"
                " conversation_id text, content_type integer, send_time integer, content blob)")
    # 1011 = 撤回通知，正文为空
    con.execute("insert into message_table values (101, 'u2', 'R:1', 1011, 1700000100, x'')")
    con.execute("insert into message_table values (102, 'u1', 'R:1', 2, 1700000000, ?)",
                (_field(1, _field(2, _field(1, b"hi"))),))
    con.execute("create table message_revoke_record_table_v2 (con_nid integer, appinfo text,"
                " sendtime integer, is_history integer, is_lookup integer,"
                " msgdata_pb blob, primary key (con_nid, appinfo))")
    con.execute("insert into message_revoke_record_table_v2 values (7, 'app', ?, 0, 1, ?)",
                (1700000100 + delta, _msgdata(1688853581562505)))
    con.commit()
    con.close()

    con = sqlite3.connect(db / "session.db")
    con.execute("create table conversation_table (id text, name text, roomname_remark text,"
                " last_message_time integer)")
    con.execute("insert into conversation_table values ('R:1','群A','',1700000100)")
    con.commit()
    con.close()
    con = sqlite3.connect(db / "user.db")
    con.execute("create table user_table (id text, name text, english_name text)")
    con.executemany("insert into user_table values (?,?,?)",
                    [("u1", "张三", "zhangsan"), ("u2", "李四", "lisi")])
    con.commit()
    con.close()

    if with_lookup:
        con = sqlite3.connect(db / "message_lookup.db")
        con.execute("create table message_lookup_table (message_id integer primary key,"
                    " server_id integer, con_numeric_id integer, send_time integer,"
                    " sequence integer, parent_message_id integer)")
        con.execute("insert into message_lookup_table values (101, 9001, 7, 1700000100, 1, 0)")
        con.execute("insert into message_lookup_table values (102, 9000, 7, 1700000000, 0, 0)")
        con.commit()
        con.close()
    return db


def test_revoke_notice_gets_text_and_revoker(tmp_path):
    _make_db(tmp_path)
    page = store.Store(str(tmp_path)).messages(limit=10)
    by_id = {m["message_id"]: m for m in page["items"]}
    notice = by_id["101"]
    assert notice["text"] == "该消息已被撤回"
    assert notice["recalled"]["revoker_id"] == 1688853581562505
    assert notice["recalled"]["revoked_at"] == 1700000100
    # 普通消息不受影响
    assert by_id["102"]["text"] == "hi"
    assert by_id["102"]["recalled"] is None


def test_revoke_tolerance_window(tmp_path):
    _make_db(tmp_path, delta=2)
    page = store.Store(str(tmp_path)).messages(limit=10)
    notice = next(m for m in page["items"] if m["message_id"] == "101")
    assert notice["text"] == "该消息已被撤回"


def test_revoke_without_lookup_db_is_not_fatal(tmp_path):
    _make_db(tmp_path, with_lookup=False)
    page = store.Store(str(tmp_path)).messages(limit=10)
    notice = next(m for m in page["items"] if m["message_id"] == "101")
    assert notice["text"] is None          # 拿不到会话数字 id，就不猜
    assert notice["recalled"] is None


def test_revoke_index_none_when_table_missing(tmp_path):
    con = sqlite3.connect(tmp_path / "message.db")
    con.execute("create table message_table (message_id integer, sender_id text,"
                " conversation_id text, content_type integer, send_time integer, content blob)")
    con.commit()
    con.close()
    assert revoke.RevokeIndex.load(tmp_path) is None
