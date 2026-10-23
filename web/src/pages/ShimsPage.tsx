import { Fragment, useState } from "react";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  Loader2Icon,
  TriangleAlertIcon,
  WrenchIcon,
} from "lucide-react";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { PageScroll } from "@/components/PageScroll";
import { Badge } from "@/components/ui/badge";
import { useShimReport } from "@/hooks/useShimReport";
import { absoluteTime, relativeTime } from "@/lib/relativeTime";
import type { ShimStatus } from "@/lib/shimsApi";
import { cn } from "@/lib/utils";

/** Maintained shim registry docs; the in-repo source is `docs/SHIMS.md`. */
export const SHIMS_DOCS_URL = "https://github.com/omnigent-ai/omnigent/blob/main/docs/SHIMS.md";

const IDLE_DAY_OPTIONS = [7, 30, 90] as const;
type IdleDays = (typeof IDLE_DAY_OPTIONS)[number];

const COLUMNS: { label: string; align?: "right" }[] = [
  { label: "Shim" },
  { label: "Owner" },
  { label: "Fires", align: "right" },
  { label: "Last fired", align: "right" },
  { label: "Review by", align: "right" },
  { label: "Status", align: "right" },
];

function formatLastFired(lastFiredAt: string | null, nowMs: number): string {
  if (lastFiredAt == null) return "Never";
  const label = relativeTime(Date.parse(lastFiredAt), nowMs);
  if (label === "") return "—";
  return label === "now" ? "just now" : `${label} ago`;
}

function ShimDetails({ shim }: { shim: ShimStatus }) {
  const models = Object.entries(shim.byModel).sort(([, a], [, b]) => b - a);
  return (
    <dl className="grid gap-x-4 gap-y-2 text-xs sm:grid-cols-[8rem_1fr]">
      <dt className="text-muted-foreground">Reason</dt>
      <dd>{shim.reason}</dd>
      <dt className="text-muted-foreground">Model gap</dt>
      <dd>{shim.modelGap}</dd>
      <dt className="text-muted-foreground">Added in</dt>
      <dd className="tabular-nums">{shim.addedIn}</dd>
      <dt className="text-muted-foreground">Fires by model</dt>
      <dd>
        {models.length === 0 ? (
          <span className="text-muted-foreground">No firings recorded</span>
        ) : (
          <ul className="flex flex-col gap-0.5" aria-label="Fires by model">
            {models.map(([model, count]) => (
              <li key={model} className="flex justify-between gap-4 sm:max-w-sm">
                <span className="truncate">{model}</span>
                <span className="tabular-nums text-muted-foreground">{count}</span>
              </li>
            ))}
          </ul>
        )}
      </dd>
    </dl>
  );
}

