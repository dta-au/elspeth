"""Finite complete Advisor text custody for the physical credential scan.

The witness grants no prompt trust or credential exemption. It binds exactly
one selected user-content scalar so the unchanged detector can inspect its
whole actual UTF-8 extent before the remaining tree receives ordinary limits.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, final

from elspeth.contracts.credential_material import DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.credential_guard import require_no_credential_material


@final
@dataclass(frozen=True, slots=True)
class PreparedAdvisorContent:
    message_index: int
    content: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.message_index) is not int or self.message_index < 0 or type(self.content) is not str:
            raise AuditIntegrityError("Prepared Advisor content requires an exact message position and complete string")


def scan_prepared_advisor_content(
    messages: Sequence[Mapping[str, Any]],
    witnesses: tuple[PreparedAdvisorContent, ...],
    *,
    role: str,
) -> list[Mapping[str, Any]]:
    """Inspect complete bound scalars, retaining ordinary residual-tree checks."""
    if role != "advisor" or type(witnesses) is not tuple or any(type(item) is not PreparedAdvisorContent for item in witnesses):
        raise AuditIntegrityError("Complete Advisor credential scan lacks nominal producer custody")
    if len({item.message_index for item in witnesses}) != len(witnesses):
        raise AuditIntegrityError("Prepared Advisor content positions overlap")
    residual: list[Mapping[str, Any]] = list(messages)
    for witness in witnesses:
        index = witness.message_index
        if index >= len(messages) or messages[index].get("role") != "user" or messages[index].get("content") != witness.content:
            raise AuditIntegrityError("Prepared Advisor content differs from the exact physical request")
        require_no_credential_material(
            witness.content,
            surface="composer_advisor_complete_content",
            max_string_bytes=max(DEFAULT_CREDENTIAL_TRAVERSAL_POLICY.max_string_bytes, len(witness.content.encode("utf-8"))),
        )
        residual[index] = dict(messages[index]) | {"content": "[complete Advisor content credential-scanned]"}
    return residual
