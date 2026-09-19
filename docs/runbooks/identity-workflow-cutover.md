# Identity workflow cutover: operator handoff

Use this handoff for a pre-1.0 release that recreates the Sessions store after
identity administration has been used. The database owner performs the
destructive archive/drop/recreate action during a stopped-service window. This
document is a procedure and a record template, not evidence that a cutover has
occurred. Complete it against the final candidate commit after the integrated
identity tests and release gates have finished. The current development base
has Sessions epoch 60 and Landscape epoch 42; measure the final candidate
values instead of copying those numbers into the operator record.

## Before stopping the service

Record the exact candidate SHA, package version, both schema epoch constants,
the installed Sessions and Landscape epochs, and whether the candidate's signed
Landscape export includes `compartment_id`. Read the constants from
`src/elspeth/web/sessions/models.py` and `src/elspeth/core/landscape/schema.py`
at that SHA and the installed values with `elspeth doctor deployment --json`
(or the platform-specific doctor). Confirm the deployed configuration's
`workflow_governance`, `compartment_id`, registration mode, token/storage quota
defaults, and the approved identity cohort. Record the database owner's archive
location and how the former audit exports will remain accessible. Do not include
secrets or raw IdP claims in this record.

Use the [session DB reset runbook](staging-session-db-recreation.md) for the
pre-1.0 recreate rule and the [identity provider guide](../guides/identity-providers.md)
for first-admin bootstrap. The old image cannot reopen a recreated store whose
epoch or contract has changed. Keep traffic drained and repair the candidate
forward if initialization or admission fails. There is no in-place VM rebuild.

## Archive and export before dropping the old store

Stop all writers. Archive both stores and their required evidence under the
database owner's access controls. Export from the **stopped live Sessions
store**, before dropping it. Keep the three CSVs beside the archive with the
same access controls. The archive preserves historical IDs; the CSVs give the
operator the identities and unrevoked grants/edges to consider restoring.
Before export, measure both stored provider/kind mismatches on the stopped
store. Run `SELECT identity_id, provider, kind, access_state FROM identities
WHERE (provider = 'service' AND kind <> 'service') OR (provider <> 'service'
AND kind = 'service') ORDER BY identity_id;` and record every returned row in
the compatibility record. Exclude mismatched rows and their grants from
re-admission; use the archive for investigation and a separately approved
repair. A historical `provider='service', kind='human'` row with an active,
unscoped `admin` grant can count as the sole human administrator and make
operator bootstrap refuse before the recreate. Resolve that lockout with the
database owner before enabling governance; after recreation appoint a valid
human bootstrap identity rather than restoring the malformed row. A
browser-provider row with `kind='service'` is also excluded even if it has a
workload grant. The current human pre-provision path refuses the first pairing,
but old stores may contain either direction.
Select only the columns below. The one-time mapping **does** retain the
administrator-entered `username` and `organisation_id` so those values can be
restored. Keep it beside the protected database archive. Do not export raw IdP
claims, email, display name, or `subject_email_at_first_seen`. An expired but
unrevoked grant is exported so its lapsed outcome can be recorded. A revoked
grant or edge stays only in the
archive. Empty edge output is valid; an empty identity or grant cohort requires
an explicit first-install or no-grant decision in the compatibility record.

For SQLite, the stopped-service reset procedure sets `DB_PATH` and
`SNAPSHOT_DIR`. Run this in a separate operator shell while its export pause is
waiting, supplying those resolved values. SQLite timestamps in this store are
UTC naive text; the SQL emits the UTC `Z` form expected by the admin requests.

