"""消息正文抽取：把 protobuf content 变成"人/Agent 能用的结构化结果"。

实测（Windows 企微 5.0.13.6005，见 `.doc/调研/企微消息内容结构.md`）：

| type | 含义 | 正文位置 |
|---|---|---|
| 0 / 2 | 文本（0 是多段，含 @提及、引用） | `1 → 2 → 1`，可重复 |
| 4 | 聊天记录 / 合并转发 | `1`（重复的嵌套消息）+ `2` 标题 |
| 13 | 文档 / 链接卡片 | `1` URL、`3` 标题、`4` 类别、`137` 文档细节 |
| 14 | 图片 | `1` 资源键、`2` 文件名、`4` 字节数、`5/6` 宽高、`10` md5 |
| 15 | 文件 | `1` 资源键、`2` 文件名、`4` 字节数、`44` 扩展名 |
| 16 | 语音 | `1` 资源键、`4` 字节数、`7` 秒数、`3` 语音转文字 |
| 20 | 文件（云盘） | `1` 云盘键、`2` 文件名、`4` 字节数 |
| 23 | 视频 | `2` 资源键、`1` 文件名、`3` 字节数、`17` 毫秒时长 |
| 29 | 表情 | `1` 图片 URL、`9` 名称 |
| 31 | 小程序 / 图文 | `1.1` 标题、`1.2` 封面、`1.5` 链接 |
| 35 | 图文卡片 | `1.1` 标题、`1.2` 摘要、`1.3` 链接 |
| 38 | 应用通知 | `1` 标题、`2` 摘要、`3` 链接 |
| 78 | 公众号文章 | `107.7` 标题、`107.10` 公众号、`107.6` 封面 |
| 80 | 富文本 / 告警卡片 | `1` markdown 正文 |
| 123 | 多元素消息（文本+图片混排） | 重复 `1`，元素 `5` 是类型、`101` 文本、`103` 附件 |
| 141 | 视频号 | `127` 作者/描述/链接 |
| 145 / 561 | 审批 / 统计卡片 | `1` 标题、`2` JSON、`4` 模板 |
| 221 | 微盘文件 | `2` 文件名、`34.2` 链接 |
| 565 | 微盘通知 | `4` 描述、`5` 文件名、`14` 链接 |

原则：解不出来就如实返回 None + 类型标签，不把资源键冒充正文；能拿到附件就把
资源键留给 `media.MediaIndex` 去映射本地缓存文件，路径解析与抽取分开。
"""

from __future__ import annotations

import json
import re

from . import media, proto


TYPE_LABELS = {
    0: "text",
    2: "text",
    3: "image",
    4: "chat_record",
    13: "doc_link",
    22: "recalled_media",
    14: "image",
    15: "file",
    16: "voice",
    20: "file",
    8: "file",
    22: "recalled_media",
    40: "call",
    82: "stats_report",
    212: "bot_card",
    23: "video",
    29: "sticker",
    31: "miniprogram",
    35: "article_card",
    38: "app_card",
    78: "article",
    80: "rich_card",
    123: "rich_text",
    8: "file_card",
    26: "redpacket",
    40: "call",
    82: "stats_report",
    69: "bot_card",
    132: "weather",
    146: "finder_video",
    212: "bot_card",
    8: "file_card",
    26: "redpacket",
    40: "call",
    82: "stats_report",
    69: "bot_card",
    132: "weather",
    146: "finder_video",
    212: "bot_card",
    141: "finder_video",
    145: "approval",
    221: "drive_file",
    503: "system",
    516: "meeting",
    515: "system",
    529: "moment_notice",
    516: "meeting",
    561: "stats_card",
    565: "drive_notice",
    573: "system",
    580: "system",
    671: "system",
    673: "system",
    1001: "system",
    1002: "system",
    1011: "system",
    1017: "system",
    1022: "system",
    1027: "system",
    1051: "system",
    1052: "system",
    1055: "system",
    1988: "system",
}

#: 按优先级尝试的兜底文本路径
TEXT_PATHS: tuple[tuple[int, ...], ...] = (
    (1, 2, 1),
    (1,),
    (1, 1),
    (1, 2, 2, 1),
    (2, 1),
)

#: 「一段文字」的消息类型：正文是重复的 `1` 元素
_SEGMENT_TYPES = (0, 2)
#: 单个附件：type -> (类别, 时长字段, 资源键字段)
_MEDIA_TYPES = {
    14: ("image", None, 1),
    15: ("file", None, 1),
    16: ("voice", 7, 1),
    20: ("file", None, 1),
    23: ("video", 17, 2),
}

_REF_HEX = re.compile(r"^[0-9a-fA-F]{24,}$")
_REF_B64 = re.compile(r"^[0-9a-zA-Z+/=_\-]{40,}$")
_URL = re.compile(rb"https?://[\x21-\x7e]{6,300}")
#: 除 \t \n \r 之外的控制字符：出现即说明这不是正文
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"[ \t]*\n[ \t]*")
_CJK = re.compile(r"[\u4e00-\u9fff]")


# --------------------------------------------------------------------------- #
# protobuf 取值小工具
# --------------------------------------------------------------------------- #
def _fmap(buf: bytes | None) -> dict[int, list[tuple[int, object]]]:
    out: dict[int, list[tuple[int, object]]] = {}
    if not buf:
        return out
    try:
        for no, wt, value in proto.fields(buf):
            out.setdefault(no, []).append((wt, value))
    except proto.ProtobufError:
        return {}
    return out


def _lens(fm, no: int) -> list[bytes]:
    return [v for wt, v in fm.get(no, []) if wt == proto.WT_LEN and isinstance(v, bytes)]


def _ints(fm, no: int) -> list[int]:
    return [v for wt, v in fm.get(no, []) if wt == proto.WT_VARINT and isinstance(v, int)]


def _first_len(fm, no: int) -> bytes | None:
    values = _lens(fm, no)
    return values[0] if values else None


def _first_int(fm, no: int) -> int | None:
    values = _ints(fm, no)
    return values[0] if values else None


def _first_str(fm, no: int) -> str | None:
    for v in _lens(fm, no):
        text = _as_text(v)
        if text:
            return text
    return None


# --------------------------------------------------------------------------- #
# 文本判定
# --------------------------------------------------------------------------- #
def type_label(content_type: int | None) -> str:
    if content_type is None:
        return "unknown"
    return TYPE_LABELS.get(content_type, "other")


def looks_like_ref(text: str) -> bool:
    """资源 id / uuid / base64 串，不是给人看的正文。"""
    core = text.strip()
    if len(core) < 24:
        return False
    return bool(_REF_HEX.match(core) or _REF_B64.match(core))


