# Sink Field-Keep Inversion — Executable Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the step-2 sink field review with a post-validation field-keep review
where the user chooses, per sink, what to keep from the validated candidate's real
arriving-field inventory — three modes: keep these + anything new (default), keep
whatever arrives, keep exactly these (planner-revision path).

**Architecture:** Wire churn is deliberately minimal: `TurnResponse` is **unchanged** —
the keep response rides the existing `chosen` + `control_signal` keys (`None` →
plus-new, `PASSTHROUGH` → keep-all, new `KEEP_EXACT` → exact). One new turn type
(`field_keep`), no new step — keep turns are legal at `STEP_3_TRANSFORMS`, between the
proposal accept and the wire turn (see Flow placement). The arriving inventory is
**recorded by candidate validation itself** (the sink-contract check already walks
`_connection_definite_emits`, state.py:4161/:4945-5009) — no second walker, so the keep
list provably equals what validation enforced. Keep decisions live in a NEW session
field (`keep_decisions`, outside the planning anchor hash) and are materialized through
the existing `guided_reviewed_sink_options` seam (planning.py:1244) at candidate
binding. `mode=exact` never amends server-side — it becomes a structured revision
request the planner answers (composer invariant 1).

**Tech Stack:** Python 3.12 (frozen slots dataclasses, `freeze_fields`,
`freeze_guided_str_sequence`), StrEnum wire vocabularies, React/TS with hand-mirrored
wire types, pytest / vitest / Playwright staging.

**Spec:** `docs/superpowers/plans/2026-08-19-invert-guided-sink-field-keep.md` (the
design plan: decisions D1–D7, risk register + comparison, blast radius). This plan
refines D4's wire encoding: keep-mode is carried by `control_signal`, not a new
response key. All D1–D7 semantics are otherwise unchanged.

## Global Constraints

- **Read `docs/agents/recent-code-hints.md` before writing any code** (whole-tree AST
  gates; not optional).
- Composer invariants absolute: no server-authored pipeline structure (`mode=exact`
  routes through the planner; extend `test_no_chain_authoring_path.py`); no
  tutorial-special paths (ADR-031).
- `release/0.7.2`, shared checkout: stage by pathspec, commit only your own hunks, no
  `git stash`.
- Every new dataclass with container fields: `freeze_fields` in `__post_init__`; after
  each backend task run
  `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing
  .venv/bin/elspeth-lints check --rules immutability.freeze_guards --root src/elspeth`
  (rc 0; the pre-commit hook does NOT fire on src/ edits).
- Wire shapes mirror into `frontend/src/types/guided.ts` in the same task; parity
  harness (`scripts/cicd/parity_harness.py`) is name-presence with the unmirrored
  ratchet at ceiling 10/10.
- Keep-stage submissions are Tier-3 input: `chosen` is validated against the
  **server-held** inventory; the client's echo of the payload is never trusted.
- Full `pytest tests/ -n 12` before merge; trust-tier corpus compare (expect no new
  findings, not zero); wardline gate of record with `--fail-on-inert` (6-ERROR
  fingerprint baseline).
- Skills text changes go live only after `sudo -n systemctl restart elspeth-web`
  (pre-authorized).

## Wire design (settled by code reads, 2026-08-19)

| Concern | Decision | Anchor |
|---|---|---|
| Response shape | `TurnResponse` TypedDict **unchanged** — reuse `chosen` + `control_signal` | protocol.py:432-444 |
| Keep-all | `ControlSignal.PASSTHROUGH` **retained and repurposed** (docstring rewrite; it already means "pass all fields through") | protocol.py:423-431 |
| Exact | New `ControlSignal.KEEP_EXACT = "keep_exact"` | same enum |
| Plus-new | `control_signal: None` + non-empty `chosen` (the default) | — |
| Contradictions | fail-closed 400s mirroring the step-2 precedent codes (`guided_step2_no_fields_selected` / `guided_step2_passthrough_conflict` → `guided_keep_no_fields` / `guided_keep_passthrough_conflict`) | emitters.py:459-471 docstring |
| New payload | `FieldKeepPayload` — the only new wire object | Task 1 |
| Turn legality | `_LEGAL_TURN_MATRIX[STEP_3_TRANSFORMS]` gains `FIELD_KEEP` — **no new GuidedStep** | protocol.py:576-601 |
| Solver | `resolve_sink` drops `required_fields` + `schema_mode`; `SinkOutputResolved.schema_mode` derived from options | chat_solver.py:2640-2965 |

## Flow placement (settled by routes recon, 2026-08-19)

**The keep stage lives between the step-3 proposal accept and the step-4 wire turn**,
inside `STEP_3_TRANSFORMS` — a distinct TURN TYPE, not a distinct step (amends design
decision D2; the user experience is unchanged — the turn renders as its own card).
The recon facts that force this:

1. **Only ONE site mints a fresh `COMPLETED`**: the STEP_4 `confirm_wiring` accept
   (guided.py:4601). The other three `TerminalState` sites are exits/restores
   (:1177 restore re-validates a previously-completed state; :2532 generic exit;
   :3562 exit-from-completed). The confirm request is atomic and fence-laden
   (audited dispatch, recovery bindings, custody checks :4469-4708) — the keep
   stage must NOT interpose user interaction inside it, and does not touch it.
2. **The candidate is already validated at the step-3→4 boundary**:
   `policy_validation = catalog.validate_composition_state(candidate)`
   (guided.py:4050). Task 2's inventories ride that same result. Keep turns fire
   exactly when `policy_validation.validation.is_valid`; an invalid candidate
   falls through to the wire turn showing blockers, as today.
3. **The anchor invariant forbids amending `reviewed_outputs` post-planning**
   (`active_proposal.reviewed_anchor_hash` must equal
   `guided_reviewed_anchor_hash(...)` over reviewed sources AND outputs —
   state_machine.py:1122-1128). Keep decisions therefore live in a NEW session
   field, `keep_decisions`, deliberately OUTSIDE the anchor: they are
   post-planning user facts the planner never consumed. `required_fields` on
   `SinkOutputResolved` stays planner-era authority (from the schema form /
   solver options only, after Task 5).
