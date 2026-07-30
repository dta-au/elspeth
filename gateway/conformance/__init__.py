"""The gateway conformance kit.

An AGENCY runs this package directly against its own derived image, with no
access to ELSPETH internals: every test in this package talks to the gateway
over HTTP only (through the ``gateway_client`` fixture), never by importing
``elspeth_llm_gateway`` itself. ``conftest.py`` is the one file in this
package that does import gateway internals, and only to build the
``gateway_client``/``bearer``/``declared_capabilities`` fixtures -- see its
module docstring for the two modes (in-process vs. a real deployed image)
that fixture supports.
"""