def _as_text(blob: bytes | None) -> str | None:
    """严格的 bytes → str：解不出 UTF-8 或含控制字符就当"不是正文"。

    这里在热路径上（每条消息都会走到），所以用 C 层正则判控制字符，
    不再逐字符 `isprintable()`——2 KB 文本那样扫一遍要 0.5 ms。
    """
    if not blob:
        return None
    try:
        s = blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not s or s.isspace():
        return None
    if _CTRL.search(s) is not None:
        return None
    return s


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    if "\n" in text or "\t" in text:
        out = _WS.sub("\n", text).strip()
    else:
        out = text.strip()
    return out or None


# --------------------------------------------------------------------------- #
# 结构解析
# --------------------------------------------------------------------------- #
def _wrap_text(blob: bytes | None) -> str | None:
    """`101`/`107` 这类包装层里的正文。

    实测有两种形态，先按"分段列表"解（和 type 0 同构，含 @提及），
    再退回单层路径 `1.2.1` 之类的旧形态。
    """
    if not blob:
        return None
    segments = _segments(blob)
    if segments:
        joined = "".join(s.get("text") or "" for s in segments)
        if joined.strip() and not looks_like_ref(joined):
            return joined
    for path in ((1, 2, 1), (1, 1), (1, 2), (1, 2, 2, 1), (2, 1)):
        text = _as_text(proto.walk_path(blob, path))
        if text and not looks_like_ref(text):
            return text
    return None


def _segments(buf: bytes) -> list[dict]:
    """type 0/2：重复的 `1` 元素，元素里 `1` 是段类型、`2` 是内容。"""
    out: list[dict] = []
    for el in _lens(_fmap(buf), 1):
        el_fm = _fmap(el)
        kind = _first_int(el_fm, 1)
        sub = _first_len(el_fm, 2)
        if kind == 5:  # @提及：`2.1` 是 userid，`2.2` 是显示名
            sub_fm = _fmap(sub)
            name = _first_str(sub_fm, 2)
            uid = _first_int(sub_fm, 1)
            out.append({"kind": "at", "user_id": uid, "name": name,
                        "text": f"@{name}" if name else "@"})
            continue
        text = _first_str(_fmap(sub), 1) if sub else None
        if text is None:
            text = _as_text(sub)
        if text is None:
            continue
        # 段内空白有语义（"好的 " + "@张三"），只去掉控制字符，不 strip
        out.append({"kind": "text", "text": text})
    return out


