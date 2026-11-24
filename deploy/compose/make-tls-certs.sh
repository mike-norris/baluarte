#!/bin/sh
# Creates a local certificate authority and the certificates the TLS overlay (tls.yaml) uses:
#
#   secrets/tls/ca.crt, ca.key                  the local CA (keep ca.key private)
#   secrets/tls/web.crt, web.key                nginx (localhost, 127.0.0.1)
#   secrets/tls/db.crt, db.key                  PostgreSQL (db, localhost)
#   secrets/tls/client-<role>.crt, .key         one client certificate per database role
#
# For local testing only: the certificates are self-signed by a CA made here. In production use
# certificates from your own CA (docs/DEPLOYMENT.md#tls). Existing files are kept; pass --force
# to make new ones. Run from the repository root:
#
#   deploy/compose/make-tls-certs.sh [--force]
#
# A client certificate's common name is the database role it signs in as, which PostgreSQL
# checks (clientcert=verify-full). Keys are created readable by the owner only. On a Linux host,
# give each key to the user of the container that reads it (PostgreSQL's own key is copied for
# it when the database starts):
#
#   sudo chown 101 secrets/tls/web.key
#   sudo chown 70:70 secrets/tls/client-baluarte_backup.key
#   sudo chown 10001 secrets/tls/client-baluarte.key secrets/tls/client-baluarte_app.key
set -eu

DIR=secrets/tls
DAYS=${BALUARTE_TLS_DAYS:-365}
ROLES="baluarte baluarte_app baluarte_backup"
FORCE=false
[ "${1:-}" = "--force" ] && FORCE=true

command -v openssl >/dev/null || { echo "openssl is required" >&2; exit 1; }
umask 077
mkdir -p "$DIR"

exists() { [ -f "$DIR/$1.crt" ] && [ -f "$DIR/$1.key" ] && [ "$FORCE" = false ]; }

key() { openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "$DIR/$1.key"; }

# sign NAME SUBJECT EXTENSIONS
sign() {
    openssl req -new -key "$DIR/$1.key" -subj "$2" -out "$DIR/$1.csr"
    printf '%s\n' "$3" > "$DIR/$1.ext"
    openssl x509 -req -in "$DIR/$1.csr" -CA "$DIR/ca.crt" -CAkey "$DIR/ca.key" \
        -CAcreateserial -days "$DAYS" -sha256 -extfile "$DIR/$1.ext" -out "$DIR/$1.crt" 2>/dev/null
    rm -f "$DIR/$1.csr" "$DIR/$1.ext"
    chmod 600 "$DIR/$1.key"
    chmod 644 "$DIR/$1.crt"
    echo "made $DIR/$1.crt"
}

if exists ca; then
    echo "kept $DIR/ca.crt"
else
    FORCE=true # a new CA needs every certificate signed again
    key ca
    openssl req -x509 -new -key "$DIR/ca.key" -sha256 -days "$DAYS" \
        -subj "/CN=Baluarte local CA" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" \
        -out "$DIR/ca.crt"
    chmod 644 "$DIR/ca.crt"
    echo "made $DIR/ca.crt"
fi

server() {
    exists "$1" && { echo "kept $DIR/$1.crt"; return; }
    key "$1"
    sign "$1" "/CN=$2" "basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth
subjectAltName=$3"
}

server web localhost "DNS:localhost,IP:127.0.0.1"
server db db "DNS:db,DNS:localhost,IP:127.0.0.1"

for role in $ROLES; do
    name="client-$role"
    exists "$name" && { echo "kept $DIR/$name.crt"; continue; }
    key "$name"
    sign "$name" "/CN=$role" "basicConstraints=CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=clientAuth"
done

rm -f "$DIR/ca.srl"
echo "Trust $DIR/ca.crt in your browser or system to open https://localhost:8443 without a warning."
