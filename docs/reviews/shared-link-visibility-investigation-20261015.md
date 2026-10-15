# Shared-link visibility: quick infrastructure inspection

Date: 2026-10-15. Read-only source/config review; no reproduction or live-deployment conclusion.

**Result:** no obvious share-route or proxy wiring defect found in the inspected source. A generated review link is deliberately usable by another authenticated user, independently of the shared-library switch. A copied ordinary session URL is deliberately private. The original throwaway report does not identify the URL, error, or deployment, so its cause is unconfirmed; it does not establish a release blocker.

Scope: inspected `.claude/worktrees/release086-email-bugs-20261014` at HEAD `8ab3eae594e05b704a91615cd6ec11b586e17bae`, including its current files. References below are relative to that source tree. The phrase “graph repository off” is interpreted provisionally as the shared library / workflow-governance setting, whose actual configuration field is `workflow_governance`.

## Confidence Assessment

**Overall Confidence:** High for the source contracts; Insufficient Data for the reported incident's cause.

| Finding | Confidence | Evidence and implication |
|---|---|---|
| Library off does not switch off generated review links | High | `src/elspeth/web/config.py:639` defaults `workflow_governance` to `off`; library endpoints gate it in `src/elspeth/web/sessions/routes/workflow/library.py:174`. The share router is included unconditionally at `src/elspeth/web/app.py:2367`; its recipient handler, `src/elspeth/web/shareable_reviews/routes.py:151`, has no governance dependency. |
| The recipient must authenticate, but need not own the session | High | `src/elspeth/web/shareable_reviews/routes.py:159` depends on `get_current_user`; lines 174–176 resolve the capability without calling `verify_session_ownership`. The existing cross-user test explicitly mints as Alice and resolves as Bob: `tests/integration/web/test_shareable_reviews_routes.py:421`. This test was read, not executed. |
| An ordinary Composer/session URL is not a sharing grant | High | `src/elspeth/web/sessions/ownership.py:49` refuses archived, different-owner, or different-auth-provider sessions with the same 404. Turning the library off does not relax that predicate. |
| Generated links inherit the sender's browser origin | High | `src/elspeth/web/shareable_reviews/service.py:105` supplies `/#/shared/`; `src/elspeth/web/frontend/src/components/composer/SaveForReviewDialog.tsx:35` prepends `window.location.origin`. An internal, loopback, alternate-host, or different-environment origin therefore travels with the copied link. This is a deployment/usage hypothesis, not evidence that it happened. |
| Access tokens are browser-origin credentials, not normal session cookies | High | `src/elspeth/web/frontend/src/api/client.ts:79` reads `auth_token` from localStorage and lines 89–94 attach it as a Bearer header. `src/elspeth/web/auth/middleware.py:93` requires Authorization. A login on a different hostname/origin does not authenticate this origin. |
| The supplied proxy and persistence templates have the expected wiring | High for templates only | `deploy/compose/nginx.conf:25` proxies all paths, forwards Host and scheme, and does not strip Authorization. Compose wires a stable required signing key and payload path at `deploy/compose/web-postgres.yaml:8`, mounts the named state volume at line 72, and declares one replica at line 78. Azure wires the payload path at `deploy/azure-container-apps/workload.bicep:322`, the signing secret at line 352, and the shared NFS volume at line 377. AWS wires the shared EFS mount at `deploy/aws-ecs/terraform/modules/scenario/ecs.tf:41` and the signing secret reference at `deploy/aws-ecs/terraform/modules/scenario/locals.tf:558`. These are repository templates, not verified deployed values. |
| A backend authentication 401 can be presented as an invalid share link | High | `src/elspeth/web/frontend/src/components/shared/SharedInspectView.tsx:148` maps every 401 to “link may have expired or been revoked.” `src/elspeth/web/frontend/src/api/shareableReviews.ts:215` suppresses automatic logout for this request. If a stored login becomes invalid after the UI admits the view, its auth refusal can consequently resemble a bad share token. Incident relevance is unknown. |

Likely diagnostic order, not a measured probability ranking:

1. Ordinary session URL copied instead of the generated `/#/shared/{token}` link: intentional cross-user refusal.
2. Recipient not authenticated on the link's exact origin, or unable to access that origin: login/auth/deployment issue. `src/elspeth/web/frontend/src/App.tsx:701` wraps the shared view in AuthGuard, which preserves the hash through login (`components/common/AuthGuard.tsx:43`).
3. Expired token or changed/different deployment signing key: recipient gets 401 “Invalid or expired share token” (`shareable_reviews/routes.py:179`). The default lifetime is 30 days (`config.py:721`); rotating the key invalidates existing links (`config.py:685`).
4. Missing snapshot blob because of retention, replaced/unshared storage, or a different environment: recipient gets 404 “Shared snapshot is no longer available” (`shareable_reviews/routes.py:180`). Resolve reads the authenticated frozen payload, not the sender's live session (`shareable_reviews/service.py:618`). A replica with a different key or payload volume could produce user-dependent/intermittent failures; the supplied shared-storage templates do not demonstrate such a mismatch.

The existing source has positive and negative authorization cases: different authenticated recipient (line 421), anonymous recipient (444), tampered token (452), expired token (463), and missing blob (518) in `tests/integration/web/test_shareable_reviews_routes.py`. Their presence is not execution evidence.

## Risk Assessment

**Implementation Risk:** High if “fixing sharing” by relaxing session ownership or removing recipient authentication; Low for a future diagnostic-message refinement. **Reversibility:** Easy for UI changes; access disclosure cannot be reversed.

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| Mistaking intended session privacy for a defect and granting cross-user session access | High | Unknown | Keep owner-only routes and the separate authenticated capability path. |
| Changing a feature flag or deployment without knowing the affected environment | Medium | Unknown | No infrastructure change recommended from this report. |
| Blaming the share token for expired recipient credentials | Low | Source permits it | If the report recurs with this symptom, distinguish auth refusal from token refusal while preserving both controls. |

## Information Gaps

The exact link type, recipient error, affected deployment/image, and deployed signing-key/storage/origin configuration are unknown. They are not prerequisites for completing this requested quick inspection. No live environment, database, secret, or cloud state was inspected, and no particular deployment is assumed affected.

## Caveats and Required Follow-ups

No source fix is justified by this report alone. If the issue recurs, the smallest useful reproduction is: on one deployment with governance off, Alice generates a review link; authenticated Bob opens it successfully; anonymous Bob must be sent to login, and Bob opening Alice's ordinary session URL must still be refused. Expired/tampered-token and missing-payload controls must retain their refusals. Inspect only nonsecret configuration selectors for origin, shared payload mount, revision, and secret-reference identity if the two-user path fails. Do not read or compare secret values in agent output.

Checks performed: source/config reads, reference searches, and a read-only committed comparison of the share service/routes, shared frontend view, AuthGuard, share API client, and Compose proxy between main SHA `9d7735c094eab212549868afeaf6019272cf5c57` and candidate HEAD. `git diff --name-only <main-sha> HEAD -- <those-paths>` returned empty output with exit 0: those selected committed paths have no change. This does not establish deployed parity or cover every dependency/uncommitted file.

Report-path admission: `git check-ignore -v docs/reviews/shared-link-visibility-investigation-20261015.md` returned no output, exit 1. No tests, application imports, HTTP/provider calls, database queries, branch changes, or source/test edits were performed. Only this report was written; no change to the ongoing release validation was requested.