def _media_descriptor(buf: bytes | None, kind: str, duration_field: int | None = None,
                      key_field: int = 1) -> dict | None:
    """图片/文件/语音/视频的附件描述（`103` 与单附件类型共用字段号，键位随类型而变）。"""
    fm = _fmap(buf)
    if not fm:
        return None
    key = None
    for blob in _lens(fm, key_field) + _lens(fm, 1) + _lens(fm, 2):
        if media.is_ascii_hex(blob):
            key = blob.decode("ascii").strip()
            break
    name = _first_str(fm, 2)
    if name and media.is_ascii_hex(name):
        name = None              # 资源键不是文件名
    name_source = "message" if name else None
    size = _first_int(fm, 4) or _first_int(fm, 22) or _first_int(fm, 3)
    width = _first_int(fm, 5)
    height = _first_int(fm, 6)
    md5 = _first_str(fm, 10) or _first_str(fm, 8)
    ext = _first_str(fm, 44)
    duration_s = None
    if duration_field:
        raw = _first_int(fm, duration_field)
        if raw:
            duration_s = round(raw / 1000, 1) if raw > 600 else raw
    if not any((key, name, size)):
        return None
    if not name:
        # 视频文件名在 `1` 或 `107`；老版本这两处可能都是资源键，不能拿键当名字
        if kind == "video":
            for candidate in (_first_str(fm, 20), _first_str(fm, 107), _first_str(fm, 1)):
                if candidate and not media.is_ascii_hex(candidate):
                    name = candidate.lstrip("/")
                    name_source = "message"
                    break
        if not name:
            # 早期版本的消息没有文件名字段，用 md5/资源键前 8 位兜底，避免导出时无名
            seed = (md5 or key or "")[:8]
            if seed:
                name = f"{kind}-{seed}"
                name_source = "synthetic"
    out = {"kind": kind, "name": name, "name_source": name_source, "ext": ext, "size": size,
           "width": width, "height": height, "md5": md5, "key": key,
           "duration_s": duration_s, "source": "cache"}
    if kind == "video":
        # 视频卡片里常带发送前本机路径（/Volumes/... 或 D:\...），留着便于溯源
        source_path = (_first_str(fm, 202) or _first_str(fm, 107)
                       or _first_str(fm, 1))
        if source_path and (source_path.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", source_path)):
            out["source_path"] = source_path
    return out


_MEDIA_EXT = {
    "image": (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".heic"),
    "video": (".mp4", ".mov", ".avi", ".mkv"),
    "voice": (".amr", ".silk", ".mp3", ".wav", ".m4a"),
}


def _kind_from_name(name: str | None, default: str = "file") -> str:
    if not name:
        return default
    lower = name.lower()
    for kind, exts in _MEDIA_EXT.items():
        if lower.endswith(exts):
            return kind
    return default


def _looks_media(fm) -> bool:
    """元素本身就是附件描述（`1` 是 ASCII hex 资源键，并带文件名/体积）。"""
    has_key = any(media.is_ascii_hex(blob) for blob in _lens(fm, 1))
    return has_key and bool(_first_str(fm, 2) or _first_int(fm, 4) or _first_int(fm, 22))


def _doc_ref(fm) -> dict | None:
    """元素里的文档引用：`17 → 1034`（`1` URL、`2.6` 标题、`2.4` docid）。"""
    card = _fmap(_first_len(_fmap(_first_len(fm, 17)), 1034))
    if not card:
        return None
    detail = _fmap(_first_len(card, 2))
    url = _first_str(card, 1)
    title = _first_str(detail, 6) or _first_str(detail, 9)
    if not (url or title):
        return None
    return _link(url=url, title=title, icon=_first_str(detail, 8),
                 extra={"doc_id": _first_str(detail, 4)})


def _ocr_pairs(meta: dict) -> list[tuple[str, str]]:
    """图片 OCR：`17 → 1042`（`1` 资源键、`2` 识别文本、`3` 类型 14）。"""
    out: list[tuple[str, str]] = []
    for blob in _lens(meta, 1042):
        fm = _fmap(blob)
        text = _first_str(fm, 2)
        if not text:
            continue
        key = None
        for candidate in _lens(fm, 1):
            if media.is_ascii_hex(candidate):
                key = candidate.decode("ascii").strip()
                break
        out.append((key or "", _clean(text) or ""))
    return out


def _extra_texts(meta: dict) -> list[str]:
    """卡片文本：`17 → 1002 → 1 → 6`（引用/小程序标题）与 `17 → 1002 → 1 → 6` 之外的同类字段。"""
    out: list[str] = []
    for blob in _lens(meta, 1002):
        inner = _fmap(_first_len(_fmap(blob), 1))
        for source in (inner, _fmap(blob)):
            text = _first_str(source, 6)
            if text:
                out.append(_clean(text) or text)
    return out


def _quote(fm) -> dict | None:
    """引用回复：`17 → 1002 → 1`（`11` 被引用人、`6` 摘要、`107` 被引用内容）。"""
    for blob in _lens(fm, 1002):
        inner = _fmap(_first_len(_fmap(blob), 1))
        if not inner:
            continue
        payload = _first_len(inner, 107)
        text = _wrap_text(payload)
        quote: dict = {
            "sender_name": _first_str(inner, 11),
            "title": _first_str(inner, 6),
            "text": _clean(text),
            "forward": None,
            "media": [],
        }
        if payload and not text:
            sub = _forward(payload, 1)
            if sub and sub.get("items"):
                quote["forward"] = sub
            else:
                entries = _entries(payload)
                if entries:
                    quote["text"] = _clean("\n".join(
                        e["text"] for e in entries if e.get("text") and e.get("source") != "ocr"))
                    quote["media"] = [e["media"] for e in entries if e["kind"] == "media"]
        # 被引用的内容自己也带 OCR（比如引用了一张截图）
        inner_meta = _fmap(_first_len(inner, 17))
        if inner_meta:
            ocr = [t for _, t in _ocr_pairs(inner_meta) if t]
            if ocr:
                quote["ocr"] = [{"text": t} for t in ocr]
        if any(quote.get(k) for k in ("sender_name", "title", "text", "forward", "ocr")):
            return quote
    return None


def _attach_meta_extras(node: dict, meta: dict) -> None:
    """把 OCR/卡片文本挂到 media 或节点的 extra_text 上（不进正文）。"""
    ocr = _ocr_pairs(meta)
    if ocr:
        by_key = {m.get("key"): m for m in node.get("media") or [] if m.get("key")}
        leftover: list[dict] = []
        for key, text in ocr:
            target = by_key.get(key)
            if target is not None and text:
                target["ocr"] = text
            elif text:
                leftover.append({"key": key or None, "text": text})
        if leftover:
            node["ocr"] = leftover
    extras = _extra_texts(meta)
    for no, values in meta.items():
        if no in (1042, 1034, 1002):
            continue
        for wt, value in values:
            if wt != proto.WT_LEN or not isinstance(value, bytes):
                continue
            for text in _harvest_strings(value, limit=4):
                if text not in extras:
                    extras.append(text)
    if extras:
        node["extra_text"] = "\n".join(dict.fromkeys(extras))


def _entries(buf: bytes) -> list[dict]:
    """type 123 / `107`：同一消息里的多段元素（文本 / 图片 / 文件 / 卡片）。

    顶层元素是 `1` 包一层（`5` 类型、`101` 文本、`103` 附件、`105` 卡片），
    转发里的元素则是扁平结构（`1` 直接就是附件描述或文本），两种都要认。
    """
    out: list[dict] = []
    for el in _lens(_fmap(buf), 1):
        el_fm = _fmap(el)
        etype = _first_int(el_fm, 5)
        handled = False

        text = _wrap_text(_first_len(el_fm, 101))
        if text:
            out.append({"kind": "text", "type": etype, "text": _clean(text)})
            handled = True

        att = _first_len(el_fm, 103)
        if att:
            kind = {14: "image", 15: "file", 16: "voice", 23: "video"}.get(etype or 14, "image")
            item = _media_descriptor(att, kind)
            if item:
                out.append({"kind": "media", "type": etype, "media": item})
                handled = True

        card = _first_len(el_fm, 105)
        if card:
            link = _link_from_payload(card)
            if link:
                out.append({"kind": "link", "type": etype, "link": link,
                            "text": link.get("title") or link.get("url")})
                handled = True

        if not handled and _looks_media(el_fm):
            name = _first_str(el_fm, 2)
            item = _media_descriptor(el, _kind_from_name(name))
            if item:
                out.append({"kind": "media", "type": etype, "media": item})
                handled = True

        if not handled:
            loose = _wrap_text(el) or _as_text(el)
            if loose:
                out.append({"kind": "text", "type": etype, "text": _clean(loose)})

        doc = _doc_ref(el_fm)
        if doc:
            # 文档卡片只有标题是有意义的正文，URL 放进 link；纯链接文本用标题替换
            link_entry = {"kind": "link", "type": etype, "link": doc,
                          "text": doc.get("title") or doc.get("url")}
            out.append(link_entry)
            for prev in reversed(out[:-1]):
                if prev.get("kind") == "text" and prev.get("text"):
                    raw = prev["text"]
                    if "http" in raw and not _CJK.search(raw):
                        prev["text"] = link_entry["text"]
                    break

        # 元素自带的 OCR / 卡片文本（`107` 里的元素也会有）
        meta = _fmap(_first_len(el_fm, 17))
        if meta:
            node = {"media": [e["media"] for e in out if e["kind"] == "media"]}
            _attach_meta_extras(node, meta)
            for ocr_item in node.get("ocr") or []:
                out.append({"kind": "ocr", "type": etype, "text": ocr_item["text"]})
            if node.get("extra_text"):
                out.append({"kind": "text", "type": etype, "source": "card",
                            "text": node["extra_text"]})
    return out


def _forward(buf: bytes, depth: int = 0) -> dict | None:
    """type 4：聊天记录 / 合并转发的嵌套消息列表。

    子消息的载荷按字段分派：`101` 文本、`103` 附件、`105` 卡片、`106` 表情、
    `107` 再嵌套（子聊天记录或富文本）。转发的转发最多再展开三层。
    """
    fm = _fmap(buf)
    raw_items = _lens(fm, 1)
    if not raw_items:
        return None
    items: list[dict] = []
    for el in raw_items:
        g = _fmap(el)
        ctype = _first_int(g, 5)
        item: dict = {
            "sender_id": _first_int(g, 1),
            "sender_name": _first_str(g, 11) or _first_str(g, 6),
            "send_time_epoch": _first_int(g, 2),
            "content_type": ctype,
            "type_label": type_label(ctype),
            "text": _clean(_wrap_text(_first_len(g, 101))),
            "media": [],
            "link": None,
            "forward": None,
            "entries": [],
        }
        att = _first_len(g, 103) or _first_len(g, 102)
        if att:
            kind = {14: "image", 15: "file", 16: "voice", 23: "video"}.get(ctype or 14, "image")
            desc = _media_descriptor(att, kind)
            if desc:
                item["media"].append(desc)
        card = _first_len(g, 105)
        if card:
            item["link"] = _link_from_payload(card)
            if item["link"] and not item["text"]:
                item["text"] = _clean(item["link"].get("title") or item["link"].get("url"))
            elif not item["link"]:
                leftover = _clean(_first_str(_fmap(card), 4))
                if leftover:
                    item["extra_text"] = leftover
        sticker = _first_len(g, 106)
        if sticker:
            sf = _fmap(sticker)
            url = _first_str(sf, 1)
            label = _first_str(sf, 9) or "表情"
            item["link"] = _link(url=url, title=label, icon=url)
            item["text"] = item["text"] or label

        # 老版本把同一份载荷放在 `105.107`（公众号/图文卡片），也有直接放 `107` 的
        nested = _first_len(g, 107) or _first_len(_fmap(card), 107)
        if nested and depth < 3:
            article = _fmap(nested)
            if _first_str(article, 7) and (_first_str(article, 10) or _first_str(article, 6)):
                item["link"] = _link(url=_first_url(article, (9,)),
                                     title=_first_str(article, 7),
                                     desc=_first_str(article, 8) or _first_str(article, 10),
                                     icon=_first_str(article, 6),
                                     extra={"account": _first_str(article, 10)})
                item["text"] = item["text"] or item["link"].get("title")
                att12 = _first_len(article, 12)
                if att12:
                    desc = _media_descriptor(att12, "image")
                    if desc:
                        item["media"].append(desc)
            elif ctype == 4:
                sub = _forward(nested, depth + 1)
                if sub:
                    item["forward"] = sub
                    item["text"] = item["text"] or (
                        f"{sub.get('title') or '聊天记录'}（{len(sub['items'])} 条转发消息）"
                    )
            else:
                entries = _entries(nested)
                if entries:
                    item["entries"] = entries
                    texts = [e["text"] for e in entries
                             if e["kind"] == "text" and e.get("source") != "ocr"]
                    for e in entries:
                        if e["kind"] == "media":
                            item["media"].append(e["media"])
                        elif e["kind"] == "link" and not item["link"]:
                            item["link"] = e["link"]
                        elif e["kind"] == "ocr" and e.get("text"):
                            item.setdefault("ocr", []).append({"text": e["text"]})
                    if not item["text"]:
                        if texts:
                            item["text"] = _clean("\n".join(texts))
                        elif item["link"]:
                            item["text"] = _clean(item["link"].get("title") or item["link"].get("url"))
                if not item["text"]:
                    item["text"] = _clean(_wrap_text(nested))

        if (any(k in g for k in (101, 102, 103, 105, 106, 107))
                and not any([item["text"], item["link"], item["media"],
                             item["forward"], item["entries"]])):
            item["payload_empty"] = True
        meta = _fmap(_first_len(g, 17))
        if meta:
            _attach_meta_extras(item, meta)
            quote = _quote(meta)
            if quote:
                item["quote"] = quote
        items.append(item)
    members = [{"id": _first_int(_fmap(m), 1), "corp_id": _first_int(_fmap(m), 2)}
               for m in _lens(fm, 6)]
    return {
        "title": _first_str(fm, 2),
        # `5` 不是条数（实测恒为 1），条数只能按实际解出来的子消息算
        "total": len(items),
        "members": [m for m in members if m["id"]],
        "items": items,
    }


def _link(url=None, title=None, desc=None, icon=None, extra=None) -> dict | None:
    cleaned_extra = {k: v for k, v in (extra or {}).items() if v}
    if not any((url, title, desc, icon, cleaned_extra)):
        return None
    out = {"url": url, "title": _clean(title), "desc": _clean(desc), "icon": icon}
    if cleaned_extra:
        out["extra"] = cleaned_extra
    return out


def _link_from_payload(blob: bytes | None) -> dict | None:
    """文档/图文卡片共用的一套字段：1 URL、2 图标、3 标题、4 类别。

    转发里的卡片把同样内容塞在 `105`，因此按字段号而不是按位置解析。
    """
    fm = _fmap(blob)
    if not fm:
        return None
    detail = _fmap(_first_len(fm, 137))
    return _link(
        url=_first_url(fm, (1, 3, 5, 6, 14)),
        title=_first_str(fm, 3) or _first_str(fm, 7) or _first_str(fm, 6),
        desc=_first_str(fm, 4) or _first_str(fm, 10),
        icon=_first_url(fm, (2, 6, 4)),
        extra={"doc_id": _first_str(detail, 4), "thumb": _first_str(detail, 17) or None},
    )


# --------------------------------------------------------------------------- #
# 各类型处理器
# --------------------------------------------------------------------------- #
def _handle_text(content: bytes, out: dict) -> None:
    segments = _segments(content)
    if segments:
        out["segments"] = segments
        out["path"] = "1.2.1"
        joined = "".join(s.get("text") or "" for s in segments)
        out["text"] = _clean(joined)


def _handle_media(content_type: int, content: bytes, out: dict) -> None:
    kind, dur_field, key_field = _MEDIA_TYPES[content_type]
    desc = _media_descriptor(content, kind, dur_field, key_field)
    if not desc:
        return
    if content_type == 20:
        # 云盘文件：`1` 是云盘键（`*1*…`），本机 Cache 里没有对应文件
        desc["source"] = "cloud"
        desc["key"] = None
        out["ref"] = _first_str(_fmap(content), 1)[:48] if _first_str(_fmap(content), 1) else None
    out["media"].append(desc)
    if kind == "voice":
        asr = _clean(_wrap_text(_first_len(_fmap(content), 3)))
        out["voice"] = {"duration_s": desc.get("duration_s"), "size": desc.get("size"),
                        "key": desc.get("key"), "text": asr}
        if asr:
            out["text"] = asr
    if desc.get("key") and not out["text"]:
        out["ref"] = desc["key"][:48]


def _handle_rich_text(content: bytes, out: dict) -> None:
    entries = _entries(content)
    if not entries:
        return
    out["entries"] = entries
    meta = _fmap(_first_len(_fmap(content), 17))
    if meta:
        _attach_meta_extras(out, meta)
    texts = [e["text"] for e in entries
             if e["kind"] == "text" and e.get("source") != "ocr"]
    for e in entries:
        if e["kind"] == "media":
            out["media"].append(e["media"])
        elif e["kind"] == "link" and not out["link"]:
            out["link"] = e["link"]
        elif e["kind"] == "ocr" and e.get("text"):
            out["ocr"].append({"text": e["text"]})
    if not texts:
        texts = [e["text"] for e in entries if e["kind"] == "link" and e.get("text")]
    if texts:
        out["text"] = _clean("\n".join(texts))


def _handle_chat_record(content: bytes, out: dict) -> None:
    fwd = _forward(content)
    if not fwd:
        return
    # 附件留在各自的子消息里，不复制一份到顶层，避免同一条消息重复计数
    out["forward"] = fwd
    title = fwd.get("title") or "聊天记录"
    out["text"] = f"{title}（{len(fwd['items'])} 条转发消息）"


_JSON_KEYS = ("text", "content", "title", "desc", "description", "summary", "name",
              "message", "alert_name", "rule_name", "body", "remark", "reason", "tip", "tips")


def _json_texts(blob: bytes | None) -> list[str]:
    """卡片类消息把正文放在 JSON 里（告警、审批、统计、应用卡片）。

    不按固定 key 白名单取值——不同卡片的字段名各不相同；只挑"像人话"的字符串
    （含中文，或长度足够的带空格短语），保持原始顺序，最多 8 条。
    """
    if not blob:
        return []
    try:
        data = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return []
    found: list[str] = []

    def human(value: str) -> bool:
        core = value.strip()
        if len(core) < 2 or core.startswith(("http://", "https://")):
            return False
        if len(core) >= 40 and " " not in core and not _CJK.search(core):
            return False
        return bool(_CJK.search(core)) or (" " in core and len(core) >= 8)

    def walk(node, depth: int = 0) -> None:
        if depth > 5 or len(found) >= 8:
            return
        if isinstance(node, dict):
            for value in node.values():
                if isinstance(value, str):
                    if human(value) and value.strip() not in found:
                        found.append(value.strip())
                else:
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node[:20]:
                walk(value, depth + 1)

    walk(data)
    return found


def _json_blobs(buf: bytes, depth: int = 0, out: list[bytes] | None = None) -> list[bytes]:
    """收集 content 里所有看起来是 JSON 的 length-delimited 字段。"""
    out = out if out is not None else []
    if depth > 4 or len(out) > 8:
        return out
    try:
        fields = proto.fields(buf, limit=400)
    except proto.ProtobufError:
        return out
    for _, wt, value in fields:
        if wt != proto.WT_LEN or not isinstance(value, bytes):
            continue
        head = value.lstrip()[:1]
        if head in (b"{", b"["):
            out.append(value)
        else:
            _json_blobs(value, depth + 1, out)
    return out


#: 卡片类：没有独立正文时，允许用 JSON 里的人话当正文
_CARD_TYPES = {8, 13, 20, 26, 31, 35, 38, 40, 69, 78, 82, 132, 141, 145, 212, 221,
               503, 515, 516, 529, 561, 565, 573, 580, 1022, 1027}


def _handle_recalled_media(content: bytes, out: dict) -> None:
    """22：撤回后残留的媒体消息（云盘键 + 尺寸 + 缩略图，正文已撤回）。

    企微撤回时把原消息的正文清掉，但文件/视频的元信息还在。这里如实说"已撤回"，
    并把还能拿到的体积、尺寸、缩略图挂上，不假装有正文。
    """
    fm = _fmap(content)
    size = _first_int(fm, 3)
    width = _first_int(fm, 5)
    height = _first_int(fm, 6)
    thumb = _first_url(fm, (7,))
    key = _first_str(fm, 2)
    kind = "video" if (width and height) else "file"
    raw_name = _first_str(fm, 107) or _first_str(fm, 1)
    if raw_name and media.is_ascii_hex(raw_name.strip("/")):
        raw_name = None
    name = (raw_name or "").lstrip("/") or None
    name_source = "message" if name else None
    if not name:
        seed = (_first_str(fm, 9) or key or "")[:8]
        if seed:
            name = f"{kind}-{seed}"
            name_source = "synthetic"
    duration = _first_int(fm, 17)
    source_path = _first_str(fm, 203) or _first_str(fm, 202)
    media_item = {
        "kind": kind, "name": name, "name_source": name_source, "ext": None, "size": size,
        "width": width, "height": height,
        "md5": _first_str(fm, 9), "key": None,
        "duration_s": round(duration / 1000, 1) if duration and duration > 600 else duration,
        "source": "cloud",
    }
    if source_path:
        media_item["source_path"] = source_path
    out["media"].append(media_item)
    bits = ["已撤回的消息"]
    if width and height:
        bits.append(f"{width}×{height}")
    if isinstance(size, int) and size > 0:
        bits.append(f"{size / 2**20:.1f} MiB")
    if name:
        bits.append(name)
    out["text"] = "（" + "，".join(bits) + "）"
    if thumb:
        out["link"] = _link(url=thumb, title="撤回前缩略图", icon=thumb)
    if key:
        out["ref"] = key[:48]

def _handle_weather(content: bytes, out: dict) -> None:
    """132：天气卡片（`1` 天气、`3` 提示、`12` 温度、`5` 图标、`6` 地址列表）。"""
    fm = _fmap(content)
    parts = [p for p in (_first_str(fm, 1), _first_str(fm, 12),
                         _first_str(fm, 14) or _first_str(fm, 3)) if p]
    if not parts:
        # 老版本把整段提示塞在 `6` 里
        legacy = _clean(_first_str(fm, 6))
        if legacy:
            parts = [re.sub(r"^[?？]+", "", legacy).strip()]
    out["link"] = _link(url=None, title=_first_str(fm, 1), desc=_first_str(fm, 3),
                        icon=_first_str(fm, 5), extra={"date": _first_str(fm, 2)})
    out["text"] = _clean(" · ".join(parts))
    places: list[str] = []
    for block in _lens(fm, 6):
        bf = _fmap(block)
        for no in (4, 5, 8, 9):
            value = _first_str(bf, no)
            if value and value not in places:
                places.append(value)
    tip = _first_str(_fmap(_first_len(fm, 6)), 15)
    extras = [*places[:6], tip] if (places or tip) else []
    if extras:
        out["extra_text"] = _clean("；".join(p for p in extras if p))


def _handle_meeting(content: bytes, out: dict) -> None:
    """516：会议邀请卡片（`5` 标题、`6` 会议正文、`9` 组织者、`4` 参会人）。

    老版本把这些字段包在 `1` 里，新版本直接在顶层，两种都要认。
    """
    fm = _fmap(content)
    inner = _fmap(_first_len(fm, 1))
    title = _first_str(fm, 5) or _first_str(inner, 5)
    body = _clean(_first_str(fm, 6) or _first_str(inner, 6))
    organizer = (_first_str(_fmap(_first_len(fm, 9)), 5)
                 or _first_str(_fmap(_first_len(inner, 9)), 5))
    attendees: list[str] = []
    for source in (fm, inner):
        for member in _lens(source, 4):
            name = _first_str(_fmap(member), 7)
            if name and name not in attendees:
                attendees.append(name)
    out["text"] = _clean("\n".join(p for p in (title, body) if p))
    url = None
    if body:
        match = _URL.search(body.encode("utf-8"))
        if match:
            url = match.group().decode("utf-8", "replace")
    out["link"] = _link(url=url, title=title, desc=organizer, icon=None,
                        extra={"organizer": organizer, "attendees": attendees[:20]})
    # 会议邀请常带附件（议程/简历），文件名在 12 的子结构里
    attachments: list[str] = []

    def scan_files(buf: bytes, depth: int = 0) -> None:
        if depth > 4 or len(attachments) >= 5:
            return
        try:
            fields = proto.fields(buf, limit=200)
        except proto.ProtobufError:
            return
        for _, wt, value in fields:
            if wt != proto.WT_LEN or not isinstance(value, bytes):
                continue
            text = _as_text(value)
            if text is None:
                scan_files(value, depth + 1)
                continue
            core = text.strip()
            if core.lower().endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".pptx",
                                      ".zip", ".png", ".jpg")):
                attachments.append(core)
            else:
                scan_files(value, depth + 1)

    for source in (fm, inner):
        scan_files(_first_len(source, 12) or b"")
    extras = [v for v in (organizer, "、".join(attendees[:10]) or None,
                          "附件：" + "、".join(dict.fromkeys(attachments)) if attachments else None)
              if v]
    if extras:
        out["extra_text"] = _clean("；".join(extras))


