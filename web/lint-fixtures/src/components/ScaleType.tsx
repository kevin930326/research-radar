// 照規則寫的尺度（ADR-0038）：字級、行高、字距自由搭配，含斷點、行高修飾與角色加尺度；不能被擋。
export function ScaleType({ label }: { label: string }) {
  return (
    <div className="text-s15 leading-185 tracking-0 split:text-s16 max-split:leading-170">
      <p className="text-s68 leading-100 tracking-n001 hover:tracking-012">{label}</p>
      <p className="text-s11 leading-190 tracking-0005 text-s15/185">{label}</p>
      <p className="text-section-title-zh leading-150 max-split:text-s21">{label}</p>
    </div>
  );
}
