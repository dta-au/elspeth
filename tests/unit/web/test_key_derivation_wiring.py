"""Every cryptographic consumer of ``secret_key`` receives a DERIVED key.

WHY THIS FILE EXISTS
--------------------
``web/key_derivation.py`` only separates keys if the call sites actually use
it. Two of the three consumers have a failure mode that no unit test of the
consumer itself can catch:

* ``generation_key`` is built at TWO sites -- the app factory and the AWS ECS
  acceptance harness. The fingerprint one produces is compared against a
  queued run's frozen copy, so if only one site were converted the harness
  would refuse valid runs with a binding-rotation error naming the wrong
  cause. Nothing else in the tree compares the two.
* ``UserSecretStore``'s own tests construct it directly with a literal master
  key, so they pass identically whether ``app.py`` hands it a derived key or
  the raw one.

SCOPE, STATED HONESTLY
----------------------
This is an AST check over the argument expression at three named call sites.
It proves those three call sites pass a derivation call. It does NOT prove
that no *fourth* consumer exists, and it cannot see a raw use added inside a
different callable -- the raw-use sweep below is what bounds that, over the
``web`` package only.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import elspeth.web as web_package
from elspeth_lints.core.ast_walker import iter_python_files

_WEB_ROOT = Path(web_package.__file__).parent

# (module path, keyword argument or call, expected derivation function)
_DERIVED_CALL_SITES = (
    ("app.py", "generation_key", "derive_binding_generation_key"),
    ("_aws_ecs_acceptance/bedrock.py", "generation_key", "derive_binding_generation_key"),
)


def _parse(relative: str) -> ast.Module:
    return ast.parse((_WEB_ROOT / relative).read_text(encoding="utf-8"))


def _keyword_value_calls(tree: ast.Module, keyword_name: str) -> list[str]:
    """Return the called-function name of every ``keyword_name=<call>(...)``."""
    called: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
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


@pytest.mark.parametrize(("module", "keyword", "derivation"), _DERIVED_CALL_SITES)
def test_every_generation_key_site_passes_a_derived_key(module: str, keyword: str, derivation: str) -> None:
    calls = _keyword_value_calls(_parse(module), keyword)
    assert calls, f"{module} no longer passes {keyword}= at all — this guard has gone blind"
    assert all(call == derivation for call in calls), f"{module} passes {keyword} from {calls}, expected {derivation}"


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
