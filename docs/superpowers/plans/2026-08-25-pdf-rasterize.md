# `pdf_rasterize` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A builtin transform `pdf_rasterize` that takes one input row carrying a payload-store reference to a PDF and emits one row per page — each carrying a PNG page image stored in the payload store plus page metadata — as ONE expand group, with a bad document becoming a quarantined row (Tier-2 data-integrity outcome), never an aborted run.

**Architecture:** The plugin file (`plugins/transforms/pdf_rasterize.py`) imports nothing native. Rendering happens in an ELSPETH-owned spawn-context worker subprocess (`plugins/infrastructure/rasterize/`) that imports pypdfium2, applies `RLIMIT_AS` + a `SIGXCPU`-handled `RLIMIT_CPU`, renders one DOCUMENT per task (parse once, render many) into a per-document temp directory, and returns a picklable, closed-vocabulary result. The parent enforces a wall-clock timeout with orphan-kill, stores each PNG in the payload store, and builds the expand group through the normal `success_multi` path. `on_page_failure` decides whether a partially-renderable document is refused whole or emitted partially.

**Tech Stack:** Python 3.12+, pydantic v2, `pypdfium2==5.13.x` (BSD-3/Apache-2.0, pure ctypes, bundled libpdfium — verified on the distroless runtime image), stdlib `zlib`/`struct` PNG encoder (no Pillow), `concurrent.futures.ProcessPoolExecutor` (spawn), `resource.setrlimit`.

**Spec:** `docs/superpowers/specs/2026-08-21-pdf-explode-stitch-risk-assessment.md` (Unit 2 = this plan; Units 1/3/4-stitcher/5 are OUT of scope). Research appendices with every citation: `docs/superpowers/plans/2026-08-25-pdf-rasterize-research/0{1,2,3,4}-*.md`. Read all four before starting any task.

## Global Constraints

