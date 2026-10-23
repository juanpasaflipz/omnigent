// TanStack Query hooks over `/v1/subscriptions` and the GitHub repository list
// the Subscriptions page pickers read. Mutations invalidate the list on success.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { fetchGithubRepos, type GithubRepoList } from "@/lib/githubIntegration";
import {
  deleteSubscription,
  listRecentSessions,
  listSubscriptions,
  setSubscriptionState,
  type SessionOption,
  type Subscription,
  type SubscriptionState,
} from "@/lib/subscriptionsApi";

export const SUBSCRIPTIONS_KEY = ["subscriptions"] as const;

/** The caller's subscriptions; polled while the page is mounted to refresh fire counts. */
export function useSubscriptions() {
  return useQuery<Subscription[]>({
    queryKey: SUBSCRIPTIONS_KEY,
    queryFn: listSubscriptions,
    staleTime: 15_000,
    refetchInterval: 60_000,
  });
}

export function useInvalidateSubscriptions() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: SUBSCRIPTIONS_KEY });
}

export function useSetSubscriptionState() {
  const invalidate = useInvalidateSubscriptions();
  return useMutation({
    mutationFn: ({ id, state }: { id: string; state: SubscriptionState }) =>
      setSubscriptionState(id, state),
    onSuccess: () => invalidate(),
  });
}

export function useDeleteSubscription() {
  const invalidate = useInvalidateSubscriptions();
  return useMutation({
    mutationFn: (id: string) => deleteSubscription(id),
    onSuccess: () => invalidate(),
  });
}

/** Repositories the caller's GitHub connection can see (`connected: false` when unlinked). */
export function useGithubRepos(enabled = true) {
  return useQuery<GithubRepoList>({
    queryKey: ["github-repos"],
    queryFn: fetchGithubRepos,
    enabled,
    staleTime: 60_000,
    retry: false,
  });
}

export function useRecentSessions(enabled: boolean) {
  return useQuery<SessionOption[]>({
    queryKey: ["subscription-session-options"],
    queryFn: listRecentSessions,
    enabled,
    staleTime: 30_000,
  });
}
