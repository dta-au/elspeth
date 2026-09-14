### Task I1: Token usage ledger and R14 daily enforcement

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: I8. Runs before: I2. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered after I0 (the nullable ledger measures and `SESSION_SCHEMA_EPOCH` 57)
and I8 (the governance switch; I1 reads none of it). I2 follows because it
consumes `RepositoryQuotaAuthority.active_policy` and `record_quota_exceeded`;
I3 follows because it consumes the `fenced_session` fixture and the
schema-version-2 admission contract. Spec: R14 (`docs/specs/2026-09-02-pluggable-sso-design.md:1161-1186`),
the `quota_exceeded` metadata rule (:834-835), the `calls` "missing measures
remain NULL" rule (:849-856), the `token_usage_ledger` row (:1422), §Testing →
Workflow governance (:1276-1310: a fire and a mutation-derivation test per
refusal, the `quota_exceeded` row carries dimension, cap, ceiling and usage, the
day rollover named at UTC midnight) and §Workflow → "complete the
Composer/run/auto-title ledger adapters and daily policy checks before replacing
R14's current `token_accounting_unavailable` refusal" (:1370-1372).

Every `path:line` below was measured on HEAD 072141b75. I0 edits only
`sessions/models.py`, `sessions/schema.py`, docs and epoch pins; I8 edits
`config.py`, `readiness.py`, `tests/unit/web/conftest.py:104-173` and docs. Neither
touches a line this task edits, so the HEAD line numbers hold. Where one file
gets several hunks, each hunk's quoted text is the Edit anchor; the line
numbers are HEAD's.

This block was executed end to end in a scratch export of HEAD with I0's two
source edits applied: every expected failure, every pass count, the manifest
fingerprints and the lint delta quoted below are the measured output of that
run, not a prediction.

Decisions this task owns:

1. **The three ledger adapters ship here.** The spec forbids replacing the
   `token_accounting_unavailable` arm before the Composer, run and auto-title
   adapters exist, because an unwritten ledger measures zero and would admit
   unbounded spend. Composer spend is charged where every Composer provider
   call becomes durable audit evidence: `SessionServiceImpl.add_messages_atomic`
   (the compose loop `_persist_llm_calls`, `routes/_helpers.py:1860`, the
   turn cohort and the planner cohort, `composer/service.py:4513`),
   `SessionServiceImpl._insert_prepared_guided_audit_rows_on_connection` (every
   guided cohort) and `RepositoryRunDiagnosticsAuditAuthority.append_audit_messages`
   (`_persist_run_diagnostics_llm_calls`, `routes/_helpers.py:2046`), each on
   the transaction that writes the audit rows, so accounting never outlives or
   precedes its evidence. Auto-title and run spend have no audit cohort and go
   through the new `SessionServiceImpl.record_token_usage` under COMPOSE and
   EXECUTE authority respectively. The run adapter reads the Landscape `calls`
   token columns (`core/landscape/schema.py:2130`).
2. **NULL is unknown, an empty day is zero.** A ledger row with a NULL
   `prompt_tokens` or `completion_tokens` makes that identity's UTC day unknown
   (`daily_token_total` returns `None`) and admission keeps refusing with
   `token_accounting_unavailable`. A day with no rows measures `0`, because every
   chargeable path above writes before the next admission reads.
3. **What is charged.** A provider call that succeeded is charged; if it
   reported no usage it is charged as unknown. A call that failed and reported
   no usage returned no completion and is not charged. A failed call that did
   report usage is charged.
4. **The bound.** Usage is `prompt_tokens + completion_tokens` for the
   identity's UTC day on the sessions database clock (`database_clock.py:54`).
   The limit is the lower of the identity policy's `tokens_per_day` (the
   evidence `cap`) and the container ceiling row's `tokens_per_day` (the
   evidence `ceiling`), whichever exist. Admission refuses when
   `usage >= limit`. A call admitted below the limit may take the day past it:
   R14 is eventually consistent on the same terms as R13 (spec :1166-1167).
5. **Run model attribution.** Landscape `calls` carry no model column, so a run
   ledger row names the calling node's configured `model`, then its
   `deployment_name`, then its `plugin_name`.
6. **The audit seam.** `auth_events` is a Landscape table; no sessions-side
   authority opens it. A refusal becomes a typed `QuotaExceeded`, and
   `SessionServiceImpl` hands it to its `quota_exceeded_recorder`, which
   `web/app.py` wires to `AuthAuditRecorder.record_quota_exceeded`. A service
   built without one gets a recorder that raises `AuditIntegrityError`, so an
   unwired refusal fails closed instead of going unaudited. A Composer or
   auto-title refusal writes no sessions row, so its audit row is written right
   after the read-only admission transaction and before the caller sees the
   decision. A run-permit refusal is audited inside the permit transaction, and
   only when that call recorded the refusal: the run facet appends the run's
   terminal `failed` event exactly once (`repository.py:1574`), so a
   terminal event present before the decision marks a replay. The run facets'
   signatures do not change, so I3 can widen them independently.
7. **Admission evidence schema version 2.** `AdmissionPolicyEvidence`
   gains `dimension`, `cap`, `ceiling` and `usage`, and its `schema_version`
   becomes `Literal[2]`. Every `admission_decision_hash` therefore changes. The
   tests that touch the hash compute it from the model rather than pinning a
   literal (`tests/unit/core/landscape/test_admission_policy_provenance.py`,
   `tests/unit/core/landscape/test_exporter.py`,
   `tests/unit/web/coordination/test_durable_run_admission.py`), and all of them
   pass unchanged in Step 4. A stored version-1 payload is refused on decode,
   never silently upgraded (Step 1 pins that).
8. **Manifest binding.** The ledger's class is `RepositoryQuotaAuthority`, with
   `record_token_usage` and `daily_token_total`, but it is not the bound symbol.
   `_NAMED_AUTHORITY_SYMBOLS` binds the symbol whose body executes the INSERT, which
   is the module function `record_token_usage_on_connection`: both the class
   method and the four adapters reach the ledger through it. It is bound to the
   existing `TablePolicy("token_usage_ledger", "session", "QuotaAuthority")`
   (`test_session_db_mutation_authority.py:121`). The policy is not widened.
9. **Churn is re-pinned, not avoided.** The new imports and methods in
   `sessions/service.py` shift the `line=` of 43 `_REVIEWED_WRITERS` rows and 23
   `_REVIEWED_READ_CONNECTIONS` rows (fingerprints unchanged). The diagnostics
   authority's two rows get a new fingerprint because their function body gained
   the ledger call. Step 15 tables every move. The gate's XFAIL counts return
   exactly to HEAD's baseline (67 unexpected, 0 stale, 16 connection
   violations, 44 unresolved, 7 without authority, measured on a pristine HEAD
   export).
10. **Two neighbouring gates are extended, not bypassed.** The execution lease
    scanner (`tests/unit/web/execution/test_session_operation_lease.py`) gains
    `record_token_usage` as a session-service effect that must carry the
    transferred context, and `self._record_run_token_usage` as a reviewed positional
    lease consumer. Six test sites in three files (`test_persist_compose_turn.py`,
    `sessions/test_service.py`, `test_run_diagnostics_authority_postgres.py`) that fed
    `add_messages_atomic` or the diagnostics writer a fabricated `{"_kind": "llm_call_audit"}` envelope with
    no `call` body now build a real envelope with `llm_call_audit_envelope`,
    because the adapter reads the full call body the writer always emits.

**Files:**
- Create: `src/elspeth/web/coordination/quota_authority.py` (the QuotaAuthority: `TokenUsageEntry`, `QuotaPolicyRow`, `ActiveQuotaPolicies`, `QuotaExceeded`, `utc_day_start`, `llm_call_usage_entries`, `record_token_usage_on_connection`, `RepositoryQuotaAuthority`)
- Modify: `src/elspeth/contracts/chargeable_admission.py:23-28, :30-46, :51-60, :63-69, :76-85, :88-93`
- Modify: `src/elspeth/web/coordination/chargeable_admission_authority.py:12-19, :48-63, :67-81`
- Modify: `src/elspeth/web/auth/audit.py:33-38, :253-256, :285-290, :1176-1181`
- Modify: `src/elspeth/web/sessions/service.py:32-38, :97-103, :4379-4384, :4401-4406, :4419-4424, :10053-10075, :10087-10093, :10097-10102, :10728-10733, :14293-14298`
- Modify: `src/elspeth/web/sessions/protocol.py:73-78, :4691-4693`
- Modify: `src/elspeth/web/coordination/run_diagnostics_authority.py:10-15, :133-138`
- Modify: `src/elspeth/web/sessions/_auto_title.py:38-44, :263-268, :314-319, :325-332`
- Modify: `src/elspeth/web/execution/service.py:33-39, :42-48, :66-72, :97-102, :723-728, :3232-3237, :3429-3434, :3621-3626, :3754-3765, :3778-3783`
- Modify: `src/elspeth/web/app.py:1671-1676`
- Create: `tests/helpers/fenced_session.py`
- Create: `tests/unit/web/coordination/conftest.py`
- Create: `tests/unit/web/coordination/test_quota_authority.py`
- Create: `tests/unit/web/sessions/test_token_usage_adapters.py`
- Create: `tests/unit/web/execution/test_run_token_ledger.py`
- Create: `tests/testcontainer/web/test_quota_authority_postgres.py`
- Modify: `tests/unit/web/composer/test_chargeable_admission.py:42-53, :61-67, :312-314`
- Modify: `tests/unit/web/coordination/test_durable_run_admission.py:7-13, :324-330, :341-349`
- Modify: `tests/unit/web/coordination/test_cli_quota_admission.py:6-12, :75-83`
- Modify: `tests/unit/web/auth/test_audit.py:1048-1050`
- Modify: `tests/unit/web/auth/test_identity_admin_routes.py:93-98`
- Modify: `tests/unit/web/sessions/test_auto_title.py:14-19, :21-26, :543-545`
- Modify: `tests/unit/web/sessions/test_auto_title_sampling_config.py:16-21, :32-37`
- Modify: `tests/unit/web/sessions/test_auto_title_endpoint_affordance.py:22-27, :39-44`
- Modify: `tests/unit/web/sessions/test_persist_compose_turn.py:6-11, :14-21, :55-60, :1166-1173`
- Modify: `tests/unit/web/sessions/test_service.py:7-12, :16-23, :88-93, :1588-1594, :1676-1682`
- Modify: `tests/unit/web/execution/test_session_operation_lease.py:1751-1760, :1789-1794`
- Modify: `tests/testcontainer/web/test_run_diagnostics_authority_postgres.py:4-9, :14-19, :28-33, :138-144, :186-192`
- Modify: `tests/testcontainer/web/conftest.py:12-21, :214-216`
- Modify: `tests/unit/architecture/test_session_db_mutation_authority.py:269-274, :1255-1260, :1745-1764` (binding, new writer row, the two re-fingerprinted diagnostics rows) plus the 66 line-only re-pins tabled in Step 15
- Test: the six created test modules and the fourteen modified test modules listed above

**Interfaces:**
- Consumes:
  - I0: `token_usage_ledger_table.c.prompt_tokens` and `.completion_tokens` are `Integer, nullable=True` (`sessions/models.py:3914-3915`); `SESSION_SCHEMA_EPOCH == 57`. The ledger table is `models.py:3893`; `quota_policies_table` is `:3832`.
  - I8: nothing is read. I8's conftest edits leave the `engine` fixture (`tests/unit/web/conftest.py:63`) and `_make_session` (`:73`) as they are on HEAD.
  - HEAD: `_register_mutation_connection` / `_resolve_mutation_connection` / `_unregister_mutation_connection` (`coordination/mutation_connection_registry.py:22`); `database_now(conn)` (`coordination/database_clock.py:54`); `AuthAuditRepository.record_auth_event` (`core/landscape/auth_audit_repository.py:156`); `SessionServiceImpl.get_session` (`sessions/service.py:6903`), `list_run_events` (`:10217`), `_require_session_operation_context_on_connection` (`:4767`); `token_usage_from_response` (`composer/llm_response_parsing.py:206`), `safe_response_model` (`:459`), `build_llm_call_record` (`:844`); `llm_call_audit_envelope` (`composer/audit.py:352`); `prepare_guided_audit_rows` (`sessions/guided_audit.py:151`). Test helpers: `ensure_test_identity` (`tests/fixtures/identities.py:10`), `DualFencedSessionServiceHarness` (`tests/unit/web/sessions/guided_test_authority.py:22`), `_admission(engine)` (`tests/unit/web/coordination/test_durable_run_admission.py:85`), `make_landscape_db` / `leader_coordination_token` / `make_factory` (`tests/fixtures/landscape.py:46`, `:456`, `:561`), `external_deployment_postgres_url` (`tests/testcontainer/web/conftest.py:40`), `_durable_recorder` / `_durable_rows` / `_metadata` (`tests/unit/web/auth/test_audit.py:629`, `:634`, `:644`), `_run_auto_title` / `_completion_without_finish` / `_LIVE_LEAK_COMPLETION` (`tests/unit/web/sessions/test_auto_title.py:81`, `:77`, `:112`).
- Produces:
  - `src/elspeth/contracts/chargeable_admission.py`: `AdmissionRefusalReason.QUOTA_EXCEEDED = "quota_exceeded"`; `QuotaDisposition.WITHIN_CAP = "within_cap"` and `QuotaDisposition.EXCEEDED = "exceeded"`; `AdmissionPolicyEvidence.schema_version: Literal[2] = 2` plus `dimension: Literal["tokens"] | None`, `cap: int | None`, `ceiling: int | None`, `usage: int | None` (all default `None`, present exactly when the disposition is WITHIN_CAP or EXCEEDED); `ChargeableAdmissionDecision` admits `refusal_reason=None` with NOT_CONFIGURED or WITHIN_CAP, and maps `QUOTA_EXCEEDED → EXCEEDED`.
  - `src/elspeth/web/coordination/quota_authority.py`:
    - `TokenUsageSource = Literal["composer", "run", "auto_title"]`
    - `TokenUsageEntry(model: str, prompt_tokens: int | None, completion_tokens: int | None, cached_prompt_tokens: int | None, reasoning_tokens: int | None)` frozen slotted dataclass.
    - `QuotaPolicyRow(policy_id: str, tokens_per_day: int, storage_bytes: int)` and `ActiveQuotaPolicies(identity: QuotaPolicyRow | None, container: QuotaPolicyRow | None)` frozen slotted dataclasses.
    - `QuotaExceeded(identity_id: str, provider: AuthProviderType, operation: str, dimension: Literal["tokens", "storage"], cap: int | None, ceiling: int | None, usage: int, identity_policy_id: str | None, container_policy_id: str | None)` frozen slotted dataclass. I2 builds it with `dimension="storage"`.
    - `utc_day_start(now: datetime) -> datetime`
    - `token_usage_entry_from_llm_call_envelope(envelope: Mapping[str, Any]) -> TokenUsageEntry | None` and `llm_call_usage_entries(tool_calls: Sequence[Mapping[str, Any]]) -> tuple[TokenUsageEntry, ...]`
    - `record_token_usage_on_connection(connection: Connection, *, session_id: str, source: TokenUsageSource, run_id: str | None, entries: Sequence[TokenUsageEntry], recorded_at: datetime) -> tuple[str, ...]`: the only INSERT into `token_usage_ledger`. For `source="run"` the entries are the run's complete ordered LLM call list and only the unrecorded suffix is appended.
    - `RepositoryQuotaAuthority.record_token_usage(connection_token: str, *, session_id: str, source: TokenUsageSource, run_id: str | None, entries: Sequence[TokenUsageEntry], recorded_at: datetime) -> tuple[str, ...]`
    - `RepositoryQuotaAuthority.daily_token_total(connection_token: str, *, identity_id: str, day_start_utc: datetime) -> int | None`
    - `RepositoryQuotaAuthority.active_policy(connection_token: str, *, identity_id: str) -> ActiveQuotaPolicies` (both rows locked `FOR UPDATE`; I2 consumes it).
  - `src/elspeth/web/auth/audit.py`: `AuthAuditWriter.record_quota_exceeded(self, outcome: QuotaExceeded) -> None` and `AuthAuditRecorder.record_quota_exceeded`; `AuthAuditOperation.QUOTA_EXCEEDED`. Row: `event_type="quota_exceeded"`, `outcome="failure"`, `identity_id=outcome.identity_id`, `failure_category=f"quota_exceeded_[dimension]"` (`quota_exceeded_tokens` / `quota_exceeded_storage`), metadata `actor, operation, dimension, cap, ceiling, usage, identity_policy_id, container_policy_id`.
  - `src/elspeth/web/sessions/service.py`: `SessionServiceImpl.__init__` gains the keyword parameter `quota_exceeded_recorder: Callable[[QuotaExceeded], None] = _refuse_unrecorded_quota_exceeded` (the default raises `AuditIntegrityError`); `SessionServiceImpl.record_token_usage(*, session_operation_context: SessionOperationContext, source: TokenUsageSource, run_id: UUID | None, entries: tuple[TokenUsageEntry, ...]) -> tuple[str, ...]` (also on `SessionServiceProtocol`, `sessions/protocol.py`).
  - `src/elspeth/web/execution/service.py`: `_run_token_usage_entries(landscape_db: LandscapeDB, *, landscape_run_id: str) -> tuple[TokenUsageEntry, ...]` and `ExecutionServiceImpl._record_run_token_usage(self, run_uuid: UUID, session_operation_lease: SessionOperationLease, *, landscape_db: LandscapeDB | None, landscape_run_id: str) -> None`.
  - Fixtures: `tests/helpers/fenced_session.py` defines `FencedSession(engine, connection_token, identity_id, session_id)`, `FencedSessionWithPolicy(engine, connection_token, identity_id, session_id, identity_policy_id, container_policy_id)`, `seed_token_policies(conn, *, identity_id) -> tuple[str, str]` (returns `("quota-identity", "quota-container")`), `IDENTITY_TOKENS_PER_DAY = 1000` and `CONTAINER_TOKENS_PER_DAY = 5000`. `tests/unit/web/coordination/conftest.py` defines `fenced_session` (an active identity `alice`, a session she owns created by `SQLiteLocalSessionOperationAuthority`, and a registered token over one open `connection.begin()` transaction, rolled back at teardown; `session_id` is a `str`) and `fenced_session_with_policy`. `tests/testcontainer/web/conftest.py` defines `pg_fenced`, the same `FencedSession` on the shared PostgreSQL container in its own database. I3 consumes `fenced_session` and `pg_fenced` by these names.

- [ ] **Step 1: Write the failing contract tests.** They pin the four measured facts, both allowance dispositions and the decode refusal of a version-1 payload, and they add `QUOTA_EXCEEDED` to the public-entry refusal matrix.

`tests/unit/web/composer/test_chargeable_admission.py` HEAD lines 42-53 read:

```python
            disposition = QuotaDisposition.NOT_CONFIGURED
        elif reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE:
            disposition = QuotaDisposition.ACCOUNTING_UNAVAILABLE
        self.decision = ChargeableAdmissionDecision(
            refusal_reason=reason,
            evidence=AdmissionPolicyEvidence(
                quota_disposition=disposition,
                identity_policy_id="identity-token-policy" if disposition is QuotaDisposition.ACCOUNTING_UNAVAILABLE else None,
                secret_wiring_hash="a" * 64,
            ),
        )
```

Replace them with:

```python
            disposition = QuotaDisposition.NOT_CONFIGURED
        elif reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE:
            disposition = QuotaDisposition.ACCOUNTING_UNAVAILABLE
        elif reason is AdmissionRefusalReason.QUOTA_EXCEEDED:
            disposition = QuotaDisposition.EXCEEDED
        exceeded = disposition is QuotaDisposition.EXCEEDED
        self.decision = ChargeableAdmissionDecision(
            refusal_reason=reason,
            evidence=AdmissionPolicyEvidence(
                quota_disposition=disposition,
                identity_policy_id="identity-token-policy"
                if disposition in {QuotaDisposition.ACCOUNTING_UNAVAILABLE, QuotaDisposition.EXCEEDED}
                else None,
                secret_wiring_hash="a" * 64,
                dimension="tokens" if exceeded else None,
                cap=1000 if exceeded else None,
                usage=1000 if exceeded else None,
            ),
        )
```

`tests/unit/web/composer/test_chargeable_admission.py` HEAD lines 61-67 read:

```python

@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["compose", "guided_full", "guided_delta", "diagnostics", "signoff"])
@pytest.mark.parametrize("reason", [AdmissionRefusalReason.IDENTITY_DISABLED, AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE])
async def test_public_entry_refuses_before_provider_work(
    composer_service_without_sessions_service: ComposerServiceImpl, entry: str, reason: AdmissionRefusalReason
) -> None:
```

Replace them with:

```python

@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["compose", "guided_full", "guided_delta", "diagnostics", "signoff"])
@pytest.mark.parametrize(
    "reason",
    [AdmissionRefusalReason.IDENTITY_DISABLED, AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE, AdmissionRefusalReason.QUOTA_EXCEEDED],
)
async def test_public_entry_refuses_before_provider_work(
    composer_service_without_sessions_service: ComposerServiceImpl, entry: str, reason: AdmissionRefusalReason
) -> None:
```

`tests/unit/web/composer/test_chargeable_admission.py` HEAD lines 312-314 read:

```python
    assert authority.operations == [ChargeableOperation.COMPOSER]
    assert planner.await_count == int(allowed and entry != "signoff")
    assert advisor.await_count == int(allowed and entry == "signoff")
```

Replace them with:

```python
    assert authority.operations == [ChargeableOperation.COMPOSER]
    assert planner.await_count == int(allowed and entry != "signoff")
    assert advisor.await_count == int(allowed and entry == "signoff")


# ── Task I1: the measured R14 contract (AdmissionPolicyEvidence schema_version 2) ──

_WIRING = "a" * 64


def test_measured_quota_evidence_carries_exactly_its_four_facts() -> None:
    """WITHIN_CAP/EXCEEDED carry dimension, cap, ceiling and usage (spec :834); no other disposition may."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="Only a measured quota assessment carries dimension, cap, ceiling or usage"):
        AdmissionPolicyEvidence(quota_disposition=QuotaDisposition.NOT_CONFIGURED, secret_wiring_hash=_WIRING, usage=0)
    with pytest.raises(ValidationError, match="Measured quota evidence requires an assessed quota policy"):
        AdmissionPolicyEvidence(
            quota_disposition=QuotaDisposition.WITHIN_CAP, secret_wiring_hash=_WIRING, dimension="tokens", cap=10, usage=0
        )
    with pytest.raises(ValidationError, match="requires dimension='tokens' and a measured usage"):
        AdmissionPolicyEvidence(quota_disposition=QuotaDisposition.WITHIN_CAP, identity_policy_id="p", secret_wiring_hash=_WIRING, cap=10)
    with pytest.raises(ValidationError, match="carries a cap exactly for an identity policy and a ceiling exactly for a container policy"):
        AdmissionPolicyEvidence(
            quota_disposition=QuotaDisposition.WITHIN_CAP,
            identity_policy_id="p",
            secret_wiring_hash=_WIRING,
            dimension="tokens",
            ceiling=10,
            usage=0,
        )


@pytest.mark.parametrize(
    ("usage", "disposition", "valid"),
    [
        (9, QuotaDisposition.WITHIN_CAP, True),
        (10, QuotaDisposition.WITHIN_CAP, False),
        (10, QuotaDisposition.EXCEEDED, True),
        (9, QuotaDisposition.EXCEEDED, False),
    ],
)
def test_measured_disposition_derives_from_usage_against_the_lower_bound(usage: int, disposition: QuotaDisposition, valid: bool) -> None:
    """Cap 50, ceiling 10: the ceiling is the bound, so 10 is exceeded and 9 is not."""
    from pydantic import ValidationError

    def build() -> AdmissionPolicyEvidence:
        return AdmissionPolicyEvidence(
            quota_disposition=disposition,
            identity_policy_id="p",
            container_policy_id="c",
            secret_wiring_hash=_WIRING,
            dimension="tokens",
            cap=50,
            ceiling=10,
            usage=usage,
        )

    if valid:
        assert build().usage == usage
    else:
        with pytest.raises(ValidationError, match="Quota disposition contradicts the measured usage"):
            build()


def test_quota_exceeded_refuses_only_with_exceeded_evidence() -> None:
    exceeded = AdmissionPolicyEvidence(
        quota_disposition=QuotaDisposition.EXCEEDED,
        identity_policy_id="p",
        secret_wiring_hash=_WIRING,
        dimension="tokens",
        cap=10,
        usage=10,
    )
    within = AdmissionPolicyEvidence(
        quota_disposition=QuotaDisposition.WITHIN_CAP,
        identity_policy_id="p",
        secret_wiring_hash=_WIRING,
        dimension="tokens",
        cap=10,
        usage=9,
    )
    from pydantic import ValidationError

    assert not ChargeableAdmissionDecision(refusal_reason=AdmissionRefusalReason.QUOTA_EXCEEDED, evidence=exceeded).allowed
    assert ChargeableAdmissionDecision(refusal_reason=None, evidence=within).allowed
    with pytest.raises(ValidationError, match="An allowance disposition cannot carry a refusal"):
        ChargeableAdmissionDecision(refusal_reason=AdmissionRefusalReason.QUOTA_EXCEEDED, evidence=within)
    with pytest.raises(ValidationError, match="Only an explicitly unconfigured token quota or a measured usage within the cap"):
        ChargeableAdmissionDecision(refusal_reason=None, evidence=exceeded)
    with pytest.raises(ValidationError, match="Admission refusal contradicts quota assessment"):
        ChargeableAdmissionDecision(refusal_reason=AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE, evidence=exceeded)


def test_version_one_admission_evidence_is_refused_not_upgraded() -> None:
    """schema_version 2 changes every canonical admission hash; a stored v1 payload fails closed on decode."""
    import json

    from pydantic import ValidationError

    from elspeth.contracts.plugin_policy_audit import decode_admission_decision

    v1_decision = {
        "evidence": {
            "schema_version": 1,
            "identity_policy_id": None,
            "container_policy_id": None,
            "quota_disposition": "not_configured",
            "secret_wiring_hash": _WIRING,
        },
        "refusal_reason": None,
    }
    with pytest.raises(ValidationError, match="Input should be 2"):
        decode_admission_decision(json.dumps(v1_decision), "0" * 64)
```