- Branch `feat/pdf-rasterize` in worktree `/home/john/elspeth/.claude/worktrees/pdf-rasterize`, based on `feature/unified-lineage` at `a371e13d0`. The branch is MID-REFACTOR (unified lineage; engine/lineage code may look odd) — do not touch `src/elspeth/engine/` or `src/elspeth/core/` except where a task names a line.
- **Every command runs from the worktree with `PYTHONPATH=/home/john/elspeth/.claude/worktrees/pdf-rasterize/src /home/john/elspeth/.venv/bin/python -m pytest ...`**; the venv is a symlink to the main checkout. NEVER `uv pip install` / `uv sync` in the worktree (`uv lock` is fine). Verify `elspeth.__file__` points into the worktree before trusting a result.
- Zero `getattr` / `hasattr` in new src AND test files (masquerade gate is whole-repo, tests included). No `.get()` on dicts (project policy). No `isinstance` against pypdfium2 types (ADR-032: parse into an owned type). No `@trust_boundary` on the renderer.
- Terminal-vocabulary lint: never compare a symbol named `*_outcome`/`*_path` to a string literal; never use the identifier `is_terminal`; the worker's result discriminator is a `StrEnum` with values outside `{success, failure, transient, completed, failed, quarantined, ...}`.
- Tier model (`docs/contracts/plugin-protocol.md:69-91`): a document that cannot be rendered is Tier-2 pipeline data whose VALUE failed the operation → `TransformResult.error(..., retryable=False)` → the engine records FAILED and routes the row via `on_error`; the run continues. Our own seam failing (`BrokenProcessPool`, payload-store `IntegrityError`, a worker exception outside the protocol) is Tier-1 → raise.
- `pypdfium2` goes in BASE `dependencies` (pyproject rule `:86-87`), NOT an extra, NOT `OPTIONAL_PLUGIN_IMPORT_MODULES`. mypy stubless override for `pypdfium2`, `pypdfium2.*`, `pypdfium2_raw`, `pypdfium2_raw.*` (no `py.typed` — verified).
- `determinism = Determinism.IO_READ`, `creates_tokens = True`, `passes_through_input = True`, `plugin_version = "1.0.0"`, `policy_capabilities` left at default, no semantics declarations, `capability_tags = ("pdf", "rasterize", "image", "blob", "fan-out")`.
- Emitted fields (all names configurable; defaults): `page_blob_ref` (str, 64-hex PNG ref), `page_number` (int, **1-based**), `document_id` (str = the input PDF's payload ref), `page_mime_type` (str, always `image/png`), `page_size_bytes` (int), `page_width_px` (int), `page_height_px` (int). Parent `blob_*` fields pass through untouched (provenance).
- Caps (pydantic `Field` with module-level defaults and hard `le=` ceilings): `dpi` default 150, `ge=36, le=300`; `max_input_bytes` default `50 * 1024 * 1024`, `le=200 * 1024 * 1024`; `max_pages` default 200, `le=2000`; `max_page_pixels` default `25_000_000`, `le=50_000_000`; `max_page_bytes` default `BINARY_DOCUMENT_MAX_BYTES`, `le=BINARY_DOCUMENT_MAX_BYTES`; `render_timeout_seconds` default 120, `le=900`; `worker_memory_limit_bytes` default `2 * 1024**3`, `le=8 * 1024**3`. `on_page_failure: Literal["fail_document", "emit_rendered"]` default `fail_document`.
- New `TransformErrorCategory` literals: `pdf_encrypted`, `pdf_malformed`, `pdf_page_render_failed`, `pdf_page_too_large`, `render_timeout`. Reuse `too_many_rows` (page cap), `blob_too_large` (input cap), `blob_not_found`, `invalid_input`, `missing_field`.
- Commit after every task with `git add <explicit paths>`; run `ruff format` + `ruff check` on touched files before each commit; the `source_file_hash` of any touched plugin is recomputed AFTER formatting as the LAST edit.
- Full `pytest tests/` (`-n 24`) runs in Task 6 only; earlier tasks run scoped suites. If the full run cannot complete on this mid-refactor branch, report exactly what failed and why — do not paper over it.

---

## File structure

| Path | Responsibility |
|---|---|
| `src/elspeth/plugins/infrastructure/rasterize/__init__.py` | package marker (docstring only) |
| `src/elspeth/plugins/infrastructure/rasterize/protocol.py` | picklable frozen dataclasses + `StrEnum`s crossing the process boundary; NO third-party imports |
| `src/elspeth/plugins/infrastructure/rasterize/png.py` | stdlib PNG encoder for packed RGB buffers |
| `src/elspeth/plugins/infrastructure/rasterize/worker.py` | top-level `rasterize_document(request)` and `worker_initializer(...)`; the ONLY module that imports pypdfium2 (inside the function) |
| `src/elspeth/plugins/infrastructure/rasterize/renderer.py` | `PoolRenderer`: pool lifecycle, wall-clock timeout, orphan kill, temp-dir ownership; maps pool failures to Tier-1 / typed outcomes |
| `src/elspeth/plugins/transforms/pdf_rasterize.py` | the plugin: config, validation, payload-store I/O, row emission, probe |
| `src/elspeth/contracts/errors.py` | +5 `TransformErrorCategory` literals |
| `tests/fixtures/pdf_documents.py` | `minimal_pdf(page_count, width_pt, height_pt)` generator; `MALFORMED_PDF`; `ENCRYPTED_PDF_PATH` |
| `tests/fixtures/pdf/encrypted_aes128_user_secret.pdf` | already staged (944 bytes; user password `secret`) |
| `tests/unit/plugins/infrastructure/rasterize/test_{png,protocol,worker,renderer}.py` | seam tests |
| `tests/unit/plugins/transforms/test_pdf_rasterize.py` | plugin tests |
| `tests/integration/pipeline/test_pdf_rasterize_pipeline.py` | expand group + quarantine routing end to end |

---

### Task 1: Dependency, protocol, PNG encoder, worker

**Files:**
- Modify: `pyproject.toml` (base deps block ending `:88-90`; mypy stubless block `:369-377`)
- Modify: `uv.lock` (via `uv lock`)
- Create: `src/elspeth/plugins/infrastructure/rasterize/__init__.py`, `protocol.py`, `png.py`, `worker.py`
- Create: `tests/fixtures/pdf_documents.py`, `tests/unit/plugins/infrastructure/rasterize/__init__.py`, `test_png.py`, `test_protocol.py`, `test_worker.py`

**Interfaces:**
- Produces `protocol.py`:

```python
"""Picklable messages crossing the rasterize worker process boundary.

No third-party imports: this module is loaded by BOTH the parent process
(which never imports pypdfium2) and the spawned worker.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class DocumentRefusalKind(StrEnum):
    """Why a whole document could not be rasterized (a Tier-2 row outcome)."""

    ENCRYPTED = "encrypted"          # PdfiumError err_code 4 (password/security)
    MALFORMED = "malformed"          # PdfiumError err_code 3 / any other open failure
    TOO_MANY_PAGES = "too_many_pages"


class PageRefusalKind(StrEnum):
    """Why one page could not be rendered within the configured limits."""

    OVERSIZE_PIXELS = "oversize_pixels"   # declared size × dpi exceeds max_page_pixels (checked BEFORE render)
    OVERSIZE_BYTES = "oversize_bytes"     # encoded PNG exceeds max_page_bytes
    MEMORY_EXHAUSTED = "memory_exhausted" # MemoryError under RLIMIT_AS
    RENDER_ERROR = "render_error"         # PdfiumError during page load/render


@dataclass(frozen=True, slots=True)
class RasterizeRequest:
    pdf_bytes: bytes
    dpi: int
    max_pages: int
    max_page_pixels: int
    max_page_bytes: int
    output_dir: Path  # parent-owned temp dir; worker writes page-<n>.png files here


@dataclass(frozen=True, slots=True)
class RenderedPage:
    page_number: int  # 1-based
    png_path: Path
    width_px: int
    height_px: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class RefusedPage:
    page_number: int
    kind: PageRefusalKind
    detail: str


@dataclass(frozen=True, slots=True)
class DocumentRefusal:
    kind: DocumentRefusalKind
    detail: str
    page_count: int | None = None


@dataclass(frozen=True, slots=True)
class RasterizeResponse:
    page_count: int
    rendered: tuple[RenderedPage, ...]
    refused: tuple[RefusedPage, ...]


RasterizeOutcome = RasterizeResponse | DocumentRefusal
```

- Produces `png.py`: `encode_rgb_png(buffer: bytes | bytearray | memoryview, *, width: int, height: int, stride: int) -> bytes` (raises `ValueError` if `len(buffer) != stride * height` or `stride < width * 3`).
- Produces `worker.py`: `rasterize_document(request: RasterizeRequest) -> RasterizeOutcome` (module-level, picklable by reference) and `worker_initializer(memory_limit_bytes: int, cpu_seconds: int) -> None`, plus `class CpuBudgetExceeded(Exception)`.

- [ ] **Step 1: Dependency + mypy override, then lock**

In `pyproject.toml` base `dependencies`, immediately after the `beautifulsoup4` line (end of the `# === Plugin catalog dependencies ===` block):

```toml
    "pypdfium2>=5.13,<6",  # backs the pdf_rasterize transform (imported only inside its worker subprocess)
```

In the `[[tool.mypy.overrides]]` stubless block (`module = [...]` list containing `"sqlcipher3.*"`), append:

```toml
    "pypdfium2",
    "pypdfium2.*",
    "pypdfium2_raw",
    "pypdfium2_raw.*",
```

Run: `cd /home/john/elspeth/.claude/worktrees/pdf-rasterize && uv lock 2>&1 | tail -3 && git diff --stat uv.lock`
Expected: `uv.lock` gains a `pypdfium2` package entry; NO other package versions move (inspect `git diff uv.lock | grep '^[-+]name'` — only `+name = "pypdfium2"`). pypdfium2 5.13.0 is already installed in the shared venv.

- [ ] **Step 2: Write the failing PNG encoder test**

`tests/unit/plugins/infrastructure/rasterize/test_png.py`:

```python
"""Stdlib PNG encoder for packed RGB buffers."""

from __future__ import annotations

import struct
import zlib

import pytest

from elspeth.plugins.infrastructure.rasterize.png import encode_rgb_png

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _chunks(png: bytes) -> list[tuple[bytes, bytes]]:
    assert png[:8] == PNG_MAGIC
    out: list[tuple[bytes, bytes]] = []
    offset = 8
    while offset < len(png):
        (length,) = struct.unpack(">I", png[offset : offset + 4])
        kind = png[offset + 4 : offset + 8]
        data = png[offset + 8 : offset + 8 + length]
        (crc,) = struct.unpack(">I", png[offset + 8 + length : offset + 12 + length])
        assert crc == zlib.crc32(kind + data) & 0xFFFFFFFF
        out.append((kind, data))
        offset += 12 + length
    return out


def test_encodes_2x2_rgb_with_filter_zero_scanlines() -> None:
    buffer = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])
    png = encode_rgb_png(buffer, width=2, height=2, stride=6)
    chunks = _chunks(png)
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    ihdr = chunks[0][1]
    assert struct.unpack(">IIBBBBB", ihdr) == (2, 2, 8, 2, 0, 0, 0)
    raw = zlib.decompress(chunks[1][1])
    assert raw == b"\x00" + buffer[:6] + b"\x00" + buffer[6:]


def test_padded_stride_drops_padding_bytes() -> None:
    buffer = bytes([1, 2, 3, 9, 9]) + bytes([4, 5, 6, 9, 9])
    png = encode_rgb_png(buffer, width=1, height=2, stride=5)
    raw = zlib.decompress(_chunks(png)[1][1])
    assert raw == b"\x00\x01\x02\x03\x00\x04\x05\x06"


@pytest.mark.parametrize(
    ("width", "height", "stride", "length"),
    [(2, 2, 6, 11), (2, 2, 5, 10), (0, 1, 0, 0), (1, 0, 3, 0)],
)
def test_rejects_inconsistent_geometry(width: int, height: int, stride: int, length: int) -> None:
    with pytest.raises(ValueError):
        encode_rgb_png(bytes(length), width=width, height=height, stride=stride)
```

- [ ] **Step 3: Run it — expect ImportError**

Run: `PYTHONPATH=/home/john/elspeth/.claude/worktrees/pdf-rasterize/src /home/john/elspeth/.venv/bin/python -m pytest tests/unit/plugins/infrastructure/rasterize/test_png.py -q -n 0`
Expected: FAIL, `ModuleNotFoundError: elspeth.plugins.infrastructure.rasterize`.

- [ ] **Step 4: Implement `png.py`**

```python
"""Minimal PNG encoder for packed 8-bit RGB scanlines (no Pillow).

The worker renders with pypdfium2 ``rev_byteorder=True`` which yields a packed
RGB buffer; this encoder writes filter-type-0 scanlines into one IDAT.
"""

from __future__ import annotations

import struct
import zlib

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_COLOUR_TYPE_RGB = 2
_BYTES_PER_PIXEL = 3


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def encode_rgb_png(buffer: bytes | bytearray | memoryview, *, width: int, height: int, stride: int) -> bytes:
    """Encode a top-to-bottom packed RGB buffer as PNG.

    Raises ``ValueError`` when the declared geometry does not describe the buffer.
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"PNG geometry must be positive, got {width}x{height}")
    row_bytes = width * _BYTES_PER_PIXEL
    if stride < row_bytes:
        raise ValueError(f"stride {stride} is smaller than {row_bytes} bytes per row")
    if len(buffer) != stride * height:
        raise ValueError(f"buffer has {len(buffer)} bytes; expected stride*height = {stride * height}")
    view = memoryview(buffer)
    scanlines = bytearray()
    for row in range(height):
        start = row * stride
        scanlines += b"\x00"
        scanlines += view[start : start + row_bytes]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, _COLOUR_TYPE_RGB, 0, 0, 0)
    return _PNG_MAGIC + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(bytes(scanlines), 6)) + _chunk(b"IEND", b"")
```

Create `src/elspeth/plugins/infrastructure/rasterize/__init__.py` with only a docstring: `"""ELSPETH-owned out-of-process PDF rasterizer seam (not a plugin directory)."""` and `tests/unit/plugins/infrastructure/rasterize/__init__.py` empty (check whether sibling test dirs under `tests/unit/plugins/infrastructure/` carry `__init__.py`; match them).

- [ ] **Step 5: Run — expect PASS**

Same command as Step 3. Expected: 6 passed.

- [ ] **Step 6: Write `protocol.py`** exactly as in the Interfaces block, and `tests/unit/plugins/infrastructure/rasterize/test_protocol.py`:

```python
from __future__ import annotations

import pickle
from pathlib import Path

from elspeth.plugins.infrastructure.rasterize.protocol import (
    DocumentRefusal,
    DocumentRefusalKind,
    PageRefusalKind,
    RasterizeRequest,
    RasterizeResponse,
    RefusedPage,
    RenderedPage,
)


def test_messages_round_trip_through_pickle() -> None:
    request = RasterizeRequest(pdf_bytes=b"%PDF-", dpi=72, max_pages=1, max_page_pixels=10, max_page_bytes=10, output_dir=Path("/tmp/x"))
    response = RasterizeResponse(
        page_count=2,
        rendered=(RenderedPage(page_number=1, png_path=Path("/tmp/x/page-1.png"), width_px=1, height_px=1, size_bytes=70),),
        refused=(RefusedPage(page_number=2, kind=PageRefusalKind.RENDER_ERROR, detail="boom"),),
    )
    refusal = DocumentRefusal(kind=DocumentRefusalKind.ENCRYPTED, detail="password", page_count=None)
    for message in (request, response, refusal):
        assert pickle.loads(pickle.dumps(message)) == message


def test_discriminator_values_stay_outside_the_terminal_vocabulary() -> None:
    forbidden = {"success", "failure", "transient", "completed", "failed", "quarantined", "buffered", "coalesced"}
    values = {member.value for member in DocumentRefusalKind} | {member.value for member in PageRefusalKind}
    assert values.isdisjoint(forbidden)
```

Run: the protocol test file. Expected: 2 passed.

- [ ] **Step 7: Write the PDF fixture module** `tests/fixtures/pdf_documents.py`:

```python
"""Hand-built PDF documents for rasterizer tests (valid xref, no external tools)."""

from __future__ import annotations

from pathlib import Path

ENCRYPTED_PDF_PATH = Path(__file__).parent / "pdf" / "encrypted_aes128_user_secret.pdf"
MALFORMED_PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n"
NOT_A_PDF = b"\x89PNG\r\n\x1a\nnot-a-pdf"


def minimal_pdf(page_count: int = 1, *, width_pt: float = 200.0, height_pt: float = 100.0) -> bytes:
    """Return a valid single-font PDF with ``page_count`` pages of the given MediaBox."""
    if page_count < 1:
        raise ValueError("page_count must be >= 1")
    objs: list[bytes] = []
    # 1 catalog, 2 pages root, 3 font, then per page: page + content stream
    first_page_obj = 4
    kids = " ".join(f"{first_page_obj + 2 * i} 0 R" for i in range(page_count))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i in range(page_count):
        content_obj = first_page_obj + 2 * i + 1
        objs.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width_pt:g} {height_pt:g}] "
                f"/Contents {content_obj} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
            ).encode()
        )
        stream = f"BT /F1 24 Tf 20 40 Td (Page {i + 1}) Tj ET".encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, obj in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)
```

- [ ] **Step 8: Write the failing worker tests** `tests/unit/plugins/infrastructure/rasterize/test_worker.py` (these run pypdfium2 IN-PROCESS — the worker function is plain Python; process isolation is Task 2's concern):

```python
from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.plugins.infrastructure.rasterize.protocol import (
    DocumentRefusal,
    DocumentRefusalKind,
    PageRefusalKind,
    RasterizeRequest,
    RasterizeResponse,
)
from elspeth.plugins.infrastructure.rasterize.worker import rasterize_document
from tests.fixtures.pdf_documents import ENCRYPTED_PDF_PATH, MALFORMED_PDF, NOT_A_PDF, minimal_pdf

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _request(pdf: bytes, tmp_path: Path, **overrides: int) -> RasterizeRequest:
    values = {"dpi": 72, "max_pages": 10, "max_page_pixels": 1_000_000, "max_page_bytes": 5 * 1024 * 1024}
    values.update(overrides)
    return RasterizeRequest(pdf_bytes=pdf, output_dir=tmp_path, **values)


def test_renders_every_page_to_png_files_in_order(tmp_path: Path) -> None:
    result = rasterize_document(_request(minimal_pdf(3), tmp_path))
    assert type(result) is RasterizeResponse
    assert result.page_count == 3
    assert result.refused == ()
    assert [page.page_number for page in result.rendered] == [1, 2, 3]
    for page in result.rendered:
        data = page.png_path.read_bytes()
        assert data[:8] == PNG_MAGIC
        assert page.size_bytes == len(data)
        assert (page.width_px, page.height_px) == (200, 100)  # 200x100 pt at 72 dpi
        assert page.png_path.parent == tmp_path


def test_dpi_scales_geometry_by_ceil(tmp_path: Path) -> None:
    result = rasterize_document(_request(minimal_pdf(1, width_pt=100.5, height_pt=50.0), tmp_path, dpi=144))
    assert type(result) is RasterizeResponse
    assert (result.rendered[0].width_px, result.rendered[0].height_px) == (201, 100)


def test_encrypted_document_is_refused_as_encrypted(tmp_path: Path) -> None:
    result = rasterize_document(_request(ENCRYPTED_PDF_PATH.read_bytes(), tmp_path))
    assert result == DocumentRefusal(kind=DocumentRefusalKind.ENCRYPTED, detail=result.detail, page_count=None)
    assert "password" in result.detail.lower()


@pytest.mark.parametrize("payload", [MALFORMED_PDF, NOT_A_PDF, b""])
def test_unparseable_document_is_refused_as_malformed(tmp_path: Path, payload: bytes) -> None:
    result = rasterize_document(_request(payload, tmp_path))
    assert type(result) is DocumentRefusal
    assert result.kind is DocumentRefusalKind.MALFORMED


def test_page_cap_refuses_the_whole_document_before_rendering(tmp_path: Path) -> None:
    result = rasterize_document(_request(minimal_pdf(3), tmp_path, max_pages=2))
    assert result == DocumentRefusal(kind=DocumentRefusalKind.TOO_MANY_PAGES, detail=result.detail, page_count=3)
    assert list(tmp_path.iterdir()) == []


def test_oversize_page_is_refused_from_declared_size_without_rendering(tmp_path: Path) -> None:
    result = rasterize_document(_request(minimal_pdf(2), tmp_path, max_page_pixels=100))
    assert type(result) is RasterizeResponse
    assert result.rendered == ()
    assert [(page.page_number, page.kind) for page in result.refused] == [
        (1, PageRefusalKind.OVERSIZE_PIXELS),
        (2, PageRefusalKind.OVERSIZE_PIXELS),
    ]
    assert list(tmp_path.iterdir()) == []


def test_oversize_png_is_refused_and_its_file_removed(tmp_path: Path) -> None:
    result = rasterize_document(_request(minimal_pdf(1), tmp_path, max_page_bytes=64))
    assert type(result) is RasterizeResponse
    assert result.rendered == ()
    assert result.refused[0].kind is PageRefusalKind.OVERSIZE_BYTES
    assert list(tmp_path.iterdir()) == []


def test_worker_module_does_not_import_pypdfium2_at_module_scope() -> None:
    import sys

    import elspeth.plugins.infrastructure.rasterize.protocol  # noqa: F401  (protocol must be import-clean)

    source = Path(rasterize_document.__code__.co_filename).read_text()
    module_level = [line for line in source.splitlines() if line.startswith("import pypdfium2") or line.startswith("from pypdfium2")]
    assert module_level == []
    assert "pypdfium2" not in sys.modules or True  # in-process tests may have loaded it; the assertion above is the contract
```

Run: `... -m pytest tests/unit/plugins/infrastructure/rasterize/test_worker.py -q -n 0`. Expected: FAIL (`worker` module missing).

- [ ] **Step 9: Implement `worker.py`**

```python
"""Rasterize worker: the ONLY module that imports pypdfium2, and only inside a function.

Runs in a spawn-context subprocess owned by ``renderer.PoolRenderer``. Everything
crossing the boundary is a ``protocol`` message. Document/page problems become
refusals (Tier-2 data outcomes); anything else raises and is treated as a code bug
by the parent.
"""

from __future__ import annotations

import math
import signal
import sys
from pathlib import Path

from elspeth.plugins.infrastructure.rasterize.png import encode_rgb_png
from elspeth.plugins.infrastructure.rasterize.protocol import (
    DocumentRefusal,
    DocumentRefusalKind,
    PageRefusalKind,
    RasterizeOutcome,
    RasterizeRequest,
    RasterizeResponse,
    RefusedPage,
    RenderedPage,
)

_PDFIUM_ERR_PASSWORD = 4
_PDFIUM_ERR_SECURITY = 5
_POINTS_PER_INCH = 72.0


class CpuBudgetExceeded(Exception):
    """Raised inside the worker when RLIMIT_CPU's soft limit delivers SIGXCPU."""


def _raise_cpu_budget(signum: int, frame: object) -> None:
    raise CpuBudgetExceeded(f"worker exceeded its CPU budget (signal {signum})")


def worker_initializer(memory_limit_bytes: int, cpu_seconds: int) -> None:
    """Apply per-worker resource limits. Linux-only; a no-op elsewhere."""
    if not sys.platform.startswith("linux"):
        return
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (memory_limit_bytes, memory_limit_bytes))
    signal.signal(signal.SIGXCPU, _raise_cpu_budget)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 5))


def _pdfium_error_code(exc: BaseException) -> int | None:
    # PdfiumError carries ``err_code`` (pypdfium2/_helpers/misc.py:7-21); parse it, never isinstance.
    code = exc.__dict__["err_code"] if "err_code" in exc.__dict__ else None
    return code if type(code) is int else None


def rasterize_document(request: RasterizeRequest) -> RasterizeOutcome:
    """Parse once, render every page, write PNGs into ``request.output_dir``."""
    import pypdfium2  # deferred: initialises libpdfium + registers atexit in THIS process only
    from pypdfium2 import PdfiumError

    try:
        document = pypdfium2.PdfDocument(request.pdf_bytes)
    except PdfiumError as exc:
        code = _pdfium_error_code(exc)
        if code in (_PDFIUM_ERR_PASSWORD, _PDFIUM_ERR_SECURITY):
            return DocumentRefusal(kind=DocumentRefusalKind.ENCRYPTED, detail=str(exc))
        return DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail=str(exc))
    except (ValueError, TypeError) as exc:  # pypdfium2 rejects empty/odd inputs before pdfium sees them
        return DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail=str(exc))

    try:
        page_count = len(document)
        if page_count > request.max_pages:
            return DocumentRefusal(
                kind=DocumentRefusalKind.TOO_MANY_PAGES,
                detail=f"document declares {page_count} pages; max_pages is {request.max_pages}",
                page_count=page_count,
            )
        scale = request.dpi / _POINTS_PER_INCH
        rendered: list[RenderedPage] = []
        refused: list[RefusedPage] = []
        for index in range(page_count):
            page_number = index + 1
            outcome = _render_page(document, index, page_number, scale, request)
            if type(outcome) is RenderedPage:
                rendered.append(outcome)
            else:
                refused.append(outcome)
        return RasterizeResponse(page_count=page_count, rendered=tuple(rendered), refused=tuple(refused))
    finally:
        document.close()


def _render_page(document: object, index: int, page_number: int, scale: float, request: RasterizeRequest) -> RenderedPage | RefusedPage:
    from pypdfium2 import PdfiumError

    try:
        page = document[index]  # type: ignore[index]  # PdfDocument.__getitem__; parsed, not typed
    except PdfiumError as exc:
        return RefusedPage(page_number=page_number, kind=PageRefusalKind.RENDER_ERROR, detail=str(exc))
    try:
        width_pt, height_pt = page.get_size()
        width_px = math.ceil(width_pt * scale)
        height_px = math.ceil(height_pt * scale)
        if width_px <= 0 or height_px <= 0 or width_px * height_px > request.max_page_pixels:
            return RefusedPage(
                page_number=page_number,
                kind=PageRefusalKind.OVERSIZE_PIXELS,
                detail=f"page would render to {width_px}x{height_px} px; max_page_pixels is {request.max_page_pixels}",
            )
        try:
            bitmap = page.render(scale=scale, rev_byteorder=True, draw_annots=False)
            png = encode_rgb_png(bitmap.buffer, width=bitmap.width, height=bitmap.height, stride=bitmap.stride)
        except MemoryError:
            return RefusedPage(page_number=page_number, kind=PageRefusalKind.MEMORY_EXHAUSTED, detail="render exceeded the worker memory limit")
        except PdfiumError as exc:
            return RefusedPage(page_number=page_number, kind=PageRefusalKind.RENDER_ERROR, detail=str(exc))
        if len(png) > request.max_page_bytes:
            return RefusedPage(
                page_number=page_number,
                kind=PageRefusalKind.OVERSIZE_BYTES,
                detail=f"encoded page is {len(png)} bytes; max_page_bytes is {request.max_page_bytes}",
            )
        png_path = Path(request.output_dir) / f"page-{page_number}.png"
        png_path.write_bytes(png)
        return RenderedPage(page_number=page_number, png_path=png_path, width_px=bitmap.width, height_px=bitmap.height, size_bytes=len(png))
    finally:
        page.close()
```

Note on `_pdfium_error_code`: `exc.__dict__["err_code"]` is the sanctioned no-`getattr` read; verify with `python -c "import pypdfium2; ..."` that `PdfiumError` instances carry `err_code` in `__dict__` (lane 3 reports `err_code` is set as an attribute in `_helpers/misc.py:7-21`). If it is a class-level descriptor instead, switch to `vars(exc)`; never `getattr`. If `bitmap.width`/`.stride` attribute reads trip the trust-tier R-rules in `elspeth-lints`, wrap the three reads in a tiny owned `@dataclass(frozen=True) _Bitmap` constructed once — ADR-032.

- [ ] **Step 10: Run the worker tests — expect PASS**

Same command as Step 8. Expected: 10 passed. If the malformed-`b""` case raises something other than `PdfiumError`/`ValueError`/`TypeError`, record the exact exception in the test's parametrize id and catch it explicitly — do NOT add a bare `except Exception`.

- [ ] **Step 11: mypy + ruff on the new package**

Run: `PYTHONPATH=... /home/john/elspeth/.venv/bin/python -m mypy src/elspeth/plugins/infrastructure/rasterize && /home/john/elspeth/.venv/bin/ruff format src/elspeth/plugins/infrastructure/rasterize tests/unit/plugins/infrastructure/rasterize tests/fixtures/pdf_documents.py && /home/john/elspeth/.venv/bin/ruff check <same paths>`
Expected: clean. Fix any `warn_unused_ignores` complaint by removing the `type: ignore` rather than keeping it.

- [ ] **Step 12: Commit**

```bash
git add pyproject.toml uv.lock src/elspeth/plugins/infrastructure/rasterize tests/unit/plugins/infrastructure/rasterize tests/fixtures/pdf_documents.py tests/fixtures/pdf/encrypted_aes128_user_secret.pdf
git commit -m "feat(rasterize): pypdfium2 worker seam — protocol, stdlib PNG encoder, rasterize_document"
```

---

### Task 2: `PoolRenderer` — process isolation, timeout, orphan kill

**Files:**
- Create: `src/elspeth/plugins/infrastructure/rasterize/renderer.py`
- Create: `tests/unit/plugins/infrastructure/rasterize/test_renderer.py`, `tests/fixtures/rasterize_fakes.py`

**Interfaces:**
- Consumes: Task 1's `protocol` types, `worker.rasterize_document`, `worker.worker_initializer`.
- Produces:

```python
@dataclass(frozen=True, slots=True)
class RenderLimits:
    dpi: int
    max_pages: int
    max_page_pixels: int
    max_page_bytes: int
    render_timeout_seconds: int
    worker_memory_limit_bytes: int


@dataclass(frozen=True, slots=True)
class RenderTimedOut:
    timeout_seconds: int


RenderResult = RasterizeResponse | DocumentRefusal | RenderTimedOut


class PoolRenderer:
    def __init__(self, limits: RenderLimits, *, worker: Callable[[RasterizeRequest], RasterizeOutcome] = rasterize_document) -> None: ...
    def render(self, pdf_bytes: bytes) -> tuple[RenderResult, Path | None]:
        """Returns (result, output_dir). The CALLER owns output_dir and must call `discard(output_dir)` in a finally."""
    def discard(self, output_dir: Path | None) -> None: ...
    def close(self) -> None: ...
```

Behaviour: pool is created lazily on first `render` as `ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"), initializer=worker_initializer, initargs=(limits.worker_memory_limit_bytes, limits.render_timeout_seconds), max_tasks_per_child=1)`. `render` makes `output_dir = Path(tempfile.mkdtemp(prefix="elspeth-rasterize-"))`, submits `RasterizeRequest(...)`, waits `future.result(timeout=limits.render_timeout_seconds)`. On `FuturesTimeoutError`: copy the `query.py:143-167` orphan-kill sequence verbatim (cancel → snapshot `_processes` → `shutdown(wait=False, cancel_futures=True)` → `proc.kill()` → set `self._pool = None` so the next call rebuilds), then return `RenderTimedOut(...)`. On `BrokenProcessPool`: shut down, set `self._pool = None`, `raise RuntimeError("pdf_rasterize worker died outside its result protocol — this is a code bug, not a document problem") from exc` (Tier-1). Any other exception propagating from the worker (including `CpuBudgetExceeded` — CPU budget IS a document-caused refusal: map it to `RenderTimedOut`; `MemoryError` escaping the per-page handler → also `RenderTimedOut`? No: MemoryError outside page rendering means the document parse itself blew the limit → return `DocumentRefusal(kind=MALFORMED, detail="document parse exceeded the worker memory limit")`) — spell each mapping out in code with a comment. `discard` = `shutil.rmtree(output_dir, ignore_errors=True)` when not None. `close` = shutdown pool (`wait=True`) and null it.

- [ ] **Step 1: Fake worker module** `tests/fixtures/rasterize_fakes.py` (must be importable by the spawned child — `tests/` is on `sys.path` via pytest `pythonpath`; assert that in the test by checking `tests.fixtures.rasterize_fakes` resolves in-process first):

```python
"""Worker callables for PoolRenderer tests (module-level so they pickle by reference)."""

from __future__ import annotations

import os
import time

from elspeth.plugins.infrastructure.rasterize.protocol import RasterizeOutcome, RasterizeRequest, RasterizeResponse


def sleeping_worker(request: RasterizeRequest) -> RasterizeOutcome:
    time.sleep(30)
    return RasterizeResponse(page_count=0, rendered=(), refused=())


def crashing_worker(request: RasterizeRequest) -> RasterizeOutcome:
    os._exit(3)


def raising_worker(request: RasterizeRequest) -> RasterizeOutcome:
    raise KeyError("worker bug")
```

- [ ] **Step 2: Failing tests** `tests/unit/plugins/infrastructure/rasterize/test_renderer.py`:

```python
from __future__ import annotations

import os
from pathlib import Path

import pytest

from elspeth.plugins.infrastructure.rasterize.protocol import DocumentRefusalKind, RasterizeResponse
from elspeth.plugins.infrastructure.rasterize.renderer import PoolRenderer, RenderLimits, RenderTimedOut
from tests.fixtures.pdf_documents import ENCRYPTED_PDF_PATH, minimal_pdf
from tests.fixtures.rasterize_fakes import crashing_worker, raising_worker, sleeping_worker


def _limits(**overrides: int) -> RenderLimits:
    values = {
        "dpi": 72, "max_pages": 10, "max_page_pixels": 1_000_000, "max_page_bytes": 5 * 1024 * 1024,
        "render_timeout_seconds": 20, "worker_memory_limit_bytes": 2 * 1024**3,
    }
    values.update(overrides)
    return RenderLimits(**values)


def test_renders_in_a_subprocess_and_hands_back_an_owned_temp_dir() -> None:
    renderer = PoolRenderer(_limits())
    try:
        result, output_dir = renderer.render(minimal_pdf(2))
        try:
            assert type(result) is RasterizeResponse
            assert output_dir is not None and output_dir.is_dir()
            assert sorted(path.name for path in output_dir.iterdir()) == ["page-1.png", "page-2.png"]
            assert all(page.png_path.parent == output_dir for page in result.rendered)
        finally:
            renderer.discard(output_dir)
        assert output_dir is not None and not output_dir.exists()
    finally:
        renderer.close()


def test_document_refusal_passes_through() -> None:
    renderer = PoolRenderer(_limits())
    try:
        result, output_dir = renderer.render(ENCRYPTED_PDF_PATH.read_bytes())
        renderer.discard(output_dir)
        assert result.kind is DocumentRefusalKind.ENCRYPTED  # type: ignore[union-attr]
    finally:
        renderer.close()


def test_timeout_kills_the_orphan_and_the_renderer_stays_usable() -> None:
    renderer = PoolRenderer(_limits(render_timeout_seconds=1), worker=sleeping_worker)
    try:
        result, output_dir = renderer.render(b"%PDF-")
        renderer.discard(output_dir)
        assert result == RenderTimedOut(timeout_seconds=1)
        # the stuck process must be dead, not orphaned
        assert renderer.live_worker_pids() == ()
        # and a fresh pool serves the next document
        renderer2_result, output_dir2 = PoolRenderer.__dict__["render"](renderer, b"%PDF-")
        renderer.discard(output_dir2)
        assert renderer2_result == RenderTimedOut(timeout_seconds=1)
    finally:
        renderer.close()


def test_worker_death_is_a_tier1_code_bug() -> None:
    renderer = PoolRenderer(_limits(), worker=crashing_worker)
    try:
        with pytest.raises(RuntimeError, match="code bug"):
            renderer.render(b"%PDF-")
    finally:
        renderer.close()


def test_worker_exception_outside_the_protocol_is_a_tier1_code_bug() -> None:
    renderer = PoolRenderer(_limits(), worker=raising_worker)
    try:
        with pytest.raises(RuntimeError, match="code bug"):
            renderer.render(b"%PDF-")
    finally:
        renderer.close()


@pytest.mark.skipif(not os.path.exists("/proc"), reason="fd census needs /proc")
def test_close_leaks_no_file_descriptors() -> None:
    before = len(os.listdir("/proc/self/fd"))
    for _ in range(3):
        renderer = PoolRenderer(_limits())
        result, output_dir = renderer.render(minimal_pdf(1))
        renderer.discard(output_dir)
        renderer.close()
    assert len(os.listdir("/proc/self/fd")) - before < 10
```

Replace the awkward `PoolRenderer.__dict__["render"](renderer, ...)` line with a plain `renderer.render(b"%PDF-")` — it is there only to remind you not to reach for `getattr`; write the plain call. Add a public `live_worker_pids() -> tuple[int, ...]` on `PoolRenderer` that returns pids of processes in the current pool that are alive (empty when the pool is None).

Run the file with `-n 0`. Expected: ImportError.

- [ ] **Step 3: Implement `renderer.py`** per the Interfaces block. Key excerpts:

```python
def render(self, pdf_bytes: bytes) -> tuple[RenderResult, Path | None]:
    output_dir = Path(tempfile.mkdtemp(prefix="elspeth-rasterize-"))
    request = RasterizeRequest(
        pdf_bytes=pdf_bytes, dpi=self._limits.dpi, max_pages=self._limits.max_pages,
        max_page_pixels=self._limits.max_page_pixels, max_page_bytes=self._limits.max_page_bytes, output_dir=output_dir,
    )
    pool = self._ensure_pool()
    future = pool.submit(self._worker, request)
    try:
        outcome = future.result(timeout=self._limits.render_timeout_seconds)
    except FuturesTimeoutError:
        self._kill_pool(future)
        return RenderTimedOut(timeout_seconds=self._limits.render_timeout_seconds), output_dir
    except BrokenProcessPool as exc:
        self._drop_pool()
        raise RuntimeError("pdf_rasterize worker died outside its result protocol — this is a code bug, not a document problem") from exc
    except CpuBudgetExceeded:
        # SIGXCPU fired inside the worker while it was between native calls: the document
        # consumed its CPU budget. Same row outcome as the wall clock; the worker is recycled
        # by max_tasks_per_child=1.
        return RenderTimedOut(timeout_seconds=self._limits.render_timeout_seconds), output_dir
    except MemoryError:
        # RLIMIT_AS tripped outside the per-page handler → the document parse itself blew the limit.
        return DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail="document parse exceeded the worker memory limit"), output_dir
    except Exception as exc:  # anything else came from OUR worker code: Tier-1
        self._drop_pool()
        raise RuntimeError("pdf_rasterize worker raised outside its result protocol — this is a code bug, not a document problem") from exc
    return outcome, output_dir
```

`_ensure_pool` builds the executor with `initializer=worker_initializer, initargs=(memory_limit, render_timeout_seconds)` and `max_tasks_per_child=1`. `_kill_pool(future)` = the `query.py:143-159` sequence. Because `except Exception` would also catch `CpuBudgetExceeded`/`MemoryError`, keep the specific clauses ABOVE it (order matters).

Note `except Exception` here is deliberate and narrow in effect (everything typed is handled above); it must re-raise as `RuntimeError` — never swallow. If ruff `BLE001` fires, add the specific `# noqa: BLE001` with the reason.

- [ ] **Step 4: Run — expect PASS** (`-n 0`; the timeout test takes ~2 s). If `tests.fixtures.rasterize_fakes` is not importable in the child, the spawned process's `sys.path` lacks the repo root: fix by asserting `Path.cwd()` is the worktree and adding a `conftest`-free solution — move the fakes to `src/elspeth/testing/rasterize_fakes.py` (the `elspeth.testing` package already exists and is importable everywhere). Prefer that location from the start if in doubt.

- [ ] **Step 5: mypy + ruff on `renderer.py` and the tests; commit**

```bash
git add src/elspeth/plugins/infrastructure/rasterize/renderer.py tests/unit/plugins/infrastructure/rasterize/test_renderer.py tests/fixtures/rasterize_fakes.py
git commit -m "feat(rasterize): PoolRenderer — spawn pool with rlimits, wall-clock timeout, orphan kill"
```

---

### Task 3: The `pdf_rasterize` plugin

**Files:**
- Modify: `src/elspeth/contracts/errors.py` (`TransformErrorCategory` Literal, `:489-605`; add a `# PDF rasterization (Tier 2 - document bytes that did not survive rendering)` group)
- Create: `src/elspeth/plugins/transforms/pdf_rasterize.py`
- Create: `tests/unit/plugins/transforms/test_pdf_rasterize.py`

**Interfaces:**
- Consumes: `PoolRenderer`, `RenderLimits`, `RenderTimedOut`, protocol types; `BaseTransform`, `TransformDataConfig`, `TransformResult`, `narrow_contract_to_output`, `create_schema_from_config`, `binary_document_signature_matches`, `BINARY_DOCUMENT_MAX_BYTES`, `PayloadNotFoundError`, `IntegrityError`, `FrameworkBugError`.
- Produces: class `PDFRasterize(BaseTransform)` with `name = "pdf_rasterize"`, config model `PDFRasterizeConfig(TransformDataConfig)`.

- [ ] **Step 1: Error-category literals** — in `contracts/errors.py` inside the `TransformErrorCategory` Literal, after the `blob_too_large` group, add:

```python
    # PDF rasterization (Tier 2 - document bytes that did not survive rendering)
    "pdf_encrypted",  # password/security-protected PDF; no password path exists
    "pdf_malformed",  # pdfium could not parse the document
    "pdf_page_render_failed",  # one or more pages refused (render error / memory) under on_page_failure=fail_document
    "pdf_page_too_large",  # a page exceeded max_page_pixels or max_page_bytes under on_page_failure=fail_document
    "render_timeout",  # the document exceeded render_timeout_seconds (wall clock or CPU budget)
```

- [ ] **Step 2: Failing plugin tests** `tests/unit/plugins/transforms/test_pdf_rasterize.py`. Use a hermetic renderer stub for most tests and the real `PoolRenderer` for one:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.payload_store import IntegrityError
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.plugins.infrastructure.rasterize.protocol import (
    DocumentRefusal, DocumentRefusalKind, PageRefusalKind, RasterizeResponse, RefusedPage, RenderedPage,
)
from elspeth.plugins.infrastructure.rasterize.renderer import RenderTimedOut
from elspeth.plugins.transforms.pdf_rasterize import PDFRasterize
from elspeth.testing import make_pipeline_row
from tests.fixtures.factories import make_context
from tests.fixtures.pdf_documents import ENCRYPTED_PDF_PATH, MALFORMED_PDF, minimal_pdf

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 60


class _StubRenderer:
    """Scripted renderer: returns the queued result and writes the PNG files it promises."""

    def __init__(self, result: Any, png_bytes: tuple[bytes, ...] = ()) -> None:
        self._result = result
        self._png_bytes = png_bytes
        self.calls: list[bytes] = []
        self.discarded: list[Path | None] = []
        self.closed = False

    def render(self, pdf_bytes: bytes) -> tuple[Any, Path | None]:
        self.calls.append(pdf_bytes)
        import tempfile
        output_dir = Path(tempfile.mkdtemp(prefix="stub-rasterize-"))
        if type(self._result) is RasterizeResponse:
            rendered = tuple(
                RenderedPage(page_number=page.page_number, png_path=output_dir / f"page-{page.page_number}.png",
                             width_px=page.width_px, height_px=page.height_px, size_bytes=len(data))
                for page, data in zip(self._result.rendered, self._png_bytes, strict=True)
            )
            for page, data in zip(rendered, self._png_bytes, strict=True):
                page.png_path.write_bytes(data)
            return RasterizeResponse(page_count=self._result.page_count, rendered=rendered, refused=self._result.refused), output_dir
        return self._result, output_dir

    def discard(self, output_dir: Path | None) -> None:
        self.discarded.append(output_dir)
        if output_dir is not None:
            import shutil
            shutil.rmtree(output_dir, ignore_errors=True)

    def close(self) -> None:
        self.closed = True


def _page(number: int) -> RenderedPage:
    return RenderedPage(page_number=number, png_path=Path("unset"), width_px=200, height_px=100, size_bytes=0)


def _transform(store: FilesystemPayloadStore, renderer: Any, **options: Any) -> PDFRasterize:
    transform = PDFRasterize({"schema": {"mode": "observed"}, **options})
    transform.on_start(make_context(payload_store=store))  # adapt to make_context's real signature
    transform.__dict__["_renderer"] = renderer
    return transform


@pytest.fixture
def store(tmp_path: Path) -> FilesystemPayloadStore:
    return FilesystemPayloadStore(tmp_path / "payloads")


def test_emits_one_row_per_page_with_page_metadata_and_parent_fields(store: FilesystemPayloadStore) -> None:
    pdf_ref = store.store(minimal_pdf(2))
    renderer = _StubRenderer(RasterizeResponse(page_count=2, rendered=(_page(1), _page(2)), refused=()), (PNG, PNG + b"x"))
    transform = _transform(store, renderer)
    result = transform.process(make_pipeline_row({"blob_ref": pdf_ref, "blob_filename": "a.pdf"}), make_context())
    assert result.status == "success" and result.is_multi_row
    rows = [row.to_dict() for row in result.rows]
    assert [row["page_number"] for row in rows] == [1, 2]
    assert {row["document_id"] for row in rows} == {pdf_ref}
    assert all(row["blob_ref"] == pdf_ref and row["blob_filename"] == "a.pdf" for row in rows)
    assert all(row["page_mime_type"] == "image/png" for row in rows)
    assert [store.retrieve(row["page_blob_ref"]) for row in rows] == [PNG, PNG + b"x"]
    assert [row["page_size_bytes"] for row in rows] == [len(PNG), len(PNG) + 1]
    assert rows[0]["page_width_px"] == 200 and rows[0]["page_height_px"] == 100
    assert result.rows[0].contract is result.rows[1].contract
    assert result.success_reason["action"] == "expanded_blob"
    assert result.success_reason["metadata"]["page_count"] == 2
    assert result.success_reason["metadata"]["refused_pages"] == []
    assert renderer.discarded and renderer.discarded[0] is not None and not renderer.discarded[0].exists()


def test_encrypted_document_is_a_typed_row_error_not_a_crash(store: FilesystemPayloadStore) -> None:
    ref = store.store(ENCRYPTED_PDF_PATH.read_bytes())
    transform = _transform(store, _StubRenderer(DocumentRefusal(kind=DocumentRefusalKind.ENCRYPTED, detail="Incorrect password")))
    result = transform.process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.status == "error" and result.retryable is False
    assert result.reason["reason"] == "pdf_encrypted"
    assert result.reason["blob_ref"] == ref


@pytest.mark.parametrize(
    ("refusal", "reason"),
    [
        (DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail="Data format error"), "pdf_malformed"),
        (DocumentRefusal(kind=DocumentRefusalKind.TOO_MANY_PAGES, detail="900 pages", page_count=900), "too_many_rows"),
        (RenderTimedOut(timeout_seconds=120), "render_timeout"),
    ],
)
def test_document_level_refusals_map_to_typed_reasons(store: FilesystemPayloadStore, refusal: Any, reason: str) -> None:
    ref = store.store(minimal_pdf(1))
    result = _transform(store, _StubRenderer(refusal)).process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.status == "error" and result.reason["reason"] == reason


def test_fail_document_refuses_the_whole_row_when_any_page_is_refused(store: FilesystemPayloadStore) -> None:
    ref = store.store(minimal_pdf(2))
    response = RasterizeResponse(page_count=2, rendered=(_page(1),), refused=(RefusedPage(2, PageRefusalKind.RENDER_ERROR, "boom"),))
    result = _transform(store, _StubRenderer(response, (PNG,))).process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.status == "error" and result.reason["reason"] == "pdf_page_render_failed"
    assert result.reason["refused_pages"] == [{"page_number": 2, "kind": "render_error", "detail": "boom"}]
    # nothing was persisted for a refused document
    assert not store.exists(__import__("hashlib").sha256(PNG).hexdigest())


def test_fail_document_uses_too_large_reason_for_size_refusals(store: FilesystemPayloadStore) -> None:
    ref = store.store(minimal_pdf(1))
    response = RasterizeResponse(page_count=1, rendered=(), refused=(RefusedPage(1, PageRefusalKind.OVERSIZE_BYTES, "big"),))
    result = _transform(store, _StubRenderer(response)).process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.reason["reason"] == "pdf_page_too_large"


def test_emit_rendered_emits_surviving_pages_and_records_the_gaps(store: FilesystemPayloadStore) -> None:
    ref = store.store(minimal_pdf(3))
    response = RasterizeResponse(page_count=3, rendered=(_page(1), _page(3)), refused=(RefusedPage(2, PageRefusalKind.MEMORY_EXHAUSTED, "oom"),))
    transform = _transform(store, _StubRenderer(response, (PNG, PNG)), on_page_failure="emit_rendered")
    result = transform.process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.status == "success"
    assert [row["page_number"] for row in result.rows] == [1, 3]
    assert result.success_reason["metadata"]["refused_pages"] == [{"page_number": 2, "kind": "memory_exhausted", "detail": "oom"}]


def test_emit_rendered_with_zero_survivors_is_still_a_row_error(store: FilesystemPayloadStore) -> None:
    ref = store.store(minimal_pdf(1))
    response = RasterizeResponse(page_count=1, rendered=(), refused=(RefusedPage(1, PageRefusalKind.RENDER_ERROR, "boom"),))
    result = _transform(store, _StubRenderer(response), on_page_failure="emit_rendered").process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.status == "error" and result.reason["reason"] == "pdf_page_render_failed"


def test_input_validation_precedes_rendering(store: FilesystemPayloadStore) -> None:
    renderer = _StubRenderer(DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail="never called"))
    transform = _transform(store, renderer)
    missing = transform.process(make_pipeline_row({"other": 1}), make_context())
    assert missing.reason["reason"] == "missing_field"
    bad_ref = transform.process(make_pipeline_row({"blob_ref": "nope"}), make_context())
    assert bad_ref.reason["reason"] == "invalid_input" and bad_ref.reason["error_type"] == "invalid_blob_ref"
    absent = transform.process(make_pipeline_row({"blob_ref": "0" * 64}), make_context())
    assert absent.reason["reason"] == "blob_not_found"
    not_pdf = transform.process(make_pipeline_row({"blob_ref": store.store(PNG)}), make_context())
    assert not_pdf.reason["reason"] == "invalid_input" and not_pdf.reason["error_type"] == "document_signature_mismatch"
    with pytest.raises(TypeError):
        transform.process(make_pipeline_row({"blob_ref": 12}), make_context())  # Tier-2 type violation: crash (blob_csv_expand precedent)
    assert renderer.calls == []


