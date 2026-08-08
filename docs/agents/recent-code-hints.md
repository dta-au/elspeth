# Recent code hints — READ BEFORE WRITING CODE

**Audience: agents. This is a rolling document.** It exists because agents keep
landing commits that pass their scoped test run and then break whole-tree
gates for every sibling on the branch (most recently 7201beeb7 →
elspeth-62a5aa4da8). Each entry is dated; when you land a new convention or a
new whole-tree trap, ADD IT HERE in the same commit. Prune entries once they
are covered by permanent docs or no longer bite. No sign-off ceremony — this
is a working document under the normal delivery posture.

## Whole-tree gates: a green scoped run proves NOTHING

These gates assert over the ENTIRE tree with exact expected sets. Your change
can be locally green, fully typed, and lint-clean, and still turn the branch
red for everyone. Run the full `pytest tests/` (CI-equivalent) before you
consider a commit done — or at absolute minimum run the gates below.

### 1. Attribute-contracts gate (2026-08-09)

`tests/unit/web/test_sessions_composer_attribute_contracts.py` pins the EXACT
set of `getattr`/`hasattr`/`getattr_static`/`__getattr__` sites in
`src/elspeth/web/sessions` and `src/elspeth/web/composer`. The contract:
**only ADR-032 LiteLLM admission boundaries may use `getattr`** (the
`_admit_*` parsers and `_call_llm`). Adding ANY dynamic attribute access
anywhere under those trees fails the gate repo-wide.

- Owned type (a class ELSPETH defines)? Use direct attribute access. If the
  attribute is optional, make it a real field with a default — do not probe.
- Genuinely parsing an object ELSPETH does not own? That is a Tier-3
  admission boundary: sentinel `getattr` + value asserts + construct an owned
  type, AND you must deliberately extend the gate's expected set. Do not do
  this casually.

### 2. Masquerade gate (2026-08-09)

`tests/unit/elspeth_lints/test_masquerade_gate.py::test_live_tree_has_zero_unbaselined_findings`
scans the WHOLE repo — **tests included** — for unadjudicated `getattr`
sites against `config/cicd/masquerade_baseline.yaml`. Traps that have fired:

- Parametrizing a test by attribute NAME and resolving with
  `getattr(module, name)` trips it. Parametrize with the objects directly and
  keep readable IDs via `pytest.param(..., id="...")` (see
  `tests/unit/web/composer/test_no_tool_policy_segments.py`).
- A `getattr(obj, "x", None)` "just to be safe" on an owned type trips it.
  The safe-looking default is the defect: it hides AttributeError and lets
  masqueraders pass. Rewrite to direct access; if a test fake breaks, fix the
  FAKE to model the real contract (give it the attribute), never the
  production code to tolerate the fake.

### 3. Trust-tier lint corpus (standing)

`elspeth-lints check --rules all --root src/elspeth` is fail-closed (exit 1,
~3.1k-line corpus, tracked as elspeth-13f0cc04fb). Do NOT expect zero and do
NOT try to clear it. The obligation is: capture the corpus BEFORE your
change, capture it AFTER, and diff — you must add nothing. Never hand-edit a
`judge_metadata_signature`; never shape code to reduce signature churn.

### 4. Wire-shape templates (2026-08-08)

The wrapped-diagnostic producer templates and `_split_wrapped_diagnostic` in
`src/elspeth/web/composer/no_tool_policy.py` derive from ONE
`_wrapped_diagnostic_wire_shape` source, and a round-trip test pins every
template. Do not hand-assemble a SEPARATOR/MARKER/header/footer suffix; add
new templates through `_wrapped_diagnostic_template` or the round-trip test
fails.

### 5. Declared oracles pin OUTPUT bytes (standing)

Several suites pin content hashes, golden files, and byte-exact corpora
(e.g. the `*-lost-c` branch-loss oracles). A behavior-preserving refactor to
a producer can still change pinned bytes. Grep for hashes/golden files near
what you touch, or run the full suite.

## Recent conventions (prune when archived)

- **2026-08-09 — `CompositionState._content_hash_memo`**: write-once memo
  slot read by `composition_content_hash` via DIRECT access. Every mutation
  constructor resets it in `__init__`. If you add a mutation path, reset the
  slot; if you build a `to_dict` stand-in for hashing tests, give it
  `_content_hash_memo: str | None = None`. Do not reintroduce `getattr` here
  (that was elspeth-62a5aa4da8).
- **2026-08-09 — SQLAlchemy `Row`**: `.count` is the TUPLE METHOD, not a
  column. Access columns through `row._mapping` (elspeth-d5578ccd98 fallout,
  Lane B).
- **2026-08-08 — branch-loss reasons are categorical**: every
  `record_coalesce_branch_loss` producer emits bare tokens from the shared
  vocabulary; a new producer must reuse it, not invent prose reasons
  (elspeth-74b795208f).
- **2026-08-08 — forwarding transforms declare their extras**: the extras
  firewall walk is SEPARATE from the presence walk; a transform that forwards
  rows must declare the extras it forwards or downstream consumers see them
  truncated (elspeth-15c72686f2).
