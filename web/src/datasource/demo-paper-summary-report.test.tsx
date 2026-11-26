// 示範站的論文頁沒有回報錯誤（Story 3.12；EXPERIENCE.md 畫面 8「有完整內容」：沒有回報錯誤，也不顯示「有成員回報此欄可能有誤」）。
// 為什麼另外寫一個檔：設計稿比對的 expect-missing/paper.txt 拿掉了「回報錯誤」（成員版現在有了），demo-paper.test.tsx 拿那份清單
// 當「示範站不得出現」的字，所以那條防線少了「回報錯誤」。那個既有測試不動，這裡另外明確斷言（同 3.11 的
// demo-paper-classification-fix.test.tsx）。示範站的資料本來就沒有回報（static/preview.ts 填空的），標記也一併斷言。
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

const REPORT_OPEN = expected.strings["paper:paper.report.open"];
const REPORTED = expected.strings["paper:paper.summary.reported"];

describe("demo paper page: no summary report", () => {
  it("the texts to look for are the member page's entry and mark", () => {
    // 和 i18n 的成員版字串是同一個（expected/notice.zh-TW.json 由 i18n 測試和 EXPERIENCE 對過）。
    expect(REPORT_OPEN.length).toBeGreaterThan(0);
    expect(REPORTED.length).toBeGreaterThan(0);
    expect(SHARD.length).toBeGreaterThan(5);
  });

  it.each(SHARD.map((paper) => paper.biblio.biblio.id))("%s: no report entry, no reported mark, no report form", async (id) => {
    const loadedManifest = await loadDemoManifest(fixtureFetch);
    const index = await loadDemoPaperIndex(loadedManifest, fixtureFetch);
    const paper = await loadDemoPaper(loadedManifest, index, id, fixtureFetch);
    expect(paper).not.toBeNull();
    const html = renderToStaticMarkup(
      <RouterContextProvider router={router}>
        <DemoPaperPage paper={paper!} names={index.names} />
      </RouterContextProvider>,
    );
    expect(html).not.toContain(REPORT_OPEN);
    expect(html).not.toContain(REPORTED);
    for (const marker of ["data-summary-report", "data-report-open", "data-report-form", 'data-note="reported"']) {
      expect(html, marker).not.toContain(marker);
    }
  });
});
