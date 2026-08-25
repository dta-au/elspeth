# Research lane 3 — out-of-process render seam, pypdfium2 facts, packaging

Read-only sweep at `a371e13d0` plus empirical probes against the pypdfium2 5.13.0
wheel (run from an extracted wheel via PYTHONPATH; nothing installed by the lane).

## 1. Process-isolation precedent — `plugins/transforms/rag/query.py`

Rationale `query.py:48-51`: threads cannot interrupt C extension code; process
isolation is the only reliable timeout. Pool `query.py:75-79`:
`ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))`.

Worker reference `query.py:142`: `pool.submit(run_regex_worker, ...)` — pickled BY
REFERENCE (module + qualname), so it lives at module top level in an importable module
(`src/elspeth/core/regex_worker.py`, returns a NamedTuple of neutral data).

Timeout + orphan kill `query.py:143-167` (load-bearing; without it the worker burns CPU
and the interpreter hangs at exit):

```python
try:
    match_result = future.result(timeout=self._regex_timeout)
except FuturesTimeoutError:
    future.cancel()
    stuck_processes = list(self._regex_pool._processes.values())
    self._regex_pool.shutdown(wait=False, cancel_futures=True)
    for proc in stuck_processes:
        if proc.is_alive():
            proc.kill()
    self._regex_pool = ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn"))
    return QueryResult(error=TransformErrorReason(reason="regex_timeout", ...))
```

Error tiers `query.py:160-186`: timeout → typed row error; `TypeError` → re-raised as
Tier-2 contract violation; any other worker exception → `RuntimeError` crash ("worker
is system-owned code — a crash is a code bug").

Lifecycle: `close()` (`query.py:221-225`) called from the transform's `close()`; engine
calls it in `engine/orchestrator/cleanup.py:208-211` inside a finally (contract
`base.py:1315-1325`).

Tests: `tests/unit/core/test_regex_worker.py:52-62` (real pool round trip);
`tests/unit/plugins/transforms/rag/test_query.py:195-249` injects a pre-failed `Future`
via a `_replace_regex_pool` helper (no subprocess); `:255-300` pool lifecycle + FD-leak
test gated on `/proc`.

Spawn mechanics: no `python -m` anywhere; interpreter is `sys.executable`; child
`sys.path` is COPIED from the parent (`multiprocessing/spawn.py:173-183, 228-229`), so a
worktree run with `<worktree>/src` on `sys.path` propagates correctly. Verify in any A/B
by asserting `elspeth.__file__` inside the worker. `max_tasks_per_child` is unused in
the repo; available on `spawn` (incompatible with `fork`). `initializer=`/`initargs=`
is the hook for `setrlimit`.

## 2. `setrlimit` — absent from `src/`; empirical behaviour through a spawn pool

| case | initializer | payload | parent sees | pool reusable? |
|---|---|---|---|---|
| A | `RLIMIT_CPU (1,2)`, no handler | busy loop | `BrokenProcessPool` | NO |
| B | `RLIMIT_CPU (1,4)` + `SIGXCPU` handler raising | busy loop | the handler's exception, pickled back | YES |
| C | as B | 200× native `page.render(scale=40)` | same | YES |
| D | `RLIMIT_AS (256 MB)` | `render(scale=80)` ≈ 384 MB | `MemoryError` | YES |
| E | none | busy loop, `future.result(timeout=2)` | `FuturesTimeoutError` | NO — orphan burns; hang at exit |

Consequences:
- Bare `RLIMIT_CPU` poisons the pool (SIGXCPU default action kills the worker →
  `BrokenProcessPool`). If used, install a `SIGXCPU` handler in the initializer that
  raises a typed exception; hard limit comfortably above soft (hard delivers SIGKILL).
- A single pdfium render that overruns in C defers the Python handler until it
  returns → the outer wall-clock `future.result(timeout=)` + orphan kill is the backstop.
- `RLIMIT_AS` is catchable as `MemoryError` BECAUSE `render()`'s default
  `bitmap_maker=PdfBitmap.new_native` allocates the pixel buffer with ctypes on the
  Python side (`pypdfium2/_helpers/bitmap.py:142-143`). Keep the default bitmap maker.
- `RLIMIT_AS` is ineffective on macOS; Windows has no `resource` module. CI is
  Linux-only (every `runs-on` in `.github/workflows/`). Guard on `sys.platform`.

## 3. Lazy-import convention

`OPTIONAL_PLUGIN_IMPORT_MODULES` (`discovery.py:18-25`) = `{bs4, chromadb, html2text,
jinja2}`; skip logic `discovery.py:84-106` — only `ModuleNotFoundError` for those roots
is skipped; `except ImportError: raise`. Existing optional-native plugins import at
MODULE scope and rely on that skip. Deferred variants to copy: `probe_factory.py:57`
(ImportError = config bug, crash); `aws_s3_common.py:36-40` (re-raise as actionable
`ImportError`); `rag/config.py:25-63` (lazy capability registry).

`PLUGIN_SCAN_CONFIG` (`discovery.py:287-292`) is non-recursive and explicit:
`plugins/infrastructure/rasterize/**` is never scanned; `plugins/transforms/pdf_rasterize.py`
is. Because the plugin imports nothing native at module scope, discovery always
registers it; `pypdfium2` must NOT be added to `OPTIONAL_PLUGIN_IMPORT_MODULES`.

## 4. pypdfium2 5.13.0 API facts (verified by running the wheel)

Wheel `py3-none-manylinux_2_17_x86_64`; pure ctypes; bundles `pypdfium2_raw/libpdfium.so`
(7.67 MB); no C extension; no `py.typed`.

**Import side effect**: `pypdfium2/__init__.py:4` → `_library_scope.init_lib()` at module
scope + `atexit.register(destroy_lib)`. Importing initialises the native library in
that process — worker-only import is substantive, not cosmetic.

Verified sequence:
```
pdf = pypdfium2.PdfDocument(<bytes>)   # bytes accepted; holds a ref to the buffer
len(pdf)                                # FPDF_GetPageCount
page = pdf[0]
page.get_size()  -> (200.0, 100.0)      # points, BEFORE any render (page.py:54-72)
page.get_rotation() -> 0
```
`get_mediabox()` falls back to ANSI A when undefined and does not inherit — use
`get_size()`.

Rendering (`page.py:354-363`, `:466-495`): `src_width = math.ceil(get_width() * scale)`;
predicted pixels = `ceil(w_pt*scale) × ceil(h_pt*scale)`, computable before the call.
`scale = dpi / 72`. Format: default BGR (3 ch); `rev_byteorder=True` → RGB. Verified
`page.render(scale=2, rev_byteorder=True)` → `w=400 h=200 stride=1200 format=2 n_channels=3
mode=RGB packed=True`; `bitmap.buffer` is `ctypes.Array[c_ubyte]`, row-major,
top-to-bottom, length `stride*height`; public attrs `.stride .width .height .format
.n_channels .mode .rev_byteorder` (`bitmap.py:49-67`). With `new_native` and no custom
stride, `stride == width * n_channels`.

**Pillow NOT required**: `to_pil()` lazy-imports PIL (`_lazy.py:13-16`). A stdlib PNG
encoder (`zlib` + `struct` + `zlib.crc32`; filter-byte-0 scanlines; IHDR/IDAT/IEND;
colour type 2 for RGB) produced a valid file (`file out.png` → `PNG image data, 400 x 200,
8-bit/color RGB`). Pillow is absent from the repo entirely.

Errors: single `PdfiumError(RuntimeError)` with `.err_code` (`_helpers/misc.py:7-21`).
Codes `SUCCESS=0, UNKNOWN=1, FILE=2, FORMAT=3, PASSWORD=4, SECURITY=5, PAGE=6`. Verified
against a real AES-128 PDF: no/wrong password → `err_code=4`; garbage/truncated →
`err_code=3`. `init_forms()` only for form-field content (must precede page handles);
default render is correct without it. `draw_annots=True` is default; consider
`draw_annots=False` for a hardened path.

**Distroless runtime VERIFIED**: `libpdfium.so` NEEDED = `libpthread, libm, libgcc_s,
libc, ld-linux`; max `GLIBC_2.16`; no libstdc++. Runtime image
(`gcr.io/distroless/python3-debian13:debug-nonroot@sha256:6418f576…`, glibc 2.41) ran
the render+encode path end to end: `DISTROLESS RUN OK`.

## 5. Dependency mechanics

- Base deps `pyproject.toml:20-91`; rule `:86-87`: "if a non-optional unit test
  references a plugin, that plugin's deps live here" (catalog/knob-schema pins are
  non-optional) → `pypdfium2` in BASE `dependencies`.
