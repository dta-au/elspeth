"""Shared Jinja2 template infrastructure for transform plugins.

Provides a sandboxed Jinja2 environment factory and the TemplateError exception.
Used by both LLM prompt templates and RAG query templates.

The sandbox prevents unsafe access. A row reaches a template only as a
``TemplateRow``: the field values its node declares it reads, projected in
this process before the context crosses to the render worker
(``TemplateRow.project``; ADR-051). No template can reach the row object, its
schema contract, their methods, or a field its node did not declare.
Constant folding of authored expressions
is disabled during bounded-size compilation; rendering runs in a child process
with CPU, memory, input and output ceilings. When every worker is busy a render
waits for one; a worker lost to a signal the row did not cause is retried.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
import pickle
import queue
import resource
import signal
import sys
import threading
from atexit import register as register_exit
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from functools import wraps
from multiprocessing import resource_tracker
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast

from jinja2 import StrictUndefined, Template, TemplateSyntaxError, nodes
from jinja2.compiler import CodeGenerator
from jinja2.exceptions import SecurityError, TemplateAssertionError, TemplateRuntimeError, UndefinedError
from jinja2.meta import TrackingCodeGenerator
from jinja2.sandbox import ImmutableSandboxedEnvironment
from jinja2.utils import missing, object_type_repr
from jinja2.visitor import NodeVisitor

from elspeth.contracts import errors as contract_errors
from elspeth.contracts.errors import PluginRetryableError
from elspeth.contracts.field_spelling import header_spelling_canonical
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.tier_registry import FrameworkBugError
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.core.templates import RETIRED_ROW_API_NAMES, TEMPLATE_ROW_METHODS, validate_jinja_source

if TYPE_CHECKING:
    from elspeth.contracts.schema_contract import PipelineRow


class TemplateError(Exception):
    """Error in template rendering (including sandbox violations)."""


class TemplateWorkerLostError(PluginRetryableError):
    """The render worker was ended mid-request by a signal the row did not cause.

    RLIMIT_CPU's SIGXCPU is the row's template running out of CPU and is a
    ``TemplateError``. Any other signal (the kernel's OOM killer, an
    operator's kill, a crash) says nothing about the row, so the render is
    retried under the run's retry policy on a new worker: the engine's
    ``RetryManager`` records each attempt, and a pooled multi-query LLM node
    retries the one query. The message names the signal number only.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=True)


_MAX_RENDER_BYTES = 4 * 1024 * 1024
_MAX_CONTEXT_BYTES = 8 * 1024 * 1024
_MAX_PARENT_PACK_BYTES = 32 * 1024 * 1024
_MAX_CONTEXT_NODES = 65536
# RLIMIT_AS is virtual address space, including the interpreter's existing
# mappings. Cap growth from the spawned worker's own baseline, not an absolute
# address size that depends on which web/test modules Python imported.
_MAX_WORKER_ADDRESS_GROWTH = 256 * 1024 * 1024
# The render's wall clock. It starts when the request reaches a worker that
# has reported ready, so interpreter start and imports are never charged to a row.
_WORKER_TIMEOUT_SECONDS = 5.0
# How long a new worker may take to report ready. Starting ELSPETH's own
# worker is not row work: one that never becomes ready is ELSPETH's failure.
_WORKER_START_TIMEOUT_SECONDS = 60.0
# The signals the run handles as a graceful stop (engine/orchestrator/shutdown.py).
# A terminal's Ctrl-C and systemd's default stop send them to every process
# in the group or unit, render workers included; the worker ignores both.
_RUN_STOP_SIGNALS = frozenset({signal.SIGINT, signal.SIGTERM})
# Reusable workers bound CPU and memory use. _WORKER_SLOTS is the one
# admission authority: a row waits for a slot (backpressure) instead of
# becoming a template error when every worker is busy.
_WORKER_COUNT = 2
_WORKER_SLOTS = threading.BoundedSemaphore(_WORKER_COUNT)
_AVAILABLE_WORKERS: queue.SimpleQueue[int] = queue.SimpleQueue()
for _worker_index in range(_WORKER_COUNT):
    _AVAILABLE_WORKERS.put(_worker_index)
_WORKERS: list[tuple[multiprocessing.Process, Any] | None] = [None] * _WORKER_COUNT
# Set by the exit handler (``_stop_template_workers_at_exit``); no worker starts after it.
_WORKER_SPAWN_LOCK = threading.Lock()
_INTERPRETER_EXITING = threading.Event()


def _charge_row_export(value: Any, budget: list[int], *, depth: int = 0) -> None:
    """Bound expanded row work before PipelineRow.to_dict makes a deep copy."""
    if depth > 64:
        raise TemplateError("Template context nesting exceeds 64 levels")
    budget[0] += 1
    budget[1] += sys.getsizeof(value)
    if budget[0] > _MAX_CONTEXT_NODES or budget[1] > _MAX_PARENT_PACK_BYTES:
        raise TemplateError("Template context exceeds the parent packing limit")
    if isinstance(value, (dict, MappingProxyType)):
        for key, item in value.items():
            _charge_row_export(key, budget, depth=depth + 1)
            _charge_row_export(item, budget, depth=depth + 1)
    elif isinstance(value, (list, tuple, frozenset)):
        for item in value:
            _charge_row_export(item, budget, depth=depth + 1)


class _NoFoldCodeGenerator(CodeGenerator):
    """Never evaluate an authored expression while compiling a template."""

    def _output_child_to_const(self, node: nodes.Expr, frame: Any, finalize: Any) -> str:
        if type(node) is nodes.TemplateData:
            return super()._output_child_to_const(node, frame, finalize)
        raise nodes.Impossible()

    def visit_EvalContextModifier(self, node: nodes.EvalContextModifier, frame: Any) -> None:
        for keyword in node.options:
            self.writeline(f"context.eval_ctx.{keyword.key} = ")
            self.visit(keyword.value, frame)
            if type(keyword.value) is nodes.Const:
                setattr(frame.eval_ctx, keyword.key, keyword.value.value)
            else:
                frame.eval_ctx.volatile = True


class _NoFoldTrackingCodeGenerator(_NoFoldCodeGenerator, TrackingCodeGenerator):
    """Use Jinja's symbol analysis without its constant-folding compiler."""

    def __init__(self, environment: ImmutableSandboxedEnvironment) -> None:
        super().__init__(environment)
        # TrackingCodeGenerator hard-codes optimized=True even when its
        # environment has optimized=False. Disable that second folding path.
        self.optimizer = None


@dataclass(frozen=True, slots=True)
class DeclaredFields:
    """A template's row holds exactly these fields, named canonically (ADR-051).

    Built from a node's ``required_input_fields`` by ``declared_row_projection``;
    an empty set is a row with no fields.
    """

    names: frozenset[str]


@dataclass(frozen=True, slots=True)
class AllFields:
    """A template's row holds the whole row: the node opted out with ``required_input_fields: []``."""


ALL_FIELDS = AllFields()

# What a template's ``row`` may hold. Required wherever a PipelineRow becomes
# a TemplateRow (``TemplateRow.project``): there is no default.
RowProjection = DeclaredFields | AllFields


def declared_row_projection(
    required_input_fields: Sequence[str] | None,
    *,
    always_declared: frozenset[str] = frozenset(),
) -> RowProjection:
    """The one reading of a template node's declaration as what its template may see (ADR-051).

    - a list names exactly the fields the row holds;
    - ``[]`` is the documented opt-out: the whole row;
    - omitted (``None``) declares nothing, so the row holds nothing. It is
      never the whole row: configuration admits ``None`` only when its static
      analysis finds no row read, and that analysis is not a proof.

    ``always_declared`` names fields a node reads by its own option whatever
    ``required_input_fields`` says (a retrieval node's ``query_field``). They
    join a list and the omitted case, and never turn ``[]`` into a list: the
    opt-out stays the whole row.
    """
    if required_input_fields is None:
        return DeclaredFields(always_declared)
    if len(required_input_fields) == 0:
        return ALL_FIELDS
    return DeclaredFields(frozenset(required_input_fields) | always_declared)


class _UndeclaredFieldError(Exception):
    """A template read a field its node does not declare (raised in the render worker only).

    The key may be computed from row data (``row[row.k]``), so it is an
    attribute, never the message: the worker turns it into value-free text
    (``_undeclared_field_text``) before anything leaves the process.
    ``spells_declared`` says the key is a header spelling of a declared field
    (``header_spelling_canonical``) that this row's producer did not record:
    configuration admits ``row['Name']`` under ``[name]`` because a producer
    whose header is ``Name`` records it, so this row's header is spelled
    otherwise. That is a data-dependent miss, not an undeclared read.
    """

    def __init__(self, key: object, *, spells_declared: bool) -> None:
        super().__init__()
        self.key = key
        self.spells_declared = spells_declared


