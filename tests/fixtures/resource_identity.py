"""Non-vacuous UUID selector spellings shared by identity regressions."""

UUID_SPELLINGS = ("canonical", "uppercase", "mixed", "braced", "hyphenless", "urn")
BLOB_UUID = "abcdefab-cdef-4abc-8def-abcdefabcdef"


def uuid_spelling(value: str, spelling: str) -> str:
    """Spell an already canonical fixture UUID without changing its identity."""
    assert any(char in "abcdef" for char in value)
    return {
        "canonical": value,
        "uppercase": value.upper(),
        "mixed": value[:8].upper() + value[8:],
        "braced": "{" + value + "}",
        "hyphenless": value.replace("-", ""),
        "urn": "urn:uuid:" + value,
    }[spelling]
