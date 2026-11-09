"""Capture one serial dependency verification with immutable source evidence."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys

LANE = Path(__file__).parent
ROOT = LANE.parents[2] / ".claude/worktrees/composer-remediation-wave1"
MODE = sys.argv[1]
assert MODE in ("chroma", "browser", "postgres", "pgsequence")
SUFFIX = sys.argv[2] if len(sys.argv) == 3 else ""
assert not SUFFIX or SUFFIX.isalnum()
PREFIX = LANE / f"d1-dependency-{MODE}{('-' + SUFFIX) if SUFFIX else ''}"
assert not PREFIX.with_suffix(".exit").exists()


def snapshot():
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    assert head == "14fef5e5342ccbd79b4fdd69b276c20f77d66dc1"
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True)
    assert status == ""
    files = {}
    for raw in subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).split(b"\0"):
        if not raw:
            continue
        name = os.fsdecode(raw)
        path = ROOT / name
        if path.is_symlink():
            files[name] = {"target": os.readlink(path)}
        else:
            with path.open("rb") as source:
                files[name] = {"sha256": hashlib.file_digest(source, "sha256").hexdigest()}
    return {"head": head, "status": status, "files": files}


before = snapshot()
PREFIX.with_suffix(".before.json").write_text(json.dumps(before, sort_keys=True))
env = dict(os.environ)
env["PYTHONPATH"] = f"{ROOT}/src:{ROOT}/elspeth-lints/src"
env["PATH"] = f"{LANE}/runtime-deps/caddy:{LANE}/runtime-deps/sqlite3/bin:" + env["PATH"]
env["PLAYWRIGHT_BROWSERS_PATH"] = str(LANE / "playwright-browsers")
cmd = [str(ROOT / ".venv/bin/python")]
if MODE == "chroma":
    cmd += [str(LANE / "run-chroma-with-local-assets.py")]
    targets = [
        "tests/unit/plugins/infrastructure/clients/retrieval/test_chroma.py",
        "tests/integration/pipeline/test_rag_indexed_smoke.py",
        "tests/integration/pipeline/test_row_type_violation_routing.py",
        "tests/integration/plugins/transforms/test_rag_pipeline.py",
        "tests/integration/plugins/transforms/test_template_worker_loss.py",
    ]
elif MODE == "browser":
    cmd += ["-m", "pytest"]
    targets = [
        "tests/integration/web/test_composer_https_acceptance.py",
        "tests/integration/web/test_composer_react_generation_acceptance.py",
    ]
else:
    cmd += ["-m", "pytest", "-m", "testcontainer"]
    targets = ["tests/testcontainer/web/test_blob_mutation_admission_postgres.py"]
    if MODE == "pgsequence":
        targets = [
            "tests/testcontainer/web/test_schema_probe_postgres.py::test_landscape_database_construction_stamps_postgres_identity[from_url]",
            "tests/testcontainer/web/test_blob_mutation_admission_postgres.py::test_postgres_admission_waiting_on_container_sees_committed_output_reservation",
        ]
cmd += ["-n", "0", "-o", f"pythonpath={ROOT}/src {ROOT}/elspeth-lints/src", *targets]
PREFIX.with_suffix(".command.json").write_text(json.dumps({"cwd": str(ROOT), "argv": cmd, "runtime": {key: env[key] for key in ("PYTHONPATH", "PATH", "PLAYWRIGHT_BROWSERS_PATH")}}, indent=2))
with PREFIX.with_suffix(".log").open("w") as log:
    result = subprocess.run(cmd, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
PREFIX.with_suffix(".exit").write_text(f"{result.returncode}\n")
after = snapshot()
PREFIX.with_suffix(".after.json").write_text(json.dumps(after, sort_keys=True))
frozen = before == after
PREFIX.with_suffix(".frozen-exit").write_text("0\n" if frozen else "1\n")
print(json.dumps({"exit": result.returncode, "frozen": frozen}))
print("\n".join(PREFIX.with_suffix(".log").read_text().splitlines()[-30:]))
assert frozen
raise SystemExit(result.returncode)
