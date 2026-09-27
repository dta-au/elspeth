"""Behavior tests for the trust-tier ratchet (``scripts/trust_tier_ratchet.py``).

The comparison is line-insensitive and one-directional: new findings block,
removed ones do not.
These tests exercise the comparison on synthetic findings; they never spawn the
real lint.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import scripts.trust_tier_ratchet as ratchet_module
from scripts.trust_tier_ratchet import (
    LINTS_SOURCE_ROOT,
    RULE_ID,
    SCAN_ROOT,
    VERIFY_MODE,
    VERIFY_MODE_ENV,
    FindingRecord,
    RatchetError,
    RatchetResult,
    RawFindingRecord,
    compare_corpora,
    format_report,
    lint_environment,
    normalise_path,
    parse_findings,
    run_raw_observations,
)

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
LEGACY_HEAD_SHA = "a11977d79509b31f374e8fcdbcd1d83574a27acf"


def _finding(
    path: str = "web/app.py", rule_id: str = "R5", message: str = "isinstance() used: x", *, line: int = 10, severity: str = "error"
) -> FindingRecord:
    return FindingRecord(path=path, rule_id=rule_id, message=message, line=line, column=4, severity=severity)


def test_ratchet_runs_the_same_rule_and_verify_mode_as_the_previous_entry() -> None:
    """The ratchet reproduces the previous entry's arguments and environment; it adds no key handling."""
    env = lint_environment()

    assert RULE_ID == "trust_tier.tier_model"
    assert SCAN_ROOT == "src/elspeth"
    assert env["PYTHONPATH"] == LINTS_SOURCE_ROOT == "elspeth-lints/src"
    assert env[VERIFY_MODE_ENV] == VERIFY_MODE == "shape-only-when-key-missing"
    assert VERIFY_MODE_ENV == "ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE"


def test_identical_corpora_pass() -> None:
    head = [_finding(), _finding(path="core/dag.py", rule_id="R6", message="Exception swallowed", line=40)]
    staged = list(head)

    result = compare_corpora(head, staged)

    assert result.exit_code == 0
    assert (result.head_count, result.staged_count, result.added_count, result.removed_count) == (2, 2, 0, 0)


def test_removed_only_passes_and_reports_the_removal() -> None:
    head = [_finding(), _finding(path="core/dag.py", rule_id="R6", message="Exception swallowed", line=40)]
    staged = [head[0]]

    result = compare_corpora(head, staged)

    assert result.exit_code == 0
    assert (result.head_count, result.staged_count, result.added_count, result.removed_count) == (2, 1, 0, 1)


def test_one_added_finding_fails_and_is_printed_verbatim() -> None:
    head = [_finding()]
    new = _finding(path="plugins/new.py", rule_id="R1", message="getattr() with default on owned type", line=7)
    staged = [*head, new]

    result = compare_corpora(head, staged)
    report = format_report(result, head_sha=HEAD_SHA)

    assert result.exit_code == 1
    assert (result.head_count, result.staged_count, result.added_count, result.removed_count) == (1, 2, 1, 0)
    assert f"+ {new.render()}" in report
    assert new.render() == "plugins/new.py:7:4: R1: getattr() with default on owned type"
    assert "FAIL" in report
    assert HEAD_SHA in report


def test_line_number_only_shift_passes() -> None:
    head = [_finding(line=10), _finding(path="core/dag.py", rule_id="R6", message="Exception swallowed", line=40)]
    staged = [_finding(line=13), _finding(path="core/dag.py", rule_id="R6", message="Exception swallowed", line=52)]

    result = compare_corpora(head, staged)

    assert result.exit_code == 0
    assert (result.added_count, result.removed_count) == (0, 0)


_BLOB_HELPER = dedent(
    '''
    from collections.abc import Mapping

    def _contains_blob_ref(value: object) -> bool:
        """Conservatively disable verdict reuse when an option contains a blob binding."""
        if isinstance(value, Mapping):
            return "blob_ref" in value or any(_contains_blob_ref(child) for child in value.values())
        if isinstance(value, (tuple, list)):
            return any(_contains_blob_ref(child) for child in value)
        return False
    '''
)


