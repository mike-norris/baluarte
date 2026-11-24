#!/bin/sh
# shellcheck shell=busybox
# Encrypted logical backups of the Baluarte database.
#
#   baluarte-backup loop     back up now, then every BALUARTE_BACKUP_INTERVAL_HOURS (Compose)
#   baluarte-backup once     back up once and exit (Kubernetes CronJob)
#   baluarte-backup health   exit 0 if the last backup succeeded within the interval + 2 hours
#   baluarte-backup list     the recorded backups against the files stored locally and off site
#   baluarte-backup restore-test [FILE]
#                            restore a backup (the newest by default) into a temporary
#                            PostgreSQL inside this container, check it and record the result
#   baluarte-backup restore FILE [SHA256]
#                            restore a backup into an empty database (disaster recovery)
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
# Restores (docs/OPERATIONS.md#restore-tests) need the age private key, which is mounted only
# for that run at BALUARTE_RESTORE_IDENTITY_FILE (/run/secrets/restore_identity). The file must
# match its recorded SHA-256 before it is decrypted, and decryption streams into pg_restore, so
# no decrypted dump is written. restore-test takes the off-site copy when there is one, restores
# into a throwaway cluster in BALUARTE_RESTORE_DIR (/tmp/baluarte-restore), checks the schema
# revision, the tables, the audit_event triggers and every audit details digest, records a
# restore_run and deletes the cluster. restore restores into BALUARTE_RESTORE_TARGET (a libpq
# connection string; password in BALUARTE_RESTORE_TARGET_PASSWORD_FILE) only if it holds no
# tables, and works without the live database.
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
identity=${BALUARTE_RESTORE_IDENTITY_FILE:-/run/secrets/restore_identity}
work=${BALUARTE_RESTORE_DIR:-/tmp/baluarte-restore}
tools=/usr/local/lib/baluarte/backup_tools.py
tab=$(printf '\t')

# JSON lines like the other services; quotes and backslashes are dropped from the message.
# shellcheck disable=SC1003
log() {
  printf '{"time":"%s","level":"%s","logger":"baluarte.backup","message":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" "$(printf '%s' "$2" | tr -d '"\\' | tr '\n\r\t' '   ')"
}

positive() { case $1 in '' | *[!0-9]* | 0) return 1 ;; *) return 0 ;; esac; }

# The live database, as the backup role. libpq reads the password from a private passfile,
# never from the environment.
database_setup() {
  password_file=${BALUARTE_BACKUP_DATABASE_PASSWORD_FILE:-}
  if [ -z "$password_file" ] || [ ! -r "$password_file" ]; then
    log ERROR "BALUARTE_BACKUP_DATABASE_PASSWORD_FILE is not set or not readable"
    exit 2
  fi
  umask 077
  printf '*:*:*:%s:%s\n' "$PGUSER" "$(cat "$password_file")" > /tmp/pgpass
  export PGPASSFILE=/tmp/pgpass
}

setup() {
  positive "$keep" || { log ERROR "BALUARTE_BACKUP_KEEP must be a positive whole number"; exit 2; }
  positive "$interval_hours" || {
    log ERROR "BALUARTE_BACKUP_INTERVAL_HOURS must be a positive whole number"; exit 2; }
  database_setup
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

# Restores ------------------------------------------------------------------------------------

# fetch NAME SHA PREFER: put the backup at $file ($source offsite or local), trying PREFER
# first, then check its SHA-256: SHA, else the off-site metadata.
fetch() {
  source=''
  file=''
  if [ "$3" = offsite ] && [ -n "$s3_endpoint" ]; then
    order="offsite local"
  else
    order="local offsite"
  fi
  for place in $order; do
    case $place in
      local)
        if [ -r "$dir/$1" ]; then
          file="$dir/$1"
          source=local
          break
        fi
        ;;
      offsite)
        [ -n "$s3_endpoint" ] || continue
        : > /tmp/offsite.err
        if rclone_ copyto "offsite:$s3_bucket/$s3_prefix$1" "$work/$1"; then
          file="$work/$1"
          source=offsite
          break
        fi
        # A copy that should be there and cannot be fetched fails the test: no fallback.
        if [ "$3" = offsite ]; then
          error="could not fetch the off-site copy: $(head -c 1000 /tmp/offsite.err)"
          return 1
        fi
        ;;
    esac
  done
  if [ -z "$file" ]; then
    error="$1 is neither in $dir nor off site"
    return 1
  fi
  expected=$2
  if [ -z "$expected" ] && [ -n "$s3_endpoint" ]; then
    expected=$(rclone_ lsjson --metadata "offsite:$s3_bucket/$s3_prefix$1" \
      | sed -n 's/.*"sha256":"\([0-9a-f]*\)".*/\1/p') || expected=''
  fi
  if [ -z "$expected" ]; then
    error="no recorded SHA-256 for $1: pass it (from Compliance > Backups or an export)"
    return 1
  fi
  actual=$(sha256sum "$file" | cut -d' ' -f1)
  if [ "$actual" != "$expected" ]; then
    error="$1 from $source has SHA-256 $actual, but $expected was recorded"
    return 1
  fi
  return 0
}

