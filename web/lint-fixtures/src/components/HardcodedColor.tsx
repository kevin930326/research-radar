// 故意違反：寫死的色碼、色彩函式與顏色名稱（ADR-0030）。
export function HardcodedColor() {
  return (
    <svg viewBox="0 0 10 10" aria-hidden="true">
      <circle cx="5" cy="5" r="4" fill="#e3b466" />
      <rect width="2" height="2" style={{ color: "rgb(227, 180, 102)", backgroundColor: "hsl(38 70% 64%)" }} />
      <rect width="2" height="2" className="bg-[red]" />
      <rect width="2" height="2" className="hover:fill-[white]" />
      <rect width="2" height="2" className="border-x-[black]" />
      <rect width="2" height="2" style={{ color: "red", borderColor: "white" }} />
      <rect width="2" height="2" fill="white" />
      <rect width="2" height="2" stroke="black" />
    </svg>
  );
}
