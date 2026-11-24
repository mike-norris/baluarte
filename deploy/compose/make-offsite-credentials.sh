#!/bin/sh
# Creates the S3 credentials the off-site overlay (offsite.yaml) uses, for local testing:
#
#   secrets/offsite_access_key_id, secrets/offsite_secret_access_key
#       what the backup service signs in with
#   secrets/seaweedfs_s3.json
#       the SeaweedFS identity with those keys, allowed only the backup bucket
#
# Existing files are kept; pass --force to make new ones. Run from the repository root:
#
#   deploy/compose/make-offsite-credentials.sh [--force]
#
# With a real S3-compatible target, put the keys it issues in the first two files instead and
# skip the SeaweedFS file (docs/OPERATIONS.md#off-site-copies).
set -eu

DIR=secrets
BUCKET=${BALUARTE_BACKUP_S3_BUCKET:-baluarte-backups}
case $BUCKET in '' | *[!a-z0-9.-]*) echo "bucket names are lower-case letters, digits, . and -" >&2; exit 2 ;; esac

if [ "${1:-}" != "--force" ] && [ -f "$DIR/offsite_access_key_id" ] &&
  [ -f "$DIR/offsite_secret_access_key" ] && [ -f "$DIR/seaweedfs_s3.json" ]; then
  echo "kept $DIR/offsite_access_key_id, offsite_secret_access_key and seaweedfs_s3.json"
  exit 0
fi

command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }
mkdir -p "$DIR"
# Readable by the containers' non-root users on Linux bind mounts, like the other dev secrets.
umask 022
key=$(openssl rand -hex 10 | tr a-f A-F)
secret=$(openssl rand -base64 30 | tr -d '/+=\n')
printf '%s\n' "$key" > "$DIR/offsite_access_key_id"
printf '%s\n' "$secret" > "$DIR/offsite_secret_access_key"
cat > "$DIR/seaweedfs_s3.json" <<EOF
{
  "identities": [
    {
      "name": "baluarte-backup",
      "credentials": [{ "accessKey": "$key", "secretKey": "$secret" }],
      "actions": ["Admin:$BUCKET", "Read:$BUCKET", "Write:$BUCKET", "List:$BUCKET", "Tagging:$BUCKET"]
    }
  ]
}
EOF
echo "made $DIR/offsite_access_key_id, offsite_secret_access_key and seaweedfs_s3.json"
