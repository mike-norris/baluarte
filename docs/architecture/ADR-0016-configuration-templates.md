# ADR-0016: Configuration templates and bulk changes

- Status: Accepted
- Date: 2026-11-24

## Context

Administrators need to state how repositories should be configured, apply that across many of them, and see where they drift (roadmap M14, FR-032 to FR-035). Baselines already say what is acceptable (`allowed: [private, internal]`, `min_approvals: 1`) and are evaluated with evidence; remediation plans (ADR-0007, ADR-0012) and label plans already change GitLab under review, behind a switch and a separate write token. A template differs from a baseline in one way that matters: it names the exact value to set, so a change can be planned from it.

## Decision

- **A separate document kind.** A `ConfigurationTemplate` (`baluarte.io/v1alpha1`) names exact values for settings, not acceptable ranges. It is stored as immutable versions per workspace, like baselines, and never claims compliance: applying a template changes settings; whether a control is met is still judged by baselines and people.
- **Only settings Baluarte can read.** Every setting must be one Baluarte collects on at least one provider, so drift can be judged from evidence, and maps to the rule type that already judges it: `visibility`, `default_branch_protected`, `merge_requests` (approvals), `security_features` (GitHub) and `labels_consistent`. Merge methods, repository metadata and CI/CD settings follow once they are collected. Each setting declares, per provider, whether it is supported, unsupported (no equivalent), or depends on the plan; this is shown wherever the template applies.
- **Inheritance by pinned extension.** `spec.extends` names one exact version of another template; the child's settings replace the parent's, setting by setting (nested groups such as `merge_requests` field by field). Chains are at most 5 deep and may not loop.
- **Precedence like baselines.** A template version is assigned to a connection, a group or a project; for each setting the nearest assignment that sets it wins, so a group can tighten what the connection sets. An assignment may carry overrides for its target, recorded with a reason of 20 to 2,000 characters. The effective settings of a project say, for each setting, which template, version and assignment decided it, and what was overridden.
- **Approved exceptions waive a setting, not a template.** Drift for one setting of one project can be excepted for a limited time, approved by someone else, as for rule exceptions (M14.2).
- **Changes are change sets.** A bulk change is one change set: the exact targets, the before and after value of each setting from stored evidence, warnings, and an approval of that set. It is executed as background jobs, staged, re-reading each setting before changing it, behind its own switch and write token, off by default (M14.3 and M14.4). Automatic remediation of drift is off by default and switched on per template assignment (M14.5); it only proposes change sets, which people approve and apply (decided 2026-11-25).

## Consequences

- Templates reuse the inventory paths, evidence and evaluators; a template adds no new way to read a provider.
- A setting Baluarte cannot read cannot be put in a template, even where a provider could write it.
- The same project can be governed by a baseline that allows a range and a template that names one value; drift is reported against the template, findings against the baseline.

## Revisit when

Settings that Baluarte does not read are needed in templates, or templates must apply across workspaces.
