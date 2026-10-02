"""Immutable cross-store admission identity; never execution authority."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RunStartPermitBinding:
    """Bind a Landscape run to the exact Sessions-issued permit subject."""

    run_id: str
    permit_id: str
    permit_epoch: int
    subject_hash: str

    def __post_init__(self) -> None:
        for value in (self.run_id, self.permit_id):
            if type(value) is not str or not value.strip():
                raise ValueError("run and permit identities must be nonblank strings")
        if type(self.permit_epoch) is not int or self.permit_epoch < 1:
            raise ValueError("permit_epoch must be a positive exact integer")
        if (
            type(self.subject_hash) is not str
            or len(self.subject_hash) != 64
            or any(c not in "0123456789abcdef" for c in self.subject_hash)
        ):
            raise ValueError("permit subject must be a lowercase SHA256 digest")
