"""Publish rendered voice files without shared temporary names or partial failures."""

from __future__ import annotations

import contextlib
import fcntl
import os
import tempfile
from pathlib import Path


def _stage(path: Path, content: bytes, mode: int) -> Path:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as tmp:
        staged = Path(tmp.name)
        try:
            os.fchmod(tmp.fileno(), mode)
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
    return staged


def write_files(files: dict[str, str], root: str) -> None:
    """Serialise writers, stage the entire batch, and restore it if publishing fails.

    Each rename is atomic. Consumers still need to reload after the whole batch;
    this does not make separate file reads a single filesystem transaction.
    """
    base = Path(root)
    for rel in files:
        path = Path(rel)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Rendered voice files must stay inside their output directory.")
    base.mkdir(parents=True, exist_ok=True)
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path | None] = {}
    changed: list[Path] = []
    # The same lock is used by all writers of this output tree, including
    # workers in separate controller processes.
    with (base / ".render.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            for rel, content in files.items():
                path = base / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
                staged[path] = _stage(path, content.encode("utf-8"), mode)
                backups[path] = _stage(path, path.read_bytes(), mode) if path.exists() else None
            try:
                for path, tmp in staged.items():
                    os.replace(tmp, path)
                    changed.append(path)
            except BaseException:
                for path in reversed(changed):
                    backup = backups[path]
                    if backup is None:
                        path.unlink(missing_ok=True)
                    else:
                        os.replace(backup, path)
                raise
        finally:
            for tmp in [*staged.values(), *backups.values()]:
                if tmp is not None:
                    with contextlib.suppress(OSError):
                        tmp.unlink(missing_ok=True)
