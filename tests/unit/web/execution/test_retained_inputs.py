"""Recovery reads the admitted source bytes even after the authored file moves."""

from pathlib import Path

import pytest

from elspeth.web.execution.retained_inputs import RetainedInputUnavailable, retain_source_file, verify_retained_input


def test_retained_source_survives_original_replacement(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    source.write_bytes(b"value\noriginal\n")
    retained = retain_source_file(source, root=tmp_path / "retained")
    source.write_bytes(b"value\nreplacement\n")
    verify_retained_input(retained)
    assert Path(retained.retained_path).read_bytes() == b"value\noriginal\n"
    assert retained.original_path == str(source)


def test_retained_corruption_refuses_instead_of_using_original(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    source.write_bytes(b"value\noriginal\n")
    retained = retain_source_file(source, root=tmp_path / "retained")
    Path(retained.retained_path).write_bytes(b"value\ncorrupt\n")
    with pytest.raises(RetainedInputUnavailable, match="retained_input_changed"):
        verify_retained_input(retained)


def test_same_source_is_content_addressed(tmp_path: Path) -> None:
    source = tmp_path / "input.csv"
    source.write_bytes(b"value\noriginal\n")
    first = retain_source_file(source, root=tmp_path / "retained")
    second = retain_source_file(source, root=tmp_path / "retained")
    assert first == second
    assert list((tmp_path / "retained").iterdir()) == [Path(first.retained_path)]
