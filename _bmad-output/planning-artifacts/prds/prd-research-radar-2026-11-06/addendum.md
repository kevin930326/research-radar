---
title: PRD 補充：Research Radar
created: 2026-11-07
updated: 2026-11-10
---

# PRD 補充：Research Radar

這份文件收錄 PRD 放不下、但架構或 UX 階段會用到的細節。

目錄：架構階段輸入（待決事項、欄位建議、NFR 技術細節、ADR、MCP）→ 硬體與部署（分工、K8s）→ Must 工時重算 → 資料範圍基準 → 報告代號對照

## 架構階段輸入

### 待架構階段決定的事項

| 編號 | 事項 | 來源 |
|---|---|---|
| ARCH-1 | 如何辨識「同一位訪客」以計算每人每日問答上限（IP、session 等）。只有 S12 即時訪客問答需要，v1 靜態快照不需要 | 訪客示範站（S12） |
| ARCH-2 | 靜態快照示範站放在哪裡、多久更新一次（原 OQ-06「獨立站或訪客帳號」已由 K1 改為靜態快照，不再需要選） | 訪客示範站 |
| ARCH-3 | 構面關係網的資料 API 與呈現方式分開，2D 鄰居圖、3D 全景圖共用同一份資料 | 構面關係網 |
| ARCH-4 | 構面數量達數千個時的效能做法（例如伺服器端分群） | 構面關係網 |
| ARCH-5 | 突現偵測的關鍵字來源（OpenAlex、LLM 擷取、構面）與演算法分開、可替換；結果記錄來源與參數，方便重算與比較 | 趨勢分析 |
| ARCH-6 | 問答的檢索與語意搜尋共用同一套（使用者要求，不另做第二套）；架構階段設計共用的方式 | 問答、語意搜尋 |
| ARCH-7 | 登入方式：成員用既有帳號（例如 Google）登入、管理者只維護允許名單、系統盡量不保存密碼；具體機制待架構決定 | 帳號與可見範圍 |
| ARCH-8 | 構面合併除了名稱也比對構面定義（有定義時），參考 Larsen & Bong (2016, MISQ) 的 construct identity 做法；待決定的是具體比對方法 | 構面合併（FR-18） |
| ARCH-9 | 從 PDF 讀出 DOI／標題的方式（K5 縮小版後 v1 不用 GROBID，例如改從第一頁文字找 DOI）。推薦清單（FR-49）也用同一套，而且辨認完要確實刪除暫存檔 | PDF 上傳（FR-12、FR-49） |
| ARCH-10 | 全文版本（預印本／接受稿／正式版）的記錄方式，以及有正式版時如何優先 | 擁有權與授權（FR-7）、S13 |
| ARCH-11 | 正式環境用 Docker Compose（Azure for Students，1 GB）還是 Oracle 主機上的 k3s；Oracle staging 穩定數週後以 ADR 決定。若改用 Oracle，NFR-1 的記憶體限制要依新主機重訂，並留意 Oracle 的閒置回收規則 | K8s 部署（4.16）、NFR-1 |
| ARCH-12 | 資料庫（PostgreSQL＋pgvector）放在 K8s 叢集外還是叢集內（影響備份 FR-48 與 StatefulSet 的維運負擔） | K8s 部署（4.16） |
| ARCH-13 | 圖書館回覆不允許時，問答檢索如何依 `processing_mode` 過濾，讓付費 PDF 讀出的原文摘要不進入送給 Haiku 的內容。另外，本機算的向量和 Workers AI 的向量如何共存於同一個模型 ID 下 | FR-13 原文摘要、FR-5 |
| ARCH-14 | 追蹤範圍外論文的資料標記（例如 `in_scope` 旗標；要同時看期刊與年份，14 本追蹤期刊在追蹤年份以前的論文也屬範圍外，PRD R1），以及在趨勢、覆蓋率、關係排行表各查詢中一致排除的方式 | 推薦清單（FR-49）、引用擴展（8.3） |
| ARCH-15 | 讀取權限的強制機制：資料存取函式一律帶 `viewer` 參數＋架構測試（禁止 API 層直接存取 ORM），或 PostgreSQL row-level security；要能讓問答檢索、語意搜尋、匯出、示範站產生器都走同一套過濾 | 擁有權與授權（FR-7） |
| ARCH-16 | 正式主機只有 1 GB 時，PostgreSQL（pgvector）放在哪裡：同一台主機、Azure 託管的 PostgreSQL，或其他；影響 NFR-1 的 750 MB 能不能守住 | NFR-1、FR-48 |
| ARCH-17 | Azure 託管 PostgreSQL 的版本（決定能不能用內建 `uuidv7()`）、pgvector 的 halfvec 支援、預設備份保留期 | NFR-7、FR-5、FR-48 |
| ARCH-18 | CI 建立多架構映像（arm64 給 Oracle、amd64 給 Windows 桌機與 Azure）的方式 | NFR-18 |
| ARCH-19 | staging 給同學試用期間產生的資料（人工判斷事件、個人文獻庫）怎麼搬到正式環境：論文資料從 OpenAlex 重新匯入，人工判斷事件用 CLI 匯出／匯入 | 8.4 時程、FR-4 |