def _relocation_fixture(tmp_path: Path, *, old_source: str = _BLOB_HELPER, new_source: str = _BLOB_HELPER) -> tuple[Path, Path]:
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    for root, path, source in (
        (old_root, "service.py", old_source),
        (new_root, "composer_preflight.py", new_source),
    ):
        target = root / "src/elspeth/web/composer" / path
        target.parent.mkdir(parents=True)
        target.write_text(source, encoding="utf-8")
    return old_root, new_root


def _blob_findings(path: str, *, severity: str = "error") -> list[FindingRecord]:
    return [
        _finding(path, "R5", "isinstance() used: if isinstance(value, Mapping):", line=6, severity=severity),
        _finding(path, "R5", "isinstance() used: if isinstance(value, (tuple, list)):", line=8, severity=severity),
    ]


def test_reviewed_relocation_accepts_exact_two_site_move_and_reports_raw_findings(tmp_path: Path) -> None:
    old_root, new_root = _relocation_fixture(tmp_path)
    old = _blob_findings("web/composer/service.py")
    new = _blob_findings("web/composer/composer_preflight.py")

    result = compare_corpora(old, new, head_root=old_root, staged_root=new_root)

    assert result.exit_code == 0
    assert result.added_count == result.removed_count == 0
    assert result.relocated == tuple(new)
    assert all(f"~ {finding.render()}" in format_report(result, head_sha=HEAD_SHA) for finding in new)
    assert compare_corpora(old, new).exit_code == 1  # no source proof, no relocation


@pytest.mark.parametrize(
    "new_source",
    [
        _BLOB_HELPER.replace('"blob_ref" in value', '"wrong_ref" in value'),
        _BLOB_HELPER.replace("from collections.abc import Mapping", "from typing import Mapping"),
        _BLOB_HELPER + "\nMapping = dict\n",
        _BLOB_HELPER + "\ndef _contains_blob_ref(value):\n    return True\n",
        _BLOB_HELPER + "\nfrom suspect import *\n",
        _BLOB_HELPER + "\nif True:\n    from suspect import *\n",
        "from __future__ import annotations\n" + _BLOB_HELPER,
        _BLOB_HELPER + "\ndel Mapping\n",
        _BLOB_HELPER + "\ntry:\n    raise RuntimeError()\nexcept RuntimeError as Mapping:\n    pass\n",
        _BLOB_HELPER + "\nmatch {}:\n    case Mapping:\n        pass\n",
        _BLOB_HELPER + "\nmatch {}:\n    case {**Mapping}:\n        pass\n",
        _BLOB_HELPER + '\nexec("Mapping = dict")\n',
        _BLOB_HELPER + '\nglobals()["Mapping"] = dict\n',
        _BLOB_HELPER + "\nif True:\n    def _contains_blob_ref(value):\n        return False\n",
        _BLOB_HELPER + "\ndef unrelated(default=(Mapping := dict)):\n    pass\n",
        _BLOB_HELPER + "\n@((Mapping := lambda fn: fn))\ndef unrelated():\n    pass\n",
        _BLOB_HELPER + "\nclass Unrelated((Mapping := dict)):\n    pass\n",
        _BLOB_HELPER + '\nclass Unrelated:\n    globals()["Mapping"] = dict\n',
        _BLOB_HELPER + '\ndef unrelated():\n    globals()["Mapping"] = dict\n',
    ],
)
def test_reviewed_relocation_rejects_changed_body_binding_or_duplicate_definition(tmp_path: Path, new_source: str) -> None:
    old_root, new_root = _relocation_fixture(tmp_path, new_source=new_source)
    old = _blob_findings("web/composer/service.py")
    new = _blob_findings("web/composer/composer_preflight.py")

    result = compare_corpora(old, new, head_root=old_root, staged_root=new_root)

    assert result.exit_code == 1
    assert result.added_count == 2
    assert result.relocated == ()


