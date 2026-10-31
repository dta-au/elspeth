"""Direct current-caller controls for the U30 proof-join repair."""

import ast

import pytest
from tests.helpers import web_reserve_provider_mint_contract as provider
from tests.unit.architecture import test_web_landscape_mutation_fencing as fencing
from tests.unit.architecture import test_web_reserve_reviewed_contracts as reviewed

PRIMARY = "src/elspeth/web/composer/service.py"
RENDERER = "src/elspeth/web/composer/prompt_guard_rendering.py"


def _callback_units() -> tuple[fencing.SourceUnit, ...]:
    paths = {path for path, _name in fencing._reserve_CALLBACK_BODIES}
    paths.update(fencing._reserve_CALLBACK_PREFIX + filename for filename in fencing._reserve_CALLBACK_ADAPTERS)
    paths.add("src/elspeth/web/composer/pipeline_planner.py")
    return reviewed._source_units(tuple(sorted(paths)))


def test_u30_current_mint_calls_keep_title_and_all_common_premises() -> None:
    units = reviewed._provider_mint_units()
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == []
    assert receipt is not None
    assert reviewed._provider_mint_rows(units, receipt["mint_calls"]) == {
        (reviewed.TURN, "provider_owner.mint(ProviderInvocationFamily.TITLE)"),
        (reviewed.DIAGNOSTICS, "owner.mint(ProviderInvocationFamily.PRIMARY)"),
        (PRIMARY, "provider_owner.mint(ProviderInvocationFamily.PRIMARY)"),
        ("src/elspeth/web/composer/advisor_checkpoint.py", "provider_owner.mint(ProviderInvocationFamily.ADVISOR)"),
        ("src/elspeth/web/composer/continuation_title_provider.py", "invocation_owner.mint(ProviderInvocationFamily.TITLE)"),
    }
    assert receipt["remaining_premises"] == (
        "UNKNOWN_COMMON_SOURCE_IMPORT_BINDING_CLOSURE",
        "UNKNOWN_COMMON_RECEIVER_MEMBER_OUTSIDE_EFFECT_CLOSURE",
        "UNKNOWN_REQUIRED_BINDING_COORDINATOR_AUTHORITY_CONTEXT_CUSTODY",
    )


def test_u30_extra_mint_in_checked_primary_is_resolved_and_refused() -> None:
    units = reviewed._provider_mint_units()
    failures, receipt = provider.provider_mint_local_contracts(units)
    assert failures == []
    assert receipt is not None
    original = next(unit for unit in units if unit.path == PRIMARY)
    before = "        return provider_owner.mint(ProviderInvocationFamily.PRIMARY)\n"
    after = "        provider_owner.mint(ProviderInvocationFamily.PLANNER)\n" + before
    assert original.source.count(before) == 1
    changed = fencing._parse_source(PRIMARY, original.source.replace(before, after, 1))
    mutated = tuple(changed if unit.path == PRIMARY else unit for unit in units)
    old_caller = provider._node(original, "ComposerServiceImpl._new_primary_custody")
    new_caller = provider._node(changed, "ComposerServiceImpl._new_primary_custody")
    assert old_caller is not None and new_caller is not None
    checked = (receipt["caller_regions"] - {(PRIMARY, id(old_caller))}) | {(PRIMARY, id(new_caller))}
    _constructors, mints, _escapes = provider._call_universe(mutated, checked)
    actual = {ast.unparse(node) for node in ast.walk(new_caller) if isinstance(node, ast.Call) and (PRIMARY, id(node)) in mints}
    assert actual == {
        "provider_owner.mint(ProviderInvocationFamily.PRIMARY)",
        "provider_owner.mint(ProviderInvocationFamily.PLANNER)",
    }
    failures, refused = provider.provider_mint_local_contracts(mutated)
    assert failures == [
        "provider mint complete producer/retention/caller contract changed "
        "elspeth.web.composer.service:ComposerServiceImpl._new_primary_custody"
    ]
    assert refused is None


def test_u30_callback_receipt_uses_current_input_ast_only() -> None:
    units = _callback_units()
    failures, receipt = fencing._reserve_batch_data_callback_consumers(units)
    assert failures == []
    actual = {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}
    assert receipt["calls"] and receipt["calls"] <= actual
    assert receipt["uses"] and receipt["uses"] <= actual
    assert receipt["observed_uses"] <= receipt["uses"]
    assert all((path, call_id) in actual and (path, arg_id) in actual for path, call_id, arg_id in receipt["argument_calls"])
    reparsed = tuple(fencing._parse_source(unit.path, unit.source) for unit in units)
    failures, other = fencing._reserve_batch_data_callback_consumers(reparsed)
    assert failures == []
    assert not other["calls"] & actual
    assert not other["uses"] & actual


@pytest.mark.parametrize(
    "replacement",
    (
        "    decoder = unknown_consumer\n    value = decoder(serialized)\n",
        "    value = json_content_parts(serialized, encoder=encoder, decoder=decoder)\n",
    ),
    ids=("decoder-shadow", "explicit-callback-transfer"),
)
def test_u30_checked_renderer_refuses_shadow_and_explicit_callback_transfer(replacement: str) -> None:
    units = _callback_units()
    failures, receipt = fencing._reserve_batch_data_callback_consumers(units)
    assert failures == []
    assert receipt["calls"]
    original = next(unit for unit in units if unit.path == RENDERER)
    before = "    value = decoder(serialized)\n"
    assert original.source.count(before) == 1
    changed = fencing._parse_source(RENDERER, original.source.replace(before, replacement, 1))
    mutated = tuple(changed if unit.path == RENDERER else unit for unit in units)
    failures, refused = fencing._reserve_batch_data_callback_consumers(mutated)
    assert failures == ["batch callback consumer complete consumer body changed " + RENDERER + ":json_content_parts"]
    assert refused == {}
