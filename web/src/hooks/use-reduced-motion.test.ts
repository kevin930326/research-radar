// 減少動態的共用判斷：用假的 matchMedia 切換系統設定，確認結果與通知跟著變、取消訂閱後不再通知、
// 沒有 matchMedia 時回 false 而且不丟例外。
import { afterEach, describe, expect, it, vi } from "vitest";

import { prefersReducedMotion, subscribeReducedMotion } from "./use-reduced-motion";

type Listener = (event: { matches: boolean }) => void;

/** 只做這個函式用得到的部分：matches、addEventListener／removeEventListener("change")。 */
function fakeMatchMedia(initial: boolean) {
  let matches = initial;
  const listeners = new Set<Listener>();
  const queries: string[] = [];
  const matchMedia = (query: string) => {
    queries.push(query);
    return {
      get matches() {
        return matches;
      },
      media: query,
      addEventListener: (type: string, listener: Listener) => {
        if (type === "change") listeners.add(listener);
      },
      removeEventListener: (type: string, listener: Listener) => {
        if (type === "change") listeners.delete(listener);
      },
    };
  };
  const set = (value: boolean) => {
    matches = value;
    for (const listener of [...listeners]) listener({ matches: value });
  };
  return { matchMedia, set, queries, listenerCount: () => listeners.size };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("reduced motion", () => {
  it("follows the system setting and notifies subscribers when it changes", () => {
    const media = fakeMatchMedia(false);
    vi.stubGlobal("window", { matchMedia: media.matchMedia });
    const received: boolean[] = [];

    expect(prefersReducedMotion()).toBe(false);
    const unsubscribe = subscribeReducedMotion((reduced) => received.push(reduced));

    media.set(true);
    expect(prefersReducedMotion()).toBe(true);
    expect(received).toEqual([true]);

    media.set(false);
    expect(prefersReducedMotion()).toBe(false);
    expect(received).toEqual([true, false]);

    expect(media.queries.every((query) => query === "(prefers-reduced-motion: reduce)")).toBe(true);
    unsubscribe();
    expect(media.listenerCount()).toBe(0);
  });

  it("stops notifying after unsubscribing", () => {
    const media = fakeMatchMedia(false);
    vi.stubGlobal("window", { matchMedia: media.matchMedia });
    const received: boolean[] = [];
    const unsubscribe = subscribeReducedMotion((reduced) => received.push(reduced));

    unsubscribe();
    media.set(true);

    expect(received).toEqual([]);
    expect(prefersReducedMotion()).toBe(true);
  });

  it("is false and never throws without matchMedia", () => {
    expect(typeof window).toBe("undefined");
    expect(prefersReducedMotion()).toBe(false);
    const unsubscribe = subscribeReducedMotion(() => {
      throw new Error("must not be called");
    });
    expect(() => unsubscribe()).not.toThrow();

    vi.stubGlobal("window", {});
    expect(prefersReducedMotion()).toBe(false);
    expect(() => subscribeReducedMotion(() => {})()).not.toThrow();
  });
});
