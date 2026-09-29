# `@tokencanopy/mcp-auth`

Reusable OAuth protected-resource boundary for Token Canopy MCP servers. It
builds RFC 9728 metadata and `WWW-Authenticate` challenges and validates
Hub-issued JWT access tokens with exact issuer, audience, expiry, signature,
and platform-claim checks, returning an authorization context carrying the
granted scope set.

The package does not acquire, refresh, store, or log tokens. MCP clients renew
through Hub and present the resulting audience-bound access token. Product
servers remain responsible for resource-local authorization after this edge
check succeeds.

## The MCP resource is not the product resource

Two constants, and they are not interchangeable (2026-08-28 security
remediation):

| Constant                      | Value                               | Audience of          |
| ----------------------------- | ----------------------------------- | -------------------- |
| `AGENTDRIVE_PRODUCT_RESOURCE` | `https://drive.tokencanopy.com`     | the public `/v0` API |
| `AGENTDRIVE_MCP_RESOURCE`     | `https://drive.tokencanopy.com/mcp` | the `/mcp` transport |

They used to be the same string, which made a token minted for a coding
agent's MCP session a fully valid `/v0` product token — the reviewed tool
surface was not a boundary at all. `AGENTDRIVE_PROTECTED_RESOURCE` now freezes
the **MCP** metadata contract, served at
`https://drive.tokencanopy.com/.well-known/oauth-protected-resource/mcp`. The
root document belongs to `/v0` and AgentDrive serves it.

## Scopes are a context, not a gate

`authenticateBearer` takes `recognizedScopes` — the resource's vocabulary —
and returns the presented scopes intersected with it. It refuses with `403
insufficient_scope` only when that intersection is EMPTY.

It used to require every scope in the bundle. Hub's own consent screen offers
a read-only choice, and a read-only grant carries a strict subset, so the
transport answered `insufficient_scope` at `initialize` and the safest consent
option was the one that did not work. Per-tool authorization decides what a
given token can do; `requiredScopes` survives as an optional hard bundle for a
future resource that genuinely has no read-only mode.

A scope value outside RFC 6749's `scope-token` grammar is treated as a
malformed TOKEN (`invalid_token`), not as an unprivileged one — silently
discarding it would hide a quoting or injection bug upstream.

If the remote JWKS verifier is temporarily unavailable, the boundary returns
`503` with `Retry-After: 5`; it never treats a key-distribution outage as a
valid request.
