#!/usr/bin/env bash
# Checks off-site backup copies with the overlay (offsite.yaml): the backup service copies its
# encrypted file to the SeaweedFS container, records it, and the copy read back independently
# matches, decrypts with the offline key and holds a restorable dump. Then removes the stack
# (including volumes) and what it created.
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
"${compose[@]}" up -d --wait --wait-timeout 180 db migrate offsite backup

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

echo "Off-site backup test passed"
