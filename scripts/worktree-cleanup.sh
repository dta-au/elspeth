#!/usr/bin/env bash
# worktree-cleanup.sh — idempotent audit and cleanup of registered git worktrees.
#
# Canonical form of the ad-hoc loop this repo's agents ran hundreds of times:
#   for w in .claude/worktrees/*; do git -C "$w" status --short; ...; git worktree remove ...
# Measured from the session transcripts on 2026-09-09 (54,325 Bash calls):
#   git worktree list 263 · add 238 · remove 191 (58 of them --force) · prune 22
#   ln -s .venv into a worktree 213 · git branch -D 60
#
# What it does — DRY-RUN BY DEFAULT; nothing changes without --execute:
#   1. Enumerates every registered worktree (`git worktree list --porcelain`).
#   2. Classifies each one; the most protective class wins:
#        MAIN       the main checkout                         never touched
#        MISSING    registered but the directory is gone       -> prune
#        LOCKED     `git worktree lock` is set                 keep
#        IN-USE     a live process has its cwd inside it       keep
#        DIRTY      uncommitted or untracked files             keep (never discard work)
#        UNKNOWN    `git status` itself failed there           keep (cannot prove clean)
#        UNMERGED   commits not reachable from --base          keep (work not landed)
#        IGNORED    clean and landed, but holds gitignored     keep unless
#                   content beyond caches (.claude/lanes/,     --discard-ignored
#                   .elspeth/, data/, .env ...)
#        REMOVABLE  clean, landed, no such content             -> remove
#   3. Flags NO-VENV on present worktrees that lack the `.venv` symlink back to
#      the main checkout (AGENTS.md "Worktrees live under .claude/worktrees").
#      A missing link makes subprocess tests fail as ordinary assertion errors;
#      --link-venv repairs it.
#
# Safety rules baked in (each one is a past incident):
#   - never `git worktree remove --force` on a present tree: a DIRTY tree is
#     kept, full stop. Untracked files count as dirty; a killed agent may have
#     left finished hunks behind and re-dispatching blind discards them.
#     Gitignored content is NOT in `git status`, and `git worktree remove`
#     deletes it silently, so it is probed separately (`--ignored=matching`):
#     caches (.venv, __pycache__, .pytest_cache, .mypy_cache, .ruff_cache,
#     .hypothesis, *.egg-info, .coverage) are disregarded; anything else —
#     lane notes under .claude/lanes/, a local .elspeth/ or data/ — keeps the
#     tree as IGNORED until you pass --discard-ignored.
#   - a `git status` that FAILS in a worktree (broken .git pointer) classifies
#     UNKNOWN, never clean: empty output from a failed command is not evidence.
#   - the ONLY forced removal is of a MISSING registration under --path, where
#     the directory is re-checked to be absent at action time, so nothing can
#     be destroyed; without --path, `git worktree prune` handles them. A
#     repo-wide prune is never run under --path (it would drop other lanes'
#     registrations outside the filter).
#   - "landed" means `git merge-base --is-ancestor <head> <base>`, not a branch
#     name match. A squash-merged branch therefore shows UNMERGED — correct,
#     because its commits are not in the base; remove it by hand after checking.
#   - IN-USE is decided from /proc/<pid>/cwd, never from a pgrep pattern:
#     every session invokes the same .venv python, so a pattern matches siblings.
#   - --delete-branches only acts on the branch of a worktree that was
#     REMOVABLE, so it can never delete unlanded commits, and never touches
#     main or release/* whatever their state.
#   - IN-USE is re-checked immediately before each removal, not only at
#     classification time.
#   - Removing a worktree unlinks its .venv symlink; the main checkout's .venv is
#     never followed. This script never runs `uv pip install` anywhere.
#   - every read goes through `git --no-optional-locks`, so the dry run does not
#     rewrite any worktree's index stat-cache; it changes nothing on disk.
#
# Usage:
#   scripts/worktree-cleanup.sh                     # report only
#   scripts/worktree-cleanup.sh --execute           # prune MISSING, remove REMOVABLE
#   scripts/worktree-cleanup.sh --execute --delete-branches --link-venv
#   scripts/worktree-cleanup.sh --path '*/p4-*'     # restrict to matching paths
#   scripts/worktree-cleanup.sh --base origin/main  # "landed" relative to another ref
#
# Options:
#   --execute            perform the actions (default: print them)
#   --base REF           ref that defines "landed" (default: the branch the MAIN
#                        checkout has checked out; required if it is detached)
#   --path GLOB          only consider worktrees whose absolute path matches GLOB
#   --delete-branches    after removing a REMOVABLE worktree, delete its branch
#   --discard-ignored    treat IGNORED worktrees as REMOVABLE (their gitignored
#                        content is deleted with them)
#   --link-venv          create the missing .venv symlink in NO-VENV worktrees
#   --quiet              print only actions and the summary, not the full table
#   -h, --help
#
# Exit codes:
#   0  report printed (dry-run) or every requested action succeeded
#   1  at least one action failed (the rest were still attempted)
#   2  usage error or preconditions not met
#
# Idempotent: a second run after --execute finds nothing MISSING or REMOVABLE.

