import type { Evaluation, Finding, FindingDetail, Observation } from "../api/evaluations";

export function evaluation(id: number, extra: Partial<Evaluation> = {}): Evaluation {
  return {
    id,
    evaluation_run_id: 7,
    observation_id: 9,
    project: { gitlab_id: 105, path: "platform/api", state: "active" },
    policy: { policy_id: 1, name: "corporate-default", version: 2 },
    rule_id: "project-visibility",
    rule_type: "gitlab.project.visibility",
    severity: "high",
    outcome: "fail",
    reason: "Visibility is public; the rule allows private.",
    unknown_reasons: [],
    expected: ["private"],
    observed: "public",
    source: "gitlab_project.visibility",
    observed_at: "2026-10-29T06:00:00Z",
    capability: { name: null, status: "not_required" },
    collection: { kind: "projects", status: "fresh", last_success_at: "2026-10-29T06:00:00Z" },
    ancestry: "complete",
    evaluated_at: "2026-10-29T06:12:00Z",
    exception_id: null,
    ...extra,
  };
}

export function finding(id: number, extra: Partial<Finding> = {}): Finding {
  return {
    id,
    project: { gitlab_id: 100 + id, path: `platform/p${String(id)}`, state: "active" },
    policy: { policy_id: 1, name: "corporate-default", version: 2 },
    rule_id: "project-visibility",
    severity: "high",
    state: "open",
    resolution: null,
    latest_outcome: "fail",
    first_seen_at: "2026-10-28T06:00:00Z",
    last_seen_at: "2026-10-29T06:12:00Z",
    resolved_at: null,
    exception: null,
    ...extra,
  };
}

export function findingDetail(
  id: number,
  latest: Evaluation = evaluation(20),
  extra: Partial<Finding> = {},
): FindingDetail {
  return {
    ...finding(id, { project: latest.project, latest_outcome: latest.outcome, ...extra }),
    latest_evaluation: latest,
  };
}

export function observation(id: number, extra: Partial<Observation> = {}): Observation {
  return {
    id,
    source: "GET /projects/:id",
    source_ref: "/projects/105",
    collected_at: "2026-10-29T06:00:00Z",
    collection_status: "ok",
    payload: { gitlab_id: 105, visibility: "public" },
    payload_digest: "a".repeat(64),
    digest_valid: true,
    error: null,
    collection_run_id: 3,
    collection_job_id: 4,
    ...extra,
  };
}
