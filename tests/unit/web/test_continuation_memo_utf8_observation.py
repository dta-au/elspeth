"""Control bounded scanner work independently of its returned size sentinel."""

import ctypes
import dis
import hashlib
import sys
from dataclasses import replace

import pytest

from elspeth.contracts import bounded_serialization, credential_material
from elspeth.contracts.credential_material import CredentialTraversalPolicy
from elspeth.web import credential_guard as guard
from elspeth.web.composer import continuation_clock as clock
from tests.helpers.continuation_memo import RetentionMemoTrace
from tests.unit.web.test_continuation_memo_accounting import _native_byte_limits, _native_scan_budget


@pytest.mark.asyncio
@pytest.mark.parametrize("orphan_on_close", (False, True))
async def test_reverse_request_observation_counts_one_parent_and_detects_orphan(monkeypatch, orphan_on_close):
    from elspeth.web import continuation_retention_memo as memo
    from tests.helpers.continuation_end_profile import EndTextMemoObservation
    from tests.unit.web.test_continuation_retention_memo import _advisor_clock

    def produce(_key):
        return memo.RecorderRequestTextProof("original-reference-document", ())

    deps = memo.RecorderRequestTextDependencies(produce, produce, hashlib.sha256, frozenset())
    actual_close = memo.RetentionStageMemo.close

    def close_with_orphan(owner):
        aliases = dict(owner._v4_recorder_request_reverse)
        actual_close(owner)
        owner._v4_recorder_request_reverse.update(aliases)

    if orphan_on_close:
        monkeypatch.setattr(memo.RetentionStageMemo, "close", close_with_orphan)
    observation = EndTextMemoObservation()
    with observation.install(monkeypatch), memo.v4_preparation_memo_scope(_advisor_clock()) as parent:
        with memo.borrow_v4_preparation_memo_scope() as child:
            child.v4_recorder_request_text(memo.RecorderRequestTextKey("actual-source", 4096), deps, produce)
            assert child.v4_restored_recorder_request_text("original-reference-document", 4096, deps, produce) == "actual-source"
            assert child.v4_restored_recorder_request_text("changed-reference-document", 4096, deps, produce) is None
        assert parent.cached_entry_count == 1
        actual_weight = parent._v4_recorder_request_bytes
    result = observation.snapshot()
    assert result["reverse_request_lookup"] == {"requests": 2, "hits": 1, "misses": 1, "errors": 0}
    assert result["families"]["request"]["requests"] == 1
    assert result["peak_retained_bytes"] == actual_weight
    assert result["parents"] == [{"budget": parent._shared_text_budget_bytes, "closed_empty": not orphan_on_close}]


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
    assert [(item.chunks, item.encoded_bytes, item.returned_size) for item in trace._byte_category_inspections] == [
        (chunks, size, preparation)
    ]
    assert [(row.calls, row.bytes) for row in trace.native_byte_totals() if row.channel == "scanner_size"] == [(1, 0)]
    assert [(row.calls, row.bytes) for row in trace.native_byte_totals() if row.channel == "scanner_extent"] == [(chunks, size)]


@pytest.mark.parametrize("charge_delta", (-1, 1))
def test_category_admission_keeps_its_independent_native_charge(monkeypatch, request, charge_delta):
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        assert (
            guard._retention_credential_finding(
                "café☃", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic"
            )
            is None
        )
    (category,) = trace._byte_category_inspections
    assert category.branch == "encoded" and category.returned_size == 8
    assert category.chunks == 1 and category.encoded_bytes == 8
    category.owner.byte_calls["category_extent"] += charge_delta
    with pytest.raises(AssertionError, match="category admission lost or duplicated its independent native charge"):
        trace.finish(stages=1, require_snapshot=False, native_byte_limits=(_native_scan_budget(1, "café☃", 8, requests=1),))


