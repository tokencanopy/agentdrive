#!/usr/bin/env bash
# The self-host quickstart, run verbatim, then proven over HTTP.
#
# Boots compose.selfhost.yml under ITS OWN project name (`agentdrive-smoke`),
# so it never touches an install made from that file with the default
# project — different containers, different volumes — mints a key with the
# CLI exactly as the README says, and drives the three surfaces a self-hoster
# gets: the /v0 REST API (create a drive, upload a file, list it, share it),
# the MCP transport at /mcp with the same key as a static bearer (discovery,
# initialize, tools/list, one tools/call), and the refusals (no key, a
# revoked key). Every step prints PASS or FAIL; the exit code is the number
# of failures.
#
#   scripts/selfhost-smoke.sh            # leaves the smoke stack running
#   scripts/selfhost-smoke.sh --down     # tears it down afterwards, volumes included
#
# Needs: docker (with compose v2), curl, python3, openssl. Nothing else — no
# jq, no GNU coreutils, so it runs on a stock macOS as well as Linux.
set -uo pipefail
cd "$(dirname "$0")/.."

TEARDOWN=0
case "${1:-}" in
  "") ;;
  --down) TEARDOWN=1 ;;
  *) echo "usage: $0 [--down]" >&2; exit 2 ;;
esac
# Its own project: `-p` overrides the file's `name:`, so the smoke stack and
# an operator's real `agentdrive` stack coexist and `--down -v` can only ever
# remove the smoke stack's volumes.
PROJECT=agentdrive-smoke
COMPOSE="docker compose -p $PROJECT -f compose.selfhost.yml"
PASS=0; FAIL=0
ok()  { PASS=$((PASS + 1)); echo "  PASS  $1"; }
bad() { FAIL=$((FAIL + 1)); echo "  FAIL  $1"; }
check() { if [ "$2" = "$3" ]; then ok "$1 ($2)"; else bad "$1 (got $2, want $3)"; fi; }
json() { python3 -c 'import json, sys; d = json.load(sys.stdin)
for k in sys.argv[1].split("."):
    d = d[int(k)] if isinstance(d, list) else d[k]
print(d)' "$1"; }
uuid() { python3 -c 'import uuid; print(uuid.uuid4())'; }
status() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "== the quickstart, verbatim"
# Append, never truncate: a developer's `.env` (see .env.example) may already
# exist and must survive this.
if ! grep -q '^AGENTDRIVE_SESSION_SECRET=' .env 2>/dev/null; then
  echo "AGENTDRIVE_SESSION_SECRET=$(openssl rand -hex 32)" >> .env
  chmod 600 .env
  echo "  appended a fresh AGENTDRIVE_SESSION_SECRET to .env"
fi
# Read the knobs the same way compose does — from `.env` — so the script and
# the stack agree; then take the smoke stack onto its OWN port, so it can run
# beside an operator's real install on the default one (environment beats
# `.env` in compose interpolation). AGENTDRIVE_SMOKE_PORT overrides it.
set -a; . ./.env; set +a
export AGENTDRIVE_PORT="${AGENTDRIVE_SMOKE_PORT:-18080}"
export AGENTDRIVE_PUBLIC_BASE_URL="http://localhost:$AGENTDRIVE_PORT"
BASE="$AGENTDRIVE_PUBLIC_BASE_URL"
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
# `--build`: this proves the checkout, not whatever image a previous run left
# behind (`up` alone never rebuilds an existing image — the README's upgrade
# line says the same). The release workflow sets AGENTDRIVE_SMOKE_NO_BUILD=1
# after tagging the published image as agentdrive:selfhost, so the smoke
# proves that image instead.
BUILD_FLAG="--build"
[ "${AGENTDRIVE_SMOKE_NO_BUILD:-}" = "1" ] && BUILD_FLAG="--no-build"
$COMPOSE up -d $BUILD_FLAG || { bad "docker compose up"; exit 1; }
for _ in $(seq 1 60); do
  [ "$(status "$BASE/health")" = "200" ] && break
  sleep 2
done
check "api /health" "$(status "$BASE/health")" 200

INIT=$($COMPOSE exec -T api python -m agentdrive.keys init)
check "keys init mints the owner" "$(echo "$INIT" | json owner | cut -c1-6)" tcusr_
CREATE=$($COMPOSE exec -T api python -m agentdrive.keys create --subject-type agent --name claude-code --scopes all)
KEY=$(echo "$CREATE" | json key)
KEY_ID=$(echo "$CREATE" | json id)
check "keys create prints an adk_ key" "$(echo "$KEY" | cut -c1-4)" adk_

echo "== the discovery documents say what a self-hosted install can say"
DISC=$(curl -s "$BASE/.well-known/oauth-protected-resource")
check "no authorization server to discover" "$(echo "$DISC" | json authorization_servers)" "[]"
check "bearer in the header" "$(echo "$DISC" | json bearer_methods_supported.0)" header
check "/jwks does not exist" "$(status "$BASE/jwks")" 404
# The document an MCP client actually reads after a 401 comes from the
# sidecar, which must name the origin the client reached — not its own
# loopback address (the adversarial review of #728 caught exactly that).
MDISC=$(curl -s "$BASE/.well-known/oauth-protected-resource/mcp")
check "the MCP resource is this origin's /mcp" "$(echo "$MDISC" | json resource 2>/dev/null || true)" "$BASE/mcp"
check "the MCP resource names no authorization server" "$(echo "$MDISC" | json authorization_servers 2>/dev/null || true)" "[]"
CHALLENGE=$(curl -s -o /dev/null -D - -X POST "$BASE/mcp" -H "Content-Type: application/json" -d '{}' | grep -i '^www-authenticate' | tr -d '\r')
case "$CHALLENGE" in
  *"resource_metadata=\"$BASE/.well-known/oauth-protected-resource/mcp\""*) ok "the MCP 401 challenge points at this origin";;
  *) bad "the MCP 401 challenge points at this origin (got: $CHALLENGE)";;