# decrypt_into ARGS...: stream the decrypted dump into pg_restore with ARGS.
decrypt_into() {
  if ! age --decrypt --identity "$identity" < "$file" 2> /tmp/restore.err \
      | pg_restore --no-owner --no-privileges --exit-on-error "$@" 2>> /tmp/restore.err; then
    error="decrypt and restore failed: $(head -c 1500 /tmp/restore.err)"
    return 1
  fi
}

identity_check() {
  if [ ! -r "$identity" ]; then
    error="no age private key at $identity: mount it for this run"
    return 1
  fi
}

# The throwaway cluster: a Unix socket in $work only, no TCP, removed afterwards.
scratch() {
  PGHOST="$work" PGPORT=5432 PGUSER=restore PGDATABASE=restored PGSSLMODE=disable \
    PGPASSFILE="$work/no-passfile" "$@"
}

scratch_start() {
  initdb -D "$work/pg" -U restore -A trust -E UTF8 --no-sync > /tmp/initdb.log 2>&1 \
    || { error="initdb failed: $(tail -c 500 /tmp/initdb.log)"; return 1; }
  if ! pg_ctl -D "$work/pg" -w -l "$work/postgres.log" -o "-c listen_addresses='' \
      -c unix_socket_directories='$work' -c port=5432 -c fsync=off -c full_page_writes=off" \
      start > /dev/null 2>&1; then
    error="the temporary PostgreSQL did not start: $(tail -c 500 "$work/postgres.log")"
    return 1
  fi
  scratch createdb restored || { error="createdb failed"; return 1; }
}

scratch_stop() {
  if [ -d "$work/pg" ]; then
    pg_ctl -D "$work/pg" -m immediate stop > /dev/null 2>&1 || true
  fi
  rm -rf "$work"
}

