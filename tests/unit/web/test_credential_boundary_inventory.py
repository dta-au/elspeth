"""Closed inventories for Web/Composer credential-boundary owner seams."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.helpers.tree_gate import iter_gate_sources

ROOT = Path(__file__).resolve().parents[3]

_EXPLICIT_REQUEST_BASES = frozenset({"_Provenance", "CreateInlineBlobRequest"})

# The values name the owner policy, not a second implementation. Exact
# equality makes a newly added free-text request field choose a disposition.
_FREE_TEXT_DISPOSITIONS = {
    "src/elspeth/web/_acceptance_common/replica_probes.py:ProbeRequest.method": "private_acceptance_probe",
    "src/elspeth/web/_acceptance_common/replica_probes.py:ProbeRequest.path": "private_acceptance_probe",
    "src/elspeth/web/auth/admin_routes.py:CreateUserRequest.display_name": "model_guard",
    "src/elspeth/web/auth/admin_routes.py:CreateUserRequest.email": "model_guard",
    "src/elspeth/web/auth/admin_routes.py:CreateUserRequest.username": "model_guard",
    "src/elspeth/web/auth/admin_routes.py:DeleteUserRequest.reason": "model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:ActivateIdentityRequest.note": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:AssertRelationshipRequest.from_identity_id": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:AssertRelationshipRequest.note": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:AssertRelationshipRequest.to_identity_id": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:DisableIdentityRequest.reason": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:EnableIdentityRequest.note": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:GrantRoleRequest.identity_id": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:GrantRoleRequest.note": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:GrantRoleRequest.scope": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:PreProvisionIdentityRequest.note": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:PreProvisionIdentityRequest.organisation_id": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:PreProvisionIdentityRequest.subject": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:PreProvisionIdentityRequest.username": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:RevokeRequest.note": "inherited_model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:_Provenance.console_request_id": "model_guard",
    "src/elspeth/web/auth/identity_admin_routes.py:_Provenance.on_behalf_of": "model_guard",
    "src/elspeth/web/auth/quota_routes.py:RevokeQuotaBody.console_request_id": "model_guard",
    "src/elspeth/web/auth/quota_routes.py:RevokeQuotaBody.on_behalf_of": "model_guard",
    "src/elspeth/web/auth/quota_routes.py:SetQuotaBody.console_request_id": "model_guard",
    "src/elspeth/web/auth/quota_routes.py:SetQuotaBody.on_behalf_of": "model_guard",
    "src/elspeth/web/auth/routes.py:LoginRequest.password": "authentication_credential_channel",  # secret-scan: allow-this-line
    "src/elspeth/web/auth/routes.py:LoginRequest.username": "authentication_credential_channel",
    "src/elspeth/web/auth/routes.py:RegisterRequest.display_name": "authentication_credential_channel",
    "src/elspeth/web/auth/routes.py:RegisterRequest.email": "authentication_credential_channel",
    "src/elspeth/web/auth/routes.py:RegisterRequest.password": "authentication_credential_channel",  # secret-scan: allow-this-line
    "src/elspeth/web/auth/routes.py:RegisterRequest.username": "authentication_credential_channel",
    "src/elspeth/web/auth/routes.py:SsoCompleteRequest.code": "authentication_credential_channel",
    "src/elspeth/web/auth/routes.py:VerifyEmailRequest.token": "authentication_credential_channel",
    "src/elspeth/web/blobs/schemas.py:CreateInlineBlobRequest.content": "data_plane_body",
    "src/elspeth/web/blobs/schemas.py:CreateInlineBlobRequest.filename": "route_guard",
    "src/elspeth/web/composer/boot_probe.py:ComposerProbeRequest.model": "server_configuration_probe",
    "src/elspeth/web/composer/pipeline_planner.py:PlannerPriorUserRequest.content": "provider_boundary_guard",
    "src/elspeth/web/composer/tools/sessions.py:_AdvisorHintRequest.problem_summary": "provider_boundary_guard",
    "src/elspeth/web/execution/schemas.py:ExecuteRequest.fanout_ack_token": "execution_acknowledgement",
    "src/elspeth/web/execution/schemas.py:ExecuteRequest.secret_ack_token": "execution_acknowledgement",
    "src/elspeth/web/preferences/models.py:UpdateComposerPreferencesRequest.tutorial_run_id": "structural_identifier",
    "src/elspeth/web/preferences/models.py:UpdateComposerPreferencesRequest.tutorial_session_id": "structural_identifier",
    "src/elspeth/web/preferences/models.py:UpdateComposerPreferencesRequest.tutorial_source_data_hash": "structural_identifier",
    "src/elspeth/web/secrets/schemas.py:CreateSecretRequest.name": "managed_secret_channel",
    "src/elspeth/web/secrets/schemas.py:CreateSecretRequest.value": "managed_secret_channel",
    "src/elspeth/web/sessions/routes/composer/state.py:ImportStateYamlRequest.yaml": "parsed_state_guard",
    "src/elspeth/web/sessions/routes/workflow/approvals.py:ApprovalDecisionBody.note": "field_guard",
    "src/elspeth/web/sessions/routes/workflow/approvals.py:ApprovalRequestBody.approver_identity_id": "structural_identifier",
    "src/elspeth/web/sessions/routes/workflow/approvals.py:ApprovalRequestBody.note": "field_guard",
    "src/elspeth/web/sessions/routes/workflow/library.py:CurateLibraryEntryRequest.note": "field_guard",
    "src/elspeth/web/sessions/routes/workflow/library.py:PublishLibraryEntryRequest.title": "field_guard",
    "src/elspeth/web/sessions/routes/workflow/reviews.py:AttestBody.note": "field_guard",
    "src/elspeth/web/sessions/routes/workflow/reviews.py:RequestReviewBody.note": "field_guard",
    "src/elspeth/web/sessions/routes/workflow/reviews.py:RequestReviewBody.reviewer_identity_id": "structural_identifier",
    "src/elspeth/web/sessions/routes/workflow/reviews.py:RequestReviewBody.state_id": "structural_identifier",
    "src/elspeth/web/sessions/schemas.py:_SessionOperationRequest.operation_id": "structural_identifier",
    "src/elspeth/web/sessions/schemas.py:AcceptProposalRequest.draft_hash": "structural_identifier",
    "src/elspeth/web/sessions/schemas.py:CreateSessionRequest.title": "route_guard",
    "src/elspeth/web/sessions/schemas.py:ForkSessionRequest.new_message_content": "route_guard",
    "src/elspeth/web/sessions/schemas.py:InterpretationResolveRequest.amended_value": "field_guard",
    "src/elspeth/web/sessions/schemas.py:RejectProposalRequest.reason": "unused_not_persisted",
    "src/elspeth/web/sessions/schemas.py:SendMessageRequest.content": "route_guard",
    "src/elspeth/web/sessions/schemas.py:UpdateSessionRequest.title": "route_guard",
}


def _is_direct_text_annotation(annotation: ast.expr) -> bool:
    if isinstance(annotation, ast.Name):
        return annotation.id == "str"
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _is_direct_text_annotation(annotation.left) or _is_direct_text_annotation(annotation.right)
    if (
        isinstance(annotation, ast.Subscript)
        and isinstance(annotation.value, ast.Name)
        and annotation.value.id in {"Annotated", "Optional"}
    ):
        inner = annotation.slice.elts[0] if isinstance(annotation.slice, ast.Tuple) else annotation.slice
        return _is_direct_text_annotation(inner)
    return False


def _free_text_request_fields(source: str, *, path: str) -> set[str]:
    found: set[str] = set()
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef):
            continue
        if not (node.name.endswith(("Request", "Body")) or node.name in _EXPLICIT_REQUEST_BASES):
            continue
        for item in node.body:
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and _is_direct_text_annotation(item.annotation):
                found.add(f"{path}:{node.name}.{item.target.id}")
    return found


def test_free_text_inventory_scanner_has_positive_and_negative_controls() -> None:
    source = """
