"""Content-addressed complete Skill bundles, including executable assets.

The manifest is the coordinator artifact. Every operation also validates all
files, so a matching SKILL.md alone cannot hide modified scripts/references.
"""

from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from .learning import canonical


def safe_name(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError("A Skill name must be a safe directory component")
    return name


def describe(source, name):
    source = Path(source)
    safe_name(name)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("A Skill source must be a real directory")
    entries = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError("Skill bundles cannot contain symlinks")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("Skill bundles contain regular files only")
        relative = path.relative_to(source).as_posix()
        if relative == "manifest.json":
            raise ValueError("manifest.json is reserved for bundle identity")
        entries.append({"path": relative, "sha256": sha256(path.read_bytes()).hexdigest(),
                        "executable": bool(path.stat().st_mode & 0o111)})
    if not any(x["path"] == "SKILL.md" for x in entries):
        raise ValueError("A Skill bundle needs SKILL.md")
    return {"schema_version": 1, "kind": "skill-bundle", "name": name, "files": entries}


def verify_bundle(directory):
    directory = Path(directory)
    manifest = directory / "manifest.json"
    if directory.is_symlink() or manifest.is_symlink():
        raise ValueError("Skill bundle identities cannot be symlinks")
    data = manifest.read_bytes()
    spec = json.loads(data)
    actual = describe(directory / "content", spec["name"])
    if actual != spec or data != canonical(actual).encode():
        raise ValueError("Skill bundle content changed")
    identity = sha256(data).hexdigest()
    if directory.name != identity:
        raise ValueError("Skill bundle identity changed")
    return identity, spec


def snapshot_bundle(store, source, name):
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    spec = describe(source, name)
    data = canonical(spec).encode()
    identity = sha256(data).hexdigest()
    target = store / identity
    if target.exists():
        verify_bundle(target)
        return target
    temporary = Path(tempfile.mkdtemp(prefix=".bundle-", dir=store))
    try:
        shutil.copytree(source, temporary / "content", symlinks=True)
        if describe(temporary / "content", name) != spec:
            raise ValueError("Skill source changed while snapshotting")
        (temporary / "manifest.json").write_bytes(data)
        # rename is atomic; callers serialize assignment changes separately.
        try:
            os.rename(temporary, target)
        except OSError:
            if not target.is_dir():
                raise
        verify_bundle(target)
        return target
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
