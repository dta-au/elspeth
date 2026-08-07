# tests/property/plugins/web_scrape/test_extraction_properties.py
"""Property-based tests for WebScrapeTransform extraction and fingerprinting.

Relocated from tests/unit/plugins/transforms/test_web_scrape_properties.py
to the correct property test directory.

Uses Hypothesis to verify invariants:
- Fingerprint determinism and collision resistance
- Normalization idempotency and whitespace collapse
- Content extraction preserves text, strips tags/scripts
- URL scheme validation
"""

import re
from typing import Any

from hypothesis import assume, given
from hypothesis.strategies import (
    booleans,
    characters,
    composite,
    integers,
    just,
    lists,
    one_of,
    sampled_from,
    text,
)

from elspeth.core.security.web import SSRFBlockedError, validate_url_for_ssrf, validate_url_scheme
from elspeth.plugins.transforms.web_scrape_extraction import extract_content
from elspeth.plugins.transforms.web_scrape_fingerprint import compute_fingerprint, normalize_for_fingerprint

# ============================================================================
# Custom Hypothesis Strategies
# ============================================================================


@composite
def valid_http_urls(draw: Any) -> str:
    """Generate valid HTTP/HTTPS URLs for testing."""
    scheme = draw(sampled_from(["http", "https"]))

    # Generate hostname (simple alphanumeric)
    hostname_parts = draw(
        lists(text(alphabet=characters(whitelist_categories=("Ll", "Nd")), min_size=1, max_size=8), min_size=1, max_size=3)
    )
    hostname = ".".join(hostname_parts) if hostname_parts else "example.com"

    # Ensure hostname is not empty and has valid structure
    if not hostname or hostname.startswith(".") or hostname.endswith("."):
        hostname = "example.com"

    # Optional path
    path = draw(one_of(just(""), text(alphabet=characters(whitelist_categories=("Ll", "Nd")), min_size=1, max_size=20)))
    if path and not path.startswith("/"):
        path = "/" + path

    return f"{scheme}://{hostname}{path}"


@composite
def forbidden_url_schemes(draw: Any) -> str:
    """Generate URLs with forbidden schemes (file://, ftp://, etc.)."""
    scheme = draw(sampled_from(["file", "ftp", "gopher", "data", "javascript"]))
    # Use alphanumeric path to avoid URL parsing issues
    path = draw(text(alphabet=characters(whitelist_categories=("Ll", "Nd")), min_size=1, max_size=20))

    # Ensure path is not empty after generation
    if not path:
        path = "example"

    return f"{scheme}://{path}"


@composite
def blocked_ip_addresses(draw: Any) -> str:
    """Generate IP addresses that should be blocked by SSRF prevention."""
    ip_type = draw(sampled_from(["loopback", "private_a", "private_b", "private_c", "metadata"]))

    if ip_type == "loopback":
        # 127.0.0.0/8
        return f"127.{draw(integers(0, 255))}.{draw(integers(0, 255))}.{draw(integers(0, 255))}"
    elif ip_type == "private_a":
        # 10.0.0.0/8
        return f"10.{draw(integers(0, 255))}.{draw(integers(0, 255))}.{draw(integers(0, 255))}"
    elif ip_type == "private_b":
        # 172.16.0.0/12
        return f"172.{draw(integers(16, 31))}.{draw(integers(0, 255))}.{draw(integers(0, 255))}"
    elif ip_type == "private_c":
        # 192.168.0.0/16
        return f"192.168.{draw(integers(0, 255))}.{draw(integers(0, 255))}"
    else:  # metadata
        # 169.254.0.0/16
        return f"169.254.{draw(integers(0, 255))}.{draw(integers(0, 255))}"


@composite
def public_ip_addresses(draw: Any) -> str:
    """Generate public IP addresses that should pass SSRF validation."""
    # Use well-known public IPs to avoid accidentally generating private ranges
    public_ips = [
        "8.8.8.8",  # Google DNS
        "1.1.1.1",  # Cloudflare DNS
        "208.67.222.222",  # OpenDNS
        "142.251.32.46",  # Google (example)
    ]
    result: str = draw(sampled_from(public_ips))
    return result


