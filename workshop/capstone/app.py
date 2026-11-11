"""Open the On-call workbench with a prepared service environment."""

import argparse
from pathlib import Path
import signal
import sys
import threading

from workbench import Workbench, make_server
from workbench_config import load_model_environment


HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=HERE / "runtime" / "workbench")
    parser.add_argument("--port", type=int, default=8765)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", dest="live", action="store_true", help="Use the configured model (default)")
    mode.add_argument("--offline", dest="live", action="store_false", help="Explicitly use a scripted wiring demonstration")
    parser.set_defaults(live=True)
    parser.add_argument("--env-file", type=Path, help="Read GLM settings from this local file instead of the repo .env")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--solution", action="store_true", help="Open the complete reference implementation")
    selection.add_argument("--implementation", type=Path, help="Use this implementation in the workbench and checks")
    args = parser.parse_args()
    implementation = args.implementation or (HERE / "self_improvement_agent.py" if args.solution
                                              else HERE / "lab" / "self_improvement_agent.py")
    if not implementation.is_file():
        parser.error(f"Implementation file does not exist: {implementation}")
    if not 0 <= args.port <= 65535:
        parser.error("Use a port between 0 and 65535")
    configuration = load_model_environment(args.env_file) if args.live else {"configured": False}
    with Workbench(args.state, implementation, live=args.live) as workbench:
        server = make_server(workbench, "127.0.0.1", args.port, HERE / "web" / "index.html")
        def stop(_signal, _frame):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        print(f"On-call workbench: http://127.0.0.1:{server.server_port}", flush=True)
        print(f"Agent implementation: {implementation}", flush=True)
        print(f"Mode: {'live model' if args.live else 'local scripted model'}", flush=True)
        if args.live and not configuration["configured"]:
            print("Model configuration missing: set GLM_API_KEY in the repo .env to bring the Agent online.", flush=True)
        try:
            server.serve_forever(poll_interval=.2)
        finally:
            server.server_close()


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        sys.exit(str(exc))
