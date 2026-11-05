import type { Change, ChangeDetail, ChangeVersion } from "../api/changes";

export const CHANGES_LIST = "GET /api/v1/changes?limit=50";

export function change(id: number, extra: Partial<Change> = {}): Change {
  return {
    id,
    project: { gitlab_id: 105, path: "acme/api", state: "active" },
    kind: "merge_request",
    iid: 40 + id,
    sha: `${String(id)}`.padStart(40, "a"),
    title: "Add rate limits",
    web_path: `/acme/api/-/merge_requests/${String(40 + id)}`,
    author: "dev-one",
    merged_by: "lead-two",
    changed_at: "2026-10-20T10:00:00Z",
    outcome: "pass",
    reasons: [],
    observation: {
      id: 900 + id,
      payload_digest: "b".repeat(64),
      collected_at: "2026-10-20T11:00:00Z",
    },
    approvals: {
      status: "known",
      approved_by: [{ username: "reviewer-three", approved_at: "2026-10-20T09:00:00Z" }],
      required: 1,
      left: 0,
      self_approved: false,
      committer_approved: null,
    },
    pipeline: {
      status: "known",
      id: 7001,
      pipeline_status: "success",
      source: "merge_request_event",
      finished_at: "2026-10-20T09:30:00Z",
      tests: { total: 120, success: 118, failed: 0, skipped: 2, error: 0 },
    },
    deployments: {
      status: "known",
      items: [
        { environment: "production", status: "success", finished_at: "2026-10-20T12:00:00Z" },
      ],
    },
    complete: true,
    ...extra,
  };
}

export function version(id: number, extra: Partial<ChangeVersion> = {}): ChangeVersion {
  return {
    observation_id: id,
    collected_at: "2026-10-20T11:00:00Z",
    source: "GET merge request change",
    payload_digest: "c".repeat(64),
    digest_valid: true,
    payload: { iid: 41, sha: "a".repeat(40) },
    ...extra,
  };
}

export function changeDetail(
  id: number,
  versions: ChangeVersion[] = [version(901)],
  extra: Partial<ChangeDetail> = {},
): ChangeDetail {
  return { ...change(id), versions, ...extra };
}
