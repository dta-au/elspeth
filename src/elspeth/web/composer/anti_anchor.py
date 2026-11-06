"""§7.7 hints for observed repeated tool failures.

When the composer LLM retries the same tool with the same canonical arguments
three times in a row, the tracker can report that repeated failure pattern.
It does not measure whether the model read new evidence. The original §7.7
repair targeted that byte-identical pattern. Later
audit of the captured Tier 1 final cohort RED (`53bc3cf2-…`, see
`notes/composer-tier1.5-step-c-diagnosis-2026-05-06.md`) proved the actual
payloads were distinct: the model drifted across failed `set_pipeline` calls,
then surrendered to a text-only "I'm stuck" reply. The tracker therefore covers
both argument identity and same-tool failures with different arguments.

The fix is small: after N=3 consecutive identical failures, inject a
`role="user"` message before the next LLM call telling the model to enumerate
which fields the validator named, what shape they expect, and to change at
least one of them. After firing, the tracker resets so the hint doesn't repeat
on the same anchor — a 4th identical failure won't re-trigger; only a NEW run
of 3 identical failures (after at least one success or different-payload
failure has reset the chain) will fire again.

The hint is `role="user"` rather than `role="tool"` because tool-role messages
require a `tool_call_id` matched to a prior assistant tool_call envelope; a
free-floating system-injected message has no such id. Wrapping in a
[ELSPETH-SYSTEM-HINT] prefix makes the system-injected origin visible in
chat_messages persistence (audit trail) and to operators inspecting transcripts.

Detection criteria (deliberately conservative):

- Failure means: any non-success outcome — JSON-decode failure, non-dict args,
  canonicalization rejection, MissingRequiredPaths, ToolArgumentError, or a
  successful dispatch whose `result.success == False` (e.g., set_pipeline
  whose validation rejects the resulting state).
- Identical means: same `(tool_name, arguments_hash)` pair where
  `arguments_hash` is the canonical SHA-256 from `DispatchAudit.arguments_hash`.
- Three-in-a-row means: the LAST 3 entries in the bounded failure deque are
  all the same `(tool_name, hash)` pair. Earlier non-matching entries do not
  poison the trigger; the deque length is bounded so stale entries age out.
- Different-argument failure means: the LAST 3 entries are failures for the same
  tool but at least two distinct argument hashes. Error details and validation
  changes are not compared, so this does not establish repair progress.
- Any tool *success* clears the failure window immediately.
"""

from __future__ import annotations

from collections import deque

from pydantic import BaseModel, ConfigDict, model_validator

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory

_RETRY_THRESHOLD = 3
_DEQUE_MAXLEN = 5

FailureKey = tuple[str, str]
FailureDeque = deque[FailureKey]


def should_inject_hint(failures: FailureDeque) -> bool:
    """Return True iff the last `_RETRY_THRESHOLD` failures are identical."""
    if len(failures) < _RETRY_THRESHOLD:
        return False
    last = list(failures)[-_RETRY_THRESHOLD:]
    return all(entry == last[0] for entry in last)


def should_inject_drift_hint(failures: FailureDeque) -> bool:
    """Return True iff the same tool failed with differing argument hashes."""
    if len(failures) < _RETRY_THRESHOLD:
        return False
    last = list(failures)[-_RETRY_THRESHOLD:]
    tool_names = {tool_name for tool_name, _arguments_hash in last}
    argument_hashes = {arguments_hash for _tool_name, arguments_hash in last}
    return len(tool_names) == 1 and len(argument_hashes) > 1


def build_anti_anchor_hint(failures: FailureDeque) -> str:
    """Compose the hint string for the current anchor.

    Always ends with a safe structural scaffold: identify validator-named
    fields, mismatch categories, and expected schema shapes without restating
    prior literal values. Generic-by-design — works for connection-naming AND
    options-validation failures (the diagnosed RED was the latter, not the
    former, so the hint cannot be connection-name-specific).
    """
    if len(failures) < _RETRY_THRESHOLD:
        # Caller mis-used the helper; defensive RAISE rather than silent default
        # since the hint message must always be meaningful.
        raise ValueError(f"build_anti_anchor_hint requires {_RETRY_THRESHOLD}+ identical failures; got {len(failures)}")
    tool_name = failures[-1][0]
    return (
        f"[ELSPETH-SYSTEM-HINT] Your last {_RETRY_THRESHOLD} calls to `{tool_name}` "
        "all failed with byte-identical arguments. "
        "Inspect the reported issue before retrying the same payload. "
        "Before the next attempt, reason only about redacted structure. "
        "Do not repeat, quote, summarize, or otherwise reproduce any literal values from prior arguments or errors "
        "in assistant prose. Identify only: "
        "(a) which field names the validator named in the error, "
        "(b) each field's structural mismatch category (for example missing, type, shape, allowed-key, or cardinality), "
        "(c) what schema shape each field expects "
        "(re-read the relevant `get_plugin_schema` result if you have not already), "
        "then submit a tool call that changes at least one structural choice."
    )


def build_drift_hint(failures: FailureDeque) -> str:
    """Report observed failures without inferring their errors or progress."""
    if not should_inject_drift_hint(failures):
        raise ValueError("build_drift_hint requires 3 same-tool failures with different arguments")
    tool_name = failures[-1][0]
    return (
        f"[ELSPETH-SYSTEM-HINT] Your last {_RETRY_THRESHOLD} calls to `{tool_name}` "
        "all failed while sending different arguments. The error details and validation changes were not compared; "
        "these observations alone do not establish whether the repairs made progress. "
        "Use the actual tool results to decide the next step. When discussing them, "
        "reason only about field names, types, shapes, allowed keys, and cardinality; "
        "do not repeat, quote, summarize, or otherwise reproduce literal values from the failed payloads in assistant prose; "
        "(a) compare the last failed payloads structurally against the validator-named fields, "
        "(b) re-read the relevant schema or assistance result if the expected shape is unclear, "
        "(c) continue the repair justified by that evidence, or "
        "(d) surface a named gap if the requested shape is unsupported by the available tools."
    )


class AntiAnchorSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    failures: tuple[FailureKey, ...]
    categories: tuple[ToolArgumentErrorCategory | None, ...]

    @model_validator(mode="after")
    def require_matching_windows(self) -> AntiAnchorSnapshot:
        if len(self.failures) != len(self.categories) or len(self.failures) > _DEQUE_MAXLEN:
            raise ValueError("anti-anchor snapshot has inconsistent failure windows")
        return self


class AntiAnchorTracker:
    """Maintains the bounded failure deque across the compose loop.

    The instance is created once per `compose()` call and lives for the
    lifetime of that call. State is *not* persisted between sessions — the
    anchor pattern is per-conversation, and a new compose() call gets a fresh
    tracker so an unrelated earlier failure cannot poison a new convergence.
    """

    def __init__(self) -> None:
        self._failures: FailureDeque = deque(maxlen=_DEQUE_MAXLEN)
        self._categories: deque[ToolArgumentErrorCategory | None] = deque(maxlen=_DEQUE_MAXLEN)

    def record_failure(self, tool_name: str, arguments_hash: str, *, category: ToolArgumentErrorCategory | None = None) -> None:
        """Append a failure. Bounded deque drops the oldest beyond `_DEQUE_MAXLEN`."""
        self._failures.append((tool_name, arguments_hash))
        self._categories.append(category)

    def record_success(self) -> None:
        """Clear the preceding failure window after a successful tool call."""
        self._failures.clear()
        self._categories.clear()

    def next_hint(self) -> str | None:
        """Return the hint the current failure window earns, or ``None``.

        This is the single authority for hint text: a hint exists only when
        one of the two trigger predicates holds, and the predicate that
        holds chooses the text. There is no fallback. The prior
        ``build_hint`` fell through to ``build_anti_anchor_hint`` when
        neither predicate matched, and that helper rejects only a SHORT
        window — so a mixed, non-triggering window of three failures
        across different tools produced a confident "byte-identical
        arguments" hint for a pattern that never occurred
        (elspeth-aa459b4dd0). A hint is control text the model acts on;
        prose for a state that did not happen is worse than no prose.
        """
        if (should_inject_hint(self._failures) or should_inject_drift_hint(self._failures)) and any(
            category
            in {
                ToolArgumentErrorCategory.SEMANTIC_RULE,
                ToolArgumentErrorCategory.MODEL_VALIDATION,
                ToolArgumentErrorCategory.SCHEMA_BOUND,
                ToolArgumentErrorCategory.RATE_CAP_PER_TERM,
                ToolArgumentErrorCategory.RATE_CAP_PER_SESSION_DAY,
                ToolArgumentErrorCategory.PROMPT_BUDGET,
                ToolArgumentErrorCategory.DISCOVERY_ONLY,
                ToolArgumentErrorCategory.DUPLICATE_RESOLVED_INTERPRETATION,
            }
            for category in list(self._categories)[-_RETRY_THRESHOLD:]
        ):
            pattern = "with different arguments" if should_inject_drift_hint(self._failures) else "with byte-identical arguments"
            return (
                f"[ELSPETH-SYSTEM-HINT] Your last {_RETRY_THRESHOLD} calls to `{self._failures[-1][0]}` failed {pattern}. "
                "The error details and validation changes were not compared; these observations alone do not establish repair progress. "
                "Re-read the specific reported rule and repair guidance. "
                "Correct only that issue; preserve valid pipeline topology and document content. "
                "Do not repeat, quote, summarize, or otherwise reproduce any literal values from prior arguments or errors in assistant prose. "
                "If the available tools cannot satisfy the rule, explain the named constraint and required user action "
                "instead of repeating unsupported requests."
            )
        if should_inject_hint(self._failures):
            return build_anti_anchor_hint(self._failures)
        if should_inject_drift_hint(self._failures):
            return build_drift_hint(self._failures)
        return None

    def should_fire(self) -> bool:
        return self.next_hint() is not None

    def build_hint(self) -> str:
        """Return the hint for a firing window; refuse to manufacture one otherwise.

        Kept for the ``should_fire()`` / ``build_hint()`` caller protocol.
        Callers that want a single call should use :meth:`next_hint`.
        """
        hint = self.next_hint()
        if hint is None:
            raise ValueError(
                "build_hint() called on a failure window that fires no anti-anchor predicate "
                f"({len(self._failures)} recorded failure(s), not identical and not same-tool drift); "
                "check should_fire() first or use next_hint()"
            )
        return hint

    def consume_fire(self) -> None:
        """Reset after a hint is injected to prevent immediate re-fire.

        The hint addressed the model; we wait for it to act. A 4th failure does
        NOT immediately re-fire — only a fresh run of N failures after the
        reset does.
        """
        self._failures.clear()
        self._categories.clear()

    def snapshot_for_continuation(self) -> AntiAnchorSnapshot:
        return AntiAnchorSnapshot(
            failures=tuple(self._failures),
            categories=tuple(self._categories),
        )

    @classmethod
    def for_continuation(cls, snapshot: AntiAnchorSnapshot) -> AntiAnchorTracker:
        if type(snapshot) is not AntiAnchorSnapshot:
            raise TypeError("anti-anchor restoration requires its owned snapshot")
        owner = cls()
        owner._failures.extend(snapshot.failures)
        owner._categories.extend(snapshot.categories)
        return owner
