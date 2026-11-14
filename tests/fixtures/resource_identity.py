"""Non-vacuous UUID selector spellings shared by identity regressions."""

from uuid import UUID, uuid4

UUID_SPELLINGS = ("canonical", "uppercase", "mixed", "braced", "hyphenless", "urn")
PARK_UUID_SPELLINGS = (*UUID_SPELLINGS, "padded")
BLOB_UUID = "abcdefab-cdef-4abc-8def-abcdefabcdef"


def case_bearing_uuid() -> UUID:
    """Keep distinct UUID seeds while guaranteeing a case-changing selector."""
    return UUID("a" + str(uuid4())[1:])


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


def park_uuid_spelling(value: str, spelling: str) -> str:
    """Park selectors also admit surrounding whitespace, unlike blob selectors."""
    if spelling == "mixed":
        # Production-created park IDs are random: their first UUID segment
        # need not contain a letter. Always change an actual hex letter.
        letter = next(char for char in value if char in "abcdef")
        return value.replace(letter, letter.upper(), 1)
    return f" \t{value}\n" if spelling == "padded" else uuid_spelling(value, spelling)
