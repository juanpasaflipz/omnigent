import { cleanup, render, screen, waitFor } from "@testing-library/react";
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
import {
  CONNECTED_REPOS,
  routeFetch,
  subscriptionWire,
  UNCONNECTED_REPOS,
} from "@/test/subscriptionFetchMocks";
import { ConnectRepositoryDialog } from "./ConnectRepositoryDialog";

const CONNECT_OK = { body: { status: "created", hook_id: 7, events: ["issues"] } };

function renderDialog(onOpenChange = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <ConnectRepositoryDialog open onOpenChange={onOpenChange} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return onOpenChange;
}

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("ConnectRepositoryDialog", () => {
  it("lists the caller's repositories in the picker", async () => {
    routeFetch({ "GET /v1/connections/github/repos": CONNECTED_REPOS });
    renderDialog();

    const picker = await screen.findByLabelText("Repository");
    const options = Array.from((picker as HTMLSelectElement).options).map((o) => o.value);
    expect(options).toEqual(["", "acme/api", "acme/web"]);
    expect(screen.getByTestId("connect-repository-submit")).toBeDisabled();
  });

  it("points to Settings when GitHub is not connected", async () => {
    routeFetch({ "GET /v1/connections/github/repos": UNCONNECTED_REPOS });
    renderDialog();

    const notice = await screen.findByTestId("github-not-connected");
    expect(notice).toHaveTextContent("Connect your GitHub account first");
    expect(screen.getByRole("link", { name: /Sandbox Integrations/ })).toHaveAttribute(
      "href",
      "/settings/integrations",
    );
    expect(screen.queryByLabelText("Repository")).toBeNull();
    expect(screen.getByTestId("connect-repository-submit")).toBeDisabled();
  });

  it("connects the repository then creates one launch subscription per checked event", async () => {
    const user = userEvent.setup();
    const requests = routeFetch({
      "GET /v1/connections/github/repos": CONNECTED_REPOS,
      "POST /v1/subscriptions/github/connect": CONNECT_OK,
      "POST /v1/subscriptions": { body: subscriptionWire() },
      "GET /v1/subscriptions": { body: { subscriptions: [] } },
    });
    const onOpenChange = renderDialog();

    await user.selectOptions(await screen.findByLabelText("Repository"), "acme/api");
    await user.selectOptions(screen.getByLabelText("Agent"), "agent-1");
    const prompt = screen.getByLabelText("Prompt");
    await user.clear(prompt);
    await user.type(prompt, "Triage {{fields_json}");
    await user.type(screen.getByLabelText("Cost cap per session (USD)"), "2");
    await user.click(screen.getByLabelText("Failed checks"));
    await user.click(screen.getByLabelText("Comments"));
    await user.click(screen.getByTestId("connect-repository-submit"));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    const writes = requests.filter((request) => request.method === "POST");
    expect(writes.map((request) => request.path)).toEqual([
      "/v1/subscriptions/github/connect",
      "/v1/subscriptions",
      "/v1/subscriptions",
      "/v1/subscriptions",
    ]);
    expect(writes[0]!.body).toEqual({ owner: "acme", repo: "api" });
    const launch = {
      source: "github",
      agent_id: "agent-1",
      prompt_template: "Triage {fields_json}",
      max_cost_usd: 2,
    };
    expect(writes.slice(1).map((request) => request.body)).toEqual([
      {
        ...launch,
        kind: "issues.opened",
        match: { repo: "acme/api" },
        note: "New issues in acme/api",
      },
      {
        ...launch,
        kind: "check_run.completed",
        match: { repo: "acme/api", conclusion: "failure" },
        note: "Failed checks in acme/api",
      },
      {
        ...launch,
        kind: "issue_comment.created",
        match: { repo: "acme/api" },
        note: "Comments in acme/api",
      },
    ]);
  });

  it("wakes a picked session instead of starting an agent", async () => {
    const user = userEvent.setup();
    const requests = routeFetch({
      "GET /v1/connections/github/repos": CONNECTED_REPOS,
      "GET /v1/sessions": { body: { data: [{ id: "conv_1", title: "Release work" }] } },
      "POST /v1/subscriptions/github/connect": CONNECT_OK,
      "POST /v1/subscriptions": { body: subscriptionWire() },
      "GET /v1/subscriptions": { body: { subscriptions: [] } },
    });
    const onOpenChange = renderDialog();

    await user.selectOptions(await screen.findByLabelText("Repository"), "acme/web");
    await user.click(screen.getByRole("button", { name: "Wake a session" }));
    await user.selectOptions(await screen.findByLabelText("Session to wake"), "conv_1");
    await user.click(screen.getByLabelText("New issues"));
    await user.click(screen.getByLabelText("New PRs"));
    await user.click(screen.getByTestId("connect-repository-submit"));

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    const created = requests.filter(
      (request) => request.method === "POST" && request.path === "/v1/subscriptions",
    );
    expect(created.map((request) => request.body)).toEqual([
      {
        source: "github",
        kind: "pull_request.opened",
        match: { repo: "acme/web" },
        note: "New PRs in acme/web",
        conversation_id: "conv_1",
      },
    ]);
  });

  it("shows the connect error from the server verbatim and creates nothing", async () => {
    const user = userEvent.setup();
    const message =
      "GitHub refused to manage webhooks on acme/api. The GitHub App needs the Repository permission 'Webhooks: Read and write'.";
    const requests = routeFetch({
      "GET /v1/connections/github/repos": CONNECTED_REPOS,
      "POST /v1/subscriptions/github/connect": {
        status: 403,
        body: { error: { code: "upstream_permission_denied", message } },
      },
      "GET /v1/subscriptions": { body: { subscriptions: [] } },
    });
    const onOpenChange = renderDialog();

    await user.selectOptions(await screen.findByLabelText("Repository"), "acme/api");
    await user.selectOptions(screen.getByLabelText("Agent"), "agent-1");
    await user.click(screen.getByTestId("connect-repository-submit"));

    expect(await screen.findByTestId("connect-repository-error")).toHaveTextContent(message);
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(
      requests.some((request) => request.method === "POST" && request.path === "/v1/subscriptions"),
    ).toBe(false);
  });

  it("needs at least one event", async () => {
    const user = userEvent.setup();
    routeFetch({ "GET /v1/connections/github/repos": CONNECTED_REPOS });
    renderDialog();

    await user.selectOptions(await screen.findByLabelText("Repository"), "acme/api");
    await user.selectOptions(screen.getByLabelText("Agent"), "agent-1");
    expect(screen.getByTestId("connect-repository-submit")).toBeEnabled();
    await user.click(screen.getByLabelText("New issues"));
    expect(screen.getByTestId("connect-repository-submit")).toBeDisabled();
  });
});
