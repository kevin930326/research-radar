// 故意違反：拿掉或改寫焦點外框、用整組過場（ADR-0037、UX-DR7）。
export function FocusOutlineRemoved({ label }: { label: string }) {
  return (
    <div>
      <button className="outline-none">{label}</button>
      <button className="focus-visible:outline-0">{label}</button>
      <button className="outline-hidden">{label}</button>
      <button className="focus:outline-none">{label}</button>
      <button className="focus-visible:outline-2">{label}</button>
      <button className="transition-all duration-250">{label}</button>
      <button className="transition duration-250">{label}</button>
      <button className="hover:transition">{label}</button>
    </div>
  );
}
