import type { FormEvent } from "react";
import { Link, useSearchParams } from "react-router";

import {
  listSecurityPolicies,
  policyTypes,
  type PolicyFilters,
  type PolicyType,
  type SecurityPolicyPage,
  type SecurityPolicySummary,
} from "../api/securityPolicies";
import { useLoad } from "../api/useLoad";
import { usePaged } from "../api/usePaged";
import { ErrorMessage } from "../components/ErrorMessage";
import { COVERAGE_VIEW, securityPolicyHref, typeLabels } from "../security-policies/format";
import { EnabledCell, SecurityPoliciesHeader } from "../security-policies/parts";

const cell = "align-top";

function readFilters(params: URLSearchParams): PolicyFilters {
  const type = params.get("type") ?? "";
  return {
    policyType: (policyTypes as readonly string[]).includes(type) ? (type as PolicyType) : "",
    includeMissing: params.get("missing") === "true",
  };
}

function writeFilters(filters: PolicyFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.policyType) params.set("type", filters.policyType);
  if (filters.includeMissing) params.set("missing", "true");
  return params;
}

function reachText(policy: SecurityPolicySummary): string {
  const { groups, projects } = policy.reaches;
  const part = (count: number, noun: string) => `${String(count)} ${noun}${count === 1 ? "" : "s"}`;
  return `${part(groups, "group")}, ${part(projects, "project")}`;
}

function PolicyTable({ policies }: { policies: SecurityPolicySummary[] }) {
  return (
    <div className="card table-responsive">
      <table className="table table-vcenter card-table mb-0">
        <caption className="visually-hidden">GitLab security policies</caption>
        <thead>
          <tr>
            {["GitLab security policy", "Type", "State", "Policy source project", "Reaches"].map(
              (header) => (
                <th key={header} scope="col" className={`${cell} font-medium`}>
                  {header}
                </th>
              ),
            )}
          </tr>
        </thead>
        <tbody className="align-top">
          {policies.map((policy) => (
            <tr key={policy.id}>
              <th scope="row" className={`${cell} font-medium`}>
                <Link to={securityPolicyHref(policy.id)}>{policy.name}</Link>
                {policy.state === "missing" && (
                  <span className="ms-2 badge bg-secondary-subtle text-secondary-emphasis border border-secondary-subtle fw-normal">
                    No longer found in GitLab
                  </span>
                )}
              </th>
              <td className={cell}>{typeLabels[policy.policy_type]}</td>
              <td className={cell}>
                <EnabledCell enabled={policy.enabled} />
              </td>
              <td className={cell}>
                {policy.origin_path}
                {policy.linked_from.length > 0 && (
                  <span className="d-block small text-secondary">
                    {`Linked to ${policy.linked_from.map((link) => link.path).join(", ")}`}
                  </span>
                )}
              </td>
              <td className={`${cell} whitespace-nowrap`}>{reachText(policy)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Filters({ filters }: { filters: PolicyFilters }) {
  const [, setParams] = useSearchParams();

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const type = form.get("type");
    setParams(
      writeFilters({
        policyType:
          typeof type === "string" ? readFilters(new URLSearchParams({ type })).policyType : "",
        includeMissing: form.get("missing") !== null,
      }),
    );
  }

  return (
    <form
      key={writeFilters(filters).toString()}
      onSubmit={apply}
      aria-label="Filter GitLab security policies"
      className="d-flex flex-wrap align-items-end gap-3 rounded border p-3"
    >
      <label className="fw-medium">
        Type
        <select name="type" defaultValue={filters.policyType} className="form-control mt-1">
          <option value="">Any</option>
          {policyTypes.map((type) => (
            <option key={type} value={type}>
              {typeLabels[type]}
            </option>
          ))}
        </select>
      </label>
      <label className="d-flex align-items-center gap-2 fw-medium">
        <input name="missing" type="checkbox" defaultChecked={filters.includeMissing} />
        Include policies no longer found in GitLab
      </label>
      <button type="submit" className="btn btn-primary">
        Apply filters
      </button>
    </form>
  );
}

export function GitLabSecurityPoliciesPage() {
  const [params] = useSearchParams();
  const filters = readFilters(params);
  const key = `security-policies?${writeFilters(filters).toString()}`;
  const first = useLoad(key, (signal) => listSecurityPolicies(filters, null, signal));
  const paged = usePaged<SecurityPolicySummary, SecurityPolicyPage>(
    key,
    first,
    (page) => page.next_after_id,
    (after) => listSecurityPolicies(filters, after),
  );

  let body;
  if (first.error) body = <ErrorMessage error={first.error} retry={first.retry} />;
  else if (!first.data) {
    body = (
      <p role="status" className="text-secondary">
        Loading GitLab security policies…
      </p>
    );
  } else if (paged.items.length === 0) {
    const filtered = filters.policyType !== "" || filters.includeMissing;
    body = (
      <p className="text-secondary border border-dashed rounded p-3">
        {filtered ? (
          "No GitLab security policies match these filters."
        ) : first.data.readable && first.data.collection.status !== "never" ? (
          <>
            {"No GitLab security policies were found. Groups without one are listed under "}
            <Link to={COVERAGE_VIEW}>Coverage</Link>.
          </>
        ) : (
          "None are listed because they couldn't be read; this doesn't mean there are none."
        )}
      </p>
    );
  } else {
    body = (
      <div>
        <PolicyTable policies={paged.items} />
        <p className="text-secondary" aria-live="polite">
          {`Showing ${String(paged.items.length)} GitLab security ${paged.items.length === 1 ? "policy" : "policies"}${
            paged.next !== null ? " (more available)" : ""
          }.`}
        </p>
        {paged.moreError && (
          <ErrorMessage error={paged.moreError} retry={() => void paged.loadMore()} />
        )}
        {paged.next !== null && (
          <button
            type="button"
            onClick={() => void paged.loadMore()}
            disabled={paged.pending}
            className="btn"
          >
            {paged.pending ? "Loading…" : "Load more"}
          </button>
        )}
      </div>
    );
  }

  return (
    <section aria-labelledby="page-title">
      <SecurityPoliciesHeader context={first.data} />
      <Filters filters={filters} />
      {body}
    </section>
  );
}
