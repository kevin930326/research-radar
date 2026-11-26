// (6) axe（WCAG 2.1 A／AA）掃兩種建置的每個頁面，加上「找不到這一頁」。
// 【探測分支 ci/firefox-crash-probe 專用，不合併】PROBE_MODE 控制怎麼跑 axe，用來找 firefox 當掉的原因。
import AxeBuilder from "@axe-core/playwright";
import axeCore from "axe-core";

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

// walk：注入 axe 後逐個元素呼叫（getrole：只算角色；run：對單一元素跑 PROBE_ONLY 的規則），當掉時印出最後一個元素。
if (MODE === "walk-getrole" || MODE === "walk-run") {
  for (const site of SITES) {
    for (const path of site.pages.filter((page) => /^\/papers\/[0-9a-f]/.test(page))) {
      test(`${MODE} ${site.name} ${path}`, async ({ page }) => {
        await page.goto(`${site.baseURL}${path}`);
        await expect(page.locator("main")).toBeVisible();
        await page.evaluate(axeCore.source);
        const signatures = await page.evaluate(() => {
          (window as unknown as { axe: { setup: (node: Node) => void } }).axe.setup(document);
          return [...document.querySelectorAll("*")].map((e) => `${e.tagName.toLowerCase()} ${[...e.attributes].map((a) => `${a.name}=${a.value.slice(0, 40)}`).join(" ")}`.slice(0, 200));
        });
        for (const [index, signature] of signatures.entries()) {
          try {
            await page.evaluate(
              async ({ index, mode, rules }) => {
                const axe = (window as unknown as { axe: any }).axe; // eslint-disable-line @typescript-eslint/no-explicit-any
                const node = document.querySelectorAll("*")[index];
                if (mode === "walk-getrole") return axe.commons.aria.getRole(node);
                axe.teardown();
                await axe.run({ include: [node] }, { runOnly: { type: "rule", values: rules } });
                return null;
              },
              { index, mode: MODE, rules: ONLY },
            );
          } catch (error) {
            console.log(`CRASH-AT ${site.name} ${path} #${index}/${signatures.length} ${signature}`);
            throw error;
          }
        }
      });
    }
  }
}

// trace：對論文頁的每個 section[aria-labelledby] 算角色；算之前把常用的原生 API 包起來，每次呼叫先用同步請求
// 把名稱送到測試端（page.route 攔下），當掉時印出最後 8 筆呼叫。
if (MODE === "trace") {
  for (const site of SITES) {
    for (const path of site.pages.filter((page) => /^\/papers\/[0-9a-f]/.test(page))) {
      test(`trace ${site.name} ${path}`, async ({ page }) => {
        const calls: string[] = [];
        await page.route(/\/__probe\//, (route) => {
          calls.push(decodeURIComponent(route.request().url().split("/__probe/")[1] ?? ""));
          return route.fulfill({ status: 204, body: "" });
        });
        await page.goto(`${site.baseURL}${path}`);
        await expect(page.locator("main")).toBeVisible();
        await page.evaluate(axeCore.source);
        const count = await page.evaluate(() => {
          (window as unknown as { axe: { setup: (node: Node) => void } }).axe.setup(document);
          const describe = (target: unknown): string => {
            if (target instanceof Element) return `${target.tagName.toLowerCase()}${target.id ? `#${target.id}` : ""}`;
            if (target instanceof Text) return "#text";
            if (target instanceof Document) return "document";
            if (target instanceof CanvasRenderingContext2D) return `ctx(${target.font})`;
            return typeof target;
          };
          let depth = 0;
          const send = (label: string) => {
            const request = new XMLHttpRequest();
            request.open("GET", `/__probe/${encodeURIComponent(label.slice(0, 180))}`, false);
            request.send();
          };
          const wrap = (owner: object, name: string, kind: "value" | "get") => {
            const descriptor = Object.getOwnPropertyDescriptor(owner, name);
            if (descriptor === undefined) return;
            const original = kind === "value" ? descriptor.value : descriptor.get;
            if (typeof original !== "function") return;
            const wrapped = function (this: unknown, ...args: unknown[]) {
              if (depth === 0) {
                depth++;
                try {
                  const first = args[0];
                  const extra = typeof first === "string" ? ` "${first.slice(0, 60)}"` : first instanceof Node ? ` ${describe(first)}` : "";
                  send(`${name} on ${describe(name === "getComputedStyle" ? first : this)}${name === "getComputedStyle" ? ` ${String(args[1] ?? "")}` : extra}`);
                } finally {
                  depth--;
                }
              }
              return original.apply(this, args);
            };
            Object.defineProperty(owner, name, kind === "value" ? { ...descriptor, value: wrapped } : { ...descriptor, get: wrapped });
          };
          wrap(window, "getComputedStyle", "value");
          for (const name of ["getBoundingClientRect", "getClientRects", "closest", "matches", "querySelector", "querySelectorAll", "getAttribute", "hasAttribute", "getRootNode", "checkVisibility", "getAnimations", "getAttributeNode", "attachShadow"])
            wrap(Element.prototype, name, "value");
          for (const name of ["getElementById", "elementsFromPoint", "elementFromPoint", "createElement", "createRange", "createTreeWalker"]) wrap(Document.prototype, name, "value");
          for (const name of ["measureText", "fillText", "getImageData", "clearRect"]) wrap(CanvasRenderingContext2D.prototype, name, "value");
          wrap(HTMLCanvasElement.prototype, "getContext", "value");
          for (const name of ["getClientRects", "getBoundingClientRect", "selectNodeContents", "setStart"]) wrap(Range.prototype, name, "value");
          wrap(CSSStyleDeclaration.prototype, "getPropertyValue", "value");
          for (const name of ["innerText", "offsetWidth", "offsetHeight", "offsetParent", "inert"]) wrap(HTMLElement.prototype, name, "get");
          for (const name of ["textContent", "nodeValue", "isConnected", "assignedSlot"]) wrap(Node.prototype, name, "get");
          for (const name of ["scrollHeight", "scrollWidth", "clientHeight", "clientWidth", "shadowRoot", "slot"]) wrap(Element.prototype, name, "get");
          return document.querySelectorAll("section[aria-labelledby]").length;
        });
        for (let index = 0; index < count; index++) {
          calls.push(`== getRole section #${index}`);
          try {
            await page.evaluate((index) => {
              const axe = (window as unknown as { axe: any }).axe; // eslint-disable-line @typescript-eslint/no-explicit-any
              return axe.commons.aria.getRole(document.querySelectorAll("section[aria-labelledby]")[index]);
            }, index);
          } catch (error) {
            console.log(`TRACE-CRASH ${site.name} ${path} calls=${calls.length}\n${calls.slice(-8).map((call) => `  LAST ${call}`).join("\n")}`);
            throw error;
          }
        }
        console.log(`TRACE-OK ${site.name} ${path} calls=${calls.length}`);
      });
    }
  }
}

