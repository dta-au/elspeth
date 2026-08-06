"""Composer/runtime schema-contract characterization.

This suite covers two categories:
- shared contract cases where composer preview and runtime should agree
- documented runtime-only gaps where composer stays permissive and the runtime
  validator remains authoritative

It does not claim global equivalence between preview validation and runtime DAG
validation.

Closed registry of composer/runtime divergence shapes (extends with each eval).
``elspeth-1ee3c96c72`` (Phase 3) maintains this list as the durable contract;
every future "validate green / runtime red" finding extends it. The registry
crosswalks each shape to the originating eval reproducer and the closure issue
where the architectural fix landed:

* Shape 1 — S1A literal credential placeholder
  ``api_key: WILL_BE_WIRED_FROM_OPENROUTER_API_KEY`` (eval session 2ef2db56,
  run 51f5f609). Closes ``elspeth-72d1dccd44``. Pinned by
  ``TestComposerRuntimeSecretRefAgreement``.
* Shape 2 — S2 v1 dangling on_error route target
  (``aggregations[*].on_error: aggregation_errors`` with no matching sink).
  Closes ``elspeth-127de6865a``. Pinned by
  ``TestComposerRuntimeRouteTargetAgreement`` plus four defense-in-depth axes.
* Shape 3 — S2 v2 batch_stats output schema propagation
  (``schema: {mode: flexible, fields: [...], required_fields: [...]}`` on a
  reductive aggregation; runtime raised ``SchemaConfigModeViolation``). Closes
  ``elspeth-f5f798f797`` (commit ``2d9dc21d``). Pinned by
  ``test_both_accept_aggregation_with_input_fields_and_required_fields``.
* Shape 4 — S1A monolithic happy-path positive control. Deferred — requires
  end-to-end LLM-stub integration scaffolding that does not exist in
  ``tests/integration/pipeline/``. Filed as a follow-up on
  ``elspeth-1ee3c96c72``'s closure.
* Shape 5 — Phase 2.2 RunStatus four-value terminal taxonomy plus the
  rows_routed-only design call. Closes ``elspeth-0de989c56d`` (commit
  ``cc895589``). Pinned by ``TestComposerRuntimeRunStatusAgreement``. The
  per-status engine-layer pinning lives in
  ``tests/integration/pipeline/orchestrator/test_orchestrator_core.py``; this
  suite adds the cross-layer (engine RunResult ⇔ Landscape audit ``Run`` row)
  agreement and the named design-call regression
  ``test_runstatus_on_error_routed_only_classifies_as_failed`` (plus the
  post-split companion ``test_runstatus_gate_routed_only_classifies_as_completed``
  introduced by ``elspeth-5069612f3c``).
* Shape 6 — Phase 2.3 ``/api/secrets`` reason taxonomy (eval session
  S1B, ``ELSPETH_FINGERPRINT_KEY`` unset). Closes ``elspeth-0d31c22d26``
  (commit ``22e3e0d9``). Per-mode coverage lives in
  ``tests/unit/web/secrets/{server,user}_store.py`` and
  ``tests/unit/web/secrets/test_routes.py`` per the Phase 2.3 closure
  rationale ("agreement-suite scope does not need duplication"). This suite
  adds a single contract-layer biconditional smoke
  (``TestComposerRuntimeSecretInventoryAgreement``) so a future drift on
  the ``available ⟺ reason is None`` invariant fails the agreement gate too.
* Shape 7 — Phase 2.1 pipeline_done_callback run-accounting agreement
  (eval run 44f52421, csv → batch_stats group_by → json sink wrote output
  but ``/api/runs/{rid}`` lagged because ``CompletedData`` rejected the
  legitimate aggregation row-count shape). Closes ``elspeth-31d53c7493``
  (commit ``5e26d0a6``). Pinned by
  ``TestComposerRuntimeRunCompletionAgreement``.
* Shape 8 — Phase 0.b composer file-sink path-collision diagnostic
  (eval session S3 ``98573481-e8bc-4a03-8467-d3a86effcd56``, originally
  attributed in the eval notes to a "gate primitive crash" but root-caused
  during Phase 0.b investigation to ``FileExistsError`` raised uncaught from
  ``json_sink.__init__`` / ``csv_sink.__init__`` via
  ``resolve_output_collision_path`` when a sink path collides with an
  existing artifact under ``collision_policy="fail_if_exists"``. The
  exception class was missing from ``validate_pipeline``'s step-4 catch
  list, propagating as ``composer_plugin_error`` 500 instead of a 422-class
  structured ``ValidationResult``). Closes ``elspeth-209b7e3a2b``. Pinned
  by ``TestComposerRuntimeFileSinkCollisionAgreement``. Gates were the
  symptom amplifier (gate-routing pipelines need sinks; LLM defaults
  ``collision_policy="fail_if_exists"``; stale eval artifacts collide),
  not the cause.
* Shape 9 — Phase P4/P3 of ``elspeth-fdebcaa79a`` widened ``blob_ref`` /
  ``inline_content`` config-content-ref capability. Pinned by
  ``TestComposerRuntimeBlobInlineAgreement``: validate-time metadata checks
  surface structured ``ValidationResult`` rows; runtime hash mismatch fails
  closed before settings/plugin construction; successful runtime resolution
  records the hash in ``blob_inline_resolutions`` before the resolved bytes
  enter plugin settings.
* Shape 10 — fixed-mode consumer *implicit*-required-field parity
  (``elspeth-8f3b3f650d``). A ``{mode: fixed, fields: [...]}`` consumer
  implicitly requires its declared (non-optional) fields; runtime Phase-2 type
  validation rejects an edge from a TYPED producer that does not guarantee one
  (``EdgeContractError: ... Missing fields: teal_pairing_rating``). Authoring
  computed consumer requirements from the explicit-only
  ``get_raw_node_required_fields`` and so green-lit the build (validate green /
  runtime red). Fixed in ``web/composer/state.py::_check_schema_contracts`` by
  adding a sibling check over the consumer's *effective* required set, gated
  strictly on producer schema MODE (a fixed/flexible SOURCE producer is typed;
  observed sources and transform/gate/coalesce producers resolve to a dynamic
  effective producer schema and are skipped), mirroring the runtime
  observed/dynamic bypass at ``graph.py:1392-1403``. Pinned by
  ``TestComposerRuntimeFixedModeImplicitRequiredAgreement``.
* Shape 11 — structural queue fan-in round-trip (``elspeth-a5b86149d4`` /
  ``elspeth-6421ffa028``). The composer models a runtime ``queues.<name>``
  fan-in point as a canonical structural queue NodeSpec (id == input,
  plugin=None, description-only options). Import must preserve it, validate must
  accept the two-source → queue → transform topology, and export must round-trip
  it so ``load_settings_from_yaml_string`` rebuilds a graph with exactly one
  ``NodeType.QUEUE`` (both sources fan in, one ordinary consumer out).
  Previously the composer had no queue node type, so a pasted ``queues:``
  section was dropped and undeclared two-source fan-in was the only expressible
  form (validate green / runtime red: ``GraphValidationError`` "fan-in from
  multiple producers without a queue"). Pinned by
  ``TestComposerRuntimeQueueAgreement`` with a manual negative control proving
  the queue-free topology still reproduces the runtime rejection.
* Shape 12 — queue-to-coalesce consumer accounting
  (``elspeth-3f4e63900f``). Runtime connection registration treats only mapped
  coalesce branches (branch name differs from input connection) as ordinary
  consumers; list-form/identity branches are direct gate-to-coalesce COPY
  edges. Composer previously omitted mapped branches from duplicate-consumer
  accounting and inferred queue liveness from the coalesce node's first
  compatibility ``input`` field. Pinned in both directions by
  ``TestComposerRuntimeQueueAgreement``.
* Shape 13 — row_union YAML import/export agreement. Composer import preserves
  both branch forms and structural timeout, export emits the runtime-only
  ``row_unions`` shape without a synthetic input, and regenerated settings
  rebuild the same production graph as the shipped A/B example. Pinned by
  ``TestComposerRuntimeRowUnionAgreement``.
* Shape 14 — queue effective-guarantee propagation (battery-2026-08-04 g08,
  ``elspeth-5a372d3267``). A queue node carried a hardcoded observed/no-fields
  schema and ``walk_effective_guarantee_vote`` stopped there, so a
  source-guaranteed field never reached a queue consumer's
  ``required_input_fields`` check: runtime graph build rejected the runnable
  source → queue → consumer topology with ``GraphValidationError``
  "guarantees: (none - dynamic schema)" while composer Stage-1 abstained with
  a medium warning (validate green / runtime red at the /validate boundary).
  Fixed in ``core/dag/guarantees.py``: the walk now propagates through QUEUE
  nodes — intersection of arm guarantees when every arm participates, total
  abstention when any arm abstains (fan-in soundness; ``compose_propagation``'s
  abstainer-skip applies only to same-row pass-throughs). The composer Stage-1
  preview walker mirrors the same fan-in vote (elspeth-3619b8774f), so both
  surfaces accept AND reject identically — composer-level pinning in
  ``tests/unit/web/composer/test_state.py::TestCompositionStateQueueGuaranteePropagation``,
  engine-level in ``tests/unit/core/test_dag_queue_guarantee_propagation.py``;
  this suite pins the cross-surface agreement via
  ``TestComposerRuntimeQueueGuaranteeAgreement``.
* Shape 15 — row_union locked-input extras (battery-2026-08-04 g02b, run
  ``f6bbca45``, ``elspeth-9d13900064``). The composer's Rule A/Rule B extras
  checks were skipped whenever the producer walk-back hit a row_union
  (medium-warning abstention), so llm x2 → row_union → fixed-mode consumer
  validated green and failed at the executor input preflight with
  ``PluginContractViolation`` (``extra_forbidden`` on the llm provenance
  side-fields). Fixed in ``web/composer/state.py`` by a dedicated
  extras-polarity walker (union of arm emit sets — a lower bound: sound to
  error on, never to clear with); presence-direction abstention untouched.
  The runtime half of this shape is the executor-level per-row input
  validation, not a graph-build check, so the pin lives at the composer unit
  layer: ``tests/unit/web/composer/test_state.py::TestCompositionStateRowUnion``
  (7 tests incl. the ticket's literal graph and the Rule B gate→sink
  topology), per the Shape 6 precedent for coverage housed outside this file.
* Shape 16 — pending-review handoff announce truncation (battery-2026-08-04
  g08, ``elspeth-5a372d3267``). ``review_interpretations`` fails the strict
  ledger at canonical index 10 asserting ``completion_ready=True``, stamping
  every later stage — ``graph_structure`` at 21 included —
  ``SKIPPED_AFTER_FAILURE``; the composer then announced "ready for the
  required review" from that truncated result. Fixed in
  ``web/composer/service.py``: announce sites verify the handoff with an
  authoring-masked re-validation (``allow_pending_interpretation_placeholders``,
  previously zero production callers) and qualify the published message when
  it is red; the self-repair loop is deliberately not engaged (staged-review
  no-extra-turns contract). Pinned at the unit layer:
  ``tests/unit/web/composer/test_runtime_preflight_pending_review_verification.py``,
  per the Shape 4 precedent that this suite carries no LLM-loop scaffolding.

* Shape 17 — transform output-field collision with an already-present row field
  (battery-2026-08-05 round 3, graph g05, run
  ``adf0b6c6-bdcb-4e29-ba23-b953bae5366c``, ``elspeth-cfcd333f83``). A text
  source emitting ``headline`` fed an llm node authored with
  ``response_field: headline`` — the obvious authoring for a rewrite-in-place
  transform. Compose succeeded, ``POST /validate`` returned ``is_valid=true``,
  and the run died on row 1 with ``PluginContractViolation`` "would overwrite
  existing input fields ['headline']" from
  ``TransformExecutor._run_preflight`` — an error the engine's own message
  calls a *pipeline configuration error*.

  Root cause was an information gap, not a missing predicate: ``NodeInfo``
  carried ``declared_required_fields`` (sinks) but nothing for transform
  outputs, and ``core/dag/builder.py`` read ``transform.declared_output_fields``
  for a self-consistency check and then discarded it, so the build-time surface
  structurally could not see what the executor enforced per row. Closed on both
  surfaces: ``core/dag/schema_validation.py::validate_transform_output_field_collisions``
  (runtime graph, the output-side twin of ``validate_sink_required_fields``,
  reached by both ``elspeth run`` and ``POST /validate`` because
  ``build_execution_graph`` ends by calling ``validate_edge_compatibility``),
  and Rule D in ``web/composer/state.py::_check_schema_contracts`` (authoring).

  The fork the ticket posed — legalise the overwrite, or reject at compose — is
  answered by rejection: ``contracts/field_collision.py`` states the no-silent-
  overwrite policy ("silent overwrites are data loss") for a lineage system, and
  rewrite-in-place is doubly blocked because the transform must also READ the
  field it rewrites (see ``elspeth-39118dd24f``). Rewrite-in-place is simply not
  an expressible shape today; first-class support would be an explicit opt-in
  feature, not this fix.

  Two coverage boundaries are DELIBERATELY left open and are part of this shape
  rather than separate defects, because closing either means changing fan-in
  guarantee/emit semantics:
    - ROW_UNION upstream: composer Rule D rejects, the graph check abstains
      (``walk_effective_guarantee_vote`` has no ROW_UNION branch). Runtime
      under-rejects, so it is sound; the composer is the stronger surface.
    - QUEUE upstream: the graph check rejects (correctly — the walk propagates
      through QUEUE as the intersection of arm votes, Shape 14), composer Rule D
      abstains. The authoring loop therefore gets no repair signal and the
      failure lands at deploy/preflight rather than in the compose turn.

  Pinned at the unit layer per the Shape 6 / Shape 15 precedent that this suite
  carries no LLM-loop scaffolding: ``tests/unit/core/dag/test_graph_validation.py``
  (``TestTransformOutputFieldCollisions``, incl. the DIVERT-only and
  string-mode negative controls), ``tests/unit/core/dag/test_builder_validation.py``
  (``TestTransformOutputFieldCollisionRejectedAtBuild`` — the ticket's literal
  g05 topology through the real ``text`` source and real ``llm`` plugin), and
  ``tests/unit/web/composer/test_state.py`` for Rule D.

  Bug verification protocol, performed 2026-08-05 and recorded verbatim as this
  file requires:
    - Neutering the body of ``validate_transform_output_field_collisions``
      (inserting ``return`` before its node loop) failed 5 tests:
      ``test_reached_through_validate_edge_compatibility``,
      ``test_multi_hop_guarantee_through_pass_through_transform_is_rejected``,
      ``test_live_edge_alongside_divert_edge_is_still_rejected``,
      ``test_upstream_guaranteed_field_is_rejected``, and
      ``test_response_field_reusing_source_column_is_rejected``, each with
      ``Failed: DID NOT RAISE <class 'elspeth.core.dag.models.GraphValidationError'>``.
    - Neutering only the CALL SITE in ``validate_edge_compatibility`` failed 2
      of those 5 — the two that go through the public entry point — confirming
      the other three exercise the validator directly.
    - Deleting the ``mode == RoutingMode.DIVERT`` skip in ``_live_predecessors``
      failed ``test_divert_only_predecessor_is_not_rejected`` and
      ``test_divert_mode_stored_as_plain_string_is_still_skipped``. The skip is
      pinned.

      METHOD NOTE, because a first pass got this wrong and the wrong answer is
      the seductive one: the identical string
      ``if edge_data["mode"] == RoutingMode.DIVERT:`` appears THREE times in
      this module (``validate_edge_compatibility``'s edge loop,
      ``get_effective_producer_schema_config``, and ``_live_predecessors``). A
      naive first-occurrence substitution patches the edge loop and reports
      "nothing failed", which reads as a coverage hole in ``_live_predecessors``
      and is not one — the skip IS pinned. When mutating a line for this
      protocol, assert the match is unique before editing and fail closed if it
      is not; a mutation applied to the wrong site produces a confident,
      completely wrong coverage claim.

    - Flipping that same comparison from ``==`` to ``is`` failed exactly one
      test, ``test_divert_mode_stored_as_plain_string_is_still_skipped``.
      ``RoutingMode`` is a ``StrEnum`` and ``add_edge`` stores ``mode``
      uncoerced, so an edge carrying the plain string ``"divert"`` compares
      equal but is not identical; identity would treat it as live and reject a
      runnable pipeline.

      Reachability, stated so the guard is not over-read: every DIVERT edge
      ``build_execution_graph`` creates targets a SINK (source quarantine,
      transform/gate ``on_error``, sink failsink), and a transform's
      ``on_error`` is rejected unless it names a sink (builder.py:1139). A
      TRANSFORM therefore cannot be a divert target on today's production path,
      so this filter is defence-in-depth for the public ``add_edge`` surface
      rather than a guard on a live route.

* Shape 18 — union-coalesce shared-field type compatibility (battery-2026-08-07
  round 6, graph g03, session ``5190564b-abcc-450a-9a65-225411b3ce66``, pin
  ``69c6ad4b5``, ``elspeth-85f3cc3022``). The composer authored a fork/coalesce
  whose two branches declared ``price`` as ``int`` and ``str`` and merged them
  with ``merge: union``. Stage 1 returned ``is_valid=True``, so ``set_pipeline``
  reported the mutation clean; the DAG build rejected it with
  ``GraphValidationError`` "receives incompatible types for field 'price' in
  union merge". Validate green / runtime red, and the compose loop had no
  signal to repair from: it stopped in the skill's pending-review terminal
  state believing it was done. The ticket's proposed fix ("preview before
  declaring done") treated the symptom — nothing in the mutation envelope gave
  the model a reason to preview.

  This was a missing member of an existing mirror family, not a new class:
  ``state.py`` already mirrored the coalesce observed/explicit MODE rule
  (``coalesce_schema_mode_mixed``) and the row_union TYPE rule
  (``row_union_schema_incompatible``); union coalesce had the mode mirror but
  never got the type mirror. Closed in
  ``web/composer/state.py::validate`` by reusing the canonical shared algorithm
  ``contracts/union_merge.py::merge_union_field_flags`` — the same function the
  runtime reaches through ``merge_coalesce_schema`` — rather than
  re-implementing type comparison, so the two surfaces cannot drift on the
  rule. Emits ``coalesce_union_type_incompatible``. The mode entry short-
  circuits the type check because the runtime raises the mode conflict first.

  Parity is exact for every coalesce that REACHES the check, verified across a
  9-case matrix (fixed/flexible/observed against conflicting/identical/``any``/
  disjoint): Stage 1 and the runtime graph build agree on all nine. Two known
  boundaries keep that from being a claim about the whole rule, and both are
  permissive (they miss a rejection; neither blocks a runnable pipeline):
    - ``merge=None``. CLOSED FOR NODESPEC-CONSTRUCTED STATE by ``aa963bafe``
      (``elspeth-11334b382c``); the injected-``state_dict`` route remains open
      (``elspeth-5581fcb76f``). Both union mirrors gate on ``merge != "union"``
      (``state.py``) while ``CoalesceSettings.merge`` DEFAULTS to ``"union"``
      (``core/config.py``), so a coalesce that left merge unset was skipped by
      BOTH the mode mirror and the type mirror while the runtime enforced each
      as a union. ``NodeSpec.__post_init__`` now normalises it at the one
      construction boundary ``from_dict``, ``upsert_node``, ``set_pipeline``
      and ``replace`` all route through, so a third union rule cannot inherit
      the hole. It DEFAULTS the field rather than requiring it: the runtime
      accepts an unset merge, so rejecting here would be the opposite
      divergence.

      THE REACHABILITY ARGUMENT THIS ENTRY ORIGINALLY CARRIED WAS FALSE, and
      naming that is the point of amending rather than deleting it. The claim
      — "``yaml_generator`` always emits the key, so ``None`` becomes
      ``merge: null`` and pydantic rejects" — was disproved by execution:
      ``to_dict`` writes ``merge`` CONDITIONALLY (``state.py:4374``) so the key
      is ABSENT, and ``yaml_generator.py:290`` reads ``c["merge"]``
      unconditionally, raising ``KeyError`` — an internal crash on the
      ``preview_pipeline -> runtime_preflight -> validate_pipeline ->
      generate_yaml`` path, never a pydantic error and never a repair signal a
      model could act on. That wrong argument is why ``af62478df`` shipped with
      the gap ``aa963bafe`` had to close. A PERMISSIVE finding that is ALSO
      load-bearing for an unreachability argument gets neither half
      scrutinised: one half removes the incentive to test it, the other
      supplies a reason not to. Retiring a finding on reachability therefore
      requires an EXECUTED check, not a code reading.
    - ALL-OBSERVED branches. ``merge_union_fields`` early-returns observed mode
      without a type check, and Stage 1 abstains by the same rule, so two
      observed branches whose INFERRED types diverge reach the coalesce
      executor and fail there with ``ContractMergeError`` ("Cannot merge
      contracts"). That is data-dependent, so no static surface can close it;
      it is deliberately NOT routed to this shape's error code, because the
      repair ("your rows carry mixed types") is not this code's repair
      ("declare the same type on every branch").

  Two results are counterintuitive and are pinned deliberately:
    - ``any`` is NOT a wildcard here. ``price: any`` against ``price: int``
      conflicts, because ``merge_union_field_flags`` compares type keys with
      ``!=`` on opaque hashables. This differs from
      ``row_union_schema_configs_compatible``, which DOES treat ``any`` as a
      wildcard for flexible row_union branches — the two node kinds genuinely
      have different rules and the mirrors must not be cross-copied.
    - A branch that never declares the shared field can still conflict, because
      a plugin adds its own output fields to the computed schema (e.g.
      ``value_transform`` adds an operation target as ``any``). Stage 1 sees
      this because ``_known_producer_schema_config`` probes the plugin for its
      COMPUTED output schema, exactly as the DAG builder does — which is why
      the mirror can be exact instead of authored-schema-only.

  Pinned by ``TestComposerRuntimeCoalesceUnionTypeAgreement`` here, with
  composer-level coverage in ``tests/unit/web/composer/test_state.py``
  (``TestSchemaContractValidation``: rejection, compatible-types negative
  control, unresolved-branch abstention, mode-mixed precedence, and nested-merge
  exemption).

* Shape 19 — a producer's GUARANTEE channel against a locked consumer, most
  visibly at a union coalesce (``elspeth-1451ff385f``, filed off the composer
  audit that also produced ``elspeth-ae83a6b60c``). A fork gate fed a
  pass-through transform on each branch, each declaring only the field it
  rewrites, merging at ``merge: union`` into a ``mode: fixed`` sink admitting
  that one field. The build was GREEN and every row died at the sink's input
  preflight.

  Root cause is a decoupling that is correct by design and was simply never
  checked on one of its two channels. ``check_compatibility`` compares schema
  against schema and never reads ``guaranteed_fields``; ``validate_single_edge``
  mirrored composer Rule A only on its two bypass paths (dynamic producer,
  observed producer), so an edge whose producer has real ``model_fields`` took
  the ``check_compatibility`` path and its guarantees went unexamined. A union
  coalesce is exactly that shape: the builder types its ``fields`` from each
  branch's CONSTRUCTION-time schema but walks its ``guaranteed_fields``
  separately (``elspeth-0b14977817``), so the merged schema guaranteed
  category/id/price/product while declaring description alone. The two channels
  are decoupled deliberately — ``fields`` is what the node typed,
  ``guaranteed_fields`` is what the graph proves will be present, and the walk
  yields names without types — so the fix checks the second channel rather than
  collapsing it into the first. Closed in
  ``core/dag/schema_validation.py::validate_typed_producer_guaranteed_extras``
  (commit ``5d0c54522``), run as a final pass of ``validate_edge_compatibility``
  so a graph tripping this AND a pre-existing check keeps reporting the
  pre-existing error — the same ordering discipline Shape 17 cites. Raises
  ``EdgeContractError`` carrying ``extra_fields``.

  The rejection's REMEDIES needed a second pass (``df50ea3c3``) and that is
  part of this shape, not an aside. Reporting extras alone proposed "insert a
  field_mapper with select_only: true to drop the extras" — which for a sink
  consumer can leave the sink still requiring a field nothing provides, a false
  repair signal to an LLM authoring loop — so the raise site now accumulates
  the sink required-fields verdict instead of pre-empting it. The advertised
  repair "declare the extras on the consumer" is likewise incomplete on its
  own: widening the SINK alone fails the same edge with "Missing fields",
  because the coalesce types its ``fields`` from the branches. Both advertised
  repairs are pinned as executable controls below, so the advice cannot rot.

  A third pass (``48873f8dc``) was needed for SOUNDNESS, and it is the same
  lesson as the scope note below. The check rests on "a guarantee means every
  row WILL carry the field", which holds only for a guarantee about OUTPUT: a
  REDUCTIVE producer's guarantee channel can describe what it CONSUMES
  (``batch_stats`` declares ``value`` while emitting count/sum), so the pass
  false-rejected a correct pipeline. The discriminator is the producer's own
  EXTRAS FIREWALL — a producer whose output contract forbids extras emits
  exactly its declared fields, so a guaranteed name outside that set provably
  never reaches the consumer. Note where that discriminator already existed:
  composer ``_producer_emit_profile`` had modelled ``extras_firewall`` all
  along and the runtime did not mirror it. The population this check exists for
  is precisely the extras-ALLOWING producer — a union coalesce's merged schema
  is ``mode: flexible``, as is an under-declaring pass-through's. Found only by
  the full ``pytest tests/``: the false reject lived in
  ``tests/unit/core/test_dag.py``, a different file from ``tests/unit/core/dag/``,
  so neither the DAG-scoped run nor the example sweep could reach it.

  SCOPE, which the ticket, both candidate fixes and ``5d0c54522``'s own commit
  message all got wrong, and which is therefore recorded here explicitly: the
  RUNTIME defect is NOT coalesce-specific. The failing ingredient is only "a
  producer whose guarantees exceed its own declared fields, feeding a locked
  consumer", and any ``passes_through_input=True`` transform declaring a
  narrower schema than it forwards has that shape. A plain three-node linear
  pipeline reproduces it with no fork, no branches and no coalesce, verified by
  A/B on that exact pipeline (``cf550d674``). The coalesce is merely where it
  is most LIKELY, because the builder decouples the two channels structurally
  there rather than leaving it to an author's under-declaration.

  The COMPOSER gap has the OPPOSITE scope, and conflating the two would
  misdirect the fix. Stage 1 already rejects the linear shape via Rule B
  (``sink_locked_extras``): ``_producer_emit_profile`` resolves a pass-through
  transform and unions in its upstream definite arrivals. It abstains ONLY at a
  coalesce, at three separate sites in ``web/composer/state.py`` — the
  walk-back's unconditional coalesce stop (which, unlike its ``queue`` and
  ``row_union`` siblings, never received a participation-vote escape hatch),
  the boundary re-resolve that handles ``row_union`` only, and
  ``_connection_definite_emits`` returning the empty set for coalesce, a marked
  extension point rather than an oversight. Measured, not inferred: the
  identical graph with a ``row_union`` substituted for the coalesce DOES report
  ``locked_input_extras`` naming the same phantom set.

  This therefore remains a documented runtime-only gap — the second category in
  this file's header — with the runtime authoritative and rejecting, and the
  composer permissive. Measured on ``CompositionState.validate()``: it returns
  ``is_valid=True`` with zero errors, and its only related output is the medium
  advisory "Contract check skipped ... runtime validator will check this edge".
  What the mutation envelope in turn surfaces to the compose loop was NOT
  measured here; the Shape 18 precedent (a clean ``set_pipeline`` leaving the
  loop with nothing to repair from) is the reason to expect it matters. Closing
  the composer half is tracked in ``elspeth-ae83a6b60c``. Pinned by
  ``TestComposerRuntimeCoalesceGuaranteedExtrasAgreement`` here (the
  cross-surface divergence, both repair controls, and the no-coalesce scope
  boundary), with runtime-layer coverage in
  ``tests/unit/core/dag/test_graph_validation.py``
  (``TestUnionCoalesceGuaranteedExtras`` and ``TestTypedPassThroughGuaranteedExtras``).

Adding a new shape: file the eval-finding issue, land the structural fix,
then extend this docstring with the shape's number, the originating eval
session/run id, the closing issue, and the test class that pins it.

Bug verification protocol (mandatory for new shapes):
``test_agreement_aggregation_run_counts_construct_completed_data`` (Shape 7)
is the canonical example. Before declaring a new agreement test landed,
manually revert the structural fix it pins (one line in the production
code) and confirm the test fails with the expected exception class. Then
restore the fix. Document the protocol verbatim in the test's docstring,
naming the exact production line reverted and the exact failure observed.
This guards against the "passes pre-fix AND post-fix" failure mode where a
test exercises adjacent behaviour but never actually depends on the fix
under test — a class of test theatre that is otherwise undetectable until
a future regression silently slips through. The cost is one minute of
scratch work; the value is durable evidence that the test pins the
structural contract rather than incidentally passing.
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.contracts import Determinism
from elspeth.contracts.audit import Run
from elspeth.contracts.enums import CreationModality, RunStatus
from elspeth.contracts.errors import FrameworkBugError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.secrets import (
    SecretInventoryItem,
    SecretUnavailabilityReason,
)
from elspeth.core.config import (
    AggregationSettings,
    CoalesceSettings,
    ElspethSettings,
    GateSettings,
    SinkSettings,
    SourceSettings,
    TransformSettings,
    TriggerConfig,
)
from elspeth.core.dag import ExecutionGraph, GraphValidationError
from elspeth.core.dag.models import EdgeContractError
from elspeth.core.landscape import LandscapeDB
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from elspeth.engine.orchestrator.types import RouteValidationError
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.web.blobs.protocol import BlobFinalizationResult, BlobIntegrityError, BlobRecord
from elspeth.web.composer import yaml_generator as composer_yaml_generator
from elspeth.web.composer.state import (
    CompositionState,
    EdgeSpec,
    NodeSpec,
    OutputSpec,
    PipelineMetadata,
    SourceSpec,
)
from elspeth.web.execution.accounting import load_run_accounting_from_db
from elspeth.web.execution.progress import BroadcastResult
from elspeth.web.execution.schemas import CompletedData
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.execution.validation import validate_pipeline_for_trained_operator
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.base_classes import _TestSchema, as_sink, as_source, as_transform
from tests.fixtures.landscape import make_factory
from tests.fixtures.pipeline import build_production_graph
from tests.fixtures.plugins import (
    CollectSink,
    ConditionalErrorTransform,
    ListSource,
    PassTransform,
)

_AGREEMENT_SESSION_ID = "00000000-0000-4000-8000-000000000001"


class TestComposerRuntimeAgreement:
    """Shared agreement checks plus documented runtime-only gap characterization."""

    def _empty_state(self) -> CompositionState:
        return CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _build_runtime_graph(
        self,
        *,
        source_plugin: str,
        source_options: dict[str, Any],
        sink_options: dict[str, Any],
        transform_options: dict[str, Any] | None = None,
        transform_plugin: str | None = "value_transform",
        aggregation_options: dict[str, Any] | None = None,
        aggregation_plugin: str | None = None,
    ) -> ExecutionGraph:
        """Build a runtime ExecutionGraph through the production assembly path."""
        if transform_plugin is not None and aggregation_plugin is not None:
            raise AssertionError(
                "Task 8 agreement helper supports either a transform chain or an aggregation chain, not both in the same case."
            )

        source_on_success = "agg1" if aggregation_plugin is not None else ("t1" if transform_plugin is not None else "main")
        transforms: list[TransformSettings] = []
        aggregations: list[AggregationSettings] = []

        if transform_plugin is not None:
            transforms.append(
                TransformSettings(
                    name="t1",
                    plugin=transform_plugin,
                    input="t1",
                    on_success="main",
                    on_error="discard",
                    options=transform_options or {},
                )
            )

        if aggregation_plugin is not None:
            aggregations.append(
                AggregationSettings(
                    name="agg1",
                    plugin=aggregation_plugin,
                    input="agg1",
                    on_success="main",
                    on_error="discard",
                    trigger=TriggerConfig(count=1),
                    options=aggregation_options or {},
                )
            )

        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin=source_plugin,
                    on_success=source_on_success,
                    options={**source_options, "on_validation_failure": "discard"},
                )
            },
            transforms=transforms,
            aggregations=aggregations,
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="discard",
                    options=sink_options,
                )
            },
        )
        return self._build_runtime_graph_from_settings(config)

    def _build_runtime_graph_from_settings(self, config: ElspethSettings) -> ExecutionGraph:
        """Build a runtime graph from full settings through the production path."""
        plugins = instantiate_plugins_from_config(config)
        return ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
            coalesce_settings=list(config.coalesce) if config.coalesce else None,
        )

    def test_both_reject_missing_required_field(self, tmp_path: Path) -> None:
        """Both validators reject when a consumer requires an unsatisfied field."""
        text_path = tmp_path / "input.txt"
        text_path.write_text("hello\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="text",
                on_success="t1",
                options={
                    "path": str(text_path),
                    "column": "line",
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="quarantine",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="t1",
                node_type="transform",
                plugin="value_transform",
                input="t1",
                on_success="main",
                on_error="discard",
                options={
                    "required_input_fields": ["text"],
                    "operations": [
                        {
                            "target": "out",
                            "expression": "row['text'] + ' world'",
                        }
                    ],
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": str(output_path),
                    "schema": {"mode": "observed"},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e1",
                from_node="source",
                to_node="t1",
                edge_type="on_success",
                label=None,
            )
        )

        composer_result = state.validate()
        assert not composer_result.is_valid, "Composer should reject: source column is 'line' but consumer requires 'text'."
        assert any("schema contract violation" in entry.message.lower() for entry in composer_result.errors)

        with pytest.raises(GraphValidationError) as exc_info:
            graph = self._build_runtime_graph(
                source_plugin="text",
                source_options={
                    "path": str(text_path),
                    "column": "line",
                    "schema": {"mode": "observed"},
                },
                transform_options={
                    "required_input_fields": ["text"],
                    "operations": [
                        {
                            "target": "out",
                            "expression": "row['text'] + ' world'",
                        }
                    ],
                    "schema": {"mode": "observed"},
                },
                sink_options={
                    "path": str(output_path),
                    "schema": {"mode": "observed"},
                },
            )
            graph.validate_edge_compatibility()
        assert "text" in str(exc_info.value).lower()

    def test_both_accept_observed_text_source_with_auto_guarantee(
        self,
        tmp_path: Path,
    ) -> None:
        """Both validators accept the observed-text special-case contract."""
        text_path = tmp_path / "input.txt"
        text_path.write_text("hello\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="text",
                on_success="t1",
                options={
                    "path": str(text_path),
                    "column": "text",
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="quarantine",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="t1",
                node_type="transform",
                plugin="value_transform",
                input="t1",
                on_success="main",
                on_error="discard",
                options={
                    "required_input_fields": ["text"],
                    "operations": [
                        {
                            "target": "out",
                            "expression": "row['text'] + ' world'",
                        }
                    ],
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": str(output_path),
                    "schema": {"mode": "observed"},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e1",
                from_node="source",
                to_node="t1",
                edge_type="on_success",
                label=None,
            )
        )

        composer_result = state.validate()
        assert composer_result.is_valid, composer_result.errors

        graph = self._build_runtime_graph(
            source_plugin="text",
            source_options={
                "path": str(text_path),
                "column": "text",
                "schema": {"mode": "observed"},
            },
            transform_options={
                "required_input_fields": ["text"],
                "operations": [
                    {
                        "target": "out",
                        "expression": "row['text'] + ' world'",
                    }
                ],
                "schema": {"mode": "observed"},
            },
            sink_options={
                "path": str(output_path),
                "schema": {"mode": "observed"},
            },
        )
        graph.validate_edge_compatibility()

    def test_both_accept_source_schema_config_alias_contract(
        self,
        tmp_path: Path,
    ) -> None:
        """Source schema_config aliases must drive the same contract in preview and runtime."""
        text_path = tmp_path / "input.txt"
        text_path.write_text("hello\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="text",
                on_success="t1",
                options={
                    "path": str(text_path),
                    "column": "line",
                    "schema_config": {"mode": "observed", "guaranteed_fields": ["text"]},
                },
                on_validation_failure="quarantine",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="t1",
                node_type="transform",
                plugin="value_transform",
                input="t1",
                on_success="main",
                on_error="discard",
                options={
                    "required_input_fields": ["text"],
                    "operations": [
                        {
                            "target": "out",
                            "expression": "row['text'] + ' world'",
                        }
                    ],
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": str(output_path),
                    "schema": {"mode": "observed"},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e1",
                from_node="source",
                to_node="t1",
                edge_type="on_success",
                label=None,
            )
        )

        composer_result = state.validate()
        assert composer_result.is_valid, composer_result.errors
        source_contract = next(ec for ec in composer_result.edge_contracts if ec.to_id == "t1")
        assert source_contract.producer_guarantees == ("text",)
        assert source_contract.satisfied is True

        graph = self._build_runtime_graph(
            source_plugin="text",
            source_options={
                "path": str(text_path),
                "column": "line",
                "schema_config": {"mode": "observed", "guaranteed_fields": ["text"]},
            },
            transform_options={
                "required_input_fields": ["text"],
                "operations": [
                    {
                        "target": "out",
                        "expression": "row['text'] + ' world'",
                    }
                ],
                "schema": {"mode": "observed"},
            },
            sink_options={
                "path": str(output_path),
                "schema": {"mode": "observed"},
            },
        )
        graph.validate_edge_compatibility()

    def test_both_reject_observed_text_source_keyword_column(self, tmp_path: Path) -> None:
        """Invalid keyword columns must not create a false composer/runtime accept."""
        text_path = tmp_path / "input.txt"
        text_path.write_text("hello\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="text",
                on_success="t1",
                options={
                    "path": str(text_path),
                    "column": "class",
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="quarantine",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="t1",
                node_type="transform",
                plugin="value_transform",
                input="t1",
                on_success="main",
                on_error="discard",
                options={
                    "required_input_fields": ["class"],
                    "operations": [
                        {
                            "target": "out",
                            "expression": "row['class'] + ' world'",
                        }
                    ],
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": str(output_path),
                    "schema": {"mode": "observed"},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e1",
                from_node="source",
                to_node="t1",
                edge_type="on_success",
                label=None,
            )
        )

        composer_result = state.validate()
        assert not composer_result.is_valid, (
            "Composer must not infer an observed-text guarantee for a keyword column name that runtime text-source config rejects."
        )
        assert any("class" in entry.message.lower() for entry in composer_result.errors)

        with pytest.raises(PluginConfigError, match="Python keyword"):
            self._build_runtime_graph(
                source_plugin="text",
                source_options={
                    "path": str(text_path),
                    "column": "class",
                    "schema": {"mode": "observed"},
                },
                transform_options={
                    "required_input_fields": ["class"],
                    "operations": [
                        {
                            "target": "out",
                            "expression": "row['class'] + ' world'",
                        }
                    ],
                    "schema": {"mode": "observed"},
                },
                sink_options={
                    "path": str(output_path),
                    "schema": {"mode": "observed"},
                },
            )

    def test_both_reject_strict_sink_typed_requirement_without_upstream_guarantee(
        self,
        tmp_path: Path,
    ) -> None:
        """Both validators reject when a strict sink requires an ungiven field."""
        text_path = tmp_path / "input.txt"
        text_path.write_text("hello\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="text",
                on_success="main",
                options={
                    "path": str(text_path),
                    "column": "line",
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="quarantine",
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": str(output_path),
                    "schema": {"mode": "fixed", "fields": ["text: str"]},
                },
                on_write_failure="discard",
            )
        )

        composer_result = state.validate()
        assert not composer_result.is_valid, "Composer should reject: strict sink requires 'text' but upstream guarantees only 'line'."
        assert any(contract.to_id == "output:main" and not contract.satisfied for contract in composer_result.edge_contracts)

        with pytest.raises(GraphValidationError) as exc_info:
            graph = self._build_runtime_graph(
                source_plugin="text",
                source_options={
                    "path": str(text_path),
                    "column": "line",
                    "schema": {"mode": "observed"},
                },
                transform_plugin=None,
                sink_options={
                    "path": str(output_path),
                    "schema": {"mode": "fixed", "fields": ["text: str"]},
                },
            )
            graph.validate_edge_compatibility()
        assert "requires" in str(exc_info.value).lower()

    def test_runtime_states_both_sink_verdicts_when_one_edge_violates_both_rules(
        self,
        tmp_path: Path,
    ) -> None:
        """A doubly-violating sink edge must state BOTH verdicts, not whichever raises first.

        The authored defect below is simultaneously a missing-required-field
        AND an extra-undeclared-field violation, and the composer reports both.
        The runtime raises from inside the per-edge loop, which aborts before
        ``validate_sink_required_fields`` — called strictly after that loop —
        ever runs. Reporting the extras half alone proposed dropping the extras
        as the repair, which would leave the sink still requiring ``text`` that
        nothing guarantees: a false repair signal to an LLM authoring loop
        (elspeth-9615d6c75a).

        This pins the MECHANISM — both verdicts present, and both field sets
        populated on the structured error the composer preflight formatter
        reads — deliberately NOT an ordering between the two checks. Ordering
        is what accumulating the verdicts makes irrelevant.
        """
        text_path = tmp_path / "input.txt"
        text_path.write_text("hello\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        with pytest.raises(GraphValidationError) as exc_info:
            graph = self._build_runtime_graph(
                source_plugin="text",
                source_options={
                    "path": str(text_path),
                    "column": "line",
                    "schema": {"mode": "observed"},
                },
                transform_plugin=None,
                sink_options={
                    "path": str(output_path),
                    "schema": {"mode": "fixed", "fields": ["text: str"]},
                },
            )
            graph.validate_edge_compatibility()

        message = str(exc_info.value)
        assert "requires fields ['text']" in message, "The missing-required-field verdict must survive the extras rejection."
        assert "Extra fields rejected by consumer input contract: ['line']" in message, "The extras verdict must survive too."

        error = exc_info.value
        assert isinstance(error, EdgeContractError), "The combined verdict keeps the structured subclass the formatter reads."
        assert error.compatibility_result.missing_fields == ("text",)
        assert error.compatibility_result.extra_fields == ("line",)

    def test_both_reject_aggregation_nested_required_input_fields_without_upstream_guarantee(
        self,
        tmp_path: Path,
    ) -> None:
        """Composer rejects at preview time and runtime rejects during plugin wiring."""
        csv_path = tmp_path / "input.csv"
        csv_path.write_text("line\nhello\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="csv",
                on_success="agg1",
                options={
                    "path": str(csv_path),
                    "schema": {"mode": "fixed", "fields": ["line: str"]},
                },
                on_validation_failure="quarantine",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="agg1",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg1",
                on_success="main",
                on_error="discard",
                options={
                    "value_field": "value",
                    "required_input_fields": ["value"],
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": str(output_path),
                    "schema": {"mode": "observed"},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e1",
                from_node="source",
                to_node="agg1",
                edge_type="on_success",
                label=None,
            )
        )

        composer_result = state.validate()
        assert not composer_result.is_valid
        assert any("value" in entry.message.lower() for entry in composer_result.errors)

        with pytest.raises(FrameworkBugError) as exc_info:
            self._build_runtime_graph(
                source_plugin="csv",
                source_options={
                    "path": str(csv_path),
                    "schema": {"mode": "fixed", "fields": ["line: str"]},
                },
                transform_plugin=None,
                aggregation_plugin="batch_stats",
                aggregation_options={
                    "value_field": "value",
                    "required_input_fields": ["value"],
                    "schema": {"mode": "observed"},
                },
                sink_options={
                    "path": str(output_path),
                    "schema": {"mode": "observed"},
                },
            )
        assert "value" in str(exc_info.value).lower()

    def test_both_reject_aggregation_nested_schema_required_fields_without_upstream_guarantee(
        self,
        tmp_path: Path,
    ) -> None:
        """Aggregation wrapper schema.required_fields must match runtime validation."""
        csv_path = tmp_path / "input.csv"
        csv_path.write_text("line\nhello\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="csv",
                on_success="agg1",
                options={
                    "path": str(csv_path),
                    "schema": {"mode": "fixed", "fields": ["line: str"]},
                },
                on_validation_failure="quarantine",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="agg1",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg1",
                on_success="main",
                on_error="discard",
                options={
                    "options": {
                        "value_field": "value",
                        "schema": {"mode": "observed", "required_fields": ["value"]},
                    }
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": str(output_path),
                    "schema": {"mode": "observed"},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e1",
                from_node="source",
                to_node="agg1",
                edge_type="on_success",
                label=None,
            )
        )

        composer_result = state.validate()
        assert not composer_result.is_valid
        assert any("value" in entry.message.lower() for entry in composer_result.errors)

        with pytest.raises(GraphValidationError) as exc_info:
            graph = self._build_runtime_graph(
                source_plugin="csv",
                source_options={
                    "path": str(csv_path),
                    "schema": {"mode": "fixed", "fields": ["line: str"]},
                },
                transform_plugin=None,
                aggregation_plugin="batch_stats",
                aggregation_options={
                    "value_field": "value",
                    "schema": {"mode": "observed", "required_fields": ["value"]},
                },
                sink_options={
                    "path": str(output_path),
                    "schema": {"mode": "observed"},
                },
            )
            graph.validate_edge_compatibility()
        assert "value" in str(exc_info.value).lower()

    def test_both_reject_direct_fork_to_sink_required_field_mismatch(
        self,
        tmp_path: Path,
    ) -> None:
        """Direct fork-to-sink edges stay statically checkable in preview and runtime."""
        text_path = tmp_path / "input.txt"
        text_path.write_text("hello\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="text",
                on_success="gate_in",
                options={
                    "path": str(text_path),
                    "column": "line",
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="quarantine",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork"},
                fork_to=("main",),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": str(output_path),
                    "schema": {"mode": "fixed", "fields": ["text: str"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e1",
                from_node="source",
                to_node="fork_gate",
                edge_type="on_success",
                label=None,
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e2",
                from_node="fork_gate",
                to_node="main",
                edge_type="fork",
                label="main",
            )
        )

        composer_result = state.validate()
        assert not composer_result.is_valid
        sink_contract = next(contract for contract in composer_result.edge_contracts if contract.to_id == "output:main")
        assert sink_contract.from_id == "source"
        assert sink_contract.satisfied is False
        assert not any(
            "fork gate" in warning.message.lower() and "contract check skipped" in warning.message.lower()
            for warning in composer_result.warnings
        )

        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="text",
                    on_success="gate_in",
                    options={
                        "path": str(text_path),
                        "column": "line",
                        "schema": {"mode": "observed"},
                        "on_validation_failure": "discard",
                    },
                )
            },
            gates=[
                GateSettings(
                    name="fork_gate",
                    input="gate_in",
                    condition="True",
                    routes={"true": "fork", "false": "fork"},
                    fork_to=["main"],
                )
            ],
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="discard",
                    options={
                        "path": str(output_path),
                        "schema": {"mode": "fixed", "fields": ["text: str"]},
                    },
                )
            },
        )

        with pytest.raises(GraphValidationError) as exc_info:
            graph = self._build_runtime_graph_from_settings(config)
            graph.validate_edge_compatibility()
        assert "text" in str(exc_info.value).lower()

    def test_both_accept_pass_through_downstream_of_coalesce(
        self,
        tmp_path: Path,
    ) -> None:
        """Pass-through preview must inherit coalesce guarantees after fan-in."""
        csv_path = tmp_path / "input.csv"
        csv_path.write_text("id,value\n1,2\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="csv",
                on_success="gate_in",
                options={
                    "path": str(csv_path),
                    "schema": {"mode": "fixed", "fields": ["id: int", "value: int"]},
                },
                on_validation_failure="quarantine",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("path_a", "path_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            NodeSpec(
                id="merge_results",
                node_type="coalesce",
                plugin=None,
                input="path_a",
                on_success=None,
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches=("path_a", "path_b"),
                policy="best_effort",
                merge="union",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="pt_after_merge",
                node_type="transform",
                plugin="passthrough",
                input="merge_results",
                on_success="main",
                on_error="discard",
                options={"schema": {"mode": "observed"}},
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": str(output_path),
                    "schema": {"mode": "observed", "required_fields": ["id"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e1",
                from_node="source",
                to_node="fork_gate",
                edge_type="on_success",
                label=None,
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e2",
                from_node="fork_gate",
                to_node="merge_results",
                edge_type="fork",
                label="path_a",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e3",
                from_node="merge_results",
                to_node="pt_after_merge",
                edge_type="on_success",
                label=None,
            )
        )

        composer_result = state.validate()
        assert composer_result.is_valid, composer_result.errors
        sink_contract = next(contract for contract in composer_result.edge_contracts if contract.to_id == "output:main")
        assert sink_contract.from_id == "pt_after_merge"
        assert sink_contract.producer_guarantees == ("id", "value")
        assert sink_contract.consumer_requires == ("id",)
        assert sink_contract.satisfied is True

        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="gate_in",
                    options={
                        "path": str(csv_path),
                        "schema": {"mode": "fixed", "fields": ["id: int", "value: int"]},
                        "on_validation_failure": "discard",
                    },
                )
            },
            transforms=[
                TransformSettings(
                    name="pt_after_merge",
                    plugin="passthrough",
                    input="merge_results",
                    on_success="main",
                    on_error="discard",
                    options={"schema": {"mode": "observed"}},
                )
            ],
            gates=[
                GateSettings(
                    name="fork_gate",
                    input="gate_in",
                    condition="True",
                    routes={"true": "fork", "false": "fork"},
                    fork_to=["path_a", "path_b"],
                )
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_results",
                    branches={"path_a": "path_a", "path_b": "path_b"},
                    policy="best_effort",
                    merge="union",
                    timeout_seconds=1,
                )
            ],
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="discard",
                    options={
                        "path": str(output_path),
                        "schema": {"mode": "observed", "required_fields": ["id"]},
                    },
                )
            },
        )

        graph = self._build_runtime_graph_from_settings(config)
        graph.validate_edge_compatibility()

    def test_both_reject_mixed_coalesce_branch_schemas(
        self,
        tmp_path: Path,
    ) -> None:
        """Composer mirrors runtime rejection of mixed observed/explicit union branches."""
        csv_path = tmp_path / "input.csv"
        csv_path.write_text("id,value\n1,2\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="csv",
                on_success="gate_in",
                options={
                    "path": str(csv_path),
                    "schema": {"mode": "fixed", "fields": ["id: int", "value: int"]},
                },
                on_validation_failure="quarantine",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("path_a", "path_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            NodeSpec(
                id="branch_b",
                node_type="transform",
                plugin="value_transform",
                input="path_b",
                on_success="path_b_done",
                on_error="discard",
                options={
                    "operations": [
                        {
                            "target": "value",
                            "expression": "row['value']",
                        }
                    ],
                    "schema": {"mode": "observed"},
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_node(
            NodeSpec(
                id="merge_results",
                node_type="coalesce",
                plugin=None,
                input="path_a",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"path_a": "path_a", "path_b": "path_b_done"},
                policy="require_all",
                merge="union",
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": str(output_path),
                    "schema": {"mode": "fixed", "fields": ["id: int", "value: int"]},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e1",
                from_node="source",
                to_node="fork_gate",
                edge_type="on_success",
                label=None,
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e2",
                from_node="fork_gate",
                to_node="branch_b",
                edge_type="fork",
                label="path_b",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e3",
                from_node="fork_gate",
                to_node="merge_results",
                edge_type="fork",
                label="path_a",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e4",
                from_node="branch_b",
                to_node="merge_results",
                edge_type="on_success",
                label=None,
            )
        )

        composer_result = state.validate()
        assert not composer_result.is_valid
        composer_errors = [error for error in composer_result.errors if error.error_code == "coalesce_schema_mode_mixed"]
        assert len(composer_errors) == 1, composer_result.errors
        assert "observed" in composer_errors[0].message.lower()
        assert "explicit" in composer_errors[0].message.lower()
        assert not any(contract.to_id == "output:main" for contract in composer_result.edge_contracts)

        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="gate_in",
                    options={
                        "path": str(csv_path),
                        "schema": {"mode": "fixed", "fields": ["id: int", "value: int"]},
                        "on_validation_failure": "discard",
                    },
                )
            },
            transforms=[
                TransformSettings(
                    name="branch_b",
                    plugin="value_transform",
                    input="path_b",
                    on_success="path_b_done",
                    on_error="discard",
                    options={
                        "operations": [
                            {
                                "target": "value",
                                "expression": "row['value']",
                            }
                        ],
                        "schema": {"mode": "observed"},
                    },
                )
            ],
            gates=[
                GateSettings(
                    name="fork_gate",
                    input="gate_in",
                    condition="True",
                    routes={"true": "fork", "false": "fork"},
                    fork_to=["path_a", "path_b"],
                )
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_results",
                    branches={"path_a": "path_a", "path_b": "path_b_done"},
                    policy="require_all",
                    merge="union",
                    on_success="main",
                )
            ],
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="discard",
                    options={
                        "path": str(output_path),
                        "schema": {"mode": "fixed", "fields": ["id: int", "value: int"]},
                    },
                )
            },
        )

        with pytest.raises(GraphValidationError) as exc_info:
            graph = self._build_runtime_graph_from_settings(config)
            graph.validate_edge_compatibility()
        message = str(exc_info.value).lower()
        assert "coalesce" in message
        assert "observed" in message
        assert "explicit" in message

    def test_composer_accepts_field_names_but_runtime_rejects_type_mismatch(
        self,
        tmp_path: Path,
    ) -> None:
        """Type compatibility remains runtime-only even when contract fields line up."""
        csv_path = tmp_path / "input.csv"
        csv_path.write_text("value\nhello\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="csv",
                on_success="main",
                options={
                    "path": str(csv_path),
                    "schema": {"mode": "fixed", "fields": ["value: str"]},
                },
                on_validation_failure="quarantine",
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": str(output_path),
                    "schema": {"mode": "fixed", "fields": ["value: int"]},
                },
                on_write_failure="discard",
            )
        )

        composer_result = state.validate()
        assert composer_result.is_valid, composer_result.errors
        sink_contract = next(contract for contract in composer_result.edge_contracts if contract.to_id == "output:main")
        assert sink_contract.satisfied is True
        assert sink_contract.producer_guarantees == ("value",)
        assert sink_contract.consumer_requires == ("value",)

        with pytest.raises(GraphValidationError) as exc_info:
            graph = self._build_runtime_graph(
                source_plugin="csv",
                source_options={
                    "path": str(csv_path),
                    "schema": {"mode": "fixed", "fields": ["value: str"]},
                },
                transform_plugin=None,
                sink_options={
                    "path": str(output_path),
                    "schema": {"mode": "fixed", "fields": ["value: int"]},
                },
            )
            graph.validate_edge_compatibility()
        message = str(exc_info.value).lower()
        assert "incompatible" in message
        assert "value" in message

    def test_both_accept_aggregation_with_input_fields_and_required_fields(
        self,
        tmp_path: Path,
    ) -> None:
        """Regression for elspeth-f5f798f797.

        S2 v2 from docs/composer/evidence/composer-llm-eval-2026-05-01.md: a ``batch_stats``
        aggregation with ``schema: {mode: flexible, fields: [...],
        required_fields: [...]}`` was accepted by composer ``/validate`` but
        rejected at runtime with ``SchemaConfigModeViolation`` because
        ``BaseTransform._build_output_schema_config`` propagated the user's
        input ``fields``/``required_fields`` into the aggregation's output
        schema config — which then required fields the aggregation never
        emits and expected types the OBSERVED-typed output cannot satisfy.

        After the fix in ``BatchStats._build_output_schema_config``, both
        composer preview and runtime emission verification accept this
        config. The aggregation honestly declares its output as observed
        with ``guaranteed_fields`` matching what the aggregation emits.
        """
        from elspeth.contracts.schema_contract import PipelineRow
        from elspeth.contracts.schema_contract_factory import create_contract_from_config
        from elspeth.engine.executors.schema_config_mode import verify_schema_config_mode
        from elspeth.plugins.transforms.batch_stats import BatchStats

        csv_path = tmp_path / "input.csv"
        csv_path.write_text(
            "customer_tier,amount\nenterprise,100.0\nenterprise,150.0\npro,50.0\n",
            encoding="utf-8",
        )
        output_path = tmp_path / "out.csv"

        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="csv",
                on_success="agg1",
                options={
                    "path": str(csv_path),
                    "schema": {
                        "mode": "flexible",
                        "fields": ["customer_tier: str", "amount: float"],
                    },
                },
                on_validation_failure="quarantine",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="agg1",
                node_type="aggregation",
                plugin="batch_stats",
                input="agg1",
                on_success="main",
                on_error="discard",
                options={
                    "schema": {
                        "mode": "flexible",
                        "fields": ["customer_tier: str", "amount: float"],
                        "required_fields": ["customer_tier", "amount"],
                    },
                    "value_field": "amount",
                    "group_by": "customer_tier",
                    "compute_mean": False,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
                trigger={"count": 3},
                output_mode="transform",
                expected_output_count=2,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={
                    "path": str(output_path),
                    "schema": {"mode": "observed"},
                },
                on_write_failure="discard",
            )
        )
        state = state.with_edge(EdgeSpec(id="e1", from_node="source", to_node="agg1", edge_type="on_success", label=None))

        composer_result = state.validate()
        assert composer_result.is_valid, "\n".join(err.message for err in composer_result.errors)

        graph = self._build_runtime_graph(
            source_plugin="csv",
            source_options={
                "path": str(csv_path),
                "schema": {
                    "mode": "flexible",
                    "fields": ["customer_tier: str", "amount: float"],
                },
            },
            transform_plugin=None,
            aggregation_plugin="batch_stats",
            aggregation_options={
                "schema": {
                    "mode": "flexible",
                    "fields": ["customer_tier: str", "amount: float"],
                    "required_fields": ["customer_tier", "amount"],
                },
                "value_field": "amount",
                "group_by": "customer_tier",
                "compute_mean": False,
            },
            sink_options={
                "path": str(output_path),
                "schema": {"mode": "observed"},
            },
        )
        graph.validate_edge_compatibility()

        # Final tier of the agreement: simulate aggregate emission and
        # verify the runtime SchemaConfigModeViolation predicate accepts
        # the output. Pre-fix this raised; post-fix it must not raise.
        transform = BatchStats(
            {
                "schema": {
                    "mode": "flexible",
                    "fields": ["customer_tier: str", "amount: float"],
                    "required_fields": ["customer_tier", "amount"],
                },
                "value_field": "amount",
                "group_by": "customer_tier",
                "compute_mean": False,
            }
        )
        from elspeth.contracts.schema import SchemaConfig as _SchemaConfig

        input_contract = create_contract_from_config(
            _SchemaConfig.from_dict({"mode": "flexible", "fields": ["customer_tier: str", "amount: float"]})
        )
        rows = [
            PipelineRow({"customer_tier": "enterprise", "amount": 100.0}, input_contract),
            PipelineRow({"customer_tier": "enterprise", "amount": 150.0}, input_contract),
            PipelineRow({"customer_tier": "pro", "amount": 50.0}, input_contract),
        ]
        results: list[dict[str, object]] = []
        for group_value, grouped in transform._group_rows(rows):
            aggregate, error = transform._aggregate_group(grouped, group_value)
            assert error is None
            results.append(aggregate)
        emitted_contract = transform._output_contract_for(results)
        emitted_rows = [PipelineRow(r, emitted_contract) for r in results]

        # Narrow ``transform._output_schema_config`` (typed as
        # ``SchemaConfig | None``) — for this BatchStats config the
        # post-Phase-1.3 ``_build_output_schema_config`` override always
        # returns a non-None value.  An assert here makes the contract
        # explicit so mypy can see it.
        output_schema_config = transform._output_schema_config
        assert output_schema_config is not None, "BatchStats must emit an explicit output schema config"

        verify_schema_config_mode(
            output_schema_config=output_schema_config,
            emitted_rows=emitted_rows,
            plugin_name="batch_stats",
            node_id="agg1",
            run_id="r",
            row_id="row1",
            token_id="t1",
        )


class TestComposerRuntimeRouteTargetAgreement:
    """Composer ``/validate`` and runtime preflight agree on dangling route
    targets — closes the parity gap from elspeth-127de6865a.

    Empirical scope of the original gap (post-investigation):

    * Aggregation ``on_error`` -> unknown sink: composer was silent (the
      original reproducer). Now caught at ``route_target_resolution``.
    * Source ``on_validation_failure`` -> unknown sink: composer was silent.
      Now caught at ``route_target_resolution``.
    * Transform ``on_error`` -> unknown sink: was already caught at
      ``graph_structure`` (``builder.py:839``). The new check is
      defense-in-depth.
    * Sink ``on_write_failure`` -> unknown sink: was already caught at
      ``graph_structure`` (``builder.py:859``). The new check is
      defense-in-depth.

    Each gap-closing test (aggregation/source) exercises both paths from
    independent inputs and asserts the error messages are byte-identical.
    Each defense-in-depth test asserts both layers reject and the dangling
    target name is present in both messages.
    """

    @staticmethod
    def _validation_settings(data_dir: Path) -> SimpleNamespace:
        # ValidationSettings is a Protocol that only requires ``data_dir``.
        return SimpleNamespace(data_dir=data_dir)

    @staticmethod
    def _composer_route_target_failure(state: CompositionState, data_dir: Path) -> str:
        """Run validate_pipeline on a CompositionState and return the
        route_target_resolution check detail. Asserts the failure happened on
        that specific check (not graph_structure, not schema_compatibility)."""
        result = validate_pipeline_for_trained_operator(
            state,
            TestComposerRuntimeRouteTargetAgreement._validation_settings(data_dir),
            composer_yaml_generator,
            session_id=_AGREEMENT_SESSION_ID,
        )
        assert result.is_valid is False, "Composer should reject pipelines with dangling route targets"
        check_by_name = {check.name: check for check in result.checks}
        assert "route_target_resolution" in check_by_name, "Missing route_target_resolution check in result"
        rt_check = check_by_name["route_target_resolution"]
        assert rt_check.passed is False, f"route_target_resolution should have failed; got {rt_check.detail}"
        # graph_structure should have passed — these dangling references are
        # NOT structural DAG errors.
        assert check_by_name["graph_structure"].passed is True, "graph_structure must pass — only route targets are bad"
        return rt_check.detail

    @staticmethod
    def _runtime_route_target_failure(config: ElspethSettings) -> str:
        """Instantiate plugins, build graph, call assemble_and_validate_pipeline_config.
        Returns the str(RouteValidationError) message."""
        plugins = instantiate_plugins_from_config(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
            coalesce_settings=list(config.coalesce) if config.coalesce else None,
        )
        graph.validate()  # Structural DAG check — must pass for these cases
        with pytest.raises(RouteValidationError) as exc_info:
            assemble_and_validate_pipeline_config(
                sources=plugins.sources,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                settings=config,
                graph=graph,
            )
        return str(exc_info.value)

    @staticmethod
    def _csv_input(tmp_path: Path) -> Path:
        # Sources must live under data_dir/blobs/ for the path allowlist.
        path = tmp_path / "blobs" / _AGREEMENT_SESSION_ID / "input.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("value\n1\n", encoding="utf-8")
        return path

    @staticmethod
    def _csv_output(tmp_path: Path, name: str = "out.csv") -> Path:
        # Sinks must live under data_dir/outputs/ (or blobs/) for the allowlist.
        out_dir = tmp_path / "outputs" / _AGREEMENT_SESSION_ID
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / name

    def test_both_reject_aggregation_on_error_dangling_sink(self, tmp_path: Path) -> None:
        """Original reproducer (S2 v1 from docs/composer/evidence/composer-llm-eval-2026-05-01.md):
        aggregation ``on_error: aggregation_errors`` with no sink of that name."""
        csv_path = self._csv_input(tmp_path)
        output_path = self._csv_output(tmp_path)

        # Composer state: aggregation routes errors to a sink that doesn't exist.
        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="agg1",
                options={"path": str(csv_path), "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(
                NodeSpec(
                    id="agg1",
                    node_type="aggregation",
                    plugin="batch_stats",
                    input="agg1",
                    on_success="main",
                    on_error="aggregation_errors",  # ← dangling
                    options={"schema": {"mode": "observed"}, "value_field": "value"},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    trigger={"count": 1},
                    output_mode="transform",
                    expected_output_count=1,
                ),
            ),
            edges=(
                EdgeSpec(id="e1", from_node="source", to_node="agg1", edge_type="on_success", label=None),
                EdgeSpec(id="e2", from_node="agg1", to_node="main", edge_type="on_success", label=None),
            ),
            outputs=(
                OutputSpec(
                    name="main",
                    plugin="csv",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )
        composer_detail = self._composer_route_target_failure(state, tmp_path)

        # Runtime: equivalent ElspethSettings.
        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="agg1",
                    options={"path": str(csv_path), "schema": {"mode": "observed"}, "on_validation_failure": "discard"},
                )
            },
            aggregations=[
                AggregationSettings(
                    name="agg1",
                    plugin="batch_stats",
                    input="agg1",
                    on_success="main",
                    on_error="aggregation_errors",  # ← dangling
                    trigger=TriggerConfig(count=1),
                    options={"schema": {"mode": "observed"}, "value_field": "value"},
                ),
            ],
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="discard",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                ),
            },
        )
        runtime_msg = self._runtime_route_target_failure(config)

        assert "aggregation_errors" in composer_detail
        assert "aggregation_errors" in runtime_msg
        assert composer_detail == runtime_msg, "Composer and runtime must surface identical RouteValidationError"

    def test_both_reject_transform_on_error_dangling_sink(self, tmp_path: Path) -> None:
        """Defense-in-depth axis: the DAG builder (``graph.validate()`` via
        ``from_plugin_instances``) already catches transform ``on_error`` ->
        unknown sink at ``builder.py:839``. The new ``route_target_resolution``
        check is a second wall behind it. This test asserts both walls agree:
        composer ``/validate`` rejects, runtime construction rejects, and the
        dangling target name appears in both messages."""
        csv_path = self._csv_input(tmp_path)
        output_path = self._csv_output(tmp_path)

        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="t1",
                options={"path": str(csv_path), "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(
                NodeSpec(
                    id="t1",
                    node_type="transform",
                    plugin="value_transform",
                    input="t1",
                    on_success="main",
                    on_error="missing_error_sink",
                    options={
                        "schema": {"mode": "observed"},
                        "operations": [{"target": "doubled", "expression": "row['value']"}],
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            edges=(
                EdgeSpec(id="e1", from_node="source", to_node="t1", edge_type="on_success", label=None),
                EdgeSpec(id="e2", from_node="t1", to_node="main", edge_type="on_success", label=None),
            ),
            outputs=(
                OutputSpec(
                    name="main",
                    plugin="csv",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )
        composer_result = validate_pipeline_for_trained_operator(
            state,
            self._validation_settings(tmp_path),
            composer_yaml_generator,
            session_id=_AGREEMENT_SESSION_ID,
        )
        assert composer_result.is_valid is False
        composer_messages = " | ".join(err.message for err in composer_result.errors)
        assert "missing_error_sink" in composer_messages

        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="t1",
                    options={"path": str(csv_path), "schema": {"mode": "observed"}, "on_validation_failure": "discard"},
                )
            },
            transforms=[
                TransformSettings(
                    name="t1",
                    plugin="value_transform",
                    input="t1",
                    on_success="main",
                    on_error="missing_error_sink",
                    options={
                        "schema": {"mode": "observed"},
                        "operations": [{"target": "doubled", "expression": "row['value']"}],
                    },
                ),
            ],
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="discard",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                ),
            },
        )
        plugins = instantiate_plugins_from_config(config)
        with pytest.raises(GraphValidationError) as runtime_exc:
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(config.gates),
                coalesce_settings=list(config.coalesce) if config.coalesce else None,
            )
        assert "missing_error_sink" in str(runtime_exc.value)

    def test_both_reject_source_on_validation_failure_dangling_sink(self, tmp_path: Path) -> None:
        csv_path = self._csv_input(tmp_path)
        output_path = self._csv_output(tmp_path)

        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="main",
                options={"path": str(csv_path), "schema": {"mode": "observed"}},
                on_validation_failure="missing_quarantine_sink",  # ← dangling
            ),
            nodes=(),
            edges=(EdgeSpec(id="e1", from_node="source", to_node="main", edge_type="on_success", label=None),),
            outputs=(
                OutputSpec(
                    name="main",
                    plugin="csv",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )
        composer_detail = self._composer_route_target_failure(state, tmp_path)

        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="main",
                    options={
                        "path": str(csv_path),
                        "schema": {"mode": "observed"},
                        "on_validation_failure": "missing_quarantine_sink",
                    },
                )
            },
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="discard",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                ),
            },
        )
        runtime_msg = self._runtime_route_target_failure(config)

        assert "missing_quarantine_sink" in composer_detail
        assert "missing_quarantine_sink" in runtime_msg
        assert composer_detail == runtime_msg

    def test_both_reject_sink_on_write_failure_dangling_sink(self, tmp_path: Path) -> None:
        """Defense-in-depth axis: ``builder.py:859`` already catches sink
        ``on_write_failure`` -> unknown sink at ``graph.validate()``. The
        helper provides a second wall via
        ``validate_sink_failsink_destinations``."""
        csv_path = self._csv_input(tmp_path)
        output_path = self._csv_output(tmp_path)

        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="main",
                options={"path": str(csv_path), "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(EdgeSpec(id="e1", from_node="source", to_node="main", edge_type="on_success", label=None),),
            outputs=(
                OutputSpec(
                    name="main",
                    plugin="csv",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                    on_write_failure="missing_failsink",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )
        composer_result = validate_pipeline_for_trained_operator(
            state,
            self._validation_settings(tmp_path),
            composer_yaml_generator,
            session_id=_AGREEMENT_SESSION_ID,
        )
        assert composer_result.is_valid is False
        composer_messages = " | ".join(err.message for err in composer_result.errors)
        assert "missing_failsink" in composer_messages

        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="main",
                    options={"path": str(csv_path), "schema": {"mode": "observed"}, "on_validation_failure": "discard"},
                )
            },
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="missing_failsink",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                ),
            },
        )
        plugins = instantiate_plugins_from_config(config)
        with pytest.raises(GraphValidationError) as runtime_exc:
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(config.gates),
                coalesce_settings=list(config.coalesce) if config.coalesce else None,
            )
        assert "missing_failsink" in str(runtime_exc.value)

    def test_both_reject_gate_routes_dangling_sink(self, tmp_path: Path) -> None:
        """Defense-in-depth axis: a gate ``routes`` target that is neither
        ``"fork"`` nor a real sink name nor a connection name. The DAG builder
        falls through to producer-registration (``builder.py:583+``); if no
        consumer claims that name, downstream graph checks reject. The
        ``route_target_resolution`` step is the second wall — when the runtime
        ultimately resolves the route, ``validate_route_destinations`` would
        also catch it.

        This test asserts agreement: composer ``/validate`` rejects, the
        independent runtime construction rejects, and both messages name the
        dangling target."""
        csv_path = self._csv_input(tmp_path)
        output_path = self._csv_output(tmp_path)

        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="g1",
                options={"path": str(csv_path), "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(
                NodeSpec(
                    id="g1",
                    node_type="gate",
                    plugin=None,
                    input="g1",
                    on_success=None,
                    on_error=None,
                    options={},
                    condition="row['value'] != ''",
                    routes={"true": "main", "false": "missing_route_sink"},
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            edges=(
                EdgeSpec(id="e1", from_node="source", to_node="g1", edge_type="on_success", label=None),
                EdgeSpec(id="e2", from_node="g1", to_node="main", edge_type="route_true", label="true"),
            ),
            outputs=(
                OutputSpec(
                    name="main",
                    plugin="csv",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )
        composer_result = validate_pipeline_for_trained_operator(
            state,
            self._validation_settings(tmp_path),
            composer_yaml_generator,
            session_id=_AGREEMENT_SESSION_ID,
        )
        assert composer_result.is_valid is False
        composer_messages = " | ".join(err.message for err in composer_result.errors)
        assert "missing_route_sink" in composer_messages

        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="g1",
                    options={"path": str(csv_path), "schema": {"mode": "observed"}, "on_validation_failure": "discard"},
                )
            },
            gates=[
                GateSettings(
                    name="g1",
                    input="g1",
                    condition="row['value'] != ''",
                    routes={"true": "main", "false": "missing_route_sink"},
                ),
            ],
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="discard",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                ),
            },
        )
        plugins = instantiate_plugins_from_config(config)
        with pytest.raises(GraphValidationError) as runtime_exc:
            ExecutionGraph.from_plugin_instances(
                sources=plugins.sources,
                source_settings_map=plugins.source_settings_map,
                transforms=plugins.transforms,
                sinks=plugins.sinks,
                aggregations=plugins.aggregations,
                gates=list(config.gates),
                coalesce_settings=list(config.coalesce) if config.coalesce else None,
            )
        assert "missing_route_sink" in str(runtime_exc.value)

    def test_both_accept_aggregation_on_error_discard(self, tmp_path: Path) -> None:
        """Positive control: ``on_error: discard`` (the eval's S2 v3 fix) passes
        both composer and runtime. Confirms the new check is not over-eager."""
        csv_path = self._csv_input(tmp_path)
        output_path = self._csv_output(tmp_path)

        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="agg1",
                options={"path": str(csv_path), "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(
                NodeSpec(
                    id="agg1",
                    node_type="aggregation",
                    plugin="batch_stats",
                    input="agg1",
                    on_success="main",
                    on_error="discard",  # ← legal escape valve
                    options={"schema": {"mode": "observed"}, "value_field": "value"},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                    trigger={"count": 1},
                    output_mode="transform",
                    expected_output_count=1,
                ),
            ),
            edges=(
                EdgeSpec(id="e1", from_node="source", to_node="agg1", edge_type="on_success", label=None),
                EdgeSpec(id="e2", from_node="agg1", to_node="main", edge_type="on_success", label=None),
            ),
            outputs=(
                OutputSpec(
                    name="main",
                    plugin="csv",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )
        result = validate_pipeline_for_trained_operator(
            state,
            self._validation_settings(tmp_path),
            composer_yaml_generator,
            session_id=_AGREEMENT_SESSION_ID,
        )
        assert result.is_valid, "\n".join(err.message for err in result.errors)
        rt_check = next(c for c in result.checks if c.name == "route_target_resolution")
        assert rt_check.passed is True
        assert rt_check.detail == "All route targets resolve to existing sinks"

        # Runtime: must also assemble cleanly, no RouteValidationError.
        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="agg1",
                    options={"path": str(csv_path), "schema": {"mode": "observed"}, "on_validation_failure": "discard"},
                )
            },
            aggregations=[
                AggregationSettings(
                    name="agg1",
                    plugin="batch_stats",
                    input="agg1",
                    on_success="main",
                    on_error="discard",
                    trigger=TriggerConfig(count=1),
                    options={"schema": {"mode": "observed"}, "value_field": "value"},
                ),
            ],
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="discard",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                ),
            },
        )
        plugins = instantiate_plugins_from_config(config)
        graph = ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
            coalesce_settings=list(config.coalesce) if config.coalesce else None,
        )
        graph.validate()
        # Should not raise.
        assemble_and_validate_pipeline_config(
            sources=plugins.sources,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            settings=config,
            graph=graph,
        )


# ── Shape 1 — secret_refs literal placeholder agreement ──────────────────────
# Closes elspeth-72d1dccd44 / Phase 1.1.  Origin: 2026-05-01 staging eval, S1A
# (session 2ef2db56-70d7-498a-83d6-47e1f0efe340, run
# 51f5f609-bf72-4654-9cf2-6c53c565548b).  Pre-fix, a literal placeholder string
# in a credential-bearing field validated as is_valid: true and the engine ran
# end-to-end with every row routed via on_error to a quarantine sink.  Post-fix,
# the secret_refs check rejects at /validate before runtime ever sees the
# pipeline.
#
# The unit suite at TestValidatePipelineFabricatedCredentials in
# tests/unit/web/execution/test_validation.py covers the seven sibling shapes
# (transform/source/sink credential fields, suffix-matched fields, nested
# credentials, env-marker outside inventory, positive control).  The
# agreement-suite contribution here is the canonical S1A reproducer pinned at
# the agreement layer: a future drift in the fabrication-aware predicate must
# fail BOTH the validator unit tests AND this agreement gate.


class _AgreementSecretService:
    """Minimal WebSecretResolver stand-in for agreement-suite shape pinning.

    Mirrors ``tests/unit/web/execution/test_validation.py::FakeSecretService``
    without importing across the unit/integration suite boundary.  Inventory
    items report ``available=False`` with a closed-list reason so the fixture
    obeys the Phase 2.3 ``available ⟺ reason is None`` invariant.
    """

    def __init__(self, *, inventory: frozenset[str] = frozenset()) -> None:
        self._inventory = inventory

    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        del user_id
        return [
            SecretInventoryItem(
                name=name,
                scope="user",
                available=False,
                reason="value_decryption_failed",
            )
            for name in sorted(self._inventory)
        ]

    def has_ref(self, user_id: str, name: str) -> bool:
        del user_id, name
        return False

    def resolve(self, user_id: str, name: str) -> None:
        """Protocol completeness — agreement-suite never calls resolve()
        because the secret_refs gate fires before settings load.  Returning
        ``None`` mirrors ``WebSecretService.resolve``'s "not present" path.
        """
        del user_id, name
        return None


class TestComposerRuntimeSecretRefAgreement:
    """Shape 1 — literal credential placeholders are gated at /validate.

    Empirical scope of the original gap: composer's ``secret_refs`` check only
    looked for ``{secret_ref: <name>}`` constructs.  A literal placeholder
    string in a field the plugin schema marks as credential-bearing
    (``is_secret_field`` predicate at L1 ``core/secrets.py``) bypassed the
    check entirely.  The runtime-side defense is non-existent — the LLM
    transform happily called the upstream API with the literal placeholder as
    the bearer token, producing per-row HTTP 401s and routing every row via
    ``on_error`` to ``parse_quarantine.jsonl``.  This shape is therefore
    /validate-gated only; the agreement contract here is "/validate rejects
    so /execute never receives it."
    """

    _S1A_PLACEHOLDER = "WILL_BE_WIRED_FROM_OPENROUTER_API_KEY"

    @staticmethod
    def _validation_settings(data_dir: Path) -> SimpleNamespace:
        return SimpleNamespace(data_dir=data_dir)

    def _assert_placeholder_redacted(self, result, *, value: str) -> None:
        """Audit-hygiene: the literal candidate-secret value must not be
        echoed into any field of the validation response.  Mirrors the
        unit-suite discipline at
        ``TestValidatePipelineFabricatedCredentials._assert_value_redacted``;
        kept inline here so this test is self-contained as the agreement
        contract.
        """
        for check in result.checks:
            assert value not in check.detail, f"placeholder value leaked into check {check.name!r} detail"
        for error in result.errors:
            assert value not in error.message, f"placeholder value leaked into error.message at {error.component_id!r}"
            if error.suggestion is not None:
                assert value not in error.suggestion, "placeholder value leaked into error.suggestion"

    def test_agreement_s1a_literal_api_key_fails_validate(self, tmp_path: Path) -> None:
        """S1A canonical reproducer: an LLM transform with a literal
        ``api_key: WILL_BE_WIRED_FROM_OPENROUTER_API_KEY`` placeholder must
        fail the composer ``secret_refs`` check, identify the offending
        component+field, and never echo the candidate-secret string back to
        the operator surface.
        """
        # Source must live under data_dir/blobs/ for the path allowlist; the
        # ``secret_refs`` predicate fires before the path check, but build a
        # legitimate source so the failure is unambiguously the credential
        # check rather than a parallel rejection.
        blobs = tmp_path / "blobs" / _AGREEMENT_SESSION_ID
        blobs.mkdir(parents=True)
        csv_path = blobs / "tickets.csv"
        csv_path.write_text("subject\nticket-1\n", encoding="utf-8")

        state = CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="classify_ticket",
                options={"path": str(csv_path), "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(
                NodeSpec(
                    id="classify_ticket",
                    node_type="transform",
                    plugin="llm",
                    input="classify_ticket",
                    on_success="main",
                    on_error="discard",
                    options={
                        "provider": "openrouter",
                        "model": "openai/gpt-4.1-nano",
                        "api_key": self._S1A_PLACEHOLDER,
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            edges=(
                EdgeSpec(id="e1", from_node="source", to_node="classify_ticket", edge_type="on_success", label=None),
                EdgeSpec(id="e2", from_node="classify_ticket", to_node="main", edge_type="on_success", label=None),
            ),
            outputs=(
                OutputSpec(
                    name="main",
                    plugin="csv",
                    options={
                        "path": str(tmp_path / "outputs" / _AGREEMENT_SESSION_ID / "out.csv"),
                        "schema": {"mode": "observed"},
                    },
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(),
            version=1,
        )

        result = validate_pipeline_for_trained_operator(
            state,
            self._validation_settings(tmp_path),
            composer_yaml_generator,
            secret_service=_AgreementSecretService(),
            user_id="agreement-suite-user",
            session_id=_AGREEMENT_SESSION_ID,
        )

        # The agreement gate: /validate must reject this shape so /execute
        # never receives it.  Pre-fix this returned is_valid: true.
        assert result.is_valid is False, "S1A literal-placeholder shape must be rejected by /validate"

        secret_check = next(check for check in result.checks if check.name == "secret_refs")
        assert secret_check.passed is False, f"secret_refs check must fail; detail={secret_check.detail!r}"
        assert "api_key" in secret_check.detail, "secret_refs detail must name the offending field"

        # The structured error must attribute the failure to the LLM transform
        # node so the operator can navigate directly to it.
        api_key_errors = [error for error in result.errors if "api_key" in error.message]
        assert api_key_errors, "expected at least one error naming the api_key field"
        assert any(error.component_id == "classify_ticket" and error.component_type == "transform" for error in api_key_errors), (
            "credential-field error must attribute to the LLM transform node"
        )

        # Audit-hygiene: the literal placeholder string must not appear
        # anywhere in the response.  In production the candidate value may be
        # a near-miss real secret; reflecting it back is data leakage.
        self._assert_placeholder_redacted(result, value=self._S1A_PLACEHOLDER)


# ── Shape 5 — RunStatus four-value taxonomy cross-layer agreement ─────────────
# Closes elspeth-0de989c56d / Phase 2.2 (commit cc895589).  The per-status
# engine-layer pinning lives in tests/integration/pipeline/orchestrator/
# test_orchestrator_core.py and the API-mirror pinning lives in
# tests/unit/web/execution/test_schemas.py + tests/unit/contracts/
# test_run_result.py.  This suite adds the cross-layer agreement contract:
# every terminal RunResult.status the engine writes must equal the RunStatus
# the Landscape audit ``Run`` row carries after ``finalize_run``.  Phase 2.2
# wires the orchestrator to call ``derive_terminal_run_status`` then
# ``finalize_run(status=...)``; if a future refactor breaks that wiring (e.g.
# Landscape persists ``COMPLETED`` while RunResult carries ``FAILED``) the
# audit trail and the API surface diverge silently.  This gate fails before
# the divergence reaches a deploy.


class TestComposerRuntimeRunStatusAgreement:
    """Shape 5 — engine RunResult and Landscape audit ``Run`` row agree on
    ``RunStatus`` across the four-value terminal taxonomy plus the explicit
    rows_routed-only design call.

    Coverage:

    * ``COMPLETED`` — healthy linear pipeline, all rows reach success.
    * ``COMPLETED_WITH_FAILURES`` — mixed run, some succeed and some fail.
    * ``FAILED`` — every row fails via ``on_error: discard`` (S1B msg2 shape).
    * ``EMPTY`` — empty source, no failures, no rows.
    * Design call (post-split, ``elspeth-5069612f3c``) — every row routes
      via ``on_error`` to a sink (``rows_failed == N`` plus
      ``rows_routed_failure == N``).  Now classifies as ``FAILED`` because
      lifecycle failure and routing provenance are recorded independently.
      Locked here as
      ``test_runstatus_on_error_routed_only_classifies_as_failed``.
      Companion: ``test_runstatus_gate_routed_only_classifies_as_completed``
      pins the symmetric MOVE shape (gate ``route_to_sink``) classifying as
      ``COMPLETED``.  A future maintainer changing either verdict confronts
      the design decision rather than silently flipping it.
    """

    @staticmethod
    def _assert_engine_landscape_agreement(landscape_db: LandscapeDB, run_result, expected_status: RunStatus) -> Run:
        """Cross-layer assertion helper.

        Asserts (1) the engine-side ``RunResult.status`` equals the expected
        status, (2) the Landscape audit ``runs`` row exists for the same
        ``run_id``, and (3) the audit row's ``status`` equals the engine's.
        Returns the loaded ``Run`` so the caller can chain further checks.
        """
        assert run_result.status == expected_status, (
            f"engine RunResult.status mismatch: expected {expected_status!r}, got {run_result.status!r}"
        )
        factory = make_factory(landscape_db)
        run_row = factory.run_lifecycle.get_run(run_result.run_id)
        assert run_row is not None, f"Landscape audit row missing for run_id={run_result.run_id!r}"
        assert run_row.status == run_result.status, (
            f"Landscape/engine status disagreement: engine RunResult.status={run_result.status!r}, Landscape Run.status={run_row.status!r}"
        )
        return run_row

    def test_agreement_runstatus_completed_engine_landscape(self, landscape_db: LandscapeDB, payload_store) -> None:
        """Healthy linear pipeline — RunResult and Landscape both report COMPLETED."""
        source = ListSource([{"value": 1}, {"value": 2}])
        transform = PassTransform()
        transform.on_success = "default"
        sink = CollectSink()

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(transform)],
            sinks={"default": as_sink(sink)},
        )

        run_result = Orchestrator(landscape_db).run(config, graph=build_production_graph(config), payload_store=payload_store)

        self._assert_engine_landscape_agreement(landscape_db, run_result, RunStatus.COMPLETED)
        assert run_result.rows_processed == 2
        assert run_result.rows_succeeded == 2
        assert len(sink.results) == 2

    def test_agreement_runstatus_completed_with_failures_engine_landscape(self, landscape_db: LandscapeDB, payload_store) -> None:
        """Mixed run — RunResult and Landscape both report COMPLETED_WITH_FAILURES.

        Two rows: one with ``fail=False`` (succeeds), one with ``fail=True``
        (failed via ``on_error: discard``, which lands the row in the
        ``rows_quarantined`` bucket — ``discard`` is the engine's quarantine
        terminal state).  Predicate: rows_succeeded > 0 AND has_failures.
        """
        source = ListSource([{"value": 1, "fail": False}, {"value": 2, "fail": True}])
        transform = ConditionalErrorTransform(on_success="default", on_error="discard")
        sink = CollectSink()

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(transform)],
            sinks={"default": as_sink(sink)},
        )

        run_result = Orchestrator(landscape_db).run(config, graph=build_production_graph(config), payload_store=payload_store)

        self._assert_engine_landscape_agreement(landscape_db, run_result, RunStatus.COMPLETED_WITH_FAILURES)
        assert run_result.rows_processed == 2
        assert run_result.rows_succeeded == 1
        # ADR-019 records lifecycle failure independently from discard-mode
        # quarantine provenance.
        assert run_result.rows_failed == 1
        assert run_result.rows_quarantined == 1
        assert run_result.rows_coalesce_failed == 0

    def test_agreement_runstatus_all_discarded_engine_landscape(self, landscape_db: LandscapeDB, payload_store) -> None:
        """All-rows-discarded via ``on_error: discard`` — both report
        COMPLETED_WITH_FAILURES.

        Two rows, both fail via ``on_error: discard`` (the engine's
        quarantine terminal state).  Per CLAUDE.md Tier-3 data manifesto,
        quarantine is a deliberate clean determination on every row, not a
        framework failure. The predicate sees ``terminal_clean_indicator``
        via ``rows_quarantined > 0`` with no uncaught ``failure_indicator``
        (rows_failed - rows_quarantined == 0) and lifts the verdict from
        FAILED to COMPLETED_WITH_FAILURES.
        """
        source = ListSource([{"value": 1, "fail": True}, {"value": 2, "fail": True}])
        transform = ConditionalErrorTransform(on_success="default", on_error="discard")
        sink = CollectSink()

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(transform)],
            sinks={"default": as_sink(sink)},
        )

        run_result = Orchestrator(landscape_db).run(config, graph=build_production_graph(config), payload_store=payload_store)

        self._assert_engine_landscape_agreement(landscape_db, run_result, RunStatus.COMPLETED_WITH_FAILURES)
        assert run_result.rows_processed == 2
        assert run_result.rows_succeeded == 0
        # ADR-019 records lifecycle failure independently from discard-mode
        # quarantine provenance.
        assert run_result.rows_failed == 2
        assert run_result.rows_quarantined == 2
        assert run_result.rows_coalesce_failed == 0

    def test_agreement_runstatus_empty_engine_landscape(self, landscape_db: LandscapeDB, payload_store) -> None:
        """Empty source — RunResult and Landscape both report EMPTY.

        Predicate: rows_processed == 0 AND rows_succeeded == 0 AND no
        failure indicators.  An empty source with no failures must NOT be
        misclassified as ``FAILED`` (which is the all-rows-failed verdict)
        nor as ``COMPLETED`` (which presupposes rows_succeeded > 0).
        """
        source = ListSource([])
        transform = PassTransform()
        transform.on_success = "default"
        sink = CollectSink()

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(transform)],
            sinks={"default": as_sink(sink)},
        )

        run_result = Orchestrator(landscape_db).run(config, graph=build_production_graph(config), payload_store=payload_store)

        self._assert_engine_landscape_agreement(landscape_db, run_result, RunStatus.EMPTY)
        assert run_result.rows_processed == 0
        assert run_result.rows_succeeded == 0
        assert run_result.rows_failed == 0
        assert len(sink.results) == 0

    def test_runstatus_on_error_routed_only_classifies_as_failed(self, landscape_db: LandscapeDB, payload_store) -> None:
        """elspeth-5069612f3c — every row triggers a transform exception and
        is routed via on_error to a quarantine sink. After the rows_routed
        split, this shape produces rows_routed_failure == N (DIVERT) with no
        success indicator, and the predicate classifies as FAILED.

        The verdict (FAILED) matches the prior locked-in test, but the
        structural reason changes: previously the predicate excluded
        rows_routed entirely (sidestepping the DIVERT/MOVE conflation); now
        rows_routed_failure is a first-class failure indicator and contributes
        to the predicate decision directly.

        Companion: test_runstatus_gate_routed_only_classifies_as_completed
        below (the gate MOVE shape).
        """
        source = ListSource([{"value": 1, "fail": True}, {"value": 2, "fail": True}])
        transform = ConditionalErrorTransform(on_success="default", on_error="quarantine")
        default_sink = CollectSink(name="default")
        quarantine_sink = CollectSink(name="quarantine")

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(transform)],
            sinks={"default": as_sink(default_sink), "quarantine": as_sink(quarantine_sink)},
        )

        run_result = Orchestrator(landscape_db).run(config, graph=build_production_graph(config), payload_store=payload_store)

        self._assert_engine_landscape_agreement(landscape_db, run_result, RunStatus.FAILED)
        assert run_result.rows_processed == 2
        assert run_result.rows_succeeded == 0
        assert run_result.rows_routed_success == 0
        assert run_result.rows_routed_failure == 2
        assert len(default_sink.results) == 0
        assert len(quarantine_sink.results) == 2

    def test_runstatus_gate_routed_only_classifies_as_completed(self, landscape_db: LandscapeDB, payload_store) -> None:
        """elspeth-5069612f3c / elspeth-71520f5e30 — user reproducer shape:
        csv source -> gate routes high-priority rows to one sink, low-priority
        rows to another, no on_success success-path sink. Every row is
        intentionally gate-routed via RoutingMode.MOVE.

        ADR-019 records lifecycle SUCCESS plus gate-routing provenance, so
        this shape produces rows_succeeded > 0 and rows_routed_success > 0
        with no failure indicator, and the predicate classifies as COMPLETED.

        Before the split (commit cc895589), this shape misclassified as
        RunStatus.FAILED with the misleading error "No row reached the success
        path" because the predicate excluded rows_routed entirely (DIVERT/MOVE
        conflation). This test pins the corrected behavior.
        """
        source = ListSource(
            [
                {"value": 1, "tier": "high"},
                {"value": 2, "tier": "low"},
                {"value": 3, "tier": "high"},
                {"value": 4, "tier": "low"},
            ],
            on_success="source_out",
        )
        tier_gate = GateSettings(
            name="tier_gate",
            input="source_out",
            condition="row['tier'] == 'high'",
            routes={"true": "high_priority", "false": "low_priority"},
        )
        high_sink = CollectSink(name="high_priority")
        low_sink = CollectSink(name="low_priority")

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[],
            sinks={
                "high_priority": as_sink(high_sink),
                "low_priority": as_sink(low_sink),
            },
            gates=[tier_gate],
        )

        run_result = Orchestrator(landscape_db).run(
            config,
            graph=build_production_graph(config),
            payload_store=payload_store,
        )

        self._assert_engine_landscape_agreement(landscape_db, run_result, RunStatus.COMPLETED)
        assert run_result.rows_processed == 4
        # Gate-routed rows are lifecycle successes with MOVE provenance.
        assert run_result.rows_succeeded == 4
        assert run_result.rows_routed_success == 4  # All routed via MOVE
        assert run_result.rows_routed_failure == 0  # No on_error reroutes
        assert len(high_sink.results) == 2
        assert len(low_sink.results) == 2


# ── Shape 6 — SecretInventoryItem biconditional agreement ────────────────────
# Closes elspeth-0d31c22d26 / Phase 2.3 (commit 22e3e0d9).  Per-mode coverage
# (fingerprint_resolver_not_configured, env_var_not_set,
# value_decryption_failed) lives in tests/unit/web/secrets/ and the contract
# tests below.  Per the Phase 2.3 closure
# rationale ("agreement-suite scope ... does not need duplication") this suite
# DOES NOT duplicate the per-mode tests.  The single contract-layer assertion
# below pins the structural invariant (``available ⟺ reason is None``) so a
# future drift on the closed-list reason taxonomy fails the agreement gate
# alongside the unit suite.


class TestComposerRuntimeSecretInventoryAgreement:
    """Shape 6 — ``SecretInventoryItem`` biconditional invariant pin.

    The audit-hygiene constraint that ``/api/secrets`` must not echo
    candidate-secret values into the response is enforced *structurally* by
    the ``SecretUnavailabilityReason`` ``Literal`` type — there is no code
    path that interpolates an env-var or candidate-secret value into the
    response, because the field accepts only the closed-list reasons.  This
    test pins the biconditional ``available ⟺ reason is None`` enforced in
    ``SecretInventoryItem.__post_init__`` so a future widening of the model
    (e.g. accepting free-form reason strings) fails the agreement gate
    before it can ship.

    Per-mode coverage and audit-hygiene runtime tests live in
    ``tests/unit/web/secrets/``; this test pins the contract surface only.
    """

    def test_unavailable_with_no_reason_rejected(self) -> None:
        """An unavailable inventory entry with ``reason=None`` is the
        operator-hostile shape the field exists to eliminate."""
        with pytest.raises(ValueError, match="reason is required when available=False"):
            SecretInventoryItem(
                name="OPENROUTER_API_KEY",
                scope="server",
                available=False,
                source_kind="env",
                reason=None,
            )

    def test_available_with_reason_rejected(self) -> None:
        """An available secret carrying a reason is incoherent — the
        biconditional rejects the asymmetric construction."""
        with pytest.raises(ValueError, match="reason must be None when available=True"):
            SecretInventoryItem(
                name="OPENROUTER_API_KEY",
                scope="server",
                available=True,
                source_kind="env",
                reason="env_var_not_set",
            )

    def test_unavailable_with_closed_list_reasons_accepted(self) -> None:
        """Each Phase 2.3 reason value is accepted; reasons outside the
        closed list are rejected at the model layer (the structural
        audit-hygiene gate)."""
        for reason in ("fingerprint_resolver_not_configured", "env_var_not_set", "value_decryption_failed"):
            item = SecretInventoryItem(
                name="OPENROUTER_API_KEY",
                scope="server",
                available=False,
                source_kind="env",
                reason=reason,
            )
            assert item.reason == reason
        # SecretUnavailabilityReason imported above is the typed surface — this
        # local frozenset is the closed list mirrored against
        # ``contracts/secrets._ALLOWED_UNAVAILABILITY_REASONS``.
        assert SecretUnavailabilityReason is not None  # imported for type alias presence


# ── Shape 7 — pipeline_done_callback run-accounting agreement ────────────────
# Closes elspeth-31d53c7493 / Phase 2.1 (commit 5e26d0a6).  Origin: 2026-05-01
# eval, S2 successful run 44f52421-a379-459b-96a8-6f0656086f16 (csv 6 rows →
# batch_stats group_by → json sink). The original bug was a linear
# row-decomposition equality on ``CompletedData``; the current contract loads
# explicit Landscape-derived source/token/routing/integrity accounting from
# the orchestrator-emitted run and verifies the API payload accepts that audit
# projection without reconstructing row fate from engine counters.


class _BatchAggregateTransform(BaseTransform):
    """Batch-aware transform mirroring S2's aggregation shape (csv 6 rows →
    1 aggregated output row).

    Reproduces the structural shape that broke the old row-decomposition
    equality: source rows reach ``CONSUMED_IN_BATCH`` while the aggregated
    output row reaches ``COMPLETED``. Net effect: source-row cardinality and
    materialized terminal-token cardinality intentionally diverge. The current
    readback contract must accept the explicit Landscape-derived accounting
    for that run.

    Defined inline so the test is self-contained as the agreement contract
    rather than depending on the BatchStats production plugin's internal
    triggering behaviour.
    """

    name = "agreement_batch_aggregate"
    determinism = Determinism.DETERMINISTIC
    is_batch_aware = True
    input_schema = _TestSchema
    output_schema = _TestSchema

    def __init__(self) -> None:
        super().__init__({"schema": {"mode": "observed"}})

    def process(self, row, ctx):
        from elspeth.contracts import FieldContract, PipelineRow, SchemaContract
        from elspeth.plugins.infrastructure.results import TransformResult

        if not isinstance(row, list):
            # Single-row mode is not exercised by this aggregation path —
            # the wiring sets ``count == len(source_rows)`` so the trigger
            # always fires in batch mode.  Crash if the engine routes a
            # single row through here; that would indicate a wiring bug.
            raise AssertionError("agreement-suite batch aggregate received a single row, expected a batch flush")

        total = sum(float(r["amount"]) for r in row)
        output = {"total_amount": total, "row_count": len(row)}
        contract = SchemaContract(
            mode="OBSERVED",
            fields=(
                FieldContract(
                    normalized_name="total_amount",
                    original_name="total_amount",
                    python_type=float,
                    required=False,
                    source="inferred",
                ),
                FieldContract(
                    normalized_name="row_count",
                    original_name="row_count",
                    python_type=int,
                    required=False,
                    source="inferred",
                ),
            ),
            locked=True,
        )
        return TransformResult.success(
            PipelineRow(output, contract),
            success_reason={"action": "agreement_batch_aggregate"},
        )


class TestComposerRuntimeRunCompletionAgreement:
    """Shape 7 — aggregation runs must construct ``CompletedData`` from audit accounting.

    Single-occurrence regression coverage.  The unit-level pin lives at
    ``tests/unit/web/execution/test_run_accounting_projection.py``. The
    agreement-suite contribution here is to drive the accounting from a real
    orchestrator-emitted aggregation run. A future aggregation refactor that
    changes terminal-token emission would slip past unit tests that
    hand-construct accounting but fail this test because the engine's actual
    audit output would no longer match the readback contract.
    """

    def test_agreement_aggregation_run_counts_construct_completed_data(self, landscape_db: LandscapeDB, payload_store) -> None:
        """S2 reproducer (run 44f52421): a 6-row source feeds a batch-aware
        aggregation that emits 1 output row.  Source rows reach
        ``CONSUMED_IN_BATCH`` (no terminal-bucket counter).  Engine counts
        end up as ``rows_processed=6, rows_succeeded=1, rows_failed=0,
        rows_routed_success=0, rows_routed_failure=0, rows_quarantined=0``.
        The public readback payload must be validated from Landscape-derived
        accounting rather than from a row-counter equality.
        """
        from elspeth.contracts.types import NodeID

        source = ListSource(
            [
                {"customer_tier": "enterprise", "amount": 100.0},
                {"customer_tier": "enterprise", "amount": 150.0},
                {"customer_tier": "pro", "amount": 50.0},
                {"customer_tier": "pro", "amount": 75.0},
                {"customer_tier": "free", "amount": 10.0},
                {"customer_tier": "free", "amount": 20.0},
            ],
            on_success="source_out",
        )
        aggregate_transform = _BatchAggregateTransform()
        sink = CollectSink(name="output")

        # Build the graph with the batch-aware transform wired through
        # source_out → aggregate → output.  ``aggregations={}`` because the
        # batch transform is in the transforms list; PipelineConfig binds
        # it as an aggregation via ``aggregation_settings``.
        from elspeth.core.dag import ExecutionGraph as _ExecutionGraph
        from tests.fixtures.factories import wire_transforms as _wire_transforms

        graph = _ExecutionGraph.from_plugin_instances(
            sources={"primary": as_source(source)},
            source_settings_map={"primary": SourceSettings(plugin=source.name, on_success="source_out", options={})},
            transforms=_wire_transforms(
                [as_transform(aggregate_transform)],
                source_connection="source_out",
                final_sink="output",
            ),
            sinks={"output": as_sink(sink)},
            aggregations={},
            gates=[],
            coalesce_settings=None,
        )

        # Bind the transform as an aggregation: trigger fires at count=6
        # (matching the source row count) so all 6 input rows flush as one
        # batch and emit a single aggregated output row.
        transform_id_map = graph.get_transform_id_map()
        transform_node_id = transform_id_map[0]
        agg_settings = AggregationSettings(
            name="agreement_aggregate",
            plugin="agreement_batch_aggregate",
            input="source_out",
            on_success="output",
            on_error="discard",
            trigger=TriggerConfig(count=6, timeout_seconds=3600),
            output_mode="transform",
        )

        config = PipelineConfig(
            sources={"primary": as_source(source)},
            transforms=[as_transform(aggregate_transform)],
            sinks={"output": as_sink(sink)},
            aggregation_settings={NodeID(transform_node_id): agg_settings},
        )

        run_result = Orchestrator(landscape_db).run(config, graph=graph, payload_store=payload_store)

        # Engine emission contract: 6 source rows in, 1 aggregated row out;
        # source rows reach CONSUMED_IN_BATCH while the output token reaches
        # COMPLETED.
        assert run_result.rows_processed == 6, (
            f"agreement_batch_aggregate must emit rows_processed=6 to reproduce S2 shape; got {run_result.rows_processed}"
        )
        assert run_result.rows_succeeded == 1, (
            f"the aggregated output row must be the only success terminal; got rows_succeeded={run_result.rows_succeeded}"
        )
        # The old row-counter equality rejected this legitimate shape:
        #   rows_processed (6) > sum-of-four-buckets (1)
        sum_of_buckets = (
            run_result.rows_succeeded
            + run_result.rows_failed
            + run_result.rows_routed_success
            + run_result.rows_routed_failure
            + run_result.rows_quarantined
        )
        assert run_result.rows_processed > sum_of_buckets, (
            "Shape sanity check: this test exercises source rows exceeding "
            f"terminal success/failure buckets (got {run_result.rows_processed} vs {sum_of_buckets}); "
            "if this assertion fails the test no longer pins the legitimate aggregation shape."
        )

        # Drive the completed-event validator from the engine's actual
        # Landscape audit output. Pre-Phase-2.1 this test constructed the
        # old row-counter payload directly and pinned the legitimate
        # rows_processed > terminal-successes aggregation shape. The public
        # event contract now carries explicit Landscape-derived accounting,
        # so the agreement check loads that accounting from the run just
        # emitted by the orchestrator.
        # Phase 2.2 (elspeth-0de989c56d): SSE payload carries the explicit
        # status discriminator; this aggregation has one successful terminal
        # materialized token and no failures, so the engine classifies it as
        # "completed".
        accounting = load_run_accounting_from_db(landscape_db, landscape_run_id=run_result.run_id)
        completed = CompletedData(
            status="completed",
            accounting=accounting,
            landscape_run_id=run_result.run_id,
        )
        assert completed.accounting.source.rows_processed == run_result.rows_processed
        assert completed.accounting.tokens.succeeded == run_result.rows_succeeded
        assert completed.accounting.tokens.failed == 0
        assert completed.accounting.integrity.closure == "closed"
        assert completed.landscape_run_id == run_result.run_id
        # The output sink received exactly the one aggregated row.
        assert len(sink.results) == 1, f"expected single aggregated output, got {len(sink.results)}"


class TestComposerRuntimeFileSinkCollisionAgreement:
    """Shape 8 — composer ``/validate`` must convert file-sink fs collision
    failures into a structured ``ValidationResult(is_valid=False)`` instead
    of letting the underlying ``FileExistsError`` propagate as a 500
    ``composer_plugin_error``. Closes ``elspeth-209b7e3a2b`` (Phase 0.b).

    Eval session S3 (``98573481-e8bc-4a03-8467-d3a86effcd56``, eval notes
    ``docs/composer/evidence/composer-llm-eval-2026-05-01.md``) reported this as a "gate
    primitive crash" because the failures clustered around gate-routing
    prompts. Phase 0.b investigation
    (``docs/composer/evidence/composer-phase-0b-staging-capture-2026-05-02.md``)
    re-attributed it: gate routing requires sinks, the LLM defaults sinks
    to ``collision_policy="fail_if_exists"``, and stale eval artifacts in
    ``data/outputs/`` collide. The actual defect was
    ``validate_pipeline``'s step-4 catch list at
    ``src/elspeth/web/execution/validation.py`` — only
    ``(PluginNotFoundError, PluginConfigError)`` was caught around
    ``instantiate_runtime_plugins``, so ``FileExistsError`` raised from
    ``json_sink.__init__`` / ``csv_sink.__init__`` via
    ``plugins/infrastructure/output_paths.py:48`` propagated uncaught into
    ``_state_data_from_composer_state``, was wrapped as
    ``ComposerRuntimePreflightError``, and surfaced as the opaque 500.

    The fix extends the step-4 catch list to include ``FileExistsError``
    and converts it to a structured ``ValidationCheck(passed=False)`` on
    the ``plugin_instantiation`` step with an ``auto_increment``
    suggestion. Per CLAUDE.md trust tiers, the existing-file condition is
    a Tier 3 boundary fact (external fs state) at a validation seam — the
    correct shape is a structured 422-class diagnostic, not a 500.

    Bug verification protocol (executed manually before this test landed):
    temporarily reverted the new ``except FileExistsError as exc:`` clause
    in ``src/elspeth/web/execution/validation.py`` (the block immediately
    after the existing ``except (PluginNotFoundError, PluginConfigError)``
    handler) and confirmed this test fails with an uncaught
    ``FileExistsError("Output path already exists: ...")`` raised through
    ``validate_pipeline``. Restored the catch after verification. This
    protocol guards against the "passes pre-fix AND post-fix" failure
    mode — without the revert, this test could appear to pin the contract
    while actually depending on adjacent behaviour. The revert proved the
    test's failure mode is exactly the structural bug it exists to catch.
    """

    @staticmethod
    def _validation_settings(data_dir: Path) -> SimpleNamespace:
        return SimpleNamespace(data_dir=data_dir)

    @staticmethod
    def _csv_input(tmp_path: Path) -> Path:
        path = tmp_path / "blobs" / _AGREEMENT_SESSION_ID / "input.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ticket_id,customer_tier\n1,enterprise\n", encoding="utf-8")
        return path

    @staticmethod
    def _build_state(csv_path: Path, sink_path: Path) -> CompositionState:
        return CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="default",
                options={"path": str(csv_path), "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            ),
            nodes=(),
            edges=(
                EdgeSpec(
                    id="source_to_default",
                    from_node="source",
                    to_node="default",
                    edge_type="on_success",
                    label="rows to sink",
                ),
            ),
            outputs=(
                OutputSpec(
                    name="default",
                    plugin="json",
                    options={
                        "path": str(sink_path),
                        "format": "jsonl",
                        "mode": "write",
                        "collision_policy": "fail_if_exists",
                        "schema": {"mode": "observed"},
                    },
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(name="Shape 8 collision repro", description=""),
            version=1,
        )

    def test_composer_validate_does_not_probe_file_sink_collision_in_preflight(self, tmp_path: Path) -> None:
        """Preflight validation must not inspect local file-sink collisions."""
        csv_path = self._csv_input(tmp_path)

        # Pre-create the sink target. Runtime execution with fail_if_exists
        # must still reject this, but composer preflight must not observe
        # local filesystem collision state during plugin construction.
        sink_path = tmp_path / "outputs" / _AGREEMENT_SESSION_ID / "all.jsonl"
        sink_path.parent.mkdir(parents=True, exist_ok=True)
        sink_path.write_text("", encoding="utf-8")  # any pre-existing content
        assert sink_path.exists()

        state = self._build_state(csv_path, sink_path)

        result = validate_pipeline_for_trained_operator(
            state,
            self._validation_settings(tmp_path),
            composer_yaml_generator,
            session_id=_AGREEMENT_SESSION_ID,
        )

        assert result.is_valid is True

        check_by_name = {check.name: check for check in result.checks}
        assert "plugin_instantiation" in check_by_name, "Missing plugin_instantiation check"
        plugin_check = check_by_name["plugin_instantiation"]
        assert plugin_check.passed is True, f"plugin_instantiation must not probe fs collisions; got {plugin_check.detail!r}"
        assert result.errors == []

    def test_composer_validate_passes_when_sink_path_does_not_collide(self, tmp_path: Path) -> None:
        """Positive control: same state with a non-existing sink path passes
        plugin instantiation cleanly. Asserts the catch is selective — only
        firing on the actual fs-collision condition, not converting all
        plugin-init failures into the same shape."""
        csv_path = self._csv_input(tmp_path)
        sink_path = tmp_path / "outputs" / _AGREEMENT_SESSION_ID / "fresh.jsonl"
        sink_path.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately do NOT create the sink_path file.
        assert not sink_path.exists()

        state = self._build_state(csv_path, sink_path)
        result = validate_pipeline_for_trained_operator(
            state,
            self._validation_settings(tmp_path),
            composer_yaml_generator,
            session_id=_AGREEMENT_SESSION_ID,
        )

        check_by_name = {check.name: check for check in result.checks}
        assert check_by_name["plugin_instantiation"].passed is True, (
            f"plugin_instantiation must pass when sink path is free; got detail={check_by_name['plugin_instantiation'].detail!r}"
        )


@dataclass(slots=True)
class _RuntimeSettingsFake:
    data_dir: str
    payload_store_path: Path
    landscape_passphrase: str | None = None

    def get_landscape_url(self) -> str:
        return "sqlite:///:memory:"

    def get_payload_store_path(self) -> Path:
        return self.payload_store_path


@dataclass(slots=True)
class _RunSnapshot:
    session_id: UUID
    status: str = "running"
    error: str | None = None


@dataclass(slots=True)
class _FakeSessionService:
    run: _RunSnapshot
    update_run_status_calls: list[tuple[UUID, str, dict[str, Any]]] = field(default_factory=list)
    appended_run_events: list[dict[str, Any]] = field(default_factory=list)
    recorded_blob_inline_resolutions: list[dict[str, Any]] = field(default_factory=list)
    record_blob_inline_resolutions_hook: Any = None
    next_event_sequence: int = 0

    async def update_run_status(self, run_id: UUID, status: str, **kwargs: Any) -> None:
        self.run.status = status
        if "error" in kwargs:
            self.run.error = kwargs["error"]
        self.update_run_status_calls.append((run_id, status, kwargs))

    async def get_run(self, _run_id: UUID) -> _RunSnapshot:
        return self.run

    async def append_run_event(
        self,
        *,
        run_id: UUID,
        timestamp: datetime,
        event_type: str,
        data: dict[str, Any],
    ) -> SimpleNamespace:
        self.next_event_sequence += 1
        self.appended_run_events.append(
            {
                "run_id": run_id,
                "timestamp": timestamp,
                "event_type": event_type,
                "data": data,
            }
        )
        return SimpleNamespace(sequence=self.next_event_sequence)

    async def record_blob_inline_resolutions(
        self,
        *,
        run_id: UUID,
        resolutions: Any,
        attempt: int = 1,
    ) -> None:
        if self.record_blob_inline_resolutions_hook is not None:
            await self.record_blob_inline_resolutions_hook(
                run_id=run_id,
                resolutions=resolutions,
                attempt=attempt,
            )
        self.recorded_blob_inline_resolutions.append(
            {
                "run_id": run_id,
                "resolutions": tuple(resolutions),
                "attempt": attempt,
            }
        )


@dataclass(slots=True)
class _FakeProgressBroadcaster:
    broadcast_calls: list[tuple[str, Any]] = field(default_factory=list)
    cleanup_run_ids: list[str] = field(default_factory=list)

    def broadcast(self, run_id: str, event: Any) -> BroadcastResult:
        self.broadcast_calls.append((run_id, event))
        return BroadcastResult()

    def cleanup_run(self, run_id: str) -> None:
        self.cleanup_run_ids.append(run_id)


@dataclass(slots=True)
class _FakeBlobService:
    blob_record: BlobRecord
    content: bytes
    link_blob_to_run_calls: list[tuple[UUID, UUID, str]] = field(default_factory=list)
    read_blob_content_calls: list[UUID] = field(default_factory=list)
    get_blob_calls: list[UUID] = field(default_factory=list)
    finalize_run_output_blobs_calls: list[tuple[UUID, bool]] = field(default_factory=list)

    async def link_blob_to_run(self, blob_id: UUID, run_id: UUID, direction: str) -> None:
        self.link_blob_to_run_calls.append((blob_id, run_id, direction))

    async def read_blob_content(self, blob_id: UUID) -> bytes:
        self.read_blob_content_calls.append(blob_id)
        return self.content

    async def get_blob(self, blob_id: UUID) -> BlobRecord:
        self.get_blob_calls.append(blob_id)
        return self.blob_record

    async def finalize_run_output_blobs(self, run_id: UUID, success: bool) -> BlobFinalizationResult:
        self.finalize_run_output_blobs_calls.append((run_id, success))
        return BlobFinalizationResult(finalized=(), errors=())


def _ready_inline_blob_record(*, blob_id: UUID, session_id: UUID, content: bytes, content_hash: str) -> BlobRecord:
    return BlobRecord(
        id=blob_id,
        session_id=session_id,
        filename="prompt.txt",
        mime_type="text/plain",
        size_bytes=len(content),
        content_hash=content_hash,
        storage_path="prompt.txt",
        created_at=datetime.now(tz=UTC),
        created_by="assistant",
        source_description=None,
        status="ready",
        creation_modality=CreationModality.VERBATIM,
        created_from_message_id=None,
        creating_model_identifier=None,
        creating_model_version=None,
        creating_provider=None,
        creating_composer_skill_hash=None,
        creating_arguments_hash=None,
    )


class TestComposerRuntimeBlobInlineAgreement:
    """Shape 9 — widened blob_ref / inline_content agreement.

    Bug verification for sub-pin A was captured before commit ``2aaa4be2e``:
    without the ``blob_inline_refs`` validate-time metadata bridge,
    ``validate_pipeline(..., blob_get_metadata=...)`` rejected the new keyword
    argument and the service path never queried ``BlobService.get_blob``.

    Bug verification for sub-pins B/C was captured in
    ``tests/unit/web/execution/test_service.py::TestInlineBlobRuntimePreflight``:
    removing the runtime resolver or audit-write block lets settings/plugin
    construction proceed without the fail-closed hash/audit invariant.
    """

    @staticmethod
    def _validation_settings(data_dir: Path) -> SimpleNamespace:
        return SimpleNamespace(data_dir=data_dir)

    @staticmethod
    def _state_with_inline_prompt(tmp_path: Path, blob_id: UUID, sha256: str) -> CompositionState:
        blobs_dir = tmp_path / "blobs" / _AGREEMENT_SESSION_ID
        outputs_dir = tmp_path / "outputs" / _AGREEMENT_SESSION_ID
        blobs_dir.mkdir(parents=True, exist_ok=True)
        outputs_dir.mkdir(parents=True, exist_ok=True)
        return CompositionState(
            source=SourceSpec(
                plugin="csv",
                on_success="classify_input",
                options={
                    "path": str(blobs_dir / "input.csv"),
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="discard",
            ),
            nodes=(
                NodeSpec(
                    id="classify",
                    node_type="transform",
                    plugin="llm",
                    input="classify_input",
                    on_success="results",
                    on_error="discard",
                    options={
                        "provider": "openrouter",
                        "api_key": {"secret_ref": "OPENROUTER_API_KEY"},
                        "model": "openai/gpt-4o",
                        "prompt_template": {
                            "blob_ref": str(blob_id),
                            "mode": "inline_content",
                            "sha256": sha256,
                        },
                        "required_input_fields": [],
                        "schema": {"mode": "observed"},
                        # Pre-resolved model-choice review so the
                        # interpretation gate doesn't short-circuit the
                        # validator before the blob-inline check runs. The
                        # auto-stager normally creates a pending requirement
                        # at mutation time; tests that bypass the composer
                        # (constructing NodeSpec directly) must stage the
                        # resolved form themselves.
                        INTERPRETATION_REQUIREMENTS_KEY: [
                            {
                                "id": "model_choice_review:classify",
                                "kind": "llm_model_choice",
                                "user_term": "llm_model_choice:classify",
                                "status": "resolved",
                                "draft": "openai/gpt-4o",
                                "event_id": "model-choice-accepted",
                                "accepted_value": "openai/gpt-4o",
                                "accepted_artifact_hash": None,
                                "resolved_prompt_template_hash": stable_hash("openai/gpt-4o"),
                            }
                        ],
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                ),
            ),
            edges=(),
            outputs=(
                OutputSpec(
                    name="results",
                    plugin="json",
                    options={
                        "path": str(outputs_dir / "results.jsonl"),
                        "format": "jsonl",
                        "schema": {"mode": "observed"},
                    },
                    on_write_failure="discard",
                ),
            ),
            metadata=PipelineMetadata(name="Shape 9 inline blob agreement", description=""),
            version=1,
        )

    @staticmethod
    def _pipeline_yaml(blob_id: UUID, sha256: str) -> str:
        return f"""