class _RetiredRowNameError(Exception):
    """A template read a retired row-API name (``row.to_dict``) in attribute form (raised in the render worker only).

    It carries nothing: the name is one of ``RETIRED_ROW_API_NAMES``, but a
    computed ``row | attr(k)`` could spell it from row data, so the reason
    names the reserved set, never the name that was read.
    """


# The value-free text of a ``_RetiredRowNameError``.
_RETIRED_ROW_NAME_TEXT = (
    "the template reads a reserved row name ("
    + ", ".join(sorted(RETIRED_ROW_API_NAMES))
    + ") as an attribute; a template row holds fields and one method, get; read a column of that name as row['<name>']"
)


class TemplateRow(Mapping[str, Any]):
    """The ``row`` a template sees: the field values its node declares it reads, and nothing else.

    A template renders operator-authored code against row data, so it never
    gets the ``PipelineRow`` itself: ``row.contract``, ``row.to_dict()`` and
    the checkpoint API are owned framework surface, and one of them
    (``from_checkpoint``) raises a Tier-1 error whose message quotes row keys.
    ``TemplateRow.project`` builds it in the parent process from the node's
    declaration, before the context crosses to the render worker, so a field
    the node did not declare never reaches the worker at all.

    ``row.name``, ``row['name']`` and ``row['Original Name']`` read a declared
    field by either spelling (the resolution is ``PipelineRow.name_index``
    filtered to the declared fields). The sandbox resolves every attribute
    and item lookup on this type to a field; the one method it admits is
    ``row.get(name)`` (``TEMPLATE_ROW_METHODS``). A retired row-API name
    (``RETIRED_ROW_API_NAMES``) in attribute form raises
    ``_RetiredRowNameError`` whatever the declaration, so ``row.contract``
    never reads a column (``row['contract']`` does). Iteration, ``in`` and
    ``length`` see the declared field names the row carries.

    Used as a value, the row is the mapping it holds: ``{{ row }}`` and every
    string conversion render its field values as a plain mapping (a nested
    value as its data, not ELSPETH's frozen carrier), ``tojson`` serializes it,
    ``last`` reads the last field name (``__reversed__``), ``reverse`` is
    the list of field names in reverse order (``_reverse_row_names``) and
    ``random`` picks one of them (``_random_mapping_key``).
    ``repr`` names the fields and never a value, because a repr can reach an
    exception message.

    A lookup, ``in`` test or ``get`` of a name the node does not declare
    raises ``_UndeclaredFieldError``: a template can learn nothing about an
    undeclared field, not even that it is absent. A declared field the row
    does not carry is an ordinary missing key (``in`` is False, ``get``
    returns its default). This is a deliberate deviation from the ``Mapping``
    contract for ``__contains__`` and ``get`` (ADR-051); ``dict(row)``,
    ``**row``, ``items`` and ``dictsort`` iterate the declared keys and work.
    """

    __slots__ = ("_declared", "_names", "_values")
    _values: Mapping[str, Any]
    _names: Mapping[str, str]
    _declared: frozenset[str] | None

    def __init__(self, values: Mapping[str, Any], names: Mapping[str, str], declared: frozenset[str] | None) -> None:
        # ``declared`` is every spelling of every declared field; None means
        # every name is declared (the ``[]`` opt-out, ``AllFields``).
        object.__setattr__(self, "_values", values)
        object.__setattr__(self, "_names", names)
        object.__setattr__(self, "_declared", declared)

    @classmethod
    def project(cls, row: PipelineRow, projection: RowProjection) -> TemplateRow:
        """Project ``row`` to what its node's template may see — the one place that happens.

        ``AllFields`` keeps the whole row exactly as ``PipelineRow`` resolves
        it. ``DeclaredFields`` keeps the declared fields the row carries,
        readable by their canonical and original spellings, and remembers
        every spelling of every declared field (from the contract, whether or
        not the row carries it) so the worker can tell an absent declared
        field from an undeclared one.
        """
        index = row.name_index()
        match projection:
            case AllFields():
                return cls(row._data, MappingProxyType(index), None)
            case DeclaredFields(names=declared):
                pass
        names = {key: target for key, target in index.items() if target in declared}
        targets = frozenset(names.values())
        values = MappingProxyType({key: value for key, value in row._data.items() if key in targets})
        spellings = declared | frozenset(names) | {fc.original_name for fc in row.contract.fields if fc.normalized_name in declared}
        return cls(values, MappingProxyType(names), spellings)

    def _is_undeclared(self, key: object) -> bool:
        return self._declared is not None and (type(key) is not str or key not in self._declared)

    def _undeclared(self, key: object) -> _UndeclaredFieldError:
        """The error for a read ``_is_undeclared`` refused, telling a spelling of a declared field from an undeclared name."""
        spells_declared = type(key) is str and self._declared is not None and header_spelling_canonical(key, self._declared) is not None
        return _UndeclaredFieldError(key, spells_declared=spells_declared)

    def __setattr__(self, key: str, value: Any) -> None:
        raise TypeError("TemplateRow is immutable")

    def __delattr__(self, key: str) -> None:
        raise TypeError("TemplateRow is immutable")

    def __getitem__(self, key: str) -> Any:
        if type(key) is str and key in self._names:
            return self._values[self._names[key]]
        if self._is_undeclared(key):
            raise self._undeclared(key)
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        if type(key) is str and key in self._names:
            return True
        if self._is_undeclared(key):
            raise self._undeclared(key)
        return False

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __reversed__(self) -> Iterator[str]:
        return reversed(list(self._values))

    def __str__(self) -> str:
        return str(_shown_value(self))

    def __repr__(self) -> str:
        return f"<TemplateRow: {', '.join(self._values)}>"

    def __getattr__(self, key: str) -> Any:
        # jinja2's ``attr`` filter asks ``hasattr`` for a name the class does
        # not define before it defers to the sandbox's getattr, so a field
        # must answer here as ``row.name`` does, and a retired row-API name
        # must be refused here as ``row.contract`` is. An undeclared name
        # raises through ``hasattr`` on purpose.
        if key.startswith("_"):
            raise AttributeError(key)
        _refuse_retired_row_name(key)
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None


def _refuse_retired_row_name(name: str) -> None:
    """The one runtime guard for a retired row-API name in attribute form, under every projection (ADR-051)."""
    if name in RETIRED_ROW_API_NAMES:
        raise _RetiredRowNameError


def _shown_value(value: object) -> object:
    """What a template shows for a value it uses whole: a template row as the plain mapping of its field values.

    ``{{ row }}``, ``pprint`` and ``urlencode`` render this. Row values are
    held frozen (a nested object as a ``mappingproxy``, a list as a tuple),
    and that carrier is ELSPETH's, not the row's: the prompt shows the data,
    thawed. Any other value is shown as itself.
    """
    if type(value) is TemplateRow:
        return deep_thaw(value._values)
    return value


def template_row_values(row: TemplateRow) -> Mapping[str, Any]:
    """Exactly the field values ``row`` holds: what its template can see (``_variables_hash``, ADR-051)."""
    return row._values


class _LocalSandboxedEnvironment(ImmutableSandboxedEnvironment):
    code_generator_class = _NoFoldCodeGenerator

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # A whole row used as a value is the mapping it holds (ADR-051):
        # tojson serializes it (and a frozen nested value) as JSON, and the
        # builtins that inspect their argument's type see the mapping.
        self.policies["json.dumps_function"] = _template_json_dumps
        for name in _MAPPING_TYPED_FILTERS:
            self.filters[name] = _row_as_mapping(self.filters[name])
        self.filters["reverse"] = _reverse_row_names(self.filters["reverse"])
        self.filters["random"] = _random_mapping_key(self.filters["random"])

    def getattr(self, obj: Any, attribute: str) -> Any:
        if type(obj) is TemplateRow:
            if attribute in TEMPLATE_ROW_METHODS:
                # TEMPLATE_ROW_METHODS is {"get"}: the row's one method.
                return obj.get
            if attribute.startswith("_"):
                return self.unsafe_undefined(obj, attribute)
            _refuse_retired_row_name(attribute)
            return self._row_field(obj, attribute)
        return super().getattr(obj, attribute)

    def getitem(self, obj: Any, argument: Any) -> Any:
        if type(obj) is TemplateRow:
            # No attribute fallback: jinja2's stock getitem would try
            # getattr(row, key) on a miss and reach the Mapping methods.
            return self._row_field(obj, argument)
        return super().getitem(obj, argument)

    def _row_field(self, row: TemplateRow, key: Any) -> Any:
        # ``in`` raises for a name the node does not declare; a declared field
        # the row does not carry is the ordinary undefined.
        if key in row:
            return row[key]
        return self.undefined(obj=row, name=key)


