import { IconMapOff } from "@tabler/icons-react";
import { Link } from "react-router";

import { PageHeader } from "../ui/PageHeader";
import { EmptyState } from "../ui/EmptyState";

export function NotFoundPage() {
  return (
    <section aria-labelledby="page-title">
      <PageHeader title="Page not found" />
      <EmptyState
        title="There is no page at this address."
        icon={IconMapOff}
        action={
          <Link to="/" className="btn btn-primary">
            Return to the dashboard
          </Link>
        }
      />
    </section>
  );
}
