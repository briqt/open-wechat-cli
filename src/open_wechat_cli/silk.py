"""silk → wav：只做解码器适配，不内置 SILK 解码实现。

企微语音缓存是 SILK v3（文件头 `\\x02#!SILK_V3`），浏览器/播放器都不认。
SILK 解码器本身有独立许可，不随本项目分发；这里做三件事：

1. 找出可用的解码器（显式模板 > 环境变量 `OWC_SILK_DECODER` > PATH 上的常见命令）；
2. 把企微那个前缀 `0x02` 去掉、写成临时文件，喂给解码器；
3. 校验产物真的是 WAV（`RIFF....WAVE`），失败就报错，不留半成品。

解码器约定：模板里用 `{in}` / `{out}` 占位；没有占位时按 `<cmd> <in> <out>` 追加。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import DependencyError, UsageError

SILK_MAGIC = b"#!SILK_V3"
#: PATH 上常见的解码器命令（哪个在就用哪个）
CANDIDATES = ("silk_v3_decoder", "silk_v3_decoder.exe", "silk-decoder", "silkdec")
ENV_KEY = "OWC_SILK_DECODER"


def detect_decoder(explicit: str | None = None) -> str | None:
    """返回解码器模板；优先显式参数，其次环境变量，最后 PATH 上的候选命令。"""
    for value in (explicit, os.environ.get(ENV_KEY)):
        if value and value.strip():
            return value.strip()
    for name in CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    return None


def _require(decoder: str | None) -> str:
    resolved = detect_decoder(decoder)
    if resolved:
        return resolved
    raise DependencyError(
        "没有可用的 SILK 解码器",
        repair=("装一个 silk_v3_decoder 并放进 PATH，或用 --decoder \"<cmd> {in} {out}\" 指定，"
                f"或设环境变量 {ENV_KEY}"),
    )


def normalize_header(data: bytes) -> bytes:
    """企微文件是 `\\x02#!SILK_V3`；解码器通常要 `#!SILK_V3` 开头。"""
    if data.startswith(b"\x02" + SILK_MAGIC):
        return data[1:]
    return data


def looks_like_silk(path: str | Path) -> bool:
    try:
        head = Path(path).read_bytes()[:10]
    except OSError:
        return False
    return head.startswith(b"\x02" + SILK_MAGIC) or head.startswith(SILK_MAGIC)


def is_wav(path: str | Path) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError:
        return False
    return head[:4] == b"RIFF" and head[8:12] == b"WAVE"


def _split_command(template: str) -> list[str]:
    """按 shell 习惯切分命令，但**不把反斜杠当转义**——Windows 路径里全是反斜杠。"""
    lexer = shlex.shlex(template, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    return list(lexer)


def _argv_for(template: str, src: Path, dst: Path) -> list[str]:
    """先切分再替换占位符：{in}/{out} 指向的路径含空格也不会被切坏。"""
    tokens = _split_command(template)
    if not any("{in}" in t or "{out}" in t for t in tokens):
        return [*tokens, str(src), str(dst)]
    return [t.replace("{in}", str(src)).replace("{out}", str(dst)) for t in tokens]


def _run(decoder: str, src: Path, dst: Path) -> None:
    argv = _argv_for(decoder, src, dst)
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=120, check=False)
    except FileNotFoundError as exc:
        raise DependencyError(f"解码器不可执行：{argv[0]}",
                              repair="检查 --decoder 路径或 PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise DependencyError(f"解码器超时：{argv[0]}",
                              repair="换一个解码器，或先手工把 silk 转成 wav") from exc
    if proc.returncode != 0 and not is_wav(dst):
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip()[:200]
        raise DependencyError(
            f"解码失败（exit {proc.returncode}）：{detail or '无输出'}",
            repair="确认解码器接受 `#!SILK_V3` 输入；必要时先手工转换",
            details={"decoder": argv[0]},
        )


def decode_file(src: str | Path, dst: str | Path, *, decoder: str | None = None) -> dict:
    """把一个 silk 文件转成 wav；返回 {"src","out","bytes"}。"""
    src_path, dst_path = Path(src), Path(dst)
    if not src_path.is_file():
        raise UsageError(f"silk 文件不存在：{src}")
    data = src_path.read_bytes()
    if not (data.startswith(b"\x02" + SILK_MAGIC) or data.startswith(SILK_MAGIC)):
        raise UsageError(f"不是 SILK v3：{src}", repair="只支持企微/微信语音缓存（#!SILK_V3）")
    template = _require(decoder)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="owc-silk-") as tmp:
        normalized = Path(tmp) / "voice.silk"
        normalized.write_bytes(normalize_header(data))
        _run(template, normalized, dst_path)
    if not is_wav(dst_path):
        try:
            dst_path.unlink()
        except OSError:
            pass
        raise DependencyError(
            "解码器没有产出合法 WAV",
            repair="确认解码器输出的是 wav（RIFF/WAVE），必要时手工转换后重试",
            details={"out": str(dst_path)},
        )
    return {"src": str(src_path), "out": str(dst_path), "bytes": dst_path.stat().st_size}


def iter_silk_files(root: str | Path) -> list[Path]:
    """目录下所有 silk 文件（按内容判断，不看扩展名）。"""
    base = Path(root)
    if base.is_file():
        return [base] if looks_like_silk(base) else []
    return [p for p in sorted(base.rglob("*")) if p.is_file() and looks_like_silk(p)]
