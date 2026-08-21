# WS1a — Lineage Core (model + prep slices) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the unified-lineage MODEL and every behaviour-neutral prep slice of WS1 — `LineageFrame`/`FrameKind`/`lineage_path` beside the stored tri-fields, the three new audit tables with their writers, the journal/codec plumbing, the universal `group_records` mint (empty expansions included), the `GroupLossSpec` contract, and the `join_group_id`-off-`TokenInfo` carrier rewiring — so that WS1b is ONE atomic representation flip (stored-field deletion, replay predicates, fixture regeneration) and nothing else.

**Architecture:** Every task here is additive or carrier-preserving: `TokenInfo.lineage_path` rides BESIDE the stored `branch_name`/`fork_group_id`/`expand_group_id` (which keep today's destructive semantics until WS1b), the durable `token_lineage_frames`/`group_records` rows are written in the same Tier-1 transactions that already mint tokens, and the one representation change taken now (`join_group_id` leaves `TokenInfo`) moves the value onto carriers already in hand (`RowResult`, `PendingOutcome`, `WorkItem`, `TokenWorkItem`) with byte-identical audit output. The WS1a exit state: full suite green with zero pinned-byte deltas; the only observable additions are the new audit rows, which are NOT exported.

**Tech Stack:** Python 3.12+, SQLAlchemy Core (Landscape schema), pytest (`-n 12` for wide runs, `-n 0` for any mutation checks), dataclasses (frozen/slots), StrEnum.

**Spec:** docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md (rev 3.2 — rulings 1–28 final, WITH the 2026-08-22 synthesis corrections applied: read the spec as committed, it is current). Scout inputs: `docs/superpowers/plans/2026-08-21-unified-lineage-inputs/{consumer-roster,fixture-oracle,test-harness}.md`.

## Global Constraints

- **Standing procedures:** docs/superpowers/plans/2026-08-21-unified-lineage-protocols.md §S1–§S5 govern fixture freezing, slice gates, casualty retirement, judge-bundle sequencing, and the WS1 STOP rule.
- **Shared checkout:** stage by explicit pathspec ONLY (`git add <exact paths>` — never `git add -A`/`-u`/`.`); commit only your own hunks; a sibling agent can sweep or amend loosely-staged files.
- **Hooks:** never bypass pre-commit hooks except under the documented `--no-verify`-with-end-of-slice-reconciliation grant; `git stash` is blocked by hook — use commits.
- **Full suite at slice boundaries:** whole-tree AST gates (attribute-contracts, masquerade, wire-shape, exact inventories) miss scoped runs — run the full `pytest tests/` before a slice is considered landed, recording `git rev-parse HEAD` BEFORE and AFTER (a red run across a HEAD change is uninterpretable; re-run, don't diagnose).
- **Trust-tier corpus:** capture `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth` output BEFORE and AFTER each slice and diff — the gate is fail-closed (exit 1 with a large corpus is the baseline); you must ADD NOTHING. Never shape code to reduce signature churn; never hand-edit a `judge_metadata_signature`.
- **Wardline gate:** `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only` (exit 0 = clean and non-inert) before handing back any slice that touches external input.
- **Judge signatures:** no hand-edited signatures, no staging bundles during this campaign (churn invalidates exact-source-bound bundles; 0.7.2 signing sequences AFTER the campaign settles).
- **Depth cap / fixpoint bound (spec §6.3, for any code that touches nesting):** the supported guarantee is 5 layers of bound-region nesting, enforced fail-closed by the builder as a `GraphValidationError`, config-overridable; the escalation fixpoint's non-convergence bound is derived at build from the actual depth (+ margin), never a constant. (WS2/WS3 implement these rules; nothing in WS1a may hard-code a constant that collides with them.)
- **Do not touch** `src/elspeth/web/composer/state.py` or `tests/unit/web/composer/test_state.py` (maintainer is committing them).
- **Pre-1.0 break posture (ruling 22):** no backward compat, no migrations, no dual reads; dev Landscape databases are wiped at the epoch bump (`auth.db` never).

---

## Task ordering and slice boundaries

Tasks 1–3 (contracts) → Task 4 (schema DDL + epoch) → Task 5 (journal plumbing) → Task 6 (durable writers) → Task 7 (empty-expansion mint) → Task 8 (in-memory push/pop + shared test builders) → Task 8a (nested differential corpus fixtures — the protocols §S1 freeze substrate) → Task 9 (join carriers, additive) → Task 10 (`join_group_id` off `TokenInfo`) → Task 11 (slice-boundary verification + WS1b handoff).

Two **slice boundaries** (full `pytest tests/` + trust-tier corpus diff + wardline): after Task 8, and after Task 10 (Task 11). Task 8a sits between them and is corpus-only (fixtures + manifest + oracle classification — no production code); its gate is the scoped corpus suites plus the production-path run for the two new scenario ids. Tasks 1–4 are individually cheap; run the scoped suites named per task plus `pytest tests/unit/contracts tests/unit/core/landscape -n 12` before each commit.

---

### Task 1: `FrameKind` + `LineageFrame` + path JSON codec + innermost-frame helpers

**Files:**
- Modify: `src/elspeth/contracts/enums.py` (append after `error_edge_label`, ~:555)
- Modify: `src/elspeth/contracts/identity.py` (new dataclass + module functions above `TokenInfo`)
- Modify: `src/elspeth/contracts/__init__.py` (`FrameKind` beside `TerminalPath` ~:178; `LineageFrame` beside the `TokenInfo` import :247; both into `__all__`)
- Test: `tests/unit/contracts/test_identity.py`, `tests/unit/contracts/test_enums.py`

**Interfaces:**
- Consumes: nothing (leaf contracts).
- Produces (canonical, used by every later task and by WS1b/WS3 — do not rename):
  - `FrameKind(StrEnum)` with `FORK = "fork"`, `EXPAND = "expand"` in `elspeth.contracts.enums`.
  - `LineageFrame(kind: FrameKind, group_id: str, member_key: str)` — frozen slots dataclass in `elspeth.contracts.identity`.
  - `lineage_path_to_json(path: tuple[LineageFrame, ...]) -> str` and `lineage_path_from_json(raw: str) -> tuple[LineageFrame, ...]` (raises `ValueError` on corrupt input) in `elspeth.contracts.identity`.
  - `innermost_fork_frame(path: tuple[LineageFrame, ...]) -> LineageFrame | None` and `innermost_expand_frame(...) -> LineageFrame | None` in `elspeth.contracts.identity` — WS1b's derived accessors (`branch_name`/`fork_group_id` = innermost FORK frame; `expand_group_id` = innermost EXPAND frame) are thin properties over exactly these two functions.
  - `path_branch_name(path: tuple[LineageFrame, ...]) -> str | None` / `path_fork_group_id(path) -> str | None` / `path_expand_group_id(path) -> str | None` in `elspeth.contracts.identity` — thin wrappers over the two innermost-frame helpers (`branch_name`/`fork_group_id` = innermost FORK frame's `member_key`/`group_id`; `expand_group_id` = innermost EXPAND frame's `group_id`). **Canon (2026-08-22 synthesis):** WS1b's `TokenInfo` properties, WS3's loss attribution, and WS4's wire-field derivations import THESE symbols — no sibling plan defines its own.
  - `pop_closer_frame(path: tuple[LineageFrame, ...], *, kind: FrameKind, group_id: str) -> tuple[LineageFrame, ...]` in `elspeth.contracts.identity` — the ONE strict-pop primitive (rulings 24/28): raises `OrchestrationInvariantError` unless `path[-1]` matches `kind`+`group_id` exactly. Tasks 6 and 8 of this plan call it instead of duplicating the pop inline; WS1b's row_union release pop and WS3's settle-member walk call the same function.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/contracts/test_identity.py`:

```python
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.identity import (
    LineageFrame,
    innermost_expand_frame,
    innermost_fork_frame,
    lineage_path_from_json,
    lineage_path_to_json,
    path_branch_name,
    path_expand_group_id,
    path_fork_group_id,
    pop_closer_frame,
)


class TestLineageFrame:
    def test_frame_construction_and_freeze(self) -> None:
        frame = LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a")
        assert frame.kind is FrameKind.FORK
        with pytest.raises(FrozenInstanceError):
            frame.group_id = "other"  # type: ignore[misc]

    @pytest.mark.parametrize(
        ("kind", "group_id", "member_key"),
        [
            pytest.param("fork", "fg-1", "path_a", id="kind-is-a-bare-string"),
            pytest.param(FrameKind.FORK, "", "path_a", id="empty-group-id"),
            pytest.param(FrameKind.EXPAND, "eg-1", "", id="empty-member-key"),
            pytest.param(FrameKind.EXPAND, None, "m", id="none-group-id"),
        ],
    )
    def test_frame_rejects_bad_fields(self, kind: object, group_id: object, member_key: object) -> None:
        with pytest.raises((TypeError, ValueError)):
            LineageFrame(kind=kind, group_id=group_id, member_key=member_key)  # type: ignore[arg-type]

    def test_json_round_trip_outermost_first(self) -> None:
        path = (
            LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-9"),
            LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a"),
        )
        assert lineage_path_from_json(lineage_path_to_json(path)) == path
        assert lineage_path_from_json(lineage_path_to_json(())) == ()

    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("not json", id="not-json"),
            pytest.param('{"a": 1}', id="not-a-list"),
            pytest.param('[["fork", "fg-1"]]', id="two-element-frame"),
            pytest.param('[["merge", "g", "m"]]', id="unknown-kind"),
        ],
    )
    def test_json_rejects_corrupt_payloads(self, raw: str) -> None:
        with pytest.raises(ValueError):
            lineage_path_from_json(raw)

    def test_innermost_helpers_pick_the_innermost_of_each_kind(self) -> None:
        outer_fork = LineageFrame(kind=FrameKind.FORK, group_id="fg-outer", member_key="a")
        expand = LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-1")
        inner_fork = LineageFrame(kind=FrameKind.FORK, group_id="fg-inner", member_key="b")
        path = (outer_fork, expand, inner_fork)
        assert innermost_fork_frame(path) is inner_fork
        assert innermost_expand_frame(path) is expand
        assert innermost_fork_frame(()) is None
        assert innermost_expand_frame((outer_fork,)) is None

    def test_path_wrappers_derive_the_retiring_stored_fields(self) -> None:
        outer_fork = LineageFrame(kind=FrameKind.FORK, group_id="fg-outer", member_key="a")
        expand = LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-1")
        inner_fork = LineageFrame(kind=FrameKind.FORK, group_id="fg-inner", member_key="b")
        path = (outer_fork, expand, inner_fork)
        assert path_branch_name(path) == "b"
        assert path_fork_group_id(path) == "fg-inner"
        assert path_expand_group_id(path) == "eg-1"
        assert path_branch_name(()) is None
        assert path_fork_group_id(()) is None
        assert path_expand_group_id((outer_fork,)) is None


class TestPopCloserFrame:
    def test_pops_exactly_the_matching_innermost_frame(self) -> None:
        outer = LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-1")
        inner = LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="a")
        assert pop_closer_frame((outer, inner), kind=FrameKind.FORK, group_id="fg-1") == (outer,)
        assert pop_closer_frame((outer,), kind=FrameKind.EXPAND, group_id="eg-1") == ()

    @pytest.mark.parametrize(
        ("path", "kind", "group_id"),
        [
            pytest.param((), FrameKind.FORK, "fg-1", id="empty-path"),
            pytest.param(
                (LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="t"),),
                FrameKind.FORK,
                "eg-1",
                id="wrong-kind",
            ),
            pytest.param(
                (LineageFrame(kind=FrameKind.FORK, group_id="fg-2", member_key="a"),),
                FrameKind.FORK,
                "fg-1",
                id="wrong-group",
            ),
            pytest.param(
                (
                    LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="a"),
                    LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="t"),
                ),
                FrameKind.FORK,
                "fg-1",
                id="matching-frame-buried-not-innermost",
            ),
        ],
    )
    def test_refuses_any_non_matching_innermost_frame(
        self, path: tuple[LineageFrame, ...], kind: FrameKind, group_id: str
    ) -> None:
        with pytest.raises(OrchestrationInvariantError, match="innermost"):
            pop_closer_frame(path, kind=kind, group_id=group_id)
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/contracts/test_identity.py -k LineageFrame -x`
Expected: FAIL — `ImportError: cannot import name 'FrameKind'`.

- [ ] **Step 3: Implement.** Append to `src/elspeth/contracts/enums.py` (after `error_edge_label`):

```python
# Lineage-frame kinds (unified lineage spec rev 3.2, §4.1). Closed vocabulary:
# a FORK group is opened by a fork gate (member_key = declared branch name); an
# EXPAND group by a multi-row transform activation (member_key = member
# token_id). A new kind requires a spec amendment — the frames table and
# group_records both carry a CHECK over this enum.
class FrameKind(StrEnum):
    FORK = "fork"
    EXPAND = "expand"
```

In `src/elspeth/contracts/identity.py`, add `import json`, `from elspeth.contracts.enums import FrameKind`, and `from elspeth.contracts.errors import OrchestrationInvariantError` to the imports (`contracts.errors` does not import `identity` — no cycle), then above `TokenInfo`:

```python
@dataclass(frozen=True, slots=True)
class LineageFrame:
    """One (kind, group_id, member_key) lineage-path entry (spec §4.1).

    Frames are minted only by the opening primitives inside TokenManager /
    DataFlowTokenRepository — never asserted by failing code — which is what
    makes the §6.2 loss guard self-authenticating.
    """

    kind: FrameKind
    group_id: str
    member_key: str

    def __post_init__(self) -> None:
        if type(self.kind) is not FrameKind:
            raise TypeError(f"LineageFrame.kind must be FrameKind, got {type(self.kind).__name__}: {self.kind!r}")
        for field_name, value in (("group_id", self.group_id), ("member_key", self.member_key)):
            if not isinstance(value, str):
                raise TypeError(f"LineageFrame.{field_name} must be str, got {type(value).__name__}: {value!r}")
            if not value:
                raise ValueError(f"LineageFrame.{field_name} must not be empty")


def lineage_path_to_json(path: tuple[LineageFrame, ...]) -> str:
    """Serialize a lineage path (outermost first) for the scheduler journal."""
    return json.dumps([[frame.kind.value, frame.group_id, frame.member_key] for frame in path], allow_nan=False)


def lineage_path_from_json(raw: str) -> tuple[LineageFrame, ...]:
    """Inverse of lineage_path_to_json. Raises ValueError on corrupt input."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Corrupt lineage_path JSON: {exc}") from exc
    if type(payload) is not list:
        raise ValueError(f"Corrupt lineage_path JSON: expected array, got {type(payload).__name__}")
    frames: list[LineageFrame] = []
    for entry in payload:
        if type(entry) is not list or len(entry) != 3:
            raise ValueError(f"Corrupt lineage_path frame: expected [kind, group_id, member_key], got {entry!r}")
        kind_raw, group_id, member_key = entry
        try:
            kind = FrameKind(kind_raw)
        except ValueError as exc:
            raise ValueError(f"Corrupt lineage_path frame kind: {kind_raw!r}") from exc
        frames.append(LineageFrame(kind=kind, group_id=group_id, member_key=member_key))
    return tuple(frames)


def innermost_fork_frame(path: tuple[LineageFrame, ...]) -> LineageFrame | None:
    """The innermost FORK frame — WS1b's branch_name/fork_group_id accessor source."""
    for frame in reversed(path):
        if frame.kind is FrameKind.FORK:
            return frame
    return None


def innermost_expand_frame(path: tuple[LineageFrame, ...]) -> LineageFrame | None:
    """The innermost EXPAND frame — WS1b's expand_group_id accessor source."""
    for frame in reversed(path):
        if frame.kind is FrameKind.EXPAND:
            return frame
    return None


def path_branch_name(path: tuple[LineageFrame, ...]) -> str | None:
    """Derived branch_name (§4.1a): the innermost FORK frame's member_key.

    WS1b re-exposes TokenInfo.branch_name as a property over exactly this
    function; WS3's loss attribution and WS4's wire fields import it too.
    """
    frame = innermost_fork_frame(path)
    return None if frame is None else frame.member_key


