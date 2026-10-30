"""CLI 契约测试：JSON 信封、错误码与退出码。

只测不依赖 Windows 的路径：doctor 在任何平台都应给出可解析信封；
错误必须带 code/repair 并以稳定退出码结束。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

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


def make_synthetic_db(root) -> None:
    """造一对最小可读的 message.db / session.db，会话名带中文与 Windows 非法字符。"""
    import sqlite3

    def field(no, payload):
        return bytes([(no << 3) | 2, len(payload)]) + payload

    def text_msg(t):
        return field(1, field(2, field(1, t.encode())))

    con = sqlite3.connect(root / "message.db")
    con.execute("create table message_table (message_id integer, sender_id text,"
                " conversation_id text, content_type integer, send_time integer, content blob)")
    con.execute("insert into message_table values (1,'u1','R:1',2,1750000000,?)",
                (text_msg("hi"),))
    con.commit()
    con.close()
    con = sqlite3.connect(root / "session.db")
    con.execute("create table conversation_table (id text, name text, roomname_remark text,"
                " last_message_time integer)")
    con.execute("insert into conversation_table values ('R:1','群/名: 测试','',1750000000)")
    con.commit()
    con.close()


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
    make_synthetic_db(tmp_path)

    out = tmp_path / "txt"
    code = cli.main(["messages", "export", "--db-dir", str(tmp_path), "--out", str(out),
                     "--format", "txt"])
    assert code == 0
    names = [p.name for p in out.iterdir()]
    assert len(names) == 1
    assert not any(ch in names[0] for ch in '\\/:*?"<>|')


def test_missing_key_file_is_precondition(tmp_path, capsys):
    """--key-file 是缓存位置，不是必须存在：缺失时要给可修的 PRECONDITION，而不是 DATA。"""
    missing = tmp_path / "keys" / "nope.json"
    code, _, err = run(
        ["key", "verify", "--db", str(tmp_path / "x.db"), "--key-file", str(missing),
         "--json"],
        capsys,
    )
    assert code == 3
    payload = last_json(err)["error"]
    assert payload["code"] == "PRECONDITION"
    assert payload["repair"]


def test_corrupt_key_file_is_precondition(tmp_path, capsys):
    bad = tmp_path / "keys.json"
    bad.write_text("not json", encoding="utf-8")
    code, _, err = run(
        ["key", "verify", "--db", str(tmp_path / "x.db"), "--key-file", str(bad), "--json"],
        capsys,
    )
    assert code == 3
    assert last_json(err)["error"]["code"] == "PRECONDITION"


def test_json_output_is_utf8_even_with_gbk_stdio(tmp_path):
    """重定向时 locale 是 cp936，消费方仍应拿到 UTF-8 的 JSON（含中文会话名）。"""
    make_synthetic_db(tmp_path)
    env = {**os.environ, "PYTHONIOENCODING": "cp936"}
    proc = subprocess.run(
        [sys.executable, "-m", "open_wechat_cli.cli", "messages", "list",
         "--db-dir", str(tmp_path), "--limit", "5", "--json"],
        capture_output=True, env=env, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.decode("utf-8"))
    assert payload["data"]["items"][0]["conv_name"] == "群/名: 测试"


# --------------------------------------------------------------------------- #
# 附件落地：硬链接优先、跨卷回退复制
# --------------------------------------------------------------------------- #
def _media_item(src, out_dir_name="图片.png"):
    return {"kind": "image", "name": out_dir_name, "local_path": src, "exists": True}


def test_copy_media_prefers_hardlink(tmp_path):
    src = tmp_path / "cache.png"
    src.write_bytes(b"\x89PNG" + b"0" * 200)
    out = tmp_path / "media"
    out.mkdir()
    seen: dict[str, str] = {}

    item = _media_item(str(src))
    cli._copy_one(item, out, seen)
    dest = Path(item["copy"])
    assert dest.read_bytes() == src.read_bytes()
    same_inode = (dest.stat().st_ino, dest.stat().st_dev) == (src.stat().st_ino, src.stat().st_dev)
    assert same_inode, "同卷下应硬链接而不是复制"

    # 同一来源第二次不会重复落地
    item2 = _media_item(str(src))
    cli._copy_one(item2, out, seen)
    assert item2["copy"] == item["copy"]
    assert len(list(out.iterdir())) == 1


def test_copy_media_falls_back_to_copy(tmp_path, monkeypatch):
    src = tmp_path / "cache.bin"
    src.write_bytes(b"x" * 64)
    out = tmp_path / "media"
    out.mkdir()

    def no_link(*args, **kwargs):
        raise OSError("不同卷或不支持硬链接")

    monkeypatch.setattr(cli.os, "link", no_link)
    item = _media_item(str(src), "跨卷.bin")
    cli._copy_one(item, out, {})
    dest = Path(item["copy"])
    assert dest.read_bytes() == src.read_bytes()


def test_copy_media_skips_missing_source(tmp_path):
    out = tmp_path / "media"
    out.mkdir()
    item = {"kind": "image", "name": "gone.png", "local_path": str(tmp_path / "gone.png"),
            "exists": False}
    cli._copy_one(item, out, {})
    assert "copy" not in item
    assert list(out.iterdir()) == []

