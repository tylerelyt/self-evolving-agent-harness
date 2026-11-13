"""Tutorial compatibility entry for the maintained On-call implementation."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.oncall.workflow import (
    RoundTask, branch_report, learn, notify_operator, patrol_config,
    recall_memory, release_policy, route_alert, run, run_once,
)


if __name__ == "__main__":
    from fleet import main
    main()
