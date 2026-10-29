"""只读查询层：会话 / 联系人 / 消息，带游标分页与显式时区。

对表名、列名做候选匹配，避免客户端小版本改 schema 就直接报错。
所有连接都是 mode=ro + query_only，不会写解密库。
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

from . import extract
from .errors import DataError, UsageError
from .sqlite_util import connect, pick_column, pick_table, table_info

MESSAGE_TABLES = ("message_table", "messages", "message")
CONVERSATION_TABLES = ("conversation_table", "conversations", "session_table", "session")
USER_TABLES = ("user_table", "users", "user", "contact_table")

MESSAGE_COLUMNS = {
    "id": ("message_id", "msg_id", "id"),
    "sender": ("sender_id", "sender", "from_id", "talker"),
    "conv": ("conversation_id", "conv_id", "session_id", "chat_id"),
    "type": ("content_type", "msg_type", "type", "message_type"),
    "time": ("send_time", "create_time", "timestamp", "time", "msg_time"),
    "content": ("content", "body", "msg_content", "payload"),
}
CONVERSATION_COLUMNS = {
    "id": ("id", "conversation_id", "conv_id", "session_id"),
    "name": ("name", "roomname", "conv_name", "title"),
    "remark": ("roomname_remark", "remark", "alias"),
    "last_time": ("last_message_time", "last_time", "update_time", "sort_time"),
}
USER_COLUMNS = {
    "id": ("id", "user_id", "vid", "userid"),
    "name": ("name", "nick_name", "nickname", "display_name"),
    "english": ("english_name", "en_name", "pinyin"),
    "remark": ("remark", "alias", "note"),
}

MAX_LIMIT = 1000


@dataclass
class TableSpec:
    table: str
    columns: dict[str, str]

    @property
    def complete(self) -> bool:
        return True


def _tz(name: str | None):
    if not name or name == "local":
        return datetime.now().astimezone().tzinfo or timezone.utc
    try:
        return ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001
        raise UsageError(
            f"不认识的时区：{name}",
            repair="用 --tz local 或 IANA 名称，例如 Asia/Shanghai",
        ) from exc


def iso_time(ts, tz) -> str | None:
    if ts in (None, ""):
        return None
    try:
        value = int(ts)
    except (TypeError, ValueError):
        return str(ts)
    return datetime.fromtimestamp(value, tz).isoformat()


def encode_cursor(payload: dict) -> str:
    return base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> dict:
    if not cursor:
        return {}
    pad = "=" * (-len(cursor) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(cursor + pad))
    except Exception as exc:  # noqa: BLE001
        raise UsageError(
            "游标不可解析",
            repair="游标是上一次输出的 next_cursor 原样回填；不要自己拼",
        ) from exc


class Store:
    def __init__(self, db_dir: str) -> None:
        self.db_dir = Path(db_dir)
        if not self.db_dir.is_dir():
            raise DataError(
                f"解密目录不存在：{db_dir}",
                repair="先跑 `owc decrypt`，或把 --db-dir 指到解密产物目录",
            )
        self._schema: dict[str, TableSpec | None] | None = None
        self._convs: dict[str, dict] | None = None
        self._users: dict[str, str] | None = None

    # -- 基础设施 -------------------------------------------------------- #
    def db_path(self, name: str) -> Path:
        direct = self.db_dir / name
        if direct.exists():
            return direct
        for p in self.db_dir.rglob(name):
            return p
        raise DataError(
            f"解密目录里没有 {name}",
            repair="确认 `owc decrypt` 覆盖了该库；必要时用 `--dbs` 显式指定要解密的库",
            details={"db_dir": str(self.db_dir)},
        )

    def _connect(self, name: str) -> sqlite3.Connection:
        return connect(self.db_path(name))

    def _spec(self, db: str, tables: tuple[str, ...], cols: dict[str, tuple[str, ...]]) -> TableSpec | None:
        path = self.db_dir / db
        if not path.exists():
            return None
        con = connect(path)
        try:
            table = pick_table(con, tables)
            if not table:
                return None
            names = [c for c, _ in table_info(con, table)]
            mapped = {}
            for logical, candidates in cols.items():
                col = pick_column(names, candidates)
                if col:
                    mapped[logical] = col
            return TableSpec(table=table, columns=mapped)
        finally:
            con.close()

    def schema(self) -> dict[str, TableSpec | None]:
        if self._schema is None:
            self._schema = {
                "message": self._spec("message.db", MESSAGE_TABLES, MESSAGE_COLUMNS),
                "conversation": self._spec("session.db", CONVERSATION_TABLES, CONVERSATION_COLUMNS),
                "user": self._spec("user.db", USER_TABLES, USER_COLUMNS),
            }
        return self._schema

    def schema_dict(self) -> dict:
        out = {}
        for k, v in self.schema().items():
            out[k] = {"table": v.table, "columns": v.columns} if v else None
        return out

    # -- 名称映射 -------------------------------------------------------- #
    def conversations(self) -> dict[str, dict]:
        if self._convs is not None:
            return self._convs
        spec = self.schema()["conversation"]
        convs: dict[str, dict] = {}
        if spec:
            con = self._connect("session.db")
            try:
                cols = spec.columns
                sel = [cols.get(k) for k in ("id", "name", "remark", "last_time")]
                sql = "select " + ", ".join(f'"{c}"' if c else "null" for c in sel) + f' from "{spec.table}"'
                for cid, name, remark, last in con.execute(sql):
                    cid_s = _s(cid)
                    if not cid_s:
                        continue
                    convs[cid_s] = {
                        "id": cid_s,
                        "name": _s(name) or _s(remark) or cid_s,
                        "remark": _s(remark),
                        "last_time": last,
                    }
            finally:
                con.close()
        self._resolve_single_chat_names(convs)
        self._convs = convs
        return convs

    def self_id(self) -> str | None:
        """把自己认出来：出现在最多会话里的 sender_id 就是本账号。

        单聊 session 里 name 常为空（`S:a_b`），有了自己就能挑出对方的名字。
        """
        if getattr(self, "_self_id", "__unset__") != "__unset__":
            return self._self_id
        self._self_id = None
        spec = self.schema()["message"]
        if not spec:
            return None
        con = None
        try:
            con = self._connect("message.db")
            cols = spec.columns
            row = con.execute(
                f'select "{cols["sender"]}", count(distinct "{cols["conv"]}") as c '
                f'from "{spec.table}" group by 1 order by c desc limit 1'
            ).fetchone()
            if row and row[0] is not None:
                self._self_id = _s(row[0])
        except Exception:  # noqa: BLE001 - 缺 message.db 时只是少个名字
            self._self_id = None
        finally:
            if con is not None:
                con.close()
        return self._self_id

    def _resolve_single_chat_names(self, convs: dict[str, dict]) -> None:
        me = self.self_id()
        users = self.users()
        for cid, info in convs.items():
            name = info.get("name") or ""
            if name and name != cid:
                continue
            if not cid.startswith("S:") or "_" not in cid:
                continue
            left, _, right = cid[2:].partition("_")
            cands = [x for x in (left, right) if x and x != me]
            for cand in cands:
                if users.get(cand):
                    info["name"] = users[cand]
                    info["peer_id"] = cand
                    break

    def users(self) -> dict[str, str]:
        if self._users is not None:
            return self._users
        spec = self.schema()["user"]
        users: dict[str, str] = {}
        if spec:
            con = self._connect("user.db")
            try:
                cols = spec.columns
                sel = [cols.get(k) for k in ("id", "name", "english", "remark")]
                sql = "select " + ", ".join(f'"{c}"' if c else "null" for c in sel) + f' from "{spec.table}"'
                for uid, name, english, remark in con.execute(sql):
                    key = _s(uid)
                    if not key:
                        continue
                    users[key] = _s(name) or _s(english) or _s(remark) or key
            finally:
                con.close()
        self._users = users
        return users

    # -- 查询 ------------------------------------------------------------ #
    def sessions(self, *, limit: int = 50, cursor: str | None = None,
                 query: str | None = None, tz_name: str | None = None) -> dict:
        tz = _tz(tz_name)
        spec = self.schema()["conversation"]
        if not spec:
            raise DataError(
                "session.db 里没找到会话表",
                repair="确认解密是否成功；用 `owc doctor` 看 quick_check 与表数量",
                details={"schema": self.schema_dict()},
            )
        limit = _clamp(limit)
        after = decode_cursor(cursor).get("last_time")
        con = self._connect("session.db")
        try:
            cols = spec.columns
            sel = ", ".join(
                f'"{cols[k]}"' if cols.get(k) else "null"
                for k in ("id", "name", "remark", "last_time")
            )
            where: list[str] = []
            params: list = []
            if after is not None and cols.get("last_time"):
                where.append(f'"{cols["last_time"]}" < ?')
                params.append(after)
            if query and cols.get("name"):
                where.append(f'"{cols["name"]}" like ?')
                params.append(f"%{query}%")
            sql = f'select {sel} from "{spec.table}"'
            if where:
                sql += " where " + " and ".join(where)
            if cols.get("last_time"):
                sql += f' order by "{cols["last_time"]}" desc'
            sql += f" limit {limit + 1}"
            rows = list(con.execute(sql, params))
        finally:
            con.close()

        more = len(rows) > limit
        rows = rows[:limit]
        items = []
        for cid, name, remark, last in rows:
            cid_s = _s(cid)
            items.append({
                "conv_id": cid_s,
                "name": _s(name) or _s(remark) or cid_s,
                "remark": _s(remark),
                "last_message_time": iso_time(last, tz),
            })
        next_cursor = None
        if more and rows:
            next_cursor = encode_cursor({"last_time": rows[-1][3]})
        return {"items": items, "next_cursor": next_cursor}

    def contacts(self, *, limit: int = 50, cursor: str | None = None,
                 query: str | None = None) -> dict:
        spec = self.schema()["user"]
        if not spec:
            raise DataError("user.db 里没找到用户表", repair="确认解密成功；`owc doctor` 看表数量")
        limit = _clamp(limit)
        after = decode_cursor(cursor).get("id")
        con = self._connect("user.db")
        try:
            cols = spec.columns
            sel = ", ".join(
                f'"{cols[k]}"' if cols.get(k) else "null"
                for k in ("id", "name", "english", "remark")
            )
            where, params = [], []
            if after is not None and cols.get("id"):
                where.append(f'"{cols["id"]}" > ?')
                params.append(after)
            if query and cols.get("name"):
                where.append(f'"{cols["name"]}" like ?')
                params.append(f"%{query}%")
            sql = f'select {sel} from "{spec.table}"'
            if where:
                sql += " where " + " and ".join(where)
            if cols.get("id"):
                sql += f' order by "{cols["id"]}"'
            sql += f" limit {limit + 1}"
            rows = list(con.execute(sql, params))
        finally:
            con.close()
        more = len(rows) > limit
        rows = rows[:limit]
        items = [
            {
                "user_id": _s(uid),
                "name": _s(name) or _s(english) or _s(remark) or _s(uid),
                "english_name": _s(english),
                "remark": _s(remark),
            }
            for uid, name, english, remark in rows
        ]
        next_cursor = encode_cursor({"id": items[-1]["user_id"]}) if more and items else None
        return {"items": items, "next_cursor": next_cursor}

    def iter_messages(
        self,
        *,
        conv: str | None = None,
        since: int | None = None,
        until: int | None = None,
        query: str | None = None,
        tz_name: str | None = None,
        after: tuple | None = None,
        order: str = "asc",
        max_items: int | None = None,
        batch: int = 2000,
    ) -> Iterator[dict]:
        spec = self.schema()["message"]
        if not spec:
            raise DataError(
                "message.db 里没找到消息表",
                repair="确认 message.db 已解密并通过 quick_check",
                details={"schema": self.schema_dict()},
            )
        cols = spec.columns
        for need in ("id", "conv", "time", "content"):
            if need not in cols:
                raise DataError(
                    f"消息表缺少必需列：{need}",
                    repair="schema 可能变了；把 `owc schema` 的输出贴出来再适配列名",
                    details={"table": spec.table, "columns": cols},
                )
        convs = self.conversations()
        users = self.users()
        tz = _tz(tz_name)
        sel = ", ".join(
            f'"{cols[k]}"' if cols.get(k) else "null"
            for k in ("id", "sender", "conv", "type", "time")
        ) + f', "{cols["content"]}"'
        where: list[str] = []
        params: list = []
        if conv:
            where.append(f'"{cols["conv"]}" = ?')
            params.append(conv)
        if since is not None:
            where.append(f'"{cols["time"]}" >= ?')
            params.append(int(since))
        if until is not None:
            where.append(f'"{cols["time"]}" <= ?')
            params.append(int(until))
        if after is not None and after[0] is not None:
            t, ident = after[0], after[1]
            cmp_t = ">" if order == "asc" else "<"
            cmp_i = ">" if order == "asc" else "<"
            where.append(
                f'("{cols["time"]}" {cmp_t} ? OR ("{cols["time"]}" = ? AND "{cols["id"]}" {cmp_i} ?))'
            )
            params.extend([t, t, ident])
        sql = f'select {sel} from "{spec.table}"'
        if where:
            sql += " where " + " and ".join(where)
        direction = "asc" if order == "asc" else "desc"
        sql += f' order by "{cols["time"]}" {direction}, "{cols["id"]}" {direction}'
        if max_items and not query:
            sql += f" limit {max_items}"

        yielded = 0
        con = self._connect("message.db")
        try:
            con.execute("pragma temp_store=memory")
            cur = con.execute(sql, params)
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                for mid, sender, cid, ctype, stime, content in rows:
                    cid_s = _s(cid)
                    sender_s = _s(sender)
                    conv_info = convs.get(cid_s, {})
                    body = extract.extract(_int(ctype), _bytes(content))
                    text = body["text"]
                    if query:
                        hay = f"{text or ''}\n{conv_info.get('name', '')}"
                        if query.lower() not in hay.lower():
                            continue
                    yielded += 1
                    yield {
                        "message_id": _s(mid),
                        "conv_id": cid_s,
                        "conv_name": conv_info.get("name", cid_s),
                        "sender_id": sender_s,
                        "sender_name": users.get(sender_s, sender_s),
                        "send_time": iso_time(stime, tz),
                        "send_time_epoch": _int(stime),
                        "content_type": _int(ctype),
                        "type_label": extract.type_label(_int(ctype)),
                        "text": text,
                        "urls": body["urls"],
                        "ref": body["ref"],
                        "_cursor_id": mid if isinstance(mid, int) else _s(mid),
                    }
                    if max_items and yielded >= max_items:
                        return
        finally:
            con.close()

    def messages(self, *, conv=None, since=None, until=None, query=None,
                 limit=50, cursor=None, tz_name=None, order="asc",
                 with_counts=False) -> dict:
        limit = _clamp(limit)
        decoded = decode_cursor(cursor)
        after = None
        if decoded:
            after = (decoded.get("t"), decoded.get("id"))
        items: list[dict] = []
        for msg in self.iter_messages(conv=conv, since=since, until=until, query=query,
                                      tz_name=tz_name, after=after, order=order,
                                      max_items=limit + 1):
            items.append(dict(msg))
        next_cursor = None
        if len(items) > limit:
            items = items[:limit]
            last = items[-1]
            next_cursor = encode_cursor({"t": last["send_time_epoch"], "id": last["_cursor_id"]})
        for item in items:
            item.pop("_cursor_id", None)
        return {
            "items": items,
            "next_cursor": next_cursor,
            "counts": self.counts() if with_counts else None,
        }

    def counts(self) -> dict:
        spec = self.schema()["message"]
        if not spec:
            return {}
        con = self._connect("message.db")
        try:
            cols = spec.columns
            total = con.execute(f'select count(*) from "{spec.table}"').fetchone()[0]
            by_type = dict(
                con.execute(
                    f'select "{cols["type"]}", count(*) from "{spec.table}" group by 1 order by 2 desc limit 20'
                ).fetchall()
            ) if cols.get("type") else {}
            convs = con.execute(
                f'select count(distinct "{cols["conv"]}") from "{spec.table}"'
            ).fetchone()[0]
            return {"messages": int(total), "conversations": int(convs),
                    "by_type": {str(k): int(v) for k, v in by_type.items()}}
        finally:
            con.close()

    def stats(self) -> dict:
        out = {"schema": self.schema_dict()}
        try:
            out["counts"] = self.counts()
        except DataError as exc:
            out["counts_error"] = exc.message
        out["users"] = len(self.users())
        out["conversations"] = len(self.conversations())
        return out


def _s(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return str(v)


def _bytes(v) -> bytes | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        return v
    if isinstance(v, memoryview):
        return bytes(v)
    return str(v).encode()


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _clamp(limit: int) -> int:
    if limit <= 0:
        raise UsageError("--limit 必须是正整数")
    return min(limit, MAX_LIMIT)


__all__ = ["Store", "iso_time", "encode_cursor", "decode_cursor", "TableSpec", "_tz"]
