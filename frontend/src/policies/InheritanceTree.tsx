import { useId, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router";

import { ApiError } from "../api/client";
import { groupHref, projectHref } from "../api/gitlab";
import {
  assignPolicy,
  getAssignmentTree,
  listPolicies,
  listVersions,
  removeAssignment,
  type PolicySummary,
} from "../api/policies";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { appliedMessage } from "./AssignForm";
import { baselineHref, canWritePolicies, editorHref, proposedMessage } from "./format";
import { buildTree, matches, type Effect, type GroupNode } from "./inheritance";
import { useDeviceClass } from "../ui/device";

const EXPAND_ALL_UP_TO = 50;

function EffectLine({ effect }: { effect: Effect }) {
  const label = `${effect.name} v${String(effect.version)}`;
  return (
    <li>
      <Link to={baselineHref(effect.name, effect.version)}>{label}</Link>
      {effect.kind === "set_here" && (
        <span className="ms-2 badge bg-info-subtle text-info-emphasis border border-info-subtle">
          set here
        </span>
      )}
      {effect.kind === "inherited" && (
        <span className="ms-2 text-secondary">{`inherited from ${effect.from}`}</span>
      )}
      {effect.kind === "out_of_scope" && (
        <span className="ms-2 text-secondary">
          {`not applied here (outside the baseline's groups; from ${effect.from})`}
        </span>
      )}
      {effect.kind === "set_here" && effect.replaces && (
        <span className="ms-2 text-secondary">{`replaces ${effect.replaces}`}</span>
      )}
    </li>
  );
}

/** Apply a baseline to one group, from its row. */
function ApplyHere({
  node,
  policies,
  onDone,
  onCancel,
}: {
  node: GroupNode;
  policies: PolicySummary[];
  onDone: (message: string) => void;
  onCancel: () => void;
}) {
  const { session, refresh } = useAuth();
  const id = useId();
  const [name, setName] = useState(policies[0]?.name ?? "");
  const versions = useLoad(`policy-versions:${name}`, (signal) => listVersions(name, signal));
  const [chosen, setChosen] = useState<number | null>(null);
  const version = chosen ?? versions.data?.[0]?.version ?? null;
  const [failure, setFailure] = useState<string | null>(null);
  const [sending, setSending] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (session.status !== "signed_in" || version === null) return;
    setSending(true);
    setFailure(null);
    try {
      const applied = await assignPolicy(
        { policy_name: name, version, target_type: "group", target_id: node.group.gitlab_id },
        session.user.csrf_token,
      );
      onDone(appliedMessage(applied));
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
      setFailure(apiError.message);
      if (apiError.status === 401) void refresh();
    } finally {
      setSending(false);
    }
  }

  if (policies.length === 0) {
    return (
      <p className="mt-2">
        {"There are no baselines yet. "}
        <Link to={editorHref("recommended")}>Start with the recommended baseline</Link>.
      </p>
    );
  }
  return (
    <form
      onSubmit={(event) => void submit(event)}
      aria-label={`Apply a baseline to ${node.group.full_path}`}
      className="mt-2 d-flex flex-wrap align-items-end gap-2 rounded border p-3"
    >
      <label className="fw-medium" htmlFor={`${id}-name`}>
        Baseline
        <select
          id={`${id}-name`}
          value={name}
          onChange={(event) => {
            setName(event.target.value);
            setChosen(null);
          }}
          className="form-control mt-1"
        >
          {policies.map((policy) => (
            <option key={policy.name} value={policy.name}>
              {policy.name}
            </option>
          ))}
        </select>
      </label>
      <label className="fw-medium" htmlFor={`${id}-version`}>
        Version
        <select
          id={`${id}-version`}
          value={version ?? ""}
          disabled={!versions.data}
          onChange={(event) => {
            setChosen(Number(event.target.value));
          }}
          className="form-control mt-1"
        >
          {!versions.data && <option value="">Loading…</option>}
          {versions.data?.map((item, index) => (
            <option key={item.version} value={item.version}>
              {`v${String(item.version)}${index === 0 ? " (latest)" : ""}`}
            </option>
          ))}
        </select>
      </label>
      <button type="submit" disabled={sending || version === null} className="btn btn-primary">
        {sending ? "Applying…" : "Apply here"}
      </button>
      <button type="button" onClick={onCancel} className="btn">
        Cancel
      </button>
      {failure && (
        <p role="alert" className="w-100 text-danger-emphasis">
          {failure}
        </p>
      )}
    </form>
  );
}

