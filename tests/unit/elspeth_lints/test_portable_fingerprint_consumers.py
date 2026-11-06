"""Persisted identities retain native Python 3.13 bytes on supported runtimes."""

import ast
import hashlib
from pathlib import Path

from elspeth_lints.rules.trust_boundary.tests.rule import _fingerprint_test_function, _resolve_test_ref, _TestRefResolutionError
from elspeth_lints.rules.trust_tier.tier_model.rule import Finding, TierModelVisitor
from elspeth_lints.rules.trust_tier.tier_model.scope_fingerprint import compute_scope_fingerprint

# Literal dump hashes were generated with native Python 3.13 ast.dump,
# independently of the consumers and shared serializer. The raw finding key
# preserves the pre-change Python 3.13 visitor's output.
_SYNC = (
    "@mark(level=1)\ndef contract(payload=None, *, enabled=True) -> object:\n"
    '    "checked contract"\n    if enabled:\n        return payload\n    return None\n'
)
_ASYNC = '@mark(level=1)\nasync def contract(payload=None) -> object:\n    "async contract"\n    return await deliver(payload)\n'
_CLASS = 'class Adapter(Base):\n    "adapter contract"\n    @staticmethod\n    def read(payload=None):\n        return payload\n'
_MODULE = '"module contract"\nVALUE = None\n'
_FINDING = "from elspeth.contracts import trust_boundary\n@trust_boundary()\ndef boundary(payload=None):\n    return payload\n"
_SYNC_HASH = "fe63baf4093f31355788fcb5088bb2bff959e0092bf5d9d60c270afa42b37ec7"
_ASYNC_HASH = "92d992f744ccca294abfe19e6c8192ec59c853a55896b5946bd91fa653e5fae9"


def _function(source: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    node = ast.parse(source).body[-1]
    assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    return node


def _finding(source: str) -> Finding:
    visitor = TierModelVisitor("contracts.py", source.splitlines(), hashlib.sha256(source.encode("utf-8")).hexdigest())
    visitor.visit(ast.parse(source))
    assert len(visitor.findings) == 1
    finding = visitor.findings[0]
    assert finding.rule_id == "R_TB_MALFORMED"
    return finding


def test_scope_fingerprints_preserve_native_313_reference_bytes() -> None:
    for source, expected in (
        (_SYNC, _SYNC_HASH),
        (_ASYNC, _ASYNC_HASH),
        (_CLASS, "77399d041997e1d697b900f23c5d1f8acd7c1d40a271d21bff1258c7d308d773"),
    ):
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        assert compute_scope_fingerprint(node) == expected
    assert compute_scope_fingerprint(None, module=ast.parse(_MODULE)) == (
        "0f489fb7d0005db1bb835de2b6cf48fad49e17ed129e58b3d9880fc0caeda03a"
    )


def test_referenced_test_fingerprints_preserve_native_313_reference_bytes() -> None:
    assert _fingerprint_test_function(_function(_SYNC)) == _SYNC_HASH
    assert _fingerprint_test_function(_function(_ASYNC)) == _ASYNC_HASH


def test_finding_key_and_scope_preserve_native_313_reference_bytes() -> None:
    # The malformed decorator Call is hashed at its FunctionDef's AST path.
    finding = _finding(_FINDING)
    assert finding.canonical_key == "contracts.py:R_TB_MALFORMED:boundary:fp=3cdc0375a45ff44c"
    assert finding.ast_path == "body[1]"
    assert finding.scope_fingerprint == "b27879b8905b732d7e1e5e8b951076b2941ffef39b54064e0e623f6cb9409b93"


def test_fingerprint_consumers_leave_input_asts_unchanged() -> None:
    for source in (_SYNC, _ASYNC, _CLASS):
        node = ast.parse(source).body[0]
        assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        before = ast.dump(node, include_attributes=True)
        compute_scope_fingerprint(node)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            _fingerprint_test_function(node)
        assert ast.dump(node, include_attributes=True) == before
    module = ast.parse(_MODULE)
    module.type_ignores.append(ast.TypeIgnore(lineno=7, tag="[fixture]"))
    before = ast.dump(module, include_attributes=True)
    assert compute_scope_fingerprint(None, module=module) == compute_scope_fingerprint(None, module=ast.parse(_MODULE))
    assert ast.dump(module, include_attributes=True) == before


def test_formatting_is_free_but_runtime_visible_content_changes_identity() -> None:
    plain = _function(_SYNC)
    cosmetic = _function(_SYNC.replace("def contract", "# comment\ndef contract").replace("return payload", "return (payload)"))
    assert compute_scope_fingerprint(cosmetic) == compute_scope_fingerprint(plain)
    assert _fingerprint_test_function(cosmetic) == _fingerprint_test_function(plain)
    for old, new in (
        ("checked contract", "changed contract"),
        ("level=1", "level=2"),
        ("payload=None", "payload=0"),
        ("return payload", "return enabled"),
        ("-> object", "-> str"),
    ):
        changed = _function(_SYNC.replace(old, new))
        assert compute_scope_fingerprint(changed) != compute_scope_fingerprint(plain)
        assert _fingerprint_test_function(changed) != _fingerprint_test_function(plain)


def test_raw_finding_identity_retains_rule_and_path_discriminators() -> None:
    node = _function(_SYNC)
    visitor = TierModelVisitor("contracts.py", _SYNC.splitlines(), hashlib.sha256(_SYNC.encode("utf-8")).hexdigest())
    visitor.path_stack = ["body[0]"]
    original = visitor._fingerprint_node("R1", node)
    assert visitor._fingerprint_node("R2", node) != original
    assert visitor._fingerprint_node("R1", _function(_SYNC.replace("payload=None", "payload=0"))) != original
    visitor.path_stack = ["body[1]"]
    assert visitor._fingerprint_node("R1", node) != original
    original_finding = _finding(_FINDING)
    cosmetic = _finding(_FINDING.replace("def boundary", "# comment\ndef boundary"))
    assert cosmetic.canonical_key == original_finding.canonical_key
    assert cosmetic.scope_fingerprint == original_finding.scope_fingerprint
    moved = _finding("import unrelated\n" + _FINDING)
    assert moved.canonical_key != original_finding.canonical_key
    assert moved.scope_fingerprint == original_finding.scope_fingerprint


def test_real_declared_test_reference_resolves_to_its_persisted_fingerprint() -> None:
    root = Path(__file__).resolve().parents[3]
    tree = ast.parse((root / "src/elspeth/contracts/identifiers.py").read_text(encoding="utf-8"))
    subject = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "validate_field_names")
    declaration = next(
        decorator
        for decorator in subject.decorator_list
        if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name) and decorator.func.id == "trust_boundary"
    )
    metadata = {keyword.arg: ast.literal_eval(keyword.value) for keyword in declaration.keywords}
    resolution = _resolve_test_ref(metadata["test_ref"], root)
    assert not isinstance(resolution, _TestRefResolutionError)
    assert resolution.fingerprint == metadata["test_fingerprint"]
