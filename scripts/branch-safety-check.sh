#!/usr/bin/env bash
# branch-safety-check.sh — read-only pre-flight before a commit, rebase, merge or push.
#
# Canonical form of the checks this repo's agents ran by hand, measured from the
# session transcripts on 2026-09-09 (54,325 Bash calls):
#   git status --short 2663 · git log --oneline 3056 · git rev-parse HEAD 1087
#   git diff --stat 1069 · merge-base --is-ancestor 529 · rev-parse @{u}/origin 546
#   git fetch 526 · elspeth.__file__ provenance probe 360 · three-dot diff 243 (a trap)
#
# Every check prints one line:  [PASS|WARN|FAIL|INFO] <check> — <evidence>  (<instrument>)
# so the report carries the command that produced each fact. FAIL means "do not
# proceed with --intent"; WARN means "proceed knowingly"; nothing here changes the tree.
#
# READ-ONLY BY DEFAULT. The single state-changing option is --fetch, which
# refreshes the remote-tracking refs before the upstream comparison. Without it
# the report is against whatever `origin/*` was last fetched, and says so.
#
# Checks, and the incident each one encodes:
#   in-progress    unfinished operations FAIL; commit may finish a resolved merge
#                  only when no other operation or unmerged index entries remain
#   protected      on main or release/*: commit WARNs; rebase/push FAIL unless
#                  --allow-protected (never move release/main unless asked)
#   staged-paths   .claude/lanes|handovers|red-team|worktrees, *.log, *.done, *.pid,
#                  scratch artefacts (AGENTS.md "Commit Hygiene")                      FAIL
#   home-paths     a literal $HOME anywhere in a staged BLOB (added, modified,
#                  renamed or copied path; whole content, binary included), read
#                  from the index with `git show :<path>` — never parsed out of
#                  a diff, whose "+++ " header is indistinguishable by shape
#                  from added content starting "++ "                                 FAIL
#                  any other /home/<x> or /Users/<x> in staged content                WARN
#   secrets        scripts/git-hooks/pre-commit-secret-scan.sh over the staged set    FAIL
#   published      HEAD == @{u}: never --amend/rebase a published commit; and the
#                  reflog of origin/<branch> shows whether a sibling session pushed it
#   base           named TWO-dot range <base>..HEAD: ahead/behind counts, diffstat,
#                  and the log. Three-dot `git diff A...B` silently widens after a
#                  rebase, so this script never uses it for a diff.
#   elsewhere      the branch is also checked out in another worktree                 INFO
#   rerere         rerere.enabled + autoupdate replay ANOTHER lane's conflict
#                  resolution into your rebase; use `git -c rerere.enabled=false`     WARN
#   hmac-key       ELSPETH_JUDGE_METADATA_HMAC_KEY in the environment ([O1]): 18 test
#                  ids fail on every tree and lint verification silently changes      FAIL
#   venv           the worktree has no .venv symlink (subprocess tests fail as
#                  ordinary assertion errors)                                          WARN
#   provenance     elspeth.__file__ and elspeth_lints.__file__ resolve INSIDE this
#                  tree with PYTHONPATH set the AGENTS.md way; a bare import in a
#                  worktree measures the MAIN checkout (a confidently wrong answer)    FAIL
#
# Usage:
#   scripts/branch-safety-check.sh                      # intent=commit, no fetch
#   scripts/branch-safety-check.sh --intent push --fetch
#   scripts/branch-safety-check.sh --intent rebase --base origin/release/0.8.0
#
# Options:
#   --intent commit|rebase|merge|push   what you are about to do (default: commit)
#   --base REF        the integration ref for the range checks (default: @{u} if set,
#                     else origin/HEAD if set; otherwise the base checks are skipped)
#   --fetch           `git fetch origin` first (the only write this script performs)
#   --allow-protected downgrade the protected-branch FAIL to WARN for rebase/push
#   -h, --help
#
# Exit codes:  0 no FAIL · 1 at least one FAIL · 2 usage / not a git tree

set -euo pipefail

INTENT="commit"; BASE=""; FETCH=0; ALLOW_PROTECTED=0
usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
    case "$1" in
        --intent) INTENT="${2:?--intent needs a value}"; shift ;;
        --base) BASE="${2:?--base needs a ref}"; shift ;;
        --fetch) FETCH=1 ;;
        --allow-protected) ALLOW_PROTECTED=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac; shift