4. **`legal_pending_shape` must admit the keep turn while `active_proposal` is
   held** — state_machine.py:1134-1142 PLUS two duplicate copies at
   `sessions/service.py:1316` and `:1939` (grep
   `trailing.step is GuidedStep.STEP_4_WIRE` before editing).
5. **Replay needs ZERO edits**: `guided_replay.py` is payload-id-keyed and
   turn-type-agnostic (`_turn_response` :350). Obligations are structural: legal
   in `_LEGAL_TURN_MATRIX`; emitted via `_append_server_turn_record`
   (guided.py:688) so `TurnRecord.payload_hash == GuidedReplayTurn.payload_id`;
   never emitted alongside a terminal (:400 exclusion).
6. **Wire review displays the FINAL contracts**: after the last keep response the
   candidate is re-bound with the keep overlay and revalidated BEFORE
   `build_step_4_wire_turn` — the user confirms what will actually run, and the
   confirm path's hash-custody checks (guided.py:4475-4479,
   pipeline_commit.py:477) bind against the post-keep candidate.

Flow: STEP_3 accept → validate → `field_keep` turn per sink (still STEP_3) →
last keep response → rebind + revalidate → wire turn → STEP_4 → confirm
(unchanged) → terminal. `mode=exact` at any keep turn detours: structured
revision request → planner round → new proposal → accept → keep turns
re-present only where inventory changed.

---

### Task 1: Protocol — turn type, step, signal, payload, validator, TS mirror

**Files:**
- Modify: `src/elspeth/web/composer/guided/protocol.py`
- Modify: `src/elspeth/web/frontend/src/types/guided.ts`
- Test: `tests/unit/web/composer/guided/test_protocol.py`

**Interfaces (produces):** `TurnType.FIELD_KEEP`, `ControlSignal.KEEP_EXACT`,
`FieldKeepPayload` (TypedDict), `_validate_field_keep_payload`. No new `GuidedStep`
(Flow placement §1-2).

- [ ] **Step 1: Failing tests** (mirror the existing per-turn-type validator tests in
      `test_protocol.py`):

```python
def test_field_keep_payload_valid() -> None:
    payload = {
        "question": "Which fields should this output keep?",
        "output_stable_id": "8b6c9d1e-3f57-4a8b-9c0d-1e2f3a4b5c6d",
        "output_name": "results",
        "guaranteed_fields": ["amount", "summary"],
        "open_remainder": True,
        "declared_fields": None,
        "default_mode": "plus_new",
    }
    assert validate_payload(TurnType.FIELD_KEEP, payload) is None

def test_field_keep_payload_rejects_nonbool_open_remainder() -> None:
    payload = {**_VALID_FIELD_KEEP, "open_remainder": "yes"}
    assert validate_payload(TurnType.FIELD_KEEP, payload) is not None

def test_field_keep_payload_rejects_duplicate_guaranteed_fields() -> None:
    payload = {**_VALID_FIELD_KEEP, "guaranteed_fields": ["a", "a"]}
    assert validate_payload(TurnType.FIELD_KEEP, payload) is not None

def test_field_keep_payload_rejects_unknown_default_mode() -> None:
    payload = {**_VALID_FIELD_KEEP, "default_mode": "exact"}
    assert validate_payload(TurnType.FIELD_KEEP, payload) is not None

def test_step_3_admits_field_keep_turns() -> None:
    assert TurnType.FIELD_KEEP in _LEGAL_TURN_MATRIX[GuidedStep.STEP_3_TRANSFORMS]
```

- [ ] **Step 2: Run to verify failure** —
      `pytest tests/unit/web/composer/guided/test_protocol.py -k field_keep -x`
      (fails: `FIELD_KEEP` not defined).

- [ ] **Step 3: Implement.** Exact edits to `protocol.py`:

Enum members (append inside the existing enums — TurnType at :29, GuidedStep at :455,
ControlSignal at :418):

```python
    FIELD_KEEP = "field_keep"          # TurnType
    # ControlSignal — rewrite PASSTHROUGH's comment for its new home, add:
    # "Keep exactly the chosen fields and nothing else" for FIELD_KEEP at
    # STEP_3_TRANSFORMS. Never amends the sink server-side: the transition converts
    # it into a structured planner revision request (composer invariant 1).
    KEEP_EXACT = "keep_exact"
```

Payload TypedDict (beside `MultiSelectWithCustomPayload`):

```python
class FieldKeepPayload(TypedDict):
    question: str
    output_stable_id: str
    output_name: str
    # Fields provably arriving at this sink per the validated candidate's own
    # sink-contract walk — the SAME set validation enforced, never a re-derivation.
    guaranteed_fields: Sequence[str]
    # True unless every path into the sink ends at an extras-firewall producer:
    # an open remainder means "plus anything else your source carries" and the
    # client MUST render that; an empty remainder is "no gap provable", never
    # "coverage proven" (ADR-007 observed sources abstain).
    open_remainder: bool
    # Non-null when the reviewed sink options carry an explicit fixed/flexible
    # schema: chosen must stay inside it (relocated 398f150859 guard).
    declared_fields: Sequence[str] | None
    default_mode: str  # closed: always "plus_new"
```

Registrations: `_LEGAL_TURN_MATRIX[GuidedStep.STEP_3_TRANSFORMS]` (:576-601) gains
`TurnType.FIELD_KEEP` (alongside PROPOSE_PIPELINE / SINGLE_SELECT / SCHEMA_FORM);
`_REQUIRED_KEYS` and `_ALLOWED_KEYS` both gain
`TurnType.FIELD_KEEP: frozenset({"question", "output_stable_id", "output_name",
"guaranteed_fields", "open_remainder", "declared_fields", "default_mode"})`
(:668-730); `_PAYLOAD_VALIDATORS` gains
`TurnType.FIELD_KEEP: _validate_field_keep_payload` (:2289-2296).

