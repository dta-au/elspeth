"""Run governance survives every plugin-context copy."""

from elspeth.contracts.call_governance import LLMCallGovernance
from elspeth.core.events import NullEventBus
from elspeth.engine.orchestrator.ceremony import RunCeremony
from elspeth.engine.orchestrator.source_iteration import SourceIterationDriver
from elspeth.engine.spans import SpanFactory
from tests.fixtures.factories import make_context


def test_governance_survives_contract_and_idle_timeout_contexts() -> None:
    governance = LLMCallGovernance(lambda: "attempt-1", lambda attempt, call: None)
    context = make_context()
    context.llm_call_governance = governance
    assert context.for_contract(None).llm_call_governance is governance
    events = NullEventBus()
    driver = SourceIterationDriver(events=events, span_factory=SpanFactory(), ceremony=RunCeremony(events=events, telemetry=None))
    assert driver._idle_timeout_context(context).llm_call_governance is governance


def test_standalone_context_has_no_host_governance() -> None:
    context = make_context()
    assert context.llm_call_governance is None
    assert context.for_contract(None).llm_call_governance is None
