# OQ-02 驗證：OpenAlex 對 14 本追蹤期刊的摘要覆蓋率

- 資料抓取日期：2026-11-06（OpenAlex 資料會更新，數字只代表當天）
- 範圍：2016–2025 年、OpenAlex type 為 `article` 或 `review`、有 DOI、且主要出處（primary_location）是該期刊的論文
- 為什麼要求有 DOI：OpenAlex 把大量 AIS 研討會論文（AMCIS、ICIS 等，沒有 DOI）錯掛到 JAIS，不加這個條件 JAIS 會變成 9,898 篇（實際約 680 篇）；其他 13 本加條件前後只差 0–19 篇
- 「有摘要」＝ `abstract_inverted_index` 不為空（用 OpenAlex 的 `has_abstract` 篩選器統計，並抽樣比對欄位內容）
- 程式：`spikes/oq-02/check_coverage.py`；原始數字：`spikes/oq-02/output/results.json`

## 1. 期刊對應（ISSN → OpenAlex source）

| 簡稱 | 期刊（預期） | 清單來源 | ISSN | OpenAlex 名稱 | source ID | 出版者 | 狀態 |
|---|---|---|---|---|---|---|---|
| MISQ | MIS Quarterly | AIS | 0276-7783, 2162-9730 | MIS Quarterly | S57293258 | MIS Quarterly | OK |
| ISR | Information Systems Research | AIS | 1047-7047, 1526-5536 | Information Systems Research | S202812398 | Institute for Operations Research and the Management Sciences | OK |
| JMIS | Journal of Management Information Systems | AIS | 0742-1222, 1557-928X | Journal of Management Information Systems | S9954729 | Taylor & Francis | OK |
| JAIS | Journal of the Association for Information Systems | AIS | 1536-9323 | Journal of the Association for Information Systems | S30879505 | Association for Information Systems | OK |
| EJIS | European Journal of Information Systems | AIS | 0960-085X, 1476-9344 | European Journal of Information Systems | S116712389 | Palgrave Macmillan | OK |
| ISJ | Information Systems Journal | AIS | 1350-1917, 1365-2575 | Information Systems Journal | S92260147 | Wiley | OK |
| JIT | Journal of Information Technology | AIS | 0268-3962, 1466-4437 | Journal of Information Technology | S135086714 | SAGE Publishing | OK |
| JSIS | Journal of Strategic Information Systems | AIS | 0963-8687, 1873-1198 | The Journal of Strategic Information Systems | S6144335 | Elsevier BV | OK |
| DSS | Decision Support Systems | AIS（2023 加入） | 0167-9236, 1873-5797 | Decision Support Systems | S11479521 | Elsevier BV | OK |
| I&M | Information & Management | AIS（2023 加入） | 0378-7206, 1872-7530 | Information & Management | S38883057 | Elsevier BV | OK |
| I&O | Information and Organization | AIS（2023 加入） | 1471-7727, 1873-7919 | Information and Organization | S35363033 | Elsevier BV | OK |
| PMJ | Project Management Journal | 王老師 | 8756-9728, 1938-9507 | Project Management Journal | S196205230 | SAGE Publishing | OK |
| TEM | IEEE Transactions on Engineering Management | 王老師 | 0018-9391, 1558-0040 | IEEE Transactions on Engineering Management | S154533451 | Institute of Electrical and Electronics Engineers | OK |
| DATABASE | The DATA BASE for Advances in Information Systems | 王老師 | 0095-0033, 1532-0936 | ACM SIGMIS Database the DATABASE for Advances in Information Systems | S4210234096 | Association for Computing Machinery | 名稱不符 |

**需要確認的期刊：**

- DATABASE（名稱不符）候選：
  - ACM SIGMIS Database the DATABASE for Advances in Information Systems｜S4210234096｜ISSN 0095-0033, 1532-0936, 2331-1584, 2331-1622｜works 1311

## 2. 總覽（10 年合計）

| 簡稱 | 論文數 | 有摘要 | 覆蓋率 | 最低年份覆蓋率 |
|---|---:|---:|---:|---:|
| MISQ | 702 | 621 | 88% | 80%（2018） |
| ISR | 894 | 702 | 79% | 61%（2017） |
| JMIS | 416 | 405 | 97% | 93%（2019） |
| JAIS | 680 | 671 | 99% | 94%（2024） |
| EJIS | 391 | 388 | 99% | 98%（2017） |
| ISJ | 455 | 444 | 98% | 91%（2025） |
| JIT | 210 | 201 | 96% | 89%（2022） |
| JSIS | 212 | 71 | 33% | 4%（2016） |
| DSS | 1176 | 128 | 11% | 2%（2016） |
| I&M | 1005 | 120 | 12% | 0%（2017） |
| I&O | 180 | 48 | 27% | 0%（2018） |
| PMJ | 471 | 431 | 92% | 69%（2016） |
| TEM | 2616 | 2475 | 95% | 71%（2016） |
| DATABASE | 235 | 235 | 100% | 100%（2016） |
| **合計** | **9643** | **6940** | **72%** | |

