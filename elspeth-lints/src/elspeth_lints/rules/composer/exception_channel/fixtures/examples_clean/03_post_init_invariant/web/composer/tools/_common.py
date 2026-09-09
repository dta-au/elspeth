from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class OwnedView:
    blob_id: str

    def __post_init__(self) -> None:
        if type(self.blob_id) is not str or not self.blob_id:
            raise TypeError("OwnedView.blob_id must be a non-empty exact string")
