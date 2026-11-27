import type { ReactNode } from "react";
import { Link } from "react-router";

/** Where a topic sits in the guide's table of contents. */
export type HelpGroup =
  | "Start here"
  | "Observe"
  | "Govern"
  | "Act"
  | "Evidence and audits"
  | "Administration"
  | "GitLab and Baluarte";

export const helpGroups: readonly HelpGroup[] = [
  "Start here",
  "GitLab and Baluarte",
  "Observe",
  "Govern",
  "Act",
  "Evidence and audits",
  "Administration",
];

export interface HelpTopic {
  slug: string;
  title: string;
  /** One line for the table of contents. */
  summary: string;
  group: HelpGroup;
  /** The app pages this topic explains, as path prefixes; "/" matches only the dashboard. */
  paths: readonly string[];
  body: ReactNode;
}

/** Numbered steps. */
function Steps({ children }: { children: ReactNode }) {
  return <ol className="mb-3">{children}</ol>;
}

/** A short aside that stands out from the text. */
function Note({ children }: { children: ReactNode }) {
  return (
    <div className="alert alert-info" role="note">
      {children}
    </div>
  );
}

function Table({
  head,
  rows,
}: {
  head: readonly string[];
  rows: readonly (readonly ReactNode[])[];
}) {
  return (
    <div className="table-responsive mb-3">
      <table className="table table-vcenter card-table border">
        <thead>
          <tr>
            {head.map((cell) => (
              <th key={cell}>{cell}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {row.map((cell, column) => (
                <td key={column}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** A link to another topic of the guide. */
function See({ topic, children }: { topic: string; children: ReactNode }) {
  return <Link to={`/help/${topic}`}>{children}</Link>;
}

export const helpTopics: readonly HelpTopic[] = [
  {
    slug: "getting-started",
    title: "Getting started",
    summary: "What Baluarte does, the first steps after setup, and finding your way around.",
    group: "Start here",
    paths: ["/"],
    body: (
      <>
        <p>
          Baluarte watches a self-managed GitLab instance for you. It reads GitLab with a read-only
          token, checks every group and project against the security <strong>baselines</strong> you
          choose, and keeps a record of what it saw and when, so you can prove your posture to an
          auditor later. It does not replace GitLab&apos;s own settings: it tells you where they are
          missing or have drifted, and keeps the evidence.
        </p>
        <h2 className="h3">The cycle</h2>
        <Steps>
          <li>
            <strong>Discover.</strong> Baluarte lists the groups and projects the token can see and
            reads their settings, changes, labels and time logs on a schedule.
          </li>
          <li>
            <strong>Judge.</strong> Each rule of each applied baseline is checked for each project.
            A rule that fails opens a <strong>finding</strong>.
          </li>
          <li>
            <strong>Decide.</strong> Fix the finding in GitLab, plan a fix in Baluarte, waive it
            with a time-limited <strong>exception</strong>, or take the project out of monitoring
            with a reason.
          </li>
          <li>
            <strong>Prove.</strong> Export evidence, build an audit package or download the auditor
            report for any period.
          </li>
        </Steps>
        <h2 className="h3">First steps for an administrator</h2>
        <p>The setup wizard has already connected your identity provider. Then:</p>
        <Steps>
          <li>
            In <Link to="/settings">Settings</Link>, check the GitLab connection with{" "}
            <strong>Test connection</strong>. The token needs only <code>read_api</code>.
          </li>
          <li>
            Map your identity provider groups to Baluarte roles, so people get the right access when
            they sign in (<See topic="roles">Roles and access</See>).
          </li>
          <li>
            In <Link to="/governance">Governance</Link>, choose{" "}
            <strong>Use the recommended baseline</strong> and apply it to the GitLab instance (
            <See topic="baselines">Baselines</See>).
          </li>
          <li>
            Wait for the next discovery and evaluation. The freshness indicator in the header shows
            when GitLab was last read.
          </li>
          <li>
            Work through <Link to="/findings">Findings</Link>, starting with high severity.
          </li>
          <li>Turn on two-person review in Settings before your evidence is used in an audit.</li>
        </Steps>
        <h2 className="h3">Finding your way around</h2>
        <ul>
          <li>
            The sidebar groups the pages: <strong>Overview</strong> for daily work,{" "}
            <strong>Observe</strong> for what GitLab looks like, <strong>Act</strong> for fixes and
            waivers, <strong>Govern</strong> for the rules, <strong>Evidence</strong> for audits and{" "}
            <strong>Admin</strong> for settings. You see only the pages your role allows.
          </li>
          <li>
            The search box at the top jumps to a project, a finding number or a page. Press{" "}
            <kbd>/</kbd> to focus it, <kbd>g</kbd> then <kbd>d</kbd>, <kbd>r</kbd> or <kbd>f</kbd>{" "}
            to open the Dashboard, Review queue or Findings, and <kbd>?</kbd> to list the shortcuts.
          </li>
          <li>
            <strong>View as of a past date</strong>, in the header, shows pages as they stood on
            that date, from the evidence recorded then. A banner says so while it is on.
          </li>
          <li>The help button in the header opens the topic for the page you are on.</li>
          <li>
            On a phone you can read every page and decide what is waiting for you; creating and
            editing are done on a larger screen.
          </li>
        </ul>
      </>
    ),
  },
  {
    slug: "results",
    title: "Reading results",
    summary: "Pass, fail, unknown, unsupported and excepted, and why a result can be unknown.",
    group: "Start here",
    paths: [],
    body: (
      <>
        <p>
          Every rule, for every project, has one result. Baluarte only says a project passes or
          fails when it can back that with a recent reading from GitLab.
        </p>
        <Table
          head={["Result", "Meaning"]}
          rows={[
            [
              <strong key="o">Pass</strong>,
              "The project meets the rule, as GitLab last reported it.",
            ],
            [<strong key="o">Fail</strong>, "It does not. A failing rule opens a finding."],
            [
              <strong key="o">Unknown</strong>,
              "Baluarte could not decide, so it does not guess. Unknown never counts as compliant.",
            ],
            [
              <strong key="o">Unsupported</strong>,
              "This GitLab lacks the feature the rule checks, for example a feature of a higher tier.",
            ],
            [
              <strong key="o">Excepted</strong>,
              "A failure waived by an approved exception that has not expired.",
            ],
          ]}
        />
        <h2 className="h3">Why a result is unknown</h2>
        <p>The result names the reason. The common ones:</p>
        <ul>
          <li>
            <strong>The collection is stale or failed.</strong> GitLab has not been read
            successfully recently. Check the freshness indicator and the GitLab connection.
          </li>
          <li>
            <strong>The project is missing or inaccessible.</strong> It was deleted, or the token
            can no longer see it.
          </li>
          <li>
            <strong>The capability was not detected.</strong> Baluarte has not yet confirmed that
            this GitLab can answer the question.
          </li>
          <li>
            <strong>Incomplete ancestry.</strong> A group above the project could not be read, so a
            baseline applied there may be missing.
          </li>
          <li>
            <strong>The evidence does not check out.</strong> The stored reading no longer matches
            its digest, so it is not trusted.
          </li>
        </ul>
        <h2 className="h3">What each result shows</h2>
        <p>
          A result shows what the rule <strong>expected</strong>, what was <strong>observed</strong>
          , where the value came from in GitLab, and when GitLab last confirmed it. The observed
          value is hidden whenever the result is unknown or unsupported, so an untrusted value is
          never shown as current.
        </p>
        <Note>
          Results are evidence for your control objectives, not a certification. Your compliance
          owner decides what they mean for SOC 2, ISO 27001 or NIST 800-53.
        </Note>
      </>
    ),
  },
  {
    slug: "roles",
    title: "Roles and access",
    summary: "What administrators, analysts, auditors and managers can each do.",
    group: "Start here",
    paths: [],
    body: (
      <>
        <p>
          Roles come from your identity provider: an administrator maps provider groups to roles in
          Settings, and you get them when you sign in. A person can hold more than one role.
        </p>
        <Table
          head={["Role", "Can"]}
          rows={[
            [
              "Administrator",
              "Everything: settings, write tokens, approving exceptions and plans, the audit log and Baluarte's own compliance.",
            ],
            [
              "Analyst",
              "Read everything monitored; propose baselines, mappings and exclusions; request exceptions; create remediation, label and issue plans; build audit packages; tag projects.",
            ],
            [
              "Auditor",
              "Read everything monitored; export evidence, build audit packages and the auditor report; read the audit log and Baluarte's own compliance. Changes nothing.",
            ],
            [
              "Manager",
              "Read everything monitored, the manager space and time summaries; see time per person for the groups assigned to them; tag projects. Cannot change baselines, settings or exceptions.",
            ],
          ]}
        />
        <h2 className="h3">Two-person review</h2>
        <p>
          When review is required, whoever proposes a change cannot approve it. That covers baseline
          and mapping changes, monitoring exclusions, exceptions, label taxonomies and every plan
          that would change GitLab. Items waiting for a decision are in the{" "}
          <Link to="/reviews">Review queue</Link>.
        </p>
        <p>
          The page or button for an action you are not allowed is hidden, and the server refuses it
          regardless.
        </p>
      </>
    ),
  },
  {
    slug: "daily-work",
    title: "Dashboard and review queue",
    summary: "Your daily view of posture, and the decisions waiting for you.",
    group: "Start here",
    paths: ["/reviews"],
    body: (
      <>
        <h2 className="h3">Dashboard</h2>
        <p>
          The <Link to="/">Dashboard</Link> shows the latest evaluation: whether it ran, how many
          rule results pass, fail or are unknown, and open findings by severity. Each number links
          to the list behind it.
        </p>
        <h2 className="h3">Review queue</h2>
        <p>
          The <Link to="/reviews">Review queue</Link> lists everything waiting for a second person:
          baseline and mapping changes, exclusions, exceptions, taxonomies and plans. Items you
          proposed are marked, because you cannot decide them yourself.
        </p>
        <Steps>
          <li>Open an item to see exactly what would change and why.</li>
          <li>
            Approve it, or reject it with a note. A plan&apos;s approval names its digest, so it
            covers exactly the steps you saw.
          </li>
          <li>Approved changes take effect at once and are recorded in the audit log.</li>
        </Steps>
        <p>
          With notifications on, Baluarte sends a daily reminder for items waiting longer than a few
          days, and a weekly digest (<See topic="settings">Settings</See>).
        </p>
      </>
    ),
  },
  {
    slug: "gitlab-gaps",
    title: "Gaps in GitLab that Baluarte covers",
    summary: "What GitLab leaves to you, by tier, and how Baluarte fills each gap.",
    group: "GitLab and Baluarte",
    paths: [],
    body: (
      <>
        <p>
          GitLab enforces settings one group or project at a time, and several controls auditors ask
          about need GitLab Premium or Ultimate. Baluarte works the same on every tier. It checks
          the whole instance against one baseline, records evidence with digests, and keeps reviewed
          decisions with their reasons.
        </p>
        <Note>
          Baluarte <strong>detects and records</strong>; it does not block a push or a merge. Where
          GitLab can enforce a control on your tier, turn it on in GitLab too, and use Baluarte to
          prove it stays on everywhere.
        </Note>
        <Table
          head={["The gap in GitLab", "How Baluarte covers it", "Where"]}
          rows={[
            [
              "Settings are per project or group. Nothing shows which of hundreds of projects are public, have an unprotected default branch, or lack two-factor sign-in.",
              "One versioned baseline, applied to the instance or a group and inherited below, checks every project and opens a finding for each gap.",
              <Link key="w" to="/governance">
                Governance
              </Link>,
            ],
            [
              "On GitLab Free, GitLab records no audit events for setting changes such as project visibility or branch protection. Audit events need Premium or Ultimate.",
              "Every reading is kept as an observation with a SHA-256 digest, so you can show how a setting stood on any past date. Every decision in Baluarte is in its audit log.",
              <Link key="w" to="/evidence">
                Evidence
              </Link>,
            ],
            [
              "On GitLab Free, a project cannot require merge request approvals. Approval rules need Premium or Ultimate.",
              "The change rules check every merge request merged into the default branch: approved by someone other than its author, its pipeline passed, and only reviewed code deployed to production. Direct pushes are found and counted as unreviewed.",
              <Link key="w" to="/changes">
                Changes
              </Link>,
            ],
            [
              "GitLab shows settings as they are now, not as they were during an audit period.",
              "Pick a past date in the header to see results, findings and controls as they stood then. Evidence packages cover a period and sample changes reproducibly.",
              <See key="w" topic="evidence">
                Evidence
              </See>,
            ],
            [
              "GitLab has no evidence package or report mapped to SOC 2, ISO 27001 or NIST 800-53 for every tier.",
              "Rules are mapped to framework controls. Export evidence per control, build an audit package with a change sample, or download the auditor report PDF, all verifiable offline.",
              <Link key="w" to="/controls">
                Controls
              </Link>,
            ],
            [
              "A setting GitLab does not meet can only be fixed or ignored; there is no record of an accepted risk.",
              "Exceptions waive one rule for one project with a justification, a second person's approval and an expiry of at most a year. When it expires, the finding reopens.",
              <Link key="w" to="/exceptions">
                Exceptions
              </Link>,
            ],
            [
              "Group labels are inherited, but nothing keeps names, colours and descriptions the same across top-level groups.",
              "One label taxonomy for every group, drift reported where it happens (missing, misnamed, misplaced, duplicate, wrong colour), and reviewed plans that fix it in GitLab.",
              <Link key="w" to="/labels">
                Labels
              </Link>,
            ],
            [
              "GitLab records time spent, but does not flag closed issues with commits and no time logged, or closed issues without an estimate.",
              "Closed-issue rules label issues missing time and set estimates from time spent, through reviewed plans. Time is summarized by group, label, milestone and tag, with timecards.",
              <Link key="w" to="/manager/time">
                Time
              </Link>,
            ],
            [
              "Groups follow how code is organized, not how teams report. Posture across teams that share groups is hard to compare.",
              "Tags such as team=payments group projects across the hierarchy. The manager space rolls posture, change review, label drift and time up by group or tag, with trends.",
              <Link key="w" to="/manager">
                Manager
              </Link>,
            ],
            [
              "Fixing many projects by hand leaves no plan, no second approver and no proof that each change took effect.",
              "Remediation plans list each change with its evidence, need a second person's approval of the exact digest, and, when allowed, apply each step once and read it back.",
              <Link key="w" to="/remediation">
                Remediation
              </Link>,
            ],
            [
              "Sandbox and personal projects sit next to production code, and leaving them out of a review is undocumented.",
              "Exclusions from monitoring carry a justification and are reviewed. Excluded projects stay listed and flagged, so an auditor sees what was left out and why.",
              <Link key="w" to="/inventory">
                Inventory
              </Link>,
            ],
            [
              "GitLab Ultimate's security policies (required scans and approval policies) are configured per group, with no single view of which projects they reach.",
              "On Ultimate, Baluarte reads the policies and shows which projects each reaches, and the security policy rules check that the required scans and approvals apply.",
              <Link key="w" to="/governance/gitlab-security-policies">
                Security policies
              </Link>,
            ],
          ]}
        />
        <h2 className="h3">Checks that work on every tier</h2>
        <p>
          The recommended baseline uses only rules that work on GitLab Free: project visibility,
          default branch protection, merge request approval settings, privileged members and
          inactive accounts, two-factor sign-in, project access token lifetime, and the three change
          rules. On Free, set the approval rule&apos;s minimum to 0 to check only the approval
          settings, and rely on the change rules for proof of review. The two security policy rules
          are offered only when GitLab can report security policies.
        </p>
        <h2 className="h3">What Baluarte cannot see</h2>
        <ul>
          <li>
            Only what the token can see. A token that is not a GitLab administrator sees only its
            user&apos;s groups and projects; the Dashboard and every export say which population was
            covered.
          </li>
          <li>
            Only what GitLab reports. Where a feature is missing on your tier or version, the result
            is unsupported or unknown, never a pass.
          </li>
          <li>
            On GitLab Free, Baluarte&apos;s audit log is the only record of a change it applies.
            Keep it, and ship it to your log platform (<See topic="audit-log">Audit log</See>).
          </li>
        </ul>
      </>
    ),
  },
  {
    slug: "inventory",
    title: "Inventory and monitoring scope",
    summary: "The groups and projects Baluarte sees, how fresh they are, and leaving some out.",
    group: "Observe",
    paths: ["/inventory"],
    body: (
      <>
        <p>
          <Link to="/inventory">Inventory</Link> lists every group and project the GitLab token can
          see, with when each was last read and any error reading it. Open one to see its details,
          its findings and the baselines that apply to it.
        </p>
        <ul>
          <li>
            <strong>Archived projects</strong> are listed but never checked.
          </li>
          <li>
            <strong>Missing or inaccessible</strong> projects were not found, or were refused, at
            the last discovery. Their results become unknown.
          </li>
          <li>
            <strong>Freshness</strong> says when each kind of collection last succeeded. A failed
            collection is never hidden behind an older success.
          </li>
        </ul>
        <h2 className="h3">Leaving projects out of monitoring</h2>
        <p>
          Research, sandbox and personal projects often should not be judged against your baselines.
          To leave them out:
        </p>
        <Steps>
          <li>
            Open the project or group and choose <strong>Exclude from monitoring</strong>. You can
            also exclude every personal-namespace project at once.
          </li>
          <li>Give a justification. It is kept with the exclusion and shown to auditors.</li>
          <li>
            With review on, someone else approves it before it takes effect, as for baseline
            changes.
          </li>
        </Steps>
        <p>
          An excluded project stays in the inventory, flagged. It gets no results, its open findings
          resolve, and exports list it with the exclusion in force. Exclusion changes nothing in
          GitLab and deletes no evidence. Ending an exclusion brings the projects back at the next
          evaluation; it needs a reason but no second person, because it widens monitoring.
        </p>
      </>
    ),
  },
  {
    slug: "findings",
    title: "Findings",
    summary: "Failing rules, what they show, and the ways to deal with one.",
    group: "Observe",
    paths: ["/findings"],
    body: (
      <>
        <p>
          A finding is opened when a rule fails for a project, and stays open until a later
          evaluation shows the project passes. It keeps its history, so the same problem returning
          reopens the same finding.
        </p>
        <p>
          <Link to="/findings">Findings</Link> can be filtered by state, result, severity, baseline,
          rule and project. Open one to see the expected and observed values, where the value came
          from, every result over time, and the evidence behind it.
        </p>
        <h2 className="h3">Dealing with a finding</h2>
        <Table
          head={["If", "Then"]}
          rows={[
            [
              "It should be fixed",
              "Fix it in GitLab, or create a remediation plan for visibility and branch protection. The finding resolves at the next evaluation after GitLab is read again.",
            ],
            [
              "It is an accepted risk for now",
              <>
                Choose <strong>Request an exception</strong> with a justification and an expiry (
                <See topic="exceptions">Exceptions</See>).
              </>,
            ],
            [
              "The project should not be judged at all",
              <>
                Exclude it from monitoring (<See topic="inventory">Inventory</See>).
              </>,
            ],
            [
              "The rule is wrong for everyone",
              <>
                Propose a baseline change (<See topic="baselines">Baselines</See>).
              </>,
            ],
          ]}
        />
        <p>
          Findings are resolved, never deleted. A resolved finding records why: the project now
          passes, the rule no longer applies, the project was archived or excluded.
        </p>
      </>
    ),
  },
  {
    slug: "changes",
    title: "Code changes",
    summary: "Merged merge requests, direct pushes and deployments, with their review evidence.",
    group: "Observe",
    paths: ["/changes"],
    body: (
      <>
        <p>
          Configuration rules say how a project is set up today. An auditor testing change
          management asks something else: for each change that reached the default branch, who
          approved it, did its pipeline pass, and what was deployed.{" "}
          <Link to="/changes">Changes</Link> answers that per change.
        </p>
        <Table
          head={["Kind", "What it is"]}
          rows={[
            ["Merge request", "A merge request merged into the project's default branch."],
            ["Direct push", "A commit on the default branch that no merged merge request brought."],
            ["Deployment", "A successful deployment to a production environment."],
          ]}
        />
        <p>Each change is judged:</p>
        <ul>
          <li>
            <strong>Review:</strong> approved by enough people other than its author. A direct push
            fails unless your baseline allows it.
          </li>
          <li>
            <strong>Pipeline:</strong> the newest pipeline for its last commit succeeded, with
            passing tests when the baseline requires them.
          </li>
          <li>
            <strong>Deployment:</strong> what went to production was a reviewed change.
          </li>
        </ul>
        <p>
          The three change rules in a baseline (<code>changes-reviewed</code>,{" "}
          <code>changes-pipeline-passed</code> and <code>deployments-reviewed</code>) fail a project
          when any change in their window fails. Administrators name the production environments in
          Settings.
        </p>
      </>
    ),
  },
  {
    slug: "controls",
    title: "Controls",
    summary: "SOC 2, ISO 27001 and NIST 800-53 controls, and how rules support them.",
    group: "Observe",
    paths: ["/controls"],
    body: (
      <>
        <p>
          <Link to="/controls">Controls</Link> lists the controls of each framework and their status
          from the latest evaluation of the rules mapped to them. Open one to see the mapped rule
          types, results per rule and the failing projects.
        </p>
        <ul>
          <li>
            A control is <strong>met</strong> when every result of its mapped rules passes or is
            excepted, <strong>not met</strong> when one fails, and <strong>unknown</strong> when
            none fails but some cannot be decided.
          </li>
          <li>
            A control with no applied rule has no automated evidence. Add the rule to a baseline, or
            record how it is met another way.
          </li>
        </ul>
        <h2 className="h3">Changing the mapping</h2>
        <p>
          The built-in mapping is guidance. Your compliance owner can change it: add or remove a
          rule type from a control, or choose <strong>Mark not applicable</strong> or manual with a
          justification and a review-by date. Changes follow the same review as baseline changes and
          are listed in every export.
        </p>
        <p>
          <strong>Export evidence for this control</strong> exports just the results, findings and
          exceptions behind one control (<See topic="evidence">Evidence</See>).
        </p>
      </>
    ),
  },
  {
    slug: "baselines",
    title: "Baselines",
    summary: "The rules every project is checked against, where they apply, and changing them.",
    group: "Govern",
    paths: ["/governance", "/policies", "/policy-versions"],
    body: (
      <>
        <p>
          A <strong>baseline</strong> is a named, versioned set of rules. It applies to nothing
          until it is assigned to the GitLab instance, a group or a project; groups and projects
          below inherit it. An assignment pins one exact version, so saving a new version never
          silently changes what is enforced.
        </p>
        <h2 className="h3">Starting with the recommended baseline</h2>
        <Steps>
          <li>
            In <Link to="/governance">Governance</Link>, choose{" "}
            <strong>Use the recommended baseline</strong>. It turns on every rule that works on any
            GitLab tier, with values chosen for a typical audit.
          </li>
          <li>Adjust values where your organization differs, then propose it.</li>
          <li>Assign it to the GitLab instance, or to the groups it should cover.</li>
          <li>With review on, another person approves each change before it takes effect.</li>
        </Steps>
        <h2 className="h3">Rules</h2>
        <Table
          head={["Rule", "Checks"]}
          rows={[
            ["Project visibility", "The project is private or internal, as allowed."],
            ["Protected default branch", "A protection covers the default branch."],
            [
              "Merge request approvals",
              "The project requires approvals, authors cannot approve their own, and pushes reset approvals.",
            ],
            [
              "Privileged members",
              "Few Maintainers and Owners, and no blocked or deactivated account keeps access.",
            ],
            ["Two-factor required", "A group above the project, or the instance, requires it."],
            ["Access token lifetime", "Project access tokens expire within the limit."],
            [
              "Changes reviewed, pipeline passed, deployments reviewed",
              <>
                Every change in the window was reviewed and passed its pipeline (
                <See topic="changes">Code changes</See>).
              </>,
            ],
            [
              "Required scans and approval policies",
              "GitLab Ultimate security policies run the required scans and require approvals.",
            ],
            [
              "Label consistency",
              <>
                No drift from the label taxonomy (<See topic="labels">Labels</See>).
              </>,
            ],
          ]}
        />
        <h2 className="h3">Coverage and changes</h2>
        <ul>
          <li>
            The <strong>Rule coverage</strong> card shows how many rule types are in force and which
            controls have no automated evidence. <strong>Add recommended rules</strong> adds any you
            are missing to a new version.
          </li>
          <li>
            Each version shows what changed from the last, who proposed it and who approved it.
            Removing a rule resolves its findings as no longer applying.
          </li>
          <li>
            GitLab security policies, which GitLab itself enforces on Ultimate, are listed under
            Governance with the projects each reaches.
          </li>
        </ul>
      </>
    ),
  },
  {
    slug: "templates",
    title: "Configuration templates",
    summary: "Exact settings for repositories, where they apply and where each value comes from.",
    group: "Govern",
    paths: ["/governance/templates", "/governance/change-sets"],
    body: (
      <>
        <p>
          A <strong>configuration template</strong> names exactly how repositories should be set up:
          visibility, default branch protection, merge request approvals, GitHub security features
          and label consistency. A baseline says what is acceptable; a template says what to set.
          Assigning a template changes nothing on GitLab or GitHub yet, and it does not make a
          project compliant: baselines still judge that.
        </p>
        <h2 className="h3">Versions and inheritance</h2>
        <p>
          Saving changed YAML under a name adds its next version; saved versions never change. A
          template can extend one exact version of another with <code>extends</code>, such as{" "}
          <code>base-repository@2</code>, and replaces the settings it names.
        </p>
        <h2 className="h3">Where templates apply</h2>
        <Steps>
          <li>
            In <Link to="/governance/templates">Governance, Templates</Link>, assign a version to
            the whole connection, a group or a project. Each holds one template.
          </li>
          <li>
            For each setting, the nearest assignment that sets it decides: the project&apos;s own,
            then its group and that group&apos;s parents, then the connection.
          </li>
          <li>
            An assignment can override settings for its target, with a reason of at least 20
            characters.
          </li>
          <li>
            Look up a project to see every setting in force, which template, version and assignment
            decided it, and what it replaced.
          </li>
        </Steps>
        <p>
          Some settings have no equivalent on a provider, or depend on its plan; the page lists
          which.
        </p>
        <h2 className="h3">Drift</h2>
        <p>
          Each setting in force is compared with what was last collected: <strong>aligned</strong>,{" "}
          <strong>drift</strong>, <strong>excepted</strong>, <strong>unknown</strong> when the
          evidence cannot tell (never counted as aligned), or <strong>unsupported</strong> when the
          provider or plan has no such setting. <strong>Alignment</strong> sums this up for the
          connection by template and group and lists the projects needing attention.
        </p>
        <p>
          To let one project differ from one setting for a while, request an exception on its
          drifting row with a justification. Someone else approves or rejects it; it waives that
          drift until it ends, and the page flags those ending within 14 days.
        </p>
        <p>
          To put drift right, choose <strong>Propose a fix</strong> on a project&apos;s drift, or
          for every drifting project under Alignment. It builds a change set from each
          project&apos;s own templates and overrides, to review and approve like any other.
        </p>
        <p>
          An assignment can also <strong>propose fixes for drift automatically</strong>. After each
          evaluation, Baluarte builds such a change set for what that assignment decides. People
          still approve and apply it; a rejected proposal is not repeated unless the drift changes.
        </p>
        <h2 className="h3">Change sets</h2>
        <p>
          In <Link to="/governance/change-sets">Governance, Change sets</Link>, choose projects by
          group, path, tag (such as owner or business unit), template, drift or findings, and the
          values wanted (a template version, settings, each project&apos;s own templates to fix
          drift, or chosen tag keys written to GitLab topics as key:value). Baluarte lists each
          project&apos;s current value from evidence and the value after, and skips what it cannot
          tell, what an approved exception allows, what a baseline would forbid and what the
          provider lacks.
        </p>
        <p>
          Someone other than its creator approves a set; one that weakens protection or removes
          access needs two such people. Approval is refused if the evidence has moved since the set
          was built. Whoever approved it never applies, continues or rolls it back: someone else
          does. Each set records why it was wanted, who asked, approved and ran it, and the controls
          its settings support, and evidence packages include it.
        </p>
        <p>
          An approved set is applied in stages only when the deployment turns template writes on and
          an admin stores a template write token in Settings. Each setting is read again first, a
          value someone changed since is left alone, and each write is read back. A set can be
          continued, cancelled and rolled back; changes that cannot be fully undone are named before
          applying.
        </p>
        <p>
          After each stage, discovery collects the projects again. The{" "}
          <strong>evidence check</strong> then shows each applied change as verified, changed again,
          awaiting collection, or cannot tell.
        </p>
      </>
    ),
  },
  {
    slug: "release-practices",
    title: "Release practices",
    summary: "How projects are meant to reach production, and whether their releases do.",
    group: "Govern",
    paths: ["/governance/release-practices", "/governance/releases"],
    body: (
      <>
        <p>
          A <strong>release practice</strong> says how projects are meant to reach production. Start
          from a preset and adjust its parameters, or build a custom one:
        </p>
        <ul>
          <li>
            <strong>Merge request to the default branch</strong>: production deploys the default
            branch, and each change is reviewed by someone other than its author.
          </li>
          <li>
            <strong>Protected release tag with deployment approval</strong>: production deploys a
            tag such as <code>*-release</code>, approved through GitLab&apos;s deployment approvals.
            Whoever created the tag may not approve it, and the deployer may not be an approver.
          </li>
          <li>
            <strong>Release branch</strong>: production deploys a branch such as{" "}
            <code>release/*</code>.
          </li>
        </ul>
        <p>
          Assign a practice to the whole connection, a group or a project; the nearest assignment
          decides. Changing a practice is audited with its values before and after.
        </p>
        <p>
          <strong>Projects and their releases</strong> compares each project&apos;s production
          deployments in the last 90 days with its practice: it follows it, differs (with the refs
          it did not expect), is not known, or has no releases. The deployer is who GitLab records
          on the deployment: a deploy job that runs automatically after approval is recorded as
          whoever started the pipeline, for a tag its creator.
        </p>
        <p>
          The baseline rule <code>gitlab.release.follows_practice</code> judges each release against
          its practice, including who approved it. <strong>Releases</strong> lists recorded
          production releases with who started, approved and deployed each, and{" "}
          <strong>Export audit release</strong> builds a package of one release for an auditor: the
          release, its notes, the merge requests since the previous release with their approvals,
          pipelines and tests, the protections in force and the pipeline&apos;s reports. It never
          holds source code, and <code>baluarte-verify-export</code> checks it offline.
        </p>
        <p>
          GitLab can let whoever starts a pipeline approve its deployments, so a tag&apos;s creator
          could approve their own release. The rule{" "}
          <code>gitlab.deployment.triggerer_cannot_approve</code> fails when it does, and the
          template setting <code>deployments.forbid_pipeline_triggerer_approval</code> turns it off
          through a change set.
        </p>
      </>
    ),
  },
  {
    slug: "labels",
    title: "Labels",
    summary: "One label taxonomy for every group, label drift, and reviewed label plans.",
    group: "Govern",
    paths: ["/labels"],
    body: (
      <>
        <p>
          A <strong>label taxonomy</strong> names the labels every group should have, with their
          colours, descriptions and old names (aliases). Each label is defined once, on the
          top-level group, so every project below inherits it.
        </p>
        <Steps>
          <li>
            In <Link to="/labels">Labels</Link>, choose <strong>Propose a label taxonomy</strong>,
            list the labels and the groups it covers, and propose it. With review on, someone else
            approves it.
          </li>
          <li>
            After the next collection, the page shows the <strong>drift</strong> per group and
            project.
          </li>
          <li>Add the label consistency rule to a baseline to turn drift into findings.</li>
        </Steps>
        <Table
          head={["Drift", "Meaning"]}
          rows={[
            ["Missing", "The label is defined nowhere in the hierarchy."],
            ["Misnamed", "It exists under an alias, or another case or spacing."],
            ["Misplaced", "It is defined below the top-level group only."],
            ["Duplicate", "It is defined below the top-level group as well, shadowing it."],
            ["Colour, description", "They differ from the taxonomy."],
            ["Unknown label", "A label the taxonomy does not name (strict taxonomies only)."],
          ]}
        />
        <h2 className="h3">Fixing drift with a label plan</h2>
        <p>
          A <Link to="/labels/plans">label plan</Link> lists the GitLab changes that would remove
          the drift. Another person approves it with <strong>Approve this digest</strong>, then{" "}
          <strong>Apply in GitLab</strong> makes the changes. Applying needs label writes switched
          on in the deployment and a label write token in Settings; without them, follow the plan by
          hand. A plan not applied within 24 hours expires.
        </p>
      </>
    ),
  },
  {
    slug: "remediation",
    title: "Remediation plans",
    summary: "Planning, approving and applying fixes for failing findings.",
    group: "Act",
    paths: ["/remediation"],
    body: (
      <>
        <p>
          A remediation plan lists the GitLab changes that would fix a set of failing findings, each
          with the evidence it rests on. Creating, submitting and approving a plan change nothing in
          GitLab. Two fixes can be planned:
        </p>
        <ul>
          <li>
            <strong>Restrict a project&apos;s visibility</strong> to the least visible level the
            rule allows. A plan never makes a project more visible.
          </li>
          <li>
            <strong>Protect an unprotected default branch</strong>, with push, merge and unprotect
            access for Maintainers.
          </li>
        </ul>
        <p>Other fixes, such as group visibility or approval rules, are made by hand in GitLab.</p>
        <h2 className="h3">From findings to a fix</h2>
        <Steps>
          <li>
            In <Link to="/remediation">Remediation</Link>, choose{" "}
            <strong>Create a dry-run plan</strong> and pick the findings, a rule or a project.
            Findings that cannot be planned are listed with the reason.
          </li>
          <li>
            Check each step&apos;s current and intended value, then{" "}
            <strong>Submit for approval</strong>.
          </li>
          <li>
            Someone else with approval rights approves it. The approval binds to the plan&apos;s
            digest, and is refused if the findings or evidence have changed.
          </li>
          <li>
            Apply it before it expires (24 hours by default): either{" "}
            <strong>Apply to GitLab</strong>, or make the changes by hand following the steps.
          </li>
        </Steps>
        <h2 className="h3">Applying from Baluarte</h2>
        <p>
          <strong>Apply to GitLab</strong> is offered only when the deployment allows remediation
          writes and an administrator has stored a remediation write token in Settings. Then each
          step is:
        </p>
        <ol>
          <li>read again with the read-only token, and skipped if already done;</li>
          <li>left alone if the project changed since the plan was made;</li>
          <li>otherwise written once, never retried;</li>
          <li>counted as applied only when a fresh read shows the intended value.</li>
        </ol>
        <p>
          The plan stops at the first failed step. Every request and its outcome is in the audit
          log, and the project is read again so findings resolve the normal way.
        </p>
        <Note>
          On GitLab Free, GitLab keeps no audit record of these changes, so Baluarte&apos;s audit
          log is the record. When applying by hand, note the plan ID and digest in your ticket.
        </Note>
      </>
    ),
  },
  {
    slug: "exceptions",
    title: "Exceptions",
    summary: "Time-limited, approved waivers for a failing rule on one project.",
    group: "Act",
    paths: ["/exceptions"],
    body: (
      <>
        <p>
          An exception records an accepted risk: one rule, for one project, until a date. While it
          is in force, the failure shows as <strong>excepted</strong> and its control counts it as
          met, but the expected and observed values and the evidence are kept.
        </p>
        <Steps>
          <li>
            On a failing finding, choose <strong>Request an exception</strong>.
          </li>
          <li>Give a justification and an expiry, at most a year away.</li>
          <li>
            An administrator other than you approves or rejects it in the{" "}
            <Link to="/reviews">Review queue</Link>.
          </li>
        </Steps>
        <ul>
          <li>Only a confirmed failure can be waived; an unknown result cannot.</li>
          <li>
            When the exception expires or is revoked, the finding reopens. To extend it, request a
            new one.
          </li>
          <li>
            Exceptions are never deleted. <Link to="/exceptions">Exceptions</Link> lists them all,
            with who requested and decided each.
          </li>
        </ul>
      </>
    ),
  },
  {
    slug: "time",
    title: "Time tracking",
    summary: "Time spent and estimated across groups, timecards, and closed-issue rules.",
    group: "Act",
    paths: ["/manager/time", "/manager/timecard", "/time"],
    body: (
      <>
        <p>
          Baluarte reads the time people log in GitLab and the estimates on issues, so you can see
          effort across groups, projects, labels, milestones and tags without a separate timesheet
          tool. Time is never judged by a rule or mapped to a control.
        </p>
        <ul>
          <li>
            <strong>Summaries</strong> (<Link to="/manager/time">Manager → Time</Link>) total time
            spent and estimated, filtered by period, group, tag, label or milestone.
          </li>
          <li>
            <strong>Time per person</strong> and <strong>timecards</strong> are for administrators
            and managers only, and a manager sees only the groups assigned to them. Every view of
            time per person is recorded in the audit log.
          </li>
        </ul>
        <h2 className="h3">Closed issues</h2>
        <p>Every closed issue should account for its time. Two rules apply:</p>
        <ul>
          <li>
            A closed issue with commits but no time logged gets a <code>missing-time</code> label.
          </li>
          <li>
            A closed issue with time spent but no estimate gets the time spent, rounded up, as its
            estimate. An existing estimate is never changed.
          </li>
        </ul>
        <p>
          <Link to="/time/issue-plans">Closed issue time</Link> shows what a plan would change now.
          The changes reach GitLab only through a plan that another person approves and that is then
          applied, which needs issue writes switched on in the deployment and the label write token.
        </p>
      </>
    ),
  },
  {
    slug: "manager",
    title: "Manager space and tags",
    summary: "Posture, change review, label drift and time by group or tag, with trends.",
    group: "Act",
    paths: ["/manager"],
    body: (
      <>
        <p>
          The <Link to="/manager">Manager</Link> space answers the questions managers ask across
          many groups: how each team&apos;s posture compares, whether changes are reviewed, whether
          labels are consistent, and where the effort goes.
        </p>
        <ul>
          <li>
            <strong>Overview:</strong> rules passing, open findings, changes reviewed, label drift
            and time spent, rolled up by group or by tag.
          </li>
          <li>
            <strong>Trends:</strong> the same figures over weeks or months.
          </li>
        </ul>
        <h2 className="h3">Tags</h2>
        <p>
          Tags are <code>key=value</code> pairs kept in Baluarte, such as <code>team=payments</code>{" "}
          or <code>tier=1</code>, that group projects across the GitLab hierarchy. Create them under{" "}
          <Link to="/manager/tags">Manager → Tags</Link> and assign them to groups or projects; a
          tag on a group applies to everything below it, and the nearest value wins. Tags never
          change GitLab and are not evidence: they only group results.
        </p>
        <h2 className="h3">Manager scopes</h2>
        <p>
          An administrator assigns groups to each manager in Settings. Scopes decide only whose time
          per person a manager may see; the overview and trends show every group.
        </p>
      </>
    ),
  },
  {
    slug: "issue-register",
    title: "Issue register",
    summary: "Which issues are auditable, why, and how to change that.",
    group: "Evidence and audits",
    paths: ["/register", "/register/rules", "/register/issues", "/register/controls"],
    body: (
      <>
        <p>
          The register lists the issues an auditor should look at. An issue is in it when a
          selection rule matches it or someone included it, and each row says which.
        </p>
        <ul>
          <li>
            <strong>Selection rules</strong> match issues by label (any of the rule&apos;s labels),
            project and issue type; an issue must meet every criterion a rule sets. Each rule gives
            its issues a category: engineering, security finding, exception or remediation. When
            several rules match, the oldest one&apos;s category counts.
          </li>
          <li>
            <strong>Including</strong> an issue makes it auditable whatever the rules say, and can
            set its category. <strong>Excluding</strong> one takes a rule-selected issue out. Both
            need a reason of at least 20 characters. Excluded issues stay listed, with the reason,
            under &ldquo;Show excluded issues&rdquo;, so nothing leaves the register unseen.
          </li>
          <li>
            Rules and decisions are ended, never edited or deleted, and every change is in the{" "}
            <Link to="/audit-log">audit log</Link>. Administrators, analysts and auditors can change
            the register; everyone can read it.
          </li>
          <li>
            Issues are read from GitLab after each project discovery, and only for workspaces with a
            register. &ldquo;Read issues now&rdquo; reads them at once. For auditable issues
            Baluarte then captures the description, comments, history, linked issues, merge requests
            with approvals and pipelines, releases and attachments; open an issue to see them.
            Anything it could not read is listed as a gap, and attachments are kept with their
            SHA-256 and only offered as downloads.
          </li>
          <li>
            <strong>GitHub</strong> issues are read after each GitHub collection, pull requests left
            out. Their evidence is the description, comments, timeline, and the issues and pull
            requests that mention them, with reviews and workflow runs. Files attached on GitHub are
            listed as gaps rather than downloaded.
          </li>
          <li>
            <strong>Controls</strong>: on an issue&apos;s page, map it to a framework control and
            cite the captured items that show the control working, each with why. While review is
            required someone else with permission to change baselines approves the mapping, from the
            issue or the review queue. Mappings are revised or withdrawn, never edited, and rest on
            the capture they cite.
          </li>
          <li>
            The <strong>Controls</strong> tab counts the issues mapped to each control and their
            gaps. Choose an <strong>audit period</strong> to see which cited evidence falls inside
            it and which is later remediation.
          </li>
          <li>
            <strong>Export</strong> a package for one issue (on its page), the issues you tick in
            the register, or the controls you tick on the Controls tab, against the chosen period.
            It holds a summary, the issue-to-control index, the cited evidence, each attachment
            once, gaps and redactions. Check it with <code>baluarte-verify-export</code> and the
            manifest hash from the <code>register.export_completed</code> audit event.
          </li>
          <li>
            &ldquo;Open between&rdquo; keeps issues open at any time in the period. Closing an issue
            is never taken as proof that a control works.
          </li>
        </ul>
      </>
    ),
  },
  {
    slug: "evidence",
    title: "Evidence for audits",
    summary: "Exports, audit packages with change samples, the auditor report, and verifying them.",
    group: "Evidence and audits",
    paths: ["/evidence"],
    body: (
      <>
        <p>
          Every reading Baluarte takes from GitLab is kept as an <strong>observation</strong> with a
          SHA-256 digest, and every result names the observation it judged. Evidence is kept for the
          retention period your operating profile sets, 365 days or more for SOC 2, ISO 27001 and
          NIST 800-53.
        </p>
        <Table
          head={["Output", "Use it for", "Who"]}
          rows={[
            [
              <strong key="o">Evidence export</strong>,
              "Results, findings, exceptions and inventory for a scope: a group, projects, a rule, a control or a period. JSON, or a ZIP of CSV files.",
              "Administrators and auditors",
            ],
            [
              <strong key="o">Audit package</strong>,
              "Everything for an audit period, with a reproducible random sample of changes for the auditor to test.",
              "Administrators, analysts and auditors",
            ],
            [
              <strong key="o">Auditor report</strong>,
              "A PDF for one framework and period: each control's status, failing projects, exceptions, attestations and exclusions.",
              "Administrators and auditors",
            ],
          ]}
        />
        <h2 className="h3">Building an audit package</h2>
        <Steps>
          <li>
            In <Link to="/evidence">Evidence</Link>, choose the period, and optionally a framework
            control and groups.
          </li>
          <li>
            Set the sample size. Keep the seed shown with the package: the same seed draws the same
            sample again.
          </li>
          <li>Hand the ZIP to the auditor, with the report included if they want one.</li>
        </Steps>
        <h2 className="h3">Verifying</h2>
        <p>
          Every export carries a manifest with the SHA-256 of each file. Anyone can check a ZIP
          offline, without Baluarte or its database, with <code>sha256sum -c SHA256SUMS</code>, or
          more fully with <code>baluarte-verify-export</code>. The report is a summary; to check it,
          compare it with the package for the same period.
        </p>
        <Note>
          Exports name people only by internal ID (<code>user:42</code>). The audit log resolves who
          that was.
        </Note>
      </>
    ),
  },
  {
    slug: "audit-log",
    title: "Audit log and Baluarte's own compliance",
    summary: "Who did what in Baluarte, and how Baluarte itself meets your frameworks.",
    group: "Evidence and audits",
    paths: ["/audit-log", "/compliance"],
    body: (
      <>
        <h2 className="h3">Audit log</h2>
        <p>
          The <Link to="/audit-log">Audit log</Link> records every action in Baluarte: sign-ins,
          settings, proposals and decisions, exports, plans and every write to GitLab. Events cannot
          be edited or deleted through Baluarte. Each shows whether its digest still matches its
          details.
        </p>
        <p>
          Turn on audit log shipping in Settings to copy every event to your syslog or log search
          platform. The shipped copy is the independent record if the database is restored or
          tampered with.
        </p>
        <h2 className="h3">Baluarte compliance</h2>
        <p>
          Baluarte keeps audit evidence, so it is itself in scope for an audit. The{" "}
          <strong>operating profile</strong> says which frameworks Baluarte is run to, and{" "}
          <Link to="/compliance">Compliance</Link> checks Baluarte against them:
        </p>
        <ul>
          <li>
            <strong>Settings</strong> such as retention, session timeouts, two-person review and
            backups, met when the value meets the framework&apos;s minimum.
          </li>
          <li>
            <strong>Checks</strong> Baluarte observes itself, such as audit log shipping, off-site
            backup copies and restore tests. Some also accept an attestation when you do it another
            way.
          </li>
          <li>
            <strong>Attestations</strong> an administrator makes for what Baluarte cannot see, such
            as encryption at rest. They expire and must be renewed.
          </li>
        </ul>
        <p>
          A setting below the minimum is refused unless a compliance exception, approved by a second
          person and with an expiry, is in force for that control.
        </p>
        <Note>
          Development mode lets one person decide their own items while testing. Every page shows a
          banner while it is on. Never use it on an instance whose evidence is meant for an audit.
        </Note>
      </>
    ),
  },
  {
    slug: "workspaces",
    title: "Workspaces",
    summary: "Keep organizations apart: their connections, data, evidence and who may see them.",
    group: "Administration",
    paths: ["/workspaces"],
    body: (
      <>
        <p>
          A workspace holds its own connections, inventory, baselines, findings, exceptions, plans,
          tags, exports and audit trail. Nothing in one workspace is visible from another.
        </p>
        <ul>
          <li>
            <strong>Who works where:</strong> each workspace maps identity provider groups to roles.
            The default workspace uses the groups on the <Link to="/settings">Settings</Link> page.
            Roles are read at sign-in, so a change applies from each person&apos;s next sign-in.
          </li>
          <li>
            <strong>Switching:</strong> when you can work in more than one workspace or connection,
            a selector appears at the top of every page. Switching starts again from the dashboard.
          </li>
          <li>
            <strong>Managing:</strong> administrators of the default workspace create workspaces and
            change their groups on <Link to="/workspaces">Workspaces</Link>. To add a
            workspace&apos;s first connection, switch to it and use{" "}
            <Link to="/connections">Connections</Link>.
          </li>
          <li>
            Every audit event records the workspace it happened in. Sign-ins and deployment settings
            belong to the default workspace.
          </li>
        </ul>
      </>
    ),
  },
  {
    slug: "connections",
    title: "Connections",
    summary:
      "Collect from GitLab deployments and GitHub organizations, each with its own credential, schedule and health.",
    group: "Administration",
    paths: ["/connections"],
    body: (
      <>
        <p>
          <Link to="/connections">Connections</Link> lists every GitLab deployment Baluarte collects
          from, with the health of its latest capability, group and project collections.
        </p>
        <ul>
          <li>
            <strong>The primary connection</strong> is the GitLab set in the deployment or on the{" "}
            <Link to="/settings">Settings</Link> page. Its URL, token and schedule stay there; here
            you can test it, collect it now or pause it.
          </li>
          <li>
            <strong>Added connections</strong> each have their own read-only token (
            <code>read_api</code>), stored encrypted and never shown again, their own schedule and
            their own timeout.
          </li>
          <li>
            A connection&apos;s host must resolve to public addresses. For a self-managed GitLab on
            your own network, tick <strong>private network</strong>. Loopback, link-local and cloud
            metadata addresses are always refused, and the check runs again before every collection.
          </li>
          <li>
            <strong>GitHub organizations</strong> on github.com are added with a fine-grained
            personal access token or a GitHub App installation (its App ID, installation ID and
            private key). Grant read access only. Baluarte collects the organization, its
            repositories, teams and members, branch settings, security features, labels and changes,
            and evaluates them like GitLab&apos;s. A <strong>webhook</strong> on a GitHub connection
            makes Baluarte read a repository again within seconds of a change: set it up on the
            connection and copy its URL and secret, shown once, into the organization&apos;s
            settings on GitHub.
          </li>
          <li>
            <strong>Pause</strong> stops scheduled collection; <strong>Remove</strong> also forgets
            the token. Either way, the inventory, findings and evidence stay. Adding a removed URL
            again restores it.
          </li>
        </ul>
        <p>
          Every change, test and collection request is in the <Link to="/audit-log">audit log</Link>
          .
        </p>
      </>
    ),
  },
  {
    slug: "settings",
    title: "Settings",
    summary: "Identity provider, GitLab connection, review, write tokens and notifications.",
    group: "Administration",
    paths: ["/settings"],
    body: (
      <>
        <p>
          <Link to="/settings">Settings</Link> is for administrators, on a larger screen.
        </p>
        <Table
          head={["Section", "What it holds"]}
          rows={[
            [
              "Identity provider",
              "The OIDC provider people sign in with, and which provider groups map to each role.",
            ],
            [
              "GitLab connection",
              <>
                The GitLab URL and the read-only token (<code>read_api</code>).{" "}
                <strong>Test connection</strong> checks it and detects what this GitLab supports.
              </>,
            ],
            [
              "Baseline change review",
              "Require a second person to approve baseline, mapping, exclusion and taxonomy changes. Your operating profile may require it.",
            ],
            [
              "Write tokens",
              "Separate tokens for label and issue plans and for remediation plans. Each is stored encrypted, never shown again, and used only when the deployment also switches the writes on.",
            ],
            [
              "Notifications",
              "Email and chat webhook channels for new findings, collection problems, items waiting for review and a weekly digest. Off until you turn a channel on.",
            ],
            [
              "Change evidence and time tracking",
              "Production environments, and how closed-issue rules label and round.",
            ],
            ["Manager scopes", "Which groups each manager may see time per person for."],
            [
              "Compliance",
              "The operating profile, retention, session limits, audit log shipping and backups.",
            ],
          ]}
        />
        <h2 className="h3">Write tokens, safely</h2>
        <ul>
          <li>
            Baluarte reads GitLab with the read-only token only. A write token is used only to apply
            a plan someone else approved.
          </li>
          <li>
            Use a token that reaches only the groups it must change, on a bot user, with an expiry.
            Remediation needs the Owner role to restrict visibility; label plans do not.
          </li>
          <li>
            The deployment switches (<code>BALUARTE_LABEL_WRITES</code>,{" "}
            <code>BALUARTE_ISSUE_WRITES</code>, <code>BALUARTE_REMEDIATION_WRITES</code>) are off by
            default. While a switch is off, no plan of that kind can be applied, whatever token is
            stored.
          </li>
        </ul>
      </>
    ),
  },
];

/** The topic with this slug, if any. */
export function helpTopic(slug: string): HelpTopic | undefined {
  return helpTopics.find((topic) => topic.slug === slug);
}

/** The topic that explains the page at this path, matched by the longest path prefix. */
export function helpTopicFor(pathname: string): HelpTopic | undefined {
  let best: { topic: HelpTopic; length: number } | undefined;
  for (const topic of helpTopics) {
    for (const path of topic.paths) {
      const matches =
        path === "/" ? pathname === "/" : pathname === path || pathname.startsWith(`${path}/`);
      if (matches && (!best || path.length > best.length)) {
        best = { topic, length: path.length };
      }
    }
  }
  return best?.topic;
}
