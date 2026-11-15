// orval（ADR-0010 工具表、AD-21）：從 repo 根目錄的 openapi.json 產生前端的型別與呼叫函式，
// 輸出到 src/generated/（不手改；CI 重新產生後比對，有差異就失敗）。
// - client: "fetch"：只產生函式與型別，不產生 hooks（hooks 會把頁面綁死在 API 上，資料來源層就沒意義）。
// - 呼叫函式只有 src/datasource/ 能 import；頁面與元件只准 import type（eslint.config.js 擋）。
import { defineConfig } from "orval";

export default defineConfig({
  radar: {
    input: { target: "../openapi.json" },
    output: {
      target: "./src/generated/api.ts",
      schemas: "./src/generated/model",
      client: "fetch",
      mode: "single",
      clean: true,
    },
  },
});
