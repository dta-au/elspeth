"""The observed-CSV gate sampler must never hand the evaluator a row the byte cut truncated.

``_sample_csv_rows`` receives the proof prefix (at most 8 KiB of a possibly
larger blob). A record the cut landed in is incomplete: it lacks trailing
fields the file does carry, so a correct gate condition would fail on it with
``missing_key``. The producer must drop exactly that record, and only when the
blob was actually cut.
"""

from __future__ import annotations

import pytest

from elspeth.web.composer.tools.generation import _sample_csv_rows

_HEADER = "order_id,customer,price\n"


def _sample(prefix: bytes, *, total: int | None = None, filename: str = "orders.csv", max_rows: int = 100) -> tuple[dict[str, str], ...]:
    return _sample_csv_rows(prefix, filename=filename, total_size_bytes=len(prefix) if total is None else total, max_rows=max_rows)


def _full_rows(rows: tuple[dict[str, str], ...], keys: tuple[str, ...] = ("order_id", "customer", "price")) -> bool:
    return all(tuple(row) == keys for row in rows)


def test_uncut_blob_keeps_a_final_row_without_a_terminator() -> None:
    rows = _sample(b"order_id,customer,price\nO-1,ann,9\nO-2,bob,7")
    assert [row["order_id"] for row in rows] == ["O-1", "O-2"]
    assert _full_rows(rows)


def test_cut_blob_drops_the_row_the_cut_landed_in() -> None:
    full = (_HEADER + "O-1,ann,9\nO-2,bob,7\nO-3,cyd,5\n").encode()
    cut = full.index(b"O-3,cyd") + len(b"O-3,c")  # inside the third data row
    rows = _sample(full[:cut], total=len(full))
    assert [row["order_id"] for row in rows] == ["O-1", "O-2"]
    assert _full_rows(rows)


def test_cut_on_a_record_boundary_keeps_every_complete_row() -> None:
    full = (_HEADER + "O-1,ann,9\nO-2,bob,7\nO-3,cyd,5\n").encode()
    cut = full.index(b"O-3,cyd")  # exactly after the second row's terminator
    rows = _sample(full[:cut], total=len(full))
    assert [row["order_id"] for row in rows] == ["O-1", "O-2"]


def test_cut_blob_with_exactly_one_complete_row_keeps_it() -> None:
    full = (_HEADER + "O-1,ann,9\nO-2,bob,7\n").encode()
    rows = _sample(full[: full.index(b"O-2") + 3], total=len(full))
    assert [row["order_id"] for row in rows] == ["O-1"]


def test_cut_blob_whose_only_data_row_is_cut_yields_no_rows() -> None:
    full = (_HEADER + "O-1,ann,9\n").encode()
    assert _sample(full[: full.index(b"O-1") + 2], total=len(full)) == ()


def test_header_longer_than_the_cut_yields_no_rows() -> None:
    full = ("a" * 40 + "," + "b" * 40 + "\nx,y\n").encode()
    assert _sample(full[:30], total=len(full)) == ()


def test_cut_inside_a_quoted_field_holding_a_newline_drops_that_record() -> None:
    full = (_HEADER + 'O-1,ann,9\nO-2,"bob\nsmith",7\n').encode()
    # The cut lands right after the newline INSIDE the quoted field: the text
    # ends with a newline, yet the record is still open.
    cut = full.index(b"smith")
    assert full[cut - 1 : cut] == b"\n"
    rows = _sample(full[:cut], total=len(full))
    assert [row["order_id"] for row in rows] == ["O-1"]


def test_quoted_newline_record_that_is_complete_is_kept() -> None:
    full = (_HEADER + 'O-1,ann,9\nO-2,"bob\nsmith",7\nO-3,cyd,5\n').encode()
    rows = _sample(full[: full.index(b"O-3") + 2], total=len(full))
    assert [row["order_id"] for row in rows] == ["O-1", "O-2"]
    assert rows[1]["customer"] == "bob\nsmith"


def test_crlf_cut_between_cr_and_lf_keeps_the_terminated_record() -> None:
    full = (_HEADER.replace("\n", "\r\n") + "O-1,ann,9\r\nO-2,bob,7\r\nO-3,cyd,5\r\n").encode()
    cut = full.index(b"O-2,bob,7") + len(b"O-2,bob,7\r")
    rows = _sample(full[:cut], total=len(full))
    assert [row["order_id"] for row in rows] == ["O-1", "O-2"]


def test_crlf_cut_mid_row_drops_that_row() -> None:
    full = (_HEADER.replace("\n", "\r\n") + "O-1,ann,9\r\nO-2,bob,7\r\n").encode()
    rows = _sample(full[: full.index(b"O-2,b") + 5], total=len(full))
    assert [row["order_id"] for row in rows] == ["O-1"]
    assert _full_rows(rows)


def test_cut_inside_a_multibyte_character_leaves_no_replacement_character() -> None:
    full = (_HEADER + "O-1,ann,9\nO-2,Zoë,7\n").encode()
    cut = full.index("ë".encode()) + 1  # one byte of a two-byte character
    rows = _sample(full[:cut], total=len(full))
    assert [row["order_id"] for row in rows] == ["O-1"]
    assert all("�" not in value for row in rows for value in row.values())


def test_a_ragged_row_that_is_not_the_cut_one_is_still_sampled_as_is() -> None:
    # Ragged rows are data, not a sampling artefact: only the cut is repaired here.
    rows = _sample(b"order_id,customer,price\nO-1,ann\nO-2,bob,7\n")
    assert [tuple(row) for row in rows] == [("order_id", "customer"), ("order_id", "customer", "price")]


def test_tsv_is_cut_aware_too() -> None:
    full = b"order_id\tcustomer\tprice\nO-1\tann\t9\nO-2\tbob\t7\n"
    rows = _sample(full[: full.index(b"O-2") + 6], total=len(full), filename="orders.tsv")
    assert [row["order_id"] for row in rows] == ["O-1"]
    assert _full_rows(rows)


def test_max_rows_bound_still_holds() -> None:
    body = "".join(f"O-{i},n,9\n" for i in range(300))
    rows = _sample((_HEADER + body).encode(), max_rows=100)
    assert len(rows) == 100


def test_large_blob_every_sampled_row_is_complete() -> None:
    # The reproduction behind the review's M1: >8 KiB, fewer than 100 rows in the
    # first 8 KiB, so the last sampled row is the one the byte cut truncated.
    full = (_HEADER + "".join(f"O-{i},{'n' * 150},9\n" for i in range(400))).encode()
    rows = _sample_csv_rows(full, filename="orders.csv", total_size_bytes=len(full))
    assert 0 < len(rows) < 100
    assert _full_rows(rows)


@pytest.mark.parametrize("cut", range(len(_HEADER) + 1, len(_HEADER) + 60))
def test_no_cut_offset_ever_yields_a_partial_row(cut: int) -> None:
    full = (_HEADER + "".join(f"O-{i},cust{i},9\n" for i in range(6))).encode()
    rows = _sample(full[:cut], total=len(full))
    assert _full_rows(rows)
