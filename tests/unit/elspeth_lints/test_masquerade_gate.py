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
4. Probe-shape drift, semantic import/shadow resolution, and boundary
   provenance invalidation — a same-count substitution or alias/rebinding
   must not create a false green.
5. Parse/read diagnostics — every independently scanned root fails closed.
6. Everything else (fixtures, self-test pairing, qualname collisions,
   stale-entry detection).
7. Finally, the live-tree "zero findings" assertion — meaningful ONLY
   because every check above it already bites.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from elspeth_lints.core.ast_walker import walk_python_files
from elspeth_lints.rules.masquerade.baseline import BaselineEntry, MasqueradeBaseline, load_baseline, render_baseline_yaml
from elspeth_lints.rules.masquerade.inventory import compute_qualname, iter_masquerade_sites
from elspeth_lints.rules.masquerade.metadata import SCAN_SUBDIRS
from elspeth_lints.rules.masquerade.rule import RULE, SiteGroup, collect_sites, group_non_amnestied_sites, scan_root
from elspeth_lints.rules.masquerade.seed_baseline import build_entries
from elspeth_lints.rules.masquerade.seed_baseline import main as seed_baseline_main

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _complete_synthetic_scan_layout(request: pytest.FixtureRequest) -> None:
    """Give ordinary tmp-path cases the same four-root layout as production."""
    if "tmp_path" not in request.fixturenames or request.node.name in {
        "test_scan_root_rejects_an_inert_repository_root",
        "test_scan_root_rejects_an_existing_but_empty_declared_root",
    }:
        return
    tmp_path = request.getfixturevalue("tmp_path")
    for subdir in SCAN_SUBDIRS:
        (tmp_path / subdir).mkdir(parents=True, exist_ok=True)


def _single_non_amnestied_group(source: str, path: str = "src/elspeth/probe.py") -> SiteGroup:
    groups = group_non_amnestied_sites(iter_masquerade_sites(ast.parse(source), path))
    assert len(groups) == 1
    return groups[0]


def test_self_test_a_fresh_banned_site_is_never_silently_green(tmp_path: Path) -> None:
    """Anti-inert guard: a synthetic tree with a brand-new banned site MUST fire.

    This is the load-bearing test in this module. If the gate's path
    filter, its scan-subdir list, or its site enumerator regresses to
    matching nothing, every OTHER test in this file (including the
    live-tree "zero findings" assertion below) would pass for the wrong
    reason — a gate that looked at nothing is indistinguishable, by
    finding-count alone, from a gate that looked at a clean tree.
    """
    (tmp_path / "src" / "elspeth").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "elspeth" / "fresh_probe.py").write_text(
        "def check(plugin):\n    return hasattr(plugin, 'run_batch')\n",
        encoding="utf-8",
    )

    findings = scan_root(tmp_path)

    assert len(findings) == 1
    assert findings[0].rule_id == "masquerade.attribute-probes"
    assert "hasattr" in findings[0].message
    assert "check" in findings[0].message
    suggestion = findings[0].suggestion
    assert suggestion is not None
    assert "seed_baseline" in suggestion
    assert "probe_shapes" in findings[0].message