class ExampleRequest(BaseModel):
    note: str
    memo: str | None
    count: int

class ExampleResponse(BaseModel):
    note: str
"""

    assert _free_text_request_fields(source, path="example.py") == {
        "example.py:ExampleRequest.memo",
        "example.py:ExampleRequest.note",
    }


def test_every_scoped_request_free_text_field_has_a_closed_disposition() -> None:
    observed: set[str] = set()
    for parsed in iter_gate_sources(ROOT / "src/elspeth/web"):
        relative = parsed.path.relative_to(ROOT).as_posix()
        observed.update(_free_text_request_fields(parsed.source, path=relative))

    assert observed == set(_FREE_TEXT_DISPOSITIONS)
    assert set(_FREE_TEXT_DISPOSITIONS.values()) == {
        "authentication_credential_channel",
        "data_plane_body",
        "execution_acknowledgement",
        "field_guard",
        "inherited_model_guard",
        "managed_secret_channel",
        "model_guard",
        "parsed_state_guard",
        "private_acceptance_probe",
        "provider_boundary_guard",
        "route_guard",
        "server_configuration_probe",
        "structural_identifier",
        "unused_not_persisted",
    }


_ROUTE_GUARD_ARGUMENTS = {
    "src/elspeth/web/blobs/schemas.py:CreateInlineBlobRequest.filename": (
        "src/elspeth/web/blobs/routes.py",
        "body.filename",
    ),
    "src/elspeth/web/sessions/schemas.py:CreateSessionRequest.title": (
        "src/elspeth/web/sessions/routes/sessions.py",
        "title",
    ),
    "src/elspeth/web/sessions/schemas.py:ForkSessionRequest.new_message_content": (
        "src/elspeth/web/sessions/routes/sessions.py",
        "body.new_message_content",
    ),
    "src/elspeth/web/sessions/schemas.py:SendMessageRequest.content": (
        "src/elspeth/web/sessions/routes/messages.py",
        "body.content",
    ),
    "src/elspeth/web/sessions/schemas.py:UpdateSessionRequest.title": (
        "src/elspeth/web/sessions/routes/sessions.py",
        "body.title",
    ),
}
_REQUEST_GUARD_CALLS = frozenset(
    {
        "reject_credential_material",
        "reject_credential_shaped_content",
        "_validate_accepted_value_content",
    }
)


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _node_calls(node: ast.AST, names: frozenset[str]) -> bool:
    return any(isinstance(child, ast.Call) and _call_name(child) in names for child in ast.walk(node))


def _classes_by_name(tree: ast.Module) -> dict[str, ast.ClassDef]:
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def _class_has_model_guard(class_name: str, classes: dict[str, ast.ClassDef], seen: frozenset[str] = frozenset()) -> bool:
    if class_name in seen:
        return False
    node = classes[class_name]
    guarded_method = any(
        isinstance(method, ast.FunctionDef)
        and _node_calls(method, frozenset({"reject_credential_material"}))
        and any(
            (isinstance(decorator, ast.Call) and _call_name(decorator) == "model_validator")
            or (isinstance(decorator, ast.Name) and decorator.id == "model_validator")
            for decorator in method.decorator_list
        )
        for method in node.body
    )
    if guarded_method:
        return True
    parent_names = [base.id for base in node.bases if isinstance(base, ast.Name) and base.id in classes]
    return any(_class_has_model_guard(parent, classes, seen | {class_name}) for parent in parent_names)


def _class_has_field_guard(class_name: str, field_name: str, classes: dict[str, ast.ClassDef]) -> bool:
    node = classes[class_name]
    for method in node.body:
        if not isinstance(method, ast.FunctionDef) or not _node_calls(method, _REQUEST_GUARD_CALLS):
            continue
        decorated_fields = {
            arg.value
            for decorator in method.decorator_list
            if isinstance(decorator, ast.Call) and _call_name(decorator) == "field_validator"
            for arg in decorator.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        }
        referenced_fields = {child.attr for child in ast.walk(method) if isinstance(child, ast.Attribute) and isinstance(child.attr, str)}
        if field_name in decorated_fields or field_name in referenced_fields:
            return True
    return False


def _guard_call_arguments(source: str, guard_name: str) -> set[str]:
    return {
        ast.unparse(call.args[0])
        for call in ast.walk(ast.parse(source))
        if isinstance(call, ast.Call) and _call_name(call) == guard_name and call.args
    }


def _unguarded_model_fields(source: str, *, path: str) -> set[str]:
    tree = ast.parse(source)
    classes = _classes_by_name(tree)
    gaps: set[str] = set()
    for site in _free_text_request_fields(source, path=path):
        owner = site.rsplit(":", 1)[1]
        class_name, field_name = owner.split(".", 1)
        if not (_class_has_model_guard(class_name, classes) or _class_has_field_guard(class_name, field_name, classes)):
            gaps.add(site)
    return gaps


def test_model_and_field_guard_dispositions_are_mechanically_verified() -> None:
    trees: dict[str, tuple[ast.Module, dict[str, ast.ClassDef]]] = {}
    for site, disposition in _FREE_TEXT_DISPOSITIONS.items():
        if disposition not in {"field_guard", "inherited_model_guard", "model_guard"}:
            continue
        relative, owner = site.rsplit(":", 1)
        class_name, field_name = owner.split(".", 1)
        if relative not in trees:
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            trees[relative] = (tree, _classes_by_name(tree))
        classes = trees[relative][1]
        if disposition in {"inherited_model_guard", "model_guard"}:
            assert _class_has_model_guard(class_name, classes), site
        else:
            assert _class_has_field_guard(class_name, field_name, classes), site


def test_route_guard_dispositions_are_mechanically_verified() -> None:
    assert set(_ROUTE_GUARD_ARGUMENTS) == {site for site, disposition in _FREE_TEXT_DISPOSITIONS.items() if disposition == "route_guard"}
    for site, (relative, argument) in _ROUTE_GUARD_ARGUMENTS.items():
        arguments = _guard_call_arguments((ROOT / relative).read_text(encoding="utf-8"), "require_no_credential_material")
        assert argument in arguments, site


def test_request_inventory_mutation_controls_detect_new_ingress_and_removed_guard() -> None:
    guarded = """
