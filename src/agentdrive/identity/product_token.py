"""Offline validation of Hub-issued product access tokens.

AgentDrive is not an authorization server (§9.1). It verifies what Hub signed,
against Hub's published JWKS, without a synchronous call to Hub on every
request — which is the point of an asymmetric signature and a one-hour
lifetime. The tradeoff is stated in the companion identity spec §6.2: revoking
a credential stops new issuance immediately, but an already-issued token stays
usable until it expires, so an emergency denylist is a separate mechanism.

Validation is deliberately strict and fails closed. Everything below rejects:

  * a signature from any key but Hub's, including `alg: none`;
  * an issuer or audience that is not exactly the configured one — §6.4
    forbids the bare gateway audience, because a token valid at every product
    makes one product's compromise every product's;
  * a missing member of the uniform required claim set;
  * a `principal_type` that disagrees with the `sub` namespace, or a branch
    carrying another branch's claims — §6.2 names this as a product-side
    obligation, and it is what stops a human token impersonating a
    credentialed runtime in artifact provenance, or a Service Account
    claiming a workspace membership it does not have.

Three principal branches exist, discriminated by `principal_type` and pinned
to their `sub` namespaces: Human (`tcusr_*`), Agent (`tcagt_*`), and Service
(`tcsvc_*`, a customer-owned backend integration with no membership, runtime,
or sponsor — Token Canopy service account design §6.3).

What this module does NOT do is authorize. It returns claims. Scope is only
half the check: §7.1 requires `scope ∩ local-grant`, and the local half lives
in Layer 3's capability resolution. A token with `content:write` and no grant
authorizes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import jwt
from jwt import PyJWKClient  # noqa: F401  (re-exported for the config lane)

from agentdrive.core.usage.models import DriveLimitsV1
from agentdrive.core.usage.policy import default_drive_limits, parse_drive_limits_v1

# §7.1. Vocabulary, not a default grant set: every issued token lists its
# scopes explicitly and Hub grants nothing when `scope` is omitted.
# `jobs:read`/`jobs:write` are deliberately absent (D11) -- a token asking for
# them is rejected at Hub rather than issued a scope authorizing nothing.
V0_SCOPES = frozenset(
    {
        "drives:read",
        "drives:write",
        "content:read",
        "content:write",
        "changes:read",
        "sharing:read",
        "sharing:write",
        "usage:read",
    }
)

# The uniform required set (companion §6.2). `aud`, `iss` and `exp` are
# validated by the JWT library itself and so are not repeated here.
#
# `membership_id` is NOT here. It moved into the Human and Agent branch sets
# below when Service Accounts arrived (service account design §6.3): a Service
# has no workspace membership, so a uniform requirement would have made a
# correct Service token unverifiable. It did not become optional -- both
# branches that had it still require it.
# `iat` is deliberately absent: it is a NUMBER, and PyJWT already requires and
# type-checks it (`options={"require": [...]}`) alongside `exp`/`iss`/`aud`.
# Listing it here would only mean asserting the wrong type on it.
_REQUIRED_CLAIMS = ("sub", "scope", "jti", "workspace_id")

# Claims each branch must carry, and must not. The forbidden sets are what
# stop one branch impersonating another: a human token carrying credential_id
# would claim a runtime it does not have in artifact provenance, and a Service
# token carrying membership_id would claim a workspace membership that does
# not exist.
#
# `client_id` is deliberately NOT forbidden on the human branch, though it was
# until Hub grew a second way to mint a human token. Hub's BFF path omits the
# claim by contract (issuance design section 2.3 -- no OAuth client exists
# behind a browser session), while its OAuth path MUST emit it, because
# RFC 9068 section 2.2 lists it as required on a JWT access token. Presence
# therefore proves nothing about who is calling. What DOES separate the
# families is the client id's NAMESPACE: Hub mints an agent token's
# `client_id` as the `tccred_*` credential id and a service token's as the
# `tck_*` key id, and neither reserved namespace may ever hold an OIDC client
# id -- so a human subject presenting one is incoherent and refused, while
# any other client id is simply the OAuth client the human authorized.
_BRANCH_CLAIMS: dict[str, dict[str, tuple[str, ...]]] = {
    "user": {
        "required": ("membership_id", "workspace_role"),
        "forbidden": ("credential_id", "runtime_id", "sponsor_id"),
    },
    "agent": {
        "required": (
            "membership_id",
            "client_id",
            "credential_id",
            "runtime_id",
            "sponsor_id",
        ),
        "forbidden": ("workspace_role",),
    },
    "service": {
        "required": ("client_id", "credential_id"),
        "forbidden": ("membership_id", "runtime_id", "sponsor_id", "workspace_role"),
    },
}

# The reserved machine-credential namespaces (agent `tccred_*`, service
# `tck_*`); a human token whose `client_id` sits in either is refused.
_CREDENTIAL_CLIENT_PREFIXES = ("tccred_", "tck_")

# `principal_type` -> the `sub` namespace that branch owns. A token whose
# discriminator and subject disagree is invalid however well-formed the rest
# of it is: the discriminator is an explicit label, never an independent grant
# of authority (§6.3).
_PRINCIPAL_TYPES = {"human": "user", "agent": "agent", "service": "service"}
_SUBJECT_PREFIXES = {"tcagt_": "agent", "tcusr_": "user", "tcsvc_": "service"}


def subject_type_for_subject(subject: str) -> str | None:
    """Return the principal family owned by a canonical subject namespace."""
    return next(
        (
            subject_type
            for prefix, subject_type in _SUBJECT_PREFIXES.items()
            if subject.startswith(prefix)
        ),
        None,
    )

# The two namespaces that predate the discriminator, for the rollout window
# below. `tcsvc_` is deliberately absent: Service is new, so a Service token
# without the claim is malformed rather than legacy.
_LEGACY_SUBJECT_PREFIXES = {"tcagt_": "agent", "tcusr_": "user"}

# During §15 step 3-5 a token minted before Hub emitted `principal_type` is
# still valid for up to its full lifetime, so a MISSING discriminator is
# derived from the subject namespace. A PRESENT mismatch never is. Step 5
# flips this to False once the maximum pre-change lifetime plus clock skew
# has passed, and the derivation goes with it.
_LEGACY_MISSING_PRINCIPAL_TYPE_ACCEPTED = True

# Asymmetric only. Listing HMAC algorithms alongside RSA is the classic
# confusion attack: an attacker signs with the *public* key as an HMAC secret.
_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384"]


class InvalidToken(Exception):
    """A bearer that is missing, malformed, unverifiable, or non-conforming.

    One exception for every cause on purpose. §7.1 answers all of them with
    the same `401` and a Bearer challenge; distinguishing "expired" from
    "wrong audience" in the type invites a handler to leak the difference.
    """


class UnknownKeyError(InvalidToken):
    """The token's `kid` is not in the cached JWKS.

    A subclass of :class:`InvalidToken` on purpose: it still answers 401 at
    the boundary. What it adds is an *operational* signal — the most common
    cause is Hub rotating its signing keys, which the boundary reacts to by
    re-fetching the JWKS once and retrying before committing to the 401. The
    boundary may therefore branch on this type, but no handler ever leaks
    "unknown key" as a distinct wire error.
    """

    def __init__(self, kid: str | None) -> None:
        super().__init__(
            f"token is signed by an unknown key (kid={kid!r})"
        )
        self.kid = kid


@dataclass(frozen=True)
class ProductTokenClaims:
    """The verified claims. Construction implies the signature checked out."""

    subject: str
    subject_type: str  # agent | user | service
    workspace_id: str
    drive_limits: DriveLimitsV1
    # None only on the Service branch: a Service Account has no workspace
    # membership, and every authorization path that reads this must find
    # nothing rather than a stale or invented value.
    membership_id: str | None
    token_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    unknown_scopes: frozenset[str] = field(default_factory=frozenset)
    credential_id: str | None = None
    runtime_id: str | None = None
    sponsor_id: str | None = None
    workspace_role: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def has_scope(self, scope: str) -> bool:
        """Set membership, never a substring test.

        `"content:read" in "content:read_secret"` is True and would be a
        privilege escalation; `in` over a set is not.
        """
        return scope in self.scopes


def _require_string(claims: dict[str, Any], name: str) -> str:
    """A claim that is present, a string, and non-empty.

    Truthiness is not enough and `str()` is worse: `str(12345)` produces a
    plausible-looking id out of a claim that was never the thing it is being
    read as, and an empty string satisfies "present" while naming nothing.
    """
    if name not in claims:
        raise InvalidToken(f"token is missing the required claim {name!r}")
    value = claims[name]
    if not isinstance(value, str) or not value:
        raise InvalidToken(f"token claim {name!r} is not a non-empty string")
    return value


def _prefix_for(branch: str) -> str:
    """The `sub` prefix a branch owns. One inverse of `_SUBJECT_PREFIXES`, so
    the two directions of the same mapping cannot drift."""
    return next(p for p, t in _SUBJECT_PREFIXES.items() if t == branch)


class ProductTokenVerifier:
    """Verifies bearers for one issuer/audience pair.

    Built once and reused: the JWKS is a document, not a connection, so
    verification is pure CPU and needs no I/O on the request path.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks: dict[str, Any],
        limits_claim_required: bool | None = None,
    ):
        from agentdrive.config import settings

        self._issuer = issuer
        self._audience = audience
        self._limits_claim_required = (
            settings.drive_limits_claim_required
            if limits_claim_required is None
            else limits_claim_required
        )
        # A JWKS is a document Hub publishes, and it must degrade per-key:
        # one entry PyJWT cannot model (a revoked algorithm, a malformed
        # `kty`, an empty key) must not abort construction and take down the
        # good signing keys sitting in the same document. Such a key is
        # unusable by definition, so skipping it costs nothing; a token
        # carrying its `kid` is rejected at lookup like any unknown key.
        keys = {}
        for key in jwks.get("keys", []):
            kid = key.get("kid")
            if not kid:
                continue
            try:
                keys[kid] = jwt.PyJWK(key)
            except Exception:
                continue
        self._keys = keys
        if not self._keys:
            raise ValueError("JWKS contains no usable keys")

    def verify(self, token: str) -> ProductTokenClaims:
        if not isinstance(token, str) or not token.strip():
            raise InvalidToken("no bearer token presented")

        try:
            header = jwt.get_unverified_header(token)
        except Exception as e:
            raise InvalidToken("token is malformed") from e

        key = self._keys.get(header.get("kid"))
        if key is None:
            # An unknown `kid` most often means Hub rotated and this deployment
            # is holding a stale JWKS. Still a 401 — accepting an unverifiable
            # token would be worse — but the distinct subtype lets the boundary
            # re-fetch the JWKS and retry once (rotation) before answering it.
            raise UnknownKeyError(header.get("kid"))

        try:
            claims = jwt.decode(
                token,
                key.key,
                algorithms=_ALGORITHMS,
                issuer=self._issuer,
                audience=self._audience,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except Exception as e:
            raise InvalidToken("token failed verification") from e

        # §6.4: ONE audience per token. PyJWT treats a list-valued `aud` as a
        # match when the configured audience appears anywhere in it, so a Hub
        # bug (or a compromised issuer) minting `aud: [drive, agentchat]`
        # would be accepted here and at AgentChat -- exactly the shared blast
        # radius §6.4 forbids by name. PyJWT has already confirmed we are in
        # the list; this confirms the list is only us.
        aud = claims.get("aud")
        single = aud == self._audience or aud == [self._audience]
        if not single:
            raise InvalidToken("token carries more than one audience")

        return self._to_claims(claims)

    def _to_claims(self, claims: dict[str, Any]) -> ProductTokenClaims:
        for name in _REQUIRED_CLAIMS:
            _require_string(claims, name)

        subject = claims["sub"]
        subject_type = self._branch(claims, subject)

        branch = _BRANCH_CLAIMS[subject_type]
        for name in branch["required"]:
            _require_string(claims, name)
        for name in branch["forbidden"]:
            # PRESENCE, not truthiness: `{"membership_id": null}` is a claim
            # the token CARRIES. Reading an explicit null as absent lets a
            # token through the very branch shape it violates, and it is what
            # Chat's verifier has always rejected — two products disagreeing
            # about what a well-formed token is means Hub can mint one that
            # only the more permissive half accepts.
            if name in claims:
                raise InvalidToken(
                    f"a {subject_type} token must not carry the claim {name!r}"
                )
        client_id = claims.get("client_id")
        if (
            subject_type == "user"
            and isinstance(client_id, str)
            and client_id.startswith(_CREDENTIAL_CLIENT_PREFIXES)
        ):
            raise InvalidToken("human token carries a credential-namespace client_id")

        # §6.3 carries the same value in `client_id` and `credential_id` in
        # v1. They are separate claims so a future authentication method may
        # split them; until one does, a disagreement is a malformed token
        # rather than a preview of that future. Chat enforces this, so this
        # must too.
        if (
            subject_type in ("agent", "service")
            and claims["client_id"] != claims["credential_id"]
        ):
            raise InvalidToken(
                "client_id and credential_id must name the same credential"
            )

        # Space-delimited per RFC 6749. Unknown values are kept rather than
        # dropped so an operator can see what a caller asked for, but they are
        # inert: `has_scope` only ever consults the recognized set.
        requested = {s for s in str(claims["scope"]).split() if s}
        raw_limits = claims.get("drive_limits")
        if raw_limits is None:
            if self._limits_claim_required:
                raise InvalidToken("token is missing a required product policy claim")
            drive_limits = default_drive_limits()
        else:
            try:
                drive_limits = parse_drive_limits_v1(raw_limits)
            except ValueError as exc:
                raise InvalidToken("token carries an invalid product policy claim") from exc
        return ProductTokenClaims(
            subject=subject,
            subject_type=subject_type,
            workspace_id=claims["workspace_id"],
            drive_limits=drive_limits,
            membership_id=claims.get("membership_id"),
            token_id=claims["jti"],
            scopes=frozenset(requested & V0_SCOPES),
            unknown_scopes=frozenset(requested - V0_SCOPES),
            credential_id=claims.get("credential_id"),
            runtime_id=claims.get("runtime_id"),
            sponsor_id=claims.get("sponsor_id"),
            workspace_role=claims.get("workspace_role"),
            raw=claims,
        )

    @staticmethod
    def _branch(claims: dict[str, Any], subject: str) -> str:
        """Which of the three principal branches this token is, or reject.

        The discriminator is `principal_type`, and it must AGREE with the
        `sub` namespace. Before Service Accounts the claim shape itself was
        the discriminator -- a `workspace_role` meant human, an agent-only
        claim meant agent -- and that rule cannot express Service, whose claim
        set overlaps Agent's on exactly the claims that used to identify
        Agent.
        """
        # PRESENCE again. The legacy window derives a MISSING discriminator;
        # a present null is a malformed one, and deriving from it would
        # resurrect exactly the shape-inference the discriminator replaced.
        if "principal_type" in claims:
            declared = claims["principal_type"]
            branch = _PRINCIPAL_TYPES.get(declared) if isinstance(declared, str) else None
            if branch is None:
                # Hub started issuing a form this build does not model.
                # Guessing which side of the claim split it belongs on would
                # be exactly the wrong instinct.
                raise InvalidToken("token carries an unrecognized principal_type")
            if not subject.startswith(_prefix_for(branch)):
                raise InvalidToken("token principal_type disagrees with its subject")
            return branch

        if not _LEGACY_MISSING_PRINCIPAL_TYPE_ACCEPTED:
            raise InvalidToken("token is missing the required claim 'principal_type'")
        derived = next(
            (t for p, t in _LEGACY_SUBJECT_PREFIXES.items() if subject.startswith(p)),
            None,
        )
        if derived is None:
            # Either an unrecognized namespace, or `tcsvc_` -- which has no
            # legacy form, so a Service token without the discriminator is
            # malformed rather than old.
            raise InvalidToken("token subject is not an agent or a user")
        return derived