# restore_check NAME: the restore test proper. Sets the restore_run fields, or error.
restore_check() {
  identity_check || return 1
  row=$(psql -AtX -v ON_ERROR_STOP=1 -v name="$1" <<'SQL'
SELECT id || '|' || file_name || '|' || sha256 || '|' || coalesce(offsite_status, '')
  FROM backup_run
 WHERE status = 'succeeded' AND (:'name' = '' OR file_name = :'name')
 ORDER BY finished_at DESC, id DESC LIMIT 1;
SQL
) || { error="could not read backup_run"; return 1; }
  if [ -z "$row" ]; then
    error="no successful backup${1:+ named $1} is recorded"
    return 1
  fi
  IFS='|' read -r backup_id file_name sha offsite_was <<ROW
$row
ROW
  prefer=local
  [ "$offsite_was" = copied ] && prefer=offsite
  fetch "$file_name" "$sha" "$prefer" || return 1
  scratch_start || return 1
  decrypt_into --dbname "host=$work port=5432 user=restore dbname=restored sslmode=disable" \
    || return 1
  revision=$(scratch psql -AtX -c "SELECT version_num FROM alembic_version" 2> /dev/null) \
    || { error="the restored database has no alembic_version"; return 1; }
  tables=$(scratch psql -AtX -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
  triggers=$(scratch psql -AtX -c "SELECT count(*) FROM pg_trigger
    WHERE tgrelid = 'public.audit_event'::regclass AND NOT tgisinternal AND tgenabled <> 'D'") \
    || triggers=0
  if [ "$triggers" -lt 1 ]; then
    error="audit_event was restored without its immutability triggers"
    return 1
  fi
  result=$(scratch psql -AtX -F "$tab" \
      -c "SELECT id, details_digest, details::text FROM audit_event ORDER BY id" \
    | python3 "$tools" digests) || { error="could not check the audit digests"; return 1; }
  read -r events mismatches mismatched <<RESULT
$result
RESULT
  if [ "$mismatches" -gt 0 ]; then
    error="$mismatches audit events' details digests do not match (ids $mismatched)"
    return 1
  fi
  return 0
}

record_restore() {
  psql -X -q -v ON_ERROR_STOP=1 \
    -v status="$1" -v started="$2" -v backup_id="${backup_id:-}" -v file="${file_name:-}" \
    -v sha="${sha:-}" -v source="${source:-}" -v revision="${revision:-}" \
    -v tables="${tables:-}" -v events="${events:-}" -v mismatches="${mismatches:-}" \
    -v error="${error:-}" -v host="$(hostname)" <<'SQL'
INSERT INTO restore_run
    (started_at, status, backup_run_id, file_name, sha256, source, schema_revision, table_count,
     audit_events, digest_mismatches, error, host)
VALUES (:'started', :'status', NULLIF(:'backup_id', '')::bigint, NULLIF(:'file', ''),
        NULLIF(:'sha', ''), NULLIF(:'source', ''), NULLIF(:'revision', ''),
        NULLIF(:'tables', '')::int, NULLIF(:'events', '')::bigint,
        NULLIF(:'mismatches', '')::bigint, NULLIF(:'error', ''), :'host');
SQL
}

restore_test() {
  database_setup
  [ -n "$s3_endpoint" ] && offsite_setup
  started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  error=''
  umask 077
  rm -rf "$work" && mkdir -p "$work"
  if restore_check "${1:-}"; then
    status=succeeded
  else
    status=failed
    error=${error:-restore test failed}
  fi
  scratch_stop
  if ! record_restore "$status" "$started"; then
    log ERROR "the restore test ($status) could not be recorded in restore_run"
    return 1
  fi
  if [ "$status" = succeeded ]; then
    log INFO "restore test of $file_name from $source succeeded: revision $revision, $tables tables, $events audit events, every digest matches"
    return 0
  fi
  log ERROR "restore test failed: $error"
  return 1
}

# restore NAME [SHA256]: disaster recovery into an empty target. The live database may be
# gone, so the expected SHA-256 is the argument, else backup_run if it answers, else the
# off-site metadata.
restore() {
  [ -n "${1:-}" ] || { echo "usage: baluarte-backup restore FILE [SHA256]" >&2; exit 2; }
  target=${BALUARTE_RESTORE_TARGET:-}
  [ -n "$target" ] || { log ERROR "set BALUARTE_RESTORE_TARGET to the empty database"; exit 2; }
  [ -n "$s3_endpoint" ] && offsite_setup
  umask 077
  rm -rf "$work" && mkdir -p "$work"
  error=''
  sha=${2:-}
  if [ -z "$sha" ] && [ -r "${BALUARTE_BACKUP_DATABASE_PASSWORD_FILE:-}" ]; then
    database_setup
    sha=$(PGCONNECT_TIMEOUT=5 psql -AtX -v name="$1" 2> /dev/null <<'SQL' || true
SELECT sha256 FROM backup_run WHERE status = 'succeeded' AND file_name = :'name' LIMIT 1;
SQL
)
  fi
  passfile=/tmp/pgpass.target
  : > "$passfile"
  if [ -r "${BALUARTE_RESTORE_TARGET_PASSWORD_FILE:-}" ]; then
    printf '*:*:*:*:%s\n' "$(cat "$BALUARTE_RESTORE_TARGET_PASSWORD_FILE")" > "$passfile"
  fi
  existing=$(PGPASSFILE=$passfile psql -AtX -d "$target" \
    -c "SELECT count(*) FROM pg_tables WHERE schemaname = 'public'") \
    || { log ERROR "cannot connect to BALUARTE_RESTORE_TARGET"; exit 1; }
  if [ "$existing" != 0 ]; then
    log ERROR "the target database has $existing tables: restore only into an empty database"
    exit 1
  fi
  if ! { identity_check && fetch "$1" "$sha" local \
      && PGPASSFILE=$passfile decrypt_into --dbname "$target"; }; then
    rm -rf "$work"
    log ERROR "restore failed: $error"
    exit 1
  fi
  rm -rf "$work"
  log INFO "restored $1 from $source with its SHA-256 checked. Next: run migrate so roles and grants are set, then revoke sessions (docs/OPERATIONS.md#verifying-a-restore)."
}

# list: the recorded backups against what is stored.
list_backups() {
  database_setup
  [ -n "$s3_endpoint" ] && offsite_setup
  umask 077
  psql -AtX -F '|' -v ON_ERROR_STOP=1 -c "SELECT file_name, size_bytes, sha256,
      to_char(finished_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS'),
      coalesce(offsite_status, '')
    FROM backup_run WHERE status = 'succeeded' ORDER BY finished_at DESC, id DESC LIMIT 100" \
    > /tmp/list.records
  : > /tmp/list.local
  for path in "$dir"/baluarte-*.dump.age; do
    [ -e "$path" ] || continue
    printf '%s|%s\n' "${path##*/}" "$(wc -c < "$path" | tr -d ' ')" >> /tmp/list.local
  done
  if [ -n "$s3_endpoint" ]; then
    : > /tmp/offsite.err
    rclone_ lsjson --metadata --files-only --include 'baluarte-*.dump.age' \
      "offsite:$s3_bucket/$s3_prefix" > /tmp/list.remote \
      || { log ERROR "could not list off site: $(head -c 500 /tmp/offsite.err)"; exit 1; }
    python3 "$tools" list /tmp/list.records /tmp/list.local /tmp/list.remote
  else
    python3 "$tools" list /tmp/list.records /tmp/list.local
  fi
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
  list) list_backups ;;
  restore-test) restore_test "${2:-}" ;;
  restore) restore "${2:-}" "${3:-}" ;;
  *)
    echo "usage: baluarte-backup [loop|once|health|list|restore-test [FILE]|restore FILE [SHA256]]" >&2
    exit 2
    ;;
esac
