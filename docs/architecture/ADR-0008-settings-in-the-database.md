# ADR-0008: Settings and secrets entered in the web UI, with a one-time setup code

- Status: Accepted
- Date: 2026-10-30

## Context

Until now an administrator installed Baluarte by setting environment variables and writing secret files: the identity provider (`BALUARTE_OIDC_*`), the GitLab URL and the `read_api` token. Every change meant editing deployment configuration and restarting. Administrators asked to enter this in the web UI instead (BAL-034, BAL-035).

That raises three problems:
- **Bootstrapping.** Nobody can sign in before the identity provider is configured, so the first configuration step cannot be protected by sign-in.
- **Secrets at rest.** The GitLab token and the OIDC client secret would live in PostgreSQL, which is backed up, restored and read by more people than the secret mounts.
- **Two sources.** Deployments managed with Helm or GitOps must keep working, and must not have their settings silently overridden from the UI.

ADR-0001 and ADR-0005 rule out a separate secrets service.

## Decision

- **Bootstrap with a one-time setup code.**
  - While setup is incomplete, every API start generates a random 12-character code (about 59 bits) and logs it once at WARNING. `baluarte-setup-code` issues a new one on demand and ends every setup session.
  - Only the code's SHA-256 digest is stored. Five wrong codes within 15 minutes lock the endpoint until the window ends; the count is kept in the database, so all API replicas share it.
  - A correct code starts a setup session: a random cookie (`HttpOnly`, `SameSite=Strict`, `__Host-` prefixed and `Secure` over https) valid for 60 minutes, stored as a digest, plus a CSRF token required on every change. These are the ADR-0006 rules.
  - Reading the code requires access to the API's logs or the ability to run a command in its container, which is already enough to read its secrets. The code adds no new trust boundary.
- **Setup completes on the first admin sign-in.** The wizard saves the identity provider settings, then asks the administrator to sign in. The first sign-in that maps to the `admin` role through the settings entered in the wizard marks setup complete (audited as `setup.completed`), clears the code and ends every setup session. From then on the setup endpoints answer 409, for good, and settings change only on the admin Settings page (`settings:manage`). This proves the settings work before setup locks, so a typo cannot lock everyone out.
- **Secrets are sealed with AES-256-GCM.**
  - The key is 32 random bytes in a file named by `BALUARTE_SECRET_KEY_FILE`, mounted into the API and worker like the other secrets. Compose uses `secrets/secret_key`; the Helm chart generates a Secret on install and keeps it.
  - Each ciphertext stores a random 96-bit nonce and the ID of the key that sealed it. The secret's name is bound as associated data, so a ciphertext copied to another row does not decrypt.
  - Secrets are never returned by the API, never written to the audit trail (only "replaced" or "cleared" flags), and registered with the log redactor when written or read.
  - Without a key, settings with secrets cannot be saved (409); a public PKCE-only client can still be. With a different key, stored secrets cannot be read and Baluarte reports a configuration error rather than guessing.
  - Rotation: set the new key, put the old one in `BALUARTE_SECRET_KEY_PREVIOUS_FILE`, run `baluarte-reencrypt-secrets`, then remove the old key.
- **A stored secret only goes to the host it was entered for.** Changing the issuer or the GitLab URL requires entering the secret again, and testing a URL with the stored token is only allowed for the stored URL.
- **The deployment wins, per section.** If the environment configures sign-in at all (any of `BALUARTE_PUBLIC_URL`, `BALUARTE_OIDC_ISSUER`, `BALUARTE_OIDC_CLIENT_ID`), sign-in comes only from the environment, there is nothing to set up, and the UI shows those settings as managed by the deployment and read-only. The same applies to GitLab when `BALUARTE_GITLAB_URL` is set. Existing deployments are unaffected.
- **Changes apply without a restart.** Stored settings are cached for 10 seconds per process. The process that saves a change sees it at once; other API replicas and the worker within 10 seconds. The worker re-reads the collection schedule every minute.
- **Every change is audited** as `settings.oidc_updated`, `settings.gitlab_updated` or `settings.gitlab_disconnected`, with the actor (`setup` before anyone has signed in, otherwise `user:<id>`) and the non-secret values.

## Consequences

- An installation can be configured entirely from the browser after `docker compose up` or `helm install`, with one command to read the code.
- One more secret to manage and back up: the key. Losing it means re-entering the GitLab token and client secret; nothing else is lost. A database backup without the key does not expose them.
- The setup and test endpoints make the API send `GET` requests to URLs a setup-code holder or an admin enters (discovery documents and GitLab diagnostics). Redirects are never followed and only https is allowed.
- Anyone who obtains the setup code before setup completes can configure an identity provider they control. Complete setup promptly after installing, keep API logs private, and issue a new code with `baluarte-setup-code` if one may have leaked.
- Roles still come from identity provider groups and are fixed per session (ADR-0006). Changing the group mapping affects new sign-ins only.

## Revisit when

A dedicated secrets manager (for example Vault or a cloud KMS) is in scope, more than one identity provider or GitLab instance is supported, or roles are assigned inside Baluarte.