def test_classified_detector_cannot_borrow_owner_from_an_unregistered_caller(monkeypatch, request):
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope():
        assert (
            credential_material._find_credential_material_with_classifier(
                "ordinary", CredentialTraversalPolicy(), credential_material._classify_string
            )
            is None
        )
    assert "classified detector lost its original guard/value/policy/memo" in trace.byte_errors
    with pytest.raises(AssertionError, match="native byte provenance invalid"):
        trace.finish(stages=1, require_snapshot=False)


def test_category_admission_refuses_changed_actual_limit(monkeypatch, request):
    from tests.helpers import continuation_memo as helper

    saved = sys.getprofile()
    assert saved is None
    changed = []
    locals_to_fast = ctypes.PYFUNCTYPE(None, ctypes.py_object, ctypes.c_int)(("PyFrame_LocalsToFast", ctypes.pythonapi))

    def previous(frame, event, _arg):
        if event != "call" or frame.f_code is not helper._UTF8_CODE:
            return
        parent = frame.f_back
        if parent is None or parent.f_code is not helper._CATEGORY_QUERY.__code__:
            return
        assert not changed
        frame.f_locals["limit"] -= 1
        locals_to_fast(frame, 0)
        changed.append(True)

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
        assert changed == [True]
        assert "category size lost its actual memo/input/limit owner" in trace.byte_errors
        with pytest.raises(AssertionError, match="native byte provenance invalid"):
            trace.finish(stages=1, require_snapshot=False, native_byte_limits=(_native_scan_budget(1, "café☃", 8, requests=1),))
        assert sys.getprofile() is previous
    finally:
        sys.setprofile(saved)


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
        if parent is None or parent.f_code is not helper._FIND:
            return
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
        if parent is None or parent.f_code is not caller_code:
            return
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
    actual_size, sizes = credential_material.utf8_size, []

    def size(text, *, errors="strict", limit=None):
        result = actual_size(text, errors=errors, limit=limit)
        sizes.append((text, errors, limit, result))
        return result

    def interrupted(_value):
        assert sizes == [("café☃", "replace", 262_144, 8)]
        raise marker

    monkeypatch.setattr(credential_material, "_classify_string", interrupted)
    monkeypatch.setattr(credential_material, "utf8_size", size)
    # Replacing the classifier deliberately takes the public guard's uncached
    # branch. Its real size work has no keyed NativeByteBudget identity.
    with clock.retention_stage_memo_scope() as memo:
        with pytest.raises(marker_type) as caught:
            guard._retention_credential_finding(
                "café☃", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic"
            )
        assert caught.value is marker
        assert memo.cached_entry_count == 0
    assert sizes == [("café☃", "replace", 262_144, 8)]


