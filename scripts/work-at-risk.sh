#!/usr/bin/env bash
# work-at-risk.sh — read-only report of work that exists only on this disk.
#
# Canonical form of the check that would have caught the 2026-10-24 incident
# (docs/maintainer/lane-process-review-2026-10-24.md): two agent trees sat
# about 62 h and 66 h with no commit while their source lived in gitignored
# snapshot directories, and the lanes held 1,287 full copies of src/elspeth
# (about 215 GB) that `git status` cannot see. A 12 h warning would have fired
# about two days earlier. Nothing in the repository bounded uncommitted work.
#
# What it does — READ-ONLY; it has no --execute and changes nothing on disk:
#   For every registered worktree (`git worktree list --porcelain`) it prints
#     STATE      CLEAN, DIRTY, UNKNOWN (`git status` failed) or MISSING
#     IN-USE     a live process has its cwd inside the tree (/proc/<pid>/cwd)
#     DIRTY      count of uncommitted and untracked paths
#     OLDEST-H   hours since the oldest of those paths was modified (mtime)
#     COMMIT-H   hours since the HEAD commit
#     SRC-COPIES src/elspeth/__init__.py files under paths git IGNORES in that
#                tree (the snapshots `git status` cannot see)
#     LANES      apparent size of the tree's .claude/lanes/
#   and one WARN line per finding:
#     uncommitted work older than 12 h · any ignored source copy · lanes > 10 GB
#
# Rules baked in (each one is a past incident):
#   - DIRTY is reported even when the tree is IN-USE. worktree-cleanup.sh ranks
#     IN-USE first, so a live agent's tree with 534 uncommitted entries showed
#     only as "in use" on 2026-10-24. As in worktree-cleanup.sh, the tree you
#     run the report from shows IN-USE: your own shell is a process in it.
#   - Gitignored source is probed separately from `git status`: the copies
#     were invisible to every dirty-tree check. The probe lists ignored
#     directories with `git ls-files --others --ignored --exclude-standard
#     --directory`, then finds src/elspeth/__init__.py beneath them. It prunes
#     every other registered worktree (the main checkout's ignored
#     .claude/worktrees/ holds them, and their tracked src/ is not a copy) and
#     .venv, node_modules, __pycache__ and .git by name.
#   - A `git status` that FAILS classifies UNKNOWN and WARNs, never CLEAN:
#     empty output from a failed command is not evidence.
#   - Failed ignored-directory or traversal reads report UNKNOWN and WARN.
#     Copy caps and lane-size cutoffs remain honest lower bounds; only an
#     observed cutoff can admit its producer's expected SIGPIPE.
#   - Every git read goes through `git --no-optional-locks`, so the report does
#     not rewrite any worktree's index stat-cache.
#   - LANES is a sum of apparent file sizes that stops at the 10 GB limit and
#     prints ">10.0G". A full `du` of the main checkout's 141 GB lanes took 26 s;
#     the early exit took 2.2 s.
#   - SRC-COPIES stops counting at 50 and prints "50+" unless --count-all is
#     given. Walking the main checkout's 4.4 M lane files with GNU find took
#     23 s for the exact count of 954 and 2.2 s to reach 50. The WARN needs only
#     one copy. (Timings taken with an interactive `find` may be a parallel bfs
#     the agent harness substitutes; this script runs GNU find.)
#   - Deleted paths count as DIRTY but have no mtime; OLDEST-H covers the paths
#     that still exist.
#
# Cost, measured 2026-10-24 over 66 registered worktrees: 25.6 s, of which
# about 15 s is the walk over the two lanes directories that hold the
# incident's copies (141 GB and 66 GB). Pruning them shrinks the run.
#
# Usage:
#   scripts/work-at-risk.sh               # run at session start and before any handover
#   scripts/work-at-risk.sh --count-all   # exact SRC-COPIES (slow on a large lanes dir)
#
# Exit codes:  0 no WARN · 1 at least one WARN · 2 usage error or not a git tree

set -euo pipefail

AGE_LIMIT_H=12
LANES_LIMIT_BYTES=$((10 * 1024 * 1024 * 1024))
COPY_CAP=50

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
    case "$1" in
        --count-all) COPY_CAP=0 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

git rev-parse --git-dir >/dev/null 2>&1 || { echo "not inside a git repository" >&2; exit 2; }
# The first entry of `git worktree list` is always the main checkout.
# (awk reads the whole listing: an early `exit` would SIGPIPE git under pipefail)
if ! WORKTREE_INVENTORY="$(git --no-optional-locks worktree list --porcelain)"; then
    echo "cannot read the registered worktree inventory; report is incomplete" >&2
    exit 2
