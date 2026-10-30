"""The provenance/wire split survives the thaw, and one module owns its key set."""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import fields
from pathlib import Path

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_freeze, deep_thaw
from elspeth.contracts.prompt_guard import (
    PROMPT_GUARD_CONTENT_PARTS_KEY,
    PROMPT_GUARD_PARTITION_KEYS,
    PROMPT_GUARD_PROVENANCE_KEYS,
    PROMPT_GUARD_TRUST_KEY,
    DetachedPromptGuardEvidence,
    PromptGuardContentPart,
    PromptGuardTrust,
    detach_prompt_guard_evidence,
    thaw_guarded_messages,
)

_SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src" / "elspeth"
_CANONICAL_DEFINITION = _SOURCE_ROOT / "contracts" / "prompt_guard.py"
_CANONICAL_SET_NAME = "PROMPT_GUARD_PROVENANCE_KEYS"
_CONTENT = "Pipeline state snapshot: {}"


def _guarded_message() -> dict[str, object]:
    """One message whose complete wire content is covered by an owned partition."""
    head, tail = _CONTENT[:17], _CONTENT[17:]
    return {
        "role": "user",
        "content": _CONTENT,
        PROMPT_GUARD_CONTENT_PARTS_KEY: (PromptGuardContentPart(head), PromptGuardContentPart(tail)),
    }


def test_the_guarded_thaw_keeps_an_owned_partition_owned() -> None:
    frozen = deep_freeze([_guarded_message()])

    thawed = thaw_guarded_messages(frozen)

    parts = thawed[0][PROMPT_GUARD_CONTENT_PARTS_KEY]
    assert type(parts) is tuple
    assert all(type(part) is PromptGuardContentPart for part in parts)
    evidence = detach_prompt_guard_evidence(thawed)
    assert evidence.content_parts[0] == parts
    assert PROMPT_GUARD_CONTENT_PARTS_KEY not in evidence.messages[0]


def test_the_wire_is_thawed_into_the_mutable_shapes_sdk_preparation_needs() -> None:
    frozen = deep_freeze([{**_guarded_message(), "tool_calls": [{"id": "call", "type": "function"}]}])

    thawed = thaw_guarded_messages(frozen)

    assert type(thawed[0]) is dict
    assert type(thawed[0]["tool_calls"]) is list
    assert type(thawed[0]["tool_calls"][0]) is dict


def test_an_unguarded_thaw_is_what_made_the_producer_look_foreign() -> None:
    """The positive control for the fix: deep_thaw alone rewrites the partition."""
    frozen = deep_freeze([_guarded_message()])

    rewritten = deep_thaw(frozen)

    assert type(rewritten[0][PROMPT_GUARD_CONTENT_PARTS_KEY]) is list
    with pytest.raises(AuditIntegrityError, match="not an owned producer identity"):
        detach_prompt_guard_evidence(rewritten)


def test_a_foreign_partition_still_fails_closed_through_the_guarded_thaw() -> None:
    """The gate keeps refusing: the thaw carries provenance, it does not bless it."""
    head, tail = _CONTENT[:17], _CONTENT[17:]
    foreign = {
        "role": "user",
        "content": _CONTENT,
        PROMPT_GUARD_CONTENT_PARTS_KEY: [PromptGuardContentPart(head), PromptGuardContentPart(tail)],
    }

    thawed = thaw_guarded_messages([foreign])

    assert type(thawed[0][PROMPT_GUARD_CONTENT_PARTS_KEY]) is list
    with pytest.raises(AuditIntegrityError, match="not an owned producer identity"):
        detach_prompt_guard_evidence(thawed)


def test_the_partition_keys_are_every_provenance_key_but_the_whole_message_one() -> None:
    """The derived list the admission checks iterate, pinned against the set."""
    assert set(PROMPT_GUARD_PARTITION_KEYS) == PROMPT_GUARD_PROVENANCE_KEYS - {PROMPT_GUARD_TRUST_KEY}
    assert sorted(PROMPT_GUARD_PARTITION_KEYS) == list(PROMPT_GUARD_PARTITION_KEYS), "order must not follow the hash seed"


def test_every_partition_key_has_a_partition_field_to_be_carried_on() -> None:
    """A partition key needs an accumulator, a validator and a field, not just a strip.

    The admission loop is derived, so a key added to the canonical set is
    refused when it conflicts — but on the honest path it would be stripped
    from the wire and then held nowhere, uninspected. Both sides of this count
    are derived, so a key without a field and a field without a key each fail.
    """
    partition_fields = [entry.name for entry in fields(DetachedPromptGuardEvidence) if entry.name.endswith("_parts")]

    assert len(PROMPT_GUARD_PARTITION_KEYS) == len(partition_fields), (
        f"{len(PROMPT_GUARD_PARTITION_KEYS)} partition keys against fields {partition_fields}"
    )


