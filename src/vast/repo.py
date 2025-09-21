"""Whitelisted repository helpers for VAST."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, List

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_ROOTS = {
    (REPO_ROOT / "migrations").resolve(),
    (REPO_ROOT / ".vast" / "generated").resolve(),
}


class RepoAccessError(Exception):
    pass


def _ensure_allowed(path: Path) -> Path:
    path = path.resolve()
    if not any(path == root or root in path.parents for root in ALLOWED_ROOTS):
        raise RepoAccessError(f"Path {path} not inside allowed directories")
    return path


def list_files(subdir: str | None = None) -> List[str]:
    roots: Iterable[Path]
    if subdir:
        target = _ensure_allowed((REPO_ROOT / subdir).resolve())
        roots = [target]
    else:
        roots = ALLOWED_ROOTS

    entries: List[str] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                entries.append(str(path.relative_to(REPO_ROOT)))
    return sorted(entries)


def read_file(path: str) -> str:
    target = _ensure_allowed((REPO_ROOT / path).resolve())
    if not target.exists():
        raise RepoAccessError(f"File {path} not found")
    return target.read_text()


def write_file(path: str, content: str, overwrite: bool = False) -> str:
    target = _ensure_allowed((REPO_ROOT / path).resolve())
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        raise RepoAccessError(f"File {path} already exists. Set overwrite=True to replace.")
    target.write_text(content)
    return str(target.relative_to(REPO_ROOT))


__all__ = ["RepoAccessError", "list_files", "read_file", "write_file"]
