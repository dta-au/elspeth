"""Exact identity of the local PDF renderer used for audited calls."""

from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib import metadata
from pathlib import Path


@lru_cache(maxsize=1)
def renderer_identity() -> str:
    """Hash owned renderer code and the installed native PDFium binary.

    The lookup uses package metadata; it never imports pypdfium2 into the
    parent process. Replay does not call this function at all.
    """
    distribution = metadata.distribution("pypdfium2")
    binary_files = [path for path in distribution.files or () if str(path).endswith("/libpdfium.so")]
    if len(binary_files) != 1:
        raise RuntimeError("PDF renderer identity requires exactly one installed libpdfium.so")
    module_dir = Path(__file__).resolve().parent
    owned_files = (
        module_dir / "protocol.py",
        module_dir / "renderer.py",
        module_dir / "worker.py",
        module_dir / "png.py",
        module_dir.parent.parent / "transforms/pdf_rasterize.py",
    )
    digest = hashlib.sha256()
    digest.update(distribution.version.encode("ascii"))
    for path in owned_files:
        digest.update(path.name.encode("ascii"))
        digest.update(path.read_bytes())
    binary = distribution.locate_file(binary_files[0])
    digest.update(binary.read_bytes())
    return digest.hexdigest()
