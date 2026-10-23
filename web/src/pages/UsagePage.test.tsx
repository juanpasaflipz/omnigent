import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/analytics", () => ({
  useOmnigentAnalytics: () => ({ trackClick: vi.fn(), trackValueChange: vi.fn() }),
}));
vi.mock("@/lib/routing", () => ({
  Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
    <a href={to}>{children}</a>
  ),
}));
// Charts need a measured layout that jsdom does not provide.
vi.mock("@/components/usage/CostTimelineChart", () => ({ CostTimelineChart: () => null }));
vi.mock("@/components/usage/UsageBreakdownCharts", () => ({ UsageBreakdownCharts: () => null }));

import { authenticatedFetch } from "@/lib/identity";
import { UsagePage } from "./UsagePage";

const USAGE = {
  cost_today: 0,
  cost_last_7d: 0,
  cost_last_30d: 0,
  total_cost_usd: 0,
  daily_costs: [],
  sessions: [],
};

function efficiency(days: number) {
  return {
    object: "usage_efficiency_report",
    window_days: days,
    totals: {
      cost_usd: 3,
      prompt_tokens: 100,
      output_tokens: 10,
      cache_hit_rate: 0.5,
      output_share: 0.09,
      sessions: 1,
      cost_per_session_usd: 3,
    },
    by_harness: {},
    by_model: {},
    sessions: [],
  };
}

function mockServer() {
  vi.mocked(authenticatedFetch).mockImplementation(async (input) => {
    const url = new URL(String(input), "http://x");
    const body =
      url.pathname === "/v1/usage/efficiency"
        ? efficiency(Number(url.searchParams.get("days")))
        : USAGE;
    return new Response(JSON.stringify(body), { status: 200 });
  });
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <UsagePage />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("UsagePage efficiency tab", () => {
  it("loads the scorecard only once the Efficiency tab is opened, for the selected window", async () => {
    mockServer();
    renderPage();
    expect(await screen.findByText("Total cost")).toBeInTheDocument();
    expect(authenticatedFetch).not.toHaveBeenCalledWith(
      expect.stringContaining("/v1/usage/efficiency"),
    );

    await userEvent.click(screen.getByRole("tab", { name: "Efficiency" }));

    expect(await screen.findByTestId("efficiency-totals")).toHaveTextContent("Last 30 days");
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/usage/efficiency?days=30&limit=50");

    await userEvent.click(screen.getByRole("button", { name: "7 days" }));
    expect(await screen.findByText(/Last 7 days/)).toBeInTheDocument();
    expect(authenticatedFetch).toHaveBeenLastCalledWith("/v1/usage/efficiency?days=7&limit=50");
  });
});
