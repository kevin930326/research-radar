# 架構決策紀錄（ADR）

每份 ADR 記一個重要的技術決策：考慮過哪些選項、選了哪個、理由、放棄了什麼。決策改了不回頭改舊的，而是寫一份新的，並在舊的那份標明「已被 ADR-00NN 取代」。

整體的架構規則（各部分要一起遵守的不變量）在
`_bmad-output/planning-artifacts/architecture/architecture-research-radar-2026-11-12/ARCHITECTURE-SPINE.md`；這裡的 ADR 是那些規則背後的理由。

| 編號 | 決策 | 狀態 | 對應的架構規則 |
|---|---|---|---|
| [0001](0001-architecture-style.md) | 模組化單體＋分層，只有外部服務做成可替換（含 C1–C5 比較表） | 提議 | AD-1、AD-2 |
| [0002](0002-read-authorization-viewer.md) | 讀取權限用 viewer 參數＋架構測試，不用資料庫的列層級權限 | 提議 | AD-3、AD-4 |
| [0003](0003-deployment-topology.md) | 服務跑在 1 GB 主機，資料庫放在主機外面；備份、還原、換版 | 提議（資料庫位置已決定：Azure 託管） | AD-17、AD-28、AD-29 |
| [0004](0004-background-jobs-and-scheduling.md) | 背景工作用 Procrastinate，排程由外部 cron 呼叫 `radar` 指令 | 提議 | AD-14、AD-15 |
| [0005](0005-hybrid-retrieval.md) | 搜尋、找相似論文、問答共用一套檢索（向量＋關鍵字） | 提議 | AD-11、AD-12 |
| [0006](0006-qa-streaming-citation-verification.md) | 問答邊串流邊驗證出處；回答只存一種形狀 | 提議 | AD-13 |
| [0007](0007-demo-static-snapshot.md) | 示範站是白名單匯出的靜態檔，每週自動更新 | 提議 | AD-20 |
| [0008](0008-login-google-oidc.md) | 登入用 Google 帳號＋允許名單，登入狀態存在資料庫 | 提議 | AD-23 |
| [0009](0009-model-choice-and-llm-gateway.md) | 模型用 Claude Haiku 5.5，所有 LLM 呼叫只走一個閘門（含價格核對） | 提議 | AD-8、AD-9 |
| [0010](0010-frontend-stack.md) | 前端一份程式、兩種建置；頁面只透過資料來源層拿資料 | 提議 | AD-21、AD-22 |
| [0011](0011-snapshots-and-number-definitions.md) | 每個數字只有一個定義：計數在資料庫，比例在前端的一個模組；趨勢的年度規則；證據簡報的匯出檔 | 提議 | AD-5、AD-16、AD-19 |
| [0012](0012-construct-identity-and-merging.md) | 構面的身分、合併與關係的識別方式 | 提議 | AD-10、AD-16 |
| [0013](0013-pdf-upload-and-identification.md) | PDF 經由 API 上傳；沒有下載聲明的檔案讀完就刪 | 提議 | AD-24 |
| [0014](0014-raw-first-ingest-and-weekly-rescan.md) | 原始資料先存再解析；每週更新把 14 本期刊整本重掃 | 提議 | AD-25 |
| [0015](0015-ai-output-tables-and-processing-plan.md) | AI 產出怎麼存、怎麼換版；哪一種論文做哪些處理 | 提議 | AD-7、AD-30 |
| [0016](0016-keyword-stats-and-burst-detection.md) | 關鍵字統計只有一個來源；爆紅關鍵字當場算，空年從序列拿掉 | 提議（空年的算法、已經結束的爆紅怎麼列、兩個篇數的連結，Kevin 2026-11-13 已確認） | AD-5、AD-31 |
| [0017](0017-ci-pipeline.md) | CI：固定檢查依序跑、amd64／arm64 原生執行機各自冒煙、冒煙通過且是 push 事件才推 GHCR；測試預設封鎖網路 | 已接受（Kevin 2026-11-14） | AD-17、AD-18、AD-29 |
| [0018](0018-database-migrations-and-roles.md) | 資料庫遷移用 Alembic、所有遷移在一個交易裡；第一個遷移只檢查功能並給應用程式角色預設權限；本機與 CI 也建 `radar_owner`、`radar_app` 兩個角色；`/readyz` 沒遷移或落後回 503、比程式新照常就緒；連線數預算 | 已接受（Kevin 2026-11-14） | AD-15、AD-17、AD-18、AD-29 |
| [0019](0019-worker-and-queue-integration.md) | worker 接上 Procrastinate：佇列的 SQL 凍結成檔由 Alembic 執行；連線池自己開；監督迴圈處理訊號與重開；做完的工作紀錄全留；排程只在 `deploy/crontab` | 已接受（Kevin 2026-11-14） | AD-14、AD-15、AD-17、AD-18 |
| [0020](0020-identifiers-and-paper-writes.md) | 識別碼的正規化規則（ISSN 不驗檢查碼）；論文寫入先新增再認領識別碼、每個來源一欄抓取時間、識別碼衝突不合併；viewer、權限表與資料層的架構測試 | 已接受（Kevin 2026-11-14） | AD-3、AD-4、AD-5、AD-6、AD-25、AD-27 |
| [0021](0021-openalex-fetch-and-raw-storage.md) | OpenAlex 只篩期刊與年份、原始資料從回應原文切出每一篇的位元組；用量取本地加總與 OpenAlex 回報的較大者；去重看 `raw_fetch`、先存物件再寫列；領域設定 `config/*.yaml` 啟動時驗證 | 已接受（Kevin 2026-11-14） | AD-2、AD-18、AD-25、AD-29 |
| [0022](0022-parsing-and-reparse.md) | OpenAlex 的解析是純函式、看不懂整篇略過；指紋 `source_hash` 的欄位與序列化；摘要在寫入函式裡寫、來源不再提供就刪、優先順序先寫成常數；排除規則的格式與正規化；`radar reparse` 只更新已匯入的論文、先放暫存表再一個交易套用 | 已接受（Kevin 2026-11-14） | AD-5、AD-7、AD-25、AD-31 |
| [0023](0023-compose-file-at-repo-root.md) | 本機開發的 compose 檔放在 repo 根目錄 `compose.yaml`（`docker compose up` 不用加 `-f`）；`deploy/` 放 crontab、正式主機的 compose 等部署用的檔案 | 已接受（Kevin 2026-11-14） | AD-17 |

