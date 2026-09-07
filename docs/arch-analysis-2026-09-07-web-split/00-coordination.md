# Web ownership split investigation

Date: 2026-09-07. Starting commit: `6623010fda6bdf395d2f5d659d0aa5feb5d382cd` on `release/0.8.0`.

The business objective is to transfer web UX workload to a specialist team. The current team retains the engine, proposed compiler, and Composer behind an API. This is a focused feasibility investigation, not an implementation or a whole-system architecture audit.

Parallel source review covers frontend/API coupling and backend/compiler responsibilities. The coordinator checks build and CI ownership and synthesizes the recommendation. An independent reviewer will check the resulting assessment against source evidence.

No production changes, tracker mutations, branch switches, or test suites are in scope. The existing modification to `.agents/skills/filigree-workflow/SKILL.md` belongs to other work.

The Loomweave index reported `never_analyzed`, with its prior run failed. A refresh was started. Until it succeeds, current source supplies the evidence; no stale entity counts or dependency results are authoritative.

At closeout, HEAD had advanced to `a1451bf6363a0c748419f8ce2c9cd11ad0cbb168`. Git comparison showed only the unrelated Filigree skill edit; the investigated product files were unchanged. The investigation's incomplete code-map refresh was cancelled after source review, leaving no background analysis running for this task.

Independent source review checked the assessment and ownership diagram. Corrections clarified both current configuration-loading paths, strengthened client capability citations, and removed misleading runtime-sequence arrows from the ownership diagram. The reviewer confirmed the revised assessment. No runtime validation was performed.

The follow-up request adds an exact file allocation. Frontend, backend and supporting build/test/docs reviews supplied candidate exceptions. Git reconciliation assigns all 5,302 tracked paths once; retained defaults and representative directory reviews are explicit. Tutorial fixture duplication was checked and rejected: the Python gate requires frontend copies to be absent. The final allocation includes the browser-title/backend-cleanup and curated-label counterparts discovered across reviews.
