import { authenticatedFetch } from "./identity";

// ── Wire types (snake_case from server) ─────────────────────────

interface DailyCostWire {
  day: string;
  cost_usd: number;
}

interface SessionUsageWire {
  id: string;
  created_at: number;
  updated_at: number;
  title: string | null;
  cost_usd: number;
  models: Record<string, number>;
  harness: string | null;
  other_harnesses: string[] | null;
  llm_model: string | null;
  agent_name: string | null;
}

interface UsageReportWire {
  cost_today: number;
  cost_last_7d: number;
  cost_last_30d: number;
  total_cost_usd: number;
  daily_costs: DailyCostWire[];
  sessions: SessionUsageWire[];
}

// ── App types (camelCase) ───────────────────────────────────────

export interface DailyCost {
  day: string;
  costUsd: number;
}

export interface SessionUsage {
  id: string;
  createdAt: number;
  updatedAt: number;
  title: string | null;
  costUsd: number;
  models: Record<string, number>;
  harness: string | null;
  otherHarnesses: string[] | null;
  llmModel: string | null;
  agentName: string | null;
}

export interface UsageReport {
  costToday: number;
  costLast7d: number;
  costLast30d: number;
  totalCostUsd: number;
  dailyCosts: DailyCost[];
  sessions: SessionUsage[];
}

// ── Fetch ───────────────────────────────────────────────────────

export async function fetchUsageReport(): Promise<UsageReport> {
  const res = await authenticatedFetch("/v1/usage");
  if (!res.ok) throw new Error(`Usage fetch failed: ${res.status}`);
  const wire: UsageReportWire = await res.json();
  return {
    costToday: wire.cost_today,
    costLast7d: wire.cost_last_7d,
    costLast30d: wire.cost_last_30d,
    totalCostUsd: wire.total_cost_usd,
    dailyCosts: (wire.daily_costs ?? []).map((d) => ({ day: d.day, costUsd: d.cost_usd })),
    sessions: (wire.sessions ?? []).map((s) => ({
      id: s.id,
      createdAt: s.created_at,
      updatedAt: s.updated_at,
      title: s.title,
      costUsd: s.cost_usd,
      models: s.models ?? {},
      harness: s.harness ?? null,
      otherHarnesses: s.other_harnesses ?? null,
      llmModel: s.llm_model ?? null,
      agentName: s.agent_name ?? null,
    })),
  };
}

// ── Efficiency scorecard (GET /v1/usage/efficiency) ─────────────

interface EfficiencyMetricsWire {
  cost_usd: number;
  prompt_tokens: number;
  output_tokens: number;
  cache_hit_rate: number | null;
  output_share: number | null;
}

interface EfficiencyTotalsWire extends EfficiencyMetricsWire {
  sessions: number;
  cost_per_session_usd: number | null;
}

interface ModelEfficiencyWire extends EfficiencyMetricsWire {
  cost_share: number | null;
}

interface SessionEfficiencyWire {
  id: string;
  title: string | null;
  harness: string | null;
  model_ids: string[];
  cost_usd: number;
  cache_hit_rate: number | null;
  output_share: number | null;
  prompt_tokens: number;
  output_tokens: number;
  created_at: number;
}

interface EfficiencyReportWire {
  object: "usage_efficiency_report";
  window_days: number;
  totals: EfficiencyTotalsWire;
  by_harness: Record<string, EfficiencyTotalsWire>;
  by_model: Record<string, ModelEfficiencyWire>;
  sessions: SessionEfficiencyWire[];
}

/** Rates are `null` when nothing was measured (zero denominator). */
export interface EfficiencyMetrics {
  costUsd: number;
  promptTokens: number;
  outputTokens: number;
  cacheHitRate: number | null;
  outputShare: number | null;
}

export interface EfficiencyTotals extends EfficiencyMetrics {
  sessions: number;
  costPerSessionUsd: number | null;
}

export interface ModelEfficiency extends EfficiencyMetrics {
  costShare: number | null;
}

export interface SessionEfficiency {
  id: string;
  title: string | null;
  harness: string | null;
  modelIds: string[];
  costUsd: number;
  cacheHitRate: number | null;
  outputShare: number | null;
  promptTokens: number;
  outputTokens: number;
  createdAt: number;
}

export interface EfficiencyReport {
  windowDays: number;
  totals: EfficiencyTotals;
  byHarness: Record<string, EfficiencyTotals>;
  byModel: Record<string, ModelEfficiency>;
  sessions: SessionEfficiency[];
}

export interface UsageEfficiencyParams {
  /** Window in UTC calendar days including today (server accepts 1–366). */
  days: number;
  /** Maximum top-cost session rows (server accepts 1–1000). */
  limit: number;
}

function metricsFromWire(wire: EfficiencyMetricsWire | undefined): EfficiencyMetrics {
  return {
    costUsd: wire?.cost_usd ?? 0,
    promptTokens: wire?.prompt_tokens ?? 0,
    outputTokens: wire?.output_tokens ?? 0,
    cacheHitRate: wire?.cache_hit_rate ?? null,
    outputShare: wire?.output_share ?? null,
  };
}

function totalsFromWire(wire: EfficiencyTotalsWire | undefined): EfficiencyTotals {
  return {
    ...metricsFromWire(wire),
    sessions: wire?.sessions ?? 0,
    costPerSessionUsd: wire?.cost_per_session_usd ?? null,
  };
}

export async function fetchUsageEfficiency({
  days,
  limit,
}: UsageEfficiencyParams): Promise<EfficiencyReport> {
  const params = new URLSearchParams({ days: String(days), limit: String(limit) });
  const res = await authenticatedFetch(`/v1/usage/efficiency?${params.toString()}`);
  if (!res.ok) throw new Error(`Usage efficiency fetch failed: ${res.status}`);
  const wire: EfficiencyReportWire = await res.json();
  return {
    windowDays: wire.window_days,
    totals: totalsFromWire(wire.totals),
    byHarness: Object.fromEntries(
      Object.entries(wire.by_harness ?? {}).map(([name, row]) => [name, totalsFromWire(row)]),
    ),
    byModel: Object.fromEntries(
      Object.entries(wire.by_model ?? {}).map(([model, row]) => [
        model,
        { ...metricsFromWire(row), costShare: row.cost_share ?? null },
      ]),
    ),
    sessions: (wire.sessions ?? []).map((s) => ({
      id: s.id,
      title: s.title ?? null,
      harness: s.harness ?? null,
      modelIds: s.model_ids ?? [],
      costUsd: s.cost_usd ?? 0,
      cacheHitRate: s.cache_hit_rate ?? null,
      outputShare: s.output_share ?? null,
      promptTokens: s.prompt_tokens ?? 0,
      outputTokens: s.output_tokens ?? 0,
      createdAt: s.created_at,
    })),
  };
}
