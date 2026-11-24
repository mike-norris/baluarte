#!/usr/bin/env bash
# Checks off-site backup copies and restores with the overlay (offsite.yaml): the backup service
# copies its encrypted file to the SeaweedFS container and records it; the copy read back
# independently matches, decrypts with the offline key and holds a restorable dump; restore-test
# restores it and records the result; list shows it; restore recovers into an empty database
# only; and a tampered copy fails the restore test. Then removes the stack (including volumes)
# and what it created.
# Usage: deploy/compose/offsite-test.sh   (from any directory)
set -euo pipefail

cd "$(dirname "$0")/../.."
project=baluarte-offsite-test
work=$(mktemp -d)
compose=(docker compose -p "$project" -f compose.yaml -f deploy/compose/offsite.yaml --profile backup)
created=()

cleanup() {
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  for file in ${created[@]+"${created[@]}"}; do rm -f "$file"; done
  # The containers' users own files in these directories.
  docker run --rm -v "$work:/w" alpine:3.24 rm -rf /w/backups /w/offsite /w/download >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }

for file in secrets/postgres_password secrets/app_database_password secrets/backup_database_password \
  secrets/secret_key secrets/offsite_access_key_id secrets/offsite_secret_access_key \
  secrets/seaweedfs_s3.json; do
  [[ -f $file ]] || created+=("$file")
done
for file in secrets/postgres_password secrets/app_database_password secrets/backup_database_password secrets/secret_key; do
  if [[ ! -f $file ]]; then
    mkdir -p secrets
    (umask 022 && openssl rand -base64 32 > "$file")
  fi
done
deploy/compose/make-offsite-credentials.sh >/dev/null

mkdir -p "$work/backups" "$work/offsite" "$work/download" "$work/age"
# mktemp makes the directory 0700; the containers' users (uid 70) must reach inside it on Linux.
chmod 755 "$work" "$work/age"
chmod 777 "$work/backups" "$work/download"
"${compose[@]}" build -q backup migrate
# The tag compose.yaml gives the backup image.
image="${BALUARTE_IMAGE_PREFIX:-}baluarte-backup:${BALUARTE_VERSION:-dev}"
docker run --rm --user 0 --entrypoint age-keygen -v "$work/age:/k" "$image" -o /k/key.txt 2>/dev/null
docker run --rm --user 0 --entrypoint chmod -v "$work/age:/k" "$image" 644 /k/key.txt
recipient=$(grep -o 'age1[0-9a-z]*' "$work/age/key.txt")

export BALUARTE_BACKUP_DIR="$work/backups" BALUARTE_OFFSITE_DIR="$work/offsite"
export BALUARTE_BACKUP_RECIPIENT="$recipient" BALUARTE_BACKUP_S3_KEEP=2
"${compose[@]}" up -d --wait --wait-timeout 180 db offsite
"${compose[@]}" run --rm -T migrate >/dev/null 2>&1 || fail "migrations failed"
# Audit events for the restore test's digest check, written by the application itself.
"${compose[@]}" run --rm -T --no-deps -e BALUARTE_LOG_LEVEL=WARNING migrate python - <<'PY' \
  >/dev/null || fail "could not write audit events"
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
"${compose[@]}" up -d --wait --wait-timeout 180 backup

db=$("${compose[@]}" ps -q db)
row=""
for _ in $(seq 30); do
  row=$(docker exec "$db" psql -U baluarte -d baluarte -Atc \
    "SELECT status, offsite_status, file_name FROM backup_run ORDER BY id DESC LIMIT 1")
  [[ -n $row ]] && break
  sleep 2
done
IFS="|" read -r status offsite name <<<"$row"
[[ $status == succeeded && $offsite == copied ]] || fail "backup_run: $row"
echo "ok  backup $name recorded as copied off site"

backup=$("${compose[@]}" ps -q backup)
for _ in 1 2 3; do
  sleep 1
  docker exec "$backup" baluarte-backup once >/dev/null 2>&1 || fail "a further backup run failed"
done

network="${project}_default"
docker run --rm --network "$network" --user 70 --entrypoint sh \
  -v "$PWD/secrets:/s:ro" -v "$work/download:/d" "$image" -c '
  export RCLONE_CONFIG_X_TYPE=s3 RCLONE_CONFIG_X_PROVIDER=Other \
    RCLONE_CONFIG_X_ENDPOINT=http://offsite:8333 RCLONE_CONFIG_X_FORCE_PATH_STYLE=true \
    RCLONE_CONFIG_X_ACCESS_KEY_ID="$(cat /s/offsite_access_key_id)" \
    RCLONE_CONFIG_X_SECRET_ACCESS_KEY="$(cat /s/offsite_secret_access_key)"
  rclone copy -q x:baluarte-backups/baluarte/ /d/' 2>/dev/null || fail "could not download the copies"
count=$(find "$work/download" -name 'baluarte-*.dump.age' | wc -l | tr -d ' ')
[[ $count == 2 ]] || fail "expected 2 copies kept off site, found $count"
echo "ok  off-site retention kept the newest 2"

newest=$(find "$work/download" -name 'baluarte-*.dump.age' | sort | tail -1)
want=$(docker exec "$db" psql -U baluarte -d baluarte -Atc \
  "SELECT sha256 FROM backup_run WHERE file_name = '$(basename "$newest")'")
got=$(sha256sum "$newest" 2>/dev/null | cut -d' ' -f1 || shasum -a 256 "$newest" | cut -d' ' -f1)
[[ -n $want && $got == "$want" ]] || fail "downloaded copy sha256 $got, recorded $want"
echo "ok  downloaded copy matches the recorded SHA-256"