def test_reviewed_relocation_requires_exact_multiplicity_severity_and_no_new_finding(tmp_path: Path) -> None:
    old_root, new_root = _relocation_fixture(tmp_path)
    old = _blob_findings("web/composer/service.py")
    new = _blob_findings("web/composer/composer_preflight.py")
    for changed in (new[:1], [*new, new[0]], [new[0], _finding(new[1].path, "R5", new[1].message, line=8, severity="warning")]):
        result = compare_corpora(old, changed, head_root=old_root, staged_root=new_root)
        assert result.exit_code == 1
        assert result.relocated == ()
    new_r6 = _finding(new[0].path, "R6", "Exception swallowed without re-raise or explicit error: except BlobNotFoundError:")
    result = compare_corpora(old, [*new, new_r6], head_root=old_root, staged_root=new_root)
    assert result.exit_code == 1
    assert result.relocated == tuple(new)
    assert result.added_count == 1
    assert f"+ {new_r6.render()}" in format_report(result, head_sha=HEAD_SHA)


_OLD_R6_SOURCE = dedent(
    """
    from elspeth.contracts.blobs import BlobNotFoundError
    from elspeth.web.composer.protocol import ComposerRuntimePreflightError

    class ComposerServiceImpl:
        def __init__(self, blob_service):
            self._blob_service = blob_service

        def _runtime_preflight(self):
            def _blob_get_metadata(blob_id):
                try:
                    return self._blob_service.get_metadata(blob_id)
                except BlobNotFoundError:
                    return None
            return _blob_get_metadata("id")

        async def _stage_pipeline_plan(self):
            self._require_sessions_service()
            if _is_pending_interpretation_handoff():
                try:
                    return self._cached_runtime_preflight()
                except ComposerRuntimePreflightError:
                    return None
    """
)
_NEW_PREFLIGHT_R6_SOURCE = dedent(
    """
    from elspeth.contracts.blobs import BlobNotFoundError

    class ComposerPreflight:
        def __init__(self, blob_service):
            self._blob_service = blob_service

        def runtime_preflight(self):
            def _blob_get_metadata(blob_id):
                try:
                    return self._blob_service.get_metadata(blob_id)
                except BlobNotFoundError:
                    return None
            return _blob_get_metadata("id")
    """
)
_NEW_PLANNING_R6_SOURCE = dedent(
    """
    from elspeth.web.composer.protocol import ComposerRuntimePreflightError

    class PlanningApplication:
        def __init__(self, preflight):
            self._preflight = preflight

        async def _stage_pipeline_plan(self):
            self._sessions_service
            if is_pending_interpretation_handoff():
                try:
                    return self._preflight.cached_runtime_preflight()
                except ComposerRuntimePreflightError:
                    return None
    """
)
_NEW_SERVICE_R6_SOURCE = dedent(
    """
    from elspeth.web.composer.composer_preflight import ComposerPreflight
    from elspeth.web.composer.planning_application import PlanningApplication

    class ComposerServiceImpl:
        def __init__(self, blob_service):
            self._preflight = ComposerPreflight(blob_service=blob_service)
            self._planning = PlanningApplication(preflight=self._preflight)
    """
)


def _r6_fixture(
    tmp_path: Path,
    *,
    old_source: str = _OLD_R6_SOURCE,
    preflight_source: str = _NEW_PREFLIGHT_R6_SOURCE,
    planning_source: str = _NEW_PLANNING_R6_SOURCE,
    service_source: str = _NEW_SERVICE_R6_SOURCE,
    allowance: bool = True,
) -> tuple[Path, Path]:
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    sources = (
        (old_root, "service.py", old_source),
        (new_root, "service.py", service_source),
        (new_root, "composer_preflight.py", preflight_source),
        (new_root, "planning_application.py", planning_source),
    )
    for root, name, source in sources:
        target = root / "src/elspeth/web/composer" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    config = old_root / "config/cicd/enforce_tier_model/web.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        "per_file_rules:\n- pattern: web/composer/service.py\n  rules: [R6]\n" if allowance else "per_file_rules: []\n",
        encoding="utf-8",
    )
    staged_config = new_root / "config/cicd/enforce_tier_model/web.yaml"
    staged_config.parent.mkdir(parents=True)
    staged_config.write_text("allow_hits: []\n", encoding="utf-8")
    lint_stub = new_root / "elspeth-lints/src/elspeth_lints/__init__.py"
    lint_stub.parent.mkdir(parents=True)
    lint_stub.write_text("", encoding="utf-8")
    return old_root, new_root


