#!/usr/bin/env bash
# pre-commit-secret-scan.sh — refuse commits that contain credential-shaped strings.
#
# Scans only files about-to-be-committed (git diff --cached --name-only --diff-filter=AM),
# not the whole repo. Skips binary files. Reports matches with file path + line number +
# matched pattern + the offending line so the operator can see what triggered.
#
# Install (per checkout):
#     ln -s ../../scripts/git-hooks/pre-commit-secret-scan.sh \
#           "$(git rev-parse --git-path hooks)/pre-commit"
#     chmod +x scripts/git-hooks/pre-commit-secret-scan.sh
#
# Bypass (only for documented placeholder strings the script can't tell from real ones):
# rewrite the line to look less credential-shaped (e.g., REDACTED-jwt-style-string),
# OR add a per-line comment marker `# secret-scan: allow-this-line` (the script ignores
# lines containing that exact string).
#
# DO NOT bypass with --no-verify. The whole point is the safe path is the cheap path.

set -euo pipefail

# Colours for terminal output (disabled if not a TTY)
if [ -t 1 ]; then
    RED='\033[0;31m'; YEL='\033[0;33m'; CYAN='\033[0;36m'; RST='\033[0m'
else
    RED=''; YEL=''; CYAN=''; RST=''
fi

# Patterns: name + regex. PCRE-flavoured but stuck to grep -E for portability.
# Ordered roughly by confidence: high-confidence first.
declare -a PATTERNS=(
    "JWT|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}"
    "OpenAI key|sk-[A-Za-z0-9]{40,}"
    "Anthropic key|sk-ant-[A-Za-z0-9_-]{40,}"
    "Slack token|xox[bpoars]-[A-Za-z0-9-]{20,}"
    "AWS access key|AKIA[A-Z0-9]{16}"
    "Google API key|AIza[A-Za-z0-9_-]{35}"
    "GitHub token|gh[pousr]_[A-Za-z0-9]{36}"
    "Bearer token (high-entropy)|Bearer [A-Za-z0-9._/+=-]{40,}"
    "Private key block|-----BEGIN ([A-Z]+ )?PRIVATE KEY-----"
    "Connection string with password|(postgres|postgresql|mysql|mongodb|redis|amqp)://[^:[:space:]]+:[^@[:space:]]{4,}@"
    "High-entropy quoted secret|(secret|api[_-]?key|access[_-]?token|password|passwd|client[_-]?secret)['\"]?\s*[:=]\s*['\"][A-Za-z0-9_/+=-]{30,}['\"]"
)

# Paths to scan: prefer filenames passed as args (pre-commit framework convention).
# Fall back to git diff --cached for direct .git/hooks/pre-commit invocation.
# The paths only select index entries: every content read below uses --cached,
# never the working-tree file. Always exclude this script itself (its pattern
# definitions are not credentials).
if [ "$#" -gt 0 ]; then
    files=("$@")
else
    mapfile -d '' -t files < <(git diff --cached --name-only --diff-filter=AMRC -z 2>/dev/null)
fi

if [ "${#files[@]}" -eq 0 ]; then
    exit 0
fi

found_count=0

for file in "${files[@]}"; do
    [ -z "$file" ] && continue
    [ "$file" = "scripts/git-hooks/pre-commit-secret-scan.sh" ] && continue
    # A caller may supply a path that has no staged blob (for example a
    # deletion). Only added, modified, renamed, or copied index blobs can add a
    # secret to this commit.
    if ! git cat-file -e ":$file" 2>/dev/null; then continue; fi

    for pattern_entry in "${PATTERNS[@]}"; do
        name="${pattern_entry%%|*}"
        regex="${pattern_entry#*|}"
        # git grep --cached reads the index blob. -I skips binary entries, -n
        # reports line numbers, and -E selects extended regular expressions.
        matches=$(git grep --cached -InE "$regex" -- "$file" 2>/dev/null || true)
        if [ -n "$matches" ]; then
            # Filter out lines marked with the per-line allow comment
            filtered=$(echo "$matches" | grep -v 'secret-scan: allow-this-line' || true)
            if [ -n "$filtered" ]; then
                if [ $found_count -eq 0 ]; then
                    echo
                    printf "${RED}pre-commit-secret-scan: refusing to commit — credential-shaped strings detected${RST}\n" >&2
                    echo
                fi
                printf "${YEL}%s${RST} matched in:\n" "$name" >&2
                while IFS= read -r m; do
                    printf "  ${CYAN}%s${RST}\n" "$m" >&2
                done <<< "$filtered"
                echo >&2
                found_count=$((found_count + 1))
            fi
        fi
    done
done

if [ $found_count -gt 0 ]; then
    cat >&2 <<'EOF'
What to do:
  - If real: remove the credential, rotate it (assume it's been seen by you and
    anyone whose hook log it touched), and recommit.
  - If a documented placeholder the script can't tell apart from a real one:
    either reword the placeholder to look less credential-shaped
    (e.g., REDACTED-token-style-here), or append the marker
    "# secret-scan: allow-this-line" to the matched line and recommit.
  - DO NOT use --no-verify. The hook exists so the safe path is the cheap path.
EOF
    exit 1
fi

exit 0
