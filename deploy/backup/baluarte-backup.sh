#!/bin/sh
# shellcheck shell=busybox
# Encrypted logical backups of the Baluarte database.
#
#   baluarte-backup loop     back up now, then every BALUARTE_BACKUP_INTERVAL_HOURS (Compose)
#   baluarte-backup once     back up once and exit (Kubernetes CronJob)
#   baluarte-backup health   exit 0 if the last backup succeeded within the interval + 2 hours
#
# pg_dump runs as a read-only role and streams straight into age, so no unencrypted dump is
# ever written. Only the age recipient (public key) is needed here; the private key that can
# decrypt backups stays offline. Every run, failed or not, is recorded in backup_run, which
# Baluarte reads to report on the backups control.
#
# Environment: PGHOST, PGPORT (5432), PGDATABASE (baluarte), PGSSLMODE,
# BALUARTE_BACKUP_DATABASE_USER (baluarte_backup), BALUARTE_BACKUP_DATABASE_PASSWORD_FILE,
# BALUARTE_BACKUP_RECIPIENT or BALUARTE_BACKUP_RECIPIENT_FILE, BALUARTE_BACKUP_DIR (/backups),
# BALUARTE_BACKUP_KEEP (14), BALUARTE_BACKUP_INTERVAL_HOURS (24).
set -eu -o pipefail

dir=${BALUARTE_BACKUP_DIR:-/backups}
keep=${BALUARTE_BACKUP_KEEP:-14}
interval_hours=${BALUARTE_BACKUP_INTERVAL_HOURS:-24}
stamp_file=/tmp/baluarte-backup.last-success
recipient=
export PGDATABASE="${PGDATABASE:-baluarte}" PGPORT="${PGPORT:-5432}"
export PGUSER="${BALUARTE_BACKUP_DATABASE_USER:-baluarte_backup}" PGAPPNAME=baluarte-backup

# JSON lines like the other services; quotes and backslashes are dropped from the message.
# shellcheck disable=SC1003
log() {
  printf '{"time":"%s","level":"%s","logger":"baluarte.backup","message":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$(printf '%s' "$2" | tr -d '"\\' | tr '\n\r\t' '   ')"
}

positive() { case $1 in '' | *[!0-9]* | 0) return 1 ;; *) return 0 ;; esac; }

setup() {
  positive "$keep" || { log ERROR "BALUARTE_BACKUP_KEEP must be a positive whole number"; exit 2; }
  positive "$interval_hours" || {
    log ERROR "BALUARTE_BACKUP_INTERVAL_HOURS must be a positive whole number"; exit 2; }
  password_file=${BALUARTE_BACKUP_DATABASE_PASSWORD_FILE:-}
  if [ -z "$password_file" ] || [ ! -r "$password_file" ]; then
    log ERROR "BALUARTE_BACKUP_DATABASE_PASSWORD_FILE is not set or not readable"
    exit 2
  fi
  # libpq reads the password from a private passfile, never from the environment.
  umask 077
  printf '*:*:*:%s:%s\n' "$PGUSER" "$(cat "$password_file")" > /tmp/pgpass
  export PGPASSFILE=/tmp/pgpass
  recipient=${BALUARTE_BACKUP_RECIPIENT:-}
  if [ -z "$recipient" ] && [ -n "${BALUARTE_BACKUP_RECIPIENT_FILE:-}" ]; then
    recipient=$(tr -d ' \r\n' < "$BALUARTE_BACKUP_RECIPIENT_FILE")
  fi
  case $recipient in
    age1*) ;;
    *) log ERROR "BALUARTE_BACKUP_RECIPIENT must be an age public key (age1...)"; exit 2 ;;
  esac
  if [ ! -d "$dir" ] || [ ! -w "$dir" ]; then
    log ERROR "$dir is not a writable directory"
    exit 2
  fi
}

# record STATUS STARTED FILE SIZE SHA256 ERROR: one backup_run row; psql quotes every value.
record() {
  psql -X -q -v ON_ERROR_STOP=1 \
    -v status="$1" -v started="$2" -v file="$3" -v size="$4" -v sha="$5" -v error="$6" \
    -v recipient="$recipient" -v host="$(hostname)" <<'SQL'
INSERT INTO backup_run
    (started_at, status, file_name, size_bytes, sha256, encrypted, recipient, error, host)
VALUES (:'started', :'status', NULLIF(:'file', ''), NULLIF(:'size', '')::bigint,
        NULLIF(:'sha', ''), :'status' = 'succeeded', :'recipient', NULLIF(:'error', ''), :'host');
SQL
}

backup() {
  started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  name="baluarte-$(date -u +%Y%m%dT%H%M%SZ).dump.age"
  partial="$dir/.$name.partial"
  if pg_dump --format=custom --no-password 2> /tmp/backup.err \
      | age --recipient "$recipient" --output "$partial" 2>> /tmp/backup.err; then
    mv "$partial" "$dir/$name"
    size=$(wc -c < "$dir/$name" | tr -d ' ')
    sha=$(sha256sum "$dir/$name" | cut -d' ' -f1)
    # The glob sorts oldest first (UTC timestamps in the names): remove all but the newest $keep.
    set -- "$dir"/baluarte-*.dump.age
    excess=$(($# - keep))
    for old in "$@"; do
      [ "$excess" -gt 0 ] || break
      rm -f "$old"
      excess=$((excess - 1))
    done
    if record succeeded "$started" "$name" "$size" "$sha" ""; then
      date +%s > "$stamp_file"
      log INFO "backup $name succeeded ($size bytes, sha256 $sha)"
      return 0
    fi
    log ERROR "backup $name was written but could not be recorded in backup_run"
    return 1
  fi
  rm -f "$partial"
  error=$(head -c 2000 /tmp/backup.err)
  log ERROR "backup failed: $error"
  record failed "$started" "" "" "" "${error:-pg_dump or age failed}" \
    || log ERROR "the failure could not be recorded in backup_run"
  return 1
}

health() {
  [ -r "$stamp_file" ] || exit 1
  [ $(($(date +%s) - $(cat "$stamp_file"))) -le $(((interval_hours + 2) * 3600)) ]
}

case ${1:-loop} in
  once)
    setup
    backup
    ;;
  loop)
    setup
    while :; do
      backup || true
      sleep $((interval_hours * 3600))
    done
    ;;
  health) health ;;
  *)
    echo "usage: baluarte-backup [loop|once|health]" >&2
    exit 2
    ;;
esac