Validator (house conventions from `_validate_multi_select_payload`, :1123-1135):

```python
def _validate_field_keep_payload(payload: Mapping[str, Any]) -> str | None:
    if (error := _current_text_error(payload["question"], "payload.question", nonempty=True)) is not None:
        return error
    if (error := _canonical_uuid_error(payload["output_stable_id"], "payload.output_stable_id")) is not None:
        return error
    if (error := _current_text_error(payload["output_name"], "payload.output_name", nonempty=True)) is not None:
        return error
    guaranteed, error = _current_string_sequence(payload["guaranteed_fields"], "payload.guaranteed_fields", unique=True)
    if error is not None:
        return error
    if type(payload["open_remainder"]) is not bool:
        return "payload.open_remainder must be a boolean"
    declared = payload["declared_fields"]
    if declared is not None:
        declared_seq, error = _current_string_sequence(declared, "payload.declared_fields", unique=True)
        if error is not None:
            return error
    if payload["default_mode"] != "plus_new":
        return "payload.default_mode is outside the closed field-keep vocabulary"
    return None
```

- [ ] **Step 4: TS mirror** in `types/guided.ts` (all in this task — the type layer is
      one closed system; the decoder/dispatcher registries are Task 7's):

```ts
/** Wire: FieldKeepPayload (protocol.py — field-keep stage). */
export interface FieldKeepPayload {
  question: string;
  output_stable_id: string;
  output_name: string;
  guaranteed_fields: string[];
  /** True = render "+ anything else your source carries"; an exhaustive list is
   *  only claimable when this is false. */
  open_remainder: boolean;
  declared_fields: string[] | null;
  default_mode: "plus_new";
}
```

  1. `TurnType` union (guided.ts:18) gains `"field_keep"`. (`GuidedStep` union is
     unchanged — no new step.)
  2. `TurnPayload` closed union (:143) gains
     `TurnPayloadEnvelope<"field_keep", FieldKeepPayload>`.
  3. `ControlSignal` union (:27) gains `"keep_exact"`. The existing escape arm of
     `GuidedRespondAction` (`chosen: null … control_signal:
     Exclude<ControlSignal, "reject">`, :269-277) must NOT silently admit
     `keep_exact` with `chosen: null` — tighten it to
     `Exclude<ControlSignal, "reject" | "keep_exact">`.
  4. `GuidedRespondAction` (:242-320) gains the exact-keep arm — the union is
     CLOSED, so without this member the widget cannot legally emit it:

```ts
  | (UnboundProposalFields & {
      /** Keep EXACTLY these fields: routed to a planner revision server-side. */
      chosen: NonEmptyStringArray;
      edited_values: null;
      custom_inputs: null;
      control_signal: "keep_exact";
    })
```

- [ ] **Step 5: Verify** — scoped pytest green, `npx vitest run src/types/guided.test.ts`
      green, freeze_guards rule rc 0 (no new dataclass yet, still run it), parity
      harness green.
- [ ] **Step 6: Commit** — `git add src/elspeth/web/composer/guided/protocol.py
      src/elspeth/web/frontend/src/types/guided.ts
      tests/unit/web/composer/guided/test_protocol.py` then
      `git commit -m "feat(composer): field_keep turn type + protocol surface"`.

### Task 2: Validation records each sink's arriving inventory

**Files:**
- Modify: `src/elspeth/web/composer/state.py` (sink-contract check region :4945-5009;
  the walk is `_connection_definite_emits` :4161 and `_producer_emit_profile` :4009-4140)
- Test: the existing home of sink-contract validation tests (locate via
  `grep -rn "sink_contract_violation" tests/unit/web/composer/ -l`)

**Interfaces (produces):** the validation result object gains a per-sink record
`sink_field_inventories: Mapping[str, SinkFieldInventory]` where

```python
@dataclass(frozen=True, slots=True)
class SinkFieldInventory:
    guaranteed_fields: tuple[str, ...]   # sorted
    open_remainder: bool

    def __post_init__(self) -> None:
        freeze_fields(self, "guaranteed_fields")
```

**Semantics (binding):** `guaranteed_fields` is exactly the definite-emits set the
sink-contract check computed for that sink's producer chain (state.py:4945-5009 —
record it where the check already holds it; do NOT run a second walk).
`open_remainder` is `False` only when the arriving profile is provably closed —
every contributing producer profile has `propagates == False` at the sink boundary
(the extras-firewall condition the same region already evaluates via
`allows_extra_fields`, :4122); any observed-source abstention or propagating
producer ⇒ `True`. The asymmetry is one-way: it is sound to render a closed list
when `open_remainder` is False, and never sound to claim completeness otherwise.

