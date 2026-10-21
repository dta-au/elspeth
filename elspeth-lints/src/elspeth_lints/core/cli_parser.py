"""Argument grammar and pure argument converters for elspeth-lints."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from elspeth_lints.core.allowlist import AuditReviewVerdict, is_substantive_audit_anchor
from elspeth_lints.core.judge import TRANSPORT_AGENT, TRANSPORT_CODEX_CLI, TRANSPORT_OPENROUTER

JUSTIFY_RATIONALE_MAX_BYTES = 8 * 1024
OPERATOR_OVERRIDE_TOKEN_ENV = "ELSPETH_JUDGE_OVERRIDE_TOKEN"
OPERATOR_OVERRIDE_TOKEN_SHA256_ENV = "ELSPETH_JUDGE_OVERRIDE_TOKEN_SHA256"
_MAX_AUDIT_IDENTITY_LENGTH = 200


# CLI spelling -> stored transport identity.  Keep ``agent`` as the legacy
# Claude Agent SDK spelling and expose Codex explicitly rather than silently
# rebinding old signed metadata to a different transport.
_CLI_TRANSPORT_CHOICES: dict[str, str] = {
    "openrouter": TRANSPORT_OPENROUTER,
    "agent": TRANSPORT_AGENT,
    "codex-cli": TRANSPORT_CODEX_CLI,
}


def _add_judge_transport_arg(parser: argparse.ArgumentParser) -> None:
    """Attach the shared ``--judge-transport`` flag to ``parser``.

    Used by both ``justify`` and ``reaudit`` so the operator-facing spelling
    and help text stay identical. The flag selects which provider produces
    the verdict; the resolved stored value is looked up via
    ``_CLI_TRANSPORT_CHOICES[args.judge_transport]`` at the call site.
    """
    parser.add_argument(
        "--judge-transport",
        choices=tuple(_CLI_TRANSPORT_CHOICES),
        default="openrouter",
        help=(
            "Which transport produces the verdict. 'openrouter' (default) uses "
            "the OpenAI-compatible SDK with temperature=0 (reproducible). 'agent' "
            "uses the Claude Agent SDK and requires the [judge-agent] extra plus "
            "Claude Code auth. 'codex-cli' uses the installed, authenticated Codex "
            "CLI with a secret-stripped subprocess and sealed tool surface. Local "
            "agent transports cannot pin temperature and are less reproducible "
            "(see reaudit)."
        ),
    )


def _add_judge_tools_arg(parser: argparse.ArgumentParser) -> None:
    """Attach the ``--judge-tools`` flag (read-only investigation mode).

    'none' (default) keeps the blinded judge — it sees only the excerpt.
    'readonly' lets the agent transport Read/Grep/Glob throughout the checkout +
    allowlist dir (fail-closed transport guard) to resolve a would-be block for
    lack of context. Requires ``--judge-transport agent`` or ``codex-cli`` (the
    OpenRouter path has no tool loop). Since 2026-07-09 signing paths accept it too — the
    excerpt-blinded signing judge systematically misjudged boundary code it
    could not see, forcing bulk operator overrides. In readonly mode the
    judge's rationale is passed through ``scrub_secrets`` before it is printed
    or persisted, so raw tool-read bytes still cannot enter signed YAML.
    """
    parser.add_argument(
        "--judge-tools",
        choices=("none", "readonly"),
        default="none",
        help=(
            "Read-only tool access for the judge. 'none' (default) is blinded "
            "(excerpt only). 'readonly' lets a local agent transport Read/Grep/Glob "
            "throughout the checkout (including tests, docs, scripts and config) "
            "+ allowlist dir to investigate; requires --judge-transport "
            "agent or codex-cli. Less reproducible than blinded mode. Valid on signing paths "
            "since 2026-07-09; readonly-mode judge rationales are secret-scrubbed "
            "before being persisted into signed allowlist entries."
        ),
    )


def _non_empty_string(value: str) -> str:
    """Argparse ``type=`` callable that rejects empty / whitespace-only input.

    Used for ``justify --owner``: the owner field is the audit signal for
    who claimed responsibility for a suppression and must be a real
    identity. ``argparse`` swallows the raised ``ArgumentTypeError`` and
    prints the message inline with its standard "argument --owner:
    invalid value" frame, so the operator sees a single clear error and
    the call exits 2.
    """
    stripped = value.strip()
    if not stripped:
        raise argparse.ArgumentTypeError(
            "must be a non-empty audit identity (e.g. an agent name or "
            "human operator name); empty / whitespace-only values are "
            "rejected because the owner field is the audit signal for "
            "who claimed responsibility for the suppression."
        )
    if any(char in value for char in ("\n", "\r", "\x85")):
        raise argparse.ArgumentTypeError(
            "must be a single-line audit identity; embedded line breaks cannot be represented safely in the inline YAML owner field."
        )
    if not is_substantive_audit_anchor(value):
        raise argparse.ArgumentTypeError(
            "must be a substantive audit identity with at least two "
            "alphanumeric characters; values like 'x', '.', '~', or '-' "
            "cannot safely distinguish rotation-grandfathered entries."
        )
    if len(value) > _MAX_AUDIT_IDENTITY_LENGTH:
        raise argparse.ArgumentTypeError(
            f"must be at most {_MAX_AUDIT_IDENTITY_LENGTH} characters; an audit "
            "identity is a short owner/reviewer name, not free text. An unbounded "
            "value bloats the entry, the grandfathering discriminator, and the "
            "text replayed verbatim into future judge prompts."
        )
    return value


def _positive_int(value: str) -> int:
    """Argparse ``type=`` callable that rejects non-positive integers."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return parsed


