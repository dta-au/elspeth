"""Root-repo CI-gate wiring for the ``gateway/`` LLM compatibility gateway.

This package runs the gateway's own test suites (``gateway/tests`` and
``gateway/conformance``) as subprocesses from the ELSPETH ``pytest tests/``
run, without ever importing ``elspeth_llm_gateway`` into the ELSPETH test
session itself -- see ``test_inprocess_conformance.py``.
"""
