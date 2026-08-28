# `[str, Any]` mapping burn-down — widen the scanner, then drop the ratchet (2026-08-29)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `scripts/check_contracts.py` see every `dict|Mapping|MutableMapping[str, Any]` annotation in `src/elspeth` (all syntactic positions, `contracts/` included), rebaseline honestly, burn the baseline to zero by real type changes, then delete the whitelist mechanism so the check is a flat ban.

**Architecture:** Phase A widens the AST scan and replaces the 547-entry `allowed_dict_patterns` ratchet with a generated `allowed_str_any_mappings` baseline (measured 2026-08-29 at HEAD `ff917243a`: **2,228 sites / 282 files**; 1,282 `dict`, 933 `Mapping`, 13 `MutableMapping`, 7 string-form). Phase B is a fanout burn-down in four waves (`contracts/` first, because it defines the owned vocabulary everyone else reaches for), lanes deleting their own baseline lines. Phase C deletes the baseline section and the loader, leaving the ban.

**Tech Stack:** Python 3.13 `ast`, PyYAML, pytest, mypy `strict` (`.venv/bin/mypy src/elspeth`), pre-commit hook `check-contracts`, CI step "Enforce contract alignment".

**Spec:** This document is the spec (decision recorded in-session 2026-08-29: John chose "widen the scanner first, then burn" — *"I'd rather an honest bad than a partial good"* — over whitelist-to-zero-as-is and over deleting the check). Governing policy: [ADR-032](../../architecture/adr/032-validate-by-trust-domain.md) — nominally type what ELSPETH owns, parse what it does not.

## Global Constraints

- **What counts as removal.** A site is removed only by one of: (a) an owned type — `PipelineRow`, a frozen dataclass, a `TypedDict`, a Pydantic model, an existing `contracts/` type; (b) a *recursive* JSON vocabulary from `contracts/json_types.py` (Task 3) whose leaf is never `Any`; (c) at an honest Tier-3 parse boundary, `Mapping[str, object]` / `dict[str, object]` — the house precedent already in the whitelist (`config` param "now typed `object`", execution repository "narrowed to `Mapping[str, object]`"). `object` forces every reader to narrow; `Any` lets it lie.
- **What does NOT count.** Renaming `dict[str, Any]` → `Mapping[str, Any]` (both are scanned after Task 1); an alias `X = dict[str, Any]` anywhere (banned by Task 1, `mcp/types.py:297-303` is burned in B-mcp); `cast("dict[str, Any]", …)` (scanned); `# type: ignore`; widening a Protocol. Never alias, pad, reorder, or add dead code to move a site off the scan ([never shape code around a gate](../../../AGENTS.md)).
- **Scope of the scan is `src/elspeth/**/*.py` only.** `tests/` (2,828 lines today) and `scripts/` (131) are out of scope for the gate; lanes fix the tests that *exercise* the signatures they change and nothing more.
- **Baseline edits are deletion-only.** A lane may delete lines from `allowed_str_any_mappings`; it may never add one. Adding requires the operator (John) — say so in the lane's report and leave the site in place.
- Whole-tree AST gates: read `docs/agents/recent-code-hints.md` before touching code. Changing a plugin `__init__`/`probe_config` signature re-pins `source_file_hash` LAST via `scripts/cicd/plugin_hash.py`.
- Every lane: own worktree under `.claude/worktrees/strany-<bucket>`, `PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src`, verify `elspeth.__file__` before trusting any result, **`pytest -n 2` max per lane** (24 CPUs do not multiply across a fanout).
- Evidence per lane = **count** of baseline entries before/after for its files (never `tail`), scoped pytest green, `.venv/bin/mypy` on the touched modules clean, and a Filigree comment on the bucket issue listing removed vs. left-in-place sites. Deliverable is the tracker write, not the chat report.
- Model: `fable` for buckets touching auth/secrets/redaction/sessions/sinks/infra clients/AWS+Textract; `opus` otherwise.

---

## Phase A — widen the scanner (serial, one worker)

### Task 1: Widen `find_dict_violations` to the whole truth

**Files:**
- Modify: `scripts/check_contracts.py:205-300` (`_is_dict_str_any`, `_is_list_of_dict_str_any`, `_is_optional_dict`, `_is_union_with_dict`, `find_dict_patterns_in_file`), `:335-420` (`find_dict_violations`), `:140-170` (`load_whitelist`), `:633-660` (`validate_dict_pattern_entry`), `:1300-1345` (main scan loop — drop the `contracts/` skip for the dict scan only; keep it for the type-definition scan)
- Test: `tests/unit/scripts/test_check_contracts.py` (append; existing tests build files under `tmp_path` and call the module functions directly — follow that pattern)

**Interfaces:**
- Produces: `find_str_any_sites(file_path: Path) -> list[StrAnySite]` where
  ```python
  @dataclass(frozen=True)
  class StrAnySite:
      file: str            # path as given (relative, e.g. "src/elspeth/core/config.py")
      line: int
      context: str         # "Class.method" | "function" | "<module>"
      slot: str            # "param:<name>" | "return" | "local:<name>" | "field:<name>" | "alias:<name>" | "cast" | "string"
      spelling: str        # "dict" | "Mapping" | "MutableMapping"
      def key(self) -> str: return f"{self.file}:{self.context}:{self.slot}"
  ```
  Baseline key format: `file:context:slot` (three colon-separated parts; `slot` never contains a colon after the kind prefix). The old `(list)` suffix is retired — a `list[dict[str, Any]]` param is simply `param:<name>` because the *inner* subscript is the hit; nesting depth is irrelevant.
- Produces: `load_whitelist(path)` returns `{"types": set, "str_any": set}`; the `"dicts"` key and `allowed_dict_patterns` are gone. A file that still has `allowed_dict_patterns` makes `load_whitelist` raise `ValueError("allowed_dict_patterns was retired on 2026-08-29; regenerate with --write-baseline")`.
- Produces: CLI flag `--write-baseline` that rewrites the `allowed_str_any_mappings` list in `config/cicd/contracts-whitelist.yaml` from the live scan (sorted, one entry per line, **preserving the `allowed_external_types` section and its comments byte-for-byte** — do this by text-splicing the YAML at the `allowed_str_any_mappings:` key, not by `yaml.dump` of the whole document), exits 0, and prints `wrote N entries`.

