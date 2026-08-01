"""Regression tests for the runtime-VAL manifest builder."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from typing import ClassVar, TypedDict

import pytest

import elspeth.contracts.schema_contract as schema_contract_module
import elspeth.contracts.secret_scrub as secret_scrub_module
import elspeth.engine.executors.declared_output_fields as declared_output_fields_module
from elspeth.contracts.declaration_contracts import (
    AggregateDeclarationContractViolation,
    DeclarationContract,
    DeclarationContractViolation,
    DispatchSite,
    ExampleBundle,
    PostEmissionInputs,
    PostEmissionOutputs,
    implements_dispatch_site,
)
from elspeth.contracts.errors import DeclaredOutputFieldsViolation, DeclaredRequiredInputFieldsPayload, FrameworkBugError
from elspeth.contracts.runtime_val_manifest import (
    _callable_dependency_hashes,
    _callable_implementation_hash,
    _class_source_hash,
    _normalize_code_constant,
    _payload_schema_hash,
    build_runtime_val_manifest,
)
from elspeth.contracts.schema_contract import SchemaContract
from elspeth.engine.executors.pass_through import PassThroughDeclarationContract
from elspeth.engine.orchestrator import prepare_for_run


class _RuntimeValManifestIndirectPayload(TypedDict):
    value: str


class _RuntimeValManifestOrderedPayload(TypedDict):
    alpha: str
    bravo: int


class _RuntimeValManifestIndirectViolation(DeclarationContractViolation):
    payload_schema: ClassVar[type] = _RuntimeValManifestIndirectPayload


class _RuntimeValManifestOpaqueDependency:
    def __init__(self) -> None:
        self.factor = 1

    def compute(self) -> int:
        return self.factor


class _RuntimeValManifestHelperClass:
    @staticmethod
    def value() -> str:
        return _runtime_val_manifest_indirect_module_helper()

    @staticmethod
    def unused() -> str:
        return "unrelated"


class _RuntimeValManifestExternalOpaque:
    __module__ = "vendor.runtime_val_test"


class _RuntimeValManifestNominalBaseOne:
    pass


class _RuntimeValManifestNominalBaseTwo:
    pass


class _RuntimeValManifestNominal(_RuntimeValManifestNominalBaseOne):
    pass


class _RuntimeValManifestMetaclass(type):
    def __call__(cls) -> object:
        return super().__call__()


class _RuntimeValManifestMetaclassConstructed(metaclass=_RuntimeValManifestMetaclass):
    pass


class _RuntimeValManifestRecursiveConstructor:
    def __init__(self, recurse: bool = False) -> None:
        if recurse:
            _RuntimeValManifestRecursiveConstructor()


class _RuntimeValManifestOwnerChain:
    helper_cls = _RuntimeValManifestHelperClass

    def call_helper(self) -> str:
        return self.helper_cls.value()


class _RuntimeValManifestNestedClassCarrier:
    Nested = _RuntimeValManifestHelperClass


class _RuntimeValManifestMixedState:
    __slots__ = ("__dict__", "slot_value")

    def __init__(self) -> None:
        self.slot_value = 1

    def compute(self) -> int:
        return self.slot_value


class _RuntimeValManifestDescriptorChild:
    def compute(self) -> int:
        return 1


class _RuntimeValManifestDescriptorOwner:
    @property
    def child(self) -> _RuntimeValManifestDescriptorChild:
        return _RuntimeValManifestDescriptorChild()


class _RuntimeValManifestUninitializedSlot:
    __slots__ = ("value",)

    def marker(self) -> str:
        return "present"


_RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY = _RuntimeValManifestOpaqueDependency()
_RUNTIME_VAL_MANIFEST_MIXED_STATE = _RuntimeValManifestMixedState()
_RUNTIME_VAL_MANIFEST_DESCRIPTOR_OWNER = _RuntimeValManifestDescriptorOwner()
_RUNTIME_VAL_MANIFEST_UNINITIALIZED_SLOT = _RuntimeValManifestUninitializedSlot()
_RUNTIME_VAL_MANIFEST_EXTERNAL_OPAQUE = _RuntimeValManifestExternalOpaque()
_RUNTIME_VAL_MANIFEST_PATTERN = re.compile("before")


def _runtime_val_manifest_indirect_module_helper() -> str:
    return "before"


_runtime_val_manifest_indirect_module_helper.factor = 1  # type: ignore[attr-defined]


def _runtime_val_manifest_closure_factory(helper: Callable[[], str]) -> Callable[[], str]:
    def wrapped() -> str:
        return helper()

    return wrapped


def _runtime_val_manifest_mixed_bound_class_factory(
    cls: type[_RuntimeValManifestOpaqueDependency],
) -> Callable[[], object]:
    def wrapped() -> object:
        cls()
        return cls.compute

    return wrapped


def _runtime_val_manifest_transitive_helper() -> str:
    return _runtime_val_manifest_indirect_module_helper()


_runtime_val_manifest_transitive_helper.factor = 1  # type: ignore[attr-defined]


def _runtime_val_manifest_mixed_bound_function_factory(helper: Callable[[], str]) -> Callable[[], object]:
    def wrapped() -> object:
        return helper.factor, helper()  # type: ignore[attr-defined]

    return wrapped


def _runtime_val_manifest_duplicate_closure_factory(marker: str) -> Callable[[], str]:
    def duplicate() -> str:
        return marker

    return duplicate


def _runtime_val_manifest_nested_duplicate_closure_factory(marker: str) -> Callable[[], str]:
    def duplicate() -> str:
        if marker == "delegate":
            return _RUNTIME_VAL_MANIFEST_NESTED_DUPLICATE_TWO()
        return marker

    return duplicate


def _runtime_val_manifest_class_closure_factory(cls: type[_RuntimeValManifestHelperClass]) -> Callable[[], str]:
    def wrapped() -> str:
        return cls.value()

    return wrapped


def _runtime_val_manifest_constructor_closure_factory(
    cls: type[_RuntimeValManifestOpaqueDependency],
) -> Callable[[], _RuntimeValManifestOpaqueDependency]:
    def wrapped() -> _RuntimeValManifestOpaqueDependency:
        return cls()

    return wrapped


def _runtime_val_manifest_instance_closure_factory(
    dependency: _RuntimeValManifestOpaqueDependency,
) -> Callable[[], int]:
    def wrapped() -> int:
        return dependency.compute()

    return wrapped


def _runtime_val_manifest_container_closure_factory(
    classes: tuple[type[_RuntimeValManifestHelperClass], ...],
) -> Callable[[], str]:
    def wrapped() -> str:
        return classes[0].value()

    return wrapped


def _runtime_val_manifest_module_closure_factory(module: object) -> Callable[[], str]:
    def wrapped() -> str:
        return module.RuntimeValHelper.value()  # type: ignore[attr-defined,no-any-return]

    return wrapped


def _runtime_val_manifest_nested_class_closure_factory(
    cls: type[_RuntimeValManifestNestedClassCarrier],
) -> Callable[[], str]:
    def wrapped() -> str:
        return cls.Nested.value()

    return wrapped


_RUNTIME_VAL_MANIFEST_WRAPPED_HELPER = _runtime_val_manifest_closure_factory(_runtime_val_manifest_indirect_module_helper)
_RUNTIME_VAL_MANIFEST_DUPLICATE_CLOSURE_ONE = _runtime_val_manifest_duplicate_closure_factory("one")
_RUNTIME_VAL_MANIFEST_DUPLICATE_CLOSURE_TWO = _runtime_val_manifest_duplicate_closure_factory("two")
_RUNTIME_VAL_MANIFEST_NESTED_DUPLICATE_ONE = _runtime_val_manifest_nested_duplicate_closure_factory("delegate")
_RUNTIME_VAL_MANIFEST_NESTED_DUPLICATE_TWO = _runtime_val_manifest_nested_duplicate_closure_factory("two")
_RUNTIME_VAL_MANIFEST_WRAPPED_CLASS = _runtime_val_manifest_class_closure_factory(_RuntimeValManifestHelperClass)
_RUNTIME_VAL_MANIFEST_WRAPPED_CONSTRUCTOR = _runtime_val_manifest_constructor_closure_factory(_RuntimeValManifestOpaqueDependency)
_RUNTIME_VAL_MANIFEST_WRAPPED_INSTANCE = _runtime_val_manifest_instance_closure_factory(_RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY)
_RUNTIME_VAL_MANIFEST_WRAPPED_CONTAINER = _runtime_val_manifest_container_closure_factory((_RuntimeValManifestHelperClass,))
_RUNTIME_VAL_MANIFEST_WRAPPED_MODULE = _runtime_val_manifest_module_closure_factory(schema_contract_module)
_RUNTIME_VAL_MANIFEST_WRAPPED_NESTED_CLASS = _runtime_val_manifest_nested_class_closure_factory(_RuntimeValManifestNestedClassCarrier)
_RUNTIME_VAL_MANIFEST_MIXED_BOUND_CLASS = _runtime_val_manifest_mixed_bound_class_factory(_RuntimeValManifestOpaqueDependency)
_RUNTIME_VAL_MANIFEST_MIXED_BOUND_FUNCTION = _runtime_val_manifest_mixed_bound_function_factory(_runtime_val_manifest_transitive_helper)


def _runtime_val_manifest_attribute_name_collision(value: object) -> object:
    return value._runtime_val_manifest_indirect_module_helper()  # type: ignore[attr-defined]


def _runtime_val_manifest_module_alias_dependency() -> object:
    return declared_output_fields_module.verify_declared_output_fields


def _runtime_val_manifest_module_qualified_constructor_dependency() -> object:
    return schema_contract_module.SchemaContract(mode="DISCOVER", fields=(), locked=False)


def _runtime_val_manifest_opaque_dependency_root() -> int:
    return _RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY.factor


def _runtime_val_manifest_opaque_dependency_method_root() -> int:
    return _RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY.compute()


def _runtime_val_manifest_mixed_state_method_root() -> int:
    return _RUNTIME_VAL_MANIFEST_MIXED_STATE.compute()


def _runtime_val_manifest_descriptor_chain_root() -> int:
    return _RUNTIME_VAL_MANIFEST_DESCRIPTOR_OWNER.child.compute()


def _runtime_val_manifest_uninitialized_slot_root() -> str:
    return _RUNTIME_VAL_MANIFEST_UNINITIALIZED_SLOT.marker()


def _runtime_val_manifest_helper_class_root() -> str:
    return _RuntimeValManifestHelperClass.value()


def _runtime_val_manifest_pattern_root(value: str) -> bool:
    return _RUNTIME_VAL_MANIFEST_PATTERN.search(value) is not None


def _runtime_val_manifest_duplicate_closure_root() -> str:
    return _RUNTIME_VAL_MANIFEST_DUPLICATE_CLOSURE_ONE() + _RUNTIME_VAL_MANIFEST_DUPLICATE_CLOSURE_TWO()


def _runtime_val_manifest_external_opaque_root() -> object:
    return _RUNTIME_VAL_MANIFEST_EXTERNAL_OPAQUE


def _runtime_val_manifest_function_attribute_root() -> object:
    return _runtime_val_manifest_indirect_module_helper.factor  # type: ignore[attr-defined,no-any-return]


def _runtime_val_manifest_nominal_root(value: object) -> bool:
    return isinstance(value, _RuntimeValManifestNominal)


def _runtime_val_manifest_metaclass_constructor_root() -> object:
    return _RuntimeValManifestMetaclassConstructed()


def _runtime_val_manifest_repeated_metaclass_binding_root() -> type[_RuntimeValManifestMetaclassConstructed]:
    _RuntimeValManifestMetaclassConstructed()
    return _RuntimeValManifestMetaclassConstructed


def _runtime_val_manifest_recursive_constructor_root() -> object:
    return _RuntimeValManifestRecursiveConstructor(True)


def _runtime_val_manifest_direct_constructor_root() -> _RuntimeValManifestOpaqueDependency:
    return _RuntimeValManifestOpaqueDependency()


def _runtime_val_manifest_default_class_root(
    cls: type[_RuntimeValManifestHelperClass] = _RuntimeValManifestHelperClass,
) -> str:
    return cls.value()


def _runtime_val_manifest_default_constructor_root(
    cls: type[_RuntimeValManifestOpaqueDependency] = _RuntimeValManifestOpaqueDependency,
) -> _RuntimeValManifestOpaqueDependency:
    return cls()


def _runtime_val_manifest_default_instance_container_root(
    dependencies: tuple[_RuntimeValManifestOpaqueDependency, ...] = (_RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY,),
) -> int:
    return dependencies[0].compute()


def _runtime_val_manifest_mixed_nested_constructor_root() -> Callable[[], type[_RuntimeValManifestOpaqueDependency]]:
    _RuntimeValManifestOpaqueDependency()

    def nested() -> type[_RuntimeValManifestOpaqueDependency]:
        return _RuntimeValManifestOpaqueDependency

    return nested


def _runtime_val_manifest_local_class_alias_root() -> str:
    alias = _RuntimeValManifestHelperClass
    return alias.value()


def _runtime_val_manifest_local_instance_alias_root() -> int:
    alias = _RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY
    return alias.compute()


@pytest.fixture()
def _isolate_runtime_val_registries() -> Iterator[None]:
    """Restore both registries after each test mutates freeze or membership state."""
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    saved_dc_registry = list(dc._REGISTRY)
    saved_dc_per_site = {site: list(lst) for site, lst in dc._REGISTRY_BY_SITE.items()}
    saved_dc_frozen = dc._FROZEN

    saved_tr_registry = list(tr._REGISTRY)
    saved_tr_reasons = dict(tr._REASONS)
    saved_tr_frozen = tr._FROZEN

    yield

    dc._REGISTRY.clear()
    dc._REGISTRY.extend(saved_dc_registry)
    for site, lst in saved_dc_per_site.items():
        dc._REGISTRY_BY_SITE[site][:] = lst
    dc._FROZEN = saved_dc_frozen

    tr._REGISTRY.clear()
    tr._REGISTRY.extend(saved_tr_registry)
    tr._REASONS.clear()
    tr._REASONS.update(saved_tr_reasons)
    tr._FROZEN = saved_tr_frozen


def test_build_runtime_val_manifest_requires_frozen_registries(_isolate_runtime_val_registries: None) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    dc._FROZEN = False
    tr._FROZEN = False

    with pytest.raises(FrameworkBugError, match="frozen"):
        build_runtime_val_manifest()


def test_manifest_records_declaration_contract_implementation_hash(
    _isolate_runtime_val_registries: None,
) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    dc._FROZEN = False
    tr._FROZEN = False
    prepare_for_run()

    baseline = build_runtime_val_manifest()
    baseline_entry = next(entry for entry in baseline["declaration_contracts"] if entry["name"] == "passes_through_input")

    original_code = PassThroughDeclarationContract.post_emission_check.__code__

    def replacement(self, inputs, outputs):
        return None

    PassThroughDeclarationContract.post_emission_check.__code__ = replacement.__code__
    try:
        mutated = build_runtime_val_manifest()
    finally:
        PassThroughDeclarationContract.post_emission_check.__code__ = original_code

    mutated_entry = next(entry for entry in mutated["declaration_contracts"] if entry["name"] == "passes_through_input")

    assert baseline_entry["name"] == mutated_entry["name"]
    assert baseline_entry["class_name"] == mutated_entry["class_name"]
    assert baseline_entry["class_module"] == mutated_entry["class_module"]
    assert baseline_entry["dispatch_sites"] == mutated_entry["dispatch_sites"]
    assert baseline_entry["implementation_hash"] != mutated_entry["implementation_hash"]


def test_manifest_ignores_declaration_contract_method_docstring_only_edits(
    _isolate_runtime_val_registries: None,
) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    dc._FROZEN = False
    tr._FROZEN = False
    prepare_for_run()

    original_code = PassThroughDeclarationContract.post_emission_check.__code__

    def replacement_without_docstring(self, inputs, outputs):
        return None

    def replacement_with_docstring(self, inputs, outputs):
        """Replacement whose only semantic difference is this docstring."""
        return None

    try:
        PassThroughDeclarationContract.post_emission_check.__code__ = replacement_without_docstring.__code__
        baseline = build_runtime_val_manifest()

        PassThroughDeclarationContract.post_emission_check.__code__ = replacement_with_docstring.__code__
        mutated = build_runtime_val_manifest()
    finally:
        PassThroughDeclarationContract.post_emission_check.__code__ = original_code

    baseline_entry = next(entry for entry in baseline["declaration_contracts"] if entry["name"] == "passes_through_input")
    mutated_entry = next(entry for entry in mutated["declaration_contracts"] if entry["name"] == "passes_through_input")

    assert baseline_entry["name"] == mutated_entry["name"]
    assert baseline_entry["class_name"] == mutated_entry["class_name"]
    assert baseline_entry["class_module"] == mutated_entry["class_module"]
    assert baseline_entry["dispatch_sites"] == mutated_entry["dispatch_sites"]
    assert baseline_entry["implementation_hash"] == mutated_entry["implementation_hash"]


def test_manifest_records_delegated_declaration_helper_implementation_hash(
    _isolate_runtime_val_registries: None,
) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    dc._FROZEN = False
    tr._FROZEN = False
    prepare_for_run()

    baseline = build_runtime_val_manifest()
    baseline_entry = next(entry for entry in baseline["declaration_contracts"] if entry["name"] == "declared_output_fields")

    original_code = declared_output_fields_module.verify_declared_output_fields.__code__

    def replacement(
        *,
        declared_output_fields: frozenset[str],
        emitted_rows: object,
        plugin_name: str,
        node_id: str,
        run_id: str,
        row_id: str,
        token_id: str,
    ) -> None:
        del declared_output_fields, emitted_rows, plugin_name, node_id, run_id, row_id, token_id
        return None

    declared_output_fields_module.verify_declared_output_fields.__code__ = replacement.__code__
    try:
        mutated = build_runtime_val_manifest()
    finally:
        declared_output_fields_module.verify_declared_output_fields.__code__ = original_code

    mutated_entry = next(entry for entry in mutated["declaration_contracts"] if entry["name"] == "declared_output_fields")

    assert baseline_entry["name"] == mutated_entry["name"]
    assert baseline_entry["class_name"] == mutated_entry["class_name"]
    assert baseline_entry["class_module"] == mutated_entry["class_module"]
    assert baseline_entry["dispatch_sites"] == mutated_entry["dispatch_sites"]
    assert baseline_entry["implementation_hash"] != mutated_entry["implementation_hash"]


def test_manifest_records_declaration_helper_global_constant_drift(
    _isolate_runtime_val_registries: None,
) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    dc._FROZEN = False
    tr._FROZEN = False
    prepare_for_run()

    baseline = build_runtime_val_manifest()
    baseline_entry = next(entry for entry in baseline["declaration_contracts"] if entry["name"] == "declared_output_fields")

    original_limit = declared_output_fields_module._MAX_VIOLATION_SAMPLES
    declared_output_fields_module._MAX_VIOLATION_SAMPLES = original_limit + 1
    try:
        mutated = build_runtime_val_manifest()
    finally:
        declared_output_fields_module._MAX_VIOLATION_SAMPLES = original_limit

    mutated_entry = next(entry for entry in mutated["declaration_contracts"] if entry["name"] == "declared_output_fields")
    assert baseline_entry["implementation_hash"] != mutated_entry["implementation_hash"]


def test_manifest_records_indirect_method_helper_implementation_hash(
    _isolate_runtime_val_registries: None,
) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    dc._FROZEN = False
    tr._FROZEN = False

    class _IndirectHelperContract(DeclarationContract):
        name: ClassVar[str] = "indirect_method_helper"
        payload_schema: ClassVar[type] = _RuntimeValManifestIndirectPayload
        violation_class: ClassVar[type[DeclarationContractViolation]] = _RuntimeValManifestIndirectViolation

        def applies_to(self, plugin: object) -> bool:
            return bool(plugin)

        @implements_dispatch_site("post_emission_check")
        def post_emission_check(
            self,
            inputs: PostEmissionInputs,
            outputs: PostEmissionOutputs,
        ) -> None:
            del inputs, outputs
            self._validate_indirectly()

        def _validate_indirectly(self) -> None:
            _runtime_val_manifest_indirect_module_helper()

        @classmethod
        def negative_example(cls) -> ExampleBundle:
            raise NotImplementedError

        @classmethod
        def positive_example_does_not_apply(cls) -> ExampleBundle:
            raise NotImplementedError

    contract = _IndirectHelperContract()
    dc._REGISTRY.append(contract)
    dc._REGISTRY_BY_SITE[DispatchSite.POST_EMISSION].append(contract)
    dc._FROZEN = True
    tr._FROZEN = True

    baseline = build_runtime_val_manifest()
    baseline_entry = next(entry for entry in baseline["declaration_contracts"] if entry["name"] == "indirect_method_helper")

    original_code = _runtime_val_manifest_indirect_module_helper.__code__

    def replacement() -> str:
        return "after"

    _runtime_val_manifest_indirect_module_helper.__code__ = replacement.__code__
    try:
        mutated = build_runtime_val_manifest()
    finally:
        _runtime_val_manifest_indirect_module_helper.__code__ = original_code

    mutated_entry = next(entry for entry in mutated["declaration_contracts"] if entry["name"] == "indirect_method_helper")

    assert baseline_entry["name"] == mutated_entry["name"]
    assert baseline_entry["class_name"] == mutated_entry["class_name"]
    assert baseline_entry["class_module"] == mutated_entry["class_module"]
    assert baseline_entry["dispatch_sites"] == mutated_entry["dispatch_sites"]
    assert baseline_entry["implementation_hash"] != mutated_entry["implementation_hash"]


def test_callable_dependency_hashes_ignore_attribute_name_collisions() -> None:
    class _Owner:
        def _runtime_val_manifest_indirect_module_helper(self) -> str:
            return "unrelated"

    dependencies = _callable_dependency_hashes(_runtime_val_manifest_attribute_name_collision, owner_cls=_Owner)

    assert dependencies == {}


def test_callable_dependency_hashes_record_module_qualified_helpers() -> None:
    dependencies = _callable_dependency_hashes(_runtime_val_manifest_module_alias_dependency)

    assert any(key.endswith(":verify_declared_output_fields") for key in dependencies)


def test_callable_dependency_hashes_record_module_qualified_owned_constructors() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_module_qualified_constructor_dependency)
    constructor = SchemaContract.__init__
    closure = constructor.__closure__
    assert closure is not None
    factory_index = constructor.__code__.co_freevars.index("__dataclass_dflt__by_normalized__")
    factory_cell = closure[factory_index]
    original_factory = factory_cell.cell_contents
    factory_cell.cell_contents = list
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_module_qualified_constructor_dependency)
    finally:
        factory_cell.cell_contents = original_factory

    assert baseline != mutated


def test_callable_dependency_hashes_record_nested_module_class_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schema_contract_module, "RuntimeValHelper", _RuntimeValManifestHelperClass, raising=False)

    def dependency_root() -> str:
        return schema_contract_module.RuntimeValHelper.value()  # type: ignore[attr-defined,no-any-return]

    baseline = _callable_dependency_hashes(dependency_root)
    original_code = _RuntimeValManifestHelperClass.value.__code__

    def replacement() -> str:
        return "after"

    _RuntimeValManifestHelperClass.value.__code__ = replacement.__code__
    try:
        mutated = _callable_dependency_hashes(dependency_root)
    finally:
        _RuntimeValManifestHelperClass.value.__code__ = original_code

    assert baseline != mutated


def test_callable_dependency_hashes_record_owned_global_instance_state() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_opaque_dependency_root)
    original_factor = _RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY.factor
    _RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY.factor = original_factor + 1
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_opaque_dependency_root)
    finally:
        _RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY.factor = original_factor

    assert baseline != mutated


def test_callable_dependency_hashes_ignore_unreferenced_owned_global_instance_state() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_opaque_dependency_root)
    _RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY.unused = "before"  # type: ignore[attr-defined]
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_opaque_dependency_root)
    finally:
        del _RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY.unused  # type: ignore[attr-defined]

    assert baseline == mutated


def test_callable_dependency_hashes_record_owned_global_instance_method_implementation() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_opaque_dependency_method_root)
    original_code = _RuntimeValManifestOpaqueDependency.compute.__code__

    def replacement(self) -> int:
        return self.factor + 1

    _RuntimeValManifestOpaqueDependency.compute.__code__ = replacement.__code__
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_opaque_dependency_method_root)
    finally:
        _RuntimeValManifestOpaqueDependency.compute.__code__ = original_code

    assert baseline != mutated


def test_callable_dependency_hashes_record_owned_global_instance_method_receiver_state() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_opaque_dependency_method_root)
    original_factor = _RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY.factor
    _RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY.factor = original_factor + 1
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_opaque_dependency_method_root)
    finally:
        _RUNTIME_VAL_MANIFEST_OPAQUE_DEPENDENCY.factor = original_factor

    assert baseline != mutated


def test_callable_dependency_hashes_record_owned_global_helper_class_implementation() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_helper_class_root)
    original_code = _RuntimeValManifestHelperClass.value.__code__

    def replacement() -> str:
        return "after"

    _RuntimeValManifestHelperClass.value.__code__ = replacement.__code__
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_helper_class_root)
    finally:
        _RuntimeValManifestHelperClass.value.__code__ = original_code

    assert baseline != mutated


def test_callable_dependency_hashes_ignore_unreferenced_helper_class_methods() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_helper_class_root)
    original_code = _RuntimeValManifestHelperClass.unused.__code__

    def replacement() -> str:
        return "changed but still unrelated"

    _RuntimeValManifestHelperClass.unused.__code__ = replacement.__code__
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_helper_class_root)
    finally:
        _RuntimeValManifestHelperClass.unused.__code__ = original_code

    assert baseline == mutated


def test_callable_dependency_hashes_record_helper_class_transitive_dependencies() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_helper_class_root)
    original_code = _runtime_val_manifest_indirect_module_helper.__code__

    def replacement() -> str:
        return "after"

    _runtime_val_manifest_indirect_module_helper.__code__ = replacement.__code__
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_helper_class_root)
    finally:
        _runtime_val_manifest_indirect_module_helper.__code__ = original_code

    assert baseline != mutated


def test_callable_dependency_hashes_record_direct_owned_constructor_implementation() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_direct_constructor_root)
    original_code = _RuntimeValManifestOpaqueDependency.__init__.__code__

    def replacement(self) -> None:
        self.factor = 2

    _RuntimeValManifestOpaqueDependency.__init__.__code__ = replacement.__code__
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_direct_constructor_root)
    finally:
        _RuntimeValManifestOpaqueDependency.__init__.__code__ = original_code

    assert baseline != mutated


def test_callable_dependency_hashes_record_nominal_class_mro() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_nominal_root)
    original_bases = _RuntimeValManifestNominal.__bases__
    _RuntimeValManifestNominal.__bases__ = (_RuntimeValManifestNominalBaseTwo,)
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_nominal_root)
    finally:
        _RuntimeValManifestNominal.__bases__ = original_bases

    assert baseline != mutated


def test_callable_dependency_hashes_record_custom_metaclass_call_implementation() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_metaclass_constructor_root)
    original_code = _RuntimeValManifestMetaclass.__call__.__code__

    class _ReplacementMetaclass(type):
        def __call__(cls) -> object:
            super()
            raise AssertionError("replacement should only be hashed")

    _RuntimeValManifestMetaclass.__call__.__code__ = _ReplacementMetaclass.__call__.__code__
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_metaclass_constructor_root)
    finally:
        _RuntimeValManifestMetaclass.__call__.__code__ = original_code

    assert baseline != mutated


def test_callable_dependency_hashes_merge_repeated_qualified_binding_uses() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_repeated_metaclass_binding_root)
    original_code = _RuntimeValManifestMetaclass.__call__.__code__

    class _ReplacementMetaclass(type):
        def __call__(cls) -> object:
            super()
            raise AssertionError("replacement should only be hashed")

    _RuntimeValManifestMetaclass.__call__.__code__ = _ReplacementMetaclass.__call__.__code__
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_repeated_metaclass_binding_root)
    finally:
        _RuntimeValManifestMetaclass.__call__.__code__ = original_code

    assert baseline != mutated


def test_callable_implementation_hash_merges_qualified_and_direct_bound_class_uses() -> None:
    baseline = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_MIXED_BOUND_CLASS)
    original_code = _RuntimeValManifestOpaqueDependency.__init__.__code__

    def replacement(self) -> None:
        self.factor = 2

    _RuntimeValManifestOpaqueDependency.__init__.__code__ = replacement.__code__
    try:
        mutated = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_MIXED_BOUND_CLASS)
    finally:
        _RuntimeValManifestOpaqueDependency.__init__.__code__ = original_code

    assert baseline != mutated


def test_callable_implementation_hash_merges_qualified_and_direct_bound_function_uses() -> None:
    baseline = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_MIXED_BOUND_FUNCTION)
    original_code = _runtime_val_manifest_indirect_module_helper.__code__

    def replacement() -> str:
        return "after"

    _runtime_val_manifest_indirect_module_helper.__code__ = replacement.__code__
    try:
        mutated = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_MIXED_BOUND_FUNCTION)
    finally:
        _runtime_val_manifest_indirect_module_helper.__code__ = original_code

    assert baseline != mutated


def test_callable_dependency_hashes_terminate_recursive_constructor_bindings() -> None:
    assert _callable_dependency_hashes(_runtime_val_manifest_recursive_constructor_root)


def test_callable_implementation_hash_records_captured_owned_class_method() -> None:
    baseline = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_CLASS)
    original_code = _RuntimeValManifestHelperClass.value.__code__

    def replacement() -> str:
        return "after"

    _RuntimeValManifestHelperClass.value.__code__ = replacement.__code__
    try:
        mutated = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_CLASS)
    finally:
        _RuntimeValManifestHelperClass.value.__code__ = original_code

    assert baseline != mutated


def test_callable_implementation_hash_records_default_owned_class_method() -> None:
    baseline = _callable_implementation_hash(_runtime_val_manifest_default_class_root)
    original_code = _RuntimeValManifestHelperClass.value.__code__

    def replacement() -> str:
        return "after"

    _RuntimeValManifestHelperClass.value.__code__ = replacement.__code__
    try:
        mutated = _callable_implementation_hash(_runtime_val_manifest_default_class_root)
    finally:
        _RuntimeValManifestHelperClass.value.__code__ = original_code

    assert baseline != mutated


def test_callable_implementation_hash_records_default_owned_constructor() -> None:
    baseline = _callable_implementation_hash(_runtime_val_manifest_default_constructor_root)
    original_code = _RuntimeValManifestOpaqueDependency.__init__.__code__

    def replacement(self) -> None:
        self.factor = 2

    _RuntimeValManifestOpaqueDependency.__init__.__code__ = replacement.__code__
    try:
        mutated = _callable_implementation_hash(_runtime_val_manifest_default_constructor_root)
    finally:
        _RuntimeValManifestOpaqueDependency.__init__.__code__ = original_code

    assert baseline != mutated


def test_callable_implementation_hash_records_closure_owned_constructor() -> None:
    baseline = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_CONSTRUCTOR)
    original_code = _RuntimeValManifestOpaqueDependency.__init__.__code__

    def replacement(self) -> None:
        self.factor = 2

    _RuntimeValManifestOpaqueDependency.__init__.__code__ = replacement.__code__
    try:
        mutated = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_CONSTRUCTOR)
    finally:
        _RuntimeValManifestOpaqueDependency.__init__.__code__ = original_code

    assert baseline != mutated


def test_callable_implementation_hash_records_closure_owned_instance_method() -> None:
    baseline = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_INSTANCE)
    original_code = _RuntimeValManifestOpaqueDependency.compute.__code__

    def replacement(self) -> int:
        return self.factor + 1

    _RuntimeValManifestOpaqueDependency.compute.__code__ = replacement.__code__
    try:
        mutated = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_INSTANCE)
    finally:
        _RuntimeValManifestOpaqueDependency.compute.__code__ = original_code

    assert baseline != mutated


def test_callable_implementation_hash_records_class_inside_closure_container() -> None:
    baseline = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_CONTAINER)
    original_code = _RuntimeValManifestHelperClass.value.__code__

    def replacement() -> str:
        return "after"

    _RuntimeValManifestHelperClass.value.__code__ = replacement.__code__
    try:
        mutated = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_CONTAINER)
    finally:
        _RuntimeValManifestHelperClass.value.__code__ = original_code

    assert baseline != mutated


def test_callable_implementation_hash_records_module_inside_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schema_contract_module, "RuntimeValHelper", _RuntimeValManifestHelperClass, raising=False)
    baseline = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_MODULE)
    original_code = _RuntimeValManifestHelperClass.value.__code__

    def replacement() -> str:
        return "after"

    _RuntimeValManifestHelperClass.value.__code__ = replacement.__code__
    try:
        mutated = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_MODULE)
    finally:
        _RuntimeValManifestHelperClass.value.__code__ = original_code

    assert baseline != mutated


def test_callable_implementation_hash_records_nested_class_inside_closure() -> None:
    baseline = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_NESTED_CLASS)
    original_code = _RuntimeValManifestHelperClass.value.__code__

    def replacement() -> str:
        return "after"

    _RuntimeValManifestHelperClass.value.__code__ = replacement.__code__
    try:
        mutated = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_NESTED_CLASS)
    finally:
        _RuntimeValManifestHelperClass.value.__code__ = original_code

    assert baseline != mutated


def test_callable_implementation_hash_records_instance_inside_default_container() -> None:
    baseline = _callable_implementation_hash(_runtime_val_manifest_default_instance_container_root)
    original_code = _RuntimeValManifestOpaqueDependency.compute.__code__

    def replacement(self) -> int:
        return self.factor + 1

    _RuntimeValManifestOpaqueDependency.compute.__code__ = replacement.__code__
    try:
        mutated = _callable_implementation_hash(_runtime_val_manifest_default_instance_container_root)
    finally:
        _RuntimeValManifestOpaqueDependency.compute.__code__ = original_code

    assert baseline != mutated


def test_callable_implementation_hash_rejects_cyclic_default_container() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)

    def dependency_root(value: object = None) -> None:
        del value

    dependency_root.__defaults__ = (cyclic,)

    with pytest.raises(FrameworkBugError, match="cyclic list"):
        _callable_implementation_hash(dependency_root)


@pytest.mark.parametrize(
    ("dependency_root", "mutated_method"),
    [
        (_runtime_val_manifest_local_class_alias_root, _RuntimeValManifestHelperClass.value),
        (_runtime_val_manifest_local_instance_alias_root, _RuntimeValManifestOpaqueDependency.compute),
    ],
)
def test_callable_dependency_hashes_record_local_alias_method_implementation(
    dependency_root: Callable[[], object],
    mutated_method: Callable[..., object],
) -> None:
    baseline = _callable_dependency_hashes(dependency_root)
    original_code = mutated_method.__code__

    def replacement(*args: object, **kwargs: object) -> object:
        del args, kwargs
        return "after"

    mutated_method.__code__ = replacement.__code__
    try:
        mutated = _callable_dependency_hashes(dependency_root)
    finally:
        mutated_method.__code__ = original_code

    assert baseline != mutated


def test_callable_dependency_hashes_record_local_class_alias_transitive_dependencies() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_local_class_alias_root)
    original_code = _runtime_val_manifest_indirect_module_helper.__code__

    def replacement() -> str:
        return "after"

    _runtime_val_manifest_indirect_module_helper.__code__ = replacement.__code__
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_local_class_alias_root)
    finally:
        _runtime_val_manifest_indirect_module_helper.__code__ = original_code

    assert baseline != mutated


def test_callable_dependency_hashes_record_mixed_dict_and_slot_receiver_state() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_mixed_state_method_root)
    original_slot_value = _RUNTIME_VAL_MANIFEST_MIXED_STATE.slot_value
    _RUNTIME_VAL_MANIFEST_MIXED_STATE.slot_value = 2
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_mixed_state_method_root)
    finally:
        _RUNTIME_VAL_MANIFEST_MIXED_STATE.slot_value = original_slot_value

    assert baseline != mutated


def test_callable_dependency_hashes_fail_closed_on_descriptor_chains() -> None:
    with pytest.raises(FrameworkBugError, match="cannot statically resolve descriptor chain"):
        _callable_dependency_hashes(_runtime_val_manifest_descriptor_chain_root)


def test_callable_dependency_hashes_record_uninitialized_owned_slots_explicitly() -> None:
    dependencies = _callable_dependency_hashes(_runtime_val_manifest_uninitialized_slot_root)

    assert '"key": "uninitialized_slot", "value": true' in json.dumps(dependencies, sort_keys=True)
    assert dependencies == _callable_dependency_hashes(_runtime_val_manifest_uninitialized_slot_root)


def test_callable_dependency_hashes_do_not_overwrite_outer_constructor_with_nested_nominal_use() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_mixed_nested_constructor_root)
    original_code = _RuntimeValManifestOpaqueDependency.__init__.__code__

    def replacement(self) -> None:
        self.factor = 2

    _RuntimeValManifestOpaqueDependency.__init__.__code__ = replacement.__code__
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_mixed_nested_constructor_root)
    finally:
        _RuntimeValManifestOpaqueDependency.__init__.__code__ = original_code

    assert baseline != mutated


def test_callable_dependency_hashes_preserve_distinct_same_qualname_closures() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_duplicate_closure_root)
    closure = _RUNTIME_VAL_MANIFEST_DUPLICATE_CLOSURE_TWO.__closure__
    assert closure is not None
    marker_index = _RUNTIME_VAL_MANIFEST_DUPLICATE_CLOSURE_TWO.__code__.co_freevars.index("marker")
    marker_cell = closure[marker_index]
    original_marker = marker_cell.cell_contents
    marker_cell.cell_contents = "after"
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_duplicate_closure_root)
    finally:
        marker_cell.cell_contents = original_marker

    assert baseline != mutated


def test_callable_dependency_hashes_preserve_nested_distinct_same_qualname_closures() -> None:
    baseline = _callable_dependency_hashes(_RUNTIME_VAL_MANIFEST_NESTED_DUPLICATE_ONE)
    closure = _RUNTIME_VAL_MANIFEST_NESTED_DUPLICATE_TWO.__closure__
    assert closure is not None
    marker_index = _RUNTIME_VAL_MANIFEST_NESTED_DUPLICATE_TWO.__code__.co_freevars.index("marker")
    marker_cell = closure[marker_index]
    original_marker = marker_cell.cell_contents
    marker_cell.cell_contents = "after"
    try:
        mutated = _callable_dependency_hashes(_RUNTIME_VAL_MANIFEST_NESTED_DUPLICATE_ONE)
    finally:
        marker_cell.cell_contents = original_marker

    assert baseline != mutated


def test_callable_dependency_hashes_record_function_attribute_state() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_function_attribute_root)
    original_factor = _runtime_val_manifest_indirect_module_helper.factor  # type: ignore[attr-defined]
    _runtime_val_manifest_indirect_module_helper.factor = 2  # type: ignore[attr-defined]
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_function_attribute_root)
    finally:
        _runtime_val_manifest_indirect_module_helper.factor = original_factor  # type: ignore[attr-defined]

    assert baseline != mutated


def test_callable_dependency_hashes_ignore_uninvoked_function_body_for_attribute_only_use() -> None:
    baseline = _callable_dependency_hashes(_runtime_val_manifest_function_attribute_root)
    original_code = _runtime_val_manifest_indirect_module_helper.__code__

    def replacement() -> str:
        return "after"

    _runtime_val_manifest_indirect_module_helper.__code__ = replacement.__code__
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_function_attribute_root)
    finally:
        _runtime_val_manifest_indirect_module_helper.__code__ = original_code

    assert baseline == mutated


def test_callable_dependency_hashes_record_owner_chained_helper_method() -> None:
    baseline = _callable_dependency_hashes(_RuntimeValManifestOwnerChain.call_helper, owner_cls=_RuntimeValManifestOwnerChain)
    original_code = _RuntimeValManifestHelperClass.value.__code__

    def replacement() -> str:
        return "after"

    _RuntimeValManifestHelperClass.value.__code__ = replacement.__code__
    try:
        mutated = _callable_dependency_hashes(
            _RuntimeValManifestOwnerChain.call_helper,
            owner_cls=_RuntimeValManifestOwnerChain,
        )
    finally:
        _RuntimeValManifestHelperClass.value.__code__ = original_code

    assert baseline != mutated


def test_callable_implementation_hash_records_captured_helper_transitive_dependencies() -> None:
    baseline = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_HELPER)
    original_code = _runtime_val_manifest_indirect_module_helper.__code__

    def replacement() -> str:
        return "after"

    _runtime_val_manifest_indirect_module_helper.__code__ = replacement.__code__
    try:
        mutated = _callable_implementation_hash(_RUNTIME_VAL_MANIFEST_WRAPPED_HELPER)
    finally:
        _runtime_val_manifest_indirect_module_helper.__code__ = original_code

    assert baseline != mutated


def test_callable_dependency_hashes_reject_unapproved_external_opaque_globals() -> None:
    with pytest.raises(FrameworkBugError, match="cannot deterministically normalize global dependency"):
        _callable_dependency_hashes(_runtime_val_manifest_external_opaque_root)


def test_callable_dependency_hashes_record_regex_global_drift() -> None:
    global _RUNTIME_VAL_MANIFEST_PATTERN

    baseline = _callable_dependency_hashes(_runtime_val_manifest_pattern_root)
    original_pattern = _RUNTIME_VAL_MANIFEST_PATTERN
    _RUNTIME_VAL_MANIFEST_PATTERN = re.compile("after")
    try:
        mutated = _callable_dependency_hashes(_runtime_val_manifest_pattern_root)
    finally:
        _RUNTIME_VAL_MANIFEST_PATTERN = original_pattern

    assert baseline != mutated


def test_manifest_rejects_source_unavailable_classes(
    monkeypatch: pytest.MonkeyPatch,
    _isolate_runtime_val_registries: None,
) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    dc._FROZEN = False
    tr._FROZEN = False
    prepare_for_run()

    def source_unavailable(cls: type[object]) -> str:
        raise OSError(f"source unavailable for {cls.__module__}.{cls.__qualname__}")

    monkeypatch.setattr("elspeth.contracts.runtime_val_manifest.inspect.getsource", source_unavailable)

    with pytest.raises(FrameworkBugError, match="source unavailable"):
        build_runtime_val_manifest()


def test_manifest_records_tier_1_implementation_hash(_isolate_runtime_val_registries: None) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr
    from elspeth.contracts.tier_registry import tier_1_error

    dc._FROZEN = False
    tr._FROZEN = False

    @tier_1_error(reason="test runtime-val manifest implementation drift", caller_module=__name__)
    class _TempTier1Error(Exception):
        def describe(self) -> str:
            return "before"

    prepare_for_run()

    baseline = build_runtime_val_manifest()
    baseline_entry = next(
        entry for entry in baseline["tier_1_errors"] if entry["class_name"] == "_TempTier1Error" and entry["class_module"] == __name__
    )

    original_code = _TempTier1Error.describe.__code__

    def replacement(self):
        return "after"

    _TempTier1Error.describe.__code__ = replacement.__code__
    try:
        mutated = build_runtime_val_manifest()
    finally:
        _TempTier1Error.describe.__code__ = original_code

    mutated_entry = next(
        entry for entry in mutated["tier_1_errors"] if entry["class_name"] == "_TempTier1Error" and entry["class_module"] == __name__
    )

    assert baseline_entry["class_name"] == mutated_entry["class_name"]
    assert baseline_entry["class_module"] == mutated_entry["class_module"]
    assert baseline_entry["reason"] == mutated_entry["reason"]
    assert baseline_entry["implementation_hash"] != mutated_entry["implementation_hash"]


def test_manifest_records_tier_1_helper_implementation_hash(_isolate_runtime_val_registries: None) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr
    from elspeth.contracts.tier_registry import tier_1_error

    dc._FROZEN = False
    tr._FROZEN = False

    @tier_1_error(reason="test Tier-1 helper drift", caller_module=__name__)
    class _TempTier1HelperError(Exception):
        def describe(self) -> str:
            return _runtime_val_manifest_indirect_module_helper()

    prepare_for_run()

    baseline = build_runtime_val_manifest()
    baseline_entry = next(
        entry for entry in baseline["tier_1_errors"] if entry["class_name"] == "_TempTier1HelperError" and entry["class_module"] == __name__
    )

    original_code = _runtime_val_manifest_indirect_module_helper.__code__

    def replacement() -> str:
        return "after"

    _runtime_val_manifest_indirect_module_helper.__code__ = replacement.__code__
    try:
        mutated = build_runtime_val_manifest()
    finally:
        _runtime_val_manifest_indirect_module_helper.__code__ = original_code

    mutated_entry = next(
        entry for entry in mutated["tier_1_errors"] if entry["class_name"] == "_TempTier1HelperError" and entry["class_module"] == __name__
    )
    assert baseline_entry["implementation_hash"] != mutated_entry["implementation_hash"]


def test_manifest_records_live_tier_1_wrapper_closure_drift(_isolate_runtime_val_registries: None) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    dc._FROZEN = False
    tr._FROZEN = False
    prepare_for_run()

    baseline = build_runtime_val_manifest()
    baseline_entry = next(
        entry
        for entry in baseline["tier_1_errors"]
        if entry["class_name"] == "AggregateDeclarationContractViolation"
        and entry["class_module"] == AggregateDeclarationContractViolation.__module__
    )

    wrapped_init = AggregateDeclarationContractViolation.__init__
    closure = wrapped_init.__closure__
    assert closure is not None
    original_init_index = wrapped_init.__code__.co_freevars.index("original_init")
    original_init_cell = closure[original_init_index]
    original_init = original_init_cell.cell_contents

    def replacement(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("replacement should only be hashed")

    original_init_cell.cell_contents = replacement
    try:
        mutated = build_runtime_val_manifest()
    finally:
        original_init_cell.cell_contents = original_init

    mutated_entry = next(
        entry
        for entry in mutated["tier_1_errors"]
        if entry["class_name"] == "AggregateDeclarationContractViolation"
        and entry["class_module"] == AggregateDeclarationContractViolation.__module__
    )
    assert baseline_entry["implementation_hash"] != mutated_entry["implementation_hash"]


def test_manifest_records_live_tier_1_nested_code_helper_drift(_isolate_runtime_val_registries: None) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    dc._FROZEN = False
    tr._FROZEN = False
    prepare_for_run()

    baseline = build_runtime_val_manifest()
    baseline_entry = next(
        entry
        for entry in baseline["tier_1_errors"]
        if entry["class_name"] == "DeclaredOutputFieldsViolation" and entry["class_module"] == DeclaredOutputFieldsViolation.__module__
    )
    original_code = secret_scrub_module._parsed_http_url_contains_sensitive_parts.__code__

    def replacement(value: str) -> bool:
        del value
        return False

    secret_scrub_module._parsed_http_url_contains_sensitive_parts.__code__ = replacement.__code__
    try:
        mutated = build_runtime_val_manifest()
    finally:
        secret_scrub_module._parsed_http_url_contains_sensitive_parts.__code__ = original_code

    mutated_entry = next(
        entry
        for entry in mutated["tier_1_errors"]
        if entry["class_name"] == "DeclaredOutputFieldsViolation" and entry["class_module"] == DeclaredOutputFieldsViolation.__module__
    )
    assert baseline_entry["implementation_hash"] != mutated_entry["implementation_hash"]


def test_manifest_ignores_tier_1_method_docstring_only_edits(_isolate_runtime_val_registries: None) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr
    from elspeth.contracts.tier_registry import tier_1_error

    dc._FROZEN = False
    tr._FROZEN = False

    @tier_1_error(reason="test runtime-val manifest docstring-only drift", caller_module=__name__)
    class _TempTier1DocstringError(Exception):
        def describe(self) -> str:
            return "same"

    prepare_for_run()

    original_code = _TempTier1DocstringError.describe.__code__

    def replacement_without_docstring(self):
        return "same"

    def replacement_with_docstring(self):
        """Replacement whose only semantic difference is this docstring."""
        return "same"

    try:
        _TempTier1DocstringError.describe.__code__ = replacement_without_docstring.__code__
        baseline = build_runtime_val_manifest()

        _TempTier1DocstringError.describe.__code__ = replacement_with_docstring.__code__
        mutated = build_runtime_val_manifest()
    finally:
        _TempTier1DocstringError.describe.__code__ = original_code

    baseline_entry = next(
        entry
        for entry in baseline["tier_1_errors"]
        if entry["class_name"] == "_TempTier1DocstringError" and entry["class_module"] == __name__
    )
    mutated_entry = next(
        entry
        for entry in mutated["tier_1_errors"]
        if entry["class_name"] == "_TempTier1DocstringError" and entry["class_module"] == __name__
    )

    assert baseline_entry["class_name"] == mutated_entry["class_name"]
    assert baseline_entry["class_module"] == mutated_entry["class_module"]
    assert baseline_entry["reason"] == mutated_entry["reason"]
    assert baseline_entry["implementation_hash"] == mutated_entry["implementation_hash"]


def test_class_source_hash_preserves_nested_non_docstring_string_expressions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only true docstrings are excluded from source identity."""

    class _SourceCarrier:
        pass

    source = """
        class _SourceCarrier:
            def emit(self) -> None:
                if True:
                    "before"
    """
    monkeypatch.setattr("elspeth.contracts.runtime_val_manifest.inspect.getsource", lambda cls: source)
    baseline = _class_source_hash(_SourceCarrier)

    source = source.replace('"before"', '"after"')
    mutated = _class_source_hash(_SourceCarrier)

    assert baseline != mutated


