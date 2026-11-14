import { IconInbox } from "@tabler/icons-react";
import { Link, useSearchParams } from "react-router";

import { useLoad } from "../api/useLoad";
import { useAuth } from "../auth/context";
import { ErrorMessage } from "../components/ErrorMessage";
import { canDecide, isMine, kindLabels, loadReviewQueue, type ReviewItem } from "../reviews/queue";
import { formatTime } from "../time";
import { Badge } from "../ui/Badge";
import { EmptyState } from "../ui/EmptyState";
import { Loading } from "../ui/Loading";
import { Notice } from "../ui/Notice";
import { PageHeader } from "../ui/PageHeader";
import { ResponsiveTable, type Column } from "../ui/ResponsiveTable";

type View = "waiting" | "mine";

const DAY_MS = 24 * 60 * 60 * 1000;

function expiry(item: ReviewItem) {
  if (!item.expiresAt) return <span className="text-secondary">No expiry</span>;
  const soon = new Date(item.expiresAt).getTime() - Date.now() < DAY_MS;
  return soon ? (
    <Badge tone="warning">{`Expires ${formatTime(item.expiresAt)}`}</Badge>
  ) : (
    <>{formatTime(item.expiresAt)}</>
  );
}

const columns: readonly Column<ReviewItem>[] = [
  {
    key: "item",
    header: "Item",
    render: (item) => (
      <div>
        <Link to={item.href} className="fw-medium">
          {item.title}
        </Link>
        <div className="small text-secondary">{kindLabels[item.kind]}</div>
      </div>
    ),
  },
  { key: "by", header: "Proposed by", render: (item) => item.proposedBy },
  {
    key: "at",
    header: "Proposed",
    render: (item) => formatTime(item.proposedAt),
    priority: 2,
  },
  { key: "expires", header: "Expires", render: expiry, priority: 2 },
];

/** One place for everything waiting for a decision, and for what the user proposed. */
export function ReviewsPage() {
  const { session } = useAuth();
  const user = session.status === "signed_in" ? session.user : null;
  const [params] = useSearchParams();
  const view: View = params.get("view") === "mine" ? "mine" : "waiting";
  const queue = useLoad(`reviews@${String(user?.user_id)}`, (signal) =>
    user
      ? loadReviewQueue(user, signal)
      : Promise.resolve({ items: [], unreadable: [], partial: [] }),
  );

  const items =
    user && queue.data
      ? queue.data.items.filter((item) =>
          view === "mine" ? isMine(user, item) : canDecide(user, item),
        )
      : [];
  const partial = (queue.data?.partial.length ?? 0) > 0;

  const tab = (value: View, label: string) => (
    <li className="nav-item">
      <Link
        to={value === "waiting" ? "/reviews" : `/reviews?view=${value}`}
        className={`nav-link ${view === value ? "active" : ""}`}
        aria-current={view === value ? "page" : undefined}
      >
        {label}
      </Link>
    </li>
  );

  return (
    <section aria-labelledby="page-title">
      <PageHeader
        pretitle="Overview"
        title="Review queue"
        subtitle="Changes, plans and exceptions waiting for a second person's decision."
      />
      <nav aria-label="Review queue views">
        <ul className="nav nav-tabs mb-3">
          {tab("waiting", "Waiting for you")}
          {tab("mine", "Proposed by you")}
        </ul>
      </nav>
      {queue.data && queue.data.unreadable.length > 0 && (
        <Notice tone="warning" title="Some lists could not be read">
          {`Items of these kinds may be missing: ${queue.data.unreadable
            .map((kind) => kindLabels[kind].toLowerCase())
            .join(", ")}.`}
        </Notice>
      )}
      {partial && (
        <Notice tone="info">
          Some kinds have more waiting items than one page; open them from their own pages to see
          the rest.
        </Notice>
      )}
      <div className="card">
        {queue.error ? (
          <div className="card-body">
            <ErrorMessage error={queue.error} retry={queue.retry} />
          </div>
        ) : !queue.data ? (
          <div className="card-body">
            <Loading label="Loading the review queue…" />
          </div>
        ) : items.length === 0 ? (
          <EmptyState icon={IconInbox} title="Nothing waiting">
            {view === "mine"
              ? "Nothing you proposed is waiting for a decision."
              : "Nothing is waiting for your decision."}
          </EmptyState>
        ) : (
          <ResponsiveTable
            caption={view === "mine" ? "Proposed by you" : "Waiting for you"}
            columns={columns}
            rows={items}
            rowKey={(item) => `${item.kind}-${String(item.id)}`}
          />
        )}
      </div>
    </section>
  );
}
