"""Deterministic mock stack: mock OAuth token server + mock fictional upstream.

Backs local development (a from-scratch quick start against the gateway
without any real agency credentials) and the Task 13 conformance kit's
OAuth/upstream fixtures. Every response either mock app produces is a pure
function of its request -- no randomness, no wall-clock dependence -- so
runs against it are exactly reproducible.
"""
