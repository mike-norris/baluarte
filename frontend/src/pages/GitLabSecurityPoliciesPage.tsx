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
import { buttonClass, fieldClass, linkClass, secondaryClass } from "../components/styles";
import { COVERAGE_VIEW, securityPolicyHref, typeLabels } from "../security-policies/format";
import { EnabledCell, SecurityPoliciesHeader } from "../security-policies/parts";

const cell = "px-3 py-2";

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
    <div className="overflow-x-auto rounded border border-slate-200 bg-white">
      <table className="w-full text-left text-sm">
        <caption className="sr-only">GitLab security policies</caption>
        <thead className="border-b border-slate-200 bg-slate-50">
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
        <tbody className="divide-y divide-slate-100 align-top">
          {policies.map((policy) => (
            <tr key={policy.id}>
              <th scope="row" className={`${cell} font-medium`}>
                <Link to={securityPolicyHref(policy.id)} className={linkClass}>
                  {policy.name}
                </Link>
                {policy.state === "missing" && (
                  <span className="ml-2 rounded bg-slate-200 px-1.5 py-0.5 text-xs font-normal">
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
                  <span className="block text-xs text-slate-600">
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
      className="flex flex-wrap items-end gap-3 rounded border border-slate-200 bg-white p-4"
    >
      <label className="text-sm font-medium">
        Type
        <select name="type" defaultValue={filters.policyType} className={fieldClass}>
          <option value="">Any</option>
          {policyTypes.map((type) => (
            <option key={type} value={type}>
              {typeLabels[type]}
            </option>
          ))}
        </select>
      </label>
      <label className="flex items-center gap-2 text-sm font-medium">
        <input name="missing" type="checkbox" defaultChecked={filters.includeMissing} />
        Include policies no longer found in GitLab
      </label>
      <button type="submit" className={buttonClass}>
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
      <p role="status" className="text-slate-600">
        Loading GitLab security policies…
      </p>
    );
  } else if (paged.items.length === 0) {
    const filtered = filters.policyType !== "" || filters.includeMissing;
    body = (
      <p className="rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
        {filtered ? (
          "No GitLab security policies match these filters."
        ) : first.data.readable && first.data.collection.status !== "never" ? (
          <>
            {"No GitLab security policies were found. Groups without one are listed under "}
            <Link to={COVERAGE_VIEW} className={linkClass}>
              Coverage
            </Link>
            .
          </>
        ) : (
          "None are listed because they couldn't be read; this doesn't mean there are none."
        )}
      </p>
    );
  } else {
    body = (
      <div className="space-y-3">
        <PolicyTable policies={paged.items} />
        <p className="text-sm text-slate-600" aria-live="polite">
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
            className={secondaryClass}
          >
            {paged.pending ? "Loading…" : "Load more"}
          </button>
        )}
      </div>
    );
  }

  return (
    <section aria-labelledby="page-title" className="space-y-6">
      <SecurityPoliciesHeader context={first.data} />
      <Filters filters={filters} />
      {body}
    </section>
  );
}
