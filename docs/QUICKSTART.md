# Quickstart

From nothing to your first findings and an evidence export in about fifteen minutes, on one machine,
using the released images. This setup is for evaluation: it listens only on 127.0.0.1. For a
server, use the [Helm chart](../deploy/helm/baluarte/README.md) and [HARDENING.md](HARDENING.md).

## You need

- Docker with Compose v2, and `git` and `openssl`.
- An OpenID Connect provider where you can register a client: Keycloak, Microsoft Entra ID, Okta,
  Authentik, or GitLab itself (Admin → Applications, or a group's Applications, with the scopes
  `openid`, `profile` and `email`). Put yourself in a group that will be Baluarte's admin group.
- A GitLab personal or service-account access token with the `read_api` scope, from an account
  that can see the groups you want to check. Baluarte only reads.

## 1. Start Baluarte

```sh
git clone --depth 1 --branch v0.1.1 https://github.com/mike-norris/baluarte.git
cd baluarte
mkdir -p secrets && (umask 022 && for name in postgres_password app_database_password backup_database_password secret_key; do
  [ -f secrets/$name ] || openssl rand -base64 32 > secrets/$name; done)
export BALUARTE_IMAGE_PREFIX=ghcr.io/mike-norris/ BALUARTE_VERSION=0.1.1
docker compose pull
docker compose up -d --no-build --wait
docker compose logs api | grep 'setup code'
```

`BALUARTE_IMAGE_PREFIX` and `BALUARTE_VERSION` make Compose use the released images instead of
building them. To check their signatures first, see [DEPLOYMENT.md](DEPLOYMENT.md#releases).
Keep `secrets/secret_key`: it seals the credentials you enter next.

## 2. Register Baluarte with your identity provider

Create a confidential client (authorization code flow) with this redirect URI:

```text
http://127.0.0.1:8080/api/v1/auth/callback
```

Note its issuer URL, client ID and client secret. Baluarte reads roles from the `groups` claim,
from the ID token or else from userinfo, and you map group names to roles in the next step. With
GitLab as the provider, the group names are group paths, such as `platform/security`.

## 3. Run the setup wizard

Open <http://127.0.0.1:8080> and enter the setup code from step 1. The wizard has four steps:

1. **Identity provider.** Enter the issuer, client ID and secret, and the group that makes someone
   an admin (**Admin groups**). The analyst, auditor and manager groups are optional. **Test
   discovery** checks the issuer before you save.
2. **GitLab.** Enter the GitLab URL (https, without `/api/v4`) and the token, then **Test
   connection**. This step can be skipped and done later under Settings.
3. **Compliance.** Choose the frameworks Baluarte itself is run to, or none for now.
4. **Review and finish.** Sign in. Setup completes when the first member of the admin group signs in.

## 4. Watch the first collection

Baluarte starts collecting within a minute of saving the GitLab connection, and then every six
hours (`BALUARTE_GITLAB_SYNC_INTERVAL_MINUTES`). Open **Inventory** to see groups and projects
arrive. The header shows how fresh the data is. A large instance takes a few minutes for its first
pass.

## 5. Apply the recommended baseline

1. Open **Baselines** and choose **Use the recommended baseline**. It switches on every rule your
   GitLab edition supports. The rules on Ultimate-only features are left out on other editions.
2. Review the rules, then **Save and apply** to the whole instance.

Two-person review is off on a new installation, so the baseline applies at once. Once you turn it
on (Settings) or a compliance profile requires it, a second person approves changes in the
**Review queue**.

Evaluation runs straight away, and again after every collection.

## 6. Read the results

- **Dashboard.** Posture across the instance, and how fresh it is.
- **Findings.** Each failing check with the expected and actual values, and the evidence observed.
  An unknown result, for example a setting the token is not allowed to read, is shown as unknown,
  never as passing.
- **Controls.** The same results mapped to SOC 2, ISO/IEC 27001 and NIST SP 800-53.

## 7. Export evidence

Open **Evidence exports**. Export a scope as JSON or as a ZIP of CSV files, or build an
audit-period package with the auditor report as a PDF. Each export carries an integrity manifest.
To verify one offline, with only Python 3.9 or later, run the verifier from the clone:

```sh
python3 backend/src/baluarte/evidence/verify.py export.zip
```

Add `--manifest-sha256` with the value recorded in the **Audit log** for that export to also check
that the manifest is the one Baluarte produced ([AUDIT_EVIDENCE.md](AUDIT_EVIDENCE.md)).

## Next steps

- Turn on two-person review and choose an operating profile: [COMPLIANCE.md](COMPLIANCE.md).
- Notifications for new findings and collection problems: [NOTIFICATIONS.md](NOTIFICATIONS.md).
- Leave projects out of monitoring, or grant exceptions: [MONITORING_SCOPE.md](MONITORING_SCOPE.md).
- Backups: [OPERATIONS.md](OPERATIONS.md).
- Run it for real: [Helm chart](../deploy/helm/baluarte/README.md) and [HARDENING.md](HARDENING.md).

To stop Baluarte, run `docker compose down`. Add `-v` to also delete its database.
