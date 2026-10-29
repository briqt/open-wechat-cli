"""store / sqlite 层的 schema 适配、分页游标与时区测试。

用明文 sqlite 造一个最小 schema，验证"候选表名/列名"匹配和游标语义；
真机 schema 的适配由 live 验证覆盖。
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from open_wechat_cli import extract, store, sqlite_util


def field(no: int, payload: bytes) -> bytes:
    return bytes([(no << 3) | 2]) + bytes([len(payload)]) + payload


def text_msg(text: str) -> bytes:
    return field(1, field(2, field(1, text.encode())))


@pytest.fixture()
def db_dir(tmp_path):
    con = sqlite3.connect(tmp_path / "message.db")
    con.execute(
        "create table message_table (message_id text, sender_id text, conversation_id text,"
        " content_type integer, send_time integer, content blob)"
    )
    rows = [
        ("m1", "u1", "c1", 2, 1_700_000_000, text_msg("第一条消息")),
        ("m2", "u2", "c1", 2, 1_700_000_100, text_msg("second message")),
        ("m3", "u1", "c2", 2, 1_700_000_200, text_msg("第三个会话")),
    ]
    con.executemany("insert into message_table values (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    con = sqlite3.connect(tmp_path / "session.db")
    con.execute("create table conversation_table (id text, name text, roomname_remark text,"
                " is_sticked integer, last_message_time integer)")
    con.executemany(
        "insert into conversation_table values (?,?,?,?,?)",
        [("c1", "群A", "备注A", 1, 1_700_000_100), ("c2", "Bob", "", 0, 1_700_000_200)],
    )
    con.commit()
    con.close()

    con = sqlite3.connect(tmp_path / "user.db")
    con.execute("create table user_table (id text, name text, english_name text)")
    con.executemany("insert into user_table values (?,?,?)",
                    [("u1", "张三", "zhangsan"), ("u2", "李四", "lisi")])
    con.commit()
    con.close()
    return tmp_path


def test_schema_mapping(db_dir):
    s = store.Store(str(db_dir))
    schema = s.schema()
    assert schema["message"].table == "message_table"
    assert schema["message"].columns["conv"] == "conversation_id"
    assert schema["conversation"].columns["remark"] == "roomname_remark"
    assert schema["user"].columns["english"] == "english_name"


def test_sessions_ordering_and_pagination(db_dir):
    s = store.Store(str(db_dir))
    page = s.sessions(limit=1)
    assert [x["name"] for x in page["items"]] == ["Bob"]
    assert page["next_cursor"]

    page2 = s.sessions(limit=1, cursor=page["next_cursor"])
    assert [x["name"] for x in page2["items"]] == ["群A"]
    assert page2["next_cursor"] is None


def test_contacts_pagination(db_dir):
    s = store.Store(str(db_dir))
    page = s.contacts(limit=1)
    assert page["items"][0]["user_id"] == "u1"
    assert page["next_cursor"]
    page2 = s.contacts(limit=1, cursor=page["next_cursor"])
    assert page2["items"][0]["user_id"] == "u2"


def test_messages_join_and_text(db_dir):
    s = store.Store(str(db_dir))
    page = s.messages(limit=10)
    assert [m["message_id"] for m in page["items"]] == ["m1", "m2", "m3"]
    assert page["items"][0]["conv_name"] == "群A"
    assert page["items"][0]["sender_name"] == "张三"
    assert page["items"][0]["text"] == "第一条消息"
    assert page["items"][1]["type_label"] == "text"


def test_messages_time_filter_and_cursor(db_dir):
    s = store.Store(str(db_dir))
    page = s.messages(limit=1, since=1_700_000_050)
    assert page["items"][0]["message_id"] == "m2"
    nxt = page["next_cursor"]
    assert nxt
    page2 = s.messages(limit=1, since=1_700_000_050, cursor=nxt)
    assert page2["items"][0]["message_id"] == "m3"


def test_messages_conv_filter(db_dir):
    s = store.Store(str(db_dir))
    page = s.messages(limit=10, conv="c2")
    assert [m["message_id"] for m in page["items"]] == ["m3"]


def test_cursor_is_opaque_and_stable():
    c = store.encode_cursor({"t": 1, "id": "m1"})
    assert json.loads(__import__("base64").urlsafe_b64decode(c + "=" * (-len(c) % 4)))["id"] == "m1"
    assert store.decode_cursor(c)["t"] == 1


def test_bad_cursor_raises_usage_error():
    from open_wechat_cli.errors import UsageError

    with pytest.raises(UsageError):
        store.decode_cursor("!!!not-base64!!!")


def test_iso_time_uses_requested_timezone():
    from datetime import datetime

    ts = 1_700_000_000
    sh = store.iso_time(ts, store._tz("Asia/Shanghai"))
    utc = store.iso_time(ts, store._tz("UTC"))
    assert sh.endswith("+08:00")
    assert utc.endswith("+00:00")
    assert datetime.fromisoformat(sh).timestamp() == datetime.fromisoformat(utc).timestamp()


def test_quick_check_ok(db_dir):
    assert sqlite_util.quick_check(db_dir / "message.db")[0] == "ok"
    assert sqlite_util.quick_check(db_dir / "user.db")[1] == 1


def test_missing_db_has_repair(db_dir):
    from open_wechat_cli.errors import DataError

    (db_dir / "message.db").unlink()
    s = store.Store(str(db_dir))
    with pytest.raises(DataError) as exc:
        s.messages(limit=1)
    assert "message.db" in exc.value.message
