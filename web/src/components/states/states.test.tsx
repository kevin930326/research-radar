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

// EXPERIENCE.md State Patterns「全站共用」：整頁載入失敗、找不到這一頁時，訊息就是頁面標題（h1），字與樣子不變
// （分頁名稱在瀏覽器裡才看得到，交給 e2e/paper.spec.ts）。Codex 審查 3.8 中 8。
describe("whole-page states are the page title", () => {
  it("load failed: the message is the h1, with the same classes as the message line", () => {
    const html = renderToStaticMarkup(<LoadFailed />);
    expect(html).toContain(
      `<h1 class="flex min-h-tap-target items-center text-body text-text-primary">${text["common:states.loadFailed.message"]}</h1>`,
    );
    expect(html).not.toMatch(/<p[\s>]/);
  });

  it("not found: the message is the h1", async () => {
    // NotFound 的「回首頁」是站內連結，要在路由裡渲染（同上面 not found 的測試）。
    const router = createAppRouter({ history: createMemoryHistory({ initialEntries: ["/no/such/page"] }) });
    await router.load();
    const html = renderToString(<RouterProvider router={router} />);
    expect(html).toContain(
      `<h1 class="flex min-h-tap-target items-center text-body text-text-primary">${text["common:states.notFound.message"]}</h1>`,
    );
  });

  it("offline is not a page title: it stays a paragraph", () => {
    const html = renderToStaticMarkup(<OfflineNotice />);
    expect(html).not.toContain("<h1");
    expect(html).toContain(`<p class="flex min-h-tap-target items-center text-body text-text-primary">`);
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
