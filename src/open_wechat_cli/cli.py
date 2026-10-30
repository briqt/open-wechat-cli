"""命令行入口：owc。

设计约定：
* 结果 → stdout，诊断 → stderr；
* `--json` 输出单一 JSON 信封，便于 Agent 消费；
* 错误统一带 code / repair / 退出码；
* key 默认不回显，只落 0600 的私有 key 文件，输出里只有指纹。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__, codec, extract, host, keyscan, media, output, render, silk, snapshot as snap
from .decrypt import decrypt_snapshot, summarize
from .errors import (
    DataError,
    DependencyError,
    FormatError,
    OwcError,
    PreconditionError,
    UsageError,
)
from .store import Store, _tz

DEFAULT_KEY_DIR = Path(
    os.environ.get("OWC_STATE_DIR")
    or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "open-wechat-cli")
) / "keys"

#: md/html 渲染上限：整份稿子要在内存里拼成一个大字符串
MAX_RENDER = 20000


def _export_root() -> Path:
    """默认导出根目录：跟 keys/cache 同一个状态目录，不往当前目录乱写。"""
    return Path(
        os.environ.get("OWC_STATE_DIR")
        or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "open-wechat-cli")
    ) / "export"


def _default_export_out(fmt: str, conv: str | None) -> Path:
    """没给 --out 时的落点：<state>/export/<会话或 all>/messages.<ext>（txt 是目录）。"""
    base = _export_root() / media.sanitize_name(conv or "all", "all")
    if fmt == "txt":
        return base / "txt"
    return base / f"messages.{fmt}"


# --------------------------------------------------------------------------- #
# key 文件
# --------------------------------------------------------------------------- #
def _key_path(account: str) -> Path:
    return DEFAULT_KEY_DIR / f"{account}.json"


def save_key(account: str, raw_key: bytes, *, extra: dict | None = None,
             path: str | None = None) -> Path:
    target = Path(path) if path else _key_path(account)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "account": account,
        "key_fingerprint": codec.key_fingerprint(raw_key),
        "key_hex": raw_key.hex(),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **(extra or {}),
    }
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def load_key(account: str | None, key_file: str | None, key_hex: str | None) -> tuple[bytes, str]:
    """按 参数 > 环境变量 > 默认 key 文件 的顺序取 key，返回 (key, 来源说明)。"""
    if key_hex:
        return _parse_key_hex(key_hex), "cli"
    if key_file:
        path = Path(key_file)
        if not path.exists():
            # 显式 --key-file 是"缓存位置"，不是"必须存在"：交给上层回退到现场扫描。
            raise PreconditionError(
                f"key 文件不存在：{key_file}",
                repair="去掉 --key-file 现场取 key，或先跑 `owc key scan --key-file <路径>`",
            )
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PreconditionError(
                f"key 文件无法解析：{key_file}",
                repair="重跑 `owc key scan --key-file <路径>` 覆盖它，或删掉该文件后重新取 key",
            ) from exc
        return _parse_key_hex(data.get("key_hex") or data.get("key")), key_file
    env = os.environ.get("OWC_KEY_HEX")
    if env:
        return _parse_key_hex(env), "env"
    if account:
        path = _key_path(account)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return _parse_key_hex(data.get("key_hex")), str(path)
    raise PreconditionError(
        "没有可用的 key",
        repair="先跑 `owc key scan --account <corp_id>`（客户端需正在运行且已登录）",
    )


def _parse_key_hex(value: str | None) -> bytes:
    if not value:
        raise UsageError("key 为空")
    try:
        data = bytes.fromhex(value.strip())
    except ValueError as exc:
        raise UsageError("key 必须是 32 位十六进制（16 字节 raw key）") from exc
    if len(data) != 16:
        raise UsageError(f"key 长度 {len(data)} 字节，应为 16")
    return data


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #
def _parse_when(value: str | None, tz_name: str | None) -> int | None:
    if not value:
        return None
    tz = _tz(tz_name)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return int(datetime.strptime(value, fmt).replace(tzinfo=tz).timestamp())
        except ValueError:
            continue
    raise UsageError(
        f"时间格式无法解析：{value}",
        repair="用 2026-09-01 或 2026-09-01T12:00:00；时区用 --tz local|Asia/Shanghai",
    )


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_doctor(args) -> int:
    env = host.environment()
    disk = shutil.disk_usage(str(Path.home()))
    env["home_free_gib"] = round(disk.free / 2**30, 1)
    env["version"] = __version__
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo("Asia/Shanghai")
        env["tz_database"] = "ok"
    except Exception:  # noqa: BLE001 - Windows 上缺 tzdata 时只会退化为 local
        env["tz_database"] = "missing (pip install tzdata)"
    if not env["pycryptodome"]:
        env["repair"] = "pip install pycryptodome（解密与取 key 都需要）"
    text = [
        f"platform        {env['platform']}",
        f"python          {env['python']['version']}  {env['python']['executable']}",
        f"pycryptodome    {'yes' if env['pycryptodome'] else 'MISSING'}",
        f"data_root       {env['data_root']} ({'ok' if env['data_root_exists'] else 'missing'})",
        f"tz_database     {env['tz_database']}",
    ]
    for inst in env["client_installs"]:
        text.append(f"client          {inst['version'] or '?'}  {inst['exe']}")
    if env["running"]:
        for p in env["running"]:
            text.append(
                f"running         pid={p['pid']} started={p.get('started')} "
                f"private={p.get('private_mb')}MB  {p.get('exe')}"
            )
    else:
        text.append("running         (WXWork.exe 未运行：取 key 会失败)")
    for acc in env["accounts"]:
        text.append(
            f"account         {acc['corp_id']}  dbs={len(acc['dbs'])}  "
            f"last={acc['last_activity']}  message.db={'yes' if acc['has_message_db'] else 'no'}"
        )
    output.emit(env, command="doctor", as_json=args.json, text="\n".join(text))
    return 0


def cmd_accounts(args) -> int:
    accounts = host.list_accounts(Path(args.root) if args.root else None)
    payload = [a.to_dict() for a in accounts]
    lines = []
    for a in accounts:
        lines.append(
            f"{a.corp_id}  message.db={'yes' if a.is_chat_account else 'no'}  "
            f"dbs={len(a.dbs)}  last={time.strftime('%Y-%m-%d %H:%M', time.localtime(a.last_activity))}"
        )
        if args.verbose:
            for d in a.dbs:
                lines.append(f"    {d.rel:<28} {d.size / 2**20:8.1f} MiB  wal={d.has_wal}")
    output.emit(payload, command="accounts", as_json=args.json,
                text="\n".join(lines) or "(没有账号目录)")
    return 0


def cmd_key_scan(args) -> int:
    if not codec.AES:
        raise DependencyError("pycryptodome 未安装", repair="pip install pycryptodome")

    account = None
    if args.account:
        accounts = host.list_accounts()
        account = host.select_account(accounts, args.account)
    elif args.db:
        pass
    else:
        raise UsageError("需要 --account 或 --db", repair="`owc accounts` 看 corp id")

    target_db = args.db or (account.db("session.db").path if account and account.db("session.db") else None)
    if not target_db:
        target_db = account.db("message.db").path if account and account.db("message.db") else None
    if not target_db or not os.path.exists(target_db):
        raise PreconditionError(
            "找不到用于校验 key 的库",
            repair="指定 --db <某个加密 .db>；或在 `owc accounts` 里确认账号下有库",
        )

    def progress(msg: str) -> None:
        output.diag(f"[key] {msg}")

    hit = keyscan.hunt_key(
        target_db,
        step=args.step,
        max_hits=args.max_hits,
        time_budget=args.time_budget,
        progress=progress,
    )
    payload = hit.to_public_dict()
    payload["verified_on"] = os.path.basename(target_db)
    if args.reveal_key:
        payload["key_hex"] = hit.key.hex()
    if args.no_persist:
        payload["persisted"] = None
    else:
        account_id = account.corp_id if account else Path(target_db).parent.parent.name
        saved = save_key(
            account_id,
            hit.key,
            extra={"verified_on": os.path.basename(target_db), "pid": hit.pid},
            path=args.key_file,
        )
        payload["persisted"] = str(saved)

    text = [
        f"key_fingerprint {payload['key_fingerprint']}",
        f"verified_on     {payload['verified_on']}",
        f"pid             {payload['pid']}",
        f"candidates      {payload['candidates_tested']:,} / {payload['scanned_mb']} MiB",
        f"elapsed         {payload['elapsed_s']}s",
        f"persisted       {payload['persisted']}",
    ]
    if args.reveal_key:
        text.append(f"key_hex         {payload['key_hex']}")
    output.emit(payload, command="key scan", as_json=args.json, text="\n".join(text))
    return 0


def cmd_key_verify(args) -> int:
    key, origin = load_key(args.account, args.key_file, args.key_hex)
    ok = codec.verify_key(key, args.db)
    info = {
        "db": args.db,
        "key_fingerprint": codec.key_fingerprint(key),
        "key_source": origin,
        "verified": ok,
        "probe": codec.probe_db(args.db),
    }
    output.emit(info, command="key verify", as_json=args.json,
                text=f"{'OK' if ok else 'FAIL'}  {args.db}\n  fingerprint={info['key_fingerprint']}")
    return 0 if ok else FormatError.exit_code


def cmd_snapshot(args) -> int:
    accounts = host.list_accounts()
    account = host.select_account(accounts, args.account)
    dbs = tuple(x.strip() for x in args.dbs.split(",") if x.strip()) if args.dbs else None

    def progress(msg: str) -> None:
        output.diag(f"[snapshot] {msg}")

    result = snap.snapshot_account(
        account, args.out, dbs=dbs, with_index=args.with_index, progress=progress
    )
    payload = result.to_dict()
    lines = [f"account  {account.corp_id}", f"out      {result.out_dir}"]
    for rel, info in payload["dbs"].items():
        lines.append(f"  {rel:<26} {info['size'] / 2**20:8.1f} MiB  {info['duration_s']:5.1f}s"
                     f"{'  (copy 期间有变化)' if info['changed_during_copy'] else ''}")
    for rel, info in payload["wals"].items():
        lines.append(f"  {rel + '-wal':<26} {info['size'] / 2**20:8.2f} MiB")
    for sk in result.skipped:
        lines.append(f"  skip {sk['rel']}: {sk['reason']}")
    output.emit(payload, command="snapshot", as_json=args.json, text="\n".join(lines))
    return 0


def cmd_decrypt(args) -> int:
    snap_dir = args.snapshot
    account = args.account
    if not account and snap_dir:
        manifest_path = Path(snap_dir) / "manifest.json"
        if manifest_path.exists():
            try:
                account = json.loads(manifest_path.read_text(encoding="utf-8"))["account"]["corp_id"]
            except Exception:  # noqa: BLE001
                account = None
    key, origin = load_key(account, args.key_file, args.key_hex)
    output.diag(f"[decrypt] key_fingerprint={codec.key_fingerprint(key)} source={origin}")

    only = tuple(x.strip() for x in args.dbs.split(",") if x.strip()) if args.dbs else None

    def progress(msg: str) -> None:
        output.diag(f"[decrypt] {msg}")

    results = decrypt_snapshot(
        snap_dir, args.out, key, only=only, apply_wal=not args.no_wal,
        verify_only=args.verify_only, progress=progress,
    )
    payload = {"summary": summarize(results), "dbs": [r.to_dict() for r in results]}
    lines = [f"{'OK  ' if r.ok else 'FAIL'} {r.rel:<26} pages={r.pages:<8} tables={r.tables:<5} "
             f"wal={r.wal_committed}  {r.quick_check or r.error or ''}" for r in results]
    for r in results:
        for note in r.notes:
            lines.append(f"     note: {note}")
    output.emit(payload, command="decrypt", as_json=args.json, text="\n".join(lines))
    failed = [r for r in results if not r.ok]
    if failed:
        output.diag(
            f"[decrypt] {len(failed)} 个库没通过校验：{', '.join(r.rel for r in failed)}；"
            "多半是活库在复制期间被改写，重跑 `owc snapshot` 即可"
        )
        return DataError.exit_code
    return 0


def cmd_schema(args) -> int:
    store = Store(args.db_dir)
    payload = store.stats()
    text = [f"db_dir  {args.db_dir}"]
    for k, v in payload["schema"].items():
        text.append(f"{k:<13} {v['table'] if v else '(missing)'}")
        if v:
            for logical, col in v["columns"].items():
                text.append(f"    {logical:<10} -> {col}")
    counts = payload.get("counts") or {}
    if counts:
        text.append(f"messages={counts.get('messages')} conversations={counts.get('conversations')}")
    output.emit(payload, command="schema", as_json=args.json, text="\n".join(text))
    return 0


def cmd_sessions(args) -> int:
    store = Store(args.db_dir, account_root=_media_root(args.media_root, None, args.db_dir))
    payload = store.sessions(limit=args.limit, cursor=args.cursor, query=args.query,
                             tz_name=args.tz)
    lines = [f"{s['last_message_time'] or '-':<26} {s['name'][:40]:<42} {s['conv_id']}"
             for s in payload["items"]]
    if payload["next_cursor"]:
        lines.append(f"next_cursor: {payload['next_cursor']}")
    output.emit(payload, command="sessions list", as_json=args.json, text="\n".join(lines))
    return 0


def cmd_contacts(args) -> int:
    store = Store(args.db_dir)
    payload = store.contacts(limit=args.limit, cursor=args.cursor, query=args.query)
    lines = [f"{c['name'][:30]:<32} {c['user_id']}" for c in payload["items"]]
    if payload["next_cursor"]:
        lines.append(f"next_cursor: {payload['next_cursor']}")
    output.emit(payload, command="contacts list", as_json=args.json, text="\n".join(lines))
    return 0


def cmd_messages_list(args) -> int:
    store = Store(args.db_dir, account_root=_media_root(args.media_root, args.account, args.db_dir))
    _maybe_index(store, getattr(args, "index", False))
    since = _parse_when(args.since, args.tz)
    until = _parse_when(args.until, args.tz)
    payload = store.messages(conv=args.conv, since=since, until=until, query=args.query,
                             limit=args.limit, cursor=args.cursor, tz_name=args.tz,
                             order=args.order, with_counts=args.with_counts)
    lines = []
    for m in payload["items"]:
        body = _render_body(m)
        lines.append(f"{m['send_time']} {m['conv_name'][:18]:<20} {m['sender_name'][:14]:<16} {body}")
        lines.extend(_render_extra(m))
    if payload["next_cursor"]:
        lines.append(f"next_cursor: {payload['next_cursor']}")
    output.emit(payload, command="messages list", as_json=args.json, text="\n".join(lines))
    return 0


def _human_size(size) -> str:
    if not isinstance(size, int) or size <= 0:
        return ""
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _render_body(msg: dict) -> str:
    """文本模式的一行摘要：正文优先，其次是附件/链接/语音/转发的可辨认描述。"""
    text = msg.get("text")
    if text:
        return text.replace("\n", " ")[:160]
    label = msg.get("type_label") or "unknown"
    if msg.get("voice"):
        voice = msg["voice"]
        dur = voice.get("duration_s")
        return f"[语音 {dur}s]" if dur else "[语音]"
    if msg.get("link"):
        link = msg["link"]
        return f"[{label}] {link.get('title') or link.get('url') or ''}".strip()
    if msg.get("forward"):
        fwd = msg["forward"]
        return f"[聊天记录] {fwd.get('title') or ''} ({len(fwd.get('items') or [])} 条)".strip()
    media_items = msg.get("media") or []
    if media_items:
        first = media_items[0]
        bits = [first.get("name") or media_items[0].get("kind") or label]
        if first.get("width") and first.get("height"):
            bits.append(f"{first['width']}x{first['height']}")
        size = _human_size(first.get("size"))
        if size:
            bits.append(size)
        return f"[{first.get('kind') or label}] " + " ".join(str(b) for b in bits)
    return f"[{label}]"


def cmd_media_decode(args) -> int:
    """把企微语音缓存（SILK v3）转成 wav；解码器由外部提供。"""
    src = Path(args.input)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = silk.iter_silk_files(src)
    if not files:
        raise UsageError(
            f"没找到 SILK 语音：{args.input}",
            repair="指向单个 silk 文件，或 `--copy-media` 导出的 media 目录",
        )
    decoder = silk.detect_decoder(args.decoder)
    if not decoder:
        raise DependencyError(
            "没有可用的 SILK 解码器",
            repair=("装一个 silk_v3_decoder 放进 PATH，或用 --decoder \"<cmd> {in} {out}\"，"
                    f"或设环境变量 {silk.ENV_KEY}"),
        )
    decoded, skipped = [], []
    for path in files:
        target = out_dir / (path.stem + ".wav")
        try:
            info = silk.decode_file(path, target, decoder=decoder)
            decoded.append(info)
            output.diag(f"[decode] {path.name} -> {target.name} ({info['bytes']} B)")
        except (DependencyError, UsageError) as exc:
            skipped.append({"file": str(path), "reason": exc.message})
    payload = {"decoder": decoder, "out": str(out_dir),
               "decoded": decoded, "skipped": skipped}
    output.emit(payload, command="media decode", as_json=args.json,
                text=f"{len(decoded)} 个转成 wav -> {out_dir}"
                     + (f"；{len(skipped)} 个跳过" if skipped else ""))
    return 0 if decoded else DataError.exit_code


def _render_extra(msg: dict) -> list[str]:
    """附件路径、链接、转发子消息的补充行（只在文本模式用）。"""
    out: list[str] = []
    link = msg.get("link") or {}
    if link.get("url"):
        out.append(f"    link  {link['url']}")
    for item in msg.get("media") or []:
        path = item.get("local_path")
        if path:
            state = "" if item.get("exists") else "  (缓存已清理)"
            out.append(f"    file  {path}{state}")
        elif item.get("source") == "cloud":
            out.append(f"    file  {item.get('name') or '(云盘)'}  (云盘文件，未落到本地)")
    out.extend(_render_forward_lines(msg.get("forward")))
    return out


def _render_forward_lines(fwd: dict | None, depth: int = 0) -> list[str]:
    lines: list[str] = []
    if not fwd:
        return lines
    pad = "  " * depth
    for item in fwd.get("items") or []:
        link = item.get("link") or {}
        body = (item.get("text") or link.get("title") or item.get("type_label") or "")
        body = body.replace("\n", " ")
        if link.get("url"):
            body += f"  {link['url']}"
        names = ", ".join(m.get("name") or m.get("kind") or "" for m in item.get("media") or [])
        lines.append(f"    {pad}fwd   {item.get('sender_name') or '-'}: {body[:120]}"
                     + (f"  <{names}>" if names else ""))
        lines.extend(_render_forward_lines(item.get("forward"), depth + 1))
    return lines


def _copy_one(item: dict, out_dir: Path, seen: dict[str, str]) -> None:
    """把附件落到导出目录：优先硬链接（同盘零拷贝、不额外占盘），失败再复制。"""
    src = item.get("local_path")
    if not src or not item.get("exists"):
        return
    cached = seen.get(src)
    if cached is None:
        raw_name = item.get("name") or Path(src).name
        if item.get("kind") == "voice" and not Path(raw_name).suffix:
            raw_name += ".silk"  # 企微语音缓存无扩展名，实际是 silk 容器
        name = media.sanitize_name(raw_name)
        dest = out_dir / name
        stem, suffix = dest.stem, dest.suffix
        i = 1
        while dest.exists() and dest.stat().st_size != Path(src).stat().st_size:
            dest = out_dir / f"{stem}-{i}{suffix}"
            i += 1
        if not dest.exists():
            try:
                os.link(src, dest)          # 同卷：不复制数据
            except OSError:
                shutil.copy2(src, dest)     # 跨卷/文件系统不支持：退回复制
        cached = str(dest)
        seen[src] = cached
    item["copy"] = cached


def _export_rendered(args, store, since, until, fmt: str, out: Path,
                     media_out: Path, copied: int) -> int:
    """md/html：给人看的稿子。默认把附件复制出来，图片和语音才有链接可点。"""
    messages: list[dict] = []
    seen: dict[str, str] = {}
    count = 0
    extra = 0
    media_out.mkdir(parents=True, exist_ok=True)
    for msg in store.iter_messages(conv=args.conv, since=since, until=until,
                                   query=args.query, tz_name=args.tz):
        targets = list(msg.get("media") or [])
        for item in (msg.get("forward") or {}).get("items") or []:
            targets.extend(item.get("media") or [])
        for item in targets:
            before = item.get("copy")
            _copy_one(item, media_out, seen)
            if item.get("copy") and item["copy"] != before:
                extra += 1
        msg.pop("_cursor_id", None)
        messages.append(msg)
        count += 1
        if count > MAX_RENDER:
            raise DataError(
                f"渲染上限 {MAX_RENDER} 条，当前范围超过这个量",
                repair="缩小 --conv/--since 范围，或先用 --format jsonl 导出后用其它工具渲染",
            )

    title = messages[0]["conv_name"] if messages else (args.conv or "聊天记录")
    convs = sorted({m["conv_name"] or m["conv_id"] for m in messages})
    stats = render.summary_line(messages)
    meta = {
        "会话": "、".join(convs) if convs else None,
        "条数": stats["messages"],
        "时间范围": f"{messages[0]['send_time']} → {messages[-1]['send_time']}" if messages else None,
        "生成时间": datetime.now().astimezone().isoformat(timespec="seconds"),
        "工具": f"owc {__version__}（只读）",
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out_dir = out.parent.resolve()
    if fmt == "md":
        body = render.to_markdown(messages, out_dir=out_dir, title=title, meta=meta)
    else:
        body = render.to_html(messages, out_dir=out_dir, title=title, meta=meta)
    out.write_text(body, encoding="utf-8")
    payload = {"out": str(out), "format": fmt, "messages": stats["messages"],
               "days": stats["days"], "first_day": stats["first"], "last_day": stats["last"],
               "media_copied": extra, "media_dir": str(media_out)}
    output.emit(payload, command="messages export", as_json=args.json,
                text=f"{stats['messages']} 条 / {stats['days']} 天 -> {out}；"
                     f"附件 {extra} 个 -> {media_out}")
    return 0


def cmd_messages_export(args) -> int:
    media_root = _media_root(args.media_root, args.account, args.db_dir)
    store = Store(args.db_dir, account_root=media_root)
    _maybe_index(store, getattr(args, "index", False))
    since = _parse_when(args.since, args.tz)
    until = _parse_when(args.until, args.tz)
    fmt = args.format
    out = Path(args.out) if args.out else _default_export_out(fmt, args.conv)
    want_media = args.copy_media or fmt in ("md", "html")
    if args.media_out:
        media_out = Path(args.media_out)
    elif fmt == "txt":
        media_out = out / "media"
    else:
        media_out = out.parent / "media"
    seen: dict[str, str] = {}
    copied = 0

    voice_decoder = None
    if getattr(args, "decode_voice", False):
        voice_decoder = silk.detect_decoder(getattr(args, "silk_decoder", None))
        if not voice_decoder:
            raise DependencyError(
                "没有可用的 SILK 解码器，--decode-voice 无法执行",
                repair=("装一个 silk_v3_decoder 放进 PATH，或用 --silk-decoder \"<cmd> {in} {out}\"，"
                        f"或设环境变量 {silk.ENV_KEY}"),
            )

    def decode_voice(items) -> None:
        for item in items:
            target = item.get("copy")
            if not voice_decoder or item.get("kind") != "voice" or not target:
                continue
            if item.get("wav"):
                continue
            wav = Path(target).with_suffix(".wav")
            try:
                info = silk.decode_file(target, wav, decoder=voice_decoder)
                item["wav"] = info["out"]
            except (DependencyError, UsageError) as exc:
                item["wav_error"] = exc.message

    def prepare(msg: dict) -> None:
        nonlocal copied
        if not want_media:
            msg.pop("_cursor_id", None)
            return
        media_out.mkdir(parents=True, exist_ok=True)
        targets = list(msg.get("media") or [])
        for item in (msg.get("forward") or {}).get("items") or []:
            targets.extend(item.get("media") or [])
        for item in targets:
            before = item.get("copy")
            _copy_one(item, media_out, seen)
            if item.get("copy") and item["copy"] != before:
                copied += 1
        if voice_decoder:
            decode_voice(targets)
        msg.pop("_cursor_id", None)

    if fmt in ("md", "html"):
        return _export_rendered(args, store, since, until, fmt, out, media_out, copied)

    base_dir = out.parent.resolve()

    def relativize(msg: dict) -> None:
        """JSONL 里的附件路径写成相对导出文件的路径，整目录搬走也能用。"""
        items = list(msg.get("media") or [])
        for fwd_item in (msg.get("forward") or {}).get("items") or []:
            items.extend(fwd_item.get("media") or [])
        for item in items:
            raw = item.get("copy")
            if not raw:
                continue
            try:
                item["copy_abs"] = str(Path(raw).resolve())
                item["copy"] = Path(os.path.relpath(str(Path(raw).resolve()),
                                                   start=str(base_dir))).as_posix()
            except (OSError, ValueError):
                continue

    if fmt == "jsonl":
        out.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with out.open("w", encoding="utf-8") as fh:
            for msg in store.iter_messages(conv=args.conv, since=since, until=until,
                                           query=args.query, tz_name=args.tz):
                prepare(msg)
                relativize(msg)
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
                count += 1
        payload = {"out": str(out), "format": fmt, "messages": count,
                   "media_copied": copied if want_media else None,
                   "media_dir": str(media_out) if want_media else None}
        output.emit(payload, command="messages export", as_json=args.json,
                    text=f"{count} 条 -> {out}" + (f"；附件 {copied} 个 -> {media_out}"
                                                   if want_media else ""))
        return 0
    if fmt == "txt":
        out.mkdir(parents=True, exist_ok=True)
        files: dict[str, object] = {}
        counts: dict[str, int] = {}
        try:
            for msg in store.iter_messages(conv=args.conv, since=since, until=until,
                                           query=args.query, tz_name=args.tz):
                prepare(msg)
                cid = msg["conv_id"] or "unknown"
                fh = files.get(cid)
                if fh is None:
                    suffix = re.sub(r"[^0-9A-Za-z]", "", cid)[-8:] or "conv"
                    name = _safe_name(msg["conv_name"] or cid) + "__" + suffix + ".txt"
                    fh = (out / name).open("w", encoding="utf-8")
                    files[cid] = fh
                    fh.write(f"# {msg['conv_name']} ({cid})\n\n")
                counts[cid] = counts.get(cid, 0) + 1
                if args.limit_per_conv and counts[cid] > args.limit_per_conv:
                    continue
                fh.write(f"[{msg['send_time']}] {msg['sender_name']}: {_render_body(msg)}\n")
                for line in _render_extra(msg):
                    fh.write(line.strip() + "\n")
        finally:
            for fh in files.values():
                fh.close()
        payload = {"out": str(out), "format": fmt, "conversations": len(files),
                   "messages": sum(counts.values()),
                   "media_copied": copied if want_media else None,
                   "media_dir": str(media_out) if want_media else None}
        output.emit(payload, command="messages export", as_json=args.json,
                    text=f"{payload['messages']} 条 / {payload['conversations']} 个会话 -> {out}"
                         + (f"；附件 {copied} 个 -> {media_out}" if want_media else ""))
        return 0
    raise UsageError(f"不支持的导出格式：{fmt}",
                     repair="用 --format jsonl / txt / md / html")


def _maybe_index(store: Store, wanted: bool) -> None:
    """按要求给解密副本补索引；失败只降级成全表扫描，不打断查询。"""
    if not wanted:
        return
    created = store.ensure_index(progress=lambda m: output.diag(f"[index] {m}"))
    if created:
        output.diag(f"[index] 已建 {', '.join(created)}；按会话/时间查询走索引，删解密副本即回收")


def _media_root(explicit: str | None, account_hint: str | None, db_dir: str | None) -> str | None:
    """找账号根目录（Cache 与 CacheMapping 的上级）。

    顺序：显式 --media-root > 快照 manifest 里的 corp_id > 调用方给的 --account。
    """
    if explicit:
        return explicit
    corp = account_hint
    if not corp and db_dir:
        manifest = Path(db_dir).resolve().parent.parent / "snapshot" / "manifest.json"
        if manifest.exists():
            try:
                corp = json.loads(manifest.read_text(encoding="utf-8"))["account"]["corp_id"]
            except (KeyError, ValueError, OSError):
                corp = None
    if not corp:
        return None
    try:
        for account in host.list_accounts():
            if account.corp_id == corp:
                return account.root
    except Exception:  # noqa: BLE001 - 找不到账号不算错误，附件路径会缺省为空
        return None
    return None


def _safe_name(name: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (name or "").strip())
    return cleaned[:80] or "conv"


def _key_or_scan(account, key_file: str | None, key_hex: str | None) -> tuple[bytes, str]:
    """有缓存 key 就用；没有就现场扫内存，并把结果落到该账号的 key 文件。"""
    try:
        return load_key(account.corp_id, key_file, key_hex)
    except PreconditionError:
        target = account.db("session.db") or account.db("message.db")
        if not target:
            raise PreconditionError(
                "该账号下没有可用于校验 key 的库",
                repair="用 `owc accounts -v` 看账号目录；换一个已登录过的账号",
            )
        output.diag("[key] 没有缓存 key，开始扫描 WXWork.exe 内存…")
        hit = keyscan.hunt_key(target.path, progress=lambda m: output.diag(f"[key] {m}"))
        saved = save_key(account.corp_id, hit.key, path=key_file,
                         extra={"verified_on": os.path.basename(target.path), "pid": hit.pid})
        return hit.key, str(saved)


def _source_signature(account, dbs: tuple[str, ...]) -> dict:
    """源库指纹：大小 + mtime 秒。客户端一写库就会变，用来判断缓存还能不能复用。"""
    sig: dict[str, list[int]] = {}
    for entry in account.dbs:
        name = entry.rel.split("/")[-1]
        if name not in dbs:
            continue
        for path, key in ((entry.path, entry.rel), (entry.path + "-wal", entry.rel + "-wal")):
            try:
                st = os.stat(path)
            except OSError:
                continue
            sig[key] = [st.st_size, int(st.st_mtime)]
    return sig


def cmd_history(args) -> int:
    """一条命令走完整链路：定位账号 → 取 key → 快照 → 解密 → 查询。

    重复执行时不再无脑重跑 2 GB 的复制+解密：解密产物还在、源库指纹没变、
    且缓存年龄不超过 `--max-age` 秒时直接复用；`--refresh` 强制重建。
    """
    accounts = host.list_accounts()
    account = host.select_account(accounts, args.account)
    cache = Path(args.cache_dir) if args.cache_dir else (
        Path(os.environ.get("OWC_STATE_DIR")
             or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "open-wechat-cli"))
        / "cache" / account.corp_id
    )
    snap_dir = cache / "snapshot"
    dec_dir = cache / "decrypted"
    dbs = tuple(x.strip() for x in args.dbs.split(",") if x.strip())
    dec_db = dec_dir / "Data" / "message.db"
    sig_path = cache / "signature.json"
    steps: list[dict] = []

    need_pipeline = not (args.reuse and dec_db.exists())
    reused = None
    if need_pipeline and dec_db.exists() and not args.refresh:
        age = max(0.0, time.time() - dec_db.stat().st_mtime)
        max_age = getattr(args, "max_age", 0) or 0
        if max_age > 0 and age <= max_age:
            need_pipeline = False
            reused = f"缓存 {age:.0f}s ≤ --max-age {max_age:.0f}s"
        elif sig_path.exists():
            try:
                prev = json.loads(sig_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                prev = {}
            if prev.get("source") and prev["source"] == _source_signature(account, dbs):
                need_pipeline = False
                reused = "源库大小/时间戳未变"
    if reused:
        output.diag(f"[history] 复用上次解密产物（{reused}）；要强制重建加 --refresh")

    if need_pipeline:
        t0 = time.time()
        snap_result = snap.snapshot_account(account, str(snap_dir), dbs=dbs,
                                            progress=lambda m: output.diag(f"[snapshot] {m}"))
        steps.append({"step": "snapshot", "duration_s": round(time.time() - t0, 2),
                      "files": list(snap_result.files)})
        key, origin = _key_or_scan(account, args.key_file, args.key_hex)
        output.diag(f"[key] fingerprint={codec.key_fingerprint(key)} source={origin}")
        t0 = time.time()
        results = decrypt_snapshot(str(snap_dir), str(dec_dir), key,
                                   progress=lambda m: output.diag(f"[decrypt] {m}"))
        steps.append({"step": "decrypt", "duration_s": round(time.time() - t0, 2),
                      "dbs": [r.to_dict() for r in results]})
        failed = [r for r in results if not r.ok]
        if failed:
            raise DataError(
                f"解密未通过：{', '.join(r.rel for r in failed)}",
                repair="重跑 `owc history`（会重新快照）；持续失败时改用管理员 + esentutl /vss 快照",
            )
        try:
            sig_path.write_text(json.dumps(
                {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                 "source": _source_signature(account, dbs)}, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass

    store = Store(str(dec_dir / "Data"), account_root=account.root)
    _maybe_index(store, getattr(args, "index", False))
    since = _parse_when(args.since, args.tz)
    until = _parse_when(args.until, args.tz)
    payload = store.messages(conv=args.conv, since=since, until=until, query=args.query,
                             limit=args.limit, cursor=args.cursor, tz_name=args.tz,
                             order=args.order, with_counts=args.with_counts)
    payload["account"] = account.corp_id
    payload["client_version"] = (host.detect_installs() or [None])[0].version if host.detect_installs() else None
    payload["cache_dir"] = str(cache)
    payload["steps"] = steps
    payload["reused_cache"] = reused
    lines = []
    for m in payload["items"]:
        body = (m["text"] or f"[{m['type_label']}]").replace("\n", " ")[:140]
        lines.append(f"{m['send_time']} {m['conv_name'][:16]:<18} {m['sender_name'][:12]:<14} {body}")
    if payload["next_cursor"]:
        lines.append(f"next_cursor: {payload['next_cursor']}")
    output.emit(payload, command="history", as_json=args.json, text="\n".join(lines))
    return 0


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="owc",
        description="只读读取本地企业微信历史（Windows / WXWork 5.x）",
    )
    p.add_argument("--version", action="version", version=f"owc {__version__}")
    p.add_argument("--json", action="store_true", help="输出单一 JSON 信封")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="环境自检：平台、客户端、进程、账号、依赖")
    d.set_defaults(func=cmd_doctor)

    a = sub.add_parser("accounts", help="列出账号目录与库")
    a.add_argument("--root", help="覆盖数据根目录（默认 %%USERPROFILE%%\\Documents\\WXWork）")
    a.add_argument("-v", "--verbose", action="store_true")
    a.set_defaults(func=cmd_accounts)

    key = sub.add_parser("key", help="内存取 key / 校验 key")
    key_sub = key.add_subparsers(dest="key_cmd", required=True)

    ks = key_sub.add_parser("scan", help="从运行中的 WXWork.exe 内存取 raw key")
    ks.add_argument("--account", help="corp id（`owc accounts` 里的目录名）")
    ks.add_argument("--db", help="用指定加密库做校验目标（不给则自动挑 session.db/message.db）")
    ks.add_argument("--step", type=int, default=4, help="扫描步长（默认 4，找不到可用 1）")
    ks.add_argument("--max-hits", type=int, default=1)
    ks.add_argument("--time-budget", type=float, default=None, help="扫描时间上限（秒）")
    ks.add_argument("--key-file", help="输出 key 到指定文件（默认写到状态目录）")
    ks.add_argument("--no-persist", action="store_true", help="只输出指纹，不落盘 key")
    ks.add_argument("--reveal-key", action="store_true", help="显式要求在输出里带 key（默认不打印）")
    ks.set_defaults(func=cmd_key_scan)

    kv = key_sub.add_parser("verify", help="用某个库校验 key 是否正确")
    kv.add_argument("--db", required=True)
    kv.add_argument("--account")
    kv.add_argument("--key-file")
    kv.add_argument("--key-hex")
    kv.set_defaults(func=cmd_key_verify)

    s = sub.add_parser("snapshot", help="把库一致性拷到一个工作目录（只读原文件）")
    s.add_argument("--account", help="corp id；不给则挑最近活跃且带 message.db 的")
    s.add_argument("--out", required=True)
    s.add_argument("--dbs", help="逗号分隔，默认 message.db,session.db,user.db")
    s.add_argument("--with-index", action="store_true", help="同时快照 Index/*.db")
    s.set_defaults(func=cmd_snapshot)

    dec = sub.add_parser("decrypt", help="解密快照 + WAL 回放 + quick_check")
    dec.add_argument("--snapshot", required=True)
    dec.add_argument("--out", required=True)
    dec.add_argument("--account")
    dec.add_argument("--key-file")
    dec.add_argument("--key-hex")
    dec.add_argument("--dbs", help="只解密指定库（文件名，逗号分隔）")
    dec.add_argument("--no-wal", action="store_true")
    dec.add_argument("--verify-only", action="store_true")
    dec.set_defaults(func=cmd_decrypt)

    sc = sub.add_parser("schema", help="看解密库的表/列映射与统计")
    sc.add_argument("--db-dir", required=True)
    sc.set_defaults(func=cmd_schema)

    sess = sub.add_parser("sessions", help="会话列表")
    sess_sub = sess.add_subparsers(dest="sessions_cmd", required=True)
    sl = sess_sub.add_parser("list")
    sl.add_argument("--db-dir", required=True)
    sl.add_argument("--limit", type=int, default=50)
    sl.add_argument("--cursor")
    sl.add_argument("--query")
    sl.add_argument("--tz", default="local")
    sl.add_argument("--media-root", help="账号目录（含 Cache/CacheMapping），用于解析附件")
    sl.set_defaults(func=cmd_sessions)

    con = sub.add_parser("contacts", help="联系人列表")
    con_sub = con.add_subparsers(dest="contacts_cmd", required=True)
    cl = con_sub.add_parser("list")
    cl.add_argument("--db-dir", required=True)
    cl.add_argument("--limit", type=int, default=50)
    cl.add_argument("--cursor")
    cl.add_argument("--query")
    cl.set_defaults(func=cmd_contacts)

    msg = sub.add_parser("messages", help="消息查询与导出")
    msg_sub = msg.add_subparsers(dest="messages_cmd", required=True)
    ml = msg_sub.add_parser("list")
    ml.add_argument("--db-dir", required=True)
    ml.add_argument("--conv")
    ml.add_argument("--since")
    ml.add_argument("--until")
    ml.add_argument("--query")
    ml.add_argument("--limit", type=int, default=50)
    ml.add_argument("--cursor")
    ml.add_argument("--tz", default="local")
    ml.add_argument("--account", help="corp id；用于自动定位附件目录")
    ml.add_argument("--media-root", help="账号目录（含 Cache/CacheMapping），覆盖自动定位")
    ml.add_argument("--order", default="asc", choices=("asc", "desc"),
                    help="asc 从头往后翻页；desc 从最新往前翻页")
    ml.add_argument("--with-counts", action="store_true",
                    help="附带全量统计（会扫完整库，默认关闭）")
    ml.add_argument("--index", action="store_true",
                    help="在解密副本上补查询索引（一次性，按会话查询从全表扫描变毫秒级）")
    ml.set_defaults(func=cmd_messages_list)

    me = msg_sub.add_parser("export")
    me.add_argument("--db-dir", required=True)
    me.add_argument("--out", help="输出文件/目录；不给就落到状态目录的 export/ 下")
    me.add_argument("--format", default="jsonl", choices=("jsonl", "txt", "md", "html"))
    me.add_argument("--conv")
    me.add_argument("--since")
    me.add_argument("--until")
    me.add_argument("--query")
    me.add_argument("--limit-per-conv", type=int, default=0)
    me.add_argument("--tz", default="local")
    me.add_argument("--account", help="corp id；用于自动定位附件目录")
    me.add_argument("--media-root", help="账号目录（含 Cache/CacheMapping），覆盖自动定位")
    me.add_argument("--copy-media", action="store_true",
                    help="把图片/文件/语音复制到 --media-out（md/html 默认开启）")
    me.add_argument("--media-out", help="附件复制目录（默认 <out>/media）")
    me.add_argument("--index", action="store_true",
                    help="在解密副本上补查询索引（一次性，按会话查询从全表扫描变毫秒级）")
    me.add_argument("--decode-voice", action="store_true",
                    help="把复制出来的语音（SILK v3）转成 wav，需要外部解码器")
    me.add_argument("--silk-decoder", help="解码器模板，例如 \"silk_v3_decoder {in} {out}\"")
    me.set_defaults(func=cmd_messages_export)

    md = sub.add_parser("media", help="附件后处理（语音转码）")
    md_sub = md.add_subparsers(dest="media_cmd", required=True)
    mdec = md_sub.add_parser("decode", help="把 SILK v3 语音转成 wav")
    mdec.add_argument("--in", dest="input", required=True, help="silk 文件或目录")
    mdec.add_argument("--out", required=True, help="wav 输出目录")
    mdec.add_argument("--decoder", help="解码器模板（{in}/{out} 占位）")
    mdec.set_defaults(func=cmd_media_decode)

    hist = sub.add_parser("history", help="一条命令：取 key → 快照 → 解密 → 查询")
    hist.add_argument("--account")
    hist.add_argument("--dbs", default="message.db,session.db,user.db")
    hist.add_argument("--cache-dir", help="快照与解密产物目录（默认状态目录下）")
    hist.add_argument("--reuse", action="store_true", help="已有解密结果时跳过快照/解密")
    hist.add_argument("--max-age", type=float, default=600,
                      help="解密缓存的新鲜期（秒），期内且源库没变就跳过复制+解密；0 表示总是重建")
    hist.add_argument("--refresh", action="store_true", help="无视缓存，强制重拍快照并解密")
    hist.add_argument("--index", action="store_true", help="解密后补查询索引")
    hist.add_argument("--key-file")
    hist.add_argument("--key-hex")
    hist.add_argument("--conv")
    hist.add_argument("--since")
    hist.add_argument("--until")
    hist.add_argument("--query")
    hist.add_argument("--limit", type=int, default=20)
    hist.add_argument("--cursor")
    hist.add_argument("--order", default="desc", choices=("asc", "desc"))
    hist.add_argument("--with-counts", action="store_true")
    hist.add_argument("--tz", default="local")
    hist.set_defaults(func=cmd_history)

    _propagate_json(p)
    return p


def _propagate_json(parser: argparse.ArgumentParser) -> None:
    """让 `--json` 既能在子命令前、也能在子命令后使用。"""
    for action in parser._actions:  # noqa: SLF001 - argparse 没有公开接口
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            for child in action.choices.values():
                child.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                                   help=argparse.SUPPRESS)
                _propagate_json(child)


def main(argv: list[str] | None = None) -> int:
    output.configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    output.set_json_mode(bool(getattr(args, "json", False)))
    command = " ".join(x for x in (args.cmd, getattr(args, "key_cmd", None),
                                    getattr(args, "sessions_cmd", None),
                                    getattr(args, "contacts_cmd", None),
                                    getattr(args, "messages_cmd", None)) if x)
    try:
        return int(args.func(args) or 0)
    except OwcError as err:
        return output.emit_error(err, command=command, as_json=args.json)
    except KeyboardInterrupt:
        output.diag("已中断")
        return 130
    except Exception as exc:  # noqa: BLE001
        wrapped = DataError(
            f"未预期的失败：{type(exc).__name__}: {exc}",
            repair="加 --json 复跑一次并保留完整错误；必要时附上 `owc doctor` 输出",
        )
        return output.emit_error(wrapped, command=command, as_json=args.json)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
