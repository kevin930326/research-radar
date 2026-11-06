## 6. 結論

**一句話：OpenAlex 對 10 本非 Elsevier 期刊的摘要覆蓋率很好（93%），但 4 本 Elsevier 期刊只有 14%，這 4 本是 OQ-02 真正的風險所在。**

| 分組 | 期刊 | 論文數 | 有摘要 | 覆蓋率 |
|---|---|---:|---:|---:|
| 非 Elsevier（10 本） | MISQ、ISR、JMIS、JAIS、EJIS、ISJ、JIT、PMJ、TEM、DATABASE | 7,070 | 6,573 | 93% |
| Elsevier（4 本） | JSIS、DSS、I&M、I&O | 2,573 | 367 | 14% |
| 全部 | 14 本 | 9,643 | 6,940 | 72% |

觀察：

1. **Elsevier 缺口是系統性的，原因是出版社要求移除摘要。** 4 本 Elsevier 期刊每一年都偏低，抽樣看到的缺摘要論文都是正式研究論文。Elsevier 在 2024 年 11 月要求 OpenAlex 移除**非開放取用**論文的摘要，開放取用論文的摘要仍保留（來源：Bianca Kramer, ["More open abstracts?"](https://bmkramer.github.io/SesameOpenScience_site/thought/202411_open_abstracts/), 2024-11-24）。（另一個常被混淆的事件：2022 年 11 月撤下 OpenAlex 摘要的是 Springer Nature，不是 Elsevier。）

   本次資料完全符合這個說法：

   | Elsevier 4 本（2016–2025） | 論文數 | 有摘要 | 覆蓋率 |
   |---|---:|---:|---:|
   | 開放取用（`is_oa:true`） | 979 | 367 | 37% |
   | 非開放取用（`is_oa:false`） | 1,594 | 0 | 0% |

   各刊非開放取用的覆蓋率都是 0%（JSIS 0/92、DSS 0/718、I&M 0/713、I&O 0/71），有摘要的全部是開放取用論文。這也解釋了為什麼 JSIS、I&O 近年覆蓋率回升（JSIS 2024 年 83%、I&O 2025 年 72%）：近年開放取用的比例變高。但開放取用論文也只有 37% 有摘要，原因這次沒有查明（可能和 OpenAlex 對開放取用的認定範圍較寬有關）。整體來說仍不足以支撐分類與問答。
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
3. **在寫 PRD 之前，再做一個小 spike（OQ-02b，另開分支）**：只針對 Elsevier 4 本、約 2,200 篇缺摘要的論文，測 Semantic Scholar 能補回多少。這決定「多來源互補」要不要列為第一版的必要功能；若補回不多，就直接接受降級處理，第二版再評估 Elsevier TDM API（前提是學校合約允許）。
   - **不測 Crossref**：Elsevier 沒有把摘要提交給 Crossref，從 Crossref 補不回這 4 本的摘要。
4. **把以下資料品質規則寫進 PRD 的匯入需求：**
   - 期刊用 OpenAlex source ID 綁定（設定檔，不寫死在程式），並只收 `primary_location` 是該期刊、且有 DOI 的論文。
   - 排除非研究文章：OpenAlex 的 type 不夠準，需要再用標題規則（Editor's Comments、Research Spotlights、About Our Authors、Erratum 等）或 Crossref 的類型過濾。
   - 覆蓋率要定期重算並記錄日期，因為 OpenAlex 的資料會變動。

## 8. 已確認事項

- **DATABASE 名稱不符**：OpenAlex 名稱為「ACM SIGMIS Database the DATABASE for Advances in Information Systems」（S4210234096），兩個 ISSN 完全吻合。Kevin 已於 2026-11-06 確認是同一本期刊。
