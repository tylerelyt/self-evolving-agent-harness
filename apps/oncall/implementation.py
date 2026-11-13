"""Load the same On-call implementation in checks and runtime processes."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


CAPSTONE = Path(__file__).resolve().parent
ROOT = CAPSTONE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
REFERENCE = CAPSTONE / "workflow.py"


def load_implementation(path):
    path = Path(path).resolve()
    if path == REFERENCE:
        from apps.oncall import workflow
        return workflow
    spec = spec_from_file_location("oncall_service_implementation", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Cannot load the On-call implementation from {path}")
    if str(CAPSTONE) not in sys.path:
        sys.path.insert(0, str(CAPSTONE))
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
