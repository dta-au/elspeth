#!/bin/bash
C=.claude/worktrees/protocol-landcheck
OUT=.claude/worktrees/residue-20261025/.claude/lanes/residue/landcheck
SEL="tests/unit/web/composer/test_continuation_attachment_history.py tests/unit/web/sessions/test_continuation_attachment_result.py tests/unit/web/sessions/test_continuation_prompt_producers.py tests/unit/web/sessions/test_continuation_message_families.py tests/unit/web/sessions/test_continuation_attachment_application.py tests/unit/web/sessions/test_continuation_p4_application_store.py tests/unit/web/sessions/test_continuation_primary_result.py tests/unit/web/sessions/test_continuation_cold_restore.py tests/unit/web/sessions/test_continuation_runtime_provider_history.py tests/unit/web/composer/test_continuation_prompt_producers.py"
for c in "$@"; do
  git -C $C checkout -q --detach $c || { echo "$c checkout-failed" >> $OUT/exits.txt; continue; }
  cd $C; export PYTHONPATH=$C/src:$C/elspeth-lints/src
  .venv/bin/python -m pytest -o "pythonpath=$C/src $C/elspeth-lints/src" -n 4 -q -p no:cacheprovider --junitxml=$OUT/$c.xml $SEL > $OUT/$c.log 2>&1
  echo "$c exit=$?" >> $OUT/exits.txt
done
touch $OUT/done
