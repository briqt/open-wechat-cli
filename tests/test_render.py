"""Markdown / HTML 渲染测试：结构化字段要真的落到人看得懂的稿子上。"""

from __future__ import annotations

from open_wechat_cli import render


def _media(tmp_path, name="shot.png", data=b"\x89PNG"):
    media_dir = tmp_path / "media"
    media_dir.mkdir(exist_ok=True)
    path = media_dir / name
    path.write_bytes(data)
    return f"media/{name}", path


def _msg(**kwargs) -> dict:
    base = {
        "message_id": "m1", "conv_id": "c1", "conv_name": "群A",
        "sender_id": "u1", "sender_name": "张三",
        "send_time": "2026-10-29T14:32:05+08:00", "send_time_epoch": 1793000000,
        "content_type": 2, "type_label": "text", "text": "你好",
        "urls": [], "ref": None, "link": None, "media": [], "segments": [],
        "entries": [], "forward": None, "voice": None,
    }
    base.update(kwargs)
    return base


def _image_msg(tmp_path) -> dict:
    rel, path = _media(tmp_path)
    return _msg(
        message_id="m2", content_type=14, type_label="image", text=None,
        media=[{"kind": "image", "name": "shot.png", "size": 4, "width": 800, "height": 600,
                "md5": None, "key": "ab" * 40, "duration_s": None,
                "local_path": str(path), "exists": True, "copy": str(path)}],
    )


def test_markdown_has_images_links_and_forward(tmp_path):
    rel, _ = _media(tmp_path, "fwd.png")
    fwd_path = tmp_path / rel
    messages = [
        _msg(text="<script>alert(1)</script> 这行要转义"),
        _image_msg(tmp_path),
        _msg(message_id="m3", content_type=13, type_label="doc_link", text="需求文档",
             link={"url": "https://doc.weixin.qq.com/doc/x", "title": "需求文档",
                   "desc": "文档", "icon": None}),
        _msg(message_id="m4", content_type=4, type_label="chat_record",
             text="张三和李四（2 条转发消息）",
             forward={"title": "张三和李四", "total": 2, "members": [], "items": [
                 {"sender_name": "李四", "send_time_epoch": 1792999000, "content_type": 2,
                  "type_label": "text", "text": "转发的一句话", "media": [], "link": None},
                 {"sender_name": "王五", "send_time_epoch": 1792999100, "content_type": 14,
                  "type_label": "image", "text": None, "link": None,
                  "media": [{"kind": "image", "name": "fwd.png", "size": 4, "width": 10,
                             "height": 10, "md5": None, "key": None, "duration_s": None,
                             "local_path": str(fwd_path), "exists": True,
                             "copy": str(fwd_path)}]},
             ]}),
        _msg(message_id="m5", content_type=16, type_label="voice", text=None,
             voice={"duration_s": 13, "size": 100, "key": "cd" * 40, "text": None},
             media=[{"kind": "voice", "name": None, "size": 100, "width": None, "height": None,
                     "md5": None, "key": "cd" * 40, "duration_s": 13,
                     "local_path": None, "exists": False}]),
    ]
    md = render.to_markdown(messages, out_dir=tmp_path, title="群A",
                            meta={"条数": 5, "工具": "owc test"})
    assert "# 群A" in md and "## 2026-10-29" in md
    assert "![](media/shot.png)" in md
    assert "https://doc.weixin.qq.com/doc/x" in md
    assert "> **张三和李四**（2 条）" in md
    assert "转发的一句话" in md
    assert "![](media/fwd.png)" in md
    assert "🎙 语音 13s" in md
    assert "<script>" in md  # Markdown 不做 HTML 转义，但内容必须原样保留


