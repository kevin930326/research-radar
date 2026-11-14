// 資料來源層的函式清單（AD-21、ADR-0010）：成員版（api）與示範站（static）兩種實作都要滿足它。
// 頁面與元件只能 import 入口（datasource/index.ts）與這個檔。

/** 這份建置是哪一個站：成員版或示範站。 */
export type SiteKind = "member" | "demo";

export interface DataSource {
  siteKind(): SiteKind;
}
