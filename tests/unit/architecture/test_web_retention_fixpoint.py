"""Adversarial retention probes for the denial-only reserve proof.

The production change that these probes detect is replacing the path-sensitive
DFS with a static graph. Deep and cyclic dependencies must terminate without
changing Python's recursion limit or weakening any supplier denial.
"""

from __future__ import annotations

import ast

import pytest
from tests.helpers.web_reserve_retention import RetentionGraph, RetentionState, consumer_combinations
from tests.unit.architecture import test_web_landscape_mutation_fencing as fencing


def _probe(source):
    return fencing._parse_source("src/elspeth/retention_fixpoint_probe.py", source)


def _run(probe):
    supplier_path = "src/elspeth/contracts/prompt_guard.py"
    supplier = fencing._parse_source(supplier_path, (fencing._repo_root() / supplier_path).read_text())
    return fencing._reserve_selected_origin_effect_failures(
        (supplier, probe),
        additional_supplier_roots=frozenset({"elspeth.contracts.prompt_guard.PromptGuardTrust"}),
    )


def _during_first_root(monkeypatch, probe, check, *, static_origins=None):
    original_cache = fencing.cache

    def decorate(function):
        wrapped = original_cache(function)
        if function.__qualname__.endswith(".scoped_expression") and function.__defaults__[2] is probe:

            def static(value, use, *args, **kwargs):
                if static_origins is not None and value in static_origins:
                    return static_origins[value]
                return wrapped(value, use, *args, **kwargs)

            return static
        if not function.__qualname__.endswith(".retained_empty_root_state"):
            return wrapped
        evaluate = function.__kwdefaults__["evaluate"]
        if evaluate.__kwdefaults__["unit"] is not probe:
            return wrapped
        first = True

        def query(value, **kwargs):
            nonlocal first
            if first:
                first = False
                check(evaluate)
            return wrapped(value, **kwargs)

        return query

    monkeypatch.setattr(fencing, "cache", decorate)


def _scope_memos(monkeypatch):
    original_cache = fencing.cache
    memos = []

    def decorate(function):
        wrapped = original_cache(function)
        if function.__qualname__.endswith(".retained_read_scopes"):
            memos.append((function.__kwdefaults__["parents"], wrapped))
        return wrapped

    monkeypatch.setattr(fencing, "cache", decorate)
    return memos


@pytest.mark.parametrize(
    "site,bound",
    (
        ("body", "field"),
        ("default", "field"),
        ("lambda-body", "field"),
        ("lambda-default", "field"),
        ("first-iterable", "field"),
        ("comprehension-body", "field"),
        ("later-iterable", "field"),
        ("comprehension-body", "absent"),
        ("module", "field"),
    ),
)
def test_scope_memo_preserves_exact_node_bound_scopes_and_repeated_reads(monkeypatch, site, bound):
    probe = _probe(
        "def outer():\n    field = 'safe'\n    def inner(arg=field):\n"
        "        body = field\n        closure = lambda value=field: field\n"
        "        first = [field for field in field]\n"
        "        later = [field for field in source for unused in field]\n"
        "        return body\n    return inner\nresult = field\n"
    )
    outer = probe.tree.body[0]
    inner = outer.body[1]
    closure = inner.body[1].value
    first, later = inner.body[2].value, inner.body[3].value
    sites = {
        "body": (inner.body[0].value, (inner, outer)),
        "default": (inner.args.defaults[0], (outer,)),
        "lambda-body": (closure.body, (closure, inner, outer)),
        "lambda-default": (closure.args.defaults[0], (inner, outer)),
        "first-iterable": (first.generators[0].iter, (inner, outer)),
        "comprehension-body": (first.elt, (first,) if bound == "field" else (inner, outer)),
        "later-iterable": (later.generators[1].iter, (later,)),
        "module": (probe.tree.body[1].value, ()),
    }
    memos = _scope_memos(monkeypatch)
    other = fencing._parse_source("src/elspeth/scope_memo_later_unit.py", "def later(field):\n    return field\n")
    fencing._reserve_selected_origin_effect_failures((probe, other))
    (parents, memo) = next((parents, memo) for parents, memo in memos if id(outer) in parents)
    memo.cache_clear()
    body = sites["body"][0]
    assert memo(body, "field") == (inner, outer)
    value, expected = sites[site]
    keys = {(body, "field"), (value, bound)}
    if bound == "absent":
        assert memo(value, "field") == (first,)
        keys.add((value, "field"))
    actual = memo(value, bound)
    assert isinstance(actual, tuple) and actual == expected
    assert actual == tuple(fencing._reserve_dataclass_field_read_scopes(value, bound, parents))
    assert memo(value, bound) is actual
    assert memo.cache_info().misses == memo.cache_info().currsize == len(keys)
    assert memo.cache_info().hits >= 1


