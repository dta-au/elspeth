"""Shared option preparation for resolver-free plugin construction probes."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Final, cast

from pydantic_core import ErrorDetails

from elspeth.contracts.blobs_inline import is_widened_blob_ref
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.core.llm_profiles import LLM_PROFILE_PRIVATE_FIELDS
from elspeth.core.secrets import redact_secret_refs_for_validation


class DeferredBlobContractProbe(Exception):
    """A constructor needs blob bytes unavailable to a resolver-free probe."""


def is_inline_content_reference(value: object) -> bool:
    """Recognize an unresolved value only when it is a valid inline marker."""
    marker = is_widened_blob_ref(value)
    return marker is not None and marker.mode == "inline_content"


def is_deferred_blob_error(error: ErrorDetails, errors: list[ErrorDetails]) -> bool:
    """Defer string markers, retaining errors on their siblings and containers.

    Pydantic reports both branches of the queries list-or-mapping union. The
    wrong container branch is irrelevant only when the selected branch contains
    a valid deferred template. Its other errors must still be reported.
    """
    if error["type"] == "string_type" and is_inline_content_reference(error["input"]):
        return True
    location = error["loc"]
    if location == ("queries", "dict[str,QueryDefinition]") and error["type"] == "dict_type" and type(error["input"]) is list:
        selected = "list[QueryDefinition]"
    elif location == ("queries", "list[QueryDefinition]") and error["type"] == "list_type" and type(error["input"]) is dict:
        selected = "dict[str,QueryDefinition]"
    else:
        return False
    return any(
        detail["loc"][:2] == ("queries", selected) and detail["type"] == "string_type" and is_inline_content_reference(detail["input"])
        for detail in errors
    )


def prepare_query_definition_probe(queries: object) -> object:
    """Detach query declarations and defer only valid file-backed templates.

    Input/output declarations remain available for contract checks. Prompt
    text is checked after authorized materialization; this projection never
    supplies replacement text or changes the saved authoring values.
    """
    prepared = deep_thaw(queries)
    if type(prepared) is dict:
        entries = list(prepared.values())
    elif type(prepared) is list:
        entries = prepared
    else:
        return prepared
    for entry in entries:
        if type(entry) is dict and "template" in entry and is_inline_content_reference(entry["template"]):
            del entry["template"]
    return prepared


_PROBE_PROMPT_FILE_PLACEHOLDER = "validation-probe-prompt-file-placeholder"
"""Inert text standing in for a file-backed llm prompt inside a probe projection."""


def _probe_prompt_file_placeholder(required_input_fields: object, *, reads_row: bool) -> str:
    """Return probe-only text for a file-backed llm prompt position.

    With ``reads_row`` the text reads every declared field by a quoted
    subscript, because the single-prompt config model refuses a declaration
    its template never reads and a read the declaration does not cover; the
    placeholder satisfies both by construction and so decides neither. A
    malformed declaration contributes no read and is refused by its own
    validator. ``repr`` yields a string literal Jinja2 lexes for any name,
    and the names are stripped exactly as that validator strips them.
    """
    if not reads_row or type(required_input_fields) not in (list, tuple):
        return _PROBE_PROMPT_FILE_PLACEHOLDER
    names = cast("list[object] | tuple[object, ...]", required_input_fields)
    reads = [f"{{{{ row[{name.strip()!r}] }}}}" for name in names if type(name) is str and name.strip()]
    return " ".join([_PROBE_PROMPT_FILE_PLACEHOLDER, *reads])


def _substitute_query_template_placeholders(queries: object) -> bool:
    """Replace valid file-backed query templates, in place, in a detached projection.

    A query renders against its own ``input_fields`` bindings, which no rule
    requires its template to read, so the placeholder reads nothing. Returns
    whether any template was replaced.
    """
    if type(queries) is dict:
        entries = list(queries.values())
    elif type(queries) is list:
        entries = queries
    else:
        return False
    substituted = False
    for entry in entries:
        if type(entry) is dict and "template" in entry and is_inline_content_reference(entry["template"]):
            entry["template"] = _PROBE_PROMPT_FILE_PLACEHOLDER
            substituted = True
    return substituted


def _stand_in_llm_prompt_template(prepared: object) -> bool:
    """``prompt_template``: in single-prompt mode the text reads every declared row field."""
    if type(prepared) is not dict or "prompt_template" not in prepared or not is_inline_content_reference(prepared["prompt_template"]):
        return False
    # Single-prompt mode is the only one whose template must read the
    # declared row fields; a query-mode fallback renders query bindings.
    single_prompt = "queries" not in prepared or prepared["queries"] is None
    declared = prepared["required_input_fields"] if "required_input_fields" in prepared else None
    prepared["prompt_template"] = _probe_prompt_file_placeholder(declared, reads_row=single_prompt)
    return True


def _stand_in_llm_system_prompt(prepared: object) -> bool:
    """``system_prompt``: inert text that reads nothing."""
    if type(prepared) is not dict or "system_prompt" not in prepared or not is_inline_content_reference(prepared["system_prompt"]):
        return False
    prepared["system_prompt"] = _PROBE_PROMPT_FILE_PLACEHOLDER
    return True


def _stand_in_llm_query_templates(prepared: object) -> bool:
    """``queries.*.template``, in query mode only."""
    if type(prepared) is not dict or "queries" not in prepared or prepared["queries"] is None:
        return False
    return _substitute_query_template_placeholders(prepared["queries"])


_PROBE_FILE_PLACEHOLDER = "validation-probe-file-placeholder"
"""Inert text standing in for a file-held ``file_inject`` entry inside a probe projection."""

_PROBE_JSON_VALUE_BY_DECLARED_TYPE: Final[Mapping[str, int | float | bool]] = MappingProxyType({"int": 0, "float": 0.5, "bool": True})
"""A non-null JSON value for each declarable scalar type other than ``str``; any other declaration, or none, takes a JSON string."""


def _file_inject_declared_fields(schema: object) -> dict[str, FieldDefinition] | ValueError:
    """The node's authored ``schema.fields`` by name, or the reason the block does not parse.

    The config model refuses an unparseable block with this same error when
    the probe constructs the plugin, so the error is returned to the caller,
    which then substitutes nothing rather than guess which fields were meant.
    """
    try:
        parsed = SchemaConfig.from_dict(cast("Mapping[str, Any]", schema))
    except ValueError as exc:
        return exc
    return {field.name: field for field in parsed.fields or ()}


def _file_inject_entry_stand_in(entry: object, *, written_field: object, declared: Mapping[str, FieldDefinition]) -> str | None:
    """Probe-only content for one entry, chosen by its ``format`` and the author's declaration.

    ``text`` is always the string itself and ``csv`` and ``jsonl`` always a
    list, whatever the file holds. The csv header is one column whose name
    avoids the delimiter, with no data rows. ``json`` takes a non-null value
    of the type the author declared for the written field (``int`` ``0``,
    ``float`` ``0.5``, ``bool`` ``true``), otherwise a JSON string, wrapped in
    an object under ``data_key`` when one is set; it is never a JSON null.
    ``None`` means the entry's own options are malformed, which the config
    model refuses whatever the content.

    An undeclared json written field is stood in although a JSON null in the
    file would make it nullable. That is sound only because such a field is
    typed ``any`` and no authoring consumer separates a nullable ``any`` from
    a non-null one; ``test_file_inject_file_content_probe.py::
    test_PIN_undeclared_json_nullability_is_unobservable_at_authoring`` goes
    red the day one does. A declared field is published from the declaration
    instead, and execution validation holds the bytes against it.
    """
    if type(entry) is not dict:
        return None
    fmt = entry["format"] if "format" in entry else None
    if fmt == "text":
        return _PROBE_FILE_PLACEHOLDER
    if fmt == "jsonl":
        return json.dumps(_PROBE_FILE_PLACEHOLDER) + "\n"
    if fmt == "csv":
        delimiter = entry["delimiter"] if "delimiter" in entry else ","
        if type(delimiter) is not str:
            return None
        return ("probe" if delimiter not in "probe" else "x") + "\n"
    if fmt != "json":
        return None
    value: str | int | float | bool = _PROBE_FILE_PLACEHOLDER
    if type(written_field) is str and written_field in declared:
        field_type = declared[written_field].field_type
        if field_type in _PROBE_JSON_VALUE_BY_DECLARED_TYPE:
            value = _PROBE_JSON_VALUE_BY_DECLARED_TYPE[field_type]
    data_key = entry["data_key"] if "data_key" in entry else None
    if data_key is None:
        return json.dumps(value)
    if type(data_key) is not str:
        return None
    return json.dumps({data_key: value})


def _stand_in_file_inject_contents(prepared: object) -> bool:
    """``files.*.content``: all valid markers of the node, or none of them.

    If any file-held entry cannot be stood in (its own options are
    malformed, or the schema block does not parse), every entry keeps its
    marker, so no partial substitution changes which error the constructor
    reports.
    """
    if type(prepared) is not dict or "files" not in prepared or type(prepared["files"]) is not dict:
        return False
    files = prepared["files"]
    held = [
        name
        for name, entry in files.items()
        if type(entry) is dict and "content" in entry and is_inline_content_reference(entry["content"])
    ]
    if not held:
        return False
    declared = _file_inject_declared_fields(prepared["schema"]) if "schema" in prepared else {}
    if type(declared) is not dict:
        return False
    mode = prepared["mode"] if "mode" in prepared else None
    select_field = prepared["output_field"] if mode == "select" and "output_field" in prepared else None
    stand_ins: dict[str, str] = {}
    for name in held:
        entry = files[name]
        written_field = select_field if mode == "select" else (entry["output_field"] if mode == "all" and "output_field" in entry else None)
        stand_in = _file_inject_entry_stand_in(entry, written_field=written_field, declared=declared)
        if stand_in is None:
            return False
        stand_ins[name] = stand_in
    for name, stand_in in stand_ins.items():
        files[name]["content"] = stand_in
    return True


_ProbeStandIn = Callable[[object], bool]

_PROBE_STAND_INS: Final[Mapping[str, tuple[tuple[str, _ProbeStandIn], ...]]] = MappingProxyType(
    {
        "llm": (
            ("prompt_template", _stand_in_llm_prompt_template),
            ("system_prompt", _stand_in_llm_system_prompt),
            ("queries.*.template", _stand_in_llm_query_templates),
        ),
        "file_inject": (("files.*.content", _stand_in_file_inject_contents),),
    }
)
"""Option positions whose file-held value is no input to the plugin's output contract.

