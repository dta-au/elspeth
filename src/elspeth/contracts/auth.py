"""Authentication contracts shared across web identity boundaries.

Layer: L0 (contracts). No upward imports.
"""

from __future__ import annotations

from typing import Literal

AuthProviderType = Literal["local", "oidc", "entra", "vanguard", "google"]
"""Closed discriminator for the ways a browser can authenticate.

One value per registered IdP profile, plus ``local``.  ``service`` is
deliberately absent: a service identity holds an operator-issued credential
and never completes an OIDC walk, so it has no profile and cannot be the
``auth_provider`` of a container, a session, or a user secret.  The profile
registry asserts parity against this Literal at import, which makes an
unregistered value a boot failure rather than a test failure.
"""

IdentityProviderType = Literal[AuthProviderType, "service"]
"""Closed discriminator for how an identity row came to exist.

A superset of :data:`AuthProviderType` by exactly ``service``, so the
``sessions.user_id`` -> ``identities.identity_id`` foreign key stays sound:
every value ``sessions`` admits is a value ``identities`` admits.

Written nested rather than as a union on purpose.  ``AuthProviderType |
Literal["service"]`` is a ``Union``, and ``get_args`` on it returns two nested
``Literal`` objects rather than the six strings, so every membership check and
every contract assertion over it reads the wrong shape.  Nesting the alias
inside ``Literal[...]`` flattens it.
"""

IdentityRole = Literal[
    "admin",
    "approver",
    "reviewer",
    "user",
    "curator",
    "auditor",
    "oversight",
]
"""Closed set of roles an identity may hold.

``user`` may author and run; ``approver`` decides approvals and holds approver
edges; ``reviewer`` attests; ``curator`` gates the library; ``admin`` is
container operations (identity, roles, org tree) and is never combined with a
workload role; ``auditor`` is read-only over the audit surfaces; ``oversight``
is read plus quota-policy write, with no activation, role grant, or disable.

Activation grants a role chosen from ``user``, ``approver``, ``reviewer`` or
``none``.  ``none`` is a request argument, not a stored role -- it writes no
``identity_roles`` row -- so it is not a member here.
"""

RelationshipType = Literal["approver"]
"""Closed set of identity-to-identity edge types.

The org tree carries one job: who oversees whom, for the approver's audit
view.  Approver eligibility and leave cover are role questions, not tree
questions, so no second edge type exists.
"""

ActivationRole = Literal["user", "approver", "reviewer", "none"]
"""What an activation may grant (spec D20).

The three workload roles an administrator may hand out with the "tick of
approval", plus ``none``: a request argument that writes no ``identity_roles``
row, and the only value an identity that already holds ``admin`` may be
activated with (R8).  ``none`` is deliberately not a member of
:data:`IdentityRole` -- nothing stores it.
"""

IdentityAccessState = Literal["pending", "active", "disabled"]
"""Closed vocabulary of ``identities.access_state`` (D12).

``pending`` is where every first sight lands and is not escapable by logging
in again; ``active`` is the only state a token is issued to; ``disabled`` is
where an administrator or a credential deletion leaves a row, which is never
deleted because it anchors that person's audit history.
"""