@pytest.mark.parametrize("hash_builder", [_callable_implementation_hash, _callable_dependency_hashes])
def test_callable_hash_builders_reject_non_function_owned_values(hash_builder: Callable[..., object]) -> None:
    """A corrupted internal call path must not receive a plausible manifest value."""
    with pytest.raises(AttributeError):
        hash_builder(object())


def test_callable_implementation_hash_canonicalizes_annotation_order() -> None:
    def annotated() -> None:
        pass

    annotated.__annotations__ = {"alpha": "str", "bravo": "int"}
    baseline = _callable_implementation_hash(annotated)
    annotated.__annotations__ = {"bravo": "int", "alpha": "str"}

    assert _callable_implementation_hash(annotated) == baseline


def test_normalize_code_constant_canonicalizes_sets() -> None:
    assert _normalize_code_constant({"bravo", "alpha"}) == {"set": ["alpha", "bravo"]}


def test_normalize_code_constant_rejects_non_string_type_identity() -> None:
    class _BadIdentity:
        pass

    original_module = _BadIdentity.__module__
    _BadIdentity.__module__ = 42  # type: ignore[assignment]
    try:
        with pytest.raises(FrameworkBugError, match="type identity"):
            _normalize_code_constant(_BadIdentity)
    finally:
        _BadIdentity.__module__ = original_module