```bash
set -euo pipefail
umask 077
: "${DB_PATH:?resolved path of the stopped live Sessions store}"
: "${SNAPSHOT_DIR:?directory holding the archived Sessions store}"
sudo install -m 0600 /dev/null "$SNAPSHOT_DIR/identity-mapping.pre.csv"
sudo install -m 0600 /dev/null "$SNAPSHOT_DIR/identity-grants.pre.csv"
sudo install -m 0600 /dev/null "$SNAPSHOT_DIR/identity-relationships.pre.csv"
sqlite3 -header -csv "$DB_PATH" "
  SELECT provider, subject, identity_id AS pre_cutover_identity_id,
         username, organisation_id, kind, access_state
  FROM identities ORDER BY provider, subject
" | sudo tee "$SNAPSHOT_DIR/identity-mapping.pre.csv" >/dev/null
sqlite3 -header -csv "$DB_PATH" "
  SELECT i.provider, i.subject, r.identity_id AS pre_cutover_identity_id,
         r.role, r.scope, replace(r.expires_at, ' ', 'T') || 'Z' AS expires_at
  FROM identity_roles AS r JOIN identities AS i ON i.identity_id = r.identity_id
  WHERE r.revoked_at IS NULL
  ORDER BY i.provider, i.subject, r.role, r.scope
" | sudo tee "$SNAPSHOT_DIR/identity-grants.pre.csv" >/dev/null
sqlite3 -header -csv "$DB_PATH" "
  SELECT f.provider AS from_provider, f.subject AS from_subject,
         r.from_identity_id AS from_pre_cutover_identity_id,
         t.provider AS to_provider, t.subject AS to_subject,
         r.to_identity_id AS to_pre_cutover_identity_id, r.relationship_type,
         replace(r.effective_from, ' ', 'T') || 'Z' AS effective_from,
         replace(r.effective_until, ' ', 'T') || 'Z' AS effective_until
  FROM identity_relationships AS r
  JOIN identities AS f ON f.identity_id = r.from_identity_id
  JOIN identities AS t ON t.identity_id = r.to_identity_id
  WHERE r.revoked_at IS NULL
  ORDER BY t.provider, t.subject, f.provider, f.subject
" | sudo tee "$SNAPSHOT_DIR/identity-relationships.pre.csv" >/dev/null
sudo test -f "$SNAPSHOT_DIR/identity-mapping.pre.csv"
sudo test -f "$SNAPSHOT_DIR/identity-grants.pre.csv"
sudo test -f "$SNAPSHOT_DIR/identity-relationships.pre.csv"
sudo touch "$SNAPSHOT_DIR/identity-export.complete"
```

For PostgreSQL, the database owner runs `psql` on the host holding the archive.
`SCHEMA_OWNER_SESSION_URL` must reach the stopped Sessions database; keep it in
the operator's private environment and away from the service configuration.
Choose an `ARCHIVE_DIR` owned by that operator. These are client-side `\copy`
exports, so the CSVs are written on that host. The UTC conversion matters when
the stored timestamp originally had a non-UTC offset.

```bash
set -euo pipefail
umask 077
: "${ARCHIVE_DIR:?directory holding the database archive for this window}"
: "${SCHEMA_OWNER_SESSION_URL:?schema-owner URL of the stopped Sessions store}"
psql "$SCHEMA_OWNER_SESSION_URL" --no-psqlrc --set=ON_ERROR_STOP=1 \
  --command "\\copy (SELECT provider, subject, identity_id AS pre_cutover_identity_id, username, organisation_id, kind, access_state FROM identities ORDER BY provider, subject) TO '$ARCHIVE_DIR/identity-mapping.pre.csv' CSV HEADER" \
  --command "\\copy (SELECT i.provider, i.subject, r.identity_id AS pre_cutover_identity_id, r.role, r.scope, to_char(r.expires_at AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS expires_at FROM identity_roles AS r JOIN identities AS i ON i.identity_id = r.identity_id WHERE r.revoked_at IS NULL ORDER BY i.provider, i.subject, r.role, r.scope) TO '$ARCHIVE_DIR/identity-grants.pre.csv' CSV HEADER" \
  --command "\\copy (SELECT f.provider AS from_provider, f.subject AS from_subject, r.from_identity_id AS from_pre_cutover_identity_id, t.provider AS to_provider, t.subject AS to_subject, r.to_identity_id AS to_pre_cutover_identity_id, r.relationship_type, to_char(r.effective_from AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS effective_from, to_char(r.effective_until AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"') AS effective_until FROM identity_relationships AS r JOIN identities AS f ON f.identity_id = r.from_identity_id JOIN identities AS t ON t.identity_id = r.to_identity_id WHERE r.revoked_at IS NULL ORDER BY t.provider, t.subject, f.provider, f.subject) TO '$ARCHIVE_DIR/identity-relationships.pre.csv' CSV HEADER"
test -f "$ARCHIVE_DIR/identity-mapping.pre.csv"
test -f "$ARCHIVE_DIR/identity-grants.pre.csv"
test -f "$ARCHIVE_DIR/identity-relationships.pre.csv"
touch "$ARCHIVE_DIR/identity-export.complete"
```

