"""Jinja2-based prompt templating with audit support."""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jinja2 import TemplateSyntaxError

from elspeth.core.canonical import canonical_json
from elspeth.plugins.infrastructure.templates import (
    SandboxedTemplate,
    TemplateError,
    TemplateRow,
    template_row_values,
    withheld_error_detail,
)

if TYPE_CHECKING:
    from elspeth.contracts.schema_contract import SchemaContract


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """A rendered prompt with audit metadata."""

    prompt: str
    template_hash: str
    variables_hash: str
    rendered_hash: str
    # New fields for file-based templates
    template_source: str | None = None  # File path or None if inline
    lookup_hash: str | None = None  # Hash of lookup data or None
    lookup_source: str | None = None  # File path or None
    contract_hash: str | None = None  # Hash of schema contract or None


def _sha256(content: str) -> str:
    """Compute SHA-256 hash of string content."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class PromptTemplate:
    """Jinja2 prompt template with audit trail support.

    Uses sandboxed environment to prevent dangerous operations.
    Tracks hashes of template, variables, and rendered output for audit.

    Templates access row data via the `row` namespace and lookup data via
    the `lookup` namespace:
        - {{ row.field_name }} - access row fields
        - {{ lookup.key }} - access lookup data

    A row reaches the template as a ``TemplateRow``: the field values its node
    declares in ``required_input_fields`` only (the whole row under the ``[]``
    opt-out), readable by normalized or original name, with ``row.get(name)``
    as the one method. The row object, its schema contract, their API and any
    undeclared field are not reachable from a template (ADR-051).

    Example:
        template = PromptTemplate(
            '''
            Analyze the following product:
            Name: {{ row.name }}
            Description: {{ row.description }}

            Provide a quality score from 1-10.
            ''',
            lookup_data={"scale": "1-10"},
            lookup_source="lookups.yaml",
        )

        result = template.render_with_metadata(
            {"name": "Widget", "description": "A useful widget"}
        )

        # result.prompt = rendered string
        # result.template_hash = hash of template
        # result.variables_hash = hash of what the template could see of the row
        # result.rendered_hash = hash of final prompt
        # result.lookup_hash = hash of lookup data
    """

    def __init__(
        self,
        template_string: str,
        *,
        template_source: str | None = None,
        lookup_data: dict[str, Any] | None = None,
        lookup_source: str | None = None,
    ) -> None:
        """Initialize template.

        Args:
            template_string: Jinja2 template string
            template_source: File path for audit (None if inline)
            lookup_data: Static lookup data from YAML file
            lookup_source: Lookup file path for audit (None if no lookup)

        Raises:
            TemplateError: If template syntax is invalid
        """
        self._template_string = template_string
        self._template_hash = _sha256(template_string)
        self._template_source = template_source

        # Lookup data for two-dimensional lookups
        # Note: We distinguish None (no lookup configured) from {} (empty lookup).
        # Both are valid, but they're semantically different for audit purposes.
        # Preserve None through — collapse to {} only at the template rendering site.
        lookup_snapshot = deepcopy(lookup_data) if lookup_data is not None else None
        self._lookup_data = lookup_snapshot
        self._lookup_source = lookup_source
        self._lookup_hash = _sha256(canonical_json(lookup_snapshot)) if lookup_snapshot is not None else None

        try:
            self._template = SandboxedTemplate(template_string)
        except TemplateSyntaxError as e:
            raise TemplateError(f"Invalid template syntax: {e}") from e

    @property
    def template_hash(self) -> str:
        """SHA-256 hash of the template string."""
        return self._template_hash

    @property
    def template_source(self) -> str | None:
        """File path if loaded from file, None if inline."""
        return self._template_source

    @property
    def lookup_hash(self) -> str | None:
        """SHA-256 hash of canonical JSON lookup data, or None."""
        return self._lookup_hash

    @property
    def lookup_source(self) -> str | None:
        """File path for lookup data, or None."""
        return self._lookup_source

    def render(self, row: TemplateRow | dict[str, Any]) -> str:
        """Render template with row data.

        Args:
            row: What the template sees as ``row``: a ``TemplateRow``
                (``TemplateRow.project(pipeline_row, projection)`` — the row's
                declared fields only, by either spelling), or a mapping of
                template variables such as a multi-query query's context
                (``QuerySpec.build_template_context``). A ``PipelineRow`` is
                never passed: the packer refuses it as a framework bug.

        Returns:
            Rendered prompt string

        Raises:
            TemplateError: If rendering fails (undefined variable, a read of an
                undeclared field, sandbox violation, etc.)
        """
        context: dict[str, Any] = {
            "row": row,
            "lookup": self._lookup_data if self._lookup_data is not None else {},
        }
        return self._template.render(**context)

    def render_static_with_metadata(self) -> RenderedPrompt:
        """Render a source prompt with lookup data and no row binding."""
        prompt = self._template.render(lookup=self._lookup_data if self._lookup_data is not None else {})
        return RenderedPrompt(
            prompt=prompt,
            template_hash=self._template_hash,
            variables_hash=_sha256(canonical_json({})),
            rendered_hash=_sha256(prompt),
            template_source=self._template_source,
            lookup_hash=self._lookup_hash,
            lookup_source=self._lookup_source,
            contract_hash=None,
        )

    def render_with_metadata(
        self,
        row: TemplateRow | dict[str, Any],
        *,
        contract: SchemaContract | None = None,
    ) -> RenderedPrompt:
        """Render template and return with audit metadata.

        Args:
            row: What the template sees as ``row`` (see ``render``).
            contract: The row's schema contract, hashed into
                ``contract_hash`` when given. It does not change what the
                template sees.

        Returns:
            RenderedPrompt with prompt string and all hashes. ``variables_hash``
            is the hash of exactly what the template could see: a projected
            row's declared field values (ADR-051), not the whole row.

        Raises:
            TemplateError: If rendering fails or a visible value is not
                canonicalizable (e.g., NaN, Infinity)
        """
        prompt = self.render(row)

        # The variables hash covers what the template could see: a projected
        # row's declared values (normalized keys), never an undeclared column;
        # a query context (``QuerySpec.build_template_context``) contributes
        # its variables, its nested ``source_row`` likewise projected.
        row_for_hash = (
            template_row_values(row)
            if type(row) is TemplateRow
            else {key: template_row_values(value) if type(value) is TemplateRow else value for key, value in row.items()}
        )
        # Wrap ValueError/TypeError from canonical_json (NaN/Infinity rejection, non-serializable types)
        # This ensures row-scoped failures don't crash the entire run (Tier 2 trust model).
        # The canonicalizer's own message quotes the offending value, so only its type is kept.
        try:
            variables_hash = _sha256(canonical_json(row_for_hash))
        except (ValueError, TypeError) as e:
            raise TemplateError(f"Cannot compute variables hash: {withheld_error_detail(e)}") from e

        # Compute rendered prompt hash
        rendered_hash = _sha256(prompt)

        # Compute contract hash if provided
        contract_hash: str | None = None
        if contract is not None:
            contract_hash = _sha256(
                canonical_json(
                    {
                        "mode": contract.mode,
                        "fields": [
                            {
                                "n": fc.normalized_name,
                                "o": fc.original_name,
                                "t": fc.python_type.__name__,
                            }
                            for fc in sorted(contract.fields, key=lambda f: f.normalized_name)
                        ],
                    }
                )
            )

        return RenderedPrompt(
            prompt=prompt,
            template_hash=self._template_hash,
            variables_hash=variables_hash,
            rendered_hash=rendered_hash,
            template_source=self._template_source,
            lookup_hash=self._lookup_hash,
            lookup_source=self._lookup_source,
            contract_hash=contract_hash,
        )

    def with_template_override(
        self,
        template_string: str,
        *,
        template_source: str | None = None,
    ) -> PromptTemplate:
        """Create a new template that preserves this template's lookup context."""
        return PromptTemplate(
            template_string,
            template_source=template_source,
            lookup_data=self._lookup_data,
            lookup_source=self._lookup_source,
        )
