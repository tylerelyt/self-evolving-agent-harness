"""Personal Gateway and governed task entry from any working directory."""

import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from self_evolving_harness.personal import PersonalHarness, main as governed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Personal Hermes with separate learning and governed execution")
    parser.add_argument("mode", choices=("gateway", "setup", "doctor", "govern"))
    parser.add_argument("--state", type=Path, default=os.environ.get("RSI_STATE_DIR"))
    args, remaining = parser.parse_known_args(argv)
    if args.mode == "doctor":
        os.execv(sys.executable, [sys.executable, "-m", "hermes_cli.main", "--version"])
    if not args.state:
        parser.error("Provide --state or RSI_STATE_DIR")
    if args.mode == "govern":
        return governed(["--state", str(args.state), *remaining])
    workspace = PersonalHarness(args.state)
    env = {**os.environ, "HERMES_HOME": str(workspace.learner_home)}
    if args.mode == "setup":
        command = ["setup", *remaining]
    else:
        command = ["gateway", "run", "--external-supervisor", *remaining]
    # Replace, rather than detach from, the supervisor-owned foreground group.
    os.execvpe(sys.executable, [sys.executable, "-m", "hermes_cli.main", *command], env)


if __name__ == "__main__":
    main()