def _harvest_strings(buf: bytes, limit: int = 6, depth: int = 0,
                     out: list[str] | None = None) -> list[str]:
    """递归捞可读字符串（卡片正文有时埋在没人认识的字段里）。"""
    out = out if out is not None else []
    if depth > 5 or len(out) >= limit:
        return out
    try:
        fields = proto.fields(buf, limit=300)
    except proto.ProtobufError:
        return out
    for _, wt, value in fields:
        if wt != proto.WT_LEN or not isinstance(value, bytes):
            continue
        text = _as_text(value)
        if text is None:
            _harvest_strings(value, limit, depth + 1, out)
            continue
        core = text.strip()
        if len(core) < 6 or looks_like_ref(core):
            continue
        if _CJK.search(core) or (" " in core and len(core) >= 8):
            if core not in out:
                out.append(core)
        if len(out) >= limit:
            break
    return out


def _append_unclaimed(out: dict, content: bytes, limit: int = 4) -> None:
    """把还没被任何字段认领的人话补进 extra_text，避免卡片正文丢在陌生字段里。"""
    claimed = " ".join(filter(None, [out.get("text") or ""]))
    link = out.get("link") or {}
    claimed += " " + " ".join(str(v) for v in (link.get("title"), link.get("desc"),
                                               link.get("url"),
                                               *(link.get("extra") or {}).values()))
    claimed += " " + " ".join(m.get("name") or "" for m in out.get("media") or [])
    picked = [t for t in _harvest_strings(content, limit + 2)
              if t not in claimed and not t.startswith(("http://", "https://"))]
    if not picked:
        return
    existing = out.get("extra_text") or ""
    merged = [p for p in [existing, *picked[:limit]] if p]
    seen: list[str] = []
    for part in merged:
        if part not in seen:
            seen.append(part)
    out["extra_text"] = _clean("；".join(seen)[:2000])


