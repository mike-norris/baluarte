import type { Exclusion, ExclusionList, Monitoring } from "../api/scope";

export const SCOPE_ACTIVE = "GET /api/v1/scope/exclusions?state=active";
export const SCOPE_PROPOSED = "GET /api/v1/scope/exclusions?state=proposed";

export const included: Monitoring = { excluded: false, exclusion_id: null, via: null, path: null };

export function excludedVia(
  via: "project" | "group" | "personal_namespaces",
  exclusionId: number,
  path: string | null,
): Monitoring {
  return { excluded: true, exclusion_id: exclusionId, via, path };
}

export function exclusion(id: number, extra: Partial<Exclusion> = {}): Exclusion {
  return {
    id,
    target_type: "project",
    target_id: 201,
    target_path: "acme/research/prototype",
    batch_id: "6f1c2a3e-0000-4000-8000-000000000001",
    justification: "Research prototypes are out of scope for the acme audit.",
    state: "active",
    proposed_by: { actor: "user:1", user_id: 1, display_name: "Ada Lovelace" },
    proposed_at: "2026-11-01T10:00:00Z",
    review_required: false,
    self_approved: true,
    decided_by: null,
    decided_at: null,
    decision_note: null,
    ...extra,
  };
}

export function exclusions(items: Exclusion[], reviewRequired = false): ExclusionList {
  return { items, review_required: reviewRequired };
}
