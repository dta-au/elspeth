"""Genuine current TITLE source first, then one planted custody defect."""

import ast
from pathlib import Path

import pytest
from tests.helpers import web_reserve_family_contracts as family
from tests.helpers import web_reserve_provider_mint_contract as mint
from tests.helpers import web_reserve_title_transfers as title
from tests.helpers.web_reserve_proposal_child_transfers import find
from tests.unit.architecture import test_web_landscape_mutation_fencing as fencing

ROOT = Path(__file__).resolve().parents[3]


def _units(*extra):
    paths = {*title.SOURCE_PATHS, *extra}
    return tuple(fencing._parse_source(path, (ROOT / path).read_text()) for path in sorted(paths))


def _definition(units, path, name):
    unit = next(unit for unit in units if unit.path == path)
    node, _ = find(unit.tree, name)
    assert node is not None
    return node


def _positive(units):
    failures, receipt = title.title_provider_local_contracts(units)
    assert failures == []
    assert receipt is not None
    return receipt


def _mint_units():
    return _units(*{"src/" + key.split(":", 1)[0].replace(".", "/") + ".py" for key in mint.CONTRACTS})


def test_title_receipt_binds_actual_reserve_origins_and_all_common_premises():
    units = _units()
    receipt = _positive(units)
    rows = {
        (unit.path, ast.unparse(node))
        for unit in units
        for node in ast.walk(unit.tree)
        if (unit.path, id(node)) in receipt["reserve_calls"]
    }
    assert rows == {
        (
            title.PRODUCER,
            "required_work.coordinator.reserve(RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL, "
            "transition_ordinal=required_work.transition_ordinal, semantic_ordinal=6, recurrence_ordinal=0)",
        )
    }
    assert receipt["validated_transfer_calls"] == receipt["reserve_calls"]
    assert receipt["entry_reference_uses"] == frozenset()
    assert receipt["remaining_premises"]
    assert receipt["validated_supplier_uses"] == frozenset()
    assert receipt["validated_calls"] == frozenset()
    assert "elspeth.web.composer.continuation_title_provider.settle_current_title_result" in receipt["function_origins"]
    assert "elspeth.web.sessions.continuation_title_provider.AdmittedTitleAuditSource" in receipt["class_origins"]
    actual = {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}
    assert receipt["reserve_calls"] <= actual
    assert receipt["selected_definitions"] <= actual
    assert receipt["checked_reference_uses"] <= actual
    for origin in receipt["source_origins"]:
        assert origin["import_statement"] in actual
        assert origin["import_alias"] in actual
        if origin["supplier_definition"] is not None:
            assert origin["supplier_definition"] in actual
        assert origin["qualification"] in {
            "CURRENT_SOURCE_ORIGIN_RUNTIME_SUPPLIER_UNKNOWN",
            "DEFERRED_SOURCE_IMPORT_COMMON_ORIGIN_CLOSURE_REQUIRED",
        }
    pair = next(iter(receipt["reserve_calls"]))
    assert receipt["reserve_receipts"][pair]["work_validation_call"] in actual
    assert receipt["reserve_receipts"][pair]["work_context_validation_call"] in actual
    assert all(dependency["qualification"] == "COMMON_ORIGIN_RECEIVER_EFFECT_PROOF_REQUIRED" for dependency in receipt["dependencies"])