Compare the CSV headers and row totals with read-only queries on the stopped
store before dropping it. In particular, count **all** `identities`, all
`identity_roles WHERE revoked_at IS NULL`, and all
`identity_relationships WHERE revoked_at IS NULL`; a header-only CSV means
zero rows, not a failed export. SQLite emits a zero-byte file for a zero-row
query. The `identity-export.complete` marker is written only after the three
commands exit successfully; it does not replace the row-total comparison.
Preserve the archive and CSVs according to the
deployment's evidence-retention decision. The pre-window export, not a later
read of the archive, is the working input for re-admission.

## Recreate and re-admit within the window

The database owner drops/recreates both stale stores and initializes them with
the final candidate's schema-owner credentials. Verify both installed epochs
and the doctor's structural and semantic checks before starting ordinary
traffic. A recreated Sessions store has no old identities or session-token
subjects. Every old bearer session fails its next authenticate/refresh and the
user must sign in again. Encrypted Composer secrets, sessions/chat, workflow
requests, library rows and run history in the old stores do not return; the
archive and pre-window signed exports remain evidence.

Restore using the current authenticated administration API; do not insert
identity, role, relationship or quota rows with ad-hoc SQL:

1. Bootstrap a consistent human administrator whose exported identity was
   active and had an unscoped, unexpired `admin` grant. Use `elspeth composer
   users bootstrap-admin PROVIDER SUBJECT --username USERNAME
   --organisation-id ORGANISATION_ID --note TEXT` and the approved quota
   defaults. Supply the exported `username` exactly; omit
   `--organisation-id` when the CSV cell is empty. Record the new
   `identity_id` and bootstrap grant's `role_id` from
   `GET /api/auth/admin/roles?identity_id=...`. If no exported grant is
   eligible, the deployment owner must name a new appointment and record it
   as such; an expired old grant is not silently extended.
2. For each other consistent exported `kind=human, access_state=active` row,
   call `POST /api/auth/admin/identities` with its `provider`, `subject`, exact
   `username`, `organisation_id` when nonempty, `role: "none"`, and a nonempty
   restoration note. Record the returned new
   `identity_id` next to `pre_cutover_identity_id`. Compare the stored
   `username` and `organisation_id` through the admin identity view before
   restoring grants. Do not auto-activate pending, disabled, or service
   identities. Existing local accounts in
   `auth.db` still need an active Sessions identity; a password alone does not
   re-admit them.
3. Restore each unrevoked grant for a restored identity only if its
   `expires_at` is empty or still later than the current UTC instant. Use
   `POST /api/auth/admin/roles` with the new `identity_id`, exact `role`,
   `scope`, `expires_at` and a note. Omit empty `scope`/`expires_at`; never
   replace an expiry with a later one. Record the returned `role_id`. Mark a
   grant that expires before or during the call `lapsed` and verify no new row
   was created. The bootstrap `admin` grant is already restored. If that
   bootstrap changed an expiring grant into a permanent one, record the
   exception and use a second active administrator to revoke/regrant with the
   original expiry when possible. Respect the last-admin protection and the
   `admin` versus workload-role exclusion; record any deliberate new
   appointment or non-restoration.
4. Restore each unrevoked `approver` relationship only if both endpoints were
   restored and its `from` identity still has an active `approver` role. Use
   `POST /api/auth/admin/relationships` with new endpoint IDs, the exported
   `relationship_type`, `effective_from`, `effective_until`, and a note.
   Record the returned `relationship_id`; otherwise mark `not_restored`.
   The time window is an annotation, and a default-approver edge supplies a
   suggestion and scoped audit/curator reachability. It does **not** limit
   approval decisions: any active non-author identity with the `approver`
   role may inspect and decide an open request. The addressed approver sorts
   first in the inbox.
5. Save `identity-mapping.<window>.csv` with the new ID (empty for an
   excluded row); `identity-grants.<window>.csv` with `restored_role_id` and
   `bootstrap`, `restored`, `lapsed`, or `not_restored`; and
   `identity-relationships.<window>.csv` with `restored_relationship_id` and
   `restored` or `not_restored`. Keep them beside the pre-window CSVs. List
   identities, grants and edges through the admin API and compare ID,
   `username`, `organisation_id`, scope, expiry and window values. Confirm an
   admitted identity can sign in and
   that chargeable admission gives the intended quota result.

