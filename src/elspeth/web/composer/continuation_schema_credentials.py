"""Credential admission for an actual retained plugin-schema response.

This inspects complete metadata without changing its wire bytes. It is not
response admission, disclosure permission, current inventory, or a P4 codec.
"""

from __future__ import annotations

from pydantic import JsonValue

from elspeth.contracts.credential_material import is_secret_field
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer._schema_response_grammar import (
    _JSON_SCHEMA_MAP_KEYS,
    _JSON_SCHEMA_SCHEMA_LIST_KEYS,
    _JSON_SCHEMA_SINGLE_SCHEMA_KEYS,
)
from elspeth.web.composer.bounded_json import _validate_json_structure
from elspeth.web.composer.tools._generation_schema_response import (
    PLUGIN_SCHEMA_RESPONSE_CONTRACT,
    AdmittedPluginSchemaResponse,
)
from elspeth.web.credential_guard import (
    field_mapper_credential_options_projection,
    require_no_credential_material,
)
from elspeth.web.secrets.ref_policy import allowed_secret_ref_fields


class _SchemaCredentials:
    def __init__(self, response: AdmittedPluginSchemaResponse, env_ref_names: frozenset[str]) -> None:
        self.env_ref_names = env_ref_names
        snapshot = response.snapshot
        self.credential_fields = allowed_secret_ref_fields(snapshot.plugin_type, snapshot.name) | frozenset(
            item.field for item in snapshot.secret_requirements
        )
        self.field_mapper = snapshot.plugin_type == "transform" and snapshot.name == "field_mapper"
        self.root = snapshot.json_schema.to_wire()
        self.visited: set[tuple[int, int, bool]] = set()

    def scan(self, value: JsonValue, *, credential: bool = False, declared_fields: bool = False) -> None:
        # A literal belonging to a credential slot is admitted using the real
        # detector, including exact managed and inventory-supplied env refs.
        require_no_credential_material(
            {"password": value} if credential else value,
            surface="continuation plugin schema",
            env_ref_names=self.env_ref_names,
            additional_credential_fields=self.credential_fields if declared_fields else frozenset(),
        )

    def literal(self, value: JsonValue, *, credential: bool, mapper_options: bool = False) -> None:
        if not credential and self.field_mapper and mapper_options:
            value = field_mapper_credential_options_projection(value)
        self.scan(value, credential=credential, declared_fields=True)

    def predicate(self, value: JsonValue) -> None:
        if not isinstance(value, dict) or not isinstance(value["field"], str):
            raise AuditIntegrityError("retained schema predicate lost its admitted shape")
        self.scan(value["field"])
        self.literal(value["equals"], credential=is_secret_field(value["field"], self.credential_fields))

    def reference(self, value: JsonValue, root: JsonValue) -> JsonValue:
        if not isinstance(value, str) or not (value == "#" or value.startswith("#/")):
            raise AuditIntegrityError("retained schema credential references require a local JSON pointer")
        current: JsonValue = root
        if value == "#":
            return current
        for part in value[2:].split("/"):
            # Percent-encoded pointers and anchors need an actual resource
            # resolver. Refuse rather than infer a different resource target.
            if "%" in part or any(part[index + 1 : index + 2] not in ("0", "1") for index, char in enumerate(part) if char == "~"):
                raise AuditIntegrityError("retained schema reference has an unsupported pointer escape")
            key = part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, dict) and key in current:
                current = current[key]
            elif (
                isinstance(current, list)
                and key.isascii()
                and key.isdecimal()
                and len(key) <= len(str(len(current)))
                and str(int(key)) == key
                and int(key) < len(current)
            ):
                current = current[int(key)]
            else:
                raise AuditIntegrityError("retained schema reference has no complete local target")
        if type(current) not in (dict, bool):
            raise AuditIntegrityError("retained schema reference is not a schema")
        return current

    def schema(self, value: JsonValue, *, root: JsonValue, credential: bool = False) -> None:
        # Local reference chains can be long even when JSON nesting is shallow.
        # Each actual node/context pair is inspected once, without recursion.
        pending = [(value, credential)]
        while pending:
            current, sensitive = pending.pop()
            self.schema_node(current, root=root, credential=sensitive, pending=pending)

    def schema_node(self, value: JsonValue, *, root: JsonValue, credential: bool, pending: list[tuple[JsonValue, bool]]) -> None:
        if type(value) is bool:
            return
        if not isinstance(value, dict):
            raise AuditIntegrityError("retained schema lost its admitted node shape")
        identity = (id(root), id(value), credential)
        if identity in self.visited:
            return
        self.visited.add(identity)
        for key, child in value.items():
            self.scan(key)
            if key in {"$id", "$anchor", "$dynamicAnchor", "$dynamicRef"}:
                # The ordinary response grammar permits resource scopes. This
                # bounded retention unit has no resolver for those semantics.
                raise AuditIntegrityError("retained schema credential resource scope is unsupported")
            if key == "$ref":
                self.scan(child)
                pending.append((self.reference(child, root), credential))
            elif key in {"default", "const", "example"}:
                self.literal(child, credential=credential, mapper_options=value is self.root)
            elif key in {"enum", "examples"}:
                if not isinstance(child, list):
                    raise AuditIntegrityError("retained schema literal cohort is not its admitted array")
                for item in child:
                    self.literal(item, credential=credential, mapper_options=value is self.root)
            elif key in _JSON_SCHEMA_MAP_KEYS:
                if not isinstance(child, dict):
                    raise AuditIntegrityError("retained schema map lost its admitted shape")
                if key == "patternProperties" and child:
                    # Regex domains may include credential slot names. This
                    # owner does not infer their language from a label string.
                    raise AuditIntegrityError("retained schema patternProperties credential domain is unsupported")
                for name, nested in child.items():
                    self.scan(name)
                    sensitive = (
                        False
                        if key in {"$defs", "definitions"}
                        else credential or (key == "properties" and is_secret_field(name, self.credential_fields))
                    )
                    pending.append((nested, sensitive))
            elif key in _JSON_SCHEMA_SINGLE_SCHEMA_KEYS:
                # An unnamed property may be a declared or conventional
                # credential slot. Boolean/type-only schemas remain inert;
                # their concrete literals need the conservative slot check.
                sensitive = credential or key in {"additionalProperties", "unevaluatedProperties"}
                pending.append((child, sensitive))
            elif key in _JSON_SCHEMA_SCHEMA_LIST_KEYS:
                if not isinstance(child, list):
                    raise AuditIntegrityError("retained schema branches lost their admitted array")
                for nested in child:
                    pending.append((nested, credential))
            elif key == "composer_required_when":
                self.predicate(child)
            elif key in {"dependentRequired", "discriminator"}:
                # These are admitted field-name/URI maps, not configured
                # credential slots. Retain and inspect every name and value.
                if not isinstance(child, dict):
                    raise AuditIntegrityError("retained schema structural map lost its admitted shape")
                if key == "dependentRequired":
                    self.scan([[name, names] for name, names in child.items()])
                else:
                    for name, item in child.items():
                        if name == "mapping" and isinstance(item, dict):
                            self.scan([[label, target] for label, target in item.items()])
                        else:
                            self.scan({name: item})
            else:
                # Prose, flags, numeric/type constraints and extensions stay
                # visible. Unknown extensions never become schema exemptions.
                self.scan({key: child})

    def knobs(self, value: JsonValue, *, credential: bool = False) -> None:
        if not isinstance(value, dict) or not isinstance(value["fields"], list):
            raise AuditIntegrityError("retained knob schema lost its admitted shape")
        for field in value["fields"]:
            if not isinstance(field, dict) or not isinstance(field["name"], str):
                raise AuditIntegrityError("retained knob field lost its admitted shape")
            sensitive = credential or is_secret_field(field["name"], self.credential_fields)
            for key, child in field.items():
                self.scan(key)
                if key == "default":
                    self.literal(child, credential=sensitive)
                elif key in {"enum", "choices"}:
                    if not isinstance(child, list):
                        raise AuditIntegrityError("retained knob choices lost their admitted array")
                    for item in child:
                        self.literal(item, credential=sensitive)
                elif key in {"visible_when", "required_when"}:
                    self.predicate(child)
                elif key == "item_schema":
                    self.knobs(child, credential=sensitive)
                elif key == "items":
                    self.schema(child, root=child, credential=sensitive)
                else:
                    self.scan({key: child})


def require_retained_plugin_schema_credentials(
    response: AdmittedPluginSchemaResponse,
    *,
    env_ref_names: frozenset[str] = frozenset(),
) -> None:
    """Inspect one complete actual schema response without changing any bytes.

    Current literal properties and local JSON-pointer references are supported.
    Nonempty patternProperties, resource IDs/anchors/dynamic references and
    nonlocal references refuse. Additional/unevaluated property schemas admit
    literals conservatively as possible credential slots; boolean and purely
    structural constraints remain supported. There is no network resolution, invented inventory or disclosure grant.
    """
    if type(response) is not AdmittedPluginSchemaResponse:
        raise TypeError("retained schema credentials require the actual admitted response")
    admitted = response.readmit(PLUGIN_SCHEMA_RESPONSE_CONTRACT)
    wire = admitted.to_wire()
    _validate_json_structure(wire, label="retained plugin schema")
    if not isinstance(wire, dict):
        raise AuditIntegrityError("retained plugin schema lost its actual response object")
    guard = _SchemaCredentials(admitted, env_ref_names)
    guard.scan({key: value for key, value in wire.items() if key not in {"json_schema", "knob_schema"}})
    guard.schema(guard.root, root=guard.root)
    guard.knobs(wire["knob_schema"])