@pytest.mark.parametrize("marker_type", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_keyed_credential_producer_exception_keeps_actual_native_byte_budget(monkeypatch, request, marker_type):
    marker = marker_type("original keyed producer classification marker")
    original_producer = guard._credential_finding_from_key

    def interrupted(_value):
        raise marker

    def produce(key):
        # Change the classifier only after the public guard constructed its
        # actual key and selected this actual producer. The original producer
        # and native detector still perform all sizing and raise the marker.
        with monkeypatch.context() as patch:
            patch.setattr(credential_material, "_classify_string", interrupted)
            return original_producer(key)

    monkeypatch.setattr(guard, "_credential_finding_from_key", produce)
    trace = RetentionMemoTrace(monkeypatch, request, require_native_bytes=True)
    with clock.retention_stage_memo_scope() as memo:
        with pytest.raises(marker_type) as caught:
            guard._retention_credential_finding(
                "café☃", policy=CredentialTraversalPolicy(), surface="native-bytes-control", scanner_role="generic"
            )
        assert caught.value is marker and memo.cached_entry_count == 0
    trace.finish(
        stages=1, require_snapshot=False, require_population=False, native_byte_limits=(_native_scan_budget(1, "café☃", 8, requests=1),)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("omitted", (None, "credential", "evidence", "recorder"))
async def test_end_text_observer_accounts_all_seven_families_and_detects_omitted_charges(monkeypatch, omitted):
    from hashlib import sha256

    from elspeth.web import continuation_retention_memo as memo
    from elspeth.web.sessions.continuation_selection import _evidence_private_references
    from tests.helpers.continuation_end_profile import EndTextMemoObservation
    from tests.unit.web.composer.test_continuation_evidence_graph import rich_evidence
    from tests.unit.web.composer.test_continuation_evidence_graph import snapshot as capture_snapshot
    from tests.unit.web.test_continuation_retention_memo import (
        _admit_uncached_recorder_data,
        _advisor_clock,
        _produce_uncached_recorder_data,
        _recorder_data_chunk,
    )

    evidence, messages, recorder, logical, _policy = rich_evidence()
    captured = capture_snapshot(evidence, messages, recorder, logical)
    chunk = captured.source_chunk
    canonical = '{"message":"observer"}'
    calls = dict.fromkeys(EndTextMemoObservation.families, 0)
    recorder_chunk = _recorder_data_chunk()

    def encode(key):
        calls["full"] += 1
        return key.canonical.encode("utf-8")

    def verify(_text):
        calls["canonical"] += 1

    def block(key):
        calls["block"] += 1
        assert sha256(key.canonical.encode("utf-8")).hexdigest() == key.expected_digest

    def request(_key):
        calls["request"] += 1
        return memo.RecorderRequestTextProof(canonical, ())

    def category(text):
        calls["credential"] += 1
        return credential_material._classify_string(text)

    def references(text):
        calls["evidence"] += 1
        return _evidence_private_references(text)

    def recorder_data(text):
        calls["recorder"] += 1
        return _produce_uncached_recorder_data(text)

    def current():
        return True

    canonical_dependencies = memo.CanonicalTextVerificationDependencies((verify,) * 17, current)
    request_dependencies = memo.RecorderRequestTextDependencies(encode, verify, sha256, frozenset({"shape"}))
    observation = EndTextMemoObservation()
    if omitted is not None:
        actual_state = observation._state

        def omit_charge(*args):
            state = actual_state(*args)
            weights = tuple(0 if family == omitted else weight for family, weight in zip(observation.families, state[3], strict=True))
            return (*state[:3], weights, *state[4:])

        monkeypatch.setattr(observation, "_state", omit_charge)

    with observation.install(monkeypatch), memo.v4_preparation_memo_scope(_advisor_clock()) as parent:
        for _ in range(2):
            with memo.borrow_v4_preparation_memo_scope() as child:
                child.canonical_text_proof(memo.CanonicalTextEncodingKey(canonical, 1_048_576), encode, sha256)
                child.canonical_text_verification(memo.CanonicalTextVerificationKey(canonical, 1_048_576, canonical_dependencies), verify)
                child.recorder_block_hash(memo.RecorderBlockHashKey(sha256(canonical.encode("utf-8")).hexdigest(), canonical), block)
                child.v4_recorder_request_text(memo.RecorderRequestTextKey(canonical, 1_048_576), request_dependencies, request)
                assert child.credential_string_category("ordinary observer text", category) is None
                assert child.evidence_references(memo.PrivateChunkVerificationKey(chunk.reference, chunk.canonical), references) == (
                    captured.snapshot.recorder,
                )
                child.recorder_data(
                    memo.PrivateChunkVerificationKey(recorder_chunk.reference, recorder_chunk.canonical),
                    recorder_data,
                    _admit_uncached_recorder_data,
                )
        entries = (
            parent._canonical_proofs,
            parent._canonical_verifications,
            parent._recorder_block_hashes,
            parent._v4_recorder_requests,
            parent._credential_strings,
            parent._evidence_references,
            parent._recorder_data,
        )
        retained = (
            sum(entry.retained_bytes for entry in parent._canonical_proofs.values()),
            sum(parent._canonical_verifications.values()),
            sum(parent._recorder_block_hashes.values()),
            sum(entry.retained_bytes for entry in parent._v4_recorder_requests.values()),
            sum(entry.retained_bytes for entry in parent._credential_strings.values()),
            sum(entry.retained_bytes for entry in parent._evidence_references.values()),
            sum(entry.retained_bytes for entry in parent._recorder_data.values()),
        )
        assert all(entries) and all(weight > 0 for weight in retained)
        assert retained == (
            parent._canonical_proof_bytes,
            parent._canonical_verification_bytes,
            parent._recorder_block_hash_bytes,
            parent._v4_recorder_request_bytes,
            parent._credential_string_bytes,
            parent._evidence_reference_bytes,
            parent._recorder_data_bytes,
        )
        assert all(count == 1 for count in calls.values())
        result = observation.snapshot()
        assert set(result["families"]) == {"full", "canonical", "block", "request", "credential", "evidence", "recorder"}
        for counts in result["families"].values():
            assert (counts["requests"], counts["misses"], counts["hits"], counts["admissions"]) == (2, 1, 1, 1)
        if omitted is None:
            assert result["peak_retained_bytes"] == sum(retained)
        else:
            # A deliberately incomplete observer must fail the independent
            # entry-based accounting oracle, rather than certify its own sum.
            with pytest.raises(AssertionError):
                assert result["peak_retained_bytes"] == sum(retained)
    result = observation.snapshot()
    assert result["parents"] and all(row["closed_empty"] for row in result["parents"])
    assert result["observer_errors"] == result["budget_violations"] == result["fingerprint_drops"] == 0


@pytest.mark.asyncio
async def test_recorder_observer_real_public_decoder_counts_nested_cold_and_warm_work(monkeypatch):
    from elspeth.web import continuation_retention_memo as memo
    from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot, encode_recorder_snapshot
    from tests.helpers.continuation_end_profile import EndTextMemoObservation
    from tests.unit.web.composer.test_continuation_audit_buffer_blocks import _snapshot
    from tests.unit.web.test_continuation_retention_memo import _advisor_clock

    snapshot = _snapshot(1)
    chunk = encode_recorder_snapshot(snapshot)
    observation = EndTextMemoObservation()
    with observation.install(monkeypatch), memo.v4_preparation_memo_scope(_advisor_clock()) as parent:
        for _ in range(2):
            with memo.borrow_v4_preparation_memo_scope():
                assert decode_recorder_snapshot(chunk) == snapshot
        result = observation.snapshot()
        counts = result["families"]["recorder"]
        assert (counts["requests"], counts["misses"], counts["hits"], counts["admissions"], counts["unretained"]) == (2, 1, 1, 1, 0)
        assert result["families"]["full"]["requests"] >= 4  # Fresh verification and admission on each call.
        assert result["families"]["request"]["requests"] > 0
        assert result["families"]["credential"]["requests"] >= 2
        retained = (
            sum(entry.retained_bytes for entry in parent._canonical_proofs.values())
            + sum(parent._canonical_verifications.values())
            + sum(parent._recorder_block_hashes.values())
            + sum(entry.retained_bytes for entry in parent._v4_recorder_requests.values())
            + sum(entry.retained_bytes for entry in parent._credential_strings.values())
            + sum(entry.retained_bytes for entry in parent._evidence_references.values())
            + sum(entry.retained_bytes for entry in parent._recorder_data.values())
        )
        assert result["peak_retained_bytes"] == retained
        assert result["observer_errors"] == result["overlapping_calls"] == result["budget_violations"] == 0
        assert not observation._nested and not any(observation._active.values())
    assert observation.snapshot()["parents"] == [{"budget": parent._shared_text_budget_bytes, "closed_empty": True}]


@pytest.mark.asyncio
@pytest.mark.parametrize("omit_child_delta", (None, "count", "bytes"))
async def test_recorder_observer_attributes_actual_nested_pressure_once(monkeypatch, omit_child_delta):
    import threading

    from elspeth.web import continuation_retention_memo as memo
    from elspeth.web.composer.continuation_audit_buffer import decode_recorder_snapshot
    from tests.helpers.continuation_end_profile import EndTextMemoObservation
    from tests.unit.web.test_continuation_retention_memo import (
        _admit_uncached_recorder_data,
        _advisor_clock,
        _produce_uncached_recorder_data,
        _recorder_data_chunk,
    )

    monkeypatch.setattr(memo, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", 1_048_576)
    seed, following = _recorder_data_chunk("a" * 70_000), _recorder_data_chunk("b" * 220_000)
    observation = EndTextMemoObservation()
    actual_state, omissions = observation._state, []

    def verify(text):
        assert text == "c" * 300_000

    def state(owner, family, cache_key):
        result = actual_state(owner, family, cache_key)
        if (
            omit_child_delta is not None
            and family == "recorder"
            and cache_key is not None
            and cache_key[1].canonical == following.canonical
        ):
            frames = observation._nested.get((observation._parents.get(owner), threading.get_ident()))
            if frames:
                vector = frames[-1][0 if omit_child_delta == "count" else 1]
                assert any(value < 0 for value in vector), "opposition needs a real nested eviction"
                omissions.append(tuple(vector))
                vector[:] = [0] * len(vector)
        return result

    monkeypatch.setattr(observation, "_state", state)
    with observation.install(monkeypatch), memo.v4_preparation_memo_scope(_advisor_clock()) as parent:
        parent.recorder_data(
            memo.PrivateChunkVerificationKey(seed.reference, seed.canonical),
            _produce_uncached_recorder_data,
            _admit_uncached_recorder_data,
        )
        seed_charge = parent._recorder_data_bytes
        parent.canonical_text_verification(memo.CanonicalTextVerificationKey("c" * 300_000, 1_048_576), verify)
        canonical_charge = parent._canonical_verification_bytes
        # These entries fit alongside initial source verification. The real
        # nested credential admission removes canonical; outer recorder
        # publication then removes the prior recorder and full text proof.
        assert seed_charge + canonical_charge + 2 * following.reference.byte_length + 100 < 1_048_576
        with memo.borrow_v4_preparation_memo_scope():
            result = decode_recorder_snapshot(following)
        assert result.withheld_replies[0].content == "b" * 220_000
        counts = observation.snapshot()["families"]
        # Admissions minus evictions must agree with independent actual entry
        # membership even when nested work evicts an outer recorder entry.
        entries = (
            parent._canonical_proofs,
            parent._canonical_verifications,
            parent._recorder_block_hashes,
            parent._v4_recorder_requests,
            parent._credential_strings,
            parent._evidence_references,
            parent._recorder_data,
        )

        def require_actual_accounting():
            assert counts["recorder"]["evictions"] == 1 and counts["recorder"]["evicted_bytes"] == seed_charge
            assert counts["canonical"]["evictions"] == 1 and counts["canonical"]["evicted_bytes"] == canonical_charge
            assert counts["full"]["evictions"] >= 1
            for family, values in zip(observation.families, entries, strict=True):
                assert counts[family]["admissions"] - counts[family]["evictions"] == len(values)

        if omit_child_delta is None:
            require_actual_accounting()
            assert not omissions
        else:
            assert len(omissions) == 1
            with pytest.raises(AssertionError):
                require_actual_accounting()
        result = observation.snapshot()
        assert result["observer_errors"] == result["overlapping_calls"] == result["budget_violations"] == 0
        assert not observation._nested and not any(observation._active.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("orphan", (None, "entries", "bytes"))
async def test_recorder_observer_unretained_work_and_closed_orphan_opposition(monkeypatch, orphan):
    from elspeth.web import continuation_retention_memo as memo
    from tests.helpers.continuation_end_profile import EndTextMemoObservation
    from tests.unit.web.test_continuation_retention_memo import (
        _admit_uncached_recorder_data,
        _advisor_clock,
        _produce_uncached_recorder_data,
        _recorder_data_chunk,
    )

    chunk = _recorder_data_chunk()
    key = memo.PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
    actual_close = memo.RetentionStageMemo.close

    def close_with_orphan(owner):
        entries, charge = dict(owner._recorder_data), owner._recorder_data_bytes
        actual_close(owner)
        if orphan == "entries":
            owner._recorder_data.update(entries)
        elif orphan == "bytes":
            owner._recorder_data_bytes = charge

    monkeypatch.setattr(memo.RetentionStageMemo, "close", close_with_orphan)
    observation = EndTextMemoObservation()
    with observation.install(monkeypatch):
        with memo.v4_preparation_memo_scope(_advisor_clock()) as parent:
            parent.recorder_data(key, _produce_uncached_recorder_data, _admit_uncached_recorder_data)
            assert parent._recorder_data_bytes > 0
        monkeypatch.setattr(memo, "CONTINUATION_V4_PREPARATION_TEXT_MEMO_MAX_RETAINED_BYTES", 1)
        with memo.v4_preparation_memo_scope(_advisor_clock()) as too_small:
            for _ in range(2):
                data = too_small.recorder_data(key, _produce_uncached_recorder_data, _admit_uncached_recorder_data)
                assert data.canonical == chunk.canonical
                assert not too_small._recorder_data and too_small._recorder_data_bytes == 0
    result = observation.snapshot()
    counts = result["families"]["recorder"]
    assert (counts["requests"], counts["misses"], counts["hits"], counts["admissions"], counts["unretained"]) == (3, 3, 0, 1, 2)
    assert result["parents"] == [
        {"budget": parent._shared_text_budget_bytes, "closed_empty": orphan is None},
        {"budget": 1, "closed_empty": True},
    ]


@pytest.mark.asyncio
async def test_recorder_observer_actual_other_thread_overlap_still_invalidates(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from elspeth.web import continuation_retention_memo as memo
    from tests.helpers.continuation_end_profile import EndTextMemoObservation
    from tests.unit.web.test_continuation_retention_memo import (
        _admit_uncached_recorder_data,
        _advisor_clock,
        _produce_uncached_recorder_data,
        _recorder_data_chunk,
    )

    chunk = _recorder_data_chunk()
    key = memo.PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
    rendezvous = threading.Barrier(2)

    def produce(text):
        data = _produce_uncached_recorder_data(text)
        rendezvous.wait(timeout=5)
        return data

    observation = EndTextMemoObservation()
    with (
        observation.install(monkeypatch),
        memo.v4_preparation_memo_scope(_advisor_clock()) as parent,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        first = executor.submit(parent.recorder_data, key, produce, _admit_uncached_recorder_data)
        second = executor.submit(parent.recorder_data, key, produce, _admit_uncached_recorder_data)
        assert first.result(timeout=5) == second.result(timeout=5)
        result = observation.snapshot()
        assert result["overlapping_calls"] > 0 and result["observer_errors"] == 0
        assert not observation._nested and not any(observation._active.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_old_gap", (False, True), ids=("atomic-start", "old-start-gap-opposition"))
async def test_recorder_observer_state_capture_and_registration_are_one_atomic_start(monkeypatch, restore_old_gap):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from elspeth.web import continuation_retention_memo as memo
    from tests.helpers.continuation_end_profile import EndTextMemoObservation
    from tests.unit.web.test_continuation_retention_memo import (
        _admit_uncached_recorder_data,
        _advisor_clock,
        _produce_uncached_recorder_data,
        _recorder_data_chunk,
    )

    chunk = _recorder_data_chunk()
    key = memo.PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
    observation = EndTextMemoObservation()
    actual_state = observation._state
    captured, release, second_entered = threading.Event(), threading.Event(), threading.Event()
    first_thread, paused = [], []

    def state(*args):
        result = actual_state(*args)
        if threading.get_ident() == first_thread[0] and not paused:
            paused.append(True)
            assert observation._lock.locked(), "initial snapshot must hold the registration lock"
            if restore_old_gap:
                # Deliberately restore precisely the reviewed scheduling hole:
                # source snapshot exists but no active frame is registered.
                observation._lock.release()
            captured.set()
            try:
                assert release.wait(5)
            finally:
                if restore_old_gap:
                    observation._lock.acquire()
        return result

    monkeypatch.setattr(observation, "_state", state)
    with (
        observation.install(monkeypatch),
        memo.v4_preparation_memo_scope(_advisor_clock()) as parent,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):

        def first_call():
            first_thread.append(threading.get_ident())
            return parent.recorder_data(key, _produce_uncached_recorder_data, _admit_uncached_recorder_data)

        def second_call():
            second_entered.set()
            return parent.recorder_data(key, _produce_uncached_recorder_data, _admit_uncached_recorder_data)

        first = executor.submit(first_call)
        second = None
        try:
            assert captured.wait(5)
            second = executor.submit(second_call)
            assert second_entered.wait(5)
            if restore_old_gap:
                second.result(timeout=5)  # Finish completely before T1 registers.
            else:
                assert observation._lock.locked()
        finally:
            release.set()
        assert second is not None and first.result(timeout=5) == second.result(timeout=5)
        result = observation.snapshot()
        counts = result["families"]["recorder"]
        assert len(parent._recorder_data) == 1 and result["observer_errors"] == 0

        def require_valid_or_rejected():
            assert result["overlapping_calls"] > 0 or (
                counts["requests"] == 2 and counts["misses"] == counts["hits"] == counts["admissions"] == len(parent._recorder_data)
            )

        if restore_old_gap:
            assert result["overlapping_calls"] == 0
            assert (counts["misses"], counts["hits"], counts["admissions"]) == (2, 0, 2)
            with pytest.raises(AssertionError):
                require_valid_or_rejected()
        else:
            require_valid_or_rejected()
        assert not observation._nested and not any(observation._active.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("callback", "after_state", "misnested"))
async def test_recorder_observer_cleans_nested_frames_on_original_and_observer_failures(monkeypatch, failure):
    from elspeth.web import continuation_retention_memo as memo
    from tests.helpers.continuation_end_profile import EndTextMemoObservation
    from tests.unit.web.test_continuation_retention_memo import (
        _admit_uncached_recorder_data,
        _advisor_clock,
        _produce_uncached_recorder_data,
        _recorder_data_chunk,
    )

    chunk = _recorder_data_chunk()
    key = memo.PrivateChunkVerificationKey(chunk.reference, chunk.canonical)
    observation = EndTextMemoObservation()
    marker = RuntimeError("original recorder admission failure")

    def admit(candidate, data):
        _admit_uncached_recorder_data(candidate, data)
        if failure == "callback":
            raise marker
        if failure == "misnested":
            observation._nested.clear()

    actual_state, calls = observation._state, []

    def state(*args):
        calls.append(True)
        if failure == "after_state" and len(calls) == 2:
            raise RuntimeError("observer state failed")
        return actual_state(*args)

    monkeypatch.setattr(observation, "_state", state)
    with observation.install(monkeypatch), memo.v4_preparation_memo_scope(_advisor_clock()) as parent:
        if failure == "callback":
            with pytest.raises(RuntimeError) as caught:
                parent.recorder_data(key, _produce_uncached_recorder_data, admit)
            assert caught.value is marker and not parent._recorder_data
        else:
            assert parent.recorder_data(key, _produce_uncached_recorder_data, admit).canonical == chunk.canonical
        assert not observation._nested and not any(observation._active.values())
        result = observation.snapshot()
        if failure == "callback":
            assert result["families"]["recorder"]["errors"] == 1 and result["observer_errors"] == 0
        else:
            assert result["observer_errors"] > 0
