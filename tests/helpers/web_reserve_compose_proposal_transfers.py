"""Finite Continuation/proposal AST transfers, conditional on Common custody.

The retired freeform turn, compose/compose_recovery and moved finalizer have
no receipts here. Current driver constructor proofs and manual proposal child
ownership replace those transfers; recovery text belongs to current recipes.
"""

import ast
import copy
import hashlib
import json

from tests.helpers import web_reserve_proposal_child_transfers as _supplier_sources
from tests.helpers.web_reserve_provider_mint_contract import provider_driver_constructor_contract

DRIVER = "src/elspeth/web/sessions/continuation_driver.py"
PHASES = "src/elspeth/web/composer/continuation_live_phases.py"
PROPOSALS = "src/elspeth/web/sessions/routes/composer/proposals.py"
SETTLEMENT = "src/elspeth/web/sessions/routes/composer/pipeline_settlement.py"
RECOVERY_MESSAGES = "src/elspeth/web/composer/continuation_message_builders.py"
RECOVERY_PRODUCERS = "src/elspeth/web/composer/continuation_prompt_producers.py"
RECOVERY_RECIPES = "src/elspeth/web/composer/continuation_prompt_recipes.py"
RECOVERY_INSTRUCTION_ORIGINS = frozenset(
    {
        "elspeth.web.composer.continuation_message_builders.recovery_instruction_message",
        "elspeth.contracts.prompt_guard.PROMPT_GUARD_TRUST_KEY",
        "elspeth.contracts.prompt_guard.PromptGuardTrust",
    }
)
RECOVERY_INSTRUCTION_FUNCTION_ORIGINS = frozenset({"elspeth.web.composer.continuation_message_builders.recovery_instruction_message"})
RECOVERY_INSTRUCTION_CLASS_ORIGINS = frozenset({"elspeth.contracts.prompt_guard.PromptGuardTrust"})

_RECOVERY_INSTRUCTION_SOURCE = """def recovery_instruction_message() -> dict[str, Any]:
    return {
        "role": "system",
        PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.INSTRUCTION,
        "content": (
            "The saved graph could not be decoded. Its payload is unavailable and must not be inferred. "
            "Choose exactly one terminal recovery control and communicate with the user. "
            "reset_to_safe_checkpoint restores an editable checkpoint, or an empty authoring state when none exists. "
            "Saving or capturing the broken graph is not required. Do not claim recovery succeeded before the "
            "server receipt. Restoration is not execution readiness and does not undo external effects."
        ),
    }
"""

# Existing reviewed proposal-body pins are retained unchanged. No new driver,
# service, recovery or whole-file hash is invented to waive a changed program.
_REVIEWED_METHOD_SHAPES = {
    PROPOSALS: "23ef777eef8f72ed82e5943f633d94fdaa6d4f76cab9bd7ef9ec642adc59a8ca",
    SETTLEMENT: "7c7465be32aeab7821c08a1a511541f23bb394aa39cd756b2f2215cda3cde599",
}


def _node(unit, path):
    body = unit.tree.body
    current = None
    for part in path.split("."):
        matches = [node for node in body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == part]
        if len(matches) != 1:
            return None
        current = matches[0]
        body = current.body
    return current


def _shape(node):
    return None if node is None else ast.dump(node, include_attributes=False)


def _exact(node, source):
    return _shape(node) == _shape(ast.parse(source).body[0])


def _canonical_ast(value):
    if isinstance(value, ast.AST):
        if isinstance(value, ast.Call):
            value = copy.copy(value)
            value.keywords = [
                keyword
                for keyword in value.keywords
                if not (keyword.arg is None and isinstance(keyword.value, ast.Dict) and not keyword.value.keys)
            ]
        return [
            type(value).__name__,
            [[name, _canonical_ast(field)] for name, field in ast.iter_fields(value) if field is not None and field != []],
        ]
    if isinstance(value, (list, tuple)):
        return [_canonical_ast(item) for item in value]
    if value is Ellipsis:
        return {"literal_type": "ellipsis"}
    return value


def _method_shape(method):
    header = copy.copy(method)
    header.body = list(method.body)
    if (
        header.body
        and isinstance(header.body[0], ast.Expr)
        and isinstance(header.body[0].value, ast.Constant)
        and type(header.body[0].value.value) is str
    ):
        header.body[0] = ast.Expr(value=ast.Constant(value="<inert docstring>"))
    payload = json.dumps(_canonical_ast(header), sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _lexical_bindings(method, name):
    bound = []
    for node in ast.walk(method):
        if (
            (isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, (ast.Store, ast.Del)))
            or (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node is not method and node.name == name)
            or (isinstance(node, ast.arg) and node.arg == name)
        ):
            bound.append(node)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.extend(alias for alias in node.names if (alias.asname or alias.name.split(".")[0]) == name)
        elif (
            (isinstance(node, ast.ExceptHandler) and node.name == name)
            or (isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name)
            or (isinstance(node, ast.MatchMapping) and node.rest == name)
            or (isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names)
            or (isinstance(node, (ast.TypeVar, ast.TypeVarTuple, ast.ParamSpec)) and node.name == name)
        ):
            bound.append(node)
    return bound


