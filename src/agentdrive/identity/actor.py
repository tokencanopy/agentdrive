"""The authenticated actor.

`V0ActorContext` is the only thing below the auth boundary that says who is
acting. It is built here, from verified claims, and nowhere else — §7.1: "the
actor is derived from the verified token; caller-controlled actor headers are
removed." A handler that could construct one from a request body would
reintroduce exactly the header the contract deletes.

It carries provenance as well as identity. §6.7 keeps subject, credential,
runtime, workspace and token id distinct rather than flattening them to one
"who", because artifact provenance and the change feed need to tell an agent
apart from the credential it authenticated with — and because RFC 8693
delegation (companion §7.1) is deferred, not refused. When an `act` chain
arrives, it extends this shape instead of redefining it.

What it deliberately does not carry is authorization. Scope is half of
`scope ∩ local-grant` (§7.1); the local half is resolved per-resource in
Layer 3. `can(...)` answers "does the token permit this at all", never "may
this actor touch that artifact".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from agentdrive.core.usage.models import DriveLimitsV1
from agentdrive.core.usage.policy import default_drive_limits

from .product_token import ProductTokenClaims

# The workspace roles that carry administrator authority over the whole
# workspace. Hub mints `workspace_role` as "owner" | "admin" | "member"
# verbatim; owner and admin are equivalent for every AgentDrive decision
# (the workspace-admin overlay and break-glass recovery). "member" carries
# no ambient authority — members are grant-only.
WORKSPACE_ADMIN_ROLES = frozenset({"owner", "admin"})


@dataclass(frozen=True)
class V0ActorContext:
    """Who is acting, and what their token permits.

    Frozen: an actor is established once per request, at the boundary. A
    mutable one invites a handler to widen its own authority mid-request.
    """

    subject: str
    subject_type: str  # agent | user | service
    workspace_id: str
    # `None` only on the Service branch. A Service Account belongs to a
    # workspace without being a MEMBER of one (service account design §7.1),
    # so there is no membership id to carry — and every path that reads one
    # must find nothing rather than a stale or invented value.
    membership_id: str | None
    token_id: str
    scopes: frozenset[str]
    drive_limits: DriveLimitsV1 = field(default_factory=default_drive_limits)
    credential_id: str | None = None
    runtime_id: str | None = None
    sponsor_id: str | None = None
    workspace_role: str | None = None

    @classmethod
    def from_claims(cls, claims: ProductTokenClaims) -> V0ActorContext:
        return cls(
            subject=claims.subject,
            subject_type=claims.subject_type,
            workspace_id=claims.workspace_id,
            drive_limits=claims.drive_limits,
            membership_id=claims.membership_id,
            token_id=claims.token_id,
            scopes=claims.scopes,
            credential_id=claims.credential_id,
            runtime_id=claims.runtime_id,
            sponsor_id=claims.sponsor_id,
            workspace_role=claims.workspace_role,
        )

    @classmethod
    def from_local_key(
        cls,
        *,
        subject: str,
        principal_type: str,
        workspace_id: str,
        scopes: frozenset[str],
        workspace_role: str | None,
        sponsor_id: str | None,
        key_id: str,
    ) -> V0ActorContext:
        """The actor a self-hosted install's API key authenticates.

        `AUTH_MODE=local` only (open-source design §4.2). It exists so the
        key path produces the SAME shape `from_claims` does rather than
        faking a `ProductTokenClaims` to get there: a fake claims object
        would have to satisfy the verifier's branch rules for a token that
        was never signed, which is exactly the kind of near-miss that later
        reads as a real Hub token.

        The provenance ids Hub mints — membership, credential, runtime —
        have no Hub counterpart here, so they are DERIVED and namespaced
        `local_`/`tccred_local_`/`tcrun_local_` rather than left empty: a
        change-feed row or an artifact minted by a local key must not be
        indistinguishable from one minted by a Hub credential. The
        credential and runtime follow the KEY, not the subject, because a
        key IS the credential — two keys for one agent are two credentials,
        as they would be at Hub.
        """
        if principal_type not in ("agent", "user"):
            raise ValueError("principal_type must be 'agent' or 'user'")
        local_credential = key_id.split("_", 1)[-1]
        return cls(
            subject=subject,
            subject_type=principal_type,
            workspace_id=workspace_id,
            membership_id="local_"
            + hashlib.sha256(f"{workspace_id}:{subject}".encode()).hexdigest()[:16],
            token_id=key_id,
            scopes=scopes,
            credential_id=(
                f"tccred_local_{local_credential}" if principal_type == "agent" else None
            ),
            runtime_id=f"tcrun_local_{local_credential}" if principal_type == "agent" else None,
            sponsor_id=sponsor_id if principal_type == "agent" else None,
            workspace_role=workspace_role if principal_type == "user" else None,
        )

    @property
    def is_agent(self) -> bool:
        return self.subject_type == "agent"

    @property
    def is_service(self) -> bool:
        """A workspace-owned backend integration (service account design §7.1).

        It reaches a resource only through an explicit `service` grant or a
        drive it created: `_principal_matches` excludes it from `workspace`
        grants because it has no workspace membership to be covered by.
        """
        return self.subject_type == "service"

    @property
    def is_workspace_admin(self) -> bool:
        """Token-verified workspace administrator: role `owner` OR `admin`.

        Drives the workspace-admin manager overlay (contract §8) and the
        residual break-glass recovery path. Hub mints `workspace_role` as
        "owner" | "admin" | "member" verbatim — an owner outranks an admin
        at Hub, so any decision that admits an admin must admit an owner
        (testing `== "admin"` alone locked owners out of break-glass).

        Reads `workspace_role`, which only a human token carries and which
        `product_token` refuses to accept on an agent's -- so an agent cannot
        reach an admin-gated path by presenting a claim it forged, and this
        property cannot be true for one.
        """
        return (
            self.subject_type == "user"
            and self.workspace_role in WORKSPACE_ADMIN_ROLES
        )

    def can(self, scope: str) -> bool:
        """Whether the TOKEN permits `scope`. Not an authorization decision.

        §7.1: "scope checks occur before local capabilities; both are
        required." This is the first half. A caller that stops here has
        authorized nothing -- it has only established that the token does not
        forbid the operation outright.
        """
        return scope in self.scopes

    def change_actor(self) -> dict[str, str]:
        """The `actor` block on a change-feed row (§6.7).

        Subject and type only. Workspace and `public` grant principals never
        act, and a future system-initiated event uses an explicit `system`
        actor with its own event type rather than borrowing a human's.
        """
        return {"type": self.subject_type, "id": self.subject}
