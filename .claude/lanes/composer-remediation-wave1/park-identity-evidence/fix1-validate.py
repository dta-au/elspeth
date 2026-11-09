"""Validate the narrow refusal-oracle fix and its terminal control runs."""

import hashlib
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

root = Path.cwd()
lane = Path("/workspace/elspeth/.claude/lanes/composer-remediation-wave1")
evidence = lane / "park-identity-evidence"
mutant = evidence / "fix1-refusal-mutant"

expected_exits = {
    "fix1-refusal-green": 0,
    "fix1-refusal-final": 0,
    "fix1-ruff": 0,
    "fix1-format": 1,
    "fix1-ruff-final": 0,
    "fix1-format-final": 0,
    "fix1-mutant-old-oracle": 1,
    "fix1-mutant-old-oracle-controlled": 0,
    "fix1-mutant-new-oracle": 1,
}
for prefix, expected in expected_exits.items():
    actual = int((evidence / (prefix + ".exit")).read_text())
    assert actual == expected, (prefix, actual)
    print(f"{prefix}.exit={actual}")

def cases(prefix):
    return {case.attrib["name"]: case for case in ET.parse(evidence / (prefix + ".xml")).iter("testcase")}

green = cases("fix1-refusal-final")
old = cases("fix1-mutant-old-oracle-controlled")
new = cases("fix1-mutant-new-oracle")
assert len(green) == 28
assert all(case.find("failure") is None and case.find("error") is None for case in green.values())
assert len(old) == len(new) == 4
assert old.keys() == new.keys()
assert all(case.find("failure") is None and case.find("error") is None for case in old.values())
for name, case in new.items():
    failure = case.find("failure")
    assert failure is not None and case.find("error") is None, name
    assert "assert response.recovery_notice.content == expected_notice" in failure.text, name
    assert "Requested selector: {" in failure.text, name
    assert "AuditIntegrityError" not in failure.text, name
assert all(name in green for name in new)
print("Final candidate: 28 passed; same four braced mutant cases: old oracle 4 passed, new oracle 4 exact-notice assertion failures.")

old_source = evidence / "fix1_refusal_old_oracle.py"
assert old_source.read_bytes() == (evidence / "reviewed-test_graph_recovery_turn.py").read_bytes()
paths = subprocess.check_output(["git", "ls-files", "-z", "src"]).decode().split("\0")
changed = [name for name in paths if name and (mutant / name).read_bytes() != (root / name).read_bytes()]
assert changed == ["src/elspeth/web/sessions/service.py"], changed
service_name = changed[0]
needle = '        elif recovery.intent.action == "park":\n'
expected_mutant = (root / service_name).read_text().replace(
    needle, '            notice += f" Requested selector: {{{recovery.intent.park_id}}}."\n' + needle, 1
)
assert (mutant / service_name).read_text() == expected_mutant
mutant_hash = hashlib.sha256((mutant / service_name).read_bytes()).hexdigest() + "\n"
assert mutant_hash == (evidence / "fix1-mutant-before.sha256").read_text()
print("Mutant tracked-source difference is exactly the one notice-injection line; unchanged throughout both controlled runs.")

head = subprocess.check_output(["git", "rev-parse", "HEAD"])
diff = subprocess.check_output(["git", "diff", "--binary"])
untracked = subprocess.check_output(["git", "ls-files", "--others", "--exclude-standard"])
values = {
    "head": head,
    "diff.sha256": (hashlib.sha256(diff).hexdigest() + "\n").encode(),
    "untracked": untracked,
}
for suffix, value in values.items():
    assert value == (evidence / ("fix1-final-before." + suffix)).read_bytes(), suffix
    (evidence / ("fix1-final-after." + suffix)).write_bytes(value)
assert head.decode().strip() == "05bcab93955b2070ffdc90e72eddd7368502d2c1"
assert not untracked
assert hashlib.sha256((root / "src/elspeth/web/composer/graph_recovery.py").read_bytes()).hexdigest() == (
    "a0bc235eb91690b063fcef6e9533df623d136fda032b5c9b37887289330a101c"
)
original_hash = hashlib.sha256((lane / "park-identity-review.diff").read_bytes()).hexdigest()
assert original_hash == "1a9999a08b9983d0cfb7a88cc508f1f9fab3518226d5c8e1487f55579c7a8616"
assert diff == (lane / "park-identity-fix1-full.diff").read_bytes()
subprocess.run(["git", "apply", "--check", "--reverse", str(lane / "park-identity-fix1.diff")], check=True)
freeze = (
    "Frozen fix1 candidate: HEAD, complete binary patch digest, and empty untracked inventory unchanged across final checks and controls.\n"
    + "HEAD=" + head.decode().strip() + "\n"
    + "full_patch_sha256=" + hashlib.sha256(diff).hexdigest() + "\n"
    + "fix_delta_sha256=" + hashlib.sha256((lane / "park-identity-fix1.diff").read_bytes()).hexdigest() + "\n"
    + "original_review_patch_sha256=" + original_hash + "\n"
    + "Production graph_recovery.py remains byte-identical to the reviewed candidate.\n"
)
(evidence / "fix1-freeze.txt").write_text(freeze)
print(freeze)