def _handle_redpacket(content: bytes, out: dict) -> None:
    """26：红包（`2` 标题、`11`/`12` 说明、`9/10` 图标）。"""
    fm = _fmap(content)
    out["text"] = _clean(_first_str(fm, 11) or _first_str(fm, 12) or _first_str(fm, 2))
    out["link"] = _link(url=None, title=_first_str(fm, 2),
                        desc=_first_str(fm, 11), icon=_first_str(fm, 9))


def _handle_file_card(content: bytes, out: dict) -> None:
    """8：文件卡片（`1` 云盘键 `*1*…`、`2` 文件名、`4` 字节数）。"""
    fm = _fmap(content)
    name = _first_str(fm, 2)
    size = _first_int(fm, 4)
    out["media"].append({"kind": "file", "name": name,
                         "name_source": "message" if name else None,
                         "ext": None, "size": size, "width": None, "height": None,
                         "md5": None, "key": None, "duration_s": None, "source": "cloud"})
    key = _first_str(fm, 1)
    if key:
        out["ref"] = key[:48]


def _handle_call(content: bytes, out: dict) -> None:
    """40：音视频通话记录（`3` 状态文本，例如"未接听"）。"""
    fm = _fmap(content)
    status = _first_str(fm, 3)
    duration = _first_int(fm, 8)
    out["text"] = _clean(status)
    if duration:
        out["extra_text"] = _clean(f"通话 {duration} 秒")


