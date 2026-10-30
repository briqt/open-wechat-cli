"""把消息记录渲染成 Markdown / HTML（人工阅读用，不参与结构化消费）。

设计取舍：
* 直接在片段层面展开——图片用 `<img>`/`![]()`，链接卡片给标题 + 域名，聊天记录
  用引用块缩进，语音给时长与文件；不把结构化字段再压成一行纯文本；
* 附件引用优先用 `--copy-media` 复制出来的相对路径，其次用 `local_path`，
  两者都没有就只显示文件名并标注「未缓存」；
* 不做站内链接、不写时间轴脚本：单文件、离线可看、可整段贴进文档。
"""

from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

TYPE_CHIPS = {
    "text": "文本",
    "rich_text": "图文",
    "image": "图片",
    "file": "文件",
    "voice": "语音",
    "video": "视频",
    "chat_record": "聊天记录",
    "doc_link": "文档链接",
    "article": "公众号",
    "miniprogram": "小程序",
    "drive_file": "微盘文件",
    "drive_notice": "微盘通知",
    "article_card": "图文卡片",
    "app_card": "应用卡片",
    "rich_card": "富文本",
    "approval": "审批",
    "stats_card": "统计卡片",
    "moment_notice": "朋友圈通知",
    "sticker": "表情",
    "system": "系统",
}


def _size(value) -> str:
    if not isinstance(value, int) or value <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def _msg_tz(msg: dict):
    """取消息自带的时区偏移：输出不能随本机时区变，否则同一份导出不同机器不一致。"""
    match = re.search(r"([+-])(\d{2}):(\d{2})$", msg.get("send_time") or "")
    if match:
        sign = -1 if match.group(1) == "-" else 1
        return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))
    return timezone(timedelta(hours=8))


def _clock(msg: dict) -> str:
    stamp = msg.get("send_time") or ""
    return stamp[11:16] if len(stamp) >= 16 else stamp


def _day(msg: dict) -> str:
    stamp = msg.get("send_time") or ""
    return stamp[:10]


def _media_src(item: dict, out_dir: Path) -> tuple[str | None, bool]:
    """返回 (相对引用路径, 是否指向真实文件)。

    导出稿要能整目录搬走/发给别人，所以永远输出**相对** out_dir 的路径：
    资源在子目录就是 `media/x.png`，在别处就是 `../assets/x.png`，
    绝不把 `C:/Users/...` 这种机器路径写进 Markdown / HTML。
    """
    for key in ("copy", "local_path"):
        raw = item.get(key)
        if not raw:
            continue
        path = Path(raw)
        if not path.exists():
            continue
        try:
            rel = os.path.relpath(str(path.resolve()), start=str(Path(out_dir).resolve()))
        except (OSError, ValueError):
            rel = path.name
        return Path(rel).as_posix(), True
    return (item.get("name") or None), False


def _media_identity(item: dict) -> tuple:
    """同一附件在 `entries[]` 与 `media[]` 里各出现一次，渲染时按这个标识去重。"""
    return (
        item.get("key") or "",
        item.get("name") or "",
        item.get("size") or 0,
        item.get("local_path") or "",
    )


KIND_LABELS = {"image": "图片", "file": "文件", "voice": "语音", "video": "视频",
               "sticker": "表情", "audio": "音频"}


def _media_label(item: dict, *, with_kind: bool = True) -> str:
    kind = KIND_LABELS.get(item.get("kind") or "", item.get("kind") or "附件")
    bits = [kind] if with_kind else []
    if item.get("name"):
        bits.append(item["name"])
    if item.get("width") and item.get("height"):
        bits.append(f"{item['width']}×{item['height']}")
    if item.get("duration_s"):
        bits.append(f"{item['duration_s']}s")
    if _size(item.get("size")):
        bits.append(_size(item.get("size")))
    return " · ".join(str(b) for b in bits)


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #
def _md_inline(text: str | None) -> str:
    if not text:
        return ""
    return text.replace("\r\n", "\n").strip()


