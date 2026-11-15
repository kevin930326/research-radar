// 照規則寫的樣式：代幣類別、var(--color-…)、版面寬度的任意值都不能被擋。
// 樣板字串、i18n 鍵、網址、英文句子、元件的英文屬性值也不能被建置檢查誤擋。
const WIDTH = 40;

export function AllowedStyles({ wide, label }: { wide: boolean; label: string }) {
  return (
    <section className="w-[40%] max-w-[320px] split:grid-cols-[240px_minmax(0,680px)] gap-x-[40px]">
      <p className={`text-body text-text-primary ${wide ? "split:mb-4" : "max-split:mb-4"}`}>{label}</p>
      <p style={{ color: "var(--color-accent-gold)", width: `${WIDTH}%` }} data-key="notice:notice.page.title">
        {label}
      </p>
      <a href="https://openalex.org/#works" data-side="bottom" data-align="start">
        {label}
      </a>
      <span title="Research Radar tracks journals">{label}</span>
      <svg viewBox="0 0 10 10" aria-hidden="true" fill="none">
        <circle cx="5" cy="5" r="4" fill="currentColor" stroke="currentColor" style={{ color: "inherit" }} />
      </svg>
    </section>
  );
}
