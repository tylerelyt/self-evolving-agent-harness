"""Immutable, content-addressed capability snapshots."""

from hashlib import sha256
import os
from pathlib import Path
import re
import tempfile


def snapshot_skill(root: Path, content: bytes, name: str) -> tuple[Path, str]:
    """Store exact Skill bytes using the existing version-directory contract.

    A hard-link publication prevents an interrupted/concurrent writer from
    replacing an already visible version. Existing bytes must match the digest.
    This single-file adapter preserves legacy evidence paths; complete bundles
    are a separate roadmap item.
    """
    if not isinstance(content, bytes):
        raise TypeError("Skill content must be bytes")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError("Skill name must be one safe path component")
    digest = sha256(content).hexdigest()
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = root / "versions" / digest
    directory = source / name
    current = root
    for part in ("versions", digest, name):
        current = current / part
        if current.is_symlink():
            raise ValueError("Version directories cannot be symbolic links")
        current.mkdir(exist_ok=True)
    entry = directory / "SKILL.md"
    if entry.is_symlink():
        raise ValueError("A version entry cannot be a symbolic link")
    descriptor, temporary = tempfile.mkstemp(prefix=".skill-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, entry)
        except FileExistsError:
            pass
        if entry.is_symlink() or entry.read_bytes() != content:
            raise ValueError("Stored Skill bytes do not match their content digest")
    finally:
        Path(temporary).unlink(missing_ok=True)
    return source, digest
