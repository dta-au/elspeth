"""Positive and planted-negative controls for finite continuation transfers."""

from __future__ import annotations

import ast
import copy
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest
from tests.helpers import web_reserve_continuation_transfers as helper


@dataclass(frozen=True)
class _Source:
    path: str
    source: str
    tree: ast.Module


@cache
def _units() -> tuple[_Source, ...]:
    root = Path(__file__).resolve().parents[3]
    return tuple(_parse(path, (root / path).read_text()) for path in helper.REQUIRED_PATHS)


def _parse(path: str, source: str) -> _Source:
    return _Source(path, source, ast.parse(source, filename=path))


def _changed(path: str, before: str, after: str) -> tuple[_Source, ...]:
    units = _units()
    original = next(unit for unit in units if unit.path == path)
    assert original.source.count(before) == 1
    changed = _parse(path, original.source.replace(before, after))
    return tuple(changed if unit.path == path else unit for unit in units)


@pytest.mark.parametrize(
    "check",
    [
        helper.iteration_local_transfers,
        helper.attachment_local_transfers,
        helper.initialization_local_transfers,
        helper.closed_source_record_transfers,
    ],
)
def test_actual_continuation_transfer_receipts_belong_to_supplied_source(check):
    units = _units()
    failures, receipt = check(units)
    assert not failures, failures
    assert receipt["selected_definitions"]
    actual = {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}
    for key in helper.SITE_KEYS:
        assert receipt[key] <= actual, key
    detached = tuple(_parse(unit.path, unit.source) for unit in units)
    detached_failures, detached_receipt = check(detached)
    assert not detached_failures, detached_failures
    for key in helper.SITE_KEYS:
        assert not receipt[key] & detached_receipt[key], key


def test_closed_source_record_receipt_keeps_generic_reflector_and_plain_class_refusals():
    units = _units()
    failures, receipt = helper.closed_source_record_transfers(units)
    assert not failures and receipt, failures
    nodes = {(unit.path, id(node)): node for unit in units for node in ast.walk(unit.tree)}
    raw_writes = [nodes[site] for site in receipt["constructor_calls"] if ast.unparse(nodes[site].func) == "object.__setattr__"]
    assert raw_writes and all(helper.receiver.HARMFUL_REFLECTOR(node) for node in raw_writes)
    for key in helper.CLOSED_SOURCE_RECORDS:
        group, name = key.split(".", 1)
        unit = next(unit for unit in units if unit.path == helper.RECORD_PATHS[group])
        assert not helper.receiver.plain_class(helper._find(unit.tree, name))


@pytest.mark.parametrize("fault", ("copy", "reinit", "default", "equality", "successor", "allocator"))
def test_closed_source_record_refuses_changed_transfer_after_only_its_complete_source_is_rebound(monkeypatch, fault):
    units = _units()
    failures, receipt = helper.closed_source_record_transfers(units)
    assert not failures and receipt, failures
    path = helper.EVIDENCE_GRAPH
    original = next(unit for unit in units if unit.path == path)
    changed = _parse(path, original.source)
    cls = helper._find(changed.tree, "RestoredTurnEvidence")
    methods = {node.name: node for node in cls.body if isinstance(node, ast.FunctionDef)}
    if fault == "copy":
        writes = [
            node
            for node in ast.walk(methods["__init__"])
            if isinstance(node, ast.Call)
            and ast.unparse(node.func) == "object.__setattr__"
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "messages"
        ]
        assert len(writes) == 1
        writes[0].args[2] = ast.parse("list(messages)", mode="eval").body
    elif fault == "reinit":
        methods["__init__"].body[0].test = ast.Constant(value=False)
    elif fault == "default":
        methods["__init__"].args.defaults[-1] = ast.Constant(value="foreign")
    elif fault == "equality":
        methods["__eq__"].body[0].test = ast.Constant(value=False)
    elif fault == "successor":
        methods["with_selected_dispatch"].body[0].value.args[1] = ast.List(elts=[], ctx=ast.Load())
    else:
        methods["__new__"].body[0].value.args[0] = ast.Name(id="foreign", ctx=ast.Load())
    ast.fix_missing_locations(changed.tree)
    monkeypatch.setitem(helper.OWNER_TEMPLATES, "graph.RestoredTurnEvidence", ast.unparse(cls))
    failures, refused = helper.closed_source_record_transfers(tuple(changed if unit.path == path else unit for unit in units))
    assert failures and not refused
    assert not any("complete declaration/binding changed" in failure for failure in failures), failures


