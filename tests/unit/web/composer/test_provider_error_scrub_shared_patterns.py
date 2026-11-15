"""Provider error text stored on LLM-call audit rows uses shared credential shapes."""

import base64
import hashlib
import hmac
import json
import random
import re
import time
from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.contracts.credential_material import find_credential_material
from elspeth.web.composer.llm_response_parsing import _LLM_ERROR_DOTTED_WORDS, _is_ordinary_dotted_name, build_llm_call_record
from elspeth.web.validation import _redact_sensitive_content

_LLM_ERROR_BASE_DOTTED_RE = re.compile(r"\b[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b")


def _stored_error(message: str) -> str:
    call = build_llm_call_record(
        model_requested="provider/model",
        messages=[{"role": "user", "content": "hello"}],
        tools=None,
        status=ComposerLLMCallStatus.API_ERROR,
        started_at=datetime.now(UTC),
        started_ns=time.monotonic_ns(),
        temperature=None,
        seed=None,
        response=None,
        error_class="ProviderError",
        error_message=message,
    )
    assert call.error_message is not None
    return call.error_message


def test_hyphenated_provider_key_is_redacted_in_audit_error() -> None:
    key = "sk-" + "proj-" + ("aB9_" * 10)
    stored = _stored_error("401 Incorrect API key provided: " + key)
    has_marker = "<redacted-sensitive:openai_key>" in stored
    leaks_key = key in stored

    assert has_marker
    assert not leaks_key
    assert "raw_error_hash=" in stored


def test_dotted_field_name_stays_in_audit_error() -> None:
    message = "Field customer.address.street is missing"

    assert _stored_error(message) == message


def test_jwt_is_redacted_in_audit_error() -> None:
    jwt = "eyJ" + ("aB9_" * 5) + "." + ("bC8_" * 5) + "." + ("cD7_" * 5)
    stored = _stored_error("Provider rejected token " + jwt)
    has_marker = "<redacted-sensitive:jwt>" in stored
    leaks_jwt = jwt in stored

    assert has_marker
    assert not leaks_jwt
    assert "raw_error_hash=" in stored


def test_bearer_match_precedes_dotted_compatibility_match() -> None:
    token = "eyJ" + "a" * 16 + "." + "b" * 16 + "." + "c" * 16
    stored = _stored_error("Bearer " + token)
    assert "<redacted-sensitive:bearer_token>" in stored
    leaks_token = token in stored
    assert not leaks_token


