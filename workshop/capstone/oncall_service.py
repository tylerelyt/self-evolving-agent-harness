"""Compatibility alias for the maintained continuous On-call service."""

from pathlib import Path
import signal
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from apps.oncall import service as _service

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda _signal, _frame: sys.exit(0))
    _service.main()
else:
    # Preserve module identity: existing tests and integrations patch this
    # object, including the globals used by its functions.
    sys.modules[__name__] = _service