class ExampleRequest(BaseModel):
    note: str

    @model_validator(mode="after")
    def validate_control(self):
        reject_credential_material(self.model_dump())
        return self
"""
    assert _unguarded_model_fields(guarded, path="example.py") == set()

    removed_guard = guarded.replace("reject_credential_material(self.model_dump())", "pass")
    assert _unguarded_model_fields(removed_guard, path="example.py") == {"example.py:ExampleRequest.note"}

    new_ingress = """
class ExampleRequest(BaseModel):
    note: str

    @field_validator("note")
    def validate_note(cls, value):
        reject_credential_material(value)
        return value

    memo: str
"""
    assert _unguarded_model_fields(new_ingress, path="example.py") == {"example.py:ExampleRequest.memo"}


_PROVIDER_MODULES = (
    "src/elspeth/web/composer/provider_gateway.py",
    "src/elspeth/web/composer/advisor_checkpoint.py",
    "src/elspeth/web/composer/boot_probe.py",
    "src/elspeth/web/composer/pipeline_planner.py",
    "src/elspeth/web/sessions/_auto_title.py",
)
_PROVIDER_CALL_DISPOSITIONS = {
    "src/elspeth/web/composer/advisor_checkpoint.py:AdvisorCheckpointOwner._call_advisor_with_audit._litellm_acompletion": "complete_response_guard",
    "src/elspeth/web/composer/boot_probe.py:probe_composer_config._litellm_acompletion": "discarded_probe_response",
    "src/elspeth/web/composer/pipeline_planner.py:_plan_pipeline_inner.call_model.completion": "complete_response_guard",
    "src/elspeth/web/composer/provider_gateway.py:ProviderGateway._call_llm._litellm_acompletion": "complete_response_guard",
    "src/elspeth/web/composer/provider_gateway.py:ProviderGateway._call_text_llm._litellm_acompletion": "complete_response_guard",
    "src/elspeth/web/composer/provider_gateway.py:_litellm_acompletion.acompletion": "request_guard",
    "src/elspeth/web/sessions/_auto_title.py:maybe_auto_title_session._litellm_acompletion": "admitted_title_only",
}
_PROVIDER_GUARD_OWNERS = {
    "src/elspeth/web/composer/advisor_checkpoint.py:AdvisorCheckpointOwner._call_advisor_with_audit._litellm_acompletion": (
        ("AdvisorCheckpointOwner._call_advisor_with_audit", "_require_no_credential_material_in_completion_fields"),
    ),
    "src/elspeth/web/composer/pipeline_planner.py:_plan_pipeline_inner.call_model.completion": (
        ("_parse_response_tool_calls", "require_no_credential_material"),
        ("_parse_response_tool_calls", "require_no_credential_material_in_tool_wire"),
        ("_plan_pipeline_inner", "build_llm_call_record"),
    ),
    "src/elspeth/web/composer/provider_gateway.py:ProviderGateway._call_llm._litellm_acompletion": (
        ("ProviderGateway._call_llm", "require_no_credential_material_in_tool_wire"),
        ("ProviderGateway._call_llm", "_require_no_credential_material_in_completion"),
        ("ProviderGateway._call_llm_with_audit", "_require_no_credential_material_in_completion"),
        ("_require_no_credential_material_in_completion_fields", "require_no_credential_material_in_llm_metadata"),
    ),
    "src/elspeth/web/composer/provider_gateway.py:ProviderGateway._call_text_llm._litellm_acompletion": (
        ("ProviderGateway._call_text_llm", "_require_no_credential_material_in_completion_fields"),
    ),
    "src/elspeth/web/composer/provider_gateway.py:_litellm_acompletion.acompletion": (
        ("_litellm_acompletion", "require_no_credential_material"),
    ),
    "src/elspeth/web/sessions/_auto_title.py:maybe_auto_title_session._litellm_acompletion": (
        ("_admit_title_candidate", "reject_credential_shaped_content"),
    ),
}


class _ProviderCallVisitor(ast.NodeVisitor):
    def __init__(self, *, path: str) -> None:
        self.path = path
        self.scopes: list[str] = []
        self.found: set[str] = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_Call(self, node: ast.Call) -> None:
        call_name: str | None = None
        if isinstance(node.func, ast.Name) and node.func.id == "_litellm_acompletion":
            call_name = node.func.id
        elif isinstance(node.func, ast.Attribute) and node.func.attr in {"_litellm_acompletion", "acompletion", "completion"}:
            call_name = node.func.attr
        if call_name is not None:
            self.found.add(f"{self.path}:{'.'.join(self.scopes)}.{call_name}")
        self.generic_visit(node)


def _provider_calls(source: str, *, path: str) -> set[str]:
    visitor = _ProviderCallVisitor(path=path)
    visitor.visit(ast.parse(source))
    return visitor.found


class _ScopedCallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scopes: list[str] = []
        self.calls: dict[str, set[str]] = {}

    def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scopes.append(node.name)
        self.calls.setdefault(".".join(self.scopes), set())
        self.generic_visit(node)
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        if self.scopes and name is not None:
            for depth in range(1, len(self.scopes) + 1):
                self.calls.setdefault(".".join(self.scopes[:depth]), set()).add(name)
        self.generic_visit(node)


def _scoped_calls(source: str) -> dict[str, set[str]]:
    visitor = _ScopedCallVisitor()
    visitor.visit(ast.parse(source))
    return visitor.calls


class _ConstructorCallVisitor(ast.NodeVisitor):
    def __init__(self, *, path: str, constructor_names: frozenset[str]) -> None:
        self.path = path
        self.constructor_names = constructor_names
        self.scopes: list[str] = []
        self.found: set[str] = set()

    def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _call_name(node)
        if call_name in self.constructor_names:
            self.found.add(f"{self.path}:{'.'.join(self.scopes)}.{call_name}")
        self.generic_visit(node)


def _constructor_calls(source: str, *, path: str, names: frozenset[str]) -> set[str]:
    visitor = _ConstructorCallVisitor(path=path, constructor_names=names)
    visitor.visit(ast.parse(source))
    return visitor.found


def _llm_audit_sources() -> dict[str, str]:
    return {parsed.path.relative_to(ROOT).as_posix(): parsed.source for parsed in iter_gate_sources(ROOT / "src/elspeth")}


def _assert_llm_audit_constructor_inventory(sources: dict[str, str]) -> None:
    direct_record_sites: set[str] = set()
    malformed_sites: set[str] = set()
    for path, source in sources.items():
        direct_record_sites.update(_constructor_calls(source, path=path, names=frozenset({"ComposerLLMCall"})))
        malformed_sites.update(_constructor_calls(source, path=path, names=frozenset({"_MalformedLLMResponseError"})))

    parsing_path = "src/elspeth/web/composer/llm_response_parsing.py"
    provider_path = "src/elspeth/web/composer/provider_gateway.py"
    assert direct_record_sites == {f"{parsing_path}:build_llm_call_record.ComposerLLMCall"}
    assert malformed_sites
    parsing_calls = _scoped_calls(sources[parsing_path])
    provider_calls = _scoped_calls(sources[provider_path])
    assert "require_no_credential_material_in_llm_metadata" in parsing_calls["build_llm_call_record"]
    assert "_require_no_credential_material_in_completion_fields" in provider_calls["_MalformedLLMResponseError.__init__"]
    assert "require_no_credential_material_in_llm_metadata" in provider_calls["_require_no_credential_material_in_completion_fields"]


def test_llm_audit_constructors_are_behind_the_common_metadata_guard() -> None:
    _assert_llm_audit_constructor_inventory(_llm_audit_sources())


def test_llm_audit_constructor_inventory_detects_new_bypass_and_guard_removal() -> None:
    sources = _llm_audit_sources()
    direct_bypass = dict(sources)
    direct_bypass["src/elspeth/web/composer/new_audit_path.py"] = """
