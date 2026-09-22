"""Strict advisor checkpoint admission and user-note sanitization."""

import json

import pytest
from pydantic import ValidationError

from elspeth.web.composer.advisor_output import (
    ADVISOR_FINDING_CATEGORIES,
    ADVISOR_NOTE_MAX_CHARS,
    AdvisorCheckpointResponse,
    advisor_response_format,
    parse_advisor_checkpoint_response,
    sanitize_advisor_note,
)


def _clean_payload() -> dict[str, object]:
    return {"verdict": "CLEAN", "category": "other", "steps": [], "findings": "", "note": None}


def test_valid_flagged_structured_response_is_admitted() -> None:
    admission = parse_advisor_checkpoint_response(
        json.dumps(
            {
                "verdict": "FLAGGED",
                "category": "prompt_defect",
                "steps": ["classify", "missing", "classify"],
                "findings": "Technical repair finding at https://technical.example.",
                "note": "Change classify.template.",
            }
        )
    )
    assert admission.schema_valid is True
    assert admission.response is not None
    assert admission.response.verdict == "FLAGGED"
    assert admission.response.findings == "Technical repair finding at https://technical.example."
    assert admission.response.note == "Change classify.template."
    assert admission.response.steps == ["classify", "missing", "classify"]


def test_valid_clean_structured_response_is_admitted() -> None:
    admission = parse_advisor_checkpoint_response(json.dumps(_clean_payload()))
    assert admission.schema_valid is True
    assert admission.response is not None
    assert admission.response.model_dump() == _clean_payload()


@pytest.mark.parametrize("category", sorted(ADVISOR_FINDING_CATEGORIES))
def test_all_owned_categories_are_admitted(category: str) -> None:
    payload = _clean_payload()
    payload["category"] = category
    admission = parse_advisor_checkpoint_response(json.dumps(payload))
    assert admission.schema_valid is True
    assert admission.response is not None
    assert admission.response.category == category


@pytest.mark.parametrize("field", ["verdict", "category", "steps", "findings", "note"])
def test_each_schema_field_is_required(field: str) -> None:
    payload = _clean_payload()
    del payload[field]
    admission = parse_advisor_checkpoint_response(json.dumps(payload))
    assert admission.schema_valid is False
    assert admission.response is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("verdict", "clean"),
        ("verdict", True),
        ("verdict", None),
        ("category", "vibes"),
        ("category", 1),
        ("steps", "classify"),
        ("steps", [1]),
        ("steps", [True]),
        ("steps", [None]),
        ("steps", None),
        ("findings", None),
        ("findings", 2),
        ("findings", ["wrong"]),
        ("note", 3),
        ("note", False),
        ("note", []),
        ("extra", "not allowed"),
    ],
)
def test_schema_rejects_wrong_types_values_and_extra_fields(field: str, value: object) -> None:
    payload = _clean_payload()
    payload[field] = value
    admission = parse_advisor_checkpoint_response(json.dumps(payload))
    assert admission.schema_valid is False
    assert admission.response is None


@pytest.mark.parametrize("text", ["", "CLEAN", "FLAGGED: fix classify", "{", "[]", "null", "42", '"CLEAN"'])
def test_non_object_or_malformed_reply_is_not_schema_valid(text: str) -> None:
    admission = parse_advisor_checkpoint_response(text)
    assert admission.schema_valid is False
    assert admission.response is None


@pytest.mark.parametrize(
    "duplicate",
    [
        '"verdict":"FLAGGED","verdict":"CLEAN"',
        '"verdict":"CLEAN","verdict":"FLAGGED"',
        '"verdict":"CLEAN","category":"other"',
        '"verdict":"CLEAN","note":null',
        '"verdict":"CLEAN","steps":[]',
        '"verdict":"CLEAN","findings":""',
    ],
)
def test_duplicate_fields_never_overwrite_a_verdict(duplicate: str) -> None:
    text = "{" + duplicate + ',"category":"other","steps":[],"findings":"repair","note":null}'
    admission = parse_advisor_checkpoint_response(text)
    assert admission.schema_valid is False
    assert admission.response is None


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_non_finite_json_values_are_rejected(constant: str) -> None:
    text = '{"verdict":"CLEAN","category":"other","steps":[],"findings":' + constant + ',"note":null}'
    admission = parse_advisor_checkpoint_response(text)
    assert admission.schema_valid is False
    assert admission.response is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("findings", "repair \ud800"),
        ("findings", "repair \udfff"),
        ("note", "change \ud800"),
        ("steps", ["classify\udfff"]),
    ],
)
def test_escaped_unpaired_surrogates_are_rejected(field: str, value: object) -> None:
    payload = _clean_payload() | {"verdict": "FLAGGED", "findings": "Repair classify."}
    payload[field] = value
    admission = parse_advisor_checkpoint_response(json.dumps(payload))
    assert admission.schema_valid is False
    assert admission.response is None


def test_paired_surrogate_escapes_are_admitted_as_unicode_scalars() -> None:
    payload = _clean_payload() | {
        "verdict": "FLAGGED",
        "findings": "Repair \U0001f600",
        "note": "Change \U0001f600",
        "steps": ["classify\U0001f600"],
    }
    admission = parse_advisor_checkpoint_response(json.dumps(payload))
    assert admission.schema_valid is True
    assert admission.response is not None
    assert admission.response.model_dump() == payload


