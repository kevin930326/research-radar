// 故意違反：任意值方括號裡與 style 的 CSS 顏色名稱（ADR-0030）。
export function ArbitraryColor() {
  return (
    <div>
      <p className="shadow-[0_0_4px_red]" />
      <p className="bg-[color:red]" />
      <p className="[color:red]" />
      <p className="hover:[background:white]" />
      <p style={{ color: "rebeccapurple" }} />
      <p className="border-[1px_solid_gainsboro]" />
    </div>
  );
}