- [ ] **Step 1: Failing tests** — three pipeline shapes through the real validation
      entry point (reuse the module's existing fixture builders):
      (a) fixed-schema source → sink: inventory == declared fields, remainder False;
      (b) observed source → sink: inventory ⊇ observed columns, remainder True;
      (c) source → extras-firewall transform (fixed output schema) → sink:
      inventory == the transform's declared emit set, upstream extras absent,
      remainder False.
- [ ] **Step 2: Run to verify failure** (attribute does not exist).
- [ ] **Step 3: Implement** — thread the recorded per-sink sets onto the validation
      result alongside the existing `sink_contract_violation` reporting; sorted
      tuples; `SinkFieldInventory` housed in `state.py` beside its producer.
- [ ] **Step 4: Scoped pytest + freeze_guards rule (new dataclass!).**
- [ ] **Step 5: Commit** (`feat(composer): validation records per-sink arriving field inventory`).

### Task 3: Keep state + `FieldKeepResponse` + keep transition

**Files:**
- Modify: `src/elspeth/web/composer/guided/state_machine.py`,
  `src/elspeth/web/composer/guided/stage_transitions.py`,
  `src/elspeth/web/composer/guided/resolved.py`,
  `src/elspeth/web/sessions/service.py` (:1316, :1939 — pending-shape copies)
- Test: `tests/unit/web/composer/guided/test_state_machine.py`,
  `tests/unit/web/composer/guided/test_stage_transitions.py`

**State-machine additions (state_machine.py — GuidedSession is closed schema; this
is a schema CUT, `GUIDED_SESSION_SCHEMA_VERSION` 11 → 12, no older decoder;
note the class docstring still says "schema version 10" — fix it while there.
The store-recreation boundary lives in `web/sessions/models.py`; pre-release wipe
posture covers existing rows — sessions.db may be wiped, auth.db never):**

```python
@dataclass(frozen=True, slots=True)
class FieldKeepInventory:
    """Guided-session copy of one sink's validation-recorded arriving inventory."""

    guaranteed_fields: tuple[str, ...]
    open_remainder: bool

@dataclass(frozen=True, slots=True)
class FieldKeepDecision:
    """One sink's settled keep decision. mode='exact' never persists — it detours
    to a planner revision before any decision is recorded."""

    mode: Literal["plus_new", "all"]
    kept_fields: tuple[str, ...]   # empty iff mode == "all"
```

Both records: `freeze_fields` on the tuple field, `to_dict`/`from_dict` with exact
keysets (idiom: `SinkOutputResolved`, resolved.py:292-340). New `GuidedSession`
fields (serialization foursome per the house pattern — `_GUIDED_SESSION_KEYS`
keyset :65, `to_dict` :1202, `from_dict` pre-checks :1305 with the
`GUIDED_MAX_COMPONENTS_PER_KIND` bound, `from_dict` construction :1358 with
canonical-UUID keys — and `__post_init__` detach-validate-freeze :1036-1046):

```python
    pending_keep: Mapping[str, FieldKeepInventory] = field(default_factory=dict)
    keep_decisions: Mapping[str, FieldKeepDecision] = field(default_factory=dict)
```

`__post_init__` invariants added in THIS task:
- `pending_keep` nonempty ⇒ `active_proposal is not None` (keep review exists only
  under a live proposal);
- `pending_keep` and `keep_decisions` keys ⊆ `output_order` set;
- `legal_pending_shape` (:1134-1142) gains the third arm
  `(self.step is GuidedStep.STEP_3_TRANSFORMS and trailing.step is
  GuidedStep.STEP_3_TRANSFORMS and trailing.turn_type is TurnType.FIELD_KEEP)` —
  and the two DUPLICATE copies at `sessions/service.py:1316` / `:1939` get the
  same arm in the same commit.
- The "COMPLETED terminal requires keep coverage" invariant is deliberately
  DEFERRED to Task 4 (adding it here would break every existing completion test
  before the flow exists).

**stage_transitions / resolved (produces):**
`sink_schema_mode_from_options(options) -> str` (public, in `resolved.py` — the
relocated `_sink_schema_mode`, stage_transitions.py:667-679, unchanged semantics:
absent schema/mode ⇒ `"observed"`);
`FieldKeepResponse`; `apply_field_keep_response(session, target_id, turn, response)
-> GuidedSession | FieldKeepRevisionRequest` — the inventory is read from
`session.pending_keep[target_id]` (server-held at emission; never client echo).

Response record (idiom copied from `FieldSelectionResponse`, stage_transitions.py:166-183):

```python
@dataclass(frozen=True, slots=True)
class FieldKeepResponse:
    """Closed response to ``field_keep`` at STEP_3_TRANSFORMS."""

    chosen: Sequence[str]
    control_signal: ControlSignal | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "chosen", freeze_guided_str_sequence(self.chosen, "FieldKeepResponse.chosen"))
        if self.control_signal is not None and type(self.control_signal) is not ControlSignal:
            raise TypeError("FieldKeepResponse.control_signal must be ControlSignal or None")
        freeze_fields(self, "chosen")
```

Revision-request record (consumed by Task 6; NEVER serialized to the provider
verbatim — the revision path owns its projection):

```python
@dataclass(frozen=True, slots=True)
class FieldKeepRevisionRequest:
    """User keeps EXACTLY these fields at one sink; the planner authors the how."""

    output_stable_id: str
    output_name: str
    keep_fields: tuple[str, ...]

    def __post_init__(self) -> None:
        freeze_fields(self, "keep_fields")
```

Transition skeleton (guards are the house set — `_require_active_turn` with
`expected_step=GuidedStep.STEP_3_TRANSFORMS, expected_turn_type=TurnType.FIELD_KEEP`, then
`_require_sink_intent`-style resolution against `reviewed_outputs`):

```python
def apply_field_keep_response(
    session: GuidedSession,
    target_id: str,
    turn: AnsweredTurn,
    response: FieldKeepResponse,
) -> GuidedSession | FieldKeepRevisionRequest:
    _require_active_turn(session, turn, expected_step=GuidedStep.STEP_3_TRANSFORMS, expected_turn_type=TurnType.FIELD_KEEP)
    if target_id not in session.pending_keep:
        raise InvariantError("field keep requires a pending server-held inventory for the target output")
    inventory = session.pending_keep[target_id]
    reviewed = session.reviewed_outputs[target_id]  # KeyError = invariant break upstream
    chosen = tuple(response.chosen)
    if len(set(chosen)) != len(chosen):
        raise ValueError("chosen fields must be unique")
    signal = response.control_signal
    if signal is ControlSignal.PASSTHROUGH:
        if chosen:
            raise ValueError("guided_keep_passthrough_conflict: keep-all cannot name fields")
        selected: tuple[str, ...] = ()
    elif signal is ControlSignal.KEEP_EXACT or signal is None:
        if not chosen:
            raise ValueError("guided_keep_no_fields: field keep requires fields or explicit keep-all")
        unknown = set(chosen) - set(inventory.guaranteed_fields)
        if unknown:
            raise ValueError(f"chosen fields are not in the validated arriving inventory: {sorted(unknown)!r}")
        declared = reviewed_schema_declared_field_names(
            reviewed.options["schema"] if "schema" in reviewed.options else None
        )
        if declared:
            undeclared = sorted(set(chosen) - set(declared))
            if undeclared:
                raise ValueError(
                    f"selected fields are not declared by the sink's explicit schema: {', '.join(undeclared)}. "
                    f"Declared fields are: {', '.join(sorted(declared))}."
                )
        if signal is ControlSignal.KEEP_EXACT:
            return FieldKeepRevisionRequest(
                output_stable_id=target_id, output_name=reviewed.name, keep_fields=chosen
            )
        selected = chosen
    else:
        raise ValueError("field keep accepts only the passthrough or keep_exact control signals")
    # NEVER amend reviewed_outputs here: it feeds the planning anchor hash
    # (state_machine.py:1122-1128) and the proposal would instantly stale.
    # Keep decisions are post-planning authority, carried beside the anchor.
    pending = dict(session.pending_keep)
    del pending[target_id]
    decisions = dict(session.keep_decisions)
    decisions[target_id] = FieldKeepDecision(
        mode="all" if signal is ControlSignal.PASSTHROUGH else "plus_new",
        kept_fields=selected,
    )
    return replace(session, pending_keep=pending, keep_decisions=decisions)
```

**Materialization seam (same task):** `guided_reviewed_sink_options` (planning.py:1244)
gains an optional `keep_decision: FieldKeepDecision | None = None` parameter; when
present and `mode == "plus_new"`, the kept fields are merged into
`options.schema.required_fields` via the existing
`_sink_options_with_declared_required_fields` helper (:1225-1243); `mode == "all"`
merges nothing. `bind_guided_reviewed_components` (planning.py:2545) passes
`guided.keep_decisions.get(stable_id)` at its per-output rebind (:2744 region) — the
single seam every builder already passes through (its own docstring demands this).

- [ ] **Step 1: Failing tests**: state_machine — round-trip `obj ==
      from_dict(to_dict())` with both new fields populated; pending-keep-without-
      proposal rejected; unknown stable-id keys rejected; trailing FIELD_KEEP turn
      + active_proposal accepted by the pending-shape invariant (and rejected
      before the invariant edit). stage_transitions — plus-new happy path records
      `FieldKeepDecision(mode="plus_new", kept_fields=(...))` and drains
      `pending_keep` (frozen-at-rest: compare tuples, the FG3 lesson); keep-all
      records `mode="all"`, empty kept_fields; exact returns a
      `FieldKeepRevisionRequest` and records NOTHING; chosen outside inventory
      rejected; explicit fixed schema constrains chosen; passthrough+chosen
      conflict; empty+no-signal conflict; missing pending inventory rejected;
      wrong step / terminal / consumed rejected. planning — `guided_reviewed_sink_options`
      with a plus-new decision merges kept fields into `schema.required_fields`;
      with an "all" decision returns options byte-identical; with `None` unchanged
      from today.
- [ ] **Step 2: Verify failure. Step 3: Implement (including the `resolved.py`
      relocation with both old call sites updated, and the two service.py
      pending-shape copies). Step 4: scoped pytest + freeze_guards rc 0 (three new
      dataclasses). Step 5: Commit**
      (`feat(composer): keep-decision state + field-keep transition`).

### Task 4: Routes — keep turns between proposal accept and wire review

**Files:** `src/elspeth/web/sessions/routes/composer/guided.py` (step-3 accept
branch :4037-4098; `_schema8_transition` :2339-2490; `_build_get_guided_turn`
:409-456), `src/elspeth/web/composer/guided/emitters.py` (new
`build_field_keep_turn`), `state_machine.py` (the completion invariant),
`guided_chat_atomic.py` (only if its `_transition_request` needs a `field_keep`
arm — the chat solver never answers keep turns; they are human-only, so expect a
REJECTION arm, not a synthesis arm).
Test: the guided route integration suites
(`tests/integration/web/composer/guided/`).

**The wiring (recon-grounded):**
1. **Emission** — in the step-3 accept branch, after
   `policy_validation = catalog.validate_composition_state(candidate)`
   (guided.py:4050): when `policy_validation.validation.is_valid` AND
   `guided.keep_decisions` does not yet cover `output_order`, do NOT emit the wire
   turn. Instead: populate `pending_keep` from Task 2's
   `sink_field_inventories` (converted to `FieldKeepInventory`), and emit the
   FIRST `field_keep` turn via the standard appender `_append_server_turn_record`
   (guided.py:688) — session stays at `STEP_3_TRANSFORMS`, `active_proposal`
   held (the Task 3 pending-shape arm makes this persistable). One turn per sink,
   sequentially, in `output_order` order. Payload from `build_field_keep_turn(
   output_stable_id, output_name, inventory, declared_fields)` — a plain emitter
   beside `build_step_2_multi_select_turn`'s old slot, `declared_fields` from
   `reviewed_schema_declared_field_names(reviewed.options.get("schema"))`.
   Invalid candidate ⇒ today's behaviour exactly (wire turn renders blockers).
2. **Response dispatch** — new arm in `_schema8_transition` (beside the :2468
   step-2 branch): `turn_type is TurnType.FIELD_KEEP and guided.step is
   GuidedStep.STEP_3_TRANSFORMS` ⇒ `_schema8_only_response_fields(body, "chosen",
   "control_signal")`, build `FieldKeepResponse`, call
   `apply_field_keep_response`. A `FieldKeepRevisionRequest` return routes to
   Task 6; a `GuidedSession` return falls through to next-turn projection.
   Because the arm lives inside `_schema8_answer_and_project_next`, the manual
   POST (guided.py:4722) and the chat route (guided_chat_atomic.py:1597) are
   covered by the same code automatically.
3. **Next-turn projection** — `_build_get_guided_turn` gains: `pending_keep`
   nonempty ⇒ next `field_keep` turn for the first uncovered sink in
   `output_order`. All covered ⇒ the existing wire-turn build path runs — with
   the candidate REBOUND through `bind_guided_reviewed_components` (which now
   sees `keep_decisions`, Task 3) and REVALIDATED before `build_step_4_wire_turn`,
   so the wire review and the confirm path's hash custody
   (guided.py:4475-4479, pipeline_commit.py:477) bind the post-keep candidate.
   The re-bound candidate hash supersedes the proposal's draft binding through
   the SAME staging path the original accept used (`service.stage_guided_pipeline_
   proposal`, guided.py:4805-4818 region) — server-side rebinding of reviewed +
   keep authority over unchanged planner structure, the same materialization
   class as binding itself, never authoring.
4. **The completion invariant (deferred from Task 3)** — `GuidedSession.
   __post_init__` gains: `terminal.kind is COMPLETED` ⇒ `keep_decisions` covers
   every id in `output_order` and `pending_keep` is empty. This is the
   "no terminal without keep" pin at the strongest seam — every construction and
   every `from_dict` load re-checks it; the single fresh-COMPLETED site
   (guided.py:4601) and the restore site (:1177) both inherit it with zero route
   edits. Exits (EXITED_TO_FREEFORM) are exempt. Update existing completion-test
   fixtures in the same commit.
5. **Replay** — zero `guided_replay.py` edits (payload-id-keyed); the emission in
   (1) uses `_append_server_turn_record`, satisfying
   `TurnRecord.payload_hash == GuidedReplayTurn.payload_id`, and keep turns are
   never emitted alongside a terminal (the :400 exclusion).

- [ ] **Step 1: Failing integration tests**: (a) a valid candidate accept emits a
      `field_keep` turn, NOT the wire turn; (b) after keep responses for every
      sink, the wire turn appears and its rendered sink contracts include the
      kept fields; (c) constructing/loading a COMPLETED session without keep
      coverage raises (the invariant pin); (d) the chat path hits the same keep
      turns (parameterized with the wizard path — the R2-F4 seam-divergence pin);
      (e) an invalid candidate skips keep and renders wire blockers as today.
- [ ] **Step 2: Verify failure. Step 3: Implement. Step 4: scoped pytest.
      Step 5: Commit** (`feat(composer): field-keep review between proposal accept
      and wire confirmation`).

### Task 5: Deletions — step-2 field review and pre-planning machinery

**Files/sites (enumerated by read, 2026-08-19):**
- `emitters.py:449-492` — `build_step_2_multi_select_turn` (and its emission call
  site in the routes layer, from the Task 4 recon).
- `protocol.py` — `MultiSelectWithCustomPayload` TypedDict; `TurnType.
  MULTI_SELECT_WITH_CUSTOM` member; `_LEGAL_TURN_MATRIX` STEP_2_SINK entry shrinks to
  `{SINGLE_SELECT, SCHEMA_FORM, REVIEW_COMPONENTS}`; `_REQUIRED_KEYS`/`_ALLOWED_KEYS`
  entries; `_validate_multi_select_payload` (:1123-1135) + `_PAYLOAD_VALIDATORS` entry.
  **`ControlSignal.PASSTHROUGH` is RETAINED** (repurposed by Task 1) — rewrite its
  comment block (:423-431), do not delete.
- `stage_transitions.py` — `FieldSelectionResponse` (:166-183); the field-review
  transition (:1240-1320); `_candidate_fields` (:654-664). `_sink_schema_mode`
  (:667-679) already relocated by Task 3.
- `planning.py` — `guided_unproducible_output_fields` (:1266-1310) and its `__all__`
  entry (:3846); `service.py` planner-context enrichment consumer + the step-3
  no-transform branch reference (find via
  `git grep -n guided_unproducible_output_fields`).
- `chat_solver.py` — `_STEP_2_SINK_TOOL` (:2640-2676): drop `required_fields` and
  `schema_mode` from `required` and `properties`; parser (:2900-2965): `expected`
  set shrinks to `{"name", "plugin", "options", "on_write_failure"}`, drop both
  per-field validations, construct
  `SinkOutputResolved(..., required_fields=(), schema_mode=sink_schema_mode_from_options(options), ...)`;
  revision projection serializer (:1396-1438) drops the solver-authored
  `schema_mode`/`required_fields` echo (they are keep-stage authority now).
- Frontend — `MultiSelectWithCustomTurn.tsx` + `.test.tsx`; the widget-owned
  `guided.css` block (:1477-1505; shared `.guided-chip-*` classes STAY — other
  widgets and Task 7 use them; check `guidedSurface.test.ts` / `ChatPanel.test.tsx`
  file-content pins); and the full turn-type registry sweep —
  `types/guided.ts`: `MultiSelectWithCustomPayload`, its `TurnPayload` envelope
  member, `"multi_select_with_custom"` in the `TurnType` union, and the
  `custom_inputs` arms of `GuidedRespondAction` (:245-262) if no other turn uses
  `custom_inputs` (verify with `git grep -n custom_inputs frontend/src` first);
  `guidedDecoder.ts`: `decodeTurnType` case (:1007), `LEGAL_TURNS.step_2_sink`
  entry (:38), `decodeMultiSelectPayload` (:1092), `decodeTurn` arm (:1774);
  `GuidedTurn.tsx`: routing-table comment + switch arm (:109-118) — the `never`
  exhaustiveness check enforces completeness of the removal.
- Tests — the 9 backend test files referencing the old turn
  (`git grep -l "MULTI_SELECT_WITH_CUSTOM\|multi_select_with_custom\|FieldSelectionResponse" tests/`),
  plus solver suites (`test_chat_solver.py`, `test_sink_discovery_loop.py`,
  `test_step_chat_sink_driver.py`) updated to the narrowed schema.

- [ ] **Step 1: Enumerate before deleting** — run the `git grep` above plus
      `git grep -n "escape_label\|guided_step2_no_fields_selected\|guided_step2_passthrough_conflict\|build_step_2_multi_select_turn\|guided_unproducible_output_fields\|_candidate_fields"`;
      every hit is deleted, updated, or justified in the commit message.
- [ ] **Step 2: Delete + update in one sweep** (tests updated in the same commit —
      the suite must be green at every commit).
- [ ] **Step 3: Verify absence** — re-run the grep set; zero hits outside this plan
      and the ADR. Scoped pytest + vitest + parity harness (removed names must be
      gone from BOTH trees; ratchet must not gain an 11th unmirrored site).
- [ ] **Step 4: Commit** (`refactor(composer)!: remove step-2 field review — keep stage owns the sink field contract`).

### Task 6: `mode=exact` → planner revision routing `[FENCE-AT-EXECUTION]`

> The one remaining execution-time lookup: the exact entry function of the guided
> revision-proposal path (the prose AMEND flavour — `GuidedRevisionBindingResult`
> construction at planning.py:3012/:3178, its solver-side projection at
> chat_solver.py:1396-1438, and the planner-service revision dispatch). The recon
> did not read that path; the implementer reads it first. Everything else below
> is settled.

**Files:** `guided.py` (the FIELD_KEEP dispatch arm's revision branch, from
Task 4.2), `planning.py`, `chat_solver.py`; tests extend
`test_no_chain_authoring_path.py` + one integration case.

**Settled semantics:**
1. On `KEEP_EXACT`, `apply_field_keep_response` returned a
   `FieldKeepRevisionRequest` with NO state change. The route branch then: clears
   `pending_keep` entirely (every held inventory is about to stale), preserves
   existing `keep_decisions`, and dispatches a planner revision through the SAME
   machinery a user-prose amend uses — the structured fact ("output `<name>`
   keeps exactly: [fields]") joins the revision context (this value class —
   output field names — is already in the redacted planner context, so zero new
   egress). The planner decides projection-transform vs fixed sink schema.
   The server authors neither (`provider="server"` never authors — extend
   `test_no_chain_authoring_path.py` with this path).
2. The revision produces a NEW proposal (new anchor, new draft_hash) through the
   normal staging path; its accept re-enters Task 4.1: inventories recomputed
   into `pending_keep`. Carry-forward rule: a stored `FieldKeepDecision` whose
   `kept_fields` ⊆ the sink's NEW inventory (and still inside the declared
   schema, if any) stands — that sink does not re-present; otherwise the stale
   decision is dropped and the sink re-presents. The exact-requesting sink
   always re-presents (no decision was ever recorded for it).
