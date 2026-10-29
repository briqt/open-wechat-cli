"""CLI 契约测试：JSON 信封、错误码与退出码。

只测不依赖 Windows 的路径：doctor 在任何平台都应给出可解析信封；
错误必须带 code/repair 并以稳定退出码结束。
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from open_wechat_cli import cli


def run(argv, capsys):
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def last_json(text: str) -> dict:
    """--json 下 stderr 是"诊断行 + 最后一行错误体"，取最后一行。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return json.loads(lines[-1])


def test_doctor_emits_envelope(capsys):
    code, out, err = run(["doctor", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["command"] == "doctor"
    assert payload["schema"] == 1
    assert "platform" in payload["data"]


def test_json_flag_after_subcommand(capsys):
    code, out, err = run(["accounts", "--json"], capsys)
    payload = json.loads(out) if out.strip() else last_json(err)
    assert payload["ok"] is (code == 0)
    assert code in (0, 3)


def test_usage_error_exit_code(capsys):
    code, out, err = run(["key", "scan", "--json"], capsys)
    assert code == 2
    payload = last_json(err)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "USAGE"
    assert payload["error"]["repair"]


def test_missing_snapshot_is_structured_error(capsys, tmp_path):
    code, out, err = run(
        ["decrypt", "--snapshot", str(tmp_path / "nope"), "--out", str(tmp_path / "dec"),
         "--key-hex", "00" * 16, "--json"],
        capsys,
    )
    assert code != 0
    payload = last_json(err)
    assert payload["error"]["code"] in ("DATA", "PRECONDITION")


def test_bad_key_hex_is_usage_error(capsys, tmp_path):
    code, _, err = run(
        ["decrypt", "--snapshot", str(tmp_path), "--out", str(tmp_path / "o"),
         "--key-hex", "abcd", "--json"],
        capsys,
    )
    assert code == 2
    assert last_json(err)["error"]["code"] == "USAGE"


def test_key_verify_reports_mismatch(tmp_path, capsys):
    db = tmp_path / "x.db"
    db.write_bytes(b"\x00" * 4096 * 2)
    code, out, _ = run(
        ["key", "verify", "--db", str(db), "--key-hex", "11" * 16, "--json"], capsys
    )
    payload = json.loads(out)
    assert payload["data"]["verified"] is False
    assert code == 5


def test_store_cli_requires_db_dir(capsys):
    code, _, err = run(["messages", "list", "--db-dir", "/nonexistent/owc", "--json"], capsys)
    assert code == 7
    assert last_json(err)["error"]["code"] == "DATA"


def test_export_txt_filenames_are_windows_safe(tmp_path):
    """会话 id 里带 `:` 等字符时，落盘文件名必须是 Windows 合法的。"""
    import sqlite3

    def field(no, payload):
        return bytes([(no << 3) | 2, len(payload)]) + payload

    def text_msg(t):
        return field(1, field(2, field(1, t.encode())))

    con = sqlite3.connect(tmp_path / "message.db")
    con.execute("create table message_table (message_id integer, sender_id text,"
                " conversation_id text, content_type integer, send_time integer, content blob)")
    con.execute("insert into message_table values (1,'u1','R:1',2,1750000000,?)",
                (text_msg("hi"),))
    con.commit()
    con.close()
    con = sqlite3.connect(tmp_path / "session.db")
    con.execute("create table conversation_table (id text, name text, roomname_remark text,"
                " last_message_time integer)")
    con.execute("insert into conversation_table values ('R:1','群/名: 测试','',1750000000)")
    con.commit()
    con.close()

    out = tmp_path / "txt"
    code = cli.main(["messages", "export", "--db-dir", str(tmp_path), "--out", str(out),
                     "--format", "txt"])
    assert code == 0
    names = [p.name for p in out.iterdir()]
    assert len(names) == 1
    assert not any(ch in names[0] for ch in '\\/:*?"<>|')
