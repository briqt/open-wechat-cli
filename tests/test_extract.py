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


def vfield(no: int, value: int) -> bytes:
    """varint 字段（`field` 只能表达 length-delimited）。"""
    return varint((no << 3) | 0) + varint(value)


def hexkey(seed: str, length: int = 214) -> bytes:
    """模拟 content 里的资源键：长串 ASCII hex。"""
    return (seed * (length // len(seed) + 1))[:length].encode()


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
    assert extract.type_label(4) == "chat_record"
    assert extract.type_label(13) == "doc_link"
    assert extract.type_label(16) == "voice"
    assert extract.type_label(80) == "rich_card"
    assert extract.type_label(123) == "rich_text"
    assert extract.type_label(9999) == "other"   # 未登记类型统一标 other，不丢表达
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


# --------------------------------------------------------------------------- #
# 结构化抽取：多段文本、附件、链接、语音、转发
# --------------------------------------------------------------------------- #
def test_multi_segment_text_with_mention():
    """type 0 是多段文本：以前只取第一段，@提及整段丢失。"""
    content = (
        field(1, vfield(1, 0) + field(2, field(1, "这个完了吗？".encode())))
        + field(1, vfield(1, 5) + field(2, vfield(1, 1688853581562505) + field(2, "韩俊祎".encode())))
        + field(1, vfield(1, 0) + field(2, field(1, " 我再看一下".encode())))
    )
    body = extract.extract(0, content)
    assert body["text"] == "这个完了吗？@韩俊祎 我再看一下"
    assert [s["kind"] for s in body["segments"]] == ["text", "at", "text"]
    assert body["segments"][1]["user_id"] == 1688853581562505
    assert body["segments"][1]["name"] == "韩俊祎"


def test_image_media_descriptor():
    key = hexkey("306902010204")
    content = (
        field(1, key)
        + field(2, "企业微信截图_3361.png".encode())
        + vfield(4, 145981)
        + vfield(5, 374)
        + vfield(6, 745)
        + field(10, b"7466c4f4bf8db17d004940213bee5a16")
    )
    body = extract.extract(14, content)
    item = body["media"][0]
    assert item["kind"] == "image"
    assert item["name"] == "企业微信截图_3361.png"
    assert (item["width"], item["height"], item["size"]) == (374, 745, 145981)
    assert item["key"] == key.decode()
    assert item["md5"] == "7466c4f4bf8db17d004940213bee5a16"
    assert body["text"] is None
    assert body["ref"]


def test_voice_duration_and_resource_key():
    key = hexkey("306802010204")
    content = field(1, key) + vfield(4, 92787) + vfield(7, 47)
    body = extract.extract(16, content)
    assert body["voice"]["duration_s"] == 47
    assert body["voice"]["size"] == 92787
    assert body["voice"]["key"] == key.decode()
    assert body["media"][0]["kind"] == "voice"
    assert body["text"] is None


def test_doc_link_card():
    content = (
        field(1, b"https://doc.weixin.qq.com/doc/w3_abc")
        + field(2, b"https://wwcdn.weixin.qq.com/node/wework/images/merge_word.png")
        + field(3, "Daily Agent｜引导英文调整".encode())
        + field(4, "文档".encode())
    )
    body = extract.extract(13, content)
    assert body["link"]["url"] == "https://doc.weixin.qq.com/doc/w3_abc"
    assert body["link"]["title"] == "Daily Agent｜引导英文调整"
    assert body["link"]["desc"] == "文档"
    assert body["text"] == "Daily Agent｜引导英文调整"


def test_chat_record_forward_items():
    """type 4 是聊天记录：以前整条消息文本为空，转发内容全丢。"""
    first = (
        vfield(1, 1688853581562505)
        + vfield(2, 1793267867)
        + vfield(5, 2)
        + field(11, "刘志强".encode())
        + field(101, field(1, field(2, field(1, "转发的一句话".encode()))))
    )
    second = (
        vfield(1, 1688857019898190)
        + vfield(2, 1793267886)
        + vfield(5, 14)
        + field(11, "陈洁仪".encode())
        + field(103, field(1, hexkey("306902010204")) + field(2, "截图.png".encode()) + vfield(4, 1234))
    )
    content = field(1, first) + field(1, second) + field(2, "刘志强和陈洁仪".encode()) + vfield(5, 2)
    body = extract.extract(4, content)
    fwd = body["forward"]
    assert fwd["title"] == "刘志强和陈洁仪"
    assert fwd["total"] == 2
    assert [i["sender_name"] for i in fwd["items"]] == ["刘志强", "陈洁仪"]
    assert fwd["items"][0]["text"] == "转发的一句话"
    assert fwd["items"][0]["content_type"] == 2
    assert fwd["items"][1]["type_label"] == "image"
    assert fwd["items"][1]["media"][0]["name"] == "截图.png"
    assert body["text"].startswith("刘志强和陈洁仪")
    # 回复里的附件留在子消息里，不在顶层重复一份
    assert body["media"] == []


def test_rich_text_entries():
    """type 123 是多元素消息：文本 + 图片混排，以前只有空文本。"""
    text_el = vfield(5, 2) + field(101, field(1, field(2, field(1, "截图的说明".encode()))))
    image_el = (
        vfield(5, 14)
        + field(103, field(1, hexkey("306902010204")) + field(2, "a.png".encode()) + vfield(4, 10))
    )
    body = extract.extract(123, field(1, text_el) + field(1, image_el))
    assert [e["kind"] for e in body["entries"]] == ["text", "media"]
    assert body["text"] == "截图的说明"
    assert body["media"][0]["name"] == "a.png"
    assert body["media"][0]["size"] == 10


def test_forward_item_keeps_link_card():
    """转发里的卡片把内容放在 `105`，以前只剩一个类型标签。"""
    item = (
        vfield(1, 1688853581834390)
        + vfield(5, 13)
        + field(11, "杜松阳".encode())
        + field(105, field(1, b"https://doc.weixin.qq.com/sheet/e3_abc")
                    + field(2, b"https://wwcdn.weixin.qq.com/node/wework/images/merge_excel.png")
                    + field(3, "纽约在线房态 微盘list".encode())
                    + field(4, "表格".encode()))
    )
    content = field(1, item) + field(2, "杜松阳".encode()) + vfield(5, 1)
    body = extract.extract(4, content)
    sub = body["forward"]["items"][0]
    assert sub["type_label"] == "doc_link"
    assert sub["text"] == "纽约在线房态 微盘list"
    assert sub["link"]["url"] == "https://doc.weixin.qq.com/sheet/e3_abc"
    assert sub["link"]["desc"] == "表格"


def test_forward_item_with_sticker():
    """转发里的表情包在 `106`，以前整条只剩类型标签。"""
    sticker = (
        field(1, b"https://wework.qpic.cn/wwpic3az/15287_4c")
        + vfield(2, 1)
        + field(6, b"4FF8A199E9D70026708C7F539AF231C1")
        + field(9, "自定义表情".encode())
    )
    item = (vfield(5, 29) + field(11, "王静".encode()) + field(106, sticker))
    body = extract.extract(4, field(1, item) + field(2, "王静".encode()))
    sub = body["forward"]["items"][0]
    assert sub["type_label"] == "sticker"
    assert sub["text"] == "自定义表情"
    assert sub["link"]["url"].startswith("https://wework.qpic.cn/")


def test_forward_inside_forward():
    """转发的转发落在 `107`，以前整段内容丢失。"""
    inner_text = (vfield(1, 111) + vfield(5, 2) + field(11, "李四".encode())
                  + field(101, field(1, field(2, field(1, "内层一句话".encode())))))
    nested = field(1, inner_text) + field(2, "王五和李四".encode()) + vfield(5, 1)
    outer_item = (vfield(5, 4) + field(11, "王静".encode()) + field(107, nested))
    body = extract.extract(4, field(1, outer_item) + field(2, "王静".encode()))
    outer = body["forward"]["items"][0]
    assert outer["type_label"] == "chat_record"
    assert outer["forward"]["title"] == "王五和李四"
    assert outer["forward"]["items"][0]["text"] == "内层一句话"
    assert "1 条转发消息" in outer["text"]
    assert "内层一句话" in extract.searchable(body)


def test_cloud_file_key_is_not_text():
    """云盘文件（type 20）的 `1` 是云盘键，不能当成聊天正文。"""
    key = b"*1*JeQaS7p9xyBzZDMi7cE4ERZgYE9WBxQSTWLkb3GTSI7LdW/MNAjOVb+sr" + b"A" * 400
    content = field(1, key) + field(2, "ACN机制(1).docx".encode()) + vfield(4, 23923767)
    body = extract.extract(20, content)
    assert body["text"] is None
    assert body["media"][0]["source"] == "cloud"
    assert body["media"][0]["name"] == "ACN机制(1).docx"
    assert body["media"][0]["key"] is None
    assert body["ref"]


def test_media_name_synthesised_when_missing():
    """早期图片消息没有文件名字段，用 md5 兜底，不能留空名。"""
    content = (field(1, hexkey("306902010204")) + vfield(4, 349891)
               + vfield(5, 1170) + vfield(6, 2532)
               + field(10, b"3f3b0f14a56e62978bdc343c672bf65b"))
    body = extract.extract(14, content)
    item = body["media"][0]
    assert item["name"] == "image-3f3b0f14"
    assert item["name_source"] == "synthetic"


def test_searchable_covers_links_media_and_forward():
    body = extract.extract(13, field(1, b"https://x.test/a") + field(3, "周报模板".encode()))
    assert "周报模板" in extract.searchable(body)
    assert "https://x.test/a" in extract.searchable(body)

