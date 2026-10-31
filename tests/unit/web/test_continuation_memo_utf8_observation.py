"""Control bounded scanner work independently of its returned size sentinel."""

import ctypes
import dis
import sys
from dataclasses import replace

import pytest

from elspeth.contracts import bounded_serialization, credential_material
from elspeth.contracts.credential_material import CredentialTraversalPolicy
from elspeth.web import credential_guard as guard
from elspeth.web.composer import continuation_clock as clock
from tests.helpers.continuation_memo import RetentionMemoTrace
from tests.unit.web.test_continuation_memo_accounting import _native_byte_limits, _native_scan_budget


@pytest.mark.parametrize(
    "text,preparation,chunks,size",
    [
        ("", 0, 0, 0),
        ("ordinary", 8, 0, 0),
        ("café☃", 8, 1, 8),
        ("\ud800", 1, 1, 1),
        ("é" * 4096, 8192, 1, 8192),
        ("é" * 4097, 8194, 2, 8194),
    ],
    ids=["empty", "ascii", "unicode", "replacement-surrogate", "chunk4096", "multichunk4097"],
)
def test_bounded_scanner_counts_real_invocations_and_only_actual_chunk_bytes(monkeypatch, request, text, preparation, chunks, size):
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        for _ in range(2):
            assert (
                guard._retention_credential_finding(
                    text, policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic"
                )
                is None
            )
    budget = replace(
        _native_scan_budget(1, text, preparation),
        limits=_native_byte_limits(scanner_size=(1, 0), scanner_extent=(chunks, size), key_preparation=(2, 2 * preparation)),
    )
    trace.finish(stages=1, require_snapshot=False, native_byte_limits=(budget,))
    assert [(row.calls, row.bytes) for row in trace.native_byte_totals() if row.channel == "scanner_size"] == [(1, 0)]
    assert [(row.calls, row.bytes) for row in trace.native_byte_totals() if row.channel == "scanner_extent"] == [(chunks, size)]


