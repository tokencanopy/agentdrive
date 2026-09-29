#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 <base-openapi> [revision-openapi]" >&2
  exit 2
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
base="$1"
revision="${2:-$repo_root/tests/openapi.golden.json}"
levels="$repo_root/api/oasdiff-levels.txt"
format="${OASDIFF_FORMAT:-text}"
tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/agentdrive-openapi-compat.XXXXXX")"
trap 'rm -rf "$tmpdir"' EXIT

materialize() {
  local source="$1"
  local destination="$2"
  if [[ -f "$source" ]]; then
    cp "$source" "$destination"
  elif git -C "$repo_root" cat-file -e "$source" 2>/dev/null; then
    git -C "$repo_root" show "$source" >"$destination"
  else
    echo "OpenAPI input is not a file or Git object: $source" >&2
    exit 2
  fi
}

materialize "$base" "$tmpdir/base.json"
materialize "$revision" "$tmpdir/revision.json"

PYTHONPATH="$repo_root/src" python3 -m agentdrive.scripts.openapi_compat \
  "$tmpdir/base.json" \
  "$tmpdir/revision.json" \
  --repo-root "$repo_root" \
  --normalized-base-out "$tmpdir/base.normalized.json" \
  --normalized-revision-out "$tmpdir/revision.normalized.json"

run_oasdiff() {
  if [[ -n "${OASDIFF_BIN:-}" ]]; then
    local binary metadata
    binary="$(command -v "$OASDIFF_BIN" 2>/dev/null || true)"
    if [[ -z "$binary" ]]; then
      echo "OASDIFF_BIN is not executable: $OASDIFF_BIN" >&2
      exit 2
    fi
    metadata="$(go version -m "$binary" 2>/dev/null || true)"
    if ! grep -Fq $'\tmod\tgithub.com/oasdiff/oasdiff\tv1.23.0\t' <<<"$metadata"; then
      echo "OASDIFF_BIN must be github.com/oasdiff/oasdiff v1.23.0: $binary" >&2
      exit 2
    fi
    "$binary" "$@"
    return
  fi
  GOWORK=off go run github.com/oasdiff/oasdiff@v1.23.0 "$@"
}

run_oasdiff breaking \
  --allow-external-refs=false \
  --fail-on WARN \
  --stability-level stable \
  --severity-levels "$levels" \
  --format "$format" \
  "$tmpdir/base.normalized.json" "$tmpdir/revision.normalized.json"