done
case "$INTENT" in commit|rebase|merge|push) ;; *) echo "--intent must be commit|rebase|merge|push" >&2; exit 2 ;; esac

ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "not inside a git work tree" >&2; exit 2; }
cd "$ROOT"
GIT_DIR="$(git rev-parse --git-dir)"
COMMON_DIR="$(git rev-parse --git-common-dir)"
MAIN="$(git worktree list --porcelain | awk 'NR==1 && $1=="worktree" {sub(/^worktree /, ""); print}')"

FAILS=0; WARNS=0
report() { # report LEVEL check evidence instrument
    local lvl="$1" chk="$2" ev="$3" ins="${4:-}"
    case "$lvl" in FAIL) FAILS=$((FAILS+1)) ;; WARN) WARNS=$((WARNS+1)) ;; esac
    printf '[%s] %-13s %s' "$lvl" "$chk" "$ev"
    [ -n "$ins" ] && printf '  (%s)' "$ins"
    printf '\n'
}

# --- where am I -------------------------------------------------------------
BRANCH="$(git branch --show-current)"
HEAD_SHA="$(git rev-parse HEAD)"
if [ "$ROOT" = "$MAIN" ]; then kind="MAIN checkout (shared)"; else kind="worktree of $MAIN"; fi
echo "tree   : $ROOT  [$kind]"
echo "HEAD   : ${HEAD_SHA:0:9}  branch: ${BRANCH:-(detached)}  intent: $INTENT"
echo

# --- in-progress operations --------------------------------------------------
inprog=""
for f in CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG; do [ -e "$GIT_DIR/$f" ] && inprog="$inprog $f"; done
for d in rebase-merge rebase-apply sequencer; do [ -d "$GIT_DIR/$d" ] && inprog="$inprog $d"; done
if ! unmerged="$(git ls-files --unmerged)"; then
    report FAIL in-progress "could not inspect unmerged index entries" "git ls-files --unmerged"
elif [ -n "$unmerged" ]; then
    report FAIL in-progress "unmerged index entries remain" "git ls-files --unmerged"
elif [ -n "$inprog" ]; then
    report FAIL in-progress "unfinished:$inprog" "ls $GIT_DIR"
elif [ -e "$GIT_DIR/MERGE_HEAD" ]; then
    if [ "$INTENT" = commit ]; then
        report PASS in-progress "resolved merge ready for completion by commit; no other operation" "ls $GIT_DIR; git ls-files --unmerged"
    else
        report FAIL in-progress "unfinished: MERGE_HEAD; complete the merge before $INTENT" "ls $GIT_DIR"
    fi
else
    report PASS in-progress "none" "ls $GIT_DIR; git ls-files --unmerged"
fi

# --- detached / protected ----------------------------------------------------
if [ -z "$BRANCH" ]; then
    report WARN branch "detached HEAD; a commit here is reachable only by sha" "git branch --show-current"
