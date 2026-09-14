### Task I8: The enforcement switch and the R11 startup refusal

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: I0. Runs before: I1, I10. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered directly after I0 (I0 → I8 → I1 ∥ I2; I8 → I3; I8 → I10). Every
later identity task that enforces anything (I3 `_assess`, I5 `publish`,
I10's governance suite) reads the switch this task creates, and the switch's
only refusal is the readiness check this task extends. Spec: R11
(sso-design.md:1115-1124), D30/D31 (:86-87), §Testing → Workflow governance
(:1276-1310). Measured on HEAD 072141b75: `grep -n workflow_governance
src/elspeth/web/config.py src/elspeth/web/readiness.py` exits 1 — the field
does not exist, and `WebSettings` is `extra="forbid"` (config.py:184), so no
downstream task can construct a governance-on settings object until this
task lands.

Two decisions this task owns (from DECISIONS.md I2):

1. The refusal is a **readiness failure, not a config-time validator.** R11
   says "a readiness failure naming both settings, not enforcement quietly
   switched off". A `model_validator` would make the R11 combination
   unconstructible, so the fire test could not exist, and it would refuse
   at load with no `/api/ready` body for an operator to read. Consumers read
   `settings.workflow_governance == "on"` directly and never AND it with the
   R11 predicate: on a misconfigured deployment enforcement stays ON and
   readiness stays 503 — the platform never routes traffic there.
2. `workflow_governance="on"` **also requires `compartment_id`.**
   `library_entries.compartment_id` is NOT NULL (models.py:3796) while
   `WebSettings.compartment_id` is `str | None` (config.py:545) and only IdP
   profiles require it (`_COMMON_IDP_REQUIRED`, auth/providers/__init__.py:44-49).
   A closed local deployment with governance on and no compartment would
   reach I5's publish and fail with an `IntegrityError` instead of a named
   refusal. Readiness refuses it here; I5 additionally refuses publish with
   `error_type="compartment_not_configured"` (HTTP 409) as defence in depth.

**Files:**
- Modify: `src/elspeth/web/config.py:545-557` (`compartment_id` at :545,
  `identity_dormancy_days` :556, `identity_pending_retention_days` :557 — the
  new field goes directly after :557, inside the identity-sprint block)
- Modify: `src/elspeth/web/readiness.py:315-345` (`_check_auth_mode`; :333 is
  the local early return, :345 the IdP success return)
- Modify: `tests/unit/web/test_readiness.py:544-583` (`_settings_stub`; its
  `values` dict ends at :579 `"quota_default_storage_bytes": None,`) and
  `:910-1005` (`class TestReadinessAuthAndReport`, which ends before
  `class TestInstanceMembershipCheck` at :1007)
- Modify: `tests/unit/web/test_config.py:2219-2259` (`class TestInstanceId`
  is the last class; the new class is appended after it; `_settings(**overrides)`
  helper at :1599, `required_web_env` fixture at :30)
- Modify: `tests/unit/web/conftest.py:104-173` (`test_client` fixture at :105;
  its body becomes the shared `_route_client` helper, and `closed_local_settings`
  / `closed_local_app` are added directly after it, before
  `inject_non_compose_loop_AuditIntegrityError` at :176)
- Modify: `docs/reference/configuration.md:371-372` (the `registration_mode` /
  `dev_admin_user` rows; the new row follows :372) and `:387` (the
  `compartment_id` row currently says "no runtime path reads it in this release")
- Modify: `docs/reference/environment-variables.md:71-72` (Optional Variables
  table rows) and `:90-100` (`### ELSPETH_WEB__REGISTRATION_MODE` section; the
  new section follows it)
- Modify: `CHANGELOG.md:35-38` (bullet after "**Authentication events in signed
  exports.**" under `## 0.8.1 - 2026-09-10`)
- Test: `tests/unit/web/test_readiness.py`, `tests/unit/web/test_config.py`

**Interfaces:**
- Consumes (all on HEAD, nothing from I0 beyond ordering):
  - `WebSettings` (config.py:173; `model_config = ConfigDict(frozen=True, extra="forbid", ...)` :184; `registration_mode: Literal["open", "email_verified", "closed"] = "open"` :219; `compartment_id: str | None = None` :545)
  - `configured_auth_settings(settings: WebSettings) -> Mapping[str, bool]` (config.py:1354; already imported by readiness.py:21) — `["compartment_id"]` is the non-blank verdict
  - `_check_auth_mode(settings: WebSettings) -> ReadinessCheck` (readiness.py:315), `ReadinessCheck(name, ok, detail)` (:42), `READINESS_CHECK_NAMES` (:31), `readiness_report(...)` (:360)
  - `settings_from_env() -> WebSettings` (config.py:1417; env prefix `ELSPETH_WEB__`, unknown names raise `RuntimeError("Unknown ELSPETH_WEB__ setting: ...")` :1434)
  - test helpers: `_settings_stub(tmp_path, **overrides)` (test_readiness.py:544), `_IDP_COMMON` (:530), `_FakeEngine` (:506), `_settings(**overrides)` (test_config.py:1599), `required_web_env` (test_config.py:32); fixture `engine` (tests/unit/web/conftest.py:63); `DualFencedSessionServiceHarness` (tests/unit/web/sessions/guided_test_authority.py, imported at conftest.py:59); `SyncASGITestClient` (conftest.py:58)
- Produces:
  - `WebSettings.workflow_governance: Literal["off", "on"] = "off"`; environment name `ELSPETH_WEB__WORKFLOW_GOVERNANCE`. Read directly (`settings.workflow_governance == "on"`) by I3 `RepositoryRunStartPermitAuthority._assess` (R2 gate), I5 `RepositoryLibraryAuthority.publish`, I4's attestation routes and I10's suite. No helper derives an "effective" value; R11 is readiness-only.
  - `_check_auth_mode` refusal details (byte-exact, pinned by the tests below):
    - R11: `workflow_governance=on is refused with auth_provider=local and registration_mode=open (R11): one person can hold many local identities, so every author-is-not-approver rule is defeatable; set registration_mode=closed or email_verified, or set workflow_governance=off`
    - compartment: `workflow_governance=on requires compartment_id: library rows and audit metadata carry the compartment marking`
    - admitted with governance on: the existing success detail with `; workflow governance on` appended (`local authentication configured; workflow governance on`, `oidc authentication configured; workflow governance on`)
  - `_workflow_governance_refusal(settings: WebSettings, provider: str) -> str | None` (readiness.py, private; the single place the R11 predicate lives)
  - fixtures in `tests/unit/web/conftest.py`: `closed_local_settings(tmp_path) -> WebSettings` (`auth_provider="local"`, `registration_mode="closed"`, `workflow_governance="on"`, `compartment_id="test-compartment"`, `quota_default_tokens_per_day=100_000`, `quota_default_storage_bytes=1_000_000`) and `closed_local_app(tmp_path, closed_local_settings) -> SyncASGITestClient` — the closed twin of `test_client`, same in-memory `StaticPool` engine, same `DualFencedSessionServiceHarness`, same `get_current_user` override to `alice`, with `client.app.state.settings` = the closed settings and `client.app.state.phase3_engine` / `phase3_sessions_service` exposed exactly as `test_client` does. I3, I4, I5 and I9 `include_router` their workflow routers onto `closed_local_app.app` in their own tests; I10 consumes it as-is. The compartment value `test-compartment` matches the `^[a-z0-9][a-z0-9-]{0,62}$` validator I6 adds later.
  - Private helper `_route_client(tmp_path: Path, settings: WebSettings) -> SyncASGITestClient` in the same conftest; `test_client` keeps its exact behaviour by calling it with the settings it constructs today.

- [ ] **Step 1: Write the failing readiness tests (fire + mutation-derivation for R11, the compartment arm, the report path, and the fixture proofs).**

First teach the stub the two settings the check will read. In
`tests/unit/web/test_readiness.py:579`, directly after
`"quota_default_storage_bytes": None,` add:

```python
        # R11 reads both of these. The defaults are the SHIPPED defaults
        # (config.py:219 and the workflow_governance field), so every existing
        # test keeps modelling the deployment it modelled before.
        "registration_mode": "open",
        "workflow_governance": "off",
```

Then append these methods to `class TestReadinessAuthAndReport`
(test_readiness.py:910), after
`test_auth_mode_names_the_registered_providers_when_one_is_unknown` (:960-964)
and before `test_report_has_exact_order_and_unique_names` (:966):

```python
    _R11_DETAIL = (
        "workflow_governance=on is refused with auth_provider=local and registration_mode=open (R11): "
        "one person can hold many local identities, so every author-is-not-approver rule is defeatable; "
        "set registration_mode=closed or email_verified, or set workflow_governance=off"
    )
    _COMPARTMENT_DETAIL = (
        "workflow_governance=on requires compartment_id: library rows and audit metadata carry the compartment marking"
    )

    def test_r11_refuses_governance_under_open_local_registration(self, tmp_path: Path) -> None:
        """Fire test. Spec R11 (sso-design.md:1115-1124): a readiness failure naming BOTH settings."""
        settings = _settings_stub(tmp_path, registration_mode="open", workflow_governance="on", compartment_id="compartment-a")
        check = _check_auth_mode(settings)
        assert check == ReadinessCheck("auth_mode", False, self._R11_DETAIL)
        assert "registration_mode" in check.detail
        assert "workflow_governance" in check.detail
        assert "auth_provider" in check.detail

    @pytest.mark.parametrize("registration_mode", ["closed", "email_verified"])
    def test_r11_derives_from_the_registration_mode_setting(self, tmp_path: Path, registration_mode: str) -> None:
        """Mutation-derivation. Change the authority (the setting), not the guard, and the verdict flips.

        R11's predicate is ``local AND open``; ``email_verified`` is "not open"
        (spec :1122-1124), so it is admitted like ``closed``.
        """
        settings = _settings_stub(
            tmp_path, registration_mode=registration_mode, workflow_governance="on", compartment_id="compartment-a"
        )
        assert _check_auth_mode(settings) == ReadinessCheck("auth_mode", True, "local authentication configured; workflow governance on")

    def test_r11_is_inert_while_governance_is_off(self, tmp_path: Path) -> None:
        """The shipped default (local + open + off) is the tutorial's deployment and must stay ready."""
        settings = _settings_stub(tmp_path, registration_mode="open", workflow_governance="off")
        assert _check_auth_mode(settings) == ReadinessCheck("auth_mode", True, "local authentication configured")

    def test_r11_predicate_is_local_and_open_not_open_alone(self, tmp_path: Path) -> None:
        """registration_mode is inert under an IdP (configuration.md:371), so open + IdP + on is admitted."""
        settings = _settings_stub(
            tmp_path,
            auth_provider="oidc",
            registration_mode="open",
            workflow_governance="on",
            **{**_IDP_COMMON, "sso_issuer": "https://issuer.invalid"},
        )
        assert _check_auth_mode(settings) == ReadinessCheck("auth_mode", True, "oidc authentication configured; workflow governance on")

    @pytest.mark.parametrize("compartment_id", [None, "", "   "])
    def test_governance_on_requires_a_compartment(self, tmp_path: Path, compartment_id: str | None) -> None:
        """library_entries.compartment_id is NOT NULL (models.py:3796); refuse by name, not by IntegrityError."""
        settings = _settings_stub(tmp_path, registration_mode="closed", workflow_governance="on", compartment_id=compartment_id)
        assert _check_auth_mode(settings) == ReadinessCheck("auth_mode", False, self._COMPARTMENT_DETAIL)

    def test_governance_compartment_arm_derives_from_the_compartment_setting(self, tmp_path: Path) -> None:
        """Mutation-derivation for the compartment arm: supply the value and the same settings are admitted."""
        settings = _settings_stub(tmp_path, registration_mode="closed", workflow_governance="on", compartment_id="compartment-a")
        assert _check_auth_mode(settings).ok is True

    def test_r11_is_checked_after_the_idp_configuration_is_complete(self, tmp_path: Path) -> None:
        """An incomplete IdP is still reported as incomplete; governance never masks a missing field."""
        settings = _settings_stub(tmp_path, auth_provider="google", workflow_governance="on", **_IDP_COMMON)
        check = _check_auth_mode(settings)
        assert check.ok is False
        assert check.detail.startswith("google configuration incomplete: missing google_hosted_domain")

    @pytest.mark.asyncio
    async def test_report_is_not_ready_under_r11_and_names_only_auth_mode(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The whole report: R11 is the ONE failing check, in its fixed position, with the R11 detail."""
        settings = _settings_stub(tmp_path, registration_mode="open", workflow_governance="on", compartment_id="compartment-a")
        session_engine = create_session_engine(f"sqlite:///{tmp_path / 'session.db'}")
        monkeypatch.setattr(readiness, "probe_session_schema", lambda conn: SchemaState.CURRENT)
        monkeypatch.setattr(readiness, "probe_landscape_schema", lambda conn: SchemaState.CURRENT)
        runner = ReadinessProbeRunner()
        try:
            with capture_logs() as logs:
                report = await readiness_report(settings, session_engine, runner, instance_draining=threading.Event())
        finally:
            runner.close()
            session_engine.dispose()
        assert report.ready is False
        assert [check.name for check in report.checks] == list(readiness.READINESS_CHECK_NAMES)
        assert [(check.name, check.detail) for check in report.checks if not check.ok] == [("auth_mode", self._R11_DETAIL)]
        # _finalize (readiness.py:349-354) logs every failed check: this is the "loudly" in D30.
        assert [log for log in logs if log.get("event") == "readiness_check_not_ready"] == [
            {"event": "readiness_check_not_ready", "log_level": "warning", "check": "auth_mode", "detail": self._R11_DETAIL}
        ]

    def test_closed_local_app_is_a_supported_governance_configuration(self, closed_local_app: Any) -> None:
        """The fixture every workflow-governance test runs against must itself pass R11 (spec :1122-1124, :1308-1311)."""
        settings = closed_local_app.app.state.settings
        assert settings.auth_provider == "local"
        assert settings.registration_mode == "closed"
        assert settings.workflow_governance == "on"
        assert settings.compartment_id == "test-compartment"
        assert _check_auth_mode(settings) == ReadinessCheck("auth_mode", True, "local authentication configured; workflow governance on")

    def test_the_shared_route_fixture_sits_in_the_r11_combination(self, test_client: Any) -> None:
        """Spec :1120-1121: the shared route fixture is exactly local + open. Prove it, so nobody 'fixes' a
        governance test by flipping the switch on the wrong fixture."""
        settings = test_client.app.state.settings
        assert (settings.auth_provider, settings.registration_mode, settings.workflow_governance) == ("local", "open", "off")
        # model_copy(update=...) skips validation, which is the point: readiness, not the model, owns R11.
        assert _check_auth_mode(settings.model_copy(update={"workflow_governance": "on"})).detail == self._R11_DETAIL
```

`Any`, `threading`, `capture_logs`, `create_session_engine`, `SchemaState`,
`ReadinessProbeRunner` and `readiness` are already imported at
test_readiness.py:11-34; nothing new is imported.

- [ ] **Step 2: Run the readiness tests to verify they fail for the right reason.**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/test_readiness.py -n 0 -k "r11 or governance or closed_local_app or shared_route_fixture" > /tmp/i8-readiness-red.log 2>&1; echo exit=$?
```

Expected: `exit=1`. `test_r11_refuses_governance_under_open_local_registration`
fails with `AssertionError: assert ReadinessCheck(name='auth_mode', ok=True, detail='local authentication configured') == ReadinessCheck(name='auth_mode', ok=False, detail='workflow_governance=on is refused ...')`
(the check still returns the :333 early success); the two `closed_local_app`
/ `test_client` tests error with `fixture 'closed_local_app' not found` and
`AttributeError: 'WebSettings' object has no attribute 'workflow_governance'`
respectively. Every OTHER test in the file must still pass: check the summary
line reads `N failed, M errors` with no failure outside the `-k` selection —
the stub defaults were chosen so the existing tests model the same deployment.

- [ ] **Step 3: Write the failing config tests.**

Append to the end of `tests/unit/web/test_config.py` (after `class TestInstanceId`, :2219-2259):

```python


class TestWorkflowGovernanceSwitch:
    """The switch every workflow enforcement reads (R2, attestation, library). R11 lives in readiness, not here."""

    def test_defaults_to_off(self) -> None:
        assert _settings().workflow_governance == "off"

    def test_accepts_on(self) -> None:
        assert _settings(workflow_governance="on").workflow_governance == "on"

    @pytest.mark.parametrize("value", ["true", "1", "ON", "yes", "", "enforce"])
    def test_rejects_every_other_spelling(self, value: str) -> None:
        with pytest.raises(ValidationError, match="workflow_governance"):
            _settings(workflow_governance=value)

    @pytest.mark.usefixtures("required_web_env")
    def test_settable_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ELSPETH_WEB__WORKFLOW_GOVERNANCE", "on")
        assert web_config.settings_from_env().workflow_governance == "on"

    def test_open_local_with_governance_on_is_constructible(self) -> None:
        """R11 is a READINESS refusal (sso-design.md:1118-1120), not a load-time one.

        A model_validator here would make the R11 combination unconstructible —
        the readiness fire test could not exist — and would refuse with a
        traceback instead of a /api/ready body an operator can read.
        """
        settings = _settings(auth_provider="local", registration_mode="open", workflow_governance="on")
        assert (settings.registration_mode, settings.workflow_governance) == ("open", "on")

    def test_governance_on_without_a_compartment_is_constructible(self) -> None:
        """Same reason: the compartment arm is readiness's, so a closed local deployment can be built and then reported."""
        settings = _settings(registration_mode="closed", workflow_governance="on")
        assert settings.compartment_id is None
```

`web_config`, `ValidationError` and `pytest` are imported at
test_config.py:15-19; `_settings` is the module-level helper at :1599.

- [ ] **Step 4: Run the config tests to verify they fail.**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/test_config.py::TestWorkflowGovernanceSwitch -n 0 > /tmp/i8-config-red.log 2>&1; echo exit=$?
```

Expected: `exit=1`; `test_defaults_to_off` fails with
`AttributeError: 'WebSettings' object has no attribute 'workflow_governance'`,
`test_accepts_on` and the constructible tests fail with
`pydantic_core._pydantic_core.ValidationError: 1 validation error for WebSettings\nworkflow_governance\n  Extra inputs are not permitted`,
`test_settable_from_environment` fails with
`RuntimeError: Unknown ELSPETH_WEB__ setting: ELSPETH_WEB__WORKFLOW_GOVERNANCE`,
and `test_rejects_every_other_spelling` fails because the raised error says
`Extra inputs are not permitted` — it matches `workflow_governance` in the
field name, so this one PASSES before the field exists. That is why Step 6
re-runs it and reads the `Input should be 'off' or 'on'` message: a
test that is green on HEAD proves nothing until the field exists.

- [ ] **Step 5: Add the setting to `WebSettings`.**

In `src/elspeth/web/config.py`, directly after :557
`identity_pending_retention_days: int = Field(default=90, gt=0)`:

```python
    # The workflow-governance switch. "on" enforces the identity workflow:
    # approvals before execution (R2), reviewer attestations and library
    # curation. "off" leaves the workflow tables inert. Read directly by
    # every enforcing authority; nothing derives an "effective" value from it.
    #
    # R11 is NOT validated here on purpose. Enabling governance under
    # auth_provider=local with registration_mode=open — where one person can
    # hold many identities and every author-is-not-approver CHECK is
    # defeatable — is refused by the auth_mode READINESS check
    # (web/readiness.py::_check_auth_mode), which names both settings in the
    # /api/ready body. A load-time refusal here would be a traceback an
    # operator cannot read from the platform, and it would make the
    # combination unconstructible for the readiness fire test. The same check
    # refuses "on" without a compartment_id, because library rows and audit
    # metadata carry the marking (sessions/models.py: library_entries.compartment_id is NOT NULL).
    workflow_governance: Literal["off", "on"] = "off"
```

`Literal` is already imported at config.py:14 (it types
`operator_telemetry` at :205). No validator, no `_JSON_COLLECTION_FIELDS`
entry: `settings_from_env` passes the raw string and the `Literal` rejects
anything but `off`/`on` with `Input should be 'off' or 'on'`.

- [ ] **Step 6: Run the config tests to verify they pass, including the spelling test for the right reason.**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/test_config.py tests/unit/web/auth/test_provider_type_contract.py tests/unit/architecture/test_legacy_oidc_path_deletion_guard.py -n 0 > /tmp/i8-config-green.log 2>&1; echo exit=$?
```

Expected: `exit=0`. Then prove the spelling test discriminates on the value,
not on the field name:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && python - <<'PY'
from pydantic import ValidationError
from tests.unit.web.test_config import _settings
try:
    _settings(workflow_governance="true")
except ValidationError as exc:
    print(exc.errors()[0]["msg"])
PY
```

Expected output: `Input should be 'off' or 'on'`. (The three extra test
files are the structural pins over `WebSettings.model_fields` —
test_provider_type_contract.py:164, test_legacy_oidc_path_deletion_guard.py:141
— and `TestFieldValidatorCoverage` (test_config.py:1502), which
enumerates `str | None` fields; a `Literal` field is not one, so it is
unaffected, and the run proves it.)

- [ ] **Step 7: Extend `_check_auth_mode` with the R11 and compartment arms.**

Replace `src/elspeth/web/readiness.py:315-345` (the whole `_check_auth_mode`)
with:

```python
def _workflow_governance_refusal(settings: WebSettings, provider: str) -> str | None:
    """R11: the ONE place the governance predicate lives. Returns the refusal detail, or None.

    Spec sso-design.md:1115-1124 (R11, D30, D31). ``provider`` is the
    already-validated runtime provider string, so this never sees a name
    outside the registry. Governance off is never refused: the default local
    tutorial deployment (local + open + off) must stay ready.

    The compartment arm exists because ``library_entries.compartment_id`` is
    NOT NULL while the setting is optional for local auth; a closed local
    deployment would otherwise reach the library publish path and fail with
    an IntegrityError instead of a named readiness refusal. Under an IdP the
    profile already requires ``compartment_id`` (auth/providers/__init__.py:44),
    so that arm cannot fire there; it is still evaluated, not special-cased.
    """
    if settings.workflow_governance != "on":
        return None
    if provider == "local" and settings.registration_mode == "open":
        return (
            "workflow_governance=on is refused with auth_provider=local and registration_mode=open (R11): "
            "one person can hold many local identities, so every author-is-not-approver rule is defeatable; "
            "set registration_mode=closed or email_verified, or set workflow_governance=off"
        )
    if not configured_auth_settings(settings)["compartment_id"]:
        return "workflow_governance=on requires compartment_id: library rows and audit metadata carry the compartment marking"
    return None


def _governance_suffix(settings: WebSettings) -> str:
    return "; workflow governance on" if settings.workflow_governance == "on" else ""


def _check_auth_mode(settings: WebSettings) -> ReadinessCheck:
    """Report whether the ACTIVE profile has everything it needs.

    Driven entirely by the profile registry: there is no per-provider branch
    here, so adding an IdP cannot leave readiness silently reporting an
    unconfigured deployment as ready. The previous form was a hand-written
    if/elif over the three providers that existed when it was written, which
    meant a fifth provider fell through to "unsupported" — and the ECS runbook
    gates its traffic cutover on this check.

    The detail names each MISSING field, because an operator reading a
    not-ready readiness response has no other way to find out which of eight
    settings they left out of the task definition.

    R11 (workflow governance under open local registration) is decided here
    too, AFTER the profile is complete, so a missing IdP field is always
    reported as the missing field and never masked by a governance refusal.
    It is a readiness failure by design (D30): enforcement is never quietly
    switched off, the platform simply never routes traffic to the replica.
    """
    # Treat the runtime value as open even though validated settings narrow it
    # statically; readiness is a total boundary if state is corrupted/mocked.
    provider = str(settings.auth_provider)
    if provider == "local":
        refusal = _workflow_governance_refusal(settings, provider)
        if refusal is not None:
            return ReadinessCheck("auth_mode", False, refusal)
        return ReadinessCheck("auth_mode", True, f"local authentication configured{_governance_suffix(settings)}")
    if provider not in PROFILE_REGISTRY:
        return ReadinessCheck(
            "auth_mode",
            False,
            f"unsupported authentication provider (registered: {', '.join(registered_provider_names())})",
        )
    profile = PROFILE_REGISTRY[provider]
    configured = configured_auth_settings(settings)
    missing = [name for name in profile.required_settings if not configured[name]]
    if missing:
        return ReadinessCheck("auth_mode", False, f"{provider} configuration incomplete: missing {', '.join(missing)}")
    refusal = _workflow_governance_refusal(settings, provider)
    if refusal is not None:
        return ReadinessCheck("auth_mode", False, refusal)
    return ReadinessCheck("auth_mode", True, f"{provider} authentication configured{_governance_suffix(settings)}")
```

Nothing else in readiness.py changes: `readiness_report` already places
`_check_auth_mode(settings)` at `by_name["auth_mode"]` (:406) and `_finalize`
(:349-354) already logs `readiness_check_not_ready` for every failed check —
that log line is the "loudly" in D30 and the report-level test pins it.

- [ ] **Step 8: Add the `closed_local_settings` and `closed_local_app` fixtures.**

In `tests/unit/web/conftest.py`, replace the `test_client` fixture
(:104-173, from the `@pytest.fixture` decorator through `return client`) with
the helper plus three fixtures below. The helper body is the current
`test_client` body verbatim except that `settings` is a parameter; `test_client`
constructs the same `WebSettings` it constructs today, so its behaviour is
unchanged for every existing consumer.

```python
def _route_client(tmp_path: Path, settings: WebSettings) -> TestClient:
    """One in-memory session service behind a router-only FastAPI app, with ``alice`` as the current user.

    Shared by ``test_client`` (the open local deployment the tutorial ships)
    and ``closed_local_app`` (the closed, governance-on deployment every
    workflow-governance test must run against — spec R11). Only ``settings``
    differs, so a route behaviour that depends on the deployment shape is
    provably a settings difference and not a fixture difference.
    """
    eng = create_session_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    initialize_session_schema(eng)
    service = DualFencedSessionServiceHarness(
        eng,
        data_dir=tmp_path,
        telemetry=build_sessions_telemetry(),
        log=structlog.get_logger("test.phase3.web"),
    )
    app = FastAPI()
    identity = UserIdentity(user_id="alice", username="alice")
    with eng.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")

    async def mock_user() -> UserIdentity:
        return identity

    async def audit_access_log_write_error_handler(request, _exc):
        # Mirrors ``create_app``'s handler, including the ``request_id``
        # correlation field. This app has no ``RequestIdMiddleware``, so the
        # honest value here is None — same lenient read as production.
        return JSONResponse(
            status_code=500,
            content={
                "error_type": "audit_access_log_write_failed",
                "detail": "Audit-grade transcript access could not be recorded; no audit-grade data returned.",
                "request_id": (
                    request.scope["state"]["request_id"]
                    if type(request.scope.get("state")) is dict and type(request.scope["state"].get("request_id")) is str
                    else None
                ),
            },
        )

    app.dependency_overrides[get_current_user] = mock_user
    app.add_exception_handler(AuditAccessLogWriteError, audit_access_log_write_error_handler)
    app.state.session_service = service
    # Phase 8 Task 2: route-level telemetry emits read
    # ``request.app.state.sessions_telemetry``. The service already
    # holds the same container as ``_telemetry``; mirror it on
    # ``app.state`` so route-level tests can observe via either name
    # (matches production wiring in ``web/app.py:579``).
    app.state.sessions_telemetry = service._telemetry
    app.state.session_engine = eng
    app.state.settings = settings
    app.state.composer_service = None
    app.state.rate_limiter = ComposerRateLimiter(limit=100)
    app.state.execution_service = None
    app.state.composer_progress_registry = None
    app.state.scoped_secret_resolver = None
    app.include_router(create_session_router())
    client = TestClient(app)
    client.app.state.phase3_engine = eng
    client.app.state.phase3_sessions_service = service
    return client


@pytest.fixture
def test_client(tmp_path: Path) -> TestClient:
    """Sync ASGI test client with app state exposing ``sessions_service``.

    This is the OPEN local deployment (``registration_mode="open"``,
    ``workflow_governance="off"`` — both shipped defaults). Spec R11 names it
    as sitting in exactly the combination governance refuses, and
    ``tests/unit/web/test_readiness.py`` pins that. Workflow-governance tests
    use ``closed_local_app`` instead; never flip the switch on this one.
    """
    return _route_client(
        tmp_path,
        WebSettings(
            data_dir=tmp_path,
            composer_max_composition_turns=15,
            composer_max_discovery_turns=10,
            composer_timeout_seconds=85.0,
            composer_rate_limit_per_minute=10,
            shareable_link_signing_key=SecretBytes(b"\x00" * 32),
        ),
    )


@pytest.fixture
def closed_local_settings(tmp_path: Path) -> WebSettings:
    """The closed local deployment every workflow-governance test runs against (spec R11, :1308-1311).

    ``compartment_id`` is set because readiness refuses governance without one
    (``library_entries.compartment_id`` is NOT NULL); the value satisfies the
    ``^[a-z0-9][a-z0-9-]{0,62}$`` shape. The two quota defaults are the ones
    ``tests/unit/web/test_local_auth_wiring.py`` uses, so an activation in a
    governance test writes a policy row like production does.
    """
    return WebSettings(
        data_dir=tmp_path,
        auth_provider="local",
        registration_mode="closed",
        workflow_governance="on",
        compartment_id="test-compartment",
        quota_default_tokens_per_day=100_000,
        quota_default_storage_bytes=1_000_000,
        composer_max_composition_turns=15,
        composer_max_discovery_turns=10,
        composer_timeout_seconds=85.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"\x00" * 32),
    )


@pytest.fixture
def closed_local_app(tmp_path: Path, closed_local_settings: WebSettings) -> TestClient:
    """``test_client``'s closed, governance-on twin. Tasks I3/I4/I5/I9 ``include_router`` their workflow routers on ``.app``."""
    return _route_client(tmp_path, closed_local_settings)
```

No new imports: `WebSettings`, `SecretBytes`, `Path`, `TestClient` and every
name the helper uses are already imported at conftest.py:33-59.

- [ ] **Step 9: Run the readiness tests and the whole `tests/unit/web` package (the conftest refactor touches every consumer of `test_client`).**

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web/test_readiness.py tests/unit/web/test_config.py -n 0 > /tmp/i8-unit-green.log 2>&1; echo exit=$?
```

Expected: `exit=0`.

Run:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/web > /tmp/i8-web-package.log 2>&1; echo exit=$?
```

Expected: `exit=0` (this is the `-n 12` default run; the package is the
blast radius of `_route_client`). If a `test_client` consumer fails, the
helper drifted from the :105-173 body — diff them; do not patch the consumer.

Run the lint and type gates on the touched files:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && ruff check src/elspeth/web/config.py src/elspeth/web/readiness.py tests/unit/web/test_readiness.py tests/unit/web/test_config.py tests/unit/web/conftest.py > /tmp/i8-ruff.log 2>&1; echo exit=$?; ruff format --check src/elspeth/web/config.py src/elspeth/web/readiness.py tests/unit/web/test_readiness.py tests/unit/web/test_config.py tests/unit/web/conftest.py >> /tmp/i8-ruff.log 2>&1; echo exit=$?; mypy src/elspeth/web/config.py src/elspeth/web/readiness.py > /tmp/i8-mypy.log 2>&1; echo exit=$?
```

Expected: three `exit=0` lines. This task touches no schema, SQL, session
persistence or lock, so no testcontainer selection is required; say so in
the commit body.

- [ ] **Step 10: Document the setting in both reference pages and the changelog.**

`docs/reference/configuration.md`: after the `dev_admin_user` row (:372) add
the row

```markdown
| `workflow_governance` | string | No | `"off"` | `off` or `on`. `on` enforces the identity workflow — approvals before execution (R2), reviewer attestations and library curation; `off` leaves the workflow tables inert. Readiness (`auth_mode`) refuses `on` under `auth_provider: local` with `registration_mode: open` (R11: one person can hold many local identities, so every author-is-not-approver rule is defeatable) and refuses `on` without a `compartment_id`. The refusal is a `503` from `/api/ready` naming both settings, never a silent off-switch |
```

and rewrite the `compartment_id` row (:387) description from
`Operator-declared marking for this container's identities and artifacts. Validated non-blank; no runtime path reads it in this release`
to
`Operator-declared marking for this container's identities and artifacts. Validated non-blank. Required by readiness whenever `workflow_governance` is `on` (library rows and audit metadata carry the marking); otherwise no runtime path reads it in this release`.

`docs/reference/environment-variables.md`: after the
`ELSPETH_WEB__PUBLIC_BASE_URL` row (:72) add

```markdown
| `ELSPETH_WEB__WORKFLOW_GOVERNANCE` | Identity-workflow enforcement switch: `off` or `on` | `off` |
```

and after the `### ELSPETH_WEB__REGISTRATION_MODE` section (:90-100, ending
`query, or fragment.`) add

```markdown
### ELSPETH_WEB__WORKFLOW_GOVERNANCE

`on` enforces the identity workflow: approvals before execution, reviewer
attestations and library curation. `off` (the default) leaves those tables
inert. Readiness refuses `on` under `ELSPETH_WEB__AUTH_PROVIDER=local` with
`ELSPETH_WEB__REGISTRATION_MODE=open` (R11) and refuses `on` without
`ELSPETH_WEB__COMPARTMENT_ID`; `/api/ready` returns `503` with the `auth_mode`
check naming both settings, so the platform never routes traffic to that
replica. Set the registration mode to `closed` or `email_verified` first.
```

`CHANGELOG.md`: after the "**Authentication events in signed exports.**"
bullet (:35-38, ending `is invented from current rows.`) add

```markdown
- **Workflow-governance switch.** `ELSPETH_WEB__WORKFLOW_GOVERNANCE=on`
  enables approval, attestation and library enforcement. Readiness refuses it
  under local authentication with open registration (R11) and without a
  compartment id, naming both settings in the `auth_mode` check rather than
  switching enforcement off silently.
```

Then re-run the docs pins that read these files:

```bash
cd "$(git rev-parse --show-toplevel)" && source .venv/bin/activate && pytest tests/unit/docs/test_release_version_surfaces.py tests/unit/docs/test_deployment_platform_docs.py tests/unit/docs/test_changelog_release_links.py tests/unit/website/test_release_site_contract.py -n 0 > /tmp/i8-docs.log 2>&1; echo exit=$?
```

Expected: `exit=0`.

- [ ] **Step 11: Branch safety, then commit by pathspec.**

```bash
cd "$(git rev-parse --show-toplevel)" && scripts/branch-safety-check.sh --intent commit
```

Expected: no `[FAIL]` line. Then:

```bash
cd "$(git rev-parse --show-toplevel)" && git status --short && git commit -m "feat(identity): workflow_governance switch and the R11 readiness refusal

WebSettings.workflow_governance (off|on, default off) is the switch every
identity-workflow enforcement reads. R11 is a readiness refusal in
_check_auth_mode, naming auth_provider, registration_mode and
workflow_governance, and additionally refusing governance without a
compartment_id (library_entries.compartment_id is NOT NULL). No schema,
SQL or lock touched; no testcontainer selection applies." -- src/elspeth/web/config.py src/elspeth/web/readiness.py tests/unit/web/test_readiness.py tests/unit/web/test_config.py tests/unit/web/conftest.py docs/reference/configuration.md docs/reference/environment-variables.md CHANGELOG.md
```

Confirm `git show --stat HEAD` lists exactly those eight files.
