// 站內頁面清單（AR-67、UX「無障礙底線」）：站內連結檢查與 axe 都照這一份跑，
// 新頁面加進來就自動被檢查。每個做出新頁面的 Story 都要把它加進這裡。
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { paperId } from "./api-mock.ts";
import { DEMO_URL, DEV_URL, MEMBER_URL } from "./sites.ts";

export interface Site {
  name: "member" | "demo" | "dev";
  baseURL: string;
  /** 這個站實際存在的頁面。 */
  pages: string[];
}

/**
 * 論文頁（Story 3.8）：假資料（fixtures/papers.json）裡版面不同的幾篇。三塊都有內容（含開放取用版本）、
 * 沒有摘要（多一個到出版社頁面的連結）、三塊都沒成功、沒有 DOI（沒有「開啟全文」）。
 */
export const PAPER_PAGES = ["normal", "no-abstract", "all-failed", "no-doi"].map((label) => `/papers/${paperId(label)}`);

const DEMO_FIXTURE = fileURLToPath(new URL("./fixtures/demo-preview/", import.meta.url));
const DEMO_EXPORT = (JSON.parse(readFileSync(`${DEMO_FIXTURE}manifest.json`, "utf-8")) as { export_id: string }).export_id;
const DEMO_SHARD = (
  JSON.parse(readFileSync(`${DEMO_FIXTURE}${DEMO_EXPORT}/papers/000.json`, "utf-8")) as {
    papers: { biblio: { biblio: { id: string; title: string } } }[];
  }
).papers;

/** 示範站範例匯出（fixtures/demo-preview）裡標題開頭是「Fake Paper N:」的那一篇的 ID。 */
export function demoPaperId(number: number): string {
  const found = DEMO_SHARD.find((paper) => paper.biblio.biblio.title.startsWith(`Fake Paper ${number}:`));
  if (found === undefined) throw new Error(`no demo Fake Paper ${number}`);
  return found.biblio.biblio.id;
}

/**
 * 示範站的論文頁（Story 3.9）：範例匯出裡版面不同的幾篇。有完整內容（1）、授權允許但沒有摘要（2）、只有書目（3）、
 * 中文重點沒有產生成功（12）、示範站可用但摘要不公開、分類是 null（9，沒有設計稿）。
 */
export const DEMO_PAPER_PAGES = [1, 2, 3, 12, 9].map((number) => `/papers/${demoPaperId(number)}`);

/** 一定不存在的網址：用來檢查「找不到這一頁」。 */
export const MISSING_PATH = "/no/such/page";

export const SITES: Site[] = [
  // /login 是登入頁（Story 3.5），不用登入；其他頁要登入（測試裡由 api-mock.ts 回「已登入」）。
  // /search 是找論文（Story 3.14）；/ 導到 /search。/library 是我的文獻庫（Story 3.17）。
  // /coverage 是資料涵蓋範圍（Story 3.15，兩站都有）。
  { name: "member", baseURL: MEMBER_URL, pages: ["/search", "/library", "/notice", "/coverage", "/login", ...PAPER_PAGES] },
  // 示範站：/papers 是論文清單（Story 3.14 示範站，讀 fixtures/demo-preview 的範例匯出）；/papers/<ID> 是論文頁
  // （Story 3.9，上面幾篇）。首頁（Epic 13）還沒做，其他網址是「找不到這一頁」。
  { name: "demo", baseURL: DEMO_URL, pages: ["/papers", "/coverage", ...DEMO_PAPER_PAGES] },
];

/**
 * 開發伺服器上的元件示範頁（Story 3.2）。開發伺服器沒有 CSP（熱更新要用 inline 程式），
 * 而 security.spec.ts 對 SITES 每一站驗 CSP，所以它不放進 SITES；站內連結檢查也只查正式建置。
 */
export const DEV_SITE: Site = { name: "dev", baseURL: DEV_URL, pages: ["/dev/components"] };

/** 無障礙底線（a11y-floor.spec.ts）跑的頁面：正式建置的每一頁，加上元件示範頁。 */
export const A11Y_SITES: Site[] = [...SITES, DEV_SITE];
