"""Compatibility entry; maintained source lives under apps/oncall."""
from pathlib import Path
import sys
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from apps.oncall.legacy import execute
if __name__ == "__main__" and "--solution" not in sys.argv and "--implementation" not in sys.argv and not any(arg.startswith("--implementation=") for arg in sys.argv):
    sys.argv.extend(["--implementation", str(Path(__file__).resolve().parent / "lab/self_improvement_agent.py")])
if __name__ == "__main__" and "--mode" not in sys.argv and not any(arg.startswith("--mode=") for arg in sys.argv) and "--tutorial" not in sys.argv:
    sys.argv.extend(["--mode", "tutorial"])
execute(globals(), 'app.py')