# Builtin filters that decide what to do by their argument's type rather than
# by the Mapping protocol: ``urlencode`` encodes pairs only for an
# ``isinstance(value, dict)``, and ``pprint`` formats a repr. Each sees a
# template row as the mapping it holds (``_shown_value``), as every
# other whole-row filter already does through iteration and lookup.
_MAPPING_TYPED_FILTERS: tuple[str, ...] = ("pprint", "urlencode")


def _row_as_mapping(filter_function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(filter_function)
    def row_as_mapping(value: Any, *args: Any, **kwargs: Any) -> Any:
        return filter_function(_shown_value(value), *args, **kwargs)

    return row_as_mapping


def _reverse_row_names(filter_function: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """``row | reverse`` is the list of the row's field names in reverse order.

    The builtin returns ``reversed(value)`` whenever that succeeds, and
    ``reversed`` of a template row is a lazy iterator (``__reversed__``, which
    ``last`` reads), so printed bare it would send the iterator's repr, a
    memory address, to the provider. The names themselves are what the row
    holds, so the row's reverse is them, in a list.
    """

    @wraps(filter_function)
    def reverse_row_names(value: Any) -> Any:
        if type(value) is TemplateRow:
            return list(reversed(value))
        return filter_function(value)

    return reverse_row_names


def _random_mapping_key(filter_function: Callable[..., Any]) -> Callable[..., Any]:
    """``row | random`` is one of the row's field names, chosen at random, as ``row | list | random`` is: a mapping's random key.

    The builtin picks ``seq[randrange(len(seq))]``: it indexes its argument
    by position, which a mapping does not support (a template row, a
    multi-query's variables and a mapping field value are keyed by name), so
    over any of them it failed every row. A mapping used as a collection is
    its keys (``first``, ``last``, ``list``; a row's ``reverse``), so
    ``random`` picks among them. ``wraps`` keeps the builtin's
    context-passing marker, so Jinja still hands it the context.
    """

    @wraps(filter_function)
    def random_mapping_key(context: Any, value: Any) -> Any:
        if isinstance(value, Mapping):
            return filter_function(context, list(value))
        return filter_function(context, value)

    return random_mapping_key


def _template_json_default(value: object) -> object:
    if type(value) is TemplateRow or type(value) is MappingProxyType:
        return dict(value)
    # json's own message for this names only the type: nothing of the value.
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _template_json_dumps(value: Any, **kwargs: Any) -> str:
    """``tojson``'s dumps: a template row and a frozen mapping value serialize as the JSON objects they hold."""
    return json.dumps(value, default=_template_json_default, **kwargs)


@dataclass(frozen=True)
class _RowTransport:
    """A projected row crossing to the render worker as plain data: its values, name index and declared spellings."""

    data: bytes
    names: bytes
    declared: frozenset[str] | None


def _pack_context_value(
    value: Any,
    *,
    depth: int = 0,
    memo: dict[int, Any] | None = None,
    active: set[int] | None = None,
    budget: list[int] | None = None,
) -> Any:
    """Detach frozen carriers with alias preservation and a parent work cap.

    A ``TemplateRow`` crosses as plain data (its projected values, name index
    and declared spellings) and arrives as the same ``TemplateRow``: the
    contract never reaches the render worker, and neither does a field the
    projection left out. An unprojected ``PipelineRow`` or a contract object
    anywhere in a template context is a caller bug.
    """
    from elspeth.contracts.freeze import FrozenJsonArray
    from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract

    if depth > 64:
        raise TemplateError("Template context nesting exceeds 64 levels")
    if memo is None:
        memo = {}
    if active is None:
        active = set()
    if budget is None:
        budget = [0, 0]
    identity = id(value)
    if identity in active:
        raise TemplateError("Template context contains a cyclic container")
    if identity in memo:
        return memo[identity]
    budget[0] += 1
    if type(value) is MappingProxyType:
        estimated_bytes = 64 + 72 * len(value)
    elif type(value) in (dict, list, tuple, FrozenJsonArray, str, bytes, int, float, bool):
        estimated_bytes = sys.getsizeof(value)
    else:
        estimated_bytes = 128
    budget[1] += estimated_bytes
    if budget[0] > _MAX_CONTEXT_NODES or budget[1] > _MAX_PARENT_PACK_BYTES:
        raise TemplateError("Template context exceeds the parent packing limit")
    active.add(identity)
    try:
        if type(value) is TemplateRow:
            _charge_row_export(value._values, budget, depth=depth + 1)
            data = pickle.dumps(deep_thaw(value._values), protocol=5)
            names = pickle.dumps(dict(value._names), protocol=5)
            budget[1] += len(data) + len(names)
            if budget[1] > _MAX_PARENT_PACK_BYTES:
                raise TemplateError("Template context exceeds the parent packing limit")
            packed: Any = _RowTransport(data, names, value._declared)
        elif type(value) is PipelineRow:
            raise FrameworkBugError("A template context carries an unprojected PipelineRow: templates see a TemplateRow.project(...) only")
        elif type(value) in (SchemaContract, FieldContract):
            raise FrameworkBugError(f"A template context carries a {type(value).__name__}: templates see row values only")
        elif type(value) in (dict, MappingProxyType):
            packed = {}
            for key, item in value.items():
                _charge_row_export(key, budget, depth=depth + 1)
                if budget[1] > _MAX_CONTEXT_BYTES:
                    raise TemplateError("Template context exceeds the parent packing limit")
                packed[key] = _pack_context_value(item, depth=depth + 1, memo=memo, active=active, budget=budget)
        elif type(value) is list:
            packed = [_pack_context_value(item, depth=depth + 1, memo=memo, active=active, budget=budget) for item in value]
        elif type(value) is FrozenJsonArray:
            packed = FrozenJsonArray(_pack_context_value(item, depth=depth + 1, memo=memo, active=active, budget=budget) for item in value)
        elif type(value) is tuple:
            packed = tuple(_pack_context_value(item, depth=depth + 1, memo=memo, active=active, budget=budget) for item in value)
        elif isinstance(value, (tuple, frozenset)):
            # deep_freeze preserves these carriers when children are already
            # frozen; charge their expanded payload before pickle sees them.
            _charge_row_export(value, budget, depth=depth)
            if budget[1] > _MAX_CONTEXT_BYTES:
                raise TemplateError("Template context exceeds the parent packing limit")
            packed = value
        else:
            packed = value
    finally:
        active.remove(identity)
    memo[identity] = packed
    return packed


def _restore_context_value(value: Any, *, memo: dict[int, Any] | None = None) -> Any:
    from elspeth.contracts.freeze import FrozenJsonArray, deep_freeze

    if memo is None:
        memo = {}
    identity = id(value)
    if identity in memo:
        return memo[identity]
    if type(value) is _RowTransport:
        # The bytes come from _pack_context_value in the parent process over
        # this worker's own pipe, never from an external source. The values
        # are frozen exactly as PipelineRow froze them, so a value renders as before.
        restored: Any = TemplateRow(
            deep_freeze(pickle.loads(value.data)),
            MappingProxyType(pickle.loads(value.names)),
            value.declared,
        )
    elif type(value) is dict:
        restored = {key: _restore_context_value(item, memo=memo) for key, item in value.items()}
    elif type(value) is list:
        restored = [_restore_context_value(item, memo=memo) for item in value]
    elif type(value) is FrozenJsonArray:
        restored = FrozenJsonArray(_restore_context_value(item, memo=memo) for item in value)
    elif type(value) is tuple:
        restored = tuple(_restore_context_value(item, memo=memo) for item in value)
    else:
        restored = value
    memo[identity] = restored
    return restored


def _check_template_source(source: str) -> None:
    try:
        validate_jinja_source(source)
    except ValueError as exc:
        raise TemplateError(str(exc)) from exc


def _template_worker(connection: Any) -> None:
    """Serve bounded renders until the parent closes the pipe or retires us.

    No exception but ``SystemExit`` leaves this function. multiprocessing's
    bootstrap prints any other escaping exception, message and traceback
    included, to the inherited stderr, and a render failure's message can
    quote row data. So every request ends in one reply (``_serve_request``);
    after a ``setup_failed`` reply the worker exits. A failure of the pipe
    protocol itself exits with status 1 and no reply, which the parent treats
    as a framework bug.

    After a ``worker_spent`` reply (its cumulative CPU leaves no room for a
    render's budget under the hard limit) the worker exits too, and the parent
    starts a fresh one.

    The worker's first message is ``ready``, sent once its interpreter has
    started and its memory limit is set; the parent starts a render's wall
    clock only after it (``_start_worker``).

    The worker ignores SIGINT and SIGTERM. A terminal's Ctrl-C goes to the
    run's whole process group, systemd's default stop sends SIGTERM to every
    process in the unit, and the stop is the run's to handle: the orchestrator
    lets in-flight work finish (``engine/orchestrator/shutdown.py``). The
    worker's own lifetime is the pipe (EOF ends it) and the parent's kill. The
    parent spawns it with both signals blocked, so a stop that arrives while
    the interpreter starts stays pending until the ``SIG_IGN`` here discards it.
    """
    for signum in _RUN_STOP_SIGNALS:
        signal.signal(signum, signal.SIG_IGN)
    signal.pthread_sigmask(signal.SIG_UNBLOCK, _RUN_STOP_SIGNALS)
    try:
        try:
            _limit_worker_address_space()
        except Exception as exc:
            connection.send(("setup_failed", type(exc).__name__))
            raise SystemExit(1) from None
        connection.send(("ready", ""))
        while True:
            try:
                source, payload, value_free = connection.recv()
            except EOFError:
                raise SystemExit(0) from None
            reply = _serve_request(source, payload, value_free)
            # The request's CPU budget ends with its render: lift the soft
            # limit back to the hard one before replying, so the budget a
            # render left behind can never end the worker while it is idle or
            # receiving the next request, which would be charged to that row.
            _, hard_limit = resource.getrlimit(resource.RLIMIT_CPU)
            resource.setrlimit(resource.RLIMIT_CPU, (hard_limit, hard_limit))
            connection.send(reply)
            if reply[0] == "setup_failed":
                raise SystemExit(1)
            if reply[0] == "worker_spent":
                raise SystemExit(0)
    except SystemExit:
        raise
    except BaseException:
        # The pipe itself failed (a malformed request, an unsendable reply).
        # Leave without printing: the parent reads EOF and an exit status of 1.
        raise SystemExit(1) from None
    finally:
        connection.close()


def _limit_worker_address_space() -> None:
    baseline_pages = int(Path("/proc/self/statm").read_text(encoding="ascii").split()[0])
    max_address_space = baseline_pages * os.sysconf("SC_PAGE_SIZE") + _MAX_WORKER_ADDRESS_GROWTH
    resource.setrlimit(resource.RLIMIT_AS, (max_address_space, max_address_space))


def _serve_request(source: str, payload: bytes, value_free: bool) -> tuple[str, str]:
    """One request, in two phases; every failure becomes the reply.

    SETUP is everything before the template meets the row: the CPU limit,
    unpickling and restoring the context the parent packed, and building the
    template the parent already compiled. A failure there is ELSPETH's own
    (``setup_failed``, which the parent turns into a ``FrameworkBugError``).
    A worker whose cumulative CPU leaves less than the render's budget under
    a finite hard RLIMIT_CPU is spent, not failed: it replies
    ``worker_spent`` without touching the request, and the parent sends the
    request to a fresh worker.

    RENDER is the template meeting the row. A Tier-1 error there is ELSPETH's
    bug too: the template sees only plain row values (``TemplateRow``), never
    owned framework API, so it cannot come from row data (``render_tier1``).
    A read of a field the node does not declare is reported with its own
    value-free text (``undeclared_field``). Any other failure is this row's
    and is reported by its class alone, whatever ``value_free`` says, because
    its message can quote row data.
    """
    try:
        # RLIMIT_CPU is cumulative over a process lifetime. Give each
        # request two more CPU seconds, keeping the inherited hard bound; the
        # worker lifts the soft limit back to that bound after the render.
        _, hard_limit = resource.getrlimit(resource.RLIMIT_CPU)
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_limit = math.ceil(usage.ru_utime + usage.ru_stime + 2)
        if hard_limit != resource.RLIM_INFINITY and cpu_limit > hard_limit:
            return "worker_spent", ""
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, hard_limit))
        context = pickle.loads(payload)
        if type(context) is not dict or any(type(key) is not str for key in context):
            raise FrameworkBugError("Template worker received an invalid context")
        context = _restore_context_value(context)
        undefined = StrictUndefined
        if value_free:
            parser = _LocalSandboxedEnvironment(undefined=StrictUndefined, autoescape=False, optimized=False)
            undefined = _value_free_undefined(_template_literals(parser.parse(source)))
        environment = _LocalSandboxedEnvironment(undefined=undefined, autoescape=False, optimized=False)
        template = environment.from_string(source)
    except Exception as exc:
        return "setup_failed", type(exc).__name__
    try:
        pieces: list[str] = []
        size = 0
        for piece in template.generate(**context):
            size += len(piece.encode("utf-8"))
            if size > _MAX_RENDER_BYTES:
                raise TemplateError(f"Rendered template exceeds {_MAX_RENDER_BYTES} UTF-8 bytes")
            pieces.append(piece)
        return "ok", "".join(pieces)
    except _ValueFreeUndefinedError as exc:
        return "safe_undefined", str(exc)[:1024]
    except _ValueFreeUnsafeAccessError as exc:
        return "safe_security", str(exc)[:1024]
    except _UndefinedContractError as exc:
        return "undefined_contract", str(exc)[:1024]
    except _UndeclaredFieldError as exc:
        return "undeclared_field", _undeclared_field_text(exc.key, source, spells_declared=exc.spells_declared)
    except _RetiredRowNameError:
        return "retired_row_name", _RETIRED_ROW_NAME_TEXT
    except contract_errors.TIER_1_ERRORS as exc:
        return "render_tier1", type(exc).__name__
    except (
        TemplateError,
        TemplateSyntaxError,
        TemplateRuntimeError,
        UndefinedError,
        SecurityError,
        MemoryError,
        ArithmeticError,
        TypeError,
        ValueError,
    ) as exc:
        # Keep the protocol bounded and do not pickle a third-party exception.
        return type(exc).__name__, type(exc).__name__ if value_free else str(exc)[:1024]
    except Exception as exc:
        return type(exc).__name__, type(exc).__name__


