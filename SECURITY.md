# Security Policy

AgentDrive stores files for AI agents and serves some of them to the public
web. A vulnerability here can expose one workspace's files to another, let an
anonymous visitor read a private artifact, or let artifact content run script
on a trusted origin. Please report issues privately rather than in public
GitHub issues.

## Reporting a vulnerability

Email **security@tokencanopy.com** with:

- a description of the issue
- steps to reproduce, or a proof of concept
- the version or commit you observed it on (the git SHA or release tag)
- any mitigation you suggest

You can also use GitHub's
[private vulnerability reporting](https://github.com/tokencanopy/agentdrive/security/advisories/new)
to file a draft advisory against this repository.

We aim to acknowledge receipt within 3 business days, give a substantive
response within 7 business days, and ship a fix for confirmed high-severity
issues within 30 days, faster when active exploitation is plausible. We credit
reporters in the advisory unless you ask to remain anonymous. We don't run a
paid bounty program.

## Supported versions

| Version | Status |
|---------|--------|
| `0.x` (current; the `/v0` API is beta) | ✅ The latest `0.x` release receives security fixes |

Fixes ship in the latest release; we don't backport to older `0.x` tags.
Self-hosters should upgrade promptly when an advisory is published.

## Scope

In scope:

- Authentication bypass: reaching a `/v0` or `/mcp` route without a valid
  credential, or acting as another principal
- Authorization flaws: reading or changing a drive, folder or artifact the
  caller holds no grant for, or crossing workspaces
- API key handling: a revoked or expired key being accepted, a key's scopes
  being widened, or key material leaking
- Share links: reaching a target through a revoked or expired link, or
  learning whether an id exists from a refusal
- Rendering isolation: artifact content executing script on the API or share
  origin, escaping the sandboxed renderer, or bypassing the HTML sanitizer
- Server-side request forgery, path traversal in the filesystem store,
  injection of any kind
- The MCP transport: tools exposed beyond what a credential's scopes allow

Out of scope:

- Deployments that expose the API beyond loopback without TLS, or that reuse
  the example values from `.env.example`
- Missing features, such as rate limits you would like to see (open an issue)
- Vulnerabilities in dependencies with no reachable path through AgentDrive
  (please report those upstream)
- Issues only reproducible against the hosted service at
  `drive.tokencanopy.com` rather than this code; those go to the same
  address

## Disclosure

Once a fix is released we publish a GitHub Security Advisory describing the
issue, affected versions and remediation. Please hold public disclosure until
the advisory is published or 90 days after first contact, whichever is
sooner.