def test_scope_memo_never_publishes_a_partially_consumed_generator(monkeypatch):
    probe = _probe("def held():\n    return value\n")
    value = probe.tree.body[0].body[0].value
    memos = _scope_memos(monkeypatch)
    _run(probe)
    (_parents, memo) = next((parents, memo) for parents, memo in memos if id(value) in parents)
    memo.cache_clear()
    original = fencing._reserve_dataclass_field_read_scopes
    error = RuntimeError("controlled incomplete lexical scope walk")

    def incomplete(subject, bound, parents):
        if subject is value:
            yield probe.tree.body[0]
            raise error
        yield from original(subject, bound, parents)

    monkeypatch.setattr(fencing, "_reserve_dataclass_field_read_scopes", incomplete)
    for _attempt in range(2):
        with pytest.raises(RuntimeError) as caught:
            memo(value, value.id)
        assert caught.value is error
    assert memo.cache_info().currsize == memo.cache_info().hits == 0
    assert memo.cache_info().misses == 2


@pytest.mark.parametrize("poison_request", (1, 2), ids=("iteration-index", "destructure-index"))
def test_scope_memo_leaves_first_scope_preparation_lazy(monkeypatch, poison_request):
    probe = _probe("def held():\n    left, right = ('safe', 'safe')\n    return left\n")
    target = probe.tree.body[0].body[0].targets[0].elts[0]
    original = fencing._reserve_dataclass_field_read_scopes
    requested, resumed = [], []

    def checked(subject, bound, parents):
        if subject is target and bound == target.id:
            requested.append(True)
            if len(requested) == poison_request:
                yield next(original(subject, bound, parents))
                resumed.append(True)
                raise AssertionError("a first-scope consumer drained the remaining generator")
        yield from original(subject, bound, parents)

    monkeypatch.setattr(fencing, "_reserve_dataclass_field_read_scopes", checked)
    _run(probe)
    assert len(requested) >= poison_request and resumed == []


@pytest.mark.parametrize("leaf", ("'safe'", "PromptGuardTrust.REFERENCE"))
def test_scope_memo_reuses_real_walker_requests_without_changing_failure_rows(monkeypatch, leaf):
    probe = _probe(
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n"
        f"    left, right = ({leaf}, {leaf})\n"
        "    for value in (left, right):\n        kept = value\n    return kept\n"
    )
    original_cache = fencing.cache
    with monkeypatch.context() as patch:
        memos = _scope_memos(patch)
        actual = _run(probe)
        (memo,) = [memo for parents, memo in memos if id(probe.tree.body[0]) in parents]
        assert memo.cache_info().hits > 0 and memo.cache_info().misses > 0

    def uncached_scopes(function):
        return function if function.__qualname__.endswith(".retained_read_scopes") else original_cache(function)

    monkeypatch.setattr(fencing, "cache", uncached_scopes)
    assert _run(probe) == actual
    assert any(probe.path + ":" in row for row in actual) is (leaf != "'safe'")


def test_scope_memo_rebuilds_after_same_ast_name_moves_between_body_and_default(monkeypatch):
    probe = _probe("def outer():\n    value = 'safe'\n    def inner(arg=None):\n        return value\n    return inner\n")
    source = probe.source
    outer, inner = probe.tree.body[0], probe.tree.body[0].body[1]
    returned = inner.body[0]
    value, default = returned.value, inner.args.defaults[0]
    memos = _scope_memos(monkeypatch)
    observed = []
    for in_default in (False, True, False):
        returned.value = ast.copy_location(ast.Constant(value=None), value) if in_default else value
        inner.args.defaults[0] = value if in_default else default
        fencing._attach_parents(probe.tree)
        _run(probe)
        (parents, memo) = [(parents, memo) for parents, memo in memos if id(outer) in parents][-1]
        scopes = memo(value, value.id)
        assert scopes == tuple(fencing._reserve_dataclass_field_read_scopes(value, value.id, parents))
        observed.append((memo, scopes))
    assert [scopes for _memo, scopes in observed] == [(inner, outer), (outer,), (inner, outer)]
    assert len({id(memo) for memo, _scopes in observed}) == 3
    assert probe.source == source


