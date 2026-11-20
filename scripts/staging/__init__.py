"""Operator tools for a staging or single-host ELSPETH deployment.

Every tool takes its deployment-specific values (base URL, accounts,
container names, sessions, destinations) from arguments or environment
variables; none has a default naming a person, host, container or session.
Tools that change anything are dry-run by default and act only with
``--execute``. See ``docs/runbooks/staging-hardware-upgrade-recovery.md``.
"""
