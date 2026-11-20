// 論文頁（Story 3.8）：EXPERIENCE.md State Patterns「8. 論文頁」每一列（區塊層級的）、三個區塊各自獨立、
// 第一個未核對的 AI 標章寫全文。用 react-dom/server 靜態渲染（ADR-0036 第 7 點）；資料是 e2e 共用的假論文。
// 整頁層級的列（載入中、錯誤、找不到或沒有權限）在路由，交給 e2e/paper.spec.ts。
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
import { afterEach, describe, expect, it, vi } from "vitest";

import fixtures from "../../e2e/fixtures/papers.json";
import type { DisplayNames, PaperPageResponse } from "../generated/model";
import expected from "../i18n/expected/notice.zh-TW.json";
import { PaperPage, firstUnverifiedBadge } from "./PaperPage";
import { citationsPerYear, formatVolume, isPaperId, splitTemplate } from "./paper-format";

const TEXT: Record<string, string> = {
  ...expected.strings,
  ...Object.fromEntries(Object.entries(expected.otherSources).map(([key, entry]) => [key, entry.text])),
};
const NAMES = fixtures.displayNames as DisplayNames;
const PAPERS = fixtures.papers as unknown as Record<string, { label: string; page: PaperPageResponse }>;

function fixture(label: string): PaperPageResponse {
  const entry = Object.values(PAPERS).find((candidate) => candidate.label === label);
  if (entry === undefined) throw new Error(`no fixture ${label}`);
  return structuredClone(entry.page);
}

// 主題標籤連到找論文（Story 3.14，站內連結），要有路由才能算出網址：只放找論文一條路由的記憶體路由。
const rootRoute = createRootRoute();
const searchRoute = createRoute({ getParentRoute: () => rootRoute, path: "/search" });
const router = createRouter({
  routeTree: rootRoute.addChildren([searchRoute]),
  history: createMemoryHistory({ initialEntries: ["/papers/rr-1"] }),
});

function render(page: PaperPageResponse): string {
  return renderToStaticMarkup(
    <RouterContextProvider router={router}>
      <PaperPage page={page} names={NAMES} />
    </RouterContextProvider>,
  );
}

/** 某個 data-block 區塊的 HTML（到下一個區塊之前）。 */
function block(html: string, name: string): string {
  const start = html.indexOf(`data-block="${name}"`);
  if (start < 0) return "";
  const next = html.indexOf("data-block=", start + 1);
  return next < 0 ? html.slice(start) : html.slice(start, next);
}

// 設計稿上有、屬於之後 Story 的字（design-compare 的 --expect-missing 清單，同一份）。
function expectMissing(name: string): string {
  return readFileSync(fileURLToPath(new URL(`../../../docs/ux/reference/expect-missing/${name}`, import.meta.url)), "utf-8");
}

const LATER_STORIES = expectMissing("paper.txt")
  .split("\n")
  .map((line) => line.trim())
  .filter((line) => line !== "" && !line.startsWith("#") && !line.endsWith("…"));

function count(html: string, text: string): number {
  return html.split(text).length - 1;
}

/** 「normal」這篇的站外連結數（Story 3.14 之前整頁的連結都是站外，數量和當時一樣）。 */
const EXTERNAL_LINKS = 4;

const FAILED = TEXT["paper:paper.state.failed"]!;
const PENDING = TEXT["paper:paper.state.pending"]!;
const BUDGET = TEXT["paper:paper.state.budget"]!;

