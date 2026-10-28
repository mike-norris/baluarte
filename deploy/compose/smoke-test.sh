#!/usr/bin/env bash
# Builds and starts the Compose stack, checks it, then removes it (including volumes).
# Usage: deploy/compose/smoke-test.sh   (from any directory)
set -euo pipefail

cd "$(dirname "$0")/../.."
project=baluarte-smoke
compose=(docker compose -p "$project")
created_secret=false

cleanup() {
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  if $created_secret; then rm -f secrets/postgres_password; fi
}
trap cleanup EXIT

if [[ ! -f secrets/postgres_password ]]; then
  mkdir -p secrets
  (umask 077 && openssl rand -base64 32 > secrets/postgres_password)
  created_secret=true
fi

fail() { echo "FAIL: $*" >&2; exit 1; }

"${compose[@]}" up -d --build --wait --wait-timeout 120

body=$(curl -fsS http://127.0.0.1:8000/api/v1/health) || fail "API health unreachable"
grep -q '"status":"ok"' <<<"$body" || fail "API health body: $body"
echo "ok  api /api/v1/health"

body=$(curl -fsS http://127.0.0.1:8080/api/v1/health) || fail "API via web proxy unreachable"
grep -q '"status":"ok"' <<<"$body" || fail "proxied health body: $body"
echo "ok  web proxy /api/v1/health"

curl -fsS http://127.0.0.1:8080/ | grep -q '<div id="root">' || fail "web index not served"
echo "ok  web /"

curl -fsS http://127.0.0.1:8080/inventory | grep -q '<div id="root">' || fail "SPA deep link not served"
echo "ok  web /inventory (SPA fallback)"

headers=$(curl -fsSI http://127.0.0.1:8080/ | tr -d '\r')
grep -qi '^content-security-policy:' <<<"$headers" || fail "missing Content-Security-Policy"
grep -qi '^server: nginx$' <<<"$headers" || fail "nginx version exposed in Server header"
echo "ok  web security headers"

for service in api web; do
  uid=$("${compose[@]}" exec -T "$service" id -u)
  [[ "$uid" != "0" ]] || fail "$service runs as root"
  echo "ok  $service runs as uid $uid"
done

"${compose[@]}" exec -T db pg_isready -U baluarte -d baluarte >/dev/null || fail "database not ready"
echo "ok  db ready"

echo "Smoke test passed."