def bypass():
    return ComposerLLMCall()
"""
    with pytest.raises(AssertionError):
        _assert_llm_audit_constructor_inventory(direct_bypass)

    record_guard_removed = dict(sources)
    parsing_path = "src/elspeth/web/composer/llm_response_parsing.py"
    record_guard_removed[parsing_path] = record_guard_removed[parsing_path].replace(
        "require_no_credential_material_in_llm_metadata(",
        "removed_credential_guard(",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_llm_audit_constructor_inventory(record_guard_removed)

    malformed_guard_removed = dict(sources)
    provider_path = "src/elspeth/web/composer/provider_gateway.py"
    malformed_guard_removed[provider_path] = malformed_guard_removed[provider_path].replace(
        "_require_no_credential_material_in_completion_fields(",
        "removed_credential_guard(",
        1,
    )
    with pytest.raises(AssertionError):
        _assert_llm_audit_constructor_inventory(malformed_guard_removed)


def test_provider_inventory_scanner_has_positive_and_negative_controls() -> None:
    source = """
async def guarded():
    await gateway._litellm_acompletion(messages=[])

async def ordinary():
    await other_call()
"""

    assert _provider_calls(source, path="example.py") == {"example.py:guarded._litellm_acompletion"}


def test_every_direct_composer_provider_call_has_an_explicit_response_disposition() -> None:
    observed: set[str] = set()
    for relative in _PROVIDER_MODULES:
        observed.update(_provider_calls((ROOT / relative).read_text(encoding="utf-8"), path=relative))

    assert observed == set(_PROVIDER_CALL_DISPOSITIONS)


def test_provider_dispositions_mechanically_verify_their_guard_owners() -> None:
    assert set(_PROVIDER_GUARD_OWNERS) == {
        site for site, disposition in _PROVIDER_CALL_DISPOSITIONS.items() if disposition != "discarded_probe_response"
    }
    calls_by_module: dict[str, dict[str, set[str]]] = {}
    for site, owners in _PROVIDER_GUARD_OWNERS.items():
        relative = site.split(":", 1)[0]
        calls = calls_by_module.setdefault(relative, _scoped_calls((ROOT / relative).read_text(encoding="utf-8")))
        for scope, guard_name in owners:
            assert guard_name in calls.get(scope, set()), (site, scope, guard_name)


def test_provider_guard_mutation_control_detects_removal() -> None:
    source = """
