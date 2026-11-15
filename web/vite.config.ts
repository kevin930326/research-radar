/// <reference types="vitest/config" />
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

import { OUT_DIRS, resolveBuildTarget } from "./build-target.ts";
import { tailwindGuard } from "./tailwind-guard.ts";

const here = (relative: string): string => fileURLToPath(new URL(relative, import.meta.url));

// 全站安全標頭只有一份來源（ADR-0029）：api 與 vite preview 都讀這個檔。
const SECURITY_HEADERS_PATH = here("../radar/entrypoints/api/security_headers.json");

function securityHeaders(): Record<string, string> {
  return JSON.parse(readFileSync(SECURITY_HEADERS_PATH, "utf-8")) as Record<string, string>;
}

export default defineConfig(({ command, isPreview }) => {
  const target = resolveBuildTarget(process.env.VITE_BUILD_TARGET, command === "build");
  return {
    // tailwindGuard：用到不產生 CSS 的類別（例如 bg-red-500）時建置與開發伺服器失敗（ADR-0030）。
    plugins: [tailwindGuard(), react(), tailwindcss()],
    resolve: {
      alias: {
        // 資料來源層（ADR-0010 第 1～3 點）：頁面只 import src/datasource（入口），
        // 入口再經這個別名拿到這一種建置的實作；另一種實作不會進建置結果。
        "#datasource-impl": here(`./src/datasource/${target === "member" ? "api" : "static"}/index.ts`),
        // 路由表也照建置目標選：示範站的路由表不含成員版的頁面。
        "#route-tree": here(`./src/routes/${target}.tsx`),
      },
    },
    build: {
      outDir: OUT_DIRS[target],
      emptyOutDir: true,
      // 不把小檔案內嵌成 data: 網址：CSP 不允許（Vite 8 預設 4096 bytes，0 是關掉）。
      assetsInlineLimit: 0,
    },
    preview: {
      host: "127.0.0.1",
      // vite preview 才需要；開發伺服器不帶 CSP（Vite 的熱更新要用到 inline 的東西）。
      headers: isPreview ? securityHeaders() : undefined,
    },
    test: {
      include: ["src/**/*.test.{ts,tsx}", "*.test.ts"],
      environment: "node",
    },
  };
});
