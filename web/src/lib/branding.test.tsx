import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import {
  DEFAULT_APP_NAME,
  DEFAULT_HEADING,
  useAppName,
  useHeading,
  usePoweredBy,
} from "./branding";
import { CapabilitiesContext } from "./CapabilitiesContext";
import type { Branding, ServerInfo } from "./capabilities";

function withInfo(info: ServerInfo | "loading") {
  return ({ children }: { children: ReactNode }) => (
    <CapabilitiesContext.Provider value={info}>{children}</CapabilitiesContext.Provider>
  );
}

function infoWith(branding: Partial<Branding> | null): ServerInfo {
  return {
    branding:
      branding === null
        ? null
        : {
            app_name: null,
            heading: null,
            logos: { main: null, loading: null, favicon: null },
            powered_by: true,
            ...branding,
          },
  } as ServerInfo;
}

function poweredBy(info: ServerInfo | "loading"): boolean {
  return renderHook(() => usePoweredBy(), { wrapper: withInfo(info) }).result.current;
}

describe("branding defaults", () => {
  it("defaults to the WakeVane name and tagline", () => {
    expect(DEFAULT_APP_NAME).toBe("WakeVane");
    expect(DEFAULT_HEADING).toBe("Wake agents. Steer outcomes.");
    const wrapper = withInfo(infoWith(null));
    expect(renderHook(() => useAppName(), { wrapper }).result.current).toBe("WakeVane");
    expect(renderHook(() => useHeading(), { wrapper }).result.current).toBe(
      "Wake agents. Steer outcomes.",
    );
  });

  it("keeps an explicit empty heading", () => {
    const wrapper = withInfo(infoWith({ heading: "" }));
    expect(renderHook(() => useHeading(), { wrapper }).result.current).toBe("");
  });

  it("shows the Omnigent credit with the default WakeVane branding", () => {
    expect(poweredBy(infoWith({ app_name: "WakeVane" }))).toBe(true);
    expect(poweredBy(infoWith(null))).toBe(true);
  });

  it("shows the credit for any other custom name", () => {
    expect(poweredBy(infoWith({ app_name: "Acme Agent" }))).toBe(true);
  });

  it("hides the credit when disabled, for Omnigent itself, and while loading", () => {
    expect(poweredBy(infoWith({ app_name: "WakeVane", powered_by: false }))).toBe(false);
    expect(poweredBy(infoWith({ app_name: "Omnigent" }))).toBe(false);
    expect(poweredBy("loading")).toBe(false);
  });
});
