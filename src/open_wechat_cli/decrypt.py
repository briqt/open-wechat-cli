"""解密 + WAL 回放 + quick_check 门禁。

语义：
* 先写临时文件，校验通过才替换目标，绝不留下半成品；
* 校验不过就保留为 `.bad` 并报错，上一份好数据不受影响；
* key 不对时不产出任何文件（page1 校验在解密前完成）。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import codec
from .errors import DataError, FormatError
from .snapshot import iter_snapshot_dbs
from .sqlite_util import quick_check, sqlite_ro_uri

try:  # pragma: no cover
    import sqlite3
except ImportError:  # pragma: no cover
    sqlite3 = None  # type: ignore[assignment]


@dataclass
class DbResult:
    rel: str
    ok: bool
    pages: int = 0
    tables: int = 0
    quick_check: str = ""
    wal_frames: int = 0
    wal_committed: int = 0
    wal_checksum_alg: str = ""
    duration_s: float = 0.0
    out: str | None = None
    error: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "rel": self.rel,
            "ok": self.ok,
            "pages": self.pages,
            "tables": self.tables,
            "quick_check": self.quick_check,
            "wal_frames": self.wal_frames,
            "wal_committed_frames": self.wal_committed,
            "wal_checksum_alg": self.wal_checksum_alg,
            "duration_s": round(self.duration_s, 2),
            "out": self.out,
            "error": self.error,
            "notes": self.notes,
        }


def decrypt_one(
    src: str,
    dst: str,
    raw_key: bytes,
    *,
    apply_wal: bool = True,
    progress: Callable[[int, int], None] | None = None,
) -> DbResult:
    rel = os.path.basename(src)
    result = DbResult(rel=rel, ok=False)
    t0 = time.time()

    if not codec.verify_key(raw_key, src):
        result.error = "key 在该库的 page1 上校验失败"
        result.duration_s = time.time() - t0
        return result

    tmp = dst + ".tmp"
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    try:
        info = codec.decrypt_file(raw_key, src, tmp, progress=progress)
        result.pages = info["pages"]
    except (FormatError, OSError) as exc:
        result.error = str(exc)
        result.duration_s = time.time() - t0
        _drop(tmp)
        return result

    wal_path = src + "-wal"
    if apply_wal and os.path.exists(wal_path):
        try:
            wal = codec.apply_wal(raw_key, wal_path, tmp)
            result.wal_frames = wal.frames
            result.wal_committed = wal.last_commit_frame
            result.wal_checksum_alg = wal.checksum_alg
            if not wal.header_ok:
                result.notes.append("WAL 头 magic 不符，已跳过回放")
            if wal.frames and not wal.last_commit_frame:
                result.notes.append("WAL 里没有提交帧，未回放（未提交事务）")
        except Exception as exc:  # noqa: BLE001 - 回放失败不该毁掉主库
            result.notes.append(f"WAL 回放失败：{exc}")

    check, tables = quick_check(tmp)
    result.quick_check = check
    result.tables = tables
    if check != "ok":
        bad = dst + ".bad"
        os.replace(tmp, bad)
        for sidecar in ("-wal", "-shm"):
            _drop(tmp + sidecar)
        result.error = f"quick_check={check}"
        result.notes.append(f"不一致快照，已保留为 {os.path.basename(bad)}")
        result.duration_s = time.time() - t0
        return result

    os.replace(tmp, dst)
    for sidecar in ("-wal", "-shm"):
        _drop(tmp + sidecar)  # quick_check 可能给临时文件建过 sidecar
    result.ok = True
    result.out = dst
    result.duration_s = time.time() - t0
    return result


def _drop(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def decrypt_snapshot(
    snapshot_dir: str,
    out_dir: str,
    raw_key: bytes,
    *,
    only: tuple[str, ...] | None = None,
    apply_wal: bool = True,
    verify_only: bool = False,
    progress: Callable[[str], None] | None = None,
) -> list[DbResult]:
    """解密快照目录下的全部（或指定）库，返回逐库结果。"""
    if sqlite3 is None:  # pragma: no cover
        raise DataError("当前 Python 缺少 sqlite3 模块", repair="换用带 sqlite3 的官方 Python")

    snap_root = Path(snapshot_dir)
    if not snap_root.is_dir():
        raise DataError(
            f"快照目录不存在：{snapshot_dir}",
            repair="先跑 `owc snapshot --account <corp_id> --out <dir>`，再把 --snapshot 指到该目录",
        )
    db_files = list(iter_snapshot_dbs(snapshot_dir))
    if not db_files:
        raise DataError(
            f"快照目录里没有 .db：{snapshot_dir}",
            repair="确认 --snapshot 指向 `owc snapshot` 的输出目录（其下应有 Data/*.db）",
        )

    out_root = Path(out_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    results: list[DbResult] = []
    for src in db_files:
        rel = str(src.relative_to(snapshot_dir)).replace("\\", "/")
        name = src.name
        if only and name not in only and rel not in only:
            continue
        if progress:
            progress(f"decrypt {rel}")
        if verify_only:
            ok = codec.verify_key(raw_key, str(src))
            results.append(DbResult(rel=rel, ok=ok, error=None if ok else "key 校验失败"))
            continue
        dst = str(out_root / rel)
        results.append(decrypt_one(str(src), dst, raw_key, apply_wal=apply_wal))
    return results


def summarize(results: list[DbResult]) -> dict:
    ok = [r for r in results if r.ok]
    return {
        "total": len(results),
        "ok": len(ok),
        "failed": len(results) - len(ok),
        "pages": sum(r.pages for r in ok),
        "wal_applied": sum(r.wal_committed for r in ok),
        "duration_s": round(sum(r.duration_s for r in results), 2),
    }


__all__ = ["DbResult", "decrypt_one", "decrypt_snapshot", "summarize", "quick_check",
           "sqlite_ro_uri"]