def test_iteration_receipt_closes_only_the_actual_publisher_nominal_use():
    units = _units()
    path = "src/elspeth/web/sessions/continuation_composition_initialization.py"
    publisher_unit = next(unit for unit in units if unit.path == path)
    failures, receipt = helper.iteration_local_transfers(units)
    assert failures == []
    publisher = helper._find(publisher_unit.tree, "publish_composition_initialization_on_connection")
    checks = [
        node
        for node in ast.walk(publisher)
        if isinstance(node, ast.Compare) and ast.unparse(node) == "type(owners.live) is not _ComposeIterationState"
    ]
    assert len(checks) == 1
    actual = {(path, id(node)) for node in ast.walk(publisher_unit.tree)}
    expected = {(path, id(checks[0].comparators[0]))}
    assert receipt["class_uses"] & actual == expected
    assert receipt["typecheck_uses"] & actual == expected
    assert (path, id(publisher)) in receipt["selected_definitions"]
    detached = tuple(_parse(unit.path, unit.source) for unit in units)
    detached_failures, detached_receipt = helper.iteration_local_transfers(detached)
    assert detached_failures == []
    assert not actual & detached_receipt["class_uses"]
    assert not actual & detached_receipt["typecheck_uses"]


@pytest.mark.parametrize(
    ("before", "after", "detail"),
    [
        (
            "type(owners.live) is not _ComposeIterationState",
            "type(owners.source) is not _ComposeIterationState",
            "publisher nominal guard changed",
        ),
        (
            "type(owners.live) is not _ComposeIterationState",
            "type(owners.live) is _ComposeIterationState",
            "publisher nominal guard changed",
        ),
        ("type(owners.live) is not _ComposeIterationState", "False", "publisher nominal guard changed"),
        (
            "    source = owners.source",
            "    type = foreign_type\n    source = owners.source",
            "publisher nominal suppliers changed",
        ),
        (
            "    source = owners.source",
            "    _ComposeIterationState = foreign_state\n    source = owners.source",
            "publisher nominal suppliers changed",
        ),
        (
            "    source = owners.source",
            "    _ComposeIterationState\n    source = owners.source",
            "class reference inventory changed",
        ),
    ],
    ids=("wrong-subject", "reversed-guard", "missing-guard", "shadow-type", "shadow-class", "extra-reference"),
)
def test_iteration_publisher_role_survives_only_the_changed_body_pin(monkeypatch, before, after, detail):
    normal_failures, normal = helper.iteration_local_transfers(_units())
    assert normal_failures == []
    assert normal
    path = "src/elspeth/web/sessions/continuation_composition_initialization.py"
    units = _changed(path, before, after)
    changed = next(unit for unit in units if unit.path == path)
    symbol = "publish_composition_initialization_on_connection"
    monkeypatch.setitem(helper._CONTRACTS[path], symbol, helper._digest(helper._find(changed.tree, symbol)))
    failures, refused = helper.iteration_local_transfers(units)
    assert any(detail in failure for failure in failures), failures
    assert not any("selected body changed" in failure or "namespace changed" in failure for failure in failures), failures
    assert refused == {}


