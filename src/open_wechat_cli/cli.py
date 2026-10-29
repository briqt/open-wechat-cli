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

from . import __version__, codec, extract, host, keyscan, output, snapshot as snap
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
        data = json.loads(Path(key_file).read_text(encoding="utf-8"))
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
    store = Store(args.db_dir)
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
    store = Store(args.db_dir)
    since = _parse_when(args.since, args.tz)
    until = _parse_when(args.until, args.tz)
    payload = store.messages(conv=args.conv, since=since, until=until, query=args.query,
                             limit=args.limit, cursor=args.cursor, tz_name=args.tz,
                             order=args.order, with_counts=args.with_counts)
    lines = []
    for m in payload["items"]:
        body = m["text"] or f"[{m['type_label']}]"
        body = body.replace("\n", " ")[:160]
        lines.append(f"{m['send_time']} {m['conv_name'][:18]:<20} {m['sender_name'][:14]:<16} {body}")
    if payload["next_cursor"]:
        lines.append(f"next_cursor: {payload['next_cursor']}")
    output.emit(payload, command="messages list", as_json=args.json, text="\n".join(lines))
    return 0


def cmd_messages_export(args) -> int:
    store = Store(args.db_dir)
    since = _parse_when(args.since, args.tz)
    until = _parse_when(args.until, args.tz)
    out = Path(args.out)
    fmt = args.format
    if fmt == "jsonl":
        out.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with out.open("w", encoding="utf-8") as fh:
            for msg in store.iter_messages(conv=args.conv, since=since, until=until,
                                           query=args.query, tz_name=args.tz):
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
                count += 1
        payload = {"out": str(out), "format": fmt, "messages": count}
        output.emit(payload, command="messages export", as_json=args.json,
                    text=f"{count} 条 -> {out}")
        return 0
    if fmt == "txt":
        out.mkdir(parents=True, exist_ok=True)
        files: dict[str, object] = {}
        counts: dict[str, int] = {}
        try:
            for msg in store.iter_messages(conv=args.conv, since=since, until=until,
                                           query=args.query, tz_name=args.tz):
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
                body = msg["text"] or f"[{msg['type_label']}]"
                fh.write(f"[{msg['send_time']}] {msg['sender_name']}: {body}\n")
        finally:
            for fh in files.values():
                fh.close()
        payload = {"out": str(out), "format": fmt, "conversations": len(files),
                   "messages": sum(counts.values())}
        output.emit(payload, command="messages export", as_json=args.json,
                    text=f"{payload['messages']} 条 / {payload['conversations']} 个会话 -> {out}")
        return 0
    raise UsageError(f"不支持的导出格式：{fmt}", repair="用 --format jsonl 或 --format txt")


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


def cmd_history(args) -> int:
    """一条命令走完整链路：定位账号 → 取 key → 快照 → 解密 → 查询。"""
    accounts = host.list_accounts()
    account = host.select_account(accounts, args.account)
    cache = Path(args.cache_dir) if args.cache_dir else (
        Path(os.environ.get("OWC_STATE_DIR")
             or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "open-wechat-cli"))
        / "cache" / account.corp_id
    )
    snap_dir = cache / "snapshot"
    dec_dir = cache / "decrypted"
    need_pipeline = not (args.reuse and (dec_dir / "Data" / "message.db").exists())
    steps: list[dict] = []

    if need_pipeline:
        dbs = tuple(x.strip() for x in args.dbs.split(",") if x.strip())
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

    store = Store(str(dec_dir / "Data"))
    since = _parse_when(args.since, args.tz)
    until = _parse_when(args.until, args.tz)
    payload = store.messages(conv=args.conv, since=since, until=until, query=args.query,
                             limit=args.limit, cursor=args.cursor, tz_name=args.tz,
                             order=args.order, with_counts=args.with_counts)
    payload["account"] = account.corp_id
    payload["client_version"] = (host.detect_installs() or [None])[0].version if host.detect_installs() else None
    payload["cache_dir"] = str(cache)
    payload["steps"] = steps
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
    ml.add_argument("--order", default="asc", choices=("asc", "desc"),
                    help="asc 从头往后翻页；desc 从最新往前翻页")
    ml.add_argument("--with-counts", action="store_true",
                    help="附带全量统计（会扫完整库，默认关闭）")
    ml.set_defaults(func=cmd_messages_list)

    me = msg_sub.add_parser("export")
    me.add_argument("--db-dir", required=True)
    me.add_argument("--out", required=True)
    me.add_argument("--format", default="jsonl", choices=("jsonl", "txt"))
    me.add_argument("--conv")
    me.add_argument("--since")
    me.add_argument("--until")
    me.add_argument("--query")
    me.add_argument("--limit-per-conv", type=int, default=0)
    me.add_argument("--tz", default="local")
    me.set_defaults(func=cmd_messages_export)

    hist = sub.add_parser("history", help="一条命令：取 key → 快照 → 解密 → 查询")
    hist.add_argument("--account")
    hist.add_argument("--dbs", default="message.db,session.db,user.db")
    hist.add_argument("--cache-dir", help="快照与解密产物目录（默认状态目录下）")
    hist.add_argument("--reuse", action="store_true", help="已有解密结果时跳过快照/解密")
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
