"""The opaque API key primitives (§4.2 as amended 2026-09-21).

Shape, hashing and the display id, with no database: the resolver's
behaviour against real rows lives in `tests/test_local_mode.py` and
`tests/test_internal_introspect.py`.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from agentdrive.identity.actor import V0ActorContext
from agentdrive.identity.api_keys import (
    ALL_SCOPES,
    DISPLAY_ID_CHARS,
    KEY_PREFIX,
    KEY_SECRET_CHARS,
    display_id,
    generate_key,
    key_hash,
    looks_like_key,
    new_subject,
)
from agentdrive.identity.product_token import V0_SCOPES


def test_a_key_is_the_prefix_plus_40_base64url_characters():
    key = generate_key()
    assert key.startswith(KEY_PREFIX)
    secret = key[len(KEY_PREFIX) :]
    assert len(secret) == KEY_SECRET_CHARS
    # Decodes as 30 bytes of base64url, which is where the 240 bits are.
    assert len(base64.urlsafe_b64decode(secret + "==")) == 30
    assert "=" not in key


def test_keys_are_unique():
    assert len({generate_key() for _ in range(200)}) == 200


def test_the_hash_is_of_the_whole_key():
    key = generate_key()
    assert key_hash(key) == hashlib.sha256(key.encode()).hexdigest()
    assert len(key_hash(key)) == 64
    assert key_hash(key) != key_hash(key[:-1] + ("A" if key[-1] != "A" else "B"))


def test_the_display_id_names_a_key_without_revealing_it():
    key = generate_key()
    ident = display_id(key)
    assert ident == KEY_PREFIX + key[len(KEY_PREFIX) : len(KEY_PREFIX) + DISPLAY_ID_CHARS]
    assert key.startswith(ident)
    # 8 of 40 secret characters: enough to name one key among a handful,
    # far too little to reconstruct the other 32.
    assert len(ident) == len(KEY_PREFIX) + DISPLAY_ID_CHARS
    assert len(ident) < len(key)
    with pytest.raises(ValueError):
        display_id("not-a-key")


def test_the_shape_check_decides_a_path_not_a_verdict():
    assert looks_like_key(generate_key())
    for bad in ("", "adk_", "adk_short", "adk_" + "x" * 41, "tok_" + "x" * 40, "x" * 44):
        assert not looks_like_key(bad), bad


def test_the_scope_vocabulary_is_the_verifiers():
    assert set(ALL_SCOPES) == set(V0_SCOPES)
    assert tuple(sorted(ALL_SCOPES)) == ALL_SCOPES, "a stable order for --scopes help"
    assert len(ALL_SCOPES) == 8


def test_subjects_keep_the_canonical_namespaces():
    assert new_subject("agent").startswith("tcagt_")
    assert new_subject("user").startswith("tcusr_")
    assert new_subject("agent") != new_subject("agent")


def test_the_actor_a_key_builds_matches_the_shape_a_hub_token_yields():
    agent = V0ActorContext.from_local_key(
        subject="tcagt_0123456789abcdef",
        principal_type="agent",
        workspace_id="default",
        scopes=frozenset({"drives:read"}),
        workspace_role=None,
        sponsor_id="tcusr_fedcba9876543210",
        key_id="adk_k7Qm2xZp",
    )
    assert agent.is_agent and not agent.is_workspace_admin
    assert agent.sponsor_id == "tcusr_fedcba9876543210"
    assert agent.token_id == "adk_k7Qm2xZp"
    # Provenance ids are derived and namespaced `local_`, so an artifact a
    # self-hosted key minted is never mistaken for one a Hub credential did.
    assert agent.membership_id.startswith("local_")
    assert agent.credential_id == "tccred_local_k7Qm2xZp"
    assert agent.runtime_id == "tcrun_local_k7Qm2xZp"

    user = V0ActorContext.from_local_key(
        subject="tcusr_fedcba9876543210",
        principal_type="user",
        workspace_id="default",
        scopes=frozenset(ALL_SCOPES),
        workspace_role="owner",
        sponsor_id=None,
        key_id="adk_aaaaaaaa",
    )
    assert user.is_workspace_admin
    # A human never carries a credential or a runtime — the branch rules
    # `product_token` enforces on a Hub token, held here too.
    assert user.credential_id is None and user.runtime_id is None
    assert user.sponsor_id is None

    with pytest.raises(ValueError, match="principal_type"):
        V0ActorContext.from_local_key(
            subject="tcsvc_x", principal_type="service", workspace_id="w",
            scopes=frozenset(), workspace_role=None, sponsor_id=None, key_id="adk_x",
        )
