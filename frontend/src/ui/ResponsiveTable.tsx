import type { ReactNode } from "react";

export interface Column<Row> {
  key: string;
  header: string;
  render: (row: Row) => ReactNode;
  /**
   * 1: always shown. 2: hidden on phones. 3: shown from xl up only. On phones, rows stack into
   * cards labelled with each header.
   */
  priority?: 1 | 2 | 3;
  /** Right-aligned numbers. */
  numeric?: boolean;
  /** This column names the row, so it is a row header for screen readers. */
  rowHeader?: true;
}

interface ResponsiveTableProps<Row> {
  columns: readonly Column<Row>[];
  rows: readonly Row[];
  rowKey: (row: Row) => string | number;
  /** The table's accessible name. */
  caption: string;
  /** Show the caption to everyone rather than only to screen readers. */
  showCaption?: boolean;
}

const HIDE: Record<1 | 2 | 3, string> = {
  1: "",
  2: "d-none d-md-table-cell",
  3: "d-none d-xl-table-cell",
};

/**
 * One table for every screen: a card table on larger screens that stacks into labelled rows on
 * phones (Tabler's `table-mobile-md`). One set of elements, so each cell has one accessible name.
 */
export function ResponsiveTable<Row>({
  columns,
  rows,
  rowKey,
  caption,
  showCaption = false,
}: ResponsiveTableProps<Row>) {
  return (
    <div className="table-responsive-md">
      <table className="table table-vcenter card-table table-mobile-md mb-0">
        <caption className={showCaption ? "caption-top px-3" : "visually-hidden"}>
          {caption}
        </caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={`${HIDE[column.priority ?? 1]} ${column.numeric ? "text-end" : ""}`}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={rowKey(row)}>
              {columns.map((column) => {
                const Cell = column.rowHeader ? "th" : "td";
                return (
                  <Cell
                    key={column.key}
                    scope={column.rowHeader ? "row" : undefined}
                    data-label={column.header}
                    className={`${HIDE[column.priority ?? 1]} ${column.numeric ? "text-md-end tabular-nums" : ""} ${column.rowHeader ? "fw-normal" : ""}`}
                  >
                    {column.render(row)}
                  </Cell>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
