"""Violation fixture: class-level __getattr__ forwarding to a wrapped object.

Class-level ``__getattr__`` (AST parent is ``ClassDef``, not ``Module``)
never gets the PEP 562 module-getattr amnesty, regardless of shape — an
instance can present arbitrary attribute names to satisfy whatever
contract the caller expects. This is the exact hazard PLAN.md Correction 1
distinguishes from ``PipelineRow.__getattr__`` (which delegates via
subscript to the instance's own data, not to a foreign object).
"""

from __future__ import annotations


class Wrapper:
    def __init__(self, wrapped: object) -> None:
        self._wrapped = wrapped

    def __getattr__(self, name: str) -> object:
        return self._wrapped.__dict__[name]
