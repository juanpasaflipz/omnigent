import type { ReactNode } from "react";
import { TriangleAlertIcon } from "lucide-react";
import { Link } from "@/lib/routing";
import { formatSessionCostUsd, formatTokenCount } from "@/lib/formatCost";
import type { EfficiencyReport, EfficiencyTotals, SessionEfficiency } from "@/lib/usageApi";

/**
 * A session is an outlier when its cache hit rate trails its harness average
 * by more than this many fraction points (25 percentage points). Matches the
 * CLI's `omni usage --efficiency` marker.
 */
export const EFFICIENCY_OUTLIER_GAP = 0.25;

/** Placeholder for a rate the server could not compute (no tokens recorded). */
const NO_VALUE = "—";

/**
 * Ids of sessions whose cache hit rate is more than {@link EFFICIENCY_OUTLIER_GAP}
 * below their harness's rate. Sessions without a resolved harness compare
 * against the `"unknown"` bucket the server groups them under; a missing rate
 * on either side never flags.
 */
export function efficiencyOutlierIds(
  report: Pick<EfficiencyReport, "byHarness" | "sessions">,
): Set<string> {
  const flagged = new Set<string>();
  for (const session of report.sessions) {
    const harnessRate = report.byHarness[session.harness ?? "unknown"]?.cacheHitRate ?? null;
    if (session.cacheHitRate == null || harnessRate == null) continue;
    if (harnessRate - session.cacheHitRate > EFFICIENCY_OUTLIER_GAP) flagged.add(session.id);
  }
  return flagged;
}

function formatPercent(rate: number | null): string {
  return rate == null ? NO_VALUE : `${Math.round(rate * 100)}%`;
}

function formatCost(costUsd: number | null): string {
  return costUsd == null ? NO_VALUE : formatSessionCostUsd(costUsd);
}

interface Column<Row> {
  label: string;
  align?: "right";
  render: (row: Row) => ReactNode;
}

