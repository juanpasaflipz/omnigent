import { useMemo, useState } from "react";
import { CalendarIcon, Loader2Icon, TriangleAlertIcon } from "lucide-react";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { PageScroll } from "@/components/PageScroll";
import { CostTimelineChart } from "@/components/usage/CostTimelineChart";
import { UsageBreakdownCharts } from "@/components/usage/UsageBreakdownCharts";
import { UsageEfficiencyView } from "@/components/usage/UsageEfficiencyView";
import { UsageSessionTable } from "@/components/usage/UsageSessionTable";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useUsageEfficiency } from "@/hooks/useUsageEfficiency";
import { useUsageReport } from "@/hooks/useUsageReport";
import { formatSessionCostUsd } from "@/lib/formatCost";
import type { DailyCost, SessionUsage } from "@/lib/usageApi";
import { cn } from "@/lib/utils";

type RangeKey = "7d" | "30d" | "90d" | "all" | "custom";
type UsageView = "overview" | "efficiency";

const USAGE_VIEWS: { value: UsageView; label: string }[] = [
  { value: "overview", label: "Overview" },
  { value: "efficiency", label: "Efficiency" },
];

function isUsageView(value: string): value is UsageView {
  return USAGE_VIEWS.some((view) => view.value === value);
}

// Server bounds for GET /v1/usage/efficiency `days`.
const EFFICIENCY_MAX_DAYS = 366;
const EFFICIENCY_SESSION_LIMIT = 50;

const RANGE_PRESETS: { key: RangeKey; label: string }[] = [
  { key: "7d", label: "7 days" },
  { key: "30d", label: "30 days" },
  { key: "90d", label: "90 days" },
  { key: "all", label: "All time" },
  { key: "custom", label: "Custom" },
];

function daysAgoIso(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days + 1);
  return d.toISOString().slice(0, 10);
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function rangeToWindow(
  key: RangeKey,
  customStart: string,
  customEnd: string,
): { since: string | null; until: string | null } {
  switch (key) {
    case "7d":
      return { since: daysAgoIso(7), until: null };
    case "30d":
      return { since: daysAgoIso(30), until: null };
    case "90d":
      return { since: daysAgoIso(90), until: null };
    case "all":
      return { since: null, until: null };
    case "custom":
      return { since: customStart || null, until: customEnd || null };
  }
}

/**
 * The efficiency scorecard always ends today and takes a day count, so a
 * custom range uses its start date; "All time" asks for the server maximum.
 */
function rangeToEfficiencyDays(key: RangeKey, customStart: string, today: string): number {
  switch (key) {
    case "7d":
      return 7;
    case "30d":
      return 30;
    case "90d":
      return 90;
    case "all":
      return EFFICIENCY_MAX_DAYS;
    case "custom": {
      const start = Date.parse(`${customStart}T00:00:00Z`);
      const end = Date.parse(`${today}T00:00:00Z`);
      if (!Number.isFinite(start) || !Number.isFinite(end)) return 30;
      const days = Math.round((end - start) / 86_400_000) + 1;
      return Math.min(EFFICIENCY_MAX_DAYS, Math.max(1, days));
    }
  }
}

function EfficiencyPanel({ days }: { days: number }) {
  const { data, isLoading, isError, refetch } = useUsageEfficiency({
    days,
    limit: EFFICIENCY_SESSION_LIMIT,
  });
  const { trackClick } = useOmnigentAnalytics();

  if (isLoading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <Loader2Icon className="h-5 w-5 animate-spin text-muted-foreground" />
      </div>
    );
  }
  if (isError || !data) {
    return (
      <div className="flex h-64 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
        <TriangleAlertIcon className="h-5 w-5" />
        <p>Failed to load efficiency data</p>
        <button
          type="button"
          className="text-primary underline"
          onClick={() => {
            trackClick("usage.efficiency_retry", "button");
            void refetch();
          }}
        >
          Retry
        </button>
      </div>
    );
  }
  return <UsageEfficiencyView report={data} />;
}

function filterDailyCosts(
  costs: DailyCost[],
  since: string | null,
  until: string | null,
): DailyCost[] {
  return costs.filter((d) => {
    if (since && d.day < since) return false;
    if (until && d.day > until) return false;
    return true;
  });
}

function filterSessions(
  sessions: SessionUsage[],
  since: string | null,
  until: string | null,
): SessionUsage[] {
  if (!since && !until) return sessions;
  const sinceEpoch = since ? new Date(since + "T00:00:00Z").getTime() / 1000 : 0;
  const untilEpoch = until ? new Date(until + "T23:59:59Z").getTime() / 1000 : Infinity;
  return sessions.filter((s) => s.updatedAt >= sinceEpoch && s.updatedAt <= untilEpoch);
}

