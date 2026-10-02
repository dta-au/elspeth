"""Tracer-bullet: set_source end-to-end through manifest + walker (spec §11).

These tests pin the integration shape established in Task 4 of the Phase 2
redaction plan.  Tasks 13/14/15 replicate the same shape for other tools,
so the assertions here are load-bearing for the bulk-promotion wave.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Annotated, Any

import pytest
from pydantic import BaseModel, ValidationError

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.pipeline_proposal import AbsentBase, PipelineProposal
from elspeth.web.composer.redaction import (
    MANIFEST,
    REDACTED_BLOB_SOURCE_PATH,
    Sensitive,
    SetSourceArgumentsModel,
    _redact_via_schema,
    _summarize_set_source_options,
    normalize_set_pipeline_redacted_arguments,
    redact_source_storage_path,
    redact_tool_call_arguments,
)
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry


def _option_shape_summary(
    *,
    mapping: int = 0,
    sequence: int = 0,
    set_: int = 0,
    scalar: int = 0,
) -> dict[str, object]:
    return {
        "_option_shape": "mapping",
        "entry_count": mapping + sequence + set_ + scalar,
        "value_shape_counts": {
            "mapping": mapping,
            "scalar": scalar,
            "sequence": sequence,
            "set": set_,
        },
    }


def test_set_source_manifest_entry_is_type_driven() -> None:
    entry = MANIFEST["set_source"]
    assert entry.argument_model is SetSourceArgumentsModel
    assert entry.policy is None


def test_set_source_argument_model_validates_real_llm_shape() -> None:
    llm_args = {
        "plugin": "csv",
        "options": {"path": "/tmp/data.csv", "header": True},
        "on_success": "rows",
        "on_validation_failure": "discard",
    }
    validated = SetSourceArgumentsModel.model_validate(llm_args)
    assert validated.plugin == "csv"
    assert validated.options == {"path": "/tmp/data.csv", "header": True}
    assert validated.on_success == "rows"
    assert validated.on_validation_failure == "discard"


def test_set_source_argument_model_rejects_missing_required() -> None:
    with pytest.raises(ValidationError):
        SetSourceArgumentsModel.model_validate({})


def test_set_source_argument_model_rejects_wrong_type() -> None:
    with pytest.raises(ValidationError):
        SetSourceArgumentsModel.model_validate(
            {
                "plugin": 42,
                "options": {},
                "on_success": "rows",
                "on_validation_failure": "discard",
            }
        )


def test_set_source_argument_model_rejects_extra_fields() -> None:
    """rev-2 M.1: extra='forbid' prevents argument_canonical/walker drift.

    Without this, a stray ``inline_blob`` or ``label`` field would be
    silently accepted by Pydantic but unrecorded in the walker schema —
    breaking the manifest/canonical-arguments parity invariant the
    adequacy guard relies on.
    """
    with pytest.raises(ValidationError):
        SetSourceArgumentsModel.model_validate(
            {
                "plugin": "csv",
                "options": {"path": "/tmp/x.csv"},
                "on_success": "rows",
                "on_validation_failure": "discard",
                "inline_blob": {"foo": "bar"},  # not a set_source field
            }
        )


def test_redact_substitutes_options_via_summarizer() -> None:
    """Sensitive[options] is replaced by the summarizer string at the top level.

    The summarizer returns canonical JSON of the options shape with scalar
    values redacted.
    Because Sensitive() substitutes the ENTIRE marked value, the top-level
    ``options`` slot in the redacted output is a string (the summarizer
    return), not a dict.  This is the load-bearing shape contract: the
    persistence boundary receives a scalar where a dict would otherwise
    sit.
    """
    tel = NoopRedactionTelemetry()
    args = {
        "plugin": "csv",
        "options": {"path": "/internal/blob/path.csv", "blob_ref": "abc"},
        "on_success": "rows",
        "on_validation_failure": "discard",
    }
    redacted = redact_tool_call_arguments("set_source", args, telemetry=tel)
    assert redacted["plugin"] == "csv"
    assert redacted["on_success"] == "rows"
    assert redacted["on_validation_failure"] == "discard"
    # Sensitive substitution: options is now the summarizer's str output.
    assert isinstance(redacted["options"], str)
    assert json.loads(redacted["options"]) == _option_shape_summary(scalar=2)
    # The original internal path MUST NOT appear in the summary.
    assert "/internal/blob/path.csv" not in redacted["options"]
    # Telemetry recorded the manifest dispatch with the type-driven shape.
    assert tel.manifest_dispatch_calls == [{"tool_name": "set_source", "shape": "type_driven"}]


def test_redact_source_options_summary_hides_paths_without_blob_ref() -> None:
    """Source option summaries must not preserve raw paths without blob_ref."""
    tel = NoopRedactionTelemetry()
    args = {
        "plugin": "csv",
        "options": {"path": "/tmp/data.csv"},
        "on_success": "rows",
        "on_validation_failure": "discard",
    }
    redacted = redact_tool_call_arguments("set_source", args, telemetry=tel)
    assert isinstance(redacted["options"], str)
    assert "/tmp/data.csv" not in redacted["options"]


def test_redact_source_options_summary_hides_credential_values() -> None:
    """Credential-bearing source plugin option values must not survive."""
    tel = NoopRedactionTelemetry()
    raw_connection_string = "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=KEYVALUE;EndpointSuffix=core.windows.net"
    raw_sas_token = "sig=abcdefghijklmnopqrstuvwxyz1234567890"
    raw_client_secret = "client-secret-value"
    raw_path = "container/private/customer.csv"
    args = {
        "plugin": "azure_blob",
        "options": {
            "connection_string": raw_connection_string,
            "sas_token": raw_sas_token,
            "client_secret": raw_client_secret,
            "container": "private-container",
            "blob_path": raw_path,
        },
        "on_success": "rows",
        "on_validation_failure": "discard",
    }

    redacted = redact_tool_call_arguments("set_source", args, telemetry=tel)
    serialized = json.dumps(redacted, sort_keys=True)

    assert isinstance(redacted["options"], str)
    assert raw_connection_string not in serialized
    assert raw_sas_token not in serialized
    assert raw_client_secret not in serialized
    assert raw_path not in serialized


def test_redact_source_storage_path_masks_file_shape_when_blob_ref_present() -> None:
    """The ``file`` option is an equivalent blob storage-path carrier to ``path``.

    Blob ownership and fork code (blobs/service.py, sessions fork) treat both
    ``path`` and ``file`` as internal storage-path carriers, so a state with
    ``options={"blob_ref": ..., "file": <internal storage_path>}`` must have its
    ``file`` masked too — otherwise the internal blob path leaks through the
    redaction surface (elspeth-a7aa07b7ce).
    """
    state = {
        "source": {
            "plugin": "csv",
            "options": {"file": "/internal/blob/secret-storage.csv", "blob_ref": "abc"},
        }
    }
    redacted = redact_source_storage_path(state)
    assert redacted["source"]["options"]["file"] == REDACTED_BLOB_SOURCE_PATH
    assert "/internal/blob/secret-storage.csv" not in str(redacted)
    # Input is not mutated.
    assert state["source"]["options"]["file"] == "/internal/blob/secret-storage.csv"


def test_redact_source_storage_path_masks_path_shape_when_blob_ref_present() -> None:
    """Regression: the ``path`` shape stays redacted (elspeth-a7aa07b7ce)."""
    state = {"source": {"options": {"path": "/internal/blob/p.csv", "blob_ref": "abc"}}}
    redacted = redact_source_storage_path(state)
    assert redacted["source"]["options"]["path"] == REDACTED_BLOB_SOURCE_PATH


def test_redact_source_storage_path_leaves_manual_file_without_blob_ref() -> None:
    """A manual ``file`` path without ``blob_ref`` is not a blob carrier — unchanged."""
    state = {"source": {"options": {"file": "/tmp/user-data.csv"}}}
    redacted = redact_source_storage_path(state)
    assert redacted["source"]["options"]["file"] == "/tmp/user-data.csv"
    assert REDACTED_BLOB_SOURCE_PATH not in str(redacted)


def test_summarize_set_source_options_accepts_coerced_datetime() -> None:
    """Pin rev-3 A7: summarizer MUST NOT raise on reachable input values.

    Spec §9 RSK-03 requires the summarizer not raise on any reachable
    input value.  Pydantic 2.x can coerce string-like inputs to
    :class:`datetime` when the field accepts ``Any``; :func:`json.dumps`
    raises :class:`TypeError` on ``datetime`` unless ``default=str`` is
    supplied.  This test pins the ``default=str`` argument so a future
    refactor that removes it fails loudly here rather than silently
    violating RSK-03.
    """
    options = {"since": datetime(2026, 1, 1, tzinfo=UTC), "key": "v"}
    result = _summarize_set_source_options(options)
    assert isinstance(result, str)


def test_summarize_set_source_options_never_serializes_untrusted_key_names() -> None:
    """Open option keys are data, not trusted audit-schema field names."""
    secret_key = "api-key=SUPER-SECRET-CANARY"
    nested_key = "nested-secret-key=PROMPT-INJECTION-CANARY"
    unicode_key = "秘密🔐キー"
    long_key = "LONG-KEY-CANARY-" + ("x" * 20_000)
    options = {
        secret_key: {nested_key: "value"},
        unicode_key: ["first", "second"],
        long_key: {"set-member-a", "set-member-b"},
    }
    equivalent_shape = {
        "different-mapping-key": {"different-nested-key": "different-value"},
        "different-sequence-key": [1, 2, 3, 4],
        "different-set-key": {1},
    }

    summary = _summarize_set_source_options(options)

    assert json.loads(summary) == _option_shape_summary(mapping=1, sequence=1, set_=1)
    assert summary == _summarize_set_source_options(equivalent_shape)
    assert len(summary) < 256
    for canary in (secret_key, nested_key, unicode_key, long_key, "set-member-a"):
        assert canary not in summary


_CANARY = "CANARY-SENSITIVE-PATH-DO-NOT-LEAK"


def test_serialization_boundary_canary_not_in_json_output() -> None:
    """Pin the Phase 3 cross-boundary integration contract (rev-2 BLOCKER_A).

    Phase 3 passes the result of :func:`redact_tool_call_arguments` through
    :func:`json.dumps` before writing to ``chat_messages.tool_calls``.  This
    test verifies the canary never survives that serialization — even
    though :func:`json.dumps` would otherwise re-emit the canary if it
    appeared anywhere in the dict. The source-option summarizer substitutes
    scalar option values before serialization, independent of blob_ref.
    """
    args = {
        "plugin": "csv",
        "options": {"path": _CANARY, "blob_ref": "abc123"},
        "on_success": "rows",
        "on_validation_failure": "discard",
    }
    result = redact_tool_call_arguments("set_source", args, telemetry=NoopRedactionTelemetry())
    serialized = json.dumps(result, sort_keys=True)
    assert _CANARY not in serialized, (
        "Sensitive canary value appeared in serialized output. "
        "Redaction did not remove it from the persistence path. "
        f"Serialized: {serialized!r}"
    )
    assert "options" in serialized  # key preserved, value redacted


# ---------------------------------------------------------------------------
# Task-7 boundary tests: no-summarizer → sentinel; nested-path → NotImplementedError
# ---------------------------------------------------------------------------


def test_redact_via_schema_substitutes_sentinel_for_sensitive_field_without_summarizer() -> None:
    """Task-7: Sensitive field with no summarizer receives REDACTED_SENSITIVE_NO_SUMMARIZER.

    The Task-4 tracer-bullet raised ``NotImplementedError`` here to force
    Task 8 to define the policy.  Task 7 defines the policy: substitute the
    no-summarizer sentinel rather than preserving the raw value.  Task 8 will
    generalise nested-path handling; this test pins the top-level case.
    """
    from elspeth.web.composer.redaction import REDACTED_SENSITIVE_NO_SUMMARIZER

    class _StubModel(BaseModel):
        secret: Annotated[str, Sensitive()]  # no summarizer

    validated = _StubModel.model_validate({"secret": "CANARY"})
    tel = NoopRedactionTelemetry()
    result = _redact_via_schema("stub_tool", validated, _StubModel, telemetry=tel)
    assert result["secret"] == REDACTED_SENSITIVE_NO_SUMMARIZER
    assert "CANARY" not in str(result.values())


def test_redact_via_schema_substitutes_nested_sensitive_path() -> None:
    """Task-8 generalisation: nested-path Sensitive field is substituted in-place.

    Task 4's tracer-bullet raised ``NotImplementedError`` for any path
    containing ``.``, ``[``, or ``{``; Task 8 supersedes that boundary by
    implementing the per-path substitute closure on ``TraversalNode``. The
    inner field's summarizer output replaces the value at the nested location
    while the surrounding structure is preserved.
    """

    class _InnerModel(BaseModel):
        inner_secret: Annotated[str, Sensitive(summarizer=lambda v: "<fixed-sum>")]
        public_field: str

    class _OuterModel(BaseModel):
        payload: _InnerModel

    validated = _OuterModel.model_validate({"payload": {"inner_secret": "RAW_SECRET", "public_field": "shown"}})
    tel = NoopRedactionTelemetry()
    result = _redact_via_schema("stub_tool", validated, _OuterModel, telemetry=tel)
    assert result["payload"]["inner_secret"] == "<fixed-sum>"
    assert result["payload"]["public_field"] == "shown"
    assert "RAW_SECRET" not in str(result)


# ---------------------------------------------------------------------------
# Tier-model burn-down (B36) pins: every guard below was rewritten from a
# ``.get()`` / ABC ``isinstance`` form to a nominal ``type() is dict`` or
# membership-form read. These tests hold the redaction behaviour fixed across
# that rewrite — a value that MUST be redacted still is, and a malformed
# first-party shape fails closed instead of passing an un-redacted path.
# ---------------------------------------------------------------------------


def test_redact_source_storage_path_rejects_non_dict_source_shape() -> None:
    """A present, non-dict source is a corrupted Tier-1 serializer output.

    ``_redact_one`` checks ``type(source) is dict`` nominally: every producer
    feeding this surface (``CompositionState.to_dict``, ``deep_thaw`` in the
    session routes, the JSON-decoded MCP result) emits plain dicts, so even a
    read-only ``Mapping`` here is a shape nothing first-party produces.
    """
    from types import MappingProxyType

    proxied = MappingProxyType({"options": {"path": "/internal/blob/x.csv", "blob_ref": "abc"}})
    with pytest.raises(AuditIntegrityError, match="non-Mapping source value"):
        redact_source_storage_path({"source": proxied})
    with pytest.raises(AuditIntegrityError, match="non-Mapping source value"):
        redact_source_storage_path({"source": "not-a-mapping"})


def test_redact_source_storage_path_rejects_non_dict_options_carrying_blob_path() -> None:
    """A non-dict ``options`` value must fail closed, never pass through.

    Before the burn-down a non-``Mapping`` options value returned the source
    unchanged; a read-only ``Mapping`` was redacted. Both now raise: silently
    returning a malformed options carrier is exactly the leak this surface
    exists to prevent, and the private path must not appear in the error.
    """
    from types import MappingProxyType

    private_path = "/internal/blob/secret-storage.csv"
    proxied_options = MappingProxyType({"path": private_path, "blob_ref": "abc"})
    with pytest.raises(AuditIntegrityError, match=r"non-dict source\.options") as excinfo:
        redact_source_storage_path({"source": {"options": proxied_options}})
    assert private_path not in str(excinfo.value)
    with pytest.raises(AuditIntegrityError, match=r"non-dict source\.options"):
        redact_source_storage_path({"sources": {"s": {"options": [private_path, "blob_ref"]}}})


def test_redact_source_storage_path_none_options_and_missing_blob_ref_pass_through() -> None:
    """The documented first-party no-op shapes are unchanged by the rewrite."""
    states: list[dict[str, Any]] = [
        {"source": {"options": None}},
        {"source": {}},
        {"source": None},
        {"source": {"options": {"path": "/tmp/user.csv"}}},
    ]
    for state in states:
        assert redact_source_storage_path(state) == state


def test_hostile_nested_object_text_is_rejected_without_decoding() -> None:
    from pydantic import ValidationError

    from elspeth.web.composer.redaction import SetSourceFromBlobArgumentsModel

    for text in ("[" * 20000, "not json", '{"k": 1}'):
        with pytest.raises(ValidationError):
            SetSourceFromBlobArgumentsModel.model_validate({"blob_id": "b", "on_success": "rows", "options": text})


def test_normalize_set_pipeline_redacted_arguments_membership_shapes() -> None:
    """Only ``source.inline_blob is None`` is dropped; every other shape is untouched."""
    assert normalize_set_pipeline_redacted_arguments("scalar") == "scalar"
    no_source: dict[str, Any] = {"nodes": []}
    assert normalize_set_pipeline_redacted_arguments(no_source) is no_source
    non_dict_source = {"source": ["x"]}
    assert normalize_set_pipeline_redacted_arguments(non_dict_source) is non_dict_source
    absent = {"source": {"plugin": "csv"}}
    assert normalize_set_pipeline_redacted_arguments(absent) is absent
    present = {"source": {"plugin": "csv", "inline_blob": "<redacted>"}}
    assert normalize_set_pipeline_redacted_arguments(present) is present
    null_blob = {"source": {"plugin": "csv", "inline_blob": None}, "nodes": []}
    normalized = normalize_set_pipeline_redacted_arguments(null_blob)
    assert normalized == {"source": {"plugin": "csv"}, "nodes": []}
    assert null_blob["source"] == {"plugin": "csv", "inline_blob": None}


def _frozen_set_pipeline_arguments(source_block: dict[str, Any]) -> Mapping[str, Any]:
    """Return a set_pipeline-shaped mapping frozen by a real freezing producer.

    ``PipelineProposal.__post_init__`` deep-freezes ``pipeline``, so the
    mapping and every nested block come back as ``mappingproxy``. Building the
    frozen form here rather than calling ``deep_freeze`` on a literal is what
    makes the pins below fail if that authority ever stops freezing — a
    literal would stay green and prove nothing.

    It is the NEAREST real producer rather than the owner of this exact value:
    nothing in the tree freezes the *redacted* projection, which reaches the
    normaliser through ``json.loads`` / ``redact_tool_call_arguments``. What
    the proposal contributes is a genuinely frozen set_pipeline-shaped
    mapping, which is the input class under test.
    """
    proposal = PipelineProposal.create(
        pipeline={"source": source_block, "nodes": []},
        base=AbsentBase(),
        repair_count=0,
        skill_hash=stable_hash("planner-skill"),
    )
    frozen = proposal.pipeline
    assert type(frozen) is MappingProxyType
    assert type(frozen["source"]) is MappingProxyType
    return frozen


def test_normalize_set_pipeline_redacted_arguments_reads_the_frozen_authority_form() -> None:
    """Frozen and thawed spellings of one proposal must normalise identically.

    ``normalize_set_pipeline_redacted_arguments`` answers "nothing to
    normalise" by returning its argument unchanged, so a mapping it fails to
    RECOGNISE is indistinguishable from one that needed no work — the two
    spellings of "no inline blob" then persist as different redacted authority
    projections. ``_create_composition_proposal`` compares that projection to
    the manifest's and raises ``AuditIntegrityError`` on a mismatch, and
    ``ComposerToolInvocation`` banks ``semantic_arguments_hash =
    stored_authority_hash`` whenever the normaliser returned its input
    identically — so an unrecognised frozen mapping is banked under a hash for
    a projection that was never produced.
    """
    null_blob_source: dict[str, Any] = {"plugin": "csv", "inline_blob": None}
    frozen = _frozen_set_pipeline_arguments(null_blob_source)
    thawed = deep_thaw(frozen)

    from_frozen = normalize_set_pipeline_redacted_arguments(frozen)
    from_thawed = normalize_set_pipeline_redacted_arguments(thawed)

    # Normalise-then-thaw and thaw-then-normalise must commute: the frozen and
    # thawed spellings of one authority carry the same redacted projection.
    # (The frozen result keeps its frozen carriers — ``nodes`` stays a tuple —
    # so the comparison is on thawed content, not container identity.)
    assert from_thawed == {"source": {"plugin": "csv"}, "nodes": []}
    assert deep_thaw(from_frozen) == from_thawed
    # The nested arm is the reachable one: a shallow thaw of the authority
    # leaves a real outer dict whose ``source`` is still frozen, which passes
    # ComposerToolInvocation's outer exact-dict reject-gate untouched.
    shallow = dict(frozen)
    assert type(shallow["source"]) is MappingProxyType
    assert deep_thaw(normalize_set_pipeline_redacted_arguments(shallow)) == from_thawed


def test_normalize_set_pipeline_redacted_arguments_leaves_a_frozen_redacted_blob_alone() -> None:
    """The untouched arm: recognising the frozen form must not drop a real blob."""
    redacted_source: dict[str, Any] = {"plugin": "csv", "inline_blob": "<redacted>"}
    frozen = _frozen_set_pipeline_arguments(redacted_source)

    assert normalize_set_pipeline_redacted_arguments(frozen) is frozen
    shallow = dict(frozen)
    assert normalize_set_pipeline_redacted_arguments(shallow) is shallow
    assert normalize_set_pipeline_redacted_arguments(deep_thaw(frozen)) == {
        "source": {"plugin": "csv", "inline_blob": "<redacted>"},
        "nodes": [],
    }
