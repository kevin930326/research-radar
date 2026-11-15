// 站內頁面清單（AR-67、UX「無障礙底線」）：站內連結檢查與 axe 都照這一份跑，
// 新頁面加進來就自動被檢查。每個做出新頁面的 Story 都要把它加進這裡。
import { DEMO_URL, MEMBER_URL } from "./sites.ts";

export interface Site {
  name: "member" | "demo";
  baseURL: string;
  /** 這個站實際存在的頁面。 */
  pages: string[];
}

/** 一定不存在的網址：用來檢查「找不到這一頁」。 */
export const MISSING_PATH = "/no/such/page";

export const SITES: Site[] = [
  { name: "member", baseURL: MEMBER_URL, pages: ["/notice"] },
  // 示範站首頁在 Epic 13、論文清單在 3.14 才有；目前沒有頁面，只檢查「找不到這一頁」。
  { name: "demo", baseURL: DEMO_URL, pages: [] },
];
