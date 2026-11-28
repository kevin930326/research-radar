// 示範站的論文頁（Story 3.9）：用 repo 裡的範例匯出（e2e/fixtures/demo-preview，假論文）。
// 放在 datasource/ 底下：要直接呼叫 static 的讀檔函式（頁面與元件的測試不能 import 它，ADR-0010 第 1 點）。
// 1. 讀分片：每一篇都找得到、種類和範例的旗標算出來的一樣（Kevin 2026-11-25：has_abstract=false 且 demo_eligible=true
//    是「沒有摘要」、demo_eligible=false 是「只有書目」）；索引裡沒有的回 null；檔案壞了丟出例外。
// 2. 畫面：三種示範站版本的字和 EXPERIENCE.md 畫面 8 相同；沒有成員功能（State Patterns「全站共用」的
//    「示範站的後端功能」）；示範站的「沒有產生成功」只寫這一句；沒有設計稿的三種狀態照 Kevin 2026-11-25 的決定。
import "../i18n";

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import {
  RouterContextProvider,
  createMemoryHistory,
  createRootRoute,
  createRoute,
  createRouter,
} from "@tanstack/react-router";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import expected from "../i18n/expected/notice.zh-TW.json";
import { DemoPaperPage } from "../pages/DemoPaperPage";
import { PreviewFileError, SHARD_SIZE, loadDemoManifest, loadDemoPaper, loadDemoPaperIndex } from "./static/preview";
import type { DemoPaper, DemoPaperKind } from "./types";

const FIXTURE = fileURLToPath(new URL("../../e2e/fixtures/demo-preview/", import.meta.url));

type Json = Record<string, unknown>;

function readFixture(path: string): Json {
  return JSON.parse(readFileSync(`${FIXTURE}${path}`, "utf8")) as Json;
}

const TEXT: Record<string, string> = {
  ...expected.strings,
  ...Object.fromEntries(Object.entries(expected.otherSources).map(([key, entry]) => [key, entry.text])),
};

interface RawPaper {
  biblio: {
    biblio: { id: string; title: string; doi_url: string | null };
    classification: { state: string } | null;
    has_abstract: boolean;
    has_full_content: boolean;
    demo_eligible: boolean;
  };
  full: { zh_summary: { state: string }; research_model: { state: string } } | null;
}

const SHARD = readFixture("2-1/papers/000.json").papers as RawPaper[];

/** 範例的旗標 → 該是哪一種（照 Kevin 2026-11-25 的規則，寫在測試裡、不呼叫程式的判斷）。 */
function expectedKind(paper: RawPaper): DemoPaperKind {
  const flags = paper.biblio;
  if (flags.has_full_content) return "full";
  if (!flags.demo_eligible) return "biblioOnly";
  if (!flags.has_abstract) return "noAbstract";
  return "withheld";
}

/** 標題開頭是「Fake Paper N:」的那一篇。 */
function byNumber(number: number): RawPaper {
  const found = SHARD.find((paper) => paper.biblio.biblio.title.startsWith(`Fake Paper ${number}:`));
  if (found === undefined) throw new Error(`no Fake Paper ${number}`);
  return found;
}

/** 假的 fetch：`/preview/<path>` 回範例檔；`change` 可以在回傳前改掉某個檔的內容。 */
function fixtureFetch(change: (path: string, body: Json) => Json | number = (_path, body) => body): typeof fetch {
  return (async (input: RequestInfo | URL) => {
    const url = String(input);
    expect(url.startsWith("/preview/")).toBe(true);
    const path = url.slice("/preview/".length);
    const result = change(path, readFixture(path));
    if (typeof result === "number") return new Response(null, { status: result });
    return new Response(JSON.stringify(result), { status: 200, headers: { "content-type": "application/json" } });
  }) as typeof fetch;
}

