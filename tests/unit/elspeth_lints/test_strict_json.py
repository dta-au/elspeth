from __future__ import annotations

import pytest

from elspeth_lints.core.strict_json import StrictJSONError, strict_json_loads


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_strict_json_rejects_non_json_numeric_constants(constant: str) -> None:
    with pytest.raises(StrictJSONError, match="non-JSON numeric constant"):
        strict_json_loads(f'{{"recorded_at": {constant}}}')