@pytest.mark.parametrize(
    "fault",
    ("source", "transition", "semantic", "recurrence", "receiver", "extra", "ticket", "context", "audit", "retained"),
)
def test_title_settlement_refuses_one_reserve_or_finisher_transfer_change(fault):
    units = _units()
    _positive(units)
    owner = _definition(units, title.PRODUCER, title.SETTLE)
    reserve = title._one_call(owner, "required_work.coordinator.reserve")
    finish = title._one_call(owner, "_sessions(service).finish_provider_attempt")
    assert reserve is not None and finish is not None
    if fault == "source":
        reserve.args[0] = ast.parse("RequiredWorkSource.PROVIDER_SETTLEMENT_SQL", mode="eval").body
    elif fault in {"transition", "semantic", "recurrence"}:
        title._keyword(
            reserve, {"transition": "transition_ordinal", "semantic": "semantic_ordinal", "recurrence": "recurrence_ordinal"}[fault]
        ).value = ast.Constant(value=9)
    elif fault == "receiver":
        reserve.func.value = ast.parse("foreign.coordinator", mode="eval").body
    elif fault == "extra":
        owner.body.insert(2, ast.parse("required_work.coordinator.reserve(RequiredWorkSource.TITLE_PROVIDER_SETTLEMENT_SQL)").body[0])
    else:
        keyword = {"ticket": "required_work", "context": "session_operation_context", "audit": "call", "retained": "continuation_title"}[
            fault
        ]
        title._keyword(finish, keyword).value = ast.Name(id="foreign", ctx=ast.Load())
    failures, receipt = title.title_provider_local_contracts(units)
    assert failures == ["TITLE transfer: complete current source contract changed " + title.PRODUCER + ":" + title.SETTLE]
    assert receipt is None


@pytest.mark.parametrize("fault", ("source", "transition", "semantic", "recurrence", "receiver", "context", "audit", "missing_keyword"))
def test_title_accounting_relationships_refuse_even_after_only_the_source_body_is_rebound(monkeypatch, fault):
    units = _units()
    _positive(units)
    owner = _definition(units, title.PRODUCER, title.SETTLE)
    reserve = title._one_call(owner, "required_work.coordinator.reserve")
    finish = title._one_call(owner, "_sessions(service).finish_provider_attempt")
    assert reserve is not None and finish is not None
    if fault == "source":
        reserve.args[0].attr = "PROVIDER_SETTLEMENT_SQL"
    elif fault in {"transition", "semantic", "recurrence"}:
        keyword = {"transition": "transition_ordinal", "semantic": "semantic_ordinal", "recurrence": "recurrence_ordinal"}[fault]
        title._keyword(reserve, keyword).value = ast.Constant(value=9)
    elif fault == "receiver":
        reserve.func.value = ast.parse("foreign.coordinator", mode="eval").body
    elif fault == "missing_keyword":
        reserve.keywords.remove(title._keyword(reserve, "semantic_ordinal"))
    else:
        keyword = "session_operation_context" if fault == "context" else "call"
        title._keyword(finish, keyword).value = ast.Name(id="foreign", ctx=ast.Load())
    monkeypatch.setitem(title.CONTRACTS, title.PRODUCER + ":" + title.SETTLE, ast.unparse(owner))
    unit = next(unit for unit in units if unit.path == title.PRODUCER)
    assert title._namespace(unit.tree) == title.MODULE_NAMESPACES[title.PRODUCER]
    failures, receipt = title.title_provider_local_contracts(units)
    assert failures == ["TITLE transfer: exact current accounting ticket transfer changed"]
    assert receipt is None


@pytest.mark.parametrize(
    "fault", ("nominal", "context", "running_value", "missing_validation", "validation_work", "reader_work", "reader_names")
)
def test_title_current_work_relationships_refuse_after_only_the_mutated_supplier_body_is_rebound(monkeypatch, fault):
    units = _units()
    _positive(units)
    if fault in {"nominal", "context", "running_value"}:
        symbol = "_running"
        owner = _definition(units, title.PRODUCER, symbol)
        if fault == "nominal":
            owner.body[0].test = ast.Constant(value=False)
        elif fault == "context":
            title._one_call(owner, "work.validate_context").args[0] = ast.Name(id="foreign", ctx=ast.Load())
        else:
            owner.body[2].value = ast.Name(id="foreign", ctx=ast.Load())
    elif fault in {"missing_validation", "validation_work"}:
        symbol = "current_title_names"
        owner = _definition(units, title.PRODUCER, symbol)
        validation = title._one_call(owner, "_running")
        assert validation is not None
        if fault == "missing_validation":
            validation.func = ast.Name(id="foreign", ctx=ast.Load())
        else:
            validation.args[0] = ast.Name(id="foreign", ctx=ast.Load())
    else:
        symbol = "read_current_title_result"
        owner = _definition(units, title.PRODUCER, symbol)
        if fault == "reader_work":
            call = title._one_call(owner, "current_title_names")
            title._keyword(call, "work").value = ast.Name(id="foreign", ctx=ast.Load())
        else:
            call = title._one_call(owner, "read_title_result_on_connection")
            title._keyword(call, "inventory").value = ast.Name(id="foreign", ctx=ast.Load())
    monkeypatch.setitem(title.CONTRACTS, title.PRODUCER + ":" + symbol, ast.unparse(owner))
    unit = next(unit for unit in units if unit.path == title.PRODUCER)
    assert title._namespace(unit.tree) == title.MODULE_NAMESPACES[title.PRODUCER]
    failures, receipt = title.title_provider_local_contracts(units)
    assert failures == ["TITLE transfer: actual nominal running/current SQL ownership links changed"]
    assert receipt is None


