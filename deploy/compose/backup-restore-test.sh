#!/usr/bin/env bash
# Verifies the backup and restore procedure in docs/OPERATIONS.md against throwaway containers:
# migrate a database, write audit events, pg_dump it, restore into a new database, re-run
# migrations, and check the schema revision, the immutability triggers and every audit digest.
# Uses its own network, container names and image tag and publishes no ports, so it does not
# interfere with the Compose stack or the smoke tests.
# Usage: deploy/compose/backup-restore-test.sh   (from any directory)
set -euo pipefail

cd "$(dirname "$0")/../.."
id="baluarte-backup-test-$$"
image="baluarte-app:backup-test"
work=$(mktemp -d)

cleanup() {
  docker rm -f "$id-db" >/dev/null 2>&1 || true
  docker network rm "$id" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

openssl rand -base64 32 > "$work/password"
chmod 644 "$work/password"
secret=(-v "$work/password:/run/secrets/postgres_password:ro")

docker network create "$id" >/dev/null
docker run -d --name "$id-db" --network "$id" --network-alias db "${secret[@]}" \
  -e POSTGRES_DB=baluarte -e POSTGRES_USER=baluarte \
  -e POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password \
  postgres:17-alpine >/dev/null
docker build -q -t "$image" backend >/dev/null
for _ in $(seq 60); do
  docker exec "$id-db" pg_isready -q -U baluarte -d baluarte && break
  sleep 1
done
docker exec "$id-db" pg_isready -q -U baluarte -d baluarte || fail "database did not start"

psql() { docker exec -i "$id-db" psql -v ON_ERROR_STOP=1 -qtAX -U baluarte "$@"; }
# Runs the app image against database $1 with the remaining arguments.
app() {
  local database=$1
  shift
  docker run --rm -i --network "$id" --read-only --tmpfs /tmp "${secret[@]}" \
    -e BALUARTE_DATABASE_URL="postgresql://baluarte@db:5432/$database" \
    -e BALUARTE_DATABASE_PASSWORD_FILE=/run/secrets/postgres_password \
    -e BALUARTE_LOG_LEVEL=WARNING "$image" "$@"
}

# Prints "<events> <mismatched ids>" by recomputing each digest as baluarte.audit does.
verify_digests() {
  app "$1" python - <<'PY'
from sqlalchemy import select
from sqlalchemy.orm import Session

from baluarte.audit import details_digest
from baluarte.db.models import AuditEvent
from baluarte.db.session import get_engine

with Session(get_engine()) as db:
    events = db.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
    bad = [e.id for e in events if details_digest(e.details) != e.details_digest]
print(len(events), ",".join(map(str, bad)) or "none")
PY
}

app baluarte baluarte-migrate --timeout 30 >/dev/null || fail "migrations failed on the source database"
app baluarte python - <<'PY' || fail "could not write audit events"
from sqlalchemy.orm import Session

from baluarte import audit
from baluarte.db.session import get_engine

with Session(get_engine()) as db:
    audit.record(db, actor="user:1", action="auth.login", resource_type="session", resource_id="1")
    audit.record(
        db,
        actor="user:1",
        action="gitlab.diagnostics_run",
        resource_type="gitlab_instance",
        details={"status": "ok", "checks": {"token": "ok"}, "note": "naïve ✓ unicode"},
    )
    audit.record(db, actor="system", action="policy.created", resource_type="policy", details={"n": 1.5})
    db.commit()
PY
source_head=$(psql -d baluarte -c "SELECT version_num FROM alembic_version")
[[ -n $source_head ]] || fail "source database has no schema revision"
echo "ok  source database migrated to $source_head with 3 audit events"

# Backup: custom format, taken from a consistent snapshot.
docker exec "$id-db" pg_dump -U baluarte -d baluarte -Fc -f /tmp/baluarte.dump
docker exec "$id-db" pg_restore -l /tmp/baluarte.dump >/dev/null || fail "dump is not readable"
echo "ok  pg_dump (custom format)"

# Restore into a new, empty database.
psql -d postgres -c "CREATE DATABASE restored OWNER baluarte"
docker exec "$id-db" pg_restore -U baluarte -d restored --no-owner --exit-on-error /tmp/baluarte.dump \
  || fail "pg_restore failed"
echo "ok  pg_restore into a new database"

app restored baluarte-migrate --timeout 30 >/dev/null || fail "migrations failed on the restored database"
restored_head=$(psql -d restored -c "SELECT version_num FROM alembic_version")
[[ $restored_head == "$source_head" ]] || fail "restored revision $restored_head != $source_head"
echo "ok  baluarte-migrate on the restored database (revision $restored_head)"

read -r count bad <<<"$(verify_digests restored)"
[[ $count == 3 && $bad == none ]] || fail "restored digests: $count events, mismatched: $bad"
echo "ok  all $count audit digests match after restore"

if psql -d restored -c "UPDATE audit_event SET actor = 'x'" 2>/dev/null; then
  fail "audit_event accepted an UPDATE after restore"
fi
echo "ok  immutability triggers restored (UPDATE on audit_event rejected)"

# Negative control: a table owner can disable the trigger and rewrite a row; the digest check
# catches that, but not a rewrite that also recomputes the digest (docs/AUDIT_EVIDENCE.md).
psql -d restored <<'SQL'
ALTER TABLE audit_event DISABLE TRIGGER audit_event_immutable;
UPDATE audit_event SET details = '{"status": "failed"}' WHERE action = 'gitlab.diagnostics_run';
ALTER TABLE audit_event ENABLE TRIGGER audit_event_immutable;
SQL
read -r count bad <<<"$(verify_digests restored)"
[[ $bad != none ]] || fail "digest check missed a rewritten audit row"
echo "ok  digest check detects a rewritten audit row (event $bad)"

echo "Backup and restore test passed."
