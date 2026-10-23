// Client for event subscriptions (`/v1/subscriptions`), the repository connect
// flow (`/v1/subscriptions/github/*`) and the software-factory template
// (`/v1/templates/factory`). Mirrors `omnigent/server/routes/subscriptions.py`,
// `subscriptions_github.py` and `templates.py`: snake_case on the wire,
// camelCase here, and non-OK responses throw a `SubscriptionApiError` carrying
// the server's message verbatim so the UI can show it as is.

import { authenticatedFetch } from "./identity";

export type SubscriptionState = "active" | "paused";

/** One of the caller's subscriptions (`GET /v1/subscriptions`). */
export interface Subscription {
  id: string;
  source: string;
  kind: string;
  match: Record<string, string | number | boolean>;
  note: string | null;
  state: SubscriptionState;
  /** Wake target: the session the notice is posted into. */
  conversationId: string | null;
  /** Launch target: the agent a new session starts with per event. */
  agentId: string | null;
  promptTemplate: string | null;
  maxCostUsd: number | null;
  fireCount: number;
  /** Unix epoch seconds of the last delivery, or null when it never fired. */
  lastFiredAt: number | null;
  createdAt: number;
}

interface SubscriptionWire {
  id: string;
  source: string;
  kind: string;
  match: Record<string, string | number | boolean> | null;
  note: string | null;
  state: SubscriptionState;
  conversation_id: string | null;
  agent_id: string | null;
  prompt_template: string | null;
  max_cost_usd: number | null;
  fire_count: number;
  last_fired_at: number | null;
  created_at: number;
}

/** Body of `POST /v1/subscriptions` as the UI sends it. */
export interface CreateSubscriptionInput {
  source: "github";
  kind: string;
  match: Record<string, string | number | boolean>;
  note?: string;
  conversation_id?: string;
  agent_id?: string;
  prompt_template?: string;
  max_cost_usd?: number;
}

/** Result of `POST /v1/subscriptions/github/connect`. */
export interface RepositoryHook {
  status: "created" | "existing";
  hookId: number;
  events: string[];
}

/** Body of `POST /v1/templates/factory`. */
export interface StartFactoryInput {
  owner: string;
  repo: string;
  dailyBudgetUsd: number;
  askBeforeMerge: boolean;
}

/** Result of `POST /v1/templates/factory`. */
export interface FactoryResult {
  repo: string;
  agentId: string;
  agentName: string;
  agentCreated: boolean;
  subscriptions: (Subscription & { created: boolean })[];
  hook: RepositoryHook;
}

/** A recent session the "Wake a session" picker offers. */
export interface SessionOption {
  id: string;
  title: string | null;
}

/** A non-OK API answer; `message` is the server's own text. */
export class SubscriptionApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  constructor(message: string, status: number, code: string | null) {
    super(message);
    this.name = "SubscriptionApiError";
    this.status = status;
    this.code = code;
  }
}

async function errorFromResponse(res: Response): Promise<SubscriptionApiError> {
  let message = `${res.status} ${res.statusText}`.trim();
  let code: string | null = null;
  try {
    const body = (await res.json()) as {
      error?: { code?: string; message?: string };
      detail?: string | { msg?: string }[];
    };
    if (body.error?.message) message = body.error.message;
    if (body.error?.code) code = body.error.code;
    if (typeof body.detail === "string") message = body.detail;
    else if (Array.isArray(body.detail) && body.detail[0]?.msg) message = body.detail[0].msg;
  } catch {
    // Non-JSON body: keep the status line.
  }
  return new SubscriptionApiError(message, res.status, code);
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await authenticatedFetch(path, init);
  if (!res.ok) throw await errorFromResponse(res);
  return (await res.json()) as T;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

function subscriptionFromWire(wire: SubscriptionWire): Subscription {
  return {
    id: wire.id,
    source: wire.source,
    kind: wire.kind,
    match: wire.match ?? {},
    note: wire.note,
    state: wire.state,
    conversationId: wire.conversation_id,
    agentId: wire.agent_id,
    promptTemplate: wire.prompt_template,
    maxCostUsd: wire.max_cost_usd,
    fireCount: wire.fire_count,
    lastFiredAt: wire.last_fired_at,
    createdAt: wire.created_at,
  };
}

export async function listSubscriptions(): Promise<Subscription[]> {
  const wire = await requestJson<{ subscriptions: SubscriptionWire[] }>("/v1/subscriptions");
  return (wire.subscriptions ?? []).map(subscriptionFromWire);
}

export async function createSubscription(input: CreateSubscriptionInput): Promise<Subscription> {
  return subscriptionFromWire(
    await requestJson<SubscriptionWire>("/v1/subscriptions", jsonInit("POST", input)),
  );
}

export async function setSubscriptionState(
  id: string,
  state: SubscriptionState,
): Promise<Subscription> {
  return subscriptionFromWire(
    await requestJson<SubscriptionWire>(
      `/v1/subscriptions/${encodeURIComponent(id)}`,
      jsonInit("PATCH", { state }),
    ),
  );
}

export async function deleteSubscription(id: string): Promise<void> {
  await requestJson(`/v1/subscriptions/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** Register (or reuse) this server's webhook on `owner/repo`. */
export async function connectRepository(owner: string, repo: string): Promise<RepositoryHook> {
  const wire = await requestJson<{
    status: "created" | "existing";
    hook_id: number;
    events: string[];
  }>("/v1/subscriptions/github/connect", jsonInit("POST", { owner, repo }));
  return { status: wire.status, hookId: wire.hook_id, events: wire.events };
}

export async function startFactory(input: StartFactoryInput): Promise<FactoryResult> {
  const wire = await requestJson<{
    repo: string;
    agent_id: string;
    agent_name: string;
    agent_created: boolean;
    subscriptions: (SubscriptionWire & { created: boolean })[];
    hook: { status: "created" | "existing"; hook_id: number; events: string[] };
  }>(
    "/v1/templates/factory",
    jsonInit("POST", {
      owner: input.owner,
      repo: input.repo,
      daily_budget_usd: input.dailyBudgetUsd,
      ask_before_merge: input.askBeforeMerge,
    }),
  );
  return {
    repo: wire.repo,
    agentId: wire.agent_id,
    agentName: wire.agent_name,
    agentCreated: wire.agent_created,
    subscriptions: wire.subscriptions.map((s) => ({
      ...subscriptionFromWire(s),
      created: s.created,
    })),
    hook: { status: wire.hook.status, hookId: wire.hook.hook_id, events: wire.hook.events },
  };
}

/** The caller's most recently updated sessions, for the wake-target picker. */
export async function listRecentSessions(): Promise<SessionOption[]> {
  const params = new URLSearchParams({
    order: "desc",
    sort_by: "updated_at",
    limit: "50",
    visibility: "mine",
  });
  const wire = await requestJson<{ data: { id: string; title: string | null }[] }>(
    `/v1/sessions?${params.toString()}`,
  );
  return (wire.data ?? []).map((row) => ({ id: row.id, title: row.title ?? null }));
}