// steps：不包 API，把「section 的角色」拆成幾步，各用一次 evaluate，看當在哪一步。
const STEPS = ["refs", "visible-h2", "subtree-h2", "name-h2", "labelledby-section", "role-section"];
if (MODE === "steps") {
  for (const site of SITES) {
    for (const path of site.pages.filter((page) => /^\/papers\/[0-9a-f]/.test(page))) {
      test(`steps ${site.name} ${path}`, async ({ page }) => {
        await page.goto(`${site.baseURL}${path}`);
        await expect(page.locator("main")).toBeVisible();
        await page.evaluate(axeCore.source);
        const count = await page.evaluate(() => {
          (window as unknown as { axe: { setup: (node: Node) => void } }).axe.setup(document);
          return document.querySelectorAll("section[aria-labelledby]").length;
        });
        for (let index = 0; index < count; index++) {
          for (const step of STEPS) {
            try {
              await page.evaluate(
                ({ index, step }) => {
                  const axe = (window as unknown as { axe: any }).axe; // eslint-disable-line @typescript-eslint/no-explicit-any
                  const section = document.querySelectorAll("section[aria-labelledby]")[index] as Element;
                  const heading = document.getElementById(section.getAttribute("aria-labelledby") ?? "");
                  const vSection = axe.utils.getNodeFromTree(section);
                  const vHeading = heading === null ? null : axe.utils.getNodeFromTree(heading);
                  if (step === "refs") return heading === null ? "none" : heading.tagName;
                  if (step === "visible-h2") return heading === null ? null : axe.commons.dom.isVisibleToScreenReaders(heading);
                  if (step === "subtree-h2") return vHeading === null ? null : axe.commons.text.subtreeText(vHeading, { inLabelledByContext: true, startNode: vSection });
                  if (step === "name-h2") return vHeading === null ? null : axe.commons.text.accessibleTextVirtual(vHeading, { inLabelledByContext: true, startNode: vSection });
                  if (step === "labelledby-section") return axe.commons.aria.arialabelledbyText(vSection);
                  return axe.commons.aria.getRole(section);
                },
                { index, step },
              );
            } catch (error) {
              console.log(`STEP-CRASH ${step} section#${index} ${site.name} ${path}`);
              throw error;
            }
          }
        }
      });
    }
  }
}

// reflect：不跑 axe，只在頁面世界讀每個有 aria-labelledby／aria-describedby 的元素的反射屬性。
if (MODE === "reflect") {
  for (const site of SITES) {
    for (const path of site.pages.filter((page) => /^\/papers\/[0-9a-f]/.test(page))) {
      test(`reflect ${site.name} ${path}`, async ({ page }) => {
        await page.goto(`${site.baseURL}${path}`);
        await expect(page.locator("main")).toBeVisible();
        const read = await page.evaluate(() => {
          const elements = [...document.querySelectorAll("[aria-labelledby], [aria-describedby]")] as (Element & { ariaLabelledByElements?: unknown; ariaDescribedByElements?: unknown })[];
          return elements.map((element) => [element.ariaLabelledByElements, element.ariaDescribedByElements].map((value) => (Array.isArray(value) ? value.length : String(value))).join("/"));
        });
        console.log(`REFLECT-READ ${read.length}`);
      });
    }
  }
}

for (const site of MODE === "perrule" || MODE === "element" || MODE.startsWith("walk") || MODE === "trace" || MODE === "steps" || MODE === "reflect" ? [] : SITES) {
  for (const path of [...site.pages, MISSING_PATH]) {
    test(`${site.name} ${path}: no axe violations`, async ({ page }) => {
      await page.goto(`${site.baseURL}${path}`);
      await expect(page.locator("main")).toBeVisible();
      if (MODE === "noaxe") {
        await page.waitForTimeout(1500);
        await page.evaluate(() => document.querySelectorAll("*").length);
        return;
      }
      console.log(`REFLECTION ${String(await page.evaluate(() => "ariaLabelledByElements" in Element.prototype))}`);
      let builder = new AxeBuilder({ page });
      builder = MODE === "full" ? builder.withTags(TAGS) : builder.withRules(ONLY);
      if (DISABLE.length > 0) builder = builder.disableRules(DISABLE);
      if (MODE === "legacy") builder = builder.setLegacyMode(true);
      const results = await builder.analyze();
      expect(results.violations.map((violation) => `${violation.id}: ${violation.help}`)).toEqual([]);
    });
  }
}
