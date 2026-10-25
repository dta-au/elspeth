"""Fast prerequisites and adversarial controls for the expensive reserve proof."""

import ast
import copy
from pathlib import Path

import pytest
from tests.helpers import web_reserve_compose_proposal_transfers as compose
from tests.helpers import web_reserve_composer_receiver as receiver
from tests.helpers import web_reserve_family_contracts as family
from tests.helpers import web_reserve_provider_mint_contract as provider
from tests.helpers import web_reserve_route_helper as route
from tests.helpers import web_reserve_terminal_sql_transfers as terminal
from tests.helpers import web_reserve_worker_caller as worker_caller
from tests.helpers import web_reserve_worker_six as worker_six
from tests.helpers import web_worker_whole_scope_origin as worker_origin
from tests.unit.architecture import test_web_landscape_mutation_fencing as fencing

ROOT = Path(__file__).parents[3]
TURN = "src/elspeth/web/sessions/composer_turn.py"
SERVICE = "src/elspeth/web/sessions/service.py"
PROTOCOL = "src/elspeth/web/sessions/protocol.py"
COMPOSER_PROTOCOL = "src/elspeth/web/composer/protocol.py"
TRANSCRIPT_READER = "src/elspeth/web/composer/transcript_blob_reader.py"


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
    history = turn.body[36].body[6]
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
        turn.body[36].body[8].value.value.args[0] = ast.Name(id="foreign_task", ctx=ast.Load())
    elif fault == "cancellation":
        turn.body[36].body[11].body[0] = ast.Pass()
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
        TRANSCRIPT_READER,
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


def test_transcript_blob_reader_call_is_under_the_reviewed_source_guard() -> None:
    """compose's inline history-blob read moved into CurrentTranscriptBlobReader.__call__.

    The compose method shape no longer contains that body, so the reviewed
    full-source guard must: an unreviewed edit inside __call__ refuses.
    """
    paths = (*fencing._reserve_FULL_SOURCE_PATHS, *fencing._reserve_CANCEL_CONSUMERS)
    sources = {item: (ROOT / item).read_text() for item in paths}
    assert TRANSCRIPT_READER in fencing._reserve_FULL_SOURCE_PATHS
    assert fencing._reserve_full_source_failures(sources) == []
    tree = ast.parse(sources[TRANSCRIPT_READER])
    reader = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CurrentTranscriptBlobReader")
    call = next(node for node in reader.body if isinstance(node, ast.FunctionDef) and node.name == "__call__")
    call.body.insert(0, ast.parse("unreviewed_effect()\n").body[0])
    sources[TRANSCRIPT_READER] = ast.unparse(tree)
    assert fencing._reserve_full_source_failures(sources) == ["full source changed " + TRANSCRIPT_READER]


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
    calls = receiver.composer_dispatch_calls(method.body[36].body[24])
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


@pytest.mark.parametrize(
    "fault",
    ("message", "required_work", "provider_owner", "session_operation_context", "retained_operation_context", "turn_evidence", "branch"),
)
def test_common_compose_entry_binds_retained_loop_arguments_and_excludes_extra_branches(fault: str) -> None:
    units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
    assert compose.compose_proposal_local_transfers(units)[0] == []
    method = fencing._reserve_node_at(units[1], "ComposerServiceImpl.compose")
    calls = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_compose_loop"
    ]
    assert len(calls) == 1
    call = calls[0]
    if fault == "message":
        assert ast.unparse(call.args[0]) == "message"
        call.args[0] = ast.Name(id="foreign_message", ctx=ast.Load())
    elif fault == "branch":
        returns = [node for node in ast.walk(method) if isinstance(node, ast.Return) and call in ast.walk(node)]
        assert len(returns) == 1
        blocks = [node.body for node in ast.walk(method) if isinstance(node, ast.Try) and returns[0] in node.body]
        assert len(blocks) == 1
        blocks[0].insert(
            blocks[0].index(returns[0]), ast.parse("if unreviewed_predicate():\n    return await unreviewed_effect()\n").body[0]
        )
    else:
        keywords = [keyword for keyword in call.keywords if keyword.arg == fault]
        assert len(keywords) == 1 and ast.unparse(keywords[0].value) == fault
        keywords[0].value = ast.Name(id="foreign_custody", ctx=ast.Load())
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer complete executed method grammar changed " + compose.SERVICE]


@pytest.mark.parametrize("fault", ("authority", "binding", "join", "outcome", "error", "context", "move"))
def test_history_receiver_prefix_refuses_changed_transfer_without_body_hash(fault: str) -> None:
    method = fencing._reserve_node_at(_source_units((TURN,))[0], "_run_composer_turn")
    prefix = method.body[36].body[4:18] + method.body[36].body[19:24]
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


@pytest.mark.parametrize(
    "fault",
    (None, "session", "message", "lifecycle", "eligibility", "settings", "owner", "factory", "before_history", "after_receiver", "effect"),
)
def test_common_cost_capture_binds_original_identity_and_closed_receiver_prefix(fault: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
    assert compose.compose_proposal_local_transfers(units)[0] == []
    method = fencing._reserve_node_at(units[0], "_run_composer_turn")
    block = method.body[36].body
    prefix = block[4:18] + block[19:24]
    assert receiver.receiver_transfer_prefix(prefix)
    route_units = (units[0], *_source_units((route.HELPERS,)))
    assert route.route_helper_local_contracts(route_units)[0] == []
    if fault is None:
        return
    capture = block[15]
    if fault in {"session", "message", "lifecycle", "eligibility", "settings"}:
        field = {
            "session": "session_id",
            "message": "request_id",
            "lifecycle": "lifecycle_version",
            "eligibility": "initially_empty",
            "settings": "settings",
        }[fault]
        keyword = next(k for k in capture.value.args[0].keywords if k.arg == field)
        keyword.value = ast.Constant(value=2) if fault == "lifecycle" else ast.Name(id="foreign", ctx=ast.Load())
    elif fault == "owner":
        capture.value.func.value = ast.Name(id="foreign_owner", ctx=ast.Load())
    elif fault == "factory":
        capture.value.args[0].func = ast.Name(id="foreign_factory", ctx=ast.Load())
    elif fault == "before_history":
        block[14], block[15] = block[15], block[14]
    elif fault == "after_receiver":
        block[15], block[18] = block[18], block[15]
    else:
        block.insert(15, ast.parse("foreign_effect(provider_owner)").body[0])
    assert not receiver.receiver_transfer_prefix(block[4:18] + block[19:24])
    assert route.route_helper_local_contracts(route_units)[0] == ["route helpers: complete finite grammar changed _run_composer_turn"]
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer complete executed method grammar changed " + TURN]
    monkeypatch.setitem(compose._REVIEWED_METHOD_SHAPES, TURN, compose._method_shape(compose._strip_inert_annotations(method, TURN)))
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer turn original request cost capture changed"]


@pytest.mark.parametrize(
    "fault", (None, "reuse", "capture_identity", "capture_lifecycle", "bind", "validate", "configuration", "effect", "after_loop")
)
def test_common_cost_entry_reuses_and_validates_policy_before_loop(fault: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
    assert compose.compose_proposal_local_transfers(units)[0] == []
    method = fencing._reserve_node_at(units[1], "ComposerServiceImpl.compose")
    captures = [n for n in ast.walk(method) if isinstance(n, ast.Call) and ast.unparse(n.func) == "capture_common_provider_cost"]
    assert len(captures) == 1
    blocks = [
        n.body
        for n in ast.walk(method)
        if isinstance(n, ast.Try) and any(isinstance(v, ast.If) and captures[0] in ast.walk(v) for v in n.body)
    ]
    assert len(blocks) == 1
    block = blocks[0]
    admission = block[0]
    if fault is None:
        return
    if fault == "reuse":
        admission.body[1].test = ast.Constant(value=True)
    elif fault in {"capture_identity", "capture_lifecycle"}:
        keyword = next(k for k in captures[0].keywords if k.arg == ("request_id" if fault == "capture_identity" else "lifecycle_version"))
        keyword.value = ast.Name(id="foreign", ctx=ast.Load())
    elif fault == "bind":
        admission.body[1].body[1].value.args[0] = ast.Name(id="foreign", ctx=ast.Load())
    elif fault in {"validate", "configuration"}:
        admission.body[2 if fault == "validate" else 3] = ast.Pass()
    elif fault == "effect":
        block.insert(1, ast.parse("foreign_effect(provider_owner)").body[0])
    else:
        block.reverse()
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer complete executed method grammar changed " + compose.SERVICE]
    monkeypatch.setitem(compose._REVIEWED_METHOD_SHAPES, compose.SERVICE, compose._method_shape(method))
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer common entry retained cost policy admission changed"]


@pytest.mark.parametrize("fault", (None, "omit_cost", "foreign", "extra", "catch_all", "reorder"))
def test_common_cost_refusal_handler_is_the_exact_closed_pair(fault: str | None) -> None:
    units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
    assert compose.compose_proposal_local_transfers(units)[0] == []
    method = fencing._reserve_node_at(units[0], "_run_composer_turn")
    invoke = method.body[36].body[24]
    assert receiver.composer_title_prelude(invoke) is not None
    handler = invoke.handlers[7]
    assert ast.unparse(handler.type) == "(PipelinePlannerError, CommonProviderCostRefused)"
    if fault is None:
        return
    if fault == "omit_cost":
        handler.type = ast.Name(id="PipelinePlannerError", ctx=ast.Load())
    elif fault == "foreign":
        handler.type.elts[1] = ast.Name(id="ForeignRefusal", ctx=ast.Load())
    elif fault == "extra":
        handler.type.elts.append(ast.Name(id="Exception", ctx=ast.Load()))
    elif fault == "catch_all":
        handler.type = ast.Name(id="BaseException", ctx=ast.Load())
    else:
        handler.type.elts.reverse()
    assert receiver.composer_title_prelude(invoke) is None
    assert receiver.composer_dispatch_calls(invoke) is None
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer complete executed method grammar changed " + TURN]


@pytest.mark.parametrize("binding", ("capture_common_provider_cost", "CommonProviderCostRefused", "state_is_structurally_empty"))
def test_common_cost_capture_import_origins_remain_closed(binding: str) -> None:
    units = _provider_mint_units()
    assert provider.provider_mint_local_contracts(units)[0] == []
    turn = next(unit for unit in units if unit.path == TURN)
    selected = next(n for n in turn.tree.body if isinstance(n, ast.ImportFrom) and any(a.name == binding for a in n.names))
    module = selected.module
    assert receiver.imported(turn.tree, binding, module, binding)
    turn.tree.body.append(ast.parse(f"from foreign import {binding}").body[0])
    assert not receiver.imported(turn.tree, binding, module, binding)
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == ["provider mint selected constructor/family/binding import origin changed elspeth.web.sessions.composer_turn"]
    assert receipt is None


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
    invoke = method.body[36].body[24]
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
        branch = invoke.body[2]
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


@pytest.mark.parametrize(
    "fault",
    (
        "mount",
        "mode",
        "user",
        "custody",
        "producer",
        "supplier",
        "default",
        "receiver_escape",
        "services_write",
        "join",
        "join_wait",
        "join_await",
        "late_join",
        "budget",
        "deadline",
        "refusal_handler",
        "refusal_audit",
        "refusal_status",
    ),
)
def test_guarded_title_interval_refuses_drift_even_with_rebound_grammar(fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
    method = fencing._reserve_node_at(units[0], "_run_composer_turn")
    invoke = method.body[36].body[24]
    title = receiver.composer_title_prelude(invoke)
    assert title is not None
    assert receiver.composer_dispatch_calls(invoke) is not None
    call = next(
        node for node in ast.walk(title.body[2]) if isinstance(node, ast.Call) and ast.unparse(node.func) == "maybe_auto_title_session"
    )
    if fault in {"mount", "mode", "user", "custody"}:
        argument = {
            "mount": "prompt_guard_mount",
            "mode": "prompt_guard_mode",
            "user": "user_message",
            "custody": "provider_custody",
        }[fault]
        next(keyword for keyword in call.keywords if keyword.arg == argument).value = ast.Name(id="foreign", ctx=ast.Load())
    elif fault == "producer":
        title.body[1].value.func.value = ast.Name(id="foreign_work", ctx=ast.Load())
    elif fault == "supplier":
        call.func = ast.Name(id="foreign_title", ctx=ast.Load())
    elif fault == "default":
        title.body[2].args.args.append(ast.arg(arg="value"))
        title.body[2].args.defaults.append(ast.parse("mutate_receiver()", mode="eval").body)
    elif fault in {"receiver_escape", "services_write"}:
        statement = "foreign_effect(composer)" if fault == "receiver_escape" else "services.composer_service = foreign"
        title.body[2].body.insert(0, ast.parse(statement).body[0])
    elif fault == "join":
        title.body[4].value.value.func = ast.Name(id="foreign_join", ctx=ast.Load())
    elif fault == "join_wait":
        next(keyword for keyword in title.body[4].value.value.keywords if keyword.arg == "wait_until_complete").value = ast.Constant(
            value=False
        )
    elif fault == "join_await":
        title.body[4].value = title.body[4].value.value
    elif fault == "late_join":
        invoke.body.insert(3, title.body.pop(4))
    elif fault == "budget":
        title.body[5].value = ast.Constant(value=999.0)
    elif fault == "deadline":
        title.body[6].test = ast.Constant(value=False)
    elif fault == "refusal_handler":
        invoke.handlers[9].type = ast.Name(id="ForeignRefusal", ctx=ast.Load())
    elif fault == "refusal_audit":
        invoke.handlers[9].body[3].body[0] = ast.Pass()
    else:
        invoke.handlers[9].body[-1].exc.keywords[0].value = ast.Constant(value=200)
    ast.fix_missing_locations(units[0].tree)
    assert receiver.composer_title_prelude(invoke) is None
    assert receiver.composer_dispatch_calls(invoke) is None
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer complete executed method grammar changed " + TURN]
    monkeypatch.setitem(compose._REVIEWED_METHOD_SHAPES, TURN, compose._method_shape(compose._strip_inert_annotations(method, TURN)))
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer turn exact paired call arguments or closed branches changed"]


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
        "title_services_write",
        "title_mount_getter",
    ),
)
def test_captured_composer_receiver_interval_is_closed_after_history(fault: str | None) -> None:
    paths = (
        TURN,
        "src/elspeth/web/sessions/routes/_helpers.py",
        "src/elspeth/web/sessions/composer_app_services.py",
        "src/elspeth/web/sessions/_auto_title.py",
        compose.SERVICE,
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
                "async def run_owned_title() -> None:\n",
                "async def run_owned_title() -> None:\n                    foreign_effect(composer)\n",
            )
        elif fault == "title_services_write":
            source = source.replace(
                "async def run_owned_title() -> None:\n",
                "async def run_owned_title() -> None:\n                    services.composer_service = foreign\n",
            )
        elif fault == "title_mount_getter":
            getter_unit = units[4]
            getter = fencing._reserve_node_at(getter_unit, "ComposerServiceImpl.composer_prompt_guard_mount")
            getter.body.insert(0, ast.parse("foreign_effect(self)").body[0])
            ast.fix_missing_locations(getter_unit.tree)
        else:
            extra = {"suspend": "await foreign_effect()", "escape": "foreign_effect(composer)", "rebind": "composer = foreign"}[fault]
            source = source.replace(capture, capture + "        " + extra + "\n")
        if fault != "title_mount_getter":
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


DIAGNOSTICS = "src/elspeth/web/execution/routes.py"
DIAGNOSTICS_CALLER = "create_execution_router.evaluate_run_diagnostics"
DIAGNOSTICS_CONTRACT = "elspeth.web.execution.routes:" + DIAGNOSTICS_CALLER


def _provider_mint_units() -> tuple[fencing.SourceUnit, ...]:
    paths = {"src/" + qualified.split(":", 1)[0].replace(".", "/") + ".py" for qualified in provider.CONTRACTS}
    # Include the actual route even before its missing contract is repaired.
    return _source_units(tuple(sorted({*paths, DIAGNOSTICS})))


def _provider_mint_rows(units, sites):
    return {(unit.path, ast.unparse(node)) for unit in units for node in ast.walk(unit.tree) if (unit.path, id(node)) in sites}


@pytest.mark.parametrize(
    "fault",
    ("nominal_budget", "replacement", "late_primary", "late_advisor", "exclude_title", "exclude_planner", "cover_primary", "cover_advisor"),
)
def test_provider_cost_contract_preserves_owner_binding_and_family_policy(fault: str) -> None:
    units = _provider_mint_units()
    assert provider.provider_mint_local_contracts(units)[0] == []
    quota = next(unit for unit in units if provider._module(unit) == provider.QUOTA)
    bind = provider._node(quota, "ProviderInvocationOwner.bind_cost_budget")
    mint = provider._node(quota, "ProviderInvocationOwner.mint")
    assert bind is not None and mint is not None
    if fault == "nominal_budget":
        bind.body[0] = ast.Pass()
    elif fault == "replacement":
        bind.body[1].body[0] = ast.Pass()
    elif fault in {"late_primary", "late_advisor"}:
        bind.body[2].test.values[0 if fault == "late_primary" else 1] = ast.Constant(value=0)
    else:
        returned = mint.body[-1].value
        policy = next(keyword.value for keyword in returned.keywords if keyword.arg == "cost_budget")
        families = policy.test.comparators[0].elts
        if fault.startswith("exclude_"):
            families.append(ast.parse("ProviderInvocationFamily." + fault.removeprefix("exclude_").upper(), mode="eval").body)
        else:
            families.pop(0 if fault == "cover_primary" else 1)
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == ["provider mint complete producer/retention/caller contract changed " + provider.QUOTA + ":ProviderInvocationOwner"]
    assert receipt is None


@pytest.mark.parametrize("fault", ("required_work", "context", "compose", "nominal_budget", "accountability"))
def test_provider_cost_contract_preserves_custody_constructor_guards(fault: str) -> None:
    units = _provider_mint_units()
    assert provider.provider_mint_local_contracts(units)[0] == []
    quota = next(unit for unit in units if provider._module(unit) == provider.QUOTA)
    assert family._check_contracts(quota, family._provider_contracts())[0] == []
    constructor = provider._node(quota, "ProviderCallCustody.__init__")
    assert constructor is not None
    if fault == "accountability":
        constructor.body[5].value = ast.Constant(value=True)
    else:
        constructor.body[{"required_work": 0, "context": 1, "compose": 2, "nominal_budget": 3}[fault]] = ast.Pass()
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == ["provider mint complete producer/retention/caller contract changed " + provider.QUOTA + ":ProviderCallCustody"]
    assert receipt is None
    failures, _ = family._check_contracts(quota, family._provider_contracts())
    assert failures == [quota.path + ":ProviderCallCustody.__init__: closed execution contract changed (guard/transfer/effect/branch)"]


@pytest.mark.parametrize(
    "fault",
    ("before_sql", "before_projection", "foreign_call", "accountability", "early_clear", "swallow_failure", "sql", "context", "projection"),
)
def test_provider_cost_contract_requires_successful_sql_and_projection_before_charge(fault: str) -> None:
    units = _provider_mint_units()
    assert provider.provider_mint_local_contracts(units)[0] == []
    quota = next(unit for unit in units if provider._module(unit) == provider.QUOTA)
    assert family._check_contracts(quota, family._provider_contracts())[0] == []
    settle = provider._node(quota, "ProviderCallCustody.settle")
    assert settle is not None
    charge = next(
        statement
        for statement in settle.body
        if isinstance(statement, ast.If)
        and any(
            isinstance(node, ast.Call) and ast.unparse(node.func) == "self._cost_budget.consume_settled" for node in ast.walk(statement)
        )
    )
    required = next(statement for statement in settle.body if isinstance(statement, ast.Try))
    projection = settle.body[settle.body.index(charge) - 1]
    consume = charge.body[0].value
    if fault in {"before_sql", "before_projection"}:
        settle.body.remove(charge)
        settle.body.insert(settle.body.index(required if fault == "before_sql" else projection), charge)
    elif fault == "foreign_call":
        consume.args[0] = ast.Name(id="foreign", ctx=ast.Load())
    elif fault == "accountability":
        consume.keywords[0].value = ast.Constant(value=True)
    elif fault == "early_clear":
        clear = settle.body[settle.body.index(charge) + 2]
        assert ast.unparse(clear) == "self.call = None"
        settle.body.remove(clear)
        settle.body.insert(settle.body.index(required), clear)
    elif fault == "swallow_failure":
        required.handlers[0].body[-1] = ast.Pass()
    elif fault in {"sql", "context"}:
        finish = next(
            node
            for node in ast.walk(required)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "self.service.finish_provider_attempt"
        )
        field = "required_work" if fault == "sql" else "session_operation_context"
        next(keyword for keyword in finish.keywords if keyword.arg == field).value = ast.Name(id="foreign", ctx=ast.Load())
    else:
        projection.body[-1] = ast.Pass()
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == ["provider mint complete producer/retention/caller contract changed " + provider.QUOTA + ":ProviderCallCustody"]
    assert receipt is None
    failures, _ = family._check_contracts(quota, family._provider_contracts())
    assert failures == [quota.path + ":ProviderCallCustody.settle: closed execution contract changed (guard/transfer/effect/branch)"]


@pytest.mark.parametrize("field", ("_cost_budget", "_cost_response_accountable"))
@pytest.mark.parametrize("fault", ("remove", "replace"))
def test_provider_cost_contract_binds_exact_owned_custody_slots(field: str, fault: str) -> None:
    paths = (
        "src/elspeth/web/required_work.py",
        "src/elspeth/web/composer/provider_quota.py",
        "src/elspeth/web/operator_telemetry_dispatch.py",
        "src/elspeth/web/operator_telemetry_installation.py",
        "src/elspeth/web/operator_telemetry_custody.py",
        "src/elspeth/web/operator_telemetry.py",
        "src/elspeth/web/coordination/lifecycle.py",
    )
    units = _source_units(paths)
    assert family.family_local_contracts(units)[0] == []
    quota = units[1]
    custody = provider._node(quota, "ProviderCallCustody")
    assert custody is not None
    slots = next(statement.value.elts for statement in custody.body if isinstance(statement, ast.Assign))
    slot = next(value for value in slots if value.value == field)
    if fault == "remove":
        slots.remove(slot)
    else:
        slot.value = "foreign"
    failures, _ = family.family_local_contracts(units)
    assert failures == ["ProviderCallCustody: owned slot/descriptor namespace changed"]


def test_provider_cost_contract_refuses_foreign_budget_import_with_unchanged_class_grammar() -> None:
    units = _provider_mint_units()
    assert provider.provider_mint_local_contracts(units)[0] == []
    quota = next(unit for unit in units if provider._module(unit) == provider.QUOTA)
    quota.tree.body.append(ast.parse("from foreign import CommonProviderCostBudget").body[0])
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == ["provider mint selected constructor/family/binding import origin changed " + provider.QUOTA]
    assert receipt is None


@pytest.mark.parametrize("fault", (None, "absent_budget", "snapshot", "policy", "value", "effect", "mutation", "descriptor"))
def test_provider_cost_completion_limit_binds_exact_owned_read_and_absent_policy(fault: str | None) -> None:
    units = _provider_mint_units()
    assert provider.provider_mint_local_contracts(units)[0] == []
    quota = next(unit for unit in units if provider._module(unit) == provider.QUOTA)
    assert family._check_contracts(quota, family._provider_contracts())[0] == []
    method = provider._node(quota, "ProviderCallCustody.cost_completion_limit")
    assert method is not None
    if fault is None:
        return
    if fault == "absent_budget":
        method.body[0].test = ast.Constant(value=False)
    elif fault == "snapshot":
        method.body[1].value.value.func = ast.Name(id="foreign_snapshot", ctx=ast.Load())
    elif fault == "policy":
        method.body[2].value.test = ast.Constant(value=True)
    elif fault == "value":
        method.body[2].value.body = ast.Constant(value=1)
    elif fault == "effect":
        method.body.insert(1, ast.parse("foreign_effect(self)").body[0])
    elif fault == "mutation":
        method.body.insert(1, ast.parse("self._cost_budget = foreign").body[0])
    else:
        method.decorator_list.append(ast.Name(id="property", ctx=ast.Load()))
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == ["provider mint complete producer/retention/caller contract changed " + provider.QUOTA + ":ProviderCallCustody"]
    assert receipt is None
    failures, _ = family._check_contracts(quota, family._provider_contracts())
    kind = (
        "binding/signature/descriptor changed"
        if fault == "descriptor"
        else "closed execution contract changed (guard/transfer/effect/branch)"
    )
    assert failures == [quota.path + ":ProviderCallCustody.cost_completion_limit: " + kind]


@pytest.mark.parametrize("fault", ("identity", "clock", "elapsed"))
def test_provider_mint_contract_binds_attempt_identity_and_database_clock(fault: str) -> None:
    units = _provider_mint_units()
    assert provider.provider_mint_local_contracts(units)[0] == []
    quota = next(unit for unit in units if provider._module(unit) == provider.QUOTA)
    binder = provider._node(quota, "_bind_attempt_call")
    assert binder is not None
    returned = binder.body[-1]
    assert isinstance(returned, ast.Return) and isinstance(returned.value, ast.Call)
    field = {"identity": "call_id", "clock": "started_at", "elapsed": "finished_at"}[fault]
    keyword = next(item for item in returned.value.keywords if item.arg == field)
    keyword.value = ast.Name(id="foreign", ctx=ast.Load())
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == [
        "provider mint complete producer/retention/caller contract changed elspeth.web.composer.provider_quota:_bind_attempt_call"
    ]
    assert receipt is None


@pytest.mark.parametrize("binding", ("time", "replace", "timedelta"))
def test_provider_mint_attempt_clock_dependencies_refuse_import_replacement(binding: str) -> None:
    units = _provider_mint_units()
    assert provider.provider_mint_local_contracts(units)[0] == []
    quota = next(unit for unit in units if provider._module(unit) == provider.QUOTA)
    quota.tree.body.append(ast.parse(f"from foreign import {binding}").body[0])
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == ["provider mint selected constructor/family/binding import origin changed elspeth.web.composer.provider_quota"]
    assert receipt is None


def test_provider_mint_contract_covers_actual_diagnostics_caller_without_closing_common_premises() -> None:
    units = _provider_mint_units()
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == []
    assert receipt is not None
    assert _provider_mint_rows(units, receipt["owner_constructor_calls"]) == {
        (TURN, "ProviderInvocationOwner(service=service, required_work=provider_binding)"),
        (
            DIAGNOSTICS,
            "ProviderInvocationOwner(service=session_service, required_work=RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN))",
        ),
    }
    assert _provider_mint_rows(units, receipt["mint_calls"]) == {
        (TURN, "provider_owner.mint(ProviderInvocationFamily.TITLE)"),
        (DIAGNOSTICS, "owner.mint(ProviderInvocationFamily.PRIMARY)"),
        ("src/elspeth/web/composer/pipeline_planner.py", "provider_owner.mint(ProviderInvocationFamily.PLANNER)"),
        ("src/elspeth/web/composer/service.py", "provider_owner.mint(ProviderInvocationFamily.PRIMARY)"),
        ("src/elspeth/web/composer/advisor_checkpoint.py", "provider_owner.mint(ProviderInvocationFamily.ADVISOR)"),
    }
    unit = next(item for item in units if item.path == DIAGNOSTICS)
    endpoint = provider._node(unit, DIAGNOSTICS_CALLER)
    assert endpoint is not None
    assert (DIAGNOSTICS, id(endpoint)) in receipt["caller_regions"]
    dependencies = [row for row in receipt["dependencies"] if row["unit_path"] == DIAGNOSTICS]
    assert {row["node_id"] for row in dependencies} == {id(node) for node in ast.walk(endpoint) if isinstance(node, ast.Call)}
    assert all(row["qualification"] == "COMMON_ORIGIN_RECEIVER_EFFECT_PROOF_REQUIRED" for row in dependencies)
    assert all(row["execution_role"] == "UNCLASSIFIED_COMMON_EXECUTION_ROLE_REQUIRED" for row in dependencies)
    member_dependencies = [row for row in receipt["receiver_dependencies"] if row["unit_path"] == DIAGNOSTICS]
    assert {row["node_id"] for row in member_dependencies} == {id(node) for node in ast.walk(endpoint) if isinstance(node, ast.Attribute)}
    assert all(row["qualification"] == "COMMON_SOURCE_RECEIVER_MEMBER_EFFECT_PROOF_REQUIRED" for row in member_dependencies)
    assert receipt["remaining_premises"] == (
        "UNKNOWN_COMMON_SOURCE_IMPORT_BINDING_CLOSURE",
        "UNKNOWN_COMMON_RECEIVER_MEMBER_OUTSIDE_EFFECT_CLOSURE",
        "UNKNOWN_REQUIRED_BINDING_COORDINATOR_AUTHORITY_CONTEXT_CUSTODY",
    )
    quota = next(item for item in units if provider._module(item) == provider.QUOTA)
    mint = provider._node(quota, "ProviderInvocationOwner.mint")
    assert mint is not None
    assert receipt["validated_transfer_methods"] == frozenset({(quota.path, id(mint))})


@pytest.mark.parametrize(
    "fault",
    (
        "service",
        "coordinator",
        "role",
        "lease_authority",
        "context",
        "receiver",
        "family",
        "completion",
        "audit",
        "refusal_status",
        "dispatch",
    ),
)
def test_provider_mint_diagnostics_contract_refuses_authority_receiver_and_audit_drift(fault: str) -> None:
    units = _provider_mint_units()
    assert provider.provider_mint_local_contracts(units)[0] == []
    unit = next(item for item in units if item.path == DIAGNOSTICS)
    endpoint = provider._node(unit, DIAGNOSTICS_CALLER)
    assert endpoint is not None
    calls = {
        expression: next(node for node in ast.walk(endpoint) if isinstance(node, ast.Call) and ast.unparse(node.func) == expression)
        for expression in (
            "ProviderInvocationOwner",
            "RequiredWorkBinding",
            "SessionOperationLease.acquire",
            "owner.mint",
            "composer.explain_run_diagnostics",
            "coordinator.assert_completed",
            "_persist_run_diagnostics_llm_calls",
        )
    }
    foreign = ast.Name(id="foreign", ctx=ast.Load())
    if fault == "service":
        next(keyword for keyword in calls["ProviderInvocationOwner"].keywords if keyword.arg == "service").value = foreign
    elif fault == "coordinator":
        calls["RequiredWorkBinding"].args[0] = foreign
    elif fault == "role":
        calls["RequiredWorkBinding"].args[3] = ast.parse("RequiredWorkRole.TITLE", mode="eval").body
    elif fault == "lease_authority":
        calls["SessionOperationLease.acquire"].args[0] = foreign
    elif fault == "context":
        next(
            keyword for keyword in calls["composer.explain_run_diagnostics"].keywords if keyword.arg == "session_operation_context"
        ).value = foreign
    elif fault == "receiver":
        calls["owner.mint"].func.value = foreign
    elif fault == "family":
        calls["owner.mint"].args[0] = ast.parse("ProviderInvocationFamily.TITLE", mode="eval").body
    elif fault == "completion":
        calls["coordinator.assert_completed"].func = foreign
    elif fault == "audit":
        calls["_persist_run_diagnostics_llm_calls"].func = foreign
    elif fault == "refusal_status":
        handler = next(
            node
            for node in ast.walk(endpoint)
            if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name) and node.type.id == "PromptGuardRefused"
        )
        refusal = handler.body[-1].exc
        assert isinstance(refusal, ast.Call)
        next(keyword for keyword in refusal.keywords if keyword.arg == "status_code").value = ast.Constant(value=200)
    else:
        calls["composer.explain_run_diagnostics"].func.value = foreign
    ast.fix_missing_locations(unit.tree)
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == ["provider mint complete producer/retention/caller contract changed " + DIAGNOSTICS_CONTRACT]
    assert receipt is None


@pytest.mark.parametrize(
    "binding",
    (
        "ProviderInvocationOwner",
        "ProviderInvocationFamily",
        "RequiredWorkBinding",
        "RequiredWorkRole",
        "RequiredWorkCoordinator",
        "RequiredWorkAuthority",
        "RequiredAuthorityKind",
        "SessionOperationLease",
        "SessionOperationKind",
    ),
)
def test_provider_mint_diagnostics_contract_refuses_import_origin_drift(binding: str) -> None:
    units = _provider_mint_units()
    assert provider.provider_mint_local_contracts(units)[0] == []
    unit = next(item for item in units if item.path == DIAGNOSTICS)
    imported = next(
        alias
        for node in unit.tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if (alias.asname or alias.name) == binding
    )
    imported.name = "Foreign" + binding
    imported.asname = binding
    # Caller source is unchanged: the qualified import check must catch this.
    actual = provider._node(unit, DIAGNOSTICS_CALLER)
    assert provider._shape(actual) == provider._shape(ast.parse(provider.CONTRACTS[DIAGNOSTICS_CONTRACT]).body[0])
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == ["provider mint selected constructor/family/binding import origin changed elspeth.web.execution.routes"]
    assert receipt is None


@pytest.mark.parametrize("with_mint", (False, True))
def test_provider_mint_contract_refuses_additional_qualified_outside_constructor_and_mint(with_mint: bool) -> None:
    units = _provider_mint_units()
    assert provider.provider_mint_local_contracts(units)[0] == []
    source = (
        "from __future__ import annotations\n"
        "from elspeth.web.composer.provider_quota import ProviderInvocationOwner as Owner, ProviderInvocationFamily as Family\n"
        "def outside(service, binding):\n"
        "    owner = Owner(service=service, required_work=binding)\n"
        + ("    return owner.mint(Family.PRIMARY)\n" if with_mint else "    return owner\n")
    )
    outside = fencing._parse_source("src/elspeth/provider_mint_universe_control.py", source)
    failures, receipt = provider.provider_mint_local_contracts((*units, outside))
    assert failures == ["provider mint actual qualified constructor/mint call universe changed"]
    assert receipt is None


def test_provider_mint_contract_does_not_treat_unqualified_mint_spelling_as_owned_custody() -> None:
    units = _provider_mint_units()
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == [] and receipt is not None
    unrelated = fencing._parse_source(
        "src/elspeth/provider_mint_unrelated_control.py",
        "from __future__ import annotations\ndef unrelated(value):\n    return value.mint('ordinary value')\n",
    )
    failures, enlarged = provider.provider_mint_local_contracts((*units, unrelated))
    assert failures == [] and enlarged is not None
    assert enlarged["mint_calls"] == receipt["mint_calls"]
    assert enlarged["owner_constructor_calls"] == receipt["owner_constructor_calls"]


def test_provider_mint_callable_escape_remains_an_unclosed_common_premise() -> None:
    units = _provider_mint_units()
    assert provider.provider_mint_local_contracts(units)[0] == []
    carrier = fencing._parse_source(
        "src/elspeth/provider_mint_carrier_control.py",
        "from __future__ import annotations\n"
        "from elspeth.web.composer.provider_quota import ProviderInvocationOwner\n"
        "def retain():\n    return ProviderInvocationOwner\n",
    )
    failures, receipt = provider.provider_mint_local_contracts((*units, carrier))
    assert failures == [] and receipt is not None
    escaped = next(
        node
        for node in ast.walk(carrier.tree)
        if isinstance(node, ast.Name) and node.id == "ProviderInvocationOwner" and isinstance(node.ctx, ast.Load)
    )
    assert (carrier.path, id(escaped)) in receipt["unclosed_callable_carriers"]


