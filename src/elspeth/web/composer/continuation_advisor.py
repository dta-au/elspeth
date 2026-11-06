"""Closed retained Advisor source DATA and actual manual-source capture.

This module does not rebuild current trusted instructions, authorize review,
run an Advisor or turn a recorded decision into a committed witness.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import ValidationError, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.web.composer.continuation import ComposeFrame, ContinuationFrameStack, ContinuationValue, PrivateChunkRef
from elspeth.web.composer.continuation_codec import (
    PrivateChunk,
    decode_continuation_snapshot,
    encode_continuation_snapshot,
    encode_private_chunk,
    verify_private_chunk,
)
from elspeth.web.composer.tools.sessions import RequestAdvisorHintArgumentsModel


class ContinuationAdvisorPremise(ContinuationValue):
    input_hash: str
    policy_hash: str
    tool_manifest_hash: str
    frontier: PrivateChunkRef


class ContinuationAdvisorInvocation(ContinuationValue):
    kind: Literal["manual", "early", "publication", "semantic"]
    state: PrivateChunkRef
    evidence: PrivateChunkRef
    transcript: PrivateChunkRef
    iteration: PrivateChunkRef
    coverage_binding: str
    manual_arguments_json: str | None
    manual_tool_call_id: str | None
    candidate: str | None
    premise: ContinuationAdvisorPremise | None

    @model_validator(mode="after")
    def require_closed_source(self) -> Self:
        expected = (
            (self.state, "composer.state.v1"),
            (self.evidence, "composer.evidence.v2"),
            (self.transcript, "composer.transcript.v1"),
            (self.iteration, "composer.iteration.v1"),
        )
        if any(reference.codec != codec for reference, codec in expected) or not self.coverage_binding:
            raise ValueError("Advisor source lost its complete current owner references")
        if self.kind == "manual":
            if self.manual_arguments_json is None or not self.manual_tool_call_id or self.candidate is not None or self.premise is not None:
                raise ValueError("manual Advisor source requires only its actual tool input")
            model = RequestAdvisorHintArgumentsModel.model_validate_json(self.manual_arguments_json, strict=True)
            if canonical_json(model.model_dump(mode="json")) != self.manual_arguments_json:
                raise ValueError("manual Advisor source lost an original argument field")
        elif self.manual_arguments_json is not None or self.manual_tool_call_id is not None:
            raise ValueError("automatic Advisor phase cannot acquire a manual tool identity")
        if self.kind == "early" and (self.candidate is not None or self.premise is not None):
            raise ValueError("EARLY advice cannot claim publication or semantic clearance")
        if self.kind in ("publication", "semantic") and self.candidate is None:
            raise ValueError("END advice requires its complete exact candidate")
        if (self.kind == "semantic") != (self.premise is not None):
            raise ValueError("only semantic advice carries its closed premise")
        if self.premise is not None and self.premise.frontier.codec != "composer.semantic-frontier.v1":
            raise ValueError("semantic advice requires the complete retained frontier")
        return self


def nearest_compose_frame(stack: ContinuationFrameStack) -> ComposeFrame:
    if type(stack) is not ContinuationFrameStack or decode_continuation_snapshot(encode_continuation_snapshot(stack)) != stack:
        raise AuditIntegrityError("Advisor source lost its complete nominal root")
    found = tuple(frame for frame in stack.frames if type(frame) is ComposeFrame)
    if len(found) != 1:
        raise AuditIntegrityError("Advisor source requires exactly one paused compose owner")
    return found[0]


def make_continuation_manual_advisor_invocation(
    *, arguments: RequestAdvisorHintArgumentsModel, tool_call_id: str, stack: ContinuationFrameStack
) -> ContinuationAdvisorInvocation:
    if type(arguments) is not RequestAdvisorHintArgumentsModel or type(tool_call_id) is not str or not tool_call_id:
        raise TypeError("manual Advisor source requires actual admitted tool arguments and identity")
    frame = nearest_compose_frame(stack)
    coverage = stable_hash(
        {
            "logical": stack.logical.model_dump(mode="json"),
            "state": frame.state.digest,
            "evidence": frame.evidence.digest,
            "transcript": frame.transcript.digest,
            "tool_call_id": tool_call_id,
        }
    )
    return ContinuationAdvisorInvocation(
        kind="manual",
        state=frame.state,
        evidence=frame.evidence,
        transcript=frame.transcript,
        iteration=frame.iteration,
        coverage_binding=coverage,
        manual_arguments_json=canonical_json(arguments.model_dump(mode="json")),
        manual_tool_call_id=tool_call_id,
        candidate=None,
        premise=None,
    )


def encode_continuation_advisor_invocation(source: ContinuationAdvisorInvocation) -> PrivateChunk:
    if type(source) is not ContinuationAdvisorInvocation:
        raise TypeError("Advisor source capture requires its closed owned value")
    chunk = encode_private_chunk(codec="composer.advisor.v1", canonical=canonical_json(source.model_dump(mode="json")))
    decode_continuation_advisor_invocation(chunk)
    return chunk


def decode_continuation_advisor_invocation(chunk: PrivateChunk) -> ContinuationAdvisorInvocation:
    if type(chunk) is not PrivateChunk or chunk.reference.codec != "composer.advisor.v1":
        raise AuditIntegrityError("active Advisor source has the wrong private codec")
    verify_private_chunk(chunk.reference, chunk.canonical)
    try:
        source = ContinuationAdvisorInvocation.model_validate_json(chunk.canonical, strict=True)
    except ValidationError as error:
        raise AuditIntegrityError("active Advisor source violates its complete owned shape") from error
    if canonical_json(source.model_dump(mode="json")) != chunk.canonical:
        raise AuditIntegrityError("active Advisor source lost or normalized an input field")
    return source
