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
# Off-site copies (docs/OPERATIONS.md#off-site-copies): when BALUARTE_BACKUP_S3_ENDPOINT is set,
# each new encrypted file is also copied with rclone to an S3-compatible bucket, then read back
# (size and SHA-256, kept as object metadata) before the run counts as copied. Only the
# encrypted file leaves this host. Copies older than the newest BALUARTE_BACKUP_S3_KEEP are
# deleted; 0 never deletes, for a target with versioning or object lock that the backup
# credentials cannot delete from.
#
# Environment: PGHOST, PGPORT (5432), PGDATABASE (baluarte), PGSSLMODE,
# BALUARTE_BACKUP_DATABASE_USER (baluarte_backup), BALUARTE_BACKUP_DATABASE_PASSWORD_FILE,
# BALUARTE_BACKUP_RECIPIENT or BALUARTE_BACKUP_RECIPIENT_FILE, BALUARTE_BACKUP_DIR (/backups),
# BALUARTE_BACKUP_KEEP (14), BALUARTE_BACKUP_INTERVAL_HOURS (24); off-site:
# BALUARTE_BACKUP_S3_ENDPOINT (https://..., or http:// on a private network), _BUCKET, _PREFIX
# (baluarte/), _REGION (us-east-1), _ACCESS_KEY_ID_FILE, _SECRET_ACCESS_KEY_FILE, _KEEP (the
# local keep).
set -eu -o pipefail

dir=${BALUARTE_BACKUP_DIR:-/backups}
keep=${BALUARTE_BACKUP_KEEP:-14}
interval_hours=${BALUARTE_BACKUP_INTERVAL_HOURS:-24}
stamp_file=/tmp/baluarte-backup.last-success
recipient=
s3_endpoint=${BALUARTE_BACKUP_S3_ENDPOINT:-}
s3_bucket=${BALUARTE_BACKUP_S3_BUCKET:-}
s3_prefix=${BALUARTE_BACKUP_S3_PREFIX:-baluarte/}
s3_keep=${BALUARTE_BACKUP_S3_KEEP:-$keep}
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
  [ -n "$s3_endpoint" ] && offsite_setup
  return 0
}

# The rclone remote "offsite", in a private config file like the passfile above.
offsite_setup() {
  case $s3_endpoint in
    https://* | http://*) ;;
    *) log ERROR "BALUARTE_BACKUP_S3_ENDPOINT must be an http(s) URL"; exit 2 ;;
  esac
  case $s3_bucket in
    '' | */* | *' '*) log ERROR "BALUARTE_BACKUP_S3_BUCKET must be a bucket name"; exit 2 ;;
  esac
  case $s3_keep in '' | *[!0-9]*)
    log ERROR "BALUARTE_BACKUP_S3_KEEP must be a whole number (0 never deletes)"; exit 2 ;;
  esac
  case $s3_prefix in '' | */) ;; *) s3_prefix="$s3_prefix/" ;; esac
  for name in ACCESS_KEY_ID SECRET_ACCESS_KEY; do
    eval "file=\${BALUARTE_BACKUP_S3_${name}_FILE:-}"
    if [ -z "$file" ] || [ ! -r "$file" ]; then
      log ERROR "BALUARTE_BACKUP_S3_${name}_FILE is not set or not readable"
      exit 2
    fi
  done
  {
    echo "[offsite]"
    echo "type = s3"
    echo "provider = Other"
    echo "endpoint = $s3_endpoint"
    echo "region = ${BALUARTE_BACKUP_S3_REGION:-us-east-1}"
    echo "access_key_id = $(tr -d ' \r\n' < "$BALUARTE_BACKUP_S3_ACCESS_KEY_ID_FILE")"
    echo "secret_access_key = $(tr -d ' \r\n' < "$BALUARTE_BACKUP_S3_SECRET_ACCESS_KEY_FILE")"
    echo "force_path_style = true"
  } > /tmp/rclone.conf
  export RCLONE_CONFIG=/tmp/rclone.conf
  # Where copies go, for logs and backup_run: never the credentials.
  host=$(printf '%s' "$s3_endpoint" | sed -E 's#^[a-z]+://([^/@]*@)?([^/]+).*#\2#')
}

