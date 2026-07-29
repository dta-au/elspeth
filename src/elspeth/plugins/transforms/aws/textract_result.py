"""Pure validation and normalization for Amazon Textract analysis results."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.canonical import canonical_json


class MalformedTextractResponse(ValueError):
    """Raised when a provider response cannot be safely normalized."""

    def __init__(self, message: str = "malformed Amazon Textract response") -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class NormalizedTextractResult:
    """Validated normalized projections plus the bounded provider aggregate."""

    text: str
    page_count: int
    block_count: int
    pages: tuple[Mapping[str, Any], ...]
    tables: tuple[Mapping[str, Any], ...]
    forms: tuple[Mapping[str, Any], ...]
    queries: tuple[Mapping[str, Any], ...]
    signatures: tuple[Mapping[str, Any], ...]
    layout: tuple[Mapping[str, Any], ...]
    metadata: Mapping[str, Any]
    native_result: Mapping[str, Any]


def _malformed(path: str, detail: str) -> NoReturn:
    raise MalformedTextractResponse(f"{path}: {detail}")


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _malformed(path, f"must be an object, got {type(value).__name__}")
    return value


def _sequence(value: Any, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _malformed(path, f"must be a list, got {type(value).__name__}")
    return value


def _required_str(source: Mapping[str, Any], key: str, path: str, *, max_length: int | None = None) -> str:
    if key not in source:
        _malformed(path, f"missing required key {key}")
    value = source[key]
    if type(value) is not str or not value:
        _malformed(f"{path}.{key}", "must be a non-empty string")
    if max_length is not None and len(value) > max_length:
        _malformed(f"{path}.{key}", f"must contain at most {max_length} characters")
    return value


def _optional_str(source: Mapping[str, Any], key: str, path: str, *, max_length: int | None = None) -> str | None:
    if key not in source:
        return None
    return _required_str(source, key, path, max_length=max_length)


def _bounded_int(value: Any, path: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        bound = f" >= {minimum}" if maximum is None else f" in [{minimum}, {maximum}]"
        _malformed(path, f"must be an integer{bound}")
    return value


def _finite_float(value: Any, path: str, *, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        _malformed(path, f"must be numeric, got {type(value).__name__}")
    converted = float(value)
    if not math.isfinite(converted) or not minimum <= converted <= maximum:
        _malformed(path, f"must be finite and in [{minimum}, {maximum}]")
    return converted


def _geometry(block: Mapping[str, Any], path: str) -> Mapping[str, Any]:
    if "Geometry" not in block:
        return {}
    source = _mapping(block["Geometry"], f"{path}.Geometry")
    result: dict[str, Any] = {}
    if "BoundingBox" in source:
        bounding_box = _mapping(source["BoundingBox"], f"{path}.Geometry.BoundingBox")
        result["BoundingBox"] = {
            member: _finite_float(
                bounding_box.get(member),
                f"{path}.Geometry.BoundingBox.{member}",
                minimum=0.0,
                maximum=1.0,
            )
            for member in ("Width", "Height", "Left", "Top")
        }
    if "Polygon" in source:
        polygon = _sequence(source["Polygon"], f"{path}.Geometry.Polygon")
        result["Polygon"] = [
            {
                "X": _finite_float(
                    _mapping(point, f"{path}.Geometry.Polygon[{index}]").get("X"),
                    f"{path}.Geometry.Polygon[{index}].X",
                    minimum=0.0,
                    maximum=1.0,
                ),
                "Y": _finite_float(
                    _mapping(point, f"{path}.Geometry.Polygon[{index}]").get("Y"),
                    f"{path}.Geometry.Polygon[{index}].Y",
                    minimum=0.0,
                    maximum=1.0,
                ),
            }
            for index, point in enumerate(polygon)
        ]
    if "RotationAngle" in source:
        result["RotationAngle"] = _finite_float(
            source["RotationAngle"],
            f"{path}.Geometry.RotationAngle",
            minimum=0.0,
            maximum=360.0,
        )
    return result


def _relationships(block: Mapping[str, Any], path: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if "Relationships" not in block:
        return ()
    relationships = _sequence(block["Relationships"], f"{path}.Relationships")
    result: list[tuple[str, tuple[str, ...]]] = []
    for index, item in enumerate(relationships):
        relationship_path = f"{path}.Relationships[{index}]"
        relationship = _mapping(item, relationship_path)
        relationship_type = _required_str(relationship, "Type", relationship_path, max_length=128)
        raw_ids = _sequence(relationship.get("Ids"), f"{relationship_path}.Ids")
        ids: list[str] = []
        for id_index, raw_id in enumerate(raw_ids):
            if type(raw_id) is not str or not raw_id:
                _malformed(f"{relationship_path}.Ids[{id_index}]", "must be a non-empty string")
            ids.append(raw_id)
        result.append((relationship_type, tuple(ids)))
    return tuple(result)


def _child_ids(block: Mapping[str, Any], path: str, relationship_type: str = "CHILD") -> tuple[str, ...]:
    return tuple(block_id for candidate_type, ids in _relationships(block, path) if candidate_type == relationship_type for block_id in ids)


def _confidence(block: Mapping[str, Any], path: str) -> float:
    if "Confidence" not in block:
        _malformed(path, "missing required key Confidence")
    return _finite_float(block["Confidence"], f"{path}.Confidence", minimum=0.0, maximum=100.0)


def _page(block: Mapping[str, Any], path: str) -> int:
    if "Page" not in block:
        _malformed(path, "missing required key Page")
    return _bounded_int(block["Page"], f"{path}.Page")


def _warning(value: Any, path: str, page_count: int) -> tuple[dict[str, Any], dict[str, Any]]:
    source = _mapping(value, path)
    code = _required_str(source, "ErrorCode", path, max_length=128)
    raw_pages = _sequence(source.get("Pages"), f"{path}.Pages")
    pages = [_bounded_int(page, f"{path}.Pages[{index}]", maximum=page_count) for index, page in enumerate(raw_pages)]
    return {"ErrorCode": code, "Pages": pages}, {"error_code": code, "pages": pages}


def _validate_known_block_members(block: Mapping[str, Any], path: str) -> None:
    _required_str(block, "BlockType", path, max_length=128)
    _required_str(block, "Id", path, max_length=256)
    if "Page" in block:
        _page(block, path)
    if "Confidence" in block:
        _confidence(block, path)
    if "Geometry" in block:
        _geometry(block, path)
    if "Relationships" in block:
        _relationships(block, path)
    if "Text" in block and type(block["Text"]) is not str:
        _malformed(f"{path}.Text", "must be a string")


def _project_pages(blocks: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    numbered_blocks = list(enumerate(blocks))
    page_blocks = sorted(
        ((index, block) for index, block in numbered_blocks if block["BlockType"] == "PAGE"),
        key=lambda item: _page(item[1], f"Blocks[{item[0]}]"),
    )
    result: list[dict[str, Any]] = []
    for page_index, page_block in page_blocks:
        page_number = _page(page_block, f"Blocks[{page_index}]")
        lines: list[dict[str, Any]] = []
        for block_index, block in numbered_blocks:
            if block["BlockType"] != "LINE" or _page(block, f"Blocks[{block_index}]") != page_number:
                continue
            line_path = f"Blocks[{block_index}]"
            lines.append(
                {
                    "id": _required_str(block, "Id", line_path),
                    "text": _required_str(block, "Text", line_path),
                    "confidence": _confidence(block, line_path),
                    "geometry": _geometry(block, line_path),
                }
            )
        text = "\n".join(line["text"] for line in lines)
        result.append(
            {
                "page": page_number,
                "text": text,
                "geometry": _geometry(page_block, f"Blocks[{page_index}]"),
                "lines": lines,
            }
        )
    return tuple(result)


def _entity_types(block: Mapping[str, Any], path: str) -> list[str]:
    if "EntityTypes" not in block:
        return []
    raw_entity_types = _sequence(block["EntityTypes"], f"{path}.EntityTypes")
    entity_types: list[str] = []
    for index, value in enumerate(raw_entity_types):
        if type(value) is not str or not value:
            _malformed(f"{path}.EntityTypes[{index}]", "must be a non-empty string")
        entity_types.append(value)
    return entity_types


def _text_from_children(
    block: Mapping[str, Any],
    index: Mapping[str, Mapping[str, Any]],
    *,
    path: str,
    active_ids: frozenset[str] = frozenset(),
) -> str:
    block_id = _required_str(block, "Id", path)
    if block_id in active_ids:
        _malformed(path, f"cyclic CHILD relationship through block {block_id!r}")
    active = active_ids | {block_id}
    child_ids = _child_ids(block, path)
    if child_ids:
        parts: list[str] = []
        for child_id in child_ids:
            child = index[child_id]
            child_type = _required_str(child, "BlockType", f"block {child_id!r}")
            if child_type == "SELECTION_ELEMENT":
                continue
            child_text = _text_from_children(child, index, path=f"block {child_id!r}", active_ids=active)
            if child_text:
                parts.append(child_text)
        return " ".join(parts)
    text = block.get("Text", "")
    if type(text) is not str:
        _malformed(f"{path}.Text", "must be a string")
    return text


def _ordered_candidates(
    blocks: Sequence[Mapping[str, Any]],
    *,
    block_type: str | None = None,
    prefix: str | None = None,
) -> list[tuple[int, Mapping[str, Any]]]:
    candidates = [
        (position, block)
        for position, block in enumerate(blocks)
        if (block_type is not None and block["BlockType"] == block_type)
        or (prefix is not None and str(block["BlockType"]).startswith(prefix))
    ]
    return sorted(candidates, key=lambda item: (_page(item[1], f"Blocks[{item[0]}]"), item[0]))


def _validate_child_graph(index: Mapping[str, Mapping[str, Any]]) -> None:
    complete: set[str] = set()

    def visit(block_id: str, active: frozenset[str]) -> None:
        if block_id in active:
            _malformed(f"block {block_id!r}", "cyclic CHILD relationship")
        if block_id in complete:
            return
        block = index[block_id]
        next_active = active | {block_id}
        for child_id in _child_ids(block, f"block {block_id!r}"):
            visit(child_id, next_active)
        complete.add(block_id)

    for candidate_id in index:
        visit(candidate_id, frozenset())


def _project_tables(blocks: Sequence[Mapping[str, Any]], index: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for table_position, table in _ordered_candidates(blocks, block_type="TABLE"):
        table_path = f"Blocks[{table_position}]"
        table_page = _page(table, table_path)
        cells: list[dict[str, Any]] = []
        occupied: set[tuple[int, int]] = set()
        for child_id in _child_ids(table, table_path):
            cell = index[child_id]
            cell_type = _required_str(cell, "BlockType", f"block {child_id!r}")
            if cell_type not in {"CELL", "MERGED_CELL"}:
                _malformed(table_path, f"TABLE CHILD must target CELL or MERGED_CELL, got {cell_type}")
            cell_path = f"block {child_id!r}"
            if _page(cell, cell_path) != table_page:
                _malformed(cell_path, "cell Page must match its TABLE")
            coordinates: dict[str, int] = {}
            for member in ("RowIndex", "ColumnIndex", "RowSpan", "ColumnSpan"):
                if member not in cell:
                    _malformed(cell_path, f"missing required key {member}")
                coordinates[member] = _bounded_int(cell[member], f"{cell_path}.{member}")
            coordinate = (coordinates["RowIndex"], coordinates["ColumnIndex"])
            if coordinate in occupied:
                _malformed(table_path, f"duplicate cell coordinate {coordinate}")
            occupied.add(coordinate)

            selection_status: str | None = None
            for selection_id in _child_ids(cell, cell_path):
                selection = index[selection_id]
                if selection["BlockType"] != "SELECTION_ELEMENT":
                    continue
                if selection_status is not None:
                    _malformed(cell_path, "cell contains multiple selection elements")
                selection_status = _required_str(selection, "SelectionStatus", f"block {selection_id!r}")
                if selection_status not in {"SELECTED", "NOT_SELECTED"}:
                    _malformed(f"block {selection_id!r}.SelectionStatus", "must be SELECTED or NOT_SELECTED")

            cells.append(
                {
                    "id": child_id,
                    "row": coordinates["RowIndex"],
                    "column": coordinates["ColumnIndex"],
                    "row_span": coordinates["RowSpan"],
                    "column_span": coordinates["ColumnSpan"],
                    "text": _text_from_children(cell, index, path=cell_path),
                    "confidence": _confidence(cell, cell_path),
                    "geometry": _geometry(cell, cell_path),
                    "selection_status": selection_status,
                }
            )
        max_row = max((cell["row"] for cell in cells), default=0)
        rows: list[list[dict[str, Any]]] = [[] for _ in range(max_row)]
        for cell in cells:
            rows[cell["row"] - 1].append(cell)
        result.append(
            {
                "id": _required_str(table, "Id", table_path),
                "page": table_page,
                "confidence": _confidence(table, table_path),
                "geometry": _geometry(table, table_path),
                "entity_types": _entity_types(table, table_path),
                "rows": rows,
            }
        )
    return tuple(result)


def _project_forms(blocks: Sequence[Mapping[str, Any]], index: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for key_position, key_block in _ordered_candidates(blocks, block_type="KEY_VALUE_SET"):
        key_path = f"Blocks[{key_position}]"
        if "KEY" not in _entity_types(key_block, key_path):
            continue
        value_ids = _child_ids(key_block, key_path, "VALUE")
        if len(value_ids) > 1:
            _malformed(key_path, "KEY block must have at most one VALUE relationship target")

        value_block: Mapping[str, Any] | None = None
        value_id: str | None = None
        if value_ids:
            value_id = value_ids[0]
            value_block = index[value_id]
            value_path = f"block {value_id!r}"
            if value_block["BlockType"] != "KEY_VALUE_SET" or "VALUE" not in _entity_types(value_block, value_path):
                _malformed(key_path, "VALUE relationship must target a KEY_VALUE_SET VALUE block")
            if _page(value_block, value_path) != _page(key_block, key_path):
                _malformed(value_path, "VALUE Page must match its KEY")

        result.append(
            {
                "page": _page(key_block, key_path),
                "key": _text_from_children(key_block, index, path=key_path),
                "value": None if value_block is None else _text_from_children(value_block, index, path=f"block {value_id!r}"),
                "key_block_id": _required_str(key_block, "Id", key_path),
                "value_block_id": value_id,
                "key_confidence": _confidence(key_block, key_path),
                "value_confidence": None if value_block is None else _confidence(value_block, f"block {value_id!r}"),
                "key_geometry": _geometry(key_block, key_path),
                "value_geometry": None if value_block is None else _geometry(value_block, f"block {value_id!r}"),
            }
        )
    return tuple(result)


def _project_queries(blocks: Sequence[Mapping[str, Any]], index: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    result: list[dict[str, Any]] = []
    for query_position, query_block in _ordered_candidates(blocks, block_type="QUERY"):
        query_path = f"Blocks[{query_position}]"
        query = _mapping(query_block.get("Query"), f"{query_path}.Query")
        answer_ids = _child_ids(query_block, query_path, "ANSWER")
        if len(answer_ids) > 1:
            _malformed(query_path, "QUERY block must have at most one ANSWER relationship target")
        answer_block: Mapping[str, Any] | None = None
        answer_id: str | None = None
        if answer_ids:
            answer_id = answer_ids[0]
            answer_block = index[answer_id]
            if answer_block["BlockType"] != "QUERY_RESULT":
                _malformed(query_path, "ANSWER relationship must target a QUERY_RESULT block")
        result.append(
            {
                "page": _page(query_block, query_path),
                "query": _required_str(query, "Text", f"{query_path}.Query"),
                "alias": _optional_str(query, "Alias", f"{query_path}.Query"),
                "answer": None if answer_block is None else _required_str(answer_block, "Text", f"block {answer_id!r}"),
                "confidence": None if answer_block is None else _confidence(answer_block, f"block {answer_id!r}"),
                "query_block_id": _required_str(query_block, "Id", query_path),
                "answer_block_id": answer_id,
            }
        )
    return tuple(result)


def _project_signatures(blocks: Sequence[Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": _required_str(block, "Id", f"Blocks[{position}]"),
            "page": _page(block, f"Blocks[{position}]"),
            "confidence": _confidence(block, f"Blocks[{position}]"),
            "geometry": _geometry(block, f"Blocks[{position}]"),
        }
        for position, block in _ordered_candidates(blocks, block_type="SIGNATURE")
    )


def _project_layout(blocks: Sequence[Mapping[str, Any]], index: Mapping[str, Mapping[str, Any]]) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": _required_str(block, "Id", f"Blocks[{position}]"),
            "block_type": _required_str(block, "BlockType", f"Blocks[{position}]"),
            "page": _page(block, f"Blocks[{position}]"),
            "text": _text_from_children(block, index, path=f"Blocks[{position}]"),
            "confidence": _confidence(block, f"Blocks[{position}]"),
            "geometry": _geometry(block, f"Blocks[{position}]"),
        }
        for position, block in _ordered_candidates(blocks, prefix="LAYOUT_")
    )


def normalize_textract_result(
    *,
    job_id: str,
    result_pages: Sequence[Mapping[str, Any]],
    feature_types: tuple[str, ...],
    s3_version: str | None,
    max_blocks: int,
    max_result_bytes: int,
) -> NormalizedTextractResult:
    """Validate a complete SUCCEEDED response sequence and normalize it."""
    if not result_pages:
        _malformed("result_pages", "must contain at least one response")

    page_count: int | None = None
    model_version: str | None = None
    blocks: list[Mapping[str, Any]] = []
    native_warnings: list[dict[str, Any]] = []
    metadata_warnings: list[dict[str, Any]] = []

    for result_index, raw_result in enumerate(result_pages):
        result_path = f"result_pages[{result_index}]"
        result = _mapping(raw_result, result_path)
        status = _required_str(result, "JobStatus", result_path)
        if status != "SUCCEEDED":
            _malformed(f"{result_path}.JobStatus", "must be SUCCEEDED")
        document_metadata = _mapping(result.get("DocumentMetadata"), f"{result_path}.DocumentMetadata")
        if "Pages" not in document_metadata:
            _malformed(f"{result_path}.DocumentMetadata", "missing required key Pages")
        candidate_page_count = _bounded_int(document_metadata["Pages"], f"{result_path}.DocumentMetadata.Pages")
        candidate_model_version = _required_str(result, "AnalyzeDocumentModelVersion", result_path, max_length=128)
        if page_count is None:
            page_count = candidate_page_count
            model_version = candidate_model_version
        elif candidate_page_count != page_count:
            _malformed(f"{result_path}.DocumentMetadata.Pages", "page count changed across result pages")
        elif candidate_model_version != model_version:
            _malformed(f"{result_path}.AnalyzeDocumentModelVersion", "model version changed across result pages")

        raw_blocks = _sequence(result.get("Blocks"), f"{result_path}.Blocks")
        for block_offset, raw_block in enumerate(raw_blocks):
            block = _mapping(raw_block, f"{result_path}.Blocks[{block_offset}]")
            blocks.append(block)
            if len(blocks) > max_blocks:
                _malformed("Blocks", f"block count exceeds max_blocks={max_blocks}")

        if "Warnings" in result:
            raw_warnings = _sequence(result["Warnings"], f"{result_path}.Warnings")
            for warning_index, raw_warning in enumerate(raw_warnings):
                native_warning, metadata_warning = _warning(
                    raw_warning,
                    f"{result_path}.Warnings[{warning_index}]",
                    candidate_page_count,
                )
                native_warnings.append(native_warning)
                metadata_warnings.append(metadata_warning)

    assert page_count is not None
    assert model_version is not None

    block_index: dict[str, Mapping[str, Any]] = {}
    for index, block in enumerate(blocks):
        path = f"Blocks[{index}]"
        _validate_known_block_members(block, path)
        block_id = _required_str(block, "Id", path)
        if block_id in block_index:
            _malformed(path, f"duplicate block id {block_id!r}")
        block_index[block_id] = block

    for index, block in enumerate(blocks):
        for _relationship_type, ids in _relationships(block, f"Blocks[{index}]"):
            for related_id in ids:
                if related_id not in block_index:
                    _malformed(f"Blocks[{index}].Relationships", f"dangling relationship to {related_id!r}")

    _validate_child_graph(block_index)

    page_block_numbers = [_page(block, f"Blocks[{index}]") for index, block in enumerate(blocks) if block["BlockType"] == "PAGE"]
    if sorted(page_block_numbers) != list(range(1, page_count + 1)):
        _malformed("Blocks", "PAGE block numbering does not match DocumentMetadata.Pages")

    copied_blocks = [deep_thaw(block) for block in blocks]
    native_result: dict[str, Any] = {
        "JobStatus": "SUCCEEDED",
        "DocumentMetadata": {"Pages": page_count},
        "AnalyzeDocumentModelVersion": model_version,
        "Warnings": native_warnings,
        "Blocks": copied_blocks,
    }
    try:
        result_size = len(canonical_json(native_result).encode("utf-8"))
    except (TypeError, ValueError) as error:
        raise MalformedTextractResponse("native result contains a non-canonical value") from error
    if result_size > max_result_bytes:
        _malformed("native_result", f"serialized result exceeds max_result_bytes={max_result_bytes}")

    pages = _project_pages(blocks)
    tables = _project_tables(blocks, block_index)
    forms = _project_forms(blocks, block_index)
    queries = _project_queries(blocks, block_index)
    signatures = _project_signatures(blocks)
    layout = _project_layout(blocks, block_index)
    text = "\n\f\n".join(page["text"] for page in pages)
    metadata = {
        "job_id": job_id,
        "job_status": "SUCCEEDED",
        "page_count": page_count,
        "block_count": len(blocks),
        "model_version": model_version,
        "warnings": metadata_warnings,
        "feature_types": list(feature_types),
        "s3_version": s3_version,
    }
    return NormalizedTextractResult(
        text=text,
        page_count=page_count,
        block_count=len(blocks),
        pages=pages,
        tables=tables,
        forms=forms,
        queries=queries,
        signatures=signatures,
        layout=layout,
        metadata=metadata,
        native_result=native_result,
    )
