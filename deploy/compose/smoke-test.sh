#!/usr/bin/env bash
# Builds and starts the Compose stack, checks it, then removes it (including volumes).
# Usage: deploy/compose/smoke-test.sh   (from any directory)
set -euo pipefail

cd "$(dirname "$0")/../.."
project=baluarte-smoke
compose=(docker compose -p "$project")
created=()

cleanup() {
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  for file in "${created[@]}"; do rm -f "$file"; done
}
trap cleanup EXIT

for file in secrets/postgres_password secrets/secret_key; do
  if [[ ! -f $file ]]; then
    mkdir -p secrets
    # Readable by the non-root API container on Linux bind mounts; dev-only secrets.
    (umask 022 && openssl rand -base64 32 > "$file")
    created+=("$file")
  fi
done

fail() { echo "FAIL: $*" >&2; exit 1; }

"${compose[@]}" up -d --build --wait --wait-timeout 120

body=$(curl -fsS http://127.0.0.1:8000/api/v1/health) || fail "API health unreachable"
grep -q '"status":"ok"' <<<"$body" || fail "API health body: $body"
echo "ok  api /api/v1/health"

body=$(curl -fsS http://127.0.0.1:8000/api/v1/ready) || fail "API not ready: $(curl -sS http://127.0.0.1:8000/api/v1/ready)"
grep -q '"status":"ready"' <<<"$body" || fail "ready body: $body"
echo "ok  api /api/v1/ready (database migrated)"

[[ $(docker inspect -f '{{.State.ExitCode}}' "$("${compose[@]}" ps -aq migrate)") == 0 ]] || fail "migrate did not exit 0"
echo "ok  migrate exited 0"

body=$(curl -fsS http://127.0.0.1:8080/api/v1/health) || fail "API via web proxy unreachable"
grep -q '"status":"ok"' <<<"$body" || fail "proxied health body: $body"
echo "ok  web proxy /api/v1/health"

# Deny by default: without OIDC configured, protected endpoints must not answer 200.
status=$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/v1/auth/me)
[[ $status == 503 ]] || fail "unconfigured /api/v1/auth/me returned $status, expected 503"
echo "ok  web proxy /api/v1/auth/me refused without OIDC (503)"
status=$(curl -sS -o /dev/null -w '%{http_code}' -X POST http://127.0.0.1:8080/api/v1/gitlab/diagnostics)
[[ $status == 503 ]] || fail "unconfigured POST /api/v1/gitlab/diagnostics returned $status, expected 503"
echo "ok  web proxy /api/v1/gitlab/diagnostics refused without OIDC (503)"
# First-run setup: a fresh stack needs setting up, and the code from the API log unlocks it.
body=$(curl -fsS http://127.0.0.1:8080/api/v1/setup/status) || fail "setup status unreachable"
grep -q '"state":"needs_setup"' <<<"$body" || fail "setup status body: $body"
grep -q '"secret_key_configured":true' <<<"$body" || fail "secret key not mounted: $body"
echo "ok  web proxy /api/v1/setup/status (needs_setup, secret key mounted)"
code=""
for _ in $(seq 1 20); do
  code=$("${compose[@]}" logs --no-log-prefix api | grep -oE 'setup code [A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}' | tail -1 | cut -d' ' -f3) || true
  [[ -n $code ]] && break
  sleep 1
done
[[ -n $code ]] || fail "no setup code in the api log"
status=$(curl -sS -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
  -d '{"code":"AAAA-AAAA-AAAA"}' http://127.0.0.1:8080/api/v1/setup/session)
[[ $status == 401 ]] || fail "wrong setup code returned $status, expected 401"
status=$(curl -sS -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
  -d "{\"code\":\"$code\"}" http://127.0.0.1:8080/api/v1/setup/session)
[[ $status == 200 ]] || fail "setup code from the log returned $status, expected 200"
echo "ok  setup code from the api log starts a setup session"
code=$("${compose[@]}" exec -T api baluarte-setup-code | tail -1)
[[ $code =~ ^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$ ]] || fail "baluarte-setup-code printed: $code"
echo "ok  baluarte-setup-code issues a new code"

BALUARTE_GITLAB_URL=https://gitlab.example.test docker compose -f compose.yaml -f deploy/compose/gitlab.yaml config -q \
  || fail "GitLab Compose override does not render"
echo "ok  GitLab Compose override renders"

# Structured logs, with OAuth parameters in access-log query strings redacted.
curl -sS -o /dev/null 'http://127.0.0.1:8080/api/v1/auth/callback?code=SMOKECODE123&state=SMOKESTATE456'
api_logs=$("${compose[@]}" logs --no-log-prefix api)
grep -q '"logger": "uvicorn.access"' <<<"$api_logs" || fail "api access log is not JSON"
grep -q 'auth/callback' <<<"$api_logs" || fail "callback request was not logged"
! grep -qE 'SMOKECODE123|SMOKESTATE456' <<<"$api_logs" || fail "OAuth parameters leaked into api logs"
echo "ok  api logs are JSON and redact OAuth parameters"

curl -fsS http://127.0.0.1:8080/ | grep -q '<div id="root">' || fail "web index not served"
echo "ok  web /"

curl -fsS http://127.0.0.1:8080/inventory | grep -q '<div id="root">' || fail "SPA deep link not served"
echo "ok  web /inventory (SPA fallback)"

curl -fsS 'http://127.0.0.1:8080/sign-in?error=expired' | grep -q '<div id="root">' || fail "sign-in page not served"
echo "ok  web /sign-in (sign-in error landing page)"

headers=$(curl -fsSI http://127.0.0.1:8080/ | tr -d '\r')
grep -qi '^content-security-policy:' <<<"$headers" || fail "missing Content-Security-Policy"
grep -qi '^server: nginx$' <<<"$headers" || fail "nginx version exposed in Server header"
echo "ok  web security headers"

for service in api web worker; do
  uid=$("${compose[@]}" exec -T "$service" id -u)
  [[ "$uid" != "0" ]] || fail "$service runs as root"
  echo "ok  $service runs as uid $uid"
done

[[ $(docker inspect -f '{{.State.Health.Status}}' "$("${compose[@]}" ps -q worker)") == healthy ]] \
  || fail "worker is not healthy"
echo "ok  worker healthy (heartbeat)"

worker_id=$("${compose[@]}" ps -q worker)
"${compose[@]}" stop -t 30 worker >/dev/null
[[ $(docker inspect -f '{{.State.ExitCode}}' "$worker_id") == 0 ]] || fail "worker did not exit cleanly on SIGTERM"
docker logs "$worker_id" 2>&1 | grep -q "received SIGTERM" || fail "worker did not handle SIGTERM"
echo "ok  worker stops cleanly on SIGTERM"

"${compose[@]}" exec -T db pg_isready -U baluarte -d baluarte >/dev/null || fail "database not ready"
echo "ok  db ready"

echo "Smoke test passed."
