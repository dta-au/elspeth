### Task I6: Compartment marking

> **Current execution note (2026-09-19):** The
> [execution map](../2026-09-19-identity-workflow-finalization.md)
> extends the original I6 draft. Ingress includes user text pasted into
> Composer chat when it creates a composition state, as well as YAML import
> and library fork. Every enabled Landscape export uses the compartment-marked
> auth-v2 contract, signed or unsigned, including verification and resume;
> auth-v1 is not retained. Historical source and epoch references below still
> need re-anchoring when a step is executed.

> Part of the [Kubernetes and Identity Workflow master plan](2026-09-13-kubernetes-and-identity-master-plan.md). Read its [Global Constraints](2026-09-13-kubernetes-and-identity-master-plan.md#global-constraints) first: they apply to every task. Runs after: I4, I5. Runs before: I7. Full ordering: [Workstream layout and ordering](2026-09-13-kubernetes-and-identity-master-plan.md#workstream-layout-and-ordering). Open operator decisions: [Self-review notes](2026-09-13-kubernetes-and-identity-master-plan.md#self-review-notes).

Ordered after I5 (it edits I5's `seed_state_from_runtime_yaml`, I5's library
audit metadata and I5's tests) and therefore after I0, I8, I1, I2, I3 and I4.
Spec: sso-design.md §Guiding principle — compartments (:1441-1463, rev2.2)
and "On the five stampings" (:1465-1477, rev2.12), §Workflow tables row
`library_entries` (:1420). Every `path:line` below was measured on HEAD
072141b75; earlier tasks add lines above some of them, so every edit below is
anchored on the quoted text, not on the number.

What the spec asks for and what this task delivers:

1. **Setting shape.** `compartment_id` already exists (`config.py:545`) with
   the blank check (`_reject_blank_auth_fields`, `"compartment_id",` at
   :662). This task adds only the `^[a-z0-9][a-z0-9-]{0,62}$` validator. The
   one tracked producer that violates it is the AWS scenario module, which
   exports `var.scenario_id` (`A`/`B`/`C`,
   `deploy/aws-ecs/terraform/modules/scenario/variables.tf:14`) at
   `locals.tf:544`; it switches to the existing `local.scenario_id_lower`
   (`locals.tf:3`). Every other value in tests and docs is already
   lowercase-hyphen (`example-compartment`, `compartment-a`, `integration`,
   and I5/I8's `alpha` / `test-compartment`).
2. **Public YAML: a comment line, not a document key.** The lowered pipeline
   document has no `metadata` section (`yaml_generator.py:416-478`) and the
   importer refuses a top-level `metadata` key and every unknown top-level key
   (`yaml_importer.py:112`, `_DECLINED_SECTION_REASONS`). Two consumers feed
   `generate_public_yaml` output straight back into the importer
   (`_aws_ecs_acceptance/capture.py:248`; I5's library fork through
   `seed_state_from_runtime_yaml`), so a key would make ELSPETH's own export
   unimportable. The marking is therefore `# compartment_id: <id>` as the
   first line of the document the download route hands the user
   (`sessions/routes/composer/state.py:1246`, beside
   `public_export_redaction_header`, which the `generate_public_yaml`
   docstring :852-878 already places at that route for the same reason).
   `generate_public_yaml` bytes do not change, so I5's `payload_digest`, the
   MCP tool output and the custody-egress substring guards are untouched.
3. **Shareable snapshot.** The snapshot blob gains the key `compartment_id`
   (`shareable_reviews/service.py`: `_BlobShape` :193, `_BLOB_KEYS` :235,
   `_build_snapshot` :275, call :496). `resolve_token` does not read it and
   `SharedInspectResponse` does not change, so legacy blobs and the frontend
   decoder (`frontend/src/api/shareableReviews.ts:133-176`) are unaffected.
   New mints of an unchanged composition get a new `payload_digest` on a
   deployment whose `compartment_id` is set.
4. **`library_entries` rows: already stamped by I5.** I5's publish route
   passes `settings.compartment_id` and `publish` writes it to the NOT NULL
   column; I5's integration test asserts `entry["compartment_id"] == "alpha"`.
   `library_authority.py` needs no edit. This task adds one pin: the
   projection bytes (and therefore `payload_digest`, the cross-container
   detection key, spec :1420) are the same in every compartment.
5. **Every `auth_events.metadata_json`: stamped centrally.** `AuthAuditRecorder`
   (`web/auth/audit.py:430`) gains `compartment_id: str | None = None`;
   `from_settings` (:475) passes `settings.compartment_id`. All sixteen HEAD
   sites that write through `RecorderFactory(db).auth_audit` (:517, :541,
   :569, :601, :622, :658, :729, :783, :843, :896, :926, :984, :1053, :1083,
   :1121, :1160), plus every writer I1, I3, I4 and I5 added the same way, go
   through one proxy, `_CompartmentStampedAuthAudit`, which puts
   `compartment_id` into every row's metadata. The key is always present
   (`None` when unconfigured), the rule `_admin_provenance` (:1206) applies to
   its console keys. An AST pin in `test_audit.py` allows exactly one
   `RecorderFactory(` call in the module. The proxy refuses a caller-supplied
   `compartment_id` key, so I5's two library metadata dicts rename the
   entry's own value to `entry_compartment_id`. The composer CLI's recorder
   (`cli.py:1977`, `_composer_auth_audit_recorder`) builds no `WebSettings`; it
   reads `ELSPETH_WEB__COMPARTMENT_ID` from the service environment, the way it
   already reads `ELSPETH_WEB__LANDSCAPE_PASSPHRASE` (:1993), through the same
   shape check. `auth_events` is a Landscape
   table; no Landscape column, CHECK or epoch changes (metadata is free-form
   JSON, `core/landscape/auth_audit_repository.py:153`).
6. **Ingress record.** `seed_state_from_runtime_yaml` (I5) writes
   `composer_meta["ingress"] = {"text_sha256": "<hex>", "foreign_compartment_ids": ["<foreign id>"]}`
   on the state it creates, computed after every import validator and before
   `save_composition_state_with_interpretations`, which reaches
   `_insert_composition_state` (`sessions/service.py:6377`,
   `composer_meta=_enveloped_state_column(state.composer_meta)`). That covers
   the paste import and I5's library fork in one place. No new
   `auth_events` type, no Landscape change. `composer_meta` is an open
   envelope (`sessions/routes/sessions.py:595`; frontend type
   `Record<string, unknown> | null`, `frontend/src/types/index.ts:266`), and
   the fork refusal only rejects keys that reference parent blob custody
   (`sessions/service.py:1912-1953`), which a sha256 and compartment ids
   cannot. `merge_composer_meta_updates` (`routes/_helpers.py:958`) carries
   the key forward, so every descendant state names the text its lineage was
   seeded from until the next seed replaces it. Composer chat also records
   ingress when pasted user text creates a composition state.
7. **Landscape exports: delivered in the auth-v2 contract.** The closed
   `audit_export_config.public_config` carries `compartment_id` for every
   enabled signed or unsigned export. The public configuration hash binds
   that marking to snapshot identity; HMAC signatures bind the signed record
   stream and manifest. Producer, registered reader, CLI fresh run, Web
   execution and resume require auth-v2 and a valid marking. Auth-v1 export
   and replay are refused, including when an old snapshot row exists for the
   run. The `audit-export-derivation-v1` label describes a separate
   cryptographic version domain and remains unchanged.

The sessions DB mutation-authority manifest gains no writer:
every composition-state insert still goes through the reviewed
`SessionServiceImpl._insert_composition_state`. One existing row moves by line
only (`ShareableReviewService._latest_mark_ready_event`, Step 22).

**Files:**
- Create: `src/elspeth/web/composer/compartment_marking.py` (the shape, the egress line, the ingress record; stdlib only, so `config.py` can import it the way it imports `elspeth.web.composer.reasoning` at :36)
- Create: `tests/unit/web/composer/test_compartment_marking.py`
- Create: `tests/integration/web/test_compartment_marking.py`
- Modify: `src/elspeth/web/config.py:36` (import) and after `_reject_blank_auth_fields` (:649-670, def at :665; the next decorator is `@field_validator("deployment_aws_region")` at :672): the shape validator
- Modify: `deploy/aws-ecs/terraform/modules/scenario/locals.tf:542-544` (comment and `ELSPETH_WEB__COMPARTMENT_ID` value)
- Modify: `src/elspeth/web/auth/audit.py:5` (`from collections.abc import Iterator`), the `elspeth.core.landscape.database` import line, `:430-443` (`@dataclass class AuthAuditRecorder` and its fields; `create_tables: bool` at :440), `:475-490` (`from_settings`; `create_tables=state_mode == "sqlite-single",` at :489), `:492-502` (`_open_landscape`, ending `raise` at :502), every `RecorderFactory(db).auth_audit` token, and I5's two `"compartment_id": compartment_id,` metadata entries
- Modify: `src/elspeth/cli.py:1977-1997` (`_composer_auth_audit_recorder`; its local import `from elspeth.web.auth.audit import AuthAuditRecorder` at :1981, `return AuthAuditRecorder(` at :1991)
- Modify: `src/elspeth/web/sessions/models.py:3281-3282` (the comment claiming SSO login metadata holds exactly `{"method", "path"}`)
- Modify: `src/elspeth/web/shareable_reviews/service.py:223` (`created_by_username: NotRequired[str]`), `:240-244` (`_BLOB_KEYS` tail), `:280-282` (`_build_snapshot` signature tail), `:287` (docstring key list), `:330-331` (blob literal), `:496-502` (the `_build_snapshot(` call in `mark_ready_for_review`)
- Modify: `src/elspeth/web/sessions/routes/composer/state.py:19` (import anchor `from elspeth.web.composer.guided.errors import InvariantError`), `:1246` (`yaml_str = public_export_redaction_header(export_state) + generate_public_yaml(export_state)`), and I5's `seed_state_from_runtime_yaml` merge block (`if composer_meta_updates is not None:`)
- Modify: `config/cicd/soft-mapping-census.yaml` (re-pinned by `scripts.check_contracts --write-census`; the `src/elspeth/web/auth/audit.py` counts move)
- Modify: `tests/unit/architecture/test_session_db_mutation_authority.py:4645-4654` (the `ShareableReviewService._latest_mark_ready_event` `WriterIdentity`, `line=685`)
- Modify: `tests/unit/web/test_config.py` (append after `class TestInstanceId`, :2219 to end of file :2259; helper `_settings(**overrides)` :1599, `ValidationError` imported :16)
- Modify: `tests/unit/web/auth/test_audit.py:38-50` (`_settings` SimpleNamespace, last field `get_session_db_url=lambda: session_url,` at :49), `:401-418` (`assert_called_once_with(` with an exact `metadata=` dict), `:895` (logout metadata pin), I5's `test_library_rows_anchor_on_the_publisher_and_carry_digest_and_compartment`, append at end of file (helpers `_request` :131, `_durable_rows` :634, `_metadata` :644; `AuthAuditRepository` :15, `create_autospec` :8, `audit_module` :19 already imported)
- Modify: `tests/unit/cli/test_web_command.py` (two tests inserted in `class TestComposerUsersBootstrapAdmin` :438 before `test_bootstrap_admin_is_refused_once_an_admin_exists`; `_invoke` :441, `_auth_event_rows` :545, `json` :6, `pytest` :16; its other metadata assertions read single keys, :364-367 and :479-482)
- Modify: `tests/unit/web/auth/test_routes.py:1586` (logout metadata pin; that app's recorder is `AuthAuditRecorder.from_settings(app.state.settings)` at :152 over settings with no compartment, :119-134)
- Modify: `tests/unit/web/shareable_reviews/test_service.py:226-228` (`@dataclass(slots=True) class _FakeSettings`), `:495-508` (`_build_service`; `readiness: AuditReadinessSnapshot,` :503, `settings = _FakeSettings()` :508), append at end of file (fixtures `session_engine_with_row` :284, `session_record` :262, `state_record` :267, `session_operation_context` :330, `payload_store` :242, `signer` :247; helpers `_ok_validation` :349, `_readiness_snapshot` :454, `_OWNER_USERNAME` :80)
- Modify: `tests/unit/web/sessions/test_routes.py:5` (`import asyncio`; add `import hashlib`), append at end of file :15627 (`_make_app` :847, `TestClient` :114, `deep_thaw` :38, `yaml` :20)
- Modify: `tests/unit/web/coordination/test_library_authority.py` (I5's file; append; helpers `_authority`, `_publish`, `_state`)
- Modify: `tests/integration/web/workflow/test_library.py` (I5's file; one assertion after `assert set(seeded.sources or {}) == {"source"}`)
- Modify: `tests/unit/deployment/test_web_settings_exports_resolve.py` (append; `REPO_ROOT` defined in its header)
- Modify: `docs/reference/configuration.md:387` (the `compartment_id` row, as I8 rewrote it), `docs/guides/identity-providers.md:507-514`, `CHANGELOG.md` (bullet directly before `- **Coordination deadlines are decided from fresh post-lock database time.**`, :40 on HEAD)

**Interfaces:**
- Consumes:
  - I5: `seed_state_from_runtime_yaml(*, session: SessionRecord, body: ImportStateYamlRequest, request: Request, user: UserIdentity, composer_meta_updates: Mapping[str, Any] | None = None) -> CompositionStateResponse` in `routes/composer/state.py`, whose only addition over `import_state_yaml` is the block `if composer_meta_updates is not None: state_data = replace(state_data, composer_meta=merge_composer_meta_updates(state_data.composer_meta, composer_meta_updates))` immediately before `service.save_composition_state_with_interpretations(`; the library fork route calling it with `composer_meta_updates={"library_fork": <provenance mapping>}`; `RepositoryLibraryAuthority.publish(*, session_id, state, title, published_by, compartment_id, record)` storing `generate_public_yaml(export_state)` under `public_projection_digest(export_state)`; `AuthAuditRecorder.record_library_published` and `_record_library_curation`, each metadata dict carrying `"compartment_id": compartment_id,`; tests `_authority(engine, tmp_path)`, `_publish(authority, *, published_by="alice", state=None, compartment_id="alpha", title="classify tickets", events=None)`, `_state()`, `test_library_rows_anchor_on_the_publisher_and_carry_digest_and_compartment`, and in `tests/integration/web/workflow/test_library.py` the fork test's locals `entry` (publish response JSON) and `seeded` (the forked session's current state record).
  - I8: `WebSettings.workflow_governance`, the `_check_auth_mode` rule that `on` requires `compartment_id`, fixture `closed_local_settings` with `compartment_id="test-compartment"` (matches the new validator), and I8's rewritten `docs/reference/configuration.md` `compartment_id` row text quoted in Step 24.
  - I1, I3, I4, I5: every `AuthAuditRecorder` writer they add opens `self._open_landscape(<operation>)` and writes through `RecorderFactory(db).auth_audit` (Step 11 rewrites the token wherever it occurs).
  - HEAD: `WebSettings.compartment_id: str | None = None` (config.py:545), `_reject_blank_auth_fields` (:665); `AuthAuditRepository.record_auth_event` / `record_login_success_and_token_issued` / `record_login_outcome` / `record_token_issued` / `record_auth_failure` (auth_audit_repository.py:156, :191, :237, :266, :293), `AuthAuditEventType` (:20), `AuthAuditOutcome` (:66); `RecorderFactory(db).auth_audit` (core/landscape/factory.py:517); `_build_snapshot(*, session_id, state_record, audit_readiness, created_by_user_id, created_by_username) -> _Snapshot` (shareable_reviews/service.py:275); `ShareableReviewService._settings` (set from the constructor's `settings: WebSettings`); `get_state_yaml` (routes/composer/state.py:1180); `merge_composer_meta_updates` (routes/_helpers.py:958); `deep_thaw` (contracts/freeze.py:126); fixtures `audit_readiness_client_with_state` (tests/integration/web/conftest.py:517).
- Produces:
  - `src/elspeth/web/composer/compartment_marking.py`: `COMPARTMENT_ID_PATTERN: Final = r"[a-z0-9][a-z0-9-]{0,62}"`, `COMPARTMENT_ID_RE: Final[re.Pattern[str]]`, `COMPARTMENT_MARKING_PREFIX: Final = "# compartment_id: "`, `class CompositionIngressRecord(TypedDict): text_sha256: str; foreign_compartment_ids: list[str]`, `is_compartment_id(value: str) -> bool`, `compartment_marking_header(compartment_id: str | None) -> str` (`""` for `None`, raises `ValueError` on a bad shape), `compartment_ingress_record(text: str, *, own_compartment_id: str | None) -> CompositionIngressRecord`.
  - `WebSettings` refuses a non-`None` `compartment_id` that is not a full match of `COMPARTMENT_ID_PATTERN`, with a message starting `compartment_id must match`.
  - `AuthAuditRecorder.compartment_id: str | None = None` (dataclass field; `from_settings` fills it); `AuthAuditRecorder._auth_audit(db: LandscapeDB) -> _CompartmentStampedAuthAudit`; every `auth_events.metadata_json` row written by `AuthAuditRecorder` carries `"compartment_id": <str | null>`; I5's library rows carry the entry's own value as `"entry_compartment_id"`; CLI-written rows carry `ELSPETH_WEB__COMPARTMENT_ID` (the CLI exits 1 on a malformed value before any write). I9's audit view and I10's governance suite read these keys.
  - The shareable-review snapshot blob carries `"compartment_id": <str | null>`.
  - `GET /api/sessions/{session_id}/state/yaml` returns `yaml` whose first line is `# compartment_id: <id>` when the deployment has a compartment.
  - Every state created by `seed_state_from_runtime_yaml` carries `composer_meta["ingress"]: CompositionIngressRecord`; for a library fork `text_sha256 == library_entries.payload_digest`.

- [ ] **Step 0: Record the trust-tier lint corpus before any edit.**

The gate exits 1 on this tree by design (AGENTS.md § Judge-signature stage); the deliverable is "no new findings", measured by diff in Step 26.

Run: `cd "$(git rev-parse --show-toplevel)" && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth > /tmp/i6-lints-before.log 2>&1; echo exit=$?`
Expected: `exit=1` with the existing finding corpus in the log.

- [ ] **Step 1: Write the failing tests for the marking module.**

Create `tests/unit/web/composer/test_compartment_marking.py`:

```python
"""The compartment marking authority: the setting's shape, the egress line, the ingress record.

Spec: sso-design.md §Guiding principle — compartments (rev2.2, rev2.12).
One module serves the config validator, the YAML download and the YAML seed
path, so the line ELSPETH writes and the line it reads back cannot drift.
"""

from __future__ import annotations

import hashlib

import pytest
import yaml

from elspeth.web.composer.compartment_marking import (
    COMPARTMENT_MARKING_PREFIX,
    compartment_ingress_record,
    compartment_marking_header,
    is_compartment_id,
)


@pytest.mark.parametrize(
    "value",
    ["a", "0", "alpha", "compartment-a", "test-compartment", "example-compartment", "integration", "a" * 63],
)
def test_admitted_shapes(value: str) -> None:
    assert is_compartment_id(value)


@pytest.mark.parametrize(
    "value",
    ["", "A", "Alpha", "-alpha", "alpha_beta", "alpha beta", "alpha.", "alpha\n", "é", "a" * 64],
)
def test_refused_shapes(value: str) -> None:
    assert not is_compartment_id(value)


def test_header_is_one_comment_line_and_empty_without_a_compartment() -> None:
    assert compartment_marking_header("alpha") == "# compartment_id: alpha\n"
    assert compartment_marking_header("alpha").startswith(COMPARTMENT_MARKING_PREFIX)
    assert compartment_marking_header(None) == ""


def test_header_refuses_a_shape_the_setting_refuses() -> None:
    with pytest.raises(ValueError, match=r"compartment_id 'Alpha' does not match"):
        compartment_marking_header("Alpha")


def test_the_header_is_invisible_to_the_yaml_parser() -> None:
    body = "sources:\n  source:\n    plugin: csv\n"
    assert yaml.safe_load(compartment_marking_header("alpha") + body) == yaml.safe_load(body)


def test_ingress_records_the_exact_text_digest_and_only_foreign_markings() -> None:
    text = "# compartment_id: other\r\n  compartment_id: third\n# compartment_id: home\n# compartment_id: other\nsinks: {}\n"
    assert compartment_ingress_record(text, own_compartment_id="home") == {
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "foreign_compartment_ids": ["other", "third"],
    }


def test_ingress_foreign_set_derives_from_the_configured_compartment() -> None:
    text = "# compartment_id: other\n# compartment_id: home\n"
    assert compartment_ingress_record(text, own_compartment_id="home")["foreign_compartment_ids"] == ["other"]
    assert compartment_ingress_record(text, own_compartment_id="other")["foreign_compartment_ids"] == ["home"]
    assert compartment_ingress_record(text, own_compartment_id=None)["foreign_compartment_ids"] == ["home", "other"]


def test_ingress_reads_back_exactly_the_line_egress_writes() -> None:
    written = compartment_marking_header("alpha")
    assert compartment_ingress_record(written, own_compartment_id="beta")["foreign_compartment_ids"] == ["alpha"]
    assert compartment_ingress_record(written, own_compartment_id="alpha")["foreign_compartment_ids"] == []


def test_ingress_ignores_anything_that_is_not_a_whole_marking_line() -> None:
    text = (
        "note: compartment_id: other\n"
        "# compartment_id: Other\n"
        "# compartment_id: other trailing\n"
        "# compartment_id: " + "a" * 64 + "\n"
    )
    assert compartment_ingress_record(text, own_compartment_id=None)["foreign_compartment_ids"] == []
```

- [ ] **Step 2: Run the module tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/composer/test_compartment_marking.py -n 0 > /tmp/i6-marking-red.log 2>&1; echo exit=$?`
Expected: `exit=2`; the log shows `ModuleNotFoundError: No module named 'elspeth.web.composer.compartment_marking'` during collection.

- [ ] **Step 3: Create the marking module.**

Create `src/elspeth/web/composer/compartment_marking.py`:

```python
"""The compartment marking: its shape, the egress line, and the ingress record.

Spec (sso-design.md §Guiding principle — compartments, rev2.2 and rev2.12):
every container has an operator-set ``compartment_id`` stamped into what
leaves it, and a composition state created from pasted text records the
sha256 of that text and any foreign marking it carries. Recording, never
authoring: nothing here reads pipeline structure out of the text (composer
invariant 1).

One authority for three consumers, the way ``yaml_generator``'s redaction
marker prefixes are shared by the exporter and the importer:

* ``WebSettings`` validates the setting with :func:`is_compartment_id`;
* the YAML download route prepends :func:`compartment_marking_header`;
* the YAML seed path records :func:`compartment_ingress_record`.

The marking is a YAML COMMENT line, never a document key. The importer
refuses a top-level ``metadata`` section and every unknown top-level key
(``yaml_importer._DECLINED_SECTION_REASONS``), and the public projection is
fed back into the importer by the ECS acceptance capture and by the library
fork, so a key would make ELSPETH's own export unimportable.
"""

from __future__ import annotations

import hashlib
import re
from typing import Final, TypedDict

COMPARTMENT_ID_PATTERN: Final = r"[a-z0-9][a-z0-9-]{0,62}"
COMPARTMENT_ID_RE: Final[re.Pattern[str]] = re.compile(COMPARTMENT_ID_PATTERN)
COMPARTMENT_MARKING_PREFIX: Final = "# compartment_id: "

# Tier-3 parse of untrusted pasted text. Line-anchored under re.MULTILINE and
# never re.DOTALL; the capture is the setting's own bounded class, so a match
# cannot span lines or exceed 63 characters. The optional ``#`` admits both
# the comment line this module writes and a bare ``compartment_id:`` line a
# person typed; the optional ``\r`` admits CRLF paste.
_MARKING_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*#?[ \t]*compartment_id:[ \t]*(" + COMPARTMENT_ID_PATTERN + r")[ \t]*\r?$",
    re.MULTILINE,
)


class CompositionIngressRecord(TypedDict):
    """``composer_meta["ingress"]`` on a composition state seeded from pasted text."""

    text_sha256: str
    foreign_compartment_ids: list[str]


def is_compartment_id(value: str) -> bool:
    """Whether ``value`` is a whole-string match of the marking shape."""
    return COMPARTMENT_ID_RE.fullmatch(value) is not None


def compartment_marking_header(compartment_id: str | None) -> str:
    """The marking line for a document a user downloads; ``""`` when no compartment is configured."""
    if compartment_id is None:
        return ""
    if not is_compartment_id(compartment_id):
        raise ValueError(f"compartment_id {compartment_id!r} does not match ^{COMPARTMENT_ID_PATTERN}$")
    return f"{COMPARTMENT_MARKING_PREFIX}{compartment_id}\n"


def compartment_ingress_record(text: str, *, own_compartment_id: str | None) -> CompositionIngressRecord:
    """The sha256 of the exact pasted text and every marking in it that is not this container's.

    Sorted and de-duplicated, so the record is a pure function of the text and
    the setting. With no compartment configured every marking is foreign: the
    deployment cannot claim any of them. ``surrogatepass`` keeps the digest
    total over any ``str`` a JSON body can carry.
    """
    found = set(_MARKING_LINE_RE.findall(text))
    if own_compartment_id is not None:
        found.discard(own_compartment_id)
    return {
        "text_sha256": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
        "foreign_compartment_ids": sorted(found),
    }
```

- [ ] **Step 4: Run the module tests to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/composer/test_compartment_marking.py -n 0 > /tmp/i6-marking.log 2>&1; echo exit=$?`
Expected: `exit=0`, `25 passed`.

- [ ] **Step 5: Write the failing config and deployment tests.**

Append to the end of `tests/unit/web/test_config.py` (after `class TestInstanceId`):

```python


class TestCompartmentIdShape:
    """``compartment_id`` is written as a YAML comment line and read back out of pasted text (Task I6)."""

    @pytest.mark.parametrize("value", ["alpha", "compartment-a", "test-compartment", "example-compartment", "integration", "a" * 63])
    def test_admitted(self, value: str) -> None:
        assert _settings(compartment_id=value).compartment_id == value

    @pytest.mark.parametrize("value", ["A", "Alpha", "-alpha", "alpha_beta", "alpha beta", "alpha\n", "a" * 64])
    def test_refused_by_shape(self, value: str) -> None:
        with pytest.raises(ValidationError, match=r"compartment_id must match \^\[a-z0-9\]\[a-z0-9-\]\{0,62\}\$"):
            _settings(compartment_id=value)

    def test_blank_keeps_the_blank_refusal(self) -> None:
        with pytest.raises(ValidationError, match="must not be blank"):
            _settings(compartment_id="   ")

    def test_unset_stays_admitted(self) -> None:
        assert _settings().compartment_id is None
```

Append to the end of `tests/unit/deployment/test_web_settings_exports_resolve.py`:

```python


def test_the_ecs_scenario_exports_a_valid_compartment_marking() -> None:
    """``var.scenario_id`` is ``A``/``B``/``C``; the marking shape is lowercase (Task I6)."""
    from elspeth.web.composer.compartment_marking import is_compartment_id

    locals_tf = (REPO_ROOT / "deploy/aws-ecs/terraform/modules/scenario/locals.tf").read_text(encoding="utf-8")
    assert "  scenario_id_lower = lower(var.scenario_id)\n" in locals_tf
    assert '{ name = "ELSPETH_WEB__COMPARTMENT_ID", value = local.scenario_id_lower },' in locals_tf
    assert '{ name = "ELSPETH_WEB__COMPARTMENT_ID", value = var.scenario_id },' not in locals_tf
    assert [is_compartment_id(letter) for letter in ("A", "B", "C")] == [False, False, False]
    assert [is_compartment_id(letter.lower()) for letter in ("A", "B", "C")] == [True, True, True]
```

- [ ] **Step 6: Run them to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/test_config.py::TestCompartmentIdShape tests/unit/deployment/test_web_settings_exports_resolve.py::test_the_ecs_scenario_exports_a_valid_compartment_marking -n 0 > /tmp/i6-config-red.log 2>&1; echo exit=$?`
Expected: `exit=1`; the seven `test_refused_by_shape` cases fail with `Failed: DID NOT RAISE <class 'pydantic_core._pydantic_core.ValidationError'>`, and the deployment test fails with `AssertionError` on the `local.scenario_id_lower` line. `test_admitted`, `test_blank_keeps_the_blank_refusal` and `test_unset_stays_admitted` pass (they pin behaviour that must not move).

- [ ] **Step 7: Add the validator and fix the Terraform producer.**

In `src/elspeth/web/config.py`, directly after `from elspeth.web.composer.reasoning import ReasoningEffort` (:36):

```python
from elspeth.web.composer.compartment_marking import COMPARTMENT_ID_PATTERN, is_compartment_id
```

Directly after the body of `_reject_blank_auth_fields` (the `return v` that precedes `@field_validator("deployment_aws_region")`, :670-672), insert:

```python
    @field_validator("compartment_id")
    @classmethod
    def _validate_compartment_id_shape(cls, value: str | None) -> str | None:
        # Declared after ``_reject_blank_auth_fields``, so a blank value keeps
        # its blank refusal. The shape is the marking's own authority
        # (web/composer/compartment_marking.py): the value is written verbatim
        # as a YAML comment line on export and matched back out of pasted
        # text on import, so it may carry no whitespace, ``#`` or newline.
        if value is not None and not is_compartment_id(value):
            raise ValueError(
                f"compartment_id must match ^{COMPARTMENT_ID_PATTERN}$ "
                "(lowercase letters, digits and hyphens; 1 to 63 characters; not starting with a hyphen)"
            )
        return value
```

In `deploy/aws-ecs/terraform/modules/scenario/locals.tf`, replace :542-544:

```hcl
    # Stamped into exports, library rows and audit metadata. The scenario is
    # the compartment for a disposable acceptance environment.
    { name = "ELSPETH_WEB__COMPARTMENT_ID", value = var.scenario_id },
```

with:

```hcl
    # Stamped into audit metadata, the YAML download, share snapshots and
    # library rows. The scenario is the compartment for a disposable
    # acceptance environment, lowercased because WebSettings refuses any
    # compartment_id outside ^[a-z0-9][a-z0-9-]{0,62}$.
    { name = "ELSPETH_WEB__COMPARTMENT_ID", value = local.scenario_id_lower },
```

- [ ] **Step 8: Run the config, deployment and readiness suites to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/test_config.py tests/unit/deployment/test_web_settings_exports_resolve.py tests/unit/deployment/test_aws_ecs_terraform_package.py tests/unit/web/test_readiness.py tests/unit/web/test_sso_wiring.py tests/unit/web/auth/test_idp_profiles.py tests/integration/web/test_sso_configured_bootstrap.py -n 0 > /tmp/i6-config.log 2>&1; echo exit=$?`
Expected: `exit=0`. `TestFieldValidatorCoverage` still passes for `compartment_id` (the blank refusal runs first).

- [ ] **Step 9: Write the failing audit-stamping tests and move the pins the stamp changes.**

In `tests/unit/web/auth/test_audit.py`, in `_settings` (:38-50), replace

```python
        get_session_db_url=lambda: session_url,
    )
```

with

```python
        get_session_db_url=lambda: session_url,
        compartment_id=None,
    )
```

In `test_identity_retirement_is_recorded_as_an_operator_disable_with_its_cause` (:366; its `assert_called_once_with(` is at :401), replace

```python
            "retired_subject": "ada#retired-identity-1",
            "reason": "local credential deleted",
        },
    )
```

with

```python
            "retired_subject": "ada#retired-identity-1",
            "reason": "local credential deleted",
            "compartment_id": None,
        },
    )
```

(`"reason": "local credential deleted",` occurs exactly once in the file on HEAD, so the anchor is unique).

Replace the logout pin at :895

```python
    assert _metadata(row) == {"method": "POST", "path": "/api/auth/login"}
```

with

```python
    assert _metadata(row) == {"method": "POST", "path": "/api/auth/login", "compartment_id": None}
```

In I5's `test_library_rows_anchor_on_the_publisher_and_carry_digest_and_compartment`, replace

```python
    assert (published["actor"], published["entry_id"], published["payload_digest"], published["compartment_id"]) == (
```

with

```python
    assert published["compartment_id"] is None, "the recorder has no compartment; the entry's own travels as entry_compartment_id"
    assert (published["actor"], published["entry_id"], published["payload_digest"], published["entry_compartment_id"]) == (
```

and replace

```python
    assert (accepted["actor"], accepted["payload_digest"], accepted["compartment_id"], accepted["note"]) == ("identity-carol", "a" * 64, "alpha", None)
```

with

```python
    assert (accepted["actor"], accepted["payload_digest"], accepted["entry_compartment_id"], accepted["note"]) == ("identity-carol", "a" * 64, "alpha", None)
```

In `tests/unit/web/auth/test_routes.py`, replace :1586

```python
        assert json.loads(event.metadata_json) == {"method": "POST", "path": "/api/auth/logout"}
```

with

```python
        assert json.loads(event.metadata_json) == {"method": "POST", "path": "/api/auth/logout", "compartment_id": None}
```

In `tests/unit/cli/test_web_command.py`, directly before `    def test_bootstrap_admin_is_refused_once_an_admin_exists(self, tmp_path: Path) -> None:` inside `class TestComposerUsersBootstrapAdmin`, insert:

```python
    def test_cli_written_rows_carry_the_compartment_from_the_service_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Task I6: the CLI stamps the marking the web service's own rows carry."""
        monkeypatch.delenv("ELSPETH_WEB__COMPARTMENT_ID", raising=False)
        unset = self._invoke(tmp_path / "unset")
        assert unset.exit_code == 0, unset.output
        monkeypatch.setenv("ELSPETH_WEB__COMPARTMENT_ID", "alpha")
        marked = self._invoke(tmp_path / "alpha")
        assert marked.exit_code == 0, marked.output
        unset_rows = _auth_event_rows(f"sqlite:///{tmp_path / 'unset' / 'runs' / 'audit.db'}")
        marked_rows = _auth_event_rows(f"sqlite:///{tmp_path / 'alpha' / 'runs' / 'audit.db'}")
        assert unset_rows and marked_rows
        assert {json.loads(row.metadata_json)["compartment_id"] for row in unset_rows} == {None}
        assert {json.loads(row.metadata_json)["compartment_id"] for row in marked_rows} == {"alpha"}

    def test_cli_refuses_a_malformed_compartment_before_writing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ELSPETH_WEB__COMPARTMENT_ID", "Alpha")
        result = self._invoke(tmp_path)
        assert result.exit_code == 1
        assert "ELSPETH_WEB__COMPARTMENT_ID must match" in result.output

```

`self._invoke` passes `--data-dir`, and the bootstrap writes its rows to `<data-dir>/runs/audit.db` (the path the existing happy-path test reads). `result.output` includes stderr: the existing `assert "already exists" in second.output` reads a message cli.py:2319 writes with `typer.echo` and `err=True`.

Append to the end of `tests/unit/web/auth/test_audit.py`:

```python


# ── Compartment marking on every row (Task I6) ────────────────────────────


def _recorder_factory_call_sites(source: str) -> list[str]:
    """The qualified enclosing scope of every ``RecorderFactory`` call in ``source``."""
    import ast

    sites: list[str] = []

    def visit(node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                visit(child, [*scope, child.name])
                continue
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "RecorderFactory":
                sites.append(".".join(scope))
            visit(child, scope)

    visit(ast.parse(source), [])
    return sites


def test_the_only_repository_handle_in_audit_py_is_the_stamping_proxy() -> None:
    """Every writer, including ones added after this pin, reaches the repository through the proxy."""
    from pathlib import Path

    source = Path(audit_module.__file__).read_text(encoding="utf-8")
    assert _recorder_factory_call_sites(source) == ["AuthAuditRecorder._auth_audit"]


def test_the_repository_handle_pin_sees_a_writer_that_bypasses_the_proxy() -> None:
    """Positive control for the pin above: a direct writer must be reported."""
    mutant = (
        "class AuthAuditRecorder:\n"
        "    def _auth_audit(self, db):\n"
        "        return _CompartmentStampedAuthAudit(RecorderFactory(db).auth_audit, compartment_id=None)\n"
        "    def record_new_event(self, db):\n"
        "        RecorderFactory(db).auth_audit.record_auth_event(metadata={})\n"
    )
    assert _recorder_factory_call_sites(mutant) == ["AuthAuditRecorder._auth_audit", "AuthAuditRecorder.record_new_event"]


@pytest.mark.parametrize("compartment_id", ["alpha", None])
def test_every_row_carries_the_container_marking(tmp_path: Any, compartment_id: str | None) -> None:
    landscape_url = f"sqlite:///{tmp_path / 'audit.db'}"
    recorder = AuthAuditRecorder(
        landscape_url=landscape_url,
        landscape_passphrase=None,
        create_tables=True,
        compartment_id=compartment_id,
    )
    recorder.record_logout(_request(), provider="entra", identity_id="identity-1", username="ada")
    recorder.record_login_success(_request(), provider="local", user_id="ada", username="ada")
    recorder.record_auth_failure(
        _request(),
        provider="oidc",
        failure_category="invalid_token",
        failure_stage="authenticate",
        user_id=None,
        username=None,
        exception_class="AuthenticationError",
    )
    rows = _durable_rows(landscape_url)
    assert sorted(row.event_type for row in rows) == ["auth_failure", "login", "logout"]
    assert all("compartment_id" in _metadata(row) for row in rows), "the key is always present, None included"
    assert {_metadata(row)["compartment_id"] for row in rows} == {compartment_id}


def test_from_settings_carries_the_configured_compartment() -> None:
    settings = _settings("default", "sqlite-single")
    assert AuthAuditRecorder.from_settings(settings).compartment_id is None
    settings.compartment_id = "alpha"
    assert AuthAuditRecorder.from_settings(settings).compartment_id == "alpha"


def test_a_caller_supplied_compartment_key_is_refused_before_any_write() -> None:
    from elspeth.contracts.errors import AuditIntegrityError

    repository = create_autospec(AuthAuditRepository, instance=True)
    proxy = audit_module._CompartmentStampedAuthAudit(repository, compartment_id="alpha")
    row: dict[str, Any] = {
        "event_type": "logout",
        "outcome": "success",
        "provider": "local",
        "user_id": None,
        "username": None,
        "failure_category": None,
        "request_id": None,
        "client_host": None,
        "user_agent": None,
    }
    with pytest.raises(AuditIntegrityError, match="must not carry compartment_id"):
        proxy.record_auth_event(**row, metadata={"compartment_id": "beta"})
    repository.record_auth_event.assert_not_called()
    proxy.record_auth_event(**row, metadata={"method": "POST"})
    assert repository.record_auth_event.call_args.kwargs["metadata"] == {"method": "POST", "compartment_id": "alpha"}
```

- [ ] **Step 10: Run the audit tests to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_routes.py::TestLogout tests/unit/cli/test_web_command.py::TestComposerUsersBootstrapAdmin -n 0 > /tmp/i6-audit-red.log 2>&1; echo exit=$?` (`class TestLogout` is at `tests/unit/web/auth/test_routes.py:1556` and encloses the :1586 pin)

Expected: `exit=1` with these failures:
- `test_every_row_carries_the_container_marking[alpha]` and `[None]`: `TypeError: AuthAuditRecorder.__init__() got an unexpected keyword argument 'compartment_id'`
- `test_from_settings_carries_the_configured_compartment`: `AttributeError: 'AuthAuditRecorder' object has no attribute 'compartment_id'`
- `test_a_caller_supplied_compartment_key_is_refused_before_any_write`: `AttributeError: module 'elspeth.web.auth.audit' has no attribute '_CompartmentStampedAuthAudit'`
- `test_the_only_repository_handle_in_audit_py_is_the_stamping_proxy`: `AssertionError` whose left side lists every `AuthAuditRecorder.record_*` writer (16 on HEAD, measured with this helper against `src/elspeth/web/auth/audit.py`, plus the writers I1, I3, I4 and I5 added)
- `test_logout_writes_a_request_bound_row` and `test_logout_records_a_durable_logout_row_and_answers_204`: `AssertionError` (the right side has `'compartment_id': None`, the left side lacks it)
- `test_identity_retirement_is_recorded_as_an_operator_disable_with_its_cause`: `AssertionError: expected call not found.`
- `test_library_rows_anchor_on_the_publisher_and_carry_digest_and_compartment`: `AssertionError: the recorder has no compartment; the entry's own travels as entry_compartment_id` (I5 writes `"compartment_id": "alpha"` into that row today)
- `test_cli_written_rows_carry_the_compartment_from_the_service_environment`: `KeyError: 'compartment_id'`
- `test_cli_refuses_a_malformed_compartment_before_writing`: `AssertionError` on `result.exit_code == 1` (the CLI exits 0 today)

`test_the_repository_handle_pin_sees_a_writer_that_bypasses_the_proxy` PASSES here: it proves the instrument reports a bypass before the production pin relies on it. A failure in any test not listed above is an exact-metadata pin this block did not measure (I1, I3 and I4 add tests to this file); add its edit to Step 9 before continuing.

- [ ] **Step 11: Stamp every row through one proxy.**

All edits in `src/elspeth/web/auth/audit.py`, in this order (the order matters: the token replacement must run before `_auth_audit` exists, or it would rewrite that method into a self-call).

1. Measure the token before replacing it:

Run: `cd "$(git rev-parse --show-toplevel)" && grep -c "RecorderFactory(db).auth_audit" src/elspeth/web/auth/audit.py > /tmp/i6-token-before.log 2>&1; echo exit=$?; cat /tmp/i6-token-before.log`
Expected: `exit=0` and a count of at least 16 (16 on HEAD plus the writers I1, I3, I4 and I5 added).

2. With the Edit tool and `replace_all: true`, replace every `RecorderFactory(db).auth_audit` with `self._auth_audit(db)`.

Run: `cd "$(git rev-parse --show-toplevel)" && grep -c "RecorderFactory(db).auth_audit" src/elspeth/web/auth/audit.py > /tmp/i6-token-after.log 2>&1; echo exit=$?; cat /tmp/i6-token-after.log`
Expected: `exit=1` and `0` (grep exits 1 on zero matches; the before-count in item 1 is the positive control that this pattern matches the file).

3. Replace `from collections.abc import Iterator` (:5) with `from collections.abc import Iterator, Mapping` (if an earlier task already added `Mapping` to that line, leave it). Directly before `from elspeth.core.landscape.database import LandscapeDB, SchemaCompatibilityError`, add:

```python
from elspeth.core.landscape.auth_audit_repository import AuthAuditEventType, AuthAuditOutcome, AuthAuditRepository
```

4. Directly before the `@dataclass` line of `class AuthAuditRecorder:` (:430-431), insert:

```python
class _CompartmentStampedAuthAudit:
    """The only path from this module to ``AuthAuditRepository``: every row carries the marking.

    Spec (sso-design.md §Guiding principle — compartments): the container's
    ``compartment_id`` is stamped into every ``auth_events.metadata_json``.
    Stamping once, here, rather than in each ``record_*`` method is what makes
    "every" hold for writers added later; ``test_audit.py`` pins that this
    module makes exactly one ``RecorderFactory`` call, the one in
    ``AuthAuditRecorder._auth_audit`` that builds this proxy.

    The key is ALWAYS present, ``None`` on a deployment with no compartment,
    for the reason ``_admin_provenance`` gives for its console keys: a reader
    must never have to guess whether an absent key meant "unmarked" or
    "written before the marking existed". A caller that supplies its own
    ``compartment_id`` is refused, because the container's marking is not the
    caller's to state; a library row's own compartment travels as
    ``entry_compartment_id``.
    """

    __slots__ = ("_compartment_id", "_repository")

    def __init__(self, repository: AuthAuditRepository, *, compartment_id: str | None) -> None:
        self._repository = repository
        self._compartment_id = compartment_id

    def _stamped(self, metadata: Mapping[str, object]) -> dict[str, object]:
        if "compartment_id" in metadata:
            raise AuditIntegrityError(
                "auth audit metadata must not carry compartment_id: AuthAuditRecorder stamps the container's marking on every row"
            )
        return {**metadata, "compartment_id": self._compartment_id}

    def record_auth_event(
        self,
        *,
        event_type: AuthAuditEventType,
        outcome: AuthAuditOutcome,
        provider: AuthProviderType,
        user_id: str | None,
        username: str | None,
        failure_category: str | None,
        request_id: str | None,
        client_host: str | None,
        user_agent: str | None,
        metadata: Mapping[str, object],
        identity_id: str | None = None,
    ) -> str:
        return self._repository.record_auth_event(
            event_type=event_type,
            outcome=outcome,
            provider=provider,
            user_id=user_id,
            username=username,
            failure_category=failure_category,
            request_id=request_id,
            client_host=client_host,
            user_agent=user_agent,
            metadata=self._stamped(metadata),
            identity_id=identity_id,
        )

    def record_login_success_and_token_issued(
        self,
        *,
        provider: AuthProviderType,
        user_id: str,
        username: str,
        request_id: str | None,
        client_host: str | None,
        user_agent: str | None,
        login_metadata: Mapping[str, object],
        token_metadata: Mapping[str, object],
        identity_id: str | None = None,
    ) -> tuple[str, str]:
        return self._repository.record_login_success_and_token_issued(
            provider=provider,
            user_id=user_id,
            username=username,
            request_id=request_id,
            client_host=client_host,
            user_agent=user_agent,
            login_metadata=self._stamped(login_metadata),
            token_metadata=self._stamped(token_metadata),
            identity_id=identity_id,
        )

    def record_login_outcome(
        self,
        *,
        outcome: AuthAuditOutcome,
        provider: AuthProviderType,
        user_id: str | None,
        username: str | None,
        failure_category: str | None,
        request_id: str | None,
        client_host: str | None,
        user_agent: str | None,
        metadata: Mapping[str, object],
        identity_id: str | None = None,
    ) -> str:
        return self._repository.record_login_outcome(
            outcome=outcome,
            provider=provider,
            user_id=user_id,
            username=username,
            failure_category=failure_category,
            request_id=request_id,
            client_host=client_host,
            user_agent=user_agent,
            metadata=self._stamped(metadata),
            identity_id=identity_id,
        )

    def record_token_issued(
        self,
        *,
        provider: AuthProviderType,
        user_id: str,
        username: str,
        request_id: str | None,
        client_host: str | None,
        user_agent: str | None,
        metadata: Mapping[str, object],
        identity_id: str | None = None,
    ) -> str:
        return self._repository.record_token_issued(
            provider=provider,
            user_id=user_id,
            username=username,
            request_id=request_id,
            client_host=client_host,
            user_agent=user_agent,
            metadata=self._stamped(metadata),
            identity_id=identity_id,
        )

    def record_auth_failure(
        self,
        *,
        provider: AuthProviderType,
        user_id: str | None,
        username: str | None,
        failure_category: str,
        request_id: str | None,
        client_host: str | None,
        user_agent: str | None,
        metadata: Mapping[str, object],
        identity_id: str | None = None,
    ) -> str:
        return self._repository.record_auth_failure(
            provider=provider,
            user_id=user_id,
            username=username,
            failure_category=failure_category,
            request_id=request_id,
            client_host=client_host,
            user_agent=user_agent,
            metadata=self._stamped(metadata),
            identity_id=identity_id,
        )


```

5. In the dataclass fields, replace

```python
    create_tables: bool
    _db: LandscapeDB | None = field(default=None, init=False, repr=False)
```

with

```python
    create_tables: bool
    # The container's compartment marking, stamped into every row's
    # metadata_json by ``_CompartmentStampedAuthAudit``. ``from_settings``
    # passes ``settings.compartment_id``; the composer CLI recorder
    # (cli.py ``_composer_auth_audit_recorder``) passes the service's
    # ``ELSPETH_WEB__COMPARTMENT_ID``. Direct construction without it (tests,
    # tools) stamps ``None``, which the always-present key makes visible.
    compartment_id: str | None = None
    _db: LandscapeDB | None = field(default=None, init=False, repr=False)
```

6. In `from_settings`, replace

```python
            landscape_passphrase=settings.landscape_passphrase,
            create_tables=state_mode == "sqlite-single",
        )
```

with

```python
            landscape_passphrase=settings.landscape_passphrase,
            create_tables=state_mode == "sqlite-single",
            compartment_id=settings.compartment_id,
        )
```

7. Replace the tail of `_open_landscape` and the head of the recorder's `record_login_success`

```python
                exception_class=type(exc).__name__,
            )
            raise

    def record_login_success(
```

with

```python
                exception_class=type(exc).__name__,
            )
            raise

    def _auth_audit(self, db: LandscapeDB) -> _CompartmentStampedAuthAudit:
        """The stamping proxy over this Landscape's auth-audit repository; this module's only ``RecorderFactory`` call."""
        return _CompartmentStampedAuthAudit(RecorderFactory(db).auth_audit, compartment_id=self.compartment_id)

    def record_login_success(
```

8. I5's library writers: with the Edit tool and `replace_all: true`, replace `                    "compartment_id": compartment_id,` (twenty spaces of indent, inside `record_library_published` and `_record_library_curation`) with `                    "entry_compartment_id": compartment_id,`.

Run: `cd "$(git rev-parse --show-toplevel)" && grep -c '"entry_compartment_id": compartment_id,' src/elspeth/web/auth/audit.py > /tmp/i6-entry-key.log 2>&1; echo exit=$?; cat /tmp/i6-entry-key.log; grep -c '"compartment_id": compartment_id,' src/elspeth/web/auth/audit.py >> /tmp/i6-entry-key.log 2>&1; echo exit=$?; cat /tmp/i6-entry-key.log`
Expected: first `exit=0` with `2`; second `exit=1` with a trailing `0`.

9. In `src/elspeth/cli.py`, in `_composer_auth_audit_recorder` (:1977-1997), replace

```python
    from elspeth.web.auth.audit import AuthAuditRecorder

    parsed = make_url(landscape_url)
```

with

```python
    from elspeth.web.auth.audit import AuthAuditRecorder
    from elspeth.web.composer.compartment_marking import COMPARTMENT_ID_PATTERN, is_compartment_id

    # The web service's own environment name for the marking, checked by the
    # authority WebSettings uses, so a CLI-written auth row carries the
    # compartment the service's rows carry (Task I6).
    compartment_id = os.environ.get("ELSPETH_WEB__COMPARTMENT_ID")
    if compartment_id is not None and not is_compartment_id(compartment_id):
        typer.echo(f"Error: ELSPETH_WEB__COMPARTMENT_ID must match ^{COMPARTMENT_ID_PATTERN}$", err=True)
        raise typer.Exit(1)
    parsed = make_url(landscape_url)
```

and replace

```python
        create_tables=sqlite_landscape,
    )
```

(the end of that function's `return AuthAuditRecorder(` call) with

```python
        create_tables=sqlite_landscape,
        compartment_id=compartment_id,
    )
```

10. In `src/elspeth/web/sessions/models.py`, replace :3281-3282

```python
# snapshot is NOT WRITTEN (measured 2026-09-07): the SSO login row's
# ``metadata_json`` holds exactly ``{"method", "path"}`` -- ``routes.py``'s
```

with

```python
# snapshot is NOT WRITTEN (measured 2026-09-07): the SSO login row's
# ``metadata_json`` holds exactly ``{"method", "path", "compartment_id"}``, the
# last stamped on every row by ``AuthAuditRecorder`` (Task I6) -- ``routes.py``'s
```

- [ ] **Step 12: Run the auth suites to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/auth tests/unit/web/test_sso_wiring.py tests/unit/web/test_local_auth_wiring.py tests/unit/cli/test_web_command.py tests/integration/web/test_sso_configured_bootstrap.py -n 0 > /tmp/i6-audit.log 2>&1; echo exit=$?`
Expected: `exit=0`.

Mutation control for the stamp: temporarily change `return {**metadata, "compartment_id": self._compartment_id}` to `return dict(metadata)` with the Edit tool.

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/auth/test_audit.py -k "compartment or logout_writes" -n 0 > /tmp/i6-audit-mutant.log 2>&1; echo exit=$?`
Expected: `exit=1`; at least `test_every_row_carries_the_container_marking[alpha]` and `[None]` fail with `AssertionError: the key is always present, None included`, and `test_a_caller_supplied_compartment_key_is_refused_before_any_write` and `test_logout_writes_a_request_bound_row` fail on their final equality. Restore the line with the Edit tool and re-run the same command: `exit=0`.

- [ ] **Step 13: Write the failing share-snapshot test.**

In `tests/unit/web/shareable_reviews/test_service.py`, replace :226-228

```python
@dataclass(slots=True)
class _FakeSettings:
    shareable_link_lifetime_seconds: int = 30 * 24 * 3600
```

with

```python
@dataclass(slots=True)
class _FakeSettings:
    shareable_link_lifetime_seconds: int = 30 * 24 * 3600
    compartment_id: str | None = None
```

In `_build_service` (:495-508), replace

```python
    readiness: AuditReadinessSnapshot,
) -> tuple[ShareableReviewService, _FakeSessionService, _FakeExecutionService, _FakeReadinessService]:
```

with

```python
    readiness: AuditReadinessSnapshot,
    compartment_id: str | None = None,
) -> tuple[ShareableReviewService, _FakeSessionService, _FakeExecutionService, _FakeReadinessService]:
```

and replace `    settings = _FakeSettings()` with `    settings = _FakeSettings(compartment_id=compartment_id)`.

Append to the end of the file:

```python


@pytest.mark.asyncio
@pytest.mark.parametrize("compartment_id", ["alpha", None])
async def test_mark_ready_for_review_stamps_the_container_compartment_into_the_blob(
    session_engine_with_row,
    payload_store,
    signer,
    session_record,
    state_record,
    session_operation_context: SessionOperationContext,
    compartment_id: str | None,
) -> None:
    """Task I6: the snapshot is one of the five stamped surfaces (spec §Guiding principle — compartments)."""
    service, *_ = _build_service(
        engine=session_engine_with_row,
        payload_store=payload_store,
        signer=signer,
        session_record=session_record,
        state_record=state_record,
        validation=_ok_validation(),
        readiness=_readiness_snapshot(session_record.id),
        compartment_id=compartment_id,
    )
    response = await service.mark_ready_for_review(
        session_id=session_record.id,
        user_id=session_record.user_id,
        username=_OWNER_USERNAME,
        session_operation_context=session_operation_context,
    )
    stored = json.loads(payload_store.retrieve(response.payload_digest.removeprefix("sha256:")))
    assert "compartment_id" in stored, "the key is always emitted, None included"
    assert stored["compartment_id"] == compartment_id
```

- [ ] **Step 14: Run it to verify it fails.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/shareable_reviews/test_service.py::test_mark_ready_for_review_stamps_the_container_compartment_into_the_blob -n 0 > /tmp/i6-share-red.log 2>&1; echo exit=$?`
Expected: `exit=1`; both parameters fail with `AssertionError: the key is always emitted, None included`.

- [ ] **Step 15: Stamp the snapshot blob.**

In `src/elspeth/web/shareable_reviews/service.py`:

After `    created_by_username: NotRequired[str]` (:223, the last `_BlobShape` field) add:

```python
    # The container's compartment marking (spec §Guiding principle —
    # compartments), ``None`` on a deployment with none configured. Absent on
    # blobs minted before Task I6; ``resolve_token`` never reads it, so a
    # legacy blob resolves unchanged.
    compartment_id: NotRequired[str | None]
```

In `_BLOB_KEYS` (:235-245), replace

```python
        "created_by_user_id",
        "created_by_username",
    }
)
```

with

```python
        "created_by_user_id",
        "created_by_username",
        "compartment_id",
    }
)
```

In `_build_snapshot`, replace the signature tail (:280-282)

```python
    created_by_user_id: str,
    created_by_username: str,
) -> _Snapshot:
```

with

```python
    created_by_user_id: str,
    created_by_username: str,
    compartment_id: str | None,
) -> _Snapshot:
```

replace the docstring line (:287) `    audit_readiness, created_by_user_id, created_by_username, created_at.` with `    audit_readiness, created_by_user_id, created_by_username, compartment_id, created_at.`, and replace (:330-331)

```python
        "created_by_user_id": created_by_user_id,
        "created_by_username": created_by_username,
    }