@pytest.mark.parametrize(
    "value,policy,tokens,preparation,calls,chunks,size,branch,result",
    [
        (
            {"café": 1},
            CredentialTraversalPolicy(),
            (("mapping", "1"), ("string", "café"), ("int", "1"), ("end", "mapping")),
            14,
            1,
            1,
            5,
            "encoded",
            None,
        ),
        (
            {"password": {"secret_ref": "managed_ref"}},
            CredentialTraversalPolicy(),
            (
                ("mapping", "1"),
                ("string", "password"),
                ("mapping", "1"),
                ("string", "secret_ref"),
                ("string", "managed_ref"),
                ("end", "mapping"),
                ("end", "mapping"),
            ),
            45,
            2,
            0,
            0,
            "ascii",
            None,
        ),
        (
            {"password": "${API_TOKEN}"},
            CredentialTraversalPolicy(env_ref_names=frozenset({"API_TOKEN"})),
            (("mapping", "1"), ("string", "password"), ("string", "${API_TOKEN}"), ("end", "mapping")),
            37,
            2,
            0,
            0,
            "ascii",
            None,
        ),
        (
            {"password": {"secret_ref": "a" * 257}},
            CredentialTraversalPolicy(),
            (
                ("mapping", "1"),
                ("string", "password"),
                ("mapping", "1"),
                ("string", "secret_ref"),
                ("string", "a" * 257),
                ("end", "mapping"),
                ("end", "mapping"),
            ),
            291,
            2,
            0,
            0,
            "length_excess",
            "traversal_limit",
        ),
        (
            {"password": {"secret_ref": "é" * 128}},
            CredentialTraversalPolicy(),
            (
                ("mapping", "1"),
                ("string", "password"),
                ("mapping", "1"),
                ("string", "secret_ref"),
                ("string", "é" * 128),
                ("end", "mapping"),
                ("end", "mapping"),
            ),
            290,
            2,
            1,
            256,
            "encoded",
            "credential_field",
        ),
        (
            {"password": {"secret_ref": "é" * 129}},
            CredentialTraversalPolicy(),
            (
                ("mapping", "1"),
                ("string", "password"),
                ("mapping", "1"),
                ("string", "secret_ref"),
                ("string", "é" * 129),
                ("end", "mapping"),
                ("end", "mapping"),
            ),
            292,
            2,
            1,
            258,
            "encoded_excess",
            "traversal_limit",
        ),
    ],
    ids=[
        "mapping-key",
        "named-reference",
        "environment-reference",
        "early-length-excess",
        "exact-reference-boundary",
        "encoded-reference-excess",
    ],
)
def test_bounded_scanner_binds_each_real_value_key_and_reference_site(
    monkeypatch, request, value, policy, tokens, preparation, calls, chunks, size, branch, result
):
    from elspeth.web.continuation_retention_memo import CredentialInputToken, CredentialPolicyKey, CredentialScanKey
    from tests.helpers.continuation_memo import NativeByteBudget

    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        for _ in range(2):
            finding = guard._retention_credential_finding(value, policy=policy, surface="bounded-sites", scanner_role="generic")
            assert (None if finding is None else finding.category) == result
    key = CredentialScanKey(
        "generic",
        "bounded-sites",
        tuple(CredentialInputToken(*token) for token in tokens),
        CredentialPolicyKey(policy.env_ref_names, frozenset(), 32, 4096, 512, 262_144, "1"),
        credential_material.find_credential_material,
    )
    limits = _native_byte_limits(
        scanner_size=(calls, 0),
        scanner_extent=(chunks, size),
        key_preparation=(2 * (len(tokens) + len(policy.env_ref_names)), 2 * preparation),
    )
    trace.finish(
        stages=1,
        require_snapshot=False,
        native_byte_limits=(NativeByteBudget(1, "credential", limits, key, guard._credential_finding_from_key),),
    )
    receipt = trace._byte_size_inspections[-1]
    assert receipt.branch == branch and receipt.encoded_bytes == size
    if branch.endswith("excess"):
        assert receipt.returned_size == 257
        assert receipt.encoded_bytes in (0, 258)


@pytest.mark.parametrize("mutation", ["wrapper", "foreign", "errors", "limit", "unrelated", "helper-code", "scanner-code", "chunk-size"])
def test_bounded_scanner_refuses_changed_helper_source_arguments_and_unrelated_calls(monkeypatch, request, mutation):
    original = bounded_serialization.utf8_size

    def changed(text, *, errors="strict", limit=None):
        if mutation == "foreign":
            return len(text.encode("utf-8", errors=errors))
        if mutation == "errors":
            errors = "strict"
        if mutation == "limit":
            limit -= 1
        return original(text, errors=errors, limit=limit)

    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    if mutation in ("wrapper", "foreign", "errors", "limit"):
        monkeypatch.setattr(credential_material, "utf8_size", changed)
    elif mutation == "helper-code":
        monkeypatch.setattr(original, "__code__", (lambda text, *, errors="strict", limit=None: 8).__code__)
    elif mutation == "scanner-code":
        code = credential_material._find.__code__
        monkeypatch.setattr(
            credential_material._find,
            "__code__",
            code.replace(co_consts=tuple("strict" if value == "replace" else value for value in code.co_consts)),
        )
    elif mutation == "chunk-size":
        monkeypatch.setattr(bounded_serialization, "_CHUNK_CHARACTERS", 8192)
    with clock.retention_stage_memo_scope():
        for _ in range(2):
            assert (
                guard._retention_credential_finding(
                    "café☃", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic"
                )
                is None
            )
        if mutation == "unrelated":
            assert original("café☃", errors="replace", limit=262_144) == 8
    with pytest.raises(AssertionError, match="native byte provenance invalid"):
        trace.finish(stages=1, require_snapshot=False)


