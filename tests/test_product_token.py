"""Hub product-token validation (§7.1, companion identity spec §6.2–§6.4).

AgentDrive is no longer an authorization server. It verifies tokens Hub
issued, offline, against Hub's JWKS — which is why every one of these is a
rejection test. The claims contract is normative, so the interesting cases are
the ones where a token is *almost* right: correct signature but the wrong
audience, the right shape but a human claim on an agent token.

Three token forms exist and all must work: agent (`sub` is `tcagt_*`, carries
`client_id`/`credential_id`/`runtime_id`/`sponsor_id`), human (`sub` is
`tcusr_*`, carries `workspace_role`, and none of the agent-only claims), and
service (`sub` is `tcsvc_*`, carries `client_id`/`credential_id` and NO
membership, runtime, sponsor, or workspace role).
"""

from __future__ import annotations

import time

import jwt
import pytest

from agentdrive.identity import product_token as pt

ISSUER = "https://auth.tokencanopy.test/oidc"
AUDIENCE = "https://api.tokencanopy.test/drive"
GIB = 1024**3
DEFAULT_LIMITS = {
    "version": 1,
    "storage_bytes_drive": 10 * GIB,
    "storage_bytes_workspace": 50 * GIB,
    "upload_bytes_hour_principal": 10 * GIB,
    "upload_bytes_hour_workspace": 25 * GIB,
    "download_bytes_day_workspace": 50 * GIB,
    "download_bytes_month_workspace": 250 * GIB,
    "public_share_bytes_day": 5 * GIB,
}


def _agent_claims(**over):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "tcagt_0000000000000001",
        "aud": AUDIENCE,
        "scope": "content:read content:write",
        "iat": now,
        "exp": now + 3600,
        "jti": "tctok_0000000000000001",
        "workspace_id": "tcws_0000000000000001",
        "membership_id": "tcagm_0000000000000001",
        "client_id": "tccred_0000000000000001",
        "credential_id": "tccred_0000000000000001",
        "runtime_id": "tcrun_0000000000000001",
        "sponsor_id": "tcusr_0000000000000009",
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


def _human_claims(**over):
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "tcusr_0000000000000001",
        "aud": AUDIENCE,
        "scope": "drives:read",
        "iat": now,
        "exp": now + 3600,
        "jti": "tctok_0000000000000002",
        "workspace_id": "tcws_0000000000000001",
        "membership_id": "tcusm_0000000000000001",
        "workspace_role": "admin",
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


@pytest.fixture
def verifier(hub_jwks):
    return pt.ProductTokenVerifier(
        issuer=ISSUER, audience=AUDIENCE, jwks=hub_jwks.public_jwks
    )


# ---------------------------------------------------------------------------
# The happy paths, so the rejections below mean something
# ---------------------------------------------------------------------------


def test_agent_token_verifies(verifier, hub_jwks):
    claims = verifier.verify(hub_jwks.sign(_agent_claims()))
    assert claims.subject == "tcagt_0000000000000001"
    assert claims.subject_type == "agent"
    assert claims.workspace_id == "tcws_0000000000000001"
    assert claims.scopes == {"content:read", "content:write"}
    assert claims.sponsor_id == "tcusr_0000000000000009"


def test_human_token_verifies(verifier, hub_jwks):
    claims = verifier.verify(hub_jwks.sign(_human_claims()))
    assert claims.subject_type == "user"
    assert claims.workspace_role == "admin"
    assert claims.sponsor_id is None


def test_drive_limits_claim_is_typed_and_carried_to_actor(verifier, hub_jwks):
    from agentdrive.identity.actor import V0ActorContext

    claims = verifier.verify(
        hub_jwks.sign(_human_claims(drive_limits=DEFAULT_LIMITS))
    )
    actor = V0ActorContext.from_claims(claims)
    assert actor.drive_limits.storage_bytes_workspace == 50 * GIB


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"version": 2},
        {**DEFAULT_LIMITS, "storage_bytes_drive": 0},
        {**DEFAULT_LIMITS, "storage_bytes_drive": True},
        {**DEFAULT_LIMITS, "unknown": 1},
    ],
)
def test_invalid_drive_limit_claim_is_rejected(verifier, hub_jwks, bad):
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(_human_claims(drive_limits=bad)))