export function UsagePage() {
  const [view, setView] = useState<UsageView>("overview");
  const { data, isLoading, isError, refetch } = useUsageReport();
  const { trackClick } = useOmnigentAnalytics();

  const [rangeKey, setRangeKey] = useState<RangeKey>("30d");
  const [customStart, setCustomStart] = useState(() => daysAgoIso(30));
  const [customEnd, setCustomEnd] = useState(todayIso);

  const today = todayIso();
  const { since, until } = rangeToWindow(rangeKey, customStart, customEnd);

  const filteredCosts = useMemo(
    () => (data ? filterDailyCosts(data.dailyCosts, since, until) : []),
    [data, since, until],
  );

  const filteredSessions = useMemo(
    () => (data ? filterSessions(data.sessions, since, until) : []),
    [data, since, until],
  );

  const totalCost = useMemo(() => {
    const fromDaily = filteredCosts.reduce((sum, d) => sum + d.costUsd, 0);
    if (fromDaily > 0) return fromDaily;
    return filteredSessions.reduce((sum, s) => sum + s.costUsd, 0);
  }, [filteredCosts, filteredSessions]);

  return (
    <PageScroll>
      <div className="mx-auto w-full max-w-4xl px-4 py-8">
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-lg font-semibold">Usage</h1>
          <div className="flex items-center gap-1.5">
            {RANGE_PRESETS.map(({ key, label }) => (
              <button
                type="button"
                key={key}
                className={cn(
                  "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                  rangeKey === key
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted text-muted-foreground hover:bg-muted/80",
                )}
                onClick={() => {
                  trackClick(`usage.range_${key}`, "button");
                  setRangeKey(key);
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        <Tabs
          value={view}
          onValueChange={(value) => {
            if (isUsageView(value)) setView(value);
          }}
          componentId="usage.view"
          className="mb-6"
        >
          <TabsList variant="pill" aria-label="Usage view" className="gap-1">
            {USAGE_VIEWS.map(({ value, label }) => (
              <TabsTrigger key={value} value={value} className="h-7 flex-none px-3">
                {label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>

        {rangeKey === "custom" && (
          <div className="mb-6 flex items-center gap-2 text-sm">
            <CalendarIcon className="h-4 w-4 text-muted-foreground" />
            <input
              type="date"
              value={customStart}
              max={customEnd < today ? customEnd : today}
              onChange={(e) => {
                const v = e.target.value;
                setCustomStart(v);
                if (v > customEnd) setCustomEnd(v);
              }}
              className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            />
            <span className="text-muted-foreground">to</span>
            <input
              type="date"
              value={customEnd}
              min={customStart}
              max={today}
              onChange={(e) => {
                const v = e.target.value;
                setCustomEnd(v);
                if (v < customStart) setCustomStart(v);
              }}
              className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            />
          </div>
        )}

        {view === "efficiency" && (
          <EfficiencyPanel days={rangeToEfficiencyDays(rangeKey, customStart, today)} />
        )}

        {view === "overview" && isLoading && (
          <div className="flex h-64 items-center justify-center">
            <Loader2Icon className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        )}

        {view === "overview" && isError && (
          <div className="flex h-64 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
            <TriangleAlertIcon className="h-5 w-5" />
            <p>Failed to load usage data</p>
            <button
              type="button"
              className="text-primary underline"
              onClick={() => {
                trackClick("usage.retry", "button");
                void refetch();
              }}
            >
              Retry
            </button>
          </div>
        )}

        {view === "overview" && data && (
          <div className="flex flex-col gap-8">
            <div className="rounded-lg border border-border bg-card p-4">
              <p className="text-xs text-muted-foreground">Total cost</p>
              <p className="mt-1 text-2xl font-semibold tabular-nums">
                {formatSessionCostUsd(totalCost)}
              </p>
              <p className="mt-0.5 text-xs text-muted-foreground">
                {filteredSessions.length} session{filteredSessions.length !== 1 && "s"}
              </p>
            </div>

            <section>
              <h2 className="mb-3 text-sm font-medium text-muted-foreground">Daily cost</h2>
              <CostTimelineChart dailyCosts={filteredCosts} />
            </section>

            <UsageBreakdownCharts sessions={filteredSessions} />

            <section>
              <h2 className="mb-3 text-sm font-medium text-muted-foreground">Sessions</h2>
              <UsageSessionTable sessions={filteredSessions} />
            </section>
          </div>
        )}
      </div>
    </PageScroll>
  );
}
