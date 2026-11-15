// 錯誤回應 → i18n 鍵（Story 3.4、ADR-0047）：已知代碼用 errors.<code>，其他一律落到通用句；
// 後端的 title、detail 不會被拿來顯示；errors.json 的每個鍵都是後端真的會回的代碼。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { FALLBACK_ERROR_KEY, errorMessageKey } from "./error-message";
import i18n from "./index";
import common from "./zh-TW/common.json";
import errors from "./zh-TW/errors.json";

const REPO_ROOT = fileURLToPath(new URL("../../../", import.meta.url));

function problem(code: string, title = "Not Found"): Record<string, unknown> {
  return { type: "about:blank", title, status: 404, code };
}

describe("errorMessageKey", () => {
  it("uses errors.<code> for codes that have their own sentence", () => {
    expect(errorMessageKey(problem("not_found"))).toBe("errors:not_found");
    expect(errorMessageKey(problem("not_authenticated", "Unauthorized"))).toBe("errors:not_authenticated");
    expect(i18n.t(errorMessageKey(problem("not_found")))).toBe(errors.not_found);
    expect(i18n.t(errorMessageKey(problem("not_authenticated")))).toBe(errors.not_authenticated);
  });

  it.each([
    ["a backend code without its own sentence", problem("origin_rejected", "Forbidden")],
    ["an unknown code", problem("no_such_code")],
    ["a non-string code", { code: 404 }],
    ["no code at all", { title: "Not Found", status: 404 }],
    ["null", null],
    ["a string", "Internal Server Error"],
    ["an array", ["not_found"]],
    ["undefined (network error)", undefined],
    ["an inherited key name", problem("toString")],
  ])("falls back to the generic sentence for %s", (_label, body) => {
    expect(errorMessageKey(body)).toBe(FALLBACK_ERROR_KEY);
    expect(i18n.t(errorMessageKey(body))).toBe(common.states.loadFailed.message);
  });

  it("never shows the backend title or detail", () => {
    const body = { ...problem("origin_rejected", "Forbidden"), detail: "Origin header missing" };
    const shown = i18n.t(errorMessageKey(body));

    expect(shown).not.toContain("Forbidden");
    expect(shown).not.toContain("Origin");
    expect(i18n.t(errorMessageKey(problem("not_found", "Not Found")))).not.toContain("Not Found");
  });

  it("only has keys for codes the backend can return (openapi.json)", () => {
    const openapi = JSON.parse(readFileSync(`${REPO_ROOT}openapi.json`, "utf-8")) as {
      components: { schemas: { ProblemDetails: { properties: { code: { enum: string[] } } } } };
    };
    const codes = openapi.components.schemas.ProblemDetails.properties.code.enum;

    for (const key of Object.keys(errors)) {
      expect(codes).toContain(key);
    }
  });
});
