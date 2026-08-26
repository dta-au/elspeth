# tests/unit/web/composer/test_sink_required_fields_parity.py
"""Composer and engine must agree on what a sink requires of its input.

The engine's authority is ``SinkProtocol.declared_required_fields``: the DAG
builder reads it off the constructed sink (``core/dag/builder.py``), stamps it
onto ``NodeInfo``, and ``validate_sink_required_fields`` enforces it at build.

Composer used to answer the same question from the RAW config surfaces alone
(``get_raw_sink_required_fields`` -> ``SchemaConfig.get_effective_required_fields``).
That is only half the rule. ``text`` and ``document`` union the field they write
FROM into their requirement, and that name arrives as an ordinary ``field:``
option which no ``schema:`` block declares and ``required_input_fields`` never
carries. So the composer computed the empty set, Rule B skipped the sink
entirely, and a pipeline whose source cannot guarantee that field validated
GREEN while the DAG build rejected it -- a false accept, on the two sinks that
are in ``REQUIRED_WEB_PLUGIN_IDS`` and therefore live on the web surface.

The fix derives from the plugin instead of restating the schema half. These
tests pin that it STAYS derived, which is the part that outlives the defect: a
sink added later that computes a requirement from any option is covered with no
arm to add here, and a sink the fixture forgets fails loudly rather than
silently joining the unadjudicated population.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.plugin_roles import sink_declared_required_fields
from elspeth.contracts.schema import get_raw_sink_required_fields
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.plugins.infrastructure.preflight import plugin_preflight_mode
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    OutputSpec,
    SourceSpec,
    _probe_sink_declared_required_fields,
)

_OBSERVED: dict[str, Any] = {"mode": "observed"}


# One minimal, VALID config per registered sink. Every entry must construct --
# credentials and clients are deferred under ``plugin_preflight_mode``, which is
# why the cloud sinks belong here rather than in an exclusion list. Keeping the
# roster complete is what makes the adjudication test below meaningful: an
# exclusion list would let a new sink be waved through by adding its name to it.
def _minimal_sink_configs(tmp_dir: str) -> dict[str, dict[str, Any]]:
    return {
        "aws_s3": {"bucket": "b", "key": "k.csv", "schema": _OBSERVED},
        "azure_blob": {
            "container": "c",
            "blob_path": "b.csv",
            "connection_string": ("DefaultEndpointsProtocol=https;AccountName=a;AccountKey=Zm9v;EndpointSuffix=core.windows.net"),
            "schema": _OBSERVED,
        },
        "chroma_sink": {
            "collection": "my_collection",
            "persist_directory": tmp_dir,
            "mode": "persistent",
            "field_mapping": {"id_field": "id", "document_field": "body"},
            "schema": _OBSERVED,
        },
        "csv": {"path": f"{tmp_dir}/a.csv", "schema": _OBSERVED},
        "database": {"url": f"sqlite:///{tmp_dir}/a.db", "table": "t", "schema": _OBSERVED},
        "dataverse": {
            "alternate_key": "name",
            "environment_url": "https://x.crm.dynamics.com",
            "auth": {
                "method": "service_principal",
                "tenant_id": "t",
                "client_id": "c",
                "client_secret": "s",
            },
            "entity": "e",
            "field_mapping": {"name": "name"},
            "schema": _OBSERVED,
        },
        "document": {"path": f"{tmp_dir}/a.docx", "field": "body", "schema": _OBSERVED},
        "json": {"path": f"{tmp_dir}/a.json", "schema": _OBSERVED},
        "text": {"path": f"{tmp_dir}/a.txt", "field": "body", "schema": _OBSERVED},
    }


def _registered_sink_names() -> frozenset[str]:
    """Every sink the live registry knows, read from the registry itself."""
    return frozenset(sink_cls.name for sink_cls in get_shared_plugin_manager().get_sinks())


def test_every_registered_sink_is_adjudicated(tmp_path: object) -> None:
    """No sink may join the parity population by default.

    Enumerated from the plugin registry rather than from a hand-written roster,
    for the same reason the coalesce merge-strategy pin reads its ``Literal`` at
    runtime: the defect this file exists for survived because a case sat in the
    unchecked population with nobody having DECIDED it belonged there. A test
    that compared two hand-maintained lists would go green on a new sink added
    to both and pin the gap as correct.
    """
    registered = _registered_sink_names()
    configured = frozenset(_minimal_sink_configs(str(tmp_path)))

    unadjudicated = registered - configured
    assert not unadjudicated, (
        f"Registered sink(s) {sorted(unadjudicated)} have no minimal config here, so "
        "nothing checks that composer's input requirement matches what the sink "
        "declares to the engine. Discharge this by adding a minimal VALID config to "
        "_minimal_sink_configs -- and if the sink consumes a row field named by an "
        "ordinary option (as text/document do with `field:`), confirm it unions that "
        "name into declared_required_fields the way text_sink/document_sink do."
    )
    stale = configured - registered
    assert not stale, (
        f"_minimal_sink_configs names sink(s) {sorted(stale)} that the registry does "
        "not have. Remove them, or the roster drifts into fiction."
    )


@pytest.mark.parametrize("sink_name", sorted(_registered_sink_names()))
def test_composer_requirement_equals_what_the_sink_declares(sink_name: str, tmp_path: object) -> None:
    """Composer's computed requirement == the engine's authority, per sink.

    Both sides are COMPUTED here, never asserted against a literal: the expected
    value is read off a constructed plugin through the same accessor the engine
    check uses. A sink whose requirement stops being a pure function of its
    schema block therefore breaks this test on arrival instead of silently
    disagreeing with the DAG build.

    The final clause is what gives this test teeth. Comparing the probe against
    the plugin only proves the PROBE is right; it cannot notice a composer that
    computes the probe and then ignores it, because both sides of that
    comparison are assembled here rather than by ``validate()``. So every sink
    that actually requires something is additionally driven through the real
    ``validate()`` seam against a source that cannot supply it. Without that,
    reverting the fix to the raw reader leaves this test green.
    """
    options = _minimal_sink_configs(str(tmp_path))[sink_name]

    # What composer computes, exactly as ``_parse_sink_required_fields`` does.
    composer_required = get_raw_sink_required_fields(options, owner=f"output:{sink_name}") | _probe_sink_declared_required_fields(
        sink_name, options
    )

    # What the engine will enforce, read off the constructed sink.
    with plugin_preflight_mode(True):
        sink = get_shared_plugin_manager().create_sink(sink_name, dict(options))
    try:
        engine_required = sink_declared_required_fields(sink)
    finally:
        sink.close()

    assert engine_required is not None, (
        f"{sink_name!r} is registered as a sink but does not expose "
        "declared_required_fields, so the engine's own check reads nothing from it."
    )
    assert composer_required == engine_required, (
        f"Composer would require {sorted(composer_required)} of {sink_name!r}'s input "
        f"while the engine enforces {sorted(engine_required)}. Composer must DERIVE "
        "this from the constructed plugin, never restate the schema half."
    )

    if not engine_required:
        return
    # This sink requires something, so validate() must reject a source that
    # guarantees none of it -- through the real seam, not a recomputation.
    result = _state_with_sink(sink_name, options).validate()
    assert not result.is_valid, (
        f"{sink_name!r} declares {sorted(engine_required)} to the engine, but "
        "validate() accepted a pipeline whose source guarantees only 'a'. The "
        "requirement is being computed and then discarded."
    )
    assert "sink_contract_violation" in {entry.error_code for entry in result.errors}


def _state_with_sink(sink_plugin: str, sink_options: dict[str, Any]) -> CompositionState:
    """A source guaranteeing only 'a', wired straight to one sink."""
    return CompositionState(
        sources={
            "main": SourceSpec(
                plugin="csv",
                options={
                    "path": "/tmp/in.csv",
                    "schema": {"mode": "observed", "guaranteed_fields": ["a"]},
                },
                on_success="t",
                on_validation_failure="discard",
            )
        },
        nodes=(),
        outputs=(OutputSpec(name="t", plugin=sink_plugin, options=sink_options, on_write_failure="discard"),),
        edges=(EdgeSpec(id="e1", from_node="source", to_node="t", edge_type="on_success", label=None),),
        metadata={},
        version=1,
    )


@pytest.mark.parametrize("sink_plugin", ["text", "document"])
def test_sink_consuming_an_unguaranteed_field_is_rejected(sink_plugin: str, tmp_path: object) -> None:
    """The false accept itself: green composer, ``EdgeContractError`` at build.

    ``field: body`` over a source that guarantees only ``a``. The engine rejects
    this with "Sink '<name>' requires fields ['body']"; composer used to return
    is_valid=True, which leaves the authoring loop no error to repair against.
    """
    options = {"path": f"{tmp_path}/out", "field": "body", "schema": _OBSERVED}
    result = _state_with_sink(sink_plugin, options).validate()

    assert not result.is_valid, (
        f"Composer accepted a {sink_plugin!r} sink reading 'body' from a source that "
        "guarantees only 'a' -- the DAG build rejects this shape."
    )
    assert "sink_contract_violation" in {entry.error_code for entry in result.errors}


def test_sink_consuming_a_guaranteed_field_stays_valid(tmp_path: object) -> None:
    """Control: the rule fires on the MISSING guarantee, not on the sink kind.

    Without this, a fix that simply rejected every text sink would pass the test
    above.
    """
    options = {"path": f"{tmp_path}/out.txt", "field": "a", "schema": _OBSERVED}
    result = _state_with_sink("text", options).validate()

    assert result.is_valid, [entry.message for entry in result.errors]


def test_sink_consuming_no_row_field_stays_valid(tmp_path: object) -> None:
    """Control: a sink that names no consumed field is unaffected."""
    options = {"path": f"{tmp_path}/out.csv", "schema": _OBSERVED}
    result = _state_with_sink("csv", options).validate()

    assert result.is_valid, [entry.message for entry in result.errors]


def test_probe_abstains_on_a_draft_sink_config() -> None:
    """A half-authored sink must not crash ``validate()``.

    ``validate()`` runs on every composer tool call, so a raise out of the probe
    would be a live 500 -- strictly worse than the false accept being fixed. The
    config-validation paths own reporting a draft config; this probe only
    abstains.
    """
    assert _probe_sink_declared_required_fields("text", {}) == frozenset()
    assert _probe_sink_declared_required_fields("text", {"field": "not an identifier"}) == frozenset()
    assert _probe_sink_declared_required_fields("nosuchsinkplugin", {}) == frozenset()
