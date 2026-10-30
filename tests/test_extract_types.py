"""本轮补齐的类型：撤回残留媒体、老版天气卡片、空载荷标记、空 extra 的 link。"""

from __future__ import annotations

from open_wechat_cli import extract


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


def _vfield(no: int, value: int) -> bytes:
    return _varint((no << 3) | 0) + _varint(value)


def test_recalled_media_keeps_metadata_and_name():
    """22：撤回后残留的视频/文件元信息要保住文件名、体积、尺寸与时长。"""
    content = (
        _field(1, "/legacy 202介绍.mp4".encode())
        + _field(2, b"*1*JeQaS7p9xyBzZDMi7cE4ERZgYE9WBxQSTWLkb3GTSI7LdW")
        + _vfield(3, 21195700)
        + _vfield(5, 270)
        + _vfield(6, 480)
        + _field(7, b"https://wework.qpic.cn/wwpic3az/thumb/0")
        + _field(9, b"6BA23F8229E509A0EE4E217F")
        + _vfield(17, 20076)
        + _field(107, "legacy 202介绍.mp4".encode())
        + _field(203, b"/var/mobile/Containers/Data/Application/x.mp4")
    )
    body = extract.extract(22, content)
    item = body["media"][0]
    assert item["kind"] == "video"
    assert item["name"] == "legacy 202介绍.mp4"
    assert item["size"] == 21195700
    assert (item["width"], item["height"]) == (270, 480)
    assert item["duration_s"] == 20.1
    assert item["source"] == "cloud"
    assert item["source_path"].startswith("/var/mobile/")
    assert "已撤回的消息" in body["text"]
    assert "270×480" in body["text"] and "20.2 MiB" in body["text"]
    assert "legacy 202介绍.mp4" in body["text"]
    assert body["link"]["url"].startswith("https://wework.qpic.cn/")


def test_recalled_media_without_name_gets_synthetic_name():
    content = _field(2, b"*1*abc") + _vfield(3, 1024) + _field(9, b"abcdef1234567890")
    item = extract.extract(22, content)["media"][0]
    assert item["name"] == "file-abcdef12"
    assert item["name_source"] == "synthetic"


def test_legacy_weather_text_in_field_6():
    """老版天气卡片只有 `6` 一段提示，没有标题/温度字段。"""
    content = _field(6, "\n?点击完善信息，提醒你最佳通勤路线与实时公交".encode())
    body = extract.extract(132, content)
    assert body["text"].startswith("点击完善信息")
    assert body["link"] is None          # 没有 url/title 时不该造空链接


def test_link_ignores_empty_extra():
    assert extract._link(extra={"date": None, "account": ""}) is None
    link = extract._link(title="标题", extra={"date": None, "account": "异乡好居"})
    assert link["extra"] == {"account": "异乡好居"}


def test_forward_item_marks_empty_payload():
    """子消息有载荷字段但解不出内容（撤回/客户端清空）时标 payload_empty。"""
    empty_text = _field(101, _field(1, b""))
    item = _vfield(5, 2) + _field(11, "张三".encode()) + empty_text
    content = _field(1, item) + _field(2, "张三".encode())
    body = extract.extract(4, content)
    sub = body["forward"]["items"][0]
    assert sub.get("payload_empty") is True
    assert sub["text"] is None