def test_input_over_max_input_bytes_is_blob_too_large(store: FilesystemPayloadStore) -> None:
    ref = store.store(minimal_pdf(1))
    transform = _transform(store, _StubRenderer(DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail="never")), max_input_bytes=64)
    result = transform.process(make_pipeline_row({"blob_ref": ref}), make_context())
    assert result.reason["reason"] == "blob_too_large"


def test_payload_store_integrity_error_propagates(store: FilesystemPayloadStore, tmp_path: Path) -> None:
    ref = store.store(minimal_pdf(1))
    (tmp_path / "payloads" / ref[:2] / ref).write_bytes(b"%PDF-tampered")  # adapt to FilesystemPayloadStore's real layout
    transform = _transform(store, _StubRenderer(DocumentRefusal(kind=DocumentRefusalKind.MALFORMED, detail="never")))
    with pytest.raises(IntegrityError):
        transform.process(make_pipeline_row({"blob_ref": ref}), make_context())


def test_real_renderer_end_to_end(store: FilesystemPayloadStore) -> None:
    ref = store.store(minimal_pdf(2))
    transform = PDFRasterize({"schema": {"mode": "observed"}, "dpi": 72})
    transform.on_start(make_context(payload_store=store))
    try:
        result = transform.process(make_pipeline_row({"blob_ref": ref}), make_context())
    finally:
        transform.close()
    assert result.status == "success"
    assert [row["page_number"] for row in result.rows] == [1, 2]
    assert store.retrieve(result.rows[0]["page_blob_ref"])[:8] == b"\x89PNG\r\n\x1a\n"


