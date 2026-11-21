import { useState, type FormEvent } from "react";
import { Link } from "react-router";

import { getJson } from "../api/client";
import type { Page, Project } from "../api/gitlab";
import {
  categories,
  categoryLabels,
  collectRegister,
  createDecision,
  createRule,
  endDecision,
  endRule,
  issueHref,
  issueTypeLabels,
  listRegister,
  listRules,
  MIN_REASON,
  PAGE_SIZE,
  type IssueType,
  type Reason,
  type RegisterCategory,
  type RegisterFilters,
  type RegisterIssue,
  type Rule,
} from "../api/register";
import { errorReasons } from "../api/send";
import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { asApiError, Problems } from "../settings/parts";
import { formatTime } from "../time";
import { Badge } from "../ui/Badge";
import { Dialog } from "../ui/Dialog";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { Notice } from "../ui/Notice";
import { PageHeader } from "../ui/PageHeader";
import { Pagination } from "../ui/Pagination";
import { SectionTabs } from "../ui/SectionTabs";

import { REGISTER_TABS } from "./registerTabs";

const TABS = REGISTER_TABS;

const SUBTITLE =
  "Issues a selection rule matches or someone included, with why. Closing an issue is never " +
  "taken as proof that a control works.";

function failure(error: unknown): string[] {
  const apiError = asApiError(error);
  return [apiError.message, ...errorReasons(apiError)];
}

function useUser() {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  return {
    csrf: user?.csrf_token ?? "",
    canWrite: user?.permissions.includes("register:write") ?? false,
  };
}

function categoryTone(category: RegisterCategory) {
  return category === "security_finding"
    ? "danger"
    : category === "exception"
      ? "warning"
      : category === "remediation"
        ? "info"
        : "secondary";
}

