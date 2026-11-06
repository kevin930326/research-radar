## 6. 結論

**一句話：OpenAlex 對 10 本非 Elsevier 期刊的摘要覆蓋率很好（93%），但 4 本 Elsevier 期刊只有 14%，這 4 本是 OQ-02 真正的風險所在。**

| 分組 | 期刊 | 論文數 | 有摘要 | 覆蓋率 |
|---|---|---:|---:|---:|
| 非 Elsevier（10 本） | MISQ、ISR、JMIS、JAIS、EJIS、ISJ、JIT、PMJ、TEM、DATABASE | 7,070 | 6,573 | 93% |
| Elsevier（4 本） | JSIS、DSS、I&M、I&O | 2,573 | 367 | 14% |
| 全部 | 14 本 | 9,643 | 6,940 | 72% |

觀察：

1. **Elsevier 缺口是系統性的，不是個別期刊的問題。** 4 本 Elsevier 期刊每一年都偏低，抽樣看到的缺摘要論文都是正式研究論文。這和 Elsevier 在 2022 年底要求 OpenAlex 撤下摘要的情況相符。近 3 年（2023–2025）略有回升（DSS 14–23%、I&M 19–26%、JSIS 58–83%、I&O 43–72%），但仍不足以支撐分類與問答。
2. **非 Elsevier 期刊的缺口大多不是真的缺。** MISQ（88%）和 ISR（79%）缺摘要的條目，抽樣約一半是「Editor's Comments」「Research Spotlights」「About Our Authors」等非研究文章（OpenAlex 仍標為 article）。扣掉這些，研究論文的實際覆蓋率更高。PMJ 和 TEM 只有 2016–2017 年偏低（69–87%），之後都在 90% 以上。
3. **OpenAlex 的期刊歸屬有雜訊。** JAIS 底下掛了約 9,200 篇沒有 DOI 的 AIS 研討會論文（AMCIS、ICIS 等），不篩掉的話 JAIS 會從 680 篇膨脹成 9,898 篇。加上「必須有 DOI」的條件就能乾淨排除，對其他 13 本影響很小（0–19 篇）。
4. **資料量不大。** 14 本期刊 10 年合計約 9,600 篇，平均每年約 1,000 篇（TEM 近年每年 340–390 篇，是最大宗）。以這個量來看，第一次匯入 10 年在儲存和 LLM 處理量上都不是瓶頸。
5. **方法可信。** 14 本都用 ISSN 找到唯一一筆 source；`has_abstract` 篩選器與 `abstract_inverted_index` 實際內容的抽樣比對全部一致。

## 7. 對 PRD 範圍的建議

1. **第一次匯入維持 10 年（OQ-05 可以定案）。** 資料量約 9,600 篇，不構成負擔；覆蓋率的問題是「哪幾本期刊」，不是「哪幾年」，縮短年數幫助不大。
2. **14 本期刊照原計畫全部追蹤**（與先前「不縮減期刊」的決定一致），但 PRD 要明寫 Elsevier 4 本的降級處理：
   - 沒摘要的論文仍收進書目與趨勢統計（用標題＋關鍵字），標記「無摘要」，不做結構化筆記。
   - 介面和趨勢圖顯示每本期刊的摘要覆蓋率（brief 已列為原則），讓使用者知道 DSS、I&M 的趨勢只根據標題。
   - 重要論文靠使用者上傳 PDF 補。
3. **在寫 PRD 之前，建議再做一個小 spike（OQ-02b）**：只針對 Elsevier 4 本、約 2,200 篇缺摘要的論文，測 Semantic Scholar 和 Crossref 能補回多少。這決定「多來源互補」要不要列為第一版的必要功能；若補回不多，就直接接受降級處理，第二版再評估 Elsevier TDM API（前提是學校合約允許）。
4. **把以下資料品質規則寫進 PRD 的匯入需求：**
   - 期刊用 OpenAlex source ID 綁定（設定檔，不寫死在程式），並只收 `primary_location` 是該期刊、且有 DOI 的論文。
   - 排除非研究文章：OpenAlex 的 type 不夠準，需要再用標題規則（Editor's Comments、Research Spotlights、About Our Authors、Erratum 等）或 Crossref 的類型過濾。
   - 覆蓋率要定期重算並記錄日期，因為 OpenAlex 的資料會變動。

## 8. 待確認

- **DATABASE 名稱不符**：OpenAlex 名稱為「ACM SIGMIS Database the DATABASE for Advances in Information Systems」，兩個 ISSN 都完全吻合，判斷是同一本期刊（OpenAlex 把 ACM SIGMIS 的舊名併在一起），只是名稱比對規則沒有認出來。請確認後即可視為 OK。
