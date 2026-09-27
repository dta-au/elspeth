"""``SourceProtocol.field_renames`` keys a source's ``field_mapping`` the way its own loader keys it.

``resolve_field_names`` matches ``field_mapping`` keys against the NORMALIZED
header for a headered source and against the configured column names AS
WRITTEN for headerless CSV. The field-name spelling rule's build-time
resolution (``FieldNameResolution.of_source_renames``) reads this declaration,
so the keying each source publishes must be the one its loader applies — else
``columns: [Name]`` + ``field_mapping: {Name: b}`` lets a declaration ``Name``
past ``elspeth validate`` and the Composer (review-C1-alias-bypass-r1 F1).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from elspeth.contracts.field_spelling import DeclaredName, FieldNameResolution, SourceFieldRenames
from elspeth.plugins.sources.aws_s3_source import AWSS3Source
from elspeth.plugins.sources.azure_blob_source import AzureBlobSource
from elspeth.plugins.sources.csv_source import CSVSource
from elspeth.plugins.sources.json_source import JSONSource

_FAKE_CONN_STRING = "DefaultEndpointsProtocol=https;AccountName=fake;AccountKey=ZmFrZQ==;EndpointSuffix=core.windows.net"


def _csv(tmp_path: Path, **options: Any) -> CSVSource:
    return CSVSource({"path": str(tmp_path / "in.csv"), "schema": {"mode": "observed"}, "on_validation_failure": "discard", **options})


def _s3(**options: Any) -> AWSS3Source:
    return AWSS3Source({"bucket": "b", "key": "k.csv", "schema": {"mode": "observed"}, "on_validation_failure": "discard", **options})


def _azure(**options: Any) -> AzureBlobSource:
    return AzureBlobSource(
        {
            "connection_string": _FAKE_CONN_STRING,
            "container": "c",
            "blob_path": "k.csv",
            "schema": {"mode": "observed"},
            "on_validation_failure": "discard",
            **options,
        }
    )


_HEADERLESS = {"csv_options": {"has_header": False}, "columns": ["id", "Name"], "field_mapping": {"Name": "b"}}


@pytest.mark.parametrize(
    ("make", "expected"),
    [
        pytest.param(
            lambda tmp: _csv(tmp, field_mapping={"name": "b"}),
            SourceFieldRenames(mapping={"name": "b"}, keys="normalized"),
            id="csv-headered",
        ),
        pytest.param(
            lambda tmp: _csv(tmp, columns=["id", "Name"], field_mapping={"Name": "b"}),
            SourceFieldRenames(mapping={"Name": "b"}, keys="as_written"),
            id="csv-columns",
        ),
        pytest.param(lambda tmp: _csv(tmp), SourceFieldRenames(mapping={}, keys="normalized"), id="csv-no-mapping"),
        pytest.param(
            lambda tmp: _s3(field_mapping={"name": "b"}),
            SourceFieldRenames(mapping={"name": "b"}, keys="normalized"),
            id="s3-csv-headered",
        ),
        pytest.param(lambda tmp: _s3(**_HEADERLESS), SourceFieldRenames(mapping={"Name": "b"}, keys="as_written"), id="s3-csv-columns"),
        pytest.param(
            lambda tmp: _s3(csv_options={"has_header": False}, schema={"mode": "fixed", "fields": ["Name: str"]}),
            SourceFieldRenames(mapping={}, keys="as_written"),
            id="s3-csv-schema-names",
        ),
        pytest.param(
            lambda tmp: _s3(format="json", key="k.json", field_mapping={"name": "b"}),
            SourceFieldRenames(mapping={"name": "b"}, keys="normalized"),
            id="s3-json",
        ),
        pytest.param(
            lambda tmp: _azure(field_mapping={"name": "b"}),
            SourceFieldRenames(mapping={"name": "b"}, keys="normalized"),
            id="azure-csv-headered",
        ),
        pytest.param(
            lambda tmp: _azure(**_HEADERLESS), SourceFieldRenames(mapping={"Name": "b"}, keys="as_written"), id="azure-csv-columns"
        ),
        pytest.param(
            lambda tmp: _azure(format="json", blob_path="k.json", field_mapping={"name": "b"}),
            SourceFieldRenames(mapping={"name": "b"}, keys="normalized"),
            id="azure-json",
        ),
        pytest.param(
            lambda tmp: JSONSource(
                {
                    "path": str(tmp / "in.json"),
                    "schema": {"mode": "observed"},
                    "on_validation_failure": "discard",
                    "field_mapping": {"name": "b"},
                }
            ),
            SourceFieldRenames(mapping={"name": "b"}, keys="normalized"),
            id="json",
        ),
    ],
)
def test_each_source_publishes_its_renames_keyed_as_its_loader_keys_them(tmp_path: Path, make: Any, expected: SourceFieldRenames) -> None:
    source = make(tmp_path)
    try:
        assert source.field_renames == expected
    finally:
        source.close()


def test_a_headerless_column_resolves_as_written_and_a_case_variant_does_not(tmp_path: Path) -> None:
    """Under ``columns: [Name]`` + ``{Name: b}`` the literal ``Name`` names ``b``; ``name`` is not a key the source renames."""
    source = _csv(tmp_path, columns=["Name"], field_mapping={"Name": "b"})
    try:
        resolution = FieldNameResolution.of_source_renames([source.field_renames])
    finally:
        source.close()

    assert list(resolution.resolve(DeclaredName.of("Name"))) == [("b", "renamed_as_written"), ("name", "normalized")]
    assert list(resolution.resolve(DeclaredName.of("name"))) == [("name", "normalized")]


def test_a_headered_mapping_key_resolves_by_normalized_form_only(tmp_path: Path) -> None:
    """Under a header row ``{name: b}`` renames every header normalizing to ``name``; nothing is keyed as written."""
    source = _csv(tmp_path, field_mapping={"name": "b"})
    try:
        resolution = FieldNameResolution.of_source_renames([source.field_renames])
    finally:
        source.close()

    assert list(resolution.resolve(DeclaredName.of("Name"))) == [("b", "renamed"), ("name", "normalized")]
    assert resolution.renames_as_written == {}