@pytest.mark.parametrize(
    ("path", "symbol"),
    (
        (title.SQL, "_current"),
        (title.SQL, "_source_chain"),
        (title.SQL, "_require_physical"),
        (title.SQL, "_require_audit_cohort"),
        (title.SQL, "AdmittedTitleAuditSource.require"),
        (title.SQL, "admit_title_audit_source_on_connection"),
        (title.SQL, "complete_title_accounting_on_connection"),
        (title.PRODUCER, "refresh_current_title_settlement"),
    ),
)
def test_title_current_authority_physical_cohort_and_transaction_guards_remain_required(path, symbol):
    units = _units()
    _positive(units)
    owner = _definition(units, path, symbol)
    guard = next(node for node in ast.walk(owner) if isinstance(node, ast.If))
    guard.test = ast.Constant(value=False)
    failures, receipt = title.title_provider_local_contracts(units)
    assert any(failure.startswith("TITLE transfer: complete current source contract changed " + path + ":") for failure in failures)
    assert receipt is None


@pytest.mark.parametrize("fault", ("register", "early_install", "sdk_cancel", "refresh_order", "original_audit", "business_writer"))
def test_title_owned_outcome_order_and_audit_only_scope_cannot_be_relaxed(fault):
    units = _units()
    _positive(units)
    if fault == "register":
        owner = _definition(units, title.INITIALIZATION, "_registered")
        validation = title._one_call(owner, "work.coordinator.validate_continuation_sql_outcome")
        assert validation is not None
        validation.func = ast.Name(id="foreign", ctx=ast.Load())
    elif fault == "early_install":
        owner = _definition(units, title.PRODUCER, "_retain_current_title_success_once")
        owner.body.insert(0, ast.parse("custody.continuation_title_result = foreign").body[0])
    elif fault == "sdk_cancel":
        owner = _definition(units, title.QUOTA, "ProviderCallCustody._cancel_undispatched")
        owner.body[0].test = ast.Constant(value=False)
    elif fault == "refresh_order":
        owner = _definition(units, title.QUOTA, "ProviderCallCustody.settle")
        reserve = next(node for node in owner.body if isinstance(node, ast.If) and title._calls(node, "binding.coordinator.reserve"))
        owner.body.remove(reserve)
        owner.body.insert(2, reserve)
    elif fault == "original_audit":
        owner = _definition(units, title.PRODUCER, "refresh_current_title_settlement")
        returned = title._one_call(owner, "replace")
        assert returned is not None
        title._keyword(returned, "audit_call").value = ast.parse("current.settlement.audit_call", mode="eval").body
    else:
        owner = _definition(units, title.SESSIONS, "SessionServiceImpl.add_messages_atomic")
        guard = next(
            node for node in ast.walk(owner) if isinstance(node, ast.If) and "writer_principal != 'compose_loop'" in ast.unparse(node.test)
        )
        guard.test = ast.Constant(value=False)
    failures, receipt = title.title_provider_local_contracts(units)
    assert any(failure.startswith("TITLE transfer: complete current source contract changed ") for failure in failures)
    assert receipt is None


