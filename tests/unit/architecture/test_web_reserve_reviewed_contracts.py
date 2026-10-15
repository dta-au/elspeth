"""Fast prerequisites and adversarial controls for the expensive reserve proof."""

import ast
from pathlib import Path

import pytest
from tests.helpers import web_reserve_compose_proposal_transfers as compose
from tests.helpers import web_reserve_composer_receiver as receiver
from tests.helpers import web_reserve_provider_mint_contract as provider
from tests.helpers import web_reserve_route_helper as route
from tests.helpers import web_reserve_terminal_sql_transfers as terminal
from tests.helpers import web_reserve_worker_caller as worker_caller
from tests.helpers import web_worker_whole_scope_origin as worker_origin
from tests.unit.architecture import test_web_landscape_mutation_fencing as fencing

ROOT = Path(__file__).parents[3]
TURN = "src/elspeth/web/sessions/composer_turn.py"
SERVICE = "src/elspeth/web/sessions/service.py"
PROTOCOL = "src/elspeth/web/sessions/protocol.py"
COMPOSER_PROTOCOL = "src/elspeth/web/composer/protocol.py"


def _source_units(paths: tuple[str, ...]) -> tuple[fencing.SourceUnit, ...]:
    return tuple(fencing._parse_source(path, (ROOT / path).read_text()) for path in paths)


def _retained_transfer_units() -> tuple[fencing.SourceUnit, ...]:
    return _source_units(
        tuple(
            sorted(
                {
                    *compose._METHOD_PATHS,
                    terminal.SERVICE,
                    terminal.REQUIRED,
                    terminal.WORKERS,
                    terminal.AUTHORITY,
                    "src/elspeth/web/composer/audit_storage.py",
                }
            )
        )
    )


def test_retained_failure_transfer_receipts_identify_exact_actual_four_calls() -> None:
    units = _retained_transfer_units()
    failures, calls = fencing._reserve_retained_failure_transfer_receipts(units)
    assert failures == []
    actual = {
        (unit.path, ast.unparse(node.func))
        for unit in units
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.Call) and (unit.path, id(node)) in calls
    }
    assert len(calls) == 4
    assert actual == {
        (TURN, "lease.create_task"),
        (TURN, "_capture_retained_operation_read"),
        (TURN, "_read_retained_operation_context"),
        (SERVICE, "private_operation_failure_draft"),
    }


@pytest.mark.parametrize(
    "mutation",
    (
        "def corrupt():\n global private_operation_failure_draft\n private_operation_failure_draft = foreign\n",
        "def corrupt():\n private_operation_failure_draft.__code__ = foreign.__code__\n",
        "def corrupt():\n ComposerOperationFailureFacts.__getattribute__ = foreign\n",
    ),
)
def test_retained_failure_supplier_refuses_deferred_replacement(mutation: str) -> None:
    units = _retained_transfer_units()
    assert fencing._reserve_retained_failure_transfer_receipts(units)[0] == []
    audit = next(unit for unit in units if unit.path == "src/elspeth/web/composer/audit_storage.py")
    audit.tree.body.extend(ast.parse(mutation).body)
    failures, calls = fencing._reserve_retained_failure_transfer_receipts(units)
    assert failures == ["retained/private transfer owned supplier mutated " + audit.path]
    assert calls == frozenset()


@pytest.mark.parametrize(
    "mutation",
    (
        "from elspeth.web.composer import audit_storage as storage\ndef corrupt():\n storage.private_operation_failure_draft = foreign\n",
        "from elspeth.web.composer.audit_storage import private_operation_failure_draft as project\ndef corrupt():\n project.__code__ = foreign.__code__\n",
        "from elspeth.web.composer.audit_storage import ComposerOperationFailureFacts as Facts\ndef corrupt():\n Facts.validate_for = foreign\n",
        "import elspeth.web.composer.audit_storage as storage\ndef corrupt():\n setattr(storage, 'private_operation_failure_draft', foreign)\n",
    ),
)
def test_retained_failure_supplier_roots_refuse_sibling_mutation(mutation: str) -> None:
    units = _retained_transfer_units()
    sibling = fencing._parse_source("src/elspeth/retained_transfer_adversary.py", mutation)
    failures = fencing._reserve_selected_origin_effect_failures((*units, sibling), fencing._reserve_RETAINED_FAILURE_ORIGINS)
    assert any(sibling.path in failure for failure in failures), failures


