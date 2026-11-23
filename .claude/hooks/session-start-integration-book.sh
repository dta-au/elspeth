#!/usr/bin/env bash
# SessionStart hook: start every session with the integration book's short
# version and an instruction to read the whole book before doing any work.
#
# Claude Code caps a hook's stdout at 10,000 characters; longer output is
# saved to a file and replaced by its path and a short preview, and Claude is
# not asked to read it. The book is far longer than that, so this hook prints
# only its short version (well under the cap) plus where to read the rest.
#
# Book source, in order: this checkout's own copy; else origin/main; else the
# newest pushed integration branch (the book can land there before main).
# A copy taken from git is written to .claude/lanes/integration-book/ (git-
# ignored) so the Read tool can open it. Prints nothing if no copy is found.
set -u

root=${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}
[ -n "$root" ] || exit 0
book=docs/maintainer/integration-book.md

if [ -f "$root/$book" ]; then
  path="$root/$book"
  source="this checkout"
else
  refs=(origin/main)
  newest=$(git -C "$root" for-each-ref --sort=-committerdate --count=1 \
    --format='%(refname:short)' refs/remotes/origin/integration/ 2>/dev/null)
  [ -n "$newest" ] && refs+=("$newest")
  path=""
  for ref in "${refs[@]}"; do
    if text=$(git -C "$root" show "$ref:$book" 2>/dev/null); then
      dir="$root/.claude/lanes/integration-book"
      mkdir -p "$dir" || exit 0
      tmp=$(mktemp "$dir/.book.XXXXXX") || exit 0
      printf '%s\n' "$text" > "$tmp" && mv -f "$tmp" "$dir/integration-book.md" || exit 0
      path="$dir/integration-book.md"
      source="$ref, because this checkout has no copy"
      break
    fi
  done
  [ -n "$path" ] || exit 0
fi

short=$(awk '/^\*\*The short version\*\*/{on=1} /^## 2\. /{on=0} on' "$path")

printf 'INTEGRATION BOOK. Before starting any work, read the whole integration book with the Read tool: %s (taken from %s). It is the rulebook for how agents work together on this repository: roles and the lead integrator, rulings, landing, testing, the shared machine, kills and cleanup, subagents, and public text. Where it and AGENTS.md disagree, stop and ask the integrator. Its short version follows; each line is expanded in the section named in brackets.\n\n' "$path" "$source"
if [ -n "$short" ]; then
  printf '%s\n' "$short"
else
  printf '(The short version could not be found in the book; read the whole book.)\n'
fi
exit 0
