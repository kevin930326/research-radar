// 示範站的資料來源：讀跟著網站一起部署的靜態 JSON（`preview/`，Story 3.7 匯出）。
// 示範站沒有登入（ADR-0010 第 2 點：只有成員版才有的功能，static 不實作）。
import type { DataSource } from "../types";
import { loadDemoCoverage, loadDemoManifest, loadDemoPaper, loadDemoPaperIndex } from "./preview";

export const datasource: DataSource = {
  siteKind: () => "demo",
  session: null,
  // 成員版的論文頁與找論文（API）；示範站的論文頁與論文清單讀 preview/（下面的 demoPapers）。
  papers: null,
  // 示範站沒有個人的文獻庫（State Patterns「全站共用」：示範站的後端功能不存在）。
  library: null,
  // 示範站的論文清單（Story 3.14 示範站）：篩選在瀏覽器裡做，讀 preview/ 的索引與標題。
  // 論文頁（Story 3.9）：再讀這一篇所在的分片。
  demoPapers: {
    manifest: () => loadDemoManifest(),
    index: (manifest) => loadDemoPaperIndex(manifest),
    paper: (manifest, index, paperId) => loadDemoPaper(manifest, index, paperId),
    // 資料涵蓋範圍（Story 3.15）：計數方塊與分類表。
    coverage: (manifest) => loadDemoCoverage(manifest),
  },
};