def test_worker_six_actual_sources_refuse_one_valid_reserve_target_change() -> None:
    units = _source_units((worker_origin.APP, worker_origin.WORKER, worker_origin.TURN))
    failures, receipt = worker_six.worker_six_local_contracts(units)
    assert failures == []
    assert len(receipt["reserve_calls"]) == len(receipt["per_call"]) == 6
    dependencies = receipt["effect_dependencies"]
    assert dependencies and all(row["qualification"] == "UNKNOWN" and row["requirement"] for row in dependencies)
    assert worker_caller.worker_caller_local_contracts(units)[0] == []
    assert worker_origin.worker_whole_scope_origin_local_contracts(units)[0] == []

    worker = next(unit for unit in units if unit.path == worker_six.WORKER)
    method, _ = worker_six.find(worker.tree, "ComposerAsyncWorker._run_started_under_lease")
    calls = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "reserve"
    ]
    assert len(calls) == 1
    # Change an existing located node: a missing-location compiler refusal is not this control.
    calls[0].func.attr = "unreviewed_reserve"
    failures, refused = worker_six.worker_six_local_contracts(units)
    assert failures == ["worker six: complete finite method grammar changed _run_started_under_lease"]
    assert refused["reserve_calls"] == frozenset() and refused["per_call"] == []
    assert refused["effect_dependencies"] == dependencies
    assert all(scope["compiler_reason"] is None for scope in refused["invocation_scopes"])


@pytest.mark.parametrize(
    "fault",
    (
        "preference_supplier",
        "preference_session",
        "missing_preference",
        "late_preference",
        "ceiling_supplier",
        "missing_ceiling",
        "late_ceiling",
        "ceiling_session",
        "ceiling_request",
        "ceiling_preferences",
        "ceiling_mode",
        "duplicate_preference",
        "duplicate_ceiling",
    ),
)
def test_original_mutation_capture_is_exact_without_complete_body_seal(fault: str) -> None:
    units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
    assert compose.compose_proposal_local_transfers(units)[0] == []
    assert compose._source3_local_transfers(units)[0] == []
    method = fencing._reserve_node_at(units[0], "_run_composer_turn")
    block = method.body[36].body
    assert receiver.receiver_transfer_prefix(block[4:18] + block[19:24])
    preference, ceiling = block[16:18]
    if fault == "preference_supplier":
        preference.value.value.func.attr = "get_foreign_preferences"
    elif fault == "preference_session":
        preference.value.value.args[0].attr = "foreign_session"
    elif fault == "missing_preference":
        del block[16]
    elif fault == "late_preference":
        block[16], block[18] = block[18], block[16]
    elif fault == "ceiling_supplier":
        ceiling.value.func.id = "ForeignCeiling"
    elif fault == "missing_ceiling":
        del block[17]
    elif fault == "late_ceiling":
        block[17], block[18] = block[18], block[17]
    elif fault == "ceiling_session":
        ceiling.value.args[0].args[0].attr = "foreign_session"
    elif fault == "ceiling_request":
        ceiling.value.args[1].args[0].id = "foreign_request"
    elif fault == "ceiling_preferences":
        ceiling.value.args[2].value.id = "foreign_preferences"
    elif fault == "ceiling_mode":
        ceiling.value.args[2].attr = "foreign_mode"
    else:
        binding = "initial_preferences" if fault == "duplicate_preference" else "mutation_ceiling"
        block.append(ast.parse(binding + " = foreign").body[0])
    ast.fix_missing_locations(units[0].tree)
    if fault.startswith("duplicate_"):
        # The local prefix stays intact; the independent whole-method binding check refuses it.
        assert receiver.receiver_transfer_prefix(block[4:18] + block[19:24])
    else:
        assert not receiver.receiver_transfer_prefix(block[4:18] + block[19:24])
    assert compose._source3_local_transfers(units)[0] == ["composer turn original request mutation capture changed"]
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer complete executed method grammar changed " + TURN]


@pytest.mark.parametrize("fault", ("missing", "foreign", "duplicate", "recovery"))
def test_original_mutation_ceiling_dispatch_is_closed(fault: str) -> None:
    units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
    assert compose.compose_proposal_local_transfers(units)[0] == []
    assert compose._source3_local_transfers(units)[0] == []
    method = fencing._reserve_node_at(units[0], "_run_composer_turn")
    invoke = method.body[36].body[24]
    calls = receiver.composer_dispatch_calls(invoke)
    assert calls is not None
    ordinary, recovery = calls
    ceiling = next(keyword for keyword in ordinary.keywords if keyword.arg == "mutation_ceiling")
    assert ast.unparse(ceiling.value) == "mutation_ceiling"
    assert all(keyword.arg != "mutation_ceiling" for keyword in recovery.keywords)
    if fault == "missing":
        ordinary.keywords.remove(ceiling)
    elif fault == "foreign":
        ceiling.value.id = "foreign_ceiling"
    else:
        duplicate = ast.keyword(arg="mutation_ceiling", value=ast.Name(id="mutation_ceiling", ctx=ast.Load()))
        (ordinary if fault == "duplicate" else recovery).keywords.append(duplicate)
    ast.fix_missing_locations(units[0].tree)
    assert receiver.composer_title_prelude(invoke) is not None
    assert receiver.composer_dispatch_calls(invoke) is None
    assert compose._source3_local_transfers(units)[0] == ["composer turn exact paired call arguments or closed branches changed"]
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer complete executed method grammar changed " + TURN]


@pytest.mark.parametrize("fault", ("type", "missing", "order", "sidecar", "reason", "status", "payload", "cause"))
def test_nominal_mutation_revocation_handler_is_exact(fault: str) -> None:
    units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
    assert compose.compose_proposal_local_transfers(units)[0] == []
    assert compose._source3_local_transfers(units)[0] == []
    method = fencing._reserve_node_at(units[0], "_run_composer_turn")
    invoke = method.body[36].body[24]
    assert receiver.composer_title_prelude(invoke) is not None
    assert receiver.composer_dispatch_calls(invoke) is not None
    handler = invoke.handlers[11]
    assert ast.unparse(handler.type) == "ComposerMutationAuthorityRevoked"
    if fault == "type":
        handler.type.id = "Exception"
    elif fault == "missing":
        del invoke.handlers[11]
    elif fault == "order":
        invoke.handlers[11], invoke.handlers[12] = invoke.handlers[12], invoke.handlers[11]
    elif fault == "sidecar":
        handler.body[0].value.func.id = "foreign_calls"
    elif fault == "reason":
        event = next(keyword.value for keyword in handler.body[1].value.value.keywords if keyword.arg == "event")
        next(keyword for keyword in event.keywords if keyword.arg == "reason").value.value = "foreign_reason"
    elif fault == "status":
        next(keyword for keyword in handler.body[-1].exc.keywords if keyword.arg == "status_code").value.value = 200
    elif fault == "payload":
        next(keyword for keyword in handler.body[-1].exc.keywords if keyword.arg == "detail").value.func.attr = "foreign_payload"
    else:
        handler.body[-1].cause.id = "foreign_cause"
    ast.fix_missing_locations(units[0].tree)
    assert receiver.composer_title_prelude(invoke) is None
    assert receiver.composer_dispatch_calls(invoke) is None
    assert compose._source3_local_transfers(units)[0] == ["composer turn exact paired call arguments or closed branches changed"]
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer complete executed method grammar changed " + TURN]


@pytest.fixture(scope="module")
def reviewed_reserve_units() -> tuple[fencing.SourceUnit, ...]:
    # The canonical inventory supplies paths only. Never mutate its cached ASTs.
    units = _source_units(tuple(unit.path for unit in fencing._production_units()))
    assert fencing._reserve_recipe_failures(units) == []
    proof = fencing._reserve_ReserveProof(units)
    assert proof.failures == []
    return units


def test_authority_conditional_callee_refuses_seal_and_original_dispatch_after_direct_call_positive(
    reviewed_reserve_units: tuple[fencing.SourceUnit, ...],
) -> None:
    units = reviewed_reserve_units
    assert fencing._reserve_recipe_failures(units) == []
    positive = fencing._reserve_ReserveProof(units)
    assert positive.failures == []
    path = "src/elspeth/web/coordination/composer_operation_authority.py"
    original = next(unit for unit in units if unit.path == path)
    old = (
        "        if (\n"
        "            request.operation_id != operation_id\n"
        "            or request.state_id != base_state_id\n"
        "            or (\n"
        "                composer_operation_request_hash(session_id=session_id, kind=kind, request=request)\n"
        "                if lifecycle_version == 1\n"
        "                else composer_operation_v2_request_hash(session_id=session_id, kind=kind, request=request)\n"
        "            )\n"
        "            != request_hash\n"
        "        ):\n"
        '            raise ValueError("composer request does not match its immutable admission binding")\n'
    )
    new = (
        "        if (\n"
        "            request.operation_id != operation_id\n"
        "            or request.state_id != base_state_id\n"
        "            or (composer_operation_request_hash if lifecycle_version == 1 else composer_operation_v2_request_hash)(\n"
        "                session_id=session_id, kind=kind, request=request\n"
        "            )\n"
        "            != request_hash\n"
        "        ):\n"
        '            raise ValueError("composer request does not match its immutable admission binding")\n'
    )
    assert original.source.count(old) == 1
    changed = fencing._parse_source(path, original.source.replace(old, new, 1))
    mutated = tuple(changed if unit.path == path else unit for unit in units)
    expected_seal = ["full source changed " + path]
    assert fencing._reserve_recipe_failures(mutated) == expected_seal
    sealed = fencing._reserve_ReserveProof(mutated)
    assert sealed.failures == expected_seal

    # Retain the normal acceptance refusal above. Inspect the same source
    # counterfactual separately through the existing original dispatch API.
    boundary = fencing._reserve_supported_boundary(mutated)
    assert not boundary.failures, boundary.failures
    dispatch = copy.copy(positive)
    dispatch.units = mutated
    dispatch.indexed = {unit.path: unit for unit in mutated}
    dispatch.calls = [(unit, node) for unit in mutated for node in ast.walk(unit.tree) if isinstance(node, ast.Call)]
    dispatch.boundary = boundary
    refusal = "protected supplier callable/namespace escape " + path
    assert dispatch.dispatch_failures() == [refusal, refusal]
    assert positive.indexed[path] is original
    assert positive.failures == []
    assert fencing._reserve_recipe_failures(units) == []


@pytest.mark.parametrize("fault", ("body", "import"))
def test_selected_decoder_body_and_import_remain_sealed(reviewed_reserve_units: tuple[fencing.SourceUnit, ...], fault: str) -> None:
    units = reviewed_reserve_units
    assert fencing._reserve_recipe_failures(units) == []
    path = "src/elspeth/web/sessions/state_decoder.py"
    original = next(unit for unit in units if unit.path == path)
    old, new = (
        ("    read_content_publication(record)\n", "    read_content_publication(None)\n")
        if fault == "body"
        else (
            "from elspeth.web.sessions.state_envelope import unwrap_state_column\n",
            "from foreign.state_envelope import unwrap_state_column\n",
        )
    )
    assert original.source.count(old) == 1
    changed = fencing._parse_source(path, original.source.replace(old, new, 1))
    mutated = tuple(changed if unit.path == path else unit for unit in units)
    assert fencing._reserve_recipe_failures(mutated) == ["transitive non-DML recipe or imported bindings changed"]
    assert fencing._reserve_recipe_failures(units) == []


@pytest.fixture(
    scope="module",
    params=(
        pytest.param("elspeth.web.sessions.state_decoder", id="defining"),
        pytest.param("elspeth.web.sessions.converters", id="legacy"),
    ),
)
def reviewed_decoder_import(
    request: pytest.FixtureRequest,
    reviewed_reserve_units: tuple[fencing.SourceUnit, ...],
) -> tuple[tuple[fencing.SourceUnit, ...], fencing.SourceUnit, str, fencing._reserve_ReserveProof]:
    units = reviewed_reserve_units
    assert fencing._reserve_recipe_failures(units) == []
    path = "src/elspeth/reserve_decoder_import_control.py"
    source = (
        "from __future__ import annotations\n"
        f"from {request.param} import state_record_from_row\n"
        f"import {request.param} as decoder_module\n"
        "def direct(row):\n"
        "    return state_record_from_row(row)\n"
        "def qualified(row):\n"
        "    return decoder_module.state_record_from_row(row)\n"
    )
    direct = fencing._parse_source(path, source)
    proof = fencing._reserve_ReserveProof((*units, direct))
    assert proof.failures == []
    calls = [node for node in ast.walk(direct.tree) if isinstance(node, ast.Call)]
    assert len(calls) == 2
    assert {ast.unparse(call.func) for call in calls} == {"state_record_from_row", "decoder_module.state_record_from_row"}
    assert all((path, id(call.func)) in proof.boundary.supplier_sites for call in calls)
    return units, direct, source, proof


@pytest.mark.parametrize("import_style", ("direct", "module"))
@pytest.mark.parametrize("escape", ("return", "store"))
def test_selected_decoder_callable_escape_is_refused_after_direct_call_positive(
    reviewed_decoder_import: tuple[tuple[fencing.SourceUnit, ...], fencing.SourceUnit, str, fencing._reserve_ReserveProof],
    import_style: str,
    escape: str,
) -> None:
    units, direct, source, proof = reviewed_decoder_import
    assert fencing._reserve_recipe_failures(units) == []
    assert proof.failures == []
    reference, line = ("state_record_from_row", 5) if import_style == "direct" else ("decoder_module.state_record_from_row", 7)
    call = next(node for node in ast.walk(direct.tree) if isinstance(node, ast.Call) and ast.unparse(node.func) == reference)
    assert (direct.path, id(call.func)) in proof.boundary.supplier_sites
    old = f"    return {reference}(row)\n"
    new = f"    return {reference}\n" if escape == "return" else f"    retained = {reference}\n"
    assert source.count(old) == 1
    escaped = fencing._parse_source(direct.path, source.replace(old, new, 1))
    assert fencing._reserve_recipe_failures((*units, escaped)) == []
    refusal = fencing._reserve_ReserveProof((*units, escaped))
    expected = (
        "selected capability escaped by return/yield " + direct.path + ":" + str(line)
        if escape == "return"
        else "protected supplier callable/namespace escape " + direct.path
    )
    assert refusal.failures == [expected]


@pytest.mark.parametrize("import_style", ("direct", "module"))
def test_selected_decoder_origin_replacement_is_refused_after_direct_call_positive(
    reviewed_decoder_import: tuple[tuple[fencing.SourceUnit, ...], fencing.SourceUnit, str, fencing._reserve_ReserveProof],
    import_style: str,
) -> None:
    units, direct, source, proof = reviewed_decoder_import
    assert fencing._reserve_recipe_failures(units) == []
    assert proof.failures == []
    reference, line = ("state_record_from_row", 5) if import_style == "direct" else ("decoder_module.state_record_from_row", 7)
    call = next(node for node in ast.walk(direct.tree) if isinstance(node, ast.Call) and ast.unparse(node.func) == reference)
    assert (direct.path, id(call.func)) in proof.boundary.supplier_sites
    old = f"    return {reference}(row)\n"
    new = (
        "    state_record_from_row.__code__ = foreign.__code__\n"
        if import_style == "direct"
        else "    decoder_module.state_record_from_row = foreign\n"
    )
    assert source.count(old) == 1
    changed = fencing._parse_source(direct.path, source.replace(old, new, 1))
    assert fencing._reserve_recipe_failures((*units, changed)) == []
    refusal = fencing._reserve_ReserveProof((*units, changed))
    assert refusal.failures == ["selected origin attribute replaced " + direct.path + ":" + str(line)]


@pytest.mark.parametrize("escape", ("store", "argument"))
def test_selected_decoder_module_escape_is_refused_after_direct_call_positive(
    reviewed_decoder_import: tuple[tuple[fencing.SourceUnit, ...], fencing.SourceUnit, str, fencing._reserve_ReserveProof],
    escape: str,
) -> None:
    units, direct, source, proof = reviewed_decoder_import
    assert fencing._reserve_recipe_failures(units) == []
    assert proof.failures == []
    old = "    return decoder_module.state_record_from_row(row)\n"
    new = "    retained_module = decoder_module\n" if escape == "store" else "    unknown_consumer(decoder_module)\n"
    assert source.count(old) == 1
    changed = fencing._parse_source(direct.path, source.replace(old, new, 1))
    assert fencing._reserve_recipe_failures((*units, changed)) == []
    refusal = fencing._reserve_ReserveProof((*units, changed))
    expected = (
        "protected supplier module namespace escape " + direct.path
        if escape == "store"
        else "selected module capability passed to unreviewed caller " + direct.path + ":7"
    )
    assert refusal.failures == [expected]


def test_selected_decoder_requires_its_actual_defining_source(reviewed_reserve_units: tuple[fencing.SourceUnit, ...]) -> None:
    units = reviewed_reserve_units
    assert fencing._reserve_recipe_failures(units) == []
    path = "src/elspeth/web/sessions/state_decoder.py"
    assert sum(unit.path == path for unit in units) == 1
    missing = tuple(unit for unit in units if unit.path != path)
    assert fencing._reserve_recipe_failures(missing) == ["required transitive recipe source is missing"]
    assert fencing._reserve_recipe_failures(units) == []


@pytest.mark.parametrize("fault", ("wrong_module", "missing_binding"))
def test_legacy_decoder_reexport_is_the_exact_reviewed_binding(reviewed_reserve_units: tuple[fencing.SourceUnit, ...], fault: str) -> None:
    units = reviewed_reserve_units
    assert fencing._reserve_recipe_failures(units) == []
    path = "src/elspeth/web/sessions/converters.py"
    original = next(unit for unit in units if unit.path == path)
    old = "from elspeth.web.sessions.state_decoder import state_record_from_row as state_record_from_row\n"
    new = "from foreign.state_decoder import state_record_from_row as state_record_from_row\n" if fault == "wrong_module" else ""
    assert original.source.count(old) == 1
    changed = fencing._parse_source(path, original.source.replace(old, new, 1))
    mutated = tuple(changed if unit.path == path else unit for unit in units)
    assert fencing._reserve_recipe_failures(mutated) == ["writer target/field recipe changed " + path + ":module-bindings"]
    assert fencing._reserve_recipe_failures(units) == []


def test_actual_internal_decoder_call_nodes_are_suppliers(
    reviewed_decoder_import: tuple[tuple[fencing.SourceUnit, ...], fencing.SourceUnit, str, fencing._reserve_ReserveProof],
) -> None:
    units, _direct, _source, proof = reviewed_decoder_import
    assert fencing._reserve_recipe_failures(units) == []
    assert proof.failures == []
    for path, symbol, argument in (
        ("src/elspeth/web/sessions/service.py", "SessionServiceImpl._row_to_state_record", "row"),
        ("src/elspeth/web/coordination/composer_operation_authority.py", "ComposerAsyncOperationAuthority.admit", "base_row"),
    ):
        unit = next(unit for unit in units if unit.path == path)
        method = fencing._reserve_node_at(unit, symbol)
        calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "state_record_from_row"
        ]
        assert len(calls) == 1
        (call,) = calls
        assert len(call.args) == 1 and ast.unparse(call.args[0]) == argument and call.keywords == []
        assert fencing._resolver_for_unit(unit).qualified_name(call.func, use=call) == (
            "elspeth.web.sessions.state_decoder.state_record_from_row"
        )
        assert (path, id(call.func)) in proof.boundary.supplier_sites


def test_narrow_decoder_selection_preserves_existing_stored_converter_applicability(
    reviewed_reserve_units: tuple[fencing.SourceUnit, ...],
) -> None:
    units = reviewed_reserve_units
    path = "src/elspeth/web/sessions/state_decoder.py"
    assert fencing._reserve_recipes.SELECTION[path] == ("state_record_from_row",)
    assert "src/elspeth/web/sessions/converters.py" not in fencing._reserve_recipes.SELECTION
    assert fencing._reserve_recipes.SELECTION["src/elspeth/web/sessions/chat_message_decoder.py"] == ("chat_message_record_from_row",)
    assert fencing._reserve_recipe_failures(units) == []
    readiness = next(unit for unit in units if unit.path == "src/elspeth/web/audit_readiness/service.py")
    imported = next(
        alias
        for node in readiness.tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "elspeth.web.sessions.converters"
        for alias in node.names
        if alias.asname == "_default_state_from_record"
    )
    assert imported.name == "state_from_record"
    constructor = fencing._reserve_node_at(readiness, "ReadinessService.__init__")
    stored = [
        node for node in constructor.body if isinstance(node, ast.AnnAssign) and ast.unparse(node.target) == "self._state_from_record"
    ]
    assert len(stored) == 1
    assert ast.unparse(stored[0].value) == "state_from_record if state_from_record is not None else _default_state_from_record"
    snapshot = fencing._reserve_node_at(readiness, "ReadinessService.compute_snapshot")
    calls = [node for node in ast.walk(snapshot) if isinstance(node, ast.Call) and ast.unparse(node.func) == "self._state_from_record"]
    assert len(calls) == 1 and len(calls[0].args) == 1 and ast.unparse(calls[0].args[0]) == "record"
    # The fixture has already required the original full proof to admit this unchanged stored-callable path.


def test_sink_callback_repository_binding_refuses_changed_authority_body(
    reviewed_reserve_units: tuple[fencing.SourceUnit, ...],
) -> None:
    units = reviewed_reserve_units
    failures, receipt = fencing._reserve_sink_effect_disjoint_receipts(units)
    assert failures == []
    assert receipt["reserve_calls"]
    path = "src/elspeth/web/coordination/repository.py"
    original = next(unit for unit in units if unit.path == path)
    changed = fencing._parse_source(path, original.source)
    method = fencing._reserve_node_at(changed, "_SessionOperationAuthorityRepository._advance_exclusive_fence_on_connection")
    method.body.append(ast.parse("raise RuntimeError('unreviewed authority callback')").body[0])
    ast.fix_missing_locations(changed.tree)
    mutated = tuple(changed if unit.path == path else unit for unit in units)
    failures, refused = fencing._reserve_sink_effect_disjoint_receipts(mutated)
    assert failures == ["sink-effect normal callback source changed _SessionOperationAuthorityRepository"]
    assert refused == {}
    assert fencing._reserve_sink_effect_disjoint_receipts(units)[0] == []


@pytest.fixture(
    scope="module",
    params=(
        pytest.param("elspeth.web.sessions.chat_message_decoder", id="defining"),
        pytest.param("elspeth.web.sessions.converters", id="legacy"),
    ),
)
def reviewed_chat_decoder_import(
    request: pytest.FixtureRequest,
    reviewed_reserve_units: tuple[fencing.SourceUnit, ...],
) -> tuple[tuple[fencing.SourceUnit, ...], fencing.SourceUnit, str, fencing._reserve_ReserveProof]:
    units = reviewed_reserve_units
    assert fencing._reserve_recipe_failures(units) == []
    path = "src/elspeth/reserve_chat_decoder_import_control.py"
    source = (
        "from __future__ import annotations\n"
        f"from {request.param} import chat_message_record_from_row\n"
        f"import {request.param} as decoder_module\n"
        "def direct(row):\n"
        "    return chat_message_record_from_row(row)\n"
        "def qualified(row):\n"
        "    return decoder_module.chat_message_record_from_row(row)\n"
    )
    direct = fencing._parse_source(path, source)
    proof = fencing._reserve_ReserveProof((*units, direct))
    assert proof.failures == []
    calls = [node for node in ast.walk(direct.tree) if isinstance(node, ast.Call)]
    assert len(calls) == 2
    assert {ast.unparse(call.func) for call in calls} == {"chat_message_record_from_row", "decoder_module.chat_message_record_from_row"}
    assert all((path, id(call.func)) in proof.boundary.supplier_sites for call in calls)
    return units, direct, source, proof


@pytest.mark.parametrize("import_style", ("direct", "module"))
@pytest.mark.parametrize("escape", ("return", "store"))
def test_selected_chat_decoder_callable_escape_is_refused_after_direct_call_positive(
    reviewed_chat_decoder_import: tuple[tuple[fencing.SourceUnit, ...], fencing.SourceUnit, str, fencing._reserve_ReserveProof],
    import_style: str,
    escape: str,
) -> None:
    units, direct, source, proof = reviewed_chat_decoder_import
    assert fencing._reserve_recipe_failures(units) == []
    assert proof.failures == []
    reference, line = (
        ("chat_message_record_from_row", 5) if import_style == "direct" else ("decoder_module.chat_message_record_from_row", 7)
    )
    call = next(node for node in ast.walk(direct.tree) if isinstance(node, ast.Call) and ast.unparse(node.func) == reference)
    assert (direct.path, id(call.func)) in proof.boundary.supplier_sites
    old = f"    return {reference}(row)\n"
    new = f"    return {reference}\n" if escape == "return" else f"    retained = {reference}\n"
    assert source.count(old) == 1
    escaped = fencing._parse_source(direct.path, source.replace(old, new, 1))
    assert fencing._reserve_recipe_failures((*units, escaped)) == []
    refusal = fencing._reserve_ReserveProof((*units, escaped))
    expected = (
        "selected capability escaped by return/yield " + direct.path + ":" + str(line)
        if escape == "return"
        else "protected supplier callable/namespace escape " + direct.path
    )
    assert refusal.failures == [expected]


@pytest.mark.parametrize("import_style", ("direct", "module"))
def test_selected_chat_decoder_origin_replacement_is_refused_after_direct_call_positive(
    reviewed_chat_decoder_import: tuple[tuple[fencing.SourceUnit, ...], fencing.SourceUnit, str, fencing._reserve_ReserveProof],
    import_style: str,
) -> None:
    units, direct, source, proof = reviewed_chat_decoder_import
    assert fencing._reserve_recipe_failures(units) == []
    assert proof.failures == []
    reference, line = (
        ("chat_message_record_from_row", 5) if import_style == "direct" else ("decoder_module.chat_message_record_from_row", 7)
    )
    call = next(node for node in ast.walk(direct.tree) if isinstance(node, ast.Call) and ast.unparse(node.func) == reference)
    assert (direct.path, id(call.func)) in proof.boundary.supplier_sites
    old = f"    return {reference}(row)\n"
    new = (
        "    chat_message_record_from_row.__code__ = foreign.__code__\n"
        if import_style == "direct"
        else "    decoder_module.chat_message_record_from_row = foreign\n"
    )
    assert source.count(old) == 1
    changed = fencing._parse_source(direct.path, source.replace(old, new, 1))
    assert fencing._reserve_recipe_failures((*units, changed)) == []
    refusal = fencing._reserve_ReserveProof((*units, changed))
    assert refusal.failures == ["selected origin attribute replaced " + direct.path + ":" + str(line)]


@pytest.mark.parametrize("escape", ("store", "argument"))
def test_selected_chat_decoder_module_escape_is_refused_after_direct_call_positive(
    reviewed_chat_decoder_import: tuple[tuple[fencing.SourceUnit, ...], fencing.SourceUnit, str, fencing._reserve_ReserveProof],
    escape: str,
) -> None:
    units, direct, source, proof = reviewed_chat_decoder_import
    assert fencing._reserve_recipe_failures(units) == []
    assert proof.failures == []
    old = "    return decoder_module.chat_message_record_from_row(row)\n"
    new = "    retained_module = decoder_module\n" if escape == "store" else "    unknown_consumer(decoder_module)\n"
    assert source.count(old) == 1
    changed = fencing._parse_source(direct.path, source.replace(old, new, 1))
    assert fencing._reserve_recipe_failures((*units, changed)) == []
    refusal = fencing._reserve_ReserveProof((*units, changed))
    expected = (
        "protected supplier module namespace escape " + direct.path
        if escape == "store"
        else "selected module capability passed to unreviewed caller " + direct.path + ":7"
    )
    assert refusal.failures == [expected]


def test_actual_chat_decoder_delegate_is_a_protected_supplier(
    reviewed_chat_decoder_import: tuple[tuple[fencing.SourceUnit, ...], fencing.SourceUnit, str, fencing._reserve_ReserveProof],
) -> None:
    units, _direct, _source, proof = reviewed_chat_decoder_import
    assert fencing._reserve_recipe_failures(units) == []
    assert proof.failures == []
    unit = next(unit for unit in units if unit.path == SERVICE)
    method = fencing._reserve_node_at(unit, "SessionServiceImpl._row_to_chat_message_record")
    assert isinstance(method, ast.FunctionDef)
    assert len(method.body) == 1 and isinstance(method.body[0], ast.Return)
    call = method.body[0].value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name) and call.func.id == "chat_message_record_from_row"
    assert len(call.args) == 1 and ast.unparse(call.args[0]) == "row"
    assert len(call.keywords) == 1 and call.keywords[0].arg == "operation_id"
    assert ast.unparse(call.keywords[0].value) == "operation_id"
    assert fencing._resolver_for_unit(unit).qualified_name(call.func, use=call) == (
        "elspeth.web.sessions.chat_message_decoder.chat_message_record_from_row"
    )
    assert (SERVICE, id(call.func)) in proof.boundary.supplier_sites


def test_selected_chat_decoder_requires_its_actual_defining_source(reviewed_reserve_units: tuple[fencing.SourceUnit, ...]) -> None:
    units = reviewed_reserve_units
    assert fencing._reserve_recipe_failures(units) == []
    path = "src/elspeth/web/sessions/chat_message_decoder.py"
    assert sum(unit.path == path for unit in units) == 1
    missing = tuple(unit for unit in units if unit.path != path)
    assert fencing._reserve_recipe_failures(missing) == ["required transitive recipe source is missing"]
    assert fencing._reserve_recipe_failures(units) == []


@pytest.mark.parametrize(
    ("old", "new"),
    (
        pytest.param(
            '    if operation_id is not None and (row.role != "user" or row.writer_principal != "route_user_message"):\n',
            '    if operation_id is not None and row.role != "user":\n',
            id="principal-guard",
        ),
        pytest.param("        raw_content=row.raw_content,\n", "        raw_content=row.content,\n", id="record-field"),
        pytest.param("from uuid import UUID\n", "from foreign.uuid import UUID\n", id="import-supplier"),
        pytest.param(
            "        operation_id=UUID(operation_id) if operation_id is not None else None,\n    )\n",
            "        operation_id=UUID(operation_id) if operation_id is not None else None,\n    )\n"
            "chat_message_record_from_row = foreign\n",
            id="module-rebinding",
        ),
    ),
)
def test_selected_chat_decoder_body_and_import_remain_sealed(
    reviewed_reserve_units: tuple[fencing.SourceUnit, ...], old: str, new: str
) -> None:
    units = reviewed_reserve_units
    assert fencing._reserve_recipe_failures(units) == []
    path = "src/elspeth/web/sessions/chat_message_decoder.py"
    original = next(unit for unit in units if unit.path == path)
    assert original.source.count(old) == 1
    changed = fencing._parse_source(path, original.source.replace(old, new, 1))
    mutated = tuple(changed if unit.path == path else unit for unit in units)
    expected = ["transitive non-DML recipe or imported bindings changed"]
    assert fencing._reserve_recipe_failures(mutated) == expected
    assert fencing._reserve_ReserveProof(mutated).failures == expected
    assert fencing._reserve_recipe_failures(units) == []


@pytest.mark.parametrize("fault", ("wrong_module", "missing_binding"))
def test_legacy_chat_decoder_reexport_is_the_exact_reviewed_binding(
    reviewed_reserve_units: tuple[fencing.SourceUnit, ...], fault: str
) -> None:
    units = reviewed_reserve_units
    assert fencing._reserve_recipe_failures(units) == []
    path = "src/elspeth/web/sessions/converters.py"
    original = next(unit for unit in units if unit.path == path)
    old = "from elspeth.web.sessions.chat_message_decoder import chat_message_record_from_row as chat_message_record_from_row\n"
    new = (
        "from foreign.chat_message_decoder import chat_message_record_from_row as chat_message_record_from_row\n"
        if fault == "wrong_module"
        else ""
    )
    assert original.source.count(old) == 1
    changed = fencing._parse_source(path, original.source.replace(old, new, 1))
    mutated = tuple(changed if unit.path == path else unit for unit in units)
    expected = ["writer target/field recipe changed " + path + ":module-bindings"]
    assert fencing._reserve_recipe_failures(mutated) == expected
    assert fencing._reserve_ReserveProof(mutated).failures == expected
    assert fencing._reserve_recipe_failures(units) == []


# Append to tests/unit/architecture/test_web_reserve_reviewed_contracts.py.
# Existing ast/pytest/fencing imports suffice. Each full-proof effect is an
# independent ordinary parametrized test, not a serial loop over full proofs.