def _raw_r6(source: str, path: str, exception: str, context: tuple[str, ...], *, severity: str = "error") -> RawFindingRecord:
    import ast

    tree = ast.parse(source)
    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name) and node.type.id == exception
    ]
    assert len(handlers) == 1
    return RawFindingRecord(
        finding=_finding(
            path,
            "R6",
            f"Exception swallowed without re-raise or explicit error: except {exception}:",
            line=handlers[0].lineno,
            severity=severity,
        ),
        symbol_context=context,
    )


def _r6_observations(
    old_source: str = _OLD_R6_SOURCE, preflight_source: str = _NEW_PREFLIGHT_R6_SOURCE, planning_source: str = _NEW_PLANNING_R6_SOURCE
) -> tuple[list[RawFindingRecord], list[RawFindingRecord]]:
    return (
        [
            _raw_r6(
                old_source,
                "web/composer/service.py",
                "BlobNotFoundError",
                ("ComposerServiceImpl", "_runtime_preflight", "_blob_get_metadata"),
            ),
            _raw_r6(
                old_source, "web/composer/service.py", "ComposerRuntimePreflightError", ("ComposerServiceImpl", "_stage_pipeline_plan")
            ),
        ],
        [
            _raw_r6(
                preflight_source,
                "web/composer/composer_preflight.py",
                "BlobNotFoundError",
                ("ComposerPreflight", "runtime_preflight", "_blob_get_metadata"),
            ),
            _raw_r6(
                planning_source,
                "web/composer/planning_application.py",
                "ComposerRuntimePreflightError",
                ("PlanningApplication", "_stage_pipeline_plan"),
            ),
        ],
    )


def _compare_r6(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    changed: str | None = None,
    replacement: str | None = None,
    allowance: bool = True,
) -> tuple[RatchetResult, tuple[Path, Path]]:
    _baseline_old, baseline_new = _r6_fixture(tmp_path / "reviewed")
    baseline_digest = ratchet_module._reviewed_source_manifest_sha256(baseline_new)
    assert baseline_digest is not None
    monkeypatch.setattr(ratchet_module, "_LEGACY_R6_STAGED_MANIFEST_SHA256", baseline_digest)
    sources = {
        "old_source": _OLD_R6_SOURCE,
        "preflight_source": _NEW_PREFLIGHT_R6_SOURCE,
        "planning_source": _NEW_PLANNING_R6_SOURCE,
        "service_source": _NEW_SERVICE_R6_SOURCE,
    }
    if changed is not None:
        assert changed in sources and replacement is not None
        sources[changed] = replacement
    old_root, new_root = _r6_fixture(tmp_path / "candidate", **sources, allowance=allowance)
    old_raw, new_raw = _r6_observations()
    result = compare_corpora(
        [],
        [r.finding for r in new_raw],
        head_root=old_root,
        staged_root=new_root,
        head_sha=LEGACY_HEAD_SHA,
        head_raw=old_raw,
        staged_raw=new_raw,
    )
    return result, (old_root, new_root)


