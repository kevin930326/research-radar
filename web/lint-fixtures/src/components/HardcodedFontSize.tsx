// 故意違反：寫死的字級（ADR-0037）。
const SIZE = "1.2rem";

export function HardcodedFontSize({ label }: { label: string }) {
  return (
    <div>
      <p className="text-[13px] text-text-primary">{label}</p>
      <p className="[font-size:13px]">{label}</p>
      <p style={{ fontSize: 13 }}>{label}</p>
      <p style={{ fontSize: SIZE }}>{label}</p>
      <p style={{ font: "13px serif" }}>{label}</p>
      <p className="leading-[1.2]">{label}</p>
      <p className="tracking-[0.1em]">{label}</p>
      <p className="font-[600]">{label}</p>
      <svg viewBox="0 0 10 10" aria-hidden="true">
        <text fontSize="13">{label}</text>
      </svg>
    </div>
  );
}

export const INLINE_CSS = "font-size: 13px";
