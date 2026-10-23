import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/analytics", () => ({
  useOmnigentAnalytics: () => ({ trackClick: vi.fn(), trackValueChange: vi.fn() }),
}));

import {
  CONNECTED_REPOS,
  routeFetch,
  subscriptionWire,
  UNCONNECTED_REPOS,
} from "@/test/subscriptionFetchMocks";
import { FactoryCard } from "./FactoryCard";

const FACTORY_RESULT = {
  repo: "acme/api",
  agent_id: "agent-f",
  agent_name: "factory-acme-api",
  agent_created: true,
  subscription_ids: ["sub-issues", "sub-checks"],
  subscriptions: [
    { ...subscriptionWire({ id: "sub-issues", agent_id: "agent-f" }), created: true },
    {
      ...subscriptionWire({
        id: "sub-checks",
        agent_id: "agent-f",
        kind: "check_run.completed",
        match: { repo: "acme/api", conclusion: "failure" },
      }),
      created: true,
    },
  ],
  hook: { status: "created", hook_id: 7, events: ["issues", "check_run"] },
};

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <FactoryCard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("FactoryCard", () => {
  it("sends the repository, budget and merge gate, then shows what was set up", async () => {
    const user = userEvent.setup();
    const requests = routeFetch({
      "GET /v1/connections/github/repos": CONNECTED_REPOS,
      "POST /v1/templates/factory": { body: FACTORY_RESULT },
      "GET /v1/subscriptions": { body: { subscriptions: [] } },
    });
    renderCard();

    await user.selectOptions(await screen.findByLabelText("Repository"), "acme/api");
    const budget = screen.getByLabelText("Budget per run (USD)");
    expect(budget).toHaveValue(25);
    await user.clear(budget);
    await user.type(budget, "10");
    expect(screen.getByLabelText("Ask me before merging")).toBeChecked();
    await user.click(screen.getByLabelText("Ask me before merging"));
    await user.click(screen.getByTestId("factory-start"));

    const result = await screen.findByTestId("factory-result");
    const factoryRequest = requests.find((request) => request.path === "/v1/templates/factory");
    expect(factoryRequest?.body).toEqual({
      owner: "acme",
      repo: "api",
      daily_budget_usd: 10,
      ask_before_merge: false,
    });
    expect(result).toHaveTextContent("Factory started for acme/api");
    expect(result).toHaveTextContent("factory-acme-api");
    expect(result).toHaveTextContent("Webhook created");
    expect(within(result).getByRole("link", { name: "New issues" })).toHaveAttribute(
      "href",
      "#subscription-sub-issues",
    );
    expect(within(result).getByRole("link", { name: "Failed checks" })).toHaveAttribute(
      "href",
      "#subscription-sub-checks",
    );
    expect(result).toHaveTextContent("open an issue in acme/api and a triage session will start");
  });

  it("defaults to asking before merging with a 25 USD budget", async () => {
    const user = userEvent.setup();
    const requests = routeFetch({
      "GET /v1/connections/github/repos": CONNECTED_REPOS,
      "POST /v1/templates/factory": {
        body: {
          ...FACTORY_RESULT,
          agent_created: false,
          hook: { ...FACTORY_RESULT.hook, status: "existing" },
        },
      },
      "GET /v1/subscriptions": { body: { subscriptions: [] } },
    });
    renderCard();

    await user.selectOptions(await screen.findByLabelText("Repository"), "acme/web");
    await user.click(screen.getByTestId("factory-start"));

    const result = await screen.findByTestId("factory-result");
    expect(requests.find((request) => request.path === "/v1/templates/factory")?.body).toEqual({
      owner: "acme",
      repo: "web",
      daily_budget_usd: 25,
      ask_before_merge: true,
    });
    expect(result).toHaveTextContent("already running");
    expect(result).toHaveTextContent("Webhook already connected");
  });

  it("shows the server's error verbatim", async () => {
    const user = userEvent.setup();
    const message =
      "OMNIGENT_GITHUB_WEBHOOK_SECRET is not set on the server, so GitHub deliveries cannot be verified.";
    routeFetch({
      "GET /v1/connections/github/repos": CONNECTED_REPOS,
      "POST /v1/templates/factory": {
        status: 409,
        body: { error: { code: "conflict", message } },
      },
    });
    renderCard();

    await user.selectOptions(await screen.findByLabelText("Repository"), "acme/api");
    await user.click(screen.getByTestId("factory-start"));

    expect(await screen.findByTestId("factory-error")).toHaveTextContent(message);
    expect(screen.queryByTestId("factory-result")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("factory-start")).toBeEnabled());
  });

  it("disables start for an empty budget", async () => {
    const user = userEvent.setup();
    routeFetch({ "GET /v1/connections/github/repos": CONNECTED_REPOS });
    renderCard();

    await user.selectOptions(await screen.findByLabelText("Repository"), "acme/api");
    await user.clear(screen.getByLabelText("Budget per run (USD)"));

    expect(screen.getByTestId("factory-start")).toBeDisabled();
  });

  it("asks the user to connect GitHub before anything else", async () => {
    routeFetch({ "GET /v1/connections/github/repos": UNCONNECTED_REPOS });
    renderCard();

    expect(await screen.findByTestId("github-not-connected")).toBeInTheDocument();
    expect(screen.queryByTestId("factory-start")).toBeNull();
  });
});
