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


@pytest.mark.parametrize("fault", ("expression", "reference", "foreign_store", "foreign_receiver"))
def test_provider_driver_store_requires_the_actual_assignment_target_after_constructor_rebind(monkeypatch, fault):
    units = _units()
    failures, receipt = mint.provider_driver_constructor_contract(units)
    assert failures == []
    assert receipt is not None
    path = "src/elspeth/web/sessions/continuation_driver.py"
    owner = _definition(units, path, "_SegmentDriver.__init__")
    stores = [node for node in owner.body if (path, id(node)) in receipt["owner_constructor_stores"]]
    assert len(stores) == 1
    store = stores[0]
    assert isinstance(store, ast.Assign) and len(store.targets) == 1
    target = store.targets[0]
    assert isinstance(target, ast.Attribute) and isinstance(target.ctx, ast.Store)
    assert ast.dump(target) == ast.dump(ast.parse("self.provider = provider").body[0].targets[0])
    assert ast.dump(target) != ast.dump(ast.parse("self.provider", mode="eval").body)
    if fault == "expression":
        owner.body[owner.body.index(store)] = ast.Expr(value=store.value)
    elif fault == "reference":
        owner.body[owner.body.index(store)] = ast.Expr(value=ast.parse("self.provider", mode="eval").body)
    elif fault == "foreign_store":
        store.targets[0] = ast.parse("self.foreign = provider").body[0].targets[0]
    else:
        store.targets[0] = ast.parse("foreign.provider = provider").body[0].targets[0]
    monkeypatch.setitem(mint.CONTRACTS, mint.DRIVER + ":_SegmentDriver.__init__", ast.unparse(owner))
    failures, receipt = mint.provider_driver_constructor_contract(units)
    assert failures == ["provider driver exact work/owner/phase transfer changed"]
    assert receipt is None


@pytest.mark.parametrize("fault", ("freeze-field", "independent-messages", "constructor-prompt"))
def test_title_value_snapshot_keeps_exact_freeze_alias_and_original_prompt_after_body_rebind(monkeypatch, fault):
    units = _units()
    _positive(units)
    path = "src/elspeth/web/sessions/_auto_title.py"
    symbol = "prepare_auto_title_request" if fault == "constructor-prompt" else "PreparedAutoTitleRequest"
    owner = _definition(units, path, symbol)
    if fault == "freeze-field":
        call = title._one_call(owner, "freeze_fields")
        assert call is not None and ast.unparse(call.args[1]) == "'kwargs'"
        call.args[1] = ast.Constant(value="user_prompt")
    elif fault == "independent-messages":
        getter = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "audit_messages")
        result = getter.body[0].value
        assert ast.unparse(result.args[1]) == "self.kwargs['messages']"
        result.args[1] = ast.Call(func=ast.Name(id="deep_freeze", ctx=ast.Load()), args=[result.args[1]], keywords=[])
    else:
        constructor = title._one_call(owner, "PreparedAutoTitleRequest")
        assert constructor is not None and ast.unparse(constructor.args[1]) == "user_message"
        constructor.args[1] = ast.Constant(value="foreign")
    monkeypatch.setitem(title.CONTRACTS, path + ":" + symbol, ast.unparse(owner))
    failures, receipt = title.title_provider_local_contracts(units)
    assert failures == ["TITLE transfer: exact prepared value freeze/alias/prompt changed"]
    assert receipt is None