- [ ] **Step 1: Write the failing detection tests**

```python
# tests/unit/scripts/test_check_contracts.py (append)
from scripts.check_contracts import StrAnySite, find_str_any_sites


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "mod.py"
    p.write_text(body)
    return p


def test_str_any_sites_every_position(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "from typing import Any, Mapping, MutableMapping, cast\n"
        "Alias = dict[str, Any]\n"
        "class C:\n"
        "    field: Mapping[str, Any]\n"
        "    def m(self, a: dict[str, Any], *, b: list[Mapping[str, Any]] | None) -> MutableMapping[str, Any]:\n"
        "        local: dict[str, Any] = {}\n"
        "        x = cast('dict[str, Any]', a)\n"
        "        y: 'Mapping[str, Any]' = {}\n"
        "        return local\n",
    )
    keys = sorted(s.key() for s in find_str_any_sites(p))
    assert keys == sorted(
        [
            f"{p}:<module>:alias:Alias",
            f"{p}:C:field:field",
            f"{p}:C.m:param:a",
            f"{p}:C.m:param:b",
            f"{p}:C.m:return",
            f"{p}:C.m:local:local",
            f"{p}:C.m:cast",
            f"{p}:C.m:local:y",
        ]
    )
    assert {s.spelling for s in find_str_any_sites(p)} == {"dict", "Mapping", "MutableMapping"}


def test_str_any_sites_ignore_honest_spellings(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        "from typing import Any, Mapping\n"
        "def f(a: dict[str, object], b: Mapping[str, str], c: dict[int, Any], d: Mapping[str, 'JsonValue']) -> dict[str, object]: ...\n",
    )
    assert find_str_any_sites(p) == []


def test_str_any_sites_typing_qualified(tmp_path: Path) -> None:
    p = _write(tmp_path, "import typing\ndef f(a: typing.Dict[str, typing.Any], b: typing.Mapping[str, typing.Any]): ...\n")
    assert [s.slot for s in find_str_any_sites(p)] == ["param:a", "param:b"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/unit/scripts/test_check_contracts.py -k str_any -v`
Expected: FAIL — `ImportError: cannot import name 'StrAnySite'`.

- [ ] **Step 3: Implement the widened walk**

Replace `_is_dict_str_any` / `_is_list_of_dict_str_any` / `_is_optional_dict` / `_is_union_with_dict` with one predicate and one collector:

```python
_MAPPING_NAMES = frozenset({"dict", "Dict", "Mapping", "MutableMapping"})


def _str_any_spelling(node: ast.AST) -> str | None:
    """Return the mapping spelling if ``node`` is ``<Mapping>[str, Any]``, else None."""
    if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Tuple) and len(node.slice.elts) == 2):
        return None
    base = node.value
    name = base.id if isinstance(base, ast.Name) else base.attr if isinstance(base, ast.Attribute) else None
    if name not in _MAPPING_NAMES:
        return None
    key_t, val_t = node.slice.elts
    if not (isinstance(key_t, ast.Name) and key_t.id == "str"):
        return None
    if isinstance(val_t, ast.Name) and val_t.id == "Any" or isinstance(val_t, ast.Attribute) and val_t.attr == "Any":
        return "dict" if name == "Dict" else name
    return None


def _hits_in(expr: ast.AST) -> list[str]:
    """Every ``[str, Any]`` mapping nested anywhere inside an annotation expression (list[...], | None, tuple[...] ...)."""
    return [s for n in ast.walk(expr) if (s := _str_any_spelling(n)) is not None]


def _hits_in_string(value: str) -> list[str]:
    try:
        return _hits_in(ast.parse(value, mode="eval").body)
    except SyntaxError:
        return []
```

The collector walks with a parent map (reuse the existing one), computes `context` exactly as today (`Class.method`, bare function, or `<module>`), and emits:

- `ast.arg` with annotation → `param:<arg>`; also walk `node.args.posonlyargs`, `vararg`, `kwarg` (today's code walks only `args + kwonlyargs`).
- function `returns` → `return`.
- `ast.AnnAssign` whose target is `ast.Name` → inside a class body: `field:<name>`; inside a function: `local:<name>`; at module level: `local:<name>`.
- `ast.Assign` at module or class level whose value is itself a hit → `alias:<name>` (this is the alias ban).
- `ast.Call` to `cast` / `typing.cast` whose first arg is a string or expression hit → `cast` (one site per call; the line disambiguates).
- Any `ast.Constant` str inside an annotation position (`arg.annotation`, `returns`, `AnnAssign.annotation`) → same slot as its position — do **not** scan arbitrary string constants (docstrings mention `dict[str, Any]`), only annotation-position strings and `cast` first args.

`find_dict_violations` becomes a thin wrapper: `[s for s in find_str_any_sites(p) if s.key() not in whitelist]`, marking matched entries as before. `find_dict_patterns_in_file` (used by stale validation) returns `{s.key() for s in find_str_any_sites(p)}`. Keep `DictViolation` for the report but populate `param_name` with the slot.

- [ ] **Step 4: Run to verify they pass**

Run: `.venv/bin/pytest tests/unit/scripts/test_check_contracts.py -v`
Expected: the three new tests PASS; existing tests that asserted `(list)` suffixes or `"dicts"` whitelist keys now FAIL — update those assertions to the new key format in the same commit (they pinned the old format, not a behaviour).

- [ ] **Step 5: Write the failing loader/baseline tests**

```python
def test_load_whitelist_rejects_retired_section(tmp_path: Path) -> None:
    wl = tmp_path / "wl.yaml"
    wl.write_text("allowed_external_types: []\nallowed_dict_patterns:\n  - 'a:b:c'\n")
    with pytest.raises(ValueError, match="retired on 2026-08-29"):
        load_whitelist(wl)


def test_write_baseline_preserves_types_section(tmp_path: Path) -> None:
    wl = tmp_path / "wl.yaml"
    wl.write_text("# keep me\nallowed_external_types:\n  # and me\n  - 'x/y:Z'\nallowed_str_any_mappings:\n  - 'stale:entry:here'\n")
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "m.py").write_text("from typing import Any\ndef f(a: dict[str, Any]): ...\n")
    write_baseline(wl, src)
    text = wl.read_text()
    assert text.startswith("# keep me\nallowed_external_types:\n  # and me\n  - 'x/y:Z'\n")
    assert "stale:entry:here" not in text
    assert f"{src / 'pkg' / 'm.py'}:f:param:a" in text
```

- [ ] **Step 6: Run to verify they fail, implement `write_baseline` + the `--write-baseline` flag + the `ValueError`, run to verify they pass**

Run: `.venv/bin/pytest tests/unit/scripts/test_check_contracts.py -v` → all PASS.

`write_baseline(whitelist_path: Path, src_dir: Path) -> int` scans `src_dir.rglob("*.py")` (no `contracts/` skip), collects keys, splits the existing file text at the line matching `^allowed_str_any_mappings:`, keeps everything before it verbatim, and writes `allowed_str_any_mappings:\n` + one `  - "<key>"\n` per sorted key (or `allowed_str_any_mappings: []\n` when empty). In `main()`, `--write-baseline` calls it and returns 0 after printing `wrote N entries`. Remove the `contracts_dir` skip from the **dict** scan loop only (`scripts/check_contracts.py:1300-1308`); the type-definition scan keeps skipping `contracts/`.

- [ ] **Step 7: Commit**

```bash
git add scripts/check_contracts.py tests/unit/scripts/test_check_contracts.py
git commit -m "feat(cicd): check_contracts scans every [str, Any] mapping in every position

Mapping/MutableMapping/typing-qualified spellings, locals, class fields,
module aliases, cast() targets and string annotations are all sites now;
contracts/ is no longer skipped. allowed_dict_patterns is retired in
favour of a generated allowed_str_any_mappings baseline (--write-baseline)."
```

### Task 2: Rebaseline honestly and re-green the gate

**Files:**
- Modify: `config/cicd/contracts-whitelist.yaml` (delete the whole `allowed_dict_patterns:` section — its comments are rationale for a ratchet that no longer exists; the honest categories they name are re-expressed as *types* in Task 3 — then generate `allowed_str_any_mappings`)
- Modify: `docs/agents/recent-code-hints.md:2174-2175` (the `__init__:config` / `probe_config:return` hint now reads: "the `check-contracts` hook flags ANY `[str, Any]` mapping annotation; a new site is not whitelistable by an agent — type it (`Mapping[str, object]` at a Tier-3 boundary, an owned type elsewhere)")
- Modify: `.pre-commit-config.yaml:224-229` comment: the hook is expected to be **green** now (baseline), and stays green only by deletion.

- [ ] **Step 1: Delete the old section and generate the baseline**

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
p = Path("config/cicd/contracts-whitelist.yaml"); t = p.read_text()
head, _, _ = t.partition("allowed_dict_patterns:")
p.write_text(head.rstrip("\n") + "\nallowed_str_any_mappings: []\n")
PY
.venv/bin/python scripts/check_contracts.py --write-baseline
```
Expected: `wrote 2228 entries` (±: any commit after `ff917243a` shifts this — record the actual number in the commit message).

- [ ] **Step 2: Verify the gate is green and the count is the truth**

```bash
.venv/bin/python scripts/check_contracts.py; echo rc=$?
grep -c '^  - "src/elspeth/' config/cicd/contracts-whitelist.yaml
.venv/bin/pytest tests/unit/scripts/test_check_contracts.py tests/unit/core/test_config_alignment.py -q
```
Expected: rc=0, all `✅` lines, count equals the `wrote N` figure, tests green.

- [ ] **Step 3: Commit (this is the "honest bad")**

```bash
git add config/cicd/contracts-whitelist.yaml docs/agents/recent-code-hints.md .pre-commit-config.yaml
git commit -m "chore(cicd): rebaseline [str, Any] mappings at the true count (N sites / M files)"
```

### Task 3: Own the vocabulary — `contracts/json_types.py` (Wave 0 prerequisite, serial)

Every later lane reaches for these; they must exist and be reviewed before the fanout. This task also burns the `contracts/` buckets B01–B05 (163 sites), because whoever defines the vocabulary should apply it to the contracts that carry it.

**Files:**
- Create: `src/elspeth/contracts/json_types.py`
- Modify: `src/elspeth/contracts/__init__.py` (export), the five `contracts/` buckets in the manifest (`errors.py` ×23, `call_data.py` ×20, `plugin_protocols.py` ×18, `schema.py` ×14, …)
- Test: `tests/unit/contracts/test_json_types.py`

**Interfaces (Produces):**
```python
# src/elspeth/contracts/json_types.py
"""Owned JSON vocabulary. Leaves are never Any — a reader narrows with isinstance, never assumes.

Trust posture (ADR-032): a JsonObject is *parsed* data. It says "this came from json.loads /
model_dump / a YAML file and has JSON shape"; it says nothing about which keys exist. Sites
that know the keys use a TypedDict or dataclass instead. Sites at a Tier-3 boundary that have
not yet parsed use Mapping[str, object].
"""
from __future__ import annotations
from collections.abc import Mapping, Sequence
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = "JsonScalar | Sequence[JsonValue] | Mapping[str, JsonValue]"
JsonObject: TypeAlias = Mapping[str, "JsonValue"]          # read-only view — the default
MutableJsonObject: TypeAlias = dict[str, "JsonValue"]      # only where the site genuinely mutates
JsonArray: TypeAlias = Sequence["JsonValue"]


def as_json_object(value: object, *, where: str) -> JsonObject:
    """Tier-3 narrowing: raise ValueError naming ``where`` unless ``value`` is a str-keyed mapping."""
    if not isinstance(value, Mapping) or any(not isinstance(k, str) for k in value):
        raise ValueError(f"{where}: expected a JSON object, got {type(value).__name__}")
    return value
```
Unit tests: `as_json_object` accepts `{}` and `{"a": [1, {"b": None}]}`, rejects `[]`, `{1: 2}`, `None` with the `where` text in the message; mypy strict accepts a function returning `JsonObject` from `json.loads(...)` only through `as_json_object` (a `reveal_type` test is unnecessary — the `tests/unit/contracts` mypy run in the suite covers it).

- [ ] Steps: failing tests → run → implement → run → burn B01–B05 using the Global Constraints removal rules (each site: owned type if the keys are known — `contracts/errors.py`'s error-detail payloads and `call_data.py`'s request/response bodies are the two big decisions; write the TypedDict where the producer is ELSPETH, `JsonObject` where the producer is a vendor SDK, `Mapping[str, object]` where the value is unparsed) → delete the burned lines from `allowed_str_any_mappings` → `.venv/bin/mypy src/elspeth/contracts` clean → `pytest tests/unit/contracts -n 2` green → commit per bucket:

```bash
git add src/elspeth/contracts/json_types.py src/elspeth/contracts/__init__.py tests/unit/contracts/test_json_types.py
git commit -m "feat(contracts): owned JSON vocabulary (JsonValue/JsonObject) with a Tier-3 narrowing helper"
# then one commit per bucket B01..B05:
git commit -m "refactor(contracts): B0N — replace [str, Any] mappings with owned types (-K baseline entries)"
```

- [ ] After B05: run the **full suite** in the worktree as a background job (`pytest tests/ -n 12`) before opening the fanout — Wave 0 changes signatures every wave depends on.

---

## Phase B — burn-down fanout (Waves 1–3)

### Task 4: File the tracker skeleton

- [ ] Create one Filigree epic `[str, Any] burn-down: drop the check_contracts ratchet` and one task per bucket in `2026-08-29-str-any-mapping-burndown.buckets.json` (B06 onward), title `strany B<nn>: <area> (<sites> sites / <loc> LOC)`, label `strany-burndown`, dependency on the epic; Wave 2/3 buckets depend on **B01–B05** (Task 3), not on each other. Record the epic id at the top of this plan.

### Task 5: Run the waves

Lane brief (copy verbatim into every subagent prompt, then append the bucket's file table):

> CWD: `/home/john/elspeth/.claude/worktrees/strany-B<nn>` (create it: `git worktree add .claude/worktrees/strany-B<nn> -b strany/B<nn> feature/unified-lineage`; symlink `.venv` from the main checkout; `export PYTHONPATH=$PWD/src:$PWD/elspeth-lints/src`; verify `python -c 'import elspeth; print(elspeth.__file__)'` prints the worktree). Read `docs/agents/recent-code-hints.md`, ADR-032, and the **Global Constraints** section of `docs/superpowers/plans/2026-08-29-str-any-mapping-burndown.md` first. For every `allowed_str_any_mappings` entry whose file is in your bucket: replace the annotation by the removal rules (owned type > `JsonObject`/`JsonValue` from `elspeth.contracts.json_types` > `Mapping[str, object]` at a Tier-3 boundary), fix the callers and the tests that exercise the changed signature, delete the entry's line from `config/cicd/contracts-whitelist.yaml`. Never add a baseline line, never alias, never `Mapping[str, Any]`, never `# type: ignore`. If a site is genuinely un-typeable, leave it and its line in place and say so in your report with the reason. Gate per commit: `.venv/bin/python scripts/check_contracts.py` rc=0; `.venv/bin/mypy <touched modules>` clean; `pytest <the tests for touched modules> -n 2` green. Commit per file or per coherent group. Deliverable = Filigree comment on `elspeth-<bucket issue>` listing: entries before/after (count), removed keys, left-in-place keys with reasons, commits. Then report the same via SendMessage to `team-lead`.

- [ ] **Wave 1** (plugins / core / engine / mcp / telemetry / tui / testing — 32 buckets, 693 sites): dispatch all buckets concurrently, `fable` on `plugins/sinks`, `plugins/sources`, `plugins/infrastructure`, `plugins/transforms/aws`, `plugins/transforms/llm`, `core` (secrets/config), `mcp`; `opus` otherwise.
- [ ] **Wave-1 merge**: `git merge --no-ff` each lane branch into `feature/unified-lineage` in bucket order; after the last merge run `.venv/bin/python scripts/check_contracts.py` (rc=0) and `--write-baseline` on a throwaway copy to prove **no new entries appeared** (`diff` the regenerated list against the committed one: only the committed one may have extra lines… it may not; they must be identical). Full suite as a background job in a worktree.
- [ ] **Wave 2** (`web/composer`, `web/sessions`, `composer_mcp` — 27 buckets, 1,207 sites): same dispatch; `fable` on `redaction.py`, `sessions/*`, `secrets`, `auth`, `blobs`. These files are the composer's authoring path — **Composer invariants apply**: a type change must not add a server-side path around the provider; if a `dict[str, Any]` is a planner tool payload, the owned type is the wire shape the planner already emits, nothing narrower.
- [ ] **Wave-2 merge**: as Wave 1, plus the composer parity gate (`scripts/cicd/parity_harness.py`) and the whole-tree wire-shape gates named in recent-code-hints.
- [ ] **Wave 3** (`web/*` remainder, `web/plugin_policy`, `web/execution`, `web/catalog`, `web/auth` — 11 buckets, 165 sites): same; `fable` on `auth`, `secrets`.
- [ ] **Wave-3 merge**: as above; full suite; `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only` (type changes at Tier-3 boundaries are exactly what it audits).

### Task 6: Residue round

- [ ] Collect every "left in place" key from the bucket comments into one list. For each, John adjudicates: fix now (second, small lane) or declare it an honest `Mapping[str, object]`/`JsonObject` conversion the lane was too cautious to make. Target after this round: **0 entries**. There is no third category — the ratchet is dropped, not shrunk.

---

## Phase C — drop the ratchet

### Task 7: Delete the whitelist mechanism

**Files:**
- Modify: `scripts/check_contracts.py` — remove `allowed_str_any_mappings` loading, `--write-baseline`, `write_baseline`, `validate_dict_pattern_entry`, and the `matched_entries` plumbing for str-any; `find_dict_violations` returns every site; the report's "Fix:" line reads `Use an owned type, JsonObject, or Mapping[str, object] at a Tier-3 boundary`.
- Modify: `config/cicd/contracts-whitelist.yaml` — delete the `allowed_str_any_mappings: []` key (a file with the key present makes `load_whitelist` raise, mirroring Task 1's guard for the older key).
- Modify: `tests/unit/scripts/test_check_contracts.py` — delete the baseline tests; add `test_str_any_site_is_always_a_violation`.
- Modify: `docs/agents/recent-code-hints.md`, `.pre-commit-config.yaml:224-229`, `AGENTS.md` Gotchas (one bullet: "`[str, Any]` mappings are banned in `src/elspeth` in every position; there is no whitelist").

- [ ] Failing test → implement → `.venv/bin/python scripts/check_contracts.py` rc=0 with a **zero-entry** report → full suite → commit:

```bash
git commit -m "chore(cicd): drop the [str, Any] whitelist — the check is a flat ban"
```

---

## Bucket manifest

Machine-readable: `2026-08-29-str-any-mapping-burndown.buckets.json` (this table is generated from it; regenerate both with `2026-08-29-str-any-mapping-burndown-tools/{widescan,bucket}.py` (run from the repo root: `python widescan.py hits.json && python bucket.py > buckets.md`) if HEAD moves before dispatch). Waves: 0 = `contracts/` (Task 3, serial); 1 = plugins/core/engine/mcp/telemetry/tui/testing; 2 = `web/composer`, `web/sessions`, `composer_mcp`; 3 = other `web/*`. ≤5,000 LOC per bucket, a file is never split (`web/sessions/service.py` at 13,600 LOC and `web/composer/service.py` at 9,503 LOC each stand alone).


### Wave 0 — 5 buckets, 163 sites, 18094 LOC

| Bucket | LOC | Sites | Files (sites) |
|---|---:|---:|---|
| B01 | 4052 | 24 | `contracts/errors.py` (2222 LOC; ×23)<br>`contracts/audit.py` (1830 LOC; ×1) |
| B02 | 4999 | 38 | `contracts/runtime_val_manifest.py` (1566 LOC; ×1)<br>`contracts/declaration_contracts.py` (1371 LOC; ×5)<br>`contracts/plugin_protocols.py` (1053 LOC; ×18)<br>`contracts/schema.py` (1009 LOC; ×14) |
| B03 | 4964 | 54 | `contracts/results.py` (862 LOC; ×2)<br>`contracts/schema_contract.py` (757 LOC; ×7)<br>`contracts/events.py` (692 LOC; ×4)<br>`contracts/plugin_context.py` (636 LOC; ×8)<br>`contracts/data.py` (469 LOC; ×3)<br>`contracts/composer_llm_audit.py` (416 LOC; ×2)<br>`contracts/call_data.py` (395 LOC; ×20)<br>`contracts/contract_records.py` (370 LOC; ×1)<br>`contracts/barrier_scalars.py` (367 LOC; ×7) |
| B04 | 3009 | 44 | `contracts/node_state_context.py` (339 LOC; ×9)<br>`contracts/coalesce_metadata.py` (323 LOC; ×3)<br>`contracts/run_result.py` (302 LOC; ×1)<br>`contracts/contexts.py` (245 LOC; ×9)<br>`contracts/contract_propagation.py` (227 LOC; ×2)<br>`contracts/composer_audit.py` (218 LOC; ×1)<br>`contracts/composer_planner_audit.py` (213 LOC; ×1)<br>`contracts/secret_scrub.py` (212 LOC; ×3)<br>`contracts/contract_builder.py` (194 LOC; ×3)<br>`contracts/transform_contract.py` (171 LOC; ×1)<br>`contracts/chat_parts.py` (169 LOC; ×7)<br>`contracts/checkpoint.py` (154 LOC; ×1)<br>`contracts/diversion.py` (86 LOC; ×1)<br>`contracts/preflight.py` (85 LOC; ×1)<br>`contracts/audit_evidence.py` (71 LOC; ×1) |
| B05 | 1070 | 3 | `contracts/config/runtime.py` (724 LOC; ×2)<br>`contracts/config/protocols.py` (346 LOC; ×1) |

### Wave 1 — 32 buckets, 693 sites, 97425 LOC

| Bucket | LOC | Sites | Files (sites) |
|---|---:|---:|---|
| B06 | 4042 | 4 | `cli.py` (4042 LOC; ×4) |
| B07 | 3400 | 20 | `core/config.py` (3400 LOC; ×20) |
| B08 | 4968 | 38 | `core/schema_shape.py` (2497 LOC; ×8)<br>`core/expression_parser.py` (1038 LOC; ×2)<br>`core/blobs_inline.py` (715 LOC; ×23)<br>`core/secrets.py` (396 LOC; ×4)<br>`core/canonical.py` (322 LOC; ×1) |
| B09 | 748 | 13 | `core/operations.py` (218 LOC; ×2)<br>`core/logging.py` (202 LOC; ×2)<br>`core/template_materialization.py` (185 LOC; ×6)<br>`core/dependency_config.py` (143 LOC; ×3) |
| B10 | 2547 | 3 | `core/dag/builder.py` (1977 LOC; ×1)<br>`core/dag/models.py` (490 LOC; ×1)<br>`core/dag/schema_factory.py` (80 LOC; ×1) |
| B11 | 3913 | 6 | `core/landscape/database.py` (2056 LOC; ×1)<br>`core/landscape/run_lifecycle_repository.py` (1857 LOC; ×5) |
| B12 | 3498 | 23 | `core/landscape/exporter.py` (1164 LOC; ×5)<br>`core/landscape/query_repository.py` (951 LOC; ×2)<br>`core/landscape/execution/batches.py` (562 LOC; ×1)<br>`core/landscape/write_repository.py` (281 LOC; ×3)<br>`core/landscape/execution/artifacts.py` (242 LOC; ×3)<br>`core/landscape/row_data.py` (150 LOC; ×2)<br>`core/landscape/formatters.py` (75 LOC; ×6)<br>`core/landscape/serialization.py` (73 LOC; ×1) |
| B13 | 5492 | 1 | `engine/processor.py` (5492 LOC; ×1) |
| B14 | 3100 | 21 | `engine/coalesce_executor.py` (1664 LOC; ×6)<br>`engine/tokens.py` (656 LOC; ×2)<br>`engine/spans.py` (633 LOC; ×6)<br>`engine/commencement.py` (147 LOC; ×7) |
| B15 | 2484 | 15 | `engine/executors/aggregation.py` (1023 LOC; ×5)<br>`engine/executors/transform.py` (969 LOC; ×6)<br>`engine/executors/state_guard.py` (492 LOC; ×4) |
| B16 | 976 | 5 | `engine/orchestrator/validation.py` (359 LOC; ×1)<br>`engine/orchestrator/schema_reconstruction.py` (329 LOC; ×2)<br>`engine/orchestrator/types.py` (288 LOC; ×2) |
| B17 | 1974 | 37 | `mcp/server.py` (993 LOC; ×11)<br>`mcp/types.py` (775 LOC; ×24)<br>`mcp/analyzer.py` (206 LOC; ×2) |
| B18 | 2537 | 9 | `mcp/analyzers/queries.py` (1295 LOC; ×6)<br>`mcp/analyzers/reports.py` (763 LOC; ×1)<br>`mcp/analyzers/diagnostics.py` (479 LOC; ×2) |
| B19 | 4483 | 37 | `plugins/infrastructure/base.py` (2423 LOC; ×18)<br>`plugins/infrastructure/clients/http.py` (1126 LOC; ×15)<br>`plugins/infrastructure/clients/dataverse.py` (934 LOC; ×4) |
| B20 | 4799 | 41 | `plugins/infrastructure/clients/llm.py` (801 LOC; ×4)<br>`plugins/infrastructure/config_base.py` (784 LOC; ×2)<br>`plugins/infrastructure/pooling/executor.py` (664 LOC; ×6)<br>`plugins/infrastructure/clients/verifier.py` (566 LOC; ×14)<br>`plugins/infrastructure/clients/retrieval/azure_search.py` (548 LOC; ×9)<br>`plugins/infrastructure/batching/mixin.py` (514 LOC; ×1)<br>`plugins/infrastructure/clients/retrieval/chroma.py` (508 LOC; ×4)<br>`plugins/infrastructure/batching/row_reorder_buffer.py` (414 LOC; ×1) |
| B21 | 1896 | 27 | `plugins/infrastructure/validation.py` (331 LOC; ×6)<br>`plugins/infrastructure/manager.py` (321 LOC; ×3)<br>`plugins/infrastructure/clients/replayer.py` (305 LOC; ×8)<br>`plugins/infrastructure/display_headers.py` (296 LOC; ×3)<br>`plugins/infrastructure/schema_factory.py` (205 LOC; ×1)<br>`plugins/infrastructure/probe_factory.py` (139 LOC; ×1)<br>`plugins/infrastructure/clients/json_utils.py` (99 LOC; ×2)<br>`plugins/infrastructure/clients/retrieval/base.py` (77 LOC; ×1)<br>`plugins/infrastructure/clients/retrieval/types.py` (62 LOC; ×1)<br>`plugins/infrastructure/utils.py` (61 LOC; ×1) |
| B22 | 4707 | 35 | `plugins/sinks/aws_s3_sink.py` (1212 LOC; ×8)<br>`plugins/sinks/database_sink.py` (1117 LOC; ×9)<br>`plugins/sinks/azure_blob_sink.py` (936 LOC; ×9)<br>`plugins/sinks/chroma_sink.py` (724 LOC; ×4)<br>`plugins/sinks/dataverse.py` (718 LOC; ×5) |
| B23 | 2079 | 12 | `plugins/sinks/json_sink.py` (626 LOC; ×2)<br>`plugins/sinks/csv_sink.py` (604 LOC; ×3)<br>`plugins/sinks/document_sink.py` (463 LOC; ×4)<br>`plugins/sinks/text_sink.py` (386 LOC; ×3) |
| B24 | 4507 | 21 | `plugins/sources/aws_s3_source.py` (1481 LOC; ×7)<br>`plugins/sources/azure_blob_source.py` (1252 LOC; ×3)<br>`plugins/sources/dataverse.py` (1021 LOC; ×8)<br>`plugins/sources/json_source.py` (753 LOC; ×3) |
| B25 | 2424 | 18 | `plugins/sources/llm/source.py` (705 LOC; ×7)<br>`plugins/sources/csv_source.py` (702 LOC; ×1)<br>`plugins/sources/text_source.py` (320 LOC; ×2)<br>`plugins/sources/llm/config.py` (304 LOC; ×5)<br>`plugins/sources/blob_rows.py` (269 LOC; ×2)<br>`plugins/sources/null_source.py` (124 LOC; ×1) |
| B26 | 4138 | 25 | `plugins/transforms/llm/transform.py` (1999 LOC; ×15)<br>`plugins/transforms/aws/textract_document_analysis.py` (1090 LOC; ×8)<br>`plugins/transforms/web_scrape.py` (1049 LOC; ×2) |
| B27 | 4248 | 30 | `plugins/transforms/blob_json_expand.py` (943 LOC; ×6)<br>`plugins/transforms/field_mapper.py` (844 LOC; ×3)<br>`plugins/transforms/azure/document_intelligence.py` (840 LOC; ×6)<br>`plugins/transforms/llm/providers/gateway.py` (823 LOC; ×10)<br>`plugins/transforms/blob_csv_expand.py` (798 LOC; ×5) |
| B28 | 4929 | 100 | `plugins/transforms/aws/textract_client.py` (784 LOC; ×17)<br>`plugins/transforms/aws/textract_result.py` (768 LOC; ×63)<br>`plugins/transforms/aws/textract_inline_analysis.py` (735 LOC; ×5)<br>`plugins/transforms/pdf_rasterize.py` (735 LOC; ×4)<br>`plugins/transforms/llm/base.py` (680 LOC; ×1)<br>`plugins/transforms/llm/providers/openrouter.py` (620 LOC; ×8)<br>`plugins/transforms/blob_fetch.py` (607 LOC; ×2) |
| B29 | 4609 | 23 | `plugins/transforms/batch_stats.py` (597 LOC; ×2)<br>`plugins/transforms/json_explode.py` (597 LOC; ×3)<br>`plugins/transforms/batch_outlier_annotator.py` (592 LOC; ×2)<br>`plugins/transforms/batch_drift_compare.py` (579 LOC; ×2)<br>`plugins/transforms/azure/base.py` (573 LOC; ×2)<br>`plugins/transforms/batch_paired_preference.py` (562 LOC; ×2)<br>`plugins/transforms/blob_text_expand.py` (559 LOC; ×3)<br>`plugins/transforms/reference_join.py` (550 LOC; ×7) |
| B30 | 4651 | 26 | `plugins/transforms/type_coerce.py` (550 LOC; ×3)<br>`plugins/transforms/batch_classifier_metrics.py` (548 LOC; ×2)<br>`plugins/transforms/value_transform.py` (541 LOC; ×2)<br>`plugins/transforms/batch_experiment_compare.py` (537 LOC; ×2)<br>`plugins/transforms/rag/transform.py` (529 LOC; ×6)<br>`plugins/transforms/line_explode.py` (522 LOC; ×3)<br>`plugins/transforms/batch_effect_size.py` (508 LOC; ×2)<br>`plugins/transforms/batch_distribution_profile.py` (504 LOC; ×2)<br>`plugins/transforms/azure/content_safety.py` (412 LOC; ×4) |
| B31 | 4776 | 41 | `plugins/transforms/aws/textract_bucket_region.py` (411 LOC; ×3)<br>`plugins/transforms/batch_replicate.py` (404 LOC; ×4)<br>`plugins/transforms/keyword_filter.py` (373 LOC; ×2)<br>`plugins/transforms/llm/multi_query.py` (370 LOC; ×4)<br>`plugins/transforms/llm/providers/azure.py` (363 LOC; ×2)<br>`plugins/transforms/batch_threshold_summary.py` (360 LOC; ×2)<br>`plugins/transforms/azure/prompt_shield.py` (343 LOC; ×2)<br>`plugins/transforms/batch_top_k.py` (343 LOC; ×2)<br>`plugins/transforms/batch_data_quality_report.py` (340 LOC; ×2)<br>`plugins/transforms/report_assemble.py` (313 LOC; ×2)<br>`plugins/transforms/llm/provider.py` (310 LOC; ×1)<br>`plugins/transforms/truncate.py` (286 LOC; ×2)<br>`plugins/transforms/llm/langfuse.py` (280 LOC; ×8)<br>`plugins/transforms/llm/templates.py` (280 LOC; ×5) |
| B32 | 1767 | 19 | `plugins/transforms/llm/validation.py` (276 LOC; ×4)<br>`plugins/transforms/aws/_guardrail_transform.py` (272 LOC; ×2)<br>`plugins/transforms/llm/providers/bedrock.py` (251 LOC; ×2)<br>`plugins/transforms/rag/query.py` (225 LOC; ×2)<br>`plugins/transforms/llm/tracing.py` (208 LOC; ×1)<br>`plugins/transforms/rag/config.py` (155 LOC; ×1)<br>`plugins/transforms/passthrough.py` (119 LOC; ×2)<br>`plugins/transforms/aws/bedrock_content_safety.py` (104 LOC; ×1)<br>`plugins/transforms/azure/document_intelligence_result.py` (79 LOC; ×3)<br>`plugins/transforms/aws/bedrock_prompt_shield.py` (78 LOC; ×1) |
| B33 | 396 | 5 | `telemetry/serialization.py` (248 LOC; ×4)<br>`telemetry/protocols.py` (148 LOC; ×1) |
| B34 | 1807 | 9 | `telemetry/exporters/otlp.py` (620 LOC; ×3)<br>`telemetry/exporters/azure_monitor.py` (505 LOC; ×3)<br>`telemetry/exporters/datadog.py` (430 LOC; ×1)<br>`telemetry/exporters/console.py` (252 LOC; ×2) |
| B35 | 866 | 21 | `testing/__init__.py` (866 LOC; ×21) |
| B36 | 273 | 1 | `tui/types.py` (273 LOC; ×1) |
| B37 | 391 | 7 | `tui/widgets/node_detail.py` (391 LOC; ×7) |

### Wave 2 — 27 buckets, 1207 sites, 120837 LOC

| Bucket | LOC | Sites | Files (sites) |
|---|---:|---:|---|
| B38 | 1526 | 14 | `composer_mcp/server.py` (1147 LOC; ×12)<br>`composer_mcp/session.py` (379 LOC; ×2) |
| B39 | 9503 | 65 | `web/composer/service.py` (9503 LOC; ×65) |
| B40 | 8295 | 36 | `web/composer/state.py` (8295 LOC; ×36) |
| B41 | 5036 | 78 | `web/composer/pipeline_planner.py` (5036 LOC; ×78) |
| B42 | 4353 | 27 | `web/composer/redaction.py` (4353 LOC; ×27) |
| B43 | 3947 | 79 | `web/composer/guided/chat_solver.py` (3947 LOC; ×79) |
| B44 | 3920 | 99 | `web/composer/guided/planning.py` (3920 LOC; ×99) |
| B45 | 3641 | 36 | `web/composer/tools/generation.py` (3641 LOC; ×36) |
| B46 | 3487 | 76 | `web/composer/tools/_common.py` (3487 LOC; ×76) |
| B47 | 2830 | 22 | `web/composer/tools/sessions.py` (2830 LOC; ×22) |
| B48 | 2711 | 8 | `web/composer/planner_authoring_aids.py` (2711 LOC; ×8) |
| B49 | 4734 | 55 | `web/composer/guided/protocol.py` (2413 LOC; ×31)<br>`web/composer/tool_batch.py` (2321 LOC; ×24) |
| B50 | 4500 | 21 | `web/composer/guided/deferred_intents.py` (2307 LOC; ×4)<br>`web/composer/tools/blobs.py` (2193 LOC; ×17) |
| B51 | 3761 | 62 | `web/composer/tools/sources.py` (1921 LOC; ×35)<br>`web/composer/tools/transforms.py` (1840 LOC; ×27) |
| B52 | 4108 | 48 | `web/composer/protocol.py` (1386 LOC; ×2)<br>`web/composer/guided/state_machine.py` (1381 LOC; ×16)<br>`web/composer/guided/stage_transitions.py` (1341 LOC; ×30) |
| B53 | 4589 | 34 | `web/composer/audit.py` (1328 LOC; ×11)<br>`web/composer/source_inspection.py` (1218 LOC; ×6)<br>`web/composer/no_tool_policy.py` (1049 LOC; ×1)<br>`web/composer/llm_response_parsing.py` (994 LOC; ×16) |
| B54 | 4302 | 134 | `web/composer/required_controls.py` (991 LOC; ×13)<br>`web/composer/tools/_dispatch.py` (921 LOC; ×13)<br>`web/composer/guided/emitters.py` (842 LOC; ×6)<br>`web/composer/pipeline_proposal.py` (797 LOC; ×41)<br>`web/composer/tools/schema_contract.py` (751 LOC; ×61) |
| B55 | 4535 | 45 | `web/composer/guided/stage_subjects.py` (726 LOC; ×1)<br>`web/composer/tutorial_service.py` (725 LOC; ×3)<br>`web/composer/yaml_importer.py` (715 LOC; ×8)<br>`web/composer/yaml_generator.py` (663 LOC; ×22)<br>`web/composer/prompts.py` (639 LOC; ×5)<br>`web/composer/pipeline_commit.py` (589 LOC; ×3)<br>`web/composer/implicit_decisions.py` (478 LOC; ×3) |
| B56 | 4792 | 70 | `web/composer/pipeline_custody.py` (467 LOC; ×6)<br>`web/composer/_producer_resolver.py` (399 LOC; ×1)<br>`web/composer/source_demand.py` (374 LOC; ×8)<br>`web/composer/guided/resolved.py` (367 LOC; ×13)<br>`web/composer/tools/secrets.py` (345 LOC; ×3)<br>`web/composer/tools/outputs.py` (342 LOC; ×7)<br>`web/composer/tools/declarations.py` (340 LOC; ×2)<br>`web/composer/_compose_loop_carriers.py` (318 LOC; ×2)<br>`web/composer/guided/intent_management.py` (296 LOC; ×1)<br>`web/composer/capability_skill.py` (276 LOC; ×9)<br>`web/composer/guided/audit.py` (275 LOC; ×5)<br>`web/composer/audit_storage.py` (271 LOC; ×2)<br>`web/composer/turn_audit.py` (256 LOC; ×5)<br>`web/composer/reviewed_source_authority.py` (236 LOC; ×3)<br>`web/composer/guided/_discovery.py` (230 LOC; ×3) |
| B57 | 1073 | 22 | `web/composer/guided/prompts.py` (211 LOC; ×1)<br>`web/composer/tools/_registry.py` (199 LOC; ×1)<br>`web/composer/proposals.py` (128 LOC; ×4)<br>`web/composer/control_messages.py` (101 LOC; ×1)<br>`web/composer/authority_hashing.py` (98 LOC; ×4)<br>`web/composer/guided/profile.py` (90 LOC; ×2)<br>`web/composer/discovery_cache.py` (74 LOC; ×1)<br>`web/composer/tutorial_models.py` (74 LOC; ×1)<br>`web/composer/_validation_probe.py` (59 LOC; ×3)<br>`web/composer/tool_error_payloads.py` (39 LOC; ×4) |
| B58 | 13600 | 74 | `web/sessions/service.py` (13600 LOC; ×74) |
| B59 | 5291 | 16 | `web/sessions/routes/composer/guided.py` (5291 LOC; ×16) |
| B60 | 3448 | 55 | `web/sessions/protocol.py` (3448 LOC; ×55) |
| B61 | 3404 | 4 | `web/sessions/routes/_helpers.py` (3404 LOC; ×4) |
| B62 | 4184 | 3 | `web/sessions/routes/composer/guided_chat_atomic.py` (2030 LOC; ×1)<br>`web/sessions/schemas.py` (1086 LOC; ×1)<br>`web/sessions/routes/messages.py` (1068 LOC; ×1) |
| B63 | 4942 | 21 | `web/sessions/routes/composer/state.py` (1003 LOC; ×2)<br>`web/sessions/routes/composer/guided_chat_intent_management.py` (944 LOC; ×1)<br>`web/sessions/routes/sessions.py` (752 LOC; ×5)<br>`web/sessions/routes/composer/compose.py` (696 LOC; ×1)<br>`web/sessions/routes/composer/proposals.py` (518 LOC; ×4)<br>`web/sessions/guided_replay.py` (467 LOC; ×3)<br>`web/sessions/guided_audit.py` (340 LOC; ×3)<br>`web/sessions/proposal_blob_refs.py` (222 LOC; ×2) |
| B64 | 325 | 3 | `web/sessions/_persist_payload.py` (192 LOC; ×1)<br>`web/sessions/converters.py` (89 LOC; ×1)<br>`web/sessions/guided_operations.py` (44 LOC; ×1) |

### Wave 3 — 11 buckets, 165 sites, 20531 LOC

| Bucket | LOC | Sites | Files (sites) |
|---|---:|---:|---|
| B65 | 3846 | 36 | `web/interpretation_state.py` (2356 LOC; ×28)<br>`web/doctor.py` (675 LOC; ×1)<br>`web/readiness.py` (544 LOC; ×2)<br>`web/provider_config_policy.py` (271 LOC; ×5) |
| B66 | 662 | 4 | `web/_aws_ecs_acceptance/s3.py` (366 LOC; ×3)<br>`web/_aws_ecs_acceptance/http_client.py` (296 LOC; ×1) |
| B67 | 795 | 15 | `web/auth/oidc.py` (656 LOC; ×13)<br>`web/auth/entra.py` (139 LOC; ×2) |
| B68 | 3394 | 3 | `web/blobs/service.py` (3394 LOC; ×3) |
| B69 | 838 | 14 | `web/catalog/service.py` (593 LOC; ×9)<br>`web/catalog/schemas.py` (144 LOC; ×2)<br>`web/catalog/schema_parse.py` (101 LOC; ×3) |
| B70 | 158 | 1 | `web/coordination/run_diagnostics_authority.py` (158 LOC; ×1) |
| B71 | 3346 | 19 | `web/execution/service.py` (3346 LOC; ×19) |
| B72 | 4737 | 42 | `web/execution/routes.py` (1852 LOC; ×1)<br>`web/execution/diagnostics.py` (805 LOC; ×1)<br>`web/execution/preflight.py` (724 LOC; ×37)<br>`web/execution/validation.py` (694 LOC; ×2)<br>`web/execution/_validation_materialization.py` (662 LOC; ×1) |
| B73 | 1098 | 9 | `web/execution/fanout_guard.py` (615 LOC; ×6)<br>`web/execution/completion_gates.py` (288 LOC; ×1)<br>`web/execution/protocol.py` (195 LOC; ×2) |
| B74 | 1254 | 17 | `web/plugin_policy/profiles.py` (1254 LOC; ×17) |
| B75 | 403 | 5 | `web/secrets/user_store.py` (403 LOC; ×5) |