- [ ] **Step 2: Run the contract tests to verify they fail.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/composer/test_chargeable_admission.py -n 0 > /tmp/i-lane-i1-step2.log 2>&1; echo exit=$?
```

Expected: `exit=2`. Collection stops at `ERROR collecting tests/unit/web/composer/test_chargeable_admission.py` with `AttributeError: type object 'AdmissionRefusalReason' has no attribute 'QUOTA_EXCEEDED'`.

- [ ] **Step 3: Extend the admission contract to schema version 2.**

`src/elspeth/contracts/chargeable_admission.py` HEAD lines 23-28 read:

```python
    QUOTA_POLICY_MISSING = "quota_policy_missing"
    TOKEN_ACCOUNTING_UNAVAILABLE = "token_accounting_unavailable"
    POLICY_GENERATION_CHANGED = "policy_generation_changed"


class QuotaDisposition(StrEnum):
```

Replace them with:

```python
    QUOTA_POLICY_MISSING = "quota_policy_missing"
    TOKEN_ACCOUNTING_UNAVAILABLE = "token_accounting_unavailable"
    POLICY_GENERATION_CHANGED = "policy_generation_changed"
    QUOTA_EXCEEDED = "quota_exceeded"


class QuotaDisposition(StrEnum):
```

`src/elspeth/contracts/chargeable_admission.py` HEAD lines 30-46 read:

```python
    NOT_CONFIGURED = "not_configured"
    POLICY_MISSING = "policy_missing"
    ACCOUNTING_UNAVAILABLE = "accounting_unavailable"


class AdmissionPolicyEvidence(BaseModel):
    """Sanitized decision facts; absence is never represented as measured zero."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[1] = 1
    identity_policy_id: str | None = Field(default=None, min_length=1)
    container_policy_id: str | None = Field(default=None, min_length=1)
    quota_disposition: QuotaDisposition
    secret_wiring_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("schema_version", mode="before")
    @classmethod
```

Replace them with:

```python
    NOT_CONFIGURED = "not_configured"
    POLICY_MISSING = "policy_missing"
    ACCOUNTING_UNAVAILABLE = "accounting_unavailable"
    # R14 measured the identity's UTC-day token total against the policies in
    # force (sso-design.md:1161-1167): WITHIN_CAP admits, EXCEEDED refuses.
    WITHIN_CAP = "within_cap"
    EXCEEDED = "exceeded"


class AdmissionPolicyEvidence(BaseModel):
    """Sanitized decision facts; absence is never represented as measured zero.

    Version 2 (identity sprint Task I1) carries the R14 measurement: the
    ``dimension``, the identity policy ``cap``, the container ``ceiling`` and the
    measured ``usage``. They are present exactly when the disposition is
    WITHIN_CAP or EXCEEDED, and absent on every other disposition.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    schema_version: Literal[2] = 2
    identity_policy_id: str | None = Field(default=None, min_length=1)
    container_policy_id: str | None = Field(default=None, min_length=1)
    quota_disposition: QuotaDisposition
    secret_wiring_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    dimension: Literal["tokens"] | None = None
    cap: int | None = Field(default=None, ge=0)
    ceiling: int | None = Field(default=None, ge=0)
    usage: int | None = Field(default=None, ge=0)

    @field_validator("schema_version", mode="before")
    @classmethod
```

`src/elspeth/contracts/chargeable_admission.py` HEAD lines 51-60 read:

```python

    @model_validator(mode="after")
    def _assessed_policy_presence(self) -> AdmissionPolicyEvidence:
        if self.quota_disposition is QuotaDisposition.ACCOUNTING_UNAVAILABLE and (
            self.identity_policy_id is None and self.container_policy_id is None
        ):
            raise ValueError("Accounting-unavailable evidence requires an assessed quota policy")
        return self

    @property
```

Replace them with:

```python

    @model_validator(mode="after")
    def _assessed_policy_presence(self) -> AdmissionPolicyEvidence:
        disposition = self.quota_disposition
        no_policy = self.identity_policy_id is None and self.container_policy_id is None
        if disposition is QuotaDisposition.ACCOUNTING_UNAVAILABLE and no_policy:
            raise ValueError("Accounting-unavailable evidence requires an assessed quota policy")
        if disposition not in {QuotaDisposition.WITHIN_CAP, QuotaDisposition.EXCEEDED}:
            if self.dimension is not None or self.cap is not None or self.ceiling is not None or self.usage is not None:
                raise ValueError("Only a measured quota assessment carries dimension, cap, ceiling or usage")
            return self
        if no_policy:
            raise ValueError("Measured quota evidence requires an assessed quota policy")
        if self.dimension != "tokens" or self.usage is None:
            raise ValueError("Measured quota evidence requires dimension='tokens' and a measured usage")
        if (self.cap is None) != (self.identity_policy_id is None) or (self.ceiling is None) != (self.container_policy_id is None):
            raise ValueError(
                "Measured quota evidence carries a cap exactly for an identity policy and a ceiling exactly for a container policy"
            )
        limit = min(bound for bound in (self.cap, self.ceiling) if bound is not None)
        if (disposition is QuotaDisposition.EXCEEDED) != (self.usage >= limit):
            raise ValueError("Quota disposition contradicts the measured usage")
        return self

    @property
```

`src/elspeth/contracts/chargeable_admission.py` HEAD lines 63-69 read:

```python


class ChargeableAdmissionDecision(BaseModel):
    """A closed refusal or an explicit no-token-quota allowance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    evidence: AdmissionPolicyEvidence
```

Replace them with:

```python


class ChargeableAdmissionDecision(BaseModel):
    """A closed refusal, an explicit no-token-quota allowance, or a measured within-cap allowance."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    evidence: AdmissionPolicyEvidence
```

`src/elspeth/contracts/chargeable_admission.py` HEAD lines 76-85 read:

```python
    @model_validator(mode="after")
    def _consistent_decision(self) -> ChargeableAdmissionDecision:
        disposition = self.evidence.quota_disposition
        if self.refusal_reason is None and disposition is not QuotaDisposition.NOT_CONFIGURED:
            raise ValueError("Only explicitly unconfigured token quotas admit chargeable work")
        if disposition is QuotaDisposition.NOT_CONFIGURED and self.refusal_reason is not None:
            raise ValueError("An unconfigured quota allowance cannot carry a refusal")
        if self.refusal_reason is not None:
            expected_disposition = {
                AdmissionRefusalReason.IDENTITY_DISABLED: QuotaDisposition.NOT_ASSESSED,
```

Replace them with:

```python
    @model_validator(mode="after")
    def _consistent_decision(self) -> ChargeableAdmissionDecision:
        disposition = self.evidence.quota_disposition
        allowing = {QuotaDisposition.NOT_CONFIGURED, QuotaDisposition.WITHIN_CAP}
        if self.refusal_reason is None and disposition not in allowing:
            raise ValueError("Only an explicitly unconfigured token quota or a measured usage within the cap admits chargeable work")
        if disposition in allowing and self.refusal_reason is not None:
            raise ValueError("An allowance disposition cannot carry a refusal")
        if self.refusal_reason is not None:
            expected_disposition = {
                AdmissionRefusalReason.IDENTITY_DISABLED: QuotaDisposition.NOT_ASSESSED,
```

`src/elspeth/contracts/chargeable_admission.py` HEAD lines 88-93 read:

```python
                AdmissionRefusalReason.POLICY_GENERATION_CHANGED: QuotaDisposition.NOT_ASSESSED,
                AdmissionRefusalReason.QUOTA_POLICY_MISSING: QuotaDisposition.POLICY_MISSING,
                AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE: QuotaDisposition.ACCOUNTING_UNAVAILABLE,
            }[self.refusal_reason]
            if disposition is not expected_disposition:
                raise ValueError("Admission refusal contradicts quota assessment")
```

Replace them with:

```python
                AdmissionRefusalReason.POLICY_GENERATION_CHANGED: QuotaDisposition.NOT_ASSESSED,
                AdmissionRefusalReason.QUOTA_POLICY_MISSING: QuotaDisposition.POLICY_MISSING,
                AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE: QuotaDisposition.ACCOUNTING_UNAVAILABLE,
                AdmissionRefusalReason.QUOTA_EXCEEDED: QuotaDisposition.EXCEEDED,
            }[self.refusal_reason]
            if disposition is not expected_disposition:
                raise ValueError("Admission refusal contradicts quota assessment")
```

- [ ] **Step 4: Run the contract tests and every test that computes an admission hash.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/composer/test_chargeable_admission.py tests/unit/web/coordination/test_durable_run_admission.py tests/unit/web/coordination/test_cli_quota_admission.py tests/unit/core/landscape/test_admission_policy_provenance.py tests/unit/core/landscape/test_exporter.py -n 0 > /tmp/i-lane-i1-step4.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `160 passed`. The Landscape provenance and exporter tests derive `admission_decision_hash` from the model, so the version-2 hash change needs no pin edit. The durable-run and CLI pins still pass because `assess` has not changed yet.

- [ ] **Step 5: Write the fenced-session fixtures and the failing QuotaAuthority tests.** `tests/helpers/fenced_session.py` is shared by the unit conftest and the PostgreSQL conftest (Step 12), so both dialects build the same `FencedSession`.

Create `tests/helpers/fenced_session.py`:

```python
"""A registered mutation-connection token over one owned session, for authority-level tests.

The identity-workflow authorities (quota Task I1, approvals Task I3) take a
``connection_token`` exactly as production facets hand one over. These fixtures
build the smallest real version of that: an active identity, a session it owns
created through the session-operation authority, and one open transaction whose
connection is registered in the mutation-connection registry for the test's
lifetime and rolled back afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import insert
from sqlalchemy.engine import Connection, Engine

from elspeth.web.sessions.models import quota_policies_table

IDENTITY_TOKENS_PER_DAY = 1000
CONTAINER_TOKENS_PER_DAY = 5000


@dataclass(frozen=True, slots=True)
class FencedSession:
    engine: Engine
    connection_token: str
    identity_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class FencedSessionWithPolicy:
    engine: Engine
    connection_token: str
    identity_id: str
    session_id: str
    identity_policy_id: str
    container_policy_id: str


def seed_token_policies(conn: Connection, *, identity_id: str) -> tuple[str, str]:
    """Insert one active identity policy and one active container ceiling; return their ids."""
    set_at = datetime(2026, 9, 1, tzinfo=UTC)
    conn.execute(
        insert(quota_policies_table).values(
            policy_id="quota-identity",
            identity_id=identity_id,
            tokens_per_day=IDENTITY_TOKENS_PER_DAY,
            storage_bytes=1_000_000,
            set_by_actor="operator",
            set_by_identity_id=None,
            set_at=set_at,
        )
    )
    conn.execute(
        insert(quota_policies_table).values(
            policy_id="quota-container",
            identity_id=None,
            tokens_per_day=CONTAINER_TOKENS_PER_DAY,
            storage_bytes=10_000_000,
            set_by_actor="config",
            set_by_identity_id=None,
            set_at=set_at,
        )
    )
    return "quota-identity", "quota-container"
```

Create `tests/unit/web/coordination/conftest.py`:

```python
"""Fenced-transaction fixtures for the identity-workflow authorities (Tasks I1, I2, I3)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.fenced_session import FencedSession, FencedSessionWithPolicy, seed_token_policies

from elspeth.web.coordination.mutation_connection_registry import (
    _register_mutation_connection,
    _resolve_mutation_connection,
    _unregister_mutation_connection,
)
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema


@pytest.fixture
def fenced_session(tmp_path: Path) -> Iterator[FencedSession]:
    """An active identity ``alice``, a session she owns, and a registered token over one open transaction."""
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    session = SQLiteLocalSessionOperationAuthority(engine).create_session_with_initial_fence(
        user_id="alice", title="fenced", auth_provider_type="local", owner_instance_id="owner", lease_seconds=30
    )
    connection = engine.connect()
    transaction = connection.begin()
    token = _register_mutation_connection(connection)
    try:
        yield FencedSession(engine=engine, connection_token=token, identity_id="alice", session_id=str(session.id))
    finally:
        _unregister_mutation_connection(token)
        transaction.rollback()
        connection.close()
        engine.dispose()


@pytest.fixture
def fenced_session_with_policy(fenced_session: FencedSession) -> FencedSessionWithPolicy:
    """``fenced_session`` plus an active identity policy (1000/day) and container ceiling (5000/day)."""
    identity_policy_id, container_policy_id = seed_token_policies(
        _resolve_mutation_connection(fenced_session.connection_token), identity_id=fenced_session.identity_id
    )
    return FencedSessionWithPolicy(
        engine=fenced_session.engine,
        connection_token=fenced_session.connection_token,
        identity_id=fenced_session.identity_id,
        session_id=fenced_session.session_id,
        identity_policy_id=identity_policy_id,
        container_policy_id=container_policy_id,
    )
```

Create `tests/unit/web/coordination/test_quota_authority.py`:

```python
"""QuotaAuthority: the token ledger, the UTC-day aggregate and R14 admission (Task I1).

Every numbered refusal has a fire test and a mutation-derivation test: the
authority row (a policy's ``tokens_per_day``, a ledger measure, the database
clock) is changed, never the guard.
"""

from __future__ import annotations

import dataclasses
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.fenced_session import CONTAINER_TOKENS_PER_DAY, IDENTITY_TOKENS_PER_DAY, FencedSession, FencedSessionWithPolicy

from elspeth.contracts.chargeable_admission import (
    AdmissionRefusalReason,
    ChargeableAdmissionPolicy,
    QuotaDisposition,
)
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.coordination import chargeable_admission_authority
from elspeth.web.coordination.chargeable_admission_authority import RepositoryChargeableAdmissionAuthority
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.coordination.quota_authority import (
    ActiveQuotaPolicies,
    QuotaPolicyRow,
    RepositoryQuotaAuthority,
    TokenUsageEntry,
    TokenUsageSource,
    llm_call_usage_entries,
    utc_day_start,
)
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions.models import quota_policies_table, sessions_table, token_usage_ledger_table

DAY = datetime(2026, 9, 13, tzinfo=UTC)
NO_REQUIRED_POLICY = ChargeableAdmissionPolicy(secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)


def _entry(prompt: int | None = 100, completion: int | None = 50, *, model: str = "openai/gpt-test") -> TokenUsageEntry:
    return TokenUsageEntry(
        model=model, prompt_tokens=prompt, completion_tokens=completion, cached_prompt_tokens=None, reasoning_tokens=None
    )


def _record(
    fenced: FencedSession | FencedSessionWithPolicy, *entries: TokenUsageEntry, at: datetime, source: TokenUsageSource = "composer"
) -> tuple[str, ...]:
    return RepositoryQuotaAuthority.record_token_usage(
        fenced.connection_token,
        session_id=fenced.session_id,
        source=source,
        run_id=None,
        entries=entries,
        recorded_at=at,
    )


def _pin_database_clock(monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
    """R14 reads the sessions database clock; the test names the instant instead of racing midnight."""
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _conn: now)


def _assess(fenced: FencedSessionWithPolicy, policy: ChargeableAdmissionPolicy = NO_REQUIRED_POLICY):
    return RepositoryChargeableAdmissionAuthority.assess(fenced.connection_token, session_id=fenced.session_id, policy=policy)


def _set_tokens_per_day(fenced: FencedSessionWithPolicy, policy_id: str, tokens_per_day: int) -> None:
    _resolve_mutation_connection(fenced.connection_token).execute(
        update(quota_policies_table).where(quota_policies_table.c.policy_id == policy_id).values(tokens_per_day=tokens_per_day)
    )


# ── the ledger ────────────────────────────────────────────────────────────


def test_record_token_usage_attributes_every_row_to_the_session_owner(fenced_session: FencedSession) -> None:
    entry_ids = _record(fenced_session, _entry(7, 3), _entry(None, None, model="openai/other"), at=DAY + timedelta(hours=1))
    rows = (
        _resolve_mutation_connection(fenced_session.connection_token)
        .execute(select(token_usage_ledger_table).order_by(token_usage_ledger_table.c.prompt_tokens.is_(None)))
        .all()
    )
    assert [row.entry_id for row in rows] == list(entry_ids)
    assert {(row.identity_id, row.session_id, row.source, row.run_id) for row in rows} == {
        (fenced_session.identity_id, fenced_session.session_id, "composer", None)
    }
    assert [(row.model, row.prompt_tokens, row.completion_tokens) for row in rows] == [
        ("openai/gpt-test", 7, 3),
        ("openai/other", None, None),
    ]


def test_record_token_usage_follows_the_session_owner_not_a_caller_argument(fenced_session: FencedSession) -> None:
    """Mutation-derivation: re-own the session and the same call is charged to the new owner."""
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    ensure_test_identity(conn, identity_id="bob")
    conn.execute(update(sessions_table).where(sessions_table.c.id == fenced_session.session_id).values(user_id="bob"))
    _record(fenced_session, _entry(), at=DAY)
    assert conn.execute(select(token_usage_ledger_table.c.identity_id)).scalar_one() == "bob"


def test_record_token_usage_requires_run_id_exactly_for_run_source(fenced_session: FencedSession) -> None:
    with pytest.raises(ValueError, match="run_id is required for, and only for, source='run'"):
        RepositoryQuotaAuthority.record_token_usage(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            source="run",
            run_id=None,
            entries=(_entry(),),
            recorded_at=DAY,
        )
    with pytest.raises(ValueError, match="run_id is required for, and only for, source='run'"):
        RepositoryQuotaAuthority.record_token_usage(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            source="composer",
            run_id="run-1",
            entries=(_entry(),),
            recorded_at=DAY,
        )


def test_run_usage_appends_only_the_calls_not_yet_recorded(fenced_session: FencedSession) -> None:
    """A resumed run re-reads its whole call list; the ledger charges each call once."""
    first = RepositoryQuotaAuthority.record_token_usage(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        source="run",
        run_id="run-1",
        entries=(_entry(1, 1),),
        recorded_at=DAY,
    )
    second = RepositoryQuotaAuthority.record_token_usage(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        source="run",
        run_id="run-1",
        entries=(_entry(1, 1), _entry(2, 2)),
        recorded_at=DAY,
    )
    assert len(first) == 1 and len(second) == 1
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    assert sorted(conn.execute(select(token_usage_ledger_table.c.prompt_tokens)).scalars()) == [1, 2]


def test_run_usage_refuses_a_ledger_longer_than_the_call_list(fenced_session: FencedSession) -> None:
    RepositoryQuotaAuthority.record_token_usage(
        fenced_session.connection_token,
        session_id=fenced_session.session_id,
        source="run",
        run_id="run-1",
        entries=(_entry(), _entry()),
        recorded_at=DAY,
    )
    from elspeth.contracts.errors import AuditIntegrityError

    with pytest.raises(AuditIntegrityError, match="more rows than the run has LLM calls"):
        RepositoryQuotaAuthority.record_token_usage(
            fenced_session.connection_token,
            session_id=fenced_session.session_id,
            source="run",
            run_id="run-1",
            entries=(_entry(),),
            recorded_at=DAY,
        )


def test_llm_call_envelopes_charge_successes_and_reported_failures_only() -> None:
    base = build_llm_call_record(
        model_requested="openai/requested",
        messages=[{"role": "user", "content": "prompt"}],
        tools=None,
        status=ComposerLLMCallStatus.SUCCESS,
        started_at=DAY,
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
    )
    measured = dataclasses.replace(base, model_returned="openai/returned", prompt_tokens=11, completion_tokens=4, reasoning_tokens=2)
    unreported_success = base
    failed_unreported = dataclasses.replace(base, status=ComposerLLMCallStatus.TIMEOUT, error_class="TimeoutError", error_message="t")
    failed_reported = dataclasses.replace(failed_unreported, prompt_tokens=9, completion_tokens=0)
    envelopes = (
        llm_call_audit_envelope(measured),
        {"_kind": "audit", "invocation": {}},
        {"id": "call_1", "type": "function", "function": {"name": "set_source", "arguments": "{}"}},
        llm_call_audit_envelope(unreported_success),
        llm_call_audit_envelope(failed_unreported),
        llm_call_audit_envelope(failed_reported),
    )
    assert llm_call_usage_entries(envelopes) == (
        TokenUsageEntry(model="openai/returned", prompt_tokens=11, completion_tokens=4, cached_prompt_tokens=None, reasoning_tokens=2),
        TokenUsageEntry(
            model="openai/requested", prompt_tokens=None, completion_tokens=None, cached_prompt_tokens=None, reasoning_tokens=None
        ),
        TokenUsageEntry(model="openai/requested", prompt_tokens=9, completion_tokens=0, cached_prompt_tokens=None, reasoning_tokens=None),
    )


# ── the UTC-day aggregate ─────────────────────────────────────────────────


def test_daily_total_sums_only_the_identity_and_the_utc_day(fenced_session: FencedSession) -> None:
    """The day boundary is UTC midnight (spec :1167), named here explicitly."""
    conn = _resolve_mutation_connection(fenced_session.connection_token)
    _record(fenced_session, _entry(1000, 1000), at=DAY - timedelta(microseconds=1))
    _record(fenced_session, _entry(100, 50), at=DAY)
    _record(fenced_session, _entry(10, 5), at=DAY + timedelta(hours=23, minutes=59, seconds=59))
    _record(fenced_session, _entry(1000, 1000), at=DAY + timedelta(days=1))
    ensure_test_identity(conn, identity_id="bob")
    conn.execute(
        insert(token_usage_ledger_table).values(
            entry_id="bob-entry",
            identity_id="bob",
            source="composer",
            session_id=None,
            run_id=None,
            model="m",
            prompt_tokens=500,
            completion_tokens=500,
            cached_prompt_tokens=None,
            reasoning_tokens=None,
            recorded_at=DAY + timedelta(hours=2),
        )
    )
    assert (
        RepositoryQuotaAuthority.daily_token_total(
            fenced_session.connection_token, identity_id=fenced_session.identity_id, day_start_utc=DAY
        )
        == 165
    )


