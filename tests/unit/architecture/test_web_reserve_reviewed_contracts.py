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


@pytest.mark.parametrize("fault", ("receiver", "operation", "actor", "provider", "failure", "missing", "extra"))
def test_actual_owner_cancel_failure_handoff_preserves_exact_original_targets(fault):
    path = "src/elspeth/web/sessions/routes/composer/operations.py"
    source = fencing._parse_source(path, (ROOT / path).read_text())
    method = fencing._reserve_node_at(source, "cancel_composer_operation")
    calls = [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run_sync_in_worker"
        and any(keyword.arg == "cancelled_failure" for keyword in node.keywords)
    ]
    assert len(calls) == 1
    original = calls[0]
    assert fencing._reserve_owner_cancel_failure_transport(original)
    changed = copy.deepcopy(original)
    if fault == "receiver":
        changed.args[0].attr = "request_cancel"
    elif fault == "missing":
        changed.keywords.pop()
    elif fault == "extra":
        changed.keywords.append(ast.keyword(arg="callback", value=ast.Name(id="foreign", ctx=ast.Load())))
    else:
        name = {"operation": "operation_id", "actor": "actor_user_id", "provider": "auth_provider_type", "failure": "cancelled_failure"}[
            fault
        ]
        keyword = next(item for item in changed.keywords if item.arg == name)
        keyword.value = ast.Name(id="foreign", ctx=ast.Load())
    assert not fencing._reserve_owner_cancel_failure_transport(changed)
    assert fencing._reserve_owner_cancel_failure_transport(original)


@pytest.mark.parametrize("fault", ("default", "type", "position", "veto"))
def test_native_service_failure_field_requires_exact_none_partition_and_veto(reviewed_reserve_units, fault):
    units = reviewed_reserve_units
    batch_path = "src/elspeth/web/composer/tool_batch.py"
    data_path = "src/elspeth/web/composer/continuation_batch_data.py"

    def partition(sources):
        batch = next(unit for unit in sources if unit.path == batch_path)
        classes = {name: fencing._reserve_node_at(batch, name) for name in fencing._reserve_BATCH_CARRIER_SOURCES}
        return fencing._reserve_batch_retained_field_partition(sources, classes)

    failures, fields = partition(units)
    assert not failures and fields, failures
    assert "service_failure" not in fields["ContinuationBatchState"]
    assert "manual_advisor_allowance" not in fields["ContinuationBatchState"]
    path = data_path if fault == "veto" else batch_path
    original = next(unit for unit in units if unit.path == path)
    changed = fencing._parse_source(path, original.source)
    if fault == "veto":
        joint = fencing._reserve_node_at(changed, "_joint")
        assert ast.unparse(joint.body[2].test) == "batch.service_failure is not None"
        joint.body[2].test = ast.Constant(value=False)
    else:
        declaration = fencing._reserve_node_at(changed, "ContinuationBatchState")
        field = next(node for node in declaration.body if isinstance(node, ast.AnnAssign) and ast.unparse(node.target) == "service_failure")
        if fault == "default":
            field.value = ast.Constant(value="foreign")
        elif fault == "type":
            field.annotation = ast.parse("BaseException | None", mode="eval").body
        else:
            declaration.body.remove(field)
            declaration.body.append(field)
    failures, refused = partition(tuple(changed if unit.path == path else unit for unit in units))
    assert failures and not refused
    assert partition(units) == ([], fields)


TURN = "src/elspeth/web/sessions/composer_turn.py"
SERVICE = "src/elspeth/web/sessions/service.py"
PROTOCOL = "src/elspeth/web/sessions/protocol.py"
COMPOSER_PROTOCOL = "src/elspeth/web/composer/protocol.py"
TRANSCRIPT_READER = "src/elspeth/web/composer/transcript_blob_reader.py"


def _source_units(paths: tuple[str, ...]) -> tuple[fencing.SourceUnit, ...]:
    # Complete ordinary-dispatch fixtures now include the actual moved body.
    # Keep existing caller indices stable and never synthesize its receipt.
    ordinary_paths = set(compose._METHOD_PATHS) - {compose.FINALIZATION}
    if ordinary_paths.issubset(paths) and compose.FINALIZATION not in paths:
        paths = (*paths, compose.FINALIZATION)
    return tuple(fencing._parse_source(path, (ROOT / path).read_text()) for path in paths)


@pytest.mark.parametrize(
    "body",
    [
        "try:\n    raise Exception()\nexcept Exception as str:\n    Envelope = TypedDict('Envelope', {'kind': Literal['audit'], 'name': str})\n",
        "match foreign:\n    case str:\n        Envelope = TypedDict('Envelope', {'kind': Literal['audit'], 'name': str})\n",
        "match foreign:\n    case [*str]:\n        Envelope = TypedDict('Envelope', {'kind': Literal['audit'], 'name': str})\n",
        "match foreign:\n    case {**str}:\n        Envelope = TypedDict('Envelope', {'kind': Literal['audit'], 'name': str})\n",
        "import builtins\nbuiltins.str = foreign\nEnvelope = TypedDict('Envelope', {'kind': Literal['audit'], 'name': str})\n",
        "__builtins__['str'] = foreign\nEnvelope = TypedDict('Envelope', {'kind': Literal['audit'], 'name': str})\n",
    ],
)
def test_typing_builtin_string_fact_refuses_implicit_and_namespace_rebinding(body):
    path = "src/elspeth/typing_builtin_binding_probe.py"
    unit = fencing._parse_source(path, "from typing import Literal, TypedDict\n" + body)
    assert fencing._reserve_typing_annotation_source_facts(unit)["typed_dict_consumers"] == frozenset()
    assert any(
        "typing supplier namespace transfer is unclosed " + path in row
        for row in fencing._reserve_typing_supplier_mutation_failures((unit,))
    )


@pytest.mark.parametrize(
    "body",
    [
        "def make[str]():\n    return TypedDict('Envelope', {'kind': Literal['audit'], 'name': str})\n",
        "def make[**str]():\n    return TypedDict('Envelope', {'kind': Literal['audit'], 'name': str})\n",
        "def make[*str]():\n    return TypedDict('Envelope', {'kind': Literal['audit'], 'name': str})\n",
        "class Frame[str]:\n    def make(self):\n        return TypedDict('Envelope', {'kind': Literal['audit'], 'name': str})\n",
        "type Envelope[str] = TypedDict('Envelope', {'kind': Literal['audit'], 'name': str})\n",
        "def make[Literal]():\n    return TypedDict('Envelope', {'kind': Literal['audit']})\n",
        "def make[TypedDict]():\n    return TypedDict('Envelope', {'kind': Literal['audit']})\n",
        "class Data[T](TypedDict):\n    phase: str\nEnvelope = TypedDict('Envelope', {'kind': Literal['audit'], 'data': Data})\n",
    ],
)
def test_typing_annotation_facts_refuse_enclosing_generic_parameter_binders(body):
    unit = fencing._parse_source("src/elspeth/typing_type_parameter_probe.py", "from typing import Literal, TypedDict\n" + body)
    assert fencing._reserve_typing_annotation_source_facts(unit)["typed_dict_consumers"] == frozenset()


@pytest.mark.parametrize("parameter", ["Format", "**Format", "*Format"])
def test_typing_global_read_refuses_generic_parameter_shadow(parameter):
    source = (
        "from typing import Literal, get_args\nFormat = Literal['x']\nclass Frame["
        + parameter
        + "]:\n    def values(self):\n        return get_args(Format)\n"
    )
    unit = fencing._parse_source("src/elspeth/typing_generic_scope_probe.py", source)
    nodes = tuple(ast.walk(unit.tree))
    parents = {id(child): parent for parent in nodes for child in ast.iter_child_nodes(parent)}
    reads = [node for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "Format"]
    assert len(reads) == 1
    assert not fencing._reserve_typing_global_read(unit, reads[0], "Format", parents)


@pytest.mark.parametrize(
    "effect",
    [
        "globals()['Format'] = tuple[typing._SpecialForm]\n",
        "namespace = locals()\nnamespace['Format'] = tuple[typing._SpecialForm]\n",
        "namespace = vars()\nnamespace['Format'] = tuple[typing._SpecialForm]\n",
        "get_namespace = globals\nget_namespace()['Format'] = tuple[typing._SpecialForm]\n",
        "import sys\nnamespace = sys.modules[__name__].__dict__\n",
        "from sys import modules as loaded\nnamespace = loaded[__name__]\n",
        "from importlib import import_module as load\nnamespace = load(__name__)\n",
        "namespace = __import__(__name__)\n",
        "def local():\n    pass\nnamespace = local.__globals__\nnamespace['Format'] = tuple[typing._SpecialForm]\n",
        "import elspeth.typing_namespace_probe as own\nown.Format = tuple[typing._SpecialForm]\n",
        "from elspeth import typing_namespace_probe as own\nown.Format = tuple[typing._SpecialForm]\n",
    ],
)
def test_typing_eager_scalar_fact_refuses_own_namespace_recovery(effect):
    path = "src/elspeth/typing_namespace_probe.py"
    source = "import typing\nfrom typing import Literal, get_args\nFormat = Literal['x']\n" + effect
    unit = fencing._parse_source(path, source + "values = frozenset(get_args(Format))\nopaque(get_args(Format))\n")
    assert fencing._reserve_typing_annotation_source_facts(unit)["scalar_query_results"] == frozenset()
    assert any(
        "typing supplier namespace transfer is unclosed " + path in row
        for row in fencing._reserve_typing_supplier_mutation_failures((unit,))
    )


@pytest.mark.parametrize(
    "between",
    [
        "opaque()\n",
        "x = opaque()\n",
        "x: opaque() = 1\n",
        "holder.value = 1\n",
        "holder['key'] = 1\n",
        "@opaque\nclass New:\n    pass\n",
        "import foreign\n",
    ],
)
def test_typing_eager_scalar_fact_requires_closed_dominating_interval(between):
    source = "from typing import Literal, get_args\nFormat = Literal['x']\n" + between + "values = frozenset(get_args(Format))\n"
    unit = fencing._parse_source("src/elspeth/typing_eager_interval_probe.py", source)
    assert fencing._reserve_typing_annotation_source_facts(unit)["scalar_query_results"] == frozenset()


@pytest.mark.parametrize("future", ["from __future__ import annotations\n", "from .__future__ import annotations\n"])
def test_typing_eager_scalar_interval_checks_real_future_annotation_semantics(future):
    source = future + "from typing import Literal, get_args\nFormat = Literal['x']\nx: opaque() = 1\nvalues = frozenset(get_args(Format))\n"
    unit = fencing._parse_source("src/elspeth/typing_future_interval_probe.py", source)
    results = fencing._reserve_typing_annotation_source_facts(unit)["scalar_query_results"]
    if future.startswith("from __future__"):
        assert len(results) == 1
    else:
        assert results == frozenset()


@pytest.mark.parametrize(
    "expression",
    ["opaque_factory()(get_args(Format))", "consume(mutate(), get_args(Format))", "opaque(get_args(Format))"],
)
def test_typing_eager_scalar_fact_requires_exact_materialization_expression(expression):
    source = "from typing import Literal, get_args\nFormat = Literal['x']\nvalues = " + expression + "\n"
    unit = fencing._parse_source("src/elspeth/typing_eager_expression_probe.py", source)
    assert fencing._reserve_typing_annotation_source_facts(unit)["scalar_query_results"] == frozenset()


def test_typing_deferred_scalar_read_gets_no_fact_after_cross_unit_rebinding():
    path = "src/elspeth/typing_deferred_producer.py"
    unit = fencing._parse_source(
        path, "from typing import Literal, get_args\nFormat = Literal['x']\ndef read():\n    return frozenset(get_args(Format))\n"
    )
    attacker = fencing._parse_source(
        "src/elspeth/typing_deferred_attacker.py",
        "import typing\nimport elspeth.typing_deferred_producer as producer\nproducer.Format = tuple[typing._SpecialForm]\n",
    )
    assert fencing._reserve_typing_annotation_source_facts(unit)["scalar_query_results"] == frozenset()
    assert any(
        "typing supplier namespace transfer is unclosed " + path in row
        for row in fencing._reserve_typing_supplier_mutation_failures((unit, attacker))
    )


@pytest.mark.parametrize(
    ("kind", "body"),
    [
        ("scalar_query_results", "def outer(Format):\n    def inner():\n        return frozenset(get_args(Format))\n    return inner()\n"),
        (
            "scalar_query_results",
            "def outer(get_args):\n    def inner():\n        return frozenset(get_args(Format))\n    return inner()\n",
        ),
        ("scalar_query_results", "def outer(*Format):\n    def inner():\n        return frozenset(get_args(Format))\n    return inner()\n"),
        (
            "scalar_query_results",
            "def outer(**get_args):\n    def inner():\n        return frozenset(get_args(Format))\n    return inner()\n",
        ),
        (
            "scalar_query_results",
            "def outer():\n    Format = foreign\n    def inner():\n        return frozenset(get_args(Format))\n    return inner()\n",
        ),
        ("scalar_query_results", "values = [get_args(Format) for Format in foreign]\n"),
        ("scalar_query_results", "values = lambda Format: frozenset(get_args(Format))\n"),
        (
            "typed_dict_consumers",
            "def outer(Literal):\n    def inner():\n        return TypedDict('Envelope', {'kind': Literal['audit']})\n    return inner()\n",
        ),
        (
            "typed_dict_consumers",
            "def outer(TypedDict):\n    def inner():\n        return TypedDict('Envelope', {'kind': Literal['audit']})\n    return inner()\n",
        ),
        (
            "typed_dict_consumers",
            "def outer(Data):\n    def inner():\n        return TypedDict('Envelope', {'kind': Literal['audit'], 'data': Data})\n    return inner()\n",
        ),
        (
            "typed_dict_consumers",
            "def outer(str):\n    def inner():\n        return TypedDict('Envelope', {'kind': Literal['audit'], 'name': str})\n    return inner()\n",
        ),
    ],
)
def test_typing_positive_facts_refuse_enclosing_lexical_shadows(kind, body):
    source = "from typing import Literal, get_args, TypedDict\nFormat = Literal['x']\nclass Data(TypedDict):\n    phase: str\n" + body
    unit = fencing._parse_source("src/elspeth/typing_enclosing_scope_probe.py", source)
    assert fencing._reserve_typing_annotation_source_facts(unit)[kind] == frozenset()


@pytest.mark.parametrize(
    "consumer",
    [
        "class Frame:\n    Format: str\n    def values(self):\n        return frozenset(get_args(Format))\n",
        "values = [item for item in get_args(Format)]\n",
        "values = [get_args(Format) for item in ('unused',)]\n",
    ],
)
def test_typing_global_read_respects_boundaries_without_deferred_result_grants(consumer):
    source = "from typing import Literal, get_args\nFormat = Literal['x']\n" + consumer
    unit = fencing._parse_source("src/elspeth/typing_scalar_scope_probe.py", source)
    nodes = tuple(ast.walk(unit.tree))
    parents = {id(child): parent for parent in nodes for child in ast.iter_child_nodes(parent)}
    reads = [node for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in {"Format", "get_args"}]
    assert reads
    assert all(fencing._reserve_typing_global_read(unit, node, node.id, parents) for node in reads)
    assert fencing._reserve_typing_annotation_source_facts(unit)["scalar_query_results"] == frozenset()


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
                    "src/elspeth/web/sessions/composer_finalization.py",
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


def test_retained_failure_branch_dependency_keeps_only_original_receiver_and_calls() -> None:
    units = _retained_transfer_units()
    failures, calls = fencing._reserve_retained_failure_transfer_receipts(units)
    assert failures == [], "the real retained-transfer positive is a prerequisite, not an opposition RED"
    assert len(calls) == 4
    audit_path = "src/elspeth/web/composer/audit_storage.py"
    assert fencing._reserve_RETAINED_FAILURE_SUPPLIERS[audit_path] == (
        "ComposerOperationFailureFacts",
        "private_operation_failure_draft",
    )
    assert "elspeth.web.composer.audit_storage.RetainedOperationFailureFacts" not in fencing._reserve_RETAINED_FAILURE_ORIGINS
    service = next(unit for unit in units if unit.path == SERVICE)
    method = fencing._reserve_node_at(service, "SessionServiceImpl.fail_composer_async_operation")
    guards = [
        statement
        for statement in method.body
        if isinstance(statement, ast.If)
        and ast.unparse(statement.test)
        == "operation_failure_facts is not None and type(operation_failure_facts) is not ComposerOperationFailureFacts"
    ]
    assert len(guards) == 1
    assert isinstance(guards[0].body[0], ast.Raise)
    actual = {
        (unit.path, ast.unparse(node.func))
        for unit in units
        for node in ast.walk(unit.tree)
        if isinstance(node, ast.Call) and (unit.path, id(node)) in calls
    }
    assert actual == {
        (TURN, "lease.create_task"),
        (TURN, "_capture_retained_operation_read"),
        (TURN, "_read_retained_operation_context"),
        (SERVICE, "private_operation_failure_draft"),
    }


@pytest.mark.parametrize(
    ("location", "imports", "effect"),
    (
        pytest.param(
            "local",
            "",
            "global RetainedOperationFailureFacts\n RetainedOperationFailureFacts = ComposerOperationFailureFacts",
            id="deferred-global",
        ),
        pytest.param(
            "local",
            "",
            "import elspeth.web.composer.audit_storage as storage\n storage.RetainedOperationFailureFacts = storage.ComposerOperationFailureFacts",
            id="qualified-local",
        ),
        pytest.param(
            "sibling",
            "from elspeth.web.composer import audit_storage as storage\nalias = storage\n",
            "alias.RetainedOperationFailureFacts = alias.ComposerOperationFailureFacts",
            id="sibling-module-alias",
        ),
        pytest.param(
            "sibling",
            "from elspeth.web.composer.audit_storage import RetainedOperationFailureFacts as Retained\n",
            "Retained.__bases__ = (foreign,)",
            id="sibling-class-alias",
        ),
    ),
)
def test_retained_failure_branch_dependency_refuses_rebinding(location: str, imports: str, effect: str) -> None:
    units = _retained_transfer_units()
    original_failures, original_calls = fencing._reserve_retained_failure_transfer_receipts(units)
    assert original_failures == [], "the real retained-transfer positive is a prerequisite, not an opposition RED"
    assert len(original_calls) == 4
    audit = next(unit for unit in units if unit.path == "src/elspeth/web/composer/audit_storage.py")
    selected = {
        name: fencing.stable_ast_dump(fencing._reserve_node_at(audit, name))
        for name in fencing._reserve_RETAINED_FAILURE_SUPPLIERS[audit.path]
    }
    path = audit.path if location == "local" else "src/elspeth/retained_branch_dependency_adversary.py"
    prefix = audit.source + "\n" if location == "local" else imports
    harmless = fencing._parse_source(path, prefix + "def branch_dependency_opponent():\n return None\n")
    control = tuple(harmless if unit.path == path else unit for unit in units) if location == "local" else (*units, harmless)
    assert fencing._reserve_retained_failure_transfer_receipts(control)[0] == []
    opponent = fencing._parse_source(path, prefix + "def branch_dependency_opponent():\n " + effect + "\n")
    assert fencing.stable_ast_dump(opponent.tree) != fencing.stable_ast_dump(harmless.tree)
    mutated = tuple(opponent if unit.path == path else unit for unit in units) if location == "local" else (*units, opponent)
    changed_audit = next(unit for unit in mutated if unit.path == audit.path)
    assert all(fencing.stable_ast_dump(fencing._reserve_node_at(changed_audit, name)) == shape for name, shape in selected.items())
    failures, calls = fencing._reserve_retained_failure_transfer_receipts(mutated)
    assert failures, "the classification dependency must not be rebound behind unchanged selected supplier bodies"
    assert all(failure.startswith("retained/private transfer branch dependency ") for failure in failures), failures
    assert calls == frozenset()
    assert fencing._reserve_retained_failure_transfer_receipts(units) == ([], original_calls)


@pytest.mark.parametrize("field", ("bases", "metaclass", "decorator", "type_parameters"))
def test_retained_failure_branch_dependency_refuses_declaration_header(field: str) -> None:
    units = _retained_transfer_units()
    original_failures, original_calls = fencing._reserve_retained_failure_transfer_receipts(units)
    assert original_failures == [], "the real retained-transfer positive is a prerequisite, not an opposition RED"
    assert len(original_calls) == 4
    audit = next(unit for unit in units if unit.path == "src/elspeth/web/composer/audit_storage.py")
    changed = fencing._parse_source(audit.path, audit.source)
    declaration = fencing._reserve_node_at(changed, "RetainedOperationFailureFacts")
    assert isinstance(declaration, ast.ClassDef)
    assert not declaration.bases and not declaration.keywords and not declaration.type_params
    assert [ast.unparse(item) for item in declaration.decorator_list] == ["dataclass(frozen=True, slots=True)"]
    original_header = fencing.stable_ast_dump(declaration)
    if field == "bases":
        declaration.bases = [ast.Name(id="ComposerOperationFailureFacts", ctx=ast.Load())]
    elif field == "metaclass":
        declaration.keywords = [ast.keyword(arg="metaclass", value=ast.Name(id="foreign", ctx=ast.Load()))]
    elif field == "decorator":
        declaration.decorator_list = [ast.Name(id="foreign", ctx=ast.Load())]
    else:
        declaration.type_params = ast.parse("class Probe[SessionOperationContext]:\n pass\n").body[0].type_params
    ast.fix_missing_locations(changed.tree)
    assert fencing.stable_ast_dump(declaration) != original_header
    assert all(
        fencing.stable_ast_dump(fencing._reserve_node_at(changed, name)) == fencing.stable_ast_dump(fencing._reserve_node_at(audit, name))
        for name in fencing._reserve_RETAINED_FAILURE_SUPPLIERS[audit.path]
    )
    mutated = tuple(changed if unit.path == audit.path else unit for unit in units)
    failures, calls = fencing._reserve_retained_failure_transfer_receipts(mutated)
    assert failures, "the classification dependency must retain its actual closed declaration header"
    assert all(failure.startswith("retained/private transfer branch dependency ") for failure in failures), failures
    assert calls == frozenset()
    assert fencing._reserve_retained_failure_transfer_receipts(units) == ([], original_calls)


@pytest.mark.parametrize("fault", ("missing", "foreign", "swapped", "constant", "none", "coroutine_default"))
def test_p4_title_limit_refuses_wrong_actual_forwarding(fault: str) -> None:
    original = _source_units((TURN,))[0]
    method = fencing._reserve_node_at(original, "_run_composer_turn")
    invoke = method.body[36].body[24]
    title = receiver.composer_title_prelude(invoke)
    dispatch = receiver.composer_dispatch_calls(invoke)
    assert title is invoke.body[0] and dispatch is not None
    changed = fencing._parse_source(TURN, original.source)
    candidate = fencing._reserve_node_at(changed, "_run_composer_turn").body[36].body[24]
    child = candidate.body[0].body[2]
    call = next(node for node in ast.walk(child) if isinstance(node, ast.Call) and ast.unparse(node.func) == "maybe_auto_title_session")
    argument = next(keyword for keyword in call.keywords if keyword.arg == "max_prepared_request_bytes")
    assert ast.unparse(argument.value) == "settings.composer_max_prepared_request_bytes"
    if fault == "missing":
        call.keywords.remove(argument)
    elif fault == "foreign":
        argument.value.value = ast.Name(id="foreign_settings", ctx=ast.Load())
    elif fault == "swapped":
        argument.value.attr = "composer_max_tool_calls_per_turn"
    elif fault == "constant":
        argument.value = ast.Constant(value=2 * 1024 * 1024)
    elif fault == "none":
        argument.value = ast.Constant(value=None)
    else:
        child.args.args.append(ast.arg(arg="limit"))
        child.args.defaults.append(copy.deepcopy(argument.value))
    ast.fix_missing_locations(changed.tree)
    assert receiver.composer_title_prelude(candidate) is None
    assert receiver.composer_dispatch_calls(candidate) is None
    assert receiver.composer_title_prelude(invoke) is title
    assert receiver.composer_dispatch_calls(invoke) == dispatch


@pytest.mark.parametrize("method_name", ("compose", "compose_recovery"))
@pytest.mark.parametrize("fault", ("budget", "missing_budget", "message", "unknown_keyword"))
def test_p4_paired_call_arguments_and_default_escape_remain_closed(method_name: str, fault: str) -> None:
    original = _source_units((TURN,))[0]
    invoke = fencing._reserve_node_at(original, "_run_composer_turn").body[36].body[24]
    dispatch = receiver.composer_dispatch_calls(invoke)
    assert dispatch is not None
    changed = fencing._parse_source(TURN, original.source)
    candidate = fencing._reserve_node_at(changed, "_run_composer_turn").body[36].body[24]
    calls = receiver.composer_dispatch_calls(candidate)
    assert calls is not None
    call = next(node for node in calls if node.func.attr == method_name)
    budget = next(keyword for keyword in call.keywords if keyword.arg == "budget_seconds")
    assert ast.unparse(budget.value) == "compose_budget_seconds"
    if fault == "budget":
        budget.value = ast.parse("budget_anchor.remaining_at_running_seconds", mode="eval").body
    elif fault == "missing_budget":
        call.keywords.remove(budget)
    elif fault == "message":
        if method_name == "compose":
            assert ast.unparse(call.args[0]) == "message_text"
            call.args[0] = ast.Name(id="foreign_message", ctx=ast.Load())
        else:
            message = next(keyword for keyword in call.keywords if keyword.arg == "message")
            assert ast.unparse(message.value) == "message_text"
            message.value = ast.Name(id="foreign_message", ctx=ast.Load())
    else:
        call.keywords.append(ast.keyword(arg="foreign_argument", value=ast.Name(id="provider_owner", ctx=ast.Load())))
    ast.fix_missing_locations(changed.tree)
    assert receiver.composer_title_prelude(candidate) is candidate.body[0]
    assert receiver.composer_dispatch_calls(candidate) is None
    assert receiver.composer_dispatch_calls(invoke) == dispatch


@pytest.mark.parametrize("fault", ("condition", "graph_guard", "extra_effect"))
def test_p4_paired_dispatch_keeps_actual_closed_branches(fault: str) -> None:
    original = _source_units((TURN,))[0]
    invoke = fencing._reserve_node_at(original, "_run_composer_turn").body[36].body[24]
    dispatch = receiver.composer_dispatch_calls(invoke)
    assert dispatch is not None
    changed = fencing._parse_source(TURN, original.source)
    candidate = fencing._reserve_node_at(changed, "_run_composer_turn").body[36].body[24]
    branch = candidate.body[2]
    assert isinstance(branch, ast.If) and ast.unparse(branch.test) == "recovery_context.malformed_head"
    if fault == "condition":
        branch.test = ast.Constant(value=True)
    elif fault == "graph_guard":
        del branch.orelse[0]
    else:
        branch.body.insert(0, ast.parse("foreign(provider_owner)").body[0])
    ast.fix_missing_locations(changed.tree)
    assert receiver.composer_dispatch_calls(candidate) is None
    assert receiver.composer_dispatch_calls(invoke) == dispatch


def test_composer_finalization_namespace_serializes_actual_clone_with_typed_ellipsis() -> None:
    unit = _source_units((compose.FINALIZATION,))[0]
    clone = fencing._parse_source(unit.path, unit.source)
    method = fencing._reserve_node_at(unit, "_turn_audit_cohort_drafts")
    assert any(isinstance(node, ast.Constant) and node.value is Ellipsis for node in ast.walk(method.returns))
    actual = compose._finalization_namespace_shape(unit.tree)
    assert len(actual) == 64
    assert compose._finalization_namespace_shape(clone.tree) == actual
    assert compose._canonical_ast(ast.Constant(value=Ellipsis)) == [
        "Constant",
        [["value", {"literal_type": "ellipsis"}]],
    ]
    marker_literal = ast.parse("{'literal_type': 'ellipsis'}", mode="eval").body
    assert compose._canonical_ast(marker_literal) != compose._canonical_ast(ast.Constant(value=Ellipsis))


@pytest.mark.parametrize("replacement", ("Ellipsis", "...", None))
def test_composer_finalization_namespace_distinguishes_actual_ellipsis_from_source_values(replacement: str | None) -> None:
    unit = _source_units((compose.FINALIZATION,))[0]
    expected = compose._finalization_namespace_shape(unit.tree)
    changed = fencing._parse_source(unit.path, unit.source)
    method = fencing._reserve_node_at(changed, "_turn_audit_cohort_drafts")
    targets = [node for node in ast.walk(method.returns) if isinstance(node, ast.Constant) and node.value is Ellipsis]
    assert targets, "the opposed Ellipsis must be in a real retained return annotation"
    targets[0].value = replacement
    assert compose._finalization_namespace_shape(changed.tree) != expected
    assert compose._finalization_namespace_shape(unit.tree) == expected


@pytest.mark.parametrize(
    ("literal", "expected"),
    (
        (None, ["Constant", []]),
        ("Ellipsis", ["Constant", [["value", "Ellipsis"]]]),
        (True, ["Constant", [["value", True]]]),
        (7, ["Constant", [["value", 7]]]),
    ),
)
def test_composer_canonical_literal_addition_preserves_existing_constant_shape(literal: object, expected: list[object]) -> None:
    assert compose._canonical_ast(ast.Constant(value=literal)) == expected


@pytest.mark.parametrize("fault", ("live-freeze", "missing-nested-freeze", "wrong-thaw"))
def test_composer_moved_finalizer_value_boundaries_refuse_after_only_source_shapes_are_rebound(monkeypatch, fault):
    units = _retained_transfer_units()
    failures, receipt = compose.compose_proposal_local_transfers(units)
    assert not failures and receipt is not None, failures
    path = compose.FINALIZATION
    original = next(unit for unit in units if unit.path == path)
    changed = fencing._parse_source(path, original.source)
    method = fencing._reserve_node_at(changed, "settle_composer_result")
    if fault == "wrong-thaw":
        calls = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "deep_thaw"
        ]
        assert len(calls) == 4
        calls[0].args[0] = ast.Name(id="settings", ctx=ast.Load())
        monkeypatch.setitem(compose._REVIEWED_METHOD_SHAPES, path, compose._method_shape(method))
    else:
        initializer = fencing._reserve_node_at(changed, "ComposerFinalizationInputs.__post_init__")
        call = initializer.body[0].value
        if fault == "live-freeze":
            call.args[1] = ast.Constant(value="lease")
        else:
            assert ast.unparse(call.args[-1]) == "'chat_ingress_inputs'"
            call.args.pop()
    ast.fix_missing_locations(changed.tree)
    monkeypatch.setattr(compose, "_FINALIZATION_NAMESPACE_SHAPE", compose._finalization_namespace_shape(changed.tree))
    failures, refused = compose.compose_proposal_local_transfers(tuple(changed if unit.path == path else unit for unit in units))
    assert failures and refused is None
    assert any("value freeze" in failure or "value thaw" in failure for failure in failures), failures


def test_composer_moved_finalizer_receipt_identifies_only_actual_wrapper_nodes() -> None:
    units = _retained_transfer_units()
    failures, receipt = compose.compose_proposal_local_transfers(units)
    assert failures == [], "the actual complete-body prerequisites must close before moved-finalizer oppositions"
    assert receipt is not None
    turn = next(unit for unit in units if unit.path == TURN)
    expected = {
        name: (TURN, id(node))
        for name in ("ComposerFinalizationInputs", "settle_composer_result", "settle_post_provider")
        for node in ast.walk(turn.tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    }
    assert set(expected) == {"ComposerFinalizationInputs", "settle_composer_result", "settle_post_provider"}
    assert receipt["moved_finalization"] == expected


@pytest.mark.parametrize(
    "fault",
    ("service", "lease", "running", "observation", "provider_binding", "budget_anchor", "result", "duplicate", "owner"),
)
def test_composer_moved_finalizer_refuses_changed_actual_handoff(fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    units = _retained_transfer_units()
    failures, receipt = compose.compose_proposal_local_transfers(units)
    assert failures == [] and receipt is not None
    original_shape = compose._REVIEWED_METHOD_SHAPES[TURN]
    mutated = tuple(fencing._parse_source(unit.path, unit.source) if unit.path == TURN else unit for unit in units)
    turn = next(unit for unit in mutated if unit.path == TURN)
    method = fencing._reserve_node_at(turn, "_run_composer_turn")
    wrapper = next(node for node in ast.walk(method) if isinstance(node, ast.AsyncFunctionDef) and node.name == "settle_post_provider")
    handoff = wrapper.body[0].value.value
    assert isinstance(handoff, ast.Call) and ast.unparse(handoff.func) == "settle_composer_result"
    constructor = handoff.args[0]
    assert isinstance(constructor, ast.Call) and ast.unparse(constructor.func) == "ComposerFinalizationInputs"
    if fault == "result":
        assert ast.unparse(handoff.args[1]) == "result"
        handoff.args[1] = ast.Name(id="foreign_result", ctx=ast.Load())
    elif fault == "duplicate":
        wrapper.body.insert(0, ast.Expr(value=ast.Await(value=copy.deepcopy(handoff))))
    elif fault == "owner":
        owners = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.Assign)
            and ast.unparse(node).startswith("continuation = lease.create_task(_capture_freeform_child(settle_post_provider(result))")
        ]
        assert len(owners) == 1
        owners[0].value.func.value = ast.Name(id="foreign_lease", ctx=ast.Load())
    else:
        argument = next(keyword for keyword in constructor.keywords if keyword.arg == fault)
        assert ast.unparse(argument.value) == fault
        argument.value = ast.Name(id="foreign_" + fault, ctx=ast.Load())
    ast.fix_missing_locations(turn.tree)
    # Re-admit only the changed caller's whole-body pin: the finite join must
    # distinguish the actual owner/argument/once-only obligation itself.
    monkeypatch.setitem(compose._REVIEWED_METHOD_SHAPES, TURN, compose._method_shape(compose._strip_inert_annotations(method, TURN)))
    failures, refused = compose.compose_proposal_local_transfers(mutated)
    assert failures and refused is None
    monkeypatch.setitem(compose._REVIEWED_METHOD_SHAPES, TURN, original_shape)
    assert compose.compose_proposal_local_transfers(units)[1] == receipt


@pytest.mark.parametrize("fault", ("base", "metaclass", "decorator", "default", "descriptor"))
def test_composer_moved_finalizer_refuses_nonplain_constructor(fault: str, monkeypatch: pytest.MonkeyPatch) -> None:
    units = _retained_transfer_units()
    failures, receipt = compose.compose_proposal_local_transfers(units)
    assert failures == [] and receipt is not None
    original_namespace_shape = compose._FINALIZATION_NAMESPACE_SHAPE
    path = "src/elspeth/web/sessions/composer_finalization.py"
    mutated = tuple(fencing._parse_source(unit.path, unit.source) if unit.path == path else unit for unit in units)
    source = next(unit for unit in mutated if unit.path == path)
    cls = fencing._reserve_node_at(source, "ComposerFinalizationInputs")
    assert isinstance(cls, ast.ClassDef)
    if fault == "base":
        cls.bases = [ast.Name(id="foreign", ctx=ast.Load())]
    elif fault == "metaclass":
        cls.keywords = [ast.keyword(arg="metaclass", value=ast.Name(id="foreign", ctx=ast.Load()))]
    elif fault == "decorator":
        cls.decorator_list = [ast.Name(id="foreign", ctx=ast.Load())]
    elif fault == "default":
        field = next(node for node in cls.body if isinstance(node, ast.AnnAssign) and ast.unparse(node.target) == "lease")
        assert field.value is None
        field.value = ast.Call(func=ast.Name(id="foreign", ctx=ast.Load()), args=[], keywords=[])
    else:
        cls.body.extend(ast.parse("def __getattribute__(self, name):\n return foreign(name)\n").body)
    ast.fix_missing_locations(source.tree)
    # Constructor obligations must survive deliberately re-admitted source
    # seals; this does not grant the changed declaration or its effects.
    monkeypatch.setattr(compose, "_FINALIZATION_NAMESPACE_SHAPE", compose._finalization_namespace_shape(source.tree))
    failures, refused = compose.compose_proposal_local_transfers(mutated)
    assert failures and refused is None
    monkeypatch.setattr(compose, "_FINALIZATION_NAMESPACE_SHAPE", original_namespace_shape)
    assert compose.compose_proposal_local_transfers(units)[1] == receipt


@pytest.mark.parametrize("fault", ("forwarding", "checkpoint", "audit", "cancellation", "response", "return"))
def test_composer_moved_finalizer_preserves_complete_selected_body(fault: str) -> None:
    units = _retained_transfer_units()
    failures, receipt = compose.compose_proposal_local_transfers(units)
    assert failures == [] and receipt is not None
    path = "src/elspeth/web/sessions/composer_finalization.py"
    mutated = tuple(fencing._parse_source(unit.path, unit.source) if unit.path == path else unit for unit in units)
    source = next(unit for unit in mutated if unit.path == path)
    method = fencing._reserve_node_at(source, "settle_composer_result")
    if fault == "forwarding":
        target = next(node for node in method.body if isinstance(node, ast.Assign) and ast.unparse(node) == "lease = inputs.lease")
        target.value = ast.Name(id="foreign_lease", ctx=ast.Load())
    elif fault in {"checkpoint", "audit"}:
        spelling = "checkpoint_projection.complete_owned()" if fault == "checkpoint" else "observation.audit_cohort_durable = True"
        targets = [node for node in ast.walk(method) if isinstance(node, (ast.Assign, ast.Expr)) and ast.unparse(node) == spelling]
        assert targets
        target = targets[-1]
        if isinstance(target, ast.Assign):
            target.value = ast.Constant(value=False)
        else:
            target.value = ast.Constant(value=None)
    elif fault in {"cancellation", "response"}:
        spelling = "_join_shielded_task_after_cancellation" if fault == "cancellation" else "_message_with_state_response"
        targets = [node for node in ast.walk(method) if isinstance(node, ast.Name) and node.id == spelling]
        assert targets
        targets[-1].id = "foreign"
    else:
        assert isinstance(method.body[-1], ast.Return) and ast.unparse(method.body[-1].value) == "record"
        method.body[-1].value = ast.Name(id="foreign", ctx=ast.Load())
    ast.fix_missing_locations(source.tree)
    failures, refused = compose.compose_proposal_local_transfers(mutated)
    assert failures and refused is None
    assert compose.compose_proposal_local_transfers(units)[1] == receipt


@pytest.mark.parametrize("fault", ("missing", "duplicate", "foreign_path", "foreign_import"))
def test_composer_moved_finalizer_refuses_source_substitution(fault: str) -> None:
    units = _retained_transfer_units()
    failures, receipt = compose.compose_proposal_local_transfers(units)
    assert failures == [] and receipt is not None
    path = "src/elspeth/web/sessions/composer_finalization.py"
    source = next(unit for unit in units if unit.path == path)
    if fault == "missing":
        mutated = tuple(unit for unit in units if unit.path != path)
    elif fault == "duplicate":
        mutated = (*units, fencing._parse_source(path, source.source))
    elif fault == "foreign_path":
        mutated = (
            *(unit for unit in units if unit.path != path),
            fencing._parse_source("src/elspeth/foreign_finalization.py", source.source),
        )
    else:
        mutated = tuple(fencing._parse_source(unit.path, unit.source) if unit.path == TURN else unit for unit in units)
        turn = next(unit for unit in mutated if unit.path == TURN)
        imports = [
            node
            for node in turn.tree.body
            if isinstance(node, ast.ImportFrom)
            and node.module == "elspeth.web.sessions.composer_finalization"
            and any(alias.name == "settle_composer_result" for alias in node.names)
        ]
        assert len(imports) == 1
        imports[0].module = "foreign.finalization"
    failures, refused = compose.compose_proposal_local_transfers(mutated)
    assert failures and refused is None
    assert compose.compose_proposal_local_transfers(units)[1] == receipt


@pytest.mark.parametrize(
    ("name", "field", "fault"),
    (
        ("persist_compose_turn", "continuation_observation", "default"),
        ("persist_continuation_compose_turn", "continuation_observation", "annotation"),
        ("_readback_continuation_p4_transaction", "observation", "annotation"),
        ("_retain_unavailable_continuation_p4", "observation", "default"),
        ("fail_continuation_tool_operation", "failure_projection_work", "annotation"),
        ("persist_continuation_compose_turn", "", "return"),
    ),
)
def test_terminal_p4_declaration_roles_survive_readmitted_namespace(
    name: str, field: str, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    units = _retained_transfer_units()
    assert terminal.terminal_sql_local_contracts(units)[0] == []
    original_namespace = terminal.NAMESPACE_GRAMMAR
    mutated = tuple(fencing._parse_source(unit.path, unit.source) if unit.path == SERVICE else unit for unit in units)
    source = next(unit for unit in mutated if unit.path == SERVICE)
    cls = fencing._reserve_node_at(source, terminal.CLASS)
    method = fencing._reserve_node_at(source, terminal.CLASS + "." + name)
    if fault == "return":
        assert ast.unparse(method.returns) == "RequiredSQLFinishOnce[CommittedContinuationP4]"
        method.returns = ast.Name(id="AuditOutcome", ctx=ast.Load())
    elif fault == "annotation":
        argument = next(argument for argument in (*method.args.args, *method.args.kwonlyargs) if argument.arg == field)
        assert argument.annotation is not None
        argument.annotation = ast.Name(id="foreign", ctx=ast.Load())
    else:
        index = next(index for index, argument in enumerate(method.args.kwonlyargs) if argument.arg == field)
        default = method.args.kw_defaults[index]
        if name == "_retain_unavailable_continuation_p4":
            assert default is None
            method.args.kw_defaults[index] = ast.Constant(value=None)
        else:
            assert isinstance(default, ast.Constant) and default.value is None
            method.args.kw_defaults[index] = ast.Constant(value=False)
    ast.fix_missing_locations(source.tree)
    monkeypatch.setattr(terminal, "NAMESPACE_GRAMMAR", terminal.namespace_grammar(cls))
    failures, _ = terminal.terminal_sql_local_contracts(mutated)
    assert any("reviewed P4" in failure and name in failure for failure in failures), failures
    monkeypatch.setattr(terminal, "NAMESPACE_GRAMMAR", original_namespace)
    assert terminal.terminal_sql_local_contracts(units)[0] == []


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
        (PROTOCOL, "5f453f3e66948e45e526fc770086498386d2c14868374fc9c55bcda15dec913d"),
        (
            "src/elspeth/web/coordination/composer_operation_authority.py",
            "f4da24239a18419aca382174e42dbbcba9d7cefb3da02a93d0a8a8c216076891",
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


@pytest.mark.parametrize(
    ("path", "symbol", "before", "after"),
    (
        (
            PROTOCOL,
            "SessionOperationAuthority.reconcile_unavailable_composer_segment",
            "candidate: ContinuationRecoveryCandidate",
            "candidate: object",
        ),
        (
            PROTOCOL,
            "SessionOperationAuthority.settle_lost_inactive_continuation",
            "candidate: ContinuationInactiveCandidate",
            "candidate: object",
        ),
        *(
            (
                "src/elspeth/web/coordination/composer_operation_authority.py",
                "ComposerAsyncOperationAuthority." + method,
                before,
                after,
            )
            for method in ("list_recoverable_segments", "list_inactive_continuations")
            for before, after in (
                ("from elspeth.web.sessions.continuation_recovery_candidates import", "from foreign.recovery import"),
                ("self._engine", "other._engine"),
                ("limit=limit", "limit=1"),
            )
        ),
    ),
)
def test_reviewed_u39_source_guard_seals_exact_candidate_and_discovery_transfers(path, symbol, before, after):
    sources = {item: (ROOT / item).read_text() for item in fencing._reserve_FULL_SOURCE_PATHS}
    assert fencing._reserve_full_source_failures(sources) == []
    original = fencing._parse_source(path, sources[path])
    node = fencing._reserve_node_at(original, symbol)
    assert isinstance(node, ast.FunctionDef)
    segment = ast.get_source_segment(original.source, node)
    assert segment is not None and segment.count(before) == 1 and original.source.count(segment) == 1
    changed = dict(sources)
    changed[path] = original.source.replace(segment, segment.replace(before, after, 1), 1)
    assert changed[path] != sources[path]
    assert fencing._reserve_full_source_failures(changed) == ["full source changed " + path]
    assert fencing._reserve_full_source_failures(sources) == []


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
    assert compose.compose_proposal_local_transfers(units)[0] == ["composer turn exact paired call arguments or closed branches changed"]


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
        ("src/elspeth/web/composer/continuation_title_provider.py", "invocation_owner.mint(ProviderInvocationFamily.TITLE)"),
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
    units = _source_units((worker_origin.APP, worker_origin.WORKER, worker_origin.TURN, "src/elspeth/web/coordination/lifecycle.py"))
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
    handlers = [handler for handler in invoke.handlers if ast.unparse(handler.type) == "ComposerMutationAuthorityRevoked"]
    assert len(handlers) == 1
    handler = handlers[0]
    index = invoke.handlers.index(handler)
    if fault == "type":
        handler.type.id = "Exception"
    elif fault == "missing":
        del invoke.handlers[index]
    elif fault == "order":
        invoke.handlers[index], invoke.handlers[index + 1] = invoke.handlers[index + 1], invoke.handlers[index]
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


def test_sink_callback_repository_binding_refuses_changed_recovery_body() -> None:
    # Local repository-seal control only; the full-source admission positives
    # retain the required common origin/effect proof.
    units = _source_units(tuple(unit.path for unit in fencing._production_units()))
    assert fencing._reserve_recipe_failures(units) == []
    failures, receipt = fencing._reserve_sink_effect_disjoint_receipts(units)
    assert failures == []
    assert receipt["reserve_calls"]
    path = "src/elspeth/web/coordination/repository.py"
    original = next(unit for unit in units if unit.path == path)
    changed = fencing._parse_source(path, original.source)
    method = fencing._reserve_node_at(changed, "_SessionOperationAuthorityRepository.recover_composer_segment")
    transitions = [
        node for node in ast.walk(method) if isinstance(node, ast.Call) and ast.unparse(node.func) == "recover_segment_on_connection"
    ]
    assert len(transitions) == 1
    transition = transitions[0]
    assert ast.unparse(transition) == (
        "recover_segment_on_connection(conn, session_id=str(session_id), operation_id=operation_id, "
        "parent=parent, segment=segment, history=history, now=now, next_eligible_at=now)"
    )
    histories = [keyword for keyword in transition.keywords if keyword.arg == "history"]
    assert len(histories) == 1 and ast.unparse(histories[0].value) == "history"
    histories[0].value = ast.Name(id="foreign_history", ctx=ast.Load())
    changed = _receiving_reparse_mutated_unit(changed)
    mutated = tuple(changed if unit.path == path else unit for unit in units)
    failures, refused = fencing._reserve_sink_effect_disjoint_receipts(mutated)
    assert failures == ["sink-effect normal callback source changed _SessionOperationAuthorityRepository"]
    assert refused == {}
    restored_failures, restored_receipt = fencing._reserve_sink_effect_disjoint_receipts(units)
    assert restored_failures == []
    assert restored_receipt["reserve_calls"] == receipt["reserve_calls"]


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


@pytest.mark.landscape_fencing_regression
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
    fencing._assert_current_batch_receipt_inventory(units, batch)


@pytest.mark.landscape_fencing_regression
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
            "prompt_guard_rendering.py",
            "    value = decoder(serialized)\n",
            "    decoder = unknown_consumer\n    value = decoder(serialized)\n",
            "complete consumer body changed",
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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
@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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
    fencing._assert_current_batch_receipt_inventory(units, batch)
    assert len(batch["state_writes"]) == 3


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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
    from collections.abc import Mapping
    from inspect import get_annotations
    from typing import Annotated, get_args, get_origin, get_type_hints

    from pydantic import BeforeValidator

    from elspeth.web.composer.redaction import (
        _RepairToolCallShadowModel,
        _restore_fixed_repair_arguments_for_validation,
        _SensitiveMarker,
        _summarize_repair_arguments,
    )

    raw = get_annotations(_RepairToolCallShadowModel)
    raw_again = get_annotations(_RepairToolCallShadowModel)
    resolved = get_type_hints(_RepairToolCallShadowModel, include_extras=True)
    resolved_again = get_type_hints(_RepairToolCallShadowModel, include_extras=True)
    assert type(raw) is dict and type(resolved) is dict
    assert raw is not raw_again and resolved is not resolved_again
    assert raw["arguments"] is raw_again["arguments"]
    assert raw.items().mapping["arguments"] is raw["arguments"]
    for current in (resolved, resolved_again):
        annotation = current["arguments"]
        assert get_origin(annotation) is Annotated
        value_type, validator, sensitive = get_args(annotation)
        assert value_type == Mapping[str, object]
        assert type(validator) is BeforeValidator
        assert validator.func is _restore_fixed_repair_arguments_for_validation
        assert type(sensitive) is _SensitiveMarker
        assert sensitive.summarizer is _summarize_repair_arguments
        assert current.values().mapping["arguments"] is annotation


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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
    if before == "if type(value) is not str:":
        declaration = fencing._reserve_node_at(owner, "_status_literal_values")
        source = ast.get_source_segment(owner.source, declaration)
        assert source is not None and source.count(before) == 1
        changed_source = owner.source.replace(source, source.replace(before, after), 1)
        assert fencing._reserve_node_at(owner, "_require_run_event_mapping") is not None
    else:
        assert owner.source.count(before) == 1
        changed_source = owner.source.replace(before, after)
    changed_owner = fencing._parse_source(owner.path, changed_source)
    assert ast.dump(fencing._reserve_node_at(changed_owner, "_require_run_event_mapping")) == ast.dump(
        fencing._reserve_node_at(owner, "_require_run_event_mapping")
    )
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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
    ("imports", "declaration", "query"),
    [
        ("from typing import Literal, get_args\n", "Format = Literal['jpeg', 'png']\n", "get_args(Format)"),
        ("from typing import Literal as L, get_args as args\n", "Format = L['jpeg', 'png']\n", "args(Format)"),
        ("import typing as t\n", "Format = t.Literal['jpeg', 'png']\n", "t.get_args(Format)"),
    ],
)
def test_typing_scalar_literal_query_result_is_data_only_on_its_original_call(imports, declaration, query):
    path = "src/elspeth/typing_scalar_result_probe.py"
    source = imports + declaration + "formats = frozenset(" + query + ")\n"
    unit = fencing._parse_source(path, source)
    facts = fencing._reserve_typing_annotation_source_facts(unit)
    actual = {id(node) for node in ast.walk(unit.tree)}
    assert len(facts["scalar_query_results"]) == 1
    assert facts["scalar_query_results"] <= actual
    assert fencing._reserve_typing_supplier_mutation_failures((unit,)) == []
    reparsed = fencing._parse_source(path, source)
    other = fencing._reserve_typing_annotation_source_facts(reparsed)
    assert len(other["scalar_query_results"]) == 1
    assert not other["scalar_query_results"] & actual
    opaque = fencing._parse_source(path, imports + declaration + "opaque_callback(" + query + ")\n")
    assert fencing._reserve_typing_annotation_source_facts(opaque)["scalar_query_results"] == frozenset()
    assert any(
        "typing supplier namespace transfer is unclosed " + path in row
        for row in fencing._reserve_typing_supplier_mutation_failures((opaque,))
    )
    direct = fencing._parse_source(path, source + "opaque_callback(Format)\n")
    assert any(
        "typing supplier namespace transfer is unclosed " + path in row
        for row in fencing._reserve_typing_supplier_mutation_failures((direct,))
    )


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("from typing import Literal, get_args", "from typing import Literal\nfrom foreign import get_args"),
        ("from typing import Literal, get_args", "from foreign import Literal\nfrom typing import get_args"),
        ("from typing import Literal, get_args", "from typing import Literal, get_args, get_args"),
        ("from typing import Literal, get_args", "from typing import Literal, Literal, get_args"),
        ("from typing import Literal, get_args", "from typing import Literal, get_args\nfrom foreign import *"),
        ("from typing import Literal, get_args", "from .typing import Literal, get_args"),
        ("values =", "get_args = foreign\nvalues ="),
        ("values =", "Literal = foreign\nvalues ="),
        ("values =", "Format = foreign\nvalues ="),
        ("values =", "del Format\nvalues ="),
        ("values =", "def change():\n    global Format\n    Format = foreign\nvalues ="),
        ("values = frozenset(get_args(Format))", "def local(Format):\n    return frozenset(get_args(Format))"),
        ("values = frozenset(get_args(Format))", "def local(get_args):\n    return frozenset(get_args(Format))"),
        ("Literal['x']", "Literal[1]"),
        ("Literal['x']", "Literal[Unknown]"),
        ("values = frozenset(get_args(Format))", "Other = Format\nvalues = frozenset(get_args(Other))"),
    ],
)
def test_typing_scalar_literal_query_withholds_fact_for_unproven_source(before, after):
    source = "from typing import Literal, get_args\nFormat = Literal['x']\nvalues = frozenset(get_args(Format))\n"
    assert source.count(before) == 1
    unit = fencing._parse_source("src/elspeth/typing_scalar_unproven_probe.py", source.replace(before, after))
    assert fencing._reserve_typing_annotation_source_facts(unit)["scalar_query_results"] == frozenset()


@pytest.mark.parametrize(
    "effect",
    [
        "Format.__class__.__getitem__ = replacement\n",
        "type(Format).__getitem__ = replacement\n",
        "vars(typing)['_SpecialForm'] = replacement\n",
        "getattr(typing, '_SpecialForm').__getitem__ = replacement\n",
        "opaque_callback(typing.get_origin(Format))\n",
        "opaque_callback(typing.get_type_hints(typing._SpecialForm))\n",
        "opaque_callback(typing.get_args(tuple[typing._SpecialForm]))\n",
    ],
)
def test_typing_scalar_result_fact_preserves_independent_supplier_effect_refusal(effect):
    path = "src/elspeth/typing_scalar_effect_probe.py"
    source = "import typing\nfrom typing import Literal, get_args\nFormat = Literal['x']\nvalues = frozenset(get_args(Format))\n"
    normal = fencing._parse_source(path, source)
    assert fencing._reserve_typing_supplier_mutation_failures((normal,)) == []
    changed = fencing._parse_source(path, source + effect)
    assert len(fencing._reserve_typing_annotation_source_facts(changed)["scalar_query_results"]) == 1
    assert any(row.startswith("typing supplier ") and path in row for row in fencing._reserve_typing_supplier_mutation_failures((changed,)))


@pytest.mark.parametrize(
    "source",
    [
        "from typing import Literal, TypedDict\nMode = Literal['move', 'copy']\nEdge = TypedDict('Edge', {'from': str, 'mode': Mode})\n",
        "from typing import Literal, TypedDict\nclass Data(TypedDict):\n    phase: str\nEnvelope = TypedDict('Envelope', {'_kind': Literal['audit'], 'pass': Data})\n",
    ],
)
def test_typing_functional_typed_dict_accepts_only_original_formal_annotation_transfer(source):
    path = "src/elspeth/typing_typed_dict_probe.py"
    unit = fencing._parse_source(path, source)
    facts = fencing._reserve_typing_annotation_source_facts(unit)
    actual = {id(node) for node in ast.walk(unit.tree)}
    assert len(facts["typed_dict_consumers"]) == 1
    assert facts["typed_dict_consumers"] <= actual
    assert fencing._reserve_typing_supplier_mutation_failures((unit,)) == []
    reparsed = fencing._parse_source(path, source)
    assert not fencing._reserve_typing_annotation_source_facts(reparsed)["typed_dict_consumers"] & actual
    escaped = fencing._parse_source(path, source + "opaque_callback({'kind': Literal['audit']})\n")
    assert any(
        "typing supplier namespace transfer is unclosed " + path in row
        for row in fencing._reserve_typing_supplier_mutation_failures((escaped,))
    )


@pytest.mark.parametrize(
    ("prefix", "expression"),
    [
        ("", "TypedDict('Envelope', {'kind': Literal['audit'], 'other': Unknown})"),
        ("", "TypedDict('Envelope', {'kind': Literal['audit'], 'other': typing._SpecialForm})"),
        ("", "TypedDict('Envelope', {'kind': Literal['audit'], 'kind': str})"),
        ("", "TypedDict('Envelope', {'kind': Literal['audit'], **fields})"),
        ("", "TypedDict('Envelope', {key(): Literal['audit']})"),
        ("", "TypedDict('Envelope', {'kind': Literal['audit']}, total=False)"),
        ("fields = {'kind': Literal['audit']}\n", "TypedDict('Envelope', fields)"),
        ("from foreign import TypedDict\n", "TypedDict('Envelope', {'kind': Literal['audit']})"),
        ("class Data:\n    phase: str\n", "TypedDict('Envelope', {'kind': Literal['audit'], 'data': Data})"),
        ("@decorate\nclass Data(TypedDict):\n    phase: str\n", "TypedDict('Envelope', {'kind': Literal['audit'], 'data': Data})"),
        ("class Data(TypedDict, metaclass=Foreign):\n    phase: str\n", "TypedDict('Envelope', {'kind': Literal['audit'], 'data': Data})"),
        ("class Data(TypedDict):\n    phase: str\nData = foreign\n", "TypedDict('Envelope', {'kind': Literal['audit'], 'data': Data})"),
    ],
)
def test_typing_functional_typed_dict_withholds_transfer_for_unsupported_field_or_factory(prefix, expression):
    path = "src/elspeth/typing_typed_dict_refusal_probe.py"
    source = "import typing\nfrom typing import Literal, TypedDict\n" + prefix + "Envelope = " + expression + "\n"
    unit = fencing._parse_source(path, source)
    assert fencing._reserve_typing_annotation_source_facts(unit)["typed_dict_consumers"] == frozenset()
    assert any(
        "typing supplier namespace transfer is unclosed " + path in row
        for row in fencing._reserve_typing_supplier_mutation_failures((unit,))
    )


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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


@pytest.mark.landscape_fencing_regression
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


_RETENTION_FIXPOINT_COMPANION_PATHS = (
    "src/elspeth/contracts/plugin_capabilities.py",
    "src/elspeth/web/async_workers.py",
    "src/elspeth/web/app.py",
    "src/elspeth/web/application_finalizers.py",
    "src/elspeth/web/process_recovery.py",
    "src/elspeth/web/coordination/repository.py",
)


def _empty_root_complete_finite_failures(probe):
    (supplier,) = _source_units(("src/elspeth/contracts/prompt_guard.py",))
    return fencing._reserve_selected_origin_effect_failures(
        (supplier, probe, *_source_units(_RETENTION_FIXPOINT_COMPANION_PATHS)),
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


def _run_fixpoint_probe(monkeypatch, probe, *, reference=False, reverse=False, observe=None, metrics=None):
    """Run both genuine root entries; the legacy DFS is only a bounded oracle."""
    original_cache = fencing.cache

    def decorated(function):
        wrapped = original_cache(function)
        expression_root = function.__qualname__.endswith(".retained_empty_root_state")
        capture_root = function.__qualname__.endswith(".retained_capture_root")
        if not (expression_root or capture_root):
            return wrapped
        evaluate = function.__kwdefaults__["evaluate"]
        if evaluate.__kwdefaults__["unit"] is not probe:
            return wrapped
        graph = evaluate.__kwdefaults__["graph"]
        graph.reverse = reverse
        oracle = function.__kwdefaults__["reference"]

        def current(value, **kwargs):
            if reference:
                if capture_root:
                    return oracle(value)
                origin, retained = oracle(value, frozenset(), read_target=kwargs.get("read_target", False))
                return fencing._reserve_retention.RetentionState(frozenset({origin}), retained, True)
            before = graph.evaluations
            hits = wrapped.cache_info().hits
            already_final = (value, kwargs.get("read_target", False), capture_root) in graph.final
            result = wrapped(value, **kwargs)
            if metrics is not None:
                metrics.append(
                    (
                        value,
                        kwargs.get("read_target", False),
                        capture_root,
                        before,
                        graph.evaluations,
                        wrapped.cache_info().hits - hits,
                        already_final,
                    )
                )
            if observe is not None:
                observe(value, kwargs.get("read_target", False), capture_root, result, oracle, graph)
            return result

        return current

    with monkeypatch.context() as patch:
        patch.setattr(fencing, "cache", decorated)
        return _empty_root_complete_finite_failures(probe)


_RETENTION_BODY_PROBES = (
    pytest.param("    a = (leaf, leaf)\n    b = (a, a)\n    return b\n", id="shared-dag"),
    pytest.param("    a = ()\n    b = a\n    a = (b, leaf)\n    b = a\n    return b\n", id="cycle"),
    pytest.param("    a = (leaf,)\n    a += ()\n", id="read-target"),
    pytest.param("    def child(value=leaf):\n        return value\n    return child\n", id="positional-default"),
    pytest.param("    def child(*, value=leaf):\n        return value\n    return child\n", id="keyword-default"),
    pytest.param("    return lambda: leaf\n", id="lambda-capture"),
    pytest.param("    def child():\n        nonlocal leaf\n        return leaf\n    return child\n", id="nonlocal"),
    pytest.param("    result=[]\n    result.extend([leaf for ignored in (0,)])\n    return result\n", id="compound-mutator"),
    pytest.param("    opaque_callback(value=[leaf for ignored in (0,)])\n", id="call-keyword"),
    pytest.param("    return (leaf for ignored in (0,))\n", id="generator"),
    pytest.param("    return leaf if True else None\n", id="conditional"),
    pytest.param("    return leaf.value\n", id="scalar-projection"),
    pytest.param("    with opaque(leaf) as carried:\n        return carried\n", id="with-item"),
    pytest.param("    match leaf:\n        case carried:\n            return carried\n", id="match-subject"),
    pytest.param("    class Child(opaque(leaf)):\n        pass\n    return Child\n", id="class-generic-child"),
)


@pytest.mark.parametrize("body", _RETENTION_BODY_PROBES)
@pytest.mark.parametrize("protected", (False, True))
def test_retention_fixpoint_matches_acyclic_dfs_and_preserves_cyclic_rows(monkeypatch, body, protected):
    """Acyclic equality and cyclic row inclusion replace path-memo storage pins.

    Exact DFS equality on cyclic inputs requires the path dependence that made
    the real gate non-terminating. The denial proof requires no dropped rows,
    including origins outside the protected-enum kinds, and exact acyclic R/O.
    """
    path = "src/elspeth/retention_fixpoint_contract_probe.py"
    leaf = "PromptGuardTrust.REFERENCE" if protected else "'safe'"
    probe = fencing._parse_source(
        path,
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n    leaf = " + leaf + "\n" + body,
    )
    observed = []

    def compare(value, read_target, capture, result, oracle, graph):
        if capture:
            acyclic = graph.final[value, False, True].acyclic
            if acyclic:
                assert oracle(value) is result
            else:
                assert not oracle(value) or result
            observed.append((True, acyclic))
            return
        origin, retained = oracle(value, frozenset(), read_target=read_target)
        if result.acyclic:
            assert result.origins == frozenset({origin})
            assert result.retained is retained
        else:
            assert origin in result.origins
            assert not retained or result.retained
        observed.append((False, result.acyclic))

    actual = _run_fixpoint_probe(monkeypatch, probe, observe=compare)
    old = _run_fixpoint_probe(monkeypatch, probe, reference=True)
    assert observed
    assert set(old) <= set(actual), sorted(set(old) - set(actual))
    if all(acyclic for _capture, acyclic in observed):
        assert actual == old
    assert set(_run_fixpoint_probe(monkeypatch, probe, reverse=True)) == set(actual)
    relevant = [row for row in actual if path + ":" in row]
    expected_retention = protected and body != "    return leaf.value\n"
    assert bool(relevant) is expected_retention, relevant


def test_retention_fixpoint_keeps_shadowed_names_and_passes_separate(monkeypatch):
    path = "src/elspeth/retention_fixpoint_shadow_probe.py"
    source = (
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\n"
        "leaf = PromptGuardTrust.REFERENCE\n"
        "def held(leaf):\n    return leaf\n"
        "def other():\n    leaf = 'safe'\n    return lambda: leaf\n"
        "result = [leaf for leaf in ('safe',)]\n"
    )
    graphs = []

    def observe(_value, _read_target, _capture, _result, _oracle, graph):
        if not graphs or graphs[-1] is not graph:
            graphs.append(graph)

    probe = fencing._parse_source(path, source)
    first = _run_fixpoint_probe(monkeypatch, probe, observe=observe)
    assert not any(path + ":" in row for row in first), first
    assert _run_fixpoint_probe(monkeypatch, probe, observe=observe) == first
    fresh = fencing._parse_source(path, source)
    assert _run_fixpoint_probe(monkeypatch, fresh, observe=observe) == first
    assert len(graphs) == 3 and len({id(graph) for graph in graphs}) == 3


def test_retention_fixpoint_store_and_read_target_have_separate_cache_entries(monkeypatch):
    probe = fencing._parse_source(
        "src/elspeth/retention_fixpoint_store_probe.py",
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n    leaf = PromptGuardTrust.REFERENCE\n    leaf += ()\n",
    )
    target = probe.tree.body[1].body[0].targets[0]
    checked = []

    def observe(_value, _read_target, _capture, _result, _oracle, graph):
        if checked:
            return

        def charge():
            return None

        ignored = graph.evaluate((target, False, False), charge)
        read = graph.evaluate((target, True, False), charge)
        assert ignored.origins == frozenset({None}) and ignored.retained is False
        assert read.retained is True
        assert graph.edges[target, False, False] == ()
        assert graph.edges[target, True, False]
        checked.append(True)

    _run_fixpoint_probe(monkeypatch, probe, observe=observe)
    assert checked == [True]


def test_retention_fixpoint_reuses_actual_walker_queries(monkeypatch):
    probe = fencing._parse_source(
        "src/elspeth/retention_fixpoint_traffic_probe.py",
        "def held():\n    value = 'safe'\n    return isinstance(value, object)\n",
    )
    metrics = []
    failures = _run_fixpoint_probe(monkeypatch, probe, metrics=metrics)
    assert not any(probe.path + ":" in row for row in failures), failures
    assert metrics
    assert any(not already_final and after > before for _node, _read, _capture, before, after, _hits, already_final in metrics)
    assert any(already_final and after == before for _node, _read, _capture, before, after, _hits, already_final in metrics)
    assert all(after == before for _node, _read, _capture, before, after, _hits, already_final in metrics if already_final)


def test_retention_fixpoint_semantic_overrides_and_path_cuts_do_not_reuse_graph(monkeypatch):
    probe = fencing._parse_source("src/elspeth/retention_fixpoint_override_probe.py", "def held():\n    value = 'safe'\n    return value\n")
    returned = probe.tree.body[0].body[-1].value
    checked = []

    def observe(_value, _read, _capture, _result, _oracle, graph):
        if checked:
            return
        # The evaluator is bound to fixed lexical inputs. Direct overrides
        # retain the old oracle API but cannot consult/publish graph entries.
        evaluate = evaluators[0]
        ordinary = evaluate(returned)
        before = dict(graph.final)
        defaults = evaluate.__kwdefaults__
        overrides = {
            "module": (defaults["module"] + "x")[:-1],
            "unit": fencing._parse_source(probe.path, "def held():\n    value = 'safe'\n    return value\n"),
            "parents": dict(defaults["parents"]),
            "retained_enum_kinds": set(defaults["retained_enum_kinds"]),
        }
        for name, changed in overrides.items():
            assert changed is not defaults[name]
            override = evaluate(returned, **{name: changed})
            assert override.retained is ordinary.retained and override.acyclic is False
            assert graph.final == before
        cut = evaluate(returned, frozenset({id(returned)}))
        assert cut.origins == frozenset({None}) and cut.retained is False
        assert graph.final == before
        checked.append(True)

    original_cache = fencing.cache
    evaluators = []

    def decorated(function):
        if function.__qualname__.endswith(".retained_empty_root_state"):
            evaluate = function.__kwdefaults__["evaluate"]
            if evaluate.__kwdefaults__["unit"] is probe:
                evaluators.append(evaluate)
        return original_cache(function)

    monkeypatch.setattr(fencing, "cache", decorated)
    _run_fixpoint_probe(monkeypatch, probe, observe=observe)
    assert checked == [True]


def test_retention_fixpoint_keeps_same_spelling_units_separate_in_one_pass(monkeypatch):
    supplier_path = "src/elspeth/contracts/prompt_guard.py"
    (supplier,) = _source_units((supplier_path,))
    safe = fencing._parse_source("src/elspeth/retention_fixpoint_safe.py", "def held():\n    value='safe'\n    return value\n")
    protected = fencing._parse_source(
        "src/elspeth/retention_fixpoint_protected.py",
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n    value=PromptGuardTrust.REFERENCE\n    return value\n",
    )
    original_cache = fencing.cache
    graphs = []

    def decorated(function):
        if function.__qualname__.endswith(".retained_empty_root_state"):
            evaluate = function.__kwdefaults__["evaluate"]
            if evaluate.__kwdefaults__["unit"] in (safe, protected):
                graph = evaluate.__kwdefaults__["graph"]
                assert graph.final == {}
                graphs.append(graph)
        return original_cache(function)

    monkeypatch.setattr(fencing, "cache", decorated)
    failures = fencing._reserve_selected_origin_effect_failures(
        (supplier, safe, protected, *_source_units(_RETENTION_FIXPOINT_COMPANION_PATHS)),
        additional_supplier_roots=frozenset({"elspeth.contracts.prompt_guard.PromptGuardTrust"}),
    )
    assert not any(safe.path + ":" in row for row in failures)
    assert any(protected.path + ":" in row for row in failures)
    assert len(graphs) == 2 and graphs[0] is not graphs[1]


@pytest.mark.parametrize("depth", (24, 48, 80))
@pytest.mark.parametrize("protected", (False, True))
def test_retention_fixpoint_complete_scanner_alias_chain_equals_dfs(monkeypatch, depth, protected):
    source = "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n"
    source += "    value0 = " + ("PromptGuardTrust.REFERENCE" if protected else "'safe'") + "\n"
    source += "".join(f"    value{index} = value{index - 1}\n" for index in range(1, depth + 1))
    source += f"    return value{depth}\n"
    probe = fencing._parse_source("src/elspeth/retention_fixpoint_complete_chain.py", source)
    actual = _run_fixpoint_probe(monkeypatch, probe)
    assert actual == _run_fixpoint_probe(monkeypatch, probe, reference=True)
    assert any(probe.path + ":" in row for row in actual) is protected


def test_retention_fixpoint_same_ast_safe_protected_safe_rebuilds_values(monkeypatch):
    probe = fencing._parse_source(
        "src/elspeth/retention_fixpoint_mutated_leaf.py",
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\ndef held():\n"
        "    leaf = 'safe'\n    a = b\n    b = (a, leaf)\n    return b\n",
    )
    held = probe.tree.body[1]
    leaf, returned = held.body[0], held.body[-1].value
    original = leaf.value
    observed = []

    def observe(value, _read, capture, result, _oracle, graph):
        if value is returned and not capture and all(previous is not graph for previous, _result in observed):
            observed.append((graph, result.retained))

    safe = _run_fixpoint_probe(monkeypatch, probe, observe=observe)
    assert not any(probe.path + ":" in row for row in safe)
    leaf.value = ast.increment_lineno(ast.parse("PromptGuardTrust.REFERENCE", mode="eval").body, original.lineno - 1)
    fencing._attach_parents(probe.tree)
    protected = _run_fixpoint_probe(monkeypatch, probe, observe=observe)
    assert any(probe.path + ":" in row for row in protected)
    assert set(_run_fixpoint_probe(monkeypatch, probe, reference=True)) <= set(protected)
    leaf.value = original
    fencing._attach_parents(probe.tree)
    assert _run_fixpoint_probe(monkeypatch, probe, observe=observe) == safe
    assert [retained for _graph, retained in observed] == [False, True, False]
    assert len({id(graph) for graph, _retained in observed}) == 3


def test_retention_fixpoint_actual_discovery_and_root_wrapper_failures_are_not_cached(monkeypatch):
    probe = fencing._parse_source(
        "src/elspeth/retention_fixpoint_discovery_error.py", "def held():\n    value = 'safe'\n    return value\n"
    )
    returned = probe.tree.body[0].body[-1].value
    expected = RuntimeError("actual retention lexical discovery failed")
    state = {"fail": False}
    failures, checked = [], []
    original_cache = fencing.cache

    def decorated(function):
        wrapped = original_cache(function)
        if function.__qualname__.endswith(".retained_name_producers") and function.__kwdefaults__["unit"] is probe:

            def lookup(value):
                if value is returned and state["fail"]:
                    failures.append(value)
                    raise expected
                return wrapped(value)

            return lookup
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
                graph = evaluate.__kwdefaults__["graph"]
                before = dict(graph.final)
                info = wrapped.cache_info()
                state["fail"] = True
                for _ in range(2):
                    with pytest.raises(RuntimeError) as caught:
                        wrapped(returned, read_target=False)
                    assert caught.value is expected
                    assert graph.final == before
                    assert (returned, False, False) not in graph.edges
                assert wrapped.cache_info().currsize == info.currsize
                assert wrapped.cache_info().misses == info.misses + 2
                state["fail"] = False
                result = wrapped(returned, read_target=False)
                assert result.retained is False and result.acyclic
                assert (returned, False, False) in graph.final
                assert wrapped(returned, read_target=False) is result

                def fail_evaluator(_value, *, read_target):
                    assert read_target is False
                    raise expected

                info = wrapped.cache_info()
                for _ in range(2):
                    with pytest.raises(RuntimeError) as caught:
                        wrapped(returned, read_target=False, evaluate=fail_evaluator)
                    assert caught.value is expected
                assert wrapped.cache_info().currsize == info.currsize
                assert wrapped.cache_info().misses == info.misses + 2
                checked.append(True)
            return wrapped(value, **kwargs)

        return current

    monkeypatch.setattr(fencing, "cache", decorated)
    actual = _empty_root_complete_finite_failures(probe)
    assert not any(probe.path + ":" in row for row in actual), actual
    assert failures == [returned, returned] and checked == [True]


_ENUM_FACT_PROBE_PATH = "src/elspeth/enum_record_fact_probe.py"
_ENUM_FACT_OWNERS = frozenset(
    {
        "elspeth.contracts.prompt_guard.PromptGuardTrust",
        "elspeth.contracts.prompt_guard.PromptGuardContentPart",
        "elspeth.contracts.plugin_capabilities.CapabilityDeclaration",
        "elspeth.contracts.plugin_capabilities.PluginCapability",
        "elspeth.contracts.plugin_capabilities.ControlRole",
    }
)
_ENUM_FACT_PROBE_SOURCE = """from enum import Enum
from elspeth.contracts.prompt_guard import PromptGuardContentPart, PromptGuardTrust
from elspeth.contracts.plugin_capabilities import CapabilityDeclaration, PluginCapability
from elspeth.web.composer.prompt_guard_rendering import json_content_parts

class OrdinaryRecord:
    text = 'ordinary'

class UnqualifiedEnum(Enum):
    VALUE = 'not a declared supplier role'

part = PromptGuardContentPart('reference', PromptGuardTrust.REFERENCE)
parts = json_content_parts('"reference"', reference_paths=frozenset({()}))
declaration = CapabilityDeclaration(PluginCapability.LLM)
opaque_callback(part.text, part.inspection_text, parts[0].text, declaration.blocks_positive_detection)
opaque_callback(OrdinaryRecord().text)
"""


@pytest.fixture(scope="module")
def actual_enum_fact_units():
    units = _source_units(tuple(unit.path for unit in fencing._production_units()))
    part = fencing._reserve_prompt_part_enum_record_receipt(units)
    renderer = fencing._reserve_prompt_renderer_return_receipt(units)
    capability = fencing._reserve_schema_capability_producer_receipt(units)
    assert part["failures"] == renderer["failures"] == capability["failures"] == []
    assert part["fields"] and renderer["origins"] and capability["origins"]
    units = (*units, fencing._parse_source(_ENUM_FACT_PROBE_PATH, _ENUM_FACT_PROBE_SOURCE))
    normal = fencing._reserve_run(units)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    return units


def _enum_fact_probe(units, monkeypatch, *, effect="", overlap=None, additional_preselected=frozenset()):
    original = fencing._reserve_selected_origin_effect_failures
    expanded = []

    def selected(current_units, extra_roots=frozenset(), *args, **kwargs):
        if "additional_supplier_roots" in kwargs and kwargs["additional_supplier_roots"]:
            expanded.append(True)
            roots = extra_roots
            if overlap is not None:
                roots = (roots | _ENUM_FACT_OWNERS) if overlap else (roots - _ENUM_FACT_OWNERS)
            return original(current_units, roots | additional_preselected, *args, **kwargs)
        return original(current_units, extra_roots, *args, **kwargs)

    probe = next(unit for unit in units if unit.path == _ENUM_FACT_PROBE_PATH)
    changed = fencing._parse_source(probe.path, probe.source + effect)
    actual = tuple(changed if unit.path == probe.path else unit for unit in units)
    with monkeypatch.context() as scope:
        scope.setattr(fencing, "_reserve_selected_origin_effect_failures", selected)
        result = fencing._reserve_run(actual)
    assert expanded, "the actual expanded supplier consumer proof must execute"
    return result


@pytest.mark.landscape_fencing_regression
@pytest.mark.parametrize("overlap", [True, False], ids=("preprotected", "new-supplier"))
def test_actual_enum_record_fields_project_under_both_supplier_arrival_roles(actual_enum_fact_units, monkeypatch, overlap):
    # The source-complete fixture already requires the real recovery/renderer
    # overlap positive. This changes arrival role, never final protection or
    # the closed scalar consumers; it does not inspect a synthetic fact dict.
    result = _enum_fact_probe(actual_enum_fact_units, monkeypatch, overlap=overlap)
    assert result["global_failures"] == []
    assert result["unproven_web_rows"] == []


@pytest.mark.landscape_fencing_regression
@pytest.mark.parametrize(
    "effect",
    (
        "opaque_callback(part.trust)\n",
        "opaque_callback(parts[0].trust)\n",
        "opaque_callback(declaration.capability)\n",
    ),
    ids=("part-enum", "renderer-enum", "capability-enum"),
)
def test_overlapping_enum_record_facts_keep_actual_retained_consumer_denials(actual_enum_fact_units, monkeypatch, effect):
    result = _enum_fact_probe(actual_enum_fact_units, monkeypatch, effect=effect)
    assert any(
        failure.startswith("retained enum value passed to unclosed caller " + _ENUM_FACT_PROBE_PATH + ":")
        for failure in result["global_failures"]
    ), result["global_failures"]


@pytest.mark.landscape_fencing_regression
def test_arbitrary_preselected_enum_does_not_gain_declared_scalar_fact_permissions(actual_enum_fact_units, monkeypatch):
    preselected = frozenset({"elspeth.enum_record_fact_probe.UnqualifiedEnum"})
    normal = _enum_fact_probe(actual_enum_fact_units, monkeypatch, additional_preselected=preselected)
    assert normal["global_failures"] == []
    assert normal["unproven_web_rows"] == []
    result = _enum_fact_probe(
        actual_enum_fact_units,
        monkeypatch,
        additional_preselected=preselected,
        effect="opaque_callback(UnqualifiedEnum.VALUE.value)\n",
    )
    assert any(_ENUM_FACT_PROBE_PATH + ":" in failure for failure in result["global_failures"]), result["global_failures"]


@pytest.mark.landscape_fencing_regression
def test_ordinary_no_supplier_role_path_preserves_its_actual_source_positive(actual_enum_fact_units):
    assert fencing._reserve_selected_origin_effect_failures(actual_enum_fact_units) == []


@pytest.mark.landscape_fencing_regression
@pytest.mark.parametrize(
    ("fault", "diagnostic"),
    (
        ("part-class", "prompt part exact nominal constructor changed PromptGuardContentPart"),
        ("part-field", "prompt part exact nominal constructor changed PromptGuardContentPart"),
        ("enum-member", "prompt part exact nominal constructor changed PromptGuardTrust"),
        ("enum-import", "prompt part actual constructor suppliers changed"),
        ("renderer-field", "prompt renderer retained output complete body changed"),
        ("late-capability-effect", "retained enum carrier constructor/field contract changed"),
        ("part-and-renderer", "prompt part exact nominal constructor changed PromptGuardContentPart"),
    ),
)
def test_invalid_enum_record_prerequisites_fail_closed_before_field_consumers(actual_enum_fact_units, monkeypatch, fault, diagnostic):
    if fault == "late-capability-effect":
        normal = _enum_fact_probe(actual_enum_fact_units, monkeypatch, overlap=True)
        assert normal["global_failures"] == []
        assert normal["unproven_web_rows"] == []
    part_path = "src/elspeth/contracts/prompt_guard.py"
    capability_path = "src/elspeth/contracts/plugin_capabilities.py"
    renderer_path = "src/elspeth/web/composer/prompt_guard_rendering.py"
    paths = {capability_path} if fault == "late-capability-effect" else {renderer_path} if fault == "renderer-field" else {part_path}
    if fault == "part-and-renderer":
        paths.add(renderer_path)
    changed = {}
    for unit in actual_enum_fact_units:
        if unit.path not in paths:
            continue
        current = fencing._parse_source(unit.path, unit.source)
        if unit.path == renderer_path:
            owner = fencing._reserve_node_at(current, "json_content_parts")
            owner.body.append(ast.parse("unqualified_renderer_effect()").body[0])
        elif fault == "late-capability-effect":
            owner = fencing._reserve_node_at(current, "CapabilityDeclaration.__post_init__")
            owner.body.append(ast.parse("unqualified_late_constructor_effect()").body[0])
            # The partial primitive prefix must remain valid; the complete
            # record prerequisite, rather than that unrelated prefix, refuses.
            assert fencing._reserve_capability_primitive_dependencies((current,))["failures"] == []
        elif fault == "part-class":
            fencing._reserve_node_at(current, "PromptGuardContentPart").bases.append(ast.Name(id="ForeignPart", ctx=ast.Load()))
        elif fault == "enum-member":
            owner = fencing._reserve_node_at(current, "PromptGuardTrust")
            member = next(node for node in owner.body if isinstance(node, ast.Assign) and node.targets[0].id == "REFERENCE")
            member.value = ast.Constant(value="foreign")
        elif fault == "enum-import":
            owner = next(node for node in current.tree.body if isinstance(node, ast.ImportFrom) and node.module == "enum")
            owner.module = "foreign_enum"
        else:
            owner = fencing._reserve_node_at(current, "PromptGuardContentPart.__post_init__")
            owner.body[0].test.values[0] = ast.Constant(value=False)
        ast.fix_missing_locations(current.tree)
        changed[unit.path] = current
    units = tuple(changed[unit.path] if unit.path in changed else unit for unit in actual_enum_fact_units)
    result = _enum_fact_probe(units, monkeypatch, overlap=True if fault == "late-capability-effect" else None)
    assert any(diagnostic in failure for failure in result["global_failures"]), result["global_failures"]
    if fault == "part-and-renderer":
        assert any("prompt renderer retained output complete body changed" in failure for failure in result["global_failures"])


_BINDING_FACT_ALIAS = "src/elspeth/web/composer/_response_json.py"
_BINDING_FACT_TURN = "src/elspeth/web/composer/turn_effects.py"
_BINDING_FACT_TITLE = "src/elspeth/web/composer/continuation_title_values.py"
_BINDING_FACT_ALIAS_ORIGIN = "elspeth.web.composer._response_json.FrozenResponseJSON"
_BINDING_FACT_HASH_ORIGIN = "elspeth.web.composer.turn_effects.authored_state_hash"
_BINDING_FACT_AUDIT_ORIGIN = "elspeth.web.composer.provider_audit_source.audit_drafts_json"
_BINDING_FACT_ROOTS = frozenset({_BINDING_FACT_ALIAS_ORIGIN, _BINDING_FACT_HASH_ORIGIN, _BINDING_FACT_AUDIT_ORIGIN})


def _binding_fact_units():
    # Actual subjects of the selected/additional-root union. This is a scoped
    # binding/effect proof, not the expanded complete-source reserve boundary.
    return _source_units(
        (_BINDING_FACT_ALIAS, _BINDING_FACT_TURN, _BINDING_FACT_TITLE, "src/elspeth/web/composer/provider_audit_source.py")
    )


def _binding_fact_owned_failures(units, roots=_BINDING_FACT_ROOTS):
    modules = {unit.path.removeprefix("src/").removesuffix(".py").replace("/", "."): unit for unit in units}
    return fencing._reserve_owned_source_binding_failures(modules, roots)


def _binding_fact_title_audit_import_is_current(unit):
    """Exact TITLE import and same-AST lexical binding, not common admission."""
    if unit.path != _BINDING_FACT_TITLE:
        return False
    local_module = "elspeth.web.composer.continuation_title_values"
    binding = "audit_drafts_json"
    failures = fencing._reserve_owned_source_binding_failures({local_module: unit}, frozenset({local_module + "." + binding}))
    declarations = [
        node
        for node in unit.tree.body
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" or (alias.asname or alias.name) == binding for alias in node.names)
    ]
    return (
        not failures
        and len(declarations) == 1
        and receiver.imported(declarations[0], binding, "elspeth.web.composer.provider_audit_source", binding)
    )


def test_binding_fact_actual_selected_producers_and_import_have_complete_scoped_positive():
    units = _binding_fact_units()
    assert _binding_fact_owned_failures(units) == []
    alias = next(unit for unit in units if unit.path == _BINDING_FACT_ALIAS)
    declaration = next(node for node in alias.tree.body if isinstance(node, ast.TypeAlias) and node.name.id == "FrozenResponseJSON")
    assert isinstance(declaration, ast.TypeAlias)
    assert isinstance(declaration.name, ast.Name)
    assert isinstance(declaration.name.ctx, ast.Store)
    title = next(unit for unit in units if unit.path == _BINDING_FACT_TITLE)
    assert _binding_fact_title_audit_import_is_current(title)
    # Check each real root independently, so an unrelated producer cannot
    # manufacture this binding positive. This does not assert common admission.
    for root in _BINDING_FACT_ROOTS:
        assert _binding_fact_owned_failures(units, frozenset({root})) == []


@pytest.mark.parametrize(
    ("path", "owner", "bound"),
    (
        (_BINDING_FACT_TURN, "PersistedBatchEffects", "authored_state_hash"),
        (_BINDING_FACT_TITLE, "TitleProviderResultSnapshot", "audit_drafts_json"),
    ),
)
def test_binding_fact_actual_annotation_nonwrites_belong_only_to_current_ast(path, owner, bound):
    unit = _source_units((path,))[0]
    cls = fencing._reserve_node_at(unit, owner)
    field = next(node for node in cls.body if isinstance(node, ast.AnnAssign) and node.target.id == bound)
    assert field.value is None
    assert fencing._reserve_annotation_only_nonwrites(unit, bound) == {id(field.target): id(cls)}
    reparsed = fencing._parse_source(path, unit.source)
    other = fencing._reserve_annotation_only_nonwrites(reparsed, bound)
    assert len(other) == 1
    assert id(field.target) not in other
    assert id(cls) not in other.values()
    changed = fencing._parse_source(path, unit.source)
    changed_cls = fencing._reserve_node_at(changed, owner)
    changed_field = next(node for node in changed_cls.body if isinstance(node, ast.AnnAssign) and node.target.id == bound)
    changed_field.value = ast.Name(id="foreign", ctx=ast.Load())
    assert fencing._reserve_annotation_only_nonwrites(changed, bound) == {}
    assert fencing._reserve_annotation_only_nonwrites(unit, bound) == {id(field.target): id(cls)}
    assert fencing._reserve_dataclass_field_local_bindings(unit, bound, ("object", "foreign.field")) == {}


@pytest.mark.parametrize(
    ("addition", "diagnostic"),
    (
        ("type FrozenResponseJSON = str\n", "selected owned origin has no unique source producer "),
        ("FrozenResponseJSON = foreign\n", "selected owned origin has no unique source producer "),
        ("def FrozenResponseJSON():\n    pass\n", "selected owned origin has no unique source producer "),
        ("del FrozenResponseJSON\n", "selected owned origin rebound or deleted "),
        (
            "def corrupt():\n    global FrozenResponseJSON\n    FrozenResponseJSON = foreign\n",
            "selected owned origin scope declaration ",
        ),
    ),
)
def test_binding_fact_actual_pep695_alias_refuses_duplicate_replacement_and_deletion(addition, diagnostic):
    units = _binding_fact_units()
    assert _binding_fact_owned_failures(units) == []
    alias = next(unit for unit in units if unit.path == _BINDING_FACT_ALIAS)
    changed = fencing._parse_source(alias.path, alias.source + "\n" + addition)
    changed_units = tuple(changed if unit is alias else unit for unit in units)
    failures = _binding_fact_owned_failures(changed_units)
    assert any(failure.startswith(diagnostic + _BINDING_FACT_ALIAS_ORIGIN) for failure in failures), failures
    assert _binding_fact_owned_failures(units) == []


@pytest.mark.parametrize(
    "addition",
    (
        "authored_state_hash = foreign\n",
        "del authored_state_hash\n",
        "def nested():\n    global authored_state_hash\n    authored_state_hash = foreign\n",
        "def enclosing():\n    authored_state_hash = original\n    def nested():\n        nonlocal authored_state_hash\n        authored_state_hash = foreign\n",
        "def nested(value=(authored_state_hash := foreign)):\n    pass\n",
        "@(authored_state_hash := foreign)\ndef nested():\n    pass\n",
    ),
)
def test_binding_fact_annotation_nonwrite_does_not_hide_real_scope_default_or_decorator_writes(addition):
    units = _binding_fact_units()
    assert _binding_fact_owned_failures(units) == []
    turn = next(unit for unit in units if unit.path == _BINDING_FACT_TURN)
    changed = fencing._parse_source(turn.path, turn.source + "\n" + addition)
    failures = _binding_fact_owned_failures(tuple(changed if unit is turn else unit for unit in units))
    assert failures
    assert all(_BINDING_FACT_HASH_ORIGIN in failure for failure in failures), failures


def test_binding_fact_missing_independent_root_does_not_receive_an_empty_success():
    units = _binding_fact_units()
    assert _binding_fact_owned_failures(units) == []
    missing = "elspeth.web.composer._response_json.NotTheActualAlias"
    assert _binding_fact_owned_failures(units, _BINDING_FACT_ROOTS | {missing}) == [
        "selected owned origin has no unique source producer " + missing
    ]


def test_binding_fact_foreign_import_does_not_gain_supplier_identity_from_a_nonwrite():
    title = _source_units((_BINDING_FACT_TITLE,))[0]
    original = "from elspeth.web.composer.provider_audit_source import "
    assert title.source.count(original) == 1
    changed = fencing._parse_source(title.path, title.source.replace(original, "from foreign_audit_source import ", 1))
    assert _binding_fact_title_audit_import_is_current(title)
    assert not _binding_fact_title_audit_import_is_current(changed)
    # A lexical nonwrite is intentionally not an import/source admission.
    assert len(fencing._reserve_annotation_only_nonwrites(changed, "audit_drafts_json")) == 1


def test_binding_fact_scoped_origin_positive_preserves_the_actual_selected_subjects():
    units = _binding_fact_units()
    assert _binding_fact_owned_failures(units) == []
    assert fencing._reserve_selected_origin_effect_failures(units, extra_roots=_BINDING_FACT_ROOTS) == []


def test_binding_fact_nonwrite_refuses_an_actual_value_bearing_owned_field():
    units = _binding_fact_units()
    assert _binding_fact_owned_failures(units) == []
    turn = next(unit for unit in units if unit.path == _BINDING_FACT_TURN)
    before = "    authored_state_hash: str\n"
    assert turn.source.count(before) == 1
    changed = fencing._parse_source(turn.path, turn.source.replace(before, "    authored_state_hash: str = foreign\n", 1))
    failures = _binding_fact_owned_failures(tuple(changed if unit is turn else unit for unit in units))
    assert len(failures) == 1
    assert failures[0].startswith("selected owned origin rebound or deleted " + _BINDING_FACT_HASH_ORIGIN + ":")


@pytest.mark.parametrize("value", ("foreign()", "ForeignSupplier.__value__", "ForeignSupplier.__dict__"))
def test_binding_fact_alias_value_requires_its_separate_origin_and_effect_proof(value):
    units = _binding_fact_units()
    assert _binding_fact_owned_failures(units) == []
    alias = next(unit for unit in units if unit.path == _BINDING_FACT_ALIAS)
    changed = fencing._parse_source(alias.path, alias.source)
    declaration = next(node for node in changed.tree.body if isinstance(node, ast.TypeAlias) and node.name.id == "FrozenResponseJSON")
    declaration.value = ast.parse(value, mode="eval").body
    # This phase recognizes a lexical producer only. Even a protected/unknown
    # lazy value remains intact for the unchanged origin/effect phase; it must
    # not be evaluated, erased or turned into a scalar/constructor receipt here.
    expected = ast.dump(declaration.value)
    assert _binding_fact_owned_failures(tuple(changed if unit is alias else unit for unit in units)) == []
    assert ast.dump(declaration.value) == expected
    assert fencing._reserve_annotation_only_nonwrites(changed, "FrozenResponseJSON") == {}


@pytest.mark.parametrize(
    "declaration",
    (
        "class Snapshot:\n    ordinary: opaque_callback(supplier)\n",
        "class Snapshot:\n    ordinary: str = opaque_callback(supplier)\n",
        "def callback(value=opaque_callback(supplier)):\n    pass\n",
        "@opaque_callback(supplier)\ndef callback():\n    pass\n",
    ),
)
def test_binding_fact_nonwrites_do_not_admit_eager_annotation_default_or_decorator_effects(declaration):
    units = _binding_fact_units()
    path = "src/elspeth/binding_fact_annotation_adversary.py"
    header = "from elspeth.web.composer.provider_audit_source import audit_drafts_json as supplier\n"
    normal = fencing._parse_source(path, header + "class Snapshot:\n    supplier: str\n")
    assert fencing._reserve_selected_origin_effect_failures((*units, normal), extra_roots=_BINDING_FACT_ROOTS) == []
    changed = fencing._parse_source(path, header + declaration)
    failures = fencing._reserve_selected_origin_effect_failures((*units, changed), extra_roots=_BINDING_FACT_ROOTS)
    assert any("selected owner capability passed to unreviewed caller " + path + ":" in failure for failure in failures), failures


@pytest.mark.parametrize(
    "addition",
    (
        "audit_drafts_json = foreign\n",
        "del audit_drafts_json\n",
        "from foreign_audit_source import audit_drafts_json\n",
        "def corrupt():\n    global audit_drafts_json\n    audit_drafts_json = foreign\n",
        "class ForeignSnapshot:\n    audit_drafts_json: str = foreign\n",
        "def corrupt(value=(audit_drafts_json := foreign)):\n    pass\n",
        "@(audit_drafts_json := foreign)\ndef corrupt():\n    pass\n",
    ),
)
def test_binding_fact_title_import_refuses_real_binding_changes(addition):
    title = _source_units((_BINDING_FACT_TITLE,))[0]
    assert _binding_fact_title_audit_import_is_current(title)
    changed = fencing._parse_source(title.path, title.source + "\n" + addition)
    assert not _binding_fact_title_audit_import_is_current(changed)
    assert _binding_fact_title_audit_import_is_current(title)


@pytest.mark.parametrize("fault", ("wrong_member", "wrong_alias", "duplicate_import", "wildcard_import", "relative_import"))
def test_binding_fact_title_import_refuses_changed_import_identity(fault):
    title = _source_units((_BINDING_FACT_TITLE,))[0]
    assert _binding_fact_title_audit_import_is_current(title)
    changed = fencing._parse_source(title.path, title.source)
    declaration = next(
        node
        for node in changed.tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "elspeth.web.composer.provider_audit_source"
    )
    alias = next(alias for alias in declaration.names if alias.name == "audit_drafts_json")
    if fault == "wrong_member":
        alias.name = "foreign_member"
        alias.asname = "audit_drafts_json"
    elif fault == "wrong_alias":
        alias.asname = "foreign_binding"
    elif fault == "duplicate_import":
        changed.tree.body.append(copy.deepcopy(declaration))
    elif fault == "wildcard_import":
        changed.tree.body.append(ast.ImportFrom(module="foreign_audit_source", names=[ast.alias(name="*")], level=0))
    else:
        declaration.level = 1
    assert not _binding_fact_title_audit_import_is_current(changed)
    assert _binding_fact_title_audit_import_is_current(title)


def test_binding_fact_title_import_refuses_a_value_bearing_actual_field():
    title = _source_units((_BINDING_FACT_TITLE,))[0]
    assert _binding_fact_title_audit_import_is_current(title)
    changed = fencing._parse_source(title.path, title.source)
    cls = fencing._reserve_node_at(changed, "TitleProviderResultSnapshot")
    field = next(node for node in cls.body if isinstance(node, ast.AnnAssign) and node.target.id == "audit_drafts_json")
    assert field.value is None
    field.value = ast.Name(id="foreign", ctx=ast.Load())
    assert not _binding_fact_title_audit_import_is_current(changed)
    assert _binding_fact_title_audit_import_is_current(title)


@pytest.mark.parametrize("fault", ("wrong_module", "missing_binding"))
def test_state_metadata_guard_import_is_the_exact_reviewed_binding(reviewed_reserve_units, fault):
    units = reviewed_reserve_units
    assert fencing._reserve_recipe_failures(units) == []
    path = "src/elspeth/web/sessions/converters.py"
    original = next(unit for unit in units if unit.path == path)
    old = "from elspeth.web.sessions.protocol import CompositionStateRecord, require_stored_state_metadata\n"
    new = "from elspeth.web.sessions.protocol import CompositionStateRecord\n"
    if fault == "wrong_module":
        new += "from foreign_protocol import require_stored_state_metadata\n"
    assert original.source.count(old) == 1
    changed = fencing._parse_source(path, original.source.replace(old, new, 1))
    mutated = tuple(changed if unit.path == path else unit for unit in units)
    expected = ["writer target/field recipe changed " + path + ":module-bindings"]
    assert fencing._reserve_recipe_failures(mutated) == expected
    assert fencing._reserve_ReserveProof(mutated).failures == expected
    assert fencing._reserve_recipe_failures(units) == []


def _receiving_changed_units(units, path, change):
    original = next(unit for unit in units if unit.path == path)
    changed = fencing._parse_source(path, original.source)
    change(changed)
    return tuple(changed if unit is original else unit for unit in units)


def _receiving_batch_classes(units):
    unit = next(unit for unit in units if unit.path == "src/elspeth/web/composer/tool_batch.py")
    return {name: fencing._reserve_node_at(unit, name) for name in fencing._reserve_BATCH_CARRIER_SOURCES}


@pytest.mark.parametrize("fault", ("delete_handler", "delete_audit", "durable_before_audit"))
def test_receiving_provider_context_handler_keeps_required_audit_before_projection(fault):
    units = _provider_mint_units()
    assert provider.provider_mint_local_contracts(units)[0] == []

    def change(unit):
        method = fencing._reserve_node_at(unit, "_run_composer_turn")
        owner, handler = next(
            (node, handler)
            for node in ast.walk(method)
            if isinstance(node, ast.Try)
            for handler in node.handlers
            if isinstance(handler.type, ast.Name) and handler.type.id == "ComposerContextUnsupported"
        )
        if fault == "delete_handler":
            owner.handlers.remove(handler)
        else:
            guarded = next(node for node in handler.body if isinstance(node, ast.If))
            assert len(guarded.body) == 2 and isinstance(guarded.body[0].value, ast.Await)
            if fault == "delete_audit":
                guarded.body.pop(0)
            else:
                guarded.body.reverse()

    changed = _receiving_changed_units(units, TURN, change)
    failures, receipt = provider.provider_mint_local_contracts(changed)
    assert failures == [
        "provider mint complete producer/retention/caller contract changed elspeth.web.sessions.composer_turn:_run_composer_turn"
    ]
    assert receipt is None
    assert provider.provider_mint_local_contracts(units)[0] == []


def test_receiving_batch_partition_retains_every_wire_field_and_only_one_settled_live_owner():
    units = fencing._production_units()
    classes = _receiving_batch_classes(units)
    failures, fields = fencing._reserve_batch_retained_field_partition(units, classes)
    assert failures == []
    actual = tuple(node.target.id for node in classes["ContinuationBatchState"].body if isinstance(node, ast.AnnAssign))
    assert actual[-1] == "manual_advisor_allowance"
    assert fields["ContinuationBatchState"] == tuple(name for name in actual if name not in {"manual_advisor_allowance", "service_failure"})
    for name in ("ContinuationCallState", "_CallWireFacts"):
        assert fields[name] == tuple(node.target.id for node in classes[name].body if isinstance(node, ast.AnnAssign))
    failures, receipt = fencing._reserve_continuation_batch_state_receipt(units)
    assert failures == []
    fencing._assert_current_batch_receipt_inventory(units, receipt)


@pytest.mark.parametrize("fault", ("default", "type", "name", "position", "detached"))
def test_receiving_batch_partition_refuses_changed_transient_declaration_or_source_identity(fault):
    units = fencing._production_units()
    path = "src/elspeth/web/composer/tool_batch.py"

    def change(unit):
        owner = fencing._reserve_node_at(unit, "ContinuationBatchState")
        transient = owner.body[-1]
        assert transient.target.id == "manual_advisor_allowance"
        if fault == "default":
            transient.value.keywords[0].value = ast.Constant(value=0)
        elif fault == "type":
            transient.annotation = ast.Name(id="foreign", ctx=ast.Load())
        elif fault == "name":
            transient.target.id = "unreviewed_owner"
        elif fault == "position":
            owner.body.insert(0, owner.body.pop())

    changed = _receiving_changed_units(units, path, change)
    classes = _receiving_batch_classes(changed)
    if fault == "detached":
        classes["ContinuationBatchState"] = copy.deepcopy(classes["ContinuationBatchState"])
    failures, fields = fencing._reserve_batch_retained_field_partition(changed, classes)
    assert failures and fields == {}
    assert all(item.startswith("continuation batch retained partition ") for item in failures)
    assert fencing._reserve_batch_retained_field_partition(units, _receiving_batch_classes(units))[0] == []


@pytest.mark.parametrize("fault", ("include_live_owner", "include_service_failure", "omit_spent", "reorder"))
def test_receiving_batch_wire_registry_cannot_hide_or_retain_another_field(fault):
    units = fencing._production_units()
    assert fencing._reserve_continuation_batch_state_receipt(units)[0] == []
    path = "src/elspeth/web/composer/continuation_batch_data.py"

    def change(unit):
        registry = fencing._reserve_node_at(unit, "_FIELDS").value
        fields = next(value for key, value in zip(registry.keys, registry.values, strict=True) if key.value == "ContinuationBatchState")
        if fault == "include_live_owner":
            fields.elts.append(ast.Constant(value="manual_advisor_allowance"))
        elif fault == "include_service_failure":
            fields.elts.append(ast.Constant(value="service_failure"))
        elif fault == "omit_spent":
            fields.elts = [item for item in fields.elts if item.value != "advisor_calls_used"]
        else:
            fields.elts[0], fields.elts[1] = fields.elts[1], fields.elts[0]

    changed = _receiving_changed_units(units, path, change)
    failures, receipt = fencing._reserve_continuation_batch_state_receipt(changed)
    assert failures == ["continuation batch DATA complete ordered registry fields changed ContinuationBatchState"]
    assert receipt == {}
    assert fencing._reserve_continuation_batch_state_receipt(units)[0] == []


@pytest.mark.parametrize("fault", ("missing", "late", "count", "receiver", "foreign_import"))
def test_receiving_batch_partition_requires_the_actual_nominal_settled_capture_guard(fault):
    units = fencing._production_units()
    path = "src/elspeth/web/composer/continuation_batch_data.py"

    def change(unit):
        joint = fencing._reserve_node_at(unit, "_joint")
        guard = joint.body[3]
        assert ast.unparse(guard.test) == "batch.manual_advisor_allowance is not None"
        if fault == "missing":
            joint.body.remove(guard)
        elif fault == "late":
            joint.body.append(joint.body.pop(3))
        elif fault == "count":
            guard.body[-1].value.keywords[0].value = ast.Constant(value=0)
        elif fault == "receiver":
            guard.body[-1].value.func.value = ast.Name(id="foreign", ctx=ast.Load())
        else:
            imported = next(
                node for node in unit.tree.body if isinstance(node, ast.ImportFrom) and node.module.endswith("manual_advisor_allowance")
            )
            imported.module = "foreign_allowance"

    changed = _receiving_changed_units(units, path, change)
    failures, fields = fencing._reserve_batch_retained_field_partition(changed, _receiving_batch_classes(changed))
    assert failures and fields == {}
    assert all(item.startswith("continuation batch retained partition ") for item in failures)
    assert fencing._reserve_batch_retained_field_partition(units, _receiving_batch_classes(units))[0] == []


@pytest.mark.parametrize("fault", ("foreign_import", "receiver", "rebound", "supplier", "extra_site", "finally_close"))
def test_receiving_manual_reserve_is_one_original_disjoint_call_with_closed_ownership(fault):
    units = fencing._production_units()
    failures, original = fencing._reserve_manual_advisor_allowance_receipt(units)
    assert failures == [] and len(original) == 1
    assert fencing._reserve_current_call_inventory(units)[0] == []
    path = "src/elspeth/web/composer/manual_advisor_allowance.py" if fault == "supplier" else "src/elspeth/web/composer/tool_batch.py"

    def change(unit):
        if fault == "supplier":
            reserve = fencing._reserve_node_at(unit, "ManualAdvisorAllowance.reserve")
            reserve.body.insert(0, ast.parse("self.used = 0").body[0])
            return
        execute = fencing._reserve_node_at(unit, "_execute_continuation_call_live")
        if fault == "foreign_import":
            imported = next(
                node for node in unit.tree.body if isinstance(node, ast.ImportFrom) and node.module.endswith("manual_advisor_allowance")
            )
            imported.module = "foreign_allowance"
        elif fault == "extra_site":
            unit.tree.body.append(ast.parse("def extra(value):\n    return value.reserve()\n").body[0])
        elif fault == "finally_close":
            close = next(
                node
                for node in ast.walk(execute)
                if isinstance(node, ast.Try)
                and any(isinstance(item, ast.Expr) and ast.unparse(item.value) == "reservation.close()" for item in node.finalbody)
            )
            close.finalbody.pop(0)
        else:
            call = next(node for node in ast.walk(execute) if isinstance(node, ast.Call) and ast.unparse(node.func) == "allowance.reserve")
            if fault == "receiver":
                call.func.value.id = "foreign"
            else:
                execute.body.insert(0, ast.parse("allowance = foreign").body[0])

    changed = _receiving_changed_units(units, path, change)
    failures, receipt = fencing._reserve_manual_advisor_allowance_receipt(changed)
    assert failures and receipt == frozenset()
    assert all(item.startswith("manual Advisor reservation ") for item in failures)
    assert fencing._reserve_current_call_inventory(changed)[0]
    assert fencing._reserve_manual_advisor_allowance_receipt(units) == ([], original)


def test_receiving_handoff_reserve_is_the_actual_locked_return_and_inventory_member():
    units = fencing._production_units()
    assert fencing._reserve_coordinator_self_slice_failures(units) == []
    failures, receipt = fencing._reserve_coordinator_self_call_receipts(units)
    assert failures == []
    unit = next(unit for unit in units if unit.path == "src/elspeth/web/required_work.py")
    method = fencing._reserve_node_at(unit, "RequiredWorkCoordinator.prepare_continuation_handoff")
    returned = method.body[-1].body[-1]
    assert isinstance(returned, ast.Return) and ast.unparse(returned.value.func) == "self.reserve"
    assert (unit.path, id(returned.value)) in receipt
    failures, inventory = fencing._reserve_current_call_inventory(units)
    assert failures == [] and receipt == inventory[unit.path]
    manual_failures, manual = fencing._reserve_manual_advisor_allowance_receipt(units)
    assert manual_failures == [] and manual == inventory["src/elspeth/web/composer/tool_batch.py"]
    assert not receipt & manual


@pytest.mark.parametrize(
    "fault",
    ("completion", "source", "recurrence", "outside_lock", "early_barrier", "rebound", "extra_call", "deferred", "unreviewed_method"),
)
def test_receiving_handoff_return_permission_does_not_admit_other_self_reservations(fault):
    units = fencing._production_units()
    assert fencing._reserve_coordinator_self_slice_failures(units) == []

    def change(unit):
        method = fencing._reserve_node_at(unit, "RequiredWorkCoordinator.prepare_continuation_handoff")
        locked = method.body[-1]
        returned = locked.body[-1]
        if fault == "completion":
            locked.body.pop(0)
        elif fault == "source":
            returned.value.args[0].attr = "LEASE_RELEASE"
        elif fault == "recurrence":
            returned.value.keywords[0].value = ast.Constant(value=0)
        elif fault == "outside_lock":
            method.body.append(locked.body.pop())
        elif fault == "early_barrier":
            locked.body.insert(0, ast.parse("self._release_prepared = True").body[0])
        elif fault == "rebound":
            locked.body.insert(0, ast.parse("self = foreign").body[0])
        elif fault == "extra_call":
            locked.body.insert(0, ast.parse("other = self.reserve(RequiredWorkSource.LEASE_RELEASE)").body[0])
        elif fault == "deferred":
            locked.body[-1] = ast.parse("def deferred():\n    return self.reserve(RequiredWorkSource.CONTINUATION_HANDOFF_SQL)\n").body[0]
        else:
            owner = fencing._reserve_node_at(unit, "RequiredWorkCoordinator")
            owner.body.append(ast.parse("def unreviewed(self):\n    return self.reserve(RequiredWorkSource.LEASE_RELEASE)\n").body[0])

    changed = _receiving_changed_units(units, "src/elspeth/web/required_work.py", change)
    assert fencing._reserve_coordinator_self_slice_failures(changed)
    failures, receipt = fencing._reserve_coordinator_self_call_receipts(changed)
    assert failures and receipt == frozenset()
    assert fencing._reserve_coordinator_self_slice_failures(units) == []


def _receiving_deadline_opposition(path, symbol, before, after, diagnostic):
    units = fencing._production_units()
    assert fencing._reserve_coordinator_self_slice_failures(units) == []
    original_failures, original_receipt = fencing._reserve_coordinator_self_call_receipts(units)
    assert original_failures == [] and original_receipt
    original = next(unit for unit in units if unit.path == path)
    source_bytes = original.source.encode("utf-8")
    start, end = 0, len(source_bytes)
    if symbol is not None:
        node = fencing._reserve_node_at(original, symbol)
        assert isinstance(node, (ast.ClassDef, ast.FunctionDef))
        assert node.end_lineno is not None and node.end_col_offset is not None
        # AST columns are UTF-8 byte offsets. Bind to the named class/member's
        # actual span: identical getter text in another class is not a locator.
        lines = source_bytes.splitlines(keepends=True)
        start = sum(map(len, lines[: node.lineno - 1])) + node.col_offset
        end = sum(map(len, lines[: node.end_lineno - 1])) + node.end_col_offset
        assert source_bytes[start:end].decode("utf-8") == ast.get_source_segment(original.source, node)
    selected = source_bytes[start:end].decode("utf-8")
    assert selected.count(before) == 1 and before != after
    replacement = selected.replace(before, after, 1).encode("utf-8")
    changed_bytes = source_bytes[:start] + replacement + source_bytes[end:]
    assert changed_bytes[:start] == source_bytes[:start]
    assert changed_bytes[start + len(replacement) :] == source_bytes[end:]
    changed_source = changed_bytes.decode("utf-8")
    assert changed_source != original.source
    changed_unit = fencing._parse_source(path, changed_source)
    if symbol is not None:
        changed_node = fencing._reserve_node_at(changed_unit, symbol)
        assert isinstance(changed_node, (ast.ClassDef, ast.FunctionDef))
        assert ast.get_source_segment(changed_source, changed_node) == replacement.decode("utf-8")
    changed = tuple(changed_unit if unit is original else unit for unit in units)
    # Deliberately enter below _reserve_recipe_failures: a stale source recipe
    # cannot supply the expected semantic refusal or hide a broken positive.
    assert fencing._reserve_coordinator_self_slice_failures(changed) == [diagnostic]
    failures, receipt = fencing._reserve_coordinator_self_call_receipts(changed)
    assert failures == ["owned coordinator receiver source is unqualified"] and receipt == frozenset()
    assert fencing._reserve_coordinator_self_slice_failures(units) == []
    assert fencing._reserve_coordinator_self_call_receipts(units) == ([], original_receipt)


@pytest.mark.parametrize(
    ("symbol", "before", "after", "diagnostic"),
    (
        (
            "RequiredWorkCoordinator.__init__",
            "sql_deadline_owner: ContinuationSQLDeadlineOwner | None = None",
            "sql_deadline_owner: ContinuationSQLDeadlineOwner | None = False",
            "owned coordinator method binding or authority getter changed",
        ),
        (
            "RequiredWorkCoordinator.__init__",
            "sql_deadline_owner: ContinuationSQLDeadlineOwner | None = None",
            "sql_deadline_owner: ContinuationSQLDeadlineOwner | None",
            "owned coordinator method binding or authority getter changed",
        ),
        (
            "RequiredWorkCoordinator.__init__",
            "*, sql_deadline_owner:",
            "*extra, sql_deadline_owner:",
            "owned coordinator method binding or authority getter changed",
        ),
        (
            "RequiredWorkCoordinator.__init__",
            "type(authority) is not RequiredWorkAuthority",
            "not isinstance(authority, RequiredWorkAuthority)",
            "owned coordinator exact authority guard is missing",
        ),
        (
            "RequiredWorkCoordinator.__init__",
            "RequiredAuthorityKind.DURABLE_COMPOSE",
            "RequiredAuthorityKind.MANUAL_PREVIEW",
            "owned coordinator SQL deadline context guard changed",
        ),
        (
            "RequiredWorkCoordinator.__init__",
            "type(sql_deadline_owner) is not ContinuationSQLDeadlineOwner",
            "not isinstance(sql_deadline_owner, ContinuationSQLDeadlineOwner)",
            "owned coordinator SQL deadline context guard changed",
        ),
        (
            "RequiredWorkCoordinator.__init__",
            "sql_deadline_owner.validate_context(",
            "other.validate_context(",
            "owned coordinator SQL deadline context guard changed",
        ),
        (
            "RequiredWorkCoordinator.__init__",
            "authority.context",
            "other.context",
            "owned coordinator SQL deadline context guard changed",
        ),
        (
            "RequiredWorkCoordinator.__init__",
            "operation_id=authority.durable_operation_id",
            "operation_id=None",
            "owned coordinator SQL deadline context guard changed",
        ),
        (
            "RequiredWorkCoordinator.__init__",
            "attempt=authority.claim_attempt",
            "attempt=None",
            "owned coordinator SQL deadline context guard changed",
        ),
        (
            "RequiredWorkCoordinator.__init__",
            "if sql_deadline_owner is not None:",
            "self._sql_deadline_owner = sql_deadline_owner\n        if sql_deadline_owner is not None:",
            "owned coordinator SQL deadline context guard changed",
        ),
        (
            "RequiredWorkCoordinator.__init__",
            "self._sql_deadline_owner = sql_deadline_owner",
            "self._sql_deadline_owner = None",
            "owned coordinator constructor has an unreviewed value after guard",
        ),
        (
            "RequiredWorkCoordinator",
            '        "_sql_deadline_owner",\n',
            "",
            "owned coordinator SQL deadline storage layout changed",
        ),
        (
            "RequiredWorkCoordinator.reserve",
            ", sql_deadline_owner=self._sql_deadline_owner",
            "",
            "owned coordinator key/ticket registration origin changed",
        ),
        (
            "RequiredWorkCoordinator.reserve",
            "sql_deadline_owner=self._sql_deadline_owner",
            "sql_deadline_owner=other",
            "owned coordinator key/ticket registration origin changed",
        ),
        (
            "RequiredWorkCoordinator.reserve",
            "with self._lock:",
            "with other._lock:",
            "owned coordinator physical lock origin changed",
        ),
        (
            "RequiredWorkCoordinator.reserve",
            "self._release_prepared or key in self._tickets",
            "key in self._tickets",
            "owned coordinator key/ticket registration origin changed",
        ),
        (
            "RequiredWorkCoordinator.reserve",
            "self._tickets[key] = ticket",
            "self._tickets[other] = ticket",
            "owned coordinator key/ticket registration origin changed",
        ),
        (
            "RequiredWorkCoordinator.reserve",
            "return ticket",
            "return other",
            "owned coordinator key/ticket registration origin changed",
        ),
    ),
)
def test_receiving_deadline_coordinator_keeps_original_owner_and_reservation(symbol, before, after, diagnostic):
    _receiving_deadline_opposition("src/elspeth/web/required_work.py", symbol, before, after, diagnostic)


@pytest.mark.parametrize(
    ("before", "after"),
    (
        (
            "type(sql_deadline_owner) is not ContinuationSQLDeadlineOwner",
            "not isinstance(sql_deadline_owner, ContinuationSQLDeadlineOwner)",
        ),
        ("or coordinator is None", "or False"),
        ("coordinator.sql_deadline_owner is not sql_deadline_owner", "coordinator.sql_deadline_owner != sql_deadline_owner"),
        ("sql_deadline_owner.validate_context(", "other.validate_context("),
        ("key.authority.context", "other.context"),
        ("operation_id=key.authority.durable_operation_id", "operation_id=None"),
        ("attempt=key.authority.claim_attempt", "attempt=None"),
        ("self._sql_deadline_owner = sql_deadline_owner", "self._sql_deadline_owner = None"),
        (
            "if sql_deadline_owner is not None:",
            "self._sql_deadline_owner = sql_deadline_owner\n        if sql_deadline_owner is not None:",
        ),
    ),
)
def test_receiving_deadline_ticket_requires_same_coordinator_and_context(before, after):
    _receiving_deadline_opposition(
        "src/elspeth/web/required_work.py",
        "RequiredWorkTicket.__init__",
        before,
        after,
        "owned SQL deadline ticket identity or context changed",
    )


@pytest.mark.parametrize("class_name", ("RequiredWorkCoordinator", "RequiredWorkTicket"))
def test_receiving_deadline_getter_cannot_substitute_an_equal_or_foreign_owner(class_name):
    _receiving_deadline_opposition(
        "src/elspeth/web/required_work.py",
        class_name + ".sql_deadline_owner",
        "return self._sql_deadline_owner",
        "return other",
        "owned SQL deadline getter changed",
    )


@pytest.mark.parametrize("class_name", ("RequiredWorkCoordinator", "RequiredWorkTicket", "ContinuationSQLDeadlineOwner"))
@pytest.mark.parametrize("operation", ("store", "delete", "reflect"))
def test_receiving_deadline_validated_owner_cannot_be_rebound(class_name, operation):
    deadline = class_name == "ContinuationSQLDeadlineOwner"
    path = "src/elspeth/web/sessions/continuation_sql_deadline.py" if deadline else "src/elspeth/web/required_work.py"
    field = "running" if deadline else "_sql_deadline_owner"
    effect = {
        "store": f"self.{field} = other",
        "delete": f"del self.{field}",
        "reflect": f"setattr(self, '{field}', other)",
    }[operation]
    # Insert an ordinary extra method, rather than corrupting a getter first.
    before = "    def __init__("
    after = f"    def replace_deadline(self):\n        {effect}\n\n" + before
    diagnostic = "owned SQL deadline protected field is rebound"
    if class_name == "RequiredWorkCoordinator":
        diagnostic = (
            "owned coordinator reflected self mutation is unreviewed"
            if operation == "reflect"
            else "owned coordinator protected field is rebound outside constructor"
        )
    _receiving_deadline_opposition(path, class_name, before, after, diagnostic)


def test_receiving_deadline_ticket_constructor_cannot_replace_owner_after_validation():
    _receiving_deadline_opposition(
        "src/elspeth/web/required_work.py",
        "RequiredWorkTicket.__init__",
        "self._lock = RLock()",
        "self._sql_deadline_owner = other\n        self._lock = RLock()",
        "owned SQL deadline protected field is rebound",
    )


@pytest.mark.parametrize("class_name", ("RequiredWorkCoordinator", "RequiredWorkTicket", "ContinuationSQLDeadlineOwner"))
def test_receiving_deadline_owner_access_cannot_be_intercepted(class_name):
    path = (
        "src/elspeth/web/sessions/continuation_sql_deadline.py"
        if class_name == "ContinuationSQLDeadlineOwner"
        else "src/elspeth/web/required_work.py"
    )
    diagnostic = (
        "owned coordinator method binding or authority getter changed"
        if class_name == "RequiredWorkCoordinator"
        else "owned SQL deadline class or descriptor binding changed"
    )
    _receiving_deadline_opposition(
        path,
        class_name,
        "    def __init__(",
        "    def __getattribute__(self, name):\n        return other\n\n    def __init__(",
        diagnostic,
    )


@pytest.mark.parametrize(
    ("symbol", "before", "after"),
    (
        ("validate_context", "context is not self.running.session_operation_context", "context != self.running.session_operation_context"),
        ("validate_context", "or operation_id != self.running.claim.operation_id", "or False"),
        ("validate_context", "or attempt != self.running.claim.attempt", "or False"),
        ("validate_context", "self.running.claim.operation_id", "other.claim.operation_id"),
        ("__init__", "type(running) is not ComposerOperationRunning", "not isinstance(running, ComposerOperationRunning)"),
        ("__init__", "running, policy, drain_signal", "other, policy, drain_signal"),
    ),
)
def test_receiving_deadline_validator_requires_actual_running_identity(symbol, before, after):
    _receiving_deadline_opposition(
        "src/elspeth/web/sessions/continuation_sql_deadline.py",
        "ContinuationSQLDeadlineOwner." + symbol,
        before,
        after,
        "owned SQL deadline running context contract changed",
    )


@pytest.mark.parametrize(
    ("path", "before", "after"),
    (
        (
            "src/elspeth/web/required_work.py",
            "from elspeth.web.sessions.continuation_sql_deadline import ContinuationSQLDeadlineOwner",
            "from foreign.deadline import ContinuationSQLDeadlineOwner",
        ),
        (
            "src/elspeth/web/sessions/continuation_sql_deadline.py",
            "from elspeth.web.sessions.composer_operations import ComposerOperationRunning",
            "from foreign.operations import ComposerOperationRunning",
        ),
    ),
)
def test_receiving_deadline_nominal_type_is_bound_to_its_actual_import(path, before, after):
    _receiving_deadline_opposition(path, None, before, after, "owned SQL deadline imported binding changed")


def _receiving_p4_custody_opposition(path, symbol, before, after, diagnostic, *, call_name=None):
    units = fencing._production_units()
    failures, original_receipt = fencing._reserve_p4_batch_custody_receipt(units)
    assert failures == [] and original_receipt["receiver_uses"] and len(original_receipt["native_calls"]) == 4
    original = next(unit for unit in units if unit.path == path)
    node = fencing._reserve_node_at(original, symbol)
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    if call_name is not None:
        calls = [part for part in ast.walk(node) if isinstance(part, ast.Call) and ast.unparse(part.func) == call_name]
        assert len(calls) == 1
        node = calls[0]
    segment = ast.get_source_segment(original.source, node)
    assert segment is not None and segment.count(before) == 1 and before != after
    assert original.source.count(segment) == 1
    changed_source = original.source.replace(segment, segment.replace(before, after, 1), 1)
    assert changed_source != original.source
    changed_unit = fencing._parse_source(path, changed_source)
    changed = tuple(changed_unit if unit is original else unit for unit in units)
    failures, receipt = fencing._reserve_p4_batch_custody_receipt(changed)
    assert failures == ["P4 batch custody " + diagnostic] and receipt == {}
    assert fencing._reserve_p4_batch_custody_receipt(units) == ([], original_receipt)


@pytest.mark.parametrize(
    ("symbol", "before", "after", "diagnostic"),
    (
        (
            "_execute_continuation_call_live",
            "failure_custody.batch is not batch",
            "failure_custody.batch != batch",
            "actual batch/call/audit identity changed",
        ),
        (
            "_execute_continuation_call_live",
            "failure_custody.call is not call",
            "failure_custody.call != call",
            "actual batch/call/audit identity changed",
        ),
        ("_execute_continuation_call_live", "else failure_custody.observation", "else other.observation", "observation origin changed"),
        (
            "_execute_continuation_call_live",
            "observation.require(recorder, call.audit)",
            "observation.require(other, call.audit)",
            "actual batch/call/audit identity changed",
        ),
        (
            "_execute_continuation_call_live",
            "on_provider_dispatch=reservation.dispatch",
            "on_provider_dispatch=other.dispatch",
            "physical dispatch callback changed",
        ),
        ("_execute_continuation_call_live", "reservation.close()", "pass", "manual reservation finally close changed"),
        (
            "_execute_continuation_call_live",
            "batch, call, recorder, llm_messages, ctx.policy_catalog",
            "batch, call, other, llm_messages, ctx.policy_catalog",
            "publication owner/context changed",
        ),
        (
            "_execute_continuation_call_live",
            "publication.register_feedback(serialized=tool_feedback_json, message=feedback_message)",
            "publication.register_feedback(serialized=tool_feedback_json, message=other)",
            "exact feedback/outcome publication changed",
        ),
        ("_execute_continuation_call_live", "tool_index=call.index", "tool_index=0", "exact result publication changed"),
        (
            "_execute_continuation_call_live",
            "except BaseException as error:",
            "except Exception as error:",
            "original/grouped exception transfer changed",
        ),
        (
            "_execute_continuation_call_live",
            "raise combined from combined.__cause__\n        raise",
            "raise error\n        raise",
            "original/grouped exception transfer changed",
        ),
        (
            "_execute_continuation_call_live",
            "if failure_custody is None:\n            batch.plugin_crash",
            "if failure_custody is not None:\n            batch.plugin_crash",
            "failure-owned copyback changed",
        ),
        (
            "_finish_continuation_batch_live",
            "failure_custody.batch is not batch",
            "failure_custody.batch != batch",
            "completed failure origin changed",
        ),
        (
            "_finish_continuation_batch_live",
            "advisor_origin = failure_custody.original",
            "advisor_origin = other",
            "completed failure origin changed",
        ),
        (
            "_finish_continuation_batch_live",
            "if not isinstance(advisor_origin, Exception):",
            "if False:",
            "completed failure origin changed",
        ),
    ),
)
def test_receiving_p4_batch_custody_preserves_exact_dispatch_and_failure_transfers(symbol, before, after, diagnostic):
    _receiving_p4_custody_opposition("src/elspeth/web/composer/tool_batch.py", symbol, before, after, diagnostic)


@pytest.mark.parametrize("replacement", ("failure_observation=None", "other_observation=observation"))
def test_receiving_p4_advisor_forwards_the_same_actual_failure_observation(replacement):
    _receiving_p4_custody_opposition(
        "src/elspeth/web/composer/tool_batch.py",
        "_execute_continuation_call_live",
        "failure_observation=observation",
        replacement,
        "exact observation forwarding changed",
        call_name="ctx.advisor_checkpoint._call_advisor_for_tool",
    )


@pytest.mark.parametrize("method", ("_execute_continuation_call_live", "_finish_continuation_batch_live"))
def test_receiving_p4_custody_default_cannot_be_foreign(method):
    _receiving_p4_custody_opposition(
        "src/elspeth/web/composer/tool_batch.py",
        method,
        "failure_custody: ContinuationToolFailureCustody | None = None",
        "failure_custody: ContinuationToolFailureCustody | None = foreign",
        "native signature changed " + method,
    )


@pytest.mark.parametrize(
    ("symbol", "before", "after", "diagnostic"),
    (
        (
            "ContinuationLivePhases._execute_one_owned",
            "inventory.running, self.work, result.response, batch, data.calls, live.recorder, call",
            "inventory.running, self.work, result.response, other, data.calls, live.recorder, call",
            "actual issued custody constructor/forwarding changed",
        ),
        (
            "ContinuationLivePhases._execute_one_owned",
            "failure_custody=self.failure_custody",
            "failure_custody=other",
            "actual issued custody constructor/forwarding changed",
        ),
        (
            "ContinuationLivePhases._persist_batch",
            "failure_custody=self.failure_custody",
            "failure_custody=other",
            "actual finish custody forwarding changed",
        ),
    ),
)
def test_receiving_p4_actual_native_callers_keep_the_original_custody(symbol, before, after, diagnostic):
    _receiving_p4_custody_opposition("src/elspeth/web/composer/continuation_live_phases.py", symbol, before, after, diagnostic)


@pytest.mark.parametrize(
    ("path", "symbol", "before", "after", "diagnostic"),
    (
        (
            "src/elspeth/web/composer/continuation_tool_failure.py",
            "ContinuationToolFailureCustody.__post_init__",
            "self.batch.current_call is not self.call",
            "self.batch.current_call != self.call",
            "nominal supplier identity changed",
        ),
        (
            "src/elspeth/web/composer/continuation_tool_failure.py",
            "ContinuationDispatchFailureObservation.require",
            "recorder is not self.owner.recorder",
            "recorder != self.owner.recorder",
            "nominal supplier identity changed",
        ),
        (
            "src/elspeth/web/composer/continuation_tool_failure.py",
            "ContinuationToolFailureCustody.failure",
            "return combine_failure_objects(original, *self.errors)",
            "return error",
            "exception supplier changed ContinuationToolFailureCustody.failure",
        ),
        (
            "src/elspeth/web/composer/continuation_prompt_producers.py",
            "ContinuationToolMessagePublication.require_call",
            "self.messages is not messages",
            "self.messages != messages",
            "publication supplier identity changed",
        ),
    ),
)
def test_receiving_p4_suppliers_cannot_substitute_equal_owners_or_drop_original_errors(path, symbol, before, after, diagnostic):
    _receiving_p4_custody_opposition(path, symbol, before, after, diagnostic)


@pytest.fixture(scope="module")
def receiving_attachment_prerequisite_units():
    return fencing._production_units()


def _receiving_runtime_initialization_opposition(units, path, symbol, before, after, diagnostic):
    transfers = fencing._reserve_continuation_transfers
    failures, positive = transfers.runtime_initialization_prerequisite_transfers(units)
    assert failures == [], failures
    assert positive["checked_nodes"]
    original = next(unit for unit in units if unit.path == path)
    owner = transfers._custody_node_at(original, symbol)
    assert isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
    segment = ast.get_source_segment(original.source, owner)
    assert segment is not None and segment.count(before) == 1 and before != after
    assert original.source.count(segment) == 1
    changed = fencing._parse_source(path, original.source.replace(segment, segment.replace(before, after, 1), 1))
    assert ast.dump(changed.tree) != ast.dump(original.tree)
    mutated = tuple(changed if unit is original else unit for unit in units)
    failures, refused = transfers.runtime_initialization_prerequisite_transfers(mutated)
    assert failures == [diagnostic]
    assert refused == {}
    assert transfers.runtime_initialization_prerequisite_transfers(units) == ([], positive)


def test_receiving_runtime_initialization_prerequisites_have_a_direct_positive(receiving_attachment_prerequisite_units):
    units = receiving_attachment_prerequisite_units
    failures, receipt = fencing._reserve_continuation_transfers.runtime_initialization_prerequisite_transfers(units)
    assert failures == [], failures
    assert set(receipt) == {"checked_nodes"}
    assert receipt["checked_nodes"]
    actual = {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}
    assert receipt["checked_nodes"] <= actual
    # No selected-definition, SQL, callback or native effect permission.


@pytest.mark.parametrize(
    ("symbol", "before", "after"),
    (
        ("_restore_live", "attachments = source.attachments", "attachments = foreign.attachments"),
        (
            "_restore_live",
            "authority.read_continuation_runtime(inventory.running, selected=selected, inventory=inventory)",
            "None",
        ),
        (
            "_restore_live",
            "finish_read.require_selection(selected, inventory)\n        finish_read.read_runtime(authority)",
            "finish_read.read_runtime(authority)\n        finish_read.require_selection(selected, inventory)",
        ),
        (
            "restore_continuation_runtime",
            "type(finish_read) is not ContinuationFinishRead",
            "not isinstance(finish_read, ContinuationFinishRead)",
        ),
        (
            "restore_continuation_runtime",
            "finish_read.require_selection(selected, inventory)",
            "finish_read.require_selection(foreign, inventory)",
        ),
        ("restore_continuation_runtime", "else finish_read.read_runtime(authority)", "else finish_read.read_runtime(foreign)"),
        (
            "restore_continuation_runtime",
            "finish_read: ContinuationFinishRead | None = None",
            "finish_read: ContinuationFinishRead | None = foreign",
        ),
        (
            "restore_continuation_runtime",
            "required_work=required_work, finish_read=finish_read",
            "required_work=required_work, finish_read=None",
        ),
        (
            "restore_continuation_runtime",
            "progress=progress,\n            finish_read=finish_read,",
            "progress=progress,\n            finish_read=None,",
        ),
        (
            "restore_continuation_runtime",
            "required_work,\n        ContinuationRuntimeSource,",
            "foreign,\n        ContinuationRuntimeSource,",
        ),
        ("restore_continuation_runtime", "if type(result) is not RestoredContinuationRuntime:", "if False:"),
    ),
)
def test_receiving_runtime_finish_read_keeps_actual_nominal_owners(receiving_attachment_prerequisite_units, symbol, before, after):
    path = fencing._reserve_continuation_transfers.RUNTIME
    _receiving_runtime_initialization_opposition(
        receiving_attachment_prerequisite_units,
        path,
        symbol,
        before,
        after,
        "runtime initialization exact caller changed " + path + ":" + symbol,
    )


@pytest.mark.parametrize(
    ("before", "after"),
    (
        ("source.admitted_evidence.snapshot is not source.evidence", "source.admitted_evidence.snapshot != source.evidence"),
        ("decode_recorder_snapshot(source.admitted_evidence.recorder_chunk)", "decode_recorder_snapshot(foreign)"),
        ("prior_application_entry=source.evidence.application_entry", "prior_application_entry=None"),
        (
            "materialization=runtime.materialization,\n        mandatory_retention=True,",
            "materialization=runtime.materialization,\n        mandatory_retention=False,",
        ),
        (
            "encode_turn_evidence(admitted, env_ref_names=names, mandatory_retention=True)",
            "encode_turn_evidence(admitted, env_ref_names=names)",
        ),
        (
            "encode_message_shelf(shelf.transcript, mandatory_retention=True, env_ref_names=names)",
            "encode_message_shelf(shelf.transcript, mandatory_retention=False, env_ref_names=names)",
        ),
        ('codec="composer.tools.v1", value=live.tools', 'codec="composer.tools.v1", value=foreign'),
        ('codec="composer.state.v1", value=live.state.to_dict()', 'codec="composer.state.v1", value=foreign'),
        ('"current_indexes": shelf.transcript.current_indexes', '"current_indexes": foreign'),
        (
            'encode_mandatory_private_chunk(codec="composer.tools.v1", value=live.tools, env_ref_names=names)',
            'encode_mandatory_private_chunk(codec="composer.tools.v1", value=live.tools, env_ref_names=frozenset())',
        ),
        ("audit = admitted.recorder_chunk", "audit = foreign"),
        ("captured_graph = admitted.graph", "captured_graph = runtime.graph"),
        ("invocations=actual_recorder.invocations[: len(current_recorder.invocations)]", "invocations=actual_recorder.invocations"),
    ),
)
def test_receiving_runtime_capture_keeps_admitted_snapshot_and_mandatory_inputs(receiving_attachment_prerequisite_units, before, after):
    path = fencing._reserve_continuation_transfers.RUNTIME
    _receiving_runtime_initialization_opposition(
        receiving_attachment_prerequisite_units,
        path,
        "_capture_owners",
        before,
        after,
        "runtime initialization exact caller changed " + path + ":_capture_owners",
    )


@pytest.mark.parametrize(
    ("before", "after"),
    (
        ("[refused, failure]", "[failure]"),
        ("[refused, failure]", "[refused]"),
        ("except BaseException as failure:", "except Exception as failure:"),
        ("except ComposerContextUnsupported as refused:", "except Exception as refused:"),
        ("state=live.state", "state=foreign"),
        ("source.evidence.effects.latest_verified_state_json", "foreign"),
        ("live.recorder.invocations[runtime.drained.invocations :]", "live.recorder.invocations"),
        ("live.recorder.llm_calls[runtime.drained.llm_calls :]", "live.recorder.llm_calls"),
        ("failed_turn=live.failed_turn", "failed_turn=None"),
        (
            "source = authority.read_continuation_runtime(running, selected=selected, inventory=inventory)",
            "source = foreign",
        ),
        (
            "try:\n                authority.read_continuation_runtime(running, selected=selected, inventory=inventory)",
            "try:\n                pass",
        ),
        (
            "authority.read_continuation_runtime(running, selected=selected, inventory=inventory)\n        return captured",
            "return captured",
        ),
        ("            ) from refused", "            ) from None"),
        ("_registered_read(work, CapturedContinuationRuntime, capture)", "_registered_read(foreign, CapturedContinuationRuntime, capture)"),
    ),
)
def test_receiving_runtime_capture_failure_preserves_both_original_outcomes(receiving_attachment_prerequisite_units, before, after):
    path = fencing._reserve_continuation_transfers.RUNTIME
    _receiving_runtime_initialization_opposition(
        receiving_attachment_prerequisite_units,
        path,
        "capture_continuation_runtime_owners",
        before,
        after,
        "runtime initialization exact caller changed " + path + ":capture_continuation_runtime_owners",
    )


@pytest.mark.parametrize(
    ("before", "after"),
    (
        ("expected_recorder=empty_recorder", "expected_recorder=None"),
        (
            "BufferingRecorder().snapshot_for_continuation(drained=RecorderDrainCursors())",
            "live.recorder.snapshot_for_continuation(drained=owners.drained)",
        ),
        (
            "mandatory_retention=True,\n        expected_recorder=empty_recorder",
            "mandatory_retention=False,\n        expected_recorder=empty_recorder",
        ),
        ("audit = admitted.recorder_chunk", "audit = empty_recorder"),
        ("canonical=original.snapshot.initial_state_json", "canonical=foreign"),
        ("materialization.inputs.source is not source", "materialization.inputs.source != source"),
    ),
)
def test_receiving_initial_capture_preserves_the_actual_empty_recorder(receiving_attachment_prerequisite_units, before, after):
    path = fencing._reserve_continuation_transfers.INITIAL_DATA
    _receiving_runtime_initialization_opposition(
        receiving_attachment_prerequisite_units,
        path,
        "capture_initial_composition",
        before,
        after,
        "runtime initialization exact caller changed " + path + ":capture_initial_composition",
    )


@pytest.mark.parametrize(
    ("before", "after"),
    (
        ("inputs.admitted_evidence,", "snapshot,"),
        ("_require_same_sources(before, after)", "pass"),
        ("env_ref_names=inventory.names,", "env_ref_names=frozenset(),"),
    ),
)
def test_receiving_first_runtime_uses_the_actual_admitted_evidence(receiving_attachment_prerequisite_units, before, after):
    path = fencing._reserve_continuation_transfers.FIRST_RUNTIME
    _receiving_runtime_initialization_opposition(
        receiving_attachment_prerequisite_units,
        path,
        "_restore_first_graph",
        before,
        after,
        "runtime initialization exact caller changed " + path + ":_restore_first_graph",
    )


@pytest.mark.parametrize(
    ("before", "after", "diagnostic"),
    (
        ("recorder_chunk != expected_recorder", "False", "moved empty-recorder guard changed"),
        (
            "expected_recorder: PrivateChunk | None = None",
            "expected_recorder: PrivateChunk | None = foreign",
            "moved capture signature changed",
        ),
        (
            "recorder.snapshot_for_continuation(drained=drained)",
            "recorder.snapshot_for_continuation(drained=foreign)",
            "moved empty-recorder guard changed",
        ),
        (
            "capture_application_recorder_source(recorder_snapshot, env_ref_names=env_ref_names)",
            "capture_application_recorder_source(foreign, env_ref_names=env_ref_names)",
            "moved empty-recorder guard changed",
        ),
        ("recorder_snapshot=recorder_snapshot,", "recorder_snapshot=foreign,", "moved empty-recorder guard changed"),
        ("mandatory_retention or prior_application_entry is not None", "mandatory_retention", "moved history/admitted return changed"),
        ("fields.application_entry is None", "False", "moved history/admitted return changed"),
        ("[: len(prior_application_entry.applications)]", "[: 0]", "moved history/admitted return changed"),
        (
            "!= prior_application_entry.applications",
            "!= prior_application_entry.applications[::-1]",
            "moved history/admitted return changed",
        ),
        ("recorder_chunk=recorder_chunk)", "recorder_chunk=foreign)", "moved history/admitted return changed"),
        ("source_chunks=(recorder_chunk, *sources.chunks)", "source_chunks=(recorder_chunk,)", "moved history/admitted return changed"),
        (
            "snapshot,\n        env_ref_names=env_ref_names,",
            "foreign,\n        env_ref_names=env_ref_names,",
            "moved history/admitted return changed",
        ),
        ("recorder=recorder_chunk.reference,", "recorder=foreign,", "moved fields forwarding changed recorder"),
        ("application_entry=application_entry,", "application_entry=foreign,", "moved fields forwarding changed application_entry"),
        ("joint_graph_json=joint_graph_json,", "joint_graph_json=foreign,", "moved fields forwarding changed joint_graph_json"),
        (
            "logical=logical,",
            "logical=(application_entry := foreign),",
            "moved fields supplier/local binding changed application_entry",
        ),
        (
            "historical_application_count=0 if application_entry is None else len(application_entry.applications)",
            "historical_application_count=0",
            "moved fields forwarding changed historical_application_count",
        ),
    ),
)
def test_receiving_capture_supplier_preserves_moved_recorder_and_history_guards(
    receiving_attachment_prerequisite_units, before, after, diagnostic
):
    _receiving_runtime_initialization_opposition(
        receiving_attachment_prerequisite_units,
        fencing._reserve_continuation_transfers.EVIDENCE_GRAPH,
        "capture_turn_evidence",
        before,
        after,
        "runtime initialization " + diagnostic,
    )


@pytest.mark.parametrize(
    ("addition", "diagnostic"),
    (
        ("recorder_chunk = foreign", "moved capture binding changed recorder_chunk"),
        ("prior_application_entry = None", "moved capture binding changed prior_application_entry"),
        ("expected_recorder = None", "moved capture binding changed expected_recorder"),
        ("capture_fields = foreign", "moved capture binding changed capture_fields"),
        ("ce = foreign", "moved capture supplier shadowed ce"),
        ("return foreign", "moved capture early/deferred return changed"),
        ("recorder_chunk.reference = foreign", "moved capture owner member changed"),
    ),
)
def test_receiving_capture_supplier_keeps_moved_guard_bindings(receiving_attachment_prerequisite_units, addition, diagnostic):
    _receiving_runtime_initialization_opposition(
        receiving_attachment_prerequisite_units,
        fencing._reserve_continuation_transfers.EVIDENCE_GRAPH,
        "capture_turn_evidence",
        "    effects = owner.effects",
        "    " + addition + "\n    effects = owner.effects",
        "runtime initialization " + diagnostic,
    )


def test_receiving_capture_supplier_refuses_nested_producer_transcript_materialization(receiving_attachment_prerequisite_units):
    _receiving_runtime_initialization_opposition(
        receiving_attachment_prerequisite_units,
        fencing._reserve_continuation_transfers.EVIDENCE_GRAPH,
        "capture_turn_evidence",
        "request_id=owner.request_id,",
        "request_id=(owner.transcript.snapshot_for_continuation(\n"
        "                messages, materialization=materialization, mandatory_retention=mandatory_retention\n"
        "            ), owner.request_id)[1],",
        "runtime initialization moved transcript materialization site/order changed",
    )


def test_receiving_capture_supplier_refuses_nested_materialization_with_changed_arguments(receiving_attachment_prerequisite_units):
    _receiving_runtime_initialization_opposition(
        receiving_attachment_prerequisite_units,
        fencing._reserve_continuation_transfers.EVIDENCE_GRAPH,
        "capture_turn_evidence",
        "request_id=owner.request_id,",
        "request_id=(owner.transcript.snapshot_for_continuation(\n"
        "                foreign, materialization=materialization, mandatory_retention=False\n"
        "            ), owner.request_id)[1],",
        "runtime initialization moved transcript materialization site/order changed",
    )


def test_receiving_capture_supplier_refuses_nested_fields_supplier_alias_rebinding(receiving_attachment_prerequisite_units):
    _receiving_runtime_initialization_opposition(
        receiving_attachment_prerequisite_units,
        fencing._reserve_continuation_transfers.EVIDENCE_GRAPH,
        "capture_turn_evidence",
        "request_id=owner.request_id,",
        "request_id=((ce := foreign), owner.request_id)[1],",
        "runtime initialization moved fields supplier/local binding changed ce",
    )


def test_receiving_capture_supplier_refuses_early_duplicate_transcript_materialization(receiving_attachment_prerequisite_units):
    _receiving_runtime_initialization_opposition(
        receiving_attachment_prerequisite_units,
        fencing._reserve_continuation_transfers.EVIDENCE_GRAPH,
        "capture_turn_evidence",
        "    recorder_snapshot = recorder.snapshot_for_continuation(drained=drained)",
        "    owner.transcript.snapshot_for_continuation(\n"
        "        messages, materialization=materialization, mandatory_retention=mandatory_retention\n"
        "    )\n"
        "    recorder_snapshot = recorder.snapshot_for_continuation(drained=drained)",
        "runtime initialization moved transcript materialization site/order changed",
    )


@pytest.mark.parametrize(
    ("expression", "label"),
    (
        ("recorder.snapshot_for_continuation(drained=drained)", "recorder snapshot"),
        ("capture_application_recorder_source(recorder_snapshot, env_ref_names=env_ref_names)", "recorder source capture"),
        (
            "capture_joint_evidence_graph(owner=owner, recorder=recorder, drained=drained, data=data, "
            "env_ref_names=env_ref_names, recorder_snapshot=recorder_snapshot, recorder_source=recorder_source)",
            "joint-graph capture",
        ),
        ("P4ApplicationSources(application_entry, owner._portable_p4_sources)", "application sources"),
        ("capture_fields()", "fields production"),
        (
            "owner.transcript.snapshot_for_continuation(foreign, materialization=materialization, mandatory_retention=False)",
            "transcript materialization",
        ),
        (
            "ce.require_evidence_fields_admission(foreign, env_ref_names=env_ref_names, sources=sources, recorder_chunk=recorder_chunk)",
            "fields admission",
        ),
        ("ce.TurnEvidenceSnapshot(**foreign, transcript=foreign)", "snapshot construction"),
        (
            "ce.admit_turn_evidence(foreign, env_ref_names=env_ref_names, mandatory_retention=mandatory_retention, "
            "source_chunks=(recorder_chunk, *sources.chunks))",
            "evidence admission",
        ),
    ),
)
def test_receiving_capture_supplier_refuses_extra_direct_moved_calls(receiving_attachment_prerequisite_units, expression, label):
    # Insert after the local producer definition, before the history guard.
    # Matching the callee must also catch duplicates with different arguments.
    original = "    fields = capture_fields() if mandatory_retention or prior_application_entry is not None else None"
    _receiving_runtime_initialization_opposition(
        receiving_attachment_prerequisite_units,
        fencing._reserve_continuation_transfers.EVIDENCE_GRAPH,
        "capture_turn_evidence",
        original,
        "    " + expression + "\n" + original,
        "runtime initialization moved " + label + " site/order changed",
    )


@pytest.mark.parametrize("fault", ("recorder_after_fields", "history_after_transcript"))
def test_receiving_capture_supplier_keeps_guard_order(receiving_attachment_prerequisite_units, fault):
    units = receiving_attachment_prerequisite_units
    transfers = fencing._reserve_continuation_transfers
    failures, positive = transfers.runtime_initialization_prerequisite_transfers(units)
    assert failures == [], failures
    original = next(unit for unit in units if unit.path == transfers.EVIDENCE_GRAPH)
    changed = fencing._parse_source(original.path, original.source)
    owner = transfers._custody_node_at(changed, "capture_turn_evidence")
    assert isinstance(owner, ast.FunctionDef)
    if fault == "recorder_after_fields":
        starts = [
            index
            for index, node in enumerate(owner.body)
            if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "recorder_snapshot"
        ]
        assert len(starts) == 1
        prefix = owner.body[starts[0] : starts[0] + 5]
        assert ast.dump(ast.Module(body=prefix, type_ignores=[])) == ast.dump(ast.parse(transfers._CAPTURE_RECORDER_GUARD))
        del owner.body[starts[0] : starts[0] + 5]
        fields = [node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "capture_fields"]
        assert len(fields) == 1
        position = owner.body.index(fields[0]) + 1
        owner.body[position:position] = prefix
        diagnostic = "runtime initialization moved recorder/fields/history order changed"
    else:
        tail = owner.body[-6:]
        assert ast.dump(ast.Module(body=tail, type_ignores=[])) == ast.dump(ast.parse(transfers._CAPTURE_HISTORY_GUARD_AND_RETURN))
        owner.body[-5], owner.body[-4] = owner.body[-4], owner.body[-5]
        diagnostic = "runtime initialization moved history/admitted return changed"
    assert ast.dump(changed.tree) != ast.dump(original.tree)
    mutated = tuple(changed if unit is original else unit for unit in units)
    failures, refused = transfers.runtime_initialization_prerequisite_transfers(mutated)
    assert failures == [diagnostic]
    assert refused == {}
    assert transfers.runtime_initialization_prerequisite_transfers(units) == ([], positive)


@pytest.mark.parametrize("fault", ("missing", "duplicate", "supplier_rebound"))
def test_receiving_runtime_initialization_requires_its_actual_supplier_units(receiving_attachment_prerequisite_units, fault):
    units = receiving_attachment_prerequisite_units
    transfers = fencing._reserve_continuation_transfers
    failures, positive = transfers.runtime_initialization_prerequisite_transfers(units)
    assert failures == [], failures
    if fault == "supplier_rebound":
        path = "src/elspeth/web/composer/continuation_finish_reads.py"
        original = next(unit for unit in units if unit.path == path)
        changed = fencing._parse_source(path, original.source + "\nContinuationFinishRead, other = foreign\n")
        mutated = tuple(changed if unit is original else unit for unit in units)
        diagnostic = "runtime initialization supplier definition changed " + path + ":ContinuationFinishRead"
    else:
        original = next(unit for unit in units if unit.path == transfers.EVIDENCE_GRAPH)
        mutated = tuple(unit for unit in units if unit is not original) if fault == "missing" else (*units, original)
        diagnostic = "runtime initialization prerequisite source set missing or duplicated"
    failures, refused = transfers.runtime_initialization_prerequisite_transfers(mutated)
    assert failures == [diagnostic]
    assert refused == {}
    assert transfers.runtime_initialization_prerequisite_transfers(units) == ([], positive)


@pytest.mark.parametrize(
    ("path", "name"),
    (
        ("src/elspeth/web/composer/continuation_runtime.py", "encode_mandatory_private_chunk"),
        ("src/elspeth/web/composer/continuation_composition_initialization_data.py", "capture_turn_evidence"),
        ("src/elspeth/web/composer/continuation_first_runtime.py", "_finish_current_evidence"),
        ("src/elspeth/web/composer/continuation_evidence_graph.py", "ce"),
    ),
)
def test_receiving_runtime_initialization_keeps_canonical_supplier_bindings(receiving_attachment_prerequisite_units, path, name):
    units = receiving_attachment_prerequisite_units
    transfers = fencing._reserve_continuation_transfers
    failures, positive = transfers.runtime_initialization_prerequisite_transfers(units)
    assert failures == [], failures
    original = next(unit for unit in units if unit.path == path)
    changed = fencing._parse_source(path, original.source + "\n" + name + " = foreign\n")
    mutated = tuple(changed if unit is original else unit for unit in units)
    failures, refused = transfers.runtime_initialization_prerequisite_transfers(mutated)
    assert failures == ["runtime initialization canonical supplier changed " + path + ":" + name]
    assert refused == {}
    assert transfers.runtime_initialization_prerequisite_transfers(units) == ([], positive)


def test_receiving_runtime_initialization_receipt_stays_on_the_supplied_ast(receiving_attachment_prerequisite_units):
    units = receiving_attachment_prerequisite_units
    transfers = fencing._reserve_continuation_transfers
    failures, original_receipt = transfers.runtime_initialization_prerequisite_transfers(units)
    assert failures == [], failures
    original = next(unit for unit in units if unit.path == transfers.EVIDENCE_GRAPH)
    detached = fencing._parse_source(original.path, original.source)
    changed = tuple(detached if unit is original else unit for unit in units)
    failures, receipt = transfers.runtime_initialization_prerequisite_transfers(changed)
    assert failures == [], failures
    old_sites = {site for site in original_receipt["checked_nodes"] if site[0] == original.path}
    current_sites = {site for site in receipt["checked_nodes"] if site[0] == original.path}
    assert old_sites and current_sites and old_sites.isdisjoint(current_sites)
    actual = {(unit.path, id(node)) for unit in changed for node in ast.walk(unit.tree)}
    assert receipt["checked_nodes"] <= actual
    assert set(receipt) == {"checked_nodes"}


def _receiving_attachment_projection_inner(units):
    """Exercise the inner grammar without substituting outer source seals."""
    transfers = fencing._reserve_continuation_transfers
    index = {unit.path: unit for unit in units}
    assert len(index) == len(units)
    selected = {}
    for path, symbol in (
        (transfers.ATTACHMENT_APPLICATION, "require_attachment_projection"),
        (transfers.ATTACHMENT_APPLICATION, "admit_attachment_application"),
        (transfers.FINISHER, "finish_current_attachment_result"),
    ):
        owner = transfers._custody_node_at(index[path], symbol)
        assert isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
        selected[path, symbol] = owner
    failures, receipt = [], transfers._empty()
    transfers._attachment_projection_transfers(units, failures, index, selected, receipt)
    return transfers._finish(failures, receipt)


def _receiving_attachment_projection_inner_opposition(units, before, after, diagnostics):
    failures, positive = _receiving_attachment_projection_inner(units)
    assert failures == [], failures
    assert positive["native_calls"]
    path = fencing._reserve_continuation_transfers.ATTACHMENT_APPLICATION
    original = next(unit for unit in units if unit.path == path)
    owner = fencing._reserve_node_at(original, "require_attachment_projection")
    assert isinstance(owner, ast.FunctionDef)
    segment = ast.get_source_segment(original.source, owner)
    assert segment is not None and segment.count(before) == 1 and before != after
    assert original.source.count(segment) == 1
    changed = fencing._parse_source(path, original.source.replace(segment, segment.replace(before, after, 1), 1))
    assert ast.dump(changed.tree) != ast.dump(original.tree)
    mutated = tuple(changed if unit is original else unit for unit in units)
    failures, refused = _receiving_attachment_projection_inner(mutated)
    assert failures == ["continuation attachment projection " + detail for detail in diagnostics]
    assert refused == {}
    assert _receiving_attachment_projection_inner(units) == ([], positive)


def test_receiving_attachment_projection_v2_inner_has_actual_positive(receiving_attachment_prerequisite_units):
    units = receiving_attachment_prerequisite_units
    failures, receipt = _receiving_attachment_projection_inner(units)
    assert failures == [], failures
    assert receipt["native_calls"] and receipt["data_constructor_calls"]
    actual = {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}
    assert receipt["native_calls"] | receipt["data_constructor_calls"] <= actual
    # This does not certify outer _select, the full helper or global effects.


@pytest.mark.parametrize(
    ("name", "before", "after"),
    (
        (
            "before_admitted",
            "before_admitted = require_admitted_turn_evidence(before, env_ref_names=env_ref_names)",
            "before_admitted = require_admitted_turn_evidence(after, env_ref_names=env_ref_names)",
        ),
        (
            "after_admitted",
            "after_admitted = require_admitted_turn_evidence(after, env_ref_names=env_ref_names)",
            "after_admitted = require_admitted_turn_evidence(before, env_ref_names=env_ref_names)",
        ),
        ("before_snapshot", "before_snapshot = before_admitted.snapshot", "before_snapshot = after_admitted.snapshot"),
        ("old", "old = before_admitted.graph", "old = after_admitted.graph"),
        ("new", "new = after_admitted.graph", "new = before_admitted.graph"),
        (
            "expected_admitted",
            "expected_admitted = admit_turn_evidence(expected, env_ref_names=env_ref_names, source_chunks=(recorder_chunk, *before.sources.chunks))",
            "expected_admitted = admit_turn_evidence(expected, env_ref_names=env_ref_names, source_chunks=(recorder_chunk,))",
        ),
    ),
)
def test_receiving_attachment_projection_v2_inner_keeps_admitted_identity_chain(
    receiving_attachment_prerequisite_units, name, before, after
):
    diagnostics = [] if name == "expected_admitted" else ["actual admitted owner prefix changed"]
    diagnostics.append("actual selected identity assignment changed " + name)
    _receiving_attachment_projection_inner_opposition(receiving_attachment_prerequisite_units, before, after, diagnostics)


@pytest.mark.parametrize("name", ("before_admitted", "after_admitted", "before_snapshot", "old", "new", "expected_admitted"))
def test_receiving_attachment_projection_v2_inner_refuses_owner_rebinding(receiving_attachment_prerequisite_units, name):
    _receiving_attachment_projection_inner_opposition(
        receiving_attachment_prerequisite_units,
        "    return new",
        "    " + name + " = foreign\n    return new",
        ["selected identity rebound " + name],
    )


def test_receiving_attachment_projection_v2_inner_keeps_admission_before_projection(receiving_attachment_prerequisite_units):
    _receiving_attachment_projection_inner_opposition(
        receiving_attachment_prerequisite_units,
        "    before_admitted = require_admitted_turn_evidence(before, env_ref_names=env_ref_names)\n"
        "    after_admitted = require_admitted_turn_evidence(after, env_ref_names=env_ref_names)\n"
        "    before_snapshot = before_admitted.snapshot",
        "    before_snapshot = before_admitted.snapshot\n"
        "    before_admitted = require_admitted_turn_evidence(before, env_ref_names=env_ref_names)\n"
        "    after_admitted = require_admitted_turn_evidence(after, env_ref_names=env_ref_names)",
        ["actual admitted owner prefix changed"],
    )


@pytest.mark.parametrize(
    ("before", "after"),
    (
        (
            "encode_turn_evidence(after_admitted, env_ref_names=env_ref_names)",
            "encode_turn_evidence(before_admitted, env_ref_names=env_ref_names)",
        ),
        (
            "encode_turn_evidence(after_admitted, env_ref_names=env_ref_names)",
            "encode_turn_evidence(expected_admitted, env_ref_names=env_ref_names)",
        ),
        (
            "encode_turn_evidence(after_admitted, env_ref_names=env_ref_names)",
            "encode_turn_evidence(after_admitted, env_ref_names=frozenset())",
        ),
        (
            "encode_turn_evidence(after_admitted, env_ref_names=env_ref_names)",
            "encode_application_graph(new, env_ref_names=env_ref_names, recorder_source=recorder_source)",
        ),
        (
            "encode_turn_evidence(\n        expected_admitted, env_ref_names=env_ref_names\n    )",
            "encode_turn_evidence(before_admitted, env_ref_names=env_ref_names)",
        ),
    ),
)
def test_receiving_attachment_projection_v2_inner_keeps_complete_evidence_comparison(
    receiving_attachment_prerequisite_units, before, after
):
    _receiving_attachment_projection_inner_opposition(
        receiving_attachment_prerequisite_units, before, after, ["complete evidence/transcript comparison changed"]
    )


@pytest.mark.parametrize("name", ("require_admitted_turn_evidence", "admit_turn_evidence"))
@pytest.mark.parametrize("fault", ("foreign_import", "rebound"))
def test_receiving_attachment_projection_v2_inner_keeps_canonical_admission_suppliers(receiving_attachment_prerequisite_units, name, fault):
    units = receiving_attachment_prerequisite_units
    failures, positive = _receiving_attachment_projection_inner(units)
    assert failures == [], failures
    assert positive["native_calls"]
    path = fencing._reserve_continuation_transfers.ATTACHMENT_APPLICATION
    original = next(unit for unit in units if unit.path == path)
    source = original.source
    if fault == "foreign_import":
        imported = "    " + name + ",\n"
        assert source.count(imported) == 1
        source = source.replace(imported, "", 1) + "\nfrom foreign import " + name + "\n"
    else:
        source += "\n" + name + " = foreign\n"
    changed = fencing._parse_source(path, source)
    assert ast.dump(changed.tree) != ast.dump(original.tree)
    mutated = tuple(changed if unit is original else unit for unit in units)
    failures, refused = _receiving_attachment_projection_inner(mutated)
    assert failures == ["continuation attachment projection canonical supplier changed " + name]
    assert refused == {}
    assert _receiving_attachment_projection_inner(units) == ([], positive)


def test_receiving_attachment_prerequisite_semantics_have_a_direct_positive(receiving_attachment_prerequisite_units):
    units = receiving_attachment_prerequisite_units
    transfers = fencing._reserve_continuation_transfers
    failures, receipt = transfers.attachment_prerequisite_transfers(units)
    assert failures == [], failures
    assert receipt["checked_definitions"]
    actual = {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}
    assert receipt["checked_definitions"] <= actual
    # This finite semantic verdict deliberately makes no _select/seal or
    # native-dispatch permission claim.
    assert set(receipt) == {"checked_definitions"}


_ATTACHMENT_SQL_PREREQUISITE_CASES = (
    ("_history", "running.claim.session_id", "foreign", "history-session"),
    ("_history", "running.claim.operation_id", "foreign", "history-operation"),
    ("_history_data", "selected.stack.frames[0] is not compose", "selected.stack.frames[0] != compose", "history-identity"),
    ("_history_data", "selected=selected", "selected=foreign", "history-selected"),
    ("_history_data", "names=names", "names=foreign", "history-names"),
    ("_history_data", "conn, session_id=session_id", "foreign, session_id=session_id", "history-connection"),
    ("_history_data", "    ).history", "    )", "history-result"),
    ("_effect", "running.claim.session_id", "foreign", "effect-session-forwarding"),
    ("_effect", "selected, key, digest", "selected, key, foreign", "effect-digest-forwarding"),
    ("_effect_data", "effects.c.session_id == str(session_id)", "effects.c.session_id == str(foreign)", "effect-query-session"),
    ("_effect_data", "effects.c.operation_id == operation_id", "effects.c.operation_id == foreign", "effect-query-operation"),
    ("_effect_data", "effects.c.effect_key == key", "effects.c.effect_key == foreign", "effect-query-key"),
    ("_effect_data", 'or row.owner != "read"', "or False", "effect-owner"),
    ("_effect_data", "or row.input_sha256 != digest", "or False", "effect-input"),
    *(
        ("_effect_data", "or row." + field + " is not None", "or False", "effect-forbidden-" + field)
        for field in (
            "action_json",
            "provider_attempt_id",
            "physical_sequence",
            "physical_generation",
            "requested_model",
            "response_finished_at",
            "committed_state_ids_json",
            "graph_sha256",
        )
    ),
    ("_effect_data", "or len(frontier) != 1", "or False", "effect-frontier-singleton"),
    ("_effect_data", "(row.disposition, row.receipt_key)", "(foreign, row.receipt_key)", "effect-frontier-disposition"),
    ("_effect_data", "(row.disposition, row.receipt_key)", "(row.disposition, foreign)", "effect-frontier-receipt"),
    ("_effect_data", 'item.disposition not in ("committed", "refused")', "False", "effect-unrelated-unresolved"),
    ("_effect_data", "return row", "return foreign", "effect-original-row"),
    (
        "verify_attachment_result_source_on_connection",
        "session_id=running.claim.session_id",
        "session_id=foreign",
        "verify-session-forwarding",
    ),
    ("verify_attachment_result_source_on_connection", "snapshot=snapshot", "snapshot=foreign", "verify-snapshot-forwarding"),
    ("verify_attachment_result_data_on_connection", "snapshot.checkpoint_id != selected.checkpoint_id", "False", "verify-checkpoint"),
    ("verify_attachment_result_data_on_connection", "or snapshot.cursor != selected.stack.cursor", "or False", "verify-cursor"),
    (
        "verify_attachment_result_data_on_connection",
        "or snapshot.payload_json != encode_continuation_snapshot(selected.stack)",
        "or False",
        "verify-payload",
    ),
    (
        "verify_attachment_result_data_on_connection",
        "or selected.stack.logical.session_id != str(session_id)",
        "or False",
        "verify-session",
    ),
    (
        "verify_attachment_result_data_on_connection",
        "or selected.stack.logical.operation_id != operation_id",
        "or False",
        "verify-operation",
    ),
    ("verify_attachment_result_data_on_connection", "_input(selected, names)", "_input(foreign, names)", "verify-original-input"),
    (
        "verify_attachment_result_data_on_connection",
        "_effect_data(conn, session_id, operation_id, selected, key, digest)",
        "_effect_data(conn, session_id, operation_id, selected, key, foreign)",
        "verify-original-effect",
    ),
    ("verify_attachment_result_data_on_connection", 'row.disposition != "committed"', "False", "verify-committed"),
    ("verify_attachment_result_data_on_connection", "or row.result_chunk_key is None", "or False", "verify-result-key"),
    ("verify_attachment_result_data_on_connection", 'frame.phase != "result_retained"', "False", "verify-applied-cut"),
    ("verify_attachment_result_data_on_connection", "or frame.settled_effect_ids.count(key) != 1", "or False", "verify-applied-once"),
    ("verify_attachment_result_data_on_connection", 'frame.phase != "effect_reconciling"', "False", "verify-pending-cut"),
    ("verify_attachment_result_data_on_connection", "or len(pending) != 1", "or False", "verify-pending-singleton"),
    ("verify_attachment_result_data_on_connection", 'or pending[0].kind != "read"', "or False", "verify-pending-kind"),
    ("verify_attachment_result_data_on_connection", "or pending[0].effect_key != key", "or False", "verify-pending-key"),
    (
        "verify_attachment_result_data_on_connection",
        'or matches[0].reference.codec != "composer.owner-result.v1"',
        "or False",
        "verify-codec",
    ),
    (
        "verify_attachment_result_data_on_connection",
        "or private_chunk_key(matches[0]) != row.result_chunk_key",
        "or False",
        "verify-private-key",
    ),
    ("verify_attachment_result_data_on_connection", "stored_codec != chunk.reference.codec", "False", "verify-stored-codec"),
    ("verify_attachment_result_data_on_connection", ') != chunk.canonical.encode("utf-8")', ") != foreign", "verify-stored-bytes"),
    (
        "verify_attachment_result_data_on_connection",
        'row.receipt_key != f"attachment-result:{chunk.reference.digest}"',
        "False",
        "verify-receipt",
    ),
    (
        "verify_attachment_result_data_on_connection",
        "attempts.c.sequence == row.creator_sequence",
        "attempts.c.sequence == foreign",
        "verify-creator-sequence",
    ),
    (
        "verify_attachment_result_data_on_connection",
        "attempts.c.generation == row.creator_generation",
        "attempts.c.generation == foreign",
        "verify-creator-generation",
    ),
    (
        "verify_attachment_result_data_on_connection",
        "creator.session_operation_id, creator.session_operation_epoch",
        "foreign, creator.session_operation_epoch",
        "verify-original-physical-owner",
    ),
    (
        "verify_attachment_result_data_on_connection",
        "require_attachment_read_audit(prepared, read_audit, names=names)",
        "pass",
        "verify-read-audit",
    ),
    (
        "verify_attachment_result_data_on_connection",
        "decode_read_owner_result(publication.result_json, env_ref_names=names)",
        "decode_read_owner_result(foreign, env_ref_names=names)",
        "verify-retained-result",
    ),
    (
        "verify_attachment_result_data_on_connection",
        "_result(retained, prepared, names)",
        "_result(foreign, prepared, names)",
        "verify-result-owner",
    ),
    (
        "verify_attachment_result_data_on_connection",
        "_history_data(conn, session_id, operation_id, selected, compose, names)",
        "_history_data(conn, session_id, operation_id, foreign, compose, names)",
        "verify-history-owner",
    ),
    (
        "verify_attachment_result_data_on_connection",
        "applied and _retained_history(history, key, retained) != history",
        "False",
        "verify-applied-history",
    ),
)


def _receiving_attachment_prerequisite_opposition(units, path, symbol, before, after):
    transfers = fencing._reserve_continuation_transfers
    failures, original_receipt = transfers.attachment_prerequisite_transfers(units)
    assert failures == [], failures
    assert original_receipt["checked_definitions"]
    original = next(unit for unit in units if unit.path == path)
    owner = fencing._reserve_node_at(original, symbol)
    assert isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
    segment = ast.get_source_segment(original.source, owner)
    assert segment is not None and segment.count(before) == 1 and before != after
    assert original.source.count(segment) == 1
    source = original.source.replace(segment, segment.replace(before, after, 1), 1)
    changed = fencing._parse_source(path, source)
    assert ast.dump(changed.tree) != ast.dump(original.tree)
    mutated = tuple(changed if unit is original else unit for unit in units)
    failures, refused = transfers.attachment_prerequisite_transfers(mutated)
    assert failures == ["attachment prerequisite exact transfer changed " + path + ":" + symbol]
    assert refused == {}
    assert transfers.attachment_prerequisite_transfers(units) == ([], original_receipt)


@pytest.mark.parametrize(
    ("symbol", "before", "after", "obligation"),
    _ATTACHMENT_SQL_PREREQUISITE_CASES,
    ids=[case[3] for case in _ATTACHMENT_SQL_PREREQUISITE_CASES],
)
def test_receiving_attachment_moved_sql_obligations(receiving_attachment_prerequisite_units, symbol, before, after, obligation):
    assert obligation
    _receiving_attachment_prerequisite_opposition(
        receiving_attachment_prerequisite_units,
        "src/elspeth/web/sessions/continuation_attachment_result.py",
        symbol,
        before,
        after,
    )


@pytest.mark.parametrize(
    ("symbol", "before", "after"),
    (
        ("validate_native_thinking_blocks", "type(value) not in (list, tuple)", "False"),
        ("validate_native_thinking_blocks", "not isinstance(block, Mapping)", "False"),
        ("validate_native_thinking_blocks", "set(block) != fields", "False"),
        ("validate_native_thinking_blocks", "any(type(block[key]) is not str for key in fields)", "False"),
        ("validate_native_thinking_blocks", "utf8_size(block[key])", "utf8_size('')"),
        (
            "validate_native_thinking_blocks",
            'raise ComposerAdmissionRefused("Provider thinking state contains invalid UTF-8 text.") from None',
            "pass",
        ),
        ("validate_native_thinking_blocks", "if not block[opaque_key]:", "if False:"),
        (
            "validate_native_thinking_blocks",
            'raise ComposerAdmissionRefused("Provider thinking state contains an unsupported native block.")',
            "continue",
        ),
        ("scan_native_thinking_blocks", "validate_native_thinking_blocks(value)", "validate_native_thinking_blocks(foreign)"),
        ("scan_native_thinking_blocks", "current = {key: block[key] for key in block}", "current = {}"),
        ("scan_native_thinking_blocks", 'surface="composer_provider_thinking"', 'surface="foreign"'),
        ("scan_native_thinking_blocks", "max(utf8_size(text) for text in current.values())", "1"),
        ("scan_native_thinking_blocks", "utf8_size(text)", "len(text)"),
        ("parse_native_thinking_blocks", "validate_native_thinking_blocks(value)", "pass"),
        (
            "native_thinking_blocks",
            "scan_native_thinking_blocks(value)\n    return parse_native_thinking_blocks(value)",
            "parse_native_thinking_blocks(value)\n    scan_native_thinking_blocks(value)\n    return []",
        ),
    ),
)
def test_receiving_attachment_thinking_preserves_complete_admission(receiving_attachment_prerequisite_units, symbol, before, after):
    _receiving_attachment_prerequisite_opposition(
        receiving_attachment_prerequisite_units, "src/elspeth/web/composer/provider_thinking.py", symbol, before, after
    )


@pytest.mark.parametrize(
    ("path", "symbol", "before", "after"),
    (
        (
            "continuation_attachment_application.py",
            "require_attachment_projection",
            "require_admitted_turn_evidence(before, env_ref_names=env_ref_names)",
            "require_admitted_turn_evidence(after, env_ref_names=env_ref_names)",
        ),
        (
            "continuation_attachment_application.py",
            "require_attachment_projection",
            "new = after_admitted.graph",
            "new = before_admitted.graph",
        ),
        (
            "continuation_attachment_application.py",
            "require_attachment_projection",
            "source_chunks=(recorder_chunk, *before.sources.chunks)",
            "source_chunks=(recorder_chunk,)",
        ),
        (
            "continuation_attachment_application.py",
            "require_attachment_projection",
            '"recorder": recorder_chunk.reference',
            '"recorder": foreign',
        ),
        (
            "continuation_attachment_application.py",
            "require_attachment_projection",
            "encode_turn_evidence(after_admitted, env_ref_names=env_ref_names)",
            "encode_turn_evidence(expected_admitted, env_ref_names=env_ref_names)",
        ),
        (
            "continuation_attachment_application.py",
            "admit_attachment_application",
            "canonical=value.before_evidence_json",
            "canonical=value.after_evidence_json",
        ),
        ("continuation_attachment_application.py", "admit_attachment_application", "before.snapshot.logical != root.logical", "False"),
        (
            "continuation_attachment_application_driver.py",
            "apply_current_attachment_runtime",
            "history = current_source.attachments",
            "history = runtime.attachments",
        ),
        (
            "continuation_attachment_application_driver.py",
            "apply_current_attachment_runtime",
            "current_source.selected is not selected",
            "current_source.selected != selected",
        ),
        (
            "continuation_attachment_application_driver.py",
            "apply_current_attachment_runtime",
            "work,\n        ContinuationRuntimeSource,",
            "foreign,\n        ContinuationRuntimeSource,",
        ),
        (
            "continuation_attachment_application_driver.py",
            "apply_current_attachment_runtime",
            "authority.read_continuation_runtime(running, selected=selected, inventory=inventory)",
            "authority.read_continuation_runtime(running, selected=foreign, inventory=inventory)",
        ),
        (
            "continuation_attachment_finisher.py",
            "finish_current_attachment_result",
            "require_admitted_turn_evidence(before, env_ref_names=inventory.names).graph",
            "require_admitted_turn_evidence(before, env_ref_names=frozenset()).graph",
        ),
    ),
)
def test_receiving_attachment_v2_and_current_history_keep_actual_owners(
    receiving_attachment_prerequisite_units, path, symbol, before, after
):
    _receiving_attachment_prerequisite_opposition(
        receiving_attachment_prerequisite_units, "src/elspeth/web/composer/" + path, symbol, before, after
    )


@pytest.mark.parametrize("side", ("before", "after"))
def test_receiving_attachment_v2_decoders_keep_each_original_source_chunk_set(receiving_attachment_prerequisite_units, side):
    before = (
        "canonical=value." + side + "_evidence_json, env_ref_names=env_ref_names),\n"
        "        env_ref_names=env_ref_names,\n        source_chunks=source_chunks,"
    )
    _receiving_attachment_prerequisite_opposition(
        receiving_attachment_prerequisite_units,
        "src/elspeth/web/composer/continuation_attachment_application.py",
        "admit_attachment_application",
        before,
        before.replace("source_chunks=source_chunks", "source_chunks=()"),
    )


@pytest.mark.parametrize(
    ("name", "module"),
    (("utf8_size", "elspeth.contracts.bounded_serialization"), ("require_no_credential_material", "elspeth.web.credential_guard")),
)
@pytest.mark.parametrize("fault", ("foreign_import", "rebound", "missing_source", "shadowed_definition"))
def test_receiving_attachment_thinking_supplier_binding_is_not_permission(receiving_attachment_prerequisite_units, name, module, fault):
    units = receiving_attachment_prerequisite_units
    transfers = fencing._reserve_continuation_transfers
    failures, receipt = transfers.attachment_prerequisite_transfers(units)
    assert failures == [], failures
    path = "src/elspeth/web/composer/provider_thinking.py"
    supplier_path = "src/" + module.replace(".", "/") + ".py"
    if fault == "missing_source":
        mutated = tuple(unit for unit in units if unit.path != supplier_path)
        assert len(mutated) == len(units) - 1
        diagnostic = "attachment prerequisite supplier source missing " + supplier_path
    else:
        target_path = supplier_path if fault == "shadowed_definition" else path
        original = next(unit for unit in units if unit.path == target_path)
        if fault == "foreign_import":
            old = "from " + module + " import " + name
            assert original.source.count(old) == 1
            source = original.source.replace(old, "from foreign.supplier import " + name, 1)
        else:
            source = original.source + "\n" + name + " = foreign\n"
        changed = fencing._parse_source(target_path, source)
        mutated = tuple(changed if unit is original else unit for unit in units)
        diagnostic = (
            "attachment prerequisite supplier definition changed " + supplier_path + ":" + name
            if fault == "shadowed_definition"
            else "attachment prerequisite supplier binding changed " + path + ":" + name
        )
    failures, refused = transfers.attachment_prerequisite_transfers(mutated)
    assert failures == [diagnostic]
    assert refused == {}
    assert transfers.attachment_prerequisite_transfers(units) == ([], receipt)


@pytest.mark.parametrize(
    ("before", "after"),
    (
        (
            "from elspeth.web.sessions.continuation_p4_application_store import _private_chunk_for_operation_on_connection",
            "from foreign.store import _private_chunk_for_operation_on_connection",
        ),
        ("_text_data(conn, session_id, operation_id, private_chunk_key(actual)) != actual.canonical", "False"),
        (
            "_private_chunk_for_operation_on_connection(conn, session_id, operation_id, ref)",
            "_private_chunk_for_operation_on_connection(conn, session_id, foreign, ref)",
        ),
        ("for ref in attachment_history_dependencies(fields)", "for ref in set(attachment_history_dependencies(fields))"),
        ("env_ref_names=names, source_chunks=chunks", "env_ref_names=names, source_chunks=()"),
        ("return AttachmentHistorySources(history, chunks)", "return AttachmentHistorySources(history, ())"),
    ),
)
def test_receiving_attachment_history_sql_supplier_keeps_bytes_and_occurrences(receiving_attachment_prerequisite_units, before, after):
    _receiving_attachment_prerequisite_opposition(
        receiving_attachment_prerequisite_units,
        "src/elspeth/web/sessions/continuation_attachment_application.py",
        "load_attachment_history_data_on_connection",
        before,
        after,
    )


@pytest.mark.parametrize("shadow", ("ContinuationLivePhases = foreign", "ContinuationLivePhases: object = foreign"))
def test_receiving_p4_custody_lookup_refuses_shadowed_actual_phase_owner(receiving_attachment_prerequisite_units, shadow):
    units = receiving_attachment_prerequisite_units
    failures, receipt = fencing._reserve_p4_batch_custody_receipt(units)
    assert failures == [], failures
    path = "src/elspeth/web/composer/continuation_live_phases.py"
    original = next(unit for unit in units if unit.path == path)
    changed = fencing._parse_source(path, original.source + "\n" + shadow + "\n")
    mutated = tuple(changed if unit is original else unit for unit in units)
    assert fencing._reserve_p4_batch_custody_receipt(mutated) == (["P4 batch custody native definition changed"], {})
    assert fencing._reserve_p4_batch_custody_receipt(units) == ([], receipt)


@pytest.mark.parametrize(
    "shadow",
    (
        "ContinuationLivePhases, other = foreign",
        "for ContinuationLivePhases in foreign:\n    pass",
        "with foreign as ContinuationLivePhases:\n    pass",
        "if foreign:\n    ContinuationLivePhases = foreign",
        "import foreign as ContinuationLivePhases",
        "(ContinuationLivePhases := foreign)",
    ),
    ids=("tuple", "for", "with", "conditional", "import", "named-expression"),
)
def test_receiving_p4_custody_lookup_refuses_indirect_phase_owner_rebinding(receiving_attachment_prerequisite_units, shadow):
    # Root received the original-positive/six-negative RED before the lexical
    # owner lookup repair. Keep the same concrete refusals on the repair.
    units = receiving_attachment_prerequisite_units
    failures, receipt = fencing._reserve_p4_batch_custody_receipt(units)
    assert failures == [], failures
    path = "src/elspeth/web/composer/continuation_live_phases.py"
    original = next(unit for unit in units if unit.path == path)
    changed = fencing._parse_source(path, original.source + "\n" + shadow + "\n")
    assert ast.dump(changed.tree) != ast.dump(original.tree)
    mutated = tuple(changed if unit is original else unit for unit in units)
    assert fencing._reserve_p4_batch_custody_receipt(mutated) == (["P4 batch custody native definition changed"], {})
    assert fencing._reserve_p4_batch_custody_receipt(units) == ([], receipt)


@pytest.mark.parametrize(
    "effect",
    (
        "ContinuationLivePhases._execute_one_owned = foreign",
        "del ContinuationLivePhases._execute_one_owned",
        "ContinuationLivePhases._execute_one_owned, other = foreign",
        "if foreign:\n    ContinuationLivePhases._persist_batch = foreign",
        "for ContinuationLivePhases._execute_one_owned in foreign:\n    pass",
        "with foreign as ContinuationLivePhases._execute_one_owned:\n    pass",
    ),
    ids=("assign", "delete", "tuple", "conditional", "for", "with"),
)
def test_receiving_p4_custody_lookup_refuses_qualified_member_mutation(receiving_attachment_prerequisite_units, effect):
    # Root measured all six original-positive/qualified-effect negatives RED
    # before this exact enclosing-execution attribute-effect check was added.
    units = receiving_attachment_prerequisite_units
    failures, receipt = fencing._reserve_p4_batch_custody_receipt(units)
    assert failures == [], failures
    path = "src/elspeth/web/composer/continuation_live_phases.py"
    original = next(unit for unit in units if unit.path == path)
    changed = fencing._parse_source(path, original.source + "\n" + effect + "\n")
    assert ast.dump(changed.tree) != ast.dump(original.tree)
    mutated = tuple(changed if unit is original else unit for unit in units)
    assert fencing._reserve_p4_batch_custody_receipt(mutated) == (["P4 batch custody native definition changed"], {})
    assert fencing._reserve_p4_batch_custody_receipt(units) == ([], receipt)


@pytest.mark.parametrize(
    "source",
    (
        "ContinuationLivePhases.unrelated_review_probe = foreign",
        "def deferred_review_probe():\n    ContinuationLivePhases._execute_one_owned = foreign",
    ),
    ids=("unselected-member", "deferred-effect"),
)
def test_receiving_p4_custody_local_lookup_does_not_claim_global_effect_closure(receiving_attachment_prerequisite_units, source):
    units = receiving_attachment_prerequisite_units
    failures, original_receipt = fencing._reserve_p4_batch_custody_receipt(units)
    assert failures == [], failures
    path = "src/elspeth/web/composer/continuation_live_phases.py"
    original = next(unit for unit in units if unit.path == path)
    changed = fencing._parse_source(path, original.source + "\n" + source + "\n")
    mutated = tuple(changed if unit is original else unit for unit in units)
    failures, receipt = fencing._reserve_p4_batch_custody_receipt(mutated)
    assert failures == [], failures
    assert receipt["native_calls"]
    (added,) = changed.tree.body[len(original.tree.body) :]
    added_sites = {(path, id(node)) for node in ast.walk(added)}
    assert not added_sites.intersection(receipt["receiver_uses"] | receipt["native_calls"] | receipt["native_uses"])
    assert receipt["execute_reference"] not in added_sites
    # The local proof issues no permission for either new effect. Whether the
    # assembled origin/effect proof refuses it is a separate receiving claim.
    assert fencing._reserve_p4_batch_custody_receipt(units) == ([], original_receipt)


@pytest.mark.parametrize("shadow", ("_execute_one_owned, other = foreign", "if foreign:\n    _execute_one_owned = foreign"))
def test_receiving_p4_custody_lookup_checks_each_actual_class_namespace(receiving_attachment_prerequisite_units, shadow):
    units = receiving_attachment_prerequisite_units
    failures, receipt = fencing._reserve_p4_batch_custody_receipt(units)
    assert failures == [], failures
    path = "src/elspeth/web/composer/continuation_live_phases.py"

    def change(unit):
        owner = fencing._reserve_node_at(unit, "ContinuationLivePhases")
        assert isinstance(owner, ast.ClassDef)
        owner.body.extend(ast.parse(shadow).body)

    mutated = _receiving_changed_units(units, path, change)
    assert fencing._reserve_p4_batch_custody_receipt(mutated) == (["P4 batch custody native definition changed"], {})
    assert fencing._reserve_p4_batch_custody_receipt(units) == ([], receipt)


@pytest.mark.parametrize("fault", ("call_model", "ctx", "acc", "llm_messages", "batch", "missing", "extra", "duplicate", "foreign"))
def test_receiving_p4_initializer_keeps_original_owners_after_phase_source_rebind(
    receiving_attachment_prerequisite_units, monkeypatch, fault
):
    continuation = fencing._reserve_continuation_transfers
    units = receiving_attachment_prerequisite_units
    failures, receipt = continuation.p4_batch_custody_receipt(units)
    assert failures == [], failures
    path = "src/elspeth/web/composer/continuation_live_phases.py"
    original = next(unit for unit in units if unit.path == path)
    primary = fencing._reserve_node_at(original, "ContinuationLivePhases._primary_for_phase")
    (initializer,) = [
        node for node in ast.walk(primary) if isinstance(node, ast.Call) and ast.unparse(node.func) == "_initialize_continuation_batch_live"
    ]
    assert (path, id(initializer)) in receipt["native_calls"]
    assert (path, id(initializer.func)) in receipt["native_uses"]

    def change(unit):
        owner = fencing._reserve_node_at(unit, "ContinuationLivePhases._primary_for_phase")
        (statement,) = [
            node
            for node in owner.body
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Await)
            and isinstance(node.value.value, ast.Call)
            and ast.unparse(node.value.value.func) == "_initialize_continuation_batch_live"
        ]
        call = statement.value.value
        if fault == "missing":
            call.keywords.pop()
        elif fault == "extra":
            call.keywords.append(ast.keyword(arg="foreign", value=ast.Constant(None)))
        elif fault == "duplicate":
            owner.body.insert(owner.body.index(statement), copy.deepcopy(statement))
        elif fault == "foreign":
            call.func = ast.Name(id="foreign_initializer", ctx=ast.Load())
        else:
            keyword = next(keyword for keyword in call.keywords if keyword.arg == fault)
            keyword.value = ast.Name(id="foreign_owner", ctx=ast.Load())

    mutated = _receiving_changed_units(units, path, change)
    changed = next(unit for unit in mutated if unit.path == path)
    cls = fencing._reserve_node_at(changed, "ContinuationLivePhases")
    with monkeypatch.context() as patch:
        patch.setitem(continuation.OWNER_TEMPLATES, "phases.ContinuationLivePhases", ast.unparse(cls))
        assert continuation.p4_batch_custody_receipt(mutated) == (["P4 batch custody actual initializer owner arguments changed"], {})
    assert continuation.p4_batch_custody_receipt(units) == ([], receipt)


@pytest.mark.parametrize(
    "fault", ("projection", "caller", "branch", "sql_source", "ordinal", "slots", "physical", "context", "guard_order")
)
def test_receiving_terminal_reserve_tickets_keep_original_owner_after_body_rebind(monkeypatch, fault):
    continuation = fencing._reserve_continuation_transfers
    units = _source_units(tuple(dict.fromkeys(path for path, _symbol in continuation.TERMINAL_RESERVE_BODY_BINDINGS)))
    failures, receipt = continuation.terminal_failure_reserve_transfers(units)
    assert failures == [], failures
    assert len(receipt["reserve_calls"]) == 2
    driver_fault = fault in {"projection", "caller", "branch"}
    path, symbol = (
        ("src/elspeth/web/sessions/continuation_driver.py", "_SegmentDriver")
        if driver_fault
        else ("src/elspeth/web/sessions/continuation_tool_failure.py", "fail_continuation_tool_operation")
    )

    def change(unit):
        if driver_fault:
            owner = fencing._reserve_node_at(unit, "_SegmentDriver.settle")
            if fault == "branch":
                (branch,) = [node for node in owner.body if isinstance(node, ast.If) and ast.unparse(node.test) == "owns_failure"]
                branch.test = ast.Constant(True)
            else:
                (call,) = [
                    node
                    for node in ast.walk(owner)
                    if isinstance(node, ast.Call)
                    and ast.unparse(node.func)
                    == (
                        "self.work.coordinator.reserve"
                        if fault == "projection"
                        else "self.services.session_service.fail_continuation_tool_operation"
                    )
                ]
                if fault == "projection":
                    call.args[0] = ast.Attribute(
                        value=ast.Name(id="RequiredWorkSource", ctx=ast.Load()), attr="TERMINAL_FAILURE_SQL", ctx=ast.Load()
                    )
                else:
                    next(keyword for keyword in call.keywords if keyword.arg == "failure_projection_work").value = ast.Name(
                        id="foreign_ticket", ctx=ast.Load()
                    )
        else:
            owner = fencing._reserve_node_at(unit, symbol)
            (loop,) = [node for node in owner.body if isinstance(node, ast.For) and ast.unparse(node.target) == "slot"]
            if fault == "slots":
                loop.iter.args[0] = ast.Constant(3)
            elif fault == "physical":
                loop.body[2].body[0].value.value.args[0] = ast.Name(id="foreign_ticket", ctx=ast.Load())
            elif fault in {"context", "guard_order"}:
                (guard,) = [
                    node
                    for node in owner.body
                    if isinstance(node, ast.If) and "authority.context is not running.session_operation_context" in ast.unparse(node.test)
                ]
                if fault == "context":
                    guard.test = ast.Constant(False)
                else:
                    owner.body.remove(guard)
                    owner.body.append(guard)
            else:
                call = loop.body[0].body[1].value
                if fault == "sql_source":
                    call.args[0].attr = "TERMINAL_FAILURE_PROJECTION"
                else:
                    call.keywords[0].value = ast.Constant(0)

    mutated = _receiving_changed_units(units, path, change)
    changed = next(unit for unit in mutated if unit.path == path)
    declaration = fencing._reserve_node_at(changed, symbol)
    with monkeypatch.context() as patch:
        patch.setitem(continuation.TERMINAL_RESERVE_BODY_BINDINGS, (path, symbol), continuation._digest(declaration))
        failures, rejected = continuation.terminal_failure_reserve_transfers(mutated)
        assert rejected == {} and any(
            failure.startswith("terminal reserve original") or "physical slots" in failure for failure in failures
        ), failures
        assert not any("complete source changed" in failure for failure in failures)
    assert continuation.terminal_failure_reserve_transfers(units) == ([], receipt)


@pytest.mark.parametrize("fault", ("deadline", "authority", "coordinator", "adoption_context", "adoption_work", "retained", "lease_source"))
def test_receiving_started_owner_keeps_exact_custody_after_body_rebind(monkeypatch, fault):
    paths = (worker_six.WORKER, "src/elspeth/web/coordination/lifecycle.py")
    units = _source_units(paths)
    failures, receipt = worker_six.continuation_start_owner_contract(units)
    assert failures == [] and receipt is not None, failures
    sites = {(unit.path, id(node)): node for unit in units for node in ast.walk(unit.tree)}
    assert receipt["supplier_arguments"]
    for path, call_id, argument_id in receipt["supplier_arguments"]:
        call = sites[path, call_id]
        assert isinstance(call, ast.Call)
        assert any(id(argument) == argument_id for argument in (*call.args, *(keyword.value for keyword in call.keywords)))
    path = paths[1] if fault == "lease_source" else paths[0]

    def change(unit):
        if fault == "lease_source":
            owner = fencing._reserve_node_at(unit, "ContinuationLeaseAdoption.join")
            (call,) = [
                node for node in ast.walk(owner) if isinstance(node, ast.Call) and ast.unparse(node.func) == "self.required_work.reserve"
            ]
            call.args[0].attr = "LEASE_RELEASE"
            return
        owner = fencing._reserve_node_at(unit, "ComposerAsyncWorker._job_v2")
        if fault == "retained":
            (stored,) = [
                node
                for node in ast.walk(owner)
                if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "self._continuation_adoptions[key]"
            ]
            stored.value = ast.Name(id="foreign_adoption", ctx=ast.Load())
            return
        constructor = {
            "deadline": "ContinuationSQLDeadlineOwner",
            "authority": "RequiredWorkAuthority",
            "coordinator": "RequiredWorkCoordinator",
            "adoption_context": "ContinuationLeaseAdoption",
            "adoption_work": "ContinuationLeaseAdoption",
        }[fault]
        (call,) = [node for node in ast.walk(owner) if isinstance(node, ast.Call) and ast.unparse(node.func) == constructor]
        if fault in {"authority", "adoption_context"}:
            call.args[1] = ast.Name(id="foreign_context", ctx=ast.Load())
        else:
            keyword = {"deadline": "drain_signal", "coordinator": "sql_deadline_owner", "adoption_work": "required_work"}[fault]
            next(item for item in call.keywords if item.arg == keyword).value = ast.Name(id="foreign_owner", ctx=ast.Load())

    mutated = _receiving_changed_units(units, path, change)
    changed = next(unit for unit in mutated if unit.path == path)
    symbol = "ContinuationLeaseAdoption" if fault == "lease_source" else "ComposerAsyncWorker._job_v2"
    declaration = fencing._reserve_node_at(changed, symbol)
    with monkeypatch.context() as patch:
        patch.setitem(worker_six.CONTINUATION_OWNER_GRAMMARS, symbol, worker_six.grammar(declaration))
        failures, rejected = worker_six.continuation_start_owner_contract(mutated)
        assert failures and rejected is None
        assert not any("complete source changed" in failure for failure in failures), failures
    assert worker_six.continuation_start_owner_contract(units) == ([], receipt)


@pytest.mark.parametrize("fault", ("service", "work", "phases_provider", "phases_lease", "store"))
def test_receiving_driver_provider_store_keeps_exact_original_owners_after_body_rebind(monkeypatch, fault):
    path = "src/elspeth/web/sessions/continuation_driver.py"
    units = _source_units((path,))
    failures, receipt = provider.provider_driver_constructor_contract(units)
    assert failures == [] and receipt is not None, failures
    sites = {(unit.path, id(node)): node for unit in units for node in ast.walk(unit.tree)}
    assert len(receipt["owner_constructor_stores"]) == len(receipt["owner_constructor_calls"]) == 1
    for site in receipt["owner_constructor_stores"]:
        assert isinstance(sites[site], ast.Assign) and ast.unparse(sites[site].targets[0]) == "self.provider"

    def change(unit):
        owner = fencing._reserve_node_at(unit, "_SegmentDriver.__init__")
        if fault == "store":
            (stored,) = [node for node in owner.body if isinstance(node, ast.Assign) and ast.unparse(node.targets[0]) == "self.provider"]
            stored.targets[0].attr = "foreign_provider"
        else:
            name = "ContinuationLivePhases" if fault.startswith("phases_") else "ProviderInvocationOwner"
            (call,) = [node for node in ast.walk(owner) if isinstance(node, ast.Call) and ast.unparse(node.func) == name]
            if fault.startswith("phases_"):
                call.args[3 if fault == "phases_provider" else 4] = ast.Name(id="foreign_owner", ctx=ast.Load())
            else:
                next(
                    keyword for keyword in call.keywords if keyword.arg == ("service" if fault == "service" else "required_work")
                ).value = ast.Name(id="foreign_owner", ctx=ast.Load())

    mutated = _receiving_changed_units(units, path, change)
    constructor = fencing._reserve_node_at(mutated[0], "_SegmentDriver.__init__")
    with monkeypatch.context() as patch:
        patch.setitem(provider.CONTRACTS, provider.DRIVER + ":_SegmentDriver.__init__", ast.unparse(constructor))
        failures, rejected = provider.provider_driver_constructor_contract(mutated)
        assert failures and rejected is None
        assert "provider driver complete constructor changed" not in failures
    assert provider.provider_driver_constructor_contract(units) == ([], receipt)


def test_receiving_p4_custody_binds_issue_and_completion_as_distinct_original_branches(receiving_attachment_prerequisite_units):
    units = receiving_attachment_prerequisite_units
    failures, receipt = fencing._reserve_p4_batch_custody_receipt(units)
    assert failures == [], failures
    path = "src/elspeth/web/composer/continuation_live_phases.py"
    original = next(unit for unit in units if unit.path == path)
    owner = fencing._reserve_node_at(original, "ContinuationLivePhases._execute_one_owned")
    prepared = [node for node in owner.body if isinstance(node, ast.If) and ast.unparse(node.test) == "preparation == 'prepared'"]
    assert len(prepared) == 2
    issued, completed = prepared
    assert owner.body[-2] is completed
    (execute,) = [
        node for node in ast.walk(issued) if isinstance(node, ast.Call) and ast.unparse(node.func) == "_execute_continuation_call_live"
    ]
    assert receipt["execute_reference"] == (path, id(execute.func))
    assert (path, id(execute)) in receipt["native_calls"]
    assert not any(
        isinstance(node, ast.Call) and ast.unparse(node.func) == "_execute_continuation_call_live" for node in ast.walk(completed)
    )


@pytest.mark.parametrize(
    ("before", "after"),
    (
        ('disposition="committed", receipt_id=receipt', 'disposition="dispatched", receipt_id=receipt'),
        ('"settled_effect_ids": (*top.settled_effect_ids, issued_effect_key)', '"settled_effect_ids": top.settled_effect_ids'),
        ('"tool_completed" if preparation == "prepared" else "tool_preparation_completed"', '"tool_preparation_completed"'),
    ),
)
def test_receiving_p4_custody_preserves_the_actual_completion_branch(before, after):
    _receiving_p4_custody_opposition(
        "src/elspeth/web/composer/continuation_live_phases.py",
        "ContinuationLivePhases._execute_one_owned",
        before,
        after,
        "actual issued/completed phase layout changed",
    )


@pytest.mark.parametrize("fault", ("missing", "duplicate", "shadow"))
def test_receiving_attachment_prerequisite_owner_cannot_be_substituted(receiving_attachment_prerequisite_units, fault):
    units = receiving_attachment_prerequisite_units
    transfers = fencing._reserve_continuation_transfers
    failures, receipt = transfers.attachment_prerequisite_transfers(units)
    assert failures == [], failures
    path = transfers.CHECKPOINT
    original = next(unit for unit in units if unit.path == path)
    if fault == "missing":
        mutated = tuple(unit for unit in units if unit is not original)
        diagnostic = "attachment prerequisite source set missing or duplicated"
    elif fault == "duplicate":
        mutated = (*units, fencing._parse_source(path, original.source))
        diagnostic = "attachment prerequisite source set missing or duplicated"
    else:
        assert fault == "shadow"
        changed = fencing._parse_source(path, original.source + "\n_effect_data: object = foreign\n")
        mutated = tuple(changed if unit is original else unit for unit in units)
        diagnostic = "attachment prerequisite exact transfer changed " + path + ":_effect_data"
    assert transfers.attachment_prerequisite_transfers(mutated) == ([diagnostic], {})
    assert transfers.attachment_prerequisite_transfers(units) == ([], receipt)


def _receiving_attachment_positive(units):
    transfers = fencing._reserve_continuation_transfers
    failures, receipt = transfers.attachment_local_transfers(units)
    # This is deliberately the direct helper, without global recipe/seal gates
    # or replacement selection facts. Its actual prerequisites must pass first.
    assert failures == [], failures
    assert receipt
    return receipt


def _receiving_attachment_execute_reference(units, path, symbol):
    unit = next(unit for unit in units if unit.path == path)
    owner = fencing._reserve_node_at(unit, symbol)
    assert isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
    calls = [node for node in ast.walk(owner) if isinstance(node, ast.Call) and ast.unparse(node.func) == "_execute_continuation_call_live"]
    assert len(calls) == 1
    return path, id(calls[0].func)


def test_receiving_attachment_inventory_joins_only_the_actual_p4_execute_reference():
    units = fencing._production_units()
    receipt = _receiving_attachment_positive(units)
    transfers = fencing._reserve_continuation_transfers
    phase_path = "src/elspeth/web/composer/continuation_live_phases.py"
    ordinary = _receiving_attachment_execute_reference(units, transfers.BATCH, "run_tool_batch")
    retained = _receiving_attachment_execute_reference(units, transfers.FINISHER, "finish_current_attachment_result")
    issued = _receiving_attachment_execute_reference(units, phase_path, "ContinuationLivePhases._execute_one_owned")
    assert len({ordinary, retained, issued}) == 3
    assert transfers._references(units, {"_execute_continuation_call_live"}) == {ordinary, retained, issued}
    assert {retained, issued} <= receipt["native_uses"]
    original = next(unit for unit in units if unit.path == phase_path)
    changed = fencing._parse_source(
        phase_path, original.source + "\nasync def foreign_attachment_dispatch():\n    await _execute_continuation_call_live()\n"
    )
    mutated = tuple(changed if unit is original else unit for unit in units)
    failures, refused = transfers.attachment_local_transfers(mutated)
    assert failures == ["continuation attachment native reference inventory changed"]
    assert refused == {}
    assert _receiving_attachment_positive(units) == receipt


@pytest.mark.parametrize(
    ("fault", "diagnostic"),
    (
        ("foreign_batch", "actual issued custody constructor/forwarding changed"),
        ("foreign_call", "actual issued custody constructor/forwarding changed"),
        ("foreign_context", "actual issued custody constructor/forwarding changed"),
        ("foreign_custody", "actual issued custody constructor/forwarding changed"),
        ("rebound_batch", "actual issued source rebound batch"),
        ("missing_retention", "actual issued custody constructor/forwarding changed"),
        ("late_retention", "actual issued custody constructor/forwarding changed"),
    ),
)
def test_receiving_attachment_p4_caller_keeps_original_issue_and_custody(fault, diagnostic):
    units = fencing._production_units()
    receipt = _receiving_attachment_positive(units)
    transfers = fencing._reserve_continuation_transfers
    path = "src/elspeth/web/composer/continuation_live_phases.py"

    def change(unit):
        owner = fencing._reserve_node_at(unit, "ContinuationLivePhases._execute_one_owned")
        candidates = [node for node in owner.body if isinstance(node, ast.If) and ast.unparse(node.test) == "preparation == 'prepared'"]
        assert len(candidates) == 2 and owner.body[-2] is candidates[1]
        (prepared,) = [
            branch
            for branch in candidates
            if any(isinstance(node, ast.Call) and ast.unparse(node.func) == "_execute_continuation_call_live" for node in ast.walk(branch))
        ]
        assert prepared is candidates[0]
        retention, custody, dispatch = prepared.body[-3:]
        assert isinstance(retention, ast.Expr) and isinstance(retention.value, ast.Await)
        assert ast.unparse(retention.value.value.func) == "self._retain"
        assert isinstance(custody, ast.Assign) and ast.unparse(custody.value.func) == "ContinuationToolFailureCustody"
        assert isinstance(dispatch, ast.Expr) and isinstance(dispatch.value, ast.Await)
        assert ast.unparse(dispatch.value.value.func) == "_execute_continuation_call_live"
        if fault in {"foreign_batch", "foreign_call"}:
            position = 3 if fault == "foreign_batch" else 6
            assert ast.unparse(custody.value.args[position]) == ("batch" if position == 3 else "call")
            custody.value.args[position] = ast.Name(id="foreign", ctx=ast.Load())
        elif fault in {"foreign_context", "foreign_custody"}:
            argument = "ctx" if fault == "foreign_context" else "failure_custody"
            (keyword,) = [item for item in dispatch.value.value.keywords if item.arg == argument]
            assert ast.unparse(keyword.value) == ("context" if argument == "ctx" else "self.failure_custody")
            keyword.value = ast.Name(id="foreign", ctx=ast.Load())
        elif fault == "rebound_batch":
            prepared.body.insert(len(prepared.body) - 3, ast.parse("batch = foreign").body[0])
        elif fault == "missing_retention":
            prepared.body.remove(retention)
        else:
            assert fault == "late_retention"
            prepared.body[-3:] = [custody, retention, dispatch]

    changed = _receiving_changed_units(units, path, change)
    failures, refused = transfers.attachment_local_transfers(changed)
    assert failures == ["P4 batch custody " + diagnostic]
    assert refused == {}
    assert _receiving_attachment_positive(units) == receipt


def test_receiving_attachment_receipt_rejects_a_detached_p4_reference(monkeypatch):
    units = fencing._production_units()
    original_receipt = _receiving_attachment_positive(units)
    detached = tuple(fencing._parse_source(unit.path, unit.source) for unit in units)
    detached_receipt = _receiving_attachment_positive(detached)
    transfers = fencing._reserve_continuation_transfers
    path = "src/elspeth/web/composer/continuation_live_phases.py"
    symbol = "ContinuationLivePhases._execute_one_owned"
    actual_reference = _receiving_attachment_execute_reference(units, path, symbol)
    detached_reference = _receiving_attachment_execute_reference(detached, path, symbol)
    assert actual_reference != detached_reference
    original_references = transfers._references
    calls = []

    def detached_inventory(supplied, names):
        references = original_references(supplied, names)
        if names == {"_execute_continuation_call_live"}:
            assert supplied is units
            assert actual_reference in references and detached_reference not in references
            calls.append(actual_reference)
            return (references - {actual_reference}) | {detached_reference}
        return references

    # Use the existing inventory seam, not a future producer attribute or an
    # injected permission argument. Only the P4 node identity is substituted.
    with monkeypatch.context() as patch:
        patch.setattr(transfers, "_references", detached_inventory)
        failures, refused = transfers.attachment_local_transfers(units)
    assert calls == [actual_reference]
    assert failures == ["continuation attachment native reference inventory changed"]
    assert refused == {}
    assert _receiving_attachment_positive(units) == original_receipt
    assert _receiving_attachment_positive(detached) == detached_receipt


@pytest.mark.parametrize(
    "fault", ("retained_default", "custody_default", "attachment_context", "attachment_registration", "ordinary_sibling")
)
def test_receiving_attachment_default_and_registration_remain_exact(fault):
    units = fencing._production_units()
    receipt = _receiving_attachment_positive(units)
    transfers = fencing._reserve_continuation_transfers
    original = next(unit for unit in units if unit.path == transfers.BATCH)
    execute = fencing._reserve_node_at(original, "_execute_continuation_call_live")
    defaults = dict(zip((arg.arg for arg in execute.args.kwonlyargs), execute.args.kw_defaults, strict=True))
    assert isinstance(defaults["retained_attachment"], ast.Constant) and defaults["retained_attachment"].value is None
    assert isinstance(defaults["failure_custody"], ast.Constant) and defaults["failure_custody"].value is None
    assert receipt["retained_defaults"] == {(transfers.BATCH, id(defaults["retained_attachment"]))}
    assert (transfers.BATCH, id(defaults["failure_custody"])) not in receipt["retained_defaults"]

    def change(unit):
        owner = fencing._reserve_node_at(unit, "_execute_continuation_call_live")
        if fault in {"retained_default", "custody_default"}:
            name = "retained_attachment" if fault == "retained_default" else "failure_custody"
            position = [arg.arg for arg in owner.args.kwonlyargs].index(name)
            owner.args.kw_defaults[position] = ast.Name(id="foreign", ctx=ast.Load())
            return
        (registration,) = [
            node
            for node in ast.walk(owner)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "retained_attachment is not None"
            and any(
                isinstance(child, ast.Call) and ast.unparse(child.func) == "ctx.message_producers.register_attachment_message"
                for child in ast.walk(node)
            )
        ]
        if fault == "attachment_context":
            assert isinstance(registration.body[0], ast.If)
            registration.body[0].test = ast.Constant(value=False)
        elif fault == "attachment_registration":
            (call,) = [
                node
                for node in ast.walk(registration)
                if isinstance(node, ast.Call) and ast.unparse(node.func) == "ctx.message_producers.register_attachment_message"
            ]
            (keyword,) = [item for item in call.keywords if item.arg == "call"]
            assert ast.unparse(keyword.value) == "tool_call"
            keyword.value = ast.Name(id="foreign", ctx=ast.Load())
        else:
            assert fault == "ordinary_sibling" and len(registration.orelse) == 1
            sibling = registration.orelse[0]
            assert isinstance(sibling, ast.If)
            sibling.test = ast.Constant(value=False)

    changed = _receiving_changed_units(units, transfers.BATCH, change)
    failures, refused = transfers.attachment_local_transfers(changed)
    if fault in {"retained_default", "custody_default"}:
        assert failures == ["continuation attachment declaration namespace changed " + transfers.BATCH]
    elif fault == "ordinary_sibling":
        assert failures == ["P4 batch custody exact result publication changed"]
    else:
        assert failures == ["continuation attachment selected admission/registration changed"]
    assert refused == {}
    assert _receiving_attachment_positive(units) == receipt


def test_receiving_batch_partition_does_not_omit_an_additional_live_field():
    units = fencing._production_units()
    path = "src/elspeth/web/composer/tool_batch.py"

    def change(unit):
        owner = fencing._reserve_node_at(unit, "ContinuationBatchState")
        owner.body.insert(-1, ast.parse("unreviewed_live_owner: object = None").body[0])

    changed = _receiving_changed_units(units, path, change)
    failures, fields = fencing._reserve_batch_retained_field_partition(changed, _receiving_batch_classes(changed))
    assert failures == []
    assert "unreviewed_live_owner" in fields["ContinuationBatchState"]
    failures, receipt = fencing._reserve_continuation_batch_state_receipt(changed)
    assert failures == ["continuation batch carrier/default declaration changed ContinuationBatchState"]
    assert receipt == {}


@pytest.mark.parametrize("fault", ("projection", "constructor", "application_erasure"))
def test_receiving_batch_partition_preserves_complete_projection_constructor_and_application_bodies(fault):
    units = fencing._production_units()
    assert fencing._reserve_continuation_batch_state_receipt(units)[0] == []
    path = (
        "src/elspeth/web/composer/continuation_application_graph.py"
        if fault == "application_erasure"
        else "src/elspeth/web/composer/continuation_batch_data.py"
    )

    def change(unit):
        if fault == "application_erasure":
            method = fencing._reserve_node_at(unit, "application_result_tools")
            call = next(node for node in ast.walk(method) if isinstance(node, ast.Call) and ast.unparse(node.func) == "replace")
            call.keywords.append(ast.keyword(arg="manual_advisor_allowance", value=ast.Constant(value=None)))
        elif fault == "projection":
            method = fencing._reserve_node_at(unit, "_members")
            branch = next(
                node
                for node in method.body
                if isinstance(node, ast.If) and ast.unparse(node.test) == "type(value) is ContinuationBatchState"
            )
            projected = branch.body[0].value.elts[1]
            projected.keys.append(ast.Constant(value="manual_advisor_allowance"))
            projected.values.append(ast.parse("value.manual_advisor_allowance", mode="eval").body)
        else:
            method = fencing._reserve_node_at(unit, "_construct")
            branch = next(
                node for node in method.body if isinstance(node, ast.If) and ast.unparse(node.test) == "kind == 'ContinuationBatchState'"
            )
            branch.body[0].value.keywords.append(ast.keyword(arg="manual_advisor_allowance", value=ast.Constant(value=None)))

    changed = _receiving_changed_units(units, path, change)
    failures, receipt = fencing._reserve_continuation_batch_state_receipt(changed)
    assert failures and receipt == {}
    assert all(item.startswith("batch DATA selected body changed ") for item in failures)
    assert fencing._reserve_continuation_batch_state_receipt(units)[0] == []


def test_receiving_reserve_inventory_never_exempts_an_unresolved_other_receiver():
    units = fencing._production_units()
    assert fencing._reserve_current_call_inventory(units)[0] == []
    foreign = fencing._parse_source(
        "src/elspeth/web/receiving_foreign_reserve_control.py",
        "def unreviewed(value):\n    return value.reserve()\n",
    )
    failures, inventory = fencing._reserve_current_call_inventory((*units, foreign))
    assert failures == ["selected Web reserve call inventory changed"]
    assert inventory == {}
    assert fencing._reserve_current_call_inventory(units)[0] == []


def test_receiving_manual_reservation_receipt_never_reuses_a_detached_call_identity():
    units = fencing._production_units()
    failures, original = fencing._reserve_manual_advisor_allowance_receipt(units)
    assert failures == [] and len(original) == 1
    path = "src/elspeth/web/composer/tool_batch.py"
    reparsed = _receiving_changed_units(units, path, lambda unit: None)
    failures, fresh = fencing._reserve_manual_advisor_allowance_receipt(reparsed)
    assert failures == [] and len(fresh) == 1 and not original & fresh
    actual_nodes = {(unit.path, id(node)) for unit in reparsed for node in ast.walk(unit.tree)}
    assert fresh <= actual_nodes and not original <= actual_nodes
    failures, inventory = fencing._reserve_current_call_inventory(reparsed)
    assert failures == [] and inventory[path] == fresh


@pytest.mark.parametrize("supplier", ("missing", "foreign"))
def test_receiving_provider_context_exception_origin_is_canonical_without_changing_other_protocol_aliases(supplier):
    units = _provider_mint_units()
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == [] and receipt is not None
    unknown = (
        "UNKNOWN_COMMON_SOURCE_IMPORT_BINDING_CLOSURE",
        "UNKNOWN_COMMON_RECEIVER_MEMBER_OUTSIDE_EFFECT_CLOSURE",
        "UNKNOWN_REQUIRED_BINDING_COORDINATOR_AUTHORITY_CONTEXT_CUSTODY",
    )
    assert receipt["remaining_premises"] == unknown
    original = next(unit for unit in units if unit.path == TURN)
    imported = next(
        node for node in original.tree.body if isinstance(node, ast.ImportFrom) and node.module == "elspeth.web.composer.protocol"
    )
    aliases = [(alias.name, alias.asname) for alias in imported.names]
    assert aliases.count(("ComposerContextUnsupported", None)) == 1
    remaining = [copy.deepcopy(alias) for alias in imported.names if alias.name != "ComposerContextUnsupported"]
    replacement = ast.unparse(ast.ImportFrom(module=imported.module, names=remaining, level=imported.level))
    if supplier == "foreign":
        replacement += "\nfrom foreign_context import ComposerContextUnsupported"
    old_import = ast.get_source_segment(original.source, imported)
    assert old_import is not None and original.source.count(old_import) == 1
    changed = fencing._parse_source(TURN, original.source.replace(old_import, replacement, 1))
    actual_import = next(node for node in changed.tree.body if isinstance(node, ast.ImportFrom) and node.module == imported.module)
    assert [(alias.name, alias.asname) for alias in actual_import.names] == [
        alias for alias in aliases if alias[0] != "ComposerContextUnsupported"
    ]
    before_bindings, after_bindings = provider._imports(original.tree), provider._imports(changed.tree)
    assert {name: origin for name, origin in before_bindings.items() if name != "ComposerContextUnsupported"} == {
        name: origin for name, origin in after_bindings.items() if name != "ComposerContextUnsupported"
    }
    if supplier == "missing":
        assert "ComposerContextUnsupported" not in after_bindings
    else:
        assert after_bindings["ComposerContextUnsupported"] == "foreign_context.ComposerContextUnsupported"
    assert ast.dump(fencing._reserve_node_at(changed, "_run_composer_turn"), include_attributes=False) == ast.dump(
        fencing._reserve_node_at(original, "_run_composer_turn"), include_attributes=False
    )
    assert ast.dump(changed.tree, include_attributes=False) == ast.dump(ast.parse(changed.source), include_attributes=False)
    mutated = tuple(changed if unit is original else unit for unit in units)
    failures, refused = provider.provider_mint_local_contracts(mutated)
    assert failures == ["provider mint selected constructor/family/binding import origin changed elspeth.web.sessions.composer_turn"]
    assert refused is None

    sources = {path: (ROOT / path).read_text() for path in fencing._reserve_CANCEL_CONSUMERS}
    assert fencing._reserve_cancel_consumer_failures(sources) == []
    altered_sources = {**sources, TURN: changed.source}
    assert fencing._reserve_cancel_consumer_failures(altered_sources) == ["owned cancellation consumer changed " + TURN]
    assert fencing._reserve_cancel_consumer_failures(sources) == []
    failures, restored = provider.provider_mint_local_contracts(units)
    assert failures == [] and restored is not None and restored["remaining_premises"] == unknown


def _receiving_current_source_binding_units():
    writers = fencing._reserve_json.loads((fencing._reserve_BASE / "reviewed-writer-recipes.json").read_text())
    paths = {key.split(":", 1)[0] for key in writers} | set(fencing._reserve_continuation_transfers.REQUIRED_PATHS)
    return _source_units(tuple(sorted(paths)))


def _receiving_current_source_binding_failures(units):
    transfers = fencing._reserve_continuation_transfers
    transfer_failures, _index, _selected, _receipt = transfers._select(units, transfers.REQUIRED_PATHS, "continuation prerequisite")
    service = next(unit for unit in units if unit.path == transfers.SERVICE)
    return {
        "writer": fencing._reserve_writer_recipe_failures(units),
        "continuation": transfer_failures,
        "compose_methods": fencing._reserve_compose_iteration_method_source_failures(service),
    }


def _receiving_require_current_source_bindings(units):
    failures = _receiving_current_source_binding_failures(units)
    details = [owner + ": " + failure for owner, rows in failures.items() for failure in rows]
    if details:
        pytest.fail("Current source binding prerequisites failed:\n" + "\n".join(details), pytrace=False)


def _receiving_changed_canonical_import(unit, module, binding, fault):
    imported = [
        node
        for node in unit.tree.body
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == module
        and any((alias.asname or alias.name) == binding for alias in node.names)
    ]
    assert len(imported) == 1
    original = imported[0]
    selected = [alias for alias in original.names if (alias.asname or alias.name) == binding]
    assert len(selected) == 1 and selected[0].name == binding and selected[0].asname is None
    remaining = [copy.deepcopy(alias) for alias in original.names if alias is not selected[0]]
    replacement = ast.unparse(ast.ImportFrom(module=module, names=remaining, level=0)) if remaining else ""
    if fault == "foreign":
        replacement += ("\n" if replacement else "") + "from foreign_prerequisite import " + binding
    else:
        assert fault == "missing"
    segment = ast.get_source_segment(unit.source, original)
    assert segment is not None and unit.source.count(segment) == 1
    changed = fencing._parse_source(unit.path, unit.source.replace(segment, replacement, 1))
    before = [
        (node.level, node.module, alias.name, alias.asname)
        for node in unit.tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if (alias.asname or alias.name) != binding
    ]
    after = [
        (node.level, node.module, alias.name, alias.asname)
        for node in changed.tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if (alias.asname or alias.name) != binding
    ]
    assert before == after
    selected_after = [
        (node.level, node.module, alias.name, alias.asname)
        for node in changed.tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if (alias.asname or alias.name) == binding
    ]
    assert selected_after == ([] if fault == "missing" else [(0, "foreign_prerequisite", binding, None)])
    assert [ast.dump(node) for node in unit.tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))] == [
        ast.dump(node) for node in changed.tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    assert ast.dump(changed.tree) == ast.dump(ast.parse(changed.source))
    return changed


def _receiving_changed_rollback_guard(service):
    old = "                            if not unpublished:\n"
    assert service.source.count(old) == 1
    changed = fencing._parse_source(service.path, service.source.replace(old, "                            if False:\n", 1))
    transfers = fencing._reserve_continuation_transfers
    assert transfers._digest(transfers._Namespace().visit(copy.deepcopy(service.tree))) == transfers._digest(
        transfers._Namespace().visit(copy.deepcopy(changed.tree))
    )
    before = fencing._reserve_node_at(service, "ComposerServiceImpl._advance_live_compose_iteration")
    after = fencing._reserve_node_at(changed, "ComposerServiceImpl._advance_live_compose_iteration")
    assert ast.dump(before.args) == ast.dump(after.args)
    assert ast.dump(ast.Module(body=before.body[:-2], type_ignores=[])) == ast.dump(ast.Module(body=after.body[:-2], type_ignores=[]))
    assert ast.dump(ast.Module(body=before.body[-2].body[0].finalbody, type_ignores=[])) == ast.dump(
        ast.Module(body=after.body[-2].body[0].finalbody, type_ignores=[])
    )
    assert ast.dump(changed.tree) == ast.dump(ast.parse(changed.source))
    return changed


def test_receiving_current_source_binding_prerequisites_cover_every_existing_named_binding():
    units = _receiving_current_source_binding_units()
    original_nodes = {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}
    _receiving_require_current_source_bindings(units)
    transfers = fencing._reserve_continuation_transfers
    before = {unit.path: ast.dump(unit.tree) for unit in units if unit.path in transfers.REQUIRED_PATHS}
    failures, index, selected, receipt = transfers._select(units, transfers.REQUIRED_PATHS, "continuation prerequisite")
    assert failures == []
    expected = {(path, name) for path, contracts in transfers._CONTRACTS.items() for name in contracts}
    assert set(selected) == expected
    assert receipt["selected_definitions"] == {(path, id(selected[path, name])) for path, name in expected}
    for path, name in expected:
        assert selected[path, name] is transfers._find(index[path].tree, name)
    assert all(receipt[key] <= original_nodes for key in transfers.SITE_KEYS)
    assert before == {unit.path: ast.dump(unit.tree) for unit in units if unit.path in transfers.REQUIRED_PATHS}


def test_receiving_current_source_binding_prerequisites_report_independent_mismatches_together():
    units = _receiving_current_source_binding_units()
    _receiving_require_current_source_bindings(units)
    transfers = fencing._reserve_continuation_transfers
    changed = []
    for unit in units:
        if unit.path == COMPOSER_PROTOCOL:
            unit = _receiving_changed_canonical_import(unit, "typing", "get_args", "missing")
        elif unit.path == transfers.BATCH:
            unit = _receiving_changed_canonical_import(
                unit, "elspeth.web.composer.manual_advisor_allowance", "ManualAdvisorAllowance", "foreign"
            )
        elif unit.path == transfers.SERVICE:
            unit = _receiving_changed_canonical_import(unit, "sqlalchemy.exc", "IntegrityError", "missing")
            unit = _receiving_changed_rollback_guard(unit)
        changed.append(unit)
    assert _receiving_current_source_binding_failures(tuple(changed)) == {
        "writer": ["writer target/field recipe changed " + COMPOSER_PROTOCOL + ":module-bindings"],
        "continuation": [
            "continuation prerequisite declaration namespace changed " + transfers.SERVICE,
            "continuation prerequisite declaration namespace changed " + transfers.BATCH,
        ],
        "compose_methods": ["compose iteration reviewed method source changed _advance_live_compose_iteration"],
    }
    _receiving_require_current_source_bindings(units)


@pytest.mark.parametrize("binding", ("cast", "get_args"))
@pytest.mark.parametrize("fault", ("missing", "foreign"))
def test_receiving_protocol_namespace_refuses_isolated_new_typing_import(binding, fault):
    units = fencing._production_units()
    _receiving_require_current_source_bindings(units)
    assert fencing._reserve_recipe_failures(units) == []
    original = next(unit for unit in units if unit.path == COMPOSER_PROTOCOL)
    changed = _receiving_changed_canonical_import(original, "typing", binding, fault)
    mutated = tuple(changed if unit is original else unit for unit in units)
    expected = ["writer target/field recipe changed " + COMPOSER_PROTOCOL + ":module-bindings"]
    assert fencing._reserve_writer_recipe_failures(mutated) == expected
    assert fencing._reserve_recipe_failures(mutated) == expected
    assert fencing._reserve_recipe_failures(units) == []
    _receiving_require_current_source_bindings(units)


def test_receiving_writer_prerequisite_preserves_single_and_multiple_refusals():
    units = fencing._production_units()
    _receiving_require_current_source_bindings(units)
    original = next(unit for unit in units if unit.path == COMPOSER_PROTOCOL)
    changed = _receiving_changed_canonical_import(original, "typing", "get_args", "missing")
    mutated = tuple(changed if unit is original else unit for unit in units)
    assert fencing._reserve_writer_recipe_failures(mutated) == [
        "writer target/field recipe changed " + COMPOSER_PROTOCOL + ":module-bindings"
    ]
    method = fencing._reserve_node_at(changed, "ComposerResult.__post_init__")
    guard = method.body[0]
    assert isinstance(guard, ast.If) and ast.unparse(guard.test).startswith("self.graph_review_identity is not None and ")
    guard.test = ast.Constant(value=False)
    double = fencing._parse_source(changed.path, ast.unparse(ast.fix_missing_locations(changed.tree)))
    doubled = tuple(double if unit is original else unit for unit in units)
    expected = [
        "writer target/field recipe changed " + COMPOSER_PROTOCOL + ":ComposerResult",
        "writer target/field recipe changed " + COMPOSER_PROTOCOL + ":module-bindings",
    ]
    assert fencing._reserve_writer_recipe_failures(doubled) == expected
    assert fencing._reserve_recipe_failures(doubled) == expected
    _receiving_require_current_source_bindings(units)


@pytest.mark.parametrize(
    ("path", "module", "binding"),
    (
        ("src/elspeth/web/composer/tool_batch.py", "elspeth.web.composer.manual_advisor_allowance", "ManualAdvisorAllowance"),
        ("src/elspeth/web/composer/tool_batch.py", "elspeth.web.composer.protocol", "ComposerContextUnsupported"),
        ("src/elspeth/web/composer/service.py", "elspeth.web.composer.protocol", "ComposerContextUnsupported"),
        ("src/elspeth/web/composer/service.py", "sqlalchemy.exc", "IntegrityError"),
    ),
)
@pytest.mark.parametrize("fault", ("missing", "foreign"))
def test_receiving_transfer_namespaces_refuse_isolated_new_canonical_imports(path, module, binding, fault):
    units = fencing._production_units()
    _receiving_require_current_source_bindings(units)
    transfers = fencing._reserve_continuation_transfers
    checks = (
        ((transfers.attachment_local_transfers, "continuation attachment"),)
        if path == transfers.BATCH
        else (
            (transfers.initialization_local_transfers, "continuation initialization"),
            (transfers.iteration_local_transfers, "continuation iteration"),
        )
    )
    original = next(unit for unit in units if unit.path == path)
    changed = _receiving_changed_canonical_import(original, module, binding, fault)
    mutated = tuple(changed if unit is original else unit for unit in units)
    for check, phase in checks:
        failures, receipt = check(units)
        assert failures == [] and receipt
        failures, refused = check(mutated)
        assert failures == [phase + " declaration namespace changed " + path]
        assert refused == {}
        failures, restored = check(units)
        assert failures == [] and restored
    _receiving_require_current_source_bindings(units)


@pytest.mark.parametrize("fault", ("default", "type", "position"))
def test_receiving_attachment_namespace_refuses_changed_live_allowance_declaration(fault):
    units = fencing._production_units()
    _receiving_require_current_source_bindings(units)
    transfers = fencing._reserve_continuation_transfers
    failures, receipt = transfers.attachment_local_transfers(units)
    assert failures == [] and receipt
    original = next(unit for unit in units if unit.path == transfers.BATCH)
    declaration = "    manual_advisor_allowance: ManualAdvisorAllowance | None = field(default=None, repr=False)\n"
    assert original.source.count(declaration) == 1
    if fault == "default":
        source = original.source.replace(declaration, declaration.replace("default=None", "default=False"), 1)
    elif fault == "type":
        source = original.source.replace(declaration, declaration.replace("ManualAdvisorAllowance | None", "object | None"), 1)
    else:
        assert fault == "position"
        predecessor = "    p4_verification_failed: bool = False\n"
        assert original.source.count(predecessor) == 1
        (owner,) = [node for node in original.tree.body if isinstance(node, ast.ClassDef) and node.name == "ContinuationBatchState"]
        (allowance,) = [
            node
            for node in owner.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "manual_advisor_allowance"
        ]
        (previous,) = [
            node
            for node in owner.body
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "p4_verification_failed"
        ]
        assert ast.get_source_segment(original.source, allowance) == declaration.strip()
        assert ast.get_source_segment(original.source, previous) == predecessor.strip()
        previous_index, allowance_index = owner.body.index(previous), owner.body.index(allowance)
        assert allowance_index == previous_index + 1
        without_allowance = original.source.replace(declaration, "", 1)
        source = without_allowance.replace(predecessor, declaration + predecessor, 1)
    changed = fencing._parse_source(original.path, source)
    if fault == "position":
        (changed_owner,) = [node for node in changed.tree.body if isinstance(node, ast.ClassDef) and node.name == "ContinuationBatchState"]
        expected_body = [ast.dump(node) for node in owner.body]
        expected_body[previous_index], expected_body[allowance_index] = expected_body[allowance_index], expected_body[previous_index]
        assert [ast.dump(node) for node in changed_owner.body] == expected_body
        inverse = copy.deepcopy(changed.tree)
        (inverse_owner,) = [node for node in inverse.body if isinstance(node, ast.ClassDef) and node.name == "ContinuationBatchState"]
        inverse_owner.body[previous_index], inverse_owner.body[allowance_index] = (
            inverse_owner.body[allowance_index],
            inverse_owner.body[previous_index],
        )
        assert ast.dump(inverse) == ast.dump(original.tree)
        assert ast.dump(changed.tree) == ast.dump(ast.parse(changed.source))
    mutated = tuple(changed if unit is original else unit for unit in units)
    failures, refused = transfers.attachment_local_transfers(mutated)
    assert failures == ["continuation attachment declaration namespace changed " + transfers.BATCH]
    assert refused == {}
    failures, restored = transfers.attachment_local_transfers(units)
    assert failures == [] and restored
    _receiving_require_current_source_bindings(units)


def test_receiving_compose_method_seal_refuses_erased_accepted_rollback_guard():
    units = fencing._production_units()
    _receiving_require_current_source_bindings(units)
    failures, receipt = fencing._reserve_compose_iteration_state_receipt(units)
    assert failures == [] and receipt
    path = fencing._reserve_continuation_transfers.SERVICE
    original = next(unit for unit in units if unit.path == path)
    changed = _receiving_changed_rollback_guard(original)
    mutated = tuple(changed if unit is original else unit for unit in units)
    expected = ["compose iteration reviewed method source changed _advance_live_compose_iteration"]
    assert fencing._reserve_compose_iteration_method_source_failures(changed) == expected
    failures, refused = fencing._reserve_compose_iteration_state_receipt(mutated)
    assert failures == expected and refused == {}
    failures, restored = fencing._reserve_compose_iteration_state_receipt(units)
    assert failures == [] and restored
    _receiving_require_current_source_bindings(units)


@pytest.mark.parametrize("fault", ("reason", "detail", "guard"))
def test_receiving_protocol_namespace_refuses_changed_closed_refusal_statements(fault):
    units = fencing._production_units()
    _receiving_require_current_source_bindings(units)
    assert fencing._reserve_writer_recipe_failures(units) == []
    assert fencing._reserve_recipe_failures(units) == []
    original = next(unit for unit in units if unit.path == COMPOSER_PROTOCOL)
    if fault == "reason":
        selected = fencing._reserve_node_at(original, "ComposerContextUnsupportedReason")
        assert isinstance(selected, ast.Assign) and isinstance(selected.value, ast.Subscript)
        assert ast.unparse(selected.value.value) == "Literal"
        assert isinstance(selected.value.slice, ast.Tuple)
        assert [item.value for item in selected.value.slice.elts] == [
            "graph_resource_limit",
            "graph_coverage_unavailable",
            "manual_advisor_allowance",
        ]
        changed_node = copy.deepcopy(selected)
        changed_node.value.slice.elts[-1].value = "unreviewed_reason"
        replacement = ast.unparse(changed_node)
        symbol = "ComposerContextUnsupportedReason"
    elif fault == "detail":
        selected = fencing._reserve_node_at(original, "_CONTEXT_UNSUPPORTED_DETAILS")
        assert isinstance(selected, ast.AnnAssign) and isinstance(selected.value, ast.Call)
        assert ast.unparse(selected.value.func) == "MappingProxyType"
        assert len(selected.value.args) == 1 and not selected.value.keywords
        details = selected.value.args[0]
        assert isinstance(details, ast.Dict)
        assert [key.value for key in details.keys] == ["graph_resource_limit", "graph_coverage_unavailable", "manual_advisor_allowance"]
        assert details.values[-1].value == (
            "The Advisor request exceeds its input allowance. Reduce the request size before sending again."
        )
        changed_node = copy.deepcopy(selected)
        changed_node.value.args[0].values[-1].value = "unreviewed public detail"
        replacement = ast.unparse(changed_node)
        symbol = "_CONTEXT_UNSUPPORTED_DETAILS"
    else:
        assert fault == "guard"
        guards = [
            node
            for node in original.tree.body
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "frozenset(_CONTEXT_UNSUPPORTED_DETAILS) != frozenset(get_args(ComposerContextUnsupportedReason))"
        ]
        assert len(guards) == 1
        selected = guards[0]
        assert ast.unparse(selected.body[0]) == ("raise FrameworkBugError('Composer context refusal reasons and public details disagree')")
        replacement = ""
    segment = ast.get_source_segment(original.source, selected)
    assert segment is not None and original.source.count(segment) == 1
    changed = fencing._parse_source(original.path, original.source.replace(segment, replacement, 1))
    if fault == "guard":
        remaining = changed.tree.body
    else:
        actual = fencing._reserve_node_at(changed, symbol)
        assert ast.dump(actual) == ast.dump(changed_node)
        remaining = [node for node in changed.tree.body if node is not actual]
    assert [ast.dump(node) for node in original.tree.body if node is not selected] == [ast.dump(node) for node in remaining]
    assert ast.dump(changed.tree) == ast.dump(ast.parse(changed.source))
    mutated = tuple(changed if unit is original else unit for unit in units)
    expected = ["writer target/field recipe changed " + COMPOSER_PROTOCOL + ":module-bindings"]
    assert fencing._reserve_writer_recipe_failures(mutated) == expected
    assert fencing._reserve_recipe_failures(mutated) == expected
    assert fencing._reserve_writer_recipe_failures(units) == []
    assert fencing._reserve_recipe_failures(units) == []
    _receiving_require_current_source_bindings(units)


def _receiving_reparse_mutated_unit(unit):
    return fencing._parse_source(unit.path, ast.unparse(ast.fix_missing_locations(unit.tree)))


def _receiving_turn_invoke(unit):
    turn = fencing._reserve_node_at(unit, "_run_composer_turn")
    assert isinstance(turn.body[36], ast.Try)
    owner = turn.body[36]
    assert ast.unparse(owner.body[18]) == "composer = services.composer_service"
    assert isinstance(owner.body[24], ast.Try)
    return owner.body[24]


def test_receiving_seven_boundary_local_producers_have_current_positives():
    units = fencing._production_units()
    failures = []
    for label, check in (
        ("family", family.family_local_contracts),
        ("compose", compose.compose_proposal_local_transfers),
        ("receiver", receiver.composer_receiver_local_contracts),
        ("worker", worker_origin.worker_whole_scope_origin_local_contracts),
        ("sink", fencing._reserve_sink_effect_disjoint_receipts),
        ("route", route.route_helper_local_contracts),
        ("proposal", fencing._reserve_proposal_child_transfers.proposal_child_local_contracts),
    ):
        current, _conditional = check(units)
        failures.extend(label + ": " + failure for failure in current)
    if failures:
        pytest.fail("Current boundary prerequisites failed:\n" + "\n".join(failures), pytrace=False)


@pytest.mark.parametrize("fault", ("missing", "shared", "moved", "duplicate_slot", "foreign_slot"))
def test_receiving_continuation_stage_list_is_fresh_and_exactly_owned(fault):
    path = "src/elspeth/web/coordination/lifecycle.py"
    original = _source_units((path,))[0]
    assert family._check_contracts(original, family._lease_contracts())[0] == []
    cls = family._find(original.tree, "SessionOperationLease")
    slots = next(
        node.value for node in cls.body if isinstance(node, ast.Assign) and any(receiver.name(t, "__slots__") for t in node.targets)
    )
    names = tuple(node.value for node in slots.elts)
    assert names.count("_continuation_stages") == 1
    assert family._owned_class(original, "SessionOperationLease", names, ("final",)) == []
    changed = fencing._parse_source(path, original.source)
    constructor = family._find(changed.tree, "SessionOperationLease.__init__")
    (allocation,) = [
        node for node in constructor.body if isinstance(node, ast.AnnAssign) and ast.unparse(node.target) == "self._continuation_stages"
    ]
    assert isinstance(allocation.value, ast.List) and allocation.value.elts == []
    if fault == "missing":
        constructor.body.remove(allocation)
    elif fault == "shared":
        allocation.value = ast.Name(id="shared_stages", ctx=ast.Load())
    elif fault == "moved":
        constructor.body.remove(allocation)
        constructor.body.insert(0, allocation)
    else:
        changed_cls = family._find(changed.tree, "SessionOperationLease")
        changed_slots = next(
            node.value
            for node in changed_cls.body
            if isinstance(node, ast.Assign) and any(receiver.name(t, "__slots__") for t in node.targets)
        )
        changed_slots.elts.append(ast.Constant("_continuation_stages" if fault == "duplicate_slot" else "foreign"))
    changed = _receiving_reparse_mutated_unit(changed)
    if fault in {"duplicate_slot", "foreign_slot"}:
        assert family._owned_class(changed, "SessionOperationLease", names, ("final",)) == [
            "SessionOperationLease: owned slot/descriptor namespace changed"
        ]
    else:
        assert (
            "SessionOperationLease.__init__: closed execution contract changed (guard/transfer/effect/branch)"
            in family._check_contracts(changed, family._lease_contracts())[0]
        )
    assert family._check_contracts(original, family._lease_contracts())[0] == []


@pytest.mark.parametrize("fault", ("missing", "required", "foreign_default", "extra"))
def test_receiving_public_handoff_deadline_retains_exact_optional_header(fault):
    path = "src/elspeth/web/coordination/lifecycle.py"
    original = _source_units((path,))[0]
    cls = family._find(original.tree, "SessionOperationLease")
    slots = next(
        node.value for node in cls.body if isinstance(node, ast.Assign) and any(receiver.name(t, "__slots__") for t in node.targets)
    )
    names = tuple(node.value for node in slots.elts)
    method = family._find(original.tree, "SessionOperationLease.consume_continuation_handoff")
    assert family._owned_method_definition_failures(original, cls, method, names) == []
    changed = fencing._parse_source(path, original.source)
    selected = family._find(changed.tree, "SessionOperationLease.consume_continuation_handoff")
    assert [parameter.arg for parameter in selected.args.kwonlyargs] == ["deadline"]
    if fault == "missing":
        selected.args.kwonlyargs.clear()
        selected.args.kw_defaults.clear()
    elif fault == "required":
        selected.args.kw_defaults[0] = None
    elif fault == "foreign_default":
        selected.args.kw_defaults[0] = ast.Call(func=ast.Name(id="foreign_deadline", ctx=ast.Load()), args=[], keywords=[])
    else:
        selected.args.kwonlyargs.append(ast.arg(arg="foreign"))
        selected.args.kw_defaults.append(ast.Constant(None))
    changed = _receiving_reparse_mutated_unit(changed)
    changed_cls = family._find(changed.tree, "SessionOperationLease")
    changed_method = family._find(changed.tree, "SessionOperationLease.consume_continuation_handoff")
    assert family._owned_method_definition_failures(changed, changed_cls, changed_method, names) == [
        "SessionOperationLease.consume_continuation_handoff: unsupported class method definition scope/descriptor"
    ]


@pytest.mark.parametrize(
    "method_name", ("_consume_continuation_handoff", "consume_continuation_handoff", "create_continuation_stage", "join_continuation_stage")
)
@pytest.mark.parametrize("fault", ("name", "bound", "default", "variadic", "extra", "descriptor", "eager_default", "other_method"))
def test_receiving_handoff_generic_definition_roles_are_exact(method_name, fault):
    path = "src/elspeth/web/coordination/lifecycle.py"
    original = _source_units((path,))[0]
    cls = family._find(original.tree, "SessionOperationLease")
    slots = next(
        node.value for node in cls.body if isinstance(node, ast.Assign) and any(receiver.name(t, "__slots__") for t in node.targets)
    )
    names = tuple(node.value for node in slots.elts)
    method = family._find(original.tree, "SessionOperationLease." + method_name)
    assert family._owned_method_definition_failures(original, cls, method, names) == []
    changed = fencing._parse_source(path, original.source)
    selected = family._find(changed.tree, "SessionOperationLease." + method_name)
    assert len(selected.type_params) == 1 and type(selected.type_params[0]) is ast.TypeVar
    if fault == "name":
        selected.type_params[0].name = "Foreign"
    elif fault == "bound":
        selected.type_params[0].bound = ast.Call(func=ast.Name(id="foreign_bound", ctx=ast.Load()), args=[], keywords=[])
    elif fault == "default":
        default_attack = ast.Call(func=ast.Name(id="foreign_default", ctx=ast.Load()), args=[], keywords=[])
        selected.type_params[0].default_value = default_attack
    elif fault == "variadic":
        selected.type_params = [ast.TypeVarTuple(name="T")]
    elif fault == "extra":
        selected.type_params.append(ast.TypeVar(name="Foreign", bound=None))
    elif fault == "descriptor":
        selected.decorator_list = [ast.Name(id="classmethod", ctx=ast.Load())]
    elif fault == "eager_default":
        selected.args.defaults = [ast.Call(func=ast.Name(id="foreign_default", ctx=ast.Load()), args=[], keywords=[])]
    else:
        assert fault == "other_method"
        selected.name = "unreviewed_handoff"
    changed_name = selected.name
    if fault == "default":
        # Python 3.12 cannot round-trip PEP 696 source. This case attacks the
        # parsed owner's metadata directly; no source-support claim follows.
        assert selected.type_params[0].default_value is default_attack
    else:
        changed = _receiving_reparse_mutated_unit(changed)
    changed_cls = family._find(changed.tree, "SessionOperationLease")
    changed_method = family._find(changed.tree, "SessionOperationLease." + changed_name)
    if fault == "default":
        assert changed_method is selected and changed_method.type_params[0].default_value is default_attack
    assert family._owned_method_definition_failures(changed, changed_cls, changed_method, names) == [
        "SessionOperationLease." + changed_name + ": unsupported class method definition scope/descriptor"
    ]
    assert family._owned_method_definition_failures(original, cls, method, names) == []


@pytest.mark.parametrize(
    "fault", ("missing", "duplicate", "order", "foreign_type", "delete_audit", "early_durable", "foreign_work", "revocation")
)
def test_receiving_context_handler_role_preserves_audit_and_closed_order(fault):
    original = _source_units((TURN,))[0]
    invoke = _receiving_turn_invoke(original)
    assert receiver.composer_title_prelude(invoke) is invoke.body[0]
    dispatch = receiver.composer_dispatch_calls(invoke)
    assert dispatch is not None and len(dispatch) == 2
    changed = fencing._parse_source(TURN, original.source)
    candidate = _receiving_turn_invoke(changed)
    handler = candidate.handlers[10]
    assert ast.unparse(handler.type) == "ComposerContextUnsupported"
    if fault == "missing":
        candidate.handlers.remove(handler)
    elif fault == "duplicate":
        candidate.handlers.insert(10, copy.deepcopy(handler))
    elif fault == "order":
        candidate.handlers[9], candidate.handlers[10] = candidate.handlers[10], candidate.handlers[9]
    elif fault == "foreign_type":
        handler.type = ast.Name(id="ForeignContextUnsupported", ctx=ast.Load())
    elif fault == "delete_audit":
        assert isinstance(handler.body[3], ast.If) and isinstance(handler.body[3].body[0].value, ast.Await)
        handler.body[3].body.pop(0)
    elif fault == "early_durable":
        handler.body[3].body.reverse()
    elif fault == "foreign_work":
        work = [node for node in ast.walk(handler) if isinstance(node, ast.keyword) and node.arg == "required_work"]
        assert len(work) == 1
        work[0].value = ast.Name(id="foreign_work", ctx=ast.Load())
    else:
        assert fault == "revocation"
        revocation = candidate.handlers[12]
        assert ast.unparse(revocation.type) == "ComposerMutationAuthorityRevoked"
        statuses = [node for node in ast.walk(revocation) if isinstance(node, ast.keyword) and node.arg == "status_code"]
        assert len(statuses) == 1 and statuses[0].value.value == 409
        statuses[0].value = ast.Constant(value=200)
    changed = _receiving_reparse_mutated_unit(changed)
    rejected = _receiving_turn_invoke(changed)
    assert receiver.composer_title_prelude(rejected) is None
    assert receiver.composer_dispatch_calls(rejected) is None
    assert receiver.composer_title_prelude(invoke) is invoke.body[0]
    assert receiver.composer_dispatch_calls(invoke) == dispatch


@pytest.mark.parametrize(
    ("owner", "fault"),
    (
        ("compose_service", "omit"),
        ("compose_service", "effect"),
        ("compose_turn", "delete_audit"),
        ("compose_turn", "early_durable"),
        ("route_turn", "delete_audit"),
        ("route_turn", "early_durable"),
    ),
)
def test_receiving_compose_and_route_shapes_bind_the_accepted_context_branch(owner, fault):
    if owner == "route_turn":
        units = _source_units((TURN, route.HELPERS))
        check = route.route_helper_local_contracts
    else:
        units = _source_units((TURN, compose.SERVICE, compose.PROPOSALS, compose.SETTLEMENT))
        check = compose.compose_proposal_local_transfers
    failures, _conditional = check(units)
    assert failures == []
    path = compose.SERVICE if owner == "compose_service" else TURN
    original = next(unit for unit in units if unit.path == path)
    changed = fencing._parse_source(path, original.source)
    if owner == "compose_service":
        method = fencing._reserve_node_at(changed, "ComposerServiceImpl.compose")
        handlers = [
            node
            for node in ast.walk(method)
            if isinstance(node, ast.ExceptHandler) and ast.unparse(node.type) == "(PromptGuardRefused, ComposerContextUnsupported)"
        ]
        assert len(handlers) == 1
        if fault == "omit":
            handlers[0].type = ast.Name(id="PromptGuardRefused", ctx=ast.Load())
        else:
            assert fault == "effect"
            handlers[0].body.insert(0, ast.Expr(value=ast.Call(func=ast.Name(id="foreign_effect", ctx=ast.Load()), args=[], keywords=[])))
    else:
        handler = _receiving_turn_invoke(changed).handlers[10]
        assert ast.unparse(handler.type) == "ComposerContextUnsupported"
        if fault == "delete_audit":
            handler.body[3].body.pop(0)
        else:
            assert fault == "early_durable"
            handler.body[3].body.reverse()
    changed = _receiving_reparse_mutated_unit(changed)
    mutated = tuple(changed if unit is original else unit for unit in units)
    failures, conditional = check(mutated)
    if owner == "route_turn":
        assert failures == ["route helpers: complete finite grammar changed _run_composer_turn"]
        assert conditional["reserve_calls"] == [] and conditional["caller_calls"] == []
    else:
        assert failures == ["composer complete executed method grammar changed " + path]
        assert conditional is None
    assert check(units)[0] == []


@pytest.mark.parametrize("fault", ("missing", "force_v2", "foreign_settings", "key"))
def test_receiving_worker_status_member_remains_bound_to_actual_gate(fault):
    units = _source_units((worker_origin.APP, worker_origin.WORKER, worker_origin.TURN))
    assert worker_origin.worker_whole_scope_origin_local_contracts(units)[0] == []
    original = units[0]
    changed = fencing._parse_source(original.path, original.source)
    status = receiver.find(changed.tree, "_create_app.system_status")
    result = next(node.value for node in status.body if isinstance(node, ast.Return))
    assert isinstance(result, ast.Dict)
    indexes = [
        i for i, key in enumerate(result.keys) if isinstance(key, ast.Constant) and key.value == "composer_operation_admission_version"
    ]
    assert len(indexes) == 1
    index = indexes[0]
    assert ast.unparse(result.values[index]) == "2 if settings.composer_continuation_enabled else 1"
    if fault == "missing":
        result.keys.pop(index)
        result.values.pop(index)
    elif fault == "force_v2":
        result.values[index] = ast.Constant(value=2)
    elif fault == "foreign_settings":
        result.values[index].test.value = ast.Name(id="foreign_settings", ctx=ast.Load())
    else:
        assert fault == "key"
        result.keys[index].value = "unreviewed_admission_version"
    changed = _receiving_reparse_mutated_unit(changed)
    failures, conditional = worker_origin.worker_whole_scope_origin_local_contracts((changed, *units[1:]))
    assert failures == ["worker whole scope: complete finite whole-body grammar changed _create_app"]
    assert all(item["qualification"] == "UNKNOWN" for item in conditional["effect_dependencies"])
    assert worker_origin.worker_whole_scope_origin_local_contracts(units)[0] == []


@pytest.mark.parametrize("fault", ("resume", "final_close", "ticket", "owned_join"))
def test_receiving_sink_lease_source_seal_covers_new_handoff_effects(fault):
    units = fencing._production_units()
    failures, receipt = fencing._reserve_sink_effect_disjoint_receipts(units)
    assert failures == [] and receipt["reserve_calls"]
    path = "src/elspeth/web/coordination/lifecycle.py"
    original = next(unit for unit in units if unit.path == path)
    changed = fencing._parse_source(path, original.source)
    handoff = fencing._reserve_node_at(changed, "SessionOperationLease._consume_continuation_handoff")
    if fault == "resume":
        resume = fencing._reserve_node_at(changed, "SessionOperationLease._resume_after_refused_handoff")
        assert ast.unparse(resume.body[1]) == "self._close_task = None"
        resume.body[1].value = ast.Name(id="foreign_task", ctx=ast.Load())
    elif fault == "final_close":
        closing = handoff.body[-1].finalbody[0]
        assert ast.unparse(closing.test) == "not resumed"
        closing.test = ast.Constant(value=True)
    elif fault == "ticket":
        calls = [
            node
            for node in ast.walk(handoff)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "self._required_work.prepare_continuation_handoff"
        ]
        assert len(calls) == 1
        calls[0].func.value = ast.Name(id="foreign_work", ctx=ast.Load())
    else:
        assert fault == "owned_join"
        joins = [
            node for node in ast.walk(handoff) if isinstance(node, ast.Await) and ast.unparse(node.value) == "self._join_owned_tasks()"
        ]
        assert len(joins) == 1
        joins[0].value = ast.Call(func=ast.Name(id="foreign_join", ctx=ast.Load()), args=[], keywords=[])
    changed = _receiving_reparse_mutated_unit(changed)
    mutated = tuple(changed if unit is original else unit for unit in units)
    failures, refused = fencing._reserve_sink_effect_disjoint_receipts(mutated)
    assert failures == ["sink-effect normal callback source changed SessionOperationLease"] and refused == {}
    assert fencing._reserve_sink_effect_disjoint_receipts(units)[0] == []


@pytest.mark.parametrize("method_name", ("prepare_continuation_handoff", "seal_after_continuation_handoff"))
@pytest.mark.parametrize("fault", ("remove", "descriptor", "default", "extra_method"))
def test_receiving_proposal_namespace_keeps_exact_handoff_declarations(method_name, fault):
    helper = fencing._reserve_proposal_child_transfers
    units = _source_units((helper.SETTLEMENT, helper.REQUIRED))
    failures, receipt = helper.proposal_child_local_contracts(units)
    assert failures == [] and receipt["child_transfer_call"] is not None and receipt["reserve_calls"]
    original = units[1]
    changed = fencing._parse_source(original.path, original.source)
    cls = receiver.find(changed.tree, "RequiredWorkCoordinator")
    method = receiver.find(changed.tree, "RequiredWorkCoordinator." + method_name)
    if fault == "remove":
        cls.body.remove(method)
    elif fault == "descriptor":
        method.decorator_list = [ast.Name(id="classmethod", ctx=ast.Load())]
    elif fault == "default":
        method.args.kwonlyargs.append(ast.arg(arg="unreviewed"))
        method.args.kw_defaults.append(ast.Call(func=ast.Name(id="foreign_default", ctx=ast.Load()), args=[], keywords=[]))
    else:
        assert fault == "extra_method"
        cls.body.append(ast.parse("def unreviewed_handoff(self):\n    return None\n").body[0])
    changed = _receiving_reparse_mutated_unit(changed)
    failures, refused = helper.proposal_child_local_contracts((units[0], changed))
    assert failures == ["proposal child transfer: finite supplier class namespace changed RequiredWorkCoordinator"]
    assert refused["child_transfer_call"] is None and refused["reserve_calls"] == []
    assert all(item["qualification"] == "UNKNOWN" for item in refused["effect_dependencies"])
    assert helper.proposal_child_local_contracts(units)[0] == []


@pytest.mark.parametrize(
    "fault",
    (
        "suspend_cancel",
        "suspend_claim",
        "suspend_release_order",
        "yield_checkpoint",
        "yield_cursor",
        "release_predicates",
        "release_rowcount",
        "foreign_recovery_import",
        "foreign_yield_import",
    ),
)
def test_receiving_sink_repository_source_seal_covers_handover_yield_and_release(fault):
    units = fencing._production_units()
    failures, receipt = fencing._reserve_sink_effect_disjoint_receipts(units)
    assert failures == [] and receipt["reserve_calls"]
    path = "src/elspeth/web/coordination/repository.py"
    (original,) = [unit for unit in units if unit.path == path]
    changed = fencing._parse_source(path, original.source)
    suspend = fencing._reserve_node_at(changed, "_SessionOperationAuthorityRepository.suspend_composer_segment")
    yielding = fencing._reserve_node_at(changed, "_SessionOperationAuthorityRepository.commit_checkpoint_and_successor")
    release = fencing._reserve_node_at(changed, "_SessionOperationAuthorityRepository._release_fence_on_connection")
    if fault == "suspend_cancel":
        mutations = [
            node for node in ast.walk(suspend) if isinstance(node, ast.Call) and ast.unparse(node.func) == "require_continuation_mutation"
        ]
        assert len(mutations) == 1
        assert ast.unparse(mutations[0]) == ("require_continuation_mutation(conn, context, allow_cancel=False, allow_expired_segment=True)")
        (cancel,) = [keyword for keyword in mutations[0].keywords if keyword.arg == "allow_cancel"]
        cancel.value = ast.Constant(value=True)
    elif fault == "suspend_claim":
        guards = [
            node
            for node in ast.walk(suspend)
            if isinstance(node, ast.If)
            and ast.unparse(node.test) == "segment.claim_token != claim.claim_token or parent.attempt != claim.attempt"
        ]
        assert len(guards) == 1
        assert ast.unparse(guards[0].body[0]) == (
            "raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)"
        )
        guards[0].test = ast.Constant(value=False)
    elif fault == "suspend_release_order":
        (transaction,) = [node for node in suspend.body if isinstance(node, ast.With)]
        transitions = [
            statement
            for statement in transaction.body
            if isinstance(statement, ast.Assign)
            and ast.unparse(statement.targets[0]) == "receipt"
            and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func) == "recover_segment_on_connection"
        ]
        releases = [
            statement
            for statement in transaction.body
            if ast.unparse(statement) == "self._release_fence_on_connection(conn, context, database_now=now)"
        ]
        assert len(transitions) == len(releases) == 1
        assert transaction.body.index(transitions[0]) < transaction.body.index(releases[0])
        transaction.body.remove(releases[0])
        transaction.body.insert(transaction.body.index(transitions[0]), releases[0])
    elif fault in {"yield_checkpoint", "yield_cursor"}:
        transitions = [
            node for node in ast.walk(yielding) if isinstance(node, ast.Call) and ast.unparse(node.func) == "yield_segment_on_connection"
        ]
        assert len(transitions) == 1
        assert ast.unparse(transitions[0]) == (
            "yield_segment_on_connection(conn, running, expected_head=expected_head, expected_checkpoint_id=expected_checkpoint_id, "
            "expected_cursor=expected_cursor, snapshot_json=snapshot_json, reason=reason, now=now)"
        )
        parameter = "expected_checkpoint_id" if fault == "yield_checkpoint" else "expected_cursor"
        (argument,) = [keyword for keyword in transitions[0].keywords if keyword.arg == parameter]
        argument.value = ast.Name(id="foreign_" + parameter, ctx=ast.Load())
    elif fault == "release_predicates":
        predicates = [
            node for node in ast.walk(release) if isinstance(node, ast.Call) and ast.unparse(node.func) == "self._exact_active_predicates"
        ]
        assert len(predicates) == 1 and ast.unparse(predicates[0]) == "self._exact_active_predicates(context, database_now)"
        predicates[0].func = ast.Name(id="foreign_predicates", ctx=ast.Load())
    elif fault == "release_rowcount":
        guards = [node for node in release.body if isinstance(node, ast.If)]
        assert len(guards) == 1 and ast.unparse(guards[0].test) == "result.rowcount != 1"
        assert ast.unparse(guards[0].body[0]) == (
            "self._raise_fence_lost(conn, context, database_now=database_now, "
            "execution_release=context.operation_kind is SessionOperationKind.EXECUTE)"
        )
        guards[0].test = ast.Constant(value=False)
    else:
        assert fault in {"foreign_recovery_import", "foreign_yield_import"}
        method = suspend if fault == "foreign_recovery_import" else yielding
        imports = [
            statement
            for statement in method.body
            if isinstance(statement, ast.ImportFrom) and statement.module == "elspeth.web.sessions.continuation"
        ]
        assert len(imports) == 1 and imports[0].level == 0
        assert [(alias.name, alias.asname) for alias in imports[0].names] == (
            [("recover_segment_on_connection", None), ("require_continuation_mutation", None)]
            if fault == "foreign_recovery_import"
            else [("yield_segment_on_connection", None)]
        )
        imports[0].module = "foreign.continuation"
    changed = _receiving_reparse_mutated_unit(changed)
    mutated = tuple(changed if unit is original else unit for unit in units)
    failures, refused = fencing._reserve_sink_effect_disjoint_receipts(mutated)
    assert failures == ["sink-effect normal callback source changed _SessionOperationAuthorityRepository"] and refused == {}
    restored_failures, restored_receipt = fencing._reserve_sink_effect_disjoint_receipts(units)
    assert restored_failures == [] and restored_receipt["reserve_calls"] == receipt["reserve_calls"]


def test_origin_diagnostics_preserve_admitted_owner_and_refused_boundary_data():
    units = (
        *_binding_fact_units(),
        *_source_units(
            (
                "src/elspeth/web/async_workers.py",
                "src/elspeth/web/app.py",
                "src/elspeth/web/application_finalizers.py",
                "src/elspeth/web/process_recovery.py",
                "src/elspeth/web/coordination/repository.py",
            )
        ),
    )
    original_uses, observed_uses = [], []
    original = fencing._reserve_selected_origin_effect_failures(units, extra_roots=_BINDING_FACT_ROOTS, protected_use_nodes=original_uses)
    diagnostics = fencing._reserve_OriginDiagnostics(units)
    observed = fencing._reserve_selected_origin_effect_failures(
        units, extra_roots=_BINDING_FACT_ROOTS, protected_use_nodes=observed_uses, origin_diagnostics=diagnostics
    )
    assert original == observed == []
    assert original_uses == observed_uses and original_uses
    assert diagnostics.report(observed)["failure_count"] == 0
    # A tiny input cannot satisfy the complete boundary. Observation preserves
    # every receipt member and its empty-on-failure behavior instead of making
    # this intentionally refused cohort an alternate positive certificate.
    refused_units = (fencing._parse_source("src/elspeth/origin_diagnostic_empty.py", "value = 1\n"),)
    absent = fencing._reserve_supported_boundary(refused_units)
    sink = fencing._reserve_OriginDiagnostics(refused_units)
    present = fencing._reserve_supported_boundary(refused_units, origin_diagnostics=sink)
    assert absent.failures == present.failures and absent.failures
    assert absent.__dict__ == present.__dict__
    assert present.disjoint_compose_state_writes == frozenset()
    assert sink.report(present.failures)["unobserved_failures"] == len(present.failures)


def test_origin_diagnostics_link_actual_retained_keyword_return_and_duplicate_nodes():
    (supplier,) = _source_units(("src/elspeth/contracts/prompt_guard.py",))
    path = "src/elspeth/origin_diagnostic_retained.py"
    unit = fencing._parse_source(
        path,
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\n"
        "def held():\n    return PromptGuardTrust.REFERENCE\n"
        "opaque(value=PromptGuardTrust.REFERENCE.__dict__)\n"
        "opaque(PromptGuardTrust.REFERENCE); opaque(PromptGuardTrust.REFERENCE)\n"
        "holder.item = PromptGuardTrust.REFERENCE\n"
        "def captured(value=PromptGuardTrust.REFERENCE):\n    return value\n",
    )
    units = (
        supplier,
        unit,
        *_source_units(
            (
                "src/elspeth/contracts/plugin_capabilities.py",
                "src/elspeth/web/async_workers.py",
                "src/elspeth/web/app.py",
                "src/elspeth/web/application_finalizers.py",
                "src/elspeth/web/process_recovery.py",
                "src/elspeth/web/coordination/repository.py",
            )
        ),
    )
    roots = frozenset({"elspeth.contracts.prompt_guard.PromptGuardTrust"})
    assert fencing._reserve_prompt_part_enum_record_receipt(units)["failures"] == []
    assert fencing._reserve_capability_primitive_dependencies(units)["failures"] == []
    missing = tuple(source for source in units if source.path != "src/elspeth/contracts/plugin_capabilities.py")
    missing_uses, missing_observed_uses = [], []
    missing_original = fencing._reserve_selected_origin_effect_failures(
        missing, additional_supplier_roots=roots, protected_use_nodes=missing_uses
    )
    missing_diagnostics = fencing._reserve_OriginDiagnostics(missing)
    missing_observed = fencing._reserve_selected_origin_effect_failures(
        missing,
        additional_supplier_roots=roots,
        protected_use_nodes=missing_observed_uses,
        origin_diagnostics=missing_diagnostics,
    )
    assert missing_observed == missing_original
    assert missing_observed_uses == missing_uses == []
    assert (
        "retained enum carrier constructor/field contract changed elspeth.contracts.plugin_capabilities.CapabilityDeclaration"
        in missing_observed
    )
    assert missing_diagnostics.report(missing_observed)["events"] == []
    # The same original companion restores the real record-domain premise;
    # all independent refusals remain in the complete owner comparison.
    original = fencing._reserve_selected_origin_effect_failures(units, additional_supplier_roots=roots)
    diagnostics = fencing._reserve_OriginDiagnostics(units)
    observed = fencing._reserve_selected_origin_effect_failures(units, additional_supplier_roots=roots, origin_diagnostics=diagnostics)
    assert observed == original
    report = diagnostics.report(observed)
    events = [event for event in report["events"] if event["node"] is not None and report["nodes"][event["node"]]["path"] == path]
    returned = [event for event in events if event["family"] == "retained enum producer return is unclosed"]
    metadata = [event for event in events if event["family"] == "retained supplier metadata transferred"]
    assert returned and metadata
    assert any(read["role"]["semantic"] == "returned-value" for event in returned for read in event["witness"]["reads"])
    assert any(read["role"]["semantic"] == "keyword:value" for event in metadata for read in event["witness"]["reads"])
    stored = [event for event in events if event["family"] == "retained supplier stored in unclosed carrier"]
    captured = [event for event in events if event["family"] == "retained enum callable capture is unclosed"]
    assert stored and captured
    assert any(read["role"]["semantic"] == "storage-value" for event in stored for read in event["witness"]["reads"])
    assert any(state["capture"] for event in captured for state in event["retention_summaries"])
    repeated = [event for event in events if event["family"] == "retained enum value passed to unclosed caller"]
    same_line = [event for event in repeated if report["nodes"][event["node"]]["span"][0] == 5]
    assert len(same_line) == 2 and same_line[0]["ordinal"] != same_line[1]["ordinal"]
    assert same_line[0]["node"] != same_line[1]["node"]
    assert all(event["witness"]["completed"] is True for event in (*returned, *metadata, *same_line))
    parents = {id(child): parent for parent in ast.walk(unit.tree) for child in ast.iter_child_nodes(parent)}
    call = next(node for node in ast.walk(unit.tree) if isinstance(node, ast.Call))
    detached = fencing._parse_source(path, unit.source)
    detached_call = next(node for node in ast.walk(detached.tree) if isinstance(node, ast.Call))
    assert diagnostics.locator(unit, call, parents) is not None
    assert diagnostics.locator(unit, detached_call, parents) is None


@pytest.mark.parametrize("limited", (False, True))
def test_origin_diagnostic_consumer_preserves_actual_coupling_unread_and_repeated_reads(limited):
    unit = fencing._parse_source(
        "src/elspeth/origin_diagnostic_choices.py", "consume(left, chosen=right, unused='PRIVATE_DIAGNOSTIC_LITERAL')\n"
    )
    node = unit.tree.body[0].value
    left, right, unread = node.args[0], node.keywords[0].value, node.keywords[1].value
    parents = {id(child): parent for parent in ast.walk(unit.tree) for child in ast.iter_child_nodes(parent)}
    enum = ("enum-value", "elspeth.origin_diagnostic_choices.Marker")

    def execute(sink):
        queried, charges = [], []

        def expressions(value, use, *, read_target=False):
            queried.append((value, use, read_target))
            assert value is not unread
            return frozenset({None, enum})

        def consumer(selected):
            first = selected(left, node)
            if first is None:
                return None
            assert selected(left, node) == first
            second = selected(right, node)
            return "controlled coupled refusal " if second == first else None

        if sink is not None:
            sink.begin(unit, node, parents, 0)
            consumer = sink.observe_consumer(consumer)
        reasons = fencing._reserve_retention.consumer_combinations(consumer, expressions, lambda: charges.append(None))
        if sink is not None:
            sink.end([reason + unit.path + ":1" for reason in reasons])
        return reasons, queried, charges

    expected = execute(None)
    diagnostics = fencing._reserve_OriginDiagnostics((unit,), max_reads=1 if limited else 48)
    actual = execute(diagnostics)
    assert actual == expected
    failures = [reason + unit.path + ":1" for reason in actual[0]]
    report = diagnostics.report(failures)
    event = report["events"][0]
    assert event["witness"]["completed"] is True
    assert event["witness"]["representative"] is True
    assert event["witness"]["read_count"] == 3
    assert all(read["origin"]["identity"] == enum[1] for read in event["witness"]["reads"])
    assert report["truncated"]["reads"] is limited
    assert "PRIVATE_DIAGNOSTIC_LITERAL" not in fencing._reserve_json.dumps(report)


def test_origin_diagnostic_consumer_propagates_budget_and_partial_attempts_unchanged():
    unit = fencing._parse_source("src/elspeth/origin_diagnostic_budget.py", "consume(value)\n")
    call = unit.tree.body[0].value
    parents = {id(child): parent for parent in ast.walk(unit.tree) for child in ast.iter_child_nodes(parent)}
    outcomes = []
    for enabled in (False, True):
        sink = fencing._reserve_OriginDiagnostics((unit,)) if enabled else None
        charges = []

        def charge(*, charges=charges):
            charges.append(None)
            if len(charges) > 1:
                raise fencing._RetentionConsumerBudgetExceeded

        def consumer(selected):
            selected(call.args[0], call)
            return "must not complete before branch choice "

        if sink is not None:
            sink.begin(unit, call, parents, 0)
            consumer = sink.observe_consumer(consumer)
        with pytest.raises(fencing._RetentionConsumerBudgetExceeded):
            fencing._reserve_retention.consumer_combinations(
                consumer, lambda value, use, *, read_target=False: frozenset({None, ("enum-value", "elspeth.diag.Marker")}), charge
            )
        outcomes.append(charges)
        if sink is not None:
            sink.end([])
            assert sink.report([])["events"] == []
    assert outcomes[0] == outcomes[1]


def test_origin_diagnostic_caps_do_not_cap_actual_owner_failures_or_change_budget_refusals(monkeypatch):
    (control,) = _source_units((fencing._RETENTION_BUDGET_CONTROL,))
    monkeypatch.setattr(fencing, "_RETENTION_ROOT_EVALUATION_BUDGET", 1)
    units = (
        control,
        *_source_units(
            (
                "src/elspeth/web/async_workers.py",
                "src/elspeth/web/app.py",
                "src/elspeth/web/application_finalizers.py",
                "src/elspeth/web/process_recovery.py",
                "src/elspeth/web/coordination/repository.py",
            )
        ),
    )
    absent = fencing._reserve_selected_origin_effect_failures(units)
    diagnostics = fencing._reserve_OriginDiagnostics(units, max_events=0, max_nodes=0)
    present = fencing._reserve_selected_origin_effect_failures(units, origin_diagnostics=diagnostics)
    assert absent == present
    assert any(message.startswith("retention budget exceeded at " + control.path) for message in present)
    report = diagnostics.report(present)
    assert report["failure_count"] == len(present) and report["events"] == []
    assert report["truncated"]["events"] and not report["complete"]


@pytest.mark.parametrize(
    ("prefix_kind", "node_budget"),
    (
        pytest.param("none", 32, id="base-fits"),
        pytest.param("ordinary", 32, id="harmless-prefix"),
        pytest.param("protected", 32, id="protected-use-prefix"),
        pytest.param("none", 1, id="own-witness-exceeds-budget"),
    ),
)
def test_origin_diagnostic_locator_budget_prioritizes_actual_late_refusal(prefix_kind, node_budget):
    path = "src/elspeth/origin_diagnostic_locator_probe.py"
    origin = "operator.attrgetter"
    declaration = "from __future__ import annotations\nfrom operator import attrgetter\nprivate = 'PRIVATE_LOCATOR_CONTROL_LITERAL'\n"
    prefix = (
        "".join(f"harmless_{index} = 0\nharmless_{index}\n" for index in range(48))
        if prefix_kind == "ordinary"
        else "".join(f"annotation_{index}: attrgetter\n" for index in range(48))
        if prefix_kind == "protected"
        else ""
    )
    roots = frozenset({origin})
    companions = _source_units(
        (
            "src/elspeth/web/async_workers.py",
            "src/elspeth/web/app.py",
            "src/elspeth/web/application_finalizers.py",
            "src/elspeth/web/process_recovery.py",
            "src/elspeth/web/coordination/repository.py",
        )
    )

    def observe(unit):
        # The real owner always requires the complete recovery registration
        # and normal SQL-authority declarations. Read their actual sources;
        # keep the controlled probe first in the unchanged traversal order.
        units = (unit, *companions)
        original_uses, observed_uses = [], []
        absent = fencing._reserve_selected_origin_effect_failures(units, extra_roots=roots, protected_use_nodes=original_uses)
        diagnostics = fencing._reserve_OriginDiagnostics(units, max_nodes=node_budget, max_events=8, max_uses=128)
        present = fencing._reserve_selected_origin_effect_failures(
            units, extra_roots=roots, protected_use_nodes=observed_uses, origin_diagnostics=diagnostics
        )
        assert present == absent
        # Compare the real owner's unchanged records, including original IDs;
        # the optional diagnostic view is not an admission receipt.
        assert observed_uses == original_uses
        return present, original_uses, diagnostics

    # Prove through the actual owner that the added source is non-failing,
    # including its real postponed-annotation protected-use records. Complete
    # companion failures remain part of the same empty premise.
    prefix_unit = fencing._parse_source(path, declaration + prefix)
    prefix_failures, prefix_uses, _prefix_diagnostics = observe(prefix_unit)
    assert prefix_failures == []
    if prefix_kind == "protected":
        annotations = [statement.annotation for statement in prefix_unit.tree.body if isinstance(statement, ast.AnnAssign)]
        assert annotations
        for annotation in annotations:
            assert any(record[0] == path and record[1] == id(annotation) and record[6] == "postponed_annotation" for record in prefix_uses)

    unit = fencing._parse_source(path, declaration + prefix + "opaque(value=attrgetter('__annotations__'))\n")
    statement = unit.tree.body[-1]
    assert isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
    late = statement.value
    assert isinstance(late.func, ast.Name) and late.func.id == "opaque" and not late.args
    (keyword,) = late.keywords
    assert keyword.arg == "value"
    argument = keyword.value
    assert isinstance(argument, ast.Call) and isinstance(argument.func, ast.Name) and argument.func.id == "attrgetter"
    assert len(argument.args) == 1 and not argument.keywords
    assert isinstance(argument.args[0], ast.Constant) and argument.args[0].value == "__annotations__"
    failures, _uses, diagnostics = observe(unit)
    reason = "retained supplier metadata transferred " + path + ":" + str(late.lineno)
    assert reason in failures
    report = diagnostics.report(failures)
    (event,) = [event for event in report["events"] if event["ordinal"] == failures.index(reason) + 1]
    assert event["family"] == "retained supplier metadata transferred"
    assert event["witness"]["completed"] is True and event["witness"]["representative"] is True
    keyword_reads = [read for read in event["witness"]["reads"] if read["role"]["semantic"] == "keyword:value"]
    assert keyword_reads
    metadata_origin = {"status": "identity-omitted-not-qualified", "kind": "metadata-query", "identity": None}
    assert all(read["origin"] == metadata_origin for read in keyword_reads)
    assert "PRIVATE_LOCATOR_CONTROL_LITERAL" not in fencing._reserve_json.dumps(report)

    if node_budget == 1:
        assert report["truncated"]["nodes"] and not report["complete"]
        assert event["node"] is None or any(read["expression"] is None or read["use"] is None for read in keyword_reads)
        return

    # The fixed32-node budget fits the original case. Earlier non-failing
    # work and the supplemental protected-use view must not spend it first.
    assert event["node"] is not None
    parents = {id(child): parent for parent in ast.walk(unit.tree) for child in ast.iter_child_nodes(parent)}
    assert diagnostics.locator(unit, late, parents) == event["node"]
    root = report["nodes"][event["node"]]
    assert root["path"] == path and root["kind"] == "Call"
    assert root["span"] == [late.lineno, late.col_offset, late.end_lineno, late.end_col_offset]
    assert root["parent"]["kind"] == "Expr" and root["parent"]["edge"] == {"field": "value", "index": None}
    for read in keyword_reads:
        assert read["expression"] is not None and read["use"] == event["node"]
        assert diagnostics.locator(unit, argument, parents) == read["expression"]
        operand = report["nodes"][read["expression"]]
        assert operand["kind"] == "Call"
        assert operand["span"] == [argument.lineno, argument.col_offset, argument.end_lineno, argument.end_col_offset]
        assert operand["parent"]["kind"] == "keyword" and operand["parent"]["edge"] == {"field": "value", "index": None}
    assert any(operand["expression"] == keyword_reads[0]["expression"] and operand["use"] == event["node"] for operand in event["operands"])
    assert any(
        state["expression"] == keyword_reads[0]["expression"]
        and state["use"] == event["node"]
        and state["direct_origin"] == metadata_origin
        for state in event["retention_summaries"]
    )


def test_origin_failure_line_preserves_original_locations_and_refuses_detached_parent_chains():
    unit = fencing._parse_source("src/elspeth/origin_budget_location.py", "def held(source):\n    return [value for value in source]\n")
    parents = {id(child): parent for parent in ast.walk(unit.tree) for child in ast.iter_child_nodes(parent)}
    (comprehension,) = [node for node in ast.walk(unit.tree) if isinstance(node, ast.comprehension)]
    parent = parents[id(comprehension)]
    assert isinstance(parent, ast.ListComp) and "lineno" not in comprehension._attributes
    original = ast.dump(unit.tree, include_attributes=True)
    for node in ast.walk(unit.tree):
        if "lineno" in node._attributes:
            assert fencing._reserve_origin_failure_line(unit, node, parents) == node.lineno
    assert fencing._reserve_origin_failure_line(unit, comprehension, parents) == parent.lineno
    assert fencing._reserve_origin_failure_line(unit, unit.tree, parents) == "<unavailable>"
    detached = fencing._parse_source(unit.path, unit.source)
    detached_parents = {id(child): parent for parent in ast.walk(detached.tree) for child in ast.iter_child_nodes(parent)}
    (detached_comprehension,) = [node for node in ast.walk(detached.tree) if isinstance(node, ast.comprehension)]
    assert fencing._reserve_origin_failure_line(unit, detached_comprehension, parents) == "<unavailable>"
    assert fencing._reserve_origin_failure_line(unit, detached_comprehension, detached_parents) == "<unavailable>"
    assert fencing._reserve_origin_failure_line(unit, comprehension, {id(comprehension): comprehension}) == "<unavailable>"
    assert ast.dump(unit.tree, include_attributes=True) == original


@pytest.mark.parametrize("expression", ("[value for value in source]", "(value for value in source)"))
def test_origin_diagnostics_preserve_actual_locationless_comprehension_budget_refusal(monkeypatch, expression):
    path = "src/elspeth/origin_locationless_budget_probe.py"
    unit = fencing._parse_source(path, "def held(source):\n    return " + expression + "\n")
    parents = {id(child): parent for parent in ast.walk(unit.tree) for child in ast.iter_child_nodes(parent)}
    (comprehension,) = [node for node in ast.walk(unit.tree) if isinstance(node, ast.comprehension)]
    parent = parents[id(comprehension)]
    assert isinstance(parent, (ast.ListComp, ast.GeneratorExp)) and "lineno" not in comprehension._attributes
    original = ast.dump(unit.tree, include_attributes=True)
    units = (
        unit,
        *_source_units(
            (
                "src/elspeth/web/async_workers.py",
                "src/elspeth/web/app.py",
                "src/elspeth/web/application_finalizers.py",
                "src/elspeth/web/process_recovery.py",
                "src/elspeth/web/coordination/repository.py",
            )
        ),
    )
    monkeypatch.setattr(fencing, "_RETENTION_ROOT_EVALUATION_BUDGET", 1)
    original_uses, observed_uses = [], []
    absent = fencing._reserve_selected_origin_effect_failures(units, protected_use_nodes=original_uses)
    diagnostics = fencing._reserve_OriginDiagnostics(units)
    present = fencing._reserve_selected_origin_effect_failures(units, protected_use_nodes=observed_uses, origin_diagnostics=diagnostics)
    assert present == absent and present
    assert observed_uses == original_uses
    report = diagnostics.report(present)
    token = diagnostics.locator(unit, comprehension, parents)
    assert token is not None
    events = [event for event in report["events"] if event["node"] == token]
    expected = (
        "retention budget exceeded at " + path + ":" + str(parent.lineno),
        "retained supplier analysis budget exceeded " + path + ":" + str(parent.lineno),
    )
    assert tuple(present[event["ordinal"] - 1] for event in events) == expected
    assert events[1]["ordinal"] == events[0]["ordinal"] + 1
    assert all(event["witness"] is None and event["provenance"] == "partial-observation-or-unavailable" for event in events)
    locator = report["nodes"][token]
    assert locator["path"] == path and locator["kind"] == "comprehension" and locator["span"] is None
    assert locator["parent"]["kind"] == type(parent).__name__
    assert locator["parent"]["span"] == [parent.lineno, parent.col_offset, parent.end_lineno, parent.end_col_offset]
    assert locator["parent"]["edge"] == {"field": "generators", "index": 0}
    assert locator["structural_lexical_owners"][0]["name"] == "held"
    detached = fencing._parse_source(path, unit.source)
    (detached_comprehension,) = [node for node in ast.walk(detached.tree) if isinstance(node, ast.comprehension)]
    assert diagnostics.locator(unit, detached_comprehension, parents) is None
    assert ast.dump(unit.tree, include_attributes=True) == original


def _eager_literal_observation(path, source, record_property, label):
    import json

    unit = fencing._parse_source(path, source)
    facts = fencing._reserve_typing_annotation_source_facts(unit)
    failures = fencing._reserve_typing_supplier_mutation_failures((unit,))
    record_property(label, json.dumps(failures))
    return unit, facts, failures


def _eager_literal_query(unit, target):
    statements = [
        statement
        for statement in unit.tree.body
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and statement.targets[0].id == target
        )
        or (isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.target.id == target)
    ]
    (statement,) = statements
    materialization = statement.value
    assert isinstance(materialization, ast.Call)
    assert isinstance(materialization.func, ast.Name) and materialization.func.id == "frozenset"
    assert len(materialization.args) == 1 and materialization.keywords == []
    query = materialization.args[0]
    assert isinstance(query, ast.Call) and len(query.args) == 1 and query.keywords == []
    return query


def test_eager_literal_actual_blob_materializations_retain_only_original_scalar_results(record_property):
    path = "src/elspeth/contracts/blobs.py"
    source = (ROOT / path).read_text()
    unit, facts, failures = _eager_literal_observation(path, source, record_property, "blob_typing_failures")
    restored, restored_facts, restored_failures = _eager_literal_observation(path, source, record_property, "restored_blob_typing_failures")
    expected = (
        ("ALLOWED_MIME_TYPES", "AllowedMimeType"),
        ("BINARY_DOCUMENT_MIME_TYPES", "BinaryDocumentMimeType"),
        ("STORAGE_MIME_TYPES", "StorageMimeType"),
        ("BLOB_STATUSES", "BlobStatus"),
        ("FINALIZE_BLOB_STATUSES", "FinalizeBlobStatus"),
        ("BLOB_CREATORS", "BlobCreator"),
        ("BLOB_RUN_LINK_DIRECTIONS", "BlobRunLinkDirection"),
    )
    queries = []
    for target, alias in expected:
        query = _eager_literal_query(unit, target)
        assert isinstance(query.func, ast.Name) and query.func.id == "get_args"
        assert isinstance(query.args[0], ast.Name) and query.args[0].id == alias
        queries.append(query)
    original_ids = {id(node) for node in ast.walk(unit.tree)}
    assert id(queries[0]) in facts["scalar_query_results"], failures
    assert facts["scalar_query_results"] == frozenset(id(query) for query in queries)
    assert facts["scalar_query_results"] <= original_ids
    assert failures == []
    restored_queries = [_eager_literal_query(restored, target) for target, _alias in expected]
    assert restored_facts["scalar_query_results"] == frozenset(id(query) for query in restored_queries)
    assert not restored_facts["scalar_query_results"] & original_ids
    assert restored_failures == []


@pytest.mark.parametrize("kind", ("def", "async def"))
@pytest.mark.parametrize("annotation", ("", ": foreign"))
def test_eager_literal_later_direct_body_has_positive_opposed_and_restored_query_identity(kind, annotation, record_property):
    path = "src/elspeth/eager_literal_temporal_probe.py"
    imports = "from typing import Literal, get_args\n"
    declaration = "Format = Literal['safe']\n"
    materialization = "values = frozenset(get_args(Format))\n"
    definition = kind + " later(instance" + annotation + "):\n    return instance.__dict__\n"
    sources = (
        imports + declaration + materialization,
        imports + declaration + materialization + definition,
        imports + definition + declaration + materialization,
        imports + declaration + materialization + definition,
    )
    observations = [
        _eager_literal_observation(path, source, record_property, label)
        for source, label in zip(sources, ("canonical", "later_body", "earlier_body", "restored_later_body"), strict=True)
    ]
    queries = [_eager_literal_query(unit, "values") for unit, _facts, _failures in observations]
    assert observations[0][1]["scalar_query_results"] == frozenset({id(queries[0])})
    assert observations[0][2] == []
    assert id(queries[1]) in observations[1][1]["scalar_query_results"], observations[1][2]
    assert observations[1][1]["scalar_query_results"] == frozenset({id(queries[1])})
    assert observations[1][2] == []
    assert observations[2][1]["scalar_query_results"] == frozenset()
    assert observations[2][2] == ["typing supplier namespace transfer is unclosed " + path + ":" + str(queries[2].lineno)]
    assert observations[3][1]["scalar_query_results"] == frozenset({id(queries[3])})
    assert observations[3][2] == []
    for query, (unit, facts, _failures) in zip(queries, observations, strict=True):
        assert facts["scalar_query_results"] <= {id(node) for node in ast.walk(unit.tree)}
        assert all(id(other) not in facts["scalar_query_results"] for other in queries if other is not query)


def test_eager_literal_query_local_fact_does_not_leak_into_whole_unit_facts_or_later_query(record_property):
    path = "src/elspeth/eager_literal_isolation_probe.py"
    source = (
        "from typing import Literal, get_args, TypedDict\n"
        "Before = Literal['before']\n"
        "before_values = frozenset(get_args(Before))\n"
        "def later(instance):\n    return instance.__dict__\n"
        "After = Literal['after']\n"
        "after_values = frozenset(get_args(After))\n"
        "Envelope = TypedDict('Envelope', {'kind': Before})\n"
    )
    unit, facts, failures = _eager_literal_observation(path, source, record_property, "isolated_query_typing_failures")
    before = _eager_literal_query(unit, "before_values")
    after = _eager_literal_query(unit, "after_values")
    envelope = unit.tree.body[-1].value
    assert isinstance(envelope, ast.Call) and isinstance(envelope.func, ast.Name) and envelope.func.id == "TypedDict"
    assert facts["literal_annotations"] == frozenset()
    assert facts["typed_dict_consumers"] == frozenset()
    assert facts["canonical_imports"] == {
        "typing.get_args": frozenset(),
        "typing.Literal": frozenset(),
        "typing.TypedDict": frozenset(),
    }
    assert id(before) in facts["scalar_query_results"], failures
    assert facts["scalar_query_results"] == frozenset({id(before)})
    assert id(after) not in facts["scalar_query_results"]
    assert failures == [
        "typing supplier namespace transfer is unclosed " + path + ":" + str(after.lineno),
        "typing supplier namespace transfer is unclosed " + path + ":" + str(envelope.lineno),
    ]


@pytest.mark.parametrize(
    ("before", "between", "after"),
    [
        pytest.param("namespace = instance.__dict__\n", "", "", id="eager-dictionary"),
        pytest.param("def earlier(instance):\n    return instance.__dict__\n", "", "", id="earlier-function"),
        pytest.param("", "", "def later(value=instance.__dict__):\n    return None\n", id="default"),
        pytest.param("", "", "@instance.__dict__\ndef later():\n    return None\n", id="decorator"),
        pytest.param("", "", "def later(value: instance.__dict__):\n    return None\n", id="annotation"),
        pytest.param("", "", "def later[T: instance.__dict__]():\n    return None\n", id="type-parameter"),
        pytest.param("", "", "if gate:\n    def later(instance):\n        return instance.__dict__\n", id="conditional-function"),
        pytest.param("", "", "class Later:\n    def method(self):\n        return self.__dict__\n", id="class-method"),
        pytest.param("", "", "class Later:\n    namespace = instance.__dict__\n", id="class-body"),
        pytest.param("def step():\n    return None\n", "step()\n", "", id="call-interval"),
        pytest.param("", "", "def later(instance):\n    return instance.__globals__\n", id="other-deferred-recovery"),
    ],
)
def test_eager_literal_temporal_exception_refuses_each_eager_or_unsupported_scope(before, between, after, record_property):
    path = "src/elspeth/eager_literal_scope_refusal_probe.py"
    imports = "from typing import Literal, get_args\n"
    declaration = "Format = Literal['safe']\n"
    materialization = "values = frozenset(get_args(Format))\n"
    source = imports + before + declaration + between + materialization + after
    unit, facts, failures = _eager_literal_observation(path, source, record_property, "scope_opposition_typing_failures")
    restored, restored_facts, restored_failures = _eager_literal_observation(
        path, imports + declaration + materialization, record_property, "scope_restoration_typing_failures"
    )
    query = _eager_literal_query(unit, "values")
    assert facts["scalar_query_results"] == frozenset()
    assert failures == ["typing supplier namespace transfer is unclosed " + path + ":" + str(query.lineno)]
    assert restored_facts["scalar_query_results"] == frozenset({id(_eager_literal_query(restored, "values"))})
    assert restored_failures == []


@pytest.mark.parametrize("binding", ("Format", "Literal", "get_args"))
@pytest.mark.parametrize("timing", ("before", "after"))
def test_eager_literal_temporal_fact_preserves_stronger_helper_and_alias_rebinding_exclusions(binding, timing, record_property):
    path = "src/elspeth/eager_literal_rebinding_probe.py"
    imports = "from typing import Literal, get_args\n"
    declaration = "Format = Literal['safe']\n"
    materialization = "values = frozenset(get_args(Format))\n"
    definition = "def later(instance):\n    return instance.__dict__\n"
    rebinding = binding + " = foreign\n"
    source = imports + declaration + (rebinding if timing == "before" else "") + materialization + definition
    if timing == "after":
        source += rebinding
    unit, facts, _failures = _eager_literal_observation(path, source, record_property, "binding_opposition_typing_failures")
    restored, restored_facts, restored_failures = _eager_literal_observation(
        path, imports + declaration + materialization + definition, record_property, "binding_restoration_typing_failures"
    )
    assert facts["scalar_query_results"] == frozenset()
    assert id(_eager_literal_query(unit, "values")) not in facts["scalar_query_results"]
    assert restored_facts["scalar_query_results"] == frozenset({id(_eager_literal_query(restored, "values"))}), restored_failures
    assert restored_failures == []


@pytest.mark.parametrize("effect", ("live-alias", "live-alias-mutation", "typing-dictionary-mutation"))
def test_eager_literal_result_never_authorizes_live_alias_transfer_or_later_typing_effect(effect, record_property):
    path = "src/elspeth/eager_literal_effect_probe.py"
    original = (
        "import typing\nfrom typing import Literal, get_args\n"
        "Format = Literal['safe']\nvalues = frozenset(get_args(Format))\n"
        "def later(instance):\n    return instance.__dict__\n"
    )
    if effect == "live-alias":
        source = original + "opaque(values)\nopaque(Format)\n"
    elif effect == "live-alias-mutation":
        source = original + "Format.__class__.__getitem__ = replacement\n"
    else:
        source = original + "def mutate():\n    typing.__dict__['_SpecialForm'] = replacement\n"
    unit, facts, failures = _eager_literal_observation(path, source, record_property, "independent_effect_typing_failures")
    restored, restored_facts, restored_failures = _eager_literal_observation(
        path, original, record_property, "independent_effect_restoration_typing_failures"
    )
    query = _eager_literal_query(unit, "values")
    assert id(query) in facts["scalar_query_results"], failures
    assert facts["scalar_query_results"] == frozenset({id(query)})
    if effect == "live-alias":
        call = unit.tree.body[-1].value
        assert isinstance(call, ast.Call) and isinstance(call.args[0], ast.Name) and call.args[0].id == "Format"
        assert id(call.args[0]) not in facts["scalar_query_results"]
        assert failures == ["typing supplier namespace transfer is unclosed " + path + ":" + str(call.lineno)]
    elif effect == "live-alias-mutation":
        mutation = unit.tree.body[-1]
        assert isinstance(mutation, ast.Assign) and isinstance(mutation.targets[0], ast.Attribute)
        assert failures == ["typing supplier namespace mutation " + path + ":" + str(mutation.lineno)]
    else:
        mutation = unit.tree.body[-1].body[0]
        assert isinstance(mutation, ast.Assign) and isinstance(mutation.targets[0], ast.Subscript)
        assert failures == ["typing supplier namespace mutation " + path + ":" + str(mutation.lineno)]
    assert restored_facts["scalar_query_results"] == frozenset({id(_eager_literal_query(restored, "values"))})
    assert id(query) not in restored_facts["scalar_query_results"]
    assert restored_failures == []


@pytest.mark.parametrize(
    ("imports", "declaration", "materialization"),
    [
        pytest.param(
            "from typing import Literal\nfrom foreign import get_args\n",
            "Format = Literal['safe']\n",
            "values = frozenset(get_args(Format))\n",
            id="foreign-query",
        ),
        pytest.param(
            "from foreign import Literal\nfrom typing import get_args\n",
            "Format = Literal['safe']\n",
            "values = frozenset(get_args(Format))\n",
            id="foreign-literal",
        ),
        pytest.param(
            "from typing import Literal, get_args, get_args\n",
            "Format = Literal['safe']\n",
            "values = frozenset(get_args(Format))\n",
            id="duplicate-query",
        ),
        pytest.param(
            "from typing import Literal, Literal, get_args\n",
            "Format = Literal['safe']\n",
            "values = frozenset(get_args(Format))\n",
            id="duplicate-literal",
        ),
        pytest.param(
            "from typing import Literal, get_args\nfrozenset = foreign\n",
            "Format = Literal['safe']\n",
            "values = frozenset(get_args(Format))\n",
            id="shadowed-materializer",
        ),
        pytest.param(
            "from typing import Literal, get_args\n", "Format = Literal[1]\n", "values = frozenset(get_args(Format))\n", id="nonstring"
        ),
        pytest.param(
            "from typing import Literal, get_args\n", "Format = Literal[()]\n", "values = frozenset(get_args(Format))\n", id="empty-literal"
        ),
        pytest.param(
            "import typing\nfrom typing import Literal, get_args\n",
            "Format = Literal[typing._SpecialForm]\n",
            "values = frozenset(get_args(Format))\n",
            id="retained-class",
        ),
        pytest.param(
            "from typing import Literal, get_args\n",
            "Format = Literal['safe']\n",
            "def read():\n    return frozenset(get_args(Format))\n",
            id="deferred-query",
        ),
        pytest.param(
            "from typing import Literal, get_args\n",
            "Format = Literal['safe']\n",
            "values = opaque(get_args(Format))\n",
            id="opaque-consumer",
        ),
        pytest.param(
            "from typing import Literal, get_origin\n",
            "Format = Literal['safe']\n",
            "values = frozenset(get_origin(Format))\n",
            id="different-query-api",
        ),
    ],
)
def test_eager_literal_new_temporal_role_keeps_each_canonical_supplier_and_result_exclusion(
    imports, declaration, materialization, record_property
):
    path = "src/elspeth/eager_literal_role_exclusion_probe.py"
    source = imports + declaration + materialization + "def later(instance):\n    return instance.__dict__\n"
    _unit, facts, _failures = _eager_literal_observation(path, source, record_property, "role_exclusion_typing_failures")
    assert facts["scalar_query_results"] == frozenset()


@pytest.mark.parametrize(
    "source",
    [
        "from typing import get_args\nFormat = Literal['safe']\nvalues = frozenset(get_args(Format))\nfrom typing import Literal\n",
        "from typing import Literal\nFormat = Literal['safe']\nvalues = frozenset(get_args(Format))\nfrom typing import get_args\n",
    ],
    ids=("literal-import-after-producer", "query-import-after-use"),
)
def test_eager_literal_canonical_import_must_dominate_its_actual_eager_use(source, record_property):
    path = "src/elspeth/eager_literal_import_order_probe.py"
    source += "def later(instance):\n    return instance.__dict__\n"
    _unit, facts, _failures = _eager_literal_observation(path, source, record_property, "late_import_typing_failures")
    assert facts["scalar_query_results"] == frozenset()


def test_eager_literal_temporal_result_does_not_admit_detached_query_alias_or_producer(record_property):
    path = "src/elspeth/eager_literal_original_identity_probe.py"
    source = (
        "from typing import Literal, get_args\nFormat = Literal['safe']\nvalues = frozenset(get_args(Format))\n"
        "def later(instance):\n    return instance.__dict__\n"
    )
    unit, facts, failures = _eager_literal_observation(path, source, record_property, "identity_typing_failures")
    other, other_facts, other_failures = _eager_literal_observation(path, source, record_property, "reparsed_identity_typing_failures")
    query = _eager_literal_query(unit, "values")
    other_query = _eager_literal_query(other, "values")
    detached_query = copy.deepcopy(query)
    detached_producer = copy.deepcopy(unit.tree.body[1].value)
    assert ast.dump(detached_query, include_attributes=True) == ast.dump(query, include_attributes=True)
    assert facts["scalar_query_results"] == frozenset({id(query)}), failures
    assert other_facts["scalar_query_results"] == frozenset({id(other_query)}), other_failures
    assert facts["scalar_query_results"] <= {id(node) for node in ast.walk(unit.tree)}
    assert not facts["scalar_query_results"] & {id(node) for node in ast.walk(other.tree)}
    assert all(
        id(node) not in facts["scalar_query_results"]
        for node in (detached_query, detached_producer, unit.tree.body[1].value, query.args[0], query.func)
    )
    assert failures == other_failures == []


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        ("local = source", {"local"}),
        ("left, (right, *rest) = source", {"left", "right", "rest"}),
        ("[*head, tail] = source", {"head", "tail"}),
        ("typing._SpecialForm = replacement", set()),
        ("typing.__dict__['_SpecialForm'] = replacement", set()),
        ("targets[index] = replacement", set()),
        ("targets[(index := 0)] = replacement", set()),
        ("local, typing._SpecialForm = source", {"local"}),
    ],
)
def test_resolver_target_names_distinguish_original_store_binders_from_load_bases(statement, expected):
    unit = fencing._parse_source("src/elspeth/typing_target_binding_probe.py", statement + "\n")
    assignment = unit.tree.body[0]
    assert isinstance(assignment, ast.Assign) and len(assignment.targets) == 1
    target = assignment.targets[0]
    assert any(node is target for node in ast.walk(unit.tree))
    assert fencing._Resolver._target_names(target) == expected


def test_typing_global_dictionary_mutation_refusal_has_no_scalar_query_prerequisite(record_property):
    import json

    path = "src/elspeth/typing_global_dictionary_mutation_probe.py"
    original = "import typing\ndef mutate():\n    pass\n"
    mutation_source = "import typing\ndef mutate():\n    typing.__dict__['_SpecialForm'] = replacement\n"
    opposed = "import typing\ndef mutate(typing):\n    typing.__dict__['_SpecialForm'] = replacement\n"
    sources = (original, mutation_source, opposed, mutation_source)
    units = [fencing._parse_source(path, source) for source in sources]
    failures = [fencing._reserve_typing_supplier_mutation_failures((unit,)) for unit in units]
    for label, rows in zip(("ordinary", "global_mutation", "parameter_receiver", "restored_global_mutation"), failures, strict=True):
        record_property(label, json.dumps(rows))
    mutation = units[1].tree.body[1].body[0]
    restored = units[3].tree.body[1].body[0]
    assert isinstance(mutation, ast.Assign) and isinstance(mutation.targets[0], ast.Subscript)
    assert isinstance(mutation.targets[0].value, ast.Attribute) and mutation.targets[0].value.attr == "__dict__"
    receiver = mutation.targets[0].value.value
    assert isinstance(receiver, ast.Name) and receiver.id == "typing" and isinstance(receiver.ctx, ast.Load)
    assert failures[0] == []
    assert failures[1] == ["typing supplier namespace mutation " + path + ":" + str(mutation.lineno)]
    assert failures[2] == []
    assert failures[3] == ["typing supplier namespace mutation " + path + ":" + str(restored.lineno)]
    resolver = fencing._resolver_for_unit(units[1])
    assert not resolver.is_local("typing", mutation)
    assert resolver.qualified_name(receiver, use=mutation) == "typing"
    assert fencing._resolver_for_unit(units[2]).is_local("typing", units[2].tree.body[1].body[0])
    assert mutation is not restored
    assert mutation.targets[0] is not restored.targets[0]
    assert receiver is not restored.targets[0].value.value


def _resolver_target_caller_unit(kind, statement, returned):
    source = (
        "import typing\nindex = 0\ntargets = {}\n"
        + kind
        + " probe(source, manager, values, foreign):\n"
        + "\n".join("    " + line for line in statement.splitlines())
        + "\n    return "
        + returned
        + "\n"
    )
    unit = fencing._parse_source("src/elspeth/resolver_target_caller_probe.py", source)
    function = unit.tree.body[-1]
    assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
    use = function.body[-1].value
    assert isinstance(function.body[-1], ast.Return)
    return unit, function, use


@pytest.mark.parametrize(
    ("kind", "statement"),
    [
        ("def", "{target}: object = source"),
        ("def", "{target} += source"),
        ("def", "with manager as {target}:\n    pass"),
        ("async def", "async with manager as {target}:\n    pass"),
        ("def", "for {target} in values:\n    pass"),
        ("async def", "async for {target} in values:\n    pass"),
    ],
    ids=("initialized-annotation", "augmentation", "with", "async-with", "for", "async-for"),
)
def test_resolver_initialized_target_callers_preserve_real_binders_and_global_load_bases(kind, statement):
    observations = []
    for target_text, returned, binds in (
        ("bound", "bound, typing, index", True),
        ("typing._SpecialForm", "typing, index", False),
        ("targets[index]", "typing, index", False),
        ("bound", "bound, typing, index", True),
    ):
        unit, function, use = _resolver_target_caller_unit(kind, statement.format(target=target_text), returned)
        resolver = fencing._resolver_for_unit(unit)
        owner = function.body[0]
        if isinstance(owner, (ast.AnnAssign, ast.AugAssign, ast.For, ast.AsyncFor)):
            target = owner.target
        else:
            assert isinstance(owner, (ast.With, ast.AsyncWith))
            target = owner.items[0].optional_vars
        assert target is not None and any(node is target for node in ast.walk(unit.tree))
        assert fencing._Resolver._target_names(target) == ({"bound"} if binds else set())
        assert resolver.is_local("bound", use) is binds
        assert resolver.is_local("source", use)
        assert not resolver.is_local("typing", use)
        assert not resolver.is_local("index", use)
        reads = [node for node in ast.walk(use) if isinstance(node, ast.Name) and node.id == "typing"]
        (read,) = reads
        assert resolver.qualified_name(read, use=use) == "typing"
        observations.append((function, target))
    assert observations[0][0] is not observations[-1][0]
    assert observations[0][1] is not observations[-1][1]


@pytest.mark.parametrize("kind", ("for", "async-for", "comprehension"))
@pytest.mark.parametrize(
    ("target_text", "bound_names"),
    [("bound", ("bound",)), ("(bound, *rest)", ("bound", "rest")), ("typing._SpecialForm", ()), ("targets[index]", ())],
)
def test_resolver_iteration_receipts_use_only_original_target_binders(kind, target_text, bound_names):
    arguments = (*bound_names, "typing", "targets", "index")
    if kind == "comprehension":
        expression = "(" + ", ".join(arguments) + ")"
        unit, function, _use = _resolver_target_caller_unit("def", "pass", "[" + expression + " for " + target_text + " in values]")
        comprehension = function.body[-1].value
        assert isinstance(comprehension, ast.ListComp) and len(comprehension.generators) == 1
        target = comprehension.generators[0].target
        iterable = comprehension.generators[0].iter
        reads_owner = comprehension.elt
    else:
        keyword = "async for" if kind == "async-for" else "for"
        declaration = "async def" if kind == "async-for" else "def"
        statement = keyword + " " + target_text + " in values:\n    consume(" + ", ".join(arguments) + ")"
        unit, function, _use = _resolver_target_caller_unit(declaration, statement, "None")
        loop = function.body[0]
        assert isinstance(loop, (ast.For, ast.AsyncFor))
        target, iterable = loop.target, loop.iter
        reads_owner = loop.body[0].value
    resolver = fencing._resolver_for_unit(unit)
    assert any(node is target for node in ast.walk(unit.tree))
    assert any(node is iterable for node in ast.walk(unit.tree))
    for name in arguments:
        (read,) = [node for node in ast.walk(reads_owner) if isinstance(node, ast.Name) and node.id == name]
        receipt = resolver.iteration_source(name, read)
        if name in bound_names:
            assert receipt is not None
            assert receipt[0] is target and receipt[1] is iterable
        else:
            assert receipt is None


@pytest.mark.parametrize("kind", ("for", "async-for", "comprehension"))
def test_resolver_iteration_receipt_never_claims_the_iterable_before_target_binding(kind):
    if kind == "comprehension":
        source = "def probe(bound):\n    return [bound for bound in bound]\n"
        unit = fencing._parse_source("src/elspeth/resolver_iterable_order_probe.py", source)
        expression = unit.tree.body[0].body[0].value
        assert isinstance(expression, ast.ListComp)
        target, iterable = expression.generators[0].target, expression.generators[0].iter
        body_read = expression.elt
    else:
        keyword = "async for" if kind == "async-for" else "for"
        declaration = "async def" if kind == "async-for" else "def"
        source = declaration + " probe(bound):\n    " + keyword + " bound in bound:\n        consume(bound)\n"
        unit = fencing._parse_source("src/elspeth/resolver_iterable_order_probe.py", source)
        loop = unit.tree.body[0].body[0]
        assert isinstance(loop, (ast.For, ast.AsyncFor))
        target, iterable = loop.target, loop.iter
        body_read = loop.body[0].value.args[0]
    assert isinstance(iterable, ast.Name) and iterable.id == "bound" and isinstance(iterable.ctx, ast.Load)
    resolver = fencing._resolver_for_unit(unit)
    assert resolver.iteration_source("bound", iterable) is None
    receipt = resolver.iteration_source("bound", body_read)
    assert receipt is not None
    assert receipt[0] is target and receipt[1] is iterable


def test_resolver_target_evaluation_walrus_stays_local_without_an_outer_iteration_receipt():
    source = "def probe(targets, values):\n    for targets[(index := 0)] in values:\n        consume(index)\n"
    unit = fencing._parse_source("src/elspeth/resolver_target_walrus_probe.py", source)
    function = unit.tree.body[0]
    loop = function.body[0]
    assert isinstance(loop, ast.For) and isinstance(loop.target, ast.Subscript)
    walrus = loop.target.slice
    assert isinstance(walrus, ast.NamedExpr) and isinstance(walrus.target, ast.Name) and walrus.target.id == "index"
    use = loop.body[0].value.args[0]
    resolver = fencing._resolver_for_unit(unit)
    assert resolver.is_local("index", use)
    assert fencing._Resolver._target_names(walrus.target) == {"index"}
    assert fencing._Resolver._target_names(loop.target) == set()
    assert resolver.iteration_source("index", use) is None
    assert resolver.iteration_source("targets", use) is None


@pytest.mark.parametrize("binding", ("parameter", "assignment"))
def test_resolver_genuine_local_name_and_parameter_still_shadow_canonical_module_import(binding):
    if binding == "parameter":
        source = "import typing\ndef probe(typing):\n    return typing\n"
    else:
        source = "import typing\ndef probe(foreign):\n    typing = foreign\n    return typing\n"
    unit = fencing._parse_source("src/elspeth/resolver_real_shadow_probe.py", source)
    function = unit.tree.body[-1]
    use = function.body[-1].value
    assert isinstance(use, ast.Name) and use.id == "typing"
    resolver = fencing._resolver_for_unit(unit)
    assert resolver.is_local("typing", use)
    assert resolver.qualified_name(use, use=use) is None
    if binding == "assignment":
        assert resolver.binding("typing", use) is function.body[0].value
    else:
        assert resolver.parameter("typing", use) is function.args.args[0]


@pytest.mark.parametrize("source", ("typing\n", "del typing\n"))
def test_resolver_target_grammar_rejects_original_nonstore_names_and_missing_context(source):
    unit = fencing._parse_source("src/elspeth/resolver_nonstore_target_probe.py", source)
    statement = unit.tree.body[0]
    name = statement.value if isinstance(statement, ast.Expr) else statement.targets[0]
    assert isinstance(name, ast.Name) and not isinstance(name.ctx, ast.Store)
    original = ast.dump(name, include_attributes=True)
    assert fencing._Resolver._target_names(name) == set()
    malformed = ast.Name(id="typing")
    assert fencing._Resolver._target_names(malformed) == set()
    assert ast.dump(name, include_attributes=True) == original


_CHECKPOINT_ROLE_PATH = "src/elspeth/web/composer/continuation_p4_outcome.py"
_CHECKPOINT_ROLE_HELPER_PATH = "src/elspeth/web/composer/continuation_tool_batch.py"
_CHECKPOINT_ROLE_MODULE = "elspeth.web.composer.continuation_p4_outcome"
_CHECKPOINT_ROLE_HELPER_MODULE = "elspeth.web.composer.continuation_tool_batch"


def _checkpoint_role_nodes(units):
    (unit,) = [unit for unit in units if unit.path == _CHECKPOINT_ROLE_PATH]
    declaration = fencing._reserve_node_at(unit, "_CHECKPOINT")
    schema = fencing._reserve_node_at(unit, "HistoricalCheckpointData")
    owner = fencing._reserve_node_at(unit, "historical_checkpoint_record")
    assert isinstance(schema, ast.ClassDef) and isinstance(owner, ast.FunctionDef)
    assert isinstance(declaration, ast.Assign) and len(declaration.targets) == 1
    assert ast.unparse(declaration) == "_CHECKPOINT = TypeAdapter(HistoricalCheckpointData)"
    constructor = declaration.value
    assert isinstance(constructor, ast.Call) and isinstance(constructor.func, ast.Name)
    calls = [
        node
        for node in ast.walk(owner)
        if isinstance(node, ast.Call) and ast.unparse(node) == "_decode_owned(_CHECKPOINT, _dump_owned(_CHECKPOINT, value))"
    ]
    assert len(calls) == 1
    decode = calls[0]
    dump = decode.args[1]
    assert isinstance(dump, ast.Call) and len(decode.args) == len(dump.args) == 2
    assert not decode.keywords and not dump.keywords
    assert isinstance(decode.func, ast.Name) and isinstance(dump.func, ast.Name)
    assert isinstance(decode.args[0], ast.Name) and isinstance(dump.args[0], ast.Name)
    assert isinstance(dump.args[1], ast.Name) and dump.args[1].id == "value"
    assert isinstance(owner.body[1], ast.If)
    assert ast.unparse(owner.body[1].test) == "type(value) is not HistoricalCheckpointData"
    return unit, schema, owner, constructor, decode, dump


def _checkpoint_role_local_result(units, record_property, label):
    failures, receipt = fencing._reserve_batch_data_callback_consumers(units)
    record_property("checkpoint_role_" + label + "_local_failures", fencing._reserve_json.dumps(failures))
    return failures, receipt


def _checkpoint_role_require_membership(units, receipt):
    unit, _schema, _owner, constructor, decode, dump = _checkpoint_role_nodes(units)
    actual = {(source.path, id(node)) for source in units for node in ast.walk(source.tree)}
    expected_calls = {(unit.path, id(node)) for node in (constructor, decode, dump)}
    expected_uses = {
        (unit.path, id(node)) for node in (constructor.func, constructor.args[0], decode.func, decode.args[0], dump.func, dump.args[0])
    }
    expected_arguments = {(unit.path, id(call), id(call.args[0])) for call in (decode, dump)}
    assert expected_calls <= actual and expected_uses <= actual
    assert expected_calls <= receipt["calls"]
    assert expected_uses <= receipt["uses"]
    assert expected_arguments <= receipt["argument_calls"]
    assert {(unit.path, id(node)) for node in (decode.func, decode.args[0], dump.func, dump.args[0])} <= receipt["observed_uses"]
    assert _CHECKPOINT_ROLE_MODULE + "._CHECKPOINT" in receipt["adapter_origins"]
    assert _CHECKPOINT_ROLE_MODULE + ".HistoricalCheckpointData" in receipt["origins"]
    return expected_calls, expected_uses


def _checkpoint_role_replace(units, path, before, after):
    (original,) = [unit for unit in units if unit.path == path]
    assert original.source.count(before) == 1
    changed = fencing._parse_source(path, original.source.replace(before, after, 1))
    assert ast.dump(changed.tree, include_attributes=True) == ast.dump(ast.parse(changed.source), include_attributes=True)
    return tuple(changed if unit is original else unit for unit in units)


def _checkpoint_role_restored(units):
    # Fresh original ASTs in the two relevant owners; no old ID is reminted.
    return tuple(
        fencing._parse_source(unit.path, unit.source) if unit.path in {_CHECKPOINT_ROLE_PATH, _CHECKPOINT_ROLE_HELPER_PATH} else unit
        for unit in units
    )


def test_checkpoint_forwarding_local_receipt_binds_actual_nominal_source_and_fresh_restoration(record_property):
    units = fencing._production_units()
    failures, receipt = _checkpoint_role_local_result(units, record_property, "original")
    assert failures == [], failures
    calls, uses = _checkpoint_role_require_membership(units, receipt)
    restored = _checkpoint_role_restored(units)
    restored_failures, restored_receipt = _checkpoint_role_local_result(restored, record_property, "restored")
    assert restored_failures == [], restored_failures
    restored_calls, restored_uses = _checkpoint_role_require_membership(restored, restored_receipt)
    assert not calls & restored_calls and not uses & restored_uses
    original_unit, *_ = _checkpoint_role_nodes(units)
    restored_unit, *_ = _checkpoint_role_nodes(restored)
    assert original_unit.source == restored_unit.source and original_unit.tree is not restored_unit.tree


@pytest.mark.parametrize(
    "binding", ("TypeAdapter", "_decode_owned", "_dump_owned", "BaseModel", "ConfigDict", "Field", "JsonValue", "UUID", "datetime")
)
@pytest.mark.parametrize("fault", ("missing", "foreign"))
def test_checkpoint_forwarding_refuses_each_isolated_nominal_import_and_restores_it(binding, fault, record_property):
    units = fencing._production_units()
    failures, receipt = _checkpoint_role_local_result(units, record_property, "original")
    assert failures == [], failures
    _checkpoint_role_require_membership(units, receipt)
    unit, *_ = _checkpoint_role_nodes(units)
    imports = [
        node
        for node in unit.tree.body
        if isinstance(node, ast.ImportFrom) and any((alias.asname or alias.name) == binding for alias in node.names)
    ]
    assert len(imports) == 1
    original = imports[0]
    (alias,) = [alias for alias in original.names if (alias.asname or alias.name) == binding]
    retained = copy.deepcopy(original)
    retained.names = [candidate for candidate in retained.names if (candidate.asname or candidate.name) != binding]
    replacement = ast.unparse(retained) if retained.names else ""
    if fault == "foreign":
        foreign = ast.ImportFrom(module="elspeth.checkpoint_foreign_supplier", names=[copy.deepcopy(alias)], level=0)
        replacement += "\n" + ast.unparse(foreign)
    segment = ast.get_source_segment(unit.source, original)
    assert segment is not None
    changed_units = _checkpoint_role_replace(units, unit.path, segment, replacement)
    (changed,) = [source for source in changed_units if source.path == unit.path]
    # Preserve every unrelated alias, including the other imported codec.
    expected = [
        (node.module, node.level, candidate.name, candidate.asname)
        for node in unit.tree.body
        if isinstance(node, ast.ImportFrom)
        for candidate in node.names
        if candidate is not alias
    ]
    actual = [
        (node.module, node.level, candidate.name, candidate.asname)
        for node in changed.tree.body
        if isinstance(node, ast.ImportFrom)
        for candidate in node.names
        if (candidate.asname or candidate.name) != binding
    ]
    assert actual == expected
    opposed, refused = _checkpoint_role_local_result(changed_units, record_property, "opposed")
    reason = "batch callback consumer canonical import changed " + unit.path + ":" + binding
    assert reason in opposed, opposed
    assert refused == {}
    restored = _checkpoint_role_restored(units)
    restored_failures, restored_receipt = _checkpoint_role_local_result(restored, record_property, "restored")
    assert restored_failures == [], restored_failures
    _checkpoint_role_require_membership(restored, restored_receipt)


@pytest.mark.parametrize(
    "fault",
    (
        "schema_base",
        "schema_config",
        "schema_field",
        "schema_effect",
        "schema_rebound",
        "adapter_schema",
        "adapter_rebound",
        "helper_rebound",
        "helper_parameter",
        "adapter_parameter",
        "helper_local",
        "wrong_adapter",
        "missing_call",
        "duplicate_call",
        "moved_call",
        "nominal_guard",
        "utc_guard",
        "cohort_guard",
        "dump_warnings",
        "decode_strict",
        "decode_roundtrip",
    ),
)
def test_checkpoint_forwarding_refuses_changed_nominal_producer_helper_and_history_owner(fault, record_property):
    units = fencing._production_units()
    failures, receipt = _checkpoint_role_local_result(units, record_property, "original")
    assert failures == [], failures
    _checkpoint_role_require_membership(units, receipt)
    unit, schema, owner, constructor, decode, _dump = _checkpoint_role_nodes(units)
    path = unit.path
    schema_text = ast.get_source_segment(unit.source, schema)
    owner_text = ast.get_source_segment(unit.source, owner)
    assert schema_text is not None and owner_text is not None
    reason = "batch callback consumer complete consumer body changed " + path + ":historical_checkpoint_record"
    if fault.startswith("schema_") and fault != "schema_rebound":
        before = schema_text
        reason = "batch callback consumer complete consumer body changed " + path + ":HistoricalCheckpointData"
        if fault == "schema_base":
            after = before.replace("HistoricalCheckpointData(BaseModel)", "HistoricalCheckpointData(ForeignBaseModel)", 1)
        elif fault == "schema_config":
            after = before.replace('extra="forbid", strict=True, frozen=True', 'extra="allow", strict=True, frozen=True', 1)
        elif fault == "schema_field":
            after = before.replace("version: int = Field(ge=1)", "version: int = Field(ge=2)", 1)
        else:
            assert fault == "schema_effect"
            after = before + "\n\n    def publish(self):\n        opaque(self)"
    elif fault == "schema_rebound":
        before, after = schema_text, schema_text + "\nHistoricalCheckpointData = foreign"
        reason = "batch callback consumer consumer binding changed " + path + ":HistoricalCheckpointData"
    elif fault == "adapter_schema":
        before = "_CHECKPOINT = " + ast.get_source_segment(unit.source, constructor)
        after = "_CHECKPOINT = TypeAdapter(RetainedP4Outcome)"
        reason = "batch callback consumer actual adapter declaration changed _CHECKPOINT"
    elif fault == "adapter_rebound":
        before = "_CHECKPOINT = " + ast.get_source_segment(unit.source, constructor)
        after = before + "\n_CHECKPOINT = foreign"
        reason = "batch callback consumer adapter rebound _CHECKPOINT"
    elif fault == "helper_rebound":
        before = "from elspeth.web.composer.continuation_tool_batch import _decode_owned, _dump_owned"
        after = before + "\n_dump_owned = foreign"
        reason = "batch callback consumer canonical import changed " + path + ":_dump_owned"
    elif fault in {"dump_warnings", "decode_strict", "decode_roundtrip"}:
        path = _CHECKPOINT_ROLE_HELPER_PATH
        (helper_unit,) = [source for source in units if source.path == path]
        name = "_dump_owned" if fault == "dump_warnings" else "_decode_owned"
        helper = fencing._reserve_node_at(helper_unit, name)
        before = ast.get_source_segment(helper_unit.source, helper)
        assert before is not None
        if fault == "dump_warnings":
            after = before.replace('warnings="error"', 'warnings="none"', 1)
        elif fault == "decode_strict":
            after = before.replace("validate_json(encoded, strict=True)", "validate_json(encoded, strict=False)", 1)
        else:
            after = before.replace("if _dump_owned(adapter, result) != encoded:", "if False:", 1)
        reason = "batch callback consumer complete consumer body changed " + path + ":" + name
    else:
        before = owner_text
        call_text = ast.get_source_segment(unit.source, decode)
        assert call_text is not None and before.count(call_text) == 1
        if fault == "helper_parameter":
            after = before.replace("*, env_ref_names:", "*, _dump_owned=None, env_ref_names:", 1)
        elif fault == "adapter_parameter":
            after = before.replace("*, env_ref_names:", "*, _CHECKPOINT=None, env_ref_names:", 1)
        elif fault == "helper_local":
            after = before.replace("    value = " + call_text, "    _dump_owned = foreign\n    value = " + call_text, 1)
        elif fault == "wrong_adapter":
            after = before.replace(call_text, call_text.replace("_CHECKPOINT", "_VALUE", 1), 1)
        elif fault == "missing_call":
            after = before.replace("    value = " + call_text, "    value = value", 1)
        elif fault == "duplicate_call":
            after = before.replace("    value = " + call_text, "    value = " + call_text + "\n    value = " + call_text, 1)
        elif fault == "moved_call":
            after = before.replace("    value = " + call_text, "    value = value", 1)
            after += "\n\ndef unrelated_checkpoint_owner(value):\n    return " + call_text
        elif fault == "nominal_guard":
            after = before.replace("type(value) is not HistoricalCheckpointData", "type(value) is not object", 1)
        elif fault == "utc_guard":
            after = before.replace("value.created_at.utcoffset() != timedelta(0)", "False", 1)
        else:
            assert fault == "cohort_guard"
            after = before.replace("value.inserted_state_ids[-1] != value.id", "False", 1)
    assert before != after
    changed = _checkpoint_role_replace(units, path, before, after)
    opposed, refused = _checkpoint_role_local_result(changed, record_property, "opposed")
    assert reason in opposed, opposed
    assert refused == {}
    restored = _checkpoint_role_restored(units)
    restored_failures, restored_receipt = _checkpoint_role_local_result(restored, record_property, "restored")
    assert restored_failures == [], restored_failures
    _checkpoint_role_require_membership(restored, restored_receipt)


def _checkpoint_role_observe_references(units, receipt):
    uses = []
    failures = fencing._reserve_selected_origin_effect_failures(
        units,
        extra_roots=receipt["origins"],
        receiver_members=receipt["receiver_members"],
        protected_supplier_bindings=receipt["supplier_bindings"],
        immutable_supplier_origins=receipt["immutable_supplier_origins"],
        validated_nonretaining_calls=receipt["calls"],
        validated_supplier_uses=receipt["uses"],
        validated_supplier_arguments=receipt["argument_calls"],
        validated_immediate_lambdas=receipt["immediate_lambdas"],
        protected_use_nodes=uses,
    )
    return failures, uses


def test_checkpoint_forwarding_reference_join_retains_all_findings_and_refuses_added_or_detached_uses(record_property):
    units = fencing._production_units()
    local_failures, receipt = _checkpoint_role_local_result(units, record_property, "original")
    assert local_failures == [], local_failures
    _checkpoint_role_require_membership(units, receipt)
    # Exactly one complete-source owner observation. Its failures remain DATA
    # for this reference discriminator; no outside-effect positive is claimed.
    base_failures, base_uses = _checkpoint_role_observe_references(units, receipt)
    record_property("checkpoint_role_complete_owner_failures", fencing._reserve_json.dumps(base_failures))
    actual = {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}
    assert all((row[0], row[1]) in actual for row in base_uses)
    assert receipt["observed_uses"] <= {(row[0], row[1]) for row in base_uses}, base_failures
    base_join = fencing._reserve_batch_callback_use_failures(units, receipt, base_uses)
    record_property("checkpoint_role_complete_use_join", fencing._reserve_json.dumps(base_join))

    path = "src/elspeth/checkpoint_role_reference_probe.py"
    assert not any(unit.path == path for unit in units)
    imports = (
        "from elspeth.web.composer.continuation_tool_batch import _decode_owned, _dump_owned\n"
        "from elspeth.web.composer.continuation_p4_outcome import _CHECKPOINT\n"
    )
    companion_paths = (
        "src/elspeth/web/async_workers.py",
        "src/elspeth/web/app.py",
        "src/elspeth/web/application_finalizers.py",
        "src/elspeth/web/process_recovery.py",
        "src/elspeth/web/coordination/repository.py",
    )
    companions = tuple(next(unit for unit in units if unit.path == name) for name in companion_paths)

    def observe(probe, label):
        failures, uses = _checkpoint_role_observe_references((probe, *companions), receipt)
        record_property("checkpoint_role_" + label + "_probe_owner_failures", fencing._reserve_json.dumps(failures))
        # These are two genuine, separate observations. Concatenation tests
        # references only: it proves no combined-graph effect/completeness fact.
        joined = fencing._reserve_batch_callback_use_failures((*units, probe), receipt, [*base_uses, *uses])
        record_property("checkpoint_role_" + label + "_complete_reference_join", fencing._reserve_json.dumps(joined))
        probe_actual = {(source.path, id(node)) for source in (probe, *companions) for node in ast.walk(source.tree)}
        assert all((row[0], row[1]) in probe_actual for row in uses)
        return failures, uses, joined

    original = fencing._parse_source(path, imports)
    original_failures, original_uses, original_join = observe(original, "imports_only")
    for label, addition, spelling, origin in (
        ("dump_argument", "opaque(_dump_owned)\n", "_dump_owned", _CHECKPOINT_ROLE_HELPER_MODULE + "._dump_owned"),
        ("decode_return", "def escaped():\n    return _decode_owned\n", "_decode_owned", _CHECKPOINT_ROLE_HELPER_MODULE + "._decode_owned"),
        ("adapter_argument", "opaque(_CHECKPOINT)\n", "_CHECKPOINT", _CHECKPOINT_ROLE_MODULE + "._CHECKPOINT"),
        ("adapter_member", "held = _CHECKPOINT.dump_python\n", "_CHECKPOINT", _CHECKPOINT_ROLE_MODULE + "._CHECKPOINT"),
        ("adapter_write", "_CHECKPOINT.dump_python = replacement\n", "_CHECKPOINT", _CHECKPOINT_ROLE_MODULE + "._CHECKPOINT"),
    ):
        changed = fencing._parse_source(path, imports + addition)
        (use,) = [
            node for node in ast.walk(changed.tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == spelling
        ]
        _opposed_failures, opposed_uses, opposed_join = observe(changed, label)
        assert any(row[0] == path and row[1] == id(use) and row[2] == ("object", origin) for row in opposed_uses), opposed_uses
        expected = "batch callback consumer unproved supplier role " + path + ":" + str(use.lineno) + ":" + origin
        assert expected in opposed_join and expected not in original_join, opposed_join
        # Restoration is freshly parsed, with its own genuine observation;
        # complete outputs, including every independent finding, must match.
        restored = fencing._parse_source(path, imports)
        assert restored.tree is not original.tree and restored.tree is not changed.tree
        restored_failures, restored_uses, restored_join = observe(restored, label + "_restored")
        assert restored_failures == original_failures
        assert restored_uses == original_uses
        assert restored_join == original_join
        detached_source = fencing._parse_source(path, changed.source)
        detached_join = fencing._reserve_batch_callback_use_failures((*units, detached_source), receipt, [*base_uses, *opposed_uses])
        assert "batch callback consumer reference detached from actual AST" in detached_join, detached_join

    reparsed = _checkpoint_role_restored(units)
    restored_failures, restored_receipt = _checkpoint_role_local_result(reparsed, record_property, "fresh_nominal_restored")
    assert restored_failures == [], restored_failures
    _checkpoint_role_require_membership(reparsed, restored_receipt)
    # A real receipt from another parse cannot admit the old original uses.
    detached_receipt_join = fencing._reserve_batch_callback_use_failures(units, restored_receipt, base_uses)
    unit, _schema, _owner, _constructor, decode, _dump = _checkpoint_role_nodes(units)
    expected = (
        "batch callback consumer unproved supplier role "
        + unit.path
        + ":"
        + str(decode.func.lineno)
        + ":"
        + _CHECKPOINT_ROLE_HELPER_MODULE
        + "._decode_owned"
    )
    assert expected in detached_receipt_join, detached_receipt_join
    again_failures, again_receipt = _checkpoint_role_local_result(units, record_property, "same_original_restored")
    assert again_failures == [], again_failures
    assert again_receipt == receipt
    assert fencing._reserve_batch_callback_use_failures(units, again_receipt, base_uses) == base_join


@pytest.mark.parametrize("binding", ("str", "int", "bool", "dict", "list", "tuple"))
def test_checkpoint_forwarding_refuses_replaced_builtin_schema_annotation_sources(binding, record_property):
    units = fencing._production_units()
    failures, receipt = _checkpoint_role_local_result(units, record_property, "original")
    assert failures == [], failures
    _checkpoint_role_require_membership(units, receipt)
    unit, schema, *_ = _checkpoint_role_nodes(units)
    assert any(isinstance(node, ast.Name) and node.id == binding for node in ast.walk(schema))
    before = ast.get_source_segment(unit.source, schema)
    assert before is not None
    changed = _checkpoint_role_replace(units, unit.path, before, binding + " = foreign\n\n" + before)
    opposed, refused = _checkpoint_role_local_result(changed, record_property, "opposed")
    assert "batch callback consumer builtin changed " + unit.path + ":" + binding in opposed, opposed
    assert refused == {}
    restored = _checkpoint_role_restored(units)
    restored_failures, restored_receipt = _checkpoint_role_local_result(restored, record_property, "restored")
    assert restored_failures == [], restored_failures
    _checkpoint_role_require_membership(restored, restored_receipt)


@pytest.mark.parametrize("placement", ("before_schema", "before_type_adapter_import"))
def test_checkpoint_forwarding_refuses_the_same_eager_constructor_before_its_declared_sources(placement, record_property):
    units = fencing._production_units()
    failures, receipt = _checkpoint_role_local_result(units, record_property, "original")
    assert failures == [], failures
    original_calls, original_uses = _checkpoint_role_require_membership(units, receipt)
    unit, schema, owner, constructor, _decode, _dump = _checkpoint_role_nodes(units)
    declaration = fencing._reserve_node_at(unit, "_CHECKPOINT")
    assert isinstance(declaration, ast.Assign) and declaration.value is constructor
    assert any(node is declaration for node in unit.tree.body)
    imports = [
        node
        for node in unit.tree.body
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == "pydantic"
        and any(alias.name == "TypeAdapter" and (alias.asname or alias.name) == "TypeAdapter" for alias in node.names)
    ]
    assert len(imports) == 1
    anchor = schema if placement == "before_schema" else imports[0]
    positions = {id(node): index for index, node in enumerate(unit.tree.body)}
    assert positions[id(imports[0])] < positions[id(schema)] < positions[id(declaration)]
    assignment_text = ast.get_source_segment(unit.source, declaration)
    anchor_text = ast.get_source_segment(unit.source, anchor)
    assert assignment_text is not None and anchor_text is not None
    assert unit.source.count(assignment_text + "\n") == unit.source.count(anchor_text) == 1
    without = unit.source.replace(assignment_text + "\n", "", 1)
    changed = fencing._parse_source(unit.path, without.replace(anchor_text, assignment_text + "\n\n" + anchor_text, 1))
    changed_units = tuple(changed if source is unit else source for source in units)
    _changed_unit, changed_schema, changed_owner, changed_constructor, _changed_decode, _changed_dump = _checkpoint_role_nodes(
        changed_units
    )
    changed_declaration = fencing._reserve_node_at(changed, "_CHECKPOINT")
    assert changed_declaration.value is changed_constructor
    assert ast.dump(changed_declaration) == ast.dump(declaration)
    assert ast.dump(changed_schema) == ast.dump(schema) and ast.dump(changed_owner) == ast.dump(owner)
    assert [ast.dump(node) for node in changed.tree.body if isinstance(node, (ast.Import, ast.ImportFrom))] == [
        ast.dump(node) for node in unit.tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert [ast.dump(node) for node in changed.tree.body if node is not changed_declaration] == [
        ast.dump(node) for node in unit.tree.body if node is not declaration
    ]
    changed_positions = {id(node): index for index, node in enumerate(changed.tree.body)}
    changed_anchor = (
        changed_schema
        if placement == "before_schema"
        else next(node for node in changed.tree.body if isinstance(node, ast.ImportFrom) and ast.dump(node) == ast.dump(imports[0]))
    )
    assert changed_positions[id(changed_declaration)] < changed_positions[id(changed_anchor)]
    opposed, refused = _checkpoint_role_local_result(changed_units, record_property, "opposed")
    reason = "batch callback consumer nominal schema must precede actual adapter constructor " + unit.path + ":_CHECKPOINT"
    assert reason in opposed, opposed
    assert refused == {}
    restored = _checkpoint_role_restored(units)
    restored_failures, restored_receipt = _checkpoint_role_local_result(restored, record_property, "restored")
    assert restored_failures == [], restored_failures
    restored_calls, restored_uses = _checkpoint_role_require_membership(restored, restored_receipt)
    assert not original_calls & restored_calls and not original_uses & restored_uses


@pytest.mark.parametrize("protected", (False, True))
def test_retention_fixpoint_real_companions_gate_and_restore_actual_probe_roots(monkeypatch, protected, record_property):
    path = "src/elspeth/retention_fixpoint_companion_probe.py"
    source = (
        "from elspeth.contracts.prompt_guard import PromptGuardTrust\n"
        "def held():\n    leaf = " + ("PromptGuardTrust.REFERENCE" if protected else "'safe'") + "\n"
        "    def child(value=leaf):\n        return value\n    return child\n"
    )
    probe = fencing._parse_source(path, source)
    (supplier,) = _source_units(("src/elspeth/contracts/prompt_guard.py",))
    units = (supplier, probe, *_source_units(_RETENTION_FIXPOINT_COMPANION_PATHS))
    plugin_path = "src/elspeth/contracts/plugin_capabilities.py"
    (plugin,) = [unit for unit in units if unit.path == plugin_path]
    assert sum(unit is probe for unit in units) == 1
    held = probe.tree.body[1]
    assert isinstance(held, ast.FunctionDef)
    child = held.body[1]
    returned = held.body[-1].value
    assert isinstance(child, ast.FunctionDef) and ast.unparse(child.args.defaults[0]) == "leaf"
    assert isinstance(returned, ast.Name) and returned.id == "child"
    probe_nodes = tuple(ast.walk(probe.tree))
    original_ast = ast.dump(probe.tree, include_attributes=True)
    roots = frozenset({"elspeth.contracts.prompt_guard.PromptGuardTrust"})
    real_cache = fencing.cache

    def observe(cohort, label):
        events, graphs = [], []

        def decorated(function):
            wrapped = real_cache(function)
            if not function.__qualname__.endswith((".retained_empty_root_state", ".retained_capture_root")):
                return wrapped
            evaluate = function.__kwdefaults__["evaluate"]
            actual_unit = evaluate.__kwdefaults__["unit"]
            if actual_unit is not probe:
                return wrapped
            graph = evaluate.__kwdefaults__["graph"]
            graphs.append(graph)
            kind = function.__name__

            def current(value, **kwargs):
                assert any(value is node for node in probe_nodes)
                result = wrapped(value, **kwargs)
                events.append((kind, value, actual_unit, graph, result))
                return result

            return current

        assert sum(unit is probe for unit in cohort) == 1
        with monkeypatch.context() as patch:
            patch.setattr(fencing, "cache", decorated)
            failures = fencing._reserve_selected_origin_effect_failures(cohort, additional_supplier_roots=roots)
        record_property("retention_companion_" + label + "_complete_failures", fencing._reserve_json.dumps(failures))
        assert ast.dump(probe.tree, include_attributes=True) == original_ast
        assert all(unit is probe for _kind, _value, unit, _graph, _result in events)
        return failures, events, graphs

    def require_reached(events, graphs):
        assert events and graphs
        assert len({id(graph) for graph in graphs}) == 1
        assert {kind for kind, *_rest in events} == {"retained_empty_root_state", "retained_capture_root"}
        expressions = [event for event in events if event[0] == "retained_empty_root_state" and event[1] is returned]
        captures = [event for event in events if event[0] == "retained_capture_root" and event[1] is child]
        assert expressions and captures
        for _kind, value, _unit, graph, result in expressions:
            assert graph.final[value, False, False] is result
            assert result.retained is protected
        for _kind, value, _unit, graph, result in captures:
            assert graph.final[value, False, True].retained is result is protected
        return graphs[0]

    reason = "retained enum carrier constructor/field contract changed elspeth.contracts.plugin_capabilities.CapabilityDeclaration"
    original_failures, original_events, original_graphs = observe(units, "original")
    assert reason not in original_failures, original_failures
    original_graph = require_reached(original_events, original_graphs)
    assert any(path + ":" in failure for failure in original_failures) is protected, original_failures

    missing = tuple(unit for unit in units if unit is not plugin)
    assert len(missing) == len(units) - 1 and any(unit is probe for unit in missing)
    missing_failures, missing_events, missing_graphs = observe(missing, "missing_plugin")
    assert reason in missing_failures, missing_failures
    assert missing_events == [] and missing_graphs == []

    fresh_plugin = fencing._parse_source(plugin.path, plugin.source)
    assert fresh_plugin.tree is not plugin.tree and fresh_plugin.source == plugin.source
    restored = tuple(fresh_plugin if unit is plugin else unit for unit in units)
    restored_failures, restored_events, restored_graphs = observe(restored, "restored")
    assert restored_failures == original_failures
    restored_graph = require_reached(restored_events, restored_graphs)
    assert restored_graph is not original_graph
    assert any(path + ":" in failure for failure in restored_failures) is protected, restored_failures


_CATALOG_ROLE_SOURCE = "src/elspeth/plugins/sources/llm/source.py"
_CATALOG_ROLE_CONFIG = "src/elspeth/plugins/sources/llm/config.py"
_CATALOG_ROLE_BASE = "src/elspeth/plugins/infrastructure/config_base.py"
_CATALOG_ROLE_SERVICE = "src/elspeth/web/catalog/service.py"
_CATALOG_ROLE_KNOBS = "src/elspeth/web/catalog/knob_schema.py"
_CATALOG_ROLE_SCHEMA = "src/elspeth/contracts/schema.py"
_CATALOG_ROLE_DEFAULTS = "src/elspeth/plugins/llm/config_validation.py"
_CATALOG_ROLE_ENUM = "src/elspeth/plugins/transforms/llm/multi_query.py"
_CATALOG_ROLE_REGISTRATION = "src/elspeth/contracts/value_source.py"
_CATALOG_ROLE_PATHS = (
    _CATALOG_ROLE_SOURCE,
    _CATALOG_ROLE_CONFIG,
    _CATALOG_ROLE_BASE,
    _CATALOG_ROLE_SERVICE,
    _CATALOG_ROLE_KNOBS,
    _CATALOG_ROLE_SCHEMA,
    "src/elspeth/plugins/transforms/llm/multi_query.py",
    _CATALOG_ROLE_DEFAULTS,
    _CATALOG_ROLE_REGISTRATION,
)


def _catalog_role_unit(units, path):
    matches = [unit for unit in units if unit.path == path]
    assert len(matches) == 1
    return matches[0]


def _catalog_role_producer(units):
    return fencing._reserve_node_at(_catalog_role_unit(units, _CATALOG_ROLE_SOURCE), "LLMSource")


def _catalog_role_receipt(units, producer):
    # The old model must fail here, rather than during collection/attribute use.
    assert "_reserve_catalog_returned_config_role_receipt" in vars(fencing), "finite catalog returned-config role receipt is absent"
    return fencing._reserve_catalog_returned_config_role_receipt(units, producer=producer)


def _catalog_role_expression(unit, symbol, expression):
    owner = fencing._reserve_node_at(unit, symbol)
    assert owner is not None
    nodes = [node for node in ast.walk(owner) if isinstance(node, ast.expr) and ast.unparse(node) == expression]
    assert len(nodes) == 1, (symbol, expression)
    return nodes[0]


def _catalog_role_ids(receipt):
    identities = set().union(
        *receipt["conditional_roles"].values(), *receipt["retained_projections"].values(), *receipt["callback_roles"].values()
    )
    for observation in receipt["deferred_retained_uses"].values():
        identities.update(
            observation[key]
            for key in ("owner", "field", "retaining_producer", "default_keyword", "value", "enum_class", "enum_member")
            if observation[key] is not None
        )
        identities.add((observation["unresolved"][1], observation["unresolved"][2]))
    for observation in receipt["class_info_operands"].values():
        identities.update(observation[key] for key in ("call", "builtin", "operand"))
        for leaf in observation["leaves"]:
            identities.update((leaf["use"], leaf["declaration"]))
        identities.add((observation["unresolved"][1], observation["unresolved"][2]))
    construction = receipt["deferred_holder_uses"]["QuerySpec.construction_return"]
    identities.update(
        construction[key]
        for key in (
            "producer",
            "owner",
            "decorator",
            "decorator_supplier",
            "call",
            "callee",
            "response_format_keyword",
            "response_format_input",
            "input_receiver",
            "input_parameter",
            "return",
        )
    )
    identities.add((construction["unresolved"][1], construction["unresolved"][2]))
    holder_check = receipt["deferred_holder_uses"]["QuerySpec.single_class_info"]
    identities.update(holder_check[key] for key in ("call", "builtin", "class_use", "owner"))
    identities.add((holder_check["unresolved"][1], holder_check["unresolved"][2]))
    for observation in receipt["deferred_producer_registrations"].values():
        identities.update(
            observation[key]
            for key in (
                "call",
                "callee_use",
                "registrar_import",
                "producer_argument",
                "producer",
                "config_attr_keyword",
                "config_attr_value",
                "property",
                "property_decorator",
                "property_parameter",
                "property_return",
                "property_projection",
                "registrar",
                "plugin_parameter",
                "config_attr_parameter",
                "registry",
                "registry_initializer",
                "membership",
                "existing_read",
                "store",
                "destination",
                "destination_receiver",
                "key",
                "value",
            )
        )
        identities.update(observation["registry_uses"])
        for pair in (*observation["argument_bindings"], *observation["store_bindings"]):
            identities.update(pair)
        for premise in (observation["unresolved"], observation["config_validation_unresolved"]):
            identities.add((premise[1], premise[2]))
    return identities


def _catalog_role_deferred_expected(units, schema_premise):
    enum = _catalog_role_unit(units, _CATALOG_ROLE_ENUM)
    enum_class = fencing._reserve_node_at(enum, "ResponseFormat")
    assert isinstance(enum_class, ast.ClassDef)
    expected = {}
    for owner_name in ("QueryDefinition", "QuerySpec"):
        owner = fencing._reserve_node_at(enum, owner_name)
        field = fencing._reserve_node_at(enum, owner_name + ".response_format")
        assert isinstance(owner, ast.ClassDef) and isinstance(field, ast.AnnAssign)
        keyword = None
        if owner_name == "QueryDefinition":
            producer = field.value
            assert isinstance(producer, ast.Call) and isinstance(producer.func, ast.Name) and producer.func.id == "Field"
            assert producer.args == [] and {kw.arg for kw in producer.keywords} == {"default", "description"}
            defaults = [kw for kw in producer.keywords if kw.arg == "default"]
            descriptions = [kw for kw in producer.keywords if kw.arg == "description"]
            assert len(defaults) == len(descriptions) == 1
            keyword = defaults[0]
            assert isinstance(descriptions[0].value, ast.Constant) and type(descriptions[0].value.value) is str
            value = keyword.value
        else:
            producer = field
            value = field.value
        assert isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == enum_class.name
        member = fencing._reserve_node_at(enum, enum_class.name + "." + value.attr)
        assert isinstance(member, ast.Assign) and isinstance(member.value, ast.Constant) and type(member.value.value) is str
        expected[owner_name + ".response_format"] = {
            "owner": (enum.path, id(owner)),
            "field": (enum.path, id(field)),
            "retaining_producer": (enum.path, id(producer)),
            "default_keyword": (enum.path, id(keyword)) if keyword is not None else None,
            "value": (enum.path, id(value)),
            "enum_class": (enum.path, id(enum_class)),
            "enum_member": (enum.path, id(member)),
            "unresolved": schema_premise,
        }
    return expected


def _catalog_role_class_info_expected(units, schema_premise):
    source = _catalog_role_unit(units, _CATALOG_ROLE_SOURCE)
    config = _catalog_role_unit(units, _CATALOG_ROLE_CONFIG)
    registry = fencing._reserve_node_at(config, "SOURCE_PROVIDER_CONFIGS")
    assert isinstance(registry, ast.AnnAssign) and isinstance(registry.value, ast.Dict)
    classes = {}
    for value in registry.value.values:
        assert isinstance(value, ast.Name)
        declaration = fencing._reserve_node_at(config, value.id)
        assert isinstance(declaration, ast.ClassDef)
        classes[value.id] = declaration
    calls = [
        node
        for node in ast.walk(source.tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"isinstance", "issubclass"}
        and len(node.args) == 2
        and not node.keywords
        and isinstance(node.args[1], ast.Tuple)
        and node.args[1].elts
        and all(isinstance(leaf, ast.Name) and leaf.id in classes for leaf in node.args[1].elts)
    ]
    assert len(calls) == 1
    call = calls[0]
    return {
        (source.path, id(call)): {
            "call": (source.path, id(call)),
            "builtin": (source.path, id(call.func)),
            "operand": (source.path, id(call.args[1])),
            "leaves": tuple(
                {"use": (source.path, id(leaf)), "declaration": (config.path, id(classes[leaf.id]))} for leaf in call.args[1].elts
            ),
            "unresolved": schema_premise,
        }
    }


def _catalog_role_holder_uses_expected(units, schema_premise):
    enum = _catalog_role_unit(units, _CATALOG_ROLE_ENUM)
    producer = fencing._reserve_node_at(enum, "_query_spec_from_definition")
    owner = fencing._reserve_node_at(enum, "QuerySpec")
    assert isinstance(producer, ast.FunctionDef) and isinstance(owner, ast.ClassDef)
    returns = [node for node in producer.body if isinstance(node, ast.Return)]
    assert len(returns) == 1 and isinstance(returns[0].value, ast.Call)
    returned = returns[0]
    call = returned.value
    assert isinstance(call.func, ast.Name) and call.func.id == owner.name
    arguments = [keyword for keyword in call.keywords if keyword.arg == "response_format"]
    assert len(arguments) == 1
    argument = arguments[0]
    assert isinstance(argument.value, ast.Attribute) and argument.value.attr == "response_format"
    assert isinstance(argument.value.value, ast.Name) and argument.value.value.id == "definition"
    parameters = [parameter for parameter in producer.args.args if parameter.arg == "definition"]
    assert len(parameters) == 1
    assert len(owner.decorator_list) == 1 and isinstance(owner.decorator_list[0], ast.Call)
    decorator = owner.decorator_list[0]
    assert isinstance(decorator.func, ast.Name) and decorator.func.id == "dataclass"
    imports = [
        alias
        for statement in enum.tree.body
        if isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module == "dataclasses"
        for alias in statement.names
        if alias.name == "dataclass" and (alias.asname or alias.name) == decorator.func.id
    ]
    assert len(imports) == 1
    holder_check = _catalog_role_expression(enum, "resolve_queries", "isinstance(item, QuerySpec)")
    assert isinstance(holder_check, ast.Call) and len(holder_check.args) == 2
    return {
        "QuerySpec.construction_return": {
            "producer": (enum.path, id(producer)),
            "owner": (enum.path, id(owner)),
            "decorator": (enum.path, id(decorator)),
            "decorator_supplier": (enum.path, id(imports[0])),
            "call": (enum.path, id(call)),
            "callee": (enum.path, id(call.func)),
            "response_format_keyword": (enum.path, id(argument)),
            "response_format_input": (enum.path, id(argument.value)),
            "input_receiver": (enum.path, id(argument.value.value)),
            "input_parameter": (enum.path, id(parameters[0])),
            "input_status": "UNRESOLVED",
            "result_status": "UNRESOLVED_HOLDER",
            "return": (enum.path, id(returned)),
            "unresolved": schema_premise,
        },
        "QuerySpec.single_class_info": {
            "call": (enum.path, id(holder_check)),
            "builtin": (enum.path, id(holder_check.func)),
            "class_use": (enum.path, id(holder_check.args[1])),
            "owner": (enum.path, id(owner)),
            "unresolved": schema_premise,
        },
    }


def _catalog_role_require_original(units, receipt):
    """Hand-derived source witnesses, never favorable concrete-dispatch facts."""
    assert receipt["failures"] == [], receipt["failures"]
    source = _catalog_role_unit(units, _CATALOG_ROLE_SOURCE)
    config = _catalog_role_unit(units, _CATALOG_ROLE_CONFIG)
    base = _catalog_role_unit(units, _CATALOG_ROLE_BASE)
    service = _catalog_role_unit(units, _CATALOG_ROLE_SERVICE)
    knobs = _catalog_role_unit(units, _CATALOG_ROLE_KNOBS)
    schema = _catalog_role_unit(units, _CATALOG_ROLE_SCHEMA)
    defaults = _catalog_role_unit(units, _CATALOG_ROLE_DEFAULTS)
    scalar_supplier = fencing._reserve_node_at(defaults, "OPENROUTER_BASE_URL")
    assert isinstance(scalar_supplier, ast.Assign) and isinstance(scalar_supplier.value, ast.Constant)
    assert type(scalar_supplier.value.value) is str
    scalar_imports = [
        alias
        for statement in config.tree.body
        if isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module == "elspeth.plugins.llm.config_validation"
        for alias in statement.names
        if alias.name == "OPENROUTER_BASE_URL" and (alias.asname or alias.name) == "OPENROUTER_BASE_URL"
    ]
    assert len(scalar_imports) == 1
    method = fencing._reserve_node_at(source, "LLMSource.discriminated_variants")
    assert isinstance(method, ast.FunctionDef)
    returned = [node for node in ast.walk(method) if isinstance(node, ast.Return)]
    assert len(returned) == 1
    assert ast.unparse(returned[0].value) == "('provider', dict(SOURCE_PROVIDER_CONFIGS))"
    forwarding = fencing._reserve_node_at(service, "CatalogServiceImpl._knob_schema")
    assert isinstance(forwarding, ast.FunctionDef)
    receiver_arg = forwarding.args.args[1]
    assert receiver_arg.arg == "plugin_cls"
    service_calls = {
        (service.path, id(node))
        for node in ast.walk(forwarding)
        if isinstance(node, ast.Call) and ast.unparse(node.func) == "lower_discriminated_to_knob_schema"
    }
    assert len(service_calls) == 2
    expected = {
        "registry": {(config.path, id(fencing._reserve_node_at(config, "SOURCE_PROVIDER_CONFIGS")))},
        "scalar_default_supplier": {(defaults.path, id(scalar_supplier))},
        "scalar_default_import": {(config.path, id(scalar_imports[0]))},
        "scalar_default_use": {(config.path, id(_catalog_role_expression(config, "OpenRouterLLMSourceConfig", "OPENROUTER_BASE_URL")))},
        "variant_classes": {
            (config.path, id(fencing._reserve_node_at(config, name)))
            for name in ("AzureOpenAILLMSourceConfig", "OpenRouterLLMSourceConfig", "BedrockLLMSourceConfig", "GatewayLLMSourceConfig")
        },
        "config_ancestry": {
            (config.path, id(fencing._reserve_node_at(config, "LLMSourceConfig"))),
            (base.path, id(fencing._reserve_node_at(base, "DataPluginConfig"))),
            (base.path, id(fencing._reserve_node_at(base, "PluginConfig"))),
        },
        "source_method": {(source.path, id(method))},
        "source_return": {(source.path, id(returned[0]))},
        "service_calls": service_calls,
        "service_receiver": {
            (service.path, id(_catalog_role_expression(service, "CatalogServiceImpl._knob_schema", "cast(Any, plugin_cls)").args[1]))
        },
        "lowering_receiver": {
            (knobs.path, id(_catalog_role_expression(knobs, "lower_discriminated_to_knob_schema", "cast(Any, plugin_cls)").args[1]))
        },
        "variant_call": {
            (knobs.path, id(_catalog_role_expression(knobs, "lower_discriminated_to_knob_schema", "discriminated_variants()")))
        },
        "variant_items": {(knobs.path, id(_catalog_role_expression(knobs, "lower_discriminated_to_knob_schema", "variants.items()")))},
        "model_fields_items": {
            (knobs.path, id(_catalog_role_expression(knobs, "lower_discriminated_to_knob_schema", "variant_cls.model_fields.items()")))
        },
        "hidden_argument": {
            (knobs.path, id(_catalog_role_expression(knobs, "lower_discriminated_to_knob_schema", "_is_composer_hidden(info)").args[0]))
        },
        "lower_argument": {
            (
                knobs.path,
                id(
                    _catalog_role_expression(
                        knobs,
                        "lower_discriminated_to_knob_schema",
                        "_lower_field(fname, info, plugin_kind=plugin_kind, plugin_name=plugin_name)",
                    ).args[1]
                ),
            )
        },
        "base_model": {(base.path, id(_catalog_role_expression(base, "PluginConfig", "BaseModel")))},
        "json_schema_operation": {
            (
                source.path,
                id(_catalog_role_expression(source, "LLMSource.get_config_schema", "adapter.json_schema(ref_template='#/$defs/{model}')")),
            )
        },
        "value_validation_operation": {
            (
                source.path,
                id(_catalog_role_expression(source, "LLMSource.__init__", "config_cls.from_dict(provider_config, plugin_name=self.name)")),
            )
        },
    }
    assert receipt["conditional_roles"] == expected
    extras = fencing._reserve_node_at(knobs, "_composer_extras")
    assert isinstance(extras, ast.FunctionDef)
    extras_return = [node.value for node in ast.walk(extras) if isinstance(node, ast.Return) and isinstance(node.value, ast.Name)]
    assert len(extras_return) == 1 and extras_return[0].id == "extra"
    default = fencing._reserve_node_at(knobs, "_attach_default")
    assert isinstance(default, ast.FunctionDef)
    default_assignment = [
        node
        for node in ast.walk(default)
        if isinstance(node, ast.Assign) and any(ast.unparse(target) == "default" for target in node.targets)
    ]
    default_read = [node.value for node in default_assignment if ast.unparse(node.value) == "info.default"]
    assert len(default_read) == 1
    assert receipt["retained_projections"] == {
        "extras": {(knobs.path, id(extras_return[0]))},
        "default": {(knobs.path, id(default_read[0]))},
        "equals": {(knobs.path, id(_catalog_role_expression(knobs, "_attach_required_when", "predicate['equals']")))},
    }
    title = fencing._reserve_node_at(base, "_plugin_config_field_title")
    assert isinstance(title, ast.FunctionDef)
    assert receipt["callback_roles"]["schema"] == {(base.path, id(title))}
    ordinary_validator = fencing._reserve_node_at(config, "LLMSourceConfig._validate_prompt_template")
    before_validator = fencing._reserve_node_at(schema, "_coerce_field_definition")
    assert isinstance(ordinary_validator, ast.FunctionDef) and isinstance(before_validator, ast.FunctionDef)
    assert {(config.path, id(ordinary_validator)), (schema.path, id(before_validator))} <= receipt["callback_roles"]["validation"]
    assert receipt["callback_roles"]["schema"].isdisjoint(receipt["callback_roles"]["validation"])
    validation_call = next(iter(expected["value_validation_operation"]))
    adapter = _catalog_role_expression(
        source, "LLMSource.get_config_schema", "TypeAdapter(Annotated[union_type, PydanticField(discriminator='provider')])"
    )
    _registration_source, registration_call = _catalog_registration_call(units)
    registration_premise = ("retained producer registration", source.path, id(registration_call))
    assert receipt["unresolved"] == {
        ("catalog receiver origin", service.path, id(receiver_arg)),
        ("returned config validation", *validation_call),
        ("schema retained-origin effects", source.path, id(adapter)),
        registration_premise,
    }
    assert receipt["closed_consumers"] == frozenset()
    assert "deferred_retained_uses" in receipt, "catalog deferred retained-use observation map is absent"
    assert "class_info_operands" in receipt, "catalog class-info operand observation map is absent"
    schema_premise = ("schema retained-origin effects", source.path, id(adapter))
    assert receipt["deferred_retained_uses"] == _catalog_role_deferred_expected(units, schema_premise)
    assert receipt["class_info_operands"] == _catalog_role_class_info_expected(units, schema_premise)
    assert "deferred_holder_uses" in receipt, "catalog deferred holder-use observation map is absent"
    assert receipt["deferred_holder_uses"] == _catalog_role_holder_uses_expected(units, schema_premise)
    assert "deferred_producer_registrations" in receipt, "catalog deferred producer-registration map is absent"
    assert receipt["deferred_producer_registrations"] == _catalog_role_registration_expected(
        units, registration_premise, ("returned config validation", *validation_call)
    )
    originals = {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}
    assert _catalog_role_ids(receipt) <= originals
    return _catalog_role_ids(receipt)


def _catalog_role_replace(units, path, before, after):
    unit = _catalog_role_unit(units, path)
    assert unit.source.count(before) == 1, (path, before)
    changed = fencing._parse_source(path, unit.source.replace(before, after, 1))
    return tuple(changed if candidate is unit else candidate for candidate in units)


def _catalog_role_restore(units):
    restored = tuple(fencing._parse_source(unit.path, unit.source) for unit in units)
    receipt = _catalog_role_receipt(restored, _catalog_role_producer(restored))
    _catalog_role_require_original(restored, receipt)
    return restored, receipt


def _catalog_role_refused(receipt, path, role):
    assert any(path in failure and role in failure for failure in receipt["failures"]), receipt["failures"]
    assert receipt["conditional_roles"] == {}
    assert receipt["retained_projections"] == {}
    assert receipt["callback_roles"] == {"schema": frozenset(), "validation": frozenset()}
    assert receipt["closed_consumers"] == frozenset()
    assert "deferred_retained_uses" in receipt, "catalog deferred retained-use observation map is absent"
    assert receipt["deferred_retained_uses"] == {}
    assert "class_info_operands" in receipt, "catalog class-info operand observation map is absent"
    assert receipt["class_info_operands"] == {}
    assert "deferred_holder_uses" in receipt, "catalog deferred holder-use observation map is absent"
    assert receipt["deferred_holder_uses"] == {}
    assert "deferred_producer_registrations" in receipt, "catalog deferred producer-registration map is absent"
    assert receipt["deferred_producer_registrations"] == {}


def test_catalog_returned_config_original_roles_expose_unresolved_receiver_and_validation():
    units = _source_units(_CATALOG_ROLE_PATHS)
    receipt = _catalog_role_receipt(units, _catalog_role_producer(units))
    _catalog_role_require_original(units, receipt)
    # A conditional role must leave the real denial owner and source169 intact.
    source = _catalog_role_unit(units, _CATALOG_ROLE_SOURCE)
    adapter = _catalog_role_expression(
        source, "LLMSource.get_config_schema", "TypeAdapter(Annotated[union_type, PydanticField(discriminator='provider')])"
    )
    assert any(
        failure == "typing supplier namespace transfer is unclosed " + source.path + ":" + str(adapter.lineno)
        for failure in fencing._reserve_typing_supplier_mutation_failures((source,))
    )


def test_catalog_returned_config_roles_are_original_identity_only_and_restore_fresh():
    units = _source_units(_CATALOG_ROLE_PATHS)
    producer = _catalog_role_producer(units)
    original = _catalog_role_receipt(units, producer)
    original_ids = _catalog_role_require_original(units, original)
    for detached in (None, copy.deepcopy(producer), ast.parse("class LLMSource: pass").body[0]):
        refused = _catalog_role_receipt(units, detached)
        _catalog_role_refused(refused, _CATALOG_ROLE_SOURCE, "producer")
    restored, receipt = _catalog_role_restore(units)
    assert original_ids.isdisjoint(_catalog_role_ids(receipt))
    _catalog_role_refused(_catalog_role_receipt(restored, producer), _CATALOG_ROLE_SOURCE, "producer")
    assert _catalog_role_receipt(units, producer) == original


@pytest.mark.parametrize("path", _CATALOG_ROLE_PATHS)
def test_catalog_returned_config_roles_refuse_missing_or_duplicate_companions(path):
    units = _source_units(_CATALOG_ROLE_PATHS)
    producer = _catalog_role_producer(units)
    _catalog_role_require_original(units, _catalog_role_receipt(units, producer))
    missing = tuple(unit for unit in units if unit.path != path)
    _catalog_role_refused(_catalog_role_receipt(missing, producer), path, "module")
    duplicate = (*units, _catalog_role_unit(units, path))
    _catalog_role_refused(_catalog_role_receipt(duplicate, producer), path, "module")
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "path,before,after,role",
    [
        (
            _CATALOG_ROLE_BASE,
            "from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator",
            "from foreign import BaseModel, Field, ValidationError, field_validator, model_validator",
            "binding",
        ),
        (
            _CATALOG_ROLE_CONFIG,
            "from elspeth.plugins.infrastructure.config_base import DataPluginConfig",
            "from foreign import DataPluginConfig",
            "binding",
        ),
        (
            _CATALOG_ROLE_CONFIG,
            "class LLMSourceConfig(DataPluginConfig):",
            "class LLMSourceConfig(DataPluginConfig, metaclass=Foreign):",
            "ancestry",
        ),
        (_CATALOG_ROLE_CONFIG, '"azure": AzureOpenAILLMSourceConfig,', '"azure": foreign_config,', "class origin"),
        (_CATALOG_ROLE_SOURCE, "    SOURCE_PROVIDER_CONFIGS,", "    SOURCE_PROVIDER_CONFIGS as foreign_registry,", "binding"),
        (
            _CATALOG_ROLE_SOURCE,
            "def discriminated_variants(cls)",
            "def discriminated_variants(cls, SOURCE_PROVIDER_CONFIGS=None)",
            "binding",
        ),
        (_CATALOG_ROLE_SOURCE, "def discriminated_variants(cls)", "def discriminated_variants(cls, dict=None)", "binding"),
        (_CATALOG_ROLE_SOURCE, "def discriminated_variants(cls)", "def absent_variants(cls)", "target"),
        (_CATALOG_ROLE_SOURCE, 'return ("provider", dict(SOURCE_PROVIDER_CONFIGS))', 'return ("provider", opaque_map())', "return"),
        (
            _CATALOG_ROLE_SOURCE,
            'return ("provider", dict(SOURCE_PROVIDER_CONFIGS))',
            'return ("provider", dict(SOURCE_PROVIDER_CONFIGS))\n\n    discriminated_variants = foreign_method',
            "binding",
        ),
        (_CATALOG_ROLE_SERVICE, "from elspeth.web.catalog.knob_schema import (", "from foreign import (", "binding"),
        (
            _CATALOG_ROLE_SERVICE,
            "        try:\n            discriminated_variants =",
            "        opaque_callback(plugin_cls)\n        try:\n            discriminated_variants =",
            "receiver",
        ),
        (_CATALOG_ROLE_KNOBS, "from pydantic.fields import FieldInfo", "from foreign import FieldInfo", "binding"),
        (
            _CATALOG_ROLE_KNOBS,
            "discriminated_variants = cast(Any, plugin_cls).discriminated_variants",
            "discriminated_variants = cast(Any, opaque_receiver).discriminated_variants",
            "receiver",
        ),
        (_CATALOG_ROLE_KNOBS, "discriminator, variants = discriminated_variants()", "discriminator, variants = foreign_method()", "target"),
        (
            _CATALOG_ROLE_KNOBS,
            "discriminator, variants = discriminated_variants()",
            "discriminated_variants = foreign_method\n    discriminator, variants = discriminated_variants()",
            "target",
        ),
        (
            _CATALOG_ROLE_KNOBS,
            "discriminator, variants = discriminated_variants()",
            "discriminator, variants = discriminated_variants()\n    variants = opaque_map(variants)",
            "result",
        ),
        (
            _CATALOG_ROLE_KNOBS,
            "for variant_value, variant_cls in variants.items():",
            "for variant_value, variant_cls in opaque_map().items():",
            "projection",
        ),
        (
            _CATALOG_ROLE_KNOBS,
            "for fname, info in variant_cls.model_fields.items():",
            "for fname, info in foreign_cls.model_fields.items():",
            "projection",
        ),
        (_CATALOG_ROLE_KNOBS, "            if _is_composer_hidden(info):", "            if _is_composer_hidden(foreign_info):", "argument"),
        (
            _CATALOG_ROLE_KNOBS,
            "                fname,\n                info,",
            "                fname,\n                foreign_info,",
            "argument",
        ),
        (_CATALOG_ROLE_KNOBS, "    del plugin_kind, plugin_name", "    opaque_callback(info)\n    del plugin_kind, plugin_name", "helper"),
    ],
)
def test_catalog_returned_config_roles_refuse_changed_binding_return_target_or_helper(path, before, after, role):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(units, path, before, after)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), path, role)
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "effect",
    [
        "R['azure'].model_fields = replacement\n",
        "dict(R)['azure'].model_fields['model'].default = replacement\n",
        "R['azure'].model_config['json_schema_extra'] = callback\n",
        "dict(R)['azure'].__get_pydantic_core_schema__ = callback\n",
        "R['azure'].model_fields.update(replacement)\n",
        "opaque_callback(R)\n",
        "held = R.values\nopaque_callback(held)\n",
    ],
)
@pytest.mark.parametrize("reexport", (False, True))
def test_catalog_returned_config_roles_follow_retained_classes_through_copy_and_reexport(effect, reexport):
    units = _source_units(_CATALOG_ROLE_PATHS)
    path = "src/elspeth/catalog_config_role_probe.py"
    exported = fencing._parse_source(
        "src/elspeth/catalog_config_role_export.py",
        "from elspeth.plugins.sources.llm.config import SOURCE_PROVIDER_CONFIGS as RENAMED\n",
    )
    imports = (
        "from elspeth.catalog_config_role_export import RENAMED as R\n"
        if reexport
        else "from elspeth.plugins.sources.llm.config import SOURCE_PROVIDER_CONFIGS as R\n"
    )
    prefix = (*units, exported) if reexport else units
    unchanged = (*prefix, fencing._parse_source(path, imports))
    _catalog_role_require_original(unchanged, _catalog_role_receipt(unchanged, _catalog_role_producer(unchanged)))
    changed = (*prefix, fencing._parse_source(path, imports + effect))
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), path, "retained")
    _catalog_role_restore(unchanged)


@pytest.mark.parametrize("copied", (False, True))
def test_catalog_returned_config_roles_follow_actual_method_return_into_projected_class_mutation(copied):
    units = _source_units(_CATALOG_ROLE_PATHS)
    path = "src/elspeth/catalog_return_role_probe.py"
    returned = "LLMSource.discriminated_variants()[1]"
    expression = "dict(" + returned + ")" if copied else returned
    original = "from elspeth.plugins.sources.llm.source import LLMSource\nR = " + expression + "\n"
    unchanged = (*units, fencing._parse_source(path, original))
    _catalog_role_require_original(unchanged, _catalog_role_receipt(unchanged, _catalog_role_producer(unchanged)))
    changed = (*units, fencing._parse_source(path, original + "R['azure'].model_config['field_title_generator'] = foreign\n"))
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), path, "retained")
    _catalog_role_restore(unchanged)


@pytest.mark.parametrize("projection", ("default", "equals"))
def test_catalog_returned_config_roles_keep_mutable_metadata_origins_through_new_knob_dicts(projection):
    units = _source_units(_CATALOG_ROLE_PATHS)
    before = 'lookup: dict[str, Any] | None = Field(default=None, description="Lookup data loaded from YAML file")'
    if projection == "default":
        scalar = before
        retained = before.replace("default=None", 'default={"held": []}')
    else:
        scalar = before.replace(
            'description="Lookup data loaded from YAML file"',
            'description="Lookup data loaded from YAML file", json_schema_extra={"composer_required_when": {"field": "provider", "equals": "azure"}}',
        )
        retained = scalar.replace('"equals": "azure"', '"equals": {"held": []}')
    unchanged = _catalog_role_replace(units, _CATALOG_ROLE_CONFIG, before, scalar)
    _catalog_role_require_original(unchanged, _catalog_role_receipt(unchanged, _catalog_role_producer(unchanged)))
    changed = _catalog_role_replace(units, _CATALOG_ROLE_CONFIG, before, retained)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_CONFIG, "mutable " + projection)
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "effect",
    (
        "    opaque_callback(_field_info)\n",
        "    retained_fields.append(_field_info)\n",
        "    import typing\n    typing._SpecialForm.__getitem__ = replacement\n",
    ),
)
def test_catalog_returned_config_roles_refuse_title_callback_retention_or_protected_effect(effect):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    before = '    words = field_name.removesuffix("_config").replace("_", " ").split()'
    changed = _catalog_role_replace(units, _CATALOG_ROLE_BASE, before, effect + before)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_BASE, "callback")
    if "typing." in effect:
        base = _catalog_role_unit(changed, _CATALOG_ROLE_BASE)
        assert any(
            "typing supplier namespace mutation " + base.path in row for row in fencing._reserve_typing_supplier_mutation_failures((base,))
        )
    _catalog_role_restore(units)


def test_catalog_returned_config_roles_refuse_replaced_title_slot_with_unchanged_callback_body():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    before = '"field_title_generator": _plugin_config_field_title,'
    changed = _catalog_role_replace(units, _CATALOG_ROLE_BASE, before, '"field_title_generator": foreign_callback,')
    original_callback = fencing._reserve_node_at(_catalog_role_unit(units, _CATALOG_ROLE_BASE), "_plugin_config_field_title")
    changed_callback = fencing._reserve_node_at(_catalog_role_unit(changed, _CATALOG_ROLE_BASE), "_plugin_config_field_title")
    assert isinstance(original_callback, ast.FunctionDef) and isinstance(changed_callback, ast.FunctionDef)
    assert ast.dump(changed_callback, include_attributes=False) == ast.dump(original_callback, include_attributes=False)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_BASE, "callback binding")
    _catalog_role_restore(units)


def test_catalog_returned_config_roles_keep_value_validation_separate_from_json_schema():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    before = "    def _validate_prompt_template(cls, value: str) -> str:\n"
    after = before + "        import typing\n        typing._SpecialForm.__getitem__ = replacement\n"
    changed = _catalog_role_replace(units, _CATALOG_ROLE_CONFIG, before, after)
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_require_original(changed, receipt)
    config = _catalog_role_unit(changed, _CATALOG_ROLE_CONFIG)
    assert any(
        "typing supplier namespace mutation " + config.path in row for row in fencing._reserve_typing_supplier_mutation_failures((config,))
    )
    assert receipt["closed_consumers"] == frozenset()
    _catalog_role_restore(units)


def test_catalog_returned_config_roles_bind_original_owned_scalar_default_without_a_literal_value_whitelist():
    units = _source_units(_CATALOG_ROLE_PATHS)
    original_ids = _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(
        units,
        _CATALOG_ROLE_DEFAULTS,
        'OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"',
        'OPENROUTER_BASE_URL = "https://another.example/api/v2"',
    )
    _catalog_role_require_original(changed, _catalog_role_receipt(changed, _catalog_role_producer(changed)))
    _restored, receipt = _catalog_role_restore(units)
    assert original_ids.isdisjoint(_catalog_role_ids(receipt))


def test_catalog_returned_config_roles_refuse_foreign_scalar_companion_with_same_source_bytes():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    supplier = _catalog_role_unit(units, _CATALOG_ROLE_DEFAULTS)
    foreign = fencing._parse_source("src/elspeth/foreign_config_defaults.py", supplier.source)
    changed = tuple(foreign if unit is supplier else unit for unit in units)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_DEFAULTS, "module")
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "path,before,after,role",
    [
        (
            _CATALOG_ROLE_CONFIG,
            "from elspeth.plugins.llm.config_validation import (",
            "from foreign_defaults import (",
            "scalar default binding",
        ),
        (
            _CATALOG_ROLE_CONFIG,
            "    OPENROUTER_BASE_URL,",
            "    OPENROUTER_BASE_URL as foreign_default,",
            "scalar default binding",
        ),
        (
            _CATALOG_ROLE_CONFIG,
            "    OPENROUTER_BASE_URL,",
            "    OPENROUTER_BASE_URL,\n    OPENROUTER_BASE_URL,",
            "scalar default binding",
        ),
        (
            _CATALOG_ROLE_CONFIG,
            "default=OPENROUTER_BASE_URL",
            "default=opaque_default()",
            "default use",
        ),
        (
            _CATALOG_ROLE_CONFIG,
            "class OpenRouterLLMSourceConfig(LLMSourceConfig):",
            "class OpenRouterLLMSourceConfig(LLMSourceConfig):\n    OPENROUTER_BASE_URL = foreign_default",
            "scalar default binding",
        ),
        (
            _CATALOG_ROLE_DEFAULTS,
            'OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"',
            "OPENROUTER_BASE_URL = opaque_default()",
            "scalar default",
        ),
        (
            _CATALOG_ROLE_DEFAULTS,
            'OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"',
            "OPENROUTER_BASE_URL = {'held': []}",
            "scalar default",
        ),
        (
            _CATALOG_ROLE_DEFAULTS,
            'OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"',
            "OPENROUTER_BASE_URL = ()",
            "scalar default",
        ),
        (
            _CATALOG_ROLE_DEFAULTS,
            'OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"',
            "from foreign_defaults import OPENROUTER_BASE_URL",
            "scalar default binding",
        ),
        (
            _CATALOG_ROLE_DEFAULTS,
            'OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"',
            'OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"\nOPENROUTER_BASE_URL = foreign_default',
            "scalar default binding",
        ),
    ],
)
def test_catalog_returned_config_roles_refuse_unknown_scalar_default_supplier_binding_or_use(path, before, after, role):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(units, path, before, after)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), path, role)
    _catalog_role_restore(units)


def test_catalog_returned_config_roles_require_canonical_import_before_eager_default_use():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    config = _catalog_role_unit(units, _CATALOG_ROLE_CONFIG)
    imports = [
        statement
        for statement in config.tree.body
        if isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module == "elspeth.plugins.llm.config_validation"
    ]
    assert len(imports) == 1
    import_source = ast.get_source_segment(config.source, imports[0])
    assert import_source is not None and config.source.count(import_source + "\n") == 1
    changed_source = config.source.replace(import_source + "\n", "", 1) + "\n" + import_source + "\n"
    changed_config = fencing._parse_source(config.path, changed_source)
    changed = tuple(changed_config if unit is config else unit for unit in units)
    changed_import = changed_config.tree.body[-1]
    assert ast.dump(changed_import, include_attributes=False) == ast.dump(imports[0], include_attributes=False)
    default_use = _catalog_role_expression(changed_config, "OpenRouterLLMSourceConfig", "OPENROUTER_BASE_URL")
    assert changed_import.lineno > default_use.lineno
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), config.path, "scalar default binding")
    _catalog_role_restore(units)


def _catalog_r3_set_default(units, owner_name, value_source):
    enum = _catalog_role_unit(units, _CATALOG_ROLE_ENUM)
    field = fencing._reserve_node_at(enum, owner_name + ".response_format")
    assert isinstance(field, ast.AnnAssign)
    before = ast.get_source_segment(enum.source, field)
    assert before is not None
    return _catalog_role_replace(units, enum.path, before, "response_format: ResponseFormat = " + value_source)


def test_catalog_returned_config_observation_maps_require_original_retained_boundaries():
    units = _source_units(_CATALOG_ROLE_PATHS)
    receipt = _catalog_role_receipt(units, _catalog_role_producer(units))
    # Intentional first RED: missing observations fail by assertion, not KeyError.
    assert "deferred_retained_uses" in receipt, "catalog deferred retained-use observation map is absent"
    assert "class_info_operands" in receipt, "catalog class-info operand observation map is absent"
    _catalog_role_require_original(units, receipt)
    source = _catalog_role_unit(units, _CATALOG_ROLE_SOURCE)
    adapter = _catalog_role_expression(
        source, "LLMSource.get_config_schema", "TypeAdapter(Annotated[union_type, PydanticField(discriminator='provider')])"
    )
    assert any(
        failure == "typing supplier namespace transfer is unclosed " + source.path + ":" + str(adapter.lineno)
        for failure in fencing._reserve_typing_supplier_mutation_failures((source,))
    )


def test_catalog_returned_config_observations_refuse_missing_or_detached_original_identities():
    units = _source_units(_CATALOG_ROLE_PATHS)
    original = _catalog_role_receipt(units, _catalog_role_producer(units))
    original_ids = _catalog_role_require_original(units, original)
    for boundary in ("QueryDefinition.response_format", "QuerySpec.response_format"):
        missing = copy.deepcopy(original)
        del missing["deferred_retained_uses"][boundary]
        with pytest.raises(AssertionError):
            _catalog_role_require_original(units, missing)
    missing_tuple = copy.deepcopy(original)
    missing_tuple["class_info_operands"] = {}
    with pytest.raises(AssertionError):
        _catalog_role_require_original(units, missing_tuple)
    _restored_units, restored = _catalog_role_restore(units)
    assert original_ids.isdisjoint(_catalog_role_ids(restored))
    for field in ("deferred_retained_uses", "class_info_operands"):
        detached = copy.deepcopy(original)
        detached[field] = restored[field]
        with pytest.raises(AssertionError):
            _catalog_role_require_original(units, detached)
    assert _catalog_role_receipt(units, _catalog_role_producer(units)) == original


def test_catalog_returned_config_deferred_field_accepts_changed_literal_description_with_same_member_origin():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_r3_set_default(
        units, "QueryDefinition", "Field(default=ResponseFormat.STANDARD, description='A different ordinary literal description')"
    )
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_require_original(changed, receipt)
    enum = _catalog_role_unit(changed, _CATALOG_ROLE_ENUM)
    standard = fencing._reserve_node_at(enum, "ResponseFormat.STANDARD")
    assert receipt["deferred_retained_uses"]["QueryDefinition.response_format"]["enum_member"] == (enum.path, id(standard))
    _catalog_role_restore(units)


@pytest.mark.parametrize("owner_name", ("QueryDefinition", "QuerySpec"))
def test_catalog_returned_config_deferred_observations_bind_the_actual_changed_enum_member(owner_name):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    value = (
        "Field(default=ResponseFormat.STRUCTURED, description='An ordinary literal description')"
        if owner_name == "QueryDefinition"
        else "ResponseFormat.STRUCTURED"
    )
    changed = _catalog_r3_set_default(units, owner_name, value)
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_require_original(changed, receipt)
    enum = _catalog_role_unit(changed, _CATALOG_ROLE_ENUM)
    structured = fencing._reserve_node_at(enum, "ResponseFormat.STRUCTURED")
    standard = fencing._reserve_node_at(enum, "ResponseFormat.STANDARD")
    observation = receipt["deferred_retained_uses"][owner_name + ".response_format"]
    assert observation["enum_member"] == (enum.path, id(structured))
    assert observation["enum_member"] != (enum.path, id(standard))
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "value_source",
    (
        "Field(default=foreign_member, description='literal')",
        "Field(default=opaque_member(), description='literal')",
        "foreign_Field(default=ResponseFormat.STANDARD, description='literal')",
        "Field(ResponseFormat.STANDARD, description='literal')",
        "Field(description=ResponseFormat.STANDARD)",
        "Field(default=ResponseFormat.UNKNOWN, description='literal')",
        "Field(default=ResponseFormat.STANDARD, description=opaque_description())",
        "Field(default=ResponseFormat.STANDARD, description='literal', json_schema_extra=opaque_callback)",
        "Field(default=ResponseFormat.STANDARD, description='literal', default_factory=opaque_callback)",
        "Field(default=ResponseFormat.STANDARD, description='literal', unexpected=False)",
        "Field(default=ResponseFormat.STANDARD, description='literal', **foreign_kwargs)",
        "Field(*foreign_args, default=ResponseFormat.STANDARD, description='literal')",
    ),
)
def test_catalog_returned_config_deferred_field_refuses_unsupported_slot_callback_or_argument(value_source):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_r3_set_default(units, "QueryDefinition", value_source)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_ENUM, "deferred")
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "before,after",
    (
        (
            "from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator",
            "from pydantic import BaseModel, ConfigDict, field_validator, model_validator\nfrom foreign import Field",
        ),
        ("class QueryDefinition(BaseModel):", "class QueryDefinition(BaseModel):\n    Field = foreign_Field"),
        ("class QueryDefinition(BaseModel):", "Field = foreign_Field\n\nclass QueryDefinition(BaseModel):"),
        ("class QueryDefinition(BaseModel):", "class QueryDefinition(BaseModel, metaclass=Foreign):"),
        ("class QueryDefinition(BaseModel):", "@foreign_decorator\nclass QueryDefinition(BaseModel):"),
    ),
)
def test_catalog_returned_config_deferred_field_requires_original_canonical_binding_and_owner(before, after):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(units, _CATALOG_ROLE_ENUM, before, after)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_ENUM, "deferred")
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "value_source",
    (
        "foreign_member",
        "opaque_member()",
        "ResponseFormat.UNKNOWN",
        "Field(default=ResponseFormat.STANDARD)",
        "getattr(ResponseFormat, 'STANDARD')",
    ),
)
def test_catalog_returned_config_deferred_assignment_refuses_unknown_or_different_holder(value_source):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_r3_set_default(units, "QuerySpec", value_source)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_ENUM, "deferred")
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "before,after",
    (
        ("class QuerySpec:", "class QuerySpec(metaclass=Foreign):"),
        ("@dataclass(frozen=True, slots=True)\nclass QuerySpec:", "@foreign_decorator\nclass QuerySpec:"),
        (
            "from dataclasses import InitVar, dataclass, field",
            "from dataclasses import InitVar, field\nfrom foreign import dataclass",
        ),
    ),
)
def test_catalog_returned_config_deferred_assignment_requires_original_owner_and_decorator(before, after):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(units, _CATALOG_ROLE_ENUM, before, after)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_ENUM, "deferred")
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "after",
    (
        "response_format: foreign_type = ResponseFormat.STANDARD",
        "response_format = ResponseFormat.STANDARD",
        "response_format: ResponseFormat = ResponseFormat.STANDARD\n    response_format: ResponseFormat = ResponseFormat.STANDARD",
    ),
)
def test_catalog_returned_config_deferred_assignment_requires_unique_original_field_syntax(after):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(units, _CATALOG_ROLE_ENUM, "response_format: ResponseFormat = ResponseFormat.STANDARD", after)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_ENUM, "deferred")
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "effect",
    (
        "ResponseFormat.STANDARD._value_ = replacement\n",
        "held = ResponseFormat.STANDARD\nsetattr(held, '_value_', replacement)\n",
        "held_class = ResponseFormat\nheld_class.EXTRA = replacement\n",
        "def mutate():\n    held = ResponseFormat.STANDARD\n    held._value_ = replacement\n",
        "QueryDefinition.model_fields['response_format'].default._value_ = replacement\n",
        "held = QueryDefinition.model_fields['response_format'].default\nopaque_callback(held)\n",
        "QuerySpec.__dataclass_fields__['response_format'].default._value_ = replacement\n",
        "held_class = QuerySpec\nopaque_callback(held_class)\n",
        "box = opaque_holder(ResponseFormat.STANDARD)\n",
    ),
)
def test_catalog_returned_config_deferred_boundaries_keep_enum_holder_writes_and_aliases_live(effect):
    units = _source_units(_CATALOG_ROLE_PATHS)
    path = "src/elspeth/catalog_deferred_holder_probe.py"
    imports = "from elspeth.plugins.transforms.llm.multi_query import QueryDefinition, QuerySpec, ResponseFormat\n"
    unchanged = (*units, fencing._parse_source(path, imports))
    _catalog_role_require_original(unchanged, _catalog_role_receipt(unchanged, _catalog_role_producer(unchanged)))
    changed = (*units, fencing._parse_source(path, imports + effect))
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), path, "retained")
    _catalog_role_restore(unchanged)


_CATALOG_R3_CLASS_QUERY = "isinstance(self._config, (AzureOpenAILLMSourceConfig, BedrockLLMSourceConfig))"


@pytest.mark.parametrize(
    "query",
    (
        _CATALOG_R3_CLASS_QUERY,
        "isinstance(self._config, (AzureOpenAILLMSourceConfig,))",
        "issubclass(type(self._config), (AzureOpenAILLMSourceConfig, BedrockLLMSourceConfig))",
    ),
)
def test_catalog_returned_config_class_info_records_canonical_builtin_literal_tuple_without_operand_data_grant(query):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(units, _CATALOG_ROLE_SOURCE, _CATALOG_R3_CLASS_QUERY, query)
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_require_original(changed, receipt)
    assert receipt["closed_consumers"] == frozenset()
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "query",
    (
        "isinstance(self._config, ())",
        "isinstance(self._config, (foreign_class,))",
        "isinstance(self._config, (AzureOpenAILLMSourceConfig, foreign_class))",
        "isinstance(self._config, (*foreign_classes,))",
        "isinstance(self._config, tuple((AzureOpenAILLMSourceConfig, BedrockLLMSourceConfig)))",
        "isinstance(self._config, [AzureOpenAILLMSourceConfig, BedrockLLMSourceConfig])",
        "isinstance(self._config, (SOURCE_PROVIDER_CONFIGS['azure'], BedrockLLMSourceConfig))",
        "isinstance(self._config, (AzureOpenAILLMSourceConfig, BedrockLLMSourceConfig), extra)",
        "isinstance(self._config, (AzureOpenAILLMSourceConfig, BedrockLLMSourceConfig), classinfo=foreign)",
        "isinstance(*foreign_args, (AzureOpenAILLMSourceConfig, BedrockLLMSourceConfig))",
        "isinstance(self._config, (AzureOpenAILLMSourceConfig, BedrockLLMSourceConfig), **foreign_kwargs)",
    ),
)
def test_catalog_returned_config_class_info_refuses_unknown_leaf_computed_tuple_or_extra_argument(query):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(units, _CATALOG_ROLE_SOURCE, _CATALOG_R3_CLASS_QUERY, query)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_SOURCE, "class-info")
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "before,after",
    (
        ("class LLMSource(BaseSource):", "isinstance = foreign_check\n\nclass LLMSource(BaseSource):"),
        (
            "class LLMSource(BaseSource):",
            "class AzureOpenAILLMSourceConfig:\n    pass\n\nclass LLMSource(BaseSource):",
        ),
        (
            "    AzureOpenAILLMSourceConfig,",
            "    AzureOpenAILLMSourceConfig as foreign_config,",
        ),
    ),
)
def test_catalog_returned_config_class_info_requires_builtin_and_original_class_import_binding(before, after):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(units, _CATALOG_ROLE_SOURCE, before, after)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_SOURCE, "class-info")
    _catalog_role_restore(units)


def test_catalog_returned_config_class_info_refuses_an_enclosing_builtin_parameter_shadow():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    source = _catalog_role_unit(units, _CATALOG_ROLE_SOURCE)
    matches = [node for node in ast.walk(source.tree) if isinstance(node, ast.Call) and ast.unparse(node) == _CATALOG_R3_CLASS_QUERY]
    assert len(matches) == 1
    owners = [
        node for node in ast.walk(source.tree) if isinstance(node, ast.FunctionDef) and any(child is matches[0] for child in ast.walk(node))
    ]
    assert len(owners) == 1
    before = "def " + owners[0].name + "("
    changed = _catalog_role_replace(units, source.path, before, before + "isinstance, ")
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), source.path, "class-info")
    _catalog_role_restore(units)


def test_catalog_returned_config_class_info_refuses_foreign_config_metaclass():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(
        units,
        _CATALOG_ROLE_CONFIG,
        "class AzureOpenAILLMSourceConfig(LLMSourceConfig):",
        "class AzureOpenAILLMSourceConfig(LLMSourceConfig, metaclass=Foreign):",
    )
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_CONFIG, "ancestry")
    _catalog_role_restore(units)


def test_catalog_returned_config_class_info_keeps_nested_first_operand_retained_calls_visible():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(
        units,
        _CATALOG_ROLE_SOURCE,
        _CATALOG_R3_CLASS_QUERY,
        "isinstance(opaque_callback(AzureOpenAILLMSourceConfig), (AzureOpenAILLMSourceConfig, BedrockLLMSourceConfig))",
    )
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_SOURCE, "retained")
    _catalog_role_restore(units)


def _catalog_holder_constructor(units):
    enum = _catalog_role_unit(units, _CATALOG_ROLE_ENUM)
    producer = fencing._reserve_node_at(enum, "_query_spec_from_definition")
    assert isinstance(producer, ast.FunctionDef)
    returns = [node for node in producer.body if isinstance(node, ast.Return)]
    assert len(returns) == 1 and isinstance(returns[0].value, ast.Call)
    assert isinstance(returns[0].value.func, ast.Name) and returns[0].value.func.id == "QuerySpec"
    return enum, producer, returns[0], returns[0].value


def _catalog_holder_replace_return(units, replacement):
    enum, _producer, returned, _call = _catalog_holder_constructor(units)
    before = ast.get_source_segment(enum.source, returned)
    assert before is not None
    return _catalog_role_replace(units, enum.path, before, replacement)


def test_catalog_returned_config_holder_uses_keep_explicit_input_and_direct_return_unresolved():
    units = _source_units(_CATALOG_ROLE_PATHS)
    receipt = _catalog_role_receipt(units, _catalog_role_producer(units))
    _catalog_role_require_original(units, receipt)
    observations = receipt["deferred_holder_uses"]
    construction = observations["QuerySpec.construction_return"]
    assert construction["input_status"] == "UNRESOLVED"
    assert construction["result_status"] == "UNRESOLVED_HOLDER"
    assert "enum_member" not in construction
    declared_default = receipt["deferred_retained_uses"]["QuerySpec.response_format"]
    assert construction["response_format_input"] != declared_default["value"]
    assert construction["owner"] == declared_default["owner"]
    assert observations["QuerySpec.single_class_info"]["owner"] == construction["owner"]


def test_catalog_returned_config_holder_uses_require_all_original_components_and_fresh_identities():
    units = _source_units(_CATALOG_ROLE_PATHS)
    original = _catalog_role_receipt(units, _catalog_role_producer(units))
    original_ids = _catalog_role_require_original(units, original)
    for boundary in ("QuerySpec.construction_return", "QuerySpec.single_class_info"):
        missing = copy.deepcopy(original)
        del missing["deferred_holder_uses"][boundary]
        with pytest.raises(AssertionError):
            _catalog_role_require_original(units, missing)
        for component in original["deferred_holder_uses"][boundary]:
            incomplete = copy.deepcopy(original)
            del incomplete["deferred_holder_uses"][boundary][component]
            with pytest.raises(AssertionError):
                _catalog_role_require_original(units, incomplete)
    relabeled = copy.deepcopy(original)
    relabeled["deferred_holder_uses"]["QuerySpec.construction_return"]["input_status"] = "PROVED_DEFAULT_STANDARD"
    with pytest.raises(AssertionError):
        _catalog_role_require_original(units, relabeled)
    _restored_units, restored = _catalog_role_restore(units)
    assert original_ids.isdisjoint(_catalog_role_ids(restored))
    detached = copy.deepcopy(original)
    detached["deferred_holder_uses"] = restored["deferred_holder_uses"]
    with pytest.raises(AssertionError):
        _catalog_role_require_original(units, detached)
    assert _catalog_role_receipt(units, _catalog_role_producer(units)) == original


def test_catalog_returned_config_holder_explicit_argument_does_not_inherit_a_changed_declared_default():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    old_enum, old_producer, _old_return, _old_call = _catalog_holder_constructor(units)
    changed = _catalog_r3_set_default(units, "QuerySpec", "ResponseFormat.STRUCTURED")
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_require_original(changed, receipt)
    enum, producer, _returned, call = _catalog_holder_constructor(changed)
    assert ast.get_source_segment(enum.source, producer) == ast.get_source_segment(old_enum.source, old_producer)
    inputs = [keyword.value for keyword in call.keywords if keyword.arg == "response_format"]
    assert len(inputs) == 1 and ast.unparse(inputs[0]) == "definition.response_format"
    construction = receipt["deferred_holder_uses"]["QuerySpec.construction_return"]
    assert construction["response_format_input"] == (enum.path, id(inputs[0]))
    assert construction["input_status"] == "UNRESOLVED" and construction["result_status"] == "UNRESOLVED_HOLDER"
    assert "enum_member" not in construction
    structured = fencing._reserve_node_at(enum, "ResponseFormat.STRUCTURED")
    assert receipt["deferred_retained_uses"]["QuerySpec.response_format"]["enum_member"] == (enum.path, id(structured))
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "fault",
    (
        "foreign_callee",
        "opaque_input",
        "foreign_projection",
        "computed_input",
        "extra_keyword",
        "star_args",
        "star_kwargs",
        "wrapper",
        "container_return",
        "alias_return",
        "missing_return",
    ),
)
def test_catalog_returned_config_holder_constructor_refuses_unsupported_call_input_or_return(fault):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    _enum, _producer, _returned, call = _catalog_holder_constructor(units)
    constructor = ast.unparse(call)
    assert constructor.count("response_format=definition.response_format") == 1
    replacement = "return " + constructor
    if fault == "foreign_callee":
        replacement = "return " + constructor.replace("QuerySpec(", "foreign_QuerySpec(", 1)
    elif fault in {"opaque_input", "foreign_projection", "computed_input"}:
        value = {
            "opaque_input": "opaque_projection()",
            "foreign_projection": "foreign_definition.response_format",
            "computed_input": "opaque_projection(definition.response_format)",
        }[fault]
        replacement = "return " + constructor.replace("response_format=definition.response_format", "response_format=" + value, 1)
    elif fault == "extra_keyword":
        replacement = "return " + constructor[:-1] + ", unexpected=foreign)"
    elif fault == "star_args":
        replacement = "return " + constructor.replace("QuerySpec(", "QuerySpec(*foreign_args, ", 1)
    elif fault == "star_kwargs":
        replacement = "return " + constructor[:-1] + ", **foreign_kwargs)"
    elif fault == "wrapper":
        replacement = "return opaque_wrapper(" + constructor + ")"
    elif fault == "container_return":
        replacement = "return (" + constructor + ",)[0]"
    elif fault == "alias_return":
        replacement = "held = " + constructor + "\n    return held"
    elif fault == "missing_return":
        replacement = "raise RuntimeError('no holder return')"
    changed = _catalog_holder_replace_return(units, replacement)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_ENUM, "holder-use")
    _catalog_role_restore(units)


def test_catalog_returned_config_holder_constructor_requires_original_unshadowed_callee():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    _enum, _producer, _returned, call = _catalog_holder_constructor(units)
    changed = _catalog_holder_replace_return(units, "QuerySpec = foreign_constructor\n    return " + ast.unparse(call))
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_ENUM, "holder-use")
    _catalog_role_restore(units)


def test_catalog_returned_config_holder_constructor_keeps_other_argument_retained_calls_visible():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    _enum, _producer, _returned, call = _catalog_holder_constructor(units)
    constructor = ast.unparse(call)
    assert constructor.count("name=name") == 1
    nested = "opaque_callback(ResponseFormat.STANDARD)"
    changed = _catalog_holder_replace_return(units, "return " + constructor.replace("name=name", "name=" + nested, 1))
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_refused(receipt, _CATALOG_ROLE_ENUM, "retained")
    enum = _catalog_role_unit(changed, _CATALOG_ROLE_ENUM)
    effect = _catalog_role_expression(enum, "_query_spec_from_definition", nested)
    assert any(enum.path + ":" + str(effect.lineno) in failure and "retained" in failure for failure in receipt["failures"])
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "effect,observed_expression",
    (
        ("opaque_callback(held)", "opaque_callback(held)"),
        (
            "held.__class__.__dataclass_fields__['response_format'].default._value_ = replacement",
            "held.__class__.__dataclass_fields__['response_format'].default._value_",
        ),
        (
            "holder_class = held.__class__\n    holder_class.__dataclass_fields__['response_format'].default._value_ = replacement",
            "holder_class.__dataclass_fields__['response_format'].default._value_",
        ),
    ),
)
def test_catalog_returned_config_holder_constructor_alias_keeps_local_effect_origin(effect, observed_expression):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    _enum, _producer, _returned, call = _catalog_holder_constructor(units)
    replacement = "held = " + ast.unparse(call) + "\n    " + effect + "\n    return held"
    changed = _catalog_holder_replace_return(units, replacement)
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_refused(receipt, _CATALOG_ROLE_ENUM, "retained")
    enum = _catalog_role_unit(changed, _CATALOG_ROLE_ENUM)
    effect_node = _catalog_role_expression(enum, "_query_spec_from_definition", observed_expression)
    assert any(enum.path + ":" + str(effect_node.lineno) in failure and "retained" in failure for failure in receipt["failures"])
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "query",
    (
        "foreign_check(item, QuerySpec)",
        "isinstance(item, foreign_QuerySpec)",
        "isinstance(item, QuerySpec, extra)",
        "isinstance(item, QuerySpec, classinfo=foreign)",
        "isinstance(*foreign_args, QuerySpec)",
        "isinstance(item, QuerySpec, **foreign_kwargs)",
    ),
)
def test_catalog_returned_config_single_holder_check_refuses_foreign_binding_or_extra_argument(query):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(units, _CATALOG_ROLE_ENUM, "isinstance(item, QuerySpec)", query)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_ENUM, "holder-use")
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "before,after",
    (
        ("def resolve_queries(", "def resolve_queries(isinstance, "),
        ("    specs: list[QuerySpec] = []", "    QuerySpec = foreign_class\n    specs: list[QuerySpec] = []"),
        ("    specs: list[QuerySpec] = []", "    class QuerySpec:\n        pass\n    specs: list[QuerySpec] = []"),
    ),
)
def test_catalog_returned_config_single_holder_check_refuses_local_builtin_or_class_shadow(before, after):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(units, _CATALOG_ROLE_ENUM, before, after)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_ENUM, "holder-use")
    _catalog_role_restore(units)


def test_catalog_returned_config_single_holder_check_keeps_nested_first_operand_effect_visible():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    nested = "opaque_callback(ResponseFormat.STANDARD)"
    changed = _catalog_role_replace(units, _CATALOG_ROLE_ENUM, "isinstance(item, QuerySpec)", "isinstance(" + nested + ", QuerySpec)")
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_refused(receipt, _CATALOG_ROLE_ENUM, "retained")
    enum = _catalog_role_unit(changed, _CATALOG_ROLE_ENUM)
    effect = _catalog_role_expression(enum, "resolve_queries", nested)
    assert any(enum.path + ":" + str(effect.lineno) in failure and "retained" in failure for failure in receipt["failures"])
    _catalog_role_restore(units)


# Retained-traversal review oppositions; method repair is separately allocated.
@pytest.mark.parametrize("wrapper", ("conditional", "starred"), ids=("conditional", "starred"))
def test_catalog_returned_config_retained_transfer_wrappers_keep_original_registry_origin(wrapper):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    path = "src/elspeth/catalog_retained_transfer_probe.py"
    imports = "from elspeth.plugins.sources.llm.config import SOURCE_PROVIDER_CONFIGS as R\n"
    unchanged = (*units, fencing._parse_source(path, imports))
    _catalog_role_require_original(unchanged, _catalog_role_receipt(unchanged, _catalog_role_producer(unchanged)))
    effect = "opaque_callback(R if flag else R)" if wrapper == "conditional" else "opaque_callback(*[R])"
    changed = (*units, fencing._parse_source(path, imports + effect + "\n"))
    probe = _catalog_role_unit(changed, path)
    calls = [node for node in ast.walk(probe.tree) if isinstance(node, ast.Call) and ast.unparse(node) == effect]
    assert len(calls) == 1
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_refused(receipt, path, "retained")
    assert "catalog config retained call or opaque escape " + path + ":" + str(calls[0].lineno) in receipt["failures"]
    _catalog_role_restore(unchanged)


@pytest.mark.parametrize("slot", ("description", "extra"), ids=("description", "extra"))
def test_catalog_returned_config_in_slice_field_arguments_keep_nested_retained_effect_visible(slot):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    before = 'lookup: dict[str, Any] | None = Field(default=None, description="Lookup data loaded from YAML file")'
    ordinary = 'description="An ordinary changed lookup description"'
    positive = before.replace('description="Lookup data loaded from YAML file"', ordinary)
    if slot == "extra":
        positive = positive[:-1] + ', json_schema_extra={"composer_description": "An ordinary extra description"})'
    unchanged = _catalog_role_replace(units, _CATALOG_ROLE_CONFIG, before, positive)
    _catalog_role_require_original(unchanged, _catalog_role_receipt(unchanged, _catalog_role_producer(unchanged)))
    nested = "opaque_callback(ResponseFormat.STANDARD)"
    retained = (
        positive.replace(ordinary, "description=" + nested)
        if slot == "description"
        else positive.replace('"An ordinary extra description"', nested)
    )
    changed = _catalog_role_replace(unchanged, _CATALOG_ROLE_CONFIG, positive, retained)
    config = _catalog_role_unit(changed, _CATALOG_ROLE_CONFIG)
    field = fencing._reserve_node_at(config, "LLMSourceConfig.lookup")
    assert isinstance(field, ast.AnnAssign) and isinstance(field.value, ast.Call)
    original_field = fencing._reserve_node_at(_catalog_role_unit(units, _CATALOG_ROLE_CONFIG), "LLMSourceConfig.lookup")
    assert isinstance(original_field, ast.AnnAssign)
    assert ast.dump(field.annotation, include_attributes=False) == ast.dump(original_field.annotation, include_attributes=False)
    defaults = [keyword.value for keyword in field.value.keywords if keyword.arg == "default"]
    assert len(defaults) == 1 and isinstance(defaults[0], ast.Constant) and defaults[0].value is None
    effect = _catalog_role_expression(config, "LLMSourceConfig.lookup", nested)
    assert isinstance(effect, ast.Call)
    keywords = {keyword.arg: keyword.value for keyword in field.value.keywords}
    if slot == "description":
        assert keywords["description"] is effect
    else:
        extra = keywords["json_schema_extra"]
        assert isinstance(extra, ast.Dict) and len(extra.keys) == len(extra.values) == 1
        assert isinstance(extra.keys[0], ast.Constant) and extra.keys[0].value == "composer_description"
        assert extra.values[0] is effect
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_refused(receipt, config.path, "retained")
    assert "catalog config retained call or opaque escape " + config.path + ":" + str(effect.lineno) in receipt["failures"]
    _catalog_role_restore(units)


def _catalog_registration_call(units):
    source = _catalog_role_unit(units, _CATALOG_ROLE_SOURCE)
    calls = [
        statement.value
        for statement in source.tree.body
        if isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and isinstance(statement.value.func, ast.Name)
        and statement.value.func.id == "register_value_source_plugin"
    ]
    assert len(calls) == 1
    return source, calls[0]


def _catalog_role_registration_expected(units, premise, validation_premise):
    source, call = _catalog_registration_call(units)
    supplier = _catalog_role_unit(units, _CATALOG_ROLE_REGISTRATION)
    producer = _catalog_role_producer(units)
    registrar = fencing._reserve_node_at(supplier, "register_value_source_plugin")
    registry = fencing._reserve_node_at(supplier, "_VALUE_SOURCE_CONFIG_ATTRS")
    getter = fencing._reserve_node_at(source, "LLMSource.provider_config")
    assert isinstance(producer, ast.ClassDef) and isinstance(registrar, ast.FunctionDef)
    assert isinstance(registry, ast.AnnAssign) and isinstance(registry.value, ast.Dict) and not registry.value.keys
    assert isinstance(getter, ast.FunctionDef) and len(getter.decorator_list) == 1
    assert len(call.args) == len(call.keywords) == 1 and call.keywords[0].arg == "config_attr"
    assert isinstance(call.args[0], ast.Name) and call.args[0].id == producer.name
    assert isinstance(call.keywords[0].value, ast.Constant) and call.keywords[0].value.value == getter.name
    imports = [
        alias
        for statement in source.tree.body
        if isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module == "elspeth.contracts.value_source"
        for alias in statement.names
        if alias.name == registrar.name and (alias.asname or alias.name) == call.func.id
    ]
    assert len(imports) == 1 and len(registrar.args.args) == len(registrar.args.kwonlyargs) == 1
    plugin_parameter, attr_parameter = registrar.args.args[0], registrar.args.kwonlyargs[0]
    returns = [node for node in getter.body if isinstance(node, ast.Return)]
    assert len(returns) == 1 and isinstance(returns[0].value, ast.Attribute)
    memberships = [
        node for node in ast.walk(registrar) if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], ast.In)
    ]
    reads = [node for node in ast.walk(registrar) if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)]
    stores = [
        node
        for node in registrar.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Subscript)
    ]
    assert len(memberships) == len(reads) == len(stores) == 1
    store = stores[0]
    destination = store.targets[0]
    registry_uses = tuple(
        (supplier.path, id(node)) for node in ast.walk(registrar) if isinstance(node, ast.Name) and node.id == registry.target.id
    )

    def local(node):
        return supplier.path, id(node)

    def caller(node):
        return source.path, id(node)

    return {
        caller(call): {
            "call": caller(call),
            "callee_use": caller(call.func),
            "registrar_import": caller(imports[0]),
            "producer_argument": caller(call.args[0]),
            "producer": caller(producer),
            "config_attr_keyword": caller(call.keywords[0]),
            "config_attr_value": caller(call.keywords[0].value),
            "property": caller(getter),
            "property_decorator": caller(getter.decorator_list[0]),
            "property_parameter": caller(getter.args.args[0]),
            "property_return": caller(returns[0]),
            "property_projection": caller(returns[0].value),
            "registrar": local(registrar),
            "plugin_parameter": local(plugin_parameter),
            "config_attr_parameter": local(attr_parameter),
            "registry": local(registry),
            "registry_initializer": local(registry.value),
            "registry_uses": registry_uses,
            "membership": local(memberships[0]),
            "existing_read": local(reads[0]),
            "store": local(store),
            "destination": local(destination),
            "destination_receiver": local(destination.value),
            "key": local(destination.slice),
            "value": local(store.value),
            "argument_bindings": ((caller(call.args[0]), local(plugin_parameter)), (caller(call.keywords[0].value), local(attr_parameter))),
            "store_bindings": ((local(destination.slice), local(plugin_parameter)), (local(store.value), local(attr_parameter))),
            "retention_status": "STRONG_ON_NORMAL_COMPLETION",
            "outcome_status": "UNRESOLVED",
            "registry_state_status": "UNRESOLVED",
            "class_hash_effects_status": "UNRESOLVED",
            "descriptor_effects_status": "UNRESOLVED",
            "effects_status": "UNRESOLVED",
            "unresolved": premise,
            "config_validation_unresolved": validation_premise,
        }
    }


def test_catalog_returned_config_registration_requires_original_strong_retention_identities_and_four_open_premises():
    units = _source_units(_CATALOG_ROLE_PATHS)
    receipt = _catalog_role_receipt(units, _catalog_role_producer(units))
    original_ids = _catalog_role_require_original(units, receipt)
    source, call = _catalog_registration_call(units)
    observations = receipt["deferred_producer_registrations"]
    assert set(observations) == {(source.path, id(call))}
    record = observations[source.path, id(call)]
    assert record["retention_status"] == "STRONG_ON_NORMAL_COMPLETION"
    for field in ("outcome_status", "registry_state_status", "class_hash_effects_status", "descriptor_effects_status", "effects_status"):
        assert record[field] == "UNRESOLVED"
    assert record["unresolved"] == ("retained producer registration", source.path, id(call))
    assert record["unresolved"] in receipt["unresolved"] and len(receipt["unresolved"]) == 4
    missing = copy.deepcopy(receipt)
    del missing["deferred_producer_registrations"][source.path, id(call)]
    with pytest.raises(AssertionError):
        _catalog_role_require_original(units, missing)
    for component in record:
        missing = copy.deepcopy(receipt)
        del missing["deferred_producer_registrations"][source.path, id(call)][component]
        with pytest.raises(AssertionError):
            _catalog_role_require_original(units, missing)
    for field, fabricated in (
        ("retention_status", "WEAK"),
        ("outcome_status", "SUCCEEDED"),
        ("registry_state_status", "EMPTY"),
        ("effects_status", "PURE"),
    ):
        forged = copy.deepcopy(receipt)
        forged["deferred_producer_registrations"][source.path, id(call)][field] = fabricated
        with pytest.raises(AssertionError):
            _catalog_role_require_original(units, forged)
    _fresh_units, restored = _catalog_role_restore(units)
    assert original_ids.isdisjoint(_catalog_role_ids(restored))
    detached = copy.deepcopy(receipt)
    detached["deferred_producer_registrations"] = restored["deferred_producer_registrations"]
    with pytest.raises(AssertionError):
        _catalog_role_require_original(units, detached)
    assert _catalog_role_receipt(units, _catalog_role_producer(units)) == receipt


@pytest.mark.parametrize("fault", ("missing", "duplicate", "foreign"))
def test_catalog_returned_config_registration_requires_explicit_original_companion(fault):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    original = _catalog_role_unit(units, _CATALOG_ROLE_REGISTRATION)
    changed = tuple(unit for unit in units if unit is not original)
    if fault == "duplicate":
        changed = (*units, fencing._parse_source(original.path, original.source))
    elif fault == "foreign":
        changed = (*changed, fencing._parse_source("src/elspeth/contracts/foreign_value_source.py", original.source))
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), original.path, "module")
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "fault",
    (
        "foreign_import",
        "shadowed_callee",
        "foreign_callee",
        "wrong_producer",
        "wrong_attr",
        "opaque_attr",
        "extra_arg",
        "extra_kw",
        "star_args",
        "star_kwargs",
        "wrapper",
        "nested_scope",
        "duplicate_call",
    ),
)
def test_catalog_returned_config_registration_refuses_changed_import_call_or_argument(fault):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    source, call = _catalog_registration_call(units)
    before = ast.get_source_segment(source.source, call)
    assert before is not None
    canonical = 'register_value_source_plugin(LLMSource, config_attr="provider_config")'
    assert before == canonical
    after = {
        "shadowed_callee": "register_value_source_plugin = foreign_registrar\n" + canonical,
        "foreign_callee": canonical.replace("register_value_source_plugin(", "foreign_registrar("),
        "wrong_producer": canonical.replace("(LLMSource,", "(LLMSourceConfig,"),
        "wrong_attr": canonical.replace('"provider_config"', '"foreign_config"'),
        "opaque_attr": canonical.replace('"provider_config"', "opaque_attr()"),
        "extra_arg": canonical.replace("(LLMSource,", "(LLMSource, foreign,"),
        "extra_kw": canonical[:-1] + ", foreign=False)",
        "star_args": canonical.replace("(LLMSource,", "(*foreign_args,"),
        "star_kwargs": canonical[:-1] + ", **foreign_kwargs)",
        "wrapper": "opaque_wrapper(" + canonical + ")",
        "nested_scope": "def delayed_registration():\n    " + canonical,
        "duplicate_call": canonical + "\n" + canonical,
    }.get(fault)
    if fault == "foreign_import":
        before = "from elspeth.contracts.value_source import register_value_source_plugin"
        after = "from elspeth.contracts.foreign_value_source import register_value_source_plugin"
    assert after is not None
    changed = _catalog_role_replace(units, source.path, before, after)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), source.path, "registration")
    _catalog_role_restore(units)


@pytest.mark.parametrize("fault", ("getter", "projection", "decorator", "builtin_shadow", "receiver", "signature"))
def test_catalog_returned_config_registration_requires_original_property_relation(fault):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    source = _catalog_role_unit(units, _CATALOG_ROLE_SOURCE)
    if fault == "builtin_shadow":
        changed = _catalog_role_replace(
            units, source.path, "class LLMSource(BaseSource):", "property = foreign_property\n\nclass LLMSource(BaseSource):"
        )
        _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), source.path, "registration")
        _catalog_role_restore(units)
        return
    getter = fencing._reserve_node_at(source, "LLMSource.provider_config")
    assert isinstance(getter, ast.FunctionDef) and len(getter.decorator_list) == 1
    before = "".join(source.source.splitlines(keepends=True)[getter.decorator_list[0].lineno - 1 : getter.end_lineno])
    substitutions = {
        "getter": ("return self._config", "return opaque_callback(self._config)"),
        "projection": ("return self._config", "return self.foreign_config"),
        "decorator": ("@property", "@foreign_property"),
        "receiver": ("        self,", "        foreign_self,"),
        "signature": ("        self,", "        self, foreign_argument,"),
    }
    old, new = substitutions[fault]
    assert before.count(old) == 1
    changed = _catalog_role_replace(units, source.path, before, before.replace(old, new, 1))
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), source.path, "registration")
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "fault", ("plugin_parameter", "attr_parameter", "guard", "conflict", "destination", "key", "value", "class_escape", "registry_escape")
)
def test_catalog_returned_config_registration_requires_original_callee_signature_body_and_store(fault):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    supplier = _catalog_role_unit(units, _CATALOG_ROLE_REGISTRATION)
    registrar = fencing._reserve_node_at(supplier, "register_value_source_plugin")
    assert isinstance(registrar, ast.FunctionDef)
    before = ast.get_source_segment(supplier.source, registrar)
    assert before is not None
    store = "_VALUE_SOURCE_CONFIG_ATTRS[plugin_cls] = config_attr"
    substitutions = {
        "plugin_parameter": ("plugin_cls: type,", "foreign_cls: type,"),
        "attr_parameter": ("config_attr: str", "foreign_attr: str"),
        "guard": ("if not config_attr:", "if False:"),
        "conflict": ("existing != config_attr", "existing is not config_attr"),
        "destination": (store, "FOREIGN_REGISTRY[plugin_cls] = config_attr"),
        "key": (store, "_VALUE_SOURCE_CONFIG_ATTRS[foreign_cls] = config_attr"),
        "value": (store, "_VALUE_SOURCE_CONFIG_ATTRS[plugin_cls] = foreign_attr"),
        "class_escape": (store, "opaque_callback(plugin_cls)\n    " + store),
        "registry_escape": (store, "opaque_callback(_VALUE_SOURCE_CONFIG_ATTRS)\n    " + store),
    }
    old, new = substitutions[fault]
    assert before.count(old) == 1
    changed = _catalog_role_replace(units, supplier.path, before, before.replace(old, new, 1))
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), supplier.path, "registration")
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "replacement",
    (
        "_VALUE_SOURCE_CONFIG_ATTRS: dict[type, str] = foreign_registry",
        "_VALUE_SOURCE_CONFIG_ATTRS: dict[type, str] = opaque_registry()",
        "_VALUE_SOURCE_CONFIG_ATTRS: dict[type, str] = {}\n_VALUE_SOURCE_CONFIG_ATTRS = foreign_registry",
    ),
)
def test_catalog_returned_config_registration_requires_original_mutable_registry_binding(replacement):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    changed = _catalog_role_replace(units, _CATALOG_ROLE_REGISTRATION, "_VALUE_SOURCE_CONFIG_ATTRS: dict[type, str] = {}", replacement)
    _catalog_role_refused(_catalog_role_receipt(changed, _catalog_role_producer(changed)), _CATALOG_ROLE_REGISTRATION, "registration")
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "effect,observed_expression",
    (
        ("held[LLMSource] = 'foreign'", "held[LLMSource]"),
        ("held.clear()", "held.clear()"),
        ("held.update({LLMSource: 'foreign'})", "held.update({LLMSource: 'foreign'})"),
        ("opaque_callback(held)", "opaque_callback(held)"),
        ("getattr(held, 'clear')()", "getattr(held, 'clear')"),
        ("getattr(held, '__setitem__')(LLMSource, 'foreign')", "getattr(held, '__setitem__')"),
        ("held.__class__.clear = foreign", "held.__class__.clear"),
    ),
)
def test_catalog_returned_config_registration_registry_aliases_keep_other_effect_sites_live(effect, observed_expression):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    path = "src/elspeth/catalog_registration_probe.py"
    imports = "from elspeth.contracts.value_source import _VALUE_SOURCE_CONFIG_ATTRS as REGISTRY\nfrom elspeth.plugins.sources.llm.source import LLMSource\nheld = REGISTRY\n"
    unchanged = (*units, fencing._parse_source(path, imports))
    _catalog_role_require_original(unchanged, _catalog_role_receipt(unchanged, _catalog_role_producer(unchanged)))
    changed = (*units, fencing._parse_source(path, imports + effect + "\n"))
    probe = _catalog_role_unit(changed, path)
    matches = [node for node in ast.walk(probe.tree) if isinstance(node, ast.expr) and ast.unparse(node) == observed_expression]
    assert len(matches) == 1
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_refused(receipt, path, "retained")
    assert any(
        failure.startswith("catalog config retained ")
        and (failure.endswith(" " + path + ":" + str(matches[0].lineno)) or (" " + path + ":" + str(matches[0].lineno) + " ") in failure)
        for failure in receipt["failures"]
    )
    _catalog_role_restore(unchanged)


def test_catalog_returned_config_registration_companion_reader_is_not_an_ignored_module():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    supplier = _catalog_role_unit(units, _CATALOG_ROLE_REGISTRATION)
    before = "    for cls in type(plugin).__mro__:\n"
    nested = "opaque_callback(_VALUE_SOURCE_CONFIG_ATTRS)"
    changed = _catalog_role_replace(units, supplier.path, before, "    " + nested + "\n" + before)
    changed_supplier = _catalog_role_unit(changed, supplier.path)
    effect = _catalog_role_expression(changed_supplier, "find_value_source_config", nested)
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_refused(receipt, supplier.path, "retained")
    assert "catalog config retained call or opaque escape " + supplier.path + ":" + str(effect.lineno) in receipt["failures"]
    _catalog_role_restore(units)


def test_catalog_returned_config_registration_keeps_nested_argument_effect_or_explicit_argument_refusal():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    source, call = _catalog_registration_call(units)
    before = ast.get_source_segment(source.source, call)
    assert before is not None
    nested = "opaque_callback(LLMSource)"
    changed = _catalog_role_replace(
        units, source.path, before, "register_value_source_plugin(\n    LLMSource,\n    config_attr=" + nested + ",\n)"
    )
    changed_source, changed_call = _catalog_registration_call(changed)
    effects = [node for node in ast.walk(changed_call) if isinstance(node, ast.Call) and ast.unparse(node) == nested]
    assert len(effects) == 1 and effects[0].lineno != changed_call.lineno
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    retained = "catalog config retained call or opaque escape " + changed_source.path + ":" + str(effects[0].lineno)
    argument = "catalog config registration argument " + changed_source.path + ":" + str(changed_call.lineno)
    assert retained in receipt["failures"] or any(failure.startswith(argument) for failure in receipt["failures"]), receipt["failures"]
    _catalog_role_refused(receipt, source.path, "retained" if retained in receipt["failures"] else "registration argument")
    _catalog_role_restore(units)


def _catalog_identity_validation_call(units):
    config = _catalog_role_unit(units, _CATALOG_ROLE_CONFIG)
    owner = fencing._reserve_node_at(config, "GatewayLLMSourceConfig._validate_structured_output_capability")
    assert isinstance(owner, ast.FunctionDef)
    calls = [
        node
        for node in ast.walk(owner)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "validate_gateway_single_prompt_structured_output_capability"
    ]
    assert len(calls) == 1 and len(calls[0].args) == 2
    return config, owner, calls[0]


@pytest.mark.parametrize("operator", ("is", "is not"))
def test_catalog_returned_config_identity_boolean_result_preserves_original_conditional_contract(operator):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    config, _owner, call = _catalog_identity_validation_call(units)
    assert isinstance(call.args[0], ast.Compare) and len(call.args[0].ops) == 1 and isinstance(call.args[0].ops[0], ast.Is)
    before = ast.get_source_segment(config.source, call.args[0])
    assert before == "self.response_format is ResponseFormat.STRUCTURED"
    anchor = ast.get_source_segment(config.source, call)
    assert anchor is not None and config.source.count(anchor) == 1 and anchor.count(before) == 1
    changed = _catalog_role_replace(
        units, config.path, anchor, anchor.replace(before, "self.response_format " + operator + " ResponseFormat.STRUCTURED", 1)
    )
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_require_original(changed, receipt)
    assert receipt["closed_consumers"] == frozenset()
    _catalog_role_restore(units)


@pytest.mark.parametrize("operator", ("is", "is not"))
def test_catalog_returned_config_identity_boolean_does_not_hide_nested_retained_operand_effect(operator):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    config, _owner, call = _catalog_identity_validation_call(units)
    before = ast.get_source_segment(config.source, call.args[0])
    assert before == "self.response_format is ResponseFormat.STRUCTURED"
    nested = "opaque_callback(ResponseFormat.STRUCTURED)"
    anchor = ast.get_source_segment(config.source, call)
    assert anchor is not None and config.source.count(anchor) == 1 and anchor.count(before) == 1
    changed = _catalog_role_replace(
        units, config.path, anchor, anchor.replace(before, "self.response_format " + operator + " " + nested, 1)
    )
    changed_config, owner, outer = _catalog_identity_validation_call(changed)
    effects = [node for node in ast.walk(owner) if isinstance(node, ast.Call) and ast.unparse(node) == nested]
    assert len(effects) == 1 and effects[0].lineno != outer.lineno
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_refused(receipt, changed_config.path, "retained")
    assert "catalog config retained call or opaque escape " + changed_config.path + ":" + str(effects[0].lineno) in receipt["failures"]
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "replacement",
    (
        "self.response_format == ResponseFormat.STRUCTURED",
        "self.response_format != ResponseFormat.STRUCTURED",
        "self.response_format is ResponseFormat.STRUCTURED is self.response_format",
        "ResponseFormat.STRUCTURED",
    ),
)
def test_catalog_returned_config_identity_rule_does_not_grant_other_comparisons_or_retained_transfers(replacement):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    config, _owner, call = _catalog_identity_validation_call(units)
    before = ast.get_source_segment(config.source, call.args[0])
    assert before == "self.response_format is ResponseFormat.STRUCTURED"
    anchor = ast.get_source_segment(config.source, call)
    assert anchor is not None and config.source.count(anchor) == 1 and anchor.count(before) == 1
    changed = _catalog_role_replace(units, config.path, anchor, anchor.replace(before, replacement, 1))
    changed_config, _owner, outer = _catalog_identity_validation_call(changed)
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_refused(receipt, changed_config.path, "retained")
    assert "catalog config retained call or opaque escape " + changed_config.path + ":" + str(outer.lineno) in receipt["failures"]
    _catalog_role_restore(units)


@pytest.mark.parametrize(
    "event",
    (
        "provider_config = foreign_property",
        "del provider_config",
        "from foreign import value as provider_config",
        "def provider_config(self):\n            return foreign_config",
    ),
)
def test_catalog_returned_config_registration_refuses_conditional_getter_slot_overwrite_with_original_body(event):
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    source = _catalog_role_unit(units, _CATALOG_ROLE_SOURCE)
    getter = fencing._reserve_node_at(source, "LLMSource.provider_config")
    assert isinstance(getter, ast.FunctionDef)
    before = ast.get_source_segment(source.source, getter)
    assert before is not None
    changed = _catalog_role_replace(units, source.path, before, before + "\n\n    if flag:\n        " + event)
    changed_source = _catalog_role_unit(changed, source.path)
    unchanged_getter = fencing._reserve_node_at(changed_source, "LLMSource.provider_config")
    assert isinstance(unchanged_getter, ast.FunctionDef)
    assert ast.get_source_segment(changed_source.source, unchanged_getter) == before
    assert [ast.dump(node, include_attributes=False) for node in getter.decorator_list] == [
        ast.dump(node, include_attributes=False) for node in unchanged_getter.decorator_list
    ]
    receipt = _catalog_role_receipt(changed, _catalog_role_producer(changed))
    _catalog_role_refused(receipt, source.path, "registration property")
    _catalog_role_restore(units)


def test_catalog_returned_config_registration_getter_slot_binding_excludes_unrelated_method_locals():
    units = _source_units(_CATALOG_ROLE_PATHS)
    _catalog_role_require_original(units, _catalog_role_receipt(units, _catalog_role_producer(units)))
    source = _catalog_role_unit(units, _CATALOG_ROLE_SOURCE)
    getter = fencing._reserve_node_at(source, "LLMSource.provider_config")
    assert isinstance(getter, ast.FunctionDef)
    before = ast.get_source_segment(source.source, getter)
    assert before is not None
    local = "\n\n    def unrelated_slot_local(self):\n        provider_config = foreign_property\n        return provider_config"
    changed = _catalog_role_replace(units, source.path, before, before + local)
    _catalog_role_require_original(changed, _catalog_role_receipt(changed, _catalog_role_producer(changed)))
    _catalog_role_restore(units)