function ShimTable({ shims, now }: { shims: ShimStatus[]; now: number }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  function toggle(name: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border bg-muted/50">
            {COLUMNS.map(({ label, align }) => (
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
          {shims.map((shim) => {
            const isOpen = expanded.has(shim.name);
            const Chevron = isOpen ? ChevronDownIcon : ChevronRightIcon;
            return (
              <Fragment key={shim.name}>
                <tr
                  className={cn(
                    "border-b border-border hover:bg-muted/30",
                    !isOpen && "last:border-b-0",
                  )}
                  data-testid="shim-row"
                >
                  <td className="px-3 py-2">
                    <button
                      type="button"
                      aria-expanded={isOpen}
                      onClick={() => toggle(shim.name)}
                      className="inline-flex items-center gap-1 text-left font-mono text-xs text-foreground hover:underline"
                    >
                      <Chevron className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                      {shim.name}
                    </button>
                  </td>
                  <td className="px-3 py-2 text-muted-foreground">{shim.owner}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{shim.fireCount}</td>
                  <td
                    className="whitespace-nowrap px-3 py-2 text-right text-muted-foreground"
                    title={
                      shim.lastFiredAt ? absoluteTime(Date.parse(shim.lastFiredAt)) : undefined
                    }
                  >
                    {formatLastFired(shim.lastFiredAt, now)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-muted-foreground">
                    {shim.reviewBy}
                  </td>
                  <td className="px-3 py-2 text-right">
                    {shim.retirable ? (
                      <Badge variant="destructive" className="text-xs">
                        Retirable
                      </Badge>
                    ) : (
                      <span className="text-xs text-muted-foreground">Active</span>
                    )}
                  </td>
                </tr>
                {isOpen && (
                  <tr className="border-b border-border bg-muted/20 last:border-b-0">
                    <td colSpan={COLUMNS.length} className="px-3 py-3">
                      <ShimDetails shim={shim} />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function EmptyState() {
  return (
    <div
      className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border px-6 py-12 text-center text-sm text-muted-foreground"
      data-testid="shims-empty"
    >
      <WrenchIcon className="h-5 w-5" />
      <p className="font-medium text-foreground">No shims registered</p>
      <p className="max-w-md">
        A shim is a small, deterministic patch over a model or provider gap. Each one records when
        it fires, so a shim that has gone idle can be retired once the gap closes.
      </p>
      <a href={SHIMS_DOCS_URL} target="_blank" rel="noreferrer" className="text-primary underline">
        Read about shims
      </a>
    </div>
  );
}

interface ShimsPageProps {
  /** Injectable clock for deterministic relative times in tests. */
  now?: Date;
}

export function ShimsPage({ now }: ShimsPageProps = {}) {
  const [idleDays, setIdleDays] = useState<IdleDays>(30);
  const { data, isLoading, isError, refetch } = useShimReport(idleDays);
  const { trackClick } = useOmnigentAnalytics();
  const nowMs = (now ?? new Date()).getTime();
  const retirableCount = data?.shims.filter((s) => s.retirable).length ?? 0;

  return (
    <PageScroll>
      <div className="mx-auto w-full max-w-4xl px-4 py-8">
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-lg font-semibold">Shims</h1>
          <div className="flex items-center gap-1.5" role="group" aria-label="Idle window">
            <span className="mr-1 text-xs text-muted-foreground">Retirable after</span>
            {IDLE_DAY_OPTIONS.map((days) => (
              <button
                type="button"
                key={days}
                aria-pressed={idleDays === days}
                className={cn(
                  "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                  idleDays === days
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted text-muted-foreground hover:bg-muted/80",
                )}
                onClick={() => {
                  trackClick(`shims.idle_${days}d`, "button");
                  setIdleDays(days);
                }}
              >
                {days} days
              </button>
            ))}
          </div>
        </div>

        {isLoading && (
          <div className="flex h-64 items-center justify-center">
            <Loader2Icon className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        )}

        {isError && (
          <div className="flex h-64 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
            <TriangleAlertIcon className="h-5 w-5" />
            <p>Failed to load shims</p>
            <button
              type="button"
              className="text-primary underline"
              onClick={() => {
                trackClick("shims.retry", "button");
                void refetch();
              }}
            >
              Retry
            </button>
          </div>
        )}

        {data &&
          (data.shims.length === 0 ? (
            <EmptyState />
          ) : (
            <div className="flex flex-col gap-4">
              <div className="rounded-lg border border-border bg-card p-4">
                <p className="text-xs text-muted-foreground">Retirable shims</p>
                <p className="mt-1 text-2xl font-semibold tabular-nums">
                  {retirableCount} of {data.shims.length}
                </p>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  No firing in the last {data.idleDays} days.{" "}
                  <a
                    href={SHIMS_DOCS_URL}
                    target="_blank"
                    rel="noreferrer"
                    className="text-primary underline"
                  >
                    About shims
                  </a>
                </p>
              </div>
              <ShimTable shims={data.shims} now={nowMs} />
            </div>
          ))}
      </div>
    </PageScroll>
  );
}
