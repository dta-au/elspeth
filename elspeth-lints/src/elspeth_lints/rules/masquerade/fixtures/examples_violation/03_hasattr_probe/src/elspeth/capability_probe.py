"""Violation fixture: hasattr used as an internal-contract capability probe.

Not in the ``tests`` root and not the direct operand of an ``assert``, so
the presence-as-subject amnesty does not apply. An object that merely
defines ``run_batch`` would pass this check regardless of whether it
actually satisfies the plugin contract.
"""

from __future__ import annotations


def supports_batch(plugin: object) -> bool:
    return hasattr(plugin, "run_batch")
