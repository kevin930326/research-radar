// 成員版的資料來源：呼叫同網域的後端 API。
import type { DataSource } from "../types";

/** 後端 API 的基底路徑（Conventions「API」）。 */
export const API_BASE = "/api/v1";

const impl = {
  siteKind: () => "member" as const,
  // 之後的請求函式會用到；先掛在實作物件上，成員版的建置結果才會帶著它，
  // e2e 用它確認示範站的建置結果不含 API 程式。
  apiBase: API_BASE,
};

export const datasource: DataSource = impl;