function EfficiencyTable<Row>({
  rows,
  columns,
  rowKey,
  emptyLabel,
  testId,
}: {
  rows: Row[];
  columns: Column<Row>[];
  rowKey: (row: Row) => string;
  emptyLabel: string;
  testId: string;
}) {
  if (rows.length === 0) {
    return (
      <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
        {emptyLabel}
      </div>
    );
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-border" data-testid={testId}>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border bg-muted/50">
            {columns.map(({ label, align }) => (
              <th
                key={label}
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
          {rows.map((row) => (
            <tr
              key={rowKey(row)}
              className="border-b border-border last:border-b-0 hover:bg-muted/30"
            >
              {columns.map(({ label, align, render }) => (
                <td
                  key={label}
                  className={`px-3 py-2 ${align === "right" ? "text-right tabular-nums" : ""}`}
                >
                  {render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

const RATE_COLUMNS = [
  {
    label: "Cache hit",
    align: "right" as const,
    render: (row: { cacheHitRate: number | null }) => formatPercent(row.cacheHitRate),
  },
  {
    label: "Output share",
    align: "right" as const,
    render: (row: { outputShare: number | null }) => formatPercent(row.outputShare),
  },
];

type HarnessRow = EfficiencyTotals & { name: string };

const HARNESS_COLUMNS: Column<HarnessRow>[] = [
  {
    label: "Harness",
    render: (row) => <span className="text-muted-foreground">{row.name}</span>,
  },
  { label: "Sessions", align: "right", render: (row) => row.sessions },
  { label: "Cost", align: "right", render: (row) => formatSessionCostUsd(row.costUsd) },
  { label: "Cost/session", align: "right", render: (row) => formatCost(row.costPerSessionUsd) },
  ...RATE_COLUMNS,
];

type ModelRow = EfficiencyReport["byModel"][string] & { name: string };

const MODEL_COLUMNS: Column<ModelRow>[] = [
  { label: "Model", render: (row) => row.name },
  { label: "Cost", align: "right", render: (row) => formatSessionCostUsd(row.costUsd) },
  { label: "Cost share", align: "right", render: (row) => formatPercent(row.costShare) },
  ...RATE_COLUMNS,
  {
    label: "Prompt tokens",
    align: "right",
    render: (row) => formatTokenCount(row.promptTokens),
  },
];

function sessionColumns(outliers: Set<string>): Column<SessionEfficiency>[] {
  return [
    {
      label: "Session",
      render: (s) => (
        <span className="flex max-w-xs items-center gap-1.5">
          <Link to={`/c/${s.id}`} className="truncate text-foreground hover:underline">
            {s.title ?? "Untitled"}
          </Link>
          {outliers.has(s.id) && (
            <span
              data-testid="efficiency-outlier"
              role="img"
              aria-label="Cache hit rate far below harness average"
              title={`Cache hit rate is more than ${Math.round(
                EFFICIENCY_OUTLIER_GAP * 100,
              )} points below the ${s.harness ?? "unknown"} average`}
              className="inline-flex shrink-0 text-amber-600 dark:text-amber-400"
            >
              <TriangleAlertIcon className="h-3.5 w-3.5" />
            </span>
          )}
        </span>
      ),
    },
    {
      label: "Harness",
      render: (s) => <span className="text-muted-foreground">{s.harness ?? NO_VALUE}</span>,
    },
    {
      label: "Model",
      render: (s) => (
        <span className="text-xs text-muted-foreground">{s.modelIds[0] ?? NO_VALUE}</span>
      ),
    },
    { label: "Cost", align: "right", render: (s) => formatSessionCostUsd(s.costUsd) },
    ...RATE_COLUMNS,
  ];
}

function TotalsCard({ totals, windowDays }: { totals: EfficiencyTotals; windowDays: number }) {
  const stats: { label: string; value: string }[] = [
    { label: "Cost", value: formatSessionCostUsd(totals.costUsd) },
    { label: "Sessions", value: String(totals.sessions) },
    { label: "Cache hit", value: formatPercent(totals.cacheHitRate) },
    { label: "Output share", value: formatPercent(totals.outputShare) },
    { label: "Cost/session", value: formatCost(totals.costPerSessionUsd) },
  ];
  return (
    <div className="rounded-lg border border-border bg-card p-4" data-testid="efficiency-totals">
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-5">
        {stats.map(({ label, value }) => (
          <div key={label}>
            <p className="text-xs text-muted-foreground">{label}</p>
            <p className="mt-1 text-lg font-semibold tabular-nums">{value}</p>
          </div>
        ))}
      </div>
      <p className="mt-3 text-xs text-muted-foreground">
        Last {windowDays} day{windowDays !== 1 && "s"} (UTC), by last activity
      </p>
    </div>
  );
}

interface Props {
  report: EfficiencyReport;
}

/** Efficiency scorecard: totals, per-harness and per-model tables, top sessions. */
export function UsageEfficiencyView({ report }: Props) {
  const outliers = efficiencyOutlierIds(report);
  const harnessRows = Object.entries(report.byHarness)
    .map(([name, row]) => ({ ...row, name }))
    .sort((a, b) => b.costUsd - a.costUsd);
  const modelRows = Object.entries(report.byModel)
    .map(([name, row]) => ({ ...row, name }))
    .sort((a, b) => b.costUsd - a.costUsd);

  return (
    <div className="flex flex-col gap-8">
      <TotalsCard totals={report.totals} windowDays={report.windowDays} />

      <section>
        <h2 className="mb-3 text-sm font-medium text-muted-foreground">By harness</h2>
        <EfficiencyTable
          rows={harnessRows}
          columns={HARNESS_COLUMNS}
          rowKey={(row) => row.name}
          emptyLabel="No sessions"
          testId="efficiency-by-harness"
        />
      </section>

      <section>
        <h2 className="mb-3 text-sm font-medium text-muted-foreground">By model</h2>
        <EfficiencyTable
          rows={modelRows}
          columns={MODEL_COLUMNS}
          rowKey={(row) => row.name}
          emptyLabel="No model usage"
          testId="efficiency-by-model"
        />
      </section>

      <section>
        <h2 className="mb-1 text-sm font-medium text-muted-foreground">Top sessions by cost</h2>
        <p className="mb-3 text-xs text-muted-foreground">
          <TriangleAlertIcon className="mr-1 inline h-3 w-3 align-[-2px] text-amber-600 dark:text-amber-400" />
          marks a cache hit rate more than {Math.round(EFFICIENCY_OUTLIER_GAP * 100)} points below
          its harness average.
        </p>
        <EfficiencyTable
          rows={report.sessions}
          columns={sessionColumns(outliers)}
          rowKey={(s) => s.id}
          emptyLabel="No sessions"
          testId="efficiency-sessions"
        />
      </section>
    </div>
  );
}