def test_drive_limits_claim_can_be_required(hub_jwks):
    verifier = pt.ProductTokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=hub_jwks.public_jwks,
        limits_claim_required=True,
    )
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(_human_claims()))


def test_drive_limits_above_deployment_ceiling_are_rejected(verifier, hub_jwks):
    too_large = {
        **DEFAULT_LIMITS,
        "storage_bytes_drive": 101 * GIB,
    }
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(_human_claims(drive_limits=too_large)))


def test_azp_is_tolerated_on_a_human_token(verifier, hub_jwks):
    """A human token minted BY the console BFF on the session holder's
    behalf carries `azp` (the authorized party) alongside the §2.3 human
    claim set.

    `azp` is a standard OIDC/RFC 9068 claim and is additive: it changes
    neither the required set nor the agent-only/human-only exclusions, so
    this verifier must accept it unchanged. Pinned here because the Hub
    side now emits it (tokencanopy `src/console/product-token.ts`) — if
    this verifier ever started rejecting unknown claims, the console
    would break with no other test noticing.
    """
    claims = verifier.verify(
        hub_jwks.sign(
            _human_claims(azp="tokencanopy-console", scope="content:read")
        )
    )
    assert claims.subject_type == "user"
    assert claims.workspace_role == "admin"
    # The claim is not part of the authorization decision — it is
    # attribution the product may log, not authority it may act on.
    assert claims.has_scope("content:read")


# ---------------------------------------------------------------------------
# Signature and envelope
# ---------------------------------------------------------------------------


def test_a_token_signed_by_someone_else_is_rejected(verifier, foreign_jwks):
    """An unknown key is rejected before the signature is even considered."""
    with pytest.raises(pt.InvalidToken):
        verifier.verify(foreign_jwks.sign(_agent_claims()))


def test_a_foreign_key_claiming_hub_kid_is_rejected(verifier, foreign_jwks):
    """The actual trust model: only Hub's KEY means anything, not its kid.

    The test above never reaches signature verification -- the foreign key
    carries its own kid, so it dies at the key lookup with zero calls to
    `jwt.decode`. This one presents a token signed by a key Hub does not hold
    while claiming Hub's kid, which is the forgery a real attacker attempts.
    Without it, deleting the signature check entirely leaves the suite green.
    """
    with pytest.raises(pt.InvalidToken):
        verifier.verify(foreign_jwks.sign(_agent_claims(), kid="hub-key-1"))


def test_an_unsigned_token_is_rejected(verifier):
    """`alg: none` is the oldest JWT attack and must never be accepted.

    Carries a valid kid deliberately: PyJWT's `algorithm="none"` emits no kid
    header, so the naive form of this test is also short-circuited at the key
    lookup and never reaches the algorithm allow-list.
    """
    forged = jwt.encode(_agent_claims(), key="", algorithm="none",
                        headers={"kid": "hub-key-1"})
    with pytest.raises(pt.InvalidToken):
        verifier.verify(forged)


