"""TokenManager: High-level token operations for the SDA engine.

Provides a simplified interface over DataFlowRepository for managing
tokens (row instances flowing through the DAG).
"""

from __future__ import annotations

__all__ = ["TokenInfo", "TokenManager"]

import copy
from collections.abc import Sequence
from typing import Any

from elspeth.contracts import AggregationParentDisposition, CoalesceParentCompletion, SourceRow, TokenInfo
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.coordination import CoordinationToken
from elspeth.contracts.enums import FrameKind, TerminalPath
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.identity import LineageFrame, innermost_own_frame, truncate_at_closer_frame
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.contracts.types import NodeID, StepResolver
from elspeth.core.dag.group_bindings import GroupBinding, GroupBindingRegistry
from elspeth.core.landscape.data_flow_repository import DataFlowRepository


class TokenManager:
    """Manages token lifecycle for the SDA engine.

    Provides high-level operations:
    - Create initial token from source row
    - Fork token to multiple branches
    - Coalesce tokens from branches
    - Update token row data after transforms

    Example:
        manager = TokenManager(recorder, step_resolver=graph.resolve_step)

        # Create token for source row
        token = manager.create_initial_token(
            run_id=run.run_id,
            source_node_id=source.node_id,
            row_index=0,
            source_row_index=0,
            ingest_sequence=0,
            source_row=source_row,
        )

        # After transform
        token = token.with_updated_data(PipelineRow({"value": 42, "processed": True}))

        # Fork to branches (node_id resolved to step internally)
        children = manager.fork_token(
            parent_token=token,
            branches=["stats", "classifier"],
            node_id=NodeID("gate_classifier_abc123"),
        )
    """

    def __init__(
        self,
        data_flow: DataFlowRepository,
        *,
        step_resolver: StepResolver,
        group_bindings: GroupBindingRegistry | None = None,
    ) -> None:
        """Initialize with data flow repository and step resolver.

        Args:
            data_flow: DataFlowRepository for audit trail
            step_resolver: Callable that resolves NodeID to 1-indexed audit step position.
                The canonical implementation is RowProcessor._resolve_audit_step_for_node.
            group_bindings: The unified FORK/EXPAND group-binding registry (barrier-scopes
                spec §3). WS3's mint-path call site (spec §4.2, ``graph.py:883``'s freeze
                note): ``expand_token`` registers each runtime-minted EXPAND group id on
                this SAME registry instance — attempted at every mint, pre-filtered via
                the cached ``by_opener_node()`` index (below), so a declared scope
                opener's node_id calls ``register_expand_group`` and an undeclared
                expand's node_id simply is not one of its keys and never calls it at
                all. None (e.g. CoalesceExecutor's own internal TokenManager, which
                never calls ``expand_token``) disables registration entirely;
                ``by_opener_node()`` is resolved ONCE here, not per mint — ``bindings``
                is build-time immutable, only the registry's own ``_expand_groups``
                index mutates.
        """
        self._data_flow = data_flow
        self._step_resolver = step_resolver
        self._group_bindings = group_bindings
        self._opener_binding_by_node_id: dict[NodeID, GroupBinding] = group_bindings.by_opener_node() if group_bindings is not None else {}
        # META-38: per-(run_id, group_id) memo of the durable release fact.
        # Populated ONLY by is_release_group's durable read — never seeded
        # from a CommittedCollect at mint time, which would give the minting
        # leader an answer a follower/resumed process could not reproduce
        # (the asymmetry this fact exists to remove). A group's release-ness
        # is immutable once minted, so the memo never invalidates.
        self._release_group_memo: dict[tuple[str, str], bool] = {}

    def is_release_group(self, run_id: str, group_id: str) -> bool:
        """Whether ``group_id`` is a collector RELEASE group (META-38 written fact).

        One durable read per (run, group) per process, through
        ``DataFlowRepository.is_release_group``; the repository raises
        ``AuditIntegrityError`` for a group with no ``group_records`` row
        (fail closed), and that raise is NOT memoised.
        """
        key = (run_id, group_id)
        if key not in self._release_group_memo:
            self._release_group_memo[key] = self._data_flow.is_release_group(run_id=run_id, group_id=group_id)
        return self._release_group_memo[key]

    def create_initial_token(
        self,
        run_id: str,
        source_node_id: str,
        row_index: int,
        source_row: SourceRow,
        *,
        source_row_index: int,
        ingest_sequence: int,
        row_id: str | None = None,
        token_id: str | None = None,
        coordination_token: CoordinationToken | None = None,
    ) -> TokenInfo:
        """Create a token for a source row.

        Args:
            run_id: Run identifier
            source_node_id: Source node that loaded the row
            row_index: Position in source (0-indexed)
            source_row: SourceRow from source (must have contract)
            row_id: Optional pre-minted row identity (the processor's fenced
                ingest path pre-mints ids so boundary checks can run BEFORE
                any durable write)
            token_id: Optional pre-minted token identity (see ``row_id``)
            coordination_token: Leader fencing token (ADR-030 §C.4 row 9) —
                threaded into ``create_row_with_token`` so this ingest-
                adjacent ``rows`` write is epoch-fenced. The happy path of
                ``RowProcessor.process_row`` does NOT come through here (it
                composes the fenced ``ingest_row_with_initial_claim``); this
                arm serves boundary-failure recording and direct callers.

        Returns:
            TokenInfo with row and token IDs, row_data as PipelineRow

        Raises:
            ValueError: If source_row has no contract

        Note:
            Payload persistence is now handled by DataFlowRepository.create_row(),
            not by TokenManager. This ensures Landscape owns its audit format.
        """
        # Guard: source must provide contract
        if source_row.contract is None:
            raise OrchestrationInvariantError(
                "SourceRow must have contract to create token. Source plugins must set contract on all valid rows."
            )

        # Convert to PipelineRow
        pipeline_row = source_row.to_pipeline_row()

        # Create row record and initial token atomically; the repository owns
        # run/source identity and payload persistence for both audit rows.
        row, token = self._data_flow.create_row_with_token(
            run_id=run_id,
            source_node_id=source_node_id,
            row_index=row_index,
            source_row_index=source_row_index,
            ingest_sequence=ingest_sequence,
            data=pipeline_row.to_dict(),
            row_id=row_id,
            token_id=token_id,
            coordination_token=coordination_token,
        )

        return TokenInfo(
            row_id=row.row_id,
            token_id=token.token_id,
            row_data=pipeline_row,
        )

    def create_quarantine_token(
        self,
        run_id: str,
        source_node_id: str,
        row_index: int,
        source_row: SourceRow,
        *,
        source_row_index: int,
        ingest_sequence: int,
        validation_error_id: str | None = None,
        coordination_token: CoordinationToken | None = None,
    ) -> TokenInfo:
        """Create a token for a quarantined row.

        Quarantined rows are invalid data that failed source validation.
        They don't have contracts (SourceRow.quarantined sets contract=None).
        They are routed directly to a quarantine sink for investigation.

        Creates a minimal PipelineRow with an empty OBSERVED contract for audit
        trail consistency, but the data is not validated or transformed.

        Args:
            run_id: Run identifier
            source_node_id: Source node that loaded the row
            row_index: Position in source (0-indexed)
            source_row: Quarantined SourceRow (contract=None is expected)

        Returns:
            TokenInfo with row and token IDs

        Raises:
            OrchestrationInvariantError: If source_row is not quarantined
        """
        if not source_row.is_quarantined:
            raise OrchestrationInvariantError("create_quarantine_token requires a quarantined SourceRow")

        # For quarantine rows, row may not be a dict (could be malformed external data).
        # PipelineRow requires exactly builtin dict (type(data) is dict), so normalize
        # dict subclasses (OrderedDict, etc.) to plain dict for the audit trail.
        if isinstance(source_row.row, dict):
            row_data: dict[str, Any] = dict(source_row.row) if type(source_row.row) is not dict else source_row.row
        else:
            row_data = {"_raw": source_row.row}

        # Create minimal OBSERVED contract for audit consistency
        # Quarantine rows don't go through transforms, but audit trail needs a contract
        from elspeth.contracts.schema_contract import SchemaContract

        quarantine_contract = SchemaContract(
            mode="OBSERVED",
            fields=(),  # Empty - no declared fields
            locked=False,  # Not locked - quarantine doesn't validate types
        )

        # Create PipelineRow with minimal contract
        pipeline_row = PipelineRow(row_data, quarantine_contract)

        # Create the row record, initial token, and optional validation-error
        # association in ONE transaction —
        # epoch-fenced when a coordination token is threaded (ADR-030 §C.4
        # row 9: the quarantine arm is an ingest-adjacent durable rows write
        # at sequence N; historically this was TWO separate transactions).
        # quarantined=True enables safe hashing for Tier-3 external data that
        # may contain non-canonical values (NaN, Infinity).
        row, token = self._data_flow.create_quarantine_row_with_token(
            run_id=run_id,
            source_node_id=source_node_id,
            row_index=row_index,
            source_row_index=source_row_index,
            ingest_sequence=ingest_sequence,
            data=pipeline_row.to_dict(),
            validation_error_id=validation_error_id,
            coordination_token=coordination_token,
        )

        return TokenInfo(
            row_id=row.row_id,
            token_id=token.token_id,
            row_data=pipeline_row,
        )

    def create_token_for_existing_row(
        self,
        row_id: str,
        row_data: PipelineRow,
    ) -> TokenInfo:
        """Create a token for a row that already exists in the database.

        Used during resume when rows were created in the original run
        but tokens need to be created for reprocessing.

        Args:
            row_id: Existing row ID in the database
            row_data: Row data as PipelineRow (reconstructed from checkpoint)

        Returns:
            TokenInfo with row and token IDs
        """
        # Create token for existing row
        token = self._data_flow.create_token(row_id=row_id)

        return TokenInfo(
            row_id=row_id,
            token_id=token.token_id,
            row_data=row_data,
        )

    def fork_token(
        self,
        parent_token: TokenInfo,
        branches: list[str],
        node_id: NodeID,
        run_id: str,
        row_data: PipelineRow | None = None,
    ) -> tuple[list[TokenInfo], str]:
        """Fork a token to multiple branches.

        ATOMIC: Creates children AND records parent FORKED outcome in single transaction.

        Args:
            parent_token: Parent token to fork
            branches: List of branch names
            node_id: NodeID of the gate/transform performing the fork (resolved to
                audit step position internally via step_resolver)
            run_id: Run ID (required for atomic outcome recording)
            row_data: Optional PipelineRow (defaults to parent's data)

        Returns:
            Tuple of (child TokenInfo list, fork_group_id)

        Note:
            Contract is propagated from row_data to all children via deepcopy.
            PipelineRow.__deepcopy__ preserves contract reference (immutable).
        """
        data = row_data if row_data is not None else parent_token.row_data
        step = self._step_resolver(node_id)

        children, fork_group_id = self._data_flow.fork_token(
            parent_ref=TokenRef(token_id=parent_token.token_id, run_id=run_id),
            row_id=parent_token.row_id,
            branches=branches,
            step_in_pipeline=step,
            parent_lineage_path=parent_token.lineage_path,
        )

        # CRITICAL: Use deepcopy to prevent nested mutable objects from being
        # shared across forked children. Shallow copy would cause mutations in
        # one branch to leak to siblings, breaking audit trail integrity.
        #
        # resume_attempt_offset and resume_checkpoint_id are intentionally NOT
        # inherited here. Fork children mint new token_ids with no run-1 node_states,
        # so attempt=0 is correct and they must not inherit the parent's resume offset.
        child_infos: list[TokenInfo] = []
        for child in children:
            innermost = child.lineage_path[-1] if child.lineage_path else None
            if innermost is None or innermost.kind is not FrameKind.FORK:
                # The durable writer always stacks a FORK frame for every fork
                # child (data_flow.fork_token); a missing one here is audit
                # corruption, not a config shape.
                raise OrchestrationInvariantError(
                    f"fork_token: child token {child.token_id!r} minted without an innermost FORK frame "
                    f"— every fork child must carry its branch name (lineage_path={child.lineage_path!r})"
                )
            child_infos.append(
                TokenInfo(
                    row_id=parent_token.row_id,
                    token_id=child.token_id,
                    row_data=copy.deepcopy(data),
                    lineage_path=child.lineage_path,
                )
            )
        return child_infos, fork_group_id

    def coalesce_tokens(
        self,
        parents: list[TokenInfo],
        merged_data: PipelineRow,
        node_id: NodeID,
        run_id: str,
        parent_completions: Sequence[CoalesceParentCompletion] = (),
    ) -> tuple[TokenInfo, str]:
        """Coalesce multiple tokens into one.

        Args:
            parents: Parent tokens to merge
            merged_data: Merged row data as PipelineRow (with merged contract)
            node_id: NodeID of the coalesce node performing the merge (resolved to
                audit step position internally via step_resolver)
            run_id: Run ID for constructing TokenRefs

        Returns:
            Tuple of (merged TokenInfo with PipelineRow row_data, join_group_id)
        """
        if not parents:
            raise OrchestrationInvariantError("coalesce_tokens requires at least one parent token")

        row_id = parents[0].row_id
        mismatched = [p.token_id for p in parents if p.row_id != row_id]
        if mismatched:
            raise OrchestrationInvariantError(
                f"coalesce_tokens requires all parents to share row_id={row_id}; mismatched token_ids={mismatched}"
            )

        step = self._step_resolver(node_id)

        # Guarded truncation (rulings 24/28 as amended by META-38) via
        # contracts.identity.truncate_at_closer_frame: a closer closes
        # exactly its own FORK frame — the anchor is the first parent's
        # innermost FORK frame, which a collector release inside the branch
        # carries BELOW its own release-group EXPAND frame. Truncation runs
        # PER PARENT, passing only release-group frames (the written
        # group_records.closes_group_id fact, memoised on this manager); any
        # other frame above the FORK frame raises. The cross-parent
        # remaining-path equality check is load-bearing: it is what catches
        # parents that disagree about their enclosing scope after each has
        # been truncated independently.
        # Anchor (amendment 1 B): the first parent's OWN frame via the guarded
        # walk — never innermost_fork_frame, which would skip an unreleased
        # scope's EXPAND frame, exactly the shape the guard must reject.
        own = innermost_own_frame(parents[0].lineage_path, is_release_group=lambda gid: self.is_release_group(run_id, gid))
        if own is None or own[1].kind is not FrameKind.FORK:
            raise OrchestrationInvariantError(
                f"coalesce_tokens: parent token {parents[0].token_id!r} has no innermost FORK frame to close "
                f"(searched below collector release-group frames; lineage_path={parents[0].lineage_path!r})"
            )
        shared_group_id = own[1].group_id
        remaining_paths = {
            truncate_at_closer_frame(
                parent.lineage_path,
                kind=FrameKind.FORK,
                group_id=shared_group_id,
                is_release_group=lambda gid: self.is_release_group(run_id, gid),
            )
            for parent in parents
        }
        if len(remaining_paths) != 1:
            raise OrchestrationInvariantError("coalesce_tokens: parents do not share their remaining lineage path after the pop")
        merged_path = remaining_paths.pop()

        # Pass the merged row dict and its contract so the envelope is persisted
        # atomically with the coalesced token INSERT (epoch 11: token_data_ref).
        # merged_data is a PipelineRow; .to_dict() is mandated over dict(row).
        # merged_contract = merged_data.contract — the contract the PipelineRow carries
        # (set by the coalesce executor after merging the branch contracts).
        merged = self._data_flow.coalesce_tokens(
            parent_refs=[TokenRef(token_id=p.token_id, run_id=run_id) for p in parents],
            row_id=row_id,
            coalesce_node_id=str(node_id),
            parent_state_ids=[item.state_id for item in parent_completions] or None,
            merged_payload=merged_data.to_dict(),
            merged_contract=merged_data.contract,
            step_in_pipeline=step,
            parent_lineage_paths={p.token_id: p.lineage_path for p in parents},
        )
        if parent_completions:
            self._data_flow.finalize_coalesce_effect(
                merged=merged,
                parent_completions=parent_completions,
            )

        # resume_attempt_offset and resume_checkpoint_id are intentionally NOT
        # inherited here. The merged token is a brand-new token_id with no run-1
        # node_states, so attempt=0 is correct and it must not inherit the parent
        # branches' resume offsets. (The branch tokens' coalesce node_states already
        # carry the provenance marker for the arriving tokens.)
        if merged.join_group_id is None:
            raise OrchestrationInvariantError(
                f"coalesce_tokens: merged token {merged.token_id!r} has no join_group_id — the durable coalesce writer always mints one"
            )
        merged_info = TokenInfo(
            row_id=row_id,
            token_id=merged.token_id,
            row_data=merged_data,
            lineage_path=merged_path,
        )
        return merged_info, merged.join_group_id

    def collect_tokens(
        self,
        members: Sequence[TokenInfo],
        output_rows: Sequence[PipelineRow],
        node_id: NodeID,
        run_id: str,
        group_id: str,
    ) -> tuple[TokenInfo, ...]:
        """Close a bound EXPAND group: strict-pop the closer's frame, mint outputs.

        spec §4.2 (ruling 24 as amended by 28 and META-38): every member
        carries the closer's own EXPAND frame — innermost, or below its own
        collector release-group frame(s) when the member is itself a
        collector release (collector-in-collector) — and all members share
        the remaining path; §7 rule 5 makes any other shape a genuine engine
        invariant. The truncation is the SHARED ``truncate_at_closer_frame``
        (contracts/identity.py): it raises OrchestrationInvariantError unless
        a frame matches kind+group_id (empty paths included) and every frame
        above it is a release group (the written ``closes_group_id`` fact,
        memoised on this manager).
        The emission is RATIFIED (2026-08-22 synthesis): the aggregation-flush
        precedent — outputs form a fresh EXPAND group over the popped base
        path (inert unless bound). An empty ``output_rows`` (M=0) still mints
        a durable, idempotent empty release group (fix-round ruling 1, spec
        §4.3/§5) — only the engine-visible return is trivially ``()``.
        """
        if not members:
            raise OrchestrationInvariantError("collect_tokens requires at least one member token")
        # truncate_at_closer_frame owns the frame validation (kind, group_id,
        # non-empty path, only release-group frames above); this method adds
        # only the cross-member consistency check, PER MEMBER after each
        # member's own truncation.
        base_path = truncate_at_closer_frame(
            members[0].lineage_path,
            kind=FrameKind.EXPAND,
            group_id=group_id,
            is_release_group=lambda gid: self.is_release_group(run_id, gid),
        )
        for member in members[1:]:
            popped = truncate_at_closer_frame(
                member.lineage_path,
                kind=FrameKind.EXPAND,
                group_id=group_id,
                is_release_group=lambda gid: self.is_release_group(run_id, gid),
            )
            if popped != base_path:
                raise OrchestrationInvariantError(
                    f"collect_tokens: member {member.token_id} does not share the group's "
                    f"remaining path after the strict pop of EXPAND group {group_id!r} — "
                    f"{popped!r} != {base_path!r} (spec §4.2). Engine/validation bug."
                )
        # The durable half is called unconditionally, including M=0: spec
        # §4.3/§5 requires an empty release to leave the same durable
        # footprint a non-empty one does (fix-round ruling 1, overriding the
        # plan's "mint nothing" text). The engine-visible return stays ()
        # either way — ``committed.children`` is empty when output_rows is.
        step = self._step_resolver(node_id)
        committed = self._data_flow.collect_tokens(
            member_refs=[TokenRef(token_id=m.token_id, run_id=run_id) for m in members],
            group_id=group_id,
            collector_node_id=str(node_id),
            output_payloads=[row.to_dict() for row in output_rows],
            output_contracts=[row.contract for row in output_rows],
            step_in_pipeline=step,
            member_lineage_paths={m.token_id: m.lineage_path for m in members},
        )
        release_frames = tuple(
            (*base_path, LineageFrame(kind=FrameKind.EXPAND, group_id=committed.release_group_id, member_key=child.token_id))
            for child in committed.children
        )
        return tuple(
            TokenInfo(
                row_id=members[0].row_id,
                token_id=child.token_id,
                row_data=row,
                lineage_path=path,
            )
            for child, row, path in zip(committed.children, output_rows, release_frames, strict=True)
        )

    def expand_token(
        self,
        parent_token: TokenInfo,
        expanded_rows: list[dict[str, Any]],
        output_contract: SchemaContract,
        node_id: NodeID,
        run_id: str,
        parent_path: TerminalPath = TerminalPath.EXPAND_PARENT,
        parent_batch_id: str | None = None,
        aggregation_parent_dispositions: Sequence[AggregationParentDisposition] = (),
    ) -> tuple[list[TokenInfo], str]:
        """Create child tokens for deaggregation (1 input -> N outputs).

        ATOMIC: Creates children and records the parent's explicit terminal
        disposition in one transaction.

        Unlike fork_token (which creates parallel paths through the same DAG),
        expand_token creates sequential children that all continue down the
        same path. Used when a transform outputs multiple rows from single input.

        Args:
            parent_token: The token being expanded
            expanded_rows: List of output row dicts (transforms output dicts, not PipelineRow)
            output_contract: Contract for output rows (from TransformResult.contract)
            node_id: NodeID of the transform performing the expansion (resolved to
                audit step position internally via step_resolver)
            run_id: Run ID (required for atomic outcome recording)
            parent_path: EXPAND_PARENT for ordinary deaggregation or
                BATCH_CONSUMED for transform-mode aggregation.
            parent_batch_id: Required for BATCH_CONSUMED and forbidden for
                EXPAND_PARENT.

        Returns:
            Tuple of (child TokenInfo list, expand_group_id)

        Note:
            Expanded rows are dicts from transform output; we wrap them in PipelineRow
            with the output_contract (post-transform schema), not parent's contract.
        """
        # Guard - contract must be locked before any expansion side effects.
        # Expansion writes child tokens and may record parent EXPANDED outcome
        # atomically in the recorder; validate preconditions first.
        if not output_contract.locked:
            raise OrchestrationInvariantError(
                f"Output contract must be locked before token expansion. "
                f"Contract mode={output_contract.mode}, locked={output_contract.locked}"
            )

        # Delegate to recorder which handles DB operations and parent linking.
        # Pass expanded_rows as child_payloads and output_contract so each child's
        # {data, contract} envelope is persisted atomically with its token INSERT
        # (epoch 11: token_data_ref). output_contract is the locked contract from
        # TransformResult.contract, shared by all expanded children.
        # expanded_rows are already plain dicts (transform output) — no .to_dict() needed.
        step = self._step_resolver(node_id)
        db_children, expand_group_id = self._data_flow.expand_token(
            parent_ref=TokenRef(token_id=parent_token.token_id, run_id=run_id),
            row_id=parent_token.row_id,
            child_payloads=expanded_rows,
            output_contract=output_contract,
            step_in_pipeline=step,
            parent_path=parent_path,
            parent_batch_id=parent_batch_id,
            aggregation_parent_dispositions=aggregation_parent_dispositions,
            parent_lineage_path=parent_token.lineage_path,
        )

        # WS3 mint-path wiring (spec §4.2, graph.py:883's freeze note): register
        # this runtime-minted EXPAND group id on the group-binding registry.
        # Attempted at every expand_token call, regardless of caller (2026-08-24
        # review M5 correction: pre-filtered HERE via the cached
        # by_opener_node() index, not inside register_expand_group — node_id
        # is a declared scope opener only if it appears there; an ordinary
        # (non-scope) multi-row node_id is simply absent, and this is a no-op).
        if self._group_bindings is not None:
            opener_binding = self._opener_binding_by_node_id.get(node_id)
            if opener_binding is not None:
                self._group_bindings.register_expand_group(expand_group_id, opener_name=opener_binding.opener_name)

        # Use output_contract (post-transform schema) for all expanded children
        # This ensures downstream transforms can access newly added/renamed fields
        #
        # CRITICAL: Use deepcopy to prevent nested mutable objects from being
        # shared across expanded children. Same reasoning as fork_token - without
        # this, mutations in one sibling leak to others, corrupting audit trail.
        # Bug fix: expand_token was sharing row_data references across tokens
        #
        # resume_attempt_offset and resume_checkpoint_id are intentionally NOT
        # inherited here. Expand children mint new token_ids with no run-1 node_states,
        # so attempt=0 is correct and they must not inherit the parent's resume offset.
        child_infos = [
            TokenInfo(
                row_id=parent_token.row_id,
                token_id=db_child.token_id,
                # Create PipelineRow with output contract
                row_data=PipelineRow(copy.deepcopy(row_data), output_contract),
                # branch_name is inherited automatically: it derives from the
                # innermost FORK frame anywhere in lineage_path, and expand
                # only ever APPENDS an EXPAND frame, never touching it.
                lineage_path=db_child.lineage_path,
            )
            for db_child, row_data in zip(db_children, expanded_rows, strict=True)
        ]
        return child_infos, expand_group_id

    def record_empty_expansion(self, parent_token: TokenInfo, run_id: str) -> str:
        """Durable member_count=0 group record for a zero-row expansion (spec §4.3).

        Deliberately does NOT call `register_expand_group`: a zero-row
        expansion mints zero children, so no `lineage_path` anywhere in the
        system can ever carry this group_id's EXPAND frame — nothing can
        call `binding_for` on it. Registering would be inert bookkeeping.
        """
        return self._data_flow.record_empty_expansion(TokenRef(token_id=parent_token.token_id, run_id=run_id))

    # NOTE: Step resolution is handled by the injected StepResolver, which
    # maps NodeID → 1-indexed audit step position. The canonical implementation
    # is RowProcessor._resolve_audit_step_for_node. TokenManager resolves steps
    # internally — callers pass node_id, not step_in_pipeline.
