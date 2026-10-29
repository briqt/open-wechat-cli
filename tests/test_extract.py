"""protobuf 抽取与类型标签的边界测试。"""

from __future__ import annotations

import pytest

from open_wechat_cli import extract, proto


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field(no: int, payload: bytes) -> bytes:
    return varint((no << 3) | 2) + varint(len(payload)) + payload


def text_message(text: str) -> bytes:
    """1 -> 2 -> 1 的普通文本消息。"""
    inner = field(1, text.encode())
    return field(1, field(2, inner))


def test_text_path_1_2_1():
    body = extract.extract(2, text_message("你好，世界"))
    assert body["text"] == "你好，世界"
    assert body["path"] == "1.2.1"


def test_rich_alert_direct_field_1():
    body = extract.extract(80, field(1, "[告警] CPU 95%".encode()))
    assert body["text"] == "[告警] CPU 95%"
    assert body["path"] == "1"


def test_resource_id_is_not_text():
    ref = b"30690201020406081012141618" * 2
    body = extract.extract(14, field(1, ref))
    assert body["text"] is None
    assert body["ref"]


def test_nested_undecoded_protobuf_is_not_text():
    """解错层级时拿到的还是一段 protobuf，不能当正文输出。"""
    inner = field(1, field(2, field(1, b"long text inside")))
    body = extract.extract(2, field(9, inner))
    assert body["text"] is None


def test_urls_extracted_from_link_card():
    raw = field(1, b"https://example.com/a?b=1")
    body = extract.extract(123, raw)
    assert body["urls"] == ["https://example.com/a?b=1"]


def test_type_labels():
    assert extract.type_label(2) == "text"
    assert extract.type_label(80) == "rich_alert"
    assert extract.type_label(9999) == "type9999"
    assert extract.type_label(None) == "unknown"


def test_malformed_protobuf_is_survivable():
    bad = b"\x0a\x7f" + b"A"  # 声称 127 字节，实际只有 1 字节
    assert proto.walk_path(bad, (1,)) is None
    assert extract.extract(2, bad)["text"] is None


def test_truncated_varint_raises():
    with pytest.raises(proto.ProtobufError):
        proto.varint(b"\x80\x80", 0)


def test_non_utf8_bytes_rejected():
    body = extract.extract(2, field(1, field(2, field(1, b"\xff\xfe\x00"))))
    assert body["text"] is None
