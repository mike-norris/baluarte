import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RouterProvider, createMemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { Delivery, NotificationsConfig } from "../api/notifications";
import type { CurrentUser } from "../auth/session";
import { routes } from "../routes";
import { analyst, json, mockApi, type Call } from "../test/api";
import { draftFrom, notificationProblems, parseRecipients, toUpdate } from "./NotificationsCard";

const admin: CurrentUser = {
  ...analyst,
  roles: ["admin"],
  permissions: [...analyst.permissions, "settings:manage"],
  csrf_token: "a".repeat(64),
};

const SETTINGS = "/api/v1/settings/notifications";
const DELIVERIES = "/api/v1/notifications/deliveries";

function config(overrides: Partial<NotificationsConfig> = {}): NotificationsConfig {
  return {
    configured: true,
    email: {
      enabled: true,
      host: "mail.example.test",
      port: 587,
      security: "starttls",
      username: "baluarte",
      password: "configured",
      from_address: "baluarte@example.test",
      recipients: ["security-team@example.test"],
      ca_pem: null,
    },
    webhook: { enabled: false, url: "not_set", host: null, ca_pem: null },
    events: {
      new_findings: { enabled: true, channels: ["email"], min_severity: "high" },
      collection_health: { enabled: false, channels: [] },
      review_waiting: { enabled: true, channels: ["email"], older_than_days: 2 },
      weekly_digest: { enabled: false, channels: [] },
    },
    public_url: "https://baluarte.example.test",
    updated_by: "user:1",
    updated_at: "2026-11-16T09:00:00Z",
    ...overrides,
  };
}

const failed: Delivery = {
  id: 4,
  notification_id: 2,
  event_type: "new_findings",
  subject: "[Baluarte] 2 new findings at high severity or above",
  channel: "webhook",
  status: "failed",
  attempts: 5,
  created_at: "2026-11-16T09:00:00Z",
  next_attempt_at: null,
  sent_at: null,
  last_error: "Could not connect: webhook hooks.example.test: connection refused",
};

const sent: Delivery = {
  ...failed,
  id: 3,
  channel: "email",
  status: "sent",
  attempts: 1,
  sent_at: "2026-11-16T09:01:00Z",
  last_error: null,
};

type Routes = Record<string, (init?: RequestInit) => Response>;

function api(user: CurrentUser = admin, routes: Routes = {}): Call[] {
  return mockApi({
    "GET /api/v1/auth/me": () => json(user),
    [`GET ${SETTINGS}`]: () => json(config()),
    [`GET ${DELIVERIES}`]: () => json({ items: [failed, sent] }),
    ...routes,
  });
}

function renderSettings() {
  const router = createMemoryRouter(routes, { initialEntries: ["/settings"] });
  render(<RouterProvider router={router} />);
}