def _md_media(items: Iterable[dict], out_dir: Path, indent: str = "  ") -> list[str]:
    lines: list[str] = []
    for item in items:
        src, real = _media_src(item, out_dir)
        kind = item.get("kind") or "file"
        cloud = item.get("source") == "cloud"
        if kind == "image" and real and src:
            lines.append(f"{indent}![]({src})")
            lines.append(f"{indent}<sub>{_media_label(item, with_kind=False)}</sub>")
        elif real and src:
            lines.append(f"{indent}- [{_media_label(item)}]({src})")
        elif cloud:
            lines.append(f"{indent}- {_media_label(item)}（云盘文件，未落到本地缓存）")
        else:
            lines.append(f"{indent}- {_media_label(item)}（未缓存，只有元信息）")
    return lines


def _md_forward(fwd: dict, out_dir: Path, indent: str,
                tz=timezone(timedelta(hours=8))) -> list[str]:
    title = fwd.get("title") or "聊天记录"
    total = fwd.get("total") or len(fwd.get("items") or [])
    lines = [f"{indent}> **{title}**（{total} 条）"]
    for item in fwd.get("items") or []:
        who = item.get("sender_name") or "-"
        stamp = item.get("send_time_epoch")
        clock = datetime.fromtimestamp(stamp, tz).strftime("%m-%d %H:%M") if stamp else ""
        body = _md_inline(item.get("text"))
        link = item.get("link") or {}
        if not body and link:
            body = link.get("title") or link.get("url") or ""
        head = f"{indent}> - **{who}**" + (f" `{clock}`" if clock else "")
        if not body:
            lines.append(head)
        else:
            first, *rest = body.split("\n")
            lines.append(f"{head}：{first}")
            # 子消息内的换行必须保持引用与列表缩进，否则会脱离列表
            for extra_line in rest:
                lines.append(f"{indent}>   {extra_line}" if extra_line.strip() else f"{indent}>")
        if link.get("url") and link.get("url") not in (body or ""):
            lines.append(f"{indent}>   🔗 {link['url']}")
        for entry in item.get("entries") or []:
            if entry.get("kind") == "text" and entry.get("text"):
                lines.append(f"{indent}>   {entry['text']}")
        for sub in item.get("media") or []:
            lines.extend(_md_media([sub], out_dir, indent + ">   "))
        if item.get("forward"):
            lines.extend(_md_forward(item["forward"], out_dir, indent + ">   ", tz))
    return lines


def _md_body(msg: dict, out_dir: Path, indent: str = "  ") -> list[str]:
    """一条消息的正文块（不含发送者那行）。"""
    lines: list[str] = []
    text = _md_inline(msg.get("text"))
    if text:
        lines.extend(indent + ln if ln.strip() else "" for ln in text.split("\n"))

    link = msg.get("link") or {}
    if link:
        title = link.get("title") or link.get("url") or "链接"
        url = link.get("url")
        desc = link.get("desc")
        tail = f" —— {desc}" if desc and desc != title else ""
        if url:
            lines.append(f"{indent}🔗 [{title}]({url}){tail}")
        else:
            lines.append(f"{indent}🔗 {title}{tail}")

    rendered: set[tuple] = set()
    for entry in msg.get("entries") or []:
        if entry.get("kind") == "text" and entry.get("text") and entry["text"] not in (text or ""):
            lines.append(f"{indent}{_md_inline(entry['text'])}")
        elif entry.get("kind") == "media":
            media = entry.get("media") or {}
            rendered.add(_media_identity(media))
            lines.extend(_md_media([media], out_dir, indent))

    voice = msg.get("voice") or {}
    if voice:
        media_items = msg.get("media") or []
        src, real = _media_src(media_items[0], out_dir) if media_items else (None, False)
        dur = f"{voice['duration_s']}s" if voice.get("duration_s") else "时长未知"
        if real and src:
            lines.append(f"{indent}🎙 语音 {dur} · [{Path(src).name}]({src})（silk 原始文件）")
        else:
            lines.append(f"{indent}🎙 语音 {dur}（缓存未落地）")

    media_items = [m for m in (msg.get("media") or []) if _media_identity(m) not in rendered]
    if media_items and not voice:
        lines.extend(_md_media(media_items, out_dir, indent))

    tz = _msg_tz(msg)
    if msg.get("forward"):
        lines.extend(_md_forward(msg["forward"], out_dir, indent, tz))
    quote = msg.get("quote") or {}
    if quote:
        who = quote.get("sender_name") or "引用"
        body = _md_inline(quote.get("text") or quote.get("title") or "")
        lines.append(f"{indent}> 引用 **{who}**：{body}")
        if quote.get("forward"):
            lines.extend(_md_forward(quote["forward"], out_dir, indent + "> ", tz))
    for item in msg.get("ocr") or []:
        if item.get("text"):
            lines.append(f"{indent}_图内文字_：{item['text'][:400]}")
    if msg.get("extra_text") and msg.get("text") != msg["extra_text"]:
        lines.append(f"{indent}_卡片文本_：{_md_inline(msg['extra_text'])[:400]}")
    return lines