def _signed_jwt(header_json: str) -> str:
    def encode(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    body = encode(header_json.encode()) + "." + encode(json.dumps({"sub": "test"}).encode())
    test_key = "runtime" + "-test-signing-key"
    signature = encode(hmac.new(test_key.encode(), body.encode(), hashlib.sha256).digest())
    token = body + "." + signature
    assert hmac.compare_digest(signature, encode(hmac.new(test_key.encode(), body.encode(), hashlib.sha256).digest()))
    return token


@pytest.mark.parametrize(
    "header", ['{"alg":"HS256","typ":"JWT"}', '{\n  "alg": "HS256", "typ": "JWT"\n}', ' {"alg":"HS256"}', '\n{"alg":"HS256"}']
)
@pytest.mark.parametrize("wrapper", ["bare", "glued", "prose", "json"])
def test_valid_jwt_header_variants_are_not_stored(header: str, wrapper: str) -> None:
    token = _signed_jwt(header)
    message = {
        "bare": token,
        "glued": "prefix" + token,
        "prose": "Provider rejected " + token + " during retry",
        "json": json.dumps({"detail": token}),
    }[wrapper]
    stored = _stored_error(message)
    leaked = token in stored
    assert not leaked
    assert "provider error detail redacted" in stored or "<redacted-sensitive:jwt>" in stored


@pytest.mark.parametrize(
    "detail",
    [
        "Field customer.address.street is missing",
        "module.package.function unavailable",
        "api.example.test unavailable",
        "model.v1.preview unavailable",
        "request 123e4567-e89b-12d3-a456-426614174000 failed",
        "request id 12345678901234567890 failed",
        "digest " + "0123456789abcdef" * 4,
        "resets at epoch 1731628800",
        "upstream temporarily unavailable",
    ],
)
def test_dotted_names_and_identifiers_remain_readable(detail: str) -> None:
    assert _stored_error(detail) == detail


@pytest.mark.parametrize("detail", ["messages.0000.content", "response_format.json_schema.name"])
def test_known_request_paths_remain_readable(detail: str) -> None:
    assert _stored_error(detail) == detail


@pytest.mark.parametrize("detail", ["invoice.address.street", "customer.longitude.street", "custom.gateway.secret"])
def test_unknown_dotted_words_are_conservatively_redacted(detail: str) -> None:
    assert detail not in _stored_error(detail)


@pytest.mark.parametrize("length", [12, 13, 14, 15])
@pytest.mark.parametrize("prefix", ["401 Incorrect API key provided: ", "Authentication failed for ", "Invalid cookie "])
def test_letter_leading_hex_credential_is_not_stored(length: int, prefix: str) -> None:
    token = ".".join("a" + "1" * (length - 1) for _ in range(3))
    leaks = token in _stored_error(prefix + token)
    assert not leaks


def _opaque_segment(rng: random.Random, alphabet: str, length: int) -> str:
    boundary_alphabet = alphabet.replace("_", "").replace("-", "")
    return rng.choice(boundary_alphabet) + "".join(rng.choice(alphabet) for _ in range(length - 2)) + rng.choice(boundary_alphabet)


def test_seeded_opaque_dotted_corpus_is_not_stored() -> None:
    rng = random.Random(2605)
    alphabets = (
        "0123456789abcdef",
        "0123456789abcdefghijklmnopqrstuvwxyz",
        "abcdefghijklmnopqrstuvwxyz234567",
        "abcdefghijklmnopqrstuvwxyz",
        "0123456789",
        "abcdefghijklmnopqrstuvwxyz0123456789_-",
    )
    for index in range(2016):
        alphabet = alphabets[index % len(alphabets)]
        length = (12, 13, 14, 15, 6, 8, 12, 15, 8, 6, 12, 14)[index % 12]
        token = ".".join(_opaque_segment(rng, alphabet, length) for _ in range(3))
        assert _LLM_ERROR_BASE_DOTTED_RE.fullmatch(token) is not None
        leaks = token in _stored_error(token)
        assert not leaks


@pytest.mark.parametrize(
    "alphabet,length",
    [
        ("0123456789abcdef", 12),
        ("0123456789abcdefghijklmnopqrstuvwxyz", 12),
        ("abcdefghijklmnopqrstuvwxyz234567", 12),
        ("abcdefghijklmnopqrstuvwxyz", 12),
        ("0123456789", 6),
        ("0123456789", 12),
        ("abcdefghijklmnopqrstuvwxyz0123456789_-", 8),
    ],
)
@pytest.mark.parametrize("wrapper", ["bare", "glued", "prose", "json", "401", "authentication", "cookie"])
def test_opaque_dotted_families_in_provider_contexts(alphabet: str, length: int, wrapper: str) -> None:
    rng = random.Random(26)
    token = ".".join(_opaque_segment(rng, alphabet, length) for _ in range(3))
    message = {
        "bare": token,
        "glued": "prefix" + token,
        "prose": "Provider rejected " + token,
        "json": json.dumps({"detail": token}),
        "401": "401 Incorrect API key provided: " + token,
        "authentication": "Authentication failed for " + token,
        "cookie": "Invalid cookie " + token,
    }[wrapper]
    leaks = token in _stored_error(message)
    assert not leaks


def test_nonce_timestamp_truncated_mac_is_not_stored() -> None:
    key = "runtime" + "-signing-key"
    nonce = "abcdef123456"
    timestamp = "173162880000"
    signature = hmac.new(key.encode(), (nonce + "." + timestamp).encode(), hashlib.sha256).hexdigest()[:12]
    token = ".".join((nonce, timestamp, signature))
    for message in (token, "prefix" + token, "Provider rejected " + token, json.dumps({"detail": token}), "Invalid cookie " + token):
        leaks = token in _stored_error(message)
        assert not leaks


@settings(max_examples=100)
@given(
    st.sampled_from(
        (
            "0123456789abcdef",
            "abcdefghijklmnopqrstuvwxyz234567",
            "0123456789abcdefghijklmnopqrstuvwxyz",
            "0123456789",
            "abcdefghijklmnopqrstuvwxyz0123456789_-",
        )
    ),
    st.integers(min_value=4, max_value=64),
    st.integers(min_value=0, max_value=2**32 - 1),
)
def test_unknown_random_dotted_matches_are_redacted(alphabet: str, length: int, seed: int) -> None:
    rng = random.Random(seed)
    token = ".".join(_opaque_segment(rng, alphabet, length) for _ in range(3))
    if _LLM_ERROR_BASE_DOTTED_RE.search(token) and not _is_ordinary_dotted_name(token):
        leaks = token in _stored_error(token)
        assert not leaks


@pytest.mark.parametrize(
    "candidate,expected",
    [
        ("customer.address.street", True),
        ("messages.1234.content", True),
        ("response_format.json_schema.name", True),
        ("messages.12345.content", False),
        ("1234.5678.9012", False),
        ("customer.eyJsecret.street", False),
        ("customer.unknown.street", False),
    ],
)
def test_dotted_exemption_has_closed_vocabulary(candidate: str, expected: bool) -> None:
    assert _is_ordinary_dotted_name(candidate) is expected


@settings(max_examples=100)
@given(
    st.integers(min_value=0, max_value=2**32 - 1),
    st.integers(min_value=4, max_value=20),
)
def test_dotted_exemption_only_accepts_vocabulary_parts(seed: int, length: int) -> None:
    rng = random.Random(seed)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    segments = [_opaque_segment(rng, alphabet, length) for _ in range(3)]
    candidate = ".".join(segments)
    result = _is_ordinary_dotted_name(candidate)
    if result:
        assert any(not segment.isdecimal() for segment in segments)
        for segment in segments:
            assert not segment.startswith("eyJ")
            if segment.isdecimal():
                assert len(segment) <= 4
            else:
                parts = re.split(r"[_-]", re.sub(r"([a-z])([A-Z])", r"\1_\2", segment).lower())
                assert all(part in _LLM_ERROR_DOTTED_WORDS for part in parts)


@settings(max_examples=100)
@given(st.integers(min_value=0, max_value=2**32 - 1), st.integers(min_value=16, max_value=40))
def test_random_base64url_triples_are_not_stored(seed: int, length: int) -> None:
    rng = random.Random(seed)
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    token = ".".join(_opaque_segment(rng, alphabet, length) for _ in range(3))
    assert _LLM_ERROR_BASE_DOTTED_RE.fullmatch(token) is not None
    stored = _stored_error("Provider rejected " + token)
    leaks_token = token in stored
    assert not leaks_token


@pytest.mark.parametrize(
    "header",
    ['{"alg":"HS256"}', '{\n "alg":"HS256"\n}', ' {"alg":"HS256"}', '\n{"alg":"HS256"}', '\t{"alg":"HS256"}', '\r\n{"alg":"HS256"}'],
)
@pytest.mark.parametrize("wrapper", ["bare", "glued", "prose", "json"])
def test_signed_jwt_variants_preserve_base_redaction(header: str, wrapper: str) -> None:
    token = _signed_jwt(header)
    message = {"bare": token, "glued": "prefix" + token, "prose": "Provider rejected " + token, "json": json.dumps({"detail": token})}[
        wrapper
    ]
    leaks_token = token in _stored_error(message)
    assert not leaks_token


@pytest.mark.parametrize("size", [200, 256, 258, 300])
def test_long_jwt_header_preserves_base_redaction(size: int) -> None:
    token = _signed_jwt(json.dumps({"alg": "HS256", "pad": "x" * size}))
    leaks_token = token in _stored_error(token)
    assert not leaks_token


@pytest.mark.parametrize("size", [4000, 4096, 4100, 4200])
def test_long_jwt_payload_preserves_base_redaction(size: int) -> None:
    token = "eyJ" + "a" * 12 + "." + "b" * size + "." + "c" * 32
    stored = _stored_error(token)
    leaks_prefix = token[:100] in stored
    assert not leaks_prefix


@pytest.mark.parametrize("empty_key", [True, False])
@pytest.mark.parametrize("wrapper", ["bare", "glued", "prose", "json"])
def test_five_segment_jwe_preserves_base_redaction(empty_key: bool, wrapper: str) -> None:
    header = base64.urlsafe_b64encode(b'{"alg":"dir","enc":"A256GCM"}').rstrip(b"=").decode()
    token = ".".join((header, "" if empty_key else "a" * 16, "b" * 16, "c" * 16, "d" * 16))
    message = {"bare": token, "glued": "prefix" + token, "prose": "Provider rejected " + token, "json": json.dumps({"detail": token})}[
        wrapper
    ]
    leaks_token = token in _stored_error(message)
    assert not leaks_token


@pytest.mark.parametrize("length", [1, 2, 3])
@pytest.mark.parametrize("wrapper", ["bare", "glued", "prose", "json"])
def test_short_header_dotted_token_preserves_base_redaction(length: int, wrapper: str) -> None:
    token = "eyJ" + "a" * length + "." + "b" * 8 + "." + "c" * 8
    message = {"bare": token, "glued": "prefix" + token, "prose": "Provider rejected " + token, "json": json.dumps({"detail": token})}[
        wrapper
    ]
    leaks_token = token in _stored_error(message)
    assert not leaks_token


@pytest.mark.parametrize("scheme", ["postgresql", "mysql", "mongodb+srv"])
@pytest.mark.parametrize("special", ["$", "!", "#"])
def test_database_url_password_is_whole_redacted(scheme: str, special: str) -> None:
    password = "first" + special + "last"
    message = scheme + "://" + "user:" + password + "@db.example.test/data"
    finding = find_credential_material(message)
    assert finding is not None and finding.category == "credential_url"
    stored = _stored_error(message)
    leaked = password in stored
    has_fallback = "provider error detail redacted" in stored
    assert not leaked
    assert has_fallback


@pytest.mark.parametrize("detail", ["resets at 1731628800", "token count 1234567890", "req_0123456789abcdef"])
def test_ordinary_numbers_survive_in_provider_error(detail: str) -> None:
    preserved = _stored_error(detail) == detail
    assert preserved


def test_provider_error_keeps_email_and_ssn_redaction() -> None:
    message = "Contact " + "person@example.test" + "; identity " + "123-45-6789"
    stored = _stored_error(message)
    assert "<redacted-sensitive:email>" in stored
    assert "<redacted-sensitive:ssn_like>" in stored


def test_default_sensitive_redactor_still_redacts_phone() -> None:
    assert "<redacted-sensitive:phone>" in _redact_sensitive_content("Call 1234567890")


@pytest.mark.parametrize(
    "message",
    [
        "https://" + "user:secret@example.test/path",
        '{"url":"https://' + "user:secret@example.test/path" + '"}',
        "https://" + "user:pass%40word@example.test/path",
        "postgresql://" + "user:secret@example.test/db",
        "mongodb+srv://" + "user:secret@example.test/db",
        "https://example.test/path?" + "token=secretvalue",
        "https://example.test/path?" + "key=secretvalue",
        "https://example.test/path?" + "X-Amz-Signature=secretvalue",
        "https://example.test/path?" + "code=" + "a" * 32,
        "-----BEGIN " + "PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
        "Contact " + "person@example.test",
        "Identity " + "123-45-6789",
        "prefix" + "eyJ" + "a" * 8 + "." + "b" * 8 + "." + "c" * 8,
    ],
)
def test_old_sensitive_forms_never_survive_in_audit_detail(message: str) -> None:
    stored = _stored_error(message)
    assert message not in stored
    assert "raw_error_hash=" in stored


def test_old_pattern_oracle_preserves_redaction_except_dotted_fields() -> None:
    # Historical production patterns are a test-only regression oracle.
    old_patterns = (
        re.compile(r"\bAuthorization\s*[:=]\s*[^\s,;}]+(?:\s+[^\s,;}]+)?", re.I),
        re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}", re.I),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"ghp_[A-Za-z0-9]{36}"),
        re.compile(r"sk-ant-[A-Za-z0-9_-]{40,}"),
        re.compile(r"sk-[A-Za-z0-9]{40,}"),
        re.compile(r"\b[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"),
        re.compile(r"https?://[^\s'\"<>]+", re.I),
        re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"),
        re.compile(r"\b\d{3}[-\s]\d{2}[-\s]\d{4}\b"),
    )
    jwt = "eyJ" + "a" * 8 + "." + "b" * 8 + "." + "c" * 8
    pem = "-----BEGIN " + "PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----"
    corpus = (
        ("Field customer.address.street is missing", None),
        ("Contact " + "person@example.test", "person@example.test"),
        ("Identity " + "123-45-6789", "123-45-6789"),
        ("https://" + "user:secret@example.test/path", "user:secret"),
        ('{"url":"https://' + "user:secret@example.test/path" + '"}', "user:secret"),
        ("https://" + "user:pass%40word@example.test/path", "user:pass%40word"),
        ("HTTPS://" + "user:secret@example.test/path", "user:secret"),
        ("https://example.test/path?" + "token=secretvalue", "secretvalue"),
        ("https://example.test/path?" + "key=secretvalue", "secretvalue"),
        ("https://example.test/path?" + "X-Amz-Signature=secretvalue", "secretvalue"),
        ("https://example.test/path?" + "code=" + "a" * 32, "a" * 32),
        (pem, "secret\n-----END PRIVATE KEY-----"),
        (pem.replace("\n", "\\n"), "secret\\n-----END PRIVATE KEY-----"),
        ("Bearer " + "a" * 24, "a" * 24),
        ("x" + jwt, jwt),
        ("7" + jwt, jwt),
        ("_" + jwt, jwt),
    )
    for message, secret in corpus:
        old_detected = any(pattern.search(message) for pattern in old_patterns)
        stored = _stored_error(message)
        if secret is None:
            assert stored == message
        else:
            assert old_detected or message.startswith((pem, "-----BEGIN")) or jwt in message
            leaked = secret in stored
            assert not leaked


