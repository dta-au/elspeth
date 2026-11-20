"""The pipeline configuration web preflight loads its runtime settings from.

It has one of two origins.

* :meth:`RenderedPipelineYaml.of_text` wraps YAML text whose document is not
  in hand: the authored YAML when nothing needed resolving. ``load()`` parses
  it with :func:`~elspeth.core.config.load_bounded_pipeline_yaml`, under every
  in-memory limit, including the 2 MiB text limit for supplied YAML.
* :meth:`RenderedPipelineYaml.of_resolved` holds the configuration after
  file-held text was substituted into it. Its size is judged as data, before
  anything is rendered: the UTF-8 bytes of its text (every string key and
  value, and any ``bytes`` value) must not exceed
  :data:`MAX_RESOLVED_PIPELINE_TEXT_BYTES`. Judging the rendered YAML instead
  made the limit depend on the language of the prompts, because ``yaml.dump``
  escapes non-ASCII text (measured: 1.2 rendered bytes per text byte for
  English, 2.3 for Chinese, 3.3 for Cyrillic).

  A resolved document that is plain data -- exact ``dict`` with exact ``str``
  keys, exact ``list``, ``str``, ``int``, ``bool``, ``None`` and non-NaN
  ``float``, no container reachable twice, within the node and depth limits --
  is kept as a frozen copy with every mapping's keys in sorted order.
  ``yaml.dump`` renders such data without tags or anchors and the safe loader
  reads it back equal and key-sorted, so ``load()`` returns a fresh thaw of the
  copy: the document is never rendered and parsed back. Any other document is
  rendered at once and parsed at load by
  :func:`~elspeth.core.config.load_rendered_pipeline_yaml`, so its refusals
  (aliases, node count, depth, unsafe tags) are the bounded loader's own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

import yaml

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.blobs_inline import BLOB_INLINE_AGGREGATE_BYTE_CAP
from elspeth.core.config import (
    MAX_IN_MEMORY_PIPELINE_YAML_DEPTH,
    MAX_IN_MEMORY_PIPELINE_YAML_NODES,
    RenderedPipelineYamlText,
    load_bounded_pipeline_yaml,
    load_rendered_pipeline_yaml,
    render_pipeline_yaml,
)

# The resolved configuration is the authored configuration (whose text the
# 2 MiB supplied-YAML limit already bounds) plus the file-held text
# substituted into it (which BLOB_INLINE_AGGREGATE_BYTE_CAP bounds). Four
# times the file-text cap holds both with room to spare for any cap of at
# least 2/3 MiB, and moves with the cap when the cap is ruled to change.
MAX_RESOLVED_PIPELINE_TEXT_BYTES: Final = 4 * BLOB_INLINE_AGGREGATE_BYTE_CAP

_PlainScalar = str | int | float | bool | None
_FrozenNode = _PlainScalar | tuple["_FrozenNode", ...] | MappingProxyType[str, "_FrozenNode"]


class ResolvedPipelineTooLargeError(Exception):
    """The resolved configuration holds more text than the resolved-configuration bound."""

    def __init__(self, total_bytes: int, limit_bytes: int) -> None:
        super().__init__(f"resolved pipeline configuration holds {total_bytes} bytes of text; the limit is {limit_bytes} bytes")
        self.total_bytes = total_bytes
        self.limit_bytes = limit_bytes


def resolved_text_bytes(document: object) -> int:
    """UTF-8 bytes of every string key and string value in ``document``, plus every ``bytes`` value.

    Walks ``dict``, ``list``, ``tuple``, ``set`` and ``frozenset`` (each
    container once). Every other value is a scalar whose rendering the node
    limit bounds, or a type the safe loader refuses when the document is
    loaded.
    """
    total = 0
    seen: set[int] = set()
    stack: list[object] = [document]
    while stack:
        value = stack.pop()
        if type(value) is str:
            # surrogatepass: a lone surrogate (which YAML's "\\uD800" escape
            # can produce) counts as the three bytes it would encode to.
            total += len(value.encode("utf-8", "surrogatepass"))
        elif type(value) is bytes:
            total += len(value)
        elif id(value) in seen:
            continue
        elif type(value) is dict:
            seen.add(id(value))
            stack.extend(value.keys())
            stack.extend(value.values())
        elif type(value) is list or type(value) is tuple or type(value) is set or type(value) is frozenset:
            seen.add(id(value))
            stack.extend(value)
    return total


class _NotPlain(Exception):
    """The document must be rendered and parsed to be loaded."""


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


def _plain_mentions(node: _FrozenNode, needle: str) -> bool:
    stack: list[_FrozenNode] = [node]
    while stack:
        value = stack.pop()
        if type(value) is str:
            if needle in value:
                return True
        elif type(value) is tuple:
            stack.extend(value)
        elif type(value) is MappingProxyType:
            for key, item in value.items():
                if needle in key:
                    return True
                stack.append(item)
    return False


@dataclass(frozen=True, slots=True)
class _SuppliedText:
    text: str

    def load(self) -> object:
        return load_bounded_pipeline_yaml(self.text)

    def mentions(self, needle: str) -> bool:
        return needle in self.text


@dataclass(frozen=True, slots=True)
class _RenderedText:
    rendered: RenderedPipelineYamlText

    @property
    def text(self) -> str:
        return self.rendered

    def load(self) -> object:
        return load_rendered_pipeline_yaml(self.rendered)

    def mentions(self, needle: str) -> bool:
        return needle in self.rendered


@dataclass(frozen=True, slots=True)
class _PlainDocument:
    document: _FrozenNode

    @property
    def text(self) -> str:
        return yaml.dump(deep_thaw(self.document), default_flow_style=False)

    def load(self) -> object:
        return deep_thaw(self.document)

    def mentions(self, needle: str) -> bool:
        # Rendering can split a word only where it folds an escaped string
        # onto a new line, and never joins two, so reading the strings sees
        # every occurrence the rendered text would show.
        return _plain_mentions(self.document, needle)


@dataclass(frozen=True, slots=True)
class RenderedPipelineYaml:
    """Pipeline YAML for the settings phase: supplied text, or a resolved configuration."""

    _source: _SuppliedText | _RenderedText | _PlainDocument

    @classmethod
    def of_text(cls, text: str) -> RenderedPipelineYaml:
        """Wrap supplied YAML text; ``load()`` parses it under every in-memory limit."""
        return cls(_SuppliedText(text))

    @classmethod
    def of_resolved(cls, document: object) -> RenderedPipelineYaml:
        """Hold a resolved configuration, judging its size as data first.

        Raises:
            ResolvedPipelineTooLargeError: its text exceeds
                :data:`MAX_RESOLVED_PIPELINE_TEXT_BYTES`.
        """
        total = resolved_text_bytes(document)
        if total > MAX_RESOLVED_PIPELINE_TEXT_BYTES:
            raise ResolvedPipelineTooLargeError(total, MAX_RESOLVED_PIPELINE_TEXT_BYTES)
        try:
            snapshot = _PlainSnapshot().copy(document, 1)
        except _NotPlain:
            return cls(_RenderedText(render_pipeline_yaml(document)))
        return cls(_PlainDocument(snapshot))

    @property
    def text(self) -> str:
        """The YAML text: as supplied, or ``yaml.dump(document, default_flow_style=False)``.

        A plain resolved configuration renders it on each request; nothing in
        the preflight asks.
        """
        return self._source.text

    def load(self) -> object:
        """Return a fresh document tree, or raise what the loader for this origin raises."""
        return self._source.load()

    def mentions(self, needle: str) -> bool:
        """Whether ``needle`` occurs in the YAML text (for a plain document, in any string key or value)."""
        return self._source.mentions(needle)