@pytest.mark.parametrize("change", ["foreign-import", "extra-reference"])
def test_iteration_publisher_origin_survives_only_the_changed_namespace_pin(monkeypatch, change):
    normal_failures, normal = helper.iteration_local_transfers(_units())
    assert normal_failures == []
    assert normal
    path = "src/elspeth/web/sessions/continuation_composition_initialization.py"
    if change == "foreign-import":
        units = _changed(
            path,
            "from elspeth.web.composer._compose_loop_carriers import _AdvisorReviewState, _ComposeIterationState",
            "from foreign.carriers import _AdvisorReviewState, _ComposeIterationState",
        )
        detail = "publisher nominal suppliers changed"
    else:
        original = next(unit for unit in _units() if unit.path == path)
        changed = _parse(path, original.source + "\ndef foreign_state_class():\n    return _ComposeIterationState\n")
        units = tuple(changed if unit.path == path else unit for unit in _units())
        detail = "class reference inventory changed"
    changed = next(unit for unit in units if unit.path == path)
    headers = helper._Namespace().visit(copy.deepcopy(changed.tree))
    monkeypatch.setitem(helper._NAMESPACES, path, helper._digest(headers))
    failures, refused = helper.iteration_local_transfers(units)
    assert any(detail in failure for failure in failures), failures
    assert not any("selected body changed" in failure or "namespace changed" in failure for failure in failures), failures
    assert refused == {}


@pytest.mark.parametrize("duplicate", [False, True], ids=("missing", "duplicated"))
def test_iteration_publisher_source_is_a_required_unique_prerequisite(duplicate):
    units = _units()
    assert helper.iteration_local_transfers(units)[0] == []
    path = "src/elspeth/web/sessions/continuation_composition_initialization.py"
    publisher = next(unit for unit in units if unit.path == path)
    changed = (*units, publisher) if duplicate else tuple(unit for unit in units if unit.path != path)
    failures, refused = helper.iteration_local_transfers(changed)
    assert failures == ["continuation iteration source set missing or duplicated"]
    assert refused == {}


def test_iteration_publisher_unreviewed_effect_refuses_the_exact_source_contract():
    assert helper.iteration_local_transfers(_units())[0] == []
    units = _changed(
        "src/elspeth/web/sessions/continuation_composition_initialization.py",
        "    source = owners.source",
        "    unqualified_publisher_effect()\n    source = owners.source",
    )
    failures, refused = helper.iteration_local_transfers(units)
    assert failures == ["continuation iteration selected body changed publish_composition_initialization_on_connection"]
    assert refused == {}


