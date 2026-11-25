// 示範站的開發伺服器與 vite preview 送出 `preview/` 的檔（Story 3.14 示範站）：正式環境的 `preview/` 由匯出
// （radar export-demo，ADR-0061）寫在網站旁邊；本機看畫面、跑 e2e 時讀 repo 裡的範例（e2e/fixtures/demo-preview/，
// 自己編的假論文），或 `DEMO_PREVIEW_DIR` 指的資料夾。只掛在伺服器上，建置結果（dist-demo）不含這些檔。
//
// 看各種狀態（只有這兩個伺服器有；打開或 curl 之後重新整理 /papers）：
//   /__demo-preview?state=loading   manifest.json 照常回，之後讀 preview/ 其他檔的請求都先停著，切回 ok 才回
//                                   （看「載入中」的骨架；資料截至哪一天已經顯示，和設計稿一樣）
//   /__demo-preview?state=error     manifest.json 照常回，其他檔都回 500（看「沒有載入成功」）
//   /__demo-preview?state=ok        回到正常
// manifest.json 本身讀不到的樣子（沒有資料日期）不在這裡切，e2e 用 page.route 測。
// 只送 `.json`，路徑不能跳出資料夾（`..`、絕對路徑、符號連結指到外面都回 404）。
import { readFileSync, realpathSync, statSync } from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";
import type { IncomingMessage, ServerResponse } from "node:http";

import type { Plugin } from "vite";

export type PreviewState = "ok" | "loading" | "error";

const STATES: readonly PreviewState[] = ["ok", "loading", "error"];
const PREFIX = "/preview/";
const SWITCH = "/__demo-preview";

/** `preview/` 底下的網址路徑 → 資料夾裡的檔；不是 .json、跳出資料夾、不是檔案就是 null。 */
export function resolvePreviewFile(root: string, urlPath: string): string | null {
  if (!urlPath.startsWith(PREFIX)) return null;
  let relativePath: string;
  try {
    relativePath = decodeURIComponent(urlPath.slice(PREFIX.length));
  } catch {
    return null;
  }
  if (!relativePath.endsWith(".json") || relativePath.includes("\0") || isAbsolute(relativePath)) return null;
  let base: string;
  let file: string;
  try {
    base = realpathSync(root);
    file = realpathSync(resolve(base, relativePath));
  } catch {
    return null;
  }
  const inside = relative(base, file);
  if (inside === "" || inside.startsWith("..") || isAbsolute(inside) || inside.split(sep).includes("..")) return null;
  return statSync(file).isFile() ? file : null;
}

type Next = () => void;

/** 伺服器的中介層：切換狀態、送出 preview/ 的檔。 */
export function demoPreviewMiddleware(root: string) {
  let state: PreviewState = "ok";
  let waiting: (() => void)[] = [];
  const release = () => {
    const pending = waiting;
    waiting = [];
    for (const go of pending) go();
  };
  return (request: IncomingMessage, response: ServerResponse, next: Next): void => {
    const url = new URL(request.url ?? "/", "http://localhost");
    if (url.pathname === SWITCH) {
      const asked = url.searchParams.get("state");
      if (asked !== null && (STATES as readonly string[]).includes(asked)) {
        state = asked as PreviewState;
        if (state !== "loading") release();
      }
      response.setHeader("Content-Type", "text/plain; charset=utf-8");
      response.end(`demo preview state: ${state}\n`);
      return;
    }
    if (!url.pathname.startsWith(PREFIX)) {
      next();
      return;
    }
    // manifest.json 不受狀態影響（見檔頭）。
    const affected = url.pathname !== `${PREFIX}manifest.json`;
    const send = () => {
      if (state === "error" && affected) {
        response.statusCode = 500;
        response.end();
        return;
      }
      const file = resolvePreviewFile(root, url.pathname);
      if (file === null) {
        response.statusCode = 404;
        response.end();
        return;
      }
      response.setHeader("Content-Type", "application/json; charset=utf-8");
      response.setHeader("Cache-Control", "no-store");
      response.end(readFileSync(file));
    };
    if (state === "loading" && affected) {
      waiting.push(send);
      return;
    }
    send();
  };
}

/** Vite 外掛：示範站的開發伺服器與 vite preview 送出 preview/（成員版不掛）。 */
export function demoPreviewFiles(root: string): Plugin {
  return {
    name: "radar-demo-preview-files",
    configureServer(server) {
      server.middlewares.use(demoPreviewMiddleware(root));
    },
    configurePreviewServer(server) {
      server.middlewares.use(demoPreviewMiddleware(root));
    },
  };
}
