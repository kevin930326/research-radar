// 示範站的開發伺服器與 vite preview 送出 preview/（Story 3.14 示範站）：只送資料夾裡的 .json、跳不出資料夾；
// 切換「載入中」「錯誤」時 manifest.json 照常回、其他檔停著或回 500，切回 ok 停著的請求才回。
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import type { IncomingMessage, ServerResponse } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { demoPreviewMiddleware, resolvePreviewFile } from "./demo-preview-server";

let base: string;
let root: string;

beforeAll(() => {
  base = mkdtempSync(join(tmpdir(), "demo-preview-"));
  root = join(base, "preview");
  mkdirSync(join(root, "2-1"), { recursive: true });
  writeFileSync(join(root, "manifest.json"), '{"export_id":"2-1"}');
  writeFileSync(join(root, "2-1", "titles.json"), '{"titles":[]}');
  writeFileSync(join(root, "2-1", "notes.txt"), "not json");
  writeFileSync(join(base, "outside.json"), '{"secret":true}');
  symlinkSync(join(base, "outside.json"), join(root, "link.json"));
});

afterAll(() => {
  rmSync(base, { recursive: true, force: true });
});

describe("resolvePreviewFile", () => {
  it("finds .json files inside the folder", () => {
    expect(resolvePreviewFile(root, "/preview/manifest.json")).toMatch(/manifest\.json$/);
    expect(resolvePreviewFile(root, "/preview/2-1/titles.json")).toMatch(/titles\.json$/);
  });

  it("refuses paths outside the folder, symlinks out, other file types and missing files", () => {
    for (const path of [
      "/preview/../outside.json",
      "/preview/%2e%2e/outside.json",
      "/preview/2-1/..%2f..%2foutside.json",
      "/preview/link.json",
      "/preview/2-1/notes.txt",
      "/preview/2-1/",
      "/preview/missing.json",
      "/preview/%E0%A4%A.json",
      "/other/manifest.json",
    ]) {
      expect(resolvePreviewFile(root, path), path).toBeNull();
    }
  });
});

interface Sent {
  status: number;
  body: string;
  headers: Record<string, string>;
}

/** 送一個請求給中介層；`next` 被呼叫時回 "next"。回傳的 promise 等到回應結束才完成。 */
function request(handler: ReturnType<typeof demoPreviewMiddleware>, url: string): Promise<Sent | "next"> {
  return new Promise((resolve) => {
    const headers: Record<string, string> = {};
    const response = {
      statusCode: 200,
      setHeader(name: string, value: string) {
        headers[name.toLowerCase()] = value;
      },
      end(body?: string | Buffer) {
        resolve({ status: this.statusCode, body: body === undefined ? "" : String(body), headers });
      },
    };
    handler({ url } as IncomingMessage, response as unknown as ServerResponse, () => resolve("next"));
  });
}

describe("demoPreviewMiddleware", () => {
  it("serves preview files without caching and leaves other paths to the next handler", async () => {
    const handler = demoPreviewMiddleware(root);
    const sent = await request(handler, "/preview/2-1/titles.json");
    expect(sent).toMatchObject({ status: 200, body: '{"titles":[]}', headers: { "cache-control": "no-store" } });
    expect(await request(handler, "/preview/missing.json")).toMatchObject({ status: 404 });
    expect(await request(handler, "/preview/link.json")).toMatchObject({ status: 404 });
    // 網址裡的「..」先被網址規則化掉，變成資料夾外的路徑：不歸這裡管（交給下一層，示範站回網頁本身）。
    expect(await request(handler, "/preview/../outside.json")).toBe("next");
    expect(await request(handler, "/papers?title=x")).toBe("next");
  });

  it("holds every file but the manifest while loading, and releases them when switched back", async () => {
    const handler = demoPreviewMiddleware(root);
    expect(await request(handler, "/__demo-preview?state=loading")).toMatchObject({ body: "demo preview state: loading\n" });
    expect(await request(handler, "/preview/manifest.json")).toMatchObject({ status: 200 });
    let done = false;
    const held = request(handler, "/preview/2-1/titles.json").then((sent) => {
      done = true;
      return sent;
    });
    await new Promise((wait) => setTimeout(wait, 20));
    expect(done).toBe(false);
    await request(handler, "/__demo-preview?state=ok");
    expect(await held).toMatchObject({ status: 200, body: '{"titles":[]}' });
  });

  it("answers 500 for every file but the manifest while in the error state; ignores unknown states", async () => {
    const handler = demoPreviewMiddleware(root);
    await request(handler, "/__demo-preview?state=error");
    expect(await request(handler, "/preview/manifest.json")).toMatchObject({ status: 200 });
    expect(await request(handler, "/preview/2-1/titles.json")).toMatchObject({ status: 500, body: "" });
    expect(await request(handler, "/__demo-preview?state=broken")).toMatchObject({ body: "demo preview state: error\n" });
    await request(handler, "/__demo-preview?state=ok");
    expect(await request(handler, "/preview/2-1/titles.json")).toMatchObject({ status: 200 });
  });
});
