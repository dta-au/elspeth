"""Tests for the red-team trigger: seam classification, angle selection,
finding parsing, and severity routing.

The trigger is the deterministic half of the adversarial review pipeline:
everything an LLM agent produces flows through ``parse_findings`` and
``route_finding``, so those two must fail closed — a malformed or
under-specified finding may never auto-file a tracker issue.
"""

from __future__ import annotations

from scripts.red_team import trigger


def _finding(**overrides: object) -> trigger.Finding:
    base: dict[str, object] = {
        "title": "Gate fails open on empty allowlist",
        "severity": "high",
        "confidence": "confirmed",
        "angle": "escape-artist",
        "commit": "abc1234",
        "files": ("src/elspeth/web/auth/tokens.py",),
        "repro": "pytest tests/unit/web/test_tokens.py -n 0",
        "detail": "The allowlist loader returns [] on parse error.",
    }
    base.update(overrides)
    return trigger.Finding(
        title=str(base["title"]),
        severity=str(base["severity"]),
        confidence=str(base["confidence"]),
        angle=str(base["angle"]),
        commit=str(base["commit"]),
        files=tuple(base["files"]),  # type: ignore[arg-type]
        repro=str(base["repro"]),
        detail=str(base["detail"]),
    )


class TestClassifyPaths:
    def test_flags_auth_paths(self) -> None:
        categories = trigger.classify_paths(["src/elspeth/web/auth/tokens.py"])
        assert categories == {"auth"}

    def test_flags_state_machine_paths(self) -> None:
        categories = trigger.classify_paths(
            [
                "src/elspeth/engine/orchestrator/lease.py",
                "src/elspeth/web/sessions/store.py",
            ]
        )
        assert categories == {"state_machine"}

    def test_flags_secrets_and_security_paths(self) -> None:
        assert trigger.classify_paths(["src/elspeth/core/secrets.py"]) == {"secrets"}
        assert trigger.classify_paths(["src/elspeth/web/secrets/vault.py"]) == {"secrets"}
        assert trigger.classify_paths(["src/elspeth/core/security/paths.py"]) == {"security"}

    def test_flags_policy_gates(self) -> None:
        categories = trigger.classify_paths(["src/elspeth/web/plugin_policy/admission.py"])
        assert categories == {"policy_gate"}

    def test_flags_cicd_gate_configs(self) -> None:
        categories = trigger.classify_paths(["config/cicd/masquerade_baseline.yaml", "scripts/cicd/plugin_hash.py"])
        assert categories == {"cicd_gate"}

    def test_ignores_docs_and_frontend(self) -> None:
        categories = trigger.classify_paths(
            [
                "docs/architecture/adr/031-tutorial.md",
                "README.md",
                "src/elspeth/web/frontend/src/App.tsx",
            ]
        )
        assert categories == set()

    def test_mixed_paths_union_categories(self) -> None:
        categories = trigger.classify_paths(
            [
                "src/elspeth/web/auth/tokens.py",
                "src/elspeth/core/checkpoint/writer.py",
                "docs/notes.md",
            ]
        )
        assert categories == {"auth", "state_machine"}


class TestSelectAttackAngles:
    def test_no_categories_yields_no_angles(self) -> None:
        assert trigger.select_attack_angles(set()) == ()

    def test_selection_is_deterministic(self) -> None:
        first = trigger.select_attack_angles({"auth", "state_machine"})
        second = trigger.select_attack_angles({"state_machine", "auth"})
        assert [angle.name for angle in first] == [angle.name for angle in second]


class TestParseFindings:
    def test_extracts_fenced_json_findings(self) -> None:
        text = (
            "I attacked the diff.\n\n"
            "```json\n"
            '{"findings": [{"title": "T", "severity": "high",'
            ' "confidence": "confirmed", "files": ["a.py"],'
            ' "repro": "pytest x -n 0", "detail": "D"}]}\n'
            "```\n"
        )
        findings, errors = trigger.parse_findings(text, angle="escape-artist", commit="abc")
        assert errors == []
        assert len(findings) == 1
        assert findings[0].title == "T"
        assert findings[0].angle == "escape-artist"
        assert findings[0].commit == "abc"
        assert findings[0].files == ("a.py",)

    def test_missing_required_field_goes_to_errors_not_findings(self) -> None:
        text = '```json\n{"findings": [{"title": "T", "confidence": "confirmed", "files": [], "repro": "r", "detail": "d"}]}\n```\n'
        findings, errors = trigger.parse_findings(text, angle="a", commit="c")
        assert findings == []
        assert len(errors) == 1

    def test_no_json_block_is_an_error(self) -> None:
        findings, errors = trigger.parse_findings("No structured output at all.", angle="a", commit="c")
        assert findings == []
        assert len(errors) == 1

    def test_malformed_json_is_an_error(self) -> None:
        findings, errors = trigger.parse_findings("```json\n{not json\n```\n", angle="a", commit="c")
        assert findings == []
        assert len(errors) == 1

    def test_empty_findings_list_is_clean(self) -> None:
        findings, errors = trigger.parse_findings('```json\n{"findings": []}\n```\n', angle="a", commit="c")
        assert findings == []
        assert errors == []


class TestRouteFinding:
    def test_confirmed_high_files_an_issue(self) -> None:
        assert trigger.route_finding(_finding(severity="high")) == "file"
        assert trigger.route_finding(_finding(severity="critical")) == "file"

    def test_speculative_critical_only_logs(self) -> None:
        finding = _finding(severity="critical", confidence="speculative")
        assert trigger.route_finding(finding) == "log"

    def test_probable_high_only_logs(self) -> None:
        finding = _finding(severity="high", confidence="probable")
        assert trigger.route_finding(finding) == "log"

    def test_confirmed_medium_only_logs(self) -> None:
        finding = _finding(severity="medium")
        assert trigger.route_finding(finding) == "log"

    def test_unknown_severity_fails_closed_to_log(self) -> None:
        finding = _finding(severity="catastrophic")
        assert trigger.route_finding(finding) == "log"

    def test_unknown_confidence_fails_closed_to_log(self) -> None:
        finding = _finding(confidence="definitely")
        assert trigger.route_finding(finding) == "log"


class TestCommandConstruction:
    def test_high_severity_maps_to_priority_one(self) -> None:
        argv = trigger.file_issue_argv(_finding(severity="high"))
        assert argv[argv.index("-p") + 1] == "1"

    def test_agent_prompt_names_commit_and_angle(self) -> None:
        angle = trigger.select_attack_angles({"auth"})[-1]
        prompt = trigger.build_agent_prompt("deadbeef", angle)
        assert "deadbeef" in prompt
        assert angle.name in prompt
