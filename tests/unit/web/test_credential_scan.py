"""The best-effort credential scan: a match refuses, a limit or unsupported value is a counted skip."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

import pytest

from elspeth.contracts.credential_material import (
    CredentialMaterialFinding,
    CredentialTraversalPolicy,
    find_credential_material,
)
from elspeth.web import credential_scan
from elspeth.web.credential_guard import CredentialMaterialRefused, require_no_credential_material
from elspeth.web.credential_scan import record_credential_scan_skipped, scan_best_effort

_TOKEN = "sk-" + "a" * 24
_MARKER = "MARKER-" + "q" * 40


@dataclass
class _Counter:
    points: list[tuple[int, dict[str, str]]] = field(default_factory=list)

    def add(self, value: int, attributes: dict[str, str]) -> None:
        self.points.append((value, dict(attributes)))


@pytest.fixture
def counter(monkeypatch: pytest.MonkeyPatch) -> _Counter:
    recording = _Counter()
    monkeypatch.setattr(credential_scan, "_CREDENTIAL_SCAN_SKIPPED_COUNTER", recording)
    return recording


@pytest.mark.parametrize(
    ("value", "policy", "bound"),
    [
        pytest.param([[[["x"]]]], CredentialTraversalPolicy(max_depth=2), "depth", id="depth"),
        pytest.param(["a", "b", "c", "d"], CredentialTraversalPolicy(max_nodes=3), "nodes", id="nodes"),
        pytest.param({"a": 1, "b": 2, "c": 3}, CredentialTraversalPolicy(max_width=2), "width", id="mapping-width"),
        pytest.param([1, 2, 3], CredentialTraversalPolicy(max_width=2), "width", id="sequence-width"),
        pytest.param("abcdefghij", CredentialTraversalPolicy(max_string_bytes=4), "string_bytes", id="string"),
        pytest.param({"abcdefghij": 1}, CredentialTraversalPolicy(max_string_bytes=4), "string_bytes", id="mapping-key"),
        pytest.param(
            {"api_key": {"secret_ref": "abcdefghij"}},
            CredentialTraversalPolicy(max_string_bytes=8),
            "reference_bytes",
            id="secret-reference",
        ),
        pytest.param(
            {"api_key": "${LONGNAME}"},
            CredentialTraversalPolicy(max_string_bytes=8, env_ref_names={"LONGNAME"}),
            "reference_bytes",
            id="environment-reference",
        ),
    ],
)
def test_each_traversal_limit_names_its_bound(value: object, policy: CredentialTraversalPolicy, bound: str) -> None:
    finding = find_credential_material(value, policy, purpose="admission")

    assert finding == CredentialMaterialFinding("traversal_limit", "traversal_boundary", bound=cast(Any, bound))


@pytest.mark.parametrize(
    ("category", "bound"),
    [("traversal_limit", None), ("token_shape", "depth"), ("unsupported_value", "width")],
)
def test_a_finding_names_a_bound_exactly_when_it_is_a_traversal_limit(category: str, bound: str | None) -> None:
    with pytest.raises(ValueError, match="bound exactly when"):
        CredentialMaterialFinding(cast(Any, category), "traversal_boundary", bound=cast(Any, bound))


@pytest.mark.parametrize(
    ("value", "max_string_bytes", "reason"),
    [
        pytest.param(_MARKER, 16, "string_bytes", id="over-long-string"),
        pytest.param({1: _MARKER}, 262_144, "unsupported_value", id="non-string-key"),
    ],
)
def test_a_skipped_scan_lets_the_value_through_and_counts_once_without_content(
    counter: _Counter, value: object, max_string_bytes: int, reason: str
) -> None:
    require_no_credential_material(value, surface="composer_yaml_import", max_string_bytes=max_string_bytes)

    assert counter.points == [(1, {"surface": "composer_yaml_import", "reason": reason})]
    assert _MARKER not in repr(counter.points)


def test_a_match_refuses_and_counts_no_skip(counter: _Counter) -> None:
    with pytest.raises(CredentialMaterialRefused) as caught:
        require_no_credential_material({"note": _TOKEN}, surface="composer_yaml_import")

    assert caught.value.finding.category == "token_shape"
    assert counter.points == []


def test_a_match_before_a_limit_refuses_and_a_match_after_it_is_not_found(counter: _Counter) -> None:
    # The detector stops at the first finding, so a limit ends the scan.
    over_limit = "x" * 40
    policy = CredentialTraversalPolicy(max_string_bytes=30)

    before = scan_best_effort([_TOKEN, over_limit], surface="composer_yaml_import", policy=policy)
    after = scan_best_effort([over_limit, _TOKEN], surface="composer_yaml_import", policy=policy)

    assert before is not None
    assert before.category == "token_shape"
    assert after is None
    assert counter.points == [(1, {"surface": "composer_yaml_import", "reason": "string_bytes"})]


def test_skip_attributes_come_from_closed_sets(counter: _Counter) -> None:
    with pytest.raises(ValueError, match="surface is not in the closed set"):
        record_credential_scan_skipped(surface=cast(Any, "not_a_surface"), reason="depth")
    with pytest.raises(ValueError, match="reason is not in the closed set"):
        record_credential_scan_skipped(surface="composer_yaml_import", reason=cast(Any, "traversal_limit"))

    assert counter.points == []


def test_a_match_is_never_counted_as_a_skip() -> None:
    with pytest.raises(ValueError, match="Only a traversal limit or an unsupported value"):
        credential_scan.credential_scan_skip_reason(CredentialMaterialFinding("token_shape", "root"))