@pytest.mark.parametrize(
    "kind,cyclic,retained", (("enum-value", False, True), ("enum-value", True, True), ("supplier-scalar", False, False))
)
def test_call_static_origin_override_includes_unknown_callee_and_scalar_early_return(monkeypatch, kind, cyclic, retained):
    probe = _probe(
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n"
        + ("    opaque = opaque\n" if cyclic else "")
        + "    return opaque(PromptGuardTrust.REFERENCE)\n"
    )
    call = probe.tree.body[1].body[-1].value
    origin = (kind, "probe.checked_call")
    checked = []

    def check(evaluate):
        result = evaluate(call)
        assert result.acyclic is not cyclic and result.origins == frozenset({origin})
        assert result.retained is retained
        checked.append(True)

    # Existing proof tables register origins on Call nodes. None == None is
    # intentional: the Call's own non-None static origin enables the override.
    _during_first_root(monkeypatch, probe, check, static_origins={call: origin, call.func: None})
    _run(probe)
    assert checked == [True]


@pytest.mark.parametrize(
    "body",
    (
        "    return opaque(value=PromptGuardTrust.REFERENCE)\n",
        "    return lambda: PromptGuardTrust.REFERENCE\n",
        "    class Child:\n        member = PromptGuardTrust.REFERENCE\n    return Child\n",
        "    def child(*, value=PromptGuardTrust.REFERENCE):\n        return value\n    return child\n",
        "    match PromptGuardTrust.REFERENCE:\n        case value:\n            return value\n",
        "    with opaque(PromptGuardTrust.REFERENCE) as value:\n        return value\n",
    ),
    ids=("call-keyword", "lambda-body", "class-generic", "paired-default", "match-subject", "with-source"),
)
def test_static_dependency_classes_preserve_retention(monkeypatch, body):
    probe = _probe("from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n" + body)
    returned = [node for node in ast.walk(probe.tree.body[1]) if isinstance(node, ast.Return)][-1].value
    checked = []

    def check(evaluate):
        # Dependency knockouts must not be hidden by a default/capture result
        # which the ordinary scanner happened to finalize earlier.
        graph = evaluate.__kwdefaults__["graph"]
        graph.final.clear()
        graph.edges.clear()
        result = evaluate(returned)
        assert result.retained
        checked.append(True)

    _during_first_root(monkeypatch, probe, check)
    failures = _run(probe)
    assert checked == [True]
    assert any(probe.path + ":" in failure for failure in failures), failures


def test_cyclic_start_value_includes_unknown_cut_origin():
    held = ("enum-value", "probe.Enum")
    graph = RetentionGraph(
        lambda _key: (0,),
        lambda _key, _states, acyclic: RetentionState(frozenset({held}), True, acyclic),
    )
    result = graph.evaluate(0, lambda: None)
    assert result.origins == frozenset({None, held}) and result.retained and not result.acyclic


@pytest.mark.parametrize("known_static", (False, True))
def test_formal_default_is_paired_and_overrides_only_unknown_static_origin(monkeypatch, known_static):
    probe = _probe(
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\n"
        "def held(other='safe', value=PromptGuardTrust.REFERENCE):\n    return value\n"
    )
    value = probe.tree.body[1].body[0].value
    static = ("object", "probe.nominal") if known_static else ("unknown", "value")
    expected = static if known_static else ("enum-value", "elspeth.contracts.prompt_guard.PromptGuardTrust")
    checked = []

    def check(evaluate):
        result = evaluate(value)
        assert result.acyclic and result.origins == frozenset({expected}) and result.retained
        checked.append(True)

    _during_first_root(monkeypatch, probe, check, static_origins={value: static})
    _run(probe)
    assert checked == [True]


def test_unpaired_retained_default_does_not_retain_safe_formal(monkeypatch):
    probe = _probe(
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\n"
        "def held(other=PromptGuardTrust.REFERENCE, value='safe'):\n    return value\n"
    )
    value = probe.tree.body[1].body[0].value
    checked = []

    def check(evaluate):
        result = evaluate(value)
        assert result.acyclic and result.origins == frozenset({None}) and not result.retained
        checked.append(True)

    _during_first_root(monkeypatch, probe, check)
    _run(probe)
    assert checked == [True]