def test_self_test_goes_red_if_a_baseline_entry_covers_it_then_green(tmp_path: Path) -> None:
    """Confirms a baseline entry actually suppresses the finding it names.

    Paired with the previous test: together they prove the gate can both
    fire on an unbaselined site AND clear on an adjudicated one — not just
    always-fire or always-clear.
    """
    (tmp_path / "src" / "elspeth").mkdir(parents=True, exist_ok=True)
    source = "def check(plugin):\n    return hasattr(plugin, 'run_batch')\n"
    (tmp_path / "src" / "elspeth" / "fresh_probe.py").write_text(source, encoding="utf-8")
    assert len(scan_root(tmp_path)) == 1

    (tmp_path / "config" / "cicd").mkdir(parents=True, exist_ok=True)
    group = _single_non_amnestied_group(source, "src/elspeth/fresh_probe.py")
    entry = BaselineEntry(
        path="src/elspeth/fresh_probe.py",
        qualname="check",
        kind="hasattr",
        occurrences=1,
        probe_shapes=group.probe_shapes,
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
    hide that. Each root must independently show a nonzero file count, and
    a nonzero candidate-site count wherever the baseline expects sites. A
    root whose baseline holds no entries may legitimately scan clean —
    scripts/ reached that state when 037ce6def eliminated its last probe —
    so for those roots the file count alone proves visitation; demanding a
    site there would make cleaning the final probe out of a root a test
    failure. A site-count check alone would also miss the case where a
    root's files are all walked but zero of them happen to be parseable
    Python; the file count is checked first and separately.
    """
    sites = collect_sites(REPO_ROOT)
    baseline = load_baseline(REPO_ROOT / "config" / "cicd" / "masquerade_baseline.yaml")
    for subdir in SCAN_SUBDIRS:
        subroot = REPO_ROOT / subdir
        assert subroot.is_dir(), f"expected scan root {subroot} to exist"

        files_in_root = sum(1 for _ in walk_python_files(subroot))
        assert files_in_root > 0, f"{subdir}: zero files visited"

        baseline_expects_sites = any(entry.path == subdir or entry.path.startswith(f"{subdir}/") for entry in baseline.entries)
        sites_in_root = [site for site in sites if site.path == subdir or site.path.startswith(f"{subdir}/")]
        if baseline_expects_sites:
            assert sites_in_root, f"{subdir}: zero candidate masquerade sites found where the baseline expects entries"


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
    (tmp_path / "src" / "elspeth").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "elspeth" / "boundary.py").write_text(
        "def admit(x):\n    a = getattr(x, 'a', None)\n    b = getattr(x, 'b', None)\n    c = getattr(x, 'c', None)\n    return a, b, c\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "cicd").mkdir(parents=True, exist_ok=True)
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
    (tmp_path / "src" / "elspeth").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "elspeth" / "boundary.py").write_text(
        "def admit(x):\n    return getattr(x, 'a', None)\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "cicd").mkdir(parents=True, exist_ok=True)
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
    (tmp_path / "src" / "elspeth").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "elspeth" / "boundary.py").write_text(
        "from elspeth.contracts.trust_boundary import trust_boundary\n"
        "\n"
        "\n"
        "@trust_boundary(tier=3, source='x', source_param='response', suppresses=())\n"
        "def admit(response):\n"
        "    return getattr(response, 'a', None)\n",
        encoding="utf-8",
    )
    (tmp_path / "config" / "cicd").mkdir(parents=True, exist_ok=True)
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
    suggestion = findings[0].suggestion
    assert suggestion is not None
    assert "Delete the entry" in suggestion
    assert "occurrences: 0" not in suggestion


def test_matching_occurrence_count_is_clean(tmp_path: Path) -> None:
    (tmp_path / "src" / "elspeth").mkdir(parents=True, exist_ok=True)
    source = "def admit(x):\n    a = getattr(x, 'a', None)\n    b = getattr(x, 'b', None)\n    return a, b\n"
    (tmp_path / "src" / "elspeth" / "boundary.py").write_text(source, encoding="utf-8")
    (tmp_path / "config" / "cicd").mkdir(parents=True, exist_ok=True)
    group = _single_non_amnestied_group(source, "src/elspeth/boundary.py")
    entry = BaselineEntry(
        path="src/elspeth/boundary.py",
        qualname="admit",
        kind="getattr",
        occurrences=2,
        probe_shapes=group.probe_shapes,
        classification="unadjudicated",
        justification="test fixture: matches",
    )
    (tmp_path / "config" / "cicd" / "masquerade_baseline.yaml").write_text(render_baseline_yaml([entry]), encoding="utf-8")

    assert scan_root(tmp_path) == []


def test_probe_shape_drift_fires_when_count_and_key_stay_unchanged(tmp_path: Path) -> None:
    """A one-for-one semantic substitution must invalidate the old adjudication."""
    source_path = tmp_path / "src" / "elspeth" / "probe.py"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    original = "def inspect_value(obj):\n    return getattr(obj, 'status', None)\n"
    source_path.write_text(original, encoding="utf-8")
    original_group = _single_non_amnestied_group(original)

    baseline_path = tmp_path / "config" / "cicd" / "masquerade_baseline.yaml"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        render_baseline_yaml(
            [
                BaselineEntry(
                    path=original_group.path,
                    qualname=original_group.qualname,
                    kind=original_group.kind,
                    occurrences=original_group.count,
                    probe_shapes=original_group.probe_shapes,
                    classification="approved-introspection",
                    justification="reviewed literal field admission",
                )
            ]
        ),
        encoding="utf-8",
    )
    assert scan_root(tmp_path) == []

    replacement = "def inspect_value(obj, name):\n    return getattr(obj, name, None)\n"
    source_path.write_text(replacement, encoding="utf-8")

    findings = scan_root(tmp_path)

    assert len(findings) == 1
    assert "probe-shape-drift" in findings[0].message


def test_probe_shape_evidence_is_stable_across_reformatting() -> None:
    compact = _single_non_amnestied_group("def f(obj):\n    return getattr(obj, 'status', None)\n")
    reformatted = _single_non_amnestied_group("def f(obj):\n    return getattr(\n        obj,\n        'status',\n        None,\n    )\n")

    assert compact.probe_shapes == reformatted.probe_shapes


def test_probe_shape_evidence_is_stable_across_equivalent_import_aliases() -> None:
    builtin = _single_non_amnestied_group("def f(obj):\n    return getattr(obj, 'status', None)\n")
    imported_alias = _single_non_amnestied_group(
        "from builtins import getattr as probe\ndef f(obj):\n    return probe(obj, 'status', None)\n"
    )

    assert builtin.probe_shapes == imported_alias.probe_shapes


def test_probe_shape_evidence_binds_each_occurrence_in_a_group(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "elspeth" / "probe.py"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    original = (
        "def inspect_value(obj):\n"
        "    first = getattr(obj, 'status', None)\n"
        "    second = getattr(obj, 'detail', None)\n"
        "    return first, second\n"
    )
    source_path.write_text(original, encoding="utf-8")
    original_group = _single_non_amnestied_group(original)
    baseline_path = tmp_path / "config" / "cicd" / "masquerade_baseline.yaml"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        render_baseline_yaml(
            [
                BaselineEntry(
                    path=original_group.path,
                    qualname=original_group.qualname,
                    kind=original_group.kind,
                    occurrences=original_group.count,
                    probe_shapes=original_group.probe_shapes,
                    classification="approved-introspection",
                    justification="reviewed two-call group",
                )
            ]
        ),
        encoding="utf-8",
    )

    replacement = original.replace("'detail'", "dynamic_name")
    source_path.write_text(replacement, encoding="utf-8")

    findings = scan_root(tmp_path)

    assert len(findings) == 1
    assert "probe-shape-drift" in findings[0].message


def test_seed_refresh_preserves_reviewed_metadata_for_an_unchanged_probe(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "elspeth" / "probe.py"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source = "def inspect_value(obj):\n    return getattr(obj, 'status', None)\n"
    source_path.write_text(source, encoding="utf-8")
    group = _single_non_amnestied_group(source)
    baseline_path = tmp_path / "config" / "cicd" / "masquerade_baseline.yaml"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        render_baseline_yaml(
            [
                BaselineEntry(
                    path=group.path,
                    qualname=group.qualname,
                    kind=group.kind,
                    occurrences=group.count,
                    probe_shapes=group.probe_shapes,
                    classification="approved-introspection",
                    justification="human-reviewed justification that must survive refresh",
                )
            ]
        ),
        encoding="utf-8",
    )

    refreshed = build_entries(tmp_path)

    assert len(refreshed) == 1
    assert refreshed[0].classification == "approved-introspection"
    assert refreshed[0].justification == "human-reviewed justification that must survive refresh"


def test_seed_refresh_does_not_carry_review_across_probe_shape_drift(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "elspeth" / "probe.py"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    original = "def inspect_value(obj):\n    return getattr(obj, 'status', None)\n"
    source_path.write_text(original, encoding="utf-8")
    group = _single_non_amnestied_group(original)
    baseline_path = tmp_path / "config" / "cicd" / "masquerade_baseline.yaml"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        render_baseline_yaml(
            [
                BaselineEntry(
                    path=group.path,
                    qualname=group.qualname,
                    kind=group.kind,
                    occurrences=group.count,
                    probe_shapes=group.probe_shapes,
                    classification="approved-introspection",
                    justification="review applies only to the literal field shape",
                )
            ]
        ),
        encoding="utf-8",
    )
    source_path.write_text("def inspect_value(obj, name):\n    return getattr(obj, name, None)\n", encoding="utf-8")

    refreshed = build_entries(tmp_path)

    assert len(refreshed) == 1
    assert refreshed[0].classification == "unadjudicated"
    assert refreshed[0].justification != "review applies only to the literal field shape"


def test_seed_refresh_does_not_carry_review_from_a_legacy_shape_unbound_entry(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "elspeth" / "probe.py"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source = "def inspect_value(obj):\n    return getattr(obj, 'status', None)\n"
    source_path.write_text(source, encoding="utf-8")
    group = _single_non_amnestied_group(source)
    baseline_path = tmp_path / "config" / "cicd" / "masquerade_baseline.yaml"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        render_baseline_yaml(
            [
                BaselineEntry(
                    path=group.path,
                    qualname=group.qualname,
                    kind=group.kind,
                    occurrences=group.count,
                    probe_shapes=(),
                    classification="approved-introspection",
                    justification="legacy review without shape evidence",
                )
            ]
        ),
        encoding="utf-8",
    )

    refreshed = build_entries(tmp_path)

    assert len(refreshed) == 1
    assert refreshed[0].classification == "unadjudicated"
    assert refreshed[0].justification != "legacy review without shape evidence"


def test_seed_check_accepts_an_unchanged_reviewed_ledger(tmp_path: Path) -> None:
    source_path = tmp_path / "src" / "elspeth" / "probe.py"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source = "def inspect_value(obj):\n    return getattr(obj, 'status', None)\n"
    source_path.write_text(source, encoding="utf-8")
    group = _single_non_amnestied_group(source)
    baseline_path = tmp_path / "config" / "cicd" / "masquerade_baseline.yaml"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        render_baseline_yaml(
            [
                BaselineEntry(
                    path=group.path,
                    qualname=group.qualname,
                    kind=group.kind,
                    occurrences=group.count,
                    probe_shapes=group.probe_shapes,
                    classification="approved-introspection",
                    justification="reviewed and unchanged",
                )
            ]
        ),
        encoding="utf-8",
    )

    assert seed_baseline_main(["--repo-root", str(tmp_path), "--check"]) == 0


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
    exercise exactly this fallback).
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
    (tmp_path / "src" / "elspeth").mkdir(parents=True, exist_ok=True)
    (tmp_path / "src" / "elspeth" / "gone.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "config" / "cicd").mkdir(parents=True, exist_ok=True)
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


def test_seeder_and_rule_agree_on_every_live_site_key_count_and_shape() -> None:
    """Blocking amendment A1, checked directly (not just via the zero-findings result).

    Every non-amnestied live site the rule enumerates must appear in a
    freshly built baseline (``seed_baseline.build_entries``) under the
    identical key AND with the identical occurrence count, and vice versa.
    A silent divergence here would produce simultaneous missing-entry and
    stale-entry (or occurrence-drift) findings — the "worst outcome" the
    ticket's A1 amendment warns about — even if, by coincidence, the raw
    counts still matched in aggregate.
    """
    seeded = {entry.key: (entry.occurrences, entry.probe_shapes) for entry in build_entries(REPO_ROOT)}
    live_groups = {group.key: (group.count, group.probe_shapes) for group in group_non_amnestied_sites(collect_sites(REPO_ROOT))}
    assert seeded == live_groups


def test_rule_is_registered_under_its_stable_id() -> None:
    assert RULE.id == "masquerade.attribute-probes"
    assert RULE.metadata.id == RULE.id


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        ("def f(x):\n    return getattr(x, 'a', None)\n", "getattr"),
        ("def f(x):\n    return hasattr(x, 'a')\n", "hasattr"),
        ("import inspect\ndef f(x):\n    return inspect.getattr_static(x, 'a', None)\n", "getattr_static"),
    ],
)
def test_each_call_kind_is_detected(source: str, expected_kind: str) -> None:
    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/kinds.py")
    assert [site.kind for site in sites] == [expected_kind]


def test_dunder_getattr_def_is_detected() -> None:
    tree = ast.parse("class C:\n    def __getattr__(self, name):\n        raise AttributeError(name)\n")
    sites = iter_masquerade_sites(tree, "src/elspeth/dunder.py")
    assert [site.kind for site in sites] == ["dunder_getattr"]
    assert sites[0].qualname == "C.__getattr__"


@pytest.mark.parametrize(
    ("source", "expected_kind"),
    [
        ("from builtins import getattr as probe\nprobe(obj, name)\n", "getattr"),
        ("import builtins as b\nb.getattr(obj, name)\n", "getattr"),
        ("from builtins import hasattr as probe\nprobe(obj, name)\n", "hasattr"),
        ("from inspect import getattr_static as probe\nprobe(obj, name)\n", "getattr_static"),
        ("import inspect as i\ni.getattr_static(obj, name)\n", "getattr_static"),
        ("from inspect import *\ngetattr_static(obj, name)\n", "getattr_static"),
    ],
)
def test_probe_calls_are_resolved_through_import_aliases(source: str, expected_kind: str) -> None:
    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/aliases.py")

    assert [site.kind for site in sites] == [expected_kind]


@pytest.mark.parametrize(
    "source",
    [
        "def f(getattr, obj):\n    return getattr(obj, 'field')\n",
        "def getattr(obj, name):\n    return 1\ngetattr(obj, 'field')\n",
        "from foreign_module import getattr\ngetattr(obj, 'field')\n",
        "adapter.getattr(obj, 'field')\n",
        "from builtins import getattr as probe\nprobe = adapter.get\nprobe(obj, 'field')\n",
    ],
)
def test_shadowed_or_foreign_probe_spellings_are_not_classified(source: str) -> None:
    assert iter_masquerade_sites(ast.parse(source), "src/elspeth/shadowed.py") == []


def test_probe_alias_join_records_any_possible_builtin_target() -> None:
    mixed = ast.parse(
        "if condition:\n"
        "    from builtins import getattr as probe\n"
        "else:\n"
        "    from foreign_module import getattr as probe\n"
        "probe(obj, 'field')\n"
    )
    identical = ast.parse(
        "if condition:\n    from builtins import getattr as probe\nelse:\n    from builtins import getattr as probe\nprobe(obj, 'field')\n"
    )

    assert [site.kind for site in iter_masquerade_sites(mixed, "src/elspeth/mixed.py")] == ["getattr"]
    assert [site.kind for site in iter_masquerade_sites(identical, "src/elspeth/identical.py")] == ["getattr"]


@pytest.mark.parametrize(
    "source",
    [
        "(probe := getattr)(obj, 'field', None)\n",
        "(probe,) = (getattr,)\nprobe(obj, 'field', None)\n",
        "probe = getattr if condition else adapter.get\nprobe(obj, 'field', None)\n",
        "for probe in (getattr,):\n    probe(obj, 'field', None)\n",
        "from math import *\ngetattr(obj, 'field', None)\n",
    ],
)
def test_builtin_probe_aliases_cannot_escape_through_expression_bindings(source: str) -> None:
    assert [site.kind for site in iter_masquerade_sites(ast.parse(source), "src/elspeth/expression_alias.py")] == ["getattr"]


@pytest.mark.parametrize(
    "source",
    [
        "table[getattr(obj, 'field')] = value\n",
        "getattr(repository, method_name).side_effect = failure\n",
        "table[getattr(obj, 'field')]: int = value\n",
        "table[getattr(obj, 'field')]: int\n",
        "for table[getattr(obj, 'field')] in rows:\n    pass\n",
        "async def run():\n    async for table[getattr(obj, 'field')] in rows:\n        pass\n",
        "with manager() as table[getattr(obj, 'field')]:\n    pass\n",
        "async def run():\n    async with manager() as table[getattr(obj, 'field')]:\n        pass\n",
        "values = [value for table[getattr(obj, 'field')] in rows]\n",
        "values = {value for table[getattr(obj, 'field')] in rows}\n",
        "values = {key: value for table[getattr(obj, 'field')] in rows}\n",
        "values = (value for table[getattr(obj, 'field')] in rows)\n",
        "async def run():\n    return [value async for table[getattr(obj, 'field')] in rows]\n",
    ],
)
def test_runtime_evaluated_store_targets_are_inventoried(source: str) -> None:
    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/store_target.py")

    assert [site.kind for site in sites] == ["getattr"]


def test_the_five_live_missed_store_target_shapes_are_all_inventoried() -> None:
    targets = (
        'trapped[getattr(cls, "name", cls.__name__)] = collisions',
        "misclassified[f\"{getattr(cls, 'name', cls.__name__)}.{key}\"] = sentinel",
        "unprotected[f\"{getattr(cls, 'name', cls.__name__)}.{key}\"] = value",
        "lost[f\"{getattr(cls, 'name', cls.__name__)}.{knob}\"] = collide",
        "getattr(auth_repository, repository_method).side_effect = failure",
    )

    builtin_sites = iter_masquerade_sites(ast.parse("\n".join(targets)), "tests/live_missed_targets.py")
    shadowed_sites = iter_masquerade_sites(
        ast.parse("from foreign import getattr\n" + "\n".join(targets)),
        "tests/shadowed_live_missed_targets.py",
    )

    assert [(site.kind, site.line) for site in builtin_sites] == [("getattr", line) for line in range(1, 6)]
    assert shadowed_sites == []


@pytest.mark.parametrize(
    ("source", "expected_kinds"),
    [
        (
            "from foreign import getattr as foreign_probe\ngetattr, table[getattr(obj, 'field')] = foreign_probe, value\n",
            [],
        ),
        (
            "from foreign import getattr\nimport builtins\ngetattr, table[getattr(obj, 'field')] = builtins.getattr, value\n",
            ["getattr"],
        ),
        (
            "from foreign import getattr as foreign_probe\n"
            "first[(probe := getattr)(obj, 'first')] = second[probe(obj, 'second')] = value\n",
            ["getattr", "getattr"],
        ),
    ],
)
def test_assignment_targets_apply_stores_in_runtime_order(source: str, expected_kinds: list[str]) -> None:
    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/store_order.py")

    assert [site.kind for site in sites] == expected_kinds


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize(
    "assignment",
    [
        "table[(probe := target_probe)] = alias = probe",
        "table[(probe := target_probe)], alias = 0, probe",
    ],
)
@pytest.mark.parametrize(
    ("initial_import", "target_import", "expected_kinds"),
    [
        ("from builtins import getattr as probe", "from foreign import getattr as target_probe", ["getattr"]),
        ("from foreign import getattr as probe", "from builtins import getattr as target_probe", []),
    ],
)
def test_assignment_rhs_probe_identity_is_captured_before_target_side_effects(
    deferred: bool,
    assignment: str,
    initial_import: str,
    target_import: str,
    expected_kinds: list[str],
) -> None:
    declaration = "def inspect():\n    return alias(obj, 'field')\n" if deferred else ""
    call = "" if deferred else "alias(obj, 'field')\n"
    source = f"{initial_import}\n{target_import}\n{declaration}{assignment}\n{call}"

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/rhs_capture.py")

    assert [site.kind for site in sites] == expected_kinds


@pytest.mark.parametrize(
    ("source", "expected_count"),
    [
        ("table[getattr(obj, 'lower'):getattr(obj, 'upper'):getattr(obj, 'step')] = value\n", 3),
        ("first, *rest, table[getattr(obj, 'field')] = values\n", 1),
        (
            "from foreign import getattr as probe\ntable[(probe := getattr)(obj, 'lower'):probe(obj, 'upper')] = value\n",
            2,
        ),
    ],
)
def test_store_target_subexpressions_are_visited_left_to_right(source: str, expected_count: int) -> None:
    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/store_shape.py")

    assert [site.kind for site in sites] == ["getattr"] * expected_count
    assert [(site.line, site.column) for site in sites] == sorted((site.line, site.column) for site in sites)


def test_each_with_target_precedes_the_next_context_expression() -> None:
    source = "with first() as table[getattr(obj, 'target')], getattr(obj, 'context'):\n    pass\n"

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/with_order.py")

    assert [(site.line, site.column) for site in sites] == [(1, 22), (1, 47)]


@pytest.mark.parametrize(
    "statement",
    [
        "table[(probe := getattr)(obj, 'target')] = value",
        "table[(probe := getattr)(obj, 'target')]: int = value",
        "for table[(probe := getattr)(obj, 'target')] in rows:\n    pass",
    ],
)
def test_store_target_walrus_is_visible_to_an_earlier_deferred_body(statement: str) -> None:
    source = f"from foreign import getattr as probe\ndef inspect():\n    return probe(obj, 'body')\n{statement}\n"

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/deferred_store_target.py")

    assert [site.kind for site in sites] == ["getattr", "getattr"]


def test_possible_builtin_alias_survives_an_abrupt_control_flow_join() -> None:
    source = (
        "from builtins import getattr as probe\n"
        "if condition:\n"
        "    from foreign import getattr as probe\n"
        "    raise SystemExit\n"
        "probe(obj, 'field', None)\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/abrupt_alias.py")

    assert [site.kind for site in sites] == ["getattr"]


def test_builtin_alias_on_an_abrupt_only_path_does_not_override_reachable_shadowing() -> None:
    source = (
        "if condition:\n"
        "    from builtins import getattr as probe\n"
        "    raise SystemExit\n"
        "else:\n"
        "    from foreign import getattr as probe\n"
        "probe(obj, 'field', None)\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/abrupt_only_alias.py")

    assert sites == []


def test_deferred_function_body_uses_the_runtime_module_binding() -> None:
    source = (
        "from foreign import getattr as probe\n"
        "def inspect_value(obj):\n"
        "    return probe(obj, 'field', None)\n"
        "from builtins import getattr as probe\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/deferred_alias.py")

    assert [site.kind for site in sites] == ["getattr"]


def test_deferred_lambda_body_uses_the_runtime_module_binding() -> None:
    source = (
        "from foreign import getattr as probe\n"
        "inspect_value = lambda obj: probe(obj, 'field', None)\n"
        "from builtins import getattr as probe\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/deferred_lambda_alias.py")

    assert [site.kind for site in sites] == ["getattr"]


@pytest.mark.parametrize(
    "source",
    [
        "def inspect_value(obj, probe=getattr):\n    return probe(obj, 'field', None)\n",
        "inspect_value = lambda obj, probe=getattr: probe(obj, 'field', None)\n",
    ],
)
def test_default_parameter_capture_preserves_builtin_probe_identity(source: str) -> None:
    assert [site.kind for site in iter_masquerade_sites(ast.parse(source), "src/elspeth/default_alias.py")] == ["getattr"]


def test_comprehension_target_preserves_builtin_probe_identity() -> None:
    source = "values = [probe(obj, 'field', None) for probe in (getattr,)]\n"

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/comprehension_alias.py")

    assert [site.kind for site in sites] == ["getattr"]


def test_match_capture_preserves_builtin_probe_identity() -> None:
    source = "match getattr:\n    case probe:\n        probe(obj, 'field', None)\n"

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/match_alias.py")

    assert [site.kind for site in sites] == ["getattr"]


@pytest.mark.parametrize(
    "source",
    [
        "for (probe,) in [(getattr,)]:\n    probe(obj, 'field', None)\n",
        "values = [probe(obj, 'field', None) for (probe,) in [(getattr,)]]\n",
        "match [getattr]:\n    case [probe]:\n        probe(obj, 'field', None)\n",
    ],
)
def test_destructured_control_flow_targets_preserve_builtin_probe_identity(source: str) -> None:
    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/destructured_alias.py")

    assert [site.kind for site in sites] == ["getattr"]


@pytest.mark.parametrize(
    "source",
    [
        "probe = {'p': getattr}['p']\nprobe(obj, 'field', None)\n",
        "for probe in {getattr: None}:\n    probe(obj, 'field', None)\n",
        "match {'p': getattr}:\n    case {'p': probe}:\n        probe(obj, 'field', None)\n",
    ],
)
def test_literal_mapping_transports_preserve_builtin_probe_identity(source: str) -> None:
    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/mapping_alias.py")

    assert [site.kind for site in sites] == ["getattr"]


def test_subscript_alias_preserves_builtin_probe_identity() -> None:
    source = "probe = (getattr,)[0]\nprobe(obj, 'field', None)\n"

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/subscript_alias.py")

    assert [site.kind for site in sites] == ["getattr"]


def test_nested_closure_sees_a_later_enclosing_probe_binding() -> None:
    source = (
        "def outer(obj):\n"
        "    def inspect_value():\n"
        "        return probe(obj, 'field', None)\n"
        "    probe = getattr\n"
        "    return inspect_value()\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/closure_alias.py")

    assert [site.kind for site in sites] == ["getattr"]


@pytest.mark.parametrize(
    "source",
    [
        (
            "from foreign import getattr as probe\n"
            "values = (probe(obj, 'field', None) for _ in range(1))\n"
            "from builtins import getattr as probe\n"
            "next(values)\n"
        ),
        (
            "def outer(obj):\n"
            "    from foreign import getattr as probe\n"
            "    values = (probe(obj, 'field', None) for _ in range(1))\n"
            "    from builtins import getattr as probe\n"
            "    return next(values)\n"
        ),
    ],
)
def test_generator_expression_sees_later_probe_bindings(source: str) -> None:
    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/generator_alias.py")

    assert [site.kind for site in sites] == ["getattr"]


def test_nested_lambda_sees_a_later_binding_in_its_enclosing_lambda() -> None:
    source = "from foreign import getattr as probe\nouter = lambda obj: ((lambda: probe(obj, 'field', None)), (probe := getattr))\n"

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/nested_lambda_alias.py")

    assert [site.kind for site in sites] == ["getattr"]


def test_future_module_named_expression_is_visible_to_a_deferred_body() -> None:
    source = (
        "from foreign import getattr as probe\n"
        "def inspect_value(obj):\n"
        "    return probe(obj, 'field', None)\n"
        "if (probe := getattr):\n"
        "    pass\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/future_walrus_alias.py")

    assert [site.kind for site in sites] == ["getattr"]


def test_deferred_body_retains_an_earlier_possible_module_binding() -> None:
    source = (
        "from builtins import getattr as probe\n"
        "def inspect_value(obj):\n"
        "    return probe(obj, 'field', None)\n"
        "inspect_value(obj)\n"
        "from foreign import getattr as probe\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/early_call_alias.py")

    assert [site.kind for site in sites] == ["getattr"]


def test_deleting_a_module_shadow_restores_builtin_lookup() -> None:
    source = "getattr = adapter.get\ndel getattr\ngetattr(obj, 'field', None)\n"

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/deleted_shadow.py")

    assert [site.kind for site in sites] == ["getattr"]


@pytest.mark.parametrize(
    "body",
    [
        "response = owned\n    return getattr(response, 'field', None)",
        "alias = response\n    alias = owned\n    return getattr(alias, 'field', None)",
        (
            "alias = response\n"
            "    if condition:\n"
            "        alias = response\n"
            "    else:\n"
            "        alias = owned\n"
            "    return getattr(alias, 'field', None)"
        ),
    ],
)
def test_boundary_source_amnesty_is_revoked_after_rebinding_or_a_mixed_join(body: str) -> None:
    source = (
        "from elspeth.contracts.trust_boundary import trust_boundary\n"
        "@trust_boundary(tier=3, source='x', source_param='response', suppresses=())\n"
        "def admit(response, owned, condition):\n"
        f"    {body}\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/rebound.py")

    assert len(sites) == 1
    assert sites[0].amnesty is False


def test_boundary_source_amnesty_survives_an_identical_control_flow_join() -> None:
    source = (
        "from elspeth.contracts.trust_boundary import trust_boundary\n"
        "@trust_boundary(tier=3, source='x', source_param='response', suppresses=())\n"
        "def admit(response, condition):\n"
        "    if condition:\n"
        "        alias = response\n"
        "    else:\n"
        "        alias = response\n"
        "    return getattr(alias, 'field', None)\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/joined.py")

    assert len(sites) == 1
    assert sites[0].amnesty is True


def test_boundary_source_reassignment_revokes_only_subsequent_amnesty() -> None:
    source = (
        "from elspeth.contracts.trust_boundary import trust_boundary\n"
        "@trust_boundary(tier=3, source='x', source_param='response', suppresses=())\n"
        "def admit(response, owned):\n"
        "    before = getattr(response, 'field', None)\n"
        "    response = owned\n"
        "    after = getattr(response, 'field', None)\n"
        "    return before, after\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/reassigned.py")

    assert [site.amnesty for site in sites] == [True, False]


def test_comprehension_target_shadowing_cannot_inherit_boundary_amnesty() -> None:
    source = (
        "from elspeth.contracts.trust_boundary import trust_boundary\n"
        "@trust_boundary(tier=3, source='x', source_param='response', suppresses=())\n"
        "def admit(response, rows):\n"
        "    values = [getattr(response, 'field', None) for response in rows]\n"
        "    after = getattr(response, 'field', None)\n"
        "    return values, after\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/comprehension.py")

    assert [site.amnesty for site in sites] == [False, True]


def test_comprehension_assignment_expression_revokes_boundary_amnesty_after_join() -> None:
    source = (
        "from elspeth.contracts.trust_boundary import trust_boundary\n"
        "@trust_boundary(tier=3, source='x', source_param='response', suppresses=())\n"
        "def admit(response, rows):\n"
        "    values = [(response := row) for row in rows]\n"
        "    return getattr(response, 'field', None), values\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/comprehension_walrus.py")

    assert len(sites) == 1
    assert sites[0].amnesty is False


def test_nested_lambda_cannot_inherit_boundary_source_amnesty() -> None:
    source = (
        "from elspeth.contracts.trust_boundary import trust_boundary\n"
        "@trust_boundary(tier=3, source='x', source_param='response', suppresses=())\n"
        "def admit(response, owned):\n"
        "    read_later = lambda: getattr(response, 'field', None)\n"
        "    response = owned\n"
        "    return read_later()\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/lambda_capture.py")

    assert len(sites) == 1
    assert sites[0].amnesty is False


def test_generator_expression_cannot_inherit_boundary_source_amnesty() -> None:
    source = (
        "from elspeth.contracts.trust_boundary import trust_boundary\n"
        "@trust_boundary(tier=3, source='x', source_param='response', suppresses=())\n"
        "def admit(response, owned):\n"
        "    values = (getattr(response, 'field', None) for _ in range(1))\n"
        "    response = owned\n"
        "    return next(values)\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/generator_capture.py")

    assert len(sites) == 1
    assert sites[0].amnesty is False


def test_type_alias_rebinding_revokes_boundary_source_amnesty() -> None:
    source = (
        "from elspeth.contracts.trust_boundary import trust_boundary\n"
        "@trust_boundary(tier=3, source='x', source_param='response', suppresses=())\n"
        "def admit(response):\n"
        "    type response = int\n"
        "    return getattr(response, 'field', None)\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/type_alias.py")

    assert len(sites) == 1
    assert sites[0].amnesty is False


def test_nested_function_cannot_inherit_boundary_decorator_authenticity() -> None:
    source = (
        "from elspeth.contracts.trust_boundary import trust_boundary as marker\n"
        "def factory():\n"
        "    @marker(tier=3, source='x', source_param='response', suppresses=())\n"
        "    def admit(response):\n"
        "        return getattr(response, 'field', None)\n"
        "    return admit\n"
        "from foreign import decorator as marker\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/nested_boundary.py")

    assert len(sites) == 1
    assert sites[0].amnesty is False


def test_outer_decorator_cannot_strip_boundary_authenticity_and_keep_amnesty() -> None:
    source = (
        "from inspect import unwrap\n"
        "from elspeth.contracts.trust_boundary import trust_boundary\n"
        "@unwrap\n"
        "@trust_boundary(tier=3, source='x', source_param='response', suppresses=())\n"
        "def admit(response):\n"
        "    return getattr(response, 'field', None)\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/stripped_boundary.py")

    assert len(sites) == 1
    assert sites[0].amnesty is False


def test_starred_default_is_not_a_trust_boundary_sentinel_amnesty() -> None:
    source = (
        "from elspeth.contracts.trust_boundary import trust_boundary\n"
        "@trust_boundary(tier=3, source='x', source_param='response', suppresses=())\n"
        "def admit(response, defaults):\n"
        "    return getattr(response, 'field', *defaults)\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/starred_default.py")

    assert len(sites) == 1
    assert sites[0].amnesty is False


@pytest.mark.parametrize(
    "mutation",
    [
        "_ALLOWED = runtime_names()",
        "_ALLOWED.add(user_name)",
    ],
)
def test_module_getattr_amnesty_rejects_a_mutated_closed_table(mutation: str) -> None:
    source = (
        "_ALLOWED = {'safe'}\n"
        f"{mutation}\n"
        "def __getattr__(name):\n"
        "    if name in _ALLOWED:\n"
        "        return expose(name)\n"
        "    raise AttributeError(name)\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/open_table.py")

    assert len(sites) == 1
    assert sites[0].kind == "dunder_getattr"
    assert sites[0].amnesty is False


@pytest.mark.parametrize(
    ("shadow", "raised"),
    [
        ("AttributeError = ForeignError", "AttributeError(name)"),
        ("builtins = foreign", "builtins.AttributeError(name)"),
    ],
)
def test_module_getattr_amnesty_rejects_shadowed_attribute_error(shadow: str, raised: str) -> None:
    source = (
        "_ALLOWED = {'safe'}\n"
        f"{shadow}\n"
        "def __getattr__(name):\n"
        "    if name in _ALLOWED:\n"
        "        return expose(name)\n"
        f"    raise {raised}\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/shadowed_error.py")

    assert len(sites) == 1
    assert sites[0].amnesty is False


def test_module_getattr_amnesty_requires_exclusive_attribute_error_identity() -> None:
    source = (
        "if condition:\n"
        "    AttributeError = RuntimeError\n"
        "_ALLOWED = {'safe'}\n"
        "def __getattr__(name):\n"
        "    if name in _ALLOWED:\n"
        "        return expose(name)\n"
        "    raise AttributeError(name)\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/conditional_error.py")

    assert len(sites) == 1
    assert sites[0].amnesty is False


def test_module_getattr_amnesty_rejects_a_decorated_hook() -> None:
    source = (
        "_ALLOWED = {'safe'}\n"
        "@evil\n"
        "def __getattr__(name):\n"
        "    if name in _ALLOWED:\n"
        "        return expose(name)\n"
        "    raise AttributeError(name)\n"
    )

    sites = iter_masquerade_sites(ast.parse(source), "src/elspeth/decorated_hook.py")

    assert len(sites) == 1
    assert sites[0].amnesty is False


@pytest.mark.parametrize("subdir", SCAN_SUBDIRS)
@pytest.mark.parametrize("failure", ["syntax", "read"])
def test_scan_root_surfaces_parse_and_read_failures_from_every_scanned_root(
    subdir: str,
    failure: str,
    tmp_path: Path,
) -> None:
    broken_path = tmp_path / subdir / f"broken_{failure}.py"
    broken_path.parent.mkdir(parents=True, exist_ok=True)
    if failure == "syntax":
        broken_path.write_text("def broken(:\n", encoding="utf-8")
    else:
        broken_path.write_bytes(b"\xff")

    findings = scan_root(tmp_path)

    expected_rule = "parse-error" if failure == "syntax" else "read-error"
    assert [finding.rule_id for finding in findings] == [expected_rule]
    assert findings[0].file_path == f"{subdir}/broken_{failure}.py"


@pytest.mark.parametrize("failure", ["syntax", "read"])
def test_scan_failure_does_not_misreport_the_failed_files_baseline_as_stale(
    failure: str,
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "src" / "elspeth" / "broken.py"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source = "def inspect_value(obj):\n    return getattr(obj, 'field', None)\n"
    source_path.write_text(source, encoding="utf-8")
    group = _single_non_amnestied_group(source, "src/elspeth/broken.py")
    baseline_path = tmp_path / "config" / "cicd" / "masquerade_baseline.yaml"
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        render_baseline_yaml(
            [
                BaselineEntry(
                    path=group.path,
                    qualname=group.qualname,
                    kind=group.kind,
                    occurrences=group.count,
                    probe_shapes=group.probe_shapes,
                    classification="approved-introspection",
                    justification="reviewed before the file became unreadable",
                )
            ]
        ),
        encoding="utf-8",
    )
    if failure == "syntax":
        source_path.write_text("def broken(:\n", encoding="utf-8")
    else:
        source_path.write_bytes(b"\xff")

    findings = scan_root(tmp_path)

    expected_rule = "parse-error" if failure == "syntax" else "read-error"
    assert [finding.rule_id for finding in findings] == [expected_rule]
    assert all("stale-baseline-entry" not in finding.message for finding in findings)


def test_scan_root_rejects_an_inert_repository_root(tmp_path: Path) -> None:
    findings = scan_root(tmp_path)

    assert len(findings) == 1
    assert findings[0].rule_id == "masquerade.attribute-probes"
    assert "inert-scan" in findings[0].message


def test_scan_root_rejects_an_existing_but_empty_declared_root(tmp_path: Path) -> None:
    (tmp_path / "src" / "elspeth").mkdir(parents=True, exist_ok=True)

    findings = scan_root(tmp_path)

    assert len(findings) == 1
    assert "inert-scan" in findings[0].message


def test_canonical_cli_root_surfaces_failures_from_the_rules_independent_roots(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from elspeth_lints.core.cli import main

    source_root = tmp_path / "src" / "elspeth"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "clean.py").write_text("VALUE = 1\n", encoding="utf-8")
    broken_path = tmp_path / "tests" / "broken.py"
    broken_path.parent.mkdir(parents=True, exist_ok=True)
    broken_path.write_text("def broken(:\n", encoding="utf-8")

    exit_code = main(
        [
            "check",
            "--rules",
            "masquerade.attribute-probes",
            "--root",
            str(source_root),
            "--repo-root",
            str(tmp_path),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    findings = json.loads(captured.out)
    assert [finding["rule_id"] for finding in findings] == ["parse-error"]
    assert findings[0]["file_path"] == "tests/broken.py"


def test_cli_does_not_duplicate_a_diagnostic_inside_its_explicit_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from elspeth_lints.core.cli import main

    broken_path = tmp_path / "src" / "elspeth" / "broken.py"
    broken_path.parent.mkdir(parents=True, exist_ok=True)
    broken_path.write_text("def broken(:\n", encoding="utf-8")

    exit_code = main(
        [
            "check",
            "--rules",
            "masquerade.attribute-probes",
            "--root",
            str(tmp_path),
            "--repo-root",
            str(tmp_path),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    findings = json.loads(captured.out)
    assert [finding["rule_id"] for finding in findings] == ["parse-error"]


def test_canonical_source_cli_root_does_not_suppress_its_own_diagnostic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from elspeth_lints.core.cli import main

    source_root = tmp_path / "src" / "elspeth"
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    exit_code = main(
        [
            "check",
            "--rules",
            "masquerade.attribute-probes",
            "--root",
            str(source_root),
            "--repo-root",
            str(tmp_path),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    findings = json.loads(captured.out)
    assert [finding["rule_id"] for finding in findings] == ["parse-error"]
    assert findings[0]["file_path"] == "src/elspeth/broken.py"


def test_cli_diagnostic_dedupe_does_not_collapse_same_suffix_from_different_roots(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from elspeth_lints.core.cli import main

    scan_root_path = tmp_path / "scan"
    repo_root_path = tmp_path / "repo"
    for root in (scan_root_path, repo_root_path):
        broken_path = root / "tests" / "broken.py"
        broken_path.parent.mkdir(parents=True, exist_ok=True)
        broken_path.write_text("def broken(:\n", encoding="utf-8")
    for subdir in SCAN_SUBDIRS:
        (repo_root_path / subdir).mkdir(parents=True, exist_ok=True)

    exit_code = main(
        [
            "check",
            "--rules",
            "masquerade.attribute-probes",
            "--root",
            str(scan_root_path),
            "--repo-root",
            str(repo_root_path),
            "--format",
            "json",
        ]
    )

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    findings = json.loads(captured.out)
    assert [finding["rule_id"] for finding in findings] == ["parse-error", "parse-error"]
    assert {finding["file_path"] for finding in findings} == {str(scan_root_path / "tests" / "broken.py"), "tests/broken.py"}
