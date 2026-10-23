// "Start a software factory": one click installs the factory foreman as the
// caller's agent, connects the repository's webhook, and subscribes the
// foreman to new issues and failed checks (`POST /v1/templates/factory`).

import { useState } from "react";
import { CheckCircle2Icon, FactoryIcon, TriangleAlertIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/scheduled/Label";
import { RepositoryPicker, splitRepository } from "@/components/subscriptions/RepositoryPicker";
import { useGithubRepos, useInvalidateSubscriptions } from "@/hooks/useSubscriptions";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { customAgentsSettingsEnabled } from "@/lib/capabilities";
import { Link } from "@/lib/routing";
import { eventLabel } from "@/lib/subscriptionEvents";
import { startFactory, type FactoryResult } from "@/lib/subscriptionsApi";

export const DEFAULT_FACTORY_BUDGET_USD = 25;

function FactoryResultSummary({ result }: { result: FactoryResult }) {
  const serverInfo = useServerInfo();
  return (
    <div
      role="status"
      data-testid="factory-result"
      className="flex flex-col gap-2 rounded-md border border-success/40 bg-success/10 px-3 py-2 text-sm"
    >
      <p className="flex items-center gap-1.5 font-medium">
        <CheckCircle2Icon className="size-4 shrink-0" />
        Factory {result.agentCreated ? "started" : "already running"} for {result.repo}
      </p>
      <p>
        Agent:{" "}
        {customAgentsSettingsEnabled(serverInfo) ? (
          <Link
            to="/settings/custom-agents"
            componentId="subscriptions.factory.agent"
            className="font-mono text-primary underline"
          >
            {result.agentName}
          </Link>
        ) : (
          <span className="font-mono">{result.agentName}</span>
        )}
        {" · "}
        Webhook {result.hook.status === "created" ? "created" : "already connected"}
      </p>
      <ul className="flex flex-col gap-0.5">
        {result.subscriptions.map((subscription) => (
          <li key={subscription.id}>
            <a href={`#subscription-${subscription.id}`} className="text-primary underline">
              {eventLabel(subscription.kind, subscription.match)}
            </a>{" "}
            <span className="text-muted-foreground">
              {subscription.created ? "subscribed" : "already subscribed"}
            </span>
          </li>
        ))}
      </ul>
      <p className="text-muted-foreground">
        What happens next: open an issue in {result.repo} and a triage session will start.
      </p>
    </div>
  );
}

export function FactoryCard() {
  const repos = useGithubRepos();
  const invalidate = useInvalidateSubscriptions();
  const [repository, setRepository] = useState("");
  const [budget, setBudget] = useState(String(DEFAULT_FACTORY_BUDGET_USD));
  const [askBeforeMerge, setAskBeforeMerge] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<FactoryResult | null>(null);

  const budgetValue = Number(budget);
  const budgetValid = budget.trim() !== "" && Number.isFinite(budgetValue) && budgetValue > 0;
  const canSubmit =
    repos.data?.connected === true && repository !== "" && budgetValid && !submitting;

  async function handleStart() {
    if (!canSubmit) return;
    const { owner, repo } = splitRepository(repository);
    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      setResult(await startFactory({ owner, repo, dailyBudgetUsd: budgetValue, askBeforeMerge }));
      await invalidate();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section
      aria-labelledby="factory-card-title"
      data-testid="factory-card"
      className="flex flex-col gap-4 rounded-lg border border-border bg-card p-4"
    >
      <div className="flex items-start gap-3">
        <FactoryIcon className="mt-0.5 size-5 shrink-0 text-muted-foreground" />
        <div>
          <h2 id="factory-card-title" className="text-sm font-semibold">
            Start a software factory
          </h2>
          <p className="mt-0.5 text-sm text-muted-foreground">
            A foreman agent triages every new issue and fixes failed checks on its own pull
            requests, asking you before anything is merged.
          </p>
        </div>
      </div>

      <RepositoryPicker
        id="factory-repository"
        value={repository}
        onChange={setRepository}
        disabled={submitting}
      />

      {repos.data?.connected && (
        <div className="flex flex-wrap items-start gap-4">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="factory-budget">Budget per run (USD)</Label>
            <Input
              id="factory-budget"
              type="number"
              min="1"
              step="1"
              value={budget}
              aria-invalid={!budgetValid}
              className="text-ui w-32"
              onChange={(event) => setBudget(event.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              The foreman&apos;s spending cap; it asks you to continue at 40%.
            </p>
          </div>
          <div className="mt-6 flex h-9 items-center gap-2">
            <Checkbox
              id="factory-ask-before-merge"
              checked={askBeforeMerge}
              onCheckedChange={(checked) => setAskBeforeMerge(checked === true)}
            />
            <label htmlFor="factory-ask-before-merge" className="text-sm">
              Ask me before merging
            </label>
          </div>
          <Button
            className="mt-6 ml-auto"
            onClick={() => void handleStart()}
            loading={submitting}
            disabled={!canSubmit}
            data-testid="factory-start"
            componentId="subscriptions.factory.start"
          >
            {submitting ? "Starting…" : "Start factory"}
          </Button>
        </div>
      )}

      {error && (
        <div
          role="alert"
          data-testid="factory-error"
          className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive"
        >
          <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}
      {result && <FactoryResultSummary result={result} />}
    </section>
  );
}