@pytest.mark.parametrize("parameters", ("value", "*, value", "*value", "**value"))
def test_formal_without_paired_default_keeps_unknown_static_origin(monkeypatch, parameters):
    probe = _probe(f"def held({parameters}):\n    return value\n")
    value = probe.tree.body[0].body[0].value
    checked = []

    def check(evaluate):
        result = evaluate(value)
        assert result.acyclic and result.origins == frozenset({("unknown", "value")})
        assert result.retained is False
        checked.append(True)

    _during_first_root(monkeypatch, probe, check)
    failures = _run(probe)
    assert checked == [True]
    assert not any(probe.path + ":" in failure for failure in failures), failures


def test_isinstance_scalar_suppression_reads_only_final_callee(monkeypatch):
    probe = _probe(
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\n"
        "def held(carrier):\n    value = PromptGuardTrust.REFERENCE\n"
        "    value = opaque(value)\n    carrier.result = isinstance(value, PromptGuardTrust)\n"
    )
    call = probe.tree.body[1].body[-1].value
    checked = []

    def check(evaluate):
        result = evaluate(call)
        assert not result.acyclic and not result.retained
        assert ("supplier-scalar", "builtins.isinstance") in result.origins
        checked.append(True)

    _during_first_root(monkeypatch, probe, check)
    failures = _run(probe)
    assert checked == [True]
    assert "retained supplier stored in unclosed carrier " + probe.path + ":5" not in failures


@pytest.mark.parametrize("cyclic", (False, True))
def test_disagreeing_name_producers_collapse_only_when_acyclic(monkeypatch, cyclic):
    probe = _probe(
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n"
        "    value = PromptGuardTrust.REFERENCE\n"
        + ("    value = opaque(value)\n" if cyclic else "    value = None\n")
        + "    return value\n"
    )
    returned = probe.tree.body[1].body[-1].value
    checked = []

    def check(evaluate):
        result = evaluate(returned)
        assert result.acyclic is not cyclic and result.retained
        if cyclic:
            assert ("enum-value", "elspeth.contracts.prompt_guard.PromptGuardTrust") in result.origins
            assert None in result.origins
        else:
            assert result.origins == frozenset({None})
        checked.append(True)

    _during_first_root(monkeypatch, probe, check)
    assert any(probe.path + ":" in row for row in _run(probe))
    assert checked == [True]


def test_cyclic_non_enum_supplier_origins_reach_denial_consumers(monkeypatch):
    probe = _probe(
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n"
        "    value = PromptGuardTrust.REFERENCE.__class__.__dict__\n"
        "    value = opaque(value, PromptGuardTrust.REFERENCE)\n    return value\n"
    )
    returned = probe.tree.body[1].body[-1].value
    checked = []

    def check(evaluate):
        result = evaluate(returned)
        assert not result.acyclic and result.retained
        assert any(origin is not None and origin[0] == "retained-supplier-namespace" for origin in result.origins)
        checked.append(True)

    _during_first_root(monkeypatch, probe, check)
    failures = _run(probe)
    assert checked == [True]
    assert "retained supplier metadata escaped " + probe.path + ":5" in failures


def test_downstream_name_keeps_dfs_disagreement_none_between_non_enum_origins(monkeypatch):
    probe = _probe(
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n"
        "    cycle = cycle\n"
        "    value = getattr(cycle, '__annotations__', PromptGuardTrust.REFERENCE)\n"
        "    value = vars(PromptGuardTrust.REFERENCE)\n    return value\n"
    )
    returned = probe.tree.body[1].body[-1].value
    checked = []

    def check(evaluate):
        reference = evaluate.__kwdefaults__["reference"]
        assert reference(returned) == (None, True)
        result = evaluate(returned)
        assert not result.acyclic and result.retained and None in result.origins
        assert {origin[0] for origin in result.origins if origin is not None} == {"type-metadata", "retained-supplier-namespace"}
        checked.append(True)

    _during_first_root(monkeypatch, probe, check)
    failures = _run(probe)
    assert checked == [True]
    assert "retained enum producer return is unclosed " + probe.path + ":6" in failures