def _handle_stats_report(content: bytes, out: dict) -> None:
    """82：统计小结卡片（部门 / 使用人数 / 使用率）。"""
    fm = _fmap(content)
    parts = [_first_str(fm, no) for no in (1, 2, 3, 4, 5)]
    head = [p for p in (parts[0], parts[1]) if p]
    out["text"] = _clean("\n".join(head))
    extras = [p for p in parts if p]
    if extras:
        out["extra_text"] = _clean("；".join(dict.fromkeys(extras)))


def _handle_bot_card(content: bytes, out: dict) -> None:
    """212：机器人/模板消息卡片（`7` 标题、`3`/`4` JSON 载荷）。"""
    fm = _fmap(content)
    title = _first_str(fm, 7)
    texts: list[str] = []
    for no in (4, 3):
        texts.extend(_json_texts(_first_len(fm, no)))
    out["link"] = _link(url=_first_str(fm, 1), title=title, desc=None)
    out["text"] = _clean(title or (texts[0] if texts else None))
    extras = [t for t in texts if t and t != title]
    if extras:
        out["extra_text"] = _clean("；".join(dict.fromkeys(extras))[:1000])


def _handle_doc_link(content: bytes, out: dict) -> None:
    fm = _fmap(content)
    payload = _first_len(fm, 105)
    out["link"] = _link_from_payload(payload) or _link_from_payload(content)
    if out["link"]:
        out["text"] = out["link"].get("title") or out["link"].get("url")


def _first_url(fm, fields: tuple[int, ...]) -> str | None:
    for no in fields:
        value = _first_str(fm, no)
        if value and value.startswith(("http://", "https://")):
            return value
    return None


def _handle_payload_card(content: bytes, out: dict) -> None:
    """31/35/38：`1` 里装着标题、摘要、链接、封面。"""
    inner = _fmap(_first_len(_fmap(content), 1))
    if inner:
        title = _first_str(inner, 1)
        desc = _first_str(inner, 3) or _first_str(inner, 4)
        url = _first_url(inner, (5, 6, 3))
        icon = _first_url(inner, (2, 4))
        out["link"] = _link(url=url, title=title, desc=desc, icon=icon)
    fm = _fmap(content)
    if not out["link"]:
        out["link"] = _link(url=_first_url(fm, (3,)), title=_first_str(fm, 1),
                            desc=_first_str(fm, 2))
    extras = [v for v in (_first_str(fm, 5), _first_str(fm, 6)) if v]
    if extras:
        out["extra_text"] = _clean("；".join(dict.fromkeys(extras)))
    if out["link"]:
        out["text"] = out["link"].get("title")


