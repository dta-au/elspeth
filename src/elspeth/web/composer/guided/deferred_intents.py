"""Creation and validation boundary for guided wrong-stage intent.

The model may suggest only :class:`DeferredIntentAction`.  The server then
validates that suggestion against the request-scoped policy catalog and the
current guided stage before creating durable audit-tier state.  Raw user text
is deliberately accepted only by :func:`create_deferred_stage_intent`, where
it is reduced to a content hash; it is never stored in deferred metadata.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from elspeth.contracts.freeze import deep_thaw, freeze_fields
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.core.expression_parser import ExpressionParser, ExpressionSecurityError, ExpressionSyntaxError
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.schemas import PluginKind
from elspeth.web.composer.guided.connection_consumers import ConsumerIdentity, canonical_connection_consumers
from elspeth.web.composer.guided.errors import GuidedSolverResponseShapeError, InvariantError
from elspeth.web.composer.guided.stage_subjects import (
    CatalogSubjectClarification,
    CatalogSubjectResolved,
    CatalogSubjectUnsupported,
    ComponentCountConstraint,
    DeferredConstraint,
    EdgeRouteConstraint,
    FailureRouteConstraint,
    OptionValueConstraint,
    PluginSubject,
    StableSubject,
    StageName,
    StatedGateRoutingConstraint,
    StatedPredicateConstraint,
    SubjectPresenceConstraint,
    constraint_from_dict,
    resolve_catalog_subject,
    stage_name_from_value,
)
from elspeth.web.composer.guided.state_machine import (
    GUIDED_MAX_CONSTRAINTS_PER_INTENT,
    GUIDED_MAX_REDACTED_SUMMARY_CHARS,
    DeferredStageIntent,
    GuidedSession,
)
from elspeth.web.composer.state import CompositionState
from elspeth.web.plugin_policy.models import PluginId, PluginUnavailableReason

_STAGE_ORDINAL: dict[StageName, int] = {"source": 0, "output": 1, "topology": 2, "wire_review": 3}
_PLUGIN_STAGE: dict[PluginKind, StageName] = {"source": "source", "sink": "output", "transform": "topology"}
_COMPONENT_STAGE: dict[str, StageName] = {
    "source": "source",
    "output": "output",
    "node": "topology",
    "edge": "wire_review",
}
_ACTION_KEYS = frozenset({"target_stage", "catalog_kind", "catalog_name", "redacted_summary", "constraints"})
_ALLOWED_CONSTRAINT_TYPES = {
    SubjectPresenceConstraint,
    OptionValueConstraint,
    ComponentCountConstraint,
    StatedGateRoutingConstraint,
    StatedPredicateConstraint,
    EdgeRouteConstraint,
    FailureRouteConstraint,
}


class DeferredIntentActionShapeError(GuidedSolverResponseShapeError):
    """The model emitted a malformed future-stage action."""


class DeferredIntentManagementActionShapeError(GuidedSolverResponseShapeError):
    """The model emitted a malformed stable-intent management action."""


class DeferredIntentClaimError(ValueError):
    """A planner terminal claimed deferred coverage it did not prove."""


def _require_nonempty_exact_str(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        raise InvariantError(f"{field_name} must be a non-empty exact str")
    return value


@dataclass(frozen=True, slots=True)
class DeferredIntentAction:
    """One model-suggested, non-authoritative future-stage instruction."""

    target_stage: StageName
    catalog_kind: PluginKind | None
    catalog_name: str | None
    redacted_summary: str
    constraints: tuple[DeferredConstraint, ...]

    def __post_init__(self) -> None:
        if self.target_stage not in _STAGE_ORDINAL:
            raise InvariantError("DeferredIntentAction.target_stage is unsupported")
        if (self.catalog_kind is None) != (self.catalog_name is None):
            raise InvariantError("DeferredIntentAction catalog fields must be paired")
        if self.catalog_kind is not None and self.catalog_kind not in _PLUGIN_STAGE:
            raise InvariantError("DeferredIntentAction.catalog_kind is unsupported")
        if self.catalog_name is not None:
            _require_nonempty_exact_str(self.catalog_name, "DeferredIntentAction.catalog_name")
        _require_nonempty_exact_str(self.redacted_summary, "DeferredIntentAction.redacted_summary")
        if len(self.redacted_summary) > GUIDED_MAX_REDACTED_SUMMARY_CHARS:
            raise InvariantError(f"DeferredIntentAction.redacted_summary exceeds {GUIDED_MAX_REDACTED_SUMMARY_CHARS} characters")
        if type(self.constraints) is not tuple:
            raise InvariantError("DeferredIntentAction.constraints must be an exact tuple")
        if not self.constraints:
            raise InvariantError("DeferredIntentAction requires at least one structural constraint")
        if len(self.constraints) > GUIDED_MAX_CONSTRAINTS_PER_INTENT:
            raise InvariantError(f"DeferredIntentAction.constraints exceeds the {GUIDED_MAX_CONSTRAINTS_PER_INTENT}-constraint limit")
        if any(type(constraint) not in _ALLOWED_CONSTRAINT_TYPES for constraint in self.constraints):
            raise InvariantError("DeferredIntentAction.constraints contains an unsupported constraint")


def _canonical_uuid_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise InvariantError(f"{field_name} must be a canonical UUID string")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise InvariantError(f"{field_name} must be a canonical UUID string") from exc
    if str(parsed) != value:
        raise InvariantError(f"{field_name} must be a canonical UUID string")
    return value


@dataclass(frozen=True, slots=True)
class DeferredIntentCancelAction:
    """Explicitly cancel one pending intent by stable identity."""

    intent_id: str
    selection_token: str

    def __post_init__(self) -> None:
        _canonical_uuid_text(self.intent_id, "DeferredIntentCancelAction.intent_id")
        _require_nonempty_exact_str(self.selection_token, "DeferredIntentCancelAction.selection_token")


@dataclass(frozen=True, slots=True)
class DeferredIntentEditAction:
    """Replace one pending intent while retaining its stable identity."""

    intent_id: str
    selection_token: str
    replacement: DeferredIntentAction

    def __post_init__(self) -> None:
        _canonical_uuid_text(self.intent_id, "DeferredIntentEditAction.intent_id")
        _require_nonempty_exact_str(self.selection_token, "DeferredIntentEditAction.selection_token")
        if type(self.replacement) is not DeferredIntentAction:
            raise InvariantError("DeferredIntentEditAction.replacement must be exact")


type DeferredIntentManagementAction = DeferredIntentCancelAction | DeferredIntentEditAction


@dataclass(frozen=True, slots=True)
class DeferredIntentAccepted:
    action: DeferredIntentAction

    def __post_init__(self) -> None:
        if type(self.action) is not DeferredIntentAction:
            raise InvariantError("DeferredIntentAccepted.action must be exact")


@dataclass(frozen=True, slots=True)
class DeferredIntentClarification:
    plugin_name: str
    plugin_kinds: tuple[PluginKind, ...]

    def __post_init__(self) -> None:
        _require_nonempty_exact_str(self.plugin_name, "DeferredIntentClarification.plugin_name")
        canonical = tuple(kind for kind in ("source", "transform", "sink") if kind in self.plugin_kinds)
        if type(self.plugin_kinds) is not tuple or len(self.plugin_kinds) < 2 or self.plugin_kinds != canonical:
            raise InvariantError("DeferredIntentClarification.plugin_kinds must be multiple unique canonical kinds")


@dataclass(frozen=True, slots=True)
class DeferredIntentUnsupported:
    plugin_kind: PluginKind
    plugin_name: str
    reason: PluginUnavailableReason

    def __post_init__(self) -> None:
        if self.plugin_kind not in _PLUGIN_STAGE:
            raise InvariantError("DeferredIntentUnsupported.plugin_kind is unsupported")
        _require_nonempty_exact_str(self.plugin_name, "DeferredIntentUnsupported.plugin_name")
        if type(self.reason) is not PluginUnavailableReason:
            raise InvariantError("DeferredIntentUnsupported.reason must be exact")


DeferredIntentRejectionReason = Literal[
    "target_not_later",
    "wrong_responsible_stage",
    "catalog_kind_mismatch",
    "malformed_catalog_identity",
    "option_value_unproven",
    "stated_fact_unproven",
]


@dataclass(frozen=True, slots=True)
class DeferredIntentRejected:
    reason: DeferredIntentRejectionReason

    def __post_init__(self) -> None:
        if self.reason not in {
            "target_not_later",
            "wrong_responsible_stage",
            "catalog_kind_mismatch",
            "malformed_catalog_identity",
            "option_value_unproven",
            "stated_fact_unproven",
        }:
            raise InvariantError("DeferredIntentRejected.reason is unsupported")


type DeferredIntentValidation = DeferredIntentAccepted | DeferredIntentClarification | DeferredIntentUnsupported | DeferredIntentRejected


def deferred_intent_action_from_dict(value: object) -> DeferredIntentAction:
    """Decode one exact LLM tool argument object into the closed action."""

    try:
        if type(value) is not dict:
            raise InvariantError("DeferredIntentAction must be an exact dict")
        unexpected = set(value) - _ACTION_KEYS
        if unexpected:
            raise InvariantError(f"DeferredIntentAction has unexpected keys {sorted(unexpected)!r}")
        missing = _ACTION_KEYS - set(value)
        if missing:
            raise InvariantError(f"DeferredIntentAction has missing keys {sorted(missing)!r}")
        constraints_raw = value["constraints"]
        if type(constraints_raw) is not list:
            raise InvariantError("DeferredIntentAction.constraints must be a list")
        if len(constraints_raw) > GUIDED_MAX_CONSTRAINTS_PER_INTENT:
            raise InvariantError(f"DeferredIntentAction.constraints exceeds the {GUIDED_MAX_CONSTRAINTS_PER_INTENT}-constraint limit")
        catalog_kind = value["catalog_kind"]
        if catalog_kind is not None and catalog_kind not in _PLUGIN_STAGE:
            raise InvariantError("DeferredIntentAction.catalog_kind is unsupported")
        catalog_name = value["catalog_name"]
        if catalog_name is not None:
            catalog_name = _require_nonempty_exact_str(catalog_name, "DeferredIntentAction.catalog_name")
        return DeferredIntentAction(
            target_stage=stage_name_from_value(value["target_stage"], "DeferredIntentAction.target_stage"),
            catalog_kind=cast(PluginKind | None, catalog_kind),
            catalog_name=catalog_name,
            redacted_summary=_require_nonempty_exact_str(value["redacted_summary"], "DeferredIntentAction.redacted_summary"),
            constraints=tuple(constraint_from_dict(item) for item in constraints_raw),
        )
    except (InvariantError, KeyError, TypeError, ValueError) as exc:
        raise DeferredIntentActionShapeError(str(exc)) from exc


def deferred_intent_management_action_from_dict(value: object) -> DeferredIntentManagementAction:
    """Decode one exact cancel/edit action with no open-ended payload bag."""

    try:
        if type(value) is not dict:
            raise InvariantError("deferred intent management action must be an exact dict")
        action = value.get("action")
        if action == "cancel":
            if set(value) != {"action", "intent_id", "selection_token"}:
                raise InvariantError("deferred intent cancel action has an invalid exact keyset")
            return DeferredIntentCancelAction(
                intent_id=_canonical_uuid_text(value["intent_id"], "deferred intent cancel intent_id"),
                selection_token=_require_nonempty_exact_str(value["selection_token"], "deferred intent cancel selection_token"),
            )
        if action == "edit":
            if set(value) != {"action", "intent_id", "selection_token", "replacement"}:
                raise InvariantError("deferred intent edit action has an invalid exact keyset")
            return DeferredIntentEditAction(
                intent_id=_canonical_uuid_text(value["intent_id"], "deferred intent edit intent_id"),
                selection_token=_require_nonempty_exact_str(value["selection_token"], "deferred intent edit selection_token"),
                replacement=deferred_intent_action_from_dict(value["replacement"]),
            )
        raise InvariantError("deferred intent management action has an unsupported discriminator")
    except (DeferredIntentActionShapeError, InvariantError, KeyError, TypeError, ValueError) as exc:
        raise DeferredIntentManagementActionShapeError(str(exc)) from exc


def _subject_stage(subject: StableSubject | PluginSubject) -> StageName:
    if type(subject) is StableSubject:
        return _COMPONENT_STAGE[subject.component_kind]
    if type(subject) is PluginSubject:
        return _PLUGIN_STAGE[subject.plugin_kind]
    raise InvariantError("DeferredIntentAction constraint subject is malformed")


def _constraint_stage(constraint: DeferredConstraint) -> StageName:
    if type(constraint) is SubjectPresenceConstraint:
        return _subject_stage(constraint.subject)
    if type(constraint) is OptionValueConstraint:
        return _subject_stage(constraint.subject)
    if type(constraint) is ComponentCountConstraint:
        return _COMPONENT_STAGE[constraint.component_kind]
    if type(constraint) is StatedPredicateConstraint:
        return "topology"
    if type(constraint) is StatedGateRoutingConstraint:
        return "topology"
    if type(constraint) is EdgeRouteConstraint:
        return "topology" if constraint.edge_type in {"route_true", "route_false", "fork"} else "wire_review"
    if type(constraint) is FailureRouteConstraint:
        return "wire_review" if constraint.target != "discard" else _subject_stage(constraint.subject)
    raise InvariantError("DeferredIntentAction constraint is malformed")


def _plugin_identities(action: DeferredIntentAction) -> tuple[tuple[PluginKind, str], ...]:
    identities: list[tuple[PluginKind, str]] = []
    if action.catalog_kind is not None and action.catalog_name is not None:
        identities.append((action.catalog_kind, action.catalog_name))

    def add_subject(subject: StableSubject | PluginSubject) -> None:
        if type(subject) is PluginSubject:
            identities.append((subject.plugin_kind, subject.plugin_name))

    for constraint in action.constraints:
        if type(constraint) is SubjectPresenceConstraint or type(constraint) is OptionValueConstraint:
            add_subject(constraint.subject)
        elif type(constraint) is ComponentCountConstraint:
            if constraint.plugin_kind is not None and constraint.plugin_name is not None:
                identities.append((constraint.plugin_kind, constraint.plugin_name))
        elif type(constraint) is StatedPredicateConstraint or type(constraint) is StatedGateRoutingConstraint:
            add_subject(constraint.subject)
        elif type(constraint) is EdgeRouteConstraint:
            add_subject(constraint.from_subject)
            add_subject(constraint.to_subject)
        elif type(constraint) is FailureRouteConstraint:
            add_subject(constraint.subject)
            if constraint.target != "discard":
                add_subject(constraint.target)
    return tuple(dict.fromkeys(identities))


def _validate_catalog_identity(
    catalog: PolicyCatalogView,
    *,
    plugin_kind: PluginKind,
    plugin_name: str,
) -> DeferredIntentClarification | DeferredIntentUnsupported | DeferredIntentRejected | None:
    try:
        plugin_id = PluginId(plugin_kind, plugin_name)
    except ValueError:
        return DeferredIntentRejected(reason="malformed_catalog_identity")
    reason = catalog.unavailable_reason(plugin_id)
    if reason is not None and reason is not PluginUnavailableReason.NOT_INSTALLED:
        return DeferredIntentUnsupported(plugin_kind=plugin_kind, plugin_name=plugin_name, reason=reason)
    resolution = resolve_catalog_subject(catalog, plugin_name=plugin_name, expected_kind=plugin_kind)
    if type(resolution) is CatalogSubjectClarification:
        return DeferredIntentClarification(plugin_name=plugin_name, plugin_kinds=resolution.plugin_kinds)
    if type(resolution) is CatalogSubjectResolved:
        if resolution.plugin_kind != plugin_kind:
            return DeferredIntentRejected(reason="catalog_kind_mismatch")
        return None
    if type(resolution) is CatalogSubjectUnsupported and resolution.visible_kinds:
        return DeferredIntentRejected(reason="catalog_kind_mismatch")
    if reason is None:
        raise InvariantError("policy catalog returned unsupported for an available plugin identity")
    return DeferredIntentUnsupported(plugin_kind=plugin_kind, plugin_name=plugin_name, reason=reason)


def _stable_option_plugin_identity(subject: StableSubject, guided: GuidedSession) -> tuple[PluginKind, str] | None:
    if subject.component_kind == "source":
        reviewed_source = guided.reviewed_sources.get(subject.stable_id)
        return ("source", reviewed_source.plugin) if reviewed_source is not None else None
    if subject.component_kind == "output":
        reviewed_output = guided.reviewed_outputs.get(subject.stable_id)
        return ("sink", reviewed_output.plugin) if reviewed_output is not None else None
    return None


type _SchemaNode = dict[str, object] | bool
type _FiniteScalarDomain = tuple[object, ...]


class _ResolvedLookup(Protocol):
    @property
    def contents(self) -> object: ...

    @property
    def resolver(self) -> _SchemaResolver: ...


class _SchemaResolver(Protocol):
    def lookup(self, reference: str) -> _ResolvedLookup: ...

    def in_subresource(self, subresource: Resource[_SchemaNode]) -> _SchemaResolver: ...


@dataclass(frozen=True, slots=True)
class _ResolvedSchemaNode:
    schema: _SchemaNode
    resolver: _SchemaResolver


_SCHEMA_ANNOTATION_KEYS = frozenset(
    {
        "$comment",
        "default",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)
_MISSING_SCHEMA_KEY = object()


def _schema_resource(schema: _SchemaNode) -> Resource[_SchemaNode]:
    return Resource.from_contents(schema, default_specification=DRAFT202012)


def _root_schema_context(root: dict[str, object]) -> _ResolvedSchemaNode:
    resource = _schema_resource(root)
    return _ResolvedSchemaNode(schema=root, resolver=Registry[_SchemaNode]().resolver_with_root(resource))


def _resolve_schema_ref(context: _ResolvedSchemaNode, reference: object) -> _ResolvedSchemaNode:
    if type(reference) is not str:
        raise InvariantError("plugin option schema has a malformed $ref")
    try:
        resolved = context.resolver.lookup(reference)
    except (Unresolvable, ValueError) as exc:
        raise InvariantError("plugin option schema has a dangling or unsupported $ref") from exc
    target = resolved.contents
    if type(target) not in {dict, bool}:
        raise InvariantError("plugin option schema $ref does not target a schema")
    return _ResolvedSchemaNode(schema=cast(_SchemaNode, target), resolver=resolved.resolver)


def _preflight_schema_refs(root: _ResolvedSchemaNode) -> None:
    """Resolve every supported reference without consulting a proposed value.

    Draft 2020-12 has two reference applicators.  This retained-literal
    authority boundary supports ``$ref`` within the root resource registry and
    its discovered subresources.  It rejects ``$dynamicRef`` because proving
    dynamic-scope behavior is outside this boundary's deliberately closed
    authority model.
    """

    completed: set[int] = set()

    def walk(context: _ResolvedSchemaNode, *, active_nodes: frozenset[int]) -> None:
        node = context.schema
        if type(node) is bool:
            return
        node_identity = id(node)
        if node_identity in active_nodes:
            raise InvariantError("plugin option schema has a cyclic $ref")
        if node_identity in completed:
            return
        if "$dynamicRef" in node:
            raise InvariantError("plugin option schema uses unsupported $dynamicRef authority")
        descendants = active_nodes | {node_identity}
        if "$ref" in node:
            walk(_resolve_schema_ref(context, node["$ref"]), active_nodes=descendants)
        for subresource in _schema_resource(node).subresources():
            walk(
                _ResolvedSchemaNode(
                    schema=subresource.contents,
                    resolver=context.resolver.in_subresource(subresource),
                ),
                active_nodes=descendants,
            )
        completed.add(node_identity)

    walk(root, active_nodes=frozenset())


def _dereference_path_schema(context: _ResolvedSchemaNode) -> _ResolvedSchemaNode | None:
    current = context
    seen: set[int] = set()
    while type(current.schema) is dict and "$ref" in current.schema:
        semantic_siblings = set(current.schema) - _SCHEMA_ANNOTATION_KEYS - {"$ref"}
        if semantic_siblings:
            return None
        current = _resolve_schema_ref(current, current.schema["$ref"])
        identity = id(current.schema)
        if identity in seen:
            raise InvariantError("plugin option schema has a cyclic $ref")
        seen.add(identity)
    return current


def _subschema_context(context: _ResolvedSchemaNode, schema: object) -> _ResolvedSchemaNode:
    if type(schema) not in {dict, bool}:
        raise InvariantError("plugin option schema child does not contain a schema")
    subresource = _schema_resource(cast(_SchemaNode, schema))
    return _ResolvedSchemaNode(schema=subresource.contents, resolver=context.resolver.in_subresource(subresource))


def _option_schema_node(root: _ResolvedSchemaNode, option_path: tuple[str, ...]) -> _ResolvedSchemaNode | None:
    current = root
    for segment in option_path:
        resolved = _dereference_path_schema(current)
        if resolved is None:
            return None
        if type(resolved.schema) is bool:
            return None
        properties = resolved.schema.get("properties", _MISSING_SCHEMA_KEY)
        if properties is _MISSING_SCHEMA_KEY:
            return None
        if type(properties) is not dict:
            raise InvariantError("plugin option schema properties declaration is malformed")
        if segment not in properties:
            return None
        current = _subschema_context(resolved, properties[segment])
    return current


def _exact_json_scalar(left: object, right: object) -> bool:
    return type(left) is type(right) and left == right


_PREDICATE_OPERATOR_BY_AST: dict[type[ast.cmpop], str] = {
    ast.Eq: "equals",
    ast.NotEq: "not_equals",
    ast.Gt: "greater_than",
    ast.GtE: "greater_than_or_equal",
    ast.Lt: "less_than",
    ast.LtE: "less_than_or_equal",
}
_REVERSED_PREDICATE_OPERATOR: dict[str, str] = {
    "equals": "equals",
    "not_equals": "not_equals",
    "greater_than": "less_than",
    "greater_than_or_equal": "less_than_or_equal",
    "less_than": "greater_than",
    "less_than_or_equal": "greater_than_or_equal",
}
_MESSAGE_OPERATOR_PATTERN: dict[str, str] = {
    "equals": r"(?:==|(?<![!<>])=(?!=)|\bequals?\b|\bequal\s+to\b)",
    "not_equals": r"(?:!=|\bdoes\s+not\s+equal\b|\bnot\s+equal\s+to\b|\bis\s+not\b)",
    "greater_than": r"(?:>(?!=)|\bgreater\s+than\b(?!\s+or\s+equal)|\bmore\s+than\b|\babove\b|\bover\b)",
    "greater_than_or_equal": r"(?:>=|\bgreater\s+than\s+or\s+equal\s+to\b|\bat\s+least\b|\bno\s+less\s+than\b)",
    "less_than": r"(?:<(?!=)|\bless\s+than\b(?!\s+or\s+equal)|\bbelow\b|\bunder\b)",
    "less_than_or_equal": r"(?:<=|\bless\s+than\s+or\s+equal\s+to\b|\bat\s+most\b|\bno\s+more\s+than\b)",
}
_FALSE_ROUTE_MARKER = re.compile(
    r"\b(?:(?:everything|anything|all)\s+else|every\s+other\s+rows?|(?:the\s+)?rest(?:\s+of\s+(?:the\s+)?rows?)?|"
    r"otherwise|remaining\s+rows?|false(?:\s+branch)?)\b",
    re.IGNORECASE,
)
_UNREPRESENTED_NEGATION = re.compile(
    r"\b(?:no|never|without|except|unless|instead|neither|nor|not|avoid(?:s|ed|ing)?|skip(?:s|ped|ping)?|"
    r"prohibit(?:s|ed|ing)?|prevent(?:s|ed|ing)?|forbid(?:s|den|ding)?|exclud(?:e[sd]?|ing))\b|"
    r"\b(?:cannot|can['\u2019]t|won['\u2019]t)\b|"
    r"\b(?:do|does|did|should|must|would|could|is|are|was|were|has|have|had)n['\u2019]t\b",
    re.IGNORECASE,
)
_GENERIC_ROUTE_DESTINATION = re.compile(
    r"\b(?:(?:to|into)|(?:go(?:es)?|land(?:s|ing)?|route[sd]?|send[sd]?)\s+(?:to|into|in))\s+"
    r"(?:(?:a|the)\s+)?[a-z0-9_][a-z0-9_-]*\b",
    re.IGNORECASE,
)
_GATE_OR_ROUTE_WORD = re.compile(
    r"\b(?:gate|route|routes|routed|routing|send|sends|sent|split|splits|divert|diverts|separate|separates|land|lands|landing)\b",
    re.IGNORECASE,
)
_GATE_WORD = re.compile(r"\bgate\b", re.IGNORECASE)
_CONDITIONAL_ROUTING_MARKER = re.compile(r"\b(?:where|whose|which|when|if|with)\b", re.IGNORECASE)
_STATED_CLAUSE_BOUNDARY = re.compile(
    r"\.(?!\d)|[;:!?\n]|\band\b|\bthen\b|\bbut\b|\bwhile\b|\balso\b",
    re.IGNORECASE,
)
_STATED_COMMAND_BOUNDARY = re.compile(r"\.(?!\d)|[!?\n]", re.IGNORECASE)
_STATED_THRESHOLD_NUMBER = r"\$?\d+(?:[.,]\d+)*(?!\d)"
_STATED_THRESHOLD_UNIT_NOUN = (
    r"(?!\s*(?:%|(?:words?|characters?|chars?|rows?|records?|branch|branches|sinks?|nodes?|tokens?|"
    r"seconds?|secs?|minutes?|ms|milliseconds?|times?|items?|entries|columns?|fields?)\b))"
)
_STATED_THRESHOLD_QUANTITY = _STATED_THRESHOLD_NUMBER + _STATED_THRESHOLD_UNIT_NOUN
_STATED_THRESHOLD_OPERATOR = r"(?<![-=<>!])(?:>=|<=|==|>|<)"
_STATED_THRESHOLD_WORDING = (
    r"(?:greater than|less than|more than|fewer than|at least|at most|no more than|no less than|above|below|over|under)"
)
_STATED_THRESHOLD_PATTERN = re.compile(
    rf"[A-Za-z_]\w*\s*{_STATED_THRESHOLD_OPERATOR}\s*{_STATED_THRESHOLD_QUANTITY}"
    rf"|{_STATED_THRESHOLD_NUMBER}\s*{_STATED_THRESHOLD_OPERATOR}\s*[A-Za-z_]\w*"
    rf"|{_STATED_THRESHOLD_WORDING}\s+{_STATED_THRESHOLD_QUANTITY}",
    re.IGNORECASE,
)
_STATED_UNIT_AFTER_LITERAL = re.compile(
    r"^\s*(?:%|words?|characters?|chars?|rows?|records?|branch|branches|sinks?|nodes?|tokens?|seconds?|secs?|"
    r"minutes?|ms|milliseconds?|times?|items?|entries|columns?|fields?)\b",
    re.IGNORECASE,
)
_STATED_COMPARISON_LITERAL = re.compile(
    r"\s*(?:[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|true\b|false\b|null\b|none\b|"
    r'"[^"\r\n]{1,128}"|\'[^\'\r\n]{1,128}\'|[A-Za-z_][A-Za-z0-9_.-]*)',
    re.IGNORECASE,
)
_AFFIRMATIVE_STATED_PREFIX = re.compile(
    r"^\s*(?:please\s+)?(?:later(?:\s+on)?[\s,]+)?"
    r"(?:(?:i|we)\s+(?:want|need|require)\s+(?:to\s+)?)?"
    r"(?:(?:add|apply|use|create|make|have)\s+(?:(?:a|the)\s+)?gate|a\s+gate|route|send|split|divert|separate|gate)"
    r"(?:\s+(?:that|which)\s+(?:route[sd]?|send[sd]?|split[sd]?|divert[sd]?|separate[sd]?))?"
    r"\s+(?:"
    r"(?:(?:to|for)\s+)?(?:(?:the|all|each|every)\s+)?"
    r"(?:(?P<subject_before_rows>(?!rows?\b)[A-Za-z0-9_-]+)\s+(?:rows?\s+)?|rows?\s+)?(?:where|whose|with)"
    r"|(?:where|whose|with)\s+(?:(?:the|all|each|every)\s+)?(?:(?P<subject_after_connector>[A-Za-z0-9_-]+)\s+)?"
    r")\s*$",
    re.IGNORECASE,
)


def _message_token_pattern(value: str) -> str:
    return rf"(?<![A-Za-z0-9_-]){re.escape(value)}(?![A-Za-z0-9_-])"


def _stated_preceding_context_is_benign(
    context: str,
    constraint: StatedPredicateConstraint | StatedGateRoutingConstraint,
) -> bool:
    """Permit only one closed source-description sentence before a command."""

    if not context.strip():
        return True
    subject = constraint.subject
    if type(subject) is not PluginSubject:
        return False
    description = re.compile(
        r"^\s*(?:this|it)\s+is\s+(?:(?:a|an|the)\s+)?"
        r"(?:[A-Za-z0-9_-]+\s+){0,2}" + _message_token_pattern(subject.plugin_name) + r"(?:\s+(?:source|input|file|data))?\s*\.\s*$",
        re.IGNORECASE,
    )
    return description.fullmatch(context) is not None


def _stated_predicate_message_match(
    message: str,
    constraint: StatedPredicateConstraint | StatedGateRoutingConstraint,
) -> re.Match[str] | None:
    prefix = _message_token_pattern(constraint.column) + r"[\s\S]{0,80}?" + _MESSAGE_OPERATOR_PATTERN[constraint.operator] + r"\s*"
    value = constraint.value
    if type(value) in {int, float}:
        pattern = re.compile(
            prefix + r"(?P<literal>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)",
            re.IGNORECASE,
        )
        expected = Decimal(str(value))
        for match in pattern.finditer(message):
            try:
                if Decimal(match.group("literal")) == expected:
                    return match
            except InvalidOperation:  # pragma: no cover - regex admits only Decimal syntax
                continue
        return None
    if type(value) is bool:
        literal_pattern = rf"\b{str(value).lower()}\b"
    elif value is None:
        literal_pattern = r"\b(?:null|none)\b"
    else:
        literal_pattern = _message_token_pattern(cast(str, value))
    return re.search(prefix + literal_pattern, message, re.IGNORECASE)


def _stated_constraint_is_grounded(
    message: str,
    constraint: StatedPredicateConstraint | StatedGateRoutingConstraint,
    *,
    guided: GuidedSession | None = None,
) -> bool:
    predicate = _stated_predicate_message_match(message, constraint)
    if predicate is None:
        return False
    clause_start = 0
    clause_end = len(message)
    # Qualifiers before ``and``/``then``/``;`` remain part of the command.
    # Only a completed sentence may start a fresh affirmative authority span.
    for boundary in _STATED_COMMAND_BOUNDARY.finditer(message):
        if boundary.end() <= predicate.start():
            clause_start = boundary.end()
            continue
        if boundary.start() >= predicate.end():
            clause_end = boundary.start()
            break
    if not _stated_preceding_context_is_benign(message[:clause_start], constraint):
        return False
    predicate_prefix = message[clause_start : predicate.start()]
    # Only promote a stated fact from a closed affirmative command grammar.
    # Free-form text before the predicate can carry approval, review, or other
    # preconditions that the closed constraint tuple cannot represent.
    prefix_match = _AFFIRMATIVE_STATED_PREFIX.fullmatch(predicate_prefix)
    if prefix_match is None:
        return False
    explicit_subjects = tuple(
        value for value in (prefix_match.group("subject_before_rows"), prefix_match.group("subject_after_connector")) if value is not None
    )
    subject = constraint.subject
    if explicit_subjects:
        if type(subject) is PluginSubject:
            allowed_subject_tokens = {subject.plugin_name.casefold()}
        elif type(subject) is StableSubject and guided is not None:
            if subject.component_kind == "source":
                source_component = guided.reviewed_sources.get(subject.stable_id) or guided.pending_source_intents.get(subject.stable_id)
                component_identity = (source_component.name, source_component.plugin) if source_component is not None else None
            elif subject.component_kind == "output":
                output_component = guided.reviewed_outputs.get(subject.stable_id) or guided.pending_output_intents.get(subject.stable_id)
                component_identity = (output_component.name, output_component.plugin) if output_component is not None else None
            else:
                component_identity = None
            allowed_subject_tokens = (
                {component_identity[0].casefold(), component_identity[1].casefold()}
                if component_identity is not None and component_identity[1] is not None
                else {component_identity[0].casefold()}
                if component_identity is not None
                else set()
            )
        else:
            allowed_subject_tokens = set()
        if any(value.casefold() not in allowed_subject_tokens for value in explicit_subjects):
            return False
    if _GATE_OR_ROUTE_WORD.search(message[clause_start:clause_end]) is None:
        return False
    if type(constraint.value) in {int, float} and _STATED_UNIT_AFTER_LITERAL.search(message[predicate.end() :]) is not None:
        return False
    # The closed tuple has no polarity/exception field.  Reject prose whose
    # remaining words contradict or qualify the matched predicate rather than
    # laundering it into affirmative mandatory authority.
    unrepresented = message[: predicate.start()] + " " + message[predicate.end() :]
    if _UNREPRESENTED_NEGATION.search(unrepresented) is not None:
        return False
    if any(_clause_has_stated_comparison(clause) for clause in _STATED_CLAUSE_BOUNDARY.split(unrepresented)):
        return False
    if type(constraint) is StatedPredicateConstraint:
        # An explicit else/otherwise clause is a routing obligation, not a
        # condition-only statement.  Do not let the solver retain the weaker
        # constraint and thereby omit the operator's branch destinations.
        return _FALSE_ROUTE_MARKER.search(message[predicate.end() :]) is None
    routing_constraint = cast(StatedGateRoutingConstraint, constraint)
    routing_tail = message[predicate.end() :]
    false_marker = _FALSE_ROUTE_MARKER.search(routing_tail)
    if false_marker is None:
        return False
    true_segment = routing_tail[: false_marker.start()]
    false_segment = routing_tail[false_marker.end() :]
    return _message_segment_affirmatively_targets(true_segment, routing_constraint.true_target) and _message_segment_affirmatively_targets(
        false_segment, routing_constraint.false_target
    )


def _message_segment_affirmatively_targets(segment: str, target: str) -> bool:
    if _UNREPRESENTED_NEGATION.search(segment) is not None:
        return False
    destination = re.compile(
        r"^\s*(?:(?:(?:go(?:es)?|land(?:s|ing)?|route[sd]?|send(?:s|ing)?|sent)"
        r"(?:\s+(?:them|rows?))?\s+(?:to|into|in))|(?:to|into))\s+"
        r"(?:(?:a|the)\s+)?" + _message_token_pattern(target) + r"(?:\s+(?:json\s+)?sink)?\s*(?:(?:,\s*and|[.;])\s*)?"
        r"(?:Every\s+row\s+must\s+land\s+in\s+exactly\s+one\s+of\s+them\.)?\s*$",
        re.IGNORECASE,
    )
    return destination.fullmatch(segment) is not None


def _message_requires_stated_constraint(message: str) -> Literal["predicate", "routing"] | None:
    """Classify explicit gate prose that weaker constraint kinds cannot encode."""

    false_marker = _FALSE_ROUTE_MARKER.search(message)
    routing_word_present = _GATE_OR_ROUTE_WORD.search(message) is not None
    destination_count = len(_GENERIC_ROUTE_DESTINATION.findall(message))
    if destination_count and _GATE_WORD.search(message) is not None:
        return "routing"
    if routing_word_present and (false_marker is not None or destination_count >= 2):
        # Two-way routing is stronger than every pre-existing deferred
        # constraint kind regardless of how the operator phrases the
        # predicate.  Unsupported comparison prose must clarify, never fall
        # back to a weaker fact that can be claimed by a zero-gate pipeline.
        return "routing"
    if routing_word_present and _CONDITIONAL_ROUTING_MARKER.search(message) is not None:
        return "routing" if destination_count else "predicate"
    routing_comparison_present = any(
        _GATE_OR_ROUTE_WORD.search(clause) is not None and _clause_has_stated_comparison(clause)
        for clause in _STATED_CLAUSE_BOUNDARY.split(message)
    )
    if not routing_comparison_present:
        return None
    return "routing" if destination_count else "predicate"


def _clause_has_stated_comparison(clause: str) -> bool:
    if _STATED_THRESHOLD_PATTERN.search(clause) is not None:
        return True
    for operator_pattern in _MESSAGE_OPERATOR_PATTERN.values():
        for operator in re.finditer(operator_pattern, clause, re.IGNORECASE):
            literal = _STATED_COMPARISON_LITERAL.match(clause, operator.end())
            if literal is None:
                continue
            if _STATED_UNIT_AFTER_LITERAL.search(clause[literal.end() :]) is None:
                return True
    return False


def _stated_subject_is_grounded(
    message: str,
    constraint: StatedPredicateConstraint | StatedGateRoutingConstraint,
    guided: GuidedSession,
) -> bool:
    """Bind a stated predicate to current component or explicit plugin authority."""

    predicate = _stated_predicate_message_match(message, constraint)
    if predicate is None:
        return False
    subject_context_start = 0
    for boundary in _STATED_CLAUSE_BOUNDARY.finditer(message):
        if boundary.end() > predicate.start():
            break
        subject_context_start = boundary.end()
    subject_context = message[subject_context_start : predicate.end()]

    subject = constraint.subject
    if type(subject) is StableSubject:
        live_components: dict[str, tuple[str, str | None]]
        if subject.component_kind == "source":
            live_components = {stable_id: (source.name, source.plugin) for stable_id, source in guided.reviewed_sources.items()}
            live_components.update((stable_id, (intent.name, intent.plugin)) for stable_id, intent in guided.pending_source_intents.items())
        elif subject.component_kind == "output":
            live_components = {stable_id: (output.name, output.plugin) for stable_id, output in guided.reviewed_outputs.items()}
            live_components.update((stable_id, (intent.name, intent.plugin)) for stable_id, intent in guided.pending_output_intents.items())
        else:
            return False
        identity = live_components.get(subject.stable_id)
        if identity is None:
            return False
        if len(live_components) == 1:
            return True
        component_name, plugin_name = identity
        if re.search(_message_token_pattern(component_name), subject_context, re.IGNORECASE) is not None:
            return True
        return (
            plugin_name is not None
            and sum(candidate_plugin == plugin_name for _, candidate_plugin in live_components.values()) == 1
            and re.search(_message_token_pattern(plugin_name), subject_context, re.IGNORECASE) is not None
        )

    plugin_subject = cast(PluginSubject, subject)
    if plugin_subject.plugin_kind == "source":
        live_plugins = {stable_id: source.plugin for stable_id, source in guided.reviewed_sources.items()}
        live_plugins.update(
            (stable_id, intent.plugin) for stable_id, intent in guided.pending_source_intents.items() if intent.plugin is not None
        )
    elif plugin_subject.plugin_kind == "sink":
        live_plugins = {stable_id: output.plugin for stable_id, output in guided.reviewed_outputs.items()}
        live_plugins.update(
            (stable_id, intent.plugin) for stable_id, intent in guided.pending_output_intents.items() if intent.plugin is not None
        )
    else:
        live_plugins = {}
    matching_ids = [stable_id for stable_id, plugin_name in live_plugins.items() if plugin_name == plugin_subject.plugin_name]
    if live_plugins.get(plugin_subject.subject_id) == plugin_subject.plugin_name:
        resolved_id = plugin_subject.subject_id
    elif len(matching_ids) == 1:
        resolved_id = matching_ids[0]
    elif matching_ids:
        return False
    else:
        plugin_context = subject_context if live_plugins else message
        return re.search(_message_token_pattern(plugin_subject.plugin_name), plugin_context, re.IGNORECASE) is not None
    if len(live_plugins) == 1:
        return True
    if plugin_subject.plugin_kind == "source":
        component_name = (
            guided.reviewed_sources[resolved_id].name
            if resolved_id in guided.reviewed_sources
            else guided.pending_source_intents[resolved_id].name
        )
    elif plugin_subject.plugin_kind == "sink":
        component_name = (
            guided.reviewed_outputs[resolved_id].name
            if resolved_id in guided.reviewed_outputs
            else guided.pending_output_intents[resolved_id].name
        )
    else:  # pragma: no cover - transform subjects have no live guided component map
        return False
    if re.search(_message_token_pattern(component_name), subject_context, re.IGNORECASE) is not None:
        return True
    return (
        len(matching_ids) == 1 and re.search(_message_token_pattern(plugin_subject.plugin_name), subject_context, re.IGNORECASE) is not None
    )


def _row_column(node: ast.expr) -> str | None:
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "row"
        and isinstance(node.slice, ast.Constant)
        and type(node.slice.value) is str
        and node.slice.value
    ):
        return node.slice.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "row"
        and node.func.attr == "get"
        and len(node.args) == 1
        and not node.keywords
        and isinstance(node.args[0], ast.Constant)
        and type(node.args[0].value) is str
        and node.args[0].value
    ):
        return node.args[0].value
    return None


def _json_literal(node: ast.expr) -> tuple[bool, object]:
    if isinstance(node, ast.Constant) and type(node.value) in {str, int, float, bool, type(None)}:
        return True, node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub))
        and isinstance(node.operand, ast.Constant)
        and type(node.operand.value) in {int, float}
    ):
        numeric = cast(int | float, node.operand.value)
        value = numeric if isinstance(node.op, ast.UAdd) else -numeric
        return True, value
    return False, None


def _gate_condition_matches_stated_predicate(
    condition: str | None,
    constraint: StatedPredicateConstraint | StatedGateRoutingConstraint,
) -> bool:
    if type(condition) is not str:
        return False
    try:
        ExpressionParser(condition)
        body = ast.parse(condition, mode="eval").body
    except (ExpressionSecurityError, ExpressionSyntaxError, SyntaxError, ValueError):
        return False
    if not isinstance(body, ast.Compare) or len(body.ops) != 1 or len(body.comparators) != 1:
        return False
    operator = _PREDICATE_OPERATOR_BY_AST.get(type(body.ops[0]))
    if operator is None:
        return False
    left_column = _row_column(body.left)
    right_column = _row_column(body.comparators[0])
    if left_column is not None and right_column is None:
        literal_present, literal = _json_literal(body.comparators[0])
        column = left_column
    elif right_column is not None and left_column is None:
        literal_present, literal = _json_literal(body.left)
        column = right_column
        operator = _REVERSED_PREDICATE_OPERATOR[operator]
    else:
        return False
    return (
        literal_present
        and column == constraint.column
        and operator == constraint.operator
        and _exact_json_scalar(literal, constraint.value)
    )


def _is_exact_json_scalar(value: object) -> bool:
    if type(value) not in {str, int, float, bool, type(None)}:
        return False
    try:
        canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise InvariantError("plugin option schema contains a non-canonical JSON scalar") from exc
    return True


def _append_exact_scalar(domain: list[object], value: object) -> None:
    if any(_exact_json_scalar(existing, value) for existing in domain):
        return
    domain.append(value)


def _union_finite_domains(domains: tuple[_FiniteScalarDomain, ...]) -> _FiniteScalarDomain:
    union: list[object] = []
    for domain in domains:
        for value in domain:
            _append_exact_scalar(union, value)
    return tuple(union)


def _intersect_finite_domains(domains: tuple[_FiniteScalarDomain, ...]) -> _FiniteScalarDomain:
    return tuple(
        value for value in domains[0] if all(any(_exact_json_scalar(value, candidate) for candidate in domain) for domain in domains[1:])
    )


def _finite_type_domain(declared: object) -> _FiniteScalarDomain | None:
    if type(declared) is str:
        names = (declared,)
    elif type(declared) is list:
        names = tuple(declared)
    else:
        return None
    if any(name not in {"boolean", "null"} for name in names):
        return None
    domain: list[object] = []
    if "boolean" in names:
        domain.extend((False, True))
    if "null" in names:
        domain.append(None)
    return tuple(domain)


def _finite_scalar_domain(
    context: _ResolvedSchemaNode,
    *,
    active_refs: frozenset[int] = frozenset(),
) -> _FiniteScalarDomain | None:
    node = context.schema
    if type(node) is bool:
        return () if node is False else None
    candidates: list[_FiniteScalarDomain] = []

    if "$ref" in node:
        referenced = _resolve_schema_ref(context, node["$ref"])
        referenced_identity = id(referenced.schema)
        if referenced_identity in active_refs:
            raise InvariantError("plugin option schema has a cyclic $ref")
        referenced_domain = _finite_scalar_domain(referenced, active_refs=active_refs | {referenced_identity})
        if referenced_domain is not None:
            candidates.append(referenced_domain)

    for union_keyword in ("anyOf", "oneOf"):
        if union_keyword not in node:
            continue
        branches = node[union_keyword]
        if type(branches) is not list:  # pragma: no cover - Draft 2020-12 meta-validation owns this guard
            raise InvariantError(f"plugin option schema {union_keyword} declaration is malformed")
        branch_domains: list[_FiniteScalarDomain] = []
        for branch in branches:
            if type(branch) not in {dict, bool}:  # pragma: no cover - Draft 2020-12 meta-validation owns this guard
                raise InvariantError(f"plugin option schema {union_keyword} branch is malformed")
            branch_context = _subschema_context(context, branch)
            branch_domain = _finite_scalar_domain(branch_context, active_refs=active_refs)
            if branch_domain is None:
                return None
            branch_domains.append(branch_domain)
        candidates.append(_union_finite_domains(tuple(branch_domains)))

    if "allOf" in node:
        branches = node["allOf"]
        if type(branches) is not list:  # pragma: no cover - Draft 2020-12 meta-validation owns this guard
            raise InvariantError("plugin option schema allOf declaration is malformed")
        finite_branches: list[_FiniteScalarDomain] = []
        for branch in branches:
            if type(branch) not in {dict, bool}:  # pragma: no cover - Draft 2020-12 meta-validation owns this guard
                raise InvariantError("plugin option schema allOf branch is malformed")
            branch_context = _subschema_context(context, branch)
            branch_domain = _finite_scalar_domain(branch_context, active_refs=active_refs)
            if branch_domain is not None:
                finite_branches.append(branch_domain)
        if finite_branches:
            candidates.append(_intersect_finite_domains(tuple(finite_branches)))

    if "const" in node:
        constant = node["const"]
        if not _is_exact_json_scalar(constant):
            return None
        candidates.append((constant,))

    if "enum" in node:
        enum = node["enum"]
        if type(enum) is not list or not enum:
            raise InvariantError("plugin option schema enum declaration is malformed")
        enum_domain: list[object] = []
        for item in enum:
            if not _is_exact_json_scalar(item):
                return None
            _append_exact_scalar(enum_domain, item)
        candidates.append(tuple(enum_domain))

    type_domain = _finite_type_domain(node.get("type"))
    if type_domain is not None:
        candidates.append(type_domain)

    if not candidates:
        return None
    return _intersect_finite_domains(tuple(candidates))


def _schema_proves_closed_literal(
    schema: _ResolvedSchemaNode,
    value: object,
    *,
    validator: Draft202012Validator,
) -> bool:
    domain = _finite_scalar_domain(schema)
    if domain is None or not any(_exact_json_scalar(item, value) for item in domain):
        return False
    try:
        return next(validator.descend(value, schema.schema, resolver=schema.resolver), None) is None
    except (RecursionError, Unresolvable) as exc:
        raise InvariantError("plugin option schema could not resolve during Draft 2020-12 validation") from exc


def _validate_option_value_constraint(
    constraint: OptionValueConstraint,
    *,
    guided: GuidedSession,
    catalog: PolicyCatalogView,
) -> DeferredIntentUnsupported | DeferredIntentRejected | None:
    subject = constraint.subject
    if type(subject) is PluginSubject:
        identity: tuple[PluginKind, str] | None = (subject.plugin_kind, subject.plugin_name)
    elif type(subject) is StableSubject:
        identity = _stable_option_plugin_identity(subject, guided)
    else:  # pragma: no cover - OptionValueConstraint owns the closed subject type
        identity = None
    if identity is None:
        return DeferredIntentRejected(reason="option_value_unproven")
    plugin_kind, plugin_name = identity
    reason = catalog.unavailable_reason(PluginId(plugin_kind, plugin_name))
    if reason is not None:
        return DeferredIntentUnsupported(plugin_kind=plugin_kind, plugin_name=plugin_name, reason=reason)
    schema = catalog.get_schema(plugin_kind, plugin_name)
    if schema.plugin_type != plugin_kind or schema.name != plugin_name:
        raise InvariantError("policy catalog returned a mismatched plugin option schema identity")
    if type(schema.json_schema) is not dict:
        raise InvariantError("policy catalog returned a malformed plugin option schema root")
    root = cast(dict[str, object], schema.json_schema)
    try:
        Draft202012Validator.check_schema(root)
    except SchemaError as exc:
        raise InvariantError("policy catalog returned an invalid Draft 2020-12 plugin option schema") from exc
    root_context = _root_schema_context(root)
    _preflight_schema_refs(root_context)
    validator = Draft202012Validator(root)
    option_schema = _option_schema_node(root_context, constraint.option_path)
    if option_schema is None or not _schema_proves_closed_literal(
        option_schema,
        constraint.value,
        validator=validator,
    ):
        return DeferredIntentRejected(reason="option_value_unproven")
    return None


def validate_deferred_intent_structure(
    action: DeferredIntentAction,
    *,
    receiving_stage: StageName,
) -> DeferredIntentRejected | None:
    """Reject an action whose target or responsible stage is structurally invalid."""

    if type(action) is not DeferredIntentAction:
        raise TypeError("action must be an exact DeferredIntentAction")
    if receiving_stage not in _STAGE_ORDINAL:
        raise InvariantError("receiving_stage is unsupported")
    if _STAGE_ORDINAL[action.target_stage] <= _STAGE_ORDINAL[receiving_stage]:
        return DeferredIntentRejected(reason="target_not_later")

    responsible_stages = [
        *([_PLUGIN_STAGE[action.catalog_kind]] if action.catalog_kind is not None else []),
        *(_constraint_stage(constraint) for constraint in action.constraints),
    ]
    if responsible_stages:
        responsible_stage = max(responsible_stages, key=_STAGE_ORDINAL.__getitem__)
        if action.target_stage != responsible_stage:
            return DeferredIntentRejected(reason="wrong_responsible_stage")
    return None


def validate_deferred_intent_action(
    action: DeferredIntentAction,
    *,
    receiving_stage: StageName,
    catalog: PolicyCatalogView,
    guided: GuidedSession,
    originating_message_content: str | None = None,
) -> DeferredIntentValidation:
    """Validate a typed suggestion against live stage and policy authority."""

    structural_rejection = validate_deferred_intent_structure(action, receiving_stage=receiving_stage)
    if type(guided) is not GuidedSession:
        raise TypeError("guided must be an exact GuidedSession")
    if structural_rejection is not None:
        return structural_rejection

    stated_requirement = (
        _message_requires_stated_constraint(originating_message_content) if type(originating_message_content) is str else None
    )
    stated_types = {type(constraint) for constraint in action.constraints}
    if (stated_requirement == "routing" and StatedGateRoutingConstraint not in stated_types) or (
        stated_requirement == "predicate" and not stated_types.intersection({StatedPredicateConstraint, StatedGateRoutingConstraint})
    ):
        return DeferredIntentRejected(reason="stated_fact_unproven")

    for plugin_kind, plugin_name in _plugin_identities(action):
        invalid = _validate_catalog_identity(catalog, plugin_kind=plugin_kind, plugin_name=plugin_name)
        if invalid is not None:
            return invalid
    for constraint in action.constraints:
        if isinstance(constraint, (StatedPredicateConstraint, StatedGateRoutingConstraint)):
            if (
                type(originating_message_content) is not str
                or not _stated_subject_is_grounded(originating_message_content, constraint, guided)
                or not _stated_constraint_is_grounded(originating_message_content, constraint, guided=guided)
            ):
                return DeferredIntentRejected(reason="stated_fact_unproven")
            if isinstance(constraint, StatedGateRoutingConstraint):
                source_names = {source.name for source in guided.reviewed_sources.values()}
                source_names.update(intent.name for intent in guided.pending_source_intents.values())
                if {constraint.true_target, constraint.false_target} & source_names:
                    return DeferredIntentRejected(reason="stated_fact_unproven")
        if type(constraint) is OptionValueConstraint:
            invalid_option = _validate_option_value_constraint(constraint, guided=guided, catalog=catalog)
            if invalid_option is not None:
                return invalid_option
    return DeferredIntentAccepted(action=action)


type _ComponentKind = Literal["source", "node", "edge", "output"]


@dataclass(frozen=True, slots=True)
class _CandidateComponent:
    kind: _ComponentKind
    stable_id: str
    name: str
    plugin_kind: PluginKind | None = None
    plugin: str | None = None
    options: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        freeze_fields(self, "options")


@dataclass(frozen=True, slots=True)
class _SubjectResolution:
    components: tuple[_CandidateComponent, ...]
    ambiguous: bool = False


@dataclass(frozen=True, slots=True)
class _DeferredCoverageContext:
    candidate: CompositionState
    components: tuple[_CandidateComponent, ...]
    exact_components: Mapping[tuple[_ComponentKind, str], _CandidateComponent]
    consumers: Mapping[str, tuple[ConsumerIdentity, ...]]

    def __post_init__(self) -> None:
        freeze_fields(self, "exact_components", "consumers")

    def resolve(self, subject: StableSubject | PluginSubject) -> _SubjectResolution:
        if type(subject) is StableSubject:
            exact = self.exact_components.get((subject.component_kind, subject.stable_id))
            components = (exact,) if exact is not None else ()
            return _SubjectResolution(components=components)
        plugin_subject = cast(PluginSubject, subject)
        component_kind = cast(
            Literal["source", "node", "output"],
            {"source": "source", "transform": "node", "sink": "output"}[plugin_subject.plugin_kind],
        )
        exact = self.exact_components.get((component_kind, plugin_subject.subject_id))
        matches = tuple(
            component
            for component in self.components
            if component.plugin_kind == plugin_subject.plugin_kind and component.plugin == plugin_subject.plugin_name
        )
        if exact is not None and exact.plugin_kind == plugin_subject.plugin_kind and exact.plugin == plugin_subject.plugin_name:
            return _SubjectResolution(components=(exact,))
        if len(matches) > 1:
            return _SubjectResolution(components=(), ambiguous=True)
        return _SubjectResolution(components=matches)

    @staticmethod
    def option_value(component: _CandidateComponent, path: tuple[str, ...]) -> tuple[bool, Any]:
        value: Any = component.options
        for segment in path:
            if not isinstance(value, Mapping) or segment not in value:
                return False, None
            value = value[segment]
        return True, value

    def route_targets(self, component: _CandidateComponent, edge_type: str) -> set[ConsumerIdentity]:
        connections: set[str]
        if component.kind == "source":
            source = self.candidate.sources[component.name]
            connections = {source.on_success} if edge_type == "on_success" and source.on_success is not None else set()
        elif component.kind != "node":
            connections = set()
        else:
            node = next(item for item in self.candidate.nodes if item.id == component.name)
            if edge_type == "on_success":
                connections = {node.on_success} if node.on_success is not None else set()
            elif edge_type == "on_error":
                connections = {node.on_error} if node.on_error is not None else set()
            elif edge_type in {"route_true", "route_false"}:
                key = "true" if edge_type == "route_true" else "false"
                value = dict(node.routes or {}).get(key)
                connections = {value} if value is not None and value != "fork" else set()
            else:
                connections = set(node.fork_to or ()) if edge_type == "fork" else set()
        return {destination for connection in connections for destination in self.consumers.get(connection, ())}

    def exclusively_reached_gate(self, subject: _CandidateComponent) -> _CandidateComponent | None:
        """Return the first gate on an exclusive success path from ``subject``.

        A later correct gate cannot discharge an earlier fan-out: before the
        stated predicate, each connection must have exactly one consumer and
        every non-gate node must continue through exactly one success edge.
        """

        current = subject
        visited: set[tuple[_ComponentKind, str]] = set()
        while True:
            identity = (current.kind, current.stable_id)
            if identity in visited:
                return None
            visited.add(identity)
            if current.kind == "node":
                node = next(item for item in self.candidate.nodes if item.id == current.name)
                if node.node_type == "gate":
                    return current
                if node.on_error not in {None, node.on_success}:
                    return None
            elif current.kind != "source":
                return None
            successors = self.route_targets(current, "on_success")
            if len(successors) != 1:
                return None
            successor = self.exact_components.get(next(iter(successors)))
            if successor is None:
                return None
            current = successor

    def route_output_name(self, gate: _CandidateComponent, route_label: Literal["true", "false"]) -> str | None:
        """Resolve one branch only when it has one linear path to one output."""

        node = next(item for item in self.candidate.nodes if item.id == gate.name)
        connection = dict(node.routes or {}).get(route_label)
        if connection is None or connection == "fork":
            return None
        visited: set[ConsumerIdentity] = set()
        while True:
            consumers = self.consumers.get(connection, ())
            if len(consumers) != 1:
                return None
            identity = consumers[0]
            if identity in visited:
                return None
            visited.add(identity)
            component = self.exact_components.get(identity)
            if component is None:
                return None
            if component.kind == "output":
                return component.name
            if component.kind != "node":
                return None
            downstream = next(item for item in self.candidate.nodes if item.id == component.name)
            if downstream.node_type == "gate" or downstream.on_success is None or downstream.on_error not in {None, downstream.on_success}:
                return None
            connection = downstream.on_success

    def failure_target(self, component: _CandidateComponent, failure_kind: str) -> str | None:
        if failure_kind == "source_validation":
            return self.candidate.sources[component.name].on_validation_failure
        if failure_kind == "node_error":
            return next(item for item in self.candidate.nodes if item.id == component.name).on_error
        return next(item for item in self.candidate.outputs if item.name == component.name).on_write_failure

    def constraint_holds(self, constraint: DeferredConstraint) -> bool:
        if type(constraint) is SubjectPresenceConstraint:
            resolved = self.resolve(constraint.subject)
            return not resolved.ambiguous and bool(resolved.components) is constraint.present
        if type(constraint) is OptionValueConstraint:
            resolved = self.resolve(constraint.subject)
            if resolved.ambiguous or len(resolved.components) != 1:
                return False
            present, value = self.option_value(resolved.components[0], constraint.option_path)
            if not present:
                return False
            equals = _exact_json_scalar(value, constraint.value)
            return equals if constraint.operator == "equals" else not equals
        if type(constraint) is ComponentCountConstraint:
            count = sum(
                component.kind == constraint.component_kind
                and (
                    constraint.plugin_name is None
                    or (component.plugin_kind == constraint.plugin_kind and component.plugin == constraint.plugin_name)
                )
                for component in self.components
            )
            return {
                "equals": count == constraint.count,
                "at_least": count >= constraint.count,
                "at_most": count <= constraint.count,
            }[constraint.operator]
        if type(constraint) is StatedPredicateConstraint:
            subjects = self.resolve(constraint.subject)
            if subjects.ambiguous or len(subjects.components) != 1:
                return False
            gate = self.exclusively_reached_gate(subjects.components[0])
            return gate is not None and _gate_condition_matches_stated_predicate(
                next(item for item in self.candidate.nodes if item.id == gate.name).condition,
                constraint,
            )
        if type(constraint) is StatedGateRoutingConstraint:
            subjects = self.resolve(constraint.subject)
            if subjects.ambiguous or len(subjects.components) != 1:
                return False
            gate = self.exclusively_reached_gate(subjects.components[0])
            return (
                gate is not None
                and _gate_condition_matches_stated_predicate(
                    next(item for item in self.candidate.nodes if item.id == gate.name).condition,
                    constraint,
                )
                and self.route_output_name(gate, "true") == constraint.true_target
                and self.route_output_name(gate, "false") == constraint.false_target
            )
        if type(constraint) is EdgeRouteConstraint:
            origins = self.resolve(constraint.from_subject)
            destinations = self.resolve(constraint.to_subject)
            if origins.ambiguous or destinations.ambiguous or not origins.components or not destinations.components:
                return False
            targets = {(component.kind, component.stable_id) for component in destinations.components}
            present = any(self.route_targets(origin, constraint.edge_type) & targets for origin in origins.components)
            return present is constraint.present
        if type(constraint) is FailureRouteConstraint:
            subjects = self.resolve(constraint.subject)
            if subjects.ambiguous or len(subjects.components) != 1:
                return False
            if constraint.target == "discard":
                expected_targets = {"discard"}
            else:
                target_resolution = self.resolve(constraint.target)
                if target_resolution.ambiguous or len(target_resolution.components) != 1:
                    return False
                expected_targets = {component.name for component in target_resolution.components}
            actual = {self.failure_target(subject, constraint.failure_kind) for subject in subjects.components}
            equals = actual == expected_targets
            return equals if constraint.operator == "equals" else not equals
        raise InvariantError("deferred intent contains an unsupported constraint")


def _coverage_context(candidate: CompositionState, reviewed_guided: GuidedSession) -> _DeferredCoverageContext:
    source_ids = {source.name: stable_id for stable_id, source in reviewed_guided.reviewed_sources.items()}
    output_ids = {output.name: stable_id for stable_id, output in reviewed_guided.reviewed_outputs.items()}
    components: list[_CandidateComponent] = []
    for name, source in candidate.sources.items():
        components.append(
            _CandidateComponent(
                kind="source",
                stable_id=source_ids.get(name, name),
                name=name,
                plugin_kind="source",
                plugin=source.plugin,
                options=cast(Mapping[str, Any], deep_thaw(source.options)),
            )
        )
    for node in candidate.nodes:
        components.append(
            _CandidateComponent(
                kind="node",
                stable_id=node.id,
                name=node.id,
                plugin_kind="transform" if node.plugin is not None else None,
                plugin=node.plugin,
                options=cast(Mapping[str, Any], deep_thaw(node.options)),
            )
        )
    for edge in candidate.edges:
        components.append(_CandidateComponent(kind="edge", stable_id=edge.id, name=edge.id))
    for output in candidate.outputs:
        components.append(
            _CandidateComponent(
                kind="output",
                stable_id=output_ids.get(output.name, output.name),
                name=output.name,
                plugin_kind="sink",
                plugin=output.plugin,
                options=cast(Mapping[str, Any], deep_thaw(output.options)),
            )
        )
    component_tuple = tuple(components)
    exact_components = {(component.kind, component.stable_id): component for component in component_tuple}
    if len(exact_components) != len(component_tuple):
        raise InvariantError("guided candidate contains duplicate same-kind stable component identities")
    try:
        consumers = canonical_connection_consumers(
            candidate,
            node_identities={node.id: node.id for node in candidate.nodes},
            output_identities={output.name: output_ids.get(output.name, output.name) for output in candidate.outputs},
        )
    except ValueError as exc:
        raise InvariantError("guided candidate canonical consumer identities are malformed") from exc
    return _DeferredCoverageContext(
        candidate=candidate,
        components=component_tuple,
        exact_components=exact_components,
        consumers=consumers,
    )


def constraint_holds(candidate: CompositionState, reviewed_guided: GuidedSession, constraint: DeferredConstraint) -> bool:
    """Return whether one persisted structural predicate is true of a candidate."""

    if type(candidate) is not CompositionState or type(reviewed_guided) is not GuidedSession:
        raise TypeError("constraint_holds requires exact candidate and reviewed guided authority")
    return _coverage_context(candidate, reviewed_guided).constraint_holds(constraint)


def evaluate_deferred_intent_coverage(
    *,
    candidate: CompositionState,
    reviewed_guided: GuidedSession,
    claimed_intent_ids: tuple[str, ...],
    required_intent_ids: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Prove model claims and return only the verified reviewed-order subset."""

    if type(candidate) is not CompositionState or type(reviewed_guided) is not GuidedSession:
        raise TypeError("deferred coverage requires exact candidate and reviewed guided authority")
    if type(claimed_intent_ids) is not tuple or any(type(intent_id) is not str for intent_id in claimed_intent_ids):
        raise DeferredIntentClaimError("guided proposal claims must be an exact string tuple")
    if type(required_intent_ids) is not tuple or any(type(intent_id) is not str for intent_id in required_intent_ids):
        raise DeferredIntentClaimError("guided required claims must be an exact string tuple")
    if len(set(claimed_intent_ids)) != len(claimed_intent_ids):
        raise DeferredIntentClaimError("guided proposal contained a duplicate deferred intent claim")
    if len(set(required_intent_ids)) != len(required_intent_ids):
        raise DeferredIntentClaimError("guided proposal contained a duplicate required deferred intent id")

    claimed = set(claimed_intent_ids)
    required = set(required_intent_ids)
    known = {intent.intent_id for intent in reviewed_guided.deferred_intents}
    if not required.issubset(known):
        raise DeferredIntentClaimError("guided proposal required an unknown deferred intent")
    if not required.issubset(claimed):
        raise DeferredIntentClaimError("guided proposal omitted required deferred intent coverage")
    context = _coverage_context(candidate, reviewed_guided)
    verified: list[str] = []
    for intent in reviewed_guided.deferred_intents:
        if intent.intent_id not in claimed:
            continue
        if not intent.constraints or not all(context.constraint_holds(constraint) for constraint in intent.constraints):
            raise DeferredIntentClaimError("guided proposal claimed an unproven deferred intent")
        verified.append(intent.intent_id)
    if claimed != set(verified):
        raise DeferredIntentClaimError("guided proposal claimed an unknown deferred intent")
    return tuple(verified)