「提議」表示還沒經過 Kevin 確認；PR 合併後改成「已接受」。

2026-11-13 把設計階段定下來的事補進架構（PRD R5–R7、UX 文件「給架構階段的事項」）：新增 0016；內容有改的是 0004、0006、0007、0010、0011、0013、0014，各自在開頭的「更新」一行寫了改什麼。狀態都沒有動。0009、0015 的內容沒有改，但架構文件 AD-8 多了一句和它們有關的規定：輸入指紋只由一個函式算，門檻檔的版本不算進去。

同一條架構規則和 ADR 的寫法不一樣時，細節以 ADR 的「決定」一節為準，並回頭把兩邊改成一致。

## 沒有另寫 ADR 的架構規則

下面幾條規則沒有對應的 ADR，因為它們沒有需要比較的替代方案，或理由已經寫在別的 ADR 裡；當時的考量記在架構資料夾的 `.memlog.md`。

| 架構規則 | 內容 | 相關的說明在 |
|---|---|---|
| AD-18 | 設定、秘密與遷移 | 慣例類的規則；代號與顯示名稱的部分在 ADR-0010 |
| AD-26 | 稽核與刪除 | ADR-0002（管理者讀取）、ADR-0006（問答紀錄） |
| AD-27 | 寫入權限表只有一份 | ADR-0002（`actor` 與 viewer 是同一個物件）、ADR-0012（合併）、ADR-0020（權限表的規則種類與架構測試） |

## 還沒寫的 ADR

addendum 列的待寫清單裡，下面兩份留到之後：

- **夜間代理人（/goal）的隔離規則**（FR-47）：等真的要啟用夜間代理人時寫。
- **授權條款（MIT 或 Apache-2.0）**：要 Kevin 決定，repo 公開之前寫。

addendum 清單裡的其他項目已經併入上面的 ADR：「主機只做服務層、重運算在桌機」在 0003；「GitHub Actions 只做 CI、排程放主機」與「不引入 Redis、用 Procrastinate」在 0004；「資料來源先不做註冊表、LLM 與 embedding 可以先做」在 0001；「模型選擇」在 0009；「部署拓撲」在 0003；「試用環境的資料怎麼搬到正式環境」在 0003。
