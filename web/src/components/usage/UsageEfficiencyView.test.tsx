import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { efficiencyOutlierIds, UsageEfficiencyView } from "./UsageEfficiencyView";
import type { EfficiencyReport, EfficiencyTotals, SessionEfficiency } from "@/lib/usageApi";

vi.mock("@/lib/routing", () => ({
  Link: ({ children, to }: { children: React.ReactNode; to: string }) => (
    <a href={to}>{children}</a>
  ),
}));

function totals(overrides: Partial<EfficiencyTotals> = {}): EfficiencyTotals {
  return {
    costUsd: 10,
    promptTokens: 1000,
    outputTokens: 100,
    cacheHitRate: 0.8,
    outputShare: 0.09,
    sessions: 2,
    costPerSessionUsd: 5,
    ...overrides,
  };
}

function session(overrides: Partial<SessionEfficiency> = {}): SessionEfficiency {
  return {
    id: "conv_1",
    title: "Session one",
    harness: "claude-native",
    modelIds: ["claude-opus-5-5"],
    costUsd: 4,
    cacheHitRate: 0.7,
    outputShare: 0.1,
    promptTokens: 500,
    outputTokens: 50,
    createdAt: 1_700_000_000,
    ...overrides,
  };
}

function report(overrides: Partial<EfficiencyReport> = {}): EfficiencyReport {
  return {
    windowDays: 30,
    totals: totals({ costUsd: 12.5, sessions: 3, costPerSessionUsd: 4.1667 }),
    byHarness: {
      "claude-native": totals({ cacheHitRate: 0.8 }),
      unknown: totals({ cacheHitRate: 0.6, costUsd: 1 }),
    },
    byModel: {
      "claude-opus-5-5": {
        costUsd: 9,
        promptTokens: 900,
        outputTokens: 90,
        cacheHitRate: 0.75,
        outputShare: 0.09,
        costShare: 0.9,
      },
      "claude-haiku-4-5": {
        costUsd: 1,
        promptTokens: 0,
        outputTokens: 0,
        cacheHitRate: null,
        outputShare: null,
        costShare: 0.1,
      },
    },
    sessions: [
      session({ id: "conv_ok", title: "Healthy", cacheHitRate: 0.7 }),
      session({ id: "conv_bad", title: "Churny", cacheHitRate: 0.4 }),
      session({ id: "conv_orphan", title: "Orphan", harness: null, cacheHitRate: 0.3 }),
      session({ id: "conv_empty", title: "No tokens", cacheHitRate: null, outputShare: null }),
    ],
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("efficiencyOutlierIds", () => {
  it("flags sessions more than 25 points below their harness, using 'unknown' for no harness", () => {
    expect(efficiencyOutlierIds(report())).toEqual(new Set(["conv_bad", "conv_orphan"]));
  });

  it("does not flag a gap of exactly 25 points or a missing rate on either side", () => {
    const flagged = efficiencyOutlierIds({
      byHarness: { a: totals({ cacheHitRate: 0.75 }), b: totals({ cacheHitRate: null }) },
      sessions: [
        session({ id: "edge", harness: "a", cacheHitRate: 0.5 }),
        session({ id: "no-harness-rate", harness: "b", cacheHitRate: 0 }),
        session({ id: "missing-harness", harness: "zzz", cacheHitRate: 0 }),
      ],
    });
    expect(flagged.size).toBe(0);
  });
});

describe("UsageEfficiencyView", () => {
  it("renders totals with cost, sessions, cache hit, output share and cost per session", () => {
    render(<UsageEfficiencyView report={report()} />);
    const card = screen.getByTestId("efficiency-totals");
    expect(within(card).getByText("$12.50")).toBeInTheDocument();
    expect(within(card).getByText("3")).toBeInTheDocument();
    expect(within(card).getByText("80%")).toBeInTheDocument();
    expect(within(card).getByText("9%")).toBeInTheDocument();
    expect(within(card).getByText("$4.17")).toBeInTheDocument();
    expect(within(card).getByText(/Last 30 days/)).toBeInTheDocument();
  });

  it("renders per-harness and per-model tables with cost share and em-dash for no tokens", () => {
    render(<UsageEfficiencyView report={report()} />);
    const harness = screen.getByTestId("efficiency-by-harness");
    expect(within(harness).getByText("claude-native")).toBeInTheDocument();
    expect(within(harness).getByText("unknown")).toBeInTheDocument();

    const models = screen.getByTestId("efficiency-by-model");
    const haikuRow = within(models).getByText("claude-haiku-4-5").closest("tr")!;
    expect(within(haikuRow).getByText("10%")).toBeInTheDocument();
    expect(within(haikuRow).getAllByText("—")).toHaveLength(2);
    const opusRow = within(models).getByText("claude-opus-5-5").closest("tr")!;
    expect(within(opusRow).getByText("90%")).toBeInTheDocument();
  });

  it("links session rows and flags only the outliers", () => {
    render(<UsageEfficiencyView report={report()} />);
    const table = screen.getByTestId("efficiency-sessions");
    expect(within(table).getByRole("link", { name: "Churny" })).toHaveAttribute(
      "href",
      "/c/conv_bad",
    );
    const flaggedTitles = within(table)
      .getAllByTestId("efficiency-outlier")
      .map((badge) => badge.closest("tr")!.querySelector("a")!.textContent);
    expect(flaggedTitles).toEqual(["Churny", "Orphan"]);

    const emptyRow = within(table).getByRole("link", { name: "No tokens" }).closest("tr")!;
    expect(within(emptyRow).getAllByText("—")).toHaveLength(2);
  });

  it("shows an empty state when the window has no sessions", () => {
    render(
      <UsageEfficiencyView
        report={report({
          byHarness: {},
          byModel: {},
          sessions: [],
          totals: totals({ sessions: 0 }),
        })}
      />,
    );
    expect(screen.getAllByText("No sessions")).toHaveLength(2);
    expect(screen.getByText("No model usage")).toBeInTheDocument();
  });
});
