"""Settings schemas remain usable without importing plugin implementations."""

import subprocess
import sys


def test_core_settings_import_does_not_load_plugins() -> None:
    script = """
import sys
from elspeth.core.config import ElspethSettings

assert 'sources' in ElspethSettings.model_fields
assert not any(name == 'elspeth.plugins' or name.startswith('elspeth.plugins.') for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True, timeout=30)