## 3. 每本期刊、每一年的論文數

| 簡稱 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| MISQ | 61 | 67 | 69 | 65 | 69 | 76 | 82 | 56 | 94 | 63 |
| ISR | 73 | 74 | 63 | 88 | 82 | 95 | 110 | 101 | 91 | 117 |
| JMIS | 41 | 42 | 41 | 43 | 42 | 44 | 40 | 41 | 41 | 41 |
| JAIS | 36 | 45 | 75 | 95 | 103 | 74 | 53 | 54 | 72 | 73 |
| EJIS | 18 | 46 | 36 | 19 | 63 | 56 | 50 | 38 | 33 | 32 |
| ISJ | 33 | 31 | 51 | 37 | 37 | 49 | 38 | 58 | 74 | 47 |
| JIT | 25 | 19 | 7 | 23 | 23 | 24 | 19 | 43 | 14 | 13 |
| JSIS | 26 | 26 | 22 | 15 | 21 | 19 | 16 | 19 | 24 | 24 |
| DSS | 116 | 118 | 104 | 122 | 121 | 122 | 92 | 126 | 141 | 114 |
| I&M | 97 | 69 | 102 | 88 | 94 | 105 | 97 | 104 | 116 | 133 |
| I&O | 11 | 16 | 17 | 19 | 11 | 21 | 17 | 20 | 23 | 25 |
| PMJ | 68 | 47 | 46 | 51 | 41 | 38 | 40 | 48 | 51 | 41 |
| TEM | 51 | 85 | 138 | 218 | 322 | 355 | 368 | 390 | 349 | 340 |
| DATABASE | 18 | 21 | 31 | 28 | 25 | 29 | 24 | 20 | 20 | 19 |

## 4. 每本期刊、每一年的摘要覆蓋率

| 簡稱 | 2016 | 2017 | 2018 | 2019 | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| MISQ | 84% | 85% | 80% | 83% | 88% | 82% | 95% | 96% | 93% | 98% |
| ISR | 68% | 61% | 63% | 70% | 72% | 83% | 77% | 90% | 90% | 93% |
| JMIS | 100% | 95% | 95% | 93% | 100% | 98% | 98% | 98% | 100% | 98% |
| JAIS | 100% | 100% | 97% | 100% | 99% | 100% | 100% | 98% | 94% | 99% |
| EJIS | 100% | 98% | 100% | 100% | 100% | 98% | 98% | 100% | 100% | 100% |
| ISJ | 100% | 100% | 100% | 95% | 100% | 100% | 100% | 100% | 93% | 91% |
| JIT | 92% | 100% | 100% | 91% | 91% | 100% | 89% | 98% | 100% | 100% |
| JSIS | 4% | 8% | 9% | 7% | 38% | 11% | 50% | 58% | 83% | 67% |
| DSS | 2% | 4% | 4% | 5% | 12% | 13% | 8% | 14% | 23% | 20% |
| I&M | 1% | 0% | 5% | 10% | 10% | 9% | 7% | 19% | 22% | 26% |
| I&O | 9% | 6% | 0% | 21% | 0% | 5% | 24% | 45% | 43% | 72% |
| PMJ | 69% | 87% | 96% | 98% | 98% | 95% | 95% | 96% | 96% | 98% |
| TEM | 71% | 79% | 80% | 90% | 96% | 95% | 94% | 99% | 100% | 99% |
| DATABASE | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |

## 5. 抽樣檢查（has_abstract 篩選器 vs. 欄位內容）

每本期刊各抽 25 篇 `has_abstract:true` 與 25 篇 `has_abstract:false`，數 `abstract_inverted_index` 實際不為空的篇數。

| 簡稱 | true 組：不為空／抽樣 | false 組：不為空／抽樣 | 一致 |
|---|---:|---:|---|
| MISQ | 25/25 | 0/25 | 是 |
| ISR | 25/25 | 0/25 | 是 |
| JMIS | 25/25 | 0/11 | 是 |
| JAIS | 25/25 | 0/9 | 是 |
| EJIS | 25/25 | 0/3 | 是 |
| ISJ | 25/25 | 0/11 | 是 |
| JIT | 25/25 | 0/9 | 是 |
| JSIS | 25/25 | 0/25 | 是 |
| DSS | 25/25 | 0/25 | 是 |
| I&M | 25/25 | 0/25 | 是 |
| I&O | 25/25 | 0/25 | 是 |
| PMJ | 25/25 | 0/25 | 是 |
| TEM | 25/25 | 0/25 | 是 |
| DATABASE | 25/25 | 0/0 | 是 |

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