def test_every_partition_key_refuses_a_partition_that_carries_no_parts() -> None:
    """Driven by the live set, so a fifth key with no refusal fails this test.

    Without it the gate would be the detector alone, which only sees how the
    keys are spelled. A key added to the canonical set is stripped from the
    wire by the comprehension below; if nothing admits it, the strip is the
    whole of its handling and the omission is invisible.
    """
    assert PROMPT_GUARD_PARTITION_KEYS, "no partition keys to check, so this test measures nothing"

    for key in PROMPT_GUARD_PARTITION_KEYS:
        with pytest.raises(AuditIntegrityError, match="conflict"):
            detach_prompt_guard_evidence([{"role": "user", "content": _CONTENT, key: None}])


def test_every_partition_key_refuses_a_whole_message_verdict_beside_it() -> None:
    """The second limb of the same rule, also driven by the live set."""
    for key in PROMPT_GUARD_PARTITION_KEYS:
        message = {"role": "user", "content": _CONTENT, key: (), PROMPT_GUARD_TRUST_KEY: PromptGuardTrust.INSTRUCTION}

        with pytest.raises(AuditIntegrityError, match="conflict"):
            detach_prompt_guard_evidence([message])


def _declared_key_values(path: Path) -> dict[str, str]:
    """Each module-level ``PROMPT_GUARD_*_KEY`` assignment, name to wire value.

    Read from the source rather than from the imported module, so the check
    needs no dynamic attribute probe of our own package.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    declared: dict[str, str] = {}
    for statement in tree.body:
        # Both forms, because `NAME: Final[str] = "..."` is an AnnAssign and is
        # the natural style beside the annotated canonical set below it.
        if type(statement) is ast.Assign:
            targets, value = statement.targets, statement.value
        elif type(statement) is ast.AnnAssign and statement.value is not None:
            targets, value = [statement.target], statement.value
        else:
            continue
        if type(value) is not ast.Constant:
            continue
        for target in targets:
            if type(target) is ast.Name and target.id.startswith("PROMPT_GUARD_") and target.id.endswith("_KEY"):
                declared[target.id] = str(value.value)
    return declared


def test_every_declared_guard_key_is_a_member_of_the_canonical_set() -> None:
    """A fifth key added without joining the set would fail open at some consumer."""
    declared = _declared_key_values(_CANONICAL_DEFINITION)
    assert declared, "the key scan found nothing, so it is not measuring the module"

    missing = {name for name, value in declared.items() if value not in PROMPT_GUARD_PROVENANCE_KEYS}

    assert not missing, f"guard keys outside PROMPT_GUARD_PROVENANCE_KEYS: {sorted(missing)}"
    assert len(declared) == len(PROMPT_GUARD_PROVENANCE_KEYS)


def _guard_key_names(nodes: Iterable[ast.AST]) -> set[str]:
    return {node.id for node in nodes if type(node) is ast.Name and node.id.startswith("PROMPT_GUARD_") and node.id.endswith("_KEY")}


def _canonical_assignments(tree: ast.Module) -> list[ast.expr]:
    """The assigned value of every module-level binding of the canonical name.

    Module level only: a binding inside a function or a conditional is not the
    canonical definition, and exempting one would be a way in.
    """
    values: list[ast.expr] = []
    for statement in tree.body:
        # Assign first, so ``targets`` is inferred as the wider list the
        # annotated form's single target also fits, exactly as the key scan
        # below orders its own two branches.
        if type(statement) is ast.Assign:
            targets, value = statement.targets, statement.value
        elif type(statement) is ast.AnnAssign and statement.value is not None:
            targets, value = [statement.target], statement.value
        else:
            continue
        if any(type(target) is ast.Name and target.id == _CANONICAL_SET_NAME for target in targets):
            values.append(value)
    return values


def _canonical_definition_literals(tree: ast.Module) -> set[int]:
    """Node ids of the container literals belonging to the canonical assignment.

    The definition has to enumerate the keys; that is what makes it canonical.
    Exempting it by node keeps the rule live through the rest of the defining
    module, which is where a file-scoped exemption used to admit anything.
    """
    exempt: set[int] = set()
    for value in _canonical_assignments(tree):
        for node in ast.walk(value):
            if type(node) is ast.Set or type(node) is ast.Tuple or type(node) is ast.List:
                exempt.add(id(node))
    return exempt


def _duplicate_enumerations(tree: ast.Module) -> list[int]:
    """Line numbers of sites that enumerate two or more guard keys by hand.

    Three shapes, because review found control-flow and mapping-key forms
    evading a detector that only knew container elements:

    1. a container literal holding the keys, e.g. ``frozenset({A, B, C})``;
    2. a chain of membership tests, e.g. ``A in message or B in message``,
       which spells the same enumeration out as control flow.
    3. a mapping literal keyed by the guard keys, e.g. ``{A: first, B: second}``.

    The derived form, ``any(key in message for key in
    PROMPT_GUARD_PROVENANCE_KEYS - {A})``, names no guard key as the subject of
    an ``in`` test and so is not an enumeration: adding a fifth key extends it
    automatically.
    """
    # The derived form subtracts an exclusion set from the canonical one, and
    # that exclusion set is itself a literal naming the excluded keys. It is
    # not an enumeration of the provenance set, so exempt it — found by this
    # detector flagging the first derived site written against it.
    exempt = {
        id(node.right)
        for node in ast.walk(tree)
        if type(node) is ast.BinOp and type(node.op) is ast.Sub and type(node.left) is ast.Name and node.left.id == _CANONICAL_SET_NAME
    }
    # The canonical assignment is exempt by node, not by file. Exempting the
    # whole defining module admitted a planted enumeration beside the real one
    # while reporting it at any other path, so the rule now reaches every other
    # site in the module that owns the keys.
    exempt |= _canonical_definition_literals(tree)
    found: list[int] = []
    for node in ast.walk(tree):
        if type(node) is ast.Set or type(node) is ast.Tuple or type(node) is ast.List:
            if id(node) not in exempt and len(_guard_key_names(node.elts)) >= 2:
                found.append(node.lineno)
        elif type(node) is ast.Dict:
            keys = [key for key in node.keys if key is not None]
            if id(node) not in exempt and len(_guard_key_names(keys)) >= 2:
                found.append(node.lineno)
        elif type(node) is ast.BoolOp:
            subjects: list[ast.AST] = []
            for value in ast.walk(node):
                if type(value) is ast.Compare and any(type(op) is ast.In or type(op) is ast.NotIn for op in value.ops):
                    subjects.append(value.left)
            if len(_guard_key_names(subjects)) >= 2:
                found.append(node.lineno)
    return found


def test_the_duplicate_enumeration_detector_discriminates() -> None:
    """Control the instrument before trusting the tree-wide zero it reports.

    One positive per shape the detector claims to catch. The membership-chain
    case is here because a detector that knew only the literal shape reported
    zero offenders while two live ``or``-chains enumerated 3 and 4 keys.
    """
    literal = ast.parse("KEYS = frozenset({PROMPT_GUARD_TRUST_KEY, PROMPT_GUARD_CONTENT_PARTS_KEY})")
    keyed_dict = ast.parse("messages = {PROMPT_GUARD_TRUST_KEY: first, PROMPT_GUARD_CONTENT_PARTS_KEY: second}")
    keyed_dict_with_unpacking = ast.parse("messages = {**message, PROMPT_GUARD_TRUST_KEY: first, PROMPT_GUARD_CONTENT_PARTS_KEY: second}")
    chain = ast.parse(
        "if PROMPT_GUARD_CONTENT_PARTS_KEY in message or PROMPT_GUARD_TRUST_KEY in message:\n    raise AuditIntegrityError('x')\n"
    )
    derived = ast.parse(
        "if any(key in message for key in PROMPT_GUARD_PROVENANCE_KEYS - {PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY}) or "
        "message.get(PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY) != rebuilt.get(PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY):\n"
        "    raise AuditIntegrityError('x')\n"
    )
    single = ast.parse("result = {PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY: parts}\nKEYS = PROMPT_GUARD_PROVENANCE_KEYS\n")
    one_key_dicts = ast.parse("messages = [{PROMPT_GUARD_TRUST_KEY: first}, {PROMPT_GUARD_CONTENT_PARTS_KEY: second}]")
    dict_values = ast.parse("messages = {'first': PROMPT_GUARD_TRUST_KEY, 'second': PROMPT_GUARD_CONTENT_PARTS_KEY}")
    repeated_dict_key = ast.parse("messages = {PROMPT_GUARD_TRUST_KEY: first, PROMPT_GUARD_TRUST_KEY: second}")
    single_with_unpacking = ast.parse("messages = {**message, PROMPT_GUARD_TRUST_KEY: first}")
    derived_dict_comprehension = ast.parse("messages = {key: message[key] for key in PROMPT_GUARD_PROVENANCE_KEYS}")
    enumerating_dict_comprehension = ast.parse(
        "messages = {key: message[key] for key in (PROMPT_GUARD_TRUST_KEY, PROMPT_GUARD_CONTENT_PARTS_KEY)}"
    )
    # Two excluded keys: the exclusion literal must not itself read as an
    # enumeration, which is the shape that first tripped this detector.
    derived_two = ast.parse(
        "if any(key in message for key in PROMPT_GUARD_PROVENANCE_KEYS - "
        "{PROMPT_GUARD_THINKING_PARTS_KEY, PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY}):\n    raise AuditIntegrityError('x')\n"
    )
    # ... but a bare literal of the same two keys, not subtracted from the
    # canonical set, is still an enumeration.
    bare_pair = ast.parse("KEYS = {PROMPT_GUARD_THINKING_PARTS_KEY, PROMPT_GUARD_TOOL_ARGUMENT_PARTS_KEY}")
    # The canonical assignment must enumerate, and must be the only thing in
    # its module that does: the exemption is per node, so a second literal
    # beside it is still reported.
    canonical = ast.parse(
        f"{_CANONICAL_SET_NAME}: Final[frozenset[str]] = frozenset({{PROMPT_GUARD_TRUST_KEY, PROMPT_GUARD_THINKING_PARTS_KEY}})"
    )
    canonical_and_plant = ast.parse(
        f"{_CANONICAL_SET_NAME}: Final[frozenset[str]] = frozenset({{PROMPT_GUARD_TRUST_KEY, PROMPT_GUARD_THINKING_PARTS_KEY}})\n"
        "PLANTED_KEYS = frozenset({PROMPT_GUARD_TRUST_KEY, PROMPT_GUARD_THINKING_PARTS_KEY})\n"
    )

    assert _duplicate_enumerations(literal) == [1], "the container-literal shape must be caught"
    assert _duplicate_enumerations(keyed_dict) == [1], "the mapping-key shape must be caught"
    assert _duplicate_enumerations(keyed_dict_with_unpacking) == [1], "unpacking must not hide the mapping's enumerated keys"
    assert _duplicate_enumerations(chain) == [1], "the membership-chain shape must be caught"
    assert _duplicate_enumerations(derived) == [], "the derived form is not an enumeration"
    assert _duplicate_enumerations(derived_two) == [], "a two-key exclusion set is not an enumeration"
    assert _duplicate_enumerations(bare_pair) == [1], "the same literal, not derived, is still an enumeration"
    assert _duplicate_enumerations(single) == [], "one key is ordinary use"
    assert _duplicate_enumerations(one_key_dicts) == [], "separate one-key messages are ordinary use"
    assert _duplicate_enumerations(dict_values) == [], "guard keys used as mapping values do not enumerate mapping keys"
    assert _duplicate_enumerations(repeated_dict_key) == [], "the threshold counts distinct guard keys"
    assert _duplicate_enumerations(single_with_unpacking) == [], "unpacking contributes no explicit guard key"
    assert _duplicate_enumerations(derived_dict_comprehension) == [], "a comprehension derived from the canonical set stays current"
    assert _duplicate_enumerations(enumerating_dict_comprehension) == [1], "the literal iterable still enumerates guard keys"
    assert _duplicate_enumerations(canonical) == [], "the canonical assignment is the one site that must enumerate"
    assert _duplicate_enumerations(canonical_and_plant) == [2], "a second enumeration beside the canonical one is reported"


def test_exactly_one_module_defines_the_canonical_set() -> None:
    """The exemption keys off this name, so a second definition would shadow it."""
    definitions = [
        path for path in sorted(_SOURCE_ROOT.rglob("*.py")) if _canonical_assignments(ast.parse(path.read_text(encoding="utf-8")))
    ]

    assert definitions == [_CANONICAL_DEFINITION], f"the canonical set is defined in {definitions}"


def test_no_module_enumerates_the_guard_keys_outside_the_canonical_assignment() -> None:
    """Two enumerations drift, and the one that is missed fails open.

    Scanned without a path exemption: the module that defines the keys is
    covered everywhere except the canonical assignment itself, which is exempt
    by node identity.
    """
    offenders: dict[str, list[int]] = {}
    scanned = 0
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        scanned += 1
        lines = _duplicate_enumerations(ast.parse(path.read_text(encoding="utf-8")))
        if lines:
            offenders[str(path.relative_to(_SOURCE_ROOT))] = lines

    assert scanned > 100, f"the scan reached only {scanned} modules, so its empty result means nothing"
    assert not offenders, f"guard keys enumerated outside the canonical assignment: {offenders}"
