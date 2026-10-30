"""导出路径测试：资源一律相对路径，默认目录落在状态目录里。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from open_wechat_cli import cli, render


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _field(no: int, payload: bytes) -> bytes:
    return _varint((no << 3) | 2) + _varint(len(payload)) + payload


def _image_message() -> bytes:
    """一张图的 content：1 资源键、2 文件名、4 体积、5/6 宽高。"""
    key = ("ab" * 40).encode()          # 80 个 ASCII 十六进制字符
    return (_field(1, key) + _field(2, b"shot.png")
            + _varint((4 << 3) | 0) + _varint(48)
            + _varint((5 << 3) | 0) + _varint(640)
            + _varint((6 << 3) | 0) + _varint(480))


def _synthetic_db(root: Path) -> Path:
    """造一份"账号目录 + 解密库"：账号目录里带 CacheMapping 与 Cache/Image。"""
    db = root / "data"
    db.mkdir(parents=True, exist_ok=True)
    account = root / "account"
    (account / "CacheMapping").mkdir(parents=True, exist_ok=True)
    image_dir = account / "Cache" / "Image" / "2026-10"
    image_dir.mkdir(parents=True, exist_ok=True)
    image = image_dir / "shot.png"
    image.write_bytes(b"\x89PNG" + b"0" * 40)

    con = sqlite3.connect(account / "CacheMapping" / "map.db")
    con.execute("create table mapping(type integer, key text, file_name text,"
                " last_modify_time integer, file_md5 integer, primary key (type, key))")
    con.execute("insert into mapping values (2, ?, '2026-10\\shot.png', 0, 0)",
                (("ab" * 40),))
    con.commit()
    con.close()

    con = sqlite3.connect(db / "message.db")
    con.execute("create table message_table (message_id text, sender_id text,"
                " conversation_id text, content_type integer, send_time integer, content blob)")
    con.execute("insert into message_table values ('m1','u1','c1',14,1700000000,?)",
                (_image_message(),))
    con.commit()
    con.close()
    con = sqlite3.connect(db / "session.db")
    con.execute("create table conversation_table (id text, name text, roomname_remark text,"
                " last_message_time integer)")
    con.execute("insert into conversation_table values ('c1','群A','',1700000000)")
    con.commit()
    con.close()
    con = sqlite3.connect(db / "user.db")
    con.execute("create table user_table (id text, name text, english_name text)")
    con.execute("insert into user_table values ('u1','张三','zhangsan')")
    con.commit()
    con.close()
    return image


def _media_items(msg: dict) -> list[dict]:
    return list(msg.get("media") or [])


def test_media_src_is_relative_even_outside_out_dir(tmp_path):
    """资源目录在目标目录之外时用 ../，不能退化成绝对路径。"""
    media_dir = tmp_path / "assets"
    media_dir.mkdir()
    image = media_dir / "a.png"
    image.write_bytes(b"x")
    out_dir = tmp_path / "export"
    out_dir.mkdir()
    src, real = render._media_src({"copy": str(image)}, out_dir)
    assert real is True
    assert src == "../assets/a.png"
    assert ":" not in src and not src.startswith("/")


def test_markdown_and_html_links_are_relative(tmp_path):
    image = tmp_path / "assets" / "a.png"
    image.parent.mkdir()
    image.write_bytes(b"x")
    msg = {
        "message_id": "m1", "conv_id": "c1", "conv_name": "群A", "sender_name": "张三",
        "send_time": "2026-10-29T14:00:00+08:00", "send_time_epoch": 1793000000,
        "content_type": 14, "type_label": "image", "text": None, "urls": [], "ref": None,
        "link": None, "segments": [], "entries": [], "forward": None, "voice": None,
        "media": [{"kind": "image", "name": "a.png", "local_path": str(image),
                   "exists": True, "copy": str(image)}],
    }
    out_dir = tmp_path / "export"
    out_dir.mkdir()
    md = render.to_markdown([msg], out_dir=out_dir, title="群A")
    html_doc = render.to_html([msg], out_dir=out_dir, title="群A")
    assert "../assets/a.png" in md
    assert "../assets/a.png" in html_doc
    assert str(tmp_path).replace("\\", "/") not in md


def test_default_out_uses_state_dir(tmp_path, monkeypatch, capsys):
    _synthetic_db(tmp_path)
    state = tmp_path / "state"
    monkeypatch.setenv("OWC_STATE_DIR", str(state))
    code = cli.main(["messages", "export", "--db-dir", str(tmp_path / "data"),
                     "--media-root", str(tmp_path / "account"), "--format", "jsonl",
                     "--copy-media", "--json"])
    assert code == 0
    out, _err = capsys.readouterr()
    payload = json.loads(out)["data"]
    target = Path(payload["out"])
    assert state.resolve() in [p.resolve() for p in target.parents]
    assert target.exists()
    rows = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    item = _media_items(rows[0])[0]
    assert item["copy"].startswith("media/")
    assert (target.parent / item["copy"]).exists()
    assert item["copy_abs"]
    assert Path(item["copy_abs"]).exists()


def test_explicit_out_and_media_out_elsewhere_stay_relative(tmp_path, capsys):
    _synthetic_db(tmp_path)
    out = tmp_path / "pkg" / "conv.jsonl"
    media_out = tmp_path / "pkg" / "assets"
    code = cli.main(["messages", "export", "--db-dir", str(tmp_path / "data"),
                     "--media-root", str(tmp_path / "account"), "--format", "jsonl",
                     "--out", str(out), "--media-out", str(media_out),
                     "--copy-media", "--json"])
    assert code == 0
    capsys.readouterr()
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    copy = _media_items(rows[0])[0]["copy"]
    assert copy.startswith("assets/")
    assert (out.parent / copy).exists()
    assert ":" not in copy


def test_markdown_export_end_to_end_relative(tmp_path, capsys):
    _synthetic_db(tmp_path)
    out = tmp_path / "bundle" / "conv.md"
    code = cli.main(["messages", "export", "--db-dir", str(tmp_path / "data"),
                     "--media-root", str(tmp_path / "account"), "--format", "md",
                     "--out", str(out), "--json"])
    assert code == 0
    capsys.readouterr()
    text = out.read_text(encoding="utf-8")
    assert "![](media/" in text
    assert str(tmp_path).replace("\\", "/") not in text
    assert (out.parent / "media").is_dir()
