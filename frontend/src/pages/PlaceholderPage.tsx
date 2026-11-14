import type { ReactNode } from "react";

import { EmptyState } from "../ui/EmptyState";
import { PageHeader } from "../ui/PageHeader";

interface PlaceholderPageProps {
  title: string;
  description: string;
  /** Context shown above the placeholder notice, such as data freshness. */
  children?: ReactNode;
}

export function PlaceholderPage({ title, description, children }: PlaceholderPageProps) {
  return (
    <section aria-labelledby="page-title">
      <PageHeader title={title} subtitle={description} />
      {children}
      <EmptyState title="Not yet implemented." />
    </section>
  );
}
