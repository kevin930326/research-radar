// 全站共用狀態「載入中」：骨架畫面（DESIGN.md「骨架畫面」：髮線色的長條、不閃動）。
// 路由設定 defaultPendingMs 300，載入超過 0.3 秒才出現。
export function PageLoading() {
  return (
    <div aria-busy="true" data-state="loading" className="flex flex-col gap-6 py-6">
      <div aria-hidden="true" className="h-12 w-[40%] max-w-[320px] bg-hairline" />
      <div aria-hidden="true" className="flex max-w-reading-width flex-col gap-3">
        <div className="h-4 w-full bg-hairline" />
        <div className="h-4 w-full bg-hairline" />
        <div className="h-4 w-[60%] bg-hairline" />
      </div>
    </div>
  );
}
