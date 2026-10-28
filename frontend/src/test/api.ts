import { vi } from "vitest";

import type { CurrentUser } from "../auth/session";

export const analyst: CurrentUser = {
  subject: "user-1",
  email: "ada@example.test",
  display_name: "Ada Lovelace",
  roles: ["analyst"],
  permissions: ["evidence:read", "findings:read", "inventory:read", "policy:write"],
  session_expires_at: "2030-01-01T00:00:00Z",
  csrf_token: "c".repeat(64),
};

export interface Call {
  url: string;
  init: RequestInit | undefined;
}

/** Replace fetch with a handler keyed on "METHOD /path"; returns the recorded calls. */
export function mockApi(routes: Record<string, () => Response | Promise<Response>>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : input.toString();
    calls.push({ url, init });
    const key = `${init?.method ?? "GET"} ${url}`;
    const handler = routes[key];
    return handler
      ? Promise.resolve(handler())
      : Promise.reject(new TypeError(`unexpected fetch: ${key}`));
  });
  return calls;
}

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

export function signedIn(user: CurrentUser = analyst): Call[] {
  return mockApi({ "GET /api/v1/auth/me": () => json(user) });
}
