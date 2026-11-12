#!/usr/bin/env bash
# Start one isolated local composer instance with the litellm capture hook.
#
#   evals/composer-live-audit/instance.sh <port> <name> [--core]
#
# State lives under evals/composer-live-audit/runs/instances/<name>/ (gitignored via runs/).
# --core keeps the DEFAULT plugin allowlist (required web core only); otherwise a broad
# allowlist is set so the composer can use the full installed transform set.
# Requires: .venv, OPENROUTER_API_KEY, ELSPETH_WEB__COMPOSER_* settings in the environment
# (see docs/guides/web-local-development.md). Prompt guard runs in debug-when-unconfigured
# mode, so this is a development harness, not a production configuration.
set -euo pipefail
PORT=$1
NAME=$2
MODE=${3:-}
ROOT=$(git rev-parse --show-toplevel)
HERE="$ROOT/evals/composer-live-audit"
INST="$HERE/runs/instances/$NAME"
cd "$ROOT"
mkdir -p "$INST/data/blobs" "$INST/data/outputs" "$INST/capture"

if [ ! -f "$INST/credentials.json" ]; then
  PW=$(openssl rand -hex 12)
  (umask 077 && printf '{"username":"audit","password":"%s"}' "$PW" > "$INST/credentials.json")
  .venv/bin/elspeth composer users add audit --password "$PW" --display-name Audit --data-dir "$INST/data" >/dev/null
fi

export AUDIT_PORT=$PORT ELSPETH_WEB__DATA_DIR="$INST/data" AUDIT_CAPTURE_DIR="$INST/capture"
export ELSPETH_WEB__COMPOSER_PROMPT_GUARD_MODE=debug-when-unconfigured
if [ "$MODE" != "--core" ]; then
  export ELSPETH_WEB__PLUGIN_ALLOWLIST='["source:blob_rows","source:null","source:llm","transform:batch_classifier_metrics","transform:batch_data_quality_report","transform:batch_distribution_profile","transform:batch_drift_compare","transform:batch_duplicate_annotator","transform:batch_effect_size","transform:batch_experiment_compare","transform:batch_outlier_annotator","transform:batch_paired_preference","transform:batch_rank","transform:batch_replicate","transform:batch_stats","transform:batch_threshold_summary","transform:batch_top_k","transform:blob_csv_expand","transform:blob_fetch","transform:blob_json_expand","transform:blob_text_expand","transform:json_explode","transform:keyword_filter","transform:line_explode","transform:passthrough","transform:reference_join","transform:report_assemble","transform:truncate","transform:type_coerce","transform:value_transform","sink:document"]'
fi
export ELSPETH_WEB__LLM_PROFILES=${ELSPETH_WEB__LLM_PROFILES:-'{"standard":{"provider":"openrouter","model":"deepseek/deepseek-v4.1-flash","credential_scope":"server","credential_ref":"OPENROUTER_API_KEY"}}'}
export ELSPETH_WEB__DEFAULT_LLM_PROFILE=${ELSPETH_WEB__DEFAULT_LLM_PROFILE:-standard}

nohup .venv/bin/python "$HERE/launch_web.py" > "$INST/web.log" 2>&1 &
echo "started pid $! on port $PORT (log: $INST/web.log)"
c=000
for _ in $(seq 1 60); do
  sleep 2
  c=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/ready" || true)
  [ "$c" = "200" ] && break
done
echo "ready=$c"

# The first login creates the identity row; then grant the deployment-wide `user` role in this
# scratch session store (without it, POST /api/sessions answers 403 user_role_required).
curl -s -X POST "http://127.0.0.1:$PORT/api/auth/login" -H 'content-type: application/json' -d @"$INST/credentials.json" >/dev/null
.venv/bin/python - "$INST/data/sessions.db" <<'PY'
import sys

sys.path.insert(0, ".")
from sqlalchemy import create_engine, text

from tests.fixtures.identities import grant_test_pipeline_user

engine = create_engine(f"sqlite:///{sys.argv[1]}")
with engine.begin() as conn:
    for (identity_id,) in conn.execute(text("select identity_id from identities")).fetchall():
        grant_test_pipeline_user(conn, identity_id=identity_id)
PY
curl -s "http://127.0.0.1:$PORT/api/system/status" | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); print('composer_available', d['composer_available'], d.get('composer_reason'))"
