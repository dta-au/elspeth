"""Tests for skill pack loading — core and deployment layers.

Verifies:
- load_skill loads core skills from the package directory
- load_skill raises FileNotFoundError for missing skills
- load_deployment_skill returns "" when data_dir is None
- load_deployment_skill returns "" when skill file does not exist
- load_deployment_skill raises on PermissionError/IsADirectoryError (operator misconfiguration)
- load_deployment_skill returns content when skill file exists
- load_deployment_skill rejects oversized files with ValueError
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from elspeth.web.composer.skills import (
    MAX_DEPLOYMENT_SKILL_BYTES,
    load_deployment_skill,
    load_skill,
)


class TestLoadSkill:
    """Core skill loading from package directory."""

    def test_loads_existing_skill(self) -> None:
        """The pipeline_composer skill exists and loads as non-empty string."""
        content = load_skill("pipeline_composer")
        assert isinstance(content, str)
        assert len(content) > 0

    def test_pipeline_composer_does_not_tell_model_to_surface_backend_owned_reviews(self) -> None:
        content = load_skill("pipeline_composer")
        normalized = " ".join(content.split())

        assert "`llm_prompt_template` | backend (auto-staged on every LLM node) | backend" in content
        assert '`kind="llm_prompt_template"` | You author any LLM `prompt_template`' not in content
        assert "except backend-owned `llm_prompt_template` rows" in normalized

    def test_pipeline_composer_preflight_uses_authored_requirement_shape(self) -> None:
        content = load_skill("pipeline_composer")

        assert "Every requirement object has `kind`, `user_term`, and `draft`, plus optional `display_title`." in content
        assert "Every requirement object has `id`, `kind`, `user_term`, `status`, and `draft`." not in content

    def test_missing_skill_raises_file_not_found(self) -> None:
        """Requesting a non-existent skill must crash — not return empty."""
        with pytest.raises(FileNotFoundError):
            load_skill("nonexistent_skill_that_does_not_exist")

    def test_comparison_guidance_preserves_literal_prompts_and_control_variables(self) -> None:
        normalized = " ".join(load_skill("pipeline_composer").split())

        assert "Preserve supplied literal prompts character-for-character" in normalized
        assert "A/B comparison" in normalized
        assert "response format, output schema and enum values" in normalized
        assert "output field names may differ" in normalized

    def test_review_recovery_does_not_reopen_resolved_content(self) -> None:
        normalized = " ".join(load_skill("pipeline_composer").split())

        assert "If the matching requirement is already resolved" in normalized
        assert "do not re-stage unchanged content" in normalized
        assert "retry only if a matching pending requirement exists" in normalized

    def test_warning_diagnosis_requires_evidence_before_mutating(self) -> None:
        normalized = " ".join(load_skill("pipeline_composer").split())

        assert "A high-severity warning does not establish its root cause" in normalized
        assert "Do not attribute a schema or presence warning to LLM nondeterminism" in normalized
        assert "If the same diagnostic survives a targeted repair" in normalized

    def test_computed_source_review_is_exempt_from_persisted_requirement_precondition(self) -> None:
        normalized = " ".join(load_skill("pipeline_composer").split())

        assert "`source_data_contract` | backend (computed from current graph demand) | YOU" in normalized
        assert "does not require a persisted `interpretation_requirements` row" in normalized
        assert "For review kinds backed by persisted requirement rows" in normalized
        assert "For persisted review kinds, if review handoff fails" in normalized
        assert "For computed `source_data_contract` recovery, recheck current missing source-field demand" in normalized
        assert "Do not call the review tool for a requirement that was not successfully staged" not in normalized

    def test_runtime_explanations_distinguish_configuration_delivery_and_compliance(self) -> None:
        normalized = " ".join(load_skill("pipeline_composer").split())

        assert "Saved configuration does not prove the provider request bytes" in normalized
        assert "provider receipt does not prove model compliance" in normalized
        assert "observations, hypotheses, and verified causes" in normalized


class TestLoadDeploymentSkill:
    """Deployment skill overlay loading."""

    def test_returns_empty_when_data_dir_is_none(self) -> None:
        """None data_dir means no deployment layer — return empty string."""
        assert load_deployment_skill("pipeline_composer", None) == ""

    def test_returns_empty_when_data_dir_has_no_skills_dir(self, tmp_path: Path) -> None:
        """data_dir exists but has no skills/ subdirectory."""
        assert load_deployment_skill("pipeline_composer", tmp_path) == ""

    def test_returns_empty_when_skill_file_missing(self, tmp_path: Path) -> None:
        """data_dir has skills/ but the specific skill file is absent."""
        (tmp_path / "skills").mkdir()
        assert load_deployment_skill("pipeline_composer", tmp_path) == ""

    def test_returns_content_when_skill_file_exists(self, tmp_path: Path) -> None:
        """Deployment skill file exists — return its content."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_file = skills_dir / "pipeline_composer.md"
        skill_file.write_text("# Deployment Skill\n\nCustom provider info here.\n")

        result = load_deployment_skill("pipeline_composer", tmp_path)
        assert result == "# Deployment Skill\n\nCustom provider info here.\n"

    def test_returns_empty_when_skill_file_is_empty(self, tmp_path: Path) -> None:
        """An empty deployment skill file returns "" (treated as absent)."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "pipeline_composer.md").write_text("")

        result = load_deployment_skill("pipeline_composer", tmp_path)
        assert result == ""

    def test_raises_when_path_is_directory(self, tmp_path: Path) -> None:
        """If the skill 'file' is actually a directory, crash — operator misconfiguration."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        # Create a directory where the file should be.
        (skills_dir / "pipeline_composer.md").mkdir()

        with pytest.raises(IsADirectoryError):
            load_deployment_skill("pipeline_composer", tmp_path)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file permissions")
    def test_raises_on_permission_error(self, tmp_path: Path) -> None:
        """Unreadable skill file must crash — not silently degrade to no overlay."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_file = skills_dir / "pipeline_composer.md"
        skill_file.write_text("# Should not be readable\n")
        skill_file.chmod(0o000)

        with pytest.raises(PermissionError):
            load_deployment_skill("pipeline_composer", tmp_path)

    def test_rejects_oversized_file(self, tmp_path: Path) -> None:
        """Files exceeding MAX_DEPLOYMENT_SKILL_BYTES raise ValueError."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        # Write content just over the limit.
        oversized = "x" * (MAX_DEPLOYMENT_SKILL_BYTES + 1)
        (skills_dir / "pipeline_composer.md").write_text(oversized)

        with pytest.raises(ValueError, match="exceeds the"):
            load_deployment_skill("pipeline_composer", tmp_path)

    def test_oversized_file_is_rejected_without_full_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Oversized files must be bounded-read — never materialized in full.

        The loader must cap the read at MAX + 1 bytes. If it fell back to the
        old unbounded path.read_text(), a hostile deployment skill could
        exhaust process memory before the size guard ever fires. This test
        proves the read is bounded by forbidding read_text() and writing a
        file an order of magnitude larger than the limit.
        """
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        skill_file = skills_dir / "pipeline_composer.md"
        # Ten times over the limit — no real skill would ever be this big.
        skill_file.write_bytes(b"x" * (MAX_DEPLOYMENT_SKILL_BYTES * 10))

        # Sentinel: if the fix regresses back to read_text(), this fires
        # before the ValueError and the test fails with a clear message.
        def _forbidden(*_args: object, **_kwargs: object) -> str:
            raise AssertionError(
                "load_deployment_skill must not call Path.read_text() — oversized files must be rejected by a bounded binary read."
            )

        monkeypatch.setattr(Path, "read_text", _forbidden)

        with pytest.raises(ValueError, match="exceeds the"):
            load_deployment_skill("pipeline_composer", tmp_path)

    def test_accepts_file_at_size_limit(self, tmp_path: Path) -> None:
        """Files exactly at MAX_DEPLOYMENT_SKILL_BYTES are accepted."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        at_limit = "x" * MAX_DEPLOYMENT_SKILL_BYTES
        (skills_dir / "pipeline_composer.md").write_text(at_limit)

        result = load_deployment_skill("pipeline_composer", tmp_path)
        assert len(result) == MAX_DEPLOYMENT_SKILL_BYTES

    def test_handles_unicode_content(self, tmp_path: Path) -> None:
        """Deployment skills with unicode content load correctly."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        content = "# Déploiement\n\nFournisseurs: éàü\n"
        (skills_dir / "pipeline_composer.md").write_text(content, encoding="utf-8")

        result = load_deployment_skill("pipeline_composer", tmp_path)
        assert result == content

    def test_accepts_string_data_dir(self, tmp_path: Path) -> None:
        """data_dir can be a string path, not just a Path object."""
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "pipeline_composer.md").write_text("content")

        result = load_deployment_skill("pipeline_composer", str(tmp_path))
        assert result == "content"
