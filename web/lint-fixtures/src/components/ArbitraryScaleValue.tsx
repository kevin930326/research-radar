// 故意違反：有了尺度（ADR-0038）之後，任意的字級、行高、字距照樣不准（由 ESLint 擋）。
export function ArbitraryScaleValue({ label }: { label: string }) {
  return (
    <div>
      <p className="text-[15px]">{label}</p>
      <p className="text-s15 leading-[1.7]">{label}</p>
      <p className="split:tracking-[0.02em]">{label}</p>
      <p className="text-s15/[1.7]">{label}</p>
    </div>
  );
}