### 架構預留 R1–R7 與其他欄位建議（來源：報告第 5(a) 節、第 7 節第 3–9 點）

PRD 只定驗收條件；以下是報告建議的具體設計，架構階段可調整。

- R1：`derivation_run`（task、task_version、provider、model_id、prompt_ref、prompt_sha256、output_schema、params、git_sha、provider_batch_id、status、cost_usd、時間）；AI 表加 NOT NULL `run_id`、`input_kind`、`input_sha256`、`lang`、`status`（active／superseded／rejected）、`superseded_by`，部分唯一索引保證單一 active；Batch `custom_id` 用 `w{openalexId}_{task}_{promptVersion}`（≤64 字元）；`llm_calls` 帳本。
- R2：原始 JSON＋`fetched_at`、LLM 原始回應，以 sha256 為鍵存到 R2 儲存空間（或 jsonb）。
- R3：萃取 schema v1 的欄位定義見 PRD FR-3（含審稿 B-10 新增的題數、α、CR、AVE、量表題目位置）。
- R4：`correction`（workspace、user、paper_id、task、field_path、corrected_run_id、新舊值、reason、時間、review_status）；`merge_decision`（構面 A／B、建議 run、accept／reject、evidence_level、理由、決定者、時間）。
- R5：`embedding(item_type, item_id, model_id, input_sha256, run_id, vec)`，主鍵 (item_type, item_id, model_id)；`embedding_model` 登錄表；每模型一個 partial HNSW 索引；全文切塊上限 4,096 token；查詢一定要帶 `model_id` 條件，才用得到 partial 索引。
- R6：`external_id(entity_type, entity_id, scheme, value_norm, value_raw)`，`UNIQUE(scheme, value_norm)`；`identifiers.py` 也正規化 ISSN-L 與 OpenAlex ID。
- R7：`workspace_id`（書目資料不帶）、visibility enum、`policy.can_read(user, resource)`；PDF 欄位 `access_type`、`license`、`attested_at`、`processing_mode`；設定旗標 `PAYWALLED_FULLTEXT_LLM` 預設關閉；bronze OA 不視為可再利用。

其他報告項目：

- M01：`api_calls(endpoint, cost, ts)` 記錄每次 OpenAlex 呼叫花費的額度（`X-RateLimit-Credits-Used`）。
- S08：OpenAlex 的 `is_retracted` 只當作「待查」訊號，不直接標示撤稿。
- S16：證據理由標籤「受 GRADE 啟發」，不是照 GRADE 評等。

### 非功能需求的技術細節（報告第 5(b) 節）