```

with

```python
        "created_by_user_id": created_by_user_id,
        "created_by_username": created_by_username,
        "compartment_id": compartment_id,
    }
```

In `mark_ready_for_review`, replace (:496-502)

```python
        snapshot = _build_snapshot(
            session_id=session_id,
            state_record=state_record,
            audit_readiness=audit_readiness,
            created_by_user_id=user_id,
            created_by_username=username,
        )
```

with

```python
        snapshot = _build_snapshot(
            session_id=session_id,
            state_record=state_record,
            audit_readiness=audit_readiness,
            created_by_user_id=user_id,
            created_by_username=username,
            compartment_id=self._settings.compartment_id,
        )
```

- [ ] **Step 16: Run the shareable-review suites to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/shareable_reviews tests/integration/web/test_shareable_reviews_routes.py tests/integration/web/test_completion_flow_e2e.py -n 0 > /tmp/i6-share.log 2>&1; echo exit=$?`
Expected: `exit=0`. The signed share-blob reader requires the current closed key set; a blob missing `compartment_id` or `created_by_username` is rejected after signature and digest verification.

- [ ] **Step 17: Write the failing download and ingress tests.**

Create `tests/integration/web/test_compartment_marking.py`:

```python
"""Compartment marking on the YAML download (Task I6).

Spec: sso-design.md §Guiding principle — compartments (rev2.2, rev2.12). The
download is the one consumer of the public projection that hands a user a
document to keep, so the marking is its first line; every other consumer of
``generate_public_yaml`` keeps bare bytes.
"""

from __future__ import annotations

from uuid import UUID

import yaml
from fastapi.testclient import TestClient

from elspeth.web.composer.compartment_marking import COMPARTMENT_MARKING_PREFIX


def test_yaml_download_carries_the_marking_line_only_when_configured(
    audit_readiness_client_with_state: tuple[TestClient, UUID],
) -> None:
    client, session_id = audit_readiness_client_with_state
    unmarked = client.get(f"/api/sessions/{session_id}/state/yaml")
    assert unmarked.status_code == 200, unmarked.text
    unmarked_yaml = unmarked.json()["yaml"]
    assert COMPARTMENT_MARKING_PREFIX not in unmarked_yaml

    client.app.state.settings = client.app.state.settings.model_copy(update={"compartment_id": "alpha"})
    marked = client.get(f"/api/sessions/{session_id}/state/yaml")
    assert marked.status_code == 200, marked.text
    marked_yaml = marked.json()["yaml"]
    assert marked_yaml.startswith("# compartment_id: alpha\n")
    assert marked_yaml.removeprefix("# compartment_id: alpha\n") == unmarked_yaml
    assert yaml.safe_load(marked_yaml) == yaml.safe_load(unmarked_yaml), "the marking is a comment, not document content"
```

