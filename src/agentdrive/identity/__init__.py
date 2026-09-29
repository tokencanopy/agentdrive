"""The authentication boundary.

AgentDrive holds no identity of its own. Hub owns principals, workspaces,
memberships, product entitlement and token issuance (§3.1), so this package is
narrow by design: verify what Hub signed, turn it into an actor, and stop.

  * `product_token` — offline validation of Hub-issued bearers against Hub's
    JWKS: exact issuer, audience, expiry, signature, and the normative claim
    contract.
  * `actor` — the verified claims as a `V0ActorContext`, built here and
    nowhere else, so no handler can invent a principal.

This package used to be the local mirror of WorkOS-managed identity: users,
organizations, memberships, onboarding, `ad_user_` tokens. All of it is in
`archive/identity/`, and the tables it read are not in the day-0 schema. That
is the control-plane/data-plane split landing, not a refactor.

Nothing is re-exported. The old `__init__` imported six submodules eagerly,
which meant importing the package pulled in the whole identity plane and its
database dependencies -- and made a circular import out of any module that
wanted just one of them.
"""
