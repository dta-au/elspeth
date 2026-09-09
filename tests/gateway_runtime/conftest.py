import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
for entry in (_REPO_ROOT / "gateway" / "src", _REPO_ROOT / "gateway"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
