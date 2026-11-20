"""Pipeline YAML text that remembers the document it was rendered from.

Web preflight renders a configuration to YAML text and later loads that text
through :func:`~elspeth.core.config.load_bounded_pipeline_yaml`, which applies
the in-memory limits (byte size, node count, nesting depth) and refuses aliases
and non-safe tags. Parsing back a document that is still in hand costs as much
as rendering it, so :meth:`RenderedPipelineYaml.load` answers from a frozen
copy of the document whenever that answer is provably the one the bounded
loader would give, and otherwise runs the bounded loader on the text.

The copy is taken only for plain data: exact ``dict`` with exact ``str`` keys,
exact ``list``, ``str``, ``int``, ``bool``, ``None`` and non-NaN ``float``,
with no container reachable twice. ``yaml.dump`` renders such data without
tags or anchors and the safe loader reads it back equal, with every mapping's
keys in sorted order (``yaml.dump`` sorts them), which is the order the copy
stores. Anything else, and any document whose node tree is over a limit,
keeps no copy; a text over the byte limit is not answered from the copy.
Either way loading parses the text, so every refusal comes from the bounded
loader itself, with its own type and message.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType

import yaml

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.config import (
    MAX_IN_MEMORY_PIPELINE_YAML_BYTES,
    MAX_IN_MEMORY_PIPELINE_YAML_DEPTH,
    MAX_IN_MEMORY_PIPELINE_YAML_NODES,
    load_bounded_pipeline_yaml,
)

_PlainScalar = str | int | float | bool | None
_FrozenNode = _PlainScalar | tuple["_FrozenNode", ...] | MappingProxyType[str, "_FrozenNode"]


class _NotPlain(Exception):
    """The document cannot be answered without parsing its text."""


class _PlainSnapshot:
    """One walk that copies plain data in the order a reload would produce it.

    Counts nodes and depth exactly as ``_BoundedInMemoryYamlLoader`` does for
    the rendered text: one node per collection, per scalar and per mapping key,
    the root at depth 1. It stops as soon as a limit would be exceeded.
    """

    __slots__ = ("_nodes", "_seen")

    def __init__(self) -> None:
        self._nodes = 0
        self._seen: set[int] = set()

    def _count(self, depth: int) -> None:
        self._nodes += 1
        if self._nodes > MAX_IN_MEMORY_PIPELINE_YAML_NODES or depth > MAX_IN_MEMORY_PIPELINE_YAML_DEPTH:
            raise _NotPlain

    def copy(self, value: object, depth: int) -> _FrozenNode:
        self._count(depth)
        if value is None or type(value) is str or type(value) is int or type(value) is bool:
            return value
        if type(value) is float:
            if math.isnan(value):
                raise _NotPlain
            return value
        if type(value) is list:
            self._first_visit(value)
            return tuple(self.copy(item, depth + 1) for item in value)
        if type(value) is dict:
            self._first_visit(value)
            copied: dict[str, _FrozenNode] = {}
            for key in sorted(value, key=_str_key):
                self._count(depth + 1)
                copied[key] = self.copy(value[key], depth + 1)
            return MappingProxyType(copied)
        raise _NotPlain

    def _first_visit(self, container: object) -> None:
        # A container reachable twice renders as an anchor and alias, which
        # the bounded loader refuses.
        if id(container) in self._seen:
            raise _NotPlain
        self._seen.add(id(container))


def _str_key(key: object) -> str:
    if type(key) is not str:
        raise _NotPlain
    return key


@dataclass(frozen=True, slots=True)
class RenderedPipelineYaml:
    """Pipeline YAML text, plus the document it was rendered from when that is plain.

    Build it with :meth:`render` (from a document) or :meth:`of_text` (text
    whose document is not in hand). ``load()`` returns exactly what
    ``load_bounded_pipeline_yaml(text)`` returns, or raises what it raises.
    """

    text: str
    _document: _FrozenNode | None

    @classmethod
    def of_text(cls, text: str) -> RenderedPipelineYaml:
        """Wrap YAML text with no document; ``load()`` parses it."""
        return cls(text=text, _document=None)

    @classmethod
    def render(cls, document: object) -> RenderedPipelineYaml:
        """Render ``document`` as ``yaml.dump(document, default_flow_style=False)`` does."""
        text = yaml.dump(document, default_flow_style=False)
        try:
            snapshot = _PlainSnapshot().copy(document, 1)
        except _NotPlain:
            return cls(text=text, _document=None)
        return cls(text=text, _document=snapshot)

    def load(self) -> object:
        """Return the bounded load of ``text``: a fresh copy of the document, or a parse.

        The byte limit is judged on the text here, at load time, exactly where
        the bounded loader would judge it; over the limit, the loader runs and
        refuses.
        """
        if self._document is None or len(self.text.encode("utf-8")) > MAX_IN_MEMORY_PIPELINE_YAML_BYTES:
            return load_bounded_pipeline_yaml(self.text)
        return deep_thaw(self._document)