fi
MAIN="$(awk 'NR==1 && $1=="worktree" {sub(/^worktree /, ""); print}' <<<"$WORKTREE_INVENTORY")"
[ -d "$MAIN" ] || { echo "cannot resolve the main checkout" >&2; exit 2; }

NOW="$(date +%s)"
CWDS="$(for d in /proc/[0-9]*; do readlink "$d/cwd" 2>/dev/null || true; done | sort -u)"
in_use() { grep -qxF -- "$1" <<<"$CWDS" || grep -qF -- "$1/" <<<"$CWDS"; }

# Parse the porcelain listing once: every registered path, and which are prunable.
declare -a WTS=()
declare -A PRUNABLE=()
cur=""
while IFS= read -r line; do
    case "$line" in
        "worktree "*) cur="${line#worktree }"; WTS+=("$cur") ;;
        prunable*)    PRUNABLE["$cur"]=1 ;;
    esac
done <<<"$WORKTREE_INVENTORY"

hours() { echo $(( ($1) / 3600 )); }

# oldest_mtime <tree>: reads NUL-separated paths relative to <tree> on stdin and
# prints the smallest mtime among those that still exist (nothing if none do).
oldest_mtime() {
    { (cd "$1" && xargs -0 -r stat -c %Y -- 2>/dev/null) || true; } | sort -n | head -n 1
}

