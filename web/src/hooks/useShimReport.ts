import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { fetchShimReport, type ShimReport } from "@/lib/shimsApi";

export function useShimReport(idleDays: number) {
  return useQuery<ShimReport>({
    queryKey: ["shims", idleDays],
    queryFn: () => fetchShimReport(idleDays),
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  });
}