def test_retained_failure_supplier_roots_allow_unchanged_sibling_imports() -> None:
    sibling = fencing._parse_source(
        "src/elspeth/retained_transfer_adversary.py",
        "from elspeth.web.composer import audit_storage as storage\n"
        "from elspeth.web.composer.audit_storage import private_operation_failure_draft as project\n"
        "from elspeth.web.composer.audit_storage import ComposerOperationFailureFacts as Facts\n"
        "def harmless():\n return 'ordinary unrelated value'\n",
    )
    failures = fencing._reserve_selected_origin_effect_failures(
        (*_retained_transfer_units(), sibling), fencing._reserve_RETAINED_FAILURE_ORIGINS
    )
    assert not any(sibling.path in failure for failure in failures), failures


@pytest.mark.parametrize(
    "fault",
    (
        "authority",
        "binding",
        "callee",
        "task_supplier",
        "join",
        "cancellation",
        "reader_forwarding",
        "capture_failure",
        "outcome_guard",
        "import",
        "dependency_rebound",
        "private_callee",
        "private_argument",
        "private_forwarding",
        "private_nominal_guard",
    ),
)
def test_retained_failure_transfers_refuse_changed_source(fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    units = _retained_transfer_units()
    assert fencing._reserve_retained_failure_transfer_receipts(units)[0] == []
    index = {unit.path: unit for unit in units}
    turn = fencing._reserve_node_at(index[TURN], "_run_composer_turn")
    history = turn.body[37].body[6]
    reader = next(
        node for node in ast.walk(history) if isinstance(node, ast.Call) and ast.unparse(node.func) == "_read_retained_operation_context"
    )
    audit = index["src/elspeth/web/composer/audit_storage.py"]
    if fault in {"authority", "binding"}:
        next(keyword for keyword in reader.keywords if keyword.arg == fault).value = ast.Name(id="foreign", ctx=ast.Load())
    elif fault == "callee":
        reader.func = ast.Name(id="foreign_reader", ctx=ast.Load())
    elif fault == "task_supplier":
        history.value.func.value = ast.Name(id="foreign_lease", ctx=ast.Load())
    elif fault == "join":
        turn.body[37].body[8].value.value.args[0] = ast.Name(id="foreign_task", ctx=ast.Load())
    elif fault == "cancellation":
        turn.body[37].body[11].body[0] = ast.Pass()
    elif fault == "reader_forwarding":
        declaration = fencing._reserve_node_at(index[TURN], "_read_retained_operation_context")
        declaration.body.insert(1, ast.parse("foreign(authority)").body[0])
    elif fault == "capture_failure":
        declaration = fencing._reserve_node_at(index[TURN], "_capture_retained_operation_read")
        declaration.body[0].handlers[0].body = [ast.parse("return foreign(read)").body[0]]
    elif fault == "outcome_guard":
        declaration = fencing._reserve_node_at(index[TURN], "_RetainedOperationReadOutcome.__post_init__")
        declaration.body[0].test = ast.Constant(value=False)
    elif fault == "import":
        imported = next(
            node for node in index[TURN].tree.body if isinstance(node, ast.ImportFrom) and node.module == "elspeth.web.async_workers"
        )
        imported.module = "foreign.async_workers"
    elif fault == "dependency_rebound":
        audit.tree.body.append(
            ast.parse("def mutate():\n global build_private_tool_evidence\n build_private_tool_evidence = foreign\n").body[0]
        )
    elif fault in {"private_callee", "private_argument"}:
        method = fencing._reserve_node_at(index[SERVICE], "SessionServiceImpl.fail_composer_async_operation")
        call = next(
            node for node in ast.walk(method) if isinstance(node, ast.Call) and ast.unparse(node.func) == "private_operation_failure_draft"
        )
        if fault == "private_callee":
            call.func = ast.Name(id="foreign_projection", ctx=ast.Load())
        else:
            call.keywords[0].value = ast.Name(id="foreign_failure", ctx=ast.Load())
    else:
        declaration = fencing._reserve_node_at(audit, "private_operation_failure_draft")
        if fault == "private_forwarding":
            declaration.body.insert(2, ast.parse("foreign(failure)").body[0])
        else:
            declaration.body[1].test = ast.Constant(value=False)
    for unit in units:
        ast.fix_missing_locations(unit.tree)
    # Re-admit the changed caller grammar: structural history/call checks must
    # still refuse, rather than succeeding only because an old digest differs.
    if fault in {"authority", "binding", "callee", "task_supplier", "join", "cancellation"}:
        monkeypatch.setitem(compose._REVIEWED_METHOD_SHAPES, TURN, compose._method_shape(compose._strip_inert_annotations(turn, TURN)))
    elif fault in {"private_callee", "private_argument"}:
        monkeypatch.setitem(terminal.GRAMMARS, terminal.FAIL, terminal.grammar(method))
    failures, calls = fencing._reserve_retained_failure_transfer_receipts(units)
    assert failures
    assert calls == frozenset()


@pytest.mark.parametrize("fault", ("omit_admission", "foreign_worker_app", "foreign_return"))
def test_reviewed_app_whole_scope_matches_live_source_and_refuses_drift(fault: str) -> None:
    units = _source_units((worker_origin.APP, worker_origin.WORKER, worker_origin.TURN))
    assert worker_origin.worker_whole_scope_origin_local_contracts(units)[0] == []
    assert worker_caller.worker_caller_local_contracts(units)[0] == []
    factory = fencing._reserve_node_at(units[0], "_create_app")
    if fault == "omit_admission":
        admission = next(
            node
            for node in ast.walk(factory)
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and ast.unparse(node.value.func) == "admit_local_stores"
        )
        admission.value = ast.Constant(value=None)
    elif fault == "foreign_worker_app":
        constructor = next(
            node for node in ast.walk(factory) if isinstance(node, ast.Call) and ast.unparse(node.func) == "ComposerAsyncWorker"
        )
        argument = next(keyword for keyword in constructor.keywords if keyword.arg == "app")
        assert ast.unparse(argument.value) == "app"
        argument.value = ast.Name(id="foreign_app", ctx=ast.Load())
    else:
        final_return = factory.body[-1]
        assert isinstance(final_return, ast.Return) and ast.unparse(final_return.value) == "app"
        final_return.value = ast.Name(id="foreign_app", ctx=ast.Load())
    ast.fix_missing_locations(units[0].tree)
    assert worker_origin.worker_whole_scope_origin_local_contracts(units)[0] == [
        "worker whole scope: complete finite whole-body grammar changed _create_app"
    ]
    expected_caller_failures = [] if fault == "foreign_return" else ["worker caller: complete finite executed grammar changed _create_app"]
    assert worker_caller.worker_caller_local_contracts(units)[0] == expected_caller_failures


@pytest.mark.parametrize("fault", ("recovery_guard", "review_identity_guard", "recovery_default", "recovery_import"))
def test_reviewed_writer_recipes_match_live_source_and_refuse_recovery_drift(fault: str) -> None:
    canonical_units = fencing._production_units()
    assert fencing._reserve_recipe_failures(canonical_units) == []
    units = tuple(fencing._parse_source(item.path, item.source) if item.path == COMPOSER_PROTOCOL else item for item in canonical_units)
    unit = next(item for item in units if item.path == COMPOSER_PROTOCOL)
    symbol = "ComposerResult"
    if fault == "recovery_guard":
        method = fencing._reserve_node_at(unit, "ComposerResult.__post_init__")
        # The owned review-identity guard precedes the existing recovery guard.
        guard = method.body[1]
        assert isinstance(guard, ast.If)
        assert ast.unparse(guard.test).startswith("self.graph_recovery_intent is not None and ")
        guard.test = ast.Constant(value=False)
    elif fault == "review_identity_guard":
        method = fencing._reserve_node_at(unit, "ComposerResult.__post_init__")
        guard = method.body[0]
        assert isinstance(guard, ast.If)
        assert ast.unparse(guard.test) == (
            "self.graph_review_identity is not None and type(self.graph_review_identity) is not AdvisorGraphReviewIdentity"
        )
        guard.test = ast.Constant(value=False)
    elif fault == "recovery_default":
        owner = fencing._reserve_node_at(unit, symbol)
        field = next(
            node
            for node in owner.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "recovery_audit_drafts"
        )
        assert isinstance(field.value, ast.Tuple) and field.value.elts == []
        field.value = ast.Constant(value=None)
    else:
        symbol = "module-bindings"
        imported = next(
            node
            for node in ast.walk(unit.tree)
            if isinstance(node, ast.ImportFrom) and node.module == "elspeth.web.composer.graph_recovery"
        )
        imported.module = "unreviewed.recovery"
    assert fencing._reserve_recipe_failures(units) == ["writer target/field recipe changed " + COMPOSER_PROTOCOL + ":" + symbol]
    assert fencing._reserve_recipe_failures(canonical_units) == []


@pytest.mark.parametrize(
    "path",
    (
        TURN,
        SERVICE,
        PROTOCOL,
        "src/elspeth/web/required_work.py",
        "src/elspeth/web/coordination/composer_operation_authority.py",
        "src/elspeth/web/sessions/composer_operation_errors.py",
        "src/elspeth/web/sessions/composer_operations.py",
        "src/elspeth/web/sessions/composer_async_worker.py",
    ),
)
def test_reviewed_source_guard_matches_live_source_and_refuses_changed_bytes(path: str) -> None:
    paths = (*fencing._reserve_FULL_SOURCE_PATHS, *fencing._reserve_CANCEL_CONSUMERS)
    sources = {item: (ROOT / item).read_text() for item in paths}
    if path in fencing._reserve_CANCEL_CONSUMERS:
        check = fencing._reserve_cancel_consumer_failures
        expected = "owned cancellation consumer changed " + path
    else:
        check = fencing._reserve_full_source_failures
        expected = "full source changed " + path
    assert check(sources) == []
    sources[path] += "\n# controlled unreviewed source change\n"
    assert check(sources) == [expected]
    tree = ast.parse((ROOT / path).read_text())
    function = next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
    function.body.insert(0, ast.parse("unreviewed_effect()\n").body[0])
    sources[path] = ast.unparse(tree)
    assert check(sources) == [expected]


@pytest.mark.parametrize(
    ("path", "old_digest"),
    (
        ("src/elspeth/web/required_work.py", "7d04c0cecccc6587da79b83ac8d290769f93fd46b19f06e021912d643592b5d1"),
        (SERVICE, "45f15a607b271ed709248984e4da55d6aaf6dd52f03a2293159d816fa830eca7"),
        (PROTOCOL, "175f6ad59656431748d5472fb7892915a9c247641da6944915b2a5d41d046083"),
        (
            "src/elspeth/web/coordination/composer_operation_authority.py",
            "21ce974db2b688f578141feb109a49abb96721deb9b7b1ba27111bbb4f09b66b",
        ),
        ("src/elspeth/web/sessions/composer_operation_errors.py", "990f34740742ced097f6199cf174cfe9e3e79a9071ea3d8102d492bcc89ddc19"),
        ("src/elspeth/web/sessions/composer_operations.py", "895edd79e2c73867f8a22d5d5ed44dc325dcb0a3fba3c484b6a1666492474f01"),
        (TURN, "387e76e614f5835265f5940c61f0c96342933fc3ecacdd23843ae7b54b15c3ce"),
        ("src/elspeth/web/sessions/composer_async_worker.py", "ce8543b819fbcf7e6b613a808724df892bf17a120831941cf9ff83cebf3191ee"),
    ),
)
def test_previous_source_guard_refuses_current_source(path: str, old_digest: str, monkeypatch: pytest.MonkeyPatch) -> None:
    sources = {item: (ROOT / item).read_text() for item in (*fencing._reserve_FULL_SOURCE_PATHS, *fencing._reserve_CANCEL_CONSUMERS)}
    if path in fencing._reserve_CANCEL_CONSUMERS:
        check = fencing._reserve_cancel_consumer_failures
        assert check(sources) == []
        monkeypatch.setitem(fencing._reserve_CANCEL_CONSUMERS, path, old_digest)
        assert check(sources) == ["owned cancellation consumer changed " + path]
    else:
        check = fencing._reserve_full_source_failures
        assert check(sources) == []
        guards = fencing._reserve_full_source_guards()
        guards[path]["sha256"] = old_digest
        monkeypatch.setattr(fencing, "_reserve_full_source_guards", lambda: guards)
        assert check(sources) == ["full source changed " + path]


def test_retained_operation_context_cannot_be_replaced_at_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
    assert compose.compose_proposal_local_transfers(units)[0] == []
    method = fencing._reserve_node_at(units[0], "_run_composer_turn")
    calls = receiver.composer_dispatch_calls(method.body[37].body[21])
    assert calls is not None
    ordinary = next(call for call in calls if call.func.attr == "compose")
    context = next(keyword for keyword in ordinary.keywords if keyword.arg == "retained_operation_context")
    assert ast.unparse(context.value) == "retained_operation_context"
    context.value = ast.Name(id="foreign_context", ctx=ast.Load())
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer complete executed method grammar changed " + TURN]
    monkeypatch.setitem(compose._REVIEWED_METHOD_SHAPES, TURN, compose._method_shape(compose._strip_inert_annotations(method, TURN)))
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer retained operation context argument changed"]


@pytest.mark.parametrize(
    ("index", "message"),
    (
        (1, "composer retained operation context admission changed"),
        (2, "composer pair admission guard changed"),
        (3, "composer owner, service, binding or context admission changed"),
    ),
)
@pytest.mark.parametrize("fault", ("alter", "move"))
def test_composer_admission_structure_refuses_drift_even_with_rebound_grammar(
    index: int, message: str, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
    method = fencing._reserve_node_at(units[1], "ComposerServiceImpl.compose")
    if fault == "alter":
        method.body[index].test = ast.Constant(value=False)
    else:
        method.body[index], method.body[index + 1] = method.body[index + 1], method.body[index]
    monkeypatch.setitem(compose._REVIEWED_METHOD_SHAPES, compose.SERVICE, compose._method_shape(method))
    assert compose.compose_proposal_local_transfers(units)[0] == [message]


@pytest.mark.parametrize("fault", ("authority", "binding", "join", "outcome", "error", "context", "move"))
def test_history_receiver_prefix_refuses_changed_transfer_without_body_hash(fault: str) -> None:
    method = fencing._reserve_node_at(_source_units((TURN,))[0], "_run_composer_turn")
    prefix = method.body[37].body[4:15] + method.body[37].body[16:21]
    assert receiver.receiver_transfer_prefix(prefix)
    if fault in {"authority", "binding"}:
        read = next(
            node
            for node in ast.walk(prefix[2])
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "_read_retained_operation_context"
        )
        keyword = next(item for item in read.keywords if item.arg == fault)
        keyword.value = ast.Name(id="foreign", ctx=ast.Load())
    elif fault == "join":
        prefix[4].value.value.args[0] = ast.Name(id="foreign_task", ctx=ast.Load())
    elif fault == "outcome":
        prefix[5].test = ast.Constant(value=False)
    elif fault == "error":
        prefix[6].body[-1] = ast.Pass()
    elif fault == "context":
        prefix[9].value = ast.Name(id="foreign_context", ctx=ast.Load())
    else:
        prefix[9], prefix[10] = prefix[10], prefix[9]
    assert not receiver.receiver_transfer_prefix(prefix)


@pytest.mark.parametrize("contract", ("compose", "route"))
@pytest.mark.parametrize("fault", ("foreign_receiver", "changed_ordinal"))
def test_reviewed_turn_grammar_matches_live_source_and_refuses_reserve_drift(contract: str, fault: str) -> None:
    if contract == "compose":
        units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
        check = compose.compose_proposal_local_transfers
        expected = "composer complete executed method grammar changed " + TURN
    else:
        units = _source_units((TURN, route.HELPERS))
        check = route.route_helper_local_contracts
        expected = "route helpers: complete finite grammar changed _run_composer_turn"
    failures, _ = check(units)
    assert failures == []
    unit = units[0]
    method = fencing._reserve_node_at(unit, "_run_composer_turn")
    reservations = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "reserve"
    ]
    assert len(reservations) == 1
    reservation = reservations[0]
    if fault == "foreign_receiver":
        reservation.func.value = ast.Name(id="foreign_work", ctx=ast.Load())
    else:
        ordinals = [keyword for keyword in reservation.keywords if keyword.arg == "transition_ordinal"]
        assert len(ordinals) == 1
        ordinals[0].value = ast.Constant(value=999)
    failures, _ = check(units)
    assert failures == [expected]


@pytest.mark.parametrize("fault", ("omit", "include"))
def test_provider_turn_contract_binds_private_transcript_filter(fault: str) -> None:
    unit = _source_units((TURN,))[0]
    actual = fencing._reserve_node_at(unit, "_run_composer_turn")
    expected = ast.parse(provider.CONTRACTS["elspeth.web.sessions.composer_turn:_run_composer_turn"]).body[0]
    assert provider._shape(actual) == provider._shape(expected)
    calls = [node for node in ast.walk(actual) if isinstance(node, ast.Call) and ast.unparse(node.func) == "service.get_messages"]
    assert len(calls) == 1
    call = calls[0]
    keywords = [keyword for keyword in call.keywords if keyword.arg == "include_private_evidence"]
    assert len(keywords) == 1 and isinstance(keywords[0].value, ast.Constant) and keywords[0].value.value is False
    if fault == "omit":
        call.keywords.remove(keywords[0])
    else:
        keywords[0].value = ast.Constant(value=True)
    assert provider._shape(actual) != provider._shape(expected)


def test_reviewed_terminal_namespace_matches_live_source_and_refuses_new_descriptor() -> None:
    units = _source_units((SERVICE, terminal.REQUIRED, terminal.WORKERS, terminal.AUTHORITY))
    failures, _ = terminal.terminal_sql_local_contracts(units)
    assert failures == []
    owner, _ = terminal.find(units[0].tree, terminal.CLASS)
    owner.body.append(ast.parse("def reserve(self, request):\n    return request\n").body[0])
    ast.fix_missing_locations(units[0].tree)
    failures, _ = terminal.terminal_sql_local_contracts(units)
    assert "terminal SQL transfer: finite current service declaration namespace changed" in failures


@pytest.mark.parametrize("branch_name", ("compose", "compose_recovery"))
@pytest.mark.parametrize("fault", ("receiver", "owner", "work", "context", "await", "escape"))
def test_both_composer_dispatch_branches_require_the_retained_authority(branch_name: str, fault: str) -> None:
    units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
    assert compose.compose_proposal_local_transfers(units)[0] == []
    assert compose._source3_local_transfers(units)[0] == []
    method = fencing._reserve_node_at(units[0], "_run_composer_turn")
    invoke = method.body[37].body[21]
    calls = receiver.composer_dispatch_calls(invoke)
    assert calls is not None and {call.func.attr for call in calls} == {"compose", "compose_recovery"}
    call = next(call for call in calls if call.func.attr == branch_name)
    if fault == "receiver":
        call.func.value = ast.Name(id="foreign_composer", ctx=ast.Load())
    elif fault in {"owner", "work", "context"}:
        keyword_name = {"owner": "provider_owner", "work": "required_work", "context": "session_operation_context"}[fault]
        keyword = next(item for item in call.keywords if item.arg == keyword_name)
        keyword.value = ast.Name(id="foreign_authority", ctx=ast.Load())
    else:
        branch = invoke.body[1]
        statements = branch.body if branch_name == "compose_recovery" else branch.orelse
        statements.insert(
            len(statements) - 1,
            ast.parse("await unreviewed_effect()" if fault == "await" else "unreviewed_effect(composer)").body[0],
        )
    assert receiver.composer_dispatch_calls(invoke) is None
    expected = (
        "composer turn compose call universe changed"
        if fault == "receiver"
        else "composer turn exact paired call arguments or closed branches changed"
    )
    assert compose._source3_local_transfers(units)[0] == [expected]
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer complete executed method grammar changed " + TURN]


def test_recovery_callee_cannot_replace_its_nominal_owner_guard() -> None:
    units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
    assert compose.compose_proposal_local_transfers(units)[0] == []
    method = fencing._reserve_node_at(units[1], "ComposerServiceImpl.compose_recovery")
    guard = next(
        node
        for node in method.body
        if isinstance(node, ast.If) and ast.unparse(node.test) == "type(provider_owner) is not ProviderInvocationOwner"
    )
    guard.test = ast.Constant(value=False)
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer recovery complete executed method grammar changed"]


def test_missing_shared_prerequisite_refuses_reserve_instead_of_granting_certificate() -> None:
    unit = fencing._parse_source("src/elspeth/web/reserve_control.py", "def run(required_work):\n    required_work.reserve(request)\n")
    proof = fencing._reserve_ReserveProof((unit,))
    assert proof.failures
    call = next(node for node in ast.walk(unit.tree) if isinstance(node, ast.Call))
    assert proof.prove(unit, call) is None
    assert any("unknown mutation receiver .reserve" in row for row in fencing._mutation_callable_escapes((unit,)))


def test_real_landscape_reserve_still_requires_current_coordination_authority() -> None:
    unit = fencing._parse_source(
        "src/elspeth/web/reserve_control.py",
        "from elspeth.core.landscape.execution.sink_effects import SinkEffectRepository\n"
        "def run(effects: SinkEffectRepository):\n"
        "    effects.reserve(request)\n",
    )
    calls = fencing.scan_production_calls((unit,))
    assert len(calls) == 1
    assert calls[0].method == "reserve"
    assert fencing._caller_authority_violations((unit,))


@pytest.mark.parametrize("codec", ("encode_composer_failure_cursor", "decode_composer_failure_cursor"))
def test_landscape_to_domain_cursor_codec_remains_a_sessions_dependency(codec: str) -> None:
    unit = fencing._parse_source(
        fencing._RUN_LIFECYCLE_PATH,
        f"from elspeth.web.sessions.composer_operations import {codec}\n"
        f"class RunLifecycleRepository:\n    def complete_run(self, value):\n        return {codec}(value)\n",
    )
    assert fencing._cross_database_violations((unit,))


@pytest.mark.parametrize(
    "fault",
    (
        None,
        "early_capture",
        "suspend",
        "escape",
        "rebind",
        "deferred_default",
        "pre_suspend",
        "join",
        "supplier",
        "unknown_definition",
        "title_default",
        "title_escape",
    ),
)
def test_captured_composer_receiver_interval_is_closed_after_history(fault: str | None) -> None:
    paths = (
        TURN,
        "src/elspeth/web/sessions/routes/_helpers.py",
        "src/elspeth/web/sessions/composer_app_services.py",
        "src/elspeth/web/sessions/_auto_title.py",
    )
    units = _source_units(paths)
    if fault is not None:
        original = units[0]
        source = original.source
        capture = "        composer = services.composer_service\n"
        if fault == "early_capture":
            assert source.count(capture) == 1
            source = source.replace(capture, "").replace(
                "        from openai import OpenAIError\n", capture + "        from openai import OpenAIError\n"
            )
        elif fault == "deferred_default":
            source = source.replace(
                "async def settle_post_provider(result: ComposerResult | GraphRecoveryResponse)",
                "async def settle_post_provider(result: ComposerResult | GraphRecoveryResponse = mutate_receiver())",
            )
        elif fault == "pre_suspend":
            source = source.replace(capture, "        await foreign_effect()\n" + capture)
        elif fault == "join":
            source = source.replace(
                "history_outcome, _ = await _join_freeform_owned_task(history_task,",
                "history_outcome, _ = await foreign_join(history_task,",
            )
        elif fault == "supplier":
            source = source.replace(
                "_read_retained_operation_context(\n                    authority=", "foreign_read(\n                    authority="
            )
        elif fault == "unknown_definition":
            source = source.replace(capture, "        def foreign(default=mutate_receiver()):\n            pass\n" + capture)
        elif fault == "title_default":
            source = source.replace("async def run_owned_title()", "async def run_owned_title(value=mutate_receiver())")
        elif fault == "title_escape":
            source = source.replace(
                "async def run_owned_title() -> None:\n", "async def run_owned_title() -> None:\n                foreign_effect(composer)\n"
            )
        else:
            extra = {"suspend": "await foreign_effect()", "escape": "foreign_effect(composer)", "rebind": "composer = foreign"}[fault]
            source = source.replace(capture, capture + "        " + extra + "\n")
        assert source != original.source
        units = (fencing._parse_source(TURN, source), *units[1:])
    turn = fencing._reserve_node_at(units[0], "_run_composer_turn")
    dispatch = next(node for node in ast.walk(turn) if isinstance(node, ast.Call) and ast.unparse(node.func) == "composer.compose")
    dto = units[2]
    factory = fencing._reserve_node_at(dto, "composer_app_services")
    read = next(keyword.value for keyword in factory.body[-1].value.keywords if keyword.arg == "composer_service")
    failures, receipt = fencing._reserve_captured_composer_receiver(units, (dto.path, id(read)), (TURN, id(dispatch)))
    if fault is None:
        assert failures == []
        assert receipt is not None
    else:
        assert failures
        assert receipt is None
