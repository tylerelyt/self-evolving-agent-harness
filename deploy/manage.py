"""Source-checkout entry for deployment; requires no tutorial imports."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from self_evolving_harness.deployment import main

if __name__ == "__main__":
    raise SystemExit(main(repo=ROOT))