esac

echo "== /v0 with no key, then with the key"
check "no key -> 401" "$(status "$BASE/v0/drives")" 401
AUTH="Authorization: Bearer $KEY"
DRIVE=$(curl -s "$BASE/v0/drives" -H "$AUTH" -H "Idempotency-Key: $(uuid)" \
  -H "Content-Type: application/json" -d '{"name":"Smoke drive"}')
DRIVE_ID=$(echo "$DRIVE" | json id 2>/dev/null || true)
ROOT=$(echo "$DRIVE" | json root_folder_id 2>/dev/null || true)
check "create a drive" "$(echo "$DRIVE_ID" | cut -c1-4)" drv_
ART=$(printf '# Hello from a self-hosted AgentDrive\n' | curl -s "$BASE/v0/drives/$DRIVE_ID/artifacts" \
  -H "$AUTH" -H "Idempotency-Key: $(uuid)" \
  -F "parent_id=$ROOT" -F "name=hello.md" -F "content=@-;filename=hello.md;type=text/markdown")
ART_ID=$(echo "$ART" | json id 2>/dev/null || true)
check "upload an artifact" "$(echo "$ART_ID" | cut -c1-4)" art_
ENTRIES=$(curl -s "$BASE/v0/drives/$DRIVE_ID/entries?parent_id=$ROOT" -H "$AUTH")
check "the upload is listed" "$(echo "$ENTRIES" | json entries.0.name 2>/dev/null || true)" hello.md
BYTES=$(curl -s -L "$BASE/v0/drives/$DRIVE_ID/artifacts/$ART_ID/content" -H "$AUTH")
check "the bytes read back" "$(echo "$BYTES" | head -1)" "# Hello from a self-hosted AgentDrive"
SHARE=$(curl -s "$BASE/v0/drives/$DRIVE_ID/shares" -H "$AUTH" -H "Idempotency-Key: $(uuid)" \
  -H "Content-Type: application/json" -d "{\"resource_type\":\"artifact\",\"resource_id\":\"$ART_ID\"}")
