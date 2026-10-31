#!/bin/bash
# Landing-rule check: run the backend selection at the base and each backend commit.
C=.claude/worktrees/protocol-landcheck
OUT=.claude/worktrees/protocol-20261025/.claude/lanes/protocol/landcheck
SEL="tests/unit/web/sessions/routes tests/unit/web/test_composer_stream.py tests/unit/web/composer/test_continuation_control.py tests/unit/web/composer/test_error_code_redaction.py tests/unit/web/sessions/test_schemas.py tests/unit/web/test_app.py tests/unit/scripts/test_check_contracts.py tests/unit/evals/composer_battery/test_drive_battery.py"
for c in "$@"; do
  git -C $C checkout -q --detach $c || { echo "$c checkout-failed" >> $OUT/exits.txt; continue; }
  cd $C
  export PYTHONPATH=$C/src:$C/elspeth-lints/src
  sel=""; for t in $SEL; do [ -e "$t" ] && sel="$sel $t"; done
  .venv/bin/python -m pytest -o "pythonpath=$C/src $C/elspeth-lints/src" -n 4 -q -p no:cacheprovider --junitxml=$OUT/$c.xml $sel > $OUT/$c.log 2>&1
  echo "$c exit=$?" >> $OUT/exits.txt
done
touch $OUT/done
