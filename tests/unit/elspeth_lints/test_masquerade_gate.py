"""Tests for the ``masquerade.attribute-probes`` gate (elspeth-b9ad1bbee3).

READ BEFORE TRUSTING THE LIVE-TREE ASSERTION IN THIS FILE: the gate
passing with zero findings over the real tree means "nothing NEW landed
outside the recognised amnesties and the seeded baseline" — it does NOT
mean the corpus is clean. Most baseline entries in
``config/cicd/masquerade_baseline.yaml`` carry ``classification:
unadjudicated``, which means NOT YET REVIEWED. See that file's header and
``docs/architecture/adr/032-validate-by-trust-domain.md`` for the full
rationale (attribute-masquerading probes are banned by *purpose*, not by
*construct*: sentinel-defaulted ``getattr`` at an external boundary is
prescribed, not banned).

Test order is deliberate and load-bearing, not decorative:

1. Anti-inert self-test — a synthetic tree with a fresh banned site MUST
   fire. AGENTS.md's documented failure mode is a gate that reports zero
   findings because its path filter or site-enumerator matched nothing
   (``elspeth-lints check`` once defaulted ``--rules`` to ``nothing`` and
   exited 0 on any tree; Wardline needs ``--fail-on-inert`` for the same
   reason).
2. Per-root coverage — the live scan must have visited files AND examined
   candidate sites in EVERY one of the four covered roots individually,
   not just in aggregate (a single global count would let three roots
   silently contribute nothing while one root carries the whole result).
3. Occurrence-count-drift — closing the collapse-identity hole: an
   already-baselined site's entry records how many non-amnestied
   occurrences it covers, and ANY divergence (increase OR decrease) must
   fire, not just growth past zero.
4. Everything else (fixtures, self-test pairing, qualname collisions,
   stale-entry detection).
5. Finally, the live-tree "zero findings" assertion — meaningful ONLY
   because every check above it already bites.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from elspeth_lints.core.ast_walker import walk_python_files
from elspeth_lints.rules.masquerade.baseline import BaselineEntry, MasqueradeBaseline, load_baseline, render_baseline_yaml
from elspeth_lints.rules.masquerade.inventory import compute_qualname, iter_masquerade_sites
from elspeth_lints.rules.masquerade.metadata import SCAN_SUBDIRS
from elspeth_lints.rules.masquerade.rule import RULE, collect_sites, group_non_amnestied_sites, scan_root
from elspeth_lints.rules.masquerade.seed_baseline import build_entries

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_self_test_a_fresh_banned_site_is_never_silently_green(tmp_path: Path) -> None:
    """Anti-inert guard: a synthetic tree with a brand-new banned site MUST fire.

    This is the load-bearing test in this module. If the gate's path
    filter, its scan-subdir list, or its site enumerator regresses to
    matching nothing, every OTHER test in this file (including the
    live-tree "zero findings" assertion below) would pass for the wrong
    reason — a gate that looked at nothing is indistinguishable, by
    finding-count alone, from a gate that looked at a clean tree.
    """
    (tmp_path / "src" / "elspeth").mkdir(parents=True)
    (tmp_path / "src" / "elspeth" / "fresh_probe.py").write_text(
        "def check(plugin):\n    return hasattr(plugin, 'run_batch')\n",
        encoding="utf-8",
    )

    findings = scan_root(tmp_path)

    assert len(findings) == 1
    assert findings[0].rule_id == "masquerade.attribute-probes"
    assert "hasattr" in findings[0].message
    assert "check" in findings[0].message


def test_self_test_goes_red_if_a_baseline_entry_covers_it_then_green(tmp_path: Path) -> None:
    """Confirms a baseline entry actually suppresses the finding it names.

    Paired with the previous test: together they prove the gate can both
    fire on an unbaselined site AND clear on an adjudicated one — not just
    always-fire or always-clear.
    """
    (tmp_path / "src" / "elspeth").mkdir(parents=True)
    (tmp_path / "src" / "elspeth" / "fresh_probe.py").write_text(
        "def check(plugin):\n    return hasattr(plugin, 'run_batch')\n",
        encoding="utf-8",
    )
    assert len(scan_root(tmp_path)) == 1

    (tmp_path / "config" / "cicd").mkdir(parents=True)
    entry = BaselineEntry(
        path="src/elspeth/fresh_probe.py",
        qualname="check",
        kind="hasattr",
        occurrences=1,
        classification="unadjudicated",
        justification="test fixture",
    )
    (tmp_path / "config" / "cicd" / "masquerade_baseline.yaml").write_text(
        render_baseline_yaml([entry]),
        encoding="utf-8",
    )

    assert scan_root(tmp_path) == []


def test_live_scan_visits_files_and_sites_in_every_covered_root() -> None:
    """Per-root non-emptiness guard.

    A path-filter regression can zero out one root while the other three
    keep contributing files and findings — a single aggregate count would
    hide that. Each of the four roots must independently show both a
    nonzero file count and a nonzero candidate-site count. A site-count
    check alone would also miss the case where a root's files are all
    walked but zero of them happen to be parseable Python; the file count
    is checked first and separately.
    """
    sites = collect_sites(REPO_ROOT)
    for subdir in SCAN_SUBDIRS:
        subroot = REPO_ROOT / subdir
        assert subroot.is_dir(), f"expected scan root {subroot} to exist"

        files_in_root = sum(1 for _ in walk_python_files(subroot))
        assert files_in_root > 0, f"{subdir}: zero files visited"

        sites_in_root = [site for site in sites if site.path == subdir or site.path.startswith(f"{subdir}/")]
        assert sites_in_root, f"{subdir}: zero candidate masquerade sites found"


def test_live_scan_visits_more_than_zero_files_and_sites_in_aggregate() -> None:
    """Coarse sanity check on top of the per-root assertions above."""
    files_visited = 0
    for subdir in SCAN_SUBDIRS:
        files_visited += sum(1 for _ in walk_python_files(REPO_ROOT / subdir))
    assert files_visited > 1000, "expected the four scan roots to contain well over 1000 Python files"

    sites = collect_sites(REPO_ROOT)
    assert len(sites) > 500, "expected several hundred candidate masquerade sites in the live tree"


def test_occurrence_count_drift_fires_when_a_probe_is_added(tmp_path: Path) -> None:
    """Closing the collapse-identity hole: adding a probe to an already-baselined site.

    Two ``getattr`` calls in one function baselined at ``occurrences: 2``;
    a third lands. The (path, qualname, kind) key is unchanged — a
    key-only comparison would stay green. The count must not.
    """
    (tmp_path / "src" / "elspeth").mkdir(parents=True)
    (tmp_path / "src" / "elspeth" / "boundary.py").write_text(
        "def admit(x):\n    a = getattr(x, 'a', None)\n    b = getattr(x, 'b', None)\n    c = getattr(x, 'c', None)\n    return a, b, c\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "cicd").mkdir(parents=True)
    entry = BaselineEntry(
        path="src/elspeth/boundary.py",
        qualname="admit",
        kind="getattr",
        occurrences=2,
        classification="unadjudicated",
        justification="test fixture: baselined at 2, tree now has 3",
    )
    (tmp_path / "config" / "cicd" / "masquerade_baseline.yaml").write_text(render_baseline_yaml([entry]), encoding="utf-8")

    findings = scan_root(tmp_path)

    assert len(findings) == 1
    assert "occurrence-count-drift" in findings[0].message
    assert "records occurrences: 2" in findings[0].message
    assert "currently has 3" in findings[0].message


def test_occurrence_count_drift_fires_when_a_probe_is_removed(tmp_path: Path) -> None:
    """The symmetric direction: a baselined count higher than what remains must also fire.

    Silently allowing a decrease would let the count drift inflated as
    sites are migrated away, re-opening room for it to creep back up
    unnoticed later.
    """
    (tmp_path / "src" / "elspeth").mkdir(parents=True)
    (tmp_path / "src" / "elspeth" / "boundary.py").write_text(
        "def admit(x):\n    return getattr(x, 'a', None)\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "cicd").mkdir(parents=True)
    entry = BaselineEntry(
        path="src/elspeth/boundary.py",
        qualname="admit",
        kind="getattr",
        occurrences=3,
        classification="unadjudicated",
        justification="test fixture: baselined at 3, tree now has 1",
    )
    (tmp_path / "config" / "cicd" / "masquerade_baseline.yaml").write_text(render_baseline_yaml([entry]), encoding="utf-8")

    findings = scan_root(tmp_path)

    assert len(findings) == 1
    assert "occurrence-count-drift" in findings[0].message
    assert "records occurrences: 3" in findings[0].message
    assert "currently has 1" in findings[0].message


def test_occurrence_count_drift_fires_when_every_occurrence_becomes_amnestied(tmp_path: Path) -> None:
    """A baselined site whose only occurrence gets refactored under a trust boundary.

    The (path, qualname, kind) key still exists in the tree (so this is
    NOT stale-baseline-entry), but zero non-amnestied occurrences remain.
    """
    (tmp_path / "src" / "elspeth").mkdir(parents=True)
    (tmp_path / "src" / "elspeth" / "boundary.py").write_text(
        "from elspeth.contracts.trust_boundary import trust_boundary\n"
        "\n"
        "\n"
        "@trust_boundary(tier=3, source='x', source_param='response', suppresses=())\n"
        "def admit(response):\n"
        "    return getattr(response, 'a', None)\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "cicd").mkdir(parents=True)
    entry = BaselineEntry(
        path="src/elspeth/boundary.py",
        qualname="admit",
        kind="getattr",
        occurrences=1,
        classification="unadjudicated",
        justification="test fixture: now fully amnestied",
    )
    (tmp_path / "config" / "cicd" / "masquerade_baseline.yaml").write_text(render_baseline_yaml([entry]), encoding="utf-8")

    findings = scan_root(tmp_path)

    assert len(findings) == 1
    assert "occurrence-count-drift" in findings[0].message
    assert "currently has 0" in findings[0].message


def test_matching_occurrence_count_is_clean(tmp_path: Path) -> None:
    (tmp_path / "src" / "elspeth").mkdir(parents=True)
    (tmp_path / "src" / "elspeth" / "boundary.py").write_text(
        "def admit(x):\n    a = getattr(x, 'a', None)\n    b = getattr(x, 'b', None)\n    return a, b\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "cicd").mkdir(parents=True)
    entry = BaselineEntry(
        path="src/elspeth/boundary.py",
        qualname="admit",
        kind="getattr",
        occurrences=2,
        classification="unadjudicated",
        justification="test fixture: matches",
    )
    (tmp_path / "config" / "cicd" / "masquerade_baseline.yaml").write_text(render_baseline_yaml([entry]), encoding="utf-8")

    assert scan_root(tmp_path) == []


def test_live_tree_has_zero_unbaselined_findings() -> None:
    """The CI-equivalent gate: every current site is amnestied or baselined.

    A green result here means "nothing new landed since the baseline was
    seeded" — see this module's docstring. It does NOT certify the
    corpus is defect-free; most entries are ``classification:
    unadjudicated``.
    """
    findings = scan_root(REPO_ROOT)
    assert findings == [], f"unadjudicated masquerade sites found: {[(f.file_path, f.line, f.message) for f in findings]}"


def test_qualname_distinguishes_same_named_methods_on_different_classes() -> None:
    """Blocking amendment A1: qualname must include ClassDef nesting.

    Two methods named ``bar`` on distinct classes ``Foo``/``Baz`` must not
    collapse to the same site identity — the scratch ``inventory2.py``
    instrument recorded only the innermost function name and dropped
    class scope, which this gate must not repeat.
    """
    tree = ast.parse(
        "class Foo:\n"
        "    def bar(self, x):\n"
        "        return getattr(x, 'a', None)\n"
        "\n"
        "class Baz:\n"
        "    def bar(self, x):\n"
        "        return getattr(x, 'b', None)\n"
    )
    sites = iter_masquerade_sites(tree, "src/elspeth/collision.py")
    qualnames = {site.qualname for site in sites}
    assert qualnames == {"Foo.bar", "Baz.bar"}


def test_module_level_site_uses_module_sentinel_qualname() -> None:
    tree = ast.parse("VALUE = getattr(_SOME_MODULE, 'thing', None)\n")
    sites = iter_masquerade_sites(tree, "src/elspeth/toplevel.py")
    assert len(sites) == 1
    assert sites[0].qualname == compute_qualname(())
    assert sites[0].qualname == "<module>"


def test_module_getattr_amnesty_requires_the_flat_gate_shape() -> None:
    """A module-level __getattr__ using elif (not flat sequential if) is NOT amnestied.

    This documents a deliberate, conservative gap: unrecognised shapes
    fall through to baseline-required, never to silently green (two real
    corpus sites — core/security and engine/orchestrator's __init__.py —
    exercise exactly this fallback; see seed_baseline.py's
    _KNOWN_CLASSIFICATIONS).
    """
    tree = ast.parse(
        "_A = ('x',)\n"
        "_B = ('y',)\n"
        "\n"
        "\n"
        "def __getattr__(name):\n"
        "    if name in _A:\n"
        "        return 1\n"
        "    elif name in _B:\n"
        "        return 2\n"
        "    else:\n"
        "        raise AttributeError(name)\n"
    )
    sites = iter_masquerade_sites(tree, "src/elspeth/elif_facade.py")
    dunder_sites = [site for site in sites if site.kind == "dunder_getattr"]
    assert len(dunder_sites) == 1
    assert dunder_sites[0].amnesty is False


def test_trust_boundary_amnesty_does_not_apply_to_a_non_source_param_receiver() -> None:
    tree = ast.parse(
        "from elspeth.contracts.trust_boundary import trust_boundary\n"
        "\n"
        "\n"
        "@trust_boundary(tier=3, source='x', source_param='response', suppresses=())\n"
        "def handler(response, other):\n"
        "    return getattr(other, 'status', None)\n"
    )
    sites = iter_masquerade_sites(tree, "src/elspeth/wrong_param.py")
    getattr_sites = [site for site in sites if site.kind == "getattr"]
    assert len(getattr_sites) == 1
    assert getattr_sites[0].amnesty is False


def test_baseline_flags_a_stale_entry_for_a_site_that_no_longer_exists(tmp_path: Path) -> None:
    (tmp_path / "src" / "elspeth").mkdir(parents=True)
    (tmp_path / "src" / "elspeth" / "gone.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "config" / "cicd").mkdir(parents=True)
    entry = BaselineEntry(
        path="src/elspeth/gone.py",
        qualname="deleted_function",
        kind="getattr",
        occurrences=1,
        classification="unadjudicated",
        justification="site no longer exists",
    )
    (tmp_path / "config" / "cicd" / "masquerade_baseline.yaml").write_text(
        render_baseline_yaml([entry]),
        encoding="utf-8",
    )

    findings = scan_root(tmp_path)

    assert len(findings) == 1
    assert "stale-baseline-entry" in findings[0].message
    assert findings[0].file_path == "config/cicd/masquerade_baseline.yaml"


def test_baseline_loader_treats_a_missing_file_as_empty(tmp_path: Path) -> None:
    baseline = load_baseline(tmp_path / "does" / "not" / "exist.yaml")
    assert baseline == MasqueradeBaseline(entries=())


def test_baseline_loader_rejects_a_non_positive_occurrences_value(tmp_path: Path) -> None:
    baseline_path = tmp_path / "masquerade_baseline.yaml"
    baseline_path.write_text(
        "entries:\n"
        "- path: src/elspeth/x.py\n"
        "  qualname: f\n"
        "  kind: getattr\n"
        "  occurrences: 0\n"
        "  classification: unadjudicated\n"
        "  justification: bad\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="occurrences must be an integer >= 1"):
        load_baseline(baseline_path)


def test_seeder_and_rule_agree_on_every_live_site_key_and_count() -> None:
    """Blocking amendment A1, checked directly (not just via the zero-findings result).

    Every non-amnestied live site the rule enumerates must appear in a
    freshly built baseline (``seed_baseline.build_entries``) under the
    identical key AND with the identical occurrence count, and vice versa.
    A silent divergence here would produce simultaneous missing-entry and
    stale-entry (or occurrence-drift) findings — the "worst outcome" the
    ticket's A1 amendment warns about — even if, by coincidence, the raw
    counts still matched in aggregate.
    """
    seeded = {entry.key: entry.occurrences for entry in build_entries(REPO_ROOT)}
    live_groups = {group.key: group.count for group in group_non_amnestied_sites(collect_sites(REPO_ROOT))}
    assert seeded == live_groups


def test_rule_is_registered_under_its_stable_id() -> None:
    assert RULE.id == "masquerade.attribute-probes"
    assert RULE.metadata.id == RULE.id


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        ("getattr(x, 'a', None)", "getattr"),
        ("hasattr(x, 'a')", "hasattr"),
        ("inspect.getattr_static(x, 'a', None)", "getattr_static"),
    ],
)
def test_each_call_kind_is_detected(source: str, expected_kind: str) -> None:
    tree = ast.parse(f"def f(x):\n    return {source}\n")
    sites = iter_masquerade_sites(tree, "src/elspeth/kinds.py")
    assert [site.kind for site in sites] == [expected_kind]


def test_dunder_getattr_def_is_detected() -> None:
    tree = ast.parse("class C:\n    def __getattr__(self, name):\n        raise AttributeError(name)\n")
    sites = iter_masquerade_sites(tree, "src/elspeth/dunder.py")
    assert [site.kind for site in sites] == ["dunder_getattr"]
    assert sites[0].qualname == "C.__getattr__"