@pytest.mark.parametrize("fault", ("import", "definition_effect", "definition_default", "duplicate", "missing"))
def test_title_receipt_refuses_namespace_supplier_and_source_identity_changes(fault):
    units = _units()
    _positive(units)
    unit = next(unit for unit in units if unit.path == title.PRODUCER)
    if fault == "import":
        unit.tree.body.append(ast.parse("from foreign import RequiredWorkBinding").body[0])
    elif fault == "definition_effect":
        unit.tree.body.append(ast.parse("foreign_effect()").body[0])
    elif fault == "definition_default":
        owner = _definition(units, title.PRODUCER, title.SETTLE)
        owner.args.kw_defaults[0] = ast.parse("foreign_effect()", mode="eval").body
    elif fault == "duplicate":
        units = (*units, unit)
    else:
        units = tuple(candidate for candidate in units if candidate is not unit)
    failures, receipt = title.title_provider_local_contracts(units)
    assert failures
    assert receipt is None


@pytest.mark.parametrize("kind", ("call", "alias", "callback", "module_alias"))
def test_title_entry_reference_universe_refuses_an_unqualified_new_caller(kind):
    units = _units()
    _positive(units)
    source = {
        "call": "from elspeth.web.composer.continuation_title_provider import settle_current_title_result as settle\nasync def other(service, running, required_work):\n    return await settle(service=service, running=running, required_work=required_work)\n",
        "alias": "from elspeth.web.composer.continuation_title_provider import run_prepared_title_provider\nretained = run_prepared_title_provider\n",
        "callback": "from elspeth.web.composer.continuation_title_provider import settle_current_title_result\ndef other(register):\n    register(settle_current_title_result)\n",
        "module_alias": "import elspeth.web.composer.continuation_title_provider as title_owner\ndef other():\n    return title_owner.run_prepared_title_provider\n",
    }[kind]
    outside = fencing._parse_source("src/elspeth/title_transfer_outside_control.py", "from __future__ import annotations\n" + source)
    failures, receipt = title.title_provider_local_contracts((*units, outside))
    assert failures == ["TITLE transfer: unqualified runner/settlement caller or callable escape"]
    assert receipt is None


def test_title_mint_is_the_exact_guarded_invocation_owner_site():
    units = _mint_units()
    failures, receipt = mint.provider_mint_local_contracts(units)
    assert failures == [] and receipt is not None
    runner = _definition(units, title.PRODUCER, title.RUN)
    call = title._one_call(runner, "invocation_owner.mint")
    assert call is not None
    assert ast.unparse(call.args[0]) == "ProviderInvocationFamily.TITLE"
    assert (title.PRODUCER, id(call)) in receipt["mint_calls"]
    assert (title.PRODUCER, id(runner)) in receipt["caller_regions"]


@pytest.mark.parametrize("fault", ("type", "work", "service", "receiver", "family", "extra"))
def test_title_mint_formal_is_never_admitted_by_annotation_or_spelling(fault):
    units = _mint_units()
    failures, _ = mint.provider_mint_local_contracts(units)
    assert failures == []
    runner = _definition(units, title.PRODUCER, title.RUN)
    call = title._one_call(runner, "invocation_owner.mint")
    assert call is not None
    if fault == "type":
        runner.body[0] = ast.Pass()
    elif fault in {"work", "service"}:
        runner.body[1].test.values[0 if fault == "work" else 1] = ast.Constant(value=False)
    elif fault == "receiver":
        call.func.value = ast.Name(id="foreign_owner", ctx=ast.Load())
    elif fault == "family":
        call.args[0].attr = "PRIMARY"
    else:
        runner.body.insert(2, ast.parse("invocation_owner.mint(ProviderInvocationFamily.TITLE)").body[0])
    failures, receipt = mint.provider_mint_local_contracts(units)
    assert failures == [
        "provider mint complete producer/retention/caller contract changed elspeth.web.composer.continuation_title_provider:" + title.RUN
    ]
    assert receipt is None


