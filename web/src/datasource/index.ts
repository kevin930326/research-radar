// 資料來源層的入口：頁面與元件只從這裡拿資料。
// `#datasource-impl` 由 vite.config.ts 依建置目標指到 api/ 或 static/（型別檢查時指到 api/，
// 兩種實作都滿足同一個 DataSource 型別）。
import { datasource as impl } from "#datasource-impl";

import type { DataSource } from "./types";

export const datasource: DataSource = impl;
