// 故意違反：寫死的行高與字距（ADR-0037）。
export function HardcodedLineHeight({ label }: { label: string }) {
  return (
    <div>
      <p className="text-body/[20px]">{label}</p>
      <p style={{ lineHeight: "20px" }}>{label}</p>
      <p style={{ letterSpacing: 2 }}>{label}</p>
      <svg viewBox="0 0 10 10" aria-hidden="true">
        <text letterSpacing="2">{label}</text>
      </svg>
      <p className="md:text-caption/(--leading)">{label}</p>
      <p className="text-body/none">{label}</p>
      <p className="text-caption/tight">{label}</p>
    </div>
  );
}
