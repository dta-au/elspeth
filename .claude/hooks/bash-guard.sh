#!/usr/bin/env bash
# PreToolUse guard for the Bash tool. Reads the hook JSON on stdin and refuses
# (exit 2) commands that agents have run by mistake with wide damage:
#   - git commit --amend and git checkout -- <path>;
#   - broad or pattern-based process kills. An agent that meant to stop its own
#     run has more than once killed every other session's processes instead.
# A kill of explicit numeric PIDs (kill -TERM 12345) is allowed: that is the
# form the integration book's kill procedure uses once the lead integrator has
# authorised it. This is a backstop for slips; it does not see Codex, scripts
# that stop their own children, or commands typed by hand.
set -u

cmd=$(jq -r '.tool_input.command // empty')
[ -n "$cmd" ] || exit 0

refuse() {
  printf 'BLOCKED by .claude/hooks/bash-guard.sh: %s\n' "$1" >&2
  exit 2
}

if grep -Eq 'commit --amend|checkout -- ' <<<"$cmd"; then
  refuse "git commit --amend and git checkout -- are refused here."
fi

# A word in command position: line start, or after ; & | ( { or a backtick,
# optionally behind sudo, nohup, exec, command, env, or a shell keyword such
# as the `do` of a loop.
cp='(^|[;&|({`])[[:space:]]*((sudo|nohup|exec|command|env|do|then|else|time)[[:space:]]+)*'
# One argument token that does not cross into the next command.
tok='[^[:space:];&|)]+'
# kill's options: -SIG, -9, or -s SIG / -n NUM.
opt="(-[sn][[:space:]]+${tok}|-${tok})"

kill_shapes=(
  "${cp}(pkill|killall)([[:space:]]|$)"
  "${cp}kill([[:space:]]+${opt})+[[:space:]]+(--[[:space:]]+)?-[0-9]+([[:space:]]|$|[;&|)])"
  "${cp}kill[[:space:]]+--[[:space:]]+-[0-9]+"
  "${cp}kill([[:space:]]+${tok})*[[:space:]]+0([[:space:]]|$|[;&|)])"
  "${cp}kill([[:space:]]+${tok})*[[:space:]]+(\\\$\\(|\`)"
  "xargs([[:space:]]+-${tok})*[[:space:]]+((sudo|nohup|exec|command|env)[[:space:]]+)*(kill|pkill|killall)([[:space:]]|$)"
  "killpg[[:space:]]*\\("
  "os\\.kill\\([[:space:]]*(-|0[[:space:]]*[,)])"
)

for shape in "${kill_shapes[@]}"; do
  if grep -Eq -- "$shape" <<<"$cmd"; then
    refuse "broad or pattern-based kill (pkill, killall, kill -1, kill 0, kill -- -PGID, kill \$(...), xargs kill, killpg). All process kills, CI cancels included, go via the lead integrator: send it the exact PIDs with each one's command line, cwd, parent and age, and after its explicit authority signal those PIDs by number (kill -TERM <pid>). Procedure: docs/maintainer/integration-book.md, section 8, \"How to kill a process\"."
  fi
done

exit 0