set -euo pipefail

EXECUTE=0
BASE=""
PATH_GLOB=""
DELETE_BRANCHES=0
DISCARD_IGNORED=0
LINK_VENV=0
QUIET=0

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --execute) EXECUTE=1 ;;
        --base) BASE="${2:?--base needs a ref}"; shift ;;
        --path) PATH_GLOB="${2:?--path needs a glob}"; shift ;;
        --delete-branches) DELETE_BRANCHES=1 ;;
        --discard-ignored) DISCARD_IGNORED=1 ;;
        --link-venv) LINK_VENV=1 ;;
        --quiet) QUIET=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

# ---------------------------------------------------------------------------
# Locate the main checkout. The first entry of `git worktree list` is always
# the main worktree, whichever worktree we were invoked from.
# ---------------------------------------------------------------------------
git rev-parse --git-dir >/dev/null 2>&1 || { echo "not inside a git repository" >&2; exit 2; }
# (awk reads the whole listing: an early `exit` would SIGPIPE git under pipefail)
MAIN="$(git worktree list --porcelain | awk 'NR==1 && $1=="worktree" {sub(/^worktree /, ""); print}')"
[ -d "$MAIN" ] || { echo "cannot resolve the main checkout" >&2; exit 2; }

if [ -z "$BASE" ]; then
    BASE="$(git -C "$MAIN" branch --show-current)"
    # Fully qualify the derived default: rev-parse prefers a TAG over a branch of
    # the same name, so a stray tag would silently redefine "landed".
    [ -n "$BASE" ] && BASE="refs/heads/$BASE"
    if [ -z "$BASE" ]; then
        echo "main checkout is detached; pass --base REF to define \"landed\"" >&2
        exit 2
    fi
fi
BASE_SHA="$(git -C "$MAIN" rev-parse --verify --quiet "${BASE}^{commit}")" \
    || { echo "--base $BASE does not resolve to a commit" >&2; exit 2; }

# ---------------------------------------------------------------------------
# Snapshot the cwd of every live process ONCE. A worktree is IN-USE when some
# process's cwd is the worktree or lies beneath it. readlink fails on processes
# we cannot inspect; those are simply not counted.
# ---------------------------------------------------------------------------
CWDS="$(for d in /proc/[0-9]*; do readlink "$d/cwd" 2>/dev/null || true; done | sort -u)"

in_use() {
    local wt="$1"
    # exact match or prefix match with a path separator
    grep -qxF -- "$wt" <<<"$CWDS" || grep -qF -- "$wt/" <<<"$CWDS"
}

# ---------------------------------------------------------------------------
# Parse the porcelain listing into one classified row per worktree.
# Porcelain blocks look like:
#   worktree /abs/path
#   HEAD <sha>
#   branch refs/heads/name        (or: detached)
#   locked [reason]               (optional)
#   prunable [reason]             (optional)
#   <blank line>
# ---------------------------------------------------------------------------
declare -a ROWS=()          # "CLASS|AHEAD|VENV|BRANCH|PATH"
declare -a MISSING=() REMOVABLE=() NOVENV=()
declare -A BRANCH_OF=() IGNORED_OF=()

