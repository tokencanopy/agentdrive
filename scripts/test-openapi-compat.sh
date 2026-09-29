#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
checker="$repo_root/scripts/check-openapi-compat.sh"
fixtures="$repo_root/tests/openapi_compat_fixtures"
tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/agentdrive-openapi-fixtures.XXXXXX")"
trap 'rm -rf "$tmpdir"' EXIT
python3 "$repo_root/scripts/build-openapi-compat-fixtures.py" \
  "$fixtures/base.json" "$tmpdir"

expect_pass() {
  local name="$1"
  shift
  if ! output="$("$checker" "$@" 2>&1)"; then
    echo "expected pass: $name" >&2
    echo "$output" >&2
    exit 1
  fi
}

expect_fail() {
  local name="$1"
  local expected="$2"
  shift 2
  if output="$("$checker" "$@" 2>&1)"; then
    echo "expected failure: $name" >&2
    exit 1
  fi
  if ! grep -Fq "$expected" <<<"$output"; then
    echo "wrong failure for $name; expected '$expected'" >&2
    echo "$output" >&2
    exit 1
  fi
}

expect_pass \
  "additive response field" \
  "$fixtures/base.json" "$fixtures/additive-response.json"
expect_fail \
  "first request maxLength" \
  "request-property-max-length-set" \
  "$fixtures/base.json" "$fixtures/request-bound-set.json"
expect_fail \
  "removed response field" \
  "response-required-property-removed" \
  "$fixtures/base.json" "$fixtures/response-field-removed.json"
expect_fail \
  "removed request field" \
  "request-property" \
  "$fixtures/base.json" "$tmpdir/request-field-removed.json"
expect_fail \
  "response type change" \
  "response-property-type-changed" \
  "$fixtures/base.json" "$tmpdir/response-type-changed.json"
expect_fail \
  "response nullability change" \
  "response-property-became-nullable" \
  "$fixtures/base.json" "$tmpdir/response-nullability-changed.json"
expect_fail \
  "operation ID rename" \
  "operationId changed" \
  "$fixtures/base.json" "$tmpdir/operation-id-renamed.json"
expect_fail \
  "stable tag change" \
  "ordered tags changed" \
  "$fixtures/base.json" "$tmpdir/operation-tag-changed.json"
expect_fail \
  "public schema rename" \
  "public schema names removed" \
  "$fixtures/base.json" "$tmpdir/schema-renamed.json"
expect_fail \
  "public schema retarget with old alias retained" \
  "stable \$ref binding changed" \
  "$fixtures/base.json" "$tmpdir/schema-retargeted-with-alias.json"
expect_fail \
  "security scheme change" \
  "securitySchemes changed" \
  "$fixtures/base.json" "$tmpdir/security-changed.json"
expect_fail \
  "stable operation marked beta" \
  "stability decrease" \
  "$fixtures/base.json" "$tmpdir/stability-decreased.json"

echo "OpenAPI compatibility policy fixtures passed."