def to_markdown(messages, *, out_dir: Path, title: str = "聊天记录",
                meta: dict | None = None) -> str:
    lines = [f"# {title}", ""]
    if meta:
        for key, value in meta.items():
            if value:
                lines.append(f"- **{key}**：{value}")
        lines.append("")
    current_day = None
    for msg in messages:
        day = _day(msg)
        if day != current_day:
            lines.append(f"## {day}")
            lines.append("")
            current_day = day
        chips = TYPE_CHIPS.get(msg.get("type_label") or "", "")
        head = f"**{_clock(msg)} · {msg.get('sender_name') or '-'}**"
        if chips and chips not in ("文本",):
            head += f" `{chips}`"
        lines.append(f"- {head}")
        body = _md_body(msg, out_dir, "  ")
        if body:
            lines.extend(body)
        else:
            lines.append(f"  _{msg.get('type_label') or '空消息'}_")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
CSS = """
:root { color-scheme: light dark; --fg:#1f2328; --muted:#6a737d; --bg:#f6f7f9;
        --card:#fff; --line:#e3e6ea; --accent:#2f6feb; --quote:#f3f5f8; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e6edf3; --muted:#9aa4af; --bg:#0f1115; --card:#171a1f;
          --line:#272b31; --accent:#6ea8ff; --quote:#1d2128; }
}
* { box-sizing: border-box; }
body { margin:0; padding:32px 20px 80px; background:var(--bg); color:var(--fg);
       font:15px/1.65 -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
main { max-width: 900px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 6px; }
.meta { color: var(--muted); font-size: 13px; margin-bottom: 24px; }
.meta span { margin-right: 14px; white-space: nowrap; }
.day { margin: 28px 0 10px; font-size: 13px; color: var(--muted); letter-spacing: .08em;
       text-transform: uppercase; border-bottom: 1px solid var(--line); padding-bottom: 6px; }
.msg { display:flex; gap:12px; padding:10px 12px; border-radius:10px; background:var(--card);
       border:1px solid var(--line); margin:10px 0; }
.avatar { flex:0 0 34px; height:34px; border-radius:50%; background:var(--accent); color:#fff;
          font-size:13px; display:flex; align-items:center; justify-content:center; }
.body { min-width:0; flex:1; }
.who { font-size:13px; color:var(--muted); margin-bottom:2px; }
.who b { color:var(--fg); font-size:14px; }
.chip { display:inline-block; font-size:11px; padding:1px 6px; border-radius:999px;
        border:1px solid var(--line); color:var(--muted); margin-left:6px; vertical-align:1px; }
.text { white-space:pre-wrap; word-break:break-word; }
.ref { border-left:3px solid var(--line); padding:2px 0 2px 10px; margin:6px 0;
       color:var(--muted); font-size:14px; white-space:pre-wrap; }
figure { margin:10px 0 4px; }
img { max-width:100%; border-radius:8px; border:1px solid var(--line); display:block; }
figcaption, .cap { font-size:12px; color:var(--muted); margin-top:4px; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
.card { border:1px solid var(--line); border-radius:10px; padding:8px 10px; margin:8px 0;
        background:var(--quote); }
.card .t { font-weight:600; }
.card .d { font-size:13px; color:var(--muted); }
.file { display:inline-flex; align-items:center; gap:6px; border:1px solid var(--line);
        border-radius:8px; padding:4px 8px; margin:6px 8px 0 0; font-size:13px; }
.fwd { border-left:3px solid var(--accent); background:var(--quote); border-radius:0 10px 10px 0;
       padding:8px 12px; margin:8px 0; }
.fwd .h { font-weight:600; font-size:13px; margin-bottom:6px; }
.fwd .item { padding:4px 0; border-top:1px dashed var(--line); }
.fwd .item:first-of-type { border-top:none; }
.fwd .fwd { background:transparent; border-left:2px dashed var(--line); margin:6px 0; padding:6px 10px; }
.fwd .who2 { color:var(--muted); font-size:12px; }
.muted { color:var(--muted); font-size:13px; }
footer { max-width:900px; margin:36px auto 0; color:var(--muted); font-size:12px; }
"""


