#!/usr/bin/env bash
# Red-team post-commit trigger. Fail-soft by design: adversarial review must
# never block or slow down git.
#
# Classification (cheap, synchronous): does HEAD touch a security seam?
# If yes, the agent fleet is launched fully detached (setsid + nohup) so the
# hook returns immediately and the agents survive the hook's process group.
# Output lands in .claude/red-team/ (gitignored).
#
# Opt-in via the installer (install-post-commit-red-team.sh); disable any
# time with ELSPETH_RED_TEAM_DISABLE=1.

set -u

if [[ "${ELSPETH_RED_TEAM_DISABLE:-0}" == "1" ]]; then
    exit 0
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
PYTHON="${REPO_ROOT}/.venv/bin/python"
[[ -x "$PYTHON" ]] || exit 0

# Exit 0 = seam matched, 3 = no seam, anything else = classifier error.
"$PYTHON" -m scripts.red_team.trigger --repo-root "$REPO_ROOT" \
    classify --commit HEAD >/dev/null 2>&1
case $? in
    0) ;;
    *) exit 0 ;;
esac

LOG_DIR="${REPO_ROOT}/.claude/red-team"
mkdir -p "$LOG_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

setsid nohup "$PYTHON" -m scripts.red_team.trigger --repo-root "$REPO_ROOT" \
    run --commit HEAD \
    >"${LOG_DIR}/trigger-${STAMP}.log" 2>&1 </dev/null &

exit 0
