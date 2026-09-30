"""Response admission is independent of network and row extraction."""

import httpx
import pytest

from elspeth.plugins.transforms.web_scrape_response import ResponseContentError, admit_response_content


def _admit(response: httpx.Response, *, mode: str = "page", charset_policy: str = "declared_or_utf8"):
    return admit_response_content(
        response,
        mode=mode,
        page_format="raw",
        accepted_mime_types=(),
        charset_policy=charset_policy,
        max_decoded_bytes=1024,
    )


def test_declared_windows_charset_is_decoded_strictly() -> None:
    response = httpx.Response(200, content=b"caf\xe9", headers={"content-type": "text/plain; charset=windows-1252"})
    assert _admit(response).text == "café"
    with pytest.raises(ResponseContentError, match="UTF-8") as exc:
        _admit(response, charset_policy="utf8_only")
    assert exc.value.code == "invalid_charset"


@pytest.mark.parametrize("status", [204, 205, 206, 304])
def test_incomplete_or_not_modified_response_is_not_content(status: int) -> None:
    response = httpx.Response(status, content=b"partial", headers={"content-type": "text/plain"})
    with pytest.raises(ResponseContentError) as exc:
        _admit(response)
    assert exc.value.code == "unaccepted_status"


def test_invalid_utf8_is_not_replacement_decoded() -> None:
    response = httpx.Response(200, content=b"\xff", headers={"content-type": "text/plain; charset=utf-8"})
    with pytest.raises(ResponseContentError) as exc:
        _admit(response)
    assert exc.value.code == "invalid_charset"


@pytest.mark.parametrize("body", [b'{"n":NaN}', b"{bad", b"[] trailing", b'{"a":1,"a":2}'])
def test_json_mode_requires_strict_complete_document(body: bytes) -> None:
    response = httpx.Response(200, content=body, headers={"content-type": "application/json"})
    with pytest.raises(ResponseContentError) as exc:
        _admit(response, mode="json")
    assert exc.value.code == "invalid_json"


def test_xml_mode_rejects_entities_and_accepts_plain_xml() -> None:
    safe = httpx.Response(200, content=b"<result><name>Example</name></result>", headers={"content-type": "application/xml"})
    assert _admit(safe, mode="xml").text.startswith("<result>")
    hostile = httpx.Response(
        200,
        content=b'<!DOCTYPE r [<!ENTITY x "secret">]><r>&x;</r>',
        headers={"content-type": "application/xml"},
    )
    with pytest.raises(ResponseContentError) as exc:
        _admit(hostile, mode="xml")
    assert exc.value.code == "invalid_xml"


@pytest.mark.parametrize("charset", ["iso-8859-1", "windows-1252"])
def test_xml_mode_validates_text_in_declared_charset(charset: str) -> None:
    body = "<name>café</name>".encode(charset)
    response = httpx.Response(200, content=body, headers={"content-type": f"application/xml; charset={charset}"})
    assert _admit(response, mode="xml").text == "<name>café</name>"


def test_mime_policy_does_not_admit_binary_or_unknown_type() -> None:
    for media_type in ("application/octet-stream", "application/x-random"):
        response = httpx.Response(200, content=b"hello", headers={"content-type": media_type})
        with pytest.raises(ResponseContentError) as exc:
            _admit(response)
        assert exc.value.reason == "non_text_content_type"


def test_decoded_cap_still_checks_injected_responses() -> None:
    response = httpx.Response(200, content=b"a" * 1025, headers={"content-type": "text/plain"})
    with pytest.raises(ResponseContentError) as exc:
        _admit(response)
    assert exc.value.reason == "body_too_large"