def test_callable_implementation_hash_rejects_opaque_defaults() -> None:
    opaque_default = object()

    def with_opaque_default(value: object = opaque_default) -> None:
        del value

    with pytest.raises(FrameworkBugError, match="deterministically normalize"):
        _callable_implementation_hash(with_opaque_default)


def test_normalize_code_constant_rejects_dataclass_factory_sentinel_impostor() -> None:
    class _FactorySentinelImpostor:
        pass

    original_module = _FactorySentinelImpostor.__module__
    original_qualname = _FactorySentinelImpostor.__qualname__
    _FactorySentinelImpostor.__module__ = "dataclasses"
    _FactorySentinelImpostor.__qualname__ = "_HAS_DEFAULT_FACTORY_CLASS"
    try:
        with pytest.raises(FrameworkBugError, match="deterministically normalize"):
            _normalize_code_constant(_FactorySentinelImpostor())
    finally:
        _FactorySentinelImpostor.__module__ = original_module
        _FactorySentinelImpostor.__qualname__ = original_qualname


def test_payload_schema_hash_canonicalizes_annotation_order() -> None:
    original_annotations = _RuntimeValManifestOrderedPayload.__annotations__
    try:
        _RuntimeValManifestOrderedPayload.__annotations__ = {"alpha": str, "bravo": int}
        baseline = _payload_schema_hash(_RuntimeValManifestOrderedPayload)
        _RuntimeValManifestOrderedPayload.__annotations__ = {"bravo": int, "alpha": str}
        assert _payload_schema_hash(_RuntimeValManifestOrderedPayload) == baseline
    finally:
        _RuntimeValManifestOrderedPayload.__annotations__ = original_annotations