function bodyOf(calls: Call[], method: string, url: string): unknown {
  const call = calls.filter((c) => c.url === url && c.init?.method === method).at(-1);
  return call?.init?.body ? JSON.parse(String(call.init.body)) : undefined;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("notifications card", () => {
  it("summarises the channels and events and lists recent deliveries", async () => {
    api();
    renderSettings();

    const card = await screen.findByRole("region", { name: "Notifications" });
    expect(
      await within(card).findByText("On: mail.example.test:587 (STARTTLS), 1 recipients"),
    ).toBeInTheDocument();
    expect(within(card).getByText("Configured (hidden)")).toBeInTheDocument();
    expect(within(card).getByText("Email, high and above")).toBeInTheDocument();
    expect(within(card).getByText("Email, waiting more than 2 days")).toBeInTheDocument();
    const table = await within(card).findByRole("table", {
      name: "Recent notification deliveries",
    });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("New findingsChat webhookFailed");
    expect(rows[1]).toHaveTextContent("Could not connect: webhook hooks.example.test");
    expect(rows[2]).toHaveTextContent("Sent");
  });

  it("saves the form, keeping stored secrets when left blank", async () => {
    const calls = api(admin, { [`PUT ${SETTINGS}`]: () => json(config()) });
    const user = userEvent.setup();
    renderSettings();

    const card = await screen.findByRole("region", { name: "Notifications" });
    await user.click(await within(card).findByRole("button", { name: "Edit notifications" }));
    await user.click(within(card).getByRole("checkbox", { name: "Post to a chat webhook" }));
    await user.type(
      within(card).getByLabelText("Webhook URL"),
      "https://hooks.example.test/services/T0/B0/token",
    );
    const health = within(card).getByRole("group", { name: "Collection unhealthy or recovered" });
    await user.click(
      within(health).getByRole("checkbox", { name: "Collection unhealthy or recovered" }),
    );
    await user.click(
      within(health).getByRole("checkbox", {
        name: "Chat webhook for Collection unhealthy or recovered",
      }),
    );
    await user.selectOptions(within(card).getByLabelText("At or above severity"), "critical");
    await user.click(within(card).getByRole("button", { name: "Save notifications" }));

    expect(await within(card).findByText(/Notifications saved/)).toBeInTheDocument();
    expect(bodyOf(calls, "PUT", SETTINGS)).toEqual({
      email: {
        enabled: true,
        host: "mail.example.test",
        port: 587,
        security: "starttls",
        username: "baluarte",
        password: null,
        from_address: "baluarte@example.test",
        recipients: ["security-team@example.test"],
        ca_pem: null,
      },
      webhook: {
        enabled: true,
        url: "https://hooks.example.test/services/T0/B0/token",
        ca_pem: null,
      },
      events: {
        new_findings: { enabled: true, channels: ["email"], min_severity: "critical" },
        collection_health: { enabled: true, channels: ["webhook"] },
        review_waiting: { enabled: true, channels: ["email"], older_than_days: 2 },
        weekly_digest: { enabled: false, channels: [] },
      },
    });
  });

  it("refuses a plain http webhook before sending anything", async () => {
    const calls = api();
    const user = userEvent.setup();
    renderSettings();

    const card = await screen.findByRole("region", { name: "Notifications" });
    await user.click(await within(card).findByRole("button", { name: "Edit notifications" }));
    await user.click(within(card).getByRole("checkbox", { name: "Post to a chat webhook" }));
    await user.type(within(card).getByLabelText("Webhook URL"), "http://hooks.example.test/x");
    await user.click(within(card).getByRole("button", { name: "Save notifications" }));

    expect(await within(card).findByRole("alert")).toHaveTextContent(
      "Webhook URL: enter an https:// URL.",
    );
    expect(calls.some((c) => c.init?.method === "PUT")).toBe(false);
  });

  it("sends a test through one channel and says what failed", async () => {
    const calls = api(admin, {
      [`POST ${SETTINGS}/test`]: () =>
        json({
          ok: false,
          channel: "email",
          stage: "tls",
          detail: "TLS failed: SMTP mail.example.test:587: the server does not offer STARTTLS",
        }),
    });
    const user = userEvent.setup();
    renderSettings();

    const card = await screen.findByRole("region", { name: "Notifications" });
    await user.click(await within(card).findByRole("button", { name: "Edit notifications" }));
    await user.click(within(card).getByRole("button", { name: "Send test email" }));

    expect(await within(card).findByRole("alert")).toHaveTextContent(
      "Test failed. TLS failed: SMTP mail.example.test:587: the server does not offer STARTTLS",
    );
    expect(bodyOf(calls, "POST", `${SETTINGS}/test`)).toMatchObject({
      channel: "email",
      email: { host: "mail.example.test", password: null },
    });
  });

  it("is not shown without settings:manage", async () => {
    const calls = api(analyst);
    renderSettings();

    expect(
      await screen.findByText("Only administrators can view and change settings."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Notifications" })).toBeNull();
    expect(calls.some((c) => c.url.startsWith(SETTINGS) || c.url === DELIVERIES)).toBe(false);
  });
});

describe("notification form checks", () => {
  it("parses recipients one per line, without repeats", () => {
    expect(
      parseRecipients("a@example.test\n\nB@example.test, b@example.test; c@example.test"),
    ).toEqual(["a@example.test", "B@example.test", "c@example.test"]);
  });

  it("names each field with a problem", () => {
    const stored = config();
    const draft = {
      ...draftFrom(stored),
      host: "smtp://mail.example.test",
      port: "70000",
      fromAddress: "nobody",
      recipients: "",
      username: "other",
      events: {
        ...stored.events,
        review_waiting: { ...stored.events.review_waiting, older_than_days: 0 },
      },
    };

    expect(notificationProblems(draft, stored)).toEqual([
      "SMTP host: enter a host name without a scheme.",
      "Port: enter a port from 1 to 65535.",
      "From: enter an email address.",
      "Recipients: enter at least one address.",
      "Password: enter it again, because the host or username changed.",
      "Waiting for review: choose 1 to 30 days.",
    ]);
  });

  it("drops the password when there is no username", () => {
    const draft = { ...draftFrom(config()), username: " ", password: "kept-nowhere" };

    expect(toUpdate(draft).email).toMatchObject({ username: null, password: null });
  });
});