In `tests/unit/web/sessions/test_routes.py`, replace the first two stdlib imports

```python
import asyncio
import json
```

with

```python
import asyncio
import hashlib
import json
```

and append to the end of the file:

```python


class TestCompartmentIngressRecord:
    """Task I6: a state seeded from pasted YAML records the text digest and every foreign marking."""

    @staticmethod
    def _pipeline_yaml() -> str:
        return yaml.safe_dump(
            {
                "sources": {"source": {"plugin": "csv", "on_success": "main", "options": {}, "on_validation_failure": "discard"}},
                "sinks": {"main": {"plugin": "json", "options": {}, "on_write_failure": "discard"}},
            }
        )

    @staticmethod
    async def _import(tmp_path: Path, *, own_compartment_id: str | None, yaml_text: str) -> Any:
        tmp_path.mkdir(parents=True, exist_ok=True)
        app, service = _make_app(tmp_path)
        app.state.settings = app.state.settings.model_copy(update={"compartment_id": own_compartment_id})
        client = TestClient(app)
        session = await service.create_session("alice", "Compartment ingress", "local")
        response = client.post(f"/api/sessions/{session.id}/state/yaml", json={"yaml": yaml_text})
        assert response.status_code == 200, response.text
        record = await service.get_current_state(session.id)
        assert record is not None
        return record

    @staticmethod
    def _ingress(record: Any) -> Any:
        meta = deep_thaw(record.composer_meta) if record.composer_meta is not None else {}
        return meta.get("ingress")

    @pytest.mark.asyncio
    async def test_import_records_the_text_digest_and_the_foreign_marking(self, tmp_path: Path) -> None:
        yaml_text = "# compartment_id: other\n# compartment_id: home\n" + self._pipeline_yaml()
        record = await self._import(tmp_path, own_compartment_id="home", yaml_text=yaml_text)
        assert self._ingress(record) == {
            "text_sha256": hashlib.sha256(yaml_text.encode("utf-8")).hexdigest(),
            "foreign_compartment_ids": ["other"],
        }

    @pytest.mark.asyncio
    async def test_the_foreign_set_derives_from_the_configured_compartment(self, tmp_path: Path) -> None:
        yaml_text = "# compartment_id: other\n# compartment_id: home\n" + self._pipeline_yaml()
        as_other = await self._import(tmp_path / "other", own_compartment_id="other", yaml_text=yaml_text)
        unconfigured = await self._import(tmp_path / "unconfigured", own_compartment_id=None, yaml_text=yaml_text)
        assert self._ingress(as_other)["foreign_compartment_ids"] == ["home"]
        assert self._ingress(unconfigured)["foreign_compartment_ids"] == ["home", "other"]

    @pytest.mark.asyncio
    async def test_unmarked_text_still_records_its_digest(self, tmp_path: Path) -> None:
        yaml_text = self._pipeline_yaml()
        record = await self._import(tmp_path, own_compartment_id="home", yaml_text=yaml_text)
        assert self._ingress(record) == {
            "text_sha256": hashlib.sha256(yaml_text.encode("utf-8")).hexdigest(),
            "foreign_compartment_ids": [],
        }

    @pytest.mark.asyncio
    async def test_the_marking_is_recorded_and_never_authored(self, tmp_path: Path) -> None:
        """Composer invariant 1: a marking changes the ingress record and nothing the pipeline is made of."""
        bare_text = self._pipeline_yaml()
        marked_text = "# compartment_id: other\n" + bare_text
        bare = await self._import(tmp_path / "bare", own_compartment_id="home", yaml_text=bare_text)
        marked = await self._import(tmp_path / "marked", own_compartment_id="home", yaml_text=marked_text)
        assert (bare.sources, bare.nodes, bare.edges, bare.outputs, bare.is_valid) == (
            marked.sources,
            marked.nodes,
            marked.edges,
            marked.outputs,
            marked.is_valid,
        )
```