function WhyList({ why }: { why: Reason[] }) {
  return (
    <ul className="list-unstyled mb-0 small">
      {why.map((reason, index) => (
        <li key={index}>
          {reason.kind === "rule" ? (
            <>Rule: {reason.rule_name}</>
          ) : (
            <>
              {reason.kind === "included" ? "Included" : "Excluded"} by {reason.decided_by}:{" "}
              <span className="text-secondary">{reason.reason}</span>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

type Deciding = { issue: RegisterIssue; decision: "include" | "exclude" } | null;

function DecisionDialog({
  deciding,
  onClose,
  onDone,
}: {
  deciding: Deciding;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const { csrf } = useUser();
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);
  if (!deciding) return null;
  const { issue, decision } = deciding;
  const name = `${issue.project}#${String(issue.iid)}`;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const reason = String(data.get("reason") ?? "").trim();
    const category = String(data.get("category") ?? "");
    if (reason.length < MIN_REASON) {
      setProblems([`Give a reason of at least ${String(MIN_REASON)} characters.`]);
      return;
    }
    setSaving(true);
    try {
      await createDecision(
        {
          project_id: issue.project_id,
          iid: issue.iid,
          decision,
          reason,
          ...(decision === "include" && category ? { category: category as RegisterCategory } : {}),
        },
        csrf,
      );
      onDone(`${name} ${decision === "include" ? "included" : "excluded"}.`);
    } catch (error) {
      setProblems(failure(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Dialog
      open
      title={`${decision === "include" ? "Include" : "Exclude"} ${name}`}
      onClose={onClose}
    >
      <form onSubmit={(event) => void submit(event)} noValidate className="stack-3">
        <Problems messages={problems} />
        <p className="mb-0">
          {decision === "exclude"
            ? "The issue leaves the register but stays listed as excluded, with your reason, for anyone who shows excluded issues."
            : "The issue is auditable whatever the selection rules say."}
        </p>
        {decision === "include" && (
          <label className="fw-medium">
            Category
            <select name="category" className="form-select mt-1" defaultValue="">
              <option value="">From the rules</option>
              {categories.map((value) => (
                <option key={value} value={value}>
                  {categoryLabels[value]}
                </option>
              ))}
            </select>
          </label>
        )}
        <label className="fw-medium d-block">
          Reason
          <textarea name="reason" rows={3} maxLength={2000} className="form-control mt-1" />
        </label>
        <div className="d-flex gap-2">
          <button type="submit" disabled={saving} className="btn btn-primary">
            {saving ? "Saving…" : decision === "include" ? "Include" : "Exclude"}
          </button>
          <button type="button" className="btn" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function filtersFrom(data: FormData): RegisterFilters {
  const value = (name: string) => {
    const entry = data.get(name);
    return typeof entry === "string" && entry.trim() ? entry.trim() : undefined;
  };
  const day = (name: string, end: boolean) => {
    const date = value(name);
    return date ? `${date}T${end ? "23:59:59" : "00:00:00"}Z` : undefined;
  };
  return {
    category: value("category") as RegisterCategory | undefined,
    state: value("state") as "opened" | "closed" | undefined,
    label: value("label"),
    assignee: value("assignee"),
    q: value("q"),
    period_start: day("period_start", false),
    period_end: day("period_end", true),
    include_excluded: data.get("include_excluded") === "on",
  };
}

export function RegisterPage() {
  const { csrf, canWrite } = useUser();
  const [filters, setFilters] = useState<RegisterFilters>({});
  const [offset, setOffset] = useState(0);
  const [message, setMessage] = useState<string | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [deciding, setDeciding] = useState<Deciding>(null);
  const loaded = useLoad(`register:${JSON.stringify(filters)}:${String(offset)}`, (signal) =>
    listRegister(filters, offset, signal),
  );
  const page = loaded.data;

  function done(text: string) {
    setDeciding(null);
    setMessage(text);
    setProblems([]);
    loaded.retry();
  }

  async function collect() {
    try {
      await collectRegister(csrf);
      setMessage("Reading the issues now. New and changed issues appear when the read finishes.");
    } catch (error) {
      setProblems(failure(error));
    }
  }

  async function end(decisionId: number, name: string) {
    try {
      await endDecision(decisionId, csrf);
      done(`The decision on ${name} was ended; the selection rules decide again.`);
    } catch (error) {
      setProblems(failure(error));
    }
  }

  return (
    <div className="stack-3">
      <PageHeader pretitle="Evidence" title="Issue register" subtitle={SUBTITLE} />
      <SectionTabs label="Issue register" tabs={TABS} />
      {message && (
        <Notice tone="success" role="status">
          {message}
        </Notice>
      )}
      <Problems messages={problems} />
      <form
        aria-label="Filter the register"
        className="card card-body d-grid gap-3 cols-sm-2 cols-lg-4"
        onSubmit={(event) => {
          event.preventDefault();
          setOffset(0);
          setFilters(filtersFrom(new FormData(event.currentTarget)));
        }}
      >
        <label className="fw-medium">
          Category
          <select name="category" className="form-select mt-1" defaultValue="">
            <option value="">Any</option>
            {categories.map((value) => (
              <option key={value} value={value}>
                {categoryLabels[value]}
              </option>
            ))}
          </select>
        </label>
        <label className="fw-medium">
          State
          <select name="state" className="form-select mt-1" defaultValue="">
            <option value="">Any</option>
            <option value="opened">Open</option>
            <option value="closed">Closed</option>
          </select>
        </label>
        <label className="fw-medium">
          Open between
          <input type="date" name="period_start" className="form-control mt-1" />
        </label>
        <label className="fw-medium">
          and
          <input type="date" name="period_end" className="form-control mt-1" />
        </label>
        <label className="fw-medium">
          Label
          <input name="label" className="form-control mt-1" />
        </label>
        <label className="fw-medium">
          Assignee
          <input name="assignee" placeholder="Username" className="form-control mt-1" />
        </label>
        <label className="fw-medium">
          Title contains
          <input name="q" className="form-control mt-1" />
        </label>
        <div className="d-flex flex-column justify-content-end gap-2">
          <label className="form-check mb-0">
            <input type="checkbox" name="include_excluded" className="form-check-input" />
            <span className="form-check-label">Show excluded issues</span>
          </label>
          <button type="submit" className="btn btn-primary">
            Apply filters
          </button>
        </div>
      </form>
      {loaded.loading && <Loading label="Loading the register…" />}
      {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
      {page && (
        <>
          <div className="d-flex flex-wrap align-items-center gap-2">
            <span className="text-secondary">
              {page.last_read_at
                ? `Issues last read ${formatTime(page.last_read_at)}.`
                : "Issues not read yet."}
            </span>
            {canWrite && page.active && (
              <button type="button" className="btn btn-sm ms-auto" onClick={() => void collect()}>
                Read issues now
              </button>
            )}
          </div>
          {!page.active ? (
            <EmptyState title="No register yet">
              {
                "Add a selection rule, or include an issue, to start the register. Issues are read only for workspaces that have one."
              }
            </EmptyState>
          ) : page.items.length === 0 && page.pending.length === 0 ? (
            <EmptyState title="No auditable issues">{"Nothing matches these filters."}</EmptyState>
          ) : (
            <div className="card">
              <div className="table-responsive">
                <table className="table table-vcenter card-table">
                  <thead>
                    <tr>
                      <th>Issue</th>
                      <th>Category</th>
                      <th>State</th>
                      <th>Why</th>
                      {canWrite && <th className="w-1">Actions</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {page.items.map((issue) => {
                      const name = `${issue.project}#${String(issue.iid)}`;
                      const decision = issue.why.find((w) => w.kind !== "rule");
                      return (
                        <tr key={`${String(issue.project_id)}-${String(issue.iid)}`}>
                          <td>
                            <div>
                              <Link to={issueHref(issue.project_id, issue.iid)}>{name}</Link>
                              {issue.excluded && (
                                <>
                                  {" "}
                                  <Badge tone="secondary">Excluded</Badge>
                                </>
                              )}
                            </div>
                            <div className="text-secondary small">
                              {issue.confidential ? "Confidential issue" : issue.title}
                            </div>
                          </td>
                          <td>
                            <Badge tone={categoryTone(issue.category)}>
                              {categoryLabels[issue.category]}
                            </Badge>
                          </td>
                          <td className="text-nowrap">
                            {issue.state === "closed"
                              ? `Closed ${issue.closed_at ? formatTime(issue.closed_at) : ""}`
                              : "Open"}
                          </td>
                          <td>
                            <WhyList why={issue.why} />
                          </td>
                          {canWrite && (
                            <td className="text-nowrap">
                              {decision?.decision_id ? (
                                <button
                                  type="button"
                                  className="btn btn-sm"
                                  onClick={() => void end(decision.decision_id ?? 0, name)}
                                >
                                  End {decision.kind === "included" ? "inclusion" : "exclusion"}
                                </button>
                              ) : (
                                <button
                                  type="button"
                                  className="btn btn-sm"
                                  onClick={() => setDeciding({ issue, decision: "exclude" })}
                                >
                                  Exclude
                                </button>
                              )}
                            </td>
                          )}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <Pagination
                range={`${String(page.total === 0 ? 0 : offset + 1)}–${String(offset + page.items.length)} of ${String(page.total)}`}
                hasPrevious={offset > 0}
                hasNext={offset + PAGE_SIZE < page.total}
                onPrevious={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                onNext={() => setOffset(offset + PAGE_SIZE)}
              />
            </div>
          )}
          {page.pending.length > 0 && (
            <section aria-label="Included, not read yet" className="card card-body stack-2">
              <h2 className="fw-semibold mb-0">Included, not read yet</h2>
              <ul className="mb-0">
                {page.pending.map((item) => (
                  <li key={item.decision_id}>
                    {item.project}#{item.iid}: {item.reason}{" "}
                    <span className="text-secondary">({item.decided_by})</span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          {canWrite && <IncludeForm onDone={done} />}
        </>
      )}
      <DecisionDialog deciding={deciding} onClose={() => setDeciding(null)} onDone={done} />
    </div>
  );
}

function useProjects() {
  return useLoad("register-projects", (signal) =>
    getJson<Page<Project>>("/api/v1/gitlab/projects", { limit: 500, state: "active" }, signal),
  );
}

function IncludeForm({ onDone }: { onDone: (message: string) => void }) {
  const { csrf } = useUser();
  const projects = useProjects();
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const projectId = Number(data.get("project_id"));
    const iid = Number(data.get("iid"));
    const reason = String(data.get("reason") ?? "").trim();
    const category = String(data.get("category") ?? "");
    const found: string[] = [];
    if (!projectId) found.push("Choose a project.");
    if (!Number.isInteger(iid) || iid < 1) found.push("Enter the issue number.");
    if (reason.length < MIN_REASON)
      found.push(`Give a reason of at least ${String(MIN_REASON)} characters.`);
    setProblems(found);
    if (found.length > 0) return;
    setSaving(true);
    try {
      await createDecision(
        {
          project_id: projectId,
          iid,
          decision: "include",
          reason,
          ...(category ? { category: category as RegisterCategory } : {}),
        },
        csrf,
      );
      form.reset();
      onDone(`Issue #${String(iid)} included.`);
    } catch (error) {
      setProblems(failure(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      aria-label="Include an issue"
      noValidate
      onSubmit={(event) => void submit(event)}
      className="card card-body stack-3"
    >
      <h2 className="fw-semibold mb-0">Include an issue</h2>
      <Problems messages={problems} />
      <div className="d-grid gap-3 cols-sm-2 cols-lg-4">
        <label className="fw-medium">
          Project
          <select name="project_id" className="form-select mt-1" defaultValue="">
            <option value="">Choose…</option>
            {(projects.data?.items ?? []).map((project) => (
              <option key={project.id} value={project.id}>
                {project.path_with_namespace}
              </option>
            ))}
          </select>
        </label>
        <label className="fw-medium">
          Issue number
          <input name="iid" inputMode="numeric" className="form-control mt-1" />
        </label>
        <label className="fw-medium">
          Category
          <select name="category" className="form-select mt-1" defaultValue="">
            <option value="">From the rules</option>
            {categories.map((value) => (
              <option key={value} value={value}>
                {categoryLabels[value]}
              </option>
            ))}
          </select>
        </label>
      </div>
      <label className="fw-medium d-block">
        Reason
        <textarea name="reason" rows={2} maxLength={2000} className="form-control mt-1" />
      </label>
      <button type="submit" disabled={saving} className="btn btn-primary">
        {saving ? "Including…" : "Include"}
      </button>
    </form>
  );
}

function RuleCard({
  rule,
  canWrite,
  onEnded,
}: {
  rule: Rule;
  canWrite: boolean;
  onEnded: (message: string) => void;
}) {
  const { csrf } = useUser();
  const [problems, setProblems] = useState<string[]>([]);
  const criteria = [
    rule.labels.length > 0 && `Labels: any of ${rule.labels.join(", ")}`,
    rule.projects.length > 0 && `Projects: ${rule.projects.join(", ")}`,
    rule.issue_types.length > 0 &&
      `Types: ${rule.issue_types.map((t) => issueTypeLabels[t]).join(", ")}`,
  ].filter(Boolean);

  async function end() {
    try {
      await endRule(rule.id, csrf);
      onEnded(`Rule ${rule.name} ended.`);
    } catch (error) {
      setProblems(failure(error));
    }
  }

  return (
    <section aria-label={rule.name} className="card card-body stack-2">
      <div className="d-flex flex-wrap align-items-center gap-2">
        <h2 className="fw-semibold mb-0">{rule.name}</h2>
        <Badge tone={categoryTone(rule.category)}>{categoryLabels[rule.category]}</Badge>
        {canWrite && (
          <button type="button" className="btn btn-sm ms-auto" onClick={() => void end()}>
            End rule
          </button>
        )}
      </div>
      <Problems messages={problems} />
      <ul className="mb-0">
        {criteria.map((line) => (
          <li key={String(line)}>{line}</li>
        ))}
      </ul>
      <p className="small text-secondary mb-0">
        Added by {rule.created_by} on {formatTime(rule.created_at)}. An issue must meet every
        criterion.
      </p>
    </section>
  );
}

function NewRuleForm({ onCreated }: { onCreated: (message: string) => void }) {
  const { csrf } = useUser();
  const projects = useProjects();
  const [problems, setProblems] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const labels = String(data.get("labels") ?? "")
      .split(/[\n,]/)
      .map((label) => label.trim())
      .filter(Boolean);
    const body = {
      name: String(data.get("name") ?? "").trim(),
      category: String(data.get("category") ?? "engineering") as RegisterCategory,
      labels,
      project_ids: data.getAll("project_ids").map(Number),
      issue_types: data.getAll("issue_types").map(String) as IssueType[],
    };
    const found: string[] = [];
    if (!body.name) found.push("Enter a name.");
    if (!labels.length && !body.project_ids.length && !body.issue_types.length) {
      found.push("Choose at least one label, project or issue type.");
    }
    setProblems(found);
    if (found.length > 0) return;
    setSaving(true);
    try {
      const created = await createRule(body, csrf);
      form.reset();
      onCreated(`Rule ${created.name} added. Its issues are being read.`);
    } catch (error) {
      setProblems(failure(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      aria-label="New selection rule"
      noValidate
      onSubmit={(event) => void submit(event)}
      className="card card-body stack-3"
    >
      <h2 className="fw-semibold mb-0">New selection rule</h2>
      <Problems messages={problems} />
      <div className="d-grid gap-3 cols-sm-2">
        <label className="fw-medium">
          Name
          <input name="name" maxLength={200} className="form-control mt-1" />
        </label>
        <label className="fw-medium">
          Category
          <select name="category" className="form-select mt-1" defaultValue="engineering">
            {categories.map((value) => (
              <option key={value} value={value}>
                {categoryLabels[value]}
              </option>
            ))}
          </select>
        </label>
        <label className="fw-medium">
          Labels (any of)
          <textarea
            name="labels"
            rows={2}
            placeholder="One label per line"
            className="form-control mt-1"
          />
        </label>
        <label className="fw-medium">
          Projects
          <select name="project_ids" multiple className="form-select mt-1">
            {(projects.data?.items ?? []).map((project) => (
              <option key={project.id} value={project.id}>
                {project.path_with_namespace}
              </option>
            ))}
          </select>
        </label>
      </div>
      <fieldset>
        <legend className="fw-medium fs-5">Issue types</legend>
        <div className="d-flex flex-wrap gap-3">
          {(Object.keys(issueTypeLabels) as IssueType[]).map((type) => (
            <label key={type} className="form-check mb-0">
              <input type="checkbox" name="issue_types" value={type} className="form-check-input" />
              <span className="form-check-label">{issueTypeLabels[type]}</span>
            </label>
          ))}
        </div>
      </fieldset>
      <button type="submit" disabled={saving} className="btn btn-primary">
        {saving ? "Adding…" : "Add rule"}
      </button>
    </form>
  );
}

export function RegisterRulesPage() {
  const { canWrite } = useUser();
  const [message, setMessage] = useState<string | null>(null);
  const loaded = useLoad("register-rules", listRules);

  function changed(text: string) {
    setMessage(text);
    loaded.retry();
  }

  return (
    <div className="stack-3">
      <PageHeader pretitle="Evidence" title="Issue register" subtitle={SUBTITLE} />
      <SectionTabs label="Issue register" tabs={TABS} />
      {message && (
        <Notice tone="success" role="status">
          {message}
        </Notice>
      )}
      {loaded.loading && <Loading label="Loading rules…" />}
      {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
      {loaded.data?.rules.length === 0 && (
        <EmptyState title="No selection rules">
          {"A rule makes issues auditable by label, project or type."}
        </EmptyState>
      )}
      {loaded.data?.rules.map((rule) => (
        <RuleCard key={rule.id} rule={rule} canWrite={canWrite} onEnded={changed} />
      ))}
      {canWrite && <NewRuleForm onCreated={changed} />}
    </div>
  );
}