def test_reviewed_legacy_r6_transfer_reports_new_cli_findings_truthfully(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result, _roots = _compare_r6(tmp_path, monkeypatch)
    assert result.exit_code == 0
    assert result.raw_cli_added_count == 2
    assert result.added_count == 0
    assert len(result.legacy_r6_transfers) == 2
    report = format_report(result, head_sha=LEGACY_HEAD_SHA)
    assert "newly surfaced CLI findings" in report
    assert "raw CLI additions before reviewed transfers: 2" in report
    assert report.count("+ reviewed legacy R6:") == 2


@pytest.mark.parametrize(
    ("changed", "replacement"),
    [
        ("preflight_source", _NEW_PREFLIGHT_R6_SOURCE.replace("return None", "return False")),
        ("preflight_source", _NEW_PREFLIGHT_R6_SOURCE.replace("from elspeth.contracts.blobs", "from suspect")),
        ("preflight_source", _NEW_PREFLIGHT_R6_SOURCE + "\nBlobNotFoundError = RuntimeError\n"),
        ("preflight_source", _NEW_PREFLIGHT_R6_SOURCE + "\nexec('BlobNotFoundError = RuntimeError')\n"),
        ("preflight_source", _NEW_PREFLIGHT_R6_SOURCE.replace("self._blob_service = blob_service", "self._blob_service = None")),
        (
            "preflight_source",
            _NEW_PREFLIGHT_R6_SOURCE.replace(
                "self._blob_service = blob_service", "blob_service = None\n        self._blob_service = blob_service"
            ),
        ),
        (
            "preflight_source",
            _NEW_PREFLIGHT_R6_SOURCE.replace(
                "self._blob_service = blob_service", "if False:\n            self._blob_service = blob_service"
            ),
        ),
        ("preflight_source", _NEW_PREFLIGHT_R6_SOURCE + "\nComposerPreflight._blob_service = None\n"),
        ("preflight_source", _NEW_PREFLIGHT_R6_SOURCE + "\nBlobNotFoundError.__bases__ = (Exception,)\n"),
        (
            "preflight_source",
            _NEW_PREFLIGHT_R6_SOURCE.replace(
                'return _blob_get_metadata("id")', 'self._blob_service = None\n        return _blob_get_metadata("id")'
            ),
        ),
        ("planning_source", _NEW_PLANNING_R6_SOURCE.replace("return None", "return False")),
        ("planning_source", _NEW_PLANNING_R6_SOURCE.replace("self._preflight = preflight", "self._preflight = None")),
        ("planning_source", _NEW_PLANNING_R6_SOURCE.replace("return None", "self._preflight = None\n                return None")),
        ("planning_source", _NEW_PLANNING_R6_SOURCE + "\nis_pending_interpretation_handoff = lambda: False\n"),
        ("service_source", _NEW_SERVICE_R6_SOURCE.replace("preflight=self._preflight", "preflight=None")),
        ("service_source", _NEW_SERVICE_R6_SOURCE.replace("self._planning =", "self._preflight = None\n        self._planning =")),
        ("service_source", "ComposerPreflight = lambda **kwargs: None\n" + _NEW_SERVICE_R6_SOURCE),
    ],
)
def test_reviewed_legacy_r6_rejects_body_or_binding_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str, replacement: str
) -> None:
    result, _roots = _compare_r6(tmp_path, monkeypatch, changed=changed, replacement=replacement)
    assert result.exit_code == 1
    assert result.added_count >= 1


def test_reviewed_legacy_r6_requires_head_coverage_raw_evidence_and_exact_multiplicity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, (old_root, new_root) = _compare_r6(tmp_path, monkeypatch, allowance=False)
    assert result.exit_code == 1 and result.added_count == 2
    old_root, new_root = _r6_fixture(tmp_path / "second")
    old_raw, new_raw = _r6_observations()
    staged = [record.finding for record in new_raw]
    variants = (
        (LEGACY_HEAD_SHA, old_raw[:1], new_raw, staged),
        (LEGACY_HEAD_SHA, old_raw, new_raw[:1], staged),
        (HEAD_SHA, old_raw, new_raw, staged),
        (LEGACY_HEAD_SHA, old_raw, new_raw, [*staged, staged[0]]),
        (LEGACY_HEAD_SHA, old_raw, new_raw, [*staged, _finding("web/composer/other.py", "R6", "new catch")]),
    )
    for sha, old, new, cli in variants:
        outcome = compare_corpora([], cli, head_root=old_root, staged_root=new_root, head_sha=sha, head_raw=old, staged_raw=new)
        assert outcome.exit_code == 1
        assert outcome.added_count >= 1
    already_visible = compare_corpora(
        [old_raw[0].finding],
        staged,
        head_root=old_root,
        staged_root=new_root,
        head_sha=LEGACY_HEAD_SHA,
        head_raw=old_raw,
        staged_raw=new_raw,
    )
    assert already_visible.exit_code == 1
    assert already_visible.added_count == 1