# source_copies <tree>: count src/elspeth/__init__.py under the tree's ignored dirs.
source_copies() {
    local tree="$1" entry listing_pid count read_rc
    local -a starts=() prune=()
    while IFS= read -r -d '' entry; do
        [[ "$entry" == */ ]] && starts+=("$tree/${entry%/}")
    done < <(git --no-optional-locks -C "$tree" ls-files -z --others --ignored --exclude-standard --directory 2>/dev/null)
    listing_pid=$!
    if ! wait "$listing_pid"; then return 1; fi
    [ ${#starts[@]} -gt 0 ] || { echo 0; return; }
    # The listing can name a directory and its children (`.claude/` and
    # `.claude/lanes/`); walking both counts every copy twice. Keep only
    # starts with no listed ancestor.
    local -A listed=()
    local -a top=()
    local s anc
    for s in "${starts[@]}"; do listed["$s"]=1; done
    for s in "${starts[@]}"; do
        anc="${s%/*}"
        while [ "${#anc}" -gt "${#tree}" ] && [ -z "${listed[$anc]:-}" ]; do anc="${anc%/*}"; done
        [ "${#anc}" -gt "${#tree}" ] || top+=("$s")
    done
    starts=("${top[@]}")
    # A -path test per registered worktree on every file made one run take 241 s,
    # so worktree prunes apply only to the ignored dirs that contain a worktree.
    local -a plain=() nested=()
    local w inner
    for s in "${starts[@]}"; do
        inner=0
        for w in "${WTS[@]}"; do
            [ "$w" = "$tree" ] && continue
            [[ "$w" == "$s" || "$w" == "$s"/* ]] && { inner=1; prune+=(-path "$w" -o); }
        done
        if [ "$inner" = 1 ]; then nested+=("$s"); else plain+=("$s"); fi
    done
    local -a names=(-name .venv -o -name node_modules -o -name __pycache__ -o -name .git)
    # One byte per match makes the cap/count independent of filename newlines.
    local -a match=(-name __init__.py -path '*/src/elspeth/__init__.py' -printf '.')
    local -a limit=(cat)
    [ "$COPY_CAP" -gt 0 ] && limit=(head -c "$COPY_CAP")
    if count="$(
        {
            if [ ${#plain[@]} -gt 0 ]; then
                find "${plain[@]}" -type d \( "${names[@]}" \) -prune -o "${match[@]}" 2>/dev/null || exit "$?"
            fi
            if [ ${#nested[@]} -gt 0 ]; then
                find "${nested[@]}" -type d \( "${prune[@]}" "${names[@]}" \) -prune -o "${match[@]}" 2>/dev/null || exit "$?"
            fi
        } | "${limit[@]}" | wc -c
    )"; then
        printf '%s\n' "$count"
    else
        read_rc=$?
        # Only this expected downstream cutoff preserves a measured lower
        # bound. An arbitrary producer error stays incomplete even at the cap.
        if [ "$read_rc" = 141 ] && [ "$COPY_CAP" -gt 0 ] && [ "$count" -ge "$COPY_CAP" ]; then
            printf '%s\n' "$count"
        else
            return 1
        fi
    fi
}

# lanes_bytes <tree>: apparent size of .claude/lanes, stopping once past the limit.
lanes_bytes() {
    local dir="$1/.claude/lanes" measured read_rc
    [ -d "$dir" ] || { echo 0; return; }
    if measured="$(find "$dir" -type f -printf '%s\n' 2>/dev/null \
        | awk -v lim="$LANES_LIMIT_BYTES" '{ s += $1; if (s > lim) { print s; exit } } END { if (s <= lim) print s + 0 }')"; then
        printf '%s\n' "$measured"
    else
        read_rc=$?
        if [ "$read_rc" = 141 ] && [ -n "$measured" ] && [ "$measured" -gt "$LANES_LIMIT_BYTES" ]; then
            printf '%s\n' "$measured"
        else
            return 1
        fi
    fi
}

gb() { awk -v b="$1" 'BEGIN { printf "%.1fG", b / 1073741824 }'; }

declare -a WARNS=()
warn() { WARNS+=("[WARN] $1 — $2  ($3)"); }

echo "main checkout : $MAIN"
echo "limits        : uncommitted > ${AGE_LIMIT_H} h · any ignored src/elspeth copy · lanes > $(gb "$LANES_LIMIT_BYTES")"
echo
printf '%-8s %-6s %6s %8s %8s %10s %8s  %s\n' STATE IN-USE DIRTY OLDEST-H COMMIT-H SRC-COPIES LANES PATH

for wt in "${WTS[@]}"; do
    if [ -n "${PRUNABLE[$wt]:-}" ] || [ ! -d "$wt" ]; then
        printf '%-8s %-6s %6s %8s %8s %10s %8s  %s\n' MISSING - - - - - - "$wt"
        continue
    fi
    use="-"; in_use "$wt" && use="yes"

    state="CLEAN" ndirty=0 oldest_h="-"
    st=""
    if st="$(git --no-optional-locks -C "$wt" status --porcelain=v1 -z -uall --no-renames 2>/dev/null | tr '\0' '\n')"; then
        if [ -n "$st" ]; then
            state="DIRTY"
            ndirty="$(grep -c . <<<"$st" || true)"
            oldest="$(cut -c4- <<<"$st" | tr '\n' '\0' | oldest_mtime "$wt")"
            if [ -n "$oldest" ]; then
                age=$(( NOW - oldest ))
                oldest_h="$(hours "$age")"
                if [ "$age" -gt $(( AGE_LIMIT_H * 3600 )) ]; then
                    warn "$wt" "$ndirty uncommitted path(s), oldest modified ${oldest_h} h ago$([ "$use" = yes ] && echo ' (tree is IN-USE)')" \
                        "git status --porcelain -uall; stat %Y"
                fi
            fi
        fi
    else
        state="UNKNOWN"
        warn "$wt" "git status failed; cannot prove the tree holds no uncommitted work" "git status"
    fi

    commit_h="-"
    if ct="$(git --no-optional-locks -C "$wt" log -1 --format=%ct HEAD 2>/dev/null)" && [ -n "$ct" ]; then
        commit_h="$(hours $(( NOW - ct )))"
    fi

    if copies="$(source_copies "$wt")"; then
        if [ "$copies" -gt 0 ]; then
            [ "$COPY_CAP" -gt 0 ] && [ "$copies" -ge "$COPY_CAP" ] && copies="${COPY_CAP}+"
            warn "$wt" "$copies src/elspeth/__init__.py under gitignored paths (source outside git)" \
                "git ls-files --others --ignored --directory; find"
        fi
    else
        copies="UNKNOWN"
        warn "$wt" "ignored-source-copy probe failed; count UNKNOWN" "git ls-files --others --ignored --directory; find"
    fi

    if lb="$(lanes_bytes "$wt")"; then
        lanes="$(gb "$lb")"
        if [ "$lb" -gt "$LANES_LIMIT_BYTES" ]; then
            lanes=">$(gb "$LANES_LIMIT_BYTES")"
            warn "$wt" ".claude/lanes exceeds $(gb "$LANES_LIMIT_BYTES")" "find -printf %s"
        fi
    else
        lanes="UNKNOWN"
        warn "$wt" ".claude/lanes size probe failed; size UNKNOWN" "find -printf %s"
    fi

    printf '%-8s %-6s %6s %8s %8s %10s %8s  %s\n' "$state" "$use" "$ndirty" "$oldest_h" "$commit_h" "$copies" "$lanes" "$wt"
done

echo
if [ ${#WARNS[@]} -eq 0 ]; then
    echo "summary: ${#WTS[@]} worktree(s), no configured probe warnings"
    exit 0
fi
printf '%s\n' "${WARNS[@]}"
echo
echo "summary: ${#WTS[@]} worktree(s), ${#WARNS[@]} warning(s) — commit the work or move it into a branch"
exit 1
