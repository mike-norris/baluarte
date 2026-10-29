const timeFormat = new Intl.DateTimeFormat("en-GB", {
  timeZone: "UTC",
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
});

/** Timestamps are shown in UTC, matching the API and the evidence records. */
export function formatTime(iso: string | null): string {
  return iso ? `${timeFormat.format(new Date(iso))} UTC` : "Never";
}
