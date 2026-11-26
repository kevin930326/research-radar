# ADR-0062：計數方塊端點、示範站 cube.json 與規則設定值（params）

- 狀態：提議（Story 3.15，等 Kevin 確認）
- 日期：2026-11-26
- 對應：Story 3.15；ADR-0007（示範站匯出的自我檢查）、ADR-0011（計數在資料庫、比例在前端一個模組）、ADR-0061（示範站匯出的檔案）；AD-19、AD-20；FR-40
- 補 ADR-0011／AD-19 沒寫到的細節：計數方塊從哪個端點、哪個檔拿；規則的設定值放哪裡、怎麼傳給前端；示範站的 cube.json 怎麼自我檢查。

## 背景

Story 1.10 已經在資料庫算好計數方塊（`radar/repo/stats.py` 的 `list_cube_counts`，每格是（期刊, 年份, 種類, key_a, key_b, 篇數）），
但還沒有端點、也沒有進示範站的匯出。資料涵蓋範圍（畫面 9）是第一個用到它的畫面：每本期刊每一年的摘要覆蓋率，
比例要在前端 `web/src/stats/` 算（AD-19），所以前端要拿到「計數＋規則的設定值＋快照」。
規則的設定值（ADR-0011 的追蹤起始年、當年度分界日、成長視窗、對照年數、覆蓋率門檻）原本沒有地方放。

## 考慮過的選項

### 一、成員版的端點

| | 做法 | 選了會怎樣 |
|---|---|---|
| A | 新增 `GET /api/v1/cube`，回 `{rows, params, snapshot}`；這次 `rows` 只放 `total` 與 `with_abstract` | 一個端點給之後的趨勢頁共用；要用別的種類時再加進 `rows` |
| B | 給資料涵蓋範圍一個專用端點，後端算好比例 | 違反 AD-19（比例只在前端一個模組算），示範站也得另算一次 |

### 二、規則的設定值放哪裡

| | 做法 | 選了會怎樣 |
|---|---|---|
| A | 新檔 `config/trend.yaml`（分界日、成長視窗、對照年數、覆蓋率門檻），追蹤起始年取 `journals.yaml`、時區取資料截止日用的那一個；組成 `TrendParams` 放進回應 | 只有一份來源，前端不寫死；改門檻不用改程式 |
| B | 前端寫死 | 成員版、示範站、之後的趨勢頁各一份，容易不一致 |

### 三、示範站的 cube.json

| | 做法 | 選了會怎樣 |
|---|---|---|
| A | 匯出時在同一個唯讀交易用 `PublicViewer` 讀計數方塊，寫成 `preview/<匯出編號>/cube.json`（白名單模型 `DemoCube`），列進 manifest；自我檢查加第 4 項：用 `papers-index.json` 逐格重算 `total`、`with_abstract`，對不上就不寫 | 和成員版同一種形狀；匯出的計數和清單一定一致 |
| B | 前端從 `papers-index.json` 自己數 | 成員版和示範站走兩條路；成員版也得另寫一份 |

## 決定

一 A、二 A、三 A。

- `GET /api/v1/cube`（operationId `cube`）：要登入（成員；權限表登記為豁免：回的是共用計數，沒有成員資料）、`no-store`。
  在唯讀快照交易（REPEATABLE READ）裡讀最新已發佈的快照與計數，兩個讀到同一個時間點。還沒有已發佈的快照時
  `snapshot` 是 null、`rows` 是空的（`CubeResponse` 的驗證擋「沒有快照卻有列」）；畫面把它當成「沒有載入成功」
  （State Patterns「9.」：空的不會發生）。
- `TrendParams`：`timezone`、`tracking_start_year`、`current_year_cutoff`（`MM-DD`，驗證是真的日期）、
  `growth_window_years`、`baseline_years`、`low_coverage_threshold`（0 到 1）。成員版回應與示範站 `cube.json` 都帶。
- 示範站：`cube.json` 進 `DEMO_FILE_MODELS`、`demo-openapi.json`、manifest 的必要檔；`DemoCube` 只收 `total` 與
  `with_abstract`、每格兩種都要有、不重複、`total > 0`、`with_abstract ≤ total`。`demo-openapi.json` 的版本
  `DEMO_FORMAT_VERSION` 從 1 改成 2，只是標明檔案格式改了；前端不讀這個版本號（實際的保護是：前端照 manifest 找
  `cube.json`，舊 manifest 沒有列它，涵蓋範圍頁顯示「沒有載入成功」）。共用的 `CubeRow` 形狀和成員版 `openapi.json`
  一樣（產生 schema 時拿掉 `default: null`，不改模型）。
- 自我檢查另外核對 manifest：列的檔和實際要寫的檔是同一批（列了卻沒有、有了卻沒列都失敗），`summary` 的篇數、
  有摘要篇數、有完整內容篇數用 `papers-index.json` 重算。
- 不改 #130 的鎖、接手、換 manifest 流程，只在換 manifest 之前多寫一個檔。讀「現在的 manifest」時，新版的模型
  讀不懂就再用上一版的模型（`PreviousDemoManifest`：沒有 `cube.json`，其他相同）讀一次，只為了知道它指向哪一次、
  能不能蓋；寫出的一律是新版。兩版都讀不懂照舊拒絕（`manifest_unreadable`）。

## 升版

已經有上一版匯出的環境（`preview/manifest.json` 沒有 `cube.json`），部署新版之後照這個順序：

1. 先備份 `preview/manifest.json`（複製一份到別處；不要搬走，搬走讀者就讀不到）。
2. 照平常的方式重新匯出一次：新版讀得懂上一版的 manifest，照 #130 的鎖與接手規則寫新的匯出目錄、換成新版
   manifest；舊的匯出目錄留著不動。
3. 確認新的 manifest 列了 `<匯出編號>/cube.json`，涵蓋範圍頁看得到數字，再刪備份。
4. 如果匯出失敗是 `manifest_unreadable`（manifest 不是上一版、也不是新版寫的），不要自己改它：人工確認它指向
   哪一次，再決定要不要移走重匯。

## 取捨

- 計數方塊這次只放兩種列；趨勢頁要的 `topic`、`method` 等之後加進同一個端點，回應會變大，到時再看要不要篩。
- 示範站的格式改了：已經發佈的舊匯出沒有 `cube.json`，新前端讀到舊 manifest 會顯示「沒有載入成功」，要照「升版」
  重新匯出一次。讀 manifest 多認得一個上一版，下次再改格式時，上上一版的模型可以拿掉。
- 自我檢查多一次逐格重算（索引欄式、篇數少，成本可以忽略）。
- 規則設定值多一個設定檔；`journals.yaml` 的 `start_year` 沿用，不重複。