PRD 的 NFR-7～NFR-17 只寫驗收條件，具體做法如下，架構階段可調整：

- UUIDv7：Azure PostgreSQL 若沒有 `uuidv7()`（PG18 才內建），在應用層產生。
- 設定檔：`journals.yaml`（OpenAlex source ID、ISSN-L）、版本化的分類表、`keyword_sources.yaml`、領域停用詞、`prompts/<task>/vN.md`、模型 ID 與價格表。
- 遷移：Alembic 命名慣例；回填寫成可續跑的 CLI 工作。
- API：commit `openapi.json` 並自動產生 TypeScript client；前端 react-i18next。
- 測試：錄製 cassette 並在 CI 封鎖網路；FakeProvider 加負面樣本；schema 體檢（AI 表必有 `run_id`，使用者表必有 `workspace_id`）。
- 日誌：結構化 JSON＋request ID；Docker 日誌大小上限；CI 跑 gitleaks。
- 稽核：`audit_event` 只記可見性變更、修正、合併、刪除與 allowlist 變更；刪除時複製到 `deleted_record`，不在每張表加 `deleted_at`。

### 待寫成 ADR 的架構決策（報告第 7 節第 22 點）

- VM 只做服務層；重運算（GROBID、重新嵌入、UMAP、本機模型擷取）用同一個 Docker 映像＋`radar` CLI 在 Kevin 的電腦跑。
- GitHub Actions 只做 CI；排程放 VM。
- 不引入 Redis、自架 Langfuse／Sentry、獨立向量資料庫、編排器；需要工作佇列時用 Procrastinate。
- 資料來源註冊表等第二個資料來源出現後再抽出來做；LLM 與嵌入的註冊表可以先做（已有真實實作與 Fake 兩種實作）。
- 模型選擇（含本機模型 vs Haiku 的評估方式）、部署拓撲、/goal 代理人規則、授權條款（MIT 或 Apache-2.0）。

### MCP 範圍（phase 2 Could，報告第 7 節第 23 點）

Python SDK v2 的 `MCPServer`；本機 stdio＋個人存取 token，或公開唯讀遠端；只暴露 search_papers、get_paper、find_construct、topic_trend、ask_library 等 3–6 個工具。遠端存取私有資料需要完整的 OAuth 2.1 資源伺服器，列為 Later。

## 硬體與部署

### 硬體與部署分工（2026-11-10，來源：pending notes）

| 機器 | 規格 | 用途 |
|---|---|---|
| Mac | 24 GB 記憶體 | 開發 |
| Windows 桌機 | RTX 3060（12 GB VRAM）、32 GB RAM、i5-12400F，長期開機 | 本機模型擷取付費全文、重運算批次（UMAP、主題模型、重新向量化）。比無風扇、記憶體頻寬低的 MacBook Air 更適合長時間批次；MoE 模型可把部分專家放在系統記憶體 |
| Oracle Cloud Always Free | VM.Standard.A1.Flex，2 OCPU／12 GB（Arm，單一主機） | K8s staging：已裝 k3s（停用 Traefik 與 ServiceLB，練 Ingress 時再開）；部署 PostgreSQL（pgvector）與 GROBID。穩定數週後以 ADR 評估是否當正式環境（ARCH-11） |
| Azure for Students | 1 GB（預定 2027-01 申請） | 正式環境預設方案（NFR-1） |

- Oracle 主機安全設定：SSH 只用金鑰、Security List 只開 22 號連接埠，對外走 Cloudflare Tunnel。
- Oracle 閒置回收：7 天內 CPU、網路、記憶體使用率都低於 20% 會被回收；正式環境選型時要納入考量。
- Arm 主機：CI 建立的 Docker 映像要同時支援 arm64（Oracle）與 amd64（Windows 桌機、Azure）。

### K8s 部署細節（4.16 Should）

