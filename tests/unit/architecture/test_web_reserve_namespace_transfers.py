"""Current namespace dependencies and mutations for reserve source receipts."""

import ast
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.helpers import web_reserve_compose_proposal_transfers as compose
from tests.helpers import web_reserve_proposal_child_transfers as child
from tests.helpers import web_reserve_terminal_sql_transfers as terminal
from tests.helpers import web_reserve_worker_caller as worker_caller
from tests.helpers import web_worker_whole_scope_origin as worker_origin

ROOT = Path(__file__).parents[3]


def _units(paths):
    return tuple(SimpleNamespace(path=path, tree=ast.parse((ROOT / path).read_text())) for path in paths)


def _nodes(units):
    return {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}


def _recovery_units():
    return _units((compose.SERVICE, compose.RECOVERY_MESSAGES))


def _policy_units():
    return _units((worker_origin.APP, worker_origin.POLICY_SETTINGS, worker_origin.POLICY_MODELS))


def test_recovery_instruction_dependency_receipt_uses_actual_producer_and_call():
    units = _recovery_units()
    failures, receipt = compose.recovery_instruction_supplier_contracts(units)
    assert failures == []
    assert receipt["supplier_definitions"]
    assert receipt["supplier_calls"]
    assert set(receipt["supplier_definitions"] + receipt["supplier_calls"] + receipt["source_origins"]) <= _nodes(units)
    assert receipt["protected_origins"] == compose.RECOVERY_INSTRUCTION_ORIGINS
    assert receipt["common_unknown"]


@pytest.mark.parametrize("fault", ("trust", "producer_import", "consumer_import", "rebind", "evaluated_escape", "argument"))
def test_recovery_instruction_dependency_refuses_changed_supplier_and_transfer(fault):
    units = _recovery_units()
    assert compose.recovery_instruction_supplier_contracts(units)[0] == []
    consumer, supplier = units
    producer = compose._node(supplier, "recovery_instruction_message")
    recovery = compose._node(consumer, "ComposerServiceImpl.compose_recovery")
    if fault == "trust":
        trust = next(node for node in ast.walk(producer) if isinstance(node, ast.Attribute) and node.attr == "INSTRUCTION")
        trust.attr = "CUSTOMER"
    elif fault in {"producer_import", "consumer_import"}:
        unit = supplier if fault == "producer_import" else consumer
        module = "elspeth.contracts.prompt_guard" if fault == "producer_import" else "elspeth.web.composer.continuation_message_builders"
        imported = next(node for node in unit.tree.body if isinstance(node, ast.ImportFrom) and node.module == module)
        imported.module = "foreign.supplier"
    elif fault == "rebind":
        supplier.tree.body.extend(ast.parse("recovery_instruction_message = foreign").body)
    elif fault == "evaluated_escape":
        supplier.tree.body.extend(ast.parse("class Foreign:\n    retained = recovery_instruction_message\n").body)
    else:
        call = next(
            node for node in ast.walk(recovery) if isinstance(node, ast.Call) and ast.unparse(node.func) == "recovery_instruction_message"
        )
        call.args.append(ast.Name(id="provider_owner", ctx=ast.Load()))
    failures, receipt = compose.recovery_instruction_supplier_contracts(units)
    assert failures
    assert receipt is None


def test_current_policy_dependency_receipt_binds_complete_value_suppliers():
    units = _policy_units()
    failures, receipt = worker_origin.continuation_policy_supplier_contracts(units)
    assert failures == []
    assert set(receipt["supplier_definitions"] + receipt["supplier_calls"] + receipt["source_origins"]) <= _nodes(units)
    assert receipt["protected_origins"] == worker_origin.POLICY_SUPPLIER_ORIGINS
    assert receipt["common_unknown"]
    call = next(node for node in ast.walk(units[0].tree) if (units[0].path, id(node)) == receipt["consumer"]["policy_call"])
    assert ast.unparse(call) == "continuation_policy_from_settings(settings)"


@pytest.mark.parametrize("fault", ("disabled", "guard_margin", "feasibility", "model_effect", "model_frozen", "import"))
def test_policy_dependency_refuses_changed_complete_settings_and_models(fault):
    units = _policy_units()
    assert worker_origin.continuation_policy_supplier_contracts(units)[0] == []
    _, settings, models = units
    if fault == "disabled":
        converter, _ = worker_origin.find(settings.tree, "continuation_policy_from_settings")
        converter.body[2].body[0].value = ast.Name(id="foreign_policy", ctx=ast.Load())
    elif fault == "guard_margin":
        guard, _ = worker_origin.find(settings.tree, "_guard_seconds")
        guard.body[-1].value = ast.Name(id="timeout", ctx=ast.Load())
    elif fault == "feasibility":
        admission, _ = worker_origin.find(settings.tree, "_require_first_segment_fits_its_chain")
        admission.body[-1].test = ast.Constant(value=False)
    elif fault == "model_effect":
        model, _ = worker_origin.find(models.tree, "ContinuationAdmissionPolicy")
        model.body.append(ast.parse("foreign_effect()").body[0])
    elif fault == "model_frozen":
        model, _ = worker_origin.find(models.tree, "ContinuationAdmissionPolicy")
        config = model.body[0].value
        next(keyword for keyword in config.keywords if keyword.arg == "frozen").value = ast.Constant(value=False)
    else:
        imported = next(
            node
            for node in settings.tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "elspeth.web.composer.continuation_policy"
        )
        imported.module = "foreign.policy"
    failures, receipt = worker_origin.continuation_policy_supplier_contracts(units)
    assert failures
    assert receipt is None


