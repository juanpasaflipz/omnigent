import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/analytics", () => ({
  useOmnigentAnalytics: () => ({ trackClick: vi.fn() }),
}));

import { authenticatedFetch } from "@/lib/identity";
import { SHIMS_DOCS_URL, ShimsPage } from "./ShimsPage";

const NOW = new Date("2026-10-22T12:00:00Z");

function shimWire(overrides: Record<string, unknown> = {}) {
  return {
    name: "compaction.bare_model_provider_prefix",
    reason: "Generic LLM client routes prefix-less ids to OpenAI.",
    model_gap: "Model (or caller) emits a bare provider model id.",
    added_in: "0.19.0",
    review_by: "0.24.0",
    owner: "runtime",
    fire_count: 12,
    last_fired_at: "2026-10-20T12:00:00+00:00",
    by_model: { "claude-haiku-4-5": 9, unknown: 3 },
    retirable: false,
    ...overrides,
  };
}

function mockReport(shims: unknown[], idleDays = 30) {
  vi.mocked(authenticatedFetch).mockImplementation(async (url) => {
    const days = Number(new URL(String(url), "http://x").searchParams.get("idle_days"));
    return new Response(JSON.stringify({ idle_days: days || idleDays, shims }), { status: 200 });
  });
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ShimsPage now={NOW} />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("ShimsPage", () => {
  it("renders one row per shim with owner, fires, last fired and review-by", async () => {
    mockReport([shimWire()]);
    renderPage();

    const row = (await screen.findAllByTestId("shim-row"))[0]!;
    expect(within(row).getByText("compaction.bare_model_provider_prefix")).toBeInTheDocument();
    expect(within(row).getByText("runtime")).toBeInTheDocument();
    expect(within(row).getByText("12")).toBeInTheDocument();
    expect(within(row).getByText("2d ago")).toBeInTheDocument();
    expect(within(row).getByText("0.24.0")).toBeInTheDocument();
    expect(within(row).getByText("Active")).toBeInTheDocument();
    expect(authenticatedFetch).toHaveBeenCalledWith("/v1/shims?idle_days=30");
  });

  it("shows the Retirable badge only on retirable shims", async () => {
    mockReport([
      shimWire(),
      shimWire({
        name: "llm.context_overflow_text_sniff",
        fire_count: 0,
        last_fired_at: null,
        by_model: {},
        retirable: true,
      }),
    ]);
    renderPage();

    const rows = await screen.findAllByTestId("shim-row");
    expect(within(rows[0]!).queryByText("Retirable")).toBeNull();
    expect(within(rows[1]!).getByText("Retirable")).toBeInTheDocument();
    expect(within(rows[1]!).getByText("Never")).toBeInTheDocument();
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
  });

  it("expands a row to show reason, model gap and per-model counts", async () => {
    mockReport([shimWire()]);
    renderPage();

    const toggle = await screen.findByRole("button", {
      name: "compaction.bare_model_provider_prefix",
    });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/routes prefix-less ids/)).toBeNull();

    await userEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/routes prefix-less ids/)).toBeInTheDocument();
    expect(screen.getByText(/emits a bare provider model id/)).toBeInTheDocument();
    const counts = screen.getByRole("list", { name: "Fires by model" });
    expect(
      within(counts)
        .getAllByRole("listitem")
        .map((item) => item.textContent),
    ).toEqual(["claude-haiku-4-59", "unknown3"]);
  });

  it("refetches with the selected idle window", async () => {
    mockReport([shimWire()]);
    renderPage();
    await screen.findAllByTestId("shim-row");

    await userEvent.click(screen.getByRole("button", { name: "90 days" }));

    expect(await screen.findByText(/No firing in the last 90 days/)).toBeInTheDocument();
    expect(authenticatedFetch).toHaveBeenLastCalledWith("/v1/shims?idle_days=90");
    expect(screen.getByRole("button", { name: "90 days" })).toHaveAttribute("aria-pressed", "true");
  });

  it("explains shims and links the docs when none are registered", async () => {
    mockReport([]);
    renderPage();

    const empty = await screen.findByTestId("shims-empty");
    expect(
      within(empty).getByText(/deterministic patch over a model or provider gap/),
    ).toBeInTheDocument();
    expect(within(empty).getByRole("link", { name: "Read about shims" })).toHaveAttribute(
      "href",
      SHIMS_DOCS_URL,
    );
  });

  it("offers a retry when the report fails to load", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response("nope", { status: 500 }));
    renderPage();

    expect(await screen.findByText("Failed to load shims")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});