def test_payload_schema_hash_rejects_opaque_annotations() -> None:
    original_annotations = _RuntimeValManifestIndirectPayload.__annotations__
    _RuntimeValManifestIndirectPayload.__annotations__ = {"value": object()}
    try:
        with pytest.raises(FrameworkBugError, match=r"cannot resolve payload schema annotations|deterministically normalize"):
            _payload_schema_hash(_RuntimeValManifestIndirectPayload)
    finally:
        _RuntimeValManifestIndirectPayload.__annotations__ = original_annotations


def test_manifest_records_live_declaration_payload_schema_drift(
    _isolate_runtime_val_registries: None,
) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    dc._FROZEN = False
    tr._FROZEN = False
    prepare_for_run()

    baseline = build_runtime_val_manifest()
    baseline_entry = next(entry for entry in baseline["declaration_contracts"] if entry["name"] == "declared_required_fields")
    original_annotations = DeclaredRequiredInputFieldsPayload.__annotations__
    DeclaredRequiredInputFieldsPayload.__annotations__ = {**original_annotations, "runtime_added": str}
    try:
        mutated = build_runtime_val_manifest()
    finally:
        DeclaredRequiredInputFieldsPayload.__annotations__ = original_annotations

    mutated_entry = next(entry for entry in mutated["declaration_contracts"] if entry["name"] == "declared_required_fields")
    assert baseline_entry["implementation_hash"] != mutated_entry["implementation_hash"]