- `all` extra (`:196-253`) hand-flattened; base deps never appear there.
- `uv.lock` (`version = 1, revision = 3`); CI `uv sync --frozen --all-extras`
  (`ci.yaml:727`); Docker `uv sync --frozen`. Regenerate with `uv lock`, never
  `uv pip install` in the worktree.
- mypy strict `:336-357`; stubless block `:359-378` — add `pypdfium2`, `pypdfium2.*`,
  `pypdfium2_raw`, `pypdfium2_raw.*` (required even for an in-function import).
- pip-audit `ci.yaml:729-757` two ignores; pip-licenses `:759-763` `--fail-on "GPL;AGPL"`
  reads the `License:` field only — passes. Bundled BUILD_LICENSES scanned: FreeType is
  FTL; only ICU/LLVM texts mention GPL as compatibility clauses.
- Docker `INSTALL_EXTRAS` validator `[a-z0-9-]` — irrelevant for a base dep.

## 6. Terminal-vocabulary lint (`manifest.symbol_inventory`)

`elspeth-lints/.../manifest/symbol_inventory/rule.py`; scans `src/elspeth` only
(`:232-249`), NOT tests. Fires only on `==`/`!=`/`in`/`not in` against a symbol whose
name ends `outcome`/`path` with a literal from `ROW_OUTCOME_VALUES` /
`TERMINAL_OUTCOME_VALUES` (`success, failure, transient`) / `TERMINAL_PATH_VALUES`
(`rule.py:34-67, 158-202`). `is_terminal` is flagged anywhere in src (`:126-156`).

Rules for the worker protocol: use a `StrEnum` compared by member; values outside the
three sets (`rendered`, `refused`, `oversize_pixels`, `encrypted`, `malformed`,
`timed_out`); never name a symbol `..._outcome`/`..._path` that is compared to a
literal; never use the identifier `is_terminal`.

## Probe artifacts (scratchpad, not in the tree)

`/tmp/claude-1000/-home-john-elspeth/43b59023-9440-4d72-b32c-a0dec852905c/scratchpad/pdfium/`:
`mkpdf.py` (minimal valid one-page PDF generator with correct xref), `sample.pdf`,
`enc.pdf` (AES-128, user password `secret`, owner `owner`; copied to
`tests/fixtures/pdf/encrypted_aes128_user_secret.pdf`), `probe*.py`, `out.png`.
