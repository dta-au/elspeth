"""Typed dependency roles for the concrete family contracts, not effect proof."""

import ast
import builtins


def module_name(path):
    return path.removeprefix("src/").removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def imports_for(unit, fn, *, include_local=True):
    selected = list(unit.tree.body)
    for statement in unit.tree.body:
        if isinstance(statement, ast.If) and isinstance(statement.test, ast.Name) and statement.test.id == "TYPE_CHECKING":
            selected.extend(statement.body)
    if include_local:
        selected.extend(node for node in ast.walk(fn) if isinstance(node, (ast.Import, ast.ImportFrom)))
    imports = {}
    for statement in selected:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                imports[alias.asname or alias.name.split(".")[0]] = (alias.name if alias.asname else alias.name.split(".")[0], statement)
        elif isinstance(statement, ast.ImportFrom) and statement.level == 0 and statement.module:
            for alias in statement.names:
                imports[alias.asname or alias.name] = (statement.module + "." + alias.name, statement)
    return imports


class ExecutionNames(ast.NodeVisitor):
    def __init__(self):
        self.names = set()
        self.members = {}

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load):
            self.names.add(node.id)

    def visit_AnnAssign(self, node):
        # PEP526: local variable annotations are not evaluated or retained.
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name):
            self.members.setdefault(node.value.id, set()).add(node.attr)
        self.generic_visit(node)