@pytest.mark.landscape_long_run
def test_callback_consumers_use_actual_ast_and_keep_carrier_permissions():
    units = fencing._production_units()
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    failures, receipt = fencing._reserve_batch_data_callback_consumers(units)
    assert failures == []
    actual = {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}
    assert receipt["calls"] and receipt["calls"] <= actual
    assert receipt["uses"] and receipt["uses"] <= actual
    assert receipt["observed_uses"] <= receipt["uses"]
    assert all((path, call_id) in actual and (path, arg_id) in actual for path, call_id, arg_id in receipt["argument_calls"])
    assert len(receipt["immediate_lambdas"]) == 1
    assert receipt["immediate_lambdas"] <= actual
    removed = next(iter(receipt["calls"]))
    assert not receipt["calls"] <= actual - {removed}
    reparsed = tuple(fencing._parse_source(unit.path, unit.source) for unit in units)
    failures, other = fencing._reserve_batch_data_callback_consumers(reparsed)
    assert failures == []
    assert not other["calls"] & actual
    assert not other["uses"] & actual
    batch_failures, batch = fencing._reserve_continuation_batch_state_receipt(units)
    assert batch_failures == []
    assert len(batch["state_writes"]) == 3
    assert len(batch["data_calls"]) == 3
    assert len(batch["calls"]) == 5


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "filename,before,after,reason",
    [
        (
            "continuation_tool_batch.py",
            "from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError",
            "from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError\nfrom foreign_adapter import TypeAdapter",
            "canonical import changed",
        ),
        (
            "continuation_tool_batch.py",
            "_RUNTIME = TypeAdapter(ValidationResult)",
            "_RUNTIME = unknown_adapter",
            "actual adapter declaration changed",
        ),
        (
            "continuation_tool_batch.py",
            'adapter.dump_python(value, mode="json", warnings="error", fallback=owned_json)',
            'unknown_adapter.dump_python(value, mode="json", warnings="error", fallback=owned_json)',
            "complete consumer body changed",
        ),
        (
            "continuation_tool_batch.py",
            "_dump_owned(_RUNTIME, result.runtime_preflight)",
            "_dump_owned(unknown_adapter, result.runtime_preflight)",
            "exact call changed",
        ),
        (
            "continuation_prepared_p4.py",
            "_dump_owned(_P4_ROWS, tuple(retained))",
            "_dump_owned(_P4_VALUE, tuple(retained))",
            "exact call changed",
        ),
        (
            "continuation_bootstrap.py",
            "_canonical_adapter(_RECOVERY, snapshot.recovery_context_json)",
            "_canonical_adapter(unknown_adapter, snapshot.recovery_context_json)",
            "exact call changed",
        ),
        (
            "continuation_bootstrap.py",
            'adapter.dump_python(value, mode="json", warnings="error", fallback=owned_json)',
            'adapter.dump_python(value, mode="python", warnings="error", fallback=owned_json)',
            "complete consumer body changed",
        ),
        ("prompt_guard_rendering.py", "parts = tuple(render(value, (), 0))", "parts = encoder", "complete consumer body changed"),
        (
            "pipeline_planner.py",
            "    references = frozenset(\n",
            "    json_content_parts = unknown_consumer\n    references = frozenset(\n",
            "prompt receiver shadowed",
        ),
    ],
)
def test_callback_consumers_refuse_changed_receivers_and_consumer_bodies(filename, before, after, reason):
    units = fencing._production_units()
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    path = "src/elspeth/web/composer/" + filename
    original = next(unit for unit in units if unit.path == path)
    assert original.source.count(before) == 1
    changed = fencing._parse_source(path, original.source.replace(before, after, 1))
    failures, receipt = fencing._reserve_batch_data_callback_consumers(tuple(changed if unit.path == path else unit for unit in units))
    assert any(reason in failure for failure in failures), failures
    assert receipt == {}


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "addition",
    [
        "opaque_callback(owned_json)\n",
        "unknown_receiver.dump_python(value, mode='json', warnings='error', fallback=owned_json)\n",
        "def returned_callable():\n    return owned_json\n",
        "def returned_lambda():\n    return lambda value: owned_json(value)\n",
        "opaque_callback(_primitive_canonical_json)\n",
        "def returned_encoder():\n    return lambda value: _primitive_canonical_json(value)\n",
        "_dump_owned(unknown_receiver, value)\n",
        "def unknown_formal(adapter, value):\n    return _dump_owned(adapter, value)\n",
        "adapters._RUNTIME.dump_python = replacement\n",
        "setattr(adapters._RUNTIME, 'dump_python', replacement)\n",
        "adapters._RUNTIME.__dict__['serializer'] = replacement\n",
        "adapters._RUNTIME = replacement\n",
        "setattr(adapters, chosen_name, replacement)\n",
        "adapters.__dict__['_RUNTIME'] = replacement\n",
        "renderer.json_content_parts = replacement\n",
        "setattr(renderer, 'json_content_parts', replacement)\n",
        "renderer.json_content_parts.__code__ = replacement\n",
        "renderer.json = replacement\n",
        "owned_json.__code__ = replacement\n",
    ],
)
def test_callback_consumers_keep_external_transfers_and_mutations_refused(addition):
    units = fencing._production_units()
    path = "src/elspeth/callback_consumer_adversary.py"
    imports = (
        "from elspeth.web.composer.continuation_codec import owned_json\n"
        "from elspeth.contracts.hashing import canonical_json as _primitive_canonical_json\n"
        "from elspeth.web.composer.continuation_tool_batch import _dump_owned\n"
        "import elspeth.web.composer.continuation_tool_batch as adapters\n"
        "import elspeth.web.composer.prompt_guard_rendering as renderer\n"
    )
    # The exact same imports must first be compatible with the full proof.
    normal = fencing._reserve_run((*units, fencing._parse_source(path, imports)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._parse_source(path, imports + addition)
    refused = fencing._reserve_ReserveProof((*units, changed))
    assert any(path in failure for failure in refused.failures), refused.failures


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "before,after",
    [
        (
            'sorted(entries, key=lambda entry: _primitive_canonical_json(entry["key"]))',
            'foreign_sorted(entries, key=lambda entry: _primitive_canonical_json(entry["key"]))',
        ),
        (
            'sorted(entries, key=lambda entry: _primitive_canonical_json(entry["key"]))',
            '(lambda entry: _primitive_canonical_json(entry["key"]))',
        ),
        (
            'sorted(entries, key=lambda entry: _primitive_canonical_json(entry["key"]))',
            "sorted(entries, key=lambda entry: _primitive_canonical_json)",
        ),
    ],
)
def test_callback_consumers_keep_immediate_sort_premise(before, after):
    units = fencing._production_units()
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    path = "src/elspeth/core/canonical.py"
    original = next(unit for unit in units if unit.path == path)
    assert original.source.count(before) == 1
    changed = fencing._parse_source(path, original.source.replace(before, after, 1))
    failures, receipt = fencing._reserve_batch_data_callback_consumers(tuple(changed if unit.path == path else unit for unit in units))
    assert any("complete consumer body changed" in failure for failure in failures), failures
    assert receipt == {}


# Append to the existing callback tests; all old tests stay unchanged.
@pytest.mark.landscape_long_run
def test_callback_definition_roles_are_exact_actual_nodes_and_scalar():
    units = fencing._production_units()
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    failures, receipt = fencing._reserve_batch_data_callback_consumers(units)
    assert failures == []
    part_path = "src/elspeth/contracts/prompt_guard.py"
    renderer_path = "src/elspeth/web/composer/prompt_guard_rendering.py"
    parts = next(unit for unit in units if unit.path == part_path)
    renderer = next(unit for unit in units if unit.path == renderer_path)
    trust = fencing._reserve_node_at(parts, "PromptGuardTrust")
    part = fencing._reserve_node_at(parts, "PromptGuardContentPart")
    rendering = fencing._reserve_node_at(renderer, "json_content_parts")
    default = next(value for arg, value in zip(rendering.args.kwonlyargs, rendering.args.kw_defaults, strict=True) if arg.arg == "decoder")
    member = next(
        node
        for node in ast.walk(rendering)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "PromptGuardTrust"
    )
    nominal = next(
        node
        for node in ast.walk(part)
        if isinstance(node, ast.Compare)
        and ast.dump(node, include_attributes=False)
        == ast.dump(ast.parse("type(self.trust) is not PromptGuardTrust", mode="eval").body, include_attributes=False)
    )
    bound = next(
        node
        for node in ast.walk(rendering)
        if isinstance(node, ast.Name) and node.id == "_COMPLETE_CONTAINER_CHARACTERS" and isinstance(node.ctx, ast.Load)
    )
    expected = {
        (part_path, id(trust.bases[0])),
        (renderer_path, id(default)),
        (renderer_path, id(default.value)),
        (renderer_path, id(member)),
        (renderer_path, id(member.value)),
        (part_path, id(nominal.comparators[0])),
        (renderer_path, id(bound)),
    }
    assert receipt["definition_uses"] == expected
    assert receipt["definition_uses"] <= receipt["uses"]
    assert receipt["immutable_supplier_origins"] == frozenset(
        {"elspeth.web.composer.prompt_guard_rendering._COMPLETE_CONTAINER_CHARACTERS"}
    )
    actual = {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}
    assert expected <= actual
    assert not expected <= actual - {(part_path, id(trust.bases[0]))}
    reparsed = tuple(fencing._parse_source(unit.path, unit.source) for unit in units)
    other_failures, other = fencing._reserve_batch_data_callback_consumers(reparsed)
    assert other_failures == []
    assert not other["definition_uses"] & actual


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "path,before,after",
    [
        ("src/elspeth/contracts/prompt_guard.py", "class PromptGuardTrust(Enum):", "class PromptGuardTrust(UnknownEnum):"),
        (
            "src/elspeth/web/composer/prompt_guard_rendering.py",
            "decoder: Callable[[str], JsonValue] = json.loads",
            "decoder: Callable[[str], JsonValue] = unknown_decoder",
        ),
        (
            "src/elspeth/web/composer/prompt_guard_rendering.py",
            "PromptGuardContentPart(encoded, PromptGuardTrust.REFERENCE)",
            "PromptGuardContentPart(encoded, PromptGuardTrust.INSTRUCTION)",
        ),
        ("src/elspeth/contracts/prompt_guard.py", "type(self.trust) is not PromptGuardTrust", "type(self.trust) is not object"),
        (
            "src/elspeth/web/composer/prompt_guard_rendering.py",
            "_COMPLETE_CONTAINER_CHARACTERS = 2_048",
            "_COMPLETE_CONTAINER_CHARACTERS = lambda: 2048",
        ),
        (
            "src/elspeth/web/composer/prompt_guard_rendering.py",
            "_COMPLETE_CONTAINER_CHARACTERS = 2_048",
            "_COMPLETE_CONTAINER_CHARACTERS = 2_048\n_COMPLETE_CONTAINER_CHARACTERS = replacement",
        ),
    ],
)
def test_callback_definition_roles_refuse_changed_source(path, before, after):
    units = fencing._production_units()
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    original = next(unit for unit in units if unit.path == path)
    assert original.source.count(before) == 1
    changed = fencing._parse_source(path, original.source.replace(before, after, 1))
    failures, receipt = fencing._reserve_batch_data_callback_consumers(tuple(changed if unit.path == path else unit for unit in units))
    assert failures
    assert receipt == {}


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "addition",
    [
        "renderer._COMPLETE_CONTAINER_CHARACTERS = replacement\n",
        "setattr(renderer, '_COMPLETE_CONTAINER_CHARACTERS', replacement)\n",
        "renderer.__dict__['_COMPLETE_CONTAINER_CHARACTERS'] = replacement\n",
        "trust.PromptGuardTrust.REFERENCE = replacement\n",
        "setattr(trust.PromptGuardTrust, 'REFERENCE', replacement)\n",
        "trust.Enum = replacement\n",
    ],
)
def test_callback_definition_roles_do_not_waive_supplier_writes(addition):
    units = fencing._production_units()
    path = "src/elspeth/callback_role_adversary.py"
    imports = "import elspeth.web.composer.prompt_guard_rendering as renderer\nimport elspeth.contracts.prompt_guard as trust\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, imports)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    refused = fencing._reserve_ReserveProof((*units, fencing._parse_source(path, imports + addition)))
    assert any(path in failure for failure in refused.failures), refused.failures


@pytest.mark.landscape_long_run
def test_nominal_yaml_serializer_receipt_uses_actual_ast_and_preserves_live_nodes():
    units = fencing._production_units()
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    failures, receipt = fencing._reserve_yaml_serializer_adapter(units)
    assert failures == []
    actual = {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}
    assert len(receipt["calls"]) == 5
    assert receipt["calls"] | receipt["uses"] <= actual
    removed = next(iter(receipt["calls"]))
    assert not receipt["calls"] <= actual - {removed}
    reparsed = tuple(fencing._parse_source(unit.path, unit.source) for unit in units)
    failures, detached = fencing._reserve_yaml_serializer_adapter(reparsed)
    assert failures == []
    assert not detached["calls"] & actual
    failures, batch = fencing._reserve_continuation_batch_state_receipt(units)
    assert failures == []
    assert len(batch["calls"]) == 5
    assert len(batch["state_writes"]) == 3
    assert len(batch["data_calls"]) == 3


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "before,after",
    [
        ("    __slots__ = ()", "    __slots__ = ('retained',)"),
        ("        return generate_yaml(state)", "        return generate_yaml(CompositionState())"),
        ("class ComposerYamlSerializer:", "class ComposerYamlSerializer(Foreign):"),
        ("        from elspeth.web.composer.yaml_generator import generate_yaml", "        from foreign import generate_yaml"),
    ],
    ids=["retained-field", "foreign-input", "foreign-allocation", "foreign-delegate"],
)
def test_nominal_yaml_serializer_refuses_changed_complete_adapter(before, after):
    units = fencing._production_units()
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    path = "src/elspeth/web/composer/yaml_serializer.py"
    original = next(unit for unit in units if unit.path == path)
    assert original.source.count(before) == 1
    changed = fencing._parse_source(path, original.source.replace(before, after, 1))
    failures, receipt = fencing._reserve_yaml_serializer_adapter(tuple(changed if unit.path == path else unit for unit in units))
    assert any("complete adapter declaration changed" in failure for failure in failures), failures
    assert receipt == {}


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "adapter.ComposerYamlSerializer = replacement",
        "adapter.ComposerYamlSerializer.generate_yaml = replacement",
        "setattr(adapter.ComposerYamlSerializer, 'generate_yaml', replacement)",
        "adapter.ComposerYamlSerializer.generate_yaml.__globals__['CompositionState'] = replacement",
        "actual.generate_yaml = replacement",
        "setattr(actual, 'generate_yaml', replacement)",
        "actual.yaml = replacement",
        "serializer_library.dump = replacement",
        "opaque_callback(adapter.ComposerYamlSerializer)",
        "alias = adapter.ComposerYamlSerializer",
        "extra = adapter.ComposerYamlSerializer()",
    ],
)
def test_nominal_yaml_serializer_keeps_canonical_supplier_mutation_refused(effect):
    units = fencing._production_units()
    path = "src/elspeth/yaml_serializer_adversary.py"
    imports = "import elspeth.web.composer.yaml_serializer as adapter\nimport elspeth.web.composer.yaml_generator as actual\nimport yaml as serializer_library\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, imports)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._parse_source(path, imports + effect + "\n")
    rejected = fencing._reserve_run((*units, changed))
    assert rejected["global_failures"], effect


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(json.loads)",
        "def hidden():\n    return json.loads",
        "alias = json.loads",
        "opaque_callback(Mapping)",
    ],
)
def test_new_canonical_supplier_first_class_transfers_stay_refused(effect):
    units = fencing._production_units()
    path = "src/elspeth/new_supplier_role_adversary.py"
    imports = "import json\nfrom collections.abc import Mapping\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, imports)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._parse_source(path, imports + effect + "\n")
    refused = fencing._reserve_run((*units, changed))
    assert any("canonical supplier first-class role is unproved " + path in failure for failure in refused["global_failures"]), refused[
        "global_failures"
    ]


@pytest.mark.parametrize("projection", (worker_caller.adapter_grammar_projection, worker_origin.adapter_grammar_projection))
def test_adapter_worker_grammar_projection_preserves_original_input(projection):
    units = _source_units((worker_origin.APP, worker_origin.WORKER, worker_origin.TURN))
    assert worker_origin.worker_whole_scope_origin_local_contracts(units)[0] == []
    assert worker_caller.worker_caller_local_contracts(units)[0] == []
    factory = fencing._reserve_node_at(units[0], "_create_app")
    original = ast.dump(factory, include_attributes=True)
    projected = projection(factory)
    assert projected is not factory
    assert ast.dump(factory, include_attributes=True) == original
    constructors = [
        node
        for node in ast.walk(factory)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ComposerYamlSerializer"
    ]
    assert len(constructors) == 1
    expected = copy.deepcopy(factory)
    preflight = next(node for node in expected.body if isinstance(node, ast.FunctionDef) and node.name == "_session_runtime_preflight")
    call = next(node.value for node in preflight.body if isinstance(node, ast.Return) and isinstance(node.value, ast.Call))
    call.args[2] = ast.copy_location(ast.Name(id="yaml_generator_module", ctx=ast.Load()), call.args[2])
    assert ast.dump(projected, include_attributes=True) == ast.dump(expected, include_attributes=True)


@pytest.mark.parametrize("fault", ("argument", "supplier", "consumer", "extra_constructor"))
def test_adapter_worker_grammar_projection_refuses_nonexact_delta(fault):
    units = _source_units((worker_origin.APP, worker_origin.WORKER, worker_origin.TURN))
    assert worker_origin.worker_whole_scope_origin_local_contracts(units)[0] == []
    assert worker_caller.worker_caller_local_contracts(units)[0] == []
    factory = fencing._reserve_node_at(units[0], "_create_app")
    constructor = next(
        node
        for node in ast.walk(factory)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ComposerYamlSerializer"
    )
    if fault == "argument":
        constructor.args.append(ast.Constant(value=None))
    elif fault == "supplier":
        constructor.func.id = "ForeignYamlSerializer"
    elif fault == "consumer":
        preflight = next(node for node in factory.body if isinstance(node, ast.FunctionDef) and node.name == "_session_runtime_preflight")
        call = next(node.value for node in preflight.body if isinstance(node, ast.Return) and isinstance(node.value, ast.Call))
        call.func.id = "foreign_validate"
    else:
        worker_statement = worker_caller.direct_call_statement(
            factory.body, ("app", "state", "composer_async_worker"), ("ComposerAsyncWorker",)
        )
        assert worker_statement is not None
        factory.body.insert(factory.body.index(worker_statement), ast.Expr(value=copy.deepcopy(constructor)))
    ast.fix_missing_locations(units[0].tree)
    assert worker_origin.worker_whole_scope_origin_local_contracts(units)[0]
    assert worker_caller.worker_caller_local_contracts(units)[0]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "positive,negative",
    [
        ("def check(value):\n    return isinstance(value, Mapping)\n", "def check(value):\n    return opaque_callback(value, Mapping)\n"),
        (
            "def check(value):\n    return type(value) is MappingProxyType\n",
            "def check(value):\n    return opaque_type(value) is MappingProxyType\n",
        ),
        (
            "def check(value):\n    kind = type(value)\n    return kind in (dict, MappingProxyType)\n",
            "def check(value):\n    kind = opaque_type(value)\n    return kind in (dict, MappingProxyType)\n",
        ),
        ("def check(value):\n    return cast(Mapping, value)\n", "def check(value):\n    return opaque_cast(Mapping, value)\n"),
        ("type Alias = list[Mapping]\n", "type Alias = Opaque[Mapping]\n"),
        (
            "def check():\n    try:\n        operation()\n    except ValidationError:\n        return False\n",
            "def check():\n    return opaque_callback(ValidationError)\n",
        ),
        (
            "def check(value):\n    match value:\n        case MappingProxyType():\n            return True\n",
            "def check(value):\n    return opaque_callback(MappingProxyType)\n",
        ),
    ],
)
def test_new_supplier_nominal_roles_require_actual_operator(positive, negative):
    path = "src/elspeth/supplier_nominal_role_probe.py"
    imports = (
        "from collections.abc import Mapping\nfrom types import MappingProxyType\n"
        "from typing import cast\nfrom pydantic import ValidationError\n"
    )
    units = fencing._production_units()
    normal = fencing._parse_source(path, imports + positive)
    baseline = fencing._reserve_run((*units, normal))
    assert baseline["global_failures"] == []
    assert baseline["unproven_web_rows"] == []
    changed = fencing._parse_source(path, imports + negative)
    failures = fencing._reserve_run((*units, changed))["global_failures"]
    assert any("canonical supplier first-class role is unproved " + path in failure for failure in failures), failures


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(Mapping)\n",
        "alias = Mapping\n",
        "def hidden():\n    return Mapping\n",
        "Mapping.__subclasses__()\n",
        "Mapping.__name__ = 'foreign'\n",
        "setattr(Mapping, '__instancecheck__', foreign)\n",
    ],
)
def test_nominal_consumption_keeps_unknown_transfer_and_mutation_denials(effect):
    path = "src/elspeth/supplier_nominal_effect_probe.py"
    source = "from collections.abc import Mapping\ndef check(value):\n    return isinstance(value, Mapping)\n"
    units = fencing._production_units()
    normal = fencing._parse_source(path, source)
    baseline = fencing._reserve_run((*units, normal))
    assert baseline["global_failures"] == []
    assert baseline["unproven_web_rows"] == []
    changed = fencing._parse_source(path, source + effect)
    failures = fencing._reserve_run((*units, changed))["global_failures"]
    assert any(path in failure for failure in failures), failures


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "positive,negative",
    [
        ("names = [member.value for member in PluginCapability]\n", "names = opaque_callback(PluginCapability)\n"),
        ("values = tuple(PluginCapability)\n", "values = opaque_tuple(PluginCapability)\n"),
        ("value = PluginCapability.LLM\n", "value = PluginCapability.unknown_member\n"),
    ],
)
def test_closed_actual_enum_roles_do_not_allow_opaque_consumers(positive, negative):
    units = fencing._production_units()
    path = "src/elspeth/supplier_enum_role_probe.py"
    imports = "from elspeth.contracts.plugin_capabilities import PluginCapability\n"
    normal = fencing._parse_source(path, imports + positive)
    baseline = fencing._reserve_run((*units, normal))
    assert baseline["global_failures"] == []
    assert baseline["unproven_web_rows"] == []
    changed = fencing._parse_source(path, imports + negative)
    failures = fencing._reserve_run((*units, changed))["global_failures"]
    assert any("canonical supplier first-class role is unproved " + path in failure for failure in failures), failures


@pytest.mark.landscape_long_run
@pytest.mark.parametrize("effect", ["table[dialect] = replacement\n", "opaque_callback(table)\n", "alias = table\n"])
def test_wire_supplier_table_read_role_preserves_write_and_escape_refusal(effect):
    path = "src/elspeth/supplier_wire_table_probe.py"
    source = (
        "from elspeth.web.composer.tools.wire_projection import _WIRE_TOOL_DEFS as table\ndef read(dialect):\n    return table[dialect]\n"
    )
    units = fencing._production_units()
    normal = fencing._parse_source(path, source)
    baseline = fencing._reserve_run((*units, normal))
    assert baseline["global_failures"] == []
    assert baseline["unproven_web_rows"] == []
    changed = fencing._parse_source(path, source + effect)
    failures = fencing._reserve_run((*units, changed))["global_failures"]
    assert any(path in failure for failure in failures), failures


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "before,after",
    [
        ('catalog_router = APIRouter(tags=["catalog"])', 'catalog_router = ForeignRouter(tags=["catalog"])'),
        (
            '@catalog_router.get("/sources", response_model=list[PluginSummary])',
            '@catalog_router.foreign("/sources", response_model=list[PluginSummary])',
        ),
        (
            '@catalog_router.get("/sources", response_model=list[PluginSummary])',
            'leak(catalog_router)\n@catalog_router.get("/sources", response_model=list[PluginSummary])',
        ),
        (
            '@catalog_router.get("/sources", response_model=list[PluginSummary])',
            'catalog_router.get = foreign\n@catalog_router.get("/sources", response_model=list[PluginSummary])',
        ),
    ],
)
def test_response_model_registration_requires_actual_constructor_and_receiver(before, after):
    units = fencing._production_units()
    baseline = fencing._reserve_run(units)
    assert baseline["global_failures"] == []
    assert baseline["unproven_web_rows"] == []
    original = next(unit for unit in units if unit.path == "src/elspeth/web/catalog/routes.py")
    assert original.source.count(before) == 1
    changed = fencing._parse_source(original.path, original.source.replace(before, after, 1))
    failures = fencing._reserve_run(tuple(changed if unit.path == original.path else unit for unit in units))["global_failures"]
    assert any("canonical supplier first-class role is unproved " + original.path in failure for failure in failures), failures


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "from elspeth.supplier_alias_holder import Held\nopaque_callback(Held.__value__.__args__[0])\n",
        "import elspeth.supplier_alias_holder as holder\nopaque_callback(holder.Held.__value__.__args__[0])\n",
        "import elspeth.supplier_alias_holder as holder\nopaque_callback(vars(holder)['Held'].__value__.__args__[0])\n",
        "import elspeth.supplier_alias_holder as holder\nopaque_callback(holder.__dict__['Held'].__value__.__args__[0])\n",
        "import elspeth.supplier_alias_holder as holder\nopaque_callback(getattr(holder, 'Held').__value__.__args__[0])\n",
        "import elspeth.supplier_alias_holder as holder\nopaque_callback(getattr(holder, '__dict__')['Held'].__value__.__args__[0])\n",
    ],
)
def test_supported_type_aliases_do_not_export_supplier_metadata(effect):
    units = fencing._production_units()
    holder = fencing._parse_source(
        "src/elspeth/supplier_alias_holder.py",
        "from __future__ import annotations\nfrom collections.abc import Mapping\ntype Held = list[Mapping]\ndef typed(value: Held) -> Held:\n    return value\n",
    )
    path = "src/elspeth/supplier_alias_consumer.py"
    clean = fencing._parse_source(
        path,
        "from __future__ import annotations\nfrom elspeth.supplier_alias_holder import Held\ndef typed(value: Held) -> Held:\n    return value\n",
    )
    baseline = fencing._reserve_run((*units, holder, clean))
    assert baseline["global_failures"] == []
    assert baseline["unproven_web_rows"] == []
    changed = fencing._parse_source(path, effect)
    failures = fencing._reserve_run((*units, holder, changed))["global_failures"]
    assert any(path in failure for failure in failures), failures