- [ ] **Step 18: Run them to verify they fail.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/integration/web/test_compartment_marking.py tests/unit/web/sessions/test_routes.py::TestCompartmentIngressRecord -n 0 > /tmp/i6-ingress-red.log 2>&1; echo exit=$?`
Expected: `exit=1` with:
- `test_yaml_download_carries_the_marking_line_only_when_configured`: `AssertionError` at `assert marked_yaml.startswith("# compartment_id: alpha\n")`
- `test_import_records_the_text_digest_and_the_foreign_marking` and `test_unmarked_text_still_records_its_digest`: `AssertionError: assert None == {` followed by pytest's repr of the expected record
- `test_the_foreign_set_derives_from_the_configured_compartment`: `TypeError: 'NoneType' object is not subscriptable`

`test_the_marking_is_recorded_and_never_authored` PASSES before and after the implementation: it is the invariant guard, and it must stay green.

- [ ] **Step 19: Write the marking line on download and the ingress record on seed.**

In `src/elspeth/web/sessions/routes/composer/state.py`, directly before `from elspeth.web.composer.guided.errors import InvariantError` (:19), add:

```python
from elspeth.web.composer.compartment_marking import compartment_ingress_record, compartment_marking_header
```

In `get_state_yaml`, replace (:1246)

```python
        yaml_str = public_export_redaction_header(export_state) + generate_public_yaml(export_state)
```

with

```python
        # Task I6: the compartment marking is the first line of the kept
        # document, a YAML comment the importer ignores and the seed path's
        # ingress record reads back (web/composer/compartment_marking.py). It
        # stays out of ``generate_public_yaml`` for the redaction header's
        # reason, and because the library's ``payload_digest`` must be one
        # string in every compartment for cross-container detection to work.
        yaml_str = (
            compartment_marking_header(request.app.state.settings.compartment_id)
            + public_export_redaction_header(export_state)
            + generate_public_yaml(export_state)
        )
```

In I5's `seed_state_from_runtime_yaml`, replace

```python
            if composer_meta_updates is not None:
                state_data = replace(
                    state_data,
                    composer_meta=merge_composer_meta_updates(state_data.composer_meta, composer_meta_updates),
                )
```

with

```python
            # Task I6 (spec §Guiding principle — compartments): a state created
            # from pasted text records the sha256 of that text and every foreign
            # compartment marking in it. Computed HERE, after every validator,
            # so a refused import records nothing and neither seed path (the
            # paste import, the library fork) can skip it. Recording only: the
            # imported structure above is untouched (composer invariant 1).
            # ``merge_composer_meta_updates`` carries the key forward, so each
            # descendant state names the text its lineage was seeded from until
            # the next seed replaces it.
            ingress = compartment_ingress_record(body.yaml, own_compartment_id=request.app.state.settings.compartment_id)
            seed_meta_updates = {"ingress": ingress} if composer_meta_updates is None else {**composer_meta_updates, "ingress": ingress}
            state_data = replace(
                state_data,
                composer_meta=merge_composer_meta_updates(state_data.composer_meta, seed_meta_updates),
            )
```

In I5's `tests/integration/web/workflow/test_library.py`, replace

```python
    assert set(seeded.sources or {}) == {"source"}
```

with

```python
    assert set(seeded.sources or {}) == {"source"}
    fork_ingress = seeded.composer_meta["ingress"]
    assert fork_ingress["text_sha256"] == entry["payload_digest"], "the fork seeds from the entry's exact projection bytes"
    assert len(fork_ingress["foreign_compartment_ids"]) == 0
```

- [ ] **Step 20: Run the download, ingress, import and library suites to verify they pass.**

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/integration/web/test_compartment_marking.py tests/unit/web/sessions/test_routes.py::TestCompartmentIngressRecord tests/unit/web/sessions/test_routes.py::TestYamlEndpoint tests/integration/web/test_yaml_export_audit_event.py tests/unit/web/composer/test_yaml_importer.py tests/unit/web/composer/test_yaml_generator.py tests/unit/web/aws_ecs_acceptance/test_http_capture.py tests/integration/web/workflow/test_library.py -n 0 > /tmp/i6-ingress.log 2>&1; echo exit=$?`
Expected: `exit=0`. In `test_library.py` the fork test's `published_meta["compartment_id"] == "alpha"` assertion now holds through the central stamp (that app's recorder is built by `from_settings` over settings with `compartment_id="alpha"`).