def test_daily_total_over_a_day_with_no_rows_is_measured_zero(fenced_session: FencedSession) -> None:
    assert (
        RepositoryQuotaAuthority.daily_token_total(
            fenced_session.connection_token, identity_id=fenced_session.identity_id, day_start_utc=DAY
        )
        == 0
    )


def test_unknown_usage_makes_the_total_unknown_not_zero(fenced_session: FencedSession) -> None:
    _record(fenced_session, _entry(100, 50), at=DAY)
    _record(fenced_session, _entry(None, 50), at=DAY + timedelta(hours=1))
    assert (
        RepositoryQuotaAuthority.daily_token_total(
            fenced_session.connection_token, identity_id=fenced_session.identity_id, day_start_utc=DAY
        )
        is None
    )


def test_unknown_total_derives_from_the_null_measure(fenced_session: FencedSession) -> None:
    """Mutation-derivation: supply the missing measure on the same row and the day is measured again."""
    (unknown_id,) = _record(fenced_session, _entry(None, 50), at=DAY)
    _resolve_mutation_connection(fenced_session.connection_token).execute(
        update(token_usage_ledger_table).where(token_usage_ledger_table.c.entry_id == unknown_id).values(prompt_tokens=25)
    )
    assert (
        RepositoryQuotaAuthority.daily_token_total(
            fenced_session.connection_token, identity_id=fenced_session.identity_id, day_start_utc=DAY
        )
        == 75
    )


def test_daily_total_refuses_a_day_start_that_is_not_utc_midnight(fenced_session: FencedSession) -> None:
    with pytest.raises(ValueError, match="day_start_utc must be a UTC midnight"):
        RepositoryQuotaAuthority.daily_token_total(
            fenced_session.connection_token, identity_id=fenced_session.identity_id, day_start_utc=DAY + timedelta(hours=1)
        )


def test_utc_day_start_converts_before_truncating() -> None:
    from datetime import timezone

    sydney_morning = datetime(2026, 9, 14, 9, 30, tzinfo=timezone(timedelta(hours=10)))
    assert utc_day_start(sydney_morning) == datetime(2026, 9, 13, tzinfo=UTC)


def test_active_policy_returns_only_unrevoked_rows(fenced_session_with_policy: FencedSessionWithPolicy) -> None:
    fenced = fenced_session_with_policy
    assert RepositoryQuotaAuthority.active_policy(fenced.connection_token, identity_id=fenced.identity_id) == ActiveQuotaPolicies(
        identity=QuotaPolicyRow(policy_id=fenced.identity_policy_id, tokens_per_day=IDENTITY_TOKENS_PER_DAY, storage_bytes=1_000_000),
        container=QuotaPolicyRow(policy_id=fenced.container_policy_id, tokens_per_day=CONTAINER_TOKENS_PER_DAY, storage_bytes=10_000_000),
    )
    _resolve_mutation_connection(fenced.connection_token).execute(
        update(quota_policies_table).where(quota_policies_table.c.policy_id == fenced.identity_policy_id).values(revoked_at=DAY)
    )
    assert RepositoryQuotaAuthority.active_policy(fenced.connection_token, identity_id=fenced.identity_id).identity is None


# ── R14 ───────────────────────────────────────────────────────────────────


def test_r14_refuses_when_the_day_total_reaches_the_identity_cap(
    fenced_session_with_policy: FencedSessionWithPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fire test. sso-design.md:1161: refuse work that would take the identity over tokens_per_day."""
    fenced = fenced_session_with_policy
    _pin_database_clock(monkeypatch, DAY + timedelta(hours=12))
    _record(fenced, _entry(600, 400), at=DAY + timedelta(hours=1))
    decision = _assess(fenced)
    assert decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert decision.evidence.quota_disposition is QuotaDisposition.EXCEEDED
    assert (decision.evidence.dimension, decision.evidence.cap, decision.evidence.ceiling, decision.evidence.usage) == (
        "tokens",
        IDENTITY_TOKENS_PER_DAY,
        CONTAINER_TOKENS_PER_DAY,
        1000,
    )
    assert (decision.evidence.identity_policy_id, decision.evidence.container_policy_id) == (
        fenced.identity_policy_id,
        fenced.container_policy_id,
    )


@pytest.mark.parametrize(("tokens_per_day", "refused"), [(1001, False), (1000, True), (999, True)])
def test_r14_derives_from_the_identity_policy_row_not_a_constant(
    fenced_session_with_policy: FencedSessionWithPolicy, monkeypatch: pytest.MonkeyPatch, tokens_per_day: int, refused: bool
) -> None:
    """Mutation-derivation: the same 1000-token day flips on the policy row's tokens_per_day alone."""
    fenced = fenced_session_with_policy
    _pin_database_clock(monkeypatch, DAY + timedelta(hours=12))
    _record(fenced, _entry(600, 400), at=DAY + timedelta(hours=1))
    _set_tokens_per_day(fenced, fenced.identity_policy_id, tokens_per_day)
    decision = _assess(fenced)
    assert (decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED) is refused
    assert decision.evidence.quota_disposition is (QuotaDisposition.EXCEEDED if refused else QuotaDisposition.WITHIN_CAP)
    assert decision.evidence.cap == tokens_per_day


def test_r14_container_ceiling_binds_when_it_is_the_lower_bound(
    fenced_session_with_policy: FencedSessionWithPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation-derivation for the ceiling: lower the container row below the usage and the identity cap no longer admits."""
    fenced = fenced_session_with_policy
    _pin_database_clock(monkeypatch, DAY + timedelta(hours=12))
    _record(fenced, _entry(300, 300), at=DAY + timedelta(hours=1))
    assert _assess(fenced).allowed
    _set_tokens_per_day(fenced, fenced.container_policy_id, 600)
    decision = _assess(fenced)
    assert decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert (decision.evidence.cap, decision.evidence.ceiling, decision.evidence.usage) == (IDENTITY_TOKENS_PER_DAY, 600, 600)


@pytest.mark.parametrize(("clock", "refused"), [(DAY - timedelta(microseconds=1), True), (DAY, False)])
def test_r14_day_rolls_over_at_utc_midnight_on_the_database_clock(
    fenced_session_with_policy: FencedSessionWithPolicy, monkeypatch: pytest.MonkeyPatch, clock: datetime, refused: bool
) -> None:
    """Rollover (spec §Testing :1297-1298): a full day at 23:59:59.999999 stops counting at 00:00:00 UTC."""
    fenced = fenced_session_with_policy
    _record(fenced, _entry(800, 200), at=DAY - timedelta(hours=1))
    _pin_database_clock(monkeypatch, clock)
    decision = _assess(fenced)
    assert (decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED) is refused
    assert decision.evidence.usage == (1000 if refused else 0)


def test_r14_empty_ledger_admits_within_the_cap_as_measured_zero(
    fenced_session_with_policy: FencedSessionWithPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pin_database_clock(monkeypatch, DAY + timedelta(hours=12))
    decision = _assess(fenced_session_with_policy)
    assert decision.allowed
    assert decision.evidence.quota_disposition is QuotaDisposition.WITHIN_CAP
    assert decision.evidence.usage == 0


def test_r14_unknown_usage_refuses_as_accounting_unavailable(
    fenced_session_with_policy: FencedSessionWithPolicy, monkeypatch: pytest.MonkeyPatch
) -> None:
    fenced = fenced_session_with_policy
    _pin_database_clock(monkeypatch, DAY + timedelta(hours=12))
    _record(fenced, _entry(None, None), at=DAY + timedelta(hours=1))
    decision = _assess(fenced)
    assert decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
    assert decision.evidence.quota_disposition is QuotaDisposition.ACCOUNTING_UNAVAILABLE
    assert decision.evidence.usage is None


def test_r14_required_policy_missing_still_refuses_before_measuring(fenced_session: FencedSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_database_clock(monkeypatch, DAY + timedelta(hours=12))
    required = ChargeableAdmissionPolicy(identity_token_quota_configured=True, secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)
    decision = RepositoryChargeableAdmissionAuthority.assess(
        fenced_session.connection_token, session_id=fenced_session.session_id, policy=required
    )
    assert decision.refusal_reason is AdmissionRefusalReason.QUOTA_POLICY_MISSING
    assert decision.evidence.usage is None
```

- [ ] **Step 6: Run the QuotaAuthority tests to verify they fail.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination/test_quota_authority.py -n 0 > /tmp/i-lane-i1-step6.log 2>&1; echo exit=$?
```

Expected: `exit=2`. Collection stops at `ERROR collecting tests/unit/web/coordination/test_quota_authority.py` with `ModuleNotFoundError: No module named 'elspeth.web.coordination.quota_authority'`.

- [ ] **Step 7: Write the QuotaAuthority and make admission measure the day.**

Create `src/elspeth/web/coordination/quota_authority.py`:

```python
"""QuotaAuthority: the sole writer of ``token_usage_ledger`` and the reader of applicable quota policy.

R14 (sso-design.md:1161-1167) refuses chargeable work that would take an
identity over its ``tokens_per_day`` or the container ceiling. The measurement
lives here and nowhere else:

* ``record_token_usage_on_connection`` appends one ledger row per provider call,
  attributed to the session's owning identity, on the CALLER's connection so the
  accounting row commits with the audit row it was derived from. The three
  adapters are the Composer audit cohorts (``SessionServiceImpl.add_messages_atomic``,
  ``_insert_prepared_guided_audit_rows_on_connection``,
  ``RepositoryRunDiagnosticsAuditAuthority.append_audit_messages``), auto-title
  (``SessionServiceImpl.record_token_usage`` from ``_auto_title.py``) and run
  finalisation (the same service method from ``execution/service.py``).
* ``RepositoryQuotaAuthority.daily_token_total`` sums one identity's UTC day.
  NULL measures mean the provider reported no usage: unknown, never zero, so a
  day containing one returns ``None`` and admission refuses with
  ``token_accounting_unavailable``. A day with no rows measures ``0``: every
  adapter above writes before the next admission can read, so an absent row is
  absent spend, not missing accounting.
* ``RepositoryQuotaAuthority.active_policy`` locks and returns the identity
  policy row and the container ceiling row in force.

Sessions-side code never opens the Landscape. A refusal is reported to the
caller as a typed ``QuotaExceeded`` which the caller's ``record`` callback turns
into the Landscape ``quota_exceeded`` row (``AuthAuditWriter.record_quota_exceeded``).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, final

from sqlalchemy import case, func, insert, or_, select
from sqlalchemy.engine import Connection

from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.sessions.models import quota_policies_table, sessions_table, token_usage_ledger_table

TokenUsageSource = Literal["composer", "run", "auto_title"]
_TOKEN_USAGE_SOURCES: frozenset[str] = frozenset({"composer", "run", "auto_title"})


@final
@dataclass(frozen=True, slots=True)
class TokenUsageEntry:
    """One provider call's usage. ``None`` is an unreported measure, never zero."""

    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_prompt_tokens: int | None
    reasoning_tokens: int | None

    def __post_init__(self) -> None:
        if type(self.model) is not str or not self.model.strip():
            raise ValueError("TokenUsageEntry.model must be a non-blank exact string")
        for name, value in (
            ("prompt_tokens", self.prompt_tokens),
            ("completion_tokens", self.completion_tokens),
            ("cached_prompt_tokens", self.cached_prompt_tokens),
            ("reasoning_tokens", self.reasoning_tokens),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"TokenUsageEntry.{name} must be a non-negative exact int or None")


@final
@dataclass(frozen=True, slots=True)
class QuotaPolicyRow:
    policy_id: str
    tokens_per_day: int
    storage_bytes: int


@final
@dataclass(frozen=True, slots=True)
class ActiveQuotaPolicies:
    """The identity's unrevoked policy row and the unrevoked container ceiling row, either possibly absent."""

    identity: QuotaPolicyRow | None
    container: QuotaPolicyRow | None


@final
@dataclass(frozen=True, slots=True)
class QuotaExceeded:
    """A committed quota refusal, handed to the caller's ``record`` callback (spec :834)."""

    identity_id: str
    provider: AuthProviderType
    operation: str
    dimension: Literal["tokens", "storage"]
    cap: int | None
    ceiling: int | None
    usage: int
    identity_policy_id: str | None
    container_policy_id: str | None


def utc_day_start(now: datetime) -> datetime:
    """The UTC midnight that opens ``now``'s day: R14's day boundary (spec :1167)."""
    if type(now) is not datetime or now.tzinfo is None:
        raise ValueError("utc_day_start requires an aware datetime")
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def token_usage_entry_from_llm_call_envelope(envelope: Mapping[str, Any]) -> TokenUsageEntry | None:
    """Derive the ledger entry for one persisted ``llm_call_audit`` envelope.

    ELSPETH wrote the envelope (``composer/audit.py`` ``llm_call_audit_envelope``
    and the guided failure projection in ``sessions/guided_audit.py``), so its
    keys are read directly. A call that did not succeed and reported no usage
    returned no completion and is not charged; a successful call that reported
    no usage is charged as unknown (both measures NULL).
    """
    if envelope["_kind"] != "llm_call_audit":
        raise AuditIntegrityError("Token usage is derived only from an llm_call_audit envelope")
    call = envelope["call"]
    prompt_tokens = call["prompt_tokens"]
    completion_tokens = call["completion_tokens"]
    if call["status"] != "success" and prompt_tokens is None and completion_tokens is None:
        return None
    return TokenUsageEntry(
        model=call["model_requested"] if call["model_returned"] is None else call["model_returned"],
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cached_prompt_tokens=call["cached_prompt_tokens"],
        reasoning_tokens=call["reasoning_tokens"],
    )


def llm_call_usage_entries(tool_calls: Sequence[Mapping[str, Any]]) -> tuple[TokenUsageEntry, ...]:
    """Every chargeable entry among a cohort's ``tool_calls`` envelopes.

    Assistant rows carry OpenAI tool-call request dicts, which have no
    ``_kind`` key; only ``llm_call_audit`` envelopes are provider calls.
    """
    entries: list[TokenUsageEntry] = []
    for envelope in tool_calls:
        if "_kind" in envelope and envelope["_kind"] == "llm_call_audit":
            entry = token_usage_entry_from_llm_call_envelope(envelope)
            if entry is not None:
                entries.append(entry)
    return tuple(entries)


def record_token_usage_on_connection(
    connection: Connection,
    *,
    session_id: str,
    source: TokenUsageSource,
    run_id: str | None,
    entries: Sequence[TokenUsageEntry],
    recorded_at: datetime,
) -> tuple[str, ...]:
    """Append ledger rows for ``entries`` on the caller's connection; return the new entry ids.

    ``connection`` must be the transaction that wrote the evidence the entries
    were derived from, so the evidence and its accounting commit together.

    For ``source='run'`` the entries are the run's COMPLETE ordered LLM call
    list: rows already recorded for the run are a prefix of it, and only the
    suffix is appended. A resumed run therefore never charges a call twice.
    """
    if source not in _TOKEN_USAGE_SOURCES:
        raise ValueError(f"token usage source {source!r} is not one of composer, run, auto_title")
    if (source == "run") != (run_id is not None):
        raise ValueError("run_id is required for, and only for, source='run'")
    if type(recorded_at) is not datetime or recorded_at.tzinfo is None:
        raise ValueError("recorded_at must be an aware datetime")
    if any(type(entry) is not TokenUsageEntry for entry in entries):
        raise TypeError("entries must contain exact TokenUsageEntry instances")
    if not entries:
        return ()
    owner = connection.execute(select(sessions_table.c.user_id).where(sessions_table.c.id == session_id)).one_or_none()
    if owner is None:
        raise AuditIntegrityError("Token usage names a session that does not exist")
    pending: Sequence[TokenUsageEntry] = entries
    if run_id is not None:
        recorded = connection.execute(
            select(func.count())
            .select_from(token_usage_ledger_table)
            .where(token_usage_ledger_table.c.run_id == run_id, token_usage_ledger_table.c.source == "run")
        ).scalar_one()
        if recorded > len(entries):
            raise AuditIntegrityError("Run token usage ledger holds more rows than the run has LLM calls")
        pending = entries[recorded:]
    entry_ids: list[str] = []
    for entry in pending:
        entry_id = str(uuid.uuid4())
        connection.execute(
            insert(token_usage_ledger_table).values(
                entry_id=entry_id,
                identity_id=owner.user_id,
                source=source,
                session_id=session_id,
                run_id=run_id,
                model=entry.model,
                prompt_tokens=entry.prompt_tokens,
                completion_tokens=entry.completion_tokens,
                cached_prompt_tokens=entry.cached_prompt_tokens,
                reasoning_tokens=entry.reasoning_tokens,
                recorded_at=recorded_at,
            )
        )
        entry_ids.append(entry_id)
    return tuple(entry_ids)


@final
class RepositoryQuotaAuthority:
    """Token-bound quota reads and ledger writes inside the caller's fenced transaction."""

    @staticmethod
    def record_token_usage(
        connection_token: str,
        *,
        session_id: str,
        source: TokenUsageSource,
        run_id: str | None,
        entries: Sequence[TokenUsageEntry],
        recorded_at: datetime,
    ) -> tuple[str, ...]:
        return record_token_usage_on_connection(
            _resolve_mutation_connection(connection_token),
            session_id=session_id,
            source=source,
            run_id=run_id,
            entries=entries,
            recorded_at=recorded_at,
        )

    @staticmethod
    def daily_token_total(connection_token: str, *, identity_id: str, day_start_utc: datetime) -> int | None:
        """``prompt + completion`` over ``[day_start_utc, day_start_utc + 1 day)``; ``None`` when any row is unknown."""
        if utc_day_start(day_start_utc) != day_start_utc or day_start_utc.utcoffset() != timedelta(0):
            raise ValueError("day_start_utc must be a UTC midnight")
        ledger = token_usage_ledger_table.c
        measured = func.coalesce(ledger.prompt_tokens, 0) + func.coalesce(ledger.completion_tokens, 0)
        unknown = case((or_(ledger.prompt_tokens.is_(None), ledger.completion_tokens.is_(None)), 1), else_=0)
        row = (
            _resolve_mutation_connection(connection_token)
            .execute(
                select(
                    func.count().label("row_count"),
                    func.coalesce(func.sum(unknown), 0).label("unknown_count"),
                    func.coalesce(func.sum(measured), 0).label("measured_total"),
                ).where(
                    ledger.identity_id == identity_id,
                    ledger.recorded_at >= day_start_utc,
                    ledger.recorded_at < day_start_utc + timedelta(days=1),
                )
            )
            .one()
        )
        if row.row_count == 0:
            return 0
        if row.unknown_count:
            return None
        return int(row.measured_total)

    @staticmethod
    def active_policy(connection_token: str, *, identity_id: str) -> ActiveQuotaPolicies:
        """Lock and return the identity's unrevoked policy and the unrevoked container ceiling."""
        conn = _resolve_mutation_connection(connection_token)
        policy = quota_policies_table.c
        identity = conn.execute(
            select(policy.policy_id, policy.tokens_per_day, policy.storage_bytes)
            .where(policy.identity_id == identity_id, policy.revoked_at.is_(None))
            .with_for_update()
        ).one_or_none()
        container = conn.execute(
            select(policy.policy_id, policy.tokens_per_day, policy.storage_bytes)
            .where(policy.identity_id.is_(None), policy.revoked_at.is_(None))
            .with_for_update()
        ).one_or_none()
        return ActiveQuotaPolicies(
            identity=None
            if identity is None
            else QuotaPolicyRow(policy_id=identity.policy_id, tokens_per_day=identity.tokens_per_day, storage_bytes=identity.storage_bytes),
            container=None
            if container is None
            else QuotaPolicyRow(
                policy_id=container.policy_id, tokens_per_day=container.tokens_per_day, storage_bytes=container.storage_bytes
            ),
        )
```

`src/elspeth/web/coordination/chargeable_admission_authority.py` HEAD lines 12-19 read:

```python
    QuotaDisposition,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.sessions.models import identities_table, quota_policies_table, sessions_table


class RepositoryChargeableAdmissionAuthority:
```

Replace them with:

```python
    QuotaDisposition,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.coordination.quota_authority import RepositoryQuotaAuthority, utc_day_start
from elspeth.web.sessions.models import identities_table, sessions_table


class RepositoryChargeableAdmissionAuthority:
```

`src/elspeth/web/coordination/chargeable_admission_authority.py` HEAD lines 48-63 read:

```python
                    quota_disposition=QuotaDisposition.NOT_ASSESSED, secret_wiring_hash=policy.secret_wiring_hash
                ),
            )
        identity_policy = conn.execute(
            select(quota_policies_table.c.policy_id)
            .where(quota_policies_table.c.identity_id == session.user_id, quota_policies_table.c.revoked_at.is_(None))
            .with_for_update()
        ).one_or_none()
        container_policy = conn.execute(
            select(quota_policies_table.c.policy_id)
            .where(quota_policies_table.c.identity_id.is_(None), quota_policies_table.c.revoked_at.is_(None))
            .with_for_update()
        ).one_or_none()
        # Boot settings supply issuance defaults and required policy slots;
        # they do not disable an explicit operator-authored allowance row.
        if not policy.token_quota_configured and identity_policy is None and container_policy is None:
```

Replace them with:

```python
                    quota_disposition=QuotaDisposition.NOT_ASSESSED, secret_wiring_hash=policy.secret_wiring_hash
                ),
            )
        policies = RepositoryQuotaAuthority.active_policy(connection_token, identity_id=session.user_id)
        identity_policy = policies.identity
        container_policy = policies.container
        # Boot settings supply issuance defaults and required policy slots;
        # they do not disable an explicit operator-authored allowance row.
        if not policy.token_quota_configured and identity_policy is None and container_policy is None:
```

`src/elspeth/web/coordination/chargeable_admission_authority.py` HEAD lines 67-81 read:

```python
                    quota_disposition=QuotaDisposition.NOT_CONFIGURED, secret_wiring_hash=policy.secret_wiring_hash
                ),
            )
        missing = (policy.identity_token_quota_configured and identity_policy is None) or (
            policy.container_token_quota_configured and container_policy is None
        )
        return ChargeableAdmissionDecision(
            refusal_reason=AdmissionRefusalReason.QUOTA_POLICY_MISSING if missing else AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE,
            evidence=AdmissionPolicyEvidence(
                identity_policy_id=identity_policy.policy_id if identity_policy is not None else None,
                container_policy_id=container_policy.policy_id if container_policy is not None else None,
                quota_disposition=QuotaDisposition.POLICY_MISSING if missing else QuotaDisposition.ACCOUNTING_UNAVAILABLE,
                secret_wiring_hash=policy.secret_wiring_hash,
            ),
        )
```

Replace them with:

```python
                    quota_disposition=QuotaDisposition.NOT_CONFIGURED, secret_wiring_hash=policy.secret_wiring_hash
                ),
            )
        identity_policy_id = identity_policy.policy_id if identity_policy is not None else None
        container_policy_id = container_policy.policy_id if container_policy is not None else None
        missing = (policy.identity_token_quota_configured and identity_policy is None) or (
            policy.container_token_quota_configured and container_policy is None
        )
        if missing:
            return ChargeableAdmissionDecision(
                refusal_reason=AdmissionRefusalReason.QUOTA_POLICY_MISSING,
                evidence=AdmissionPolicyEvidence(
                    identity_policy_id=identity_policy_id,
                    container_policy_id=container_policy_id,
                    quota_disposition=QuotaDisposition.POLICY_MISSING,
                    secret_wiring_hash=policy.secret_wiring_hash,
                ),
            )
        # R14's day is the UTC day on the SESSIONS DATABASE clock (spec :1167),
        # never a replica's wall clock: two replicas must agree which day it is.
        usage = RepositoryQuotaAuthority.daily_token_total(
            connection_token, identity_id=session.user_id, day_start_utc=utc_day_start(database_now(conn))
        )
        if usage is None:
            return ChargeableAdmissionDecision(
                refusal_reason=AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE,
                evidence=AdmissionPolicyEvidence(
                    identity_policy_id=identity_policy_id,
                    container_policy_id=container_policy_id,
                    quota_disposition=QuotaDisposition.ACCOUNTING_UNAVAILABLE,
                    secret_wiring_hash=policy.secret_wiring_hash,
                ),
            )
        cap = identity_policy.tokens_per_day if identity_policy is not None else None
        ceiling = container_policy.tokens_per_day if container_policy is not None else None
        limit = min(bound for bound in (cap, ceiling) if bound is not None)
        # A call admitted below the limit may take the day past it: R14 is
        # eventually consistent on the same terms as R13 (spec :1166-1167).
        exceeded = usage >= limit
        return ChargeableAdmissionDecision(
            refusal_reason=AdmissionRefusalReason.QUOTA_EXCEEDED if exceeded else None,
            evidence=AdmissionPolicyEvidence(
                identity_policy_id=identity_policy_id,
                container_policy_id=container_policy_id,
                quota_disposition=QuotaDisposition.EXCEEDED if exceeded else QuotaDisposition.WITHIN_CAP,
                secret_wiring_hash=policy.secret_wiring_hash,
                dimension="tokens",
                cap=cap,
                ceiling=ceiling,
                usage=usage,
            ),
        )
