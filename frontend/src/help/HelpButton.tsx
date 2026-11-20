import { IconHelp } from "@tabler/icons-react";
import { Link, useLocation } from "react-router";

import { helpTopicFor } from "./topics";

/** The header's help button: the topic for the page you are on, or the guide's front page. */
export function HelpButton() {
  const { pathname } = useLocation();
  const topic = pathname.startsWith("/help") ? undefined : helpTopicFor(pathname);
  return (
    <Link
      to={topic ? `/help/${topic.slug}` : "/help"}
      className="btn btn-ghost-secondary btn-icon"
      aria-label={topic ? `Help: ${topic.title}` : "Help"}
      title={topic ? `Help: ${topic.title}` : "Help"}
    >
      <IconHelp size={18} aria-hidden="true" />
    </Link>
  );
}
