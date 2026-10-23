// "Connect a repository": pick a GitHub repository, choose whether its events
// wake an existing session or start an agent, and which events count. Submit
// registers the server's webhook on the repository, then creates one
// subscription per checked event. Server errors are shown verbatim.

import { useState } from "react";
import { TriangleAlertIcon } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/scheduled/Label";
import {
  NATIVE_SELECT_CLASS,
  RepositoryPicker,
  splitRepository,
} from "@/components/subscriptions/RepositoryPicker";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import {
  useGithubRepos,
  useInvalidateSubscriptions,
  useRecentSessions,
} from "@/hooks/useSubscriptions";
import {
  buildSubscriptionPayloads,
  DEFAULT_PROMPT_TEMPLATE,
  PROMPT_TEMPLATE_PLACEHOLDERS,
  REPOSITORY_EVENT_OPTIONS,
  type RepositoryEventOption,
  type SubscriptionTarget,
} from "@/lib/subscriptionEvents";
import { connectRepository, createSubscription } from "@/lib/subscriptionsApi";
import { cn } from "@/lib/utils";

type Action = "session" | "agent";
type EventId = RepositoryEventOption["id"];

interface ConnectRepositoryDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function ConnectRepositoryDialog({ open, onOpenChange }: ConnectRepositoryDialogProps) {
  const repos = useGithubRepos(open);
  const connected = repos.data?.connected === true;
  const [repository, setRepository] = useState("");
  const [action, setAction] = useState<Action>("agent");
  const [conversationId, setConversationId] = useState("");
  const [agentId, setAgentId] = useState("");
  const [promptTemplate, setPromptTemplate] = useState(DEFAULT_PROMPT_TEMPLATE);
  const [costCap, setCostCap] = useState("");
  const [events, setEvents] = useState<EventId[]>(["issues"]);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const sessions = useRecentSessions(open && connected && action === "session");
  const agents = useAvailableAgents({ enabled: open && connected && action === "agent" });
  const invalidate = useInvalidateSubscriptions();

  const costCapValue = costCap.trim() === "" ? undefined : Number(costCap);
  const costCapValid =
    costCapValue === undefined || (Number.isFinite(costCapValue) && costCapValue > 0);
  const targetChosen =
    action === "session"
      ? conversationId !== ""
      : agentId !== "" && promptTemplate.trim() !== "" && costCapValid;
  const canSubmit =
    connected && repository !== "" && events.length > 0 && targetChosen && !submitting;

  function reset() {
    setRepository("");
    setAction("agent");
    setConversationId("");
    setAgentId("");
    setPromptTemplate(DEFAULT_PROMPT_TEMPLATE);
    setCostCap("");
    setEvents(["issues"]);
    setError(null);
  }

  function handleOpenChange(next: boolean) {
    if (!next) reset();
    onOpenChange(next);
  }

  function toggleEvent(id: EventId, checked: boolean) {
    setEvents((current) =>
      checked ? [...current.filter((event) => event !== id), id] : current.filter((e) => e !== id),
    );
  }