async function loadPaper(id: string, fetchImpl: typeof fetch = fixtureFetch()): Promise<DemoPaper | null> {
  const manifest = await loadDemoManifest(fixtureFetch());
  const index = await loadDemoPaperIndex(manifest, fixtureFetch());
  return loadDemoPaper(manifest, index, id, fetchImpl);
}

/** 只改分片裡某一篇（照 id）的內容。 */
function changePaper(id: string, change: (paper: Json) => void) {
  return fixtureFetch((path, body) => {
    if (!path.endsWith("/papers/000.json")) return body;
    const papers = body.papers as Json[];
    const target = papers.find((paper) => ((paper.biblio as Json).biblio as Json).id === id);
    if (target === undefined) throw new Error(`no paper ${id}`);
    change(target);
    return body;
  });
}

describe("demo paper loader", () => {
  it("finds every paper of the example shard and tells its kind from demo_eligible and has_abstract", async () => {
    expect(SHARD.length).toBeGreaterThan(0);
    expect(SHARD.length).toBeLessThanOrEqual(SHARD_SIZE);
    const kinds = new Set<DemoPaperKind>();
    for (const raw of SHARD) {
      const paper = await loadPaper(raw.biblio.biblio.id);
      expect(paper, raw.biblio.biblio.title).not.toBeNull();
      expect(paper!.kind, raw.biblio.biblio.title).toBe(expectedKind(raw));
      expect(paper!.page.paper.id).toBe(raw.biblio.biblio.id);
      expect(paper!.page.paper.title).toBe(raw.biblio.biblio.title);
      expect(paper!.page.abstract === null).toBe(raw.full === null);
      kinds.add(paper!.kind);
    }
    // 範例要四種都有（第 1 部分的範例：有完整內容、授權允許但沒有摘要、只有書目、摘要來源不允許公開）。
    expect([...kinds].sort()).toEqual(["biblioOnly", "full", "noAbstract", "withheld"]);
  });

  it("an id that is not in this export is not found", async () => {
    expect(await loadPaper("00000000-0000-4000-8000-000000000000")).toBeNull();
  });

  it("uses the manifest's snapshot, fills member-only fields with nothing and hides blocks the demo does not have", async () => {
    const full = (await loadPaper(byNumber(1).biblio.biblio.id))!;
    expect(full.page.snapshot).toEqual({ number: 2, as_of: "2026-11-16T03:00:00Z" });
    expect(full.page.in_my_library).toBe(false);
    expect(full.page.classification.value?.fix_member_count).toBe(0);
    expect(full.page.classification.value?.topic?.members_disagree).toBe(false);
    expect(full.page.zh_summary.value?.reported_fields).toEqual([]);
    const noAbstract = (await loadPaper(byNumber(2).biblio.biblio.id))!;
    expect(noAbstract.page.zh_summary.state).toBe("no_input");
    expect(noAbstract.page.research_model.state).toBe("no_input");
    const withheld = (await loadPaper(byNumber(9).biblio.biblio.id))!;
    expect(byNumber(9).biblio.classification).toBeNull();
    expect(withheld.page.classification.state).toBe("not_applicable");
  });

  it("a paper with full content but no full layer (or the other way round) is a broken file", async () => {
    const fullId = byNumber(1).biblio.biblio.id;
    await expect(loadPaper(fullId, changePaper(fullId, (paper) => (paper.full = null)))).rejects.toThrow(PreviewFileError);
    const biblioId = byNumber(3).biblio.biblio.id;
    await expect(
      loadPaper(
        biblioId,
        changePaper(biblioId, (paper) => {
          paper.full = structuredClone(byNumber(1).full) as unknown as Json;
          (paper.full as Json).paper_id = biblioId;
          (paper.biblio as Json).has_full_content = true;
        }),
      ),
    ).rejects.toThrow(PreviewFileError);
  });

  it("full content of a paper that is not demo eligible is a broken file", async () => {
    const fullId = byNumber(1).biblio.biblio.id;
    await expect(
      loadPaper(fullId, changePaper(fullId, (paper) => ((paper.biblio as Json).demo_eligible = false))),
    ).rejects.toThrow(PreviewFileError);
  });

  it("rejects a shard from another export, a wrong position, a wrong type and a shard the manifest does not list", async () => {
    const id = byNumber(1).biblio.biblio.id;
    await expect(loadPaper(id, fixtureFetch((path, body) => (path.includes("/papers/") ? { ...body, export_id: "2-2" } : body)))).rejects.toThrow(
      PreviewFileError,
    );
    await expect(loadPaper(id, fixtureFetch((path, body) => (path.includes("/papers/") ? { ...body, shard: 1 } : body)))).rejects.toThrow(
      PreviewFileError,
    );
    await expect(
      loadPaper(id, fixtureFetch((path, body) => (path.includes("/papers/") ? { ...body, papers: [...(body.papers as Json[])].reverse() } : body))),
    ).rejects.toThrow(PreviewFileError);
    await expect(loadPaper(id, changePaper(id, (paper) => ((paper.biblio as Json).has_abstract = "yes")))).rejects.toThrow(PreviewFileError);
    await expect(
      loadPaper(id, changePaper(id, (paper) => (((paper.full as Json).zh_summary as Json).state = "done"))),
    ).rejects.toThrow(PreviewFileError);
    await expect(loadPaper(id, fixtureFetch((path) => (path.includes("/papers/") ? 404 : readFixture(path))))).rejects.toThrow();
    const manifest = await loadDemoManifest(fixtureFetch());
    const index = await loadDemoPaperIndex(manifest, fixtureFetch());
    const unlisted = { ...manifest, files: manifest.files.filter((file) => !file.includes("/papers/")) };
    await expect(loadDemoPaper(unlisted, index, id, fixtureFetch())).rejects.toThrow(PreviewFileError);
  });
});