def _handle_article(content: bytes, out: dict) -> None:
    """78：公众号文章卡片，正文在 `107`。"""
    inner = _fmap(_first_len(_fmap(content), 107))
    if not inner:
        return
    out["link"] = _link(url=_first_url(inner, (9,)), title=_first_str(inner, 7),
                        desc=_first_str(inner, 8) or _first_str(inner, 10),
                        icon=_first_str(inner, 6),
                        extra={"app_id": _first_str(inner, 1), "wxid": _first_str(inner, 2),
                               "account": _first_str(inner, 10), "path": _first_str(inner, 3)})
    extras = [v for v in (_first_str(inner, 8), _first_str(inner, 10)) if v]
    if extras:
        out["extra_text"] = _clean("；".join(dict.fromkeys(extras)))
    att = _first_len(inner, 12)
    if att:
        desc = _media_descriptor(att, "image")
        if desc:
            out["media"].append(desc)
    if out["link"]:
        out["text"] = out["link"].get("title")


def _handle_sticker(content: bytes, out: dict) -> None:
    fm = _fmap(content)
    url = _first_str(fm, 1)
    vendor = _first_str(fm, 18)
    out["link"] = _link(url=url, title=_first_str(fm, 9) or "表情", icon=url,
                        extra={"vendor": vendor} if vendor else None)
    out["text"] = out["link"].get("title") if out["link"] else None
    if vendor:
        out["extra_text"] = vendor


def _handle_rich_card(content: bytes, out: dict) -> None:
    """80：markdown 正文直接落在 `1`，`4.1` 是同一份内容的 JSON。"""
    fm = _fmap(content)
    out["text"] = _clean(_first_str(fm, 1))
    if out["text"]:
        out["path"] = "1"
        return
    payload = _fmap(_first_len(fm, 4))
    out["text"] = _clean(_first_str(payload, 1))
    if out["text"]:
        out["path"] = "4.1"


def _handle_approval(content: bytes, out: dict) -> None:
    fm = _fmap(content)
    title = _first_str(fm, 1)
    out["link"] = _link(url=_first_str(fm, 4), title=title, desc=None,
                        extra={"payload": _first_str(fm, 2)} if _first_str(fm, 2) else None)
    out["text"] = title


def _handle_stats_card(content: bytes, out: dict) -> None:
    fm = _fmap(content)
    title = _first_str(fm, 1)
    out["link"] = _link(url=_first_str(fm, 4), title=title,
                        extra={"payload": _first_str(fm, 2)} if _first_str(fm, 2) else None)
    out["text"] = title


def _handle_drive_file(content: bytes, out: dict) -> None:
    fm = _fmap(content)
    detail = _fmap(_first_len(fm, 34))
    card = _fmap(_first_len(detail, 2))
    name = _first_str(fm, 2) or _first_str(card, 3)
    out["link"] = _link(url=_first_str(card, 1), title=name, desc=_first_str(card, 4),
                        icon=_first_str(card, 2))
    out["media"].append({"kind": "file", "name": name, "name_source": "message" if name else None,
                         "ext": None, "size": _first_int(fm, 4), "width": None, "height": None,
                         "md5": None, "key": None, "duration_s": None, "source": "cloud"})
    out["text"] = name


def _handle_drive_notice(content: bytes, out: dict) -> None:
    fm = _fmap(content)
    name = _first_str(fm, 5)
    subtitle = _first_str(fm, 6)
    out["link"] = _link(url=_first_str(fm, 14), title=name or _first_str(fm, 4),
                        desc=_first_str(fm, 4), icon=_first_str(fm, 7))
    out["media"].append({"kind": "file", "name": name, "name_source": "message" if name else None,
                         "ext": None, "size": None, "width": None, "height": None,
                         "md5": None, "key": None, "duration_s": None, "source": "cloud"})
    out["text"] = _clean(" · ".join(p for p in (_first_str(fm, 4), name, subtitle) if p))


def _handle_finder(content: bytes, out: dict) -> None:
    inner = _fmap(_first_len(_fmap(content), 127))
    if not inner:
        return
    out["link"] = _link(url=_first_str(inner, 7), title=_first_str(inner, 6),
                        desc=_first_str(inner, 5), icon=_first_str(inner, 4),
                        extra={"video": _first_str(inner, 2)})
    out["text"] = _clean(_first_str(inner, 6) or _first_str(inner, 5))


def _handle_moment(content: bytes, out: dict) -> None:
    """529：客户朋友圈通知。正文在 `4.5`，正文之外还挂着文章卡片(`4.17`)与图片(`4.6`)。"""
    fm = _fmap(content)
    inner = _fmap(_first_len(fm, 4))
    title = _first_str(fm, 2)
    body = _clean(_first_str(inner, 5))
    out["text"] = _clean("\n".join(p for p in (title, body) if p))

    card = _fmap(_first_len(inner, 17))
    if card:
        out["link"] = _link(url=_first_url(card, (2, 3)), title=_first_str(card, 1),
                            desc=None, icon=_first_url(card, (2,)))
    att = _first_len(inner, 6)
    if att:
        desc = _media_descriptor(att, "image")
        if desc:
            desc["source"] = "cloud"   # 朋友圈图片走 CDN，本机 Cache 里没有对应文件
            desc["key"] = None
            out["media"].append(desc)


_HANDLERS = {
    "text": _handle_text,
    "rich_text": _handle_rich_text,
    "chat_record": _handle_chat_record,
    "doc_link": _handle_doc_link,
    "payload_card": _handle_payload_card,
    "article": _handle_article,
    "sticker": _handle_sticker,
    "weather": _handle_weather,
    "recalled_media": _handle_recalled_media,
    "file_card": _handle_file_card,
    "call": _handle_call,
    "stats_report": _handle_stats_report,
    "bot_card": _handle_bot_card,
    "redpacket": _handle_redpacket,
    "meeting": _handle_meeting,
    "rich_card": _handle_rich_card,
    "approval": _handle_approval,
    "stats_card": _handle_stats_card,
    "drive_file": _handle_drive_file,
    "drive_notice": _handle_drive_notice,
    "finder_video": _handle_finder,
    "moment_notice": _handle_moment,
}

#: 类型 -> 处理器名
_TYPE_HANDLERS = {
    0: "text", 2: "text",
    4: "chat_record",
    13: "doc_link",
    22: "recalled_media",
    31: "payload_card", 35: "payload_card", 38: "payload_card",
    78: "article",
    80: "rich_card",
    123: "rich_text",
    8: "file_card",
    26: "redpacket",
    40: "call",
    82: "stats_report",
    69: "bot_card",
    132: "weather",
    146: "finder_video",
    212: "bot_card",
    141: "finder_video",
    145: "approval",
    221: "drive_file",
    516: "meeting",
    529: "moment_notice",
    516: "meeting",
    561: "stats_card",
    565: "drive_notice",
    29: "sticker",
}