@pytest.mark.landscape_long_run
def test_lazy_type_syntax_cannot_retain_an_imported_json_callable():
    units = fencing._production_units()
    path = "src/elspeth/supplier_callable_alias_probe.py"
    imports = "from collections.abc import Mapping\nfrom json import loads\n"
    normal = fencing._parse_source(path, imports + "type Held = list[Mapping]\n")
    baseline = fencing._reserve_run((*units, normal))
    assert baseline["global_failures"] == []
    assert baseline["unproven_web_rows"] == []
    # No metadata read is needed to prove the declaration itself is refused.
    changed = fencing._parse_source(path, imports + "type Held = list[loads]\n")
    failures = fencing._reserve_run((*units, changed))["global_failures"]
    assert any("canonical supplier first-class role is unproved " + path in failure for failure in failures), failures


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "PluginCapability.LLM._value_ = 'changed'\n",
        "member = PluginCapability.LLM\nmember._value_ = 'changed'\n",
        "def mutate():\n    member = PluginCapability.LLM\n    member._value_ = 'changed'\n",
        "setattr(PluginCapability.LLM, '_value_', 'changed')\n",
        "object.__setattr__(PluginCapability.LLM, '_value_', 'changed')\n",
        "del PluginCapability.LLM._value_\n",
        "type(PluginCapability.LLM).__new__ = replacement\n",
        "PluginCapability.LLM.__class__.__new__ = replacement\n",
        "cls = type(PluginCapability.LLM)\ncls.__new__ = replacement\n",
        "opaque_callback(type(PluginCapability.LLM))\n",
        "opaque_callback(PluginCapability.LLM)\n",
        "vars(PluginCapability.LLM)['_value_'] = 'changed'\n",
        "members = tuple(PluginCapability)\nmembers[0]._value_ = 'changed'\n",
    ],
)
def test_retained_canonical_enum_members_preserve_mutation_and_recovery_denials(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_enum_member_probe.py"
    source = (
        "from elspeth.contracts.plugin_capabilities import PluginCapability\n"
        "def read():\n    member = PluginCapability.LLM\n    return member.name, member.value\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(
        ("retained supplier " in failure or "retained enum " in failure or "selected origin " in failure) and path in failure
        for failure in changed["global_failures"]
    ), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "catalog_router.routes[0].response_model.__args__[0].model_validate_json = replacement\n",
        "model = catalog_router.routes[0].response_model.__args__[0]\nmodel.model_validate_json = replacement\n",
        "def mutate():\n    model = catalog_router.routes[0].response_model.__args__[0]\n    model.model_validate_json = replacement\n",
        "setattr(catalog_router.routes[0].response_model.__args__[0], 'model_validate_json', replacement)\n",
        "opaque_callback(catalog_router.routes[0].response_model.__args__[0])\n",
        "def leak():\n    return catalog_router.routes[0].response_model.__args__[0]\n",
        "vars(catalog_router.routes[0].response_model.__args__[0])['model_validate_json'] = replacement\n",
        "getattr(catalog_router, 'routes')[0].response_model.__args__[0].model_validate_json = replacement\n",
        "import elspeth.web.catalog.routes as catalog\ngetattr(catalog, 'catalog_router').routes[0].response_model.__args__[0].model_validate_json = replacement\n",
        "import elspeth.web.catalog.routes as catalog\nvars(catalog)['catalog_router'].routes[0].response_model.__args__[0].model_validate_json = replacement\n",
    ],
)
def test_registered_schema_recovery_is_not_a_mutable_class_permission(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_route_model_probe.py"
    source = (
        "from elspeth.web.catalog.routes import catalog_router\n"
        "def read():\n    return catalog_router.routes[0].response_model.__args__[0].__name__\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(
        ("retained supplier " in failure or "selected owner reflection is unproved " in failure or "selected origin " in failure)
        and path in failure
        for failure in changed["global_failures"]
    ), changed["global_failures"]


def test_actual_enum_metaclass_and_capability_record_retain_the_same_member():
    from enum import EnumMeta, EnumType, ReprEnum, StrEnum

    from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, PluginCapability

    assert EnumMeta is EnumType
    assert type(PluginCapability) is EnumType
    assert ReprEnum in StrEnum.__mro__
    record = CapabilityDeclaration(PluginCapability.LLM)
    assert record.capability is PluginCapability.LLM
    assert record.control_role is None
    assert record.blocks_positive_detection is False


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "box = CapabilityDeclaration(PluginCapability.LLM)\nbox.capability._value_ = 'changed'\n",
        "def mutate():\n    box = CapabilityDeclaration(PluginCapability.LLM)\n    box.capability._value_ = 'changed'\n",
        "box = CapabilityDeclaration(PluginCapability.LLM)\ntype(box.capability).__new__ = replacement\n",
        "box = CapabilityDeclaration(PluginCapability.LLM)\nsetattr(box.capability, '_value_', 'changed')\n",
        "boxes = [CapabilityDeclaration(PluginCapability.LLM)]\nboxes[0].capability._value_ = 'changed'\n",
        "members = [PluginCapability.LLM]\nmembers[0]._value_ = 'changed'\n",
        "for member in PluginCapability:\n    member._value_ = 'changed'\n",
        "values = [setattr(member, '_value_', 'changed') for member in PluginCapability]\n",
        "for member in PluginCapability:\n    opaque_callback(member)\n",
        "for member in PluginCapability:\n    member = foreign\n    member._value_ = 'changed'\n",
    ],
)
def test_actual_enum_carrier_and_iteration_keep_selected_member_identity(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_enum_carrier_probe.py"
    source = (
        "from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, PluginCapability\n"
        "def read():\n    box = CapabilityDeclaration(PluginCapability.LLM)\n    return box.capability.value\n"
        "names = [member.value for member in PluginCapability]\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(
        ("retained supplier " in failure or "retained enum " in failure or "selected origin " in failure) and path in failure
        for failure in changed["global_failures"]
    ), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize("supplier", ["EnumType", "EnumMeta", "ReprEnum", "EnumDict", "_EnumDict"])
def test_actual_enum_allocation_suppliers_keep_canonical_mutation_denial(supplier):
    units = fencing._production_units()
    path = "src/elspeth/retained_enum_allocation_probe.py"
    source = (
        "from enum import " + supplier + "\n"
        "from elspeth.contracts.plugin_capabilities import PluginCapability\n"
        "def read():\n    return PluginCapability.LLM.value\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + supplier + ".__call__ = replacement\n")))
    assert any("selected origin attribute replaced " + path in failure for failure in changed["global_failures"]), changed[
        "global_failures"
    ]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "value.__class__.model_validate_json = replacement\n",
        "type(value).model_validate_json = replacement\n",
        "getattr(value, '__class__').model_validate_json = replacement\n",
        "opaque_callback(value.__class__)\n",
        "object.__getattribute__(value, '__class__').model_validate_json = replacement\n",
    ],
)
def test_nominal_data_remains_data_but_does_not_export_its_mutable_class(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_nominal_data_probe.py"
    source = (
        "from elspeth.web.catalog.schemas import PluginSummary\n"
        "value = PluginSummary(name='source', description='safe', plugin_type='source', config_fields=[])\n"
        "def read():\n    return value.name\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(
        ("retained supplier " in failure or "selected origin " in failure) and path in failure for failure in changed["global_failures"]
    ), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(Mapping)\n",
        "opaque_callback(loads)\n",
        "foreign_cast(Mapping[str, object], value)\n",
        "type Held = list[loads]\n",
        "opaque_callback(Held.__value__.__args__[0])\n",
        "cast = foreign_cast\ncast(Mapping[str, object], value)\n",
    ],
)
def test_canonical_cast_discards_only_its_complete_closed_type_operand(effect):
    units = fencing._production_units()
    path = "src/elspeth/discarded_supplier_type_probe.py"
    source = (
        "from collections.abc import Mapping\nfrom typing import cast\nfrom json import loads\n"
        "value = {}\nresult = cast(Mapping[str, object], value)\n"
        "type Held = list[Mapping]\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(Tree.__value__)\n",
        "opaque_callback(Tree.__value__.__args__[0])\n",
        "import json\njson.loads = replacement\n",
    ],
)
def test_recursive_lazy_type_declaration_retains_metadata_denial(effect):
    units = fencing._production_units()
    path = "src/elspeth/recursive_supplier_type_probe.py"
    source = (
        "from __future__ import annotations\nfrom collections.abc import Mapping\n"
        "type Tree = str | Mapping[str, Tree] | tuple[Tree, ...]\n"
        "def typed(value: Tree) -> Tree:\n    return value\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "before,after",
    [
        ("json: Mapping[str, Any] | None = None", "json: Mapping[str, Any] | None = foreign"),
        ("class HTTPCallRequest:", "class HTTPCallRequest(metaclass=foreign):"),
        ("json: Mapping[str, Any] | None = None", "global json\n    json = None"),
    ],
)
def test_json_import_and_class_local_http_slot_are_distinct_bindings(before, after):
    units = fencing._production_units()
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    source = next(unit for unit in units if unit.path == "src/elspeth/contracts/call_data.py")
    assert source.source.count(before) == 1
    changed = fencing._parse_source(source.path, source.source.replace(before, after, 1))
    result = fencing._reserve_run(tuple(changed if unit.path == source.path else unit for unit in units))
    assert any(
        "selected imported binding" in failure and "elspeth.contracts.call_data:json" in failure for failure in result["global_failures"]
    ), result["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "holder = {PluginCapability.LLM: None}\nopaque_callback(holder)\n",
        "holder = {PluginCapability.LLM: None}\ncopy = dict(holder)\n",
        "holder = [PluginCapability.LLM]\ncopy = sorted(holder)\n",
        "from dataclasses import asdict\nbox = CapabilityDeclaration(PluginCapability.LLM)\nasdict(box)\n",
    ],
)
def test_retained_enum_keys_and_known_callers_do_not_erase_capability_origin(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_enum_transfer_probe.py"
    source = (
        "from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, PluginCapability\n"
        "box = CapabilityDeclaration(PluginCapability.LLM)\n"
        "name = box.capability.value\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any("retained enum value passed to unclosed caller " + path in failure for failure in changed["global_failures"]), changed[
        "global_failures"
    ]


def test_actual_eager_engine_span_annotation_retains_canonical_mapping():
    from collections.abc import Mapping
    from typing import get_origin

    from elspeth.contracts.events import _validate_engine_span_attributes

    assert get_origin(_validate_engine_span_attributes.__annotations__["attributes"]) is Mapping


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "annotation.__origin__.__getitem__ = replacement\n",
        "opaque_callback(annotation.__origin__)\n",
        "setattr(annotation.__origin__, '__getitem__', replacement)\n",
        "getattr(_validate_engine_span_attributes, '__annotations__')['attributes'].__origin__.__getitem__ = replacement\n",
        "from typing import get_type_hints\nget_type_hints(_validate_engine_span_attributes)['attributes'].__origin__.__getitem__ = replacement\n",
    ],
)
def test_actual_eager_annotation_recovery_preserves_supplier_mutation_denials(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_annotation_probe.py"
    source = (
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "annotation = _validate_engine_span_attributes.__annotations__['attributes']\n"
        "name = annotation.__origin__.__name__\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(
        ("retained supplier " in failure or "selected origin " in failure) and path in failure for failure in changed["global_failures"]
    ), changed["global_failures"]


def test_actual_dataclass_field_retains_its_nominal_ceiling_type():
    from dataclasses import fields

    from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation

    field = next(field for field in fields(ComposerMutationRevocation) if field.name == "ceiling")
    assert field.type is ComposerMutationCeiling


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "for field in fields(ComposerMutationRevocation):\n    field.type.__new__ = replacement\n",
        "for field in fields(ComposerMutationRevocation):\n    opaque_callback(field.type)\n",
        "type(fields(ComposerMutationRevocation)[0].type).__call__ = replacement\n",
    ],
)
def test_dataclass_field_iteration_does_not_erase_retained_type_identity(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_field_metadata_probe.py"
    source = (
        "from dataclasses import fields\n"
        "from elspeth.contracts.composer_mutation import ComposerMutationRevocation\n"
        "def read():\n    for field in fields(ComposerMutationRevocation):\n        name = field.name\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(
        ("retained supplier " in failure or "selected origin " in failure) and path in failure for failure in changed["global_failures"]
    ), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "holder = [annotation]\nopaque_callback(holder)\n",
        "holder = {annotation: None}\nopaque_callback(holder)\n",
        "holder = {'value': annotation}\nholder['value'].__origin__.__getitem__ = replacement\n",
        "def leak():\n    return {'value': annotation}\n",
    ],
)
def test_metadata_container_keeps_complete_retained_leaf_origin(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_metadata_container_probe.py"
    source = (
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "annotation = _validate_engine_span_attributes.__annotations__['attributes']\n"
        "name = annotation.__origin__.__name__\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any("retained supplier " in failure and path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(annotation)\n",
        "result = _is_union(annotation, callback=opaque_callback)\n",
        "import elspeth.web.composer.redaction as owner\nowner.Union = replacement\n",
        "import elspeth.web.composer.redaction as owner\nsetattr(owner, 'UnionType', replacement)\n",
        "_is_union.__code__ = replacement\n",
    ],
)
def test_metadata_identity_boolean_consumer_preserves_all_other_transfer_denials(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_metadata_boolean_probe.py"
    source = (
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "from elspeth.web.composer.redaction import _is_union\n"
        "annotation = _validate_engine_span_attributes.__annotations__['attributes']\n"
        "result = _is_union(origin=annotation)\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "iterator = walk_model_schema(_RepairToolCallShadowModel)\nopaque_callback(iterator)\n",
        "for node in walk_model_schema(_RepairToolCallShadowModel):\n    opaque_callback(node.field_type)\n",
        "for node in walk_model_schema(_RepairToolCallShadowModel):\n    node.metadata[0].func = replacement\n",
        "iterator = walk_model_schema(_RepairToolCallShadowModel)\nvalues = list(iterator)\n",
        "iterator = walk_model_schema(_RepairToolCallShadowModel)\nopaque_callback({'value': iterator})\n",
    ],
)
def test_redaction_iterator_and_nodes_are_retaining_not_json_consumers(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_redaction_node_probe.py"
    source = "from elspeth.web.composer.redaction import walk_model_schema, _RepairToolCallShadowModel\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any("retained supplier " in failure and path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(metadata)\n",
        "import elspeth.web.composer.redaction as owner\nowner.any = replacement\n",
        "import elspeth.web.composer.redaction as owner\nowner.isinstance = replacement\n",
        "_has_sensitive.__code__ = replacement\n",
        "_count_sensitive.__code__ = replacement\n",
    ],
)
def test_exact_sensitive_metadata_count_consumers_do_not_export_marker_callbacks(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_metadata_count_probe.py"
    source = (
        "from elspeth.web.composer.redaction import _has_sensitive, _count_sensitive, _RepairToolCallShadowModel\n"
        "metadata = _RepairToolCallShadowModel.model_fields['arguments'].metadata\n"
        "present = _has_sensitive(metadata=metadata)\ncount = _count_sensitive(metadata)\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "before,after",
    [
        ("return any(isinstance(m, _SensitiveMarker) for m in metadata)", "return opaque_callback(metadata)"),
        ("count += 1", "opaque_callback(m)\n            count += 1"),
        ("class _SensitiveMarker:", "class _SensitiveMarker(metaclass=foreign):"),
    ],
)
def test_metadata_scalar_receipt_checks_complete_executed_owner_and_marker_allocation(before, after):
    units = fencing._production_units()
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    unit = next(unit for unit in units if unit.path == "src/elspeth/web/composer/redaction.py")
    # Scope count mutation to its complete actual function to avoid unrelated counters.
    target = fencing._reserve_node_at(unit, "_count_sensitive") if before == "count += 1" else unit.tree
    segment = ast.get_source_segment(unit.source, target) if target is not unit.tree else unit.source
    assert segment is not None and segment.count(before) == 1
    changed_segment = segment.replace(before, after, 1)
    assert unit.source.count(segment) == 1
    changed = fencing._parse_source(unit.path, unit.source.replace(segment, changed_segment, 1))
    result = fencing._reserve_run(tuple(changed if old.path == unit.path else old for old in units))
    assert any("retained metadata scalar owner changed " in failure for failure in result["global_failures"]), result["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(normalized)\n",
        "normalized[0].__origin__.__getitem__ = replacement\n",
        "inner, metadata = normalized\nopaque_callback(inner)\n",
        "inner, metadata = normalized\ninner.__origin__.__getitem__ = replacement\n",
        "def leak():\n    inner, metadata = normalized\n    return {'type': inner}\n",
        "import elspeth.web.composer.redaction as owner\nowner.get_origin = replacement\n",
    ],
)
def test_complete_metadata_normalizers_retain_every_returned_and_destructured_identity(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_metadata_normalizer_probe.py"
    source = (
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "from elspeth.web.composer.redaction import _normalise\n"
        "annotation = _validate_engine_span_attributes.__annotations__['attributes']\n"
        "normalized = _normalise(field_type=annotation, metadata=())\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(
        ("retained supplier " in failure or "selected origin " in failure) and path in failure for failure in changed["global_failures"]
    ), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "    opaque_callback(field_type)\n",
        "    global get_origin\n    get_origin = replacement\n",
    ],
)
def test_normalizer_receipt_refuses_new_effect_before_retained_return(effect):
    units = fencing._production_units()
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    unit = next(unit for unit in units if unit.path == "src/elspeth/web/composer/redaction.py")
    needle = "    aliases: set[TypeAliasType] = set()\n"
    assert unit.source.count(needle) == 1
    changed = fencing._parse_source(unit.path, unit.source.replace(needle, effect + needle, 1))
    result = fencing._reserve_run(tuple(changed if old.path == unit.path else old for old in units))
    assert any("retained metadata normalizer owner changed " in failure for failure in result["global_failures"]), result["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "recovery",
    [
        "vars(owner)['__annotations__']['attributes']",
        "owner.__dict__.get('__annotations__')['attributes']",
        "getattr(owner, '__annotations__')['attributes']",
        "attrgetter('__annotations__')(owner)['attributes']",
        "methodcaller('__getattribute__', '__annotations__')(owner)['attributes']",
    ],
)
@pytest.mark.parametrize(
    "effect",
    [
        "annotation.__origin__.__getitem__ = replacement\n",
        "opaque_callback(annotation)\n",
    ],
)
def test_literal_metadata_namespace_and_query_recovery_preserve_actual_type_identity(recovery, effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_metadata_query_probe.py"
    source = (
        "from operator import attrgetter, methodcaller\n"
        "from elspeth.contracts.events import _validate_engine_span_attributes as owner\n"
        "annotation = " + recovery + "\nname = annotation.__origin__.__name__\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any("retained supplier " in failure and path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(query)\n",
        "def leak():\n    return query\n",
        "query.__class__.__call__ = replacement\n",
        "query(owner, callback=foreign)\n",
    ],
)
def test_metadata_query_factory_is_not_a_transferable_callback_permission(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_metadata_query_factory_probe.py"
    source = (
        "from operator import attrgetter\n"
        "from elspeth.contracts.events import _validate_engine_span_attributes as owner\n"
        "query = attrgetter('__annotations__')\n"
        "name = query(owner)['attributes'].__origin__.__name__\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any("retained supplier " in failure and path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "expression",
    [
        "[item for item in ('ordinary',)]",
        "{item for item in ('ordinary',)}",
        "{item: item for item in ('ordinary',)}",
        "(item for item in ('ordinary',))",
        "lambda item: item",
        "lambda *items, **options: (items, options)",
        "lambda: (local := 'ordinary')",
    ],
)
def test_retained_binding_lookup_handles_expression_scopes_without_statement_bodies(expression):
    units = fencing._production_units()
    path = "src/elspeth/retained_expression_scope_probe.py"
    source = "value = " + expression + "\n"
    result = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert result["global_failures"] == []
    assert result["unproven_web_rows"] == []


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "    result = [opaque_callback(inner) for item in ('ordinary',)]\n",
        "    result = {opaque_callback(inner) for item in ('ordinary',)}\n",
        "    result = {item: opaque_callback(inner) for item in ('ordinary',)}\n",
        "    result = (opaque_callback(inner) for item in ('ordinary',))\n",
        "    result = lambda: opaque_callback(inner)\n",
        "    result = [opaque_callback(item) for item in (inner,)]\n",
    ],
)
def test_expression_scopes_keep_enclosing_destructured_and_iteration_metadata(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_expression_capture_probe.py"
    source = (
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "from elspeth.web.composer.redaction import _normalise\n"
        "def inspect():\n"
        "    annotation = _validate_engine_span_attributes.__annotations__['attributes']\n"
        "    inner, metadata = _normalise(annotation, ())\n"
        "    names = [inner for inner in ('ordinary',)]\n"
        "    name = inner.__origin__.__name__\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any("retained supplier " in failure and path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(annotation)\n",
        "_type_identity(annotation, extra=foreign)\n",
        "import elspeth.contracts.runtime_val_manifest as owner\nowner.type = replacement\n",
        "import elspeth.contracts.runtime_val_manifest as owner\nsetattr(owner, '_TYPING_SPECIAL_FORM_NAMES', (replacement,))\n",
        "_type_identity.__code__ = replacement\n",
        "_typing_special_form_name.__code__ = replacement\n",
    ],
)
def test_manifest_name_projection_discards_only_complete_checked_scalar_result(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_manifest_name_probe.py"
    source = (
        "from collections.abc import Mapping\n"
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "from elspeth.contracts.runtime_val_manifest import _type_identity, _typing_special_form_name\n"
        "annotation = _validate_engine_span_attributes.__annotations__['attributes']\n"
        "name = _type_identity(cls=annotation.__origin__)\n"
        "canonical_name = _type_identity(Mapping)\n"
        "special_name = _typing_special_form_name(value=annotation)\n"
        "ordinary_consumer(name, canonical_name, special_name)\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "before,after",
    [
        ('    return f"{module_name}:{qualname}"', '    opaque_callback(cls)\n    return f"{module_name}:{qualname}"'),
        ("                return special_form_name", "                return value"),
        ('_TYPING_SPECIAL_FORM_MODULES = ("typing", "typing_extensions")', "_TYPING_SPECIAL_FORM_MODULES = foreign"),
        (
            '_TYPING_SPECIAL_FORM_NAMES = ("Annotated", "ClassVar", "Literal", "NotRequired", "ReadOnly", "Required", "Union")',
            "_TYPING_SPECIAL_FORM_NAMES = (foreign,)",
        ),
    ],
)
def test_manifest_scalar_receipt_requires_complete_body_and_literal_name_tables(before, after):
    units = fencing._production_units()
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    unit = next(unit for unit in units if unit.path == "src/elspeth/contracts/runtime_val_manifest.py")
    assert unit.source.count(before) == 1
    changed = fencing._parse_source(unit.path, unit.source.replace(before, after, 1))
    result = fencing._reserve_run(tuple(changed if old.path == unit.path else old for old in units))
    assert any("retained manifest scalar " in failure for failure in result["global_failures"]), result["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(PluginCapability.LLM)\n",
        "node_has_capability(node, PluginCapability.LLM, callback=foreign)\n",
        "node_has_capability(PluginCapability.LLM, PluginCapability.LLM)\n",
        "import elspeth.web.plugin_policy.coverage as owner\nowner.any = replacement\n",
        "import elspeth.web.plugin_policy.coverage as owner\nsetattr(owner, 'get_shared_plugin_manager', replacement)\n",
        "node_has_capability.__code__ = replacement\n",
    ],
)
def test_actual_capability_identity_consumers_close_only_the_exact_enum_parameter(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_enum_identity_probe.py"
    source = (
        "from elspeth.contracts.plugin_capabilities import PluginCapability\n"
        "from elspeth.web.plugin_policy.coverage import node_has_capability, source_has_capability, transform_plugin_has_capability\n"
        "a = node_has_capability(node, PluginCapability.LLM)\n"
        "b = source_has_capability(source=source, capability=PluginCapability.LLM)\n"
        "c = transform_plugin_has_capability(plugin, capability=PluginCapability.LLM)\n"
        "ordinary_consumer(a, b, c)\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "before,after",
    [
        (
            "    return transform_plugin_has_capability(node.plugin, capability)",
            "    opaque_callback(capability)\n    return transform_plugin_has_capability(node.plugin, capability)",
        ),
        ("    return transform_plugin_has_capability(node.plugin, capability)", "    return capability"),
    ],
)
def test_enum_identity_consumer_receipt_refuses_complete_body_effect_or_retained_return(before, after):
    units = fencing._production_units()
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    unit = next(unit for unit in units if unit.path == "src/elspeth/web/plugin_policy/coverage.py")
    assert unit.source.count(before) == 1
    changed = fencing._parse_source(unit.path, unit.source.replace(before, after, 1))
    result = fencing._reserve_run(tuple(changed if old.path == unit.path else old for old in units))
    assert any("retained enum identity consumer changed " in failure for failure in result["global_failures"]), result["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize("attribute", ["__name__", "__qualname__", "__module__"])
def test_unknown_mixed_metadata_attribute_is_not_a_scalar_escape(attribute):
    units = fencing._production_units()
    path = "src/elspeth/retained_metadata_name_probe.py"
    source = (
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "annotation = _validate_engine_span_attributes.__annotations__['attributes']\n"
        "name = annotation.__origin__." + attribute + "\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + "opaque_callback(name)\n")))
    assert any("retained supplier " in failure and path in failure for failure in changed["global_failures"]), changed["global_failures"]


def test_selected_fields_supplier_extends_existing_receiver_proof_with_one_actual_store():
    units = fencing._production_units()
    old_failures, old_leaves, old_roots, old_stores = fencing._reserve_dataclass_field_receiver_shapes(units)
    assert old_failures == ()
    assert len(old_stores) == 5
    failures, leaves, roots, stores = fencing._reserve_dataclass_field_receiver_shapes(units, include_fields=True)
    assert failures == ()
    qualified = "elspeth.contracts.errors.DeclaredInputFieldAbsentViolation"
    assert leaves == old_leaves | {qualified}
    assert roots == old_roots | {qualified}
    unit = next(unit for unit in units if unit.path == "src/elspeth/contracts/errors.py")
    constructor = fencing._reserve_node_at(unit, "DeclaredInputFieldAbsentViolation.__init__")
    actual = [
        statement.targets[0]
        for statement in constructor.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Attribute)
        and statement.targets[0].attr == "fields"
    ]
    assert len(actual) == 1
    assert stores == old_stores | {(unit.path, id(actual[0]))}
    reparsed = fencing._parse_source(unit.path, unit.source)
    detached_failures, _, _, detached_stores = fencing._reserve_dataclass_field_receiver_shapes(
        tuple(reparsed if old.path == unit.path else old for old in units),
        include_fields=True,
    )
    assert detached_failures == ()
    assert (unit.path, id(actual[0])) not in detached_stores


@pytest.mark.parametrize(
    "before,after",
    [
        (
            "class DeclaredInputFieldAbsentViolation(PluginContractViolation):",
            "class DeclaredInputFieldAbsentViolation(PluginContractViolation, metaclass=foreign):",
        ),
        (
            "    def __init__(self, *, component: str, fields: tuple[str, ...]) -> None:",
            "    @staticmethod\n    def __init__(self, *, component: str, fields: tuple[str, ...]) -> None:",
        ),
        ("        self.fields = fields", "        self = foreign\n        self.fields = fields"),
        ("        self.fields = fields", "        self.fields = fields\n        self.fields = foreign"),
        (
            "    def to_transform_error_reason(self) -> TransformErrorReason:",
            "    @property\n    def fields(self):\n        return foreign\n\n    def to_transform_error_reason(self) -> TransformErrorReason:",
        ),
    ],
)
def test_plural_fields_receiver_requires_actual_allocation_formal_and_slot(before, after):
    units = fencing._production_units()
    failures, _, _, _ = fencing._reserve_dataclass_field_receiver_shapes(units, include_fields=True)
    assert failures == ()
    unit = next(unit for unit in units if unit.path == "src/elspeth/contracts/errors.py")
    cls = fencing._reserve_node_at(unit, "DeclaredInputFieldAbsentViolation")
    segment = ast.get_source_segment(unit.source, cls)
    assert segment is not None and segment.count(before) == 1
    assert unit.source.count(segment) == 1
    changed = fencing._parse_source(unit.path, unit.source.replace(segment, segment.replace(before, after, 1), 1))
    failures, _, _, _ = fencing._reserve_dataclass_field_receiver_shapes(
        tuple(changed if old.path == unit.path else old for old in units),
        include_fields=True,
    )
    assert any("elspeth.contracts.errors.DeclaredInputFieldAbsentViolation" in failure for failure in failures), failures


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "DeclaredInputFieldAbsentViolation.__init__(dataclasses, component='node', fields=('name',))\n",
        "DeclaredInputFieldAbsentViolation.__new__ = replacement\n",
        "opaque_callback(DeclaredInputFieldAbsentViolation)\n",
        "class Foreign(DeclaredInputFieldAbsentViolation):\n    pass\n",
    ],
)
def test_plural_fields_allocation_receipt_keeps_foreign_receiver_and_class_escape_refusal(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_fields_receiver_probe.py"
    source = (
        "import dataclasses\n"
        "from elspeth.contracts.errors import DeclaredInputFieldAbsentViolation\n"
        "error = DeclaredInputFieldAbsentViolation(component='node', fields=('name',))\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(Annotated)\n",
        "result = Annotated == foreign\n",
        "result = foreign in (Annotated,)\n",
        "saved = [Annotated]\nopaque_callback(saved)\n",
    ],
)
def test_canonical_supplier_identity_operators_do_not_grant_overloaded_or_retaining_consumers(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_identity_operator_probe.py"
    source = (
        "from typing import Annotated, TypeAliasType\n"
        "from types import UnionType, ModuleType\n"
        "a = foreign is Annotated\nb = Annotated is not foreign\n"
        "c = foreign is Annotated is other\n"
        "d = isinstance(foreign, TypeAliasType)\ne = isinstance(foreign, UnionType)\nf = isinstance(foreign, ModuleType)\n"
        "ordinary_consumer(a, b, c, d, e, f)\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "annotation.__origin__.__getitem__ = replacement\ndel annotation\n",
        "opaque_callback(annotation)\ndel annotation\n",
        "annotation = foreign\ndel annotation\n",
        "from collections.abc import Mapping\ndel Mapping\n",
    ],
)
def test_direct_derived_metadata_alias_cleanup_preserves_mutation_and_import_refusals(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_metadata_cleanup_probe.py"
    source = (
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "annotation = _validate_engine_span_attributes.__annotations__['attributes']\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source + "del annotation\n")))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure or "elspeth.retained_metadata_cleanup_probe:" in failure for failure in changed["global_failures"]), changed[
        "global_failures"
    ]


def test_actual_pydantic_public_and_underlying_field_tables_share_metadata_identity():
    from collections.abc import Mapping
    from typing import get_origin

    from elspeth.web.composer.redaction import _RepairToolCallShadowModel

    assert _RepairToolCallShadowModel.model_fields is _RepairToolCallShadowModel.__pydantic_fields__
    field = _RepairToolCallShadowModel.__pydantic_fields__["arguments"]
    assert field is _RepairToolCallShadowModel.model_fields["arguments"]
    assert get_origin(field.annotation) is Mapping
    assert field.metadata is _RepairToolCallShadowModel.model_fields["arguments"].metadata


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "recovery",
    [
        "owner.__pydantic_fields__",
        "vars(owner)['__pydantic_fields__']",
        "owner.__dict__.get('__pydantic_fields__')",
        "getattr(owner, '__pydantic_fields__')",
        "attrgetter('__pydantic_fields__')(owner)",
        "methodcaller('__getattribute__', '__pydantic_fields__')(owner)",
    ],
)
@pytest.mark.parametrize(
    "effect",
    [
        "fields['arguments'].annotation.__origin__.__getitem__ = replacement\n",
        "setattr(fields['arguments'].metadata[0], 'func', replacement)\n",
        "opaque_callback(fields)\n",
    ],
)
def test_underlying_pydantic_field_table_retains_annotation_and_callback_origins(recovery, effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_pydantic_fields_probe.py"
    source = (
        "from operator import attrgetter, methodcaller\n"
        "from elspeth.web.composer.redaction import _RepairToolCallShadowModel as owner\n"
        "fields = " + recovery + "\n"
        "annotation = fields['arguments'].annotation\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(
        ("retained supplier " in failure and path in failure) or ("selected origin " in failure and path in failure)
        for failure in changed["global_failures"]
    ), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "target,iterable,leaf",
    [
        ("key, value", "[(annotation, annotation)]", "value"),
        ("key, (value, other)", "[(annotation, (annotation, annotation))]", "other"),
        ("[key, value]", "[[annotation, annotation]]", "key"),
    ],
)
@pytest.mark.parametrize("effect", ["opaque_callback(LEAF)", "LEAF.__origin__.__getitem__ = replacement"])
def test_retained_metadata_iteration_unpacking_preserves_every_leaf(target, iterable, leaf, effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_metadata_unpacking_probe.py"
    source = (
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "annotation = _validate_engine_span_attributes.__annotations__['attributes']\n"
        "for " + target + " in " + iterable + ":\n    pass\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect.replace("LEAF", leaf) + "\n")))
    assert any("retained supplier " in failure and path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "target,tail",
    [
        ("key, key", ""),
        ("key, *values", ""),
        ("key, owner.value", ""),
        ("key, value", "value = foreign\n"),
    ],
)
def test_retained_metadata_iteration_unpacking_refuses_ambiguous_or_effectful_targets(target, tail):
    units = fencing._production_units()
    path = "src/elspeth/retained_metadata_unpacking_binding_probe.py"
    header = (
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "annotation = _validate_engine_span_attributes.__annotations__['attributes']\n"
    )
    normal = fencing._reserve_run(
        (*units, fencing._parse_source(path, header + "for key, value in [(annotation, annotation)]:\n    pass\n"))
    )
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    source = header + "for " + target + " in [(annotation, annotation)]:\n    pass\n" + tail
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert any("retained supplier iteration " in failure and path in failure for failure in changed["global_failures"]), changed[
        "global_failures"
    ]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "saved = [value for key, value in [(annotation, annotation)]]\nopaque_callback(saved)\n",
        "saved = [value for key, value in [(annotation, annotation)] if opaque_callback(value)]\n",
    ],
)
def test_retained_metadata_comprehension_unpacking_keeps_value_and_filter_refusals(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_metadata_unpacking_comprehension_probe.py"
    header = (
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "annotation = _validate_engine_span_attributes.__annotations__['attributes']\n"
    )
    normal = fencing._reserve_run(
        (*units, fencing._parse_source(path, header + "saved = [value for key, value in [(annotation, annotation)]]\n"))
    )
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, header + effect)))
    assert any("retained supplier " in failure and path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize("producer", ["get_type_hints(owner, include_extras=True)", "get_annotations(owner)"])
@pytest.mark.parametrize("view", ["items", "keys", "values"])
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(view)\n",
        "view.mapping['attributes'].__origin__.__getitem__ = replacement\n",
        "opaque_callback(hints)\n",
    ],
)
def test_canonical_annotation_dictionary_views_preserve_retained_metadata(producer, view, effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_annotation_dictionary_probe.py"
    source = (
        "from typing import get_type_hints\nfrom inspect import get_annotations\n"
        "from elspeth.contracts.events import _validate_engine_span_attributes as owner\n"
        "hints = " + producer + "\n"
        "view = hints." + view + "()\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any("retained supplier " in failure and path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "factory = hints.items\nview = factory()\n",
        "view = hints.items(callback=foreign)\n",
        "value = hints['attributes']\nview = value.items()\n",
        "view = owner.__annotations__.items()\n",
        "opaque_callback(hints.items)\n",
        "for key, value in hints.items():\n    opaque_callback(value)\n",
        "for key, value in hints.items():\n    opaque_callback(key)\n",
    ],
)
def test_annotation_dictionary_view_grant_requires_actual_fresh_dictionary_and_direct_zero_arg_call(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_annotation_dictionary_shape_probe.py"
    header = (
        "from typing import get_type_hints\n"
        "from elspeth.contracts.events import _validate_engine_span_attributes as owner\n"
        "hints = get_type_hints(owner, include_extras=True)\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, header + "for key, value in hints.items():\n    pass\n")))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, header + effect)))
    assert any("retained supplier " in failure and path in failure for failure in changed["global_failures"]), changed["global_failures"]


def test_actual_annotation_owners_return_fresh_builtin_dictionaries_with_retained_values():
    from inspect import get_annotations
    from typing import get_type_hints

    from elspeth.web.composer.redaction import _RepairToolCallShadowModel

    raw = get_annotations(_RepairToolCallShadowModel)
    raw_again = get_annotations(_RepairToolCallShadowModel)
    resolved = get_type_hints(_RepairToolCallShadowModel, include_extras=True)
    resolved_again = get_type_hints(_RepairToolCallShadowModel, include_extras=True)
    assert type(raw) is dict and type(resolved) is dict
    assert raw is not raw_again and resolved is not resolved_again
    assert raw["arguments"] is raw_again["arguments"]
    assert resolved["arguments"] == resolved_again["arguments"]
    assert raw.items().mapping["arguments"] is raw["arguments"]
    assert resolved.values().mapping["arguments"] is resolved["arguments"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize("slot", ["model_fields", "__pydantic_fields__"])
@pytest.mark.parametrize(
    "effect",
    [
        "owner.SLOT = replacement\n",
        "del owner.SLOT\n",
        "setattr(owner, 'SLOT', replacement)\n",
        "delattr(owner, 'SLOT')\n",
        "type.__setattr__(owner, 'SLOT', replacement)\n",
        "namespace = vars(owner)\nnamespace['SLOT'] = replacement\n",
        "owner.__dict__['SLOT'] = replacement\n",
        "class Foreign(owner):\n    SLOT = replacement\n",
        "class Foreign(owner):\n    @classmethod\n    def SLOT(cls):\n        return replacement\n",
    ],
)
def test_model_field_table_slot_replacement_cannot_hide_behind_unselected_model_receiver(slot, effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_model_field_slot_probe.py"
    source = (
        "from elspeth.core.config import ElspethSettings as owner\nfields = owner.model_fields\nannotation = fields['sources'].annotation\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect.replace("SLOT", slot))))
    assert any("retained model field-table " in failure and path in failure for failure in changed["global_failures"]), changed[
        "global_failures"
    ]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "capture",
    [
        "lambda: value",
        "lambda bound=value: bound",
        "lambda *, bound=value: bound",
        "lambda: [value]",
        "lambda: value.__origin__",
    ],
)
def test_unpacked_metadata_cannot_escape_through_implicit_lambda_return_or_defaults(capture):
    units = fencing._production_units()
    path = "src/elspeth/retained_unpacking_lambda_probe.py"
    header = (
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "annotation = _validate_engine_span_attributes.__annotations__['attributes']\n"
    )
    normal_source = header + "saved = [value for key, value in [(annotation, annotation)]]\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, normal_source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed_source = header + "saved = [(" + capture + ") for key, value in [(annotation, annotation)]]\n"
    changed = fencing._reserve_run((*units, fencing._parse_source(path, changed_source)))
    assert any(
        "retained supplier lambda metadata capture is unclosed " in failure and path in failure for failure in changed["global_failures"]
    ), changed["global_failures"]


@pytest.mark.landscape_long_run
def test_lambda_parameter_shadow_does_not_inherit_outer_retained_metadata_origin():
    units = fencing._production_units()
    path = "src/elspeth/retained_unpacking_lambda_shadow_probe.py"
    source = (
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "annotation = _validate_engine_span_attributes.__annotations__['attributes']\n"
        "for key, value in [(annotation, annotation)]:\n    pass\n"
        "ordinary = lambda value: value\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + "captured = lambda: value\n")))
    assert any(
        "retained supplier lambda metadata capture is unclosed " in failure and path in failure for failure in changed["global_failures"]
    ), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(Outer.__value__.__args__[0])\n",
        "Outer.__value__.__args__[0].__value__.__origin__.__getitem__ = replacement\n",
        "Inner = foreign\n",
        "Sequence = foreign\n",
    ],
)
def test_nested_local_lazy_type_alias_grammar_retains_recovery_and_binding_refusals(effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_nested_lazy_alias_probe.py"
    source = (
        "from collections.abc import Mapping, Sequence\n"
        "type Inner = Mapping[str, object]\n"
        "type Outer = list[Inner]\n"
        "type Recursive = str | Mapping[str, Recursive] | Sequence[Recursive]\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure or "elspeth.retained_nested_lazy_alias_probe:" in failure for failure in changed["global_failures"]), (
        changed["global_failures"]
    )


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "changed_inner",
    [
        "type Inner = list[loads]",
        "type Inner = Mapping[str, loads]",
        "type Inner = Mapping[str, opaque_callback()]",
    ],
)
def test_nested_lazy_alias_does_not_launder_function_or_executed_leaf_as_type(changed_inner):
    units = fencing._production_units()
    path = "src/elspeth/retained_nested_lazy_function_probe.py"
    source = "from collections.abc import Mapping\nfrom json import loads\ntype Inner = Mapping[str, object]\ntype Outer = list[Inner]\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed_source = source.replace("type Inner = Mapping[str, object]", changed_inner)
    changed = fencing._reserve_run((*units, fencing._parse_source(path, changed_source)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "retained",
    [
        "_validate_engine_span_attributes.__annotations__['attributes']",
        "PluginCapability.LLM",
    ],
)
@pytest.mark.parametrize(
    "effect",
    [
        "holder.value = retained\n",
        "table['value'] = retained\n",
        "table['value'], alias = (retained, retained)\n",
        "class Stored:\n    value = retained\n",
        "class Stored:\n    if True:\n        value = retained\n",
    ],
)
def test_retained_metadata_and_enum_values_cannot_disappear_into_unknown_carrier_storage(retained, effect):
    units = fencing._production_units()
    path = "src/elspeth/retained_unknown_carrier_storage_probe.py"
    source = (
        "from elspeth.contracts.events import _validate_engine_span_attributes\n"
        "from elspeth.contracts.plugin_capabilities import PluginCapability\n"
        "class Holder:\n    pass\n"
        "holder = Holder()\ntable = {}\n"
        "retained = " + retained + "\n"
        "alias = retained\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any("retained supplier stored in " in failure and path in failure for failure in changed["global_failures"]), changed[
        "global_failures"
    ]


@pytest.mark.parametrize(
    "before,after",
    [
        ("zip(fields(self), astuple(self), strict=True)", "zip(fields(self), astuple(self))"),
        ("zip(fields(self), astuple(self), strict=True)", "opaque_callback(fields(self), astuple(self))"),
        ("type(value) is not str or not value.strip()", "not value.strip() or type(value) is not str"),
        ('raise ValueError(f"ApprovalBinding.{field.name} must be a nonblank exact string")', "opaque_callback(field)"),
        ("names = {field.name for field in fields(cls)}", "names = {field.type for field in fields(cls)}"),
        ("from dataclasses import asdict, astuple, dataclass, fields", "from foreign import asdict, astuple, dataclass, fields"),
        ("@dataclass(frozen=True, slots=True)\nclass ApprovalBinding:", "@dataclass(frozen=False, slots=True)\nclass ApprovalBinding:"),
        ("class ApprovalBinding:", "class ApprovalBinding(Foreign):"),
    ],
)
def test_approval_field_consumer_receipt_requires_complete_actual_owner_and_suppliers(before, after):
    path = "src/elspeth/web/coordination/approval_authority.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    normal = fencing._reserve_approval_binding_metadata_receipt((unit,))
    assert normal["failures"] == []
    assert len(normal["calls"]) == 1
    assert normal["origins"]
    assert unit.source.count(before) == 1
    changed = fencing._parse_source(path, unit.source.replace(before, after))
    refused = fencing._reserve_approval_binding_metadata_receipt((changed,))
    assert refused["failures"]
    assert refused["calls"] == set()
    assert refused["origins"] == {}


def test_approval_field_consumer_receipt_keeps_same_ast_and_separate_tuple_columns():
    path = "src/elspeth/web/coordination/approval_authority.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    receipt = fencing._reserve_approval_binding_metadata_receipt((unit,))
    assert receipt["failures"] == []
    cls = fencing._reserve_node_at(unit, "ApprovalBinding")
    post = fencing._reserve_node_at(unit, "ApprovalBinding.__post_init__")
    assert receipt["calls"] == {(path, id(post.body[0].iter))}
    field_loads = [node for node in ast.walk(cls) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "field"]
    value_loads = [node for node in ast.walk(post) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "value"]
    assert field_loads and value_loads
    assert all(receipt["origins"][path, id(node)] == ("type-field", "dataclasses.Field") for node in field_loads)
    assert all(receipt["origins"][path, id(node)] == ("unknown", "approval value") for node in value_loads)
    detached = fencing._parse_source(path, unit.source)
    detached_receipt = fencing._reserve_approval_binding_metadata_receipt((detached,))
    assert detached_receipt["failures"] == []
    assert receipt["calls"].isdisjoint(detached_receipt["calls"])
    assert set(receipt["origins"]).isdisjoint(detached_receipt["origins"])
    assert fencing._reserve_approval_binding_metadata_receipt(())["failures"]
    assert fencing._reserve_approval_binding_metadata_receipt((unit, unit))["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "ApprovalBinding.__post_init__(foreign)\n",
        "saved = ApprovalBinding.from_json\nopaque_callback(saved)\n",
        "class Foreign(ApprovalBinding):\n    pass\n",
        "ApprovalBinding.__dataclass_fields__['config_hash'].name = replacement\n",
        "setattr(ApprovalBinding, '__post_init__', replacement)\n",
        "import elspeth.web.coordination.approval_authority as owner\nowner.zip = replacement\n",
        "import dataclasses\ndataclasses.fields = replacement\n",
        "held = binding.__post_init__\nopaque_callback(held)\n",
    ],
)
def test_approval_field_consumer_full_proof_preserves_class_supplier_and_method_refusals(effect):
    units = fencing._production_units()
    path = "src/elspeth/approval_field_consumer_probe.py"
    source = (
        "from elspeth.web.coordination.approval_authority import ApprovalBinding\n"
        "binding = ApprovalBinding('a', 'b', 'c', 'd', 'e', 'f')\n"
        "serialized = binding.as_json()\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.parametrize(
    "before,after",
    [
        ("return _SensitiveMarker(summarizer=summarizer)", "return _SensitiveMarker(summarizer=opaque_callback(summarizer))"),
        ("self.summarizer = summarizer", "self.summarizer = summarizer()"),
        ("class _SensitiveMarker:", "class _SensitiveMarker(Foreign):"),
        ("class _SensitiveMarker:", "@foreign_decorator\nclass _SensitiveMarker:"),
        ('__slots__ = ("summarizer",)', '__slots__ = ("summarizer", "foreign")'),
        ("from __future__ import annotations", "# annotations execute eagerly"),
    ],
)
def test_sensitive_factory_receipt_preserves_complete_retaining_owner(before, after):
    path = "src/elspeth/web/composer/redaction.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    normal = fencing._reserve_sensitive_factory_metadata_receipt((unit,))
    assert normal["failures"] == []
    assert len(normal["returns"]) == 1
    assert unit.source.count(before) == 1
    changed = fencing._parse_source(path, unit.source.replace(before, after))
    refused = fencing._reserve_sensitive_factory_metadata_receipt((changed,))
    assert refused["failures"]
    assert refused["returns"] == set()


def test_sensitive_factory_receipt_requires_same_ast_and_unrebound_factory():
    path = "src/elspeth/web/composer/redaction.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    normal = fencing._reserve_sensitive_factory_metadata_receipt((unit,))
    assert normal["failures"] == []
    factory = fencing._reserve_node_at(unit, "Sensitive")
    assert normal["returns"] == {(path, id(factory.body[-1]))}
    assert isinstance(factory.body[-1], ast.Return)
    assert isinstance(factory.body[-1].value, ast.Call)
    assert factory.body[-1].value.func.id == "_SensitiveMarker"
    copied = fencing._parse_source(path, unit.source)
    copied_receipt = fencing._reserve_sensitive_factory_metadata_receipt((copied,))
    assert copied_receipt["failures"] == []
    assert normal["returns"].isdisjoint(copied_receipt["returns"])
    changed = fencing._parse_source(path, unit.source + "\nSensitive = replacement\n")
    refused = fencing._reserve_sensitive_factory_metadata_receipt((changed,))
    assert refused["failures"]
    assert refused["returns"] == set()
    assert fencing._reserve_sensitive_factory_metadata_receipt(())["failures"]
    assert fencing._reserve_sensitive_factory_metadata_receipt((unit, unit))["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "marker.summarizer.__code__ = replacement\n",
        "setattr(marker, 'summarizer', replacement)\n",
        "opaque_callback(marker.summarizer)\n",
        "held = marker.summarizer\nopaque_callback(held)\n",
        "import elspeth.web.composer.redaction as owner\nowner.Sensitive = replacement\n",
        "import elspeth.web.composer.redaction as owner\nowner._SensitiveMarker = replacement\n",
    ],
)
def test_sensitive_factory_retains_marker_callback_and_supplier_refusals(effect):
    units = fencing._production_units()
    path = "src/elspeth/sensitive_factory_retention_probe.py"
    source = "from elspeth.web.composer.redaction import Sensitive\nmarker = Sensitive()\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.parametrize(
    "before,after",
    [
        ('        if type(key) is not str:\n            raise TypeError("Payload schema annotation keys must be exact strings")\n', ""),
        ("if type(key) is not str:", "if not isinstance(key, str):"),
        ("required.add(key)", "required.add(annotation)"),
        ("optional.add(key)", "opaque_callback(key)"),
        ("hints = get_type_hints(schema, include_extras=True)", "hints = foreign_hints(schema)"),
        ("return frozenset(required), frozenset(optional)", "return hints, optional"),
    ],
)
def test_payload_schema_key_receipt_requires_actual_checked_owner(before, after):
    path = "src/elspeth/contracts/declaration_contracts.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    normal = fencing._reserve_payload_schema_key_metadata_receipt((unit,))
    assert normal["failures"] == []
    assert normal["origins"]
    assert before in unit.source
    changed = fencing._parse_source(path, unit.source.replace(before, after))
    refused = fencing._reserve_payload_schema_key_metadata_receipt((changed,))
    assert refused["failures"]
    assert refused["origins"] == {}


@pytest.mark.parametrize("effect", ["set = foreign\n", "str = foreign\n", "get_type_hints = foreign\n"])
def test_payload_schema_key_receipt_requires_actual_supplier_bindings(effect):
    path = "src/elspeth/contracts/declaration_contracts.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    assert fencing._reserve_payload_schema_key_metadata_receipt((unit,))["failures"] == []
    changed = fencing._parse_source(path, unit.source + "\n" + effect)
    refused = fencing._reserve_payload_schema_key_metadata_receipt((changed,))
    assert refused["failures"]
    assert refused["origins"] == {}


def test_payload_schema_key_receipt_preserves_guard_operand_values_and_ast_identity():
    path = "src/elspeth/contracts/declaration_contracts.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    receipt = fencing._reserve_payload_schema_key_metadata_receipt((unit,))
    assert receipt["failures"] == []
    owner = fencing._reserve_node_at(unit, "_resolve_typeddict_key_sets")
    loop = next(node for node in owner.body if isinstance(node, ast.For))
    guarded_keys = [
        node for node in ast.walk(loop.body[0]) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "key"
    ]
    later_keys = [
        node
        for statement in loop.body[1:]
        for node in ast.walk(statement)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "key"
    ]
    annotations = [
        node for node in ast.walk(owner) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "annotation"
    ]
    assert guarded_keys and later_keys and annotations
    assert all((path, id(node)) not in receipt["origins"] for node in (*guarded_keys, *annotations))
    assert receipt["origins"] == {(path, id(node)): ("supplier-scalar", "checked payload schema key") for node in later_keys}
    copied = fencing._parse_source(path, unit.source)
    copied_receipt = fencing._reserve_payload_schema_key_metadata_receipt((copied,))
    assert copied_receipt["failures"] == []
    assert set(receipt["origins"]).isdisjoint(copied_receipt["origins"])
    assert fencing._reserve_payload_schema_key_metadata_receipt(())["failures"]
    assert fencing._reserve_payload_schema_key_metadata_receipt((unit, unit))["failures"]


@pytest.mark.parametrize("name", ["type", "str", "set", "frozenset", "TypeError", "get_type_hints", "_resolve_typeddict_key_sets"])
def test_payload_schema_key_receipt_refuses_nested_global_supplier_replacement(name):
    path = "src/elspeth/contracts/declaration_contracts.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    normal = fencing._reserve_payload_schema_key_metadata_receipt((unit,))
    assert normal["failures"] == []
    assert normal["origins"]
    effect = "\ndef alter():\n    global " + name + "\n    " + name + " = lambda value: str\nalter()\n"
    changed = fencing._parse_source(path, unit.source + effect)
    refused = fencing._reserve_payload_schema_key_metadata_receipt((changed,))
    assert "payload schema key supplier global declaration escaped" in refused["failures"]
    assert refused["origins"] == {}


def test_protected_supplier_globals_preserve_unrelated_and_local_bindings_and_ast_identity():
    path = "src/elspeth/protected_supplier_global_probe.py"
    module = "elspeth.protected_supplier_global_probe"
    protected = {module + ".type", module + ".zip"}
    normal_source = (
        "def local(type):\n    return type\n"
        "def unrelated():\n    global counter\n    counter = 1\n"
        "def outer():\n    type = 1\n"
        "    def inner():\n        nonlocal type\n        type = 2\n"
    )
    normal = fencing._parse_source(path, normal_source)
    assert fencing._reserve_protected_supplier_globals(normal, module, protected) == []
    changed = fencing._parse_source(path, normal_source + "def alter():\n    global type, zip\n    type = zip = replacement\n")
    found = fencing._reserve_protected_supplier_globals(changed, module, protected)
    declarations = [node for node in ast.walk(changed.tree) if isinstance(node, ast.Global) and node.names == ["type", "zip"]]
    assert len(declarations) == 1
    assert found == [(declarations[0], "type"), (declarations[0], "zip")]
    copied = fencing._parse_source(path, changed.source)
    copied_found = fencing._reserve_protected_supplier_globals(copied, module, protected)
    assert [(node.names, name) for node, name in copied_found] == [(node.names, name) for node, name in found]
    assert {id(node) for node, _ in found}.isdisjoint(id(node) for node, _ in copied_found)
    assert fencing._reserve_protected_supplier_globals(changed, module + "_other", protected) == []


@pytest.mark.landscape_long_run
@pytest.mark.parametrize("name", ["type", "zip"])
def test_common_proof_refuses_implicit_global_approval_supplier_replacement(name):
    units = fencing._production_units()
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    path = "src/elspeth/web/coordination/approval_authority.py"
    unit = next(unit for unit in units if unit.path == path)
    effect = "\ndef alter():\n    global " + name + "\n    " + name + " = replacement\nalter()\n"
    changed_unit = fencing._parse_source(path, unit.source + effect)
    changed = fencing._reserve_run(tuple(changed_unit if candidate is unit else candidate for candidate in units))
    expected = "protected supplier global binding escaped elspeth.web.coordination.approval_authority:" + name
    assert expected in changed["global_failures"], changed["global_failures"]


@pytest.mark.parametrize(
    "before,after",
    [
        ("if annotation is not expected:", "if False:"),
        ("if get_origin(annotation) is not Literal:", "if False:"),
        ("if type(value) is not str:", "if not isinstance(value, str):"),
        ("names.append(value)", "names.append(annotation)"),
        ("return frozenset(names)", "return annotation"),
        (
            'RunStatusResponse.model_fields["status"].annotation, SessionRunStatus',
            'RunStatusResponse.model_fields["status"].annotation, TerminalSessionRunStatus',
        ),
        (
            'RunResultsResponse.model_fields["status"].annotation, TerminalSessionRunStatus',
            'RunResultsResponse.model_fields["status"].annotation, opaque_callback(TerminalSessionRunStatus)',
        ),
        ("status: SessionRunStatus", "status: object"),
    ],
)
def test_run_status_literal_receipt_requires_checked_owner_and_actual_callers(before, after):
    units = tuple(
        unit
        for unit in fencing._production_units()
        if unit.path in {"src/elspeth/web/execution/schemas.py", "src/elspeth/web/sessions/protocol.py"}
    )
    normal = fencing._reserve_run_status_literal_metadata_receipt(units)
    assert normal["failures"] == []
    assert len(normal["arguments"]) == 4
    owner = next(unit for unit in units if unit.path == "src/elspeth/web/execution/schemas.py")
    assert owner.source.count(before) == 1
    changed_owner = fencing._parse_source(owner.path, owner.source.replace(before, after))
    changed = fencing._reserve_run_status_literal_metadata_receipt(tuple(changed_owner if unit is owner else unit for unit in units))
    assert changed["failures"]
    assert changed["origins"] == {}
    assert changed["arguments"] == set()
    assert changed["uses"] == set()


@pytest.mark.parametrize(
    "path,effect",
    [
        ("src/elspeth/web/execution/schemas.py", "get_args = foreign\n"),
        ("src/elspeth/web/execution/schemas.py", "def alter():\n    global type\n    type = foreign\nalter()\n"),
        ("src/elspeth/web/execution/schemas.py", "SessionRunStatus = foreign\n"),
        ("src/elspeth/web/sessions/protocol.py", "SessionRunStatus = foreign\n"),
        ("src/elspeth/web/sessions/protocol.py", "def alter():\n    global Literal\n    Literal = foreign\nalter()\n"),
    ],
)
def test_run_status_literal_receipt_requires_current_canonical_suppliers(path, effect):
    units = tuple(
        unit
        for unit in fencing._production_units()
        if unit.path in {"src/elspeth/web/execution/schemas.py", "src/elspeth/web/sessions/protocol.py"}
    )
    assert fencing._reserve_run_status_literal_metadata_receipt(units)["failures"] == []
    owner = next(unit for unit in units if unit.path == path)
    changed_owner = fencing._parse_source(path, owner.source + "\n" + effect)
    changed = fencing._reserve_run_status_literal_metadata_receipt(tuple(changed_owner if unit is owner else unit for unit in units))
    assert changed["failures"]
    assert changed["origins"] == {}
    assert changed["arguments"] == set()


def test_run_status_literal_receipt_preserves_raw_metadata_and_same_ast_roles():
    units = tuple(
        unit
        for unit in fencing._production_units()
        if unit.path in {"src/elspeth/web/execution/schemas.py", "src/elspeth/web/sessions/protocol.py"}
    )
    receipt = fencing._reserve_run_status_literal_metadata_receipt(units)
    assert receipt["failures"] == []
    unit = next(unit for unit in units if unit.path == "src/elspeth/web/execution/schemas.py")
    owner = fencing._reserve_node_at(unit, "_status_literal_values")
    loop = next(node for node in owner.body if isinstance(node, ast.For))
    checked = [node for node in ast.walk(loop.body[1]) if isinstance(node, ast.Name) and node.id == "value"]
    unchecked = [node for node in ast.walk(loop.body[0]) if isinstance(node, ast.Name) and node.id == "value"]
    inputs = [
        node
        for node in ast.walk(owner)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in {"annotation", "expected"}
    ]
    assert checked and unchecked and inputs
    assert all(receipt["origins"][unit.path, id(node)][0] == "supplier-scalar" for node in checked)
    assert all(receipt["origins"][unit.path, id(node)][0] == "type-metadata" for node in (*unchecked, *inputs))
    calls = [
        statement.value
        for statement in unit.tree.body
        if isinstance(statement, ast.AnnAssign)
        and isinstance(statement.target, ast.Name)
        and statement.target.id in {"RUN_STATUS_ALL_VALUES", "RUN_STATUS_TERMINAL_VALUES"}
    ]
    assert len(calls) == 2
    assert receipt["arguments"] == {(unit.path, id(call), id(argument)) for call in calls for argument in call.args}
    assert all((unit.path, id(argument)) not in receipt["origins"] for call in calls for argument in call.args)
    assert all(receipt["origins"][unit.path, id(call)][0] == "supplier-scalar" for call in calls)
    copied = tuple(fencing._parse_source(candidate.path, candidate.source) for candidate in units)
    copied_receipt = fencing._reserve_run_status_literal_metadata_receipt(copied)
    assert copied_receipt["failures"] == []
    assert set(receipt["origins"]).isdisjoint(copied_receipt["origins"])
    assert receipt["arguments"].isdisjoint(copied_receipt["arguments"])
    assert fencing._reserve_run_status_literal_metadata_receipt(())["failures"]
    assert fencing._reserve_run_status_literal_metadata_receipt((*units, unit))["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(RunStatusResponse.model_fields['status'].annotation)\n",
        "RunStatusResponse.model_fields['status'].annotation.__args__ = replacement\n",
        "setattr(RunStatusResponse.model_fields['status'].annotation, '__args__', replacement)\n",
        "held = RunStatusResponse.model_fields['status']\nopaque_callback(held)\n",
    ],
)
def test_run_status_checked_projection_does_not_grant_raw_field_metadata_transfers(effect):
    units = fencing._production_units()
    path = "src/elspeth/run_status_metadata_probe.py"
    source = (
        "from elspeth.web.execution.schemas import RunStatusResponse, RUN_STATUS_ALL_VALUES\n"
        "held = RunStatusResponse.model_fields['status']\n"
        "status_values = RUN_STATUS_ALL_VALUES\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.parametrize("selector", ["'__dict__'", "'type'", "'zip'", "name", "17"])
def test_protected_implicit_supplier_reflection_requires_exact_module_and_selector(selector):
    module = "elspeth.web.coordination.approval_authority"
    bindings = {module + ".type", module + ".zip"}
    harmless = ast.parse("'__name__'", mode="eval").body
    assert fencing._reserve_protected_supplier_reflection(module, harmless, bindings) is False
    selected = ast.parse(selector, mode="eval").body
    assert fencing._reserve_protected_supplier_reflection(module + "_other", selected, bindings) is False
    assert fencing._reserve_protected_supplier_reflection(module, selected, frozenset()) is False
    assert fencing._reserve_protected_supplier_reflection(module, selected, bindings) is True


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "getattr(owner, '__dict__')['type'] = replacement\n",
        "held = getattr(owner, '__dict__')\nheld['zip'] = replacement\n",
        "opaque_callback(getattr(owner, 'type'))\n",
        "getattr(owner, selector)['type'] = replacement\n",
    ],
)
def test_common_proof_refuses_reflected_implicit_supplier_namespace_recovery(effect):
    units = fencing._production_units()
    path = "src/elspeth/implicit_supplier_reflection_probe.py"
    source = "import elspeth.web.coordination.approval_authority as owner\nname = getattr(owner, '__name__')\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any("selected owner reflection is unproved " + path + ":" in failure for failure in changed["global_failures"]), changed[
        "global_failures"
    ]