@pytest.mark.parametrize(
    "wrapper,target,position,keyword",
    (
        ("_chunk", "_chunk_data", 2, None),
        ("_source_chain", "title_source_chain_data_on_connection", None, "generation"),
        ("_require_physical", "_require_physical_data", 6, None),
        ("_require_audit_cohort", "_require_audit_cohort_data", None, "settled"),
        ("_read_title_result_data_on_connection", "read_title_result_data_on_connection", None, "names"),
    ),
)
def test_title_moved_source_wrappers_keep_exact_original_arguments_after_body_rebind(monkeypatch, wrapper, target, position, keyword):
    units = _units()
    _positive(units)
    owner = _definition(units, title.SQL, wrapper)
    call = title._one_call(owner, target)
    assert call is not None
    if keyword is None:
        call.args[position] = ast.Name(id="foreign", ctx=ast.Load())
    else:
        title._keyword(call, keyword).value = ast.Name(id="foreign", ctx=ast.Load())
    monkeypatch.setitem(title.CONTRACTS, title.SQL + ":" + wrapper, ast.unparse(owner))
    failures, receipt = title.title_provider_local_contracts(units)
    assert failures == ["TITLE transfer: exact moved source identity/arguments changed"]
    assert receipt is None


@pytest.mark.parametrize(
    "path,symbol,guard",
    (
        (title.SQL, "_chunk_data", "codec != ref.codec"),
        (title.SQL, "title_source_chain_data_on_connection", "physical_ids !="),
        (title.SQL, "_require_physical_data", "physical_row.lease_token"),
        (title.SQL, "read_title_result_data_on_connection", "effect.receipt_key"),
        (title.SQL, "_require_audit_cohort_data", "call.call_id is None"),
        (title.BOOTSTRAP, "pretitle_recovery_origin_data_on_connection", "previous_attempt.revoked_at"),
        (title.BOOTSTRAP, "require_pretitle_publication_data_on_connection", "creator.sequence"),
        (title.BOOTSTRAP, "_require_pretitle_inserted_data_on_connection", "frame.ingress_phase"),
    ),
)
def test_title_moved_source_guards_remain_complete_owned_supplier_bodies(path, symbol, guard):
    units = _units()
    _positive(units)
    owner = _definition(units, path, symbol)
    guards = [node for node in ast.walk(owner) if isinstance(node, ast.If) and guard in ast.unparse(node.test)]
    assert len(guards) == 1
    guards[0].test = ast.Constant(value=False)
    failures, receipt = title.title_provider_local_contracts(units)
    assert failures == ["TITLE transfer: complete current source contract changed " + path + ":" + symbol]
    assert receipt is None


@pytest.mark.parametrize("fault", ("module", "alias", "binding", "origin_input", "publication_input"))
def test_title_moved_pretitle_imports_keep_the_actual_local_supplier_after_body_rebind(monkeypatch, fault):
    units = _units()
    _positive(units)
    owner = _definition(units, title.SQL, "title_source_chain_data_on_connection")
    imports = [
        node
        for node in owner.body
        if isinstance(node, ast.ImportFrom) and node.module == "elspeth.web.sessions.continuation_bootstrap_source"
    ]
    assert len(imports) == 1
    if fault == "module":
        imports[0].module = "elspeth.web.sessions.foreign_source"
    elif fault == "alias":
        imports[0].names[0].asname = "foreign_source"
    elif fault == "binding":
        owner.body.insert(2, ast.parse("pretitle_recovery_origin_data_on_connection = foreign_source").body[0])
    else:
        target = (
            "pretitle_recovery_origin_data_on_connection" if fault == "origin_input" else "require_pretitle_publication_data_on_connection"
        )
        call = title._one_call(owner, target)
        assert call is not None
        title._keyword(call, "selected").value = ast.Name(id="foreign", ctx=ast.Load())
    monkeypatch.setitem(title.CONTRACTS, title.SQL + ":title_source_chain_data_on_connection", ast.unparse(owner))
    failures, receipt = title.title_provider_local_contracts(units)
    assert failures == ["TITLE transfer: exact moved source identity/arguments changed"]
    assert receipt is None


