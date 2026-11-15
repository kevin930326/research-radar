// 故意違反：自己查「減少動態」，沒有用 src/hooks/use-reduced-motion.ts（ADR-0010 第 14 點）。
export function ReducedMotionQuery() {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  return <p data-reduced={String(reduced)} />;
}
