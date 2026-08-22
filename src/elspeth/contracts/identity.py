"""Entity identifiers and token structures.

These types answer: "How do we refer to things?"
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.freeze import require_int
from elspeth.contracts.schema_contract import PipelineRow


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
            if type(value) is not str:
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


def pop_fork_frame(path: tuple[LineageFrame, ...], *, group_id: str) -> tuple[LineageFrame, ...]:
    """Remove the FORK frame identified by group_id, wherever it sits (ruling 27).

    Unlike pop_closer_frame's strict-innermost pop (rulings 24/28, where a
    closer's own frame IS the innermost frame by construction — coalesce and
    fork are strictly LIFO-nested), a row_union release closes only the
    branch-selection scope. A row-multiplying transform inside the branch
    (e.g. an expand) may have stacked an EXPAND frame on top of the branch's
    FORK frame before the token reached the union — elspeth-a5b86149d4,
    tests/integration/pipeline/test_row_union_branch_cardinality.py. FORK and
    EXPAND scopes do not close in LIFO order with respect to each other: the
    row_union closes the branch scope here, the expand's own multiplicity
    scope closes elsewhere (or never explicitly closes). So this removes the
    named FORK frame from wherever it sits in the path and preserves every
    other frame in order — discarding a surviving EXPAND frame on release
    would fabricate lineage, since the expand genuinely happened.
    """
    for index in range(len(path) - 1, -1, -1):
        frame = path[index]
        if frame.kind is FrameKind.FORK and frame.group_id == group_id:
            return path[:index] + path[index + 1 :]
    raise OrchestrationInvariantError(f"pop_fork_frame: no FORK frame for group {group_id!r} in path {path!r} (ruling 27 pop)")


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """Identity and data for a token flowing through the DAG.

    Lineage is ONE field — the lineage path (spec §4.1): a stack of typed frames,
    outermost first. branch_name / fork_group_id / expand_group_id are DERIVED
    accessors over the path (ruling 21: the only read path for the legacy names;
    any stored-field resurrection is a defect). join_group_id is a merge EVENT,
    not a membership — it left TokenInfo (ruling 20) and rides RowResult /
    PendingOutcome / TokenWorkItem carriers.

    Resume state (resume_attempt_offset, resume_checkpoint_id): carried on the token —
    not WorkItem — because SinkExecutor buffers TokenInfos across WorkItem boundaries,
    so the per-token offset must survive that buffering. See ADDENDUM 4 in the F1 design spec.

    Frozen for immutability - use with_updated_data() to create new instances.
    """

    row_id: str
    token_id: str
    row_data: PipelineRow  # CHANGED from dict[str, Any]
    lineage_path: tuple[LineageFrame, ...] = ()  # Outermost first (spec §4.1).
    resume_attempt_offset: int = 0  # Added to every node_states.attempt written while
    # re-driving THIS token on resume, so its records coexist with the append-only run-1
    # records under UniqueConstraint(token_id, node_id, attempt). Value is the prior run's
    # highest recorded attempt + 1 (processor.resume_incomplete_token), so 0 is NOT a
    # reliable "run-1" marker: a token never stepped before the interrupt (max_attempt = -1)
    # is re-driven on resume with offset 0. The authoritative resume discriminator is
    # `resume_checkpoint_id is not None` — the field explain() filters on.
    resume_checkpoint_id: str | None = None  # Resumed-from checkpoint id, stamped on every
    # node_state written during this token's resume re-drive (provenance). None = run-1;
    # not None = resume re-drive. This — NOT a zero/non-zero offset — is the resume marker.

    def __post_init__(self) -> None:
        """Validate identity invariants at construction time.

        row_id and token_id are the most fundamental identity fields in
        the system — every audit trail record references them. Empty strings
        would produce valid-looking but meaningless audit entries.
        """
        if not isinstance(self.row_id, str):
            raise TypeError(f"TokenInfo.row_id must be str, got {type(self.row_id).__name__}: {self.row_id!r}")
        if not self.row_id:
            raise ValueError("TokenInfo.row_id must not be empty")
        if not isinstance(self.token_id, str):
            raise TypeError(f"TokenInfo.token_id must be str, got {type(self.token_id).__name__}: {self.token_id!r}")
        if not self.token_id:
            raise ValueError("TokenInfo.token_id must not be empty")
        if type(self.lineage_path) is not tuple:
            raise TypeError(
                f"TokenInfo.lineage_path must be tuple[LineageFrame, ...], got {type(self.lineage_path).__name__}: {self.lineage_path!r}"
            )
        for frame in self.lineage_path:
            if type(frame) is not LineageFrame:
                raise TypeError(f"TokenInfo.lineage_path entries must be LineageFrame, got {type(frame).__name__}: {frame!r}")
        # One-way resume invariant: a positive resume_attempt_offset only ever originates
        # from a resume re-drive (processor.resume_incomplete_token), which always stamps
        # the checkpoint id. The implication is one-directional — offset 0 is ambiguous
        # (run-1 OR a never-stepped resume token), so we do NOT require the converse.
        require_int(self.resume_attempt_offset, "TokenInfo.resume_attempt_offset", min_value=0)
        if self.resume_attempt_offset > 0 and self.resume_checkpoint_id is None:
            raise ValueError(
                f"TokenInfo.resume_attempt_offset={self.resume_attempt_offset} > 0 requires a "
                f"resume_checkpoint_id (a positive offset only arises from a resume re-drive), got None"
            )

    @property
    def branch_name(self) -> str | None:
        return path_branch_name(self.lineage_path)

    @property
    def fork_group_id(self) -> str | None:
        return path_fork_group_id(self.lineage_path)

    @property
    def expand_group_id(self) -> str | None:
        return path_expand_group_id(self.lineage_path)

    def with_updated_data(self, new_data: PipelineRow) -> TokenInfo:
        """Return a new TokenInfo with updated row_data, preserving lineage_path.

        This method ensures that when row_data is updated after a transform,
        all identity and lineage metadata (lineage_path, and the derived
        branch_name/fork_group_id/expand_group_id accessors over it) are preserved.

        Critically, resume_attempt_offset and resume_checkpoint_id are also
        preserved via dataclasses.replace — this is the propagation mechanism that
        ensures a token re-driven on resume carries its offset through every
        downstream node_state write (transform → next transform → sink). Dropping
        these fields here would cause a collision one node downstream.

        Use this instead of constructing TokenInfo manually when updating
        a token's data after processing.

        Args:
            new_data: The new PipelineRow to use

        Returns:
            A new TokenInfo with the same identity/lineage but new row_data
        """
        return replace(self, row_data=new_data)
