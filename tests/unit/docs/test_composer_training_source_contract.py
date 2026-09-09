"""Source-boundary semantics in the Composer training surfaces."""

from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[3]
_BOUNDARY_DISTINCTION = (
    "Source validation uses its configured failure route so other rows can continue. "
    "A row that contradicts an acknowledged producer guarantee instead records failed boundary evidence and stops the run."
)


@pytest.mark.parametrize(
    "relative_path",
    (
        "docs/guides/composer-training-one-hour.md",
        "docs/guides/composer-training-one-hour-slides.html",
    ),
)
def test_training_distinguishes_source_validation_from_producer_guarantees(relative_path: str) -> None:
    text = " ".join((_ROOT / relative_path).read_text(encoding="utf-8").split())

    assert _BOUNDARY_DISTINCTION in text
