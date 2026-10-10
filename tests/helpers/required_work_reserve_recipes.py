"""Workspace source recipe invalidation; not a receiver/authority admission.

Review.md supplies the semantic dependency argument. Structural equality seals
that reviewed recipe against changes but never proves a caller's runtime type.
"""

import ast

from elspeth_lints.core.ast_dump import stable_ast_dump

SELECTION = {
    "src/elspeth/web/required_work.py": (
        "RequiredAuthorityKind",
        "ComposerRequiredStage",
        "RequiredWorkSubphase",
        "RequiredWorkSource",
        "SOURCE_MAPPING",
        "_SOURCE_BY_ORDINAL",
        "_source_for_ordinal",
        "_uuid",
        "RequiredWorkAuthority",
        "RequiredWorkKey",
        "make_required_work_key",
        "RequiredWorkCoordinator.__init__",
        "RequiredWorkCoordinator.authority",
        "RequiredWorkCoordinator.reserve",
        "RequiredWorkTicket.__init__",
        "RequiredWorkBinding",
    ),
    "src/elspeth/contracts/session_operation.py": (
        "SessionOperationKind",
        "_require_nonblank",
        "_require_positive_int",
        "SessionOperationFence",
        "SessionOperationContext",
    ),
    "src/elspeth/contracts/errors.py": ("AuditIntegrityError",),
    "src/elspeth/web/operator_telemetry_installation.py": (
        "InstallationStage",
        "TelemetryInstallationRecord",
        "TelemetryInstallation.__init__",
        "TelemetryInstallation.assert_process",
        "TelemetryInstallation.reserve",
    ),
    "src/elspeth/web/operator_telemetry_custody.py": ("OperatorTelemetryCleanupOwner.assert_process",),
}


def selected(source, symbol):
    tree = ast.parse(source)
    parts = symbol.split(".")
    nodes = tree.body
    for part in parts:
        matches = [n for n in nodes if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == part]
        matches += [n for n in nodes if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == part for t in n.targets)]
        if len(matches) != 1:
            return None
        node = matches[0]
        nodes = node.body if isinstance(node, ast.ClassDef) else []
    return stable_ast_dump(node)


def snapshot(sources):
    result = {}
    for path, symbols in SELECTION.items():
        source = sources[path]
        tree = ast.parse(source)
        # Imports are executable binding facts. Include imports throughout the
        # file conservatively, including TYPE_CHECKING/local changes.
        result[path + ":module-bindings"] = tuple(
            stable_ast_dump(n) for n in tree.body if not isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        )
        result[path + ":imports"] = tuple(stable_ast_dump(n) for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom)))
        for symbol in symbols:
            result[path + ":" + symbol] = selected(source, symbol)
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef) and cls.name in {
                "RequiredWorkCoordinator",
                "TelemetryInstallation",
                "OwnedTestTelemetryInstallation",
                "OperatorTelemetryCleanupOwner",
            }:
                layout = ast.ClassDef(
                    name=cls.name,
                    bases=cls.bases,
                    keywords=cls.keywords,
                    body=[
                        n
                        for n in cls.body
                        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__slots__" for t in n.targets)
                    ]
                    or [ast.Pass()],
                    decorator_list=cls.decorator_list,
                    type_params=cls.type_params,
                )
                result[path + ":" + cls.name + ":storage-layout"] = stable_ast_dump(layout)
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name in {
                "RequiredWorkCoordinator",
                "RequiredWorkTicket",
                "TelemetryInstallation",
                "OperatorTelemetryCleanupOwner",
            }:
                result[path + ":" + node.name + ":descriptor-layout"] = tuple(
                    stable_ast_dump(n)
                    for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name in {"__getattribute__", "__getattr__", "__setattr__", "__hash__", "__eq__", "__new__"}
                )
    return result