@pytest.mark.parametrize(
    "fault",
    ("settings", "omit", "import", "local", "extra_use", "settings_rebind", "authority_rebind", "authority_escape", "worker_authority"),
)
def test_policy_transfer_refuses_drift_after_app_grammar_is_rebound(fault, monkeypatch):
    units = _units((worker_origin.APP, worker_origin.WORKER, worker_origin.TURN))
    assert worker_origin.worker_whole_scope_origin_local_contracts(units)[0] == []
    assert worker_caller.worker_caller_local_contracts(units)[0] == []
    app = units[0]
    factory, _ = worker_origin.find(app.tree, "_create_app")
    authority = next(
        node for node in factory.body if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "composer_async_authority"
    ).value
    keyword = next(item for item in authority.keywords if item.arg == "continuation_policy")
    if fault == "settings":
        keyword.value.args[0] = ast.Name(id="foreign_settings", ctx=ast.Load())
    elif fault == "omit":
        authority.keywords.remove(keyword)
    elif fault == "import":
        imported = next(
            node
            for node in app.tree.body
            if isinstance(node, ast.ImportFrom) and node.module == "elspeth.web.composer.continuation_policy_settings"
        )
        imported.module = "foreign.settings"
    elif fault == "worker_authority":
        worker = next(
            node.value
            for node in factory.body
            if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "app.state.composer_async_worker"
        )
        next(item for item in worker.keywords if item.arg == "authority").value = ast.Name(id="foreign_authority", ctx=ast.Load())
    else:
        statement = {
            "local": "continuation_policy_from_settings = foreign",
            "extra_use": "foreign(continuation_policy_from_settings)",
            "settings_rebind": "settings = foreign",
            "authority_rebind": "composer_async_authority = foreign",
            "authority_escape": "foreign(composer_async_authority)",
        }[fault]
        factory.body.insert(len(factory.body) - 1, ast.parse(statement).body[0])
    ast.fix_missing_locations(app.tree)
    monkeypatch.setitem(worker_origin.GRAMMARS, "_create_app", worker_origin.grammar(factory))
    failures, _ = worker_origin.worker_whole_scope_origin_local_contracts(units)
    assert failures
    assert any("policy supplier" in failure or "continuation_policy_from_settings" in failure for failure in failures)


@pytest.mark.parametrize("fault", ("base", "descriptor", "default", "class_effect"))
def test_current_terminal_namespace_keeps_every_class_header_effect(fault):
    (unit,) = _units((terminal.SERVICE,))
    current, _ = terminal.find(unit.tree, terminal.CLASS)
    assert terminal.namespace_grammar(current) == terminal.NAMESPACE_GRAMMAR
    changed = copy.deepcopy(current)
    if fault == "base":
        changed.bases.append(ast.Name(id="ForeignBase", ctx=ast.Load()))
    elif fault == "descriptor":
        changed.body.append(ast.parse("def reserve(self, request):\n    return request\n").body[0])
    elif fault == "default":
        method = next(
            node for node in changed.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "persist_continuation_compose_turn"
        )
        index = next(index for index, argument in enumerate(method.args.kwonlyargs) if argument.arg == "continuation_env_ref_names")
        method.args.kw_defaults[index] = ast.parse("foreign_default()", mode="eval").body
    else:
        changed.body.append(ast.parse("foreign_effect()").body[0])
    assert terminal.namespace_grammar(changed) != terminal.NAMESPACE_GRAMMAR


@pytest.mark.parametrize("fault", ("source", "registration", "actual_outcome", "effect"))
def test_continuation_sql_validator_body_stays_bound_beside_its_header(fault):
    units = _units((child.SETTLEMENT, child.REQUIRED))
    assert child.proposal_child_local_contracts(units)[0] == []
    required = units[1]
    coordinator, _ = child.find(required.tree, "RequiredWorkCoordinator")
    validator, _ = child.find(required.tree, "RequiredWorkCoordinator.validate_continuation_sql_outcome")
    original_namespace = child.namespace_grammar(coordinator)
    if fault == "source":
        guard = validator.body[0]
        guard.test = ast.Constant(value=False)
    elif fault == "registration":
        validator.body[1].body[0] = ast.Pass()
    elif fault == "actual_outcome":
        validator.body[1].body[1].value.args[1] = ast.Name(id="foreign_outcome", ctx=ast.Load())
    else:
        validator.body.append(ast.parse("foreign(ticket)").body[0])
    assert child.namespace_grammar(coordinator) == original_namespace
    failures, _ = child.proposal_child_local_contracts(units)
    assert "proposal child transfer: continuation SQL outcome ownership changed" in failures


@pytest.mark.parametrize("supplier", ("recovery", "policy"))
def test_new_supplier_receipts_require_the_actual_defining_source(supplier):
    if supplier == "recovery":
        units = _recovery_units()
        check = compose.recovery_instruction_supplier_contracts
    else:
        units = _policy_units()
        check = worker_origin.continuation_policy_supplier_contracts
    assert check(units)[0] == []
    failures, receipt = check(units[:-1])
    assert failures
    assert receipt is None
