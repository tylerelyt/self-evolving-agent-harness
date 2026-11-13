"""Run the maintained workload from an isolated deployment working directory."""

from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from apps.oncall.__main__ import main

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _signal, _frame: sys.exit(0))
    main()
