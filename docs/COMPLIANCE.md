# Operating profile

Baluarte keeps audit evidence, so it is itself in scope when an auditor examines how that evidence was produced. The **operating profile** says which frameworks Baluarte itself is run to and checks Baluarte against them. It is separate from the baselines Baluarte applies to the GitLab instance it monitors ([POLICY_ENGINE.md](POLICY_ENGINE.md)).

Choose the profile in the setup wizard (setup cannot complete without it) and change it later in Settings > Compliance. Any combination of these can be selected:

| ID | Framework |
|---|---|
| `soc2` | SOC 2 Type II (AICPA Trust Services Criteria 2017, revised points of focus 2022) |
| `iso27001` | ISO/IEC 27001:2022, Annex A |
| `nist_800_53_moderate` | NIST SP 800-53 Rev. 5, Moderate baseline |

With no framework selected, the defaults below apply, the posture is shown for information, every control is `not_applicable` and nothing is enforced.

> These are Baluarte's minimums, chosen so that a typical audit of each framework can be supported from Baluarte's own records. They are not a certification and do not replace the organization's control set. The organization's compliance owner may require stricter values (for example longer retention for federal systems under OMB M-21-31, or a shorter idle timeout) and should review the control mapping below against their own.

## Operating settings

| Setting | Default | Bounds | SOC 2 | ISO 27001 | NIST 800-53 Moderate | Environment override |
|---|---|---|---|---|---|---|
| `audit_retention_days` | 365 | 30 to 3650 | >= 365 | >= 365 | >= 365 | |
| `evidence_retention_days` | 365 | 30 to 3650 | >= 365 | >= 365 | >= 365 | |
| `job_retention_days` | 90 | 7 to 3650 | | | | |
| `session_idle_minutes` | 60 | 5 to 1440 | <= 30 | <= 30 | <= 15 | `BALUARTE_SESSION_IDLE_MINUTES` |
| `session_lifetime_minutes` | 480 | 15 to 10080, >= idle | <= 720 | <= 720 | <= 720 | `BALUARTE_SESSION_LIFETIME_MINUTES` |
| `require_review` | false | | true | true | true | |
| `legal_hold` | false | | | | | |

When several frameworks are selected the strictest value wins, and the API names the framework that sets it. Why these values:

- **Retention, 365 days.** A SOC 2 Type II report covers an observation period of up to 12 months, and the auditor samples events from all of it; ISO 27001 surveillance audits are annual; NIST AU-11 leaves the period to the organization, and one year is the common floor. Evidence (observations, evaluations, runs) follows the same period so every finding in the period can be traced to what was observed. Jobs are operational records and have no framework minimum.
- **Idle timeout, 30 or 15 minutes.** SOC 2 CC6.1 and ISO A.8.5 expect sessions to end after inactivity without fixing a value; 30 minutes is common practice. NIST AC-11 and AC-12 at Moderate are usually set to 15 minutes.
- **Session lifetime, 12 hours.** A working day plus margin, so a stolen session cookie cannot be used indefinitely (AC-12).
- **Two-person review.** SOC 2 CC8.1, ISO A.8.32 and NIST CM-3 expect changes to be authorized by someone other than their author. Baseline changes decide what Baluarte reports as compliant, so they are changes in scope.

`require_review` is the same setting as "Require a second person to approve baseline changes" (`/api/v1/settings/governance`), which refuses to turn it off while the profile requires it. A session limit set in the environment wins over the profile and cannot be changed in the UI; if it misses the minimum the posture shows the control as not met.

## Enforcement and exceptions

A value that misses the minimum is refused (409 `below_minimum`, naming each control, its required value and the frameworks that set it) unless a **compliance exception** is in force for that control. An exception has a justification (10 to 2000 characters) and an expiry date at most 365 days away; it is in force while approved and before that date (UTC). While two-person review is on, someone other than the requester must approve it; otherwise it is approved at once and recorded as self-approved. An exception can be revoked at any time. When an exception ends, the setting stays as it was and the posture shows the control as not met again, so the gap is visible rather than silently reverted.

Exceptions can name any control, so a known gap (for example a database without TLS on a single host) can be recorded as accepted risk; the posture then shows it `excepted` instead of `not_met`. Exceptions are never deleted.

## Controls