function Row({
  node,
  search,
  expandAll,
  canWrite,
  policies,
  onChanged,
}: {
  node: GroupNode;
  search: string;
  expandAll: boolean;
  canWrite: boolean;
  policies: PolicySummary[];
  onChanged: (message: string) => void;
}) {
  const { session, refresh } = useAuth();
  const [open, setOpen] = useState(expandAll);
  const [action, setAction] = useState<"apply" | number | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const children = node.children.filter((child) => matches(child, search));
  const expanded = open || search.trim() !== "";
  const setHere = node.effects.filter((effect) => effect.kind === "set_here");

  async function remove(effect: Effect) {
    if (session.status !== "signed_in") return;
    setFailure(null);
    try {
      const proposed = await removeAssignment(effect.assignmentId, session.user.csrf_token);
      onChanged(
        proposed
          ? proposedMessage(proposed)
          : `Stopped checking ${effect.name} on group ${node.group.full_path}. Checking again now.`,
      );
    } catch (error) {
      const apiError =
        error instanceof ApiError ? error : new ApiError(0, "The response could not be read.");
      setFailure(apiError.message);
      if (apiError.status === 401) void refresh();
    }
  }

  return (
    <li className="py-1">
      <div className="d-flex flex-wrap align-items-start column-gap-3 row-gap-1 rounded px-2 py-1">
        <span className="d-flex min-w-48 align-items-center gap-1 fw-medium">
          {node.children.length > 0 ? (
            <button
              type="button"
              onClick={() => {
                setOpen(!expanded);
              }}
              aria-expanded={expanded}
              aria-label={`Subgroups of ${node.group.full_path}`}
              className="w-5 text-secondary"
            >
              {expanded ? "▾" : "▸"}
            </button>
          ) : (
            <span className="w-5" aria-hidden="true" />
          )}
          <Link to={groupHref(node.group.gitlab_id)}>{node.group.full_path}</Link>
        </span>
        <div className="grow">
          {node.effects.length === 0 ? (
            <p className="text-secondary">Nothing checked</p>
          ) : (
            <ul>
              {node.effects.map((effect) => (
                <EffectLine key={effect.name} effect={effect} />
              ))}
            </ul>
          )}
          {node.unknown && (
            <p className="text-warning-emphasis">{`⚠ Partly unknown: ${node.unknown}, so baselines applied above it may be missing.`}</p>
          )}
          {node.group.state !== "active" && (
            <p className="text-warning-emphasis">
              {`⚠ This group is ${node.group.state === "missing" ? "no longer found in GitLab" : "not readable with Baluarte's token"}.`}
            </p>
          )}
          {node.projectAssignments.length > 0 && (
            <ul className="mt-1 border-start border-2 ps-3">
              {node.projectAssignments.map((item) => (
                <li key={item.assignmentId}>
                  <Link to={projectHref(item.projectGitlabId)} className="fw-medium">
                    {item.path}
                  </Link>
                  {" (project): "}
                  <Link to={baselineHref(item.name, item.version)}>
                    {`${item.name} v${String(item.version)}`}
                  </Link>
                  <span className="ms-2 badge bg-info-subtle text-info-emphasis border border-info-subtle">
                    set here
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
        {canWrite && node.group.state === "active" && (
          <div className="d-flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => {
                setAction(action === "apply" ? null : "apply");
              }}
              aria-label={`Apply a baseline to ${node.group.full_path}`}
              className="btn"
            >
              Apply here…
            </button>
            {setHere.map((effect) => (
              <button
                key={effect.assignmentId}
                type="button"
                onClick={() => {
                  setAction(effect.assignmentId);
                }}
                aria-label={`Remove ${effect.name} from ${node.group.full_path}`}
                className="btn"
              >
                {`Remove ${effect.name}`}
              </button>
            ))}
          </div>
        )}
      </div>
      {action === "apply" && (
        <ApplyHere
          node={node}
          policies={policies}
          onDone={(message) => {
            setAction(null);
            onChanged(message);
          }}
          onCancel={() => {
            setAction(null);
          }}
        />
      )}
      {typeof action === "number" &&
        setHere
          .filter((effect) => effect.assignmentId === action)
          .map((effect) => (
            <div key={effect.assignmentId} className="mt-2 rounded border p-3">
              <p>
                {`Stop checking ${effect.name} on group ${node.group.full_path}? It falls back to ${effect.replaces ?? "nothing for this baseline"}.`}
              </p>
              <div className="d-flex gap-2">
                <button
                  type="button"
                  onClick={() => void remove(effect)}
                  className="btn btn-danger"
                >
                  Remove
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setAction(null);
                  }}
                  className="btn"
                >
                  Cancel
                </button>
              </div>
              {failure && (
                <p role="alert" className="text-danger-emphasis">
                  {failure}
                </p>
              )}
            </div>
          ))}
      {expanded && children.length > 0 && (
        <ul aria-label={`Subgroups of ${node.group.full_path}`} className="ml-5 border-l pl-2">
          {children.map((child) => (
            <Row
              key={child.group.gitlab_id}
              node={child}
              search={search}
              expandAll={expandAll}
              canWrite={canWrite}
              policies={policies}
              onChanged={onChanged}
            />
          ))}
        </ul>
      )}
    </li>
  );
}

/** Instance ▸ groups ▸ subgroups, with what is set at each level and what is inherited. */
export function InheritanceTree({
  version,
  onChanged,
}: {
  version: number;
  onChanged: (message: string) => void;
}) {
  const { session } = useAuth();
  const device = useDeviceClass();
  // Phones read where baselines apply; assigning needs a larger screen.
  const canWrite =
    canWritePolicies(session.status === "signed_in" ? session.user : null) && device !== "phone";
  const id = useId();
  const [search, setSearch] = useState("");
  const data = useLoad(`baseline-tree#${String(version)}`, getAssignmentTree);
  const policies = useLoad(`policies#${String(version)}`, listPolicies);
  const tree = useMemo(() => (data.data ? buildTree(data.data) : null), [data.data]);

  if (data.error) return <ErrorMessage error={data.error} retry={data.retry} />;
  if (!data.data || !tree) {
    return (
      <p role="status" className="text-secondary">
        Loading groups and baselines…
      </p>
    );
  }
  const roots = tree.roots.filter((node) => matches(node, search));
  const expandAll = data.data.groups.length <= EXPAND_ALL_UP_TO;
  const instanceNames = tree.instance;

  return (
    <section aria-labelledby={`${id}-title`}>
      <h2 id={`${id}-title`} className="visually-hidden">
        Inheritance
      </h2>
      <label className="d-block max-w-md fw-medium" htmlFor={`${id}-search`}>
        Find a group
        <input
          id={`${id}-search`}
          type="search"
          value={search}
          maxLength={200}
          placeholder="group/subgroup"
          onChange={(event) => {
            setSearch(event.target.value);
          }}
          className="form-control mt-1"
        />
      </label>
      <div className="rounded border p-2">
        <ul aria-label="Where baselines apply">
          <li className="py-1">
            <div className="d-flex flex-wrap align-items-start column-gap-3 row-gap-1 rounded px-2 py-1.5">
              <span className="min-w-48 fw-semibold">This instance</span>
              <div className="grow">
                {instanceNames.length === 0 ? (
                  <p className="text-secondary">Nothing applied to the whole instance</p>
                ) : (
                  <ul>
                    {instanceNames.map((item) => (
                      <li key={item.assignmentId}>
                        <Link to={baselineHref(item.name, item.version)}>
                          {`${item.name} v${String(item.version)}`}
                        </Link>
                        <span className="ms-2 badge bg-info-subtle text-info-emphasis border border-info-subtle">
                          set here
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
            {roots.length === 0 ? (
              <p className="ml-7 py-2 text-secondary">
                {search.trim() ? "No group matches." : "No groups have been collected yet."}
              </p>
            ) : (
              <ul aria-label="Top-level groups" className="ml-5 border-l pl-2">
                {roots.map((node) => (
                  <Row
                    key={node.group.gitlab_id}
                    node={node}
                    search={search}
                    expandAll={expandAll}
                    canWrite={canWrite}
                    policies={policies.data ?? []}
                    onChanged={onChanged}
                  />
                ))}
              </ul>
            )}
          </li>
        </ul>
      </div>
    </section>
  );
}
