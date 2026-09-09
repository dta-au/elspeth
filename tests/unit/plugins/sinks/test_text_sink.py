"""Tests for the strict, line-oriented local text sink."""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.plugin_semantics import SemanticValueType, TextFraming, UnknownSemanticPolicy
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.infrastructure.preflight import plugin_preflight_mode
from elspeth.plugins.sinks.text_sink import TextSink, TextSinkConfig
from tests.fixtures.base_classes import inject_write_failure
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import make_factory


def _config(path: Path, **overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "path": str(path),
        "field": "line_text",
        "encoding": "utf-8",
        "mode": "write",
        "schema": {"mode": "observed"},
    }
    config.update(overrides)
    return config


def _sink_context() -> PluginContext:
    return make_context(landscape=make_factory().plugin_audit_writer())


@pytest.mark.parametrize("encoding", ["utf-8", "ascii", "latin-1", "cp1252"])
def test_text_sink_accepts_only_supported_ascii_compatible_encodings(encoding: str) -> None:
    parsed = TextSinkConfig.from_dict(_config(Path("out.txt"), encoding=encoding), plugin_name="text")
    assert parsed.encoding == encoding


@pytest.mark.parametrize("encoding", ["utf-16", "utf-32", "utf-7", "shift_jis", "unknown-codec"])
def test_text_sink_rejects_encodings_outside_closed_set(encoding: str) -> None:
    with pytest.raises(PluginConfigError, match="encoding"):
        TextSinkConfig.from_dict(_config(Path("out.txt"), encoding=encoding), plugin_name="text")


@pytest.mark.parametrize("field", ["", "two words", "with-hyphen", "9lives", "class"])
def test_text_sink_rejects_non_identifier_or_keyword_field(field: str) -> None:
    with pytest.raises(PluginConfigError, match="identifier"):
        TextSinkConfig.from_dict(_config(Path("out.txt"), field=field), plugin_name="text")


def test_text_sink_config_schema_has_no_headers_property() -> None:
    assert "headers" not in TextSinkConfig.model_json_schema()["properties"]


@pytest.mark.parametrize(
    ("mode", "collision_policy"),
    [
        ("append", "fail_if_exists"),
        ("append", "auto_increment"),
        ("write", "append_or_create"),
    ],
)
def test_text_sink_rejects_incompatible_mode_and_collision_policy(mode: str, collision_policy: str) -> None:
    with pytest.raises(PluginConfigError, match="collision_policy"):
        TextSinkConfig.from_dict(
            _config(Path("out.txt"), mode=mode, collision_policy=collision_policy),
            plugin_name="text",
        )


def test_preflight_construction_never_mutates_or_resolves_output(tmp_path: Path) -> None:
    path = tmp_path / "out.txt"
    path.write_bytes(b"winner\n")

    with plugin_preflight_mode(True):
        sink = TextSink(_config(path, collision_policy="auto_increment"))

    assert sink._path == path
    assert list(tmp_path.iterdir()) == [path]


def test_configure_for_resume_switches_to_append_contract(tmp_path: Path) -> None:
    sink = inject_write_failure(TextSink(_config(tmp_path / "out.txt")))

    sink.configure_for_resume()

    assert sink._mode == "append"
    assert sink._collision_policy == "append_or_create"


def test_text_sink_assistance_pins_strict_line_and_resume_contract() -> None:
    assistance = TextSink.get_agent_assistance(issue_code=None)

    assert assistance is not None
    hints = " ".join(assistance.composer_hints)
    assert "configured field" in hints
    assert "string" in hints
    assert "CR or LF" in hints
    assert "append" in hints
    assert "resume" in hints


class TestTextSinkResumeModeResolution:
    """elspeth-fc9906e398: resolved effect mode must claim what resume executes."""

    def test_resume_purpose_resolves_post_resume_append_mode(self) -> None:
        """A write-configured text sink resumes in append mode; the resolver must say so."""
        from elspeth.contracts.sink_effects import ResolvedSinkEffectMode, SinkEffectExecutionPurpose

        config = {"path": "/tmp/out.txt", "field": "line_text", "schema": {"mode": "observed"}, "mode": "write"}

        resolved = TextSink._resolve_sink_effect_mode(config, purpose=SinkEffectExecutionPurpose.RESUME)

        assert resolved == ResolvedSinkEffectMode("append")

    def test_fresh_purpose_keeps_configured_mode(self) -> None:
        from elspeth.contracts.sink_effects import ResolvedSinkEffectMode, SinkEffectExecutionPurpose

        config = {"path": "/tmp/out.txt", "field": "line_text", "schema": {"mode": "observed"}, "mode": "write"}

        resolved = TextSink._resolve_sink_effect_mode(config, purpose=SinkEffectExecutionPurpose.FRESH)

        assert resolved == ResolvedSinkEffectMode("write")