def create_deferred_stage_intent(
    action: DeferredIntentAction,
    *,
    receiving_stage: StageName,
    intent_id: str,
    originating_message_id: str,
    originating_message_content: str,
    guided: GuidedSession | None = None,
) -> DeferredStageIntent:
    """Create durable state from a server-validated action and private row.

    The model's ``redacted_summary`` is a classification hint, not durable
    text authority.  The stored summary is rendered from closed stage/catalog
    facts so even a model that echoes the user cannot copy raw prose out of the
    private message row.
    """

    if type(action) is not DeferredIntentAction:
        raise TypeError("action must be an exact DeferredIntentAction")
    _require_nonempty_exact_str(originating_message_content, "originating_message_content")
    stated_requirement = _message_requires_stated_constraint(originating_message_content)
    stated_types = {type(constraint) for constraint in action.constraints}
    if (stated_requirement == "routing" and StatedGateRoutingConstraint not in stated_types) or (
        stated_requirement == "predicate" and not stated_types.intersection({StatedPredicateConstraint, StatedGateRoutingConstraint})
    ):
        raise InvariantError("explicit stated gate prose is not represented by a closed stated constraint")
    if any(
        isinstance(constraint, (StatedPredicateConstraint, StatedGateRoutingConstraint))
        and not _stated_constraint_is_grounded(originating_message_content, constraint, guided=guided)
        for constraint in action.constraints
    ):
        raise InvariantError("stated deferred constraint is not grounded in its originating user message")
    subject = (
        f"{action.catalog_kind} plugin {action.catalog_name!r}"
        if action.catalog_kind is not None and action.catalog_name is not None
        else "structural requirement"
    )
    durable_summary = (
        f"Future {action.target_stage.replace('_', ' ')} instruction for {subject}; {len(action.constraints)} structural constraint(s)."
    )
    return DeferredStageIntent.create(
        intent_id=intent_id,
        receiving_stage=receiving_stage,
        target_stage=action.target_stage,
        catalog_kind=action.catalog_kind,
        catalog_name=action.catalog_name,
        redacted_summary=durable_summary,
        originating_message_id=originating_message_id,
        message_content_hash=stable_hash(originating_message_content),
        constraints=action.constraints,
    )