def _calls(method, name):
    """Only executable calls, excluding nested/deferred bodies and annotations."""

    def walk(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            if node is method:
                for statement in node.body:
                    yield from walk(statement)
            return
        if isinstance(node, ast.TypeAlias):
            return
        yield node
        children = ([node.value] if node.value is not None else []) if isinstance(node, ast.AnnAssign) else ast.iter_child_nodes(node)
        for child in children:
            yield from walk(child)

    return [node for node in walk(method) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name]


def _index(units, required):
    selected = {}
    for unit in units:
        if unit.path in required:
            if unit.path in selected:
                return None
            selected[unit.path] = unit
    return selected if set(selected) == set(required) else None


def _import(unit, name, module):
    return _supplier_sources.import_origin(unit.tree, name, module, name)


def compose_proposal_local_transfers(units):
    """Actual driver constructors and the still-live manual proposal transfer.

    The existing receiver receipt proves _job/segment entry, app services and
    phase binding. The shared closed driver-constructor receipt proves nominal
    work/provider ownership here; it does not authorize arbitrary call sites.
    """
    selected = _index(units, {DRIVER, PHASES, PROPOSALS, SETTLEMENT})
    if selected is None:
        return ["composer transfer current source missing or duplicated"], None
    failures, driver = provider_driver_constructor_contract(units)
    if failures:
        return failures, None
    init = _node(selected[DRIVER], "_SegmentDriver.__init__")
    phases = _node(selected[PHASES], "ContinuationLivePhases")
    accept = _node(selected[PROPOSALS], "accept_composition_proposal")
    settle = _node(selected[SETTLEMENT], "settle_pipeline_proposal_under_compose_lock")
    if any(node is None for node in (init, phases, accept, settle)):
        return ["composer transfer current declaration missing"], None
    for path, method in ((PROPOSALS, accept), (SETTLEMENT, settle)):
        if _method_shape(method) != _REVIEWED_METHOD_SHAPES[path]:
            return ["composer complete executed method grammar changed " + path], None
    if not _supplier_sources.owned_origin(selected[PHASES].tree, "ContinuationLivePhases", phases):
        return ["composer current phase supplier binding changed"], None
    if phases.bases or phases.keywords or phases.type_params or len(phases.decorator_list) != 1:
        return ["composer current phase constructor changed"], None
    if _shape(phases.decorator_list[0]) != _shape(ast.parse("dataclass(slots=True)", mode="eval").body):
        return ["composer current phase constructor changed"], None
    fields = ast.parse(
        "service: ComposerServiceImpl\nauthority: ComposerAsyncOperationAuthority\n"
        "work: RequiredWorkBinding\nprovider_owner: ProviderInvocationOwner\nlease: SessionOperationLease"
    ).body
    if len(phases.body) < len(fields) or any(_shape(left) != _shape(right) for left, right in zip(phases.body, fields, strict=False)):
        return ["composer current phase positional ownership schema changed"], None
    if any(
        _lexical_bindings(phases, name)
        for name in ("__init__", "__post_init__", "__new__", "__getattribute__", "__getattr__", "__setattr__", "__delattr__")
    ):
        return ["composer current phase custom constructor or lookup unproved"], None
    for name in ("service", "authority", "work", "provider_owner", "lease"):
        if any(
            _lexical_bindings(statement, name)
            for statement in phases.body[len(fields) :]
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            return ["composer current phase constructor field rebound"], None
    origins = []
    imports = (
        (DRIVER, "ProviderInvocationOwner", "elspeth.web.composer.provider_quota"),
        (DRIVER, "RequiredWorkBinding", "elspeth.web.required_work"),
        (DRIVER, "ContinuationLivePhases", "elspeth.web.composer.continuation_live_phases"),
        (PHASES, "dataclass", "dataclasses"),
        (PROPOSALS, "settle_pipeline_proposal_under_compose_lock", "elspeth.web.sessions.routes.composer.pipeline_settlement"),
        (PROPOSALS, "RequiredWorkCoordinator", "elspeth.web.required_work"),
        (PROPOSALS, "RequiredWorkAuthority", "elspeth.web.required_work"),
        (PROPOSALS, "RequiredWorkBinding", "elspeth.web.required_work"),
        (PROPOSALS, "RequiredAuthorityKind", "elspeth.web.required_work"),
        (PROPOSALS, "RequiredWorkRole", "elspeth.web.required_work"),
        (SETTLEMENT, "RequiredWorkCoordinator", "elspeth.web.required_work"),
        (SETTLEMENT, "RequiredWorkBinding", "elspeth.web.required_work"),
    )
    for path, name, module in imports:
        origin = _import(selected[path], name, module)
        if origin is None:
            return ["composer transfer canonical supplier changed " + name], None
        origins.append((path, id(origin[1])))
    if not _supplier_sources.owned_origin(selected[SETTLEMENT].tree, "settle_pipeline_proposal_under_compose_lock", settle):
        return ["proposal settlement supplier declaration rebound"], None
    proposal_calls = _calls(accept, "settle_pipeline_proposal_under_compose_lock")
    owner_calls = _calls(init, "ProviderInvocationOwner")
    phase_calls = _calls(init, "ContinuationLivePhases")
    if len(proposal_calls) != 1 or len(owner_calls) != 1 or len(phase_calls) != 1:
        return ["composer current transfer call universe changed"], None
    if _lexical_bindings(accept, "settle_pipeline_proposal_under_compose_lock"):
        return ["proposal settlement canonical caller shadowed"], None
    if not _exact(
        accept.body[2].body[5].body[0],
        "required_work = RequiredWorkCoordinator(RequiredWorkAuthority(RequiredAuthorityKind.MANUAL_PROPOSAL, "
        "lease.context, proposal_id=str(proposal.id), invocation_id=str(uuid4()), tool_call_id=proposal.tool_call_id))",
    ) or not _exact(accept.body[2].body[5].body[1], "lease.bind_required_work(required_work)"):
        return ["proposal exact actual authority and lease binding changed"], None
    constructors = {
        "provider_owner": (DRIVER, id(owner_calls[0])),
        "live_phases": (DRIVER, id(phase_calls[0])),
    }
    return [], {
        "validated_transfer_nodes": frozenset({*constructors.values(), (PROPOSALS, id(proposal_calls[0]))}),
        "constructor_calls": constructors,
        "proposal_call": (PROPOSALS, id(proposal_calls[0])),
        "driver_constructor": driver,
        "source_origins": tuple(origins),
        "supplier_definitions": ((DRIVER, id(init)), (PHASES, id(phases)), (SETTLEMENT, id(settle))),
        "common_unknown": (
            "actual worker/driver entry and app-services receiver receipt",
            "runtime class, dataclass, import and callable identity",
            "current actor, SQL admission, lease and proposal authority",
            "outside writes, descriptor effects and intervening callbacks",
            "phase runtime binding and provider invocation admission",
        ),
    }


def recovery_instruction_supplier_contracts(units):
    """Actual literal supplier and two current recipe calls, never V1 recovery."""
    selected = _index(units, {RECOVERY_MESSAGES, RECOVERY_PRODUCERS, RECOVERY_RECIPES})
    if selected is None:
        return ["recovery instruction supplier: current source missing or duplicated"], None
    source, consumer = selected[RECOVERY_MESSAGES], selected[RECOVERY_PRODUCERS]
    recipes = selected[RECOVERY_RECIPES]
    recipe = _node(recipes, "RecoveryInstructionRecipe")
    if (
        recipe is None
        or not _exact(
            recipe,
            "class RecoveryInstructionRecipe(ContinuationValue):\n    kind: Literal['recovery_instruction'] = 'recovery_instruction'",
        )
        or not _supplier_sources.owned_origin(recipes.tree, "RecoveryInstructionRecipe", recipe)
    ):
        return ["recovery instruction supplier: closed recipe schema changed"], None
    method = _node(source, "recovery_instruction_message")
    producer = _node(consumer, "CurrentContinuationMessageProducers.recovery_instruction")
    rebuild = _node(consumer, "CurrentContinuationMessageProducers._rebuild_shelf")
    restore = _node(consumer, "CurrentContinuationMessageProducers.restore_shelf")
    register = _node(consumer, "CurrentContinuationMessageProducers._register")
    if any(node is None for node in (method, producer, rebuild, restore, register)):
        return ["recovery instruction supplier: current producer declaration missing"], None
    if not _exact(method, _RECOVERY_INSTRUCTION_SOURCE) or not _supplier_sources.owned_origin(
        source.tree, "recovery_instruction_message", method
    ):
        return ["recovery instruction supplier: complete producer changed"], None
    for unit in (source, consumer):
        if _import(unit, "annotations", "__future__") is None:
            return ["recovery instruction supplier: postponed annotations required"], None
    origins = []
    for unit, name, module in (
        (source, "PROMPT_GUARD_TRUST_KEY", "elspeth.contracts.prompt_guard"),
        (source, "PromptGuardTrust", "elspeth.contracts.prompt_guard"),
        (consumer, "recovery_instruction_message", "elspeth.web.composer.continuation_message_builders"),
        (consumer, "RecoveryInstructionRecipe", "elspeth.web.composer.continuation_prompt_recipes"),
        (recipes, "ContinuationValue", "elspeth.web.composer.continuation"),
        (recipes, "Literal", "typing"),
    ):
        origin = _import(unit, name, module)
        if origin is None:
            return ["recovery instruction supplier: canonical dependency changed " + name], None
        origins.append((unit.path, id(origin[1])))
    if not _exact(
        producer,
        "def recovery_instruction(self) -> dict[str, Any]:\n"
        "    return self._register(recovery_instruction_message(), RecoveryInstructionRecipe())",
    ):
        return ["recovery instruction supplier: actual recipe issuance changed"], None
    if not _exact(
        restore,
        "def restore_shelf(self, snapshot: MessageShelfSnapshot) -> list[dict[str, Any]]:\n"
        "    inputs = self._require_current()\n"
        "    restored = self._rebuild_shelf(snapshot, inputs=inputs)\n"
        "    self._require_current()\n"
        "    return restored",
    ):
        return ["recovery instruction supplier: current reconstruction brackets changed"], None
    if not _exact(
        register,
        "def _register(self, message: dict[str, Any], recipe: ContinuationMessageRecipe) -> dict[str, Any]:\n"
        "    if type(self.inputs) is _ReadyProducerInputs and type(recipe) is not InitialMessageRecipe:\n"
        "        raise AuditIntegrityError('ready message producer admits only its actual initial-message family')\n"
        "    if any(existing is message for existing, _, _ in self._produced) or any(\n"
        "        item.message is message for item in self._feedback_produced):\n"
        "        raise AuditIntegrityError('message producer registered the same actual object twice')\n"
        "    self._produced.append((message, recipe, _wire(message)))\n"
        "    return message",
    ):
        return ["recovery instruction supplier: exact object/recipe registration changed"], None
    if (
        len(rebuild.body) < 4
        or not _exact(
            rebuild.body[2],
            "if type(inputs) is not _CurrentProducerInputs or inputs != self.inputs:\n"
            "    raise AuditIntegrityError('shelf assembly changed its actual producer inputs')",
        )
        or not _exact(
            rebuild.body[3],
            "if type(snapshot) is not MessageShelfSnapshot or encode_message_shelf(snapshot).canonical != inputs.evidence_messages_json:\n"
            "    raise AuditIntegrityError('message reconstruction changed the complete selected transcript shelf')",
        )
    ):
        return ["recovery instruction supplier: exact selected shelf admission changed"], None
    branches = [
        node
        for node in ast.walk(rebuild)
        if isinstance(node, ast.If)
        and _shape(node.test) == _shape(ast.parse("type(recipe) is RecoveryInstructionRecipe", mode="eval").body)
    ]
    if len(branches) != 1 or len(branches[0].body) != 1 or not _exact(branches[0].body[0], "current = recovery_instruction_message()"):
        return ["recovery instruction supplier: exact recovery reconstruction branch changed"], None
    calls = (*_calls(producer, "recovery_instruction_message"), *_calls(rebuild, "recovery_instruction_message"))
    all_calls = [
        node
        for node in ast.walk(consumer.tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "recovery_instruction_message"
    ]
    if len(calls) != 2 or set(calls) != set(all_calls) or any(call.args or call.keywords for call in calls):
        return ["recovery instruction supplier: exact current call universe changed"], None
    if any(_lexical_bindings(node, "recovery_instruction_message") for node in (producer, rebuild)):
        return ["recovery instruction supplier: consumer origin shadowed"], None
    if _supplier_sources._executed_protected_loads(
        source.tree, {"recovery_instruction_message", "PROMPT_GUARD_TRUST_KEY", "PromptGuardTrust"}
    ):
        return ["recovery instruction supplier: evaluated producer escape"], None
    return [], {
        "supplier_definitions": ((source.path, id(method)),),
        "supplier_calls": tuple((consumer.path, id(call)) for call in calls),
        "source_origins": tuple(origins),
        "protected_origins": RECOVERY_INSTRUCTION_ORIGINS,
        "function_origins": RECOVERY_INSTRUCTION_FUNCTION_ORIGINS,
        "class_origins": RECOVERY_INSTRUCTION_CLASS_ORIGINS,
        "current_recipe_definitions": (
            *((consumer.path, id(node)) for node in (producer, rebuild, restore, register)),
            (recipes.path, id(recipe)),
        ),
        "common_unknown": (
            "runtime prompt-guard enum and callable-code identity",
            "current producer receiver, schema suppliers and selected SQL admission",
            "complete reconstruction control flow and retained wire equality",
            "outside or deferred supplier mutation",
        ),
    }