class TestTextSinkSemanticRequirement:
    """The authoring-time statement of the runtime one-record-per-row invariant.

    ``prepare_effect`` diverts any value carrying CR or LF, so a pipeline that
    routes newline-bearing text here publishes a 0-byte file and reports
    success. This requirement is what makes that a refusal at authoring time
    instead (elspeth-afdf55a17c, ADR-039).
    """

    def _requirement(self, path: Path):
        requirements = TextSink(_config(path)).input_semantic_requirements()
        assert len(requirements.fields) == 1
        return requirements.fields[0]

    def test_requirement_is_declared_on_the_configured_field(self, tmp_path: Path) -> None:
        requirement = self._requirement(tmp_path / "out.txt")
        assert requirement.field_name == "line_text"
        assert requirement.requirement_code == "text.field.single_line_text"
        assert requirement.configured_by == ("field",)

    def test_requirement_follows_a_renamed_field(self, tmp_path: Path) -> None:
        sink = TextSink(_config(tmp_path / "out.txt", field="body"))
        assert sink.input_semantic_requirements().fields[0].field_name == "body"

    def test_accepts_compact_framing_only(self, tmp_path: Path) -> None:
        """COMPACT is the whole accepted set — every other member bears newlines."""
        requirement = self._requirement(tmp_path / "out.txt")
        assert requirement.accepted_text_framings == frozenset({TextFraming.COMPACT})

    def test_line_compatible_is_excluded(self, tmp_path: Path) -> None:
        """The regression guard for the exact defect.

        LINE_COMPATIBLE is the newline-BEARING side of the vocabulary:
        line_explode accepts it as line-SPLITTABLE and web_scrape declares it
        for markdown. Adding it here would bless web_scrape(markdown) -> text,
        which is the zero-byte failure this work exists to prevent.
        """
        requirement = self._requirement(tmp_path / "out.txt")
        assert TextFraming.LINE_COMPATIBLE not in requirement.accepted_text_framings
        assert TextFraming.NEWLINE_FRAMED not in requirement.accepted_text_framings
        assert TextFraming.UNCONSTRAINED not in requirement.accepted_text_framings

    def test_content_kind_is_deliberately_unconstrained(self, tmp_path: Path) -> None:
        """Empty means "dimension unconstrained", and that is the decision here.

        Constraining it blocks nothing framing does not already block, while
        downgrading to UNKNOWN every producer that declares framing but
        honestly abstains on kind — a generative producer does exactly that.
        """
        requirement = self._requirement(tmp_path / "out.txt")
        assert requirement.accepted_content_kinds == frozenset()

    def test_value_type_is_constrained_to_str(self, tmp_path: Path) -> None:
        requirement = self._requirement(tmp_path / "out.txt")
        assert requirement.accepted_value_types == frozenset({SemanticValueType.STR})

    def test_unknown_policy_is_warn_so_an_undeclared_producer_is_not_blocked(self, tmp_path: Path) -> None:
        """Only a positive conflicting claim blocks. FAIL would make the sink
        unwireable from every producer that declares nothing — the same
        over-blocking that refused the correct llm -> line_explode repair."""
        requirement = self._requirement(tmp_path / "out.txt")
        assert requirement.unknown_policy is UnknownSemanticPolicy.WARN

    def test_probe_config_constructs_standalone(self) -> None:
        """The satisfiability invariant instantiates this directly, not via the
        manager, and hard-fails a declarer it cannot probe."""
        sink = TextSink(TextSink.probe_config())
        try:
            assert sink.input_semantic_requirements().fields[0].requirement_code == "text.field.single_line_text"
        finally:
            sink.close()


class TestTextSinkConflictAssistance:
    """A prohibition that names no alternative is what produced g11."""

    def test_requirement_code_arm_names_both_remedies(self) -> None:
        assistance = TextSink.get_agent_assistance(issue_code="text.field.single_line_text")
        assert assistance is not None
        assert assistance.plugin_name == "text"
        assert assistance.issue_code == "text.field.single_line_text"
        assert "line_explode" in assistance.summary
        assert "document" in assistance.summary
        assert assistance.suggested_fixes
        assert any("line_explode" in fix for fix in assistance.suggested_fixes)
        assert any("document" in fix for fix in assistance.suggested_fixes)

    def test_unrelated_issue_code_returns_nothing(self) -> None:
        assert TextSink.get_agent_assistance(issue_code="some.other.code") is None

    def test_discovery_branch_is_unchanged(self) -> None:
        assistance = TextSink.get_agent_assistance()
        assert assistance is not None
        assert assistance.issue_code is None
        assert assistance.composer_hints