def family_dependency_roles(units, helper):
    """Return source-owned typed seeds; class fields require receiver evidence.

    Imported identities are exact import binding targets, not terminal reexport
    identities. SOURCE4 must resolve/reject their aliases and relevant effects.
    Definition annotations outside postponed modules remain executed effects.
    """
    failures, roots = helper["family_failures"](units)
    semantic_requirements = helper["semantic_dependency_requirements"](units)
    roots.update(semantic_requirements)
    catalogue = {
        "src/elspeth/web/required_work.py": helper["_binding_contracts"](),
        "src/elspeth/web/composer/provider_quota.py": helper["_provider_contracts"](),
        "src/elspeth/web/operator_telemetry_dispatch.py": helper["_telemetry_contracts"](),
        "src/elspeth/web/operator_telemetry_installation.py": helper["_installation_contracts"](),
        "src/elspeth/web/coordination/lifecycle.py": {
            **helper["_lease_contracts"](),
            "SessionOperationLease.adopt": helper["_adopt_contract"](),
            "SessionOperationLease._renew_forever": helper["_renew_contract"](),
            "_raise_adopt_failure_after_release": None,
        },
        "src/elspeth/web/operator_telemetry_custody.py": {
            "OperatorTelemetryCleanupOwner.__init__": None,
            "OperatorTelemetryCleanupOwner.assert_process": None,
        },
        "src/elspeth/web/operator_telemetry.py": {"bootstrap_operator_telemetry": None},
    }
    runtime, annotation, member_uses = {}, {}, {}
    object_index, import_index = {}, {}
    for unit in units:
        module = module_name(unit.path)
        object_index[module] = {"kind": "mutable_module_namespace", "path": unit.path, "line": 1}
        for statement in unit.tree.body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                identity = module + "." + statement.name
                object_index[identity] = {
                    "kind": "owned_class" if isinstance(statement, ast.ClassDef) else "owned_function",
                    "path": unit.path,
                    "line": statement.lineno,
                }
                if isinstance(statement, ast.ClassDef):
                    for member in statement.body:
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            object_index[identity + "." + member.name] = {
                                "kind": "receiver_bound_method_descriptor",
                                "owner_class": identity,
                                "member": member.name,
                                "path": unit.path,
                                "line": member.lineno,
                                "binding": "classmethod"
                                if any(isinstance(d, ast.Name) and d.id == "classmethod" for d in member.decorator_list)
                                else "property"
                                if any(isinstance(d, ast.Name) and d.id == "property" for d in member.decorator_list)
                                else "bound_method",
                            }
                        elif isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name):
                            object_index[identity + "." + member.target.id] = {
                                "kind": "receiver_bound_field_descriptor",
                                "owner_class": identity,
                                "member": member.target.id,
                                "path": unit.path,
                                "line": member.lineno,
                                "producer": "owned dataclass field declaration",
                            }
                        elif (
                            isinstance(member, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id == "__slots__" for t in member.targets)
                            and isinstance(member.value, (ast.Tuple, ast.List))
                        ):
                            for slot in member.value.elts:
                                if isinstance(slot, ast.Constant) and type(slot.value) is str:
                                    object_index[identity + "." + slot.value] = {
                                        "kind": "receiver_bound_field_descriptor",
                                        "owner_class": identity,
                                        "member": slot.value,
                                        "path": unit.path,
                                        "line": member.lineno,
                                        "producer": "owned slot declaration",
                                    }
            elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        object_index[module + "." + target.id] = {
                            "kind": "owned_module_value_binding",
                            "path": unit.path,
                            "line": statement.lineno,
                        }
        for qualified in catalogue.get(unit.path, {}):
            fn = helper["_find"](unit.tree, qualified)
            if fn is None:
                continue
            bindings = imports_for(unit, fn)
            definition_bindings = imports_for(unit, fn, include_local=False)
            for identity, statement in bindings.values():
                import_index.setdefault(identity, []).append(
                    {
                        "path": unit.path,
                        "line": statement.lineno,
                        "import_kind": "module_namespace" if isinstance(statement, ast.Import) else "symbol_binding",
                    }
                )
            local = {arg.arg for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
            local.update(node.id for node in ast.walk(fn) if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)))
            local.update(node.name for node in ast.walk(fn) if isinstance(node, ast.ExceptHandler) and node.name is not None)
            visitor = ExecutionNames()
            body = fn.body
            if qualified == "bootstrap_operator_telemetry":
                retained = [node for node in body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
                body = retained[:4]
            for statement in body:
                visitor.visit(statement)
            # Body arguments/locals do not bind definition expressions. In a
            # non-postponed annotation `owner: owner()`, the callee is global.
            definition_visitor = ExecutionNames()
            for expression in helper["_definition_expressions"](unit, fn):
                definition_visitor.visit(expression)
            for name in visitor.names - local:
                identity = bindings[name][0] if name in bindings else "builtins." + name if name in vars(builtins) else module + "." + name
                runtime.setdefault(identity, []).append({"path": unit.path, "method": qualified, "line": fn.lineno, "scope": "body"})
                if name in visitor.members:
                    member_uses.setdefault(identity, set()).update(visitor.members[name])
            nonexecuting = [node.annotation for statement in body for node in ast.walk(statement) if isinstance(node, ast.AnnAssign)]
            if helper["_postponed_annotations"](unit):
                nonexecuting.extend(
                    arg.annotation for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs) if arg.annotation is not None
                )
                if fn.returns is not None:
                    nonexecuting.append(fn.returns)
            for expression in nonexecuting:
                for node in ast.walk(expression):
                    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in local:
                        identity = (
                            definition_bindings[node.id][0]
                            if node.id in definition_bindings
                            else "builtins." + node.id
                            if node.id in vars(builtins)
                            else module + "." + node.id
                        )
                        annotation.setdefault(identity, []).append(
                            {"path": unit.path, "method": qualified, "line": fn.lineno, "scope": "nonexecuting annotation"}
                        )
            for name in definition_visitor.names:
                identity = (
                    definition_bindings[name][0]
                    if name in definition_bindings
                    else "builtins." + name
                    if name in vars(builtins)
                    else module + "." + name
                )
                runtime.setdefault(identity, []).append({"path": unit.path, "method": qualified, "line": fn.lineno, "scope": "definition"})
                if name in definition_visitor.members:
                    member_uses.setdefault(identity, set()).update(definition_visitor.members[name])
    # Every method definition in the canonical owned classes executes in the
    # class suite, even when that method body is outside this family catalogue.
    # Current supported grammar prohibits class-local supplier shadowing.
    for unit, qualified, fn in helper["owned_method_definitions"](units):
        module = module_name(unit.path)
        bindings = imports_for(unit, fn, include_local=False)
        for identity, statement in bindings.values():
            import_index.setdefault(identity, []).append(
                {
                    "path": unit.path,
                    "line": statement.lineno,
                    "import_kind": "module_namespace" if isinstance(statement, ast.Import) else "symbol_binding",
                }
            )
        visitor = ExecutionNames()
        for expression in helper["_definition_expressions"](unit, fn):
            visitor.visit(expression)
        for name in visitor.names:
            identity = bindings[name][0] if name in bindings else "builtins." + name if name in vars(builtins) else module + "." + name
            runtime.setdefault(identity, []).append(
                {"path": unit.path, "method": qualified, "line": fn.lineno, "scope": "canonical class method definition"}
            )
            if name in visitor.members:
                member_uses.setdefault(identity, set()).update(visitor.members[name])
    records = []
    for identity in sorted(roots):
        role = object_index.get(identity)
        if role is None:
            if identity.startswith("builtins."):
                role = {
                    "kind": "immutable_builtin_referent_mutable_namespace_binding",
                    "namespace": "builtins",
                    "member": identity.removeprefix("builtins."),
                }
            elif identity in import_index:
                role = {
                    "kind": "imported_mutable_module_namespace_binding"
                    if any(site["import_kind"] == "module_namespace" for site in import_index[identity])
                    else "import_binding_target_requires_reexport_resolution",
                    "import_sites": import_index[identity],
                }
            elif identity in {"os.getpid"}:
                role = {"kind": "stdlib_member_binding", "namespace": "os", "member": "getpid"}
            else:
                role = {
                    "kind": "unresolved_supplied_identity",
                    "required_action": "resolve against complete supplied source universe or refuse",
                }
        uses = runtime.get(identity, [])
        annotations = annotation.get(identity, [])
        receiver = role["kind"].startswith("receiver_bound_")
        requirements = semantic_requirements.get(identity, [])
        annotation_only = bool(annotations) and not uses and not receiver and not requirements
        records.append(
            {
                "identity": identity,
                **role,
                "executed_global_uses": uses,
                "nonexecuting_annotation_uses": annotations,
                "annotation_only": annotation_only,
                "semantic_producer_requirements": requirements,
                "executed_selected_members": sorted(member_uses.get(identity, set())),
                "origin_index_seed": not receiver and not annotation_only,
                "receiver_identity_required": receiver,
                "semantics": "local contract covers actual operation; external receiver/descriptor mutation still requires SOURCE4"
                if receiver
                else "annotation reference has no executed dependency"
                if annotation_only
                else "required constructor/class origin independent of textual annotations; SOURCE5 must protect it"
                if requirements
                else "SOURCE5 must preserve this binding/producer and refuse relevant mutation or escape",
            }
        )
    return {
        "status": "TYPED_LOCAL_DEPENDENCIES_NOT_EFFECT_PROOF",
        "family_failures": failures,
        "records": records,
        "origin_seeds": [record["identity"] for record in records if record["origin_index_seed"]],
        "receiver_bound_members": [record for record in records if record["receiver_identity_required"]],
        "annotation_only": [record for record in records if record["annotation_only"]],
        "semantic_requirements": semantic_requirements,
    }