def extract(content_type: int | None, content: bytes | None) -> dict:
    """返回结构化结果；解不出正文时 text=None，但不会丢附件与链接。"""
    out: dict = {
        "text": None, "urls": [], "ref": None, "path": None,
        "segments": [], "entries": [], "media": [], "link": None,
        "forward": None, "voice": None, "quote": None, "ocr": [], "extra_text": None,
    }
    if not content:
        return out

    _extract_into(content_type, content, out)
    return out


def _extract_into(content_type: int | None, content: bytes, out: dict) -> None:
    ct = content_type if isinstance(content_type, int) else None
    if ct in _MEDIA_TYPES:
        _handle_media(ct, content, out)
    else:
        name = _TYPE_HANDLERS.get(ct)
        if name:
            _HANDLERS[name](content, out)

    # 附件类消息的正文就是附件本身：不要再去 content 里捞"正文"，
    # 否则云盘键、"*1*…" 之类的资源串会被当成聊天内容输出。
    if out["text"] is None and not (ct in _MEDIA_TYPES and out["media"]):
        _fallback_text(content, out, allow_scan=ct not in (0, 2))

    # 卡片 JSON 里的人话：只在"还没有正文"或"正文很短（多半只是标题）"时才值得解
    fm = _fmap(content)
    if not out["extra_text"] and (out["text"] is None or len(out["text"]) < 40):
        rich: list[str] = []
        for blob in _json_blobs(content):
            rich.extend(_json_texts(blob))
        for no in (2, 3, 4, 1):
            if not rich:
                rich.extend(_json_texts(_first_len(fm, no)))
        if rich:
            out["extra_text"] = _clean("；".join(dict.fromkeys(rich))[:2000])
            if out["text"] is None and ct in _CARD_TYPES and out["extra_text"]:
                out["text"] = out["extra_text"]

    if ct in _CARD_TYPES and not out["forward"]:
        _append_unclaimed(out, content)
    elif ct not in _TYPE_HANDLERS and ct not in _MEDIA_TYPES and not out["forward"]:
        # 没登记的类型：兜底之后再把剩余人话收进来，别因为不认识就整条丢掉
        _append_unclaimed(out, content)

    meta = _fmap(_first_len(fm, 17))
    if meta:
        _attach_meta_extras(out, meta)
        quote = _quote(meta)
        if quote:
            out["quote"] = quote

    if b"http" in content:
        urls = _URL.findall(content)
        if urls:
            seen: list[str] = []
            for u in urls[:5]:
                s = u.decode("utf-8", "replace")
                if s not in seen:
                    seen.append(s)
            out["urls"] = seen

    if out["text"] is None and not out["ref"] and (out["media"] or out["forward"]):
        # 附件消息没有正文是正常的，这里只补一个资源引用，方便人去追
        first = next((m.get("key") for m in out["media"] if m.get("key")), None)
        if first:
            out["ref"] = first[:48]
    return out


def _fallback_text(content: bytes, out: dict, *, allow_scan: bool = True) -> None:
    """结构化解析没拿到正文时的兜底：先走已知文本路径，再视类型扫嵌套可读串。"""
    for path in TEXT_PATHS:
        blob = proto.walk_path(content, path)
        text = _as_text(blob)
        if not text:
            continue
        if looks_like_ref(text):
            out.setdefault("ref", text.strip()[:48])
            continue
        out["text"] = _clean(text)
        out["path"] = ".".join(str(p) for p in path)
        return
    raw = _as_text(content)
    if raw and not looks_like_ref(raw):
        out["text"] = _clean(raw)
        out["path"] = "raw"
        return
    if raw:
        out["ref"] = raw.strip()[:48]
        return
    if not allow_scan:
        return
    # 最后兜底：递归捞可读字符串（不少系统/卡片消息的正文就埋在嵌套层里）
    found: list[str] = []

    def scan(buf: bytes, depth: int = 0) -> None:
        if depth > 6 or len(found) >= 3:
            return
        try:
            for _, wt, value in proto.fields(buf, limit=400):
                if wt != proto.WT_LEN or not isinstance(value, bytes):
                    continue
                text = _as_text(value)
                if text is None:
                    scan(value, depth + 1)
                    continue
                core = text.strip()
                if looks_like_ref(core) or len(core) < 4:
                    continue
                if _CJK.search(core) or (" " in core and len(core) >= 8):
                    if core not in found:
                        found.append(core)
        except proto.ProtobufError:
            return

    scan(content)
    if found:
        out["text"] = _clean("\n".join(found))
        out["path"] = "scan"


def searchable(body: dict) -> str:
    """给 --query 用的检索文本：正文 + 链接 + 附件名 + 转发子消息（含嵌套）。"""
    parts: list[str] = [body.get("text") or ""]
    parts.append(body.get("extra_text") or "")
    parts.extend(o.get("text") or "" for o in body.get("ocr") or [])
    link = body.get("link") or {}
    parts.extend([link.get("title") or "", link.get("desc") or "", link.get("url") or ""])
    parts.extend(str(v) for v in (link.get("extra") or {}).values() if isinstance(v, str))
    parts.extend(m.get("name") or "" for m in body.get("media") or [])
    parts.extend(m.get("source_path") or "" for m in body.get("media") or [])
    parts.extend(m.get("ocr") or "" for m in body.get("media") or [])
    parts.extend(e.get("text") or "" for e in body.get("entries") or [])

    def walk_quote(quote: dict | None) -> None:
        if not quote:
            return
        parts.extend([quote.get("sender_name") or "", quote.get("title") or "",
                      quote.get("text") or ""])
        for m in quote.get("media") or []:
            parts.append(m.get("name") or "")
        parts.extend(o.get("text") or "" for o in quote.get("ocr") or [])
        walk_forward(quote.get("forward"))

    def walk_forward(fwd: dict | None) -> None:
        if not fwd:
            return
        parts.append(fwd.get("title") or "")
        for item in fwd.get("items") or []:
            parts.append(item.get("text") or "")
            parts.append(item.get("sender_name") or "")
            parts.append(item.get("extra_text") or "")
            parts.extend(o.get("text") or "" for o in item.get("ocr") or [])
            inner = item.get("link") or {}
            parts.extend([inner.get("title") or "", inner.get("desc") or "",
                          inner.get("url") or ""])
            parts.extend(str(v) for v in (inner.get("extra") or {}).values()
                         if isinstance(v, str))
            parts.extend(m.get("name") or "" for m in item.get("media") or [])
            parts.extend(m.get("source_path") or "" for m in item.get("media") or [])
            parts.extend(m.get("ocr") or "" for m in item.get("media") or [])
            parts.extend(e.get("text") or "" for e in item.get("entries") or [])
            walk_quote(item.get("quote"))
            walk_forward(item.get("forward"))

    walk_forward(body.get("forward"))
    walk_quote(body.get("quote"))
    for seg in body.get("segments") or []:
        parts.append(seg.get("name") or "")
    return "\n".join(p for p in parts if p)
