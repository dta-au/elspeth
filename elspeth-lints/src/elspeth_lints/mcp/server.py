"""``elspeth-judge`` MCP server -- the key-free agent staging surface.

This server lets a key-free agent assemble and inspect an authority-free review
*bundle* (``core.review_bundle``); the operator fires it with the key-bearing
``elspeth-lints sign-bundle`` / ``rekey`` CLI. It mirrors the protocol shape of
``src/elspeth/mcp/server.py`` (``Server``, ``@server.list_tools()``,
``@server.call_tool()``, ``stdio_server()``).

[O1] linchpin -- **the agent never holds the HMAC key.** Every tool handler
calls ``_assert_no_hmac_key_in_env()`` as its *first statement*, before any
optional-dependency import (the ``mcp`` SDK or ``claude-agent-sdk``). If the
key-check sat after a lazy optional-dep import, a handler invoked with the key
set but the extra absent would trip the install-hint ``ImportError`` instead of
failing closed -- silently making the structural guarantee contingent on an
optional dependency. Fail-closed precedes everything.

There is **no authoritative MCP path**: ``verify_signatures`` is always
shape-only. The authoritative HMAC recompute lives on the CLI/library
``diagnose_judge_signatures`` surface, which upgrades only when the operator's
shell holds the key.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

from elspeth_lints.core.allowlist import _JUDGE_METADATA_SIGNATURE_ENV_VAR, JudgeVerdict

__all__ = [
    "HmacKeyPresentError",
    "build_scan_actions",
    "create_server",
    "main",
    "run_server",
]


class HmacKeyPresentError(RuntimeError):
    """Raised when the operator-only HMAC key is present in the agent's env.

    The message names the offending env var so the fail-closed result the
    dispatcher surfaces is self-describing.
    """


def _assert_no_hmac_key_in_env() -> None:
    """Fail closed if the operator-only HMAC key is in the environment.

    Must be the **first statement** of every tool handler, before any
    optional-dependency import. ``raw`` is treated as absent when unset or
    empty (matching ``_verification_mode``'s shape-only branch).
    """
    raw = os.environ.get(_JUDGE_METADATA_SIGNATURE_ENV_VAR)
    if raw:
        raise HmacKeyPresentError(
            f"refusing to run: {_JUDGE_METADATA_SIGNATURE_ENV_VAR} is present in this environment. "
            "The elspeth-judge MCP surface is structurally key-free -- staging asserts, only the "
            "operator CLI (sign-bundle / rekey) holds the key and mints signatures."
        )


@dataclass(frozen=True, slots=True)
class _ServerContext:
    """Resolved roots a tool handler operates over."""

    root: Path
    allowlist_dir: Path
    staged_dir: Path

    def __post_init__(self) -> None:
        """Freeze staging scope in absolute coordinates at server creation."""
        object.__setattr__(self, "root", Path(self.root).resolve())
        object.__setattr__(self, "allowlist_dir", Path(self.allowlist_dir).resolve())
        object.__setattr__(self, "staged_dir", Path(self.staged_dir).resolve())


@dataclass(frozen=True, slots=True)
class _ToolOutcome:
    """An mcp-independent tool result the dispatcher returns.

    ``create_server`` translates this into the ``mcp.types`` protocol objects;
    keeping the dispatcher free of ``mcp`` imports lets the structural
    fail-closed test reach every handler without the SDK installed.
    """

    text: str
    is_error: bool


@dataclass(frozen=True, slots=True)
class _ToolSpec:
    """One registered tool: description, JSON Schema, and handler."""

    description: str
    input_schema: dict[str, Any]
    handler: Callable[[_ServerContext, dict[str, Any]], str]


# Registry of every tool the server exposes. ``create_server`` registers all of
# these; the structural fail-closed test enumerates this exact table, so a
# future tool added here without routing through ``_assert_no_hmac_key_in_env``
# is caught automatically.
_TOOLS: dict[str, _ToolSpec] = {}


def _run_tool(ctx: _ServerContext, name: str, arguments: dict[str, Any]) -> _ToolOutcome:
    """Synchronous, mcp-independent dispatcher.

    Fail-closed (``HmacKeyPresentError``) and operational/argument errors are
    converted into error ``_ToolOutcome``s; genuinely unexpected errors
    propagate so they surface as protocol errors rather than silent text.
    """
    spec = _TOOLS.get(name)
    if spec is None:
        return _ToolOutcome(text=f"unknown tool: {name!r}", is_error=True)
    try:
        text = spec.handler(ctx, arguments)
    except HmacKeyPresentError as exc:
        return _ToolOutcome(text=str(exc), is_error=True)
    except ImportError as exc:
        # An optional-extra-absent install hint (e.g. stage_preview without
        # [judge-agent]) -- a clean error result, never a crash.
        return _ToolOutcome(text=str(exc), is_error=True)
    except (ValueError, KeyError, FileNotFoundError, OSError) as exc:
        return _ToolOutcome(text=f"{name}: {exc}", is_error=True)
    return _ToolOutcome(text=text, is_error=False)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

_OPERATOR_KEY_PLACEHOLDER = f"{_JUDGE_METADATA_SIGNATURE_ENV_VAR}=<operator-held-key>"

# Above this many judge calls the rendered command carries
# ``--continue-on-block``. A run this long is left unattended, and without the
# flag one judged BLOCK ends it and the remaining actions are never attempted.
# The flag costs nothing when nothing blocks -- exit 3 is reached only *after* a
# BLOCK -- so the threshold is a command-noise judgement rather than a safety
# trade-off, and there is no risk argument for lowering it to zero. Ten is the
# point where an operator stops watching the run.
_CONTINUE_ON_BLOCK_JUDGE_CALL_THRESHOLD = 10


def _require_str_arg(arguments: dict[str, Any], name: str) -> str:
    value = arguments.get(name)
    if type(value) is not str or not value:
        raise ValueError(f"argument {name!r} is required and must be a non-empty string")
    return value


def _resolve_bundle_path(ctx: _ServerContext, arguments: dict[str, Any]) -> Path:
    """Resolve ``<staged_dir>/<bundle_id>.json`` from the ``bundle_id`` arg."""
    from elspeth_lints.core.review_bundle import resolve_staged_bundle_path

    bundle_id = _require_str_arg(arguments, "bundle_id")
    return resolve_staged_bundle_path(staged_dir=ctx.staged_dir, bundle_id=bundle_id)


def _shell_join_keep_user(parts: list[str]) -> str:
    import shlex

    return " ".join(part if part == '"$USER"' else shlex.quote(part) for part in parts)


def _judge_calling_kinds() -> frozenset[str]:
    """The action kinds that spend a real judge call, from the fire-time authority.

    ``sign_bundle_transaction`` owns this split -- it is the module that decides
    which exit-1 is a judged BLOCK -- so the price quoted here cannot drift from
    what the transaction actually spends. ``rotation`` and ``stale_delete`` are
    mechanical YAML rewrites with no judge in the path.
    """
    from elspeth_lints.core.sign_bundle_transaction import _JUDGE_GATED_KINDS

    return _JUDGE_GATED_KINDS


def _bundle_lane_costs(bundle: Any) -> dict[str, tuple[int, int]]:
    """Per-lane ``(action_count, judge_call_count)`` for the lanes actually staged.

    The lane names come from the bundle's own actions, whose ``lane`` is derived
    from ``kind`` by ``BundleAction.__post_init__`` -- so a rendered ``--lanes``
    value can never be a hand-typed string that the CLI would reject.
    """
    judge_calling = _judge_calling_kinds()
    costs: dict[str, list[int]] = {}
    for action in bundle.actions:
        counts = costs.setdefault(action.lane, [0, 0])
        counts[0] += 1
        if action.kind in judge_calling:
            counts[1] += 1
    return {lane: (actions, judge_calls) for lane, (actions, judge_calls) in costs.items()}


def _sign_bundle_command_text(
    ctx: _ServerContext,
    bundle_path: Path,
    *,
    lanes: str | None,
    continue_on_block: bool,
) -> str:
    """Render one paste-ready ``sign-bundle`` invocation at the given scope.

    Mirrors ``judge_signature_diagnosis._justify_command``: the operator key is
    an ``env`` placeholder (never a real value), and ``--owner "$USER"`` is left
    unquoted so the operator's shell expands it.
    """
    parts = [
        "env",
        _OPERATOR_KEY_PLACEHOLDER,
        "PYTHONPATH=elspeth-lints/src",
        ".venv/bin/python",
        "-m",
        "elspeth_lints.core.cli",
        "sign-bundle",
        str(bundle_path),
        "--root",
        str(ctx.root),
        "--allowlist-dir",
        str(ctx.allowlist_dir),
        "--owner",
        '"$USER"',
        "--judge-transport",
        "codex-cli",
        "--judge-tools",
        "readonly",
    ]
    if lanes is not None:
        parts.extend(("--lanes", lanes))
    if continue_on_block:
        parts.append("--continue-on-block")
    parts.append("--dry-run")
    return _shell_join_keep_user(parts)


def _sign_bundle_command(ctx: _ServerContext, bundle_path: Path, bundle: Any) -> str:
    """The paste-ready whole-bundle operator ``sign-bundle`` command.

    Whole-bundle, so no ``--lanes``: this is the command for firing everything
    that was staged. ``sign_bundle_plan`` renders the per-lane alternatives and
    prices each one, so the operator can see that a cheaper scope exists rather
    than having this surface pick for them.
    """
    judge_calls = sum(count for _, count in _bundle_lane_costs(bundle).values())
    return _sign_bundle_command_text(
        ctx,
        bundle_path,
        lanes=None,
        continue_on_block=judge_calls > _CONTINUE_ON_BLOCK_JUDGE_CALL_THRESHOLD,
    )


def _sign_bundle_plan(ctx: _ServerContext, bundle_path: Path, bundle: Any) -> dict[str, Any]:
    """Price the staged bundle and render a lane-scoped command for each lane.

    The whole-bundle command alone hid the cost of what it was about to spend
    (elspeth-23ee8e3440): a bundle mixing a mechanical ``resign`` lane with a
    large un-rationaled ``new_judgment`` lane read exactly like a cheap one.
    Every command here carries ``--dry-run``, so pasting one spends nothing;
    ``judge_calls`` is what that scope costs once ``--dry-run`` is dropped.
    Lanes are ordered cheapest-first so the price is what the reader sees first.
    """
    costs = _bundle_lane_costs(bundle)
    judge_calls_total = sum(count for _, count in costs.values())
    per_lane = [
        {
            "lane": lane,
            "actions": actions,
            "judge_calls": judge_calls,
            "command": _sign_bundle_command_text(
                ctx,
                bundle_path,
                lanes=lane,
                continue_on_block=judge_calls > _CONTINUE_ON_BLOCK_JUDGE_CALL_THRESHOLD,
            ),
        }
        for lane, (actions, judge_calls) in sorted(costs.items(), key=lambda item: (item[1][1], item[0]))
    ]

    notes = [
        "Every command carries --dry-run and spends no judge call; judge_calls is what that scope costs once --dry-run is dropped.",
    ]
    if len(per_lane) > 1:
        notes.append(
            f"This bundle mixes {len(per_lane)} lanes. sign_bundle_command fires all of them "
            f"({judge_calls_total} judge call(s)); each per_lane command fires one lane and costs "
            "only that lane's judge calls. Unselected actions are never attempted, never judged, "
            "and stay exactly as they are in the allowlist."
        )
    if judge_calls_total > _CONTINUE_ON_BLOCK_JUDGE_CALL_THRESHOLD:
        notes.append(
            f"--continue-on-block is rendered wherever a scope exceeds "
            f"{_CONTINUE_ON_BLOCK_JUDGE_CALL_THRESHOLD} judge call(s): without it one judged BLOCK "
            "ends the run and the remaining actions are never attempted. With it, blocked actions "
            "are journalled and left fail-closed, the survivors publish coherently, and the command "
            "exits 3 rather than 0."
        )

    justify_total = sum(1 for action in bundle.actions if action.kind == "justify")
    missing_rationale = sum(1 for action in bundle.actions if action.kind == "justify" and not (action.draft_rationale or "").strip())
    if missing_rationale:
        notes.append(
            f"{missing_rationale} of {justify_total} justify action(s) carry no draft_rationale. Each "
            "still spends a judge call at fire time and the judge rules on a generic placeholder, so "
            "this is an expensive, near-certain-BLOCK run: annotate them with stage_annotate (and "
            "check them with stage_preview) before firing the new_judgment lane."
        )

    return {
        "actions_total": len(bundle.actions),
        "judge_calls_total": judge_calls_total,
        "judge_calling_kinds": sorted(_judge_calling_kinds()),
        "per_lane": per_lane,
        "notes": notes,
    }


def _rekey_command(ctx: _ServerContext, bundle_path: Path, *, old_key_env: str, new_key_env: str) -> str:
    """The paste-ready operator ``rekey`` command for a staged rekey bundle.

    The two keys are supplied through the operator-named env vars (placeholders
    here, never values); the CLI reads them by the ``--old-key-env`` /
    ``--new-key-env`` names.
    """
    parts = [
        "env",
        f"{old_key_env}=<old-operator-key>",
        f"{new_key_env}=<new-operator-key>",
        "PYTHONPATH=elspeth-lints/src",
        ".venv/bin/python",
        "-m",
        "elspeth_lints.core.cli",
        "rekey",
        "--in",
        str(bundle_path),
        "--old-key-env",
        old_key_env,
        "--new-key-env",
        new_key_env,
        "--root",
        str(ctx.root),
        "--allowlist-dir",
        str(ctx.allowlist_dir),
    ]
    return _shell_join_keep_user(parts)


# --------------------------------------------------------------------------- #
# Tool handlers
# --------------------------------------------------------------------------- #


def _tool_verify_signatures(ctx: _ServerContext, arguments: dict[str, Any]) -> str:
    """Structurally shape-only read-only signature diagnosis (never authoritative).

    Unlike the agent-shell ``diagnose-judge-signatures`` script (shape-only only
    because the shell happens to lack the key), this is **structurally** key-free
    -- ``_assert_no_hmac_key_in_env()`` aborts if a key is ever present -- so it
    is a provably-unprivileged read regardless of the surrounding env.
    """
    _assert_no_hmac_key_in_env()
    from elspeth_lints.core.judge_signature_diagnosis import (
        diagnose_judge_signatures,
        render_judge_signature_diagnosis_json,
    )

    report = diagnose_judge_signatures(root=ctx.root, allowlist_dir=ctx.allowlist_dir)
    return render_judge_signature_diagnosis_json(report)


def _tool_stage_status(ctx: _ServerContext, arguments: dict[str, Any]) -> str:
    """Summarise a staged bundle: per-lane/kind counts + the operator command."""
    _assert_no_hmac_key_in_env()
    from elspeth_lints.core.review_bundle import read_bundle

    bundle_path = _resolve_bundle_path(ctx, arguments)
    bundle = read_bundle(bundle_path)
    if bundle.rekey is not None and not bundle.actions:
        from elspeth_lints.core.source_snapshot import capture_source_snapshot

        if (
            not Path(bundle.root).is_absolute()
            or not Path(bundle.allowlist_dir).is_absolute()
            or Path(bundle.root).resolve() != ctx.root.resolve()
            or Path(bundle.allowlist_dir).resolve() != ctx.allowlist_dir.resolve()
        ):
            raise ValueError("staged rekey bundle root/allowlist scope is stale; re-run stage_rekey")
        live_source = capture_source_snapshot(source_root=ctx.root, allowlist_dir=ctx.allowlist_dir)
        if (
            bundle.source_rev,
            bundle.source_dirty,
            bundle.source_snapshot_sha256,
        ) != (
            live_source.source_rev,
            live_source.source_dirty,
            live_source.source_snapshot_sha256,
        ):
            raise ValueError("staged rekey bundle source binding is stale; re-run stage_rekey")
    else:
        from elspeth_lints.core.bundle_verify import verify_bundle_against_tree

        verification = verify_bundle_against_tree(bundle, root=ctx.root, allowlist_dir=ctx.allowlist_dir)
        if not verification.ok:
            raise ValueError("staged bundle is stale; re-run stage_scan: " + "; ".join(verification.mismatches))

    lane_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    preview_outcomes: dict[str, int] = {}
    for action in bundle.actions:
        lane_counts[action.lane] = lane_counts.get(action.lane, 0) + 1
        kind_counts[action.kind] = kind_counts.get(action.kind, 0) + 1
        outcome = action.preview.verdict if action.preview is not None else "none"
        preview_outcomes[outcome] = preview_outcomes.get(outcome, 0) + 1
    justify_missing_rationale = sorted(
        action.key for action in bundle.actions if action.kind == "justify" and not (action.draft_rationale or "").strip()
    )

    payload = {
        "bundle_id": bundle.bundle_id,
        "root": bundle.root,
        "allowlist_dir": bundle.allowlist_dir,
        "staged_by": bundle.staged_by,
        "created_at": bundle.created_at,
        "source_rev": bundle.source_rev,
        "source_dirty": bundle.source_dirty,
        "source_snapshot_sha256": bundle.source_snapshot_sha256,
        "source_verification": "ok",
        "actions_total": len(bundle.actions),
        "lane_counts": lane_counts,
        "kind_counts": kind_counts,
        "preview_outcomes": preview_outcomes,
        "justify_missing_rationale": justify_missing_rationale,
        "has_rekey_plan": bundle.rekey is not None,
        "sign_bundle_command": _sign_bundle_command(ctx, bundle_path, bundle),
        "sign_bundle_plan": _sign_bundle_plan(ctx, bundle_path, bundle),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _finding_canonical_key(finding: Any) -> str:
    key = finding.canonical_key
    if callable(key):
        key = key()
    if not isinstance(key, str):
        raise ValueError(f"finding.canonical_key must be str; got {type(key).__name__}")
    return key


def _new_judgment_action_from_finding(finding: Any) -> Any:
    from elspeth_lints.core.review_bundle import BundleAction

    symbol_context = finding.symbol_context
    symbol = ".".join(symbol_context) if symbol_context else "_module_"
    return BundleAction(
        lane="new_judgment",
        kind="justify",
        key=_finding_canonical_key(finding),
        file_path=finding.file_path,
        symbol=symbol,
        rule=finding.rule_id,
        fingerprint=finding.fingerprint,
        scope_fingerprint=finding.scope_fingerprint,
        ast_path=finding.ast_path,
    )


def _build_scan_plan(ctx: _ServerContext) -> tuple[list[Any], Any]:
    """Survey the tree+allowlist into non-overlapping bundle actions (key-free, no LLM).

    Routing is explicit and non-overlapping:

    * ``drift_repair`` / ``stale_delete`` -- from ``diagnose_judge_signatures``
      (signable drift vs non-signable orphan statuses on judge-gated entries);
    * ``rotation`` -- remove findings already assigned to judge-gated diagnosis,
      then plan the residual pre-judge population; residual ambiguity fails
      staging because no deterministic action can represent it safely;
    * ``new_judgment`` -- live findings covered by neither an exact canonical
      key nor a per-file rule in the full allowlist, excluding identity groups
      already reserved for a diagnosis action in this bundle.
    """
    from elspeth_lints.core.bundle_verify import _STALE_DELETE_ORPHAN_STATUSES
    from elspeth_lints.core.judge_signature_diagnosis import (
        _SIGNABLE_DIAGNOSIS_STATUSES,
        diagnose_judge_signatures,
    )
    from elspeth_lints.core.review_bundle import BundleAction
    from elspeth_lints.core.tier_model_scan import (
        TargetCoverage,
        census_tree_targets,
        plan_non_judge_rotations,
        routable_new_judgment_findings,
        scan_tree_findings,
    )
    from elspeth_lints.rules.trust_tier.tier_model.rule import _load_tier_model_allowlist

    actions: list[Any] = []

    # drift_repair + stale_delete from the keyless diagnosis index.
    diagnosis = diagnose_judge_signatures(root=ctx.root, allowlist_dir=ctx.allowlist_dir)
    for item in diagnosis.items:
        if item.status in _SIGNABLE_DIAGNOSIS_STATUSES:
            actions.append(
                BundleAction(
                    lane="resign",
                    kind="drift_repair",
                    key=item.key,
                    source_file=item.source_file,
                    diagnosis_status=item.status,
                )
            )
        elif item.status in _STALE_DELETE_ORPHAN_STATUSES:
            actions.append(BundleAction(lane="resign", kind="stale_delete", key=item.key, source_file=item.source_file))

    # Scan once, classify full coverage, then remove findings already assigned
    # to judge-gated diagnosis before planning the residual pre-judge lane.
    allowlist = _load_tier_model_allowlist(ctx.allowlist_dir)
    raw_findings = tuple(scan_tree_findings(root=ctx.root))
    rotation_plan = plan_non_judge_rotations(
        findings=raw_findings,
        allowlist=allowlist,
        diagnosis_items=diagnosis.items,
    )
    # Coverage is exact at the canonical-key level; see bundle_verify.py for
    # why the three sets stay separate.
    target_coverage = TargetCoverage(
        exact_keys=frozenset(entry.key for entry in allowlist.entries),
        diagnosis_assigned_keys=frozenset(
            item.repair_key for item in diagnosis.items if item.status in _SIGNABLE_DIAGNOSIS_STATUSES and item.repair_key is not None
        ),
        resign_assigned_keys=frozenset(rotation.new_key for rotation in rotation_plan.rotations),
    )
    target_scan = census_tree_targets(
        root=ctx.root,
        findings=raw_findings,
        coverage=target_coverage,
        per_file_rules=allowlist.per_file_rules,
    )
    if rotation_plan.ambiguous:
        groups = ", ".join(
            f"{group.prefix} ({group.finding_count} finding(s), {group.entry_count} entry/entries)" for group in rotation_plan.ambiguous
        )
        raise ValueError(f"stage_scan found ambiguous non-judge target group(s): {groups}")
    for rotation in rotation_plan.rotations:
        actions.append(BundleAction(lane="resign", kind="rotation", key=rotation.old_key, source_file=rotation.entry_source_file))

    # New-judgment coverage is exact. Identity-prefix grouping is used only to
    # keep outstanding diagnosis work in a separate authority lane.
    for finding in routable_new_judgment_findings(
        uncovered_findings=target_scan.uncovered_findings,
        diagnosis_items=diagnosis.items,
        rotation_plan=rotation_plan,
    ):
        actions.append(_new_judgment_action_from_finding(finding))

    actions.sort(key=lambda action: (action.kind, action.key))
    return actions, target_scan.census


def _build_scan_actions(ctx: _ServerContext) -> list[Any]:
    """Compatibility wrapper returning only the deterministic action inventory."""
    actions, _census = _build_scan_plan(ctx)
    return actions


def build_scan_actions(*, root: Path, allowlist_dir: Path) -> tuple[Any, ...]:
    """Public key-free action inventory shared by staging and corpus review."""
    _assert_no_hmac_key_in_env()
    ctx = _ServerContext(
        root=Path(root),
        allowlist_dir=Path(allowlist_dir),
        staged_dir=Path("."),
    )
    return tuple(_build_scan_actions(ctx))


def _tool_stage_scan(ctx: _ServerContext, arguments: dict[str, Any]) -> str:
    """Build/refresh the authority-free worklist bundle. Key-free, no LLM, fast."""
    _assert_no_hmac_key_in_env()
    import uuid
    from datetime import datetime

    from elspeth_lints.core.review_bundle import SCHEMA_VERSION, ReviewBundle, write_bundle
    from elspeth_lints.core.source_snapshot import SourceSnapshotChangedError, observe_source_snapshot

    bundle_id = _require_str_arg(arguments, "bundle_id") if "bundle_id" in arguments else f"stage-scan-{uuid.uuid4().hex[:12]}"
    staged_by_arg = arguments.get("staged_by")
    staged_by = staged_by_arg if isinstance(staged_by_arg, str) and staged_by_arg else "elspeth-judge-agent"

    source_before = observe_source_snapshot(source_root=ctx.root, allowlist_dir=ctx.allowlist_dir)
    actions, target_census = _build_scan_plan(ctx)
    source_after = observe_source_snapshot(source_root=ctx.root, allowlist_dir=ctx.allowlist_dir)
    if source_before != source_after:
        raise SourceSnapshotChangedError("source binding changed while deriving stage_scan actions")
    bundle = ReviewBundle(
        bundle_id=bundle_id,
        schema_version=SCHEMA_VERSION,
        created_at=datetime.now(UTC).isoformat(),
        staged_by=staged_by,
        root=str(ctx.root),
        allowlist_dir=str(ctx.allowlist_dir),
        source_rev=source_after.source_rev,
        source_dirty=source_after.source_dirty,
        source_snapshot_sha256=source_after.source_snapshot_sha256,
        actions=tuple(actions),
    )
    path = write_bundle(bundle, staged_dir=ctx.staged_dir)

    lane_counts: dict[str, int] = {}
    kind_counts: dict[str, int] = {}
    for action in bundle.actions:
        lane_counts[action.lane] = lane_counts.get(action.lane, 0) + 1
        kind_counts[action.kind] = kind_counts.get(action.kind, 0) + 1

    payload = {
        "bundle_id": bundle.bundle_id,
        "written_path": str(path),
        "actions_total": len(bundle.actions),
        "lane_counts": lane_counts,
        "kind_counts": kind_counts,
        "target_census": {
            "raw_target_count": target_census.raw_target_count,
            "exact_covered_count": target_census.exact_covered_count,
            "per_file_covered_count": target_census.per_file_covered_count,
            "uncovered_count": target_census.uncovered_count,
        },
        "sign_bundle_command": _sign_bundle_command(ctx, path, bundle),
        "sign_bundle_plan": _sign_bundle_plan(ctx, path, bundle),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _tool_stage_annotate(ctx: _ServerContext, arguments: dict[str, Any]) -> str:
    """Attach agent-authored draft rationales to staged ``justify`` actions.

    The key-free rationale-custody surface (elspeth-0502deb48c): ``stage_scan``
    builds justify actions with ``draft_rationale=None`` and hand-editing the
    bundle JSON is not an official mutator, so without this tool the preview
    judge always rules on an empty rationale and the operator fire always
    stores the generic fallback. Annotating binds the site-specific rationale
    to the action; ``stage_preview`` then judges it and the operator
    ``sign-bundle`` carries it into the authoritative judge call.

    Setting a rationale CLEARS any existing preview on that action — a preview
    verdict rendered for a different rationale is stale evidence.
    """
    _assert_no_hmac_key_in_env()
    from dataclasses import replace

    from elspeth_lints.core.bundle_verify import verify_bundle_against_tree
    from elspeth_lints.core.review_bundle import read_bundle, write_bundle

    bundle_path = _resolve_bundle_path(ctx, arguments)
    bundle = read_bundle(bundle_path)
    verification = verify_bundle_against_tree(bundle, root=ctx.root, allowlist_dir=ctx.allowlist_dir)
    if not verification.ok:
        raise ValueError("staged bundle is stale; re-run stage_scan: " + "; ".join(verification.mismatches))

    rationales_arg = arguments.get("rationales")
    if not isinstance(rationales_arg, dict) or not rationales_arg:
        raise ValueError("stage_annotate requires a non-empty 'rationales' object mapping action key -> rationale text")
    justify_keys = {action.key for action in bundle.actions if action.kind == "justify"}
    rationales: dict[str, str] = {}
    for key, text in rationales_arg.items():
        if not isinstance(key, str) or not isinstance(text, str) or not text.strip():
            raise ValueError("stage_annotate rationales must map string action keys to non-empty rationale strings")
        if key not in justify_keys:
            raise ValueError(f"stage_annotate key does not name a staged justify action: {key!r}")
        rationales[key] = text

    new_actions: list[Any] = []
    for action in bundle.actions:
        text = rationales.get(action.key) if action.kind == "justify" else None
        if text is None:
            new_actions.append(action)
            continue
        new_actions.append(replace(action, draft_rationale=text, preview=None))

    new_bundle = replace(bundle, actions=tuple(new_actions))
    verification = verify_bundle_against_tree(new_bundle, root=ctx.root, allowlist_dir=ctx.allowlist_dir)
    if not verification.ok:
        raise ValueError("staged bundle changed during annotate; refusing rewrite: " + "; ".join(verification.mismatches))
    path = write_bundle(new_bundle, staged_dir=ctx.staged_dir)
    missing = sorted(action.key for action in new_bundle.actions if action.kind == "justify" and not (action.draft_rationale or "").strip())
    payload = {
        "bundle_id": new_bundle.bundle_id,
        "written_path": str(path),
        "annotated": len(rationales),
        "justify_missing_rationale": missing,
        "sign_bundle_command": _sign_bundle_command(ctx, path, new_bundle),
        "sign_bundle_plan": _sign_bundle_plan(ctx, path, new_bundle),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _surrounding_code_for(ctx: _ServerContext, action: Any) -> str:
    """Scrubbed source excerpt for the preview prompt (trust-boundary gate).

    ``surrounding_code`` is shipped to the LLM, so it MUST funnel through the
    secret scrubber ``extract_safe_excerpt`` (path-contains + reads + scrubs in
    one call) -- the same chokepoint ``justify`` uses (cli.py:1649). A raw read
    here would leak un-scrubbed source bytes (the C2-2 leak the structural
    bypass gate forbids). Re-scan the file to locate the staged finding's line
    for a targeted excerpt; any failure degrades to an empty excerpt (the agent
    transport reads source via its read-only ``tool_scope`` regardless).
    """
    from elspeth_lints.core.judge import JUDGE_EXCERPT_CONTEXT_LINES
    from elspeth_lints.core.source_excerpt import (
        SourceExcerptPathOutsideRootError,
        extract_safe_excerpt,
        resolve_safe_excerpt_path,
    )
    from elspeth_lints.core.tier_model_scan import scan_single_file_findings

    if not action.file_path:
        return ""
    try:
        target_file = resolve_safe_excerpt_path(root=ctx.root, target_file=ctx.root / action.file_path)
    except (FileNotFoundError, SourceExcerptPathOutsideRootError):
        return ""
    line = 1
    try:
        for finding in scan_single_file_findings(target_file=target_file, root=ctx.root):
            if _finding_canonical_key(finding) == action.key:
                line = finding.line
                break
    except (OSError, ValueError):
        pass
    try:
        excerpt = extract_safe_excerpt(
            root=ctx.root,
            target_file=target_file,
            line=line,
            context_lines=JUDGE_EXCERPT_CONTEXT_LINES,
        )
    except (OSError, ValueError, SourceExcerptPathOutsideRootError):
        return ""
    return excerpt.text


def _verdict_str(verdict: JudgeVerdict) -> str:
    return verdict.value


def _tool_stage_preview(ctx: _ServerContext, arguments: dict[str, Any]) -> str:
    """Populate each ``new_judgment`` action with a NON-authoritative Codex verdict.

    [O1] ordering: ``_assert_no_hmac_key_in_env()`` is the first line, BEFORE the
    lazy judge import -- so a key-present call fails closed even when the Codex
    CLI is absent. ``ActionPreview.authoritative`` is
    structurally ``False``; the bundle stays signature-free. BLOCKED previews are
    surfaced so the agent can fix code/rationale before the operator step.
    """
    _assert_no_hmac_key_in_env()
    from dataclasses import replace

    from elspeth_lints.core import judge as judge_mod
    from elspeth_lints.core.review_bundle import ActionPreview, read_bundle, write_bundle

    bundle_path = _resolve_bundle_path(ctx, arguments)
    bundle = read_bundle(bundle_path)
    from elspeth_lints.core.bundle_verify import verify_bundle_against_tree

    verification = verify_bundle_against_tree(bundle, root=ctx.root, allowlist_dir=ctx.allowlist_dir)
    if not verification.ok:
        raise ValueError("staged bundle is stale; re-run stage_scan: " + "; ".join(verification.mismatches))

    has_justify = any(action.kind == "justify" for action in bundle.actions)
    tool_scope = judge_mod.build_readonly_tool_scope(root=ctx.root, allowlist_dir=ctx.allowlist_dir) if has_justify else None

    # Preview/fire parity (elspeth-0502deb48c): the real signing path feeds the
    # judge the rule's own definition plus duplicate-rationale evidence
    # (cli.py ``justify``). A preview judged without them rules on a poorer
    # record than the authoritative call, so its verdict is a false signal.
    allowlist_entries: tuple[Any, ...] = ()
    if has_justify:
        from elspeth_lints.core.allowlist import load_allowlist
        from elspeth_lints.rules.trust_tier.tier_model.rule import RULES as _TIER_MODEL_RULES

        allowlist_entries = tuple(load_allowlist(ctx.allowlist_dir, valid_rule_ids=frozenset(_TIER_MODEL_RULES)).entries)
    from elspeth_lints.core.allowlist_similarity import find_similar_allowlist_entries
    from elspeth_lints.rules.trust_tier.tier_model.rule import describe_rule

    new_actions: list[Any] = []
    blocked: list[dict[str, str]] = []
    previewed = 0
    for action in bundle.actions:
        if action.kind != "justify":
            new_actions.append(action)
            continue
        rationale = action.draft_rationale or ""
        rationale_duplicate_count, similar_entries = find_similar_allowlist_entries(
            allowlist_entries,
            rationale=rationale,
            exclude_key=action.key,
        )
        request = judge_mod.JudgeRequest(
            file_path=action.file_path or "",
            rule_id=action.rule or "",
            rule_definition=describe_rule(action.rule or ""),
            symbol=action.symbol or "",
            fingerprint=action.fingerprint or "",
            rationale=rationale,
            surrounding_code=_surrounding_code_for(ctx, action),
            rationale_duplicate_count=rationale_duplicate_count,
            similar_entries=similar_entries,
        )
        try:
            response = judge_mod.call_judge(request, transport=judge_mod.TRANSPORT_CODEX_CLI, tool_scope=tool_scope)
        except (ModuleNotFoundError, judge_mod.JudgeConfigurationError) as exc:
            # Convert the optional external-runtime failure to the MCP server's
            # clean ImportError outcome rather than a protocol traceback.
            raise ImportError(f"stage_preview Codex judge unavailable: {exc}") from exc
        verdict = _verdict_str(response.verdict)
        preview = ActionPreview(
            verdict=verdict,
            rationale=response.judge_rationale,
            model=response.model_id,
            transport=response.judge_transport,
            authoritative=False,
        )
        new_actions.append(replace(action, preview=preview))
        previewed += 1
        if verdict == "BLOCKED":
            blocked.append({"key": action.key, "rationale": response.judge_rationale})

    new_bundle = replace(bundle, actions=tuple(new_actions))
    verification = verify_bundle_against_tree(bundle, root=ctx.root, allowlist_dir=ctx.allowlist_dir)
    if not verification.ok:
        raise ValueError("staged bundle changed during preview; refusing rewrite: " + "; ".join(verification.mismatches))
    path = write_bundle(new_bundle, staged_dir=ctx.staged_dir)
    payload = {
        "bundle_id": new_bundle.bundle_id,
        "written_path": str(path),
        "previewed": previewed,
        "blocked": blocked,
        "sign_bundle_command": _sign_bundle_command(ctx, path, new_bundle),
        "sign_bundle_plan": _sign_bundle_plan(ctx, path, new_bundle),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


# Shape-only "currently valid" statuses (a key-free diagnosis cannot prove HMAC
# validity, only shape + source binding -- so the partition is advisory; the
# operator ``rekey`` CLI's Pass-1 is the authoritative gate).
_REKEY_VALID_STATUSES = frozenset({"OK_SHAPE_ONLY", "OK_AUTHORITATIVE"})


def _tool_stage_rekey(ctx: _ServerContext, arguments: dict[str, Any]) -> str:
    """Enumerate currently-valid judge-gated entries and flag broken ones.

    Shape-only and *advisory*: without the key, HMAC validity cannot be
    determined, so a shape-valid-but-HMAC-invalid entry may be mislabeled into
    ``rekey.keys``. The operator ``rekey`` CLI's Pass-1 (keyed verify) is the
    authoritative gate. Only the env-var NAMES are recorded -- never key bytes.
    """
    _assert_no_hmac_key_in_env()
    import uuid
    from datetime import datetime

    from elspeth_lints.core.judge_signature_diagnosis import _OK_STATUSES, diagnose_judge_signatures
    from elspeth_lints.core.review_bundle import SCHEMA_VERSION, RekeyPlan, ReviewBundle, write_bundle
    from elspeth_lints.core.source_snapshot import SourceSnapshotChangedError, observe_source_snapshot

    old_key_env = _require_str_arg(arguments, "old_key_env")
    new_key_env = _require_str_arg(arguments, "new_key_env")
    bundle_id = _require_str_arg(arguments, "bundle_id") if "bundle_id" in arguments else f"stage-rekey-{uuid.uuid4().hex[:12]}"
    staged_by_arg = arguments.get("staged_by")
    staged_by = staged_by_arg if isinstance(staged_by_arg, str) and staged_by_arg else "elspeth-judge-agent"

    source_before = observe_source_snapshot(source_root=ctx.root, allowlist_dir=ctx.allowlist_dir)
    diagnosis = diagnose_judge_signatures(root=ctx.root, allowlist_dir=ctx.allowlist_dir)
    valid_keys: list[str] = []
    broken_keys: list[str] = []
    for item in diagnosis.items:
        if item.status in _REKEY_VALID_STATUSES:
            valid_keys.append(item.key)
        elif item.status not in _OK_STATUSES:
            # Not OK and not PRE_JUDGE -> a judge-gated entry that does not
            # currently verify shape/binding. (PRE_JUDGE is non-judge-gated and
            # is not part of the rekey set.)
            broken_keys.append(item.key)

    source_after = observe_source_snapshot(source_root=ctx.root, allowlist_dir=ctx.allowlist_dir)
    if source_before != source_after:
        raise SourceSnapshotChangedError("source binding changed while deriving stage_rekey diagnosis")

    rekey = RekeyPlan(
        old_key_env=old_key_env,
        new_key_env=new_key_env,
        keys=tuple(sorted(valid_keys)),
        broken_keys=tuple(sorted(broken_keys)),
    )
    bundle = ReviewBundle(
        bundle_id=bundle_id,
        schema_version=SCHEMA_VERSION,
        created_at=datetime.now(UTC).isoformat(),
        staged_by=staged_by,
        root=str(ctx.root),
        allowlist_dir=str(ctx.allowlist_dir),
        source_rev=source_after.source_rev,
        source_dirty=source_after.source_dirty,
        source_snapshot_sha256=source_after.source_snapshot_sha256,
        actions=(),
        rekey=rekey,
    )
    path = write_bundle(bundle, staged_dir=ctx.staged_dir)
    payload = {
        "bundle_id": bundle.bundle_id,
        "written_path": str(path),
        "old_key_env": old_key_env,
        "new_key_env": new_key_env,
        "valid_count": len(rekey.keys),
        "broken_count": len(rekey.broken_keys),
        "rekey_command": _rekey_command(ctx, path, old_key_env=old_key_env, new_key_env=new_key_env),
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


_TOOLS.update(
    {
        "verify_signatures": _ToolSpec(
            description=(
                "Read-only, structurally key-free signature diagnosis of the tier_model allowlist "
                "(always shape-only; the authoritative HMAC recompute is the operator CLI)."
            ),
            input_schema={"type": "object", "properties": {}},
            handler=_tool_verify_signatures,
        ),
        "stage_status": _ToolSpec(
            description=(
                "Verify the exact source binding, refuse stale bundles, then summarise per-lane/kind "
                "counts and preview outcomes and emit the paste-ready operator command plus a "
                "per-lane plan pricing each scope in judge calls."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "bundle_id": {"type": "string", "description": "Bundle id (file is <staged-dir>/<id>.json)"},
                },
                "required": ["bundle_id"],
            },
            handler=_tool_stage_status,
        ),
        "stage_scan": _ToolSpec(
            description=(
                "Survey the source tree + tier_model allowlist into an authority-free worklist bundle "
                "(drift_repair / rotation / stale_delete / new_judgment lanes). Key-free, no LLM."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "bundle_id": {"type": "string", "description": "Bundle id to write (default: generated)"},
                    "staged_by": {"type": "string", "description": "Agent/operator label recorded on the bundle"},
                },
            },
            handler=_tool_stage_scan,
        ),
        "stage_annotate": _ToolSpec(
            description=(
                "Attach agent-authored draft rationales to staged justify actions (the key-free "
                "rationale-custody surface: stage_preview judges them and the operator sign-bundle "
                "carries them into the authoritative judge call). Clears any existing preview on "
                "annotated actions; refuses stale bundles, unknown keys, and empty rationales."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "bundle_id": {"type": "string", "description": "Bundle id (file is <staged-dir>/<id>.json)"},
                    "rationales": {
                        "type": "object",
                        "description": "Map of staged justify action key -> site-specific rationale text",
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["bundle_id", "rationales"],
            },
            handler=_tool_stage_annotate,
        ),
        "stage_preview": _ToolSpec(
            description=(
                "Fully verify the sign bundle, run the read-only Codex CLI judge over each "
                "new_judgment action, reverify before overwrite, and record a NON-authoritative "
                "preview verdict (never signs; stale bundles receive no judge call). "
                "Requires an installed + authenticated Codex CLI and the [mcp] extra."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "bundle_id": {"type": "string", "description": "Bundle id (file is <staged-dir>/<id>.json)"},
                },
                "required": ["bundle_id"],
            },
            handler=_tool_stage_preview,
        ),
        "stage_rekey": _ToolSpec(
            description=(
                "Enumerate currently-valid judge-gated entries and flag broken ones into a rekey "
                "bundle (records env-var NAMES only -- never key bytes). Shape-only/advisory; the "
                "operator rekey CLI's Pass-1 is the authoritative gate."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "old_key_env": {"type": "string", "description": "NAME of the env var holding the OLD key"},
                    "new_key_env": {"type": "string", "description": "NAME of the env var holding the NEW key"},
                    "bundle_id": {"type": "string", "description": "Bundle id to write (default: generated)"},
                    "staged_by": {"type": "string", "description": "Agent/operator label recorded on the bundle"},
                },
                "required": ["old_key_env", "new_key_env"],
            },
            handler=_tool_stage_rekey,
        ),
    }
)


def create_server(
    *,
    root: Path,
    allowlist_dir: Path,
    staged_dir: Path,
) -> Any:
    """Create the ``elspeth-judge`` MCP server bound to the given roots."""
    from mcp.server import Server
    from mcp.types import CallToolResult, TextContent, Tool

    ctx = _ServerContext(root=Path(root), allowlist_dir=Path(allowlist_dir), staged_dir=Path(staged_dir))
    server = Server("elspeth-judge")

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_tools() -> list[Tool]:
        return [Tool(name=name, description=spec.description, inputSchema=spec.input_schema) for name, spec in _TOOLS.items()]

    @server.call_tool()  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> CallToolResult | list[TextContent]:
        outcome = _run_tool(ctx, name, arguments)
        if outcome.is_error:
            return CallToolResult(content=[TextContent(type="text", text=outcome.text)], isError=True)
        return [TextContent(type="text", text=outcome.text)]

    return server


async def run_server(*, root: Path, allowlist_dir: Path, staged_dir: Path) -> None:
    """Run the server over stdio."""
    from mcp.server.stdio import stdio_server

    server = create_server(root=root, allowlist_dir=allowlist_dir, staged_dir=staged_dir)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for ``python -m elspeth_lints.mcp`` / the console script."""
    import asyncio

    parser = argparse.ArgumentParser(
        prog="elspeth-judge-mcp",
        description="ELSPETH key-free judge/signature staging MCP server",
    )
    parser.add_argument("--root", type=Path, default=Path("src/elspeth"), help="Source tree to scan")
    parser.add_argument(
        "--allowlist-dir",
        type=Path,
        default=Path("config/cicd/enforce_tier_model"),
        help="Directory of per-module tier_model allowlist YAML files",
    )
    parser.add_argument(
        "--staged-dir",
        type=Path,
        default=Path(".elspeth/staged-reviews"),
        help="Directory where staged review bundles are written/read",
    )
    args = parser.parse_args(argv)
    asyncio.run(run_server(root=args.root, allowlist_dir=args.allowlist_dir, staged_dir=args.staged_dir))


if __name__ == "__main__":
    main()
