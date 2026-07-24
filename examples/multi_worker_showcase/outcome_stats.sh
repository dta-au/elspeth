#!/usr/bin/env bash
# Report completed terminal outcomes for one exact run as total|success|failure.
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "usage: $0 <audit-db> <run-id>" >&2
    exit 2
fi

DB="$1"
RUN_ID="$2"

case "$RUN_ID" in
    *[![:alnum:]_-]*)
        echo "invalid run id" >&2
        exit 2
        ;;
esac

sqlite3 "file:${DB}?mode=ro" \
    "PRAGMA query_only=ON; SELECT COUNT(*), COALESCE(SUM(outcome='success'), 0), COALESCE(SUM(outcome='failure'), 0) FROM token_outcomes WHERE run_id='$RUN_ID' AND completed=1;"
