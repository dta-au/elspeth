"""Private, bounded context of one prepared Composer provider request."""

from dataclasses import dataclass, field
from typing import Literal

ProviderContextUnavailableReason = Literal[
    "bounds", "unsupported", "malformed_json", "duplicate_keys", "credential_field", "recognized_credential"
]


@dataclass(frozen=True, slots=True)
class PrivateProviderContext:
    """Immutable safe context or an explicit unavailable receipt.

    Construction belongs to the protocol-aware Composer capture boundary.
    Prepared context does not establish SDK dispatch or remote receipt.
    """

    disposition: Literal["retained", "unavailable"]
    canonical: str | None = field(repr=False)
    reasons: tuple[ProviderContextUnavailableReason, ...] = ()

    def __post_init__(self) -> None:
        if self.disposition not in {"retained", "unavailable"}:
            raise TypeError("Private provider context disposition is invalid")
        if type(self.reasons) is not tuple or any(
            reason not in {"bounds", "unsupported", "malformed_json", "duplicate_keys", "credential_field", "recognized_credential"}
            for reason in self.reasons
        ):
            raise TypeError("Private provider context reasons are invalid")
        if self.disposition == "retained":
            if type(self.canonical) is not str or len(self.canonical.encode("utf-8")) > 1_048_576 or self.reasons:
                raise TypeError("Private provider context retained content is invalid")
        elif self.canonical is not None or not self.reasons:
            raise TypeError("Private provider context unavailable receipt is invalid")