def _esc(text) -> str:
    return html.escape(str(text or ""))


def _html_media(items: Iterable[dict], out_dir: Path) -> str:
    out: list[str] = []
    for item in items:
        src, real = _media_src(item, out_dir)
        kind = item.get("kind") or "file"
        cloud = item.get("source") == "cloud"
        if kind == "image" and real and src:
            out.append(
                f'<figure><a href="{_esc(src)}"><img src="{_esc(src)}" alt="{_esc(item.get("name"))}"'
                f' loading="lazy"></a><figcaption>{_esc(_media_label(item, with_kind=False))}'
                "</figcaption></figure>"
            )
        elif real and src:
            out.append(f'<a class="file" href="{_esc(src)}">📎 {_esc(_media_label(item))}</a>')
        elif cloud:
            out.append(f'<span class="file">☁ {_esc(_media_label(item))}（云盘文件，未落到本地缓存）</span>')
        else:
            out.append(f'<span class="file">📎 {_esc(_media_label(item))}（未缓存）</span>')
    return "".join(out)


def _html_forward(fwd: dict, out_dir: Path,
                  tz=timezone(timedelta(hours=8))) -> str:
    rows: list[str] = []
    for item in fwd.get("items") or []:
        body = (item.get("text") or "").strip()
        sub_link = item.get("link") or {}
        if not body and sub_link:
            body = sub_link.get("title") or sub_link.get("url") or ""
        inner = f'<div class="text">{_esc(body)}</div>' if body else ""
        if sub_link.get("url") and sub_link.get("url") not in body:
            inner += (f'<div class="cap"><a href="{_esc(sub_link["url"])}">'
                      f'{_esc(sub_link["url"])}</a></div>')
        for entry in item.get("entries") or []:
            if entry.get("kind") == "text" and entry.get("text") and entry["text"] not in body:
                inner += f'<div class="text">{_esc(entry["text"])}</div>'
        inner += _html_media(item.get("media") or [], out_dir)
        if item.get("forward"):
            inner += _html_forward(item["forward"], out_dir, tz)
        clock = ""
        if item.get("send_time_epoch"):
            clock = datetime.fromtimestamp(item["send_time_epoch"], tz).strftime("%m-%d %H:%M")
        rows.append(
            f'<div class="item"><div class="who2">{_esc(item.get("sender_name") or "-")}'
            f'　{_esc(clock)}</div>{inner or "<div class=muted>（无正文）</div>"}</div>'
        )
    title = fwd.get("title") or "聊天记录"
    total = fwd.get("total") or len(fwd.get("items") or [])
    return f'<div class="fwd"><div class="h">💬 {_esc(title)}（{total} 条）</div>{"".join(rows)}</div>'


