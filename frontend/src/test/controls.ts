import type {
  Control,
  ControlDetail,
  ControlList,
  MappingChange,
} from "../api/controls";

export const CONTROLS_SOC2 = "GET /api/v1/controls?framework=soc2";
export const PENDING_MAPPING = "GET /api/v1/control-mapping/changes?state=proposed";

const counts = { pass: 0, fail: 0, unknown: 0, excepted: 0, projects: 0 };

export function control(id: string, extra: Partial<Control> = {}): Control {
  return {
    framework: "soc2",
    id,
    title: "Changes are authorized, tested and approved",
    area: "Change management",
    status: "met",
    mapped: [
      {
        rule_type: "gitlab.branch.default_protected",
        source: "default",
        change_id: null,
        rationale: "Protected branches stop changes that skip review.",
      },
    ],
    change: null,
    counts: { ...counts, pass: 12, projects: 12 },
    ...extra,
  };
}

export function controlList(controls: Control[]): ControlList {
  return {
    mapping_version: "2026.11",
    frameworks: [
      { id: "soc2", name: "SOC 2" },
      { id: "iso27001", name: "ISO/IEC 27001:2022" },
      { id: "nist_800_53_moderate", name: "NIST SP 800-53 Rev. 5 Moderate" },
    ],
    controls,
  };
}

export function mappingChange(id: number, extra: Partial<MappingChange> = {}): MappingChange {
  return {
    id,
    framework: "soc2",
    control_id: "CC8.1",
    kind: "map",
    rule_type: "gitlab.project.merge_request_approvals",
    justification: "Project approval settings are how acme enforces peer review.",
    evidence_ref: null,
    review_by: null,
    supersedes_id: null,
    state: "proposed",
    proposed_by: { actor: "user:1", user_id: 1, display_name: "Ada Lovelace" },
    proposed_at: "2026-11-01T10:00:00Z",
    decided_by: null,
    decided_at: null,
    decision_note: null,
    self_approved: false,
    mapping_version: "2026.11",
    ...extra,
  };
}

export function controlDetail(extra: Partial<ControlDetail> = {}): ControlDetail {
  return {
    ...control("CC8.1", {
      status: "not_met",
      counts: { pass: 9, fail: 3, unknown: 1, excepted: 0, projects: 13 },
    }),
    rules: [
      {
        policy: "corporate-default",
        rule_id: "default-branch-protected",
        rule_type: "gitlab.branch.default_protected",
        counts: { pass: 9, fail: 3, unknown: 1, excepted: 0, projects: 13 },
      },
    ],
    changes: [],
    ...extra,
  };
}
