"""Keep a real Hermes patrol running with the workshop implementation."""

import argparse
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "patrol"))

import patrol
import self_improvement_agent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True, help="Running HTTP lab directory")
    args = parser.parse_args()
    patrol.main(["--state", str(args.state), "run", "--oncall", "--page-oncall", "--offline",
                 "--service", "payments", "--sample", "1", "--interval", "3"], oncall_steps=self_improvement_agent)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Patrol stopped", file=sys.stderr)
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        sys.exit(str(exc))