def create_deferred_clarification_intent(
    *,
    receiving_stage: StageName,
    intent_id: str,
    originating_message_id: str,
    originating_message_content: str,
) -> DeferredStageIntent:
    """Retain a structurally unverified future-stage instruction durably.

    Last-resort retention (R2-F15 / elspeth-a96b2f1b0a): when the model cannot
    express the user's future-stage instruction as a well-formed action even
    after the bounded repair turn, the instruction is kept as a constraint-free
    clarification intent instead of being discarded. The empty constraint set
    makes it permanently unclaimable by the planner
    (:func:`evaluate_deferred_intent_coverage` rejects claims on
    constraint-free intents), so it stays visibly pending until the user
    cancels it or edits it into a structural instruction. ``wire_review`` is
    the latest stage and therefore strictly later than every stage that offers
    ``retain_deferred_intent``. The summary is rendered from closed facts only
    — never from user prose; the prose lives solely in the private message row
    this intent binds by id and content hash.
    """

    _require_nonempty_exact_str(originating_message_content, "originating_message_content")
    return DeferredStageIntent.create(
        intent_id=intent_id,
        receiving_stage=receiving_stage,
        target_stage="wire_review",
        catalog_kind=None,
        catalog_name=None,
        redacted_summary=(
            "Future-stage instruction retained without verified structure; needs the target stage and a concrete structural requirement."
        ),
        originating_message_id=originating_message_id,
        message_content_hash=stable_hash(originating_message_content),
        constraints=(),
    )
