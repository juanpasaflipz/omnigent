// Friendly GitHub event choices for the "Connect a repository" dialog and how
// each becomes a `POST /v1/subscriptions` body. Kinds and fields follow the
// normalization in `omnigent/server/routes/events.py` (docs/SUBSCRIPTIONS.md).

import type { CreateSubscriptionInput } from "./subscriptionsApi";

export interface RepositoryEventOption {
  id: "issues" | "failed_checks" | "new_prs" | "comments";
  label: string;
  kind: string;
  /** Extra filter on top of `{repo}`. */
  match: Record<string, string>;
}

export const REPOSITORY_EVENT_OPTIONS: readonly RepositoryEventOption[] = [
  { id: "issues", label: "New issues", kind: "issues.opened", match: {} },
  {
    id: "failed_checks",
    label: "Failed checks",
    kind: "check_run.completed",
    match: { conclusion: "failure" },
  },
  { id: "new_prs", label: "New PRs", kind: "pull_request.opened", match: {} },
  { id: "comments", label: "Comments", kind: "issue_comment.created", match: {} },
];

/** Placeholders a launch prompt template may use (docs/SUBSCRIPTIONS.md). */
export const PROMPT_TEMPLATE_PLACEHOLDERS = [
  "{source}",
  "{kind}",
  "{subscription_id}",
  "{note}",
  "{fields_json}",
] as const;

export const DEFAULT_PROMPT_TEMPLATE =
  "A {kind} event arrived from GitHub. Look into it and act on it.\nEvent: {fields_json}";

export type SubscriptionTarget =
  | { type: "session"; conversationId: string }
  | { type: "agent"; agentId: string; promptTemplate: string; maxCostUsd?: number };

/**
 * One subscription body per checked event, all filtered to `repo`
 * (`owner/name`) and aimed at the chosen session or agent.
 */
export function buildSubscriptionPayloads(
  repo: string,
  eventIds: readonly RepositoryEventOption["id"][],
  target: SubscriptionTarget,
): CreateSubscriptionInput[] {
  return REPOSITORY_EVENT_OPTIONS.filter((option) => eventIds.includes(option.id)).map((option) => {
    const base: CreateSubscriptionInput = {
      source: "github",
      kind: option.kind,
      match: { repo, ...option.match },
      note: `${option.label} in ${repo}`,
    };
    if (target.type === "session") return { ...base, conversation_id: target.conversationId };
    return {
      ...base,
      agent_id: target.agentId,
      prompt_template: target.promptTemplate,
      ...(target.maxCostUsd !== undefined ? { max_cost_usd: target.maxCostUsd } : {}),
    };
  });
}

/** Human label for a subscription's kind, e.g. "Failed checks". */
export function eventLabel(kind: string, match: Record<string, unknown>): string {
  const option = REPOSITORY_EVENT_OPTIONS.find(
    (candidate) =>
      candidate.kind === kind &&
      Object.entries(candidate.match).every(([key, value]) => match[key] === value),
  );
  return option?.label ?? kind;
}