@pytest.mark.parametrize(
    ("path", "before", "after", "detail"),
    [
        (
            helper.CARRIERS,
            'freeze_fields(self, "chat_ingress_inputs")',
            'freeze_fields(self, "provider_owner")',
            "bindings declaration changed",
        ),
        (
            helper.CARRIERS,
            "from elspeth.contracts.freeze import freeze_fields",
            "from foreign.freeze import freeze_fields",
            "namespace changed",
        ),
        (
            helper.CARRIERS,
            "continuation_cache_history: ContinuationCachesSnapshot | None = None",
            "continuation_cache_history: ContinuationCachesSnapshot | None = foreign_history",
            "state declaration changed",
        ),
        (
            helper.RUNTIME,
            "continuation_cache_history=caches.history,",
            "continuation_cache_history=foreign_history,",
            "selected body changed",
        ),
        (
            helper.RUNTIME,
            "if live.continuation_cache_history is None:",
            "if False:",
            "selected body changed",
        ),
        (
            helper.INITIALIZATION,
            "runtime.live.state = effects.initial_state",
            "runtime.live.state = foreign_state",
            "selected body changed",
        ),
        (
            helper.INITIALIZATION,
            "if not source.same_original_source(after) or source.original != after.original:",
            "if False:",
            "selected body changed",
        ),
    ],
)
def test_iteration_transfer_refuses_changed_owned_sources(path, before, after, detail):
    failures, receipt = helper.iteration_local_transfers(_changed(path, before, after))
    assert any(detail in failure for failure in failures), failures
    assert not receipt


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            helper.BATCH,
            "retained_attachment: AttachmentResultCheckpoint | None = None,",
            "retained_attachment: AttachmentResultCheckpoint | None = foreign_result,",
        ),
        (helper.BATCH, "or retained_attachment.prepared.batch is not batch", "or False"),
        (helper.BATCH, "or retained_attachment.prepared.accumulator is not acc", "or False"),
        (helper.BATCH, "or batch.current_call is not call", "or False"),
        (helper.BATCH, 'or call.tool_name not in ("read_attachment", "get_blob_content")', "or False"),
        (helper.BATCH, "or retained_attachment.result.updated_state is not call.pre_state", "or False"),
        (helper.BATCH, "or call_model.completion.tool_batch.calls != retained_attachment.prepared.calls", "or False"),
        (helper.BATCH, "return admit_discovery_result(_tool_name, _retained_attachment.result)", "return foreign_result"),
        (helper.FINISHER, "acc=data.accumulator,", "acc=foreign_accumulator,"),
        (helper.FINISHER, "llm_messages=restored.messages,", "llm_messages=foreign_messages,"),
        (helper.FINISHER, "batch=data.batch,", "batch=foreign_batch,"),
        (helper.FINISHER, "retained_attachment=bound_source,", "retained_attachment=current,"),
        (helper.FINISHER, "if response.reference != frame.response or call_model.completion.tool_batch.calls != data.calls:", "if False:"),
        (helper.FINISHER, "or ctx.recorder is not restored.recorder", "or False"),
        (helper.FINISHER, "bound_source = replace(current, prepared=prepared, result=result)", "bound_source = current"),
        (helper.CHECKPOINT, "if value.updated_state is not call.pre_state or value.affected_nodes:", "if False:"),
        (helper.CHECKPOINT, 'raise ContinuationCASConflict("attachment checkpoint changed before current admission")', "pass"),
        (helper.ATTACHMENT_DRIVER, "restored=runtime.graph,", "restored=foreign_graph,"),
        (
            helper.PRODUCERS,
            "self._produced.append((message, recipe, _wire(message)))",
            "self._produced.append((dict(message), recipe, _wire(message)))",
        ),
        (
            helper.PRODUCERS,
            'if len(existing) != 1:\n            raise AuditIntegrityError("argument refresh duplicated its original registration")',
            'if False:\n            raise AuditIntegrityError("argument refresh duplicated its original registration")',
        ),
        (helper.FAMILY_BUILDERS, "if detach_prompt_guard_evidence([message]) != detach_prompt_guard_evidence([expected]):", "if False:"),
        (helper.MESSAGE_BUILDERS, '"tool_call_id": call_id,', '"tool_call_id": "foreign",'),
    ],
)
def test_attachment_transfer_refuses_changed_actual_ownership(path, before, after):
    failures, receipt = helper.attachment_local_transfers(_changed(path, before, after))
    assert failures
    assert not receipt


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("RequiredWorkSource.OWNED_TURN_SETUP_PRODUCER,", "RequiredWorkSource.CONTINUATION_STATE_SQL,"),
        ("transition_ordinal=work.transition_ordinal,", "transition_ordinal=0,"),
        ("semantic_ordinal=work.semantic_ordinal,", "semantic_ordinal=0,"),
        ("producer = work.coordinator.reserve(", "producer = foreign_coordinator.reserve("),
        ("producer.complete_owned(error)", "producer.complete_owned()"),
        (
            'task = asyncio.create_task(coroutine, name="continuation-pretitle-skill-history")',
            'task = asyncio.create_task(foreign_coroutine, name="continuation-pretitle-skill-history")',
        ),
        ("        coroutine.close()\n", "        pass\n"),
        (
            "failure, _ = await _join_freeform_owned_task(task, cancellation_observations=cancellations)",
            "failure, _ = await foreign_join(task, cancellation_observations=cancellations)",
        ),
        ("await _prepare_skill_history(service, required_work)", "await _prepare_skill_history(service, foreign_work)"),
    ],
)
def test_initialization_reserve_refuses_changed_producer_or_caller(before, after):
    failures, receipt = helper.initialization_local_transfers(_changed(helper.PRETITLE, before, after))
    assert failures
    assert not receipt


