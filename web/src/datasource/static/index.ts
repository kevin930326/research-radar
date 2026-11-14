// 示範站的資料來源：讀跟著網站一起部署的靜態 JSON（Epic 3 之後的 Story 才有資料檔）。
import type { DataSource } from "../types";

export const datasource: DataSource = {
  siteKind: () => "demo",
};