```

- [ ] **Step 8: Run the coordination suite; the two pins on the old refusal must now fail.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination -n 0 > /tmp/i-lane-i1-step8.log 2>&1; echo exit=$?
```

Expected: `exit=1`, `3 failed, 547 passed`. The failures are exactly `test_cli_quota_admission.py::test_cli_quota_policy_is_not_disabled_by_absent_issuance_defaults[False-True]`, `[True-True]` and `test_durable_run_admission.py::test_enabled_quota_never_treats_empty_ledger_as_zero[True]`, each with `AssertionError: assert None is <AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE: 'token_accounting_unavailable'>`: an explicit policy over an empty ledger is now measured (WITHIN_CAP, usage 0) instead of refused. Every `test_quota_authority.py` test passes.

- [ ] **Step 9: Move the two pins to the measured behaviour.** An empty UTC day is measured zero; a missing required policy still refuses with `quota_policy_missing` and carries no usage.

`tests/unit/web/coordination/test_durable_run_admission.py` HEAD lines 7-13 read:

```python
from sqlalchemy import func, insert, select, update
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.contracts import SessionOperationFenceLost, SessionOperationKind, StartPermitState
from elspeth.web.coordination.repository import SessionDerivedCustodyError
```

Replace them with:

```python
from sqlalchemy import func, insert, select, update
from tests.fixtures.identities import ensure_test_identity

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy, QuotaDisposition
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.contracts import SessionOperationFenceLost, SessionOperationKind, StartPermitState
from elspeth.web.coordination.repository import SessionDerivedCustodyError
```

`tests/unit/web/coordination/test_durable_run_admission.py` HEAD lines 324-330 read:

```python


@pytest.mark.parametrize("with_policy", [False, True])
def test_enabled_quota_never_treats_empty_ledger_as_zero(engine, with_policy):
    authority, context, run, _ = _admission(engine)
    if with_policy:
        with engine.begin() as conn:
```

Replace them with:

```python


@pytest.mark.parametrize("with_policy", [False, True])
def test_enabled_quota_over_an_empty_ledger_admits_under_the_cap(engine, with_policy):
    """Task I1: every chargeable call now writes the ledger before the next admission reads it,
    so an empty UTC day is measured zero; a missing required policy still refuses."""
    authority, context, run, _ = _admission(engine)
    if with_policy:
        with engine.begin() as conn:
```

`tests/unit/web/coordination/test_durable_run_admission.py` HEAD lines 341-349 read:

```python
            )
    policy = ChargeableAdmissionPolicy(identity_token_quota_configured=True, secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)
    permit = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=policy))
    expected = AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE if with_policy else AdmissionRefusalReason.QUOTA_POLICY_MISSING
    assert permit.admission_decision.refusal_reason is expected
    assert permit.admission_decision.evidence.identity_policy_id == ("quota-alice" if with_policy else None)


def test_recovery_refuses_disabled_owner_without_rewriting_original_permit(engine):
```

Replace them with:

```python
            )
    policy = ChargeableAdmissionPolicy(identity_token_quota_configured=True, secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)
    permit = authority.mutate(context, lambda tx: tx.runs.issue_start_permit(run_id=run.id, policy=policy))
    evidence = permit.admission_decision.evidence
    if with_policy:
        assert permit.state is StartPermitState.START_PERMITTED
        assert permit.admission_decision.refusal_reason is None
        assert (evidence.quota_disposition, evidence.cap, evidence.ceiling, evidence.usage) == (QuotaDisposition.WITHIN_CAP, 1000, None, 0)
    else:
        assert permit.admission_decision.refusal_reason is AdmissionRefusalReason.QUOTA_POLICY_MISSING
        assert evidence.usage is None
    assert evidence.identity_policy_id == ("quota-alice" if with_policy else None)


def test_recovery_refuses_disabled_owner_without_rewriting_original_permit(engine):
```

`tests/unit/web/coordination/test_cli_quota_admission.py` HEAD lines 6-12 read:

```python
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy, ChargeableOperation, QuotaDisposition
from elspeth.web.config import WebSettings
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
```

Replace them with:

```python
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts.chargeable_admission import ChargeableAdmissionPolicy, ChargeableOperation, QuotaDisposition
from elspeth.web.config import WebSettings
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
```

`tests/unit/web/coordination/test_cli_quota_admission.py` HEAD lines 75-83 read:

```python
            context, lambda tx: tx.session.assess_chargeable_operation(policy=policy, operation=ChargeableOperation.COMPOSER)
        )
        if explicit_quota:
            assert decision.refusal_reason is AdmissionRefusalReason.TOKEN_ACCOUNTING_UNAVAILABLE
            assert decision.evidence.identity_policy_id == quota_id
            assert decision.evidence.quota_disposition is QuotaDisposition.ACCOUNTING_UNAVAILABLE
            # A revoked policy no longer applies: absence remains an explicit,
            # measured allowance, not a permanently enabled global switch.
            from datetime import UTC, datetime
```

Replace them with:

```python
            context, lambda tx: tx.session.assess_chargeable_operation(policy=policy, operation=ChargeableOperation.COMPOSER)
        )
        if explicit_quota:
            # Task I1: the CLI-authored row is measured, not merely noticed. No
            # chargeable call has run, so the UTC day measures zero under its cap.
            assert decision.allowed
            assert decision.evidence.identity_policy_id == quota_id
            assert decision.evidence.quota_disposition is QuotaDisposition.WITHIN_CAP
            assert (decision.evidence.cap, decision.evidence.ceiling, decision.evidence.usage) == (1000, None, 0)
            # A revoked policy no longer applies: absence remains an explicit,
            # measured allowance, not a permanently enabled global switch.
            from datetime import UTC, datetime
```

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination -n 0 > /tmp/i-lane-i1-step9.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `550 passed`.

- [ ] **Step 10: Write the failing audit-writer test.** It reads the LANDSCAPE engine through `_durable_rows`: `auth_events` is a Landscape table (`core/landscape/schema.py:2542`). `_RecordingAuditWriter` lists every `AuthAuditWriter` member explicitly, so it gains the new one.

`tests/unit/web/auth/test_audit.py` HEAD lines 1048-1050 read:

```python
        ("success", "dormant"),
        ("failure", "dormant"),
    ]
```

Replace them with:

```python
        ("success", "dormant"),
        ("failure", "dormant"),
    ]


def test_quota_exceeded_row_carries_dimension_cap_ceiling_and_usage(tmp_path: Any) -> None:
    """R14 (spec :834, :1297): the row names the dimension, the cap, the ceiling in force and the measured usage.

    Read from the LANDSCAPE engine: ``auth_events`` is a Landscape table
    (core/landscape/schema.py:2542), never a sessions table.
    """
    from elspeth.web.coordination.quota_authority import QuotaExceeded

    recorder, url = _durable_recorder(tmp_path)
    recorder.record_quota_exceeded(
        QuotaExceeded(
            identity_id="identity-1",
            provider="local",
            operation="composer",
            dimension="tokens",
            cap=1000,
            ceiling=5000,
            usage=1000,
            identity_policy_id="quota-identity",
            container_policy_id="quota-container",
        )
    )
    (row,) = _durable_rows(url)
    assert (row.event_type, row.outcome, row.provider, row.identity_id, row.failure_category) == (
        "quota_exceeded",
        "failure",
        "local",
        "identity-1",
        "quota_exceeded_tokens",
    )
    assert (row.user_id, row.username, row.request_id, row.client_host, row.user_agent) == (None, None, None, None, None)
    assert _metadata(row) == {
        "actor": "system",
        "operation": "composer",
        "dimension": "tokens",
        "cap": 1000,
        "ceiling": 5000,
        "usage": 1000,
        "identity_policy_id": "quota-identity",
        "container_policy_id": "quota-container",
    }
```

`tests/unit/web/auth/test_identity_admin_routes.py` HEAD lines 93-98 read:

```python

    def record_relationship_changed(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_relationship_changed", request, kwargs)

    def only(self, method: str) -> _AuditCall:
        matches = [call for call in self.calls if call.method == method]
```

Replace them with:

```python

    def record_relationship_changed(self, request: Request | None, **kwargs: Any) -> None:
        self._note("record_relationship_changed", request, kwargs)

    def record_quota_exceeded(self, outcome: Any) -> None:
        return None

    def only(self, method: str) -> _AuditCall:
        matches = [call for call in self.calls if call.method == method]
```

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py -n 0 > /tmp/i-lane-i1-step10.log 2>&1; echo exit=$?
```

Expected: `exit=1`, `1 failed, 82 passed`, with `AttributeError: 'AuthAuditRecorder' object has no attribute 'record_quota_exceeded'`.

- [ ] **Step 11: Add `record_quota_exceeded` to the writer Protocol, the operation enum and the recorder.**

`src/elspeth/web/auth/audit.py` HEAD lines 33-38 read:

```python

if TYPE_CHECKING:
    from elspeth.web.config import WebSettings


_slog = structlog.get_logger(__name__)
```

Replace them with:

```python

if TYPE_CHECKING:
    from elspeth.web.config import WebSettings
    from elspeth.web.coordination.quota_authority import QuotaExceeded


_slog = structlog.get_logger(__name__)
```

`src/elspeth/web/auth/audit.py` HEAD lines 253-256 read:

```python


AdminActivationCause = Literal["admin_activation", "pre_provision", "bootstrap"]
"""How an ``identity_activated`` admin row came to be written.
```

Replace them with:

```python

    # R13/R14 (identity sprint Tasks I1, I2): no ``request``. A quota refusal
    # is decided inside an admission transaction, below every HTTP handler,
    # and the row is written before that transaction commits (R4).
    def record_quota_exceeded(self, outcome: QuotaExceeded) -> None:
        """Write the Landscape ``quota_exceeded`` row for a committed quota refusal."""


AdminActivationCause = Literal["admin_activation", "pre_provision", "bootstrap"]
"""How an ``identity_activated`` admin row came to be written.
```

`src/elspeth/web/auth/audit.py` HEAD lines 285-290 read:

```python
    IDENTITY_DISABLED = "identity_disabled"
    ROLE_CHANGED = "role_changed"
    RELATIONSHIP_CHANGED = "relationship_changed"


def _bounded_text(value: str | None, *, max_length: int = MAX_AUTH_AUDIT_TEXT_LENGTH) -> str | None:
```

Replace them with:

```python
    IDENTITY_DISABLED = "identity_disabled"
    ROLE_CHANGED = "role_changed"
    RELATIONSHIP_CHANGED = "relationship_changed"
    QUOTA_EXCEEDED = "quota_exceeded"


def _bounded_text(value: str | None, *, max_length: int = MAX_AUTH_AUDIT_TEXT_LENGTH) -> str | None:
```

`src/elspeth/web/auth/audit.py` HEAD lines 1176-1181 read:

```python
                **provenance.request_columns,
            )


class _AdminProvenanceMetadata(TypedDict):
    """The metadata every admin-mutation row starts from; the two console keys are the L0-pinned names."""
```

Replace them with:

```python
                **provenance.request_columns,
            )

    def record_quota_exceeded(self, outcome: QuotaExceeded) -> None:
        """Write the ``quota_exceeded`` row: dimension, cap, ceiling in force and measured usage (spec :834).

        Anchored on the refused identity. ``user_id``/``username`` stay NULL
        because the refusal is the authority's own act, joined to the person's
        trail by ``identity_id`` exactly as ``record_identity_dormant`` is, and no
        request column is invented for a refusal decided below the handler.
        """
        with self._open_landscape(AuthAuditOperation.QUOTA_EXCEEDED) as db:
            RecorderFactory(db).auth_audit.record_auth_event(
                event_type="quota_exceeded",
                outcome="failure",
                provider=outcome.provider,
                identity_id=outcome.identity_id,
                user_id=None,
                username=None,
                failure_category=f"quota_exceeded_{outcome.dimension}",
                request_id=None,
                client_host=None,
                user_agent=None,
                metadata={
                    "actor": "system",
                    "operation": outcome.operation,
                    "dimension": outcome.dimension,
                    "cap": outcome.cap,
                    "ceiling": outcome.ceiling,
                    "usage": outcome.usage,
                    "identity_policy_id": outcome.identity_policy_id,
                    "container_policy_id": outcome.container_policy_id,
                },
            )


class _AdminProvenanceMetadata(TypedDict):
    """The metadata every admin-mutation row starts from; the two console keys are the L0-pinned names."""
```

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py -n 0 > /tmp/i-lane-i1-step11.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `83 passed`.

- [ ] **Step 12: Write the failing adapter tests.** Three new files, auto-title fakes that record usage (every auto-title fake now needs `record_token_usage`), and the six fabricated `llm_call_audit` envelope sites replaced with real ones. `test_quota_authority_postgres.py` and the `pg_fenced` fixture are written here and run in Step 16.

Create `tests/unit/web/sessions/test_token_usage_adapters.py`:

```python
"""Task I1 through the real SessionServiceImpl: the Composer ledger adapters and the R14 audit seam.

Every Composer provider call is charged in the transaction that makes its audit
row durable; auto-title and run spend come through ``record_token_usage``; and a
quota refusal reaches the ``quota_exceeded_recorder`` exactly once, before the
caller learns of it.
"""

from __future__ import annotations

import dataclasses
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy, ChargeableOperation
from elspeth.contracts.composer_llm_audit import ComposerLLMCall, ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.coordination import chargeable_admission_authority
from elspeth.web.coordination.contracts import StartPermitState
from elspeth.web.coordination.quota_authority import QuotaExceeded, TokenUsageEntry
from elspeth.web.coordination.sqlite_authority import SQLiteLocalSessionOperationAuthority
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY
from elspeth.web.sessions._persist_payload import AuditMessageDraft
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.guided_audit import prepare_guided_audit_rows
from elspeth.web.sessions.models import (
    quota_policies_table,
    session_operation_fences_table,
    token_usage_ledger_table,
)
from elspeth.web.sessions.protocol import CompositionStateData, RunDiagnosticsAuditAuthority, RunDiagnosticsAuditDraft
from elspeth.web.sessions.schema import initialize_session_schema
from elspeth.web.sessions.service import SessionServiceImpl
from elspeth.web.sessions.telemetry import build_sessions_telemetry
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.fenced_session import CONTAINER_TOKENS_PER_DAY, IDENTITY_TOKENS_PER_DAY, seed_token_policies
from tests.unit.web.conftest import _make_session as _make_session_row
from tests.unit.web.coordination.test_durable_run_admission import _admission
from tests.unit.web.sessions.guided_test_authority import DualFencedSessionServiceHarness

DAY = datetime(2026, 9, 13, tzinfo=UTC)
_REQUIRED = ChargeableAdmissionPolicy(identity_token_quota_configured=True, secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)


def _call(
    *, status: ComposerLLMCallStatus = ComposerLLMCallStatus.SUCCESS, prompt: int | None = None, completion: int | None = None
) -> ComposerLLMCall:
    failed = status is not ComposerLLMCallStatus.SUCCESS
    call = build_llm_call_record(
        model_requested="test/model",
        messages=[{"role": "user", "content": "prompt"}],
        tools=None,
        status=status,
        started_at=DAY,
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        error_class="TimeoutError" if failed else None,
        error_message="timed out" if failed else None,
    )
    return dataclasses.replace(call, prompt_tokens=prompt, completion_tokens=completion)


def _compose_context(session_id: str) -> SessionOperationContext:
    return SessionOperationContext(
        fence=SessionOperationFence(
            session_id=session_id, operation_id=f"ledger-{session_id}", lease_token=f"ledger-token-{session_id}", operation_epoch=1
        ),
        operation_kind=SessionOperationKind.COMPOSE,
    )


def _seed_compose_session(engine: Engine) -> tuple[str, SessionOperationContext]:
    session_id = str(uuid4())
    context = _compose_context(session_id)
    with engine.begin() as conn:
        _make_session_row(conn, session_id=session_id)
        conn.execute(
            insert(session_operation_fences_table).values(
                session_id=session_id,
                operation_id=context.fence.operation_id,
                lease_token=context.fence.lease_token,
                operation_kind=context.operation_kind.value,
                owner_instance_id="ledger-test-owner",
                operation_epoch=context.fence.operation_epoch,
                lease_expires_at=datetime.now(UTC) + timedelta(hours=1),
                released_at=None,
            )
        )
    return session_id, context


def _ledger(engine: Engine) -> list[tuple[Any, ...]]:
    with engine.connect() as conn:
        rows = conn.execute(select(token_usage_ledger_table)).all()
    return sorted(
        ((row.identity_id, row.session_id, row.source, row.run_id, row.model, row.prompt_tokens, row.completion_tokens) for row in rows),
        key=repr,
    )


@pytest.fixture
def harness(engine: Engine) -> DualFencedSessionServiceHarness:
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
    return DualFencedSessionServiceHarness(engine, telemetry=build_sessions_telemetry(), log=structlog.get_logger("test"))


# ── the Composer adapters ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_composer_cohort_charges_its_provider_calls_with_the_audit_rows(
    harness: DualFencedSessionServiceHarness, engine: Engine
) -> None:
    session_id, context = _seed_compose_session(engine)
    await harness.add_messages_atomic(
        UUID(session_id),
        (
            AuditMessageDraft(role="audit", content="measured", tool_calls=(llm_call_audit_envelope(_call(prompt=7, completion=3)),)),
            AuditMessageDraft(
                role="audit", content="timed out", tool_calls=(llm_call_audit_envelope(_call(status=ComposerLLMCallStatus.TIMEOUT)),)
            ),
            AuditMessageDraft(role="audit", content="tool", tool_calls=({"_kind": "audit", "invocation": {}},)),
        ),
        writer_principal="compose_loop",
        session_operation_context=context,
    )
    assert _ledger(engine) == [("test_user", session_id, "composer", None, "test/model", 7, 3)]


@pytest.mark.asyncio
async def test_composer_cohort_that_fails_after_charging_leaves_no_ledger_row(
    harness: DualFencedSessionServiceHarness, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger row rolls back with the audit cohort: accounting never outlives the evidence it was derived from."""
    session_id, context = _seed_compose_session(engine)

    def _fail_after_the_ledger_write(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("session bump failed after the ledger insert")

    monkeypatch.setattr(harness, "_session_mutations", _fail_after_the_ledger_write)
    with pytest.raises(RuntimeError, match="session bump failed after the ledger insert"):
        await harness.add_messages_atomic(
            UUID(session_id),
            (AuditMessageDraft(role="audit", content="measured", tool_calls=(llm_call_audit_envelope(_call(prompt=7, completion=3)),)),),
            writer_principal="compose_loop",
            session_operation_context=context,
        )
    assert _ledger(engine) == []


def test_guided_cohort_charges_its_llm_rows(harness: DualFencedSessionServiceHarness, engine: Engine) -> None:
    session_id, context = _seed_compose_session(engine)
    rows = prepare_guided_audit_rows(
        invocations=(), llm_calls=(_call(prompt=5, completion=6), _call(status=ComposerLLMCallStatus.TIMEOUT)), chat_turns=()
    )
    with harness._session_process_locked_begin(session_id) as conn, harness._session_write_lock(conn, session_id):
        harness._insert_prepared_guided_audit_rows_on_connection(
            conn,
            session_id=session_id,
            composition_state_id=None,
            audit_rows=rows,
            sequence_no=harness._reserve_sequence_range(conn, session_id, count=len(rows)),
            created_at=datetime.now(UTC),
            session_operation_context=context,
        )
    assert _ledger(engine) == [("test_user", session_id, "composer", None, "test/model", 5, 6)]


@pytest.mark.asyncio
async def test_run_diagnostics_cohort_charges_its_llm_rows(harness: DualFencedSessionServiceHarness, engine: Engine) -> None:
    session = await harness.create_session("alice", "Pipeline", "local")
    state = await harness.save_composition_state(session.id, CompositionStateData(is_valid=True), provenance="session_seed")
    run = await harness.create_run(session.id, state.id)
    authority = RunDiagnosticsAuditAuthority(run_id=run.id, session_id=session.id, state_id=state.id)
    await harness.add_run_diagnostics_audit_messages_atomic(
        authority,
        (
            RunDiagnosticsAuditDraft(
                content="explained", tool_calls=({**llm_call_audit_envelope(_call(prompt=12, completion=8)), "run_id": str(run.id)},)
            ),
        ),
    )
    assert _ledger(engine) == [("alice", str(session.id), "composer", None, "test/model", 12, 8)]


@pytest.mark.asyncio
async def test_record_token_usage_charges_auto_title_under_compose_authority(
    harness: DualFencedSessionServiceHarness, engine: Engine
) -> None:
    session_id, context = _seed_compose_session(engine)
    entry = TokenUsageEntry(model="openai/title", prompt_tokens=30, completion_tokens=6, cached_prompt_tokens=None, reasoning_tokens=None)
    entry_ids = await harness.record_token_usage(session_operation_context=context, source="auto_title", run_id=None, entries=(entry,))
    assert len(entry_ids) == 1
    assert _ledger(engine) == [("test_user", session_id, "auto_title", None, "openai/title", 30, 6)]


@pytest.mark.asyncio
async def test_record_token_usage_refuses_run_spend_under_compose_authority(
    harness: DualFencedSessionServiceHarness, engine: Engine
) -> None:
    _session_id, context = _seed_compose_session(engine)
    entry = TokenUsageEntry(model="openai/run", prompt_tokens=1, completion_tokens=1, cached_prompt_tokens=None, reasoning_tokens=None)
    with pytest.raises(ValueError, match="source='run' token usage requires execute authority"):
        await harness.record_token_usage(session_operation_context=context, source="run", run_id=uuid4(), entries=(entry,))
    assert _ledger(engine) == []


# ── R14's audit seam ──────────────────────────────────────────────────────


def _quota_service(tmp_path: Path, **recorder: Any) -> tuple[Engine, SQLiteLocalSessionOperationAuthority, SessionServiceImpl]:
    engine = create_session_engine(f"sqlite:///{tmp_path / 'sessions.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
        seed_token_policies(conn, identity_id="alice")
    authority = SQLiteLocalSessionOperationAuthority(engine)
    service = SessionServiceImpl(
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test"),
        session_operation_authority=authority,
        chargeable_admission_policy=_REQUIRED,
        **recorder,
    )
    return engine, authority, service


def _spend(engine: Engine, *, session_id: str, tokens: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            insert(token_usage_ledger_table).values(
                entry_id=str(uuid4()),
                identity_id="alice",
                source="composer",
                session_id=session_id,
                run_id=None,
                model="test/model",
                prompt_tokens=tokens,
                completion_tokens=0,
                cached_prompt_tokens=None,
                reasoning_tokens=None,
                recorded_at=DAY + timedelta(hours=1),
            )
        )


def _exceeded(operation: str) -> QuotaExceeded:
    return QuotaExceeded(
        identity_id="alice",
        provider="local",
        operation=operation,
        dimension="tokens",
        cap=IDENTITY_TOKENS_PER_DAY,
        ceiling=CONTAINER_TOKENS_PER_DAY,
        usage=IDENTITY_TOKENS_PER_DAY,
        identity_policy_id="quota-identity",
        container_policy_id="quota-container",
    )


@pytest.mark.asyncio
async def test_composer_quota_refusal_writes_quota_exceeded_before_returning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fire test for R14's audit half (spec :1161-1163): the refusal writes quota_exceeded with dimension=tokens."""
    recorded: list[QuotaExceeded] = []
    engine, authority, service = _quota_service(tmp_path, quota_exceeded_recorder=recorded.append)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="q", auth_provider_type="local", owner_instance_id="owner", lease_seconds=30
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=30
    )
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _conn: DAY + timedelta(hours=12))
    _spend(engine, session_id=str(session.id), tokens=IDENTITY_TOKENS_PER_DAY)
    decision = await service.assess_chargeable_operation(session_operation_context=context, operation=ChargeableOperation.COMPOSER)
    assert decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert recorded == [_exceeded("composer")]


