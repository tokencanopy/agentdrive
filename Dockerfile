# syntax=docker/dockerfile:1.7
#
# AgentDrive (Loft) production container.
#
# Two stages:
#   1. builder — uv installs the locked dependency set into a venv
#   2. runtime — slim Python copies the venv + source, runs uvicorn
#
# Single worker on purpose:
#   - the indexer worker is a singleton per-instance (FOR UPDATE SKIP LOCKED
#     handles cross-instance, but per-instance must be 1 until B1 is fixed)
#   - asyncpg pool sized for one process; multi-worker would multiply
#     connection count without coordination
#
# Cloud Run injects $PORT (default 8080). We bind there.

ARG PYTHON_VERSION=3.12-slim-bookworm
ARG UV_VERSION=0.5.4
ARG NODE_VERSION=22-bookworm-slim

# Pull uv from its official image. Naming this as a stage lets us COPY --from
# below without the ARG-in-image-ref limitation.
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# ----- hosted MCP sidecar builder -----
# Builds the TypeScript MCP from its SOURCE OF TRUTH in this app:
# `mcp/auth` and `mcp/server`. The build context is the AgentDrive root, so
# every path below is relative to it and this image builds from a checkout of
# AgentDrive alone (open-source design §4.4).
#
# It has moved twice. The source first lived in another repository and was
# vendored here as a byte-verified snapshot with a sync script, a manifest and
# a provenance document; the port to the monorepo replaced that with a
# repo-root context reading `packages/{mcp-auth,agentdrive-mcp}`; and this
# brings it inside the app. There is still exactly one copy.
#
# The BUILD MANIFESTS below are deliberately NOT the packages' own
# `package.json` files, and this is the one duplication that survives. The real
# manifests carry eslint, prettier, vitest and typescript-eslint plus
# `prebuild`/`pretest` hooks that assume the root npm workspace. Using them
# here would drag the entire lint and test toolchain into the image builder and
# break on hooks resolving a workspace that does not exist inside `/mcp`. These
# are build-only: TypeScript and the runtime dependencies, nothing else.
#
# The manifests can therefore drift from the packages' real dependencies. Two
# things catch it: `npm ci` fails loudly when a manifest and the lockfile
# disagree, and the build fails when an import has no dependency behind it.
# `tests/test_mcp_deployment_contract.py` pins the shape.
FROM node:${NODE_VERSION} AS mcp-builder
WORKDIR /mcp
COPY deploy/mcp/package.json deploy/mcp/package-lock.json ./
COPY deploy/mcp/auth/package.json deploy/mcp/auth/tsconfig.json ./auth/
COPY deploy/mcp/server/package.json deploy/mcp/server/tsconfig.json ./server/
COPY mcp/auth/src ./auth/src
COPY mcp/server/src ./server/src
RUN npm ci --ignore-scripts \
 && npm run build \
 && npm prune --omit=dev

# ----- builder -----
FROM python:${PYTHON_VERSION} AS builder
COPY --from=uv /uv /uvx /usr/local/bin/

WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# Sync deps first (cache-friendly: changes to source don't bust the dep layer).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Now copy source + finalize the venv with the project itself.
COPY src ./src
COPY README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ----- runtime -----
FROM python:${PYTHON_VERSION} AS runtime

# The runtime image is Python-based, but it also executes the Node MCP binary
# copied from the builder above. Keep the native Node dependencies explicit so
# a future Python base-image change cannot make the sidecar fail at startup.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libatomic1 libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

# Non-root user. uvicorn doesn't need root; Cloud Run runs as PID 1 anyway,
# but local `docker run` benefits from this.
RUN groupadd --system --gid 1001 app \
 && useradd  --system --uid 1001 --gid app --create-home --home-dir /home/app app

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src   /app/src
# The MCP sidecar is started only when MCP_PROXY_URL is configured. Keeping
# the built sidecar in the same immutable image means staging and production
# promote one digest; Terraform controls whether production starts it.
COPY --from=mcp-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=mcp-builder --chown=app:app /mcp /opt/agentdrive-mcp
# The public SDK-design page renders its single canonical Markdown source at
# import time. Keep this one reviewed document in the runtime image without
# broadly shipping the rest of docs/.
COPY --chown=app:app docs/agentdriveSDK.md /app/docs/agentdriveSDK.md
# Schema + migrations for the migrate job (run by Cloud Run Job before
# each deploy). Without the migrations/ COPY the runner silently sees an
# empty migration set in prod and only the baseline applies.
COPY --chown=app:app schema.sql /app/schema.sql
COPY --chown=app:app migrations /app/migrations
# SKILL.md is read at import time by web/settings_routes.py (via the
# parents[3] walk to repo root). Ship it so the container's layout
# matches the dev checkout.
COPY --chown=app:app skills /app/skills
# NOTE deliberately NO `COPY api/`: the v0 operation manifest is PACKAGE
# DATA (src/agentdrive/api/v0-operations.json, loaded via
# importlib.resources), so it rides with `COPY src` above. It used to live
# at the repo root outside the package and was never copied — every
# deployed container 500'd its first /openapi.json and then served an
# UNENRICHED spec off FastAPI's internally-cached raw schema. Data files a
# package reads live INSIDE the package so forgetting to ship them is
# unrepresentable. api/ now holds only CI-side files (oasdiff config).

# The self-host compose file mounts a named volume at /data for the
# filesystem object store (`STORAGE_BACKEND=fs`, `STORAGE_FS_ROOT=/data`). A
# named volume takes its ownership from the image path it covers when that
# path exists, so the directory is created here and owned by `app`; without
# it Docker would mount the volume root-owned and the store's boot probe
# would refuse to start with a permission error.
RUN mkdir -p /data && chown app:app /data
USER app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Build-time assertion: the image can actually load the operation manifest
# (and any future file its import chain reads). Fails the BUILD, not the
# first boot in production.
RUN python -c "from agentdrive.api.v0_manifest import catalog_operations; assert len(catalog_operations) == 59, len(catalog_operations)"

EXPOSE 8080

# The supervisor runs the API and (when configured) the localhost Node MCP
# sidecar as sibling processes, forwarding signals and failing the revision
# if either child exits. It also owns the same bounded forwarded-IP default
# that the old direct uvicorn command used.
CMD ["python", "-m", "agentdrive.process_supervisor"]
