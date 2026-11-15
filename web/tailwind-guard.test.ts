// 建置檢查（tailwind-guard.ts）：預設類別與不存在的斷點被擋、照規則寫的不被誤擋、整個 src/ 通過，
// 而且 Vite 外掛真的會讓建置失敗。
import { readFileSync, readdirSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { beforeAll, describe, expect, it } from "vitest";

import {
  extractTokens,
  findInvalidClasses,
  formatError,
  isCheckedFile,
  loadDesignSystem,
  NOT_CLASSES,
  tailwindGuard,
} from "./tailwind-guard.ts";

const WEB_ROOT = path.dirname(fileURLToPath(import.meta.url));
const FIXTURES = path.join(WEB_ROOT, "lint-fixtures", "src", "components");

type DesignSystem = Awaited<ReturnType<typeof loadDesignSystem>>;
let design: DesignSystem;

beforeAll(async () => {
  design = await loadDesignSystem();
});

function checkFile(file: string): string[] {
  return findInvalidClasses(design, extractTokens(file, readFileSync(file, "utf-8")));
}

function walk(directory: string): string[] {
  return readdirSync(directory).flatMap((name) => {
    const file = path.join(directory, name);
    return statSync(file).isDirectory() ? walk(file) : [file];
  });
}

describe("tailwind guard", () => {
  it("blocks default Tailwind classes and unknown breakpoints", () => {
    expect(checkFile(path.join(FIXTURES, "DefaultTailwindClass.tsx")).sort()).toEqual(
      ["bg-red-500", "lg:flex", "m-0", "text-xl"].sort(),
    );
  });

  it("blocks arbitrary breakpoints, custom @media / @container and dark: even though they emit CSS", () => {
    expect(checkFile(path.join(FIXTURES, "ForbiddenVariants.tsx")).sort()).toEqual(
      [
        "min-[1024px]:flex",
        "max-[600px]:hidden",
        "[@media(min-width:900px)]:flex",
        "[@container(min-width:400px)]:flex",
        "@min-[400px]:flex",
        "dark:bg-background",
      ].sort(),
    );
    // not- 包一層也一樣；主題裡的斷點（split、max-split、tablet）照樣可以用。
    expect(findInvalidClasses(design, ["not-dark:flex", "hover:max-[600px]:hidden"])).toEqual([
      "not-dark:flex",
      "hover:max-[600px]:hidden",
    ]);
    expect(findInvalidClasses(design, ["split:flex", "max-split:hidden", "tablet:grid-cols-3", "min-w-[0]"])).toEqual(
      [],
    );
  });

  it("the not-a-class list: every entry has a reason and would be flagged without the list", () => {
    expect(NOT_CLASSES.size).toBeGreaterThan(0);
    for (const [word, reason] of NOT_CLASSES) {
      expect(reason.trim(), word).not.toBe("");
    }
    const words = [...NOT_CLASSES.keys()];
    expect(findInvalidClasses(design, words)).toEqual([]);
    // 只放過整個字：前面加 variant 就是想當類別用，照樣擋（也證明每一項不放清單就會被擋）。
    const asClasses = words.map((word) => `hover:${word}`);
    expect(findInvalidClasses(design, asClasses)).toEqual(asClasses);
  });

  it("does not exempt real Tailwind class names: ease-in, flex-start, row-reverse, order-desc written as classes are flagged", () => {
    // 這些字不在例外清單：寫成 className 時（例如以為 ease-in 是時間曲線類別）建置要失敗。
    const code =
      'export const A = <p className="ease-in ease-out flex-start flex-end row-reverse col-reverse order-asc order-desc" />;';
    expect(findInvalidClasses(design, extractTokens("x.tsx", code))).toEqual([
      "ease-in",
      "ease-out",
      "flex-start",
      "flex-end",
      "row-reverse",
      "col-reverse",
      "order-asc",
      "order-desc",
    ]);
    for (const word of ["ease-in", "ease-out", "flex-start", "flex-end", "row-reverse", "col-reverse", "order-asc", "order-desc"]) {
      expect(NOT_CLASSES.has(word), word).toBe(false);
    }
  });

  it("allows token classes, layout arbitrary values, i18n keys, URLs, sentences and component props", () => {
    expect(checkFile(path.join(FIXTURES, "AllowedStyles.tsx"))).toEqual([]);
  });

  it("checks variants by the whole class, not just the utility", () => {
    expect(findInvalidClasses(design, ["split:mb-4", "hover:underline", "max-split:mb-4"])).toEqual([]);
    expect(findInvalidClasses(design, ["split:mb-0", "md:text-body", "hover:text-red-500"])).toEqual([
      "split:mb-0",
      "md:text-body",
      "hover:text-red-500",
    ]);
  });

  it("flags bare utilities that emit no CSS, but not positional prop words", () => {
    expect(findInvalidClasses(design, ["rounded", "shadow", "blur"])).toEqual(["rounded", "shadow", "blur"]);
    expect(findInvalidClasses(design, ["bottom", "start", "top", "end", "left", "right", "inset"])).toEqual([]);
  });

  it("skips half words next to ${} in template literals", () => {
    const tokens = extractTokens("x.ts", "const a = `px-${n} text-body mb-${m}`;");
    expect(tokens).toEqual(["text-body"]);
  });

  it("passes every checked file in src/", () => {
    const files = walk(path.join(WEB_ROOT, "src")).filter(isCheckedFile);
    expect(files.length).toBeGreaterThan(10);
    const errors = files.flatMap((file) => {
      const invalid = checkFile(file);
      return invalid.length > 0 ? [formatError(file, invalid)] : [];
    });
    expect(errors).toEqual([]);
  });

  it("only checks non-test ts/tsx under src/", () => {
    expect(isCheckedFile(path.join(WEB_ROOT, "src", "pages", "NoticePage.tsx"))).toBe(true);
    expect(isCheckedFile(path.join(WEB_ROOT, "src", "router.tsx?import"))).toBe(true);
    expect(isCheckedFile(path.join(WEB_ROOT, "src", "components", "components.test.tsx"))).toBe(false);
    expect(isCheckedFile(path.join(WEB_ROOT, "src", "styles.css"))).toBe(false);
    expect(isCheckedFile(path.join(FIXTURES, "DefaultTailwindClass.tsx"))).toBe(false);
    // 資料來源層沒有畫面：HTTP 標頭（content-type）這類字串不是類別。
    expect(isCheckedFile(path.join(WEB_ROOT, "src", "datasource", "api", "index.ts"))).toBe(false);
  });

  it("the Vite plugin skips the datasource layer", async () => {
    const plugin = tailwindGuard();
    const transform = plugin.transform as (this: unknown, code: string, id: string) => Promise<unknown>;
    const file = path.join(WEB_ROOT, "src", "datasource", "api", "headers.ts");
    const context = {
      error(message: string): never {
        throw new Error(message);
      },
    };
    const code = 'export const HEADERS = { "content-type": "application/json", order: "z-index border-box end-to-end" };';
    expect(findInvalidClasses(design, extractTokens(file, code))).not.toEqual([]);
    await expect(transform.call(context, code, file)).resolves.toBeNull();
  });

  it("vite.config.ts registers the guard for builds and the dev server", async () => {
    const { default: config } = await import("./vite.config.ts");
    const previous = process.env.VITE_BUILD_TARGET;
    process.env.VITE_BUILD_TARGET = "member";
    try {
      for (const env of [
        { command: "build", mode: "production" },
        { command: "serve", mode: "development" },
      ] as const) {
        const resolved = typeof config === "function" ? await config({ ...env, isPreview: false }) : config;
        const names = (resolved.plugins ?? [])
          .flat(Infinity as 1)
          .map((plugin) => (plugin !== null && typeof plugin === "object" && "name" in plugin ? plugin.name : null));
        expect(names, env.command).toContain("research-radar:tailwind-guard");
      }
    } finally {
      if (previous === undefined) {
        delete process.env.VITE_BUILD_TARGET;
      } else {
        process.env.VITE_BUILD_TARGET = previous;
      }
    }
  });

  it("the Vite plugin fails the build with the file name and the classes", async () => {
    const plugin = tailwindGuard();
    const transform = plugin.transform as (this: unknown, code: string, id: string) => Promise<unknown>;
    const file = path.join(WEB_ROOT, "src", "components", "Broken.tsx");
    const context = {
      error(message: string): never {
        throw new Error(message);
      },
    };
    const code = readFileSync(path.join(FIXTURES, "DefaultTailwindClass.tsx"), "utf-8");
    const message = await transform.call(context, code, file).then(
      () => "",
      (error: unknown) => (error instanceof Error ? error.message : String(error)),
    );
    expect(message).toContain("src/components/Broken.tsx");
    for (const name of ["bg-red-500", "lg:flex", "m-0", "text-xl"]) {
      expect(message).toContain(name);
    }
    await expect(
      transform.call(context, readFileSync(path.join(FIXTURES, "AllowedStyles.tsx"), "utf-8"), file),
    ).resolves.toBeNull();
  });
});
