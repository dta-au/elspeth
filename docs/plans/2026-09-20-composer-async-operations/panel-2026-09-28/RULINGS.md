# Owner rulings on the 2026-09-28 storage panel

John, 2026-09-28, on `RECOMMENDATION.md` (B′: separate `composer_async_operations` table, shared request
normaliser pinned by a golden vector with C as fallback). These rulings bind the plan re-base.

| # | Question | Ruling |
|---|---|---|
| 1 | Identity | **Yes.** The job's `operation_id` IS the `message_ingress_receipts` key. Ingress stays the immutable acceptance record, written by the worker in the same transaction as the user row. The composite FK runs from ingress to the job. The `message_already_accepted` / `message_idempotency_conflict` 409 arms and the SPA's transcript-matching recovery are deleted at cutover: one wire name, no alias, no dual acceptance. |
| 2 | Positive fence predicate (amends F-B2/C2) | **Yes.** If any job row is bound to this fence triple, the write requires that job to be `running` with `cancel_requested_at IS NULL`, unless the write is `audit_only`. T06 first inventories every legitimate write under the same SOL after the terminal CAS, and ships a negative control for writes after the terminal. |
| 3 | Compose events table | **No**, per the panel. Forensic columns (`claim_owner_instance_id`, `attempt`, `settled_by`) stay on the terminal row. |
| 4 | Retention of settled rows | **Approved as proposed.** The delete guard applies while the session lives, and settled rows are retained with their session. |
| 5 | Head moves between admission and start | **Refuse** — endorsed by John 2026-09-28. Detail below. |

## Ruling 5 — refuse a moved base before any side effect (endorsed)

- **Question.** Should the job record a base state that moved, or refuse it? This is a question about
  auditing operators (authorisation in context) rather than workflows (lineage). A lineage record would be
  complete either way. Only a refusal keeps "every compose turn's base is the state its operator saw"
  true, which matches the existing rules:
  - `stale_compose_state` is raised when the head moves mid-turn;
  - a stale proposal draft or anchor is answered with 409;
  - recompose's `expected_user_message_id` guards against transcript drift.
- **Binding.** The send's `state_id` (the head the user saw; absent means "no state") and a new
  recompose `state_id` are both part of the request hash.
- **Check.** Under the COMPOSE SOL, before the user row is inserted, the worker compares the current
  head with the bound base.
- **Outcome on mismatch.** A terminal 409 with today's `stale_compose_state` body. No user row, no
  provider call. The SPA shows today's stale copy and keeps the body in custody, so the user can
  resend it.
- **SPA guard.** While a same-tab send is queued, proposal Accept/Reject is disabled.
- **Supersedes D9.** A foreign `state_id` stays a 404. The visible change is that a stale `state_id`
  no longer silently composes against the current head.

## Rulings 6–7 (John, 2026-09-28, on the re-survey's open questions)

| # | Question | Ruling |
|---|---|---|
| 6 | The single wire name for the send id (ruling 1) | **`operation_id`** everywhere: the request DTOs, the 202 acknowledgement, the poll URL, the transcript field (`ChatMessageResponse.client_request_id` → `operation_id`) and the ingress column (renamed in the epoch-72 cut). No alias. Inside the job row and code, the server-minted fence id is always `session_operation_id` (glossary rule). |
| 7 | Absent `state_id` (ruling 5) | **Literal: absent means "no state".** A send or recompose without `state_id` to a session that has a head is a terminal 409 `stale_compose_state` before any side effect. Every caller that composes on an existing pipeline states the base it saw. Tests, the ACA observation probe and `acceptance.sh` migrate at cutover; the eval battery drives fresh sessions and is unaffected. The SPA reloads authoritative state after every non-success terminal, so refusals stay rare. |
