import type { ReactNode } from "react";

interface PlaceholderPageProps {
  title: string;
  description: string;
  /** Context shown above the placeholder notice, such as data freshness. */
  children?: ReactNode;
}

export function PlaceholderPage({ title, description, children }: PlaceholderPageProps) {
  return (
    <section aria-labelledby="page-title">
      <h1 id="page-title" className="text-2xl font-semibold">
        {title}
      </h1>
      <p className="mt-2 text-slate-600">{description}</p>
      {children}
      <p className="mt-6 rounded border border-dashed border-slate-300 p-4 text-sm text-slate-600">
        Not yet implemented.
      </p>
    </section>
  );
}