// 分類標籤、「看有完整內容的論文」連到論文清單（站內連結）：只放論文清單一條路由的記憶體路由。
const rootRoute = createRootRoute();
const papersRoute = createRoute({ getParentRoute: () => rootRoute, path: "/papers" });
const router = createRouter({
  routeTree: rootRoute.addChildren([papersRoute]),
  history: createMemoryHistory({ initialEntries: ["/papers/rr-1"] }),
});

async function render(number: number): Promise<{ html: string; paper: DemoPaper; raw: RawPaper }> {
  const raw = byNumber(number);
  const paper = (await loadPaper(raw.biblio.biblio.id))!;
  const manifest = await loadDemoManifest(fixtureFetch());
  const index = await loadDemoPaperIndex(manifest, fixtureFetch());
  const html = renderToStaticMarkup(
    <RouterContextProvider router={router}>
      <DemoPaperPage paper={paper} names={index.names} />
    </RouterContextProvider>,
  );
  return { html, paper, raw };
}

const t = (key: string): string => {
  const value = TEXT[key];
  if (value === undefined) throw new Error(`no text ${key}`);
  return value;
};

/** 成員才有的功能（State Patterns「全站共用」的「示範站的後端功能」：不存在，不顯示入口）。 */
const MEMBER_ONLY = [
  "paper:paper.library.heading",
  "paper:paper.library.add",
  "paper:paper.library.added",
  "paper:paper.library.remove",
  "paper:paper.summary.reported",
];
/**
 * 「沒有產生成功」那一行：成員版的 paper.state.failed 和示範站的 demo.paper.failed 現在同字（55 條第 31 條），
 * 用字分不出來，改看那一行標的 key（data-text-key，見 parts.tsx 的 useFailedText）。
 */
