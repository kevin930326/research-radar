// 示範站的論文頁沒有修正分類（Story 3.11；EXPERIENCE.md：示範站只顯示 AI 標章，含「已核對」）。
// 為什麼另外寫一個檔：設計稿比對的 expect-missing/paper.txt 拿掉了「修正分類」（成員版現在有了），demo-paper.test.tsx 拿那份清單
// 當「示範站不得出現」的字，所以那條防線少了「修正分類」。那個既有測試不動（審查 3.11 T10），這裡另外明確斷言。
// 放在 datasource/ 底下：要直接呼叫 static 的讀檔函式（同 demo-paper.test.tsx，ADR-0010 第 1 點）。
import "../i18n";

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { RouterContextProvider, createMemoryHistory, createRootRoute, createRoute, createRouter } from "@tanstack/react-router";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import expected from "../i18n/expected/notice.zh-TW.json";
import { DemoPaperPage } from "../pages/DemoPaperPage";
import { loadDemoManifest, loadDemoPaper, loadDemoPaperIndex } from "./static/preview";

const FIXTURE = fileURLToPath(new URL("../../e2e/fixtures/demo-preview/", import.meta.url));

function readFixture(path: string): unknown {
  return JSON.parse(readFileSync(`${FIXTURE}${path}`, "utf8")) as unknown;
}

const fixtureFetch = (async (input: RequestInfo | URL) => {
  const path = String(input).slice("/preview/".length);
  return new Response(JSON.stringify(readFixture(path)), { status: 200, headers: { "content-type": "application/json" } });
}) as typeof fetch;

const manifest = readFixture("manifest.json") as { export_id: string };
const SHARD = (readFixture(`${manifest.export_id}/papers/000.json`) as { papers: { biblio: { biblio: { id: string } } }[] }).papers;

const rootRoute = createRootRoute();
const router = createRouter({
  routeTree: rootRoute.addChildren([createRoute({ getParentRoute: () => rootRoute, path: "/papers" })]),
  history: createMemoryHistory({ initialEntries: ["/papers/rr-1"] }),
});

const FIX_OPEN = expected.strings["paper:paper.fix.open"];

describe("demo paper page: no classification fix", () => {
  it("the text to look for is the member page's entry", () => {
    // 和 i18n 的成員版字串是同一個（expected/notice.zh-TW.json 由 i18n 測試和 EXPERIENCE 對過）。
    expect(FIX_OPEN.length).toBeGreaterThan(0);
    expect(SHARD.length).toBeGreaterThan(5);
  });

  it.each(SHARD.map((paper) => paper.biblio.biblio.id))("%s: no fix entry, count button, history or form", async (id) => {
    const loadedManifest = await loadDemoManifest(fixtureFetch);
    const index = await loadDemoPaperIndex(loadedManifest, fixtureFetch);
    const paper = await loadDemoPaper(loadedManifest, index, id, fixtureFetch);
    expect(paper).not.toBeNull();
    const html = renderToStaticMarkup(
      <RouterContextProvider router={router}>
        <DemoPaperPage paper={paper!} names={index.names} />
      </RouterContextProvider>,
    );
    expect(html).not.toContain(FIX_OPEN);
    for (const marker of ["data-classification-fix", "data-fix-open", "data-fix-count", "data-fix-history", "data-fix-form"]) {
      expect(html, marker).not.toContain(marker);
    }
  });
});