@pytest.mark.asyncio
async def test_composer_quota_audit_derives_from_the_policy_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutation-derivation: one more token of allowance on the policy row and nothing is refused or audited."""
    recorded: list[QuotaExceeded] = []
    engine, authority, service = _quota_service(tmp_path, quota_exceeded_recorder=recorded.append)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="q", auth_provider_type="local", owner_instance_id="owner", lease_seconds=30
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=30
    )
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _conn: DAY + timedelta(hours=12))
    _spend(engine, session_id=str(session.id), tokens=IDENTITY_TOKENS_PER_DAY)
    with engine.begin() as conn:
        conn.execute(
            update(quota_policies_table)
            .where(quota_policies_table.c.policy_id == "quota-identity")
            .values(tokens_per_day=IDENTITY_TOKENS_PER_DAY + 1)
        )
    decision = await service.assess_chargeable_operation(session_operation_context=context, operation=ChargeableOperation.COMPOSER)
    assert decision.allowed
    assert recorded == []


@pytest.mark.asyncio
async def test_an_unwired_quota_recorder_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine, authority, service = _quota_service(tmp_path)
    session = authority.create_session_with_initial_fence(
        user_id="alice", title="q", auth_provider_type="local", owner_instance_id="owner", lease_seconds=30
    )
    context = authority.acquire(
        session_id=session.id, operation_kind=SessionOperationKind.COMPOSE, owner_instance_id="owner", lease_seconds=30
    )
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _conn: DAY + timedelta(hours=12))
    _spend(engine, session_id=str(session.id), tokens=IDENTITY_TOKENS_PER_DAY)
    with pytest.raises(AuditIntegrityError, match="quota_exceeded refusal for identity alice has no auth audit writer wired"):
        await service.assess_chargeable_operation(session_operation_context=context, operation=ChargeableOperation.COMPOSER)


@pytest.mark.asyncio
async def test_run_permit_quota_refusal_is_audited_once_across_replays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: list[QuotaExceeded] = []
    engine, _authority, service = _quota_service(tmp_path, quota_exceeded_recorder=recorded.append)
    _admission_authority, context, run, _envelope = _admission(engine)
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _conn: DAY + timedelta(hours=12))
    _spend(engine, session_id=str(run.session_id), tokens=IDENTITY_TOKENS_PER_DAY)
    first = await service.issue_run_start_permit(run.id, session_operation_context=context)
    replay = await service.issue_run_start_permit(run.id, session_operation_context=context)
    assert first.state is StartPermitState.REFUSED
    assert first.admission_decision is not None
    assert first.admission_decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert replay == first
    assert recorded == [_exceeded("run")]
```

Create `tests/unit/web/execution/test_run_token_ledger.py`:

```python
"""Task I1 run adapter: a run's Landscape LLM calls become token-ledger entries on every terminal path."""

from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

from elspeth.contracts import CallStatus, CallType, NodeType
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.landscape.schema import calls_table
from elspeth.web.coordination.quota_authority import TokenUsageEntry
from elspeth.web.execution import service as execution_service
from elspeth.web.execution.service import _run_token_usage_entries
from tests.fixtures.landscape import leader_coordination_token, make_factory, make_landscape_db

T0 = datetime(2026, 9, 13, 12, tzinfo=UTC)


def test_run_entries_are_the_llm_calls_in_creation_order_with_their_node_model() -> None:
    db = make_landscape_db()
    factory = make_factory(db)
    schema = SchemaConfig.from_dict({"mode": "observed"})
    run_id = "run-token-ledger"
    factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id=run_id)
    coordination = leader_coordination_token(factory, run_id)
    source = factory.data_flow.register_node(
        coordination_token=coordination,
        plugin_name="inline_blob",
        node_type=NodeType.SOURCE,
        plugin_version="1.0",
        config={},
        schema_config=schema,
    )
    openai = factory.data_flow.register_node(
        coordination_token=coordination,
        plugin_name="llm",
        node_type=NodeType.TRANSFORM,
        plugin_version="1.0",
        config={"model": "openai/run-model"},
        schema_config=schema,
    )
    azure = factory.data_flow.register_node(
        coordination_token=coordination,
        plugin_name="azure_llm",
        node_type=NodeType.TRANSFORM,
        plugin_version="1.0",
        config={"model": None, "deployment_name": "azure-deployment"},
        schema_config=schema,
    )
    _row, token = factory.data_flow.create_row_with_token(
        coordination_token=coordination,
        source_node_id=source.node_id,
        row_index=0,
        source_row_index=0,
        ingest_sequence=0,
        data={"text": "x"},
    )
    openai_state = factory.execution.record_completed_node_state(
        token_id=token.token_id,
        node_id=openai.node_id,
        coordination_token=coordination,
        step_index=1,
        input_data={"text": "x"},
        output_data={"label": 1},
        duration_ms=1.0,
    )
    azure_state = factory.execution.record_completed_node_state(
        token_id=token.token_id,
        node_id=azure.node_id,
        coordination_token=coordination,
        step_index=2,
        input_data={"text": "x"},
        output_data={"label": 2},
        duration_ms=1.0,
    )
    operation = factory.execution.begin_operation(coordination_token=coordination, node_id=source.node_id, operation_type="source_load")

    def insert_call(
        call_id: str,
        *,
        offset: int,
        call_type: CallType = CallType.LLM,
        status: CallStatus = CallStatus.SUCCESS,
        state_id: str | None = None,
        operation_id: str | None = None,
        prompt: int | None = None,
        completion: int | None = None,
    ) -> None:
        with db.write_connection() as conn:
            conn.execute(
                calls_table.insert().values(
                    call_id=call_id,
                    state_id=state_id,
                    operation_id=operation_id,
                    call_index=offset,
                    call_type=call_type.value,
                    status=status.value,
                    request_hash=f"{call_id}-request",
                    created_at=T0 + timedelta(seconds=offset),
                    prompt_tokens=prompt,
                    completion_tokens=completion,
                )
            )

    insert_call("c5-azure-reported-error", offset=5, status=CallStatus.ERROR, state_id=azure_state.state_id, prompt=3, completion=0)
    insert_call("c1-openai", offset=1, state_id=openai_state.state_id, prompt=10, completion=5)
    insert_call("c2-transport", offset=2, call_type=CallType.HTTP, state_id=openai_state.state_id, prompt=99, completion=99)
    insert_call("c3-openai-unreported-error", offset=3, status=CallStatus.ERROR, state_id=openai_state.state_id)
    insert_call("c4-operation-unreported", offset=4, operation_id=operation.operation_id)

    assert _run_token_usage_entries(db, landscape_run_id=run_id) == (
        TokenUsageEntry(model="openai/run-model", prompt_tokens=10, completion_tokens=5, cached_prompt_tokens=None, reasoning_tokens=None),
        TokenUsageEntry(model="inline_blob", prompt_tokens=None, completion_tokens=None, cached_prompt_tokens=None, reasoning_tokens=None),
        TokenUsageEntry(model="azure-deployment", prompt_tokens=3, completion_tokens=0, cached_prompt_tokens=None, reasoning_tokens=None),
    )


def test_every_terminal_run_path_charges_the_run_token_ledger() -> None:
    """Result, graceful shutdown and exception recovery each charge the run's calls (sso-design.md:1371)."""
    tree = ast.parse(Path(execution_service.__file__).read_text(encoding="utf-8"))
    impl = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ExecutionServiceImpl")
    methods = {node.name: node for node in impl.body if isinstance(node, ast.FunctionDef)}

    def charges(name: str) -> int:
        return sum(
            1
            for node in ast.walk(methods[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_record_run_token_usage"
        )

    assert (charges("_run_pipeline"), charges("_persist_failed_run_status")) == (2, 1)
```

Create `tests/testcontainer/web/test_quota_authority_postgres.py`:

```python
"""QuotaAuthority on PostgreSQL: the timestamptz day window, the NULL-aware aggregate and R14 (Task I1).

SQLite stores ``recorded_at`` as text and PostgreSQL as ``timestamptz``, so the
UTC-midnight boundary and the unknown-row count (a ``SUM`` over a ``CASE``
expression) are proven on the production dialect, not inferred from the unit run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.helpers.fenced_session import CONTAINER_TOKENS_PER_DAY, IDENTITY_TOKENS_PER_DAY, FencedSession, seed_token_policies

from elspeth.contracts.chargeable_admission import AdmissionRefusalReason, ChargeableAdmissionPolicy, QuotaDisposition
from elspeth.web.coordination import chargeable_admission_authority
from elspeth.web.coordination.chargeable_admission_authority import RepositoryChargeableAdmissionAuthority
from elspeth.web.coordination.mutation_connection_registry import _resolve_mutation_connection
from elspeth.web.coordination.quota_authority import RepositoryQuotaAuthority, TokenUsageEntry
from elspeth.web.secrets.wiring_policy import EMPTY_SECRET_WIRING_POLICY

pytestmark = pytest.mark.testcontainer

DAY = datetime(2026, 9, 13, tzinfo=UTC)


def _record(fenced: FencedSession, prompt: int | None, completion: int | None, *, at: datetime) -> None:
    RepositoryQuotaAuthority.record_token_usage(
        fenced.connection_token,
        session_id=fenced.session_id,
        source="composer",
        run_id=None,
        entries=(
            TokenUsageEntry(
                model="m", prompt_tokens=prompt, completion_tokens=completion, cached_prompt_tokens=None, reasoning_tokens=None
            ),
        ),
        recorded_at=at,
    )


def test_daily_total_window_and_unknown_measure_on_postgres(pg_fenced: FencedSession) -> None:
    _record(pg_fenced, 1000, 1000, at=DAY - timedelta(microseconds=1))
    _record(pg_fenced, 100, 50, at=DAY)
    _record(pg_fenced, 10, 5, at=DAY + timedelta(hours=23, minutes=59, seconds=59, microseconds=999999))
    _record(pg_fenced, 1000, 1000, at=DAY + timedelta(days=1))
    assert (
        RepositoryQuotaAuthority.daily_token_total(pg_fenced.connection_token, identity_id=pg_fenced.identity_id, day_start_utc=DAY) == 165
    )
    _record(pg_fenced, None, 5, at=DAY + timedelta(hours=3))
    assert (
        RepositoryQuotaAuthority.daily_token_total(pg_fenced.connection_token, identity_id=pg_fenced.identity_id, day_start_utc=DAY) is None
    )


def test_r14_refuses_and_derives_from_the_usage_on_postgres(pg_fenced: FencedSession, monkeypatch: pytest.MonkeyPatch) -> None:
    seed_token_policies(_resolve_mutation_connection(pg_fenced.connection_token), identity_id=pg_fenced.identity_id)
    monkeypatch.setattr(chargeable_admission_authority, "database_now", lambda _conn: DAY + timedelta(hours=12))
    policy = ChargeableAdmissionPolicy(secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash)
    _record(pg_fenced, 999, 0, at=DAY + timedelta(hours=1))
    within = RepositoryChargeableAdmissionAuthority.assess(pg_fenced.connection_token, session_id=pg_fenced.session_id, policy=policy)
    assert within.allowed
    assert within.evidence.quota_disposition is QuotaDisposition.WITHIN_CAP
    _record(pg_fenced, 1, 0, at=DAY + timedelta(hours=2))
    refused = RepositoryChargeableAdmissionAuthority.assess(pg_fenced.connection_token, session_id=pg_fenced.session_id, policy=policy)
    assert refused.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED
    assert (refused.evidence.cap, refused.evidence.ceiling, refused.evidence.usage) == (
        IDENTITY_TOKENS_PER_DAY,
        CONTAINER_TOKENS_PER_DAY,
        1000,
    )
```

`tests/unit/web/sessions/test_auto_title.py` HEAD lines 14-19 read:

```python
    QuotaDisposition,
)
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions.telemetry import _FakeCounter
```

Replace them with:

```python
    QuotaDisposition,
)
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.coordination.quota_authority import TokenUsageEntry
from elspeth.web.sessions import _auto_title
from elspeth.web.sessions.telemetry import _FakeCounter
```

`tests/unit/web/sessions/test_auto_title.py` HEAD lines 21-26 read:

```python
class _TitleService:
    def __init__(self) -> None:
        self.updates: list[tuple[object, str]] = []

    async def assess_chargeable_operation(
        self, *, session_operation_context: SessionOperationContext, operation: ChargeableOperation
```

Replace them with:

```python
class _TitleService:
    def __init__(self) -> None:
        self.updates: list[tuple[object, str]] = []
        self.usage: list[tuple[str, object, tuple[TokenUsageEntry, ...]]] = []

    async def record_token_usage(
        self,
        *,
        session_operation_context: SessionOperationContext,
        source: str,
        run_id: object,
        entries: tuple[TokenUsageEntry, ...],
    ) -> tuple[str, ...]:
        del session_operation_context
        self.usage.append((source, run_id, entries))
        return tuple(f"entry-{index}" for index in range(len(entries)))

    async def assess_chargeable_operation(
        self, *, session_operation_context: SessionOperationContext, operation: ChargeableOperation
```

`tests/unit/web/sessions/test_auto_title.py` HEAD lines 543-545 read:

```python
        )
    assert caught.value is cancellation
    assert counter.calls == [(1, {"exception_class": "CancelledError"}, None)]
```

Replace them with:

```python
        )
    assert caught.value is cancellation
    assert counter.calls == [(1, {"exception_class": "CancelledError"}, None)]


# ── Task I1: the auto-title token-ledger adapter ──────────────────────────


@pytest.mark.asyncio
async def test_auto_title_charges_a_returned_completion_even_when_the_title_is_rejected(monkeypatch) -> None:
    """R14 (sso-design.md:1161): a completion the gate discards was still spent."""
    response = ModelResponse(
        model="openai/returned",
        choices=[{"index": 0, "message": {"role": "assistant", "content": _LIVE_LEAK_COMPLETION}}],
        usage={"prompt_tokens": 21, "completion_tokens": 4, "total_tokens": 25},
    )
    service, _failed, _rejected = await _run_auto_title(monkeypatch, response)
    assert service.updates == []
    assert service.usage == [
        (
            "auto_title",
            None,
            (
                TokenUsageEntry(
                    model="openai/returned", prompt_tokens=21, completion_tokens=4, cached_prompt_tokens=None, reasoning_tokens=None
                ),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_auto_title_charges_an_unreported_completion_as_unknown_never_zero(monkeypatch) -> None:
    service, _failed, _rejected = await _run_auto_title(monkeypatch, _completion_without_finish("Useful Pipeline Title"))
    assert service.updates != []
    assert service.usage == [
        (
            "auto_title",
            None,
            (
                TokenUsageEntry(
                    model="openai/test", prompt_tokens=None, completion_tokens=None, cached_prompt_tokens=None, reasoning_tokens=None
                ),
            ),
        )
    ]


@pytest.mark.asyncio
async def test_auto_title_provider_timeout_charges_nothing(monkeypatch) -> None:
    async def _raise_timeout(**_kwargs: object) -> object:
        raise TimeoutError("title generation timed out")

    monkeypatch.setattr(_auto_title, "_AUTO_TITLE_FAILED_COUNTER", _FakeCounter())
    monkeypatch.setattr(_auto_title, "_litellm_acompletion", _raise_timeout)
    service = _TitleService()
    await _auto_title.maybe_auto_title_session(
        service=service,
        session_id=_TEST_SESSION_ID,
        user_message="Build a CSV pipeline",
        model="openai/test",
        temperature=None,
        seed=None,
        session_operation_context=_TEST_CONTEXT,
    )
    assert service.usage == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reported", [False, True])
async def test_auto_title_malformed_response_is_charged_only_when_it_reported_usage(monkeypatch, reported: bool) -> None:
    """Mutation-derivation for the malformed arm: the provider's usage report alone decides the charge."""
    response = (
        ModelResponse(choices=[], usage={"prompt_tokens": 9, "completion_tokens": 0, "total_tokens": 9})
        if reported
        else ModelResponse(choices=[])
    )
    service, failed, _rejected = await _run_auto_title(monkeypatch, response)
    assert failed.calls == [(1, {"exception_class": "MalformedResponseError"}, None)]
    expected = TokenUsageEntry(model="openai/test", prompt_tokens=9, completion_tokens=0, cached_prompt_tokens=None, reasoning_tokens=None)
    assert service.usage == ([("auto_title", None, (expected,))] if reported else [])
```

`tests/unit/web/sessions/test_auto_title_sampling_config.py` HEAD lines 16-21 read:

```python
    QuotaDisposition,
)
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind

_TEST_SESSION_ID = uuid4()
_TEST_CONTEXT = SessionOperationContext(
```

Replace them with:

```python
    QuotaDisposition,
)
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.coordination.quota_authority import TokenUsageEntry

_TEST_SESSION_ID = uuid4()
_TEST_CONTEXT = SessionOperationContext(
```

`tests/unit/web/sessions/test_auto_title_sampling_config.py` HEAD lines 32-37 read:

```python
class _TitleService:
    def __init__(self) -> None:
        self.updates: list[tuple[object, str]] = []

    async def assess_chargeable_operation(
        self, *, session_operation_context: SessionOperationContext, operation: ChargeableOperation
```

Replace them with:

```python
class _TitleService:
    def __init__(self) -> None:
        self.updates: list[tuple[object, str]] = []
        self.usage: list[tuple[str, object, tuple[TokenUsageEntry, ...]]] = []

    async def record_token_usage(
        self,
        *,
        session_operation_context: SessionOperationContext,
        source: str,
        run_id: object,
        entries: tuple[TokenUsageEntry, ...],
    ) -> tuple[str, ...]:
        del session_operation_context
        self.usage.append((source, run_id, entries))
        return tuple(f"entry-{index}" for index in range(len(entries)))

    async def assess_chargeable_operation(
        self, *, session_operation_context: SessionOperationContext, operation: ChargeableOperation
```

`tests/unit/web/sessions/test_auto_title_endpoint_affordance.py` HEAD lines 22-27 read:

```python
    QuotaDisposition,
)
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind

_SENTINEL_CREDENTIAL = "sk-auto-title-endpoint-affordance-sentinel"  # secret-scan: allow-this-line
_TEST_SESSION_ID = uuid4()
```

Replace them with:

```python
    QuotaDisposition,
)
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.coordination.quota_authority import TokenUsageEntry

_SENTINEL_CREDENTIAL = "sk-auto-title-endpoint-affordance-sentinel"  # secret-scan: allow-this-line
_TEST_SESSION_ID = uuid4()
```

`tests/unit/web/sessions/test_auto_title_endpoint_affordance.py` HEAD lines 39-44 read:

```python
class _TitleService:
    def __init__(self) -> None:
        self.updates: list[tuple[object, str]] = []

    async def assess_chargeable_operation(
        self, *, session_operation_context: SessionOperationContext, operation: ChargeableOperation
```

Replace them with:

```python
class _TitleService:
    def __init__(self) -> None:
        self.updates: list[tuple[object, str]] = []
        self.usage: list[tuple[str, object, tuple[TokenUsageEntry, ...]]] = []

    async def record_token_usage(
        self,
        *,
        session_operation_context: SessionOperationContext,
        source: str,
        run_id: object,
        entries: tuple[TokenUsageEntry, ...],
    ) -> tuple[str, ...]:
        del session_operation_context
        self.usage.append((source, run_id, entries))
        return tuple(f"entry-{index}" for index in range(len(entries)))

    async def assess_chargeable_operation(
        self, *, session_operation_context: SessionOperationContext, operation: ChargeableOperation
```

`tests/unit/web/sessions/test_persist_compose_turn.py` HEAD lines 6-11 read:

```python

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5
```

Replace them with:

```python

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5
```

`tests/unit/web/sessions/test_persist_compose_turn.py` HEAD lines 14-21 read:

```python
from sqlalchemy import insert, text

from elspeth.contracts.advisory_locks import ELSPETH_BLOB_CUSTODY_LOCK_CLASSID, ELSPETH_SESSIONS_LOCK_CLASSID
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions._persist_payload import StatePayload
from elspeth.web.sessions.models import session_operation_fences_table
```

Replace them with:

```python
from sqlalchemy import insert, text

from elspeth.contracts.advisory_locks import ELSPETH_BLOB_CUSTODY_LOCK_CLASSID, ELSPETH_SESSIONS_LOCK_CLASSID
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationFence, SessionOperationKind
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.coordination.contracts import SessionOperationFenceLost
from elspeth.web.sessions._persist_payload import StatePayload
from elspeth.web.sessions.models import session_operation_fences_table
```

`tests/unit/web/sessions/test_persist_compose_turn.py` HEAD lines 55-60 read:

```python
            operation_epoch=context.fence.operation_epoch,
            lease_expires_at=datetime.now(UTC) + timedelta(hours=1),
            released_at=None,
        )
    )
```

Replace them with:

```python
            operation_epoch=context.fence.operation_epoch,
            lease_expires_at=datetime.now(UTC) + timedelta(hours=1),
            released_at=None,
        )
    )


def _llm_call_envelope() -> dict[str, object]:
    """A real ``llm_call_audit`` envelope: the Task I1 ledger adapter reads every call field the writer emits."""
    return llm_call_audit_envelope(
        build_llm_call_record(
            model_requested="test/model",
            messages=[{"role": "user", "content": "prompt"}],
            tools=None,
            status=ComposerLLMCallStatus.SUCCESS,
            started_at=datetime(2026, 9, 13, tzinfo=UTC),
            started_ns=time.monotonic_ns(),
            temperature=None,
            seed=None,
        )
    )
