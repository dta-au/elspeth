"""tests/unit/architecture/test_legacy_oidc_path_deletion_guard.py

Whole-tree gate for the legacy OIDC/Entra browser path's deletion (identity
sprint step E, spec §Deleted + phase 4 "AST no-deleted-imports assertion").
It asserts over the ENTIRE src tree and over the tracked deploy and runbook
trees: a green scoped run somewhere else proves nothing about its subject.

Deleting a code path is not finished when the modules are gone. What kills
it is that nothing can name it again by accident: no module imports it, no
setting resurrects it under its old name, no task definition exports it. The
first two are AST facts and are checked as AST facts here -- a substring
scan would fire on this file's own docstring, and on every honest mention of
the words in a comment.

The third has its own dedicated pin
(``tests/unit/deployment/test_web_settings_exports_resolve.py``, which
resolves every tracked ``ELSPETH_WEB__*`` export against the live field set).
This gate adds the half that pin cannot see: a deleted setting name is
refused BY NAME, so re-adding ``oidc_issuer`` to ``WebSettings`` would make
that pin pass again while quietly restoring the vocabulary the spec deleted.

If a future profile genuinely needs one of these names, delete its entry
here in the same commit that adds it, with the ruling in the message. The
list is a record of a decision, not a spelling checker.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.helpers.tree_gate import iter_gate_sources

from elspeth.web.config import WebSettings

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "src" / "elspeth"

# The modules that housed the legacy browser-client providers.
DELETED_MODULES = (
    "elspeth.web.auth.oidc",
    "elspeth.web.auth.entra",
)

# The names those modules exported. An import of one of these from ANY module
# is the same defect as importing the module: a resurrected provider under a
# new address is still the deleted path.
DELETED_SYMBOLS = frozenset({"OIDCAuthProvider", "EntraAuthProvider"})

# ``WebSettings`` fields deleted with the path (spec §Settings, row "remove").
# ``entra_tenant_id`` is NOT here: the Entra profile still derives its issuer
# from it.
DELETED_SETTINGS = (
    "oidc_issuer",
    "oidc_audience",
    "oidc_client_id",
    "oidc_audience_claim",
    "oidc_authorization_endpoint",
    "oidc_token_endpoint",
    "oidc_authorization_allowed_origins",
)

# Helpers whose only callers were the legacy path. They are gone from
# ``web/auth/urls.py``; a module importing one again would be reviving the
# browser-facing origin policy the profile registry replaced.
DELETED_URL_HELPERS = frozenset({"validate_oidc_browser_endpoints", "oidc_browser_endpoint_origin"})

# The legacy bearer path's decode entry points on ``JWKSTokenValidator``.
# They took the same pinned core as ``decode_id_token``, so they were not
# unsafe -- they were a SECOND public way in, and after the bearer path went
# their only callers were their own tests. The SSO walk decodes through
# ``decode_id_token_with_refresh``; one entry point is one thing to keep
# pinned.
DELETED_VALIDATOR_METHODS = frozenset({"decode_token", "decode_token_with_refresh"})
ID_TOKEN_MODULE = SRC_ROOT / "web" / "auth" / "id_token.py"


def _import_targets(tree: ast.Module) -> set[str]:
    """Every module path and imported name in one parsed module."""
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                targets.add(node.module)
            targets.update(alias.name for alias in node.names)
    return targets


def test_the_deleted_provider_modules_are_gone_from_the_tree() -> None:
    survivors = [path.name for path in (SRC_ROOT / "web" / "auth").iterdir() if path.name in ("oidc.py", "entra.py")]
    assert not survivors, f"legacy provider modules are back in web/auth: {survivors} (identity sprint step E deleted them)"


def test_no_module_imports_the_deleted_providers() -> None:
    offenders: list[str] = []
    for parsed in iter_gate_sources(SRC_ROOT):
        targets = _import_targets(parsed.tree)
        named = sorted((targets & DELETED_SYMBOLS) | {module for module in DELETED_MODULES if module in targets})
        if named:
            offenders.append(f"{parsed.path.relative_to(REPO_ROOT)}: {', '.join(named)}")
    assert not offenders, (
        "the deleted legacy OIDC/Entra provider path was imported again:\n"
        + "\n".join(offenders)
        + "\nEvery non-local deployment authenticates through the SSO walk (spec D2)."
    )


def test_no_module_imports_the_deleted_browser_endpoint_helpers() -> None:
    offenders: list[str] = []
    for parsed in iter_gate_sources(SRC_ROOT):
        named = sorted(_import_targets(parsed.tree) & DELETED_URL_HELPERS)
        if named:
            offenders.append(f"{parsed.path.relative_to(REPO_ROOT)}: {', '.join(named)}")
    assert not offenders, (
        "a browser-facing endpoint helper deleted with the legacy path was imported again:\n"
        + "\n".join(offenders)
        + "\nEndpoint policy is the profile's (validate_discovered_endpoints), applied to every discovered endpoint."
    )


def test_the_validator_keeps_one_decode_entry_point() -> None:
    """``decode_token``/``decode_token_with_refresh`` stay deleted.

    Read from the AST rather than by attribute lookup: this file must not
    add a dynamic-attribute site, and the source is the authority anyway.
    """
    tree = ast.parse(ID_TOKEN_MODULE.read_text(encoding="utf-8"))
    defined = {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    resurrected = sorted(defined & DELETED_VALIDATOR_METHODS)
    assert not resurrected, (
        f"the legacy bearer decode entry points are back on JWKSTokenValidator: {resurrected}. "
        "The SSO walk decodes through decode_id_token_with_refresh; a second public decode is a "
        "second place a caller can be handed an unpinned algorithm list."
    )
    assert "decode_id_token_with_refresh" in defined, "the live decode entry point must still exist"


def test_the_deleted_settings_do_not_come_back_by_name() -> None:
    resurrected = sorted(name for name in DELETED_SETTINGS if name in WebSettings.model_fields)
    assert not resurrected, (
        f"deleted WebSettings fields are back: {resurrected} — the pluggable-SSO vocabulary (sso_*) replaced them "
        "(spec §Settings). Deployments export the sso_* names; re-adding one of these makes both vocabularies live."
    )


def test_the_settings_the_profiles_still_need_are_not_collateral_damage() -> None:
    """The deletion must not have taken the Entra tenant or the SSO fields."""
    for name in ("entra_tenant_id", "sso_issuer", "sso_client_id", "sso_endpoint_origins"):
        assert name in WebSettings.model_fields, f"{name} is required by a live profile and must not be deleted"
