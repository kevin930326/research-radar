// 共用元件（Story 3.2）：用 react-dom/server 靜態渲染（ADR-0036 第 7 點，不另裝 jsdom）。
// 說明框的開合、Esc、焦點交給 Playwright（e2e/dev-components.spec.ts）。
import "../i18n";

import { RouterProvider, createMemoryHistory, createRootRoute, createRouter } from "@tanstack/react-router";
import { renderToStaticMarkup, renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import expected from "../i18n/expected/notice.zh-TW.json";
import { AiBadge, ProvenanceDetails, type Provenance } from "./AiBadge";
import { Button } from "./Button";
import { PaperAbstract, PaperTitle } from "./PaperText";
import { LoadFailed } from "./states/LoadFailed";
import { StatusTag } from "./StatusTag";
import { ExternalLink, TextLink } from "./TextLink";
import { taipeiDate } from "./taipei-date";

const text = expected.strings;
// 測試用的字放在變數裡（JSX 裡不寫死文字，i18next/no-literal-string）。
const LABEL = "label";
const UNVERIFIED: Provenance = { model: "claude-haiku-5-5", generatedAt: "2026-11-11T17:30:00Z", verified: false };
const VERIFIED: Provenance = { ...UNVERIFIED, verified: true };

/** 去掉標籤，只留畫面上看得到的字。 */
function visibleText(html: string): string {
  return html.replace(/<[^>]+>/g, "");
}

describe("Button", () => {
  it("primary: warm white, full height, rounded, 0.25 s hover", () => {
    const html = renderToStaticMarkup(
      <Button variant="primary" arrow>{LABEL}</Button>,
    );
    expect(html).toMatch(/^<button [^>]*type="button"/);
    expect(html).toContain('data-variant="primary"');
    for (const name of ["bg-text-primary", "text-on-button", "h-button-height", "rounded-full", "px-8", "duration-250"]) {
      expect(html).toContain(name);
    }
    expect(html).toContain('aria-hidden="true"');
  });

  it("secondary: transparent with a 1px border", () => {
    const html = renderToStaticMarkup(<Button variant="secondary">{LABEL}</Button>);
    expect(html).toContain('data-variant="secondary"');
    for (const name of ["border", "border-border", "bg-transparent", "text-text-primary", "h-button-height"]) {
      expect(html.split('"').join(" ").split(" ")).toContain(name);
    }
  });

  it("disabled: not clickable, 40% opacity", () => {
    const html = renderToStaticMarkup(
      <Button variant="primary" disabled>{LABEL}</Button>,
    );
    expect(html).toMatch(/<button [^>]*disabled=""/);
    expect(html).toContain("disabled:opacity-40");
  });
});

describe("links", () => {
  it("external: new tab, noopener noreferrer, ends with ↗", () => {
    const html = renderToStaticMarkup(<ExternalLink href="https://doi.org/10.1/x">DOI</ExternalLink>);
    expect(html).toContain('href="https://doi.org/10.1/x"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
    expect(visibleText(html)).toBe("DOI↗");
    expect(html).toContain("min-h-tap-target");
    expect(html).toContain("min-w-tap-target");
  });

  it("internal: same tab, at least 44×44", async () => {
    const rootRoute = createRootRoute({ component: () => <TextLink to="/">{LABEL}</TextLink> });
    const router = createRouter({ routeTree: rootRoute, history: createMemoryHistory() });
    await router.load();
    const html = renderToString(<RouterProvider router={router} />);
    expect(html).toMatch(/<a [^>]*href="\/"/);
    expect(html).not.toContain("target=");
    expect(html).toContain("min-h-tap-target");
    expect(html).toContain("min-w-tap-target");
  });
});

describe("AiBadge", () => {
  it("shows nothing without provenance", () => {
    expect(renderToStaticMarkup(<AiBadge provenance={null} />)).toBe("");
    expect(renderToStaticMarkup(<AiBadge provenance={undefined} first />)).toBe("");
  });

  // 菱形是設計稿的 SVG 圖示（Story 3.8 起；之前是 ◇／◆ 字元），所以看得到的字只有標籤，菱形看 data-ai-badge-mark。
  it("unverified: open diamond + short label, or the full wording for the first one on the page", () => {
    const short = renderToStaticMarkup(<AiBadge provenance={UNVERIFIED} />);
    expect(visibleText(short)).toBe(text["ai:ai.badge.short"]);
    expect(short).toContain('data-ai-badge-mark="open"');
    const full = renderToStaticMarkup(<AiBadge provenance={UNVERIFIED} first />);
    expect(visibleText(full)).toBe(text["ai:ai.badge.full"]);
    expect(full).toContain('data-ai-badge-mark="open"');
  });

  it("verified: solid diamond + verified label, first or not", () => {
    for (const first of [false, true]) {
      const html = renderToStaticMarkup(<AiBadge provenance={VERIFIED} first={first} />);
      expect(visibleText(html)).toBe(text["ai:ai.badge.verified"]);
      expect(html).toContain('data-ai-badge-mark="solid"');
      expect(html).toContain('data-ai-badge="verified"');
    }
  });

  it("is a button you can click (at least 44×44) with a closed popover", () => {
    const html = renderToStaticMarkup(<AiBadge provenance={UNVERIFIED} />);
    expect(html).toMatch(/^<button [^>]*type="button"/);
    expect(html).toContain('aria-expanded="false"');
    expect(html).toContain("min-h-tap-target");
    expect(html).toContain("min-w-tap-target");
  });
});

describe("taipeiDate", () => {
  it("converts UTC to the Taipei calendar date", () => {
    expect(taipeiDate("2026-11-11T17:30:00Z")).toBe("2026-11-12");
    expect(taipeiDate("2026-11-11T15:59:59Z")).toBe("2026-11-11");
    expect(taipeiDate("2026-11-11T16:00:00Z")).toBe("2026-11-12");
  });

  it("returns null for a malformed time instead of throwing", () => {
    expect(taipeiDate("not-a-date")).toBeNull();
    expect(taipeiDate("")).toBeNull();
  });
});

describe("AiBadge with a malformed generatedAt", () => {
  it("still renders the badge, without a date", () => {
    const html = renderToStaticMarkup(<AiBadge provenance={{ ...UNVERIFIED, generatedAt: "not-a-date" }} />);
    expect(visibleText(html)).toBe(text["ai:ai.badge.short"]);
    expect(html).toContain('data-ai-badge-mark="open"');
  });

  it("the popover content shows the model and leaves out the unreadable date", () => {
    const html = renderToStaticMarkup(
      <ProvenanceDetails full={LABEL} provenance={{ ...UNVERIFIED, generatedAt: "not-a-date" }} />,
    );
    expect(html).toContain(`<span data-provenance="model">${UNVERIFIED.model}</span>`);
    expect(html).not.toContain('data-provenance="date"');
    expect(renderToStaticMarkup(<ProvenanceDetails full={LABEL} provenance={UNVERIFIED} />)).toContain(
      '<span data-provenance="date">2026-11-12</span>',
    );
  });
});

describe("StatusTag", () => {
  it("plain tag: not clickable, 22px, square, 1px border", () => {
    const html = renderToStaticMarkup(<StatusTag>{LABEL}</StatusTag>);
    expect(html).toMatch(/^<span [^>]*data-status-tag/);
    expect(html).not.toContain("<button");
    for (const name of ["h-[22px]", "rounded-none", "border-border", "font-mono", "text-label-mono"]) {
      expect(html).toContain(name);
    }
  });

  it("with info: a 44×44 button around the same tag", () => {
    const html = renderToStaticMarkup(<StatusTag info="why">{LABEL}</StatusTag>);
    expect(html).toMatch(/^<button [^>]*type="button"/);
    expect(html).toContain("min-h-tap-target");
    expect(html).toContain("min-w-tap-target");
    expect(html).toContain("data-status-tag");
    // 說明框平常是關的，內容不在畫面上。
    expect(visibleText(html)).toBe(LABEL);
  });
});

describe("paper title and abstract", () => {
  it("default to lang=en", () => {
    expect(renderToStaticMarkup(<PaperTitle>{LABEL}</PaperTitle>)).toMatch(/^<p lang="en" data-paper-text="title"/);
    expect(renderToStaticMarkup(<PaperAbstract>{LABEL}</PaperAbstract>)).toMatch(
      /^<p lang="en" data-paper-text="abstract"/,
    );
  });

  it("take the paper's language and heading level", () => {
    const html = renderToStaticMarkup(
      <PaperTitle lang="zh-Hant" as="h1" size="row">{LABEL}</PaperTitle>,
    );
    expect(html).toMatch(/^<h1 lang="zh-Hant"/);
    expect(html).toContain("text-paper-title-row");
    expect(html).toContain("text-balance");
    expect(renderToStaticMarkup(<PaperAbstract lang="zh-Hant">{LABEL}</PaperAbstract>)).toContain('lang="zh-Hant"');
  });
});

describe("LoadFailed", () => {
  it("the reload button is at least 44×44", () => {
    const html = renderToStaticMarkup(<LoadFailed />);
    const button = /<button [^>]*>/.exec(html)?.[0] ?? "";
    expect(button).toContain("min-h-tap-target");
    expect(button).toContain("min-w-tap-target");
  });
});
