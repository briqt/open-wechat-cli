"""消息正文抽取：把 protobuf content 变成"人/Agent 能用的文本"。

实测（Windows 5.0.13.6005）：
* 普通文本消息的正文在 `1 -> 2 -> 1`；
* 告警/审批类富文本直接在 `1`；
* 图片/文件类消息里的长串是资源 id，不应打成正文。

这里不追求穷尽所有形态：解不出来就如实返回 None + 类型标签 + 可提取的 URL/引用，
不把资源 id 冒充成聊天内容。
"""

from __future__ import annotations

import re

from . import proto

TYPE_LABELS = {
    0: "text",
    2: "text",
    3: "image",
    4: "voice",
    13: "sticker",
    14: "image",
    15: "file",
    22: "recall",
    23: "video",
    29: "redpacket",
    31: "miniprogram",
    35: "file",
    38: "location",
    40: "card",
    78: "file",
    80: "rich_alert",
    123: "link_card",
    141: "vote",
    145: "approval",
    221: "transfer",
    503: "system",
    515: "system",
    529: "system",
    561: "chat_record",
    565: "merged_forward",
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

#: 按优先级尝试的文本路径
TEXT_PATHS: tuple[tuple[int, ...], ...] = (
    (1, 2, 1),
    (1,),
    (1, 1),
    (1, 2, 2, 1),
    (2, 1),
)

_REF_HEX = re.compile(r"^[0-9a-fA-F]{24,}$")
_REF_B64 = re.compile(r"^[0-9a-zA-Z+/=_\-]{40,}$")
_URL = re.compile(rb"https?://[\x21-\x7e]{6,300}")


def type_label(content_type: int | None) -> str:
    if content_type is None:
        return "unknown"
    return TYPE_LABELS.get(content_type, f"type{content_type}")


def looks_like_ref(text: str) -> bool:
    """资源 id / uuid / base64 串，不是给人看的正文。"""
    core = text.strip()
    if len(core) < 24:
        return False
    return bool(_REF_HEX.match(core) or _REF_B64.match(core))


def _as_text(blob: bytes | None) -> str | None:
    if not blob:
        return None
    try:
        s = blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not s.strip():
        return None
    # 控制字符意味着这是一段还没解开的嵌套 protobuf，而不是正文
    if any(ch < " " and ch not in "\n\r\t" for ch in s):
        return None
    printable = sum(1 for ch in s if ch.isprintable() or ch in "\n\r\t")
    if printable / max(len(s), 1) <= 0.9:
        return None
    return s


def extract(content_type: int | None, content: bytes | None) -> dict:
    """返回 {text, urls, ref, path}；解不出来时 text=None。"""
    out: dict = {"text": None, "urls": [], "ref": None, "path": None}
    if not content:
        return out

    for path in TEXT_PATHS:
        blob = proto.walk_path(content, path)
        text = _as_text(blob)
        if not text:
            continue
        if looks_like_ref(text):
            out.setdefault("ref", text.strip()[:48])
            continue
        out["text"] = text
        out["path"] = ".".join(str(p) for p in path)
        break

    urls = _URL.findall(content)
    if urls:
        seen: list[str] = []
        for u in urls[:5]:
            s = u.decode("utf-8", "replace")
            if s not in seen:
                seen.append(s)
        out["urls"] = seen

    if out["text"] is None and not out["ref"]:
        raw = _as_text(content)
        if raw and not looks_like_ref(raw):
            out["text"] = raw.strip()
            out["path"] = "raw"
        elif raw:
            out["ref"] = raw.strip()[:48]

    return out
