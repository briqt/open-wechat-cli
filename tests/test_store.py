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


def test_message_stats_returns_raw_conversation_facts(db_dir):
    s = store.Store(str(db_dir))
    result = s.message_stats(since=1_700_000_050, until=1_700_000_250, tz_name="UTC")
    assert result["messages"] == 2
    assert result["conversations"] == 2
    by_id = {item["conv_id"]: item for item in result["items"]}
    assert by_id["c1"]["messages"] == 1
    assert by_id["c1"]["senders"] == 1
    assert by_id["c1"]["content_types"] == {"2": 1}
    assert by_id["c2"]["last_message"].endswith("+00:00")


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


def test_ensure_index_is_idempotent_and_keeps_queries_working(db_dir):
    """索引是可选加速：建完只多两个索引，查询结果不变，重复调用不再重建。"""
    s = store.Store(str(db_dir))
    created = s.ensure_index()
    assert set(created) == set(store.Store.INDEXES)
    with sqlite3.connect(db_dir / "message.db") as con:
        names = {r[0] for r in con.execute("select name from sqlite_master where type='index'")}
    assert set(store.Store.INDEXES) <= names

    assert s.ensure_index() == []          # 第二次不再重建
    page = store.Store(str(db_dir)).messages(limit=10, conv="c1")
    assert [m["message_id"] for m in page["items"]] == ["m1", "m2"]


def test_ensure_index_uses_mapped_table_and_columns(tmp_path):
    """索引要按 schema 映射出来的真实表名/列名建，不能写死 message_table。"""
    con = sqlite3.connect(tmp_path / "message.db")
    con.execute(
        "create table messages (msg_id text, sender text, conv_id text, msg_type integer,"
        " ts integer, body blob)"
    )
    con.execute("insert into messages values ('m1','u1','c1',2,1700000000,x'00')")
    con.commit()
    con.close()

    s = store.Store(str(tmp_path))
    created = s.ensure_index()
    assert set(created) == set(store.Store.INDEXES)
    with sqlite3.connect(tmp_path / "message.db") as check:
        rows = {r[0]: r[1] for r in check.execute(
            "select name, sql from sqlite_master where type='index'")}
    assert all('"messages"' in sql for sql in rows.values())
    assert any('"conv_id"' in sql and '"ts"' in sql for sql in rows.values())


def test_ensure_index_on_unwritable_copy_degrades(tmp_path, db_dir, monkeypatch):
    """建不了索引（只读介质/被占用）时不能把查询能力打掉。"""
    s = store.Store(str(db_dir))

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("readonly database")

    monkeypatch.setattr(store.sqlite3, "connect", boom)
    assert s.ensure_index() == []
    monkeypatch.undo()
    assert s.messages(limit=1)["items"]