Mutation control for the ingress derivation: temporarily change `own_compartment_id=request.app.state.settings.compartment_id` in `seed_state_from_runtime_yaml` to `own_compartment_id=None` with the Edit tool.

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/sessions/test_routes.py::TestCompartmentIngressRecord -n 0 > /tmp/i6-ingress-mutant.log 2>&1; echo exit=$?`
Expected: `exit=1`; `test_import_records_the_text_digest_and_the_foreign_marking` fails with `['home', 'other']` on the left and `['other']` on the right. Restore the line and re-run: `exit=0`.

- [ ] **Step 21: Pin that the library projection carries no marking.**

Append to I5's `tests/unit/web/coordination/test_library_authority.py`:

```python


def test_the_published_projection_is_compartment_independent(engine, tmp_path) -> None:
    """Task I6: the marking lives on the row and in audit metadata, never in the projection bytes.

    ``payload_digest`` is how the same artifact in two containers is detected
    (spec §Workflow tables, ``library_published`` rows); a marking inside the
    bytes would give every compartment a different digest for one pipeline.
    """
    authority = _authority(engine, tmp_path)
    alpha = _publish(authority, compartment_id="alpha")
    beta = _publish(authority, compartment_id="beta")
    assert (alpha.compartment_id, beta.compartment_id) == ("alpha", "beta")
    assert alpha.payload_digest == beta.payload_digest == public_projection_digest(_state())
    assert b"compartment_id" not in authority._payload_store.retrieve(alpha.payload_digest)
