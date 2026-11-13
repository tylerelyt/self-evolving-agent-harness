"""Compatibility entry; maintained source lives under apps/oncall."""
from pathlib import Path
import sys
_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from apps.oncall.legacy import execute
execute(globals(), 'patrol/enterprise_cases.py')