```

`tests/unit/web/sessions/test_persist_compose_turn.py` HEAD lines 1166-1173 read:

```python
    await service.add_messages_atomic(
        session_uuid,
        (
            AuditMessageDraft(role="audit", content="a", tool_calls=({"_kind": "llm_call_audit"},)),
            AuditMessageDraft(role="audit", content="b", tool_calls=({"_kind": "llm_call_audit"},)),
            AuditMessageDraft(role="audit", content="c", tool_calls=({"_kind": "audit"},)),
        ),
        writer_principal="compose_loop",
```

Replace them with:

```python
    await service.add_messages_atomic(
        session_uuid,
        (
            AuditMessageDraft(role="audit", content="a", tool_calls=(_llm_call_envelope(),)),
            AuditMessageDraft(role="audit", content="b", tool_calls=(_llm_call_envelope(),)),
            AuditMessageDraft(role="audit", content="c", tool_calls=({"_kind": "audit"},)),
        ),
        writer_principal="compose_loop",
```

`tests/unit/web/sessions/test_service.py` HEAD lines 7-12 read:

```python
import errno
import os
import threading
import traceback
import uuid
from datetime import UTC, datetime
```

Replace them with:

```python
import errno
import os
import threading
import time
import traceback
import uuid
from datetime import UTC, datetime
```

`tests/unit/web/sessions/test_service.py` HEAD lines 16-23 read:

```python
from sqlalchemy import event, func, insert, select
from sqlalchemy.pool import StaticPool

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.execution.schemas import (
    RunAccounting,
    RunAccountingIntegrity,
```

Replace them with:

```python
from sqlalchemy import event, func, insert, select
from sqlalchemy.pool import StaticPool

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.execution.schemas import (
    RunAccounting,
    RunAccountingIntegrity,
```

`tests/unit/web/sessions/test_service.py` HEAD lines 88-93 read:

```python
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test"),
    )
```

Replace them with:

```python
        engine,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test"),
    )


def _llm_call_envelope() -> dict[str, object]:
    """A real ``llm_call_audit`` envelope: the Task I1 ledger adapter reads every call field the writer emits."""
    return llm_call_audit_envelope(
        build_llm_call_record(
            model_requested="test/model",
            messages=[{"role": "user", "content": "prompt"}],
            tools=None,
            status=ComposerLLMCallStatus.SUCCESS,
            started_at=datetime(2026, 9, 13, tzinfo=UTC),
            started_ns=time.monotonic_ns(),
            temperature=None,
            seed=None,
        )
    )
```

`tests/unit/web/sessions/test_service.py` HEAD lines 1588-1594 read:

```python
        record = await service.add_run_diagnostics_audit_message(
            authority,
            "diagnostics explanation audited",
            tool_calls=[{"_kind": "llm_call_audit", "run_id": str(run.id), "call": {}}],
        )

        assert record.role == "audit"
```

Replace them with:

```python
        record = await service.add_run_diagnostics_audit_message(
            authority,
            "diagnostics explanation audited",
            tool_calls=[{**_llm_call_envelope(), "run_id": str(run.id)}],
        )

        assert record.role == "audit"
```

`tests/unit/web/sessions/test_service.py` HEAD lines 1676-1682 read:

```python
        drafts = tuple(
            RunDiagnosticsAuditDraft(
                content=f"diagnostics call {i}",
                tool_calls=({"_kind": "llm_call_audit", "run_id": str(run.id), "call": {}},),
            )
            for i in range(3)
        )
```

Replace them with:

```python
        drafts = tuple(
            RunDiagnosticsAuditDraft(
                content=f"diagnostics call {i}",
                tool_calls=({**_llm_call_envelope(), "run_id": str(run.id)},),
            )
            for i in range(3)
        )
```

`tests/testcontainer/web/test_run_diagnostics_authority_postgres.py` HEAD lines 4-9 read:

```python

import asyncio
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
```

Replace them with:

```python

import asyncio
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
```

`tests/testcontainer/web/test_run_diagnostics_authority_postgres.py` HEAD lines 14-19 read:

```python
from tests.fixtures.identities import ensure_test_identity

import elspeth.web.coordination.run_diagnostics_authority as run_diagnostics_authority_module
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table
```

Replace them with:

```python
from tests.fixtures.identities import ensure_test_identity

import elspeth.web.coordination.run_diagnostics_authority as run_diagnostics_authority_module
from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.web.composer.audit import llm_call_audit_envelope
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.coordination.contracts import SessionOperationKind
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table
```

`tests/testcontainer/web/test_run_diagnostics_authority_postgres.py` HEAD lines 28-33 read:

```python
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


@pytest.fixture()
```

Replace them with:

```python
from elspeth.web.sessions.telemetry import build_sessions_telemetry

pytestmark = pytest.mark.testcontainer


def _llm_call_envelope() -> dict[str, object]:
    """A real ``llm_call_audit`` envelope: the Task I1 ledger adapter reads every call field the writer emits."""
    return llm_call_audit_envelope(
        build_llm_call_record(
            model_requested="test/model",
            messages=[{"role": "user", "content": "prompt"}],
            tools=None,
            status=ComposerLLMCallStatus.SUCCESS,
            started_at=datetime(2026, 9, 13, tzinfo=UTC),
            started_ns=time.monotonic_ns(),
            temperature=None,
            seed=None,
        )
    )


@pytest.fixture()
```

`tests/testcontainer/web/test_run_diagnostics_authority_postgres.py` HEAD lines 138-144 read:

```python
            diagnostics.add_run_diagnostics_audit_message(
                authority,
                "must not survive archive",
                tool_calls=[{"_kind": "llm_call_audit", "run_id": str(run.id)}],
            )
        )
        assert await asyncio.to_thread(diagnostics_attempted_lock.wait, 10), "diagnostics never attempted the session lock"
```

Replace them with:

```python
            diagnostics.add_run_diagnostics_audit_message(
                authority,
                "must not survive archive",
                tool_calls=[{**_llm_call_envelope(), "run_id": str(run.id)}],
            )
        )
        assert await asyncio.to_thread(diagnostics_attempted_lock.wait, 10), "diagnostics never attempted the session lock"
```

`tests/testcontainer/web/test_run_diagnostics_authority_postgres.py` HEAD lines 186-192 read:

```python
            diagnostics.add_run_diagnostics_audit_message(
                authority,
                "commits before archive",
                tool_calls=[{"_kind": "llm_call_audit", "run_id": str(run.id)}],
            )
        )
        assert await asyncio.to_thread(diagnostics_has_lock.wait, 10), "diagnostics never reached its locked insert"
```

Replace them with:

```python
            diagnostics.add_run_diagnostics_audit_message(
                authority,
                "commits before archive",
                tool_calls=[{**_llm_call_envelope(), "run_id": str(run.id)}],
            )
        )
        assert await asyncio.to_thread(diagnostics_has_lock.wait, 10), "diagnostics never reached its locked insert"
```

`tests/testcontainer/web/conftest.py` HEAD lines 12-21 read:

```python

import pytest
from sqlalchemy.engine import make_url
from tests.helpers.postgres_target import postgres_test_target, provisioned_postgres_url
from xdist import is_xdist_worker

from elspeth.web import aws_rds_trust

_SEQUENTIAL_TEST_COMMAND = (
    "CI=1 uv run --frozen pytest -q -n 0 -m testcontainer "
```

Replace them with:

```python

import pytest
from sqlalchemy.engine import make_url
from tests.fixtures.identities import ensure_test_identity
from tests.helpers.fenced_session import FencedSession
from tests.helpers.postgres_target import postgres_test_target, provisioned_postgres_url
from xdist import is_xdist_worker

from elspeth.web import aws_rds_trust
from elspeth.web.coordination.mutation_connection_registry import _register_mutation_connection, _unregister_mutation_connection
from elspeth.web.coordination.repository import PostgresSessionOperationRepository
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema

_SEQUENTIAL_TEST_COMMAND = (
    "CI=1 uv run --frozen pytest -q -n 0 -m testcontainer "
```

`tests/testcontainer/web/conftest.py` HEAD lines 214-216 read:

```python
        "ELSPETH_TEST_RDS_TRUST_UID": str(file_stat.st_uid),
        "ELSPETH_TEST_RDS_TRUST_MODE": str(stat.S_IMODE(file_stat.st_mode)),
    }
```

Replace them with:

```python
        "ELSPETH_TEST_RDS_TRUST_UID": str(file_stat.st_uid),
        "ELSPETH_TEST_RDS_TRUST_MODE": str(stat.S_IMODE(file_stat.st_mode)),
    }


@pytest.fixture
def pg_fenced(external_deployment_postgres_url: str) -> Iterator[FencedSession]:
    """``fenced_session`` on the shared PostgreSQL container: its own database, identity ``alice``, one open registered transaction."""
    database = f"fenced_{uuid.uuid4().hex}"
    control = create_session_engine(external_deployment_postgres_url, isolation_level="AUTOCOMMIT")
    with control.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    engine = create_session_engine(make_url(external_deployment_postgres_url).set(database=database).render_as_string(hide_password=False))
    try:
        initialize_session_schema(engine)
        with engine.begin() as conn:
            ensure_test_identity(conn, identity_id="alice")
        session = PostgresSessionOperationRepository(engine).create_session_with_initial_fence(
            user_id="alice", title="fenced", auth_provider_type="local", owner_instance_id="owner", lease_seconds=120
        )
        connection = engine.connect()
        transaction = connection.begin()
        token = _register_mutation_connection(connection)
        try:
            yield FencedSession(engine=engine, connection_token=token, identity_id="alice", session_id=str(session.id))
        finally:
            _unregister_mutation_connection(token)
            transaction.rollback()
            connection.close()
    finally:
        engine.dispose()
        with control.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
        control.dispose()
```

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/sessions/test_token_usage_adapters.py tests/unit/web/sessions/test_auto_title.py tests/unit/web/sessions/test_auto_title_sampling_config.py tests/unit/web/sessions/test_auto_title_endpoint_affordance.py tests/unit/web/sessions/test_persist_compose_turn.py tests/unit/web/sessions/test_service.py tests/unit/web/execution/test_run_token_ledger.py -n 0 > /tmp/i-lane-i1-step12.log 2>&1; echo exit=$?
```

Expected: `exit=2`. Collection stops at `ERROR collecting tests/unit/web/execution/test_run_token_ledger.py` with `ImportError: cannot import name '_run_token_usage_entries' from 'elspeth.web.execution.service'`. Without that file the same selection runs:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/sessions/test_token_usage_adapters.py tests/unit/web/sessions/test_auto_title.py tests/unit/web/sessions/test_auto_title_sampling_config.py tests/unit/web/sessions/test_auto_title_endpoint_affordance.py tests/unit/web/sessions/test_persist_compose_turn.py tests/unit/web/sessions/test_service.py -n 0 > /tmp/i-lane-i1-step12b.log 2>&1; echo exit=$?
```

Expected: `exit=1`, `12 failed, 228 passed`. The twelve failures are nine of the ten `test_token_usage_adapters.py` tests and the three new auto-title charging tests. The measured distinct errors are `AttributeError: 'DualFencedSessionServiceHarness' object has no attribute 'record_token_usage'`, `TypeError: SessionServiceImpl.__init__() got an unexpected keyword argument 'quota_exceeded_recorder'`, `Failed: DID NOT RAISE <class 'elspeth.contracts.errors.AuditIntegrityError'>`, and ledger / `service.usage` equality assertions whose right-hand side holds the row the adapter has not written yet. `test_composer_cohort_that_fails_after_charging_leaves_no_ledger_row` already passes: it is the rollback guard, and it goes red only if the ledger write escapes the audit transaction.

- [ ] **Step 13: Wire the adapters and the audit seam.** `sessions/service.py`: the recorder argument, the fail-closed default, audited permits, `record_token_usage`, and the two Composer cohort adapters.

`src/elspeth/web/sessions/service.py` HEAD lines 32-38 read:

```python
from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.blobs import BlobForkPlanEntry, BlobGuidedOperationWriteFence, fork_blob_id
from elspeth.contracts.blobs_inline import ResolvedBlobContent
from elspeth.contracts.chargeable_admission import ChargeableAdmissionDecision, ChargeableAdmissionPolicy, ChargeableOperation
from elspeth.contracts.composer_audit import ComposerToolStatus, PipelineDispatchAuditPayload
from elspeth.contracts.composer_interpretation import (
    InterpretationChoice,
```

Replace them with:

```python
from elspeth.contracts.auth import AuthProviderType
from elspeth.contracts.blobs import BlobForkPlanEntry, BlobGuidedOperationWriteFence, fork_blob_id
from elspeth.contracts.blobs_inline import ResolvedBlobContent
from elspeth.contracts.chargeable_admission import (
    AdmissionRefusalReason,
    ChargeableAdmissionDecision,
    ChargeableAdmissionPolicy,
    ChargeableOperation,
)
from elspeth.contracts.composer_audit import ComposerToolStatus, PipelineDispatchAuditPayload
from elspeth.contracts.composer_interpretation import (
    InterpretationChoice,
```

`src/elspeth/web/sessions/service.py` HEAD lines 97-103 read:

```python
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.repository import (
    PostgresSessionOperationRepository,
    SessionDerivedCustodyError,
```

Replace them with:

```python
    SessionOperationFenceLost,
    SessionOperationKind,
)
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.quota_authority import (
    QuotaExceeded,
    TokenUsageEntry,
    TokenUsageSource,
    llm_call_usage_entries,
    record_token_usage_on_connection,
)
from elspeth.web.coordination.repository import (
    PostgresSessionOperationRepository,
    SessionDerivedCustodyError,
```

`src/elspeth/web/sessions/service.py` HEAD lines 4379-4384 read:

```python
    return _proposal_event_record_from_row(row)


class SessionServiceImpl:
    """Concrete async session service backed by worker-dispatched SQLAlchemy Core."""
```

Replace them with:

```python
    return _proposal_event_record_from_row(row)


def _refuse_unrecorded_quota_exceeded(outcome: QuotaExceeded) -> None:
    """The recorder a service gets when none is wired: a quota refusal nobody audits fails closed (R4)."""
    raise AuditIntegrityError(f"quota_exceeded refusal for identity {outcome.identity_id} has no auth audit writer wired")


class SessionServiceImpl:
    """Concrete async session service backed by worker-dispatched SQLAlchemy Core."""
```

`src/elspeth/web/sessions/service.py` HEAD lines 4401-4406 read:

```python
        session_operation_lease_seconds: int = 30,
        runtime_preflight: SessionRuntimePreflight | None = None,
        chargeable_admission_policy: ChargeableAdmissionPolicy | None = None,
    ) -> None:
        from elspeth.web.coordination.audit_access_log_authority import RepositoryAuditAccessLogAuthority
```

Replace them with:

```python
        session_operation_lease_seconds: int = 30,
        runtime_preflight: SessionRuntimePreflight | None = None,
        chargeable_admission_policy: ChargeableAdmissionPolicy | None = None,
        quota_exceeded_recorder: Callable[[QuotaExceeded], None] = _refuse_unrecorded_quota_exceeded,
    ) -> None:
        from elspeth.web.coordination.audit_access_log_authority import RepositoryAuditAccessLogAuthority
```

`src/elspeth/web/sessions/service.py` HEAD lines 4419-4424 read:

```python
        self._chargeable_admission_policy = chargeable_admission_policy or ChargeableAdmissionPolicy(
            secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash
        )
        if owner_instance_id is not None and (type(owner_instance_id) is not str or not owner_instance_id.strip()):
            raise ValueError("owner_instance_id must be a nonblank exact string")
        if type(session_operation_lease_seconds) is not int or not 1 <= session_operation_lease_seconds <= 3600:
```

Replace them with:

```python
        self._chargeable_admission_policy = chargeable_admission_policy or ChargeableAdmissionPolicy(
            secret_wiring_hash=EMPTY_SECRET_WIRING_POLICY.canonical_hash
        )
        self._quota_exceeded_recorder = quota_exceeded_recorder
        if owner_instance_id is not None and (type(owner_instance_id) is not str or not owner_instance_id.strip()):
            raise ValueError("owner_instance_id must be a nonblank exact string")
        if type(session_operation_lease_seconds) is not int or not 1 <= session_operation_lease_seconds <= 3600:
```

`src/elspeth/web/sessions/service.py` HEAD lines 10053-10075 read:

```python
        )

    async def assess_run_start_admission(self, run_id: UUID, *, session_operation_context: SessionOperationContext) -> RunStartPermitRecord:
        return cast(
            "RunStartPermitRecord",
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.runs.assess_start_admission(run_id=run_id, policy=self._chargeable_admission_policy),
            ),
        )

    async def issue_run_start_permit(self, run_id: UUID, *, session_operation_context: SessionOperationContext) -> RunStartPermitRecord:
        return cast(
            "RunStartPermitRecord",
            await self._run_sync(
                self._session_operation_authority.mutate,
                session_operation_context,
                lambda transaction: transaction.runs.issue_start_permit(run_id=run_id, policy=self._chargeable_admission_policy),
            ),
        )

    async def observe_run_start_permit_for_cleanup(
```

Replace them with:

```python
        )

    async def assess_run_start_admission(self, run_id: UUID, *, session_operation_context: SessionOperationContext) -> RunStartPermitRecord:
        return await self._audited_start_admission(
            run_id,
            session_operation_context=session_operation_context,
            admit=lambda transaction: transaction.runs.assess_start_admission(run_id=run_id, policy=self._chargeable_admission_policy),
        )

    async def issue_run_start_permit(self, run_id: UUID, *, session_operation_context: SessionOperationContext) -> RunStartPermitRecord:
        return await self._audited_start_admission(
            run_id,
            session_operation_context=session_operation_context,
            admit=lambda transaction: transaction.runs.issue_start_permit(run_id=run_id, policy=self._chargeable_admission_policy),
        )

    async def _audited_start_admission(
        self,
        run_id: UUID,
        *,
        session_operation_context: SessionOperationContext,
        admit: Callable[[SessionOperationMutationTransaction], RunStartPermitRecord],
    ) -> RunStartPermitRecord:
        """Decide one permit; write ``quota_exceeded`` before commit only when THIS call refused on quota.

        The run facet appends the run's terminal ``failed`` event exactly once,
        when a refusal is first recorded (``_RepositoryRunMutations._record_admission_refusal``).
        A terminal event that already exists before ``admit`` runs therefore marks
        a replayed refusal, which is not audited a second time. The EXECUTE fence
        admits one holder, so no concurrent decision can race this read.
        """
        session = await self.get_session(UUID(session_operation_context.fence.session_id))
        already_terminal = any(event.event_type in SESSION_TERMINAL_RUN_STATUS_VALUES for event in await self.list_run_events(run_id))

        def _decide(transaction: SessionOperationMutationTransaction) -> RunStartPermitRecord:
            permit = admit(transaction)
            refusal = permit.execution_refusal or permit.admission_decision
            if not already_terminal and refusal is not None and refusal.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED:
                self._quota_exceeded_recorder(self._quota_exceeded_outcome(session, refusal, operation=ChargeableOperation.RUN))
            return permit

        return cast(
            "RunStartPermitRecord",
            await self._run_sync(self._session_operation_authority.mutate, session_operation_context, _decide),
        )

    @staticmethod
    def _quota_exceeded_outcome(
        session: SessionRecord, decision: ChargeableAdmissionDecision, *, operation: ChargeableOperation
    ) -> QuotaExceeded:
        evidence = decision.evidence
        if decision.refusal_reason is not AdmissionRefusalReason.QUOTA_EXCEEDED or evidence.dimension is None or evidence.usage is None:
            raise AuditIntegrityError("Only a measured quota_exceeded decision has a quota_exceeded audit row")
        return QuotaExceeded(
            identity_id=session.user_id,
            provider=session.auth_provider_type,
            operation=operation.value,
            dimension=evidence.dimension,
            cap=evidence.cap,
            ceiling=evidence.ceiling,
            usage=evidence.usage,
            identity_policy_id=evidence.identity_policy_id,
            container_policy_id=evidence.container_policy_id,
        )

    async def observe_run_start_permit_for_cleanup(
```

`src/elspeth/web/sessions/service.py` HEAD lines 10087-10093 read:

```python
    async def assess_chargeable_operation(
        self, *, session_operation_context: SessionOperationContext, operation: ChargeableOperation
    ) -> ChargeableAdmissionDecision:
        return cast(
            "ChargeableAdmissionDecision",
            await self._run_sync(
                self._session_operation_authority.mutate,
```

Replace them with:

```python
    async def assess_chargeable_operation(
        self, *, session_operation_context: SessionOperationContext, operation: ChargeableOperation
    ) -> ChargeableAdmissionDecision:
        decision = cast(
            "ChargeableAdmissionDecision",
            await self._run_sync(
                self._session_operation_authority.mutate,
```

`src/elspeth/web/sessions/service.py` HEAD lines 10097-10102 read:

```python
                ),
            ),
        )

    async def request_run_cancellation(
        self, run_id: UUID, *, session_id: UUID, user_id: str, auth_provider_type: AuthProviderType
```

Replace them with:

```python
                ),
            ),
        )
        if decision.refusal_reason is AdmissionRefusalReason.QUOTA_EXCEEDED:
            # A Composer or auto-title refusal writes no sessions row, so its
            # audit row follows the read-only admission transaction and still
            # precedes the caller learning of the refusal (R4).
            session = await self.get_session(UUID(session_operation_context.fence.session_id))
            await self._run_sync(self._quota_exceeded_recorder, self._quota_exceeded_outcome(session, decision, operation=operation))
        return decision

    async def record_token_usage(
        self,
        *,
        session_operation_context: SessionOperationContext,
        source: TokenUsageSource,
        run_id: UUID | None,
        entries: tuple[TokenUsageEntry, ...],
    ) -> tuple[str, ...]:
        """Charge provider calls whose evidence is not a Composer audit cohort (Task I1).

        Auto-title spends under COMPOSE authority; a run's LLM calls are charged
        under the run's EXECUTE authority. Composer cohorts are charged inside
        their own audit transaction and never come through here.
        """
        if type(session_operation_context) is not SessionOperationContext:
            raise TypeError("session_operation_context must be an exact SessionOperationContext")
        expected_kind = SessionOperationKind.EXECUTE if source == "run" else SessionOperationKind.COMPOSE
        if session_operation_context.operation_kind is not expected_kind:
            raise ValueError(f"source={source!r} token usage requires {expected_kind.value} authority")
        if not entries:
            return ()
        sid = session_operation_context.fence.session_id

        def _sync() -> tuple[str, ...]:
            with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
                self._require_session_operation_context_on_connection(
                    conn,
                    session_operation_context,
                    session_id=sid,
                    expected_kind=expected_kind,
                    now=self._guided_database_now(conn),
                )
                if (
                    run_id is not None
                    and conn.execute(select(runs_table.c.session_id).where(runs_table.c.id == str(run_id))).scalar_one_or_none() != sid
                ):
                    raise AuditIntegrityError("Run token usage names a run outside the operation's session")
                return record_token_usage_on_connection(
                    conn,
                    session_id=sid,
                    source=source,
                    run_id=None if run_id is None else str(run_id),
                    entries=entries,
                    recorded_at=database_now(conn),
                )

        return cast("tuple[str, ...]", await self._run_sync(_sync))

    async def request_run_cancellation(
        self, run_id: UUID, *, session_id: UUID, user_id: str, auth_provider_type: AuthProviderType
```

`src/elspeth/web/sessions/service.py` HEAD lines 10728-10733 read:

```python
                )
            )
            sequence_no += 1
        return tuple(records)

    async def seed_or_complete_guided_start_operation(
```

Replace them with:

```python
                )
            )
            sequence_no += 1
        entries = llm_call_usage_entries(tuple(audit_row.envelope for audit_row in audit_rows if audit_row.kind == "llm"))
        if entries:
            # Task I1 Composer adapter (guided): the calls this cohort audits are
            # charged in the transaction that makes their audit rows durable.
            record_token_usage_on_connection(
                conn, session_id=session_id, source="composer", run_id=None, entries=entries, recorded_at=database_now(conn)
            )
        return tuple(records)

    async def seed_or_complete_guided_start_operation(
```

`src/elspeth/web/sessions/service.py` HEAD lines 14293-14298 read:

```python
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
            with self._session_mutations(conn, session_id=sid, session_operation_context=session_operation_context) as session_mutations:
                session_mutations.mark_session_updated(updated_at=now)
```

Replace them with:

```python
                    created_at=now,
                    session_operation_context=session_operation_context,
                )
            entries = llm_call_usage_entries(
                tuple(envelope for draft in drafts if draft.tool_calls is not None for envelope in draft.tool_calls)
            )
            if entries:
                # Task I1 Composer adapter (compose loop, turn cohort, planner
                # evidence): charged in the transaction that makes the audit rows durable.
                record_token_usage_on_connection(
                    conn, session_id=sid, source="composer", run_id=None, entries=entries, recorded_at=database_now(conn)
                )
            with self._session_mutations(conn, session_id=sid, session_operation_context=session_operation_context) as session_mutations:
                session_mutations.mark_session_updated(updated_at=now)
```

`sessions/protocol.py`: the Protocol member (the docstring is its body, so `SessionServiceProtocol` fakes built with `create_autospec` gain it automatically).

`src/elspeth/web/sessions/protocol.py` HEAD lines 73-78 read:

```python
    from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
    from elspeth.web.composer.pipeline_planner import PipelinePlanResult
    from elspeth.web.composer.pipeline_proposal import PipelineProposal, ProposalBase
    from elspeth.web.execution.envelope import RunExecutionInput
    from elspeth.web.sessions._persist_payload import AuditMessageDraft