@pytest.mark.parametrize("retained", (False, True))
def test_cold_retention_chain_does_not_use_python_recursion(monkeypatch, retained):
    """A cold 1,200-edge query finishes without path enumeration or recursion.

    Stop immediately after the cold query: this measures retention evaluation,
    independently of the older direct-origin resolver's deep-alias behavior.
    Complete scanner denials are covered by the finite semantic matrix.
    """
    source = "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n"
    source += "    value0 = " + ("PromptGuardTrust.REFERENCE" if retained else "'safe'") + "\n"
    source += "".join(f"    value{index} = value{index - 1}\n" for index in range(1, 1201))
    source += "    return value1200\n"
    probe = _probe(source)
    returned = probe.tree.body[1].body[-1].value
    checked = []

    class Complete(Exception):
        pass

    def check(evaluate):
        checked.append(evaluate(returned)[1])
        raise Complete

    _during_first_root(monkeypatch, probe, check)
    with pytest.raises(Complete):
        _run(probe)
    assert checked == [retained]


@pytest.mark.parametrize("read_target", (False, True))
def test_store_name_has_no_producer_edge_unless_read_as_target(monkeypatch, read_target):
    probe = _probe(
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n    leaf = PromptGuardTrust.REFERENCE\n    leaf += ()\n"
    )
    target = probe.tree.body[1].body[0].targets[0]
    assert isinstance(target.ctx, ast.Store)
    checked = []

    def check(evaluate):
        checked.append(evaluate(target, read_target=read_target)[1])

    _during_first_root(monkeypatch, probe, check)
    _run(probe)
    assert checked == [read_target]


@pytest.mark.parametrize("reverse", (False, True))
@pytest.mark.parametrize("cyclic", (False, True))
def test_graph_handles_deep_diamonds_and_large_cycles_without_recursion(reverse, cyclic):
    """A retained input must reach every dependent in finite counted work."""
    depth = 12_000
    held = ("enum-value", "probe.Enum")

    def dependencies(key):
        if key == 0:
            return (depth,) if cyclic else ()
        return (key - 1, max(0, key - 2))

    def transfer(key, states, acyclic):
        inputs = [states[child] for child in dependencies(key)]
        origins = frozenset({held}) if key == 0 else frozenset(element for state in inputs for element in state.origins)
        retained = key == 0 or any(state.retained for state in inputs)
        return RetentionState(origins, retained, acyclic)

    graph = RetentionGraph(dependencies, transfer, reverse=reverse)
    result = graph.evaluate(depth, lambda: None)
    assert result.retained is True and held in result.origins
    assert result.acyclic is not cyclic
    assert len(graph.final) == depth + 1
    assert graph.evaluations <= 4 * (depth + 1)
    before = graph.evaluations
    assert graph.evaluate(depth, lambda: None) is result
    assert graph.evaluations == before


def test_budget_trip_abandons_whole_scc_and_next_root_rederives():
    held = ("enum-value", "probe.Enum")
    graph = RetentionGraph(
        lambda key: (1 - key,) if key in {0, 1} else (0,),
        lambda key, states, acyclic: RetentionState(
            states[1 - key].origins | ({held} if key == 0 else set()),
            key == 0 or states[1 - key].retained,
            acyclic,
        ),
    )
    attempts = 0

    def charge():
        nonlocal attempts
        attempts += 1
        if attempts > 2:
            raise fencing._RetentionBudgetExceeded

    with pytest.raises(fencing._RetentionBudgetExceeded):
        graph.evaluate(0, charge)
    assert graph.final == {}, "no tentative member may become a reusable final value"
    before = graph.evaluations
    result = graph.evaluate(1, lambda: None)
    assert result.retained and held in result.origins
    assert graph.evaluations > before
    assert set(graph.final) == {0, 1}


def test_failed_transfer_does_not_publish_root_and_preserves_error():
    expected = RuntimeError("retention graph transfer failure")
    failed = True

    def transfer(key, _states, acyclic):
        if key == 1 and failed:
            raise expected
        return RetentionState(frozenset({None}), False, acyclic)

    graph = RetentionGraph(lambda key: (0,) if key == 1 else (), transfer)
    for _ in range(2):
        with pytest.raises(RuntimeError) as caught:
            graph.evaluate(1, lambda: None)
        assert caught.value is expected and set(graph.final) == {0}
    failed = False
    assert graph.evaluate(1, lambda: None).acyclic


