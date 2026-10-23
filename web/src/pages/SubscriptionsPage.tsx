/**
 * Subscriptions page (`/subscriptions`, behind the `subscriptions_page` flag).
 *
 * Top: the "Start a software factory" card. Below: the caller's event
 * subscriptions with pause/resume and delete, and a "Connect a repository"
 * dialog that registers the GitHub webhook and creates subscriptions for the
 * picked events. See docs/SUBSCRIPTIONS.md ("From the web UI").
 */

import { useMemo, useState } from "react";
import {
  BellRingIcon,
  Loader2Icon,
  PauseIcon,
  PlayIcon,
  PlusIcon,
  Trash2Icon,
  TriangleAlertIcon,
} from "lucide-react";
import { PageScroll } from "@/components/PageScroll";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConnectRepositoryDialog } from "@/components/subscriptions/ConnectRepositoryDialog";
import { FactoryCard } from "@/components/subscriptions/FactoryCard";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import {
  useDeleteSubscription,
  useSetSubscriptionState,
  useSubscriptions,
} from "@/hooks/useSubscriptions";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import { Link } from "@/lib/routing";
import { eventLabel } from "@/lib/subscriptionEvents";
import type { Subscription } from "@/lib/subscriptionsApi";

const COLUMNS: { label: string; align?: "right" }[] = [
  { label: "Event" },
  { label: "Match" },
  { label: "Target" },
  { label: "State" },
  { label: "Fires", align: "right" },
  { label: "Last fired", align: "right" },
  { label: "", align: "right" },
];

function formatLastFired(lastFiredAt: number | null, nowMs: number): string {
  if (lastFiredAt == null) return "Never";
  const label = relativeTime(lastFiredAt * 1000, nowMs);
  if (label === "") return "—";
  return label === "now" ? "just now" : `${label} ago`;
}

function formatMatch(match: Subscription["match"]): string {
  const entries = Object.entries(match);
  if (entries.length === 0) return "any";
  return entries.map(([key, value]) => `${key}=${String(value)}`).join(", ");
}

function SubscriptionTarget({
  subscription,
  agentNames,
}: {
  subscription: Subscription;
  agentNames: Map<string, string>;
}) {
  if (subscription.conversationId) {
    return (
      <Link
        to={`/c/${subscription.conversationId}`}
        componentId="subscriptions.open_session"
        className="text-primary underline"
      >
        Session
      </Link>
    );
  }
  if (subscription.agentId) {
    const name = agentNames.get(subscription.agentId) ?? subscription.agentId.slice(0, 8);
    return <span title={subscription.agentId}>Start {name}</span>;
  }
  return <span className="text-muted-foreground">—</span>;
}

function SubscriptionTable({ subscriptions, now }: { subscriptions: Subscription[]; now: number }) {
  const setState = useSetSubscriptionState();
  const remove = useDeleteSubscription();
  const agents = useAvailableAgents({
    enabled: subscriptions.some((subscription) => subscription.agentId),
  });
  const agentNames = useMemo(
    () => new Map((agents.data ?? []).map((agent) => [agent.id, agent.display_name])),
    [agents.data],
  );

  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border bg-muted/50">
            {COLUMNS.map(({ label, align }, index) => (
              <th
                key={label || index}
                className={`px-3 py-2 font-medium text-muted-foreground ${
                  align === "right" ? "text-right" : "text-left"
                }`}
              >
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {subscriptions.map((subscription) => {
            const paused = subscription.state === "paused";
            return (
              <tr
                key={subscription.id}
                id={`subscription-${subscription.id}`}
                className="border-b border-border last:border-b-0 hover:bg-muted/30"
                data-testid="subscription-row"
              >
                <td className="px-3 py-2">
                  <div className="font-medium">
                    {eventLabel(subscription.kind, subscription.match)}
                  </div>
                  <div className="font-mono text-xs text-muted-foreground">
                    {subscription.source}/{subscription.kind}
                  </div>
                  {subscription.note && (
                    <div className="text-xs text-muted-foreground">{subscription.note}</div>
                  )}
                </td>
                <td className="px-3 py-2 font-mono text-xs">{formatMatch(subscription.match)}</td>
                <td className="px-3 py-2">
                  <SubscriptionTarget subscription={subscription} agentNames={agentNames} />
                </td>
                <td className="px-3 py-2">
                  {paused ? (
                    <Badge variant="secondary" className="text-xs">
                      Paused
                    </Badge>
                  ) : (
                    <span className="text-xs text-muted-foreground">Active</span>
                  )}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">{subscription.fireCount}</td>
                <td
                  className="whitespace-nowrap px-3 py-2 text-right text-muted-foreground"
                  title={
                    subscription.lastFiredAt
                      ? absoluteTime(subscription.lastFiredAt * 1000)
                      : undefined
                  }
                >
                  {formatLastFired(subscription.lastFiredAt, now)}
                </td>
                <td className="whitespace-nowrap px-3 py-2 text-right">
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={paused ? "Resume subscription" : "Pause subscription"}
                    componentId={paused ? "subscriptions.resume" : "subscriptions.pause"}
                    disabled={setState.isPending}
                    onClick={() =>
                      setState.mutate({
                        id: subscription.id,
                        state: paused ? "active" : "paused",
                      })
                    }
                  >
                    {paused ? <PlayIcon className="size-4" /> : <PauseIcon className="size-4" />}
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label="Delete subscription"
                    componentId="subscriptions.delete"
                    disabled={remove.isPending}
                    onClick={() => remove.mutate(subscription.id)}
                  >
                    <Trash2Icon className="size-4" />
                  </Button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function EmptyState() {
  return (
    <div
      className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border px-6 py-12 text-center text-sm text-muted-foreground"
      data-testid="subscriptions-empty"
    >
      <BellRingIcon className="h-5 w-5" />
      <p className="font-medium text-foreground">No subscriptions yet</p>
      <p className="max-w-md">
        Connect a repository to wake a session or start an agent when an issue opens, a check fails,
        a pull request arrives or someone comments.
      </p>
    </div>
  );
}

interface SubscriptionsPageProps {
  /** Injectable clock for deterministic relative times in tests. */
  now?: Date;
}

export function SubscriptionsPage({ now }: SubscriptionsPageProps = {}) {
  const { data, isLoading, isError, refetch } = useSubscriptions();
  const [connectOpen, setConnectOpen] = useState(false);
  const nowMs = (now ?? new Date()).getTime();

  return (
    <PageScroll>
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 py-8">
        <h1 className="text-lg font-semibold">Subscriptions</h1>

        <FactoryCard />

        <div className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <h2 className="text-sm font-semibold">Your subscriptions</h2>
            <Button
              size="sm"
              className="gap-1.5"
              onClick={() => setConnectOpen(true)}
              data-testid="connect-repository-open"
              componentId="subscriptions.connect.open"
            >
              <PlusIcon className="size-4" />
              Connect a repository
            </Button>
          </div>

          {isLoading && (
            <div className="flex h-32 items-center justify-center">
              <Loader2Icon className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          )}
          {isError && (
            <div className="flex h-32 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
              <TriangleAlertIcon className="h-5 w-5" />
              <p>Failed to load subscriptions</p>
              <button
                type="button"
                className="text-primary underline"
                onClick={() => void refetch()}
              >
                Retry
              </button>
            </div>
          )}
          {data &&
            (data.length === 0 ? (
              <EmptyState />
            ) : (
              <SubscriptionTable subscriptions={data} now={nowMs} />
            ))}
        </div>
      </div>
      <ConnectRepositoryDialog open={connectOpen} onOpenChange={setConnectOpen} />
    </PageScroll>
  );
}