wt="" head="" branch="" locked=0 prunable=0
reset() { wt="" head="" branch="" locked=0 prunable=0; }
flush() {
    [ -n "$wt" ] || return 0
    if [ -n "$PATH_GLOB" ]; then
        # shellcheck disable=SC2053  # the glob is meant to be unquoted here
        # Reset ALL parsed state on a filtered-out block: leaving `prunable`
        # behind once mis-classified the next matching worktree as MISSING.
        [[ "$wt" == $PATH_GLOB ]] || { reset; return 0; }
    fi
    local class="" ahead="-" venv="-" shortbranch="${branch#refs/heads/}"
    [ -n "$shortbranch" ] || shortbranch="(detached)"

    if [ "$wt" = "$MAIN" ]; then
        class="MAIN"
    elif [ "$prunable" = 1 ] || [ ! -d "$wt" ]; then
        class="MISSING"; MISSING+=("$wt")
    else
        # present worktree: venv status is independent of the class
        if [ -e "$wt/.venv" ]; then venv="ok"; else venv="NO-VENV"; NOVENV+=("$wt"); fi
        if [ "$locked" = 1 ]; then
            class="LOCKED"
        elif in_use "$wt"; then
            class="IN-USE"
        else
            # One status call answers both "dirty?" and "ignored content?".
            # --no-optional-locks: do not rewrite the worktree's index on a read.
            # The `if` form is deliberate: under `set -e` a failing command
            # substitution in a plain assignment would abort the whole script,
            # and swallowing it would read as "clean" (the fail-open the reviewer
            # reached by breaking a .git pointer).
            local st="" st_ok=1
            if st="$(git --no-optional-locks -C "$wt" status --porcelain --ignored=matching 2>/dev/null)"; then :; else st_ok=0; fi
            local dirty ignored
            dirty="$(grep -v '^!!' <<<"$st" || true)"
            ignored="$(grep '^!! ' <<<"$st" | sed 's/^!! //' \
                | grep -vE '^(\.venv/?|\.coverage|.*/?(__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.hypothesis|[^/]*\.egg-info)/?)$' \
                | grep -vE '(^|/)__pycache__/|\.pyc$' || true)"
            if [ "$st_ok" = 0 ]; then
                class="UNKNOWN"
            elif [ -n "$dirty" ]; then
                class="DIRTY"
            elif ! git -C "$MAIN" merge-base --is-ancestor "$head" "$BASE_SHA"; then
                class="UNMERGED"
                ahead="$(git -C "$MAIN" rev-list --count "$BASE_SHA..$head")"
            elif [ -n "$ignored" ] && [ "$DISCARD_IGNORED" = 0 ]; then
                class="IGNORED"; ahead="$(grep -c . <<<"$ignored")"
                IGNORED_OF["$wt"]="$(head -n 3 <<<"$ignored" | tr '\n' ' ')"
            else
                class="REMOVABLE"; REMOVABLE+=("$wt"); BRANCH_OF["$wt"]="$shortbranch"
            fi
        fi
    fi
    ROWS+=("$class|$ahead|$venv|$shortbranch|$wt")
    reset
}

while IFS= read -r line; do
    case "$line" in
        "worktree "*) flush; wt="${line#worktree }" ;;
        "HEAD "*)     head="${line#HEAD }" ;;
        "branch "*)   branch="${line#branch }" ;;
        detached)     branch="" ;;
        locked*)      locked=1 ;;
        prunable*)    prunable=1 ;;
        "")           flush ;;
    esac
done < <(git -C "$MAIN" worktree list --porcelain; echo)

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
echo "main checkout : $MAIN"
echo "base (landed) : $BASE @ ${BASE_SHA:0:9}"
echo "mode          : $([ "$EXECUTE" = 1 ] && echo EXECUTE || echo DRY-RUN)"
echo

if [ "$QUIET" = 0 ]; then
    printf '%-10s %6s %-8s %-48s %s\n' CLASS AHEAD VENV BRANCH PATH
    for row in "${ROWS[@]}"; do
        IFS='|' read -r c a v b p <<<"$row"
        printf '%-10s %6s %-8s %-48s %s\n' "$c" "$a" "$v" "$b" "$p"
        [ "$c" = IGNORED ] && printf '%-10s %6s %-8s %-48s   holds: %s\n' '' '' '' '' "${IGNORED_OF[$p]}"
    done
    echo
fi

count() { local k="$1" n=0; for row in "${ROWS[@]}"; do [ "${row%%|*}" = "$k" ] && n=$((n+1)); done; echo "$n"; }
echo "summary: main=$(count MAIN) missing=$(count MISSING) locked=$(count LOCKED) in-use=$(count IN-USE)" \
     "dirty=$(count DIRTY) unknown=$(count UNKNOWN) unmerged=$(count UNMERGED) ignored=$(count IGNORED)" \
     "removable=$(count REMOVABLE) no-venv=${#NOVENV[@]}"