@pytest.mark.parametrize(
    "before,after",
    [
        ("Final[MappingProxyType[str, str]]", "Final[MappingProxyType[str, loads]]"),
        ("Final[MappingProxyType[str, str]]", "Final[MappingProxyType[str, opaque_callback()]]"),
        ("Final[MappingProxyType[str, str]]", "Final[foreign[str, str]]"),
        ("from types import MappingProxyType", "from foreign import MappingProxyType"),
        ("from typing import Final", "from foreign import Final"),
    ],
)
def test_configuration_eager_annotation_receipt_requires_finite_type_grammar(before, after):
    paths = {"src/elspeth/contracts/config/alignment.py", "src/elspeth/contracts/config/defaults.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    normal = fencing._reserve_configuration_annotation_metadata_receipt(units)
    assert normal["failures"] == []
    assert normal["uses"]
    owner = next(unit for unit in units if unit.path.endswith("/alignment.py"))
    assert before in owner.source
    changed_owner = fencing._parse_source(owner.path, owner.source.replace(before, after))
    changed = fencing._reserve_configuration_annotation_metadata_receipt(tuple(changed_owner if unit is owner else unit for unit in units))
    assert changed["failures"]
    assert changed["uses"] == set()


@pytest.mark.parametrize("name", ["MappingProxyType", "Final", "str"])
def test_configuration_eager_annotation_receipt_rejects_nested_supplier_globals(name):
    paths = {"src/elspeth/contracts/config/alignment.py", "src/elspeth/contracts/config/defaults.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    assert fencing._reserve_configuration_annotation_metadata_receipt(units)["failures"] == []
    owner = next(unit for unit in units if unit.path.endswith("/defaults.py"))
    effect = "\ndef alter():\n    global " + name + "\n    " + name + " = replacement\nalter()\n"
    changed_owner = fencing._parse_source(owner.path, owner.source + effect)
    changed = fencing._reserve_configuration_annotation_metadata_receipt(tuple(changed_owner if unit is owner else unit for unit in units))
    assert changed["failures"]
    assert changed["uses"] == set()


def test_configuration_eager_annotation_receipt_grants_only_same_ast_annotation_loads():
    paths = {"src/elspeth/contracts/config/alignment.py", "src/elspeth/contracts/config/defaults.py"}
    names = {"FIELD_MAPPINGS", "SETTINGS_TO_RUNTIME", "RUNTIME_TO_SUBSYSTEM", "INTERNAL_DEFAULTS", "POLICY_DEFAULTS"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    normal = fencing._reserve_configuration_annotation_metadata_receipt(units)
    assert normal["failures"] == []
    expected = set()
    value_loads = set()
    for unit in units:
        for statement in unit.tree.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.target.id in names:
                expected.update(
                    (unit.path, id(node))
                    for node in ast.walk(statement.annotation)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                )
                value_loads.update(
                    (unit.path, id(node))
                    for node in ast.walk(statement.value)
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                )
    assert expected and value_loads
    assert normal["uses"] == expected
    assert normal["uses"].isdisjoint(value_loads)
    copied = tuple(fencing._parse_source(unit.path, unit.source) for unit in units)
    detached = fencing._reserve_configuration_annotation_metadata_receipt(copied)
    assert detached["failures"] == []
    assert normal["uses"].isdisjoint(detached["uses"])
    assert fencing._reserve_configuration_annotation_metadata_receipt(())["failures"]
    assert fencing._reserve_configuration_annotation_metadata_receipt((*units, units[0]))["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(owner.__annotations__['FIELD_MAPPINGS'])\n",
        "get_args(owner.__annotations__['FIELD_MAPPINGS'])[0].__origin__.__getitem__ = replacement\n",
        "held = owner.__annotations__['FIELD_MAPPINGS']\nsetattr(held, '__args__', replacement)\n",
        "import typing\ntyping.Final._getitem = replacement\n",
    ],
)
def test_configuration_eager_annotations_retain_metadata_recovery_and_supplier_denials(effect):
    units = fencing._production_units()
    path = "src/elspeth/configuration_annotation_recovery_probe.py"
    source = (
        "import elspeth.contracts.config.alignment as owner\nfrom typing import get_args\nheld = owner.__annotations__['FIELD_MAPPINGS']\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.parametrize(
    "effect",
    [
        "typing._SpecialForm.__getitem__ = replacement\n",
        "from typing import _SpecialForm as Form\nForm.__getitem__ = replacement\n",
        "Form = typing._SpecialForm\nForm.__getitem__ = replacement\n",
        "type(Final).__getitem__ = replacement\n",
        "getattr(typing, '_SpecialForm').__getitem__ = replacement\n",
        "getattr(typing, '__dict__')['_SpecialForm'] = replacement\n",
        "space = vars(typing)\nspace['_SpecialForm'] = replacement\n",
        "setattr(typing._SpecialForm, '__getitem__', replacement)\n",
        "delattr(typing._SpecialForm, '__getitem__')\n",
        "typing._caches.clear()\n",
        "opaque_callback(typing._SpecialForm)\n",
        "def alter():\n    from typing import _SpecialForm as Form\n    Form.__getitem__ = replacement\n",
    ],
)
def test_typing_supplier_denial_closes_actual_namespace_and_class_alias_effects(effect):
    path = "src/elspeth/typing_supplier_mutation_probe.py"
    source = (
        "import typing\nfrom typing import Final\nfrom types import MappingProxyType\n"
        "value: Final[MappingProxyType[str, str]] = MappingProxyType({'x': 'y'})\n"
        "form = typing._SpecialForm\n"
    )
    normal = fencing._parse_source(path, source)
    assert fencing._reserve_typing_supplier_mutation_failures((normal,)) == []
    changed = fencing._parse_source(path, source + effect)
    failures = fencing._reserve_typing_supplier_mutation_failures((changed,))
    assert failures
    assert all(path in failure and failure.startswith("typing supplier ") for failure in failures)


def test_typing_supplier_denial_does_not_classify_ordinary_results_or_foreign_locals_as_suppliers():
    path = "src/elspeth/typing_supplier_plain_data_probe.py"
    source = (
        "import typing\n"
        "data = typing.cast(dict, {'x': 1})\n"
        "data['x'] = 2\n"
        "def local(typing):\n    typing.value = 1\n"
        "def separate():\n    import foreign as typing\n    typing.value = 1\n"
        "read = typing.get_args(list[str])\n"
    )
    unit = fencing._parse_source(path, source)
    assert fencing._reserve_typing_supplier_mutation_failures((unit,)) == []
    changed = fencing._parse_source(path, source + "typing._SpecialForm.__getitem__ = replacement\n")
    failures = fencing._reserve_typing_supplier_mutation_failures((changed,))
    assert len(failures) == 1
    assert failures[0].startswith("typing supplier namespace mutation " + path + ":")


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "typing._SpecialForm.__getitem__ = replacement\n",
        "from typing import _SpecialForm as Form\nForm.__getitem__ = replacement\n",
        "type(typing.Final).__getitem__ = replacement\n",
        "setattr(typing._SpecialForm, '__getitem__', replacement)\n",
    ],
)
def test_original_proof_retains_typing_supplier_namespace_mutation_denial(effect):
    units = fencing._production_units()
    path = "src/elspeth/typing_supplier_complete_probe.py"
    source = "import typing\nform = typing._SpecialForm\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(failure.startswith("typing supplier ") and path in failure for failure in changed["global_failures"]), changed[
        "global_failures"
    ]


@pytest.mark.parametrize(
    "effect",
    [
        "Form = typing.cast(object, typing._SpecialForm)\nForm.__getitem__ = replacement\n",
        "Form = typing.get_args(tuple[typing._SpecialForm])[0]\nForm.__getitem__ = replacement\n",
        "typing.NamedTuple('Held', [('form', typing._SpecialForm)])\n",
    ],
)
def test_typing_supplier_identity_queries_do_not_launder_retained_class_or_factory_transfer(effect):
    path = "src/elspeth/typing_supplier_identity_probe.py"
    source = "import typing\ndata = typing.cast(dict, {'x': 1})\ndata['x'] = 2\nplain = typing.TypeVar('Plain')\n"
    normal = fencing._parse_source(path, source)
    assert fencing._reserve_typing_supplier_mutation_failures((normal,)) == []
    changed = fencing._parse_source(path, source + effect)
    failures = fencing._reserve_typing_supplier_mutation_failures((changed,))
    assert failures
    assert all(path in failure and failure.startswith("typing supplier ") for failure in failures)


@pytest.mark.parametrize("carrier", ["[typing._SpecialForm]", "{'form': typing._SpecialForm}", "('form', typing._SpecialForm)"])
def test_typing_supplier_literal_carriers_keep_their_actual_class_dependency(carrier):
    path = "src/elspeth/typing_supplier_literal_carrier_probe.py"
    source = "import typing\nplain = ['safe', 1]\nopaque_callback(plain)\n"
    assert fencing._reserve_typing_supplier_mutation_failures((fencing._parse_source(path, source),)) == []
    changed = fencing._parse_source(path, source + "held = " + carrier + "\nopaque_callback(held)\n")
    failures = fencing._reserve_typing_supplier_mutation_failures((changed,))
    assert any("typing supplier namespace transfer is unclosed " + path + ":" in failure for failure in failures)


@pytest.mark.parametrize(
    "before,after",
    [
        ("class PluginNotFoundError(ValueError):", "class PluginNotFoundError(Foreign):"),
        ("class PluginNotFoundError(ValueError):", "@foreign_decorator\nclass PluginNotFoundError(ValueError):"),
        ("class PluginNotFoundError(ValueError):", "class PluginNotFoundError(ValueError, metaclass=Foreign):"),
        (
            '    """Raised when a plugin name is not found in the registry."""',
            '    """Raised when a plugin name is not found in the registry."""\n    def __new__(cls):\n        return foreign()',
        ),
    ],
)
def test_plugin_lookup_exception_receipt_requires_actual_plain_builtin_subclass(before, after):
    path = "src/elspeth/plugins/infrastructure/manager.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    normal = fencing._reserve_plugin_lookup_exception_receipt((unit,))
    assert normal["failures"] == []
    assert normal["exception"] == "elspeth.plugins.infrastructure.manager.PluginNotFoundError"
    assert unit.source.count(before) == 1
    changed = fencing._parse_source(path, unit.source.replace(before, after))
    refused = fencing._reserve_plugin_lookup_exception_receipt((changed,))
    assert refused["failures"]
    assert refused["exception"] is None


@pytest.mark.parametrize(
    "effect",
    [
        "ValueError = foreign\n",
        "PluginNotFoundError = foreign\n",
        "def alter():\n    global ValueError\n    ValueError = foreign\nalter()\n",
    ],
)
def test_plugin_lookup_exception_receipt_retains_supplier_binding_refusals(effect):
    path = "src/elspeth/plugins/infrastructure/manager.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    assert fencing._reserve_plugin_lookup_exception_receipt((unit,))["failures"] == []
    changed = fencing._parse_source(path, unit.source + "\n" + effect)
    refused = fencing._reserve_plugin_lookup_exception_receipt((changed,))
    assert refused["failures"]
    assert refused["exception"] is None


def test_plugin_lookup_exception_role_is_only_actual_direct_except_operand():
    path = "src/elspeth/plugins/infrastructure/manager.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    receipt = fencing._reserve_plugin_lookup_exception_receipt((unit,))
    assert receipt["failures"] == []
    caller = fencing._parse_source(
        "src/elspeth/plugin_lookup_except_probe.py",
        (
            "try:\n    lookup()\nexcept PluginNotFoundError:\n    pass\n"
            "opaque_callback(PluginNotFoundError)\n"
            "try:\n    lookup()\nexcept (PluginNotFoundError, ValueError):\n    pass\n"
        ),
    )
    parents = {id(child): parent for parent in ast.walk(caller.tree) for child in ast.iter_child_nodes(parent)}
    names = [node for node in ast.walk(caller.tree) if isinstance(node, ast.Name) and node.id == "PluginNotFoundError"]
    assert len(names) == 3
    direct = next(node for node in names if isinstance(parents[id(node)], ast.ExceptHandler))
    origin = ("object", receipt["exception"])
    assert fencing._reserve_plugin_lookup_exception_role(direct, origin, parents, receipt)
    assert not fencing._reserve_plugin_lookup_exception_role(direct, ("object", "foreign.Error"), parents, receipt)
    assert all(not fencing._reserve_plugin_lookup_exception_role(node, origin, parents, receipt) for node in names if node is not direct)
    detached = fencing._parse_source(caller.path, caller.source)
    other_direct = next(statement.handlers[0].type for statement in detached.tree.body if isinstance(statement, ast.Try))
    assert not fencing._reserve_plugin_lookup_exception_role(other_direct, origin, parents, receipt)
    assert fencing._reserve_plugin_lookup_exception_receipt(())["failures"]
    assert fencing._reserve_plugin_lookup_exception_receipt((unit, unit))["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "PluginNotFoundError.__bases__ = (Foreign,)\n",
        "setattr(PluginNotFoundError, '__new__', replacement)\n",
        "opaque_callback(PluginNotFoundError)\n",
    ],
)
def test_plugin_lookup_exception_match_does_not_grant_mutation_or_callback_transfer(effect):
    units = fencing._production_units()
    path = "src/elspeth/plugin_lookup_exception_effect_probe.py"
    source = (
        "from elspeth.plugins.infrastructure.manager import PluginNotFoundError\n"
        "try:\n    lookup()\nexcept PluginNotFoundError:\n    pass\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.parametrize(
    "before,after",
    [
        ("if origin is typing.Union or origin is types.UnionType:", "if origin in (typing.Union, types.UnionType):"),
        ("if not any(member is known for known in members):", "if member not in members:"),
        ("members.append(member)", "members.append(opaque_callback(member))"),
        ("if any(scalar is bool for scalar in scalar_types):", "if bool in scalar_types:"),
        ("return tuple(members)", "return set(members)"),
        ("return float(value)", "return annotation"),
        (
            "_coerce_env_scalar(value, WebSettings.model_fields[field_name].annotation)",
            "opaque_callback(value, WebSettings.model_fields[field_name].annotation)",
        ),
        ("for key, value in os.environ.items():", "for key, value in foreign.items():"),
    ],
)
def test_environment_annotation_receipt_requires_complete_identity_owners_and_current_caller(before, after):
    path = "src/elspeth/web/config.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    normal = fencing._reserve_environment_annotation_consumer_receipt((unit,))
    assert normal["failures"] == []
    assert normal["normalizer"] == "elspeth.web.config._annotation_scalar_types"
    assert normal["arguments"] and normal["returns"]
    assert unit.source.count(before) == 1
    changed = fencing._parse_source(path, unit.source.replace(before, after))
    refused = fencing._reserve_environment_annotation_consumer_receipt((changed,))
    assert refused["failures"]
    assert refused["normalizer"] is None
    assert refused["origins"] == {}
    assert refused["arguments"] == set()
    assert refused["returns"] == set()


@pytest.mark.parametrize(
    "effect",
    [
        "typing = foreign\n",
        "tuple = foreign\n",
        "def alter():\n    global any\n    any = foreign\nalter()\n",
        "_annotation_scalar_types = foreign\n",
    ],
)
def test_environment_annotation_receipt_retains_canonical_supplier_bindings(effect):
    path = "src/elspeth/web/config.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    assert fencing._reserve_environment_annotation_consumer_receipt((unit,))["failures"] == []
    changed = fencing._parse_source(path, unit.source + "\n" + effect)
    refused = fencing._reserve_environment_annotation_consumer_receipt((changed,))
    assert refused["failures"]
    assert refused["origins"] == {}
    assert refused["arguments"] == set()


def test_environment_annotation_receipt_retains_types_and_limits_transfer_to_exact_argument_nodes():
    path = "src/elspeth/web/config.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    receipt = fencing._reserve_environment_annotation_consumer_receipt((unit,))
    assert receipt["failures"] == []
    normalizer = fencing._reserve_node_at(unit, "_annotation_scalar_types")
    returns = [node for node in ast.walk(normalizer) if isinstance(node, ast.Return)]
    assert returns
    assert receipt["returns"] == {(path, id(node)) for node in returns}
    assert all(receipt["origins"][path, id(node.value)][0] == "type-metadata" for node in returns)
    assert not any(origin[0] == "supplier-scalar" for origin in receipt["origins"].values())
    caller = fencing._reserve_node_at(unit, "settings_from_env")
    call = next(
        node
        for node in ast.walk(caller)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_coerce_env_scalar"
    )
    assert (path, id(call), id(call.args[1])) in receipt["arguments"]
    assert (path, id(call), id(call.args[0])) not in receipt["arguments"]
    assert (path, id(call)) not in receipt["origins"]
    assert all(
        (path, id(node)) not in receipt["origins"]
        for node in ast.walk(caller)
        if isinstance(node, ast.Attribute) and node.attr == "model_fields"
    )
    copied = fencing._parse_source(path, unit.source)
    other = fencing._reserve_environment_annotation_consumer_receipt((copied,))
    assert other["failures"] == []
    assert set(receipt["origins"]).isdisjoint(other["origins"])
    assert receipt["arguments"].isdisjoint(other["arguments"])
    assert fencing._reserve_environment_annotation_consumer_receipt(())["failures"]
    assert fencing._reserve_environment_annotation_consumer_receipt((unit, unit))["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(types[0])\n",
        "types[0].__new__ = replacement\n",
        "setattr(types[0], '__new__', replacement)\n",
    ],
)
def test_environment_annotation_normalizer_does_not_launder_retained_classes(effect):
    units = fencing._production_units()
    path = "src/elspeth/environment_annotation_recovery_probe.py"
    source = "from elspeth.web.config import _annotation_scalar_types\ntypes = _annotation_scalar_types(int | None)\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.parametrize(
    "before,after",
    [
        ("if annotation is not RunEventType or get_origin(annotation) is not Literal:", "if False:"),
        ("if type(mapping) is not dict:", "if not isinstance(mapping, dict):"),
        ("if type(value) is not str:", "if not isinstance(value, str):"),
        ("if type(key) is not str:", "if not isinstance(key, str):"),
        ("mapping_keys.add(key)", "mapping_keys.add(opaque_callback(key))"),
        ("literal_values.add(value)", "literal_values.add(opaque_callback(value))"),
        ("if mapping_keys != literal_values:", "if False:"),
        (
            '_require_run_event_mapping(RunEvent.model_fields["event_type"].annotation, RunEvent._EVENT_TYPE_TO_DATA_TYPE)',
            '_require_run_event_mapping(RunEvent.model_fields["event_type"].annotation, foreign)',
        ),
    ],
)
def test_run_event_receipt_requires_checked_body_and_actual_mapping_call(before, after):
    path = "src/elspeth/web/execution/schemas.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    assert fencing._reserve_run_event_mapping_receipt((unit,))["failures"] == []
    # The value-str predicate also occurs in the separately checked status
    # helper. This test mutates only the complete event-owner source segment.
    owner = fencing._reserve_node_at(unit, "_require_run_event_mapping")
    segment = ast.get_source_segment(unit.source, owner)
    if before in segment:
        assert segment.count(before) == 1
        changed_source = unit.source.replace(segment, segment.replace(before, after))
    else:
        assert unit.source.count(before) == 1
        changed_source = unit.source.replace(before, after)
    refused = fencing._reserve_run_event_mapping_receipt((fencing._parse_source(path, changed_source),))
    assert refused["failures"]
    assert refused["origins"] == {} and refused["arguments"] == set() and refused["uses"] == set()