def _retire_worker(index: int) -> None:
    entry = _WORKERS[index]
    _WORKERS[index] = None
    if entry is None:
        return
    process, connection = entry
    connection.close()
    if process.is_alive():
        process.kill()
    if process.pid is not None:
        process.join()


def _stop_template_workers() -> None:
    for index in range(len(_WORKERS)):
        _retire_worker(index)


def _stop_template_workers_at_exit() -> None:
    """Stop the workers at interpreter exit, and start none after that.

    multiprocessing's own exit handler runs after this one (it was registered
    first). It SIGTERMs every live daemon child and then waits for it. A
    worker ignores SIGTERM, so one started after this point by a thread still
    rendering at exit would make that wait endless. The flag is set under the
    spawn lock: a start already under way registers its worker before this
    handler stops them, and any later start is refused.
    """
    with _WORKER_SPAWN_LOCK:
        _INTERPRETER_EXITING.set()
    _stop_template_workers()


register_exit(_stop_template_workers_at_exit)


def _worker_death(process: Any, *, rendering: bool) -> Exception:
    """Classify a worker that closed the pipe without replying, by how it ended.

    The worker replies to every request it can (``_template_worker``), so a
    missing reply means the process ended. Only its exit status says why, and
    the text names that status alone.

    - SIGXCPU while rendering is this row's template running out of its CPU
      seconds: a routed ``TemplateError``. The soft RLIMIT_CPU is this
      request's budget only from its SETUP until its reply; the worker lifts
      it back to the hard limit before replying (``_template_worker``).
    - Any other signal, or SIGXCPU before a request was rendering, says
      nothing about the row (the kernel's OOM killer, an operator's kill, a
      crash): ``TemplateWorkerLostError``, which the run retries. A stop the
      run handles itself (SIGINT, SIGTERM) never ends a worker: it ignores both.
    - An exit with no signal is ELSPETH's bug: the worker leaves only through
      ``SystemExit``, after a reply or on EOF, so none can end it silently.
    """
    process.join(_WORKER_TIMEOUT_SECONDS)
    exitcode = process.exitcode
    if rendering and exitcode == -signal.SIGXCPU:
        return TemplateError("Template exceeded the CPU limit")
    if exitcode is not None and exitcode < 0:
        return TemplateWorkerLostError(f"Template worker was stopped by signal {-exitcode}")
    return FrameworkBugError(f"Template worker ended without a reply (exit status {exitcode})")


