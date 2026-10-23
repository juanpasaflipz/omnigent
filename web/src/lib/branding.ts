import type { Branding } from "./capabilities";
import { useServerInfo } from "./CapabilitiesContext";

export const DEFAULT_APP_NAME = "WakeVane";
export const DEFAULT_HEADING = "Wake agents. Steer outcomes.";

const EMPTY_BRANDING: Branding = {
  app_name: null,
  heading: null,
  logos: { main: null, loading: null, favicon: null },
  powered_by: true,
};

/** Current operator branding, or all-null while loading / when unset. */
export function useBranding(): Branding {
  const info = useServerInfo();
  return info !== "loading" && info.branding ? info.branding : EMPTY_BRANDING;
}

/** Operator app name, falling back to the built-in default. */
export function useAppName(): string {
  return useBranding().app_name ?? DEFAULT_APP_NAME;
}

/** Operator hero heading; an explicit `""` is kept (hides it), default only when unset. */
export function useHeading(): string {
  return useBranding().heading ?? DEFAULT_HEADING;
}

/** Logo URL for a variant, or null to fall back to the mascot; `loading` falls back to `main`. */
export function useLogoUrl(variant: "main" | "loading"): string | null {
  const { logos } = useBranding();
  return variant === "loading" ? (logos.loading ?? logos.main) : logos.main;
}

/** Show "Powered by Omnigent" whenever the app is not named Omnigent, unless disabled. */
export function usePoweredBy(): boolean {
  const info = useServerInfo();
  const branding = useBranding();
  return (
    info !== "loading" &&
    (branding.app_name ?? DEFAULT_APP_NAME) !== "Omnigent" &&
    branding.powered_by
  );
}
