// (6) axe（WCAG 2.1 A／AA）掃兩種建置的每個頁面，加上「找不到這一頁」。
// 【探測分支 ci/firefox-crash-probe 專用，不合併】PROBE_MODE 控制怎麼跑 axe，用來找 firefox 當掉的原因。
import AxeBuilder from "@axe-core/playwright";

import { expect, test } from "./fixtures.ts";
import { MISSING_PATH, SITES } from "./pages.ts";

const TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"];
const MODE = process.env.PROBE_MODE ?? "full";
const ONLY = (process.env.PROBE_ONLY ?? "").split(",").filter((rule) => rule !== "");
const DISABLE = (process.env.PROBE_DISABLE ?? "").split(",").filter((rule) => rule !== "");

// perrule：PROBE_ONLY 裡每一條規則單獨跑一次，另外整組一起跑一次，只跑論文頁。
if (MODE === "perrule") {
  for (const site of SITES) {
    for (const path of site.pages.filter((page) => /^\/papers\/[0-9a-f]/.test(page))) {
      for (const rules of [...ONLY.map((rule) => [rule]), ONLY]) {
        const label = rules.length === 1 ? rules[0] : "GROUP";
        test(`rule ${label} ${site.name} ${path}`, async ({ page }) => {
          await page.goto(`${site.baseURL}${path}`);
          await expect(page.locator("main")).toBeVisible();
          await new AxeBuilder({ page }).withRules(rules).analyze();
        });
      }
    }
  }
}

// element：用會當的規則（PROBE_ONLY），開頁後先照 PROBE_PREP 改頁面，再用 PROBE_INCLUDE／PROBE_EXCLUDE 限制範圍。
const PREP = process.env.PROBE_PREP ?? "";
const INCLUDE = process.env.PROBE_INCLUDE ?? "";
const EXCLUDE = process.env.PROBE_EXCLUDE ?? "";
if (MODE === "element") {
  for (const site of SITES) {
    for (const path of site.pages.filter((page) => /^\/papers\/[0-9a-f]/.test(page))) {
      test(`element ${PREP || "-"} ${site.name} ${path}`, async ({ page }) => {
        await page.goto(`${site.baseURL}${path}`);
        await expect(page.locator("main")).toBeVisible();
        const changed = await page.evaluate((prep) => {
          const all = (selector: string) => [...document.querySelectorAll(selector)];
          if (prep === "aside-to-div") {
            for (const aside of all("aside")) {
              const div = document.createElement("div");
              for (const attribute of [...aside.attributes]) div.setAttribute(attribute.name, attribute.value);
              div.append(...aside.childNodes);
              aside.replaceWith(div);
            }
            return all("aside").length === 0;
          }
          if (prep === "no-h1-lang") { for (const e of all("h1[lang]")) e.removeAttribute("lang"); return true; }
          if (prep === "no-br") { for (const e of all("br")) e.remove(); return true; }
          if (prep === "no-sticky") {
            for (const e of all("*")) if (getComputedStyle(e).position === "sticky") (e as HTMLElement).style.setProperty("position", "static", "important");
            return all("*").every((e) => getComputedStyle(e).position !== "sticky");
          }
          if (prep === "no-balance") {
            for (const e of all("*")) (e as HTMLElement).style.setProperty("text-wrap", "wrap", "important");
            return all("*").every((e) => getComputedStyle(e).getPropertyValue("text-wrap-style") !== "balance");
          }
          return true;
        }, PREP);
        expect(changed).toBe(true);
        let builder = new AxeBuilder({ page }).withRules(ONLY);
        if (INCLUDE !== "") builder = builder.include(INCLUDE);
        if (EXCLUDE !== "") builder = builder.exclude(EXCLUDE);
        await builder.analyze();
      });
    }
  }
}

for (const site of MODE === "perrule" || MODE === "element" ? [] : SITES) {
  for (const path of [...site.pages, MISSING_PATH]) {
    test(`${site.name} ${path}: no axe violations`, async ({ page }) => {
      await page.goto(`${site.baseURL}${path}`);
      await expect(page.locator("main")).toBeVisible();
      if (MODE === "noaxe") {
        await page.waitForTimeout(1500);
        await page.evaluate(() => document.querySelectorAll("*").length);
        return;
      }
      let builder = new AxeBuilder({ page });
      builder = ONLY.length > 0 ? builder.withRules(ONLY) : builder.withTags(TAGS);
      if (DISABLE.length > 0) builder = builder.disableRules(DISABLE);
      if (MODE === "legacy") builder = builder.setLegacyMode(true);
      const results = await builder.analyze();
      expect(results.violations.map((violation) => `${violation.id}: ${violation.help}`)).toEqual([]);
    });
  }
}
