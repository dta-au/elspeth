"""The closed event vocabulary shared by continuation metadata and owners."""

from typing import Final, Literal, get_args

ContinuationEventTag = Literal[
    "ingress",
    "checkpoint",
    "input_prepared",
    "provider_admitted",
    "may_dispatch",
    "response_retained",
    "tool_prepared",
    "effect_committed",
    "effect_refused",
    "effect_unknown",
    "review_retained",
    "yielded",
    "recovered",
    "settled",
]
CONTINUATION_EVENT_TAGS: Final[tuple[ContinuationEventTag, ...]] = get_args(ContinuationEventTag)
