// 故意違反：Tailwind 預設類別與不存在的斷點（主題已清掉，不產生 CSS）；由建置檢查 tailwind-guard.ts 擋。
export function DefaultTailwindClass({ label }: { label: string }) {
  return (
    <div className="bg-red-500 lg:flex">
      <p className="m-0 text-xl">{label}</p>
    </div>
  );
}