class TestConfig:
    def test_input_field_may_not_name_an_emitted_field(self) -> None:
        with pytest.raises(Exception, match="page_blob_ref"):
            PDFRasterize({"schema": {"mode": "observed"}, "blob_ref_field": "page_blob_ref"})

    def test_emitted_field_names_must_be_distinct(self) -> None:
        with pytest.raises(ValueError, match="distinct"):
            PDFRasterize({"schema": {"mode": "observed"}, "page_number_field": "document_id"})

    @pytest.mark.parametrize(("option", "value"), [("dpi", 301), ("dpi", 35), ("max_page_bytes", 5 * 1024 * 1024 + 1), ("max_pages", 2001), ("on_page_failure", "ignore")])
    def test_ceilings_are_hard(self, option: str, value: Any) -> None:
        with pytest.raises(ValueError):
            PDFRasterize({"schema": {"mode": "observed"}, option: value})

    def test_declares_created_fields_and_probe(self) -> None:
        transform = PDFRasterize(PDFRasterize.probe_config())
        assert transform.declared_output_fields == frozenset(
            {"page_blob_ref", "page_number", "document_id", "page_mime_type", "page_size_bytes", "page_width_px", "page_height_px"}
        )
        assert PDFRasterize.creates_tokens is True and PDFRasterize.passes_through_input is True


