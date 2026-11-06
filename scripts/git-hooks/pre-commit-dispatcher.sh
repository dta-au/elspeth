#!/usr/bin/env bash
# Dispatcher: routes the pre-commit hook through pre-commit with the staged
# path list supplied explicitly.
#
# Why this exists: the default pre-commit staged-file isolation can stash and
# restore unstaged changes. The older ELSPETH local hook avoided that by running
# `pre-commit run --all-files`, but that made pre-commit a slow whole-repo gate.
# Passing `--files <staged>` keeps stash=False while preserving the incremental
# pre-commit contract. Deleted paths are not passed because a cached deletion
# may still exist in the worktree; the deletion-only branch supplies a
# guaranteed-nonexistent sentinel so always_run hooks execute without stashing
# or exposing unstaged content to filename-bound hooks. PR CI remains the
# full-codebase gate.
# Selected regular files must match the index before filename hooks run.
# Keep their writers held until commit completes; this check does not lock them.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
INSTALL_PYTHON="${ELSPETH_PRE_COMMIT_PYTHON:-${REPO_ROOT}/.venv/bin/python3}"
cd "$REPO_ROOT"

mapfile -d '' -t STAGED_FILES < <(
    git --no-optional-locks -c diff.autoRefreshIndex=false diff --cached --name-only -z --diff-filter=ACMRTUXB
)
# Process substitution does not propagate Git's exit status through mapfile.
STAGED_DIFF_PID=$!
if ! wait "$STAGED_DIFF_PID"; then
    echo 'Cannot read staged paths; refusing filename hooks.' >&2
    exit 1
fi

for STAGED_PATH in "${STAGED_FILES[@]}"; do
    # pre-commit normalizes ./-name back to -name, then appends filenames to
    # hook argv without an option terminator. Do not disguise this boundary:
    # a leading-dash top-level name (including a directory) is unsupported.
    if [[ "$STAGED_PATH" == -* ]]; then
        printf 'Unsupported leading-dash staged path %q; refusing filename hooks.\n' "$STAGED_PATH" >&2
        exit 1
    fi
    # Omit filenames from this metadata format: tabs/newlines in a path must
    # not turn one index entry into several records. Literal pathspecs also
    # prevent glob and :(...) filenames from selecting other entries.
    if ! STAGED_ENTRY="$(git --literal-pathspecs ls-files --format='%(objectmode) %(objectname) %(stage)' -- "$STAGED_PATH")"; then
        printf 'Cannot read index entry for %q; refusing filename hooks.\n' "$STAGED_PATH" >&2
        exit 1
    fi
    if [[ ! "$STAGED_ENTRY" =~ ^([0-9]{6})\ ([0-9a-f]+)\ 0$ ]]; then
        printf 'Expected one resolved index entry for %q; refusing filename hooks.\n' "$STAGED_PATH" >&2
        exit 1
    fi
    STAGED_MODE=${BASH_REMATCH[1]}
    STAGED_OBJECT=${BASH_REMATCH[2]}
    case "$STAGED_MODE" in
        100644|100755) ;;
        *)
            # Hooks follow filenames, not Git's symlink text or gitlink tree.
            printf 'Unsupported staged file mode %s for %q; refusing filename hooks.\n' "$STAGED_MODE" "$STAGED_PATH" >&2
            exit 1
            ;;
    esac
    if [[ -L "$STAGED_PATH" || ! -f "$STAGED_PATH" ]]; then
        printf 'Working input is not a regular file: %q; refusing filename hooks.\n' "$STAGED_PATH" >&2
        exit 1
    fi
    if ! git cat-file blob "$STAGED_OBJECT" > /dev/null; then
        printf 'Cannot read staged blob for %q; refusing filename hooks.\n' "$STAGED_PATH" >&2
        exit 1
    fi
    if ! WORKING_PERMISSIONS="$(stat -c '%a' -- "$STAGED_PATH")" || [[ ! "$WORKING_PERMISSIONS" =~ ^[0-7]+$ ]]; then
        printf 'Cannot read working mode for %q; refusing filename hooks.\n' "$STAGED_PATH" >&2
        exit 1
    fi
    # Git records the owner's executable bit, not every POSIX permission.
    # Check it explicitly even when core.fileMode=false masks Git's own diff.
    WORKING_MODE=100644
    if (( (8#$WORKING_PERMISSIONS & 0100) != 0 )); then
        WORKING_MODE=100755
    fi
    if ! WORKING_OBJECT="$(git hash-object --no-filters -- "$STAGED_PATH")"; then
        printf 'Cannot read working bytes for %q; refusing filename hooks.\n' "$STAGED_PATH" >&2
        exit 1
    fi
    if [[ "$WORKING_MODE" != "$STAGED_MODE" || "$WORKING_OBJECT" != "$STAGED_OBJECT" ]]; then
        printf 'Staged bytes/mode differ from working input %q; stage the complete owned file before running filename hooks.\n' "$STAGED_PATH" >&2
        exit 1
    fi
done

if [[ ${#STAGED_FILES[@]} -eq 0 ]]; then
    if git --no-optional-locks -c diff.autoRefreshIndex=false diff --cached --quiet --; then
        exit 0
    else
        DIFF_STATUS=$?
        if [[ "$DIFF_STATUS" -ne 1 ]]; then
            echo 'Cannot determine staged deletions; refusing filename hooks.' >&2
            exit 1
        fi
    fi

    # `--files` with zero values is parsed as args.files=[] and re-enables
    # pre-commit's stash path. A child of Git's index file cannot exist, but is
    # a truthy filename argument: pre-commit drops it from the classifier while
    # keeping stash=False and running always_run hooks.
    INDEX_PATH="$(git rev-parse --git-path index)"
    STAGED_FILES=("${INDEX_PATH}/.elspeth-no-staged-files")
fi

if [[ -x "$INSTALL_PYTHON" ]]; then
    exec "$INSTALL_PYTHON" -mpre_commit run --files "${STAGED_FILES[@]}"
elif command -v pre-commit >/dev/null; then
    exec pre-commit run --files "${STAGED_FILES[@]}"
else
    echo '`pre-commit` not found. Did you forget to install project tooling?' >&2
    exit 1
fi