def test_raw_r6_observer_has_positive_and_negative_controls(tmp_path: Path) -> None:
    old_root, _new_root = _r6_fixture(tmp_path)
    lint_source = Path(__file__).resolve().parents[3] / "elspeth-lints/src"
    link = old_root / "elspeth-lints/src"
    link.parent.mkdir(parents=True)
    link.symlink_to(lint_source, target_is_directory=True)
    observations = run_raw_observations(
        old_root, python=str(Path(__file__).resolve().parents[3] / ".venv/bin/python"), paths=["web/composer/service.py"], rule_ids=["R6"]
    )
    assert {(record.finding.rule_id, record.symbol_context) for record in observations} >= {
        ("R6", ("ComposerServiceImpl", "_runtime_preflight", "_blob_get_metadata")),
        ("R6", ("ComposerServiceImpl", "_stage_pipeline_plan")),
    }
    target = old_root / "src/elspeth/web/composer/service.py"
    target.write_text("class ComposerServiceImpl:\n    pass\n", encoding="utf-8")
    assert (
        run_raw_observations(
            old_root,
            python=str(Path(__file__).resolve().parents[3] / ".venv/bin/python"),
            paths=["web/composer/service.py"],
            rule_ids=["R6"],
        )
        == []
    )


def test_a_second_occurrence_of_an_existing_key_counts_as_added() -> None:
    """Keys form a multiset: a duplicate of a standing finding is still a new finding."""
    head = [_finding(line=10)]
    staged = [_finding(line=10), _finding(line=90)]

    result = compare_corpora(head, staged)
    report = format_report(result, head_sha=HEAD_SHA)

    assert result.exit_code == 1
    assert result.added_count == 1
    assert result.added[0].head_multiplicity == 1
    assert result.added[0].staged_multiplicity == 2
    assert "already present 1x in HEAD, 2x staged" in report
    assert f"+ {_finding(line=90).render()}" in report


def test_note_severity_findings_never_gate_but_are_counted() -> None:
    """Mirror the CLI: only non-note severities fail, so a new ``@trust_boundary`` suppression note passes."""
    head = [_finding()]
    staged = [*head, _finding(path="core/x.py", rule_id="R_TB_SUPPRESSED", message="@trust_boundary suppressed R5", severity="note")]

    result = compare_corpora(head, staged)

    assert result.exit_code == 0
    assert (result.head_notes, result.staged_notes, result.added_count) == (0, 1, 0)


def test_passing_report_says_ok_and_carries_the_counts() -> None:
    result = compare_corpora([_finding()], [_finding()])

    report = format_report(result, head_sha=HEAD_SHA)

    assert report.startswith(f"trust-tier ratchet: working tree vs HEAD {HEAD_SHA}\n")
    assert "head 1, staged 1, added 0, removed 0" in report
    assert report.rstrip().endswith("OK: the staged tree adds no trust-tier finding relative to HEAD.")


def test_parse_findings_reads_the_cli_json_shape_and_relativises_absolute_paths(tmp_path: Path) -> None:
    tree_root = tmp_path / "tree"
    tree_root.mkdir()
    payload = (
        "["
        '{"column": 2, "file_path": "web/app.py", "fingerprint": "abc", "line": 5, "message": "m", "rule_id": "R5", '
        '"severity": "error", "suggestion": null},'
        f'{{"column": 0, "file_path": "{tree_root}/elspeth-lints/src/rule.py", "fingerprint": "def", "line": 0, '
        '"message": "Stale map entry", "rule_id": "trust_tier.tier_model", "severity": "error", "suggestion": null}'
        "]"
    )

    findings = parse_findings(payload, tree_root=tree_root)

    assert findings == [
        FindingRecord(path="web/app.py", rule_id="R5", message="m", line=5, column=2, severity="error"),
        FindingRecord(
            path="elspeth-lints/src/rule.py", rule_id="trust_tier.tier_model", message="Stale map entry", line=0, column=0, severity="error"
        ),
    ]