```

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/coordination/test_library_authority.py::test_the_published_projection_is_compartment_independent -n 0 > /tmp/i6-library.log 2>&1; echo exit=$?`
Expected: `exit=0` (I5 already keeps the marking out of the bytes; this pins it).

Mutation control: in `src/elspeth/web/coordination/library_authority.py`, temporarily replace `        payload_yaml = generate_public_yaml(export_state)` with `        payload_yaml = compartment_marking_header(compartment_id) + generate_public_yaml(export_state)` and add `from elspeth.web.composer.compartment_marking import compartment_marking_header` to its imports, with the Edit tool.

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/web/coordination/test_library_authority.py::test_the_published_projection_is_compartment_independent -n 0 > /tmp/i6-library-mutant.log 2>&1; echo exit=$?`
Expected: `exit=1` with `elspeth.contracts.errors.AuditIntegrityError: payload store address differs from the projection digest`. Revert both edits with the Edit tool, then confirm the file is back to I5's bytes:

Run: `cd "$(git rev-parse --show-toplevel)" && git diff --exit-code -- src/elspeth/web/coordination/library_authority.py > /tmp/i6-library-revert.log 2>&1; echo exit=$?`
Expected: `exit=0` if I5 is committed on this branch (no diff); if I5's file is still uncommitted in this worktree, compare instead with `grep -c "compartment_marking" src/elspeth/web/coordination/library_authority.py` and expect `0` with `exit=1`.

- [ ] **Step 22: Run the mutation-authority gate and re-pin the one line that moved.**

This task adds no sessions writer. The only manifest identity in an edited file is the read connection `ShareableReviewService._latest_mark_ready_event` (`line=685`), which Step 15 moved down by nine lines without touching its body. `WriterIdentity` (test_session_db_mutation_authority.py:49-58) is a dataclass whose equality includes `line`, and the gate reports drift through `pytest.xfail` (:18351), so drift shows as an XFAIL with exit 0, never as a failure: read the reason, not the exit code.

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/architecture/test_session_db_mutation_authority.py::test_all_production_sessions_writers_are_reviewed_typed_authorities -n 0 -rxX > /tmp/i6-manifest-red.log 2>&1; echo exit=$?`
Expected: `exit=0` and `1 xfailed`. The gate already XFAILs on a clean HEAD, and after I4 it also carries the lines I4 Step 9 leaves, so the exit code and the summary word are the same before and after the re-pin: read the reason. The XFAIL reason printed by `-rxX` begins `Sessions mutation authority inventory drift.`; under `Stale reviewed read connections (1):` it names `src/elspeth/web/shareable_reviews/service.py:685 ShareableReviewService._latest_mark_ready_event write_connection <sessions-write-connection> fp=cdb73b156f37ffa7#1`, and the same symbol with the same fingerprint appears at `service.py:694` as a live site under both `Unexpected/unreviewed` and `Connections outside exact contained authority` (`grep -c "_latest_mark_ready_event" /tmp/i6-manifest-red.log` prints `3`). No other identity may appear in any section beyond I1 Step 15's recorded baseline and the two `RepositoryReviewAuthority._lock_then_read_clock unknown_execute` lines I4 Step 9 leaves: every section count equals I4 Step 9's green-run count except `Unexpected/unreviewed` and `Connections outside exact contained authority`, which are each 1 above it, and `Stale reviewed read connections`, which reads `(1)` instead of `(0)`. A different fingerprint means the method body changed (Step 15 applied wrongly); any other identity means an edit landed outside this task: fix the code rather than re-pin it.

