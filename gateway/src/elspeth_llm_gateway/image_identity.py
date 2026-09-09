"""Secret-free identity projection for offline gateway image admission."""

import argparse
import contextlib
import json
import os
import re
import sys
from collections.abc import Iterator, Sequence
from typing import Final

from elspeth_llm_gateway import CONTRACT_MAJOR, __version__
from elspeth_llm_gateway.core.adapter_identity import compute_adapter_fingerprint, resolve_adapter
from elspeth_llm_gateway.sdk.protocol import AdapterDescriptor

_IMAGE_IDENTITY_SCHEMA: Final = "elspeth.llm-gateway.image-identity.v1"
_RUNTIME_IDENTITY: Final = "elspeth-llm-gateway"


def collect_image_identity(adapter_name: str) -> dict[str, object]:
    """Return the code-owned runtime and installed-adapter identity."""
    adapter = resolve_adapter(adapter_name)
    descriptor = adapter.descriptor()
    if not isinstance(descriptor, AdapterDescriptor) or descriptor.name != adapter_name:
        raise RuntimeError("adapter descriptor identity mismatch")
    adapter_fingerprint = compute_adapter_fingerprint(adapter, adapter_name=adapter_name, require_package=True)
    if re.fullmatch(r"[0-9a-f]{64}", adapter_fingerprint) is None or adapter_fingerprint == "0" * 64:
        raise RuntimeError("adapter fingerprint unavailable")
    return {
        "schema": _IMAGE_IDENTITY_SCHEMA,
        "runtime_identity": _RUNTIME_IDENTITY,
        "runtime_version": __version__,
        "contract_major": CONTRACT_MAJOR,
        "adapter_name": descriptor.name,
        "adapter_version": descriptor.version,
        "adapter_api_major": descriptor.adapter_api_major,
        "adapter_fingerprint": adapter_fingerprint,
    }


@contextlib.contextmanager
def _suppress_process_output() -> Iterator[None]:
    """Discard adapter writes to Python streams and process stdout/stderr."""
    saved_stdout = os.dup(1)
    saved_stderr = os.dup(2)
    try:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            sys.stdout.flush()
            sys.stderr.flush()
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                yield
    finally:
        os.dup2(saved_stdout, 1)
        os.dup2(saved_stderr, 2)
        os.close(saved_stdout)
        os.close(saved_stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """Emit the canonical identity document for an installed adapter."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adapter", required=True, help="installed adapter entry-point name")
    args = parser.parse_args(argv)
    try:
        with _suppress_process_output():
            identity = collect_image_identity(args.adapter)
            output = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    # Adapter import and descriptor code is third-party. SystemExit is a
    # valid way for it to abort, but its message must not escape the fixed
    # admission envelope; this isolated CLI boundary therefore catches the
    # full BaseException hierarchy after restoring stdout/stderr.
    except BaseException:
        sys.stderr.write("gateway_image_identity_unavailable\n")
        return 1
    sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through ``main``
    raise SystemExit(main())