def test_absolute_path_outside_the_tree_is_left_alone(tmp_path: Path) -> None:
    assert normalise_path("/somewhere/else/x.py", tree_root=tmp_path) == "/somewhere/else/x.py"
    assert normalise_path("web/app.py", tree_root=tmp_path) == "web/app.py"


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        '{"file_path": "x"}',
        "[1]",
        '[{"column": 2, "file_path": "x", "line": 5, "message": "m", "rule_id": "R5"}]',
        '[{"column": 2, "file_path": 3, "line": 5, "message": "m", "rule_id": "R5", "severity": "error"}]',
        '[{"column": "2", "file_path": "x", "line": 5, "message": "m", "rule_id": "R5", "severity": "error"}]',
    ],
)
def test_parse_findings_refuses_malformed_documents(payload: str, tmp_path: Path) -> None:
    with pytest.raises(RatchetError):
        parse_findings(payload, tree_root=tmp_path)


@pytest.mark.parametrize(
    "shadow",
    ["src/elspeth/config/cicd/enforce_tier_model.yaml", "src/config/cicd/enforce_tier_model/web.yaml"],
)
def test_reviewed_transfer_rejects_allowlist_shadow_with_unchanged_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shadow: str
) -> None:
    accepted, (old_root, new_root) = _compare_r6(tmp_path, monkeypatch)
    assert accepted.exit_code == 0
    digest = ratchet_module._reviewed_source_manifest_sha256(new_root)
    shadow_path = new_root / shadow
    shadow_path.parent.mkdir(parents=True)
    shadow_path.write_text("per_file_rules: []\n", encoding="utf-8")
    assert ratchet_module._reviewed_source_manifest_sha256(new_root) == digest
    old_raw, new_raw = _r6_observations()
    rejected = compare_corpora(
        [],
        [record.finding for record in new_raw],
        head_root=old_root,
        staged_root=new_root,
        head_sha=LEGACY_HEAD_SHA,
        head_raw=old_raw,
        staged_raw=new_raw,
    )
    assert rejected.exit_code == 1 and rejected.added_count == 2


def test_sealed_r5_relocation_requires_old_and_new_cli_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_root, new_root = tmp_path / "old", tmp_path / "new"
    for root, name, source in (
        (
            old_root,
            "service.py",
            "class ComposerServiceImpl:\n    def _serialize_response_via_walker(self, response):\n        return response\n",
        ),
        (new_root, "turn_audit.py", "def _serialize_response_via_walker(response):\n    return response\n"),
    ):
        target = root / "src/elspeth/web/composer" / name
        target.parent.mkdir(parents=True)
        target.write_text(source, encoding="utf-8")
    old_config = old_root / "config/cicd/enforce_tier_model/web.yaml"
    old_config.parent.mkdir(parents=True)
    old_config.write_text("per_file_rules:\n- pattern: web/composer/service.py\n  rules: [R6]\n", encoding="utf-8")
    new_config = new_root / "config/cicd/enforce_tier_model/web.yaml"
    new_config.parent.mkdir(parents=True)
    new_config.write_text("allow_hits: []\n", encoding="utf-8")
    lint_stub = new_root / "elspeth-lints/src/elspeth_lints/__init__.py"
    lint_stub.parent.mkdir(parents=True)
    lint_stub.write_text("", encoding="utf-8")
    digest = ratchet_module._reviewed_source_manifest_sha256(new_root)
    assert digest is not None
    monkeypatch.setattr(ratchet_module, "_LEGACY_R6_STAGED_MANIFEST_SHA256", digest)
    message = "isinstance() used: if isinstance(response, Mapping):"
    old = RawFindingRecord(
        _finding("web/composer/service.py", "R5", message, line=6),
        ("ComposerServiceImpl", "_serialize_response_via_walker"),
    )
    new = RawFindingRecord(
        _finding("web/composer/turn_audit.py", "R5", message, line=5),
        ("_serialize_response_via_walker",),
    )

    def compare(head: list[FindingRecord], old_raw: list[RawFindingRecord], new_raw: list[RawFindingRecord]) -> RatchetResult:
        return compare_corpora(
            head,
            [new.finding],
            head_root=old_root,
            staged_root=new_root,
            head_sha=LEGACY_HEAD_SHA,
            head_raw=old_raw,
            staged_raw=new_raw,
        )

    accepted = compare([old.finding], [old], [new])
    assert accepted.exit_code == 0 and accepted.sealed_r5_relocations == (new.finding,)
    assert "~ reviewed CLI R5:" in format_report(accepted, head_sha=LEGACY_HEAD_SHA)
    assert compare([], [old], [new]).exit_code == 1
    assert compare([old.finding], [], [new]).exit_code == 1
    assert compare([old.finding], [old], [RawFindingRecord(new.finding, ("wrong_owner",))]).exit_code == 1
    assert compare([old.finding, old.finding], [old], [new]).exit_code == 1
    (new_root / "src/elspeth/web/composer/turn_audit.py").write_text("changed = True\n", encoding="utf-8")
    assert compare([old.finding], [old], [new]).exit_code == 1


