#!/bin/bash
# Build the ELSPETH project-control documents as separate professional PDFs.
#
# Companion to build-arch-pack.sh.  Where the arch pack concatenates many
# chapters into one volume, the control set is deliberately one PDF per
# document: the RAID register carries its own review cycle and is reissued
# independently of the program document.
#
# Pipeline (per document): source markdown -> preprocess.py (hrule strip,
# relative-link rewrite) -> pandoc (typst output) -> postprocess.py ->
# typst compile -> PDF.  Shares lib.sh, template.typ and the visual system
# with the arch pack so the set reads as one house style.
#
# Requirements: pandoc >= 3.0, typst >= 0.14, mermaid-cli (mmdc), python3.
#
# Usage:
#   ./build-control-pack.sh                    # .typ intermediates only, all docs
#   ./build-control-pack.sh --pdf              # compile every document to PDF
#   ./build-control-pack.sh --pdf program      # compile one document
#   ./build-control-pack.sh --list             # show the document keys
#
# Environment:
#   ELSPETH_CONTROL_PACK_DIR      Override the source directory (default:
#                                 docs/project-control).  Mirrors the
#                                 ELSPETH_ARCH_PACK_DIR idiom.
#   ELSPETH_CONTROL_PACK_PDF_DIR  Override where finished PDFs are written
#                                 (default: the source directory, so the
#                                 documents and their PDFs stay together).
#   FORCE_DATE                Override the title-page date (default: today).
#
# Note: docs/project-control/ is gitignored per ADR-024, so this tracked
# builder reads untracked sources by design — the same arrangement as the
# arch pack, which discovers its chapters under the ignored docs-archive/.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
# shellcheck source=lib.sh
source "$SCRIPT_DIR/lib.sh"

SOURCE_DIR="${ELSPETH_CONTROL_PACK_DIR:-$PROJECT_ROOT/docs/project-control}"
BUILD_DIR="$SCRIPT_DIR/out"        # .typ intermediates; gitignored (root .gitignore: tools/pdf/out/)
# Finished PDFs land beside their source markdown so the control set is one
# folder. docs/project-control/ is gitignored in full except its README, so the
# PDFs are never staged and never meet the pre-commit large-file threshold.
PDF_DIR="${ELSPETH_CONTROL_PACK_PDF_DIR:-$SOURCE_DIR}"
METADATA_DIR="$SCRIPT_DIR/control-pack"

# ─────────────────────────────────────────────────────────────
# Document manifest:  key | source markdown | metadata file
#
# Each entry becomes one self-contained PDF.  To add a document,
# add a line here and a matching metadata YAML under control-pack/.
# ─────────────────────────────────────────────────────────────
DOCUMENTS=(
    "program|2026-09-05-work-packages.md|program.yaml"
    "raid-register|2026-09-05-implementation-raid-register.md|raid-register.yaml"
    "prd|2026-09-05-elspeth-prd.md|prd.yaml"
)

doc_key()      { echo "${1%%|*}"; }
doc_source()   { local r="${1#*|}"; echo "${r%%|*}"; }
doc_metadata() { echo "${1##*|}"; }

if [[ "${1:-}" == "--list" ]]; then
    echo "Document keys:"
    for entry in "${DOCUMENTS[@]}"; do
        printf '  %-16s %s\n' "$(doc_key "$entry")" "$(doc_source "$entry")"
    done
    exit 0
fi

WANT_PDF=0
if [[ "${1:-}" == "--pdf" ]]; then
    WANT_PDF=1
    shift
fi

# Remaining arguments select specific documents; no arguments builds all.
SELECTED=("$@")

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "[error] source directory not found: $SOURCE_DIR" >&2
    echo "        set ELSPETH_CONTROL_PACK_DIR to override" >&2
    exit 1
fi

els_check_toolchain
mkdir -p "$BUILD_DIR" "$PDF_DIR"
echo "Source: $SOURCE_DIR"
echo "PDFs:   $PDF_DIR"

build_document() {
    local key="$1" source_file="$2" metadata_file="$3"

    local src="$SOURCE_DIR/$source_file"
    local metadata="$METADATA_DIR/$metadata_file"
    if [[ ! -f "$src" ]]; then
        echo "  [error] missing source: $src" >&2
        exit 1
    fi
    if [[ ! -f "$metadata" ]]; then
        echo "  [error] missing metadata: $metadata" >&2
        exit 1
    fi

    local output_typ="$BUILD_DIR/elspeth-$key.typ"
    local output_pdf="$PDF_DIR/elspeth-$key.pdf"
    local mermaid_dir="$SCRIPT_DIR/.mermaid-tmp-$key"

    local combined processed stamped
    combined="$(mktemp)"
    processed="$(mktemp)"
    stamped="$(mktemp --suffix=.yaml)"
    # shellcheck disable=SC2064  # expand the paths now, not at trap time
    trap "rm -f '$combined' '$processed' '$stamped'; rm -rf '$mermaid_dir'" RETURN

    echo ""
    echo "[$key] $source_file"

    : > "$combined"
    els_concat_chapters "$combined" "$SOURCE_DIR" "$source_file"

    echo "  Preprocessing markdown..."
    python3 "$SCRIPT_DIR/preprocess.py" \
        --input="$combined" \
        --output="$processed" \
        --mermaid-dir="$mermaid_dir" \
        --mermaid-rel-base="$SCRIPT_DIR"

    echo "  Stamping build date..."
    els_stamp_date "$metadata" "$stamped"

    echo "  Generating Typst intermediate..."
    els_run_pandoc "$processed" "$output_typ" "$stamped"

    echo "  Post-processing Typst output..."
    python3 "$SCRIPT_DIR/postprocess.py" "$output_typ" "$output_typ"
    echo "    -> $output_typ"

    if [[ "$WANT_PDF" == "1" ]]; then
        echo "  Compiling PDF..."
        els_compile_pdf "$output_typ" "$output_pdf"
        echo "    -> $output_pdf ($(wc -c < "$output_pdf" | xargs) bytes)"
    fi
}

BUILT=0
for entry in "${DOCUMENTS[@]}"; do
    key="$(doc_key "$entry")"
    if [[ ${#SELECTED[@]} -gt 0 ]] && [[ ! " ${SELECTED[*]} " == *" $key "* ]]; then
        continue
    fi
    build_document "$key" "$(doc_source "$entry")" "$(doc_metadata "$entry")"
    BUILT=$(( BUILT + 1 ))
done

if [[ "$BUILT" == "0" ]]; then
    echo "[error] no documents matched: ${SELECTED[*]}" >&2
    echo "        run with --list to see the available keys" >&2
    exit 1
fi

echo ""
echo "Done. $BUILT document(s)."