Each control is a **setting** (met when its setting meets the requirement), a **check** (met when Baluarte observes it) or an **attestation** (met while an administrator's statement is current, for what Baluarte cannot see).

| Control | Kind | SOC 2 | ISO 27001 | NIST 800-53 | Met when |
|---|---|---|---|---|---|
| `audit-retention` | setting | CC7.2 | A.8.15 | AU-11 | `audit_retention_days` meets the minimum |
| `evidence-retention` | setting | CC4.1 | A.5.33 | AU-11, SI-12 | `evidence_retention_days` meets the minimum |
| `session-idle` | setting | CC6.1 | A.8.5 | AC-11, AC-12 | `session_idle_minutes` meets the maximum |
| `session-lifetime` | setting | CC6.1 | A.8.5 | AC-12 | `session_lifetime_minutes` meets the maximum |
| `change-review` | setting | CC8.1 | A.8.32 | CM-3 | `require_review` is on |
| `audit-integrity` | check | CC7.2 | A.8.15 | AU-9 | Every audit event matches its digest (rechecked at most every 5 minutes per process) |
| `audit-protection` | check | CC7.2 | A.8.15 | AU-9 | The application's database role is not a superuser, does not own `audit_event`, and has no `UPDATE`, `DELETE` or `TRUNCATE` on it ([DEPLOYMENT.md](DEPLOYMENT.md#database-roles)) |
| `secrets-sealed` | check | CC6.1 | A.8.24 | SC-12, SC-28 | A secret key is configured |
| `tls-web` | check | CC6.7 | A.5.14, A.8.24 | SC-8 | The public URL is https |
| `tls-database` | check | CC6.7 | A.5.14, A.8.24 | SC-8 | The database connection is encrypted and `sslmode` is `require`, `verify-ca` or `verify-full` |
| `tls-gitlab` | check | CC6.7 | A.5.14, A.8.24 | SC-8 | The GitLab URL is https |
| `retention-running` | check | CC7.2 | A.5.33 | SI-12 | The retention job succeeded in the last 48 hours |
| `collection-health` | check | CC7.1 | A.8.16 | CA-7 | GitLab is configured and there is no collection alert |
| `inventory-population` | check | CC4.1 | A.8.16 | CA-7, CM-8 | The inventory covers the whole instance |
| `log-shipping` | attestation (90 days) | CC7.2 | A.8.15 | AU-4, AU-9 | Logs, including the `baluarte.audit` stream, are kept outside Baluarte |
| `backups` | attestation (90 days) | A1.2 | A.8.13 | CP-9, CP-4 | Backups are encrypted, off the database host, and a restore was tested in the last 12 months |
| `encryption-at-rest` | attestation (365 days) | CC6.1 | A.8.24 | SC-28 | Database and backup storage is encrypted |
| `identity-mfa` | attestation (365 days) | CC6.1 | A.8.5 | IA-2(1), IA-2(2) | The identity provider requires MFA for everyone who can sign in |
| `access-review` | attestation (90 days) | CC6.2, CC6.3 | A.5.18 | AC-2 | Membership of the groups mapped to Baluarte roles was reviewed |

A check that cannot run (a database error, a setting that cannot be read) is `not_met` and says why; it is never reported as met. The catalogue, with a how-to for each control, is served by `GET /api/v1/compliance/catalogue`.

## Statuses

| Status | Meaning |
|---|---|
| `met` | The control is in place |
| `not_met` | It is not, or the check could not run |
| `needs_attestation` | Nobody has attested to it, or the last attestation expired |
| `excepted` | Not met, but an approved exception is in force |
| `not_applicable` | No framework is selected; the control's own result is in `evidence.result` |

## API

Reading needs `compliance:read` (admin and auditor); every change needs `settings:manage` and is audited.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/compliance/catalogue` | Frameworks and controls, with mappings, requirements and how-tos |
| `GET`, `PUT /api/v1/compliance/profile` | The profile in force, with each setting's source (`deployment`, `database` or `default`) and the minimums. `PUT` answers 409 `below_minimum` or `deployment_setting`. Audited as `settings.compliance_updated` |
| `GET /api/v1/compliance/posture` | One result per control and a summary by status |
| `GET`, `POST /api/v1/compliance/exceptions`, `POST .../{id}/approve`, `.../reject`, `.../revoke` | Compliance exceptions. A rejection needs a note; the requester cannot decide their own (409 `not_allowed`). Audited as `compliance.exception_*` |
| `GET`, `POST /api/v1/compliance/attestations` | Attestations; only attestation controls take one. Audited as `compliance.attested` |
| `GET`, `PUT /api/v1/setup/compliance` | The same profile during first-run setup, with a setup session |

The evidence manifest states the profile and posture at export time (`operations`, [AUDIT_EVIDENCE.md](AUDIT_EVIDENCE.md#manifest-and-verification)). Retention is described in [OPERATIONS.md](OPERATIONS.md#retention).

## Not yet

Shipping logs and running backups from Baluarte's own services, with checks that replace the `log-shipping` and `backups` attestations, and an access-review report, are planned for a later release.

## Limits

The profile is enforced by the application, and the application's database role also stores the profile. Someone who controls that role, for example through a compromised API process, could lower a setting, or call `baluarte_expire_audit` directly to remove audit events older than 30 days. The database refuses anything younger. A run by the scheduled job is itself audited; a direct call is not. The control against this is an independent copy of the `baluarte.audit` log stream, kept outside Baluarte and its database, which is what the `log-shipping` control asks you to attest.
