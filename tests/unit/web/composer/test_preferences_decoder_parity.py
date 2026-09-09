"""Python↔TypeScript parity for the composer-preferences exact-record decoder.

``src/elspeth/web/frontend/src/api/preferencesDecoder.ts`` declares ``KEYS``,
the exact set of properties the GET/PATCH payload may carry. The decoder fails
CLOSED in both directions: a missing key and an unexpected key are equally
fatal. That makes ``KEYS`` a closed cross-language contract with
``ComposerPreferences`` — and nothing executable tied the two together until
this test.

``ComposerPreferences`` is the authority, not
``UpdateComposerPreferencesRequest``. The request model is a partial form and
carries an eleventh field, ``tutorial_completed_via``, which is request-only
telemetry that is never persisted and never appears in a response. The decoder
reads responses, so comparing it against the request model would demand a key
the server never sends and fail on a correct tree.

Drift here ships green and breaks at the user. A developer who adds an
eleventh response field follows the covenant in ``web/preferences/models.py``,
touches no frontend file, and gets a green pytest run (no Python test read the
TS decoder) and a green vitest run (``preferencesDecoder.test.ts`` builds its
own local fixture). In production ``decodeUserComposerPreferences`` then throws
``unexpected <key>`` on every GET: ``preferencesStore.bootstrap`` catches it
and leaves ``defaultMode`` null, so every user sees the "Couldn't load your
preferences" alert, session creation degrades to forced-freeform, tutorial
resume is lost, and ``show_advanced`` is pinned false with no way to turn it
on. Deploy skew is not required — an atomic deploy whose frontend simply was
not rebuilt carries the old decoder.

Follows the parity pattern of ``test_graph_topology_parity.py`` and
``tests/unit/web/catalog/test_audit_characteristic_vocabulary_parity.py``:
regex one named TS declaration, guard that it matched exactly once, guard that
it yielded members at all, then compare against the Python authority.
"""

from __future__ import annotations

import re
from pathlib import Path

import elspeth
from elspeth.web.preferences.models import ComposerPreferences

_PACKAGE_ROOT = Path(elspeth.__file__).parent
_DECODER_PATH = _PACKAGE_ROOT / "web" / "frontend" / "src" / "api" / "preferencesDecoder.ts"

# Anchored on the named const. The `[^\]]*` body cannot span the closing
# bracket, so this matches exactly one declaration; `KEYS` is the only
# bracketed literal in the module (MODES and STAGES are Records, and
# `new Set(KEYS)` names the const rather than repeating its members).
_KEYS_DECLARATION_RE = re.compile(
    r"const\s+KEYS\s*=\s*\[(?P<body>[^\]]*)\]\s*as\s+const\s*;",
)
_MEMBER_RE = re.compile(r'"([^"]+)"')


def _ts_keys() -> set[str]:
    """Parse the `KEYS` declaration out of preferencesDecoder.ts.

    One helper, used by every assertion below — the sibling topology parity
    test grew two near-identical parsers and that duplication is not repeated
    here.
    """
    text = _DECODER_PATH.read_text(encoding="utf-8")
    matches = _KEYS_DECLARATION_RE.findall(text)
    assert len(matches) == 1, (
        f"Expected exactly one `KEYS` declaration in {_DECODER_PATH.name}, matched {len(matches)}. "
        "The declaration moved, was renamed, or Prettier rewrote its shape — re-anchor this regex "
        "rather than deleting the parity assertion, which is the only thing pinning the decoder's "
        "closed key set to `ComposerPreferences`."
    )
    return set(_MEMBER_RE.findall(matches[0]))


def test_decoder_path_resolves_and_the_regex_matches_real_members() -> None:
    """Smoke test: the anchor path resolves and the parse is not vacuous."""
    assert _DECODER_PATH.is_file(), f"Expected the preferences decoder at {_DECODER_PATH} — anchor path is wrong."
    assert _ts_keys(), (
        f"No members parsed from KEYS in {_DECODER_PATH}. The regex or the file format has drifted, "
        "and the parity assertion below would be vacuous."
    )


def test_decoder_keys_match_the_composer_preferences_field_set() -> None:
    py_fields = set(ComposerPreferences.model_fields)
    ts_keys = _ts_keys()
    assert py_fields, "No fields read from `ComposerPreferences` — the comparison below would be vacuous."

    missing_in_ts = py_fields - ts_keys
    missing_in_py = ts_keys - py_fields

    assert not missing_in_ts, (
        f"Fields on `ComposerPreferences` that {_DECODER_PATH.name} does not accept: "
        f"{sorted(missing_in_ts)}. `exactRecord` rejects an unexpected key, so the decoder throws on "
        "EVERY preferences GET the moment the server sends one of these — preferences fail to load "
        "for every user. Add them to KEYS and decode them in "
        "`decodeUserComposerPreferences`."
    )
    assert not missing_in_py, (
        f"Keys required by {_DECODER_PATH.name} that `ComposerPreferences` does not send: "
        f"{sorted(missing_in_py)}. `exactRecord` rejects a missing key just as hard, so the decoder "
        "throws on every GET. Either the field was removed from the response model and KEYS was not "
        "updated, or KEYS names a request-only field — `tutorial_completed_via` lives on "
        "`UpdateComposerPreferencesRequest` and is never part of a response."
    )


def test_the_decoder_reads_its_key_set_rather_than_restating_it() -> None:
    """`KEYS` is only load-bearing if `exactRecord` closes the record against it.

    Pinning the members alone would stay green if someone hand-rolled the
    property checks inside `exactRecord` and left KEYS as dead decoration —
    the decoder would then be exactly as unguarded as it was before this test
    existed, while this file reported parity.
    """
    text = _DECODER_PATH.read_text(encoding="utf-8")
    assert "for (const key of KEYS)" in text, (
        f"`exactRecord` in {_DECODER_PATH.name} no longer iterates KEYS to require every expected "
        "key. If the missing-key check is now derived some other way, that path needs its own pin "
        "to `ComposerPreferences`."
    )
    assert "new Set(KEYS)" in text, (
        f"`exactRecord` in {_DECODER_PATH.name} no longer builds its allowed-key set from KEYS. The "
        "unexpected-key rejection is what makes this a CLOSED contract; derived from anything else, "
        "the parity assertion above stops describing the shipped behaviour."
    )
