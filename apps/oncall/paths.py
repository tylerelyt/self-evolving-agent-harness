"""Locate maintained On-call runtime modules."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
APP = Path(__file__).resolve().parent
CAPSTONE = APP  # Compatibility name for previously recorded integrations.


def prepare():
    for directory in (ROOT / "src", APP, APP / "patrol"):
        value = str(directory)
        if value in sys.path:
            sys.path.remove(value)
        sys.path.insert(0, value)
