"""Runtime-rejection parity gate (elspeth-2ed41f0a4a).

Whole-tree gate: every raise site the composer's Stage-2 preflight can reach
(``core/dag/`` and ``core/config.py``) must carry a reviewed authoring-side
disposition in ``config/cicd/runtime_rejection_parity.yaml``. See the module
docstring of ``scripts/cicd/runtime_rejection_parity.py`` for the vocabulary
and the reason this exists.

WHAT TO DO WHEN THIS FAILS
--------------------------
* "NEW runtime rejection sites" — you added a runtime rule. Decide whether
  Stage 1 (``web/composer/state.py::CompositionState.validate()`` or a tool
  argument model) mirrors it. Run
  ``.venv/bin/python scripts/cicd/runtime_rejection_parity.py --write`` to
  seed the entry, then set its ``disposition`` (and ``counterpart``/``note``).
  A new ``unmirrored`` entry raises the ratchet — do that only with a ticket.
* "STALE baseline entries" — a rule moved or was reworded; ``--write`` drops
  the stale entry and seeds the new key; carry the adjudication across.
* "counterparts that are not a Stage-1 string literal" — a ``mirrored`` claim
  names an error_code that does not exist. Fix the claim, not the gate.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from scripts.cicd.runtime_rejection_parity import (
    BASELINE_PATH,
    REPO_ROOT,
    BaselineEntry,
    RaiseSite,
    enumerate_raise_sites,
    format_report,
    load_baseline,
    merge_baseline,
    render_baseline,
    stage1_error_code_literals,
    verify,
)

# Ratchet: the number of adjudicated ``unmirrored`` sites may only go DOWN.
# Raising it requires a ticket on the new entry AND a deliberate edit here.
# 10 = the 2026-08-17 census residue, tracked as elspeth-96e2dd023f (YAML
# bounds x3, llm profile lowering x3, secret fingerprinting x2, dsn x1,
# forgiven-field ancestor types x1 / elspeth-ae1410181c).
# 11 (2026-08-23, maintainer-ruled): + spec §7 rule 5 (ruling 28) undeclared-opener
# limb (bound_regions.py::validate_openers_bound_in_region, key 54e4545d6795d37c) —
# authorable in real YAML today (test_row_union_branch_cardinality.py's
# reclassified build-rejection test is the live proof), Stage 1 has zero rule-5
# predicate. Tracked as elspeth-239500195b (Stage-1 rule-5 mirror via probe
# plumbing); ratchet back to 10 when that ticket lands.
# 12 (2026-08-25, META-38 commit 3): + spec §7 rule 5 FORK arm (same function,
# key 8690ed0e601cb2fd) — an UNBOUND fork inside a bound region, the shape the
# falsifier built with the real builder; authorable (fork gates, scopes and
# collectors are NodeSpec-authorable since C4), Stage 1 still has zero rule-5
# predicate. Same ticket elspeth-239500195b; ratchet back to 10 when it lands.
# 14 (2026-08-26, elspeth-e6e552ce34): + schema_validation.py::
# validate_observed_producer_declared_types (key af293f6130ecdfd3) — a consumer
# declaring a concrete type for a field that provably arrives typed otherwise
# across an observed producer chain. The composer DOES cover this family, via
# the blocking preview proof arm declared_input_type_mismatch_against_source_
# schema (generation.py, edeb498b3) — but preview IS Stage-2 preflight, the very
# stage this gate measures, so that is coverage, not a mirror. Labelling the
# entry `mirrored` on its strength would have held the ratchet at 13 while making
# it the first of 154 mirrored entries to cite a proof arm instead of a Stage-1
# code, silently redefining what the gate measures. Stage 1 has no
# resolve_guaranteed_field_type and no structural source-type channel. Tracked as
# elspeth-98b238bb3c (Stage-1 mirror; shares the probe-plumbing need with
# elspeth-239500195b); ratchet back to 13 when it lands.
UNMIRRORED_CEILING = 14  # 13->14: observed-producer declared types (elspeth-e6e552ce34); ratchet back with elspeth-98b238bb3c


# --------------------------------------------------------------------------
# The whole-tree gate
# --------------------------------------------------------------------------


def test_every_runtime_rejection_site_has_a_reviewed_parity_disposition() -> None:
    live = enumerate_raise_sites()
    baseline = load_baseline()
    report = verify(live, baseline, stage1_literals=stage1_error_code_literals())

    assert report.clean, (
        "\n"
        + format_report(report)
        + "\n\n"
        + textwrap.dedent(
            """
        Refresh with `.venv/bin/python scripts/cicd/runtime_rejection_parity.py --write`,
        then adjudicate every `unadjudicated` entry (see the module docstring for the
        vocabulary). Never hand-edit a `key`.
        """
        )
    )


def test_unmirrored_runtime_rejections_are_ratcheted() -> None:
    baseline = load_baseline()
    unmirrored = [entry for entry in baseline if entry.disposition == "unmirrored"]
    described = "\n".join(f"  - {e.site.path}::{e.site.qualname} [{e.site.key}] {e.note}" for e in unmirrored)
    assert len(unmirrored) <= UNMIRRORED_CEILING, (
        f"{len(unmirrored)} unmirrored runtime rejection sites exceed the ratchet ({UNMIRRORED_CEILING}):\n{described}\n"
        "Mirror the predicate in Stage 1, or — with a ticket recorded in the entry's note — raise the ceiling deliberately."
    )


def test_baseline_file_is_canonically_rendered() -> None:
    """``--write`` output must be byte-stable so a refresh with no drift is a no-op diff."""
    baseline = load_baseline()
    rendered = render_baseline(merge_baseline(enumerate_raise_sites(), baseline))
    assert BASELINE_PATH.read_text(encoding="utf-8") == rendered, (
        "config/cicd/runtime_rejection_parity.yaml is not in canonical form; run "
        ".venv/bin/python scripts/cicd/runtime_rejection_parity.py --write and commit the result."
    )


# --------------------------------------------------------------------------
# Scanner semantics
# --------------------------------------------------------------------------


def _scan(tmp_path: Path, source: str, *, rel: str = "pkg/mod.py") -> tuple[RaiseSite, ...]:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(source), encoding="utf-8")
    return enumerate_raise_sites(tmp_path, roots=(rel,))


def test_key_ignores_line_numbers_and_whitespace(tmp_path: Path) -> None:
    compact = _scan(
        tmp_path,
        """
        class Settings:
            def validate(self, v):
                if v: raise ValueError(f"Gate name '{v}' is reserved")
        """,
    )
    spaced = _scan(
        tmp_path,
        """


        class Settings:

            def validate(self, v):

                if v:
                    raise ValueError(
                        f"Gate name '{v}' is reserved"
                    )
        """,
    )
    assert [s.key for s in compact] == [s.key for s in spaced]
    [site] = compact
    assert site.qualname == "Settings.validate"
    assert site.exception == "ValueError"
    assert site.message == "Gate name '{}' is reserved"


def test_dynamic_messages_get_ordinals_not_collisions(tmp_path: Path) -> None:
    sites = _scan(
        tmp_path,
        """
        def build(msg):
            if msg == 1:
                raise GraphValidationError(msg)
            if msg == 2:
                raise GraphValidationError(msg)
            raise GraphValidationError("literal " + describe(msg), component_id="x")
        """,
    )
    assert [(s.message, s.ordinal) for s in sites] == [
        ("<dynamic:msg>", 0),
        ("<dynamic:msg>", 1),
        ("literal <call:describe>", 0),
    ]
    assert len({s.key for s in sites}) == 3


def test_declarative_field_constraints_are_sites(tmp_path: Path) -> None:
    """``Field(min_length=..)`` rejects with no raise; the census must still see it."""
    sites = _scan(
        tmp_path,
        """
        class CoalesceSettings(BaseModel):
            branches: dict[str, str] = Field(min_length=2, description="x")
            merge: str = Field(default="union", description="no constraint")
            timeout_seconds: float | None = Field(default=None, gt=0)
        """,
    )
    assert [(s.qualname, s.exception, s.message) for s in sites] == [
        ("CoalesceSettings", "FieldConstraint", "branches: min_length"),
        ("CoalesceSettings", "FieldConstraint", "timeout_seconds: gt"),
    ]


def test_bare_reraise_is_not_a_site(tmp_path: Path) -> None:
    sites = _scan(
        tmp_path,
        """
        def f():
            try:
                g()
            except ValueError:
                raise
            raise KeyError("x")
        """,
    )
    assert [s.exception for s in sites] == ["KeyError"]


def test_merge_preserves_adjudication_by_key_and_seeds_new_sites(tmp_path: Path) -> None:
    before = _scan(
        tmp_path,
        """
        def f(v):
            raise ValueError("first")
        """,
    )
    adjudicated = (BaselineEntry(site=before[0], disposition="mirrored", counterpart=("some_code",), note=""),)
    after = _scan(
        tmp_path,
        """
        def f(v):
            raise ValueError("first")
            raise ValueError("second")
        """,
    )
    merged = merge_baseline(after, adjudicated)
    by_message = {e.site.message: e for e in merged}
    assert by_message["first"].disposition == "mirrored"
    assert by_message["first"].counterpart == ("some_code",)
    assert by_message["second"].disposition == "unadjudicated"


def test_verify_reports_every_defect_class(tmp_path: Path) -> None:
    live = _scan(
        tmp_path,
        """
        def f(v):
            raise ValueError("live-only")
            raise ValueError("kept")
            raise ValueError("bad-mirror")
            raise ValueError("no-note")
        """,
    )
    by_message = {s.message: s for s in live}
    stale_site = RaiseSite("pkg/mod.py", "gone", "ValueError", "gone", 0)
    baseline = (
        BaselineEntry(site=by_message["kept"], disposition="unadjudicated", counterpart=(), note=""),
        BaselineEntry(site=by_message["bad-mirror"], disposition="mirrored", counterpart=("not_a_code",), note=""),
        BaselineEntry(site=by_message["no-note"], disposition="unmirrored", counterpart=(), note=""),
        BaselineEntry(site=stale_site, disposition="structural", counterpart=(), note="n"),
    )
    report = verify(live, baseline, stage1_literals=frozenset({"real_code", "fn:real_fn"}))

    assert not report.clean
    assert [s.message for s in report.missing_from_baseline] == ["live-only"]
    assert [e.site.message for e in report.stale_in_baseline] == ["gone"]
    assert [e.site.message for e in report.unadjudicated] == ["kept"]
    assert [(e.site.message, code) for e, code in report.unknown_counterpart] == [("bad-mirror", "not_a_code")]
    assert [e.site.message for e in report.missing_note] == ["no-note"]
    assert [e.site.message for e in report.unmirrored] == ["no-note"]
    text = format_report(report)
    assert "live-only" in text and "not_a_code" in text


def test_counterpart_universe_accepts_literals_and_fn_names() -> None:
    literals = stage1_error_code_literals(REPO_ROOT)
    # A real Stage-1 code and a real tool-layer validator name resolve; an
    # invented code does not.
    assert "edge_field_type_incompatible" in literals
    assert "fn:validate_composer_output_name" in literals
    assert "definitely_not_a_real_error_code" not in literals


@pytest.mark.parametrize("path", [BASELINE_PATH])
def test_baseline_entries_carry_the_required_fields(path: Path) -> None:
    for entry in load_baseline(path):
        assert entry.disposition, entry.site
        if entry.disposition == "mirrored":
            assert entry.counterpart, f"mirrored without counterpart: {entry.site}"
        if entry.disposition in {"abstains", "structural", "not_authorable", "unmirrored"}:
            assert entry.note.strip(), f"{entry.disposition} without note: {entry.site}"