3. Loop bound: the existing proposal/turn budgets and decline/exhaustion
   affordances — no new budget machinery.

- [ ] Failing tests: exact response leaves `keep_decisions` unchanged and empties
      `pending_keep`; the revision request carries exactly the structured fact;
      no-chain-authoring extended; carry-forward keeps a still-valid decision and
      drops an invalidated one; integration: exact → revision → accept →
      re-present only where owed.
- [ ] Implement per the read revision entry; scoped pytest; commit
      (`feat(composer): exact field-keep routes through planner revision`).

### Task 7: Frontend `FieldKeepTurn`

**Files:**
- Create: `frontend/src/components/chat/guided/FieldKeepTurn.tsx` + `.test.tsx`
- Modify: `frontend/src/components/chat/guided/GuidedTurn.tsx`,
  `frontend/src/api/guidedDecoder.ts`,
  `frontend/src/components/chat/guided/guided.css`

**Conventions (inherited from `MultiSelectWithCustomTurn.tsx:1-91`, binding):**
props are `{ payload: FieldKeepPayload; onSubmit: (body: GuidedRespondAction) =>
void; disabled?: boolean; isTutorial?: boolean }` with SYNC `onSubmit`; every
`GuidedRespondAction` field explicit, unused ones `null`; `<fieldset>+<legend>` for
the chip group; chips are `<button>` with `aria-pressed` (shared
`.guided-chip-btn`/`.guided-chip-group` classes — do NOT redefine); DOM IDs
prefixed with `useId()`; visible labels are the accessible name; guided.css tokens
only, no hardcoded colours, reduced-motion rules for any transition; `Button
variant="bare"` / `Input bare` from `@/components/ui`. `guided.css` gains a
widget-owned block (note: `guidedSurface.test.ts:16` and several
`ChatPanel.test.tsx` assertions pin guided.css file content — run them).