With `workflow_governance=on`, require readiness to pass with a configured
`compartment_id`, closed registration for local auth, and at least one active
non-author approver for the intended workflow. Recreated compositions need
fresh approvals; a later rejection for a state retires earlier approvals for
that state and blocks its run. Verify one representative request, inspected
state, decision and run admission before reopening ordinary traffic. A signed
Landscape export from the candidate must include `compartment_id`; record the
export version and verifier outcome in the compatibility record. The
compartment-ingress proof includes chat-pasted text when it creates a
composition state. A reviewer attestation requires an open exact-state review
request for that reviewer; recreate those requests rather than carrying over
old attestations. No old approval, review request, attestation, or library
entry is copied into the new store.

## Deployment shape

| Shape | Schema-owner action | Candidate and cohort handoff |
| --- | --- | --- |
| ECS/Fargate, PostgreSQL | Stop old tasks; archive/export, drop and recreate both databases; run the schema-owner `doctor aws-ecs --init-schema` task. | Use the [ECS schema compatibility gate](aws-ecs-deployment.md#3-apply-the-schema-compatibility-gate) for a release cutover. The [ordinary ECS redeploy](aws-ecs-existing-service-redeploy.md) assumes current schemas. Bootstrap the admin, restore the cohort, then enable traffic. |
| Azure Container Apps, PostgreSQL | Drain old revision; archive/export, drop and recreate both databases; rerun the [cold-install schema Job](azure-container-apps-cold-install.md). | Apply the candidate revision only after schema initialization. The [ordinary ACA redeploy](azure-container-apps-existing-service-redeploy.md) assumes current schemas. Restore the cohort before its authenticated check and traffic handback. |
| VM, SQLite | Stop service; archive the Sessions and Landscape file/sidecar sets and export the cohort; recreate both stale files. | Use the [SQLite reset](staging-session-db-recreation.md#staging-reset-for-elspethexamplegovau) and [VM upgrade](ansible-ubuntu-deployment.md). Restore the cohort before traffic. |
| VM, external PostgreSQL | Stop service; archive/export, drop and recreate both databases; run `elspeth doctor deployment --init-schema` with schema-owner URLs. | Return to least-privileged runtime URLs, run the [VM external-state doctor](ansible-ubuntu-deployment.md), and restore the cohort before traffic. |

Kubernetes follows the separate K8 deployment work after this handoff; do not
infer a Kubernetes cutover from one of these four procedures.

## Compatibility record and notice

Fill this ordinary operator record for the actual deployment window. Store it
with the database archive and completed mapping files; it is not a signed plan
or a substitute for the product's signed audit export.

| Field | Recorded value |
| --- | --- |
| Deployment, window and release | `<deployment>; <start/end UTC>; <version>` |
| Candidate and predecessor | `<exact candidate SHA/image digest>; <predecessor SHA/image digest>` |
| Candidate Sessions / Landscape epochs | `<measured at candidate SHA>; <measured at candidate SHA>` |
| Installed predecessor epochs and schema status | `<doctor output reference and measured values>` |
| Archive/export decision and location | `<both stores, three CSVs, access/retention decision>` |
| Recreated store and initialization result | `<database owner action; terminal doctor result>` |
| Identity, grant and edge outcomes | `<completed CSV paths; excluded/lapsed/new appointments>` |
| Provider/kind consistency | `<both mismatch directions measured; any sole-admin recovery decision>` |
| Governance and quota check | `<readiness; approver; sample admission/refusal>` |
| Signed export and ingress compatibility | `<format/version; compartment_id present; chat-paste ingress evidence; verifier result>` |
| Rollback disposition | `old code forbidden on recreated stores; repair candidate forward` |
| Notice and traffic handback | `<actual notice channel/time; operator's readiness and cohort decision>` |

Send the notice only after the listed cohort has actually been re-admitted.
Address it to the cohort and adapt the excluded-account sentence to the
deployment's support channel. Do not send a claim that everyone was activated
when the completed mapping shows otherwise.

```text
Subject: ELSPETH <deployment> database recreation — <date and window>

ELSPETH was unavailable while its Sessions and Landscape databases were
recreated for <release>. The active accounts listed for this window have been
re-activated by an administrator. All previous sessions are invalid; please
sign in again. If you see “Account is pending”, contact <admin contact> for
activation.

Stored Composer provider secrets must be entered again before a pipeline can
use them. Composer sessions and chat, run history, workflow approvals, review
requests and attestations, and the shared library start empty. Pre-window signed
exports remain evidence; the operator retains the identity mapping that
relates their old identity IDs to newly admitted accounts. Uploaded payload
files remain in storage, although old session links to them do not return.
```
