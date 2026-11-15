// 故意違反：尺度以外的字級、行高、字距（ADR-0038）；由建置檢查 tailwind-guard.ts 擋。
// 第 6 行不產生 CSS（DESIGN.md 的尺度沒有這些名稱）；第 7 行會產生 CSS，但不是尺度：
// 間距代幣當行高、負的字距、Tailwind 內建的行高名稱、CSS 變數當行高或字距。
export function OffScaleType({ label }: { label: string }) {
  return (
    <div className="text-s27 leading-999 tracking-005 text-s15/999 leading-1.7 tracking-0.02em">
      <p className="leading-4 leading-gutter-desktop -tracking-004 leading-none leading-(--x) split:tracking-(--x)">
        {label}
      </p>
    </div>
  );
}
