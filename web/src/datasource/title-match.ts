// 「用標題裡的字篩選」的比對規則（AD-20：寫在資料來源層的一個函式；示範站論文清單 Story 3.14 用，
// 成員版之後如果也加這個條件，兩邊用同一個函式、通過同一批資料檔）。
// 字面比對，不是語意搜尋（EXPERIENCE.md 畫面 7）：先把全形與半形統一（Unicode NFKC）、不分大小寫，
// 字出現在標題的任何位置就算。前後的空白不算；只有空白等於沒有條件。

/** 比對前的寫法：NFKC（全形與半形統一）再轉小寫。 */
export function normalizeForTitleMatch(text: string): string {
  return text.normalize("NFKC").toLowerCase();
}

/** 標題裡有沒有這段字（`query` 去掉前後空白後是空的就算有）。 */
export function titleMatches(title: string, query: string): boolean {
  const needle = normalizeForTitleMatch(query.trim());
  if (needle === "") return true;
  return normalizeForTitleMatch(title).includes(needle);
}