@pytest.mark.parametrize(
    ("check", "addition"),
    [
        (
            helper.iteration_local_transfers,
            "from elspeth.web.composer._compose_loop_carriers import _ComposeIterationState\nescaped = _ComposeIterationState\n",
        ),
        (
            helper.attachment_local_transfers,
            "from elspeth.web.composer.tool_batch import _execute_continuation_call_live\nasync def foreign(batch):\n    await _execute_continuation_call_live(batch=batch)\n",
        ),
        (
            helper.initialization_local_transfers,
            "from elspeth.web.composer.continuation_initialization import _prepare_skill_history\nasync def foreign(service, work):\n    await _prepare_skill_history(service, work)\n",
        ),
    ],
)
def test_continuation_receipts_refuse_an_extra_foreign_reference(check, addition):
    units = (*_units(), _parse("src/elspeth/web/unreviewed_continuation_transfer.py", addition))
    failures, receipt = check(units)
    assert any("reference inventory changed" in failure for failure in failures), failures
    assert not receipt


def test_continuation_receipts_refuse_duplicate_or_missing_source():
    units = _units()
    for changed in ((*units, units[0]), units[1:]):
        failures, receipt = helper.continuation_local_transfers(changed)
        assert failures
        assert not receipt


@pytest.mark.parametrize(
    ("check", "path", "symbol", "namespace", "before", "after", "detail"),
    [
        (
            helper.iteration_local_transfers,
            helper.RUNTIME,
            "_restore_live",
            False,
            "continuation_cache_history=caches.history,",
            "continuation_cache_history=foreign_history,",
            "restored-history argument changed",
        ),
        (
            helper.iteration_local_transfers,
            helper.CARRIERS,
            "_ComposeIterationState",
            True,
            "continuation_cache_history: ContinuationCachesSnapshot | None = None",
            "continuation_cache_history: ContinuationCachesSnapshot | None = foreign_history",
            "retained-history default changed",
        ),
        (
            helper.iteration_local_transfers,
            helper.INITIALIZATION,
            "_bind_first_state_on_connection",
            False,
            "runtime.live.state = effects.initial_state",
            "runtime.live.state = foreign_state",
            "first-state store changed",
        ),
        (
            helper.attachment_local_transfers,
            helper.FINISHER,
            "finish_current_attachment_result",
            False,
            "batch=data.batch,",
            "batch=foreign_batch,",
            "exact native caller changed",
        ),
        (
            helper.attachment_local_transfers,
            helper.BATCH,
            None,
            True,
            "retained_attachment: AttachmentResultCheckpoint | None = None,",
            "retained_attachment: AttachmentResultCheckpoint | None = foreign_result,",
            "exact optional formal/default changed",
        ),
        (
            helper.attachment_local_transfers,
            helper.BATCH,
            None,
            False,
            "or batch.current_call is not call",
            "or False",
            "selected admission/registration changed",
        ),
        (
            helper.attachment_local_transfers,
            helper.BATCH,
            None,
            False,
            "_retained_attachment: AttachmentResultCheckpoint | None = retained_attachment,",
            "_retained_attachment: AttachmentResultCheckpoint | None = foreign_checkpoint,",
            "retained closure capture changed",
        ),
        (
            helper.initialization_local_transfers,
            helper.PRETITLE,
            "_prepare_skill_history",
            False,
            "RequiredWorkSource.OWNED_TURN_SETUP_PRODUCER,",
            "RequiredWorkSource.CONTINUATION_STATE_SQL,",
            "actual coordinator/source/ordinal transfer changed",
        ),
        (
            helper.initialization_local_transfers,
            helper.PRETITLE,
            "_prepare_skill_history",
            False,
            "transition_ordinal=work.transition_ordinal,",
            "transition_ordinal=0,",
            "actual coordinator/source/ordinal transfer changed",
        ),
        (
            helper.initialization_local_transfers,
            helper.PRETITLE,
            "_prepare_skill_history",
            False,
            "semantic_ordinal=work.semantic_ordinal,",
            "semantic_ordinal=0,",
            "actual coordinator/source/ordinal transfer changed",
        ),
        (
            helper.initialization_local_transfers,
            helper.PRETITLE,
            "_prepare_skill_history",
            False,
            "producer.complete_owned(error)",
            "producer.complete_owned()",
            "task/completion transfer changed",
        ),
    ],
)
def test_continuation_structural_transfer_refuses_after_rebinding_only_changed_digests(
    check, path, symbol, namespace, before, after, detail, monkeypatch
):
    normal_failures, normal = check(_units())
    assert not normal_failures, normal_failures
    assert normal
    units = _changed(path, before, after)
    changed = next(unit for unit in units if unit.path == path)
    if symbol is not None:
        declaration = helper._find(changed.tree, symbol)
        assert declaration is not None
        monkeypatch.setitem(helper._CONTRACTS[path], symbol, helper._digest(declaration))
    if namespace:
        headers = helper._Namespace().visit(copy.deepcopy(changed.tree))
        monkeypatch.setitem(helper._NAMESPACES, path, helper._digest(headers))
    # Execute-body admission/capture checks have no local body pin to rebind.
    # Other premises, including current SQL recheck/lock ordering, remain exact
    # reviewed contracts; this does not claim arbitrary trusted-source rewrites
    # preserve those semantics.
    failures, receipt = check(units)
    assert any(detail in failure for failure in failures), failures
    assert not any("selected body changed" in failure or "namespace changed" in failure for failure in failures), failures
    assert not receipt