- 第 1 階段的 Kustomize 物件：API Deployment、worker Deployment、每週更新 CronJob、migration Job、ConfigMap（非秘密設定）、Secret（API key 等）、Ingress。CI 用 kind 建臨時叢集跑冒煙測試。
- 第 2 階段：同一份 Kustomize 設定加 overlay 部署到 Oracle k3s staging。
- NFR-18 讓 v1 程式不用改就能搬上去：migration 與排程本來就是獨立的 `radar` CLI 指令（NFR-11），在 K8s 裡分別對應 Job 與 CronJob。

## Must 工時重算（2026-11-08 起，更新至 2026-11-10；來源：報告 6.3＋各次決定）

基準說明：起點「現有 Must 120／142／164」來自報告 §6.3，沒有逐功能明細；下表第一列 138／163／188 是它再加上 M01–M12 的淨增工時（+18／+21／+24）。FR-50 個人文獻庫在 brief（F5、F6）就有，原基準是否包含待核對。逐項工時在拆 Story 時估，重估規則見 PRD 8.5。

| 步驟 | 樂觀 | 中位 | 悲觀 |
|---|---|---|---|
| 報告：現有 Must＋M01–M12 | 138 | 163 | 188 |
| −K1、K2 | −13 | −10.5 | −8 |
| −K3（雷達動畫原本就不在 Must，省 0）〔推定〕 | 0 | 0 | 0 |
| K4 不採用 | 0 | 0 | 0 |
| ＋#11 單篇匯出、#12 每人上限、#13 版本欄位與 OA 連結、GIF 畫面 | +3 | +5 | +6.5 |
| −K7（提前採用） | −3 | −2.5 | −2 |
| −K5 縮小版（GROBID 延到 Should） | −6 | −5 | −4 |
| 2026-11-08 小計 | 約 119 | 約 150 | 約 181 |
| ＋FR-49／FR-50 個人文獻庫與推薦清單（D1、D5；子工作見下表）〔推定〕 | +5.5 | +8 | +11 |
| ＋審稿 #2 引用支持率評估（評估題與 LLM 評審 prompt）〔推定〕 | +1 | +1.5 | +2 |
| ＋審稿 #4 證據簡報前的成員同義詞確認（確認畫面並排定義與出處 2–4 h、成員確認標記＋CLI 複核清單 0.5–1 h）〔推定〕 | +2.5 | +3.75 | +5 |
| ＋審稿 #5 趨勢正規化（占比與切換 1–2 h、篩選期刊 1–1.5 h、期刊等權模式 1–1.5 h、兩組驗收測試資料 0.5 h）〔推定〕 | +3.5 | +4.5 | +5.5 |
| ＋審稿 #6 中文摘要評估與負面樣本測試（人工判定 1–2 h 另計入標註人力）〔推定〕 | +1 | +1.5 | +2 |
| ＋審稿 #7 權限驗收（換人測試＋CI 端點清單檢查 2–3 h、跨問答／搜尋／匯出／示範站／log 的金絲雀測試 2–3 h）〔推定〕 | +4 | +5 | +6 |
| ＋審稿 #8 示範站白名單欄位、示範站可用論文判定與測試〔推定〕 | +1 | +1.5 | +2 |
| ＋審稿 #11 匯入的追蹤範圍外論文預設私人（引用擴展補進的全實驗室可見，PRD R1）＋分享勾選框＋手動書目私人與未驗證標示〔推定〕 | +1 | +1.5 | +2 |
| 2026-11-10 審稿 Critical／High 後小計 | 約 140 | 約 177.5 | 約 217 |
| ＋審稿 B 堆（B-2 證據簡報與研究問題守門 1–1.5、B-3 修正衝突 1–1.5、B-4 信賴區間 0.5、B-5 門檻 0.5–1、B-6 NFR-19 量測 1–1.5、B-7 上傳限制 1.5–2、B-9 回報錯誤 1–1.5、B-10 量表欄位 0.5、B-13 問答隱私與刪除 1–1.5）〔推定〕 | +8 | +9.75 | +11.5 |
| **目前 Must（2026-11-10 定稿）** | **約 148** | **約 187** | **約 229** |
| −12 月垂直切片會完成的 Must 工作（11/19～12/20 約 60 h，扣規劃 15–25 h）〔推定，實測為準〕 | −40 | −35 | −30 |
| **寒假剩餘 Must** | **約 108** | **約 152** | **約 199** |
| ＋示範站首頁第一屏動畫（PRD R2，取代 K3；還沒併入上面的小計，拆 Story 時重估）〔粗估，沒有實測〕 | +6 | +8 | +10 |

