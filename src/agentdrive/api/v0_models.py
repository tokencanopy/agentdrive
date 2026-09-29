"""Shared /v0 response models for runtime response validation (response_model).

Each model mirrors the exact wire shape produced by the corresponding
`core/v0_*.payload`/`change_document` functions. FastAPI validates the route's
returned dict against `response_model` at runtime, so a payload that drifts
from the contract fails loudly (500 ResponseValidationError) instead of
shipping a malformed response. The OpenAPI `components.schemas` are generated
from these models too, so the served spec and the runtime guard stay in lockstep.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Output timestamps are RFC 3339 strings (the payload functions serialize
# datetimes with ``isoformat()``). Mark the format so the served OpenAPI
# advertises ``format: date-time`` and SDK generators emit a datetime type.
RFC3339 = Annotated[str, Field(json_schema_extra={"format": "date-time"})]


class DriveOut(BaseModel):
    id: str = Field(pattern=r"^drv_[a-f0-9]{16}$")
    workspace_id: str
    created_by: str | None
    name: str
    metadata: dict[str, Any]
    revision: str = Field(pattern=r"^rev_[a-f0-9]{16}$")
    root_folder_id: str
    storage_bytes: int
    retrieval_bytes: int
    created_at: RFC3339
    updated_at: RFC3339
    deleted_at: RFC3339 | None
    state: Literal["active", "deleted"]


class DriveListOut(BaseModel):
    items: list[DriveOut]
    next_cursor: str | None


class UsageMeterOut(BaseModel):
    scope: Literal["workspace", "drive", "principal", "share"]
    used: int = Field(ge=0)
    reserved: int = Field(ge=0)
    limit: int = Field(gt=0)
    remaining: int = Field(ge=0)
    reset_at: RFC3339 | None
    model_config = ConfigDict(extra="forbid")


class UsageMetersOut(BaseModel):
    drive_storage: UsageMeterOut
    workspace_storage: UsageMeterOut
    workspace_download_day: UsageMeterOut
    workspace_download_month: UsageMeterOut
    model_config = ConfigDict(extra="forbid")


class EffectiveLimitsOut(BaseModel):
    max_file_bytes: int = Field(gt=0)
    max_inline_file_bytes: int = Field(gt=0)
    share_default_ttl_seconds: int = Field(gt=0)
    share_max_ttl_seconds: int = Field(gt=0)
    model_config = ConfigDict(extra="forbid")


class DriveUsageOut(BaseModel):
    storage_bytes: int = Field(ge=0)
    retrieval_bytes: int = Field(ge=0)
    meters: UsageMetersOut
    effective_limits: EffectiveLimitsOut
    model_config = ConfigDict(extra="forbid")


class FolderOut(BaseModel):
    id: str = Field(pattern=r"^fld_[a-f0-9]{16}$")
    drive_id: str = Field(pattern=r"^drv_[a-f0-9]{16}$")
    parent_id: str | None
    name: str | None
    metadata: dict[str, Any]
    revision: str = Field(pattern=r"^rev_[a-f0-9]{16}$")
    state: Literal["active", "deleted"]
    created_at: RFC3339
    updated_at: RFC3339
    deleted_at: RFC3339 | None


class FolderListOut(BaseModel):
    items: list[FolderOut]
    next_cursor: str | None


class FolderCascadeOut(BaseModel):
    folder: FolderOut
    cascade: dict[str, int]


class ArtifactOut(BaseModel):
    id: str = Field(pattern=r"^art_[a-f0-9]{16}$")
    drive_id: str = Field(pattern=r"^drv_[a-f0-9]{16}$")
    parent_id: str = Field(pattern=r"^fld_[a-f0-9]{16}$")
    name: str
    content_type: str | None
    content_preview: str | None
    labels: list[str]
    metadata: dict[str, Any]
    head_version_id: str | None
    revision: str = Field(pattern=r"^rev_[a-f0-9]{16}$")
    state: Literal["active", "deleted"]
    created_at: RFC3339
    updated_at: RFC3339
    deleted_at: RFC3339 | None
    effective_visibility: Literal["public", "shared", "private"] = Field(
        description=(
            "Server-computed exposure summary, resolved over the artifact's "
            "live grants, its whole folder ancestry, and the drive. "
            "'public' when any live grant has "
            "principal_type 'public'; otherwise 'shared' when a live grant "
            "names a principal other than the drive's creator; otherwise "
            "'private'. Describes exposure, NOT the caller's own access."
        )
    )


class ArtifactListOut(BaseModel):
    items: list[ArtifactOut]
    next_cursor: str | None


class FolderEntryOut(BaseModel):
    """Compact D13 folder member returned by the unified namespace list."""

    type: Literal["folder"]
    id: str = Field(pattern=r"^fld_[a-f0-9]{16}$")
    name: str
    revision: str = Field(pattern=r"^rev_[a-f0-9]{16}$")
    updated_at: RFC3339
    state: Literal["active", "deleted"]
    deleted_at: RFC3339 | None


class ArtifactEntryOut(BaseModel):
    """Compact D13 artifact member; content and rich metadata stay excluded."""

    type: Literal["artifact"]
    id: str = Field(pattern=r"^art_[a-f0-9]{16}$")
    name: str
    revision: str = Field(pattern=r"^rev_[a-f0-9]{16}$")
    updated_at: RFC3339
    state: Literal["active", "deleted"]
    deleted_at: RFC3339 | None
    size_bytes: int = Field(ge=0)
    content_type: str | None
    head_version_id: str | None


EntryOut = Annotated[FolderEntryOut | ArtifactEntryOut, Field(discriminator="type")]


class EntryListOut(BaseModel):
    entries: list[EntryOut]
    next_cursor: str | None


class LookupOut(BaseModel):
    type: Literal["folder", "artifact"]
    id: str
    parent_id: str | None
    revision: str = Field(pattern=r"^rev_[a-f0-9]{16}$")


class VersionOut(BaseModel):
    id: str = Field(pattern=r"^ver_[a-f0-9]{16}$")
    artifact_id: str = Field(pattern=r"^art_[a-f0-9]{16}$")
    version_number: int = Field(ge=1)
    parent_version_id: str | None
    content_type: str
    size_bytes: int = Field(ge=0)
    hash: str
    created_by: str | None
    created_at: RFC3339
    # What produced this version, when anything did. A sheet edit session
    # sets both; a plain upload sets neither. The id is opaque and outlives
    # the session it names, which is swept a day after it terminates — so
    # the message, not the id, is the durable half.
    origin_session_id: str | None = None
    origin_message: str | None = None


class VersionListOut(BaseModel):
    items: list[VersionOut]
    next_cursor: str | None


class VersionCreatedOut(VersionOut):
    """The append/restore response — a version plus the artifact's new
    revision, which the version-creating 201 rotates."""

    artifact_revision: str = Field(
        pattern=r"^rev_[a-f0-9]{16}$",
        description=(
            "The artifact's revision after this version became head — the "
            "If-Match value for the next mutation."
        ),
    )


class GrantOut(BaseModel):
    id: str = Field(pattern=r"^grn_[a-f0-9]{16}$")
    drive_id: str = Field(pattern=r"^drv_[a-f0-9]{16}$")
    resource_type: Literal["drive", "folder", "artifact"]
    resource_id: str
    principal_type: Literal["agent", "user", "service", "workspace", "public"]
    principal_id: str | None
    role: Literal["viewer", "editor", "manager"]
    revision: str = Field(pattern=r"^rev_[a-f0-9]{16}$")
    state: Literal["active", "revoked", "expired"]
    expires_at: RFC3339 | None
    revoked_at: RFC3339 | None
    created_at: RFC3339


class GrantListOut(BaseModel):
    items: list[GrantOut]
    next_cursor: str | None


class ShareOut(BaseModel):
    id: str = Field(pattern=r"^shr_[a-f0-9]{16}$")
    drive_id: str = Field(pattern=r"^drv_[a-f0-9]{16}$")
    resource_type: Literal["artifact", "artifact_version", "folder"]
    resource_id: str
    created_by: str | None
    revision: str = Field(pattern=r"^rev_[a-f0-9]{16}$")
    state: Literal["active", "revoked", "expired"]
    expires_at: RFC3339 | None
    revoked_at: RFC3339 | None
    created_at: RFC3339
    rotated_at: RFC3339 | None


class ShareCreateOut(ShareOut):
    """The create/rotate response — the ONLY response carrying the plaintext
    secret."""

    secret: str | None = Field(
        default=None,
        description=(
            "Plaintext share secret. Present only on first execution of a "
            "create or rotate; null on idempotent replay — rotate to obtain "
            "a new secret."
        ),
    )
    url: str | None = Field(
        default=None,
        description=(
            "The redemption URL for this share, on the public share origin. "
            "Present exactly when `secret` is — the URL EMBEDS the secret, so "
            "it is a credential and is never returned by list or get, and "
            "never stored in the idempotency ledger. A caller cannot compose "
            "this itself: the origin is deployment configuration, not "
            "something a client can know."
        ),
    )


class ShareListOut(BaseModel):
    items: list[ShareOut]
    next_cursor: str | None


class ViewerSessionOut(BaseModel):
    id: str = Field(pattern=r"^vwr_[a-f0-9]{16}$")
    drive_id: str = Field(pattern=r"^drv_[a-f0-9]{16}$")
    artifact_id: str = Field(pattern=r"^art_[a-f0-9]{16}$")
    version_id: str = Field(pattern=r"^ver_[a-f0-9]{16}$")
    expires_at: RFC3339
    created_at: RFC3339


class ViewerSessionCreateOut(ViewerSessionOut):
    """The mint response — the ONLY response carrying the plaintext viewer
    credential. The credential authorizes the isolated viewer host's
    `/view/doc` and `/view/content` for this one pinned version, via an
    Authorization header only — it must never be placed in a URL, cookie,
    or persistent storage."""

    expires_in: int = Field(
        description="Seconds until the session expires, from mint time."
    )
    credential: str | None = Field(
        default=None,
        description=(
            "Plaintext viewer credential. Present only on first execution "
            "of a mint; null on idempotent replay — mint a new session with "
            "a fresh Idempotency-Key to obtain a credential."
        ),
    )


class UploadChecksumOut(BaseModel):
    algorithm: Literal["crc32c"]
    value: str = Field(pattern=r"^[A-Za-z0-9+/]{6}==$")


class UploadContentOut(BaseModel):
    size_bytes: int = Field(ge=0)
    media_type: str
    checksum: UploadChecksumOut


class UploadTargetArtifactOut(BaseModel):
    kind: Literal["artifact"]
    parent_folder_id: str = Field(pattern=r"^fld_[a-f0-9]{16}$")
    name: str


class UploadTargetVersionOut(BaseModel):
    kind: Literal["version"]
    artifact_id: str = Field(pattern=r"^art_[a-f0-9]{16}$")


class UploadResultOut(BaseModel):
    kind: Literal["artifact", "version"]
    artifact_id: str = Field(pattern=r"^art_[a-f0-9]{16}$")
    version_id: str = Field(pattern=r"^ver_[a-f0-9]{16}$")
    revision: str = Field(pattern=r"^rev_[a-f0-9]{16}$")


class UploadFailureOut(BaseModel):
    code: str


class UploadCleanupOut(BaseModel):
    state: Literal["none", "pending", "quarantined", "deleting", "cleaned", "blocked"]


class UploadInitiationOut(BaseModel):
    """The V4-signed XML resumable initiation target (§5.6 as amended
    2026-08-20). Secret material: the URL plus the exact signed header
    values. The client POSTs it with EXACTLY ``required_headers`` and an
    empty body; the 201 response's ``Location`` header is the resumable
    session URI. CLOSED schema on purpose — a generated client must not
    learn a broader security-sensitive target contract than the wire
    carries."""

    url: str
    method: Literal["POST"]
    required_headers: dict[str, str]
    expires_at: RFC3339
    model_config = ConfigDict(extra="forbid")


class UploadChunksOut(BaseModel):
    """How to stream chunks against the session URI the initiation's
    ``Location`` disclosed: the unchanged ``gcs-xml-resumable`` protocol
    (PUT + ``Content-Range``, 308/``Range`` resume)."""

    method: Literal["PUT"]
    required_headers: dict[str, str]
    model_config = ConfigDict(extra="forbid")


class UploadTransferOut(BaseModel):
    """The one-time external transfer disclosure (B3 §5.2, amended
    2026-08-20 to the two-step browser-initiated shape). Secret material:
    present ONLY in the initial successful begin response, never in
    status, replay, or any stored record."""

    chunk_protocol: Literal["gcs-xml-resumable"]
    initiation: UploadInitiationOut
    chunks: UploadChunksOut


class UploadOut(BaseModel):
    id: str = Field(pattern=r"^upld_[a-f0-9]{16}$")
    drive_id: str = Field(pattern=r"^drv_[a-f0-9]{16}$")
    state: Literal[
        "preparing", "active", "completing", "cancelling",
        "completed", "cancelled", "expired", "rejected",
    ]
    target: UploadTargetArtifactOut | UploadTargetVersionOut = Field(
        discriminator="kind"
    )
    content: UploadContentOut
    expires_at: RFC3339
    target_disclosed: bool
    restart_required: bool
    result: UploadResultOut | None
    failure: UploadFailureOut | None
    cleanup: UploadCleanupOut


class UploadSessionOut(BaseModel):
    """The non-secret session representation (status, replay, cancel,
    complete). Deliberately has NO transfer field — it is structurally
    incapable of carrying the bearer target."""

    upload: UploadOut


class UploadWithTransferOut(UploadOut):
    transfer: UploadTransferOut


class UploadBeginOut(BaseModel):
    """The one 201 begin response — the only shape carrying ``transfer``."""

    upload: UploadWithTransferOut


class DownloadTargetOut(BaseModel):
    """The one signed, generation-pinned direct GET target (B3 §5.7).
    Secret material: present only in the fresh mint response, never in any
    stored record, replay, or log. CLOSED schema on purpose: Packet 5 and
    generated clients must not learn a broader security-sensitive target
    contract than the wire actually carries."""

    url: str
    method: Literal["GET"]
    # Exactly the empty object (§5.7): no signed request header is ever
    # required, and a generated client must not infer that arbitrary
    # required capability headers can appear.
    required_headers: dict[str, str] = Field(
        json_schema_extra={"maxProperties": 0, "additionalProperties": False},
    )
    content_disposition: str
    model_config = ConfigDict(extra="forbid")


class DownloadOut(BaseModel):
    artifact_id: str = Field(pattern=r"^art_[a-f0-9]{16}$")
    version_id: str = Field(pattern=r"^ver_[a-f0-9]{16}$")
    expires_at: RFC3339
    target: DownloadTargetOut
    model_config = ConfigDict(extra="forbid")


class DownloadCapabilityOut(BaseModel):
    """The §5.7 mint response — a capability computation, not a stored
    resource: 200 only, freshly signed on every call."""

    download: DownloadOut
    model_config = ConfigDict(extra="forbid")


class ChangeActorOut(BaseModel):
    # `service` joins the vocabulary; the SHAPE is frozen (§6.7). A reader
    # that cannot render an actor kind drops the whole page, not one row.
    type: Literal["agent", "user", "service", "system"]
    id: str | None


class ChangeResourceOut(BaseModel):
    type: Literal["drive", "folder", "artifact"]
    id: str


class ChangeOut(BaseModel):
    id: str = Field(pattern=r"^chg_[a-f0-9]{16}$")
    change_set_id: str
    type: str
    drive_id: str = Field(pattern=r"^drv_[a-f0-9]{16}$")
    actor: ChangeActorOut
    resource: ChangeResourceOut
    previous_revision: str | None
    revision: str | None
    occurred_at: RFC3339
    data: dict[str, Any]


class ChangePageOut(BaseModel):
    items: list[ChangeOut]
    next_cursor: str
    has_more: bool


class SearchHitOut(BaseModel):
    id: str = Field(pattern=r"^art_[a-f0-9]{16}$")
    drive_id: str = Field(pattern=r"^drv_[a-f0-9]{16}$")
    parent_id: str | None
    name: str
    version_id: str | None
    rank: float
    snippet: str = Field(
        description=(
            "HTML-safe highlighted excerpt. The ONLY markup it may contain is "
            "the server's own <mark>...</mark> highlight pair; artifact "
            "content is entity-escaped, so this may be rendered as HTML."
        )
    )
    content_type: str | None
    updated_at: RFC3339


class SearchPageOut(BaseModel):
    items: list[SearchHitOut]
    next_cursor: str | None
