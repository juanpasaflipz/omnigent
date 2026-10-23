// Test-only routing for a mocked `authenticatedFetch`: answers by
// "METHOD /path" (query string ignored) and records every request body.

import { vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";

export interface RouteReply {
  status?: number;
  body: unknown;
}
export type RouteTable = Record<string, RouteReply | ((body: unknown) => RouteReply)>;

export interface RecordedRequest {
  method: string;
  path: string;
  body: unknown;
}

export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

/** Route the mocked `authenticatedFetch`; returns the request log. */
export function routeFetch(routes: RouteTable): RecordedRequest[] {
  const requests: RecordedRequest[] = [];
  vi.mocked(authenticatedFetch).mockImplementation(async (input, init) => {
    const path = new URL(String(input), "http://x").pathname;
    const method = init?.method ?? "GET";
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : undefined;
    requests.push({ method, path, body });
    const route = routes[`${method} ${path}`];
    if (route === undefined) return jsonResponse({ detail: "Not Found" }, 404);
    const reply = typeof route === "function" ? route(body) : route;
    return jsonResponse(reply.body, reply.status ?? 200);
  });
  return requests;
}

export const CONNECTED_REPOS: RouteReply = {
  body: {
    connected: true,
    truncated: false,
    repos: [
      {
        full_name: "acme/api",
        clone_url: null,
        default_branch: "main",
        private: true,
        pushed_at: null,
      },
      {
        full_name: "acme/web",
        clone_url: null,
        default_branch: "main",
        private: false,
        pushed_at: null,
      },
    ],
  },
};

export const UNCONNECTED_REPOS: RouteReply = {
  body: { connected: false, repos: [], truncated: false },
};

export function subscriptionWire(overrides: Record<string, unknown> = {}) {
  return {
    id: "sub1",
    owner_user_id: "alice@example.com",
    conversation_id: null,
    agent_id: "agent-1",
    prompt_template: "Triage {fields_json}",
    execution_target: "connected_host",
    host_id: null,
    workspace: null,
    max_cost_usd: null,
    model_override: null,
    source: "github",
    kind: "issues.opened",
    match: { repo: "acme/api" },
    note: "New issues in acme/api",
    state: "active",
    created_at: 1_760_000_000,
    updated_at: null,
    last_fired_at: null,
    fire_count: 0,
    ...overrides,
  };
}
