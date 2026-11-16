// 故意違反：頁面 import 產生的 API 呼叫函式（值），要被擋（ADR-0010 第 1 點、ADR-0047）。
import { me } from "../generated/api";

export function ImportGeneratedPage() {
  void me();
  return null;
}