def test_manifest_records_live_schema_contract_constructor_closure_drift(
    _isolate_runtime_val_registries: None,
) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    dc._FROZEN = False
    tr._FROZEN = False
    prepare_for_run()

    baseline = build_runtime_val_manifest()
    constructor = SchemaContract.__init__
    closure = constructor.__closure__
    assert closure is not None
    factory_index = constructor.__code__.co_freevars.index("__dataclass_dflt__by_normalized__")
    factory_cell = closure[factory_index]
    original_factory = factory_cell.cell_contents
    factory_cell.cell_contents = list
    try:
        mutated = build_runtime_val_manifest()
    finally:
        factory_cell.cell_contents = original_factory

    assert baseline != mutated


def test_callable_dependency_hashes_reject_non_string_module_identity() -> None:
    def dependency_root() -> None:
        pass

    original_module = dependency_root.__module__
    dependency_root.__module__ = 42  # type: ignore[assignment]
    try:
        with pytest.raises(FrameworkBugError, match="module identity"):
            _callable_dependency_hashes(dependency_root)
    finally:
        dependency_root.__module__ = original_module


def test_manifest_crashes_if_registered_contract_lacks_payload_schema(
    _isolate_runtime_val_registries: None,
) -> None:
    """A corrupted registry entry must not hash ``None`` as its payload schema."""
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    class _NoPayloadSchemaContract(DeclarationContract):
        name = "no_payload_schema"

        def applies_to(self, plugin: object) -> bool:
            return False

        @implements_dispatch_site("post_emission_check")
        def post_emission_check(
            self,
            inputs: PostEmissionInputs,
            outputs: PostEmissionOutputs,
        ) -> None:
            raise AssertionError("not reached")

        @classmethod
        def negative_example(cls) -> ExampleBundle:
            raise NotImplementedError

        @classmethod
        def positive_example_does_not_apply(cls) -> ExampleBundle:
            raise NotImplementedError

    contract = _NoPayloadSchemaContract()
    dc._REGISTRY.append(contract)
    dc._REGISTRY_BY_SITE[DispatchSite.POST_EMISSION].append(contract)
    dc._FROZEN = True
    tr._FROZEN = True

    with pytest.raises(AttributeError, match="payload_schema"):
        build_runtime_val_manifest()