### FR-49／FR-50 子工作

只算扣除 FR-12 重疊後的增量。2026-11-10 對抗式審稿指出，原先估的 +2.25 h 太低。

| 子工作 | 樂觀 | 悲觀 |
|---|---|---|
| 個人文獻庫表＋加入／移出＋列表頁 | 1.5 | 3 |
| 問答「個人」範圍過濾 | 0.5 | 1 |
| 下載聲明勾選＋未勾選路徑（辨認後刪檔，含刪檔測試） | 1 | 2 |
| 範圍外論文用單篇 DOI 向 OpenAlex 補書目與摘要 | 1 | 2 |
| 追蹤範圍外旗標，在趨勢、覆蓋率、排行表一致排除（含測試） | 1 | 2 |
| 換成我自己下載的版本 | 0.5 | 1 |

### 可能漏算的工作

拆 Story 時要核對；不計入上表總數〔推定〕。

| 工作 | 粗估 |
|---|---|
| 靜態快照產生器（FR-42，獨立建置流程） | 4–8 h |
| 前端各畫面（論文頁至少 8 個動作、上傳頁、排行表、趨勢頁等）是否已含在報告基準 | 待核對 |
| 金標準標註人力（摘要層級 50 篇每篇 5–10 分鐘、全文層級 20 篇針對性閱讀每篇 15–30 分鐘、試標 5 篇、中文摘要人工判定、分類專用 100 篇每篇 2–3 分鐘） | Kevin 約 14–26 h、同學約 1.5–2.5 h，不計入功能預算（PRD 8.4） |
| 構面合併審核人力（前 200 名＋長尾掛靠建議；建議筆數由 OQ-10 試擷取推估） | 待推估，屬 Kevin 的人工時間 |

### 剩下的槓桿

- 可用的槓桿：K6（3–5 h）、K8（4–6 h）、K9（4–6 h），另有可行性檢查點不通過時 FR-20 降級（2–3 h）。
- 2026-11-10 定稿重算（以寒假剩餘 Must 計）：中位 152 − (4＋5＋5) ≈ 138 h；悲觀 199 − (3＋4＋4) ≈ 188 h（槓桿取低值）。
- K6、K8 標為預期會觸發；觸發時依底線延後上線，不砍驗證。

### 不計入 Must 的工時

- 非功能預算要守住「最小」，避免和 Azure 部署互相排擠。M13 與第 5(b) 節的慣例另計非功能預算（約 9–11 h）；NFR-18 的 K8s-ready 慣例也計入非功能預算（約 2–4 h〔推定〕）。
- 審稿 #9：OA 全文管線（抓 TEI、解析、切塊、全文向量、全文擷取 prompt、頁碼）移到 Should 第一順位，和本機模型共用，估 6–10 h〔推定〕，不計入 Must。
- 從付費 PDF 讀原文摘要（D2）是 Should，搭 GROBID 約 +0.5–1 h，不計入 Must。

## 資料範圍基準（OQ-02，抓取日期 2026-11-06）