In `tests/unit/architecture/test_session_db_mutation_authority.py` (:4645-4654), replace

```python
        "ShareableReviewService._latest_mark_ready_event",
        "<sessions-write-connection>",
        "write_connection",
        "cdb73b156f37ffa7",
        1,
        None,
        line=685,
```

with the same row carrying the line the gate printed (694 when Step 15 is applied exactly as written):

```python
        "ShareableReviewService._latest_mark_ready_event",
        "<sessions-write-connection>",
        "write_connection",
        "cdb73b156f37ffa7",
        1,
        None,
        line=694,
```

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/architecture/test_session_db_mutation_authority.py tests/unit/web/test_sessions_composer_attribute_contracts.py -n 0 -rxX > /tmp/i6-manifest.log 2>&1; echo exit=$?; grep -c "_latest_mark_ready_event" /tmp/i6-manifest.log; grep -c "Stale reviewed read connections (0):" /tmp/i6-manifest.log; grep -c "Stale reviewed read connections (1):" /tmp/i6-manifest-red.log`
Expected: `exit=0`, no `failed`, and exactly `1 xfailed`, `test_all_production_sessions_writers_are_reviewed_typed_authorities`: the gate already XFAILs on a clean HEAD (measured 2026-09-15 on a `git archive` export of 46219b2b7, where these two files gave `1 xfailed in 207.66s` with I1 Step 15's baseline counts; `test_sessions_composer_attribute_contracts.py` has no xfail), and I4 Step 9 leaves two lines in it, so this task cannot bring it to a pass. Read the XFAIL text instead: its counts equal I1 Step 15's recorded baseline except `Unexpected/unreviewed` and `Unresolved write executions`, which are each 1 above it (the two `RepositoryReviewAuthority._lock_then_read_clock unknown_execute` lines I4 Step 9 leaves). The three counts are `0` (no section names the re-pinned row), `1` (the stale read connection section is empty) and `1` (the positive control: the same grep matches the red log, so the first two counts do not come from a grep that matches nothing). A nonzero first count or a `0` second count means the `line=` value is not the one the gate printed: copy it again from `/tmp/i6-manifest-red.log`.

- [ ] **Step 23: Re-pin the soft-mapping census and run the PostgreSQL proofs.**

The proxy's `Mapping[str, object]` parameters and `dict[str, object]` return are new soft sites in `src/elspeth/web/auth/audit.py`.

Run: `cd "$(git rev-parse --show-toplevel)" && python -m scripts.check_contracts > /tmp/i6-census-red.log 2>&1; echo exit=$?`
Expected: `exit=1`; the drift names `src/elspeth/web/auth/audit.py` and no other file: `Mapping[str, object]` rises by 7 (the proxy's six `metadata` parameters and `_stamped`'s parameter) and `dict[str, object]` by 1 (`_stamped`'s return), so the message reads `soft total pinned <N> -> live <N+8> (REGRESSION)`. That rise is accepted in this task (the proxy mirrors `AuthAuditRepository`'s own `Mapping[str, object]` signatures, which the census counts as soft at auth_audit_repository.py) and is raised as an open question; no alias or retyping is used to hide it.

Run: `cd "$(git rev-parse --show-toplevel)" && python -m scripts.check_contracts --write-census > /tmp/i6-census-write.log 2>&1; echo exit=$?`
Expected: `exit=0`; `git diff -- config/cicd/soft-mapping-census.yaml` changes only the `src/elspeth/web/auth/audit.py` rows and the totals.

Run: `cd "$(git rev-parse --show-toplevel)" && python -m scripts.check_contracts > /tmp/i6-census.log 2>&1; echo exit=$?; pytest tests/unit/scripts/test_check_contracts.py -n 0 >> /tmp/i6-census.log 2>&1; echo exit=$?`
Expected: `exit=0` twice.

This task changes what every Landscape `auth_events` write carries and what sessions `composition_states.composer_meta` holds, so it runs the PostgreSQL proofs (F7). `test_landscape_write_gate_postgres.py` writes through `AuthAuditRecorder.from_settings(settings).record_auth_failure(` (:201), which is the stamped path.

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/testcontainer/web/test_landscape_write_gate_postgres.py tests/testcontainer/core/test_auth_event_export_snapshot_postgres.py tests/testcontainer/web/test_library_postgres.py -m testcontainer -n 0 > /tmp/i6-pg.log 2>&1; echo exit=$?`
Expected: `exit=0` (Docker required; without `-m testcontainer` the selection is empty and pytest exits 5).

- [ ] **Step 24: Update the docs and the changelog.**

`docs/reference/configuration.md`: replace the `compartment_id` row (:387 on HEAD, as Task I8 rewrote it)

```markdown
| `compartment_id` | string | Yes (every IdP) | - | Operator-declared marking for this container's identities and artifacts. Validated non-blank. Required by readiness whenever `workflow_governance` is `on` (library rows and audit metadata carry the marking); otherwise no runtime path reads it in this release |
```

with

```markdown
| `compartment_id` | string | Yes (every IdP) | - | Operator-declared marking for this container's identities and artifacts. Lowercase letters, digits and hyphens, 1 to 63 characters, not starting with a hyphen (`^[a-z0-9][a-z0-9-]{0,62}$`); validated non-blank. Stamped into every `auth_events.metadata_json`, the first line of the YAML download (`# compartment_id: <id>`), shareable-review snapshots, library entries and every signed or unsigned Landscape export. YAML import and chat-pasted state creation record foreign markings. Required by readiness whenever `workflow_governance` is `on` |
```

`docs/guides/identity-providers.md`: replace :507-514

```markdown
Its purpose is to make the same artifact appearing in two deployments
detectable later. **In this release the setting is validated and stored but
not yet consumed:** the `library_entries` table carries a non-null
`compartment_id` column and an index for it, and no runtime path writes or
reads one. The stamping of the marking into published library rows, exported
YAML, audit metadata and signed exports is the work this column is waiting
for. It is required now so that the value is fixed before anything starts
depending on it, and so no deployment has to be reconfigured when it does.
```

with

```markdown
Its purpose is to make the same artifact appearing in two deployments
detectable later. The value is lowercase letters, digits and hyphens, 1 to 63
characters, not starting with a hyphen (`^[a-z0-9][a-z0-9-]{0,62}$`): it is
written verbatim as a YAML comment line and read back out of pasted text, so
it cannot carry whitespace or `#`. ELSPETH stamps it into:

- every authentication audit row (`auth_events.metadata_json` carries
  `compartment_id`, `null` when none is configured);
- the first line of a YAML download, `# compartment_id: <id>`;
- the shareable-review snapshot;
- every published library entry (the `library_entries.compartment_id`
  column; the entry's `payload_digest` is deliberately the same in every
  compartment, which is what makes the same pipeline detectable across them);
- every signed or unsigned Landscape export through its auth-v2 public
  configuration record.

Importing pasted YAML or creating a composition state from user text pasted
into Composer chat records the sha256 of the text and every `compartment_id`
marking in it that is not this deployment's own.
```

`CHANGELOG.md`: directly before the line `- **Coordination deadlines are decided from fresh post-lock database time.**` (:40 on HEAD; below any bullets I0, I8 and I1 to I5 added under `## 0.8.1 - 2026-09-10`), insert

```markdown
- **Compartment marking.** `ELSPETH_WEB__COMPARTMENT_ID` must match
  `^[a-z0-9][a-z0-9-]{0,62}$`; the AWS acceptance scenarios now export the
  lowercase scenario letter. The marking is stamped into every
  authentication audit row, the first line of the YAML download, the
  shareable-review snapshot and library entries, and a pasted YAML import
  records the text's sha256 and any foreign marking on the new composition
  state. User text pasted into Composer chat records the same ingress when
  it creates a composition state. Signed and unsigned Landscape exports
  carry the marking in their auth-v2 public configuration record.

```

Run: `cd "$(git rev-parse --show-toplevel)" && pytest tests/unit/docs/test_release_version_surfaces.py tests/unit/docs/test_deployment_platform_docs.py tests/unit/docs/test_changelog_release_links.py tests/unit/website/test_release_site_contract.py tests/unit/deployment/test_web_settings_exports_resolve.py -n 0 > /tmp/i6-docs.log 2>&1; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 25: Lint and type-check the touched files.**

Run: `cd "$(git rev-parse --show-toplevel)" && ruff check src/elspeth/cli.py src/elspeth/web/composer/compartment_marking.py src/elspeth/web/config.py src/elspeth/web/auth/audit.py src/elspeth/web/sessions/models.py src/elspeth/web/shareable_reviews/service.py src/elspeth/web/sessions/routes/composer/state.py tests/unit/web/composer/test_compartment_marking.py tests/unit/web/test_config.py tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_routes.py tests/unit/cli/test_web_command.py tests/unit/web/shareable_reviews/test_service.py tests/unit/web/sessions/test_routes.py tests/unit/web/coordination/test_library_authority.py tests/integration/web/workflow/test_library.py tests/integration/web/test_compartment_marking.py tests/unit/deployment/test_web_settings_exports_resolve.py tests/unit/architecture/test_session_db_mutation_authority.py > /tmp/i6-ruff.log 2>&1; echo exit=$?`
Expected: `exit=0`. Then the same file list with `ruff format --check` in place of `ruff check`, writing `/tmp/i6-ruff-format.log`: `exit=0`.

Run: `cd "$(git rev-parse --show-toplevel)" && mypy src/elspeth/cli.py src/elspeth/web/composer/compartment_marking.py src/elspeth/web/config.py src/elspeth/web/auth/audit.py src/elspeth/web/shareable_reviews/service.py src/elspeth/web/sessions/routes/composer/state.py > /tmp/i6-mypy.log 2>&1; echo exit=$?`
Expected: `exit=0`.

- [ ] **Step 26: Compare the trust-tier corpus and run the full-suite gate.**

Run: `cd "$(git rev-parse --show-toplevel)" && ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth > /tmp/i6-lints-after.log 2>&1; echo exit=$?; diff /tmp/i6-lints-before.log /tmp/i6-lints-after.log > /tmp/i6-lints-diff.log 2>&1; echo diff_exit=$?`
Expected: `exit=1` (the standing fail-closed corpus). Read `/tmp/i6-lints-diff.log`: a line added for `compartment_marking.py`, `config.py`, `audit.py`, `shareable_reviews/service.py` or `routes/composer/state.py` that is not a pure line-number move of a finding present before is a new defect this task introduced; fix the code (never add a suppression) and re-run until none remain.

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/full-suite-gate.sh --execute --detach > /tmp/i6-gate.log 2>&1; echo exit=$?`
Expected: `exit=0`; the log prints a `.done` path and a log directory. Poll for the `.done` file, then read `summary.txt` in that directory: every stage exit code `0` and `frozen=YES`. A red in `e2e/recovery`, `integration/pipeline` or `unit/engine/orchestrator` is re-run with `-n 0` before it is attributed to this task (AGENTS.md § Gotchas).

- [ ] **Step 27: Branch safety, then commit by pathspec.**

Run: `cd "$(git rev-parse --show-toplevel)" && scripts/branch-safety-check.sh --intent commit > /tmp/i6-branch-safety.log 2>&1; echo exit=$?; git status --short >> /tmp/i6-branch-safety.log`
Expected: `exit=0` (no `[FAIL]` line); `git status --short` lists this task's files and nothing staged from another lane.

The three created files (`src/elspeth/web/composer/compartment_marking.py`, `tests/unit/web/composer/test_compartment_marking.py`, `tests/integration/web/test_compartment_marking.py`) are untracked, and a commit pathspec that names an untracked path is refused (`error: pathspec '...' did not match any file(s) known to git`), so mark exactly those three intent-to-add first (this records only that the paths exist; nothing else enters the index):

```bash
cd "$(git rev-parse --show-toplevel)" && git add -N src/elspeth/web/composer/compartment_marking.py tests/unit/web/composer/test_compartment_marking.py tests/integration/web/test_compartment_marking.py; echo exit=$?
```

Expected: `exit=0`.

```bash
cd "$(git rev-parse --show-toplevel)" && git commit -m "feat(identity): compartment marking on audit rows, YAML download, share snapshots and seed ingress" -- src/elspeth/cli.py src/elspeth/web/composer/compartment_marking.py src/elspeth/web/config.py src/elspeth/web/auth/audit.py src/elspeth/web/sessions/models.py src/elspeth/web/shareable_reviews/service.py src/elspeth/web/sessions/routes/composer/state.py deploy/aws-ecs/terraform/modules/scenario/locals.tf config/cicd/soft-mapping-census.yaml tests/unit/web/composer/test_compartment_marking.py tests/unit/web/test_config.py tests/unit/web/auth/test_audit.py tests/unit/web/auth/test_routes.py tests/unit/cli/test_web_command.py tests/unit/web/shareable_reviews/test_service.py tests/unit/web/sessions/test_routes.py tests/unit/web/coordination/test_library_authority.py tests/integration/web/workflow/test_library.py tests/integration/web/test_compartment_marking.py tests/unit/deployment/test_web_settings_exports_resolve.py tests/unit/architecture/test_session_db_mutation_authority.py docs/reference/configuration.md docs/guides/identity-providers.md CHANGELOG.md
```

Then run `git show --stat HEAD` and confirm it lists exactly those 24 files.
