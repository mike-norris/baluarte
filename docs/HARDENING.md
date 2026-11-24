# Production hardening

A checklist for running Baluarte where its evidence will be relied on. The defaults below are
already in the images and the Helm chart. The checklist covers what only the operator can set.
The threat model and controls are in [SECURITY.md](SECURITY.md). Backups and restore are in
[OPERATIONS.md](OPERATIONS.md). The operating profile, which checks several of these items for
you, is in [COMPLIANCE.md](COMPLIANCE.md).

## Already the default

- **Images.** Pinned base images, OS updates applied at build, pip removed from the app
  image, and a non-root user. Each release is scanned before it is published and signed. Verify
  the signature before you deploy ([DEPLOYMENT.md](DEPLOYMENT.md#releases)) and pin images by
  version or digest.
- **Pods.** `runAsNonRoot`, the `RuntimeDefault` seccomp profile, a read-only root filesystem, no
  privilege escalation, all capabilities dropped, and no service account token mounted.
- **Network.** NetworkPolicies (`networkPolicy.enabled`, on by default) let only the web pod accept
  connections from outside the release. The API accepts them only from the web pod, the
  development database only from the pods that use it, and the worker, migration and backup pods
  from nothing. They need a CNI that enforces NetworkPolicy. Egress stays open because GitLab, the
  identity provider, the database, SMTP and webhooks are at addresses only you know. Add an egress
  policy for them if your cluster requires one.
- **Web server.**
  - Security headers: Content-Security-Policy, X-Frame-Options, Referrer-Policy, Permissions-Policy,
    Cross-Origin-Opener-Policy and Cross-Origin-Resource-Policy.
  - Strict-Transport-Security on responses that reached the proxy over HTTPS.
  - Requests are limited to 2 MB.
  - Sign-in and setup requests are rate limited per client address (5 a second, bursts of 20).
- **Sessions.** An opaque, server-side, `HttpOnly`, `SameSite` session cookie, `Secure` with the
  `__Host-` prefix when the public URL is HTTPS. Sessions end after 60 idle minutes and 8 hours
  at most (`BALUARTE_SESSION_IDLE_MINUTES`, `BALUARTE_SESSION_LIFETIME_MINUTES`).
- **Secrets.** Secrets are read only from files. A secret set directly in the environment is
  refused. Log lines are redacted, and secrets entered in the UI are sealed in the database.
- **Database roles.** The API and worker connect as a role that cannot change the schema or alter
  audit events ([DEPLOYMENT.md](DEPLOYMENT.md#database-roles)).

## Checklist

1. **Serve over HTTPS only.** Terminate TLS at the Ingress or a reverse proxy and set the public URL
   (`BALUARTE_PUBLIC_URL` or the setup wizard) to `https://…`. Cookies are marked Secure and HSTS is
   sent only then. The proxy must pass `X-Forwarded-Proto: https`; ingress-nginx, Traefik and the
   common cloud load balancers do.
2. **Do not expose the Compose stack as it ships.** It binds to 127.0.0.1 and keeps its secret
   files world-readable for convenience. On a server, prefer the Helm chart. If you must use
   Compose, use the TLS overlay with your own certificates ([DEPLOYMENT.md](DEPLOYMENT.md#tls)),
   which serves only HTTPS and keeps the API port (8000) closed, or put a TLS reverse proxy in
   front of the web port.
3. **Use an external PostgreSQL** with TLS (`sslmode=verify-full`, ideally with client
   certificates as in [DEPLOYMENT.md](DEPLOYMENT.md#tls)), backups and point-in-time recovery. The chart's
   `devDatabase` is for evaluation only. Give the migration user `CREATEROLE`, or create the app and
   backup roles yourself ([DEPLOYMENT.md](DEPLOYMENT.md#database-roles)).
4. **Keep the secret key with the backups.** `secrets/secret_key`, or the chart's generated
   Secret, seals the GitLab token and client secret. Store it in your secret manager. Without it a
   restored database needs them entered again.
5. **Least-privilege GitLab token.** Use a `read_api` token from a dedicated account rather than a
   person's, with the lowest role that reads what your baselines check. Some reads, such as project
   access tokens, need Maintainer. The capability report on the GitLab page shows what the token is
   refused, and those checks report unknown, never compliant. Only turn on label
   or issue writes (`BALUARTE_LABEL_WRITES`, `BALUARTE_ISSUE_WRITES`) with a separate write token,
   and only if you use those plans ([GITLAB_INTEGRATION.md](GITLAB_INTEGRATION.md)). Remediation
   writes (`BALUARTE_REMEDIATION_WRITES`) use a third token, which needs Owner on the projects it
   fixes. Limit it to those groups, give it an expiry, and turn the switch on only while you apply
   plans from Baluarte ([REMEDIATION.md](REMEDIATION.md#applying-a-plan)). Template writes
   (`BALUARTE_TEMPLATE_WRITES`) use a fourth token with the same care
   ([CONFIGURATION_TEMPLATES.md](CONFIGURATION_TEMPLATES.md#execution)).
6. **Map roles to dedicated identity provider groups**, and keep the admin group small. Review its
   membership at least quarterly; the operating profile's `access-review` attestation records it.
7. **Leave development mode off.** `BALUARTE_DEVELOPMENT_MODE=true` lets one person decide their
   own changes. The API logs a warning at startup while it is on.
8. **Turn on two-person review** ("Require a second person to approve baseline changes" in
   Settings), and choose an operating profile so the posture page shows what is not yet met
   ([COMPLIANCE.md](COMPLIANCE.md)).
9. **Ship the audit log** to your SIEM, over syslog with TLS or to Elasticsearch/OpenSearch, so
   that someone with database access cannot remove the record unseen
   ([COMPLIANCE.md](COMPLIANCE.md#audit-log-shipping)).
10. **Schedule encrypted backups** and test a restore ([OPERATIONS.md](OPERATIONS.md#scheduled-backups)).
11. **Restrict who can reach the web UI** with `networkPolicy.webFrom`, for example only the
    Ingress controller's namespace, and with your proxy's allow-lists or VPN where possible.
12. **Watch collection health.** Notifications for collection failures and the review reminders
    ([NOTIFICATIONS.md](NOTIFICATIONS.md)) keep stale data from being taken as current.
13. **Upgrade by release.** Read the CHANGELOG, back up, verify the new images' signatures, then
    `helm upgrade`. The migration Job runs before the new pods take traffic.