@pytest.mark.parametrize(
    "fault", ("prepare_cap", "descriptor_owner", "constructor_owner", "dispatch_cap", "dispatch_owner", "verify_prompt")
)
def test_title_prepared_owner_and_limit_keep_exact_forwarding_after_body_rebind(monkeypatch, fault):
    units = _units()
    _positive(units)
    if fault.startswith("dispatch"):
        path, symbol, target = title.PRODUCER, title.RUN, "_maybe_auto_title_session_required"
    elif fault == "verify_prompt":
        path, symbol, target = title.SQL, "title_input_descriptor", "prepared_input.verify"
    else:
        path, symbol = title.SQL, "prepare_title_provider_on_connection"
        target = {
            "prepare_cap": "prepare_provider_input",
            "descriptor_owner": "title_input_descriptor",
            "constructor_owner": "PreparedTitleProvider",
        }[fault]
    owner = _definition(units, path, symbol)
    call = title._one_call(owner, target)
    assert call is not None
    if fault == "constructor_owner":
        assert ast.unparse(call.args[-1]) == "provider_input"
        call.args[-1] = ast.Name(id="foreign", ctx=ast.Load())
    else:
        keyword = {
            "prepare_cap": "max_bytes",
            "descriptor_owner": "prepared_input",
            "dispatch_cap": "max_prepared_request_bytes",
            "dispatch_owner": "prepared_input",
            "verify_prompt": "user_prompt",
        }[fault]
        title._keyword(call, keyword).value = ast.Name(id="foreign", ctx=ast.Load())
    monkeypatch.setitem(title.CONTRACTS, path + ":" + symbol, ast.unparse(owner))
    failures, receipt = title.title_provider_local_contracts(units)
    assert failures == ["TITLE transfer: original prepared input/cap forwarding changed"]
    assert receipt is None


@pytest.mark.parametrize("fault", ("prepared", "owner", "service", "running", "work", "reorder", "cancel", "frame", "extra"))
def test_title_actual_driver_keeps_both_owned_transitions_after_body_rebind(monkeypatch, fault):
    units = _units()
    _positive(units)
    owner = _definition(units, title.DRIVER, "_SegmentDriver.advance")
    run = title._one_call(owner, title.RUN)
    settle = title._one_call(owner, title.SETTLE)
    assert run is not None and settle is not None
    branches = [
        node for node in ast.walk(owner) if isinstance(node, ast.If) and ast.unparse(node.test) == "top.provider_effect_key is None"
    ]
    assert len(branches) == 1
    branch = branches[0]
    if fault in {"prepared", "owner"}:
        title._keyword(run, "prepared" if fault == "prepared" else "invocation_owner").value = ast.Name(id="foreign", ctx=ast.Load())
    elif fault in {"service", "running", "work"}:
        title._keyword(settle, "required_work" if fault == "work" else fault).value = ast.Name(id="foreign", ctx=ast.Load())
    elif fault == "reorder":
        branch.body.reverse()
    elif fault == "cancel":
        awaited = next(node for node in ast.walk(branch) if isinstance(node, ast.Await) and node.value is run)
        awaited.value = ast.Call(
            func=ast.Attribute(value=ast.Name(id="asyncio", ctx=ast.Load()), attr="shield", ctx=ast.Load()), args=[run], keywords=[]
        )
    elif fault == "frame":
        initialize = next(
            node for node in owner.body if isinstance(node, ast.If) and ast.unparse(node.test) == "type(top) is InitializeFrame"
        )
        initialize.test = ast.Constant(value=True)
    else:
        branch.body.append(ast.parse("await run_prepared_title_provider(prepared=prepared, invocation_owner=self.provider)").body[0])
    monkeypatch.setitem(title.CONTRACTS, title.DRIVER + ":_SegmentDriver.advance", ast.unparse(owner))
    failures, receipt = title.title_provider_local_contracts(units)
    assert failures == ["TITLE transfer: actual driver preparation/owner/settlement join changed"]
    assert receipt is None


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
    advance = _definition(units, title.DRIVER, "_SegmentDriver.advance")
    actual_entries = frozenset(
        (title.DRIVER, id(node.func))
        for node in ast.walk(advance)
        if isinstance(node, ast.Call) and ast.unparse(node.func) in {title.RUN, title.SETTLE}
    )
    assert len(actual_entries) == 2
    assert receipt["entry_reference_uses"] == actual_entries
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
