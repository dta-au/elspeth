"""Fast prerequisites and adversarial controls for the expensive reserve proof."""

import ast
from pathlib import Path

import pytest
from tests.helpers import web_reserve_compose_proposal_transfers as compose
from tests.helpers import web_reserve_route_helper as route
from tests.helpers import web_reserve_terminal_sql_transfers as terminal
from tests.unit.architecture import test_web_landscape_mutation_fencing as fencing

ROOT = Path(__file__).parents[3]
TURN = "src/elspeth/web/sessions/composer_turn.py"
SERVICE = "src/elspeth/web/sessions/service.py"
PROTOCOL = "src/elspeth/web/sessions/protocol.py"


def _source_units(paths: tuple[str, ...]) -> tuple[fencing.SourceUnit, ...]:
    return tuple(fencing._parse_source(path, (ROOT / path).read_text()) for path in paths)


@pytest.mark.parametrize("path", (TURN, SERVICE, PROTOCOL))
def test_reviewed_source_guard_matches_live_source_and_refuses_changed_bytes(path: str) -> None:
    paths = (*fencing._reserve_FULL_SOURCE_PATHS, *fencing._reserve_CANCEL_CONSUMERS)
    sources = {item: (ROOT / item).read_text() for item in paths}
    if path == TURN:
        check = fencing._reserve_cancel_consumer_failures
        expected = "owned cancellation consumer changed " + path
    else:
        check = fencing._reserve_full_source_failures
        expected = "full source changed " + path
    assert check(sources) == []
    sources[path] += "\n# controlled unreviewed source change\n"
    assert check(sources) == [expected]


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


def test_reviewed_terminal_namespace_matches_live_source_and_refuses_new_descriptor() -> None:
    units = _source_units((SERVICE, terminal.REQUIRED, terminal.WORKERS, terminal.AUTHORITY))
    failures, _ = terminal.terminal_sql_local_contracts(units)
    assert failures == []
    owner, _ = terminal.find(units[0].tree, terminal.CLASS)
    owner.body.append(ast.parse("def reserve(self, request):\n    return request\n").body[0])
    ast.fix_missing_locations(units[0].tree)
    failures, _ = terminal.terminal_sql_local_contracts(units)
    assert "terminal SQL transfer: finite current service declaration namespace changed" in failures


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