@pytest.mark.parametrize(
    "effect",
    [
        "RunEventType = foreign\n",
        "get_args = foreign\n",
        "set = foreign\n",
        "def alter():\n    global type\n    type = foreign\nalter()\n",
    ],
)
def test_run_event_receipt_preserves_actual_supplier_bindings(effect):
    path = "src/elspeth/web/execution/schemas.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    assert fencing._reserve_run_event_mapping_receipt((unit,))["failures"] == []
    refused = fencing._reserve_run_event_mapping_receipt((fencing._parse_source(path, unit.source + "\n" + effect),))
    assert refused["failures"]
    assert refused["origins"] == {} and refused["arguments"] == set()


def test_run_event_receipt_keeps_inputs_and_precheck_keys_as_metadata():
    path = "src/elspeth/web/execution/schemas.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    receipt = fencing._reserve_run_event_mapping_receipt((unit,))
    assert receipt["failures"] == []
    owner = fencing._reserve_node_at(unit, "_require_run_event_mapping")
    loops = [node for node in owner.body if isinstance(node, ast.For)]
    assert len(loops) == 2
    for loop in loops:
        names = [
            node
            for node in ast.walk(loop.body[0])
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == loop.target.id
        ]
        assert names and all(receipt["origins"][path, id(node)][0] == "type-metadata" for node in names)
        checked = [
            node
            for node in ast.walk(loop.body[1])
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == loop.target.id
        ]
        assert checked and all(receipt["origins"][path, id(node)][0] == "supplier-scalar" for node in checked)
    call = next(
        node.value
        for node in unit.tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_require_run_event_mapping"
    )
    assert receipt["arguments"] == {(path, id(call), id(argument)) for argument in call.args}
    assert all(receipt["origins"][path, id(argument)][0] == "type-metadata" for argument in call.args)
    assert (path, id(call)) not in receipt["origins"]
    assert not any(
        (path, id(node)) in receipt["origins"] for node in ast.walk(call) if isinstance(node, ast.Attribute) and node.attr == "model_fields"
    )
    copied = fencing._parse_source(path, unit.source)
    detached = fencing._reserve_run_event_mapping_receipt((copied,))
    assert detached["failures"] == []
    assert receipt["arguments"].isdisjoint(detached["arguments"])
    assert set(receipt["origins"]).isdisjoint(detached["origins"])
    assert fencing._reserve_run_event_mapping_receipt(())["failures"]
    assert fencing._reserve_run_event_mapping_receipt((unit, unit))["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(annotation)\n",
        "annotation.__args__[0].__class__.__new__ = replacement\n",
        "RunEvent.model_fields['event_type'].annotation = str\n",
    ],
)
def test_run_event_checked_consumer_does_not_release_original_field_metadata(effect):
    units = fencing._production_units()
    path = "src/elspeth/run_event_metadata_probe.py"
    source = "from elspeth.web.execution.schemas import RunEvent\nannotation = RunEvent.model_fields['event_type'].annotation\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.parametrize(
    "path,before,after",
    [
        (
            "src/elspeth/contracts/composer_mutation.py",
            "ceiling: ComposerMutationCeiling",
            "ceiling: opaque_callback(ComposerMutationCeiling)",
        ),
        (
            "src/elspeth/contracts/composer_mutation.py",
            "class ComposerMutationRevocation:",
            "class ComposerMutationRevocation(metaclass=foreign):",
        ),
        (
            "src/elspeth/contracts/composer_mutation.py",
            "def __init__(self, revocation: ComposerMutationRevocation)",
            "def __init__(self, revocation: opaque_callback(ComposerMutationRevocation))",
        ),
        (
            "src/elspeth/web/coordination/composer_mutation_guard.py",
            ") -> ComposerMutationRevocation | None:",
            ") -> opaque_callback(ComposerMutationRevocation):",
        ),
        (
            "src/elspeth/web/coordination/composer_mutation_guard.py",
            "def require_auto_mutation_on_connection(",
            "@foreign\ndef require_auto_mutation_on_connection(",
        ),
    ],
)
def test_mutation_annotation_roles_require_exact_current_owners(path, before, after):
    units = tuple(
        unit
        for unit in fencing._production_units()
        if unit.path
        in {
            "src/elspeth/contracts/composer_mutation.py",
            "src/elspeth/web/coordination/composer_mutation_guard.py",
        }
    )
    normal = fencing._reserve_mutation_annotation_roles(units)
    assert normal["failures"] == [] and len(normal["uses"]) == 5
    unit = next(unit for unit in units if unit.path == path)
    assert unit.source.count(before) == 1
    changed = fencing._parse_source(path, unit.source.replace(before, after))
    refused = fencing._reserve_mutation_annotation_roles(tuple(changed if current.path == path else current for current in units))
    assert refused["failures"]
    assert refused["uses"] == set()


def test_mutation_annotation_roles_have_only_five_same_ast_retained_uses():
    units = tuple(
        unit
        for unit in fencing._production_units()
        if unit.path
        in {
            "src/elspeth/contracts/composer_mutation.py",
            "src/elspeth/web/coordination/composer_mutation_guard.py",
        }
    )
    receipt = fencing._reserve_mutation_annotation_roles(units)
    assert receipt["failures"] == [] and len(receipt["uses"]) == 5
    nodes = {(unit.path, id(node)): node for unit in units for node in ast.walk(unit.tree)}
    assert all(
        isinstance(nodes[key], ast.Name)
        and nodes[key].id
        in {
            "ComposerMutationCeiling",
            "ComposerMutationRevocation",
        }
        for key in receipt["uses"]
    )
    assert set(receipt) == {"failures", "uses", "roots", "bindings"}
    copied = tuple(fencing._parse_source(unit.path, unit.source) for unit in units)
    other = fencing._reserve_mutation_annotation_roles(copied)
    assert other["failures"] == [] and receipt["uses"].isdisjoint(other["uses"])
    assert fencing._reserve_mutation_annotation_roles(units[:1])["failures"]
    assert fencing._reserve_mutation_annotation_roles((*units, units[0]))["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(ComposerMutationRevocation.__annotations__['ceiling'])\n",
        "ComposerMutationRevocation.__annotations__['ceiling'].__new__ = replacement\n",
        "setattr(ComposerMutationRevocation.__annotations__['ceiling'], '__new__', replacement)\n",
    ],
)
def test_mutation_annotation_role_preserves_recovered_class_denials(effect):
    units = fencing._production_units()
    path = "src/elspeth/mutation_annotation_recovery_probe.py"
    source = "from elspeth.contracts.composer_mutation import ComposerMutationRevocation\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.parametrize(
    "before,after",
    [
        ("type(self.trust) is not PromptGuardTrust", "False"),
        ("type(self.text) is not str", "False"),
        ("type(self.inspection_text) is not str", "False"),
        ("class PromptGuardContentPart:", "class PromptGuardContentPart(metaclass=foreign):"),
        ('REFERENCE = "operator_reference"', 'REFERENCE = opaque_callback("operator_reference")'),
    ],
)
def test_prompt_part_enum_receipt_requires_complete_constructor_and_trust_owner(before, after):
    path = "src/elspeth/contracts/prompt_guard.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    assert fencing._reserve_prompt_part_enum_record_receipt((unit,))["failures"] == []
    assert unit.source.count(before) == 1
    refused = fencing._reserve_prompt_part_enum_record_receipt((fencing._parse_source(path, unit.source.replace(before, after)),))
    assert refused["failures"] and refused["record"] is None and refused["fields"] == {}


@pytest.mark.parametrize(
    "effect",
    [
        "field = foreign\n",
        "dataclass = foreign\n",
        "Enum = foreign\n",
        "def alter():\n    global type\n    type = foreign\nalter()\n",
    ],
)
def test_prompt_part_enum_receipt_keeps_canonical_constructor_bindings(effect):
    path = "src/elspeth/contracts/prompt_guard.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    assert fencing._reserve_prompt_part_enum_record_receipt((unit,))["failures"] == []
    refused = fencing._reserve_prompt_part_enum_record_receipt((fencing._parse_source(path, unit.source + "\n" + effect),))
    assert refused["failures"] and refused["fields"] == {}


def test_prompt_part_enum_receipt_keeps_trust_as_live_enum_metadata():
    path = "src/elspeth/contracts/prompt_guard.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    normal = fencing._reserve_prompt_part_enum_record_receipt((unit,))
    assert normal["failures"] == []
    assert normal["record"] == "elspeth.contracts.prompt_guard.PromptGuardContentPart"
    assert normal["fields"]["trust"] == ("enum-value", "elspeth.contracts.prompt_guard.PromptGuardTrust")
    assert set(normal["fields"]) == {"text", "inspection_text", "trust"}
    assert normal["fields"]["text"][0] == normal["fields"]["inspection_text"][0] == "supplier-scalar"
    assert fencing._reserve_prompt_part_enum_record_receipt(())["failures"]
    assert fencing._reserve_prompt_part_enum_record_receipt((unit, unit))["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "part.trust._value_ = 'changed'\n",
        "setattr(part.trust, '_value_', 'changed')\n",
        "type(part.trust).__new__ = replacement\n",
        "opaque_callback(part)\n",
    ],
)
def test_prompt_part_enum_record_retains_singleton_and_record_escape_denials(effect):
    units = fencing._production_units()
    path = "src/elspeth/prompt_part_retention_probe.py"
    source = (
        "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\n"
        "part = PromptGuardContentPart('reference', PromptGuardTrust.REFERENCE)\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.parametrize(
    "before,after",
    [
        ("if type(self.capability) is not PluginCapability:", "if False:"),
        ("if type(self.blocks_positive_detection) is not bool:", "if False:"),
    ],
)
def test_capability_scalar_field_requires_its_actual_primitive_checks(before, after):
    path = "src/elspeth/contracts/plugin_capabilities.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    assert fencing._reserve_capability_primitive_dependencies((unit,))["failures"] == []
    assert unit.source.count(before) == 1
    changed = fencing._parse_source(path, unit.source.replace(before, after))
    assert fencing._reserve_capability_primitive_dependencies((changed,))["failures"]


@pytest.mark.parametrize(
    "effect",
    [
        "type = foreign\n",
        "bool = foreign\n",
        "TypeError = foreign\n",
        "def alter():\n    global type\n    type = foreign\nalter()\n",
    ],
)
def test_capability_scalar_check_keeps_implicit_primitive_bindings(effect):
    path = "src/elspeth/contracts/plugin_capabilities.py"
    unit = next(unit for unit in fencing._production_units() if unit.path == path)
    assert fencing._reserve_capability_primitive_dependencies((unit,))["failures"] == []
    changed = fencing._parse_source(path, unit.source + "\n" + effect)
    assert fencing._reserve_capability_primitive_dependencies((changed,))["failures"]
    assert fencing._reserve_capability_primitive_dependencies(())["failures"]
    assert fencing._reserve_capability_primitive_dependencies((unit, unit))["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "producer,consumer",
    [
        ("def held():\n    return PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)\n", "part = held()\nopaque_callback(part)\n"),
        (
            "def held():\n    part = PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)\n    return part\n",
            "part = held()\npart.trust._value_ = 'changed'\n",
        ),
        (
            "def held():\n    yield PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)\n",
            "part = next(held())\nsetattr(part.trust, '_value_', 'changed')\n",
        ),
        (
            "def held():\n    yield from (PromptGuardContentPart('x', PromptGuardTrust.REFERENCE),)\n",
            "part = next(held())\ntype(part.trust).__new__ = replacement\n",
        ),
        ("def held():\n    return PromptGuardTrust.REFERENCE\n", "marker = held()\nmarker._value_ = 'changed'\n"),
    ],
)
def test_unknown_enum_producer_must_not_erase_retained_origin(producer, consumer):
    units = fencing._production_units()
    path = "src/elspeth/enum_producer_escape_probe.py"
    imports = "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, imports)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, imports + producer + consumer)))
    assert any(failure.startswith("retained enum producer return is unclosed " + path) for failure in changed["global_failures"]), changed[
        "global_failures"
    ]