def test_an_hmac_token_signed_with_the_public_key_is_rejected(verifier, hub_jwks):
    """Algorithm confusion: sign HS256 using Hub's PUBLIC key as the secret.

    A verifier that resolved the algorithm from the token header rather than
    from an allow-list would accept this, because the "secret" is public.
    """
    import base64
    import hashlib
    import hmac
    import json

    from cryptography.hazmat.primitives import serialization

    pub = hub_jwks._private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Hand-rolled, because PyJWT refuses to ENCODE with an asymmetric key as
    # an HMAC secret. That protects us at signing time and not at all at
    # verification time -- and an attacker is not using our encoder.
    def seg(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj, separators=(",", ":")).encode()
        ).rstrip(b"=")

    signing_input = (
        seg({"alg": "HS256", "typ": "JWT", "kid": "hub-key-1"})
        + b"."
        + seg(_agent_claims())
    )
    mac = hmac.new(pub, signing_input, hashlib.sha256).digest()
    forged = (
        signing_input + b"." + base64.urlsafe_b64encode(mac).rstrip(b"=")
    ).decode()

    with pytest.raises(pt.InvalidToken):
        verifier.verify(forged)


def test_garbage_is_rejected(verifier):
    for junk in ("", "not.a.token", "a.b.c", "Bearer x"):
        with pytest.raises(pt.InvalidToken):
            verifier.verify(junk)


# ---------------------------------------------------------------------------
# Exact issuer / audience / expiry (§6.2, §6.4)
# ---------------------------------------------------------------------------


def test_wrong_issuer_is_rejected(verifier, hub_jwks):
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(_agent_claims(iss="https://evil.test/oidc")))


def test_a_token_for_another_product_is_rejected(verifier, hub_jwks):
    """§6.4: one audience per token. An AgentChat token must be useless here,
    or every product in the suite shares one blast radius."""
    with pytest.raises(pt.InvalidToken):
        verifier.verify(
            hub_jwks.sign(_agent_claims(aud="https://api.tokencanopy.test/agentchat"))
        )


def test_the_bare_gateway_audience_is_rejected(verifier, hub_jwks):
    """§6.4 forbids it explicitly -- it would authorize every product."""
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(_agent_claims(aud="https://api.tokencanopy.test")))


def test_an_expired_token_is_rejected(verifier, hub_jwks):
    now = int(time.time())
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(_agent_claims(iat=now - 7200, exp=now - 3600)))


# ---------------------------------------------------------------------------
# The normative claim contract (§6.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    ["sub", "scope", "jti", "workspace_id", "membership_id"],
)
def test_every_required_claim_is_required(verifier, hub_jwks, missing):
    """§6.2 calls this set uniform and required. A token missing workspace_id
    would otherwise reach the authorization layer with nothing to scope."""
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(_agent_claims(**{missing: None})))


def test_agent_only_claims_are_rejected_on_a_human_token(verifier, hub_jwks):
    """§6.2 states this as a product-side obligation. A human token carrying
    credential_id would let a human impersonate a credentialed runtime in
    provenance."""
    with pytest.raises(pt.InvalidToken):
        verifier.verify(
            hub_jwks.sign(_human_claims(credential_id="tccred_0000000000000001"))
        )


def test_a_human_oauth_token_carrying_client_id_is_accepted(verifier, hub_jwks):
    """Hub mints human tokens two ways. The BFF omits `client_id` (issuance
    design 2.3 -- no OAuth client behind a browser session); the OAuth path
    MUST emit it, because RFC 9068 2.2 lists it as required. Reading its mere
    presence as an agent marker rejected every OAuth client -- MCP included --
    while the console kept working because it uses the path that omits it."""
    claims = verifier.verify(
        hub_jwks.sign(
            _human_claims(
                client_id="https://claude.ai/oauth/claude-code-client-metadata"
            )
        )
    )
    assert claims.subject_type == "user"
    assert claims.credential_id is None


def test_a_human_token_without_client_id_is_still_accepted(verifier, hub_jwks):
    """The console path. `client_id` is OPTIONAL on a human token -- requiring
    it would reject every BFF-minted token, which is the opposite failure."""
    claims = verifier.verify(hub_jwks.sign(_human_claims()))
    assert claims.subject_type == "user"