| 分組 | 期刊 | 論文數 | 有摘要 | 覆蓋率 |
|---|---|---|---|---|
| 非 Elsevier（10 本） | MISQ、ISR、JMIS、JAIS、EJIS、ISJ、JIT、PMJ、TEM、DATABASE | 7,070 | 6,573 | 93% |
| Elsevier（4 本） | JSIS、DSS、I&M、I&O | 2,573 | 367 | 14% |
| 全部 | 14 本 | 9,643 | 6,940 | 72% |

- Elsevier 缺口的原因：Elsevier 在 2024 年 11 月要求 OpenAlex 移除非開放取用論文的摘要（UX 寫覆蓋率說明時可用）。
- Crossref 不作為補 Elsevier 摘要的來源：Elsevier 沒有把摘要提交給 Crossref。

## 報告代號對照（來源：研究整理報告）

### Should

| 代號 | 內容 |
|---|---|
| S01 | AI 文獻驗證器 |
| S02 | 文獻整理表＋RIS／BibTeX／CSV 批次匯出（含證據層級欄） |
| S03 | RIS／BibTeX 匯入 |
| S04 | 中文查詢管線＋構面中英對照 |
| S05 | 參數化 SQL 工具回答計數題，並顯示算法 |
| S06 | 原生引用：`search_result` 區塊＋伺服器端驗證＋顯示原句 |
| S07 | 公開評估報告 |
| S08 | 撤稿警示 |
| S09 | 證據缺口地圖（構面×構面） |
| S10 | 交接包：網頁管理介面、LLM 關閉模式、每週自動還原演練、中文 runbook、完整匯出、外部 uptime 檢查 |
| S11 | 「我的點子落在哪？」 |
| S12 | 即時訪客問答 |
| S13 | 完整的合法免費版本清單＋NDLTD「台灣做過了嗎？」連結 |
| S14 | 方法＋AI 揭露一鍵匯出（PRISMA-S 欄位＋AI 使用紀錄） |
| S15 | 研究雷達期刊版（首頁的掃描動畫在 PRD R2 移到 FR-44，列入 Must） |
| S16 | 受 GRADE 啟發的證據理由標籤 |
| S17 | 合集與入門必讀 |
| S18 | 依合併證據層級篩選＋外部構面工具連結 |
| S19 | 工程證據包（Sentry、SLO、runbook＋RCA、ADR、負載測試、AI 協作說明、招募用 README） |
| S20 | AISeL OAI-PMH 研討會序列（若要清理 JAIS 誤歸則提前） |

### Could

| 代號 | 內容 |
|---|---|
| C01 | 3D：離線算圖的飛越影片；互動 3D（限時 8 h） |
| C02 | 離線 2D 地形圖（UMAP＋密度等高線＋自動標籤） |
| C03 | 引用分析，以及 VOSviewer／bibliometrix／Gephi 匯出 |
| C04 | BERTopic 新興主題 |
| C05 | 理論×情境、方法×主題矩陣 |
| C06 | 單向推送到實驗室 Zotero 群組 |
| C07 | 每週摘要（email／Discord）與 RSS |
| C08 | 題目登記與撞題提醒；國科會計畫用圖表匯出 |
| C09 | 學長姐論文脈絡（國圖開放資料、中大機構典藏） |
| C10 | 複製研究候選；假設地圖 |
| C11 | 訊號收件匣、IS Research Wrapped、時光縮影影片 |
| C12 | MCP（phase 2） |
| C13 | 用綠色 OA、TEI、CORE 補覆蓋；OpenCitations 補引用 |
| C14 | 用第二個領域包證明可以泛化 |

### Later

| 代號 | 內容 |
|---|---|
| L01 | 多實驗室與成員角色 |
| L02 | 個人化 mute／boost；遊戲化 |
| L03 | Zotero 雙向同步、瀏覽器擴充、PWA、LINE 推播 |
| L04 | DSPy 最佳化與微調 |
| L05 | 統合分析／MASEM |
| L06 | 遠端 MCP 存取私有資料、Terraform、自架 reranker |