tables=$(docker run --rm --entrypoint sh -v "$work:/w:ro" "$image" -c \
  "age -d -i /w/age/key.txt /w/download/$(basename "$newest") | pg_restore --list | grep -c 'TABLE DATA'")
[[ $tables -gt 10 ]] || fail "decrypted copy lists $tables tables"
echo "ok  the copy decrypts with the offline key into a dump of $tables tables"

psql_() { docker exec "$db" psql -U baluarte -d baluarte -AtXc "$1"; }
restore_test=("${compose[@]}" run --rm -T --no-deps
  -v "$work/age/key.txt:/run/secrets/restore_identity:ro" backup)

"${restore_test[@]}" restore-test > "$work/restore-test.log" 2>&1 \
  || fail "restore-test failed: $(tail -c 800 "$work/restore-test.log")"
row=$(psql_ "SELECT status, source, file_name, schema_revision, table_count, audit_events,
  digest_mismatches FROM restore_run ORDER BY id DESC LIMIT 1")
IFS="|" read -r status source restored revision tables events mismatches <<<"$row"
[[ $status == succeeded && $source == offsite && $restored == "$(basename "$newest")" ]] \
  || fail "restore_run: $row"
[[ $revision == "$(psql_ "SELECT version_num FROM alembic_version")" && $mismatches == 0 \
  && $events -ge 3 ]] \
  || fail "restore_run: $row"
echo "ok  restore-test restored the off-site copy: revision $revision, $tables tables, $events audit events, 0 digest mismatches"

listing=$("${restore_test[@]}" list 2>&1) || fail "list failed: $listing"
grep -q "^$(basename "$newest") .* ok  *ok$" <<<"$listing" || fail "list: $listing"
[[ $(grep -c " ok  *missing$" <<<"$listing") -ge 2 ]] || fail "list: $listing"
echo "ok  list shows the newest copies local and off site, and the trimmed ones missing off site"

docker exec "$db" createdb -U baluarte recovered
recover=("${compose[@]}" run --rm -T --no-deps
  -v "$work/age/key.txt:/run/secrets/restore_identity:ro"
  -v "$PWD/secrets/postgres_password:/run/secrets/target_password:ro"
  -e "BALUARTE_RESTORE_TARGET=host=db dbname=recovered user=baluarte sslmode=disable"
  -e BALUARTE_RESTORE_TARGET_PASSWORD_FILE=/run/secrets/target_password backup)
"${recover[@]}" restore "$(basename "$newest")" > "$work/restore.log" 2>&1 \
  || fail "restore failed: $(tail -c 800 "$work/restore.log")"
recovered=$(docker exec "$db" psql -U baluarte -d recovered -AtXc "SELECT version_num FROM alembic_version")
[[ $recovered == "$revision" ]] || fail "the recovered database is at revision $recovered"
if "${recover[@]}" restore "$(basename "$newest")" > "$work/restore.log" 2>&1; then
  fail "restore overwrote a database that has tables"
fi
grep -q "restore only into an empty database" "$work/restore.log" || fail "$(cat "$work/restore.log")"
echo "ok  restore recovered into an empty database and refused one with tables"

# Replace the newest off-site copy with other bytes: the restore test must catch it.
docker run --rm --network "$network" --user 70 --entrypoint sh \
  -v "$PWD/secrets:/s:ro" "$image" -c '
  export RCLONE_CONFIG_X_TYPE=s3 RCLONE_CONFIG_X_PROVIDER=Other \
    RCLONE_CONFIG_X_ENDPOINT=http://offsite:8333 RCLONE_CONFIG_X_FORCE_PATH_STYLE=true \
    RCLONE_CONFIG_X_ACCESS_KEY_ID="$(cat /s/offsite_access_key_id)" \
    RCLONE_CONFIG_X_SECRET_ACCESS_KEY="$(cat /s/offsite_secret_access_key)"
  head -c 4096 /dev/urandom > /tmp/x && rclone copyto -q /tmp/x "x:baluarte-backups/baluarte/'"$(basename "$newest")"'"' \
  2>/dev/null || fail "could not replace the off-site copy"
if "${restore_test[@]}" restore-test > "$work/restore-test.log" 2>&1; then
  fail "restore-test passed with a tampered copy"
fi
row=$(psql_ "SELECT status, error FROM restore_run ORDER BY id DESC LIMIT 1")
[[ $row == failed\|*"was recorded"* ]] || fail "restore_run: $row"
echo "ok  a tampered off-site copy fails the restore test, recorded as failed"

# Rewrite an audit row's details behind the trigger's back, back up again: the restore test
# must find the digest that no longer matches.
docker exec "$db" psql -U baluarte -d baluarte -qX -v ON_ERROR_STOP=1 -c "
  ALTER TABLE audit_event DISABLE TRIGGER audit_event_immutable;
  UPDATE audit_event SET details = '{\"status\": \"failed\"}' WHERE action = 'gitlab.diagnostics_run';
  ALTER TABLE audit_event ENABLE TRIGGER audit_event_immutable;" >/dev/null
docker exec "$backup" baluarte-backup once >/dev/null 2>&1 || fail "the backup after the rewrite failed"
if "${restore_test[@]}" restore-test > "$work/restore-test.log" 2>&1; then
  fail "restore-test passed with a rewritten audit row"
fi
row=$(psql_ "SELECT status, audit_events, digest_mismatches, error FROM restore_run ORDER BY id DESC LIMIT 1")
[[ $row == "failed|$events|1|"*"digests do not match"* ]] || fail "restore_run: $row"
echo "ok  a rewritten audit row fails the restore test with its digest mismatch"

echo "Off-site backup and restore test passed"