describe("paper page: three AI blocks with content", () => {
  const html = render(fixture("normal"));

  it("shows biblio, then summary, research model and abstract in the design's order", () => {
    const order = ["biblio", "zh_summary", "research_model", "abstract"].map((name) => html.indexOf(`data-block="${name}"`));
    expect(order.every((position) => position >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(html).toContain(TEXT["paper:paper.summary.screeningOnly"]);
    expect(html).toContain(TEXT["paper:paper.model.abstractOnly"]);
  });

  it("has the data date bar with the snapshot number", () => {
    expect(html).toContain("2026-11-12");
    expect(html).toContain("#12");
  });

  it("opens the full text in a new tab and shows the open access link when there is one", () => {
    expect(html).toContain('data-full-text="open"');
    expect(html).toContain('data-full-text="open-mobile"');
    expect(count(html, "https://example.com/open-access/rr-1.pdf")).toBe(2);
    expect(html).toContain(TEXT["paper:paper.fullText.openAccess"]);
    const noOa = render(fixture("normal-no-oa"));
    expect(noOa).not.toContain(TEXT["paper:paper.fullText.openAccess"]);
    expect(noOa).toContain('data-full-text="open"');
  });

  it("external links open in a new tab without the opener", () => {
    // 站外連結（http 開頭）：書目的 DOI、開啟全文（桌機、手機各一）、開放取用版本、期刊頁……數量固定，不能少。
    const external = [...html.matchAll(/<a [^>]*>/g)].filter((match) => /href="https?:/.test(match[0]));
    expect(external.length).toBe(EXTERNAL_LINKS);
    for (const match of external) {
      expect(match[0]).toContain('target="_blank"');
      expect(match[0]).toContain('rel="noopener noreferrer"');
      expect(match[0]).toContain('aria-describedby="new-tab-note"');
    }
    expect(html).toContain(`<span id="new-tab-note" hidden="">${TEXT["common:links.external.newTab"]}</span>`);
  });

  it("topic tags link to search with the topic filter in the same tab", () => {
    const classification = block(html, "classification");
    const topics = [...classification.matchAll(/<a [^>]*data-topic-link[^>]*>/g)].map((match) => match[0]);
    // 「normal」有兩個主題（platforms、trust），一個一個連結。
    expect(topics.length).toBe(2);
    for (const tag of topics) {
      expect(tag).toMatch(/href="\/search\?topic=[a-z0-9_]+"/);
      expect(tag).not.toContain("target=");
      expect(tag).not.toContain("aria-describedby");
    }
    expect(topics[0]).toContain('href="/search?topic=platforms"');
    expect(topics[1]).toContain('href="/search?topic=trust"');
    // 方法的標籤不是連結（找論文沒有方法的篩選）：整頁的連結只有站外的 EXTERNAL_LINKS 個和這兩個主題。
    expect(count(html, "<a ")).toBe(EXTERNAL_LINKS + 2);
  });

  it("does not render things that belong to later stories", () => {
    // 畫面上每一段字（標籤之間的字，去掉空白）；清單的每一行都不能是其中一段。
    const segments = html
      .split(/<[^>]+>/)
      .map((segment) => segment.replace(/\s+/g, ""))
      .filter((segment) => segment !== "");
    expect(LATER_STORIES.length).toBeGreaterThan(10);
    for (const text of LATER_STORIES) {
      expect(segments).not.toContain(text);
    }
  });

  it("averages citations per year up to the snapshot year", () => {
    // 1,234 次、2023 年、快照 2026 年 → 1234 / 3 = 411。
    expect(html).toContain(">1,234<");
    expect(html).toContain(">411<");
  });
});

describe("State Patterns 8: blocks", () => {
  it("empty: no abstract", () => {
    const html = render(fixture("no-abstract"));
    expect(html).toContain(TEXT["paper:paper.noAbstract.heading"]);
    expect(html).toContain(TEXT["paper:paper.noAbstract.message"]);
    expect(html).toContain(TEXT["paper:paper.noAbstract.publisher"]);
    expect(html).toContain('href="https://doi.org/10.5555/rr.3"');
    expect(html).toContain(TEXT["paper:paper.classification.titleOnly"]);
    expect(html).toContain('data-tag="unclassified"');
    expect(block(html, "research_model")).toContain(TEXT["paper:paper.model.noAbstract"]);
    expect(block(html, "research_model")).not.toContain("data-ai-badge");
    expect(html).not.toContain('data-block="zh_summary"');
    expect(html).not.toContain('data-block="abstract"');
    expect(html).toContain(TEXT["paper:paper.biblio.noAbstract"]);
    // 沒有摘要也沒有 DOI：不放到出版社頁面的連結。
    const page = fixture("no-abstract");
    page.paper.doi_url = null;
    page.full_text_links.doi_url = null;
    expect(render(page)).not.toContain(TEXT["paper:paper.noAbstract.publisher"]);
  });

  it.each([
    ["pending", PENDING],
    ["budget", BUDGET],
    ["all-failed", FAILED],
  ])("%s: each AI block is one line, without a badge or a time", (label, line) => {
    const html = render(fixture(label));
    expect(block(html, "zh_summary")).toContain(line);
    expect(block(html, "research_model")).toContain(line);
    expect(html).toContain(`${TEXT["paper:paper.classification.label"]}</span><span class="text-s15 leading-180 text-text-secondary">${line}`);
    expect(html).not.toContain("data-ai-badge");
    expect(html).not.toContain('data-tag');
    expect(count(html, line)).toBe(3);
    // 書目、原文摘要、取得全文照常。
    expect(html).toContain('data-block="abstract"');
    expect(html).toContain('data-full-text="open"');
  });

  it("failed only in the summary: the other blocks are unaffected", () => {
    const html = render(fixture("zh-failed"));
    expect(block(html, "zh_summary")).toContain(FAILED);
    expect(block(html, "zh_summary")).not.toContain("data-ai-badge");
    expect(block(html, "research_model")).toContain("data-ai-badge");
    expect(block(html, "classification")).toContain("data-tag");
    expect(count(html, FAILED)).toBe(1);
    // 中文重點沒成功時，第一個標章（分類）寫全文。
    expect(html).toContain(TEXT["ai:ai.badge.full"]);
  });

  it("three blocks in three different states on one page", () => {
    const html = render(fixture("mixed"));
    expect(block(html, "zh_summary")).toContain('data-block-state="ready"');
    expect(block(html, "zh_summary")).toContain("data-ai-badge");
    expect(html).toContain(`data-block-state="failed"`);
    expect(count(html, FAILED)).toBe(1);
    expect(block(html, "research_model")).toContain(PENDING);
    expect(count(html, PENDING)).toBe(1);
  });

  it("no basis: a summary field the abstract does not mention", () => {
    const page = fixture("normal");
    page.zh_summary.value!.theory = null;
    const html = render(page);
    expect(count(html, TEXT["paper:paper.summary.notMentioned"]!)).toBe(1);
  });

  it("empty summary and research model: four 'not mentioned' and the no-hypotheses line, with badges", () => {
    const html = render(fixture("empty"));
    expect(count(block(html, "zh_summary"), TEXT["paper:paper.summary.notMentioned"]!)).toBe(4);
    expect(block(html, "zh_summary")).toContain("data-ai-badge");
    expect(block(html, "research_model")).toContain(TEXT["paper:paper.model.empty"]);
    expect(block(html, "research_model")).toContain("data-ai-badge");
  });

  it("no basis: supported or not, not stated, not determined, and every evidence type", () => {
    const html = render(fixture("support-variants"));
    for (const key of ["not_stated", "not_determined", "partially_supported", "not_supported"]) {
      expect(html).toContain(TEXT[`paper:paper.support.${key}`]);
    }
    for (const key of ["analytical", "simulation", "qualitative", "conceptual"]) {
      expect(html).toContain(TEXT[`paper:paper.evidence.${key}`]);
    }
    // 只有「未說明」「不判定」旁邊有說明圖示（名稱給報讀器）；其他三種沒有。
    const supports = fixture("support-variants").research_model.value!.hypotheses.map((item) => item.support);
    const withInfo = supports.filter((support) => support === "not_stated" || support === "not_determined").length;
    expect(withInfo).toBeGreaterThan(0);
    expect(count(html, "data-support-info")).toBe(withInfo);
    expect(count(html, `aria-label="${TEXT["paper:paper.supportInfo.label"]}"`)).toBe(withInfo);
    expect(render(fixture("normal"))).not.toContain("data-support-info");
    // 證據來源是全文：寫「全文」，沒有「只根據摘要」那一行。
    expect(html).toContain(TEXT["paper:paper.model.fromFulltext"]);
    expect(html).not.toContain(TEXT["paper:paper.model.abstractOnly"]);
  });

  it("no basis: no open access version", () => {
    expect(render(fixture("normal-no-oa"))).not.toContain('data-full-text="open-access"');
  });

  it("no DOI: one line instead of the full text button, no bottom bar", () => {
    const html = render(fixture("no-doi"));
    expect(count(html, TEXT["paper:paper.fullText.noDoi"]!)).toBe(2);
    expect(html).not.toContain('data-full-text="open"');
    expect(html).not.toContain('data-full-text="bar"');
    expect(html).not.toContain("doi.org");
    // 沒有快照：沒有資料日期列。
    expect(html).not.toContain("data-data-date");
  });

  it("someone reported a field; members fixed the classification and disagree", () => {
    const html = render(fixture("reported"));
    const summary = block(html, "zh_summary");
    expect(count(summary, TEXT["paper:paper.summary.reported"]!)).toBe(1);
    expect(summary.indexOf('data-summary-field="method"')).toBeLessThan(summary.indexOf(TEXT["paper:paper.summary.reported"]!));
    expect(html).toContain(TEXT["paper:paper.classification.disagree"]!.replace("{{members}}", "2"));
    // 分類已核對：「已核對」，全文移到中文重點。
    expect(block(html, "classification")).toContain(TEXT["ai:ai.badge.verified"]);
    expect(summary).toContain(TEXT["ai:ai.badge.full"]);
    const fixed = render(fixture("fixed"));
    expect(fixed).toContain(TEXT["paper:paper.classification.fixed"]!.replace("{{members}}", "1"));
    expect(fixed).not.toContain(TEXT["paper:paper.classification.disagree"]!.replace("{{members}}", "1"));
  });

  it("not applicable (manually entered, Epic 6): the AI blocks are not shown", () => {
    const html = render(fixture("not-applicable"));
    for (const name of ["zh_summary", "classification", "research_model"]) {
      expect(html).not.toContain(`data-block="${name}"`);
    }
    expect(html).toContain('data-block="biblio"');
  });

  it("a 100-character summary field is shown in full", () => {
    const page = fixture("long-100");
    const html = render(page);
    for (const value of Object.values(page.zh_summary.value!)) {
      if (typeof value === "string" && value.length === 100) {
        expect(html).toContain(value);
      }
    }
  });
});

// Codex 審查 3.8 中 1、中 2、中 5、中 6 與 Opus 畫面審查低 8、低 9 的反例。
describe("review fixes", () => {
  const SUMMARY_TEXT = "only-in-the-empty-value-marker";

  it("ready or empty without provenance: no content and no badge, the 'not generated' line instead", () => {
    const page = fixture("normal");
    page.zh_summary.provenance = null;
    page.research_model.provenance = null;
    page.classification.provenance = null;
    const html = render(page);
    const value = fixture("normal");
    for (const name of ["zh_summary", "research_model", "classification"]) {
      const part = block(html, name);
      expect(part, name).toContain(FAILED);
      expect(part, name).not.toContain(PENDING);
      expect(part, name).toContain('data-provenance="missing"');
      expect(part, name).not.toContain("data-ai-badge");
    }
    expect(html).not.toContain(value.zh_summary.value!.research_question!);
    expect(html).not.toContain(value.research_model.value!.constructs[0]!.label_zh);
    expect(block(html, "classification")).not.toContain("data-tag");
    expect(html).not.toContain(TEXT["paper:paper.summary.screeningOnly"]);
    expect(html).not.toContain(TEXT["paper:paper.model.abstractOnly"]);
    expect(firstUnverifiedBadge(page)).toBeNull();

    const empty = fixture("empty");
    empty.zh_summary.provenance = null;
    empty.research_model.provenance = null;
    const emptyHtml = render(empty);
    expect(block(emptyHtml, "zh_summary")).toContain(FAILED);
    expect(block(emptyHtml, "zh_summary")).not.toContain(TEXT["paper:paper.summary.notMentioned"]);
    expect(block(emptyHtml, "research_model")).toContain(FAILED);
    expect(block(emptyHtml, "research_model")).not.toContain(TEXT["paper:paper.model.empty"]);
    expect(block(emptyHtml, "zh_summary")).not.toContain("data-ai-badge=");
    expect(block(emptyHtml, "research_model")).not.toContain("data-ai-badge=");
  });

  it("empty never reads its value, even when one comes along", () => {
    const page = fixture("empty");
    page.zh_summary.value = {
      ...fixture("normal").zh_summary.value!,
      research_question: SUMMARY_TEXT,
      reported_fields: ["method"],
    };
    page.research_model.value = fixture("normal").research_model.value;
    const html = render(page);
    const summary = block(html, "zh_summary");
    expect(summary).not.toContain(SUMMARY_TEXT);
    expect(count(summary, TEXT["paper:paper.summary.notMentioned"]!)).toBe(4);
    expect(summary).not.toContain(TEXT["paper:paper.summary.reported"]);
    const model = block(html, "research_model");
    expect(model).toContain(TEXT["paper:paper.model.empty"]);
    expect(model).not.toContain("data-construct");
    expect(model).not.toContain(fixture("normal").research_model.value!.constructs[0]!.label_zh);
  });

  it("members who fixed the classification are counted once for the whole paper", () => {
    // A 只修主題、B 只修方法：兩欄各 1 位，整篇 2 位（不是取比較大的那一欄）。
    const page = fixture("normal");
    const value = page.classification.value!;
    value.topic = { ...value.topic!, human_verified: true, fix_member_count: 1 };
    value.method = { ...value.method!, human_verified: true, fix_member_count: 1 };
    value.fix_member_count = 2;
    const html = block(render(page), "classification");
    expect(html).toContain(TEXT["paper:paper.classification.fixed"]!.replace("{{members}}", "2"));
    expect(html).not.toContain(TEXT["paper:paper.classification.fixed"]!.replace("{{members}}", "1"));
  });

  it("a fixed classification shows the verified badge next to 'fixed by 1 member'", () => {
    const page = fixture("fixed");
    expect(page.classification.provenance!.human_verified).toBe(true);
    const html = block(render(page), "classification");
    expect(html).toContain(TEXT["paper:paper.classification.fixed"]!.replace("{{members}}", "1"));
    expect(html).toContain(TEXT["ai:ai.badge.verified"]);
    expect(html).not.toContain(TEXT["ai:ai.badge.full"]);
    expect(firstUnverifiedBadge(page)).toBe("zh_summary");
  });

  it("display names not loaded: the rest of the page shows, the classification line is a message bar with retry", () => {
    const page = fixture("normal");
    const html = renderToStaticMarkup(<PaperPage page={page} names={null} onRetryNames={() => {}} />);
    const failed = html.slice(html.indexOf('data-names="failed"'));
    expect(failed).toContain(TEXT["paper:paper.names.loadFailed"]);
    expect(failed).toMatch(new RegExp(`<button type="button"[^>]*>${TEXT["paper:paper.names.retry"]}</button>`));
    expect(failed).toContain('role="alert"');
    // 分類的標籤與標章不顯示；期刊名稱不顯示；其他區塊照常。
    expect(html).not.toContain('data-block="classification"');
    expect(html).not.toContain(NAMES.journals.find((entry) => entry.key === "jes")!.name);
    expect(html).toContain(`>${page.paper.title}<`);
    expect(block(html, "zh_summary")).toContain(page.zh_summary.value!.research_question!);
    expect(block(html, "research_model")).toContain("data-construct");
    expect(html).toContain('data-block="abstract"');
    // 第一個未核對的標章（寫全文）移到中文重點。
    expect(firstUnverifiedBadge(page, false)).toBe("zh_summary");
    expect(block(html, "zh_summary")).toContain(TEXT["ai:ai.badge.full"]);
    expect(count(html, TEXT["ai:ai.badge.full"]!)).toBe(1);
    // 只有一個訊息條。
    expect(count(html, 'data-names="failed"')).toBe(1);
  });

  it("display names not loaded but nothing needs them: no message bar, the classification state line stays", () => {
    const page = fixture("all-failed");
    page.paper.journal = null;
    const html = renderToStaticMarkup(<PaperPage page={page} names={null} />);
    expect(html).not.toContain('data-names="failed"');
    expect(block(html, "classification")).toContain(FAILED);
    // 期刊有值時，就算分類沒有標籤，也要讓人知道期刊名稱沒讀到。
    const withJournal = fixture("all-failed");
    const journalHtml = renderToStaticMarkup(<PaperPage page={withJournal} names={null} />);
    expect(count(journalHtml, 'data-names="failed"')).toBe(1);
    expect(block(journalHtml, "classification")).toContain(FAILED);
  });

  it("the snapshot number's button is read as '#12', without the separator dot", () => {
    const html = render(fixture("normal"));
    expect(html).toMatch(/<button[^>]*aria-label="#12"[^>]*data-info-popover-trigger/);
  });
});

describe("AI text on the paper page", () => {
  it("nothing from the AI text is clickable or executable", () => {
    const html = render(fixture("xss"));
    const ai = ["zh_summary", "research_model"].map((name) => block(html, name)).join("");
    expect(ai).not.toMatch(/<(a|script|img|iframe|style|svg on)\b/);
    expect(ai).not.toMatch(/\s(src|on[a-z]+)=/);
    expect(ai).not.toContain("javascript:window");
    for (const match of ai.matchAll(/href="([^"]*)"/g)) {
      expect(match[1]).toMatch(/^https:\/\/doi\.org\//);
    }
  });
});

describe("first unverified badge", () => {
  it("is the classification, then the summary, then the research model", () => {
    expect(firstUnverifiedBadge(fixture("normal"))).toBe("classification");
    expect(firstUnverifiedBadge(fixture("reported"))).toBe("zh_summary");
    expect(firstUnverifiedBadge(fixture("zh-failed"))).toBe("classification");
    const modelOnly = fixture("all-failed");
    modelOnly.research_model = fixture("normal").research_model;
    expect(firstUnverifiedBadge(modelOnly)).toBe("research_model");
    expect(firstUnverifiedBadge(fixture("all-failed"))).toBeNull();
  });

  it("only the first badge on the page has the full wording", () => {
    const html = render(fixture("normal"));
    expect(count(html, TEXT["ai:ai.badge.full"]!)).toBe(1);
    expect(count(html, `</svg>${TEXT["ai:ai.badge.short"]}`)).toBe(2);
  });
});

describe("paper formatting", () => {
  it("volume, issue and pages", () => {
    expect(formatVolume({ volume: "42", issue: "3", first_page: "894", last_page: "925" })).toBe("42(3), 894–925");
    expect(formatVolume({ volume: "42", issue: null, first_page: "894", last_page: null })).toBe("42, 894");
    expect(formatVolume({ volume: null, issue: null, first_page: null, last_page: null })).toBeNull();
  });

  it("citations per year match the design's examples", () => {
    expect(citationsPerYear(10639, 2019, 2026)).toBe(1520);
    expect(citationsPerYear(8, 2025, 2026)).toBe(8);
    expect(citationsPerYear(8, 2026, 2026)).toBe(8);
  });

  it("paper ids are UUIDs", () => {
    expect(isPaperId("38000000-0000-4000-8000-000000000001")).toBe(true);
    expect(isPaperId("not-a-uuid")).toBe(false);
    expect(isPaperId("../me")).toBe(false);
  });

  it("splits a template at its placeholders", () => {
    expect(splitTemplate("a {{x}} b {{y}}")).toEqual([
      { text: "a ", slot: null },
      { text: "", slot: "x" },
      { text: " b ", slot: null },
      { text: "", slot: "y" },
    ]);
  });
});

describe("paper page content", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("research model: constructs, then each relation as cause → effect with its sentence, and the counts", () => {
    const page = fixture("normal");
    const html = block(render(page), "research_model");
    const model = page.research_model.value!;
    for (const construct of model.constructs) {
      expect(html).toContain(construct.label_zh);
      expect(html).toContain(construct.name_en);
    }
    expect(html).toContain(TEXT["paper:paper.model.constructs"]!.replace("{{constructs}}", String(model.constructs.length)));
    expect(html).toContain(TEXT["paper:paper.model.relations"]!.replace("{{relations}}", String(model.hypotheses.length)));
    const relations = html.split("data-relation=").slice(1);
    expect(relations).toHaveLength(model.hypotheses.length);
    model.hypotheses.forEach((hypothesis, index) => {
      const relation = relations[index]!;
      const cause = model.constructs.find((construct) => construct.construct_id === hypothesis.cause_construct_id)!;
      const effect = model.constructs.find((construct) => construct.construct_id === hypothesis.effect_construct_id)!;
      const arrow = relation.indexOf("<svg");
      expect(relation.indexOf(cause.label_zh)).toBeGreaterThan(-1);
      expect(relation.indexOf(cause.label_zh)).toBeLessThan(arrow);
      expect(relation.indexOf(effect.label_zh, arrow)).toBeGreaterThan(arrow);
      expect(relation).toContain(`“${hypothesis.sentence}”`);
    });
  });

  it("a relation whose cause or effect is not a listed construct is left out; no constructs reads as empty", () => {
    const page = fixture("normal");
    page.research_model.value!.hypotheses[0]!.effect_construct_id = "c9999999-0000-4000-8000-000000000009";
    const html = block(render(page), "research_model");
    expect(html.split("data-relation=").length - 1).toBe(1);
    expect(html).toContain(TEXT["paper:paper.model.relations"]!.replace("{{relations}}", "1"));
    page.research_model.value!.constructs = [];
    const empty = block(render(page), "research_model");
    expect(empty).toContain(TEXT["paper:paper.model.empty"]);
    expect(empty).not.toContain("data-relation");
  });

  it("journal, topic and method names come from the display names", () => {
    const html = render(fixture("normal"));
    const name = (list: { key: string; name: string }[], key: string) => list.find((entry) => entry.key === key)!.name;
    expect(block(html, "biblio")).toContain(name(NAMES.journals, "jes"));
    const tags = [...block(html, "classification").matchAll(/data-tag="true"[^>]*>([^<]*)</g)].map((match) => match[1]);
    expect(tags).toEqual([name(NAMES.topics, "platforms"), name(NAMES.topics, "trust"), name(NAMES.methods, "experiment")]);
  });

  it("a summary field that is empty or blank reads 'not mentioned'", () => {
    const page = fixture("normal");
    page.zh_summary.value!.theory = "";
    page.zh_summary.value!.findings = "   ";
    expect(count(block(render(page), "zh_summary"), TEXT["paper:paper.summary.notMentioned"]!)).toBe(2);
  });

  it("without a snapshot, the year for citations per year is today's (Taipei)", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    // 台北已經是 2027 年（UTC 還是 2026）。
    vi.setSystemTime(new Date("2026-12-31T17:00:00Z"));
    const html = render(fixture("no-snapshot"));
    // 1,234 次、2023 年、2027 年 → 1234 / 4 = 309。
    expect(html).toContain(">309<");
  });

  it("without a publication year, only the citation count", () => {
    const page = fixture("normal");
    page.paper.publication_year = null;
    const html = render(page);
    expect(html).toContain(">1,234<");
    expect(html).not.toContain(TEXT["paper:paper.biblio.citations"]!.split("{{perYear}}")[0]!.split("{{citations}}")[1]!.trim());
  });

  it("the abstract's source is shown next to its heading", () => {
    const page = fixture("normal");
    const source = (html: string) => block(html, "abstract");
    expect(source(render(page))).toContain(TEXT["paper:paper.abstract.source"]!.replace("{{source}}", TEXT["paper:paper.source.openalex"]!));
    page.abstract!.source = "semantic_scholar";
    expect(source(render(page))).toContain(
      TEXT["paper:paper.abstract.source"]!.replace("{{source}}", TEXT["paper:paper.source.semantic_scholar"]!),
    );
    page.abstract!.source = "uploaded_pdf";
    expect(source(render(page))).not.toContain(TEXT["paper:paper.abstract.source"]!.replace(" {{source}}", ""));
  });

  it("external addresses that are not http(s) are treated as no link", () => {
    const page = fixture("normal");
    page.full_text_links.open_access_url = "javascript:alert(1)";
    page.full_text_links.doi_url = "data:text/html,hi";
    page.paper.doi_url = "javascript:alert(2)";
    page.paper.openalex_url = "vbscript:x";
    const html = render(page);
    expect(html).not.toMatch(/javascript:|data:text|vbscript:/);
    expect(html).not.toContain('data-full-text="open"');
    expect(html).not.toContain(TEXT["paper:paper.fullText.openAccess"]);
  });

  it("no DOI but an open access version: the link, without the 'no full text link' line", () => {
    const page = fixture("no-doi");
    page.full_text_links.open_access_url = "https://example.com/open-access/rr-11.pdf";
    const html = render(page);
    expect(html).toContain(TEXT["paper:paper.fullText.openAccess"]);
    expect(html).not.toContain(TEXT["paper:paper.fullText.noDoi"]);
  });

  it("classification with no tags: no line and the full badge wording moves on", () => {
    const page = fixture("normal");
    page.classification.value!.topic!.keys = [];
    page.classification.value!.method!.keys = [];
    expect(firstUnverifiedBadge(page)).toBe("zh_summary");
    const html = render(page);
    expect(html).not.toContain('data-block="classification"');
    expect(count(html, TEXT["ai:ai.badge.full"]!)).toBe(1);
  });

  it("the two expect-missing lists for the paper page stay the same apart from their header", () => {
    const body = (text: string) => text.slice(text.indexOf("\n\n"));
    expect(body(expectMissing("paper-ai-failed.txt"))).toBe(body(expectMissing("paper.txt")));
  });
});
