#!/usr/bin/env bash
# Starts the Compose stack with the TLS overlay (tls.yaml) and checks that the site is served
# only over TLS and that PostgreSQL accepts only TLS connections with the right client
# certificate and password; then removes the stack (including volumes) and what it created.
# Usage: deploy/compose/tls-smoke-test.sh   (from any directory)
set -euo pipefail

cd "$(dirname "$0")/../.."
project=baluarte-tls-smoke
port=${BALUARTE_TLS_PORT:-8443}
compose=(docker compose -p "$project" -f compose.yaml -f deploy/compose/tls.yaml)
created=()
made_tls=false

cleanup() {
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  for file in ${created[@]+"${created[@]}"}; do rm -f "$file"; done
  if [[ $made_tls == true ]]; then
    if [[ $(uname) == Linux ]]; then sudo rm -rf secrets/tls; else rm -rf secrets/tls; fi
  fi
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

for file in secrets/postgres_password secrets/app_database_password secrets/backup_database_password secrets/secret_key; do
  if [[ ! -f $file ]]; then
    mkdir -p secrets
    # Readable by the non-root containers on Linux bind mounts; dev-only secrets.
    (umask 022 && openssl rand -base64 32 > "$file")
    created+=("$file")
  fi
done

if [[ ! -d secrets/tls ]]; then
  made_tls=true
  deploy/compose/make-tls-certs.sh >/dev/null
  if [[ $(uname) == Linux ]]; then
    # Bind mounts keep host ownership: hand each key to the container user that reads it.
    sudo chown 101 secrets/tls/web.key
    sudo chown 70:70 secrets/tls/client-baluarte_backup.key
    sudo chown 10001 secrets/tls/client-baluarte.key secrets/tls/client-baluarte_app.key
  fi
fi

BALUARTE_TLS_PORT=$port "${compose[@]}" up -d --build --wait --wait-timeout 180

ca=secrets/tls/ca.crt
body=$(curl -fsS --cacert "$ca" "https://localhost:$port/api/v1/ready") ||
  fail "not ready over https: $(curl -sS --cacert "$ca" "https://localhost:$port/api/v1/ready")"
grep -q '"status":"ready"' <<<"$body" || fail "ready body: $body"
echo "ok  https://localhost:$port/api/v1/ready with the local CA (database migrated over TLS)"

hsts=$(curl -fsS -o /dev/null -D - --cacert "$ca" "https://localhost:$port/" | tr -d '\r' |
  grep -i '^strict-transport-security:' || true)
[[ -n $hsts ]] || fail "no Strict-Transport-Security over https"
echo "ok  HSTS sent"

if curl -fsS -o /dev/null --tls-max 1.1 --cacert "$ca" "https://localhost:$port/" 2>/dev/null; then
  fail "TLS 1.1 accepted"
fi
echo "ok  TLS 1.1 refused"

db=$("${compose[@]}" ps -q db)
sessions=$(docker exec "$db" psql -U baluarte -d baluarte -Atc \
  "SELECT count(*) FILTER (WHERE NOT s.ssl), count(*) FILTER (WHERE s.ssl)
     FROM pg_stat_ssl s JOIN pg_stat_activity a USING (pid) WHERE a.client_addr IS NOT NULL")
[[ ${sessions%%|*} == 0 && ${sessions##*|} -gt 0 ]] || fail "database sessions (plain|tls): $sessions"
echo "ok  every application session to PostgreSQL uses TLS ($sessions)"

network="${project}_default"
probe() {
  # As the API's user, who owns its client key on Linux (see make-tls-certs.sh).
  docker run --rm --user 10001 --network "$network" -v "$PWD/secrets/tls:/t:ro" \
    -e PGPASSWORD="$(cat secrets/app_database_password)" postgres:17-alpine \
    psql "host=db user=baluarte_app dbname=baluarte $1" -Atc "SELECT 1" >/dev/null 2>&1
}
probe "sslmode=disable" && fail "a plain connection was accepted"
echo "ok  plain connection refused"
probe "sslmode=verify-full sslrootcert=/t/ca.crt" && fail "TLS without a client certificate accepted"
echo "ok  TLS without a client certificate refused"
probe "sslmode=verify-full sslrootcert=/t/ca.crt sslcert=/t/client-baluarte.crt sslkey=/t/client-baluarte.key" &&
  fail "another role's certificate was accepted"
echo "ok  another role's client certificate refused"

echo "TLS smoke test passed"
