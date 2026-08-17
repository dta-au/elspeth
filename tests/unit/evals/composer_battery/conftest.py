import sys
from pathlib import Path

_BATTERY_DIR = Path(__file__).resolve().parents[4] / "evals" / "composer-battery"
if str(_BATTERY_DIR) not in sys.path:
    sys.path.insert(0, str(_BATTERY_DIR))
