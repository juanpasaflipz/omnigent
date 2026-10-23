import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  fetchUsageEfficiency,
  type EfficiencyReport,
  type UsageEfficiencyParams,
} from "@/lib/usageApi";

export function useUsageEfficiency({ days, limit }: UsageEfficiencyParams) {
  return useQuery<EfficiencyReport>({
    queryKey: ["usage", "efficiency", days, limit],
    queryFn: () => fetchUsageEfficiency({ days, limit }),
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  });
}