@pytest.mark.parametrize(
    ("path", "symbol", "before", "after", "detail"),
    [
        (
            helper.ATTACHMENT_APPLICATION,
            "require_attachment_projection",
            "tool_outcomes=list(prepared.batch.tool_outcomes),",
            "tool_outcomes=prepared.batch.tool_outcomes,",
            "detached state/copy transfer changed",
        ),
        (
            helper.ATTACHMENT_APPLICATION,
            "require_attachment_projection",
            "refused_resources=set(prepared.batch.refused_resources),",
            "refused_resources=prepared.batch.refused_resources,",
            "detached state/copy transfer changed",
        ),
        (
            helper.ATTACHMENT_APPLICATION,
            "require_attachment_projection",
            "state=nominal.updated_state,",
            "state=prepared.batch.state,",
            "detached state/copy transfer changed",
        ),
        (
            helper.ATTACHMENT_APPLICATION,
            "require_attachment_projection",
            "batch, call, response=nominal,",
            "prepared.batch, call, response=nominal,",
            "actual native append changed",
        ),
        (
            helper.ATTACHMENT_APPLICATION,
            "require_attachment_projection",
            "_append_continuation_tool_outcome(\n        batch, call, response=nominal, error_class=None, error_category=None, error_message=None, post_version=nominal.updated_state.version\n    )",
            "None",
            "actual native append changed",
        ),
        (
            helper.ATTACHMENT_APPLICATION,
            "require_attachment_projection",
            "pre_state=call.pre_state, result=nominal,",
            "pre_state=nominal.updated_state, result=nominal,",
            "original receipt/call/pre-state changed",
        ),
        (
            helper.ATTACHMENT_APPLICATION,
            "require_attachment_projection",
            "or invocation.started_at != audit.started_at",
            "or invocation.started_at < audit.started_at",
            "original audit cohort/timing changed",
        ),
        (
            helper.ATTACHMENT_APPLICATION,
            "require_attachment_projection",
            "if encode_application_graph(new, env_ref_names=env_ref_names, recorder_source=recorder_source) != expected_json:",
            "if encode_application_graph(new, env_ref_names=env_ref_names, recorder_source=recorder_source) == expected_json:",
            "complete successor graph comparison changed",
        ),
        (
            helper.ATTACHMENT_APPLICATION,
            "require_attachment_projection",
            "if encode_turn_evidence(after, env_ref_names=env_ref_names) != encode_turn_evidence(expected, env_ref_names=env_ref_names):",
            "if encode_turn_evidence(before, env_ref_names=env_ref_names) != encode_turn_evidence(expected, env_ref_names=env_ref_names):",
            "complete evidence/transcript comparison changed",
        ),
        (
            helper.ATTACHMENT_APPLICATION,
            "require_attachment_projection",
            "batch.next_tool_index += 1",
            "batch.next_tool_index += 2",
            "actual successor state transfer changed",
        ),
        (
            helper.ATTACHMENT_APPLICATION,
            "require_attachment_projection",
            "batch.current_call = None",
            "batch.current_call = None\n    batch.tool_outcomes.clear()",
            "detached batch receiver escaped",
        ),
        (
            helper.FINISHER,
            "finish_current_attachment_result",
            "prepared_source=current.prepared,",
            "prepared_source=prepared,",
            "actual checker caller changed",
        ),
    ],
)
def test_attachment_projection_structure_survives_only_the_mutated_body_pin(monkeypatch, path, symbol, before, after, detail):
    positive_failures, positive = helper.attachment_local_transfers(_units())
    assert not positive_failures, positive_failures
    assert positive
    units = _changed(path, before, after)
    changed = next(unit for unit in units if unit.path == path)
    declaration = helper._find(changed.tree, symbol)
    assert declaration is not None
    monkeypatch.setitem(helper._CONTRACTS[path], symbol, helper._digest(declaration))
    failures, receipt = helper.attachment_local_transfers(units)
    assert any(detail in failure for failure in failures), failures
    assert not any("selected body changed" in failure or "namespace changed" in failure for failure in failures), failures
    assert not receipt