echo

# ---------------------------------------------------------------------------
# Actions. Every action is printed in the form that would run; under DRY-RUN
# that is all that happens.
# ---------------------------------------------------------------------------
FAILED=0
act() {
    # act <description> <command...>
    local desc="$1"; shift
    if [ "$EXECUTE" = 1 ]; then
        echo "RUN   $desc"
        if "$@"; then echo "  ok"; else echo "  FAILED (exit $?)"; FAILED=1; fi
    else
        echo "WOULD $desc"
    fi
}

remove_missing() {
    # Re-check at action time: the directory must still be absent. --force is
    # what git requires to drop a registration whose directory is gone; with
    # nothing on disk there is nothing it can destroy.
    [ ! -e "$1" ] || { echo "  refusing: $1 exists now"; return 1; }
    git -C "$MAIN" worktree remove --force "$1"
}
if [ ${#MISSING[@]} -gt 0 ]; then
    if [ -n "$PATH_GLOB" ]; then
        # Bounded to the filter: a repo-wide prune would also drop every OTHER
        # lane's missing registration and the printed count would be wrong.
        for w in "${MISSING[@]}"; do act "git worktree remove --force $w   # MISSING: directory absent" remove_missing "$w"; done
    else
        # `git worktree prune` drops every registration whose directory is gone.
        # Idempotent; harmless when there is nothing to prune.
        act "git worktree prune -v   # ${#MISSING[@]} MISSING registration(s)" git -C "$MAIN" worktree prune -v
    fi
fi

for w in "${REMOVABLE[@]}"; do
    # Re-take the IN-USE snapshot for this path at action time: the classification
    # pass is seconds old and a process may have entered the tree since (TOCTOU).
    if [ "$EXECUTE" = 1 ]; then
        CWDS="$(for d in /proc/[0-9]*; do readlink "$d/cwd" 2>/dev/null || true; done | sort -u)"
        if in_use "$w"; then echo "SKIP  git worktree remove $w   # became IN-USE since classification"; continue; fi
    fi
    # No --force: git re-checks cleanliness itself, a second guard behind ours.
    act "git worktree remove $w" git -C "$MAIN" worktree remove "$w"
    if [ "$DELETE_BRANCHES" = 1 ] && [[ "${BRANCH_OF[$w]}" == main || "${BRANCH_OF[$w]}" == release/* ]]; then
        # A protected integration branch is never deleted, landed or not: a
        # worktree that happens to have it checked out says nothing about it.
        echo "SKIP  git branch -D ${BRANCH_OF[$w]}   # protected integration branch"
    elif [ "$DELETE_BRANCHES" = 1 ] && [ "${BRANCH_OF[$w]}" != "(detached)" ]; then
        # -D rather than -d: -d only accepts branches merged into HEAD/upstream,
        # but we already proved this head is an ancestor of --base, which is the
        # stronger, explicitly named test. Skipped under DRY-RUN like everything else.
        act "git branch -D ${BRANCH_OF[$w]}   # landed in $BASE" git -C "$MAIN" branch -D "${BRANCH_OF[$w]}"
    fi
done

if [ "$LINK_VENV" = 1 ]; then
    for w in "${NOVENV[@]}"; do
        [ -d "$w" ] || continue   # removed earlier in this same run
        # A dangling symlink (target moved) is replaced; a real directory is left alone,
        # because a worktree with its own venv is a deliberate choice, not a defect.
        if [ -L "$w/.venv" ]; then act "rm dangling $w/.venv" rm -f "$w/.venv"; fi
        [ -d "$w/.venv" ] && continue
        act "ln -s $MAIN/.venv $w/.venv" ln -s "$MAIN/.venv" "$w/.venv"
    done
fi

if [ "$EXECUTE" = 0 ] && { [ ${#MISSING[@]} -gt 0 ] || [ ${#REMOVABLE[@]} -gt 0 ] || { [ "$LINK_VENV" = 1 ] && [ ${#NOVENV[@]} -gt 0 ]; }; }; then
    echo
    echo "dry-run: re-run with --execute to apply the actions above"
fi

exit "$FAILED"
