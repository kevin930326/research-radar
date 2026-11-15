// 照規則寫的樣式：代幣類別、var(--color-…)、版面寬度的任意值都不能被擋。
// 樣板字串、i18n 鍵、網址、英文句子、元件的英文屬性值（color="primary"、例外清單裡的 inline-start 等）、
// 非字級屬性的 em 長度也不能被誤擋。
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
      {/* 元件自己的 color 屬性（不是 CSS 顏色名稱）、非字級屬性的 em 長度、var() 的代幣色 */}
      <Chip color="primary" label={label} />
      <p style={{ width: "2em", marginTop: "0.5rem" }} className="bg-[var(--color-accent-gold)]">
        {label}
      </p>
      {/* 只過場要動的屬性 */}
      <p className="transition-transform transition-[border-color] duration-250">{label}</p>
      {/* 例外清單（tailwind-guard.ts 的 NOT_CLASSES）：元件屬性值與資料，不是類別 */}
      <p data-side="inline-start" data-placement="bottom-start" data-align="flex-start" data-direction="row-reverse">
        {label}
      </p>
      <p data-ease="ease-in" data-order="order-desc">
        {label}
      </p>
    </section>
  );
}

function Chip({ color, label }: { color: "primary" | "secondary"; label: string }) {
  return <span data-color={color}>{label}</span>;
}