def _timezone_aware_datetime_arg(value: str) -> datetime:
    """Argparse ``type=`` callable that accepts only timezone-aware timestamps."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"must be a valid ISO-8601 timestamp; got {value!r}") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("must include a timezone offset")
    return parsed


def _bounded_rationale_string(value: str) -> str:
    """Argparse ``type=`` callable for operator-supplied judge rationales."""
    stripped = value.strip()
    if not stripped:
        raise argparse.ArgumentTypeError(
            "must be a non-empty rationale; empty / whitespace-only values would produce an audit entry with no operator explanation."
        )
    byte_count = len(value.encode("utf-8"))
    if byte_count > JUSTIFY_RATIONALE_MAX_BYTES:
        raise argparse.ArgumentTypeError(
            f"must be at most {JUSTIFY_RATIONALE_MAX_BYTES} bytes when UTF-8 "
            f"encoded; got {byte_count} bytes. Shorten the rationale and keep "
            "supporting detail in the linked issue or review notes."
        )
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="elspeth-lints")
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Run static-analysis rules")
    check.add_argument(
        "--rules",
        default=None,
        help=(
            "Rule selection, required: 'all' for every registered rule, a comma-separated "
            "list of rule ids, or 'nothing' for the empty skeleton run"
        ),
    )
    check.add_argument("--rule-set", choices=("static", "full"), default="static")
    check.add_argument("--format", choices=("text", "json", "sarif", "github"), default="text")
    check.add_argument("--root", type=Path, default=Path.cwd())
    check.add_argument(
        "--repo-root",
        type=Path,
        help=(
            "Repository working tree root for rules that resolve repository-relative "
            "evidence paths. When omitted, rules derive it from --root using their "
            "documented fallback; when supplied, this explicit value wins."
        ),
    )
    check.add_argument(
        "--allowlist-dir",
        type=Path,
        help=(
            "Override every rule's per-rule allowlist directory with this one. "
            "Useful for shadow runs and cross-branch comparisons. "
            "When unset, each rule resolves its own per-rule default directory."
        ),
    )
    check.add_argument(
        "--fail-on-inert",
        action="store_true",
        help=(
            "Fail when a selected incremental rule matches no non-fixture Python files. "
            "Use for full-corpus gates; partial --files selections may legitimately reach only a subset of selected rules."
        ),
    )
    check.add_argument("--files", nargs="*", type=Path)

    rotate = subparsers.add_parser(
        "rotate",
        help="Rotate stale fingerprints in tier_model allowlist entries (mechanical, no judge)",
    )
    rotate.add_argument(
        "--root",
        type=Path,
        default=Path("src/elspeth"),
        help="Source tree to scan. Default: src/elspeth.",
    )
    rotate.add_argument(
        "--allowlist-dir",
        type=Path,
        required=True,
        help="Directory of per-module tier_model allowlist YAML files",
    )
    rotate.add_argument("--dry-run", action="store_true", help="Plan only; do not modify files")
    rotate.add_argument(
        "--rotation-log",
        type=Path,
        default=Path(".elspeth/rotations.log"),
        help=("JSONL audit manifest written on non-dry-run rotation applies. Default: .elspeth/rotations.log."),
    )
    rotate.add_argument(
        "--no-auto-pair-symmetric",
        dest="auto_pair_symmetric",
        action="store_false",
        default=True,
        help=(
            "Surface N:N prefix groups (same count of findings and entries, "
            "N>=2) as ambiguous instead of auto-pairing them by sorted "
            "fingerprint. Default is to auto-pair, which is safe for "
            "tier_model because its reason metadata is qname-level."
        ),
    )
    rotate.add_argument(
        "--remove-stale",
        dest="remove_stale",
        action="store_true",
        default=False,
        help=(
            "Remove stale allowlist entries (entries whose underlying "
            "violation site no longer exists). Default is to keep stale "
            "entries and surface them for operator-confirmed cleanup."
        ),
    )
    rotate.add_argument(
        "--no-remove-stale",
        dest="remove_stale",
        action="store_false",
        help=("Deprecated compatibility flag. Stale removal is already off by default; pass --remove-stale to opt into deletion."),
    )
    rotate.add_argument(
        "--accept-todo-debt",
        action="store_true",
        help=(
            "Allow apply even when the plan reports historical TODO-stub "
            "allowlist entries. Default is to refuse so placeholder debt "
            "cannot be carried through a rotation silently."
        ),
    )
    rotate.add_argument(
        "--format",
        dest="rotate_format",
        choices=("text", "json"),
        default="text",
        help="Output format for the rotation plan summary",
    )

    justify = subparsers.add_parser(
        "justify",
        help="Gate a proposed allowlist entry through the cicd-judge (Opus)",
    )
    justify.add_argument(
        "--root",
        type=Path,
        default=Path("src/elspeth"),
        help="Source tree to scan (used to re-run the rule on --file-path)",
    )
    justify.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help=(
            "Repository root for trust-boundary honesty rules that resolve "
            "repo-relative evidence such as test_ref nodeids. Defaults to the "
            "rule's source-root heuristic."
        ),
    )
    justify.add_argument(
        "--allowlist-dir",
        type=Path,
        default=Path("config/cicd/enforce_tier_model"),
        help="Directory of per-module allowlist YAML files where the entry will be written",
    )
    justify.add_argument(
        "--file-path",
        type=str,
        required=True,
        help="Source file (path relative to --root) containing the finding being suppressed",
    )
    justify.add_argument(
        "--rule",
        type=str,
        default="trust_tier.tier_model",
        help="Rule id whose finding is being suppressed",
    )
    justify.add_argument(
        "--symbol",
        type=str,
        required=True,
        help=(
            "Qualified symbol name that must uniquely identify one current finding "
            "(e.g. 'MyClass._method'). Use the literal '_module_' for module-scope "
            "findings (matches the canonical-key sentinel)."
        ),
    )
    justify.add_argument(
        "--fingerprint",
        type=str,
        default=None,
        help=(
            "Exact finding fingerprint used to disambiguate when --symbol "
            "matches multiple current findings. Accepts the bare fingerprint "
            "or fp=<fingerprint>."
        ),
    )
    justify.add_argument(
        "--rationale",
        type=_bounded_rationale_string,
        required=True,
        help=(f"Agent's proposed justification for the suppression (max {JUSTIFY_RATIONALE_MAX_BYTES} UTF-8 bytes)."),
    )
    justify.add_argument(
        "--owner",
        type=_non_empty_string,
        required=True,
        help=(
            "Audit identity (agent name or human operator) requesting this "
            "exemption. Recorded verbatim in the entry's `owner` field; this "
            "is the audit signal for who took responsibility for the "
            "suppression."
        ),
    )
    justify.add_argument(
        "--operator-override",
        action="store_true",
        help=(
            "Bypass the judge's verdict. The judge is still called (to record "
            "what the model would have said) but the entry is written with "
            "verdict=OVERRIDDEN_BY_OPERATOR. Requires "
            f"{OPERATOR_OVERRIDE_TOKEN_ENV} and "
            f"{OPERATOR_OVERRIDE_TOKEN_SHA256_ENV} to authorize the override."
        ),
    )
    justify.add_argument(
        "--max-tokens",
        type=_positive_int,
        default=None,
        help=(
            "Maximum completion tokens for the judge response. Defaults to "
            "the judge module's DEFAULT_JUDGE_MAX_TOKENS; increase when the "
            "provider returns finish_reason=length."
        ),
    )
    justify.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the proposed entry and the judge verdict; do not write",
    )
    justify.add_argument(
        "--format",
        dest="justify_format",
        choices=("text", "json"),
        default="text",
        help="Output format for the verdict",
    )
    _add_judge_transport_arg(justify)
    _add_judge_tools_arg(justify)

    audit_verdict = subparsers.add_parser(
        "audit-verdict",
        help="Attach a human post-review verdict to a judge-accepted allowlist entry",
    )
    audit_verdict.add_argument(
        "--allowlist-dir",
        type=Path,
        default=Path("config/cicd/enforce_tier_model"),
        help="Directory of per-module allowlist YAML files containing the entry",
    )
    audit_verdict.add_argument(
        "--key",
        required=True,
        help="Exact allow_hits key whose prior ACCEPTED judge verdict is being reviewed",
    )
    audit_verdict.add_argument(
        "--verdict",
        choices=tuple(verdict.value for verdict in AuditReviewVerdict),
        required=True,
        help="Post-review verdict to attach",
    )
    audit_verdict.add_argument(
        "--reviewer",
        type=_non_empty_string,
        required=True,
        help="Audit identity of the human/operator recording the review",
    )
    audit_verdict.add_argument(
        "--rationale",
        type=_bounded_rationale_string,
        required=True,
        help=(f"Reason the prior judge-accepted suppression is being marked wrong (max {JUSTIFY_RATIONALE_MAX_BYTES} UTF-8 bytes)."),
    )
    audit_verdict.add_argument(
        "--reviewed-at",
        type=_timezone_aware_datetime_arg,
        default=None,
        help="Timezone-aware ISO-8601 review timestamp. Defaults to current UTC.",
    )
    audit_verdict.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and print the review block; do not write",
    )

    reaudit = subparsers.add_parser(
        "reaudit",
        help=("Re-run the cicd-judge across existing allowlist entries to detect decay (read-only on YAML; emits a triage report)"),
    )
    reaudit.add_argument(
        "--root",
        type=Path,
        default=Path("src/elspeth"),
        help="Source tree to scan for the entries' underlying findings",
    )
    reaudit.add_argument(
        "--allowlist-dir",
        type=Path,
        default=Path("config/cicd/enforce_tier_model"),
        help="Directory of per-module allowlist YAML files to reaudit",
    )
    reaudit.add_argument(
        "--rule",
        type=str,
        default="trust_tier.tier_model",
        help=("Rule-package selector (controls which scanner re-derives findings). Currently only 'trust_tier.tier_model' is supported."),
    )
    reaudit.add_argument(
        "--since",
        type=str,
        default=None,
        help=(
            "ISO-8601 date or timestamp; entries whose judge_recorded_at is at "
            "or after this point are skipped (their judgment is fresh; no decay). "
            "Pre-judge entries (no judge_recorded_at) are never filtered out by "
            "--since — they are gated by --include-pre-judge instead."
        ),
    )
    reaudit.add_argument(
        "--limit",
        type=_positive_int,
        default=None,
        help=(
            "Only reaudit the first N entries surviving the other filters. "
            "Use for incremental sweeps — the ~700-entry allowlist should "
            "not be re-judged in one pass."
        ),
    )
    reaudit.add_argument(
        "--max-calls",
        type=_positive_int,
        default=None,
        help=(
            "Stop this invocation after N actual judge calls. Unlike --limit, "
            "this is a spend guard: entries resolved before the judge boundary "
            "do not consume it, and an exhausted budget leaves the sweep "
            "incomplete so it can be resumed later."
        ),
    )
    reaudit.add_argument(
        "--include-pre-judge",
        action="store_true",
        help=(
            "Also reaudit entries with no stored judge verdict (the historical "
            "pre-judge corpus). Off by default because the pre-judge entry set "
            "is too large for routine sweeps."
        ),
    )
    reaudit.add_argument(
        "--format",
        dest="reaudit_format",
        choices=("text", "json", "markdown"),
        default="text",
        help="Report format. Markdown is the most operator-readable for triage.",
    )
    reaudit.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write report to this path instead of stdout.",
    )
    # T6b crash-recovery surfaces — mutually exclusive with each other.
    # Without either flag, the command starts a fresh sweep and prints
    # the new run_id to stderr so the operator can recover from a crash.
    reaudit_recovery = reaudit.add_mutually_exclusive_group()
    reaudit_recovery.add_argument(
        "--resume",
        dest="resume_run_id",
        type=str,
        default=None,
        help=(
            "Resume a prior killed/crashed sweep by run_id. Reads the "
            "<allowlist-dir>/.reaudit-state/<run_id>.jsonl sidecar, skips "
            "entries already classified, continues from the first un-classified "
            "entry. Crashes if the allowlist or filter arguments have drifted "
            "between the original sweep and this resume."
        ),
    )
    reaudit_recovery.add_argument(
        "--render-incomplete",
        dest="render_incomplete_run_id",
        type=str,
        default=None,
        help=(
            "Render the partial report captured by a killed/crashed sweep "
            "without re-running the judge. Reads the sidecar at "
            "<allowlist-dir>/.reaudit-state/<run_id>.jsonl, reconstructs the "
            "outcomes, and renders via the same formatter as a normal run. "
            "Exits non-zero because the rendered sweep is incomplete by "
            "definition (a clean sweep would not need this flag)."
        ),
    )
    _add_judge_transport_arg(reaudit)
    _add_judge_tools_arg(reaudit)

    diagnose_judge_signatures = subparsers.add_parser(
        "diagnose-judge-signatures",
        help=(
            "Read-only signed judge metadata diagnosis. Agent-safe: does not write "
            "allowlist YAML and does not require ELSPETH_JUDGE_METADATA_HMAC_KEY; "
            "when the key is present in an operator-held shell, also verifies HMACs."
        ),
    )
    diagnose_judge_signatures.add_argument(
        "--root",
        type=Path,
        default=Path("src/elspeth"),
        help="Source tree to scan for the entries' underlying findings",
    )
    diagnose_judge_signatures.add_argument(
        "--allowlist-dir",
        type=Path,
        default=Path("config/cicd/enforce_tier_model"),
        help="Directory of per-module allowlist YAML files to inspect read-only",
    )
    diagnose_judge_signatures.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            "Dotenv file containing diagnosis-relevant keys such as "
            "ELSPETH_JUDGE_METADATA_HMAC_KEY. Existing environment values win; "
            "unrelated keys are ignored."
        ),
    )
    diagnose_judge_signatures.add_argument(
        "--format",
        dest="diagnose_format",
        choices=("text", "json"),
        default="text",
        help="Output format for the diagnosis report",
    )

    sign_judge_signatures = subparsers.add_parser(
        "sign-judge-signatures",
        help=(
            "OPERATOR-ONLY: re-run justify for diagnosed signed allowlist drift, "
            "optionally adding manifest-listed first-time justifications. Loads "
            "operator secrets from --env-file, removes stale diagnosed rows, and "
            "writes fresh signed allow_hits entries."
        ),
    )
    sign_judge_signatures.add_argument(
        "--root",
        type=Path,
        default=Path("src/elspeth"),
        help="Source tree to scan for the entries' underlying findings",
    )
    sign_judge_signatures.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help=("Repository root used by trust-boundary scanners. Omit for the tier-model-only default."),
    )
    sign_judge_signatures.add_argument(
        "--allowlist-dir",
        type=Path,
        default=Path("config/cicd/enforce_tier_model"),
        help="Active directory of per-module allowlist YAML files to replace coherently on success",
    )
    sign_judge_signatures.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            "Dotenv file containing signing-relevant keys. Existing environment "
            "values win; unrelated keys are ignored and secret values are not printed."
        ),
    )
    sign_judge_signatures.add_argument(
        "--owner",
        type=_non_empty_string,
        required=True,
        help="Audit identity (operator) recorded on freshly signed entries.",
    )
    sign_judge_signatures.add_argument(
        "--manifest",
        type=Path,
        action="append",
        default=[],
        help=(
            "YAML file containing entries: [{file_path, rule, symbol, fingerprint, "
            "rationale}] for brand-new findings that diagnosis cannot infer."
        ),
    )
    sign_judge_signatures.add_argument(
        "--operator-override",
        action="store_true",
        help=("Forward --operator-override to each justify call. Requires the operator override token environment exactly like justify."),
    )
    sign_judge_signatures.add_argument(
        "--max-tokens",
        type=_positive_int,
        default=None,
        help="Override the judge response max_tokens for each justify call.",
    )
    sign_judge_signatures.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the exact repair plan without calling the judge, removing stale rows, or writing signed entries.",
    )
    sign_judge_signatures.add_argument(
        "--format",
        dest="justify_format",
        choices=("text", "json"),
        default="text",
        help="Per-entry justify output format.",
    )
    _add_judge_transport_arg(sign_judge_signatures)
    _add_judge_tools_arg(sign_judge_signatures)

    sign_bundle = subparsers.add_parser(
        "sign-bundle",
        help=(
            "OPERATOR-ONLY: re-verify a staged review bundle against the source "
            "tree and fire it. The ONLY place a judge signature is minted from a "
            "bundle. Re-derives every binding from the tree and aborts on any "
            "staleness BEFORE creating a transaction; deterministic stale-delete / "
            "safe-rotation actions run first, then drift_repair / new_judgment run the "
            "real judge (re-judging prevents laundering a stale verdict over "
            "changed content). All actions write to a recoverable private copy and "
            "the active allowlist changes only through one coherent atomic directory "
            "exchange after final re-verification. BLOCK/failure/interruption prints "
            "a --resume command; --dry-run creates no transaction. Requires "
            "ELSPETH_JUDGE_METADATA_HMAC_KEY (an agent may PROPOSE the bundle, only "
            "an operator-held environment signs it)."
        ),
    )
    sign_bundle.add_argument("bundle", type=Path, help="Path to the staged review bundle JSON to fire.")
    # Mirror sign-judge-signatures' arg set so the drift_repair lane's
    # _namespace_for_signing_spec(spec, args) finds every attr it reads.
    sign_bundle.add_argument(
        "--root",
        type=Path,
        default=Path("src/elspeth"),
        help="Source tree to re-scan for the entries' underlying findings",
    )
    sign_bundle.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root used by trust-boundary scanners. Omit for the tier-model-only default.",
    )
    sign_bundle.add_argument(
        "--allowlist-dir",
        type=Path,
        default=Path("config/cicd/enforce_tier_model"),
        help="Directory of per-module allowlist YAML files to repair in place",
    )
    sign_bundle.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            "Dotenv file containing signing-relevant keys. Existing environment "
            "values win; unrelated keys are ignored and secret values are not printed."
        ),
    )
    sign_bundle.add_argument(
        "--owner",
        type=_non_empty_string,
        required=True,
        help="Audit identity (operator) recorded on freshly signed entries.",
    )
    sign_bundle.add_argument(
        "--operator-override",
        action="store_true",
        help="Forward --operator-override to each justify call. Requires the operator override token environment exactly like justify.",
    )
    sign_bundle.add_argument(
        "--max-tokens",
        type=_positive_int,
        default=None,
        help="Override the judge response max_tokens for each justify call.",
    )
    sign_bundle.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the verify + per-lane plan without calling the judge or "
            "creating/writing a transaction, rotation log, or allowlist entry."
        ),
    )
    sign_bundle.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt before creating/resuming the private transaction.",
    )
    sign_bundle_scope = sign_bundle.add_mutually_exclusive_group()
    sign_bundle_scope.add_argument(
        "--lanes",
        type=_sign_bundle_lanes,
        default=None,
        metavar="LANE[,LANE]",
        help=(
            "Scope the transaction to a comma-separated subset of bundle lanes: "
            "'resign' (drift_repair + rotation + stale_delete — new rationale optional) "
            "and/or 'new_judgment' (justify — judges the staged "
            "rationale). Unselected actions are never attempted, never judged, "
            "and stay exactly as they are in the allowlist (fail-closed); the "
            "coherent publish covers the selected lanes only. The selection is "
            "journalled in the transaction and a resume must use the same "
            "--lanes value (omitting the flag on resume inherits it). Default: "
            "all lanes."
        ),
    )
    sign_bundle_scope.add_argument(
        "--stale-key",
        default=None,
        metavar="KEY",
        help=(
            "Retire exactly one verified orphan by its full key, without judge calls. "
            "The complete bundle is still verified. Resume inherits this immutable scope; "
            "exact-scope journals require this version or newer."
        ),
    )
    sign_bundle.add_argument(
        "--continue-on-block",
        action="store_true",
        help=(
            "Do not stop the transaction on a judged BLOCKED verdict (exit-1 action "
            "failure): journal the action as blocked, leave its entry fail-closed "
            "(stale or absent — never signed), and continue with the remaining "
            "actions. Verification/infrastructure failures (exit 2) still stop the "
            "transaction. On completion the successful actions publish coherently "
            "and the blocked keys are printed as a remediation worklist; the "
            "command exits 3 instead of 0 so a partially-signed run is never "
            "mistaken for a clean one. Blocked actions stay skipped on resume "
            "(re-judging a recorded BLOCK would be verdict shopping); remediate "
            "the code or rationale and re-stage instead."
        ),
    )
    sign_bundle.add_argument(
        "--resume",
        type=Path,
        default=None,
        metavar="TRANSACTION_DIR",
        help=(
            "Resume a preserved sign-bundle transaction. Re-verifies the bundle, "
            "live source tree, unchanged active allowlist, transaction journal, "
            "and every previously produced authoritative signature before continuing."
        ),
    )
    sign_bundle.add_argument(
        "--rotation-log",
        type=Path,
        default=Path(".elspeth/rotations.log"),
        help=(
            "JSONL audit manifest finalized only with coherent publish when a rotation action applies, so "
            "check-rotation-audit finds a record for the allowlist key rewrite "
            "(mirrors `rotate --rotation-log`). Default: .elspeth/rotations.log."
        ),
    )
    sign_bundle.add_argument(
        "--format",
        dest="justify_format",
        choices=("text", "json"),
        default="text",
        help="Per-entry justify output format.",
    )
    _add_judge_transport_arg(sign_bundle)
    _add_judge_tools_arg(sign_bundle)
    sign_bundle.add_argument(
        "--judge-concurrency",
        type=int,
        default=1,
        metavar="N",
        help=(
            "Run up to N judge calls at once for new_judgment (justify) actions. Only the judge "
            "subprocess overlaps: every candidate write, decision event, and journal entry is "
            "still performed by one writer in bundle order, so crash/resume semantics are "
            "unchanged. drift_repair, rotation, and stale_delete always run one at a time. "
            f"1..{_SIGN_BUNDLE_MAX_JUDGE_CONCURRENCY}; default 1."
        ),
    )

    rekey = subparsers.add_parser(
        "rekey",
        help=(
            "OPERATOR-ONLY: rotate the judge-metadata HMAC key. Verifies every "
            "judge-gated entry under the OLD key, recomputes its signature under the "
            "NEW key, and atomically rewrites ONLY the judge_metadata_signature line "
            "(binding + audit lines stay byte-identical -- a scheme-preserving "
            "signature-only swap). Re-derives the full judge-gated set from the live "
            "tree at fire time; idempotent/re-runnable (Pass-1 accepts OLD or NEW, "
            "Pass-2 skips already-NEW), so a partial run self-heals. Requires BOTH "
            "key env vars (named by --old-key-env / --new-key-env); fails closed "
            "without them. An agent may PROPOSE the bundle; only an operator-held "
            "environment holds the keys and signs."
        ),
    )
    # ``--in`` maps to a python keyword as a dest; bind an explicit non-keyword dest.
    rekey.add_argument(
        "--in",
        dest="bundle_path",
        type=Path,
        required=True,
        help="Path to the staged rekey bundle JSON (its RekeyPlan env-var names cross-check the flags).",
    )
    rekey.add_argument(
        "--old-key-env",
        dest="old_key_env",
        type=_non_empty_string,
        required=True,
        help="NAME of the env var holding the OLD (current) HMAC key bytes. The key never appears on the CLI.",
    )
    rekey.add_argument(
        "--new-key-env",
        dest="new_key_env",
        type=_non_empty_string,
        required=True,
        help="NAME of the env var holding the NEW (target) HMAC key bytes.",
    )
    rekey.add_argument(
        "--root",
        type=Path,
        default=Path("src/elspeth"),
        help="Source tree (for the canonical-pair baseline-regen gate; rekey itself loads source-less).",
    )
    rekey.add_argument(
        "--allowlist-dir",
        type=Path,
        default=Path("config/cicd/enforce_tier_model"),
        help="Directory of per-module allowlist YAML files to re-key in place",
    )
    rekey.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the verify + planned re-key count without verifying-and-writing any signature.",
    )
    rekey.add_argument(
        "--yes",
        action="store_true",
        help="Skip the interactive confirmation prompt before the (destructive) write phase.",
    )

    migrate_judge_scope = subparsers.add_parser(
        "migrate-judge-scope",
        help=(
            "Re-sign currently-valid v1 (file_fingerprint) judge-gated entries as "
            "v2 (scope_fingerprint) without re-running the LLM judge. OPERATOR-ONLY: "
            "writes signed metadata; requires ELSPETH_JUDGE_METADATA_HMAC_KEY (same "
            "custody constraint as `justify` — an agent may PROPOSE this invocation, "
            "only an operator-held environment runs and signs it)."
        ),
    )
    migrate_judge_scope.add_argument(
        "--root",
        type=Path,
        default=Path("src/elspeth"),
        help="Source tree to scan for the entries' underlying findings",
    )
    migrate_judge_scope.add_argument(
        "--allowlist-dir",
        type=Path,
        default=Path("config/cicd/enforce_tier_model"),
        help="Directory of per-module allowlist YAML files to migrate in place",
    )
    migrate_judge_scope.add_argument(
        "--owner",
        type=_non_empty_string,
        required=True,
        help=(
            "Audit identity (operator) running the mechanical migration. Recorded "
            "verbatim in the report; the migration carries each entry's existing "
            "`owner` field forward unchanged (it does not rewrite ownership)."
        ),
    )
    migrate_judge_scope.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and report what WOULD migrate; write nothing.",
    )

    dump_edges = subparsers.add_parser("dump-edges", help="Dump import edges for architecture review")
    dump_edges.add_argument("--root", type=Path, default=Path.cwd())
    dump_edges.add_argument("--format", choices=("json", "mermaid", "dot"), default="json")
    dump_edges.add_argument("--output", type=Path)
    dump_edges.add_argument("--include-layer", action="append", choices=("L0", "L1", "L2", "L3"))
    dump_edges.add_argument("--collapse-to-subsystem", dest="collapse_to_subsystem", action="store_true", default=True)
    dump_edges.add_argument("--no-collapse", dest="collapse_to_subsystem", action="store_false")
    dump_edges.add_argument("--no-timestamp", action="store_true", default=False)
    dump_edges.add_argument("--exclude", action="append", default=[])

    # CI gate: every new ``allow_hits`` entry in this PR must carry the
    # atomic judge metadata quartet. See ``judge_coverage.py`` docstring
    # for the convergent finding C1 context, the rotation-grandfather
    # policy, and the scope boundary (``allow_hits:`` is the only
    # judge-covered entry schema; non-empty legacy entry schemas are
    # reported as unrecognized rather than silently skipped).
    check_coverage = subparsers.add_parser(
        "check-judge-coverage",
        help=(
            "Fail if any new allowlist entry in this PR is missing judge metadata. "
            "Diffs HEAD against --baseline-ref; entries present in baseline (modulo "
            "fingerprint rotation) are grandfathered."
        ),
    )
    check_coverage.add_argument(
        "--baseline-ref",
        required=True,
        help=(
            "Git ref to diff HEAD against. CI sets this to the PR's merge-base "
            "(${{ github.event.pull_request.base.sha }}); local devs typically use "
            "origin/main. The ref is consumed by 'git ls-tree' and 'git show'; "
            "any rev-spec git can resolve is acceptable."
        ),
    )
    check_coverage.add_argument(
        "--allowlist-root",
        type=Path,
        default=Path("config/cicd"),
        help=(
            "Directory whose 'enforce_*' subdirectories are checked, or one "
            "'enforce_*' directory to check directly. Default: config/cicd. "
            "Non-empty legacy entry shapes without an 'allow_hits:' block produce "
            "UNRECOGNIZED_ENTRY_SHAPE violations."
        ),
    )
    check_coverage.add_argument(
        "--forbid-unverified-judge-metadata",
        action="store_true",
        help=(
            "Fail complete newly-signed allow_hits entries instead of trusting their "
            "signature shape. Use in fork PR CI, where the HMAC key is unavailable "
            "and new signatures cannot be recomputed."
        ),
    )
    check_coverage.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help=(
            "Repository working tree root. Required for git commands. Defaults to "
            "the current working directory (the repo root in standard CI usage)."
        ),
    )

    check_blankets = subparsers.add_parser(
        "check-per-file-blanket-ratchet",
        help=(
            "Reject permanent multi-rule per-file suppressions when matched source "
            "files are touched, and reject new or broadened blankets while "
            "grandfathering untouched baseline-equivalent legacy debt."
        ),
    )
    check_blankets.add_argument(
        "--baseline-ref",
        required=True,
        help="Git ref whose permanent multi-rule blankets form the transitional debt baseline.",
    )
    check_blankets.add_argument(
        "--allowlist-root",
        type=Path,
        default=Path("config/cicd"),
        help="Allowlist tree to scan recursively. Default: config/cicd.",
    )
    check_blankets.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository working tree root used for Git baseline reads.",
    )

    check_quality = subparsers.add_parser(
        "check-judge-quality",
        help=(
            "Run the labelled judge-quality corpus through the live cicd-judge "
            "and fail if verdict/decorator accuracy drops below threshold."
        ),
    )
    check_quality.add_argument(
        "--corpus",
        type=Path,
        default=Path("config/cicd/judge-quality-corpus/v1.jsonl"),
        help="Strict JSONL corpus of labelled judge-quality cases.",
    )
    check_quality.add_argument(
        "--min-accuracy",
        type=float,
        default=0.90,
        help="Minimum exact-match accuracy required, as a fraction in [0.0, 1.0]. Default: 0.90.",
    )
    check_quality.add_argument(
        "--min-cases",
        type=int,
        default=10,
        help="Minimum accepted corpus size. Default: 10.",
    )
    check_quality.add_argument(
        "--max-cases",
        type=int,
        default=30,
        help="Maximum accepted corpus size. Default: 30.",
    )
    _add_judge_transport_arg(check_quality)
    _add_judge_tools_arg(check_quality)
    check_quality.add_argument(
        "--root",
        type=Path,
        default=Path("src/elspeth"),
        help="Source tree the readonly judge may search. Default: src/elspeth.",
    )
    check_quality.add_argument(
        "--allowlist-dir",
        type=Path,
        default=Path("config/cicd/enforce_tier_model"),
        help="Allowlist directory the readonly judge may search alongside --root.",
    )
    check_quality.add_argument(
        "--model",
        default=None,
        help=(
            "Judge model id in the SELECTED transport's namespace (OpenRouter slug, "
            "Agent SDK id, or Codex model). Defaults to that transport's own default."
        ),
    )
    check_quality.add_argument(
        "--max-tokens",
        type=_positive_int,
        default=None,
        help="Maximum completion tokens for each judge response. Defaults to DEFAULT_JUDGE_MAX_TOKENS.",
    )
    check_quality.add_argument(
        "--format",
        dest="judge_quality_format",
        choices=("text", "json"),
        default="text",
        help="Output format for the quality report.",
    )

    check_trust_boundary_diff = subparsers.add_parser(
        "check-trust-boundary-diff",
        help=(
            "Report @trust_boundary decorators added in this PR. "
            "Diffs HEAD against --baseline-ref and exits 0 when the report "
            "is produced; this is a review-surfacing step, not an enforcing gate."
        ),
    )
    check_trust_boundary_diff.add_argument(
        "--baseline-ref",
        required=True,
        help="Git ref to diff HEAD against (typically the PR merge-base).",
    )
    check_trust_boundary_diff.add_argument(
        "--root",
        type=Path,
        default=Path("src/elspeth"),
        help="Source tree to scan for changed Python files.",
    )
    check_trust_boundary_diff.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository working tree root. Required for git commands.",
    )

    check_rotation_audit = subparsers.add_parser(
        "check-rotation-audit",
        help=("Fail if this PR rotates tier_model allowlist fingerprints without matching .elspeth/rotations.log manifest records."),
    )
    check_rotation_audit.add_argument(
        "--baseline-ref",
        required=True,
        help="Git ref to diff HEAD against (typically the PR merge-base).",
    )
    check_rotation_audit.add_argument(
        "--allowlist-root",
        type=Path,
        default=Path("config/cicd"),
        help="Directory whose enforce_* allowlist YAML files are checked.",
    )
    check_rotation_audit.add_argument(
        "--rotation-log",
        type=Path,
        default=Path(".elspeth/rotations.log"),
        help="JSONL manifest produced by elspeth-lints rotate.",
    )
    check_rotation_audit.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository working tree root. Required for git commands.",
    )

    # CI gate: override-rate drift (convergent finding C3).
    check_overrides = subparsers.add_parser(
        "check-override-rate",
        help=(
            "Fail if the rolling-window OVERRIDDEN_BY_OPERATOR rate across all "
            "allowlists exceeds --max-rate. Insufficient-data windows pass with a "
            "notice; see override_rate.py docstring for the small-sample policy."
        ),
    )
    check_overrides.add_argument(
        "--allowlist-root",
        type=Path,
        default=Path("config/cicd"),
        help="Directory whose 'enforce_*' subdirectories are aggregated.",
    )
    check_overrides.add_argument(
        "--window-days",
        type=int,
        default=30,
        help="Rolling-window length in days (default: 30).",
    )
    check_overrides.add_argument(
        "--min-samples",
        type=int,
        default=10,
        help=(
            "Pass the gate when fewer than this many judge-recorded entries fall "
            "inside the window (default: 10). Prevents small-N noise from "
            "tripping the rate threshold during the first weeks of operation."
        ),
    )
    check_overrides.add_argument(
        "--max-rate",
        type=float,
        default=0.10,
        help=(
            "Maximum tolerated override rate as a fraction in [0.0, 1.0] "
            "(default: 0.10 = 10%%). Computed as "
            "OVERRIDDEN_BY_OPERATOR / (ACCEPTED + OVERRIDDEN_BY_OPERATOR) "
            "across the window."
        ),
    )
    check_overrides.add_argument(
        "--max-overrides",
        type=int,
        default=None,
        help=(
            "Optional absolute cap on OVERRIDDEN_BY_OPERATOR entries inside "
            "the window. When set, the gate fails if the count exceeds this "
            "value even when the ratio remains below --max-rate."
        ),
    )
    check_overrides.add_argument(
        "--reference-time",
        type=str,
        default=None,
        help=(
            "Window anchor as a timezone-aware ISO-8601 timestamp (e.g. "
            "'2026-05-23T00:00:00Z'). Defaults to current UTC; override for "
            "reproducibility in CI replays."
        ),
    )
    check_overrides.add_argument(
        "--counter-snapshot",
        type=Path,
        default=None,
        help=(
            "Path to the hash-bound override-rate counter snapshot. Default: <allowlist-root>/.judge-metrics/override-rate-counters.json."
        ),
    )

    return parser


_SIGN_BUNDLE_LANES = ("new_judgment", "resign")

# Ceiling for ``--judge-concurrency``: each in-flight judge is one ``codex
# exec`` subprocess plus its own read-only MCP server, and the provider's
# rate limit — not the CPU — is the real bound. Kept small on purpose.
_SIGN_BUNDLE_MAX_JUDGE_CONCURRENCY = 8


def _sign_bundle_lanes(raw: str) -> tuple[str, ...]:
    """Parse a comma-separated lane subset; reject unknown or empty selections."""
    lanes = tuple(sorted({part.strip() for part in raw.split(",") if part.strip()}))
    if not lanes:
        raise argparse.ArgumentTypeError("--lanes requires at least one lane name")
    unknown = [lane for lane in lanes if lane not in _SIGN_BUNDLE_LANES]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown lane(s) {', '.join(unknown)}; valid lanes: {', '.join(_SIGN_BUNDLE_LANES)}")
    return lanes
