// 建置目標（AD-21、ADR-0010 第 3 點）：成員版 member 或示範站 demo。
// vite.config.ts 與它的單元測試共用這一份判斷。

export type BuildTarget = "member" | "demo";

const TARGETS: readonly BuildTarget[] = ["member", "demo"];

/** 每種建置的輸出資料夾。 */
export const OUT_DIRS: Record<BuildTarget, string> = {
  member: "dist",
  demo: "dist-demo",
};

/**
 * 讀 `VITE_BUILD_TARGET`。建置（`vite build`）一定要明講是哪一種；開發伺服器與測試沒給時用 member。
 * 給了但不是 member／demo 一律失敗，不默默改用預設值。
 */
export function resolveBuildTarget(raw: string | undefined, isBuild: boolean): BuildTarget {
  if (raw === undefined || raw === "") {
    if (isBuild) {
      throw new Error("VITE_BUILD_TARGET 必須是 member 或 demo（建置時一定要指定）");
    }
    return "member";
  }
  if ((TARGETS as readonly string[]).includes(raw)) {
    return raw as BuildTarget;
  }
  throw new Error(`VITE_BUILD_TARGET 只接受 member 或 demo，收到：${raw}`);
}
