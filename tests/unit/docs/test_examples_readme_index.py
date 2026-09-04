"""Regression checks for the shipped examples index."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
EXAMPLES_DIR = REPO_ROOT / "examples"
EXAMPLES_README = EXAMPLES_DIR / "README.md"

# Example directories may be exempted only when they are intentionally hidden from
# the public index; keep the set empty unless a concrete exception is approved.
EXAMPLE_INDEX_EXEMPTIONS: frozenset[str] = frozenset()


def _discover_indexable_examples() -> list[Path]:
    return sorted(example_dir for example_dir in EXAMPLES_DIR.iterdir() if example_dir.is_dir() and not example_dir.name.startswith("."))


def test_every_example_directory_is_indexed() -> None:
    indexable_examples = _discover_indexable_examples()
    indexable_names = {example_dir.name for example_dir in indexable_examples}
    unknown_exemptions = EXAMPLE_INDEX_EXEMPTIONS - indexable_names
    assert not unknown_exemptions, f"Unknown example index exemptions: {sorted(unknown_exemptions)}"

    index_text = EXAMPLES_README.read_text(encoding="utf-8")
    missing = [
        example_dir.name
        for example_dir in indexable_examples
        if example_dir.name not in EXAMPLE_INDEX_EXEMPTIONS and f"]({example_dir.name}/)" not in index_text
    ]

    assert not missing, f"Add these example directories to examples/README.md or explicitly exempt them in {Path(__file__).name}: {missing}"
