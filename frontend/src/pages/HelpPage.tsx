import { IconArrowLeft, IconArrowRight } from "@tabler/icons-react";
import { Link, NavLink, useParams } from "react-router";

import { helpGroups, helpTopic, helpTopics, type HelpTopic } from "../help/topics";
import { PageHeader } from "../ui/PageHeader";
import { NotFoundPage } from "./NotFoundPage";

/** Topics in reading order: by group, then as listed. */
const ordered: readonly HelpTopic[] = helpGroups.flatMap((group) =>
  helpTopics.filter((topic) => topic.group === group),
);

/** The guide's table of contents, beside each topic on larger screens. */
function Contents() {
  return (
    <nav aria-label="Help topics" className="card">
      <div className="card-body">
        {helpGroups.map((group) => (
          <div key={group} className="mb-3">
            <div className="subheader mb-1">{group}</div>
            <ul className="nav nav-vertical flex-column">
              {ordered
                .filter((topic) => topic.group === group)
                .map((topic) => (
                  <li key={topic.slug} className="nav-item">
                    <NavLink to={`/help/${topic.slug}`} className="nav-link py-1 px-0">
                      {topic.title}
                    </NavLink>
                  </li>
                ))}
            </ul>
          </div>
        ))}
      </div>
    </nav>
  );
}

/** The guide's front page: every topic, by group. */
export function HelpPage() {
  return (
    <section aria-labelledby="page-title">
      <PageHeader
        title="Help"
        subtitle="How to use Baluarte, step by step, and the gaps in GitLab it covers."
      />
      <p className="mb-4">
        New to Baluarte? Start with <Link to="/help/getting-started">Getting started</Link>. To see
        what Baluarte adds to GitLab on your tier, read{" "}
        <Link to="/help/gitlab-gaps">Gaps in GitLab that Baluarte covers</Link>. The help button in
        the header opens the topic for the page you are on.
      </p>
      {helpGroups.map((group) => (
        <section key={group} aria-labelledby={`help-${group}`} className="mb-4">
          <h2 id={`help-${group}`} className="h3">
            {group}
          </h2>
          <div className="row row-cards">
            {ordered
              .filter((topic) => topic.group === group)
              .map((topic) => (
                <div key={topic.slug} className="col-md-6 col-xl-4">
                  <div className="card card-link h-100">
                    <div className="card-body">
                      <h3 className="card-title mb-1">
                        <Link to={`/help/${topic.slug}`} className="stretched-link">
                          {topic.title}
                        </Link>
                      </h3>
                      <p className="text-secondary mb-0">{topic.summary}</p>
                    </div>
                  </div>
                </div>
              ))}
          </div>
        </section>
      ))}
    </section>
  );
}

/** One topic of the guide, with the contents beside it and links to the topics either side. */
export function HelpTopicPage() {
  const { topic: slug = "" } = useParams();
  const topic = helpTopic(slug);
  if (!topic) return <NotFoundPage />;
  const index = ordered.indexOf(topic);
  const previous = ordered[index - 1];
  const next = ordered[index + 1];

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle={<Link to="/help">Help</Link>}
        title={topic.title}
        subtitle={topic.summary}
      />
      <div className="row g-3">
        <div className="col-lg-3 d-none d-lg-block">
          <Contents />
        </div>
        <div className="col-lg-9">
          <article className="card">
            <div className="card-body help-topic">{topic.body}</div>
          </article>
          <nav aria-label="More help" className="d-flex justify-content-between gap-2 mt-3">
            {previous ? (
              <Link to={`/help/${previous.slug}`} className="btn btn-ghost-secondary">
                <IconArrowLeft size={18} aria-hidden="true" className="me-1" />
                {previous.title}
              </Link>
            ) : (
              <span />
            )}
            {next ? (
              <Link to={`/help/${next.slug}`} className="btn btn-ghost-secondary">
                {next.title}
                <IconArrowRight size={18} aria-hidden="true" className="ms-1" />
              </Link>
            ) : (
              <Link to="/help" className="btn btn-ghost-secondary">
                All help topics
              </Link>
            )}
          </nav>
        </div>
      </div>
    </section>
  );
}