elif [[ "$BRANCH" == main || "$BRANCH" == release/* ]]; then
    msg="$BRANCH is a protected integration branch"
    case "$INTENT" in
        commit|merge) report WARN protected "$msg; stage only your own pathspecs" "git branch --show-current" ;;
        *) if [ "$ALLOW_PROTECTED" = 1 ]; then report WARN protected "$msg (--allow-protected)" "git branch --show-current"
           else report FAIL protected "$msg; $INTENT needs the operator to name it (or --allow-protected)" "git branch --show-current"; fi ;;
    esac
else
    report PASS protected "$BRANCH is a task branch" "git branch --show-current"
fi

# --- working tree and staged set --------------------------------------------
STATUS="$(git --no-optional-locks status --porcelain)"
n_staged=$(grep -cE '^[MADRCT]' <<<"$STATUS" || true)
n_unstaged=$(grep -cE '^.[MD]' <<<"$STATUS" || true)
n_untracked=$(grep -cE '^\?\?' <<<"$STATUS" || true)
report INFO worktree "staged=$n_staged unstaged=$n_unstaged untracked=$n_untracked" "git status --porcelain"

STAGED_FILES="$(git diff --cached --name-only)"
if [ -n "$STAGED_FILES" ]; then
    bad="$(grep -E '^\.claude/(lanes|handovers|red-team|worktrees)/|\.(log|done|pid)$|(^|/)(dry-run|dryrun)[^/]*$|^\.elspeth/' <<<"$STAGED_FILES" || true)"
    if [ -n "$bad" ]; then report FAIL staged-paths "artefact paths staged: $(tr '\n' ' ' <<<"$bad")" "git diff --cached --name-only"
    else report PASS staged-paths "no lane/log/artefact paths in the staged set" "git diff --cached --name-only"; fi

    # Scan the STAGED BLOBS, not the diff. Parsing a unified diff by line shape
    # is ambiguous — added content that starts with "++ " is byte-identical to a
    # "+++ b/path" header once git prepends its own "+" — and the rule is about
    # tracked-file CONTENT, so a renamed or copied file that already carried the
    # path must fail too. `git show :<path>` reads the index blob for every
    # added/modified/renamed/copied staged path; NULs from binary blobs are
    # dropped so bash does not warn, and grep -a keeps scanning them.
    staged_blobs="$(git diff --cached --name-only -z --diff-filter=AMRC | xargs -0 -r -I{} git show ':{}' | tr -d '\0' || true)"
    if grep -qaF -- "$HOME" <<<"$staged_blobs"; then
        report FAIL home-paths "a staged blob contains $HOME; hooks and skills bind to the checkout, not a user" "git diff --cached --name-only -z --diff-filter=AMRC | xargs -0 -I{} git show ':{}' | grep -aF \$HOME"
    elif grep -qaE '/(home|Users)/[A-Za-z0-9_.-]+' <<<"$staged_blobs"; then
        report WARN home-paths "a staged blob mentions a /home or /Users path: $(grep -aoE '/(home|Users)/[A-Za-z0-9_.-]+' <<<"$staged_blobs" | sort -u | tr '\n' ' ')" "git show :<staged path> | grep -aE '/(home|Users)/'"
    else
        report PASS home-paths "no user-home paths in any staged blob" "git show :<staged path> | grep -aE '/(home|Users)/'"
    fi

    if [ -x scripts/git-hooks/pre-commit-secret-scan.sh ]; then
        if scripts/git-hooks/pre-commit-secret-scan.sh >/dev/null 2>&1; then
            report PASS secrets "staged set is clean" "scripts/git-hooks/pre-commit-secret-scan.sh"
        else
            report FAIL secrets "credential-shaped strings in the staged set; run the hook for the lines" "scripts/git-hooks/pre-commit-secret-scan.sh"
        fi
    fi
else
    report INFO staged-paths "nothing staged" "git diff --cached --name-only"
fi

# --- upstream / published ----------------------------------------------------
if [ "$FETCH" = 1 ]; then
    if git fetch --quiet origin 2>/dev/null; then report INFO fetch "origin refreshed" "git fetch origin"
    else report WARN fetch "git fetch origin failed; remote-tracking refs may be stale" "git fetch origin"; fi
else
    report INFO fetch "not fetched (--fetch to refresh); origin/* is as of the last fetch" ""
fi

UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
if [ -n "$UPSTREAM" ]; then
    U_SHA="$(git rev-parse '@{u}')"
    # rev-list's three-dot is the symmetric difference — the right tool for
    # ahead/behind. It is `git diff`'s three-dot that is the trap, not this one.
    read -r behind ahead <<<"$(git rev-list --left-right --count '@{u}...HEAD')"
    if [ "$HEAD_SHA" = "$U_SHA" ]; then
        report INFO published "HEAD == $UPSTREAM: PUBLISHED — never --amend or rebase it" "git rev-parse HEAD @{u}"
    else
        report INFO upstream "$UPSTREAM: ahead $ahead, behind $behind" "git rev-list --left-right --count @{u}...HEAD"
        [ "$ahead" -gt 0 ] && report INFO unpublished "$ahead local commit(s) not on $UPSTREAM" "git log --oneline @{u}..HEAD"
    fi
    if [ -n "$BRANCH" ]; then
        # A sibling session sharing this .git can push your commit within seconds;
        # the reflog records it locally as "update by push", no fetch required.
        pushes="$(git reflog show --date=iso "refs/remotes/origin/$BRANCH" 2>/dev/null | grep -c 'update by push' || true)"
        last="$(git reflog show --date=iso "refs/remotes/origin/$BRANCH" 2>/dev/null | grep -m1 'update by push' | cut -c1-80 || true)"
        [ -n "$last" ] && report INFO push-log "$pushes push(es) recorded locally; latest: $last" "git reflog show origin/$BRANCH --date=iso"
    fi
else
    report INFO upstream "no upstream configured for ${BRANCH:-HEAD}" "git rev-parse --abbrev-ref @{u}"
fi

# --- base relation (named two-dot range) -------------------------------------
if [ -z "$BASE" ]; then
    if [ -n "$UPSTREAM" ]; then BASE="$UPSTREAM"
    elif git rev-parse --verify --quiet origin/HEAD >/dev/null; then BASE="origin/HEAD"; fi
fi
if [ -n "$BASE" ] && git rev-parse --verify --quiet "${BASE}^{commit}" >/dev/null; then
    BASE_SHA="$(git rev-parse "${BASE}^{commit}")"
    ahead_b="$(git rev-list --count "$BASE_SHA..HEAD")"
    behind_b="$(git rev-list --count "HEAD..$BASE_SHA")"
    if git merge-base --is-ancestor "$BASE_SHA" HEAD; then
        report PASS base "$BASE (${BASE_SHA:0:9}) is an ancestor of HEAD; ahead $ahead_b" "git merge-base --is-ancestor $BASE HEAD"
    else
        report WARN base "HEAD is behind $BASE by $behind_b (ahead $ahead_b); rebase ONLY when you are next" "git rev-list --count HEAD..$BASE"
    fi
    if [ "$ahead_b" -gt 0 ]; then
        stat="$(git diff --stat "$BASE_SHA..HEAD" | tail -1 | sed 's/^ *//')"
        report INFO delta "$BASE..HEAD: $stat" "git diff --stat $BASE..HEAD   # two-dot, base NAMED"
        echo "        log $BASE..HEAD (newest first, max 12):"
        # -n 12 rather than `| head`: head closing the pipe SIGPIPEs git under pipefail
        git log --oneline -n 12 "$BASE_SHA..HEAD" | sed 's/^/          /'
    fi
elif [ -n "$BASE" ]; then
    report WARN base "--base $BASE does not resolve" "git rev-parse $BASE"
else
    report INFO base "no base ref (no upstream, no origin/HEAD); pass --base REF" ""
fi

# --- branch checked out elsewhere -------------------------------------------
if [ -n "$BRANCH" ]; then
    others="$(git worktree list --porcelain | awk -v b="refs/heads/$BRANCH" -v me="$ROOT" '
        $1=="worktree"{p=$2} $1=="branch" && $2==b && p!=me {print p}')"
    [ -n "$others" ] && report INFO elsewhere "branch also checked out in: $(tr '\n' ' ' <<<"$others")" "git worktree list --porcelain"
fi

# --- rerere ------------------------------------------------------------------
if [ "$(git config --get rerere.enabled || true)" = true ]; then
    au="$(git config --get rerere.autoupdate || echo false)"
    if [[ "$INTENT" == rebase || "$INTENT" == merge ]]; then
        report WARN rerere "enabled (autoupdate=$au) and the cache is shared across worktrees: run with 'git -c rerere.enabled=false $INTENT ...'" "git config rerere.enabled"
    else
        report INFO rerere "enabled (autoupdate=$au); matters for rebase/merge" "git config rerere.enabled"
    fi
fi

# --- environment -------------------------------------------------------------
if [ -n "${ELSPETH_JUDGE_METADATA_HMAC_KEY:-}" ]; then
    report FAIL hmac-key "ELSPETH_JUDGE_METADATA_HMAC_KEY is set in this shell ([O1]); tell the operator, do not run gates from here" "env | grep -c ^ELSPETH_JUDGE"
else
    report PASS hmac-key "not present in the environment" "env | grep ^ELSPETH_JUDGE"
fi

# --- venv + provenance -------------------------------------------------------
if [ -e "$ROOT/.venv/bin/python" ]; then
    PY="$ROOT/.venv/bin/python"; report PASS venv "$ROOT/.venv present" "ls -la .venv"
else
    PY="$MAIN/.venv/bin/python"
    report WARN venv "no .venv in this worktree: subprocess tests will fail as assertion errors; run scripts/worktree-cleanup.sh --link-venv" "ls -la .venv"
fi
if [ -x "$PY" ]; then
    prov="$(PYTHONPATH="$ROOT/src:$ROOT/elspeth-lints/src" "$PY" -c '
import elspeth, elspeth_lints
print(elspeth.__file__); print(elspeth_lints.__file__)' 2>&1 || true)"
    e_file="$(sed -n 1p <<<"$prov")"; l_file="$(sed -n 2p <<<"$prov")"
    if [[ "$e_file" == "$ROOT/src/"* && "$l_file" == "$ROOT/elspeth-lints/src/"* ]]; then
        report PASS provenance "elspeth and elspeth_lints import from THIS tree" "PYTHONPATH=\$ROOT/src:\$ROOT/elspeth-lints/src python -c 'import elspeth; print(elspeth.__file__)'"
    else
        report FAIL provenance "imports resolve outside this tree: $(tr '\n' ' ' <<<"$prov")" "python -c 'import elspeth; print(elspeth.__file__)'"
    fi
else
    report FAIL provenance "no usable python at $PY" "ls -la $PY"
fi

echo
echo "result : FAIL=$FAILS WARN=$WARNS  intent=$INTENT  HEAD=${HEAD_SHA:0:9}"
[ "$FAILS" -eq 0 ]
