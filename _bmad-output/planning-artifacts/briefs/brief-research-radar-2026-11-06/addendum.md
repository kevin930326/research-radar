---
title: 產品簡介補充：Research Radar
created: 2026-11-06
updated: 2026-11-06
---

# 產品簡介補充：Research Radar

這份文件收錄 brief.md 放不下、但 PRD 與架構階段會用到的細節。原始來源：`docs/input/brief-input.md`。文中 F1～F8 對應 brief.md「解決方式」的第 1～8 項。

## 給 PRD 的功能細節

- **F2 上傳與去重**：從 PDF 讀出 DOI 或標題，自動補齊作者、期刊、年份；用 DOI 加檔案雜湊值去重，同一篇不重複存。中文論文（資管學報、臺灣博碩士論文）第一版只用上傳的方式納入，不自動追蹤。
- **F4 資料缺口**：趨勢圖要標出每本期刊、每年的摘要覆蓋率；只有標題和關鍵字的論文會計入論文數，但不參與需要摘要的分析。
- **F5 問答範圍**：個人文獻庫、共用文獻庫、所有追蹤期刊三選一。範例問題：「這個主題近年有哪些偏技術的方向」、「比較這幾篇的研究方法」。
- **F6 可見範圍**：書目與分類全實驗室共享；筆記與 PDF 預設私人，可選擇分享；問答紀錄只有本人看得到。
- **F8 個人 key**：加密存放、不寫進日誌、不傳回前端。

## 給架構階段的初步技術方向

以下僅為初步建議，在 bmad-architecture 階段逐一確認並寫成 ADR。

| 部分 | 建議 | 理由 |
| --- | --- | --- |
| 後端 | Python＋FastAPI | 抓論文、解析 PDF、串接 LLM 的工具多屬 Python 生態 |
| 資料庫 | PostgreSQL＋pgvector | 一般資料與向量放在同一個資料庫，一條查詢就能同時篩期刊、年份與語意相似度 |
| 背景工作 | 工作佇列＋排程器 | 定時抓論文、處理上傳 |
| 前端 | React | 沿用 PetVerse 經驗 |
| 檔案 | Cloudflare R2（免費 10 GB） | PDF 不放資料庫，只存路徑 |
| 部署 | Docker Compose | 本機與正式環境一致 |
| 品質 | 自動化測試＋GitHub Actions CI | 延續 AI Usage Monitor 的做法 |

- **混合架構**：Cloudflare Workers 每次執行只有 10 毫秒 CPU，不能拿來解析 PDF。因此 PDF 放 R2、向量化用 Workers AI 或開源模型（架構階段決定，見 OQ-08），FastAPI＋PostgreSQL 的核心放在自己的主機。如果全部改用 Cloudflare，技術會和 AI Usage Monitor 重疊，作品集就少了 Python 後端和 PostgreSQL。
- **GROBID**（PDF 解析）：要實測 1 GB 主機跑不跑得動。
- **主機**：正式環境預定用 Azure for Students（每年 100 美元、免信用卡），約 2027-01 申請。測試環境用家裡的 PC＋Cloudflare Tunnel。其他候選：Heroku 學生額度＋Neon。DigitalOcean 學生額度已在 2026-08 結束。
- **LLM 費用**：Console 為本專案開的工作區每月支出上限 30 美元；Max 附贈額度的餘額和同組織的校園小幫手共用。

## 可借鏡的專案（依功能）

- **F1**：arxiv-sanity-lite（只用 CPU 的推薦）、Litmaps（新論文寄信）、自己的校園小幫手（Discord 推播每週摘要）。
- **F2**：GROBID（PDF 解析）。
- **F3**：OpenAlex Topics（免費的基本分類，LLM 只做資管專屬的分類）、Elicit（「論文 × 欄位」表格）、TheoryOn／INN（構念關係）。
- **F4**：BERTopic／BERTrend（新興主題）、CiteSpace（突現偵測）、ASReview（主動學習篩選，PRD 再決定要不要納入）。
- **F5**：PaperQA2（檢索 → 重排 → 回答）、OpenScholar（檢查引用是否存在）、Consensus（支持比例；只是計票，要標明限制）、Stanford 研究（AI 點子新穎但可行性較弱）。
- **工程品質**：Langfuse＋Ragas（追蹤費用、量化 RAG 品質）。
- **第二版**：ResearchRabbit／Connected Papers（引用網路）、scite（引用立場）、TOEIC Cards 的 SM-2（論文複習卡）。

## 第一版時程細節

- 每天可投入約 4～5 小時，但 12/16 前大部分時間要留給 PetVerse 和課業，所以規劃期以每天約 1 小時估算。
- AI 寫程式很快，時間主要花在審查、測試和理解設計，每天都要留時間弄懂「為什麼這樣做」。
