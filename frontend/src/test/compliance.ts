import type {
  Catalogue,
  ControlException,
  ControlPosture,
  Posture,
  Profile,
} from "../api/compliance";

export function profile(extra: Partial<Profile> = {}): Profile {
  return {
    frameworks: [],
    configured: false,
    settings: {
      audit_retention_days: 365,
      evidence_retention_days: 365,
      job_retention_days: 90,
      session_idle_minutes: 60,
      session_lifetime_minutes: 480,
      require_review: false,
      legal_hold: false,
      backup_interval_hours: 24,
    },
    sources: {
      audit_retention_days: "default",
      evidence_retention_days: "default",
      job_retention_days: "default",
      session_idle_minutes: "default",
      session_lifetime_minutes: "default",
      require_review: "default",
      legal_hold: "default",
      backup_interval_hours: "default",
    },
    minimums: {},
    updated_by: null,
    updated_at: null,
    ...extra,
  };
}

export const catalogue: Catalogue = {
  frameworks: [
    { id: "soc2", name: "SOC 2 Type II" },
    { id: "iso27001", name: "ISO/IEC 27001:2022" },
    { id: "nist_800_53_moderate", name: "NIST SP 800-53 Rev 5 Moderate" },
  ],
  controls: [
    {
      id: "session-idle",
      title: "Idle sessions end",
      area: "Access",
      kind: "setting",
      description: "Sessions end after a period of inactivity.",
      how_to: "Lower the inactivity limit in Settings.",
      mappings: {
        soc2: ["CC6.1"],
        iso27001: ["A.8.5"],
        nist_800_53_moderate: ["AC-11", "AC-12"],
      },
      setting: "session_idle_minutes",
      requirements: {
        soc2: { op: "<=", value: 30 },
        iso27001: { op: "<=", value: 30 },
        nist_800_53_moderate: { op: "<=", value: 15 },
      },
      attestation_days: null,
    },
    {
      id: "backups",
      title: "Backups are encrypted and tested",
      area: "Resilience",
      kind: "attestation",
      description: "Database backups are encrypted, kept off the host and restore-tested.",
      how_to: "Follow docs/OPERATIONS.md, then attest.",
      mappings: {
        soc2: ["A1.2"],
        iso27001: ["A.8.13"],
        nist_800_53_moderate: ["CP-9", "CP-4"],
      },
      setting: null,
      requirements: null,
      attestation_days: 90,
    },
    {
      id: "tls-web",
      title: "The web address uses TLS",
      area: "Encryption",
      kind: "check",
      description: "Browsers reach Baluarte over https.",
      how_to: "Put Baluarte behind a TLS-terminating proxy.",
      mappings: {
        soc2: ["CC6.7"],
        iso27001: ["A.5.14", "A.8.24"],
        nist_800_53_moderate: ["SC-8"],
      },
      setting: null,
      requirements: null,
      attestation_days: null,
    },
  ],
};

export function control(extra: Partial<ControlPosture> & { id: string }): ControlPosture {
  return {
    title: catalogue.controls.find((item) => item.id === extra.id)?.title ?? extra.id,
    status: "met",
    detail: "",
    evidence: {},
    exception: null,
    attestation: null,
    ...extra,
  };
}

export function posture(extra: Partial<Posture> = {}): Posture {
  return {
    evaluated_at: "2026-11-02T12:00:00Z",
    frameworks: ["soc2"],
    configured: true,
    summary: {
      met: 1,
      not_met: 1,
      needs_attestation: 1,
      excepted: 0,
      not_applicable: 0,
    },
    controls: [
      control({
        id: "session-idle",
        status: "met",
        detail: "Sessions end after 30 minutes idle.",
      }),
      control({
        id: "backups",
        status: "needs_attestation",
        detail: "Never attested.",
      }),
      control({
        id: "tls-web",
        status: "not_met",
        detail: "The public URL is http://127.0.0.1:8090.",
        evidence: { public_url: "http://127.0.0.1:8090" },
      }),
    ],
    ...extra,
  };
}

export function controlException(extra: Partial<ControlException> = {}): ControlException {
  return {
    id: 7,
    control_id: "tls-web",
    justification: "Local test stack only; TLS terminates at the proxy in production.",
    expires_on: "2026-12-31",
    state: "proposed",
    in_force: false,
    requested_by: "user:2",
    requested_at: "2026-11-02T10:00:00Z",
    decided_by: null,
    decided_at: null,
    self_approved: false,
    note: null,
    ...extra,
  };
}
