import { authenticatedFetch } from "./identity";

// ── Wire types (snake_case from server, GET /v1/shims) ──────────

interface ShimStatusWire {
  name: string;
  reason: string;
  model_gap: string;
  added_in: string;
  review_by: string;
  owner: string;
  fire_count: number;
  last_fired_at: string | null;
  by_model: Record<string, number>;
  retirable: boolean;
  first_seen_at?: string | null;
}

interface ShimReportWire {
  idle_days: number;
  shims: ShimStatusWire[];
}

// ── App types (camelCase) ───────────────────────────────────────

export interface ShimStatus {
  name: string;
  reason: string;
  modelGap: string;
  addedIn: string;
  reviewBy: string;
  owner: string;
  fireCount: number;
  /** ISO-8601 UTC timestamp of the latest firing, or `null` when it never fired. */
  lastFiredAt: string | null;
  /** Firings per model id; `"unknown"` when the call site had no model. */
  byModel: Record<string, number>;
  /** True when the server has observed the shim for `idleDays` and it has not fired in that time. */
  retirable: boolean;
  /** ISO-8601 UTC timestamp from which this deployment has observed the shim. */
  firstSeenAt: string | null;
}

export interface ShimReport {
  idleDays: number;
  shims: ShimStatus[];
}

export async function fetchShimReport(idleDays: number): Promise<ShimReport> {
  const params = new URLSearchParams({ idle_days: String(idleDays) });
  const res = await authenticatedFetch(`/v1/shims?${params.toString()}`);
  if (!res.ok) throw new Error(`Shims fetch failed: ${res.status}`);
  const wire: ShimReportWire = await res.json();
  return {
    idleDays: wire.idle_days,
    shims: (wire.shims ?? []).map((s) => ({
      name: s.name,
      reason: s.reason,
      modelGap: s.model_gap,
      addedIn: s.added_in,
      reviewBy: s.review_by,
      owner: s.owner,
      fireCount: s.fire_count,
      lastFiredAt: s.last_fired_at ?? null,
      byModel: s.by_model ?? {},
      retirable: s.retirable,
      firstSeenAt: s.first_seen_at ?? null,
    })),
  };
}
