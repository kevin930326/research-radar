// 故意違反：動態 import() 產生的檔（載入的一定是值），要被擋。
export async function loadGenerated() {
  return import("../generated/api");
}