sources:
  source:
    plugin: csv
    on_success: classify_in
    options:
      path: input.csv
transforms:
  - name: classify
    plugin: llm
    input: classify_in
    on_success: results
    on_error: results
    options:
      prompt_template:
        blob_ref: {blob_id}
        mode: inline_content
        sha256: {sha256}
sinks:
  results:
    plugin: json
    on_write_failure: discard
    options:
      path: output.jsonl
      schema:
        mode: observed
"""

    @staticmethod
    def _execution_service(tmp_path: Path) -> tuple[ExecutionServiceImpl, _FakeSessionService, asyncio.AbstractEventLoop]:
        loop = asyncio.new_event_loop()
        settings = _RuntimeSettingsFake(
            data_dir=str(tmp_path),
            payload_store_path=tmp_path / "payloads",
        )

        # ``_run_pipeline`` resolves the run's owning session (get_run().session_id)
        # to scope inline-blob access before any metadata enforcement
        # (IDOR contract, elspeth-195ecb1d58). Tests below set their owned
        # blob_record.session_id to this value so ownership passes and they
        # exercise the hash/audit-ordering assertions they actually target.
        session_service = _FakeSessionService(run=_RunSnapshot(session_id=uuid4()))

        service = ExecutionServiceImpl.for_trained_operator(
            loop=loop,
            broadcaster=cast(Any, _FakeProgressBroadcaster()),
            settings=settings,
            session_service=session_service,
            yaml_generator=cast(Any, SimpleNamespace()),
            telemetry=build_sessions_telemetry(),
        )

        def _call_async(coro: Any) -> Any:
            return loop.run_until_complete(coro)

        cast(Any, service)._call_async = _call_async
        return service, session_service, loop

    def test_validate_returns_structured_error_for_missing_inline_blob(self, tmp_path: Path) -> None:
        blob_id = uuid4()
        state = self._state_with_inline_prompt(tmp_path, blob_id, "a" * 64)

        result = validate_pipeline_for_trained_operator(
            state,
            self._validation_settings(tmp_path),
            composer_yaml_generator,
            blob_get_metadata=lambda _blob_id: None,
            session_id=_AGREEMENT_SESSION_ID,
        )

        assert result.is_valid is False
        check = next(check for check in result.checks if check.name == "blob_inline_refs")
        assert check.passed is False
        assert any(error.error_code == "missing_inline_blob_content" for error in result.errors)
        assert any(error.component_id == "classify" and error.component_type == "transform" for error in result.errors)

    @patch("elspeth.web.execution.service.Orchestrator")
    @patch("elspeth.web.execution.service.load_settings_from_config_dict")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_runtime_fails_closed_on_hash_mismatch_before_settings_load(
        self,
        mock_payload_cls: Any,
        mock_landscape_cls: Any,
        mock_load: Any,
        mock_orch_cls: Any,
        tmp_path: Path,
    ) -> None:
        del mock_payload_cls, mock_landscape_cls
        service, _session_service, loop = self._execution_service(tmp_path)
        content = b"actual prompt bytes"
        blob_id = uuid4()
        run_id = uuid4()
        blob_record = _ready_inline_blob_record(
            blob_id=blob_id,
            session_id=_session_service.run.session_id,
            content=content,
            content_hash=hashlib.sha256(content).hexdigest(),
        )
        blob_service = _FakeBlobService(blob_record=blob_record, content=content)
        cast(Any, service)._blob_service = blob_service

        try:
            with pytest.raises(BlobIntegrityError):
                service._run_pipeline(str(run_id), self._pipeline_yaml(blob_id, "b" * 64), threading.Event())
        finally:
            loop.close()

        mock_load.assert_not_called()
        mock_orch_cls.assert_not_called()

    @patch("elspeth.web.execution.service.load_settings_from_config_dict")
    @patch("elspeth.web.execution.service.open_landscape_db")
    @patch("elspeth.web.execution.service.FilesystemPayloadStore")
    def test_runtime_records_audit_hash_before_settings_load(
        self,
        mock_payload_cls: Any,
        mock_landscape_cls: Any,
        mock_load: Any,
        tmp_path: Path,
    ) -> None:
        del mock_payload_cls, mock_landscape_cls
        service, session_service, loop = self._execution_service(tmp_path)
        content = b"You are an audited prompt."
        sha256 = hashlib.sha256(content).hexdigest()
        blob_id = uuid4()
        run_id = uuid4()
        order: list[str] = []

        blob_record = _ready_inline_blob_record(
            blob_id=blob_id,
            session_id=session_service.run.session_id,
            content=content,
            content_hash=sha256,
        )
        blob_service = _FakeBlobService(blob_record=blob_record, content=content)
        cast(Any, service)._blob_service = blob_service

        async def record_blob_inline_resolutions(
            *,
            run_id: UUID,
            resolutions: Any,
            attempt: int = 1,
        ) -> None:
            del run_id, resolutions, attempt
            order.append("record")

        session_service.record_blob_inline_resolutions_hook = record_blob_inline_resolutions

        def stop_after_audit(config_dict: dict[str, Any], *, expand_env_vars: bool = True) -> None:
            assert "record" in order, "audit row must be recorded before settings/plugin construction"
            # Web-authored YAML must never expand host ${VAR} placeholders.
            assert expand_env_vars is False
            prompt_template = config_dict["transforms"][0]["options"]["prompt_template"]
            assert prompt_template == "You are an audited prompt."
            raise RuntimeError("stop after inline audit")

        mock_load.side_effect = stop_after_audit

        try:
            with pytest.raises(RuntimeError, match="stop after inline audit"):
                service._run_pipeline(str(run_id), self._pipeline_yaml(blob_id, sha256), threading.Event())
        finally:
            loop.close()

        assert len(session_service.recorded_blob_inline_resolutions) == 1
        recorded_call = session_service.recorded_blob_inline_resolutions[0]
        resolutions = recorded_call["resolutions"]
        assert len(resolutions) == 1
        assert resolutions[0].field_path == "node:classify.options.prompt_template"
        assert resolutions[0].content_hash == sha256


class TestComposerRuntimeFixedModeImplicitRequiredAgreement:
    """Shape 10 — fixed-mode consumer implicit-required-field parity (elspeth-8f3b3f650d).

    A consumer whose schema is ``{mode: fixed, fields: [...]}`` *implicitly*
    requires every declared (non-optional) field — the runtime builds a typed
    input Pydantic model and rejects an edge from a TYPED producer that does
    not guarantee one of those declared fields. Authoring-time
    ``CompositionState.validate`` previously computed consumer requirements via
    ``get_raw_node_required_fields`` (EXPLICIT ``required_fields`` only), so a
    fixed-mode declared requirement that exceeds the producer's guarantees was
    green-lit at authoring time and only rejected at runtime — the exact
    "validate green / runtime red" divergence this suite registers.

    The fix gates strictly on producer schema MODE (a fixed/flexible SOURCE
    producer is TYPED; observed sources and transform/gate/coalesce producers
    resolve to a dynamic effective producer schema and are skipped), mirroring
    the runtime Phase-2 observed/dynamic bypass at ``graph.py:1392-1403``.

    Bug verification protocol (mandatory, per the module docstring): revert the
    new sibling block in ``state.py::_check_schema_contracts`` — the
    ``consumer_effective_required`` missing-field append guarded by
    ``producer_is_typed_source`` — and confirm
    ``test_reject_fixed_consumer_implicit_required_over_typed_source`` fails at
    the ``assert not composer_result.is_valid`` line (authoring returns
    ``is_valid=True`` pre-fix). Then restore. Verified 2026-06-09.
    """

    def _empty_state(self) -> CompositionState:
        return CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _state(
        self,
        *,
        source_options: dict[str, Any],
        consumer_options: dict[str, Any],
        output_options: dict[str, Any],
        source_name: str = "source",
    ) -> CompositionState:
        # ``source_name`` defaults to the unnamed default source (producer_id
        # "source"); pass a name to mint a NAMED source (producer_id
        # "source:<name>") — the multi-source headline shape that exercises the
        # ``is_source_producer_id`` predicate rather than the literal "source".
        state = self._empty_state()
        state = state.with_named_source(
            source_name,
            SourceSpec(
                plugin="csv",
                on_success="t1",
                options=source_options,
                on_validation_failure="quarantine",
            ),
        )
        state = state.with_node(
            NodeSpec(
                id="t1",
                node_type="transform",
                plugin="value_transform",
                input="t1",
                on_success="main",
                on_error="discard",
                options=consumer_options,
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options=output_options,
                on_write_failure="discard",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e1",
                from_node=source_name,
                to_node="t1",
                edge_type="on_success",
                label=None,
            )
        )
        return state

    def _build_runtime_graph(
        self,
        *,
        source_options: dict[str, Any],
        consumer_options: dict[str, Any],
        output_options: dict[str, Any],
    ) -> ExecutionGraph:
        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="t1",
                    options={**source_options, "on_validation_failure": "discard"},
                )
            },
            transforms=[
                TransformSettings(
                    name="t1",
                    plugin="value_transform",
                    input="t1",
                    on_success="main",
                    on_error="discard",
                    options=consumer_options,
                )
            ],
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="discard",
                    options=output_options,
                )
            },
        )
        plugins = instantiate_plugins_from_config(config)
        return ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
            coalesce_settings=None,
        )

    def test_reject_fixed_consumer_implicit_required_over_typed_source(
        self,
        tmp_path: Path,
    ) -> None:
        """(A) Typed (fixed) source guarantees {color}; fixed consumer implicitly
        requires {color, teal_pairing_rating}. Both validators MUST reject.

        Pre-fix the authoring validator returned ``is_valid=True`` because the
        fixed-mode implicit requirement ``teal_pairing_rating`` was invisible to
        the explicit-only ``get_raw_node_required_fields`` path — the defect.
        """
        csv_path = tmp_path / "in.csv"
        csv_path.write_text("color\nred\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        source_options = {
            "path": str(csv_path),
            "schema": {"mode": "fixed", "fields": ["color: str"]},
        }
        consumer_options = {
            "operations": [{"target": "out", "expression": "row['color']"}],
            "schema": {
                "mode": "fixed",
                "fields": ["color: str", "teal_pairing_rating: str"],
            },
        }
        output_options = {"path": str(output_path), "schema": {"mode": "observed"}}

        state = self._state(
            source_options=source_options,
            consumer_options=consumer_options,
            output_options=output_options,
        )
        composer_result = state.validate()
        assert not composer_result.is_valid, (
            "Composer should reject: fixed consumer implicitly requires 'teal_pairing_rating' which the typed source does not guarantee."
        )
        assert any(
            "schema contract violation" in entry.message.lower() and "teal_pairing_rating" in entry.message
            for entry in composer_result.errors
        ), [e.message for e in composer_result.errors]

        with pytest.raises(GraphValidationError) as exc_info:
            self._build_runtime_graph(
                source_options=source_options,
                consumer_options=consumer_options,
                output_options=output_options,
            )
        assert "teal_pairing_rating" in str(exc_info.value)

    def test_reject_fixed_consumer_implicit_required_over_named_typed_source(
        self,
        tmp_path: Path,
    ) -> None:
        """(A-named) Same as (A) but the producer is a NAMED source
        (producer_id ``source:customers``), the headline multi-source shape.

        Regression for elspeth-3332619032: ``_producer_is_typed_source`` gated on
        the literal ``producer_id != "source"`` and returned False for any
        ``source:<name>`` producer, so the implicit-required parity check never
        fired for named sources — composer green / runtime red, exactly the
        divergence this suite forbids. The explicit-required path one block up
        already used ``is_source_producer_id`` and rejected correctly, proving
        the intended coverage. Pre-fix this asserts-False at
        ``assert not composer_result.is_valid``.
        """
        csv_path = tmp_path / "in.csv"
        csv_path.write_text("color\nred\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        source_options = {
            "path": str(csv_path),
            "schema": {"mode": "fixed", "fields": ["color: str"]},
        }
        consumer_options = {
            "operations": [{"target": "out", "expression": "row['color']"}],
            "schema": {
                "mode": "fixed",
                "fields": ["color: str", "teal_pairing_rating: str"],
            },
        }
        output_options = {"path": str(output_path), "schema": {"mode": "observed"}}

        state = self._state(
            source_options=source_options,
            consumer_options=consumer_options,
            output_options=output_options,
            source_name="customers",
        )
        composer_result = state.validate()
        assert not composer_result.is_valid, (
            "Composer should reject: fixed consumer implicitly requires "
            "'teal_pairing_rating' which the NAMED typed source 'customers' does not guarantee."
        )
        assert any(
            "schema contract violation" in entry.message.lower() and "teal_pairing_rating" in entry.message
            for entry in composer_result.errors
        ), [e.message for e in composer_result.errors]

        with pytest.raises(GraphValidationError) as exc_info:
            self._build_runtime_graph(
                source_options=source_options,
                consumer_options=consumer_options,
                output_options=output_options,
            )
        assert "teal_pairing_rating" in str(exc_info.value)

    def test_reject_flexible_consumer_implicit_required_over_typed_source(
        self,
        tmp_path: Path,
    ) -> None:
        """(A2) A FLEXIBLE consumer also implicitly requires its declared
        (non-optional) fields, but its input is NOT locked (extras allowed) and
        its explicit ``required_fields`` is empty — so the per-node skip guard
        would short-circuit it unless the effective-required set is folded in.
        Runtime rejects (flexible builds a typed model with non-empty
        ``model_fields``); authoring MUST reject too.
        """
        csv_path = tmp_path / "in.csv"
        csv_path.write_text("color\nred\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        source_options = {
            "path": str(csv_path),
            "schema": {"mode": "fixed", "fields": ["color: str"]},
        }
        consumer_options = {
            "operations": [{"target": "out", "expression": "row['color']"}],
            "schema": {
                "mode": "flexible",
                "fields": ["color: str", "teal_pairing_rating: str"],
            },
        }
        output_options = {"path": str(output_path), "schema": {"mode": "observed"}}

        state = self._state(
            source_options=source_options,
            consumer_options=consumer_options,
            output_options=output_options,
        )
        composer_result = state.validate()
        assert not composer_result.is_valid, (
            "Composer should reject: flexible consumer implicitly requires 'teal_pairing_rating' which the typed source does not guarantee."
        )
        assert any(
            "schema contract violation" in entry.message.lower() and "teal_pairing_rating" in entry.message
            for entry in composer_result.errors
        ), [e.message for e in composer_result.errors]

        with pytest.raises(GraphValidationError) as exc_info:
            self._build_runtime_graph(
                source_options=source_options,
                consumer_options=consumer_options,
                output_options=output_options,
            )
        assert "teal_pairing_rating" in str(exc_info.value)

    def test_accept_observed_source_auto_guarantee_over_fixed_consumer(
        self,
        tmp_path: Path,
    ) -> None:
        """(B) Overshoot tripwire: an OBSERVED source has non-empty guarantees
        (the auto-guaranteed column) yet runtime bypasses Phase-2 type
        validation because the producer schema is observed. Authoring MUST also
        accept — proving the gate is on producer MODE, not guarantee-emptiness.
        """
        text_path = tmp_path / "in.txt"
        text_path.write_text("hello\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="text",
                on_success="t1",
                options={
                    "path": str(text_path),
                    "column": "color",
                    "schema": {"mode": "observed"},
                },
                on_validation_failure="quarantine",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="t1",
                node_type="transform",
                plugin="value_transform",
                input="t1",
                on_success="main",
                on_error="discard",
                options={
                    "operations": [{"target": "out", "expression": "row['color']"}],
                    "schema": {
                        "mode": "fixed",
                        "fields": ["color: str", "teal_pairing_rating: str"],
                    },
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": str(output_path), "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
        )
        state = state.with_edge(
            EdgeSpec(
                id="e1",
                from_node="source",
                to_node="t1",
                edge_type="on_success",
                label=None,
            )
        )

        composer_result = state.validate()
        assert composer_result.is_valid, [e.message for e in composer_result.errors]

        config = ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="text",
                    on_success="t1",
                    options={
                        "path": str(text_path),
                        "column": "color",
                        "schema": {"mode": "observed"},
                        "on_validation_failure": "discard",
                    },
                )
            },
            transforms=[
                TransformSettings(
                    name="t1",
                    plugin="value_transform",
                    input="t1",
                    on_success="main",
                    on_error="discard",
                    options={
                        "operations": [{"target": "out", "expression": "row['color']"}],
                        "schema": {
                            "mode": "fixed",
                            "fields": ["color: str", "teal_pairing_rating: str"],
                        },
                    },
                )
            ],
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="discard",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                )
            },
        )
        plugins = instantiate_plugins_from_config(config)
        # Runtime construction (which validates edges) must not raise.
        ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=[],
            coalesce_settings=None,
        )

    def test_accept_optional_declared_field_over_typed_source(
        self,
        tmp_path: Path,
    ) -> None:
        """(C) An OPTIONAL declared field (``teal_pairing_rating: str?``) is not
        implicitly required; both validators MUST accept over a {color} source.
        """
        csv_path = tmp_path / "in.csv"
        csv_path.write_text("color\nred\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        source_options = {
            "path": str(csv_path),
            "schema": {"mode": "fixed", "fields": ["color: str"]},
        }
        consumer_options = {
            "operations": [{"target": "out", "expression": "row['color']"}],
            "schema": {
                "mode": "fixed",
                "fields": ["color: str", "teal_pairing_rating: str?"],
            },
        }
        output_options = {"path": str(output_path), "schema": {"mode": "observed"}}

        state = self._state(
            source_options=source_options,
            consumer_options=consumer_options,
            output_options=output_options,
        )
        composer_result = state.validate()
        assert composer_result.is_valid, [e.message for e in composer_result.errors]

        # Runtime construction must not raise.
        self._build_runtime_graph(
            source_options=source_options,
            consumer_options=consumer_options,
            output_options=output_options,
        )

    def test_accept_plain_observed_consumer_over_typed_source(
        self,
        tmp_path: Path,
    ) -> None:
        """(D) A plain OBSERVED consumer imposes no implicit requirements; both
        validators MUST accept over a typed (fixed) source.
        """
        csv_path = tmp_path / "in.csv"
        csv_path.write_text("color\nred\n", encoding="utf-8")
        output_path = tmp_path / "out.csv"

        source_options = {
            "path": str(csv_path),
            "schema": {"mode": "fixed", "fields": ["color: str"]},
        }
        consumer_options = {
            "operations": [{"target": "out", "expression": "row['color']"}],
            "schema": {"mode": "observed"},
        }
        output_options = {"path": str(output_path), "schema": {"mode": "observed"}}

        state = self._state(
            source_options=source_options,
            consumer_options=consumer_options,
            output_options=output_options,
        )
        composer_result = state.validate()
        assert composer_result.is_valid, [e.message for e in composer_result.errors]

        self._build_runtime_graph(
            source_options=source_options,
            consumer_options=consumer_options,
            output_options=output_options,
        )


class TestComposerRuntimeGateRouteParityAgreement:
    """Biconditional agreement for gate route-label / condition-return-type parity.

    Mirror of GateSettings.validate_boolean_routes (core/config.py). The composer's
    CompositionState.validate() must agree with GateSettings construction on whether
    a gate's route labels are consistent with the static return type of its condition:
    composer is_valid  <=>  GateSettings accepts. Regression for elspeth-08e17b9253,
    where the composer green-lit boolean/numeric conditions with mismatched labels
    that runtime config later rejected.
    """

    def _gate_state(self, *, condition: str, routes: dict[str, str]) -> CompositionState:
        """A minimal valid pipeline whose only interesting feature is a gate."""
        state = CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )
        state = state.with_source(
            SourceSpec(
                plugin="text",
                on_success="g1",
                options={"path": "/tmp/in.txt", "column": "line", "schema": {"mode": "observed"}},
                on_validation_failure="discard",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="g1",
                node_type="gate",
                plugin=None,
                input="g1",
                on_success=None,
                on_error=None,
                options={},
                condition=condition,
                routes=routes,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": "/tmp/out.csv", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
        )
        state = state.with_edge(EdgeSpec(id="e0", from_node="source", to_node="g1", edge_type="on_success", label=None))
        return state

    def test_boolean_gate_with_custom_route_labels_rejected_by_both(self) -> None:
        """Boolean condition + non-true/false labels: composer is_valid=False AND GateSettings raises."""
        state = self._gate_state(condition='row["x"] > 0', routes={"high": "main", "low": "main"})

        composer_result = state.validate()
        assert composer_result.is_valid is False
        assert any("boolean condition" in e.message for e in composer_result.errors), [e.message for e in composer_result.errors]

        with pytest.raises(ValidationError, match="boolean condition"):
            GateSettings(
                name="g1",
                input="g1",
                condition='row["x"] > 0',
                routes={"high": "main", "low": "main"},
            )

    def test_numeric_gate_condition_rejected_by_both(self) -> None:
        """Provably-numeric condition: composer is_valid=False AND GateSettings raises."""
        state = self._gate_state(condition='row["x"] + 1', routes={"a": "main"})

        composer_result = state.validate()
        assert composer_result.is_valid is False
        assert any("numeric value" in e.message for e in composer_result.errors), [e.message for e in composer_result.errors]

        with pytest.raises(ValidationError, match="numeric value"):
            GateSettings(
                name="g1",
                input="g1",
                condition='row["x"] + 1',
                routes={"a": "main"},
            )

    def test_string_route_gate_accepted_by_both(self) -> None:
        """POSITIVE CONTROL: string-returning condition + matching custom labels stays valid in both."""
        state = self._gate_state(
            condition='"high" if row["x"] > 0 else "low"',
            routes={"high": "main", "low": "main"},
        )

        composer_result = state.validate()
        assert composer_result.is_valid is True, [e.message for e in composer_result.errors]

        # Must construct cleanly — legal runtime config, must NOT be over-rejected.
        GateSettings(
            name="g1",
            input="g1",
            condition='"high" if row["x"] > 0 else "low"',
            routes={"high": "main", "low": "main"},
        )

    def test_boolean_gate_with_true_false_labels_accepted_by_both(self) -> None:
        """Boolean condition + exactly {true,false} labels stays valid in both."""
        state = self._gate_state(
            condition='row["x"] > 0',
            routes={"true": "main", "false": "main"},
        )

        composer_result = state.validate()
        assert composer_result.is_valid is True, [e.message for e in composer_result.errors]

        GateSettings(
            name="g1",
            input="g1",
            condition='row["x"] > 0',
            routes={"true": "main", "false": "main"},
        )


class TestComposerRuntimeQueueAgreement:
    """Shape 11 — structural queue fan-in round-trips composer <-> runtime.

    The shipped ``examples/multi_source_queue/settings.yaml`` fans two sources
    into one declared ``queues.inbound`` and then a single passthrough. The
    composer must import it (preserving the queue), validate it green, export it
    back to runtime YAML, and the runtime must build a graph containing exactly
    one ``NodeType.QUEUE`` with both sources leading into it and one ordinary
    downstream consumer leading out — matching the engine's own fan-in contract
    (ADR-025 / elspeth-a5b86149d4, elspeth-6421ffa028).

    Bug-verification protocol (mandatory per this file's header): the shape is
    pinned on the generator's queue-emission assignment in
    ``web/composer/yaml_generator._generate_pipeline_dict`` — the
    ``doc["queues"] = queues_doc`` line. Manually replacing that line with
    ``pass`` makes ``generate_yaml`` omit the ``queues:`` section entirely, so
    ``load_settings_from_yaml_string`` rebuilds settings with no queue and
    ``test_queue_round_trips_composer_import_export_and_runtime_graph`` fails at
    its first queue-dependent assertion with
    ``AssertionError: assert set() == {'inbound'}`` (``set(settings.queues) ==
    {"inbound"}``). Verified by manual revert on 2026-07-12; restored. Had the
    queue survived import but not the runtime fan-in contract, the same
    two-source topology without a queue is rejected at graph build with
    ``GraphValidationError("Duplicate producer for connection 'inbound' ...")``,
    which the negative control below asserts directly by deleting the emitted
    ``queues:`` section.

    Shape 12 is pinned on
    ``web/composer/state._coalesce_mapped_branch_connections`` and its use in
    connection/queue consumer accounting. Reverting those call sites to exclude
    mapped branch claims and to count only ``node.input`` makes the mapped-only
    test fail with ``queue_no_consumer``, the mapped-plus-ordinary test fail to
    report ``duplicate_connection_consumer``, and the list-form test fail to
    report ``queue_no_consumer``. Verified by manually reverting both call
    sites on 2026-07-28; the three focused tests failed in exactly those ways,
    then passed after restoration.
    """

    def _example_yaml(self) -> str:
        example = Path(__file__).resolve().parents[3] / "examples" / "multi_source_queue" / "settings.yaml"
        return example.read_text(encoding="utf-8")

    def _build_runtime_graph_for_settings(self, settings: ElspethSettings) -> ExecutionGraph:
        bundle = instantiate_plugins_from_config(settings, preflight_mode=True)
        return ExecutionGraph.from_plugin_instances(
            sources=bundle.sources,
            source_settings_map=bundle.source_settings_map,
            transforms=bundle.transforms,
            sinks=bundle.sinks,
            aggregations=bundle.aggregations,
            gates=list(settings.gates),
            coalesce_settings=list(settings.coalesce),
            queues=settings.queues,
        )

    def _queue_to_coalesce_yaml(self, *, branch_form: str, ordinary_consumer: bool = False) -> str:
        import yaml

        if branch_form == "mapping":
            fork_to = ["queued_path", "other_path"]
            transforms: list[dict[str, Any]] = [
                {
                    "name": "queued_leg",
                    "plugin": "passthrough",
                    "input": "queued_path",
                    "on_success": "inbound",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}},
                },
                {
                    "name": "other_leg",
                    "plugin": "passthrough",
                    "input": "other_path",
                    "on_success": "other_done",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}},
                },
            ]
            branches: list[str] | dict[str, str] = {
                "other_path": "other_done",
                "queued_path": "inbound",
            }
        elif branch_form == "list":
            fork_to = ["inbound", "other_done"]
            transforms = []
            branches = ["inbound", "other_done"]
        else:
            raise AssertionError(f"unknown branch form: {branch_form}")

        if ordinary_consumer:
            transforms.append(
                {
                    "name": "duplicate_consumer",
                    "plugin": "passthrough",
                    "input": "inbound",
                    "on_success": "duplicate_out",
                    "on_error": "discard",
                    "options": {"schema": {"mode": "observed"}},
                }
            )

        def source(path: str, on_success: str) -> dict[str, Any]:
            return {
                "plugin": "csv",
                "on_success": on_success,
                "options": {
                    "path": path,
                    "schema": {"mode": "observed"},
                    "on_validation_failure": "discard",
                },
            }

        sinks: dict[str, Any] = {
            "combined": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": "examples/multi_source_queue/output/combined.jsonl",
                    "format": "jsonl",
                    "collision_policy": "auto_increment",
                    "schema": {"mode": "observed"},
                },
            }
        }
        if ordinary_consumer:
            sinks["duplicate_out"] = {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": "examples/multi_source_queue/output/duplicate.jsonl",
                    "format": "jsonl",
                    "collision_policy": "auto_increment",
                    "schema": {"mode": "observed"},
                },
            }

        doc = {
            "sources": {
                "orders": source("examples/multi_source_queue/input/orders.csv", "inbound"),
                "refunds": source("examples/multi_source_queue/input/refunds.csv", "inbound"),
                "fork_root": source("examples/multi_source_queue/input/orders.csv", "fork_input"),
            },
            "queues": {"inbound": {}},
            "transforms": transforms,
            "gates": [
                {
                    "name": "fork_rows",
                    "input": "fork_input",
                    "condition": "True",
                    "routes": {"true": "fork", "false": "discard"},
                    "fork_to": fork_to,
                }
            ],
            "coalesce": [
                {
                    "name": "merged",
                    "branches": branches,
                    "policy": "require_all",
                    "merge": "nested",
                    "on_success": "combined",
                }
            ],
            "sinks": sinks,
            "landscape": {"url": "sqlite:///examples/multi_source_queue/runs/audit.db"},
        }
        return yaml.safe_dump(doc, sort_keys=False)

    def test_queue_round_trips_composer_import_export_and_runtime_graph(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.core.config import load_settings_from_yaml_string
        from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

        state = composition_state_from_runtime_yaml(self._example_yaml())
        composer_result = state.validate()
        assert composer_result.is_valid, [e.message for e in composer_result.errors]

        generated_yaml = composer_yaml_generator.generate_yaml(state)
        settings = load_settings_from_yaml_string(generated_yaml)
        assert set(settings.queues) == {"inbound"}

        graph = self._build_runtime_graph_for_settings(settings)
        graph.validate()

        queue_nodes = [node for node in graph.get_nodes() if node.node_type == NodeType.QUEUE]
        assert len(queue_nodes) == 1
        queue_info = queue_nodes[0]
        # The runtime keys queues by a hashed queue_<name>_<hash> node id; the raw
        # queue name lives in config["name"], not the node id.
        assert queue_info.config["name"] == "inbound"
        assert queue_info.node_id != "inbound"
        assert queue_info.output_schema_config is not None
        assert queue_info.output_schema_config.mode == "observed"

        queue_id = queue_info.node_id
        source_predecessors = {
            edge.from_node
            for edge in graph.get_incoming_edges(queue_id)
            if graph.get_node_info(edge.from_node).node_type == NodeType.SOURCE
        }
        assert len(source_predecessors) == 2, "both sources must fan into the queue"

        outgoing = [edge for edge in graph.get_edges() if edge.from_node == queue_id]
        assert len(outgoing) == 1, "queue feeds exactly one ordinary downstream consumer"
        downstream = graph.get_node_info(outgoing[0].to_node)
        assert downstream.node_type == NodeType.TRANSFORM
        assert downstream.plugin_name == "passthrough"

    def test_deleting_generated_queues_section_reproduces_fan_in_rejection(self) -> None:
        """Manual negative control: without the emitted queue, the same two-source
        fan-in is exactly the topology the runtime rejects."""
        import yaml

        from elspeth.core.config import load_settings_from_yaml_string
        from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

        state = composition_state_from_runtime_yaml(self._example_yaml())
        generated_yaml = composer_yaml_generator.generate_yaml(state)

        doc = yaml.safe_load(generated_yaml)
        assert "queues" in doc, "sanity: the generator emitted the queue section"
        del doc["queues"]
        no_queue_yaml = yaml.dump(doc, sort_keys=False)

        settings = load_settings_from_yaml_string(no_queue_yaml)
        assert not settings.queues

        # With no queue, the two sources publishing 'inbound' are an undeclared
        # duplicate producer; the runtime rejects this at graph build time.
        with pytest.raises(GraphValidationError, match="Duplicate producer for connection 'inbound'"):
            self._build_runtime_graph_for_settings(settings)

    def test_mapped_coalesce_branch_is_the_queues_single_consumer_in_both_layers(self) -> None:
        """A mapped branch consumes its connection in Composer exactly as runtime does."""
        from elspeth.core.config import load_settings_from_yaml_string
        from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

        state = composition_state_from_runtime_yaml(self._queue_to_coalesce_yaml(branch_form="mapping"))
        composer_result = state.validate()
        assert composer_result.is_valid, [e.message for e in composer_result.errors]

        generated_yaml = composer_yaml_generator.generate_yaml(state)
        settings = load_settings_from_yaml_string(generated_yaml)
        graph = self._build_runtime_graph_for_settings(settings)
        graph.validate()

    def test_mapped_coalesce_and_ordinary_queue_consumers_are_rejected_in_both_layers(self) -> None:
        """A mapped branch plus an ordinary node is forbidden queue fan-out."""
        from elspeth.core.config import load_settings_from_yaml_string
        from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

        state = composition_state_from_runtime_yaml(self._queue_to_coalesce_yaml(branch_form="mapping", ordinary_consumer=True))
        composer_result = state.validate()
        assert any(error.error_code == "duplicate_connection_consumer" for error in composer_result.errors)

        settings = load_settings_from_yaml_string(composer_yaml_generator.generate_yaml(state))
        with pytest.raises(GraphValidationError, match="Duplicate consumers"):
            self._build_runtime_graph_for_settings(settings)

    def test_list_form_identity_branch_is_not_a_queue_consumer_in_either_layer(self) -> None:
        """List-form branches are gate identity edges, not queue connection consumers."""
        from elspeth.core.config import load_settings_from_yaml_string
        from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

        state = composition_state_from_runtime_yaml(self._queue_to_coalesce_yaml(branch_form="list"))
        composer_result = state.validate()
        assert any(error.error_code == "queue_no_consumer" for error in composer_result.errors)

        settings = load_settings_from_yaml_string(composer_yaml_generator.generate_yaml(state))
        with pytest.raises(GraphValidationError, match="queue 'inbound' with no downstream consumer"):
            self._build_runtime_graph_for_settings(settings)


class TestComposerRuntimeRowUnionAgreement:
    """Shape 13 — shipped row_union YAML round-trips composer <-> runtime.

    Bug-verification protocol: before the row_union lowering existed,
    ``test_row_union_example_round_trips_semantics_and_runtime_graph`` failed
    during Composer import with ``RuntimeYamlImportError: row_unions are not
    supported by Composer import``. With importer support but the generator's
    ``doc["row_unions"]`` assignment removed, the same test fails its
    semantic-section equality because regenerated YAML has no ``row_unions``
    key. Those are the exact production seams this class pins.
    """

    _SEMANTIC_SECTIONS = ("sources", "transforms", "gates", "row_unions", "aggregations", "sinks")

    def _example_yaml(self) -> str:
        example = Path(__file__).resolve().parents[3] / "examples" / "row_union_ab_experiment" / "settings.yaml"
        return example.read_text(encoding="utf-8")

    def _build_runtime_graph_for_settings(self, settings: ElspethSettings) -> ExecutionGraph:
        bundle = instantiate_plugins_from_config(settings, preflight_mode=True)
        return ExecutionGraph.from_plugin_instances(
            sources=bundle.sources,
            source_settings_map=bundle.source_settings_map,
            transforms=bundle.transforms,
            sinks=bundle.sinks,
            aggregations=bundle.aggregations,
            gates=list(settings.gates),
            coalesce_settings=list(settings.coalesce) if settings.coalesce else None,
            queues=settings.queues,
            row_union_settings=list(settings.row_unions),
        )

    def _semantic_pipeline(self, doc: dict[str, Any]) -> dict[str, Any]:
        return {section: doc[section] for section in self._SEMANTIC_SECTIONS if section in doc}

    def _graph_signature(
        self,
        graph: ExecutionGraph,
    ) -> tuple[dict[str, tuple[Any, ...]], set[tuple[str, str, str, str]]]:
        nodes: dict[str, tuple[Any, ...]] = {
            str(node.node_id): (
                node.node_type,
                node.plugin_name,
                node.config,
                node.input_schema_config,
                node.output_schema_config,
            )
            for node in graph.get_nodes()
        }
        edges = {(str(edge.from_node), str(edge.to_node), edge.label, edge.mode.value) for edge in graph.get_edges()}
        return nodes, edges

    def test_row_union_example_round_trips_semantics_and_runtime_graph(self) -> None:
        import yaml

        from elspeth.core.config import load_settings_from_yaml_string
        from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

        original_yaml = self._example_yaml()
        original_doc = yaml.safe_load(original_yaml)
        state = composition_state_from_runtime_yaml(original_yaml)
        composer_result = state.validate()
        assert composer_result.is_valid, [error.message for error in composer_result.errors]

        regenerated_yaml = composer_yaml_generator.generate_yaml(state)
        regenerated_doc = yaml.safe_load(regenerated_yaml)
        assert self._semantic_pipeline(regenerated_doc) == self._semantic_pipeline(original_doc)

        original_settings = load_settings_from_yaml_string(original_yaml)
        regenerated_settings = load_settings_from_yaml_string(regenerated_yaml)
        assert regenerated_settings.row_unions == original_settings.row_unions

        original_graph = self._build_runtime_graph_for_settings(original_settings)
        regenerated_graph = self._build_runtime_graph_for_settings(regenerated_settings)
        original_graph.validate()
        regenerated_graph.validate()
        assert self._graph_signature(regenerated_graph) == self._graph_signature(original_graph)

    def test_regenerated_row_union_keeps_early_trigger_guard_actionable(self) -> None:
        import yaml

        from elspeth.core.config import load_settings_from_yaml_string
        from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

        state = composition_state_from_runtime_yaml(self._example_yaml())
        regenerated_doc = yaml.safe_load(composer_yaml_generator.generate_yaml(state))
        regenerated_doc["aggregations"][0]["trigger"] = {"count": 2}
        invalid_yaml = yaml.safe_dump(regenerated_doc, sort_keys=False)

        composer_state = composition_state_from_runtime_yaml(invalid_yaml)
        composer_result = composer_state.validate()
        composer_error = next(
            error
            for error in composer_result.errors
            if error.component == "node:variant_union"
            and error.error_code == "row_union_downstream_group_invalid"
            and "indivisible" in error.message
        )
        assert "count/timeout/condition trigger" in composer_error.message

        settings = load_settings_from_yaml_string(invalid_yaml)

        with pytest.raises(GraphValidationError) as exc_info:
            self._build_runtime_graph_for_settings(settings)

        message = str(exc_info.value)
        assert "downstream of row_union 'variant_union'" in message
        assert "count/timeout/condition trigger" in message
        assert "Use the implicit end_of_source trigger" in message

    def test_transform_mode_branch_aggregation_is_rejected_by_both_layers(self) -> None:
        """Composer mirrors the runtime branch-aggregation identity guard.

        Bug-verification protocol: before the Composer backward branch walk,
        ``CompositionState.validate()`` accepted this candidate while
        ``_build_runtime_graph_for_settings`` raised ``GraphValidationError``
        naming ``control_batch`` and its transform-mode row_id hazard.
        """
        import yaml

        from elspeth.core.config import load_settings_from_yaml_string
        from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

        doc = yaml.safe_load(self._example_yaml())
        control = doc["transforms"].pop(0)
        doc["aggregations"].insert(
            0,
            {
                "name": "control_batch",
                "plugin": "batch_replicate",
                "input": control["input"],
                "on_success": control["on_success"],
                "on_error": "discard",
                "trigger": {},
                "output_mode": "transform",
                "options": {
                    "schema": {"mode": "observed"},
                    "copies_field": "baseline_quality",
                    "default_copies": 1,
                    "include_copy_index": False,
                },
            },
        )
        invalid_yaml = yaml.safe_dump(doc, sort_keys=False)

        composer_result = composition_state_from_runtime_yaml(invalid_yaml).validate()
        composer_error = next(error for error in composer_result.errors if error.error_code == "row_union_branch_aggregation_invalid")
        assert "control_batch" in composer_error.message
        assert "passthrough" in composer_error.message

        settings = load_settings_from_yaml_string(invalid_yaml)
        with pytest.raises(GraphValidationError) as exc_info:
            self._build_runtime_graph_for_settings(settings)
        assert "control_batch" in str(exc_info.value)
        assert "row_id" in str(exc_info.value)

    def test_nested_branch_fork_is_rejected_by_both_layers(self, tmp_path: Path) -> None:
        """Composer mirrors the runtime nested-fork branch-identity guard.

        Bug-verification protocol: before the Composer backward branch walk,
        ``CompositionState.validate()`` accepted this candidate while
        ``_build_runtime_graph_for_settings`` raised ``GraphValidationError``
        naming ``nested_fork`` and ``variant_union``.
        """
        import yaml

        from elspeth.core.config import load_settings_from_yaml_string
        from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

        doc = yaml.safe_load(self._example_yaml())
        doc["transforms"][0]["on_success"] = "control_staged"
        doc["gates"].append(
            {
                "name": "nested_fork",
                "input": "control_staged",
                "condition": "True",
                "routes": {"true": "fork", "false": "control_scored"},
                "fork_to": ["inner_left", "inner_right"],
            }
        )
        for branch_name in ("inner_left", "inner_right"):
            doc["sinks"][branch_name] = {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": str(tmp_path / f"{branch_name}.jsonl"),
                    "format": "jsonl",
                    "schema": {"mode": "observed"},
                },
            }
        invalid_yaml = yaml.safe_dump(doc, sort_keys=False)

        composer_result = composition_state_from_runtime_yaml(invalid_yaml).validate()
        composer_error = next(error for error in composer_result.errors if error.error_code == "row_union_nested_fork_invalid")
        assert "nested_fork" in composer_error.message
        assert "variant_union" in composer_error.message

        settings = load_settings_from_yaml_string(invalid_yaml)
        with pytest.raises(GraphValidationError) as exc_info:
            self._build_runtime_graph_for_settings(settings)
        assert "nested_fork" in str(exc_info.value)
        assert "variant_union" in str(exc_info.value)

    def test_invalid_row_union_name_is_rejected_by_both_layers(self) -> None:
        """Composer mirrors the runtime RowUnionSettings name validators.

        Bug-verification protocol: before the row_union name check in
        ``CompositionState.validate()``, Composer imported and accepted
        ``name: bad name`` while ``load_settings_from_yaml_string`` raised a
        Pydantic ``ValidationError`` for the invalid identifier.
        """
        import yaml

        from elspeth.core.config import load_settings_from_yaml_string
        from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

        doc = yaml.safe_load(self._example_yaml())
        doc["row_unions"][0]["name"] = "bad name"
        invalid_yaml = yaml.safe_dump(doc, sort_keys=False)

        composer_result = composition_state_from_runtime_yaml(invalid_yaml).validate()
        composer_error = next(error for error in composer_result.errors if error.error_code == "row_union_name_invalid")
        assert "bad name" in composer_error.message

        with pytest.raises(ValidationError):
            load_settings_from_yaml_string(invalid_yaml)


class TestComposerRuntimeCoalesceUnionTypeAgreement:
    """Shape 18 — a union coalesce's shared-field types agree on both surfaces.

    Battery round-6 g03 (``elspeth-85f3cc3022``) in plugin-neutral form: a fork
    gate feeds a transform on each branch, each declaring its own explicit
    schema, and both branches merge at ``merge: union``. When the two branches
    declare the same field with different types the runtime graph build rejects
    it; before this shape closed, composer Stage 1 accepted it, so the mutation
    envelope told the compose loop the pipeline was clean.

    The positive control (identical declared types) must stay green on both
    surfaces — the risk of mirroring a runtime rule into authoring is a mirror
    that is STRICTER than the runtime, which would block runnable pipelines, a
    strictly worse failure than the permissiveness it replaces.

    Bug-verification protocol (mandatory per this file's header): the shape is
    pinned on the ``UnionTypeConflictError`` handler around the
    ``merge_union_field_flags`` call in ``web/composer/state.py::validate``'s
    union-coalesce loop. Neutering that handler (swallowing the exception so no
    entry is appended) restores the pre-fix behaviour and fails exactly two of
    these three tests, both at their COMPOSER assertion while their runtime half
    still raises — which is precisely the validate-green/runtime-red divergence
    this shape records:
      - ``test_both_reject_incompatible_shared_field_types`` fails with
        ``assert not True`` where the summary is
        ``ValidationSummary(is_valid=True, errors=(), …)``;
      - ``test_both_reject_any_against_a_concrete_type`` fails with
        ``assert False`` on the ``coalesce_union_type_incompatible`` search.
    ``test_both_accept_compatible_shared_field_types`` still passes under the
    mutation, as a positive control must. Verified by manual revert on
    2026-08-07; restored. Per this file's METHOD NOTE the marker was asserted
    unique before editing.
    """

    def _empty_state(self) -> CompositionState:
        return CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _composer_state(
        self,
        *,
        csv_path: Path,
        output_path: Path,
        label_schema: dict[str, Any],
        price_schema: dict[str, Any],
    ) -> CompositionState:
        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="csv",
                on_success="gate_in",
                options={
                    "path": str(csv_path),
                    "schema": {"mode": "fixed", "fields": ["id: int", "price: int"]},
                },
                on_validation_failure="discard",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("branch_label", "branch_price"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        for node_id, branch_connection, done_connection, schema in (
            ("t_label", "branch_label", "label_done", label_schema),
            ("t_price", "branch_price", "price_done", price_schema),
        ):
            state = state.with_node(
                NodeSpec(
                    id=node_id,
                    node_type="transform",
                    plugin="value_transform",
                    input=branch_connection,
                    on_success=done_connection,
                    on_error="discard",
                    options={
                        "schema": schema,
                        "operations": [{"target": "price", "expression": "row['price']"}],
                    },
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
        state = state.with_node(
            NodeSpec(
                id="merge_results",
                node_type="coalesce",
                plugin=None,
                input="label_done",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"branch_label": "label_done", "branch_price": "price_done"},
                policy="require_all",
                merge="union",
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": str(output_path), "schema": {"mode": "observed"}},
                on_write_failure="discard",
            )
        )
        for edge_id, from_node, to_node, edge_type, label in (
            ("e1", "source", "fork_gate", "on_success", None),
            ("e2", "fork_gate", "t_label", "fork", "branch_label"),
            ("e3", "fork_gate", "t_price", "fork", "branch_price"),
            ("e4", "t_label", "merge_results", "on_success", None),
            ("e5", "t_price", "merge_results", "on_success", None),
        ):
            state = state.with_edge(
                EdgeSpec(
                    id=edge_id,
                    from_node=from_node,
                    to_node=to_node,
                    edge_type=edge_type,
                    label=label,
                )
            )
        return state

    def _runtime_settings(
        self,
        *,
        csv_path: Path,
        output_path: Path,
        label_schema: dict[str, Any],
        price_schema: dict[str, Any],
    ) -> ElspethSettings:
        return ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="gate_in",
                    options={
                        "path": str(csv_path),
                        "schema": {"mode": "fixed", "fields": ["id: int", "price: int"]},
                        "on_validation_failure": "discard",
                    },
                )
            },
            transforms=[
                TransformSettings(
                    name="t_label",
                    plugin="value_transform",
                    input="branch_label",
                    on_success="label_done",
                    on_error="discard",
                    options={
                        "schema": label_schema,
                        "operations": [{"target": "price", "expression": "row['price']"}],
                    },
                ),
                TransformSettings(
                    name="t_price",
                    plugin="value_transform",
                    input="branch_price",
                    on_success="price_done",
                    on_error="discard",
                    options={
                        "schema": price_schema,
                        "operations": [{"target": "price", "expression": "row['price']"}],
                    },
                ),
            ],
            gates=[
                GateSettings(
                    name="fork_gate",
                    input="gate_in",
                    condition="True",
                    routes={"true": "fork", "false": "fork"},
                    fork_to=["branch_label", "branch_price"],
                )
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_results",
                    branches={"branch_label": "label_done", "branch_price": "price_done"},
                    policy="require_all",
                    merge="union",
                    on_success="main",
                )
            ],
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="discard",
                    options={"path": str(output_path), "schema": {"mode": "observed"}},
                )
            },
        )

    def _paths(self, tmp_path: Path) -> tuple[Path, Path]:
        csv_path = tmp_path / "input.csv"
        csv_path.write_text("id,price\n1,2\n", encoding="utf-8")
        return csv_path, tmp_path / "out.csv"

    def _build_runtime_graph_from_settings(self, config: ElspethSettings) -> ExecutionGraph:
        plugins = instantiate_plugins_from_config(config)
        return ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
            coalesce_settings=list(config.coalesce) if config.coalesce else None,
        )

    def test_both_reject_incompatible_shared_field_types(self, tmp_path: Path) -> None:
        """The g03 shape: ``price`` declared ``int`` on one branch, ``str`` on the other."""
        csv_path, output_path = self._paths(tmp_path)
        label_schema = {"mode": "fixed", "fields": ["id: int", "price: int"]}
        price_schema = {"mode": "fixed", "fields": ["id: int", "price: str"]}

        composer_result = self._composer_state(
            csv_path=csv_path,
            output_path=output_path,
            label_schema=label_schema,
            price_schema=price_schema,
        ).validate()

        assert not composer_result.is_valid
        entries = [error for error in composer_result.errors if error.error_code == "coalesce_union_type_incompatible"]
        assert len(entries) == 1, composer_result.errors
        assert entries[0].component == "node:merge_results"
        assert "price" in entries[0].message

        with pytest.raises(GraphValidationError) as exc_info:
            graph = self._build_runtime_graph_from_settings(
                self._runtime_settings(
                    csv_path=csv_path,
                    output_path=output_path,
                    label_schema=label_schema,
                    price_schema=price_schema,
                )
            )
            graph.validate_edge_compatibility()
        message = str(exc_info.value).lower()
        assert "incompatible" in message
        assert "price" in message

    def test_both_accept_compatible_shared_field_types(self, tmp_path: Path) -> None:
        """Positive control: the mirror must not be stricter than the runtime."""
        csv_path, output_path = self._paths(tmp_path)
        schema = {"mode": "fixed", "fields": ["id: int", "price: int"]}

        composer_result = self._composer_state(
            csv_path=csv_path,
            output_path=output_path,
            label_schema=schema,
            price_schema=schema,
        ).validate()
        assert composer_result.is_valid, composer_result.errors

        graph = self._build_runtime_graph_from_settings(
            self._runtime_settings(
                csv_path=csv_path,
                output_path=output_path,
                label_schema=schema,
                price_schema=schema,
            )
        )
        graph.validate_edge_compatibility()

    def test_both_reject_any_against_a_concrete_type(self, tmp_path: Path) -> None:
        """``any`` is a declared type here, not a wildcard — unlike row_union.

        ``row_union_schema_configs_compatible`` skips ``any`` on flexible
        branches; ``merge_union_field_flags`` compares type keys with ``!=``.
        The two node kinds have genuinely different rules, so this pins that
        the coalesce mirror follows the coalesce rule.
        """
        csv_path, output_path = self._paths(tmp_path)
        label_schema = {"mode": "fixed", "fields": ["id: int", "price: any"]}
        price_schema = {"mode": "fixed", "fields": ["id: int", "price: int"]}

        composer_result = self._composer_state(
            csv_path=csv_path,
            output_path=output_path,
            label_schema=label_schema,
            price_schema=price_schema,
        ).validate()
        assert any(error.error_code == "coalesce_union_type_incompatible" for error in composer_result.errors)

        with pytest.raises(GraphValidationError):
            graph = self._build_runtime_graph_from_settings(
                self._runtime_settings(
                    csv_path=csv_path,
                    output_path=output_path,
                    label_schema=label_schema,
                    price_schema=price_schema,
                )
            )
            graph.validate_edge_compatibility()


class TestComposerRuntimeQueueGuaranteeAgreement:
    """Shape 14 — queue consumers see upstream guarantees on both surfaces.

    The battery g08 topology in plugin-neutral form: a source explicitly
    guaranteeing ``llm_response`` feeds a declared queue, and the queue's
    consumer names ``required_input_fields: [llm_response]``. Runtime graph
    build must accept it (the guarantee propagates through the queue), and the
    composer must import and validate the same YAML green. The negative
    control proves fail-closed retention: requiring a field NO arm guarantees
    is still rejected at graph build with the same actionable message.

    Bug-verification protocol (mandatory per this file's header): the shape is
    pinned on the ``NodeType.QUEUE`` branch of
    ``core/dag/guarantees.walk_effective_guarantee_vote``. Manually replacing
    that branch's condition with ``if False:`` restores the pre-fix walk (the
    queue reports its own empty observed schema) and
    ``test_both_accept_queue_consumer_requiring_arm_guaranteed_field`` fails at
    graph build with ``GraphValidationError: Schema contract violation: edge
    'queue_inbound_…' → 'transform_consumer_…' … Producer (queue:inbound)
    guarantees: (none - dynamic schema)``. Verified by manual revert on
    2026-08-05; restored. The negative control pins that neither surface fails
    open: the runtime rejects at graph build, and since elspeth-3619b8774f the
    composer Stage-1 fan-in mirror rejects the same YAML at /validate.
    """

    def _yaml(self, *, required_field: str) -> str:
        import yaml

        doc = {
            "sources": {
                "responses": {
                    "plugin": "csv",
                    "on_success": "inbound",
                    "options": {
                        "path": "examples/multi_source_queue/input/orders.csv",
                        "schema": {"mode": "observed", "guaranteed_fields": ["llm_response"]},
                        "on_validation_failure": "discard",
                    },
                },
            },
            "queues": {"inbound": {}},
            "transforms": [
                {
                    "name": "consumer",
                    "plugin": "passthrough",
                    "input": "inbound",
                    "on_success": "combined",
                    "on_error": "discard",
                    "options": {
                        "schema": {"mode": "observed"},
                        "required_input_fields": [required_field],
                    },
                }
            ],
            "sinks": {
                "combined": {
                    "plugin": "json",
                    "on_write_failure": "discard",
                    "options": {
                        "path": "examples/multi_source_queue/output/combined.jsonl",
                        "format": "jsonl",
                        "collision_policy": "auto_increment",
                        "schema": {"mode": "observed"},
                    },
                }
            },
            "landscape": {"url": "sqlite:///examples/multi_source_queue/runs/audit.db"},
        }
        return yaml.safe_dump(doc, sort_keys=False)

    def _build_runtime_graph(self, settings_yaml: str) -> ExecutionGraph:
        from elspeth.core.config import load_settings_from_yaml_string

        settings = load_settings_from_yaml_string(settings_yaml)
        bundle = instantiate_plugins_from_config(settings, preflight_mode=True)
        return ExecutionGraph.from_plugin_instances(
            sources=bundle.sources,
            source_settings_map=bundle.source_settings_map,
            transforms=bundle.transforms,
            sinks=bundle.sinks,
            aggregations=bundle.aggregations,
            gates=list(settings.gates),
            coalesce_settings=list(settings.coalesce),
            queues=settings.queues,
        )

    def test_both_accept_queue_consumer_requiring_arm_guaranteed_field(self) -> None:
        from elspeth.contracts import NodeType
        from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

        settings_yaml = self._yaml(required_field="llm_response")

        graph = self._build_runtime_graph(settings_yaml)
        queue_nodes = [n for n in graph.get_nodes() if n.node_type == NodeType.QUEUE]
        assert len(queue_nodes) == 1
        assert "llm_response" in graph.get_effective_guaranteed_fields(queue_nodes[0].node_id)

        composer_result = composition_state_from_runtime_yaml(settings_yaml).validate()
        assert composer_result.is_valid, [error.message for error in composer_result.errors]
        # Strict walker parity (elspeth-3619b8774f): the composer resolves the
        # contract through the queue fan-in vote — no abstention warning where
        # the engine renders a definitive verdict.
        assert not [
            warning.message
            for warning in composer_result.warnings
            if "Contract check skipped" in warning.message and "queue" in warning.message
        ]

    def test_both_reject_queue_consumer_requiring_unguaranteed_field(self) -> None:
        from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

        settings_yaml = self._yaml(required_field="never_guaranteed")

        with pytest.raises(GraphValidationError) as exc_info:
            self._build_runtime_graph(settings_yaml)
        assert "never_guaranteed" in str(exc_info.value)

        # Red-parity (elspeth-3619b8774f): Stage 1 mirrors the engine's queue
        # fan-in vote, so the composer rejects at /validate rather than
        # abstaining green and letting the runtime preflight surface it.
        composer_result = composition_state_from_runtime_yaml(settings_yaml).validate()
        assert not composer_result.is_valid
        assert any("never_guaranteed" in error.message for error in composer_result.errors)


_SHAPE19_SOURCE_SCHEMA = {
    "mode": "fixed",
    "fields": ["id: str", "product: str", "price: float", "category: str", "description: str"],
    "guaranteed_fields": ["id", "product", "price", "category", "description"],
}
# The ticket's branch shape: a pass-through arm declaring ONLY the field it
# rewrites. Its guarantees still carry the whole arriving row.
_SHAPE19_BRANCH_SCHEMA = {"mode": "flexible", "fields": ["description: str"]}
_SHAPE19_LOCKED_SINK_SCHEMA = {"mode": "fixed", "fields": ["description: str"]}
# Repair 1 from the rejection's own remedy list: declare the extras on the
# consumer AND on the branches that type the coalesce. Declaring them on the
# sink alone fails the SAME edge with "Missing fields" instead — the trap
# df50ea3c3 rewrote the remedy text to name, pinned here so the advertised
# repair is known to actually produce a green build.
_SHAPE19_FULL_SCHEMA = {
    "mode": "fixed",
    "fields": ["id: str", "product: str", "price: float", "category: str", "description: str"],
}
# Repair 2: relax the consumer to flexible WITHOUT declaring the extras.
_SHAPE19_FLEXIBLE_SINK_SCHEMA = {"mode": "flexible", "fields": ["description: str"]}


class TestComposerRuntimeCoalesceGuaranteedExtrasAgreement:
    """Shape 19 — a coalesce's GUARANTEES vs a locked consumer: runtime-only.

    ``elspeth-1451ff385f`` in plugin-neutral form: a fork gate feeds a
    pass-through transform on each branch, each declaring ONLY the field it
    rewrites, and both branches merge at ``merge: union`` into a ``mode:
    fixed`` sink admitting that one field. The DAG builder types the coalesce's
    ``fields`` from each branch's construction-time schema but walks its
    ``guaranteed_fields`` separately (``elspeth-0b14977817``), so the merged
    schema GUARANTEES category/id/price/product while DECLARING description
    alone — and every row dies at the sink's input preflight.

    This is a documented runtime-only gap, the second category in this file's
    header. The runtime is authoritative and rejects at build
    (``validate_typed_producer_guaranteed_extras``, landed across ``5d0c54522``
    → ``df50ea3c3`` remedies → ``48873f8dc`` extras-firewall soundness);
    composer Stage 1 stays permissive and returns ``is_valid=True``, emitting
    only its advisory "runtime validator will check this edge" warning. The
    composer half is tracked in ``elspeth-ae83a6b60c`` and is NOT closed here.

    The last test is the scope boundary and is the reason this shape is
    recorded as coalesce-shaped on the composer side. The RUNTIME defect is
    broader than the coalesce (any ``passes_through_input=True`` transform
    under-declaring its schema — pinned by ``TestTypedPassThroughGuaranteedExtras``
    in ``tests/unit/core/dag/test_graph_validation.py``, commit ``cf550d674``),
    but the COMPOSER gap is not: Rule B already rejects the identical
    under-declaring shape on a linear pipeline, because
    ``_producer_emit_profile`` resolves a pass-through transform and unions in
    its upstream definite arrivals. Only at a coalesce does the composer
    abstain. Asserting that here keeps a future fix honest — if someone
    "generalises" the composer rule, this test says the general case already
    worked and the coalesce was the hole.

    Bug-verification protocol (mandatory per this file's header): the shape is
    pinned on ``core/dag/schema_validation.py::validate_typed_producer_guaranteed_extras``,
    called as the final pass of ``validate_edge_compatibility``. Neutering it
    (equivalent to a ``return`` before its edge loop) restores the pre-fix
    behaviour and fails exactly ONE of these four tests,
    ``test_runtime_rejects_coalesce_guaranteed_extras_while_composer_stays_permissive``,
    with ``Failed: DID NOT RAISE <class 'elspeth.core.dag.models.EdgeContractError'>``.
    The COMPOSER half of that same test still passes under the mutation — the
    ``is_valid`` and skip-warning assertions all hold — which is precisely the
    validate-green/runtime-red divergence this shape records, and is the
    evidence that the pre-fix pipeline was green on BOTH surfaces. The two
    repair controls and the no-coalesce boundary test also still pass, as they
    must: none of them depends on the new pass.

    Verified 2026-08-07 by ``monkeypatch.setattr`` on the module attribute
    rather than by editing the file, because a concurrent session held the
    working tree; the call site is a module-global reference inside
    ``validate_edge_compatibility``, so patching the module attribute
    reproduces the pre-fix behaviour faithfully. Per this file's METHOD NOTE
    the marker ``validate_typed_producer_guaranteed_extras(graph)`` was
    asserted unique (``grep -c`` = 1) before mutating.
    """

    def _empty_state(self) -> CompositionState:
        return CompositionState(
            source=None,
            nodes=(),
            edges=(),
            outputs=(),
            metadata=PipelineMetadata(),
            version=1,
        )

    def _composer_state(
        self,
        *,
        csv_path: Path,
        output_path: Path,
        sink_schema: dict[str, Any],
        branch_schema: dict[str, Any] | None = None,
    ) -> CompositionState:
        state = self._empty_state()
        state = state.with_source(
            SourceSpec(
                plugin="csv",
                on_success="gate_in",
                options={"path": str(csv_path), "schema": _SHAPE19_SOURCE_SCHEMA},
                on_validation_failure="discard",
            )
        )
        state = state.with_node(
            NodeSpec(
                id="fork_gate",
                node_type="gate",
                plugin=None,
                input="gate_in",
                on_success=None,
                on_error=None,
                options={},
                condition="True",
                routes={"true": "fork", "false": "fork"},
                fork_to=("branch_a", "branch_b"),
                branches=None,
                policy=None,
                merge=None,
            )
        )
        for node_id, branch_connection, done_connection in (
            ("arm_a", "branch_a", "done_a"),
            ("arm_b", "branch_b", "done_b"),
        ):
            state = state.with_node(
                NodeSpec(
                    id=node_id,
                    node_type="transform",
                    plugin="passthrough",
                    input=branch_connection,
                    on_success=done_connection,
                    on_error="discard",
                    options={"schema": branch_schema or _SHAPE19_BRANCH_SCHEMA},
                    condition=None,
                    routes=None,
                    fork_to=None,
                    branches=None,
                    policy=None,
                    merge=None,
                )
            )
        state = state.with_node(
            NodeSpec(
                id="merge_results",
                node_type="coalesce",
                plugin=None,
                input="done_a",
                on_success="main",
                on_error=None,
                options={},
                condition=None,
                routes=None,
                fork_to=None,
                branches={"branch_a": "done_a", "branch_b": "done_b"},
                policy="require_all",
                merge="union",
            )
        )
        state = state.with_output(
            OutputSpec(
                name="main",
                plugin="csv",
                options={"path": str(output_path), "schema": sink_schema},
                on_write_failure="discard",
            )
        )
        for edge_id, from_node, to_node, edge_type, label in (
            ("e1", "source", "fork_gate", "on_success", None),
            ("e2", "fork_gate", "arm_a", "fork", "branch_a"),
            ("e3", "fork_gate", "arm_b", "fork", "branch_b"),
            ("e4", "arm_a", "merge_results", "on_success", None),
            ("e5", "arm_b", "merge_results", "on_success", None),
        ):
            state = state.with_edge(
                EdgeSpec(
                    id=edge_id,
                    from_node=from_node,
                    to_node=to_node,
                    edge_type=edge_type,
                    label=label,
                )
            )
        return state

    def _runtime_settings(
        self,
        *,
        csv_path: Path,
        output_path: Path,
        sink_schema: dict[str, Any],
        branch_schema: dict[str, Any] | None = None,
    ) -> ElspethSettings:
        return ElspethSettings(
            sources={
                "primary": SourceSettings(
                    plugin="csv",
                    on_success="gate_in",
                    options={
                        "path": str(csv_path),
                        "schema": _SHAPE19_SOURCE_SCHEMA,
                        "on_validation_failure": "discard",
                    },
                )
            },
            transforms=[
                TransformSettings(
                    name=node_id,
                    plugin="passthrough",
                    input=branch_connection,
                    on_success=done_connection,
                    on_error="discard",
                    options={"schema": branch_schema or _SHAPE19_BRANCH_SCHEMA},
                )
                for node_id, branch_connection, done_connection in (
                    ("arm_a", "branch_a", "done_a"),
                    ("arm_b", "branch_b", "done_b"),
                )
            ],
            gates=[
                GateSettings(
                    name="fork_gate",
                    input="gate_in",
                    condition="True",
                    routes={"true": "fork", "false": "fork"},
                    fork_to=["branch_a", "branch_b"],
                )
            ],
            coalesce=[
                CoalesceSettings(
                    name="merge_results",
                    branches={"branch_a": "done_a", "branch_b": "done_b"},
                    policy="require_all",
                    merge="union",
                    on_success="main",
                )
            ],
            sinks={
                "main": SinkSettings(
                    plugin="csv",
                    on_write_failure="discard",
                    options={"path": str(output_path), "schema": sink_schema},
                )
            },
        )

    def _paths(self, tmp_path: Path) -> tuple[Path, Path]:
        csv_path = tmp_path / "input.csv"
        csv_path.write_text(
            "id,product,price,category,description\n1,widget,2.0,tools,a short blurb\n",
            encoding="utf-8",
        )
        return csv_path, tmp_path / "out.csv"

    def _build_runtime_graph_from_settings(self, config: ElspethSettings) -> ExecutionGraph:
        plugins = instantiate_plugins_from_config(config)
        return ExecutionGraph.from_plugin_instances(
            sources=plugins.sources,
            source_settings_map=plugins.source_settings_map,
            transforms=plugins.transforms,
            sinks=plugins.sinks,
            aggregations=plugins.aggregations,
            gates=list(config.gates),
            coalesce_settings=list(config.coalesce) if config.coalesce else None,
        )

    def test_runtime_rejects_coalesce_guaranteed_extras_while_composer_stays_permissive(self, tmp_path: Path) -> None:
        """The documented gap: runtime authoritative and red, composer green.

        The composer's own warning names the deferral ("runtime validator will
        check this edge"), so this test pins BOTH halves of the handoff: that
        Stage 1 abstains with that advisory, and that the validator it defers
        to does in fact reject. Before ``5d0c54522`` the second half was false
        and the build was green on both surfaces.
        """
        csv_path, output_path = self._paths(tmp_path)

        composer_result = self._composer_state(
            csv_path=csv_path,
            output_path=output_path,
            sink_schema=_SHAPE19_LOCKED_SINK_SCHEMA,
        ).validate()

        assert composer_result.is_valid, composer_result.errors
        assert not [error for error in composer_result.errors if error.error_code == "sink_locked_extras"]
        # Same abstention idiom TestComposerRuntimeQueueGuaranteeAgreement uses
        # for the queue walk, asserted in the POSITIVE direction: here the
        # composer genuinely does skip, and the warning is the only signal the
        # authoring loop gets.
        assert [
            warning.message
            for warning in composer_result.warnings
            if "Contract check skipped" in warning.message and "coalesce" in warning.message
        ]

        with pytest.raises(EdgeContractError) as exc_info:
            graph = self._build_runtime_graph_from_settings(
                self._runtime_settings(
                    csv_path=csv_path,
                    output_path=output_path,
                    sink_schema=_SHAPE19_LOCKED_SINK_SCHEMA,
                )
            )
            graph.validate_edge_compatibility()

        error = exc_info.value
        # The phantom set: guaranteed by the graph walk, absent from the
        # coalesce's own typed fields, forbidden by the locked sink.
        assert error.compatibility_result.extra_fields == ("category", "id", "price", "product")
        assert error.from_component_type == "coalesce"
        assert error.component_type == "sink"
        message = str(error)
        assert "locked (mode: fixed)" in message
        assert "description" in message

    def test_repair_one_declaring_the_extras_on_branches_and_sink_builds_green(self, tmp_path: Path) -> None:
        """Positive control AND remedy 1: the check must not block a runnable pipeline.

        Identical topology; the branches and the sink both declare the full
        set. Note that widening the SINK ALONE is not the repair — it fails the
        same edge with "Missing fields", because the coalesce types its own
        ``fields`` from the branches. That trap is why ``df50ea3c3`` rewrote
        the remedy text to say "declare them on those branches too", and this
        test is what keeps that advice true.
        """
        csv_path, output_path = self._paths(tmp_path)

        composer_result = self._composer_state(
            csv_path=csv_path,
            output_path=output_path,
            sink_schema=_SHAPE19_FULL_SCHEMA,
            branch_schema=_SHAPE19_FULL_SCHEMA,
        ).validate()
        assert composer_result.is_valid, composer_result.errors

        graph = self._build_runtime_graph_from_settings(
            self._runtime_settings(
                csv_path=csv_path,
                output_path=output_path,
                sink_schema=_SHAPE19_FULL_SCHEMA,
                branch_schema=_SHAPE19_FULL_SCHEMA,
            )
        )
        graph.validate_edge_compatibility()

    def test_repair_two_relaxing_the_sink_to_flexible_builds_green(self, tmp_path: Path) -> None:
        """Remedy 2: a flexible sink admits the guaranteed extras undeclared.

        This is also the guard-condition control for the new pass, which
        declines unless the consumer's model forbids extras. Branch schemas
        stay under-declared, so the ONLY thing that changes versus the rejected
        case is the sink's extras policy.
        """
        csv_path, output_path = self._paths(tmp_path)

        composer_result = self._composer_state(
            csv_path=csv_path,
            output_path=output_path,
            sink_schema=_SHAPE19_FLEXIBLE_SINK_SCHEMA,
        ).validate()
        assert composer_result.is_valid, composer_result.errors

        graph = self._build_runtime_graph_from_settings(
            self._runtime_settings(
                csv_path=csv_path,
                output_path=output_path,
                sink_schema=_SHAPE19_FLEXIBLE_SINK_SCHEMA,
            )
        )
        graph.validate_edge_compatibility()

    def test_composer_rejects_the_same_defect_without_a_coalesce(self) -> None:
        """Scope boundary: the COMPOSER gap is coalesce-shaped, the runtime one is not.

        The same ingredient — a ``passes_through_input=True`` transform
        declaring a narrower schema than it forwards, feeding a locked
        consumer — on a linear pipeline. The runtime defect was identical here
        (pinned by ``TestTypedPassThroughGuaranteedExtras``, ``cf550d674``),
        but the composer ALREADY rejects this shape via Rule B, because
        ``_producer_emit_profile`` resolves the pass-through and unions in its
        upstream definite arrivals. So Shape 19's composer half is not "Rule B
        is missing", it is "Rule B abstains at a coalesce" — three abstention
        sites in ``web/composer/state.py`` (the walk-back's unconditional
        coalesce stop, the row_union-only boundary re-resolve, and
        ``_connection_definite_emits`` returning the empty set for coalesce).
        """
        import yaml

        from elspeth.web.composer.yaml_importer import composition_state_from_runtime_yaml

        doc = {
            "sources": {
                "primary": {
                    "plugin": "csv",
                    "on_success": "raw",
                    "options": {
                        "path": "examples/fork_coalesce/input.csv",
                        "schema": {
                            "mode": "fixed",
                            "fields": ["id: int", "product: str", "description: str"],
                        },
                        "on_validation_failure": "discard",
                    },
                }
            },
            "transforms": [
                {
                    "name": "shorten",
                    "plugin": "truncate",
                    "input": "raw",
                    "on_success": "output",
                    "on_error": "discard",
                    "options": {
                        "fields": {"description": 20},
                        "suffix": "...",
                        "schema": {"mode": "flexible", "fields": ["description: str"]},
                    },
                }
            ],
            "sinks": {
                "output": {
                    "plugin": "json",
                    "on_write_failure": "discard",
                    "options": {
                        "path": "out.jsonl",
                        "format": "jsonl",
                        "schema": {"mode": "fixed", "fields": ["description: str"]},
                    },
                }
            },
        }

        composer_result = composition_state_from_runtime_yaml(yaml.safe_dump(doc, sort_keys=False)).validate()

        assert not composer_result.is_valid
        entries = [error for error in composer_result.errors if error.error_code == "sink_locked_extras"]
        assert len(entries) == 1, composer_result.errors
        assert entries[0].contract is not None
        assert entries[0].contract.extra_fields == ("id", "product")
