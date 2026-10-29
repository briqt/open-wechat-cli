"""只读 sqlite 访问的小工具：一律 mode=ro，绝不开写连接。"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def sqlite_ro_uri(path: str | os.PathLike[str]) -> str:
    """构造跨平台可用的只读 URI（Windows 路径需要正斜杠）。"""
    p = Path(path).resolve()
    posix = p.as_posix()
    if not posix.startswith("/"):
        posix = "/" + posix  # Windows: C:/... -> /C:/...
    return f"file:{posix}?mode=ro"


def connect(path: str | os.PathLike[str]) -> sqlite3.Connection:
    con = sqlite3.connect(sqlite_ro_uri(path), uri=True, timeout=5.0)
    con.text_factory = lambda b: b.decode("utf-8", "replace")  # type: ignore[assignment]
    con.execute("pragma query_only=1")
    return con


def quick_check(path: str | os.PathLike[str]) -> tuple[str, int]:
    """返回 (quick_check 结果, 表数量)。打不开时返回 (错误信息, 0)。"""
    try:
        con = connect(path)
    except sqlite3.Error as exc:
        return f"open failed: {exc}", 0
    try:
        check = con.execute("pragma quick_check").fetchone()
        tables = con.execute(
            "select count(*) from sqlite_master where type='table'"
        ).fetchone()
        return str(check[0]), int(tables[0] or 0)
    except sqlite3.Error as exc:
        return f"query failed: {exc}", 0
    finally:
        con.close()


def table_info(con: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    rows = con.execute(f'pragma table_info("{table}")').fetchall()
    return [(r[1], (r[2] or "").upper()) for r in rows]


def list_tables(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(
        "select name from sqlite_master where type='table' order by name"
    )]


def pick_table(con: sqlite3.Connection, candidates: tuple[str, ...]) -> str | None:
    tables = {t.lower(): t for t in list_tables(con)}
    for cand in candidates:
        if cand.lower() in tables:
            return tables[cand.lower()]
    return None


def pick_column(columns: list[str], candidates: tuple[str, ...]) -> str | None:
    lowered = {c.lower(): c for c in columns}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None