**Registry edits (a new turn type touches all of these; the `GuidedTurn` `never`
exhaustiveness check turns a miss into a compile error):**
1. `guidedDecoder.ts:1007` `decodeTurnType` — add `case "field_keep":`.
2. `guidedDecoder.ts:38` `LEGAL_TURNS.step_3_transforms` — add `"field_keep"` to
   the set. (`STEP_INDEX` is unchanged — no new step.)
4. New `decodeFieldKeepPayload` mirroring `decodeMultiSelectPayload`
   (:1092-1101 — `exactRecord` pins the exact key set):

```ts
function decodeFieldKeepPayload(value: unknown, path: string): FieldKeepPayload {
  const payload = exactRecord(value, path, [
    "question", "output_stable_id", "output_name",
    "guaranteed_fields", "open_remainder", "declared_fields", "default_mode",
  ]);
  const defaultMode = stringValue(payload.default_mode, `${path}.default_mode`);
  if (defaultMode !== "plus_new") invalid(`${path}.default_mode`, "unknown field-keep default mode");
  return {
    question: stringValue(payload.question, `${path}.question`),
    output_stable_id: stringValue(payload.output_stable_id, `${path}.output_stable_id`),
    output_name: stringValue(payload.output_name, `${path}.output_name`),
    guaranteed_fields: stringArray(payload.guaranteed_fields, `${path}.guaranteed_fields`),
    open_remainder: booleanValue(payload.open_remainder, `${path}.open_remainder`),
    declared_fields: payload.declared_fields === null
      ? null
      : stringArray(payload.declared_fields, `${path}.declared_fields`),
    default_mode: "plus_new",
  };
}
```

   (If the module lacks a `booleanValue` helper, add one beside `stringValue`
   following its exact error idiom.)
