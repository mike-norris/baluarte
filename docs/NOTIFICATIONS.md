# Notifications

Baluarte sends the few things people must act on to email and a chat webhook, once each, and records every delivery (FR-024). Notifications are **off by default**: nothing leaves Baluarte until an administrator turns on a channel under Settings → Notifications, so a deployment without them needs no outbound access.

## What is sent, and when

| Event | When | Content | Dedup key |
|---|---|---|---|
| New findings | After each evaluation run that opens findings at or above the chosen severity (default `high`) | Up to 10 findings (severity, project path, baseline and rule, link) and the total | `findings:<evaluation run id>` |
| Collection unhealthy | When GitLab collection becomes unhealthy (failing 3 runs in a row, or overdue), as the collection health check computes it ([OPERATIONS.md](OPERATIONS.md#monitoring-collection)) | Which kinds, how many failures, since when; a link to the dashboard | `collection:unhealthy:after-<previous notification id>` |
| Collection recovered | When it is healthy again | Since when it was unhealthy | `collection:recovered:<unhealthy notification id>` |
| Waiting for review | Once a day, from 08:00 UTC | Items in the review queue (the Reviews page, `GET /api/v1/reviews`) waiting longer than N days (default 2), counted by kind; approved rule and compliance exceptions and label, issue and remediation plans expiring within 7 days, counted by kind; links | `review:<UTC date>` |
| Weekly digest | Mondays, from 08:00 UTC | Rule results of the latest evaluation, open findings by severity, controls not met or awaiting attestation, the review queue by kind, what expires in the next 7 days | `digest:<ISO week>` |

Each event is switched on separately and sent to the channels chosen for it. An event sends nothing while it is off, or while none of its channels is on.

**Once each.** Every notification is recorded with its dedup key, which is unique in the database: however many workers notice an evaluation run, a health transition, a day or a week, only the first records it. The collection health keys name the notification before them, so unhealthy and recovered alternate and every worker agrees which is next. A day with nothing waiting for review is recorded without being sent, so it is checked once. If the worker is down at 08:00 the reminder goes out when it is back, the same day; a digest goes out on Monday or not at all for that week.

## Privacy

Messages carry no personal data: no names or email addresses of people, only counts, project paths, baseline and rule IDs, and links to Baluarte. Review items are counted by kind; who proposed them is never named. A project in a personal namespace has its owner's username in its path, so it is shown as "personal project <GitLab ID>" instead. Collection errors are not quoted (they can contain GitLab responses); the message says which kinds failed and links to the dashboard.

Links start with Baluarte's address (`public_url`, from the identity provider settings). When it is not configured, messages say so and leave the links out.

Recipient addresses are stored in the setting and shown to administrators only; audit events record how many recipients there are, not who.

## Channels

### Email (SMTP)

- **Host and port**: the mail server, e.g. port 587 for STARTTLS or 465 for implicit TLS.
- **Security**: `starttls` or `tls`. There is no plain-text option. With STARTTLS Baluarte refuses a server that does not offer it, before sending anything else.
- **Username and password** (optional): the password is sealed in `app_secret` and never shown again. It is sent only to the host and username it was entered for: change either and it must be entered again.
- **From** and **recipients** (at most 50; a team list works best). Messages are plain text with a simple HTML part, marked `Auto-Submitted: auto-generated`.
- **CA certificate** (optional): for a server whose certificate comes from a private CA. The certificate is always verified, against the system trust store or this CA, as for [audit log shipping](COMPLIANCE.md#audit-log-shipping).

### Chat webhook

One https URL that accepts Slack-style JSON, `{"text": "..."}`. Slack, Mattermost, Rocket.Chat and others accept it. The URL embeds its token, so it is sealed like a password and never shown; Settings shows only its host. Redirects are not followed and plain http is refused. An optional CA certificate works as for email.

### Setup

- **Slack**: create an app with Incoming Webhooks, add a webhook to the channel and paste its `https://hooks.slack.com/services/...` URL.
- **Mattermost**: Integrations → Incoming Webhooks → Add, choose the channel and paste the `https://<your-mattermost>/hooks/...` URL.
- **SMTP**: use a relay that accepts mail from the worker's address, with STARTTLS or TLS. A dedicated sender address and a team list as the recipient keep messages out of personal inboxes.

Use **Send test** on each channel before saving: it sends a test message with the values in the form (secrets left blank are the stored ones) and says which stage failed:

| Stage | Meaning |
|---|---|
| connect | The host could not be reached: DNS, firewall, port, or a timeout |
| TLS | The certificate is not trusted for that host name, or the server does not offer STARTTLS |
| authentication | The server refused the username or password (SMTP), or answered 401 or 403 (webhook) |
| rejected | The server refused the message: recipients, sender or content (SMTP), or another non-2xx answer (webhook) |
| config | The channel is incomplete, e.g. no webhook URL stored |

The reason never contains the password or the webhook URL.

## Delivery

Each notification has one delivery per channel. The worker's per-loop hook queues `notify.deliver` at most once a minute while a delivery is due or a scheduled message has not been composed yet. Each run composes the scheduled messages that are due, then sends due deliveries one at a time, each locked while it is sent so two workers never send the same one; a run sends at most 20 messages and stops after 50 seconds.

A failed attempt is retried after 1, 5, 15 and 60 minutes; the fifth failure marks the delivery `failed`. A delivery whose channel has been turned off fails at once. Errors are stored redacted, with the stage that failed.

Settings → Notifications shows the latest 50 deliveries; `GET /api/v1/notifications/deliveries` returns them to administrators and auditors (`compliance:read`). Notifications and their deliveries are kept for the audit retention period of the operating profile, then deleted by the retention job (a delivery still pending is kept).

## API

- `GET /api/v1/settings/notifications`, `PUT /api/v1/settings/notifications` and `POST /api/v1/settings/notifications/test`: need `settings:manage`. In an update a `null` password or webhook URL keeps the stored one and an empty string removes it. Validation errors name the field, e.g. `email.recipients: ...`. 409 when a secret is sent and no secret key is configured.
- `GET /api/v1/notifications/deliveries`: needs `settings:manage` or `compliance:read`.

Every settings change is audited as `notifications.settings_changed` (channels, hosts, number of recipients, events, and whether a secret was replaced or removed) and every test as `notifications.test_sent` (channel, outcome, stage).

## Deployment

No new environment variables. The worker needs outbound access to the SMTP server and webhook host only, and the secret key (`BALUARTE_SECRET_KEY_FILE`) mounted, as the API does; both are already the case in the Compose file and Helm chart. Data is in `notification` and `notification_delivery` ([DATA_MODEL.md](DATA_MODEL.md)).
