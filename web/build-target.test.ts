import { describe, expect, it } from "vitest";

import { OUT_DIRS, resolveBuildTarget } from "./build-target.ts";

describe("resolveBuildTarget", () => {
  it.each(["member", "demo"] as const)("accepts %s", (target) => {
    expect(resolveBuildTarget(target, true)).toBe(target);
  });

  it.each(["", "Member", "preview", "member "])("rejects %j when building", (raw) => {
    expect(() => resolveBuildTarget(raw, true)).toThrow();
  });

  it("requires an explicit target when building", () => {
    expect(() => resolveBuildTarget(undefined, true)).toThrow();
  });

  it("defaults to member for the dev server and tests", () => {
    expect(resolveBuildTarget(undefined, false)).toBe("member");
  });

  it("rejects unknown values even outside a build", () => {
    expect(() => resolveBuildTarget("other", false)).toThrow();
  });

  it("builds the two sites into separate folders", () => {
    expect(OUT_DIRS).toEqual({ member: "dist", demo: "dist-demo" });
  });
});