def test_attachment_projection_supplier_survives_only_the_changed_namespace_pin(monkeypatch):
    positive_failures, positive = helper.attachment_local_transfers(_units())
    assert not positive_failures, positive_failures
    assert positive
    units = _changed(helper.ATTACHMENT_APPLICATION, "from dataclasses import replace", "from foreign.dataclasses import replace")
    changed = next(unit for unit in units if unit.path == helper.ATTACHMENT_APPLICATION)
    monkeypatch.setitem(
        helper._NAMESPACES, helper.ATTACHMENT_APPLICATION, helper._digest(helper._Namespace().visit(copy.deepcopy(changed.tree)))
    )
    failures, receipt = helper.attachment_local_transfers(units)
    assert any("projection canonical supplier changed replace" in failure for failure in failures), failures
    assert not any("selected body changed" in failure or "namespace changed" in failure for failure in failures), failures
    assert not receipt


def test_attachment_projection_refuses_an_added_native_caller_after_only_namespace_rebinding(monkeypatch):
    positive_failures, positive = helper.attachment_local_transfers(_units())
    assert not positive_failures, positive_failures
    assert positive
    original = next(unit for unit in _units() if unit.path == helper.ATTACHMENT_APPLICATION)
    addition = "\n\ndef foreign_projection(batch, call):\n    _append_continuation_tool_outcome(batch, call, response=None)\n"
    changed = _parse(original.path, original.source + addition)
    units = tuple(changed if unit.path == original.path else unit for unit in _units())
    monkeypatch.setitem(helper._NAMESPACES, original.path, helper._digest(helper._Namespace().visit(copy.deepcopy(changed.tree))))
    failures, receipt = helper.attachment_local_transfers(units)
    assert any("projection native append reference inventory changed" in failure for failure in failures), failures
    assert not any("selected body changed" in failure or "namespace changed" in failure for failure in failures), failures
    assert not receipt