def test_a_credential_namespace_client_is_rejected_on_a_human_token(
    verifier, hub_jwks
):
    """What `client_id` CAN still prove. Hub sets an agent token's `client_id`
    to the credential id, and an OIDC client id may never sit in that reserved
    namespace -- so a human subject presenting one is incoherent. This is the
    check that would be LOST by simply dropping the claim from the agent-only
    set rather than narrowing it to a namespace."""
    with pytest.raises(pt.InvalidToken):
        verifier.verify(
            hub_jwks.sign(_human_claims(client_id="tccred_0000000000000001"))
        )


def test_workspace_role_is_rejected_on_an_agent_token(verifier, hub_jwks):
    """The mirror obligation. workspace_role drives token-verified admin
    checks such as break-glass recovery -- an agent must never carry one."""
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(_agent_claims(workspace_role="admin")))


def test_an_unknown_subject_prefix_is_rejected(verifier, hub_jwks):
    """Only tcagt_ and tcusr_ act (§6.7). Anything else means Hub started
    issuing a form this version does not understand -- fail closed."""
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(_agent_claims(sub="tcbot_0000000000000001")))


def test_scope_is_parsed_as_a_set_not_a_string(verifier, hub_jwks):
    """Scope checks are membership tests; a substring test would let
    `content:read` satisfy a `content:read_secret` check."""
    claims = verifier.verify(hub_jwks.sign(_agent_claims(scope="drives:read")))
    assert claims.scopes == {"drives:read"}
    assert not claims.has_scope("drives:write")
    assert claims.has_scope("drives:read")


def test_unknown_scopes_do_not_grant_anything(verifier, hub_jwks):
    """§7.1: jobs:read/jobs:write are not v0 vocabulary (D11). An unknown
    scope must be inert rather than quietly authorizing."""
    claims = verifier.verify(hub_jwks.sign(_agent_claims(scope="jobs:read")))
    assert not claims.has_scope("content:read")
    assert claims.unknown_scopes == {"jobs:read"}


def test_a_multi_audience_token_is_rejected(verifier, hub_jwks):
    """§6.4: one audience per token, stated as a prohibition.

    PyJWT treats a list-valued `aud` as a match when the configured audience
    appears anywhere in it, so this token would otherwise be valid here AND
    at AgentChat -- the shared blast radius §6.4 forbids by name.
    """
    with pytest.raises(pt.InvalidToken):
        verifier.verify(
            hub_jwks.sign(
                _agent_claims(aud=[AUDIENCE, "https://api.tokencanopy.test/agentchat"])
            )
        )


def test_a_single_element_audience_list_is_accepted(verifier, hub_jwks):
    """`aud: ["…/drive"]` is one audience, spelled as a list. Rejecting it
    would break a conforming issuer over JSON shape rather than semantics."""
    claims = verifier.verify(hub_jwks.sign(_agent_claims(aud=[AUDIENCE])))
    assert claims.subject_type == "agent"


def test_one_unparseable_key_does_not_take_down_the_whole_jwks(hub_jwks):
    """Hub's JWKS is a document, and a single entry PyJWT cannot model must
    not abort verifier construction. If a bad entry raised out of the key
    loop, no verifier would be built at all -- every signing key in the same
    document, good ones included, would stop authenticating anyone. A bad
    key is skipped; the good key still verifies.
    """
    good = hub_jwks.public_jwks["keys"][0]
    poisoned = {
        "keys": [
            good,
            {"kid": "broken-key-1"},  # no kty → PyJWK raises
            {"kid": "broken-key-2", "kty": "RSA"},  # no n/e → PyJWK raises
        ]
    }
    v = pt.ProductTokenVerifier(issuer=ISSUER, audience=AUDIENCE, jwks=poisoned)
    claims = v.verify(hub_jwks.sign(_agent_claims()))
    assert claims.subject == "tcagt_0000000000000001"


def test_a_jwks_of_only_unparseable_keys_is_rejected(hub_jwks):
    """Skipping bad keys must not turn 'nothing usable' into a silent accept:
    a JWKS where every entry fails to model still fails closed at build time,
    exactly as an empty JWKS does."""
    with pytest.raises(ValueError):
        pt.ProductTokenVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks={"keys": [{"kid": "broken-key-1"}, {"kid": "broken-key-2"}]},
        )