def test_base_pattern_oracle_over_dotted_token_corpus() -> None:
    # Reconstruct the base's ten patterns here; no git subprocess or fixture copy.
    base_patterns = (
        re.compile(r"\bAuthorization\s*[:=]\s*[^\s,;}]+(?:\s+[^\s,;}]+)?", re.I),
        re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}", re.I),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"ghp_[A-Za-z0-9]{36}"),
        re.compile(r"sk-ant-[A-Za-z0-9_-]{40,}"),
        re.compile(r"sk-[A-Za-z0-9]{40,}"),
        re.compile(r"\b[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"),
        re.compile(r"https?://[^\s'\"<>]+", re.I),
        re.compile(r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}\b"),
        re.compile(r"\b\d{3}[-\s]\d{2}[-\s]\d{4}\b"),
    )
    headers = [
        '{"alg":"HS256"}',
        '{\n "alg":"HS256"\n}',
        ' {"alg":"HS256"}',
        '\n{"alg":"HS256"}',
        '\t{"alg":"HS256"}',
        '\r\n{"alg":"HS256"}',
    ]
    seeds = [("jwt", _signed_jwt(header)) for header in headers]
    seeds.extend(("jwt", _signed_jwt(json.dumps({"alg": "HS256", "pad": "x" * size}))) for size in (200, 256, 258, 300))
    seeds.extend(("jwt", "eyJ" + "a" * 12 + "." + "b" * size + "." + "c" * 32) for size in (4000, 4096, 4100, 4200))
    jwe_header = base64.urlsafe_b64encode(b'{"alg":"dir","enc":"A256GCM"}').rstrip(b"=").decode()
    seeds.extend(("jwe", ".".join((jwe_header, key, "b" * 16, "c" * 16, "d" * 16))) for key in ("", "a" * 16))
    seeds.extend(("short", "eyJ" + "a" * length + "." + "b" * 8 + "." + "c" * 8) for length in range(1, 4))
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    rng = random.Random(26)
    seeds.extend(
        ("random", ".".join("".join(rng.choice(alphabet) for _ in range(length)) for _ in range(3)))
        for length in (4, 8, 16, 32, 64)
        for _ in range(4)
    )
    seeds.extend(
        ("ordinary", value) for value in ("customer.address.street", "module.package.function", "api.example.test", "model.v1.preview")
    )
    seeds.extend(("random", "AbCdEfGh.12AbCdEf.IjKlMnOp") for _ in range(5))
    assert len(seeds) == 48
    misses: list[str] = []
    for kind, token in seeds:
        for wrapper in ("bare", "glued", "prose", "json"):
            message = {
                "bare": token,
                "glued": "prefix" + token,
                "prose": "Provider rejected " + token,
                "json": json.dumps({"detail": token}),
            }[wrapper]
            base_changed = any(pattern.search(message) for pattern in base_patterns)
            lane_verbatim = _stored_error(message) == message
            if base_changed and lane_verbatim and kind != "ordinary":
                misses.append(kind + ":" + wrapper)
    assert not misses