def test_registers_via_builtin_discovery() -> None:
    manager = PluginManager()
    manager.register_builtin_plugins()
    assert manager.get_transform_by_name("pdf_rasterize").name == "pdf_rasterize"
```

Adapt `make_context(...)` calls to the real factory signatures in `tests/fixtures/factories.py:120-152` (there may be separate lifecycle/transform context factories; `blob_csv_expand`'s tests set `_payload_store` directly — prefer exercising `on_start` as `test_blob_rows.py:43-75` does). Adapt the tampering path in `test_payload_store_integrity_error_propagates` to `FilesystemPayloadStore`'s real on-disk layout (read `core/payload_store.py:210-262`).

- [ ] **Step 3: Run — expect ImportError.**

- [ ] **Step 4: Implement `pdf_rasterize.py`**. Skeleton (fill every method; the shape mirrors `blob_csv_expand.py` and `textract_inline_analysis.py:452-508`):

```python
"""Rasterize a payload-store PDF into one PNG page row per page (one expand group)."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.binary_documents import BINARY_DOCUMENT_MAX_BYTES, binary_document_signature_matches
from elspeth.contracts.contexts import LifecycleContext, TransformContext
from elspeth.contracts.contract_propagation import narrow_contract_to_output
from elspeth.contracts.errors import FrameworkBugError, TransformErrorReason
from elspeth.contracts.payload_store import IntegrityError, PayloadNotFoundError
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.schema import FieldDefinition, SchemaConfig
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.plugins.infrastructure.rasterize.protocol import (
    DocumentRefusal, DocumentRefusalKind, PageRefusalKind, RasterizeResponse, RefusedPage,
)
from elspeth.plugins.infrastructure.rasterize.renderer import PoolRenderer, RenderLimits, RenderResult, RenderTimedOut
from elspeth.plugins.infrastructure.results import TransformResult
from elspeth.plugins.infrastructure.schema_factory import create_schema_from_config

DEFAULT_DPI = 150
MIN_DPI = 36
MAX_DPI = 300
DEFAULT_MAX_INPUT_BYTES = 50 * 1024 * 1024
HARD_MAX_INPUT_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_PAGES = 200
HARD_MAX_PAGES = 2_000
DEFAULT_MAX_PAGE_PIXELS = 25_000_000
HARD_MAX_PAGE_PIXELS = 50_000_000
DEFAULT_RENDER_TIMEOUT_SECONDS = 120
HARD_MAX_RENDER_TIMEOUT_SECONDS = 900
DEFAULT_WORKER_MEMORY_LIMIT_BYTES = 2 * 1024**3
HARD_MAX_WORKER_MEMORY_LIMIT_BYTES = 8 * 1024**3
PAGE_MIME_TYPE = "image/png"
_PAYLOAD_REF_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_INVARIANT_PROBE_BLOB_REF = "0" * 64
_INVARIANT_PROBE_PNG = b"\x89PNG\r\n\x1a\n" + b"pdf-rasterize-invariant-probe"

_SIZE_REFUSALS = frozenset({PageRefusalKind.OVERSIZE_PIXELS, PageRefusalKind.OVERSIZE_BYTES})


class PDFRasterizeConfig(TransformDataConfig):
    """Configuration for pdf_rasterize."""

    blob_ref_field: str = Field(default="blob_ref", min_length=1, max_length=256, title="PDF reference field", description="Input row field containing the payload-store SHA-256 content hash of the PDF bytes.")
    page_blob_ref_field: str = Field(default="page_blob_ref", min_length=1, max_length=256, title="Page image reference field", description="Output field receiving the payload-store content hash of each rendered PNG page.")
    page_number_field: str = Field(default="page_number", ..., description="Output field receiving the 1-based page number.")
    document_id_field: str = Field(default="document_id", ..., description="Output field receiving the source PDF's payload-store content hash, identical on every page row.")
    page_mime_type_field: str = Field(default="page_mime_type", ..., description="Output field receiving the page image MIME type (always image/png).")
    page_size_bytes_field: str = Field(default="page_size_bytes", ..., description="Output field receiving the encoded PNG byte length.")
    page_width_field: str = Field(default="page_width_px", ..., description="Output field receiving the rendered page width in pixels.")
    page_height_field: str = Field(default="page_height_px", ..., description="Output field receiving the rendered page height in pixels.")
    dpi: int = Field(default=DEFAULT_DPI, ge=MIN_DPI, le=MAX_DPI, title="Render DPI", description="Raster resolution; 150 keeps a Letter/A4 page comfortably under the 5 MiB per-page bound.")
    max_input_bytes: int = Field(default=DEFAULT_MAX_INPUT_BYTES, gt=0, le=HARD_MAX_INPUT_BYTES, ...)
    max_pages: int = Field(default=DEFAULT_MAX_PAGES, gt=0, le=HARD_MAX_PAGES, ...)
    max_page_pixels: int = Field(default=DEFAULT_MAX_PAGE_PIXELS, gt=0, le=HARD_MAX_PAGE_PIXELS, ..., description="Refuse a page whose declared size at the configured dpi exceeds this many pixels, before any bitmap is allocated.")
    max_page_bytes: int = Field(default=BINARY_DOCUMENT_MAX_BYTES, gt=0, le=BINARY_DOCUMENT_MAX_BYTES, ..., description="Maximum encoded PNG bytes per page; may be reduced but never raised above the 5 MiB downstream provider bound.")
    render_timeout_seconds: int = Field(default=DEFAULT_RENDER_TIMEOUT_SECONDS, gt=0, le=HARD_MAX_RENDER_TIMEOUT_SECONDS, ..., description="Wall-clock and CPU budget for rendering one document in the worker subprocess.")
    worker_memory_limit_bytes: int = Field(default=DEFAULT_WORKER_MEMORY_LIMIT_BYTES, gt=0, le=HARD_MAX_WORKER_MEMORY_LIMIT_BYTES, ..., description="RLIMIT_AS applied to the render worker subprocess.")
    on_page_failure: Literal["fail_document", "emit_rendered"] = Field(default="fail_document", title="Page failure policy", description="fail_document: any refused page fails the whole row (typed error routed via on_error). emit_rendered: emit the pages that rendered and record the refused page numbers in the success metadata; zero survivors is still a row error.")

    @model_validator(mode="after")
    def _reject_field_name_collisions(self) -> PDFRasterizeConfig:
        emitted = (self.page_blob_ref_field, self.page_number_field, self.document_id_field, self.page_mime_type_field,
                   self.page_size_bytes_field, self.page_width_field, self.page_height_field)
        for name in (self.blob_ref_field, *emitted):
            if not name.strip() or not name.isidentifier():
                raise ValueError(f"pdf_rasterize field names must be non-empty identifiers, got {name!r}")
        if len(set(emitted)) != len(emitted):
            raise ValueError("pdf_rasterize emitted field names must be distinct")
        if self.blob_ref_field in emitted:
            raise ValueError(f"blob_ref_field {self.blob_ref_field!r} may not name a field pdf_rasterize creates")
        return self

    @property
    def declared_input_fields(self) -> frozenset[str]:
        return super().declared_input_fields | frozenset({self.blob_ref_field})
```

Every `Field` MUST carry `title=` and `description=` (options-metadata gate, appendix 02 #49). Output-schema builder: copy `_build_blob_csv_output_schema_config` (`blob_csv_expand.py:125-151`) with added fields `page_blob_ref_field: str`, `page_number_field: int`, `document_id_field: str`, `page_mime_type_field: str`, `page_size_bytes_field: int`, `page_width_field: int`, `page_height_field: int`, all `required=True`.

Class body:

```python
class PDFRasterize(BaseTransform):
    """Render each page of a PDF into a PNG payload and emit one row per page."""

    output_naming_config_keys = frozenset({"page_blob_ref_field", "page_number_field", "document_id_field", "page_mime_type_field",
                                           "page_size_bytes_field", "page_width_field", "page_height_field"})
    name = "pdf_rasterize"
    determinism = Determinism.IO_READ
    plugin_version = "1.0.0"
    source_file_hash: str | None = "sha256:0000000000000000"  # recomputed as the LAST step of this task
    config_model = PDFRasterizeConfig
    usage_when_to_use: str = (
        "Use when each row carries a payload-store content hash for a PDF (from the blob_rows source or blob_fetch) "
        "and you need one row per page carrying a rendered PNG image — typically feeding aws_textract_inline_analysis "
        "with document_format png and blob_ref_field page_blob_ref so a multipage PDF becomes N synchronous single-page calls."
    )
    usage_when_not_to_use: str = (
        "Not a text extractor and not for images or non-PDF payloads: pages are rendered as pixels only. Documents "
        "already staged in S3 belong in aws_textract_document_analysis, which handles multipage PDFs without rasterizing."
    )
    example_use: str = """transform:
  plugin: pdf_rasterize
  options:
    blob_ref_field: blob_ref
    dpi: 150
    max_pages: 200
    on_page_failure: fail_document
    schema:
      mode: observed