5. `decodeTurn` switch (:1774) — add the `field_keep` arm returning the envelope.
6. `GuidedTurn.tsx` routing table comment + switch — new arm, identical prop shape
   to the multi-select arm (:109-118):

```tsx
    case "field_keep":
      return (
        <FieldKeepTurn
          key={turnInstanceKey}
          payload={turn.payload}
          onSubmit={guardedSubmit}
          disabled={disabled}
          isTutorial={isTutorial}
        />
      );
```

**Component behaviour (binding):**
- Mode control: a three-option radio group (`<fieldset>+<legend>`, standard
  `<input type="radio">` with visible labels) — "Keep these and anything new"
  (default, per `payload.default_mode`), "Keep exactly these", "Keep everything
  that arrives". Selecting keep-everything disables the field chips (they are
  irrelevant to that submission).
- Field chips: one per `guaranteed_fields` entry, ALL `aria-pressed=true`
  initially. When `declared_fields` is non-null, offer ONLY fields present in it
  (the sink's explicit schema constrains the choice — relocated 398f150859
  surface); when that intersection empties, plus-new/exact are unreachable and
  the radio collapses to keep-everything.
- `open_remainder === true` ⇒ render the line
  "+ anything else your source carries" adjacent to the chip group — MANDATORY
  (D5: epistemics are wire facts, not chrome); absent when false.