def path_fork_group_id(path: tuple[LineageFrame, ...]) -> str | None:
    """Derived fork_group_id (§4.1a): the innermost FORK frame's group_id."""
    frame = innermost_fork_frame(path)
    return None if frame is None else frame.group_id


def path_expand_group_id(path: tuple[LineageFrame, ...]) -> str | None:
    """Derived expand_group_id (§4.1a): the innermost EXPAND frame's group_id."""
    frame = innermost_expand_frame(path)
    return None if frame is None else frame.group_id


def pop_closer_frame(path: tuple[LineageFrame, ...], *, kind: FrameKind, group_id: str) -> tuple[LineageFrame, ...]:
    """Strict pop (spec rulings 24/28): remove exactly the closer's own frame.

    The closer names the frame it is entitled to pop (kind + group_id).
    Anything else — an empty path, a different innermost kind, a different
    group — is lineage corruption, never a recoverable state. Both strict-pop
    layers (engine coalesce_tokens and the durable Tier-1 twin) route through
    this one function so the refusal semantics cannot drift.
    """
    if not path or path[-1].kind is not kind or path[-1].group_id != group_id:
        innermost = path[-1] if path else None
        raise OrchestrationInvariantError(
            f"pop_closer_frame: path has no matching innermost {kind.name} frame for group {group_id!r} "
            f"(innermost={innermost!r}); a closer pops exactly its own frame (spec rulings 24/28)"
        )
    return path[:-1]