"""
    capability_tags: tuple[str, ...] = ("pdf", "rasterize", "image", "blob", "fan-out")
    creates_tokens = True
    passes_through_input = True

    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        return {"schema": {"mode": "observed"}, "blob_ref_field": "blob_ref"}

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        cfg = PDFRasterizeConfig.from_dict(options, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        ... store every option on self ...
        self._limits = RenderLimits(dpi=cfg.dpi, max_pages=cfg.max_pages, max_page_pixels=cfg.max_page_pixels,
                                    max_page_bytes=cfg.max_page_bytes, render_timeout_seconds=cfg.render_timeout_seconds,
                                    worker_memory_limit_bytes=cfg.worker_memory_limit_bytes)
        self._renderer = PoolRenderer(self._limits)  # pool is created lazily on first render
        self.declared_output_fields = frozenset(field.name for field in _pdf_rasterize_added_output_fields(cfg))
        self.input_schema = create_schema_from_config(cfg.schema_config, "PDFRasterizeInput", allow_coercion=False)
        self._output_schema_config = _build_pdf_rasterize_output_schema_config(cfg.schema_config, cfg)
        self.output_schema = create_schema_from_config(self._output_schema_config, "PDFRasterizeOutput", allow_coercion=False)
        self._reject_input_options_naming_created_fields({"blob_ref_field": cfg.blob_ref_field})  # LAST, after declared_output_fields
```

`on_start`: `blob_csv_expand.py:269-273` shape. `close`: `self._renderer.close()` then `super().close()` (check `BaseTransform.close` signature). `get_agent_assistance` hints (each ≤ 280 chars):

1. "Place pdf_rasterize after blob_rows or blob_fetch; the default blob_ref_field matches their blob_ref output."
2. "Downstream aws_textract_inline_analysis must set blob_ref_field: page_blob_ref and document_format: png — the page image is a new field, the PDF's blob_ref is preserved unchanged."
3. "Keep max_page_bytes at or below the downstream max_document_bytes (5 MiB ceiling); dpi 150 fits Letter/A4, raise dpi only with max_page_pixels headroom."
4. "on_page_failure: fail_document quarantines the whole PDF row on any refused page; emit_rendered emits the surviving pages and records the refused page numbers in the run audit."
5. "Every page row carries document_id (the PDF's payload hash) and a 1-based page_number for grouping and ordering downstream."

`process(row, ctx)` order: field present → `type(value) is not str` → `raise TypeError(...)` (Tier-2 type violation, `blob_csv_expand.py:280-284` precedent) → `_PAYLOAD_REF_PATTERN.fullmatch` else `invalid_input`/`invalid_blob_ref` → `retrieve` (`PayloadNotFoundError` → `blob_not_found`; `IntegrityError` re-raise) → empty → `invalid_input`/`empty_document` → `len > max_input_bytes` → `blob_too_large` → `binary_document_signature_matches("pdf", body)` else `invalid_input`/`document_signature_mismatch` → `result, output_dir = self._renderer.render(body)` inside `try/finally: self._renderer.discard(output_dir)` → `_map_document_result(...)`.

Mapping:
- `DocumentRefusal`: ENCRYPTED → `pdf_encrypted`; MALFORMED → `pdf_malformed`; TOO_MANY_PAGES → `too_many_rows` with `page_count`, `max_pages`.
- `RenderTimedOut` → `render_timeout` with `max_seconds`.
- `RasterizeResponse`: if `refused` and policy `fail_document` → `pdf_page_too_large` when every refusal kind ∈ `_SIZE_REFUSALS` else `pdf_page_render_failed`, with `refused_pages: [{"page_number","kind","detail"}...]`, `page_count`. If `rendered == ()` → the same error regardless of policy. Otherwise store each PNG (`self._payload_store.store(page.png_path.read_bytes())`) and build rows:

```python
base = row.to_dict()
output_rows = []
for page in response.rendered:
    output = copy.deepcopy(base)
    output[self._page_blob_ref_field] = page_ref
    output[self._page_number_field] = page.page_number
    output[self._document_id_field] = blob_ref
    output[self._page_mime_type_field] = PAGE_MIME_TYPE
    output[self._page_size_bytes_field] = page.size_bytes
    output[self._page_width_field] = page.width_px
    output[self._page_height_field] = page.height_px
    output_rows.append(output)
```

then the contract triple and `success_multi(..., success_reason={"action": "expanded_blob", "fields_added": sorted(self.declared_output_fields), "metadata": {"blob_ref": blob_ref, "page_count": response.page_count, "rendered_pages": len(output_rows), "refused_pages": [...], "on_page_failure": self._on_page_failure}})`.

Every error reason dict includes `"field": self._blob_ref_field` and `"blob_ref": blob_ref` where known.

Invariant probe: `forward_invariant_probe_rows` as `blob_csv_expand.py:233-241`; `execute_forward_invariant_probe` swaps BOTH `self.__dict__["_payload_store"]` (a store whose `retrieve(_INVARIANT_PROBE_BLOB_REF)` returns `minimal_pdf(1)`-equivalent bytes — embed a minimal one-page PDF as a module constant `_INVARIANT_PROBE_PDF` built by a tiny module-level helper identical to `tests/fixtures/pdf_documents.minimal_pdf(1)`; do NOT import from tests) and `self.__dict__["_renderer"]` (a `_InvariantRenderer` whose `render` writes `_INVARIANT_PROBE_PNG` to a temp dir and returns a one-page `RasterizeResponse`; its `discard` removes the dir; `store` must accept the PNG — use an in-memory store for the probe). Restore both in `finally` with the `had_*`/`delattr` dance.

- [ ] **Step 5: Run the plugin tests — expect PASS.** Then run the invariant sweeps that touch every transform:

`... -m pytest tests/invariants tests/unit/plugins/test_discovery.py tests/unit/plugins/test_catalog_reference_content.py tests/unit/plugins/test_validation_path_agreement.py -q -n 8`

Expected: the COUNT pins fail (33→34 etc. — Task 4's job); every OTHER test passes. If `test_input_schema_config_is_captured.py` / `test_transform_input_contract_is_satisfiable.py` report `pdf_rasterize` as an unexpected rejection, that is the `@model_validator` — add it to both `_EXPECTED_*` sets AND a `_TRANSFORM_REJECTION_CASES` entry in `test_validation_path_agreement.py` (appendix 02 #30/#31/#34) NOW, in this task.

- [ ] **Step 6: Format, then hash LAST**

```bash
/home/john/elspeth/.venv/bin/ruff format src/elspeth/plugins/transforms/pdf_rasterize.py tests/unit/plugins/transforms/test_pdf_rasterize.py src/elspeth/contracts/errors.py
/home/john/elspeth/.venv/bin/ruff check <same>
PYTHONPATH=/home/john/elspeth/.claude/worktrees/pdf-rasterize/src /home/john/elspeth/.venv/bin/python - <<'PY'
from pathlib import Path
from scripts.cicd.plugin_hash import compute_source_file_hash, fix_source_file_hash
path = Path("src/elspeth/plugins/transforms/pdf_rasterize.py")
fix_source_file_hash(path, "PDFRasterize", compute_source_file_hash(path))
print(compute_source_file_hash(path))
PY
grep -n 'source_file_hash: str | None = "sha256:' src/elspeth/plugins/transforms/pdf_rasterize.py
```

(`scripts` may need `PYTHONPATH=...:/home/john/elspeth/.claude/worktrees/pdf-rasterize` — the canonical heredoc is at `docs/contracts/plugin-catalogue-reference-content.md:141-152`.) Expected: the printed hash equals the line in the file (strict equality, not substring).

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/contracts/errors.py src/elspeth/plugins/transforms/pdf_rasterize.py tests/unit/plugins/transforms/test_pdf_rasterize.py tests/invariants/test_input_schema_config_is_captured.py tests/invariants/test_transform_input_contract_is_satisfiable.py tests/unit/plugins/test_validation_path_agreement.py
git commit -m "feat(plugins): pdf_rasterize — one PNG page row per PDF page as an expand group"
```

---

### Task 4: Whole-tree pin sweep

**Files:** every item in `02-pin-inventory.md` §1–§3, §7, §8 #44 (all exact paths/lines there). This task is mechanical but ORDER-SENSITIVE; follow appendix §12.

- [ ] **Step 1: Count pins (§1 #1–#8, #11)** — edit the literals: `test_discovery.py:265,300,323`; `test_catalog_reference_content.py:34-86 (+identity), :213, :214-218, :256`; `tests/unit/web/catalog/test_service.py:60`; `boundary_expectations.py:150-183` (+ `"pdf_rasterize": Determinism.IO_READ`, alphabetical). Run the four files. Expected: only the knob-schema golden still fails.

- [ ] **Step 2: Knob-schema golden (#9)** — generate `tests/golden/web/catalog/knob_schema/transform__pdf_rasterize.json` with the test's own expression:

```bash
PYTHONPATH=... /home/john/elspeth/.venv/bin/python - <<'PY'
import json
from pathlib import Path
from elspeth.web.catalog.service import CatalogServiceImpl
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager  # adapt: read test_knob_schema_golden.py for the exact imports/helper
service = CatalogServiceImpl(get_shared_plugin_manager())
schema = service._schema_cache[("transform", "pdf_rasterize")].knob_schema
payload = {"plugin_kind": "transform", "plugin_name": "pdf_rasterize", "knob_schema": schema}
Path("tests/golden/web/catalog/knob_schema/transform__pdf_rasterize.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
PY
```

Read `tests/unit/web/catalog/test_knob_schema_golden.py` first and use ITS helper (`_stable_json` or equivalent) rather than the sketch above if they differ. Run the golden test. Expected: PASS.

- [ ] **Step 3: State-engine PB-09 trio (§2 #13–#29)** in this order:
  1. Edit `scripts/state_engine_plugin_matrix.py:43,44,160,293,602`.
  2. `PYTHONPATH=... /home/john/elspeth/.venv/bin/python scripts/state_engine_plugin_matrix.py render-skeleton tests/golden/state_engine/plugin_lifecycle_matrix.json` — exits 1 by design; open the new `transform:pdf_rasterize` entry and set: `variants: ["default"]`, `external_observation_required: false`, `applicable_pb_boundaries: ["PB-02", "PB-09"]`, `local_fixture: "hermetic"`, `release_lane: "local"` (copy `blob_csv_expand`'s entry at `:1105-1132` as the model). Re-run until exit 0.
  3. `tests/integration/plugins/test_state_engine_plugin_lifecycle_matrix.py:141` add `"transform:pdf_rasterize": [{"blob_ref": "filled-by-harness"}]`; `:211-212` add `if case.plugin_key == "transform:pdf_rasterize": rows[0]["blob_ref"] = store.store(minimal_pdf(1))` (import from `tests.fixtures.pdf_documents`); `:391` → 35. NOTE: the lifecycle harness will spawn the real render worker for this plugin — acceptable (one document, one page, 72 dpi is not configurable there; default 150 dpi on a 200×100 pt page is 417×209 px).
  4. v3 `catalog.json:140` transforms list + PB-09 `required_cases` (+1 dict entry mirroring `blob_csv_expand`'s); v3 `evidence_selectors.json` +5 node_ids (exact strings in appendix §2); v2 `catalog.json` transforms list + flat `required_cases` string.
  5. Rotate `V2_CATALOG_SHA256` at `test_state_engine_catalog_contract.py:35` (`sha256sum docs/architecture/state_engine/proof-catalog/v2/catalog.json`); `:167` → 52. `test_state_engine_plugin_matrix.py:38,46,73,79`.
  6. Run: `... python scripts/state_engine_plugin_matrix.py check tests/golden/state_engine/plugin_lifecycle_matrix.json` then the validate-catalog / validate-selectors commands (find their invocation in `.github/workflows/ci.yaml` around `:684` and in `scripts/state_engine_assessment_lib/selectors.py:477-497`) — validate-selectors LAST. Then `pytest tests/unit/architecture tests/unit/plugins/test_state_engine_plugin_matrix.py tests/integration/plugins/test_state_engine_plugin_lifecycle_matrix.py -q -n 8`. Expected: all pass.

- [ ] **Step 4: Allowlists (§3 #32/#33)** — `config/cicd/contracts-whitelist.yaml`: add the `probe_config:return` and `__init__:options` entries next to the blob_csv_expand ones. Run: `PYTHONPATH=... python scripts/check_contracts.py` (find the exact invocation in `.pre-commit-config.yaml:214-224`). Expected: clean. Then `filigree` ticket for the paired cleanup (house rule): create `elspeth` issue "Lift contracts-whitelist entries for pdf_rasterize when `dict[str, Any]` plugin option contracts are typed" with label `whitelist-cleanup`, referencing the two entries — use `mcp__filigree__issue_create` or `filigree create` from the MAIN checkout path (the CLI fails inside worktrees); if neither is available, write the ticket text into the commit body and say so.

- [ ] **Step 5: Silent sites (§7)** — add `"pdf"` to `_ACRONYMS` in `src/elspeth/web/composer/guided/_display.py:29-46` AND `ACRONYMS` in `src/elspeth/web/frontend/src/components/catalog/pluginDisplayName.ts:23-38` (both, alphabetical). Add `pdf_rasterize` to `EXPECTED_CORE_TAGS` and `_REQUIRED_GUIDANCE` in `tests/unit/plugins/transforms/test_core_catalogue_metadata.py` (tags tuple from Task 3; guidance substrings `("pdf", "png", "page", "aws_textract_inline_analysis")` for to-use and `("text extractor", "s3")` for avoid — check they match the final prose). Add `import pypdfium2` to the image import smoke in `.github/workflows/build-push.yaml:139-152` (#44).

- [ ] **Step 6: Digest budget (§9)** — measure:

```bash
PYTHONPATH=... /home/john/elspeth/.venv/bin/python - <<'PY'
# adapt imports by reading planner_authoring_aids.py:1564 discovery_digest and its catalog argument
from elspeth.web.composer.planner_authoring_aids import discovery_digest
...
print(digest["budget"]["canonical_bytes_used"], "/ 24576")
PY
```

Record the number in the commit body. Expected: < 24,576 and roughly ≤ 21,000 (one plugin ≈ +500 B on the spec's 20,386 baseline). If it is not, shorten the plugin's `usage_*` prose, re-hash (Task 3 Step 6), and re-measure.

- [ ] **Step 7: Run the whole unit tree for plugins/web/catalog/architecture**

`... -m pytest tests/unit/plugins tests/unit/web tests/unit/architecture tests/unit/contracts tests/invariants tests/unit/cicd -q -n 16 2>&1 | tail -15`
Expected: `N passed` with **zero failures**. Count failures (`grep -c FAILED`), never tail them.

- [ ] **Step 8: Commit**

```bash
git add tests/unit/plugins/test_discovery.py tests/unit/plugins/test_catalog_reference_content.py tests/unit/web/catalog/test_service.py src/elspeth/web/audit_readiness/boundary_expectations.py tests/golden/web/catalog/knob_schema/transform__pdf_rasterize.json scripts/state_engine_plugin_matrix.py tests/golden/state_engine/plugin_lifecycle_matrix.json tests/integration/plugins/test_state_engine_plugin_lifecycle_matrix.py docs/architecture/state_engine/proof-catalog/v3/catalog.json docs/architecture/state_engine/proof-catalog/v3/evidence_selectors.json docs/architecture/state_engine/proof-catalog/v2/catalog.json tests/unit/architecture/test_state_engine_catalog_contract.py tests/unit/plugins/test_state_engine_plugin_matrix.py config/cicd/contracts-whitelist.yaml src/elspeth/web/composer/guided/_display.py src/elspeth/web/frontend/src/components/catalog/pluginDisplayName.ts tests/unit/plugins/transforms/test_core_catalogue_metadata.py .github/workflows/build-push.yaml
git commit -m "test(plugins): whole-tree pins for pdf_rasterize — counts, knob golden, PB-09 trio, whitelist, acronyms"
```

---

### Task 5: Textract / blob_rows / composer prose, hashes, docs

**Files:**
- Modify: `src/elspeth/plugins/transforms/aws/textract_inline_analysis.py:266-275` (usage strings), `:723-732` (hints), `:258` (hash); `src/elspeth/plugins/transforms/aws/textract_document_analysis.py:324-328`, `:1086`, `:311` (hash); `src/elspeth/plugins/sources/blob_rows.py:7`, `:171`, `:124` (hash); `src/elspeth/web/composer/tools/sources.py:1034-1040`
- Modify: `tests/unit/plugins/transforms/aws/test_textract_inline_analysis.py:812-823` (convert to a claim test), `:646-656` (provenance comment)
- Modify: `examples/textract_inline/README.md:3-13,41-46`, `examples/textract_inline/input/README.md`, `examples/AGENTS.md:189`, `tests/e2e/examples/test_shipped_examples.py:72-74` (comment path fix)
- Modify: `docs/reference/configuration.md:917`, `docs/guides/user-manual.md:188`, `docs/guides/docker.md:364`, `docs/agents/recent-code-hints.md` (`:433` count; §6 additions; a NEW dated entry for the rasterize seam conventions)

- [ ] **Step 1: Failing claim test** — replace `test_assistance_names_the_authority_boundaries` body at `test_textract_inline_analysis.py:812-823` with a test that keeps the five pinned substrings AND asserts the new claim:

```python
def test_assistance_names_the_authority_boundaries_and_the_multipage_on_ramp() -> None:
    assistance = AWSTextractInlineAnalysis.get_agent_assistance()
    assert assistance is not None
    hints = " ".join(assistance.composer_hints)
    for required in ("blob_rows", "jpeg", "single-page", "aws_textract_document_analysis", "billable"):
        assert required in hints
    # A multipage PDF reaches this plugin only through pdf_rasterize; the hint must say so
    # AND name the two options the downstream node must set (a declaration test pins
    # existence, a claim test pins the advice).
    on_ramp = [hint for hint in assistance.composer_hints if "pdf_rasterize" in hint]
    assert len(on_ramp) == 1
    assert "blob_ref_field: page_blob_ref" in on_ramp[0] and "document_format: png" in on_ramp[0]
    assert AWSTextractInlineAnalysis.get_agent_assistance(issue_code="anything") is None
```

Run it. Expected: FAIL (`on_ramp` empty).

- [ ] **Step 2: Rewrite the inline plugin prose (additive; every gate substring survives — appendix 04 §4)**

`usage_when_to_use`:
> "Use when each row carries a payload-store content hash (from the blob_rows source, or one rasterized page per row from pdf_rasterize) for a JPEG, PNG, or single-page PDF document up to 5 MiB and you need synchronous Textract OCR, forms, tables, queries, signatures, or layout enrichment. Extracted remote content remains untrusted before LLM consumption."

`usage_when_not_to_use`:
> "Not for multipage or larger documents, or documents already stored in S3 — rasterize a multipage PDF into per-page images with pdf_rasterize first, or use aws_textract_document_analysis. AnalyzeDocument has no idempotency guarantee, so SDK and engine retries can each repeat a billable provider call."

`composer_hints`: keep the six existing strings; replace hint 3 with:
> "PDF support is single-page only; a multipage PDF goes through pdf_rasterize first, and the downstream node then sets blob_ref_field: page_blob_ref and document_format: png. Mixed formats need homogeneous sources or branches with one transform instance per format."

(≤ 280 chars — count it.) Add a provenance comment above `test_multi_page_response_fails_page_count_policy` at `:646`: `# Also the pdf_rasterize case: a rasterized page whose response reports Pages != 1 must still fail — the guard validates the provider RESPONSE and is not relaxed by the exploder.`

- [ ] **Step 3: Sibling prose** — `textract_document_analysis.py:324-328` append "…or rasterize a multipage PDF with pdf_rasterize to stay on the synchronous path." (gate needs `inline bytes`, `synchronous` present — keep them). `:1086` append a hint "Multipage PDFs can alternatively be split with pdf_rasterize and analyzed inline page by page." `blob_rows.py:7` add `pdf_rasterize` to the example consumers; `:171` → "Mixed document formats need homogeneous sources or branches — one explicitly configured consumer per format; a multipage PDF can be split into PNG pages by pdf_rasterize." `web/composer/tools/sources.py:1038-1039` → "...feeding aws_textract_inline_analysis (directly, or via pdf_rasterize for multipage PDFs); mixed formats need one homogeneous source per format."

Run: `... -m pytest tests/unit/plugins/transforms/aws tests/unit/plugins/transforms/test_external_catalogue_metadata.py tests/unit/plugins/test_catalog_reference_content.py tests/unit/plugins/sources/test_blob_rows.py tests/unit/web/composer/test_tool_declarations.py tests/unit/contracts/test_plugin_assistance_coverage.py -q -n 8`. Expected: all pass (uniqueness, substrings, ≤280-char hints).

- [ ] **Step 4: Examples + docs** — rewrite `examples/textract_inline/README.md:11-13` to: "Documents already stored in S3 or over 5 MiB belong to the asynchronous `aws_textract_document_analysis` plugin. A multipage PDF can instead be split by the `pdf_rasterize` transform into one PNG page per row and analyzed inline page by page (set `blob_ref_field: page_blob_ref` and `document_format: png` on the analysis node)." Update `:41-46` and `input/README.md` similarly; fix the comment path at `tests/e2e/examples/test_shipped_examples.py:72-74`; add the row at `examples/AGENTS.md:189`. Add the transform-table row in `docs/reference/configuration.md:917`, the `plugins list` line in `docs/guides/user-manual.md:188`, fix the counts in `docs/guides/docker.md:364` ("9 sources, 34 transforms, 9 sinks") and `docs/agents/recent-code-hints.md:433` (52).

- [ ] **Step 5: `recent-code-hints.md`** — (a) in §6 "New-plugin exact inventories" add the sites appendix 02 found missing: v2 proof catalog + `V2_CATALOG_SHA256`, `test_service.py:60`, the two `tests/invariants` `_EXPECTED_*` allowlists, the Python↔TS acronym mirror, `contracts-whitelist.yaml` constructor/probe entries; (b) a NEW dated `2026-08-25 — pdf_rasterize / out-of-process render seam` entry with: the worker-only native import rule (`pypdfium2` initialises libpdfium at import); `setrlimit` is now used in `plugins/infrastructure/rasterize/worker.py` (first use in the tree; `RLIMIT_AS` catchable as `MemoryError` only with pypdfium2's default `new_native` bitmap maker; bare `RLIMIT_CPU` poisons a `ProcessPoolExecutor` — the `SIGXCPU` handler is load-bearing); the orphan-kill sequence; `max_page_bytes` ≤ downstream `max_document_bytes` has no cross-node validation (open ticket); the trigger-downstream-of-an-exploder builder gap (spec §4 Medium) is pre-existing and widened.

- [ ] **Step 6: Hashes LAST** — `ruff format` the three plugin files, then recompute and paste for `AWSTextractInlineAnalysis` (`textract_inline_analysis.py`), `AWSTextractDocumentAnalysis` (`textract_document_analysis.py` — confirm the class name at `:311` area), and `BlobRowsSource` (`blob_rows.py` — confirm class name at `:124`) with the Task 3 Step 6 heredoc, changing path/class each time. Verify each with strict equality:

```bash
PYTHONPATH=... /home/john/elspeth/.venv/bin/python - <<'PY'
from pathlib import Path
import re
from scripts.cicd.plugin_hash import compute_source_file_hash
for p in ["src/elspeth/plugins/transforms/aws/textract_inline_analysis.py", "src/elspeth/plugins/transforms/aws/textract_document_analysis.py", "src/elspeth/plugins/sources/blob_rows.py", "src/elspeth/plugins/transforms/pdf_rasterize.py"]:
    text = Path(p).read_text()
    declared = re.search(r'source_file_hash: str \| None = "(sha256:[0-9a-f]{16})"', text).group(1)
    print(p, declared == compute_source_file_hash(Path(p)))
PY
```

Expected: four `True`.

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/plugins/transforms/aws/textract_inline_analysis.py src/elspeth/plugins/transforms/aws/textract_document_analysis.py src/elspeth/plugins/sources/blob_rows.py src/elspeth/web/composer/tools/sources.py tests/unit/plugins/transforms/aws/test_textract_inline_analysis.py examples/textract_inline/README.md examples/textract_inline/input/README.md examples/AGENTS.md tests/e2e/examples/test_shipped_examples.py docs/reference/configuration.md docs/guides/user-manual.md docs/guides/docker.md docs/agents/recent-code-hints.md
git commit -m "docs(plugins): textract/blob_rows/composer prose names pdf_rasterize as the multipage on-ramp; hints doc entry"
```

---

### Task 6: Integration test, gates, full suite

**Files:**
- Create: `tests/integration/pipeline/test_pdf_rasterize_pipeline.py`

- [ ] **Step 1: Failing integration test** — model on `tests/integration/pipeline/test_deaggregation.py:193-317` (programmatic production path). Pipeline: a `csv` source with columns `doc_name,blob_ref` (two good PDFs of 2 and 3 pages, one malformed payload, one encrypted) → `pdf_rasterize` (`on_error: error_sink`, `dpi: 72`) → a `json` sink `pages` + an error sink `errors` (find the exact `on_error` + error-sink YAML shape in `test_deaggregation.py` or `examples/`). Stage the payloads into a `FilesystemPayloadStore(tmp_path / "payloads")` before the run. Assert:
  1. run completes (status not an abort; check the exact run-status vocabulary in `test_deaggregation.py`);
  2. the `pages` sink holds 5 rows; `page_number` sequences `[1,2]` and `[1,2,3]` grouped by `document_id`; each `page_blob_ref` retrieves PNG bytes;
  3. the `errors` sink holds 2 rows whose error reasons are `pdf_malformed` and `pdf_encrypted` — the DATA-INTEGRITY outcome the user asked for;
  4. Landscape: for the good documents, exactly 5 expanded tokens each with one `token_parents` row; `expand_group_id` derived via `elspeth.contracts.identity.path_expand_group_id` from `lineage_path` is non-None for the 5 page tokens and groups them 2+3; the two bad-document tokens carry a terminal FAILURE outcome routed to `errors` (assert with `TerminalOutcome.FAILURE` / the routed path enum — never string literals);
  5. a second scenario with `on_page_failure: emit_rendered` and a scripted renderer is NOT needed here (unit-tested); skip.

Run it. Expected: FAIL until the YAML/plumbing is right; iterate until PASS. If the engine on this mid-refactor branch refuses something structural (e.g. a collector/scope registration error, an `OrchestrationInvariantError` unrelated to this plugin), STOP, capture the full traceback into the task report, and mark the step blocked — do not modify engine code.

- [ ] **Step 2: Gates**
  - `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only` → expect exit 1 with **6 active ERROR** (baseline; none in `plugins/`) and ≥129 boundaries. Record the counts.
  - `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth > /tmp/claude-1000/-home-john-elspeth/43b59023-9440-4d72-b32c-a0dec852905c/scratchpad/lints-after.txt`; produce the same for a `git archive a371e13d0` export of the base (appendix: memory says the baseline needs a FULL tree export) and diff the **per-rule counts**. Expected: no new findings attributable to `pdf_rasterize.py` or `rasterize/`. If R1/R5/R6 fire on the worker's pypdfium2 attribute reads, fix by constructing an owned dataclass at the seam (ADR-032) — never by adding a suppression.
  - `PYTHONPATH=... python -m mypy src/elspeth/plugins/transforms/pdf_rasterize.py src/elspeth/plugins/infrastructure/rasterize` clean.
  - masquerade: `... -m pytest tests/unit/elspeth_lints/test_masquerade_gate.py -q -n 0` passes (no new subjects).

- [ ] **Step 3: Full suite** — `git rev-parse HEAD` before; `PYTHONPATH=... /home/john/elspeth/.venv/bin/python -m pytest tests/ -q -n 24 -p no:cacheprovider 2>&1 | tail -5`; `git rev-parse HEAD` after (must match). Record `N passed / M failed`. Expected: only pre-existing failures already present on `a371e13d0` (the ticketed xdist event-loop flake in `tests/unit/web/execution/test_service.py`). For every failure: `grep FAILED` → count → attribute each to this branch or the base by re-running that node on a `git worktree` of the base ONLY if cheap; otherwise list it as unattributed. Do not claim green without the `N passed` line.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/pipeline/test_pdf_rasterize_pipeline.py
git commit -m "test(pipeline): pdf_rasterize expand group + bad-document quarantine end to end"
```

- [ ] **Step 5: Report** — the final message must list: commits on the branch; the full-suite line; wardline/lints/mypy results; the digest byte count; the list of things explicitly NOT done (no `examples/` variant, no stitcher, no Unit-1 engine work, no cross-node `max_page_bytes` validation — with ticket ids if created).

---

## Self-review notes

- Spec coverage (Unit 2 only): five caps ✔ (`max_input_bytes`, `max_pages`, `max_page_pixels`, `max_page_bytes`, `render_timeout_seconds`) + memory limit; out-of-process rendering with rlimits ✔; no module-scope native import ✔; base dependency ✔; whole-tree pin inventory ✔ (Task 4); `document_id`/page number as grouping keys, no `page_count` stamping for gap arithmetic ✔ (page_count appears only in `success_reason.metadata`); `pdf_rasterize` not in the untrusted-producer set ✔ (decision recorded in appendix 04 §7 and the hints entry); Textract prose + `Pages != 1` guard untouched ✔ (Task 5); no `policy_capabilities` ✔; no composer recipe / server-side path ✔ (nothing touches a composer tool handler; the `sources.py` edit is a description string, verified unpinned).
- Out of scope, by design: Unit 1 (expand-group completeness), Unit 3 (stitcher), Unit 5 (example dir), the exploder-trigger builder guard.
- Type consistency: `RasterizeOutcome` (worker) vs `RenderResult` (renderer adds `RenderTimedOut`) — both defined in Task 1/2 Interfaces and consumed by name in Task 3.