@pytest.mark.parametrize("channel", ["scanner_size", "scanner_extent"])
@pytest.mark.parametrize("difference", [-1, 1])
def test_bounded_scanner_refuses_missing_or_duplicate_independent_charges(monkeypatch, request, channel, difference):
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        assert (
            guard._retention_credential_finding(
                "café☃", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic"
            )
            is None
        )
    trace.records[0].byte_calls[channel] += difference
    budget = _native_scan_budget(1, "café☃", 8, requests=1)
    with pytest.raises(AssertionError, match="bounded scanner lost or duplicated its invocation or chunk charge"):
        trace.finish(stages=1, require_snapshot=False, native_byte_limits=(budget,))


def test_bounded_scanner_refuses_a_skipped_native_chunk_event(monkeypatch, request):
    from tests.helpers import continuation_memo as helper

    # Keep the native callback and source bindings; omit only the actual len
    # event. The original return must oppose this missing observation.
    monkeypatch.setitem(helper._EXTENT_SITES, helper._UTF8_CODE, frozenset())
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        assert (
            guard._retention_credential_finding(
                "café☃", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic"
            )
            is None
        )
    assert "bounded scanner chunks disagree with its actual return branch" in trace.byte_errors
    with pytest.raises(AssertionError, match="native byte provenance invalid"):
        trace.finish(stages=1, require_snapshot=False)


@pytest.mark.parametrize(
    "origin", ["original", "wrong-frame", "other-site", "other-source", "cache-origin", "keyword-origin", "next-instruction"]
)
def test_bounded_scanner_admits_only_its_exact_originating_call_and_caches(monkeypatch, request, origin):
    from tests.helpers import continuation_memo as helper

    saved = sys.getprofile()
    assert saved is None, "control requires an unprofiled harness"
    observed = []
    instructions = {item.offset: item for item in dis.get_instructions(helper._FIND, show_caches=True)}
    trace = None

    def previous(frame, event, _arg):
        if event != "call" or frame.f_code is not helper._UTF8_CODE or trace is None:
            return
        parent = frame.f_back
        assert parent is not None and parent.f_code is helper._FIND
        inspection = trace._byte_size_pending[id(parent)]
        assert inspection.call_code is helper._FIND and type(inspection.call_offset) is int
        offset = inspection.call_offset
        window = helper._SCANNER_SIZE_WINDOWS[helper._FIND][offset][0]
        assert instructions[offset].opname in ("CALL", "CALL_KW")
        assert parent.f_lasti in window
        observed.append((offset, parent.f_lasti))
        if origin == "wrong-frame":
            inspection.caller = id(frame)
        elif origin == "other-site":
            inspection.call_offset = next(site for site in helper._SCANNER_SIZE_SITES[helper._FIND] if site != offset)
        elif origin == "other-source":
            inspection.call_code = helper._REFERENCE
            inspection.call_offset = next(iter(helper._SCANNER_SIZE_SITES[helper._REFERENCE]))
        elif origin == "cache-origin":
            # A 3.12 cache word is valid as the suspended parent address, but
            # it is never a valid originating monitoring CALL address.
            if instructions[offset].opname == "CALL":
                assert instructions[offset + 2].opname == "CACHE"
            inspection.call_offset = offset + 2
        elif origin == "keyword-origin":
            assert instructions[offset - 2].opname in ("KW_NAMES", "LOAD_CONST")
            inspection.call_offset = offset - 2
        elif origin == "next-instruction":
            inspection.call_offset = window.stop

    sys.setprofile(previous)
    try:
        trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
        with clock.retention_stage_memo_scope():
            assert (
                guard._retention_credential_finding(
                    "café☃", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic"
                )
                is None
            )
        assert len(observed) == 1, "the original native scanner call must reach the opposition"
        budget = _native_scan_budget(1, "café☃", 8, requests=1)
        if origin == "original":
            trace.finish(stages=1, require_snapshot=False, native_byte_limits=(budget,))
            (inspection,) = trace._byte_size_inspections
            assert (inspection.call_offset, inspection.profile_offset) == observed[0]
        else:
            assert "bounded scanner changed its original arguments or carrier" in trace.byte_errors
            with pytest.raises(AssertionError, match="native byte provenance invalid"):
                trace.finish(stages=1, require_snapshot=False, native_byte_limits=(budget,))
        assert sys.getprofile() is previous
    finally:
        sys.setprofile(saved)