def _start_worker(index: int) -> tuple[Any, Any]:
    """Spawn worker ``index`` and wait until it reports ready.

    The worker is registered before the wait, so a caller that fails here
    retires it like any other worker whose exchange did not complete.
    """
    process_context = multiprocessing.get_context("spawn")
    parent, child = process_context.Pipe(duplex=True)
    process = cast("Any", process_context).Process(target=_template_worker, args=(child,))
    process.daemon = True
    # The child inherits this thread's signal mask: a stop signal during its
    # interpreter start stays pending until the worker ignores it
    # (``_template_worker``). multiprocessing starts its resource tracker on a
    # process's first spawn and unblocks SIGINT and SIGTERM in this thread
    # afterwards (bpo-33613), so it is started before the block, never inside it.
    resource_tracker.ensure_running()
    with _WORKER_SPAWN_LOCK:
        if _INTERPRETER_EXITING.is_set():
            parent.close()
            child.close()
            raise FrameworkBugError("A template render started after the interpreter began to exit")
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _RUN_STOP_SIGNALS)
        try:
            process.start()
        except BaseException:
            parent.close()
            child.close()
            raise
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        child.close()
        _WORKERS[index] = (process, parent)
    try:
        if not parent.poll(_WORKER_START_TIMEOUT_SECONDS):
            raise FrameworkBugError(f"Template worker did not become ready within {_WORKER_START_TIMEOUT_SECONDS:g} s")
        status, value = parent.recv()
    except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
        raise _worker_death(process, rendering=False) from exc
    if status == "setup_failed":
        raise FrameworkBugError(f"Template worker setup failed: {value}")
    if status != "ready":
        raise FrameworkBugError("Template worker's first message was not ready")
    return process, parent


def _exchange(entry: tuple[Any, Any], source: str, payload: bytes, value_free: bool) -> tuple[str, str]:
    """Send one request to a running worker and read its reply."""
    process, parent = entry
    try:
        parent.send((source, payload, value_free))
        if not parent.poll(_WORKER_TIMEOUT_SECONDS):
            raise TemplateError("Template exceeded the execution time limit")
        status, value = parent.recv()
    except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
        # A worker that ended with the request still unread resets the
        # socket instead of closing it; either way it is a death.
        raise _worker_death(process, rendering=True) from exc
    return status, value


def _run_template_worker(source: str, payload: bytes, *, value_free: bool = False) -> str:
    if len(payload) > _MAX_CONTEXT_BYTES:
        raise TemplateError(f"Template context exceeds {_MAX_CONTEXT_BYTES} bytes")
    index = _AVAILABLE_WORKERS.get_nowait()
    response_received = False
    try:
        entry = _WORKERS[index]
        if entry is None or not entry[0].is_alive():
            _retire_worker(index)
            entry = _start_worker(index)
        status, value = _exchange(entry, source, payload, value_free)
        if status == "worker_spent":
            # The worker exits after this reply: its cumulative CPU leaves no
            # room for a render's budget under the hard RLIMIT_CPU. That is its
            # lifetime, not this row: a fresh worker starts from zero.
            _retire_worker(index)
            status, value = _exchange(_start_worker(index), source, payload, value_free)
            if status == "worker_spent":
                raise FrameworkBugError("The hard RLIMIT_CPU leaves a new template worker less than one render's CPU budget")
        response_received = True
        if status == "setup_failed":
            # The worker exits after this reply; do not hand its slot on.
            _retire_worker(index)
            raise FrameworkBugError(f"Template worker setup failed: {value}")
        if status == "render_tier1":
            _retire_worker(index)
            raise FrameworkBugError(f"Template rendering raised a Tier-1 error: {value}")
        if status == "ok":
            if type(value) is not str:
                # The worker only ever sends a joined string: the protocol is broken.
                raise FrameworkBugError("Template worker returned a non-string result")
            return value
        if status == "undeclared_field":
            # The worker built this text from the template's own literals.
            raise TemplateError(f"Undeclared field: {value}")
        if status == "retired_row_name":
            # A fixed text: it names the reserved set, never the name read.
            raise TemplateError(f"Reserved row name: {value}")
        if value_free:
            if status == "safe_undefined":
                raise TemplateError(f"Undefined variable: {value}")
            if status == "safe_security":
                raise TemplateError(f"Sandbox violation: {value}")
            if status == "undefined_contract":
                raise _UndefinedContractError(value)
            if status == "UndefinedError":
                raise TemplateError(f"Undefined variable: {value} (message withheld: it can quote row data)")
            if status == "SecurityError":
                raise TemplateError(f"Sandbox violation: {value} (message withheld: it can quote row data)")
            if status == "TemplateError":
                raise TemplateError("Template rendering failed: TemplateError (message withheld: it can quote row data)")
            if status == "MemoryError":
                _retire_worker(index)
                raise TemplateError("Template worker exceeded the memory limit")
            raise TemplateError(f"Template rendering failed: {value} (message withheld: it can quote row data)")
        if status == "TemplateSyntaxError":
            raise TemplateSyntaxError(value, 1)
        if status == "UndefinedError":
            raise UndefinedError(value)
        if status == "SecurityError":
            raise SecurityError(value)
        if status == "TemplateError":
            raise TemplateError(value)
        if status == "MemoryError":
            _retire_worker(index)
            raise TemplateError("Template worker exceeded the memory limit")
        raise TemplateRuntimeError(value)
    finally:
        try:
            # An interrupted exchange may leave a response (or partial frame)
            # in the pipe. Reusing it would attribute that output to another row.
            if not response_received:
                _retire_worker(index)
        finally:
            _AVAILABLE_WORKERS.put(index)


class _BoundedTemplate:
    def __init__(self, source: str, *, value_free: bool = False) -> None:
        self._source = source
        self._value_free = value_free

    def render(self, **context: Any) -> str:
        _check_template_source(self._source)
        _WORKER_SLOTS.acquire()
        try:
            transport = _pack_context_value(context)
            return _run_template_worker(self._source, pickle.dumps(transport, protocol=5), value_free=self._value_free)
        finally:
            _WORKER_SLOTS.release()