@pytest.mark.parametrize(
    "before,after",
    [
        ("parts = tuple(render(value, (), 0))", "parts = opaque_callback(render(value, (), 0))"),
        ("yield PromptGuardContentPart(encoded, PromptGuardTrust.REFERENCE)", "yield opaque_callback(PromptGuardTrust.REFERENCE)"),
        ("return parts", "return foreign"),
        ("yield from render(child, (*path, key), depth + 1)", "yield from foreign(child, (*path, key), depth + 1)"),
    ],
)
def test_prompt_renderer_retaining_result_requires_the_complete_original_producer(before, after):
    paths = {"src/elspeth/web/composer/prompt_guard_rendering.py", "src/elspeth/contracts/prompt_guard.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    normal = fencing._reserve_prompt_renderer_return_receipt(units)
    assert normal["failures"] == [] and normal["returns"] and normal["origins"]
    unit = next(unit for unit in units if unit.path.endswith("prompt_guard_rendering.py"))
    owner = fencing._reserve_node_at(unit, "json_content_parts")
    segment = ast.get_source_segment(unit.source, owner)
    assert segment.count(before) == 1
    changed = fencing._parse_source(unit.path, unit.source.replace(segment, segment.replace(before, after)))
    refused = fencing._reserve_prompt_renderer_return_receipt(tuple(changed if current.path == unit.path else current for current in units))
    assert refused["failures"]
    assert refused["owner"] is None and refused["origins"] == {} and refused["returns"] == set()


def test_prompt_renderer_receipt_retains_generator_tuple_and_nominal_partition_members():
    paths = {"src/elspeth/web/composer/prompt_guard_rendering.py", "src/elspeth/contracts/prompt_guard.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    receipt = fencing._reserve_prompt_renderer_return_receipt(units)
    assert receipt["failures"] == []
    assert receipt["owner"] == "elspeth.web.composer.prompt_guard_rendering.json_content_parts"
    assert receipt["origins"] and receipt["returns"]
    assert {origin[0] for origin in receipt["origins"].values()} == {"enum-record", "enum-record-sequence"}
    copied = tuple(fencing._parse_source(unit.path, unit.source) for unit in units)
    other = fencing._reserve_prompt_renderer_return_receipt(copied)
    assert other["failures"] == []
    assert set(receipt["origins"]).isdisjoint(other["origins"])
    assert receipt["returns"].isdisjoint(other["returns"])
    assert fencing._reserve_prompt_renderer_return_receipt(())["failures"]
    assert fencing._reserve_prompt_renderer_return_receipt((*units, units[0]))["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "parts[0].trust._value_ = 'changed'\n",
        "setattr(parts[0].trust, '_value_', 'changed')\n",
        "type(parts[0].trust).__new__ = replacement\n",
        "opaque_callback(parts)\n",
    ],
)
def test_actual_prompt_renderer_result_never_erases_its_retained_enum_records(effect):
    units = fencing._production_units()
    path = "src/elspeth/prompt_renderer_result_probe.py"
    source = (
        "from elspeth.web.composer.prompt_guard_rendering import json_content_parts\n"
        "parts = json_content_parts('\"reference\"', reference_paths=frozenset({()}))\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.parametrize(
    "before,after",
    [
        ("if type(value) is not CapabilityDeclaration:", "if False:"),
        ("type(value.blocks_positive_detection) is not bool", "False"),
        ("return CapabilityDeclaration(value.capability, value.control_role, value.blocks_positive_detection)", "return foreign"),
        (
            "return CapabilityDeclaration(value.capability, value.control_role, value.blocks_positive_detection)",
            "return opaque_callback(value)",
        ),
        ('return FrameworkBugError("get_plugin_schema producer returned malformed data")', "return opaque_callback()"),
    ],
)
def test_schema_capability_producer_requires_actual_complete_admission_and_return(before, after):
    paths = {"src/elspeth/web/composer/tools/_generation_schema_response.py", "src/elspeth/contracts/plugin_capabilities.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    receipt = fencing._reserve_schema_capability_producer_receipt(units)
    assert receipt["failures"] == [] and receipt["origins"] and receipt["returns"]
    unit = next(unit for unit in units if unit.path.endswith("_generation_schema_response.py"))
    assert unit.source.count(before) == 1
    changed = fencing._parse_source(unit.path, unit.source.replace(before, after))
    refused = fencing._reserve_schema_capability_producer_receipt(
        tuple(changed if current.path == unit.path else current for current in units)
    )
    assert refused["failures"] and refused["owner"] is None
    assert refused["origins"] == {} and refused["returns"] == set()


@pytest.mark.parametrize(
    "effect",
    [
        "CapabilityDeclaration = foreign\n",
        "FrameworkBugError = foreign\n",
        "type = foreign\n",
        "def alter():\n    global bool\n    bool = foreign\nalter()\n",
    ],
)
def test_schema_capability_producer_requires_canonical_constructor_and_primitive_bindings(effect):
    paths = {"src/elspeth/web/composer/tools/_generation_schema_response.py", "src/elspeth/contracts/plugin_capabilities.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    assert fencing._reserve_schema_capability_producer_receipt(units)["failures"] == []
    unit = next(unit for unit in units if unit.path.endswith("_generation_schema_response.py"))
    changed = fencing._parse_source(unit.path, unit.source + "\n" + effect)
    refused = fencing._reserve_schema_capability_producer_receipt(
        tuple(changed if current.path == unit.path else current for current in units)
    )
    assert refused["failures"] and refused["owner"] is None


def test_schema_capability_producer_retains_only_checked_same_ast_value_loads_and_return():
    paths = {"src/elspeth/web/composer/tools/_generation_schema_response.py", "src/elspeth/contracts/plugin_capabilities.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    receipt = fencing._reserve_schema_capability_producer_receipt(units)
    assert receipt["failures"] == []
    assert receipt["owner"] == "elspeth.web.composer.tools._generation_schema_response._capability"
    assert set(receipt["origins"].values()) == {("enum-record", "elspeth.contracts.plugin_capabilities.CapabilityDeclaration")}
    assert len(receipt["returns"]) == 1
    unit = next(unit for unit in units if unit.path.endswith("_generation_schema_response.py"))
    owner = fencing._reserve_node_at(unit, "_capability")
    initial = {id(node) for node in ast.walk(owner.body[0])}
    assert all(node_id not in initial for path, node_id in receipt["origins"] if path == unit.path)
    copied = tuple(fencing._parse_source(unit.path, unit.source) for unit in units)
    other = fencing._reserve_schema_capability_producer_receipt(copied)
    assert other["failures"] == []
    assert set(receipt["origins"]).isdisjoint(other["origins"])
    assert receipt["returns"].isdisjoint(other["returns"])
    assert fencing._reserve_schema_capability_producer_receipt(())["failures"]
    assert fencing._reserve_schema_capability_producer_receipt((*units, unit))["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "capability.capability._value_ = 'changed'\n",
        "setattr(capability.capability, '_value_', 'changed')\n",
        "type(capability.capability).__new__ = replacement\n",
        "opaque_callback(capability)\n",
        "opaque_callback(_capability)\n",
    ],
)
def test_schema_capability_actual_result_keeps_enum_and_callable_escape_denials(effect):
    units = fencing._production_units()
    path = "src/elspeth/schema_capability_result_probe.py"
    source = (
        "from elspeth.web.composer.tools._generation_schema_response import _capability\n"
        "from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, PluginCapability\n"
        "capability = _capability(CapabilityDeclaration(PluginCapability.LLM))\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.parametrize(
    "before,after",
    [
        ("return json_content_parts(serialized, ensure_ascii=ensure_ascii, separators=separators)", "return opaque_callback(serialized)"),
        ("except AuditIntegrityError:", "except Exception:"),
        ("return (PromptGuardContentPart(serialized, inspection_text=_json_inspection_text(serialized)),)", "return foreign"),
        ("value, offset = decoder.raw_decode(serialized, start)", "value, offset = foreign(serialized, start)"),
    ],
)
def test_prompt_argument_producer_closes_both_returns_and_actual_inspection_helper(before, after):
    paths = {"src/elspeth/web/composer/prompt_guard_rendering.py", "src/elspeth/contracts/prompt_guard.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    receipt = fencing._reserve_prompt_argument_return_receipt(units)
    assert receipt["failures"] == [] and len(receipt["returns"]) == 2
    unit = next(unit for unit in units if unit.path.endswith("prompt_guard_rendering.py"))
    assert unit.source.count(before) == 1
    changed = fencing._parse_source(unit.path, unit.source.replace(before, after))
    refused = fencing._reserve_prompt_argument_return_receipt(tuple(changed if current.path == unit.path else current for current in units))
    assert refused["failures"] and refused["owner"] is None and refused["returns"] == set()


@pytest.mark.parametrize(
    "effect",
    [
        "json = foreign\n",
        "_json_inspection_text = foreign\n",
        "def alter():\n    global len\n    len = foreign\nalter()\n",
    ],
)
def test_prompt_argument_producer_keeps_json_and_owned_helper_bindings(effect):
    paths = {"src/elspeth/web/composer/prompt_guard_rendering.py", "src/elspeth/contracts/prompt_guard.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    assert fencing._reserve_prompt_argument_return_receipt(units)["failures"] == []
    unit = next(unit for unit in units if unit.path.endswith("prompt_guard_rendering.py"))
    changed = fencing._parse_source(unit.path, unit.source + "\n" + effect)
    refused = fencing._reserve_prompt_argument_return_receipt(tuple(changed if current.path == unit.path else current for current in units))
    assert refused["failures"] and refused["owner"] is None


def test_prompt_argument_receipt_returns_are_same_ast_and_never_scalar_authority():
    paths = {"src/elspeth/web/composer/prompt_guard_rendering.py", "src/elspeth/contracts/prompt_guard.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    receipt = fencing._reserve_prompt_argument_return_receipt(units)
    assert receipt["failures"] == [] and len(receipt["returns"]) == 2
    assert receipt["record"] == "elspeth.contracts.prompt_guard.PromptGuardContentPart"
    copied = tuple(fencing._parse_source(unit.path, unit.source) for unit in units)
    other = fencing._reserve_prompt_argument_return_receipt(copied)
    assert other["failures"] == [] and receipt["returns"].isdisjoint(other["returns"])
    assert fencing._reserve_prompt_argument_return_receipt(())["failures"]
    assert fencing._reserve_prompt_argument_return_receipt((*units, units[0]))["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "type(parts[0]).__new__ = replacement\n",
        "setattr(type(parts[0]), '__new__', replacement)\n",
        "opaque_callback(parts)\n",
        "opaque_callback(json_argument_content_parts)\n",
    ],
)
def test_actual_prompt_argument_result_keeps_nominal_class_and_escape_denials(effect):
    units = fencing._production_units()
    path = "src/elspeth/prompt_argument_result_probe.py"
    source = (
        "from elspeth.web.composer.prompt_guard_rendering import json_argument_content_parts\n"
        "parts = json_argument_content_parts('\"reference\"')\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.parametrize(
    "before,after",
    [
        ("return draft", "return capability"),
        ('edge = "input" if role is ControlRole.INPUT else "output"', "edge = opaque_callback(role)"),
        ("{capability.value} control mode.", "{opaque_callback(capability)} control mode."),
    ],
)
def test_control_disclosure_receipt_requires_the_complete_scalar_consumer(before, after):
    paths = {"src/elspeth/web/composer/required_controls.py", "src/elspeth/contracts/plugin_capabilities.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    receipt = fencing._reserve_control_disclosure_scalar_receipt(units)
    assert receipt["failures"] == [] and receipt["owner"] is not None
    unit = next(unit for unit in units if unit.path.endswith("required_controls.py"))
    owner = fencing._reserve_node_at(unit, "_disclosure_draft")
    segment = ast.get_source_segment(unit.source, owner)
    assert segment.count(before) == 1
    changed = fencing._parse_source(unit.path, unit.source.replace(segment, segment.replace(before, after)))
    refused = fencing._reserve_control_disclosure_scalar_receipt(
        tuple(changed if current.path == unit.path else current for current in units)
    )
    assert refused["failures"] and refused["owner"] is None


@pytest.mark.parametrize(
    "path,before,after",
    [
        ("src/elspeth/contracts/plugin_capabilities.py", 'INPUT = "input"', 'INPUT = opaque_callback("input")'),
        ("src/elspeth/contracts/plugin_capabilities.py", 'CONTENT_SAFETY = "content_safety"', "CONTENT_SAFETY = object()"),
        ("src/elspeth/contracts/plugin_capabilities.py", "from enum import StrEnum", "from foreign import StrEnum"),
        (
            "src/elspeth/web/composer/required_controls.py",
            "from elspeth.contracts.plugin_capabilities import ControlMode, ControlRole, PluginCapability",
            "from foreign import ControlMode, ControlRole, PluginCapability",
        ),
    ],
)
def test_control_disclosure_receipt_requires_actual_enum_value_and_import_owners(path, before, after):
    paths = {"src/elspeth/web/composer/required_controls.py", "src/elspeth/contracts/plugin_capabilities.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    assert fencing._reserve_control_disclosure_scalar_receipt(units)["failures"] == []
    unit = next(unit for unit in units if unit.path == path)
    assert unit.source.count(before) == 1
    changed = fencing._parse_source(path, unit.source.replace(before, after))
    refused = fencing._reserve_control_disclosure_scalar_receipt(tuple(changed if current.path == path else current for current in units))
    assert refused["failures"] and refused["owner"] is None


def test_control_disclosure_receipt_does_not_grant_formal_or_graph_origins():
    paths = {"src/elspeth/web/composer/required_controls.py", "src/elspeth/contracts/plugin_capabilities.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    receipt = fencing._reserve_control_disclosure_scalar_receipt(units)
    assert receipt["failures"] == []
    assert receipt["parameters"] == {
        "capability": "elspeth.contracts.plugin_capabilities.PluginCapability",
        "role": "elspeth.contracts.plugin_capabilities.ControlRole",
    }
    assert set(receipt) == {"failures", "owner", "parameters", "roots", "bindings"}
    assert fencing._reserve_control_disclosure_scalar_receipt(())["failures"]
    assert fencing._reserve_control_disclosure_scalar_receipt((*units, units[0]))["failures"]
    unit = next(unit for unit in units if unit.path.endswith("required_controls.py"))
    changed = fencing._parse_source(unit.path, unit.source + "\ndef alter():\n    global ControlRole\n    ControlRole = foreign\nalter()\n")
    assert fencing._reserve_control_disclosure_scalar_receipt(
        tuple(changed if current.path == unit.path else current for current in units)
    )["failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(PluginCapability.PROMPT_SHIELD)\n",
        "opaque_callback(_disclosure_draft)\n",
        "_disclosure_draft.__code__ = replacement\n",
        "ControlRole.INPUT._value_ = 'changed'\n",
    ],
)
def test_actual_control_disclosure_projection_does_not_release_enum_or_callable_origins(effect):
    units = fencing._production_units()
    path = "src/elspeth/control_disclosure_scalar_probe.py"
    source = (
        "from elspeth.web.composer.required_controls import _disclosure_draft\n"
        "from elspeth.contracts.plugin_capabilities import PluginCapability, ControlRole\n"
        "text = _disclosure_draft(capability=PluginCapability.PROMPT_SHIELD, control_plugin='control', llm_component_id='node', llm_component_kind='node', role=ControlRole.INPUT)\n"
        "opaque_callback(text)\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.parametrize(
    "before,after",
    [
        ("return options", "return role"),
        ("if role is ControlRole.OUTPUT and", "if opaque_callback(role) and"),
        ('options["source"] = "OUTPUT"', 'options["source"] = role'),
        ('options["source"] = "OUTPUT"', 'options["source"] = ControlRole.OUTPUT'),
        ("options.update(_direct_control_options(summaries, plugin_name))", "options.update(_direct_control_options(summaries, role))"),
    ],
)
def test_control_options_role_receipt_rejects_retention_and_transfer(before, after):
    paths = {"src/elspeth/web/composer/required_controls.py", "src/elspeth/contracts/plugin_capabilities.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    receipt = fencing._reserve_control_options_role_receipt(units)
    assert receipt["failures"] == [] and len(receipt["uses"]) == 1
    unit = next(unit for unit in units if unit.path.endswith("required_controls.py"))
    owner = fencing._reserve_node_at(unit, "_control_options")
    segment = ast.get_source_segment(unit.source, owner)
    assert segment.count(before) == 1
    changed = fencing._parse_source(unit.path, unit.source.replace(segment, segment.replace(before, after)))
    refused = fencing._reserve_control_options_role_receipt(tuple(changed if current.path == unit.path else current for current in units))
    assert refused["failures"] and refused["owner"] is None and refused["uses"] == set()


def test_control_options_role_receipt_binds_one_identity_operand_without_output_grant():
    paths = {"src/elspeth/web/composer/required_controls.py", "src/elspeth/contracts/plugin_capabilities.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    receipt = fencing._reserve_control_options_role_receipt(units)
    assert receipt["failures"] == [] and len(receipt["uses"]) == 1
    assert set(receipt) == {"failures", "owner", "uses", "role", "roots", "bindings"}
    copied = tuple(fencing._parse_source(unit.path, unit.source) for unit in units)
    other = fencing._reserve_control_options_role_receipt(copied)
    assert other["failures"] == [] and receipt["uses"].isdisjoint(other["uses"])
    assert fencing._reserve_control_options_role_receipt(())["failures"]
    assert fencing._reserve_control_options_role_receipt((*units, units[0]))["failures"]


@pytest.mark.parametrize(
    "effect",
    [
        "_control_options = foreign\n",
        "list = foreign\n",
        "def alter():\n    global ControlRole\n    ControlRole = foreign\nalter()\n",
    ],
)
def test_control_options_role_receipt_keeps_real_owner_and_bindings(effect):
    paths = {"src/elspeth/web/composer/required_controls.py", "src/elspeth/contracts/plugin_capabilities.py"}
    units = tuple(unit for unit in fencing._production_units() if unit.path in paths)
    assert fencing._reserve_control_options_role_receipt(units)["failures"] == []
    unit = next(unit for unit in units if unit.path.endswith("required_controls.py"))
    changed = fencing._parse_source(unit.path, unit.source + "\n" + effect)
    refused = fencing._reserve_control_options_role_receipt(tuple(changed if current.path == unit.path else current for current in units))
    assert refused["failures"] and refused["owner"] is None


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "effect",
    [
        "opaque_callback(ControlRole.INPUT)\n",
        "opaque_callback(_control_options)\n",
        "_control_options.__code__ = replacement\n",
        "_control_options(plugin_name='control', alias='operator', fields=(), summaries=ControlRole.INPUT, role=ControlRole.OUTPUT, disclosure_draft='review')\n",
    ],
)
def test_actual_control_options_consumes_only_its_exact_role_argument(effect):
    units = fencing._production_units()
    path = "src/elspeth/control_options_role_probe.py"
    source = (
        "from elspeth.web.composer.required_controls import _control_options\n"
        "from elspeth.contracts.plugin_capabilities import ControlRole\n"
        "options = _control_options(plugin_name='control', alias='operator', fields=(), summaries={}, role=ControlRole.INPUT, disclosure_draft='review')\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + effect)))
    assert any(path in failure for failure in changed["global_failures"]), changed["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "body,consumer,retained",
    [
        pytest.param(
            "    return part if True else None\n",
            "part = held()\nopaque_callback(part)\n",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="conditional-record-return",
        ),
        pytest.param(
            "    return part if True else None\n",
            "part = held()\nopaque_callback(part)\n",
            "PromptGuardTrust.REFERENCE",
            id="conditional-enum-return",
        ),
        pytest.param(
            "    yield (part if True else None)\n",
            "part = next(held())\nopaque_callback(part)\n",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="conditional-record-yield",
        ),
        pytest.param(
            "    yield from ((part,) if True else ())\n",
            "part = next(held())\nopaque_callback(part)\n",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="conditional-record-yield-from",
        ),
        pytest.param(
            "    return part or None\n",
            "part = held()\nopaque_callback(part)\n",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="boolean-record-return",
        ),
        pytest.param(
            "    return (choice := part)\n",
            "part = held()\nopaque_callback(part)\n",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="named-record-return",
        ),
    ],
)
def test_conditional_enum_producer_preserves_retained_return_leaves(body, consumer, retained):
    units = fencing._production_units()
    path = "src/elspeth/conditional_enum_producer_escape_probe.py"
    imports = "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\n"
    scalar_source = imports + "def held():\n    part = 'safe'\n" + body + consumer
    normal = fencing._reserve_run((*units, fencing._parse_source(path, scalar_source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    assert scalar_source.count("part = 'safe'") == 1
    retained_source = scalar_source.replace("part = 'safe'", "part = " + retained)
    changed = fencing._reserve_run((*units, fencing._parse_source(path, retained_source)))
    assert any(failure.startswith("retained enum producer return is unclosed " + path) for failure in changed["global_failures"]), changed[
        "global_failures"
    ]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "body,consumer",
    [
        pytest.param("    return [part for _ in (0,)]\n", "opaque_callback(held()[0])\n", id="list-comprehension"),
        pytest.param("    return {part for _ in (0,)}\n", "opaque_callback(held())\n", id="set-comprehension"),
        pytest.param("    return {part: 'x' for _ in (0,)}\n", "opaque_callback(held())\n", id="dictionary-key"),
        pytest.param("    return {'x': part for _ in (0,)}\n", "opaque_callback(held()['x'])\n", id="dictionary-value"),
        pytest.param("    return (part for _ in (0,))\n", "opaque_callback(next(held()))\n", id="generator-expression"),
        pytest.param("    return lambda: part\n", "opaque_callback(held()())\n", id="lambda-capture"),
        pytest.param("    return lambda value=part: value\n", "opaque_callback(held()())\n", id="lambda-default"),
        pytest.param("    return opaque_wrapper([part for _ in (0,)])\n", "opaque_callback(held())\n", id="call-wrapper"),
        pytest.param("    values = [part for _ in (0,)]\n    return values\n", "opaque_callback(held()[0])\n", id="container-alias"),
        pytest.param("    callback = lambda: part\n    return callback\n", "opaque_callback(held()())\n", id="closure-alias"),
        pytest.param(
            "    def capture():\n        return part\n    return capture\n", "opaque_callback(held()())\n", id="nested-function-capture"
        ),
        pytest.param("    yield [part for _ in (0,)]\n", "opaque_callback(next(held())[0])\n", id="yield-comprehension"),
        pytest.param("    yield from (part for _ in (0,))\n", "opaque_callback(next(held()))\n", id="yield-from-generator"),
        pytest.param("    return (lambda: [part for _ in (0,)])()\n", "opaque_callback(held()[0])\n", id="immediate-nested-lambda"),
        pytest.param("    return [value for value in (part,)]\n", "opaque_callback(held()[0])\n", id="first-iterable-capture"),
    ],
)
def test_nested_enum_export_preserves_captures_through_actual_expression_tree(body, consumer):
    units = fencing._production_units()
    path = "src/elspeth/nested_enum_export_probe.py"
    imports = "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\n"
    scalar_source = imports + "def held():\n    part = 'safe'\n" + body + consumer
    normal = fencing._reserve_run((*units, fencing._parse_source(path, scalar_source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    assert scalar_source.count("part = 'safe'") == 1
    retained_source = scalar_source.replace("part = 'safe'", "part = PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)")
    changed = fencing._reserve_run((*units, fencing._parse_source(path, retained_source)))
    assert any(failure.startswith("retained enum producer return is unclosed " + path) for failure in changed["global_failures"]), changed[
        "global_failures"
    ]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "safe_body,retained_body",
    [
        pytest.param("    return [part for part in (0,)]\n", "    return [part for other in (0,)]\n", id="comprehension-shadow"),
        pytest.param("    return lambda part: part\n", "    return lambda other: part\n", id="lambda-parameter-shadow"),
        pytest.param(
            "    def capture(part):\n        return part\n    return capture\n",
            "    def capture(other):\n        return part\n    return capture\n",
            id="nested-parameter-shadow",
        ),
        pytest.param("    return [part.value for _ in (0,)]\n", "    return [part for _ in (0,)]\n", id="proved-enum-value-projection"),
        pytest.param(
            "    callback = lambda part='safe': part\n    return callback\n",
            "    callback = lambda part=part: part\n    return callback\n",
            id="default-executes-outside-lambda",
        ),
    ],
)
def test_nested_enum_export_distinguishes_lexical_shadows_and_scalar_projection(safe_body, retained_body):
    units = fencing._production_units()
    path = "src/elspeth/nested_enum_shadow_probe.py"
    source = "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n    part = PromptGuardTrust.REFERENCE\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source + safe_body)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run((*units, fencing._parse_source(path, source + retained_body)))
    assert any(failure.startswith("retained enum producer return is unclosed " + path) for failure in changed["global_failures"]), changed[
        "global_failures"
    ]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "source",
    [
        "part = 'safe'\ndef assign():\n    global part\n    part = RETAINED\nassign()\ndef held():\n    return lambda: part\n",
        "def held():\n    part = 'safe'\n    def assign():\n        nonlocal part\n        part = RETAINED\n    assign()\n    return lambda: part\n",
    ],
)
def test_nested_enum_export_retains_explicit_lexical_rebinding(source):
    units = fencing._production_units()
    path = "src/elspeth/nested_enum_rebinding_probe.py"
    imports = "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\n"
    assert source.count("RETAINED") == 1
    normal = fencing._reserve_run((*units, fencing._parse_source(path, imports + source.replace("RETAINED", "'safe'"))))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    changed = fencing._reserve_run(
        (
            *units,
            fencing._parse_source(
                path,
                imports + source.replace("RETAINED", "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)"),
            ),
        )
    )
    assert any(failure.startswith("retained enum producer return is unclosed " + path) for failure in changed["global_failures"]), changed[
        "global_failures"
    ]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "body,expected,retained",
    [
        pytest.param(
            "    result = []\n    result.extend([part for _ in (0,)])\n    return result\n",
            "retained enum value passed to unclosed caller ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="extend-comprehension",
        ),
        pytest.param(
            "    result = []\n    alias = result\n    alias.extend([part for _ in (0,)])\n    return result\n",
            "retained enum value passed to unclosed caller ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="aliased-mutator",
        ),
        pytest.param(
            "    result = []\n    result.append([part for _ in (0,)])\n",
            "retained enum value passed to unclosed caller ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="append-compound",
        ),
        pytest.param(
            "    opaque_callback(value=[part for _ in (0,)])\n",
            "retained enum value passed to unclosed caller ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="keyword-comprehension",
        ),
        pytest.param(
            "    opaque_callback((part for _ in (0,)))\n",
            "retained enum value passed to unclosed caller ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="generator-argument",
        ),
        pytest.param(
            "    opaque_callback({part: 'x' for _ in (0,)})\n",
            "retained enum value passed to unclosed caller ",
            "PromptGuardTrust.REFERENCE",
            id="dictionary-key-argument",
        ),
        pytest.param(
            "    opaque_callback(part if True else None)\n",
            "retained enum value passed to unclosed caller ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="conditional-argument",
        ),
        pytest.param(
            "    opaque_callback(part or None)\n",
            "retained enum value passed to unclosed caller ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="boolean-argument",
        ),
        pytest.param(
            "    opaque_callback(saved := part)\n",
            "retained enum value passed to unclosed caller ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="named-argument",
        ),
        pytest.param(
            "    opaque_callback(*(part for _ in (0,)))\n",
            "retained enum value passed to unclosed caller ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="expanded-argument",
        ),
        pytest.param(
            "    holder.retained_probe = [part for _ in (0,)]\n",
            "retained supplier stored in unclosed carrier ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="attribute-store",
        ),
        pytest.param(
            "    result = {}\n    result['payload'] = [part for _ in (0,)]\n",
            "retained supplier stored in unclosed carrier ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="subscript-store",
        ),
        pytest.param(
            "    result = {}\n    result[part if True else None] = 'scalar'\n",
            "retained supplier stored as unclosed carrier key ",
            "PromptGuardTrust.REFERENCE",
            id="retained-store-key",
        ),
        pytest.param(
            "    result = []\n    result += [part for _ in (0,)]\n",
            "retained supplier augmentation is unclosed ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="local-augmentation",
        ),
        pytest.param(
            "    holder.retained_probe += [part for _ in (0,)]\n",
            "retained supplier augmentation is unclosed ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="attribute-augmentation",
        ),
        pytest.param(
            "    result = [part for _ in (0,)]\n    result.clear()\n",
            "retained supplier callable recovered ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="retained-receiver-call",
        ),
        pytest.param(
            "    for holder.retained_probe in (part for _ in (0,)):\n        pass\n",
            "retained supplier iteration target is unclosed ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="iteration-external-target",
        ),
        pytest.param(
            "    class Carrier(metaclass=opaque_metaclass, payload=[part for _ in (0,)]):\n        pass\n",
            "retained enum declaration transfer is unclosed ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="metaclass-keyword-transfer",
        ),
        pytest.param(
            "    result = []\n    result.extend([part])\n",
            "retained enum value passed to unclosed caller ",
            "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)",
            id="existing-literal-backstop",
        ),
    ],
)
def test_compound_enum_transfer_closes_actual_retaining_contexts(body, expected, retained):
    units = fencing._production_units()
    path = "src/elspeth/compound_enum_transfer_probe.py"
    prefix = (
        "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\ndef held(holder=None):\n    part = 'safe'\n"
    )
    ordinary = prefix + body
    normal = fencing._reserve_run((*units, fencing._parse_source(path, ordinary)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    assert ordinary.count("part = 'safe'") == 1
    protected = ordinary.replace("part = 'safe'", "part = " + retained)
    refused = fencing._reserve_run((*units, fencing._parse_source(path, protected)))
    assert any(failure.startswith(expected + path) for failure in refused["global_failures"]), refused["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "ordinary,protected",
    [
        pytest.param("[part for part in ('safe',)]", "[part for other in ('safe',)]", id="comprehension-shadow"),
        pytest.param("[part.value for _ in (0,)]", "[part for _ in (0,)]", id="owned-value-projection"),
        pytest.param("[part.name for _ in (0,)]", "[part for _ in (0,)]", id="owned-name-projection"),
        pytest.param("[str(part) for _ in (0,)]", "[part for _ in (0,)]", id="existing-str-consumer"),
        pytest.param("[isinstance(part, object) for _ in (0,)]", "[part for _ in (0,)]", id="existing-bool-consumer"),
    ],
)
def test_compound_enum_transfer_preserves_actual_shadows_and_scalar_consumers(ordinary, protected):
    units = fencing._production_units()
    path = "src/elspeth/compound_enum_scalar_probe.py"
    prefix = "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n    part = PromptGuardTrust.REFERENCE\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, prefix + "    opaque_callback(value=" + ordinary + ")\n")))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    refused = fencing._reserve_run((*units, fencing._parse_source(path, prefix + "    opaque_callback(value=" + protected + ")\n")))
    assert any(failure.startswith("retained enum value passed to unclosed caller " + path) for failure in refused["global_failures"]), (
        refused["global_failures"]
    )


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "signature",
    [
        pytest.param("part=RETAINED", id="positional-default"),
        pytest.param("prefix='ordinary', part=RETAINED", id="last-positional-alignment"),
        pytest.param("prefix='ordinary', part=RETAINED, /", id="positional-only-default"),
        pytest.param("*, part=RETAINED", id="keyword-only-default"),
        pytest.param("*, ordinary=None, part=RETAINED", id="keyword-only-alignment"),
    ],
)
def test_actual_callable_default_retention_reaches_definition_and_returned_formal(signature):
    units = fencing._production_units()
    path = "src/elspeth/enum_default_formal_probe.py"
    prefix = "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\n"
    template = prefix + "def held(" + signature + "):\n    return part\nopaque_callback(held())\n"
    assert template.count("RETAINED") == 1
    normal = fencing._reserve_run((*units, fencing._parse_source(path, template.replace("RETAINED", "'safe'"))))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    refused = fencing._reserve_run(
        (
            *units,
            fencing._parse_source(
                path,
                template.replace("RETAINED", "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)"),
            ),
        )
    )
    assert any(failure.startswith("retained enum callable capture is unclosed " + path) for failure in refused["global_failures"]), refused[
        "global_failures"
    ]
    assert any(failure.startswith("retained enum producer return is unclosed " + path) for failure in refused["global_failures"]), refused[
        "global_failures"
    ]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "consumer",
    [
        "def held():\n    return exposed\nopaque_callback(held())\n",
        "alias = exposed\nopaque_callback(alias)\n",
        "opaque_callback(exposed.__defaults__[0])\n",
    ],
)
def test_actual_callable_retains_default_even_when_its_body_returns_scalar(consumer):
    units = fencing._production_units()
    path = "src/elspeth/enum_default_callable_probe.py"
    template = (
        "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\n"
        "def exposed(part=RETAINED):\n    return 'ordinary'\n"
    ) + consumer
    assert template.count("RETAINED") == 1
    normal = fencing._reserve_run((*units, fencing._parse_source(path, template.replace("RETAINED", "'safe'"))))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    refused = fencing._reserve_run(
        (
            *units,
            fencing._parse_source(
                path,
                template.replace("RETAINED", "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)"),
            ),
        )
    )
    assert any(failure.startswith("retained enum callable capture is unclosed " + path) for failure in refused["global_failures"]), refused[
        "global_failures"
    ]


@pytest.mark.landscape_long_run
def test_imported_callable_cannot_hide_its_actual_retained_default():
    units = fencing._production_units()
    owner_path = "src/elspeth/enum_default_owner_probe.py"
    consumer_path = "src/elspeth/enum_default_import_probe.py"
    template = (
        "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\n"
        "def exposed(*, part=RETAINED):\n    return 'ordinary'\n"
    )
    consumer = fencing._parse_source(
        consumer_path, "from elspeth.enum_default_owner_probe import exposed as alias\nopaque_callback(alias)\n"
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(owner_path, template.replace("RETAINED", "'safe'")), consumer))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    refused = fencing._reserve_run(
        (
            *units,
            fencing._parse_source(owner_path, template.replace("RETAINED", "PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)")),
            consumer,
        )
    )
    assert any(failure.startswith("retained enum callable capture is unclosed " + owner_path) for failure in refused["global_failures"]), (
        refused["global_failures"]
    )


@pytest.mark.landscape_long_run
@pytest.mark.parametrize("kind", ["function", "lambda"])
def test_callable_scalar_output_does_not_erase_its_outer_enum_capture(kind):
    units = fencing._production_units()
    path = "src/elspeth/enum_scalar_closure_probe.py"
    capture = "    def capture():\n        return part.value\n" if kind == "function" else "    capture = lambda: part.value\n"
    # The safe pair accesses an ordinary object's attribute in both bodies;
    # no enum scalar projection alone is treated as an unsafe result value.
    prefix = "from elspeth.contracts.prompt_guard import PromptGuardTrust\n"
    template = prefix + "def held(part):\n" + capture + "    opaque_callback(capture)\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, template)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    retained = template.replace("def held(part):\n", "def held():\n    part = PromptGuardTrust.REFERENCE\n")
    refused = fencing._reserve_run((*units, fencing._parse_source(path, retained)))
    assert any(
        path in failure
        and failure.startswith(
            (
                "retained enum callable capture is unclosed ",
                "retained supplier lambda metadata capture is unclosed ",
                "retained enum value passed to unclosed caller ",
            )
        )
        for failure in refused["global_failures"]
    ), refused["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "subject,recipient,statement",
    [
        pytest.param("[part for _ in (0,)]", "Probe", "return CHECK", id="returned-comprehension"),
        pytest.param("[part for _ in (0,)]", "Probe", "CHECK", id="discarded-comprehension"),
        pytest.param("part", "Probe", "CHECK", id="direct-record"),
        pytest.param("[part for _ in (0,)]", "(str, Probe)", "CHECK", id="tuple-class-info"),
        pytest.param("[part for _ in (0,)]", "Alias", "CHECK", id="aliased-class-info"),
    ],
)
def test_retained_class_check_requires_the_actual_hook_free_recipient(subject, recipient, statement):
    units = fencing._production_units()
    path = "src/elspeth/retained_class_check_probe.py"
    prefix = (
        "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\n"
        "captured = []\n"
        "class Meta(type):\n"
        "    def __instancecheck__(cls, candidate):\n"
        "        captured.append(candidate)\n"
        "        return False\n"
        "class Probe(metaclass=Meta):\n"
        "    pass\n"
        "Alias = Probe\n"
        "def held():\n"
        "    part = PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)\n"
        "    opaque_callback(isinstance([part for _ in (0,)], object))\n"
    )
    # Same actual custom recipient, but only ordinary text crosses it. The
    # protected-value object check separately proves the retained safe shape.
    normal_call = "isinstance('safe', " + recipient + ")"
    normal_source = prefix + "    " + statement.replace("CHECK", normal_call) + "\nheld()\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, normal_source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    assert normal_source.count(normal_call) == 1
    protected = normal_source.replace(normal_call, "isinstance(" + subject + ", " + recipient + ")")
    refused = fencing._reserve_run((*units, fencing._parse_source(path, protected)))
    assert any(
        failure.startswith("retained supplier class-check recipient is unclosed " + path) for failure in refused["global_failures"]
    ), refused["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "subject",
    [
        pytest.param("PromptGuardContentPart", id="actual-class"),
        pytest.param("type(part)", id="actual-record-class-recovery"),
    ],
)
def test_retained_issubclass_requires_actual_class_and_hook_free_recipient(subject):
    units = fencing._production_units()
    path = "src/elspeth/retained_subclass_check_probe.py"
    prefix = (
        "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\n"
        "captured = []\n"
        "class Meta(type):\n"
        "    def __subclasscheck__(cls, candidate):\n"
        "        captured.append(candidate)\n"
        "        return False\n"
        "class Probe(metaclass=Meta):\n"
        "    pass\n"
        "class Ordinary:\n"
        "    pass\n"
        "def held():\n"
        "    part = PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)\n"
    )
    safe_call = "issubclass(" + subject + ", object)"
    ordinary = prefix + "    opaque_callback(" + safe_call + ")\n    issubclass(Ordinary, Probe)\nheld()\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, ordinary)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    assert ordinary.count("issubclass(Ordinary, Probe)") == 1
    protected = ordinary.replace("issubclass(Ordinary, Probe)", "issubclass(" + subject + ", Probe)")
    refused = fencing._reserve_run((*units, fencing._parse_source(path, protected)))
    assert any(
        failure.startswith("retained supplier class-check recipient is unclosed " + path) for failure in refused["global_failures"]
    ), refused["global_failures"]


@pytest.mark.landscape_long_run
def test_class_check_recipient_respects_actual_builtin_shadow():
    units = fencing._production_units()
    path = "src/elspeth/retained_class_check_shadow_probe.py"
    prefix = "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\n"
    ordinary = prefix + "def held(object):\n    part = 'safe'\n    return isinstance([part for _ in (0,)], object)\n"
    normal = fencing._reserve_run((*units, fencing._parse_source(path, ordinary)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    assert ordinary.count("part = 'safe'") == 1
    protected = ordinary.replace("part = 'safe'", "part = PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)")
    refused = fencing._reserve_run((*units, fencing._parse_source(path, protected)))
    assert any(
        failure.startswith("retained supplier class-check recipient is unclosed " + path) for failure in refused["global_failures"]
    ), refused["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "scope_source",
    [
        pytest.param("class Holder:\n    object = Probe\n    result = isinstance(VALUE, object)\n", id="class-local-recipient"),
        pytest.param(
            "class Holder:\n    object = Probe\n    def method(value=isinstance(VALUE, object)):\n        return value\n",
            id="class-eager-default",
        ),
        pytest.param(
            "class Holder:\n    builtins_alias = FakeBuiltins\n    result = isinstance(VALUE, builtins_alias.object)\n",
            id="class-module-alias-shadow",
        ),
        pytest.param("class Holder:\n    isinstance = capture\n    result = isinstance(VALUE, object)\n", id="class-callee-shadow"),
        pytest.param(
            "object = Probe\ndef held():\n    global object\n    return isinstance(VALUE, object)\nheld()\n", id="global-recipient"
        ),
        pytest.param(
            "def outer():\n    object = Probe\n    def held():\n        nonlocal object\n        return isinstance(VALUE, object)\n    return held()\nouter()\n",
            id="nonlocal-recipient",
        ),
        pytest.param(
            "def outer(object):\n    def held():\n        return isinstance(VALUE, object)\n    return held()\nouter(Probe)\n",
            id="enclosing-formal-recipient",
        ),
        pytest.param("result = [isinstance(VALUE, object) for object in (Probe,)]\n", id="comprehension-recipient"),
    ],
)
def test_class_check_uses_actual_lexical_recipient_not_module_spelling(scope_source):
    units = fencing._production_units()
    path = "src/elspeth/retained_lexical_class_check_probe.py"
    prefix = (
        "import builtins as builtins_alias\n"
        "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\n"
        "captured = []\n"
        "class Meta(type):\n"
        "    def __instancecheck__(cls, candidate):\n"
        "        captured.append(candidate)\n"
        "        return False\n"
        "class Probe(metaclass=Meta):\n"
        "    pass\n"
        "class FakeBuiltins:\n"
        "    object = Probe\n"
        "def capture(candidate, recipient):\n"
        "    captured.append(candidate)\n"
        "    return False\n"
        "part = PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)\n"
    )
    assert scope_source.count("VALUE") == 1
    normal_source = prefix + scope_source.replace("VALUE", "'safe'")
    normal = fencing._reserve_run((*units, fencing._parse_source(path, normal_source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    protected_source = prefix + scope_source.replace("VALUE", "part")
    refused = fencing._reserve_run((*units, fencing._parse_source(path, protected_source)))
    assert any(
        failure.startswith("retained supplier class-check recipient is unclosed " + path) for failure in refused["global_failures"]
    ), refused["global_failures"]


@pytest.mark.landscape_long_run
@pytest.mark.parametrize(
    "consumer",
    [
        pytest.param("result = isinstance(part, object)\n", id="module-builtin"),
        pytest.param("class Holder:\n    result = isinstance(part, object)\n", id="plain-class-builtin"),
        pytest.param(
            "class Holder:\n    def method(value=isinstance(part, object)):\n        return value\n", id="class-eager-default-builtin"
        ),
        pytest.param("def held():\n    return isinstance(part, object)\nresult = held()\n", id="function-builtin"),
        pytest.param(
            "class Holder:\n    object = Unrelated\n    def method():\n        return isinstance(part, object)\nresult = Holder.method()\n",
            id="method-skips-class-namespace",
        ),
        pytest.param(
            "class Outer:\n    object = Unrelated\n    class Inner:\n        result = isinstance(part, object)\n",
            id="nested-class-skips-outer-class",
        ),
        pytest.param("result = [isinstance(part, object) for ignored in (0,)]\n", id="comprehension-free-builtin"),
        pytest.param("import builtins as runtime\nresult = runtime.isinstance(part, runtime.object)\n", id="explicit-builtins-import"),
        pytest.param("from builtins import object as recipient\nresult = isinstance(part, recipient)\n", id="explicit-object-import"),
    ],
)
def test_class_check_preserves_actual_builtin_recipient_scopes(consumer):
    units = fencing._production_units()
    path = "src/elspeth/retained_lexical_builtin_positive.py"
    source = (
        "from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust\n"
        "class Unrelated:\n    pass\n"
        "part = PromptGuardContentPart('x', PromptGuardTrust.REFERENCE)\n" + consumer
    )
    normal = fencing._reserve_run((*units, fencing._parse_source(path, source)))
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []


def _finite_retained_lookup_failures(probe):
    """Exercise the real walker without claiming a complete reserve proof.

    Companion receipt owners are intentionally absent from this small cohort.
    Each caller calibrates this path projection with a same-path protected
    refusal after its scalar positive; unrelated receipt failures stay outside
    the finite lexical lookup assertion.
    """
    (supplier,) = _source_units(("src/elspeth/contracts/prompt_guard.py",))
    failures = fencing._reserve_selected_origin_effect_failures(
        (supplier, probe),
        additional_supplier_roots=frozenset({"elspeth.contracts.prompt_guard.PromptGuardTrust"}),
    )
    return [failure for failure in failures if probe.path + ":" in failure]


@pytest.mark.parametrize(
    "body,refusal",
    [
        pytest.param(
            "    a = (leaf, leaf)\n    b = (a, a)\n    c = (b, b)\n    d = (c, c)\n    return d\n",
            "retained enum producer return is unclosed ",
            id="shared-binding-dag",
        ),
        pytest.param(
            "    a = ()\n    b = a\n    a = (b, leaf)\n    b = a\n    return b\n",
            "retained enum producer return is unclosed ",
            id="cycle-with-later-protected-leaf",
        ),
        pytest.param(
            "    a = ()\n    b = (a, leaf)\n    a = b\n    return a\n",
            "retained enum producer return is unclosed ",
            id="cycle-with-earlier-protected-leaf",
        ),
        pytest.param(
            "    a = ()\n    b = a\n    a = (b, leaf)\n    b = a\n    b += ()\n",
            "retained supplier augmentation is unclosed ",
            id="cycle-read-target",
        ),
        pytest.param(
            "    a = (leaf, leaf)\n    b = (a, a)\n    return lambda: b\n",
            "retained enum producer return is unclosed ",
            id="shared-binding-capture",
        ),
    ],
)
def test_retained_lookup_memo_preserves_finite_dags_and_cycle_cuts(body, refusal, monkeypatch):
    path = "src/elspeth/retained_lookup_finite_probe.py"
    prefix = "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n    leaf = 'safe'\n"
    scalar = fencing._parse_source(path, prefix + body)
    assert _finite_retained_lookup_failures(scalar) == []
    assert prefix.count("leaf = 'safe'") == 1
    protected = fencing._parse_source(path, prefix.replace("leaf = 'safe'", "leaf = PromptGuardTrust.REFERENCE") + body)
    failures = _finite_retained_lookup_failures(protected)
    assert any(failure.startswith(refusal + path + ":") for failure in failures), failures

    # Compare the same actual ASTs with only this lookup decorator bypassed.
    # All other caches, recursive decisions and source guards stay original.
    original_cache = fencing.cache

    def uncached_lookup(function):
        return function if function.__qualname__.endswith(".retained_name_producers") else original_cache(function)

    monkeypatch.setattr(fencing, "cache", uncached_lookup)
    assert _finite_retained_lookup_failures(scalar) == []
    assert _finite_retained_lookup_failures(protected) == failures


def test_retained_lookup_memo_reuses_only_current_unit_lexical_queries(monkeypatch):
    original_cache = fencing.cache
    lookup_caches = []

    def observed_cache(function):
        # Delegate every cache and result unchanged; observe only the measured
        # lookup's real wrapper and actual current-unit identity.
        wrapped = original_cache(function)
        if function.__qualname__.endswith(".retained_name_producers"):
            lookup_caches.append((function.__kwdefaults__["unit"], wrapped))
            assert wrapped.cache_info().currsize == 0
        return wrapped

    monkeypatch.setattr(fencing, "cache", observed_cache)
    path = "src/elspeth/retained_lookup_memo_probe.py"
    source = (
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\n"
        "def held():\n"
        "    leaf = 'safe'\n"
        "    a = (leaf, leaf)\n"
        "    b = (a, a)\n"
        "    c = (b, b)\n"
        "    d = (c, c)\n"
        "    return d\n"
    )
    probe = fencing._parse_source(path, source)
    assert _finite_retained_lookup_failures(probe) == []
    first = [wrapped for unit, wrapped in lookup_caches if unit is probe]
    assert len(first) == 1
    info = first[0].cache_info()
    assert info.hits > 0
    assert info.misses == info.currsize > 0
    assert info.currsize <= sum(isinstance(node, ast.Name) for node in ast.walk(probe.tree))
    held = next(node for node in probe.tree.body if isinstance(node, ast.FunctionDef) and node.name == "held")
    returned = next(node.value for node in held.body if isinstance(node, ast.Return))
    original_producer = next(
        node.targets[0]
        for node in held.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "d"
    )
    result = first[0](returned)
    assert result[0] == id(held)
    assert len(result[1]) == 1 and result[1][0] is original_producer
    assert first[0](returned) is result
    assert first[0](returned)[1] is result[1]

    # Reuse this exact SourceUnit/AST, changing only its actual leaf. A memo
    # attached to SourceUnit, spelling or a prior pass must not survive here.
    leaf_assignment = next(
        node
        for node in ast.walk(probe.tree)
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "leaf" for target in node.targets)
    )
    original_leaf = leaf_assignment.value
    leaf_assignment.value = ast.increment_lineno(ast.parse("PromptGuardTrust.REFERENCE", mode="eval").body, original_leaf.lineno - 1)
    fencing._attach_parents(probe.tree)
    failures = _finite_retained_lookup_failures(probe)
    assert any(failure.startswith("retained enum producer return is unclosed " + path + ":") for failure in failures), failures
    leaf_assignment.value = original_leaf
    fencing._attach_parents(probe.tree)
    assert _finite_retained_lookup_failures(probe) == []
    current = [wrapped for unit, wrapped in lookup_caches if unit is probe]
    assert len(current) == 3
    assert len({id(wrapped) for wrapped in current}) == 3
    assert all(wrapped.cache_info().hits > 0 for wrapped in current)

    fresh_probe = fencing._parse_source(path, source)
    assert _finite_retained_lookup_failures(fresh_probe) == []
    fresh = [wrapped for unit, wrapped in lookup_caches if unit is fresh_probe]
    assert len(fresh) == 1 and all(fresh[0] is not wrapped for wrapped in current)
    fresh_held = next(node for node in fresh_probe.tree.body if isinstance(node, ast.FunctionDef) and node.name == "held")
    fresh_returned = next(node.value for node in fresh_held.body if isinstance(node, ast.Return))
    fresh_result = fresh[0](fresh_returned)
    assert fresh_result[0] == id(fresh_held)
    assert fresh_result[1] is not result[1]
    assert fresh_result[1][0] is not original_producer


def _empty_root_complete_finite_failures(probe):
    (supplier,) = _source_units(("src/elspeth/contracts/prompt_guard.py",))
    return fencing._reserve_selected_origin_effect_failures(
        (supplier, probe),
        additional_supplier_roots=frozenset({"elspeth.contracts.prompt_guard.PromptGuardTrust"}),
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("    a = (leaf, leaf)\n    b = (a, a)\n    return b\n", id="shared-dag"),
        pytest.param("    a = ()\n    b = a\n    a = (b, leaf)\n    b = a\n    return b\n", id="cycle"),
        pytest.param("    a = (leaf,)\n    a += ()\n", id="augmentation-read"),
        pytest.param("    def child(value=leaf):\n        return value\n    return child\n", id="positional-default"),
        pytest.param("    def child(*, value=leaf):\n        return value\n    return child\n", id="keyword-default"),
        pytest.param("    return lambda: leaf\n", id="capture"),
        pytest.param("    result = []\n    result.extend([leaf for ignored in (0,)])\n    return result\n", id="compound-mutator"),
        pytest.param("    opaque_callback(value=[leaf for ignored in (0,)])\n", id="compound-keyword"),
        pytest.param("    def child():\n        nonlocal leaf\n        return leaf\n    return child\n", id="nonlocal"),
    ],
)
def test_retained_empty_root_memo_preserves_complete_finite_verdicts(body, monkeypatch):
    path = "src/elspeth/retained_empty_root_semantic_probe.py"
    prefix = "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n    leaf = 'safe'\n"
    ordinary = fencing._parse_source(path, prefix + body)
    protected = fencing._parse_source(path, prefix.replace("leaf = 'safe'", "leaf = PromptGuardTrust.REFERENCE") + body)
    normal = _empty_root_complete_finite_failures(ordinary)
    assert not any(path + ":" in failure for failure in normal), normal
    refused = _empty_root_complete_finite_failures(protected)
    assert any(path + ":" in failure for failure in refused), refused
    original_cache = fencing.cache

    def bypass_only_empty_root(function):
        return function if function.__qualname__.endswith(".retained_empty_root_state") else original_cache(function)

    monkeypatch.setattr(fencing, "cache", bypass_only_empty_root)
    # Exact complete API lists, including unrelated missing companion receipts.
    # Path projection calibrates the finite witness; it is not the equivalence.
    assert _empty_root_complete_finite_failures(ordinary) == normal
    assert _empty_root_complete_finite_failures(protected) == refused


def _observe_current_empty_root_queries(monkeypatch, path, check):
    original_cache = fencing.cache

    def observe(function):
        wrapped = original_cache(function)
        if not function.__qualname__.endswith(".retained_empty_root_state"):
            return wrapped
        evaluator = function.__kwdefaults__["evaluate"]
        unit = evaluator.__kwdefaults__["unit"]
        assert wrapped.cache_info().currsize == 0
        checked = False

        def current_query(value, **kwargs):
            nonlocal checked
            result = wrapped(value, **kwargs)
            if unit.path == path and not checked:
                checked = True
                # Checks run during this actual unit's query. A retained wrapper
                # must never be used for a new miss after shared loop cells move.
                check(unit, wrapped, evaluator)
            return result

        return current_query

    monkeypatch.setattr(fencing, "cache", observe)


def test_retained_empty_root_memo_keeps_shadowing_and_actual_unit_identity(monkeypatch):
    path = "src/elspeth/retained_empty_root_shadow_probe.py"
    observations = []

    def check(unit, root, evaluate):
        held = next(node for node in unit.tree.body if isinstance(node, ast.FunctionDef) and node.name == "held")
        returned = held.body[0].value
        assert root(returned, read_target=False) == evaluate(returned, frozenset(), read_target=False)
        assert root(returned, read_target=False)[1] is False
        outer_target = unit.tree.body[1].targets[0]
        assert root(outer_target, read_target=True)[1] is True
        assert root(outer_target, read_target=False) == (None, False)
        observations.append((unit, root))

    _observe_current_empty_root_queries(monkeypatch, path, check)
    source = (
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\n"
        "leaf = PromptGuardTrust.REFERENCE\n"
        "def held(leaf):\n    return leaf\n"
        "def other():\n    leaf = 'safe'\n    return lambda: leaf\n"
        "result = [leaf for leaf in ('safe',)]\n"
    )
    probe = fencing._parse_source(path, source)
    verdict = _empty_root_complete_finite_failures(probe)
    assert not any(path + ":" in failure for failure in verdict), verdict
    assert len(observations) == 1 and observations[0][0] is probe
    assert _empty_root_complete_finite_failures(probe) == verdict
    assert len(observations) == 2 and observations[1][0] is probe
    assert observations[0][1] is not observations[1][1]


def test_retained_empty_root_memo_reuses_complete_pairs_without_recursive_cut_pollution(monkeypatch):
    path = "src/elspeth/retained_empty_root_work_probe.py"
    observations = []

    def check(unit, root, evaluate):
        # This is reached only through a genuine original effect-pass root call.
        assert root.cache_info().misses > 0
        held = next(node for node in unit.tree.body if isinstance(node, ast.FunctionDef))
        returned = next(node.value for node in held.body if isinstance(node, ast.Return))
        result = root(returned, read_target=False)
        assert result == evaluate(returned, frozenset(), read_target=False)
        before = root.cache_info()
        assert root(returned, read_target=False) is result
        after = root.cache_info()
        assert after.hits == before.hits + 1
        assert after.misses == before.misses and after.currsize == before.currsize
        # A path-cut result is evaluated directly and never enters the root cache.
        info = root.cache_info()
        assert evaluate(returned, frozenset({id(returned)}), read_target=False) == (None, False)
        assert root.cache_info() == info
        assert root(returned, read_target=False) is result
        leaf = next(node for node in held.body if isinstance(node, ast.Assign) and node.targets[0].id == "leaf")
        target = leaf.targets[0]
        assert root(target, read_target=False) == (None, False)
        assert root(target, read_target=True) == evaluate(target, frozenset(), read_target=True)
        assert root(target, read_target=True)[1] is result[1]
        observations.append((unit, root, returned, result))

    _observe_current_empty_root_queries(monkeypatch, path, check)
    source = (
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\n"
        "def held():\n"
        "    leaf = 'safe'\n"
        "    a = ()\n    b = a\n    a = (b, leaf)\n    b = a\n"
        "    return b\n"
    )
    probe = fencing._parse_source(path, source)
    assert _finite_retained_lookup_failures(probe) == []
    assert len(observations) == 1 and observations[0][3][1] is False
    held = next(node for node in probe.tree.body if isinstance(node, ast.FunctionDef))
    leaf = next(node for node in held.body if isinstance(node, ast.Assign) and node.targets[0].id == "leaf")
    original = leaf.value
    leaf.value = ast.increment_lineno(ast.parse("PromptGuardTrust.REFERENCE", mode="eval").body, original.lineno - 1)
    fencing._attach_parents(probe.tree)
    failures = _finite_retained_lookup_failures(probe)
    assert any(failure.startswith("retained enum producer return is unclosed " + path + ":") for failure in failures), failures
    assert len(observations) == 2 and observations[1][3][1] is True
    assert observations[1][2] is observations[0][2]
    leaf.value = original
    fencing._attach_parents(probe.tree)
    assert _finite_retained_lookup_failures(probe) == []
    assert len(observations) == 3 and observations[2][3][1] is False
    assert len({id(entry[1]) for entry in observations}) == 3
    assert all(entry[0] is probe for entry in observations)
    fresh = fencing._parse_source(path, source)
    assert _finite_retained_lookup_failures(fresh) == []
    assert len(observations) == 4 and observations[3][0] is fresh
    assert observations[3][1] is not observations[0][1]
    assert observations[3][2] is not observations[0][2]
    assert observations[3][3][1] is False
    # No old wrapper/evaluator is invoked after its unit/pass has finished.


def test_retained_empty_root_memo_does_not_cache_failed_computations(monkeypatch):
    path = "src/elspeth/retained_empty_root_exception_probe.py"
    checked = []

    def check(unit, root, evaluate):
        node = unit.tree.body[0].value
        expected = RuntimeError("original evaluator failure")
        calls = []

        def fail(value, seen, *, read_target):
            assert value is node and seen == frozenset() and read_target is False
            calls.append(value)
            raise expected

        before = root.cache_info()
        # Exercise the actual wrapper in the current unit, with a deliberately
        # failing bound evaluator; no original walker verdict is fabricated.
        for _ in range(2):
            with pytest.raises(RuntimeError) as caught:
                root(node, read_target=False, evaluate=fail)
            assert caught.value is expected
        assert calls == [node, node]
        assert root.cache_info().currsize == before.currsize
        assert root.cache_info().misses == before.misses + 2
        assert root(node, read_target=False) == evaluate(node, frozenset(), read_target=False)
        checked.append(unit)

    _observe_current_empty_root_queries(monkeypatch, path, check)
    probe = fencing._parse_source(path, "value = 'safe'\n")
    assert _finite_retained_lookup_failures(probe) == []
    assert checked == [probe]


def test_retained_empty_root_memo_reuses_actual_walker_queries(monkeypatch):
    original_cache = fencing.cache
    path = "src/elspeth/retained_empty_root_traffic_probe.py"
    observations = []

    def observe(function):
        wrapped = original_cache(function)
        if not function.__qualname__.endswith(".retained_empty_root_state"):
            return wrapped
        unit = function.__kwdefaults__["evaluate"].__kwdefaults__["unit"]

        def actual_query(value, **kwargs):
            before = wrapped.cache_info()
            result = wrapped(value, **kwargs)
            after = wrapped.cache_info()
            if unit.path == path:
                observations.append((value, kwargs["read_target"], before, after))
            return result

        return actual_query

    monkeypatch.setattr(fencing, "cache", observe)
    # Actual class-check code queries its argument for retained operands,
    # subject and enum arguments. No test-triggered query contributes here.
    probe = fencing._parse_source(path, "def held():\n    value = 'safe'\n    return isinstance(value, object)\n")
    assert _finite_retained_lookup_failures(probe) == []
    assert observations
    assert any(after.hits == before.hits + 1 for _, _, before, after in observations)
    assert any(after.misses == before.misses + 1 for _, _, before, after in observations)
    keys = [(id(value), read_target) for value, read_target, _, _ in observations]
    assert len(set(keys)) < len(keys)
    assert sum(after.misses - before.misses for _, _, before, after in observations) == len(set(keys))


class _RetentionNoHits(dict):
    def get(self, key, default=None):
        return default


class _RetentionFrames(list):
    def __init__(self):
        super().__init__()
        self.entries = 0
        self.maximum_depth = 0

    def append(self, value):
        self.entries += 1
        super().append(value)
        self.maximum_depth = max(self.maximum_depth, len(self))


def _run_retention_context_probe(monkeypatch, probe, *, reuse=True, check=None, observe=None, lookup=None):
    original_cache = fencing.cache
    state = {}

    def decorated(function):
        wrapped = original_cache(function)
        if function.__qualname__.endswith(".retained_name_producers"):
            if function.__kwdefaults__["unit"] is not probe or lookup is None:
                return wrapped

            def lexical(value):
                lookup(value, state)
                return wrapped(value)

            return lexical
        if not function.__qualname__.endswith(".retained_empty_root_state"):
            return wrapped
        evaluate = function.__kwdefaults__["evaluate"]
        if evaluate.__kwdefaults__["unit"] is not probe:
            return wrapped
        memo = evaluate.__kwdefaults__["memo"]
        if not reuse:
            memo["entries"] = _RetentionNoHits()
        if observe is not None:
            observe(memo)
        state.update(evaluate=evaluate, memo=memo)
        checked = False

        def current(value, **kwargs):
            nonlocal checked
            result = wrapped(value, **kwargs)
            if not checked and check is not None:
                checked = True
                assert memo["traces"] == []
                # Direct probes are inside the live unit, after its real root
                # query, and never contribute to the independent work witness.
                check(evaluate, memo, state)
            return result

        return current

    with monkeypatch.context() as patch:
        patch.setattr(fencing, "cache", decorated)
        result = _empty_root_complete_finite_failures(probe)
    return result


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("    a = (leaf, leaf)\n    b = (a, a)\n    return b\n", id="dag"),
        pytest.param("    a = ()\n    b = a\n    a = (b, leaf)\n    b = a\n    return b\n", id="cycle"),
        pytest.param("    a = (leaf,)\n    a += ()\n", id="read-target"),
        pytest.param("    def child(value=leaf):\n        return value\n    return child\n", id="positional-default"),
        pytest.param("    def child(*, value=leaf):\n        return value\n    return child\n", id="keyword-default"),
        pytest.param("    return lambda: leaf\n", id="lambda-capture"),
        pytest.param("    def child():\n        nonlocal leaf\n        return leaf\n    return child\n", id="nonlocal"),
        pytest.param("    result=[]\n    result.extend([leaf for ignored in (0,)])\n    return result\n", id="compound-mutator"),
        pytest.param("    opaque_callback(value=[leaf for ignored in (0,)])\n", id="compound-keyword"),
        pytest.param("    return (leaf for ignored in (0,))\n", id="generator"),
        pytest.param("    return leaf if True else None\n", id="conditional"),
    ],
)
def test_retention_context_reuse_preserves_complete_finite_verdicts(monkeypatch, body):
    path = "src/elspeth/retention_context_matrix_probe.py"
    prefix = "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n    leaf = 'safe'\n"
    scalar = fencing._parse_source(path, prefix + body)
    protected = fencing._parse_source(path, prefix.replace("leaf = 'safe'", "leaf = PromptGuardTrust.REFERENCE") + body)
    positive = _run_retention_context_probe(monkeypatch, scalar)
    assert not any(path + ":" in failure for failure in positive), positive
    refused = _run_retention_context_probe(monkeypatch, protected)
    assert any(path + ":" in failure for failure in refused), refused
    assert _run_retention_context_probe(monkeypatch, scalar, reuse=False) == positive
    assert _run_retention_context_probe(monkeypatch, protected, reuse=False) == refused


def test_retention_context_traces_callable_membership_and_child_hits(monkeypatch):
    path = "src/elspeth/retention_context_capture_probe.py"
    source = (
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\n"
        "def held():\n    leaf = PromptGuardTrust.REFERENCE\n"
        "    def callback():\n        return leaf\n"
        "    return callback.value\n"
    )
    probe = fencing._parse_source(path, source)
    owner = probe.tree.body[1]
    callback = next(node for node in owner.body if isinstance(node, ast.FunctionDef))
    parent = next(node.value for node in owner.body if isinstance(node, ast.Return))
    child = parent.value
    checked = []

    def check(evaluate, memo, _state):
        entries = memo["entries"]
        entries.clear()
        frames = _RetentionFrames()
        memo["traces"] = frames
        cut = frozenset({id(callback)})
        child_result = evaluate(child, cut)
        assert child_result[1] is False
        child_entry = entries[(child, False)]
        assert id(callback) in child_entry[0] and id(callback) in child_entry[1]
        before = frames.entries
        parent_result = evaluate(parent, cut)
        assert parent_result[1] is False
        # Attribute body has exactly one Name child; it must actually hit the
        # primed completed child entry, rather than replaying that child's body.
        assert frames.entries == before + 1
        parent_entry = entries[(parent, False)]
        assert id(callback) in parent_entry[0] and id(callback) in parent_entry[1]
        assert evaluate(parent, frozenset())[1] is True
        assert entries[(parent, False)] is not parent_entry
        # Immediate path-cut false cannot replace a later empty-path answer.
        assert evaluate(child, frozenset({id(child)})) == (None, False)
        assert evaluate(child, frozenset())[1] is True
        checked.append(True)

    result = _run_retention_context_probe(monkeypatch, probe, check=check)
    assert checked == [True] and any(path + ":" in failure for failure in result)
    assert _run_retention_context_probe(monkeypatch, probe, reuse=False) == result


def test_retention_context_initial_intersection_short_circuit_and_full_origin(monkeypatch):
    path = "src/elspeth/retention_context_short_probe.py"
    probe = fencing._parse_source(
        path,
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\n"
        "def held():\n    left = PromptGuardTrust.REFERENCE\n    right = 'safe'\n    return left or right\n",
    )
    held = probe.tree.body[1]
    root = held.body[-1].value
    left, right = root.values
    target = held.body[0].targets[0]
    checked = []

    def check(evaluate, memo, _state):
        entries = memo["entries"]
        entries.clear()
        first = evaluate(root, frozenset())
        assert first[1] is True
        footprint, intersection, retained_pair = entries[(root, False)]
        assert id(left) in footprint and id(right) not in footprint
        assert intersection == frozenset() and retained_pair is first
        irrelevant = frozenset({id(probe.tree)})
        frames = _RetentionFrames()
        memo["traces"] = frames
        assert evaluate(root, irrelevant) is first
        assert frames.entries == 0
        second = evaluate(root, frozenset({id(left)}))
        assert second[1] is False and frames.entries > 0
        assert id(right) in entries[(root, False)][0]
        assert evaluate(target, read_target=False) == (None, False)
        read = evaluate(target, read_target=True)
        assert read[1] is True and (target, False) in entries and (target, True) in entries
        actual_enum = held.body[0].value
        origin_pair = evaluate(actual_enum)
        assert origin_pair[0] == ("enum-value", "elspeth.contracts.prompt_guard.PromptGuardTrust")
        assert evaluate(actual_enum, irrelevant) is origin_pair
        checked.append(True)

    result = _run_retention_context_probe(monkeypatch, probe, check=check)
    assert checked == [True]
    assert _run_retention_context_probe(monkeypatch, probe, reuse=False) == result


def test_retention_context_semantic_overrides_disable_parent_reuse(monkeypatch):
    path = "src/elspeth/retention_context_override_probe.py"
    probe = fencing._parse_source(path, "def held():\n    leaf = 'safe'\n    return leaf\n")
    leaf = probe.tree.body[0].body[-1].value
    checked = []

    def lookup(value, state):
        if value is leaf and state.get("override"):
            state["override"] = False
            evaluate = state["evaluate"]
            evaluate(value, parents=dict(evaluate.__kwdefaults__["parents"]))

    def check(evaluate, memo, state):
        entries = memo["entries"]
        overrides = {
            "parents": dict(evaluate.__kwdefaults__["parents"]),
            "retained_enum_kinds": set(evaluate.__kwdefaults__["retained_enum_kinds"]),
            "module": (evaluate.__kwdefaults__["module"] + " ")[:-1],
            "unit": fencing._parse_source(path, "def held():\n    leaf = 'safe'\n    return leaf\n"),
        }
        for name, changed in overrides.items():
            entries.clear()
            assert changed is not evaluate.__kwdefaults__[name]
            assert evaluate(leaf, **{name: changed})[1] is False
            assert (leaf, False) not in entries
        entries.clear()
        state["override"] = True
        assert evaluate(leaf)[1] is False
        assert state["override"] is False and (leaf, False) not in entries
        assert memo["traces"] == []
        assert evaluate(leaf)[1] is False and (leaf, False) in entries
        checked.append(True)

    result = _run_retention_context_probe(monkeypatch, probe, check=check, lookup=lookup)
    assert checked == [True] and not any(path + ":" in failure for failure in result)


def test_retention_context_failed_body_keeps_original_error_and_no_entry(monkeypatch):
    path = "src/elspeth/retention_context_error_probe.py"
    probe = fencing._parse_source(path, "def held():\n    leaf = 'safe'\n    return leaf\n")
    leaf = probe.tree.body[0].body[-1].value
    expected = RuntimeError("original lexical dependency failure")
    failures = []

    def lookup(value, state):
        if value is leaf and state.get("fail"):
            failures.append(value)
            raise expected

    def check(evaluate, memo, state):
        entries = memo["entries"]
        entries.clear()
        state["fail"] = True
        for _ in range(2):
            with pytest.raises(RuntimeError) as caught:
                evaluate(leaf)
            assert caught.value is expected
            assert (leaf, False) not in entries and memo["traces"] == []
        state["fail"] = False
        assert evaluate(leaf)[1] is False and (leaf, False) in entries

    result = _run_retention_context_probe(monkeypatch, probe, check=check, lookup=lookup)
    assert failures == [leaf, leaf] and not any(path + ":" in failure for failure in result)


@pytest.mark.parametrize("depth", (24, 48, 80))
def test_retention_context_deep_finite_chain_matches_no_reuse(monkeypatch, depth):
    path = "src/elspeth/retention_context_deep_probe.py"
    source = "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n    value0 = 'safe'\n"
    source += "".join(f"    value{index} = value{index - 1}\n" for index in range(1, depth + 1))
    source += f"    return value{depth}\n"
    probe = fencing._parse_source(path, source)
    result = _run_retention_context_probe(monkeypatch, probe)
    assert not any(path + ":" in failure for failure in result), result
    assert _run_retention_context_probe(monkeypatch, probe, reuse=False) == result
    protected = fencing._parse_source(path, source.replace("value0 = 'safe'", "value0 = PromptGuardTrust.REFERENCE"))
    refused = _run_retention_context_probe(monkeypatch, protected)
    assert any(path + ":" in failure for failure in refused), refused
    assert _run_retention_context_probe(monkeypatch, protected, reuse=False) == refused


def test_retention_context_fresh_passes_and_actual_walker_work(monkeypatch):
    path = "src/elspeth/retention_context_work_probe.py"
    source = (
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\n"
        "def held():\n    leaf = 'safe'\n"
        "    a = (leaf, leaf)\n    b = (a, a)\n    c = (b, b)\n    d = (c, c)\n    return d\n"
    )
    probe = fencing._parse_source(path, source)
    observations = []

    def observe(memo):
        frames = _RetentionFrames()
        memo["traces"] = frames
        observations.append((memo, frames))

    normal = _run_retention_context_probe(monkeypatch, probe, observe=observe)
    no_reuse = _run_retention_context_probe(monkeypatch, probe, reuse=False, observe=observe)
    assert no_reuse == normal and not any(path + ":" in failure for failure in normal)
    cached, uncached = observations
    assert 0 < cached[1].entries < uncached[1].entries
    assert 0 < cached[1].maximum_depth <= uncached[1].maximum_depth
    nodes = set(ast.walk(probe.tree))
    for memo, frames in observations:
        assert frames == [] and memo["entries"]
        assert len(memo["entries"]) <= 2 * (len(nodes) + 1)
        for (node, read_target), (footprint, intersection, pair) in memo["entries"].items():
            assert node is None or node in nodes
            assert type(read_target) is bool and type(footprint) is frozenset and type(intersection) is frozenset
            assert intersection <= footprint and len(footprint) <= len(nodes)
            assert type(pair) is tuple and len(pair) == 2 and type(pair[1]) is bool
    # Metrics above count only genuine analysis calls: no direct check callback.
    held = probe.tree.body[1]
    leaf = held.body[0]
    old = leaf.value
    leaf.value = ast.increment_lineno(ast.parse("PromptGuardTrust.REFERENCE", mode="eval").body, old.lineno - 1)
    fencing._attach_parents(probe.tree)
    protected = _run_retention_context_probe(monkeypatch, probe, observe=observe)
    assert any(path + ":" in failure for failure in protected)
    assert _run_retention_context_probe(monkeypatch, probe, reuse=False) == protected
    leaf.value = old
    fencing._attach_parents(probe.tree)
    assert _run_retention_context_probe(monkeypatch, probe, observe=observe) == normal
    fresh = fencing._parse_source(path, source)
    assert _run_retention_context_probe(monkeypatch, fresh, observe=observe) == normal
    assert len({id(memo) for memo, _ in observations}) == len(observations)


@pytest.mark.parametrize(
    "body",
    [
        "leaf = PromptGuardTrust.REFERENCE\ndef held(leaf):\n    return leaf\nresult = [leaf for leaf in ('safe',)]\n",
        "def held():\n    leaf = PromptGuardTrust.REFERENCE\n    return leaf.value\n",
    ],
)
def test_retention_context_preserves_lexical_shadows_and_scalar_member_projection(monkeypatch, body):
    path = "src/elspeth/retention_context_scalar_probe.py"
    probe = fencing._parse_source(path, "from elspeth.contracts.prompt_guard import PromptGuardTrust\n" + body)
    result = _run_retention_context_probe(monkeypatch, probe)
    assert not any(path + ":" in failure for failure in result), result
    assert _run_retention_context_probe(monkeypatch, probe, reuse=False) == result


@pytest.mark.parametrize("depth", (128, 256))
def test_retention_context_cold_chain_is_valid_on_original_and_candidate(monkeypatch, depth):
    """Run this unchanged selector on both exact original7834 and candidate.

    It deliberately does not require the new context memo API. The first
    evaluator call probes the cold terminal Name before the ordinary walk can
    prime intermediate assignments. No recursion limit is changed.
    """
    path = "src/elspeth/retention_context_cold_depth_probe.py"
    source = "def held():\n    value0 = 'safe'\n"
    source += "".join(f"    value{index} = value{index - 1}\n" for index in range(1, depth + 1))
    source += f"    return value{depth}\n"
    probe = fencing._parse_source(path, source)
    returned = probe.tree.body[0].body[-1].value
    original_cache = fencing.cache
    checked = []

    def decorated(function):
        wrapped = original_cache(function)
        if not function.__qualname__.endswith(".retained_empty_root_state"):
            return wrapped
        evaluate = function.__kwdefaults__["evaluate"]
        if evaluate.__kwdefaults__["unit"] is not probe:
            return wrapped
        first = True

        def current(value, **kwargs):
            nonlocal first
            if first:
                first = False
                # This read is an optional instrument assertion, not an origin
                # or guard result. The genuinely original evaluator has no memo.
                memo = evaluate.__kwdefaults__.get("memo")
                if memo is not None:
                    assert memo["entries"] == {} and memo["traces"] == []
                result = evaluate(returned, frozenset())
                assert result[1] is False
                checked.append(result)
            return wrapped(value, **kwargs)

        return current

    monkeypatch.setattr(fencing, "cache", decorated)
    failures = _empty_root_complete_finite_failures(probe)
    assert len(checked) == 1 and not any(path + ":" in failure for failure in failures), failures


def test_retention_context_keeps_same_spelling_units_separate(monkeypatch):
    (supplier,) = _source_units(("src/elspeth/contracts/prompt_guard.py",))
    safe = fencing._parse_source("src/elspeth/retention_context_safe_unit.py", "def held():\n    leaf = 'safe'\n    return leaf\n")
    retained = fencing._parse_source(
        "src/elspeth/retention_context_retained_unit.py",
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n    leaf = PromptGuardTrust.REFERENCE\n    return leaf\n",
    )
    original_cache = fencing.cache
    observed = []
    reuse = True

    def decorated(function):
        wrapped = original_cache(function)
        if function.__qualname__.endswith(".retained_empty_root_state"):
            evaluate = function.__kwdefaults__["evaluate"]
            unit = evaluate.__kwdefaults__["unit"]
            if unit is safe or unit is retained:
                memo = evaluate.__kwdefaults__["memo"]
                assert memo["entries"] == {} and memo["traces"] == []
                if not reuse:
                    memo["entries"] = _RetentionNoHits()
                observed.append((unit, memo))
        return wrapped

    monkeypatch.setattr(fencing, "cache", decorated)
    arguments = {"additional_supplier_roots": frozenset({"elspeth.contracts.prompt_guard.PromptGuardTrust"})}
    normal = fencing._reserve_selected_origin_effect_failures((supplier, safe, retained), **arguments)
    assert not any(safe.path + ":" in failure for failure in normal)
    assert any(retained.path + ":" in failure for failure in normal)
    reuse = False
    assert fencing._reserve_selected_origin_effect_failures((supplier, safe, retained), **arguments) == normal
    assert len(observed) == 4 and len({id(memo) for _, memo in observed}) == 4


class _TwoWitnessPublications(dict):
    """Observe real completed publication; do not replace evaluator results."""

    def __init__(self):
        super().__init__()
        self.publications = []

    def __setitem__(self, key, value):
        self.publications.append((key, self.get(key), value))
        super().__setitem__(key, value)


def _two_witness_probe(body):
    return fencing._parse_source(
        "src/elspeth/two_witness_probe.py",
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n" + body,
    )


def _run_two_witness_policy(monkeypatch, probe, policy, *, check=None, lookup=None, observations=None):
    def observe(memo):
        assert memo["previous_entries"] == {}
        if policy == "one":
            memo["previous_entries"] = _RetentionNoHits()
        frames = _RetentionFrames()
        memo["traces"] = frames
        if observations is not None:
            observations.append((memo, frames))

    assert policy in {"two", "one", "none"}
    return _run_retention_context_probe(monkeypatch, probe, reuse=policy != "none", observe=observe, check=check, lookup=lookup)


@pytest.mark.parametrize("first_cut", (False, True))
def test_two_witness_complete_pair_reuse_and_no_promotion(monkeypatch, first_cut):
    probe = _two_witness_probe("    leaf = PromptGuardTrust.REFERENCE\n    return leaf\n")
    value = probe.tree.body[1].body[0].value
    checked = []

    def check(evaluate, memo, _state):
        entries, previous = memo["entries"], memo["previous_entries"]
        entries.clear()
        previous.clear()
        frames = memo["traces"]
        key = (value, False)
        empty, cut = frozenset(), frozenset({id(value)})
        a, b = (cut, empty) if first_cut else (empty, cut)
        first = evaluate(value, a)
        first_entry = entries[key]
        second = evaluate(value, b)
        primary, secondary = entries[key], previous[key]
        assert first[1] is not second[1]
        retained = second if first_cut else first
        assert retained[0] == ("enum-value", "elspeth.contracts.prompt_guard.PromptGuardTrust")
        assert secondary[0] is primary and secondary[1] is first_entry
        before = frames.entries
        assert evaluate(value, a | {id(probe.tree)}) is first
        assert frames.entries == before  # Actual A/B/A secondary hit.
        assert entries[key] is primary and previous[key] is secondary
        assert evaluate(value, b) is second  # Original primary still wins.
        assert frames.entries == before
        # Every saved witness is a complete tuple, never an association chain.
        assert len(primary) == len(secondary[1]) == 3
        assert type(secondary[1][0]) is frozenset and type(secondary[1][1]) is frozenset
        checked.append(True)

    result = _run_two_witness_policy(monkeypatch, probe, "two", check=check)
    assert checked == [True] and any(probe.path + ":" in failure for failure in result)
    assert _run_two_witness_policy(monkeypatch, probe, "one") == result
    assert _run_two_witness_policy(monkeypatch, probe, "none") == result


def test_two_witness_incompatible_context_and_bounded_displacement(monkeypatch):
    probe = _two_witness_probe("    left = PromptGuardTrust.REFERENCE\n    right = 'safe'\n    return left or right\n")
    root = probe.tree.body[1].body[-1].value
    left, right = root.values
    checked = []

    def check(evaluate, memo, _state):
        entries, previous = memo["entries"], memo["previous_entries"]
        entries.clear()
        previous.clear()
        frames = memo["traces"]
        key = (root, False)
        a = evaluate(root)
        a_entry = entries[key]
        assert a[1] is True and id(right) not in a_entry[0]
        b = evaluate(root, frozenset({id(root)}))
        b_entry = entries[key]
        assert b == (None, False) and previous[key][1] is a_entry
        before = frames.entries
        c = evaluate(root, frozenset({id(left)}))
        c_entry = entries[key]
        assert c[1] is False and frames.entries > before
        assert id(right) in c_entry[0]
        assert previous[key][0] is c_entry and previous[key][1] is b_entry
        before = frames.entries
        assert evaluate(root, frozenset({id(root)})) is b
        assert frames.entries == before and entries[key] is c_entry
        assert previous[key][1] is b_entry
        assert evaluate(root)[1] is True and frames.entries > before  # A was displaced.
        assert previous[key][1] is c_entry
        assert all(len(witness) == 3 for _, witness in previous.values())
        assert len(previous) <= len(entries)
        checked.append(True)

    result = _run_two_witness_policy(monkeypatch, probe, "two", check=check)
    assert checked == [True]
    assert _run_two_witness_policy(monkeypatch, probe, "one") == result
    assert _run_two_witness_policy(monkeypatch, probe, "none") == result


def test_two_witness_parent_merges_secondary_callable_footprint(monkeypatch):
    probe = _two_witness_probe(
        "    leaf = PromptGuardTrust.REFERENCE\n    def callback():\n        return leaf\n    return callback.value\n"
    )
    held = probe.tree.body[1]
    callback = held.body[1]
    parent = held.body[-1].value
    child = parent.value
    checked = []

    def check(evaluate, memo, _state):
        entries, previous = memo["entries"], memo["previous_entries"]
        entries.clear()
        previous.clear()
        frames = memo["traces"]
        cut = frozenset({id(callback)})
        first = evaluate(child, cut)
        saved = entries[(child, False)]
        assert first[1] is False and id(callback) in saved[0] and id(callback) in saved[1]
        assert evaluate(child)[1] is True
        assert previous[(child, False)][1] is saved
        before = frames.entries
        assert evaluate(parent, cut)[1] is False
        assert frames.entries == before + 1  # Parent body, child secondary hit.
        footprint, intersection, _ = entries[(parent, False)]
        assert id(callback) in footprint and id(callback) in intersection
        assert evaluate(parent)[1] is True
        checked.append(True)

    result = _run_two_witness_policy(monkeypatch, probe, "two", check=check)
    assert checked == [True]
    assert _run_two_witness_policy(monkeypatch, probe, "none") == result


def test_two_witness_same_key_recursion_displaces_latest_completed_publication(monkeypatch):
    probe = _two_witness_probe("    a = b\n    b = a\n    return a\n")
    root = probe.tree.body[1].body[0].value
    checked = []

    def check(evaluate, memo, _state):
        entries = _TwoWitnessPublications()
        memo["entries"] = entries
        memo["previous_entries"].clear()
        assert evaluate(root)[1] is False
        publications = [row for row in entries.publications if row[0] == (root, False)]
        assert len(publications) >= 2
        last, nested = publications[-1], publications[-2]
        assert last[1] is nested[2]
        assert (root, False) in memo["previous_entries"]
        anchor, displaced = memo["previous_entries"][(root, False)]
        assert anchor is last[2] is entries[(root, False)]
        assert displaced is nested[2]
        assert displaced[0] == displaced[1] == frozenset({id(root)})
        assert displaced[2] == (None, False)
        before = memo["traces"].entries
        assert evaluate(root, frozenset({id(root)})) is displaced[2]
        assert memo["traces"].entries == before
        checked.append(True)

    result = _run_two_witness_policy(monkeypatch, probe, "two", check=check)
    assert checked == [True] and not any(probe.path + ":" in failure for failure in result)
    assert _run_two_witness_policy(monkeypatch, probe, "none") == result


@pytest.mark.parametrize("interference", ("clear", "replace"))
def test_two_witness_primary_anchor_refuses_stale_secondary(monkeypatch, interference):
    probe = _two_witness_probe("    leaf = PromptGuardTrust.REFERENCE\n    return leaf\n")
    value = probe.tree.body[1].body[0].value
    checked = []

    def check(evaluate, memo, _state):
        entries, previous = memo["entries"], memo["previous_entries"]
        entries.clear()
        previous.clear()
        assert evaluate(value)[1] is True
        assert evaluate(value, frozenset({id(value)}))[1] is False
        key = (value, False)
        anchor, old = previous[key]
        assert old[2][1] is True
        if interference == "clear":
            entries.pop(key)
        else:
            replacement = (anchor[0], anchor[1], anchor[2])
            assert replacement == anchor and replacement is not anchor
            entries[key] = replacement
        before = memo["traces"].entries
        assert evaluate(value)[1] is True
        assert memo["traces"].entries > before
        if interference == "clear":
            assert key not in previous
        else:
            assert previous[key][1] is replacement
        checked.append(True)

    result = _run_two_witness_policy(monkeypatch, probe, "two", check=check)
    assert checked == [True]
    assert _run_two_witness_policy(monkeypatch, probe, "one") == result


def test_two_witness_store_read_target_slots_are_separate(monkeypatch):
    probe = _two_witness_probe("    leaf = PromptGuardTrust.REFERENCE\n    leaf += ()\n")
    target = probe.tree.body[1].body[0].targets[0]
    checked = []

    def check(evaluate, memo, _state):
        entries, previous = memo["entries"], memo["previous_entries"]
        entries.clear()
        previous.clear()
        for read_target in (True, False):
            first = evaluate(target, read_target=read_target)
            assert first[1] is read_target
            evaluate(target, frozenset({id(target)}), read_target=read_target)
            assert (target, read_target) in previous
        before = memo["traces"].entries
        assert evaluate(target, read_target=True)[1] is True
        assert evaluate(target, read_target=False)[1] is False
        assert memo["traces"].entries == before
        assert {key for key in previous if key[0] is target} == {(target, False), (target, True)}
        checked.append(True)

    result = _run_two_witness_policy(monkeypatch, probe, "two", check=check)
    assert checked == [True]
    assert _run_two_witness_policy(monkeypatch, probe, "none") == result


def test_two_witness_error_and_override_leave_existing_completed_witnesses(monkeypatch):
    probe = _two_witness_probe("    left = PromptGuardTrust.REFERENCE\n    right = 'safe'\n    return left or right\n")
    root = probe.tree.body[1].body[-1].value
    left, right = root.values
    expected = RuntimeError("two-witness actual lexical dependency failure")
    checked = []

    def lookup(value, state):
        if value is right and state.get("fail"):
            raise expected
        if value is right and state.get("override"):
            state["override"] = False
            evaluate = state["evaluate"]
            evaluate(value, parents=dict(evaluate.__kwdefaults__["parents"]))

    def check(evaluate, memo, state):
        entries, previous = memo["entries"], memo["previous_entries"]
        entries.clear()
        previous.clear()
        assert evaluate(root)[1] is True
        assert evaluate(root, frozenset({id(root)}))[1] is False
        key = (root, False)
        primary, secondary = entries[key], previous[key]
        state["fail"] = True
        with pytest.raises(RuntimeError) as caught:
            evaluate(root, frozenset({id(left)}))
        assert caught.value is expected
        assert entries[key] is primary and previous[key] is secondary and memo["traces"] == []
        # A successfully completed child need not be rolled back with its parent.
        assert (left, False) in entries
        state["fail"] = False
        state["override"] = True
        assert evaluate(root, frozenset({id(left)}))[1] is False
        assert not state["override"]
        assert entries[key] is primary and previous[key] is secondary and memo["traces"] == []
        # Direct semantic overrides must bypass a would-be secondary hit too.
        changed = dict(evaluate.__kwdefaults__["parents"])
        before = memo["traces"].entries
        assert evaluate(root, parents=changed)[1] is True
        assert memo["traces"].entries > before
        assert entries[key] is primary and previous[key] is secondary
        checked.append(True)

    result = _run_two_witness_policy(monkeypatch, probe, "two", check=check, lookup=lookup)
    assert checked == [True]
    assert _run_two_witness_policy(monkeypatch, probe, "one") == result


@pytest.mark.parametrize(
    "body",
    (
        "    a = (leaf, leaf)\n    b = (a, a)\n    return b\n",
        "    a = ()\n    b = a\n    a = (b, leaf)\n    return a\n",
        "    def child(value=leaf):\n        return value\n    return child\n",
        "    def child(*, value=leaf):\n        return value\n    return child\n",
        "    return lambda: leaf\n",
        "    def child():\n        nonlocal leaf\n        return leaf\n    return child\n",
        "    result=[]\n    result.extend([leaf for ignored in (0,)])\n    return result\n",
        "    opaque_callback(value=[leaf for ignored in (0,)])\n",
        "    return (leaf for ignored in (0,))\n",
        "    return leaf if True else None\n",
        "    return leaf.value\n",
    ),
)
def test_two_witness_complete_api_matches_one_slot_and_no_reuse(monkeypatch, body):
    scalar = _two_witness_probe("    leaf = 'safe'\n" + body)
    retained = _two_witness_probe("    leaf = PromptGuardTrust.REFERENCE\n" + body)
    positive = _run_two_witness_policy(monkeypatch, scalar, "two")
    assert not any(scalar.path + ":" in failure for failure in positive)
    protected = _run_two_witness_policy(monkeypatch, retained, "two")
    if body != "    return leaf.value\n":
        assert any(retained.path + ":" in failure for failure in protected)
    else:
        assert not any(retained.path + ":" in failure for failure in protected)
    for policy in ("one", "none"):
        assert _run_two_witness_policy(monkeypatch, scalar, policy) == positive
        assert _run_two_witness_policy(monkeypatch, retained, policy) == protected


def test_two_witness_actual_walk_storage_and_fresh_passes(monkeypatch, record_property):
    probe = _two_witness_probe("    leaf = 'safe'\n    a = b\n    b = (a, leaf)\n    return b\n")
    observed = []
    two = _run_two_witness_policy(monkeypatch, probe, "two", observations=observed)
    one = _run_two_witness_policy(monkeypatch, probe, "one", observations=observed)
    none = _run_two_witness_policy(monkeypatch, probe, "none", observations=observed)
    assert two == one == none and not any(probe.path + ":" in failure for failure in two)
    # Only genuine walker calls above: no check callback or direct evaluator probes.
    nodes = set(ast.walk(probe.tree))
    for memo, frames in observed:
        assert frames == [] and frames.entries > 0 and frames.maximum_depth > 0
        assert len(memo["previous_entries"]) <= len(memo["entries"]) <= 2 * (len(nodes) + 1)
        for (node, read_target), (anchor, displaced) in memo["previous_entries"].items():
            assert node is None or node in nodes
            assert type(read_target) is bool and anchor is memo["entries"][(node, read_target)]
            for footprint, intersection, pair in (anchor, displaced):
                assert type(footprint) is frozenset and type(intersection) is frozenset
                assert intersection <= footprint and len(footprint) <= len(nodes)
                assert type(pair) is tuple and len(pair) == 2 and type(pair[1]) is bool
    assert observed[0][0]["previous_entries"]  # Real recursion published a displaced witness.
    # Record only the finished real analysis, before any mutation or direct probe.
    for policy, (memo, frames) in zip(("two", "one", "none"), observed, strict=True):
        record_property(policy + "_body_evaluations", frames.entries)
        record_property(policy + "_maximum_trace_depth", frames.maximum_depth)
        record_property(policy + "_primary_keys", len(memo["entries"]))
        record_property(policy + "_secondary_keys", len(memo["previous_entries"]))
        witnesses = tuple(memo["entries"].values()) + tuple(item[1] for item in memo["previous_entries"].values())
        record_property(policy + "_footprint_members", sum(len(item[0]) for item in witnesses))
        record_property(policy + "_intersection_members", sum(len(item[1]) for item in witnesses))
    held = probe.tree.body[1]
    original = held.body[0].value
    held.body[0].value = ast.increment_lineno(ast.parse("PromptGuardTrust.REFERENCE", mode="eval").body, original.lineno - 1)
    fencing._attach_parents(probe.tree)
    protected = _run_two_witness_policy(monkeypatch, probe, "two", observations=observed)
    assert any(probe.path + ":" in failure for failure in protected)
    assert _run_two_witness_policy(monkeypatch, probe, "one") == protected
    held.body[0].value = original
    fencing._attach_parents(probe.tree)
    assert _run_two_witness_policy(monkeypatch, probe, "two", observations=observed) == two
    fresh = _two_witness_probe("    leaf = 'safe'\n    a = b\n    b = (a, leaf)\n    return b\n")
    assert _run_two_witness_policy(monkeypatch, fresh, "two", observations=observed) == two
    assert len({id(memo) for memo, _ in observed}) == len(observed)