def test_budget_trip_preserves_only_completed_successor_components():
    held = ("enum-value", "probe.Enum")

    def dependencies(key):
        return (1, 2) if key == 0 else ((0,) if key == 1 else ())

    def transfer(key, states, acyclic):
        inputs = [states[child] for child in dependencies(key)]
        return RetentionState(
            frozenset({held}) if key == 2 else frozenset(element for state in inputs for element in state.origins),
            key == 2 or any(state.retained for state in inputs),
            acyclic,
        )

    graph = RetentionGraph(dependencies, transfer)
    attempts = 0

    def charge():
        nonlocal attempts
        attempts += 1
        if attempts > 2:
            raise fencing._RetentionBudgetExceeded

    with pytest.raises(fencing._RetentionBudgetExceeded):
        graph.evaluate(0, charge)
    assert set(graph.final) == {2}
    successor = graph.final[2]
    assert successor.retained and successor.acyclic
    assert graph.evaluate(1, lambda: None).retained
    assert graph.final[2] is successor


def test_consumer_exemptions_apply_per_complete_combination():
    metadata = ("metadata-query", "probe.query")
    unsafe = ("type-metadata", "probe.type")
    first, second = object(), object()

    def origins(value, _use, *, read_target=False):
        assert read_target is False
        return frozenset({metadata, unsafe}) if value is first else frozenset({None, unsafe})

    def consumer(selected):
        target = selected(first, first)
        argument = selected(second, first)
        assert selected(first, first) == target
        if target == unsafe:
            return "callable recovered"
        if argument == unsafe:
            return "metadata transferred"
        return None

    assert set(consumer_combinations(consumer, origins, lambda: None)) == {"callable recovered", "metadata transferred"}


def test_consumer_keeps_coupled_arguments_and_charges_partial_products():
    safe, unsafe = ("enum-sequence", "probe.Enum"), ("enum-value", "probe.Enum")
    values = tuple(object() for _ in range(7))
    attempts = 0

    def charge():
        nonlocal attempts
        attempts += 1
        if attempts > 30:
            raise fencing._RetentionBudgetExceeded

    def origins(_value, _use, *, read_target=False):
        return frozenset({safe, unsafe})

    def consumer(selected):
        arguments = [selected(value, values) for value in values]
        # The scalar exemption couples the first argument to the others. It
        # must never exempt all alternatives because one argument qualifies.
        return None if arguments[0] == safe and all(arg == safe for arg in arguments) else "unclosed caller"

    with pytest.raises(fencing._RetentionBudgetExceeded):
        consumer_combinations(consumer, origins, charge)
    assert attempts == 31
    assert consumer_combinations(consumer, origins, lambda: None) == ("unclosed caller",)


def test_consumer_projects_failed_last_factor_once_across_earlier_products():
    """An uncached failed SCC must not be re-run for each earlier assignment."""
    safe, held = None, ("enum-compound", "failed root")
    values = tuple(object() for _ in range(5))
    failed_projects = 0
    graph = RetentionGraph(
        lambda key: (1 - key,),
        lambda key, states, acyclic: RetentionState(states[1 - key].origins, key == 0 or states[1 - key].retained, acyclic),
    )

    def origins(value, _use, *, read_target=False):
        nonlocal failed_projects
        if value is values[-1]:
            failed_projects += 1
            attempts = 0

            def root_charge():
                nonlocal attempts
                attempts += 1
                if attempts > 1:
                    raise fencing._RetentionBudgetExceeded

            with pytest.raises(fencing._RetentionBudgetExceeded):
                graph.evaluate(0, root_charge)
            assert graph.final == {}
        return frozenset({safe, held})

    def consumer(selected):
        arguments = [selected(value, values) for value in values]
        return "unclosed caller" if held in arguments else None

    assert consumer_combinations(consumer, origins, lambda: None) == ("unclosed caller",)
    assert failed_projects == 1
    assert graph.final == {}, "consumer-local fallback caching never publishes failed graph state"