# ---------------------------------------------------------------------------
# The third branch: Service Accounts (service account design §6.3, §15 step 3)
# ---------------------------------------------------------------------------


def _service_claims(**over):
    """A Service token: no membership, runtime, sponsor, or workspace role.

    Built from scratch rather than by mutating `_agent_claims`, because what
    makes it a Service token is as much what it OMITS as what it carries.
    """
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "tcsvc_0000000000000001",
        "principal_type": "service",
        "aud": AUDIENCE,
        "scope": "drives:read content:read",
        "iat": now,
        "exp": now + 3600,
        "jti": "tctok_0000000000000003",
        "workspace_id": "tcws_0000000000000001",
        "client_id": "tck_0000000000000001",
        "credential_id": "tck_0000000000000001",
    }
    claims.update(over)
    return {k: v for k, v in claims.items() if v is not None}


def test_service_token_verifies(verifier, hub_jwks):
    claims = verifier.verify(hub_jwks.sign(_service_claims()))
    assert claims.subject == "tcsvc_0000000000000001"
    assert claims.subject_type == "service"
    assert claims.credential_id == "tck_0000000000000001"
    assert claims.scopes == {"drives:read", "content:read"}


def test_a_service_token_carries_no_membership_runtime_sponsor_or_role(
    verifier, hub_jwks
):
    """A Service Account has none of these. Every authorization path that
    reads one must find nothing rather than a stale or invented value."""
    claims = verifier.verify(hub_jwks.sign(_service_claims()))
    assert claims.membership_id is None
    assert claims.runtime_id is None
    assert claims.sponsor_id is None
    assert claims.workspace_role is None


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param(
            _service_claims(sub="tcusr_0000000000000001"), id="service-on-human-sub"
        ),
        pytest.param(
            _service_claims(sub="tcagt_0000000000000001"), id="service-on-agent-sub"
        ),
        pytest.param(
            _human_claims(principal_type="human", sub="tcsvc_0000000000000001"),
            id="human-on-service-sub",
        ),
        pytest.param(
            _agent_claims(principal_type="agent", sub="tcsvc_0000000000000001"),
            id="agent-on-service-sub",
        ),
        pytest.param(
            _human_claims(principal_type="human", sub="tcagt_0000000000000001"),
            id="human-on-agent-sub",
        ),
        pytest.param(
            _agent_claims(principal_type="agent", sub="tcusr_0000000000000001"),
            id="agent-on-human-sub",
        ),
        pytest.param(_agent_claims(principal_type="robot"), id="unknown-type"),
        pytest.param(_agent_claims(principal_type=""), id="empty-type"),
        pytest.param(_agent_claims(principal_type=7), id="non-string-type"),
    ],
)
def test_principal_type_must_agree_with_the_subject_namespace(
    verifier, hub_jwks, claims
):
    """§6.3: `principal_type` is an explicit discriminator, never an
    independent grant of authority. A present mismatch is fatal even when
    every other claim is well-formed."""
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(claims))


@pytest.mark.parametrize(
    "forbidden",
    ["membership_id", "runtime_id", "sponsor_id", "workspace_role"],
)
def test_forbidden_claims_are_rejected_on_a_service_token(
    verifier, hub_jwks, forbidden
):
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(_service_claims(**{forbidden: "x"})))


@pytest.mark.parametrize("required", ["client_id", "credential_id"])
def test_machine_client_claims_are_required_on_a_service_token(
    verifier, hub_jwks, required
):
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(_service_claims(**{required: None})))


@pytest.mark.parametrize("branch", ["human", "agent"])
def test_membership_id_is_still_required_on_the_human_and_agent_branches(
    verifier, hub_jwks, branch
):
    """The claim moved out of the UNIFORM required set into two branches.
    It did not become optional."""
    build = _human_claims if branch == "human" else _agent_claims
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(build(membership_id=None)))