const MEMBER_FAILED_KEY = 'data-text-key="paper:paper.state.failed"';
const DEMO_FAILED_KEY = 'data-text-key="demo:demo.paper.failed"';
/** 放進正規表示式用：key 裡的「.」要跳脫，不然會對到任何字。 */
const DEMO_FAILED_KEY_PATTERN = DEMO_FAILED_KEY.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
/**
 * 成員版論文頁設計稿上有、之後的 Story 才做的字（修正分類、回報錯誤、我的 PDF、找相似論文、引用、頁尾……；
 * design-compare 的 --expect-missing 清單 paper.txt，同一份）。示範站現在一個都不會有：成員才有的那幾個永遠不會有，
 * 其他的（引用、頁尾、導覽列的連結）各自的 Story 才放。清單是一行一段、去掉空白（和比對工具相同的比法）。
 */
const LATER_OR_MEMBER_ONLY = readFileSync(fileURLToPath(new URL("../../../docs/ux/reference/expect-missing/paper.txt", import.meta.url)), "utf-8")
  .split("\n")
  .map((line) => line.trim())
  .filter((line) => line !== "" && !line.startsWith("#"));

/** 清單的一行對到畫面上的一段字（去掉空白後整段相同；行尾「…」是只比開頭）。 */
function matchesLine(paragraph: string, line: string): boolean {
  return line.endsWith("…") ? paragraph.startsWith(line.slice(0, -1)) : paragraph === line;
}

function expectNoMemberFeatures(html: string) {
  expect(LATER_OR_MEMBER_ONLY.length).toBeGreaterThan(5);
  for (const key of MEMBER_ONLY) expect(html, key).not.toContain(t(key));
  expect(html).not.toContain(MEMBER_FAILED_KEY);
  // 兩句之後又不同字時，照原本用字比對（直接寫 t(...)、沒標 key 的也抓得到）。
  if (t("paper:paper.state.failed") !== t("demo:demo.paper.failed")) {
    expect(html).not.toContain(t("paper:paper.state.failed"));
  }
  const paragraphs = [...html.matchAll(/>([^<]+)</g)].map((match) => match[1]!.replace(/\s+/g, "")).filter((text) => text !== "");
  expect(paragraphs.length).toBeGreaterThan(5);
  for (const line of LATER_OR_MEMBER_ONLY) {
    expect(paragraphs.filter((paragraph) => matchesLine(paragraph, line)), line).toEqual([]);
  }
  // 「N 位成員修正過」：佔位符後面那一段。
  const fixedTail = t("paper:paper.classification.fixed").split("}}")[1]!.trim();
  expect(html).not.toContain(fixedTail);
  expect(html).not.toContain("data-library");
}

