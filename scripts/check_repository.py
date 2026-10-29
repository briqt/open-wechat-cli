"""Check tracked/distributed paths; ignored local files are not an export manifest."""

from __future__ import annotations

import argparse
import subprocess
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

PRIVATE_ROOTS = {".doc", ".scratch", ".venv", "venv", "runtime", "snapshot", "snapshots", "decrypted", "export", "exports", "out", "keys"}
PRIVATE_NAMES = {"AGENTS.md", "CLAUDE.md", "BRIEF.md", ".config.json", "keys.json"}
GENERATED = {"__pycache__", ".pytest_cache", ".git", "build", "dist"}
SENSITIVE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".dmp", ".key", ".pem", ".keystore", ".jks", ".pyc")


def forbidden(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    return (
        any(p in PRIVATE_ROOTS | GENERATED or p.endswith(".egg-info") for p in parts)
        or (len(parts) == 1 and parts[0] in PRIVATE_NAMES)
        or any(p == ".env" or (p.startswith(".env.") and p != ".env.example") for p in parts)
        or path.endswith(SENSITIVE_SUFFIXES)
    )


def archive_paths(path: Path) -> list[str]:
    if path.suffix == ".whl" or path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    with tarfile.open(path) as archive:
        # Source distributions have a single package-version directory prefix.
        return ["/".join(PurePosixPath(n).parts[1:]) for n in archive.getnames()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives", nargs="*", type=Path, default=[])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
    paths = [p for p in result.stdout.decode().split("\0") if p]
    failures = [f"tracked: {p}" for p in paths if forbidden(p)]
    for path in args.archives:
        # setuptools source distributions include generated package metadata.
        names = [p for p in archive_paths(path) if not any(part.endswith(".egg-info") for part in PurePosixPath(p).parts)]
        failures.extend(f"{path.name}: {p}" for p in names if forbidden(p))
    if failures:
        print("\n".join(failures))
        return 1
    print(f"Boundary check passed: {len(paths)} tracked files, {len(args.archives)} archives.")
    print("This path check does not replace a review of staged file contents.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
