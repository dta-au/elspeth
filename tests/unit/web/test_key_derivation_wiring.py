"""Every cryptographic consumer of ``secret_key`` receives a DERIVED key.

WHY THIS FILE EXISTS
--------------------
``web/key_derivation.py`` only separates keys if the call sites actually use
it. Every consumer has a failure mode that no unit test of the consumer
itself can catch:

* ``generation_key`` is built at TWO sites -- the app factory and the AWS ECS
  acceptance harness. The fingerprint one produces is compared against a
  queued run's frozen copy, so if only one site were converted the harness
  would refuse valid runs with a binding-rotation error naming the wrong
  cause. Nothing else in the tree compares the two.
* ``UserSecretStore``'s own tests construct it directly with a literal master
  key, so they pass identically whether ``app.py`` hands it a derived key or
  the raw one. ``SessionTokenIssuer`` is constructed the same way in its own
  tests, with the same consequence for the session-token signing key.
* No test of any single consumer can see that TWO consumers were handed the
  SAME derived key: each still receives 32 valid bytes and each still behaves
  correctly. Passing ``derive_binding_generation_key`` where
  ``derive_session_token_key`` belongs -- one plausible copy-paste -- collapses
  exactly the independence ``key_derivation.py`` exists to provide, making
  disclosure of a binding-fingerprint key a session-token forgery key, with
  every consumer's own suite still green. That is a class of defect, not one
  instance of it, so it is checked over the wiring as a whole below rather
  than by naming the pairing that happens to be wrong today.

SCOPE, STATED HONESTLY
----------------------
The per-site checks are an AST check over each named consumer and argument.
They prove those call sites pass the derivation each was meant to receive.
The keyword inventory also rejects unregistered signing/generation consumers,
but these checks do not prove no consumer using another argument exists, and
they cannot see a raw use added inside a different callable -- the raw-use
sweep below is what bounds that, over the ``web`` package only.

The collision check pins distinct derivation *names* against distinct
consumers in ``app.py``'s wiring. That the derivations return distinct key
*material* is a property of ``key_derivation.py`` itself, pinned by
``test_each_purpose_gets_a_different_key_from_one_master`` in
``test_key_derivation.py``. Neither test is sufficient alone: two identical
``info`` strings would defeat this file, and a copy-pasted call site would
defeat that one.

The collision check reads ``app.py`` alone, deliberately.
``derive_binding_generation_key`` legitimately feeds a second consumer in the
ECS acceptance harness -- the same purpose in a different process -- so
widening that walk to the package would report a shared purpose as a
collision.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import elspeth.web as web_package
from elspeth_lints.core.ast_walker import iter_python_files

_WEB_ROOT = Path(web_package.__file__).parent

# (module path, consumer, keyword argument, expected derivation function)
_DERIVED_CALL_SITES = (
    ("app.py", "RequestPluginSnapshotFactory", "generation_key", "derive_binding_generation_key"),
    ("app.py", "SessionTokenIssuer", "signing_key", "derive_session_token_key"),
    ("app.py", "RepositoryRateLimitAuthority", "signing_key", "derive_rate_limit_key"),
    ("_aws_ecs_acceptance/bedrock.py", "build_plugin_snapshot", "generation_key", "derive_binding_generation_key"),
)

_DERIVATION_PREFIX = "derive_"


def _parse(relative: str) -> ast.Module:
    return ast.parse((_WEB_ROOT / relative).read_text(encoding="utf-8"))


def _keyword_value_calls(tree: ast.Module, consumer: str, keyword_name: str) -> list[str]:
    """Return derivations for one consumer's named key argument."""
    called: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _consumer_name(node) != consumer:
            continue
        for keyword in node.keywords:
            if keyword.arg != keyword_name:
                continue
            value = keyword.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
                called.append(value.func.id)
            else:
                called.append(ast.dump(value))
    return called


@pytest.mark.parametrize(("module", "consumer", "keyword", "derivation"), _DERIVED_CALL_SITES)
def test_every_derived_key_site_passes_its_own_derivation(module: str, consumer: str, keyword: str, derivation: str) -> None:
    calls = _keyword_value_calls(_parse(module), consumer, keyword)
    assert calls, f"{module} no longer passes {consumer}({keyword}=) — this guard has gone blind"
    assert all(call == derivation for call in calls), f"{module} passes {consumer}({keyword}=) from {calls}, expected {derivation}"


def test_named_key_arguments_have_no_unregistered_consumers() -> None:
    """Callee scoping must not silently stop inventorying a new signing user."""
    for module in {site[0] for site in _DERIVED_CALL_SITES}:
        expected = {(consumer, keyword) for path, consumer, keyword, _ in _DERIVED_CALL_SITES if path == module}
        keywords = {keyword for _, keyword in expected}
        actual = {
            (_consumer_name(node), keyword.arg)
            for node in ast.walk(_parse(module))
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg in keywords
        }
        assert actual == expected