describe("demo paper page", () => {
  it("full content: bibliography, classification, summary, research model and abstract, with AI badges", async () => {
    const { html } = await render(1);
    expect(html).toContain('data-demo-paper="full"');
    expect(html).toContain(t("paper:paper.summary.heading"));
    expect(html).toContain(t("paper:paper.model.heading"));
    expect(html).toContain(t("paper:paper.abstract.heading"));
    expect(html).toContain("Made up abstract 1");
    expect(html).toContain(t("paper:paper.biblio.hasAbstract"));
    expect(html).toContain(t("ai:ai.badge.full"));
    expect(html).toContain(t("paper:paper.fullText.open"));
    expectNoMemberFeatures(html);
  });

  it("classification tags go to the demo paper list with that one condition", async () => {
    const { html } = await render(1);
    expect(html).toContain('href="/papers?topic=erp"');
    expect(html).toContain('href="/papers?method=survey_sem"');
    expect(html).not.toContain('href="/search');
  });

  it("eligible without an abstract: the no-abstract block and the research model line", async () => {
    const { html, raw } = await render(2);
    expect(raw.biblio).toMatchObject({ demo_eligible: true, has_abstract: false });
    expect(html).toContain('data-demo-paper="noAbstract"');
    expect(html).toContain(t("paper:paper.noAbstract.message"));
    expect(html).toContain(t("paper:paper.noAbstract.publisher"));
    // 研究模型那一塊的那一行（不看整頁的字：「沒有摘要」和書目上的標記同字）。
    const model = html.slice(html.indexOf('data-block="research_model"'));
    expect(model).toMatch(/^data-block="research_model" data-block-state="no_input"/);
    expect(model.slice(0, model.indexOf("</section>"))).toMatch(
      new RegExp(`<div data-block-state="no_input"[^>]*>${t("paper:paper.model.noAbstract")}</div>`),
    );
    expect(html).toContain(t("paper:paper.biblio.noAbstract"));
    // 只根據標題的分類（範例這一篇是標題分類）。
    expect(html).toContain(t("paper:paper.classification.titleOnly"));
    // 分類照常（標籤加 AI 標章）。
    expect(html).toContain('data-block="classification" data-block-state="ready"');
    expect(html).toContain('href="/papers?topic=erp"');
    expect(html).toContain(t("ai:ai.badge.full"));
    expect(html).not.toContain(t("demo:demo.paper.biblioOnly"));
    expectNoMemberFeatures(html);
  });

  it("bibliography only: the licence notice and its two links, no content blocks and no abstract mark", async () => {
    const { html, raw } = await render(3);
    expect(raw.biblio.demo_eligible).toBe(false);
    expect(html).toContain('data-demo-paper="biblioOnly"');
    expect(html).toContain(t("demo:demo.paper.biblioOnly"));
    expect(html).toContain(`href="${raw.biblio.biblio.doi_url}"`);
    expect(html).toContain(t("paper:paper.noAbstract.publisher"));
    expect(html).toMatch(new RegExp(`href="/papers\\?full=true"[^>]*>${t("demo:demo.paper.fullPapers")}<`));
    for (const key of [
      "paper:paper.summary.heading",
      "paper:paper.model.heading",
      "paper:paper.abstract.heading",
      "paper:paper.noAbstract.heading",
      "paper:paper.biblio.hasAbstract",
      "paper:paper.biblio.noAbstract",
    ]) {
      expect(html, key).not.toContain(`>${t(key)}<`);
    }
    // 分類照常（標籤加 AI 標章）。
    expect(html).toContain('data-block="classification"');
    expect(html).toContain(t("ai:ai.badge.full"));
    expectNoMemberFeatures(html);
  });

  it("bibliography only also covers the IEEE papers and papers without an abstract that are not eligible", async () => {
    for (const number of [7, 8, 14]) {
      const { html, raw } = await render(number);
      expect(raw.biblio.demo_eligible, `Fake Paper ${number}`).toBe(false);
      expect(html, `Fake Paper ${number}`).toContain(t("demo:demo.paper.biblioOnly"));
      expect(html, `Fake Paper ${number}`).not.toContain(t("paper:paper.noAbstract.message"));
    }
  });

  it("not generated successfully: only the short demo sentence, without the member wording, no badge on that block", async () => {
    const { html, raw } = await render(12);
    expect(raw.full?.zh_summary.state).toBe("failed");
    expect(html).toContain(`>${t("demo:demo.paper.failed")}<`);
    const summary = html.slice(html.indexOf('data-block="zh_summary"'));
    expect(summary.slice(0, summary.indexOf("</section>"))).toMatch(new RegExp(`${DEMO_FAILED_KEY_PATTERN}[^>]*>${t("demo:demo.paper.failed")}<`));
    expect(html).not.toContain(MEMBER_FAILED_KEY);
    expect(html).toContain('data-block="zh_summary" data-block-state="failed"');
    expectNoMemberFeatures(html);
  });

  it("research model not generated successfully: that line is tagged with the demo key", async () => {
    // 範例沒有研究模型 failed 的那一篇：拿第 1 篇，只把研究模型改成 failed。
    const id = byNumber(1).biblio.biblio.id;
    const paper = (await loadPaper(id, changePaper(id, (raw) => {
      (raw.full as Json).research_model = { state: "failed", value: null, provenance: null };
    })))!;
    const index = await loadDemoPaperIndex(await loadDemoManifest(fixtureFetch()), fixtureFetch());
    const html = renderToStaticMarkup(
      <RouterContextProvider router={router}>
        <DemoPaperPage paper={paper} names={index.names} />
      </RouterContextProvider>,
    );
    const model = html.slice(html.indexOf('data-block="research_model"'));
    expect(model).toMatch(/^data-block="research_model" data-block-state="failed"/);
    expect(model.slice(0, model.indexOf("</section>"))).toMatch(
      new RegExp(`${DEMO_FAILED_KEY_PATTERN}[^>]*>${t("demo:demo.paper.failed")}<`),
    );
    expect(html).not.toContain(MEMBER_FAILED_KEY);
  });

  it("no design yet (Kevin 2026-11-25): pending uses the member sentence, failed classification is the label plus the line, null classification is hidden", async () => {
    const pending = await render(5);
    expect(pending.raw.biblio.classification?.state).toBe("pending_batch");
    expect(pending.html).toContain('data-block="classification" data-block-state="pending_batch"');
    expect(pending.html).toContain(t("paper:paper.state.pending"));
    // 中文重點、研究模型也是 pending_batch：同一句。
    expect(pending.raw.full).toMatchObject({ zh_summary: { state: "pending_batch" }, research_model: { state: "pending_batch" } });
    for (const block of ["zh_summary", "research_model"]) {
      const section = pending.html.slice(pending.html.indexOf(`data-block="${block}"`));
      expect(section, block).toMatch(new RegExp(`^data-block="${block}" data-block-state="pending_batch"`));
      expect(section.slice(0, section.indexOf("</section>")), block).toContain(t("paper:paper.state.pending"));
    }

    const failed = await render(4);
    expect(failed.raw.biblio.classification?.state).toBe("failed");
    const failedLine = failed.html.slice(failed.html.indexOf('data-block="classification"'));
    expect(failedLine).toMatch(/^data-block="classification" data-block-state="failed"/);
    expect(failedLine).toContain(`>${t("paper:paper.classification.label")}<`);
    expect(failedLine).toContain(`>${t("demo:demo.paper.failed")}<`);
    // 分類那一行（「分類」＋這一句）標的是示範站的 key。
    expect(failedLine).toMatch(new RegExp(`^data-block="classification" data-block-state="failed" ${DEMO_FAILED_KEY_PATTERN}`));
    expect(failed.html).not.toContain(MEMBER_FAILED_KEY);

    const hidden = await render(9);
    expect(hidden.raw.biblio.classification).toBeNull();
    expect(hidden.html).toContain('data-demo-paper="withheld"');
    expect(hidden.html).not.toContain('data-block="classification"');
    expect(hidden.html).not.toContain(t("paper:paper.classification.label"));
    // 書目與分類之後什麼都不放（沒有設計稿）：沒有內容區塊，也沒有「只有書目」的說明。
    expect(hidden.html).not.toContain(t("demo:demo.paper.biblioOnly"));
    expect(hidden.html).not.toContain(t("paper:paper.summary.heading"));
    expect(hidden.html).not.toContain(t("paper:paper.noAbstract.message"));
  });
});

describe("citation export on the demo paper page (Story 3.10)", () => {
  // 設計稿 paper-demo.html：四種版本都有「引用」三顆（和成員版同一份實作，AR-24）。
  it.each([1, 2, 3, 12])("Fake Paper %i has the three citation buttons in the rail and the mobile block", async (number) => {
    const { html } = await render(number);
    for (const key of ["paper:paper.cite.heading", "paper:paper.cite.copyApa", "paper:paper.cite.downloadRis", "paper:paper.cite.downloadBibtex"]) {
      expect(html.split(`>${t(key)}<`).length - 1, key).toBe(2);
    }
    expect(html).toContain('data-cite="rail"');
    expect(html).toContain('data-cite="mobile"');
  });
});
