"""把消息里的资源键解析成本地缓存文件。

企微把图片/文件/语音/视频的消息本体放在 `Cache` 下，`CacheMapping` 库里存了
「资源键 → 缓存相对路径」的映射（实测 `mapping(type, key, file_name, ...)`，
type 1=文件 2=图片 3=语音 4=视频）。消息 content 里带的那串 ASCII hex 就是
这里的 `key`，因此不需要猜测命名规则，也不用扫盘。

设计约束：
* 只读：CacheMapping 先复制到临时目录再打开，避免只读打开带 WAL 的库时出错，
  也不会给客户端库加写锁；
* 解不出来就返回 None，绝不把资源键当路径瞎猜。
"""

from __future__ import annotations

import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

#: mapping.type -> (资源类别, Cache 下的相对目录)
MAPPING_KINDS = {
    1: ("file", "Cache/File"),
    2: ("image", "Cache/Image"),
    3: ("voice", "Cache/Voice"),
    4: ("video", "Cache/Video"),
}

_HEX = re.compile(r"^[0-9a-fA-F]{16,}$")


def is_ascii_hex(value: bytes | str | None) -> bool:
    """资源键是长串 ASCII hex；二进制形态的 id 不是。"""
    if value is None:
        return False
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return False
    return bool(_HEX.match(value.strip()))


class MediaIndex:
    """账号目录下的缓存映射索引。"""

    def __init__(self, root: Path, rows: dict[str, tuple[int, str]]) -> None:
        self.root = root
        self._rows = rows
        self._resolved: dict[str, dict] = {}

    # -- 构造 ------------------------------------------------------------ #
    @classmethod
    def from_account_root(cls, root: str | Path) -> MediaIndex | None:
        root_path = Path(root)
        mapping_dir = root_path / "CacheMapping"
        if not mapping_dir.is_dir():
            return None
        candidates = sorted(
            (p for p in mapping_dir.glob("*.db")),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            return None
        rows = _load_mapping(candidates[0])
        if not rows:
            return None
        return cls(root_path, rows)

    # -- 查询 ------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self._rows)

    def resolve(self, key: str | None) -> dict | None:
        if not key:
            return None
        cached = self._resolved.get(key)
        if cached is not None:
            return cached
        row = self._rows.get(key)
        if not row:
            return None
        mtype, file_name = row
        kind, sub = MAPPING_KINDS.get(mtype, (f"type{mtype}", None))
        normalized = str(file_name).replace("\\", "/")
        raw = Path(normalized)
        path = raw if raw.is_absolute() else (self.root / sub / normalized if sub else raw)
        try:
            exists = path.exists()
            size = path.stat().st_size if exists else None
        except OSError:
            exists, size = False, None
        info = {
            "kind": kind,
            "path": str(path),
            "exists": exists,
            "size": size,
        }
        self._resolved[key] = info
        return info


def _load_mapping(db: Path) -> dict[str, tuple[int, str]]:
    """复制后再打开：客户端正在写这个库，只读挂载容易拿不到 WAL 里的新记录。"""
    with tempfile.TemporaryDirectory(prefix="owc-map-") as tmp:
        copy = Path(tmp) / "mapping.db"
        try:
            shutil.copy2(db, copy)
            for suffix in ("-wal", "-shm"):
                sidecar = db.with_name(db.name + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, copy.with_name(copy.name + suffix))
        except OSError:
            return {}
        try:
            con = sqlite3.connect(str(copy))
        except sqlite3.Error:
            return {}
        try:
            rows = con.execute("select type, key, file_name from mapping").fetchall()
        except sqlite3.Error:
            return {}
        finally:
            con.close()
    out: dict[str, tuple[int, str]] = {}
    for mtype, key, file_name in rows:
        if not key or not file_name:
            continue
        out[str(key)] = (int(mtype), str(file_name))
    return out


def sanitize_name(name: str | None, fallback: str = "media") -> str:
    """导出附件时用的安全文件名。"""
    cleaned = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", (name or "").strip())
    cleaned = cleaned.strip(". ")
    return cleaned[:120] or fallback
