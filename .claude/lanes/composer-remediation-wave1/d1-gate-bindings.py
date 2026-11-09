"""Maintain only individually reviewed D1 source bindings; never a tree refresh."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

from elspeth_lints.core.ast_dump import stable_ast_dump
from tests.helpers import web_reserve_compose_proposal_transfers as compose
from tests.helpers import web_reserve_proposal_child_transfers as child

ROOT = Path.cwd()
BASE = "5e192342c3fb025fd9a91244bebe437158e9de8e"
ORIGINAL_PROPOSALS_SHAPE = "23ef777eef8f72ed82e5943f633d94fdaa6d4f76cab9bd7ef9ec642adc59a8ca"
ORIGINAL_CHILD_SHAPE = "07455a11c3149e3fea7dd0bd23d8e6892a6f0f8d4015e980e37ebffcb6cc8a7d"
RECIPES = ROOT / "tests/helpers/required_work_reserve_recipes"


def source(path, baseline=False):
    return subprocess.check_output(["git", "show", BASE + ":" + path], text=True) if baseline else (ROOT / path).read_text()


def node(path, symbol, baseline=False):
    return compose._node(SimpleNamespace(tree=ast.parse(source(path, baseline))), symbol)


guards_path = RECIPES / "reviewed-full-source-guards.json"
guards = json.loads(guards_path.read_text())
original_guards = json.loads(guards_path.read_text())
for path in ("src/elspeth/web/sessions/protocol.py", "src/elspeth/web/sessions/proposal_authority.py"):
    old = hashlib.sha256(source(path, True).encode()).hexdigest()
    reviewed = json.loads(subprocess.check_output(["git", "show", "HEAD:" + str(guards_path.relative_to(ROOT))], text=True))
    assert reviewed[path]["sha256"] == old, path
    guards[path]["sha256"] = hashlib.sha256(source(path).encode()).hexdigest()
    print("full source", path, old, "->", guards[path]["sha256"])
assert guards.keys() == original_guards.keys()
assert all(guards[path]["protected_exports"] == original_guards[path]["protected_exports"] for path in guards)
guards_path.write_text(json.dumps(guards, indent=2, sort_keys=True) + "\n")

writer_path = RECIPES / "reviewed-writer-recipes.json"
writers = json.loads(subprocess.check_output(["git", "show", "HEAD:" + str(writer_path.relative_to(ROOT))], text=True))
original_keys = set(writers)
selected = (
    (compose.PROPOSALS, "accept_composition_proposal"),
    (compose.SETTLEMENT, "_settle_pipeline_proposal_under_compose_lock"),
)
for path, symbol in selected:
    key = path + ":" + symbol
    old_node = node(path, symbol, True)
    if path == compose.PROPOSALS:
        # b8921de06 added this exact server-policy injection before BASE;
        # the old recipe/pin omitted it. Preserve it in the new evidence.
        calls = [call for call in ast.walk(old_node) if isinstance(call, ast.Call)
                 and isinstance(call.func, ast.Name) and call.func.id == "run_sync_in_worker"
                 and call.args and isinstance(call.args[0], ast.Name) and call.args[0].id == "execute_tool"]
        assert len(calls) == 1
        policies = [keyword for keyword in calls[0].keywords if keyword.arg == "secret_wiring_policy"]
        assert len(policies) == 1
        assert ast.unparse(policies[0].value) == "runtime_secret_wiring_policy(request.app.state.settings.secret_wiring_allowlist)"
        calls[0].keywords.remove(policies[0])
        assert compose._method_shape(old_node) == ORIGINAL_PROPOSALS_SHAPE
    assert writers[key] == stable_ast_dump(old_node), key
    writers[key] = stable_ast_dump(node(path, symbol))
    print("writer recipe", key)
new_key = compose.PROPOSALS + ":_reject_legacy_proposal_refusal"
assert new_key not in writers
writers[new_key] = stable_ast_dump(node(compose.PROPOSALS, "_reject_legacy_proposal_refusal"))
blob_path = "src/elspeth/contracts/blobs.py"
def bindings(baseline=False):
    return stable_ast_dump(ast.Module(body=[item for item in ast.parse(source(blob_path, baseline)).body
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))], type_ignores=[]))
assert writers[blob_path + ":module-bindings"] == bindings(True)
writers[blob_path + ":module-bindings"] = bindings()
print("writer recipe module bindings", blob_path, "AuditIntegrityError import only")
assert set(writers) == original_keys | {new_key}
writer_path.write_text(json.dumps(writers, indent=2, sort_keys=True) + "\n")
print("writer recipe added", new_key)

old_compose = compose._method_shape(node(compose.PROPOSALS, "accept_composition_proposal", True))
print("PROPOSALS old pin", ORIGINAL_PROPOSALS_SHAPE, "BASE", old_compose, "HEAD", compose._method_shape(node(compose.PROPOSALS, "accept_composition_proposal")))
old_child = child.grammar(node(compose.SETTLEMENT, child.INTERNAL, True))
assert old_child == ORIGINAL_CHILD_SHAPE
print("INTERNAL", old_child, "->", child.grammar(node(compose.SETTLEMENT, child.INTERNAL)))