def test_html_escapes_and_renders_structures(tmp_path):
    messages = [
        _msg(text="<script>alert(1)</script> & 符号"),
        _image_msg(tmp_path),
        _msg(message_id="m4", content_type=4, type_label="chat_record",
             text="张三和李四（1 条转发消息）",
             forward={"title": "张三和李四", "total": 1, "members": [], "items": [
                 {"sender_name": "李四", "send_time_epoch": 1792999000, "content_type": 2,
                  "type_label": "text", "text": "转发内容 <b>x</b>", "media": [],
                  "link": None},
             ]}),
    ]
    html_doc = render.to_html(messages, out_dir=tmp_path, title="群A", meta={"条数": 3})
    assert html_doc.startswith("<!doctype html>")
    assert "<script>alert(1)</script>" not in html_doc
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_doc
    assert '<img src="media/shot.png"' in html_doc
    assert 'class="fwd"' in html_doc
    assert "转发内容 &lt;b&gt;x&lt;/b&gt;" in html_doc
    assert "<style>" in html_doc and "</html>" in html_doc


def test_rich_text_media_rendered_once(tmp_path):
    """图文消息的附件同时在 media[] 和 entries[] 里，导出稿只能出现一次。"""
    rel, path = _media(tmp_path, "inline.png")
    item = {"kind": "image", "name": "inline.png", "size": 4, "width": 800, "height": 600,
            "md5": None, "key": "ef" * 40, "duration_s": None,
            "local_path": str(path), "exists": True, "copy": str(path)}
    msg = _msg(message_id="m7", content_type=123, type_label="rich_text",
               text="配图说明",
               media=[item],
               entries=[{"kind": "media", "type": 14, "media": item},
                        {"kind": "text", "type": 2, "text": "配图说明"}])

    md = render.to_markdown([msg], out_dir=tmp_path, title="群A")
    html_doc = render.to_html([msg], out_dir=tmp_path, title="群A")
    assert md.count("![](media/inline.png)") == 1
    assert html_doc.count('<img src="media/inline.png"') == 1
    assert md.count("配图说明") == 1
    assert html_doc.count("配图说明") == 1


def test_uncached_media_is_labelled(tmp_path):
    messages = [_msg(message_id="m9", content_type=14, type_label="image", text=None,
                     media=[{"kind": "image", "name": "gone.png", "size": 10, "width": 1,
                             "height": 1, "md5": None, "key": None, "duration_s": None,
                             "local_path": None, "exists": False}])]
    md = render.to_markdown(messages, out_dir=tmp_path, title="群A")
    html_doc = render.to_html(messages, out_dir=tmp_path, title="群A")
    assert "未缓存" in md
    assert "未缓存" in html_doc


def test_summary_line():
    stats = render.summary_line([
        _msg(), _msg(message_id="m2", send_time="2026-10-30T01:00:00+08:00")])
    assert stats == {"messages": 2, "days": 2, "first": "2026-10-29", "last": "2026-10-30"}


def test_forward_clock_follows_message_offset(tmp_path):
    """转发的子消息只有 epoch，显示时间必须按父消息的时区，不能依赖本机时区。"""
    from datetime import datetime, timedelta, timezone

    epoch = int(datetime(2026, 10, 29, 17, 16, tzinfo=timezone(timedelta(hours=8))).timestamp())
    item = {"sender_name": "李四", "send_time_epoch": epoch, "content_type": 2,
            "type_label": "text", "text": "转发的一句话", "media": [], "link": None}
    fwd = {"title": "李四", "total": 1, "members": [], "items": [item]}

    sh = _msg(content_type=4, type_label="chat_record", text="李四（1 条转发消息）",
              send_time="2026-10-29T17:16:00+08:00", send_time_epoch=epoch, forward=fwd)
    utc = dict(sh, send_time="2026-10-29T09:16:00+00:00")

    md_sh = render.to_markdown([sh], out_dir=tmp_path, title="群A")
    md_utc = render.to_markdown([utc], out_dir=tmp_path, title="群A")
    assert "10-29 17:16" in md_sh
    assert "10-29 09:16" in md_utc
    assert render._msg_tz(sh).utcoffset(None) == timedelta(hours=8)
    assert render._msg_tz(utc).utcoffset(None) == timedelta(0)
    # 没有偏移信息时固定按东八区，避免"跑在哪台机器上结果不同"
    assert render._msg_tz({"send_time": "2026-10-29T17:16:00"}).utcoffset(None) == \
        timedelta(hours=8)

