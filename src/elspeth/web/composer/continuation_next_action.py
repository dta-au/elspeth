"""Retained allowance selection for the implemented committed P4 recovery cuts.

This selector sizes the next segment. It grants no dispatch, current metadata,
decoded witness or business writer. Complete current result/journal admission
still runs after the actual new claim and physical fence have been established.
Other phase owners remain explicit refusals until their real producers exist.
"""

from __future__ import annotations

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import ComposeFrame, InitializeFrame, ToolBatchFrame
from elspeth.web.composer.continuation_bootstrap import bootstrap_values, decode_continuation_bootstrap
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy, ContinuationAdmissionPolicy, decode_continuation_policy
from elspeth.web.composer.continuation_selection import SelectedContinuationState


def next_continuation_action_policy(
    *, selected: SelectedContinuationState, policy: ContinuationAdmissionPolicy
) -> ContinuationActionPolicy:
    if type(selected) is not SelectedContinuationState or type(policy) is not ContinuationAdmissionPolicy:
        raise TypeError("next allowance requires its actual selected DATA and retained policy")
    selected.__post_init__()
    root = selected.stack
    if decode_continuation_policy(root.admission_policy_json, root.policy_hash) != policy:
        raise AuditIntegrityError("next allowance changed its retained complete policy")
    top = root.frames[-1]
    if type(top) is InitializeFrame and top.provider_effect_key is not None:
        entries = root.effect_frontier
        if (
            len(root.frames) != 1
            or top.ingress_phase != "title_pending"
            or top.provider_effect_key != "provider:3:0"
            or top.provider_input_sha256 is None
            or top.bootstrap is None
            or top.bootstrap.codec != "composer.bootstrap.v1"
            or top.ingress_receipt_json is None
            or len(entries) != 1
            or entries[0].effect_id != top.provider_effect_key
            or root.pending_owner_results
            or root.semantic_frontier is not None
            or root.active_action_json is not None
            or root.active_action_source is not None
        ):
            raise AuditIntegrityError("next allowance lost its closed TITLE provider cut")
        entry = entries[0]
        if entry.disposition == "prepared":
            admitted = (
                top.title_attempt_id is None and top.title_response is None and not root.pending_settlement_ids and entry.receipt_id is None
            )
        elif entry.disposition == "dispatched":
            admitted = (
                top.title_attempt_id is not None
                and top.title_response is None
                and entry.receipt_id is None
                and root.pending_settlement_ids == (top.title_attempt_id,)
            )
        elif entry.disposition == "committed":
            admitted = (
                top.title_attempt_id is not None
                and top.title_response is not None
                and top.title_response.codec == "composer.title-provider-result.v1"
                and entry.receipt_id == f"title:{top.title_response.digest}"
                and root.pending_settlement_ids in ((), (top.title_attempt_id,))
            )
        else:
            admitted = False
        if not admitted:
            raise AuditIntegrityError("next allowance cannot redispatch an unresolved TITLE provider")
        # SQL-only wait/result/accounting sizing. The current TITLE source owner
        # rechecks every event, physical creator, original audit and ledger.
        # No old prepared/dispatched cut can mint another provider invocation.
        return policy.actions.session_sql
    if type(top) is InitializeFrame:
        if top.ingress_phase == "ready" and top.title_response is not None:
            if (
                len(root.frames) != 1
                or top.bootstrap is None
                or top.bootstrap.codec != "composer.bootstrap.v1"
                or top.ingress_receipt_json is None
                or top.title_attempt_id is None
                or top.title_response.codec != "composer.title-provider-result.v1"
                or top.provider_effect_key is not None
                or top.provider_input_sha256 is not None
                or root.pending_owner_results
                or root.pending_settlement_ids
                or root.active_action_json is not None
                or root.active_action_source is not None
                or root.semantic_frontier is not None
                or len(root.effect_frontier) != 1
                or root.effect_frontier[0].effect_id != "provider:3:0"
                or root.effect_frontier[0].disposition != "committed"
                or root.effect_frontier[0].receipt_id != f"title:{top.title_response.digest}"
            ):
                raise AuditIntegrityError("next allowance lost its closed applied TITLE ready cut")
            # SQL-only sizing. The actual current ready reader must prove the
            # application event, settled predecessor and all original sources.
            return policy.actions.session_sql
        published = top.ingress_phase in ("title_pending", "ready")
        if (
            len(root.frames) != 1
            or top.ingress_phase not in ("reserved", "inserted", "title_pending", "ready")
            or top.title_attempt_id is not None
            or (not published and top.bootstrap is not None)
            or (published and (top.bootstrap is None or top.bootstrap.codec != "composer.bootstrap.v1"))
            or top.title_response is not None
            or top.provider_effect_key is not None
            or top.provider_input_sha256 is not None
            or root.effect_frontier
            or root.pending_owner_results
            or root.pending_settlement_ids
            or root.active_action_json is not None
            or root.active_action_source is not None
            or root.semantic_frontier is not None
            or (top.ingress_phase == "reserved" and top.ingress_receipt_json is not None)
            or (top.ingress_phase != "reserved" and top.ingress_receipt_json is None)
        ):
            raise AuditIntegrityError("next allowance has no closed reserved/inserted ingress cut")
        if published:
            if len(selected.chunks) != 1 or selected.chunks[0].reference != top.bootstrap:
                raise AuditIntegrityError("next allowance lost its complete published bootstrap source")
            snapshot = decode_continuation_bootstrap(selected.chunks[0])
            values = bootstrap_values(snapshot)
            if (
                snapshot.logical != root.logical
                or root.logical.kind != "compose_message"
                or values.retained_context is None
                or values.retained_context.availability != "available"
                or top.ingress_phase != ("title_pending" if snapshot.auto_title_eligible else "ready")
            ):
                # Keep the original refusal for a manufactured later phase
                # carrying no actual complete bounded bootstrap input.
                raise AuditIntegrityError("next allowance has no closed reserved/inserted ingress cut")
        # SQL-only sizing. Actual current ingress source/receipt admission is
        # mandatory after start; no title/provider/Compose entry is granted.
        # Published cuts additionally require the actual bootstrap publication
        # event/predecessor/creator/private-source reader under the fresh claim.
        return policy.actions.session_sql
    if type(top) is ComposeFrame and top.phase == "action_ready":
        if (
            len(root.frames) != 1
            or top.provider_effect_key is not None
            or top.provider_input_sha256 is not None
            or top.pending_response is not None
            or top.initial_state != top.state
            or top.tools is None
            or top.composition_slice != 0
            or top.discovery_slice != 0
            or top.composition_total != 0
            or top.discovery_total != 0
            or top.advisor_calls != 0
            or top.repair_turns != 0
            or top.advisor_checkpoint_passes != 0
            or top.mutation_success_seen
            or top.persisted_assistant_message_id is not None
            or top.persisted_assistant_content is not None
            or top.persisted_tool_call_turn
            or top.advisor_repair_context_introduced
            or top.pending_advisor_elision_indices
            or root.pending_settlement_ids
            or root.pending_owner_results
            or root.semantic_frontier is None
            or root.active_action_json is not None
            or root.active_action_source is not None
            or len(root.effect_frontier) > 1
            or any(
                item.effect_id != "provider:3:0"
                or item.disposition != "committed"
                or item.receipt_id is None
                or not item.receipt_id.startswith("title:")
                for item in root.effect_frontier
            )
        ):
            raise AuditIntegrityError("next allowance has no closed unconsumed first Compose cut")
        # Structural sizing only. Fresh current initialization admission must
        # prove the real event/ready predecessor and complete captured graph.
        # The original PRIMARY preparer/gateway/guard/quota still own dispatch.
        return policy.actions.primary
    if type(top) is ComposeFrame and top.phase == "response_retained":
        matches = tuple(item for item in root.effect_frontier if item.effect_id == top.provider_effect_key)
        if (
            len(root.frames) != 1
            or root.pending_settlement_ids
            or root.pending_owner_results
            or root.active_action_json is not None
            or root.active_action_source is not None
            or top.pending_response is None
            or top.pending_response.codec != "composer.response.v1"
            or top.provider_effect_key is None
            or not top.provider_effect_key.startswith("provider:0:")
            or top.provider_input_sha256 is None
            or len(matches) != 1
            or matches[0].disposition != "committed"
            or not matches[0].receipt_id
            or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
        ):
            raise AuditIntegrityError("next allowance cannot retry an unresolved primary provider effect")
        # A structural allowance only. The new actual claim must use the SQL
        # primary result reader and its original audit/accounting source joins.
        # It receives no provider allowance and never dispatches this result.
        return policy.actions.session_sql
    if type(top) is not ToolBatchFrame or sum(type(frame) is ComposeFrame for frame in root.frames) != 1:
        raise AuditIntegrityError("next allowance has no implemented retained P4 recovery cut")
    if top.accumulator.codec == "composer.tool-batch.v1":
        prefix = f"attachment:{top.response.digest}:"
        pending = root.pending_owner_results
        if top.phase == "effect_reconciling" and len(pending) == 1 and pending[0].kind == "read":
            attachment_key = pending[0].effect_key
            admitted = pending[0].result.codec == "composer.owner-result.v1" and attachment_key not in top.settled_effect_ids
        elif top.phase == "result_retained" and not pending and top.settled_effect_ids:
            attachment_key = top.settled_effect_ids[-1]
            admitted = top.settled_effect_ids.count(attachment_key) == 1
        else:
            raise AuditIntegrityError("next allowance cannot repeat an unresolved attachment read")
        frontier = tuple(item for item in root.effect_frontier if item.effect_id == attachment_key)
        if (
            not admitted
            or not attachment_key.startswith(prefix)
            or len(root.frames) != 2
            or type(root.frames[0]) is not ComposeFrame
            or root.active_action_json is not None
            or root.active_action_source is not None
            or root.pending_settlement_ids
            or len(frontier) != 1
            or frontier[0].disposition != "committed"
            or frontier[0].receipt_id is None
            or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
        ):
            raise AuditIntegrityError("next allowance lost its exact committed attachment read cut")
        # Only immutable-result/current-version SQL is admitted here. The complete
        # actual call, inner DATA, creator and current root are checked after start.
        return policy.actions.session_sql
    if top.phase == "tool_ready" and top.accumulator.codec == "composer.coupled-batch-data.v1":
        key = f"attachment:{top.response.digest}:{top.next_tool_index - 1}"
        frontier = tuple(item for item in root.effect_frontier if item.effect_id == key)
        if (
            len(root.frames) != 2
            or type(root.frames[0]) is not ComposeFrame
            or root.frames[0].pending_response != top.response
            or top.next_tool_index < 1
            or root.pending_owner_results
            or root.pending_settlement_ids
            or root.active_action_json is not None
            or root.active_action_source is not None
            or not top.settled_effect_ids
            or top.settled_effect_ids[-1] != key
            or top.settled_effect_ids.count(key) != 1
            or len(frontier) != 1
            or frontier[0].disposition != "committed"
            or not frontier[0].receipt_id
            or any(item.disposition not in ("committed", "refused") for item in root.effect_frontier)
        ):
            raise AuditIntegrityError("next allowance lost its completed attachment application cut")
        # SQL-only structural allowance. The actual current runtime reader must
        # reprove the immutable application event/checkpoints/result/receipt;
        # no subsequent read/provider/business action is admitted by this DATA.
        return policy.actions.session_sql
    effect_key = f"batch:{top.response.digest}:p4"
    if top.phase == "effect_reconciling" and top.accumulator.codec == "composer.p4-pending-batch.v1":
        pending = root.pending_owner_results
        if (
            len(pending) != 1
            or pending[0].kind != "persisted_turn"
            or pending[0].effect_key != effect_key
            or pending[0].result.codec != "composer.owner-result.v1"
            or effect_key in top.settled_effect_ids
        ):
            raise AuditIntegrityError("next allowance cannot retry a prepared or unknown business effect")
    elif top.phase == "result_retained" and top.accumulator.codec == "composer.applied-p4-batch.v1":
        if root.pending_owner_results or effect_key not in top.settled_effect_ids:
            raise AuditIntegrityError("next allowance lost its actual settled P4 application cut")
    else:
        raise AuditIntegrityError("next allowance has no implemented retained P4 recovery cut")
    # This conservative structural branch performs no context-sensitive DATA
    # decode before the new running owner can obtain actual current names.
    # Both supported cuts consume only the already-committed result or journal.
    return policy.actions.session_sql