  async function handleSubmit() {
    if (!canSubmit) return;
    const target: SubscriptionTarget =
      action === "session"
        ? { type: "session", conversationId }
        : { type: "agent", agentId, promptTemplate, maxCostUsd: costCapValue };
    setSubmitting(true);
    setError(null);
    try {
      const { owner, repo } = splitRepository(repository);
      await connectRepository(owner, repo);
      await Promise.all(
        buildSubscriptionPayloads(repository, events, target).map((payload) =>
          createSubscription(payload),
        ),
      );
      await invalidate();
      handleOpenChange(false);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
      await invalidate();
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent
        className="flex max-h-[90vh] flex-col overflow-hidden p-0 sm:max-w-[560px]"
        data-testid="connect-repository-dialog"
      >
        <DialogHeader className="shrink-0 px-6 pt-6 pb-0">
          <DialogTitle>Connect a repository</DialogTitle>
          <DialogDescription>
            Omnigent registers its webhook on the repository for you, then reacts to the events you
            pick.
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto px-6 py-4">
          <RepositoryPicker
            id="subscription-repository"
            value={repository}
            onChange={setRepository}
            disabled={submitting}
          />

          {connected && (
            <>
              <fieldset className="flex flex-col gap-2">
                <legend className="mb-1.5 text-ui font-medium leading-none">
                  What should happen
                </legend>
                <div className="flex gap-1.5" role="group" aria-label="What should happen">
                  {(
                    [
                      { value: "agent", label: "Start an agent" },
                      { value: "session", label: "Wake a session" },
                    ] as const
                  ).map((option) => (
                    <button
                      key={option.value}
                      type="button"
                      aria-pressed={action === option.value}
                      className={cn(
                        "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                        action === option.value
                          ? "bg-primary text-primary-foreground"
                          : "bg-muted text-muted-foreground hover:bg-muted/80",
                      )}
                      onClick={() => setAction(option.value)}
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              </fieldset>

              {action === "session" ? (
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor="subscription-session">Session to wake</Label>
                  <select
                    id="subscription-session"
                    className={NATIVE_SELECT_CLASS}
                    value={conversationId}
                    onChange={(event) => setConversationId(event.target.value)}
                  >
                    <option value="">Choose a session…</option>
                    {(sessions.data ?? []).map((session) => (
                      <option key={session.id} value={session.id}>
                        {session.title || session.id}
                      </option>
                    ))}
                  </select>
                  <p className="text-xs text-muted-foreground">
                    Each event posts a notice into this session and starts a turn.
                  </p>
                </div>
              ) : (
                <>
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="subscription-agent">Agent</Label>
                    <select
                      id="subscription-agent"
                      className={NATIVE_SELECT_CLASS}
                      value={agentId}
                      onChange={(event) => setAgentId(event.target.value)}
                    >
                      <option value="">Choose an agent…</option>
                      {(agents.data ?? []).map((agent) => (
                        <option key={agent.id} value={agent.id}>
                          {agent.display_name}
                        </option>
                      ))}
                    </select>
                    <p className="text-xs text-muted-foreground">
                      Each event starts a new session with this agent.
                    </p>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="subscription-prompt">Prompt</Label>
                    <Textarea
                      id="subscription-prompt"
                      value={promptTemplate}
                      rows={4}
                      className="text-ui"
                      onChange={(event) => setPromptTemplate(event.target.value)}
                    />
                    <p className="text-xs text-muted-foreground">
                      Placeholders: {PROMPT_TEMPLATE_PLACEHOLDERS.join(", ")}.
                    </p>
                  </div>
                  <div className="flex flex-col gap-1.5">
                    <Label htmlFor="subscription-cost-cap">Cost cap per session (USD)</Label>
                    <Input
                      id="subscription-cost-cap"
                      type="number"
                      min="0"
                      step="0.5"
                      placeholder="No cap"
                      value={costCap}
                      aria-invalid={!costCapValid}
                      className="text-ui sm:max-w-40"
                      onChange={(event) => setCostCap(event.target.value)}
                    />
                  </div>
                </>
              )}

              <fieldset className="flex flex-col gap-2">
                <legend className="mb-1.5 text-ui font-medium leading-none">Events</legend>
                {REPOSITORY_EVENT_OPTIONS.map((option) => (
                  <div key={option.id} className="flex items-center gap-2">
                    <Checkbox
                      id={`subscription-event-${option.id}`}
                      checked={events.includes(option.id)}
                      onCheckedChange={(checked) => toggleEvent(option.id, checked === true)}
                    />
                    <label htmlFor={`subscription-event-${option.id}`} className="text-sm">
                      {option.label}
                    </label>
                  </div>
                ))}
              </fieldset>
            </>
          )}

          {error && (
            <div
              role="alert"
              data-testid="connect-repository-error"
              className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-sm text-destructive"
            >
              <TriangleAlertIcon className="mt-0.5 size-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}
        </div>

        <DialogFooter className="mx-0 mb-0 shrink-0 rounded-none border-t-0 bg-transparent px-6 py-4 sm:justify-end">
          <Button
            variant="outline"
            onClick={() => handleOpenChange(false)}
            componentId="subscriptions.connect.cancel"
          >
            Cancel
          </Button>
          <Button
            onClick={() => void handleSubmit()}
            loading={submitting}
            disabled={!canSubmit}
            data-testid="connect-repository-submit"
            componentId="subscriptions.connect.submit"
          >
            Connect
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
