/**
 * Client for notifications: the email and chat webhook channels, which events go where, test
 * messages and recent deliveries. Secrets (the SMTP password, the webhook URL) are write-only:
 * the API reports them as configured or not, and in an update null keeps the stored one while
 * an empty string removes it.
 */

import { ApiError, getJson } from "./client";
import { postJson, sendJson } from "./send";

const SETTINGS = "/api/v1/settings/notifications";

export type SecretState = "configured" | "not_set";
export type Channel = "email" | "webhook";
export type Severity = "low" | "medium" | "high" | "critical";
export type Security = "starttls" | "tls";

export const CHANNELS: readonly Channel[] = ["email", "webhook"];
export const SEVERITIES: readonly Severity[] = ["low", "medium", "high", "critical"];
export const MAX_RECIPIENTS = 50;

export interface EventSettings {
  enabled: boolean;
  channels: Channel[];
}

export interface FindingsEvent extends EventSettings {
  min_severity: Severity;
}

export interface ReviewEvent extends EventSettings {
  older_than_days: number;
}

export interface NotificationEvents {
  new_findings: FindingsEvent;
  collection_health: EventSettings;
  review_waiting: ReviewEvent;
  weekly_digest: EventSettings;
}

export type EventName = keyof NotificationEvents;

export interface EmailSettings {
  enabled: boolean;
  host: string;
  port: number;
  security: Security;
  username: string | null;
  from_address: string;
  recipients: string[];
  ca_pem: string | null;
}

export interface NotificationsConfig {
  configured: boolean;
  email: EmailSettings & { password: SecretState };
  webhook: {
    enabled: boolean;
    url: SecretState;
    host: string | null;
    ca_pem: string | null;
  };
  events: NotificationEvents;
  /** Baluarte's address, which links in messages start with; null leaves links out. */
  public_url: string | null;
  updated_by: string | null;
  updated_at: string | null;
}

export interface NotificationsUpdate {
  email: EmailSettings & { password: string | null };
  webhook: { enabled: boolean; url: string | null; ca_pem: string | null };
  events: NotificationEvents;
}

export interface ChannelTestResult {
  ok: boolean;
  channel: Channel;
  /** What failed: config, connect, tls, auth or rejected; null when the message was sent. */
  stage: string | null;
  detail: string;
}

export type DeliveryStatus = "pending" | "sent" | "failed";

export interface Delivery {
  id: number;
  notification_id: number;
  event_type: string;
  subject: string;
  channel: Channel;
  status: DeliveryStatus;
  attempts: number;
  created_at: string;
  next_attempt_at: string | null;
  sent_at: string | null;
  last_error: string | null;
}

export function getNotifications(signal?: AbortSignal): Promise<NotificationsConfig> {
  return getJson(SETTINGS, undefined, signal);
}

export async function saveNotifications(
  update: NotificationsUpdate,
  csrfToken: string,
): Promise<NotificationsConfig> {
  const result = await sendJson<NotificationsConfig>("PUT", SETTINGS, update, csrfToken);
  if (result === null) throw new ApiError(204, "The API returned no content.");
  return result;
}

export function testNotifications(
  update: NotificationsUpdate,
  channel: Channel,
  csrfToken: string,
): Promise<ChannelTestResult> {
  return postJson(`${SETTINGS}/test`, { ...update, channel }, csrfToken);
}

export async function getDeliveries(signal?: AbortSignal): Promise<Delivery[]> {
  const page = await getJson<{ items: Delivery[] }>(
    "/api/v1/notifications/deliveries",
    undefined,
    signal,
  );
  return page.items;
}
