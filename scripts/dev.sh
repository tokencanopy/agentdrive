#!/usr/bin/env bash
#
# One command to bring up the full local stack:
#   • Postgres + GCS emulator   (docker compose, detached)
#   • schema + migrations       (idempotent)
#   • the app                   (uvicorn --reload)
#
# Usage:   ./scripts/dev.sh        (or `make dev`)
#          PORT=8765 ./scripts/dev.sh
#
# Ctrl-C stops the app. The containers keep running; stop them
# with `docker compose down` (add `-v` to wipe the dev DB).
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"

# ── prereqs ────────────────────────────────────────────────────────────────
command -v docker >/dev/null || { echo "error: Docker is required"; exit 1; }
command -v uv     >/dev/null || { echo "error: uv is required — https://docs.astral.sh/uv/"; exit 1; }

# ── infra ────────────────────────────────────────────────────────────────
echo "→ Postgres + GCS emulator (docker compose up -d)…"
docker compose up -d

# ── env ────────────────────────────────────────────────────────────────
if [ ! -f .env ]; then
  echo "→ creating .env from .env.example (+ a fresh SESSION_SECRET)…"
  cp .env.example .env
  python3 -c 'import secrets; print("SESSION_SECRET=" + secrets.token_urlsafe(48))' >> .env
fi

# ── deps + schema (idempotent) ──────────────────────────────────────────
echo "→ uv sync…"; uv sync --quiet
echo "→ applying schema + migrations…"; uv run python -m agentdrive.scripts.apply_schema

# ── app (Ctrl-C stops it) ────────────────────────────────────────────────
echo "→ app on http://localhost:${PORT}  (Ctrl-C to stop)"
uv run uvicorn agentdrive.app:app --reload --port "$PORT" --app-dir src
