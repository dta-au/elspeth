#!/usr/bin/env bash
# timeit.sh LABEL WORKLOAD REPS  -> appends "label workload user sys real exit" lines to timings.txt
L=.claude/lanes/exec-perf; label=$1; w=$2; reps=${3:-2}
for i in $(seq 1 "$reps"); do
  find "$L/$w/runs" "$L/$w/output" -type f -delete
  TIMEFORMAT="%U %S %R"
  t=$( { time elspeth run --settings "$L/$w/settings.yaml" --execute > "$L/$w/time.log" 2>&1; echo "exit=$?" > "$L/$w/time.exit"; } 2>&1 )
  echo "$label $w $t $(cat "$L/$w/time.exit") $(cat "$L/$w"/output/* | sha256sum | cut -c1-16)" | tee -a "$L/timings.txt"
done