@pytest.mark.parametrize("argument", ["text", "errors", "limit"])
def test_bounded_scanner_refuses_equal_result_with_altered_native_arguments(monkeypatch, request, argument):
    saved = sys.getprofile()
    assert saved is None, "control requires an unprofiled harness"
    native_size = bounded_serialization.utf8_size
    native_code, caller_code = native_size.__code__, credential_material._find.__code__
    # Typed local Python-API callables keep the GIL and do not mutate the
    # signatures of shared ctypes.pythonapi function objects. 3.12 needs the
    # explicit dictionary-to-fast-locals synchronization; 3.13 is write-through.
    locals_to_fast = ctypes.PYFUNCTYPE(None, ctypes.py_object, ctypes.c_int)(("PyFrame_LocalsToFast", ctypes.pythonapi))
    fast_to_locals = ctypes.PYFUNCTYPE(ctypes.c_int, ctypes.py_object)(("PyFrame_FastToLocalsWithError", ctypes.pythonapi))
    mutations, native_returns = [], []

    def previous(frame, event, _arg):
        if frame.f_code is not native_code or event not in ("call", "return"):
            return
        assert bounded_serialization.utf8_size is native_size and native_size.__code__ is native_code
        parent = frame.f_back
        assert parent is not None and parent.f_code is caller_code
        if event == "call":
            assert not mutations, "the control must mutate exactly one original native invocation"
            local_values = frame.f_locals
            original = local_values[argument]
            if argument == "text":
                replacement = ("!" + original)[1:]
                assert replacement == original and replacement is not original
            elif argument == "errors":
                assert original == "replace"
                replacement = "strict"
            else:
                assert type(original) is int and original > 8
                replacement = original - 1
            local_values[argument] = replacement
            locals_to_fast(frame, 0)
            assert fast_to_locals(frame) == 0
            assert frame.f_locals[argument] is replacement and replacement is not original, "actual fast-local mutation is required"
            mutations.append((id(frame), id(parent), replacement))
        else:
            assert len(mutations) == 1 and mutations[0][:2] == (id(frame), id(parent))
            assert fast_to_locals(frame) == 0 and frame.f_locals[argument] is mutations[0][2]
            assert type(_arg) is int and _arg == 8, "the original native producer must keep its actual return"
            native_returns.append(_arg)

    sys.setprofile(previous)
    try:
        trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
        with clock.retention_stage_memo_scope():
            assert (
                guard._retention_credential_finding(
                    "café☃", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic"
                )
                is None
            )
        assert len(mutations) == 1 and native_returns == [8]
        assert "bounded scanner changed its original arguments or carrier" in trace.byte_errors
        with pytest.raises(AssertionError, match="native byte provenance invalid"):
            trace.finish(stages=1, require_snapshot=False)
        assert sys.getprofile() is previous
    finally:
        sys.setprofile(saved)


@pytest.mark.parametrize("marker_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_bounded_scanner_preserves_the_original_exception_after_real_size_work(monkeypatch, request, marker_type):
    marker = marker_type("original scanner classification marker")

    def interrupted(_value):
        raise marker

    monkeypatch.setattr(credential_material, "_classify_string", interrupted)
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        with pytest.raises(marker_type) as caught:
            guard._retention_credential_finding(
                "café☃", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic"
            )
        assert caught.value is marker
    trace.finish(
        stages=1, require_snapshot=False, require_population=False, native_byte_limits=(_native_scan_budget(1, "café☃", 8, requests=1),)
    )
