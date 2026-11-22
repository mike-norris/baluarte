import { useState } from "react";

import {
  readWebhook,
  removeWebhook,
  setWebhook,
  type WebhookDelivery,
  type WebhookSecret,
} from "../api/connections";
import { errorReasons } from "../api/send";
import { useLoad } from "../api/useLoad";
import { ErrorMessage } from "../components/ErrorMessage";
import { asApiError, Problems } from "../settings/parts";
import { formatTime } from "../time";
import { Badge, type Tone } from "../ui/Badge";
import { Notice } from "../ui/Notice";

const statusTones: Record<WebhookDelivery["status"], Tone> = {
  queued: "info",
  processed: "success",
  ignored: "secondary",
  failed: "danger",
};

/**
 * A GitHub connection's signed webhook (roadmap M13.4b): set up or rotate the secret, shown once,
 * and the latest deliveries. Scheduled collection goes on either way.
 */
export function WebhookPanel({ connectionId, csrf }: { connectionId: number; csrf: string }) {
  const loaded = useLoad(`webhook:${String(connectionId)}`, (signal) =>
    readWebhook(connectionId, signal),
  );
  const [shown, setShown] = useState<WebhookSecret | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);

  async function act(run: () => Promise<void>) {
    setBusy(true);
    setProblems([]);
    try {
      await run();
      loaded.retry();
    } catch (error) {
      const apiError = asApiError(error);
      setProblems([apiError.message, ...errorReasons(apiError)]);
    } finally {
      setBusy(false);
    }
  }

  const webhook = loaded.data;
  return (
    <section aria-label="Webhook" className="border rounded p-3 stack-2">
      <div className="d-flex flex-wrap align-items-center gap-2">
        <h3 className="fw-semibold mb-0">Webhook</h3>
        {webhook && (
          <Badge tone={webhook.configured ? "success" : "secondary"}>
            {webhook.configured ? "Set up" : "Not set up"}
          </Badge>
        )}
      </div>
      <p className="small text-secondary mb-0">
        With a webhook, GitHub tells Baluarte about pull requests, reviews, workflow runs,
        deployments and setting changes as they happen, so evidence arrives in minutes instead of at
        the next scheduled collection.
      </p>
      {loaded.error && <ErrorMessage error={loaded.error} retry={loaded.retry} />}
      <Problems messages={problems} />
      {shown && (
        <Notice tone="warning" role="status" title="Copy the secret now">
          <p>
            Add a webhook in the organization&apos;s settings on GitHub with this payload URL and
            secret, content type <code>application/json</code>, and these events. The secret is not
            shown again.
          </p>
          <dl className="mb-0">
            <dt>Payload URL</dt>
            <dd>
              <code className="text-break">{shown.url}</code>
            </dd>
            <dt>Secret</dt>
            <dd>
              <code className="text-break">{shown.secret}</code>
            </dd>
            <dt>Events</dt>
            <dd>{shown.events.join(", ")}</dd>
          </dl>
        </Notice>
      )}
      {webhook && !shown && webhook.configured && (
        <p className="small mb-0">
          Payload URL: <code className="text-break">{webhook.url}</code>
        </p>
      )}
      <div className="d-flex flex-wrap gap-2">
        <button
          type="button"
          className="btn"
          disabled={busy || !webhook}
          onClick={() =>
            void act(async () => {
              setShown(await setWebhook(connectionId, csrf));
            })
          }
        >
          {webhook?.configured ? "Rotate secret" : "Set up webhook"}
        </button>
        {webhook?.configured && (
          <button
            type="button"
            className="btn"
            disabled={busy}
            onClick={() =>
              void act(async () => {
                await removeWebhook(connectionId, csrf);
                setShown(null);
              })
            }
          >
            Turn off
          </button>
        )}
      </div>
      {webhook && webhook.deliveries.length > 0 && (
        <div className="table-responsive">
          <table className="table table-sm mb-0">
            <caption className="small">Latest deliveries</caption>
            <thead>
              <tr>
                <th scope="col">Received</th>
                <th scope="col">Event</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {webhook.deliveries.map((delivery) => (
                <tr key={delivery.delivery_id}>
                  <td>{formatTime(delivery.received_at)}</td>
                  <td>
                    {delivery.event}
                    {delivery.action ? ` (${delivery.action})` : ""}
                  </td>
                  <td>
                    <Badge tone={statusTones[delivery.status]}>{delivery.status}</Badge>
                    {delivery.error && (
                      <div className="small text-secondary text-break">{delivery.error}</div>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
