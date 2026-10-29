"""The repository boundary check needs both positive and negative fixtures."""

from __future__ import annotations

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("boundary_check", Path(__file__).resolve().parents[1] / "scripts" / "check_repository.py")
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


def test_project_files_are_allowed():
    for path in ("README.md", "pyproject.toml", "LICENSE", "src/example.py", "tests/test_codec.py", ".github/workflows/ci.yml", ".env.example", "skills/open-wechat-cli/SKILL.md"):
        assert not CHECK.forbidden(path), path


def test_private_data_and_generated_files_are_excluded():
    for path in (".doc/AGENTS.md", "AGENTS.md", "CLAUDE.md", ".scratch/probe.py", "runtime/export.jsonl", "keys/account.json", "message.db", "data/message.db-wal", ".env", ".env.local", "src/package.egg-info/PKG-INFO", "tests/__pycache__/cache.pyc"):
        assert CHECK.forbidden(path), path
