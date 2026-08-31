# Recent code hints — dated appendix

This file is the dated changelog behind the rules in
[CONTRIBUTING.md — Whole-tree gates and conventions you will hit](../../CONTRIBUTING.md#whole-tree-gates-and-conventions-you-will-hit):
one item per incident or ruling, newest first within each original section, each keeping the commit hashes, ticket ids,
gate names and re-pin commands a future reader may need, and linking to the CONTRIBUTING heading whose rule it
instantiates. It exists because scoped-green commits kept breaking whole-tree gates for every sibling (7201beeb7 →
elspeth-62a5aa4da8). When you land a new gate or convention, add the rule to CONTRIBUTING.md and the dated item here in
the same commit; the rules live there, the history lives here.

- **2026-09-01 — secret wiring is deny-by-default at three seams, and collectors use the transform policy vocabulary** (elspeth-f3c1aafd25; 9da1b39b8, c163b6366)
  `WebSettings.secret_wiring_allowlist` authorizes only an exact
  `(secret, component_type, plugin, option_key)` match; an empty policy denies every wiring. The policy vocabulary is
  `source|transform|sink`: aggregation and collector nodes both match `transform`, consistently with secret-reference
  placement and authored-state admission. Enforce this contract (1) in `wire_secret_ref`, before writing a marker; (2)
  in `validate_secret_evidence`, over `policy.authored_state`, so authenticated executable web paths—with their required
  secret-service and user context—admit markers from imports and every other entry path; and (3) in `/execute`, where
  `secret_guard` returns a 428 challenge bound to the exact authored composition state before the fanout guard or run
  creation. The validator explicitly reports a skipped check when that context is unavailable; do not describe that mode
  as admission. Only the authenticated execute request may return the challenge token: an LLM or composer-tool argument
  is never approval. Keep the admission and execute walks on authored state because server-side operator-profile lowering
  may inject credentials after admission by design.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-30 — `.agents/skills/` is the ONE canonical skills tree; every `.claude/skills/<name>` is a committed relative symlink (`git ls-files -s` mode 120000) into it, and the design pack lives at top-level `design/`** (elspeth-1e9d011295)
  Add or edit a skill under `.agents/skills/` only; a real directory under `.claude/skills/` is a regression. Pin paths in
  tests, scripts and `per-file-ignores` at `.agents/skills/...` and `design/...`. Git never sees a path *through* a
  symlink (`git check-ignore` says "beyond a symbolic link"), so the installer's write to
  `.claude/skills/loomweave-workflow/SKILL.md` lands at `.agents/skills/loomweave-workflow/SKILL.md`, which `.gitignore`
  already covers. `Path.glob("**")` on Python 3.13 and the shared `iter_python_files` walker (`followlinks=False`) both
  refuse to descend into the symlinks, so nothing is double-counted; glob `.agents/skills` only. The 2026-08-29 item
  below now reads `.agents/skills/**/*.py`.
  → [Gate: walker and scratch directory](../../CONTRIBUTING.md#gate-walker-and-scratch-directory)
- **2026-08-29 — `.agents/skills/**/*.py` IS scanned by every whole-tree test gate**
  The masquerade, `hasattr`, mock-discipline and walker-authority gates all scan
  `.claude/skills/**/*.py`; only `.claude/worktrees/` is excluded. A skill helper
  script is production code to those gates — no `getattr`/`hasattr`, no unspecced
  mocks, exact-type checks on parsed JSON. It is NOT under the
  `elspeth-lints --root src/elspeth` tier gate. Ruff `T20` (print) is ignored there
  by `per-file-ignores`, the same treatment as `scripts/`. First occupant:
  `.claude/skills/lane-manager/` (hub-side lane orchestration; state under
  `.claude/lanes/`, tests in `tests/unit/test_lane_manager_skill.py`).
  See [CONTRIBUTING: Gate walker and scratch directory](../../CONTRIBUTING.md#gate-walker-and-scratch-directory).

- **2026-08-29 — five tier_model precision classes are FIXED; do not reshape code around them or expect the pre-fix finding sets** (elspeth-8d46db34ff, ae34b48b3, df3463583)
  1. D1 — a name bound in a `try` body survives the post-try derived-name join when
     EVERY handler ends in an unconditional `raise`/`return`/`break`/`continue`:
     `try: data = json.loads(resp.content) except ... as e: raise X from e` keeps
     `data` rooted at `source_param`; a single falling-through handler still drops it.
  2. D2 — the frozen-dataclass `__post_init__` R5 exemption follows
     `for part in self.<field>` loop variables and locals bound to a MODULE-PRIVATE
     call over a self field (`frozen = _freeze(self.row, ...)`); a public callee
     (`freeze(self.row)`) is NOT trusted.
  3. D3 — R4 uses the same "explicit outcome" predicate as R6 (`_handler_is_silent`):
     a non-default `return`/`yield`, a `raise`, a routed `TransformResult.error`, or a
     recorded error entry clears a broad handler; `return None`/`return []` still fire.
  4. D4 — recording a CONSTRUCTED record into a validator accumulator is explicit:
     receiver name in `errors/warnings/diagnostics/entries/failures/issues/problems`
     (bare or `self._errors`), value a non-builtin call or a handler-local bound to
     one, or any `append/add` of a call carrying `error_code=`; `errors.append(str(exc))`
     and `seen.append(_normalise(exc))` still fire.
  5. D5 — a BOUND value derives by the assignment rule everywhere:
     `for e in _require_sequence(payload)`, `enumerate(f(payload))`, `f(payload).items()`,
     comprehension iterables and `with f(payload) as h` bind derived targets exactly as
     `e = f(payload)` always did (`_value_depends_on_boundary`); the strict
     `subject_is_rooted` rule is for FINDING SUBJECTS only — `normalise(payload).get()`
     still roots at `normalise`.
  6. D6 — the `if` join skips a branch that cannot fall through, like the `try` join:
     `else: return []` no longer erases names bound on the surviving branches.
  NOT changed, deliberately: a nested `def`/`lambda` inside a boundary does NOT inherit
  the enclosing derived names (`TestBoundaryDoesNotInheritIntoNestedScopes`, since
  df3463583) — a closure can escape the boundary and defer the Tier-3 read past its
  invariant, and the lint has no escape analysis; that stays a policy call for the
  operator. Measured allowlist-disabled at ae34b48b3: 2573 → 2460, 113 removed, 0 added.
  A re-stage finds signed entries for the removed sites gone (`stale_delete`), and
  per-file caps in `config/cicd/enforce_tier_model/*.yaml` ratchet down (chat_solver 31→13).
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — the tier_model `_R5_NAMED_BOUNDARY_CONTEXTS` exemption map is MEASURED: every entry must resolve to exactly ONE live definition, and a moved function is NOT successor-included** (elspeth-0bd4fb6042, 99d43f87d)
  Before elspeth-0bd4fb6042 the map carried 12 dead entries (2 dead file keys including
  `web/sessions/routes.py`, 7 dead function keys) and 2 bare-name collisions
  (`state.py::from_dict` matched SIX classmethods, `redaction.py::provider` two closures)
  that nothing reported. Now: (a) keys are qualified `symbol_stack` paths, so a same-named
  method on another class or a future function reusing a retired name inherits nothing;
  (b) `resolve_named_boundary_contexts(root)` is the single authority —
  `tests/unit/elspeth_lints/test_tier_model_named_boundary_map.py` pins the live tree and
  prints failing rows as a table, and every whole-tree `collect_check_result` on a
  `src/elspeth` root re-runs it and reports a stale entry as an ERROR
  (`STALE NAMED-BOUNDARY MAP ENTRIES` / JSON `stale_named_boundary_contexts` / lint message
  "Stale tier-model named-boundary map entry"), the same fail-closed treatment a stale
  allowlist entry gets; pre-commit `files=` mode skips it like allowlist staleness.
  Consequences: adding an entry needs audit evidence AND the pin passing; deleting a
  function named in the map turns the whole-tree gate red until the entry goes; when a
  function MOVES, do NOT add the new location — its findings surface and go through the
  allowlist like any other site. Deleting a DEAD entry moves the corpus by ZERO. The
  "+3 for `_extract_runtime_model_snapshot`" expected by lens B had already surfaced at the
  package split (`routes/_helpers.py:964/970/971` are in the 2594 baseline at 99d43f87d —
  2594, not 2535: the earlier count dropped digit-bearing filenames).
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — masquerade resolver STOP RULES (do not re-walk the six-freeze road)** (elspeth-de6f571887, elspeth-02cd60d8cd, elspeth-34ac84b4b6, elspeth-df09888129)
  The sparse-SSA `flow.py` prototype (archived at
  `~/codex-cleanup-archive-2026-08-16/orphaned-work/ac-masquerade-bounded/`) was REJECTED by
  two independent reviews on 2026-08-09 (comments on closed issue elspeth-de6f571887):
  late-global / try-match / annotation-timing / loop-header fail-open misses and
  non-monotone output. Standing rules from elspeth-02cd60d8cd: NO CFG/SSA/history/replay/
  lazy-cache/object-emulator growth in the masquerade engine; ≤2.5× runtime per input
  doubling. The shipped 1,278-line inventory visitor is the single authority. METHOD RULE:
  corpus agreement with the incumbent (0 diffs over 2,866 files) does NOT validate a
  fail-closed analyzer — the defect shapes were simply absent from the tree; adversarial
  cases are the oracle. Open follow-ups: elspeth-34ac84b4b6 (6 verified evasions) and
  elspeth-df09888129 (perf).
  See [CONTRIBUTING: Gate: masquerade sites](../../CONTRIBUTING.md#gate-masquerade-sites-tests-included).

- **2026-08-29 — a hand-built fixture can pin a shape NO PRODUCER EMITS, and then every test agrees with a projection that never fires** (elspeth-f5e6723133)
  Found in B59. `_tool_call_outcomes_by_call_id` (`web/sessions/routes/_helpers.py`) read
  `envelope["version_after"]` / `["status"]` off the TOP level of a `role="tool"` row's
  `tool_calls[0]`, but the fallback writer stores exactly
  `redacted_tool_invocation_content_and_envelope(...)[1]`, which is
  `{"_kind": "audit", "invocation": {...}}` — the per-call delta is one level DOWN. On every
  real row the fallback branch matched nothing and an applied mutation fell through to
  COMPLETED, rendering in the transcript as a lookup: the dishonesty elspeth-f5e6723133
  created the projection to remove. Four unit tests, a route-level test and an eval parity
  test were all green, because each built its envelope by hand in the flat shape.
  1. Mint the fixture through the producer. The pin that catches this is
     `TestProducerBuiltFallbackEnvelope`, whose rows come from
     `redacted_tool_invocation_content_and_envelope` and never restate the layout.
     Generalises the `NodeSpec(...)`-not-a-literal rule from frozen containers to any
     persisted wire shape.
  2. A cross-check between two reimplementations proves neither.
     `evals/lib/battery_capture.py::tool_outcomes` re-implements this projection for offline
     scoring and carried the IDENTICAL bug, so
     `test_tool_outcomes_agree_with_the_server_projection` passed by agreeing on the wrong
     answer. Its sibling `evals/lib/decode_tools.py::_first_audit_invocation` unwraps
     `["invocation"]` correctly — when two readers of one shape disagree, one is the bug.
  3. The unwrap needs `isinstance(x, Mapping)`, not the house exact-type idiom.
     `ChatMessageRecord.__post_init__` runs `freeze_fields(self, "tool_calls")`, so a row read
     back from the DB is `mappingproxy` at BOTH the envelope and the `invocation` level;
     `type(x) is dict` is permanently False and would re-disable the branch.
     Mutation-verified both ways.
  See [CONTRIBUTING: Convention: test discipline (fakes, mocks, fixtures)](../../CONTRIBUTING.md#convention-test-discipline-fakes-mocks-fixtures).

- **2026-08-29 — three honest shapes for the secure-file and provider-error findings in the acceptance harness**
  Measured in B28 on `web/_aws_ecs_acceptance/{secure_documents,receipt_store,textract}.py`,
  21 → 2 with no sidecar restatement.
  1. A non-raising `@trust_boundary` on a provider-error PROJECTION whose `None` the caller
     fails closed on. `textract.py::_client_error_code` reads `error.response.get("Error")`
     off a botocore `ClientError`; the tier_model walk keeps the `<source_param>.<attr>.get()`
     chain, so `suppresses=("R1","R5")` under `source_param="error"` cleared all three sites.
     `non_raising=True` is honest ONLY because `_probe_invocable` treats `None` as
     not-invocable and raises — an accept-gate consumer would make the same declaration a
     silent skip (the W3-1 polarity rule).
  2. `FileNotFoundError` swallows in a secure writer have stdlib forms that say what they
     mean. The create pre-check `try: path.lstat() / except FileNotFoundError: pass /
     else: raise exists` is `if os.path.lexists(path): raise exists` (outcome-identical: any
     other lstat errno fails `mkstemp`/`os.link` one step later under the same `write_check`);
     the `finally` temp-file cleanup is `Path(tmp).unlink(missing_ok=True)`; and an
     open-or-create lock is a handler that RETURNS the freshly published descriptor
     (`_open_receipt_manifest_lock` → `_publish_receipt_manifest_lock`). What stays is the
     EINTR retry in `_flock_retry_interrupted` — unbounded by design, rationalised, not shaped.
  3. Parse, THEN bind. `receipt_store` compared nine `document.get(k)` values against the
     manifest BEFORE `_validate_stored_receipt` had enforced `set(payload) == fields`. Running
     the schema parse first (the order every other receipt kind already used) makes every
     downstream read membership-form on a validated document; the only observable change is
     that a document failing both now reports `receipt_store_schema`, and no document that was
     rejected is accepted. `json.loads` output on a path with no frozen owner between it and
     the read is `type(x) is not dict`.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — widening a `@trust_boundary`'s `suppresses=` ROTATES its `test_fingerprint`, and `trust_boundary.tests` is a CI-ONLY gate that no pre-commit hook runs**
  Measured in B60 on `web/sessions/routes/composer/state.py::_reject_disallowed_source_paths`.
  1. The fingerprint is over the referenced TEST's AST, not the decorated function.
     `elspeth_lints.rules.trust_boundary.tests.rule::_fingerprint_test_function` is
     `sha256(ast.dump(func, annotate_fields=True, include_attributes=False))` of the function
     `test_ref` resolves to. Editing the *test* to pin a newly-suppressed arm rotates it, while
     comments, whitespace and line moves inside that test are free. Recompute it by calling the
     rule's own `_resolve_test_ref(test_ref, repo_root).fingerprint` — never hand-edit, never
     guess from the old value.
  2. Nothing local catches a stale one. `.pre-commit-config.yaml` has no
     `trust_boundary.tests` hook (it has `trust_tier.tier_model`), and the runtime decorator in
     `contracts/trust_boundary.py` stores `test_fingerprint` without ever checking it. The only
     enforcement is `.github/workflows/ci.yaml`'s
     `--rules trust_boundary.tests,trust_boundary.scope,trust_boundary.tier`. Run that command
     after ANY decorator or pinning-test edit: a green `pytest` and a green commit prove nothing
     about it.
  Corollary for burn-down lanes: widening `suppresses=` on an existing honest boundary is the
  cheap zero-code removal (B47/B42), but it is only honest if the boundary's own pinning test
  asserts the widened arm. Here the `test_ref` pinned only the 400-reject arm, which stays green
  if the `isinstance(value, str)` skip gate is deleted — so the R5 widening required adding the
  skip assertions (int, `None`, nested mapping, absent key) to the named test, which rotated the
  fingerprint. Same both-arms rule as the frozen-input pins in W2 brief item 15, applied to
  boundary metadata.
  See [CONTRIBUTING: Why a green scoped run proves nothing](../../CONTRIBUTING.md#why-a-green-scoped-run-proves-nothing).

- **2026-08-29 — tier_model R5 carries a HARD-CODED per-file exemption map in the rule (`_R5_NAMED_BOUNDARY_CONTEXTS`), so "the identical `isinstance` chain fires in my file but not in that one" is not evidence of a reproducible structural exemption** (elspeth-0bd4fb6042)
  Measured in B61 on `web/sessions/_auto_title.py`, whose `_auto_title_exception_class` runs four
  `isinstance(exc, …)` arms reporting ZERO findings while the structurally identical
  `_guided_full_failure_code` in `routes/composer/guided_plan.py` reports seven. A minimal probe
  file reproduced the chain and DID fire, ruling out any general "exception dispatch is exempt"
  behaviour. The cause is `TierModelVisitor._is_allowed_r5_context` →
  `_is_named_tier3_boundary_context`, which looks the current definition's QUALIFIED path
  (`".".join(symbol_stack)`, e.g. `CompositionState.from_dict`; bare names only for module-level
  functions — qualified since elspeth-0bd4fb6042) up in a `ClassVar[dict[str, frozenset[str]]]`
  keyed by scan-root-relative file path (`elspeth-lints/.../trust_tier/tier_model/rule.py`,
  ~line 418); the map already lists
  `web/sessions/_auto_title.py: {"_auto_title_exception_class", "maybe_auto_title_session"}`
  alongside `web/app.py`, `web/auth/oidc.py`, `web/composer/audit.py`,
  `web/composer/llm_response_parsing.py` and a dozen others. Two consequences for a burn-down
  lane: (a) do not hunt for the code shape that "clears" such a site — diff the file against that
  map first; (b) the map is rule-owned and operator-maintained, so adding your own function to it
  is gaming the gate, not fixing it. The other three R5 contexts
  (`_is_tier1_frozen_dataclass_post_init_guard`, `_is_pydantic_before_validator`,
  `_is_fastapi_route_handler` — the last requires a `web/` path AND a FastAPI method decorator)
  ARE structural and reproducible.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — `isinstance(x, PayloadStore)` was the last cluster of `runtime_checkable` Protocol admission gates in `src/elspeth`; all four are gone**
  `git grep 'isinstance(.*PayloadStore)' -- src` is now empty. They sat in
  `web/sessions/guided_payloads.py` (×2) and `web/sessions/guided_replay.py` (×1) plus a
  `payload_store is None or not isinstance(...)` pair, each raising
  `TypeError`/`AuditIntegrityError` as if it were a control. Per ADR-032 it was not one:
  `PayloadStore` declares four methods, so any object with those names passes and a
  dynamic-attribute implementation is rejected. Deleting them removes theatre, not defence — the
  real custody control in every case is the content-address round trip that follows
  (`store` → `retrieve` → `hmac.compare_digest`, or in `load_guided_json_payload` the
  re-derivation of the SHA-256 from the retrieved bytes). Keep the `payload_store is None` half
  where the parameter is `PayloadStore | None`: that one is a real fail-closed gate. If you add a
  new store seam, do not reintroduce the Protocol `isinstance` — annotate nominally and prove the
  bytes.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — the `deep_freeze` trap covers `PreparedGuidedAuditRow.envelope` and its NESTED values, which is what `validate_guided_audit_payload_references` reads**
  `__post_init__` runs `freeze_fields(self, "envelope")`, so `row.envelope["invocation"]` is a
  `mappingproxy` on EVERY row, including freshly built ones — measured by constructing a real
  `PreparedGuidedAuditRow`, not inferred from the builder, which hands `__post_init__` a plain
  dict. `type(invocation) is dict` is therefore permanently False; the correct form names
  `deep_freeze`'s output pair, `type(invocation) not in (dict, MappingProxyType)`, exactly as the
  dataclass's own envelope guard does. Polarity saved this one: the site is a reject-gate
  (`not in …` → raise), so the wrong form would have failed loudly rather than silently skipping
  every audit row — but the same value read behind an accept-gate elsewhere would go quiet.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — pydantic passes a NESTED mapping through by IDENTITY even under `strict=True`, so a `dict[str, Any]` request field is NOT proof that its inner values are exact dicts**
  Measured in B57 on `GuidedRespondRequest`:
  `model_validate({..., "edited_values": {"plugin": "csv", "options": MappingProxyType({...})}},
  strict=True).edited_values["options"]` is a `mappingproxy`, while the OUTER `edited_values` is
  coerced to an exact `dict` (a `MappingProxyType` supplied there is rejected outright). This is
  the mirror image of the B45 entry: pydantic RECONSTRUCTS a field it has a model for and passes
  an `Any` through untouched. It matters because "the field is annotated `dict`" is the usual
  argument for converting an inner `isinstance(x, Mapping)` to `type(x) is dict`, and it is not a
  valid one for anything below the annotated level. Over HTTP the JSON decoder only ever produces
  exact dicts, so such a narrowing is typically LATENT rather than live; say so honestly in a
  rationale rather than claiming a live break, and check whether any in-process caller (the guided
  chat lane builds `GuidedRespondRequest`s directly) can reach the site.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — a guided TURN payload is `deep_thaw`-ed at BOTH of its producers, so the "frozen composition state" argument does NOT transfer to it**
  B57 nearly shipped four rationales asserting that
  `turn["payload"]["knobs"] / ["prefilled"] / ["options"]` arrive as `MappingProxyType` on
  durable load. Measured, they do not: `_load_durable_current_turn` builds
  `payload=dict(deep_thaw(prepared.payload))` and `_finalize_guided_turn` builds
  `payload=dict(deep_thaw(turn["payload"]))`, so every guided turn payload reaching the route
  layer is exact `dict`/`list` all the way down. `freeze_guided_json_mapping` really does produce
  `mappingproxy` + `FrozenJsonArray` (both verified), but the thaw stands between it and every
  route read — the "find the thaw between the frozen owner and the read" test, applied in the
  direction that DEFEATS the frozen-container claim. The Mapping ABC checks at those sites are
  still right, on ADR-032 Tier-3 read-back grounds and to agree with the
  `@trust_boundary`-declared `_validate_schema_form_payload` that certifies the same bytes — but a
  rationale must argue the trust domain, not a container identity that is measurably false.
  General rule: run the two-line `type()` probe before writing "arrives deep-frozen" into a
  rationale; a plausible frozen-state story is the most likely way for a false claim to enter the
  judge corpus.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — dispatching on a pydantic discriminated union's own tag clears the R5 findings AND narrows better than the exact-class form**
  In `routes/composer/guided.py::_schema8_transition`, six `isinstance(action, <ConcreteAction>)`
  tests over `GuidedComponentAction` (declared `Annotated[..., Field(discriminator="action")]`)
  became `action.action == "add" | "edit" | ...`. This is not a lint dodge: pydantic guarantees the
  tag matches the class it built, and mypy narrows a `Literal` tag on BOTH branches where
  `type(x) is C` narrows only the positive one — probed, the exact-class form raises five
  `union-attr` errors on the `kind` ternary that reads `action.target` / `action.component_kind`.
  Reach for the tag whenever the union carries a discriminator; keep the closed union's fail-closed
  `else: raise` arm, which still fires for a tag outside the set.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — POLARITY decides how bad a `type(x) is dict` freeze trap is, and an accept-gate that GATES A CONTROL BLOCK is fail-OPEN, not a conservative abstention**
  Landed by the Wave-3 hygiene lane over the four sites the frozen-narrowing audit flagged. The
  rule: read what the SILENT branch of the gate causes, not just whether the gate is wrong.
  `_discover_blob_rows_sources` (`web/execution/service.py`) returning `[]` does not skip one field
  — it skips the whole `blob_rows` admission block: session-ownership (IDOR), `ready` status,
  payload-hash and metadata-divergence checks. `FrozenRunSettings.__post_init__` freezes
  `executable_config`, so the ONLY thing that made the exact-`dict` test work was a `deep_thaw`
  several hundred lines away in `_run_pipeline`; moving or removing that thaw would have disabled
  admission with every test still green. Same shape in `web/catalog/knob_schema.py`, where an
  unread `json_schema_extra` answers "not hidden" and offers an audit-anchor field as a
  user-editable knob. Fixes, in order of preference: name `(dict, MappingProxyType)` so the gate
  reads the frozen form (house spelling is `type(x) in (dict, MappingProxyType)` — `isinstance`
  would add an R5 the boundary must then suppress); or make the gate reject. What NOT to do is pin
  "frozen input -> `[]`" — a criterion written from the defect expires with the fix. Pin the
  commuting property instead: frozen and thawed forms of the SAME value produce the SAME answer,
  built through the real freezing owner (`FrozenRunSettings`, `PipelineProposal`), plus an
  untouched arm. Measured corollaries: a frozen mapping reaching
  `_canonical_state_from_private_pipeline` raises AttributeError (`mappingproxy` has no `pop`)
  BEFORE it can raise `TypeError` on item assignment — that adapter mutates `raw` in place, so its
  contract is a thawed mapping and a reject-gate in `AuditIntegrityError` currency states it; and a
  `dict(mappingproxy)` shallow thaw leaves a REAL outer dict with FROZEN children, which sails
  through any outer-only exact-`dict` reject-gate (`ComposerToolInvocation` in
  `pipeline_commit.py` is one), so the nested test is usually the load-bearing one.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — the tier-model boundary walk loses `source_param` derivation through a BRANCHED assignment, not through `tuple(...)`**
  Measured on B54 with the rule's own `scan_file`. A name assigned in an `if`/`elif` (even when
  every branch's RHS mentions the boundary parameter) is NOT derived afterwards, so every
  `.get()`/`isinstance` on it, on its loop targets, and on anything further downstream re-appears as
  R1/R5 despite the decorator. The SAME expression written as one assignment keeps the trail —
  including through `tuple(x.values())`, later `tuple(view.values())` hops, plain `for` targets and
  generator-expression targets. Trail LOST:
  `if isinstance(queries, Mapping): definitions = tuple(queries.values())` /
  `elif isinstance(queries, Sequence) and not isinstance(queries, (str, bytes)): definitions = tuple(queries)` /
  `else: return _unprovable()`. Trail KEPT — guard first, then ONE assignment:
  `if not isinstance(queries, (Mapping, Sequence)) or isinstance(queries, (str, bytes)): return _unprovable()`
  followed by
  `definitions = tuple(queries.values()) if isinstance(queries, Mapping) else tuple(queries)`.
  So when a boundary-decorated function still reports findings, hoist the refusal into a guard and
  collapse the binding to one expression before reaching for a rationale. Confirm from the
  `R_TB_SUPPRESSED` lines in the after-corpus, never by reading the decorator. Two other
  trail-losers re-confirmed here: a value bound from a helper call (`_stable_source_items(state)`,
  `_upstream_producers(node, graph)`) is never rooted at any `source_param`, so a Tier-3 parse that
  must be suppressed has to live in a function that takes the untrusted mapping AS its
  `source_param` (B54 extracted `coverage._declared_scan_scopes(options)` for exactly that).
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — the `deep_freeze` trap is NOT limited to composition state: `ToolResult.data` is frozen too, and tracing a value's PRODUCERS is not a valid check**
  B43b shipped a real regression this way, caught only by B51's trap note. `ToolResult` is
  `@dataclass(frozen=True, slots=True)` and its `__post_init__` runs `freeze_fields(self, "data")`
  whenever `data is not None`, so `finalized_candidate_result.data` in `run_tool_batch` is a
  `mappingproxy` — for which BOTH `type(x) is dict` and `isinstance(x, dict)` are False; only
  `isinstance(x, Mapping)` is True. Converting that guard to the exact form silently sent every
  prevalidation rejection down the `else` arm, wrapping the candidate's keys under
  `"candidate_data"` instead of spreading them, and changing the PREVALIDATION_REJECTED payload the
  composer model receives.
  1. Producer-tracing is the wrong check. An agent traced every producer of `.data` to plain dict
     literals and `dict | None` annotations and concluded the swap was safe. It was not: the
     *container* freezes the field after construction, so what the producer built is irrelevant.
     Check the owning dataclass's `__post_init__` for `freeze_fields`, not the call sites.
  2. 8838 tests passed over it. The composer unit and integration suites asserted only the keys
     that `feedback_data.update({...})` adds (`status`, `applied`), which survive either arm. A
     status-only assertion cannot see this class of break — the pin has to assert a key that came
     from the frozen value itself, plus `"candidate_data" not in payload`
     (`tests/integration/web/composer/test_freeform_proposal_prevalidation.py::
     test_semantic_rejection_reaches_next_model_turn_then_repair_creates_one_proposal`), and be
     mutation-tested by flipping the guard back and confirming it FAILS.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — the `deep_freeze` → `mappingproxy` trap also covers `NodeSpec.branches`, and a frozen-input pin only detects a dead guard in the MAPPED form**
  `NodeSpec.__post_init__` calls `freeze_fields(self, "branches")` separately from `options`, so
  `node.branches` is a `mappingproxy` too (`isinstance(b, Mapping)` True, `type(b) is dict` False —
  measured through real `NodeSpec` construction by lane B50). A regression test for such a guard
  must (a) construct the value through the real producer (`NodeSpec(...)` / `SourceSpec(...)`, never
  a hand-built dict — a hand-built dict gave a false all-clear) and (b) for branches use the mapped
  form `{name: connection}`: the list/identity form is normalised by
  `_row_union_normalized_branches` into an identity mapping, so a `type() is dict` swap returns
  names where connections are wanted only in the mapped form and is undetectable in the list form.
  See [CONTRIBUTING: Convention: test discipline (fakes, mocks, fixtures)](../../CONTRIBUTING.md#convention-test-discipline-fakes-mocks-fixtures).

- **2026-08-29 — a tier-model `fp=` and `scope_fingerprint` are per-ENCLOSING-SCOPE, so editing a sibling method's docstring or a module-level string literal does NOT stale a signed entry in another method**
  Measured in B55 (`web/secrets/service.py`): expanding `WebSecretService.resolve_scoped`'s
  docstring and rewording the `detail=` strings inside the module-level `_log_*_rate_limited`
  helpers left all five signed `resolve` / `check_user_ref_resolvable` fingerprints and scope
  fingerprints byte-identical (`scan_file` before/after), while `file_fingerprint` moved.
  Corollary: do not hold back an honest docstring or log-text edit on "signature churn" grounds
  unless it is INSIDE the signed handler's own function — and never shape the edit around it either
  way. Verify with the rule's `scan_file` (fields `fingerprint`, `scope_fingerprint`) rather than
  guessing. Same lane: `resolve_scoped` shares `resolve`'s None-on-miss contract for the
  `resolve_secret_refs` aggregator, so its three R6 handlers are rationalised (sidecar
  `B55.rationales.json`) and pinned per exception class by
  `tests/unit/web/secrets/test_service.py::TestResolveScoped`, which previously did not exist — a
  new `WebSecretResolver` method is a parity item for that class.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — a signed tier-model entry binds by `scope_fingerprint`, NOT by its `fp=` key or its `ast_path`; a mismatch in either of those does not make it stale** (0b980f4d9)
  Measured in B45 on `config/cicd/enforce_tier_model/web.yaml`. The entry for
  `transforms.py:R1:_clear_removed_sink_edge_route` carries `fp=7b08c4d302f899b3` and
  `ast_path: body[39]/body[8]/body[2]/test/left`, while the live tree computes
  `fp=9f4682f37e40559b` and `body[43]/body[8]/body[2]/test/left` — BOTH stale — yet the finding is
  suppressed under the real allowlist and is never reported as a stale entry, because the entry's
  `scope_fingerprint` (`9a16b614…`) still matches the enclosing function exactly. The contrast is in
  the same two files: the two entries that ARE reported stale (`R5:_execute_upsert_node`,
  `R5:_execute_set_source_from_blob`) differ in `scope_fingerprint` too, because their enclosing
  functions were edited. `fp=`/`ast_path` drift is the NORMAL state after siblings add module-level
  statements, so judging coverage by comparing them will report a bound, judged site as uncovered —
  and re-rationalising it puts a second authority on unchanged code that already carries a binding
  ruling. Determine coverage by RUNNING the real allowlist and diffing against the
  allowlist-disabled corpus; to know *why* a site is or is not covered, compare `scope_fingerprint`
  between the YAML entry and `Finding.scope_fingerprint`, not the key suffix. Reverse direction: a
  per-file `pattern:`/`max_hits:` ratchet suppresses without any judge ruling at all, so before
  calling a suppressed site "signed", confirm there is no `source_file:` pattern block for it (at
  0b980f4d9 there is none for web/composer/tools/*; the only overages are in `plugins.yaml`).
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — an `isinstance(x, Mapping)` → `type(x) is dict` swap can pass mypy AND every runtime test and still be wrong; the discriminator is `reveal_type` on the NEGATIVE arm**
  Measured in B45 on the two coalesce `branches` sites in `web/composer/tools/transforms.py`
  (`dict(validated.branches) if isinstance(validated.branches, Mapping) else tuple(validated.branches)`).
  Both cheap checks give a false green: mypy compiles the converted form clean because `tuple()`
  accepts *any* iterable, and no end-to-end test can tell the forms apart because pydantic
  RECONSTRUCTS the field — `_UpsertNodeArgumentsModel.model_validate` turns a `MappingProxyType`, a
  `dict` subclass and a plain `dict` all into exactly `dict`, and a tuple into exactly `list`
  (measured). Only a narrowing probe caught it: for `b: list[str] | dict[str, str]`,
  `isinstance(b, Mapping)` reveals the negative arm as `list[str]`, while `type(b) is dict` reveals
  it as the un-narrowed `list[str] | dict[str, str]`. The exact-type form therefore FORFEITS the
  static proof that `tuple(...)` receives a list, and its else-branch on a mapping yields a tuple of
  the KEYS — a named coalesce branch map silently persisted as a positional branch tuple.
  Generalises B41's dataclass-union finding to builtin containers, and is the operational companion
  to B51's `deep_freeze`/mappingproxy entry: composition state is frozen (so `type() is dict` is
  always False there), while pydantic-validated TOOL ARGUMENTS are exact (so `type() is dict` is
  always True there) — both make the swap untestable at runtime for opposite reasons. Method to
  reuse before any `isinstance` → `type() is` conversion: write a four-line `reveal_type` probe for
  both forms and read the negative arm. A green mypy run on the real file is NOT that check.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — B51's `deep_freeze` trap re-checked across B40's four `type()` guards: all four are safe, each for a DIFFERENT reason, and one of them is safe only because it was NOT converted**
  The trap is real — `NodeSpec.__post_init__` freezes, so `node.branches` and `node.options` are
  `mappingproxy` and a frozen `options["interpretation_requirements"]` list is a `tuple`. What the
  re-check found is that "is this value deep-frozen?" is the wrong question on its own; ask what
  stands between the frozen bag and the guard.
  1. `_duplicate_consumer_repair_suggestions`'s `type(patched_branches) is not dict` reads
     `_serialize_node` output, and `_serialize_branches` thaws (`dict(deep_thaw(branches))`) — so
     the value is an exact `dict`. This site is ALSO structurally immune: the first alias iteration
     writes a plain dict back into `patched_consumer["branches"]`, so even a forced `mappingproxy`
     accumulates correctly (verified by mutation — the behavioural test PASSES under the mutation,
     which is why the pin is on the serializer's output type, not on the repair behaviour). A
     behavioural test that survives the mutation is not a pin for that guard.
  2. `type(stored_rows) not in (list, tuple)` is safe only because it names the PAIR.
     `stored_options` is `NodeSpec.options` at every call site, so the stored list is always a
     `tuple`; narrowing to `is not list` makes `_normalize_echoed_interpretation_requirements`
     abstain on every real call and silently switches off echo normalisation.
  3. `ReviewedSourceAuthority.__post_init__` names `(dict, MappingProxyType)`, which is exactly
     `deep_freeze`'s output pair for a mapping.
  4. `_merged_component_rejection_result` must KEEP its `isinstance(data, Mapping)`.
     `ToolResult.__post_init__` runs `freeze_fields(self, "data")`, so `base.data` is a
     `mappingproxy` and `type(data) is dict` would be permanently False — every merge would take
     the else branch and the rejection envelope would reach the model carrying ONLY
     `components_withheld`, with `error_code` and every detail silently dropped. A data-loss trap,
     and a standing invitation because the surrounding file is full of `type()` conversions.
  All four are pinned in `tests/unit/web/composer/test_frozen_state_nominal_type_guards.py`, each
  mutation-verified to fail alone under its specific narrowing. General rule: before swapping
  `isinstance(x, Mapping)` for `type(x) is dict` on composition-state or ToolResult data, find the
  thaw (or the absence of one) between the frozen owner and the read — and if you keep `isinstance`,
  say in a comment WHY, because the next lane will otherwise "converge" it.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — a `@trust_boundary`-suppressed site DOES stop counting toward a `per_file_rules` `max_hits:` ceiling, and driving a pattern to ZERO turns the gate line into `Unused tier-model per-file rule`**
  This narrows the B37 note ("suppressing sites under a boundary lowers the count without clearing
  the finding"): the count really does go all the way down, so on a file whose only allowlist
  coverage is a per-file ceiling, declaring honest boundaries is a complete fix for the overage.
  Measured in B40 on `web/composer/tools/_common.py`, whose two ceilings (R1 `max_hits: 10`, R5
  `max_hits: 13`) both reported overage at HEAD (`matched 16/10`, `matched 24/13`). One
  `@observation_boundary` on `_options_with_ascii_safe_scrape_headers` moved them to 14/10 and
  22/13 — exactly the four sites it suppressed — and the finished lane (41 → 8 allowlist-disabled
  findings) cleared both ERROR lines. But note the endgame: R1 reached zero, and the R1 `pattern:`
  entry then reported `Unused tier-model per-file rule` instead. That is a NEW gate line the lane
  cannot clear, because `config/cicd/enforce_tier_model/*.yaml` is operator-owned — hand the dead
  entry to the operator rather than trying to land at exactly one residual finding to keep the rule
  alive. Two corollaries: (a) a per-file `pattern:` entry carries no
  `scope_fingerprint`/`ast_path`/`judge_metadata_signature`, so a SIDECAR RATIONALE for a site it
  covers buys nothing — only code removals move the `matched N` — and no signed ast_path runs
  through the file, so restructuring is free; (b) run the experiment (one decorator, re-scan under
  the REAL allowlist, read the `matched N/cap` line) before investing in a sweep, and keep the two
  measurements separate: removals show up in the allowlist-DISABLED diff, cap progress only in the
  real-allowlist `matched` line.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — swapping `isinstance(x, (list, tuple))` for the house `type(x) not in (list, tuple)` breaks mypy narrowing when `x` is `T | None`, and the fix is to delete the Optional, not to add a `cast`**
  In `_normalize_echoed_interpretation_requirements` (B40) the guarded local was
  `stored_rows = stored_options[KEY] if KEY in stored_options else None`; `isinstance` narrowed away
  the `None` arm for the `for stored in stored_rows` below it, and `type(...) not in (...)` does not,
  so mypy failed with `Item "None" of "Any | None" has no attribute "__iter__"`. Hoisting the
  membership test into its own early return (`if KEY not in stored_options: return options, False`)
  and then reading `stored_options[KEY]` unconditionally removes the sentinel entirely — the local is
  no longer Optional, mypy is satisfied, and the abstain branch is more legible than the sentinel it
  replaces. Check the local's declared type, not just the else-branch, before the swap, and run mypy
  on the file rather than trusting the scoped tests.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — a platform-conditional stdlib constant probed with `getattr(os, "O_NOFOLLOW", None)` is BOTH a tier_model R2 and a masquerade baseline entry; the honest form is a direct read under `except AttributeError`**
  `_open_owner_only_database` (`web/auth/local.py`, B30) probed `os.O_NOFOLLOW` with a `None` default
  and then raised `LocalAuthStorageSecurityError` on `None`. Python documents `O_NOFOLLOW` as "not
  present if not defined by the C library", so the check is real, but the getattr-default is not
  needed to express it: `try: nofollow = os.O_NOFOLLOW / except AttributeError as exc: raise
  LocalAuthStorageSecurityError(...) from exc` is the same fail-closed outcome with no
  dynamic-attribute site — it clears R2 and deletes the `module-getattr` row from
  `config/cicd/masquerade_baseline.yaml` (reseed in the same commit; `seed_baseline --check` exits 0
  only after the reseed). Do NOT reach for `hasattr` (R3) or a module-level probe that turns a
  first-open failure into an import-time one unless the module already fails to import on that
  platform for the same reason. Companion in the same lane: `_parse_https_url`
  (`web/auth/urls.py`) tightened `isinstance(raw_value, str)` to `type(raw_value) is not str`, the
  house scalar idiom. Note (lens-A audit, comment 8798 F7): the originally taught "impostor
  `__contains__` sails past the scans" threat cannot occur there — `raw_value.strip()` returns an
  exact `str` for any subclass, and every separator scan runs on that minted value, so the base tree
  already rejected the impostor. The narrowing stands as fail-closed house style, not as closing a
  live hole; pinned by
  tests/unit/web/auth/test_urls.py::test_url_values_must_be_exact_str_not_a_subclass_or_lookalike.
  See [CONTRIBUTING: Gate: attribute contracts (dynamic-attribute sites)](../../CONTRIBUTING.md#gate-attribute-contracts-dynamic-attribute-sites).

- **2026-08-29 — `isinstance(value, Enum)` is a PERMANENT R5 justify candidate: `type(value) is Enum` is unconditionally False, so the "swap to the house exact-type idiom" move silently deletes the check**
  An enum member's concrete type is always the authored subclass (`ResponseFormat`,
  `OutputFieldType`, …) — `Enum` itself is never any member's `type()`. There are 8 such sites
  tree-wide across at least five buckets (`telemetry/serialization.py`,
  `telemetry/exporters/{console,datadog}.py`, `contracts/audit_export.py`,
  `core/landscape/serialization.py`, `core/landscape/execution/sink_effect_identity.py`,
  `web/catalog/knob_schema.py`), and every one is the same shape: lower a member to
  `str(member.value)` on the way onto a wire, audit or persisted projection. They are Tier-1 values
  and not admission gates — nothing is accepted or rejected, only lowered — so the disposition is a
  rationale, and `issubclass(type(value), Enum)` is the identical test spelled to evade the matcher
  and must not be used. B01 already ruled the `audit_export._string` instance; B32 ruled
  `knob_schema._attach_default`. The swap is tempting because the *class*-side question one function
  away legitimately reads `isclass(inner) and issubclass(inner, Enum)` (`_kind_for_scalar`) — class
  side and instance side are different questions and must agree. Mirror-image lesson from the same
  file: `not isinstance(x, Mapping)` DOES convert to `type(x) is not dict` when the value's permitted
  set really is the one concrete builtin — in `knob_schema._attach_required_when` the predicate is
  authored as a dict literal in `json_schema_extra` and the same function already tests
  `type(extra) is not dict` one line earlier, so the swap is stricter, consistent, and removes the
  finding. Check the file's own neighbouring idiom before deciding, and fix the error message if it
  says "mapping".
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — the structural-container / nominal-element split is the house shape for validating a constructor parameter, and its `isinstance` half is a justify candidate, not a conversion candidate**
  Learned burning down `web/composer/guided` (B46). `GuidedSession._validated_component_mapping`
  states it in one function: the CONTAINER test is `isinstance(value, Mapping)` because the container
  is an abstract protocol ELSPETH does not own and the reachable domain genuinely spans `dict` and
  the `MappingProxyType` the class's own `freeze_guided_json_mapping` produces — `type(x) is dict`
  would reject the frozen form the class itself emits — while the per-ELEMENT test one line below is
  `type(item) is not item_type` because `item_type` is always an owned closed class
  (`SourceResolved`, `SourceIntent`, `SinkOutputResolved`, `SinkIntent`) that must not admit a
  subclass into a session that is later content-hashed. Same split in `reorder_reviewed_components`
  (`Sequence` container, exact `UUID` element) and in `SourceIntent.__post_init__`. Corollary trap:
  the `isinstance(x, (str, bytes, bytearray))` operand paired with every `Sequence` acceptance CANNOT
  become `type(x) in {...}` — the exclusion has to catch str SUBCLASSES too, and the exact-type form
  lets one through into the element loop, where an empty `str` subclass would even pass a permutation
  check as an empty ordering. Also from that lane: `d[k] if k in d else None` cleared all four R1
  sites with byte-identical semantics, including `updated.get(key)` inside a
  `for key in <constant key tuple>` loop, where the membership form is spelled as an
  `if key not in updated: continue` guard ahead of the read rather than a ternary.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — `web/execution/` has DECLARATION tests that pin the exact tier_model finding set, so REMOVING a finding there turns the lane red until the `Counter` is updated**
  `tests/unit/web/execution/test_validation_trust_tier.py` calls `scan_file_with_observations` at
  test time and asserts equality against two hand-maintained literals — `_ADJUDICATION_CANDIDATES`
  (per-file lists, covering the `_validation_*.py` glob plus `validation.py`) and
  `_COMPLETION_GATE_ADJUDICATION_CANDIDATES` (a `Counter` for `completion_gates.py`) — plus
  `_EXPECTED_SUPPRESSION_OBSERVATIONS` for the `@trust_boundary`-suppressed lines. B53 cleared the
  three `R1:parse_completion_gates` reads and the file's own pin failed with
  `Right contains 1 more item: {'R1:parse_completion_gates': 3}`. Update the literal AND its
  explanatory comment in the same commit; the comments are the standing adjudication note for those
  sites, so a stale one misdescribes the code a judge will read. Note `_production_files` discovers
  by glob with a `_KNOWN_PRODUCTION_FILE_COUNT` floor, so a new `_validation_*.py` sibling joins the
  pinned set automatically — but `accounting.py`, `fanout_guard.py`, `outputs.py` and `preflight.py`
  are NOT covered by it.
  See [CONTRIBUTING: Gate: trust-tier lint corpus](../../CONTRIBUTING.md#gate-trust-tier-lint-corpus).

- **2026-08-29 — before converting an `x.get(k, ())` iteration to a membership read, check the PUBLISHED field type, not the builder's local variable**
  In `web/execution/fanout_guard.py` the local `queue_predecessors` inside `_build_producer_index` is
  a `dict[str, dict[str, _Producer]]` (keyed for dedupe), but the frozen field it becomes on
  `_ProducerIndex` is `Mapping[str, tuple[_Producer, ...]]` — `frozen_predecessors` converts each
  inner dict to a sorted tuple on the way out. Reading the builder and "fixing" the walk site to
  `.values()` therefore looks like a bug fix and is actually a break: the consumer already iterates
  producers, not keys. Same file, two names, two shapes. Mypy catches it, so run
  `PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src .venv/bin/mypy <files>` on every touched file before
  committing rather than relying on the scoped test run. Related, confirmed empirically in B53:
  converting `isinstance(x, InterpretationReviewPending)` to `type(x) is ...` in
  `_identity_state_for_compiled_ids` fails mypy with `Incompatible return value type`, exactly as the
  `type(x) is C` narrowing entry predicts — that union discriminator's else-branch IS used.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — `deep_freeze` turns every NESTED mapping into a `mappingproxy`, so `type(x) is dict` on anything reached through a frozen `options` bag is ALWAYS False**
  Measured in B51: `type(deep_freeze({"provider_config": {...}})["provider_config"])` is
  `mappingproxy`. `NodeSpec.__post_init__` (and its `SourceSpec`/`OutputSpec` siblings) call
  `freeze_fields(self, "options")`, so every composition-state option value below the top level is
  frozen. This makes the Wave-1 `isinstance` → `type() is` sweep actively DANGEROUS on composition
  state: `web/execution/service.py`'s nested path allowlist reads
  `provider_config = node.options["provider_config"]` and guards it with
  `isinstance(provider_config, Mapping)` — converting that to the exact-type idiom would `continue`
  past every frozen node and silently disable the defence-in-depth confinement of RAG
  `persist_directory` writes, with no test failure to show for it (`/execute` does not require
  `/validate` first, so this loop is the last gate). Rule of thumb: `type(x) is dict` is only ever
  correct on a value known NOT to have been through `deep_freeze` — a freshly parsed YAML/JSON tree,
  not composition state. Check the owning dataclass for `freeze_fields` before converting any
  container check.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — `for k, v in mapping.items()` DOES keep the `@trust_boundary` derived-name trail, and swapping `cast(dict[str, Any], x).get(...)` for an annotated local RESTORES suppression**
  Two measured complements to the `enumerate()` and call-laundering holes. (1) `subject_is_rooted`
  descends an `ast.Call` through `Call.func`, so `named.items()` roots at the `Attribute`'s value
  `named` — derived — where `enumerate(named)` roots at the builtin and is lost; the loop's tuple
  unpacking then keeps both targets derived. (2) The same `Call.func` descent is why
  `cast(dict[str, Any], source).get("plugin")` belongs to `cast` and suppresses nothing: bind
  `singular: dict[str, Any] = source` first and read `singular.get("plugin")`, which is derived
  through ordinary assignment propagation. That is not a lint dodge — it deletes a `cast` and reads
  better. In B51 one `@observation_boundary(source_param="config", suppresses=("R1",))` on
  `_discover_blob_rows_sources` plus those two moves cleared all 4 of the function's R1 findings,
  verified by the `R_TB_SUPPRESSED` stream. Corollary from the same lane: `@observation_boundary` on
  a genuine non-raising projector passes `trust_boundary.tests,scope,tier --fail-on-inert` with no
  `test_ref` obligation.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — three traps from B39 (`web/composer/tools/generation.py`)** (elspeth-obs-07866fb4e4)
  1. `type(x) is C` on a closed OWNED union needs `@final` on `C`, or mypy refuses to narrow the
     NEGATIVE branch. Converting `isinstance(resolved_blob, UnresolvedClaimedProofBlob)` to the
     house `type(...) is ...` idiom cleared the R5 finding and immediately produced five
     `union-attr` errors on the reads BELOW it: mypy narrows the positive branch of `type(x) is C`
     but will not remove `C` from the union on the negative branch unless `C` cannot be subclassed.
     `@final` on the marker class is the honest fix — it declares the closedness the idiom already
     assumes — not a `cast` and not a revert. Budget it whenever an `isinstance` discriminator over
     an owned union whose else-branch reads members is swapped.
  2. A signed tier-model entry can go DEAD, and its site then looks exactly like a lane's own
     target. Five `web.yaml` entries for this file suppress nothing: four
     `R6:compute_proof_diagnostics` keys whose handlers were extracted into
     `_compute_proof_diagnostics_for_source` (the SYMBOL component can never bind again) and one
     whose `fp=` drifted when a `MemoryError` handler was added beside the signed one. So "raw count
     minus plan count" is not a lane doing extra work. The probe that tells dead from live: copy the
     ONE file into a throwaway root and run `check --rules trust_tier.tier_model --root $T
     --repo-root <wt> --allowlist-dir <wt>/config/cicd/enforce_tier_model` — note the explicit
     `--allowlist-dir`, because the allowlist path resolves off `--root`, not `--repo-root`, and
     without it the run dies with `FileNotFoundError`. Entries that bind clear their findings;
     entries that do not are printed by key as "Stale tier-model allowlist entry". Other files'
     entries also report stale in that run (their files are absent from `$T`) — only lines naming
     your file are evidence. Carry a dead entry's reasoning onto a live `ast=` key and flag the dead
     key for pruning (elspeth-obs-07866fb4e4); never hand-edit the YAML.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — mypy gives `type(x) is C` NO negative-branch narrowing on a union, so the exact-type idiom is not a drop-in for `isinstance` when the ELSE branch reads the other arm**
  Measured in B41 with a three-case probe: on `A | B`, `if isinstance(r, B): ... return` leaves `r`
  correctly narrowed to `A` afterwards and checks clean, while BOTH `type(r) is B` and
  `type(r) is A` leave the full union on the negative branch — the probe raised `union-attr` on
  `r.plugin` and `arg-type` on the other ordering. Converting `if isinstance(resolved, ToolResult):`
  in `tools/sessions.build_set_pipeline_candidate` (over the owned closed union
  `_ResolvedSourceBlob | ToolResult`) produced three fresh mypy errors on the success arm's
  `resolved.plugin` / `resolved.options` reads. The rule of thumb "`type(x) is C` is the house idiom
  for scalars and closed owned unions" holds only where the negative branch does not need the other
  arm's type; a POSITIVE-branch-only discriminator (`type(v) is dict` then `dict(v)`,
  `type(s) is not str` then `raise`) is still fine and still checks clean. Do not reach for a `cast`
  to rescue the swap: that deletes the real type check to change a lint shape. Such a site is a
  justify candidate, not a conversion candidate.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — how to tell which findings sit at SIGNED allowlist sites, and what that does (and does NOT) change; signature churn is never a reason to withhold a fix** (d39651187f78a341)
  The mechanism: a keyless `--rules all` run reports a signed entry's site as a finding anyway
  (signature verification is fail-closed without the operator key), so a site can look
  un-allowlisted while a matching, judge-ACCEPTED, non-expired entry exists. Rewriting such a site
  re-rolls its `fp=` key, nothing matches, and the gate gains a `Stale tier-model allowlist entry`
  line. Measured in B41 on
  `sessions.py:R5:_detect_unresolved_interpretation_placeholders_typed`
  (`fp=d39651187f78a341`, judge_verdict ACCEPTED, expires 2026-10-06): an
  `isinstance(prompt_template, str)` → `type(...) is not str` conversion removed one R5 and added
  one stale entry. The maintainer's ruling: an honest fix is ALWAYS preferred to minimising churn —
  signing effort is the lowest priority, behind clean honest code. So if the right code change
  removes a finding at a signed site (binding or stale), MAKE IT; the entry becomes `stale_delete`
  and the operator re-signs once. An earlier version of this entry said "leave it alone" — that was
  wrong, and B41 reverted its conversion for the wrong reason. What the signed/unsigned distinction
  is actually good for is narrower: (a) do NOT write a rationale that merely restates a
  still-binding signed ruling for code you did not change — either change the code or leave the site
  entirely alone; (b) a stale-signed site is UNCOVERED, so fix or rationalise it like any other
  finding. To identify them, diff the LIVE run (real allowlist) against the worklist's
  allowlist-DISABLED "raw findings" section: a site listed in the raw section that the live run does
  NOT report is covered by a binding signed entry. (B41 ultimately left that site as `isinstance` on
  its own merits — it is the prescribed maximally-informative Tier-3 form, and an exact-type check
  would newly reject the three `str` subclasses the tree defines — not to protect the signature.)
  Unrelated but same lane: take the baseline AFTER `git merge feature/unified-lineage`, or sibling
  lanes' merged work shows up as your own whole-tree delta.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — the `@trust_boundary` suppressor also loses the derived-name trail through a `try:` whose handler RETURNS, so the decode-then-read idiom suppresses nothing**
  Third known hole in the same walk, after the `enumerate()`/comprehension ones. `_visit_try_like`
  (tier_model `rule.py`) visits each handler from `branch_start` — the state BEFORE the try — and
  then sets the post-try derived names to `_intersect_snapshots(body_end, *handler_ends)`. It does
  not model the handler's early `return` as unreachable, so a name bound in the try body is
  intersected away by any handler that does not rebind it. Concretely,
  `try: payload = _decode_json(error_json) / except (TypeError, ValueError): return None` leaves
  `payload` NOT derived, and a `@trust_boundary(source_param="error_json")` on that function
  suppresses ZERO of the `payload.get(...)` / `isinstance(payload, ...)` reads below it — measured in
  B52 on `web/execution/diagnostics.py`, where the decorator produced 0 suppressions until the code
  was split. Note the asymmetry: ordinary assignment propagation uses the PERMISSIVE subtree scan
  (`_value_depends_on_boundary` → `_expr_contains_derived_reference`), so
  `payload = _decode_json(error_json)` outside a try WOULD be derived even though the value passes
  through a call; only the try/except join drops it. Do NOT pre-seed a name before the try to restore
  the trail — that is reshaping code to dodge a lint. The honest fix is to split the raising decode
  from a non-raising projection: keep `try: payload = _decode_json(...)` in the caller and give the
  parse its own boundary function whose `source_param` IS the already-decoded value, returning an
  owned frozen dataclass (`_node_state_error_envelope` → `_NodeStateErrorEnvelope`). All 8 R1/R5
  reads then sit directly on the source param and suppress, the caller reads owned attributes
  nominally, and only the one R6 on the decode handler needs a rationale. That split also caught a
  live defect: `payload.get("type") in _DIVERSION_ERROR_TYPES` raises
  `TypeError: unhashable type` for an envelope carrying a list/mapping `type`, which would abort a
  whole run's diagnostics read; normalising the field to `str | None` inside the owned envelope makes
  the membership test total.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — an exact key-set assertion (`if set(item) != expected: raise`) makes every later `.get()` on that mapping an unreachable-`None` read, and a SIGNED rationale can therefore be actively wrong rather than merely stale**
  `_parse_step_2_sink_tool_arguments` (`guided/chat_solver.py`, B37) proves
  `set(item) == {"name","plugin","options","required_fields","schema_mode","on_write_failure"}` and
  then read four of those six with `item.get(...)` while reading the other two as `item["name"]` /
  `item["on_write_failure"]` two lines apart — the `.get()`s were leftovers, not a contract. Their
  four `fp=`-keyed entries in `config/cicd/enforce_tier_model/web.yaml` each argue that "a
  missing/malformed key becomes None and is rejected at the boundary"; the missing half of that has
  been impossible since the key-set assertion landed, and all four fingerprints had already gone
  stale. Generalises the "`d.get(k, DEFAULT)` right after `if k in d`" rule: the guard does not have
  to name the key — a whole-set equality upstream is the same proof, and converging on the subscript
  the neighbouring lines already use is a removal, not a dodge. Corollary when auditing: read a stale
  signed rationale for CORRECTNESS before re-justifying the site, because re-signing prose that
  describes an unreachable branch launders a defect into the audit trail. Same lane, two measurement
  notes: (a) adding a `@trust_boundary` / `@observation_boundary` does NOT shift module-level
  `body[N]` indices the way a docstring or a new statement does — decorators live in
  `decorator_list`, so a decorate-only commit leaves every other signed `ast_path` in the file intact
  (verified by diffing `scan_file` keys before/after: removals only, no re-indexed survivors); (b) a
  per-file `pattern:` entry with `max_hits:` is a RATCHET that reports its own overage
  (`matched 36/11`), so suppressing sites under a boundary lowers the count without clearing the
  finding — only the operator can lower the cap, and the residual is not a lane defect.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — a "NOT a declared trust boundary because X" comment is a claim about the tree at the time it was written; re-verify X before honouring it**
  `web/composer/redaction._coerce_stringified_json_object` carried a comment refusing
  `@observation_boundary` because bare `json.loads` could escape a `RecursionError`. Its own
  docstring, two paragraphs down, recorded that decoding had since moved to `bounded_json_loads`
  (which maps `RecursionError` to `JsonBoundaryError`, a `ValueError` the handler already catches) —
  the refusal had outlived its reason and was blocking an honest declaration. B36 declared it and
  pinned the invariant with a 20,000-deep `"["*N` input that must return the input object untouched
  (`test_coerce_stringified_json_object_never_raises_on_hostile_text`). Two neighbours from the same
  lane: (1) a value returned from a positional-arg call on the source param
  (`decoded = bounded_json_loads(value, ...)`) stays OUTSIDE the boundary walk, so the R5 on
  `isinstance(decoded, dict)` survived the decorator; because `bounded_json_loads` runs plain
  `json.loads` with no `object_pairs_hook`, a JSON object decodes to exactly `dict` and
  `type(decoded) is dict` is the exact nominal form, not a weakening. (2) In a fail-closed redactor,
  swapping an ABC `isinstance(x, Mapping)` for the Tier-1 nominal `type(x) is dict` changes what
  happens to the OTHER branch: `redact_source_storage_path._redact_one` used to return a
  non-`Mapping` `options` value unchanged, so a read-only-`Mapping` carrier that was previously
  redacted would have started passing through un-redacted under a naive swap. The correct move is to
  make every non-dict, non-None carrier RAISE `AuditIntegrityError` (it is a corrupted first-party
  serializer shape either way), and pin it with a `MappingProxyType` options carrying a private path
  — see `test_redact_source_storage_path_rejects_non_dict_options_carrying_blob_path`. Read the
  else-branch before the swap, and in a redaction surface prefer strengthen-to-raise over
  preserve-pass-through.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — the `trust_boundary.tests` gate reads exception names out of `invariant` PROSE with a `*Error|*Exception|*Warning` suffix regex, so a boundary whose raise type has no such suffix must not mention an Error-suffixed base class in its invariant** (0f6b9b6a3)
  `bind_guided_reviewed_components` (`guided/planning.py`, B38) raises
  `GuidedCandidateBindingRejected`; an invariant reading "raises GuidedCandidateBindingRejected (an
  AuditIntegrityError ...)" made the gate extract ONLY `AuditIntegrityError`, find the test_ref
  raising `GuidedCandidateBindingRejected`, and report `R_TB_TESTS_INVARIANT_MISMATCH`. Name only the
  exception the test's `pytest.raises(...)` names, or none. Two neighbours from the same lane: (a)
  `test_fingerprint` is `sha256(ast.dump(<test FunctionDef>, annotate_fields=True,
  include_attributes=False))` — compute it from the test file rather than guessing, and any edit to
  that test body re-rolls it; (b) mypy does NOT narrow a union of TypedDicts on `"key" in d`, so read
  a member that only some union arms carry through a `members: Mapping[str, object] = d` view
  (`_projection_kind_summary`) instead of reaching for `.get()` or a `cast`. Trust-tier note from the
  same file: the persisted guided TURN payload (`current_turn["payload"]` — wire and proposal
  projections) is content-hash-verified on load (`routes/composer/guided.py`,
  `guided_json_payload_id("turn", ...)`), so it is Tier-1 server data: membership-form reads raising
  `AuditIntegrityError`, never a `@trust_boundary`. The one Tier-3 payload in that module is the
  planner's candidate `pipeline`, and its boundary is the binder itself. Worktree trap hit on the way
  to committing this: the `mypy` pre-commit hook is `.venv/bin/mypy` with no `PYTHONPATH` of its own,
  so inside a worktree it resolves `elspeth` imports through the MAIN checkout's editable install and
  type-checks a split tree. `export PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src` before `git commit`;
  never `--no-verify` around it. (The six `interpretation_state.SOURCE_AUTHORING_KEY` "not explicitly
  export" errors it surfaced were a pre-existing implicit re-export, fixed on
  `feature/unified-lineage` by 0f6b9b6a3 — merge, do not re-fix.)
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — the membership-form ternary that clears R1 is NOT a drop-in for `.get()` on a value that might not be a container**
  `d["k"] if "k" in d else None` is exactly equivalent to `d.get("k")` for a Mapping and clears the
  tier_model R1 finding (verified in B35: one conversion, one rescan, −1). But `"k" in x` raises
  `TypeError` on a non-container where `.get()` could not exist at all, so inside a function whose
  `@trust_boundary` / `@observation_boundary` invariant declares "never raises" the conversion
  silently breaks that contract unless a Mapping guard stays ahead of it in the same `and` chain. In
  `web/composer/pipeline_planner._serialize_provider_discovery_result` the generator reads
  `isinstance(candidate, Mapping) and (candidate["id"] if "id" in candidate else None) == selected_id`
  — the guard first, the membership second, both inside the same short-circuit. A second trap in the
  same conversion: `"k" in c and c["k"] == want` is NOT equivalent to `c.get("k") == want` when
  `want` can be `None`, because the `.get()` form matches a candidate that lacks the key entirely.
  Keep the ternary (which preserves that) rather than the two-term `and` unless the compared value is
  known never to be `None`.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — `PlannerDeclined` subclasses `PipelinePlannerError`, so the planner exception classifiers must stay `isinstance`**
  `pipeline_planner.py` declares `class PlannerDeclined(PipelinePlannerError)`, and both
  `plan_pipeline`'s summary classifier and `_PlannerAttemptTrail.finalize_active_exception` depend on
  that: the former orders the `PlannerDeclined` arm ahead of the `PipelinePlannerError` arm on
  purpose, and the latter needs a decline to fall INTO the `PipelinePlannerError` arm to reach
  `exc.code`. Converting either to `type(exc) is PipelinePlannerError` (the house scalar/closed-union
  idiom) is a behaviour change that misroutes every decline. R5 findings on exception classification
  over an open owned hierarchy are justify candidates, not conversion candidates.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — the `R_TB_SUPPRESSED` lines in an allowlist-disabled `--rules all` run are the ORACLE for what widening a `suppresses` tuple will clear, and comprehension loop variables ARE tracked**
  Before hand-editing anything inside a `@trust_boundary`, read that file's `R_TB_SUPPRESSED`
  diagnostics: each names the site, the `source_param`, and the currently declared `suppresses=(...)`.
  Every site already listed there under R5 has a resolved derivation from `source_param`, and
  `_boundary_root` roots R1 (`node.func.value`) and R5 (`node.args[0]`) at the SAME name — so a
  `d.get(k)` on a name whose `isinstance` is already suppressed on that line clears the moment `"R1"`
  joins the tuple. In `planner_authoring_aids.py` (B42) widening three `("R5",)` tuples to
  `("R1", "R5")` removed 9 of 27 findings with zero code change, and that included
  `schema.get("composer_hidden")` where `schema` is a SET-COMPREHENSION loop variable over
  `raw.get("properties").items()`. The walk does follow comprehension/genexp targets bound directly
  from a derived iterable; what defeated B47 was the `enumerate(nodes)` CALL wrapping the iterable,
  not the comprehension (this narrows the broader B47 claim). So: widen, re-measure, and only then
  write rationales or restructure what actually survives — every pre-emptive edit shifts `body[N]`
  for later findings in the same function for no gain.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — adding a DOCSTRING shifts every `body[N]` index inside that function, exactly like the `@overload` trap**
  A docstring is `body[0]`, so writing one on an existing function moves every tier_model finding in
  it by one (`body[10]/body[0]/handlers[0]` → `body[10]/body[1]/handlers[0]`) and invalidates any
  signed `ast_path` in `config/cicd/enforce_tier_model/*.yaml` that runs through it. Measured in B25
  on `web/paths._is_uuid_path_segment`. This bites harder than the overload case because adding a
  docstring feels like a comment-only edit and is a routine part of "clean the file to house style".
  Re-derive ast paths after ANY docstring addition, not just after signature changes.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — R4 fires on a broad handler even when it returns an explicit error RESULT; only a `raise` clears it**
  R4 and R6 are asymmetric, and the asymmetry is in the rule source
  (`trust_tier/tier_model/rule.py::_check_exception_handler`): R6 calls `_handler_is_silent`, which
  treats a non-default `return` as an explicit outcome and does NOT fire, whereas the R4 arm scans
  only for `ast.Raise`. So the whole diagnostic-probe idiom —
  `except Exception as exc: return ContractCheck(name, False, sanitize_error(...))` — is a permanent
  R4 justify candidate no matter how explicitly it reports. Do not "fix" these by re-raising: in
  `web/doctor.py` and `web/readiness.py` the handler is what keeps one failed probe from aborting the
  other ~20 checks, which is the entire point of a preflight report. Rationalise them, naming the
  reporting symbol (`sanitize_error`, `_exception_failures`, `_finish_lock_cleanup`) as the control.
  B25 rationalised 16 such handlers in `doctor.py` and 9 in `readiness.py` on exactly this ground.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — the `@trust_boundary` suppression dataflow walk does NOT follow dynamic escapes, and widening an existing decorator's `suppresses` tuple is the cheapest correct burn-down move**
  Two facts, learned burning down `web/composer` (B47). The comprehension half of the original claim
  is NARROWED by the oracle entry above — plain comprehension targets ARE tracked.
  1. Several boundaries were declared `suppresses=("R5",)` while their bodies were full of honest
     `source_param`-derived `.get()` reads. Adding `"R1"` to the tuple removed 20 findings in
     `required_controls.py` alone with no code change — check the tuple BEFORE writing a rationale or
     restructuring a parse. `@trust_boundary`/`observation_boundary` still suppress ONLY R1 and R5
     (`contracts/trust_boundary.py`); R2/R3/R4/R6/R7/R8/R9 always need real code or a rationale.
  2. The walk tracks derivation from `source_param` through plain attribute and subscript reads and
     through `for` loops over a derived value, but it CANNOT follow `vars(x)`,
     `object.__getattribute__(x, ...)`, `type(x).__mro__`, or `descriptor.__get__(...)` — nor a name
     bound by a comprehension/genexp loop variable whose iterable is wrapped in a call
     (`next((i for i, node in enumerate(nodes) if node.get("id") == ...)` stayed flagged inside a
     decorated function whose ordinary-statement reads of `nodes` were suppressed). Those sites
     surface unsuppressed even inside a correctly declared boundary, so they need a rationale or a
     membership-form rewrite; do NOT conclude the decorator is wrong or widen it further.
  Membership-form is often available with byte-identical semantics and no new raise path:
  `"id" in node and node["id"] == target` is exactly `.get("id") == target` (missing key → no match,
  never raises), which matters in `required_controls.py`, whose splice helpers run inside the planner
  `candidate_finalizer` seam where an unprefixed exception is a TERMINAL failure. Prefer that over
  `node["id"]` there.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — a tier_model R6 fires on `except E: errors.append(_err(...))`, which is the HEALTHIEST validator shape in this tree, not a swallow** (elspeth-bceffeba19)
  `_handler_is_silent` (tier_model `rule.py`) treats only `raise`, a non-default `return` and a
  non-default `yield` as explicit outcomes; a handler that appends a `ValidationEntry` to the
  validator's accumulator and falls off the end matches "no raise, no return ⇒ likely swallow". Every
  `CompositionState.validate`-family validator is built that way on purpose, because /validate must
  RETURN a verdict rather than raise (the 500 defect class elspeth-bceffeba19), so the finding is a
  rule blind spot and the disposition is a rationale naming the accumulator AND the `error_code` —
  not a restructure. Seven of B34's twenty R6 sites in `web/composer/state.py` were this
  (`output_name_invalid`, `scope_name_invalid`, `coalesce_union_type_incompatible`,
  `contract_config_invalid`, `row_union_name_invalid`, `row_union_branch_invalid`,
  `row_union_on_success_invalid`). Do NOT convert them to a `*_validation_message() -> str | None`
  helper to clear the finding: several build a structured detail (`CoalesceUnionTypeDetail`) or mutate
  a dedupe set (`schema_config_reported`) that a string return cannot carry, and it is indirection to
  move a finding out of sight either way. Corollary for measuring a lane: a boundary-suppressed
  removal does not delete a line from `check --rules all`, it turns it into an `R_TB_SUPPRESSED`
  informational line, so the whole-file count drops by less than the number removed (B34: real 47→34
  but file total 75→70, because 8 of the 13 became suppressed lines). Count with
  `--allowlist-dir <empty>` and `grep -v R_TB_SUPPRESSED`.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).
- **2026-08-29 — the `@trust_boundary`/`@observation_boundary` suppressor loses the derived-name trail through `enumerate(source_param)`** (elspeth-obs-d4eed3b8c2)
  `_visit_for_like` roots a `for` target with `subject_is_rooted(node.iter, ...)`, and `subject_is_rooted`
  descends an `ast.Call` through `Call.func`, so `for i, item in enumerate(queries):` roots at the builtin
  `enumerate`, not at `queries`; `item` and everything derived from it drop out of the derived-name set even
  inside a correctly declared boundary. Direct reads of the `source_param` in the same function ARE suppressed,
  so the split looks arbitrary. Same for any builtin wrapper (`zip`, `sorted`, `reversed`, `list`) over the
  source param — positional-arg passing never roots a call. Rationalise those sites naming the tracer
  limitation; do not rewrite `enumerate` into a hand-maintained counter to restore the trail. Live example:
  `_well_formed_query_entries` in `web/composer/state.py`.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — the pre-commit mypy hook typechecks a changed file's DEPENDENTS, so editing a module others import THROUGH can fail on a pre-existing `no_implicit_reexport` error**
  A first tier commit to `web/interpretation_state.py` was rejected by three errors in files it never touched:
  `composer/pipeline_proposal.py`, `composer/reviewed_source_authority.py` and `composer/guided/chat_solver.py`
  all import `SOURCE_AUTHORING_KEY` *through* `interpretation_state`, which re-imports it from its real owner
  `web/composer/state.py`. The errors were latent at HEAD (verified by running mypy against HEAD's copy of the
  file) and surface only once the re-exporting module enters the hook's changed set. Fix it AT the re-export —
  `from ... import X as X` — which clears every consumer at once; do not chase the importers. Ruff then splits
  that import into its own `from ... import (...)` statement, adding a module-level statement and shifting every
  later `body[N]`: check `config/cicd/enforce_tier_model/*.yaml` for a signed `ast_path` in the file first
  (`interpretation_state.py` has none; the only signed entry in that bucket is
  `web/app.py:R1:_BodySizeLimitMiddleware`, which is `fp=`-keyed and immune to the shift).
  See [CONTRIBUTING: Convention: repository and process hygiene](../../CONTRIBUTING.md#convention-repository-and-process-hygiene).

- **2026-08-29 — `web/interpretation_state.py` reads its scalar string node options through ONE declared boundary, `_node_str_option(node, key)`**
  It returns the value only when the key is present AND holds a `str`, else `None`. Ten
  `node.options.get(...)` + `isinstance(..., str)` pairs across `materialize_state_for_execution`,
  `_materialize_node_for_authoring`, `_materialize_node_for_execution`, `_ensure_prompt_template_hash`,
  `_web_scrape_raw_fields`, `_legacy_placeholder_sites`, `_missing_prompt_template_review_sites` and
  `_missing_model_choice_review_sites` consume the owned `str | None` and never re-interrogate the runtime type.
  Read a new scalar string option through it rather than growing an eleventh inline pair. The module's other
  Tier-3 reads use the membership form (`options[KEY] if KEY in options else None`), and `select_only` — a
  presence-AND-value question — is spelled `"select_only" not in options or options["select_only"] is not True`,
  never a ternary.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — the tier_model dataflow walk does NOT carry taint out of a `try:` body, and that — not helper calls — is why a decorated boundary can still show dozens of R1/R5 findings**
  In `yaml_importer.composition_state_from_runtime_yaml`, decorated
  `@trust_boundary(source_param="pipeline_yaml")`, 16 sites reading `doc` stayed open. The obvious explanation
  (the walk cannot cross the `_require_mapping(parsed, ...)` call) is WRONG and would have made bad judge
  evidence: `_queues_from_runtime_mapping` in the same file crosses `_require_mapping` twice and every one of
  its sites IS suppressed. Three probes pinned it: aliasing `parsed = pipeline_yaml` inside the `try:` clears 0
  sites; hoisting `parsed = yaml.safe_load(pipeline_yaml)` ABOVE the `try:` (leaving `safe_load` and
  `_require_mapping` in place) clears 15. When a boundary's parse is wrapped in `try:` to map parser errors,
  expect the downstream reads to need rationales; do not restructure to satisfy the walk, since hoisting drops
  the error mapping. Corollary: measure before decorating —
  `_collector_nodes_from_runtime_lists` was decorated, measured and reverted because it cleared 0 findings, and
  a suppression that suppresses nothing is a live test obligation for no gain.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — `@observation_boundary` is the cheap correct form for a pure projector, but it stops at comprehensions and closures**
  Two guided emitters (`_wire_schema`, `_structured_output_fields`) project untrusted `NodeSpec.options` and
  never raise, so `@observation_boundary` (= `trust_boundary(non_raising=True)`) fits with NO
  `test_ref`/`test_fingerprint` obligation — it cleared 15 of 35 findings in that file for two decorators. It
  did NOT clear reads whose derivation runs through a list comprehension plus tuple unpacking
  (`for query_name, query in entries`) or through a nested closure over an enclosing local
  (`_wire_schema.names`): the walk is intra-procedural, does not descend into a nested `FunctionDef`, and does
  not track comprehension results. Budget rationales for those.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — a declared `@trust_boundary` does NOT cover a name assigned across an `if`/`try` JOIN, so "already decorated" files still carry live R1/R5**
  `DerivedNameState` propagates source_param-derivedness statement by statement and INTERSECTS the end-states of
  both `if` arms (`visit_If`) and of body-vs-handlers (`_visit_try_like`). In
  `source_demand.stamp_source_options_with_guarantees`, which carries
  `@observation_boundary(source_param="options")`, `schema` is derived on the `dict(raw_schema)` arm and not on
  the literal `{"mode": "observed"}` arm, so it is un-derived at the join and its `isinstance` still fires. Same
  in `parse_source_data_contract_accepted_fields`, where `payload = json.loads(value)` sits inside a `try` and
  the handler arm never binds `payload`. Do not write a spurious mention of the source_param into the other arm
  — the rootedness test is syntactic, so `x = (options and ...)` would suppress while meaning nothing.
  Rationalise the site and state that the boundary is already declared. Choosing a decorator:
  `observation_boundary` (= `non_raising=True`) needs NO `test_ref` and is the cheap honest route for a
  projection/abstain helper; a RAISING `trust_boundary` additionally needs `test_ref` + `test_fingerprint` bound
  to a test whose own body raises while invoking the function through `source_param`. The fingerprint is
  obtainable key-free (the `trust_boundary.tests` rule emits the canonical value); the hard part is that a
  suitable directly-invoking raising test must exist. Six such `observation_boundary` decorators landed clean
  (`guided_blob_refs` x3, `prompts` x2, `yaml_generator` x1) with `trust_boundary.tests,scope,tier` passing
  under `--fail-on-inert`.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — a whole-tree `trust_tier.tier_model` run under the REAL allowlist currently UNDER-suppresses, because one stale entry makes the loader refuse** (elspeth-obs-ce7f1c5f56)
  `config/cicd/enforce_tier_model/web.yaml` `allow_hits[154]` binds to `web/composer/guided/steps.py`, which no
  longer exists, and the loader emits "stale allowlist entry ... Refusing to load." Signed entries that bind
  perfectly well then report as ACTIVE findings in a `--root src/elspeth` run (e.g.
  `yaml_generator.py:R9:_strip_web_metadata`), so a lane comparing a real-allowlist corpus before/after
  misreads pre-existing noise as its own regression. To attribute a suspected signed-entry break to a change,
  scan the ONE file under a throwaway root (`mkdir -p $T/web/composer && git show <base>:src/.../f.py >
  $T/web/composer/f.py`, then `check --root $T --repo-root <worktree>`) and A/B base-vs-current there; in
  isolation the entry binds and the finding disappears. Operator remedy: drop the entry and re-sign.
  See [CONTRIBUTING: Gate: trust-tier lint corpus](../../CONTRIBUTING.md#gate-trust-tier-lint-corpus).

- **2026-08-29 — `interpretation_state` re-exported `SOURCE_AUTHORING_KEY` without the `X as X` form, so mypy failed the PRE-COMMIT hook for six untouched modules**
  Under `--no-implicit-reexport` a plain `from ... import SOURCE_AUTHORING_KEY` is not a re-export, so
  `pipeline_proposal`, `tools/_common`, `tools/sources`, `tools/sessions`, `service` and
  `reviewed_source_authority` all reported `does not explicitly export attribute`. Fixed with the redundant
  alias (`SOURCE_AUTHORING_KEY as SOURCE_AUTHORING_KEY`), which ruff splits into its own
  `from elspeth.web.composer.state import (...)` block — that split is expected; the file already does it for
  `plugin_policy.coverage`. General trap: this failure reproduces from an UNMODIFIED file, so before "fixing" a
  mypy hook failure, run mypy on an untouched file and attribute it before editing anything.
  See [CONTRIBUTING: Convention: repository and process hygiene](../../CONTRIBUTING.md#convention-repository-and-process-hygiene).

- **2026-08-29 — NARROWING a broad `except` does not clear a tier_model R4; it CONVERTS it into an R6 at the same line**
  `TierModelVisitor._check_exception_handler` sets `is_broad` only for a Name/Tuple naming `Exception` or
  `BaseException`; anything else falls through to `_handler_is_silent`, which returns True for any handler whose
  own scope contains no `raise` and no non-default `return` (`_is_default_return_value` counts
  `None`/`""`/`0`/`False`/empty containers as silent). So `except BaseException as exc: x = exc` is R4 and
  `except (TypeError, ValueError) as exc: x = exc` is R6. Measured on `web/composer/tool_batch.py`: zero corpus
  gain either way. The only shape that clears both is a handler that RETURNS a non-default value, which means
  lifting the guarded region into a helper — the file's own `_try_finalize_proposal_custody`
  (`except BlobQuotaExceededError: return "quota_exceeded"`) is the template. Two limits: (1) a handler ending
  in `break` or `continue` cannot be extracted at all, which rules out every per-tool-call handler in
  `run_tool_batch`; (2) extracting a 5-line `try`/`except` solely so the rule sees a non-default `return` —
  especially when the natural return is `None`, forcing an invented sentinel Literal — is helper indirection to
  move a finding out of sight and is banned. Otherwise the honest disposition is a rationale stating this proof.
  Do not "simplify" a caught tuple either: `(json.JSONDecodeError, JsonBoundaryError, TypeError, ValueError)` in
  that file is redundant as dispatch (both JSON classes subclass `ValueError`) and is written out to DOCUMENT
  the audit taxonomy.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — `@trust_boundary` suppression is CALL-LAUNDERED: decorating a function does NOTHING for a finding on a value a helper RETURNED**
  The tier_model walk roots a subject at `source_param` through subscript, attribute, `.get(...)`, iteration,
  unpacking and walrus, but `trust_boundary_suppress.subject_is_rooted` descends an `ast.Call` through
  `Call.func`, NOT through `Call.args`, so `helper(payload)`'s result belongs to `helper`, not to `payload`. Two
  consequences: (1) `items, error = _current_sequence(payload["x"], ...)` leaves every per-element
  `isinstance(item, Mapping)` unsuppressed whatever decorator the enclosing function carries — the element check
  belongs to the element and must be removed or rationalised; (2) even INSIDE a decorated boundary,
  `for index, item in enumerate(value)` launders `item`, because the `For` iter is a call rooted at `enumerate`.
  Do not rewrite such a loop to `for index in range(len(value)): item = value[index]` to regain suppression;
  the honest answers are a rationale or a real removal. Decide suppressibility by RUNNING the rule (the
  non-failing `R_TB_SUPPRESSED` observation stream names every site a decorator actually covered) rather than by
  reading the decorator.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — in `web/composer/guided/protocol.py`, an exact-key check that feeds subscripts is `_exact_nested_mapping`, not `_exact_nested_keys` + `assert isinstance`**
  The module's ~17 call sites used to re-assert `isinstance(x, Mapping)` after a successful
  `_exact_nested_keys` purely so the following subscripts type-check — a runtime re-check of a fact the helper
  had just proved, and an R5 finding at every one. `_exact_nested_mapping` returns
  `(narrowed_mapping | None, error | None)`, converging those sites on the `(value, error)` idiom
  `_sequence_of_mappings` / `_current_sequence` already used; `assert x is not None` is not an R5. Where the
  subject is already `Any` (a subscript of an already-narrowed mapping, e.g. `node["behavior"]`) the assert is
  DELETED — mypy needs nothing and the check was dead. Same file: `_validate_propose_pipeline_payload`'s four
  edge indices (`outgoing_flows`, `incoming_edges`, `gate_routes`, `gate_forks`) are seeded DENSE over the
  domain their writes are proven to lie in, as `adjacency`/`reverse_adjacency` always were, so every read is a
  total member lookup and a broken domain invariant crashes instead of being relabelled "no flows" by a
  `.get(k, ())` default. Keep new indices in that shape; the `.get()`s that remain are those whose key is an
  LLM-authored id with no membership proof (`component_kind_by_id`, `node_by_id`) and are justified as such.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — `type(x) is C` and `isinstance(x, C)` narrow DIFFERENTLY in the negative branch** (ff917243a, a4f633728)
  Only `isinstance` removes `list` from the non-list arm under mypy; the exact-type form leaves the else-branch
  type untouched. `type(x) is C` is right for validating a scalar (it also subsumes the bool-vs-int special
  case, since `type(True) is int` is False), but it is NOT a drop-in for a union discriminator whose else-branch
  is used — check the else branch under mypy before converting (`engine/executors/state_guard.py:399`: the
  mapping arm is only assignable without a cast because `isinstance` narrowed `list` out of it). Nor is it a
  drop-in for a container check whose fall-through is a failure path: `aws_s3_sink._json_value_chars` must
  accept exactly what `json.JSONEncoder` accepts, and `json` dispatches on `isinstance`, so an exact-type test
  would send every `dict`/`list` SUBCLASS the encoder serialises down the "unsupported value" arm and mis-size
  the estimate. Tree-wide consequence: the Wave-1 burn-down converted ~50 `isinstance` checks to `type(x) is C`
  / `type(x) in {...}` across 17 files (`git diff ff917243a..a4f633728 -- src/`), an undocumented commitment
  that these ELSPETH-owned types are CLOSED — a subclass instance will DIVERT or be rejected at those sites,
  never pass: `SinkEffectPipelineMembersInput`, `SinkEffectPlan`, `SinkEffectMember`, `SinkEffectInspection`,
  `SinkEffectExecutionPurpose`, `NodeStateStatus`, `DiversionAttribution`, `AuditExportSnapshotChunkInput`, and
  the composer node models `nodes.Name`, `nodes.Output`, `nodes.TemplateData`; the same sites pin the builtins
  `str`/`int`/`float`/`bool`/`bytes`/`dict`/`list`/`tuple` and `MappingProxyType` exactly. Do not subclass one
  of these expecting the parent's checks to admit it — add the subclass to the check (or restore `isinstance`
  with an explicit rationale) in the same change.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — when a decode helper's return type is a union keyed on an argument, `@overload` on that argument's `Literal` removes the caller-side re-checks**
  `_returned_attempt` in `engine/executors/sink_effects.py` is overloaded on
  `action: Literal[SinkEffectAttemptAction.INSPECT | COMMIT | RECONCILE]`, so each call site gets the concrete
  result type and the five unreachable "decoded to the wrong result type" raises were deleted rather than
  rationalised. Trap: overload stubs are class-body statements, so adding them shifts every later `body[N]`
  index in the class — any signed tier_model allowlist entry whose `ast_path` runs through that class
  (`config/cicd/enforce_tier_model/*.yaml`) goes stale. Re-derive the ast paths after adding overloads, and
  place any NEW helper after the last signed site in the class rather than before it where possible.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — a tier_model R1 `d.get(k, DEFAULT)` sitting right after `if k in d or <helper>(...)` is usually a DEFECT, not a justify candidate**
  Check whether the helper caches the key before it returns True. In `RowUnionExecutor` it does:
  `_check_landscape_for_completion` calls `_mark_completed(key, reason)` on every truthy return, and
  `_mark_completed`'s bounded eviction is `OrderedDict.popitem(last=False)`, which pops the OLDEST entry and so
  can never evict the one just written (plain assignment does not reorder, and a fresh key is newest — true even
  at `max_completed_keys=1`). That made `self._completed_keys.get(key, _CLOSED_BY_RELEASE)` both unreachable AND
  wrong if reached: the landscape arm can cache `_CLOSED_BY_BRANCH_LOSS` or `_CLOSED_BY_PRIOR_FAILURE`, which
  the default would relabel `"released"` on its way into `_fail_pending`'s resume failure outcome. Read it as a
  member so a broken invariant crashes instead of writing a mislabelled audit reason. The same look applies to
  an R8 `setdefault` on a group accumulator: when the file already spells the accumulator as
  `if k not in d: d[k] = ...` elsewhere (`RowUnionExecutor.accept`, `CollectorJournalRestorer.restore`),
  converging on that idiom is a removal, not a dodge. Contrast a genuine justify: a `.get()` whose absent key is
  a first-class VALUE — `row_union_state_ids.get(token_id)` in `barrier_coordination.restore_from_journal`
  returns None for exactly the holdless members of a released group, and `reconcile_released_group` reads None
  as "already completed, do not write twice".
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — a `@trust_boundary` / `@observation_boundary` can only ever suppress R1 and R5**
  `contracts/trust_boundary.py:73` declares `type BoundaryRule = Literal["R1", "R5"]`, so decorating a function
  does NOTHING for an R2 (`getattr` default), R3, R4 (broad except), R6, R7, R8 or R9 tier_model finding —
  those must be removed with a real code change or rationalised. Do not reach for `@observation_boundary` as a
  way to clear a swallowed-exception finding; it is not one. Widening that vocabulary is not a lane-local edit
  either: the comment at lines 64-72 requires the operator to confirm a static-analysis story first, because a
  suppressible rule must be derivable from `source_param` through the tier_model dataflow walk, and R4/R6 are
  properties of a handler rather than of a derived value.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — SDK providers read `raw_response` through ONE declared boundary: `plugins/transforms/llm/provider.finish_reason_from_raw_response`**
  Azure and Bedrock each walked `response.raw_response.get("choices")[0].get("finish_reason")` inline; both
  copies are now the non-raising `@trust_boundary` in `provider.py`. `LLMResponse` deep-freezes the envelope, so
  inside it lists are `tuple` and dicts are `MappingProxyType` — the boundary discriminates with
  `isinstance(..., Sequence)`/`Mapping`, and `type(x) is list`/`dict` there would silently return `None` for
  every real response. A new SDK-shaped provider (anything routed through `AuditedLLMClient`) calls this helper
  rather than growing a third copy; the HTTP providers (openrouter/gateway) keep their own raising body
  validators because they parse bytes, not an SDK envelope. Related: an R6 on a handler that only stores the
  error and raises AFTER the `try` (the `terminal_error`/`redacted_error` record-then-raise and bounded-egress
  seams) is rationalised, not restructured — moving the raise into the handler either skips the audit write or
  re-attaches provider text via `__context__`.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — `validate_credential_safe_https_url` now lives at `elspeth.core.url_validation`, NOT `elspeth.plugins.infrastructure.*`**
  The module is stdlib-only (`re` + `urllib.parse`) and was already consumed by `web/` and `core/` as well as
  plugins, so it moved down a layer under ADR-006's "move the needed code down" protocol; that removed the L1
  upward import in `core/llm_profiles.py`, whose lazy in-function import is now a plain module-scope one.
  ADR-006 forbids compatibility re-exports, so there is NO shim at the old path — all 11 import sites and the
  test moved in the same commit (`tests/unit/plugins/infrastructure/test_url_validation.py` →
  `tests/unit/core/test_url_validation.py`). When porting a branch written before this, rewrite the import
  rather than resurrecting the module. The move shifts tier_model's `source_snapshot_sha256` (a file appears
  under `src/elspeth/core` and disappears under `src/elspeth/plugins/infrastructure`); that is honest relocation
  churn, not drift. The remaining L1 in `core/llm_profiles.py` (`plugins.transforms.llm.transform`) is
  deliberately still lazy: the provider config models are fused with their httpx/`AuditedHTTPClient` runtime
  clients, so neither of ADR-006's other two remedies applies yet.
  See [CONTRIBUTING: Convention: repository and process hygiene](../../CONTRIBUTING.md#convention-repository-and-process-hygiene).

- **2026-08-29 — inside a `@trust_boundary`, a name bound ONLY in a `try` body loses its source_param derivation after the `try`**
  The tier_model derivation walk intersects the derived-name snapshots of the body and every handler at the
  join, and does not notice that a handler ending in `raise` never falls through — so
  `try: data = json.loads(response.content) except ...: raise ...` leaves `data` un-derived and every
  `data.get`/`isinstance` below it is reported. Keep the decode in a plain helper
  (`data = _decode_gateway_json(response, ...)` in `providers/gateway.py`) or assert the shape member by member
  instead of catching `KeyError`; do not widen the `try` to swallow the checks. Filed as a lint observation;
  until it is fixed this is the shape that suppresses honestly. The same join rule applies to an `if`/`for`: a
  name pre-bound to `None` and rebound from `source_param` inside a loop body is NOT derived after the loop, so
  read and act on it inside the body that bound it (`LLMTransform.get_post_call_hints`) rather than accumulating
  into an outer variable.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — removing a baselined `getattr` site is a masquerade-gate edit**
  `config/cicd/masquerade_baseline.yaml` pins `occurrences` per (path, qualname, kind) and fires on a DECREASE
  and on a stale entry, so the commit that deletes the probe must delete its baseline block too. Landed this way
  for `textract_client._record_send_attempt`/`_observed_send_attempts`, replaced by a `threading.local` subclass
  with a declared `count`.
  See [CONTRIBUTING: Gate: masquerade sites](../../CONTRIBUTING.md#gate-masquerade-sites-tests-included).

- **2026-08-29 — `raise X from None` inside an `except` is NOT context-free; only a raise AFTER the handler is**
  `from None` clears `__cause__` and sets `__suppress_context__`, but the interpreter still attaches the
  in-flight exception as `__context__`. The S3 sink's serialization failures must carry neither (a
  `UnicodeEncodeError` / json `TypeError` message quotes the row's characters and would ride into the audit
  trail), and `tests/unit/plugins/sinks/test_aws_s3_sink.py::TestSerialization` pins `__context__ is None`. That
  is why `_EncodedTextWriter._encode`, `_check_json_record`, `_csv_scalar_text`, and the json loop in
  `_serialize_rows_to_spool` record a flag in the handler and raise after it — the tier-model R6 rule cannot see
  that the flag is consumed, so those sites are rationalised, not restructured. Do not "fix" them by moving the
  raise into the handler; the scoped tests catch it.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-29 — a component that polls a `Clock` must also SLEEP through it**
  `elspeth.core.clock.Clock` carries `sleep(seconds)`: `SystemClock` blocks, `MockClock` advances.
  `SinkEffectCoordinator` defaults its sleep to `clock.sleep` and takes the wall-clock `shutdown_event.wait`
  path only on a `SystemClock`; `Orchestrator._clock` threads through `SinkFlushCoordinator` → `SinkExecutor` →
  both coordinators. Before this, a resume whose crashed predecessor still held a sink-effect lease slept the
  full five-minute TTL in real time inside a unit test (`test_resume_does_not_rewrite_sequence_zero`, ~300s wall,
  14s CPU). A new poll-until-deadline loop must measure the deadline and wait on the SAME injected clock — never
  `time.sleep` beside `clock.monotonic()` — and any test fake typed as `Clock` needs `sleep`.
  See [CONTRIBUTING: Convention: test discipline (fakes, mocks, fixtures)](../../CONTRIBUTING.md#convention-test-discipline-fakes-mocks-fixtures).

- **2026-08-29 — computing a tier_model justify key by hand: `TierModelVisitor` is PATH-SENSITIVE, and the wrong relative path silently INVENTS findings**
  No `elspeth-lints check` flag emits the `<file>:<RULE>:<Symbol>:ast=<path>` justify key (`--format json`
  carries line/col/fingerprint but neither `symbol_context` nor `ast_path`), so a lane needing keys after its
  edits shift the ast paths has to call the rule in-process. Use
  `collect_check_result(root, allowlist_path=<empty dir>)` and read `Finding.symbol_context` /
  `Finding.ast_path` — NOT a hand-built `TierModelVisitor`. The visitor's first argument must be the path
  relative to the SCAN ROOT (`src/elspeth`), because `_R5_NAMED_BOUNDARY_CONTEXTS` is keyed on exactly that
  string and is gated by `if not self.file_path.startswith("web/")`. Passing a path relative to `src/` instead
  (`elspeth/web/composer/service.py`) misses both the prefix test and the dict key, and the named-boundary
  exemptions silently stop applying: one measurement produced 41 findings that way against the scanner's real 31
  in `web/composer/service.py` — ten fabricated justify candidates in `_cached_runtime_preflight` and
  `_validate_advisor_arguments`, with no error and no warning. Cross-check any in-process count against the
  CLI's line count for the same file before trusting it.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-29 — an ELSPETH-owned "closed sum type" can still be SUBCLASSED by test doubles, and `type(x) is C` breaks them**
  The Wave-1 `isinstance` → `type(x) is C` conversion is right for the closed unions listed in the narrowing
  entry above, but check for subclasses in `tests/` as well as `src/` before converting:
  `_ToolOutcomeResponse = ToolResult | Mapping[str, Any] | None` is documented as closed on `_ToolOutcome`, yet
  `ToolResult` is deliberately subclassed by doubles that ride the real compose loop — `_StrayToolResult`
  (`tests/property/web/composer/test_compose_loop_invariants.py`,
  `tests/integration/pipeline/test_composer_llm_eval_characterization.py`) and `_NonCanonicalizableResult`
  (`tests/unit/web/composer/test_compose_loop_audit_wiring.py`) — precisely to prove that a drifted response
  shape is sentinelized rather than persisted. An exact-type test at
  `_tool_batch_staged_terminal_interpretation_review_handoff` would route those down the "not a tool result" arm
  and skip the `not response.success` check, admitting a FAILED batch into the terminal pending-review verdict.
  Those three R5 sites are rationalised, not converted. `grep -rn '(ToolResult)' src/ tests/` is the check;
  "closed" in a docstring means no PRODUCTION variant, not no subclass.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-29 — tier_model R8 EXEMPTS `d.setdefault(k, []).append(v)` but fires on the same call the moment its result is bound to a name**
  The rule has an explicit carve-out, `_is_immediate_setdefault_grouping_call`, for the grouping idiom where the
  `setdefault` is the receiver of an immediate `.append`/`.extend`. So in one accumulator loop
  `consumers.setdefault(name, []).append(identity)` is silent while
  `destinations = consumers.setdefault(name, [])` two lines above it is an R8 — which reads as rule noise until
  the carve-out is known. Measured on `web/composer/guided/connection_consumers.py`, where the assigned form was
  there only because the loop de-duplicates (`if identity not in destinations`) and so cannot chain. The removal
  is the membership form the brief prefers — `if k not in d: d[k] = []` then `d[k]` — not a `defaultdict`
  (which re-hides the missing key on READ) and not a rewrite of the neighbouring chained calls, which are exempt
  and must be left alone. Corollary: an R8 on an accumulator seed is usually a real removal candidate, unlike an
  R8 on a lookup, so check whether the site chains before writing a rationale for it.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — for a best-effort telemetry emit, the operator-SIGNED precedent is `web/composer/telemetry_phase8.py`, and the rationale ground is audit primacy, NOT a new log line**
  `config/cicd/enforce_tier_model/web.yaml` carries a per-file R4 `pattern:` entry for that module covering all
  its `record_*` helpers; the reason is that telemetry is best-effort, a broken OTel exporter must not fail a
  request whose audit row already wrote, and the `_assert_*` programmer-error guards fire BEFORE the `try` so
  input validation still crashes loudly. Any sibling emitter with that shape
  (`web/composer/tutorial_telemetry.py`, `advisor_checkpoint_telemetry.py`) is rationalised against that
  adjudicated pattern rather than grown a divergent convention — adding structlog to one counter helper removes
  no finding and leaves two shapes for one mechanism. The one thing worth CHANGING at these sites is guard
  ordering: hoist any ELSPETH-OWNED call out of the swallowed region so its programmer errors propagate. In
  `advisor_checkpoint_telemetry.record_advisor_checkpoint_pass` the owned `stable_hash` was computed inside
  `with suppress(Exception)` around the `slog.info` emit, so a canonicalization `ValueError`/`TypeError` about
  an owned payload was indistinguishable from an exporter outage; binding `findings_hash` above the `suppress`
  fixes that for one line. Use the same test to decide whether a swallow needs a `TIER_1_ERRORS` re-raise arm at
  all: if the protected body contains no owned call (a bare `counter.add`, a third-party registry lookup), no
  Tier-1 error can originate there and the arm would be dead code — say so in the rationale instead of writing
  it.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-28 — `.pre-commit-config.yaml` lint hooks are pinned by `tests/unit/elspeth_lints/test_pre_commit_triggers.py`** (elspeth-7e8bf1c28b)
  Three whole-config contracts:
  1. A `--files` hook may select ONLY `RuleScope.INCREMENTAL` rules — `--files` is inert for a WHOLE_REPO rule,
     which rescans `--root` regardless, so such a hook is a whole-repo scan behind a subject-code trigger. Run
     those as policy hooks keyed to `config/cicd/<allowlist>/` plus the rule dir, with `pass_filenames: false`,
     and enumerate rule ids instead of `family/*` when the family mixes scopes.
  2. Every tracked non-fixture file a `--files` hook's rules would judge under its `--root` must also match the
     hook's `files:` regex — `--root` does not scope an explicit file list, so the trigger is the only scope,
     and a trigger narrower than the `path_filter` is silent (`Skipped`).
  3. Every `config/cicd/<dir>/` needs a consumer that re-runs on edit — a hook trigger naming it, a CI workflow,
     or the commit-msg script.
  Pre-commit ANDs `files` with `types: [python]`, so a Python-typed hook never fires on a YAML allowlist.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-28 — incremental lint policy gates pass `--fail-on-inert`, but changed-file hooks MUST NOT**
  A selected incremental rule whose `path_filter` matches zero non-fixture Python files never reaches
  `analyze()`, so without the flag it is byte-for-byte indistinguishable from a clean run. The full-root
  Composer and contract-invariant gates opt in and emit an ERROR finding naming the rule and exact filter. The
  `--files` hooks deliberately do not: a changed web file may legitimately reach `composer.catch_order` but not
  `composer.exception_channel`, and requiring every selected rule to match that partial input would reject
  ordinary commits. Lint-rule examples under `elspeth-lints/src/elspeth_lints/rules/**/fixtures/` never satisfy
  the count; real `tests/fixtures/` code still does. WHOLE_REPO rules are also exempt because their filter gates
  only the shared parse/read diagnostic walk while `analyze(empty_tree, root, context)` runs independently. The
  flag detects a wholly dead filter, not a dead alternate inside an otherwise-live regex; do not parse opaque
  regex text into a pretend directory inventory.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-28 — the no-stash pre-commit dispatcher needs a truthy, guaranteed-nonexistent `--files` sentinel for deletion-only indexes** (elspeth-c1e85e08c9)
  A deletion-only index is non-empty even though the `ACMRTUXB` path list is empty. Do not remove the
  dispatcher's empty-list branch and call `pre-commit run --files` with zero values: pre-commit parses that as
  `args.files=[]` and silently re-enables stashing. Do not pass the deleted path either: a cached deletion can
  still exist in the worktree, and filename-bound hooks would inspect content absent from the commit. The
  dispatcher first proves the index differs from HEAD, then passes a child of Git's index *file* as its
  impossible sentinel. Pre-commit drops that path from its classifier, keeps `args.files` truthy (stash
  disabled), and executes `always_run` hooks such as `secret-scan`. An empty-string placeholder is not
  equivalent; pre-commit normalizes it to `.`. This fix does not make `files:` regexes deletion-aware;
  whole-repo manifest/tree gates remain CI-owned unless the operator separately changes the local fast-gate
  policy in `.pre-commit-config.yaml`.
  See [CONTRIBUTING: Convention: repository and process hygiene](../../CONTRIBUTING.md#convention-repository-and-process-hygiene).

- **2026-08-28 — `composer.exception_channel` follows the module-local call graph; "catch locally" is a real exemption, not a comment** (elspeth-24ba2e24fa)
  The rule was dead for three months (`path_filter` named `tools.py` after it became a package) and, re-armed
  naively, flagged 59 sites that are all the sanctioned shape — a `@trust_boundary` Tier-3 parser or private
  helper raising `ValueError`, and its handler catching and converting. The rule now reports a bare
  `TypeError`/`ValueError`/`UnicodeError` only when some path lets it ESCAPE the module: a raise is contained
  when it sits inside a `try` whose handler names the exception, a base class, or everything, or when EVERY
  module-local call to its function is so guarded (transitively). A function with no module-local caller (a
  public handler, or a helper only reached from another module) escapes — fail closed. `__post_init__` is exempt
  (ADR-032 Tier-1 nominal invariants MUST crash). `_dispatch.py` is excluded by filter (dispatcher invariants,
  not handlers). Consequences: parse an LLM-authored value ONCE inside the guard and reuse it — a second "safe
  by construction" call outside the `try` is a finding; a trust-boundary helper with no production caller is
  dead code, delete it. The gate is zero-finding with `--fail-on-inert`; keep it that way rather than seeding
  `config/cicd/enforce_composer_exception_channel/`, whose `allowed:` schema the rule never read.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-28 — allowlist YAML enumeration is ONE non-recursive authority that REFUSES nested documents: `allowlist.iter_allowlist_yaml_paths`** (elspeth-3262174e37)
  `load_allowlist`, the source snapshot, and the judge-coverage HEAD loader (`allowlist_io.iter_yaml_documents`)
  all share it; a `*.yaml`/`*.yml` below the top level of an `enforce_*` dir raises
  `NestedAllowlistDocumentError` (surfaced as `AllowlistIOError` / `JudgeCoverageError`). The judge-coverage
  BASELINE side (`git ls-tree -r`) refuses nested paths the same way and keys `source_file` by the dir-relative
  path — never collapse a git path to its basename. Root-level aggregators over `config/cicd` (`override_rate`
  hash, `per_file_blanket_ratchet` HEAD) use `iter_allowlist_root_yaml_paths`, which skips every dot-prefixed
  subtree: `.sign-bundle-transactions/` materialises basename-colliding candidate allowlists on disk by design
  and was protected only by being untracked. Do not add a private `glob("*.yaml")`/`rglob` over allowlist dirs.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-28 — there is ONE Python-file walk authority: `elspeth_lints.core.ast_walker`** (elspeth-faadf9873e)
  `iter_python_files` / `walk_python_files` prune `EXCLUDED_WALK_DIRS` (`.venv`, `.worktrees`, `node_modules`,
  caches, ...) and the root-relative `EXCLUDED_WALK_PREFIXES` (`.claude/worktrees`) BEFORE descent. Every other
  walker — tier_model `iter_scannable_python_files`, audit_evidence `iter_python_paths`, the manifest rules,
  `contract_manifest.scan_source_tree`, `scripts/cicd/runtime_rejection_parity.py`, and
  `tests/unit/test_mock_discipline_baseline.py` — delegates to it. Do NOT write a new `rglob("*.py")` /
  `os.walk` / dot-prefix-skip / `.parts[:2]` idiom: a new walker MUST call `iter_python_files` (or import the
  two constants when it genuinely needs its own traversal) AND be registered in `WALKERS` in
  `tests/unit/elspeth_lints/test_python_file_walker_authority.py`, which runs every walker over a synthetic tree
  with both worktree conventions and pins `elspeth-lints/src` + `scripts/cicd` against any private walk. Keep
  BOTH `.worktrees/` and `.claude/worktrees/`; never add a bare undotted `worktrees` component. Changing the
  constants changes what tier_model's `source_snapshot_sha256` hashes — measure the hash before and after.
  See [CONTRIBUTING: Gate walker and scratch directory](../../CONTRIBUTING.md#gate-walker-and-scratch-directory).

- **2026-08-28 — root-wide Python lint walks prune repository-local worktrees BEFORE descent, and path filters run BEFORE parsing** (elspeth-9328bf28bb)
  `core/ast_walker.py::iter_python_files` uses a top-down walk so excluded directories are never entered;
  `.claude/worktrees` is an exact ROOT-RELATIVE prefix, never a bare `worktrees` component (tracked source may
  legitimately use that name). A scan rooted inside a worktree remains valid because the excluded prefix is
  measured from the requested root. Keep explicit `--files` behavior unchanged. Keep candidate selection on
  actual filenames: `Path.rglob("*.py")` includes directories whose names end in `.py`, which the generic
  diagnostics surface misreported as read errors. Keep directory-enumeration errors skippable, matching
  `Path.rglob`'s legacy behavior; an unreadable or concurrently removed subtree must not abort the whole CLI
  with a traceback, while per-file read errors remain structured findings. Tier-model discovery is not an
  exception: its source-snapshot seal, directory scan, layer-import scan, and `dump-edges` graph all route
  through `tier_model.iter_scannable_python_files`, which delegates traversal to the core walker and applies
  only caller-supplied `exclude_patterns` afterward — do not reintroduce a local `rglob` or exclusion tuple
  there. Authority-bearing operator verbs default `--root` to `src/elspeth`; widening that root must be an
  explicit operator choice, never something the CLI forces through a missing default. In the CLI, enumerate →
  apply every selected rule's `path_filter` → parse; reverting that order makes a narrow rule parse the entire
  repository. Do not turn `_path_matches_rule` from `re.search` into `fullmatch` as part of a walker change:
  shipped unanchored filters rely on substring semantics and require a separate audited migration.
  See [CONTRIBUTING: Gate walker and scratch directory](../../CONTRIBUTING.md#gate-walker-and-scratch-directory).

- **2026-08-29 — `sign_bundle_transaction._JUDGE_GATED_KINDS` is the ONE authority for "which action kinds spend a judge call"; price off it, never off a local copy** (elspeth-23ee8e3440)
  The MCP staging surface renders the operator's paste-ready `sign-bundle` command, and that command's cost is
  entirely a function of this set (`justify` + `drift_repair` reach `_run_justify`; `rotation` and
  `stale_delete` are mechanical YAML rewrites). Three call sites already read it, so a fourth private
  `frozenset({"justify", "drift_repair"})` would quote a price the transaction does not spend the moment the
  split moves. `mcp/server.py::_judge_calling_kinds()` imports it lazily for exactly this reason. Traps:
  1. A lane's price is not its size. The `resign` lane holds all three mechanical kinds *and* `drift_repair`, so
     a 366-action `resign` lane can cost 0 judge calls while a same-size `new_judgment` lane costs 366. Any "how
     expensive is this bundle" answer must count kinds, not actions.
  2. The renderer's `--lanes` values must come from the bundle's own `action.lane` (derived from `kind` by
     `BundleAction.__post_init__`), never a typed literal — that is what keeps them inside
     `cli._SIGN_BUNDLE_LANES` without importing the argparse module into the keyless MCP surface.
  3. Every rendered command carries `--dry-run`, so pasting one spends nothing. Word any cost annotation as what
     the scope costs *once `--dry-run` is dropped*; saying the command costs N judge calls is false.
  4. Testing it: assert on rendered VALUES (`--lanes new_judgment`), never the bare flag name — the response
     carries several commands and a substring check cannot discriminate. Pair every presence assertion with an
     absence control on the opposite bundle (single-lane vs mixed, at the threshold vs one over), and build
     `BundleAction` fixtures by direct construction: the masquerade gate scans tests, so parametrizing by
     attribute name and resolving with `getattr` turns the whole tree red.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-28 — in `sign-bundle`, a judged BLOCK is DURABLE STATE, not a return value: never use one as a generic "stop the run" mechanism** (elspeth-88d8b186f3)
  `run_sign_bundle_transaction` journals the BLOCK into the manifest's `blocked_actions` the moment the executor
  returns exit 1 from a judge-gated kind — in every mode, before it decides whether to keep firing — and
  recovery converts an interrupted verdict back from the authenticated `blocked_without_override` decision
  event. The action is then never judged again in that transaction; without the `--continue-on-block` opt-in the
  journalled BLOCK is terminal and a resume stops on it. Traps:
  1. A test that needs the first run to stop mid-way must fail an action with exit 2 (patch
     `cli._execute_new_judgment_action`), NOT with a BLOCK verdict — a BLOCK sticks, so the resume will not
     re-judge it and the old "block it, then resume with an accept-all judge" shape asserts a verdict-shopping
     outcome the engine deliberately refuses.
  2. Exit 1 means judged BLOCK only for `_JUDGE_GATED_KINDS` (`justify`, `drift_repair`); the deterministic
     executors return 0 or 2 only. Keep any new exit-code branch on that set rather than on the bare code.
  3. The `blocked_without_override` decision event only exists inside the shipped `enforce_*` allowlist layout,
     so a neutral-named fixture dir cannot exercise interrupted-verdict recovery at all.
  4. A `sign-bundle` exit code does NOT say whether the transaction is resumable, and `code != 0` as a proxy for
     it printed an infinite recovery loop. `_execute_sign_bundle` returns `_SignBundleOutcome(exit_code,
     resumable)` for that reason: exit 3 is reached only *after* the coherent publish, and exit 1 covers both a
     stopped-on-BLOCK transaction (resumable with `--continue-on-block`) and an all-blocked run that signed
     nothing (terminal). A terminal outcome offered the paste-ready `--resume` command, which reproduced the
     same code and guidance forever. Any new return site must answer `resumable` honestly rather than defaulting
     it, and the docs state the same rule (`docs/judge-signature-handoff.md`, the `judge-signature-workflow`
     skill) — update both with the code.
  5. Testing that guidance: `--resume` appears in stderr on the SUCCESS path too (`_run_sign_bundle` announces
     the freshly created transaction with "if interrupted, resume with"), so asserting on the flag name cannot
     discriminate and `_recovery_path()` matches that line as well. Assert on `_emit_sign_bundle_recovery`'s own
     text ("re-verify and resume with"), and pair any "no guidance" assertion with a control on an exit-2
     infrastructure failure — otherwise deleting the emit call outright passes.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-27 — the field-collision gates are CAPABILITY-KEYED: they arm only when `can_overwrite_input_fields(passes_through_input=, forwards_input_fields=)` (contracts/field_collision.py) is True — and `declared_output_fields` is an HONEST guarantee claim, never narrowed to keep a gate asleep** (elspeth-6ea3619737, elspeth-0d1da6dc44, elspeth-c84fa33f75)
  The three consumers move in lockstep: `TransformExecutor._run_preflight`, build-time
  `validate_transform_output_field_collisions` (core/dag/schema_validation.py), and composer Rule D
  (`_probe_transform_collision_surface` in web/composer/state.py — ONE probe construction returns fields plus
  capability; do not add a second probe). Traps:
  1. A hand-built transform fake or `add_node` call that declares colliding output fields MUST also model the
     presence flags truthfully (`passes_through_input=True` for the llm/enricher class) or the gate silently
     disarms and the test goes green for the wrong reason. The executor mock spec in
     `tests/unit/engine/test_executors.py` carries `forwards_input_fields`.
  2. The capability is INSTANCE-level on field_mapper and both explode transforms (`forwards_input_fields` is
     computed in `__init__`); the class attribute answers False and is not evidence.
  3. Do NOT key the exclusion on executor path: the reductive batch aggregators are unreachable only via
     AggregationExecutor routing, and arming path-wise breaks 12 plugins whose `group_by` field sits in both the
     required-input and declared-output sets.
  4. field_mapper's `_derive_declared_output_fields` reads `_promised_on_every_input_row`
     (`get_effective_guaranteed_fields() ∪ get_effective_required_fields()`) on BOTH branches — fixed-schema
     required fields AND `required_fields` now declare rename targets, the elspeth-0d1da6dc44 false-negative
     cure; the `required_fields` limb is sound ONLY because the build-time edge contract fail-closes against
     abstaining upstreams, so relaxing that Phase-1 strictness means revisiting the limb. Meanwhile
     `_build_field_mapper_output_schema_config` deliberately does NOT union `declared_output_fields` back in:
     the open branch's emit description still abstains (elspeth-c84fa33f75 — an emit-set claim there is a
     category error). `test_an_open_emit_set_is_a_strict_no_op` pins the emit half only.
  See [CONTRIBUTING: Convention: passes_through_input presence discipline](../../CONTRIBUTING.md#convention-passes_through_input-presence-discipline).

- **2026-08-27 — `source_data_contract` is the SIXTH InterpretationKind, its demand set is a DELTA-RUN of Stage-1's own edge-contract ledger, and the /validate remedy for the uploaded-source missing-field shape is the CARD, not the patch_source_options prose** (elspeth-da68332faf work item 2, elspeth-d39ec0c4d9, 083ec4b95)
  Traps for anyone touching interpretation kinds or source guarantees:
  1. A new InterpretationKind is a WIDE parity sweep: the contracts enum plus its exact-list test,
     `ck_interpretation_events_kind` in sessions/models.py (no Alembic — delete data/sessions.db), BOTH resolve
     dispatches in sessions/service.py (each ends in AssertionError→500), the `request_interpretation_review`
     tool enum (_dispatch.py) AND its pinned test, `_assert_affected_component` (tools/sessions.py), frontend
     `INTERPRETATION_KIND_VALUES` plus every compile-forced switch (tsc finds them: AcknowledgementCard,
     ChatInput's `pendingSubjectNoun`, executionStore's run-blocker copy) plus the exactly-N interpretation.test.ts
     pin, and the two `ids=[kind.value for kind in InterpretationKind ...]` parametrizations in
     test_request_interpretation_review_tool.py, which are a deliberate collection-time tripwire — exclude a kind
     there ONLY with a comment naming where its equivalent property is pinned.
  2. The demand backtrace (`web/composer/source_demand.py`) derives by re-running `CompositionState.validate()`
     on a hypothetically-stamped source and diffing per-edge `missing_fields` — never restate the guarantees
     walk. Any test state feeding it must be structurally clean enough for Stage-1 to REACH the edge-contract
     loop: two nodes both routing `on_error` to the same unknown sink name short-circuits validation with zero
     edge_contracts and reads as "no demand" (use `on_error="discard"`), and an llm node whose prompt references
     row fields needs `required_input_fields` declared (even `[]`) plus a `schema` block or the producer-side
     probe fails and the pass-through vote fail-closes to zero guarantees.
  3. `validate_pipeline_for_trained_operator` on (uploaded/path-bound observed source + downstream demand)
     reports `interpretation_review_pending` for the data-contract card BEFORE the edge-contract error — the
     elspeth-d39ec0c4d9 guarantee-repair advice no longer fires for card-ELIGIBLE sources on that path (still
     pinned at unit level and still live for card-ineligible shapes). Do not "fix" the ordering back.
  4. The card's draft is SERVER-COMPUTED (canonical JSON: demanded_fields + illustrative sample_header +
     missing_from_sample); the writer boundary recomputes it and rejects any planner-supplied field list. The
     resolved requirement's `accepted_artifact_hash` binds the FIELD SET
     (`source_data_contract_artifact_hash`), never the sample — drift re-opens the card via
     `_pending_source_data_contract_sites`, which is DERIVED per read (no mutation-time staging), so demand
     arising after bind blocks and re-asks with no staging hook.
  5. Known gap CLOSED (083ec4b95): `_backend_surface_args_for_site` (composer/service.py) carries the
     source_data_contract arm, so the kind-general settlement surfacer (freeform + guided wire-confirm) mints the
     card with the server-computed draft; the planner's `request_interpretation_review` is no longer the only
     minting path.
  6. MULTI-SOURCE FAN-IN IS ATTRIBUTED, not under-demanded (ruling on elspeth-da68332faf): queue/row_union
     fan-in is an AND over N INDEPENDENT per-source promises — every released row comes from exactly one arm, so
     a consumer requirement must be promised by EVERY feeding source, each for its own rows (Stage-1 already
     intersects arm votes). `backtraced_source_demand` runs a sufficiency∩necessity delta: H_all = every
     card-eligible source stamped with the baseline-missing fields, H_not_S = the same minus this source; a field
     is demanded iff the miss clears under H_all and not under H_not_S. A single eligible source reduces EXACTLY
     to the old solo delta (H_not_S is the baseline; no third validate() run). Consequences: (a) an INELIGIBLE
     source on the intersection (declared `schema.fields`, non-observed mode, or the composer-authored
     `source_authoring` marker) is never stamped, so the miss never clears and NO card demands the field — the
     shape fails closed with ordinary edge-contract advice, and that emptiness is deliberate; (b) BARE-observed
     arms make Stage-1 itself skip the fan-in edge (arm vote abstains → "Contract check skipped" warning, zero
     edge_contracts), so there is no ledger miss to attribute and no card, and runtime per-row enforcement owns
     that shape — the fan-in card fires when arms participate (each guarantees SOMETHING) but the intersection
     misses the requirement; (c) a resolved source's disregard-strip recompute re-derives its OWN demand even
     while a sibling is pending (necessity holds for its rows), which keeps its site closed with no re-ask loop.
     Pins: `TestFanInDemandAttribution` (test_source_demand.py), `TestFanInSiteLifecycle`
     (test_interpretation_state_source_data_contract.py).
  7. `SOURCE_AUTHORING_KEY`'s canonical definition moved to `web/composer/state.py` (beside `SourceSpec`, whose
     options carry it); `interpretation_state` re-exports it unchanged for its importers. `source_demand` reads
     it from state — do not re-import `interpretation_state` into `source_demand` (import cycle) and do not
     re-literal the string.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-27 — expand width is fenced at run (`settings.max_expand_group_width`, default 100k), and two conventions around it** (elspeth-258bd49d81)
  (a) The gate lives in the traversal's multi-row arm AHEAD of `expand_token` and routes the refusal through
  `handle_transform_error_status` — but the `error_sink` unpacked from `_execute_transform_with_retry` is None
  on a SUCCESS result (the executor only sets it on the error path), so an engine-synthesized refusal after a
  successful transform must pass `transform.on_error`, never the in-scope `error_sink`; reusing it builds a
  `(FAILURE, ON_ERROR_ROUTED)` RowResult with `sink_name=None`, which the RowResult invariant rejects.
  `TokenManager.expand_token` carries the same ceiling as a fail-closed backstop BEFORE any DB work (the mint is
  one eager transaction); callers without a loss channel (aggregation flush) hit that raise and the run fails
  closed rather than OOM. (b) Loss-ledger categories derive from ONE helper,
  `token_traversal._branch_loss_reason` — a new category that must stay explicit in
  `group_losses`/`coalesce_branch_losses` (like `expand_width_exceeded`,
  `retry_exhausted`→`max_retries_exceeded`) extends the helper, never the arms, which previously restated the
  mapping twice. `expand_width_exceeded` is also a `TransformErrorCategory` member — engine-synthesized like
  `retry_exhausted`; it is NOT a `GroupSettlementReason` (that closed enum is the coalesce/scope settlement
  vocabulary and stays untouched).
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-27 — server-owned option metadata is a THREE-surface parity set: planner projection, echo-tolerant write gates, and disclosure provenance** (elspeth-c67fbbbd83, elspeth-4496f61e30)
  The keys are `source_authoring`, `interpretation_requirements`, `prompt_template_parts`,
  `resolved_prompt_template_hash` — always derived from `AUTHORING_METADATA_OPTION_KEYS` /
  `_PROFILE_LOWERING_METADATA_OPTION_KEYS`, never restated. (a) The per-turn "Current pipeline state" context
  block and BOTH `plan_pipeline` `provider_current_state` call sites run
  `prompts.project_server_owned_option_metadata` between `to_dict()` and the storage-path redactor: three keys
  dropped, `interpretation_requirements` rows REDUCED to
  `PLANNER_CONTEXT_INTERPRETATION_REQUIREMENT_FIELDS` (id/kind/user_term/draft/status — resolved-vs-pending
  stays legible). NEVER strip inside `to_dict()` itself: it feeds `composition_content_hash` and pinned literals
  in `test_state_serialisation_contract.py`. (b) The reserved-key write gates are echo-tolerant:
  `_normalize_echoed_interpretation_requirements` (`tools/_common.py`) reduces a supplied row that
  `stable_hash`-matches a stored row — full row OR its context projection — to its `{kind, user_term, draft}`
  shell before the resolver-owned admission gate, and `_drop_echoed_source_authoring` (`tools/sources.py`) drops
  an exactly-matching `source_authoring` block before `_reject_manual_source_authoring`; both surface a
  `server_owned_metadata_note` advisory through `_mutation_result` data. Any non-matching value — one field off
  — still rejects (the elspeth-4496f61e30 forgery guard), and the auto-wired required-control disclosure row is
  never normalized. The shells round-trip through `reconcile_authoritative_reviews`, which is why
  `patch_source_options` now runs that reconciliation like `patch_node_options` always did — dropping the shell
  instead of reconciling silently downgrades a resolved review to pending. (c)
  `implicit_decisions._provenance_for_path` attributes TOP-LEVEL-rooted server-owned option paths as
  `server_stamped`. A NEW server-owned option key must join all three surfaces in one change; a lane that adds
  one and skips (b) recreates the one-turn planner trap this entry exists to close.
  Source defect: the next entry in docs/agents/recent-code-hints.md lost its headline; it survives only from
  the fragment beginning: plugin from authored options, and for a pass-through plugin that failure votes
  "participates with ZERO guarantees" — a permanent false reject, not a draft error. Its content, preserved
  here (elspeth-d4ae04b374, elspeth-bc527113e7, 5d48f67c0): a composer llm node never carries
  `provider`/`endpoint`/`api_key` (the operator profile injects them at LOWERING, after Stage 1), so the
  validation probe failed `provider: Field required` on every llm node ever authored, and
  `_effective_producer_vote`'s known-pass-through fail-closed arm rendered every downstream required-fields
  consumer `guarantees: [(none)]` — the elspeth-d4ae04b374 blocker, which two prior seats looked for in the
  coalesce merge (it reproduces on a LINEAR csv→llm→consumer edge; the coalesce only unions the zeros).
  Conventions from the fix (5d48f67c0): (a) `prepare_validation_probe_options(options, plugin=...)` — `plugin`
  is a REQUIRED keyword precisely so a new probe call site cannot silently opt out of the stub injection; pass
  the plugin name, `None` only when genuinely unknown. (b) The llm stub is provider `gateway` (logical-alias
  model, no local catalog to reject composer model names) with `required_capabilities` DERIVED from
  `GATEWAY_SUPPORTED_CAPABILITIES` — never restate the closed set. It is sound ONLY because the llm output
  contract is measured provider-independent (the provider instance is not built until `on_start`); before adding
  a stub for another operator-profiled plugin (elspeth-bc527113e7 tracks the roster), prove the same
  independence first. (c) An authored `provider` (YAML import) is left untouched — the stub fires only when
  `provider` is absent.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-27 — an LLM-AUTHORED blob-bound CSV source carries `schema.guaranteed_fields` STAMPED AT BIND TIME, and a graph fake needs the NAME-keyed transform map** (elspeth-da68332faf, elspeth-d39ec0c4d9)
  Two test-fixture traps from landing it, both of which redden green-looking suites elsewhere:
  1. EVIDENCE CLASS decides the stamp (the maintainer's ruling, 2026-08-27): auto-declare fires ONLY when
     `SOURCE_AUTHORING_KEY` is in the bound options — composer/LLM-authored blobs (invented_source flow), whose
     bytes are content-hash-bound to the run. An UPLOADED/verbatim/rebindable source's header is a SAMPLE and
     NEVER auto-declares (it feeds the ask-the-user interpretation card, elspeth-da68332faf work item 2); the
     guided inspection prefill deliberately has no guarantee fallback for the same reason. blob_rows binds are
     the one non-authored stamp: the plugin fabricates every row as exactly its five fixed custody fields
     (blob_rows.py load() row construction), so that claim is PLUGIN-CONTRACT truth, not sample inference
     (adjudicated). Do not extend the content stamp to uploaded/guided surfaces. A test that binds an
     LLM-AUTHORED CSV blob and pins the source options exactly sees
     `schema: {mode: observed, guaranteed_fields: [<canonical header keys>]}`; verbatim binds keep the bare
     observed block. The stamp is all-or-nothing on NAMES (partial guaranteed_fields is a complete-claim
     violation) and PER-ROW VERIFIED against the actual content, with the predicate DERIVED from the runtime
     authority (`verify_source_guaranteed_fields`, ADR-016: KEY presence on each VALID row; the csv source
     QUARANTINES ragged records — "expected N fields, got M" — so they never emit and cannot shrink the stamp; a
     `,,` record is a row of empty VALUES, which is data). Zero valid data rows = abstain entirely — never stamp
     an empty list, `()` participates and guarantees nothing. Also abstains on `columns`/`field_mapping`,
     `skip_rows`, author-written `guaranteed_fields`/`fields`, non-observed mode, non-UTF-8 `encoding`, and
     undeclarable or colliding headers. The quarantine-not-padding premise is pinned by
     `test_ragged_row_premise_quarantine_not_padding` (`test_promote_set_source_from_blob.py`) — if the csv
     source ever starts padding short rows, fix the DERIVATION to intersect emitted-row key sets, never the pin.
  2. `_edge_patch_targets_by_dag_id`'s transform arm reads `graph.get_transform_name_id_map()` (builder keys it
     on `wired.settings.name`, the composer node id) instead of zipping the positional
     `get_transform_id_map()` against state order. A hand-built graph fake that models only the positional map
     silently maps NO transforms — diagnostics degrade to "unmapped DAG node" with no error. Model both maps on
     the fake (`_EdgeSuggestionGraph` in `tests/unit/web/execution/test_validation.py` is the reference).
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-26 — `except IntegrityError: raise` must come BEFORE the `PayloadNotFoundError` arm, and a single-kind test cannot discriminate**
  The two payload-store errors are unrelated `Exception` siblings (`contracts/payload_store.py`), so a plain
  `IntegrityError` propagates under EITHER ordering — and even with the integrity clause deleted outright. A
  test raising one therefore passes against correct and inverted code alike; two of the three blob expanders
  shipped with the clauses inverted and a green suite. Only an error matching BOTH clauses discriminates,
  because `except` matches in source order: define
  `class _CorruptAndMissingError(IntegrityError, PayloadNotFoundError)` (its `__init__` must delegate to
  `PayloadNotFoundError.__init__`, which requires a non-empty `content_hash`) and assert it propagates. Pair it
  with a control asserting an ordinary missing blob still routes as `blob_not_found`, or a mutation that
  re-raises EVERYTHING passes the integrity test while destroying the quarantine path. Prove both: revert the
  ordering and ONLY the dual-kind test should die; re-raise everything and the control should die. Scope
  honestly — with today's classes this is LATENT, not live: no class in the tree is both kinds, `IntegrityError`
  and `PayloadNotFoundError` have no subclasses, and `FilesystemPayloadStore.retrieve` raises plain
  `IntegrityError` (`core/payload_store.py:286`), which propagates correctly even inverted. It arms the moment
  anyone relates the two classes or a store raises a subclass of both. Worth fixing and pinning, not a live
  row-killer; an earlier report overstated it and had to be corrected.
  See [CONTRIBUTING: Convention: test discipline (fakes, mocks, fixtures)](../../CONTRIBUTING.md#convention-test-discipline-fakes-mocks-fixtures).

- **2026-08-26 — the planner discovery digest carries FAR less than the plugin file does, so the obvious trim targets move a budget by exactly zero**
  A plugin's digest entry — what actually reaches the planner prompt — is five fields: `name`, `purpose`,
  `not_for`, `required_options`, `capability_tags`. `purpose` is the class docstring line and `not_for` is
  `usage_when_not_to_use`. NOT carried: `example_use`, `usage_when_to_use` verbatim, `composer_hints`, the knob
  schema — the four biggest strings in a typical plugin file. Measured 2026-08-26: digest 21639 bytes over 36
  transforms, median entry 389 bytes, largest 533 (`aws_textract_document_analysis`). So when a digest budget is
  over, only `purpose` and `not_for` are payable and they are small; if the numbers say a plugin cannot pay, the
  CEILING is the problem and it goes to a ruling rather than deforming a contract to fit. The ceiling inventory
  lives in `web/composer/planner_authoring_aids.py:949-1056` — `_SCHEMA_EVIDENCE` 96K, `_PLANNER_CONTRACT` 48K,
  `_DISCOVERY_DIGEST` 24K, its public-text cap 1K, `_MODEL_CATALOG` 32K, `_EXPRESSION_GRAMMAR` 8K. Two
  corollaries, each of which cost a wasted cut: (a) SOURCE-STRING BYTES ARE NOT BUDGET BYTES — the value travels
  through canonical JSON nested inside a larger serialization, so sizing a cut by `len(text.encode())`
  overestimates the saving (an estimated ~204-byte cut landed 69); measure the budget figure before and after on
  a scratch copy, never the string. (b) A BUDGET ASSERTION MAY NOT MEASURE THE PLUGIN AT ALL:
  `test_planner_authoring_aids.py:1291-1293` builds its contract list from a hardcoded
  `("web_scrape", "llm", "field_mapper")`, so a new plugin contributes nothing to it. Before assuming a red
  budget test belongs to a change, run the counterfactual — delete the new files from a copied tree and
  re-measure. Doing that showed the 48K overrun was pre-existing (`llm` alone is 39K of it) and unrelated to two
  newly added plugins.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-26 — a `*_field` option with a NON-NONE DEFAULT leaks its column name into `consumed_input_fields` on EVERY arm, including arms that never read it — and an arm-branched `declared_input_fields` is no protection** (elspeth-d6eeb3a71d)
  `_config_named_input_columns` (base.py:1105) reads the VALIDATED config, so it sees defaults the author never
  wrote, and it folds in every option `is_column_naming_config_option` (base.py:82 — `field`/`fields`/`group_by`
  or a `_field`/`_fields` suffix) accepts that is not listed in `output_naming_config_keys`. It is completely
  arm-unaware and BYPASSES the `declared_input_fields` property, so branching that property on a
  `source: blob|field` discriminator fixes nothing. It bites only when the leaked name collides with a field the
  plugin CREATES, because `base.py:1176` (`demote = self_created_input_fields - consumed_input_fields`) is the
  ONLY functional consumer in `src/` — everything else that mentions the property is a comment. When it does
  collide, the created field is never demoted, stays required on input, and every row is rejected for missing
  the field the transform exists to produce, from a config whose author never wrote the option at all. The shape
  that fixes it, now in all three blob expanders: default the option to `None`, add `read_<opt>` for the
  effective spelling (`self.<opt> or DEFAULT_<OPT>`), add `named_<opt>` returning `None` on the arm that does
  not read it, feed the arm-aware value through `declared_input_fields`, and read `read_<opt>` in `__init__`.
  Note the asymmetry: `None` keeps the name out of the DERIVED surface while `declared_input_fields` puts it
  back on the arm that really reads it, so the blob arm's requirement survives. This is INVISIBLE to inspection
  — find it by A/B-ing the contract surfaces against the real pre-change plugin:
  `git show HEAD:<path> > /tmp/old.py`, load it with `importlib.util.spec_from_file_location` so both classes
  live in one process, and diff `consumed_input_fields` / `self_created_input_fields` / `demoted_input_fields` /
  `declared_input_fields` / `declared_output_fields` / the `input_schema` required-flag map across a matrix of
  configs and payloads. Forty config x payload pairs took minutes to run and are the only reason the blob arm
  could be shown unchanged while the inline arm's leak closed.
  See [CONTRIBUTING: Convention: passes_through_input presence discipline](../../CONTRIBUTING.md#convention-passes_through_input-presence-discipline).

- **2026-08-26 — `_reject_input_options_naming_created_fields` must be called at the END of `__init__`, and needs a config-time twin**
  It READS `self.self_created_input_fields` rather than capturing it (base.py:984), so a call placed before
  `declared_output_fields` is populated sees an empty set and passes vacuously — a guard that looks correct and
  does nothing. Two further traps. (a) `validate_transform_config` never CONSTRUCTS the transform, so a guard
  living only in `__init__` makes pre-validation report a config valid that the engine then rejects; that is
  exactly the divergence `tests/unit/plugins/test_validation_path_agreement.py` polices, and it only catches the divergence
  if a rejection case exists for the plugin. Pair the call with a `@model_validator` over the created set that
  IS knowable at config time, and DERIVE that set from the same function that builds the output fields rather
  than restating it — `pdf_rasterize` is the reference shape (`_reject_field_name_collisions` at :247 alongside
  the `__init__` call at :434). (b) A test that calls the helper directly on an instance proves the HELPER
  works, not that it is WIRED IN; the `__init__` call stays silently deletable and a mutation that removes it
  survives. Prove the wiring instead by patching `self_created_input_fields` to a name no by-name validator
  inspects — `patch.object(Cls, "self_created_input_fields", new_callable=PropertyMock)` — and asserting
  construction is still refused, with a control showing the same config constructs unpatched. The general form,
  worth carrying past this guard: a coverage sweep that exercises one representative member of a set is shielded
  by whatever sorts first, so a guarded scalar can hide every unguarded member behind it —
  `tests/invariants/test_input_options_do_not_name_created_fields.py` tested only `sorted(created)[0]` and has
  since been widened for that reason (see its own note at :76).
  See [CONTRIBUTING: Convention: test discipline (fakes, mocks, fixtures)](../../CONTRIBUTING.md#convention-test-discipline-fakes-mocks-fixtures).

- **2026-08-26 — ZERO ROWS IS A FAIL STATE, and an empty ROW is not an empty RESULT**
  Ruling from the maintainer; it reverses a call that flip-flopped twice before settling, so check this entry
  rather than trusting a sibling plugin read first. Nothing downstream can consume zero rows, so a transform
  that produces none has failed to produce data and must return `TransformResult.error({...}, retryable=False)`
  so the row leaves through `on_error`. Reuse an existing `TransformErrorCategory` literal; the name does not
  matter. The distinction that matters is between an empty VALUE and an empty RESULT: a CSV row spelled `,,,` is
  a row whose values are empty and is perfectly good data, and so is the empty string between two newlines —
  both must be EMITTED. Only a container that yields no row at all is the failure. Two mistakes follow from
  getting that backwards: filtering blank values away as if they were not rows, and synthesising a blank row to
  rescue a genuinely empty container. Do neither. The pair worth testing is one input under two configs —
  `"\n\n\n"` with `skip_blank_lines: false` emits three empty-valued rows, and the SAME bytes with
  `skip_blank_lines: true` drop them all and must error. `blob_csv_expand`'s `empty_csv` was right all along;
  `blob_json_expand` and `blob_text_expand` were brought back into line. The mechanism note this replaces still
  holds, but ONLY for genuine filters: `can_drop_rows = True` + `TransformResult.success_empty()` is the sole
  legal zero-emission shape (`success_multi([])` is invalid outright, `contracts/results.py:424`, and
  `engine/executors/can_drop_rows.py` raises unless BOTH `passes_through_input` and `can_drop_rows` are
  declared), and `record_empty_expansion` (`token_traversal.py:210`, gated on `creates_tokens`) mints the
  durable `member_count=0` group record that a bound `require_all` empty-group failure needs. That machinery
  exists for row FILTERS and bound-group openers. "The container I was handed was empty" is NOT a legitimate use
  of it — an expander reaching for `can_drop_rows` to make an empty document look successful is the error this
  entry exists to prevent.
  See [CONTRIBUTING: Convention: passes_through_input presence discipline](../../CONTRIBUTING.md#convention-passes_through_input-presence-discipline).

- **2026-08-26 — under concurrent edit, a single read of a file is not evidence; hash before AND after every measurement**
  Sibling agents edit plugin files while they are under review. During one read-only review
  `blob_json_expand.py` moved four times, and a scratchpad snapshot taken along the way contained two things
  that never reached the settled file: a `json.dumps(...)` stringification of nested values in the record
  projection, and a `@model_validator` named `_unused_*`. Reporting either would have been a confident false
  finding against a live author. So: stamp every measurement with `sha256sum` at both ends and discard any
  result whose brackets differ, re-read before accusing, and when another agent's module must be mutated to test
  it, patch the class attribute at runtime from a scratchpad pytest plugin (`-p my_mutant` with a
  `pytest_configure` that sets the attribute) rather than writing to their file at all.
  See [CONTRIBUTING: Convention: repository and process hygiene](../../CONTRIBUTING.md#convention-repository-and-process-hygiene).
- **2026-08-26 — a green measurement is not a result until the apparatus is known to have run** (elspeth-623c69c59f, elspeth-8783933d99, 3f18a95c6, ef5e6e593, b06c5f6dc, fe8b0cc4c)
  Five independent instances in one afternoon across four sessions; every one produced a confident, wrong, reassuring answer. Verify the apparatus before believing the reading:
  1. A dirty shared checkout actively hides defects. elspeth-623c69c59f's test reproduces ONLY at pristine HEAD; in the shared checkout it PASSES, because an uncommitted fix in the tree masks it. A pass in the shared checkout is not a pass at HEAD whenever `git status` is non-empty on any file feeding the test. Measure on a clean `git archive <sha> | tar -x` export with `PYTHONPATH=<export>/src`, and assert `elspeth.__file__` resolves into the export.
  2. Prose is not provenance. The same file carried a docstring describing the fix in the past tense for a fix that had never been committed; it convinced two sessions hours apart. Run `git log -S"<symbol>"` before believing a docstring's past tense.
  3. A zero can be a crash. A trust-tier A/B exported with `git archive HEAD src` omitted `config/`, so `elspeth-lints` crashed on the missing allowlist and reported "0 findings" on BOTH passes. That gate exits 1 with a large known corpus by design: read it as a before/after COUNT over identity sets (line numbers stripped), never as pass/fail, and never trust a zero you did not positively cause.
  4. A harness that never loaded the mutant reports every mutation as caught. "All seven mutations passing" meant the mutant was never imported. A mutation check must first prove the mutant is LIVE by showing it fails something.
  5. Agreement between two hand-written sets reads as corroboration and is not. A node-kind partition comment inventoried its sibling authorities, listed two, and there were three — the missed one a byte-identical tuple in the same feature. Fixed at `3f18a95c6`: the tuple now derives from `chat_solver.PLUGIN_FREE_NODE_TYPES`, mutation-verified by dropping `queue` and watching the ROUTES module fail at import. Only derivation from the single authority is evidence.
  6. Structural conformance is measured, not declared. Widening a `runtime_checkable` Protocol silently reclassifies every implementation tree-wide with no import-time signal (`ef5e6e593` -> `TypeError: Unknown transform type` from the ENGINE at `token_traversal.py:1091`, elspeth-8783933d99). ADR-032 already forbids such a Protocol as a SECURITY control; this is the same mechanism as a DISPATCH control. Dispatch on an owned base class or explicit registration. The hardening landed (elspeth-8783933d99, ADR-032 addendum 2026-08-27): engine dispatch keys nominally on GateSettings in negative form over the closed node_to_plugin / config.transforms containers.
  7. A rebuild-and-compare check passes hardest when it did nothing. Twice in one lane `git add` failed silently on an index lock, so the index still equalled HEAD, the generated patch was 0 bytes, and `cmp` printed MATCH. Such a procedure must assert its own preconditions before comparing: the patch must be non-empty, and the index must differ from HEAD. Re-verify against the CURRENT HEAD too — HEAD can move while you wait on a lock.
  8. Removing a value can blind a nearby assertion while the test stays green. A guided fixture carried `{"tier": "'high'"}` in the predecessor and `{"tier": "'priority'"}` in the replanned candidate; repairing the incoherent pair out of both sides left the two mappings identical, so the re-keyed `mapping == {"amount": "amount"}` passed whether the binder carried the replan through or restored the predecessor wholesale. When a repair removes a value, check whether that value was the only thing making a nearby assertion discriminate; prefer the whole-object assertion (fixed at `b06c5f6dc`; the vacuous form shipped in `fe8b0cc4c`).
  The unifying rule: confirmations are where this happens, because the search is for agreement rather than for a result. Prefer stating "not traced" over a green that cannot be accounted for.
  See [CONTRIBUTING: Why a green scoped run proves nothing](../../CONTRIBUTING.md#why-a-green-scoped-run-proves-nothing).

- **2026-08-26 — editing any plugin source file moves frozen corpus bytes; a whole-tree trap with no local symptom** (elspeth-e6e552ce34)
  Every plugin declares a `source_file_hash` line, the node audit record carries that byte, and `docs/architecture/dag/scenario-corpus/v1/manifest.yaml` pins the audit records LITERALLY. A one-line edit under `src/elspeth/plugins/` — even a pure declaration such as adding a class attribute — bumps its hash and turns the DAG scenario corpus red, with nothing in the plugin's own suite to warn. elspeth-e6e552ce34 cost 32 reds in `tests/integration/core/dag` this way (csv_source + passthrough).
  1. A plugin edit's verification selection MUST include `tests/integration/core/dag` AND `tests/unit/architecture`; a green `tests/unit/plugins` run certifies nothing here.
  2. Recompute the hash with `scripts/cicd/plugin_hash.py::compute_source_file_hash` (the hash line self-normalizes, so it is not self-referential) and confirm the declared value matches before blaming the corpus.
  3. Re-pin IN DEPENDENCY ORDER, because each step feeds the next: first the manifest's literal `source_file_hash` bytes (they appear in BOTH plain `"..."` and JSON-escaped `\"...\"` forms, so grep the bare 16-hex token, not the key), then any `resumed_full_projection_sha256`, then `tests/unit/architecture/test_dag_scenario_corpus_contract.py::
     EXPECTED_CASE_REGISTRY_SHA256` (it hashes every case's full `model_dump`, manifest oracle values included). Re-pinning out of order moves the later digests twice.
  This is a manifest BYTE CORRECTION, not an oracle re-freeze: the oracle-freeze surface excludes `audit_records`, so no snapshot under `tests/fixtures/dag_scenario_corpus/oracle_freeze/` should move — if one does, semantics changed and the META-39/META-41 ruling path applies, not a re-pin. Prove confinement positively: export HEAD, revert ONLY the hash byte(s), and show the suite green except for reds equally red at the pre-series base.
  See [CONTRIBUTING: Gate: plugin inventories, source hashes, scenario-corpus manifest, fingerprint baseline](../../CONTRIBUTING.md#gate-plugin-inventories-source-hashes-scenario-corpus-manifest-fingerprint-baseline).

- **2026-08-26 — `getattr(cls, "input_schema")` on a plugin CLASS measures nothing and returns a confident wrong answer** (elspeth-d6eeb3a71d)
  `BaseTransform.__init_subclass__` (base.py ~:589) MOVES a class-body `input_schema = SomeModel` into `cls._declared_input_schema` and `delattr`s the class attribute, so the property always wins the MRO. This is deliberate: it stops an undemoted model reintroducing elspeth-d6eeb3a71d. Consequence for plugin audits: `getattr(cls, "input_schema", None).model_fields` is None for EVERY plugin, so a census reports zero and looks like a finding rather than a failure to look. `inspect.getattr_static` does not help either — it returns the property. Read `cls._declared_input_schema`, and always run a positive control (for example `tests/fixtures/dag_scenario_corpus/plugins.py::
  CorpusEOFBatchSumTransform`, which declares `input_schema = CorpusInputSchema`); if the control also comes back empty, the probe is broken, not the corpus. Measured correctly: zero builtin plugins carry a code-declared `input_schema`; every consumer schema comes from the authored `schema:` block.
  See [CONTRIBUTING: Gate: attribute contracts](../../CONTRIBUTING.md#gate-attribute-contracts-dynamic-attribute-sites).

- **2026-08-26 — two new plugin contract facts, and the rule that a PRESENCE flag never implies a VALUE promise** (elspeth-48aeea6ad9, elspeth-8783933d99)
  `preserves_input_values` (TRANSFORM, `BaseTransform`/`TransformProtocol`) says forwarded values are never rewritten; `observed_value_type` (SOURCE, `BaseSource`/`SourceProtocol`) gives the structural cell type an observed source emits (`csv` = `"str"`, the only declarer today). Both are DISTINCT from `passes_through_input`/`forwards_input_fields`, which promise only that the FIELD survives and say nothing about its value — which is why `resolve_guaranteed_field_type` used to abstain at every observed pass-through. Threading covers every plugin-bearing kind (updated 2026-08-27, elspeth-48aeea6ad9): `builder.py` reads `preserves_input_values` at the TRANSFORM, AGGREGATION and COLLECTOR `add_node` sites (and `observed_value_type` at the source site), and the walk's abstention guard gates AGGREGATION/COLLECTOR pass-throughs on the same promise WITHOUT the TRANSFORM arm's `config.fields` declaration-discipline escape hatch (aggregation output is dynamic by design — do not flatten the asymmetry).
  1. `builder.py` reads both attributes DIRECTLY, so every protocol-modeling fake in `tests/` needs them; omitting one is an `AttributeError` at build, not a skip (~14 test files had to model the contract). Add them to the fake rather than weakening the builder's read (ADR-032: nominal typing for what we own). Second, quieter failure mode: `TransformProtocol` is `@runtime_checkable`, so a fake reaching an `isinstance` site fails STRUCTURAL CONFORMANCE instead of raising, and the message names neither the protocol nor the missing attribute — `test_protocols.py` reports only "Must conform to TransformProtocol". (token_traversal's "Unknown transform type" variant is GONE — engine dispatch is nominal on GateSettings since elspeth-8783933d99.) When adding a protocol attribute, grep for fakes that assign the sibling flags in `__init__` (`self.passes_through_input = ...`) as well as in the class body; an AST sweep over class bodies alone misses them.
  2. A new declarer of either fact is a soundness argument, not a config change: the structural source arm answers only for fields in the source's OWN `guaranteed_fields`, which keeps over-recursion self-limiting for fields introduced mid-path.
  See [CONTRIBUTING: Convention: passes_through_input presence discipline](../../CONTRIBUTING.md#convention-passes_through_input-presence-discipline).

- **2026-08-26 — structured output is a THREE-surface parity set: per-query (multi-query), config top-level (single-prompt transform), and the LLM source**
  One shared lowering/extraction, four traps. The `response_format`/`output_fields` pair exists top-level on `LLMConfig` AND `LLMSourceConfig` (rejected when `queries` is set; structured without output_fields rejected; suffix collisions with `response_field` plus operational names rejected).
  1. The json_schema lowering and the Tier-3 parse/validate live ONLY in `transforms/llm/validation.py` (`build_structured_response_directive` / `extract_structured_fields`); all three surfaces call them. Do not re-inline a fourth copy. `extract_structured_fields` returns `({}, error)` on failure, never `(None, ...)`.
  2. A new single-request field on `LLMConfig` propagates by TEST to the source configs (`test_source_provider_schema_stays_in_parity_
     with_transform_single_request_fields`) and must be filed in `test_gateway_config.py`'s `_DELIBERATELY_PUBLIC_FIELDS` or `_LLM_PRIVATE_OPTIONS`; the field is red on three suites until all three homes are decided.
  3. The gateway json_schema capability check has a single-prompt arm (`validate_gateway_single_prompt_structured_output_capability`) on BOTH `GatewayConfig` and `GatewayLLMSourceConfig`. The old comment "single-query mode can never reach structured output" is gone — do not reintroduce a queries-only guard.
  4. The web catalog goldens (`tests/golden/web/catalog/{knob_schema,policy_view}/{transform,source}__llm.json`) pin the config schema bytes: any LLM config field change is a golden re-record to adjudicate. `policy_view` is written with `json.dumps(..., indent=2, sort_keys=True)`; omitting sort_keys makes the diff 700 lines of noise.
  See [CONTRIBUTING: Gate: declared oracles pin output bytes](../../CONTRIBUTING.md#gate-declared-oracles-pin-output-bytes).

- **2026-08-26 — a row_union-released token's resume authority is the SCHEDULER JOURNAL; the workset's mint-frame projection is a known-divergent artifact** (elspeth-54edda5699)
  Pinned by `tests/e2e/recovery/test_row_union_released_token_resume.py`. `get_resume_workset` rebuilds `IncompleteTokenSpec.lineage_path` from MINT frames, so a released token's spec silently regains the FORK frame the union popped; dispatching it would re-run the whole branch into a released barrier (measured under mutation: the scheduler's incompatible-work-item guard fail-closes at the union, attempt 1). The healer is `run_resume_processing_loop`'s drain-first precedence: journal rows carry the popped `lineage_path_json` byte-exactly, and after `drain_scheduled_work` the row-replay set is DISCARDED (`unprocessed_rows = ()`), with mixed journal coverage refused as AuditIntegrityError. Do not "fix" the workset projection to re-pop frames, and do not weaken the drain-first / coverage-refusal pair; the pin test kills either mutation.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-26 — the guided deferral surface is PLURAL end to end: one resolution plus 1..K retains per reply** (elspeth-3a21f09f09)
  The R2-F15 pair (one resolve + one retain) is generalized to a GROUP at both solver sites (`chat_solver.py` step-1/step-2). `GUIDED_MAX_DEFERRED_RETAINS_PER_REPLY` (8) caps one reply's retain calls (breach = shape error into the existing clarification-retention net); the durable bound stays `GUIDED_MAX_DEFERRED_INTENTS` (256) at settlement.
  1. The renames are TOTAL, no compat shims: `GuidedChat*Outcome.action`->`actions`, `Step{1,2}*Resolved*.deferred_action`->`deferred_actions`, `DeferredRequestRetained.retained_intent_id`->`retained_intent_ids`, `DeferredRequestAuthority.new_intent_id`->`new_intent_ids`, `GuidedStateOperationCommand.retained_deferred_intent_id`->`retained_deferred_intent_ids: tuple[UUID, ...] = ()` (absent = EMPTY TUPLE, never None). Constructing any of them with the singular name is a TypeError.
  2. `manage_deferred_intent` stays SINGULAR by design; a multi-call reply containing it is still a shape rejection. Do not fold it into the group.
  3. `apply_deferred_request` FOLDS N actions against the EVOLVING guided state (`_apply_one_deferred_action`), so action 2 can legitimately contradict action 1 retained in the same Send; the composed chat takes the FIRST non-success disposition's status/error_class.
  4. Settlement custody (`service.py::_verify_guided_deferred_intent_append`) verifies K ordered appends whose ids match the claimed tuple EXACTLY; a set/count comparison is a mutation the wrong-order test kills.
  5. The repair thread answers EVERY call id with per-call errors (`rejected_calls=`/`errors=` aligned tuples). Step-3/wire chats never offer `retain_deferred_intent`, so there is deliberately no third solver site to sweep.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-26 — §7 rule 5's fork closer kinds are NOT interchangeable: a ROW_UNION-bound fork inside any bound region is a build-time rejection** (elspeth-9db785ace7)
  `core/dag/bound_regions.py::validate_openers_bound_in_region`. A row_union is a pass-through closer (ruling 27, `pop_fork_frame`): it releases the ORIGINAL branch tokens, so an enclosing region's member presents branch-count tokens and rule 5's one-token-per-member certification is false. The runtime symptom was the collector's Tier-1 duplicate-member `AuditIntegrityError` calling itself build-time impossible. Only a COALESCE (merging, `truncate_at_closer_frame`, one successor per group) may close an in-region fork.
  1. The spec's §7 rule 5 prose ("forks close at an in-region coalesce/row_union — as before", 2026-08-21-barrier-scopes-full-nesting-spec.md) still declares the kinds interchangeable; the code is ruled right and the spec sentence is stale. Do not "fix" the code back to match it.
  2. Through `build_execution_graph` only the COLLECTOR enclosure reaches the new limb: a fork nested in a row_union branch, and a barrier downstream of a row_union, are pre-empted by older builder guards (builder.py group-indivisibility + nested-fork walks). The limb stays enclosing-kind-agnostic as the backstop, and the raw `validate_openers_bound_in_region` invocation is how tests pin the coalesce/row_union enclosures.
  3. The runtime duplicate-arrival guards (collector.py, coalesce_executor.py, row_union_executor.py) are deliberate fail-closed defenses; never weaken them to tolerate this shape.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-25 — the guided collector guard is LIFTED (WS6 lane 2, ruling 7878); `guided_collector_not_authorable` is RETIRED**
  The guided projection now carries a closed collector behavior arm. Conventions from the parity sweep:
  1. The retired code is gone from `generation.py`'s `_CLOSED_VALIDATION_ERROR_CODES` AND from `_VALIDATION_ERROR_PATTERNS` (disjoint paths; retiring one leaves the planner served advice for a rejection that can no longer fire). Do not reintroduce the code or a binder-level collector refusal; collector defects are ordinary Stage-1 validation errors now.
  2. The proposal projection's collector behavior is `{"kind": "collector", "opener_stable_id": <the OPENER's projection stable id>, "policy": "require_all"|"best_effort"}`. `scope_name` is DELIBERATELY private. The opener is resolved to a stable id at `_build_projection`'s dispatch, and `_node_behavior` raises typed on an unresolvable opener.
  3. A collector's `on_error` is OPTIONAL on the wire (`validate_payload` accepts one `node_success` plus AT MOST one `node_error`) because group-failure handling is structural (ADR-042 §6). Do not "fix" it to the transform/aggregation exact-two-flows rule, and do not extend the on_error->"discard" default at `_canonical_state_from_private_pipeline` or the freeform builder to collectors: an omitted collector on_error must STAY None.
  4. Frontend: `ProposalNodeBehavior` in `types/guided.ts` gained the collector arm; the two behavior renderers (`behaviorSummary` in `ProposePipelineTurn.tsx`, `behaviorDetails` in `WireStageTurn.tsx`) are compile-forced switches, so extending the union without both arms fails `tsc`. The decoder (`guidedDecoder.ts`) mirrors `validate_payload` including the collector flow/opener reconciliation.
  5. `scope_on_group_failure` decode/type residuals are deleted from `guidedDecoder.ts` and `types/index.ts`; the wire never carries the field again, and old persisted states never carried it either (it serialised omitted-when-None).
  6. A new planner-authorable REFERENCE field must join the guided binder's dangling-reference coverage. The binder's `_collect_dangling` walk (behind `guided_route_target_unknown`) covers on_success/on_error/to_node and does NOT discover new reference fields by itself, so `scope_opener` shipped uncovered. The fix is a sibling referential check (`guided_collector_opener_unresolved`) with connectivity facts; the projection raise stays as the fail-closed last resort. Two sub-traps: the closed catalogue is CONTAINMENT-FREE (no code may be a substring of another — `guided_scope_opener_unknown` was rejected by `test_codes_are_containment_free` because Stage-1's `scope_opener_unknown` exists), and binder existence vs Stage-1 kind semantics stay split (the binder checks the id RESOLVES; validation's `scope_opener_unknown` owns opener-must-be-a-transform), both repairable in the planner loop (`build_set_pipeline_candidate` -> `acceptable` gate -> `_PipelineCandidateRejected`).
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-25 — LLM messages are `Sequence[ChatMessage]`; `wire_messages`/`audit_messages` are the ONLY two exits, and image bytes must never reach audit, tracing, logs, or exception text**
  `contracts/chat_parts.py`, llm image-input feature. One owned type replaces the old `list[dict[str, str]]` provider seam: `ChatMessage(role, content)` where `content` is a plain `str` for text-only messages (byte-identical audit behavior to the pre-image tree) or a non-empty `tuple[TextPart | ImagePart, ...]` for a multimodal message. `ImagePart` is constructed via `ImagePart.from_bytes(format=, data=, blob_ref=)` — never the bare dataclass constructor outside tests — and `__post_init__` re-asserts every invariant (byte-signature match via `binary_documents.binary_document_signature_matches`, `sha256`/`byte_count` agreement) so a hand-built instance cannot lie. Two projections are the only sanctioned way out of the module: `wire_messages()` (OpenAI content-parts dialect, base64 image data URLs, provider ONLY) and `audit_messages()` (each `ImagePart` reduced to `ImagePart.audit_view()`: format/sha256/byte_count/blob_ref, never `data`) for `LLMCallRequest` recording. `parts_hash()` is the one other reader of raw bytes (order-sensitive SHA-256 over `audit_view()`s, used for the multi-query `parts_hash` audited on each call); do not add a third. The type lives in `contracts/chat_parts.py` rather than beside the LLM transform because `AuditedLLMClient` (`plugins/infrastructure/clients`) cannot import the transform-layer serializer — a deliberate one-layer-lower placement vs. the originating spec's prose. New code touching message content must go through `wire_messages`/`audit_messages`; never hand-serialize a `ChatMessage`, and never let `ImagePart.data` reach anything that is not the wire projection.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-25 — the death-matrix harness hosts a LIVE-leader + follower composition, and "multi-worker collector test" names TWO different items**
  Integration phase 1b, `tests/e2e/recovery/test_barrier_process_death_matrix.py` collector family.
  1. `spawn_database_process_with_pause` + `release()` (not `kill()`) keeps the opener's LEADER alive and paused while a second child — a real `ProcessorMode.FOLLOWER` processor built via the new `_make_processor(mode=ProcessorMode.FOLLOWER, scheduler_lease_owner=...)` kwarg (no coordination token; the owner must be a registered `run_workers` row) — writes to the same DB; the released leader then continues its action. That is how a follower-reported collector loss (`adopted_epoch NULL`, re-derived via META-9.1) is replayed by a leader whose in-memory registry DID mint the group.
  2. Do not conflate the worklist's item 12 with item 13. Item 12 is the multi-worker CONCURRENT-ADOPTION RACE (two processes racing the leader-fenced CAS on one BLOCKED row), which needs two simultaneous leader fences — the Postgres multi-worker suite's composition, `test_barrier_recovery_postgres.py`; this single-seat SQLite harness's second leader is always a takeover of an expired seat. Item 13 is the non-opener-worker / post-resume LOSS pair, which the harness hosts fully.
  3. A scoped run on the shared checkout measures sibling WIP in `src/`: the family went red mid-session on a half-edited `processor.py` (missing import) that was in no commit. Verify from a `git archive HEAD` export with your test files copied in, and read the sibling's `git status` before diagnosing.
  See [CONTRIBUTING: Convention: test discipline](../../CONTRIBUTING.md#convention-test-discipline-fakes-mocks-fixtures).

- **2026-08-25 — META-38: merging closers TRUNCATE at their own frame; `pop_closer_frame` is GONE** (branch feature/unified-lineage)
  A collector release permanently carries its own release-group EXPAND frame innermost (`collect_tokens` mints `(EXPAND, release_group_id, own token_id)`), so "the closer's frame is `path[-1]`" is false for every closer downstream of a collector.
  1. Every MERGING closer (coalesce, collector, the settle seam's consumed-token pop) calls `contracts.identity.truncate_at_closer_frame(path, kind=, group_id=, is_release_group=)` — scans innermost->outward for its frame, returns `path[:index]`, and RAISES if any frame above the match is not a collector release group. Never re-implement the pop inline; never index `[-1]`.
  2. The release fact is WRITTEN, `group_records.closes_group_id` (non-NULL only on release groups); the ONE predicate is `data_flow/tokens.py::is_release_group(conn, run_id, group_id)` (missing row -> `AuditIntegrityError`), reached from the engine through `TokenManager.is_release_group` (per-run memo filled ONLY by that durable read — never seed it from a `CommittedCollect`). Do not derive release-ness from lineage shape, member_count, terminal paths, or bindings.
  3. The closer's group id is the CALLER's fact: `_record_group_member_terminals` takes a required `group_id`; the coalesce mints anchor on `innermost_fork_frame` (a search); `_note_coalesce_group_failed_from_token` SEARCHES for the FORK frame like its row_union twin. Never re-derive a closer's group from `path[-1]`.
  4. Pass-through closers (row_union, `pop_fork_frame`) are unchanged; they preserve every other frame.
  5. Crafted release tokens in tests must be minted through the real `collect_tokens` so the written fact exists. A synthesized release frame makes `is_release_group` fail closed, and the fix is the fixture, never the predicate.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-25 — resume has a THIRD entry guard (group satisfiability), and two traps around it** (WS5 Tasks 1/2, spec §8)
  1. Both resume surfaces — advisory `RecoveryManager.can_resume` and enforcing `ResumeCoordinator.resume()` — call `check_group_satisfiability_resumable(db, run_id, group_binding_view_from_graph(graph))` before any mutation, so EVERY graph object reaching them must answer `get_group_bindings()`. Set `get_group_bindings.return_value = GroupBindingRegistry(bindings=())` on a `MagicMock(spec=ExecutionGraph)` stub even though it "works" bare: measured, a bare spec mock's `.bindings` IS iterable (MagicMock's magic `__iter__` yields empty), so such tests reach the gate and pass VACUOUSLY — the right outcome for zero bound groups by the wrong mechanism, and a future gate arm reading anything a mock answers non-emptily flips silently. A bare-`object()` or `MagicMock(spec=object)` graph fails with AttributeError. Model the contract on the FAKE, never weaken the guard.
  2. The WS5 plan's third-sibling construction ("best_effort defers the merge past load") is FALSE against the live engine: best_effort ARRIVAL merges the moment every branch is accounted for, so a plain fork resolves in-row while the source is still `loading` and the lifecycle gate masks the group gate. The measured construction that yields exhausted-source + checkpoint + open bound group is an EOF-triggered aggregation UPSTREAM of the fork (see `_run_fork_coalesce_to_eof_flush_crash` in `tests/integration/audit/test_contract_violation_token_outcomes.py`); the crash path already terminalizes the consumed branches, so an adversarial image writes NO second terminal (unique index) — it removes settlement evidence instead.
  Shared group/lineage raw-seed helpers live in `tests/fixtures/group_lineage.py`; extend there, not per-file. The ADR-038 abandonment sweep deliberately does NOT mirror this arm (ADR-038 §3a) — do not "complete" the symmetry.
  See [CONTRIBUTING: Convention: test discipline](../../CONTRIBUTING.md#convention-test-discipline-fakes-mocks-fixtures).

- **2026-08-25 — group-settlement reasons are a CLOSED StrEnum (coalesce / scope closers only), and the merged-vs-failed discriminator is RELEASE status, not completion** (ADR-042, unified-lineage WS6 Task 6)
  Every coalesce/collector settlement reason (`late_arrival_after_merge`, `scope_group_failed`, `empty_expansion`, `all_members_lost`) comes from `contracts.enums.GroupSettlementReason`; never write the string at an emission site. `tests/unit/engine/test_group_settlement_reasons.py` AST-scans all of `src/` for the literals and is red on the first one. row_union's `row_union_branch_lost` / `late_arrival_after_release` / `row_union_group_failed` are a SIBLING vocabulary by ruling (META-9.3) and stay outside the enum; do not fold them, and `row_union_group_failed` is NOT the settlement channel's `group_failed=` flag. A group that closed by FAILURE has `completed_at` set on its closer node_states just like a merge does; only `status == COMPLETED` discriminates, which is why `has_released_group_for_node` / `get_released_group_ids_for_nodes` exist beside the plain-completion reads — do not "simplify" them onto `completed_at`. `CoalesceExecutor._completed_keys` carries the flavor (`True` merged / `False` failed / `None` unknown -> Landscape point lookup); restore seeding is ALWAYS `None`, never merged. A late-arrival `CoalesceOutcome` without a `failure_reason` raises in the intake coordinator instead of defaulting, so a test double returning `late_arrival=True` must set the reason. Test-double trap: the coalesce executor tests' `_restore_reads_from_execution_double` is a deliberately narrow `SimpleNamespace`, so any NEW read the executor makes on `_barrier_restore_reads` must be bound there (autospec'd) or every late-arrival test fails with `AttributeError`.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-24 — WS2 config-validation campaign conventions (barrier scopes: collectors, bound regions, parity)** (709e4abb3, elspeth-88bb77953c; branch feature/unified-lineage)
  Eight traps; the first four from the campaign plan, the rest measured while landing Tasks 12-13.
  1. (a) The scope-binding config keys (`scope_name`/`scope_opener`/`scope_policy`; `scope_on_group_failure` existed then but is DELETED as of 709e4abb3 — ADR-042 §6, group-failure handling is structural) exist ONLY on collector nodes and serialise omitted-when-None. Adding any new key to an EXISTING node type's serialisation moves every canonical topology hash in `tests/unit/core/dag/canonical_hash_corpus.json` and every stored `composition_content_hash`: a new key must be omitted-when-absent or it is a corpus-wide re-record to adjudicate.
  2. (b) Bound-region SESE walks (`core/dag/bound_regions.py`) exclude RoutingMode.DIVERT edges BY PINNED DECISION (WS2 plan decision 1; §7 rule 9 treats in-region on_error as legal failure semantics, not topology). "Fixing" rule 4 to walk on_error edges rejects the corpus branch-loss fixtures wholesale (8 cases measured at planning time).
  3. (c) Every new `raise` under `src/elspeth/core/dag/` or `src/elspeth/core/config.py` needs a SAME-COMMIT parity adjudication: run `scripts/cicd/runtime_rejection_parity.py --write` and adjudicate the seeded entry (mirrored counterparts must be real Stage-1 codes; the unmirrored ceiling has no slack). A message reword re-keys the site — carry the adjudication across, never hand-edit a `key:`.
  4. (d) The escalation fixpoint bound is DERIVED at build (`derive_escalation_fixpoint_bound`, 1000 + 8·depth, `core/dag/bound_regions.py`) and the leader drain reads it from the built graph. Never reintroduce a constant iteration bound in `leader_drain`/EOF flush code; competing formulas are deleted, not forked.
  5. (e) The capability gate derives its field inventory from `canonical_set_pipeline_schema()`: documenting a field in `pipeline_capabilities.md` REQUIRES the set_pipeline schema (and its redaction.py pydantic authority) to carry it — md and schema move in one commit or the gate is red from both directions. `enum` constraints on set_pipeline node properties trip `assert_set_pipeline_schema_compatible` ("advertised enum is narrower than the runtime model") when the pydantic field is a plain `str | None`; use `["string","null"]` with the vocabulary in the description.
  6. (f) The redaction snapshot regenerates ONLY via `scripts/cicd/bootstrap_redaction_snapshot.py --write`, never by hand. A regeneration whose entry hashes move with `sensitive_path_count` UNCHANGED still verdicts `direction=weaken` under `check_redaction_direction.py`'s conservative same-count rule: that is disposition (c) of the 2026-08-18 entry — the PR needs the `policy-weaken-justified` label plus the exact phrase "Redaction policy weakening rationale" in the body, never a code workaround.
  7. (g) The guided capability core is hash-pinned (`capability_core_hash`, `capability_skill.py`) and its exact bytes front EVERY authoring surface. Lane-scoped guidance goes in the guided overlay/skill files, NEVER the shared core; a core edit moves the pinned hash and every surface's prompt at once.
  8. (h) Collectors validate (Stage 1) and build (DAG) but CANNOT RUN until WS4: `engine/orchestrator/graph_registration.py` rejects any graph whose collector id map is non-empty (pinned decision 5). The GUIDED lane additionally refused collector-bearing candidates (`guided_collector_not_authorable`, `guided/planning.py::_reject_collector_candidate_nodes`). RULED by the maintainer (comment 7878 on elspeth-88bb77953c): the guard lifts AFTER the WS6 disposition-vocabulary freeze, never before. LIFTED 2026-08-25 as the WS6 lane-2 parity sweep; the guard, the predecessor guard, and the error code no longer exist.
  See [CONTRIBUTING: Gate: plugin inventories, source hashes, scenario-corpus manifest, fingerprint baseline](../../CONTRIBUTING.md#gate-plugin-inventories-source-hashes-scenario-corpus-manifest-fingerprint-baseline).

- **2026-08-25 — RULED exemption to "never regenerate the pre-flip oracle" (META-39), and the rule that would have caught it earlier** (532f037fe)
  `fork-coalesce-policies` `require-all-lost-c` + `quorum-impossible-lost-c` were re-frozen 2026-08-25 under ADR-042 (META-39): the old snapshots witnessed a failed-group survivor labelled `late_arrival_after_merge`, which spec §2 rev 3.2 forbids — the engine is right, the oracle pinned a locked-in-buggy label. The never-regenerate rule guards ZERO-behaviour-delta checkpoints (WS1), not ruled vocabulary changes. A ruled re-freeze is still a per-case `ELSPETH_ORACLE_FREEZE=write …::test_frozen_oracle_surface[<scenario>--<case>]` write from a CLEAN export (the live tree carries sibling WIP in `src/`), plus those cases' `projection_sha256` in `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`, and `git status --porcelain` must show only those files.
  RULE: any change touching disposition / terminal / settlement VOCABULARY bytes MUST include `tests/integration/core/dag` in its export verification selection — a reason string feeds `compute_error_hash`, so the corpus's frozen `error_hash` and `projection_sha256` move even when every count is unchanged. 532f037fe shipped green on a 13-suite selection that excluded it.
  META-41 (2026-08-25, same authority): SEVEN `fork-coalesce-policies` movers re-frozen for META-40 — five cases' `projection_sha256` re-pinned in the manifest (`member_disposition` appears in the coalesce failure / late-arrival hold payloads: first-nested, first-select, first-union, require-all-lost-c, quorum-impossible-lost-c), PLUS the two frozen oracle snapshots (require-all-lost-c, quorum-impossible-lost-c) re-frozen: one survivor terminal per case moved from the seam-written executor cause (`branch_lost:path_c` / `quorum_impossible:need=3,max_possible=2`) to `scope_group_failed`. Each lost-c case has TWO failure survivors; the late-arrival one was already `scope_group_failed` since META-39, but the seam-settled one carried the cause until META-40. Those snapshots are the corpus's ONLY pin on the seam's terminal vocabulary (the semantic projection excludes terminal `error_hash`), so reverting the META-40 seam write fails exactly them and nothing else in the family.
  RULE (WS5 close gate): a registry-hash re-pin rides EVERY ruled manifest re-freeze commit — `tests/unit/architecture/
  test_dag_scenario_corpus_contract.py::EXPECTED_CASE_REGISTRY_SHA256` hashes every harness case's FULL `model_dump` (manifest `expected` oracle values included, not just ids), so META-39's re-pin went stale there and META-41's compounded it; neither lane's scoped selection included `tests/unit/architecture`.
  See [CONTRIBUTING: Gate: declared oracles pin output bytes](../../CONTRIBUTING.md#gate-declared-oracles-pin-output-bytes).

- **2026-08-23 — a NEW corpus case fails the WS1 frozen-oracle gate closed, and the fix is a scoped write, never a full regenerate** (elspeth-7d68dd828e)
  `tests/integration/core/
  dag/test_oracle_freeze.py::test_frozen_oracle_surface` (campaign instrument, deleted at WS1/WS2 close) parametrizes over EVERY non-build-workflow case in the live manifest via `iter_harness_cases`, with no exemption for a case that did not exist at the pre-flip freeze commit. Adding one fails with "No frozen snapshot ... run the freeze writer at the recorded pre-flip commit", because there is no scenario-level or case-level classification for "born after the freeze". The correct response is not to touch any existing snapshot (that is the oracle tampering the module docstring warns about) but to write only the new case's own file, once, by name: `ELSPETH_ORACLE_FREEZE=write pytest "tests/integration/core/dag/test_oracle_freeze.py::test_frozen_oracle_surface[<scenario>--<case>]"` (repeat per new case; `-k` also works if it resolves to exactly the new ids). Verify with `git status --porcelain tests/fixtures/dag_scenario_corpus/oracle_freeze/` that only brand-new (`??`) files appeared, never an `M` against an existing snapshot. Landed with elspeth-7d68dd828e (conditional-routing:route-reopen-resume-second-sink, multiple-independent-sources:two-source-parallel-regions).
  See [CONTRIBUTING: Gate: declared oracles pin output bytes](../../CONTRIBUTING.md#gate-declared-oracles-pin-output-bytes).

- **2026-08-22 — WS1a unified-lineage prep conventions** (branch feature/unified-lineage)
  Four traps until the WS1b flip lands:
  1. `TokenInfo.lineage_path` is WRITE-ONLY during WS1 prep; reading it from production code before the WS1b flip is a dual-representation defect. The only sanctioned reads are the two strict-pop sites (engine `TokenManager.coalesce_tokens` and the durable twin in `data_flow/tokens.py`), both routed through `contracts.identity.pop_closer_frame` (since META-38, 2026-08-25: `truncate_at_closer_frame`).
  2. Crafted-token tests that feed `coalesce_tokens` MUST build real fork lineage via `create_token(..., lineage_frames=...)` (or a real `fork_token`); the durable strict pop rejects frame-less parents and the fix is ALWAYS the fixture, never the pop.
  3. A zero-row `success_empty()` traversal of a `creates_tokens=True` transform mints a `group_records` row (member_count=0); plain filters mint nothing. Count rows over that table accordingly.
  4. `TokenInfo` no longer carries `join_group_id`; merge context rides `RowResult`/`PendingOutcome`/`WorkItem` carriers (COALESCED path requires it, every other path forbids it). The `tokens` / `token_work_items` COLUMNS keep it permanently.
  Maintainer ruling 2026-08-22: nested regions (fork-in-fork, expand-in-fork) are NEW behaviour — today's engine rejects those topologies at build time, so no frozen differential oracle exists for them; the depth-1 oracle-freeze snapshots under `tests/fixtures/dag_scenario_corpus/oracle_freeze/` are the pre-flip oracle and must NEVER be regenerated after the freeze commit.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-22 — WS1b Phase A lifts the read restriction for `IncompleteTokenSpec.lineage_path` and `token_lineage_frames`, NOT for `TokenInfo.lineage_path`** (Tasks 4/5, branch feature/unified-lineage)
  `TokenInfo.lineage_path` stays write-only until the flip. Two NEW sanctioned reads landed alongside, each scoped to its own field/table:
  1. `engine/processor.py::classify_resume_start` and `resume_incomplete_token` read `IncompleteTokenSpec.lineage_path` (spec §4.1a) to select the resume-start dispatch arm. PINNED order is merged (join) first, then innermost-EXPAND, then innermost-FORK, then raise; the fork-child branch identity is the innermost FORK frame's `member_key`, never `spec.branch_name` directly.
  2. MCP read surfaces (`mcp/analyzers/queries.py::list_tokens`, `mcp/analyzers/reports.py::get_outcome_analysis`'s fork/join counts) batch-load `token_lineage_frames` via `DataFlowRepository.load_lineage_paths` / the newly-added `DataFlowReadRepository.load_lineage_paths` forwarder (`core/landscape/factory.py`) and project `branch_name`/`fork_group_id`/`expand_group_id` as DERIVED values via `contracts.identity.path_branch_name`/`path_fork_group_id`/`path_expand_group_id` — never a stored-column read (ruling 21, ratified 2026-08-22: the legacy wire names stay, the mechanism underneath changes).
  `DataFlowReadRepository` (the read-only port `RecorderFactory.read_only()` returns, which is what the real `elspeth-landscape` MCP server runs on) did NOT forward `load_lineage_paths` before this; only the writable `DataFlowRepository` had it. A test built against a writable `RecorderFactory` stays green without the forwarder, so the gap is invisible until the real read-only MCP server is exercised.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-21 — repair advice must be executed, not just re-validated: a transform's `schema` block is BOTH the composer contract and the runtime INPUT model**
  Rule C's remedies each cleared `validate()` and were pinned there, and two of them left a node whose fixed-mode pydantic input model (`extra_forbidden`, checked in `_run_preflight` BEFORE `process()`) rejected the very field the mapping reads: the nested read's top-level container (`user` for `user.name`), or the normalized key (`name`) a row actually arrives under for a non-fixed-point mapping source (`Name`). A third remedy ("remove the target from `schema.fields`") hit `FieldMapperConfig`'s at-least-one-field invariant in the single-field shape, and "make upstream guarantee the literal" can NEVER clear because `_mapping_target_is_guaranteed` abstains for every non-fixed-point source, strict or not. Measured repairs: nested read -> `strict: true` PLUS `schema.mode: flexible`; non-fixed-point source -> target optional (`name: type?`) PLUS `schema.mode: flexible`, or upstream rename to the stable spelling PLUS rewriting the mapping key to that same spelling. Truth-pin advice by running it through the executor-shaped chain (`input_schema.model_validate(strict=True)` -> `process()` -> `verify_schema_config_mode`), not only through `validate()`; see `_run_field_mapper_as_the_executor_would` in `tests/unit/web/composer/test_state.py`.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-21 — a config-time DECLARATION check compares two name spaces, and the shortfall may be UNDECLARABLE** (elspeth-a9ba80cb0b)
  A single-prompt `llm` node could declare `required_input_fields: ["case_study_1"]` while its template read `{{ row.case_study }}`: presence was checked, agreement never was, and the edge contract was satisfied by the DECLARATION, so every row raised `UndefinedError` at render. Multi-query already had this check; the single-prompt branch did not. Five traps, all measured:
  1. Do NOT copy the multi-query message. A query renders a SYNTHETIC context (`input_fields` + `source_row`), so an unbound `row.<name>` provably raises; single-prompt binds `row` to the WHOLE row, so an undeclared reference raises only when that column is in fact absent. "Fails for every row" is FALSE here. It is a CONTRACT check: the reference escapes the set the DAG checks against upstream guarantees, and `verify_declared_required_fields` re-checks per row.
  2. Coverage is EXACT, and a `normalize_field_name` bridge is UNSOUND in the obvious direction. `SchemaContract.find_name` matches a field's `normalized_name` OR its `original_name` — two exact spellings, and config time knows neither. Measured with `required_input_fields: ["a_b"]` against a row whose one column is `a_b`, TWELVE declarable spellings (`A_B`, `a__b`, `A_B_`, `row["a b"]`, ...) are accepted and only ONE renders. The one sound inference runs the other way: a literal that is not a legal declaration entry (`"Original Header"`, `"class"`) can never BE a `normalized_name`, so it can only be an `original_name` and its canonical form IS the row key. Bridge those, and REPORT them when that canonical name is not declared; drop only a literal with no declarable form at all (`"!!!"`). `undeclared_row_fields` / `declarable_field_name` / `describe_undeclared_row_fields` (`plugins/sources/field_normalization.py`) single-own this, and live beside `normalize_field_name` deliberately: the first version put them in `core/templates.py` and drew the third-ever `L1: Upward import` finding in the tree.
  3. Leading a rejection with "declare what you read" HANDS THE PLANNER A REPAIR THAT BREAKS THE RUN. `verify_declared_required_fields` is a plain set difference over ROW KEYS with no dual-name limb, so declaring a read name the producer does not guarantee is accepted at config time and then raises `DeclaredRequiredInputFieldsViolation` on EVERY row (measured for `{{ row.Name }}` + `["name"]` and for a `field_mapper` rename leaving a stale `original_name`). The remedy must LEAD with rewrite-the-reference and qualify add-the-name as correct only when the producer guarantees that exact spelling; it must also name the DECLARABLE form of a bracket literal — `'Original Header' (declare as 'original_header')`.
  4. Do NOT attempt guard analysis. Measured through the real `PromptTemplate`: `{% if row.x is defined %}`, `{{ row.x | default('') }}`, `{% if 'x' in row %}` and `{{ row.get('x','') }}` RENDER when the column is absent, while `{% if row.x %}`, `{{ row.x if row.x else '' }}`, `{{ row.x or 'n/a' }}` and `{{ '' if row.x is none else row.x }}` RAISE — one token apart. A genuinely optional guarded read has no honest repair, and ZERO exist in the tree (the one that looks like a guard, `examples/chroma_rag_qa`'s `{% if row.sci__rag_context %}`, is a FAKE guard that raises and whose declaration is load-bearing).
  5. The composer twin is REQUIRED, not redundant. The composer's probes DO construct the node and DO see the plugin's rejection — three times inside one `validate()` (`_semantic_validator._instantiate_consumer`, `_probe_transform_declared_inputs`, `_probe_transform_declared_output_fields`) — and every one swallows it through `_is_config_probe_exception`, deliberate and test-pinned so a draft never crashes validation. Stage 2 preflight rejects only via `preview_pipeline`, and codelessly (`error_code=None`, matched by 0 of the 115 `_VALIDATION_ERROR_PATTERNS`).
  Also: `extract_jinja2_field_usage` has no scope tracking, so a template rebinding the name `row` (a macro parameter named `row`, `{% for row in row.cases %}`) donates the local's attribute reads to the row-field set and is rejected with no satisfiable declaration — pre-existing, and no in-tree template does it. Two sibling facts for testing nearby: use `schema: {"mode": "observed"}` in a fixture, because a `mode: fixed` block makes `_reject_fixed_schema_omitting_consumed_fields` reject EVERY case including the correct one; and pydantic compiles `model_validator`s at class-build time, so reassigning one post-hoc to instrument it is a SILENT no-op — hook `model_validate` instead.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-21 — repair advice for a composer error code has TWO surfaces on DISJOINT paths, and one error code must mean ONE defect** (elspeth-920bd88299, 88137581b, 7015a561f)
  A validation error's remedy text reaches the LLM planner two ways, and neither path can see the other: a tool call (`upsert_node` / `preview_pipeline`) returns the rendered message from `web/composer/state.py` and never the catalogue; the one-shot planner's repair turn gets only `tools/generation.py::_VALIDATION_ERROR_PATTERNS`, because `pipeline_planner._allowlisted_candidate_feedback` projects `explanation`/`suggested_fix` from the `error_code` ALONE and withholds the message (a custody boundary — the message quotes authored option values; do not widen the `detail` allowlist at :2432). 88137581b rewrote Rule C's message and left the catalogue on advice authored a month earlier in 7015a561f; the planner alternated remedy sets and never converged, and zero tests pinned either text so the drift landed green. The fix is structural: the prose lives in `state.py` constants that `generation.py` IMPORTS (the direction is forced — `generation.py` already imports private names from `state.py`, and `state.py` has no edge to `tools.*` at any scope). Four traps:
  1. One code, two emitters is the deeper bug. Rule C (declared output not guaranteed) and Rule D (declared output collides with an input field) shared `transform_contract_violation`, and the catalogue is keyed on the CODE, so a Rule D collision on an `llm` node got field_mapper advice naming `mapping` and `select_only`. Rule C now carries `transform_declared_output_not_guaranteed`; Rule D keeps the old code because `config/cicd/runtime_rejection_parity.yaml` already binds it to `validate_transform_output_field_collisions`.
  2. `_VALIDATION_ERROR_PATTERNS` matches in LIST ORDER against raw text. Two rules whose headlines share a prefix must be ordered specific-first, and a split needs a distinguishable headline (hence "Transform output guarantee violation:"). `frontend/src/lib/validationHumaniser.ts` matches headlines too, and a non-match silently promotes the raw engineer dump to the user-facing headline rather than erroring.
  3. Do not hand-write the row-key predicate in prose. Both obvious spellings are measurably wrong: `str.isidentifier` admits `Name`/`userID`/`_id`/`a__b` where `field_mapper` abstains, and "lowercase, no spaces, not a keyword" REJECTS `class_`/`_1`/`if_`/`2024_total` where it does not. Rule C asks the PLUGIN instead — two canonical counterfactual configs through `_probe_transform_output_schema`, reading back whether the target lands in its guarantees.
  4. Pin TRUTH, not existence. The replaced remedies were, measured: "drop it from the schema declaration" (names a mapping TARGET while `schema.fields` holds SOURCES — the edit is accepted and the next error is byte-identical), "map the missing field through" (under `select_only` the name is already a target, so it is always a duplicate-target rejection), `strict: true` (inert for every non-fixed-point source, and mutually exclusive with the declare-the-source remedy), and declare-source-in-`fields`-plus-`guaranteed_fields` (correct only conjoined with dropping the target's own entry, and a silent no-op for a non-fixed-point source). A test asserting the message MENTIONS `guaranteed_fields` holds green through all of that. Assert that applying the remedy CLEARS the error, in a topology where a consumer actually requires the field.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-21 — a config-time name comparison must compare CANONICAL ROW KEYS, not config literals** (elspeth-bb470636d1)
  The companion to the fixed-point entry below: that one fixed `field_mapper`'s GUARD, this one its config VALIDATOR, and either alone leaves the other's shape reachable. `_reject_overlapping_rename_graphs` rejected rename chains with `target in set(self.mapping)` — literal strings — while `process` deletes the renamed source by a key it picks at RUNTIME (the literal when it is already in the output dict, else `contract.resolve_name(source)`) and writes `output[target]` under the LITERAL target. So `{"a":"b","b":"c"}` was rejected while `{"a":"b","B":"c"}` was ACCEPTED and then destroyed the same value. The key is `_canonical_row_key`: `normalize_field_name`, except that a DOTTED name (a nested read, never a row key) and a name that normalizes to nothing (`ExternalHeaderError` — it names no field, so it can alias nothing) are keyed by themselves. Three traps:
  1. The canonical key ADDS a rejection limb; it must NOT replace the literal one, and canonicalising the identity guard on its own is a REGRESSION a green suite will not show. A row can carry `'B'` and `'b'` as two DISTINCT keys — a source's `field_mapping` values bypass `normalize_field_name` (`resolve_field_names` validates them with `isidentifier()` alone) and headerless `columns` are taken as already-clean identifiers. So "a row key is always a normalization fixed point" is FALSE, and a canonical identity guard waves `{"B": "b", "b": "y"}` through to destroy one value (measured: `{'B':'1','b':'2'}` emits `{'y':'2'}`; the reversed spelling emits `{'y':'1'}`). Reject on EITHER limb. Conversely the canonical limb needs a CANONICAL identity guard, or `{"A": "a"}` — a verified no-op — is newly rejected at build time.
  2. Pin the ACCEPTED shapes by running `process()`, not by asserting the config constructed. `assert transform._mapping == mapping` pins the EXISTENCE of a relief and says nothing about its safety; that gap let the identity-guard regression pass 125 green tests.
  3. A "no false overlap" test only DISCRIMINATES when the invented key lands on the TARGET side, because membership is tested on targets. `{"meta.source": "a", "meta_source": "b"}` passes with or without the dotted-name branch; `{"meta.source": "x", "y": "meta_source"}` is the shape that fails. Same for the unnormalizable branch: put the odd literal on the target (`{"!!!": "mapped", "a": "???"}`). Mutation-test every branch of the key function AND every limb of the rule that calls it — two branches survived the first mutation round, and the identity-guard regression was caught only by an adversarial reviewer.
  The class is NARROWED, not closed: `resolve_name` is an `original_name` index lookup, not a call to `normalize_field_name`, so a source whose `field_mapping` moves a header off its normalized form (`{"Weird Header": "b"}`) still resolves to a key no config literal predicts.
  See [CONTRIBUTING: Convention: passes_through_input presence discipline](../../CONTRIBUTING.md#convention-passes_through_input-presence-discipline).

- **2026-08-21 — a caught exception's TIER is the discriminator, and the cheapest way to say so is a NARROWED PARAMETER TYPE** (elspeth-181db83da7, elspeth-82d4c5146c)
  1. `except SomePluginContractViolation` is the WRONG key for "may this be routed to `on_error`?". Registration in `TIER_1_ERRORS` is what forbids routing (ADR-008 §"TIER_1 registration is load-bearing"), and the registry cuts ACROSS the class tree in both directions: `SinkTransactionalInvariantError` is a REGISTERED `PluginContractViolation` (so it must keep crashing even though its base is Tier 2), while `UnexpectedEmptyEmissionViolation` is the one UNREGISTERED member of the otherwise-Tier-1 `DeclarationContractViolation` hierarchy. Always `except contract_errors.TIER_1_ERRORS: raise` FIRST, as a live module attribute, never a from-import, since the tuple is materialized per access (errors.py:1738).
  2. The two violation hierarchies are SIBLINGS, not parent and child: `DeclarationContractViolation` (declaration_contracts.py) is `(AuditEvidenceBase, RuntimeError)`, and `issubclass(DeclarationContractViolation, PluginContractViolation)` is FALSE. `PassThroughContractViolation` / `DeclaredOutputFieldsViolation` / `UnexpectedEmptyEmissionViolation` are all outside a `PluginContractViolation`-keyed blast radius.
  3. `isinstance(exc, contract_errors.TIER_1_ERRORS)` is correct but EXPENSIVE: every `isinstance` in the tree is individually judge-gated by `trust_tier.tier_model`, and files carry per-file `max_hits` ceilings (`engine/executors/transform.py` is 2). When the tier is statically known at the call site, narrow the CALLEE's parameter annotation to the Tier-1 union instead and let mypy enforce it: same rule, zero new suppressions.
  4. A COMPENSATING record can outlive the defect it compensated for. `_record_terminal_contract_failure` pre-wrote a FAILURE/UNROUTED `token_outcomes` row because the run was about to abort (elspeth-82d4c5146c); once the violation became routable, the routing path wrote the real outcome and the pre-write became a duplicate the audit store rejects (`LandscapeRecordError ... IntegrityError`, raised from sink-effect finalization, nowhere near the changed code). When restoring a path that was previously dead, grep for what was recording on its behalf, and verify BOTH destinations: a named `on_error` sink terminalizes through sink-effect finalization, `discard` through traversal. `ix_token_outcomes_terminal_unique` (`core/landscape/schema.py:677`) is keyed on `token_id` alone under `completed == 1`, with no sink discrimination, so either path raises on a double write; the ZERO-write direction has no automatic detection anywhere and must be checked by hand.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-21 — "does this config literal name a row key?" is `normalize_field_name(x) == x`, NEVER `x.isidentifier()`** (elspeth-f262a8c678)
  Sources key a row by the NORMALIZED header, and `normalize_field_name` lowercases and keyword-suffixes as well as scrubbing punctuation. So `'B'`, `'Name'`, `'userID'`, `'ID'` and `'class'` are all perfectly good identifiers that no row is ever keyed by; they reach a transform only through `contract.resolve_name`, exactly like the visibly messy `'First Name'`. An `isidentifier()` proxy recognises ONE of the two halves of "original header" and silently mis-files the other, which is how `field_mapper` came to guarantee a column its own `process` deletes (`SchemaConfigModeViolation: missing required fields ['name']` on a correct rename, plus a `DeclaredOutputFieldsViolation` when the deleted normalized name is another entry's target). The predicate is normalization's FIXED POINTS; `value_transform._row_key_aliases` had already learned this and is the handling to copy — answer `ExternalHeaderError` (a literal that normalizes to nothing names no field) and let a bare `ValueError` propagate, or the exception class a bad config key raises at construction changes. Two traps when auditing this class:
  1. A corpus scan finds nothing, because a case-variant source is a pure blind spot — zero in-tree configs use one, which is precisely why no test caught it. Test the predicate against `SchemaContract.resolve_name` rather than against `normalize_field_name` again, or the assertion restates the implementation.
  2. The fix moves BUILD-TIME verdicts, and not in the obvious direction: a wider "unresolved" set makes `field_mapper` ABSTAIN more, which through `schema_validation.py`'s `if not vote.fields and not vote.participated` clears graphs it used to reject. Measure with a HEAD-vs-patched `ExecutionGraph` matrix and check the moved cells against the class that already abstains, not against zero.
  See [CONTRIBUTING: Convention: passes_through_input presence discipline](../../CONTRIBUTING.md#convention-passes_through_input-presence-discipline).

- **2026-08-20 — editing a builtin plugin: `source_file_hash` tracks content, `plugin_version` does not**
  1. `source_file_hash` must be recomputed for ANY content change, not just a new plugin. Compute with `scripts/cicd/plugin_hash.py::compute_source_file_hash`, and do it AFTER `ruff format` — the pre-commit formatter rewraps lines and restales a hash computed first. The computation normalises the hash line itself, so it is stable once written.
  2. `plugin_version` is a STATIC declaration. All 52 builtins sit at `1.0.0` and none has ever been changed in place (verified over the whole history, 2026-08-20). Do not invent a bump for a behaviour change, and do not read an unbumped version as an oversight.
  Two traps when verifying: (a) `computed in path.read_text()` is a SUBSTRING test that passes on a mere comment — compare strictly, `Cls.source_file_hash == compute_source_file_hash(path)`; (b) no local test enforces the hash at all — a stale hash with a mutated body passed 10,213 tests. The gate is CI-only, so that strict comparison is the only local defense.
  See [CONTRIBUTING: Gate: plugin inventories, source hashes, scenario-corpus manifest, fingerprint baseline](../../CONTRIBUTING.md#gate-plugin-inventories-source-hashes-scenario-corpus-manifest-fingerprint-baseline).

- **2026-08-20 — a construction-time normalisation in `web/composer/state.py` invalidates PERSISTED hashes; it is never a local change** (elspeth-da00e1c1cb)
  `NodeSpec.__post_init__` is advertised as the one construction boundary every path routes through, so normalisations keep landing there, and each one silently breaks two seams that read bytes written by an OLDER build:
  1. `restore_owned_composition_state_authority` (`pipeline_proposal.py`) requires `to_dict(from_dict(payload)) == payload`. A payload authored before the normalisation can never satisfy it again, and it must NOT be migrated: `tool_arguments_hash`, `private_arguments_hash` and `draft_hash` all bind the raw bytes, so rewriting them trades one integrity error for three.
  2. `composition_content_hash` hashes `to_dict()` AFTER normalisation, and that value is STORED in `PresentBase.composition_content_hash`. `sessions/service.py` re-derives and compares it on the fork path, so a normalisation retroactively unbinds every stored base binding for the shapes it touches. This is the wider blast radius and needs no owned authority to fire.
  `tests/unit/web/composer/test_state_serialisation_contract.py` pins both: content hashes for representative authored shapes, and an AST check that every spec's `from_dict` reads EXACTLY its declared dataclass fields (the undeclared-field rejection uses `dataclasses.fields()` as the set of keys a restore observes). A reddened hash pin is the gate working — decide what happens to already-persisted states before re-pinning. Two traps: (a) an AST gate looking for `object.__setattr__` is BLIND, because house style routes field rewrites through `freeze_fields` (`contracts/freeze.py`) — pin behaviour, not syntax; (b) do not "fix" the restore by tolerating stale bytes. The coalesce defaults are read from `CoalesceSettings.model_fields` precisely so they track the runtime, so a payload omitting `merge`/`policy` records no epoch: accepting it lets a later default change re-interpret already-reviewed bytes with every hash still green. Quarantine, do not migrate.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-20 — a REDUCTIVE transform's output `SchemaConfig` must not carry the authored INPUT `fields`** (elspeth-a2bf676e6f, elspeth-f5f798f797)
  A transform node's `schema:` block is the INPUT contract: `BaseTransform._build_output_schema_config` documents its argument as "the transform's input schema config (base fields)" and instructs reductive subclasses to "drop input-side declarations" (canonical override: `BatchStats`, elspeth-f5f798f797). `field_mapper` overrode the method but still passed `cfg.schema_config.fields` through, so every field it CONSUMES-but-never-emits (a renamed-away source in both modes, and anything outside the whitelist under `select_only`) stayed `required` on the OUTPUT config. `SchemaConfig.get_effective_guaranteed_fields()` is `explicit guaranteed | required declared fields`, so those names were demanded as output guarantees and the transform's own contract rejected its own emitted row with `SchemaConfigModeViolation`. Composer-side this surfaced as a Rule C "Transform contract violation" on a CORRECT cleanup pipeline, and a renaming mapper had NO satisfiable declaration at all under `strict: false`. Two traps: (a) a rename target may be declared by EITHER name — prefer a declaration written against the emitted (target) name, else carry the source's across so the type does not degrade to `any`; (b) three existing tests used "declared in `schema.fields` but not selected" as a VEHICLE to trip Rule C or the post-emission check — that shape is legal now, so re-point such a vehicle at an unguaranteed RENAME rather than deleting the test. STILL OPEN, deliberately unbundled: `base_guaranteed` in `_build_field_mapper_output_schema_config` reads the explicit `guaranteed_fields` tuple only, not `get_effective_guaranteed_fields()`, so a `mode: fixed` mapper with no explicit `guaranteed_fields` guarantees nothing and Rule C still false-positives on a plain whitelist. Changing it moves DAG contract propagation, so it needs its own sweep.
  See [CONTRIBUTING: Convention: passes_through_input presence discipline](../../CONTRIBUTING.md#convention-passes_through_input-presence-discipline).

- **2026-08-19 — `plan_pipeline` requires the session schema tracker; aid-supplied manifest keys are palette-retained AND escalation-exempt** (elspeth-cb3561382e, 275e05bf71, ac44757161)
  `plan_pipeline` takes `schemas_loaded`/`mark_schema_loaded` as REQUIRED kwargs; a new call site must thread the per-session tracker (`ComposerServiceImpl._mark_plugin_schema_loaded` via `functools.partial`), never default it away. Manifest keys supplied by authoring aids (`model.catalog`, `expression.grammar` — the closed `_AID_SUPPLIED_INFORMATION_KEYS` set) keep their palette tools advertised as the oversize escape AND are exempt from no-gain ESCALATION (per-call DISCOVERY_NO_GAIN feedback still fires; the turn budget is the doom-loop backstop). Extending either set means revisiting both properties together: a supplied key whose tool stays advertised without the exemption is a request-killer (two no-gain calls = terminal).
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-18 — `ToolResult.to_dict()` keys are declared TWICE: the dataclass and the redaction manifest** (elspeth-f14aba9686)
  Adding a serialized key to `ToolResult.to_dict()` without declaring it in `redaction.py` does not degrade gracefully: `_ToolResultResponseModel` is `extra="forbid"`, so every type-driven mutating tool REJECTS the response at the audit-persistence boundary (a scoped tool test stays green; the failure is in `redact_tool_call_response`). Declare the key on the model (Sensitive envelope for payload-bearing keys) AND in `_TOOL_RESULT_OPTIONAL_RESPONSE_KEYS`, then regenerate `redaction_policy_snapshot.json` via `scripts/cicd/bootstrap_redaction_snapshot.py --write`, never by hand. Three dispositions:
  1. Declarative entries with `handles_no_sensitive_data=True` have EMPTY `known_response_keys`; the shared-list edit never reaches them and the new key aggregates as `REDACTED_UNKNOWN_RESPONSE_FIELD` (value-free, safe, +1 telemetry event — the pre-existing `affected_nodes` pattern).
  2. Declarative entries with `handles_no_sensitive_data=False` and NON-EMPTY `known_response_keys` (the `set_metadata` / `set_output` / `upsert_node` class) DO inherit the shared-list key: each such entry's snapshot hash moves, and that churn feeds disposition 3.
  3. `check_redaction_direction.py` can verdict a pure strengthening as `weaken` via its conservative same-count rule when an entry's hash moves only by gaining a non-sensitive key; that needs the `policy-weaken-justified` label plus the exact-phrase rationale on the PR, not a code workaround.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-18 — guided prompt traps: the palette gate polices the COMPOSED prompt, step skills feed THREE surfaces, and the planner context is where a redaction is explained** (elspeth-63cf3803e6; the palette gate itself predates it — 377bcc9a3, 2026-07-20)
  1. `test_guided_chat_prompts_name_only_tools_in_their_actual_palette` asserts over `load_step_chat_skill(step)` — base.md PLUS the step file — for every GuidedStep, so one base.md edit can redden all four step assertions at once. Exact pins: `list_sources`/`list_transforms`/`list_models` in NO composed prompt; `list_sinks`/`get_plugin_schema` absent from step 1 and present in step 2 (steps 3-4 are NOT policed for those two); `confirm_wiring` absent from step 4. Say what to do, never which tool not to call — a natural phrasing like "don't call `list_transforms` to confirm a negative" turns the gate red.
  2. A `guided/skills/step_*.md` file renders on the PLANNER surface and on the step CHAT surfaces (per-step solver and the deferred-intent management chat). The chats receive no planner-context enrichments — `unproducible_output_fields` never reaches them — so a skill branch conditioned on a planner-context key's ABSENCE is vacuously true in every chat session. Scope such branches to authoring and give question-answering a hedged variant.
  3. When a redaction makes the planner burn discovery turns, the fix seam is a static usage line INSIDE `guided_redacted_planner_context` (the `output_usage` / `reviewed_configuration_usage` precedent) — adjacent to the confusing keys, zero new egress — not the system prompt. The phrasing constraints (RESTORED, never "owns") live on that key's comment in planning.py; read them there before rewording. The projection is pinned by full-dict equality in `test_proposal_audit_projection.py`, so any added key is a deliberate test update, and the canary assertions above the pin prove the addition leaked no private value.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-17 — a full-suite run in the SHARED checkout is not evidence unless HEAD is unchanged across it, and a worktree A/B UNDER-COLLECTS**
  Two ways a whole-tree measurement lies.
  1. `pytest tests/` takes ~18 minutes; four sibling commits landed inside one such window on 2026-08-17 and the run reported 456 failures across engine/pipeline/e2e that did not exist before or after (a representative slice re-run immediately after: 22 passed). Record `git rev-parse HEAD` BEFORE and AFTER a long run; if they differ, a red result is uninterpretable — re-run rather than diagnose.
  2. Running the A/B side in a `git worktree` silently changes what is collected: `evals/*` is git-ignored except for tracked re-includes, so a fresh worktree has no `evals/composer-rgr`, `composer-harness`, and every suite that GLOBS those assets collects fewer tests there (measured: `test_convergence_scenarios.py` 11 vs 32, `test_paths.py` 22 vs 40, `test_execution_repository.py` 148 vs 161). A worktree test-count delta is therefore NOT attributable to the change under test. To attribute a count honestly, diff per-file collected counts (`pytest --collect-only -q | sed 's/::.*//' | uniq -c`) between the two trees and read the per-file rows, not the total. Worktree e2e recovery tests also fail on capture-root binding, so a worktree pass/fail is its own instrument.
  See [CONTRIBUTING: Why a green scoped run proves nothing](../../CONTRIBUTING.md#why-a-green-scoped-run-proves-nothing).

- **2026-08-17 — a directory-scoped test `conftest.py` that mutates `sys.path` is PROCESS-GLOBAL, not directory-scoped**
  `tests/unit/evals/composer_battery/
  conftest.py` (Task 7 of the composer-battery build, elspeth-composer-battery) is the first such shim under `tests/unit/evals/`: it `sys.path.insert(0, …)`s `evals/composer-battery/` so tests can `import drive_battery` (a module in a hyphenated, non-package directory). pytest loads a directory's `conftest.py` once per worker process, but the path mutation persists for the REST of that worker's session, so every later test module collected on the same xdist worker resolves a bare top-level import against `evals/composer-battery/` FIRST, ahead of site-packages and the repo root. That directory holds generically-named modules (`report.py`, and Task 8's `planner_probe.py`) that could shadow an unrelated `import report`. Verified 2026-08-17: no test currently does a bare `import report`/`from report import`, so this is latent, not live. The next agent adding a `tests/unit/**/conftest.py` with a similar `sys.path` insertion must grep for a same-name collision first (`grep -rn "^\s*import <name>\b\|^\s*from <name> import" tests/ src/ evals/`), and prefer `sys.path.append(...)` over `insert(0, ...)` unless import priority is actually required.
  See [CONTRIBUTING: Convention: test discipline](../../CONTRIBUTING.md#convention-test-discipline-fakes-mocks-fixtures).

- **2026-08-17 — a NEW runtime rejection needs a Stage-1 disposition (whole-tree gate)** (elspeth-96e2dd023f, elspeth-2ed41f0a4a)
  `tests/unit/scripts/cicd/test_runtime_rejection_parity_gate.py` AST-enumerates every `raise <Exception>(...)` under `src/elspeth/core/dag/` and `src/elspeth/core/config.py` PLUS every declarative pydantic `Field(min_length=/max_length=/gt=/...)` constraint on those settings models, and requires each site to carry a reviewed disposition in `config/cicd/runtime_rejection_parity.yaml` (`mirrored` with a real Stage-1 `error_code` or `fn:<validator>` counterpart, `abstains`, `structural`, `not_authorable`, or `unmirrored` under a ratchet ceiling — 10 today, elspeth-96e2dd023f). Adding a rule to the DAG builder or a settings validator fails the gate until `.venv/bin/python scripts/cicd/runtime_rejection_parity.py --write` is run and the seeded entry adjudicated; that is the point (elspeth-2ed41f0a4a: Shape 17 landed a runtime rule with nothing requiring its authoring counterpart). Rewording a message re-keys the site (`--write` drops the stale entry and seeds the new one; carry the adjudication across). Never hand-edit a `key`. Sibling conventions landed with it: Stage 1 mirrors the runtime NAME/LABEL rules by calling the runtime's own validators (`_composer_node_id_validation_message`, `_routing_label_errors` -> `_validate_connection_or_sink_name` / `validate_composer_output_name`), so fixture node ids/labels must be runtime-valid (leading letter, <=38 chars, not `fork`/`continue`/`on_success`, sinks lowercase) — a UUID or a gate named `fork` now fails Stage 1 exactly as it fails `settings_load`; fix the FIXTURE, never relax the mirror. Coalesce `merge: select` and `policy: quorum` are rejected as unauthorable (no `select_branch`/`quorum_count` on NodeSpec); `best_effort` needs `timeout_seconds`. Cycle detection (`_node_topology_cycle`) is whole-graph.
  See [CONTRIBUTING: Gate: runtime-rejection parity](../../CONTRIBUTING.md#gate-runtime-rejection-parity).

- **2026-08-17 — a guided correction that writes a routing scalar must move its SINK MIRROR EDGE in the same materialization** (elspeth-a0a830fc95, elspeth-67b44040ee)
  Scalar routes are the runtime authority and sink-targeting edges are their mirror (elspeth-67b44040ee); the guided public `connections` projection derives every reviewable connection from the scalars and never reads `edges`. Every correction path in `guided/planning.py` (`materialize_guided_authorized_candidate`) therefore ends by calling `_reconcile_draft_sink_mirror_edges` — edges follow scalars, retargeted onto the slot's current sink or dropped when the slot no longer names one, and never invented. Add a new correction path and it must call that, or Stage 1 fails closed on `edge_route_mismatch` against a delta with no repair surface (deterministic REPAIR_EXHAUSTED). Two traps:
  1. The edge-correction arm is scoped to the SELECTED slot only, because `_edge_preserved_state_fingerprint` hashes the whole document and proves nothing else moved. The slot and its mirror are ONE authority, so that function REMOVES (never marker-substitutes) the slot's mirror edges on both sides; a wider sweep there is an undetected out-of-authority edit.
  2. Do NOT "fix" the inverse half by clearing a dropped producer's scalar. Measured 2026-08-17, every producer kind: transform `on_success=None` -> `transform_missing_on_success`, `"discard"` -> `transform_on_success_dangling`; deleting a gate route key -> `gate_route_labels_mismatch` (emptying `routes` -> `gate_missing_routes`); source `"discard"` -> `source_on_success_dangling`. There is no valid cleared value, so clearing converts a benign undrawn route into a rejection the delta cannot repair.
  The node arm sweeps ALL of the owner's slots (as `upsert_node` does), so a provider-authored edge patch retargeting a GATE route to a different sink is snapped back rather than honoured — correct, because `routes` is deliberately absent from the node-patch schema. Every pre-existing correction fixture used `edges=()`, which is why the suite could not see any of this; new fixtures live beside `_mirror_edge_correction_predecessor`.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-15 — commencement gate conditions have separate execution and audit forms**
  Only the raw configured expression enters `ExpressionParser`. Every `CommencementGateFailedError`, successful `CommencementGateResult`, and persisted preflight result carries the AST-derived rendering from `redact_commencement_gate_condition`: direct subscript/`.get()` string keys and dict keys stay visible for structural diagnosis, while every other string literal — including values nested inside a composite lookup key — becomes `<redacted-string-literal>`. Config admission catches only the parser's syntax/security rejection types and hides the raw Pydantic input; parser AST diagnostics can contain raw literals. Do not restore the raw condition in a downstream formatter or use heuristic secret-pattern matching; arbitrary literals are the protected class.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-15 — union collision audit provenance stops at field and branch identity**
  `_merge_data` must retain raw `collision_values` internally so `first_wins` can restore the configured branch's value, but `build_coalesce_merge` must never pass those values into `CoalesceMetadata`. Stable unsalted hashes and Python type names remain value-derived sensitive material: low-entropy candidates can be recovered offline and correlated across runs. Persist `union_field_collisions` and `union_field_origins`; keep `union_field_collision_values` absent from new `context_after_json` records.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-15 — value-source findings are response and log egress**
  Catalog-membership and sibling-derivation failures flow into the Composer `/validate` response, exception text, check-detail/log surfaces, model repair prose, and persisted composition-state `validation_errors`. Never render, summarize, hash, measure, or call `repr` on a configured value there: even a short ordinary-looking scalar or a derived length can disclose private configuration. Keep component, field, sibling-field, catalog, and remediation relationships in fixed structural prose so failures remain actionable without becoming an echo channel.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-15 — every `NodeStateGuard` caller names the scope it owns**
  `auto_fail_phase` is required, has no default, and is a closed runtime-and-static vocabulary because the guard spans materially different work: transform execution, gate evaluation/routing, aggregation flushes, and pre-attempt shutdown persistence. The value is durably recorded in `ExecutionError.phase` and shown verbatim to operators; a generic, falsey, or misspelled fallback silently creates false attribution. A new guard site therefore requires deliberate vocabulary extension plus caller-path tests. Keep explicit inner failure phases authoritative: once the caller has persisted a terminal state, the guard must stand down rather than overwrite it with an auto-generated failure.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-15 — engine span correlation belongs to the durable run lifecycle**
  Production engine spans are completion events emitted through the existing `TelemetryManager`; do not create a second tracer/provider lifecycle beside its exporter fan-out. A fresh run binds trace identity to persisted `Run.started_at`. Resume and follower paths use `SpanFactory.trace_scope(...)` and deliberately do not emit a second whole-run span or fabricate a leader parent. SQLite may return that timestamp without `tzinfo`; normalize it as UTC at the span boundary, but tolerate cross-host wall-clock skew. Row spans live at the universal scheduler-claim seam so fresh, resumed, and follower work share one path, and an operation span stays open through the engine's validation, authority, disposition, and terminal audit work — plugin return alone is not success. Row and aggregation parents are path-dependent: fresh ingestion may inherit source/row context, while late leader-drain, resume, and follower work uses durable run correlation. Correlate spans to Landscape through opaque run, node, token, and batch identities; audit-only row-content hashes must not enter engine span attributes. Row-event producers retain real hashes for in-process audit correlation, but `TelemetryManager` projects `RowCreated.content_hash` and `TransformCompleted` hashes to `None` before observers or exporters see them, and reconstructs exact owned base events so a subclass cannot smuggle sibling hash metadata across the boundary; never substitute a shared redaction marker that downstream could treat as a real hash. A handled exception already present in the caller is not a span-body failure, and a telemetry callback failure must not replace or rewrite an active workload failure. Exporters retain fresh-run trace origin until both `RunFinished` and the enclosing run span completion arrive, in either order; joined/resumed runs clear on their sole terminal event.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-15 — a selector lane may contain SEVERAL trusted profile probes**
  The state-engine profile reporter accepts repeated observations that agree on every profile-identity field (case, store, deployment, backend version, probe shape) and binds the FIRST probe test as the report's `deployment_probe`; it fail-closes only on disagreement. Do not "fix" a multi-probe lane by deleting probe tests or splitting the lane — the single-observation invariant is about one run claiming two DIFFERENT profiles. Discovered by the first full-lane single-invocation evidence run (Task 12); per-cohort runs never exercised two probes together. Also: evidence venvs must be built on the release interpreter (Python 3.13 — `ci.yaml` maintains 3.12/3.13); a bare `uv venv` picks the newest local Python (3.14) whose annotation semantics fail ~11 suite tests spuriously.
  See [CONTRIBUTING: Convention: test discipline](../../CONTRIBUTING.md#convention-test-discipline-fakes-mocks-fixtures).

- **2026-08-12 — a live-evidence artifact cannot authenticate its own upload digest**
  The final GitHub artifact/archive digest exists only after upload, so embedding it in `manifest.json` is circular, and self-declared hashes are not producer authentication. Ingestion selects the artifact through the read-only Actions API, downloads that API record's archive, verifies the API-reported digest over the downloaded bytes, safely admits the exact five regular members, and byte-compares them with the supplied directory. Reject duplicate/traversal/extra/encrypted/oversized or compression-bomb members. GitHub's archive endpoint redirects to a different origin: strip the bearer token on every cross-origin redirect, never forward it to the signed blob host.
  See [CONTRIBUTING: Convention: repository and process hygiene](../../CONTRIBUTING.md#convention-repository-and-process-hygiene).

- **2026-08-12 — PB-09 plugin variants are a three-way exact-set contract**
  `scripts/state_engine_plugin_matrix.py check` derives the closed variant set from production-owned Pydantic discriminators and registries, constructs every variant through real config validation, and compares the mechanical discovery projection with `tests/golden/state_engine/plugin_lifecycle_matrix.json`. The discovery suite separately pins live plugin keys, golden variants, and v3 PB-09 `(plugin_key, variant_id)` pairs. Adding a plugin or a supported auth/provider mode requires updating the reviewed golden fields and v3 PB-09 cases together; `render-skeleton` deliberately exits nonzero while any new reviewed field is `UNCLASSIFIED`. The golden is reviewed evidence, never variant authority.
  See [CONTRIBUTING: Gate: plugin inventories, source hashes, scenario-corpus manifest, fingerprint baseline](../../CONTRIBUTING.md#gate-plugin-inventories-source-hashes-scenario-corpus-manifest-fingerprint-baseline).

- **2026-08-12 — follower teardown has one exit seam and partial startup is tracked explicitly**
  `FollowerProcessor.run()` stops its heartbeat before departing the single-use worker on every exit, including unexpected traversal exceptions. Do not add a new exception arm that departs early or bypasses the common `finally`; exact-once departure and stop-before-depart ordering are pinned. CLI follower startup records a transform or sink only after its `on_start()` returns; pass those exact started subsets to `cleanup_plugins`, and never call `on_complete()` or `close()` on the plugin whose startup raised, or on later plugins that were never started. `cleanup_plugins` also requires an explicit `pending_exc`: preflight and startup `except` arms pass the bound exception they re-raise, while steady-state and follower scopes initialize a local to `None` and set it only from `except BaseException` around the exact scope whose exception leaves that boundary. A normal return passes `None`, even when the boundary was invoked inside an outer handled `except`. Never derive cleanup policy from `sys.exc_info()` or `sys.exception()` in the helper or a `finally` caller. This explicit input does not change Tier-1 cleanup precedence, lifecycle callback ordering, or exact partial-startup subset cleanup.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-12 — Python 3.14 annotation closures expose class namespaces to Runtime-VAL**
  PEP 649 `__annotate__` functions close over `__classdict__` and use `LOAD_FROM_DICT_OR_GLOBALS`; never normalize that whole dictionary, because it contains unrelated interpreter state such as `_abc_impl`. Normalize only the exact names read by supported bytecode shapes, including whether each binding resolves from class, module globals, or builtins, and fail closed on any unrecognized dictionary use. Slot member descriptors bind by exact declaring `module:qualname` plus descriptor name. Python 3.14 also emits `slice` objects as code constants, so preserve all three normalized bounds rather than falling back to repr or narrowing supported Python.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-15 — adding a field to the composer spec/tool contract fires THREE pins, and the third is a PRODUCTION wire decoder, not a test** (elspeth-b48212113e, 7694f5f1b, 80fa17fed)
  Any new field on SourceSpec/NodeSpec/OutputSpec or a composer tool argument model must ALSO land in:
  1. The `canonical-field-inventory` table in `src/elspeth/web/composer/skills/pipeline_capabilities.md` (`test_capability_skill_identity` derives the real schema and diffs the table).
  2. The redaction-policy snapshot — regenerate via `scripts/cicd/bootstrap_redaction_snapshot.py --write` and review that only hashes moved, never `sensitive_path_count`, unless a new Sensitive path was intended.
  3. The frontend's strict guided wire decoder (`frontend/src/api/guidedDecoder.ts`, `decodeCompositionState`), whose `exactRecord` key lists reject any unenumerated key AT RUNTIME. Missing this is invisible to every backend suite and to frontend tests that stub `composition_state: null`: the first guided re-plan after deploy emits the new key and every `/guided` response becomes "received but could not be read" while the server keeps returning 200 (elspeth-b48212113e, fixed 7694f5f1b). Grep the frontend for `exactRecord` lists naming sibling keys before calling a wire-contract change done.
  Serialise optional spec fields as omitted-when-None so pre-existing persisted states and their `composition_content_hash` values stay byte-identical (see `description`, 80fa17fed). The guided planner's advertised full-document schema derives from the registered `set_pipeline` JSON schema via `canonical_set_pipeline_schema()`, so extending that schema plus the redaction.py models covers the planner lane automatically.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).
- **2026-08-09 — Gate: attribute contracts (dynamic-attribute sites)**
  `tests/unit/web/test_sessions_composer_attribute_contracts.py` pins the EXACT set of `getattr` / `hasattr` / `getattr_static` / `__getattr__` sites in `src/elspeth/web/sessions` and `src/elspeth/web/composer`. The contract: only ADR-032 LiteLLM admission boundaries may use `getattr` — the `_admit_*` parsers and `_capture_composer_llm_completion_fields`. Adding any dynamic attribute access anywhere under those trees fails the gate repo-wide. Owned type (a class ELSPETH defines): use direct attribute access, and make an optional attribute a real field with a default rather than probing for it. Genuinely parsing an object ELSPETH does not own: that is a Tier-3 admission boundary — sentinel `getattr` plus value asserts plus construction of an owned type — and the gate's expected set must be deliberately extended.
  See [CONTRIBUTING: Gate: attribute contracts](../../CONTRIBUTING.md#gate-attribute-contracts-dynamic-attribute-sites).

- **2026-08-09 — Gate: masquerade sites (tests included)** (elspeth-de6f571887, elspeth-02cd60d8cd, elspeth-682e0c6581, elspeth-f1def53d38, elspeth-2a72512454)
  `tests/unit/elspeth_lints/test_masquerade_gate.py::test_live_tree_has_zero_unbaselined_findings` scans the WHOLE repo — tests included — for unadjudicated `getattr` sites against `config/cicd/masquerade_baseline.yaml`. Traps that have fired:
  1. Parametrizing a test by attribute NAME and resolving with `getattr(module, name)` trips it. Parametrize with the objects directly and keep readable IDs via `pytest.param(..., id="...")` (see `tests/unit/web/composer/test_no_tool_policy_segments.py`).
  2. A `getattr(obj, "x", None)` "just to be safe" on an owned type trips it; the safe-looking default hides AttributeError and lets masqueraders pass. Rewrite to direct access, and if a test fake breaks, fix the FAKE to model the real contract, never the production code to tolerate the fake.
  3. Baseline entries bind a sorted `probe_shapes` fingerprint for every occurrence, not only `(path, qualname, kind)` and a count, so a one-for-one rewrite (literal field to dynamic reflection, receiver/default change, imported alias rebinding) deliberately fires `probe-shape-drift` even when key and count are unchanged. Refresh with `python -m elspeth_lints.rules.masquerade.seed_baseline`, which preserves an existing classification/justification only when key, count, and shapes all still match and resets changed or new subjects to `unadjudicated`. Do not hand-edit the fingerprints.
  4. Probe classification resolves `builtins.getattr` / `builtins.hasattr` and `inspect.getattr_static` through imports, lexical shadowing, reassignment, comprehensions, possible-target control-flow joins, and deferred module bindings. Abrupt-only paths do not pollute the reachable binding, but any reachable builtin target is still inventoried; aliasing a builtin is not an escape hatch, and a rebound `@trust_boundary` source parameter no longer receives boundary amnesty.
  5. Assignment targets are executable syntax: attribute receivers, subscript containers and indices (including slices), and target-side named expressions must be inventoried for ordinary/annotated assignments, `for`/`async for`, `with`/`async with`, and comprehensions. Preserve CPython order with the shared target walkers; for chained or destructured assignment, freeze RHS binding/source evidence once before the first target store, because re-resolving after a target-side walrus creates paired false positives and false negatives.
  Do NOT rewrite the probe resolver — decided 2026-08-09, re-walked by accident and re-confirmed 2026-08-16. Two full attempts were built and rejected by independent review: a partial-CPython state model (Freezes 1-5, 5,216 lines, never merged) and a sparse-SSA definition/phi/value-graph solver (Freeze 6, whose rejection found late-global, try/match, annotation-timing, loop-header, star-import and callee/argument misses plus non-monotone `PROJECT` output and `CALL_RESULT` role collision, while its adversarial scaling was fine). The systems review classified Freezes 1-5 as a Fixes-that-Fail / Limits-to-Growth loop. `inventory.py` is the single authority, and the standing stop rules (elspeth-02cd60d8cd) are: no CFG/SSA/history/replay/lazy-cache/object-emulator growth, and <=2.5x runtime per input doubling. New semantic coverage lands as narrow, ticket-level RED tests against the existing visitor — open siblings elspeth-682e0c6581 (definition-header replay), elspeth-f1def53d38 (PEP 695/696 scopes), elspeth-2a72512454 (destructured RHS alias evidence). That entire history exists ONLY in `filigree get-comments elspeth-de6f571887` (a CLOSED issue); read that comment stream before touching resolution.
  See [CONTRIBUTING: Gate: masquerade sites](../../CONTRIBUTING.md#gate-masquerade-sites-tests-included).

- **2026-08-16 — Corpus agreement cannot validate a probe-resolver change**
  A candidate resolver matched the shipped one on all 2,866 files (474/474 sites, zero false positives) while still being fail-open on late-global. The evasion shapes are absent from a tree that currently passes the gate, so the oracle must be adversarial hand-written cases; a corpus run only answers "would today's findings change".
  See [CONTRIBUTING: Gate: masquerade sites](../../CONTRIBUTING.md#gate-masquerade-sites-tests-included).

- **2026-08-16 — Two probe-resolver fail-open holes closed in place; the mechanisms are now conventions** (elspeth-34ac84b4b6, elspeth-682e0c6581, elspeth-f1def53d38, elspeth-2a72512454)
  1. *Loop heads are a fixpoint.* `_loop_head_bindings` / `_comprehension_head_bindings` join the body's reachable states back into the loop head until stable, so a probe used before its rebind inside a `for`/`while`/comprehension is visible on iteration 2+, `continue`/`break` carried states count, and boundary provenance is dropped for any name the body can rebind. It terminates only because binding targets are finite: `_MAX_TARGET_DEPTH` collapses any dotted target deeper than 8 segments to `<shadowed>`. Never build an unbounded target string — `node = node.next` in a loop hung the whole-tree scan before that cap existed.
  2. *Resolution keeps evidence.* `_resolve_binding_expression` no longer returns an empty set for an unmodelled shape: every *uncalled* probe reference inside it (`wrap(getattr)`, `partial(getattr, o)`, `[getattr]`, `probes[0]`, `for p in probes`) flows out beside the `<shadowed>` marker, so the eventual call is inventoried. A *called* probe contributes nothing, since the call itself is the site. Consequence: passing `getattr`/`hasattr`/`getattr_static` as a value is inventoried where the carrier is called — do it only where a baseline entry is acceptable.
  3. *Definition headers replay in CPython order.* `definition_header_expressions` is the single evaluation-order authority for the live visitor and the deferred projection: decorators, positional then keyword defaults, then — outside `from __future__ import annotations` — signature annotations in CPython's order (`args` BEFORE `posonlyargs`, vararg, kwonly, kwarg, return); classes: decorators, bases, keywords. Variable annotations execute after the value is stored, only outside function bodies, and never under PEP 563 (`_ExecutionContext`); unexecuted annotations are still inventoried (dead-code style, deferred bindings for stringized ones) but their walrus effects never touch the live state. Default captures happen at each default's own evaluation point. Modelled semantics are 3.12/3.13; 3.14 (PEP 649) defers annotations and rejects a walrus inside one. `test_definition_header_expression_order_matches_the_running_interpreter` pins the enumerator to the interpreter and must `compile(..., dont_inherit=True)`, because the test module itself imports `annotations` from `__future__`, which `compile` otherwise inherits.
  4. *PEP 695/696 annotation scopes are modelled* (elspeth-f1def53d38). Bounds, constraints, PEP 696 defaults and `type` alias values are LAZY: inventoried against the deferred bindings, with the declared self-name bound and — when the scope is immediately inside a class body — the class dict consulted first (position-aware: `_ClassBodyCursor` projects the body once and suffix-joins, so states from the def onward count and names the class bound before it shadow the globals). Generic annotations and class bases/keywords are EAGER inside the annotation scope (type params shadow, the enclosing class IS visible); generic *defaults* evaluate in the enclosing scope (type params do NOT shadow them, a walrus there binds outside). Type params are closure cells of the body and every nested scope (`_push_binding_scope` shadows every active one and skips `"annotation"` frames like `"class"` frames). A walrus is a SyntaxError in bounds/annotations/alias values, so the projection needs nothing. 3.12 = 3.13 on all of this (verified with an interpreter oracle); do not add a lazy-force cache, a CFG, or a second evaluator to "improve" it.
  5. Still open in this family: elspeth-2a72512454 (destructured literal RHS timing). Not modelled and not claimed: `getattr.__call__(...)`, `a, b = wrap(getattr)` (destructuring a laundered value), reflective laundering (`getattr(builtins, "getattr")` — the outer call is itself a site).
  See [CONTRIBUTING: Gate: masquerade sites](../../CONTRIBUTING.md#gate-masquerade-sites-tests-included).

- **2026-08-16 — `probe_shape` is invariant under a resolver change** (elspeth-df09888129)
  `probe_shape` is computed from the AST node and kind, neither of which passes through resolution (verified: 354 distinct / 474 total digests, zero differing files), which is what makes resolver work safe to attempt at all, since drift would reset the baseline's 39 human adjudications. Land the digest comparison as a test before changing anything; the 2026-08-16 change was measured that way — 488/488 sites, zero digest/kind/amnesty drift, whole-tree 65.0s to 64.8s after a two-state fast path in `_join_binding_states`. Cost context (2026-08-29): one whole-tree scan is ~38s CPU (was ~60s, after removing the per-candidate re-walk elspeth-df09888129, an eager per-function deferred projection, a doubled If/Try branch projection, and a full alias-visitor pass per file, with an identical site list as the oracle), and `test_masquerade_gate.py` shares one scan across its read-only live tests via the module-scoped `live_sites` fixture — the `scan_root` gate and the seeder-agreement test keep independent scans on purpose. The remaining multiplier is `_suite_transfer_kinds` re-evaluating nested suites once per enclosing statement (~18% of a scan), a property of the shared alias evaluator's control-transfer semantics, not a local fix. The per-statement state copy in the possible-bindings model is itself quadratic in suite length (~3-4x per input doubling on flat 1600-statement inputs, before and after); the stop rule is about not making that class worse, and any loop-head work multiplies it by the fixpoint pass count.
  See [CONTRIBUTING: Gate: masquerade sites](../../CONTRIBUTING.md#gate-masquerade-sites-tests-included).

- **standing — Gate: trust-tier lint corpus** (elspeth-13f0cc04fb)
  `elspeth-lints check --rules all --root src/elspeth` is fail-closed (exit 1, ~3.1k-line corpus, tracked as elspeth-13f0cc04fb). Do not expect zero and do not try to clear it. The obligation: capture the corpus BEFORE the change, capture it AFTER, and diff — nothing may be added. Never hand-edit a `judge_metadata_signature`; never shape code to reduce signature churn. Exception, release closeout (2026-08-17, 0.7.2): the "do not clear it" rule scopes to ordinary feature work, the same qualifier AGENTS.md uses; when a release package is being made ready for merge, clearing the corpus IS the work, the operator lifts the standing ban and signs at the end, and the corpus is a worklist rather than a fixed backdrop. Two things never relax: no hand-edited signature, no code shaped to reduce churn. Scale measured 2026-08-17 — the tier_model allowlist held 606 entries, 351 requiring action (178 `NO_MATCHING_FINDING` orphans, 127 `AST_PATH_BINDING_DRIFT`, 39 `IDENTITY_PREFIX_REPLACEMENT`, 35 `PRE_JUDGE`, 6 `SCOPE_BINDING_DRIFT`, 1 `SOURCE_FILE_MISSING`; 229 of them in `web.yaml`). Those binding failures are INVISIBLE to `check --rules trust_tier.tier_model`, which reported only 6 per-file `max_hits` overflows — use `mcp__elspeth-judge__verify_signatures` for the signature-health surface, remembering it is shape-only without the key. Stage the bundle LAST: bundles are exact-source-bound to Git HEAD plus a digest of every scannable file, so any sibling edit that shifts an AST position invalidates one already staged.
  See [CONTRIBUTING: Gate: trust-tier lint corpus](../../CONTRIBUTING.md#gate-trust-tier-lint-corpus).

- **2026-08-08 — Gate: wire-shape templates** (elspeth-2ed41f0a4a)
  The wrapped-diagnostic producer templates and `_split_wrapped_diagnostic` in `src/elspeth/web/composer/no_tool_policy.py` derive from ONE `_wrapped_diagnostic_wire_shape` source, and a round-trip test pins every template. Do not hand-assemble a SEPARATOR/MARKER/header/footer suffix; add new templates through `_wrapped_diagnostic_template`. Two corrections (2026-08-09, elspeth-2ed41f0a4a), plus a corollary:
  1. The round-trip test's case list is HAND-MAINTAINED, so until now a template added without an entry was simply never exercised — the claim that it "fails" was false. `test_the_round_trip_parametrization_covers_every_` `wrapped_template` now AST-scans the module and fails when the list is incomplete; add the entry when adding a template.
  2. Building the suffix through a template is only HALF the contract. A backend-authored suffix must ALSO be registered in `_canonical_trusted_suffix_segments`, with a matching `_split_wrapped_diagnostic` arm. Registration in the `_AugmentationBranch` literal governs the PREFIX invariant only, not the segment recognizer — miss it and `visible_message_segments` fails closed to one `AssistantTextSegment`, publishing an operator-facing notice as MODEL PROSE, silently: `enforce_augmentation_prefix_invariant` still passes.
  3. Corollary: exactly ONE backend suffix per message. Two concatenated canonical suffixes match no recognizer arm, so stacking a second announcement onto an already-augmented `ComposerResult.message` demotes BOTH disclosures. Rebuild from `raw_assistant_content` and fold the other fact into the single suffix's `Cause:` region.
  See [CONTRIBUTING: Gate: wire-shape templates](../../CONTRIBUTING.md#gate-wire-shape-templates).

- **standing — Gate: declared oracles pin OUTPUT bytes**
  Several suites pin content hashes, golden files, and byte-exact corpora (for example the `*-lost-c` branch-loss oracles). A behavior-preserving refactor to a producer can still change pinned bytes. Grep for hashes and golden files near what is touched, or run the full suite.
  See [CONTRIBUTING: Gate: declared oracles pin output bytes](../../CONTRIBUTING.md#gate-declared-oracles-pin-output-bytes).

- **2026-08-09 — Gate: new-plugin exact inventories, source hashes, and catalog pins** (d181ee569, 0ec120e2d, b288157c3, da5838874, 9fa971fc1, 4c205e6fb, f36e6968d, elspeth-5e2068854c)
  Adding any builtin plugin fires a fixed set of whole-tree exact pins. For a new TRANSFORM the full list (all hit while landing `aws_textract_inline_analysis`, d181ee569):
  1. `tests/unit/plugins/test_discovery.py` `EXPECTED_TRANSFORM_COUNT`.
  2. `tests/unit/plugins/test_catalog_reference_content.py` — total reference count, per-kind `Counter`, `EXPECTED_BUILTIN_IDENTITIES`, and (for a non-profiled plugin) the `DIRECT_CONFIG_REFERENCES` count.
  3. `tests/unit/plugins/transforms/test_external_catalogue_metadata.py` — an EXTERNAL_CALL/NON_DETERMINISTIC transform must appear in `EXPECTED_EXTERNAL_TAGS` (exact tuple) and `_REQUIRED_GUIDANCE` (casefolded substrings of the usage strings). When it surfaces externally-controlled text, declare `content_trust = ContentTrust.UNTRUSTED`; the guidance test derives its producer set from that declaration ("untrusted before llm" must appear in its guidance).
  4. `tests/unit/plugins/test_validation_path_agreement.py` — any config with a `@model_validator` needs a rejection case in `_TRANSFORM_REJECTION_CASES`.
  5. `tests/unit/web/catalog/test_service.py` serialized-summary total and the knob-schema golden `tests/golden/web/catalog/knob_schema/<kind>__<name>.json` (generate via `CatalogServiceImpl._schema_cache`).
  6. `config/cicd/contracts-whitelist.yaml` for `__init__:config` / `probe_config:return` `dict[str, Any]` params (pre-commit Check Contracts).
  7. `capability_tags` gate: a tuple of 2-6 lowercase kebab tags; a 7th tag fails.
  8. `PluginAssistance` text is scanned for credential-shaped patterns — "…token: SDK…" trips `token\s*:`, so phrase around it.
  9. An untrusted-content producer declares `content_trust = ContentTrust.UNTRUSTED`; Composer prompt-shield admission derives the closed producer vocabulary from registered transform declarations.
  10. Pin `source_file_hash` LAST (ruff/format edits restale it) via `scripts/cicd/plugin_hash.py`.
  Sites missed on a first pass landing `pdf_rasterize` (2026-08-25), to be added to the checklist: `tests/unit/web/catalog/test_service.py:60`, where the serialized-summary total is a bare `sum(...) == N` int literal next to the per-kind counts; `tests/unit/architecture/test_state_engine_catalog_contract.py:35` `V2_CATALOG_SHA256`, a whole-catalog proof hash to rotate LAST, only after every other v2/v3 catalog pin for the new plugin has landed (`:174-175` is what it pins against), since rotating early means rotating twice; `tests/invariants/test_input_schema_config_is_captured.py:80-87` `_EXPECTED_MUTATION_REJECTIONS` and `tests/invariants/test_transform_input_contract_is_satisfiable.py:85-92` `_EXPECTED_ARMING_REJECTIONS`, two separate allowlists both subset-asserted against the live registry, where an unlisted rejection HARD-FAILS, so a new plugin whose config rejects the synthetic mutation/arming probe must be added to BOTH; `config/cicd/contracts-whitelist.yaml` entries in both the `probe_config:return` block (`:173-177`) AND the constructor block (`:216-222`), the constructor entry's trailing segment matching the ACTUAL `__init__` parameter name (e.g. `options`, not the generic `config`); and the Python-to-TS acronym mirror, which has NO parity test — `web/composer/guided/_display.py` `_ACRONYMS` and `web/frontend/src/components/catalog/pluginDisplayName.ts` `ACRONYMS` must each be hand-edited, since a missing entry (e.g. `"pdf"`) humanises the name wrong ("Pdf Rasterize") with no test catching it.
  Sources have the same shape (see 0ec120e2d for the blob_rows list: source count/names, registry, catalog, golden, contracts whitelist). Sites missed again landing `blob_json_expand` + `blob_text_expand` (2026-08-26, b288157c3), in neither list above: `scripts/state_engine_plugin_matrix.py` `EXPECTED_COUNTS` and `EXPECTED_VARIANT_COUNT`, plus `tests/golden/state_engine/plugin_lifecycle_matrix.json` and the PB-09 cases in `docs/architecture/state_engine/proof-catalog/v3/catalog.json`; `src/elspeth/web/audit_readiness/boundary_expectations.py` `EXPECTED_TRANSFORM_DETERMINISMS`, where touching the file fires a pre-commit cohort gate demanding the trailer `telemetry-backfill: audit-readiness` in the commit message; and `tests/unit/plugins/test_state_engine_plugin_matrix.py` default-variant subjects.
  The lifecycle-matrix metadata is DERIVED, not chosen — do not guess the five fields to force green. Each is forced independently by the existing corpus: PB-04 is batch-transforms only (13/13 members), so every row-transform carries exactly `("PB-02","PB-09")`; every non-hermetic `local_fixture` (`provider-contract-fake`, `real-process-http`) pairs with an `external_call` or `non_deterministic` plugin, so an `io_read` transform has no external call to observe and gets `hermetic` / `local` / `external_observation_required: false`. Cross-check against the structural twins (`blob_csv_expand`, `pdf_rasterize`). Do NOT derive this by clustering: grouping the transforms by the reviewed tuple gives EIGHT clusters and no cluster is purely single-variant `io_read` row-transform — the one holding the twins also holds deterministic row-transforms, so a cluster-selection argument lands on the right answer for the wrong reason. The golden authored cannot discriminate this; only the field-by-field derivation can. The v3 PB-09 *cases* are a substitution, not a judgement: all existing entries are byte-identical in `cell_applicability`, so clone a twin's case and swap the key.
  The inventory update is a MIRROR sweep across BOTH catalogs, never a v3-only edit (the reference_join sweep, 9fa971fc1, is the exact template; pdf_rasterize's da5838874 is the same procedure). An earlier version of this guidance forbade the inventory edit entirely on the premise that v2 is byte-frozen; that premise is false — `V2_CATALOG_SHA256` is a deliberate-change ratchet that has rotated on every plugin addition (4c205e6fb, da5838874, 9fa971fc1), and the tests have required v2 to match live discovery since the original contract (f36e6968d). The impossibility described is real only for the v3-ONLY edit, which diverges the byte-identical `execution_profiles` blocks and trades one red for another (`test_v3_transition_is_lossless_outside_pb09` is the lockstep guard working). The sweep, in dependency order: add the plugin to `first_party_plugins` and PB-09 in BOTH v2 and v3 (surgical text edits — a `json.dumps` round-trip reflows v2 and silently re-sorts PB-09, which is kind-grouped); clone the twin's `evidence_selectors.json` lane node_ids + 40 cells; re-derive `CANONICAL_V2_LEGS_SHA256` then `CANONICAL_V2_EXECUTION_PROFILES_SHA256` (`scripts/state_engine_assessment_lib/common.py`) via the validator's own `_semantic_sha256`; move the `== N` case-count pin; rotate `V2_CATALOG_SHA256` LAST. One constant satisfies both catalogs because the mirror keeps the blocks byte-identical. elspeth-5e2068854c remains open only for the residual cleanup (the constant is misnamed `V2_*` while also gating v3, and its error text says "identity revision" where practice is rotation in place).
  Discovery reads the MAIN CHECKOUT from inside a worktree — the AGENTS.md venv trap running in the READ direction. A sweep of these counts can transiently see the sibling branch's plugin set (observed 2026-08-26: the worktree's own two new plugins absent, a plugin that exists only on another branch present). Hash-bracket the run and re-read at a settled state; a count "corrected" to a transient value was never real.
  See [CONTRIBUTING: Gate: plugin inventories, source hashes, scenario-corpus manifest, fingerprint baseline](../../CONTRIBUTING.md#gate-plugin-inventories-source-hashes-scenario-corpus-manifest-fingerprint-baseline).

- **2026-08-11 — Gate: CSS barrel structure**
  Every custom property referenced with `var()` must also be defined in a stylesheet; an inline React style does not satisfy the whole-tree token gate. Do not add a standalone `@media (forced-colors: active)` block before the canonical final block in `styles/themes.css`: the whole-barrel contrast gate treats the first block as canonical and will inspect only that partial corpus.
  See [CONTRIBUTING: Gate: CSS barrel structure](../../CONTRIBUTING.md#gate-css-barrel-structure).

- **2026-08-12 — Gate: Playwright auth state is worktree-global**
  Never run two Playwright commands concurrently in the same worktree. Global setup rewrites the shared `tests/e2e/.auth/user.json`; distinct backend and frontend ports do not isolate that file, so otherwise independent runs can corrupt each other's authenticated state. Run every Playwright suite sequentially per worktree.
  See [CONTRIBUTING: Gate: Playwright auth state](../../CONTRIBUTING.md#gate-playwright-auth-state).

- **2026-08-29 — Gate: whole-tree gates walk through `tests/helpers/tree_gate.py`, and scratch goes in `tests/_scratch/`**
  A sibling session dropped a throwaway `test_zz_scratch_repro_*.py` into `tests/unit/web/composer/` and deleted it while another session's full suite was mid-run. The hasattr gate had already enumerated it via `rglob` and died on the read with a bare `FileNotFoundError` naming a file nobody committed. Every one of the ~25 whole-tree gates had the same exposure, which produced the two rules below.
  See [CONTRIBUTING: Gate walker and scratch directory](../../CONTRIBUTING.md#gate-walker-and-scratch-directory).

- **2026-08-29 — Never write a private Python-file walk under `tests/`**
  Enumerate with `iter_gate_files(root)` / `iter_gate_sources(root)` from `tests/helpers/tree_gate.py`. They derive from the lints exclusion authority (`ast_walker.iter_python_files`), subtract everything git ignores so local gates measure exactly the tree CI measures, and raise a NAMED `TreeNotFrozenError` (file vanished mid-walk, so the run is void — use a worktree) or `GateSourceError` (undecodable / unparseable) instead of skipping. `test_python_file_walker_authority.py` pins all of `tests/` against the literal `rglob("*.py")` / `os.walk(` — including in docstrings and comments — so a fresh walk, or a mention of one, turns that test red. A walk that is genuinely not a Python-file walk (the conftest directory fingerprint) goes in its `_NOT_PYTHON_FILE_WALKS` allowlist with a reason.
  See [CONTRIBUTING: Gate walker and scratch directory](../../CONTRIBUTING.md#gate-walker-and-scratch-directory).

- **2026-08-29 — Scratch repro tests live in `tests/_scratch/` and nowhere else**
  It is gitignored, pytest still collects and runs it, and no gate can see it, so a scratch file appearing or vanishing mid-run cannot crash or skew a gate on the shared checkout. A scratch file anywhere else under `tests/` IS a gate input from the moment it exists.
  See [CONTRIBUTING: Gate walker and scratch directory](../../CONTRIBUTING.md#gate-walker-and-scratch-directory).

- **2026-08-29 — A SKIPPED ledger row is not a verdict: never read `ValidationCheck.passed` alone for a check the ledger may not have reached** (elspeth-fa18d54eef)
  `execution/validation.py::_skipped_checks` emits every check DOWNSTREAM of a halted stage as `passed=False` with `outcome_code=CHECK_OUTCOME_SKIPPED_AFTER_FAILURE` — `advisor_signoff` included — so every pending-handoff strict preflight carries a "failing" advisor check that means NEVER EVALUATED. Reading it as a failure published the "advisory review did not clear" notice over a CLEAN advisor verdict (elspeth-fa18d54eef; live in three sessions before the telemetry caught it). Dispatch through `execution/completion_gates.advisor_signoff_check_failed` (skipped-aware), or for a new check name discriminate on `outcome_code` directly. The companion trap is FIXTURE DIVERGENCE: `_handoff_result()`-style hand-built ValidationResults with `checks=[]` pin a shape `validate_pipeline` never emits (the real producer appends the skipped tail), which is why seven scripted reproductions missed a bug three live sessions hit. When a consumer dispatches on checks, give the fixture the producer's skipped rows — `_producer_honest_handoff_result` in `tests/unit/web/composer/test_advisor_terminal_publication.py` is the worked example.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-29 — Advisor-cohort terminal copy carries a SHARED withheld-prose disclosure, and every publication site is attributed** (elspeth-fa18d54eef)
  `no_tool_policy.ADVISOR_PROSE_WITHHELD_PUBLIC_DISCLOSURE` is appended to all six advisor-cohort terminal messages (`_ADVISOR_SIGNOFF_PENDING_NOTICE`, `_ADVISOR_SIGNOFF_PENDING_HANDOFF_NOTICE`, `ADVISOR_REPAIR_SUCCESS`/`REVIEW`/`REVIEW_WITH_FINDINGS`/`UNVERIFIED_PUBLIC_MESSAGE`). Edit the disclosure ONCE there, never fork per-message copies, and keep it OUT of `compose_preflight_failure_message`, whose chrome is shared with non-cohort turns where prose is not withheld. The finalize suffixes and the `visible_message_segments` recognizer derive from the same constants, so extending a notice keeps trusted chrome minting by construction; a hand-copied suffix string anywhere else breaks `test_advisor_terminal_publication.py`. Separately, every advisor-cohort terminal publication emits `composer.advisor_terminal_publication` (`record_advisor_terminal_publication`, closed branch + preflight-shape vocabularies, best-effort per the signed telemetry_phase8 posture, elspeth-fa18d54eef): adding a publication branch to `_replace_advisor_repair_public_result` or a new blocked terminal means adding its branch literal and emit, since an unattributed publication site re-opens the forensic hole this closed.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-29 — A `@trust_boundary` whose `source_param` is a `Path` DOES suppress the reads of the document that function opens itself, and the try-join rule is about where the READ sits, not whether the handler raises**
  Measured in B27 on `web/_aws_ecs_acceptance/`, 215 findings, with the rule's own `scan_file_with_observations`. Four facts that each cost a lane time:
  1. `source_param="path"` works. `receipt = _read_protected_document(path, check=...)` leaves `receipt` derived, because assignment propagation uses the PERMISSIVE subtree scan (`_value_depends_on_boundary` to `_expr_contains_derived_reference`) rather than `subject_is_rooted`, so a value passing through a call still carries the trail. Both `evidence.py::_validate_evidence_export_receipt` and `::_reverify_bound_evidence_export_receipt` went to zero findings this way. This is the same shape `ecs_metadata.py::fetch_task_identity` already uses with `source_param="raw_uri"`: the parameter names the external SOURCE and the function fetches the bytes itself. Do not split such a function into a caller-reads/boundary-parses pair on the assumption that a call launders the trail — measure first.
  2. The try-join rule stated correctly: a name bound in a `try:` body is derived for reads INSIDE that try and loses derivation only AFTER it, because `_visit_try_like` intersects the body-end and handler-end snapshots. It has nothing to do with whether the handler raises or returns. The remedy is the shape `fetch_task_identity` and `_trace_documents` already use in this package: put the shape check inside the same `try` and `raise ValueError` there, letting the single existing `except` translate it to the domain error. That also removes the wart of one function rejecting malformed input through two mechanisms.
  3. Every `@trust_boundary` argument must be a LITERAL. A non-literal (a lane probed with `"0"*64`) emits an `R_TB_NONLITERAL` diagnostic and voids THAT decorator's suppression — its function surfaces its R5/R1 again while a sibling literal boundary in the same file stays fully suppressed (`parse_trust_boundary_decorator` returns `(None, diagnostics)` per call; re-measured by the W4 audit, `auditW4B/probes/web/p4_nonliteral.py`). It is reported, not silent, but easy to miss in a large corpus. And `@classmethod` must be the OUTERMOST decorator; `@trust_boundary` above it raises `TypeError: <classmethod(...)> is not a callable object` at import.
  4. `test_fingerprint` survives `ruff format`. It is `sha256(ast.dump(..., include_attributes=False))` of the referenced test, so line joins and rewraps are free — verified by recomputing after the format hook rewrote two of the pinning tests and getting the identical hash. Editing the test's statements still rotates it.
  Corollary for burn-down lanes: `.get()` on a mapping the function BUILDS ITSELF (an accumulator proven `(str, str)` by the loop above it) is not a Tier-3 read at all. `name not in observed or observed[name] != values[name]` is exactly `observed.get(name) != values[name]` whenever the right-hand side cannot be None — prove that side before converting, and say how it was proved.
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-29 — What a `@trust_boundary` suppression walk actually follows (MEASURED; it CORRECTS the guidance in circulation)**
  Three throwaway probe modules scanned under a private `--root` with the allowlist disabled (tier-B26, Wave 4) settle what `source_param` propagates through. Read the KEEP/LOSE table before placing a boundary; guessing costs a redesign.
  1. KEEP: `x = helper(param)` — a helper's RETURN VALUE does carry the trail. `_assign_targets_from_value` marks a target derived when the RHS *mentions* a derived name (`expression_depends_on_current_names`, a whole-subtree scan), NOT via `subject_is_rooted`. Earlier lane briefs said helper returns lose the trail; that is wrong. It is why `_validate_tf_binding_receipt(path, ...)` can carry `source_param="path"` while the document arrives as `_read_protected_document(path)`.
  2. KEEP: `json.loads(param["k"])` outside a try; `for item in derived:`; comprehension and genexp generators over a derived name or subscript; branched assignment (`if: x = p["a"] else: x = p["b"]`); `list(...)` and `cast(...)`-style wrapping of a derived value.
  3. LOSE: a name assigned inside a `try:` body and read AFTER the try. The visitor intersects derived-name snapshots across the handler paths and the handler does not assign the name — true whether the handler raises, returns, or reassigns. Reading the name INSIDE the same try keeps it. This is why both exemplars (`ecs_metadata.fetch_task_identity`, `operator_telemetry.AWSOperatorTelemetryQueries._trace_documents`) wrap the WHOLE parse in ONE `try:`, `raise ValueError` internally, and convert in the handler. That shape is not stylistic; it is what keeps the trail. Write every new boundary that way.
  4. LOSE: nested `def` / closure bodies (`iter_own_scope` stops at nested scopes) — hoist the closure to a module-level named projection with its own boundary.
  5. LOSE: `zip(...)` and `enumerate(...)` loop targets — the iter is a `Call` whose func is a bare `Name`, so `subject_is_rooted` is False. Do NOT drop `strict=True` or rewrite into index arithmetic to make them rootable; that trades a real guarantee for a hidden finding. Rationalise those sites instead.
  6. LOSE: `cast(T, derived["k"]).get(...)` — `subject_is_rooted` descends `Call.func`, which bottoms out at `Name("cast")`. A `cast` in the receiver position severs the trail even though a `cast` on the RHS of an assignment does not.
  7. Mechanics before writing the decorator: `suppresses=` may only ever name R1 and R5, and the honest minimal set is house style (`("R1",)` and `("R5",)` are both common in the tree). A boundary that returns a sentinel instead of raising is an `observation_boundary`, takes no `test_ref`/`test_fingerprint`, and needs no test, but the gate mechanically proves no `raise` in its body is control-dependent on a derived guard. NEVER hand-compute `test_fingerprint`: write the decorator without one and paste the value `elspeth-lints check --rules trust_boundary.tests` reports. A `test_ref` must resolve to a test whose OWN body holds the `pytest.raises`, calls the decorated function directly through `source_param`, and names an exception the `invariant` prose also names. Pointing two boundaries at one shared parametrized test works, but any lane that later adds a param case to it will trip `R_TB_TESTS_FINGERPRINT_MISMATCH`.
  8. One shape that looks like a fix and is not: extracting a `_decode_or_none(raw)` helper to escape the try-body loss introduces a NEW R6 whenever the helper's handler returns a bare `None`. Either move the reads into the existing try, or give the extracted helper a raising contract (tier-B26 measured both).
  See [CONTRIBUTING: Convention: trust-tier rules](../../CONTRIBUTING.md#convention-trust-tier-rules).

- **2026-08-25 — pdf_rasterize / out-of-process render seam** (elspeth-cc2e6dc8b9)
  First plugin in the tree to load a native library and to use `setrlimit`; both traps are reusable for the next native-dependency plugin.
  1. `pypdfium2` initialises libpdfium as an IMPORT side effect (`pypdfium2/__init__.py` to `_library_scope.init_lib()` at module scope, plus an `atexit` teardown). The import must stay WORKER-ONLY — inside the spawned render subprocess — never at module scope in `pdf_rasterize.py`, or every process that merely imports the plugin (discovery, the main engine process, test collection) pays the native-library cost and inherits its failure modes.
  2. `plugins/infrastructure/rasterize/worker.py` is the first use of `setrlimit` in this tree, and two things are load-bearing. `RLIMIT_AS` is catchable as a plain `MemoryError` ONLY because pypdfium2's default bitmap maker (`PdfBitmap.new_native`) allocates the pixel buffer via ctypes on the Python side, so switching bitmap makers silently breaks the memory guard. A BARE `RLIMIT_CPU` with no handler poisons the whole `ProcessPoolExecutor`: the default SIGXCPU action kills the worker and the pool returns `BrokenProcessPool`, unusable for the next submission — the initializer's `SIGXCPU` handler, which raises a typed exception instead of dying, is what keeps the pool reusable across renders. The timeout/orphan-kill sequence follows the `rag/query.py` precedent: `future.result(timeout=)`, then on timeout `future.cancel()` + `shutdown(wait=False, cancel_futures=True)` + `.kill()` any still-alive process + rebuild the pool; skip any step and a timed-out render either hangs the interpreter at exit or leaves the pool unusable.
  3. `max_page_bytes` has NO cross-node validation against a downstream `aws_textract_inline_analysis`'s `max_document_bytes` (which permits reduction below the 5 MiB default but never a raise). A composer-authored pipeline can set `max_page_bytes` above a lower configured `max_document_bytes`, and every page is then rejected downstream with no config-time warning. Open ticket elspeth-cc2e6dc8b9, which also covers Textract's unmodeled 10,000 px/side dimension limit; the interim mitigation is the composer hint telling the planner to keep `max_page_bytes` at or below the downstream `max_document_bytes`.
  4. The graph builder accepts a `trigger: {}` count trigger immediately downstream of an EXPAND-group transform (e.g. `pdf_rasterize`, `json_explode`) with no error, while rejecting the structurally identical hazard downstream of a row_union (`builder.py:1344-1400`). The gap predates `pdf_rasterize` and is not fixed by it; landing a second expand-group producer only widens its blast radius. Tracked as spec §4 Medium in `docs/specs/2026-08-21-pdf-explode-stitch-risk-assessment.md`; the guard is universal batch-lane work, not owned by this plugin.
  5. Every plugin's `usage_when_not_to_use` and `summary` — not `usage_when_to_use`, not `composer_hints` — is embedded verbatim in the composer's initial scaffolding request, against a global, whole-catalog 96 KiB cap. Editing ANY plugin's `not_for`/summary prose spends from that one shared budget; there is no per-plugin allowance. Landing this plugin's prose plus a few sentences of cross-references in sibling plugins' `usage_when_not_to_use` strings (textract inline/document, for the multipage on-ramp) took live headroom from comfortable to single-digit bytes. The only local signal is a failing `tests/unit/web/composer/test_pipeline_planner.py::test_initial_request_declares_supplied_information_and_omits_redundant_discovery` — nothing in the plugin's own test file or the catalogue-metadata gates catches it, because they check substrings and lengths per-plugin, never the summed whole-catalog payload. Verify that test explicitly after editing any `usage_when_not_to_use` or `summary`, even a one-clause addition.
  6. Follow-up the same day: per-page text extraction landed (pdfium text layer, no OCR) as `extract_text: bool = True` and `page_text_field`. `usage_when_not_to_use` originally opened "Not a text extractor", which the new feature made false; it was reworded THE SAME DAY — a planner-facing falsehood outweighs the freeze, which was about budget, not immutability — to "Not an OCR text extractor: only the PDF text layer is read, empty on scans — OCR needs aws_textract_inline_analysis…", which is accurate and keeps the `_REQUIRED_GUIDANCE["pdf_rasterize"]` substrings ("text extractor", "s3") intact. It fit the ~36-byte headroom only by shrinking the S3 sentence elsewhere in the SAME string; net growth was +21 bytes. `composer_hints` (uncapped) still carries the fuller `extract_text`/`page_text` explanation.
  7. Fix round, same day, three review findings: `minimal_pdf()` gained a `textless_pages: frozenset[int]` kwarg (default empty, so every existing call — including the committed `examples/pdf_rasterize` fixtures — stays byte-identical) to construct a real page with an empty content stream, proving a genuinely text-layer-less page yields `""` rather than leaving an untested code path; the `type(page.text) is not str` `FrameworkBugError` guard (`pdf_rasterize.py`, row-mapping loop) is now pinned by a stub-renderer test; and a new `max_page_text_bytes` config knob (default 1 MiB, ceiling 5 MiB, same shape as `max_page_pixels`/`max_page_bytes`) caps the UTF-8-encoded extracted text, enforced by the worker AFTER extraction (only when `extract_text` is true, never evaluated when false) and folding the new `PageRefusalKind.OVERSIZE_TEXT` into the existing size-refusal set, so the `pdf_page_too_large` vs `pdf_page_render_failed` rule reads "ALL refusals are a size kind" — a worked example of extending that rule without touching its call sites.
  See [CONTRIBUTING: Gate: plugin inventories, source hashes, scenario-corpus manifest, fingerprint baseline](../../CONTRIBUTING.md#gate-plugin-inventories-source-hashes-scenario-corpus-manifest-fingerprint-baseline).

- **2026-08-17 — Worker-pool admission follows the WORKER's lifetime, and the preflight coordinator owns the caller's budget** (elspeth-5269b43bca, elspeth-8607553d3b, elspeth-e4949acbe1)
  `run_sync_in_worker` bounds outstanding submissions at `ADMISSION_CAPACITY` (16 running + 16 queued), released when the sync work actually finishes — never when the awaiter times out or is cancelled. Past capacity it waits `ADMISSION_WAIT_SECONDS` then raises `AsyncWorkerAdmissionTimeoutError`, a `TimeoutError` subclass, so every deadline arm already classifies it as its own TIMEOUT. An awaiter that gives up while its submission is still QUEUED drops it — it never runs; a RUNNING one completes but its outcome is discarded (the cancelled wrapper also carries the old elspeth-e4949acbe1 "no unretrieved exception" contract, and the drain callback is gone). Consequence: a write that MUST land goes through a shielded/deferred-cancellation wrapper (`_await_custody_settlement`, `_await_pipeline_staging_write_with_deferred_cancellation`, `_run_sync_with_post_commit_projection`); do not rely on "the worker runs anyway" for a bare `run_sync_in_worker` call. `RuntimePreflightCoordinator.run` takes the per-caller budget as `timeout=` and returns `RuntimePreflightFailure(TimeoutError)` on expiry while the in-flight entry stays until the sync preflight completes, so a same-key retry JOINS it. Never wrap `asyncio.wait_for` inside the shared worker coroutine again — that made the timeout the shared task's outcome and evicted the entry while the thread still ran, so every retry queued another hung worker.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-14 — Adding ANY index to the Landscape metadata is a delete-and-recreate boundary, and the epoch bump has a docs/website tail**
  `_validate_schema` compares the FULL metadata shape, not just `_REQUIRED_INDEXES`, so a new `Index(...)` in `core/landscape/schema.py` makes every existing `audit.db` refuse to open with "Landscape database schema is outdated"; a `create_all` on an existing table does NOT add it, so there is no self-heal. Bump `SQLITE_SCHEMA_EPOCH` with an epoch-history entry, and expect the pins to fan out well past `src/`: three test assertions (`test_schema_epoch_and_required_columns`, `test_token_ownership_run_scope`, guided `test_schema9_epoch`), `CHANGELOG.md`, `website/get-started.html` ("29 → NN", pinned by `test_release_site_contract`), and `docs/guides/sharing-pipelines.md` (pinned by `test_release_version_surfaces`).
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-14 — A two-column equality join needs a two-column index, or SQLite guesses wrong** (elspeth-c675c8c2d9)
  An audit database has no `sqlite_stat1` (nothing runs `ANALYZE`), so when a join offers `run_id=?` AND `token_id=?` and each column has its OWN single-column index, SQLite's fixed selectivity guess cannot tell that one matches an entire run and the other a handful. It picked `run_id` and turned the run-accounting census into a nested scan: 618s to project one 60k-token run through `GET /api/sessions/{id}/runs` (elspeth-c675c8c2d9). Two lessons: prefer deriving a per-token census from the SMALL table and subtracting against a count (`token_outcomes` carries a composite FK to `tokens`, so "no decision recorded" is arithmetic, not an anti-join), and remember a SQLAlchemy `.subquery()` referenced by N separate `conn.execute()` calls is executed N times — this one was paid four times over. Cost regressions here are testable without wall-clock flake: a SQLite progress handler attached on the engine's `checkout` event counts VM steps, and asserting a RATIO across two data scales discriminates linear from quadratic (see `test_accounting_cost_grows_with_token_count_not_its_square`).
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-13 — A live region must PRE-EXIST its content, and that rule is not polite-only** (da146cd67, elspeth-3f40c9aba2)
  The node must be mounted before the text appears, and only the text may change. Inserting a region that already carries its text is the form with documented AT failures — for ASSERTIVE regions as much as polite ones, so do not "fix" a `role="alert"` by making it conditional on reliability grounds. Test the MECHANISM, not the symptom, and note these are TWO defect classes with different tells, both of which were live here. (a) Node-replacement blindness: re-querying by test id passes even when React REPLACED the node, so hold the element and assert `expect(after).toBe(before)` across the transition; INVISIBLE without a mutation test (`RunOutcomeNotice.test.tsx`, `AcknowledgementStack.test.tsx`). (b) Never performed the transition: the body mounts with the end state already seeded, so the scenario the title names never happens; VISIBLE BY READING, since a test whose title names a transition and whose body has a single `render()` is not exercising one. That is the worse class — node identity is the mechanism, the transition is the EVENT, and a test missing the event never reaches the mechanism. It was live in `ProgressView.test.tsx`, the declared M07 announcement authority (fixed da146cd67); the suite-wide sweep is elspeth-3f40c9aba2. `key={...}` is the cheapest guard-check there is: force a remount, and if the identity assertion does NOT redden it is miswritten. That is what turns an existence pin into a truth pin.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-13 — Pick live-region politeness by CONSISTENCY WITH THE DECLARED AUTHORITY, not by how bad the news is**
  `ProgressView` announces all five terminal run statuses — `failed` and `cancelled` included — through ONE polite `role="status"`. A second App-level region escalating those two to assertive made the same event *more* urgent when the operator had looked away than when watching it, and assertive cuts off the current utterance without re-reading it. A finished background run is a WCAG 4.1.3 status message, not an action-forcing alert. A permanently-mounted second assertive node also makes every singular `getByRole("alert")` in the tree ambiguous — this fired, breaking an unrelated App recovery-panel test. If something ever must interrupt, build ONE app-wide announcer owning a single shared assertive node, never a second per-feature region. Also: `components/ui/AlertBanner.tsx` assigns `role="alert"` to strong tones, so borrowing the `.alert-banner` CSS classes is fine but swapping in the COMPONENT silently reintroduces an assertive region.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-13 — Prose that names a control must derive WHICH controls exist, never assume**
  Review-card labels vary by interpretation kind: `llm_prompt_template` renders "View prompt" + "Approve", never "Acknowledge"; "Change…" appears only where `supportsAmendment`. Both prose surfaces — the `ChatInput` placeholder and the `subscriptions.ts` system note — go through `characterisePendingControls` in `components/chat/acknowledgementLabels.ts`, whose invariant is ONE-WAY: never name a control the pending card(s) do not render, and let a mixed set fall to control-free wording. That module is a deliberate LEAF (no React, no store) because `stores/subscriptions.ts` imports it and was otherwise the tree's only store-to-components edge. Same one-owner shape: `components/execution/runTerminalPhrases.ts` owns the terminal-run vocabulary that both `ProgressView` and `RunOutcomeNotice` speak.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-13 — A version row's wire projection is REDACTED, so "no change" needs the guided blob axis**
  `_state_response` runs `redact_guided_snapshot_storage_paths`, which overwrites a guided committed source's `path`/`file` carriers with a CONSTANT sentinel, so two versions differing only in which input file the pipeline reads are byte-identical across every content field. `versionLabels.isSnapshotOnly` therefore also compares `composer_meta.guided_session.reviewed_sources` blob bindings — BOTH the surviving `options.blob_ref` and any `blob:<uuid>` carrier, since the sentinel arm keeps no `blob_ref`. That is the ONLY thing under `composer_meta` allowed to move the verdict; everything else there is bookkeeping. `composer_meta` is untrusted wire data, so it is PARSED (ADR-032) with an explicit unreadable arm that fails closed. The label says "no visible change", not "no pipeline change": the client can only claim what the projection shows, and a backend per-version content hash is what would earn the stronger word.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-13 — Aggregation members' live BUFFERED acceptances keep the ORIGINAL batch_id across crash-retry**
  `handle_incomplete_batches` retries a dead EXECUTING/FAILED batch on a NEW batch linked through the durable `batches.retry_of_batch_id` chain and copies `batch_members`, but the acceptance-time BUFFERED `token_outcomes` rows are immutable history and still name the original batch. Any consumer proving "this member is buffered into this batch" — the `complete_aggregation_result` receipt writer and the restore receipt validators — must bind against the whole retry lineage (`core/landscape/batch_lineage.batch_retry_lineage_ids`), never `token_outcomes.batch_id == batch_id` alone; the strict form bricks every resumed EOF/flush-fault recovery. Do not "fix" this by writing a second live BUFFERED row at restore: duplicate live acceptances are refused as corruption by `_derive_restored_batch_id`.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-12 — Planner calls and semantic attempts are paired, not counted positionally**
  A physical provider transport failure has no semantic attempt. Every response-bearing planner call (`success` or `malformed_response`) has exactly one adjacent `planner_attempt_audit` row, whose logical `ordinal` is contiguous even when physical `planner_call_ordinal` values have retry gaps. Each `plan_pipeline` request restarts both ordinal spaces at 1, so a session transcript can contain multiple valid ordinal-reset cohorts. Persist each request's LLM calls, attempts, then tool invocations through the existing atomic audit writer; never infer attempt/call ownership by array position, and never turn an unavailable or malformed audit view into zero-call evidence.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-12 — Restricted planner terminals carry schema and materializer custody together**
  `PlannerTerminalContract` owns the exact schema advertised on every normal, repair, and escape-hatch turn plus the function that expands that admitted request shape into the canonical pipeline. Restricted contracts also carry a request instruction telling the provider to follow the advertised delta rather than the shared core's full-document language. If the materializer restores server-owned source, node, or output configuration, return `PlannerTerminalMaterialization` with those component refs; materialization happens before the ordinary candidate finalizer, so relying only on the finalizer diff would expose private validator detail in repair feedback. Freeform and guided-full keep the canonical identity contract. Reviewed guided initial/correction requests select an authority-derived delta, while prose amend/replace remains full-document authoring.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-12 — PostgreSQL token-lock classification needs a fresh statement after a lock wait**
  Do not combine `NOT EXISTS(token_outcomes)` and `SELECT ... FOR UPDATE` when deciding token fate under READ COMMITTED: the predicate can retain the statement's pre-wait snapshot after a competing outcome writer commits. Lock token rows first in stable order, then classify outcomes in a second statement. Every later outcome writer must re-check for an existing `ABANDONED` row after acquiring the same token lock. SQLite cannot prove this protocol because `FOR UPDATE` is inert there; retain the independent PostgreSQL race tests for both lock winners.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-12 — Every successful aggregation completion owns an epoch-32 result receipt**
  Validate the plugin output and declaration contract before completing anything, then commit the node, batch, ordered payload refs, and exact member actions in one transaction. PostgreSQL writers lock member tokens first, then node state, then batch. Transform results use a consumed member as the expansion parent; passthrough results carry one output per member and retain the original token identities; empty results carry no output refs. Restore must load and purely validate every candidate receipt before it mutates any candidate. For empty results, terminal member outcomes, branch losses, and BLOCKED-to-TERMINAL scheduler transitions share one barrier transaction. Do not notify or fire a downstream coalesce/row_union from the empty-routing plan: replay the durable loss ledger only after that transaction commits, otherwise a failed aggregation commit can strand a consumed sibling barrier or lose its merged output. Payload retention and affected-run accounting must include the receipt refs.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-12 — Completed barrier effects are continuations, not late arrivals**
  Aggregation expansion receipts and completed coalesce-effect receipts can exist while their exact input scheduler rows are still `BLOCKED` (process death before `complete_barrier`). Restore must validate the durable receipt and publish its READY/PENDING_SINK successor in the same strict barrier completion that consumes those inputs. Never replay the committed plugin/merge, and never let completed-key reconciliation discard the persisted result as if every blocked parent were a late arrival.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-12 — A long-running transform must re-prove scheduler ownership before terminal audit writes**
  `TransformExecutor` calls the processor's rate-limited active-claim heartbeat immediately after plugin return or exception and before node-state completion, transform-error/routing writes, contract evolution, or result visibility. If recovery or eviction has moved authority, `NodeStateGuard.abandon_open_state()` leaves that stale attempt OPEN — the honest hard-kill image — and the ownership-loss exception must propagate immediately; do not auto-fail, complete, or otherwise mutate the stale attempt. The scheduler drain then clears any in-memory staged branch losses and records only the canonical lease-loss evidence.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-12 — Sink-redrive recovery is admitted by the complete durable bundle, not by `pending_sink_name` alone**
  A `LEASED` row with any sink-redrive field set is sink-shaped debt and must satisfy `pending_sink_bundle_clause()` before it can return to `PENDING_SINK`. Repeat the same subtype/bundle predicate inside the recovery CAS; the diagnostic SELECT is not the safety boundary. A partial or concurrently corrupted bundle fails closed and the whole recovery transaction rolls back without rotating attempts, changing owners, or appending events.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-11 — The AWS IAM policy templates and the deploy README's floor commit are both pinned; editing either without its sibling update is red**
  `tests/unit/deployment/test_aws_iam_policy_oracles.py` pins the exact set of actions granted under an `aws:RequestTag/ACCEPTANCE_RUN_ID` condition and the exact set of wildcard patterns, so any grant added to `deploy/aws-ecs/terraform/iam/*.json.tftpl` fails until it is adjudicated. The verdict a new create needs: does the API ALSO authorize against a pre-existing untagged parent (the D11 trap — `ec2:CreateSubnet` also authorizes against its VPC, which carries no request tag)? If yes, add the `aws:ResourceTag` arm to the policy AND record it in `_DUAL_PURPOSE_PARENT_ARMS`; recording it WITHOUT the arm is red, because every entry is proved against the rendered policy. An earlier revision of this gate let a novel Sid discharge the pin with no arm present, which was worse than not gating at all, since the green then asserted a verdict had been reviewed. Neither set decides whether an API is dual-purpose (a fact about AWS, not about this tree); they only make the question unskippable. Two boundaries worth knowing: create-shaped actions granted OUTSIDE a RequestTag condition are adjudicated by nothing, and membership pins the action SET, not the condition SHAPE.
  See [CONTRIBUTING: Gate: declared oracles pin output bytes](../../CONTRIBUTING.md#gate-declared-oracles-pin-output-bytes).

- **2026-08-11 — "Minimum image revision" in `deploy/aws-ecs/terraform/README.md` is machine-checked, not prose** (elspeth-af1efcb8d8)
  Ship a new `ELSPETH_WEB__` name and `test_documented_minimum_image_revision_is_the_true_settings_floor` fails until that paragraph names the earliest ancestor of HEAD whose `WebSettings` defines every shipped name. Correct the paragraph, never the number alone. It was last wrong by six settings — `settings_from_env` raises on an unknown key and `WebSettings` is `extra="forbid"`, so an operator obeying it pins an image that fails every task at settings load, after a successful apply. The test skips only when the checkout has no git history at all, and fails loudly under `GITHUB_ACTIONS` (same rule as `_require_terraform`, elspeth-af1efcb8d8); an unresolvable or non-ancestor SHA is always red.
  See [CONTRIBUTING: Gate: declared oracles pin output bytes](../../CONTRIBUTING.md#gate-declared-oracles-pin-output-bytes).

- **2026-08-11 — Cancellation-safe settlement outcomes belong in the locked transaction**
  A deferred-cancellation wrapper drains its shielded database worker, then deliberately re-raises `CancelledError`, so any audit write left to an outer exception handler can be skipped even though an earlier dispatch committed successfully; process failure creates the same gap. For commit-boundary trust revocation, insert or exactly reuse `auto_commit.revoked` inside the session-locked settlement transaction, return the revocation as an internal outcome so the context commits, and raise `TrustModeAutoCommitRevokedError` only after `_run_sync` returns. The route translates that error but never owns a second revocation write.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-11 — Caller-owned DB transactions cannot publish inline-custody files directly**
  The guided-full settlement must insert the originating message and blob row in one transaction to satisfy the composite lineage FK, but a DB rollback cannot roll back a canonical filesystem rename. Stage those bytes at the bounded `.{blob_id}.inline-custody-staged` sibling, return the publication to the transaction owner, and arbitrate the outcome from the committed row under the same-session custody lock. A transaction error has an ambiguous commit outcome: re-query and publish when the row won; remove the stage when no row exists, or when this attempt created it and rollback restored an exact pre-existing `pending` row. Startup likewise discards a stage beside an exact `pending` row because inline settlement commits only `ready`; retaining that non-authoritative stage makes the supported pending retry state unbootable. The writer's `..{blob_id}.inline-custody-staged.custody.tmp` is always disposable, never row authority: startup enumerates it and the durable stage only after taking the session lock, then deletes temps and reconciles stages. Reject symlink/non-regular candidates and validate a row's exact canonical storage path before moving anything. Nofollow-open and retain both the `blobs/` root and session directory descriptors across live staging/publication/cleanup and the whole startup pass — checking only session/final components still lets a `blobs -> outside` ancestor escape custody. On first use, fsync the resolved data directory after linking `blobs/`, then fsync `blobs/` after linking the session directory; fsyncing only the stage file and session directory does not make those new ancestor entries crash-durable. Reconciliation hashes every candidate incrementally with `_STREAM_CHUNK_BYTES` through a no-follow descriptor; `Path.read_bytes()` under the custody lock recreates the several-large-blobs worker-memory failure this protocol prevents.
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-10 — A `DateTime(timezone=True)` column does NOT round-trip aware on SQLite** (a5d7fc0e7)
  The blobs write stamps `datetime.now(UTC)`, the column declares `timezone=True`, and `BlobRecord.created_at` still comes back with `tzinfo=None` through the SQLite dialect. So a `created_at.tzinfo is not None` assertion reads as obviously correct, raises on EVERY write under SQLite, and passes under PostgreSQL — an environment-dependent production break a PostgreSQL-only test lane would never show. Check `created_at` for shape (`type(x) is datetime`) unless awareness has been proven on the backend actually in use. `verify_finalized_pipeline_custody` (`web/composer/pipeline_custody.py`) documents the narrowing and `test_verify_accepts_a_naive_created_at` pins it against a well-meaning re-tightening. Found while extracting the check from an abandoned WIP branch (a5d7fc0e7): salvaged WIP is a hypothesis, not reviewed code — probe its assertions against a live round-trip before porting them. The same function arrived using a `getattr(record, field_name)` loop, which the attribute-contracts and masquerade gates forbid outright; `BlobRecord` is an owned type, so direct attribute access was always the correct form.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-09 — Composer edge/route contract** (Lane W2, elspeth-67b44040ee)
  Scalar routing fields are the runtime authority; SINK-targeting edges are their mirror and must agree; node-targeting on_success edges are advisory. One shared predicate — `edge_lowering_error` in `web/composer/state.py` — decides which (component kind, edge type, target kind) combinations are legal, for BOTH upsert_edge admission and Stage-1 `validate()`; its full matrix is pinned by `test_edge_route_reconciliation.py`, so extend the matrix and its pin together. upsert_edge/remove_edge/upsert_node reconcile the mirror through `_apply_sink_edge_route` / `_clear_removed_sink_edge_route` / `_reconcile_node_sink_mirror_edges` (tools/transforms.py); do not hand-sync a route in a new tool. Two traps: (a) deterministic runtime-fatal routes are now Stage-1 ERRORS, not warnings — `quarantine_unknown_output`, `failsink_unknown_output`/`_self_reference`/`_ineligible_plugin`/`_chain`, `aggregation_on_error_unknown_sink`, `gate_route_target_unknown`, `gate_routes_empty`, gate fork-consistency — so a test fixture with `on_validation_failure="quarantine"` and no quarantine sink no longer validates green (this silently broke dozens of fixtures; declare the sink or use "discard"); (b) one sink-route slot carries ONE edge (`edge_route_conflict`), so a second edge id on the same (from_node, edge_type) sink route is rejected at upsert and red in Stage 1.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-09 — Plugin config unions use nominal admission plus owned MRO evidence**
  `declares_discriminated_config_variants()` derives whether an admitted `BaseSource`, `BaseTransform`, or `BaseSink` class declares `discriminated_variants()` anywhere in its live MRO. Consumers such as the options-metadata lint first admit the nominal Base* category, then use that non-cached evidence and call the declared method directly. Do not bring back `getattr`/`hasattr` capability probes, treat the runtime-checkable structural Protocol as an identity control, or hard-code the currently known LLM implementations.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-09 — Re-check mutable exception facts and every composer completion at their exit gate**
  Nominal ownership of an exception does not make its class or instance attributes immutable. Operator-facing acceptance envelopes must clamp `error_code` again at projection time, requiring an exact `str` from the closed vocabulary. In the freeform Composer, the B-4D-3 budget-exhaustion bonus response is still a model completion: apply the shared per-turn tool cap before its no-tool/generic-budget branch, using the already-charged composition count. Raw `_call_llm` test-seam responses that fail tool-call identity admission still re-raise `AuditIntegrityError`, but their LLM audit row is `MALFORMED_RESPONSE`/`malformed_response`, never `SUCCESS`.
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-09 — Review bundles are v2 exact-source assertions**
  Staging and firing bind full Git HEAD, tracked-source dirty state, and every scanner Python/YAML byte. The YAML set is the production loader's non-recursive top-level `*.yaml` inventory, not nested YAML or `.yml`. Relevant untracked inputs (ignored included), harmless byte drift, or a HEAD advance invalidate the bundle even when its action list is unchanged. Scanner inputs must be non-symlink lexical Git paths: reject symlinks rather than resolving an alias to a tracked target, and strip ambient `GIT_*` variables from evidence commands. Transaction candidates supply physical allowlist bytes but retain the public allowlist path as their logical Git identity; never hash the candidate under its private transaction path.
  See [CONTRIBUTING: Convention: lints and tier-model tooling](../../CONTRIBUTING.md#convention-lints-and-tier-model-tooling).

- **2026-08-09 — `CompositionState._content_hash_memo`** (elspeth-62a5aa4da8)
  A write-once memo slot read by `composition_content_hash` via DIRECT access. Every mutation constructor resets it in `__init__`. Adding a mutation path means resetting the slot; a `to_dict` stand-in built for hashing tests needs `_content_hash_memo: str | None = None`. Do not reintroduce `getattr` here — that was elspeth-62a5aa4da8.
  See [CONTRIBUTING: Gate: attribute contracts](../../CONTRIBUTING.md#gate-attribute-contracts-dynamic-attribute-sites).

- **2026-08-09 — Advisor evidence has ONE derivation per surface** (elspeth-eacfec09a6, elspeth-c1b8b26d32)
  In `web/composer/service.py`, anything rendered to the advisor must also be reachable by the deterministic injection pre-scan. Node control-flow fields derive from `_advisor_control_flow_fields` — `_render_node_control_flow` publishes it and `_advisor_prompt_template_injection_finding` scans it. Add a new control-flow field THERE, never as a fresh `if node.x is not None` branch in the renderer; hand-enumerating the two consumers separately is what left `trigger` rendered-but-unscanned (elspeth-eacfec09a6). Two rules that look redundant but are not: the scan reads the COMPLETE value while the renderer truncates (scan broader than render, pinned by a disagreement test — do not "simplify" it to scan only what is rendered), and render-admission (`_advisor_summary_renders_option_value`) is a SEPARATE predicate from scan-shape (`_advisor_prose_shaped_option_value`) because the two consumers need opposite failure directions (elspeth-c1b8b26d32). Render paths that bypass the admission predicate entirely — e.g. `required_input_fields` via the `[requires: ...]` segment — need their own scan arm.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-09 — "validated" is reserved for a GREEN Stage-2 preflight** (elspeth-2ed41f0a4a)
  The planner staging announce (`protocol.PIPELINE_STAGED_*`) is FIVE constants, not two, selected in `service._stage_pipeline_plan` by the actual runtime-preflight verdict over `PipelinePlanResult.candidate_state` (elspeth-2ed41f0a4a). Only a green verdict may say "validated" or mint a `PipelineCommitIntent`; a new staging surface picks a constant by verdict rather than reusing the green ones as generic staging copy.
  1. The non-green arms split on SHAPE, not on `is_valid`. A red verdict and a pending-interpretation handoff are both `is_valid=False`, but only the first is a validator objection, and reporting a pending review card as "issues that must be fixed" sends the operator hunting for a defect that is not there. Use `_is_pending_interpretation_handoff`, and note its blocker code is the lowercase `interpretation_review_pending` — import `INTERPRETATION_REVIEW_PENDING_CODE` rather than hand-writing the string, or the test fixture silently misses the arm it means to exercise.
  2. Catch ONLY `ComposerRuntimePreflightError` around `_cached_runtime_preflight`. `RuntimePreflightCoordinator._capture` funnels every `Exception` — timeouts included — into that single envelope, so an `except TimeoutError` arm is dead code and a test scripting a bare `TimeoutError` exercises a path production cannot produce. `asyncio.CancelledError` is a `BaseException`, escapes `_capture`, and must keep propagating: broadening the catch turns a cancelled request into a staged proposal carrying a verdict nobody waited for.
  3. The non-green arms set `raw_assistant_content=""` (the replacement shape) because the `ComposerResult` field-pairing invariant requires it for any failed preflight; the green arm keeps `None`, or it would falsely imply synthesis on a verbatim response.
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-09 — Registered `pipeline_decision` user_terms need THREE arms** (elspeth-c2c35e52ae)
  A new entry in `REGISTERED_PIPELINE_DECISION_USER_TERMS` (`web/interpretation_state.py`) is not usable until it also has (a) a binding arm in `validate_pipeline_decision_semantics` — a registered term that falls through validates on ANY node and wedges later at the hash — and (b) an arm in `pipeline_decision_artifact_hash` pinning exactly the material the review adjudicates. Gates bind on `node_type == "gate"`, NOT plugin, since structural nodes have `plugin=None`. An exact-set test pins the closed registry, and doc listings render `sorted(REGISTERED_...)` dynamically — never hardcode the set in prose. If the hash reads NodeSpec fields outside `options`, add a to_dict/from_dict round-trip test (`fork_to` is tuple in memory, list on the wire) so serializer changes cannot drift accepted reviews (elspeth-c2c35e52ae).
  See [CONTRIBUTING: Convention: web composer and frontend](../../CONTRIBUTING.md#convention-web-composer-and-frontend).

- **2026-08-09 — SQLAlchemy `Row`: `.count` is the TUPLE METHOD, not a column** (elspeth-d5578ccd98)
  Access columns through `row._mapping` (elspeth-d5578ccd98 fallout, Lane B).
  See [CONTRIBUTING: Convention: validate by trust domain](../../CONTRIBUTING.md#convention-validate-by-trust-domain).

- **2026-08-08 — Branch-loss reasons are categorical** (elspeth-74b795208f)
  Every `record_coalesce_branch_loss` producer emits bare tokens from the shared vocabulary; a new producer must reuse it, not invent prose reasons (elspeth-74b795208f).
  See [CONTRIBUTING: Convention: audit and lineage recording](../../CONTRIBUTING.md#convention-audit-and-lineage-recording).

- **2026-08-08 — Forwarding transforms declare their extras** (elspeth-15c72686f2)
  The extras firewall walk is SEPARATE from the presence walk; a transform that forwards rows must declare the extras it forwards or downstream consumers see them truncated (elspeth-15c72686f2).
  See [CONTRIBUTING: Convention: passes_through_input presence discipline](../../CONTRIBUTING.md#convention-passes_through_input-presence-discipline).