One row per (plugin, position). Each stand-in rewrites a VALID
``inline_content`` marker in the detached projection, in place, and returns
whether it did. A position belongs here only when the plugin's guaranteed
field names, their types and their nullability are the same for every bytes
the file could hold; otherwise the node must keep deferring.
"""


def prepare_validation_probe_options(options: Mapping[str, Any], *, plugin: str | None) -> dict[str, Any]:
    """Return detached runtime options safe for validation-only construction.

    ``plugin`` is required (``None`` for an unknown plugin) so a new probe
    call site cannot silently opt out of the deployment-injected stubs below —
    a forgotten argument is a TypeError, not a quietly zeroed contract.

    For a profile-authored ``plugin="llm"`` with no private binding fields, an
    inert gateway provider/model block is supplied for the probe. A
    profile-authored composer llm node (transform and source alike — both are
    named "llm") carries only its public ``profile`` alias: lowering removes
    that alias and injects the private ``provider``/``model``/credential binding
    (``plugin_policy/profiles.py::lower_options``), after Stage 1 has already
    run. The validation-only projection mirrors that executable shape by
    removing ``profile`` and supplying inert bindings. Probing construction on
    authored options alone otherwise fails on the missing private fields (and
    on ``profile`` being extra executable input), and
    ``_effective_producer_vote``'s known-pass-through fail-closed arm turned
    that permanent condition into "participates with zero guarantees" — a
    false ``guarantees: [(none)]`` reject for any required-fields consumer
    downstream of any llm (elspeth-d4ae04b374).

    The llm output contract is a pure function of the provider-INDEPENDENT
    config (schema block, ``response_field``, ``queries``/``output_fields``) —
    the provider instance is not even built until ``on_start()`` — so a stub
    provider yields the same contract math as the lowered runtime build.
    Gateway is the stub because its ``model`` is a logical alias with no local
    catalog to validate against — exactly the shape of composer-authored model
    names. ``required_capabilities`` derives from the closed vocabulary rather
    than restating it, so a structured-output node (which demands
    ``json_schema``) still constructs. An authored ``provider`` — a YAML
    import that carries its own — is left to stand on its own config.

    For ``plugin="llm"`` the projection also supplies probe-only text wherever
    a prompt position (``prompt_template``, ``system_prompt``, a query's
    ``template``) holds a VALID ``inline_content`` marker. The config model
    types those positions ``str``, so the marker alone fails construction, the
    probe defers, and ``_effective_producer_vote`` answers a deferred probe
    with zero guarantees: every consumer declaring fields downstream of a
    file-prompted llm was rejected with ``guarantees: [(none)]``, although the
    list above shows prompt text is no input to the output contract. Deferral
    is right where the bytes decide the contract (``reference_join``'s table)
    and stays the answer for every other plugin and every other llm option.

    The placeholder is not the file's text and proves nothing about it. Every
    check that reads prompt text (Jinja2 syntax, row-access rules, the
    template/``required_input_fields`` agreement, header-spelled lookups) is
    left to execution validation, which materializes the authorized bytes and
    builds the same config model on them before a run is admitted. Those
    checks did not run at authoring before either: the string-type failure
    stopped the model ahead of them. A malformed marker is not substituted and
    stays a config probe failure. The text exists only in the returned
    detached dict, which the call sites hand to a plugin constructor and drop;
    saved options keep the marker.

    When any of those positions was substituted, the projection also omits
    ``approved_prompt_artifact_hash``. That hash binds the node's effective
    prompt text: ``system_prompt`` plus ``prompt_template`` in single-prompt
    mode, or plus each query's effective template (its own, else
    ``prompt_template``) in query mode. Approving a file's review card
    stamps it from the reviewed bytes
    (``sessions/pending_interpretation.py``), so the saved node carries a
    digest of text this projection does not hold; the config model would
    compare it with the placeholder, refuse, and return the node to zero
    guarantees the moment the user approved. One file-backed position is
    enough to make the effective text unknowable here, so one substitution
    drops the hash. For a file prompt the saved hash has no authority at run
    time either: ``materialize_state_for_execution`` drops it for a marker,
    and the run binds the admitted review snapshot's hash after the bytes are
    substituted (``execution/service.py::_bind_admitted_prompt_artifact_hashes``),
    which the config model then re-derives and checks. The authoring
    projection has no re-bind step, so dropping the hash here mirrors the
    existing behaviour and removes no check; what refuses changed text
    before a run is the review and its approval rows. An inline prompt
    substitutes nothing, keeps its hash in the projection and is still
    checked here; so is a malformed marker, or a well-formed marker of any
    other mode, neither of which is substituted.

    For ``plugin="file_inject"`` the projection supplies probe-only content
    wherever ``files.<name>.content`` holds a VALID ``inline_content`` marker,
    which in the composer is every entry (content must be a user upload). The
    same deferral otherwise zeroed the node's guarantees, upstream fields
    included, although the written field NAMES come from ``output_field``
    options and never from the bytes. The stand-in parses under the entry's
    ``format`` (``text``; ``csv`` with its ``delimiter``; ``jsonl``; ``json``
    with or without ``data_key``) in both modes, and is never a JSON null.

    One fact still depends on the bytes: a ``json`` entry holding a JSON null
    makes an undeclared written field nullable. That field is typed ``any``,
    and no authoring consumer separates a nullable ``any`` from a non-null
    one, so the stand-in decides no verdict (pinned; see
    ``_file_inject_entry_stand_in``); a declared field takes its type and
    nullability from the declaration. A node still defers only when an
    entry's own options or the schema block are malformed, and then every
    entry keeps its marker. ``default_content`` is never touched: the
    composer refuses it outright.

    The stand-in proves nothing about the file. Every check that reads the
    content runs in ``FileInjectConfig._validate_injection`` (parsing under the
    format, data_key and delimiter, canonical-value and depth rules, the
    per-entry and per-node size caps, and ``_validate_declared_output_types``,
    which holds an authored type and nullability against the parsed value).
    Execution validation runs it on the materialized bytes: the bytes replace
    the marker in ``_validation_materialization`` and the plugin is then built
    by ``validate_runtime_plugins``; run admission runs the same validation
    before a run is created. A refusal that does not depend on the bytes (text
    under a narrow declared type, a bad option beside a marker) is now made at
    authoring, as it would be for the inline twin.
    """
    from elspeth.web.interpretation_state import strip_authoring_options

    thawed = cast(dict[str, Any], deep_thaw(options))
    runtime_options = strip_authoring_options(thawed)
    prepared = redact_secret_refs_for_validation(runtime_options)
    # Approval evidence is server-stamped after review and is retained by the
    # runtime config. It is not a private provider binding: its presence must
    # not disable the same contract probe that worked before approval.
    binding_fields = LLM_PROFILE_PRIVATE_FIELDS - {"approved_prompt_artifact_hash"}
    if plugin == "llm" and "profile" in prepared and not set(prepared).intersection(binding_fields):
        from elspeth.plugins.llm.config_validation import GATEWAY_SUPPORTED_CAPABILITIES

        # ``profile`` is public authoring input, not executable plugin config;
        # trusted lowering consumes it before writing the private provider and
        # model binding. The branch arms only when every private provider-binding
        # field is absent; approval evidence is retained and validated. A malformed
        # profile-plus-private-binding draft still fails closed.
        del prepared["profile"]
        prepared["provider"] = "gateway"
        prepared["model"] = "validation-probe-model"
        prepared["endpoint"] = "https://validation-probe.invalid/v1"
        prepared["api_key"] = "validation-probe-placeholder"
        prepared["required_capabilities"] = tuple(sorted(GATEWAY_SUPPORTED_CAPABILITIES))
    if plugin is not None and plugin in _PROBE_STAND_INS:
        substituted = False
        for _position, stand_in in _PROBE_STAND_INS[plugin]:
            if stand_in(prepared):
                substituted = True
        # The approval hash binds effective prompt text this projection does
        # not hold once a file stands at any prompt position.
        if plugin == "llm" and substituted and "approved_prompt_artifact_hash" in prepared:
            del prepared["approved_prompt_artifact_hash"]
    return prepared