@composite
def html_documents(draw: Any) -> str:
    """Generate valid HTML documents for extraction testing."""
    # Generate simple HTML structure
    title = draw(text(min_size=1, max_size=50))
    body_content = draw(text(min_size=10, max_size=200))

    # Optional script/style tags (should be stripped)
    include_script = draw(booleans())
    include_style = draw(booleans())

    parts = ["<html><head>"]
    if include_script:
        parts.append("<script>console.log('test');</script>")
    if include_style:
        parts.append("<style>body { color: red; }</style>")
    parts.append(f"<title>{title}</title>")
    parts.append("</head><body>")
    parts.append(f"<p>{body_content}</p>")
    parts.append("</body></html>")

    return "".join(parts)


# ============================================================================
# Fingerprint Property Tests
# ============================================================================


@given(text(min_size=1, max_size=1000))
def test_fingerprint_deterministic_property(content: str):
    """Fingerprint must be deterministic: same input → same output."""
    fp1 = compute_fingerprint(content, mode="content")
    fp2 = compute_fingerprint(content, mode="content")

    assert fp1 == fp2, "Fingerprint is non-deterministic!"
    assert len(fp1) == 64, "SHA-256 fingerprint should be 64 hex chars"


@given(text(min_size=1, max_size=1000))
def test_normalize_idempotent_property(content: str):
    """Normalization must be idempotent: normalize(normalize(x)) = normalize(x)."""
    normalized_once = normalize_for_fingerprint(content)
    normalized_twice = normalize_for_fingerprint(normalized_once)

    assert normalized_once == normalized_twice, "Normalization is not idempotent!"


@given(text(min_size=1, max_size=500), text(min_size=1, max_size=500))
def test_fingerprint_collision_resistance(content1: str, content2: str):
    """Different content should produce different fingerprints (with high probability)."""
    assume(content1 != content2)  # Only test when inputs differ

    fp1 = compute_fingerprint(content1, mode="full")
    fp2 = compute_fingerprint(content2, mode="full")

    # SHA-256 collision is astronomically unlikely
    assert fp1 != fp2, f"Fingerprint collision detected: {content1!r} vs {content2!r}"


@given(text(min_size=10, max_size=200))
def test_content_mode_whitespace_invariant(content: str):
    """Content mode should ignore whitespace variations."""
    # Create variations with different whitespace
    variation1 = content
    variation2 = re.sub(r"\s+", "  ", content)  # Double spaces
    variation3 = re.sub(r"\s+", "\n", content)  # Newlines

    fp1 = compute_fingerprint(variation1, mode="content")
    fp2 = compute_fingerprint(variation2, mode="content")
    fp3 = compute_fingerprint(variation3, mode="content")

    assert fp1 == fp2 == fp3, "Content mode not whitespace-invariant"


# ============================================================================
# URL Validation Property Tests
# ============================================================================


@given(valid_http_urls())
def test_valid_urls_pass_validation(url: str):
    """Valid HTTP/HTTPS URLs should pass scheme validation."""
    # Should not raise
    validate_url_scheme(url)


@given(forbidden_url_schemes())
def test_forbidden_schemes_rejected(url: str):
    """URLs with forbidden schemes should raise SSRFBlockedError."""
    try:
        validate_url_scheme(url)
        raise AssertionError(f"Expected SSRFBlockedError for {url}")
    except SSRFBlockedError:
        pass  # Expected