def test_sealed_r6_tightening_requires_exact_same_site_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_source = (
        "async def _surface_pending_interpretation_reviews_under_writer():\n"
        "    for site in sites:\n"
        "        try:\n"
        "            await writer(site)\n"
        "        except ValueError:\n"
        "            continue\n"
    )
    new_source = old_source.replace("except ValueError:", "except InterpretationResolveError:")
    old_root, new_root = _r6_fixture(tmp_path)
    for root, source in ((old_root, old_source), (new_root, new_source)):
        target = root / "src/elspeth/web/composer/interpretation_surfacing.py"
        target.write_text(source, encoding="utf-8")
    digest = ratchet_module._reviewed_source_manifest_sha256(new_root)
    assert digest is not None
    monkeypatch.setattr(ratchet_module, "_LEGACY_R6_STAGED_MANIFEST_SHA256", digest)
    context = ("_surface_pending_interpretation_reviews_under_writer",)
    old = _raw_r6(old_source, "web/composer/interpretation_surfacing.py", "ValueError", context)
    new = _raw_r6(new_source, "web/composer/interpretation_surfacing.py", "InterpretationResolveError", context)

    def compare(
        head: list[FindingRecord],
        staged: list[FindingRecord],
        old_raw: list[RawFindingRecord],
        new_raw: list[RawFindingRecord],
    ) -> RatchetResult:
        return compare_corpora(
            head,
            staged,
            head_root=old_root,
            staged_root=new_root,
            head_sha=LEGACY_HEAD_SHA,
            head_raw=old_raw,
            staged_raw=new_raw,
        )

    accepted = compare([old.finding], [new.finding], [old], [new])
    assert accepted.exit_code == 0
    assert accepted.raw_cli_added_count == 1
    assert accepted.sealed_r6_tightenings == (new.finding,)
    assert "reviewed same-site R6 exception-type tightening" in format_report(accepted, head_sha=LEGACY_HEAD_SHA)
    sibling = _finding("web/composer/interpretation_surfacing.py", "R6", old.finding.message, line=1)
    assert compare([old.finding, sibling], [new.finding, sibling], [old], [new]).exit_code == 0
    assert compare([], [new.finding], [old], [new]).exit_code == 1
    assert compare([old.finding], [new.finding], [], [new]).exit_code == 1
    assert compare([old.finding], [new.finding], [old], [RawFindingRecord(new.finding, ("wrong_owner",))]).exit_code == 1
    assert compare([old.finding, old.finding], [new.finding], [old], [new]).exit_code == 1
    assert compare([old.finding], [new.finding, new.finding], [old], [new]).exit_code == 1
    additional = _finding("web/composer/interpretation_surfacing.py", "R6", "different catch")
    assert compare([old.finding], [new.finding, additional], [old], [new]).exit_code == 1
    target = new_root / "src/elspeth/web/composer/interpretation_surfacing.py"
    target.write_text(new_source + "changed = True\n", encoding="utf-8")
    assert compare([old.finding], [new.finding], [old], [new]).exit_code == 1