def _html_body(msg: dict, out_dir: Path) -> str:
    parts: list[str] = []
    text = (msg.get("text") or "").strip()
    link = msg.get("link") or {}

    if text:
        parts.append(f'<div class="text">{_esc(text)}</div>')

    if link:
        title = link.get("title") or link.get("url") or "链接"
        url = link.get("url")
        desc = link.get("desc")
        head = f'<a href="{_esc(url)}">{_esc(title)}</a>' if url else _esc(title)
        desc_html = f'<div class="d">{_esc(desc)}</div>' if desc and desc != title else ""
        parts.append(f'<div class="card"><div class="t">{head}</div>{desc_html}</div>')

    rendered: set[tuple] = set()
    for entry in msg.get("entries") or []:
        if entry.get("kind") == "text" and entry.get("text") and entry["text"] not in text:
            parts.append(f'<div class="text">{_esc(entry["text"])}</div>')
        elif entry.get("kind") == "media":
            media = entry.get("media") or {}
            rendered.add(_media_identity(media))
            parts.append(_html_media([media], out_dir))

    voice = msg.get("voice") or {}
    if voice:
        dur = f"{voice['duration_s']} 秒" if voice.get("duration_s") else "时长未知"
        media = msg.get("media") or []
        src, real = _media_src(media[0], out_dir) if media else (None, False)
        if real and src:
            parts.append(
                f'<div class="cap">🎙 语音 {dur} · <a href="{_esc(src)}">'
                f'{_esc(Path(src).name)}</a>（silk 原始文件，需转码后播放）</div>'
            )
        else:
            parts.append(f'<div class="cap">🎙 语音 {dur}（缓存未落地）</div>')

    media_items = [m for m in (msg.get("media") or []) if _media_identity(m) not in rendered]
    if media_items and not voice:
        parts.append(_html_media(media_items, out_dir))

    tz = _msg_tz(msg)
    if msg.get("forward"):
        parts.append(_html_forward(msg["forward"], out_dir, tz))
    quote = msg.get("quote") or {}
    if quote:
        who = quote.get("sender_name") or "引用"
        body = quote.get("text") or quote.get("title") or ""
        parts.append(f'<div class="ref">引用 <b>{_esc(who)}</b>：{_esc(body)}</div>')
        if quote.get("forward"):
            parts.append(_html_forward(quote["forward"], out_dir, tz))
    for item in msg.get("ocr") or []:
        if item.get("text"):
            parts.append(f'<div class="cap">图内文字：{_esc(item["text"][:400])}</div>')
    if msg.get("extra_text") and msg.get("text") != msg["extra_text"]:
        parts.append(f'<div class="cap">卡片文本：{_esc(msg["extra_text"][:400])}</div>')
    return "".join(parts)


def to_html(messages, *, out_dir: Path, title: str = "聊天记录",
            meta: dict | None = None) -> str:
    blocks: list[str] = []
    current_day = None
    for msg in messages:
        day = _day(msg)
        if day != current_day:
            blocks.append(f'<div class="day">{_esc(day)}</div>')
            current_day = day
        sender = (msg.get("sender_name") or "-").strip()
        chip = TYPE_CHIPS.get(msg.get("type_label") or "", "")
        chip_html = f'<span class="chip">{_esc(chip)}</span>' if chip and chip != "文本" else ""
        fallback = chip or msg.get("type_label") or "空消息"
        body = (_html_body(msg, out_dir)
                or f'<div class="muted">（{_esc(fallback)}：客户端未落正文）</div>')
        blocks.append(
            '<div class="msg">'
            f'<div class="avatar">{_esc(sender[:1])}</div>'
            '<div class="body">'
            f'<div class="who"><b>{_esc(sender)}</b>{chip_html}　{_esc(_clock(msg))}</div>'
            f"{body}</div></div>"
        )
    meta_html = "".join(
        f"<span>{_esc(k)}：{_esc(v)}</span>" for k, v in (meta or {}).items() if v
    )
    return (
        "<!doctype html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{_esc(title)}</title>\n<style>{CSS}</style>\n</head>\n<body>\n<main>\n"
        f"<h1>{_esc(title)}</h1>\n<div class=\"meta\">{meta_html}</div>\n"
        + "\n".join(blocks)
        + "\n</main>\n<footer>由 open-wechat-cli 只读导出 · 附件为本地缓存原文 · "
        "silk 语音需转码后播放</footer>\n</body>\n</html>\n"
    )


def summary_line(messages: list[dict]) -> dict:
    """给导出回执用的行列统计。"""
    days = sorted({_day(m) for m in messages if _day(m)})
    return {"messages": len(messages), "days": len(days),
            "first": days[0] if days else None, "last": days[-1] if days else None}


def dump_manifest(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
