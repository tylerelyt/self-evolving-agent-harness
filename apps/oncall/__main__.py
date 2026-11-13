"""Stable application entry; legacy commands remain supported."""

import argparse
import signal
import sys

from .paths import prepare


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the maintained On-call application")
    parser.add_argument("mode", choices=("serve", "demo", "workbench"),
                        help="Continuous service, finite verification run, or interactive workbench")
    args, remainder = parser.parse_known_args(argv)
    prepare()
    if args.mode != "workbench" and "--live" in remainder:
        from workbench_config import load_model_environment
        load_model_environment()
    if args.mode == "workbench":
        from app import main as run
        previous = sys.argv
        try:
            sys.argv = ["oncall-workbench", "--solution", *remainder]
            run()
        finally:
            sys.argv = previous
    elif args.mode == "serve":
        from .service import main as run
        run(remainder)
    else:
        from fleet import main as run
        run(remainder)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _signal, _frame: sys.exit(0))
    main()
