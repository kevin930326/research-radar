// 全站共用狀態（EXPERIENCE.md State Patterns「全站共用」）：元件輸出的文字和 i18n／EXPERIENCE.md 相同，
// 路由設定用的是這幾個共用元件。用 react-dom/server 靜態渲染，不另外裝 jsdom；
// 互動（離線事件、找不到頁面、點連結）交給 Playwright。
import "../../i18n";

import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { renderToStaticMarkup, renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import expected from "../../i18n/expected/notice.zh-TW.json";
import { PENDING_MS, createAppRouter } from "../../router";
import { LoadFailed } from "./LoadFailed";
import { NotFound } from "./NotFound";
import { OfflineNotice } from "./Offline";
import { PageLoading } from "./PageLoading";

const text = expected.strings;

/** 去掉標籤，只留畫面上看得到的字。 */
function visibleText(html: string): string {
  return html.replace(/<[^>]+>/g, "");
}

describe("site-wide states", () => {
  it("loading: a skeleton with no text, marked busy", () => {
    const html = renderToStaticMarkup(<PageLoading />);
    expect(html).toContain('aria-busy="true"');
    expect(visibleText(html)).toBe("");
  });

  it("load failed: message and a reload button", () => {
    const html = renderToStaticMarkup(<LoadFailed />);
    expect(html).toContain('role="alert"');
    expect(visibleText(html)).toBe(
      `${text["common:states.loadFailed.message"]}${text["common:states.loadFailed.action"]}`,
    );
    expect(html).toMatch(/<button type="button"[^>]*>[^<]+<\/button>/);
  });

  it("offline: message only", () => {
    const html = renderToStaticMarkup(<OfflineNotice />);
    expect(html).toContain('role="status"');
    expect(visibleText(html)).toBe(text["common:states.offline.message"]);
  });

  it("not found (member build): message and a link home", async () => {
    const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/no/such/page"] }) });
    await router.load();
    const html = renderToString(<RouterProvider router={router} />);
    expect(visibleText(html)).toContain(
      `${text["common:states.notFound.message"]}${text["common:states.notFound.home"]}`,
    );
    expect(html).toMatch(/<a [^>]*href="\/"[^>]*>/);
  });
});

describe("router defaults", () => {
  it("uses the shared states and waits 300 ms before showing the skeleton", () => {
    const router = createAppRouter({ history: createMemoryHistory() });
    expect(PENDING_MS).toBe(300);
    expect(router.options.defaultPendingMs).toBe(300);
    expect(router.options.defaultPendingComponent).toBe(PageLoading);
    expect(router.options.defaultErrorComponent).toBe(LoadFailed);
    expect(router.options.defaultNotFoundComponent).toBe(NotFound);
  });
});