@pytest.mark.parametrize(
    "changes",
    [
        {"note": "Must not accompany CLEAN"},
        {"note": ""},
        {"steps": ["classify"]},
        {"verdict": "FLAGGED", "findings": ""},
        {"verdict": "FLAGGED", "findings": " \t\n"},
    ],
)
def test_semantic_failures_remain_distinguishable_from_schema_failures(changes: dict[str, object]) -> None:
    payload = _clean_payload() | changes
    admission = parse_advisor_checkpoint_response(json.dumps(payload))
    assert admission.schema_valid is True
    assert admission.response is None


def test_checkpoint_model_is_frozen() -> None:
    response = AdvisorCheckpointResponse.model_validate(_clean_payload())
    with pytest.raises(ValidationError, match="frozen"):
        response.note = "Mutation is not permitted"


def test_response_format_uses_the_required_strict_owned_schema() -> None:
    response_format = advisor_response_format()
    assert response_format == {
        "type": "json_schema",
        "json_schema": {
            "name": "advisor_checkpoint_response",
            "strict": True,
            "schema": AdvisorCheckpointResponse.model_json_schema(),
        },
    }
    schema = AdvisorCheckpointResponse.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"verdict", "category", "steps", "findings", "note"}
    assert schema["properties"]["category"] == {"$ref": "#/$defs/AdvisorFindingCategory"}
    assert set(schema["$defs"]["AdvisorFindingCategory"]["enum"]) == ADVISOR_FINDING_CATEGORIES
    assert schema["properties"]["steps"] == {"items": {"type": "string"}, "title": "Steps", "type": "array"}


@pytest.mark.parametrize(
    ("note", "expected", "urls", "emails"),
    [
        (None, None, 0, 0),
        ("  \t\n", None, 0, 0),
        ("https://example.test/path", "[link removed]", 1, 0),
        ("www.example.test/path", "[link removed]", 1, 0),
        ("_https://example.test/path_", "_[link removed]", 1, 0),
        ("_www.example.test/path_", "_[link removed]", 1, 0),
        ("**https://example.test/path**", "**[link removed]", 1, 0),
        ("_node.option_ rate: 0.5", "_node.option_ rate: 0.5", 0, 0),
        ("custom+scheme://example.test/path", "[link removed]", 1, 0),
        ("www." + chr(0x200B) + "example.test/path", "[link removed]", 1, 0),
        ("w" + chr(0x200B) + "ww.example.test/path", "[link removed]", 1, 0),
        ("reader@example.test", "<redacted-sensitive:email>", 0, 1),
        ("reader@" + chr(0x200B) + "example.test", "<redacted-sensitive:email>", 0, 1),
        ("https://example.test/reader@example.test", "[link removed]", 1, 0),
        ("node.option rate: 0.5", "node.option rate: 0.5", 0, 0),
        ("a" + chr(0x2028) + "b" + chr(0x2029) + "c", "a\nb\nc", 0, 0),
        ("a\r\nb\rc", "a\nb\nc", 0, 0),
        ("\x1b[31mkeep\x1b[0m\x00this\x07", "keepthis", 0, 0),
        ("a" + chr(0x202E) + "b" + chr(0x2066) + "c" + chr(0xFEFF), "abc", 0, 0),
        ("BEGIN_UNTRUSTED_ADVISOR_FINDINGSkeepEND_UNTRUSTED_ADVISOR_FINDINGS", "keep", 0, 0),
        ("a" + "\n" * 12 + "b", "a\n\nb", 0, 0),
        ("FLAGGED: Keep this text\nCATEGORY: other\nSTEPS: classify", "FLAGGED: Keep this text\nCATEGORY: other\nSTEPS: classify", 0, 0),
        (chr(0x200B) + "\x00", None, 0, 0),
    ],
)
def test_note_sanitization_order_and_negative_controls(note: str | None, expected: str | None, urls: int, emails: int) -> None:
    result = sanitize_advisor_note(note)
    assert result.note == expected
    assert result.url_redactions == urls
    assert result.email_redactions == emails


def test_note_redactions_are_counted_before_final_cap() -> None:
    note = "x" * (ADVISOR_NOTE_MAX_CHARS - 5) + " https://example.test/long/path reader@example.test"
    result = sanitize_advisor_note(note)
    assert result.note is not None
    assert len(result.note) == ADVISOR_NOTE_MAX_CHARS
    assert result.note.endswith("…")
    assert result.url_redactions == 1
    assert result.email_redactions == 1
    assert "https" not in result.note
    assert "reader@" not in result.note


def test_redaction_counts_measure_substitutions_and_not_preexisting_sentinels() -> None:
    result = sanitize_advisor_note("[link removed] <redacted-sensitive:email> https://one.test www.two.test a@one.test b@two.test")
    assert result.note == (
        "[link removed] <redacted-sensitive:email> [link removed] [link removed] <redacted-sensitive:email> <redacted-sensitive:email>"
    )
    assert result.url_redactions == 2
    assert result.email_redactions == 2