@pytest.mark.parametrize("field", ("continuation_title", "continuation_title_selected", "continuation_title_result"))
def test_title_owned_slots_and_constructor_cannot_drop_or_replace_a_live_carrier(field):
    units = _mint_units()
    failures, _ = mint.provider_mint_local_contracts(units)
    assert failures == []
    quota = next(unit for unit in units if unit.path == title.QUOTA)
    assert family._check_contracts(quota, family._provider_contracts())[0] == []
    constructor = _definition(units, title.QUOTA, "ProviderCallCustody.__init__")
    assignment = next(node for node in constructor.body if isinstance(node, ast.AnnAssign) and ast.unparse(node.target) == "self." + field)
    assignment.value = ast.Name(id="foreign", ctx=ast.Load())
    failures, _ = family._check_contracts(quota, family._provider_contracts())
    assert failures == [title.QUOTA + ":ProviderCallCustody.__init__: closed execution contract changed (guard/transfer/effect/branch)"]


def test_title_family_settlement_checks_refresh_before_the_original_finisher():
    units = _mint_units()
    failures, _ = mint.provider_mint_local_contracts(units)
    assert failures == []
    quota = next(unit for unit in units if unit.path == title.QUOTA)
    assert family._check_contracts(quota, family._provider_contracts())[0] == []
    owner = _definition(units, title.QUOTA, "ProviderCallCustody.settle")
    refreshed = title._one_call(owner, "refresh_current_title_settlement")
    assert refreshed is not None
    refreshed.args[0] = ast.Name(id="foreign", ctx=ast.Load())
    failures, _ = family._check_contracts(quota, family._provider_contracts())
    assert failures == [title.QUOTA + ":ProviderCallCustody.settle: closed execution contract changed (guard/transfer/effect/branch)"]


@pytest.mark.parametrize(
    "predicate",
    (
        "self.continuation_title is not None",
        "self.attempt is not None",
        "self.required_work.role is not RequiredWorkRole.TITLE",
        "self.required_work.semantic_ordinal != 6",
        "self._cost_budget is not None",
        "type(prepared.clock) is not ContinuationActionClock",
        "prepared.work.coordinator is not self.required_work.coordinator",
        "prepared.work.transition_ordinal != self.required_work.transition_ordinal",
        "prepared.running.session_operation_context is not self.context",
        "prepared.composer._require_sessions_service() is not self.service",
    ),
)
def test_title_binding_preserves_each_actual_first_invocation_and_live_owner_guard(predicate):
    units = _mint_units()
    failures, _ = mint.provider_mint_local_contracts(units)
    assert failures == []
    owner = _definition(units, title.QUOTA, "ProviderCallCustody.bind_continuation_title")
    guard = next(node for node in owner.body if isinstance(node, ast.If))
    position = next(index for index, value in enumerate(guard.test.values) if ast.unparse(value) == predicate)
    guard.test.values[position] = ast.Constant(value=False)
    failures, receipt = mint.provider_mint_local_contracts(units)
    assert failures == ["provider mint complete producer/retention/caller contract changed " + mint.QUOTA + ":ProviderCallCustody"]
    assert receipt is None


@pytest.mark.parametrize("field", ("continuation_title", "continuation_title_result", "continuation_title_selected"))
def test_title_slot_descriptor_inventory_remains_exact(field):
    units = _mint_units()
    failures, _ = mint.provider_mint_local_contracts(units)
    assert failures == []
    quota = next(unit for unit in units if unit.path == title.QUOTA)
    expected = (
        "__weakref__",
        "_admitted_ns",
        "_admitting",
        "_context",
        "_cost_budget",
        "_cost_response_accountable",
        "_required_work",
        "_sdk_entered",
        "_service",
        "_settlements",
        "_undispatched_recurrence",
        "attempt",
        "call",
        "continuation_title",
        "continuation_title_result",
        "continuation_title_selected",
    )
    assert family._owned_class(quota, "ProviderCallCustody", expected) == []
    owner = _definition(units, title.QUOTA, "ProviderCallCustody")
    slots = next(statement.value for statement in owner.body if isinstance(statement, ast.Assign))
    slot = next(value for value in slots.elts if value.value == field)
    slot.value = "foreign"
    assert family._owned_class(quota, "ProviderCallCustody", expected) == ["ProviderCallCustody: owned slot/descriptor namespace changed"]