```

In `src/elspeth/contracts/__init__.py`: add `FrameKind` to the `elspeth.contracts.enums` import block (the one containing `TerminalPath`, ~:178) and change :247 to `from elspeth.contracts.identity import LineageFrame, TokenInfo`; add `"FrameKind"` and `"LineageFrame"` to `__all__` (sorted position).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/contracts/test_identity.py tests/unit/contracts/test_enums.py -x`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/contracts/enums.py src/elspeth/contracts/identity.py src/elspeth/contracts/__init__.py tests/unit/contracts/test_identity.py
git commit -m "feat(contracts): add FrameKind, LineageFrame and lineage-path codec (WS1a)"
```

---

### Task 2: `TokenInfo.lineage_path` field (beside the stored tri-fields)

**Files:**
- Modify: `src/elspeth/contracts/identity.py:33-110` (`TokenInfo` field, `__post_init__`, docstrings)
- Test: `tests/unit/contracts/test_identity.py`

**Interfaces:**
- Consumes: Task 1's `LineageFrame`.
- Produces: `TokenInfo.lineage_path: tuple[LineageFrame, ...] = ()` — outermost first; preserved by `with_updated_data()` (`dataclasses.replace`). **Prep-phase contract:** the stored `branch_name`/`fork_group_id`/`join_group_id`/`expand_group_id` fields keep today's destructive semantics and remain what every consumer reads; `lineage_path` is write-complete but read only by tests until WS1b flips the representation (ruling 26's deltas land AT the flip, not here).

- [ ] **Step 1: Write the failing test** — append to `tests/unit/contracts/test_identity.py` inside `TestTokenInfo` (reuse the file's `_make_contract`/`PipelineRow` pattern from its existing tests):

```python
    def test_lineage_path_defaults_empty_and_survives_with_updated_data(self) -> None:
        contract = _make_contract()
        path = (LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a"),)
        token = TokenInfo(
            row_id="row-1",
            token_id="tok-1",
            row_data=PipelineRow({"field": "v"}, contract),
            branch_name="path_a",
            fork_group_id="fg-1",
            lineage_path=path,
        )
        assert token.lineage_path == path
        updated = token.with_updated_data(PipelineRow({"field": "w"}, contract))
        assert updated.lineage_path == path
        bare = TokenInfo(row_id="row-1", token_id="tok-2", row_data=PipelineRow({"field": "v"}, contract))
        assert bare.lineage_path == ()

    @pytest.mark.parametrize(
        "bad_path",
        [
            pytest.param([LineageFrame(kind=FrameKind.FORK, group_id="g", member_key="m")], id="list-not-tuple"),
            pytest.param((("fork", "g", "m"),), id="raw-tuple-entry"),
        ],
    )
    def test_lineage_path_rejects_untyped_values(self, bad_path: object) -> None:
        with pytest.raises(TypeError):
            TokenInfo(
                row_id="row-1",
                token_id="tok-1",
                row_data=PipelineRow({"field": "v"}, _make_contract()),
                lineage_path=bad_path,  # type: ignore[arg-type]
            )
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/contracts/test_identity.py -k lineage_path -x`
Expected: FAIL — `TypeError: TokenInfo.__init__() got an unexpected keyword argument 'lineage_path'`.

- [ ] **Step 3: Implement.** In `TokenInfo`, add after `expand_group_id: str | None = None` (:39):

```python
    lineage_path: tuple[LineageFrame, ...] = ()  # Outermost first (spec §4.1). WS1a prep:
    # rides BESIDE the stored branch_name/fork_group_id/join_group_id/expand_group_id,
    # which keep today's destructive semantics and remain the read path. WS1b deletes
    # the stored fields and re-exposes them as read-only properties over this path
    # (innermost_fork_frame / innermost_expand_frame). Do NOT read this field from
    # production code during WS1a — that would be a dual-representation read.
```

and in `__post_init__` after the optional-string loop (:66-76):

```python
        if type(self.lineage_path) is not tuple:
            raise TypeError(
                f"TokenInfo.lineage_path must be tuple[LineageFrame, ...], got {type(self.lineage_path).__name__}: {self.lineage_path!r}"
            )
        for frame in self.lineage_path:
            if type(frame) is not LineageFrame:
                raise TypeError(f"TokenInfo.lineage_path entries must be LineageFrame, got {type(frame).__name__}: {frame!r}")
```

Update the class docstring lineage bullet list (:20-25) with one line: `- lineage_path: typed frame stack (unified lineage spec §4.1); tri-fields above are retired at WS1b`. `with_updated_data` needs no code change (`replace` preserves); extend its docstring's preserved-fields list to include `lineage_path`.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/contracts/test_identity.py -x`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/contracts/identity.py tests/unit/contracts/test_identity.py
git commit -m "feat(contracts): TokenInfo carries lineage_path beside the stored tri-fields (WS1a prep)"
```

---

### Task 3: `GroupLossSpec` contract (consumed by WS3)

**Files:**
- Modify: `src/elspeth/contracts/scheduler.py` (new dataclass directly below `BranchLossSpec`, :70-86)
- Test: `tests/unit/contracts/test_scheduler_contracts.py`

**Interfaces:**
- Consumes: nothing.
- Produces (canonical — WS3 consumes this exact shape): `GroupLossSpec(closer_name: str, group_id: str, member_key: str, token_id: str, reason: str)` frozen dataclass in `elspeth.contracts.scheduler`. **Decision (made here, per the spec's §6.2 type):** unlike `BranchLossSpec`, the spec type carries NO `recorded_by` — the staging repository verb supplies `recorded_by` (the lease owner it already holds) when it writes the `group_losses` row; WS3 implements that verb. `BranchLossSpec` is NOT touched in WS1a; both types coexist until WS3 retires it.

- [ ] **Step 1: Write the failing test** — append to `tests/unit/contracts/test_scheduler_contracts.py`:

```python
from elspeth.contracts.scheduler import GroupLossSpec


class TestGroupLossSpec:
    def test_construction_and_field_order(self) -> None:
        spec = GroupLossSpec(
            closer_name="merge_paths",
            group_id="fg-1",
            member_key="path_c",
            token_id="tok-3",
            reason="dropped_by_filter",
        )
        assert (spec.closer_name, spec.group_id, spec.member_key, spec.token_id, spec.reason) == (
            "merge_paths",
            "fg-1",
            "path_c",
            "tok-3",
            "dropped_by_filter",
        )

    def test_frozen(self) -> None:
        spec = GroupLossSpec(closer_name="c", group_id="g", member_key="m", token_id="t", reason="r")
        with pytest.raises(FrozenInstanceError):
            spec.reason = "other"  # type: ignore[misc]
```

(Import `FrozenInstanceError` from `dataclasses` and `pytest` per the file's existing imports.)

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/contracts/test_scheduler_contracts.py -k GroupLossSpec -x`
Expected: FAIL — `ImportError: cannot import name 'GroupLossSpec'`.

- [ ] **Step 3: Implement** — in `src/elspeth/contracts/scheduler.py`, directly below `BranchLossSpec`:

```python
@dataclass(frozen=True)
class GroupLossSpec:
    """Durable group-loss record riding a lossy disposition (spec §6.2, rev 3.2).

    The unified replacement for ``BranchLossSpec``: one loss names one member
    of one group at one closer. Natural key = (run_id, closer_name, group_id,
    member_key) — group-scoped, so the rev-2 ledger key collision is
    structurally impossible. ``token_id`` is recorded for lineage-corruption
    detection (same-key different-token raises Tier-1). ``reason`` stays
    within the categorical branch-loss vocabulary — bare shared tokens, never
    prose. ``recorded_by`` deliberately does NOT ride the spec: the staging
    repository verb stamps the lease owner it already holds (WS3).

    WS1a defines the type; WS3 lands the writer, the frame-authenticated
    guard, and the ``BranchLossSpec`` retirement. Until then nothing
    constructs this outside tests.
    """

    closer_name: str
    group_id: str
    member_key: str
    token_id: str
    reason: str
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/contracts/test_scheduler_contracts.py -x`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/contracts/scheduler.py tests/unit/contracts/test_scheduler_contracts.py
git commit -m "feat(contracts): add GroupLossSpec (unified loss ledger spec type, consumed by WS3)"
```

---

### Task 4: Schema DDL — `token_lineage_frames`, `group_records`, `group_losses`, `token_work_items.lineage_path_json`, epoch 34

**Files:**
- Modify: `src/elspeth/core/landscape/schema.py` (epoch history + `SQLITE_SCHEMA_EPOCH` :311-317; new tables after `coalesce_branch_losses_table`'s index block ~:1064; new column in `token_work_items_table` after `expand_group_id` :728)
- Modify: `src/elspeth/core/landscape/database.py` (required-column list, pattern at :288-399)
- Modify: `CHANGELOG.md`, `website/get-started.html:105-106`, `docs/guides/sharing-pipelines.md:86`, `docs/product/current-state.md` (+ every other live "epoch 33" statement found by the grep in Step 3)
- Test: `tests/unit/core/landscape/test_schema_epoch_and_required_columns.py:40`, `tests/unit/core/landscape/test_token_ownership_run_scope.py:35`, `tests/integration/web/composer/guided/test_schema9_epoch.py:20`, plus the doc-prose pins `tests/unit/docs/test_composer_capability_docs.py:110` and `tests/unit/docs/test_staging_session_recreation_policy.py:15`

**Interfaces:**
- Consumes: Task 1's `FrameKind` (for CHECK constraints).
- Produces (canonical table shapes — WS1b/WS3/WS4/WS5 consume; do not rename):
  - `token_lineage_frames(token_id, run_id, depth, kind, group_id, member_key)` PK `(token_id, run_id, depth)`, INDEX `(run_id, group_id, member_key)`, composite FK to `tokens`.
  - `group_records(run_id, group_id, kind, opener_token_id, member_count, created_at)` PK `(run_id, group_id)`, plus `uq_group_records_opener` UNIQUE `(run_id, opener_token_id)` (one token opens at most one group — the terminal-outcome guard makes this a true invariant; it is what makes the empty-expansion mint idempotent).
  - `group_losses(loss_id PK, run_id FK, closer_name, group_id, member_key, token_id, reason, recorded_by, recorded_at, adopted_epoch)` UNIQUE `(run_id, closer_name, group_id, member_key)`. Written from WS3; WS1a lands DDL only.
  - `token_work_items.lineage_path_json` (Text, NOT NULL) — Task 5's journal carrier.
- **Decision (RATIFIED by the 2026-08-22 synthesis):** `group_records` rows are minted for BOTH opener kinds — `kind='fork'` in `fork_token`'s transaction and `kind='expand'` in `expand_token`'s (Task 6) — because the spec's table carries `kind` and §5's cross-check reads `member_count` uniformly; FORK roster AUTHORITY remains config (§5), the fork row is audit enrichment. WS1a is authoritative for this behaviour campaign-wide (see Task 6's canon note).

- [ ] **Step 1: Write the failing test edits.** In `tests/unit/core/landscape/test_schema_epoch_and_required_columns.py:40` change `assert SQLITE_SCHEMA_EPOCH == 33` to `== 34`, and add to the same file (module level, following its import style):

```python
def test_unified_lineage_tables_exist_with_exact_keys() -> None:
    from elspeth.core.landscape.schema import group_losses_table, group_records_table, token_lineage_frames_table

    assert [c.name for c in token_lineage_frames_table.primary_key.columns] == ["token_id", "run_id", "depth"]
    assert {c.name for c in token_lineage_frames_table.columns} == {"token_id", "run_id", "depth", "kind", "group_id", "member_key"}
    assert [c.name for c in group_records_table.primary_key.columns] == ["run_id", "group_id"]
    assert {c.name for c in group_records_table.columns} == {"run_id", "group_id", "kind", "opener_token_id", "member_count", "created_at"}
    assert {c.name for c in group_losses_table.columns} == {
        "loss_id", "run_id", "closer_name", "group_id", "member_key", "token_id", "reason", "recorded_by", "recorded_at", "adopted_epoch",
    }
    # No defaulted getattr here — the whole-tree masquerade gate pins the exact
    # set of dynamic-attribute sites; every table constraint carries .name.
    natural = next(c for c in group_losses_table.constraints if c.name == "uq_group_losses_natural")
    assert [col.name for col in natural.columns] == ["run_id", "closer_name", "group_id", "member_key"]
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/core/landscape/test_schema_epoch_and_required_columns.py -x`
Expected: FAIL — epoch is still 33 and `token_lineage_frames_table` does not exist.

- [ ] **Step 3: Implement schema.** In `src/elspeth/core/landscape/schema.py`:

(a) Append to the epoch history comment (:311-316) and bump:

```python
#   34 → Unified lineage groundwork (WS1a, barrier-scopes spec rev 3.2):
#        token_lineage_frames (typed lineage-frame stack per token, written in
#        the token-INSERT transaction), group_records (roster record for every
#        opening operation, empty expansions included), group_losses (the
#        unified loss ledger — written from WS3), and
#        token_work_items.lineage_path_json (journal-riding lineage path).
#        Pre-1.0 delete-and-recreate boundary; no migration.
SQLITE_SCHEMA_EPOCH = 34
```

(b) Import `FrameKind` in the existing `elspeth.contracts` import block (`from elspeth.contracts.enums import FrameKind` beside the module's other contracts imports).

(c) In `token_work_items_table` add after `Column("expand_group_id", String(128)),` (:728):

```python
    # Epoch 34: the token's typed lineage path (outermost first), serialized by
    # contracts.identity.lineage_path_to_json. The journal is authoritative for
    # resume, so the path must ride the row exactly like the (retiring)
    # tri-columns above it; WS1b deletes those and this column stays.
    Column("lineage_path_json", Text, nullable=False),
```

(d) After the `coalesce_branch_losses` index block (~:1064) add the three tables:

```python
# === Unified lineage (epoch 34, barrier-scopes spec rev 3.2 §4.3) ===

token_lineage_frames_table = Table(
    "token_lineage_frames",
    metadata,
    Column("token_id", String(64), primary_key=True),
    Column("run_id", String(64), primary_key=True),
    Column("depth", Integer, primary_key=True),  # 0 = outermost
    Column("kind", String(16), nullable=False),
    Column("group_id", String(64), nullable=False),
    Column("member_key", String(128), nullable=False),  # FORK: branch name; EXPAND: member token_id
    CheckConstraint(_enum_in_check("kind", FrameKind), name="ck_token_lineage_frames_kind"),
    ForeignKeyConstraint(["token_id", "run_id"], ["tokens.token_id", "tokens.run_id"]),
)
Index(
    "ix_token_lineage_frames_group",
    token_lineage_frames_table.c.run_id,
    token_lineage_frames_table.c.group_id,
    token_lineage_frames_table.c.member_key,
)

group_records_table = Table(
    "group_records",
    metadata,
    Column("run_id", String(64), ForeignKey("runs.run_id"), primary_key=True),
    Column("group_id", String(64), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("opener_token_id", String(64), nullable=False),
    # member_count=0 is legal and REQUIRED for empty expansions (§4.3): it is
    # the durable referent the require_all empty-group failure needs.
    Column("member_count", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint("member_count >= 0", name="ck_group_records_member_count_nonneg"),
    CheckConstraint(_enum_in_check("kind", FrameKind), name="ck_group_records_kind"),
    ForeignKeyConstraint(["opener_token_id", "run_id"], ["tokens.token_id", "tokens.run_id"]),
)
# One token opens at most one group: every opener records a terminal parent
# disposition (FORK_PARENT / EXPAND_PARENT / BATCH_CONSUMED / FILTER_DROPPED)
# in the same claim, so a second open is unreachable. This uniqueness is what
# makes the empty-expansion mint idempotent under re-driven claims.
Index(
    "uq_group_records_opener",
    group_records_table.c.run_id,
    group_records_table.c.opener_token_id,
    unique=True,
)

group_losses_table = Table(
    "group_losses",
    metadata,
    Column("loss_id", String(64), primary_key=True),
    Column("run_id", String(64), ForeignKey("runs.run_id"), nullable=False),
    Column("closer_name", String(128), nullable=False),
    Column("group_id", String(64), nullable=False),
    Column("member_key", String(128), nullable=False),
    Column("token_id", String(64), nullable=False),
    # Categorical vocabulary only (2026-08-08 convention; String(64) per the
    # battery-round-7 Postgres lesson pinned in
    # test_coalesce_branch_loss_reason_postgres.py — WS3 owes the same
    # three-proof treatment on this column).
    Column("reason", String(64), nullable=False),
    Column("recorded_by", String(128), nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False),
    Column("adopted_epoch", Integer),  # NULL = not yet replayed into leader memory (§E.5 cursor)
    UniqueConstraint("run_id", "closer_name", "group_id", "member_key", name="uq_group_losses_natural"),
    ForeignKeyConstraint(["token_id", "run_id"], ["tokens.token_id", "tokens.run_id"]),
)
```

(e) In `src/elspeth/core/landscape/database.py`, extend the required-column list (append near the `token_work_items` block at :376-399, same tuple style):

```python
    # Epoch 34: unified lineage groundwork (WS1a).
    ("token_work_items", "lineage_path_json"),
    ("token_lineage_frames", "token_id"),
    ("token_lineage_frames", "run_id"),
    ("token_lineage_frames", "depth"),
    ("token_lineage_frames", "kind"),
    ("token_lineage_frames", "group_id"),
    ("token_lineage_frames", "member_key"),
    ("group_records", "run_id"),
    ("group_records", "group_id"),
    ("group_records", "kind"),
    ("group_records", "opener_token_id"),
    ("group_records", "member_count"),
    ("group_records", "created_at"),
    ("group_losses", "loss_id"),
    ("group_losses", "run_id"),
    ("group_losses", "closer_name"),
    ("group_losses", "group_id"),
    ("group_losses", "member_key"),
    ("group_losses", "token_id"),
    ("group_losses", "reason"),
    ("group_losses", "recorded_by"),
    ("group_losses", "recorded_at"),
    ("group_losses", "adopted_epoch"),
```

(f) **Epoch-pin fan-out.** Run `git grep -n "epoch 33\|epoch is 33\|→ 33\|== 33"` and adjudicate every hit: statements of the CURRENT epoch move to 34 (`website/get-started.html:105-106` "29 → 33" becomes "29 → 34"; `docs/guides/sharing-pipelines.md:86` "below epoch 33" becomes "below epoch 34"; `docs/product/current-state.md`; the prose pinned by `tests/unit/docs/test_composer_capability_docs.py` and `tests/unit/docs/test_staging_session_recreation_policy.py` — update doc AND pin together); history entries ("at epoch 29 lacks it") stay. Update the epoch comments in `test_token_ownership_run_scope.py:35` and `test_schema9_epoch.py:17-20` to 34 with a one-line reason. Add a CHANGELOG entry under 0.7.2: `Landscape SQLITE_SCHEMA_EPOCH 33 → 34: unified-lineage tables (token_lineage_frames, group_records, group_losses) and token_work_items.lineage_path_json; existing audit stores must be recreated.`

NOTE: this bump makes every existing dev `audit.db` refuse to open — that is the delete-and-recreate boundary working. Wipe dev Landscape stores (never `auth.db`).

- [ ] **Step 4: Run to verify pass.** Task 5 has not landed yet, so every INSERT path still omits `lineage_path_json` — that is why this task and Task 5 MUST be committed together in one slice if the scoped suite below is red on NOT-NULL violations. Run first:

Run: `pytest tests/unit/core/landscape/test_schema_epoch_and_required_columns.py tests/unit/core/landscape/test_schema.py tests/unit/core/landscape/test_token_ownership_run_scope.py tests/integration/web/composer/guided/test_schema9_epoch.py tests/unit/docs/ -x`

If scheduler-suite fixtures fail on the NOT NULL `lineage_path_json` column, proceed directly to Task 5 and commit both tasks as one commit (`feat(landscape): epoch 34 unified-lineage tables + journal lineage_path`); otherwise commit here.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/core/landscape/schema.py src/elspeth/core/landscape/database.py \
  tests/unit/core/landscape/test_schema_epoch_and_required_columns.py \
  tests/unit/core/landscape/test_token_ownership_run_scope.py \
  tests/integration/web/composer/guided/test_schema9_epoch.py \
  CHANGELOG.md website/get-started.html docs/guides/sharing-pipelines.md docs/product/current-state.md \
  tests/unit/docs/test_composer_capability_docs.py tests/unit/docs/test_staging_session_recreation_policy.py
git commit -m "feat(landscape): epoch 34 — token_lineage_frames, group_records, group_losses DDL"
```

---

### Task 5: `lineage_path` rides the scheduler journal (TokenWorkItem, codecs, enqueue verbs, resume specs)

**Files:**
- Modify: `src/elspeth/contracts/scheduler.py` (`TokenWorkItem` :107-153, `BarrierEmission` :155-204)
- Modify: `src/elspeth/engine/scheduler_work_codec.py` (`ScheduledWorkFields` :50-75, `ready_fields` :90-115, `ready_emission` :117-137, `work_item_from_scheduler` :139-158)
- Modify: `src/elspeth/core/landscape/scheduler/work_items.py` (`item_from_mapping` :49-88, `ready_work_item_values` :91-144, `comparable_fields` :251-276)
- Modify: `src/elspeth/core/landscape/scheduler/queue.py` (all enqueue verbs: :48, :154, :216, :330, and the values block :508-511 inside the fenced ingest)
- Modify: `src/elspeth/core/landscape/scheduler_repository.py` (wrappers :114, :162, :214, :266 — thread the new kwarg)
- Modify: `src/elspeth/core/landscape/scheduler/payload_codec.py` (`token_from_journal_item` :74-102)
- Modify: `src/elspeth/core/landscape/scheduler/barrier.py` (emission-insert dicts :703-706 and :775-778 gain `"lineage_path_json"`)
- Modify: `src/elspeth/engine/processor.py` (:2513-2535 ingest call; `_sink_emission_from_result` :3509-3525; resume TokenInfo :2868-2878)
- Modify: `src/elspeth/engine/scheduler_drain.py` (:897-907 pending-sink replay TokenInfo; enqueue calls :1118-1140 and :1142-1170)
- Modify: `src/elspeth/core/checkpoint/recovery.py` (`IncompleteTokenSpec` :247-263; `_get_incomplete_token_work` :703-761)
- Test: `tests/unit/engine/test_scheduler_work_codec.py`

**Interfaces:**
- Consumes: Task 1 codec + Task 2 field + Task 4 column.
- Produces: `TokenWorkItem.lineage_path: tuple[LineageFrame, ...] = ()` (typed; the column holds `lineage_path_json`); `BarrierEmission.lineage_path: tuple[LineageFrame, ...] = ()`; `ScheduledWorkFields.lineage_path`; every rehydrate seam (`work_item_from_scheduler`, `token_from_journal_item`, scheduler_drain pending-sink replay, resume dispatch) sets `TokenInfo.lineage_path` from the journal/frames; `IncompleteTokenSpec.lineage_path: tuple[LineageFrame, ...]` loaded from `token_lineage_frames`. WS3 reads `TokenWorkItem.lineage_path` for the settle-member frame walk.

- [ ] **Step 1: Write the failing test** — append to `tests/unit/engine/test_scheduler_work_codec.py`, using the file's REAL helpers (`_make_codec` :84, `_make_item` :100 — its default token already carries the four stored lineage fields — and `_scheduler_row_from_fields` :121):

```python
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import LineageFrame

_LINEAGE_PATH = (
    LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-1"),
    LineageFrame(kind=FrameKind.FORK, group_id="fork-1", member_key="branch-a"),
)


def test_lineage_path_round_trips_through_ready_fields_and_rehydrate() -> None:
    item = _make_item(
        token=replace(_make_item().token, lineage_path=_LINEAGE_PATH),
    )
    codec = _make_codec()
    fields = codec.ready_fields(item)
    assert fields.lineage_path == _LINEAGE_PATH
    emission = codec.ready_emission(item)
    assert emission.lineage_path == _LINEAGE_PATH
    scheduled = _scheduler_row_from_fields(fields)
    rehydrated = codec.work_item_from_scheduler(scheduled)
    assert rehydrated.token.lineage_path == _LINEAGE_PATH
```

Also: `_scheduler_row_from_fields` (:121-147) gains `lineage_path=fields.lineage_path,` (it materializes the durable row exactly as the repository persists the bundle — that line IS part of this task's contract), and the file's existing strict-field-equality invariant test must include `lineage_path` in the compared bundle (it iterates `dataclass_fields`, so verify it picks the new field up automatically).

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/engine/test_scheduler_work_codec.py -x`
Expected: FAIL — `ScheduledWorkFields` has no `lineage_path`.

- [ ] **Step 3: Implement, outward from the contracts.**

(a) `contracts/scheduler.py`: import `LineageFrame` from `elspeth.contracts.identity`; add to `TokenWorkItem` after `expand_group_id` (:141): `lineage_path: tuple[LineageFrame, ...] = ()` with the comment `# Epoch 34: typed lineage path (outermost first); column form is lineage_path_json.` Add the same field to `BarrierEmission` after :194.

(b) `scheduler_work_codec.py`: add `lineage_path: tuple[LineageFrame, ...]` to `ScheduledWorkFields` (after `expand_group_id` :71); in `ready_fields` add `lineage_path=token.lineage_path,`; in `ready_emission` add `lineage_path=fields.lineage_path,`; in `work_item_from_scheduler` add `lineage_path=scheduled.lineage_path,` to the `TokenInfo` construction (:142-150). Import `LineageFrame` alongside `TokenInfo`.

(c) `core/landscape/scheduler/work_items.py`: import `lineage_path_from_json, lineage_path_to_json, LineageFrame` from `elspeth.contracts.identity`. `ready_work_item_values` gains a required keyword param `lineage_path: tuple[LineageFrame, ...]` (between `expand_group_id` and `coalesce_node_id` in the signature) and the dict gains `"lineage_path_json": lineage_path_to_json(lineage_path),` after `"expand_group_id"`. In `item_from_mapping` add before the `TokenWorkItem(` construction:

```python
    try:
        lineage_path = lineage_path_from_json(data["lineage_path_json"])
    except ValueError as exc:
        raise AuditIntegrityError(f"Corrupt token_work_items.lineage_path_json for work_item_id={data['work_item_id']!r}: {exc}") from exc
```

and `lineage_path=lineage_path,` in the construction. Add `"lineage_path_json"` to `comparable_fields` (after `"expand_group_id"`).

(d) `queue.py`: each of `enqueue_ready` (:48), `enqueue_ready_claimed` (:154), `enqueue_ready_claimed_legacy_unfenced` (:216), `enqueue_ready_claimed_on` (:330), and `ingest_row_with_initial_claim` gains keyword param `lineage_path: tuple[LineageFrame, ...] = ()` and threads it into its `ready_work_item_values(...)` call (all sites: :93-113, :206-209 block's builder, :264-267's, :319-322's, :383-386's, :508-511's — mirror the tri-field lines exactly). Default `()` matches the existing `branch_name=None` style: root tokens and legacy/test callers legitimately have empty paths.

(e) `scheduler_repository.py`: the delegation wrappers (:114, :162, :214, :266) gain the same `lineage_path: tuple[LineageFrame, ...] = ()` kwarg and pass it through.

(f) `core/landscape/scheduler/barrier.py`: at the two emission-persist sites (:703-706 dict form, :775-778 kwargs form) add `"lineage_path_json": lineage_path_to_json(emission.lineage_path)` / `lineage_path=emission.lineage_path` beside the four existing lineage fields (read both blocks first; mirror their exact form — one writes a raw values dict, one calls `ready_work_item_values`).

(g) `payload_codec.py` `token_from_journal_item`: add `lineage_path=item.lineage_path,` to the `TokenInfo` construction (:92-102).

(h) `engine/processor.py`: :2513-2535 ingest call gains `lineage_path=fields.lineage_path,`; `_sink_emission_from_result` (:3509-3525) gains `lineage_path=token.lineage_path,`; resume reconstruction :2868-2878 gains `lineage_path=spec.lineage_path,`.

(i) `engine/scheduler_drain.py`: :897-907 TokenInfo gains `lineage_path=scheduled.lineage_path,`; both enqueue calls (:1118-1140, :1142-1170) gain `lineage_path=fields.lineage_path,`.

(j) `core/checkpoint/recovery.py`: `IncompleteTokenSpec` gains `lineage_path: tuple[LineageFrame, ...]` (after `expand_group_id` :260; import `LineageFrame`). In `_get_incomplete_token_work` (:715-740), after `incomplete_rows` is fetched, batch-load the run's frames and thread them:

```python
            frames_by_token: dict[str, list[tuple[int, LineageFrame]]] = {}
            frame_rows = conn.execute(
                select(
                    token_lineage_frames_table.c.token_id,
                    token_lineage_frames_table.c.depth,
                    token_lineage_frames_table.c.kind,
                    token_lineage_frames_table.c.group_id,
                    token_lineage_frames_table.c.member_key,
                ).where(token_lineage_frames_table.c.run_id == run_id)
            ).fetchall()
            for frame_row in frame_rows:
                frames_by_token.setdefault(frame_row.token_id, []).append(
                    (
                        int(frame_row.depth),
                        LineageFrame(kind=FrameKind(frame_row.kind), group_id=frame_row.group_id, member_key=frame_row.member_key),
                    )
                )
```

and in the `IncompleteTokenSpec(` construction (:749-759): `lineage_path=tuple(frame for _depth, frame in sorted(frames_by_token.get(row.token_id, []))),`. Import `token_lineage_frames_table` beside the file's existing `tokens_table` import and `FrameKind` from `elspeth.contracts.enums`.

(k) Fixture sweep: `git grep -ln "IncompleteTokenSpec(" tests/ src/` — every constructor gains `lineage_path=()` (or the real path where the test crafts fork/expand tokens).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/engine/test_scheduler_work_codec.py tests/unit/core/landscape -n 12` then `pytest tests/unit/engine tests/integration/pipeline -n 12`
Expected: PASS (fixtures that call the enqueue verbs directly inherit the `()` default).

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/contracts/scheduler.py src/elspeth/engine/scheduler_work_codec.py \
  src/elspeth/core/landscape/scheduler/work_items.py src/elspeth/core/landscape/scheduler/queue.py \
  src/elspeth/core/landscape/scheduler_repository.py src/elspeth/core/landscape/scheduler/payload_codec.py \
  src/elspeth/core/landscape/scheduler/barrier.py src/elspeth/engine/processor.py \
  src/elspeth/engine/scheduler_drain.py src/elspeth/core/checkpoint/recovery.py \
  tests/unit/engine/test_scheduler_work_codec.py
git commit -m "feat(scheduler): lineage_path rides TokenWorkItem/BarrierEmission and every rehydrate seam"
```

(If Task 4 was held for the NOT-NULL reason, this is the combined commit.)

---

### Task 6: Durable writers — frames + `group_records` in `data_flow/tokens.py`

**Files:**
- Modify: `src/elspeth/core/landscape/data_flow/tokens.py` (`create_token` :327-402 gains a `lineage_frames` seam; `fork_token` :404-544; `expand_token` child-insert transaction :1236-1440; `coalesce_tokens` new-effect path :763-815)
- Modify: `src/elspeth/core/landscape/data_flow_repository.py` (facade pass-through for the `create_token` seam, :342 region pattern)
- Test: `tests/unit/core/landscape/test_token_recording.py`

**Interfaces:**
- Consumes: Task 4 tables; Task 1 `FrameKind`.
- Produces: every fork/expand child and every coalesce-merged token gets its full frame stack in `token_lineage_frames`, written in the SAME transaction as its token INSERT; every fork/expand opener mints one `group_records` row (kind `fork`/`expand`, `member_count` = roster size) in the opener's transaction; `data_flow.create_token(..., lineage_frames=...)` lets crafted-token tests write frames through the production Tier-1 writer (no raw test inserts). Replay/idempotent paths (`_reconcile_fork_replay`, `_reconcile_expansion_replay`, existing-coalesce-effect) return BEFORE the mint — re-drives never double-mint. **Durable strict pop (rulings 24/28):** `coalesce_tokens` refuses (Tier-1 `AuditIntegrityError`) parents whose innermost durable frame is not a shared-group FORK frame or whose remaining paths differ — routed through Task 1's `pop_closer_frame` (the engine twin in Task 8 calls the same function; neither layer re-implements the pop). WS1b's replay-predicate rewrite asserts the same rule on the replay side.
- **Canon (2026-08-22 synthesis — authoritative for siblings):** `group_records` mints for BOTH kinds — FORK in `fork_token`'s transaction and EXPAND in `expand_token`'s — and WS1a owns this behaviour across the campaign. The FORK roster AUTHORITY stays config (§5; the fork row is audit enrichment). Consequence for WS3: its FORK `token_parents` fallback in `_opener_token_id_for_group` is dead code under this landed behaviour and WS3 DELETES it.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/core/landscape/test_token_recording.py` (uses the file's `_setup`/`_make_row` helpers and `register_test_node`, verified present at :28-56):

```python
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import LineageFrame
from elspeth.core.landscape.schema import group_records_table, token_lineage_frames_table


def _frames_for(db: LandscapeDB, token_id: str, run_id: str = "run-1") -> list[tuple[int, str, str, str]]:
    with db.engine.connect() as conn:
        rows = conn.execute(
            select(
                token_lineage_frames_table.c.depth,
                token_lineage_frames_table.c.kind,
                token_lineage_frames_table.c.group_id,
                token_lineage_frames_table.c.member_key,
            )
            .where(token_lineage_frames_table.c.token_id == token_id)
            .where(token_lineage_frames_table.c.run_id == run_id)
            .order_by(token_lineage_frames_table.c.depth)
        ).fetchall()
    return [(int(r.depth), str(r.kind), str(r.group_id), str(r.member_key)) for r in rows]


def _group_record(db: LandscapeDB, group_id: str, run_id: str = "run-1"):
    with db.engine.connect() as conn:
        return conn.execute(
            select(group_records_table)
            .where(group_records_table.c.run_id == run_id)
            .where(group_records_table.c.group_id == group_id)
        ).one_or_none()


class TestUnifiedLineageWriters:
    def test_fork_writes_child_frames_and_group_record(self) -> None:
        db, factory = _setup()
        _row, parent = _make_row(factory)
        children, fork_group_id = factory.data_flow.fork_token(
            parent_ref=TokenRef(token_id=parent.token_id, run_id="run-1"),
            row_id=parent.row_id,
            branches=["a", "b"],
            step_in_pipeline=1,
        )
        for child, branch in zip(children, ["a", "b"], strict=True):
            assert _frames_for(db, child.token_id) == [(0, "fork", fork_group_id, branch)]
        record = _group_record(db, fork_group_id)
        assert record is not None
        assert (record.kind, record.opener_token_id, record.member_count) == ("fork", parent.token_id, 2)

    def test_expand_child_frames_stack_on_parent_frames(self) -> None:
        db, factory = _setup()
        _row, root = _make_row(factory)
        (branch_child, _other), fork_group_id = factory.data_flow.fork_token(
            parent_ref=TokenRef(token_id=root.token_id, run_id="run-1"),
            row_id=root.row_id,
            branches=["a", "b"],
            step_in_pipeline=1,
        )
        children, expand_group_id = factory.data_flow.expand_token(
            parent_ref=TokenRef(token_id=branch_child.token_id, run_id="run-1"),
            row_id=root.row_id,
            child_payloads=[{"v": 1}, {"v": 2}],
            output_contract=_MINIMAL_CONTRACT,
            step_in_pipeline=2,
        )
        for child in children:
            assert _frames_for(db, child.token_id) == [
                (0, "fork", fork_group_id, "a"),
                (1, "expand", expand_group_id, child.token_id),
            ]
        record = _group_record(db, expand_group_id)
        assert record is not None
        assert (record.kind, record.opener_token_id, record.member_count) == ("expand", branch_child.token_id, 2)

    def test_fork_replay_does_not_double_mint(self) -> None:
        db, factory = _setup()
        _row, parent = _make_row(factory)
        ref = TokenRef(token_id=parent.token_id, run_id="run-1")
        _children, fork_group_id = factory.data_flow.fork_token(parent_ref=ref, row_id=parent.row_id, branches=["a", "b"], step_in_pipeline=1)
        replayed, replay_group = factory.data_flow.fork_token(parent_ref=ref, row_id=parent.row_id, branches=["a", "b"], step_in_pipeline=1)
        assert replay_group == fork_group_id
        with db.engine.connect() as conn:
            count = conn.execute(select(func.count()).select_from(group_records_table)).scalar()
        assert count == 1
        assert _frames_for(db, replayed[0].token_id) == [(0, "fork", fork_group_id, "a")]

    def test_coalesce_pops_the_shared_fork_frame(self) -> None:
        db, factory = _setup()
        _row, parent = _make_row(factory)
        children, fork_group_id = factory.data_flow.fork_token(
            parent_ref=TokenRef(token_id=parent.token_id, run_id="run-1"),
            row_id=parent.row_id,
            branches=["a", "b"],
            step_in_pipeline=1,
        )
        merged = factory.data_flow.coalesce_tokens(
            parent_refs=[TokenRef(token_id=c.token_id, run_id="run-1") for c in children],
            row_id=parent.row_id,
            merged_payload={"v": 1},
            merged_contract=_MINIMAL_CONTRACT,
            coalesce_node_id="agg-0",
            step_in_pipeline=2,
        )
        assert _frames_for(db, merged.token_id) == []  # depth-1 fork popped to empty path

    def test_coalesce_refuses_parents_without_fork_frames(self) -> None:
        _db, factory = _setup()
        row_a, tok_a = _make_row(factory, row_index=0)
        tok_b = factory.data_flow.create_token(row_a.row_id)
        with pytest.raises(AuditIntegrityError, match="innermost FORK"):
            factory.data_flow.coalesce_tokens(
                parent_refs=[TokenRef(token_id=tok_a.token_id, run_id="run-1"), TokenRef(token_id=tok_b.token_id, run_id="run-1")],
                row_id=row_a.row_id,
                merged_payload={"v": 1},
                merged_contract=_MINIMAL_CONTRACT,
                coalesce_node_id="agg-0",
                step_in_pipeline=2,
            )

    def test_create_token_lineage_frames_seam_for_crafted_tokens(self) -> None:
        db, factory = _setup()
        row, _tok = _make_row(factory)
        crafted = factory.data_flow.create_token(
            row.row_id,
            branch_name="a",
            fork_group_id="fg-crafted",
            lineage_frames=(LineageFrame(kind=FrameKind.FORK, group_id="fg-crafted", member_key="a"),),
        )
        assert _frames_for(db, crafted.token_id) == [(0, "fork", "fg-crafted", "a")]
```

(Add `from sqlalchemy import func` to the file's sqlalchemy import if absent.)

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/core/landscape/test_token_recording.py -k UnifiedLineage -x`
Expected: FAIL — frames table empty / `create_token` has no `lineage_frames` kwarg.

- [ ] **Step 3: Implement** in `src/elspeth/core/landscape/data_flow/tokens.py`. Import `FrameKind` from `elspeth.contracts.enums`, `LineageFrame` and `pop_closer_frame` from `elspeth.contracts.identity`, `OrchestrationInvariantError` from `elspeth.contracts.errors`, and `group_records_table, token_lineage_frames_table` beside the module's existing schema imports. Add two private helpers on the repository class (typed on `LineageFrame` — WS1b consumes `_load_lineage_frames` with exactly this return shape):

```python
    @staticmethod
    def _load_lineage_frames(conn: Connection, *, token_id: str, run_id: str) -> tuple[LineageFrame, ...]:
        """The token's durable lineage frames, outermost first, as typed frames."""
        rows = conn.execute(
            select(
                token_lineage_frames_table.c.kind,
                token_lineage_frames_table.c.group_id,
                token_lineage_frames_table.c.member_key,
            )
            .where(token_lineage_frames_table.c.token_id == token_id)
            .where(token_lineage_frames_table.c.run_id == run_id)
            .order_by(token_lineage_frames_table.c.depth)
        ).fetchall()
        return tuple(
            LineageFrame(kind=FrameKind(row.kind), group_id=str(row.group_id), member_key=str(row.member_key))
            for row in rows
        )

    @staticmethod
    def _insert_lineage_frames(conn: Connection, *, token_id: str, run_id: str, frames: Sequence[LineageFrame]) -> None:
        for depth, frame in enumerate(frames):
            result = conn.execute(
                token_lineage_frames_table.insert().values(
                    token_id=token_id,
                    run_id=run_id,
                    depth=depth,
                    kind=frame.kind.value,
                    group_id=frame.group_id,
                    member_key=frame.member_key,
                )
            )
            if result.rowcount == 0:
                raise AuditIntegrityError(f"lineage frame INSERT affected zero rows (token_id={token_id}, depth={depth})")
```

(a) **`fork_token`** (:447-544, inside the existing `with self._db.write_connection() as conn:`, AFTER the `existing_terminal` replay return at :464-472): load `parent_frames = self._load_lineage_frames(conn, token_id=parent_ref.token_id, run_id=parent_ref.run_id)` once after `fork_group_id = generate_id()`; inside the child loop, after the `token_parents_table` insert (:499-510), add `self._insert_lineage_frames(conn, token_id=child_id, run_id=parent_ref.run_id, frames=[*parent_frames, LineageFrame(kind=FrameKind.FORK, group_id=fork_group_id, member_key=branch_name)])`. After the loop, before the parent-FORKED outcome insert (:524), mint the group record:

```python
            result = conn.execute(
                group_records_table.insert().values(
                    run_id=parent_ref.run_id,
                    group_id=fork_group_id,
                    kind=FrameKind.FORK.value,
                    opener_token_id=parent_ref.token_id,
                    member_count=len(branches),
                    created_at=now(),
                )
            )
            if result.rowcount == 0:
                raise AuditIntegrityError(f"fork_token: group_records INSERT affected zero rows (group_id={fork_group_id})")
```

(b) **`expand_token`** (:1258-1440, after the replay returns at :1281-1324): load `parent_frames` after the terminal check; inside the child loop after the `token_parents_table` insert (:1392-1403): `self._insert_lineage_frames(conn, token_id=child_id, run_id=parent_ref.run_id, frames=[*parent_frames, LineageFrame(kind=FrameKind.EXPAND, group_id=expand_group_id, member_key=child_id)])`. After the loop (before the parent-disposition insert :1417), mint the group record exactly as in (a) with `kind=FrameKind.EXPAND.value`, `member_count=len(child_data_refs)` (BATCH_CONSUMED flushes included — one row per aggregation flush is accepted audit enrichment, §4.3).

(c) **`coalesce_tokens`** (new-effect path only — after the `existing is not None` return at :776-786, before the merged-token INSERT at :792): durable strict pop, routed through Task 1's `pop_closer_frame` (never re-implemented inline), wrapping its engine-layer refusal in the Tier-1 error this layer owes:

```python
            parent_paths = [
                self._load_lineage_frames(conn, token_id=ref.token_id, run_id=run_id) for ref in parent_refs
            ]
            anchor = parent_paths[0]
            if not anchor or anchor[-1].kind is not FrameKind.FORK:
                raise AuditIntegrityError(
                    f"coalesce_tokens: parent token {parent_refs[0].token_id!r} has no innermost FORK lineage frame "
                    f"to pop (frames={anchor!r}); a closer pops exactly its own frame (spec rulings 24/28)"
                )
            shared_group_id = anchor[-1].group_id
            remaining_paths: set[tuple[LineageFrame, ...]] = set()
            for ref, path in zip(parent_refs, parent_paths, strict=True):
                try:
                    remaining_paths.add(pop_closer_frame(path, kind=FrameKind.FORK, group_id=shared_group_id))
                except OrchestrationInvariantError as exc:
                    # pop_closer_frame refuses empty paths, non-FORK innermost
                    # frames, and cross-group parents in one place; this layer
                    # re-raises as the Tier-1 audit error it owes.
                    raise AuditIntegrityError(
                        f"coalesce_tokens: durable strict pop refused for parent token {ref.token_id!r}: {exc}"
                    ) from exc
            if len(remaining_paths) != 1:
                raise AuditIntegrityError(
                    "coalesce_tokens: parents do not share their remaining lineage path after the pop; "
                    f"distinct remaining paths={len(remaining_paths)}"
                )
            merged_frames = remaining_paths.pop()
```

and after the merged-token INSERT (:792-799 block): `self._insert_lineage_frames(conn, token_id=token_id, run_id=run_id, frames=merged_frames)`.

(d) **`create_token` seam** (:327-402): add keyword param `lineage_frames: Sequence[LineageFrame] = ()` (documented: "crafted-token seam for tests and recovery tooling — production forks/expands write frames via their own transactions"); after the token INSERT (:390-400), when `lineage_frames` is non-empty, open the same `self._ops` write path used by the insert and call `_insert_lineage_frames(conn, token_id=..., run_id=..., frames=tuple(lineage_frames))`. Read the `_ops.execute_insert` implementation first and use the same connection discipline (if `_ops` is per-statement, wrap token INSERT + frames in `self._db.write_connection()` exactly as `create_row_with_token` :308-323 does).

(e) `data_flow_repository.py`: thread `lineage_frames` through the `create_token` facade method (mirror the `coalesce_tokens` pass-through at :342-360).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/core/landscape/test_token_recording.py -x` then `pytest tests/unit/core/landscape tests/integration/pipeline tests/property/audit -n 12`
Expected: PASS. If any suite drives `coalesce_tokens` over raw-seeded parents without fork frames, fix the FIXTURE via the `create_token(..., lineage_frames=...)` seam — never weaken the pop (the masquerade-gate rule: fix the fake to model the contract).

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/core/landscape/data_flow/tokens.py src/elspeth/core/landscape/data_flow_repository.py tests/unit/core/landscape/test_token_recording.py
git commit -m "feat(landscape): write token_lineage_frames + universal group_records in the minting transactions"
```

Then capture a trust-tier corpus snapshot and diff against the pre-task capture (this file is Tier-1-dense): add nothing.

---

### Task 7: Empty-expansion `group_records` mint (`member_count=0`)

**Files:**
- Modify: `src/elspeth/core/landscape/data_flow/tokens.py` (new verb beside `expand_token`)
- Modify: `src/elspeth/core/landscape/data_flow_repository.py` (facade)
- Modify: `src/elspeth/engine/tokens.py` (TokenManager wrapper)
- Modify: `src/elspeth/engine/token_traversal.py:198-226` (the `success_empty()` zero-row block)
- Test: `tests/unit/core/landscape/test_token_recording.py`, `tests/unit/engine/test_token_traversal_characterization.py`

**Interfaces:**
- Consumes: Task 4 `group_records` + `uq_group_records_opener`.
- Produces: `DataFlowRepository.record_empty_expansion(parent_ref: TokenRef) -> str` (returns the group_id; idempotent per opener; divergent replay raises `AuditIntegrityError`); `TokenManager.record_empty_expansion(parent_token: TokenInfo, run_id: str) -> str`. **The zero-row path mints iff `transform.creates_tokens` is True** (spec §4.3 as corrected by the 2026-08-22 synthesis: the empty-expansion mint is gated on the opener capability — a plain filter returning `success_empty()` is not an opener and mints NOTHING); WS2's `require_all` empty-group failure gets its durable referent from exactly this row.

- [ ] **Step 1: Write the failing tests.** In `test_token_recording.py`:

```python
    def test_record_empty_expansion_mints_zero_member_group_idempotently(self) -> None:
        db, factory = _setup()
        _row, parent = _make_row(factory)
        ref = TokenRef(token_id=parent.token_id, run_id="run-1")
        group_id = factory.data_flow.record_empty_expansion(ref)
        assert factory.data_flow.record_empty_expansion(ref) == group_id  # re-driven claim
        record = _group_record(db, group_id)
        assert record is not None
        assert (record.kind, record.opener_token_id, record.member_count) == ("expand", parent.token_id, 0)

    def test_record_empty_expansion_refuses_divergent_replay(self) -> None:
        _db, factory = _setup()
        _row, parent = _make_row(factory)
        ref = TokenRef(token_id=parent.token_id, run_id="run-1")
        factory.data_flow.expand_token(
            parent_ref=ref, row_id=parent.row_id, child_payloads=[{"v": 1}],
            output_contract=_MINIMAL_CONTRACT, step_in_pipeline=1,
        )
        with pytest.raises(AuditIntegrityError, match="divergent empty-expansion"):
            factory.data_flow.record_empty_expansion(ref)
```

In `tests/unit/engine/test_token_traversal_characterization.py`, extend the existing zero-row/FILTER_DROPPED characterization (find it via `grep -n "success_empty\|FILTER_DROPPED" tests/unit/engine/test_token_traversal_characterization.py` and read it before writing) with two new assertions at its end — the GATE is the contract, not just the mint:

- (a) when the zero-row transform declares `creates_tokens=True` (a real multi-row transform whose activation returned `success_empty()`), the run's `group_records` table holds exactly one row for the dropped token with `member_count == 0` and `kind == "expand"`;
- (b) when the zero-row result comes from a plain filter (`creates_tokens=False`), `group_records` holds NO row for that token.

(Query via the test's existing factory/db handle using `group_records_table`; if the existing characterization only drives one of the two transform shapes, add the missing twin using the file's own builders.)

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/core/landscape/test_token_recording.py -k empty_expansion -x`
Expected: FAIL — no `record_empty_expansion` attribute.

- [ ] **Step 3: Implement.**

(a) `data_flow/tokens.py`, beside `expand_token`:

```python
    def record_empty_expansion(self, parent_ref: TokenRef) -> str:
        """Mint the durable group record for a zero-row expansion (spec §4.3).

        The zero-row multi-row-transform path never calls expand_token, so a
        require_all empty group previously had no durable referent. Idempotent
        per opener (uq_group_records_opener: one token opens at most one
        group): a re-driven claim returns the committed group_id. An opener
        that already opened a NON-empty group is a divergent replay — Tier-1.
        """
        self._ownership.validate_token_run_ownership(parent_ref)
        with self._db.write_connection() as conn:
            existing = conn.execute(
                select(group_records_table.c.group_id, group_records_table.c.member_count)
                .where(group_records_table.c.run_id == parent_ref.run_id)
                .where(group_records_table.c.opener_token_id == parent_ref.token_id)
            ).one_or_none()
            if existing is not None:
                if int(existing.member_count) != 0:
                    raise AuditIntegrityError(
                        f"record_empty_expansion: opener {parent_ref.token_id!r} already opened group "
                        f"{existing.group_id!r} with member_count={existing.member_count}; divergent empty-expansion replay"
                    )
                return str(existing.group_id)
            group_id = generate_id()
            result = conn.execute(
                group_records_table.insert().values(
                    run_id=parent_ref.run_id,
                    group_id=group_id,
                    kind=FrameKind.EXPAND.value,
                    opener_token_id=parent_ref.token_id,
                    member_count=0,
                    created_at=now(),
                )
            )
            if result.rowcount == 0:
                raise AuditIntegrityError(f"record_empty_expansion: INSERT affected zero rows (group_id={group_id})")
            return group_id
```

(b) `data_flow_repository.py` facade: `def record_empty_expansion(self, parent_ref: TokenRef) -> str: return self.tokens.record_empty_expansion(parent_ref)` (mirroring :342-360).

(c) `engine/tokens.py` TokenManager, after `expand_token`:

```python
    def record_empty_expansion(self, parent_token: TokenInfo, run_id: str) -> str:
        """Durable member_count=0 group record for a zero-row expansion (spec §4.3)."""
        return self._data_flow.record_empty_expansion(TokenRef(token_id=parent_token.token_id, run_id=run_id))
```

(d) `engine/token_traversal.py` — in the zero-row block, FIRST lines inside `if len(transform_result.rows) == 0:` (before `_record_dropped_by_filter_outcome` at :202), add the GATED mint (`transform.creates_tokens` is the same attribute the block below the zero-row arm already validates for the multi-child path):

```python
                # Spec §4.3 (2026-08-22 synthesis correction): an empty expansion
                # mints a durable group record (member_count=0) — the referent a
                # bound require_all empty-group failure needs — but ONLY for an
                # opener: the mint is gated on creates_tokens. A plain filter's
                # success_empty() is not an expansion and mints nothing.
                # Idempotent per opener under re-driven claims.
                if transform.creates_tokens:
                    self._processor._token_manager.record_empty_expansion(
                        current_token,
                        self._processor._run_id,
                    )
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/core/landscape/test_token_recording.py tests/unit/engine/test_token_traversal_characterization.py -x` then `pytest tests/unit/engine tests/integration/pipeline -n 12`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/core/landscape/data_flow/tokens.py src/elspeth/core/landscape/data_flow_repository.py \
  src/elspeth/engine/tokens.py src/elspeth/engine/token_traversal.py \
  tests/unit/core/landscape/test_token_recording.py tests/unit/engine/test_token_traversal_characterization.py
git commit -m "feat(engine): mint member_count=0 group_records on empty expansions of token-creating transforms (spec §4.3)"
```

---

### Task 8: TokenManager in-memory frame push/pop + shared test-builder migration — SLICE BOUNDARY

**Files:**
- Modify: `src/elspeth/engine/tokens.py` (`fork_token` :295-305, `expand_token` :448-459, `coalesce_tokens` :344-369)
- Modify: `tests/integration/pipeline/test_barrier_intake_dispositions.py` (`_branch_token` :148-154 and its `_persist_token_for_scheduler` seeding)
- Modify: `tests/unit/engine/test_processor.py` (`_persist_blocked_scheduler_work` :162+, `_persist_token_for_scheduler`)
- Modify: `tests/integration/pipeline/test_aggregation_recovery.py` (its token builders, if they craft branch tokens — read first)
- Create: `tests/unit/engine/test_token_lineage_path.py`
- Test: as above plus the whole suite (slice boundary)

**Interfaces:**
- Consumes: Tasks 1–7.
- Produces: every `TokenInfo` minted by `TokenManager` carries its full `lineage_path`; `coalesce_tokens` performs the in-memory **strict pop** (`OrchestrationInvariantError` on violation — the engine-layer twin of Task 6's durable check); the three cross-tier shared test builders construct lineage-complete tokens (in-memory path AND durable frames via the `create_token(..., lineage_frames=...)` seam), which the e2e death matrix, timing-invariance and both Postgres suites inherit. WS1b consumes: the in-memory path is now write-complete everywhere, so the flip only changes READS. **Deliberately NOT here (WS1b flip items):** row_union release pop (ruling 27 — `processor.py:3043-3048` retain-identity stands until the fixture regeneration) and any accessor read of the path.

- [ ] **Step 1: Write the failing tests** — create `tests/unit/engine/test_token_lineage_path.py`:

```python
"""In-memory lineage-path push/pop pins (WS1a prep; spec §4.1a differential).

These tests pin BOTH truths during the prep phase: lineage_path is the
corrected (preservative) representation, while the stored tri-fields keep
today's destructive semantics until the WS1b flip. If a stored-field
assertion here reddens, a prep slice has leaked the flip early — stop.
"""

from __future__ import annotations

from elspeth.contracts import TokenInfo
from elspeth.contracts.enums import FrameKind, NodeType
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.types import NodeID
from elspeth.engine.tokens import TokenManager
from tests.fixtures.landscape import make_recorder_with_run, register_test_node

import pytest

_CONTRACT = SchemaContract(mode="OBSERVED", fields=(), locked=True)


def _manager() -> tuple[TokenManager, str]:
    setup = make_recorder_with_run(run_id="run-1", source_node_id="source-0", source_plugin_name="csv")
    register_test_node(setup.data_flow, "run-1", "gate-0", node_type=NodeType.TRANSFORM, plugin_name="passthrough")
    manager = TokenManager(setup.factory.data_flow, step_resolver=lambda node_id: 1)
    return manager, "run-1"


def _root(manager: TokenManager, run_id: str) -> TokenInfo:
    from elspeth.contracts import SourceRow

    source_row = SourceRow.valid({"col": "v"}, contract=_CONTRACT, source_row_index=0)
    return manager.create_initial_token(
        run_id=run_id, source_node_id="source-0", row_index=0, source_row=source_row,
        source_row_index=0, ingest_sequence=0,
    )


class TestForkPush:
    def test_fork_children_stack_a_fork_frame_and_keep_destructive_stored_fields(self) -> None:
        manager, run_id = _manager()
        root = _root(manager, run_id)
        children, fork_group_id = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), run_id)
        for child, branch in zip(children, ["a", "b"], strict=True):
            assert child.lineage_path == (LineageFrame(kind=FrameKind.FORK, group_id=fork_group_id, member_key=branch),)
            assert child.branch_name == branch          # stored field: unchanged semantics
            assert child.fork_group_id == fork_group_id


class TestExpandPush:
    def test_expand_inside_fork_branch_stacks_and_stored_fields_stay_destructive(self) -> None:
        manager, run_id = _manager()
        root = _root(manager, run_id)
        (child_a, _child_b), fork_group_id = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), run_id)
        grandchildren, expand_group_id = manager.expand_token(
            child_a, [{"v": 1}, {"v": 2}], _CONTRACT, NodeID("gate-0"), run_id,
        )
        for grandchild in grandchildren:
            assert grandchild.lineage_path == (
                LineageFrame(kind=FrameKind.FORK, group_id=fork_group_id, member_key="a"),
                LineageFrame(kind=FrameKind.EXPAND, group_id=expand_group_id, member_key=grandchild.token_id),
            )
            # §4.1a row 2 pinned at PREP: destructive stored semantics until WS1b —
            # expand_token drops fork_group_id, inherits branch_name in memory only.
            assert grandchild.branch_name == "a"
            assert grandchild.fork_group_id is None
            assert grandchild.expand_group_id == expand_group_id


class TestCoalesceStrictPop:
    def test_merge_pops_exactly_the_shared_fork_frame(self) -> None:
        manager, run_id = _manager()
        root = _root(manager, run_id)
        children, _fork_group_id = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), run_id)
        merged = manager.coalesce_tokens(children, PipelineRow({"v": 1}, _CONTRACT), NodeID("gate-0"), run_id)
        assert merged.lineage_path == ()
        assert merged.join_group_id is not None  # stored field until Task 10

    def test_merge_refuses_a_parent_with_no_fork_frame(self) -> None:
        manager, run_id = _manager()
        root = _root(manager, run_id)
        children, _fg = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), run_id)
        stray = root  # lineage_path == ()
        with pytest.raises(OrchestrationInvariantError, match="innermost FORK"):
            manager.coalesce_tokens([children[0], stray], PipelineRow({"v": 1}, _CONTRACT), NodeID("gate-0"), run_id)


class TestMemoryDurableConsistency:
    def test_in_memory_path_equals_durable_frames_after_each_primitive(self) -> None:
        from sqlalchemy import select
        from elspeth.core.landscape.schema import token_lineage_frames_table

        setup = make_recorder_with_run(run_id="run-1", source_node_id="source-0", source_plugin_name="csv")
        register_test_node(setup.data_flow, "run-1", "gate-0", node_type=NodeType.TRANSFORM, plugin_name="passthrough")
        manager = TokenManager(setup.factory.data_flow, step_resolver=lambda node_id: 1)
        root = _root(manager, "run-1")
        children, _fg = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), "run-1")
        grandchildren, _eg = manager.expand_token(children[1], [{"v": 1}], _CONTRACT, NodeID("gate-0"), "run-1")
        for token in (root, *children, *grandchildren):
            with setup.db.engine.connect() as conn:
                rows = conn.execute(
                    select(token_lineage_frames_table.c.kind, token_lineage_frames_table.c.group_id, token_lineage_frames_table.c.member_key)
                    .where(token_lineage_frames_table.c.token_id == token.token_id)
                    .where(token_lineage_frames_table.c.run_id == "run-1")
                    .order_by(token_lineage_frames_table.c.depth)
                ).fetchall()
            durable = tuple(LineageFrame(kind=FrameKind(r.kind), group_id=r.group_id, member_key=r.member_key) for r in rows)
            assert durable == token.lineage_path
```

Adjust the small fixture details (`SourceRow.valid` spelling, `make_recorder_with_run` attribute names) against `tests/fixtures/landscape.py` (a MODULE, not a package — the import in the snippet's header is already correct) and existing TokenManager tests (`tests/unit/engine/test_tokens.py`, `test_token_manager_pipeline_row.py`) — read both before finalizing; the ASSERTIONS above are the contract.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/engine/test_token_lineage_path.py -x`
Expected: FAIL — `child.lineage_path == ()` (no push yet).

- [ ] **Step 3: Implement** in `src/elspeth/engine/tokens.py` (import `LineageFrame` and `pop_closer_frame` from `elspeth.contracts.identity`, `FrameKind` from `elspeth.contracts.enums`):

`fork_token` child construction (:295-304) gains:

```python
                lineage_path=parent_token.lineage_path
                + (LineageFrame(kind=FrameKind.FORK, group_id=fork_group_id, member_key=child.branch_name),),
```

(`child.branch_name` is always set for fork children — the durable writer inserted it; `LineageFrame` rejects None/empty, which is the correct fail-closed shape.)

`expand_token` child construction (:448-458) gains:

```python
                lineage_path=parent_token.lineage_path
                + (LineageFrame(kind=FrameKind.EXPAND, group_id=expand_group_id, member_key=db_child.token_id),),
```

`coalesce_tokens` — before the `self._data_flow.coalesce_tokens` call (:344), add the strict pop (engine layer, rulings 24/28), routed through Task 1's `pop_closer_frame` — the same primitive the durable twin (Task 6c) calls, so the two layers cannot drift:

```python
        # Strict pop (rulings 24/28) via contracts.identity.pop_closer_frame: a
        # closer pops exactly its own innermost FORK frame; §7 rule 5 (WS2)
        # makes any other shape unbuildable, so a violation here is an
        # engine/validation bug, not a config shape.
        anchor = parents[0].lineage_path
        if not anchor or anchor[-1].kind is not FrameKind.FORK:
            raise OrchestrationInvariantError(
                f"coalesce_tokens: parent token {parents[0].token_id!r} has no innermost FORK frame to pop "
                f"(lineage_path={anchor!r})"
            )
        shared_group_id = anchor[-1].group_id
        remaining_paths = {
            pop_closer_frame(parent.lineage_path, kind=FrameKind.FORK, group_id=shared_group_id)
            for parent in parents
        }
        if len(remaining_paths) != 1:
            raise OrchestrationInvariantError(
                "coalesce_tokens: parents do not share their remaining lineage path after the pop"
            )
        merged_path = remaining_paths.pop()
```

(`pop_closer_frame` refuses empty paths, non-FORK innermost frames, and cross-group parents in one place — its `OrchestrationInvariantError` message carries "innermost FORK", which is what the Step 1 `match=` pins bind to.)

and the returned `TokenInfo` (:364-369) gains `lineage_path=merged_path,`.

- [ ] **Step 4: Migrate the shared builders (one early slice — test-harness scout risk 1/2).**
  - `tests/integration/pipeline/test_barrier_intake_dispositions.py` `_branch_token` (:148-154): give crafted branch tokens a real frame — add parameters `fork_group_id: str = "fg-row-1"` and build `lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id=fork_group_id, member_key=branch),)`, plus `fork_group_id=fork_group_id` on the stored field. Its `_persist_token_for_scheduler` twin must persist the SAME frames durably: thread `lineage_frames=token.lineage_path` into the `create_token` seam (Task 6d/6e — the seam takes `Sequence[LineageFrame]`, so the in-memory path threads through unchanged). The enqueue in `_persist_blocked_scheduler_work` (`tests/unit/engine/test_processor.py:187+`) gains `lineage_path=token.lineage_path`.
  - Read `tests/integration/pipeline/test_aggregation_recovery.py`'s builders; aggregation members are linear tokens (`lineage_path=()`), so they usually need no change — verify rather than assume.
  - Run the three consumer tiers: `pytest tests/integration/pipeline/test_barrier_intake_dispositions.py tests/unit/engine/test_processor.py tests/e2e/recovery/test_barrier_process_death_matrix.py tests/e2e/recovery/test_barrier_timing_invariance.py -n 12` and fix crafted fixtures (never the pop) until green. Postgres twins run in Task 11's full pass if testcontainers are available locally.

- [ ] **Step 5: Run the scoped suites, then the SLICE BOUNDARY full suite**

Run: `pytest tests/unit/engine tests/unit/core/landscape tests/integration/pipeline tests/property -n 12`
Then: record `git rev-parse HEAD`; run `pytest tests/`; re-record HEAD (must be unchanged); trust-tier corpus diff (add nothing); wardline gate command from Global Constraints.
Expected: full suite green with zero pinned-byte deltas (the dag_scenario_corpus manifest must NOT move — the new tables are not exported).

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/engine/tokens.py tests/unit/engine/test_token_lineage_path.py \
  tests/integration/pipeline/test_barrier_intake_dispositions.py tests/unit/engine/test_processor.py \
  tests/integration/pipeline/test_aggregation_recovery.py
git commit -m "feat(engine): TokenManager pushes/pops lineage frames (strict pop, rulings 24/28)"
```

---

### Task 8a: Nested differential corpus fixtures — fork-in-fork depth-2 + expand-in-fork (FROZEN)

> **Canon (2026-08-22 synthesis):** nested differential fixture authoring is a WS1a deliverable — this task, NOT WS1b. The protocols plan's §S1 freeze (its Task 3) is not authoritative until these fixtures exist, because a fixture created after the rewrite cannot be its own oracle; protocols Task 3 precondition 2 cites "WS1a Task 8a" by name. §4.1a rows 2–4 and depth 2+ have ZERO corpus substrate today: `sequential-nested-fork-coalesce` EXISTS but is two depth-1 regions in SERIES, never a region inside a region.

**Files:**
- Create: `tests/fixtures/dag_scenario_corpus/v1/nested-fork-in-fork/depth2-require-all.yaml`, `tests/fixtures/dag_scenario_corpus/v1/nested-fork-in-fork/input.csv`
- Create: `tests/fixtures/dag_scenario_corpus/v1/nested-expand-in-fork/explode-in-branch.yaml`, `tests/fixtures/dag_scenario_corpus/v1/nested-expand-in-fork/input.json`
- Modify: `tests/fixtures/dag_scenario_corpus/schema.py` (`EXPECTED_SCENARIOS` :75-100 — two appended entries)
- Modify: `docs/architecture/dag/scenario-corpus/v1/manifest.yaml` (two scenario entries + two harness evidence entries)
- Modify: `tests/fixtures/dag_scenario_corpus/oracle_freeze.py` (`SCENARIO_CLASSIFICATION` — two `FROZEN` entries; this module is CREATED by the protocols plan's Task 1)
- Modify: `tests/unit/architecture/test_dag_scenario_corpus_contract.py` (`EXPECTED_CASE_REGISTRY_SHA256` :536 rotation, dated A/B note per the file's :454-486 ledger discipline)
- Test: `tests/unit/architecture/test_dag_scenario_corpus_contract.py`, `tests/unit/architecture/test_oracle_freeze_registry.py`, `tests/integration/core/dag/test_dag_scenario_production_path.py -k "nested-fork-in-fork or nested-expand-in-fork"`

**Interfaces:**
- Consumes: nothing from Tasks 1–8 (pure corpus authoring; runs against TODAY's engine — that is the point: the frozen bytes are the pre-rewrite oracle). Consumes the protocols plan Task 1's `OracleClass`/`SCENARIO_CLASSIFICATION` module for the classification step.
- Produces: two new FROZEN corpus scenarios — `nested-fork-in-fork` (a fork inside a fork branch, closed inner-then-outer by two require_all coalesces: §4.1a depth-2 FORK/FORK substrate) and `nested-expand-in-fork` (a `json_explode` expansion inside one fork branch, pure fan-out terminals: §4.1a mixed FORK/EXPAND substrate) — wired into `EXPECTED_SCENARIOS` and `SCENARIO_CLASSIFICATION` as `FROZEN`. Protocols §S1's freeze (executed before the WS1b flip — WS1b Task 7) snapshots them; the WS1 checkpoint then requires their projections byte-identical across the whole rewrite.
- **Ordering note:** if `tests/fixtures/dag_scenario_corpus/oracle_freeze.py` does not exist yet (protocols Task 1 not landed), land everything else here and add the two classification entries in a coordinated commit with the protocols lane — `test_oracle_freeze_registry.py` fails closed on any `EXPECTED_SCENARIOS` entry without a classification, which is exactly the guard working.
- **STOP rule:** if the builder or runtime REJECTS either topology (build error or non-`completed` run), STOP — do not "fix" the fixture into a shape the engine accepts, and do not touch the engine. A depth-2 region the current engine cannot execute is a campaign premise shift (the differential oracle cannot exist) and goes to the maintainer.

- [ ] **Step 1: Author the fixture dirs.** `tests/fixtures/dag_scenario_corpus/v1/nested-fork-in-fork/input.csv` (same 3-row shape as `sequential-nested-fork-coalesce`):

```csv
id,value
1,10
2,20
3,30
```

`tests/fixtures/dag_scenario_corpus/v1/nested-fork-in-fork/depth2-require-all.yaml` — outer fork; the `outer_a` branch forks AGAIN; the inner region closes first (`merge_inner`), then the outer region closes over the merged token and the untouched `outer_b` sibling:

```yaml
sources:
  primary:
    plugin: csv
    on_success: outer_fork_input
    options:
      path: ${input_primary}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["id: int", "value: int"]}
concurrency:
  max_workers: 1
gates:
  - name: outer_fork
    input: outer_fork_input
    condition: "True"
    routes: {"true": fork, "false": discard}
    fork_to: [outer_a, outer_b]
  - name: inner_fork
    input: outer_a
    condition: "True"
    routes: {"true": fork, "false": discard}
    fork_to: [inner_a1, inner_a2]
coalesce:
  - name: merge_inner
    branches: {inner_a1: inner_a1, inner_a2: inner_a2}
    policy: require_all
    merge: nested
  - name: merge_outer
    branches: {outer_a: merge_inner, outer_b: outer_b}
    policy: require_all
    merge: nested
    on_success: output
sinks:
  output:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_output}
      format: jsonl
      schema: {mode: observed}
```

`tests/fixtures/dag_scenario_corpus/v1/nested-expand-in-fork/input.json` (same orders shape as `row-expansion-parent-child-recovery` — 2/1/3 children keeps the group-cardinality differential visible):

```json
[
  {"order_id": 1, "items": [{"sku": "A1", "qty": 2}, {"sku": "B2", "qty": 1}]},
  {"order_id": 2, "items": [{"sku": "C3", "qty": 5}]},
  {"order_id": 3, "items": [{"sku": "A1", "qty": 1}, {"sku": "D4", "qty": 3}, {"sku": "E5", "qty": 2}]}
]
```

`tests/fixtures/dag_scenario_corpus/v1/nested-expand-in-fork/explode-in-branch.yaml` — fork to two branches; `explode_path` expands INSIDE the branch (EXPAND frame stacked on a FORK frame), `control` terminates the sibling branch directly (pure fan-out is LEGAL — same shape as `fork-multiple-terminals-partial-failure`); no coalesce, because a require_all closer cannot receive N expansion children against one sibling arrival:

```yaml
sources:
  primary:
    plugin: json
    on_success: fork_input
    options:
      path: ${input_primary}
      on_validation_failure: discard
      schema: {mode: fixed, fields: ["order_id: int", "items: any"]}
concurrency:
  max_workers: 1
gates:
  - name: branch_gate
    input: fork_input
    condition: "True"
    routes: {"true": fork, "false": discard}
    fork_to: [explode_path, control]
transforms:
  - name: explode_items
    plugin: json_explode
    input: explode_path
    on_success: exploded
    on_error: discard
    options:
      array_field: items
      output_field: item
      include_index: true
      schema: {mode: observed}
sinks:
  exploded:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_exploded}
      format: jsonl
      schema: {mode: observed}
  control:
    plugin: json
    on_write_failure: discard
    options:
      path: ${output_control}
      format: jsonl
      schema: {mode: observed}
```

- [ ] **Step 2: Wire `EXPECTED_SCENARIOS` and run to verify the fail-closed mismatch.** Append to `EXPECTED_SCENARIOS` in `tests/fixtures/dag_scenario_corpus/schema.py` (after the `multi-worker-lease-reclaim-late-completion` entry — order IS the manifest ordinal order):

```python
    ("nested-fork-in-fork", "Nested fork within a fork branch (depth-2 bound regions)"),
    ("nested-expand-in-fork", "Row expansion inside a fork branch"),
```

Run: `pytest tests/unit/architecture/test_dag_scenario_corpus_contract.py -x`
Expected: FAIL — "DAG scenario IDs/order mismatch" (the manifest does not carry the scenarios yet).

- [ ] **Step 3: Add the manifest entries.** In `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`, append to the `evidence:` list:

```yaml
  - id: harness-nested-fork-in-fork-depth2-require-all
    kind: harness
    locator: nested-fork-in-fork:depth2-require-all
    claim: A depth-2 fork-in-fork with inner and outer require-all nested merges executes exact semantic schema propagation, output, parent-set, disposition, and scheduler runtime evidence.
    stages: [config, build, runtime]
  - id: harness-nested-expand-in-fork-explode-in-branch
    kind: harness
    locator: nested-expand-in-fork:explode-in-branch
    claim: A row expansion inside one fork branch executes exact semantic parent/child identity, per-branch outputs, disposition, and scheduler runtime evidence while the sibling branch terminates independently.
    stages: [config, build, runtime]
```

and append the two scenarios after `multi-worker-lease-reclaim-late-completion` (ordinals 16 and 17; titles byte-identical to Step 2's; the `*config_pass`/`*concurrency_unknown`/`*freeform_pass`/`*guided_fail`/`*round_trip_unknown`/`*scale_unknown` aliases are all anchored earlier in the file; `elspeth-ef29ef6ba4` is the existing corpus-gap owner issue the manifest already uses for audit/recovery/concurrency cells — the maintainer may later re-home these cells onto a dedicated issue):

```yaml
  - id: nested-fork-in-fork
    ordinal: 16
    title: Nested fork within a fork branch (depth-2 bound regions)
    cases:
      - id: depth2-require-all
        workflow: run
        fixture: nested-fork-in-fork/depth2-require-all.yaml
        input_fixtures:
          primary: nested-fork-in-fork/input.csv
        output_artifacts:
          output: output.jsonl
        expected: {"kind":"semantic_runtime","status":"completed","sink_outputs":[{"sink_name":"output","rows":["{\"outer_a\":{\"inner_a1\":{\"id\":1,\"value\":10},\"inner_a2\":{\"id\":1,\"value\":10}},\"outer_b\":{\"id\":1,\"value\":10}}","{\"outer_a\":{\"inner_a1\":{\"id\":2,\"value\":20},\"inner_a2\":{\"id\":2,\"value\":20}},\"outer_b\":{\"id\":2,\"value\":20}}","{\"outer_a\":{\"inner_a1\":{\"id\":3,\"value\":30},\"inner_a2\":{\"id\":3,\"value\":30}},\"outer_b\":{\"id\":3,\"value\":30}}"]}],"rows_processed":3,"rows_succeeded":3,"rows_failed":0,"audit_record_counts":[],"source_operation_count":1,"projection_sha256":"0000000000000000000000000000000000000000000000000000000000000000","projection_counts":{"rows":0,"tokens":0,"parent_links":0,"node_states":0,"routes":0,"terminal_dispositions":0,"scheduler_work":0}}
    dimensions:
      config: *config_pass
      build:
        status: pass
        evidence: [harness-nested-fork-in-fork-depth2-require-all]
      contracts:
        status: unknown
        reason: Nested-lineage contracts are pinned by the unified-lineage campaign suites, not yet by corpus contract evidence.
        owner_issue: elspeth-ef29ef6ba4
        exit_gate: A corpus contract case pins nested schema propagation for this scenario.
      runtime:
        status: pass
        evidence: [harness-nested-fork-in-fork-depth2-require-all]
      audit:
        status: unknown
        reason: The run case pins semantic runtime evidence only; exact durable and portable audit projections are not yet corpus evidence for this scenario.
        owner_issue: elspeth-ef29ef6ba4
        exit_gate: The scenario corpus case passes exact durable audit-state and exported-record assertions.
      recovery:
        status: unknown
        reason: No recovery case exists for this scenario.
        owner_issue: elspeth-ef29ef6ba4
        exit_gate: A scenario corpus recovery case passes database-reopen and public-resume assertions.
      concurrency: *concurrency_unknown
      freeform: *freeform_pass
      guided: *guided_fail
      round_trip: *round_trip_unknown
      scale: *scale_unknown

  - id: nested-expand-in-fork
    ordinal: 17
    title: Row expansion inside a fork branch
    cases:
      - id: explode-in-branch
        workflow: run
        fixture: nested-expand-in-fork/explode-in-branch.yaml
        input_fixtures:
          primary: nested-expand-in-fork/input.json
        output_artifacts:
          exploded: exploded.jsonl
          control: control.jsonl
        expected: {"kind":"semantic_runtime","status":"completed","sink_outputs":[{"sink_name":"control","rows":["{\"items\":[{\"qty\":2,\"sku\":\"A1\"},{\"qty\":1,\"sku\":\"B2\"}],\"order_id\":1}","{\"items\":[{\"qty\":5,\"sku\":\"C3\"}],\"order_id\":2}","{\"items\":[{\"qty\":1,\"sku\":\"A1\"},{\"qty\":3,\"sku\":\"D4\"},{\"qty\":2,\"sku\":\"E5\"}],\"order_id\":3}"]},{"sink_name":"exploded","rows":["{\"item\":{\"qty\":2,\"sku\":\"A1\"},\"item_index\":0,\"order_id\":1}","{\"item\":{\"qty\":1,\"sku\":\"B2\"},\"item_index\":1,\"order_id\":1}","{\"item\":{\"qty\":5,\"sku\":\"C3\"},\"item_index\":0,\"order_id\":2}","{\"item\":{\"qty\":1,\"sku\":\"A1\"},\"item_index\":0,\"order_id\":3}","{\"item\":{\"qty\":3,\"sku\":\"D4\"},\"item_index\":1,\"order_id\":3}","{\"item\":{\"qty\":2,\"sku\":\"E5\"},\"item_index\":2,\"order_id\":3}"]}],"rows_processed":3,"rows_succeeded":9,"rows_failed":0,"audit_record_counts":[],"source_operation_count":1,"projection_sha256":"0000000000000000000000000000000000000000000000000000000000000000","projection_counts":{"rows":0,"tokens":0,"parent_links":0,"node_states":0,"routes":0,"terminal_dispositions":0,"scheduler_work":0}}
    dimensions:
      config: *config_pass
      build:
        status: pass
        evidence: [harness-nested-expand-in-fork-explode-in-branch]
      contracts:
        status: unknown
        reason: Nested-lineage contracts are pinned by the unified-lineage campaign suites, not yet by corpus contract evidence.
        owner_issue: elspeth-ef29ef6ba4
        exit_gate: A corpus contract case pins nested schema propagation for this scenario.
      runtime:
        status: pass
        evidence: [harness-nested-expand-in-fork-explode-in-branch]
      audit:
        status: unknown
        reason: The run case pins semantic runtime evidence only; exact durable and portable audit projections are not yet corpus evidence for this scenario.
        owner_issue: elspeth-ef29ef6ba4
        exit_gate: The scenario corpus case passes exact durable audit-state and exported-record assertions.
      recovery:
        status: unknown
        reason: No recovery case exists for this scenario.
        owner_issue: elspeth-ef29ef6ba4
        exit_gate: A scenario corpus recovery case passes database-reopen and public-resume assertions.
      concurrency: *concurrency_unknown
      freeform: *freeform_pass
      guided: *guided_fail
      round_trip: *round_trip_unknown
      scale: *scale_unknown
```

The `sink_outputs` rows above are the statically derivable truths (nested-merge key order is sorted, matching `sequential-nested-fork-coalesce`'s pinned rows; the exploded rows are byte-identical to `row-expansion-parent-child-recovery`'s). The all-zero `audit_record_counts`/`projection_sha256`/`projection_counts` (and, for the expand case, `rows_succeeded`) are DELIBERATE seed sentinels for the next step's capture run — they are schema-valid, so the manifest loads, and semantically wrong, so the case cannot silently pass.

- [ ] **Step 4: Capture the observed runtime pins.** Run each case; the harness assertion prints expected vs observed for every semantic field:

Run: `pytest tests/integration/core/dag/test_dag_scenario_production_path.py -k "nested-fork-in-fork or nested-expand-in-fork" -x`
Expected: FAIL on the seeded sentinels, with the OBSERVED `audit_record_counts`, `projection_sha256`, `projection_counts` (and `rows_succeeded`, and the harness's `sink_outputs` ordering) in the failure output. Transcribe the observed values into the manifest — adjudicating each (token/parent-link counts must be consistent with the topology: e.g. depth2-require-all mints 1 root + 2 outer children + 2 inner children + 2 merged tokens per row), keep the `sink_outputs` order the harness reports, then re-run the same command.
Expected: PASS. **STOP-rule check:** a build rejection or a non-`completed` status here is the STOP condition above — escalate, do not adapt.

- [ ] **Step 5: Rotate the case-registry pin.** Run `pytest tests/unit/architecture/test_dag_scenario_corpus_contract.py -x`; the `EXPECTED_CASE_REGISTRY_SHA256` assertion (:5112) fails. Verify the delta is EXACTLY the two new cases (diff the normalized-case dump the test materializes), then update the constant at :536 with a dated rotation note in the comment block at :454-486, following its A/B discipline ("2026-08-2X: added nested-fork-in-fork:depth2-require-all and nested-expand-in-fork:explode-in-branch (WS1a Task 8a nested differential substrate); token-normalized old/new manifests diff only by the two new scenario blocks"). Adjudicate any OTHER failure in that suite individually — never blind-rerun a pin.

- [ ] **Step 6: Classify FROZEN.** In `tests/fixtures/dag_scenario_corpus/oracle_freeze.py` (protocols plan Task 1's module — see the Ordering note if it is not on the branch yet), add to `SCENARIO_CLASSIFICATION`:

```python
    "nested-fork-in-fork": OracleClass.FROZEN,
    "nested-expand-in-fork": OracleClass.FROZEN,
```

Run: `pytest tests/unit/architecture/test_oracle_freeze_registry.py tests/unit/architecture/test_dag_scenario_corpus_contract.py -x`
Expected: PASS (every `EXPECTED_SCENARIOS` entry classified; FROZEN needs no `MIGRATION.md`).

- [ ] **Step 7: Commit**

```bash
git add tests/fixtures/dag_scenario_corpus/v1/nested-fork-in-fork \
  tests/fixtures/dag_scenario_corpus/v1/nested-expand-in-fork \
  tests/fixtures/dag_scenario_corpus/schema.py tests/fixtures/dag_scenario_corpus/oracle_freeze.py \
  docs/architecture/dag/scenario-corpus/v1/manifest.yaml \
  tests/unit/architecture/test_dag_scenario_corpus_contract.py
git commit -m "test(corpus): nested-fork-in-fork + nested-expand-in-fork differential scenarios, classified FROZEN (WS1a Task 8a)"
```

---

### Task 9: Join-context carriers (additive) — `RowResult`, `PendingOutcome`, `WorkItem`, `CoalesceOutcome`

**Files:**
- Modify: `src/elspeth/contracts/results.py` (`RowResult` :531-601)
- Modify: `src/elspeth/contracts/engine.py` (`PendingOutcome` :147-190)
- Modify: `src/elspeth/engine/work_items.py` (`WorkItem` :26+, `WorkItemFactory.create`/`create_continuation` — read the class first)
- Modify: `src/elspeth/engine/coalesce_executor.py` (`CoalesceOutcome` :51-84; merged-token construction :1179-1212)
- Modify: `src/elspeth/engine/tokens.py` (`coalesce_tokens` returns `tuple[TokenInfo, str]`)
- Modify: `src/elspeth/engine/processor.py` (`_terminal_coalesce_row_result` :2750-2774; resume arm :2932-2938; `_notify_coalesce_of_lost_branch` :3229-3261; `_complete_committed_coalesce_residual` :3777-3804)
- Modify: `src/elspeth/engine/barrier_coordination.py` (`_fire_coalesce_merge` :667-701)
- Modify: `src/elspeth/engine/orchestrator/outcomes.py` (`_route_to_sink` :84-93)
- Test: `tests/unit/engine/test_token_lineage_path.py` (new class), `tests/unit/contracts/test_identity.py` siblings for results (`tests/unit/contracts/` results tests — locate with `git grep -ln "RowResult(" tests/unit/contracts`)

**Interfaces:**
- Consumes: Task 8's TokenManager.
- Produces (Task 10 flips readers onto these):
  - `RowResult.join_group_id: str | None = None`; `__post_init__` requires it non-None exactly when `path == TerminalPath.COALESCED` (mirroring the existing sink_name rule at :600) and forbids it otherwise.
  - `PendingOutcome.join_group_id: str | None = None`; validated `path == COALESCED` requires it, all other paths forbid it. **The sink batch grouping key (`sink_flush.pending_sort_key` :244-249) is NOT extended** — per-token join context must not split effect batches (sink-effect identity would churn).
  - `WorkItem.join_group_id: str | None` + factory params (merge-event attribute of the merged token's work item, ruling 20).
  - `CoalesceOutcome.join_group_id: str | None = None` (set iff `merged_token` is set).
  - `TokenManager.coalesce_tokens(...) -> tuple[TokenInfo, str]` — `(merged_token, join_group_id)`, the same shape as `fork_token`/`expand_token`.
  - `processor._terminal_coalesce_row_result(token, coalesce_name, *, join_group_id: str, context: str)`.
- **Decision (made here — resolves the spec's §4.1 carrier question):** the COALESCED accounting site (`outcomes.py:257`) reads the `RowResult` carrier; the sink-finalization site (`sink.py:760`) reads a per-token map derived in `sink_flush` from the buffered `(TokenInfo, PendingOutcome)` entries (Task 10). Never a DB query on either path — the pinned commitment holds.

- [ ] **Step 1: Write the failing tests** — append to `tests/unit/engine/test_token_lineage_path.py`:

```python
class TestJoinCarriers:
    def test_coalesce_tokens_returns_merged_token_and_join_group_id(self) -> None:
        manager, run_id = _manager()
        root = _root(manager, run_id)
        children, _fg = manager.fork_token(root, ["a", "b"], NodeID("gate-0"), run_id)
        merged, join_group_id = manager.coalesce_tokens(children, PipelineRow({"v": 1}, _CONTRACT), NodeID("gate-0"), run_id)
        assert isinstance(join_group_id, str) and join_group_id
        assert merged.join_group_id == join_group_id  # stored field agrees until Task 10 removes it

    def test_row_result_requires_join_group_id_exactly_for_coalesced(self) -> None:
        from elspeth.contracts.enums import TerminalOutcome, TerminalPath
        from elspeth.contracts.results import RowResult
        from elspeth.contracts.errors import OrchestrationInvariantError

        manager, run_id = _manager()
        token = _root(manager, run_id)
        with pytest.raises(OrchestrationInvariantError, match="join_group_id"):
            RowResult(token=token, final_data=token.row_data, outcome=TerminalOutcome.SUCCESS,
                      path=TerminalPath.COALESCED, sink_name="out")
        ok = RowResult(token=token, final_data=token.row_data, outcome=TerminalOutcome.SUCCESS,
                       path=TerminalPath.COALESCED, sink_name="out", join_group_id="jg-1")
        assert ok.join_group_id == "jg-1"
        with pytest.raises(OrchestrationInvariantError, match="join_group_id"):
            RowResult(token=token, final_data=token.row_data, outcome=TerminalOutcome.SUCCESS,
                      path=TerminalPath.DEFAULT_FLOW, sink_name="out", join_group_id="jg-1")
```

And a `PendingOutcome` pin beside the existing PendingOutcome tests (`git grep -ln "PendingOutcome(" tests/unit` — extend that file):

```python
def test_pending_outcome_join_group_id_is_coalesced_only() -> None:
    ok = PendingOutcome(outcome=TerminalOutcome.SUCCESS, path=TerminalPath.COALESCED, join_group_id="jg-1")
    assert ok.join_group_id == "jg-1"
    with pytest.raises(ValueError, match="join_group_id"):
        PendingOutcome(outcome=TerminalOutcome.SUCCESS, path=TerminalPath.COALESCED)
    with pytest.raises(ValueError, match="join_group_id"):
        PendingOutcome(outcome=TerminalOutcome.SUCCESS, path=TerminalPath.DEFAULT_FLOW, join_group_id="jg-1")
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/engine/test_token_lineage_path.py -k JoinCarriers -x`
Expected: FAIL — `coalesce_tokens` returns a bare `TokenInfo`.

- [ ] **Step 3: Implement.**

(a) `contracts/results.py` `RowResult`: add `join_group_id: str | None = None` after `sink_name`; in `__post_init__` beside the COALESCED sink_name rule (:600-601):

```python
        if self.path == TerminalPath.COALESCED and self.join_group_id is None:
            raise OrchestrationInvariantError("(SUCCESS, COALESCED) outcome requires join_group_id to be set")
        if self.path != TerminalPath.COALESCED and self.join_group_id is not None:
            raise OrchestrationInvariantError(f"RowResult.join_group_id is only valid for COALESCED results, got path={self.path!r}")
```

(b) `contracts/engine.py` `PendingOutcome`: add `join_group_id: str | None = None`; in `__post_init__`:

```python
        if self.path is TerminalPath.COALESCED and self.join_group_id is None:
            raise ValueError("PendingOutcome with path=COALESCED requires join_group_id")
        if self.path is not TerminalPath.COALESCED and self.join_group_id is not None:
            raise ValueError(f"PendingOutcome with path={self.path.name} must not have join_group_id")
```

(c) `engine/tokens.py` `coalesce_tokens`: change the return to `tuple[TokenInfo, str]`:

```python
        merged_info = TokenInfo(
            row_id=row_id,
            token_id=merged.token_id,
            row_data=merged_data,
            join_group_id=merged.join_group_id,
            lineage_path=merged_path,
        )
        return merged_info, merged.join_group_id
```

(`merged.join_group_id` is the audit `Token` model's field — the `tokens` COLUMN stays permanently, so this source is stable through WS1b.)

(d) `engine/coalesce_executor.py`: `CoalesceOutcome` gains `join_group_id: str | None = None` with a `__post_init__` arm: merged_token set ⇒ join_group_id required; held/failure ⇒ forbidden. At :1179 destructure `merged_token, join_group_id = self._token_manager.coalesce_tokens(...)` and add `join_group_id=join_group_id,` to the `CoalesceOutcome(` at :1206-1212. Sweep the file for any other `coalesce_tokens(` caller (`grep -n "coalesce_tokens(" src/elspeth/engine/coalesce_executor.py`).

(e) `engine/processor.py` `_terminal_coalesce_row_result` (:2750-2774): signature becomes `(self, token, coalesce_name, *, join_group_id: str, context: str)`; pass `join_group_id=join_group_id` into the `RowResult`. Update its three callers: resume arm :2932-2938 passes `join_group_id=spec.join_group_id` guarded by the arm's own `spec.join_group_id is not None` predicate (:2906); `_notify_coalesce_of_lost_branch` :3241 passes `join_group_id=outcome.join_group_id` (assert non-None via the CoalesceOutcome invariant); `barrier_coordination._fire_coalesce_merge` :671 passes `join_group_id=outcome.join_group_id` (the port is `Callable[..., RowResult]` :216 — keyword flows through).

(f) `engine/work_items.py`: `WorkItem` gains `join_group_id: str | None = None`; `WorkItemFactory.create`/`create_continuation` accept and thread it (read the factory first — mirror `coalesce_name`'s handling). The two merged-item construction sites pass it: `processor.py:3250-3253` (`join_group_id=outcome.join_group_id`) and `barrier_coordination.py:686-691` (same). `_complete_committed_coalesce_residual` (:3790-3793) passes `join_group_id=residual.result_join_group_id` on its `create_continuation`, and its terminal `RowResult` (:3795-3804) gains `join_group_id=residual.result_join_group_id`.

(g) `orchestrator/outcomes.py` `_route_to_sink` (:84-93): the `PendingOutcome(` gains `join_group_id=result.join_group_id if path is TerminalPath.COALESCED else None` — wait, `RowResult` already forbids it off-COALESCED, so simply `join_group_id=result.join_group_id`. `_route_to_sink`'s signature gains `join_group_id: str | None = None` threaded from `accumulate_row_outcomes`'s call (`join_group_id=result.join_group_id` — read the `_route_to_sink` call at :270-279 and thread there).

(h) Sweep for RowResult COALESCED constructions: `git grep -n "TerminalPath.COALESCED" src/elspeth/engine/` — every `RowResult(... path=TerminalPath.COALESCED ...)` site must now pass `join_group_id`: `scheduler_drain.py:918-924` (pending-sink replay — pass `join_group_id=scheduled.join_group_id`, the kept work-item field) plus the sites in (e)/(f). Test fixtures constructing COALESCED RowResults get the same treatment (`git grep -ln "TerminalPath.COALESCED" tests/ | xargs grep -ln "RowResult("`).

(i) Sweep every `coalesce_tokens(` caller for the new tuple return: `git grep -n "coalesce_tokens(" src/ tests/` — production is only `coalesce_executor.py:1179` (done in (d)), but tests that call `TokenManager.coalesce_tokens` directly must destructure. That includes Task 8's own `TestCoalesceStrictPop`: update its merge test to `merged, join_group_id = manager.coalesce_tokens(...)` (its `merged.lineage_path == ()` and `merged.join_group_id is not None` assertions stand as written until Task 10).

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/engine tests/unit/contracts tests/integration/pipeline -n 12`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/contracts/results.py src/elspeth/contracts/engine.py src/elspeth/engine/tokens.py \
  src/elspeth/engine/coalesce_executor.py src/elspeth/engine/processor.py src/elspeth/engine/barrier_coordination.py \
  src/elspeth/engine/work_items.py src/elspeth/engine/orchestrator/outcomes.py src/elspeth/engine/scheduler_drain.py \
  tests/unit/engine/test_token_lineage_path.py
git commit -m "feat(engine): join_group_id rides RowResult/PendingOutcome/WorkItem/CoalesceOutcome carriers"
```

(Include the swept test files in the pathspec list explicitly as they are touched.)

---

### Task 10: `join_group_id` leaves `TokenInfo` — reader rewires

**Files:**
- Modify: `src/elspeth/contracts/identity.py` (remove the field, its `__post_init__` loop entry :69, docstring :23)
- Modify: `src/elspeth/engine/orchestrator/outcomes.py:257-258` (read `result.join_group_id`)
- Modify: `src/elspeth/engine/executors/sink.py` (`write` :1264+, `_write_primary_effect` :661+, finalization member :752-764)
- Modify: `src/elspeth/engine/orchestrator/sink_flush.py:241-303` (build the per-token join map)
- Modify: `src/elspeth/engine/processor.py` (:2868-2878 drop kwarg; `_sink_emission_from_result` :3523 reads `result.join_group_id`; `_complete_committed_coalesce_residual` :3777-3786 drop kwarg)
- Modify: `src/elspeth/engine/scheduler_work_codec.py` (:110 reads `item.join_group_id`; :148 stops passing to `TokenInfo`, passes to the factory)
- Modify: `src/elspeth/core/landscape/scheduler/payload_codec.py:96-99` (drop kwarg)
- Modify: `src/elspeth/engine/scheduler_drain.py:897-907` (drop kwarg)
- Modify: `src/elspeth/engine/tokens.py` (drop the kwarg from the merged `TokenInfo`)
- Test: whole-tree sweep + the suites below

**Interfaces:**
- Consumes: Task 9's carriers.
- Produces (the WS1b baseline): `TokenInfo` has NO `join_group_id`; a merge is an event carried by `RowResult.join_group_id` / `PendingOutcome.join_group_id` / `WorkItem.join_group_id` / `TokenWorkItem.join_group_id` (contract field AND column both KEPT — **decision:** the `token_work_items.join_group_id` column stays, the `schema.py:848-855` COALESCED validity predicate keeps reading it unchanged, resolving the roster's §4.1-vs-§4.3 tension in favour of ruling 20's kept field); the `tokens.join_group_id` column and `uq_tokens_coalesce_result_identity` (:630-636) stay permanently (the `coalesce_effects` composite-FK anchor). `SinkExecutor.write` gains `join_group_id_by_token: Mapping[str, str | None]`.

- [ ] **Step 1: Write the failing test** — in `tests/unit/contracts/test_identity.py`:

```python
    def test_token_info_has_no_join_group_id(self) -> None:
        """§4.1 / ruling 20: a merge is an event, not a membership — the join
        context rides RowResult/PendingOutcome/WorkItem carriers, never TokenInfo."""
        import dataclasses

        assert "join_group_id" not in {f.name for f in dataclasses.fields(TokenInfo)}
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/contracts/test_identity.py -k no_join_group_id -x`
Expected: FAIL — the field exists.

- [ ] **Step 3: Implement, readers first (mechanical, grep-driven).**

(a) `orchestrator/outcomes.py:257`:

```python
        elif pair == (TerminalOutcome.SUCCESS, TerminalPath.COALESCED) and result.join_group_id is None:
            raise OrchestrationInvariantError(f"(SUCCESS, COALESCED) result missing join_group_id. Token: {result.token}")
```

(This arm is now double-covered by `RowResult.__post_init__`; keep it — the accounting seam is where a carrier regression would otherwise surface as a silent wrong count.)

(b) `orchestrator/sink_flush.py` (:253-300): inside the group loop, after `group_tokens`:

```python
                join_group_id_by_token = {
                    token.token_id: (pending.join_group_id if pending is not None else None)
                    for token, pending in group_pairs
                }
```

and pass `join_group_id_by_token=join_group_id_by_token,` to `sink_executor.write(...)` (:287-300). Do NOT touch `pending_sort_key` — batch composition must not move (sink-effect identity is a frozen invariant).

(c) `executors/sink.py`: `write` (:1264) gains `join_group_id_by_token: Mapping[str, str | None]` (keyword, required); thread into `_write_primary_effect` (:661), whose finalization-member build (:752-764) becomes:

```python
                join_group_id=(join_group_id_by_token[member.token_id] if pending_outcome.path is TerminalPath.COALESCED else None),
```

`token_by_id` (:751) stays for the other member fields. Sweep other `write(` callers: `git grep -n "sink_executor.write(\|\.write(" src/elspeth/engine/orchestrator/` and thread an explicit map (quarantine and non-coalesce lanes pass `{token.token_id: None for token in tokens}` or derive from their own pendings).

(d) `processor.py:2868-2878`: delete `join_group_id=spec.join_group_id,` (the resume ARMS still read `spec.join_group_id` — `IncompleteTokenSpec` keeps its field, sourced from the kept `tokens` column; only the `TokenInfo` kwarg dies). `:3523` becomes `join_group_id=result.join_group_id,`. `_complete_committed_coalesce_residual` :3777-3786 deletes the kwarg (the RowResult/WorkItem carriers from Task 9f already carry it).

(e) `scheduler_work_codec.py`: `ready_fields` :110 becomes `join_group_id=item.join_group_id,`; `work_item_from_scheduler` :148 removes the `TokenInfo` kwarg and passes `join_group_id=scheduled.join_group_id` into `self.create_work_item(...)` — extend the `WorkItemFactory` Protocol (:35-47) with `join_group_id: str | None = None`.

(f) `payload_codec.py:96-99` and `scheduler_drain.py:897-907`: delete the `join_group_id=` TokenInfo kwarg (drain's RowResult at :918 already carries `scheduled.join_group_id` from Task 9h).

(g) `engine/tokens.py` merged `TokenInfo`: delete `join_group_id=merged.join_group_id,` (the tuple return keeps the value).

(h) `contracts/identity.py`: delete the field (:38), its `__post_init__` tuple entry (:69), and the docstring line (:23); update `with_updated_data`'s docstring list.

(i) **Whole-tree sweep:** `git grep -n "join_group_id" src/elspeth/ | grep -v "tokens_table\|token_outcomes\|token_work_items\|coalesce_effects\|result_join_group_id\|TokenWorkItem\|BarrierEmission\|schema.py"` — adjudicate every remaining hit against the §4.1 replacement table; audit-surface reads (`contracts/audit.py` `Token`, exporters, MCP, web read models) read COLUMNS, not `TokenInfo`, and are untouched until WS1b. **Allowlist (do NOT sweep, kept by the spec):** `contracts/engine.py:63/:106` receipt fields, `restore_read_model.py` `coalesce_effects` joins, everything named in roster Risk note 8. Then the test sweep: `git grep -ln "join_group_id=" tests/ | xargs grep -ln "TokenInfo("` — fixtures constructing `TokenInfo(join_group_id=...)` move the value to the carrier the test actually exercises (or drop it when inert). Known instances to fix by name: `tests/unit/engine/test_scheduler_work_codec.py:100+` (`_make_item`'s default token — move `join_group_id="join-1"` off the `TokenInfo`; the round-trip invariant then proves the value survives via `WorkItem.join_group_id` → `ScheduledWorkFields` → `TokenWorkItem`, which is exactly the Task 10 contract), Task 8/9's own `TestCoalesceStrictPop`/`TestJoinCarriers` assertions on `merged.join_group_id` (delete the stored-field assertions; the tuple's second element is now the only in-memory truth), and the shared builders migrated in Task 8 if any crafts a merged token.

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/unit/contracts tests/unit/engine tests/unit/core/landscape tests/integration/pipeline tests/property -n 12`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/contracts/identity.py src/elspeth/engine/orchestrator/outcomes.py \
  src/elspeth/engine/orchestrator/sink_flush.py src/elspeth/engine/executors/sink.py \
  src/elspeth/engine/processor.py src/elspeth/engine/scheduler_work_codec.py \
  src/elspeth/core/landscape/scheduler/payload_codec.py src/elspeth/engine/scheduler_drain.py \
  src/elspeth/engine/tokens.py src/elspeth/engine/work_items.py tests/unit/contracts/test_identity.py
git commit -m "feat!: join_group_id leaves TokenInfo — merge context rides RowResult/PendingOutcome/WorkItem (ruling 20)"
```

(Add each swept test file to the pathspec explicitly.)

---

### Task 11: Slice-boundary verification + WS1b handoff statement

**Files:**
- Modify: `docs/agents/recent-code-hints.md` (one dated entry — see Step 4)
- No other production files.

**Interfaces:**
- Produces — **what WS1b consumes (state of the tree at WS1a exit):**
  1. `LineageFrame`/`FrameKind`/`lineage_path_{to,from}_json`/`innermost_{fork,expand}_frame`/`path_branch_name`/`path_fork_group_id`/`path_expand_group_id`/`pop_closer_frame` in `elspeth.contracts.{identity,enums}` — WS1b's derived accessors are properties over the three `path_*` wrappers; WS1b's row_union release pop and WS3's settle-member walk call `pop_closer_frame`. These are the campaign's ONLY lineage helpers — no sibling plan defines its own (2026-08-22 synthesis canon).
  2. `TokenInfo.lineage_path` write-complete on every in-memory token; stored `branch_name`/`fork_group_id`/`expand_group_id` still present with today's destructive semantics — **WS1b deletes them and installs the read-only properties** (`branch_name`/`fork_group_id` → innermost FORK frame's `member_key`/`group_id`; `expand_group_id` → innermost EXPAND frame's `group_id`), landing the §4.1a enumerated deltas atomically.
  3. Durable truth: `token_lineage_frames` written in every minting transaction; `group_records` for every opener (fork, expand, aggregation flush, empty expansion `member_count=0`); `group_losses` DDL in place, unwritten (WS3's writer). Epoch 34.
  4. `TokenWorkItem.lineage_path` (+ `lineage_path_json` column) round-trips through both codecs; every rehydrate seam (`work_item_from_scheduler`, `token_from_journal_item`, drain pending-sink replay, resume dispatch via `IncompleteTokenSpec.lineage_path`) reconstructs the path. WS1b adds the bidirectional codec-vs-frames-table integrity cross-check on restore; WS3 walks `TokenWorkItem.lineage_path` for settle-member.
  5. Strict pop enforced at BOTH layers for coalesce (engine `OrchestrationInvariantError`, durable `AuditIntegrityError` — both routed through `contracts.identity.pop_closer_frame`, the one pop primitive). **Row_union release does NOT pop yet** (ruling 27 is a WS1b flip item — `processor.py:3043-3048` retain-identity and the `row-union-interleave` fixture stand until the WS1b regeneration).
  6. `join_group_id` is gone from `TokenInfo`; carriers per Task 10's Interfaces block; `tokens.join_group_id` + `uq_tokens_coalesce_result_identity` + `token_work_items.join_group_id` (+ the `:848-855` predicate) KEPT.
  7. `GroupLossSpec` for WS3.
  8. Export surface unchanged: the three new tables are NOT exported — WS1b's fixture-regeneration slice takes the export/manifest churn in one adjudicated rotation (fixture-oracle Risk 4).
  9. WS1b still owes, before its flip: the pre-flip frozen-oracle capture (protocols plan Task 3 — stable projections emitted to committed files, fixture-oracle Risk 6) and the replay-predicate rewrite (`_reconcile_fork_replay`/`_reconcile_expansion_replay` onto frames equality, §4.4). The nested differential fixtures are NOT a WS1b debt: Task 8a of THIS plan authored `nested-fork-in-fork` and `nested-expand-in-fork` and classified them FROZEN (protocols §S1 executes before the WS1b flip — WS1b Task 7). (`sequential-nested-fork-coalesce` exists in v1 but is two depth-1 regions in series — it is NOT nested substrate, which is why Task 8a exists.)

- [ ] **Step 1: Full-suite A/B.** `git rev-parse HEAD` → run `pytest tests/` (~18 min; main checkout, NOT a worktree — worktrees under-collect eval-globbing suites) → `git rev-parse HEAD` again; if HEAD moved, re-run rather than diagnose. Expected: green.
- [ ] **Step 2: Trust-tier corpus diff.** Compare the post-Task-10 capture against the pre-Task-1 capture: `diff <(sort pre.txt) <(sort post.txt)` — you must have ADDED NOTHING. If a finding moved because code moved, adjudicate; never reshape code to dodge churn.
- [ ] **Step 3: Wardline.** Run the gate-of-record command from Global Constraints; exit 0 required.
- [ ] **Step 4: Leave the trap notes.** Add ONE dated entry to `docs/agents/recent-code-hints.md`: `lineage_path` is write-only during WS1 prep (reading it from production code before the WS1b flip is a dual-representation defect); crafted-token tests MUST use `create_token(..., lineage_frames=...)` or the durable strict pop rejects them; a zero-row `success_empty()` traversal of a `creates_tokens=True` transform now mints a `group_records` row — plain filters mint nothing (test row-counts over that table accordingly); the two `nested-*` corpus scenarios are FROZEN differential oracles — never regenerate their snapshots.
- [ ] **Step 5: Commit**

```bash
git add docs/agents/recent-code-hints.md
git commit -m "docs(agents): WS1a lineage-core conventions and traps"
```

---

## Coverage check against assigned scope

| Assigned scope item | Task(s) |
|---|---|
| `LineageFrame` + `FrameKind` + accessors backed by stored fields during prep | 1, 2 (helpers landed; stored fields remain the read path; properties are WS1b) |
| `path_branch_name`/`path_fork_group_id`/`path_expand_group_id` + `pop_closer_frame` (synthesis canon 1) | 1 (called by 6 and 8; imported by WS1b/WS3/WS4 — no sibling defines its own) |
| Nested differential corpus fixtures, classified FROZEN (synthesis canon 6) | 8a |
| `GroupLossSpec` contract | 3 |
| `token_lineage_frames` + `group_records` + `group_losses` DDL | 4 |
| `TokenManager.fork_token`/`expand_token`/`coalesce_tokens` push/pop + strict pop assert | 8 (engine), 6 (durable twin) |
| Universal `group_records` mint incl. `success_empty` zero-row change | 6, 7 |
| `TokenWorkItem` gains `lineage_path` (codec round-trip, payload_codec purity) | 5 |
| `join_group_id` TokenInfo removal + concrete carriers (outcomes.py:257 → `RowResult`; sink.py:760 → per-token map from buffered `PendingOutcome`s) | 9, 10 |
| WS1b interface statement | 11 |

## Decisions ratified by the 2026-08-22 cross-plan synthesis (formerly "Open Questions")

Every decision this plan surfaced while drafting has been RATIFIED by the synthesis canon — nothing here remains open. For the record (do not re-litigate):

1. **`group_records` for FORK openers** (Task 4/6) — RATIFIED: minted for BOTH kinds; WS1a is authoritative for siblings; FORK roster AUTHORITY stays config (§5); WS3 deletes its FORK `token_parents` fallback in `_opener_token_id_for_group` as dead code.
2. **`GroupLossSpec` has no `recorded_by`** (Task 3) — RATIFIED: the staging verb stamps `recorded_by` at write time from the lease owner it already holds; WS3 honours this seam.
3. **`token_work_items.join_group_id` column stays** (Task 10) — RATIFIED (ruling 20): WS1b's column-deletion slice allowlists it; the `schema.py:848-855` COALESCED validity predicate is unchanged.
4. **Empty-expansion mint scope** (Task 7) — RATIFIED as GATED: the mint fires only for `transform.creates_tokens=True` (Task 7d shows the gate); a plain filter's `success_empty()` mints nothing.

The campaign's one genuinely open item — the `examples/row_union_ab_experiment/settings_screened.yaml` replacement story — is a maintainer pedagogy call and is owned outside this plan; it stays open there, not here.