rclone_() { rclone --config /tmp/rclone.conf --retries 3 --low-level-retries 5 "$@" 2>> /tmp/offsite.err; }

# offsite NAME SIZE SHA: copy one file, read it back, then trim older copies. Sets
# offsite_status, offsite_location and offsite_error.
offsite() {
  : > /tmp/offsite.err
  key="$s3_prefix$1"
  offsite_location="s3://$s3_bucket/$key (at $host)"
  offsite_status=failed
  if ! rclone_ copyto "$dir/$1" "offsite:$s3_bucket/$key" \
      --metadata --metadata-set "sha256=$3"; then
    offsite_error="copy failed: $(head -c 1500 /tmp/offsite.err)"
    return 1
  fi
  # Read back: the stored size and the checksum written with it must match the local file.
  listed=$(rclone_ lsjson --metadata "offsite:$s3_bucket/$key") || listed=
  stored_size=$(printf '%s' "$listed" | sed -n 's/.*"Size":\([0-9]*\).*/\1/p')
  stored_sha=$(printf '%s' "$listed" | sed -n 's/.*"sha256":"\([0-9a-f]*\)".*/\1/p')
  if [ "$stored_size" != "$2" ] || [ "$stored_sha" != "$3" ]; then
    offsite_error="read back did not match (size ${stored_size:-none}, sha256 ${stored_sha:-none})"
    return 1
  fi
  offsite_status=copied
  offsite_error=
  if [ "$s3_keep" -gt 0 ]; then
    # Names sort oldest first (UTC timestamps): delete all but the newest $s3_keep.
    rclone_ lsf --files-only --include 'baluarte-*.dump.age' "offsite:$s3_bucket/$s3_prefix" \
      | sort > /tmp/offsite.list || true
    excess=$(($(wc -l < /tmp/offsite.list) - s3_keep))
    while [ "$excess" -gt 0 ] && read -r old; do
      rclone_ deletefile "offsite:$s3_bucket/$s3_prefix$old" \
        || log WARNING "could not delete old off-site copy $old"
      excess=$((excess - 1))
    done < /tmp/offsite.list
  fi
  return 0
}

# record STATUS STARTED FILE SIZE SHA256 ERROR: one backup_run row, with the off-site outcome
# when there is one; psql quotes every value.
record() {
  psql -X -q -v ON_ERROR_STOP=1 \
    -v status="$1" -v started="$2" -v file="$3" -v size="$4" -v sha="$5" -v error="$6" \
    -v recipient="$recipient" -v host="$(hostname)" \
    -v offsite_status="${offsite_status:-}" -v offsite_location="${offsite_location:-}" \
    -v offsite_error="${offsite_error:-}" <<'SQL'
INSERT INTO backup_run
    (started_at, status, file_name, size_bytes, sha256, encrypted, recipient, error, host,
     offsite_status, offsite_location, offsite_error)
VALUES (:'started', :'status', NULLIF(:'file', ''), NULLIF(:'size', '')::bigint,
        NULLIF(:'sha', ''), :'status' = 'succeeded', :'recipient', NULLIF(:'error', ''), :'host',
        NULLIF(:'offsite_status', ''), NULLIF(:'offsite_location', ''),
        NULLIF(:'offsite_error', ''));
SQL
}

backup() {
  offsite_status=''
  offsite_location=''
  offsite_error=''
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
    copied=true
    if [ -n "$s3_endpoint" ]; then
      if offsite "$name" "$size" "$sha"; then
        log INFO "backup $name copied off site to $offsite_location"
      else
        copied=false
        log ERROR "backup $name was not copied off site: $offsite_error"
      fi
    fi
    if record succeeded "$started" "$name" "$size" "$sha" ""; then
      log INFO "backup $name succeeded ($size bytes, sha256 $sha)"
      # Healthy only when the off-site copy, if configured, succeeded too.
      if [ "$copied" = true ]; then
        date +%s > "$stamp_file"
        return 0
      fi
      return 1
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