```

Replace them with:

```python
    from elspeth.web.composer.pipeline_commit import PipelineDispatchAuditBinding
    from elspeth.web.composer.pipeline_planner import PipelinePlanResult
    from elspeth.web.composer.pipeline_proposal import PipelineProposal, ProposalBase
    from elspeth.web.coordination.quota_authority import TokenUsageEntry, TokenUsageSource
    from elspeth.web.execution.envelope import RunExecutionInput
    from elspeth.web.sessions._persist_payload import AuditMessageDraft
```

`src/elspeth/web/sessions/protocol.py` HEAD lines 4691-4693 read:

```python

    async def request_run_cancellation(
        self, run_id: UUID, *, session_id: UUID, user_id: str, auth_provider_type: AuthProviderType
```

Replace them with:

```python

    async def record_token_usage(
        self,
        *,
        session_operation_context: SessionOperationContext,
        source: TokenUsageSource,
        run_id: UUID | None,
        entries: tuple[TokenUsageEntry, ...],
    ) -> tuple[str, ...]:
        """Charge auto-title (COMPOSE) or run (EXECUTE) provider calls to the session owner's token ledger."""

    async def request_run_cancellation(
        self, run_id: UUID, *, session_id: UUID, user_id: str, auth_provider_type: AuthProviderType
```

`coordination/run_diagnostics_authority.py`: the run-diagnostics cohort adapter.

`src/elspeth/web/coordination/run_diagnostics_authority.py` HEAD lines 10-15 read:

```python
from sqlalchemy import Engine, func, insert, select, update

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table, runs_table, sessions_table
from elspeth.web.sessions.protocol import (
```

Replace them with:

```python
from sqlalchemy import Engine, func, insert, select, update

from elspeth.contracts.freeze import deep_thaw
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.coordination.quota_authority import llm_call_usage_entries, record_token_usage_on_connection
from elspeth.web.sessions.locking import locked_session_transaction
from elspeth.web.sessions.models import chat_messages_table, runs_table, sessions_table
from elspeth.web.sessions.protocol import (
```

`src/elspeth/web/coordination/run_diagnostics_authority.py` HEAD lines 133-138 read:

```python
                        created_at=now,
                    )
                )
            updated = conn.execute(
                update(sessions_table).where(sessions_table.c.id == sid, sessions_table.c.archived_at.is_(None)).values(updated_at=now)
            )
```

Replace them with:

```python
                        created_at=now,
                    )
                )
            entries = llm_call_usage_entries(tuple(envelope for row in rows if row.tool_calls is not None for envelope in row.tool_calls))
            if entries:
                # Task I1 Composer adapter (run diagnostics): the explanation's
                # provider calls are charged with their audit rows.
                record_token_usage_on_connection(
                    conn, session_id=sid, source="composer", run_id=None, entries=entries, recorded_at=database_now(conn)
                )
            updated = conn.execute(
                update(sessions_table).where(sessions_table.c.id == sid, sessions_table.c.archived_at.is_(None)).values(updated_at=now)
            )
```

`sessions/_auto_title.py`: charge every returned completion; keep one `except` clause so the tier-model corpus does not grow (Step 17).

`src/elspeth/web/sessions/_auto_title.py` HEAD lines 38-44 read:

```python
from elspeth.contracts.chargeable_admission import ChargeableOperation
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer.service import _apply_endpoint_kwargs, _litellm_acompletion
from elspeth.web.validation import _redact_sensitive_content, reject_credential_shaped_content

if TYPE_CHECKING:
```

Replace them with:

```python
from elspeth.contracts.chargeable_admission import ChargeableOperation
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationContext
from elspeth.web.composer.llm_response_parsing import safe_response_model, token_usage_from_response
from elspeth.web.composer.service import _apply_endpoint_kwargs, _litellm_acompletion
from elspeth.web.coordination.quota_authority import TokenUsageEntry
from elspeth.web.validation import _redact_sensitive_content, reject_credential_shaped_content

if TYPE_CHECKING:
```

`src/elspeth/web/sessions/_auto_title.py` HEAD lines 263-268 read:

```python
    return _AdmittedAutoTitleCompletion(content=content, finish_reason=admitted_finish_reason)


async def maybe_auto_title_session(
    *,
    service: SessionServiceProtocol,
```

Replace them with:

```python
    return _AdmittedAutoTitleCompletion(content=content, finish_reason=admitted_finish_reason)


async def _charge_auto_title_response(
    service: SessionServiceProtocol,
    session_operation_context: SessionOperationContext,
    *,
    model: str,
    response: object,
    reported_only: bool,
) -> None:
    """Charge one returned auto-title completion to the token ledger (R14, Task I1).

    A returned completion is spend whether or not it makes a usable title, and
    unreported usage is charged as unknown, never as zero. A response the gate
    rejected as malformed is charged only when it reported usage: without a
    report it returned no completion.
    """
    usage = token_usage_from_response(response)
    if reported_only and usage.prompt_tokens is None and usage.completion_tokens is None:
        return
    returned_model = safe_response_model(response)
    await service.record_token_usage(
        session_operation_context=session_operation_context,
        source="auto_title",
        run_id=None,
        entries=(
            TokenUsageEntry(
                model=model if returned_model is None else returned_model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cached_prompt_tokens=usage.cached_prompt_tokens,
                reasoning_tokens=usage.reasoning_tokens,
            ),
        ),
    )


async def maybe_auto_title_session(
    *,
    service: SessionServiceProtocol,
```

`src/elspeth/web/sessions/_auto_title.py` HEAD lines 314-319 read:

```python
    if seed is not None:
        kwargs["seed"] = seed
    _apply_endpoint_kwargs(kwargs, base_url=api_base, api_key=api_key)
    try:
        response = await _litellm_acompletion(**kwargs)
        admitted = _admit_auto_title_completion(response)
```

Replace them with:

```python
    if seed is not None:
        kwargs["seed"] = seed
    _apply_endpoint_kwargs(kwargs, base_url=api_base, api_key=api_key)
    response: object | None = None
    try:
        response = await _litellm_acompletion(**kwargs)
        admitted = _admit_auto_title_completion(response)
```

`src/elspeth/web/sessions/_auto_title.py` HEAD lines 325-332 read:

```python
        # scheduling failures, but those failures still need an operational
        # signal so "provider declined" does not look identical to "feature
        # silently broke."
        _record_auto_title_failure(exc)
        return
    if admitted.content is None:
        return
    candidate = _admit_title_candidate(admitted.content)
```

Replace them with:

```python
        # scheduling failures, but those failures still need an operational
        # signal so "provider declined" does not look identical to "feature
        # silently broke."
        if response is not None:
            await _charge_auto_title_response(service, session_operation_context, model=model, response=response, reported_only=True)
        _record_auto_title_failure(exc)
        return
    await _charge_auto_title_response(service, session_operation_context, model=model, response=response, reported_only=False)
    if admitted.content is None:
        return
    candidate = _admit_title_candidate(admitted.content)
```

`execution/service.py`: the run adapter and its three terminal call sites (result, graceful shutdown, exception recovery).

`src/elspeth/web/execution/service.py` HEAD lines 33-39 read:

```python
import structlog
from opentelemetry import metrics
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from elspeth.config_loading import load_settings_from_config_dict, load_settings_from_yaml_string
```

Replace them with:

```python
import structlog
from opentelemetry import metrics
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import and_, select
from sqlalchemy.exc import SQLAlchemyError

from elspeth.config_loading import load_settings_from_config_dict, load_settings_from_yaml_string
```

`src/elspeth/web/execution/service.py` HEAD lines 42-48 read:

```python
from elspeth.contracts.aws_textract import TextractProfiledAuditIdentities
from elspeth.contracts.chargeable_admission import ChargeableAdmissionDecision, ChargeableAdmissionRefused, ChargeableOperation
from elspeth.contracts.cli import ProgressEvent
from elspeth.contracts.enums import NodeStateStatus, RunStatus, is_llm_authored_creation_modality
from elspeth.contracts.errors import AuditIntegrityError, GracefulShutdownError, IncompleteSourceResumeError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.plugin_capabilities import PluginCapability
```

Replace them with:

```python
from elspeth.contracts.aws_textract import TextractProfiledAuditIdentities
from elspeth.contracts.chargeable_admission import ChargeableAdmissionDecision, ChargeableAdmissionRefused, ChargeableOperation
from elspeth.contracts.cli import ProgressEvent
from elspeth.contracts.enums import CallStatus, CallType, NodeStateStatus, RunStatus, is_llm_authored_creation_modality
from elspeth.contracts.errors import AuditIntegrityError, GracefulShutdownError, IncompleteSourceResumeError
from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.plugin_capabilities import PluginCapability
```

`src/elspeth/web/execution/service.py` HEAD lines 66-72 read:

```python
from elspeth.core.events import EventBus
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_lifecycle_repository import is_valid_sha256_hex
from elspeth.core.landscape.schema import node_states_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.core.secrets import SecretResolutionError
from elspeth.engine.orchestrator.core import Orchestrator
```

Replace them with:

```python
from elspeth.core.events import EventBus
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.run_lifecycle_repository import is_valid_sha256_hex
from elspeth.core.landscape.schema import calls_table, node_states_table, nodes_table, operations_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.core.secrets import SecretResolutionError
from elspeth.engine.orchestrator.core import Orchestrator
```

`src/elspeth/web/execution/service.py` HEAD lines 97-102 read:

```python
from elspeth.web.config import WebSettings
from elspeth.web.coordination.contracts import RecoveryRequiredReason, SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.execution._semantic_helpers import semantic_affected_component_id
from elspeth.web.execution.accounting import load_run_accounting_from_db
from elspeth.web.execution.completion_gates import (
```

Replace them with:

```python
from elspeth.web.config import WebSettings
from elspeth.web.coordination.contracts import RecoveryRequiredReason, SessionOperationFenceLost
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.coordination.quota_authority import TokenUsageEntry
from elspeth.web.execution._semantic_helpers import semantic_affected_component_id
from elspeth.web.execution.accounting import load_run_accounting_from_db
from elspeth.web.execution.completion_gates import (
```

`src/elspeth/web/execution/service.py` HEAD lines 723-728 read:

```python
    RunStatus.FAILED: "failed",
    RunStatus.EMPTY: "empty",
}


def _session_status_from_run_result_status(status: RunStatus) -> SessionRunStatus:
```

Replace them with:

```python
    RunStatus.FAILED: "failed",
    RunStatus.EMPTY: "empty",
}


def _run_token_usage_entries(landscape_db: LandscapeDB, *, landscape_run_id: str) -> tuple[TokenUsageEntry, ...]:
    """The run's LLM calls in creation order, as token-ledger entries (Task I1 run adapter).

    Reads the Landscape ``calls`` token columns (epoch 40): ``call_type='llm'``
    rows are the logical LLM calls, parented by a node state or by an operation;
    transport rows never carry the charge (sso-design.md:855). The model is the
    calling node's configured ``model`` (Azure configures ``deployment_name``),
    and the plugin name when the node configures neither. A call that errored
    and reported no usage returned no completion and is not charged; a
    successful call without usage is charged as unknown.
    """
    measures = (
        calls_table.c.call_id,
        calls_table.c.created_at,
        calls_table.c.status,
        calls_table.c.prompt_tokens,
        calls_table.c.completion_tokens,
        calls_table.c.cached_prompt_tokens,
        calls_table.c.reasoning_tokens,
        nodes_table.c.plugin_name,
        nodes_table.c.config_json,
    )
    with landscape_db.read_only_connection() as conn:
        state_calls = conn.execute(
            select(*measures)
            .select_from(
                calls_table.join(node_states_table, calls_table.c.state_id == node_states_table.c.state_id).join(
                    nodes_table,
                    and_(nodes_table.c.node_id == node_states_table.c.node_id, nodes_table.c.run_id == node_states_table.c.run_id),
                )
            )
            .where(node_states_table.c.run_id == landscape_run_id, calls_table.c.call_type == CallType.LLM.value)
        ).all()
        operation_calls = conn.execute(
            select(*measures)
            .select_from(
                calls_table.join(operations_table, calls_table.c.operation_id == operations_table.c.operation_id).join(
                    nodes_table,
                    and_(nodes_table.c.node_id == operations_table.c.node_id, nodes_table.c.run_id == operations_table.c.run_id),
                )
            )
            .where(operations_table.c.run_id == landscape_run_id, calls_table.c.call_type == CallType.LLM.value)
        ).all()
    entries: list[TokenUsageEntry] = []
    for row in sorted([*state_calls, *operation_calls], key=lambda call: (call.created_at, call.call_id)):
        if row.status != CallStatus.SUCCESS.value and row.prompt_tokens is None and row.completion_tokens is None:
            continue
        config = json.loads(row.config_json)
        model = row.plugin_name
        for key in ("model", "deployment_name"):
            if key in config and config[key] is not None:
                model = config[key]
                break
        entries.append(
            TokenUsageEntry(
                model=model,
                prompt_tokens=row.prompt_tokens,
                completion_tokens=row.completion_tokens,
                cached_prompt_tokens=row.cached_prompt_tokens,
                reasoning_tokens=row.reasoning_tokens,
            )
        )
    return tuple(entries)


def _session_status_from_run_result_status(status: RunStatus) -> SessionRunStatus:
```

`src/elspeth/web/execution/service.py` HEAD lines 3232-3237 read:

```python
                        rows_quarantined=result.rows_quarantined,
                        failure_samples=samples_text,
                    )
            # Cancelled-race recovery: catch only the narrow subclass.  See
            # IllegalRunTransitionError docstring for why bare ValueError must
            # propagate (Tier-1 invariant breaches must not be masked).
```

Replace them with:

```python
                        rows_quarantined=result.rows_quarantined,
                        failure_samples=samples_text,
                    )
            # R14 (Task I1): charge the run's LLM calls before its terminal status.
            self._record_run_token_usage(run_uuid, session_operation_lease, landscape_db=landscape_db, landscape_run_id=result.run_id)
            # Cancelled-race recovery: catch only the narrow subclass.  See
            # IllegalRunTransitionError docstring for why bare ValueError must
            # propagate (Tier-1 invariant breaches must not be masked).
