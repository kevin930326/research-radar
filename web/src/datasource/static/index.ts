// 示範站的資料來源：讀跟著網站一起部署的靜態 JSON（Epic 3 之後的 Story 才有資料檔）。
// 示範站沒有登入（ADR-0010 第 2 點：只有成員版才有的功能，static 不實作）。
import type { DataSource } from "../types";

export const datasource: DataSource = {
  siteKind: () => "demo",
  session: null,
  // 示範站的論文頁是 Story 3.9（讀 radar export-demo 匯出的靜態檔）。
  papers: null,
};
