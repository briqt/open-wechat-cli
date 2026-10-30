"""输出约定：结果走 stdout，诊断走 stderr，异常统一收口成一条 JSON。

* `--json` 时 stdout 只有一个 JSON 对象，可供 Agent/脚本解析；
* 默认文本模式，人类可读；
* 任何错误都以 {"ok": false, "error": {...}} 落在 stderr，并带退出码。
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

SCHEMA = 1

_JSON_MODE = False


def configure_stdio() -> None:
    """把 stdout/stderr 固定成 UTF-8。

    结果里有中文会话名和正文；重定向到文件/管道时 Python 默认改用 locale 编码
    （中文 Windows 是 cp936），消费方按 UTF-8 解析就会拿到非法字节，甚至因
    超出 GBK 的字符（emoji 等）在 flush 时抛 UnicodeEncodeError。控制台本身
    仍由 Windows 控制台按 UTF-8 转码，行为不变。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            if (stream.encoding or "").lower().replace("-", "") == "utf8":
                continue
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            continue


def set_json_mode(enabled: bool) -> None:
    """在 --json 下把诊断也写成一行一行的 JSON，便于"最后一行是错误体"的解析。"""
    global _JSON_MODE
    _JSON_MODE = enabled


def emit(data: Any, *, command: str, as_json: bool, text: str | None = None,
         meta: dict[str, Any] | None = None) -> None:
    """输出成功结果。"""
    if as_json:
        payload = {
            "ok": True,
            "command": command,
            "schema": SCHEMA,
            "data": data,
            "meta": {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **(meta or {})},
        }
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    else:
        if text is not None:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")


def emit_error(err: Any, *, command: str, as_json: bool) -> int:
    """输出失败结果，返回退出码。"""
    body = {"ok": False, "command": command, "schema": SCHEMA, "error": err.to_dict()}
    if as_json:
        # 单行：stderr 可能先是若干 diag 行，最后一行一定是这份错误体
        sys.stderr.write(json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n")
    else:
        lines = [f"[{err.code}] {err.message}"]
        if err.repair:
            lines.append(f"  repair: {err.repair}")
        for k, v in (err.details or {}).items():
            lines.append(f"  {k}: {v}")
        sys.stderr.write("\n".join(lines) + "\n")
    return err.exit_code


def diag(message: str) -> None:
    """诊断信息永远走 stderr，不污染 stdout 的结果。"""
    text = message.rstrip("\n")
    if _JSON_MODE:
        sys.stderr.write(json.dumps({"type": "diag", "message": text}, ensure_ascii=False) + "\n")
    else:
        sys.stderr.write(text + "\n")


def table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    """极简对齐表，避免额外依赖。"""
    if not rows:
        return "(空)"
    widths = {c: len(c) for c in columns}
    cells: list[list[str]] = []
    for r in rows:
        line = []
        for c in columns:
            v = r.get(c)
            s = "" if v is None else str(v)
            widths[c] = max(widths[c], len(s))
            line.append(s)
        cells.append(line)
    out = ["  ".join(c.ljust(widths[c]) for c in columns)]
    out.append("  ".join("-" * widths[c] for c in columns))
    for line in cells:
        out.append("  ".join(s.ljust(widths[c]) for s, c in zip(line, columns)))
    return "\n".join(out)