def test_a_service_subject_without_the_discriminator_is_rejected(verifier, hub_jwks):
    """Service is new, so it has no legacy form. The derivation below covers
    only the two namespaces that predate the discriminator."""
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(_service_claims(principal_type=None)))


@pytest.mark.parametrize(
    "claims,expected",
    [
        pytest.param(_human_claims(), "user", id="legacy-human"),
        pytest.param(_agent_claims(), "agent", id="legacy-agent"),
    ],
)
def test_a_legacy_token_without_the_discriminator_still_verifies(
    verifier, hub_jwks, claims, expected
):
    """§15 step 3: tokens minted before Hub emits `principal_type` stay valid
    for up to their full lifetime. Step 5 removes this."""
    verified = verifier.verify(hub_jwks.sign(claims))
    assert verified.subject_type == expected


@pytest.mark.parametrize(
    "claims,expected",
    [
        pytest.param(_human_claims(principal_type="human"), "user", id="human"),
        pytest.param(_agent_claims(principal_type="agent"), "agent", id="agent"),
    ],
)
def test_an_explicit_discriminator_is_accepted_on_the_existing_branches(
    verifier, hub_jwks, claims, expected
):
    """Hub starts emitting it on every token in §15 step 4. This verifier
    must already accept it, which is why step 3 deploys first."""
    verified = verifier.verify(hub_jwks.sign(claims))
    assert verified.subject_type == expected


# ---------------------------------------------------------------------------
# Strictness: absent vs null, and the machine-client identity equality
# ---------------------------------------------------------------------------
#
# Chat's verifier checks claim PRESENCE and enforces
# `client_id == credential_id`. These pinned the same rules here, because two
# products disagreeing about what a well-formed token is means Hub can mint
# one that half the platform accepts — and the half that accepts it is the
# half that was more permissive, which is the wrong half to be authoritative.


@pytest.mark.parametrize(
    "claim",
    ["membership_id", "runtime_id", "sponsor_id", "workspace_role"],
)
def test_an_explicit_null_forbidden_claim_is_still_forbidden(
    verifier, hub_jwks, claim
):
    """`{"membership_id": null}` is a claim the token CARRIES. Reading it as
    absent lets a malformed token through the branch shape it violates."""
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign({**_service_claims(), claim: None}))


def test_an_explicit_null_principal_type_is_not_treated_as_legacy(verifier, hub_jwks):
    """The legacy window derives a MISSING discriminator. A present null is a
    malformed one, and deriving from it would resurrect the exact
    shape-inference the discriminator replaced."""
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign({**_agent_claims(), "principal_type": None}))


@pytest.mark.parametrize("claim", ["sub", "scope", "jti", "workspace_id"])
def test_a_non_string_required_claim_is_rejected(verifier, hub_jwks, claim):
    """`str()` on a number produces a plausible-looking id. A claim that is
    not a string was never the thing it is being read as."""
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign({**_agent_claims(), claim: 12345}))


@pytest.mark.parametrize("branch", ["service", "agent"])
def test_client_id_and_credential_id_must_agree(verifier, hub_jwks, branch):
    """§6.3 carries the same value in both in v1. They are separate claims so
    a future authentication method may split them — until one does, a
    disagreement is a malformed token, not a preview of that future."""
    build = _service_claims if branch == "service" else _agent_claims
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(build(client_id="tck_other")))


@pytest.mark.parametrize("branch", ["service", "agent", "human"])
def test_an_empty_string_claim_is_rejected(verifier, hub_jwks, branch):
    build = {
        "service": _service_claims,
        "agent": _agent_claims,
        "human": _human_claims,
    }[branch]
    with pytest.raises(pt.InvalidToken):
        verifier.verify(hub_jwks.sign(build(workspace_id="")))
