# Remote recovery of Composer remediation evidence

This lane was explicitly requested for publication on 10 November 2026 after the initial checkpoint omitted local evidence. Its original captures are now tracked alongside the remediation branch. They are historical evidence, not new verification runs.

Large runtime assets use Git LFS. After fetching the branch, run `git lfs install --local` and `git lfs pull`. A checkout containing only LFS pointer text cannot run the bundled binaries. The runtime assets here are the Linux binaries from the original environment; native Windows/macOS/BSD qualification is still outstanding.

The original `combined-foundations-lints.json`, `.before.json`, `.after.json`, `.command.json`, `.exit`, `.frozen-exit` and `.stderr` are preserved. Both freeze records identify `b4064d2700bd4c9413a1cdcb794a95226681d373`. The raw lint exit is 1 and the frozen capture exit is 0. Do not regenerate these files in place or represent a new run as the original baseline. The combined review patch and final static-gate records are also preserved. Their pending lint adjudication and broad behavior verification remain pending.

`runtime-deps/pytest_plugins/composer_test_cache.py` and its real Chroma assets are included. The existing runbook's explicit plugin loading and source-root checks still apply. SQLite and Caddy binaries and Playwright browsers are included; the Terraform plugin directory was empty. Repository-wide cache and credential filename exclusions continue to apply. This lane's `.gitignore` lists additional files excluded because they contained credential-shaped strings, including conservative matches in test fixtures and historical logs. Excluded files are not available from a clone; do not cite their absence as a passing result.

The four source worktrees are reconstructed from commits already in this branch's history. From the repository root, create only missing worktrees:

```bash
git worktree add --detach .claude/worktrees/composer-remediation-landscape-metadata dd3b3524ccd2a1910db98f77eb35f9d7ce69a249
git worktree add --detach .claude/worktrees/composer-remediation-resource-identity 05bcab93955b2070ffdc90e72eddd7368502d2c1
git worktree add --detach .claude/worktrees/composer-remediation-mcp-lifecycle dc67bdd2f94bcedd6f59d29957791c5bccd58c06
git worktree add --detach .claude/worktrees/composer-remediation-park-identity dd3b3524ccd2a1910db98f77eb35f9d7ce69a249
```

Do not overwrite an existing worktree. These commands restore source revisions, not ignored content that originally lived inside those worktrees. In particular, this root lane backup is not a backup of the separate resource-identity worktree's nested lane. Its selected final report is preserved under `docs/architecture/composer-remediation/reports/`.

Historical command records and test-created symlinks retain their original `/workspace/elspeth` paths. They are evidence, not portable launch commands. Adapt runbook paths to the new checkout, recreate the virtual environment or its deliberate worktree links, and install project/frontend dependencies before testing. Never follow a historical test symlink as an authority for a new run.
