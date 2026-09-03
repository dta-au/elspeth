# ELSPETH PDF Pipeline

Builds two document sets as professional PDFs, sharing one visual system:

| Set | Driver | Output |
|-----|--------|--------|
| Architecture Pack | `build-arch-pack.sh` | one bound volume, `out/elspeth-arch-pack.pdf` |
| Project control set | `build-control-pack.sh` | one PDF per document, written beside its source markdown in `docs/project-control/` |

## Pipeline

```text
docs/arch-pack-* or archived docs-archive/*/docs/arch-pack-*
                                      -> build-arch-pack.sh
                                      |
markdown chapters (4 tracks)      -> els_concat_chapters -> COMBINED
                                      |
                                      preprocess.py       -> PROCESSED
                                      |
                                      pandoc + filters    -> *.typ
                                      |
                                      postprocess.py      -> *.typ
                                      |
                                      typst compile       -> PDF
```

## Requirements

| Tool        | Minimum  |
|-------------|----------|
| `pandoc`    | 3.0      |
| `typst`     | 0.14     |
| `mmdc`      | mermaid-cli (Node) |
| `python3`   | 3.10+    |

The `typst` binary bundles the body and heading fonts (Libertinus Serif,
TeX Gyre Heros, Liberation Mono); no system font installation is needed.

## Build

```bash
./build-arch-pack.sh         # Generate .typ intermediate only
./build-arch-pack.sh --pdf   # Generate .typ and compile to PDF
```

Outputs:

- Intermediate: `tools/pdf/elspeth-arch-pack.typ` (gitignored)
- Final PDF: `tools/pdf/out/elspeth-arch-pack.pdf` (gitignored)

## Project control set

```bash
./build-control-pack.sh --list             # show the document keys
./build-control-pack.sh --pdf              # build every document
./build-control-pack.sh --pdf programme    # build one
```

One PDF per document rather than a bound volume: the RAID register carries its
own review cycle and is reissued independently of the programme document.
Finished PDFs are written beside their source markdown in
`docs/project-control/`, which is gitignored in full except its README, so they
are never staged and never meet the pre-commit large-file threshold. The `.typ`
intermediates stay in `out/`.

To add a document, add one line to the `DOCUMENTS` manifest in
`build-control-pack.sh` and a matching metadata YAML under `control-pack/`.

| Variable | Effect |
|----------|--------|
| `ELSPETH_CONTROL_PACK_DIR` | Source directory (default `docs/project-control`). |
| `ELSPETH_CONTROL_PACK_PDF_DIR` | Where finished PDFs are written (default: the source directory). |

## Environment overrides

| Variable                  | Effect |
|---------------------------|--------|
| `ELSPETH_ARCH_PACK_DIR`   | Use a specific arch-pack source directory instead of discovering the latest active or archived `arch-pack-*`. |
| `FORCE_DATE`              | Override the title-page date (defaults to today). |

## Files

| File                  | Purpose |
|-----------------------|---------|
| `build-control-pack.sh` | Driver for the project-control set.  Carries the document manifest (key, source markdown, metadata file) and builds each entry as a standalone PDF. |
| `control-pack/*.yaml` | Per-document title-page metadata for the control set — one file per document. |
| `build-arch-pack.sh`  | Top-level driver.  Defines the chapter list across four tracks (narrative, subsystem, appendix, reference) and orchestrates concat → preprocess → pandoc → postprocess → typst. |
| `lib.sh`              | Shared bash helpers: toolchain version checks, source discovery, chapter concatenation, pandoc invocation, typst compile, build-date stamping. |
| `metadata.yaml`       | Title-page metadata for the **arch pack**: title, status, classification, codebase HEAD, scope blurb, revision history.  Pandoc binds these into `template.typ`.  The control set uses one file per document under `control-pack/`. |
| `template.typ`        | Pandoc-driven Typst template.  Defines colour palette, page geometry, header/footer, heading styles, table styling, code-block styling, ToC, List of Tables, List of Figures, and the title page. |
| `preprocess.py`       | Markdown-stage transforms before pandoc.  Renders mermaid fences to PNG, strips standalone hrules, and rewrites relative inter-chapter links to plain text. |
| `postprocess.py`      | Typst-stage transforms after pandoc.  Strips redundant per-cell alignment directives. |
| `fix-tables.lua`      | Pandoc Lua filter.  Tags table-containing figures with `kind: table` for numbering, and applies hand-tuned column widths to tables matched by `table-profiles.json`. |
| `table-profiles.json` | Header-pattern-matched column-width overrides, shared by both drivers.  Add a profile only when an auto-sized table demonstrably reads poorly in the rendered PDF; match on at least two header cells so a profile cannot capture an unrelated table of the same width. |

## Adapting the pipeline

The visual system (deep navy + teal accent, ISO/NIST-style document control
table, A4 with binding gutter, 3-track footer chip) is a port of the
Wardline Framework Specification PDF pipeline.  Wardline-specific elements
(RFC 2119 keyword emphasis, rule-ID chips, severity-matrix landscape
rotation, Part I/II title-page grid, §-link auto-styling) were removed
because they don't match the arch-pack's register and would silently
mis-style or fail to resolve.

To adapt for a different document set:

1. **Change the chapter list** in `build-arch-pack.sh`.
2. **Update `metadata.yaml`** (title, identifier, classification, blurb).
3. **Tune `template.typ`** if you need a different palette, page geometry,
   or front-matter layout.
4. **Add table profiles** to `table-profiles.json` only after building the
   PDF and identifying tables whose auto-sized layout is poor.
