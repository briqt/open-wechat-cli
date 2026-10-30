"""SILK→WAV 适配层测试：用假解码器验证整条链路，不依赖真实 SILK 实现。"""

from __future__ import annotations

import struct
import sys

import pytest

from open_wechat_cli import silk
from open_wechat_cli.errors import DependencyError, UsageError

SILK_BYTES = b"\x02#!SILK_V3" + b"\x10\x00" + b"\x01\x02\x03\x04" * 8
WAV_HEADER = b"RIFF" + struct.pack("<I", 36) + b"WAVEfmt " + b"\x10\x00\x00\x00" + b"\x01\x00\x01\x00"


def _fake_decoder(tmp_path, *, make_wav: bool = True, seen_file: str | None = None):
    """写一个假解码器：读入 silk、校验头、写出最小 wav。"""
    script = tmp_path / "fake_decoder.py"
    body = [
        "import pathlib, sys",
        f"seen = pathlib.Path({str(seen_file)!r}) if {bool(seen_file)!r} else None",
        "src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])",
        "data = src.read_bytes()",
        "if seen is not None:",
        "    seen.write_text(data[:10].decode('latin-1'))",
        "if not data.startswith(b'#!SILK_V3'):",
        "    sys.exit(3)",
    ]
    if make_wav:
        body.append("dst.write_bytes(b'RIFF' + b'\\x24\\x00\\x00\\x00' + b'WAVEfmt ')")
    else:
        body.append("dst.write_bytes(b'not a wav')")
    script.write_text("\n".join(body), encoding="utf-8")
    return f'"{sys.executable}" "{script}" {{in}} {{out}}'


def test_decode_normalizes_header_and_validates_wav(tmp_path):
    src = tmp_path / "voice"
    src.write_bytes(SILK_BYTES)
    seen = tmp_path / "seen.txt"
    info = silk.decode_file(src, tmp_path / "out.wav",
                            decoder=_fake_decoder(tmp_path, seen_file=str(seen)))
    assert info["bytes"] == (tmp_path / "out.wav").stat().st_size
    assert silk.is_wav(tmp_path / "out.wav")
    # 解码器拿到的是去掉 0x02 前缀的 `#!SILK_V3`
    assert seen.read_text().startswith("#!SILK_V3")


def test_decode_rejects_non_silk(tmp_path):
    src = tmp_path / "x.amr"
    src.write_bytes(b"#!AMR\n" + b"0" * 20)
    with pytest.raises(UsageError):
        silk.decode_file(src, tmp_path / "out.wav", decoder=_fake_decoder(tmp_path))


def test_decode_without_decoder_is_structured_error(tmp_path, monkeypatch):
    src = tmp_path / "voice"
    src.write_bytes(SILK_BYTES)
    monkeypatch.delenv(silk.ENV_KEY, raising=False)
    monkeypatch.setattr(silk, "CANDIDATES", ())
    with pytest.raises(DependencyError) as exc:
        silk.decode_file(src, tmp_path / "out.wav")
    assert "SILK" in exc.value.message and exc.value.repair


def test_decoder_garbage_output_is_rejected_and_not_left_behind(tmp_path):
    src = tmp_path / "voice"
    src.write_bytes(SILK_BYTES)
    target = tmp_path / "out.wav"
    with pytest.raises(DependencyError):
        silk.decode_file(src, target, decoder=_fake_decoder(tmp_path, make_wav=False))
    assert not target.exists()


def test_iter_silk_files_filters_by_content(tmp_path):
    (tmp_path / "a").write_bytes(SILK_BYTES)
    (tmp_path / "b").write_bytes(b"\x02#!SILK_V3" + b"x" * 8)
    (tmp_path / "c.wav").write_bytes(b"RIFF....WAVE")
    names = sorted(p.name for p in silk.iter_silk_files(tmp_path))
    assert names == ["a", "b"]
    assert silk.iter_silk_files(tmp_path / "c.wav") == []


def test_env_decoder_template_is_used(tmp_path, monkeypatch):
    src = tmp_path / "voice"
    src.write_bytes(SILK_BYTES)
    monkeypatch.setenv(silk.ENV_KEY, _fake_decoder(tmp_path))
    info = silk.decode_file(src, tmp_path / "env.wav")
    assert silk.is_wav(info["out"])
