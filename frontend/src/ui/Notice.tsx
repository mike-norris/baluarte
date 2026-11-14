import { IconAlertCircle, IconAlertTriangle, IconCircleCheck, IconInfoCircle } from "@tabler/icons-react";
import type { ReactNode } from "react";

type NoticeTone = "info" | "success" | "warning" | "danger";

const ICONS = {
  info: IconInfoCircle,
  success: IconCircleCheck,
  warning: IconAlertTriangle,
  danger: IconAlertCircle,
} as const;

interface NoticeProps {
  tone: NoticeTone;
  title?: ReactNode;
  children?: ReactNode;
  /** `alert` for problems that need attention now, `status` for results, none for static text. */
  role?: "alert" | "status";
}

/** A Tabler alert: body-coloured text with a coloured icon, readable in both themes. */
export function Notice({ tone, title, children, role }: NoticeProps) {
  const Glyph = ICONS[tone];
  return (
    <div role={role} className={`alert alert-${tone}`}>
      <div className="d-flex gap-2">
        <Glyph size={20} className="alert-icon flex-shrink-0" aria-hidden="true" />
        <div style={{ minWidth: 0 }}>
          {title && <p className="alert-title mb-1">{title}</p>}
          {children && <div className="text-body">{children}</div>}
        </div>
      </div>
    </div>
  );
}
