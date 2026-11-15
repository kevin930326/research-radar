// 故意違反：DESIGN.md 以外的斷點（任意值斷點、自訂 @media／@container）與 dark:（只有深色主題）；
// 這些都會產生 CSS，由建置檢查 tailwind-guard.ts 擋（ADR-0010 第 15 點、ADR-0030）。
export function ForbiddenVariants({ label }: { label: string }) {
  return (
    <div className="min-[1024px]:flex max-[600px]:hidden">
      <p className="[@media(min-width:900px)]:flex [@container(min-width:400px)]:flex">{label}</p>
      <p className="@min-[400px]:flex dark:bg-background">{label}</p>
    </div>
  );
}