@given(blocked_ip_addresses())
def test_blocked_ips_rejected(ip: str):
    """Private/loopback/metadata IPs should be blocked."""
    import socket
    from unittest.mock import patch

    is_ipv6 = ":" in ip

    def _mock_getaddrinfo(*args, **kwargs):
        if is_ipv6:
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (ip, 0, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    # Mock DNS resolution to return the blocked IP
    with patch("socket.getaddrinfo", side_effect=_mock_getaddrinfo):
        try:
            validate_url_for_ssrf("http://test.example.com/")
            raise AssertionError(f"Expected SSRFBlockedError for IP {ip}")
        except SSRFBlockedError as e:
            assert ip in str(e), f"Error message should mention blocked IP {ip}"


# ============================================================================
# Content Extraction Property Tests
# ============================================================================


@given(html_documents())
def test_extract_content_raw_preserves_input(html: str):
    """Raw format must preserve input exactly (identity function)."""
    result = extract_content(html, format="raw")
    assert result == html, "Raw format should be identity function"


@given(html_documents())
def test_extract_content_strips_script_tags(html: str):
    """Script tags should always be stripped when configured."""
    result = extract_content(html, format="text", strip_elements=["script"])

    # Result should not contain script content
    assert "<script>" not in result
    assert "console.log" not in result


@given(html_documents(), sampled_from(["markdown", "text"]))
def test_extract_content_produces_string(html: str, format: str):
    """Extraction must always return a string."""
    result = extract_content(html, format=format)

    assert isinstance(result, str), f"Expected string, got {type(result)}"
    # Non-empty HTML should produce non-empty result (unless all tags are stripped)
    # This is a weak assertion but catches catastrophic failures


@given(text(alphabet=characters(blacklist_characters="<>&"), min_size=10, max_size=100))
def test_extract_content_text_no_html_tags(content: str):
    """Text format should remove all HTML tags."""
    # Generate content without <, >, or & to avoid HTML entity ambiguity
    html = f"<html><body><p>{content}</p><div>More content</div></body></html>"

    result = extract_content(html, format="text")

    # No HTML tags should remain in result
    assert "<html>" not in result
    assert "<body>" not in result
    assert "<p>" not in result
    assert "<div>" not in result
    # Original content should be present, modulo whitespace normalisation — which
    # this docstring always intended but the old assertion could not express: it
    # tolerated only EDGE stripping, via an `or content.strip()` escape hatch.
    # In COMPACT mode (a text_separator carrying no CR/LF) extraction now collapses
    # record separators INSIDE a DOM text node into the separator, because that is
    # what makes the declared TextFraming.COMPACT true rather than merely intended
    # (elspeth-afdf55a17c). Compare on collapsed whitespace so the property pins
    # what extraction actually guarantees: every content CHARACTER survives.
    def _collapse(value: str) -> str:
        return " ".join(value.split())

    assert _collapse(content) in _collapse(result)


# ============================================================================
# Normalization Property Tests
# ============================================================================


@given(text(min_size=10, max_size=200))
def test_normalize_removes_leading_trailing_whitespace(content: str):
    """Normalization should strip leading/trailing whitespace."""
    normalized = normalize_for_fingerprint(content)

    assert not normalized.startswith(" ")
    assert not normalized.startswith("\t")
    assert not normalized.startswith("\n")
    assert not normalized.endswith(" ")
    assert not normalized.endswith("\t")
    assert not normalized.endswith("\n")


@given(text(min_size=10, max_size=200))
def test_normalize_collapses_whitespace_sequences(content: str):
    """Normalization should collapse whitespace sequences to single space."""
    normalized = normalize_for_fingerprint(content)

    # No consecutive spaces
    assert "  " not in normalized
    # No tabs
    assert "\t" not in normalized
    # No newlines
    assert "\n" not in normalized
    assert "\r" not in normalized


# ============================================================================
# Integration Property Tests
# ============================================================================


@given(html_documents())
def test_fingerprint_stable_across_extraction_formats(html: str):
    """Different extraction formats may produce different fingerprints."""
    # This is a documentation test - showing that format matters
    markdown = extract_content(html, format="markdown")
    text = extract_content(html, format="text")
    raw = extract_content(html, format="raw")

    fp_markdown = compute_fingerprint(markdown, mode="content")
    fp_text = compute_fingerprint(text, mode="content")
    fp_raw = compute_fingerprint(raw, mode="content")

    # All should be valid fingerprints (64 hex chars)
    assert len(fp_markdown) == 64
    assert len(fp_text) == 64
    assert len(fp_raw) == 64

    # They may or may not be equal (depends on content)
    # This just verifies the pipeline works


@given(text(min_size=10, max_size=100), text(min_size=10, max_size=100))
def test_different_content_different_fingerprints_property(content1: str, content2: str):
    """Property: Different normalized content → different fingerprints."""
    assume(normalize_for_fingerprint(content1) != normalize_for_fingerprint(content2))

    fp1 = compute_fingerprint(content1, mode="content")
    fp2 = compute_fingerprint(content2, mode="content")

    assert fp1 != fp2, "Different content should have different fingerprints"
