# Scheduled, encrypted database backups for Baluarte (docs/OPERATIONS.md#scheduled-backups).
# pg_dump must match the server's major version; the base pins PostgreSQL 17 on Alpine 3.24,
# whose age package is pinned below.
FROM postgres:17-alpine3.24

RUN apk add --no-cache age=1.3.2-r1

COPY --chmod=0555 baluarte-backup.sh /usr/local/bin/baluarte-backup

# The image's postgres user (uid 70). Writes only to /backups and /tmp.
USER 70:70
WORKDIR /tmp
ENTRYPOINT ["/usr/local/bin/baluarte-backup"]
CMD ["loop"]
