# Contributing to AgentDrive

Thanks for considering a contribution. AgentDrive is a Python service (FastAPI,
PostgreSQL, and an object store) with a TypeScript MCP transport and a React
component library. This page gets you from a clone to a green test run and a
pull request.

## Fork and pull request workflow

This is a public repository, so you contribute from a fork: fork
`tokencanopy/agentdrive` on GitHub, clone your fork, push a branch there, and
open a pull request against `main`. Keep a pull request to one change, and say
in its description what behaviour changes and how you verified it.

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Docker with Compose v2 (PostgreSQL and the storage emulator for tests)
- Node.js 22 (the MCP transport under `mcp/` and the component library under
  `design-system/`)

## First run

```bash
uv sync
docker compose up -d                     # Postgres + the GCS emulator
cp .env.example .env
python -c 'import secrets; print("SESSION_SECRET=" + secrets.token_urlsafe(48))' >> .env
uv run python -m agentdrive.scripts.apply_schema
uv run uvicorn agentdrive.app:app --reload --port 8000
```

To run the whole product the way a self-hoster does — one container with the
API, the MCP transport and a filesystem store — follow **Self-host in ten
minutes** in `README.md`.

## Running the checks

```bash
uv run ruff check                                   # lint; the repository does not use a formatter
uv run pytest -n auto --ignore=tests/browser         # needs `docker compose up -d`
STORAGE_BACKEND=fs STORAGE_FS_ROOT=/tmp/agentdrive-fs uv run pytest -n auto --ignore=tests/browser
python scripts/check_public_snapshot.py             # the publish gate
scripts/selfhost-smoke.sh --down                    # the quickstart, end to end
```

The MCP transport has its own gates:

```bash
npm ci
npm test --workspace @tokencanopy/agentdrive-mcp
npm run lint --workspace @tokencanopy/agentdrive-mcp
```

CI runs all of these on every pull request.

## Changing the API

The `/v0` API is a contract, not an implementation detail. Its operations are
declared in `src/agentdrive/api/v0-operations.json`, the served OpenAPI
document is pinned in `tests/openapi.golden.json`, and `tests/conformance/`
proves the two agree. A change to any route must update the golden file in the
same pull request (`uv run python -m agentdrive.scripts.dump_openapi`), and the
compatibility check will tell you whether the change breaks existing clients.
The governing document is `docs/agentdrive-v0-api-contract-reset.md`.

## Database changes

Add a new numbered file under `migrations/` and fold its end state into
`schema.sql` in the same pull request. Never edit a migration that has shipped;
the runner refuses a changed checksum.

## Styles

The stylesheet the server renders with is generated from
`design-system/src/styles/agentdrive.css` by `make css`. Edit the source, never
the generated copy under `src/agentdrive/static/`. The unprefixed class names
(`.btn`, `.card`, `.kind`) are a public API of the published component library,
so renaming one is a breaking change.

## Never commit private data

Everything in this repository is public, including history. Use synthetic
values (`user@example.com`, `*.example.test`, fictional ids) in code, tests,
fixtures and docs. `scripts/check_public_snapshot.py` refuses common leaks in
CI, but it is a backstop, not a review.

Images are the one thing no pattern can read, so every image in the tree is
listed with its SHA-256 in `scripts/reviewed-images.txt`, and CI refuses an
image that is missing there or whose bytes changed. Adding or replacing one
means looking at it and updating that line in the same pull request.

## Releases

Maintainers cut two independent release trains from tags. `vX.Y.Z` builds
and publishes the container image to `ghcr.io/tokencanopy/agentdrive` and
runs the self-host quickstart against it. `design-system-vX.Y.Z` publishes
`@tokencanopy/agentdrive-design-system`, whose `package.json` version must
match the tag.

## Security issues

Please don't open a public issue for a vulnerability. See `SECURITY.md`.

## License

By contributing you agree that your contributions are licensed under the
Apache License 2.0, the license of this repository.