class _BoundedEnvironment(_LocalSandboxedEnvironment):
    def __init__(self, *, value_free: bool = False, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._value_free = value_free

    def parse(self, source: str, name: str | None = None, filename: str | None = None) -> nodes.Template:
        _check_template_source(source)
        try:
            return super().parse(source, name=name, filename=filename)
        except RecursionError as exc:
            raise TemplateError("Template expression nesting exceeds the parser limit") from exc

    def _parse(self, source: str, name: str | None, filename: str | None) -> nodes.Template:
        # Jinja's parse() wraps this method and hands a TemplateSyntaxError
        # raised here to its own error handling, so a configuration-literal
        # rejection reads exactly as Jinja's compile-time rejection does.
        ast = super()._parse(source, name, filename)
        _check_template_ast(ast)
        _check_configuration_literals(ast, self)
        return ast

    def from_string(
        self,
        source: str | nodes.Template,
        globals: object = None,
        template_class: type[Template] | None = None,
    ) -> Template:
        if globals is not None or template_class is not None:
            raise TemplateError("Custom template globals and classes are unsupported")
        if type(source) is not str:
            raise TemplateError("Pre-parsed Jinja templates are unsupported")
        ast = self.parse(source)
        # Jinja's stock compiler folds authored constants here. This
        # environment disables that path, so syntax validation is bounded by
        # the source/AST limits and does not wait for a child on the web loop.
        compiled = super().from_string(ast)
        # Static text has a fixed output no larger than its bounded source.
        # Keep it in-process so ordinary blob paths need no worker startup.
        if all(type(node) is nodes.Output and all(type(child) is nodes.TemplateData for child in node.nodes) for node in ast.body):
            return compiled
        return cast("Template", _BoundedTemplate(source, value_free=self._value_free))


def _check_template_ast(ast: nodes.Template) -> None:
    # Admit depth before Jinja's code generator and our name analysis recurse.
    # find_all itself recurses, so it cannot safely enforce this budget.
    pending = [(child, 1) for child in ast.iter_child_nodes()]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 2048:
            raise TemplateError("Template AST exceeds 2048 nodes")
        if depth > 64:
            raise TemplateError("Template AST nesting exceeds 64 levels")
        pending.extend((child, depth + 1) for child in node.iter_child_nodes())
    if next(ast.find_all(nodes.Pow), None) is not None:
        raise TemplateError("Power expressions are not supported in pipeline templates")
    for modifier in ast.find_all(nodes.EvalContextModifier):
        if any(type(option.value) is not nodes.Const or type(option.value.value) is not bool for option in modifier.options):
            raise TemplateError("Template autoescape requires a literal boolean")


# The filters that take a filter or test NAME as a positional argument, and
# its position after the filtered value (jinja2 ``prepare_map`` /
# ``prepare_select_or_reject``).
_NAME_ARGUMENT_FILTERS: Mapping[str, tuple[str, int]] = MappingProxyType(
    {
        "map": ("filter", 0),
        "select": ("test", 0),
        "reject": ("test", 0),
        "selectattr": ("test", 1),
        "rejectattr": ("test", 1),
    }
)


def _check_configuration_literals(ast: nodes.Template, environment: ImmutableSandboxedEnvironment) -> None:
    """Reject a template the operator's own literals make fail on every row.

    Such a failure is a configuration error, not a fact about a row, so it is
    refused when the template is built (``elspeth validate``, the composer,
    run start) instead of being routed once per row:

    - An unknown filter or test name. Jinja rejects one at compile time, but
      only outside ``{% if %}`` and inline ``if`` expressions; inside them it
      defers the error to render. The same holds for a literal name given to
      ``map``, ``select``, ``reject``, ``selectattr`` and ``rejectattr``.
    - ``truncate`` arguments that cannot bind (too many, an unknown keyword,
      one given twice), or whose literal values break its preconditions
      (``length >= len(end)``, ``leeway >= 0``) or its slicing (a ``length``
      that is not an integer). Each precondition is decided over only the
      arguments it reads, so a row-derived argument it does not read does
      not defer it; one it does read is decided at render and routes.
    - A number literal too large for a float (``1e400``). Python reads it as
      infinity, which Jinja's code generator writes back as the bare name
      ``inf``: in an expression (``truncate(1e400)``, ``x == 1e400``) every
      render then fails with ``NameError``.

    The messages quote only template text.
    """
    for constant in ast.find_all(nodes.Const):
        if type(constant.value) is float and not math.isfinite(constant.value):
            raise TemplateAssertionError(
                "A number literal in this template is too large for a float (it overflows to infinity).", constant.lineno
            )
    for test in ast.find_all(nodes.Test):
        if test.name not in environment.tests:
            raise TemplateAssertionError(f"No test named {test.name!r}.", test.lineno)
    for node in ast.find_all(nodes.Filter):
        if node.name not in environment.filters:
            raise TemplateAssertionError(f"No filter named {node.name!r}.", node.lineno)
        if node.name in _NAME_ARGUMENT_FILTERS:
            named_kind, position = _NAME_ARGUMENT_FILTERS[node.name]
            if len(node.args) > position:
                argument = node.args[position]
                named_registry = environment.filters if named_kind == "filter" else environment.tests
                if type(argument) is nodes.Const and type(argument.value) is str and argument.value not in named_registry:
                    raise TemplateAssertionError(f"No {named_kind} named {argument.value!r}.", node.lineno)
        elif node.name == "truncate":
            _check_truncate_literals(node, environment)


# truncate(s, length=255, killwords=False, end='...', leeway=None): the
# arguments after the filtered value, in order, and the defaults Jinja uses
# for the three its preconditions read.
_TRUNCATE_PARAMETERS = ("length", "killwords", "end", "leeway")
_TRUNCATE_DEFAULTS: Mapping[str, int | str | None] = MappingProxyType({"length": 255, "end": "...", "leeway": None})
# Stand-ins for an argument the row supplies: each satisfies every
# precondition it takes part in, so the probe below fails only on what the
# literals alone decide. ``end=""`` asks the least of ``length >= len(end)``;
# ``leeway=0`` passes ``leeway >= 0`` and adds nothing to ``length + leeway``.
# A row-supplied ``length`` stands in as ``len(end)``, the least that holds.
_TRUNCATE_ROW_END = ""
_TRUNCATE_ROW_LEEWAY = 0


def _check_truncate_literals(node: nodes.Filter, environment: ImmutableSandboxedEnvironment) -> None:
    """Refuse ``truncate`` arguments that fail on every row, deciding each precondition over only the arguments it reads.

    Jinja's ``do_truncate`` fails a render, whatever the row, when:

    - the call cannot bind: more than four arguments after the filtered
      value, a keyword it does not take, or one argument given twice;
    - ``length`` is not an integer (it slices with it; read alone);
    - ``length >= len(end)`` fails (reads length and end — a literal ``end``
      that is not a string fails ``len()`` whatever ``length`` is, and a
      negative literal ``length`` fails it whatever ``end`` is);
    - ``leeway >= 0`` fails (reads leeway alone);
    - ``length + leeway`` overflows (reads both).

    An argument computed from the row — or one a ``*args``/``**kwargs``
    spread may supply — is replaced by the stand-in that satisfies every
    precondition it takes part in, and Jinja's own filter then runs on an
    empty string: its assertions run before it reads the string, and an
    empty string returns unchanged. So a refusal names a failure the
    operator's literals decide on their own, and an argument the row decides
    is left to render, where it routes.
    """
    if len(node.args) > len(_TRUNCATE_PARAMETERS):
        raise TemplateAssertionError(
            f"truncate() takes at most {len(_TRUNCATE_PARAMETERS)} arguments after the filtered value, got {len(node.args)}.",
            node.lineno,
        )
    arguments: dict[str, nodes.Expr] = dict(zip(_TRUNCATE_PARAMETERS, node.args, strict=False))
    for keyword in node.kwargs:
        # Jinja's parser builds a call's keyword arguments as Keyword nodes
        # with a str key (its annotations say Pair); anything else is left to render.
        key: object = keyword.key
        if type(key) is not str:
            return
        if key not in _TRUNCATE_PARAMETERS:
            raise TemplateAssertionError(f"truncate() got an unexpected keyword argument {key!r}.", node.lineno)
        if key in arguments:
            raise TemplateAssertionError(f"truncate() got multiple values for argument {key!r}.", node.lineno)
        arguments[key] = keyword.value
    spread = node.dyn_args is not None or node.dyn_kwargs is not None
    literals: dict[str, int | float | str | None] = {}
    for name, default in _TRUNCATE_DEFAULTS.items():
        if name in arguments:
            is_literal, value = _literal_argument(arguments[name])
            if is_literal:
                literals[name] = value
        elif not spread:
            # Absent and nothing can supply it: Jinja's default.
            literals[name] = default
    # The empty-string probe returns before truncate slices, so a non-integer
    # length (truncate(10.5), truncate(True)) would pass it and then fail every
    # row long enough to be truncated ("slice indices must be integers") or
    # every row outright.
    if "length" in literals and type(literals["length"]) is not int:
        raise TemplateAssertionError(f"truncate() length must be an integer literal, got {type(literals['length']).__name__}.", node.lineno)
    # The literals override the stand-ins; every other argument the probe
    # reads is the row's, so it takes the stand-in.
    probe: dict[str, int | float | str | None] = {"end": _TRUNCATE_ROW_END, "leeway": _TRUNCATE_ROW_LEEWAY, **literals}
    if "length" not in probe:
        row_end = probe["end"]
        # A literal end that is not a string fails len() whatever the length;
        # any stand-in length lets the probe report that.
        probe["length"] = len(row_end) if type(row_end) is str else 0
    try:
        environment.filters["truncate"](environment, "", **probe)
    except (AssertionError, TypeError, ArithmeticError) as exc:
        # ArithmeticError: ``length + leeway`` with an int literal too large
        # for a float overflows on every render.
        raise TemplateAssertionError(f"truncate() arguments can never be satisfied: {exc}", node.lineno) from exc


def _literal_argument(argument: nodes.Expr) -> tuple[bool, int | float | str | None]:
    """Return ``(True, value)`` for an argument written as a literal, else ``(False, None)``.

    A literal is a ``Const`` (the parser builds one only for a number, bool,
    string or ``none``; a bool is an int here) or a unary minus
    or plus over a number or bool ``Const``: Jinja parses ``-1`` as
    ``Neg(Const(1))`` and ``+10.5`` as ``Pos(Const(10.5))``, and evaluates
    both without the row. Constant EXPRESSIONS (``10 / 2``) are deliberately
    not folded: the environment disables Jinja's constant folding
    (``optimized=False``), so configuration validation never evaluates
    operator arithmetic either.
    """
    if type(argument) is nodes.Const:
        return True, argument.value
    if (type(argument) is nodes.Neg or type(argument) is nodes.Pos) and type(argument.node) is nodes.Const:
        operand = argument.node.value
        if type(operand) in (int, float, bool):
            return True, (-operand if type(argument) is nodes.Neg else +operand)
    return False, None


def create_sandboxed_environment(*, value_free: bool = False) -> ImmutableSandboxedEnvironment:
    """Create an ImmutableSandboxedEnvironment with StrictUndefined.

    Args:
        value_free: Render failures use only operator-authored keys and types.

    Returns:
        A sandboxed Jinja2 environment that:
        - Raises on undefined variables (StrictUndefined)
        - Blocks attribute access and method calls (ImmutableSandboxedEnvironment)
        - Does not HTML-escape output (autoescape=False)
        - Bounds compile input, render time, worker memory, and output size
    """
    return _BoundedEnvironment(
        undefined=StrictUndefined,
        autoescape=False,
        optimized=False,
        value_free=value_free,
    )


# Exceptions a render of an operator's template over row data may raise as a
# per-row operational failure (tier-model-deep-dive: "Pipeline Templates as
# Tier 2 Data"). UndefinedError and SecurityError are TemplateRuntimeErrors;
# OverflowError and ZeroDivisionError are ArithmeticErrors.
_RENDER_FAILURES = (TemplateSyntaxError, TemplateRuntimeError, ArithmeticError, TypeError, ValueError)

# int() refuses a decimal string longer than this (0: no limit).
_INT_DIGIT_LIMIT = sys.get_int_max_str_digits()

# Stands in for a lookup key the template does not spell out, i.e. one computed
# at render time (``row[row.k]``, ``attr(row.k)``, ``row.items[row.i]``).
_UNSPELLED_KEY = "<a key the template does not spell out>"


class _ValueFreeUndefinedError(UndefinedError):
    """An undefined lookup whose message ELSPETH built, naming no row value."""


class _ValueFreeUnsafeAccessError(SecurityError):
    """A sandbox-refused attribute lookup whose message ELSPETH built, naming no row value."""


class _UndefinedContractError(FrameworkBugError):
    """The installed Jinja2 no longer uses its documented undefined contract.

    A framework fault, never a row fault: every template that looks a key up
    would fail the same way. As a ``FrameworkBugError`` it is Tier-1, so the
    run aborts instead of routing every row.
    """


def withheld_error_detail(exc: BaseException) -> str:
    """The value-free text of a failure whose own message may quote row data: its class only.

    Jinja's messages are not value-free. An undefined or sandbox-refused
    lookup quotes its KEY, and a template may compute that key from the row
    (``{{ row[row.k] }}`` renders ``'dict object' has no attribute '<the value
    of k>'``). A Python error inside the template quotes operands
    (``wordwrap`` renders ``invalid width -5``, a codec error quotes the
    offending character and its offset), and the canonicalizer quotes an
    out-of-range integer. The row itself stays attributable through the token
    (``transform_errors`` stores it), so the failing input can be recovered
    without copying it into the reason.
    """
    return f"{type(exc).__name__} (message withheld: it can quote row data)"


def _template_literals(ast: nodes.Template) -> frozenset[str | int]:
    """Every name and constant the operator wrote into the template.

    A lookup key found here is printable: it is config text, not row text.
    A row value that happens to equal one of these prints as that config text,
    the same rule as ``safe_validation_error_text`` printing a field its schema
    declares.
    """
    literals: set[str | int] = set()
    for name_node in ast.find_all(nodes.Name):
        literals.add(name_node.name)
    for getattr_node in ast.find_all(nodes.Getattr):
        literals.add(getattr_node.attr)
    for keyword in ast.find_all(nodes.Keyword):
        literals.add(keyword.key)
    for const in ast.find_all(nodes.Const):
        value = const.value
        if type(value) is str:
            # A dotted literal (``map(attribute='a.b')``) is looked up part by
            # part, and Jinja looks a digit part up as an int (``'a.0'`` reads
            # element 0). A part int() refuses (a non-decimal digit such as
            # '²', or more digits than Python converts) fails Jinja's own int()
            # before any lookup, so it can never be a key.
            literals.add(value)
            for part in value.split("."):
                literals.add(part)
                if part.isdecimal() and (_INT_DIGIT_LIMIT == 0 or len(part) <= _INT_DIGIT_LIMIT):
                    literals.add(int(part))
        elif type(value) is int:
            # ``items[-1]`` parses as Neg(Const(1)).
            literals.update((value, -value))
    return frozenset(literals)


def _undeclared_field_text(key: object, source: str, *, spells_declared: bool) -> str:
    """The value-free text of a read of an undeclared field: the key only when the template spells it out.

    A computed key (``row[row.k]``) can be a row value, so it prints as
    ``_UNSPELLED_KEY`` unless it equals one of the template's own literals,
    and only a spelled key says it is a spelling of a declared field (whether
    a computed key spells one is itself a fact about row data). The declared
    field is not named: behind a source ``field_mapping`` the literal's
    normalized form need not be the name the node declares.
    """
    parser = _LocalSandboxedEnvironment(undefined=StrictUndefined, autoescape=False, optimized=False)
    literals = _template_literals(parser.parse(source))
    if (type(key) is str or type(key) is int) and key in literals:
        if spells_declared:
            return (
                f"the template reads {key!r}, a spelling of a declared field that this row does not carry under "
                "that spelling (its producer recorded another original name, or the field is absent); read the "
                "field by its declared name"
            )
        return f"the template reads {key!r}, a field this node does not declare in required_input_fields"
    return f"the template reads {_UNSPELLED_KEY}, a field this node does not declare in required_input_fields"


def _value_free_undefined(literals: frozenset[str | int]) -> type[StrictUndefined]:
    """A StrictUndefined whose error message names only what the template spells out.

    Every failing operation on an Undefined (``__str__``, ``__add__``,
    ``__getattr__`` ...) raises ``self._undefined_exception(self._undefined_message)``,
    so these two hooks cover them all. Jinja's hint text is never used: the
    sandbox's unsafe-attribute hint quotes the (possibly row-derived) key.
    """

    class _ValueFreeUndefined(StrictUndefined):
        __slots__ = ()

        def __init__(
            self,
            hint: str | None = None,
            obj: Any = missing,
            name: str | None = None,
            exc: type[TemplateRuntimeError] = UndefinedError,
        ) -> None:
            if exc is UndefinedError:
                value_free_exc: type[TemplateRuntimeError] = _ValueFreeUndefinedError
            elif exc is SecurityError:
                value_free_exc = _ValueFreeUnsafeAccessError
            else:
                raise _UndefinedContractError(f"jinja2 built an Undefined with an unexpected exception type {exc.__name__}")
            super().__init__(hint, obj, name, value_free_exc)

        @property
        def _undefined_message(self) -> str:
            name: object = self._undefined_name
            if (type(name) is str or type(name) is int) and name in literals:
                key = repr(name)
            else:
                key = _UNSPELLED_KEY
            # A template row is named as "the row", never by its internal class.
            if self._undefined_exception is _ValueFreeUnsafeAccessError:
                owner = "the row" if type(self._undefined_obj) is TemplateRow else object_type_repr(self._undefined_obj)
                return f"access to attribute {key} of {owner} is unsafe"
            if self._undefined_obj is missing:
                return "a value is undefined" if name is None else f"{key} is undefined"
            if type(self._undefined_obj) is TemplateRow:
                return f"the row has no field {key}"
            if type(name) is str:
                return f"{object_type_repr(self._undefined_obj)!r} has no attribute {key}"
            return f"{object_type_repr(self._undefined_obj)} has no element {key}"

    return _ValueFreeUndefined


class SandboxedTemplate:
    """An operator-authored template validated before bounded, value-free rendering.

    Rendering is where a template meets row data, so a render failure raises
    ``TemplateError`` whose message names no row value (see ``render``). Each caller keeps its own config-time handling of
    ``TemplateSyntaxError`` from the constructor.
    """

    __slots__ = ("_template",)

    def __init__(self, source: str) -> None:
        """Validate and compile ``source`` without evaluating row expressions.

        Raises:
            TemplateSyntaxError: The template is malformed (including an
                unknown filter or test, a ``TemplateAssertionError``).
        """
        self._template = create_sandboxed_environment(value_free=True).from_string(source)

    def render(self, **context: Any) -> str:
        """Render with ``context``: the ONE place a render failure becomes text.

        A row reaches ``context`` only as a ``TemplateRow`` (``TemplateRow.project``);
        an unprojected ``PipelineRow`` is a ``FrameworkBugError``.

        Three kinds of text are ever emitted: a lookup failure raised by this
        template's own undefined type, whose message names the owner's TYPE and
        the key only when the operator's template spells that key out; a read
        of a field the node does not declare (``Undeclared field: ...``), which
        names the key under the same rule; and, for anything else, the
        exception's class name (``withheld_error_detail``).

        Raises:
            TemplateError: A per-row operational failure, prefixed by its kind
                (``Undefined variable``, ``Sandbox violation``, ``Template
                rendering failed``) with a value-free detail.
            TemplateWorkerLostError: The worker was ended by a signal the row
                did not cause. Retryable: the run retries the render.
            FrameworkBugError: ELSPETH's own failure (packing the context,
                starting or preparing the worker, a broken worker protocol or
                Jinja contract). Tier-1: it aborts the run, never becomes a row
                error.

        All workers busy is not a failure: the render waits for one.
        """
        try:
            return self._template.render(**context)
        except contract_errors.TIER_1_ERRORS:
            raise
        except _ValueFreeUndefinedError as exc:
            raise TemplateError(f"Undefined variable: {exc}") from exc
        except _ValueFreeUnsafeAccessError as exc:
            raise TemplateError(f"Sandbox violation: {exc}") from exc
        except UndefinedError as exc:
            raise TemplateError(f"Undefined variable: {withheld_error_detail(exc)}") from exc
        except SecurityError as exc:
            raise TemplateError(f"Sandbox violation: {withheld_error_detail(exc)}") from exc
        except _RENDER_FAILURES as exc:
            raise TemplateError(f"Template rendering failed: {withheld_error_detail(exc)}") from exc


def find_runtime_unbound_variables(ast: nodes.Template) -> frozenset[str]:
    """Return names that may require render context on a reachable path.

    Jinja's ``find_undeclared_variables`` deliberately reports names assigned
    in conditional branches because its code-generation analysis merges all
    branch stores. That is too broad for ELSPETH's StrictUndefined preflight:
    a local assigned in every branch is defined when a later interpolation
    runs. Keep Jinja's conservative candidate set, then remove a candidate only
    when a path- and order-sensitive walk proves it bound at every load.
    """
    if type(ast.environment) is not _BoundedEnvironment:
        raise TemplateError("Template name discovery requires a bounded environment")
    _check_template_ast(ast)
    tracker = _NoFoldTrackingCodeGenerator(ast.environment)
    tracker.visit(ast)
    candidates = frozenset(tracker.undeclared_identifiers)
    analyzer = _DefiniteBindingAnalyzer(candidates)
    analyzer.analyze(ast.body, frozenset())
    return frozenset(analyzer.unbound | (candidates - analyzer.seen))


class _DefiniteBindingAnalyzer(NodeVisitor):
    """Conservative flow analysis for Jinja locals relevant to candidates.

    Dispatch runs through jinja2's own ``NodeVisitor`` (one ``visit_<Class>``
    per concrete node class). Every visitor takes the set of names definitely
    bound before the node and returns the set definitely bound after it;
    unhandled nodes scan their children without binding anything.
    """

    def __init__(self, candidates: frozenset[str]) -> None:
        self._candidates = candidates
        # Root assignments update the render context. Local frames do not;
        # retain their enclosing context separately from their lexical locals.
        self._context_bound: frozenset[str] | None = None
        self.unbound: set[str] = set()
        self.seen: set[str] = set()

    def analyze(self, statements: Iterable[nodes.Node], bound: frozenset[str]) -> frozenset[str]:
        current = bound
        for statement in statements:
            current = self.visit(statement, current)
        return current

    def _block_context(self, bound: frozenset[str]) -> frozenset[str]:
        return bound if self._context_bound is None else self._context_bound

    def _analyze_scope(
        self,
        statements: Iterable[nodes.Node],
        bound: frozenset[str],
        *,
        context_bound: frozenset[str],
    ) -> None:
        enclosing_context = self._context_bound
        self._context_bound = context_bound
        try:
            self.analyze(statements, bound)
        finally:
            self._context_bound = enclosing_context

    def generic_visit(self, node: nodes.Node, bound: frozenset[str]) -> frozenset[str]:
        self._scan_children(node, bound)
        return bound

    def visit_Name(self, node: nodes.Name, bound: frozenset[str]) -> frozenset[str]:
        if node.ctx == "load" and node.name in self._candidates:
            self.seen.add(node.name)
            if node.name not in bound:
                self.unbound.add(node.name)
        return bound

    def visit_Assign(self, node: nodes.Assign, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.node, bound)
        self._scan_assignment_target(node.target, bound)
        return bound | _stored_names(node.target)

    def visit_AssignBlock(self, node: nodes.AssignBlock, bound: frozenset[str]) -> frozenset[str]:
        if node.filter is not None:
            self._scan(node.filter, bound)
        self._analyze_scope(node.body, bound, context_bound=self._block_context(bound))
        self._scan_assignment_target(node.target, bound)
        return bound | _stored_names(node.target)

    def visit_If(self, node: nodes.If, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.test, bound)
        branch_results = [self.analyze(node.body, bound)]
        for elif_node in node.elif_:
            self._scan(elif_node.test, bound)
            branch_results.append(self.analyze(elif_node.body, bound))
        branch_results.append(self.analyze(node.else_, bound) if node.else_ else bound)
        return frozenset.intersection(*branch_results)

    def visit_For(self, node: nodes.For, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.iter, bound)
        loop_bound = bound | _stored_names(node.target) | {"loop"}
        if node.test is not None:
            self._scan(node.test, loop_bound)
        self._analyze_scope(node.body, loop_bound, context_bound=self._block_context(bound))
        self._analyze_scope(node.else_, bound, context_bound=self._block_context(bound))
        return bound

    def visit_With(self, node: nodes.With, bound: frozenset[str]) -> frozenset[str]:
        for value in node.values:
            self._scan(value, bound)
        local_bound = bound
        for target in node.targets:
            local_bound |= _stored_names(target)
        self._analyze_scope(node.body, local_bound, context_bound=self._block_context(bound))
        return bound

    def visit_Macro(self, node: nodes.Macro, bound: frozenset[str]) -> frozenset[str]:
        for default in node.defaults:
            self._scan(default, bound)
        argument_names = frozenset(argument.name for argument in node.args)
        self._analyze_scope(
            node.body,
            bound | argument_names | {"caller", "kwargs", "varargs"},
            context_bound=self._block_context(bound),
        )
        return bound | {node.name}

    def visit_CallBlock(self, node: nodes.CallBlock, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.call, bound)
        for default in node.defaults:
            self._scan(default, bound)
        argument_names = frozenset(argument.name for argument in node.args)
        self._analyze_scope(node.body, bound | argument_names, context_bound=self._block_context(bound))
        return bound

    def visit_Import(self, node: nodes.Import, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.template, bound)
        return bound | {node.target}

    @trust_boundary(
        tier=3,
        source=(
            "a jinja2 FromImport AST node produced by the sandboxed template compiler "
            "from operator-authored template text — jinja2, not ELSPETH, owns its shape"
        ),
        source_param="node",
        suppresses=("R5",),
        invariant=(
            "admits only str entries and (name, alias) 2-tuples with a str alias from "
            "node.names; any other shape raises TemplateError instead of silently "
            "mis-computing the definitely-bound name set"
        ),
        test_ref="tests/unit/plugins/infrastructure/test_templates.py::test_from_import_binding_rejects_malformed_names",
        test_fingerprint="8d52b5b569a25d169595912e4e57b424b6f07c34ec5245e9b098d79e670c04e7",
    )
    def visit_FromImport(self, node: nodes.FromImport, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.template, bound)
        imported_names: set[str] = set()
        for item in node.names:
            if isinstance(item, str):
                imported_names.add(item)
            elif isinstance(item, tuple) and len(item) == 2 and isinstance(item[1], str):
                imported_names.add(item[1])
            else:
                raise TemplateError(f"jinja2 FromImport name entry has an unsupported shape: {item!r}")
        return bound | imported_names

    def visit_FilterBlock(self, node: nodes.FilterBlock, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.filter, bound)
        self._analyze_scope(node.body, bound, context_bound=self._block_context(bound))
        return bound

    def visit_OverlayScope(self, node: nodes.OverlayScope, bound: frozenset[str]) -> frozenset[str]:
        self._scan(node.context, bound)
        self._analyze_scope(node.body, bound, context_bound=bound)
        return bound

    def visit_ScopedEvalContextModifier(self, node: nodes.ScopedEvalContextModifier, bound: frozenset[str]) -> frozenset[str]:
        for option in node.options:
            self._scan(option, bound)
        self.analyze(node.body, bound)
        return bound

    def visit_Block(self, node: nodes.Block, bound: frozenset[str]) -> frozenset[str]:
        # Jinja passes only the render context to an unscoped block. A scoped
        # block derives a context that also contains the caller's local names.
        block_bound = bound if node.scoped else self._block_context(bound)
        self._analyze_scope(node.body, block_bound, context_bound=block_bound)
        return bound

    def visit_Scope(self, node: nodes.Scope, bound: frozenset[str]) -> frozenset[str]:
        self._analyze_scope(node.body, bound, context_bound=self._block_context(bound))
        return bound

    def _scan(self, node: nodes.Node, bound: frozenset[str]) -> None:
        self.visit(node, bound)

    def _scan_children(self, node: nodes.Node, bound: frozenset[str]) -> None:
        for child in node.iter_child_nodes():
            self._scan(child, bound)

    def _scan_assignment_target(self, target: nodes.Node, bound: frozenset[str]) -> None:
        if isinstance(target, nodes.NSRef) and target.name in self._candidates:
            self.seen.add(target.name)
            if target.name not in bound:
                self.unbound.add(target.name)


def _stored_names(target: nodes.Node) -> frozenset[str]:
    if isinstance(target, nodes.Name) and target.ctx in {"param", "store"}:
        return frozenset({target.name})
    return frozenset(child.name for child in target.find_all(nodes.Name) if child.ctx in {"param", "store"})
