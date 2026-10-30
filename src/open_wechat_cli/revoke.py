"""撤回还原：把撤回通知（content_type 1011）和 `message_revoke_record_table_v2` 对上。

企微把「谁在什么时候撤回了消息」记在 message.db 的 `message_revoke_record_table_v2`：
`con_nid`（会话数字 id）、`sendtime`（撤回时间）、`msgdata_pb`（`4` 撤回者、`8` 提示文案、
`12` 撤回时间、`13` 标志位）。消息表里的撤回通知本身没有正文，所以之前只能显示成空的系统消息。

这里不猜内容：只把通知补成「该消息已被撤回」并标出撤回者与时间。原始消息若已被客户端删除，
就如实没有正文。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import proto

#: 匹配容差（秒）：通知与撤回记录的时间戳可能差 1-2 秒
TOLERANCE = 2


@dataclass
class RevokeRecord:
    conv_nid: int
    sendtime: int
    revoker_id: int | None = None
    text: str = "该消息已被撤回"

    def to_dict(self) -> dict:
        return {"revoker_id": self.revoker_id, "revoked_at": self.sendtime,
                "text": self.text}


def _parse_msgdata(blob: bytes | None) -> tuple[int | None, str | None]:
    if not blob:
        return None, None
    revoker = None
    text = None
    try:
        for no, wt, value in proto.fields(blob, limit=40):
            if wt == proto.WT_VARINT and no == 4 and isinstance(value, int):
                revoker = revoker or value
            elif wt == proto.WT_LEN and no == 8 and isinstance(value, bytes):
                try:
                    candidate = value.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if candidate.strip():
                    text = candidate.strip()
    except proto.ProtobufError:
        return revoker, text
    return revoker, text


class RevokeIndex:
    """撤回记录索引：按 (会话数字 id, 撤回时间) 查。"""

    def __init__(self, records: list[RevokeRecord], lookup_path: Path | None) -> None:
        self._by_conv: dict[int, list[RevokeRecord]] = {}
        for record in records:
            self._by_conv.setdefault(record.conv_nid, []).append(record)
        for items in self._by_conv.values():
            items.sort(key=lambda r: r.sendtime)
        self._lookup_path = lookup_path
        self._lookup: sqlite3.Connection | None = None

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_conv.values())

    @classmethod
    def load(cls, db_dir: str | Path) -> RevokeIndex | None:
        """从解密目录加载；没有撤回表就返回 None（不是错误）。"""
        base = Path(db_dir)
        message_db = base / "message.db"
        if not message_db.exists():
            return None
        records: list[RevokeRecord] = []
        try:
            con = sqlite3.connect(f"file:{message_db.resolve().as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        try:
            rows = con.execute(
                "select con_nid, sendtime, msgdata_pb from message_revoke_record_table_v2"
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            con.close()
        for conv_nid, sendtime, blob in rows:
            if conv_nid is None or sendtime is None:
                continue
            revoker, text = _parse_msgdata(bytes(blob) if blob else None)
            records.append(RevokeRecord(conv_nid=int(conv_nid), sendtime=int(sendtime),
                                        revoker_id=revoker, text=text or "该消息已被撤回"))
        if not records:
            return None
        lookup = base / "message_lookup.db"
        return cls(records, lookup if lookup.exists() else None)

    def _conv_of(self, message_id: str | int) -> int | None:
        if self._lookup is None:
            return None
        try:
            if self._lookup is None:
                return None
            row = self._lookup.execute(
                "select con_numeric_id from message_lookup_table where message_id = ?",
                (int(message_id),),
            ).fetchone()
        except (sqlite3.Error, ValueError, TypeError):
            return None
        return int(row[0]) if row else None

    def match(self, message_id: str | int, send_time: int | None) -> dict | None:
        if send_time is None or not self:
            return None
        open_lookup(self)
        conv = self._conv_of(message_id)
        if conv is None:
            return None
        for record in self._by_conv.get(conv, []):
            if abs(record.sendtime - int(send_time)) <= TOLERANCE:
                return record.to_dict()
        return None

    def close(self) -> None:
        if self._lookup is not None:
            self._lookup.close()
            self._lookup = None


def open_lookup(index: RevokeIndex | None) -> None:
    """惰性打开 message_lookup.db（只读）。"""
    if index is None or index._lookup is not None or index._lookup_path is None:
        return
    try:
        index._lookup = sqlite3.connect(
            f"file:{index._lookup_path.resolve().as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        index._lookup = None