def test_manifest_crashes_if_registered_contract_has_non_type_payload_schema(
    _isolate_runtime_val_registries: None,
) -> None:
    """A corrupted registry entry must not hash an arbitrary schema object."""
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    class _NonTypePayloadSchemaContract(DeclarationContract):
        name = "non_type_payload_schema"
        payload_schema = object()

        def applies_to(self, plugin: object) -> bool:
            return False

        @implements_dispatch_site("post_emission_check")
        def post_emission_check(
            self,
            inputs: PostEmissionInputs,
            outputs: PostEmissionOutputs,
        ) -> None:
            raise AssertionError("not reached")

        @classmethod
        def negative_example(cls) -> ExampleBundle:
            raise NotImplementedError

        @classmethod
        def positive_example_does_not_apply(cls) -> ExampleBundle:
            raise NotImplementedError

    contract = _NonTypePayloadSchemaContract()
    dc._REGISTRY.append(contract)
    dc._REGISTRY_BY_SITE[DispatchSite.POST_EMISSION].append(contract)
    dc._FROZEN = True
    tr._FROZEN = True

    with pytest.raises(FrameworkBugError, match="TypedDict"):
        build_runtime_val_manifest()


def test_manifest_rejects_structural_payload_schema_impostor(
    _isolate_runtime_val_registries: None,
) -> None:
    import elspeth.contracts.declaration_contracts as dc
    import elspeth.contracts.tier_registry as tr

    class _PayloadSchemaImpostor:
        __annotations__ = {"value": str}
        __required_keys__ = frozenset({"value"})
        __optional_keys__ = frozenset()

    class _ImpostorPayloadSchemaContract(DeclarationContract):
        name = "impostor_payload_schema"
        payload_schema = _PayloadSchemaImpostor

        def applies_to(self, plugin: object) -> bool:
            return False

        @implements_dispatch_site("post_emission_check")
        def post_emission_check(
            self,
            inputs: PostEmissionInputs,
            outputs: PostEmissionOutputs,
        ) -> None:
            raise AssertionError("not reached")

        @classmethod
        def negative_example(cls) -> ExampleBundle:
            raise NotImplementedError

        @classmethod
        def positive_example_does_not_apply(cls) -> ExampleBundle:
            raise NotImplementedError

    contract = _ImpostorPayloadSchemaContract()
    dc._REGISTRY.append(contract)
    dc._REGISTRY_BY_SITE[DispatchSite.POST_EMISSION].append(contract)
    dc._FROZEN = True
    tr._FROZEN = True

    with pytest.raises(FrameworkBugError, match="TypedDict"):
        build_runtime_val_manifest()
