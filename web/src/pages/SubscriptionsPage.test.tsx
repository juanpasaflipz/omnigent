import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/analytics", () => ({
  useOmnigentAnalytics: () => ({ trackClick: vi.fn(), trackValueChange: vi.fn() }),
}));
vi.mock("@/hooks/useAvailableAgents", () => ({
  useAvailableAgents: () => ({
    data: [testAgent("agent-1", "triager", { display_name: "Triager" })],
    isLoading: false,
  }),
}));

import { testAgent } from "@/test/agentFixtures";
import { CONNECTED_REPOS, routeFetch, subscriptionWire } from "@/test/subscriptionFetchMocks";
import { SubscriptionsPage } from "./SubscriptionsPage";

const NOW = new Date("2026-10-22T12:00:00Z");
const TWO_HOURS_AGO = Math.floor(NOW.getTime() / 1000) - 2 * 3600;

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SubscriptionsPage now={NOW} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("SubscriptionsPage", () => {
  it("lists each subscription's event, match, target, note, state, fires and last fired", async () => {
    routeFetch({
      "GET /v1/connections/github/repos": CONNECTED_REPOS,
      "GET /v1/subscriptions": {
        body: {
          subscriptions: [
            subscriptionWire({ fire_count: 3, last_fired_at: TWO_HOURS_AGO }),
            subscriptionWire({
              id: "sub2",
              agent_id: null,
              conversation_id: "conv_9",
              kind: "check_run.completed",
              match: { repo: "acme/api", conclusion: "failure" },
              note: "CI on my PR",
              state: "paused",
            }),
          ],
        },
      },
    });
    renderPage();

    const [launch, wake] = await screen.findAllByTestId("subscription-row");
    expect(launch).toHaveAttribute("id", "subscription-sub1");
    expect(within(launch!).getByText("New issues")).toBeInTheDocument();
    expect(within(launch!).getByText("github/issues.opened")).toBeInTheDocument();
    expect(within(launch!).getByText("repo=acme/api")).toBeInTheDocument();
    expect(within(launch!).getByText("Start Triager")).toBeInTheDocument();
    expect(within(launch!).getByText("New issues in acme/api")).toBeInTheDocument();
    expect(within(launch!).getByText("Active")).toBeInTheDocument();
    expect(within(launch!).getByText("3")).toBeInTheDocument();
    expect(within(launch!).getByText("2h ago")).toBeInTheDocument();

    expect(within(wake!).getByText("Failed checks")).toBeInTheDocument();
    expect(within(wake!).getByText("repo=acme/api, conclusion=failure")).toBeInTheDocument();
    expect(within(wake!).getByRole("link", { name: "Session" })).toHaveAttribute(
      "href",
      "/c/conv_9",
    );
    expect(within(wake!).getByText("Paused")).toBeInTheDocument();
    expect(within(wake!).getByText("Never")).toBeInTheDocument();
    expect(screen.getByTestId("factory-card")).toBeInTheDocument();
  });

  it("pauses, resumes and deletes subscriptions", async () => {
    const user = userEvent.setup();
    const requests = routeFetch({
      "GET /v1/connections/github/repos": CONNECTED_REPOS,
      "GET /v1/subscriptions": {
        body: {
          subscriptions: [subscriptionWire(), subscriptionWire({ id: "sub2", state: "paused" })],
        },
      },
      "PATCH /v1/subscriptions/sub1": { body: subscriptionWire({ state: "paused" }) },
      "PATCH /v1/subscriptions/sub2": { body: subscriptionWire({ id: "sub2" }) },
      "DELETE /v1/subscriptions/sub1": { body: { deleted: true, id: "sub1" } },
    });
    renderPage();

    const [active, paused] = await screen.findAllByTestId("subscription-row");
    await user.click(within(active!).getByRole("button", { name: "Pause subscription" }));
    await user.click(within(paused!).getByRole("button", { name: "Resume subscription" }));
    await user.click(within(active!).getByRole("button", { name: "Delete subscription" }));

    await waitFor(() =>
      expect(requests.filter((request) => request.method !== "GET")).toEqual([
        { method: "PATCH", path: "/v1/subscriptions/sub1", body: { state: "paused" } },
        { method: "PATCH", path: "/v1/subscriptions/sub2", body: { state: "active" } },
        { method: "DELETE", path: "/v1/subscriptions/sub1", body: undefined },
      ]),
    );
  });

  it("shows the empty state and opens the connect dialog", async () => {
    const user = userEvent.setup();
    routeFetch({
      "GET /v1/connections/github/repos": CONNECTED_REPOS,
      "GET /v1/subscriptions": { body: { subscriptions: [] } },
    });
    renderPage();

    expect(await screen.findByTestId("subscriptions-empty")).toBeInTheDocument();
    await user.click(screen.getByTestId("connect-repository-open"));
    expect(await screen.findByTestId("connect-repository-dialog")).toBeInTheDocument();
  });

  it("offers a retry when the list fails to load", async () => {
    routeFetch({
      "GET /v1/connections/github/repos": CONNECTED_REPOS,
      "GET /v1/subscriptions": { status: 500, body: {} },
    });
    renderPage();

    expect(await screen.findByText("Failed to load subscriptions")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
