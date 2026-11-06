import type {
  DriftItem,
  DriftPage,
  LabelPlan,
  LabelPlanStep,
  OwnerLabels,
  TaxonomyVersion,
  TaxonomyView,
  WriteTokenStatus,
} from "../api/labels";

export const TAXONOMY = "GET /api/v1/labels/taxonomy";
export const DRIFT = "GET /api/v1/labels/drift?limit=50";
export const PLANS = "GET /api/v1/labels/plans?limit=50";
export const WRITE_TOKEN = "GET /api/v1/settings/label-write-token";

const ada = { actor: "user:1", user_id: 1, display_name: "Ada Lovelace" };
const grace = { actor: "user:2", user_id: 2, display_name: "Grace Hopper" };

export const TAXONOMY_YAML = `apiVersion: baluarte.io/v1alpha1
kind: LabelTaxonomy
metadata:
  name: "acme-labels"
spec:
  scope:
    groups: ["acme"]
  placement: top_level_group
  strict: false
  labels:
    - name: "priority::high"
      color: "#d9534f"
      description: "Fix this sprint"
      aliases: ["P1"]
`;

export function taxonomyVersion(id: number, extra: Partial<TaxonomyVersion> = {}): TaxonomyVersion {
  return {
    id,
    version: id,
    name: "acme-labels",
    state: "approved",
    digest: "d".repeat(64),
    source_yaml: TAXONOMY_YAML,
    spec: {
      scope: { groups: ["acme"] },
      placement: "top_level_group",
      strict: false,
      labels: [
        {
          name: "priority::high",
          color: "#d9534f",
          description: "Fix this sprint",
          aliases: ["P1"],
        },
        { name: "type::bug", color: "#1F75CB", description: null, aliases: [] },
      ],
    },
    justification: null,
    proposed_by: ada,
    proposed_at: "2026-11-01T10:00:00Z",
    review_required: true,
    self_approved: false,
    decided_by: grace,
    decided_at: "2026-11-01T11:00:00Z",
    decision_note: null,
    in_force_from: "2026-11-01T11:00:00Z",
    ...extra,
  };
}

export function taxonomyView(extra: Partial<TaxonomyView> = {}): TaxonomyView {
  const current = taxonomyVersion(1);
  return {
    current,
    versions: [current],
    review_required: true,
    as_of: null,
    evaluation_run: null,
    ...extra,
  };
}

const project = { type: "project" as const, gitlab_id: 105, path: "acme/api" };
const group = { type: "group" as const, gitlab_id: 10, path: "acme" };

export function drift(kind: DriftItem["kind"], extra: Partial<DriftItem> = {}): DriftItem {
  return {
    id: `project:105:${kind}:priority::high`,
    owner: project,
    top_level_group: "acme",
    kind,
    label: "priority::high",
    observed: { name: "priority::high", color: "#ff0000", description: "Fix this sprint", defined_at: group },
    expected: { name: "priority::high", color: "#d9534f", description: "Fix this sprint", placement: group },
    reason: null,
    observations: [{ id: 501, payload_digest: "e".repeat(64), collected_at: "2026-11-05T08:00:00Z" }],
    ...extra,
  };
}

export function driftPage(items: DriftItem[], extra: Partial<DriftPage> = {}): DriftPage {
  return {
    items,
    next_after: null,
    totals: {
      total: items.length,
      by_kind: { color: 1, unknown: 1 },
      by_group: [{ group: "acme", total: items.length, by_kind: { color: 1, unknown: 1 } }],
    },
    taxonomy: { id: 1, version: 1, name: "acme-labels" },
    collected_at: "2026-11-05T08:00:00Z",
    as_of: null,
    evaluation_run: null,
    ...extra,
  };
}

export function ownerLabels(extra: Partial<OwnerLabels> = {}): OwnerLabels {
  return {
    owner: project,
    status: "known",
    reason: null,
    observation: { id: 501, payload_digest: "e".repeat(64), collected_at: "2026-11-05T08:00:00Z" },
    labels: [
      { id: 7, name: "priority::high", color: "#ff0000", description: null, priority: null, defined_at: group },
      { id: 9, name: "local", color: "#00aa00", description: "Project only", priority: 1, defined_at: project },
    ],
    drift: [drift("color")],
    as_of: null,
    evaluation_run: null,
    ...extra,
  };
}

export function step(n: number, extra: Partial<LabelPlanStep> = {}): LabelPlanStep {
  return {
    step: n,
    owner: group,
    action: "update",
    label: "priority::high",
    prior: { color: "#ff0000" },
    intended: { color: "#d9534f" },
    idempotency_key: `k${String(n)}`,
    status: "pending",
    request_status: null,
    verified: null,
    message: null,
    finished_at: null,
    ...extra,
  };
}

export function labelPlan(id: number, extra: Partial<LabelPlan> = {}): LabelPlan {
  return {
    id,
    state: "draft",
    scope: { groups: ["acme"] },
    taxonomy: { id: 1, version: 1, name: "acme-labels" },
    steps: [
      step(1),
      step(2, { action: "rename", label: "type::bug", prior: { name: "Bug" }, intended: { name: "type::bug" } }),
    ],
    manual: [
      {
        owner: project,
        kind: "duplicate",
        label: "priority::high",
        reason: "Also defined on the project; remove the project label by hand.",
      },
    ],
    plan_digest: "f".repeat(64),
    created_by: ada,
    created_at: "2026-11-05T09:00:00Z",
    expires_at: "2026-11-06T09:00:00Z",
    digest_valid: true,
    submitted_at: null,
    decided_by: null,
    decided_at: null,
    decision_note: null,
    review_required: true,
    self_approved: false,
    apply_started_at: null,
    finished_at: null,
    failed_step: null,
    failure: null,
    can_apply: { allowed: false, reasons: ["not_approved"] },
    ...extra,
  };
}

export function tokenStatus(extra: Partial<WriteTokenStatus> = {}): WriteTokenStatus {
  return {
    set: false,
    writes_enabled: true,
    scopes: null,
    expires_at: null,
    token_name: null,
    checked_at: null,
    check_error: null,
    ...extra,
  };
}
