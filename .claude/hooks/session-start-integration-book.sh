#!/bin/sh
# SessionStart hook: start every session with the integration book's short
# version and where to read the whole book.
#
# Claude Code caps a hook's stdout at 10,000 characters; longer output is
# saved to a file and replaced by its path and a short preview, and Claude is
# not asked to read it. The book is far longer than that, so this hook prints
# only its short version plus the book's path. On a new session or after
# /clear it asks for the whole book to be read before any work; on resume or
# after compaction it asks only for the sections the next step needs.
#
# Book source, in order: this checkout's own copy; else one of the fixed refs
# below, read from the local remote-tracking refs (no fetch). Only these refs
# are trusted to supply text that every session reads. Update the second one
# when the integration branch changes. A copy taken from git is written to
# .claude/lanes/integration-book-copy/ (gitignored) so the Read tool can open it.
# Prints nothing if no copy is found.

TRUSTED_REFS="origin/main origin/integration/0817"

root=${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel 2>/dev/null)}
[ -n "$root" ] || exit 0
book=docs/maintainer/integration-book.md
source=$(jq -r '.source // empty' 2>/dev/null)

if [ -f "$root/$book" ]; then
  path="$root/$book"
  origin="this checkout"
else
  path=""
  for ref in $TRUSTED_REFS; do
    if git -C "$root" cat-file -e "$ref:$book" 2>/dev/null; then
      dir="$root/.claude/lanes/integration-book-copy"
      mkdir -p "$dir" || exit 0
      tmp=$(mktemp "$dir/.book.XXXXXX") || exit 0
      if git -C "$root" show "$ref:$book" > "$tmp" && mv -f "$tmp" "$dir/integration-book.md"; then
        path="$dir/integration-book.md"
        origin="$ref as last fetched, because this checkout has no copy"
      else
        rm -f "$tmp"
      fi
      break
    fi
  done
  [ -n "$path" ] || exit 0
fi

case "$source" in
  resume|compact)
    ask="You have read this book before in this session. Before acting on any rule, re-read the section the next step needs with the Read tool"
    ;;
  *)
    ask="Before starting any work, read the whole integration book with the Read tool"
    ;;
esac

printf 'INTEGRATION BOOK. %s: %s (taken from %s). It is the rulebook for how agents work together on this repository: roles and the lead integrator, rulings, landing, testing, the shared machine, kills and cleanup, subagents, and public text. Where it and AGENTS.md disagree, stop and ask the integrator. Its short version follows; each line is expanded in the section named in brackets.\n\n' "$ask" "$path" "$origin"
short=$(awk '/^\*\*The short version\*\*/{on=1} /^## 2\. /{on=0} on' "$path")
if [ -n "$short" ]; then
  printf '%s\n' "$short"
else
  printf '(The short version could not be found in the book; read the whole book.)\n'
fi
exit 0
