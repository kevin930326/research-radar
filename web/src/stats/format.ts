// 顯示位數只由這裡決定（AD-19、ADR-0011 規則 7）：統計函式回傳不四捨五入的比例，畫面要顯示時才經過這裡。
// 圖、表格、說明框的數字因此一致。驗收情境在 tests/golden/stats/format-percent.json（format.test.ts 讀）。

/**
 * 0 到 1 的比例 → 整數百分比的文字（例如 0.725 → "73%"）。
 * 四捨五入是「.5 進位」：先用 12 位有效數字消掉浮點誤差（0.575 × 100 在浮點是 57.49999…，要當成 57.5），
 * 再進位，所以 39 ÷ 40 是 "98%"、9 ÷ 24 是 "38%"。比例是 null（算不出來）時回 null，由畫面顯示「—」。
 */
export function formatPercent(ratio: number | null): string | null {
  if (ratio === null) return null;
  if (!Number.isFinite(ratio) || ratio < 0 || ratio > 1) throw new RangeError(`ratio out of range: ${ratio}`);
  const percent = Number((ratio * 100).toPrecision(12));
  return `${Math.floor(percent + 0.5)}%`;
}