def test_combination_and_graph_work_share_one_aggregate_cap():
    budget = 20
    charged = 0
    values = tuple(object() for _ in range(5))
    graph = RetentionGraph(
        lambda key: ((key + 1) % 40,),
        lambda _key, _states, acyclic: RetentionState(frozenset({None}), False, acyclic),
    )

    def charge():
        nonlocal charged
        charged += 1
        if charged > budget:
            raise fencing._RetentionConsumerBudgetExceeded

    def origins(value, _use, *, read_target=False):
        if value is values[-1]:
            graph.evaluate(0, charge)
        return frozenset({None, ("enum-value", "probe.Enum")})

    def consumer(selected):
        arguments = [selected(value, values) for value in values]
        return "unclosed caller" if any(arguments) else None

    with pytest.raises(fencing._RetentionConsumerBudgetExceeded):
        consumer_combinations(consumer, origins, charge)
    assert charged == budget + 1 and graph.final == {}


def test_actual_consumer_budget_is_not_reset_by_nested_graph_root(monkeypatch):
    limit = 12
    source = "def held():\n    value0 = 'safe'\n"
    source += "".join(f"    value{index} = value{index - 1}\n" for index in range(1, 41))
    source += "    return value40\n"
    probe = _probe(source)
    returned = probe.tree.body[0].body[-1].value
    checked = []

    def check(evaluate):
        budget = evaluate.__kwdefaults__["charge"].__kwdefaults__["retained_root_budget"]
        assert budget["consumer_evaluations"] == 1, "the partial combination has already consumed work"
        with pytest.raises(fencing._RetentionConsumerBudgetExceeded) as caught:
            evaluate(returned)
        assert budget["consumer_evaluations"] == limit + 1
        assert (returned, False, False) not in evaluate.__kwdefaults__["graph"].final
        checked.append(True)
        # The observer must restore the exception to the real consumer
        # boundary after inspecting it. Swallowing it here can leave the
        # ordinary root already-final and therefore perform no further charge.
        raise caught.value

    monkeypatch.setattr(fencing, "_RETENTION_ROOT_EVALUATION_BUDGET", limit)
    _during_first_root(monkeypatch, probe, check)
    failures = _run(probe)
    assert checked == [True]
    assert any(row.startswith("retention budget exceeded at " + probe.path + ":") for row in failures)


@pytest.mark.parametrize("case", ("metadata-query", "preserving-conversion", "identity", "disclosure", "control-options"))
@pytest.mark.parametrize("unsafe_alternative", (False, True))
def test_actual_consumer_exemptions_are_per_complete_origin_combination(monkeypatch, case, unsafe_alternative):
    """Inject factors at the projector boundary; run the real scalar predicate.

    Closed receipt identities are local test inputs to the actual consumer,
    whose full exemption/coupling code is executed unchanged. This is separate
    from the receipt producers' own source-validation contracts.
    """
    arguments = {
        "metadata-query": "value",
        "preserving-conversion": "value",
        "identity": "receiver, capability",
        "disclosure": "capability=a, control_plugin=b, llm_component_id=c, llm_component_kind=d, role=e",
        "control-options": "plugin_name=a, alias=b, fields=c, summaries=d, role=e, disclosure_draft=f",
    }
    probe = _probe("def held():\n    return target(" + arguments[case] + ")\n")
    call = probe.tree.body[0].body[0].value
    factors = {argument: frozenset({None}) for argument in (*call.args, *(keyword.value for keyword in call.keywords))}
    role = ("enum-value", "probe.Role")
    extra_role = ("enum-value", "probe.OtherRole")
    if case == "metadata-query":
        allowed = ("metadata-query", "probe.query")
        factors[call.func] = frozenset({allowed, ("type-metadata", "probe.type")}) if unsafe_alternative else frozenset({allowed})
    elif case == "preserving-conversion":
        factors[call.func] = frozenset({("object", "builtins.list")})
        allowed = ("enum-sequence", "probe.Enum")
        factors[call.args[0]] = frozenset({allowed, ("enum-value", "probe.Enum")}) if unsafe_alternative else frozenset({allowed})
    elif case == "identity":
        factors[call.func] = frozenset({("object", "probe.identity")})
        factors[call.args[0]] = frozenset({None, role}) if unsafe_alternative else frozenset({None})
        factors[call.args[1]] = frozenset({("enum-value", "probe.Capability")})
    else:
        factors[call.func] = frozenset({("object", "probe." + case.replace("-", "_"))})
        role_value = next(keyword.value for keyword in call.keywords if keyword.arg == "role")
        factors[role_value] = frozenset({role, extra_role}) if unsafe_alternative else frozenset({role})
    original = fencing._reserve_retention.consumer_combinations
    observed = []

    def controlled(consumer, expressions, charge):
        if consumer.__defaults__[0] is not call:
            return original(consumer, expressions, charge)
        # The lambda and scalar function are owned Python functions. Resolve
        # their lexical cells directly, without dynamic attribute probes.
        captures = dict(zip(consumer.__code__.co_freevars, consumer.__closure__, strict=True))
        effect = captures["retained_supplier_effect"].cell_contents
        policy = {name: cell.cell_contents for name, cell in zip(effect.__code__.co_freevars, effect.__closure__, strict=True)}
        if case == "identity":
            policy["enum_identity_consumers"]["probe.identity"] = "receiver"
        elif case == "disclosure":
            policy["control_disclosure"].update(owner="probe.disclosure", parameters={"role": "probe.Role"})
        elif case == "control-options":
            policy["control_options"].update(owner="probe.control_options", role="probe.Role")

        def projected(value, use, *, read_target=False):
            return factors[value] if value in factors else expressions(value, use, read_target=read_target)

        result = original(consumer, projected, charge)
        observed.append(result)
        return result

    monkeypatch.setattr(fencing._reserve_retention, "consumer_combinations", controlled)
    failures = _run(probe)
    reason = "retained supplier callable recovered " if case == "metadata-query" else "retained enum value passed to unclosed caller "
    assert observed == [(reason,) if unsafe_alternative else ()]
    assert (reason + probe.path + ":2" in failures) is unsafe_alternative


