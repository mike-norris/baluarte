import type { ReactNode } from "react";

export interface KeyValueItem {
  term: string;
  value: ReactNode;
}

/** Facts about one thing as a Tabler datagrid: a term above each value. */
export function KeyValue({ items }: { items: readonly KeyValueItem[] }) {
  return (
    <dl className="datagrid mb-0">
      {items.map((item) => (
        <div key={item.term} className="datagrid-item">
          <dt className="datagrid-title">{item.term}</dt>
          <dd className="datagrid-content mb-0">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}