```

`src/elspeth/web/execution/service.py` HEAD lines 3429-3434 read:

```python
                success=False,
                session_operation_lease=session_operation_lease,
            )
            session_operation_lease.guard_external_effect()
            self._call_async(
                self._session_service.update_run_status(
```

Replace them with:

```python
                success=False,
                session_operation_lease=session_operation_lease,
            )
            # R14 (Task I1): calls made before the shutdown are spent.
            self._record_run_token_usage(run_uuid, session_operation_lease, landscape_db=landscape_db, landscape_run_id=run_id)
            session_operation_lease.guard_external_effect()
            self._call_async(
                self._session_service.update_run_status(
```

`src/elspeth/web/execution/service.py` HEAD lines 3621-3626 read:

```python
                    try:
                        status_update_exc_class = self._persist_failed_run_status(
                            run_uuid,
                            error=_operator_failure_diagnostic(
                                class_chain=_exception_class_chain(exc),
                                exc_message=_operator_exc_message(exc),
```

Replace them with:

```python
                    try:
                        status_update_exc_class = self._persist_failed_run_status(
                            run_uuid,
                            landscape_db=landscape_db,
                            error=_operator_failure_diagnostic(
                                class_chain=_exception_class_chain(exc),
                                exc_message=_operator_exc_message(exc),
```

`src/elspeth/web/execution/service.py` HEAD lines 3754-3765 read:

```python
            self._broadcaster.cleanup_run(run_id)
        return None

    def _persist_failed_run_status(
        self,
        run_uuid: UUID,
        *,
        error: str,
        session_operation_lease: SessionOperationLease,
    ) -> str | None:
        """Best-effort failed-status persistence for exception recovery.
```

Replace them with:

```python
            self._broadcaster.cleanup_run(run_id)
        return None

    def _record_run_token_usage(
        self,
        run_uuid: UUID,
        session_operation_lease: SessionOperationLease,
        *,
        landscape_db: LandscapeDB | None,
        landscape_run_id: str,
    ) -> None:
        """Charge the run's LLM calls to the run owner's token ledger under the run's EXECUTE authority (R14)."""
        if landscape_db is None:
            return
        entries = _run_token_usage_entries(landscape_db, landscape_run_id=landscape_run_id)
        if not entries:
            return
        session_operation_lease.guard_external_effect()
        self._call_async(
            self._session_service.record_token_usage(
                session_operation_context=session_operation_lease.context,
                source="run",
                run_id=run_uuid,
                entries=entries,
            )
        )

    def _persist_failed_run_status(
        self,
        run_uuid: UUID,
        *,
        error: str,
        session_operation_lease: SessionOperationLease,
        landscape_db: LandscapeDB | None,
    ) -> str | None:
        """Best-effort failed-status persistence for exception recovery.
```

`src/elspeth/web/execution/service.py` HEAD lines 3778-3783 read:

```python
                    session_operation_context=session_operation_context,
                )
            )
        except (SQLAlchemyError, OSError) as status_err:
            # Narrow catch (canonical pattern, commits b8ba2214/127417cb):
            # SQLAlchemyError family + OSError only. Programmer bugs in
```

Replace them with:

```python
                    session_operation_context=session_operation_context,
                )
            )
            # R14 (Task I1): the failed run's calls are spent. After the status
            # write, so a ledger failure degrades to the same transient report.
            self._record_run_token_usage(run_uuid, session_operation_lease, landscape_db=landscape_db, landscape_run_id=str(run_uuid))
        except (SQLAlchemyError, OSError) as status_err:
            # Narrow catch (canonical pattern, commits b8ba2214/127417cb):
            # SQLAlchemyError family + OSError only. Programmer bugs in
```

`web/app.py`: wire the recorder.

`src/elspeth/web/app.py` HEAD lines 1671-1676 read:

```python
        # The fence rows this replica writes name the same identity the wire
        # shows, so a 409 from one replica pairs with the 2xx from the other.
        owner_instance_id=instance_id,
    )
    app.state.session_service = session_service
```

Replace them with:

```python
        # The fence rows this replica writes name the same identity the wire
        # shows, so a 409 from one replica pairs with the 2xx from the other.
        owner_instance_id=instance_id,
        # R14 refusals write their Landscape quota_exceeded row through the one
        # auth audit engine this app owns (Task I1).
        quota_exceeded_recorder=audit_recorder.record_quota_exceeded,
    )
    app.state.session_service = session_service
```

`tests/unit/web/execution/test_session_operation_lease.py`: `record_token_usage` is a session-service effect that must carry the transferred context, and `self._record_run_token_usage` is a reviewed positional lease consumer.

`tests/unit/web/execution/test_session_operation_lease.py` HEAD lines 1751-1760 read:

```python
        "read_blob_content",
        "_fetch_blob_contents",
        "finalize_run_output_blobs",
    }
)
_EXECUTION_GATE_CALL_NAMES = _EXECUTION_EFFECT_NAMES | {"_persist_and_broadcast_run_event", "_finalize_output_blobs"}
_SESSION_SERVICE_EFFECTS = frozenset({"create_run", "update_run_status", "append_run_event", "record_blob_inline_resolutions"})
_BLOB_SERVICE_EFFECTS = frozenset({"get_blob", "link_blob_to_run", "read_blob_content", "finalize_run_output_blobs"})
```

Replace them with:

```python
        "read_blob_content",
        "_fetch_blob_contents",
        "finalize_run_output_blobs",
        "record_token_usage",
    }
)
_EXECUTION_GATE_CALL_NAMES = _EXECUTION_EFFECT_NAMES | {"_persist_and_broadcast_run_event", "_finalize_output_blobs"}
_SESSION_SERVICE_EFFECTS = frozenset(
    {"create_run", "update_run_status", "append_run_event", "record_blob_inline_resolutions", "record_token_usage"}
)
_BLOB_SERVICE_EFFECTS = frozenset({"get_blob", "link_blob_to_run", "read_blob_content", "finalize_run_output_blobs"})
```

`tests/unit/web/execution/test_session_operation_lease.py` HEAD lines 1789-1794 read:

```python
        "self._settle_admission_refusal": 1,
        "self._materialize_durable_cancellation": 1,
        "self._record_recovery_refusal": 1,
    }
    keyword_consumers = {
        "self._broadcast_progress_event",
```

Replace them with:

```python
        "self._settle_admission_refusal": 1,
        "self._materialize_durable_cancellation": 1,
        "self._record_recovery_refusal": 1,
        "self._record_run_token_usage": 1,
    }
    keyword_consumers = {
        "self._broadcast_progress_event",
```

- [ ] **Step 14: Run every suite the task touches.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/coordination tests/unit/web/composer/test_chargeable_admission.py tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py tests/unit/web/sessions/test_token_usage_adapters.py tests/unit/web/sessions/test_auto_title.py tests/unit/web/sessions/test_auto_title_sampling_config.py tests/unit/web/sessions/test_auto_title_endpoint_affordance.py tests/unit/web/sessions/test_persist_compose_turn.py tests/unit/web/sessions/test_service.py tests/unit/web/sessions/test_operation_fence_wiring.py tests/unit/web/sessions/test_static_direct_writers.py tests/unit/web/execution/test_run_token_ledger.py tests/unit/web/execution/test_session_operation_lease.py tests/unit/web/test_app.py tests/unit/core/landscape/test_admission_policy_provenance.py tests/unit/core/landscape/test_exporter.py -n 0 > /tmp/i-lane-i1-step14.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `1412 passed`.

- [ ] **Step 15: Bind the ledger writer in the mutation-authority manifest and re-pin the measured churn.** The manifest is fail-closed and measured, not typed from memory. On HEAD the gate XFAILs with this baseline (measured on a pristine HEAD export): `Unexpected/unreviewed (67)`, `Stale reviewed (0)`, `Connections outside exact contained authority (16)`, `Unresolved write executions (44)`, `Writers without a named authority (7)`, `Writers under the wrong table authority (0)`, `Stale reviewed read connections (0)`, `Stale non-Sessions connection classifications (0)`, `Invalid reviewed read connections (0)`. Before this step the same gate reports `Unexpected/unreviewed (93)`, `Stale reviewed (45)` and `Stale reviewed read connections (23)`.

Structural edits in `tests/unit/architecture/test_session_db_mutation_authority.py`: bind `record_token_usage_on_connection` in `_NAMED_AUTHORITY_SYMBOLS` (:262), add its writer row to `_REVIEWED_WRITERS` (:1198), and give the two run-diagnostics rows their new fingerprint and lines. The fingerprint is the AST of the enclosing function; it matches only when `quota_authority.py` and `run_diagnostics_authority.py` are exactly the Step 7 and Step 13 text.

`tests/unit/architecture/test_session_db_mutation_authority.py` HEAD lines 269-274 read:

```python
        "src/elspeth/web/coordination/approval_lifecycle_authority.py",
        "RepositoryApprovalLifecycleAuthority.apply",
        "IdentityApprovalLifecycleAuthority",
    ),
    # ACA shared UI state and quota transactions; admit exact methods only.
    AuthoritySymbol(
```

Replace them with:

```python
        "src/elspeth/web/coordination/approval_lifecycle_authority.py",
        "RepositoryApprovalLifecycleAuthority.apply",
        "IdentityApprovalLifecycleAuthority",
    ),
    # Task I1: the token ledger's only writer. Every adapter (Composer audit
    # cohorts, run diagnostics, auto-title, run finalisation) reaches it on the
    # caller's own connection, the dead_site_supersession shape.
    AuthoritySymbol(
        "src/elspeth/web/coordination/quota_authority.py",
        "record_token_usage_on_connection",
        "QuotaAuthority",
    ),
    # ACA shared UI state and quota transactions; admit exact methods only.
    AuthoritySymbol(
```

`tests/unit/architecture/test_session_db_mutation_authority.py` HEAD lines 1255-1260 read:

```python
        "IdentityApprovalLifecycleAuthority",
        line=24,
    ),
    # ACA durable progress, single-use tickets, and shared quota inventory.
    WriterIdentity(
        "src/elspeth/web/coordination/composer_progress_authority.py",
```

Replace them with:

```python
        "IdentityApprovalLifecycleAuthority",
        line=24,
    ),
    # Task I1 (sso-design.md:1422, R14): one ledger row per charged provider
    # call, attributed to the session owner, appended on the transaction that
    # wrote the call's evidence.
    WriterIdentity(
        "src/elspeth/web/coordination/quota_authority.py",
        "record_token_usage_on_connection",
        "token_usage_ledger",
        "insert",
        "311925fabbc6ca39",
        1,
        "QuotaAuthority",
        line=198,
    ),
    # ACA durable progress, single-use tickets, and shared quota inventory.
    WriterIdentity(
        "src/elspeth/web/coordination/composer_progress_authority.py",
```

`tests/unit/architecture/test_session_db_mutation_authority.py` HEAD lines 1745-1764 read:

```python
        "RepositoryRunDiagnosticsAuditAuthority.append_audit_messages",
        "chat_messages",
        "insert",
        "d0ef6a582e1fc2a3",
        1,
        "RunDiagnosticsAuditMutationAuthority",
        line=121,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/run_diagnostics_authority.py",
        "RepositoryRunDiagnosticsAuditAuthority.append_audit_messages",
        "sessions",
        "update",
        "d0ef6a582e1fc2a3",
        1,
        "RunDiagnosticsAuditMutationAuthority",
        line=137,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
```

Replace them with:

```python
        "RepositoryRunDiagnosticsAuditAuthority.append_audit_messages",
        "chat_messages",
        "insert",
        "2b314874628e7ac0",
        1,
        "RunDiagnosticsAuditMutationAuthority",
        line=123,
    ),
    WriterIdentity(
        "src/elspeth/web/coordination/run_diagnostics_authority.py",
        "RepositoryRunDiagnosticsAuditAuthority.append_audit_messages",
        "sessions",
        "update",
        "2b314874628e7ac0",
        1,
        "RunDiagnosticsAuditMutationAuthority",
        line=146,
    ),
    WriterIdentity(
        "src/elspeth/web/sessions/service.py",
```

Line-only re-pins. Each row keeps its path, symbol, table, operation, fingerprint, ordinal and authority; only `line=` moves, because Step 13 added lines above it in `src/elspeth/web/sessions/service.py`. Each row is uniquely identified by its fingerprint followed by its HEAD `line=` value (every anchor was checked to occur exactly once). `_REVIEWED_READ_CONNECTIONS` starts at :3954, and some of its rows are written in keyword form (`fingerprint=` / `ordinal=` / `authority=None` / `line=`) and the rest positionally; change only the `line=` value in either form.

| Collection | Symbol | Table | Operation | Fingerprint | Ordinal | Authority | HEAD `line=` | New `line=` |
|---|---|---|---|---|---|---|---|---|
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.count_tool_responses_for_assistant` | `<sessions-write-connection>` | write_connection | `397c36aee21eb535` | 1 | None | 9567 | 9587 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.list_audit_access_log` | `<sessions-write-connection>` | write_connection | `7b29bb527f2da6ec` | 1 | None | 9665 | 9685 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_guided_operation._sync` | `<sessions-write-connection>` | write_connection | `788d302f1873a5e1` | 1 | None | 5443 | 5463 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_guided_start_reconciliation._sync` | `<sessions-write-connection>` | write_connection | `12be925744d987c0` | 1 | None | 5495 | 5515 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_session._sync` | `<sessions-write-connection>` | write_connection | `3082b284dcdde398` | 1 | None | 6907 | 6927 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.list_sessions._sync` | `<sessions-write-connection>` | write_connection | `734dc503cf1adb8b` | 1 | None | 6974 | 6994 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_composer_preferences._sync` | `<sessions-write-connection>` | write_connection | `b96528533c8ce4fd` | 1 | None | 7314 | 7334 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_pipeline_dispatch_recovery._sync` | `<sessions-write-connection>` | write_connection | `e55c3bb74cb8ef7c` | 1 | None | 7983 | 8003 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.list_proposal_events._sync` | `<sessions-write-connection>` | write_connection | `7017fa5ec317a4b4` | 1 | None | 8306 | 8326 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.list_interpretation_events._sync` | `<sessions-write-connection>` | write_connection | `68b7373dab65abcf` | 1 | None | 8918 | 8938 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_messages._sync` | `<sessions-write-connection>` | write_connection | `1bfef5906a786d9e` | 1 | None | 9497 | 9517 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_verified_guided_root_intent._sync` | `<sessions-write-connection>` | write_connection | `adeaa7cfa27f4b34` | 1 | None | 9546 | 9566 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_current_state._sync` | `<sessions-write-connection>` | write_connection | `8a380455a32a960b` | 1 | None | 9947 | 9967 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_state_versions._sync` | `<sessions-write-connection>` | write_connection | `227880bc8eb4fb5a` | 1 | None | 9971 | 9991 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_run_execution_input._sync` | `<sessions-write-connection>` | write_connection | `060f65def285276f` | 1 | None | 10127 | 10245 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_run._sync` | `<sessions-write-connection>` | write_connection | `5bc478ef3c0f3159` | 1 | None | 10158 | 10276 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.list_runs_for_session._sync` | `<sessions-write-connection>` | write_connection | `0b7e7759f27b0141` | 1 | None | 10173 | 10291 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.list_run_events._sync` | `<sessions-write-connection>` | write_connection | `3d5bacc9c0cc086c` | 1 | None | 10222 | 10340 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_active_run._sync` | `<sessions-write-connection>` | write_connection | `e2674828bb9717c7` | 1 | None | 10317 | 10435 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_state._sync` | `<sessions-write-connection>` | write_connection | `48cf615d2b446722` | 1 | None | 10336 | 10454 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.list_pending_landscape_reconciliations._sync` | `<sessions-write-connection>` | write_connection | `af0aa9fb126f5b07` | 1 | None | 13309 | 13434 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl._session_principal_context._sync` | `<sessions-write-connection>` | write_connection | `3a0d4f58a95545b6` | 1 | None | 14098 | 14223 |
| _REVIEWED_READ_CONNECTIONS | `SessionServiceImpl.get_state_version_numbers._sync` | `<sessions-write-connection>` | write_connection | `79cbcb06fa877308` | 1 | None | 14354 | 14488 |
| _REVIEWED_WRITERS | `_SessionComposerMutations.create_composition_proposal` | `proposal_events` | insert | `55c8854837524a3f` | 1 | SessionComposerMutationAuthority | 3185 | 3198 |
| _REVIEWED_WRITERS | `_SessionComposerMutations.create_composition_proposal` | `composition_proposals` | insert | `4d7437366c54fbeb` | 1 | SessionComposerMutationAuthority | 3201 | 3214 |
| _REVIEWED_WRITERS | `_SessionComposerMutations.create_pipeline_composition_proposal` | `proposal_events` | insert | `58a94a42ebf58130` | 1 | SessionComposerMutationAuthority | 3308 | 3321 |
| _REVIEWED_WRITERS | `_SessionComposerMutations.create_pipeline_composition_proposal` | `composition_proposals` | insert | `be0a21ec508fea9c` | 1 | SessionComposerMutationAuthority | 3319 | 3332 |
| _REVIEWED_WRITERS | `_SessionComposerMutations.create_guided_pipeline_proposal` | `proposal_events` | insert | `fc9b265fb5f1ff86` | 1 | SessionComposerMutationAuthority | 3385 | 3398 |
| _REVIEWED_WRITERS | `_SessionComposerMutations.create_guided_pipeline_proposal` | `composition_proposals` | insert | `48004d8c43dcec6a` | 1 | SessionComposerMutationAuthority | 3396 | 3409 |
| _REVIEWED_WRITERS | `_SessionComposerMutations.reject_pending_proposal` | `proposal_events` | insert | `17277db356846ba4` | 1 | SessionComposerMutationAuthority | 3518 | 3531 |
| _REVIEWED_WRITERS | `_SessionComposerMutations.reject_pending_proposal` | `composition_proposals` | update | `8b790876eab3ca5d` | 1 | SessionComposerMutationAuthority | 3529 | 3542 |
| _REVIEWED_WRITERS | `_SessionComposerMutations.accept_pending_ordinary_proposal` | `proposal_events` | insert | `f381d823a069aec1` | 1 | SessionComposerMutationAuthority | 3639 | 3652 |
| _REVIEWED_WRITERS | `_SessionComposerMutations.accept_pending_ordinary_proposal` | `proposal_blob_effect_receipts` | update | `838f74d6c673e89a` | 1 | SessionComposerMutationAuthority | 3651 | 3664 |
| _REVIEWED_WRITERS | `_SessionComposerMutations.accept_pending_ordinary_proposal` | `composition_proposals` | update | `136c26279232b29b` | 1 | SessionComposerMutationAuthority | 3663 | 3676 |
| _REVIEWED_WRITERS | `_GuidedSessionMutations.record_nonterminal_event` | `guided_operation_events` | insert | `c66670f774b6404d` | 1 | GuidedSessionMutationAuthority | 3790 | 3803 |
| _REVIEWED_WRITERS | `_GuidedSessionMutations.bind` | `guided_operations` | update | `0d2776483587fc01` | 1 | GuidedSessionMutationAuthority | 3830 | 3843 |
| _REVIEWED_WRITERS | `_GuidedSessionMutations.require_no_active_confirmation` | `guided_operations` | update | `d2fd5f53fcc3d7de` | 1 | GuidedSessionMutationAuthority | 3851 | 3864 |
| _REVIEWED_WRITERS | `_GuidedSessionMutations.claim_confirmation` | `guided_operations` | update | `d2fd5f53fcc3d7de` | 1 | GuidedSessionMutationAuthority | 3881 | 3894 |
| _REVIEWED_WRITERS | `_GuidedSessionMutations.claim_confirmation` | `guided_operations` | update | `95efdd37e97b888e` | 1 | GuidedSessionMutationAuthority | 3905 | 3918 |
| _REVIEWED_WRITERS | `_GuidedSessionMutations.complete` | `guided_operations` | update | `e7ef88803ab1d8bb` | 1 | GuidedSessionMutationAuthority | 3964 | 3977 |
| _REVIEWED_WRITERS | `_GuidedSessionMutations.complete` | `guided_operation_events` | insert | `e7ef88803ab1d8bb` | 1 | GuidedSessionMutationAuthority | 3994 | 4007 |
| _REVIEWED_WRITERS | `_GuidedSessionMutations.fail` | `guided_operations` | update | `9c6096dc61fbf4cd` | 1 | GuidedSessionMutationAuthority | 4034 | 4047 |
| _REVIEWED_WRITERS | `_GuidedSessionMutations.fail` | `guided_operation_events` | insert | `9c6096dc61fbf4cd` | 1 | GuidedSessionMutationAuthority | 4068 | 4081 |
| _REVIEWED_WRITERS | `_GuidedSessionMutations.mark_session_updated` | `sessions` | update | `b175fa9ac09b0b80` | 1 | GuidedSessionMutationAuthority | 4104 | 4117 |
| _REVIEWED_WRITERS | `_GuidedComposerMutations.reject_pending_proposal` | `proposal_events` | insert | `472e557358d79356` | 1 | GuidedSessionComposerMutationAuthority | 4143 | 4156 |
| _REVIEWED_WRITERS | `_GuidedComposerMutations.reject_pending_proposal` | `composition_proposals` | update | `50a069cc3fb3a8a1` | 1 | GuidedSessionComposerMutationAuthority | 4154 | 4167 |
| _REVIEWED_WRITERS | `_GuidedComposerMutations.record_pending_proposal_rejection` | `proposal_events` | insert | `14992d37a16a7085` | 1 | GuidedSessionComposerMutationAuthority | 4192 | 4205 |
| _REVIEWED_WRITERS | `_GuidedComposerMutations.record_pending_proposal_rejection` | `composition_proposals` | update | `385ebb9b57262fac` | 1 | GuidedSessionComposerMutationAuthority | 4203 | 4216 |
| _REVIEWED_WRITERS | `_GuidedComposerMutations.record_pending_proposal_acceptance` | `proposal_events` | insert | `516d95862038f4d9` | 1 | GuidedSessionComposerMutationAuthority | 4254 | 4267 |
| _REVIEWED_WRITERS | `_GuidedComposerMutations.record_pending_proposal_acceptance` | `composition_proposals` | update | `769b7f0ff6157a92` | 1 | GuidedSessionComposerMutationAuthority | 4265 | 4278 |
| _REVIEWED_WRITERS | `SessionServiceImpl._record_guided_fork_child_terminal_event` | `guided_operation_events` | insert | `1a8637d3ecf9263b` | 1 | SessionForkAuthority | 4959 | 4979 |
| _REVIEWED_WRITERS | `SessionServiceImpl.reserve_guided_operation._sync` | `guided_operations` | insert | `1161702a3f59ea98` | 1 | GuidedSessionAdmissionAuthority | 5330 | 5350 |
| _REVIEWED_WRITERS | `SessionServiceImpl.reserve_guided_operation._sync` | `guided_operations` | update | `1161702a3f59ea98` | 1 | GuidedSessionAdmissionAuthority | 5389 | 5409 |
| _REVIEWED_WRITERS | `SessionServiceImpl.reconcile_guided_start_operation._sync` | `guided_operation_admission_blocks` | insert | `ca00ab3741ac8f83` | 1 | GuidedSessionAdmissionAuthority | 5587 | 5607 |
| _REVIEWED_WRITERS | `SessionServiceImpl.reconcile_guided_start_operation._sync` | `guided_operations` | update | `ca00ab3741ac8f83` | 1 | GuidedSessionAdmissionAuthority | 5625 | 5645 |
| _REVIEWED_WRITERS | `SessionServiceImpl.renew_guided_operation._sync` | `guided_operations` | update | `343692d13bca60b9` | 1 | GuidedSessionMutationAuthority | 5731 | 5751 |
| _REVIEWED_WRITERS | `SessionServiceImpl.fail_guided_operation_with_audit._sync` | `sessions` | update | `65a76a89a8b9b65a` | 1 | GuidedSessionMutationAuthority | 6043 | 6063 |
| _REVIEWED_WRITERS | `SessionServiceImpl._insert_chat_message` | `chat_messages` | insert | `53042278a36c4c3d` | 1 | SessionMutationAuthority | 6205 | 6225 |
| _REVIEWED_WRITERS | `SessionServiceImpl._insert_composition_state` | `composition_states` | insert | `abb7447bf7be0537` | 1 | SessionMutationAuthority | 6377 | 6397 |
| _REVIEWED_WRITERS | `SessionServiceImpl.update_composer_preferences._sync` | `proposal_events` | insert | `78fe65cf99c28d0f` | 1 | SessionComposerMutationAuthority | 7392 | 7412 |
| _REVIEWED_WRITERS | `SessionServiceImpl.update_composer_preferences._sync` | `sessions` | update | `78fe65cf99c28d0f` | 1 | SessionComposerMutationAuthority | 7407 | 7427 |
| _REVIEWED_WRITERS | `SessionServiceImpl.settle_guided_fork_operation._sync` | `chat_messages` | update | `77fdbd32e4f8a672` | 1 | SessionForkAuthority | 13894 | 14019 |
| _REVIEWED_WRITERS | `SessionServiceImpl.settle_guided_fork_operation._sync` | `composition_states` | delete | `77fdbd32e4f8a672` | 1 | SessionForkAuthority | 13905 | 14030 |
| _REVIEWED_WRITERS | `SessionServiceImpl.settle_guided_fork_operation._sync` | `chat_messages` | update | `77fdbd32e4f8a672` | 2 | SessionForkAuthority | 13925 | 14050 |
| _REVIEWED_WRITERS | `SessionServiceImpl.settle_guided_fork_operation._sync` | `guided_operations` | insert | `77fdbd32e4f8a672` | 1 | SessionForkAuthority | 13973 | 14098 |
| _REVIEWED_WRITERS | `SessionServiceImpl.settle_guided_fork_operation._sync` | `sessions` | update | `77fdbd32e4f8a672` | 1 | SessionForkAuthority | 14027 | 14152 |

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/architecture/test_session_db_mutation_authority.py -n 0 -rx > /tmp/i-lane-i1-step15.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `265 passed, 1 xfailed`. Read the XFAIL text in the log: it must show exactly the HEAD baseline counts listed above. A `Stale reviewed` row means a copied line or fingerprint is wrong; a `record_token_usage_on_connection` row under `Writers without a named authority` means the binding is missing. `test_run_diagnostics_writer_is_exactly_bound_to_its_handle_free_authority` (:11043) and the facet and guided tests that pin `sessions/service.py` rows pass only once their rows are re-pinned.

- [ ] **Step 16: Run the PostgreSQL suites.** The task adds SQL (a `SUM` over a `CASE` expression bounded by a `timestamptz` window, and `FOR UPDATE` policy reads) and changes the diagnostics writer's transaction, so F7 applies. Docker is required.

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/testcontainer/web/test_quota_authority_postgres.py tests/testcontainer/web/test_chargeable_admission_postgres.py tests/testcontainer/web/test_run_diagnostics_authority_postgres.py -n 0 -m testcontainer > /tmp/i-lane-i1-step16.log 2>&1; echo exit=$?
```

Expected: `exit=0`, `8 passed`: the two new PostgreSQL quota proofs, the three chargeable-admission lock-order proofs, and the diagnostics archive-race proofs with real envelopes.

- [ ] **Step 17: Static gates.**

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff check src/elspeth/web/coordination/quota_authority.py src/elspeth/contracts/chargeable_admission.py src/elspeth/web/coordination/chargeable_admission_authority.py src/elspeth/web/auth/audit.py src/elspeth/web/sessions/service.py src/elspeth/web/sessions/protocol.py src/elspeth/web/coordination/run_diagnostics_authority.py src/elspeth/web/sessions/_auto_title.py src/elspeth/web/execution/service.py src/elspeth/web/app.py tests/helpers/fenced_session.py tests/unit/web/coordination/conftest.py tests/unit/web/coordination/test_quota_authority.py tests/unit/web/sessions/test_token_usage_adapters.py tests/unit/web/execution/test_run_token_ledger.py tests/testcontainer/web/test_quota_authority_postgres.py tests/unit/web/composer/test_chargeable_admission.py tests/unit/web/coordination/test_durable_run_admission.py tests/unit/web/coordination/test_cli_quota_admission.py tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py tests/unit/web/sessions/test_auto_title.py tests/unit/web/sessions/test_auto_title_sampling_config.py tests/unit/web/sessions/test_auto_title_endpoint_affordance.py tests/unit/web/sessions/test_persist_compose_turn.py tests/unit/web/sessions/test_service.py tests/unit/web/execution/test_session_operation_lease.py tests/testcontainer/web/test_run_diagnostics_authority_postgres.py tests/testcontainer/web/conftest.py tests/unit/architecture/test_session_db_mutation_authority.py > /tmp/i-lane-i1-ruff.log 2>&1; echo exit=$?
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff format --check src/elspeth/web/coordination/quota_authority.py src/elspeth/contracts/chargeable_admission.py src/elspeth/web/coordination/chargeable_admission_authority.py src/elspeth/web/auth/audit.py src/elspeth/web/sessions/service.py src/elspeth/web/sessions/protocol.py src/elspeth/web/coordination/run_diagnostics_authority.py src/elspeth/web/sessions/_auto_title.py src/elspeth/web/execution/service.py src/elspeth/web/app.py tests/helpers/fenced_session.py tests/unit/web/coordination/conftest.py tests/unit/web/coordination/test_quota_authority.py tests/unit/web/sessions/test_token_usage_adapters.py tests/unit/web/execution/test_run_token_ledger.py tests/testcontainer/web/test_quota_authority_postgres.py tests/unit/web/composer/test_chargeable_admission.py tests/unit/web/coordination/test_durable_run_admission.py tests/unit/web/coordination/test_cli_quota_admission.py tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py tests/unit/web/sessions/test_auto_title.py tests/unit/web/sessions/test_auto_title_sampling_config.py tests/unit/web/sessions/test_auto_title_endpoint_affordance.py tests/unit/web/sessions/test_persist_compose_turn.py tests/unit/web/sessions/test_service.py tests/unit/web/execution/test_session_operation_lease.py tests/testcontainer/web/test_run_diagnostics_authority_postgres.py tests/testcontainer/web/conftest.py tests/unit/architecture/test_session_db_mutation_authority.py > /tmp/i-lane-i1-format.log 2>&1; echo exit=$?
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && mypy src/elspeth/contracts/chargeable_admission.py src/elspeth/web/coordination/quota_authority.py src/elspeth/web/coordination/chargeable_admission_authority.py src/elspeth/web/coordination/run_diagnostics_authority.py src/elspeth/web/auth/audit.py src/elspeth/web/sessions/_auto_title.py src/elspeth/web/sessions/protocol.py src/elspeth/web/sessions/service.py src/elspeth/web/execution/service.py src/elspeth/web/app.py > /tmp/i-lane-i1-mypy.log 2>&1; echo exit=$?
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth > /tmp/i-lane-i1-lints.log 2>&1; echo exit=$?
```

Expected: ruff `exit=0` twice; mypy `exit=0` with `Success: no issues found in 10 source files`. The lint gate exits `1` on HEAD as well (the documented fail-closed state), so compare corpora rather than exit codes: run the same command on the base commit into `/tmp/i-lane-i1-lints-base.log` and compare the two finding sets. Measured on this change: both corpora hold 2042 findings and are identical once each `path:line:col` is reduced to `path`. The auto-title `R6` finding stays a single finding because Step 13 keeps the malformed-response arm inside the existing `except` clause; splitting it into its own clause adds a finding. Every other difference is the same finding moved to a new line in `web/execution/service.py`, `web/sessions/protocol.py`, `web/sessions/service.py` or `web/sessions/_auto_title.py`.

- [ ] **Step 18: Check branch safety and commit by file pathspec.**

```bash
cd "$(git rev-parse --show-toplevel)" && scripts/branch-safety-check.sh --intent commit > /tmp/i-lane-i1-safety.log 2>&1; echo exit=$?
```

Expected: `exit=0` with no `[FAIL]` line.

The seven created files are untracked, and a commit pathspec that names an untracked path is refused (`error: pathspec '...' did not match any file(s) known to git`, exit 1), so mark exactly those seven intent-to-add first (this records only that the paths exist; nothing else enters the index):

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N src/elspeth/web/coordination/quota_authority.py tests/helpers/fenced_session.py tests/unit/web/coordination/conftest.py tests/unit/web/coordination/test_quota_authority.py tests/unit/web/sessions/test_token_usage_adapters.py tests/unit/web/execution/test_run_token_ledger.py tests/testcontainer/web/test_quota_authority_postgres.py; echo exit=$?
```

Expected: `exit=0`. Then commit exactly the 30 files:

```bash
cd "$(git rev-parse --show-toplevel)" && git commit -m "feat(identity): token usage ledger adapters and R14 daily quota enforcement" -- src/elspeth/web/coordination/quota_authority.py src/elspeth/contracts/chargeable_admission.py src/elspeth/web/coordination/chargeable_admission_authority.py src/elspeth/web/auth/audit.py src/elspeth/web/sessions/service.py src/elspeth/web/sessions/protocol.py src/elspeth/web/coordination/run_diagnostics_authority.py src/elspeth/web/sessions/_auto_title.py src/elspeth/web/execution/service.py src/elspeth/web/app.py tests/helpers/fenced_session.py tests/unit/web/coordination/conftest.py tests/unit/web/coordination/test_quota_authority.py tests/unit/web/sessions/test_token_usage_adapters.py tests/unit/web/execution/test_run_token_ledger.py tests/testcontainer/web/test_quota_authority_postgres.py tests/unit/web/composer/test_chargeable_admission.py tests/unit/web/coordination/test_durable_run_admission.py tests/unit/web/coordination/test_cli_quota_admission.py tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_identity_admin_routes.py tests/unit/web/sessions/test_auto_title.py tests/unit/web/sessions/test_auto_title_sampling_config.py tests/unit/web/sessions/test_auto_title_endpoint_affordance.py tests/unit/web/sessions/test_persist_compose_turn.py tests/unit/web/sessions/test_service.py tests/unit/web/execution/test_session_operation_lease.py tests/testcontainer/web/test_run_diagnostics_authority_postgres.py tests/testcontainer/web/conftest.py tests/unit/architecture/test_session_db_mutation_authority.py
```

Check `git show --stat HEAD` lists exactly those 30 files (7 created, 23 modified) and the summary line reads `30 files changed`.