async def call_provider():
    response = await completion()
    require_no_credential_material(response)
    return response
"""
    assert "require_no_credential_material" in _scoped_calls(source)["call_provider"]
    mutant = source.replace("require_no_credential_material(response)", "pass")
    assert "require_no_credential_material" not in _scoped_calls(mutant)["call_provider"]


def test_common_tool_owners_hold_the_guard_for_new_tools_automatically() -> None:
    owners = {
        "src/elspeth/web/composer/tools/_dispatch.py": ("require_no_credential_material_for_tool",),
        "src/elspeth/web/composer/tool_batch.py": (
            "require_no_credential_material",
            "require_no_credential_material_in_llm_metadata",
            "require_no_credential_material_in_tool_wire",
        ),
        "src/elspeth/composer_mcp/server.py": ("require_no_credential_material_for_tool",),
    }

    for relative, required_calls in owners.items():
        source = (ROOT / relative).read_text(encoding="utf-8")
        for required_call in required_calls:
            assert any(required_call in calls for calls in _scoped_calls(source).values())
            mutant = source.replace(required_call, "removed_credential_guard")
            assert all(required_call not in calls for calls in _scoped_calls(mutant).values())


_STATE_GUARD_DISPOSITIONS = {
    "src/elspeth/composer_mcp/server.py:_dispatch_tool": "mcp_dispatch",
    "src/elspeth/web/composer/service.py:ComposerServiceImpl.compose": "provider_serialization",
    "src/elspeth/web/composer/tools/sessions.py:_execute_get_pipeline_state": "provider_disclosure",
    "src/elspeth/web/execution/service.py:ExecutionServiceImpl._execute_locked": "execution",
    "src/elspeth/web/execution/service.py:ExecutionServiceImpl.compile_approval_binding": "approval_binding",
    "src/elspeth/web/execution/service.py:ExecutionServiceImpl.validate_state": "validation",
    "src/elspeth/web/sessions/routes/composer/state.py:seed_state_from_runtime_yaml": "yaml_persistence",
    "src/elspeth/web/sessions/routes/workflow/library.py:create_library_router.fork_entry": "library_fork",
    "src/elspeth/web/sessions/routes/workflow/library.py:create_library_router.publish_entry": "library_publication",
}
_STATE_MATERIALIZATION_DISPOSITIONS = {
    "src/elspeth/web/audit_readiness/routes.py:create_audit_readiness_router.explain.state_from_record": "provider_guard_downstream",
    "src/elspeth/web/audit_readiness/service.py:ReadinessService.compute_snapshot._state_from_record": "read_only_diagnostics",
    "src/elspeth/web/composer/tutorial_service.py:_require_tutorial_launch_readiness.state_from_record": "validation_guard_downstream",
    "src/elspeth/web/execution/routes.py:create_execution_router.validate_session_pipeline.state_from_record": "validation_guard_downstream",
    "src/elspeth/web/execution/service.py:ExecutionServiceImpl._execute_locked.state_from_record": "direct_state_guard",
    "src/elspeth/web/execution/service.py:ExecutionServiceImpl.compile_approval_binding.state_from_record": "direct_state_guard",
    "src/elspeth/web/execution/service.py:ExecutionServiceImpl.validate.state_from_record": "direct_state_guard_downstream",
    "src/elspeth/web/sessions/converters.py:pipeline_dict_from_record.state_from_record": "serialization_helper",
    "src/elspeth/web/sessions/mutation_capabilities.py:_SessionComposerMutations.create_pipeline_composition_proposal.state_from_record": "proposal_internal",
    "src/elspeth/web/sessions/pending_interpretation.py:_SessionPendingInterpretationPlanner.plan.state_from_record": "interpretation_internal",
    "src/elspeth/web/sessions/pending_interpretation.py:_SessionPendingInterpretationValidator.__call__.state_from_record": "interpretation_internal",
    "src/elspeth/web/sessions/pending_interpretation.py:_source_data_contract_demand_from_state_record.state_from_record": "interpretation_internal",
    "src/elspeth/web/sessions/proposal_authority.py:_verify_committed_pipeline_authority.state_from_record": "proposal_internal",
    "src/elspeth/web/sessions/routes/composer/compose.py:recompose._state_from_record": "provider_guard_downstream",
    "src/elspeth/web/sessions/routes/composer/pipeline_settlement.py:settle_pipeline_proposal_under_compose_lock._state_from_record": "proposal_internal",
    "src/elspeth/web/sessions/routes/composer/proposals.py:accept_composition_proposal._state_from_record": "proposal_internal",
    "src/elspeth/web/sessions/routes/composer/state.py:_surface_reverted_interpretation_reviews._state_from_record": "interpretation_internal",
    "src/elspeth/web/sessions/routes/composer/state.py:get_current_state._state_from_record": "authenticated_state_response",
    "src/elspeth/web/sessions/routes/composer/state.py:get_state_yaml._state_from_record": "authenticated_state_response",
    "src/elspeth/web/sessions/routes/composer/state.py:seed_state_from_runtime_yaml.composition_state_from_runtime_yaml": "direct_state_guard",
    "src/elspeth/web/sessions/routes/messages.py:register_message_routes.send_message._state_from_record": "provider_guard_downstream",
    "src/elspeth/web/sessions/routes/workflow/inspect.py:create_workflow_inspect_router.inspect_workflow_state.state_from_record": "authenticated_state_response",
    "src/elspeth/web/sessions/routes/workflow/library.py:create_library_router.fork_entry.composition_state_from_runtime_yaml": "direct_state_guard",
    "src/elspeth/web/sessions/routes/workflow/library.py:create_library_router.publish_entry.state_from_record": "direct_state_guard",
    "src/elspeth/web/sessions/service.py:SessionServiceImpl.resolve_interpretation_event._sync.state_from_record": "interpretation_internal",
    "src/elspeth/web/sessions/service.py:SessionServiceImpl.settle_pipeline_composition_proposal._sync.state_from_record": "proposal_internal",
    "src/elspeth/web/shareable_reviews/service.py:ShareableReviewService.mark_ready_for_review.state_from_record": "validation_guard_downstream",
    "src/elspeth/web/shareable_reviews/service.py:_build_snapshot.state_from_record": "validated_publication",
}


class _ExactCallSiteVisitor(ast.NodeVisitor):
    def __init__(self, *, path: str, names: frozenset[str]) -> None:
        self.path = path
        self.names = names
        self.scopes: list[str] = []
        self.found: set[str] = set()

    def _visit_scope(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.scopes.append(node.name)
        self.generic_visit(node)
        self.scopes.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scope(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _call_name(node)
        if name in self.names:
            self.found.add(f"{self.path}:{'.'.join(self.scopes)}.{name}")
        self.generic_visit(node)


def _call_sites(source: str, *, path: str, names: frozenset[str]) -> set[str]:
    visitor = _ExactCallSiteVisitor(path=path, names=names)
    visitor.visit(ast.parse(source))
    return visitor.found


def test_state_use_and_publication_inventory_discovers_new_materialization_sites() -> None:
    observed: set[str] = set()
    names = frozenset({"_state_from_record", "composition_state_from_runtime_yaml", "state_from_record"})
    for parsed in iter_gate_sources(ROOT / "src/elspeth/web"):
        relative = parsed.path.relative_to(ROOT).as_posix()
        observed.update(_call_sites(parsed.source, path=relative, names=names))

    assert observed == set(_STATE_MATERIALIZATION_DISPOSITIONS)


def test_state_guard_inventory_and_removal_mutation_control() -> None:
    observed: set[str] = set()
    names = frozenset({"require_no_credential_material_in_state"})
    for parsed in iter_gate_sources(ROOT / "src/elspeth"):
        relative = parsed.path.relative_to(ROOT).as_posix()
        observed.update(_call_sites(parsed.source, path=relative, names=names))

    expected = {f"{scope}.require_no_credential_material_in_state" for scope in _STATE_GUARD_DISPOSITIONS}
    assert observed == expected

    source = """
def publish(state):
    require_no_credential_material_in_state(state)
    persist(state)
"""
    assert _call_sites(source, path="example.py", names=names) == {"example.py:publish.require_no_credential_material_in_state"}
    mutant = source.replace("require_no_credential_material_in_state(state)", "pass")
    assert _call_sites(mutant, path="example.py", names=names) == set()
