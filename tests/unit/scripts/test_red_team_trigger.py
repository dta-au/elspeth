"""Tests for the red-team trigger: seam classification, angle selection,
finding parsing, and local report persistence.

The trigger is the deterministic half of the adversarial review pipeline:
everything an LLM agent produces flows through ``parse_findings``.
Automatic reviews persist local evidence and never publish issues.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from scripts.red_team import trigger


def _finding(**overrides: object) -> trigger.Finding:
    return replace(
        trigger.Finding(
            title="Gate fails open on empty allowlist",
            severity="high",
            confidence="confirmed",
            angle="escape-artist",
            commit="abc1234",
            files=("src/elspeth/web/auth/tokens.py",),
            repro="pytest tests/unit/web/test_tokens.py -n 0",
            detail="The allowlist loader returns [] on parse error.",
        ),
        **overrides,
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


class TestCommandConstruction:
    def test_agent_prompt_names_commit_and_angle(self) -> None:
        angle = trigger.select_attack_angles({"auth"})[-1]
        prompt = trigger.build_agent_prompt("deadbeef", angle)
        assert "deadbeef" in prompt
        assert angle.name in prompt


class TestLocalReports:
    def test_report_preserves_findings_and_errors_across_runs(self, tmp_path: Path) -> None:
        report = trigger.append_review_log(tmp_path, [_finding()], ["bad JSON"])
        trigger.append_review_log(tmp_path, [_finding(title="Second finding", severity="low")], [])
        text = report.read_text()
        assert "Gate fails open on empty allowlist" in text
        assert "[high/confirmed]" in text
        assert "src/elspeth/web/auth/tokens.py" in text
        assert "pytest tests/unit/web/test_tokens.py -n 0" in text
        assert "The allowlist loader returns [] on parse error." in text
        assert "parse error: bad JSON" in text
        assert "Second finding" in text

    def test_run_logs_confirmed_critical_findings_without_publishing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(trigger, "_git_output", lambda *args: "abc1234")
        monkeypatch.setattr(trigger, "changed_paths", lambda *args: ["src/elspeth/web/auth/tokens.py"])
        finding = {
            "title": "Critical boundary escape",
            "severity": "critical",
            "confidence": "confirmed",
            "files": ["src/elspeth/web/auth/tokens.py"],
            "repro": "reproduce the escape",
            "detail": "Evidence retained for operator triage.",
        }
        output = "```json\n" + json.dumps({"findings": [finding]}) + "\n```"
        process = Mock(returncode=0)
        process.communicate.return_value = (json.dumps({"result": output}), "")
        popen = Mock(return_value=process)
        monkeypatch.setattr(trigger.subprocess, "Popen", popen)
        publish = Mock(side_effect=AssertionError("Unexpected external command"))
        monkeypatch.setattr(trigger.subprocess, "run", publish)

        assert trigger.run_red_team("HEAD", tmp_path, dry_run=False) == 0

        report = tmp_path / ".claude/red-team/review-log.md"
        assert "[critical/confirmed]" in report.read_text()
        assert "Critical boundary escape" in report.read_text()
        assert len(list((tmp_path / ".claude/red-team/runs").glob("*.json"))) == 3
        assert all(call.args[0][0] == "claude" for call in popen.call_args_list)
        publish.assert_not_called()
