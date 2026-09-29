"""V0ActorContext: who is acting, derived only from a verified token."""

from __future__ import annotations

import dataclasses
import time

import pytest

from agentdrive.identity import product_token as pt
from agentdrive.identity.actor import V0ActorContext

ISSUER = "https://auth.tokencanopy.test/oidc"
AUDIENCE = "https://api.tokencanopy.test/drive"


@pytest.fixture
def verifier(hub_jwks):
    return pt.ProductTokenVerifier(
        issuer=ISSUER, audience=AUDIENCE, jwks=hub_jwks.public_jwks
    )


def _claims(**over):
    now = int(time.time())
    base = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 3600,
        "jti": "tctok_0000000000000001",
        "workspace_id": "tcws_0000000000000001",
        "sub": "tcagt_0000000000000001",
        "membership_id": "tcagm_0000000000000001",
        "scope": "content:read content:write",
        # Hub mints `client_id` on every agent token (it is the OAuth
        # access-token contract's client identifier), and the normative claim
        # table requires it on this branch — so the fixture carries it.
        "client_id": "tccred_0000000000000001",
        "credential_id": "tccred_0000000000000001",
        "runtime_id": "tcrun_0000000000000001",
        "sponsor_id": "tcusr_0000000000000009",
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


def _actor(verifier, hub_jwks, **over):
    return V0ActorContext.from_claims(verifier.verify(hub_jwks.sign(_claims(**over))))


def test_agent_actor_keeps_provenance_distinct(verifier, hub_jwks):
    """§6.7 keeps subject, credential, runtime, workspace and token id
    separate so provenance can tell an agent from the credential it used."""
    a = _actor(verifier, hub_jwks)
    assert (a.subject, a.subject_type, a.is_agent) == (
        "tcagt_0000000000000001", "agent", True,
    )
    assert a.credential_id == "tccred_0000000000000001"
    assert a.runtime_id == "tcrun_0000000000000001"
    assert a.sponsor_id == "tcusr_0000000000000009"
    assert a.token_id == "tctok_0000000000000001"


def test_scope_is_only_half_the_check(verifier, hub_jwks):
    a = _actor(verifier, hub_jwks)
    assert a.can("content:write")
    assert not a.can("sharing:write")


def test_an_agent_can_never_be_a_workspace_admin(verifier, hub_jwks):
    """Break-glass recovery is admin-gated (§6.8). `workspace_role` is
    human-only and product_token rejects it on an agent token, so this
    property has no path to True for an agent -- asserted here because the
    consequence of it ever being True is an agent granting itself access."""
    assert not _actor(verifier, hub_jwks).is_workspace_admin

    with pytest.raises(pt.InvalidToken):
        _actor(verifier, hub_jwks, workspace_role="admin")


def test_human_admin_is_recognized(verifier, hub_jwks):
    a = _actor(
        verifier, hub_jwks,
        sub="tcusr_0000000000000001", membership_id="tcusm_0000000000000001",
        workspace_role="admin", scope="drives:read",
        client_id=None, credential_id=None, runtime_id=None, sponsor_id=None,
    )
    assert a.subject_type == "user"
    assert a.is_workspace_admin
    assert not a.is_agent


def test_a_human_owner_is_a_workspace_admin(verifier, hub_jwks):
    """Hub mints `workspace_role` as owner | admin | member VERBATIM, and an
    owner outranks an admin — the property must admit both. Testing
    `== "admin"` alone locked workspace owners out of every admin-gated
    path (break-glass, and now the workspace-admin overlay)."""
    a = _actor(
        verifier, hub_jwks,
        sub="tcusr_0000000000000001", membership_id="tcusm_0000000000000001",
        workspace_role="owner", scope="drives:read",
        client_id=None, credential_id=None, runtime_id=None, sponsor_id=None,
    )
    assert a.subject_type == "user"
    assert a.is_workspace_admin


def test_a_human_member_is_not_an_admin(verifier, hub_jwks):
    a = _actor(
        verifier, hub_jwks,
        sub="tcusr_0000000000000001", membership_id="tcusm_0000000000000001",
        workspace_role="member", scope="drives:read",
        client_id=None, credential_id=None, runtime_id=None, sponsor_id=None,
    )
    assert not a.is_workspace_admin


def test_change_actor_exposes_subject_only(verifier, hub_jwks):
    """The feed names who acted, not what they authenticated with -- a
    credential id in a change row would leak it to every reader."""
    block = _actor(verifier, hub_jwks).change_actor()
    assert block == {"type": "agent", "id": "tcagt_0000000000000001"}


def test_an_actor_cannot_be_mutated(verifier, hub_jwks):
    """Established once at the boundary. A handler that could widen its own
    scopes mid-request would make the scope check advisory."""
    a = _actor(verifier, hub_jwks)
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.scopes = frozenset({"sharing:write"})  # type: ignore[misc]