check "share the artifact" "$(echo "$SHARE" | json id 2>/dev/null | cut -c1-4)" shr_
SHARE_URL=$(echo "$SHARE" | json url 2>/dev/null || true)
case "$SHARE_URL" in
  "$BASE"/s/*) ok "the share link is on this origin ($SHARE_URL)";;
  *) bad "the share link is on this origin (got: $SHARE_URL)";;
esac
SHARE_PAGE=$(curl -s -L "$SHARE_URL")
check "the share page opens with no credential" "$(status -L "$SHARE_URL")" 200
case "$SHARE_PAGE" in
  *"Open in console"*) bad "no console button on a self-hosted share page";;
  *) ok "no console button on a self-hosted share page";;
esac

echo "== the same key as an MCP bearer"
rpc() {
  curl -s -X POST "$BASE/mcp" -H "$AUTH" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -H "MCP-Protocol-Version: 2025-06-18" -d "$1"
}
INITR=$(rpc '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"selfhost-smoke","version":"0"}}}')
check "initialize" "$(echo "$INITR" | json result.serverInfo.name 2>/dev/null || echo "$INITR" | head -c 120)" tokencanopy-agentdrive
TOOLS=$(rpc '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | python3 -c 'import json, sys; print(",".join(sorted(t["name"] for t in json.load(sys.stdin)["result"]["tools"])))' 2>/dev/null || true)
case ",$TOOLS," in *,list_drives,*) ok "tools/list offers list_drives";; *) bad "tools/list offers list_drives (got: $TOOLS)";; esac
CALL=$(rpc '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_drives","arguments":{}}}')
if echo "$CALL" | python3 -c 'import json, sys; r = json.load(sys.stdin); sys.exit(1 if r.get("result", {}).get("isError") or "error" in r else 0)'; then
  ok "tools/call list_drives"
else
  bad "tools/call list_drives ($(echo "$CALL" | head -c 200))"
fi

echo "== revoke, and the key is refused everywhere"
$COMPOSE exec -T api python -m agentdrive.keys revoke "$KEY_ID" > /dev/null
check "revoked key on /v0 -> 401" "$(status "$BASE/v0/drives" -H "$AUTH")" 401
# The sidecar keeps a 30-second positive cache per key, so a `tools/list`
# right after revocation can still be answered from it — by design; the API
# is the authority, and every `tools/call` reaches it. A refusal is a 401/403
# from the transport OR a JSON-RPC error from the API; an empty or unreadable
# answer is NOT a refusal.
AFTER_BODY=$(mktemp)
AFTER_CODE=$(curl -s -o "$AFTER_BODY" -w '%{http_code}' -X POST "$BASE/mcp" -H "$AUTH" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -H "MCP-Protocol-Version: 2025-06-18" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"list_drives","arguments":{}}}')
if [ "$AFTER_CODE" = "401" ] || [ "$AFTER_CODE" = "403" ]; then
  ok "revoked key's tools/call is refused (transport $AFTER_CODE)"
elif [ "$AFTER_CODE" = "200" ] && python3 -c 'import json, sys
r = json.load(open(sys.argv[1]))
sys.exit(0 if r.get("result", {}).get("isError") or "error" in r else 1)' "$AFTER_BODY"; then
  ok "revoked key's tools/call is refused by the API"
else
  bad "revoked key still called a tool (HTTP $AFTER_CODE: $(head -c 200 "$AFTER_BODY"))"
fi
rm -f "$AFTER_BODY"

echo "== the container logs for THIS run are clean"
LOGS=$($COMPOSE logs --no-color --since "$START" api 2>/dev/null)
if echo "$LOGS" | grep -q "Traceback"; then bad "api log has a traceback"; else ok "api log has no traceback"; fi
if echo "$LOGS" | grep -qE '" 5[0-9][0-9] '; then bad "api log has a 5xx"; else ok "api log has no 5xx"; fi

echo
echo "================  $PASS passed, $FAIL failed  ================"
if [ "$TEARDOWN" = "1" ]; then
  $COMPOSE down -v > /dev/null 2>&1 && echo "smoke stack torn down (its volumes removed)"
else
  echo "smoke stack left running: $BASE  (tear down with: $COMPOSE down -v)"
fi
exit "$FAIL"