def test_existing_aliased_callable_and_unfenced_dml_plant_stays_red():
    """The handover's section-5.4 plant, on an in-memory actual-source unit."""
    path = "src/elspeth/web/sessions/routes/_helpers.py"
    source = (fencing._repo_root() / path).read_text()
    original = fencing._parse_source(path, source)
    marker = "_task0_known_positive"
    assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == marker for node in ast.walk(original.tree))
    planted = fencing._parse_source(
        path,
        source + "\nfrom sqlalchemy import update as _task0_mutate\n"
        "from elspeth.core.landscape.schema import runs_table as _task0_audit_runs\n"
        "def _task0_known_positive(factory, conn, callback):\n"
        "    repo = factory.run_lifecycle\n    alias = repo.begin_run\n    callback(alias)\n"
        "    conn.execute(_task0_mutate(_task0_audit_runs).values(status='failed'))\n",
    )
    baseline = (*fencing._mutation_callable_escapes((original,)), *fencing._raw_write_surface_violations((original,)))
    assert not any(marker in row for row in baseline)
    escaped = [row for row in fencing._mutation_callable_escapes((planted,)) if marker in row]
    writes = [row for row in fencing._raw_write_surface_violations((planted,)) if marker in row]
    assert len(escaped) == 1 and escaped[0].endswith(" callable escape .begin_run"), escaped
    assert len(writes) == 2 and any("DML update runs" in row for row in writes) and any("DML construction" in row for row in writes), writes


def test_existing_hot_function_callable_escape_plant_stays_red():
    """Use the reviewed factory.run_lifecycle spelling at the original seam."""
    path = "src/elspeth/web/composer/tool_batch.py"
    source = (fencing._repo_root() / path).read_text()
    original = fencing._parse_source(path, source)
    owner = next(
        node for node in original.tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_execute_continuation_call_live"
    )
    anchors = [
        node
        for node in ast.walk(owner)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Name)
        and node.value.id == "state"
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Attribute)
        and node.targets[0].attr == "state"
        and isinstance(node.targets[0].value, ast.Name)
        and node.targets[0].value.id == "batch"
    ]
    assert len(anchors) == 1
    anchor = anchors[0]
    lines = source.splitlines(keepends=True)
    line = lines[anchor.lineno - 1]
    indent = line[: len(line) - len(line.lstrip())]
    insertion = "".join(
        indent + statement + "\n"
        for statement in (
            "_task0_repo = factory.run_lifecycle",
            "_task0_alias = _task0_repo.begin_run",
            "callback(_task0_alias)",
        )
    )
    planted = fencing._parse_source(path, "".join((*lines[: anchor.lineno - 1], insertion, *lines[anchor.lineno - 1 :])))
    wanted = f"{path}:{anchor.lineno + 1} _execute_continuation_call_live callable escape .begin_run"
    assert wanted not in fencing._mutation_callable_escapes((original,))
    assert wanted in fencing._mutation_callable_escapes((planted,))
