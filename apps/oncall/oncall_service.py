"""Compatibility import for integrations using the former service name."""
from pathlib import Path
import signal
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from apps.oncall import service

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _signal, _frame: sys.exit(0))
    service.main()
else:
    sys.modules[__name__] = service
