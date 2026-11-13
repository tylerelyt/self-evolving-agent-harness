#!/usr/bin/env python3
"""Run native Curator lifecycle transitions on copies in a fresh teaching Home."""

import argparse
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from shutil import copytree
import subprocess
import sys
from tempfile import TemporaryDirectory

from hermes_runtime import ONCALL_SKILL, ROOT, SOURCE


CANDIDATE = "oncall-recovery-window-check"
RETIRED = "retired-oncall-recovery-draft"
RETIRED_CONTENT = (
    f"---\nname: {RETIRED}\ndescription: A retired teaching draft.\n---\n\n"
    "# Retired draft\n\nThis superseded draft only checked one recovery window.\n"
).encode("utf-8")
CURATOR_CONFIG = {
    "curator": {"enabled": True, "consolidate": False, "prune_builtins": False,
                "stale_after_days": 30, "archive_after_days": 90},
    "skills": {"external_dirs": []},
}


def pinned_revision():
    expected = json.loads((Path(__file__).resolve().parents[1] / "integrations/versions.json").read_text())["hermes_agent"]
    actual = subprocess.check_output(["git", "-C", str(SOURCE), "rev-parse", "HEAD"], text=True).strip()
    changes = subprocess.check_output(
        ["git", "--no-optional-locks", "-C", str(SOURCE), "status", "--porcelain", "--untracked-files=normal"],
        text=True,
    )
    if actual != expected or changes:
        raise RuntimeError("Curator requires the course's pinned, clean Hermes dependency")
    return actual


def restore_in_copy(state, archived, expected):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from tools import skill_usage

    # Native restore moves the archive back to an active directory. Exercise it
    # on a separate copy so the retired draft never re-enters the staging library.
    with TemporaryDirectory(prefix="curator-restore-", dir=state) as isolated:
        home = Path(isolated)
        (home / "config.yaml").write_text(json.dumps(CURATOR_CONFIG), encoding="utf-8")
        copytree(archived.parent, home / "skills" / ".archive" / RETIRED)
        token = set_hermes_home_override(home)
        try:
            restored, message = skill_usage.restore_skill(RETIRED)
            if not restored:
                raise RuntimeError(f"Native Curator could not restore the retired draft: {message}")
            if ((home / "skills" / RETIRED / "SKILL.md").read_bytes() != expected
                    or RETIRED in skill_usage.list_archived_skill_names()):
                raise RuntimeError("Native restore did not recover the archived draft bytes")
        finally:
            reset_hermes_home_override(token)


def curate(state, candidate, formal):
    revision = pinned_revision()
    sources = {CANDIDATE: candidate, ONCALL_SKILL: formal}
    contents = {name: source.read_bytes() for name, source in sources.items()}
    home = state / "hermes"
    home.mkdir(parents=True, exist_ok=False)
    (home / "config.yaml").write_text(json.dumps(CURATOR_CONFIG), encoding="utf-8")
    os.environ["HERMES_HOME"] = str(home)
    sys.path.insert(0, str(SOURCE))
    import hermes_cli.env_loader
    hermes_cli.env_loader.load_hermes_dotenv = lambda **_kwargs: []
    from agent import curator
    from agent.skill_utils import parse_frontmatter
    from tools import skill_usage

    skills = home / "skills"
    staged = {}
    for name, content in {**contents, RETIRED: RETIRED_CONTENT}.items():
        metadata, body = parse_frontmatter(content.decode("utf-8"))
        if metadata.get("name") != name or not body.strip():
            raise ValueError(f"Expected a nonempty Skill with name {name}")
        path = skills / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        managed, message = skill_usage.adopt_skill(name)
        if not managed:
            raise RuntimeError(f"Native Curator could not manage {name}: {message}")
        staged[name] = path

    skill_usage.set_pinned(ONCALL_SKILL, True)
    now = datetime.now(timezone.utc)
    age_days = 120
    aged_at = (now - timedelta(days=age_days)).isoformat()
    # Age the retired draft and the pinned copy equally so the pin is exercised.
    # This is a constructed clock in this Home, never a source history edit.
    with skill_usage._usage_file_lock():
        usage = skill_usage.load_usage()
        if usage[ONCALL_SKILL]["pinned"] is not True:
            raise RuntimeError("Native Curator did not persist the formal Skill's pin")
        for name in (RETIRED, ONCALL_SKILL):
            usage[name]["created_at"] = aged_at
        if not skill_usage.save_usage(usage):
            raise RuntimeError("Could not persist the constructed lifecycle fixture")

    transitions = curator.apply_automatic_transitions(now=now)
    records = {name: skill_usage.get_record(name) for name in staged}
    archived = skills / ".archive" / RETIRED / "SKILL.md"
    listed = skill_usage.list_archived_skill_names()
    if (transitions["checked"] != len(staged) or transitions["archived"] != 1 or listed != [RETIRED]
            or records[RETIRED]["state"] != "archived" or staged[RETIRED].exists()
            or archived.read_bytes() != RETIRED_CONTENT):
        raise RuntimeError("Native Curator did not archive exactly the retired draft")
    restore_in_copy(state, archived, RETIRED_CONTENT)
    if archived.read_bytes() != RETIRED_CONTENT or staged[RETIRED].exists():
        raise RuntimeError("The restore exercise changed the staging library")
    if (records[CANDIDATE]["state"] != "active" or records[ONCALL_SKILL]["state"] != "active"
            or records[ONCALL_SKILL]["pinned"] is not True
            or any(staged[name].read_bytes() != content or sources[name].read_bytes() != content
                   for name, content in contents.items())):
        raise RuntimeError("Curator changed the candidate or the protected formal Skill")

    return {
        "status": "curated", "mode": "native-automatic-lifecycle", "hermes_revision": revision,
        "candidate": staged[CANDIDATE].relative_to(state).as_posix(),
        "candidate_sha256": sha256(contents[CANDIDATE]).hexdigest(),
        "formal_sha256": sha256(contents[ONCALL_SKILL]).hexdigest(),
        "native_transitions": transitions, "constructed_age_days": age_days,
        "retired_draft": {"name": RETIRED, "state": records[RETIRED]["state"],
                          "archive": archived.relative_to(state).as_posix(),
                          "sha256": sha256(RETIRED_CONTENT).hexdigest(),
                          "listed_in_native_archive": True, "recovery_verified": True},
        "formal_protection": {"pinned": True, "state": records[ONCALL_SKILL]["state"],
                              "source_unchanged": True, "copy_unchanged": True},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("--state", "--candidate", "--formal-skill", "--out"):
        parser.add_argument(option, type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() or args.out.is_symlink():
        parser.error("--out must be a new file")
    result = curate(args.state.resolve(), args.candidate.resolve(), args.formal_skill.resolve())
    with args.out.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
        output.write("\n")
    print(args.out)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, ImportError, subprocess.CalledProcessError) as exc:
        sys.exit(f"Recovery Skill Curator failed: {exc}")
