// 站內頁面清單（AR-67、UX「無障礙底線」）：站內連結檢查與 axe 都照這一份跑，
// 新頁面加進來就自動被檢查。每個做出新頁面的 Story 都要把它加進這裡。
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

/** 一定不存在的網址：用來檢查「找不到這一頁」。 */
export const MISSING_PATH = "/no/such/page";

export const SITES: Site[] = [
  // /login 是登入頁（Story 3.5），不用登入；其他頁要登入（測試裡由 api-mock.ts 回「已登入」）。
  // /search 是找論文（Story 3.14）；/ 導到 /search。/library 是我的文獻庫（Story 3.17）。
  { name: "member", baseURL: MEMBER_URL, pages: ["/search", "/library", "/notice", "/login", ...PAPER_PAGES] },
  // 示範站：/papers 是論文清單（Story 3.14 示範站，讀 fixtures/demo-preview 的範例匯出）；首頁（Epic 13）、
  // 論文頁（Story 3.9）還沒做，其他網址是「找不到這一頁」。
  { name: "demo", baseURL: DEMO_URL, pages: ["/papers"] },
];

/**
 * 開發伺服器上的元件示範頁（Story 3.2）。開發伺服器沒有 CSP（熱更新要用 inline 程式），
 * 而 security.spec.ts 對 SITES 每一站驗 CSP，所以它不放進 SITES；站內連結檢查也只查正式建置。
 */
export const DEV_SITE: Site = { name: "dev", baseURL: DEV_URL, pages: ["/dev/components"] };

/** 無障礙底線（a11y-floor.spec.ts）跑的頁面：正式建置的每一頁，加上元件示範頁。 */
export const A11Y_SITES: Site[] = [...SITES, DEV_SITE];
