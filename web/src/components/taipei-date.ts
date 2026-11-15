// 時間在 API 一律是 ISO 8601 UTC，只有前端轉成台北時間（架構 API 慣例）。
// 時間來自 API（外部資料），格式壞掉時回 null，由呼叫端不顯示，不讓整頁壞掉。

const FORMATTER = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Taipei",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

/** UTC 的 ISO 時間 → 台北的日期 `YYYY-MM-DD`（例如 2026-11-11T17:30:00Z → 2026-11-12）；看不懂的時間回 null。 */
export function taipeiDate(iso: string): string | null {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  const parts = Object.fromEntries(FORMATTER.formatToParts(date).map((part) => [part.type, part.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}