def _derivation_name(node: ast.AST) -> str | None:
    """Return the name of a ``derive_*(...)`` call, or ``None`` for anything else."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.startswith(_DERIVATION_PREFIX):
        return node.func.id
    return None


def _consumer_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _derivation_consumers(tree: ast.Module) -> list[tuple[str, str]]:
    """Pair every ``derive_*(...)`` with the construction slot it is passed to.

    The slot -- ``SessionTokenIssuer(signing_key=)`` -- rather than the bare
    callee name is what "purpose" means here: one class could plausibly take
    two independently derived keys, and each of them is its own consumer.
    """
    pairs: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        consumer = _consumer_name(node)
        if consumer is None:
            continue
        for index, argument in enumerate(node.args):
            derivation = _derivation_name(argument)
            if derivation is not None:
                pairs.append((derivation, f"{consumer}(arg{index})"))
        for keyword in node.keywords:
            derivation = _derivation_name(keyword.value)
            if derivation is not None:
                pairs.append((derivation, f"{consumer}({keyword.arg}=)"))
    return pairs


def test_no_derivation_in_the_app_factory_feeds_two_consumers() -> None:
    """Purpose separation is a property of the WIRING, not of any one site.

    Deliberately written over whatever ``app.py`` derives rather than over a
    fixed list of pairings: a fourth purpose added tomorrow arrives with its
    own derivation and its own slot and passes untouched, while any two
    purposes sharing a derivation fail whichever two they are. A set-equality
    against today's three names would have to be edited for the first and
    would say nothing about the second.
    """
    tree = _parse("app.py")
    pairs = _derivation_consumers(tree)
    total = sum(1 for node in ast.walk(tree) if _derivation_name(node) is not None)

    assert total, "app.py derives no purpose keys at all — this guard has gone blind"
    assert len(pairs) == total, (
        f"app.py makes {total} derivation calls but only {len(pairs)} are passed straight into a "
        "construction. A derivation bound to a local first (session_key = derive_...(); "
        "SessionTokenIssuer(signing_key=session_key)) is invisible to this walk, and one local "
        "handed to two consumers is exactly the collision this test exists to catch. Extend "
        "_derivation_consumers to follow assignment targets; do not delete this assertion."
    )

    consumers_by_derivation: dict[str, set[str]] = {}
    slots_receiving: dict[str, set[str]] = {}
    for derivation, consumer in pairs:
        consumers_by_derivation.setdefault(derivation, set()).add(consumer)
        slots_receiving.setdefault(consumer, set()).add(derivation)

    shared = {derivation: sorted(consumers) for derivation, consumers in consumers_by_derivation.items() if len(consumers) > 1}
    assert shared == {}, (
        f"one derivation feeds several consumers, so they share key material: {shared}. "
        "Disclosure of any one of them then discloses the others — the failure "
        "web/key_derivation.py exists to prevent."
    )

    mixed = {consumer: sorted(derivations) for consumer, derivations in slots_receiving.items() if len(derivations) > 1}
    assert mixed == {}, f"one construction slot is fed by several derivations, so its key depends on which site ran: {mixed}"


def test_the_user_secret_store_is_constructed_with_a_derived_master() -> None:
    """``app.py`` must not hand the store the raw ``secret_key``."""
    tree = _parse("app.py")
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "UserSecretStore"
    ]
    assert len(constructions) == 1, f"expected exactly one UserSecretStore construction in app.py, found {len(constructions)}"

    master_argument = constructions[0].args[1]
    assert isinstance(master_argument, ast.Call), "UserSecretStore master key is not a derivation call"
    assert isinstance(master_argument.func, ast.Name)
    assert master_argument.func.id == "derive_user_secret_master_key"


def test_no_web_module_feeds_the_raw_secret_key_to_a_byte_consumer() -> None:
    """``settings.secret_key.encode(...)`` is the shape every converted site had.

    ``config.py`` is excluded, and the reason is substantive rather than
    bookkeeping: it is the one module where ``secret_key`` is the SUBJECT of
    validation rather than key material.
    :meth:`WebSettings._enforce_secret_key_in_production` weighs the operator's
    raw bytes for entropy, and that check MUST run before derivation --
    HKDF output is uniformly distributed by construction, so running the
    uniform-byte test on a derived key would pass for every input, including
    ``"aaaaaaaa..."``. Deriving first would not secure that check; it would
    silently empty it.

    Bounded deliberately: this walks the ``web`` package only, and it catches
    the *encode* idiom rather than every conceivable raw use. It is a
    regression tripwire for the three sites this change converted, not a proof
    that no raw use can ever exist.
    """
    offenders: list[str] = []
    # Walked through ``ast_walker``, the single authority for Python-file
    # discovery. A private ``rglob`` here would quietly disagree with every
    # other gate about what "the source tree" means — which files are
    # excluded, which directories are skipped — and a sweep that scans a
    # different set from the rest of the suite is a sweep whose empty result
    # means nothing.
    for path in sorted(iter_python_files(_WEB_ROOT)):
        if path.name == "config.py" and path.parent == _WEB_ROOT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "encode":
                continue
            target = node.func.value
            if isinstance(target, ast.Attribute) and target.attr == "secret_key":
                offenders.append(f"{path.relative_to(_WEB_ROOT)}:{node.lineno}")

    assert offenders == [], f"raw secret_key bytes reach a consumer at: {offenders}"
