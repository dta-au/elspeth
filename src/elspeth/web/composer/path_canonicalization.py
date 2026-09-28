"""Canonical source and sink path forms for ordinary Composer authoring."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any, cast

from pydantic import JsonValue

from elspeth.contracts.blobs import BLOB_REF_PATH_PREFIX
from elspeth.web.paths import SINK_LOCAL_PATH_OPTION_KEYS

_SINK_OUTPUT_POOL = "outputs"


def canonical_sink_local_paths(options: Mapping[str, Any]) -> dict[str, JsonValue]:
    """Root bare relative sink paths in the managed outputs pool.

    Absolute paths pass through for the deployment S2 allowlist to judge.
    Parent traversal is rejected without echoing the untrusted path.
    """
    updated = cast(dict[str, JsonValue], dict(options))
    for key in SINK_LOCAL_PATH_OPTION_KEYS:
        if key not in updated:
            continue
        value = updated[key]
        if type(value) is not str or not value or value.startswith(BLOB_REF_PATH_PREFIX):
            continue
        raw = PurePosixPath(value)
        if not raw.parts:
            raise ValueError(f"option {key!r} must name a path, not the current directory")
        if ".." in raw.parts:
            raise ValueError(
                f"option {key!r} must not contain '..' path segments; give a path like 'results.json' or 'reports/results.json'"
            )
        if raw.is_absolute():
            continue
        if raw.parts[0] != _SINK_OUTPUT_POOL:
            updated[key] = str(PurePosixPath(_SINK_OUTPUT_POOL) / raw)
    return updated
