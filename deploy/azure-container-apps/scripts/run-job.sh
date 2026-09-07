#!/usr/bin/env bash
# Wait on exactly the execution we start, never a historical successful run.
set -Eeuo pipefail
test "$#" -eq 2 || { echo 'usage: run-job.sh RESOURCE_GROUP JOB_NAME' >&2; exit 2; }
execution=$(az containerapp job start --resource-group "$1" --name "$2" --query name --output tsv)
test -n "$execution"
deadline=$((SECONDS + 2100))
while (( SECONDS < deadline )); do
  status=$(az containerapp job execution show --resource-group "$1" --name "$2" \
    --job-execution-name "$execution" --query properties.status --output tsv)
  case "$status" in
    Succeeded) exit 0 ;;
    Failed|Stopped|Degraded) echo 'Job execution failed; inspect its protected logs' >&2; exit 1 ;;
    Running|Processing|Pending|Unknown) sleep 5 ;;
    *) echo 'Unexpected Job execution status; stop and inspect the execution' >&2; exit 1 ;;
  esac
done
echo 'Job execution timed out; inspect or stop this execution before retrying' >&2
exit 1
