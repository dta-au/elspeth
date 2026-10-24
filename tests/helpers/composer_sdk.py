"""Fail-closed SDK scripts for real Composer/Advisor audit paths.

Install this callable at litellm.acompletion, never at an admitted completion,
quota, prompt-guard or audit owner. This bounded fixture supports nonempty
primary tool-loop requests and tool-less Advisor requests. Prose/title and
boot probes require their own explicit scripts; there is no real SDK fallback.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from unittest.mock import AsyncMock

from elspeth.web.composer._compose_loop_carriers import _AdmittedLLMCompletion


class ComposerSDKScript:
    """Route only the actual SDK request shape, retaining separate call oracles.

    Replies are the original raw fixture values or original exception objects.
    No completion is decoded back into a synthetic provider response, and no
    audit/custody/usage/cost object is manufactured by this helper. Production
    receives each raw reply and performs its ordinary admission and settlement.
    This shape routing is a test convenience, never proof of provider family.
    """

    def __init__(
        self,
        *,
        primary_model: str,
        primary: Sequence[object],
        advisor_model: str | None = None,
        advisor: Sequence[object] = (),
    ) -> None:
        self.primary_model = primary_model
        self.advisor_model = advisor_model
        self.primary = AsyncMock(side_effect=tuple(primary))
        self.advisor = AsyncMock(side_effect=tuple(advisor))

    async def __call__(self, **kwargs: Any) -> object:
        if "tools" in kwargs:
            if not kwargs["tools"] or kwargs["model"] != self.primary_model:
                raise AssertionError("Unexpected primary SDK request shape or model")
            selected = self.primary
        else:
            if self.advisor_model is None or kwargs["model"] != self.advisor_model or "max_tokens" not in kwargs:
                raise AssertionError("Unscripted tool-less SDK request")
            selected = self.advisor
        try:
            response = await selected(**kwargs)
        except StopAsyncIteration as exhausted:
            raise AssertionError("Composer SDK script exhausted") from exhausted
        if isinstance(response, _AdmittedLLMCompletion):
            raise AssertionError("SDK script requires the original raw response, not admitted completion DATA")
        return response