- Submit bodies (the widget constructs wire objects directly — there is no
  encoder layer; the closed `GuidedRespondAction` union is the contract):
  - plus-new ⇒ `{ chosen: [ordered kept fields], custom_inputs: null,
    edited_values: null, proposal_id: null, draft_hash: null, edit_target: null,
    control_signal: null }` — chosen in `guaranteed_fields` order (pin with a
    test, as the multi-select does for option order);
  - keep-everything ⇒ same nulls with `chosen: null, control_signal: "passthrough"`;
  - exact ⇒ `chosen: [ordered kept fields], control_signal: "keep_exact"`.
  - Continue is disabled when the effective mode needs fields and none are
    pressed (mirror the multi-select CONTINUE INVARIANT, including the
    disabled-click-does-not-fire test).

- [ ] **Step 1: Failing vitest** — `FieldKeepTurn.test.tsx` with per-file payload
      fixtures (house style: no shared turn fixtures; `test/guided-fixtures.ts` has
      only `nullResponse()` — spread it FIRST in `toEqual` literals or it clobbers
      explicitly-set fields). Describe blocks mirroring the multi-select map:
      initial render (legend, chips all pressed, remainder line both directions,
      declared-fields filtering); chip toggle; mode radio (default plus-new,
      chips disabled under keep-everything); submit per mode (three wire-shape
      assertions using `...nullResponse()` then overriding `control_signal`);
      continue-disabled; DOM ID isolation (two instances); tutorial passive mode.
- [ ] **Step 2: Verify failure** (`npx vitest run
      src/components/chat/guided/FieldKeepTurn.test.tsx` — component not found).
- [ ] **Step 3: Implement** component + registries + guided.css widget block.
- [ ] **Step 4: Verify** — vitest (new file + `guidedSurface.test.ts` +
      `ChatPanel.test.tsx` + `guided.test.ts`), `tsc` clean (the exhaustiveness
      check), parity harness green.
- [ ] **Step 5: Commit** (`feat(composer-ui): field-keep turn component`).

### Task 8: Planner briefs + skills convergence

**Files:** `src/elspeth/web/composer/skills/pipeline_composer.md`,
`src/elspeth/web/composer/skills/pipeline_capabilities.md`.

- [ ] State affirmatively in both: the sink field contract is reviewed by the user at
      the end against the validated pipeline's real field inventory; plan transforms
      from the user's intent; never interrogate for sink field lists (brief
      sufficiency — say what the model already has, per the redundant-turns rule).
- [ ] `git grep -n "custom field\|required_fields" src/elspeth/web/composer/skills/`
      — update every stale step-2-era mention.
- [ ] `sudo -n systemctl restart elspeth-web`; probe `/api/system/status` 200 with a
      retry loop (socket recreation lags ~3s).
- [ ] Commit (`docs(composer-skills): field contract is end-reviewed`).

### Task 9: Canary re-record, ADR, hints entry, full gates

**Files:** `frontend/tests/e2e/composer-guided-live.staging.spec.ts`,
`composer-guided-ab-live.staging.spec.ts`, `tutorial-probe.staging.spec.ts`,
`composer-capability-parity.staging.spec.ts`; new
`docs/architecture/adr/03X-sink-field-keep-at-validation.md`;
`docs/agents/recent-code-hints.md`.

- [ ] Re-record the tutorial fixed script for the new flow (ADR-031: script update,
      never a code branch). The base keep path adds ZERO provider calls — the
      ≤2-provider-call pin does NOT relax; a needed increase is a defect signal.
      Update exact phase-string pins.
- [ ] ADR: information-timeline rationale; fixed-schema firewall constraint
      (guarantees.py:421-427 — fixed kills rows, never projects, hence the planner
      owns exact-keep); invariant-1 routing; supersession of the step-2 review.
- [ ] recent-code-hints entry (same commit as the last code change): the keep stage
      is the ONLY sink field-contract authority; any future pipeline builder must
      pass `guided_reviewed_sink_options` WITH the session's keep decisions, and
      the COMPLETED-requires-keep-coverage invariant lives in
      `GuidedSession.__post_init__` — never route-local (extends the R2-F4 note).
- [ ] Full `pytest tests/ -n 12` (HEAD stamped before AND after — shared checkout);
      trust-tier corpus compare; wardline fingerprint compare;
      `npm run test:e2e:staging` (operator-gated if it needs the live edge).
- [ ] Commit (`docs: ADR + hints for the field-keep inversion`).

### Post-merge measurement (operator-fired)

Battery A/B on the unix socket (`--base unix:///run/elspeth/uvicorn.sock`, never the
hostname; never overlap rounds): provider-call and discovery-turn counts on the 1×1
and transform scenarios. Expected: no base-path regression; exact-mode = exactly one
extra revision round. Count tool calls, not seconds.
